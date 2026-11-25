# SPDX-License-Identifier: MPL-2.0
"""Unit tests of the Python program of `cst bluetooth` (bluetooth/setup/) for what the
whole-program tests in test_setup.py cannot reach on a host: apply's last lines when every
check passes (the kernel ISO probe runs the real Python there), the waits of the restarts
(with a fake clock), and the step down from root to SUDO_USER (runuser). The machine is
the fake of setup_fakes.py; nothing real is touched.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import errno
import json
import os
import re
import shutil
import time
from typing import override
import unittest
from unittest import mock

from bluetooth.adapters import LocalBluetoothHostAdapter
from bluetooth.domain import parse_pending
from bluetooth.setup import wiring
from bluetooth.setup.adapters import LocalSetupFiles
from bluetooth.setup.application.change import Change
from bluetooth.setup.application.survey import SetupMachine
from bluetooth.setup.domain import (
    A2DP_SINK_UUID,
    BapState,
    Exit,
    Options,
    Paths,
    backup_name_taken,
    backup_order,
    bap_state,
    is_running,
    parse_command_line,
    resolve,
)
from bluetooth.tests.setup_fakes import DEV, ISO, MAC, REPO, Machine, SetupModel
from bluetooth.tests.test_ldac_watch import cards, pw_dump
from cst_core.domain.files import OwnedState
from cst_core.adapters import SystemctlAdapter
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.ports import CommandRunnerPort, SystemctlHost

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

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.slept: list[float] = []
        self.at = now

    def now(self) -> float:
        return self.at

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@dataclass(frozen=True)
class SetupDoubles:
    """What a test puts in place of change()'s defaults: a runner, the files and host adapters, the
    clock's time (None: FAKE's commands through the real runner, the real adapters, a fixed time)."""

    runner: CommandRunnerPort | None = None
    files: LocalSetupFiles | None = None
    host: LocalBluetoothHostAdapter | None = None
    now: float | None = None


def change(
    m: Machine,
    *args: str,
    settings: dict[str, str] | None = None,
    show_timeout: float | None = None,
    container: bool = True,
    doubles: SetupDoubles | None = None,
) -> tuple[Change, Console, Clock]:
    """The use cases over the fake machine, as inside a toolbox for the host commands (so the
    kernel ISO probe is the fake's), whatever the command; show_timeout: systemctl show's limit
    (cst_core's 20 s when None); container=False: the program believes it runs on the host (its
    commands still go to the fake). doubles: a test's own runner, adapters or time."""
    paths = Paths(
        home=m.home, config=f"{m.home}/.config", etc=f"{m.root}/etc", usr_share=f"{m.root}/usr/share", checkout=REPO
    )
    opts = parse_command_line(list(args), "cst-t", "help\n")
    assert isinstance(opts, Options)
    cfg = resolve(opts, {"DEVICE": MAC, **(settings or {})}, list(settings or {}), paths, "cst-t")
    assert not isinstance(cfg, Exit)
    d = doubles or SetupDoubles()
    console, clock = Console(), (Clock() if d.now is None else Clock(d.now))
    env = m.env(container=True)
    runner = d.runner
    if runner is None:
        runner = SubprocessCommandRunner(HostExecutionContext(exists=lambda _path: True), base_env=env)
    machine = SetupMachine(
        runner=runner,
        systemd=SystemctlAdapter(runner, timeout=show_timeout),
        user_systemd_host=SystemctlAdapter(runner, user=True, on_host=SystemctlHost.EVERY),
        user_systemd=SystemctlAdapter(runner, user=True, on_host=SystemctlHost.LOCAL),
        clock=clock,
        console=console,
        files=LocalSetupFiles() if d.files is None else d.files,
        host=LocalBluetoothHostAdapter() if d.host is None else d.host,
        container=container,
        bt_timeout=5,
        lc_ctype="",
        c_locale=wiring.c_locale_words(),
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

    def test_one_show_through_cst_core(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, _, _ = change(m, "status")
        state = run.unit_show("wireplumber.service", user=True)
        self.assertEqual(state, ("active", "wp-0"))
        self.assertEqual(
            m.read("log").splitlines()[-1], "systemctl --user show -p ActiveState -p InvocationID wireplumber.service"
        )
        # In the C locale, whatever the caller's.
        self.assertIn("LC_ALL=C", m.env_log().splitlines()[-1].split("\t", 1)[1].split())

    def test_a_system_unit_and_a_user_unit_of_one_name(self) -> None:
        # Each scope is a unit of its own: one's state never answers for the other's.
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, _, _ = change(m, "status")
        self.assertEqual(run.unit_show("bluetooth.service", user=False), ("active", "bt-0"))
        self.assertEqual(run.unit_show("bluetooth.service", user=True), ("inactive", ""))
        self.assertEqual(run.unit_show("wireplumber.service", user=True), ("active", "wp-0"))
        self.assertEqual(run.unit_show("wireplumber.service", user=False), ("inactive", ""))
        # Kept per scope, and forgotten in both after a restart.
        shows = len(m.read("log").splitlines())
        self.assertEqual(run.unit_show("bluetooth.service", user=True), ("inactive", ""))
        self.assertEqual(len(m.read("log").splitlines()), shows)
        run.forget_unit("bluetooth.service")
        run.unit_show("bluetooth.service", user=False)
        run.unit_show("bluetooth.service", user=True)
        self.assertEqual(len([c for c in m.read("log").splitlines()[shows:] if c.startswith("systemctl")]), 2)

    def test_a_systemctl_that_does_not_answer(self) -> None:
        # Nothing answers (PID 1 or the user manager stuck): the look gives up at the limit and
        # tells nothing, so the unit reads as not running and a wait for it can end.
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        m.put("hang-show", "")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, _, _ = change(m, "status", show_timeout=1)
        start = time.monotonic()
        self.assertEqual(run.unit_show("bluetooth.service", user=False), ("", ""))
        self.assertLess(time.monotonic() - start, 10)

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


class WatcherInstallTest(unittest.TestCase):
    """What the installer wrote, not what plan saw, decides the watcher's daemon-reload and restart."""

    def test_files_that_changed_after_plan(self) -> None:
        # plan found both files current and the watcher running; by the install they are not
        # (a git pull meanwhile): the installer writes them, and the watcher is restarted.
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, console, _ = change(m, "apply", "--yes", settings={"LDAC_RATE": "48000"})
        run.lw_prog_st = run.lw_unit_st = OwnedState.OK
        run.lw_enabled, run.lw_active = "enabled", "active"
        run.lw_install()
        self.assertIn(f"written:   {m.lw_prog}\n", console.stdout)
        self.assertIn(f"written:   {m.lw_unit} (LDAC to 48000 Hz)\n", console.stdout)
        self.assertIn("restarted: cst-bt-ldac48.service (new files)\n", console.stdout)
        calls = m.read("log").splitlines()
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertIn("systemctl --user restart cst-bt-ldac48.service", calls)

    def test_current_files_neither_reload_nor_restart(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, console, _ = change(m, "apply", "--yes", settings={"LDAC_RATE": "48000"})
        run.lw_prog_st = run.lw_unit_st = OwnedState.MISSING
        run.lw_enabled, run.lw_active = "enabled", ""  # only started (the fake's enable talks on stderr)
        run.lw_install()
        run, console, _ = change(m, "apply", "--yes", settings={"LDAC_RATE": "48000"})
        run.lw_prog_st = run.lw_unit_st = OwnedState.OK
        run.lw_enabled, run.lw_active = "enabled", "active"
        before = len(m.read("log").splitlines())
        run.lw_install()
        self.assertIn(f"unchanged: {m.lw_prog}\nunchanged: {m.lw_unit} (LDAC to 48000 Hz)\n", console.stdout)
        self.assertIn("unchanged: cst-bt-ldac48.service enabled and active\n", console.stdout)
        self.assertEqual(
            [c for c in m.read("log").splitlines()[before:] if c.startswith("systemctl --user") and "is-" not in c], []
        )


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
        shows = m.read("log").splitlines().count("systemctl show -p ActiveState -p InvocationID bluetooth.service")
        self.assertEqual(shows, 30)
        # Registered: no wait beyond the first second.
        m.put("bt-journal", "Endpoint registered: sender=:1.7 path=/MediaEndpointLE/BAPSink/lc3\n")
        run, console, clock = change(m, "apply", "--yes", settings={"LE_AUDIO": "on"})
        self.assertTrue(run.restart_wireplumber())
        self.assertEqual(clock.slept, [1])


class BearerUnknownTest(unittest.TestCase):
    """plan on the host without bluetoothctl: the preferred bearer cannot be read."""

    def test_bluetoothctl_not_found(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        self.enterContext(
            mock.patch.object(
                LocalSetupFiles,
                "which",
                new=lambda _self, name: name != "bluetoothctl" and shutil.which(name) is not None,
            )
        )
        run, console, _ = change(m, "plan", settings={"BEARER": "bredr"}, container=False)
        self.assertEqual(run.plan(), 10, console.stdout + console.stderr)
        self.assertEqual(run.bearer_state, "unknown")
        self.assertIn(f"  {'?':<9} {'preferred bearer':<52} bluetoothctl not found\n", console.stdout)
        self.assertNotIn("bluetoothctl", m.read("log"))


class RestoreA2dpTest(unittest.TestCase):
    """After the WirePlumber restart, a headset without its Audio Sink is connected again and
    switched back to A2DP: up to 20 looks, half a second apart."""

    def machine(self, *, connected: str | None, delay: int = 0) -> Machine:
        """The headset on HFP without A2DP; after bluetoothctl connect, its card is CONNECTED
        (an active profile, with A2DP offered) from the look after DELAY looks (None: never)."""
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        m.put("bt-info", "\tPaired: yes\n\tBREDR.Connected: yes\n\tLE.Connected: no\n")
        hfp = json.loads(json.dumps(cards(active="headset-head-unit")))
        hfp[1]["profiles"]["a2dp-sink"]["available"] = False
        m.put("pactl-cards", json.dumps(hfp))
        if connected is not None:
            m.put("pactl-cards-connected", json.dumps(cards(active=connected)))
            m.put("a2dp-delay", f"{delay}\n")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        return m

    def calls(self, m: Machine) -> list[str]:
        return [
            c for c in m.read("log").splitlines() if c.startswith(("bluetoothctl connect", "pactl set-card-profile"))
        ]

    def test_a2dp_comes_back_after_the_connect(self) -> None:
        m = self.machine(connected="headset-head-unit", delay=2)
        run, console, clock = change(m, "apply", "--yes")
        run.restore_a2dp()
        self.assertEqual(
            console.stdout,
            "reconnect: the headset's A2DP profile (Audio Sink)\nrestored:  the headset is back on A2DP\n",
        )
        self.assertEqual(console.stderr, "")
        self.assertEqual(
            self.calls(m),
            [f"bluetoothctl connect {MAC} {A2DP_SINK_UUID}", f"pactl set-card-profile bluez_card.{DEV} a2dp-sink"],
        )
        self.assertEqual(clock.slept, [0.5, 0.5])

    def test_a2dp_that_never_comes(self) -> None:
        m = self.machine(connected=None)
        run, console, clock = change(m, "apply", "--yes")
        run.restore_a2dp()
        self.assertEqual(console.stdout, "reconnect: the headset's A2DP profile (Audio Sink)\n")
        self.assertEqual(console.stderr, "WARNING: A2DP did not come back; switch the headset off and on\n")
        self.assertEqual(self.calls(m), [f"bluetoothctl connect {MAC} {A2DP_SINK_UUID}"])
        self.assertEqual(clock.slept, [0.5] * 20)

    def test_already_on_a2dp_when_it_comes(self) -> None:
        # The look that sees A2DP also sees the card on it: no switch.
        m = self.machine(connected="a2dp-sink")
        run, console, clock = change(m, "apply", "--yes")
        run.restore_a2dp()
        self.assertEqual(
            console.stdout,
            "reconnect: the headset's A2DP profile (Audio Sink)\nrestored:  the headset is back on A2DP\n",
        )
        self.assertEqual(self.calls(m), [f"bluetoothctl connect {MAC} {A2DP_SINK_UUID}"])
        self.assertEqual(clock.slept, [])


class AdapterTargetTest(unittest.TestCase):
    """After the bluetoothd restart, the adapter waited for: busctl's, else /org/bluez/hci0
    (bluetooth-setup.sh's $(adapter_path || echo /org/bluez/hci0))."""

    def powered(self, objects: object, *, fail: bool = False) -> list[str]:
        m = Machine(self)
        m.put(m.main_conf, DONE)
        m.services()
        m.put("objects.json", json.dumps(objects))
        if fail:
            m.flag("fail-objects")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        run, _, _ = change(m, "apply", "--yes")
        self.assertTrue(run.restart_bluetooth())
        calls = m.read("log").splitlines()
        first = next(i for i, c in enumerate(calls) if c.startswith("busctl get-property "))
        return calls[first:]

    def test_no_adapter_in_the_answer(self) -> None:
        log = self.powered({"type": "a{oa{sa{sv}}}", "data": [{"/org/bluez": {}}]})
        self.assertEqual(log, ["busctl get-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Powered"])

    def test_busctl_failed_after_naming_one(self) -> None:
        # Its path, a newline, then the fallback: the one word bash's $(...) gave.
        objects = {"type": "a{oa{sa{sv}}}", "data": [{"/org/bluez/hci1": {"org.bluez.Adapter1": {}}}]}
        log = self.powered(objects, fail=True)
        self.assertEqual(
            log, ["busctl get-property org.bluez /org/bluez/hci1", "/org/bluez/hci0 org.bluez.Adapter1 Powered"]
        )


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


VENDOR = "[General]\n#Experimental = false\n"


class InProcessCase(unittest.TestCase):
    """Whole commands in process (Change.run_command) on the fake machine, with one fixed second on
    the clock, on a machine where every check passes."""

    NOW = 1_700_000_000.0

    def machine(self) -> Machine:
        m = Machine(self)
        m.put(m.main_conf, VENDOR)
        m.services()
        m.adapter()  # every check passes: apply exits 0
        m.flag("iso-socket")
        m.put("pw-dump.json", json.dumps(pw_dump(rate=48000)))
        m.put("wp-roles", "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        return m

    def base(self, m: Machine) -> str:
        return f"{m.main_conf}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.NOW))}"

    def run_one(self, m: Machine, *args: str) -> tuple[int, str, str]:
        log = os.path.join(m.state, "log")
        if os.path.exists(log):
            os.unlink(log)
        run, console, _ = change(m, *args, doubles=SetupDoubles(now=self.NOW))
        rc = run.run_command()
        run.cleanup()
        assert isinstance(rc, int), rc
        return rc, console.stdout, console.stderr

    def text(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def sudo_cps(self, m: Machine) -> list[str]:
        return [c for c in m.read("log").splitlines() if c.startswith("sudo env LC_ALL=C cp ")]


class BackupNameTest(InProcessCase):
    """main.conf's backups (apply and revert take one before they replace it): each has a name of its
    own, main.conf.bak-YYYYmmdd-HHMMSS and then -1, -2, ... in the same second; none is replaced. The
    runs here share one second (the clock is fixed); FAKE's sudo cp imitates GNU cp's
    --update=none-fail (tests/e2e test_180 runs the real one)."""

    def test_apply_then_revert_in_one_second(self) -> None:
        # Before, revert's backup took apply's name and replaced the vendor text with apply's, so a
        # second revert found Experimental = true in the oldest backup and wrote it back.
        m = self.machine()
        base = self.base(m)
        rc, out, err = self.run_one(m, "apply", "--yes", "--no-restart")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"backup:    {base}\n", out)
        self.assertEqual(self.sudo_cps(m), [f"sudo env LC_ALL=C cp -p --update=none-fail {m.main_conf} {base}"])
        self.assertEqual(self.text(base), VENDOR)
        rc, out, err = self.run_one(m, "revert", "--yes", "--no-restart")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"backup:    {base}-1\n", out)
        self.assertIn(f"           the two keys as in {base}\n", out)
        self.assertEqual(self.text(base), VENDOR)
        self.assertIn(f"KernelExperimental = {ISO}", self.text(base + "-1"))
        self.assertNotIn("Experimental = true", self.text(m.main_conf))
        rc, out, err = self.run_one(m, "revert", "--yes", "--no-restart")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"unchanged: Experimental and KernelExperimental are already as in {base}\n", out)
        self.assertEqual(self.sudo_cps(m), [])
        self.assertNotIn("Experimental = true", self.text(m.main_conf))

    def test_the_names_taken_are_skipped(self) -> None:
        m = self.machine()
        base = self.base(m)
        planted = {base: "first\n", base + "-1": "second\n"}
        for path, text in planted.items():
            m.put(path, text)
            os.utime(path, (2_000_000_000, 2_000_000_000))  # newer than main.conf: never the oldest
        rc, out, err = self.run_one(m, "apply", "--yes", "--no-restart")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"backup:    {base}-2\n", out)
        self.assertEqual(self.sudo_cps(m), [f"sudo env LC_ALL=C cp -p --update=none-fail {m.main_conf} {base}-2"])
        self.assertEqual(self.text(base + "-2"), VENDOR)
        for path, text in planted.items():
            self.assertEqual((self.text(path), os.stat(path).st_mtime), (text, 2_000_000_000), path)

    def test_a_name_another_run_takes_meanwhile(self) -> None:
        # Free when apply looked, created by another run before the copy: left alone, the next name taken.
        m = self.machine()
        base = self.base(m)
        m.flag("race-backup")
        rc, out, err = self.run_one(m, "apply", "--yes", "--no-restart")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"backup:    {base}-1\n", out)
        self.assertEqual(self.text(base), "another run's backup\n")
        self.assertEqual(self.text(base + "-1"), VENDOR)
        self.assertNotIn("cp:", out + err)
        self.assertEqual(len(self.sudo_cps(m)), 2)

    def test_a_name_another_run_takes_meanwhile_in_another_language(self) -> None:
        # A sudoers that keeps no LC_ALL (env_keep and env_check without it) and a pt_BR session: cp
        # gets the C locale from env all the same, so its "File exists" is read and the next name taken.
        # Before, sudo cp spoke Portuguese ("Arquivo existe"), and the run stopped with exit 1.
        m = self.machine()
        base = self.base(m)
        m.flag("race-backup")
        m.flag("sudo-keeps-no-lc-all")
        env = {**m.env(container=True), "LANG": "pt_BR.UTF-8"}
        runner = SubprocessCommandRunner(HostExecutionContext(exists=lambda _path: True), base_env=env)
        run, console, _ = change(m, "apply", "--yes", "--no-restart", doubles=SetupDoubles(runner=runner, now=self.NOW))
        rc = run.run_command()
        run.cleanup()
        self.assertEqual(rc, 0, console.stdout + console.stderr)
        self.assertIn(f"backup:    {base}-1\n", console.stdout)
        self.assertEqual(self.text(base), "another run's backup\n")
        self.assertEqual(self.text(base + "-1"), VENDOR)
        self.assertEqual(len(self.sudo_cps(m)), 2)

    def test_fakes_cp_speaks_the_session_language_without_env(self) -> None:
        # The test above can bite: under that sudoers a plain `sudo cp` gets the caller's LANG.
        m = self.machine()
        m.flag("sudo-keeps-no-lc-all")
        dst = m.main_conf + ".bak-taken"
        m.put(dst, "taken\n")
        env = {**m.env(container=True), "LANG": "pt_BR.UTF-8"}
        runner = SubprocessCommandRunner(HostExecutionContext(exists=lambda _path: True), base_env=env)
        for argv, words in (
            (["sudo", "cp", "-p", "--update=none-fail", m.main_conf, dst], f"cp: não substituindo '{dst}'\n"),
            (
                ["sudo", "env", "LC_ALL=C", "cp", "-p", "--update=none-fail", m.main_conf, dst],
                f"cp: not replacing '{dst}'\n",
            ),
        ):
            r = runner.run(argv, locale_c=True)
            self.assertEqual((r.returncode, r.stderr), (1, words), argv)

    def test_a_copy_that_fails_stops_the_run(self) -> None:
        m = self.machine()
        m.put("fail-sudo-cp", "3")
        rc, out, err = self.run_one(m, "apply", "--yes", "--no-restart")
        self.assertEqual(rc, 3, out + err)
        self.assertTrue(
            err.endswith(
                "cst-t: stopped: a command failed (exit 3). Backup of main.conf: none taken. Undo: cst-t revert\n"
            ),
            err,
        )
        self.assertEqual(len(self.sudo_cps(m)), 1)
        self.assertEqual(self.text(m.main_conf), VENDOR)

    def test_a_copy_that_fails_half_way_stops_the_run(self) -> None:
        # A full disk: cp leaves the start of the copy under the name it was given. That name is
        # there now, but no other run took it: the run stops, and tries no other name.
        m = self.machine()
        base = self.base(m)
        m.flag("partial-backup")
        rc, out, err = self.run_one(m, "apply", "--yes", "--no-restart")
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"cp: error writing '{base}': No space left on device\n", err)
        self.assertTrue(err.endswith("Backup of main.conf: none taken. Undo: cst-t revert\n"), err)
        self.assertEqual(self.sudo_cps(m), [f"sudo env LC_ALL=C cp -p --update=none-fail {m.main_conf} {base}"])
        self.assertFalse(os.path.exists(base + "-1"))
        self.assertEqual(self.text(m.main_conf), VENDOR)

    def test_no_free_name_stops_the_run(self) -> None:
        # The second's name and -1 to -99 are taken: apply gives up before main.conf is written.
        m = self.machine()
        base = self.base(m)
        planted = [base, *(f"{base}-{n}" for n in range(1, 100))]
        for path in planted:
            m.put(path, "taken\n")
            os.utime(path, (2_000_000_000, 2_000_000_000))
        rc, out, err = self.run_one(m, "apply", "--yes", "--no-restart")
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"cst-t: ERROR: no free name for a backup of {m.main_conf} ({base} and 99 more are taken)\n", err)
        self.assertEqual(self.sudo_cps(m), [])
        calls = m.read("log").splitlines()
        self.assertEqual([c for c in calls if c.startswith(("sudo install", "sudo mv"))], [])
        self.assertFalse(os.path.exists(f"{base}-100"))
        self.assertEqual(self.text(m.main_conf), VENDOR)
        self.assertFalse(os.path.exists(m.pending))
        for path in planted:
            self.assertEqual(self.text(path), "taken\n", path)

    def test_the_oldest_backup(self) -> None:
        m = self.machine()
        base = self.base(m)
        keys = f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n"
        for name, text in ((f"{base}-10", VENDOR), (f"{base}-2", VENDOR)):
            m.put(name, text)
            os.utime(name, (1_000_000, 1_000_000))
        m.put(m.main_conf, keys)
        rc, out, _ = self.run_one(m, "revert", "--check")
        self.assertEqual(rc, 10, out)
        self.assertIn(f"would set the two keys as in {base}-2:\n", out)  # before, -10 came first (as text)
        os.utime(f"{base}-10", (999_999, 999_999))  # older: it wins, whatever the names
        rc, out, _ = self.run_one(m, "revert", "--check")
        self.assertIn(f"would set the two keys as in {base}-10:\n", out)

    def test_backup_name_taken(self) -> None:
        self.assertTrue(backup_name_taken("cp: not replacing '/e/main.conf.bak-1'\n"))
        self.assertTrue(backup_name_taken("cp: cannot create regular file '/e/x': File exists\n"))
        self.assertFalse(backup_name_taken("cp: error writing '/e/x': No space left on device\n"))
        self.assertFalse(backup_name_taken("sudo: a password is required\n"))
        self.assertFalse(backup_name_taken(""))

    def test_backup_order(self) -> None:
        stamp = "/e/main.conf.bak-20261124-101010"
        self.assertEqual(backup_order(5.9, stamp), (5, stamp, 0, stamp))
        self.assertEqual(backup_order(5, stamp + "-7"), (5, stamp, 7, stamp + "-7"))
        names = [stamp + "-10", stamp + "-2", stamp, stamp + "-1"]
        self.assertEqual(
            sorted(names, key=lambda n: backup_order(5, n)), [stamp, stamp + "-1", stamp + "-2", stamp + "-10"]
        )
        # Every other name sorts as before (time, then name): the old script's, another .bak-*.
        old = ["/e/main.conf.bak-x", "/e/main.conf.bak-20240101-000000", "/e/main.conf.bak-20250101-000000-x"]
        self.assertEqual(sorted(old, key=lambda n: backup_order(5, n)), sorted(old))
        self.assertEqual(backup_order(5, "/e/main.conf.bak-x"), (5, "/e/main.conf.bak-x", 0, "/e/main.conf.bak-x"))
        # A bare stamp is never read as a shorter stamp and a number; a digit that is not ASCII is no number.
        self.assertEqual(backup_order(1, "/e/main.conf.bak-20261124")[2], 0)
        self.assertEqual(backup_order(1, stamp + "-\u0663")[1:3], (stamp + "-\u0663", 0))
        self.assertLess(backup_order(1, stamp + "-99"), backup_order(2, stamp))


class SavingHost(LocalBluetoothHostAdapter):
    """The host adapter with each save of the record of restarts owed noted (the units it was given);
    fail: each save raises OSError instead, as a ~/.local/state that cannot be written does."""

    def __init__(self, *, fail: bool = False) -> None:
        self.saves: list[tuple[str, ...]] = []
        self.fail = fail

    @override
    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        if save:
            self.saves.append(tuple(skipped))
            if self.fail:
                raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        return super().restart_pending(path, ids, skipped, save=save)


class Stopped(BaseException):
    """The run stopped at a save of the record, as a SIGTERM would stop it."""


class StagedHost(SavingHost):
    """SavingHost with a script for its saves (numbered from 0): one in fail_at raises OSError (a
    ~/.local/state that cannot be written that once); after(n) runs once the n-th save is written (a
    service that restarts by itself then); stop_when() true at a save stops the run there (Stopped),
    before it is noted or written."""

    def __init__(
        self,
        *,
        fail_at: frozenset[int] = frozenset(),
        after: Callable[[int], None] = lambda _n: None,
        stop_when: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.after = after
        self.stop_when = stop_when

    @override
    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        if not save:
            return super().restart_pending(path, ids, skipped, save=save)
        if self.stop_when():
            raise Stopped
        n = len(self.saves)
        self.fail = n in self.fail_at
        out = super().restart_pending(path, ids, skipped, save=save)
        self.after(n)
        return out


class OwedBeforeTheWriteUnitTest(InProcessCase):
    """The record of restarts owed is written before each write that needs a restart, never for a
    renewal of comments; a record that cannot be written is warned of once, and the run goes on."""

    def run_with(self, m: Machine, host: SavingHost, *args: str) -> tuple[int, str, str]:
        log = os.path.join(m.state, "log")
        if os.path.exists(log):
            os.unlink(log)
        run, console, _ = change(m, *args, doubles=SetupDoubles(host=host, now=self.NOW))
        rc = run.run_command()
        run.cleanup()
        assert isinstance(rc, int), rc
        return rc, console.stdout, console.stderr

    def test_a_renewal_of_comments_owes_nothing(self) -> None:
        m = self.machine()
        self.run_one(m, "apply", "--yes")
        for path in (m.wp_conf, m.le_conf):
            m.put(path, self.text(path).replace("# managed-by", "# an older comment\n# managed-by", 1))
        host = SavingHost()
        rc, out, err = self.run_with(m, host, "apply", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"adopted:   {m.wp_conf} (comments renewed, settings unchanged: no restart)\n", out)
        self.assertIn(f"adopted:   {m.le_conf} (comments renewed, no restart)\n", out)
        self.assertIn("== restarts\nnone needed\n", out)
        self.assertEqual(host.saves, [])
        shows = [c for c in m.read("log").splitlines() if c.startswith("systemctl") and "InvocationID" in c]
        self.assertEqual(len(shows), 2, shows)  # plan's look at each service, no other
        self.assertFalse(os.path.exists(m.pending))

    def test_a_record_it_cannot_write(self) -> None:
        m = self.machine()
        host = SavingHost(fail=True)
        rc, out, err = self.run_with(m, host, "apply", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(err.count("WARNING: could not record the restarts still owed"), 1, err)
        self.assertIn(f"KernelExperimental = {ISO}", self.text(m.main_conf))
        calls = m.read("log").splitlines()
        self.assertIn("sudo systemctl restart bluetooth", calls)
        self.assertIn("systemctl --user restart wireplumber", calls)
        # Each write tries again (main.conf; the rule; the drop-in) with every unit owed so far, then
        # the restarts', then the clearing one.
        bt, wp = "bluetooth.service", "wireplumber.service"
        self.assertEqual(host.saves, [(bt, wp), (bt, wp), (bt, wp), (wp, bt), ()])

    def stopped_apply(self, m: Machine, host: SavingHost) -> None:
        """apply --yes, stopped by host (Stopped) at a save of the record."""
        log = os.path.join(m.state, "log")
        if os.path.exists(log):
            os.unlink(log)
        run, _console, _ = change(m, "apply", "--yes", doubles=SetupDoubles(host=host, now=self.NOW))
        self.addCleanup(run.cleanup)
        with self.assertRaises(Stopped):
            run.run_command()

    def test_a_failed_save_is_in_the_next_one(self) -> None:
        # The save before main.conf's write fails once; the one before the rule's write still owes
        # bluetoothd. The run stops at the restarts' save. Before, that save owed WirePlumber alone,
        # and bluetoothd stayed on the old main.conf with nothing owing its restart.
        m = self.machine()
        host = StagedHost(fail_at=frozenset({0}), stop_when=lambda: os.path.exists(m.le_conf))
        self.stopped_apply(m, host)
        bt, wp = "bluetooth.service", "wireplumber.service"
        self.assertEqual(host.saves[:2], [(bt, wp), (bt, wp)])
        self.assertIn(f"KernelExperimental = {ISO}", self.text(m.main_conf))
        self.assertEqual([c for c in m.read("log").splitlines() if " restart " in f" {c} "], [])
        self.assertEqual(
            parse_pending(self.text(m.pending)),
            {bt: m.unit_inv("system", "bluetooth"), wp: m.unit_inv("user", "wireplumber")},
        )

    def test_a_service_that_restarted_since_an_earlier_save(self) -> None:
        # WirePlumber restarts by itself right after the save before main.conf's write (it reads the
        # old rule); the save before the rule's write owes it again, under its new invocation. The run
        # stops at the restarts' save. Before, that save was skipped (the record owed WirePlumber
        # already, under the old invocation), so nothing owed its restart after the stop.
        m = self.machine()

        def restarted_by_itself(n: int) -> None:
            if n == 0:
                m.unit("user", "wireplumber", inv="wp-by-itself")

        host = StagedHost(after=restarted_by_itself, stop_when=lambda: os.path.exists(m.le_conf))
        self.stopped_apply(m, host)
        self.assertEqual(parse_pending(self.text(m.pending)).get("wireplumber.service"), "wp-by-itself")
        rc, out, _ = self.run_one(m, "plan")
        self.assertEqual(rc, 10, out)
        # main.conf is written already: bluetoothd's row names the owed restart, WirePlumber's the note.
        self.assertIn("still owed: an earlier run did not restart it; apply asks first", out)
        self.assertEqual(out.count("(still owed: an earlier run did not restart it)"), 1, out)


class SetupModelParityTest(unittest.TestCase):
    """SetupModel (setup_fakes) answers apply and revert as FAKE does: the same command lines in the
    same order, the same output, the same files, from two start states."""

    def machine(self) -> Machine:
        m = Machine(self)
        m.put(m.main_conf, "[General]\n#Experimental = false\n")
        m.services()
        m.adapter()
        m.flag("iso-socket")
        m.put("wp-roles", "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n")
        return m

    def runs(self, m: Machine, *, model: bool, lang: str = "") -> list[tuple[int | object, str, str, list[str]]]:
        """apply --yes, then revert --yes: (exit, stdout, stderr, command lines) of each, as the test
        sees them; lang: the LANG the commands get."""
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{m.bin}:/usr/bin:/bin"}))
        out: list[tuple[int | object, str, str, list[str]]] = []
        for args in (("apply", "--yes"), ("revert", "--yes")):
            if model:
                twin = SetupModel(m, lang=lang)
                run, console, _ = change(m, *args, doubles=SetupDoubles(runner=twin))
                rc = run.run_command()
                calls = twin.calls
            else:
                real = SubprocessCommandRunner(
                    HostExecutionContext(exists=lambda _p: True),
                    base_env=m.env(container=True, extra={"LANG": lang} if lang else None),
                )
                with mock.patch.object(real, "run", wraps=real.run) as spy:
                    run, console, _ = change(m, *args, doubles=SetupDoubles(runner=real))
                    rc = run.run_command()
                calls = [list(c.args[0]) for c in spy.call_args_list]
            run.cleanup()
            out.append((rc, *self.plain(m, console.stdout, console.stderr, calls)))
        return out

    @staticmethod
    def plain(m: Machine, stdout: str, stderr: str, calls: list[list[str]]) -> tuple[str, str, list[str]]:
        """The machine's folder and mktemp's file named alike on both sides."""

        def norm(text: str) -> str:
            return re.sub(r"\S*/tmp\.[A-Za-z0-9_]+", "<mktemp>", text).replace(m.tmp, "<machine>")

        return norm(stdout), norm(stderr), [norm(" ".join(c)) for c in calls]

    def files(self, m: Machine) -> dict[str, str]:
        """The files of the home, the root and the state folder: the units' .inv, .active and the
        counter the fault tests judge by are FAKE's state too. Only FAKE's logs of what it was asked
        (log, env-log) are left out: the model keeps its calls in a list."""
        out: dict[str, str] = {}
        logs = {os.path.join(m.state, "log"), os.path.join(m.state, "env-log")}
        for top in (m.home, m.root, m.state):
            for d, _dirs, names in os.walk(top):
                for n in names:
                    p = os.path.join(d, n)
                    if p in logs:
                        continue
                    with open(p, encoding="utf-8", errors="replace") as f:
                        out[os.path.relpath(p, m.tmp)] = f.read()
        return out

    def test_a_raced_backup_name_as_fake_does(self) -> None:
        fake, model = self.machine(), self.machine()
        for m in (fake, model):
            m.flag("race-backup")
            os.utime(m.main_conf, (1_000_000, 1_000_000))  # its backup is the oldest, whatever the clock
        self.assertEqual(self.runs(model, model=True), self.runs(fake, model=False))
        self.assertEqual(self.files(model), self.files(fake))
        self.assertIn("another run's backup\n", self.files(model).values())

    def test_a_raced_backup_name_in_another_language_as_fake_does(self) -> None:
        # A sudoers that keeps no LC_ALL and a pt_BR session: both read cp's words in the C locale.
        fake, model = self.machine(), self.machine()
        for m in (fake, model):
            m.flag("race-backup")
            m.flag("sudo-keeps-no-lc-all")
            os.utime(m.main_conf, (1_000_000, 1_000_000))
        on_model = self.runs(model, model=True, lang="pt_BR.UTF-8")
        self.assertEqual(on_model, self.runs(fake, model=False, lang="pt_BR.UTF-8"))
        self.assertEqual(self.files(model), self.files(fake))
        self.assertEqual(on_model[0][0], 0, on_model[0])

    def test_apply_then_revert_as_fake_does(self) -> None:
        fake, model = self.machine(), self.machine()
        on_fake = self.runs(fake, model=False)
        on_model = self.runs(model, model=True)
        for (rc_f, out_f, err_f, calls_f), (rc_m, out_m, err_m, calls_m) in zip(on_fake, on_model, strict=True):
            self.assertEqual(calls_m, calls_f)
            self.assertEqual((rc_m, out_m, err_m), (rc_f, out_f, err_f))
        self.assertEqual(self.files(model), self.files(fake))
        self.assertIn("restart:   bluetooth.service", on_model[0][1])
        self.assertIn("restart:   bluetooth.service", on_model[1][1])


if __name__ == "__main__":
    unittest.main()
