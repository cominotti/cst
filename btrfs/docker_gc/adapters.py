"""docker-gc's machine side: its settings (environment), the docker commands, the last-used state, the run lock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import partial
import json
import os
import subprocess
import sys
import time
from typing import Any

from btrfs.docker_gc.domain import HEARTBEAT_SECONDS, ts
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.domain.text import is_yes

STATE_DIR = os.environ.get("DOCKER_GC_STATE", "/var/lib/docker-gc")  # override: tests
LAST_USED = os.path.join(STATE_DIR, "last-used.json")
LAST_RUN = os.path.join(STATE_DIR, "last-run.json")
LOCK = "/run/docker-gc.lock"
KEEP_CONTAINER_DAYS = int(os.environ.get("KEEP_CONTAINER_DAYS", 7))
KEEP_IMAGE_DAYS = int(os.environ.get("KEEP_IMAGE_DAYS", 14))
KEEP_BUILD_CACHE_DAYS = int(os.environ.get("KEEP_BUILD_CACHE_DAYS", 14))
PROTECTED = tuple(
    r.strip() + "/" for r in os.environ.get("DOCKER_GC_PROTECT", "registry.example.com").split(",") if r.strip()
)
FLUSH_SECONDS = int(os.environ.get("DOCKER_GC_FLUSH", 30))

# One journal line per message, "<prio>docker-gc: msg".
log = partial(journal_log, tag="docker-gc")


def docker(*args: str, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        r = subprocess.run(["docker", *args], check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"docker {' '.join(args[:2])}: no answer in {timeout}s") from e
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return r


def is_protected(im: Mapping[str, Any]) -> bool:
    """An image of a protected registry (DOCKER_GC_PROTECT), by tag or digest."""
    refs = (im.get("RepoTags") or []) + (im.get("RepoDigests") or [])
    return any(r.startswith(PROTECTED) for r in refs)


def inspect_all(kind: str, list_args: Sequence[str]) -> list[dict[str, Any]]:
    """Inspect every object listed. Objects that vanished in between are
    dropped; any other gap aborts (a container missing from the result would
    leave its image unprotected)."""
    ids = sorted(set(docker(*list_args).stdout.split()))
    out = []
    for i in range(0, len(ids), 100):
        r = docker(kind, "inspect", *ids[i : i + 100], check=False)
        try:
            out.extend(json.loads(r.stdout or "[]"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{kind} inspect: unreadable output ({e})") from e
    got = {o.get("Id") or o.get("Name") for o in out}
    missing = set(ids) - got
    if missing and missing & set(docker(*list_args).stdout.split()):
        raise RuntimeError(f"{kind} inspect failed for {len(missing)} object(s); nothing done")
    return out


# ---- last-used state ------------------------------------------------------------------
def load_state() -> dict[str, Any]:
    try:
        with open(LAST_USED) as f:
            st = json.load(f)
        if isinstance(st.get("images"), dict) and isinstance(st.get("since"), (int, float)):
            return st
    except OSError, ValueError:
        pass
    return {"since": time.time(), "images": {}, "heartbeat": 0}


def save_state(st: Mapping[str, Any]) -> None:
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    tmp = LAST_USED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LAST_USED)


def recorder_alive(st: Mapping[str, Any]) -> bool:
    active = subprocess.run(["systemctl", "-q", "is-active", "docker-gc-recorder.service"], check=False).returncode == 0
    return active and time.time() - st.get("heartbeat", 0) < 3 * HEARTBEAT_SECONDS


def container_use(c: Mapping[str, Any]) -> float:
    """Last moment a container was using its image."""
    s = c.get("State") or {}
    if s.get("Running"):
        return time.time()
    return max(ts(s.get("StartedAt")), ts(s.get("FinishedAt")), ts(c.get("Created")))


def df() -> dict[str, str]:
    r = docker("system", "df", "--format", "{{json .}}", check=False)
    out = {}
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
            out[d.get("Type", "?")] = d.get("Size", "?")
        except ValueError:
            pass
    return out


def locked() -> int:
    """The run lock's fd, held until the process ends; exit 0 when another run holds it."""
    fd = take_lock(LOCK, wait=False, mode=0o644, nofollow=False)
    if fd is None:
        log(5, "another docker-gc run is active; skipped")
        sys.exit(0)
    return fd


def docker_up() -> bool:
    if docker("info", "--format", "{{.ID}}", check=False, timeout=60).returncode != 0:
        print("docker is not running; nothing to do")
        return False
    return True


def ask(q: str) -> bool:
    try:
        return is_yes(input(q))
    except EOFError:
        return False
