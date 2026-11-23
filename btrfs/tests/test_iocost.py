# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs iocost` (`btrfs/iocost` and `btrfs/btrfs-iocost`)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import dataclasses
import io
import os
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn, override
import unittest
from unittest import mock

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
    IocostParsedArgs,
    format_iocost_plan,
    format_iocost_status,
    is_auto_eligible_disk,
    parse_iocost_args,
    parse_iocost_qos,
    plan_iocost_apply,
)
from cst_core.adapters.locks import take_lock
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget, program_installer
from cst_core.adapters.runtime import RootElevation, enforce_host_root, lc_ctype_handover
from cst_core.application.install import InstallService, Placement, staged
from cst_core.domain.install import INSTALL_MANIFEST, Component, select_entries
from cst_core.domain.record import ComponentState
from cst_core.ports import CommandResult, WriteFailed
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
    """systemctl, udevadm and installer.py calls, recorded; fail_on: a part of the command line (such as
    "installer.py mark" or "systemctl enable") whose call answers exit 1; on_call sees each command first."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.load_state = "not-found"
        self.active_state = "inactive"
        self.enabled_state = "disabled"
        self.fail_on: set[str] = set()
        self.on_call: Callable[[list[str]], None] | None = None
        # `udevadm info -q property -n /dev/NAME`: these properties, or by default a disk udev processed
        self.udev_props: dict[str, str] = {}
        # every `udevadm info` call answers (exit status, stderr) instead
        self.udev_failure: tuple[int, str] | None = None

    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if self.on_call is not None:
            self.on_call(cmd)
        line = " ".join(cmd)
        if any(part in line for part in self.fail_on):
            return CommandResult(1, "", f"{line}: failed", argv=tuple(cmd))
        if cmd[:2] == ["udevadm", "info"] and self.udev_failure is not None:
            return CommandResult(self.udev_failure[0], "", self.udev_failure[1], argv=tuple(cmd))
        if cmd[:2] == ["udevadm", "info"]:
            name = cmd[-1].removeprefix("/dev/")
            return CommandResult(0, self.udev_props.get(name, "USEC_INITIALIZED=1234\n"), "", argv=tuple(cmd))
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


class _RefusingRemove(InstallService):
    """An installer whose remove fails before it removes anything."""

    def __init__(self) -> None:
        super().__init__(LocalInstallTarget(), lambda _e: b"")

    @override
    def remove(self, places: Sequence[Placement]) -> Iterator[str]:
        raise WriteFailed(places[0].path, "Read-only file system")


def _steps(calls: list[list[str]]) -> list[str]:
    """The commands that change something, in order: "installer WORD" or the first two words."""
    return [
        f"installer {c[4]}" if len(c) > 4 and c[3].endswith("installer.py") else " ".join(c[:2])
        for c in calls
        if c[:2] not in (["systemctl", "show"], ["udevadm", "info"])
    ]


class BtrfsIocostDomainTest(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertEqual(parse_iocost_args([]).action, IocostAction.STATUS)
        self.assertEqual(parse_iocost_args(["--help"]).action, IocostAction.HELP)
        self.assertEqual(parse_iocost_args(["plan", "--live-only", "--device", "/dev/nvme0n1"]).device, "nvme0n1")
        parsed = parse_iocost_args(["apply", "--live-only", "--device=nvme1n1"])
        self.assertTrue(parsed.live_only)
        self.assertEqual(parsed.device, "nvme1n1")
        self.assertIsNotNone(parse_iocost_args(["status", "--live-only"]).error)
        self.assertIsNotNone(parse_iocost_args(["revert", "--device", "nvme0n1"]).error)
        self.assertIsNotNone(parse_iocost_args(["apply", "--device"]).error)
        self.assertIsNotNone(parse_iocost_args(["unknown"]).error)

    def test_device_needs_live_only(self) -> None:
        for argv in (["apply", "--device", "nvme0n1"], ["plan", "--device=nvme0n1"]):
            with self.subTest(argv=argv):
                parsed = parse_iocost_args(argv)
                self.assertEqual(parsed.code, 2)
                self.assertEqual(
                    parsed.error, "--device needs --live-only (it writes one disk now and installs nothing)"
                )
        self.assertEqual(
            parse_iocost_args(["plan", "--device", "nvme0n1", "--live-only"]),
            IocostParsedArgs(IocostAction.PLAN, live_only=True, device="nvme0n1"),
        )
        self.assertEqual(
            parse_iocost_args(["plan", "--live-only"]), IocostParsedArgs(IocostAction.PLAN, live_only=True)
        )

    def test_a_help_word_after_the_subcommand_asks_for_help(self) -> None:
        for argv in (["apply", "--help"], ["revert", "--live-only", "-h"], ["plan", "help"], ["status", "--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(parse_iocost_args(argv), IocostParsedArgs(IocostAction.HELP))

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
            udev_control=os.path.join(self.tmp, "run", "udev", "control"),
            lock_path=os.path.join(self.tmp, "run", "btrfs-setup", "iocost-managed.lock"),
            install_root=os.path.join(self.tmp, "root"),
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

    def _component_files(self) -> list[str]:
        return [
            staged(self.paths.install_root, e.dest)
            for e in select_entries(INSTALL_MANIFEST, component=Component.BTRFS_IOCOST)
        ]

    def _seed_component(self) -> None:
        """btrfs:iocost as an apply leaves it: its files in place, listed in the record, done."""
        svc = program_installer(os.path.join(REPO, "btrfs", "btrfs-iocost"))
        for e in select_entries(INSTALL_MANIFEST, component=Component.BTRFS_IOCOST):
            place = Placement.of(
                e, path=staged(self.paths.install_root, e.dest), dest=e.dest, record=self.paths.record_path
            )
            os.makedirs(os.path.dirname(place.path), exist_ok=True)
            svc.put(place, b"installed\n", as_root=False)
        svc.mark(self.paths.record_path, Component.BTRFS_IOCOST.value, ComponentState.DONE)

    def _recorded_components(self) -> dict[str, str]:
        rec = JsonInstallRecord().read(self.paths.record_path)
        assert rec is not None
        return {d: f.component for d, f in rec.files.items()}

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

    def test_plan_live_only_shows_no_component_install(self) -> None:
        snap = collect_iocost_snapshot(self.paths, runner=self.runner)
        plan = plan_iocost_apply(snap, target_device="nvme0n1")
        lines = format_iocost_plan(snap, plan, live_only=True)
        self.assertFalse(any("component btrfs:iocost" in line for line in lines), lines)
        self.assertIn("would write:   /sys/fs/cgroup/io.cost.qos <- '259:1 enable=1 ctrl=auto'", "\n".join(lines))
        self.assertTrue(any("would install: component btrfs:iocost" in line for line in format_iocost_plan(snap, plan)))

    def test_apply_live_only_and_full_revert_respect_vendor(self) -> None:
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.runner.calls,
            [
                ["udevadm", "info", "-q", "property", "-n", "/dev/nvme0n1"],
                ["udevadm", "info", "-q", "property", "-n", "/dev/nvme1n1"],
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
                ],
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

        self._seed_component()
        rc_rev, out_rev = revert_iocost(self.paths, live_only=False, runner=self.runner)
        self.assertEqual(rc_rev, 0)
        self.assertFalse(os.path.exists(self.paths.managed_file))
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "259:1 enable=0 ctrl=auto")
        self.assertTrue(any("non-CST enable=1 kept" in line for line in out_rev))
        self.assertEqual([p for p in self._component_files() if os.path.lexists(p)], [])
        self.assertEqual(self._recorded_components(), {})

    def test_unwritable_qos_fails_and_refusals(self) -> None:
        os.chmod(self.paths.qos_path, 0o444)
        self.addCleanup(os.chmod, self.paths.qos_path, 0o644)
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 1)
        self.assertIn(f"ERROR: {self.paths.qos_path} nvme0n1: Permission denied", out)
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

        # The command line root runs, as the bash front's "sudo -- env CST_PROG=... SELF ARGS" ran it: the
        # system Python on this entry, every argument passed on.
        with mock.patch.object(sys, "executable", "/home/u/.venv/bin/python3"), self.assertRaises(SystemExit):
            run_iocost_cli(
                ["apply", "--live-only", "--device", "nvme0n1"],
                deps=IocostCliDeps(
                    entry_file=os.path.join(REPO, "btrfs", "btrfs-iocost"),
                    env={"CST_PROG": "cst btrfs iocost"},
                    out=io.StringIO(),
                    err=io.StringIO(),
                    ctx=_FakeCtx(container=False, root=False),
                    elevate=fake_elevate,
                ),
            )
        self.assertEqual(
            seen[-1],
            [
                "sudo",
                "--",
                "env",
                "CST_PROG=cst btrfs iocost",
                lc_ctype_handover(),
                "/usr/bin/python3",
                "-I",
                "-B",
                os.path.realpath(os.path.join(REPO, "btrfs", "btrfs-iocost")),
                "apply",
                "--live-only",
                "--device",
                "nvme0n1",
            ],
        )

        mapped = iocost_paths_for(
            os.path.join(REPO, "btrfs", "btrfs-iocost"), ctx=_FakeCtx(container=True, root=False), read_only=True
        )
        self.assertEqual(mapped.qos_path, f"/run/host{IOCOST_QOS_PATH}")
        self.assertEqual(mapped.sys_block, "/run/host/sys/block")

    def _installer_words(self) -> list[str]:
        """The installer commands run, in order (install, mark, remove)."""
        return [s.removeprefix("installer ") for s in _steps(self.runner.calls) if s.startswith("installer ")]

    def _ran(self, *words: str) -> bool:
        return any(c[: len(words)] == list(words) for c in self.runner.calls)

    def test_apply_marks_done_last_after_the_writes_and_the_service(self) -> None:
        rc, out = apply_iocost(self.paths, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            _steps(self.runner.calls),
            ["installer install", "systemctl daemon-reload", "systemctl enable", "installer mark"],
        )
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertIn("259:1 enable=1 ctrl=auto", fh.read())
        self.assertIn("note:      udevd not running; rules are read when it starts", out)

    def test_apply_failures_leave_the_component_pending(self) -> None:
        for fail in ("systemctl daemon-reload", "systemctl enable", "udevadm control", "installer.py install"):
            with self.subTest(fail=fail):
                self.runner = _FakeRunner()
                self.runner.fail_on = {fail}
                self._write(self.paths.udev_control, "")
                rc, out = apply_iocost(self.paths, runner=self.runner)
                self.assertNotEqual(rc, 0)
                self.assertNotIn("mark", self._installer_words())
                if fail != "installer.py install":
                    self.assertTrue(any(line.startswith("ERROR:") and fail in line for line in out), out)

    def test_a_failed_qos_write_stops_apply_before_the_service(self) -> None:
        os.chmod(self.paths.qos_path, 0o444)
        self.addCleanup(os.chmod, self.paths.qos_path, 0o644)
        rc, out = apply_iocost(self.paths, runner=self.runner)
        self.assertEqual(rc, 1)
        self.assertIn(f"ERROR: {self.paths.qos_path} nvme0n1: Permission denied", out)
        self.assertEqual(self._installer_words(), ["install"])
        self.assertFalse(self._ran("systemctl", "enable"))
        self.assertFalse(os.path.exists(self.paths.managed_file))

    def test_revert_removes_nothing_when_disable_fails(self) -> None:
        self.runner.load_state = "loaded"
        self.runner.fail_on = {"systemctl disable"}
        self._write(self.paths.managed_file, "259:1\n")
        self._write(self.paths.qos_path, "259:1 enable=1 ctrl=auto\n")
        rc, out = revert_iocost(self.paths, runner=self.runner)
        self.assertNotEqual(rc, 0)
        self.assertIn("nothing was removed", out)
        self.assertEqual(self._installer_words(), [])
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1 enable=1 ctrl=auto\n")

    def test_revert_resets_the_disks_after_the_rule_is_gone(self) -> None:
        self.runner.load_state = "loaded"
        self._write(self.paths.udev_control, "")
        self._write(self.paths.managed_file, "259:1\n")
        self._write(self.paths.qos_path, "259:1 enable=1 ctrl=auto\n")
        qos_at_reload: list[str] = []

        def seen(cmd: list[str]) -> None:
            if cmd[:2] == ["udevadm", "control"]:
                with open(self.paths.qos_path, encoding="utf-8") as fh:
                    qos_at_reload.append(fh.read())

        self.runner.on_call = seen
        rc, out = revert_iocost(self.paths, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertEqual(qos_at_reload, ["259:1 enable=1 ctrl=auto\n"])
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1 enable=0 ctrl=auto\n")
        self.assertEqual(
            _steps(self.runner.calls),
            ["systemctl disable", "systemctl daemon-reload", "udevadm control"],
        )
        self.assertFalse(os.path.exists(self.paths.managed_file))

    def test_revert_failures_after_the_removal_are_errors(self) -> None:
        for fail in ("systemctl daemon-reload", "udevadm control", "remove"):
            with self.subTest(fail=fail):
                self.runner = _FakeRunner()
                self.runner.fail_on = {fail}
                self._write(self.paths.udev_control, "")
                self._write(self.paths.managed_file, "259:1\n")
                refusing = _RefusingRemove() if fail == "remove" else None
                rc, out = revert_iocost(self.paths, runner=self.runner, installer=refusing)
                self.assertNotEqual(rc, 0)
                self.assertTrue(os.path.exists(self.paths.managed_file))
                if refusing is not None:
                    self.assertIn("Read-only file system", "\n".join(out))
                    self.assertFalse(self._ran("systemctl", "daemon-reload"))

    def test_the_scan_leaves_disks_with_a_hwdb_solution_to_udev(self) -> None:
        self._write(self.paths.qos_path, "")
        self.runner.udev_props["nvme0n1"] = "IOCOST_SOLUTIONS=isolation\nUSEC_INITIALIZED=1234\n"
        self.runner.udev_props["nvme1n1"] = "DEVTYPE=disk\n"  # udev has not processed it yet
        status = "\n".join(format_iocost_status(collect_iocost_snapshot(self.paths, runner=self.runner)))
        self.assertIn("(hwdb solution isolation)", status)
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")
        self.assertIn("kept:      259:1 (nvme0n1: hwdb solution isolation; left to udev)", out)
        self.assertIn("note:      nvme1n1: not processed by udev yet; the udev rule writes it when udev does", out)
        self.assertFalse(os.path.exists(self.paths.managed_file))

    def test_a_device_run_writes_without_asking_udev(self) -> None:
        # The udev rule's run: the rule matched IOCOST_SOLUTIONS=="" itself, and during a RUN the udev
        # database still holds the previous event's properties.
        self._write(self.paths.qos_path, "")
        self.runner.udev_props["nvme0n1"] = "IOCOST_SOLUTIONS=isolation\n"
        rc, out = apply_iocost(self.paths, live_only=True, target_device="nvme0n1", runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self._ran("udevadm", "info"))
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1 enable=1 ctrl=auto\n")

    def test_without_udevadm_the_scan_claims_the_disk_as_before(self) -> None:
        self._write(self.paths.qos_path, "")
        self.runner.fail_on = {"udevadm info -q property -n /dev/nvme0n1"}
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"set:       {self.paths.qos_path} <- '259:1 enable=1 ctrl=auto' (nvme0n1)", out)
        self.assertTrue(any(line.startswith("note:      nvme0n1: udevadm info failed (") for line in out), out)

    def test_the_udevadm_note_names_its_first_line_stripped(self) -> None:
        self.runner.udev_failure = (4, "\n  Unknown device \t\nsecond line\n")
        snap = collect_iocost_snapshot(self.paths, runner=self.runner)
        self.assertEqual(
            {d.name: d.udev_error for d in snap.disks if d.name.startswith("nvme")},
            {"nvme0n1": "Unknown device", "nvme1n1": "Unknown device"},
        )
        self.runner.udev_failure = (4, " \n")
        snap = collect_iocost_snapshot(self.paths, runner=self.runner)
        self.assertEqual({d.udev_error for d in snap.disks if d.name.startswith("nvme")}, {"exit 4"})

    def test_a_disk_udev_configures_is_no_longer_cst_s(self) -> None:
        # An earlier run claimed it before 90-iocost.rules ran: revert must not reset the vendor's setting.
        self._write(self.paths.qos_path, "259:1 enable=1 ctrl=user\n")
        self._write(self.paths.managed_file, "259:1\n")
        self.runner.udev_props["nvme0n1"] = "IOCOST_SOLUTIONS=isolation\nUSEC_INITIALIZED=1234\n"
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        with open(self.paths.managed_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:2\n")  # nvme1n1 only: the scan claimed it, it has no solution

    def test_cst_s_own_write_on_a_disk_with_a_hwdb_solution_stays_cst_s(self) -> None:
        # An earlier run this boot wrote enable=1 ctrl=auto, and udev never replaced it: the scan keeps the
        # disk CST's, so revert still resets it.
        self._write(self.paths.qos_path, "259:1 enable=1 ctrl=auto\n")
        self._write(self.paths.managed_file, "259:1\n")
        self.runner.udev_props["nvme0n1"] = "IOCOST_SOLUTIONS=isolation\nUSEC_INITIALIZED=1234\n"
        self.runner.udev_props["nvme1n1"] = "DEVTYPE=disk\n"  # not processed yet: left out of this test
        rc, out = apply_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertIn(
            "unchanged: 259:1 (nvme0n1: CST's enable=1 auto from earlier this boot, kept until revert or reboot;"
            " hwdb solution isolation)",
            out,
        )
        with open(self.paths.managed_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1\n")
        rc_r, out_r = revert_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc_r, 0, out_r)
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1 enable=0 ctrl=auto\n")
        self.assertFalse(os.path.exists(self.paths.managed_file))

    def test_the_service_runs_after_udev_settle(self) -> None:
        with open(os.path.join(REPO, "btrfs", "units", "cst-btrfs-iocost.service"), encoding="utf-8") as fh:
            after = [line.split("=", 1)[1].split() for line in fh if line.startswith("After=")]
        self.assertEqual(after, [["systemd-udev-settle.service", "systemd-udevd.service", "local-fs.target"]])

    def _hold_lock(self) -> int:
        os.makedirs(os.path.dirname(self.paths.lock_path), exist_ok=True)
        fd = take_lock(self.paths.lock_path, wait=False, mode=0o644)
        assert fd is not None
        return fd

    def test_a_run_waits_for_the_lock_and_gives_up_with_nothing_written(self) -> None:
        paths = dataclasses.replace(self.paths, lock_timeout=0.1)
        self._write(self.paths.managed_file, "259:1\n")
        fd = self._hold_lock()
        try:
            for name, run in (
                ("apply --live-only", lambda: apply_iocost(paths, live_only=True, runner=self.runner)),
                ("apply", lambda: apply_iocost(paths, runner=self.runner)),
                ("revert --live-only", lambda: revert_iocost(paths, live_only=True, runner=self.runner)),
            ):
                with self.subTest(run=name):
                    rc, out = run()
                    self.assertEqual(rc, 1)
                    self.assertEqual(out, [f"ERROR: another btrfs-iocost run holds {self.paths.lock_path}"])
                    with open(self.paths.qos_path, encoding="utf-8") as fh:
                        self.assertEqual(fh.read(), "259:2 enable=1 ctrl=user rpct=95.00\n")
                    with open(self.paths.managed_file, encoding="utf-8") as fh:
                        self.assertEqual(fh.read(), "259:1\n")
            self.assertEqual(self._installer_words(), [])
        finally:
            os.close(fd)
        rc, out = apply_iocost(paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1 enable=1 ctrl=auto\n")
        self.assertEqual(os.stat(self.paths.lock_path).st_mode & 0o777, 0o644)

    def test_the_lock_is_free_when_enable_starts_the_service(self) -> None:
        free_at_enable: list[bool] = []

        def try_lock(cmd: list[str]) -> None:
            if cmd[:2] == ["systemctl", "enable"]:
                fd = take_lock(self.paths.lock_path, wait=False, mode=0o644)
                free_at_enable.append(fd is not None)
                if fd is not None:
                    os.close(fd)

        self.runner.on_call = try_lock
        rc, out = apply_iocost(self.paths, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertEqual(free_at_enable, [True])

    def test_the_installed_zipapp_reverts_without_an_installer(self) -> None:
        # /usr/local/sbin/btrfs-iocost is a zipapp: no checkout, so no cst_core/installer.py beside it.
        # revert removes btrfs:iocost in this process; apply refuses at once (it installs from a checkout).
        zipapp = os.path.join(self.tmp, "usr", "local", "sbin", "btrfs-iocost")
        self._write(zipapp, "PK\x03\x04")
        installed = iocost_paths_for(zipapp, ctx=_FakeCtx(container=False, root=True), read_only=False)
        self.assertEqual(installed.installer_script, "")
        paths = dataclasses.replace(self.paths, installer_script="", entry_file=zipapp)
        self._seed_component()
        self.runner.load_state = "loaded"
        self._write(self.paths.managed_file, "259:1\n")
        self._write(self.paths.qos_path, "259:1 enable=1 ctrl=auto\n")

        rc, out = revert_iocost(paths, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertEqual([p for p in self._component_files() if os.path.lexists(p)], [])
        self.assertEqual(self._recorded_components(), {})
        self.assertFalse(any(c[0].endswith("python3") for c in self.runner.calls), self.runner.calls)
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1 enable=0 ctrl=auto\n")

        self.runner.calls.clear()
        rc_a, out_a = apply_iocost(paths, runner=self.runner)
        self.assertEqual(rc_a, 1)
        self.assertEqual(
            out_a,
            ["ERROR: apply installs btrfs:iocost from a CST checkout: run it from one (cst btrfs iocost apply)"],
        )
        self.assertEqual(self.runner.calls, [])

    def test_revert_without_io_cost_qos_forgets_the_managed_disks(self) -> None:
        os.unlink(self.paths.qos_path)
        self._write(self.paths.managed_file, "259:1\n")
        rc, out = revert_iocost(self.paths, live_only=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.paths.managed_file))

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
