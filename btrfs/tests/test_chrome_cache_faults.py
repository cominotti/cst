# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `chrome-cache apply` and `off` (tests/fault_injection.py).

apply and off (btrfs/chrome_cache/adapters.py) run on the real file system, over a temporary HOME, with
the adapters' os module replaced by FaultyOs: every os.mkdir, os.unlink, os.open (the reads' too),
os.fchmod, os.fsync and os.rename they make is a point of a FaultPlan that is dead after a kill
(write_flags cleans up in an `except BaseException`, which a real kill never runs).

Start states (STARTS): apply with no config folder, with the folder and no file, over a file of the user's
own flags (mode 0600), with the cap line gone under the marker, and with a stale .chrome-flags.conf.cst-new
of a killed run; off of the managed flags alone and among the user's. For each, every mode at every point,
and every FAIL followed by a KILL at each later point of that run (the KILL lands in the cleanup's unlink),
checked:
- after a failure, the flags file holds its start text and mode and no .cst-new is left;
- after a kill, the file holds its start text or the new text, never a part, and a .cst-new is left only by
  a run that died before its rename;
- the same command again, with no fault, converges (apply: the flags managed; off: nothing more to turn
  off), removes a stale .cst-new and keeps the mode; a third run changes nothing and makes no mkdir,
  unlink, fchmod or rename call.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import ClassVar, override
import unittest
from unittest import mock

from btrfs.chrome_cache import adapters
from btrfs.chrome_cache.domain import (
    APP,
    FLAG_DIR,
    FLAG_SIZE,
    FLAGS_REL,
    OFF_LINE,
    ON_LINE,
    USER_INSTALL_REL,
    ChromeFlagsState,
    decide,
    turn_off,
)
from tests.fault_injection import (
    Fault,
    FaultMode,
    FaultPlan,
    FaultyOs,
    Killed,
    cut_points,
    every_fault,
    stub_fsync,
)
from tests.repo_files import REPO

POINTS = ("mkdir", "unlink", "open", "fchmod", "fsync", "rename")
CHANGING = ("os.mkdir", "os.unlink", "os.fchmod", "os.rename")
TMP_NAME = f".{os.path.basename(FLAGS_REL)}.cst-new"
BLOCK = f"{ON_LINE}\n{FLAG_DIR}\n{FLAG_SIZE}\n"
USERS_OWN = "--enable-features=VaapiVideoDecoder\n"


class Command(StrEnum):
    APPLY = "apply"
    OFF = "off"


@dataclass(frozen=True)
class Start:
    """A state before the run: the command, whether the config folder is there, the flags file's text and
    mode (None: no file), whether a stale .cst-new is there, and the pinned numbers of points and of FAIL
    then KILL cases."""

    name: str
    command: Command
    folder: bool
    text: str | None
    mode: int
    stale: bool
    points: int
    fail_then_kill: int


STARTS: tuple[Start, ...] = (
    Start("A1", Command.APPLY, folder=False, text=None, mode=0o644, stale=False, points=15, fail_then_kill=3),
    Start("A2", Command.APPLY, folder=True, text=None, mode=0o644, stale=False, points=10, fail_then_kill=3),
    Start("A3", Command.APPLY, folder=True, text=USERS_OWN, mode=0o600, stale=False, points=11, fail_then_kill=3),
    Start(
        "A4",
        Command.APPLY,
        folder=True,
        text=f"{ON_LINE}\n{FLAG_DIR}\n",
        mode=0o644,
        stale=False,
        points=11,
        fail_then_kill=3,
    ),
    Start("A5", Command.APPLY, folder=True, text=None, mode=0o644, stale=True, points=10, fail_then_kill=3),
    Start("O1", Command.OFF, folder=True, text=BLOCK, mode=0o644, stale=False, points=11, fail_then_kill=3),
    Start("O2", Command.OFF, folder=True, text=USERS_OWN + BLOCK, mode=0o600, stale=False, points=11, fail_then_kill=3),
)
BY_NAME = {s.name: s for s in STARTS}


def target_text(s: Start) -> str:
    """The flags file's text after a run that worked."""
    new = decide(s.text or "")[1] if s.command == Command.APPLY else turn_off(s.text or "")
    assert new is not None
    return new


def converged(s: Start, text: str) -> bool:
    if s.command == Command.APPLY:
        return decide(text)[0] == ChromeFlagsState.MANAGED
    return turn_off(text) is None


class ChromeFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.tmp = tempfile.mkdtemp(prefix="cst-chrome-faults-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plan = FaultPlan(dead_after_kill=True)
        self.enterContext(mock.patch.object(adapters, "os", FaultyOs(self.plan, POINTS)))
        self.n = 0

    @staticmethod
    def open_fds() -> set[str]:
        """This process's open descriptors, without the one the listing itself holds open while it reads
        (its link names the folder it lists)."""
        folder = f"/proc/{os.getpid()}/fd"
        fds: set[str] = set()
        with os.scandir(folder) as entries:
            for entry in entries:
                with contextlib.suppress(FileNotFoundError):
                    if os.readlink(entry.path) != folder:
                        fds.add(entry.name)
        return fds

    # ---- the fixture ------------------------------------------------------
    def fresh(self) -> str:
        """A new HOME in the start state; its path."""
        self.n += 1
        home = os.path.join(self.tmp, f"home{self.n}")
        os.makedirs(os.path.join(home, USER_INSTALL_REL, "app", APP, "current", "active"))
        cfg = os.path.join(home, os.path.dirname(FLAGS_REL))
        if self.start.folder:
            os.makedirs(cfg)
        if self.start.text is not None:
            path = os.path.join(home, FLAGS_REL)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.start.text)
            os.chmod(path, self.start.mode)
        if self.start.stale:
            with open(os.path.join(cfg, TMP_NAME), "w", encoding="utf-8") as fh:
                fh.write("half")
        return home

    def run_once(self, home: str, *faults: Fault) -> tuple[bool, bool] | None:
        """One run with these faults: (it raised OSError, it changed the file); None when killed."""
        before = self.open_fds()
        self.plan.arm(*faults)
        try:
            changed = adapters.apply(home)[2] if self.start.command == Command.APPLY else adapters.off(home)[1]
        except Killed:
            # A kill right after an os.open loses the descriptor it gave (as a dead process would): closed here.
            for fd in self.open_fds() - before:
                with contextlib.suppress(OSError):
                    os.close(int(fd))
            return None
        except OSError:
            result = (True, False)
        else:
            result = (False, changed)
        self.assertEqual(self.open_fds() - before, set(), f"{self.start.name} {faults}: a descriptor left open")
        return result

    # ---- what the state is --------------------------------------------------
    def file(self, home: str) -> tuple[str | None, int | None]:
        path = os.path.join(home, FLAGS_REL)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None, None
        with open(path, encoding="utf-8") as fh:
            return fh.read(), stat.S_IMODE(st.st_mode)

    def stale_left(self, home: str) -> bool:
        return os.path.lexists(os.path.join(home, os.path.dirname(FLAGS_REL), TMP_NAME))

    # ---- the checks ---------------------------------------------------------
    def check(self, *faults: Fault) -> None:
        when = f"{self.start.name} {faults}"
        home = self.fresh()
        start_text, start_mode = self.file(home)
        new_mode = start_mode if start_mode is not None else 0o644
        result = self.run_once(home, *faults)
        calls = list(self.plan.calls)
        text, mode = self.file(home)
        if result is None:
            self.assertIn(text, (start_text, target_text(self.start)), f"{when}: killed: the file")
            if self.stale_left(home) and not self.start.stale:
                self.assertNotIn("os.rename", calls, f"{when}: a .cst-new left after the rename")
        else:
            raised, changed = result
            failed = any(m != FaultMode.KILL for m, _k in faults)
            if failed and (raised or not changed):
                self.assertEqual((text, mode), (start_text, start_mode), f"{when}: failed: the file")
                # A .cst-new may stay only when the cleanup's unlink failed too (a full disk: FAIL_FROM).
                first = faults[0][1]
                cleanup_failed = any(
                    c == "os.unlink" and n > first and self.plan.fault_at(n) is not None for n, c in enumerate(calls, 1)
                )
                if not self.start.stale and not cleanup_failed:
                    self.assertFalse(self.stale_left(home), f"{when}: failed: a .cst-new left")
            else:
                self.assertEqual(text, target_text(self.start), when)
                self.assertEqual(mode, new_mode, when)
        again = self.run_once(home)
        self.assertEqual((again or (True, False))[0], False, f"{when}: the run again: {again}")
        text, mode = self.file(home)
        self.assertIsNotNone(text, when)
        assert text is not None
        self.assertTrue(converged(self.start, text), f"{when}: the run again: {text!r}")
        self.assertEqual(text, target_text(self.start), when)
        self.assertEqual(mode, new_mode, f"{when}: the mode")
        self.assertFalse(self.stale_left(home), f"{when}: a .cst-new after the run again")
        third = self.run_once(home)
        self.assertEqual(third, (False, False), f"{when}: the third run")
        self.assertEqual([c for c in self.plan.calls if c in CHANGING], [], f"{when}: the third run")

    def check_nothing_to_do(self) -> None:
        home = self.fresh()
        self.assertEqual(self.run_once(home), (False, False))
        self.assertEqual([c for c in self.plan.calls if c in CHANGING], [])
        self.assertEqual(self.file(home), (self.start.text, self.start.mode))

    def points(self, *faults: Fault) -> list[str]:
        home = self.fresh()
        if not faults:
            return cut_points(self.plan, lambda: self.run_once(home))
        self.run_once(home, *faults)
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
            for then in every_fault(len(self.points(fail)), (FaultMode.KILL,), after=fail[1]):
                with self.subTest(fault=fail, then=then):
                    cases += 1
                    self.check(fail, then)
        self.assertEqual(cases, self.start.fail_then_kill)


class A1ChromeFaultTest(ChromeFaultBase):
    """apply with no config folder."""

    start = BY_NAME["A1"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class A2ChromeFaultTest(ChromeFaultBase):
    """apply with the config folder and no flags file."""

    start = BY_NAME["A2"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class A3ChromeFaultTest(ChromeFaultBase):
    """apply over a file of the user's own flags, mode 0600."""

    start = BY_NAME["A3"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class A4ChromeFaultTest(ChromeFaultBase):
    """apply with the cap line gone under the marker."""

    start = BY_NAME["A4"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class A5ChromeFaultTest(ChromeFaultBase):
    """apply with a stale .chrome-flags.conf.cst-new of a killed run."""

    start = BY_NAME["A5"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class O1ChromeFaultTest(ChromeFaultBase):
    """off of the managed flags alone."""

    start = BY_NAME["O1"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class O2ChromeFaultTest(ChromeFaultBase):
    """off of the managed flags among the user's, mode 0600."""

    start = BY_NAME["O2"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class AlreadyManagedChromeTest(ChromeFaultBase):
    """N: apply over the managed flags: no write, no mkdir, unlink, fchmod or rename."""

    start = Start("N", Command.APPLY, folder=True, text=BLOCK, mode=0o644, stale=False, points=0, fail_then_kill=0)

    def test_nothing_to_do_makes_no_changing_call(self) -> None:
        self.check_nothing_to_do()


class AlreadyOffChromeTest(ChromeFaultBase):
    """N: off over the off marker: no write, no mkdir, unlink, fchmod or rename."""

    start = Start(
        "N", Command.OFF, folder=True, text=f"{OFF_LINE}\n", mode=0o644, stale=False, points=0, fail_then_kill=0
    )

    def test_nothing_to_do_makes_no_changing_call(self) -> None:
        self.check_nothing_to_do()


# The matrix classes of this module run in one child process; it prints its open descriptors before and after.
_WHOLE_MODULE_CHILD = """
import io, json, os, sys, unittest
sys.path.insert(0, sys.argv[1])
from btrfs.tests import test_chrome_cache_faults as m
cases = [c for c in vars(m).values() if isinstance(c, type) and issubclass(c, m.ChromeFaultBase)]
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in cases)
before = sorted(os.listdir("/proc/self/fd"), key=int)
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
after = sorted(os.listdir("/proc/self/fd"), key=int)
print(json.dumps({"tests": result.testsRun, "ok": result.wasSuccessful(), "before": before, "after": after}))
"""


class NoDescriptorLeftTest(unittest.TestCase):
    """The whole matrix, run in one process, leaves no descriptor open: the ones a killed run lost are
    closed, and every other run must give its own back (run_unit.py's ResourceWarning gate does not see a
    raw os.open descriptor)."""

    def test_the_matrix_in_one_process_closes_every_descriptor_it_opened(self) -> None:
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _WHOLE_MODULE_CHILD, REPO],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=300,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        self.assertTrue(seen["ok"], done.stderr)
        self.assertGreater(seen["tests"], 0)
        self.assertEqual(seen["after"], seen["before"], "descriptors left open by the matrix")
