# SPDX-License-Identifier: MPL-2.0
"""docker-gc's machine side: its settings (environment), the docker commands, the last-used state, the run lock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import partial
import json
import os
import sys
import time
from typing import Any

from btrfs.docker_gc.domain import HEARTBEAT_SECONDS, ts
from cst_core.adapters import SubprocessCommandRunner
from cst_core.adapters.files import write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.domain.text import is_yes
from cst_core.ports import CommandResult, CommandRunnerPort

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
SYSTEMCTL_TIMEOUT = 10  # seconds for systemctl is-active
RUNNER: CommandRunnerPort = SubprocessCommandRunner()

# One journal line per message, "<prio>docker-gc: msg".
log = partial(journal_log, tag="docker-gc")


def docker(*args: str, timeout: int = 600, check: bool = True) -> CommandResult:
    """docker ARGS (stdin ours; output that is not UTF-8 raises). No answer
    within timeout, or a failure with check: RuntimeError. A docker that
    cannot be started raises (as subprocess.run did)."""
    r = RUNNER.run(["docker", *args], timeout=timeout, stdin="inherit", decode_errors="strict")
    if r.fault == "timeout":
        raise RuntimeError(f"docker {' '.join(args[:2])}: no answer in {timeout}s") from r.cause
    if r.cause is not None:
        raise r.cause
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
    write_atomic(LAST_USED, json.dumps(st), mode="umask", durable="file", relabel=False)


def save_summary(summary: Mapping[str, object]) -> None:
    """LAST_RUN, replaced atomically (it was rewritten in place): what status shows of the last run."""
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    write_atomic(LAST_RUN, json.dumps(summary), mode="umask", durable=False, relabel=False)


def recorder_alive(st: Mapping[str, Any]) -> bool:
    """The recorder runs and wrote its heartbeat lately. A systemctl that does
    not answer counts as a recorder that is not running: images are held back."""
    done = RUNNER.run(
        ["systemctl", "-q", "is-active", "docker-gc-recorder.service"], timeout=SYSTEMCTL_TIMEOUT, output="inherit"
    )
    if done.fault == "timeout":
        log(
            4,
            f"systemctl is-active docker-gc-recorder.service: no answer in {SYSTEMCTL_TIMEOUT}s; images are held back",
        )
        return False
    if done.cause is not None:
        raise done.cause
    return done.returncode == 0 and time.time() - st.get("heartbeat", 0) < 3 * HEARTBEAT_SECONDS


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


def remove_image(img: Mapping[str, Any], plan_at: float) -> str:
    """Re-checked right before removal: still unused (fresh container list and
    recorder state), and each tag still pointing at this very image."""
    iid = img["id"]
    if iid in {c.get("Image") for c in inspect_all("container", ("ps", "-aq", "--no-trunc"))}:
        return "now used by a container"
    if load_state()["images"].get(iid, 0) > plan_at - 60:
        return "used meanwhile"
    for t in img["tags"]:
        r = docker("image", "inspect", "--format", "{{.Id}}", t, check=False)
        if r.returncode != 0 or r.stdout.strip() != iid:
            continue  # re-pulled or retagged meanwhile: that tag is not ours
        r = docker("image", "rm", t, check=False)
        if r.returncode != 0:
            return f"cannot remove {t}: {r.stderr.strip()[:100]}"
    r = docker("image", "inspect", iid, check=False)
    if r.returncode == 0:
        try:
            left = json.loads(r.stdout)[0].get("RepoTags") or []
        except ValueError, IndexError:
            left = ["?"]
        if left:
            return "got new tags meanwhile"
        r = docker("image", "rm", iid, check=False)
        if r.returncode != 0 and "no such image" not in r.stderr.lower():
            return r.stderr.strip()[:120]
    return ""


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
