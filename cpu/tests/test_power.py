# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst cpu power` (`cpu/power` and `cpu/cpu-power`)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from typing import NoReturn, override
import unittest

from cst_core.adapters.runtime import RootElevation, enforce_host_root
from cpu.power.adapters import (
    PowerPaths,
    apply_power_plan,
    collect_power_snapshot,
    power_paths_for,
)
from cpu.power.cli import PowerCliDeps, run_power_cli
from cpu.power.domain import (
    PowerAction,
    format_power_plan,
    format_power_status,
    parse_power_args,
    plan_power_apply,
    plan_power_revert,
)
from tests.repo_files import REPO


class _FakePowerCtx:
    def __init__(self, *, container: bool = False, root: bool = True, host_root: str = "/run/host") -> None:
        self._container = container
        self._root = root
        self._host_root = host_root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def etc_dir(self) -> str:
        return f"{self._host_root}/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        base = f"{self._host_root}/run" if self._container else "/run"
        return os.path.join(base, subpath.strip("/")) if subpath.strip("/") else base


class CpuPowerDomainTest(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertEqual(parse_power_args([]), parse_power_args(["status"]))
        self.assertEqual(parse_power_args(["--help"]).action, PowerAction.HELP)
        self.assertEqual(parse_power_args(["apply", "--gpu-only"]), parse_power_args(["apply", "--gpu-only"]))
        self.assertTrue(parse_power_args(["plan", "--gpu-only"]).gpu_only)
        self.assertIsNotNone(parse_power_args(["status", "extra"]).error)
        self.assertIsNotNone(parse_power_args(["revert", "--gpu-only"]).error)
        self.assertIsNotNone(parse_power_args(["apply", "--unknown"]).error)
        self.assertIsNotNone(parse_power_args(["bogus"]).error)


class CpuPowerLifecycleTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-power-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = PowerPaths(
            conf=os.path.join(self.tmp, "etc", "cpu-setup", "cpu-setup.conf"),
            sys_drm=os.path.join(self.tmp, "sys", "class", "drm"),
            sys_cpu=os.path.join(self.tmp, "sys", "devices", "system", "cpu"),
            run=os.path.join(self.tmp, "run", "cpu-setup"),
            game_state=os.path.join(self.tmp, "run", "cpu-setup", "game.state"),
        )
        self._write_card("card1", slot="0000:04:00.0", driver="xe", control="auto", connectors={"DP-1": "disconnected"})
        self._write_card(
            "card3", slot="0000:84:00.0", driver="amdgpu", control="auto", connectors={"DP-7": "connected"}
        )
        for cpu in ("0", "1"):
            self._write_idle(cpu, "state0", "POLL", 0, "0")
            self._write_idle(cpu, "state1", "C2_ACPI", 127, "0")
            self._write_idle(cpu, "state2", "C3_ACPI", 1048, "0")

    def _write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    def _write_card(self, card: str, *, slot: str, driver: str, control: str, connectors: dict[str, str]) -> None:
        base = os.path.join(self.paths.sys_drm, card)
        self._write(os.path.join(base, "device", "uevent"), f"DRIVER={driver}\nPCI_SLOT_NAME={slot}\n")
        self._write(os.path.join(base, "device", "power", "control"), control + "\n")
        status = "active" if control == "on" or any(v == "connected" for v in connectors.values()) else "suspended"
        self._write(os.path.join(base, "device", "power", "runtime_status"), status + "\n")
        for out_name, conn_state in connectors.items():
            self._write(os.path.join(base, f"{card}-{out_name}", "status"), conn_state + "\n")

    def _write_idle(self, cpu: str, state: str, name: str, latency: int, disable: str) -> None:
        sdir = os.path.join(self.paths.sys_cpu, f"cpu{cpu}", "cpuidle", state)
        self._write(os.path.join(sdir, "name"), name + "\n")
        self._write(os.path.join(sdir, "latency"), f"{latency}\n")
        self._write(os.path.join(sdir, "disable"), disable + "\n")

    def test_plan_apply_idempotent_hotplug_move_and_revert_cycle(self) -> None:
        self._write(self.paths.conf, "GPU_PM_MODE='always'\nCPU_CSTATE_MAX_LATENCY_US='200'\n")
        snap0 = collect_power_snapshot(self.paths)
        plan0 = plan_power_apply(snap0)
        plan_lines = "\n".join(format_power_plan(snap0, plan0))
        self.assertIn("Dry run: nothing is changed.", plan_lines)
        self.assertIn("card3/device/power/control auto -> on", plan_lines)
        self.assertIn("2 change(s) -> 1 (ceiling=200 us)", plan_lines)
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card3/device/power/control")), "auto")
        self.assertEqual(self._read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state2/disable")), "0")

        # Apply sets card3=on and disables C3_ACPI, leaving card1=auto and C2_ACPI=0 alone.
        rc, apply_lines = apply_power_plan(self.paths, snap0, plan0)
        self.assertEqual(rc, 0)
        self.assertTrue(any("card3/device/power/control auto -> on" in line for line in apply_lines))
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card3/device/power/control")), "on")
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card1/device/power/control")), "auto")
        self.assertEqual(self._read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state1/disable")), "0")
        self.assertEqual(self._read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state2/disable")), "1")
        self.assertEqual(self._read(os.path.join(self.paths.sys_cpu, "cpu1/cpuidle/state2/disable")), "1")

        # Second apply is completely idempotent.
        snap1 = collect_power_snapshot(self.paths)
        status_txt = "\n".join(format_power_status(snap1))
        self.assertIn("power/control=on (active) [managed]", status_txt)
        self.assertIn("state2 C3_ACPI (1048 us): disabled on 2/2 CPUs [managed: 2]", status_txt)
        _, again_lines = apply_power_plan(self.paths, snap1, plan_power_apply(snap1))
        self.assertEqual(again_lines, ["unchanged: GPU runtime PM", "unchanged: CPU cpuidle"])

        # Hotplug move: display unplugged from card3, plugged into card1 -> card3 auto, card1 on.
        self._write(os.path.join(self.paths.sys_drm, "card3/card3-DP-7/status"), "disconnected\n")
        self._write(os.path.join(self.paths.sys_drm, "card1/card1-DP-1/status"), "connected\n")
        snap_plug = collect_power_snapshot(self.paths)
        apply_power_plan(self.paths, snap_plug, plan_power_apply(snap_plug, gpu_only=True), gpu_only=True)
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card3/device/power/control")), "auto")
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card1/device/power/control")), "on")
        self.assertEqual(self._read(self.paths.gpu_managed_file), "0000:04:00.0")

        # Game profile holding card3/card1 prevents revert/hotplug from fighting it.
        ctrl1 = os.path.join(self.paths.sys_drm, "card1/device/power/control")
        self._write(self.paths.game_state, f"sysfs {ctrl1} auto\n")
        snap_game = collect_power_snapshot(self.paths)
        apply_power_plan(self.paths, snap_game, plan_power_revert(snap_game))
        self.assertEqual(self._read(ctrl1), "on")
        os.remove(self.paths.game_state)

        # Full revert after game ends restores card1=auto and C3_ACPI=0, cleans marker files.
        snap_end = collect_power_snapshot(self.paths)
        apply_power_plan(self.paths, snap_end, plan_power_revert(snap_end))
        self.assertEqual(self._read(ctrl1), "auto")
        self.assertEqual(self._read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state2/disable")), "0")
        self.assertFalse(os.path.exists(self.paths.gpu_managed_file))
        self.assertFalse(os.path.exists(self.paths.cstate_managed_file))

    def test_gpu_mode_game_and_preexisting_on_respected(self) -> None:
        # card1 was already `on` before CST ran -> CST never adds it to gpu-pm-managed and never reverts it.
        self._write_card("card1", slot="0000:04:00.0", driver="xe", control="on", connectors={"DP-1": "connected"})
        self._write(self.paths.conf, "GPU_PM_MODE='always'\nCPU_CSTATE_MAX_LATENCY_US='off'\n")
        snap = collect_power_snapshot(self.paths)
        apply_power_plan(self.paths, snap, plan_power_apply(snap))
        self.assertEqual(self._read(self.paths.gpu_managed_file), "0000:84:00.0")
        self.assertFalse(os.path.exists(self.paths.cstate_managed_file))

        # Switching to GPU_PM_MODE=game reverts card3 (CST-managed), leaves pre-existing card1=on alone.
        self._write(self.paths.conf, "GPU_PM_MODE='game'\nCPU_CSTATE_MAX_LATENCY_US='off'\n")
        snap2 = collect_power_snapshot(self.paths)
        apply_power_plan(self.paths, snap2, plan_power_apply(snap2))
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card3/device/power/control")), "auto")
        self.assertEqual(self._read(os.path.join(self.paths.sys_drm, "card1/device/power/control")), "on")
        self.assertFalse(os.path.exists(self.paths.gpu_managed_file))

    def test_cli_container_elevation_and_readonly_in_toolbox(self) -> None:
        paths_ro = power_paths_for(ctx=_FakePowerCtx(container=True, root=False, host_root=self.tmp), read_only=True)
        self.assertEqual(paths_ro.conf, f"{self.tmp}/etc/cpu-setup/cpu-setup.conf")

        out, err = io.StringIO(), io.StringIO()
        rc_plan = run_power_cli(
            ["plan"],
            deps=PowerCliDeps(
                entry_file=os.path.join(REPO, "cpu", "cpu-power"),
                env={"CST_PROG": "cst cpu"},
                out=out,
                err=err,
                ctx=_FakePowerCtx(container=True, root=False, host_root=self.tmp),
            ),
        )
        self.assertEqual((rc_plan, err.getvalue()), (0, ""))
        self.assertIn("Dry run: nothing is changed.", out.getvalue())

        req = RootElevation(
            "cst cpu power",
            "apply",
            ("apply",),
            os.path.join(REPO, "cpu", "cpu-power"),
            "writes /sys and /run/cpu-setup",
        )
        refusal = enforce_host_root(req, ctx=_FakePowerCtx(container=True, root=False))
        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertIn("must run on the host", refusal)

        elevated: list[list[str]] = []

        def _elevate(argv: list[str]) -> NoReturn:
            elevated.append(argv)
            raise SystemExit(99)

        with self.assertRaises(SystemExit):
            enforce_host_root(req, ctx=_FakePowerCtx(container=False, root=False), elevate=_elevate)
        self.assertEqual(elevated[0][0], "sudo")

        r_help = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", os.path.join(REPO, "cpu", "cpu-power"), "--help"],
            env={"PATH": "/usr/bin:/bin", "CST_PROG": "cst cpu"},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual((r_help.returncode, r_help.stderr), (0, ""))
        self.assertIn("Usage: cst cpu power", r_help.stdout)


if __name__ == "__main__":
    unittest.main()
