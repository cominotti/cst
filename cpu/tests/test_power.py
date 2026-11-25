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
from unittest import mock

from cst_core.adapters.locks import take_lock
from cst_core.adapters.runtime import RootElevation, enforce_host_root
from cpu.power.adapters import (
    PowerPaths,
    apply_power_plan,
    collect_power_snapshot,
    execute_power_action,
    power_paths_for,
)
from cpu.power.cli import PowerCliDeps, run_power_cli
from cpu.game.domain import parse_cstate_limit_us
from cpu.power.domain import (
    GpuPmMode,
    boot_cstate_limit_us,
    PowerAction,
    parse_gpu_pm_mode,
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

    def path(self, host_path: str) -> str:
        return (
            f"{self._host_root}/{host_path.lstrip('/')}" if self._container and host_path.startswith("/") else host_path
        )

    def etc_dir(self) -> str:
        return f"{self._host_root}/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        base = f"{self._host_root}/run" if self._container else "/run"
        return os.path.join(base, subpath.strip("/")) if subpath.strip("/") else base


class CpuPowerDomainTest(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertEqual(parse_power_args([]), parse_power_args(["status"]))
        self.assertEqual(parse_power_args(["--help"]).action, PowerAction.HELP)
        applied = parse_power_args(["apply", "--gpu-only"])
        self.assertEqual((applied.action, applied.gpu_only, applied.error), (PowerAction.APPLY, True, None))
        self.assertTrue(parse_power_args(["plan", "--gpu-only"]).gpu_only)
        self.assertIsNotNone(parse_power_args(["status", "extra"]).error)
        self.assertIsNotNone(parse_power_args(["revert", "--gpu-only"]).error)
        self.assertIsNotNone(parse_power_args(["apply", "--unknown"]).error)
        self.assertIsNotNone(parse_power_args(["bogus"]).error)


class CpuSettingsWordsTest(unittest.TestCase):
    def test_a_unicode_blank_around_a_word_is_part_of_it(self) -> None:
        # cpu-setup.conf is read as bash sources it, so a no-break space stays in the value; str.strip()
        # used to drop it, and "\N{NO-BREAK SPACE}game" was game, "50\N{NO-BREAK SPACE}" 50 us.
        self.assertEqual(parse_gpu_pm_mode(" game\t"), (GpuPmMode.GAME, None))
        word = "\N{NO-BREAK SPACE}game"
        self.assertEqual(
            parse_gpu_pm_mode(word), (GpuPmMode.ALWAYS, f"GPU_PM_MODE={word!r} must be always, game, or off")
        )
        self.assertEqual(parse_cstate_limit_us(" 50\n", default_us=200), (50, None))
        for value in ("50\N{NO-BREAK SPACE}", "off\N{IDEOGRAPHIC SPACE}"):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_cstate_limit_us(value, default_us=200),
                    (200, f"{value!r} must be a positive microsecond integer or 'off'"),
                )

    def test_a_cstate_cap_past_ints_digit_limit_is_no_number(self) -> None:
        # int() raised ValueError past 4300 digits, out of cpu-power status, plan and apply, the game
        # profile's power step and cst perf. Such a value is no number: the default cap and the error.
        huge = "1" * 5000
        error = f"{huge!r} must be a positive microsecond integer or 'off'"
        self.assertEqual(parse_cstate_limit_us(huge, default_us=200), (200, error))
        self.assertEqual(boot_cstate_limit_us(huge, explicit=True, cpuidle_driver="intel_idle"), (200, error))
        self.assertEqual(
            parse_cstate_limit_us("0" * 5000, default_us=200),
            (200, f"{'0' * 5000!r} must be a positive microsecond integer or 'off'"),
        )
        self.assertEqual(parse_cstate_limit_us("7" * 4300, default_us=200), (int("7" * 4300), None))


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

        host_user = _FakePowerCtx(container=False, root=False)
        with self.assertRaises(SystemExit):
            enforce_host_root(req, ctx=host_user, elevate=_elevate)
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

    def test_cstate_cap_default_follows_the_cpuidle_driver(self) -> None:
        driver = os.path.join(self.paths.sys_cpu, "cpuidle", "current_driver")
        cases = (
            # This host (intel_idle) and an unreadable driver: the 200 us default, as before.
            ("intel_idle", "GPU_PM_MODE='always'\n", 200),
            (None, "GPU_PM_MODE='always'\n", 200),
            # AMD (acpi_idle) without the setting: no cap; a value the file sets always counts.
            ("acpi_idle", "GPU_PM_MODE='always'\n", None),
            ("acpi_idle", "GPU_PM_MODE='always'\nCPU_CSTATE_MAX_LATENCY_US='200'\n", 200),
            ("acpi_idle", "CPU_CSTATE_MAX_LATENCY_US='off'\n", None),
            ("intel_idle", "CPU_CSTATE_MAX_LATENCY_US='off'\n", None),
            # A value of a no-break space is set (not empty to bash), and no number: the 200 us default
            # with a warning, also with acpi_idle (str.strip() made it "not set": no cap there).
            ("acpi_idle", "CPU_CSTATE_MAX_LATENCY_US='\N{NO-BREAK SPACE}'\n", 200),
            ("intel_idle", "CPU_CSTATE_MAX_LATENCY_US='50\N{NO-BREAK SPACE}'\n", 200),
        )
        for drv, conf, want in cases:
            with self.subTest(driver=drv, conf=conf):
                if drv is None:
                    if os.path.exists(driver):
                        os.remove(driver)
                else:
                    self._write(driver, drv + "\n")
                self._write(self.paths.conf, conf)
                self.assertEqual(collect_power_snapshot(self.paths).cstate_limit_us, want)

    def test_a_problem_line_warns_with_its_reason(self) -> None:
        # The line alone hid a no-break space at its end: the warning names what is wrong with it.
        self._write(self.paths.conf, "GPU_PM_MODE='always'\nCPU_CSTATE_MAX_LATENCY_US='50\N{NO-BREAK SPACE}'\n")
        warnings = collect_power_snapshot(self.paths).config_warnings
        self.assertIn(
            f"{self.paths.conf} line 2: CPU_CSTATE_MAX_LATENCY_US='50\N{NO-BREAK SPACE}': a no-break space (U+00A0) "
            "at the end of the value: CST does not trim it (delete it)",
            warnings,
        )

    def test_apply_and_revert_wait_for_the_run_lock(self) -> None:
        # cpu-game-profile holds /run/cpu-setup/lock while it switches; apply and revert wait for it
        # and change nothing when it stays held. status and plan never wait.
        self._write(self.paths.conf, "GPU_PM_MODE='always'\nCPU_CSTATE_MAX_LATENCY_US='200'\n")
        card3 = os.path.join(self.paths.sys_drm, "card3/device/power/control")
        c3 = os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state2/disable")
        lock = os.path.join(self.paths.run, "lock")
        # Created as cpu-game-profile creates it: 0644 (cst doctor reads it as a user).
        self.assertEqual(execute_power_action(parse_power_args(["revert"]), self.paths)[0], 0)
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o644)
        held = take_lock(lock, wait=False)
        assert held is not None
        try:
            with mock.patch("cpu.power.adapters._POWER_LOCK_TIMEOUT_S", 0.3, create=True):
                self.assertEqual(execute_power_action(parse_power_args(["plan"]), self.paths)[0], 0)
                rc, out = execute_power_action(parse_power_args(["apply"]), self.paths)
                self.assertEqual(rc, 1, out)
                self.assertIn(f"ERROR: {lock} still held after 0.3 s; nothing changed", out)
                self.assertEqual((self._read(card3), self._read(c3)), ("auto", "0"))
                self.assertFalse(os.path.exists(os.path.join(self.paths.run, "gpu-pm-managed")))
        finally:
            os.close(held)
        rc, out = execute_power_action(parse_power_args(["apply"]), self.paths)
        self.assertEqual(rc, 0, out)
        self.assertEqual((self._read(card3), self._read(c3)), ("on", "1"))
        held = take_lock(lock, wait=False)
        assert held is not None
        try:
            with mock.patch("cpu.power.adapters._POWER_LOCK_TIMEOUT_S", 0.3, create=True):
                rc, out = execute_power_action(parse_power_args(["revert"]), self.paths)
                self.assertEqual(rc, 1, out)
                self.assertEqual((self._read(card3), self._read(c3)), ("on", "1"))
        finally:
            os.close(held)
        rc, out = execute_power_action(parse_power_args(["revert"]), self.paths)
        self.assertEqual(rc, 0, out)
        self.assertEqual((self._read(card3), self._read(c3)), ("auto", "0"))


if __name__ == "__main__":
    unittest.main()
