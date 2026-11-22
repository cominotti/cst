# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst cpu irq` (`cpu/irq` and `cpu/cpu-irq`)."""

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

from cst_core.adapters.runtime import RootElevation, enforce_host_root
from cst_core.domain.values import CpuSet
from cst_core.ports import CommandResult
from cpu.irq.adapters import IrqPaths, apply_irq_policy, collect_irq_snapshot, irq_paths_for, revert_irq_policy
from cpu.irq.cli import IrqCliDeps, run_irq_cli
from cpu.irq.domain import (
    IrqAction,
    compute_banned_cpulist,
    format_cpulist,
    format_irq_plan,
    format_irq_status,
    parse_irq_args,
    render_irq_dropin,
    render_irq_env,
)
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
        self.install_rpm_succeeds = True

    def _run_systemctl(self, cmd: list[str]) -> CommandResult:
        if cmd[:2] == ["systemctl", "show"]:
            out = f"LoadState={self.load_state}\nActiveState={self.active_state}\nUnitFileState={self.enabled_state}\n"
            return CommandResult(0, out, "", argv=tuple(cmd))
        if cmd == ["systemctl", "enable", "irqbalance.service"]:
            self.enabled_state = "enabled"
        elif cmd == ["systemctl", "restart", "irqbalance.service"]:
            self.active_state = "active"
        return CommandResult(0, "", "", argv=tuple(cmd))

    def _run_installer_fake(self, cmd: list[str], input_text: str | None) -> CommandResult:
        if "text" in cmd:
            dest = cmd[cmd.index("text") + 1]
            prev: str | None = None
            if os.path.exists(dest):
                with open(dest, encoding="utf-8") as fh:
                    prev = fh.read()
            if prev == input_text:
                return CommandResult(0, f"unchanged: {dest}\n", "", argv=tuple(cmd))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(input_text or "")
            return CommandResult(0, f"written:   {dest}\n", "", argv=tuple(cmd))
        if "remove-text" in cmd:
            lines: list[str] = []
            for dest in cmd[cmd.index("remove-text") + 1 :]:
                if os.path.exists(dest):
                    os.remove(dest)
                    lines.append(f"removed:   {dest}")
            return CommandResult(0, ("\n".join(lines) + "\n") if lines else "", "", argv=tuple(cmd))
        return CommandResult(0, "", "", argv=tuple(cmd))

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        **_kw: object,
    ) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd and cmd[0] == "systemctl":
            return self._run_systemctl(cmd)
        if cmd[:2] == ["rpm-ostree", "install"]:
            if not self.install_rpm_succeeds:
                return CommandResult(1, "", "error: network unreachable", argv=tuple(cmd))
            self.load_state, self.active_state, self.enabled_state = "loaded", "inactive", "disabled"
            return CommandResult(0, "Checking out tree... done\n", "", argv=tuple(cmd))
        if "installer.py" in " ".join(cmd):
            return self._run_installer_fake(cmd, input_text)
        return CommandResult(0, "", "", argv=tuple(cmd))


class CpuIrqDomainTest(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertEqual(parse_irq_args([]).action, IrqAction.STATUS)
        self.assertEqual(parse_irq_args(["status"]).action, IrqAction.STATUS)
        self.assertEqual(parse_irq_args(["--help"]).action, IrqAction.HELP)
        self.assertTrue(parse_irq_args(["plan", "--no-pkg"]).no_pkg)
        self.assertTrue(parse_irq_args(["apply", "--no-pkg"]).no_pkg)
        self.assertIsNotNone(parse_irq_args(["status", "--no-pkg"]).error)
        self.assertIsNotNone(parse_irq_args(["revert", "--no-pkg"]).error)
        self.assertIsNotNone(parse_irq_args(["bogus"]).error)

    def test_compute_banned_cpulist_topology_cases(self) -> None:
        self.assertEqual(format_cpulist([0, 1, 2, 5, 8, 9, 10]), "0-2,5,8-10")
        banned, notes = compute_banned_cpulist(CpuSet.parse("12-23"), CpuSet.parse("0-23"))
        self.assertEqual((banned, notes), ("12-23", ()))
        am5_ban, _ = compute_banned_cpulist(CpuSet.parse("12-23"), CpuSet.parse("0-15"))
        self.assertEqual(am5_ban, "12-15")
        trimmed, trim_notes = compute_banned_cpulist(CpuSet.parse("0-15"), CpuSet.parse("0-15"))
        self.assertEqual(trimmed, "2-15")
        self.assertTrue(any(">= 2 unbanned" in n for n in trim_notes))
        tiny, _ = compute_banned_cpulist(CpuSet.parse("0-1"), CpuSet.parse("0-1"))
        self.assertEqual(tiny, "")
        none_overlap, _ = compute_banned_cpulist(CpuSet.parse("12-23"), CpuSet.parse("0-3"))
        self.assertEqual(none_overlap, "")


class CpuIrqLifecycleTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-irq-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.runner = _FakeRunner()
        self.paths = IrqPaths(
            conf=os.path.join(self.tmp, "etc", "cpu-setup", "cpu-setup.conf"),
            online_cpus=os.path.join(self.tmp, "sys", "devices", "system", "cpu", "online"),
            env_path=os.path.join(self.tmp, "etc", "cpu-setup", "irqbalance.env"),
            sysconfig_path=os.path.join(self.tmp, "etc", "sysconfig", "irqbalance"),
            dropin_path=os.path.join(self.tmp, "etc", "systemd", "system", "irqbalance.service.d", "10-cst-ban.conf"),
            installer_script="/opt/fake/cst_core/installer.py",
        )
        self._write(self.paths.conf, "BUILD_CPUS='12-23'\n")
        self._write(self.paths.online_cpus, "0-23\n")

    def _write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_status_and_plan_change_nothing(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = run_irq_cli(
            ["plan"],
            deps=IrqCliDeps(
                entry_file=os.path.join(REPO, "cpu", "cpu-irq"),
                out=out,
                err=err,
                ctx=_FakeCtx(container=True, root=False),
                runner=self.runner,
            ),
        )
        snap = collect_irq_snapshot(self.paths, runner=self.runner, has_rpm_ostree=True)
        plan_lines = format_irq_plan(snap)
        status_lines = format_irq_status(snap)
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertTrue(any("would install: irqbalance" in line for line in plan_lines))
        self.assertTrue(any("IRQBALANCE_BANNED_CPULIST=12-23" in line for line in plan_lines))
        self.assertTrue(any("unit not installed" in line for line in status_lines))
        self.assertFalse(os.path.exists(self.paths.env_path))
        self.assertFalse(os.path.exists(self.paths.dropin_path))

    def test_container_refusal_and_non_root_elevation(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = run_irq_cli(
            ["apply"],
            deps=IrqCliDeps(
                entry_file=os.path.join(REPO, "cpu", "cpu-irq"),
                out=out,
                err=err,
                ctx=_FakeCtx(container=True, root=False),
                runner=self.runner,
            ),
        )
        self.assertEqual(rc, 1)
        self.assertRegex((out.getvalue() + err.getvalue()).lower(), "container")

        seen: list[list[str]] = []

        def fake_elevate(argv: list[str]) -> NoReturn:
            seen.append(argv)
            raise SystemExit(0)

        with self.assertRaises(SystemExit):
            enforce_host_root(
                RootElevation("cst cpu irq", "apply", ("apply",), os.path.join(REPO, "cpu", "cpu-irq"), "writes"),
                ctx=_FakeCtx(container=False, root=False),
                elevate=fake_elevate,
            )
        self.assertEqual(seen[0][0], "sudo")

    def test_apply_no_pkg_and_rpm_ostree_and_revert(self) -> None:
        rc_nopkg, out_nopkg = apply_irq_policy(self.paths, no_pkg=True, runner=self.runner, has_rpm_ostree=False)
        self.assertEqual(rc_nopkg, 0)
        self.assertTrue(any("--no-pkg" in line for line in out_nopkg))
        self.assertEqual(self._read(self.paths.env_path), render_irq_env("12-23"))
        self.assertEqual(self._read(self.paths.dropin_path), render_irq_dropin())

        rc_apply, out_apply = apply_irq_policy(self.paths, no_pkg=False, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc_apply, 0)
        self.assertIn(["rpm-ostree", "install", "--idempotent", "--apply-live", "irqbalance"], self.runner.calls)
        self.assertTrue(any("restarted: irqbalance.service" in line for line in out_apply))

        self.runner.calls.clear()
        rc_again, out_again = apply_irq_policy(self.paths, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc_again, 0)
        self.assertTrue(any("unchanged: irqbalance.service enabled and active" in line for line in out_again))
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in self.runner.calls))

        rc_rev, out_rev = revert_irq_policy(self.paths, runner=self.runner)
        self.assertEqual(rc_rev, 0)
        self.assertFalse(os.path.exists(self.paths.env_path))
        self.assertFalse(os.path.exists(self.paths.dropin_path))
        self.assertFalse(os.path.exists(os.path.dirname(self.paths.dropin_path)))
        self.assertTrue(any("restarted: irqbalance.service (CST ban-list removed)" in line for line in out_rev))

    def test_no_overlap_removes_stale_files_and_container_paths(self) -> None:
        self._write(self.paths.env_path, render_irq_env("12-23"))
        self._write(self.paths.dropin_path, render_irq_dropin())
        self._write(self.paths.online_cpus, "0-3\n")
        rc, out = apply_irq_policy(self.paths, no_pkg=True, runner=self.runner, has_rpm_ostree=False)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.paths.env_path))
        self.assertFalse(os.path.exists(self.paths.dropin_path))
        self.assertTrue(any("no online CPUs overlap" in line for line in out))

        mapped = irq_paths_for(
            os.path.join(REPO, "cpu", "cpu-irq"), ctx=_FakeCtx(container=True, root=False), read_only=True
        )
        self.assertTrue(mapped.conf.startswith("/run/host/etc/"))

    def test_entrypoint_help_direct_subprocess(self) -> None:
        p = subprocess.run(
            [sys.executable, "-I", "-B", os.path.join(REPO, "cpu", "cpu-irq"), "--help"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("IRQBALANCE_BANNED_CPULIST", p.stdout)

    def test_cpu_setup_routing(self) -> None:
        from cpu.setup.domain import CpuRoute, route
        from cpu.setup.wiring import Handed, watch_argv

        self.assertEqual(route("irq"), CpuRoute.WATCH)
        self.assertEqual(route("irq", ("status",)), CpuRoute.WATCH)
        self.assertEqual(route("irq", ("plan", "--no-pkg")), CpuRoute.WATCH)
        self.assertEqual(route("irq", ("apply",)), CpuRoute.ROOT)
        self.assertEqual(route("irq", ("revert",)), CpuRoute.ROOT)
        handed = Handed(
            script=os.path.join(REPO, "cpu-setup.sh"),
            arg0="cpu-setup.sh",
            cst_prog="cst cpu",
            sudo_user="",
            quiet={},
        )
        argv = watch_argv(handed, "irq", ("plan", "--no-pkg"), in_container=True)
        self.assertEqual(argv[-3:], [os.path.join(REPO, "cpu", "cpu-irq"), "plan", "--no-pkg"])
