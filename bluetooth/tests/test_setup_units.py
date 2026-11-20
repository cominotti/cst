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
import shutil
import time
import unittest
from unittest import mock

from bluetooth.adapters import LocalBluetoothHostAdapter
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
    bap_state,
    is_running,
    parse_command_line,
    resolve,
)
from bluetooth.tests.setup_fakes import DEV, ISO, MAC, REPO, Machine
from bluetooth.tests.test_ldac_watch import cards, pw_dump
from cst_core.domain.files import OwnedState
from cst_core.adapters import SystemctlAdapter
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.ports import SystemctlHost

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


def change(
    m: Machine,
    *args: str,
    settings: dict[str, str] | None = None,
    show_timeout: float | None = None,
    container: bool = True,
) -> tuple[Change, Console, Clock]:
    """The use cases over the fake machine, as inside a toolbox for the host commands (so the
    kernel ISO probe is the fake's), whatever the command; show_timeout: systemctl show's limit
    (cst_core's 20 s when None); container=False: the program believes it runs on the host (its
    commands still go to the fake)."""
    paths = Paths(
        home=m.home, config=f"{m.home}/.config", etc=f"{m.root}/etc", usr_share=f"{m.root}/usr/share", checkout=REPO
    )
    opts = parse_command_line(list(args), "cst-t", "help\n")
    assert isinstance(opts, Options)
    cfg = resolve(opts, {"DEVICE": MAC, **(settings or {})}, list(settings or {}), paths, "cst-t")
    assert not isinstance(cfg, Exit)
    console, clock = Console(), Clock()
    env = m.env(container=True)
    runner = SubprocessCommandRunner(HostExecutionContext(exists=lambda _path: True), base_env=env)
    machine = SetupMachine(
        runner=runner,
        systemd=SystemctlAdapter(runner, timeout=show_timeout),
        user_systemd_host=SystemctlAdapter(runner, user=True, on_host=SystemctlHost.EVERY),
        user_systemd=SystemctlAdapter(runner, user=True, on_host=SystemctlHost.LOCAL),
        clock=clock,
        console=console,
        files=LocalSetupFiles(),
        host=LocalBluetoothHostAdapter(),
        container=container,
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


if __name__ == "__main__":
    unittest.main()
