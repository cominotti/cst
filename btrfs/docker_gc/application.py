# SPDX-License-Identifier: MPL-2.0
"""docker-gc's use cases: record image use, plan a run, apply it, the weekly run, cleanup-now and status."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from btrfs.docker_gc.domain import (
    BAD_SETTINGS_EXIT,
    HEARTBEAT_SECONDS,
    MIN_UPTIME,
    NEWER_STATE_EXIT,
    NOT_RECORDED,
    UnknownProtection,
    DockerEvent,
    GcPaths,
    GcPlan,
    GcSettings,
    GcState,
    ImageDrop,
    NewerState,
    PlanInputs,
    Protection,
    RunSummary,
    container_refusal,
    event_image,
    heartbeat_fresh,
    last_run_line,
    plan_gc,
    plan_lines,
    protection_line,
    recorded_line,
    recorder_down_too_long,
    render_settings_file,
    resolve_protection,
    run_log_line,
)
from btrfs.docker_gc.ports import ConsolePort, DockerPort, GcHostPort, GcSettingsPort, GcStatePort
from cst_core.ports import ClockPort

type GcCommand = Literal["record", "run", "cleanup-now", "status", "settings"]
COMMANDS: tuple[GcCommand, ...] = ("record", "run", "cleanup-now", "status", "settings")

RESOLVE_TIMEOUT = 30  # seconds for the image inspect behind one event
RESOLVED_FOR = 60  # seconds a reference -> ID answer is reused
RESOLVED_MAX = 2000  # answers kept before the cache starts over


class Recording:
    """The recorder's state while it follows docker events (an EventSink).

    The stream calls use, tick and finish under one lock; resolve outside it.
    """

    def __init__(self, gc: DockerGc, state: GcState) -> None:
        self._gc = gc
        self.since = state.since
        self.images = dict(state.images)
        self.heartbeat = state.heartbeat
        self.dirty = False
        self.last_beat = 0.0
        self._resolved: dict[str, tuple[str, float]] = {}  # image reference -> (image id, resolved at)

    def snapshot(self) -> GcState:
        return GcState(since=self.since, images=dict(self.images), heartbeat=self.heartbeat)

    def note(self, image_id: str, at: float) -> None:
        if image_id and at > self.images.get(image_id, 0):
            self.images[image_id] = at
            self.dirty = True

    def save(self) -> None:
        self._gc.state.save_state(self.snapshot())

    def begin(self) -> None:
        """The first save, before the stream: the heartbeat now; the next one is due from now."""
        self.heartbeat = self._gc.clock.now()
        self.save()  # also fixes "since" on the very first start
        self.dirty = False
        self.last_beat = self._gc.clock.now()

    # ---- EventSink -------------------------------------------------------
    def started(self) -> None:
        self._gc.console.log(6, "recording image use from docker events")

    def resolve(self, event: DockerEvent) -> str:
        ref = event_image(event)
        if not ref:
            return ""
        if ref.startswith("sha256:") and len(ref) == 71:
            return ref
        clock = self._gc.clock
        hit = self._resolved.get(ref)
        if hit and clock.now() - hit[1] < RESOLVED_FOR:
            return hit[0]
        iid = self._gc.docker.image_id(ref, timeout=RESOLVE_TIMEOUT)
        if len(self._resolved) > RESOLVED_MAX:
            self._resolved.clear()
        self._resolved[ref] = (iid, clock.now())
        return iid

    def use(self, image_id: str) -> None:
        self.note(image_id, self._gc.clock.now())

    def tick(self) -> None:
        """On a clock, not only when an event arrives: what changed, and the heartbeat."""
        now = self._gc.clock.now()
        if self.dirty or now - self.last_beat >= HEARTBEAT_SECONDS:
            self.heartbeat = self.last_beat = now
            try:
                self.save()
                self.dirty = False
            except OSError as e:  # stale heartbeat = images held: safe
                self._gc.console.log(3, f"cannot save state: {e}")

    def finish(self) -> None:
        self.save()


class DockerGc:
    """docker-gc's commands over its ports."""

    def __init__(
        self,
        settings: GcSettings,
        *,
        docker: DockerPort,
        state: GcStatePort,
        host: GcHostPort,
        console: ConsolePort,
        clock: ClockPort,
        config: GcSettingsPort,
        paths: GcPaths,
        protect_env: str | None = None,
        in_container: bool = False,
        named_files: bool = False,
    ) -> None:
        self.settings = settings
        self.docker, self.state, self.host, self.console, self.clock = docker, state, host, console, clock
        self.config, self.paths, self.protect_env = config, paths, protect_env
        # Inside a toolbox or container (container_refusal), and whether the caller named the
        # settings file and the installed program (DOCKER_GC_CONF and DOCKER_GC_INSTALLED).
        self.in_container, self.named_files = in_container, named_files
        self.protection = Protection((), "not read yet")

    def read_protection(self) -> None:
        """The protected registries (resolve_protection) into the settings; raises UnknownProtection.
        The installed program is read only while the settings file is missing."""
        file_text = self.config.read_settings()
        self.protection = resolve_protection(
            self.paths,
            env=self.protect_env,
            file_text=file_text,
            installed=self.config.installed_sources() if file_text is None else None,
        )
        self.settings = replace(self.settings, protected=self.protection.prefixes)

    def execute(self, command: GcCommand, *, dry: bool = False, write: bool = False) -> int:
        """Run one command; its exit status. A docker error is one journal line and exit 1; a state file of
        a newer docker-gc one line and NEWER_STATE_EXIT; protected registries it cannot tell (UnknownProtection)
        one line and BAD_SETTINGS_EXIT, before anything else (the recorder needs no settings). Inside a
        toolbox or container, a command container_refusal names ends first: one line and exit 1."""
        if self.in_container:
            refused = container_refusal(
                command, dry=dry, write=write, settings_file=self.paths.settings_file, named_files=self.named_files
            )
            if refused is not None:
                self.console.error(refused)
                return 1
        try:
            if command == "record":
                return self.record()
            self.read_protection()
            if command == "settings":
                return self.settings_file(write=write)
            if command == "cleanup-now":
                return self.cleanup_now()
            if command == "run":
                self.run(dry=dry)
            else:
                self.status()
        except NewerState as e:
            self.console.log(3, str(e))
            return NEWER_STATE_EXIT
        except UnknownProtection as e:
            for line in str(e).splitlines():
                self.console.log(3, line)
            return BAD_SETTINGS_EXIT
        except RuntimeError as e:
            self.console.log(3, str(e))
            return 1
        return 0

    # ---- record ----------------------------------------------------------
    def record(self) -> int:
        """Follow docker events and record each image's last use; the exit status for systemd."""
        st = self.state.load_state()
        booted = self.clock.now() - self.host.uptime()
        # Down for a while with Docker running: `--rm` containers of that time
        # left no trace, so the image clock starts over.
        if recorder_down_too_long(st, self.clock.now(), booted):
            st = replace(st, since=self.clock.now())
            self.console.log(5, "recorder was down for a while: image clock restarted")
        rec = Recording(self, st)
        # Whatever happened while no recorder ran: every container counts as a use.
        for c in self.docker.containers():
            rec.note(c.image_id, c.last_use(self.clock.now()))
        rec.begin()
        end = self.docker.follow_events(rec, every=self.settings.flush_seconds)
        if end.stopped or end.returncode < 0:
            return 0
        # docker events ended by itself (daemon restart): systemd restarts us.
        return end.returncode or 1

    # ---- plan and apply --------------------------------------------------
    def recorder_alive(self, st: GcState) -> bool:
        """The recorder runs and wrote its heartbeat lately."""
        return self.host.recorder_active() and heartbeat_fresh(st, self.clock.now())

    def plan(self, now: float, *, timed: bool = True) -> GcPlan:
        containers = self.docker.containers()
        st = self.state.load_state()
        volumes = self.docker.volumes()
        images = self.docker.images()
        inputs = PlanInputs(containers, volumes, images, st, self.recorder_alive(st))
        return plan_gc(inputs, self.settings, now=now, timed=timed)

    def show(self, p: GcPlan, *, full: bool = False) -> None:
        for line in plan_lines(p, self.settings, full=full):
            self.console.say(line)

    def remove_image(self, img: ImageDrop, plan_at: float) -> str:
        """Re-checked right before removal: still unused (fresh container list and
        recorder state), and each tag still pointing at this very image. Why it was kept, or ""."""
        if img.id in {c.image_id for c in self.docker.containers()}:
            return "now used by a container"
        if self.state.load_state().images.get(img.id, 0) > plan_at - 60:
            return "used meanwhile"
        for t in img.tags:
            if self.docker.image_id(t, timeout=600) != img.id:
                continue  # re-pulled or retagged meanwhile: that tag is not ours
            r = self.docker.remove_image(t)
            if r.returncode != 0:
                return f"cannot remove {t}: {r.stderr.strip()[:100]}"
        left = self.docker.image_tags(img.id)
        if left is None:
            return ""
        if left:
            return "got new tags meanwhile"
        r = self.docker.remove_image(img.id)
        if r.returncode != 0 and "no such image" not in r.stderr.lower():
            return r.stderr.strip()[:120]
        return ""

    def apply(self, p: GcPlan) -> int:
        """Remove what the plan lists; the number of failures."""
        failed = 0
        for c in p.containers:
            if self.docker.remove_container(c.id).returncode != 0:
                failed += 1
                self.console.log(4, f"could not remove container {c.name}")
        # Exactly the planned volumes; `rm` refuses one that got used meanwhile.
        for v in p.volumes:
            r = self.docker.remove_volume(v)
            if r.returncode != 0 and "no such volume" not in r.stderr.lower():
                failed += 1
                self.console.log(4, f"could not remove volume {v[:16]}: {r.stderr.strip()[:100]}")
        for img in p.images:
            why = self.remove_image(img, p.at)
            if why:
                self.console.log(5, f"kept image {(img.tags or (img.id,))[0]}: {why}")
        # --all: unused cache of any kind; until= is measured from its last use.
        r = self.docker.prune_build_cache(unused_hours=self.settings.keep_build_cache_days * 24)
        if r.returncode != 0:
            failed += 1
            self.console.log(4, f"could not prune the build cache: {r.stderr.strip()[:100]}")
        return failed

    # ---- the commands ----------------------------------------------------
    def docker_up(self) -> bool:
        if not self.docker.is_up():
            self.console.say("docker is not running; nothing to do")
            return False
        return True

    def take_lock(self) -> bool:
        if not self.state.take_run_lock():
            self.console.log(5, "another docker-gc run is active; skipped")
            return False
        return True

    def run(self, *, dry: bool) -> None:
        """The weekly run (dry: only the list)."""
        if not self.docker_up():
            return
        # Both state files of a newer docker-gc stop the run here (NewerState), before anything is
        # removed and before the uptime skip, so they stop it on a fresh boot too: the image record
        # (read again by the plan) and the last run's summary (written at the end, never read).
        self.state.load_state()
        self.state.load_summary()
        if self.host.uptime() < MIN_UPTIME and not dry:
            self.console.log(5, "booted less than 10 minutes ago; skipped this time")
            return
        if not self.take_lock():
            return
        p = self.plan(self.clock.now())
        self.show(p)
        if dry:
            self.console.say("(dry run: nothing removed)")
            return
        before = self.docker.disk_usage()
        failed = self.apply(p)
        after = self.docker.disk_usage()
        summary = RunSummary(
            at=p.at,
            containers=len(p.containers),
            volumes=len(p.volumes),
            images=len(p.images),
            image_bytes=sum(i.size for i in p.images),
            failed=failed,
            before=before,
            after=after,
        )
        self.state.save_summary(summary)
        self.console.log(5, run_log_line(summary))

    def cleanup_now(self) -> int:
        """One-off and interactive: everything unused not seen in use in the window, after asking."""
        if not self.docker_up():
            return 0
        if not self.console.interactive():
            self.console.error("cleanup-now asks for confirmation: run it in a terminal.")
            return 1
        if not self.take_lock():
            return 0
        p = self.plan(self.clock.now(), timed=False)
        self.show(p, full=True)
        if p.empty():
            self.console.say("Nothing to remove.")
            return 0
        prot = tuple(i for i in p.images if i.protected)
        p = replace(p, images=tuple(i for i in p.images if not i.protected))
        if not self.console.ask(
            f"\nRemove the containers, volumes and {len(p.images)} image(s) above"
            f"{' (the [protected registry] ones are asked next)' if prot else ''}? [y/N] "
        ):
            self.console.say("Nothing removed.")
            return 0
        if prot and self.console.ask(
            f"Also remove the {len(prot)} image(s) marked [protected registry]? "
            f"Pulling them again needs that registry. [y/N] "
        ):
            p = replace(p, images=p.images + prot)
        failed = self.apply(p)
        self.console.say(f"Done; failures: {failed}. Now: {dict(self.docker.disk_usage())}")
        return 0

    def settings_file(self, *, write: bool) -> int:
        """`docker-gc settings [--write]`: the protected registries and where they come from. While the
        settings file is missing: the dry run says what --write would put there (exit 10), --write
        writes it (what is protected now, so nothing changes; `cst btrfs docker-gc enable` runs it
        before it installs a new docker-gc). With the file there, --write changes nothing."""
        p, path = self.protection, self.paths.settings_file
        self.console.say(protection_line(p))
        if p.to_write is None:
            if write:
                self.console.say(f"unchanged: {path}")
            return 0
        value = f"DOCKER_GC_PROTECT={','.join(p.to_write)}"
        if not write:
            self.console.say(f"would write: {path} ({value})")
            return 10
        try:
            self.config.write_settings(render_settings_file(p.to_write))
        except OSError as e:
            self.console.error(f"cannot write {path}: {e.strerror}")
            return 1
        self.console.say(f"written:   {path} ({value})")
        return 0

    def status(self) -> None:
        """The protected registries, the tracking state, the last run, and what a weekly run would remove now."""
        self.console.say(protection_line(self.protection))
        if self.state.state_exists():
            st = self.state.load_state()
            self.console.say(recorded_line(st, self.settings, recorder_ok=self.recorder_alive(st)))
        else:
            self.console.say(NOT_RECORDED)
        self.console.say(last_run_line(self.state.load_summary()))
        if not self.docker_up():
            return
        self.console.say("a weekly run now would remove:")
        self.show(self.plan(self.clock.now()))
