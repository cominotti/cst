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
from typing import override
import unittest
from unittest import mock

from btrfs.tests import bulk_fakes
from btrfs.tests.bulk_fakes import BulkHome, arm_points, run_armed
from tests.fault_injection import FaultMode, FaultPlan, Killed, stub_fsync


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


class CopyTreeNocowTest(unittest.TestCase):
    """The fake copy gives a NOCOW copy's files +C (as btrfs does: inherited from their folder)."""

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.fx = BulkHome()
        self.addCleanup(self.fx.close)
        self.src = os.path.join(self.fx.tmp, "src")
        os.makedirs(os.path.join(self.src, "sub"))
        for rel in ("disk.img", "sub/d2"):
            with open(os.path.join(self.src, rel), "w") as f:
                f.write(rel)
        self.dst = self.fx.bulk_path("emulators/android/avd.bulk-new")
        os.makedirs(self.dst)

    def copied(self) -> list[str]:
        return sorted(
            os.path.relpath(os.path.join(root, n), self.dst)
            for root, dirs, files in os.walk(self.dst)
            for n in dirs + files
        )

    def test_a_copy_has_c(self) -> None:
        self.fx.machine.copy_tree(self.src, self.dst, True)
        self.assertEqual(self.copied(), ["disk.img", "sub", "sub/d2"])
        self.assertEqual([p for p in self.copied() if not self.fx.is_nocow(os.path.join(self.dst, p))], [])

    def test_a_copy_killed_after_cp_has_c(self) -> None:
        plan = FaultPlan(dead_after_kill=True)
        arm_points(self.fx, plan, torn_rmtree=False)
        plan.arm((FaultMode.KILL, 1))
        with self.assertRaises(Killed):
            self.fx.machine.copy_tree(self.src, self.dst, True)
        self.assertEqual(plan.calls, ["cp Bulk/emulators/android/avd.bulk-new"])
        self.assertEqual(self.copied(), ["disk.img", "sub", "sub/d2"])
        self.assertEqual([p for p in self.copied() if not self.fx.is_nocow(os.path.join(self.dst, p))], [])

    def test_a_copy_on_write_copy_has_no_c(self) -> None:
        self.fx.machine.copy_tree(self.src, self.dst, False)
        self.assertEqual([p for p in self.copied() if self.fx.is_nocow(os.path.join(self.dst, p))], [])


class RmtreePointTest(unittest.TestCase):
    """One deletion by u_rmtree is one point, whatever it deletes."""

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.fx = BulkHome()
        self.addCleanup(self.fx.close)
        self.plan = FaultPlan(dead_after_kill=True)
        arm_points(self.fx, self.plan, torn_rmtree=False)

    def test_a_file_a_link_and_a_folder_are_one_point_each(self) -> None:
        folder = os.path.dirname(self.fx.write("tree/a"))
        self.fx.write("left")
        os.symlink(folder, self.fx.home_path("link"))
        for rel in ("left", "link", "tree"):
            path = self.fx.home_path(rel)
            with self.subTest(rel=rel):
                self.plan.arm()
                self.fx.user.u_rmtree(path)
                self.assertEqual(self.plan.calls, [f"rmtree {rel}"])
                self.assertFalse(os.path.lexists(path))

    def test_a_failure_at_a_files_deletion_leaves_the_file(self) -> None:
        path = self.fx.write("left")
        self.plan.arm((FaultMode.FAIL, 1))
        with self.assertRaises(OSError):
            self.fx.user.u_rmtree(path)
        self.assertEqual(self.plan.calls, ["rmtree left"])
        self.assertTrue(os.path.lexists(path))

    def test_no_point_inside_a_files_deletion(self) -> None:
        path = self.fx.write("left")
        self.plan.arm((FaultMode.FAIL, 2))  # the call after the deletion, never reached here
        self.fx.user.u_rmtree(path)
        self.assertEqual(self.plan.calls, ["rmtree left"])
        self.assertFalse(os.path.lexists(path))


if __name__ == "__main__":
    unittest.main()
