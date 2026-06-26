import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

from registry import ProviderVersion

IMAGE = "terraform-schema-archive-dump"
_DOCKERFILE_DIR = os.path.dirname(__file__)

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
    status: str  # "done" | "retry" | "failure"
    stdout: str
    stderr: str
    schema: Optional[bytes]


def build_image():
    subprocess.run(
        ["docker", "build", "-t", IMAGE, "-f", os.path.join(_DOCKERFILE_DIR, "Dockerfile"), _DOCKERFILE_DIR],
        check=True,
    )


def dump(pv: ProviderVersion, container_name: str, timeout: float) -> DumpResult:
    source = f"{pv.registry}/{pv.org}/{pv.provider}"
    version = pv.version[1:] if pv.version.startswith("v") else pv.version
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "main.tf"), "w") as f:
            f.write(_MAIN_TF.format(source=source, version=version))
        cmd = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", "bridge",
            "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{work}:/work", "-w", "/work",
            "--entrypoint", "/bin/sh", IMAGE,
            "-c", "tofu init -input=false -no-color && tofu providers schema -json > schema.json",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, start_new_session=True,
            )
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            subprocess.run(["docker", "kill", container_name], capture_output=True)
            return DumpResult("retry", _text(e.stdout), _text(e.stderr) + "\ndump timed out", None)

        schema = _read_schema(os.path.join(work, "schema.json"))
        status = _classify(returncode, stderr, schema)
        return DumpResult(status, stdout, stderr, schema if status == "done" else None)


def _classify(returncode: int, stderr: str, schema: Optional[bytes]) -> str:
    if returncode == 0 and schema is not None:
        return "done"
    if any(marker in stderr.lower() for marker in _RETRYABLE_MARKERS):
        return "retry"
    return "failure"


def _read_schema(path: str) -> Optional[bytes]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    try:
        json.loads(data)
    except json.JSONDecodeError:
        return None
    return data


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode(errors="replace")
