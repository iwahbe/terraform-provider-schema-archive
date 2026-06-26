import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from random import randrange, shuffle
from time import monotonic
from typing import Annotated, Dict, List, Optional

from packaging.version import InvalidVersion
from packaging.version import Version as SemVer
from pydantic import BaseModel, ConfigDict, Field, JsonValue

import dump
from duration import parse_duration
from registry import CrawlState, ProviderVersion, Registry
from registry_opentofu import REGISTRY as OPENTOFU, OpenTofuRegistry

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
ARCHIVE_JSON = os.path.join(REPO_ROOT, "archive.json")
SCHEMA_ARCHIVE = os.path.join(REPO_ROOT, "schema-archive")
SCHEMA_LATEST = os.path.join(REPO_ROOT, "schema-latest")

DEFAULT_DUMP_TIMEOUT = 600
BACKOFF_BASE = 30
BACKOFF_CAP = 1800
MAX_CONSECUTIVE_RETRIES = 10

RESULT_COLUMN = 72
SYMBOLS = {"done": "✅", "retry": "🕐", "failure": "☠️"}


class Status(str, Enum):
    pending = "pending"
    done = "done"
    retry = "retry"
    failure = "failure"


class Version(BaseModel):
    version: str
    status: Status


class Provider(BaseModel):
    registry: str
    org: str
    name: str
    versions: List[Version]


class Archive(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    crawl_state: Annotated[Dict[str, JsonValue], Field(alias="crawl-state")]
    status: List[Provider]


def registries() -> Dict[str, Registry]:
    return {OPENTOFU: OpenTofuRegistry()}


def version_key(version: str):
    text = version[1:] if version.startswith("v") else version
    try:
        return (1, SemVer(text))
    except InvalidVersion:
        return (0, text)


def is_prerelease(version: str) -> bool:
    text = version[1:] if version.startswith("v") else version
    return "-" in text.split("+", 1)[0]


def latest_key(version: str):
    "OpenTofu's notion of latest: released versions outrank pre-releases, then by version."
    return (not is_prerelease(version), version_key(version))


def load_archive() -> Archive:
    if not os.path.exists(ARCHIVE_JSON):
        return Archive(crawl_state={}, status=[])
    with open(ARCHIVE_JSON) as f:
        return Archive.model_validate_json(f.read())


def save_archive(archive: Archive):
    archive.status.sort(key=lambda p: (p.registry, p.org, p.name))
    for provider in archive.status:
        provider.versions.sort(key=lambda v: version_key(v.version), reverse=True)
    with open(ARCHIVE_JSON, "w") as f:
        f.write(archive.model_dump_json(by_alias=True, indent=2))
        f.write("\n")


def populate(archive: Archive):
    known = CrawlState()
    index: Dict[tuple, Provider] = {}
    for provider in archive.status:
        index[(provider.registry, provider.org, provider.name)] = provider
        for version in provider.versions:
            known.add(ProviderVersion(provider.registry, provider.org, provider.name, version.version))

    def add(pv: ProviderVersion):
        if pv in known:
            return
        known.add(pv)
        key = (pv.registry, pv.org, pv.provider)
        provider = index.get(key)
        if provider is None:
            provider = Provider(registry=pv.registry, org=pv.org, name=pv.provider, versions=[])
            index[key] = provider
            archive.status.append(provider)
        provider.versions.append(Version(version=pv.version, status=Status.pending))

    for name, registry in registries().items():
        registry.load(archive.crawl_state.get(name))
        registry.generate(add)
        archive.crawl_state[name] = registry.store()


def build_queue(archive: Archive) -> List[tuple]:
    "Each provider's latest released version first, then older versions backfilled latest-first; the provider worked on is random throughout."
    latest: List[tuple] = []
    backfill: List[deque] = []
    for provider in archive.status:
        if not provider.versions:
            continue
        top = max(provider.versions, key=lambda v: latest_key(v.version))
        if top.status in (Status.pending, Status.retry):
            latest.append((provider, top))
        rest = [v for v in provider.versions
                if v is not top and v.status in (Status.pending, Status.retry)]
        rest.sort(key=lambda v: latest_key(v.version), reverse=True)
        if rest:
            backfill.append(deque((provider, v) for v in rest))
    shuffle(latest)
    return latest + _interleave_random(backfill)


def _interleave_random(queues: List[deque]) -> List[tuple]:
    "Drain per-provider latest-first queues, taking from a random provider at each step."
    queues = [q for q in queues if q]
    result: List[tuple] = []
    while queues:
        i = randrange(len(queues))
        result.append(queues[i].popleft())
        if not queues[i]:
            queues.pop(i)
    return result


class Signals:
    def __init__(self):
        self.count = 0
        self.container: Optional[str] = None

    def install(self):
        signal.signal(signal.SIGINT, self.__handle)

    def __handle(self, signum, frame):
        self.count += 1
        if self.count == 1:
            self.__notice("finishing current dump; press Ctrl-C again to abort now")
        elif self.container is not None:
            self.__notice("aborting current dump")
            subprocess.run(["docker", "kill", self.container], capture_output=True)

    def __notice(self, text: str):
        "Print a message two lines below the in-progress line, then restore the cursor so it can finish."
        if sys.stderr.isatty():
            sys.stderr.write("\x1b7\x1b[2B\r\x1b[2K" + text + "\x1b8")
        else:
            sys.stderr.write("\n" + text + "\n")
        sys.stderr.flush()

    def stop_requested(self) -> bool:
        return self.count >= 1

    def aborted(self) -> bool:
        return self.count >= 2


def container_name(provider: Provider, version: Version) -> str:
    raw = f"dump-{provider.org}-{provider.name}-{version.version}-{os.getpid()}"
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)


def per_dump_timeout(deadline: Optional[float]) -> float:
    if deadline is None:
        return DEFAULT_DUMP_TIMEOUT
    return max(1, min(DEFAULT_DUMP_TIMEOUT, deadline - monotonic()))


def backoff(attempt: int, signals: Signals, deadline: Optional[float]):
    delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
    waited = 0
    while waited < delay:
        if signals.stop_requested() or (deadline is not None and monotonic() >= deadline):
            return
        time.sleep(1)
        waited += 1


def write_outputs(provider: Provider, version: Version, result: dump.DumpResult):
    directory = os.path.join(SCHEMA_ARCHIVE, provider.registry, provider.org, provider.name, version.version)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "stdout.txt"), "w") as f:
        f.write(result.stdout)
    with open(os.path.join(directory, "stderr.txt"), "w") as f:
        f.write(result.stderr)
    if result.schema is not None:
        with open(os.path.join(directory, "schema.json"), "wb") as f:
            f.write(result.schema)
    metadata_status = "success" if result.status == "done" else result.status
    metadata = {"timestamp": datetime.now(timezone.utc).isoformat(), "status": metadata_status}
    if result.format_version is not None:
        metadata["format_version"] = result.format_version
    with open(os.path.join(directory, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")


class ProgressLine:
    "Emits one stderr line per dump: `Archiving <addr>....<symbol>`, a dot per tick, result right-aligned."

    def __init__(self, text: str):
        self.width = len(text)
        print(text, end="", file=sys.stderr, flush=True)

    def tick(self):
        print(".", end="", file=sys.stderr, flush=True)
        self.width += 1

    def finish(self, symbol: str):
        print(" " * max(1, RESULT_COLUMN - self.width) + symbol, file=sys.stderr, flush=True)


def run_dumps(archive: Archive, deadline: Optional[float], max_count: Optional[int], signals: Signals):
    queue = build_queue(archive)
    if not queue:
        return
    dump.build_image()
    dumped = 0
    consecutive_retries = 0
    for provider, version in queue:
        if signals.stop_requested():
            break
        if deadline is not None and monotonic() >= deadline:
            break
        if max_count is not None and dumped >= max_count:
            break
        name = container_name(provider, version)
        signals.container = name
        line = ProgressLine(f"Archiving {provider.registry}/{provider.org}/{provider.name}@{version.version}")
        result = dump.dump(
            ProviderVersion(provider.registry, provider.org, provider.name, version.version),
            name,
            per_dump_timeout(deadline),
            line.tick,
        )
        signals.container = None
        if signals.aborted():
            print(file=sys.stderr)
            break
        line.finish(SYMBOLS[result.status])
        write_outputs(provider, version, result)
        version.status = Status(result.status)
        if version.status == Status.done:
            update_latest_symlink(provider)
        save_archive(archive)
        dumped += 1
        if result.status == "retry":
            consecutive_retries += 1
            if consecutive_retries >= MAX_CONSECUTIVE_RETRIES:
                print("too many consecutive retryable errors; pausing", file=sys.stderr)
                break
            backoff(consecutive_retries, signals, deadline)
        else:
            consecutive_retries = 0


def update_latest_symlink(provider: Provider):
    "Point schema-latest at the provider's highest successfully dumped version."
    done = [v for v in provider.versions if v.status == Status.done]
    if not done:
        return
    latest = max(done, key=lambda v: latest_key(v.version))
    link = os.path.join(SCHEMA_LATEST, provider.registry, provider.org, provider.name)
    target = os.path.join(SCHEMA_ARCHIVE, provider.registry, provider.org, provider.name, latest.version)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink(os.path.relpath(target, os.path.dirname(link)), link)


def parse_args():
    parser = argparse.ArgumentParser(description="Maintain the Terraform provider schema archive.")
    parser.add_argument("--timeout", type=parse_duration, default=None,
                        help="stop after this long, e.g. 1h30m or 2m15s")
    parser.add_argument("--max", type=int, default=None,
                        help="stop after dumping this many provider versions")
    return parser.parse_args()


def main():
    args = parse_args()
    signals = Signals()
    signals.install()
    deadline = monotonic() + args.timeout if args.timeout is not None else None

    archive = load_archive()
    populate(archive)
    save_archive(archive)
    run_dumps(archive, deadline, args.max, signals)


if __name__ == "__main__":
    main()
