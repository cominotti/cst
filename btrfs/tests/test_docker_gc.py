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

import contextlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from collections.abc import Mapping
from typing import override
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_docker_gc.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.docker_gc import adapters as gc_adapters
from btrfs.docker_gc import cli as gc_cli
from btrfs.docker_gc import domain as gc_domain
from btrfs.docker_gc import wiring as gc_wiring
from btrfs.docker_gc.application import DockerGc, Recording
from btrfs.docker_gc.domain import (
    DAY,
    NEWER_STATE_EXIT,
    Container,
    DockerEvent,
    EventsEnd,
    GcCommand,
    GcPaths,
    GcPlan,
    GcSettings,
    GcState,
    Image,
    ImageDrop,
    NewerState,
    PlanInputs,
    RunSummary,
    Volume,
)
from btrfs.docker_gc.ports import EventSink
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.domain.install import manifest_by_src
from cst_core.ports import CommandResult
from tests.probes import bytes_program

sys.dont_write_bytecode = True

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-gc")
NOW = 1_700_000_000.0
# The exact bytes of state files the writers produce (tests/fixtures/state-formats).
STATE_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(TOOL)), "tests", "fixtures", "state-formats")
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
        self.container_errors: list[RuntimeError] = []  # raised by the next containers() calls, in order
        self.end = EventsEnd(stopped=False, returncode=0)
        self.events: list[DockerEvent] = []

    def is_up(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.up

    def containers(self) -> list[Container]:
        if self.container_errors:
            raise self.container_errors.pop(0)
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
        self.summary_error: NewerState | None = None
        self.state_error: NewerState | None = None
        self.lock_free = True
        self.save_error: OSError | None = None

    def state_exists(self) -> bool:
        return self.state is not None

    def load_state(self) -> GcState:
        if self.state_error is not None:
            raise self.state_error
        return self.state or GcState(since=NOW, images={}, heartbeat=0)

    def save_state(self, state: GcState) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(state)
        self.state = state

    def load_summary(self) -> RunSummary | None:
        if self.summary_error is not None:
            raise self.summary_error
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


class FakeConfig:
    """GcSettingsPort: the settings file's text (None: missing), the installed program's sources
    (None: none installed), what was written."""

    def __init__(self, text: str | None = "DOCKER_GC_PROTECT=\n", installed: tuple[str, ...] | None = None) -> None:
        self.text = text
        self.installed = installed
        self.written: list[str] = []
        self.reads = 0
        self.installed_reads = 0
        self.fail_write: OSError | None = None

    def read_settings(self) -> str | None:
        self.reads += 1
        return self.text

    def installed_sources(self) -> tuple[str, ...] | None:
        self.installed_reads += 1
        return self.installed

    def write_settings(self, text: str) -> None:
        if self.fail_write is not None:
            raise self.fail_write
        self.written.append(text)
        self.text = text


PATHS = GcPaths("/state", "/run/gc", "/etc/cst/docker-gc.conf", "/usr/local/sbin/docker-gc")
# An invented registry, protected by the settings file of the tests that need one.
REGISTRY = "registry.example.com"


class Fixture:
    """A DockerGc over fakes, for one test."""

    def __init__(self, settings: GcSettings | None = None, config: FakeConfig | None = None) -> None:
        self.docker = FakeDocker()
        self.state = FakeState()
        self.host = FakeHost()
        self.console = FakeConsole()
        self.clock = FakeClock()
        self.config = config or FakeConfig()
        self.gc = DockerGc(
            settings or GcSettings(),
            docker=self.docker,
            state=self.state,
            host=self.host,
            console=self.console,
            clock=self.clock,
            config=self.config,
            paths=PATHS,
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
        return gc_domain.plan_gc(inputs, GcSettings(protected=("registry.example.com/",)), now=NOW, timed=timed)

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

    def test_plan_lines_list_images_by_size_and_mark_the_protected_ones(self) -> None:
        plan = GcPlan(
            containers=(),
            volumes=(),
            images=(
                ImageDrop("sha256:aaaaaaaaaaaaaaaaaa", protected=False, size=999),
                ImageDrop("sha256:b", protected=True, tags=("registry/x:1",), size=5_000_000),
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
        plan = GcPlan((), tuple(f"v{i}" for i in range(5)), (), recorder_ok=True, at=NOW)
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
    @override
    def setUp(self) -> None:
        self.f = Fixture()

    def test_a_docker_error_is_logged_and_exits_1(self) -> None:
        self.f.docker.error = RuntimeError("docker info: no answer in 60s")
        self.assertEqual(self.f.gc.execute(GcCommand.RUN, dry=True), 1)
        self.assertEqual(self.f.console.lines, ["<3>docker info: no answer in 60s"])

    def test_status_without_records_and_without_docker(self) -> None:
        self.f.docker.up = False
        self.assertEqual(self.f.gc.execute(GcCommand.STATUS), 0)
        self.assertEqual(
            self.f.console.lines,
            [
                "protected registries: none (DOCKER_GC_PROTECT in /etc/cst/docker-gc.conf)",
                "image use not recorded yet (recorder not started)",
                "last run: never",
                "docker is not running; nothing to do",
            ],
        )

    def test_a_failed_build_cache_prune_is_logged_and_counted(self) -> None:
        self.f.docker.prune = failed("Error response from daemon: busy\n")
        plan = GcPlan((), (), (), recorder_ok=True, at=0)
        self.assertEqual(self.f.gc.apply(plan), 1)
        self.assertEqual(self.f.console.lines, ["<4>could not prune the build cache: Error response from daemon: busy"])
        self.assertEqual(self.f.docker.calls, [("prune", "336")])
        # A prune that works is no failure and no line.
        self.f.docker.prune = ok()
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.apply(plan), 0)
        self.assertEqual(self.f.console.lines, [])

    def test_a_newer_state_stops_the_run_even_on_a_fresh_boot(self) -> None:
        # The uptime skip used to come first: on a machine up less than 10 minutes the run exited 0
        # without looking, and the next run wrote over the newer summary.
        self.f.host.up_for = 60.0
        self.f.state.summary_error = NewerState("last-run.json is format 2")
        self.assertEqual(self.f.gc.execute(GcCommand.RUN, dry=False), NEWER_STATE_EXIT)
        self.f.state.summary_error = None
        self.f.state.state_error = NewerState("last-used.json is format 2")
        self.assertEqual(self.f.gc.execute(GcCommand.RUN, dry=False), NEWER_STATE_EXIT)

    def test_a_second_run_while_the_lock_is_held_exits_0(self) -> None:
        self.f.state.lock_free = False
        for dry in (False, True):
            self.f.console.lines.clear()
            self.assertEqual(self.f.gc.execute(GcCommand.RUN, dry=dry), 0)
            self.assertEqual(self.f.console.lines, ["<5>another docker-gc run is active; skipped"])
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.execute(GcCommand.CLEANUP_NOW), 0)
        self.assertEqual(self.f.console.lines, ["<5>another docker-gc run is active; skipped"])

    def test_a_run_just_after_boot_is_skipped_but_a_dry_run_is_not(self) -> None:
        self.f.host.up_for = 599
        self.assertEqual(self.f.gc.execute(GcCommand.RUN), 0)
        self.assertEqual(self.f.console.lines, ["<5>booted less than 10 minutes ago; skipped this time"])
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.execute(GcCommand.RUN, dry=True), 0)
        self.assertEqual(self.f.console.lines[-1], "(dry run: nothing removed)")

    def test_a_run_saves_its_summary(self) -> None:
        self.f.docker.listed_images = [Image("sha256:a", tags=("alpine:old",), size=7)]
        self.f.state.state = GcState(since=NOW - 30 * DAY, images={}, heartbeat=NOW)
        self.f.docker.ids["alpine:old"] = "sha256:a"
        self.assertEqual(self.f.gc.execute(GcCommand.RUN), 0)
        self.assertEqual(self.f.state.summary, RunSummary(NOW, 0, 0, 1, 7, 0, {"Images": "1GB"}, {"Images": "1GB"}))
        self.assertEqual(self.f.docker.calls[-2:], [("rm image", "alpine:old"), ("prune", "336")])

    def test_an_unreadable_inspect_stops_the_run_before_anything_is_removed(self) -> None:
        self.f.docker.container_errors = [gc_domain.InspectError("container inspect failed for 1 object(s)")]
        self.assertEqual(self.f.gc.execute(GcCommand.RUN), 1)
        self.assertEqual(self.f.console.lines, ["<3>container inspect failed for 1 object(s); nothing done"])
        self.assertEqual(self.f.docker.calls, [])

    def test_an_incomplete_record_stops_every_command_with_its_own_status(self) -> None:
        # Docker answers the same at every try: the recorder unit is not restarted for this status.
        for command in (GcCommand.RUN, GcCommand.STATUS, GcCommand.RECORD):
            with self.subTest(command=command):
                self.f.console.lines.clear()
                self.f.docker.container_errors = [gc_domain.IncompleteRecordError("container inspect: c1: no Image")]
                self.assertEqual(self.f.gc.execute(command), gc_domain.INCOMPLETE_RECORD_EXIT)
                self.assertEqual(self.f.console.lines[-1], "<3>container inspect: c1: no Image; nothing done")
                self.assertEqual(self.f.docker.calls, [])

    def test_an_unreadable_container_list_during_the_removals_keeps_that_image_and_goes_on(self) -> None:
        # The re-check of the first image cannot read the containers: that image stays (counted as a
        # failure), the run goes on with the next one; nothing claims "nothing done" after a removal.
        a = ImageDrop("sha256:a", protected=False, tags=("a:1",), size=1)
        b = ImageDrop("sha256:b", protected=False, tags=("b:1",), size=1)
        self.f.docker.ids = {"a:1": "sha256:a", "b:1": "sha256:b"}
        self.f.docker.tags = {"sha256:a": [], "sha256:b": []}
        self.f.docker.container_errors = [gc_domain.InspectError("container inspect failed for 1 object(s)")]
        self.assertEqual(self.f.gc.apply(GcPlan((), (), (a, b), recorder_ok=True, at=NOW)), 1)
        self.assertEqual(
            self.f.console.lines,
            ["<4>kept image a:1: container list unreadable: container inspect failed for 1 object(s)"],
        )
        self.assertNotIn(("rm image", "a:1"), self.f.docker.calls)
        self.assertIn(("rm image", "b:1"), self.f.docker.calls)

    def test_remove_image_rechecks_before_removing(self) -> None:
        img = ImageDrop("sha256:a", protected=False, tags=("a:1", "a:2"), size=1)
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
        self.f = Fixture(config=FakeConfig(f"DOCKER_GC_PROTECT={REGISTRY}\n"))
        self.f.state.state = GcState(since=NOW - 30 * DAY)
        self.f.docker.listed_images = [
            Image("sha256:a", tags=("a:1",), size=1),
            Image("sha256:d", tags=("registry.example.com/x:1",), size=1),
        ]
        self.f.docker.ids = {"a:1": "sha256:a", "registry.example.com/x:1": "sha256:d"}
        self.f.console.answers = [True, False]
        self.assertEqual(self.f.gc.execute(GcCommand.CLEANUP_NOW), 0)
        removed = [c[1] for c in self.f.docker.calls if c[0] == "rm image"]
        self.assertEqual(removed, ["a:1"])
        asks = [line for line in self.f.console.lines if line.startswith("ASK")]
        self.assertEqual(len(asks), 2)
        self.assertIn("1 image(s) above (the [protected registry] ones are asked next)?", asks[0])
        self.assertIn("Also remove the 1 image(s) marked [protected registry]? Pulling them again needs", asks[1])
        self.assertEqual(self.f.console.lines[-1], "Done; failures: 0. Now: {'Images': '1GB'}")

    def test_cleanup_now_needs_a_terminal_and_stops_at_no(self) -> None:
        self.f.console.tty = False
        self.assertEqual(self.f.gc.execute(GcCommand.CLEANUP_NOW), 1)
        self.assertEqual(self.f.console.lines, ["ERR cleanup-now asks for confirmation: run it in a terminal."])
        self.f.console.tty = True
        self.f.console.lines.clear()
        self.assertEqual(self.f.gc.execute(GcCommand.CLEANUP_NOW), 0)
        self.assertEqual(self.f.console.lines[-1], "Nothing to remove.")
        self.f.docker.listed_volumes = [Volume("v", anonymous=True)]
        self.assertEqual(self.f.gc.execute(GcCommand.CLEANUP_NOW), 0)
        self.assertEqual(self.f.console.lines[-1], "Nothing removed.")
        self.assertNotIn(("rm volume", "v"), self.f.docker.calls)


class RecordTest(unittest.TestCase):
    @override
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
                self.assertEqual(self.f.gc.execute(GcCommand.RECORD), want)
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
        self.f.gc.execute(GcCommand.RECORD)
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
        self.f.gc.execute(GcCommand.RECORD)
        self.assertEqual(self.f.state.saved[0].since, NOW)
        self.assertEqual(dict(self.f.state.saved[0].images), {"sha256:x": 5})
        self.assertEqual(self.f.console.lines[0], "<5>recorder was down for a while: image clock restarted")
        # A reboot, Docker down too: the clock goes on.
        self.f = Fixture()
        self.f.state.state = GcState(since=0, images={}, heartbeat=NOW - 1000)
        self.f.host.up_for = 100
        self.f.gc.execute(GcCommand.RECORD)
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
    @override
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
        self.assertEqual(
            self.run_cli("bogus"),
            (2, "usage: docker-gc record|run [--dry-run]|cleanup-now|status|settings [--write]\n"),
        )
        self.assertEqual(self.f.console.lines, [])

    def test_only_status_and_a_dry_run_get_sigpipe_back(self) -> None:
        # They only read: `docker-gc status | head` ends quietly. The weekly run,
        # cleanup-now and the recorder keep Python's handling (they remove things).
        self.assertEqual(self.run_cli("status")[0], 0)
        self.assertEqual(self.restored, ["x"])
        self.assertEqual(self.run_cli()[0], 0)  # no command: status
        self.assertEqual(self.run_cli("run", "--dry-run")[0], 0)
        self.assertEqual(self.run_cli("settings")[0], 0)
        self.assertEqual(self.restored, ["x", "x", "x", "x"])
        for argv in (("run",), ("cleanup-now",), ("bogus",), ("settings", "--write")):
            self.run_cli(*argv)
        self.assertEqual(self.restored, ["x"] * 4, "run, cleanup-now and settings --write must keep Python's handling")


# main's (654bbda) btrfs/docker_gc/domain.py, its built-in registry replaced by REGISTRY (tests/fixtures/legacy).
LEGACY_DOMAIN = os.path.join(_CHECKOUT, "tests", "fixtures", "legacy", "btrfs-docker_gc-domain.py@654bbda")
# How the docker-gc of CST up to 2026-11-15 (the plain script; then btrfs/docker_gc/adapters.py) wrote its
# built-in registry, an invented one here.
OLD_PROTECTED_LINE = (
    'PROTECTED = tuple(r.strip() + "/" for r in\n'
    '                  os.environ.get("DOCKER_GC_PROTECT", "old.example:5000").split(",") if r.strip())\n'
)


class SettingsDomainTest(unittest.TestCase):
    def test_the_settings_file_is_read_strictly(self) -> None:
        read = gc_domain.parse_settings_file
        self.assertEqual(
            read("/f", "# mine\nDOCKER_GC_PROTECT=a.example, ghcr.io/team/ ,h:5000\n"),
            ("a.example", "ghcr.io/team", "h:5000"),
        )
        self.assertEqual(read("/f", "DOCKER_GC_PROTECT=\n"), ())
        # The path separators of the distribution spec's reference grammar: ".", "_", "__", a run of "-".
        self.assertEqual(
            read("/f", "DOCKER_GC_PROTECT=ghcr.io/my__team,ghcr.io/my--org,h/a.b_c-d\n"),
            ("ghcr.io/my__team", "ghcr.io/my--org", "h/a.b_c-d"),
        )
        self.assertEqual(read("/f", "# nothing set\n"), ())
        for text, why in (
            ("DOCKER_GC_PROTCT=a.example\n", "/f line 1: DOCKER_GC_PROTCT: unknown setting"),
            ("DOCKER_GC_PROTECT=a.example\nnot a line\n", "/f line 2: not a line:"),
            ("DOCKER_GC_PROTECT=a.example,bad host\n", "/f line 1: DOCKER_GC_PROTECT: not a registry: bad host"),
            ("DOCKER_GC_PROTECT=ghcr.io/a___b\n", "/f line 1: DOCKER_GC_PROTECT: not a registry: ghcr.io/a___b"),
            ("DOCKER_GC_PROTECT=ghcr.io/team-\n", "/f line 1: DOCKER_GC_PROTECT: not a registry: ghcr.io/team-"),
            ("DOCKER_GC_PROTECT=$REG\n", "/f line 1:"),
        ):
            with self.assertRaises(gc_domain.UnknownProtection, msg=text) as cm:
                read("/f", text)
            self.assertIn(why, str(cm.exception))
            self.assertTrue(str(cm.exception).endswith("nothing is removed until /f is fixed"))

    def test_the_file_it_writes_reads_back(self) -> None:
        for regs in ((), (REGISTRY,), ("a.example", "ghcr.io/team")):
            self.assertEqual(gc_domain.parse_settings_file("/f", gc_domain.render_settings_file(regs)), regs)

    def test_the_built_in_registry_of_an_installed_docker_gc(self) -> None:
        with open(LEGACY_DOMAIN) as f:
            legacy = f.read()
        self.assertEqual(gc_domain.builtin_protected([legacy]), (REGISTRY,))  # main's GcSettings
        self.assertEqual(gc_domain.builtin_protected(["import os\n" + OLD_PROTECTED_LINE]), ("old.example:5000",))
        with open(gc_domain.__file__) as f:
            self.assertEqual(gc_domain.builtin_protected([f.read()]), ())  # this CST: none built in
        self.assertIsNone(gc_domain.builtin_protected(["print('not docker-gc')\n"]))
        self.assertIsNone(gc_domain.builtin_protected(["def broken(:\n", "x = 1\n"]))
        self.assertIsNone(gc_domain.builtin_protected([]))

    def test_a_source_it_cannot_evaluate_shows_no_list(self) -> None:
        # literal_eval raises TypeError on an unhashable set item, the parser MemoryError on a nesting
        # too deep: no list (so a refusal, exit 78), never a traceback; the other sources still count.
        unhashable = 'x.get({[]}, "a")\n'
        self.assertIsNone(gc_domain.builtin_protected([unhashable]))
        self.assertIsNone(gc_domain.builtin_protected(["-" * 200_000 + "1\n"]))
        self.assertEqual(
            gc_domain.builtin_protected([unhashable, "import os\n" + OLD_PROTECTED_LINE]), ("old.example:5000",)
        )
        with self.assertRaises(gc_domain.UnknownProtection):
            gc_domain.resolve_protection(PATHS, env=None, file_text=None, installed=(unhashable,))

    def test_where_the_protected_registries_come_from(self) -> None:
        resolve = gc_domain.resolve_protection
        p = resolve(PATHS, env=None, file_text=f"DOCKER_GC_PROTECT={REGISTRY}\n", installed=("x",))
        self.assertEqual((p.registries, p.to_write, p.prefixes), ((REGISTRY,), None, (REGISTRY + "/",)))
        self.assertEqual(p.source, "DOCKER_GC_PROTECT in /etc/cst/docker-gc.conf")
        p = resolve(PATHS, env=None, file_text=None, installed=None)
        self.assertEqual((p.registries, p.to_write), ((), ()))
        p = resolve(PATHS, env=None, file_text=None, installed=("import os\n" + OLD_PROTECTED_LINE,))
        self.assertEqual((p.registries, p.to_write), (("old.example:5000",), ("old.example:5000",)))
        self.assertIn("built into the installed /usr/local/sbin/docker-gc", p.source)
        for unknown in ((), ("print(1)\n",)):
            with self.assertRaises(gc_domain.UnknownProtection) as cm:
                resolve(PATHS, env=None, file_text=None, installed=unknown)
            self.assertIn("/etc/cst/docker-gc.conf is missing, and the installed", str(cm.exception))
        # This docker-gc has no registry of its own (it reads the file): with the file gone that is no
        # "none" (the next run would remove what was protected), but a refusal.
        with open(gc_domain.__file__) as f:
            this_one = f.read()
        with self.assertRaises(gc_domain.UnknownProtection) as cm:
            resolve(PATHS, env=None, file_text=None, installed=(this_one,))
        self.assertIn("/usr/local/sbin/docker-gc reads them from that file: write", str(cm.exception))
        self.assertIn("(DOCKER_GC_PROTECT= for none)", str(cm.exception))
        p = resolve(PATHS, env="e.example", file_text=None, installed=None)
        self.assertEqual(
            (p.registries, p.to_write, p.source), (("e.example",), (), "DOCKER_GC_PROTECT in the environment")
        )


class SettingsUseCaseTest(unittest.TestCase):
    def test_settings_writes_the_file_only_when_missing(self) -> None:
        f = Fixture(config=FakeConfig(None, installed=("import os\n" + OLD_PROTECTED_LINE,)))
        self.assertEqual(f.gc.execute(GcCommand.SETTINGS), 10)
        self.assertEqual(f.config.written, [])
        self.assertEqual(
            f.console.lines[-1], "would write: /etc/cst/docker-gc.conf (DOCKER_GC_PROTECT=old.example:5000)"
        )
        self.assertEqual(f.gc.execute(GcCommand.SETTINGS, write=True), 0)
        self.assertEqual(f.config.written, [gc_domain.render_settings_file(("old.example:5000",))])
        self.assertEqual(f.console.lines[-1], "written:   /etc/cst/docker-gc.conf (DOCKER_GC_PROTECT=old.example:5000)")
        self.assertEqual(f.gc.execute(GcCommand.SETTINGS, write=True), 0)
        self.assertEqual(len(f.config.written), 1, "a second --write wrote again")
        self.assertEqual(
            f.console.lines[-2:],
            [
                "protected registries: old.example:5000 (DOCKER_GC_PROTECT in /etc/cst/docker-gc.conf)",
                "unchanged: /etc/cst/docker-gc.conf",
            ],
        )
        self.assertEqual(f.gc.execute(GcCommand.SETTINGS), 0)

    def test_the_installed_program_is_read_only_while_the_file_is_missing(self) -> None:
        # With the file there, the installed program does not count: a damaged one cannot stop a run.
        f = Fixture(config=FakeConfig("DOCKER_GC_PROTECT=\n", installed=("print(1)\n",)))
        self.assertEqual(f.gc.execute(GcCommand.STATUS), 0)
        self.assertEqual(f.config.installed_reads, 0)
        f = Fixture(config=FakeConfig(None, installed=("import os\n" + OLD_PROTECTED_LINE,)))
        self.assertEqual(f.gc.execute(GcCommand.STATUS), 0)
        self.assertEqual(f.config.installed_reads, 1)

    def test_a_failed_write_is_one_line_and_exit_1(self) -> None:
        f = Fixture(config=FakeConfig(None))
        f.config.fail_write = PermissionError(13, "Permission denied")
        self.assertEqual(f.gc.execute(GcCommand.SETTINGS, write=True), 1)
        self.assertEqual(f.console.lines[-1], "ERR cannot write /etc/cst/docker-gc.conf: Permission denied")

    def test_without_the_file_the_installed_programs_registry_stays_protected(self) -> None:
        # Between pulling this CST and the next `enable`: this checkout's cleanup-now still asks apart
        # for the images of the registry the installed docker-gc protects.
        f = Fixture(config=FakeConfig(None, installed=("import os\n" + OLD_PROTECTED_LINE,)))
        f.state.state = GcState(since=NOW - 30 * DAY)
        f.docker.listed_images = [Image("sha256:d", tags=("old.example:5000/x:1",), size=1)]
        f.docker.ids = {"old.example:5000/x:1": "sha256:d"}
        f.console.answers = [True, False]
        self.assertEqual(f.gc.execute(GcCommand.CLEANUP_NOW), 0)
        self.assertEqual([c for c in f.docker.calls if c[0] == "rm image"], [])
        self.assertIn("[protected registry]", "\n".join(f.console.lines))

    def test_settings_it_cannot_read_stop_every_command_but_the_recorder(self) -> None:
        for config in (FakeConfig("DOCKER_GC_PROTCT=x\n"), FakeConfig(None, installed=("print(1)\n",))):
            for command in (GcCommand.STATUS, GcCommand.RUN, GcCommand.CLEANUP_NOW, GcCommand.SETTINGS):
                f = Fixture(config=config)
                f.state.state = GcState(since=NOW - 30 * DAY)
                f.docker.listed_volumes = [Volume("v", anonymous=True)]
                self.assertEqual(f.gc.execute(command), gc_domain.BAD_SETTINGS_EXIT, command)
                self.assertTrue(f.console.lines and all(ln.startswith("<3>") for ln in f.console.lines))
                self.assertEqual(f.docker.calls, [], "it reached docker")
        f = Fixture(config=FakeConfig("DOCKER_GC_PROTCT=x\n"))
        f.gc.execute(GcCommand.RECORD)
        self.assertEqual(f.config.reads, 0, "the recorder read the settings")


class SettingsFileTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="docker-gc-settings-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = GcPaths(
            self.tmp, self.tmp, os.path.join(self.tmp, "etc", "cst", "docker-gc.conf"), os.path.join(self.tmp, "prog")
        )
        self.files = gc_adapters.GcSettingsFile(self.paths)

    def test_the_file_missing_written_and_unreadable(self) -> None:
        self.assertIsNone(self.files.read_settings())
        self.files.write_settings("DOCKER_GC_PROTECT=\n")
        self.assertEqual(self.files.read_settings(), "DOCKER_GC_PROTECT=\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.settings_file).st_mode), 0o644)
        os.remove(self.paths.settings_file)
        os.mkdir(self.paths.settings_file)  # something there that cannot be read as a file
        with self.assertRaises(gc_domain.UnknownProtection):
            self.files.read_settings()

    def test_the_sources_of_the_installed_program(self) -> None:
        self.assertIsNone(self.files.installed_sources())
        with open(LEGACY_DOMAIN) as f:
            legacy = f.read()
        with zipfile.ZipFile(self.paths.installed, "w") as z:  # a zipapp as CST installs it
            z.writestr("__main__.py", "")
            z.writestr("btrfs/docker_gc/domain.py", legacy)
            z.writestr("btrfs/docker_gc/cli.py", "x = 1\n")
        self.assertEqual(self.files.installed_sources(), (legacy,))
        with open(self.paths.installed, "w") as f:  # the plain script of an older CST
            f.write(OLD_PROTECTED_LINE)
        self.assertEqual(self.files.installed_sources(), (OLD_PROTECTED_LINE,))
        os.remove(self.paths.installed)
        os.symlink(os.path.join(self.tmp, "nowhere"), self.paths.installed)  # there, but unreadable
        self.assertEqual(self.files.installed_sources(), ())
        os.remove(self.paths.installed)
        # A zipapp whose member is damaged (its deflate data overwritten): no sources, never a traceback.
        with zipfile.ZipFile(self.paths.installed, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("btrfs/docker_gc/domain.py", legacy)
            start = z.getinfo("btrfs/docker_gc/domain.py").header_offset
        with open(self.paths.installed, "r+b") as f:
            f.seek(start + 30 + len("btrfs/docker_gc/domain.py"))
            f.write(b"\xff" * 64)
        self.assertTrue(zipfile.is_zipfile(self.paths.installed))
        self.assertEqual(self.files.installed_sources(), ())


class WiringTest(unittest.TestCase):
    def test_settings_and_paths_from_the_environment(self) -> None:
        self.assertEqual(gc_wiring.settings_from({}), GcSettings())
        got = gc_wiring.settings_from(
            {"KEEP_IMAGE_DAYS": "3", "DOCKER_GC_PROTECT": "a.example,b.example", "DOCKER_GC_FLUSH": "5"}
        )
        # DOCKER_GC_PROTECT is read with the settings file, at the command (resolve_protection).
        self.assertEqual((got.keep_image_days, got.protected, got.flush_seconds), (3, (), 5))
        with self.assertRaises(ValueError):
            gc_wiring.settings_from({"KEEP_CONTAINER_DAYS": "x"})
        self.assertEqual(
            gc_wiring.paths_from({}),
            GcPaths("/var/lib/docker-gc", "/run/docker-gc", "/etc/cst/docker-gc.conf", "/usr/local/sbin/docker-gc"),
        )
        self.assertEqual(gc_wiring.paths_from({"DOCKER_GC_STATE": "/s", "DOCKER_GC_RUNTIME": "/r"}).lock, "/r/run.lock")
        env = {"DOCKER_GC_CONF": "/c", "DOCKER_GC_INSTALLED": "/i", "DOCKER_GC_PROTECT": "a.example"}
        p = gc_wiring.paths_from(env)
        self.assertEqual((p.settings_file, p.installed), ("/c", "/i"))
        gc = gc_wiring.build_docker_gc(env)
        self.assertEqual((gc.protect_env, gc.paths), ("a.example", p))

    def test_the_installed_program_is_the_manifests(self) -> None:
        self.assertEqual(GcPaths().installed, manifest_by_src()["btrfs/docker-gc"].dest)


class ContainerRefusalTest(unittest.TestCase):
    """Inside a toolbox or container (wiring.runs_in_container), the commands that talk to docker refuse."""

    def test_each_command_refuses_and_names_the_host_command(self) -> None:
        for (cmd, dry, write), on_host in (
            ((GcCommand.RUN, False, False), "cst btrfs docker-gc run"),
            ((GcCommand.RUN, True, False), "cst btrfs docker-gc run --dry-run"),
            ((GcCommand.CLEANUP_NOW, False, False), "cst btrfs docker-gc cleanup-now"),
            ((GcCommand.STATUS, False, False), "cst btrfs docker-gc status"),
            ((GcCommand.RECORD, False, False), "sudo /usr/local/sbin/docker-gc record"),
            ((GcCommand.SETTINGS, False, True), "sudo /usr/local/sbin/docker-gc settings --write"),
            ((GcCommand.SETTINGS, False, False), "sudo /usr/local/sbin/docker-gc settings"),
        ):
            f = Fixture(config=FakeConfig(text=None, installed=None))
            f.gc.in_container = True
            self.assertEqual(f.gc.execute(cmd, dry=dry, write=write), 1, cmd)
            self.assertEqual(len(f.console.lines), 1, f.console.lines)
            line = f.console.lines[0]
            self.assertTrue(line.startswith("ERR docker-gc "), line)
            self.assertIn(
                f"refused inside a toolbox or container: {PATHS.settings_file} there is the container's", line
            )
            self.assertTrue(line.endswith(f"Run it on the host: {on_host}"), line)
            self.assertEqual((f.docker.calls, f.config.reads, f.config.text), ([], 0, None), cmd)

    def test_settings_runs_when_both_files_are_named(self) -> None:
        f = Fixture(config=FakeConfig(text=None, installed=None))
        f.gc.in_container, f.gc.named_files = True, True
        self.assertEqual(f.gc.execute(GcCommand.SETTINGS, write=True), 0, f.console.lines)
        self.assertIsNotNone(f.config.text)
        f.gc.named_files = False
        self.assertEqual(f.gc.execute(GcCommand.RUN), 1)
        f = Fixture()
        f.gc.in_container, f.gc.named_files = True, True
        self.assertEqual(
            f.gc.execute(GcCommand.STATUS), 1, "a command that talks to docker refuses with the files named too"
        )
        self.assertEqual(f.docker.calls, [])

    def test_the_wiring_tells_the_container(self) -> None:
        named = {"DOCKER_GC_CONF": "/c", "DOCKER_GC_INSTALLED": "/i"}
        for env, want in (
            ({"CST_DOCKER_GC_CONTAINER": "1"}, (True, False)),
            ({"CST_DOCKER_GC_CONTAINER": "0", **named}, (False, True)),
            ({"CST_DOCKER_GC_CONTAINER": "1", "DOCKER_GC_CONF": "/c"}, (True, False)),
        ):
            gc = gc_wiring.build_docker_gc(env)
            self.assertEqual((gc.in_container, gc.named_files), want, env)
        self.assertEqual(gc_wiring.runs_in_container({}), HostExecutionContext().is_container())


class StateFilesTest(unittest.TestCase):
    @override
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

    def test_a_missing_state_folder_is_made_0755_whatever_the_umask(self) -> None:
        # os.makedirs(mode=0o755) before write_atomic made the folder with the umask taken off
        # (0700 under umask 077); write_atomic's own ensure_dir makes it exactly 0755.
        self.addCleanup(os.umask, os.umask(0o077))
        for name, save in (
            ("state", lambda f: f.save_state(GcState(since=5, images={}, heartbeat=9))),
            ("summary", lambda f: f.save_summary(RunSummary(1, 0, 0, 0, 0, 0))),
        ):
            with self.subTest(name=name):
                folder = os.path.join(self.tmp, name, "docker-gc")
                files = gc_adapters.GcStateFiles(GcPaths(folder, os.path.join(self.tmp, "run")))
                save(files)
                self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(folder)).st_mode), 0o755)
                self.assertEqual(len(os.listdir(folder)), 1)

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
                | {"before": {}, "after": {}, "v": 1},
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
            self.assertEqual(f.read(), '{"since": 5, "images": {"sha256:a": 7.5}, "heartbeat": 9, "v": 1}')
        self.assertEqual(self.files.load_state(), GcState(since=5, images={"sha256:a": 7.5}, heartbeat=9))
        for text in (
            "not json",
            "[]",
            '{"since": "x", "images": {}}',
            '{"since": 1, "images": []}',
            # A value that is not a time: the whole file counts as unreadable (recorded from now,
            # so nothing is removed for KEEP_IMAGE_DAYS), never as an image without a use.
            '{"since": 1, "images": {"sha256:a": 5, "sha256:b": "x"}, "heartbeat": 2}',
            '{"since": 1, "images": {}, "heartbeat": null}',
        ):
            with self.subTest(text=text):
                self.write("last-used.json", text)
                self.assertEqual(self.files.load_state().images, {})
                self.assertGreaterEqual(self.files.load_state().since, before)

    def fixture(self, name: str) -> str:
        with open(os.path.join(STATE_FIXTURES, name)) as f:
            return f.read()

    def test_the_writers_produce_the_checked_in_files_exactly(self) -> None:
        # The bytes main's docker-gc was shown to read (REPORT, the mixed-version proof): a change
        # here changes the format, and needs that proof again.
        state = GcState(since=1794000000.5, images={"sha256:" + "a" * 64: 1794000100.25}, heartbeat=1794000200.0)
        self.files.save_state(state)
        summary = RunSummary(1794000300.0, 1, 2, 3, 4500000, 0, {"Images": "1 GB"}, {"Images": "0.5 GB"})
        self.files.save_summary(summary)
        for name, fixture in (
            (self.files.paths.last_used, "docker-gc-last-used-v1.json"),
            (self.files.paths.last_run, "docker-gc-last-run-v1.json"),
        ):
            with self.subTest(fixture), open(name) as f:
                self.assertEqual(f.read(), self.fixture(fixture))
        # What an older docker-gc wrote (no "v"): read the same.
        self.write("last-used.json", self.fixture("docker-gc-last-used-v0.json"))
        self.write("last-run.json", self.fixture("docker-gc-last-run-v0.json"))
        # (status shows only the counts: before and after are not read back)
        self.assertEqual(
            (self.files.load_state(), self.files.load_summary()), (state, RunSummary(1794000300.0, 1, 2, 3, 4500000, 0))
        )

    def test_a_state_of_a_newer_docker_gc_is_refused_and_left_as_it_is(self) -> None:
        for name, load in (("last-used.json", self.files.load_state), ("last-run.json", self.files.load_summary)):
            with self.subTest(name):
                text = '{"since": 1, "images": {}, "heartbeat": 2, "at": 1, "v": 2}'
                self.write(name, text)
                with self.assertRaises(NewerState) as caught:
                    load()
                path = os.path.join(self.tmp, name)
                self.assertTrue(
                    str(caught.exception).startswith(f"{path} is format 2 of the docker-gc-"), caught.exception
                )
                self.assertIn("this CST reads format 1 and older", str(caught.exception))
                with open(path) as f:
                    self.assertEqual(f.read(), text)

    def test_a_version_that_is_no_whole_number_is_not_a_state_docker_gc_wrote(self) -> None:
        before = time.time()
        for v in ('"1"', "true", "-1"):
            with self.subTest(v=v):
                self.write("last-used.json", f'{{"since": 1, "images": {{}}, "heartbeat": 2, "v": {v}}}')
                self.assertGreaterEqual(self.files.load_state().since, before)
                self.write(
                    "last-run.json",
                    f'{{"at": 1, "containers": 1, "volumes": 1, "images": 1, "image_bytes": 1, "failed": 0, "v": {v}}}',
                )
                self.assertIsNone(self.files.load_summary())

    def test_a_summary_that_is_not_one_reads_as_never(self) -> None:
        for text in ("not json", "[]", '{"at": 1}', '{"at": 1, "containers": 1, "volumes": 1, "images": 1}'):
            with self.subTest(text=text):
                self.write("last-run.json", text)
                self.assertIsNone(self.files.load_summary())


class RecorderActiveTest(unittest.TestCase):
    """recorder_active: systemctl is-active with no stdin and a time limit."""

    @override
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
        self.assertEqual(
            (r.returncode, r.stderr),
            (2, "usage: docker-gc record|run [--dry-run]|cleanup-now|status|settings [--write]\n"),
        )

    def test_an_unknown_command_is_a_usage_error_before_the_settings_are_read(self) -> None:
        # Before, the KEEP_* values were parsed first: a bad one gave a ValueError traceback
        # (exit 1) instead of the usage line.
        r = subprocess.run(
            [sys.executable, "-I", "-B", TOOL, "bogus"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "KEEP_IMAGE_DAYS": "x"},
        )
        self.assertEqual(
            (r.returncode, r.stderr),
            (2, "usage: docker-gc record|run [--dry-run]|cleanup-now|status|settings [--write]\n"),
        )

    def test_status_and_a_dry_run_into_a_closed_pipe_stop_quietly(self) -> None:
        # "cst btrfs docker-gc status | head -1" ended in "Exception ignored ...
        # BrokenPipeError" (exit 120). Docker is a stub that is not running.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "docker"), "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(os.path.join(tmp, "docker"), 0o755)
            env = {
                "PATH": f"{tmp}:/usr/bin:/bin",
                "HOME": tmp,
                "DOCKER_GC_STATE": tmp,
                # Never the machine's settings file or installed docker-gc.
                "DOCKER_GC_CONF": os.path.join(tmp, "docker-gc.conf"),
                "DOCKER_GC_INSTALLED": os.path.join(tmp, "no-docker-gc"),
                "CST_DOCKER_GC_CONTAINER": "0",  # on the host, also when the tests run in a toolbox
            }
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


class DockerInspectTest(unittest.TestCase):
    """DockerCli.inspect_all and image_tags over docker's answers (subprocess.run patched)."""

    @override
    def setUp(self) -> None:
        self.docker = gc_adapters.DockerCli(SubprocessCommandRunner())
        self.container: dict[str, object] = {
            "Id": "c1",
            "Image": "sha256:a",
            "State": {"Status": "exited"},
            "Config": {"Image": "a:1", "Labels": None},
            "HostConfig": {},
            "Mounts": [{"Type": "volume", "Name": "v1"}],
        }

    def answer(self, records: object) -> contextlib.AbstractContextManager[object]:
        def run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["ps", "-aq"]:
                return subprocess.CompletedProcess(argv, 0, "c1\n", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

        return mock.patch.object(subprocess, "run", side_effect=run)

    def test_a_record_without_a_key_the_plan_relies_on_is_an_inspect_error(self) -> None:
        for key in ("Image", "State", "Config", "HostConfig", "Mounts"):
            with self.subTest(key=key):
                rec = {k: v for k, v in self.container.items() if k != key}
                with self.answer([rec]), self.assertRaises(gc_domain.IncompleteRecordError) as stop:
                    self.docker.containers()
                self.assertEqual(str(stop.exception), f"container inspect: c1: no {key}")
        for key, value, kind in (("Image", None, "NoneType"), ("Mounts", "x", "str"), ("State", [], "list")):
            with (
                self.subTest(key=key, value=value),
                self.answer([{**self.container, key: value}]),
                self.assertRaisesRegex(gc_domain.InspectError, f"^container inspect: c1: {key} is {kind}$"),
            ):
                self.docker.containers()
        with (
            self.answer([{**self.container, "Config": {"Image": "a:1"}}]),
            self.assertRaises(gc_domain.InspectError) as stop,
        ):
            self.docker.containers()
        self.assertEqual(str(stop.exception), "container inspect: c1: no Config.Labels")

    def test_a_record_that_is_no_object_is_an_inspect_error(self) -> None:
        # Not taken for an empty container next to the real one: the run stops, naming it.
        for junk, shown in (("junk", "junk"), (5, "5"), (None, "None"), ([], "[]")):
            with (
                self.subTest(junk=junk),
                self.answer([self.container, junk]),
                self.assertRaises(gc_domain.InspectError) as stop,
            ):
                self.docker.containers()
            self.assertEqual(str(stop.exception), f"container inspect: {shown}: not an object")

    def test_docker_s_nulls_are_accepted(self) -> None:
        with self.answer([{**self.container, "Mounts": None}]):
            [c] = self.docker.containers()
        self.assertEqual((c.image_id, c.volumes, c.label_keys), ("sha256:a", frozenset(), frozenset()))

    def test_image_tags_without_repo_tags_cannot_rule_tags_out(self) -> None:
        for record, tags in (
            ({"Id": "sha256:a"}, ["?"]),
            ({"Id": "sha256:a", "RepoTags": "a:1"}, ["?"]),
            ({"Id": "sha256:a", "RepoTags": None}, []),
            ({"Id": "sha256:a", "RepoTags": ["a:1", 3]}, ["a:1"]),
        ):
            with self.subTest(record=record), self.answer([record]):
                self.assertEqual(self.docker.image_tags("sha256:a"), tags)


class DockerCommandTest(unittest.TestCase):
    """DockerCli.run: a docker that does not answer stops the run with one line; one that is missing raises."""

    @override
    def setUp(self) -> None:
        self.docker = gc_adapters.DockerCli(SubprocessCommandRunner())

    def test_no_answer_and_missing(self) -> None:
        hang = subprocess.TimeoutExpired(["docker"], 30)
        with mock.patch.object(subprocess, "run", side_effect=hang), self.assertRaises(RuntimeError) as stop:
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

    def info_says(self, rc: int, stderr: str) -> bool:
        done = subprocess.CompletedProcess(["docker"], rc, "", stderr)
        with mock.patch.object(subprocess, "run", return_value=done):
            return self.docker.is_up()

    def test_is_up_only_says_not_running_when_the_daemon_is_not_running(self) -> None:
        self.assertTrue(self.info_says(0, ""))
        for down in (
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n",
            (
                "failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct "
                "and if the daemon is running: dial unix /var/run/docker.sock: connect: no such file or directory\n"
            ),
            "... dial unix /var/run/docker.sock: connect: connection refused\n",
        ):
            self.assertFalse(self.info_says(1, down), down)
        missing = FileNotFoundError(2, "No such file or directory", "docker")
        with mock.patch.object(subprocess, "run", side_effect=missing):
            self.assertFalse(self.docker.is_up())

    def test_is_up_reports_any_other_docker_error(self) -> None:
        denied = (
            "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock: "
            "dial unix /var/run/docker.sock: connect: permission denied\n"
        )
        for stderr, want in ((denied, "docker info: " + denied.strip()), ("", "docker info: exit 1")):
            with self.assertRaises(RuntimeError) as stop:
                self.info_says(1, stderr)
            self.assertEqual(str(stop.exception), want)

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
