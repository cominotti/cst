# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst cpu irq` (`cpu/irq` and `cpu/cpu-irq`)."""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import io
import os
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn, override
import unittest

from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.runtime import RootElevation, enforce_host_root
from cst_core.domain.install import Component
from cst_core.domain.record import ComponentState
from cst_core.domain.values import CpuSet
from cst_core.ports import CommandResult
from cpu.irq.adapters import IrqPaths, apply_irq_policy, collect_irq_snapshot, irq_paths_for, revert_irq_policy
from cpu.irq.cli import IrqCliDeps, run_irq_cli
from cpu.irq.domain import (
    IrqAction,
    compute_banned_cpulist,
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
        self.install_goes_live = True  # False: the package is there only after a reboot
        self.restart_rc = 0

    def _run_systemctl(self, cmd: list[str]) -> CommandResult:
        if cmd[:2] == ["systemctl", "show"]:
            out = f"LoadState={self.load_state}\nActiveState={self.active_state}\nUnitFileState={self.enabled_state}\n"
            return CommandResult(0, out, "", argv=tuple(cmd))
        if cmd == ["systemctl", "enable", "irqbalance.service"]:
            self.enabled_state = "enabled"
        elif cmd == ["systemctl", "restart", "irqbalance.service"]:
            if self.restart_rc != 0:
                return CommandResult(self.restart_rc, "", "restart failed", argv=tuple(cmd))
            self.active_state = "active"
        return CommandResult(0, "", "", argv=tuple(cmd))

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        **_kw: object,
    ) -> CommandResult:
        del input_text
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd and cmd[0] == "systemctl":
            return self._run_systemctl(cmd)
        if cmd[:2] == ["rpm-ostree", "install"]:
            if not self.install_rpm_succeeds:
                return CommandResult(1, "", "error: network unreachable", argv=tuple(cmd))
            if self.install_goes_live:
                self.load_state, self.active_state, self.enabled_state = "loaded", "inactive", "disabled"
            return CommandResult(0, "Checking out tree... done\n", "", argv=tuple(cmd))
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
            rpm_ostree_path=os.path.join(self.tmp, "usr", "bin", "rpm-ostree"),
            record_path=os.path.join(self.tmp, "var", "lib", "cst", "installed.json"),
        )
        self._write(self.paths.conf, "BUILD_CPUS='12-23'\n")
        self._write(self.paths.online_cpus, "0-23\n")

    def _write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _irq_state(self) -> str | None:
        rec = JsonInstallRecord().read(self.paths.record_path)
        return None if rec is None else rec.components.get(Component.CPU_IRQ)

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

        request = RootElevation("cst cpu irq", "apply", ("apply",), os.path.join(REPO, "cpu", "cpu-irq"), "writes")
        host_user = _FakeCtx(container=False, root=False)
        with self.assertRaises(SystemExit):
            enforce_host_root(request, ctx=host_user, elevate=fake_elevate)
        self.assertEqual(seen[0][0], "sudo")

    def test_apply_no_pkg_and_rpm_ostree_and_revert(self) -> None:
        rc_nopkg, out_nopkg = apply_irq_policy(self.paths, no_pkg=True, runner=self.runner, has_rpm_ostree=False)
        self.assertEqual(rc_nopkg, 0)
        self.assertTrue(any("--no-pkg" in line for line in out_nopkg))
        self.assertEqual(self._read(self.paths.env_path), render_irq_env("12-23"))
        self.assertEqual(self._read(self.paths.dropin_path), render_irq_dropin())
        # No irqbalance.service runs the ban-list yet: cpu:irq stays pending.
        self.assertEqual(self._irq_state(), ComponentState.PENDING)
        self.assertIn("pending:   cpu:irq (irqbalance.service is not there yet: run apply again once it is)", out_nopkg)

        rc_apply, out_apply = apply_irq_policy(self.paths, no_pkg=False, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc_apply, 0)
        self.assertIn(["rpm-ostree", "install", "--idempotent", "--apply-live", "irqbalance"], self.runner.calls)
        self.assertTrue(any("restarted: irqbalance.service" in line for line in out_apply))
        self.assertEqual(self._irq_state(), ComponentState.DONE)

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

    def test_installed_zipapp_writes_through_the_installer_service(self) -> None:
        # /usr/local/sbin/cpu-irq is a zipapp: no checkout, so no cst_core/installer.py beside it.
        # apply and revert write and remove through InstallService in this process, run no installer.
        zipapp = os.path.join(self.tmp, "usr", "local", "sbin", "cpu-irq")
        self._write(zipapp, "PK\x03\x04")
        paths = dataclasses.replace(
            irq_paths_for(zipapp, ctx=_FakeCtx(container=False, root=True), read_only=False),
            conf=self.paths.conf,
            online_cpus=self.paths.online_cpus,
            env_path=self.paths.env_path,
            sysconfig_path=self.paths.sysconfig_path,
            dropin_path=self.paths.dropin_path,
            rpm_ostree_path=self.paths.rpm_ostree_path,
            record_path=self.paths.record_path,
        )
        self.runner.load_state = "loaded"  # irqbalance.service is there: apply restarts it, cpu:irq is done
        rc, out = apply_irq_policy(paths, no_pkg=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._read(self.paths.env_path), render_irq_env("12-23"))
        self.assertEqual(self._read(self.paths.dropin_path), render_irq_dropin())
        self.assertIn(f"written:   {self.paths.env_path}", out)
        self.assertEqual(self._irq_state(), ComponentState.DONE)
        rec = JsonInstallRecord().read(self.paths.record_path)
        assert rec is not None
        self.assertEqual(
            sorted(rec.files),
            ["/etc/cpu-setup/irqbalance.env", "/etc/systemd/system/irqbalance.service.d/10-cst-ban.conf"],
        )
        self.assertFalse(any(c[0].endswith("python3") for c in self.runner.calls), self.runner.calls)

        rc_rev, out_rev = revert_irq_policy(paths, runner=self.runner)
        self.assertEqual(rc_rev, 0, out_rev)
        self.assertFalse(os.path.exists(self.paths.env_path))
        self.assertFalse(os.path.exists(self.paths.dropin_path))
        rec = JsonInstallRecord().read(self.paths.record_path)
        assert rec is not None
        self.assertEqual(dict(rec.files), {})

    def test_irq_stays_pending_until_irqbalance_runs_the_ban_list(self) -> None:
        # rpm-ostree installed the package (exit 0) but irqbalance.service is not live before a reboot:
        # the files are written, nothing is started, and cpu:irq stays pending until an apply starts it.
        self.runner.install_goes_live = False
        rc, out = apply_irq_policy(self.paths, no_pkg=False, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._read(self.paths.env_path), render_irq_env("12-23"))
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in self.runner.calls))
        self.assertEqual(self._irq_state(), ComponentState.PENDING)
        self.assertIn("pending:   cpu:irq (irqbalance.service is not there yet: run apply again once it is)", out)
        self.runner.load_state = "loaded"  # after the reboot
        rc, out = apply_irq_policy(self.paths, no_pkg=True, runner=self.runner)
        self.assertEqual(rc, 0, out)
        self.assertIn("restarted: irqbalance.service (ban 12-23)", out)
        self.assertEqual(self._irq_state(), ComponentState.DONE)

    def test_rpm_ostree_probe_reads_the_host_view(self) -> None:
        # In a toolbox, rpm-ostree is the host's (/run/host/usr/bin/rpm-ostree), not the container's PATH.
        mapped = irq_paths_for(
            os.path.join(REPO, "cpu", "cpu-irq"),
            ctx=_FakeCtx(container=True, root=False, host_root=self.tmp),
            read_only=True,
        )
        self.assertEqual(mapped.rpm_ostree_path, f"{self.tmp}/usr/bin/rpm-ostree")
        self.assertFalse(collect_irq_snapshot(self.paths, runner=self.runner).rpm_ostree_present)
        self._write(self.paths.rpm_ostree_path, "#!/bin/sh\n")
        self.assertTrue(collect_irq_snapshot(self.paths, runner=self.runner).rpm_ostree_present)

    def test_package_before_files_and_done_after_the_service(self) -> None:
        # A failed rpm-ostree install writes no file and records nothing.
        self.runner.install_rpm_succeeds = False
        rc_fail, out_fail = apply_irq_policy(self.paths, no_pkg=False, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc_fail, 1)
        self.assertIn("error: network unreachable", out_fail)
        self.assertFalse(os.path.exists(self.paths.env_path))
        self.assertFalse(os.path.exists(self.paths.dropin_path))
        self.assertFalse(os.path.exists(self.paths.record_path))

        # A failed restart of irqbalance leaves cpu:irq pending; the next good apply marks it done.
        self.runner.install_rpm_succeeds = True
        self.runner.restart_rc = 1
        rc_rs, out_rs = apply_irq_policy(self.paths, no_pkg=False, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc_rs, 1, out_rs)
        self.assertIn("restart failed", out_rs)
        self.assertEqual(self._irq_state(), ComponentState.PENDING)
        self.runner.restart_rc = 0
        rc_ok, out_ok = apply_irq_policy(self.paths, no_pkg=False, runner=self.runner, has_rpm_ostree=True)
        self.assertEqual(rc_ok, 0, out_ok)
        self.assertEqual(self._irq_state(), ComponentState.DONE)

    def test_bad_build_cpus_or_settings_refuse_apply_and_keep_files(self) -> None:
        # An empty ban-list would remove the files; a bad BUILD_CPUS must refuse instead (exit 1),
        # touching no file and restarting nothing.
        self._write(self.paths.env_path, render_irq_env("12-23"))
        self._write(self.paths.dropin_path, render_irq_dropin())
        self.runner.load_state, self.runner.active_state, self.runner.enabled_state = "loaded", "active", "enabled"
        for bad_conf, why in (
            ("BUILD_CPUS='12-abc'\n", "invalid BUILD_CPUS"),
            ("BUILD_CPUS=''\n", "BUILD_CPUS is empty"),
            ("BUILD_CPUS='12-23'\nP_CORES = '0-7'\n", "syntax errors"),
        ):
            with self.subTest(conf=bad_conf):
                self._write(self.paths.conf, bad_conf)
                self.runner.calls.clear()
                rc, out = apply_irq_policy(self.paths, no_pkg=True, runner=self.runner)
                self.assertEqual(rc, 1)
                self.assertTrue(any(line.startswith("ERROR:") and why in line for line in out), out)
                self.assertEqual(self._read(self.paths.env_path), render_irq_env("12-23"))
                self.assertEqual(self._read(self.paths.dropin_path), render_irq_dropin())
                self.assertEqual([c for c in self.runner.calls if c[:2] != ["systemctl", "show"]], [], "only reads")
                plan = format_irq_plan(collect_irq_snapshot(self.paths, runner=self.runner, has_rpm_ostree=True))
                self.assertTrue(any(line.startswith("ERROR:") and why in line for line in plan), plan)
                self.assertFalse(any(line.startswith("would ") for line in plan), plan)

    def test_a_drifted_drop_in_with_a_matching_env_is_a_change(self) -> None:
        # An older or hand-edited drop-in, the env file as apply writes it, irqbalance running:
        # apply rewrites the drop-in and must restart irqbalance so systemd reads it.
        env_path, dropin_path = self.paths.env_path, self.paths.dropin_path
        self._write(env_path, render_irq_env("12-23"))
        self.runner.load_state, self.runner.active_state, self.runner.enabled_state = "loaded", "active", "enabled"
        service_unchanged = "unchanged:   irqbalance.service (enabled, active)"

        self._write(dropin_path, render_irq_dropin())
        plan = format_irq_plan(collect_irq_snapshot(self.paths, runner=self.runner, has_rpm_ostree=True))
        self.assertIn(service_unchanged, plan)  # control: nothing drifted

        self._write(dropin_path, render_irq_dropin().replace("EnvironmentFile=-", "EnvironmentFile="))
        plan = format_irq_plan(collect_irq_snapshot(self.paths, runner=self.runner, has_rpm_ostree=True))
        self.assertIn(f"unchanged:   {env_path}", plan)
        self.assertIn(f"would write: {dropin_path}", plan)
        self.assertIn("would enable & start/restart: irqbalance.service", plan)
        self.assertNotIn(service_unchanged, plan)

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
