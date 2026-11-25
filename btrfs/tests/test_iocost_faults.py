# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs iocost apply` (tests/fault_injection.py), over IocostWorld.

apply (btrfs/iocost/application.py) installs btrfs:iocost and marks it done through the installer,
writes io.cost.qos and the managed-devices file, and reloads systemd and udev and enables the service.
It runs here over IocostWorld (btrfs/tests/fakes.py), the in-memory fakes of its ports, where every
mutating call is a point of a FaultPlan: the installer's install and mark, the managed-devices file's
saves, each io.cost.qos write, daemon-reload, enable --now and the udev rules' reload (not the reads,
the locks, udevadm info, systemctl show or the installer's plan). The world keeps the state those calls
leave: the component's files and its record state, what systemd and udev have loaded, the service, the
io.cost.qos lines and the managed set; and it checks that each call holds the locks the protocol must
hold. btrfs/tests/test_iocost_ports.py keeps the fakes equal to the real adapters.

For every mode at every point, and every FAIL followed by a KILL at each later point of that run,
checked:
- the component is marked done only when everything it stands for is in place: the files
  installed, systemd reloaded after that, the service enabled, the udev rules reloaded (udevd
  runs here) and the io.cost.qos line written;
- a run that returns 0 left the component done;
- apply again, with no fault, returns 0 and leaves it done;
- no exception gets out of apply: a failed save of the managed-devices file is exit 1 with an
  `ERROR: cannot write PATH: REASON` line (the component stays pending);
- the io.cost.qos line apply wrote stays CST's: after apply again, its device is in the
  managed-devices set (apply saves the devices it is about to enable before it writes their
  lines), so revert resets it. Before, a save that failed or a kill after the write left the line
  enable=1 and unlisted, and the next apply kept it as a vendor's.

A guard for a7c5e92 (iocost marks the component done last): on its parent these cases fail with
"done before what it stands for".
"""

from __future__ import annotations

import errno
import os
from typing import override
import unittest

from btrfs.tests.fakes import IocostWorld
from tests.fault_injection import Fault, FaultMode, Killed, cut_points, every_fault

QOS_LINE = "259:1 enable=1 ctrl=auto"  # what apply writes for the fixture's NVMe disk
VENDOR_LINE = "259:2 enable=1 ctrl=user rpct=95.00"
# The mutating calls of one apply from a fresh machine, and the cases of each mode family.
POINTS = 8
CASES_FAIL_THEN_KILL = (
    1  # a failed step ends apply (it rolls nothing back): one later call, the save after the qos write
)


class IocostApplyFaultTest(unittest.TestCase):
    def start(self) -> None:
        """A fresh machine: one NVMe disk, io.cost files empty, udevd running, nothing installed."""
        self.world = IocostWorld()
        self.world.put_disk("nvme0n1", "259:1", model="Samsung SSD 990 PRO 2TB")

    def run_once(self, *faults: Fault) -> tuple[int, list[str]] | None:
        """One apply with these faults; None when it was killed."""
        self.world.plan.arm(*faults)
        try:
            return self.world.service().apply()
        except Killed:
            return None

    def assert_done_means_applied(self, when: str) -> None:
        w = self.world
        unit = w.file(w.unit_path)
        if w.state() == "done":
            self.assertEqual(len(w.in_place()), 3, f"{when}: done, files not installed")
            self.assertTrue(
                unit is not None and w.systemd.loaded == unit, f"{when}: done, systemd not reloaded after the install"
            )
            self.assertFalse(w.systemd.dirty, f"{when}: done, systemd not reloaded after the install")
            self.assertTrue(w.systemd.enabled, f"{when}: done, service not enabled")
            self.assertEqual(w.udev_loaded, w.file(w.rule_path), f"{when}: done, udev rules not reloaded")
            self.assertEqual((w.qos or {}).get("259:1"), QOS_LINE, f"{when}: done, io.cost.qos not written")

    def failed_at(self, name: str) -> bool:
        """Whether the last run's call named so was a failure the plan answered (FAIL, FAIL_FROM)."""
        calls = self.world.plan.calls
        n = calls.index(name) + 1 if name in calls else 0
        return n > 0 and self.world.plan.fault_at(n) in {FaultMode.FAIL, FaultMode.FAIL_FROM}

    def check(self, *faults: Fault) -> None:
        when = f"{faults}"
        self.start()
        result = self.run_once(*faults)
        self.assert_done_means_applied(when)
        if result is not None and self.failed_at("managed save"):
            self.assertEqual(result[0], 1, f"{when}: the managed file's save failed: {result[1]}")
            self.assertIn(f"ERROR: cannot write {self.world.managed_path}: No space left on device", result[1])
            self.assertNotEqual(self.world.state(), "done", f"{when}: the managed file's save failed, done")
        if result is not None and result[0] == 0:
            self.assertEqual(self.world.state(), "done", f"{when}: exit 0, not done: {result[1]}")
        rc, lines = self.run_once() or (None, [])
        self.assertEqual(rc, 0, f"{when}: apply again: {lines}")
        self.assertEqual(self.world.state(), "done", f"{when}: apply again, not done")
        self.assert_done_means_applied(f"{when}: apply again")
        self.assertIn("259:1", self.world.managed, f"{when}: apply again: 259:1 is not CST's")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one apply from a fresh machine with these faults."""
        self.start()
        if not faults:
            return cut_points(self.world.plan, self.run_once)
        self.run_once(*faults)
        return list(self.world.plan.calls)

    def every(self, mode: FaultMode) -> int:
        calls = self.points()
        self.assert_done_means_applied("no fault")
        self.assertEqual(len(calls), POINTS, calls)
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                self.check(fault)
        return len(calls)

    def test_the_points_of_one_apply(self) -> None:
        self.assertEqual(
            self.points(),
            [
                "installer install",
                "managed save",
                f"qos {QOS_LINE}",
                "managed save",
                "systemd daemon-reload",
                "systemd enable --now",
                "udev reload-rules",
                "installer mark done",
            ],
        )

    def test_every_kill(self) -> None:
        self.assertEqual(self.every(FaultMode.KILL), POINTS)

    def test_every_failure(self) -> None:
        self.assertEqual(self.every(FaultMode.FAIL), POINTS)

    def test_every_full_disk(self) -> None:
        self.assertEqual(self.every(FaultMode.FAIL_FROM), POINTS)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        cases = 0
        for fail in every_fault(POINTS, (FaultMode.FAIL,)):
            for kill in every_fault(len(self.points(fail)), (FaultMode.KILL,), after=fail[1]):
                with self.subTest(fault=fail, then=kill):
                    cases += 1
                    self.check(fail, kill)
        self.assertEqual(cases, CASES_FAIL_THEN_KILL)


class IocostManagedSaveFailureTest(unittest.TestCase):
    """A failed save of the managed-devices file is an ERROR line (moved from test_iocost.py, where it
    patched the adapters' write_atomic and ensure_dir and their save of the managed-devices file): on the
    lifecycle tests' machine, an NVMe disk to enable beside a vendor's line, an HDD and a loop device, no
    udevd."""

    @override
    def setUp(self) -> None:
        w = IocostWorld()
        w.udevd = False
        w.qos = {"259:2": VENDOR_LINE}
        w.put_disk("nvme0n1", "259:1", model="Samsung SSD 990 PRO 2TB")
        w.put_disk("nvme1n1", "259:2", model="Vendor Hwdb SSD")
        w.put_disk("sda", "8:0", rotational=True, scheduler="bfq", model="Spinning HDD")
        w.put_disk("loop0", "7:0")
        self.world = w

    def installer_words(self) -> list[str]:
        return [c.removeprefix("installer ") for c in self.world.calls if c.startswith("installer ")]

    def test_apply_whose_save_of_the_managed_file_fails_is_an_error_line(self) -> None:
        for err, reason in (
            (OSError(errno.ENOSPC, "No space left on device"), "No space left on device"),
            (OSError(errno.EROFS, os.strerror(errno.EROFS)), "Read-only file system"),
        ):
            with self.subTest(reason):
                self.setUp()
                w = self.world
                w.fail("managed save", err)
                qos_before = w.qos_text()
                rc, out = w.service().apply()
                self.assertEqual(rc, 1, out)
                # The save of the devices it is about to enable comes first: no line is written without it.
                self.assertEqual(
                    out[-2:],
                    [f"ERROR: cannot write {w.managed_path}: {reason}", "nothing was written to io.cost.qos"],
                )
                self.assertNotIn("set:       ", "\n".join(out))
                self.assertEqual(w.qos_text(), qos_before)
                self.assertEqual(self.installer_words(), ["install"])  # not marked done: still pending
                self.assertNotIn("systemd enable --now", w.calls)

    def test_a_save_that_fails_after_the_qos_write_keeps_the_line_cst_s(self) -> None:
        # The save after the writes fails (the one before them worked): the line apply wrote stays CST's,
        # so the next apply keeps it as its own and revert resets it.
        w = self.world
        w.plan.arm((FaultMode.FAIL, 4))  # install, save, the qos write, then the second save
        rc, out = w.service().apply()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out[-1], f"ERROR: cannot write {w.managed_path}: No space left on device")
        self.assertIn("set:       ", "\n".join(out))
        self.assertEqual(w.plan.calls, ["installer install", "managed save", f"qos {QOS_LINE}", "managed save"])
        self.assertEqual(w.managed, {"259:1"})
        w.plan.arm()
        rc, out = w.service().apply()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("259:1 (nvme0n1: pre-existing/vendor", "\n".join(out))
        self.assertIn("unchanged: 259:1 (nvme0n1: already enable=1 auto)", out)
        rc, out = w.service().revert()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"reverted:  {w.qos_path} <- '259:1 enable=0 ctrl=auto' (nvme0n1)", out)
        self.assertIn("kept:      259:2 (nvme1n1: non-CST enable=1 kept)", out)
        self.assertEqual(w.managed, set())


class IocostWorldLocksTest(unittest.TestCase):
    """The fakes check the locks: a use case that drops one is caught here, not only by the front tests
    over real lock files."""

    def test_a_qos_write_without_the_qos_lock_is_caught(self) -> None:
        w = IocostWorld()
        with self.assertRaisesRegex(AssertionError, "without the qos lock"):
            w.service().qos.write(QOS_LINE)
        with self.assertRaisesRegex(AssertionError, "without the qos lock"):
            w.service().managed.save(frozenset({"259:1"}))

    def test_enable_needs_the_component_lock_and_the_qos_lock_free(self) -> None:
        w = IocostWorld()
        with self.assertRaisesRegex(AssertionError, "without the component lock"):
            w.service().systemd.enable_now()
        w.held.update(component=True, qos=True)
        with self.assertRaisesRegex(AssertionError, "io.cost.qos lock the service takes"):
            w.service().systemd.enable_now()

    def test_a_busy_lock_and_one_that_cannot_be_made(self) -> None:
        w = IocostWorld()
        w.put_disk("nvme0n1", "259:1")
        w.busy.add("qos")
        self.assertEqual(
            w.service().apply(live_only=True), (1, [f"ERROR: another btrfs-iocost run holds {w.lock_paths['qos']}"])
        )
        w.busy.clear()
        w.lock_errors["component"] = FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
        self.assertEqual(w.service().apply(), (1, [f"ERROR: cannot take {w.lock_paths['component']}: File exists"]))
        self.assertEqual(w.calls, [])


if __name__ == "__main__":
    unittest.main()
