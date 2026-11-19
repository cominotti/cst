# SPDX-License-Identifier: MPL-2.0
"""Unit tests of the Python program of `cst bluetooth` (bluetooth/setup/) for what the
whole-program tests in test_setup.py cannot reach on a host: apply's last lines when every
check passes (the kernel ISO probe runs the real Python there), the waits of the restarts
(with a fake clock), and the step down from root to SUDO_USER (runuser). The machine is
the fake of setup_fakes.py; nothing real is touched.
"""

from collections.abc import Sequence
import json
import os
import unittest
from unittest import mock

from bluetooth.adapters import LocalBluetoothHostAdapter
from bluetooth.setup import wiring
from bluetooth.setup.adapters import LocalSetupFiles
from bluetooth.setup.application.change import Change
from bluetooth.setup.application.survey import SetupMachine
from bluetooth.setup.domain import (
    BapState,
    Exit,
    Options,
    Paths,
    bap_state,
    is_running,
    parse_command_line,
    resolve,
    unit_activity,
)
from bluetooth.tests.setup_fakes import ISO, MAC, REPO, Machine
from bluetooth.tests.test_ldac_watch import pw_dump
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner

DONE = f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n"
SELF = os.path.join(REPO, "bluetooth-setup.sh")
ENTRY = os.path.join(REPO, "bluetooth", "bt-setup")


class Console:
    """SetupConsolePort that keeps what was printed; no terminal to ask on."""

    def __init__(self) -> None:
        self.stdout = ""
        self.stderr = ""

    def out(self, text: str) -> None:
        self.stdout += text

    def err(self, text: str) -> None:
        self.stderr += text

    def out_closed(self) -> None:
        self.stdout += "<closed>"

    def ask(self, prompt: str) -> str | None:
        return None


class Clock:
    """ClockPort whose sleeps return at once and are recorded."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> float:
        return 1_700_000_000.0

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def change(m: Machine, *args: str, settings: dict[str, str] | None = None) -> tuple[Change, Console, Clock]:
    """The use cases over the fake machine, as inside a toolbox for the host commands (so the
    kernel ISO probe is the fake's), whatever the command."""
    paths = Paths(
        home=m.home, config=f"{m.home}/.config", etc=f"{m.root}/etc", usr_share=f"{m.root}/usr/share", checkout=REPO
    )
    opts = parse_command_line(list(args), "cst-t", "help\n")
    assert isinstance(opts, Options)
    cfg = resolve(opts, {"DEVICE": MAC, **(settings or {})}, list(settings or {}), paths, "cst-t")
    assert not isinstance(cfg, Exit)
    console, clock = Console(), Clock()
    env = m.env(container=True)
    machine = SetupMachine(
        runner=SubprocessCommandRunner(HostExecutionContext(exists=lambda _path: True), base_env=env),
        clock=clock,
        console=console,
        files=LocalSetupFiles(),
        host=LocalBluetoothHostAdapter(),
        container=True,
        bt_timeout=5,
        lc_ctype="",
        python="/usr/bin/python3",
    )
    return Change(opts, cfg, paths, machine), console, clock


class BapStateTest(unittest.TestCase):
    def test_bap_states_are_plain_strings(self) -> None:
        line = "Endpoint registered: sender=:1.5 path=/MediaEndpointLE/BAPSink/lc3"
        self.assertEqual(bap_state(line, [":1.5"]), "registered")
        self.assertEqual(bap_state(line.replace("registered", "unregistered"), [":1.5"]), "unregistered")
        self.assertEqual(bap_state(line, [":1.6"]), "no-record")
        self.assertEqual(
            {s.name: s.value for s in BapState},
            {
                "REGISTERED": "registered",
                "UNREGISTERED": "unregistered",
                "NO_RECORD": "no-record",
                "UNKNOWN": "unknown",
            },
        )


class UnitActivityTest(unittest.TestCase):
    """What `systemctl show -p ActiveState -p InvocationID` says of a unit, and when it runs."""

    def test_the_two_properties_in_either_order(self) -> None:
        self.assertEqual(unit_activity("ActiveState=active\nInvocationID=0a1b\n"), ("active", "0a1b"))
        self.assertEqual(unit_activity("InvocationID=0a1b\nActiveState=reloading"), ("reloading", "0a1b"))

    def test_missing_empty_and_odd_lines(self) -> None:
        self.assertEqual(unit_activity(""), ("", ""))
        self.assertEqual(unit_activity("ActiveState=inactive\nInvocationID=\n"), ("inactive", ""))
        self.assertEqual(unit_activity("no equals sign\nActiveState=failed\n"), ("failed", ""))
        self.assertEqual(unit_activity("InvocationID=0a1b\n"), ("", "0a1b"))

    def test_running_as_systemctl_is_active_sees_it(self) -> None:
        for word in ("active", "reloading", "refreshing"):
            self.assertTrue(is_running(word), word)
        for word in ("inactive", "failed", "activating", "deactivating", "maintenance", "", "Active", "unknown"):
            self.assertFalse(is_running(word), word)


class ApplyDoneTest(unittest.TestCase):
    """apply's last lines when every check passes, with LE Audio off and on."""

    def machine(self) -> Machine:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        m.adapter()
        m.flag("iso-socket")
        m.put("pw-dump.json", json.dumps(pw_dump(rate=48000)))
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        return m

    def test_le_audio_off(self) -> None:
        m = self.machine()
        m.put("wp-roles", "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n")
        run, console, _ = change(m, "apply", "--yes", "--no-restart")
        self.assertEqual(run.apply(), 0, console.stdout + console.stderr)
        self.assertEqual((run.fails, run.warns), (0, 0), console.stdout)
        self.assertTrue(
            console.stdout.endswith(
                "\nDone. LE Audio is off in PipeWire: the headset plays over a2dp-sink, with LDAC (auto).\n"
                "Turn LE Audio on: cst-t le-audio on\n"
            ),
            console.stdout,
        )

    def test_le_audio_on(self) -> None:
        m = self.machine()
        m.put("wp-roles", "default|\n")
        m.put("bt-journal", "Endpoint registered: sender=:1.7 path=/MediaEndpointLE/BAPSink/lc3\n")
        run, console, _ = change(m, "apply", "--yes", "--no-restart", settings={"LE_AUDIO": "on"})
        self.assertEqual(run.apply(), 0, console.stdout + console.stderr)
        self.assertTrue(
            console.stdout.endswith(
                "\nDone. With the headset on LE: bap-* profiles (LC3). On BR/EDR: a2dp-sink, with LDAC (auto).\n"
                "Choose the transport: cst-t apply --bearer le|bredr   (for connections the PC starts)\n"
            ),
            console.stdout,
        )

    def test_a_roles_answer_it_cannot_read_is_a_warning(self) -> None:
        # bt-helper wp-roles answered with no roles state: not a passing "LE Audio roles included"
        # (LE Audio is off here), a warning that names the answer.
        m = self.machine()
        m.put("wp-roles", "garbage|x\n")
        run, console, _ = change(m, "apply", "--yes", "--no-restart")
        run.apply()
        self.assertEqual((run.fails, run.warns), (0, 1), console.stdout)
        self.assertNotIn("LE Audio roles included", console.stdout)
        self.assertIn("WirePlumber bluez5.roles", console.stdout)
        self.assertIn("bt-helper wp-roles answered 'garbage', not a roles state", console.stdout)

    def test_no_headset(self) -> None:
        # No headset set: no LDAC rule and no headset checks, so no word of a headset playing.
        m = self.machine()
        m.put("wp-roles", "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n")
        run, console, _ = change(m, "apply", "--yes", "--no-restart", settings={"DEVICE": ""})
        self.assertEqual(run.apply(), 0, console.stdout + console.stderr)
        self.assertEqual((run.fails, run.warns), (0, 0), console.stdout)
        self.assertTrue(
            console.stdout.endswith(
                "\nDone. The headset's parts were skipped: no headset set: set DEVICE=AA:BB:CC:DD:EE:FF in "
                "~/.config/cst/bluetooth.conf (bluetoothctl devices lists the paired ones), or use --device MAC\n"
                "Turn LE Audio on: cst-t le-audio on\n"
            ),
            console.stdout,
        )

    def test_a_warning_is_counted(self) -> None:
        m = self.machine()
        run, console, _ = change(m, "apply", "--yes", "--no-restart")  # no wp-roles answer: a warning
        self.assertEqual(run.apply(), 0)
        self.assertTrue(console.stdout.endswith("\nDone, with 1 warning(s). Details: cst-t status\n"), console.stdout)


class WaitTest(unittest.TestCase):
    """The restarts wait for the adapter (15 s) and the BAP endpoint (15 s), asking every half second."""

    def test_the_adapter_that_does_not_answer(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        os.unlink(os.path.join(m.state, "prop-org.bluez.Adapter1-Powered"))
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, console, clock = change(m, "apply", "--yes")
        self.assertTrue(run.restart_bluetooth())
        self.assertEqual(clock.slept, [0.5] * 30)
        self.assertEqual(console.stderr, "WARNING: the adapter did not answer within 15 s after the restart\n")

    def test_the_bap_endpoint_that_does_not_come(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, console, clock = change(m, "apply", "--yes", settings={"LE_AUDIO": "on"})
        self.assertTrue(run.restart_wireplumber())
        self.assertEqual(clock.slept, [1] + [0.5] * 30)
        self.assertIn("WARNING: WirePlumber did not register the BAP endpoint within 15 s\n", console.stderr)
        # Each look reads bluetoothd's invocation again (it may restart by itself meanwhile).
        with open(os.path.join(m.state, "log"), encoding="utf-8") as f:
            shows = f.read().splitlines().count("systemctl show -p ActiveState -p InvocationID bluetooth.service")
        self.assertEqual(shows, 30)
        # Registered: no wait beyond the first second.
        m.put("bt-journal", "Endpoint registered: sender=:1.7 path=/MediaEndpointLE/BAPSink/lc3\n")
        run, console, clock = change(m, "apply", "--yes", settings={"LE_AUDIO": "on"})
        self.assertTrue(run.restart_wireplumber())
        self.assertEqual(clock.slept, [1])


class StepDownTest(unittest.TestCase):
    """As root, the program runs again as SUDO_USER, as bluetooth-setup.sh did with runuser."""

    def step_down(self, env: dict[str, str], *args: str, uid: int = 0) -> tuple[Exit | None, list[Sequence[str]]]:
        execs: list[Sequence[str]] = []

        def replace(argv: Sequence[str]) -> None:
            execs.append(list(argv))

        opts = parse_command_line(list(args), "cst-t", "help\n")
        assert isinstance(opts, Options)
        with (
            mock.patch.object(wiring, "effective_uid", return_value=uid),
            mock.patch.object(wiring, "account", side_effect=lambda u: (1000, f"/home/{u}") if u == "tester" else None),
            mock.patch.object(wiring, "replace", side_effect=replace),
        ):
            done = wiring.step_down(env, ENTRY, opts, "cst-t")
        return done, execs

    def test_as_a_user_nothing_happens(self) -> None:
        self.assertEqual(self.step_down({"SUDO_USER": "tester"}, "status", uid=1000), (None, []))

    def test_runuser_with_the_session_of_the_user(self) -> None:
        _, execs = self.step_down({"SUDO_USER": "tester"}, "status", "--device", MAC)
        session = [
            "HOME=/home/tester",
            "USER=tester",
            "LOGNAME=tester",
            "XDG_RUNTIME_DIR=/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        ]
        head = [
            "runuser",
            "-u",
            "tester",
            "--",
            "env",
            "-u",
            "XDG_CONFIG_HOME",
            "-u",
            "CST_BT_ROOT",
            "-u",
            "CST_BT_CONTAINER",
            "-u",
            "CST_BT_LIBWP",
            "-u",
            "SUDO_USER",
            *session,
        ]
        self.assertEqual(execs, [[*head, SELF, "status", "--device", MAC]])
        # CST_PROG is handed on only when the caller set it.
        _, execs = self.step_down({"SUDO_USER": "tester", "CST_PROG": "cst bluetooth"}, "plan")
        self.assertEqual(execs, [[*head, "CST_PROG=cst bluetooth", SELF, "plan"]])

    def test_without_a_user_to_switch_to(self) -> None:
        msg = (
            "cst-t: ERROR: run it from your user account (needs SUDO_USER): the WirePlumber parts belong to "
            "your session; it calls sudo by itself for main.conf\n"
        )
        for env in ({}, {"SUDO_USER": ""}, {"SUDO_USER": "root"}):
            self.assertEqual(self.step_down(env, "plan"), (Exit(1, err=msg), []), env)
        self.assertEqual(
            self.step_down({"SUDO_USER": "ghost"}, "plan"), (Exit(1, err="id: 'ghost': no such user\n"), [])
        )


if __name__ == "__main__":
    unittest.main()
