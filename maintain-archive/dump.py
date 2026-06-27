import gzip
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

from registry import ProviderVersion

IMAGE = "terraform-schema-archive-dump"
_DOCKERFILE_DIR = os.path.dirname(__file__)

# GitHub blocks any push containing a file larger than 100 MiB. Schemas are stored
# gzip-compressed; a schema whose compressed size would exceed this cap is rejected
# rather than written, so an oversized schema can never be committed or block a push.
MAX_SCHEMA_GZ_BYTES = 80 * 1024 * 1024

_RETRYABLE_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "i/o timeout",
    "temporary failure",
    "tls handshake",
    "no such host",
    "502 bad gateway",
    "503 service",
    "504 gateway",
    "eof",
)

_MAIN_TF = """terraform {{
  required_providers {{
    p = {{
      source  = "{source}"
      version = "{version}"
    }}
  }}
}}
"""


@dataclass
class DumpResult:
    status: str  # "done" | "retry" | "failure" | "rejected"
    stdout: str
    stderr: str
    schema_gz: Optional[bytes]  # gzip-compressed schema, present only when status == "done"
    format_version: Optional[str]


def build_image():
    subprocess.run(
        ["docker", "build", "-t", IMAGE, "-f", os.path.join(_DOCKERFILE_DIR, "Dockerfile"), _DOCKERFILE_DIR],
        check=True,
    )


def dump(pv: ProviderVersion, container_name: str, timeout: float) -> DumpResult:
    source = f"{pv.registry}/{pv.org}/{pv.provider}"
    version = pv.version[1:] if pv.version.startswith("v") else pv.version
    with tempfile.TemporaryDirectory() as work:
        # The container runs tofu as root but with --cap-drop ALL, so it lacks
        # CAP_DAC_OVERRIDE and is subject to ordinary permission checks. TemporaryDirectory
        # creates `work` mode 0700 owned by the host user, which the container's root cannot
        # enter — tofu then aborts with "stat .: permission denied". Make the bind-mounted
        # working directory world-accessible so the sandboxed process can use it.
        os.chmod(work, 0o777)
        with open(os.path.join(work, "main.tf"), "w") as f:
            f.write(_MAIN_TF.format(source=source, version=version))
        cmd = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", "bridge",
            "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{work}:/work", "-w", "/work",
            "--entrypoint", "/bin/sh", IMAGE,
            "-c", "tofu init -input=false -no-color && tofu providers schema -json -no-color > schema.json",
        ]
        out_path, err_path = os.path.join(work, "dump-stdout"), os.path.join(work, "dump-stderr")
        timed_out = False
        with open(out_path, "w") as out, open(err_path, "w") as err:
            proc = subprocess.Popen(cmd, stdout=out, stderr=err, start_new_session=True)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", container_name], capture_output=True)
                proc.wait()
                timed_out = True

        stdout, stderr = _read(out_path), _read(err_path)
        if timed_out:
            return DumpResult("retry", stdout, stderr + "\ndump timed out", None, None)
        format_version, schema = _extract(os.path.join(work, "schema.json"), source)
        status = _classify(proc.returncode, stderr, schema)
        if status != "done":
            return DumpResult(status, stdout, stderr, None, None)
        status, schema_gz = finalize_schema(schema)
        if status == "rejected":
            note = f"\nrejected: compressed schema exceeds the {MAX_SCHEMA_GZ_BYTES}-byte limit"
            return DumpResult("rejected", stdout, stderr + note, None, None)
        return DumpResult("done", stdout, stderr, schema_gz, format_version)


def finalize_schema(schema: bytes, limit: int = MAX_SCHEMA_GZ_BYTES) -> tuple[str, Optional[bytes]]:
    "Gzip a successful schema dump, rejecting it when the compressed size exceeds `limit`."
    schema_gz = gzip.compress(schema, 9, mtime=0)
    if len(schema_gz) > limit:
        return "rejected", None
    return "done", schema_gz


def _classify(returncode: int, stderr: str, schema: Optional[bytes]) -> str:
    if returncode == 0 and schema is not None:
        return "done"
    if any(marker in stderr.lower() for marker in _RETRYABLE_MARKERS):
        return "retry"
    return "failure"


def _extract(path: str, source: str) -> tuple[Optional[str], Optional[bytes]]:
    "Pull format_version and the schema of `source` out of the full dump."
    if not os.path.exists(path):
        return None, None
    with open(path, "rb") as f:
        data = f.read()
    try:
        document = json.loads(data)
    except json.JSONDecodeError:
        return None, None
    schemas = document.get("provider_schemas") if isinstance(document, dict) else None
    if not isinstance(schemas, dict) or source not in schemas:
        return None, None
    schema = json.dumps(schemas[source], indent=2).encode() + b"\n"
    return document.get("format_version"), schema


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()
