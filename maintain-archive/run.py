import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
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

TICK_SECONDS = 30
RESULT_COLUMN = 72
SYMBOLS = {"done": "✅", "retry": "🕐", "failure": "☠️", "rejected": "🚫"}


class Status(str, Enum):
    pending = "pending"
    done = "done"
    retry = "retry"
    failure = "failure"
    rejected = "rejected"


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
    def __init__(self, display: "Display"):
        self.count = 0
        self.__display = display
        self.__containers: set = set()
        self.__lock = threading.Lock()

    def install(self):
        signal.signal(signal.SIGINT, self.__handle)

    def __handle(self, signum, frame):
        self.count += 1
        if self.count == 1:
            self.__display.message("finishing current dumps; press Ctrl-C again to abort now")
        else:
            self.__display.message("aborting current dumps")
            with self.__lock:
                containers = list(self.__containers)
            for name in containers:
                subprocess.run(["docker", "kill", name], capture_output=True)

    def add_container(self, name: str):
        with self.__lock:
            self.__containers.add(name)

    def remove_container(self, name: str):
        with self.__lock:
            self.__containers.discard(name)

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


def backoff(attempt: int, stop):
    delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
    waited = 0
    while waited < delay:
        if stop():
            return
        time.sleep(1)
        waited += 1


def org_prefix(org: str) -> str:
    "First two characters of an org, used to shard org directories so no folder grows unbounded."
    return org[:2]


def archive_dir(registry: str, org: str, provider: str, version: str) -> str:
    return os.path.join(SCHEMA_ARCHIVE, registry, org_prefix(org), org, provider, version)


def latest_link(registry: str, org: str, provider: str) -> str:
    return os.path.join(SCHEMA_LATEST, registry, org_prefix(org), org, provider)


def write_outputs(provider: Provider, version: Version, result: dump.DumpResult):
    directory = archive_dir(provider.registry, provider.org, provider.name, version.version)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "stdout.txt"), "w") as f:
        f.write(result.stdout)
    with open(os.path.join(directory, "stderr.txt"), "w") as f:
        f.write(result.stderr)
    if result.schema_gz is not None:
        with open(os.path.join(directory, "schema.json.gz"), "wb") as f:
            f.write(result.schema_gz)
    metadata_status = "success" if result.status == "done" else result.status
    metadata = {"timestamp": datetime.now(timezone.utc).isoformat(), "status": metadata_status}
    if result.format_version is not None:
        metadata["format_version"] = result.format_version
    with open(os.path.join(directory, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")


class Display:
    "A live region drawn at the bottom: in-progress `Archiving …` lines, then any sticky footer messages, with the cursor resting at the start of the line below it. Completed dumps flush into permanent scrollback above the region; footer messages stay last. Each update redraws in place: cursor up to the top, rewrite, back down."

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.__lock = threading.Lock()
        self.__active: Dict[int, dict] = {}
        self.__footer: List[str] = []
        self.__region = 0
        self.__next = 0
        self.__last: List[str] = []

    def start(self, prefix: str) -> int:
        with self.__lock:
            slot = self.__next
            self.__next += 1
            self.__active[slot] = {"prefix": prefix, "start": monotonic()}
            self.__redraw([])
            return slot

    def finish(self, slot: int, symbol: str):
        with self.__lock:
            info = self.__active.pop(slot, None)
            if info is not None:
                self.__redraw([self.__line(info, symbol)])

    def message(self, text: str):
        with self.__lock:
            if self.enabled:
                self.__footer.append(text)
                self.__redraw([])
            else:
                sys.stderr.write(text + "\n")
                sys.stderr.flush()

    def refresh(self):
        with self.__lock:
            if self.__active_lines() != self.__last:
                self.__redraw([])

    def __line(self, info: dict, symbol: Optional[str]) -> str:
        text = info["prefix"] + "." * int((monotonic() - info["start"]) // TICK_SECONDS)
        if symbol is None:
            return text
        return text + " " * max(1, RESULT_COLUMN - len(text)) + symbol

    def __active_lines(self) -> List[str]:
        return [self.__line(info, None) for info in self.__active.values()]

    def __redraw(self, committed: List[str]):
        active_lines = self.__active_lines()
        if not self.enabled:
            for line in committed:
                sys.stderr.write(line + "\n")
            if committed:
                sys.stderr.flush()
            self.__last = active_lines
            return
        drawn = active_lines + self.__footer
        out = []
        if self.__region:
            out.append(f"\x1b[{self.__region}A")
        for line in committed + drawn:
            out.append("\r\x1b[2K" + line + "\n")
        leftover = self.__region - (len(committed) + len(drawn))
        if leftover > 0:
            out.append("\r\x1b[2K\n" * leftover)
            out.append(f"\x1b[{leftover}A")
        out.append("\r")
        sys.stderr.write("".join(out))
        sys.stderr.flush()
        self.__region = len(drawn)
        self.__last = active_lines


def run_dumps(archive: Archive, deadline: Optional[float], max_count: Optional[int],
              jobs: int, signals: Signals, display: Display):
    queue = build_queue(archive)
    if not queue:
        return
    dump.build_image()

    work = deque(queue)
    state_lock = threading.Lock()
    state = {"claimed": 0, "retries": 0, "stop": False}

    def should_stop() -> bool:
        return (signals.stop_requested() or state["stop"]
                or (deadline is not None and monotonic() >= deadline))

    def claim():
        with state_lock:
            if should_stop() or not work:
                return None
            if max_count is not None and state["claimed"] >= max_count:
                return None
            state["claimed"] += 1
            return work.popleft()

    def worker():
        while True:
            item = claim()
            if item is None:
                return
            provider, version = item
            pv = ProviderVersion(provider.registry, provider.org, provider.name, version.version)
            name = container_name(provider, version)
            slot = display.start(f"Archiving {provider.registry}/{provider.org}/{provider.name}@{version.version}")
            signals.add_container(name)
            result = dump.dump(pv, name, per_dump_timeout(deadline))
            signals.remove_container(name)
            if signals.aborted():
                return
            display.finish(slot, SYMBOLS[result.status])
            with state_lock:
                write_outputs(provider, version, result)
                version.status = Status(result.status)
                if version.status == Status.done:
                    update_latest_symlink(provider)
                save_archive(archive)
                if result.status == "retry":
                    state["retries"] += 1
                    attempt = state["retries"]
                    paused = attempt >= MAX_CONSECUTIVE_RETRIES
                    state["stop"] = state["stop"] or paused
                else:
                    state["retries"] = 0
                    attempt = 0
                    paused = False
            if paused:
                display.message("too many consecutive retryable errors; pausing")
            elif attempt and not should_stop():
                backoff(attempt, should_stop)

    ticker_stop = threading.Event()

    def ticker():
        while not ticker_stop.wait(1):
            display.refresh()

    ticker_thread = threading.Thread(target=ticker, name="ticker", daemon=True)
    threads = [threading.Thread(target=worker, name=f"dump-{i}") for i in range(jobs)]
    ticker_thread.start()
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        ticker_stop.set()
        ticker_thread.join()


def update_latest_symlink(provider: Provider):
    "Point schema-latest at the provider's highest successfully dumped version."
    done = [v for v in provider.versions if v.status == Status.done]
    if not done:
        return
    latest = max(done, key=lambda v: latest_key(v.version))
    link = latest_link(provider.registry, provider.org, provider.name)
    target = archive_dir(provider.registry, provider.org, provider.name, latest.version)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink(os.path.relpath(target, os.path.dirname(link)), link)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description="Maintain the Terraform provider schema archive.")
    parser.add_argument("--timeout", type=parse_duration, default=None,
                        help="stop after this long, e.g. 1h30m or 2m15s")
    parser.add_argument("--max", type=int, default=None,
                        help="stop after dumping this many provider versions")
    parser.add_argument("--jobs", "-j", type=positive_int, default=1,
                        help="number of dumps to run in parallel")
    return parser.parse_args()


def main():
    args = parse_args()
    display = Display(sys.stderr.isatty())
    signals = Signals(display)
    signals.install()
    deadline = monotonic() + args.timeout if args.timeout is not None else None

    archive = load_archive()
    populate(archive)
    save_archive(archive)
    run_dumps(archive, deadline, args.max, args.jobs, signals, display)


if __name__ == "__main__":
    main()
