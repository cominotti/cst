# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs iocost apply` (tests/fault_injection.py).

apply installs btrfs:iocost and marks it done through the installer, writes io.cost.qos and the
managed-devices file itself, and reloads systemd and udev and enables the service through
commands. Each mutating call goes through a FaultPlan: the runner's commands (FaultyRunner; not
systemctl show, udevadm info or the installer's plan, which only read) and the module's two file
writers. IocostMachine, the runner under it, keeps the state those commands leave: the
component's files and its record state, whether systemd was reloaded after the install, the
service, and the udev rules. os.fsync is stubbed (durability is not what these tests check).

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
  managed-devices file (apply saves the devices it is about to enable before it writes their
  lines), so revert resets it. Before, a save that failed or a kill after the write left the line
  enable=1 and unlisted, and the next apply kept it as a vendor's.

A guard for a7c5e92 (iocost marks the component done last): on its parent these cases fail with
"done before what it stands for".
"""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import functools
import os
import shutil
import tempfile
from typing import override
import unittest
from unittest import mock

from btrfs.iocost import adapters as iocost_adapters
from btrfs.iocost.adapters import IocostPaths, apply_iocost
from cst_core.ports import CommandResult
from tests.fault_injection import (
    Fault,
    FaultMode,
    FaultPlan,
    FaultyRunner,
    Killed,
    cut_points,
    every_fault,
    stub_fsync,
)

QOS_LINE = "259:1 enable=1 ctrl=auto"  # what apply writes for the fixture's NVMe disk
READS = (["systemctl", "show"], ["udevadm", "info"])
# The mutating calls of one apply from a fresh machine, and the cases of each mode family.
POINTS = 8
CASES_FAIL_THEN_KILL = 1  # a failed step ends apply (it rolls nothing back): one later call, the qos write


def _installer_command(argv: Sequence[str]) -> str | None:
    """The installer's subcommand (python -I -B installer.py SUBCOMMAND ...), or None."""
    return argv[4] if len(argv) > 4 and argv[3].endswith("installer.py") else None


def reads(argv: Sequence[str]) -> bool:
    """systemctl show, udevadm info and the installer's plan only read."""
    return list(argv[:2]) in READS or _installer_command(argv) == "plan"


def read_or_empty(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def point_name(argv: Sequence[str]) -> str:
    sub = _installer_command(argv)
    return f"installer {sub}" if sub is not None else " ".join(argv[:3])


@dataclasses.dataclass
class IocostMachine:
    """systemctl, udevadm and installer.py, and what they have changed."""

    installed: bool = False
    done: bool = False
    reloaded_after_install: bool = False
    enabled: bool = False
    udev_reloaded: bool = False

    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        cmd = list(argv)
        sub = _installer_command(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            active, enabled = ("active", "enabled") if self.enabled else ("inactive", "disabled")
            load = "loaded" if self.installed else "not-found"
            text = f"LoadState={load}\nActiveState={active}\nUnitFileState={enabled}\n"
            return CommandResult(0, text, "", argv=tuple(cmd))
        if cmd[:2] == ["udevadm", "info"]:
            return CommandResult(0, "USEC_INITIALIZED=1234\n", "", argv=tuple(cmd))
        if sub == "install":
            self.installed, self.done, self.reloaded_after_install, self.udev_reloaded = True, False, False, False
        elif sub == "mark":
            self.done = True
        elif cmd[:2] == ["systemctl", "daemon-reload"]:
            self.reloaded_after_install = self.installed
        elif cmd[:3] == ["systemctl", "enable", "--now"]:
            self.enabled = self.installed
        elif cmd[:3] == ["udevadm", "control", "--reload-rules"]:
            self.udev_reloaded = self.installed
        return CommandResult(0, "", "", argv=tuple(cmd))  # the installer's plan: nothing to say


class IocostApplyFaultTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-iocost-faults-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        sys_fs = os.path.join(self.tmp, "sys", "fs", "cgroup")
        self.paths = IocostPaths(
            qos_path=os.path.join(sys_fs, "io.cost.qos"),
            model_path=os.path.join(sys_fs, "io.cost.model"),
            controllers_path=os.path.join(sys_fs, "cgroup.controllers"),
            sys_block=os.path.join(self.tmp, "sys", "block"),
            managed_file=os.path.join(self.tmp, "run", "btrfs-setup", "iocost-managed"),
            rule_dest=os.path.join(self.tmp, "etc", "udev", "rules.d", "91-cst-iocost.rules"),
            installer_script="/opt/fake/cst_core/installer.py",
            udev_control=os.path.join(self.tmp, "run", "udev", "control"),
            lock_path=os.path.join(self.tmp, "run", "btrfs-setup", "iocost-managed.lock"),
            install_root=os.path.join(self.tmp, "root"),
        )
        self.plan = FaultPlan()
        self.machine = IocostMachine()

    def write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def start(self) -> None:
        """A fresh machine: one NVMe disk, io.cost files empty, udevd running, nothing installed."""
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        self.write(self.paths.controllers_path, "cpuset cpu io memory pids\n")
        self.write(self.paths.qos_path, "")
        self.write(self.paths.model_path, "")
        self.write(self.paths.udev_control, "")
        base = os.path.join(self.paths.sys_block, "nvme0n1")
        for rel, text in (("dev", "259:1"), ("queue/rotational", "0"), ("queue/scheduler", "[none]")):
            self.write(os.path.join(base, rel), text + "\n")
        self.write(os.path.join(base, "device", "model"), "Samsung SSD 990 PRO 2TB\n")
        self.machine = IocostMachine()

    def qos_written(self) -> bool:
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            return QOS_LINE in fh.read()

    def run_once(self, *faults: Fault) -> tuple[int, list[str]] | None:
        """One apply with these faults; None when it was killed."""
        self.plan.arm(*faults)
        real_qos, real_managed = iocost_adapters._write_qos_line, iocost_adapters._save_managed_devnos

        def write_qos(path: str, payload: str) -> OSError | None:
            return self.plan.step(f"qos {payload}", functools.partial(real_qos, path, payload), lambda e: e)

        def save_managed(path: str, devnos: frozenset[str]) -> None:
            self.plan.step("managed file", functools.partial(real_managed, path, devnos))

        runner = FaultyRunner(self.plan, self.machine, reads=reads, name=point_name)
        with (
            mock.patch.object(iocost_adapters, "_write_qos_line", write_qos),
            mock.patch.object(iocost_adapters, "_save_managed_devnos", save_managed),
        ):
            try:
                return apply_iocost(self.paths, runner=runner)
            except Killed:
                return None

    def assert_done_means_applied(self, when: str) -> None:
        m = self.machine
        if m.done:
            self.assertTrue(m.installed, f"{when}: done, files not installed")
            self.assertTrue(m.reloaded_after_install, f"{when}: done, systemd not reloaded after the install")
            self.assertTrue(m.enabled, f"{when}: done, service not enabled")
            self.assertTrue(m.udev_reloaded, f"{when}: done, udev rules not reloaded")
            self.assertTrue(self.qos_written(), f"{when}: done, io.cost.qos not written")

    def failed_at(self, name: str) -> bool:
        """Whether the last run's call named so was a failure the plan answered (FAIL, FAIL_FROM)."""
        n = self.plan.calls.index(name) + 1 if name in self.plan.calls else 0
        return n > 0 and self.plan.fault_at(n) in {FaultMode.FAIL, FaultMode.FAIL_FROM}

    def check(self, *faults: Fault) -> None:
        when = f"{faults}"
        self.start()
        result = self.run_once(*faults)
        self.assert_done_means_applied(when)
        if result is not None and self.failed_at("managed file"):
            self.assertEqual(result[0], 1, f"{when}: the managed file's save failed: {result[1]}")
            self.assertIn(f"ERROR: cannot write {self.paths.managed_file}: No space left on device", result[1])
            self.assertFalse(self.machine.done, f"{when}: the managed file's save failed, done")
        if result is not None and result[0] == 0:
            self.assertTrue(self.machine.done, f"{when}: exit 0, not done: {result[1]}")
        rc, lines = self.run_once() or (None, [])
        self.assertEqual(rc, 0, f"{when}: apply again: {lines}")
        self.assertTrue(self.machine.done, f"{when}: apply again, not done")
        self.assert_done_means_applied(f"{when}: apply again")
        self.assertIn("259:1\n", read_or_empty(self.paths.managed_file), f"{when}: apply again: 259:1 is not CST's")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one apply from a fresh machine with these faults."""
        self.start()
        if not faults:
            return cut_points(self.plan, self.run_once)
        self.run_once(*faults)
        return list(self.plan.calls)

    def every(self, mode: FaultMode) -> int:
        calls = self.points()
        self.assert_done_means_applied("no fault")
        self.assertEqual(len(calls), POINTS, calls)
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                self.check(fault)
        return len(calls)

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


if __name__ == "__main__":
    unittest.main()
