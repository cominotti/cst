# SPDX-License-Identifier: MPL-2.0
"""Unit tests for btrfs/docker-gc (no root, no Docker engine).

The program is a thin entry over the btrfs.docker_gc package. The domain is
tested on values, the use cases (application) with fakes of their ports,
and the adapters on temporary folders, a patched subprocess.run and small
programs on PATH. btrfs/tests/test_docker_gc_program.py runs the whole
program as a process with a fake docker.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest btrfs.tests.test_docker_gc -v
"""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_docker_gc.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.docker_gc import adapters as gc_adapters, cli as gc_cli, domain as gc_domain, wiring as gc_wiring
from btrfs.docker_gc.application import DockerGc, Recording
from btrfs.docker_gc.domain import (
    DAY,
    Container,
    DockerEvent,
    EventsEnd,
    GcPaths,
    GcPlan,
    GcSettings,
    GcState,
    Image,
    ImageDrop,
    PlanInputs,
    RunSummary,
    Volume,
)
from btrfs.docker_gc.ports import EventSink
from cst_core.adapters import SubprocessCommandRunner
from cst_core.ports import CommandResult
from tests.probes import bytes_program

sys.dont_write_bytecode = True

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-gc")
NOW = 1_700_000_000.0
REPLACEMENT = chr(0xFFFD)  # what a byte that is not UTF-8 becomes


def ok(stderr: str = "") -> CommandResult:
    return CommandResult(0, "", stderr)


def failed(stderr: str) -> CommandResult:
    return CommandResult(1, "", stderr)


def container(
    name: str,
    *,
    image: str = "sha256:i",
    status: str = "exited",
    ended: float = 0,
    labels: frozenset[str] = frozenset(),
    restart: str = "no",
    volumes: frozenset[str] = frozenset(),
) -> Container:
    return Container(
        id="id-" + name,
        name=name,
        image_id=image,
        image_ref="ref-" + name,
        status=status,
        running=status == "running",
        started=0,
        finished=ended,
        created=0,
        label_keys=labels,
        restart=restart,
        volumes=volumes,
    )


class FakeDocker:
    """DockerPort over lists; records what it was asked."""

    def __init__(self) -> None:
        self.up = True
        self.listed_containers: list[Container] = []
        self.listed_volumes: list[Volume] = []
        self.listed_images: list[Image] = []
        self.ids: dict[str, str] = {}
        self.tags: dict[str, list[str] | None] = {}
        self.prune = ok()
        self.calls: list[tuple[str, ...]] = []
        self.error: RuntimeError | None = None
        self.end = EventsEnd(stopped=False, returncode=0)
        self.events: list[DockerEvent] = []

    def is_up(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.up

    def containers(self) -> list[Container]:
        return list(self.listed_containers)

    def volumes(self) -> list[Volume]:
        return list(self.listed_volumes)

    def images(self) -> list[Image]:
        return list(self.listed_images)

    def image_id(self, ref: str, *, timeout: int) -> str:
        self.calls.append(("image_id", ref, str(timeout)))
        return self.ids.get(ref, "")

    def image_tags(self, image_id: str) -> list[str] | None:
        return self.tags.get(image_id)

    def remove_container(self, container_id: str) -> CommandResult:
        self.calls.append(("rm container", container_id))
        return ok()

    def remove_volume(self, name: str) -> CommandResult:
        self.calls.append(("rm volume", name))
        return ok()

    def remove_image(self, ref: str) -> CommandResult:
        self.calls.append(("rm image", ref))
        return ok()

    def prune_build_cache(self, *, unused_hours: int) -> CommandResult:
        self.calls.append(("prune", str(unused_hours)))
        return self.prune

    def disk_usage(self) -> Mapping[str, str]:
        return {"Images": "1GB"}

    def follow_events(self, sink: EventSink, *, every: float) -> EventsEnd:
        self.calls.append(("events", str(every)))
        sink.started()
        for ev in self.events:
            sink.use(sink.resolve(ev))
        sink.finish()
        return self.end


class FakeState:
    def __init__(self) -> None:
        self.state: GcState | None = None
        self.saved: list[GcState] = []
        self.summary: RunSummary | None = None
        self.lock_free = True
        self.save_error: OSError | None = None

    def state_exists(self) -> bool:
        return self.state is not None

    def load_state(self) -> GcState:
        return self.state or GcState(since=NOW, images={}, heartbeat=0)

    def save_state(self, state: GcState) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(state)
        self.state = state

    def load_summary(self) -> RunSummary | None:
        return self.summary

    def save_summary(self, summary: RunSummary) -> None:
        self.summary = summary

    def take_run_lock(self) -> bool:
        return self.lock_free


class FakeHost:
    def __init__(self) -> None:
        self.up_for = 3600.0
        self.active = True

    def uptime(self) -> float:
        return self.up_for

    def recorder_active(self) -> bool:
        return self.active


class FakeConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.answers: list[bool] = []
        self.tty = True

    def say(self, line: str) -> None:
        self.lines.append(line)

    def error(self, line: str) -> None:
        self.lines.append("ERR " + line)

    def log(self, prio: int, msg: str) -> None:
        self.lines.append(f"<{prio}>{msg}")

    def interactive(self) -> bool:
        return self.tty

    def ask(self, question: str) -> bool:
        self.lines.append("ASK " + question.strip())
        return self.answers.pop(0) if self.answers else False


class FakeClock:
    def __init__(self, at: float = NOW) -> None:
        self.at = at

    def now(self) -> float:
        return self.at

    def monotonic(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


class Fixture:
    """A DockerGc over fakes, for one test."""

    def __init__(self, settings: GcSettings | None = None) -> None:
        self.docker = FakeDocker()
        self.state = FakeState()
        self.host = FakeHost()
        self.console = FakeConsole()
        self.clock = FakeClock()
        self.gc = DockerGc(
            settings or GcSettings(),
            docker=self.docker,
            state=self.state,
            host=self.host,
            console=self.console,
            clock=self.clock,
        )


class DomainTest(unittest.TestCase):
    def test_auto_named_only_for_docker_generated_names(self) -> None:
        self.assertTrue(gc_domain.auto_named("admiring_turing"))
        self.assertTrue(gc_domain.auto_named("admiring_turing2"))  # a collision adds one digit
        self.assertFalse(gc_domain.auto_named("admiring_turing12"))
        self.assertFalse(gc_domain.auto_named("admiring_turing" + chr(0x663)))  # an ASCII digit only, as Docker adds
        self.assertFalse(gc_domain.auto_named("redis_cache"))
        self.assertFalse(gc_domain.auto_named("boring_wozniak"))  # never generated

    def test_ts_reads_docker_time_stamps(self) -> None:
        self.assertEqual(gc_domain.ts("2020-01-02T00:00:00.5Z"), 1577923200.5)
        self.assertEqual(gc_domain.ts("2020-01-02T03:00:00.123456789+03:00"), 1577923200.123456)
        for unset in (None, "", "0001-01-01T00:00:00Z", "yesterday"):
            with self.subTest(value=unset):
                self.assertEqual(gc_domain.ts(unset), 0)

    def test_ephemeral_needs_a_generated_name_or_testcontainers_and_no_owner(self) -> None:
        self.assertTrue(gc_domain.ephemeral(container("admiring_turing")))
        self.assertTrue(gc_domain.ephemeral(container("db", labels=frozenset({"org.testcontainers"}))))
        self.assertFalse(gc_domain.ephemeral(container("db")))
        compose = frozenset({"com.docker.compose.project"})
        self.assertFalse(gc_domain.ephemeral(container("admiring_turing", labels=compose)))
        self.assertFalse(gc_domain.ephemeral(container("admiring_turing", restart="always")))

    def test_is_protected_reads_tags_and_digests(self) -> None:
        prot = gc_domain.protected_registries(" registry.example.com, ,other ")
        self.assertEqual(prot, ("registry.example.com/", "other/"))
        self.assertTrue(gc_domain.is_protected(Image("i", tags=("registry.example.com/org/app:1",)), prot))
        self.assertTrue(gc_domain.is_protected(Image("i", digests=("registry.example.com/o/a@sha256:1",)), prot))
        self.assertFalse(gc_domain.is_protected(Image("i", tags=("alpine:latest",)), prot))

    def test_event_image_names_only_use_events(self) -> None:
        start = DockerEvent("container", "start", image_attr="redis:7", actor_id="c1")
        self.assertEqual(gc_domain.event_image(start), "redis:7")
        self.assertEqual(gc_domain.event_image(DockerEvent("image", "pull", actor_id="sha256:x")), "sha256:x")
        self.assertEqual(gc_domain.event_image(DockerEvent("container", "exec_start", image_attr="redis:7")), "")
        self.assertEqual(gc_domain.event_image(DockerEvent("network", "connect", actor_id="n")), "")

    def plan(self, *, timed: bool, recorder_ok: bool = True) -> GcPlan:
        old = NOW - 30 * DAY
        inputs = PlanInputs(
            containers=[
                container("admiring_turing", image="sha256:a", ended=old, volumes=frozenset({"v1"})),
                container("redis_cache", image="sha256:b", ended=old, volumes=frozenset({"v3"})),
                container("eager_euler", image="sha256:c", ended=NOW - DAY),
            ],
            volumes=[
                Volume("v1", anonymous=True),
                Volume("v2", anonymous=True),
                Volume("v3", anonymous=True),
                Volume("named", anonymous=False),
            ],
            images=[
                Image("sha256:a", tags=("alpine:old",), size=7),
                Image("sha256:b", tags=("redis:7",)),
                Image("sha256:d", tags=("registry.example.com/x:1",), size=3),
                Image("sha256:e", size=1),
                Image("sha256:f", last_tag_time=NOW - DAY),
            ],
            state=GcState(since=NOW - 20 * DAY, images={"sha256:e": NOW - 2 * DAY}),
            recorder_ok=recorder_ok,
        )
        return gc_domain.plan_gc(inputs, GcSettings(), now=NOW, timed=timed)

    def test_the_weekly_plan(self) -> None:
        p = self.plan(timed=True)
        self.assertEqual([c.name for c in p.containers], ["admiring_turing"])
        self.assertEqual(p.volumes, ("v1", "v2"))  # v3: its container stays; named volumes never
        self.assertEqual([i.id for i in p.images], ["sha256:a"])  # e: used lately; f: tagged lately
        self.assertEqual(p.held, 1)  # the protected registry: cleanup-now only
        self.assertEqual(p.images[0].used, NOW - 20 * DAY)  # the recorder's start counts as a use
        self.assertEqual(self.plan(timed=True, recorder_ok=False).images, ())
        self.assertEqual(self.plan(timed=True, recorder_ok=False).held, 2)

    def test_cleanup_now_plans_protected_images_too(self) -> None:
        p = self.plan(timed=False)
        self.assertEqual([(i.id, i.protected) for i in p.images], [("sha256:a", False), ("sha256:d", True)])
        self.assertEqual(p.held, 0)

    def test_plan_lines_list_images_by_size_and_mark_the_private_ones(self) -> None:
        plan = GcPlan(
            containers=(),
            volumes=(),
            images=(
                ImageDrop("sha256:aaaaaaaaaaaaaaaaaa", (), 999, 0, protected=False),
                ImageDrop("sha256:b", ("registry/x:1",), 5_000_000, 0, protected=True),
            ),
            held=0,
            recorder_ok=True,
            at=10 * DAY,
        )
        lines = gc_domain.plan_lines(plan, GcSettings())
        self.assertIn("unused images: 2, up to 5.0 MB (shared layers counted per image)", lines)
        self.assertEqual(
            [line for line in lines if "last use never" in line],
            [
                "     5.0 MB  registry/x:1  (last use never)  [protected registry]",
                "      999 B  <untagged> aaaaaaaaaaaa  (last use never)",
            ],
        )

    def test_plan_lines_cut_long_lists_unless_full(self) -> None:
        plan = GcPlan((), tuple(f"v{i}" for i in range(5)), (), 0, True, NOW)
        lines = gc_domain.plan_lines(plan, GcSettings())
        self.assertEqual(lines[1:5], ["orphaned anonymous volumes: 5", "  v0", "  v1", "  v2"])
        self.assertIn("  (lists truncated; 'docker-gc cleanup-now' shows everything before asking)", lines)
        full = gc_domain.plan_lines(plan, GcSettings(), full=True)
        self.assertEqual(len([line for line in full if line.startswith("  v")]), 5)

    def test_the_last_run_in_decimal_units(self) -> None:
        line = gc_domain.last_run_line(RunSummary(0, 1, 2, 3, 1234567, 0))
        self.assertRegex(line, r"^last run [0-9-]+ [0-9:]+: 1 container\(s\), 2 volume\(s\), 3 image\(s\) \(1\.2 MB\)")
        self.assertEqual(gc_domain.last_run_line(None), "last run: never")


class ApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture()

    def test_a_docker_error_is_logged_and_exits_1(self) -> None:
        self.f.docker.error = RuntimeError("docker info: no answer in 60s")
        self.assertEqual(self.f.gc.execute("run", dry=True), 1)
        self.assertEqual(self.f.console.lines, ["<3>docker info: no answer in 60s"])

    def test_status_without_records_and_without_docker(self) -> None:
        self.f.docker.up = False
        self.assertEqual(self.f.gc.execute("status"), 0)
        self.assertEqual(
            self.f.console.lines,
            [
                "image use not recorded yet (recorder not started)",
                "last run: never",
                "docker is not running; nothing to do",
            ],
        )

    def test_a_failed_build_cache_prune_is_logged_and_counted(self) -> None:
        self.f.docker.prune = failed("Error response from daemon: busy\n")
        plan = GcPlan((), (), (), 0, True, 0)
        self.assertEqual(self.f.gc.apply(plan), 1)
        self.assertEqual(self.f.console.lines, ["<4>could not prune the build cache: Error response from daemon: busy"])
        self.assertEqual(self.f.docker.calls, [("prune", "336")])
        # A prune that works is no failure and no line.
        self.f.docker.prune = ok()
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.apply(plan), 0)
        self.assertEqual(self.f.console.lines, [])

    def test_a_second_run_while_the_lock_is_held_exits_0(self) -> None:
        self.f.state.lock_free = False
        for dry in (False, True):
            self.f.console.lines.clear()
            self.assertEqual(self.f.gc.execute("run", dry=dry), 0)
            self.assertEqual(self.f.console.lines, ["<5>another docker-gc run is active; skipped"])
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.execute("cleanup-now"), 0)
        self.assertEqual(self.f.console.lines, ["<5>another docker-gc run is active; skipped"])

    def test_a_run_just_after_boot_is_skipped_but_a_dry_run_is_not(self) -> None:
        self.f.host.up_for = 599
        self.assertEqual(self.f.gc.execute("run"), 0)
        self.assertEqual(self.f.console.lines, ["<5>booted less than 10 minutes ago; skipped this time"])
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.execute("run", dry=True), 0)
        self.assertEqual(self.f.console.lines[-1], "(dry run: nothing removed)")

    def test_a_run_saves_its_summary(self) -> None:
        self.f.docker.listed_images = [Image("sha256:a", tags=("alpine:old",), size=7)]
        self.f.state.state = GcState(since=NOW - 30 * DAY, images={}, heartbeat=NOW)
        self.f.docker.ids["alpine:old"] = "sha256:a"
        self.assertEqual(self.f.gc.execute("run"), 0)
        self.assertEqual(self.f.state.summary, RunSummary(NOW, 0, 0, 1, 7, 0, {"Images": "1GB"}, {"Images": "1GB"}))
        self.assertEqual(self.f.docker.calls[-2:], [("rm image", "alpine:old"), ("prune", "336")])

    def test_remove_image_rechecks_before_removing(self) -> None:
        img = ImageDrop("sha256:a", ("a:1", "a:2"), 1, 0, protected=False)
        self.f.docker.listed_containers = [container("x", image="sha256:a")]
        self.assertEqual(self.f.gc.remove_image(img, NOW), "now used by a container")
        self.f.docker.listed_containers = []
        self.f.state.state = GcState(since=0, images={"sha256:a": NOW - 30})
        self.assertEqual(self.f.gc.remove_image(img, NOW), "used meanwhile")
        self.f.state.state = None
        self.f.docker.ids = {"a:1": "sha256:a", "a:2": "sha256:other"}  # a:2 was re-pulled meanwhile
        self.f.docker.tags["sha256:a"] = ["a:2"]
        self.assertEqual(self.f.gc.remove_image(img, NOW), "got new tags meanwhile")
        self.assertIn(("rm image", "a:1"), self.f.docker.calls)
        self.assertNotIn(("rm image", "a:2"), self.f.docker.calls)
        self.f.docker.tags["sha256:a"] = []
        self.assertEqual(self.f.gc.remove_image(img, NOW), "")
        self.assertEqual(self.f.docker.calls[-1], ("rm image", "sha256:a"))

    def test_cleanup_now_asks_twice_for_protected_images(self) -> None:
        self.f.state.state = GcState(since=NOW - 30 * DAY)
        self.f.docker.listed_images = [
            Image("sha256:a", tags=("a:1",), size=1),
            Image("sha256:d", tags=("registry.example.com/x:1",), size=1),
        ]
        self.f.docker.ids = {"a:1": "sha256:a", "registry.example.com/x:1": "sha256:d"}
        self.f.console.answers = [True, False]
        self.assertEqual(self.f.gc.execute("cleanup-now"), 0)
        removed = [c[1] for c in self.f.docker.calls if c[0] == "rm image"]
        self.assertEqual(removed, ["a:1"])
        asks = [line for line in self.f.console.lines if line.startswith("ASK")]
        self.assertEqual(len(asks), 2)
        self.assertIn("1 image(s) above (the [protected registry] ones are asked next)?", asks[0])
        self.assertEqual(self.f.console.lines[-1], "Done; failures: 0. Now: {'Images': '1GB'}")

    def test_cleanup_now_needs_a_terminal_and_stops_at_no(self) -> None:
        self.f.console.tty = False
        self.assertEqual(self.f.gc.execute("cleanup-now"), 1)
        self.assertEqual(self.f.console.lines, ["ERR cleanup-now asks for confirmation: run it in a terminal."])
        self.f.console.tty = True
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.execute("cleanup-now"), 0)
        self.assertEqual(self.f.console.lines[-1], "Nothing to remove.")
        self.f.docker.listed_volumes = [Volume("v", anonymous=True)]
        self.assertEqual(self.f.gc.execute("cleanup-now"), 0)
        self.assertEqual(self.f.console.lines[-1], "Nothing removed.")
        self.assertNotIn(("rm volume", "v"), self.f.docker.calls)


class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture(GcSettings(flush_seconds=7))

    def test_exit_status_after_the_stream(self) -> None:
        for end, want in (
            (EventsEnd(stopped=True, returncode=-15), 0),
            (EventsEnd(stopped=False, returncode=-9), 0),
            (EventsEnd(stopped=False, returncode=0), 1),
            (EventsEnd(stopped=False, returncode=3), 3),
        ):
            with self.subTest(end=end):
                self.f.docker.end = end
                self.assertEqual(self.f.gc.execute("record"), want)
        self.assertIn(("events", "7"), self.f.docker.calls)

    def test_uses_are_recorded_and_saved(self) -> None:
        self.f.docker.listed_containers = [
            container("web", image="sha256:c", status="running"),
            container("old", image="sha256:o", ended=NOW - DAY),
        ]
        self.f.docker.ids = {"redis:7": "sha256:r"}
        full_id = "sha256:" + "9" * 64
        self.f.docker.events = [
            DockerEvent("container", "start", image_attr="redis:7"),
            DockerEvent("image", "pull", actor_id=full_id),
            DockerEvent("container", "create", image_attr="gone:1"),
        ]
        self.f.gc.execute("record")
        first, last = self.f.state.saved[0], self.f.state.saved[-1]
        self.assertEqual(dict(first.images), {"sha256:c": NOW, "sha256:o": NOW - DAY})
        self.assertEqual(first.heartbeat, NOW)
        self.assertEqual(sorted(last.images), sorted(["sha256:c", "sha256:o", "sha256:r", full_id]))
        self.assertEqual(self.f.console.lines, ["<6>recording image use from docker events"])
        # A full image ID is not looked up.
        self.assertEqual([c[1] for c in self.f.docker.calls if c[0] == "image_id"], ["redis:7", "gone:1"])

    def test_a_recorder_down_for_a_while_restarts_the_clock(self) -> None:
        self.f.state.state = GcState(since=0, images={"sha256:x": 5}, heartbeat=NOW - 1000)
        self.f.host.up_for = 3600  # Docker kept running: the heartbeat stopped after the boot
        self.f.gc.execute("record")
        self.assertEqual(self.f.state.saved[0].since, NOW)
        self.assertEqual(dict(self.f.state.saved[0].images), {"sha256:x": 5})
        self.assertEqual(self.f.console.lines[0], "<5>recorder was down for a while: image clock restarted")
        # A reboot, Docker down too: the clock goes on.
        self.f = Fixture()
        self.f.state.state = GcState(since=0, images={}, heartbeat=NOW - 1000)
        self.f.host.up_for = 100
        self.f.gc.execute("record")
        self.assertEqual(self.f.state.saved[0].since, 0)

    def test_tick_saves_what_changed_and_the_heartbeat(self) -> None:
        rec = Recording(self.f.gc, GcState(since=0))
        rec.last_beat = NOW
        rec.tick()
        self.assertEqual(self.f.state.saved, [])  # nothing new, heartbeat fresh
        rec.use("sha256:a")
        rec.tick()
        self.assertEqual(self.f.state.saved[-1].images, {"sha256:a": NOW})
        self.f.clock.sleep(300)
        rec.tick()
        self.assertEqual(self.f.state.saved[-1].heartbeat, NOW + 300)
        self.f.state.save_error = OSError(28, "No space left on device")
        rec.use("sha256:b")
        self.f.clock.sleep(1)
        rec.tick()
        self.assertEqual(self.f.console.lines, ["<3>cannot save state: [Errno 28] No space left on device"])
        self.assertTrue(rec.dirty, "saved at the next tick")

    def test_resolved_references_are_reused_for_a_minute(self) -> None:
        rec = Recording(self.f.gc, GcState(since=0))
        self.f.docker.ids = {"a:1": "sha256:a"}
        ev = DockerEvent("container", "start", image_attr="a:1")
        self.assertEqual([rec.resolve(ev), rec.resolve(ev)], ["sha256:a", "sha256:a"])
        self.f.clock.sleep(61)
        rec.resolve(ev)
        self.assertEqual([c[:2] for c in self.f.docker.calls], [("image_id", "a:1")] * 2)
        self.assertEqual(self.f.docker.calls[0][2], "30")


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture()
        self.f.docker.up = False
        self.restored: list[str] = []

    def run_cli(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gc_cli.run_cli(list(argv), service=self.f.gc, restore_signals=lambda: self.restored.append("x"))
        return rc, err.getvalue()

    def test_unknown_command_is_a_usage_error(self) -> None:
        self.assertEqual(self.run_cli("bogus"), (2, "usage: docker-gc record|run [--dry-run]|cleanup-now|status\n"))
        self.assertEqual(self.f.console.lines, [])

    def test_only_status_and_a_dry_run_get_sigpipe_back(self) -> None:
        # They only read: `docker-gc status | head` ends quietly. The weekly run,
        # cleanup-now and the recorder keep Python's handling (they remove things).
        self.assertEqual(self.run_cli("status")[0], 0)
        self.assertEqual(self.restored, ["x"])
        self.assertEqual(self.run_cli()[0], 0)  # no command: status
        self.assertEqual(self.run_cli("run", "--dry-run")[0], 0)
        self.assertEqual(self.restored, ["x", "x", "x"])
        for argv in (("run",), ("cleanup-now",), ("bogus",)):
            self.run_cli(*argv)
        self.assertEqual(self.restored, ["x", "x", "x"], "run and cleanup-now must keep Python's handling")


class WiringTest(unittest.TestCase):
    def test_settings_and_paths_from_the_environment(self) -> None:
        self.assertEqual(gc_wiring.settings_from({}), GcSettings())
        got = gc_wiring.settings_from(
            {"KEEP_IMAGE_DAYS": "3", "DOCKER_GC_PROTECT": "a.example,b.example", "DOCKER_GC_FLUSH": "5"}
        )
        self.assertEqual((got.keep_image_days, got.protected, got.flush_seconds), (3, ("a.example/", "b.example/"), 5))
        with self.assertRaises(ValueError):
            gc_wiring.settings_from({"KEEP_CONTAINER_DAYS": "x"})
        self.assertEqual(gc_wiring.paths_from({}), GcPaths("/var/lib/docker-gc", "/run/docker-gc"))
        self.assertEqual(gc_wiring.paths_from({"DOCKER_GC_STATE": "/s", "DOCKER_GC_RUNTIME": "/r"}).lock, "/r/run.lock")


class StateFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="docker-gc-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.files = gc_adapters.GcStateFiles(GcPaths(self.tmp, os.path.join(self.tmp, "run")))

    def test_log_lines_carry_the_priority_and_the_program_name(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gc_adapters.gc_log(5, "kept image x")
        self.assertEqual(out.getvalue(), "<5>docker-gc: kept image x\n")

    def test_a_second_lock_while_the_first_is_held_is_refused(self) -> None:
        self.assertTrue(self.files.take_run_lock())
        other = gc_adapters.GcStateFiles(self.files.paths)
        self.assertFalse(other.take_run_lock())

    def test_the_lock_is_in_the_runtime_folder_and_leaves_no_state(self) -> None:
        # The sandboxed docker-gc.service may write only in its StateDirectory and RuntimeDirectory;
        # the lock was /run/docker-gc.lock, read-only under ProtectSystem=strict. It is in the
        # RuntimeDirectory (/run/docker-gc, which the lock makes for a run outside the unit), so a
        # dry run or cleanup-now leaves nothing in the state folder that 'disable --purge' removes.
        # A fresh interpreter, so that the environment sets the paths as on the machine.
        state = os.path.join(self.tmp, "state", "docker-gc")
        runtime = os.path.join(self.tmp, "not-yet", "docker-gc")
        code = (
            "import os, sys; sys.path.insert(0, sys.argv[1]);"
            "from btrfs.docker_gc import adapters as a, wiring as w;"
            "p = w.paths_from(os.environ); f = a.GcStateFiles(p); assert f.take_run_lock();"
            "print(p.lock); print(oct(os.stat(os.path.dirname(p.lock)).st_mode & 0o777))"
        )
        r = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, _CHECKOUT],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "DOCKER_GC_STATE": state,
                "DOCKER_GC_RUNTIME": runtime,
            },
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(r.stdout.splitlines(), [os.path.join(runtime, "run.lock"), "0o755"])
        self.assertTrue(os.path.isfile(os.path.join(runtime, "run.lock")))
        self.assertFalse(os.path.exists(state))

    def test_the_run_summary_replaces_a_link_and_leaves_its_target(self) -> None:
        # It was rewritten in place: through a link, into the file it pointed to.
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write("keep")
        os.symlink(target, self.files.paths.last_run)
        self.files.save_summary(RunSummary(1, 0, 0, 0, 0, 0))
        self.assertFalse(os.path.islink(self.files.paths.last_run))
        with open(self.files.paths.last_run) as f:
            self.assertEqual(
                json.load(f),
                {"at": 1, "containers": 0, "volumes": 0, "images": 0, "image_bytes": 0, "failed": 0}
                | {"before": {}, "after": {}},
            )
        with open(target) as f:
            self.assertEqual(f.read(), "keep")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["elsewhere", "last-run.json"])
        self.assertEqual(self.files.load_summary(), RunSummary(1, 0, 0, 0, 0, 0))

    def write(self, name: str, text: str) -> None:
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(text)

    def test_the_recorded_state_and_what_is_not_one(self) -> None:
        self.assertFalse(self.files.state_exists())
        before = time.time()
        self.assertGreaterEqual(self.files.load_state().since, before)  # none: recorded from now
        self.files.save_state(GcState(since=5, images={"sha256:a": 7.5}, heartbeat=9))
        with open(self.files.paths.last_used) as f:
            self.assertEqual(f.read(), '{"since": 5, "images": {"sha256:a": 7.5}, "heartbeat": 9}')
        self.assertEqual(self.files.load_state(), GcState(since=5, images={"sha256:a": 7.5}, heartbeat=9))
        for text in ("not json", "[]", '{"since": "x", "images": {}}', '{"since": 1, "images": []}'):
            with self.subTest(text=text):
                self.write("last-used.json", text)
                self.assertEqual(self.files.load_state().images, {})
                self.assertGreaterEqual(self.files.load_state().since, before)

    def test_a_summary_that_is_not_one_reads_as_never(self) -> None:
        for text in ("not json", "[]", '{"at": 1}', '{"at": 1, "containers": 1, "volumes": 1, "images": 1}'):
            with self.subTest(text=text):
                self.write("last-run.json", text)
                self.assertIsNone(self.files.load_summary())


class RecorderActiveTest(unittest.TestCase):
    """recorder_active: systemctl is-active with no stdin and a time limit."""

    def setUp(self) -> None:
        self.host = gc_adapters.GcHost(SubprocessCommandRunner())

    def test_systemctl_gets_no_stdin_and_a_timeout(self) -> None:
        done = subprocess.CompletedProcess(["systemctl"], 0)
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            self.assertTrue(self.host.recorder_active())
        self.assertEqual(run.call_args.args[0], ["systemctl", "-q", "is-active", "docker-gc-recorder.service"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], gc_adapters.SYSTEMCTL_TIMEOUT)

    def test_a_systemctl_that_does_not_answer_holds_images_back(self) -> None:
        hang = subprocess.TimeoutExpired(["systemctl"], gc_adapters.SYSTEMCTL_TIMEOUT)
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", side_effect=hang), contextlib.redirect_stdout(out):
            self.assertFalse(self.host.recorder_active())
        self.assertEqual(
            out.getvalue(),
            "<4>docker-gc: systemctl is-active docker-gc-recorder.service: no answer in 10s; images are held back\n",
        )


class EntryTest(unittest.TestCase):
    def test_the_program_runs_from_the_checkout(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", "-B", TOOL, "bogus"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            cwd="/",
        )
        self.assertEqual((r.returncode, r.stderr), (2, "usage: docker-gc record|run [--dry-run]|cleanup-now|status\n"))

    def test_status_and_a_dry_run_into_a_closed_pipe_stop_quietly(self) -> None:
        # "cst btrfs docker-gc status | head -1" ended in "Exception ignored ...
        # BrokenPipeError" (exit 120). Docker is a stub that is not running.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "docker"), "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(os.path.join(tmp, "docker"), 0o755)
            env = {"PATH": f"{tmp}:/usr/bin:/bin", "HOME": tmp, "DOCKER_GC_STATE": tmp}
            for args in (("status",), ("run", "--dry-run")):
                r, w = os.pipe()
                os.close(r)
                try:
                    p = subprocess.run(
                        [sys.executable, "-I", "-B", TOOL, *args],
                        check=False,
                        stdout=w,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        env=env,
                        timeout=60,
                    )
                finally:
                    os.close(w)
                self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""), args)


class DockerCommandTest(unittest.TestCase):
    """DockerCli.run: a docker that does not answer stops the run with one line; one that is missing raises."""

    def setUp(self) -> None:
        self.docker = gc_adapters.DockerCli(SubprocessCommandRunner())

    def test_no_answer_and_missing(self) -> None:
        hang = subprocess.TimeoutExpired(["docker"], 30)
        with mock.patch.object(subprocess, "run", side_effect=hang):
            with self.assertRaises(RuntimeError) as stop:
                self.docker.run("image", "ls", "-q", timeout=30)
        self.assertEqual(str(stop.exception), "docker image ls: no answer in 30s")
        self.assertIsInstance(stop.exception.__cause__, subprocess.TimeoutExpired)
        missing = FileNotFoundError(2, "No such file or directory", "docker")
        with mock.patch.object(subprocess, "run", side_effect=missing), self.assertRaises(FileNotFoundError):
            self.docker.run("info")
        done = subprocess.CompletedProcess(["docker"], 1, "", "daemon down\n")
        with mock.patch.object(subprocess, "run", return_value=done), self.assertRaises(RuntimeError) as stop:
            self.docker.run("info")
        self.assertEqual(str(stop.exception), "docker info: daemon down")

    def test_not_utf8_output_is_replaced(self) -> None:
        # Bytes of docker's output that are not UTF-8 become U+FFFD (they raised UnicodeDecodeError).
        tmp = tempfile.mkdtemp(prefix="cst-gc-bytes-")
        self.addCleanup(shutil.rmtree, tmp)
        bytes_program(tmp, "docker", b"img\xff\n")
        with mock.patch.dict(os.environ, {"PATH": f"{tmp}:/usr/bin:/bin"}):
            self.assertEqual(self.docker.run("image", "ls", "-q").stdout, "img" + REPLACEMENT + "\n")

    def test_inspect_json_is_read_with_docker_s_defaults(self) -> None:
        c = gc_adapters.container_of(
            {
                "Id": "c1",
                "Name": "/admiring_turing",
                "Image": "sha256:a",
                "Config": {"Image": "alpine", "Labels": None},
                "HostConfig": {"RestartPolicy": {"Name": ""}},
                "State": {"Status": "exited", "Running": False, "FinishedAt": "2020-01-02T00:00:00Z"},
                "Mounts": [{"Type": "volume", "Name": "v"}, {"Type": "bind", "Source": "/x"}],
            }
        )
        self.assertEqual(
            (c.name, c.restart, c.label_keys, c.volumes, c.finished, c.created),
            ("admiring_turing", "no", frozenset(), frozenset({"v"}), 1577923200.0, 0),
        )
        self.assertEqual(gc_adapters.container_of({"Id": "c2"}).image_ref, "?")
        self.assertEqual(
            gc_adapters.image_of({"Id": "i", "RepoTags": None, "Size": None, "Metadata": None}), Image("i")
        )
        anonymous = gc_adapters.volume_of({"Name": "v", "Labels": {"com.docker.volume.anonymous": ""}})
        self.assertTrue(anonymous.anonymous)
        self.assertIsNone(gc_adapters.event_of("not json"))
        self.assertIsNone(gc_adapters.event_of("[1]"))
        self.assertEqual(
            gc_adapters.event_of('{"Type": "container", "Action": "start", "Actor": {"Attributes": {"image": "r"}}}'),
            DockerEvent("container", "start", image_attr="r"),
        )


if __name__ == "__main__":
    unittest.main()
