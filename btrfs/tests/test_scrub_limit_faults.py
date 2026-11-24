# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs scrub-limit set|off` (tests/fault_injection.py).

scrub_limit_apply writes snapshots.conf itself (cst_core's ensure_dir and write_atomic, os.remove
and os.rmdir in its rollback) and the drop-in through the installer, then reloads systemd; both of
those are commands. Each of these mutating calls goes through a FaultPlan: the module's own file
calls by patching its names, the commands through FaultyRunner (systemctl show only reads).
SystemdModel, the runner under it, keeps what systemd has loaded: the drop-in's text at the last
daemon-reload that worked, and whether a write or a removal of the drop-in came since (systemd's
NeedDaemonReload, which follows the files' times and the list of drop-ins, not their text).
os.fsync is stubbed.

Start states: four with systemd in step, and two where it is behind (an earlier run stopped after
its installer, before its daemon-reload), so the repair run itself is under fault. For each, every
mode at every point, and every FAIL followed by a KILL at each later point of that run (a run that
dies while it rolls back), checked:
- no OSError gets out (it would be a traceback);
- a run that returns 0 left the target: snapshots.conf and the drop-in as asked, and systemd has
  loaded that drop-in;
- a run that returns non-zero left both files and systemd's view as they were, or its last line
  says ROLLBACK INCOMPLETE; it says systemd has not loaded the files only from a start where
  systemd was behind;
- converges: the same command again, with no fault, returns 0 and leaves the target, loaded.

One class per start state (a work unit of tests/run_unit.py), one method per mode family, the
case counts pinned.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
from dataclasses import dataclass
import functools
import os
import shutil
import tempfile
from typing import ClassVar, override
import unittest
from unittest import mock

from btrfs.scrub_limit import adapters as scrub_adapters
from btrfs.scrub_limit.adapters import ScrubPaths, scrub_limit_apply
from btrfs.scrub_limit.domain import render_scrub_dropin, update_snapshots_conf_scrub_limit
from btrfs.tests.fakes import ScrubFakeRunner
from cst_core.ports import CommandResult
from tests.fault_injection import (
    Fault,
    FaultMode,
    FaultPlan,
    FaultyOs,
    FaultyRunner,
    Killed,
    cut_points,
    every_fault,
    stub_fsync,
)
from tests.repo_files import REPO

DROPIN_500M = render_scrub_dropin("500M")
NOT_LOADED = "systemd has not loaded them"


def as_asked(conf: str | None, limit: str | None) -> str | None:
    """snapshots.conf as set LIMIT (off: None) leaves it."""
    new = update_snapshots_conf_scrub_limit(conf, limit)
    return conf if new is None else new


@dataclass(frozen=True)
class Start:
    """A state before the run: the two files, the limit asked (None: off), what systemd has loaded
    (behind: not what is on disk), and the pinned numbers of points and of FAIL then KILL cases."""

    conf: str | None
    dropin: str | None
    limit: str | None
    points: int
    fail_then_kill: int
    loaded: str | None = None
    behind: bool = False


SET_BOTH_NEW = Start(None, None, "500M", points=4, fail_then_kill=8)
SET_KEEPS_A_KEY = Start("NAS_ENABLED=1\n", None, "500M", points=4, fail_then_kill=6)
SET_500M_TO_1G = Start("SCRUB_LIMIT='500M'\n", DROPIN_500M, "1G", points=4, fail_then_kill=5, loaded=DROPIN_500M)
OFF = Start("SCRUB_LIMIT='500M'\n", DROPIN_500M, None, points=5, fail_then_kill=6, loaded=DROPIN_500M)
# A first-ever set 500M killed after its installer: the files as asked, systemd loaded no drop-in.
SET_BEHIND = Start(as_asked(None, "500M"), DROPIN_500M, "500M", points=2, fail_then_kill=0, behind=True)
# An off killed after its installer: no drop-in, no SCRUB_LIMIT, systemd still has the 500M drop-in.
OFF_BEHIND = Start(
    as_asked("SCRUB_LIMIT='500M'\n", None), None, None, points=3, fail_then_kill=1, loaded=DROPIN_500M, behind=True
)


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class SystemdModel(ScrubFakeRunner):
    """The fake installer and systemctl, and what systemd has loaded of the drop-in."""

    def __init__(self, dropin: str, start: Start) -> None:
        super().__init__()
        self.dropin = dropin
        self.loaded = start.loaded
        self.dirty = start.behind  # a write or a removal of the drop-in since the last reload that worked

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        self.need_daemon_reload = "yes\n" if self.dirty else "no\n"
        r = super().run(argv, input_text=input_text, **kw)
        if "installer.py" in " ".join(cmd) and r.stdout.startswith(("written:", "removed:")):
            self.dirty = True
        if cmd == ["systemctl", "daemon-reload"] and r.returncode == 0:
            self.loaded, self.dirty = read(self.dropin), False
        return r


def reads(argv: Sequence[str]) -> bool:
    return list(argv[:2]) == ["systemctl", "show"]


def point_name(argv: Sequence[str]) -> str:
    return f"installer {argv[4]}" if "installer.py" in " ".join(argv) else " ".join(argv)


@contextlib.contextmanager
def faulty_files(plan: FaultPlan) -> Generator[None]:
    """scrub-limit's own file calls through plan while the block runs: ensure_dir and write_atomic as
    it imports them, os.remove and os.rmdir as it calls them (only that module's calls: the fake
    installer's own removals are another process's)."""
    real_ensure, real_write = scrub_adapters.ensure_dir, scrub_adapters.write_atomic

    def ensure_dir(path: str, mode: int) -> None:
        plan.step("ensure_dir", functools.partial(real_ensure, path, mode))

    def write_atomic(path: str, text: str, **kw: object) -> None:
        plan.step("write_atomic conf", functools.partial(real_write, path, text, **kw))

    with (
        mock.patch.object(scrub_adapters, "ensure_dir", ensure_dir),
        mock.patch.object(scrub_adapters, "write_atomic", write_atomic),
        mock.patch.object(scrub_adapters, "os", FaultyOs(plan, ("remove", "rmdir"))),
    ):
        yield


class ScrubFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-scrub-faults-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.conf = os.path.join(self.tmp, "etc", "btrfs-setup", "snapshots.conf")
        self.dropin = os.path.join(
            self.tmp, "etc", "systemd", "system", "btrfs-scrub-weekly.service.d", "10-cst-limit.conf"
        )
        self.paths = ScrubPaths(
            conf_path=self.conf,
            dropin_path=self.dropin,
            btrfs_sysfs=os.path.join(self.tmp, "sys"),
            installer_script=os.path.join(REPO, "cst_core", "installer.py"),
            record_path=os.path.join(self.tmp, "installed.json"),  # never the real install record
        )
        self.plan = FaultPlan()

    def fresh(self) -> SystemdModel:
        """The start state on disk, and systemd's view of it."""
        s = self.start
        shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)
        if s.conf is not None:
            write(self.conf, s.conf)
        if s.dropin is not None:
            write(self.dropin, s.dropin)
        return SystemdModel(self.dropin, s)

    def run_once(self, model: SystemdModel, *faults: Fault) -> tuple[int, list[str]] | None:
        """One run of the start's command with these faults; None when it was killed."""
        self.plan.arm(*faults)
        runner = FaultyRunner(self.plan, model, reads=reads, name=point_name)
        with faulty_files(self.plan):
            try:
                return scrub_limit_apply(self.paths, self.start.limit, runner=runner)
            except Killed:
                return None
            except OSError as e:
                self.fail(f"{faults}: an OSError got out (a traceback) at {self.plan.calls[-1]}: {e}")

    def assert_target(self, model: SystemdModel, when: str) -> None:
        s = self.start
        self.assertEqual(read(self.conf), as_asked(s.conf, s.limit), f"{when}: snapshots.conf")
        self.assertEqual(
            read(self.dropin), None if s.limit is None else render_scrub_dropin(s.limit), f"{when}: drop-in"
        )
        self.assertEqual(model.loaded, read(self.dropin), f"{when}: systemd did not load the drop-in")

    def check(self, *faults: Fault) -> None:
        s = self.start
        when = f"{faults}"
        model = self.fresh()
        result = self.run_once(model, *faults)
        if result is not None:
            rc, lines = result
            if rc == 0:
                self.assert_target(model, f"{when}: exit 0")
            elif "ROLLBACK INCOMPLETE" not in lines[-1]:
                files = (read(self.conf), read(self.dropin))
                self.assertEqual(files, (s.conf, s.dropin), f"{when}: exit {rc}: {lines}")
                self.assertEqual(model.loaded, s.loaded, f"{when}: exit {rc}: systemd's view: {lines}")
                if NOT_LOADED in lines[-1]:
                    self.assertTrue(s.behind, f"{when}: exit {rc}: systemd's view was in step: {lines}")
        rc_again, lines_again = self.run_once(model) or (None, [])
        self.assertEqual(rc_again, 0, f"{when}: the run again: {lines_again}")
        self.assert_target(model, f"{when}: the run again")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one run from the start state with these faults."""
        model = self.fresh()
        if not faults:
            calls = cut_points(self.plan, functools.partial(self.run_once, model))
            self.assert_target(model, "no fault")
            return calls
        self.run_once(model, *faults)
        return list(self.plan.calls)

    def every(self, mode: FaultMode) -> None:
        calls = self.points()
        self.assertEqual(len(calls), self.start.points, calls)
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                self.check(fault)

    def every_failure_then_a_kill(self) -> None:
        cases = 0
        for fail in every_fault(self.start.points, (FaultMode.FAIL,)):
            for kill in every_fault(len(self.points(fail)), (FaultMode.KILL,), after=fail[1]):
                with self.subTest(fault=fail, then=kill):
                    cases += 1
                    self.check(fail, kill)
        self.assertEqual(cases, self.start.fail_then_kill)


class SetBothNewFaultTest(ScrubFaultBase):
    """set 500M: no snapshots.conf, no drop-in."""

    start = SET_BOTH_NEW

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class SetKeepsAKeyFaultTest(ScrubFaultBase):
    """set 500M: snapshots.conf with another key, no drop-in."""

    start = SET_KEEPS_A_KEY

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class Set500MTo1GFaultTest(ScrubFaultBase):
    """set 1G over 500M, systemd in step."""

    start = SET_500M_TO_1G

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class OffFaultTest(ScrubFaultBase):
    """off from 500M, systemd in step."""

    start = OFF

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class SetBehindFaultTest(ScrubFaultBase):
    """set 500M again after a first-ever set killed after its installer (systemd loaded no drop-in)."""

    start = SET_BEHIND

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class OffBehindFaultTest(ScrubFaultBase):
    """off again after an off killed after its installer (systemd still has the 500M drop-in)."""

    start = OFF_BEHIND

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()
