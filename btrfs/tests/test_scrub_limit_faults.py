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
its installer, before its daemon-reload), so the repair run itself is under fault; each of the six
with no install record and with btrfs:core done in it, and set 1G over 500M with btrfs:core pending.
For each, every
mode at every point, and every FAIL followed by a KILL at each later point of that run (a run that
dies while it rolls back), checked:
- no OSError gets out (it would be a traceback);
- a run that returns 0 left the target: snapshots.conf and the drop-in as asked, and systemd has
  loaded that drop-in;
- a run that returns non-zero left both files and systemd's view as they were, or its last line
  says ROLLBACK INCOMPLETE; it says systemd has not loaded the files only from a start where
  systemd was behind;
- converges: the same command again, with no fault, returns 0 and leaves the target, loaded;
- btrfs:core's state in the install record (SystemdModel records the installer's writes through the
  real InstallService): no run, whatever its end (exit 0, killed, failed, rolled back), and not the
  run again, changes the state btrfs:core had before the first run. A start with no state for it (no
  record: btrfs:core never installed) may end with btrfs:core pending, never done, and never with a
  state no recorded file names.

The start states with btrfs:core in the record hold a second file of it besides the drop-in (a seed
text, as an install leaves btrfs:core): off's remove-text then leaves btrfs:core a file, so its state.

One class per start state (a work unit of tests/run_unit.py), one method per mode family, the
case counts pinned.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
from dataclasses import dataclass, replace
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
from btrfs.tests.fakes import RecordingInstallerRunner, core_text
from cst_core.adapters.install import JsonInstallRecord
from cst_core.domain.record import ComponentState
from cst_core.ports import CommandResult
from tests.fault_injection import Fault, FaultMode, FaultPlan, FaultyRunner, Killed, cut_points, every_fault, stub_fsync
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
    (behind: not what is on disk), btrfs:core's state in the install record (None: no record), and the
    pinned numbers of points and of FAIL then KILL cases."""

    conf: str | None
    dropin: str | None
    limit: str | None
    points: int
    fail_then_kill: int
    loaded: str | None = None
    behind: bool = False
    core: ComponentState | None = None


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


class SystemdModel(RecordingInstallerRunner):
    """The installer (its writes and their record through the real InstallService) and systemctl, and
    what systemd has loaded of the drop-in."""

    def __init__(self, dropin: str, start: Start, record: str) -> None:
        super().__init__(record)
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


class FaultyOs:
    """The os module as scrub-limit's adapters see it, with os.remove and os.rmdir through a plan
    (only that module's calls: the fake installer's own removals are another process's)."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan
        self.path = os.path

    def remove(self, path: str) -> None:
        self.plan.step("os.remove", functools.partial(os.remove, path))

    def rmdir(self, path: str) -> None:
        self.plan.step("os.rmdir", functools.partial(os.rmdir, path))

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)


@contextlib.contextmanager
def faulty_files(plan: FaultPlan) -> Generator[None]:
    """scrub-limit's own file calls through plan while the block runs: ensure_dir and write_atomic as
    it imports them, os.remove and os.rmdir as it calls them."""
    real_ensure, real_write = scrub_adapters.ensure_dir, scrub_adapters.write_atomic

    def ensure_dir(path: str, mode: int) -> None:
        plan.step("ensure_dir", functools.partial(real_ensure, path, mode))

    def write_atomic(path: str, text: str, **kw: object) -> None:
        plan.step("write_atomic conf", functools.partial(real_write, path, text, **kw))

    with (
        mock.patch.object(scrub_adapters, "ensure_dir", ensure_dir),
        mock.patch.object(scrub_adapters, "write_atomic", write_atomic),
        mock.patch.object(scrub_adapters, "os", FaultyOs(plan)),
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
        )
        self.record = os.path.join(self.tmp, "installed.json")  # never the real install record
        self.plan = FaultPlan()

    def fresh(self) -> SystemdModel:
        """The start state on disk, and systemd's view of it."""
        s = self.start
        shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)
        if s.conf is not None:
            write(self.conf, s.conf)
        if s.dropin is not None:
            write(self.dropin, s.dropin)
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.record)
        model = SystemdModel(self.dropin, s, self.record)
        if s.core is not None:
            # btrfs:core as an install leaves it: a seed text and the drop-in recorded, then done (or not).
            model.svc.put(core_text(os.path.join(self.tmp, "core-seed"), self.record), b"seed\n", as_root=False)
            if s.dropin is not None:
                model.svc.put(core_text(self.dropin, self.record), s.dropin.encode(), as_root=False)
            if s.core == ComponentState.DONE:
                model.svc.mark(self.record, "btrfs:core")
        return model

    def assert_core(self, when: str) -> None:
        """btrfs:core's state is the start's (with no state at the start: none, or pending with a file)."""
        rec = JsonInstallRecord().read(self.record)
        state = None if rec is None else rec.components.get("btrfs:core")
        if self.start.core is not None:
            self.assertEqual(state, self.start.core, f"{when}: btrfs:core's state")
            return
        self.assertIn(state, (None, ComponentState.PENDING), f"{when}: btrfs:core's state")
        if state is not None and rec is not None:
            self.assertTrue(any(f.component == "btrfs:core" for f in rec.files.values()), f"{when}: {rec}")

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
        self.assert_core(f"{when}: the run")
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
        self.assert_core(f"{when}: the run again")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one run from the start state with these faults."""
        model = self.fresh()
        if not faults:
            calls = cut_points(self.plan, functools.partial(self.run_once, model))
            self.assert_target(model, "no fault")
            self.assert_core("no fault")
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


# The same starts with btrfs:core in the install record: done for each, pending for one. The points
# and the FAIL then KILL counts are the same as without a record (scrub-limit never marks btrfs:core).
DONE = ComponentState.DONE


class SetBothNewCoreDoneFaultTest(SetBothNewFaultTest):
    """set 500M: no snapshots.conf, no drop-in; btrfs:core done."""

    start = replace(SET_BOTH_NEW, core=DONE)


class SetKeepsAKeyCoreDoneFaultTest(SetKeepsAKeyFaultTest):
    """set 500M: snapshots.conf with another key, no drop-in; btrfs:core done."""

    start = replace(SET_KEEPS_A_KEY, core=DONE)


class Set500MTo1GCoreDoneFaultTest(Set500MTo1GFaultTest):
    """set 1G over 500M, systemd in step; btrfs:core done."""

    start = replace(SET_500M_TO_1G, core=DONE)


class Set500MTo1GCorePendingFaultTest(Set500MTo1GFaultTest):
    """set 1G over 500M, systemd in step; btrfs:core pending (an install that did not finish)."""

    start = replace(SET_500M_TO_1G, core=ComponentState.PENDING)


class OffCoreDoneFaultTest(OffFaultTest):
    """off from 500M, systemd in step; btrfs:core done."""

    start = replace(OFF, core=DONE)


class SetBehindCoreDoneFaultTest(SetBehindFaultTest):
    """set 500M again after a first-ever set killed after its installer; btrfs:core done."""

    start = replace(SET_BEHIND, core=DONE)


class OffBehindCoreDoneFaultTest(OffBehindFaultTest):
    """off again after an off killed after its installer; btrfs:core done."""

    start = replace(OFF_BEHIND, core=DONE)
