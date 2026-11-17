# SPDX-License-Identifier: MPL-2.0
"""docker-gc's machine side: the docker commands and their JSON, the state files and the run lock,
systemctl and /proc/uptime, the terminal and the journal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
import signal
import subprocess
import sys
import threading
import time

from btrfs.docker_gc.domain import (
    Container,
    DockerEvent,
    EventsEnd,
    GcPaths,
    GcState,
    Image,
    RunSummary,
    Volume,
    ts,
)
from btrfs.docker_gc.ports import EventSink
from cst_core.adapters.files import write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.domain.text import is_yes
from cst_core.ports import CommandResult, CommandRunnerPort

SYSTEMCTL_TIMEOUT = 10  # seconds for systemctl is-active
EVENTS_ARGV = ("docker", "events", "--format", "{{json .}}", "--filter", "type=container", "--filter", "type=image")


# ---- docker's JSON, read without trusting its shape ---------------------------------
def _obj(value: object) -> dict[str, object]:
    """value when it is a JSON object, else an empty one (Docker's null)."""
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()}


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strs(value: object) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _num(value: object) -> float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _time(value: object) -> float:
    return ts(value if isinstance(value, str) else None)


def container_of(o: Mapping[str, object]) -> Container:
    """A Container from one object of `docker container inspect`."""
    config, state = _obj(o.get("Config")), _obj(o.get("State"))
    policy = _obj(_obj(o.get("HostConfig")).get("RestartPolicy"))
    listed = o.get("Mounts")
    mounts = [_obj(m) for m in listed if isinstance(m, dict)] if isinstance(listed, list) else []
    return Container(
        id=_str(o.get("Id")),
        name=_str(o.get("Name")).lstrip("/"),
        image_id=_str(o.get("Image")),
        image_ref=_str(config.get("Image", "?")),
        status=_str(state.get("Status")),
        running=bool(state.get("Running")),
        started=_time(state.get("StartedAt")),
        finished=_time(state.get("FinishedAt")),
        created=_time(o.get("Created")),
        label_keys=frozenset(_obj(config.get("Labels"))),
        restart=_str(policy.get("Name")) or "no",
        volumes=frozenset(_str(m.get("Name")) for m in mounts if m.get("Type") == "volume") - {""},
    )


def volume_of(o: Mapping[str, object]) -> Volume:
    return Volume(name=_str(o.get("Name")), anonymous="com.docker.volume.anonymous" in _obj(o.get("Labels")))


def image_of(o: Mapping[str, object]) -> Image:
    size = _num(o.get("Size")) or 0
    return Image(
        id=_str(o.get("Id")),
        tags=_strs(o.get("RepoTags")),
        digests=_strs(o.get("RepoDigests")),
        size=int(size),
        last_tag_time=_time(_obj(o.get("Metadata")).get("LastTagTime")),
    )


def event_of(line: str) -> DockerEvent | None:
    """One line of `docker events --format '{{json .}}'`; None for a line that is not an event."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if not isinstance(ev, dict):
        return None
    ev = _obj(ev)
    actor = _obj(ev.get("Actor"))
    return DockerEvent(
        kind=_str(ev.get("Type")),
        action=_str(ev.get("Action")),
        image_attr=_str(_obj(actor.get("Attributes")).get("image")),
        actor_id=_str(actor.get("ID")),
    )


class DockerCli:
    """DockerPort over the docker command (stdin ours; bytes that are not UTF-8 become U+FFFD)."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self._runner = runner

    def run(self, *args: str, timeout: int = 600, check: bool = True) -> CommandResult:
        """docker ARGS. No answer within timeout, or a failure with check: RuntimeError.
        A docker that cannot be started raises (as subprocess.run did)."""
        r = self._runner.run(["docker", *args], timeout=timeout, stdin="inherit")
        if r.fault == "timeout":
            raise RuntimeError(f"docker {' '.join(args[:2])}: no answer in {timeout}s") from r.cause
        if r.cause is not None:
            raise r.cause
        if check and r.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
        return r

    def inspect_all(self, kind: str, list_args: Sequence[str]) -> list[dict[str, object]]:
        """Inspect every object listed. Objects that vanished in between are
        dropped; any other gap aborts (a container missing from the result would
        leave its image unprotected)."""
        ids = sorted(set(self.run(*list_args).stdout.split()))
        out: list[dict[str, object]] = []
        for i in range(0, len(ids), 100):
            r = self.run(kind, "inspect", *ids[i : i + 100], check=False)
            try:
                found = json.loads(r.stdout or "[]")
            except json.JSONDecodeError as e:
                raise RuntimeError(f"{kind} inspect: unreadable output ({e})") from e
            out.extend(_obj(o) for o in (found if isinstance(found, list) else []))
        got = {o.get("Id") or o.get("Name") for o in out}
        missing = set(ids) - got
        if missing and missing & set(self.run(*list_args).stdout.split()):
            raise RuntimeError(f"{kind} inspect failed for {len(missing)} object(s); nothing done")
        return out

    def is_up(self) -> bool:
        return self.run("info", "--format", "{{.ID}}", check=False, timeout=60).returncode == 0

    def containers(self) -> list[Container]:
        return [container_of(o) for o in self.inspect_all("container", ("ps", "-aq", "--no-trunc"))]

    def volumes(self) -> list[Volume]:
        return [volume_of(o) for o in self.inspect_all("volume", ("volume", "ls", "-q"))]

    def images(self) -> list[Image]:
        return [image_of(o) for o in self.inspect_all("image", ("image", "ls", "-aq", "--no-trunc"))]

    def image_id(self, ref: str, *, timeout: int) -> str:
        r = self.run("image", "inspect", "--format", "{{.Id}}", ref, check=False, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""

    def image_tags(self, image_id: str) -> list[str] | None:
        r = self.run("image", "inspect", image_id, check=False)
        if r.returncode != 0:
            return None
        try:
            found = json.loads(r.stdout)
            first = found[0] if isinstance(found, list) else None
        except ValueError, IndexError:
            return ["?"]
        if not isinstance(first, dict):
            return ["?"]
        return list(_strs(_obj(first).get("RepoTags")))

    def remove_container(self, container_id: str) -> CommandResult:
        return self.run("container", "rm", container_id, check=False)

    def remove_volume(self, name: str) -> CommandResult:
        return self.run("volume", "rm", name, check=False)

    def remove_image(self, ref: str) -> CommandResult:
        return self.run("image", "rm", ref, check=False)

    def prune_build_cache(self, *, unused_hours: int) -> CommandResult:
        return self.run("builder", "prune", "-f", "--all", "--filter", f"until={unused_hours}h", check=False)

    def disk_usage(self) -> dict[str, str]:
        r = self.run("system", "df", "--format", "{{json .}}", check=False)
        out: dict[str, str] = {}
        for line in r.stdout.splitlines():
            try:
                d = _obj(json.loads(line))
            except ValueError:
                continue
            out[str(d.get("Type", "?"))] = str(d.get("Size", "?"))
        return out

    def follow_events(self, sink: EventSink, *, every: float) -> EventsEnd:
        """docker events until it ends or SIGTERM (systemctl stop) ends it; a thread ticks the sink."""
        lock = threading.Lock()

        def flusher() -> None:  # on a clock, not only when an event arrives
            while True:
                time.sleep(every)
                with lock:
                    sink.tick()

        threading.Thread(target=flusher, daemon=True).start()
        proc = subprocess.Popen(EVENTS_ARGV, stdout=subprocess.PIPE, text=True)
        stopping: list[bool] = []

        def stop(*_: object) -> None:  # systemctl stop: end the loop, flush below
            stopping.append(True)
            proc.terminate()

        signal.signal(signal.SIGTERM, stop)
        sink.started()
        assert proc.stdout is not None
        for line in proc.stdout:
            event = event_of(line)
            if event is None:
                continue
            image_id = sink.resolve(event)
            with lock:
                sink.use(image_id)
        with lock:
            sink.finish()
        rc = proc.wait()
        return EventsEnd(stopped=bool(stopping), returncode=rc)


class GcStateFiles:
    """GcStatePort over docker-gc's state folder and run lock."""

    def __init__(self, paths: GcPaths) -> None:
        self.paths = paths
        self._lock_fd: int | None = None

    def state_exists(self) -> bool:
        return os.path.exists(self.paths.last_used)

    def load_state(self) -> GcState:
        try:
            with open(self.paths.last_used) as f:
                st = json.load(f)
        except OSError, ValueError:
            st = None
        if isinstance(st, dict):
            images, since = st.get("images"), _num(st.get("since"))
            heartbeat = _num(st.get("heartbeat", 0))
            if isinstance(images, dict) and since is not None and heartbeat is not None:
                kept = {str(k): t for k, v in images.items() if (t := _num(v)) is not None}
                if len(kept) == len(images):  # a value that is not a time: not a state docker-gc wrote
                    return GcState(since=since, images=kept, heartbeat=heartbeat)
        return GcState(since=time.time(), images={}, heartbeat=0)

    def save_state(self, state: GcState) -> None:
        os.makedirs(self.paths.state_dir, mode=0o755, exist_ok=True)
        data = {"since": state.since, "images": dict(state.images), "heartbeat": state.heartbeat}
        write_atomic(self.paths.last_used, json.dumps(data), mode="umask", durable="file", relabel=False)

    def load_summary(self) -> RunSummary | None:
        try:
            with open(self.paths.last_run) as f:
                lr = json.load(f)
        except OSError, ValueError:
            return None
        lr = _obj(lr)
        nums = {k: _num(lr.get(k)) for k in ("at", "containers", "volumes", "images", "image_bytes", "failed")}
        at, containers, volumes, images = nums["at"], nums["containers"], nums["volumes"], nums["images"]
        image_bytes, failed = nums["image_bytes"], nums["failed"]
        if at is None or containers is None or volumes is None or images is None:
            return None
        if image_bytes is None or failed is None:
            return None
        return RunSummary(at, int(containers), int(volumes), int(images), int(image_bytes), int(failed))

    def save_summary(self, summary: RunSummary) -> None:
        """LAST_RUN, replaced atomically (it was rewritten in place): what status shows of the last run."""
        os.makedirs(self.paths.state_dir, mode=0o755, exist_ok=True)
        data = {
            "at": summary.at,
            "containers": summary.containers,
            "volumes": summary.volumes,
            "images": summary.images,
            "image_bytes": summary.image_bytes,
            "failed": summary.failed,
            "before": dict(summary.before),
            "after": dict(summary.after),
        }
        write_atomic(self.paths.last_run, json.dumps(data), mode="umask", durable=False, relabel=False)

    def take_run_lock(self) -> bool:
        """The run lock, held (its fd open) until the process ends."""
        os.makedirs(os.path.dirname(self.paths.lock), mode=0o755, exist_ok=True)
        fd = take_lock(self.paths.lock, wait=False, mode=0o644, nofollow=False)
        if fd is None:
            return False
        self._lock_fd = fd
        return True


def gc_log(prio: int, msg: str) -> None:
    """One journal line per message, "<prio>docker-gc: msg"."""
    journal_log(prio, msg, tag="docker-gc")


class GcHost:
    """GcHostPort: /proc/uptime and systemctl."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self._runner = runner

    def uptime(self) -> float:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])

    def recorder_active(self) -> bool:
        """A systemctl that does not answer counts as a recorder that is not running: images are held back."""
        done = self._runner.run(
            ["systemctl", "-q", "is-active", "docker-gc-recorder.service"], timeout=SYSTEMCTL_TIMEOUT, output="inherit"
        )
        if done.fault == "timeout":
            gc_log(
                4,
                f"systemctl is-active docker-gc-recorder.service: no answer in {SYSTEMCTL_TIMEOUT}s; images are held back",
            )
            return False
        if done.cause is not None:
            raise done.cause
        return done.returncode == 0


class TerminalConsole:
    """ConsolePort over this process's stdout, stderr and stdin."""

    def say(self, line: str) -> None:
        print(line)

    def error(self, line: str) -> None:
        print(line, file=sys.stderr)

    def log(self, prio: int, msg: str) -> None:
        gc_log(prio, msg)

    def interactive(self) -> bool:
        return sys.stdin.isatty()

    def ask(self, question: str) -> bool:
        try:
            return is_yes(input(question))
        except EOFError:
            return False
