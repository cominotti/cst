# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst bluetooth apply` and `revert`: their restarts and the record
of the restarts still owed (tests/fault_injection.py).

The use case runs in process (Change.run_command over test_setup_units' change()), on SetupModel
(setup_fakes: FAKE in Python, which also keeps what bluetoothd and WirePlumber read at their last
start). Its mutating calls go through a FaultPlan: the commands that change something (sudo cp,
install and mv of main.conf, the restarts) through FaultyRunner; the user's files (write_user_file,
write_through, copy, remove_file, make_dirs) through a LocalSetupFiles of the test; and the save
of the record (restart_pending with save) through a LocalBluetoothHostAdapter of the test. The
LDAC watcher is off. os.fsync is stubbed.

Start states: a fresh apply (bluetoothd and WirePlumber both owe a restart), an apply where only
WirePlumber owes one, an apply that resumes a --no-restart run's record (both owed), and a revert
of an applied machine. For each, every mode at every point, every FAIL followed by a KILL at each
later point, and every kill followed by each fault at each point of the run that resumes, checked
for each of the two services:
- R1, after the faulted run: the service runs with what it reads now, or the record owes its
  restart under its current invocation, or the run said it could not record the restarts still
  owed, or the run returned and said how to finish ("To finish:", "Not restarted"), or a faulted
  run of the case lost that service's restart in the window;
- R2, resume: the same command again with no fault exits 0 and leaves each service running with
  what it reads, except one whose restart a faulted run lost in the window: after an apply,
  `plan` says "no restart needed" for it (the documented outcome, F-B1 below); after an apply
  the record owes nothing;
- R3, never twice: the run that resumes restarts a service only when it read other files than are
  on disk (WirePlumber: or bluetoothd restarted after it) or the record owed it; a third run says
  "none needed", restarts nothing and changes nothing;
- no exception gets out of run_command but a kill.

The window (F-B1 in the R5 out-of-scope notes; bluetooth/README.md, section 4: "a restart is owed
to the next apply only once the run reached its restarts"): the record is saved only in
do_restarts, after every write. A run that stops after a write took effect (main.conf for
bluetoothd, a .conf of WirePlumber's folder for WirePlumber) and before a save of the record worked
loses that service's restart; the cases are counted and pinned per class, so a fix shows up as a
change of these numbers.

Each run's clock is a minute after the one before, so each main.conf backup has a name of its own
(revert takes the two keys from the oldest backup).

After a revert, only apply resumes a restart the record owes (change.py: "apply resumes them"):
revert again finds nothing to revert and says "none needed". So for the revert start, R2 accepts a
restart owed in the record, which `plan` then shows as still owed (F-B2 in the out-of-scope notes).

One class per start state and mode family (a work unit of tests/run_unit.py), the case counts pinned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import functools
import os
import re
import shutil
from typing import ClassVar, override
import unittest
from unittest import mock

from bluetooth.adapters import LocalBluetoothHostAdapter
from bluetooth.domain import parse_pending
from bluetooth.setup.adapters import LocalSetupFiles
from bluetooth.tests.setup_fakes import BLUETOOTH_SERVICE, WIREPLUMBER_SERVICE, Machine, SetupModel
from bluetooth.tests.test_setup_units import DONE, SetupDoubles, change
from tests.fault_injection import (
    ALL_MODES,
    Fault,
    FaultMode,
    FaultPlan,
    FaultyRunner,
    Killed,
    cut_points,
    every_fault,
    stub_fsync,
)

UNITS = (BLUETOOTH_SERVICE, WIREPLUMBER_SERVICE)
VENDOR = "[General]\n#Experimental = false\n"
SAVE = "record save"
SCRATCH = "write_user_file tmp."  # the scratch copy of main.conf every run writes (mktemp's file)
FILE_POINTS = ("sudo mv", "write_user_file", "remove_file", "copy")  # the changes a service reads
APPLY, REVERT = ("apply", "--yes"), ("revert", "--yes")


@dataclass(frozen=True)
class Start:
    """A start state: main.conf, the settings, the clean runs that make it, the command under
    fault, and the pinned numbers: points, FAIL then KILL cases, kill then fault cases, window cases."""

    main_conf: str
    settings: tuple[tuple[str, str], ...]
    before: tuple[tuple[str, ...], ...]
    command: tuple[str, ...]
    points: int
    fail_then_kill: int
    kill_then_fault: int
    window: tuple[int, int, int]  # the cases in the window, for KILL, FAIL and FAIL_FROM


APPLY_FRESH = Start(VENDOR, (), (), APPLY, points=12, fail_then_kill=3, kill_then_fault=228, window=(5, 4, 5))
APPLY_WIREPLUMBER_ONLY = Start(DONE, (), (), APPLY, points=8, fail_then_kill=2, kill_then_fault=105, window=(3, 2, 3))
APPLY_RESUME = Start(
    VENDOR,
    (),
    (("apply", "--yes", "--no-restart"),),
    APPLY,
    points=5,
    fail_then_kill=3,
    kill_then_fault=48,
    window=(0, 0, 0),
)
REVERT_APPLIED = Start(VENDOR, (), (APPLY,), REVERT, points=10, fail_then_kill=3, kill_then_fault=138, window=(3, 2, 3))


class FaultyFiles(LocalSetupFiles):
    """The user's files, each change a point of plan."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan

    @override
    def write_user_file(self, path: str, text: str, mode: int) -> None:
        self.plan.step(
            f"write_user_file {os.path.basename(path)}", functools.partial(super().write_user_file, path, text, mode)
        )

    @override
    def write_through(self, path: str, text: str) -> None:
        self.plan.step(f"write_through {os.path.basename(path)}", functools.partial(super().write_through, path, text))

    @override
    def copy(self, src: str, dest: str) -> None:
        self.plan.step(f"copy {os.path.basename(dest)}", functools.partial(super().copy, src, dest))

    @override
    def remove_file(self, path: str) -> bool:
        return self.plan.step(f"remove_file {os.path.basename(path)}", functools.partial(super().remove_file, path))

    @override
    def make_dirs(self, path: str) -> None:
        self.plan.step(f"make_dirs {os.path.basename(path)}", functools.partial(super().make_dirs, path))


class FaultyHost(LocalBluetoothHostAdapter):
    """The host adapter, the save of the record a point of plan (a look at it only reads)."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan

    @override
    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        real = functools.partial(super().restart_pending, path, ids, skipped, save=save)
        return self.plan.step(SAVE, real) if save else real()


def changes(argv: Sequence[str]) -> bool:
    """A command that changes the machine: sudo (main.conf, bluetoothd's restart), a user unit's restart."""
    return argv[0] == "sudo" or (argv[0] == "systemctl" and "restart" in argv)


def point_name(argv: Sequence[str]) -> str:
    return " ".join(os.path.basename(a) for a in argv[:3])


class SetupFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.m = Machine(self)
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{self.m.bin}:/usr/bin:/bin"}))
        self.plan = FaultPlan()
        self.model = SetupModel(self.m)
        self.runs = 0  # each run a minute after the one before: its main.conf backup has a name of its own
        self.lost: set[str] = set()  # the services whose restart a faulted run of this case lost in the window

    # ---- the fixture ------------------------------------------------------
    def fresh(self) -> None:
        """The start state: the machine, then the clean runs that make it."""
        m, s = self.m, self.start
        for d in (m.home, m.root, m.state):
            shutil.rmtree(d)
        for d in (m.home, m.state, os.path.join(m.root, "etc", "bluetooth")):
            os.makedirs(d)
        m.put(m.main_conf, s.main_conf)
        m.services()
        m.adapter()
        m.flag("iso-socket")
        m.put("wp-roles", "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n")
        self.model = SetupModel(m)
        self.lost = set()
        for args in s.before:
            result = self.run_once(args)
            self.assertEqual(result and result[0], 0, f"the start's {args}: {result}")

    def run_once(self, args: Sequence[str], *faults: Fault) -> tuple[int, str] | None:
        """One run of args with these faults: (exit, stdout and stderr); None when it was killed."""
        self.plan.arm(*faults)
        self.runs += 1
        runner = FaultyRunner(self.plan, self.model, reads=lambda a: not changes(a), name=point_name)
        run, console, _ = change(
            self.m,
            *args,
            settings=dict(self.start.settings),
            doubles=SetupDoubles(
                runner=runner,
                files=FaultyFiles(self.plan),
                host=FaultyHost(self.plan),
                now=1_700_000_000.0 + 60 * self.runs,
            ),
        )
        try:
            rc = run.run_command()
        except Killed:
            rc = None
        if run.tmp and os.path.lexists(run.tmp):
            os.unlink(run.tmp)  # the scratch copy (the program's cleanup, not under fault)
        if rc is None:
            return None
        assert isinstance(rc, int), rc
        return rc, console.stdout + console.stderr

    # ---- what the state is --------------------------------------------------
    def owed(self) -> set[str]:
        """The units the record owes a restart under their current invocation."""
        try:
            with open(self.m.pending, encoding="utf-8") as f:
                recorded = parse_pending(f.read())
        except FileNotFoundError:
            recorded = {}
        return {u for u, inv in recorded.items() if inv == self.model.inv(u)}

    def in_window(self) -> set[str]:
        """The services whose files the last run changed with no save of the record working after it:
        bluetoothd for main.conf (sudo mv), WirePlumber for a .conf of its folder."""
        window: set[str] = set()
        for i, name in enumerate(self.plan.calls, 1):
            if self.plan.fault_at(i) in (FaultMode.FAIL, FaultMode.FAIL_FROM):
                continue  # this change did not take effect
            word, _, rest = name.partition(" ")
            if name.startswith("sudo mv"):
                window.add(BLUETOOTH_SERVICE)
            elif word in ("write_user_file", "remove_file") and rest.endswith(".conf"):
                window.add(WIREPLUMBER_SERVICE)
            elif name == SAVE:
                window.clear()
        return window

    # ---- the checks ---------------------------------------------------------
    def assert_r1(self, result: tuple[int, str] | None, when: str) -> None:
        self.lost |= self.in_window()
        text = "" if result is None else result[1]
        warned = "could not record the restarts still owed" in text
        hinted = result is not None and ("To finish:" in text or "Not restarted" in text)
        owed = self.owed()
        for u in UNITS:
            ok = self.model.in_step(u) or u in owed or warned or hinted or u in self.lost
            self.assertTrue(ok, f"{when}: {u} runs with old files, and nothing owes its restart: {text}")

    def resume(self, when: str) -> None:
        """R2 and R3: the command again, then a third time."""
        s = self.start
        owed_before = self.owed()
        first = len(self.model.restarts)
        result = self.run_once(s.command)
        self.assertEqual((result or (None, ""))[0], 0, f"{when}: the run again: {result}")
        for u, needed in self.model.restarts[first:]:
            self.assertTrue(needed or u in owed_before, f"{when}: the run again restarted {u} a second time")
        owed = self.owed()
        for u in UNITS:
            if self.model.in_step(u):
                continue
            if s.command == REVERT and u in owed:
                continue  # only apply resumes it (F-B2)
            self.assertIn(u, self.lost, f"{when}: the run again left {u} on old files, outside the window")
            if s.command == REVERT:
                continue  # plan shows what apply would do, not revert
            plan = self.run_once(("plan",))
            label = "bluetooth.service" if u == BLUETOOTH_SERVICE else "wireplumber (your session)"
            self.assertRegex((plan or (0, ""))[1], rf"ok +{re.escape(label)} +no restart needed", f"{when}: plan")
        if s.command == APPLY:
            self.assertEqual(self.owed(), set(), f"{when}: the record still owes a restart after the run again")
        third_from = len(self.model.restarts)
        third = self.run_once(s.command)
        self.assertEqual((third or (None, ""))[0], 0, f"{when}: the third run: {third}")
        self.assertIn("== restarts\nnone needed\n", (third or (0, ""))[1], f"{when}: the third run")
        self.assertEqual(self.model.restarts[third_from:], [], f"{when}: the third run restarted")
        changed = [c for c in self.plan.calls if not c.startswith(SCRATCH)]
        self.assertEqual(changed, [], f"{when}: the third run changed something")

    def check(self, *faults: Fault, then: tuple[Fault, ...] = ()) -> bool:
        """One case; whether a faulted run of it stopped in the window."""
        when = f"{faults}" + (f" then {then}" if then else "")
        self.fresh()
        self.assert_r1(self.run_once(self.start.command, *faults), when)
        if then:
            self.assert_r1(self.run_once(self.start.command, *then), when)
        self.resume(when)
        return bool(self.lost)

    def points(self, *faults: Fault) -> list[str]:
        self.fresh()
        if not faults:
            calls = cut_points(self.plan, functools.partial(self.run_once, self.start.command))
            for u in UNITS:
                self.assertTrue(self.model.in_step(u), f"no fault: {u}")
            return calls
        self.run_once(self.start.command, *faults)
        return list(self.plan.calls)

    def every(self, mode: FaultMode) -> None:
        calls = self.points()
        self.assertEqual(len(calls), self.start.points, calls)
        window = 0
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                window += self.check(fault)
        self.assertEqual(window, self.start.window[ALL_MODES.index(mode)], "cases in the window")

    def every_failure_then_a_kill(self) -> None:
        cases = 0
        for fail in every_fault(self.start.points, (FaultMode.FAIL,)):
            for kill in every_fault(len(self.points(fail)), (FaultMode.KILL,), after=fail[1]):
                with self.subTest(fault=fail, then=kill):
                    cases += 1
                    self.check(fail, kill)
        self.assertEqual(cases, self.start.fail_then_kill)

    def every_kill_then_a_fault_in_the_resume(self) -> None:
        cases = 0
        for kill in every_fault(self.start.points, (FaultMode.KILL,)):
            self.fresh()
            self.run_once(self.start.command, kill)
            self.run_once(self.start.command)
            for fault in every_fault(len(self.plan.calls)):
                with self.subTest(kill=kill, then=fault):
                    cases += 1
                    self.check(kill, then=(fault,))
        self.assertEqual(cases, self.start.kill_then_fault)


class ApplyFreshFaultTest(SetupFaultBase):
    """apply on a vendor main.conf, no rule: both services owe a restart."""

    start = APPLY_FRESH

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class ApplyFreshResumeFaultTest(SetupFaultBase):
    """apply on a vendor main.conf, no rule: both services owe a restart. A kill, then a fault in the run that resumes."""

    start = APPLY_FRESH

    def test_every_kill_then_a_fault_in_the_resume(self) -> None:
        self.every_kill_then_a_fault_in_the_resume()


class ApplyWirePlumberOnlyFaultTest(SetupFaultBase):
    """apply with main.conf done, no rule: only WirePlumber owes a restart."""

    start = APPLY_WIREPLUMBER_ONLY

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class ApplyWirePlumberOnlyResumeFaultTest(SetupFaultBase):
    """apply with main.conf done, no rule: only WirePlumber owes a restart. A kill, then a fault in the run that resumes."""

    start = APPLY_WIREPLUMBER_ONLY

    def test_every_kill_then_a_fault_in_the_resume(self) -> None:
        self.every_kill_then_a_fault_in_the_resume()


class ApplyResumeFaultTest(SetupFaultBase):
    """apply after a --no-restart run: the files done, the record owes both restarts."""

    start = APPLY_RESUME

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class ApplyResumeResumeFaultTest(SetupFaultBase):
    """apply after a --no-restart run: the files done, the record owes both restarts. A kill, then a fault in the run that resumes."""

    start = APPLY_RESUME

    def test_every_kill_then_a_fault_in_the_resume(self) -> None:
        self.every_kill_then_a_fault_in_the_resume()


class RevertAppliedFaultTest(SetupFaultBase):
    """revert of an applied machine."""

    start = REVERT_APPLIED

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class RevertAppliedResumeFaultTest(SetupFaultBase):
    """revert of an applied machine. A kill, then a fault in the run that resumes."""

    start = REVERT_APPLIED

    def test_every_kill_then_a_fault_in_the_resume(self) -> None:
        self.every_kill_then_a_fault_in_the_resume()
