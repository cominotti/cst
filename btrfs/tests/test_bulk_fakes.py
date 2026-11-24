# SPDX-License-Identifier: MPL-2.0
"""The helpers of btrfs/tests/bulk_fakes.py that the fault and MBT tests of btrfs-bulk build on.

A fault in these helpers hides a fault of the code under test (an error swallowed, a call that is
no point, a +C the fake forgets) or leaves scratch folders behind, so each gets a test here.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from btrfs.tests import bulk_fakes
from btrfs.tests.bulk_fakes import BulkHome, run_armed
from tests.fault_injection import FaultMode, FaultPlan


def fail() -> None:
    raise RuntimeError("planted failure")


def nothing() -> None:
    """A mutating call's effect, when only the trace matters."""


class BulkHomeCleanupTest(unittest.TestCase):
    """A BulkHome removes its temporary folder whatever fails while it is built or closed."""

    def scratch(self) -> str:
        """A folder of this test's own as tempfile's default folder: what BulkHome leaves is seen."""
        folder = tempfile.mkdtemp(prefix="bulk-fakes-test-")
        self.addCleanup(shutil.rmtree, folder)
        self.enterContext(mock.patch.object(tempfile, "tempdir", folder))
        return folder

    def test_a_home_that_fails_to_build_leaves_no_folder(self) -> None:
        folder = self.scratch()
        with (
            mock.patch.object(bulk_fakes, "Wiring", side_effect=RuntimeError("planted")),
            self.assertRaises(RuntimeError),
        ):
            BulkHome()
        self.assertEqual(os.listdir(folder), [])

    def test_close_removes_the_folder_when_undoing_a_patch_fails(self) -> None:
        folder = self.scratch()
        fx = BulkHome()
        fx.stack.callback(fail)
        with self.assertRaises(RuntimeError):
            fx.close()
        self.assertEqual(os.listdir(folder), [])


class RunArmedTest(unittest.TestCase):
    """run_armed ends a run at a kill or at the failure a fault gave, and lets any other error through."""

    def crash_after(self, plan: FaultPlan, points: int) -> None:
        """points mutating calls, then an OSError the plan did not give (the code crashing)."""
        for n in range(points):
            plan.step(f"call {n + 1}", nothing)
        raise FileExistsError("planted crash")

    def test_an_os_error_with_no_fault_armed_is_raised(self) -> None:
        plan = FaultPlan()
        with self.assertRaises(FileExistsError):
            run_armed(plan, lambda: self.crash_after(plan, 1))
        self.assertEqual(plan.calls, [], "the plan is disarmed after")

    def test_an_os_error_before_the_armed_failure_fired_is_raised(self) -> None:
        for mode in (FaultMode.FAIL, FaultMode.FAIL_FROM):
            plan = FaultPlan()
            with self.subTest(mode=mode.value), self.assertRaises(FileExistsError):
                run_armed(plan, lambda plan=plan: self.crash_after(plan, 2), (mode, 3))

    def test_an_os_error_after_a_kill_fired_is_raised(self) -> None:
        plan = FaultPlan()  # alive after the kill: the code caught it (a BaseException) and went on

        def catch_the_kill_then_crash() -> None:
            with contextlib.suppress(BaseException):
                plan.step("call 1", nothing)
            raise FileExistsError("planted crash")

        with self.assertRaises(FileExistsError):
            run_armed(plan, catch_the_kill_then_crash, (FaultMode.KILL, 1))

    def test_the_failure_a_fault_gave_ends_the_run(self) -> None:
        for mode in (FaultMode.FAIL, FaultMode.FAIL_FROM):
            plan = FaultPlan()
            with self.subTest(mode=mode.value):
                calls = run_armed(plan, lambda plan=plan: self.crash_after(plan, 3), (mode, 2))
                self.assertEqual(calls, ["call 1", "call 2"])

    def test_a_kill_ends_the_run(self) -> None:
        plan = FaultPlan()
        calls = run_armed(plan, lambda: self.crash_after(plan, 3), (FaultMode.KILL, 2))
        self.assertEqual(calls, ["call 1", "call 2"])


if __name__ == "__main__":
    unittest.main()
