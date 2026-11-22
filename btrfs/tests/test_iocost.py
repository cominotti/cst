# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs iocost` (`btrfs/iocost` and `btrfs/btrfs-iocost`)."""

from __future__ import annotations

from collections.abc import Sequence
import io
import os
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn, override
import unittest

from btrfs.iocost.adapters import (
    IocostPaths,
    apply_iocost,
    collect_iocost_snapshot,
    iocost_paths_for,
    revert_iocost,
)
from btrfs.iocost.cli import IocostCliDeps, run_iocost_cli
from btrfs.iocost.domain import (
    IOCOST_QOS_PATH,
    BlockDisk,
    IocostAction,
    format_iocost_plan,
    format_iocost_status,
    is_auto_eligible_disk,
    parse_iocost_args,
    parse_iocost_qos,
    plan_iocost_apply,
)
from cst_core.adapters.runtime import RootElevation, enforce_host_root
from cst_core.ports import CommandResult
from tests.repo_files import REPO


class _FakeCtx:
    def __init__(self, *, container: bool = False, root: bool = True, host_root: str = "/run/host") -> None:
        self._container = container
        self._root = root
        self._host_root = host_root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def path(self, host_path: str) -> str:
        return (
            f"{self._host_root}/{host_path.lstrip('/')}" if self._container and host_path.startswith("/") else host_path
        )

    def etc_dir(self) -> str:
        return f"{self._host_root}/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        base = f"{self._host_root}/run" if self._container else "/run"
        return os.path.join(base, subpath.strip("/")) if subpath.strip("/") else base


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.load_state = "not-found"
        self.active_state = "inactive"
        self.enabled_state = "disabled"

    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            return CommandResult(
                0,
                f"LoadState={self.load_state}\nActiveState={self.active_state}\nUnitFileState={self.enabled_state}\n",
                "",
                argv=tuple(cmd),
            )
        if cmd[:3] == ["systemctl", "enable", "--now"]:
            self.load_state, self.active_state, self.enabled_state = "loaded", "active", "enabled"
        elif cmd[:3] == ["systemctl", "disable", "--now"]:
            self.active_state, self.enabled_state = "inactive", "disabled"
        return CommandResult(0, "", "", argv=tuple(cmd))


class BtrfsIocostDomainTest(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertEqual(parse_iocost_args([]).action, IocostAction.STATUS)
        self.assertEqual(parse_iocost_args(["--help"]).action, IocostAction.HELP)
        self.assertEqual(parse_iocost_args(["plan", "--device", "/dev/nvme0n1"]).device, "nvme0n1")
        parsed = parse_iocost_args(["apply", "--live-only", "--device=nvme1n1"])
        self.assertTrue(parsed.live_only)
        self.assertEqual(parsed.device, "nvme1n1")
        self.assertIsNotNone(parse_iocost_args(["status", "--live-only"]).error)
        self.assertIsNotNone(parse_iocost_args(["revert", "--device", "nvme0n1"]).error)
        self.assertIsNotNone(parse_iocost_args(["apply", "--device"]).error)
        self.assertIsNotNone(parse_iocost_args(["unknown"]).error)

    def test_parse_qos_and_eligible_disks(self) -> None:
        entries = parse_iocost_qos("259:1 enable=1 ctrl=auto rpct=95.00\n259:2 enable=0 ctrl=user\n")
        self.assertEqual((entries["259:1"].enable, entries["259:2"].enable), (1, 0))
        self.assertTrue(is_auto_eligible_disk(BlockDisk("nvme0n1", "259:1", rotational=False, scheduler="none")))
        self.assertTrue(is_auto_eligible_disk(BlockDisk("sda", "8:0", rotational=False, scheduler="mq-deadline")))
        self.assertFalse(is_auto_eligible_disk(BlockDisk("sdb", "8:16", rotational=True, scheduler="bfq")))
        self.assertFalse(is_auto_eligible_disk(BlockDisk("loop0", "7:0", rotational=False, scheduler="none")))
        self.assertFalse(is_auto_eligible_disk(BlockDisk("dm-0", "253:0", rotational=False, scheduler="none")))


class BtrfsIocostLifecycleTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-btrfs-iocost-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.runner = _FakeRunner()
        self.paths = IocostPaths(
            qos_path=os.path.join(self.tmp, "sys", "fs", "cgroup", "io.cost.qos"),
            model_path=os.path.join(self.tmp, "sys", "fs", "cgroup", "io.cost.model"),
            controllers_path=os.path.join(self.tmp, "sys", "fs", "cgroup", "cgroup.controllers"),
            sys_block=os.path.join(self.tmp, "sys", "block"),
            managed_file=os.path.join(self.tmp, "run", "btrfs-setup", "iocost-managed"),
            rule_dest=os.path.join(self.tmp, "etc", "udev", "rules.d", "91-cst-iocost.rules"),
            installer_script="/opt/fake/cst_core/installer.py",
        )
        self._write(self.paths.controllers_path, "cpuset cpu io memory pids\n")
        self._write(self.paths.qos_path, "259:2 enable=1 ctrl=user rpct=95.00\n")
        self._write(self.paths.model_path, "")
        self._add_disk("nvme0n1", "259:1", rot="0", sched="[none]", model="Samsung SSD 990 PRO 2TB")
        self._add_disk("nvme1n1", "259:2", rot="0", sched="[none]", model="Vendor Hwdb SSD")
        self._add_disk("sda", "8:0", rot="1", sched="[bfq]", model="Spinning HDD")
        self._add_disk("loop0", "7:0", rot="0", sched="none")

    def _write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _add_disk(self, name: str, devno: str, *, rot: str, sched: str, model: str = "") -> None:
        base = os.path.join(self.paths.sys_block, name)
        self._write(os.path.join(base, "dev"), devno + "\n")
        self._write(os.path.join(base, "queue", "rotational"), rot + "\n")
        self._write(os.path.join(base, "queue", "scheduler"), sched + "\n")
        if model:
            self._write(os.path.join(base, "device", "model"), model + "\n")

    def test_status_plan_and_preexisting_preserved(self) -> None:
        snap = collect_iocost_snapshot(self.paths, runner=self.runner)
        status_lines = format_iocost_status(snap)
        self.assertTrue(any("1 active device(s)" in line for line in status_lines))
        self.assertTrue(any("nvme1n1" in line and "vendor/manual" in line for line in status_lines))

        plan = plan_iocost_apply(snap)
        self.assertEqual([w.devno for w in plan.writes], ["259:1"])
        self.assertEqual(plan.writes[0].line, "259:1 enable=1 ctrl=auto")
        self.assertEqual(plan.next_managed, frozenset({"259:1"}))
        self.assertTrue(any("pre-existing:" in line for line in format_iocost_plan(snap, plan)))

    def test_apply_live_only_and_full_revert_respect_vendor(self) -> None:
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.runner.calls,
            [
                [
                    "systemctl",
                    "show",
                    "-p",
                    "LoadState",
                    "-p",
                    "ActiveState",
                    "-p",
                    "UnitFileState",
                    "cst-btrfs-iocost.service",
                ]
            ],
        )
        self.assertTrue(any("259:1 enable=1 ctrl=auto" in line for line in out))
        self.assertTrue(any("pre-existing/vendor" in line for line in out))
        with open(self.paths.managed_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "259:1")

        self._write(self.paths.qos_path, "259:1 enable=1 ctrl=auto\n259:2 enable=1 ctrl=user\n")
        self.runner.calls.clear()
        rc_full, out_full = apply_iocost(self.paths, live_only=False, runner=self.runner)
        self.assertEqual(rc_full, 0)
        self.assertTrue(any("install" in c and "btrfs:iocost" in c for c in self.runner.calls))
        self.assertTrue(
            any(c == ["systemctl", "enable", "--now", "cst-btrfs-iocost.service"] for c in self.runner.calls)
        )
        self.assertTrue(any("unchanged: 259:1" in line for line in out_full))

        rc_rev, out_rev = revert_iocost(self.paths, live_only=False, runner=self.runner)
        self.assertEqual(rc_rev, 0)
        self.assertFalse(os.path.exists(self.paths.managed_file))
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "259:1 enable=0 ctrl=auto")
        self.assertTrue(any("non-CST enable=1 kept" in line for line in out_rev))
        self.assertTrue(any("remove" in c and "btrfs:iocost" in c for c in self.runner.calls))

    def test_unwritable_qos_graceful_skip_and_refusals(self) -> None:
        os.chmod(self.paths.qos_path, 0o444)
        self.addCleanup(os.chmod, self.paths.qos_path, 0o644)
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0)
        self.assertTrue(any("not writable" in line for line in out))
        self.assertFalse(os.path.exists(self.paths.managed_file))

        out_s, err_s = io.StringIO(), io.StringIO()
        rc_c = run_iocost_cli(
            ["apply"],
            deps=IocostCliDeps(
                entry_file=os.path.join(REPO, "btrfs", "btrfs-iocost"),
                out=out_s,
                err=err_s,
                ctx=_FakeCtx(container=True, root=False),
            ),
        )
        self.assertEqual(rc_c, 1)
        self.assertRegex((out_s.getvalue() + err_s.getvalue()).lower(), "container")

        seen: list[list[str]] = []

        def fake_elevate(argv: list[str]) -> NoReturn:
            seen.append(argv)
            raise SystemExit(0)

        with self.assertRaises(SystemExit):
            enforce_host_root(
                RootElevation(
                    "cst btrfs iocost", "apply", ("apply",), os.path.join(REPO, "btrfs", "btrfs-iocost"), "writes"
                ),
                ctx=_FakeCtx(container=False, root=False),
                elevate=fake_elevate,
            )
        self.assertEqual(seen[0][0], "sudo")

        mapped = iocost_paths_for(
            os.path.join(REPO, "btrfs", "btrfs-iocost"), ctx=_FakeCtx(container=True, root=False), read_only=True
        )
        self.assertEqual(mapped.qos_path, f"/run/host{IOCOST_QOS_PATH}")
        self.assertEqual(mapped.sys_block, "/run/host/sys/block")

    def test_entrypoint_help_direct_subprocess(self) -> None:
        p = subprocess.run(
            [sys.executable, "-I", "-B", os.path.join(REPO, "btrfs", "btrfs-iocost"), "--help"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("io.cost.qos", p.stdout)
