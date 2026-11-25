# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests of `cst bluetooth` (bluetooth-setup.sh): the whole program, run
as a user runs it, on the fake machine of setup_fakes.py (stub commands on
PATH, a scratch HOME and a scratch host root). They pin what a user or a
caller sees: the output, the exit status, the files written and the commands
run, in order.

Run from the repository root:
    invowk cmd test module bluetooth.tests.test_setup
"""

from collections.abc import Sequence
import hashlib
import json
import os
import pwd
import re
import signal
import stat
import time
from typing import Final
import unittest

from bluetooth.setup.domain import senders_of, watcher_prog_state
from bluetooth.tests.setup_fakes import DEV, ISO, MAC, SETUP, Machine
from cst_core.domain.files import OwnedState
from bluetooth.tests.test_bluetooth import JOURNAL, OLD_WP, VENDOR
from bluetooth.tests.test_ldac_watch import cards, pw_dump
from tests.test_module_routing import userns_root

GENERAL = "[General]\n#Experimental = false\n"


class SeamTest(unittest.TestCase):
    """CST_BT_CONTAINER and CST_BT_ROOT: the tests' host root and toolbox, instead of the real ones."""

    def test_the_host_root_and_the_toolbox_come_from_the_overrides(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
        m.services()
        for container in (False, True):
            r = m.run("plan", container=container)
            self.assertEqual(r.returncode, 10, r.text)
            self.assertIn(f"host /etc in {m.root}/etc\n", r.stdout)
            self.assertIn(f"    --- {m.main_conf}\t", r.stdout)
            hosted = [c for c in r.calls if c.startswith("flatpak-spawn --host systemctl")]
            self.assertEqual(bool(hosted), container, r.calls)
        # A toolbox refuses apply; the host runs it (and stops at the missing session here).
        m.flag("no-session")
        r = m.run("apply", container=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("must run on the host, not inside a toolbox/container", r.stderr)
        r = m.run("apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no systemd --user session", r.stderr)
        with open(m.main_conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), GENERAL)
        self.assertNotIn(ISO, GENERAL)


class StoppedTest(unittest.TestCase):
    """apply and revert stop at the first command that fails, name its exit status and how to undo."""

    def test_a_failed_command_stops_apply_with_its_exit_status(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
        m.services()
        m.put("fail-sudo-cp", "3")
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, 3, r.text)
        self.assertTrue(
            r.stderr.endswith(
                f"{SETUP}: stopped: a command failed (exit 3). Backup of main.conf: none taken. Undo: {SETUP} revert\n"
            ),
            r.stderr,
        )
        m.flag("fail-sudo-cp", on=False)
        m.put("fail-sudo-install", "4")
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, 4, r.text)
        self.assertRegex(
            r.stderr,
            r"stopped: a command failed \(exit 4\)\. Backup of main\.conf: \S+/main\.conf\.bak-\d{8}-\d{6}\. Undo: ",
        )
        with open(m.main_conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), GENERAL)

    def test_a_failure_inside_a_nested_step_is_named_once(self) -> None:
        # The rule's folder cannot be written: the step fails inside a $(...) of a function.
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
        m.services()
        os.makedirs(m.wp_dir)
        os.chmod(m.wp_dir, 0o555)
        self.addCleanup(os.chmod, m.wp_dir, 0o755)
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, 1, r.text)
        stops = [line for line in r.stderr.splitlines() if ": stopped: " in line]
        self.assertEqual(len(stops), 1, r.stderr)
        self.assertRegex(
            stops[0],
            r"stopped: a command failed \(exit 1\)\. Backup of main\.conf: \S+/main\.conf\.bak-\d{8}-\d{6}\. Undo: ",
        )
        self.assertTrue(r.stderr.endswith(stops[0] + "\n"), r.stderr)
        self.assertFalse(os.path.exists(m.wp_conf))

    def test_a_settings_file_it_cannot_read_stops_the_run(self) -> None:
        # Read twice (the settings, then the keys it sets): each read names it, the second stops.
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
        m.services()
        m.user_settings(f"DEVICE={MAC}\nLE_AUDIO=on\n")
        os.chmod(m.settings, 0)
        line = f"bt-helper: [Errno 13] Permission denied: '{m.settings}'\n"
        for args in (("plan",), ("le-audio", "off", "--yes")):
            r = m.run(*args)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", line + line), args)

    def test_a_rule_file_it_cannot_read_stops_before_any_change(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
        m.user_settings(f"DEVICE={MAC}\n")
        m.services()
        os.makedirs(m.wp_dir)
        with open(m.wp_conf, "wb") as f:
            f.write(b"# \xff\n")
        bad = "bt-helper: 'utf-8' codec can't decode byte 0xff in position 2: invalid start byte\n"
        for cmd in ("plan", "status"):
            r = m.run(cmd)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", bad), cmd)
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, 1, r.text)
        self.assertEqual(
            r.stderr,
            f"{bad}{SETUP}: stopped: a command failed (exit 1). Backup of main.conf: none taken. Undo: {SETUP} revert\n",
        )
        with open(m.main_conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), GENERAL)
        with open(m.wp_conf, "rb") as f:
            self.assertEqual(f.read(), b"# \xff\n")

    def test_a_command_killed_by_a_signal_gives_128_plus_its_number(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
        m.services()
        m.put("fail-sudo-cp", "TERM")
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, 143, r.text)
        self.assertTrue(
            r.stderr.endswith(
                f"{SETUP}: stopped: a command failed (exit 143). Backup of main.conf: none taken. Undo: {SETUP} revert\n"
            ),
            r.stderr,
        )


class StepDownTest(unittest.TestCase):
    """Run as root (through sudo), the program runs again as SUDO_USER without the tests' overrides."""

    def test_the_test_overrides_do_not_reach_the_run_as_the_user(self) -> None:
        # sudo -E or a root shell could pass on CST_BT_ROOT, CST_BT_CONTAINER (Machine sets
        # both) and CST_BT_LIBWP (the libwireplumber that bt-helper wp-roles loads).
        if not userns_root():
            self.skipTest("needs an unprivileged user namespace (unshare -r); tier 1 test_96 runs it as root")
        m = Machine(self)
        me = pwd.getpwuid(os.getuid())
        r = m.run(
            "plan",
            "--device",
            "AA:BB:CC:DD:EE:FF",
            root=True,
            env={"SUDO_USER": me.pw_name, "CST_BT_LIBWP": "/nowhere/libwp.so"},
        )
        self.assertEqual(r.returncode, 0, r.text)
        session = (
            f"HOME={me.pw_dir} USER={me.pw_name} LOGNAME={me.pw_name} XDG_RUNTIME_DIR=/run/user/{me.pw_uid} "
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{me.pw_uid}/bus"
        )
        unset = "-u XDG_CONFIG_HOME -u CST_BT_ROOT -u CST_BT_CONTAINER -u CST_BT_LIBWP -u SUDO_USER"
        self.assertEqual(
            r.calls,
            [
                f"runuser -u {me.pw_name} -- env {unset} {session} {os.path.realpath(SETUP)} plan --device AA:BB:CC:DD:EE:FF"
            ],
        )


DONE = f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n"
CLASSIC = "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n"
BAP_REGISTERED = "Endpoint registered: sender=:1.7 path=/MediaEndpointLE/BAPSink/lc3\n"
HELP_SHA256 = "024e16e4bf3bf117be2240efc15cb17a6f007bcaae54113ab9ab55d918420a08"
USAGE_SHA256 = "9dd2a72a99899fde725a9245c38af75e42d2a058658c2e7d3757c2802563a128"
LW = "cst-bt-ldac48.service"
# plan's reasons for bluetoothd's restart, and the note under a row whose restart the record owes too.
BT_MAIN_CONF_ROW = "main.conf changes: apply asks first (Bluetooth keyboards, mice and headsets drop for seconds)"
BT_OWED_ROW = "still owed: an earlier run did not restart it; apply asks first (Bluetooth keyboards, mice and headsets drop for seconds)"
OWED = "(still owed: an earlier run did not restart it)"
# The rows of an older CST's copy of the watcher's unit (where the user manager does not read units).
LW_STALE = "an older CST's copy, where your user manager does not read units: apply removes it"
LW_STALE_FOREIGN = "recorded as CST's, not written by CST: left alone; apply drops its record line"


def row(word: str, label: str, note: str = "") -> str:
    """One line of plan's and status's tables, as the program prints it."""
    return f"  {word:<9} {label:<52} {note}\n"


def wp_label(path: str) -> str:
    return "wireplumber.conf.d/" + os.path.basename(path)


class SetupCase(unittest.TestCase):
    """A test with a fake machine: main.conf, the user's settings, bluetoothd and WirePlumber running."""

    def machine(
        self, main_conf: str = VENDOR, settings: str | None = None, *, services: bool = True, headset: bool = True
    ) -> Machine:
        """headset: the user's settings name the headset MAC (CST ships none), first in the file."""
        m = Machine(self)
        m.put(m.main_conf, main_conf)
        if headset and "DEVICE" not in (settings or ""):
            settings = f"DEVICE={MAC}\n" + (settings or "")
        if settings is not None:
            m.user_settings(settings)
        if services:
            m.services()
        return m

    def text(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def after(self, calls: Sequence[str], first: str) -> list[str]:
        """The calls after the first one that starts with first."""
        for i, c in enumerate(calls):
            if c.startswith(first):
                return list(calls[i + 1 :])
        return self.fail(f"no call {first!r} in {calls}")

    def order(self, calls: Sequence[str], *wanted: str) -> None:
        """The calls that are one of wanted (or it and more words) come first in that order."""
        seen = [w for c in calls for w in wanted if c == w or c.startswith(w + " ")]
        firsts = [w for i, w in enumerate(seen) if w not in seen[:i]]
        self.assertEqual(firsts, list(wanted), calls)


# The LDAC rule main's (535aea1) apply wrote, for an invented headset (MAC): what a machine set up with
# the old default headset (DEVICE in bluetooth/bluetooth.conf) has, next to a settings file without DEVICE.
OLD_RULE = os.path.join(os.path.dirname(SETUP), "tests", "fixtures", "legacy", f"50-bluez-{DEV}.conf@535aea1")
HINT = "no headset set: set DEVICE=AA:BB:CC:DD:EE:FF in ~/.config/cst/bluetooth.conf"


class HeadsetTest(SetupCase):
    """CST ships no headset (DEVICE in bluetooth/bluetooth.conf is empty). The headset is --device,
    else DEVICE in the user's settings, else (no DEVICE line there) the one LDAC rule CST wrote:
    what a machine set up with the old default has; apply, and revert before it removes the rule,
    save it as DEVICE. With none, plan, apply, status and revert skip the headset's parts."""

    def old_rule(self, m: Machine, name: str | None = None) -> str:
        with open(OLD_RULE, encoding="utf-8") as f:
            return m.put(os.path.join(m.wp_dir, name) if name is not None else m.wp_conf, f.read())

    def plan(self, m: Machine, *args: str) -> str:
        r = m.run("plan", *args, env={"CST_PROG": "cst-t"})
        self.assertIn(r.returncode, (0, 10), r.text)
        return r.stdout

    def test_only_rules_cst_wrote_name_a_headset(self) -> None:
        m = self.machine(DONE, headset=False)
        self.old_rule(m)
        self.old_rule(m, f"50-bluez-{DEV}.conf.bak")  # a backup: WirePlumber does not read it
        m.put(os.path.join(m.wp_dir, "50-bluez-11_22_33_44_55_66.conf"), "monitor.bluez.rules = []\n")
        self.old_rule(m, "50-bluez-jbl.conf")  # not an address
        self.assertIn(f"Headset {MAC} (from CST's LDAC rule; apply saves it as DEVICE in ", self.plan(m))
        # A second rule (the old script's marker counts): no headset is taken.
        m.put(os.path.join(m.wp_dir, "50-bluez-AA_BB_CC_DD_EE_FF.conf"), f"# {OLD_WP}\n")
        out = self.plan(m)
        self.assertIn("Headset none (no DEVICE set)", out)
        self.assertIn(
            row(
                "skip",
                "the headset's LDAC rule",
                f"no headset set, and CST has LDAC rules for several ({MAC} AA:BB:CC:DD:EE:FF): "
                "set DEVICE=<one of them> in ~/.config/cst/bluetooth.conf, or use --device MAC",
            ),
            out,
        )

    def test_the_headset_of_an_older_setup_is_kept_and_saved(self) -> None:
        # The upgrade from main's install: its rule, and a settings file without DEVICE.
        settings = "# CST bluetooth settings (override bluetooth/bluetooth.conf in the repo)\nLE_AUDIO=off\n"
        m = self.machine(DONE, settings, headset=False)
        rule = self.old_rule(m)
        out = self.plan(m)
        self.assertIn(
            f"Headset {MAC} (from CST's LDAC rule; apply saves it as DEVICE in ~/.config/cst/bluetooth.conf)", out
        )
        self.assertIn(
            row(
                "set",
                "~/.config/cst/bluetooth.conf",
                f"DEVICE={MAC}: the headset of CST's LDAC rule (CST no longer ships a default headset)",
            ),
            out,
        )
        self.assertIn(row("ok", wp_label(rule), f"up to date (LDAC auto for {MAC})"), out)
        self.assertIn("\nApply with: cst-t apply\n", out)
        self.assertEqual(self.text(m.settings), settings, "plan changed the settings")
        r = m.run("apply", "--yes", "--no-restart")
        self.assertIn(f"written:   ~/.config/cst/bluetooth.conf: DEVICE={MAC}\n", r.stdout, r.text)
        self.assertIn(f"unchanged: {rule}\n", r.stdout)
        self.assertEqual(self.text(m.settings), settings + f"DEVICE={MAC}\n")
        with open(OLD_RULE, encoding="utf-8") as f:
            self.assertEqual(self.text(rule), f.read())
        out = self.plan(m)
        self.assertIn(f"Headset {MAC} (settings)", out)
        self.assertNotIn("~/.config/cst/bluetooth.conf ", out)

    def test_revert_saves_the_headset_of_the_rule_it_removes(self) -> None:
        # Before any apply saved it, the rule is the only place that names the headset.
        m = self.machine(DONE, "LE_AUDIO=off\n", headset=False)
        rule = self.old_rule(m)
        r = m.run("revert", "--check")
        self.assertIn(f"would save DEVICE={MAC} in ~/.config/cst/bluetooth.conf\n", r.stdout, r.text)
        self.assertIn(f"would remove {rule}\n", r.stdout)
        self.assertEqual((self.text(m.settings), os.path.exists(rule)), ("LE_AUDIO=off\n", True))
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertLess(
            r.stdout.index(f"written:   ~/.config/cst/bluetooth.conf: DEVICE={MAC}"),
            r.stdout.index(f"removed:   {rule}"),
        )
        self.assertEqual(self.text(m.settings), f"LE_AUDIO=off\nDEVICE={MAC}\n")
        self.assertFalse(os.path.exists(rule))
        self.assertIn(f"Headset {MAC} (settings)", self.plan(m))

    def test_without_a_headset_its_parts_are_skipped(self) -> None:
        m = self.machine(settings="BEARER=bredr\nLDAC_RATE=48000\n", headset=False)
        m.adapter(listed=False)
        out = self.plan(m)
        self.assertIn("Headset none (no DEVICE set), LDAC quality auto", out)
        for line in (
            row(
                "skip",
                "the headset's LDAC rule",
                f"{HINT} (bluetoothctl devices lists the paired ones), or use --device MAC",
            ),
            row("skip", "preferred bearer", "BEARER=bredr is for a headset, and none is set: apply leaves it"),
            row("skip", "LDAC rate watcher", "off: no headset is set (LDAC_RATE=48000 needs one)"),
            row("update", "main.conf [General] Experimental", "(not set) -> true"),
        ):
            self.assertIn(line, out)
        r = m.run("apply", "--yes")
        self.assertIn(f"skipped:   {HINT}", r.stdout, r.text)
        self.assertIn("WARNING: BEARER=bredr is for a headset, and none is set; bearer not changed\n", r.stderr)
        self.assertIn("written:   " + m.main_conf, r.stdout)
        self.assertIn(
            row("skip", "headset", f"{HINT} (bluetoothctl devices lists the paired ones), or use --device MAC"),
            r.stdout,
        )
        self.assertEqual(self.text(m.settings), "BEARER=bredr\nLDAC_RATE=48000\n", "apply wrote a headset")
        self.assertFalse([n for n in os.listdir(m.wp_dir) if n.startswith("50-bluez-")], "apply wrote a rule")
        self.assertFalse(os.path.exists(m.lw_unit), "apply set up the watcher")
        self.assertFalse([c for c in r.calls if c.startswith(("bluetoothctl", "timeout"))], r.calls)
        self.assertEqual(m.run("plan").returncode, 0)
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(f"unchanged: {HINT}", r.stdout)

    def test_an_empty_device_in_your_file_means_none(self) -> None:
        m = self.machine(DONE, "DEVICE=\n", headset=False)
        rule = self.old_rule(m)
        self.assertIn("Headset none (no DEVICE set)", self.plan(m))
        self.assertEqual(m.run("revert", "--yes").returncode, 0)
        self.assertTrue(os.path.exists(rule), "revert removed the rule of a headset that is not set")
        self.assertEqual(self.text(m.settings), "DEVICE=\n")

    def test_your_device_wins_over_a_rule_and_is_not_rewritten(self) -> None:
        m = self.machine(DONE, "DEVICE=AA:BB:CC:DD:EE:FF\n", headset=False)
        rule = self.old_rule(m)
        out = self.plan(m)
        self.assertIn("Headset AA:BB:CC:DD:EE:FF (settings)", out)
        self.assertNotIn(f"DEVICE={MAC}", out)
        m.run("apply", "--yes", "--no-restart")
        self.assertEqual(self.text(m.settings), "DEVICE=AA:BB:CC:DD:EE:FF\n")
        with open(OLD_RULE, encoding="utf-8") as f:
            self.assertEqual(self.text(rule), f.read(), "apply changed the rule of another headset")

    def test_a_rule_it_cannot_read_names_no_headset(self) -> None:
        # The search for the rule's headset happens before any command runs: no traceback.
        m = self.machine(DONE, "LE_AUDIO=off\n", headset=False)
        os.makedirs(m.wp_dir)
        with open(m.wp_conf, "wb") as f:
            f.write(b"# \xff\n")
        locked = m.put(os.path.join(m.wp_dir, "50-bluez-11_22_33_44_55_66.conf"), f"# {OLD_WP}\n", mode=0)
        for args in (("plan",), ("status",), ("revert", "--check")):
            r = m.run(*args)
            self.assertNotIn("Traceback", r.stderr, args)
        self.assertIn("Headset none (no DEVICE set)", self.plan(m))
        os.chmod(locked, 0o600)

    def test_a_rule_name_cst_never_writes_names_no_headset(self) -> None:
        # CST (every version) writes the address in capitals; a lower-case name is not its file, and
        # apply would manage the capital name next to it (a second rule that revert leaves behind).
        m = self.machine(DONE, "LE_AUDIO=off\n", headset=False)
        rule = self.old_rule(m, f"50-bluez-{DEV.lower()}.conf")
        self.assertIn("Headset none (no DEVICE set)", self.plan(m))
        m.run("apply", "--yes", "--no-restart")
        self.assertEqual(sorted(n for n in os.listdir(m.wp_dir) if n.startswith("50-bluez-")), [os.path.basename(rule)])
        self.assertEqual(self.text(m.settings), "LE_AUDIO=off\n")

    def test_without_a_headset_the_oldest_rule_name_is_left_alone(self) -> None:
        # apply-le-audio.sh's first name for the rule: with a headset apply renames it (the rule
        # under the headset's name); with none there is no name to give it, so it stays.
        m = self.machine(DONE, "LE_AUDIO=off\n", headset=False)
        legacy = m.put(os.path.join(m.wp_dir, "50-jbl-live-beam-4.conf"), OLD_WP)
        self.assertNotIn("older name of the same rule", self.plan(m))
        r = m.run("apply", "--yes", "--no-restart")
        self.assertNotIn("removed:", r.stdout, r.text)
        self.assertEqual(self.text(legacy), OLD_WP)

    def test_status_says_a_running_watcher_has_no_headset(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        m.run("apply", "--no-restart")
        m.user_settings("DEVICE=\nLDAC_RATE=48000\n")
        self.assertIn(
            row(
                "warn",
                LW,
                "running although no headset is set (LDAC_RATE=48000 needs one): cst-t apply stops and removes it",
            ),
            m.run("status", env={"CST_PROG": "cst-t"}).stdout,
        )

    def test_the_option_wins_and_is_not_saved(self) -> None:
        m = self.machine(DONE, "LE_AUDIO=off\n", headset=False)
        self.old_rule(m)
        out = self.plan(m, "--device", "aa:bb:cc:dd:ee:ff")
        self.assertIn("Headset AA:BB:CC:DD:EE:FF (--device)", out)
        self.assertNotIn(row("set", "~/.config/cst/bluetooth.conf", f"DEVICE={MAC}"), out)
        r = m.run("plan", "--device", "headset")
        self.assertEqual(r.returncode, 2)
        self.assertIn("got 'headset'", r.stderr)


class CommandLineTest(SetupCase):
    """The options, their errors and the help, before anything is read from the machine."""

    def test_help(self) -> None:
        m = Machine(self)
        for args in (("help",), ("-h",), ("--help",), ("",), ()):
            r = m.run(*args, env={"CST_PROG": "cst bluetooth"})
            self.assertEqual((r.returncode, r.stderr), (0, ""), args)
            self.assertEqual(hashlib.sha256(r.stdout.encode()).hexdigest(), HELP_SHA256, r.stdout)
            self.assertTrue(
                r.stdout.startswith("\nbluetooth-setup.sh  (the " + "\u0060bluetooth\u0060 module of CST"), r.stdout
            )
            self.assertTrue(r.stdout.endswith("\nUsage: cst bluetooth <command> [options]\n"))
            self.assertEqual(r.calls, [])
        r = m.run("help")
        self.assertTrue(r.stdout.endswith(f"\nUsage: {SETUP} <command> [options]\n"))
        r = m.run("nonsense", env={"CST_PROG": "cst bluetooth"})
        self.assertEqual((r.returncode, r.stdout), (2, ""))
        self.assertEqual(hashlib.sha256(r.stderr.encode()).hexdigest(), USAGE_SHA256, r.stderr)
        self.assertTrue(r.stderr.startswith("Unknown command: nonsense\n\nbluetooth-setup.sh  (the"))

    def test_usage_errors(self) -> None:
        m = self.machine()
        cases = {
            ("plan", "--ldac-quality", "best"): "cst-t plan: the LDAC quality must be auto, hq, sq or mq (got 'best')",
            ("plan", "--bearer=usb"): "cst-t plan: the bearer must be le, bredr or last-used (got 'usb')",
            ("plan", "--bogus"): "cst-t plan: unknown option '--bogus' (see cst-t help)",
            (
                "plan",
                "--device",
                "nope",
            ): "cst-t plan: the device must be a MAC address like AA:BB:CC:DD:EE:FF (got 'nope'; set DEVICE in "
            + m.settings
            + " or use --device)",
            ("status", "--force"): "cst-t status: unknown option '--force' (see cst-t help)",
            ("apply", "--check"): "cst-t apply: unknown option '--check' (see cst-t help)",
            ("plan", "--device"): "cst-t plan: --device needs a value",
            ("plan", "--device", "--force"): "cst-t plan: --device needs a value",
            (
                "plan",
                "--le-audio",
                "maybe",
            ): "cst-t plan: LE Audio must be on or off (got 'maybe'; LE_AUDIO in the settings or --le-audio)",
            ("apply", "--le-audio"): "cst-t apply: --le-audio needs a value",
            ("revert", "--le-audio", "on"): "cst-t revert: unknown option '--le-audio' (see cst-t help)",
            (
                "plan",
                "--le-audio=yes",
            ): "cst-t plan: LE Audio must be on or off (got 'yes'; LE_AUDIO in the settings or --le-audio)",
            ("le-audio", "maybe"): "cst-t le-audio: say on or off (got 'maybe')",
            ("le-audio", "on", "--le-audio", "off"): "cst-t le-audio: unknown option '--le-audio' (see cst-t help)",
            ("le-audio", "on", "--device", MAC): "cst-t le-audio: unknown option '--device' (see cst-t help)",
            ("le-audio", "on", "--force"): "cst-t le-audio: unknown option '--force' (see cst-t help)",
            ("le-audio", "--yes", "on"): "cst-t le-audio: unknown option 'on' (see cst-t help)",
            ("plan", "stray"): "cst-t plan: unknown option 'stray' (see cst-t help)",
            (
                "plan",
                "--ldac-quality",
                "best",
                "--bearer",
                "x",
            ): "cst-t plan: the bearer must be le, bredr or last-used (got 'x')",
            (
                "plan",
                "--le-audio",
                "x",
                "--ldac-quality",
                "y",
            ): "cst-t plan: LE Audio must be on or off (got 'x'; LE_AUDIO in the settings or --le-audio)",
        }
        for args, err in cases.items():
            r = m.run(*args, env={"CST_PROG": "cst-t"})
            self.assertEqual((r.returncode, r.stdout, r.stderr), (2, "", err + "\n"), args)
            self.assertEqual(r.calls, [], args)

    def test_values_and_flags(self) -> None:
        m = self.machine(DONE, "DEVICE=aa:bb:cc:dd:ee:ff\nBEARER=bredr\n")
        r = m.run("plan", "--device", "", "--bearer", "")
        # --device "": the settings' DEVICE; --bearer "": no bearer (it overrides the settings).
        self.assertIn("Headset AA:BB:CC:DD:EE:FF (settings), LDAC quality auto", r.stdout)
        self.assertIn(row("ok", "preferred bearer", "left alone (no BEARER set)"), r.stdout)
        r = m.run("plan", "--device=11:22:33:44:55:66", "--ldac-quality=hq", "--le-audio=on")
        self.assertIn(
            "Headset 11:22:33:44:55:66 (--device), LDAC quality hq, LE Audio on (--le-audio, this run only)", r.stdout
        )
        self.assertIn("bluetoothctl info 11:22:33:44:55:66", r.calls)
        # --force=anything is --force.
        m.unit("system", "bluetooth", inv="bt-0")
        r = m.run("apply", "--force=no", "--yes", "--no-restart")
        self.assertIn(
            "Not restarted (--no-restart). To finish: sudo systemctl restart bluetooth && systemctl --user restart wireplumber",
            r.stdout,
        )

    def test_bad_settings_are_named(self) -> None:
        for text, err in (
            ("DEVICE=headset\n", "got 'headset'"),
            ("LDAC_RATE=96000\n", "LDAC_RATE must be empty (watcher off) or 48000 (got '96000' in the settings)"),
            ("LE_AUDIO=true\n", "LE Audio must be on or off (got 'true'"),
            ("BEARER=usb\n", "the bearer must be le, bredr or last-used (got 'usb')"),
        ):
            m = self.machine(settings=text)
            r = m.run("plan")
            self.assertEqual(r.returncode, 2, text)
            self.assertIn(err, r.stderr)
        m = self.machine(settings="DEVICE=headset\n")
        self.assertIn(m.settings, m.run("plan").stderr)
        # le-audio on|off fixes a broken LE_AUDIO itself.
        m = self.machine(settings="LE_AUDIO=true\n")
        r = m.run("le-audio", "off", "--no-restart")
        self.assertIn("written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=off", r.stdout)

    def test_bt_timeout(self) -> None:
        m = self.machine(DONE, "BEARER=bredr\n")
        m.put("bt-info", "\tPaired: yes\n\tPreferredBearer: bredr\n")
        for raw, via in (
            ("", "timeout 5 "),
            ("7", "timeout 7 "),
            ("08", "timeout 8 "),
            ("007", "timeout 7 "),
            ("0", "direct"),
        ):
            env = {} if raw == "" else {"BT_TIMEOUT": raw}
            r = m.run("plan", env=env)
            self.assertEqual(r.returncode, 10, r.text)
            info = r.calls.index(f"bluetoothctl info {MAC}")
            self.assertTrue(r.calls[info + 1].startswith(f"  stdin=/dev/null via={via}"), (raw, r.calls))
        for bad in ("soon", "1234567890", "-1", " 5"):
            r = m.run("plan", env={"BT_TIMEOUT": bad})
            self.assertEqual(r.returncode, 2, bad)
            self.assertEqual(r.stderr, f"BT_TIMEOUT must be a whole number of seconds (got '{bad}')\n")


class PlanTest(SetupCase):
    def test_fresh_machine(self) -> None:
        m = self.machine()
        r = m.run("plan", env={"CST_PROG": "cst-t"})
        self.assertEqual((r.returncode, r.stderr), (10, ""), r.text)
        self.assertTrue(
            r.stdout.startswith("Dry run: nothing is changed. ok = nothing to do; adopt = apply only renews")
        )
        self.assertIn(
            f"Headset {MAC} (settings), LDAC quality auto, LE Audio off (the CST default); host /etc in {m.root}/etc\n",
            r.stdout,
        )
        for line in (
            row("update", "main.conf [General] Experimental", "(not set) -> true"),
            row("update", "main.conf [General] KernelExperimental", f"(not set) -> {ISO} (ISO socket)"),
            row("write", wp_label(m.wp_conf), "missing"),
            row("write", wp_label(m.le_conf), "LE Audio off (the CST default): PipeWire will offer only A2DP and HFP"),
            row("ok", "preferred bearer", "left alone (no BEARER set)"),
            row("ok", "LDAC rate watcher", "off (LDAC_RATE empty; 48000 turns it on)"),
            row(
                "restart",
                "bluetooth.service",
                "main.conf changes: apply asks first (Bluetooth keyboards, mice and headsets drop for seconds)",
            ),
            row("restart", "wireplumber (your session)", "audio stops for a moment; A2DP is restored afterwards"),
            row("", "", "(the LE Audio change takes effect only with this restart)"),
            "\n== main.conf, as apply would write it\n",
            f"    --- {m.main_conf}\t",
            "    +KernelExperimental = " + ISO + "\n",
            "\nApply with: cst-t apply\n",
        ):
            self.assertIn(line, r.stdout)
        self.assertEqual(self.text(m.main_conf), VENDOR, "plan changed main.conf")
        self.assertFalse(os.path.exists(m.wp_dir), "plan wrote a WirePlumber file")
        self.assertEqual(self.text(m.settings), f"DEVICE={MAC}\n", "plan changed the settings")

    def test_nothing_to_do_and_the_changed_files_diffs(self) -> None:
        m = self.machine(DONE)
        m.run("apply", "--yes", "--no-restart")
        m.services()  # restarted by hand: nothing owed
        os.unlink(m.pending)
        r = m.run("plan")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertTrue(r.stdout.endswith("\nNothing to do.\n"), r.stdout)
        self.assertIn(row("ok", "main.conf [General] Experimental", "true"), r.stdout)
        self.assertIn(row("ok", wp_label(m.wp_conf), f"up to date (LDAC auto for {MAC})"), r.stdout)
        self.assertIn(
            row("ok", wp_label(m.le_conf), "LE Audio off (the CST default): PipeWire offers only A2DP and HFP"),
            r.stdout,
        )
        self.assertIn(row("ok", "bluetooth.service", "no restart needed"), r.stdout)
        # Another quality: the rule's diff, and only WirePlumber restarts.
        r = m.run("plan", "--ldac-quality", "hq")
        self.assertEqual(r.returncode, 10)
        self.assertIn(
            row("update", wp_label(m.wp_conf), "differs (apply writes LDAC hq; the old file is kept as .bak)"), r.stdout
        )
        self.assertIn(f"\n== {wp_label(m.wp_conf)}, as apply would write it\n    --- {m.wp_conf}\t", r.stdout)
        self.assertIn("    +++ /dev/fd/", r.stdout)
        self.assertIn(
            '    -        bluez5.a2dp.ldac.quality = "auto"\n    +        bluez5.a2dp.ldac.quality = "hq"\n', r.stdout
        )
        self.assertIn(row("ok", "bluetooth.service", "no restart needed"), r.stdout)
        self.assertIn(
            row("restart", "wireplumber (your session)", "audio stops for a moment; A2DP is restored afterwards"),
            r.stdout,
        )
        r = m.run("apply", "--ldac-quality", "hq", "--no-restart")
        self.assertIn(f"backup:    {m.wp_conf}.bak\n", r.stdout)
        self.assertIn(f"written:   {m.wp_conf} (LDAC hq)\n", r.stdout)
        self.assertIn('bluez5.a2dp.ldac.quality = "auto"', self.text(m.wp_conf + ".bak"))

    def test_the_old_scripts_files_are_adopted(self) -> None:
        m = self.machine(DONE)
        m.put(m.wp_conf, OLD_WP)
        m.put(os.path.join(m.wp_dir, "50-jbl-live-beam-4.conf"), OLD_WP)
        r = m.run("plan")
        self.assertIn(
            row(
                "adopt",
                wp_label(m.wp_conf),
                "up to date; written by apply-le-audio.sh or with older comments: apply only renews the comments, no restart",
            ),
            r.stdout,
        )
        self.assertIn(
            row(
                "remove",
                "wireplumber.conf.d/50-jbl-live-beam-4.conf",
                "older name of the same rule (apply-le-audio.sh)",
            ),
            r.stdout,
        )
        r = m.run("apply", "--no-restart")
        self.assertIn(f"removed:   {m.wp_dir}/50-jbl-live-beam-4.conf (older name of the same rule)\n", r.stdout)
        self.assertIn(f"adopted:   {m.wp_conf} (comments renewed, settings unchanged: no restart)\n", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(m.wp_dir, "50-jbl-live-beam-4.conf")))
        self.assertIn("# managed-by: bluetooth-setup.sh (CST)", self.text(m.wp_conf))
        self.assertEqual(stat.S_IMODE(os.stat(m.wp_conf).st_mode), 0o644)

    def test_foreign_rule_is_a_conflict_and_apply_stops(self) -> None:
        m = self.machine(DONE)
        m.put(m.wp_conf, "monitor.bluez.rules = []\n")
        r = m.run("plan")
        self.assertIn(
            row(
                "CONFLICT",
                wp_label(m.wp_conf),
                "not written by CST or the old script: apply refuses; move it away by hand",
            ),
            r.stdout,
        )
        r = m.run("apply", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            r.stderr,
            f"cst-t: ERROR: {m.wp_conf} was not written by CST or apply-le-audio.sh: move it away, then run apply again.\n",
        )
        self.assertEqual(self.text(m.main_conf), DONE)

    def test_main_conf_missing_or_a_link(self) -> None:
        m = self.machine()
        os.unlink(m.main_conf)
        r = m.run("plan", env={"CST_PROG": "cst-t"})
        self.assertEqual(
            (r.returncode, r.stderr),
            (1, f"cst-t: ERROR: {m.main_conf} does not exist (is the bluez package installed?)\n"),
        )
        real = m.put("real-main.conf", VENDOR)
        os.symlink(real, m.main_conf)
        r = m.run("status", env={"CST_PROG": "cst-t"})
        self.assertEqual(
            (r.returncode, r.stderr), (1, f"cst-t: ERROR: {m.main_conf} is a symlink: refusing to edit it\n")
        )
        r = m.run("revert", "--check", env={"CST_PROG": "cst-t"})
        self.assertEqual((r.returncode, r.stderr), (1, f"cst-t: ERROR: {m.main_conf} is missing or a symlink\n"))


class RestartTest(SetupCase):
    """apply's restarts: in order, each failure stops what depends on it; the record of restarts owed."""

    def test_restarts_in_order(self) -> None:
        m = self.machine(settings="LE_AUDIO=on\n")
        m.put("bt-journal", BAP_REGISTERED)
        m.adapter(listed=False)  # it answers the wait after the restart; the checks find none
        r = m.run("apply", "--yes")
        self.order(
            r.calls,
            "sudo env LC_ALL=C cp -p",
            "sudo install -m 0644",
            "sudo mv -f",
            "sudo systemctl restart bluetooth",
            "busctl get-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Powered",
            "systemctl --user restart wireplumber",
        )
        after = self.after(r.calls, "systemctl --user restart wireplumber")
        self.assertEqual(
            after[0], "systemctl show -p ActiveState -p InvocationID bluetooth.service", after
        )  # the BAP wait
        self.assertIn("restart:   bluetooth.service (Bluetooth connections drop for a few seconds)\n", r.stdout)
        self.assertIn("restart:   wireplumber (your session; audio stops for a moment)\n", r.stdout)
        self.assertFalse(os.path.exists(m.pending))
        self.assertNotEqual(m.unit_inv("system", "bluetooth"), "bt-0")

    def test_no_bap_wait_with_le_audio_off(self) -> None:
        m = self.machine(DONE)
        r = m.run("apply", "--yes")
        after = self.after(r.calls, "systemctl --user restart wireplumber")
        self.assertEqual(after[0], f"bluetoothctl info {MAC}", after)  # restore_a2dp, no BAP wait
        self.assertNotIn("sudo systemctl restart bluetooth", r.calls)

    def test_no_restart_hint_names_only_what_is_needed(self) -> None:
        m = self.machine(DONE)
        r = m.run("apply", "--no-restart")
        self.assertIn("Not restarted (--no-restart). To finish: systemctl --user restart wireplumber\n", r.stdout)
        # Absolute paths left out: the checkout's own path may contain "restart"; any other word still counts
        # (try-restart, reload-or-restart).
        words = " ".join(
            w for c in r.calls if not c.startswith("systemctl --user is-") for w in c.split() if not w.startswith("/")
        )
        self.assertNotIn("restart", words, r.calls)
        m = self.machine()
        r = m.run("apply", "--no-restart")
        self.assertIn(
            "Not restarted (--no-restart). To finish: sudo systemctl restart bluetooth && systemctl --user restart wireplumber\n",
            r.stdout,
        )

    def test_failed_bluetooth_restart_stops_before_wireplumber(self) -> None:
        m = self.machine()
        m.put("fail-sudo-systemctl", "1")
        r = m.run("apply", "--yes", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1, r.text)
        self.assertIn(
            "cst-t: ERROR: bluetooth.service was not restarted. To finish: sudo systemctl restart bluetooth && systemctl --user restart wireplumber\n",
            r.stderr,
        )
        self.assertRegex(
            r.stderr,
            r"cst-t: ERROR: a restart failed \(see above\)\. Backup of main\.conf: \S+\.bak-\d{8}-\d{6}\. Undo: cst-t revert\n$",
        )
        self.assertNotIn("systemctl --user restart wireplumber", r.calls)
        self.assertEqual(json.loads(self.text(m.pending)), {"bluetooth.service": "bt-0", "wireplumber.service": "wp-0"})

    def test_failed_wireplumber_restart(self) -> None:
        m = self.machine(DONE)
        m.flag("units/user-wireplumber.fail")
        r = m.run("apply", "--yes", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1)
        self.assertIn(
            "Job for wireplumber failed.\ncst-t: ERROR: wireplumber was not restarted. To finish: systemctl --user restart wireplumber\n",
            r.stderr,
        )
        self.assertTrue(
            r.stderr.endswith(
                "cst-t: ERROR: a restart failed (see above). Backup of main.conf: none taken. Undo: cst-t revert\n"
            )
        )

    def test_a_cancelled_question_keeps_both_owed(self) -> None:
        m = self.machine(settings="BEARER=bredr\n")
        m.put("bt-info", "\tPaired: yes\n")
        r = m.run("apply", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(
            "No terminal to ask on: use --yes to restart. To finish later: sudo systemctl restart bluetooth && systemctl --user restart wireplumber\n",
            r.stdout,
        )
        self.assertTrue(
            r.stdout.endswith(
                "\nNot done yet: the restarts above, the preferred bearer and the checks.\nAfter restarting, run: cst-t apply\n"
            ),
            r.stdout,
        )
        self.assertEqual(json.loads(self.text(m.pending)), {"bluetooth.service": "bt-0", "wireplumber.service": "wp-0"})
        self.assertFalse([c for c in r.calls if "restart" in c or "bearer" in c], r.calls)
        r = m.run("apply", "--no-restart", "--bearer", "")
        self.assertIn(row("", "", "(still owed: an earlier run did not restart it)"), m.run("plan").stdout)

    def test_restore_a2dp_after_the_restart(self) -> None:
        m = self.machine(DONE)
        m.put("bt-info", "\tPaired: yes\n\tBREDR.Connected: yes\n\tLE.Connected: no\n")
        m.put("pactl-cards", json.dumps(cards(active="headset-head-unit")))
        r = m.run("apply", "--yes")
        self.assertIn(f"pactl set-card-profile bluez_card.{DEV} a2dp-sink", r.calls)
        self.assertIn("restored:  the headset is back on A2DP\n", r.stdout)
        # Its microphone in use (a call): left alone.
        m.put("pactl-sources", json.dumps([{"index": 7, "name": f"bluez_input.{DEV}.0"}]))
        m.put("pactl-outputs", json.dumps([{"source": 7}]))
        r = m.run("apply", "--yes", "--force")
        self.assertIn(
            "WARNING: the headset is in headset-head-unit with its microphone in use; A2DP not restored (switch the profile after the call)\n",
            r.stderr,
        )
        self.assertNotIn(f"pactl set-card-profile bluez_card.{DEV} a2dp-sink", r.calls)
        # Already on A2DP: nothing to do.
        m.put("pactl-cards", json.dumps(cards()))
        r = m.run("apply", "--yes", "--force")
        self.assertNotIn("restored:", r.stdout)
        self.assertNotIn("WARNING: the headset", r.stderr)


class RestartOwedTest(SetupCase):
    """A restart that was not made stays owed while its unit runs with the same invocation ID."""

    def test_owed_until_apply_restarts(self) -> None:
        m = self.machine()
        r = m.run("apply", "--yes", "--no-restart")
        self.assertEqual(json.loads(self.text(m.pending)), {"bluetooth.service": "bt-0", "wireplumber.service": "wp-0"})
        self.assertEqual(stat.S_IMODE(os.stat(m.pending).st_mode), 0o644)
        r = m.run("plan")
        self.assertEqual(r.returncode, 10)
        # main.conf is already as apply wants it: only the record owes bluetoothd's restart. Before,
        # the row said "main.conf changes", with "(still owed: ...)" under it.
        self.assertIn(row("restart", "bluetooth.service", BT_OWED_ROW), r.stdout)
        self.assertNotIn("main.conf changes", r.stdout)
        self.assertEqual(r.stdout.count("(still owed: an earlier run did not restart it)"), 1)  # WirePlumber's
        r = m.run("apply", "--yes")
        self.order(r.calls, "sudo systemctl restart bluetooth", "systemctl --user restart wireplumber")
        self.assertFalse(os.path.exists(m.pending))
        r = m.run("plan")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_forced_run_that_did_not_restart(self) -> None:
        # apply --force --no-restart on a machine already applied: main.conf is not touched, yet the
        # record owes bluetoothd's restart. plan has no --force and sees only the record; before, its
        # row said "main.conf changes", which never changed.
        m = self.machine(DONE)
        m.run("apply", "--yes")
        r = m.run("apply", "--yes", "--force", "--no-restart")
        self.assertIn("Not restarted (--no-restart).", r.stdout)  # the checks find no adapter: exit 1
        self.assertEqual(json.loads(self.text(m.pending)).get("bluetooth.service"), m.unit_inv("system", "bluetooth"))
        r = m.run("plan")
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn(row("restart", "bluetooth.service", BT_OWED_ROW), r.stdout)
        self.assertNotIn("main.conf changes", r.stdout)
        self.assertNotIn("\n== main.conf, as apply would write it\n", r.stdout)

    def test_revert_that_did_not_restart(self) -> None:
        m = self.machine()
        m.run("apply", "--yes")
        r = m.run("revert", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.text)
        # plan now finds main.conf to change back (apply's keys are gone) and the record owes the
        # restart too: both reasons, the main.conf one first.
        r = m.run("plan")
        self.assertIn(row("restart", "bluetooth.service", BT_MAIN_CONF_ROW) + row("", "", OWED), r.stdout)
        self.assertNotIn(BT_OWED_ROW, r.stdout)

    def test_a_restart_by_hand_or_a_stop_ends_what_is_owed(self) -> None:
        m = self.machine()
        m.run("apply", "--yes", "--no-restart")
        m.unit("user", "wireplumber", inv="wp-by-hand")
        r = m.run("plan")
        self.assertEqual(r.stdout.count("still owed"), 1)  # bluetoothd's; WirePlumber follows it
        self.assertIn(
            row("restart", "wireplumber (your session)", "audio stops for a moment; A2DP is restored afterwards"),
            r.stdout,
        )
        m.unit("system", "bluetooth", active=False)  # stopped (it keeps its last ID): reads main.conf when it starts
        r = m.run("plan")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_status_says_what_is_owed_also_when_checks_fail(self) -> None:
        m = self.machine()
        m.run("apply", "--yes", "--no-restart")
        owed = "A restart an earlier run did not make is still owed (bluetooth.service wireplumber.service): cst-t apply makes it.\n"
        r = m.run("status", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("\n" + owed, r.stdout)
        self.assertRegex(r.stdout, r"\d check\(s\) failed, \d warning\(s\)\.\n$")
        m.adapter()
        m.flag("iso-socket")
        m.put("wp-roles", CLASSIC)
        r = m.run("status", container=True, env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(owed + "All checks passed", r.stdout)

    def test_a_unit_that_does_not_run_owes_nothing(self) -> None:
        m = self.machine()
        m.unit("system", "bluetooth", active=False)
        m.run("apply", "--no-restart")
        self.assertEqual(json.loads(self.text(m.pending)), {"wireplumber.service": "wp-0"})

    def test_an_unreadable_record_is_replaced(self) -> None:
        m = self.machine(DONE)
        os.makedirs(os.path.dirname(m.pending))
        with open(m.pending, "wb") as f:
            f.write(b"\xff{")
        self.assertNotIn("still owed", m.run("plan").stdout)
        m.run("apply", "--no-restart")
        self.assertEqual(json.loads(self.text(m.pending)), {"wireplumber.service": "wp-0"})


class OwedBeforeTheWriteTest(SetupCase):
    """A run records the restart a write needs before that write: stopped after the write, before its
    restarts, the restart stays owed; plan shows it and the next apply makes it, once."""

    BT_RESTART: Final = "sudo systemctl restart bluetooth"
    WP_RESTART: Final = "systemctl --user restart wireplumber"

    def wait_for_main_conf(self, m: Machine, *, keys: bool) -> None:
        """main.conf with (or without) the two keys apply sets: the orphaned step of a run killed by
        SIGHUP (FAKE's PARENT-HUP execs it after the signal), waited for 10 s at most."""
        for _ in range(100):
            if (f"KernelExperimental = {ISO}" in self.text(m.main_conf)) == keys:
                return
            time.sleep(0.1)
        self.fail(f"main.conf never {'had' if keys else 'lost'} the keys")

    def restarts(self, calls: Sequence[str]) -> list[str]:
        return [c for c in calls if c in (self.BT_RESTART, self.WP_RESTART)]

    def resume_twice(self, m: Machine, *restarted: str) -> None:
        """The next apply makes each restart once and forgets the record; a third has nothing to do."""
        r = m.run("apply", "--yes")
        self.assertEqual(self.restarts(r.calls), list(restarted), r.text)
        self.assertFalse(os.path.exists(m.pending), r.text)
        r = m.run("apply", "--yes")
        self.assertIn("== restarts\nnone needed\n", r.stdout)
        self.assertEqual(self.restarts(r.calls), [])
        self.assertFalse(os.path.exists(m.pending))

    def test_stopped_after_main_conf(self) -> None:
        # Before, the record was written only at the restarts: none after this stop, and plan said
        # no restart was needed while bluetoothd ran on the old main.conf.
        m = self.machine()
        m.adapter(listed=False)  # it answers the wait after the restart; the checks find none
        m.put("fail-sudo-mv", "PARENT-HUP")
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, -signal.SIGHUP, r.text)
        self.wait_for_main_conf(m, keys=True)
        m.flag("fail-sudo-mv", on=False)
        self.assertEqual(json.loads(self.text(m.pending)), {"bluetooth.service": "bt-0", "wireplumber.service": "wp-0"})
        r = m.run("plan")
        self.assertEqual(r.returncode, 10, r.text)
        # main.conf has the keys: the restart is owed, not a main.conf change (before: "main.conf changes").
        self.assertIn(row("restart", "bluetooth.service", BT_OWED_ROW), r.stdout)
        self.assertEqual(r.stdout.count("(still owed: an earlier run did not restart it)"), 1, r.stdout)
        self.resume_twice(m, self.BT_RESTART, self.WP_RESTART)

    def test_stopped_after_wireplumbers_files(self) -> None:
        # main.conf is done: only WirePlumber owes a restart. The run is stopped at the look that
        # comes right before its restarts, after the rule and the drop-in are written.
        m = self.machine(DONE)
        m.put("term-show-when", m.le_conf)
        r = m.run("apply", "--yes")
        self.assertEqual(r.returncode, -signal.SIGTERM, r.text)
        self.assertTrue(os.path.exists(m.wp_conf) and os.path.exists(m.le_conf), r.text)
        m.flag("term-show-when", on=False)
        self.assertEqual(json.loads(self.text(m.pending)), {"wireplumber.service": "wp-0"})
        r = m.run("plan")
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn(row("ok", "bluetooth.service", "no restart needed"), r.stdout)
        self.assertEqual(r.stdout.count("(still owed: an earlier run did not restart it)"), 1, r.stdout)
        self.resume_twice(m, self.WP_RESTART)

    def test_revert_stopped_after_main_conf(self) -> None:
        m = self.machine()
        m.adapter(listed=False)
        m.run("apply", "--yes")
        self.assertFalse(os.path.exists(m.pending))
        m.put("fail-sudo-mv", "PARENT-HUP")
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, -signal.SIGHUP, r.text)
        self.wait_for_main_conf(m, keys=False)
        m.flag("fail-sudo-mv", on=False)
        self.assertEqual(
            json.loads(self.text(m.pending)),
            {
                "bluetooth.service": m.unit_inv("system", "bluetooth"),
                "wireplumber.service": m.unit_inv("user", "wireplumber"),
            },
        )
        # main.conf lost apply's keys, so plan would write them again: that reason first, then the note.
        r = m.run("plan")
        self.assertIn(row("restart", "bluetooth.service", BT_MAIN_CONF_ROW) + row("", "", OWED), r.stdout)
        self.assertEqual(r.stdout.count("(still owed: an earlier run did not restart it)"), 2, r.stdout)


class RevertResumesTest(SetupCase):
    """revert makes the restarts the record owes, as apply does, also when nothing is left to revert."""

    BT_RESTART: Final = "sudo systemctl restart bluetooth"
    WP_RESTART: Final = "systemctl --user restart wireplumber"
    OWED: Final = "(still owed: an earlier run did not restart it)"

    def restarts(self, calls: Sequence[str]) -> list[str]:
        return [c for c in calls if c in (self.BT_RESTART, self.WP_RESTART)]

    def test_revert_again_after_a_stopped_one(self) -> None:
        # Before, the second revert found nothing to revert and said "none needed": bluetoothd and
        # WirePlumber stayed on the applied settings, and only an apply made the restarts.
        m = self.machine()
        m.adapter(listed=False)  # it answers the wait after the restart; the checks find none
        m.put("bt-journal", BAP_REGISTERED)  # LE Audio on again after revert: the restart waits for it
        m.run("apply", "--yes")
        m.put("fail-sudo-systemctl", "PARENT-TERM")  # stopped at bluetoothd's restart
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, -signal.SIGTERM, r.text)
        m.flag("fail-sudo-systemctl", on=False)
        owed = {
            "bluetooth.service": m.unit_inv("system", "bluetooth"),
            "wireplumber.service": m.unit_inv("user", "wireplumber"),
        }
        self.assertEqual(json.loads(self.text(m.pending)), owed)
        r = m.run("revert", "--check")
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn("would restart bluetooth.service (after asking) and wireplumber\n", r.stdout)
        self.assertIn(
            f"owed:      bluetooth.service {self.OWED}\nowed:      wireplumber.service {self.OWED}\n", r.stdout
        )
        self.assertEqual(json.loads(self.text(m.pending)), owed)
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(f"== restarts\nowed:      bluetooth.service {self.OWED}\n", r.stdout)
        self.assertEqual(self.restarts(r.calls), [self.BT_RESTART, self.WP_RESTART])
        self.assertFalse(os.path.exists(m.pending))
        r = m.run("revert", "--yes")
        self.assertTrue(r.stdout.endswith("== restarts\nnone needed\n"), r.stdout)
        self.assertEqual(self.restarts(r.calls), [])
        self.assertEqual(m.run("revert", "--check").returncode, 0)

    def test_only_wireplumber_owed(self) -> None:
        # Nothing to revert (no backup, no rule, no drop-in): WirePlumber's restart alone, with no
        # question about Bluetooth (no terminal here, and no --yes).
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)  # LE Audio on (no drop-in): the restart waits for it
        m.put(m.pending, json.dumps({"wireplumber.service": "wp-0"}))
        r = m.run("revert", "--check")
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn("would restart wireplumber\n", r.stdout)
        self.assertNotIn("bluetooth.service (after asking)", r.stdout)
        r = m.run("revert")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(f"== restarts\nowed:      wireplumber.service {self.OWED}\n", r.stdout)
        self.assertEqual(self.restarts(r.calls), [self.WP_RESTART])
        self.assertNotIn("No terminal to ask on", r.stdout)
        self.assertFalse(os.path.exists(m.pending))

    def test_no_restart_keeps_them_owed(self) -> None:
        m = self.machine(DONE)
        m.put(m.pending, json.dumps({"bluetooth.service": "bt-0", "wireplumber.service": "wp-0"}))
        r = m.run("revert", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(
            "Not restarted (--no-restart). To finish: sudo systemctl restart bluetooth && systemctl --user restart wireplumber\n",
            r.stdout,
        )
        self.assertEqual(self.restarts(r.calls), [])
        self.assertEqual(json.loads(self.text(m.pending)), {"bluetooth.service": "bt-0", "wireplumber.service": "wp-0"})


class InvocationReadTest(SetupCase):
    """One `systemctl show` per service for its state and invocation ID, read again only after CST
    restarts it (the BAP wait reads bluetoothd's at each look)."""

    BT: Final = "systemctl show -p ActiveState -p InvocationID bluetooth.service"
    WP: Final = "systemctl --user show -p ActiveState -p InvocationID wireplumber.service"
    RESTARTS: Final = ("sudo systemctl restart bluetooth", "systemctl --user restart wireplumber")

    def reads(self, calls: Sequence[str]) -> list[str]:
        """The calls that read an invocation ID or restart a service, in order."""
        return [c for c in calls if "InvocationID" in c or "is-active --quiet" in c or c in self.RESTARTS]

    def test_plan_reads_each_once(self) -> None:
        m = self.machine()
        self.assertEqual(self.reads(m.run("plan").calls), [self.BT, self.WP])

    def test_apply_reads_again_only_after_a_restart(self) -> None:
        m = self.machine(settings="LE_AUDIO=on\n")
        m.put("bt-journal", BAP_REGISTERED)
        m.adapter(listed=False)
        r = m.run("apply", "--yes")
        self.assertEqual(
            self.reads(r.calls),
            [
                self.BT,  # plan
                self.WP,
                self.BT,  # read again for the record of what is owed, before main.conf is written (either
                self.WP,  # may have restarted meanwhile)
                self.BT,  # again before the rule is written
                self.WP,
                self.BT,  # and again before the restarts
                self.WP,
                "sudo systemctl restart bluetooth",
                "systemctl --user restart wireplumber",
                self.BT,  # the BAP wait's look (registered at once); the checks reuse it
                self.WP,  # the record after the restarts; the checks' services rows reuse both
            ],
        )
        self.assertFalse(os.path.exists(m.pending))

    def test_status_reads_each_once(self) -> None:
        m = self.machine()
        m.put("bt-journal", BAP_REGISTERED)
        r = m.run("status")
        self.assertEqual([c for c in self.reads(r.calls) if "InvocationID" in c], [self.BT, self.WP])

    def test_reloading_still_runs_activating_does_not(self) -> None:
        """As `systemctl is-active`: a reloading unit still runs its invocation (its restart stays
        owed), an activating one does not run yet (it reads main.conf when it starts)."""
        m = self.machine()
        m.run("apply", "--yes", "--no-restart")
        m.unit_active_state("system", "bluetooth", "reloading")
        self.assertEqual(m.run("plan").stdout.count("still owed"), 2)
        m.unit_active_state("system", "bluetooth", "activating")
        self.assertEqual(m.run("plan").stdout.count("still owed"), 1)


class LeAudioTest(SetupCase):
    """The LE Audio drop-in (LE_AUDIO=off): written, adopted, removed, left alone; plan, status, revert."""

    def test_off_on_off(self) -> None:
        m = self.machine(DONE)
        r = m.run("apply", "--no-restart")
        self.assertIn(f"written:   {m.le_conf} (LE Audio off: PipeWire offers only A2DP and HFP)\n", r.stdout)
        self.assertIn(
            "override.monitor.bluez.properties = {\n  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n}",
            self.text(m.le_conf),
        )
        self.assertEqual(stat.S_IMODE(os.stat(m.le_conf).st_mode), 0o644)
        # apply's checks read the drop-in again after writing it.
        self.assertIn(row("ok", "LE Audio", "off (the CST default; PipeWire offers only classic profiles)"), r.stdout)
        self.assertNotIn("not in place yet", r.stdout)
        m.user_settings(f"DEVICE={MAC}\nLE_AUDIO=on\n")
        r = m.run("plan")
        self.assertIn(
            row(
                "remove",
                wp_label(m.le_conf),
                "LE Audio on (~/.config/cst/bluetooth.conf): apply removes it and restarts WirePlumber",
            ),
            r.stdout,
        )
        r = m.run("apply", "--no-restart")
        self.assertIn(f"removed:   {m.le_conf} (LE Audio on)\n", r.stdout)
        self.assertFalse(os.path.exists(m.le_conf))
        self.assertIn(
            row("ok", "LE Audio", "on (~/.config/cst/bluetooth.conf): PipeWire offers the bap-* profiles too"),
            m.run("plan").stdout,
        )
        r = m.run("plan", "--le-audio", "off")
        self.assertIn(
            row(
                "write",
                wp_label(m.le_conf),
                "LE Audio off (--le-audio, this run only): PipeWire will offer only A2DP and HFP",
            ),
            r.stdout,
        )

    def test_changed_and_foreign_drop_ins(self) -> None:
        m = self.machine(DONE)
        m.run("apply", "--no-restart")
        good = self.text(m.le_conf)
        m.put(m.le_conf, good.replace("hfp_ag ]", "hfp_ag bap_sink ]"))
        r = m.run("plan")
        self.assertIn(
            row("update", wp_label(m.le_conf), "differs (apply rewrites it: LE Audio off; restarts WirePlumber)"),
            r.stdout,
        )
        self.assertIn(f"\n== {wp_label(m.le_conf)}, as apply would write it\n", r.stdout)
        self.assertIn("    -  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag bap_sink ]\n", r.stdout)
        m.put(m.le_conf, "override.monitor.bluez.properties = { bluez5.roles = [ a2dp_sink ] }\n")
        r = m.run("plan")
        self.assertIn(
            row("CONFLICT", wp_label(m.le_conf), "not written by CST: apply refuses; move it away by hand"), r.stdout
        )
        r = m.run("apply", env={"CST_PROG": "cst-t"})
        self.assertEqual(
            (r.returncode, r.stderr),
            (1, f"cst-t: ERROR: {m.le_conf} was not written by CST: move it away, then run apply again.\n"),
        )
        r = m.run("apply", "--le-audio", "on", "--no-restart")
        self.assertIn(f"WARNING: {m.le_conf} was not written by CST: left alone (it may keep LE Audio off)\n", r.stderr)
        self.assertTrue(os.path.exists(m.le_conf))
        self.assertIn(
            row("CONFLICT", wp_label(m.le_conf), "not written by CST: left alone (it may keep LE Audio off)"),
            m.run("plan", "--le-audio", "on").stdout,
        )

    def test_older_comments_are_adopted_without_a_restart(self) -> None:
        m = self.machine(DONE)
        m.run("apply", "--no-restart")
        os.unlink(m.pending)
        m.put(m.le_conf, self.text(m.le_conf).replace("# managed-by", "# an older comment\n# managed-by", 1))
        r = m.run("plan")
        self.assertEqual(r.returncode, 10)
        self.assertIn(
            row("adopt", wp_label(m.le_conf), "LE Audio off; older comments: apply only renews them, no restart"),
            r.stdout,
        )
        self.assertIn(row("ok", "wireplumber (your session)", "no restart needed"), r.stdout)
        r = m.run("apply", "--no-restart")
        self.assertIn(f"adopted:   {m.le_conf} (comments renewed, no restart)\n", r.stdout)
        self.assertIn("== restarts\nnone needed\n", r.stdout)
        self.assertNotIn("an older comment", self.text(m.le_conf))

    def test_revert_removes_the_drop_in(self) -> None:
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)  # LE Audio on again after it: apply waits for the endpoint
        m.run("apply", "--no-restart")
        r = m.run("revert", "--check")
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn(f"would remove {m.le_conf} (LE Audio on again)\n", r.stdout)
        self.assertIn("would restart wireplumber\n", r.stdout)
        self.assertTrue(os.path.exists(m.le_conf))
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(
            f"removed:   {m.le_conf} (LE Audio on again; LE_AUDIO=off in the settings writes it again at the next apply)\n",
            r.stdout,
        )
        self.assertIn(f"removed:   {m.wp_conf}\n", r.stdout)
        self.assertIn("systemctl --user restart wireplumber", r.calls)
        self.assertNotIn("sudo systemctl restart bluetooth", r.calls)
        self.assertFalse(os.path.exists(m.le_conf))
        r = m.run("revert")
        self.assertTrue(r.stdout.endswith("== restarts\nnone needed\n"), r.stdout)

    def test_revert_leaves_foreign_files(self) -> None:
        m = self.machine(DONE)
        m.put(m.le_conf, "monitor.bluez.properties = { }\n")
        m.put(m.wp_conf, "monitor.bluez.rules = []\n")
        r = m.run("revert")
        self.assertIn(f"WARNING: {m.le_conf} was not written by CST: left alone\n", r.stderr)
        self.assertIn(f"WARNING: {m.wp_conf} was not written by CST or apply-le-audio.sh: left alone\n", r.stderr)
        self.assertTrue(os.path.exists(m.le_conf) and os.path.exists(m.wp_conf))

    def test_the_effective_roles_check(self) -> None:
        m = self.machine(DONE)
        label = "WirePlumber bluez5.roles"

        def status(*args: str) -> str:
            r = m.run("status", *args, env={"CST_PROG": "cst-t"})
            return r.stdout

        m.put("wp-roles", CLASSIC)
        out = status()
        self.assertIn(row("ok", label, "[ a2dp_sink a2dp_source hfp_hf hfp_ag ] (no LE Audio roles)"), out)
        r = m.run("status")
        self.assertIn(f"  XDG_CONFIG_HOME={m.home}/.config", r.calls)
        m.put("wp-roles", "le|[a2dp_sink, bap_sink]\n")
        self.assertIn(
            row(
                "BAD",
                label,
                "[a2dp_sink, bap_sink]: LE Audio roles in it although LE Audio is off; another WirePlumber file adds them (see the files above)",
            ),
            status(),
        )
        m.put("wp-roles", "default|\n")
        self.assertIn(
            row("note", label, "not set yet (PipeWire's defaults, with LE Audio): cst-t apply writes the CST file"),
            status(),
        )
        m.run("apply", "--no-restart")
        self.assertIn(
            row(
                "BAD",
                label,
                "not set: WirePlumber does not use the CST file, so PipeWire's defaults (with LE Audio) apply",
            ),
            status(),
        )
        self.assertIn(
            row("ok", label, "not set: the PipeWire defaults (LE Audio roles included)"), status("--le-audio", "on")
        )
        m.put("wp-roles", CLASSIC)
        self.assertIn(
            row(
                "warn",
                label,
                "[ a2dp_sink a2dp_source hfp_hf hfp_ag ]: no LE Audio roles although LE Audio is on (a WirePlumber file other than CST's sets them)",
            ),
            status("--le-audio", "on"),
        )
        m.flag("wp-roles", on=False)
        self.assertIn(row("warn", label, "could not be read (libwireplumber-0.5 not found on the host)"), status())

    def test_other_fragments_are_named(self) -> None:
        m = self.machine(DONE)
        m.put(os.path.join(m.wp_dir, "51-mine.conf"), "monitor.bluez.properties = {\n  bluez5.enable-msbc = true\n}\n")
        late = m.put(
            os.path.join(m.root, "etc/xdg/wireplumber/wireplumber.conf.d/99-late.conf"),
            "monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n",
        )
        r = m.run("plan")
        self.assertIn(
            row(
                "warn",
                "~/.config/wireplumber/wireplumber.conf.d/51-mine.conf",
                "also sets monitor.bluez.properties: while LE Audio is off the CST file replaces that section, so its settings there do not apply",
            ),
            r.stdout,
        )
        self.assertIn(
            row(
                "warn",
                late,
                "sets monitor.bluez.properties after the CST file: it can bring the LE Audio roles back (status checks them)",
            ),
            r.stdout,
        )
        self.assertNotIn("monitor.bluez.properties", m.run("plan", "--le-audio", "on").stdout)

    def test_bearer_warnings(self) -> None:
        m = self.machine(DONE)
        m.put("bt-info", "\tPaired: yes\n\tPreferredBearer: le\n")
        warn = row(
            "warn",
            "preferred bearer",
            f"{MAC} prefers LE while LE Audio is off: cst-t apply --bearer bredr (or BEARER=bredr in the settings)",
        )
        self.assertIn(warn, m.run("status", env={"CST_PROG": "cst-t"}).stdout)
        self.assertNotIn("prefers LE", m.run("status", "--le-audio", "on").stdout)
        m.put("bt-info", "\tPaired: yes\n\tPreferredBearer: bredr\n")
        self.assertNotIn("prefers LE", m.run("status").stdout)
        r = m.run("plan", "--bearer", "le")
        self.assertIn(
            row(
                "warn",
                "preferred bearer",
                "BEARER=le while LE Audio is off: connections the PC starts get no audio profile; use bredr",
            ),
            r.stdout,
        )
        self.assertIn(row("set", "preferred bearer", "-> le (only for connections the PC starts)"), r.stdout)
        m.put("bt-info", "\tPaired: no\n")
        self.assertIn(
            row("skip", "preferred bearer", f"{MAC} is not paired: apply leaves it"),
            m.run("plan", "--bearer", "le").stdout,
        )


class LinkTest(SetupCase):
    """A file of CST's that is a link is judged by what it points to; a write replaces the link, never its target."""

    def test_a_linked_rule(self) -> None:
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)
        m.run("apply", "--no-restart")
        kept = os.path.join(m.home, "dotfiles-wp.conf")
        os.rename(m.wp_conf, kept)
        os.symlink(kept, m.wp_conf)
        r = m.run("plan")
        self.assertEqual(r.stderr, "")
        self.assertIn(row("ok", wp_label(m.wp_conf), f"up to date (LDAC auto for {MAC})"), r.stdout)
        with open(kept, "a", encoding="utf-8") as f:
            f.write("monitor.bluez.properties = { bluez5.hw-volume = [ ] }\n")
        self.assertIn(
            row("update", wp_label(m.wp_conf), "differs (apply writes LDAC auto; the old file is kept as .bak)"),
            m.run("plan").stdout,
        )
        r = m.run("apply", "--no-restart")
        self.assertTrue(r.stderr.endswith("ERROR: verification found problems (see above).\n"), r.text)
        self.assertFalse(os.path.islink(m.wp_conf))
        self.assertIn("bluez5.hw-volume", self.text(kept), "the link was written through")
        os.unlink(m.wp_conf)
        os.symlink(kept, m.wp_conf)
        r = m.run("revert")
        self.assertIn(f"removed:   {m.wp_conf}\n", r.stdout)
        self.assertFalse(os.path.lexists(m.wp_conf))
        self.assertTrue(os.path.isfile(kept))

    def test_a_linked_drop_in(self) -> None:
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)
        m.run("apply", "--no-restart")
        kept = os.path.join(m.home, "dotfiles-le.conf")
        os.rename(m.le_conf, kept)
        os.symlink(kept, m.le_conf)
        r = m.run("apply", "--no-restart")
        self.assertIn("unchanged: LE Audio off\n", r.stdout)
        self.assertTrue(os.path.islink(m.le_conf))
        with open(kept, "a", encoding="utf-8") as f:
            f.write("monitor.bluez.properties = { bluez5.hw-volume = [ ] }\n")
        r = m.run("apply", "--no-restart")
        self.assertIn(f"written:   {m.le_conf} (LE Audio off: PipeWire offers only A2DP and HFP)\n", r.stdout)
        self.assertFalse(os.path.islink(m.le_conf))
        self.assertIn("bluez5.hw-volume", self.text(kept))
        os.unlink(m.le_conf)
        os.symlink(kept, m.le_conf)
        r = m.run("apply", "--le-audio", "on", "--no-restart")
        self.assertIn(f"removed:   {m.le_conf} (LE Audio on)\n", r.stdout)
        self.assertFalse(os.path.lexists(m.le_conf))
        self.assertTrue(os.path.isfile(kept))
        os.symlink(os.path.join(m.home, "gone.conf"), m.le_conf)
        self.assertIn(
            row("ok", "LE Audio", "on (--le-audio, this run only): PipeWire offers the bap-* profiles too"),
            m.run("plan", "--le-audio", "on").stdout,
        )
        self.assertIn(
            row("write", wp_label(m.le_conf), "LE Audio off (the CST default): PipeWire will offer only A2DP and HFP"),
            m.run("plan").stdout,
        )

    def test_a_link_to_something_not_ours_stays_a_conflict(self) -> None:
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)
        os.makedirs(m.wp_dir)
        foreign = m.put("foreign.conf", "monitor.bluez.properties = { }\n")
        folder = os.path.join(m.home, "a-folder")
        os.mkdir(folder)
        for target in (foreign, os.devnull, folder):
            os.symlink(target, m.le_conf)
            os.symlink(target, m.wp_conf)
            r = m.run("plan")
            self.assertEqual(r.stderr, "", target)
            self.assertIn(
                row("CONFLICT", wp_label(m.le_conf), "not written by CST: apply refuses; move it away by hand"),
                r.stdout,
            )
            self.assertIn(
                row(
                    "CONFLICT",
                    wp_label(m.wp_conf),
                    "not written by CST or the old script: apply refuses; move it away by hand",
                ),
                r.stdout,
            )
            r = m.run("revert")
            self.assertEqual(r.stderr.count("left alone"), 2, (target, r.stderr))
            self.assertNotIn("grep:", r.stderr)
            self.assertTrue(os.path.islink(m.le_conf) and os.path.islink(m.wp_conf), target)
            os.unlink(m.le_conf)
            os.unlink(m.wp_conf)


class LeAudioCommandTest(SetupCase):
    """le-audio [on|off]: the setting in the user's file, then apply; without on/off it only shows it."""

    def test_without_a_settings_folder(self) -> None:
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)
        r = m.run("le-audio", "on", "--yes", "--no-restart", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1, r.text)  # apply's checks: no adapter on this machine
        self.assertEqual(r.stderr, "cst-t: ERROR: verification found problems (see above).\n")
        self.assertTrue(
            r.stdout.startswith(
                "written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=on\nNow: cst-t apply --yes --no-restart\n== BlueZ: "
            ),
            r.stdout,
        )
        self.assertTrue(self.text(m.settings).endswith("\nLE_AUDIO=on\n"), self.text(m.settings))
        self.assertEqual(stat.S_IMODE(os.stat(m.settings).st_mode), 0o644)
        r = m.run("le-audio", "on", env={"CST_PROG": "cst-t"})
        self.assertTrue(
            r.stdout.startswith("unchanged: ~/.config/cst/bluetooth.conf already has LE_AUDIO=on\nNow: cst-t apply\n"),
            r.stdout,
        )
        # In a toolbox it only shows the setting; changing it is the host's.
        r = m.run("le-audio", "off", container=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("must run on the host", r.stderr)

    def test_keeps_the_other_lines_and_the_link(self) -> None:
        m = self.machine(DONE, headset=False)
        real = m.put("dotfiles.conf", "# mine\nLDAC_RATE=48000", mode=0o600)
        os.makedirs(os.path.dirname(m.settings))
        os.symlink(real, m.settings)
        m.run("le-audio", "on", "--no-restart")
        self.assertTrue(os.path.islink(m.settings))
        self.assertEqual(self.text(real), "# mine\nLDAC_RATE=48000\nLE_AUDIO=on\n")
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o600)
        m.run("le-audio", "off", "--no-restart")
        self.assertEqual(self.text(real), "# mine\nLDAC_RATE=48000\nLE_AUDIO=off\n")

    def test_shows_the_setting(self) -> None:
        m = self.machine(DONE, headset=False)
        r = m.run("le-audio", env={"CST_PROG": "cst-t"}, container=True)
        self.assertEqual(r.returncode, 0, r.text)
        self.assertEqual(
            r.stdout,
            "LE Audio in PipeWire: off (the CST default)\n"
            + row("write", wp_label(m.le_conf), "LE Audio off (the CST default): PipeWire will offer only A2DP and HFP")
            + "Turn it on: cst-t le-audio on\n",
        )
        self.assertFalse(os.path.exists(os.path.dirname(m.settings)))
        m.user_settings("LE_AUDIO=on\n")
        self.assertIn("Turn it off: cst-t le-audio off\n", m.run("le-audio", env={"CST_PROG": "cst-t"}).stdout)


class MainConfTest(SetupCase):
    """main.conf: backed up and replaced through sudo; revert takes the two keys from the oldest backup."""

    def test_apply_writes_it_through_sudo(self) -> None:
        for selinux in (False, True):
            m = self.machine()
            m.flag("selinux", on=selinux)
            r = m.run("apply", "--yes", "--no-restart")
            sudo = [c for c in r.calls if c.startswith("sudo ")]
            backup = re.fullmatch(r"backup:    (\S+)", r.stdout.splitlines()[1])
            assert backup is not None, r.stdout
            ctx = " --preserve=context" if selinux else ""
            z = " -Z" if selinux else ""
            self.assertEqual(len(sudo), 3, sudo)
            self.assertEqual(
                sudo[0], f"sudo env LC_ALL=C cp -p{ctx} --update=none-fail {m.main_conf} {backup.group(1)}"
            )
            self.assertRegex(sudo[1], rf"^sudo install -m 0644 -o root -g root{z} \S+ {re.escape(m.main_conf)}\.new$")
            self.assertEqual(sudo[2], f"sudo mv -f {m.main_conf}.new {m.main_conf}")
            self.assertEqual(self.text(backup.group(1)), VENDOR)
            self.assertIn(f"KernelExperimental = {ISO}", self.text(m.main_conf))
            self.assertIn(
                f"written:   {m.main_conf}\n           Experimental = true, KernelExperimental = {ISO}\n", r.stdout
            )
            r = m.run("apply", "--yes", "--no-restart")
            self.assertIn(
                f"unchanged: {m.main_conf} (Experimental and KernelExperimental already set in [General])\n", r.stdout
            )

    def test_revert_to_the_oldest_backup(self) -> None:
        m = self.machine(DONE)
        m.put("bt-journal", BAP_REGISTERED)
        old = m.put(m.main_conf + ".bak-20240101-000000", VENDOR)
        newer = m.put(m.main_conf + ".bak-20250101-000000", DONE)
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        r = m.run("revert", "--check")
        self.assertEqual(r.returncode, 10)
        self.assertIn(f"would set the two keys as in {old}:\n    --- {m.main_conf}\t", r.stdout)
        self.assertIn("would restart bluetooth.service (after asking) and wireplumber\n", r.stdout)
        self.assertEqual(self.text(m.main_conf), DONE)
        r = m.run("revert", "--yes")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(f"           the two keys as in {old}\n", r.stdout)
        self.assertNotIn("KernelExperimental = " + ISO, self.text(m.main_conf))
        self.order(
            r.calls,
            "sudo env LC_ALL=C cp -p",
            "sudo install",
            "sudo mv -f",
            "sudo systemctl restart bluetooth",
            "systemctl --user restart wireplumber",
        )
        r = m.run("revert", "--check")
        self.assertIn("unchanged: Experimental and KernelExperimental are already as in ", r.stdout)

    def test_revert_without_a_backup(self) -> None:
        m = self.machine(DONE)
        r = m.run("revert", "--check")
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(f"WARNING: no backup {m.main_conf}.bak-* found; main.conf not changed\n", r.stderr)


class SendersOfTest(unittest.TestCase):
    BUS = "NAME PID PROCESS\n:1.5 2 wireplumber\n:1.6 002 wireplumber\n:1.7 \N{SUPERSCRIPT TWO} odd\n:1.8 30 other\n"

    def test_the_pid_column_is_compared_as_awk_compares_it(self) -> None:
        # Numbers when both are ASCII digits ("002" is pid 2), strings otherwise.
        self.assertEqual(senders_of(self.BUS, "2"), [":1.5", ":1.6"])
        self.assertEqual(senders_of(self.BUS, "30"), [":1.8"])
        self.assertEqual(senders_of(self.BUS, "\N{SUPERSCRIPT TWO}"), [":1.7"])

    def test_digits_of_other_than_ascii_are_no_number(self) -> None:
        # isdigit() takes "²": int() refused it (a ValueError out of the survey), and "٢" was read as 2.
        self.assertEqual(senders_of(self.BUS, "\N{SUPERSCRIPT THREE}"), [])
        self.assertEqual(senders_of(self.BUS, "\N{ARABIC-INDIC DIGIT TWO}"), [])
        self.assertEqual(senders_of(":1.9 \N{ARABIC-INDIC DIGIT TWO} x\n", "2"), [])
        self.assertEqual(senders_of(f":1.9 {'2' * 5000} x\n", "2"), [])


class WatcherProgStateTest(unittest.TestCase):
    """The watcher's program is judged by the plan line that names its path, wherever plan prints it."""

    def test_the_line_of_the_watchers_path_decides(self) -> None:
        prog = "/h/.local/lib/cst/bt-ldac-watch"
        settings = "would write: /h/.config/cst/x.conf (from /h/old, before it is replaced)\n"
        self.assertEqual(watcher_prog_state(f"{settings}would record: {prog}\n", prog, lambda: True), OwnedState.ADOPT)
        self.assertEqual(watcher_prog_state(f"unchanged: {prog}\n", prog, lambda: False), OwnedState.OK)
        self.assertEqual(watcher_prog_state(f"would write: {prog}\n", prog, lambda: True), OwnedState.CHANGE)
        self.assertEqual(watcher_prog_state(f"would write: {prog}\n", prog, lambda: False), OwnedState.CONFLICT)
        self.assertEqual(watcher_prog_state("", prog, lambda: True), OwnedState.CHANGE)  # a plan that failed

    def test_a_home_with_a_trailing_slash_is_the_same_path(self) -> None:
        # HOME=/h/: the program's path is /h//.local/...; installer.py plan prints home.rstrip("/") + ...
        prog, printed = "/h//.local/lib/cst/bt-ldac-watch", "/h/.local/lib/cst/bt-ldac-watch"
        self.assertEqual(watcher_prog_state(f"unchanged: {printed}\n", prog, lambda: True), OwnedState.OK)
        self.assertEqual(watcher_prog_state(f"would record: {printed}\n", prog, lambda: True), OwnedState.ADOPT)

    def test_a_plan_with_no_line_of_that_path_is_judged_by_its_last_line(self) -> None:
        prog = "/h/.local/lib/cst/bt-ldac-watch"
        settings = "would write: /h/.config/cst/x.conf (from /o/bt-ldac-watch, before it is replaced)\n"
        self.assertEqual(watcher_prog_state("unchanged: /o/bt-ldac-watch\n", prog, lambda: False), OwnedState.OK)
        self.assertEqual(
            watcher_prog_state(f"{settings}would record: /o/bt-ldac-watch\n", prog, lambda: False), OwnedState.ADOPT
        )


class WatcherTest(SetupCase):
    """The LDAC rate watcher (LDAC_RATE=48000): installed through the installer, enabled, started; removed."""

    def test_install_then_nothing_then_remove(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        r = m.run("apply", "--no-restart")
        self.assertEqual(r.returncode, 1, r.text)  # verification: no adapter here
        lw = [c for c in r.calls if c.startswith("systemctl --user") and "is-" not in c and "show" not in c]
        self.assertEqual(
            lw,
            [
                "systemctl --user daemon-reload",
                f"systemctl --user enable {LW}",
                f"systemctl --user start {LW}",
            ],
        )
        self.assertTrue(os.access(m.lw_prog, os.X_OK))
        self.assertIn("--rate 48000", self.text(m.lw_unit))
        self.assertIn(f"enabled:   {LW} (starts with your session)\n", r.stdout)
        self.assertIn(f"started:   {LW} (logs: journalctl --user -u {LW})\n", r.stdout)
        self.assertIn(f"written:   {m.lw_unit} (LDAC to 48000 Hz)\n", r.stdout)
        rec = json.loads(self.text(m.record))
        self.assertEqual(sorted(rec["files"]), sorted([m.lw_prog, m.lw_unit]))
        self.assertIsNone(rec["files"][m.lw_unit]["src"])
        self.assertEqual(rec["files"][m.lw_prog]["src"], "bluetooth/bt-ldac-watch")
        self.assertEqual(rec["components"], {"bluetooth:ldac": "done"})
        r = m.run("plan")
        self.assertIn(row("ok", f"{LW} (your session)", "enabled, active"), r.stdout)
        self.assertIn(
            row(
                "note",
                "LDAC rate watcher",
                "on because ~/.config/cst/bluetooth.conf sets LDAC_RATE=48000 (CST default: off)",
            ),
            r.stdout,
        )
        r = m.run("apply", "--no-restart")
        self.assertIn(f"unchanged: {LW} enabled and active\n", r.stdout)
        self.assertEqual(
            [c for c in r.calls if c.startswith("systemctl --user") and "is-" not in c and "show" not in c], []
        )
        with open(m.lw_prog, "a", encoding="utf-8") as f:
            f.write("# old\n")
        self.assertIn(
            row("update", "~/.local/lib/cst/bt-ldac-watch", "older copy (apply installs the one in CST)"),
            m.run("plan").stdout,
        )
        r = m.run("apply", "--no-restart")
        self.assertIn(f"systemctl --user restart {LW}", r.calls)
        self.assertIn(f"restarted: {LW} (new files)\n", r.stdout)
        m.user_settings("")
        r = m.run("plan")
        self.assertIn(
            row("remove", "LDAC rate watcher", f"LDAC_RATE is empty: apply stops and removes {LW} and its program"),
            r.stdout,
        )
        r = m.run("apply", "--no-restart")
        self.assertEqual(
            [c for c in r.calls if c.startswith("systemctl --user") and "is-" not in c and "show" not in c],
            [
                f"systemctl --user disable --now {LW}",
                "systemctl --user daemon-reload",
            ],
        )
        self.assertIn(f"removed:   {m.lw_unit} (stopped and disabled)\nremoved:   {m.lw_prog}\n", r.stdout)
        self.assertFalse(
            os.path.exists(m.lw_prog) or os.path.exists(os.path.dirname(m.lw_prog)) or os.path.exists(m.lw_unit)
        )
        self.assertEqual(json.loads(self.text(m.record))["files"], {})
        self.assertIn(row("ok", "LDAC rate watcher", "off (LDAC_RATE empty; 48000 turns it on)"), m.run("plan").stdout)

    def test_the_unit_goes_where_the_user_manager_reads_units(self) -> None:
        # XDG_CONFIG_HOME in the shell only: the user manager reads ~/.config/systemd/user (its UnitPath,
        # FAKE's default here), so the unit goes there. Before, it went to $XDG_CONFIG_HOME/systemd/user,
        # which the manager never loaded (tier 1 test_220: `systemctl --user enable` then fails).
        m = self.machine(DONE)
        xdg = os.path.join(m.home, "xdg")
        m.put(os.path.join(xdg, "cst", "bluetooth.conf"), f"DEVICE={MAC}\nLDAC_RATE=48000\n")
        env = {"XDG_CONFIG_HOME": xdg}
        r = m.run("apply", "--no-restart", env=env)
        self.assertIn(f"written:   {m.lw_unit} (LDAC to 48000 Hz)\n", r.stdout)
        self.assertIn("systemctl --user show -p UnitPath --value", r.calls)
        self.assertFalse(os.path.exists(os.path.join(xdg, "systemd")), r.text)
        self.assertIn(m.lw_unit, json.loads(self.text(m.record))["files"])
        self.assertIn(row("ok", f"systemd/user/{LW}", "up to date (LDAC to 48000 Hz)"), m.run("plan", env=env).stdout)

    def test_the_unit_follows_the_folder_the_manager_names(self) -> None:
        # A manager with XDG_CONFIG_HOME of its own (UnitPath names it first): there. One that does not
        # answer: ~/.config/systemd/user, systemd's default.
        for answer, folder in (
            ("y/systemd/user /etc/systemd/user\n", "y/systemd/user"),
            (None, ".config/systemd/user"),
        ):
            with self.subTest(answer=answer):
                m = self.machine(DONE, "LDAC_RATE=48000\n")
                if answer is None:
                    m.flag("no-unit-path")
                else:
                    m.put("unit-path", f"{m.home}/{answer}")
                unit = os.path.join(m.home, folder, LW)
                r = m.run("apply", "--no-restart")
                self.assertIn(f"written:   {unit} (LDAC to 48000 Hz)\n", r.stdout)
                self.assertTrue(os.path.isfile(unit))
                self.assertEqual(sorted(json.loads(self.text(m.record))["files"]), sorted([m.lw_prog, unit]))

    def test_an_older_copy_elsewhere_is_removed(self) -> None:
        # The upgrade: an older CST wrote and recorded the unit under the shell's XDG_CONFIG_HOME
        # (here: an apply while the manager named that folder). Now the manager reads ~/.config: plan
        # shows the older copy's removal, apply removes it through the installer and writes the new
        # one, a second apply changes nothing, revert removes the new one.
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        old = os.path.join(m.home, "xdg", "systemd", "user", LW)
        m.put("unit-path", f"{os.path.dirname(old)} /etc/systemd/user\n")
        m.run("apply", "--no-restart")
        self.assertTrue(os.path.isfile(old))
        os.unlink(m.state + "/unit-path")
        r = m.run("plan")
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn(row("remove", "~/xdg/systemd/user/" + LW, LW_STALE), r.stdout)
        self.assertIn(row("write", f"systemd/user/{LW}", "the LDAC rate watcher (LDAC_RATE=48000)"), r.stdout)
        r = m.run("apply", "--no-restart")
        self.assertIn(f"removed:   {old} (an older CST's copy)\n", r.stdout)
        self.assertIn(f"written:   {m.lw_unit} (LDAC to 48000 Hz)\n", r.stdout)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(sorted(json.loads(self.text(m.record))["files"]), sorted([m.lw_prog, m.lw_unit]))
        r = m.run("apply", "--no-restart")
        self.assertNotIn("older CST", r.stdout)
        self.assertIn(f"unchanged: {LW} enabled and active\n", r.stdout)
        self.assertNotIn(LW_STALE, m.run("plan").stdout)
        r = m.run("revert", "--yes", "--no-restart")
        self.assertIn(f"removed:   {m.lw_unit} (stopped and disabled)\n", r.stdout)
        self.assertEqual(json.loads(self.text(m.record))["files"], {})

    def test_a_home_not_written_plainly_still_plans(self) -> None:
        # HOME as a shell or pam_env can leave it. A trailing slash: the record is read where the
        # installer writes it (record_file), so the older copy is still shown. One with a "." in it,
        # which the record's reader refuses: plan prints its rows, without the older copies. Before,
        # both ended with a traceback ("not a plain absolute path").
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        old = os.path.join(m.home, "xdg", "systemd", "user", LW)
        m.put("unit-path", f"{os.path.dirname(old)} /etc/systemd/user\n")
        m.run("apply", "--no-restart")
        os.unlink(m.state + "/unit-path")
        r = m.run("plan", env={"HOME": m.home + "//"})
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, 10, r.text)
        self.assertIn(f"{old} {LW_STALE}\n", r.stdout)  # the label is not shortened to ~ for this HOME
        for cmd in ("plan", "status"):
            with self.subTest(cmd=cmd):
                r = m.run(cmd, env={"HOME": m.home + "/."})
                self.assertNotIn("Traceback", r.stderr)
                self.assertIn(f"systemd/user/{LW}", r.stdout)
                self.assertNotIn(LW_STALE, r.stdout)

    def test_an_older_copy_cst_did_not_write_is_left_alone(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        old = os.path.join(m.home, "xdg", "systemd", "user", LW)
        m.put("unit-path", f"{os.path.dirname(old)} /etc/systemd/user\n")
        m.run("apply", "--no-restart")
        os.unlink(m.state + "/unit-path")
        m.put(old, "[Unit]\nDescription=mine\n")
        r = m.run("plan")
        self.assertIn(row("forget", "~/xdg/systemd/user/" + LW, LW_STALE_FOREIGN), r.stdout)
        r = m.run("revert", "--yes", "--no-restart")
        self.assertIn(
            f"WARNING: {old} was not written by CST: left alone (forgotten from the install record)\n", r.stderr
        )
        self.assertEqual(self.text(old), "[Unit]\nDescription=mine\n")
        self.assertEqual(json.loads(self.text(m.record))["files"], {})
        self.assertNotIn(LW_STALE_FOREIGN, m.run("plan").stdout)

    def test_apply_adopts_a_current_watcher_the_record_does_not_list(self) -> None:
        """A current program and unit missing from the install record (installed before it existed)
        are added to it, with no write, no daemon-reload and no restart."""
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        m.run("apply", "--no-restart")
        rec = json.loads(self.text(m.record))
        before = {path: rec["files"].pop(path) for path in (m.lw_prog, m.lw_unit)}
        with open(m.record, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        stats = {path: (os.stat(path).st_ino, os.stat(path).st_mtime_ns) for path in (m.lw_prog, m.lw_unit)}
        # plan reads the installer's plan: a current copy its record does not list is "adopt" (apply adds it
        # to the record, writing nothing), and the service needs no restart for it.
        r = m.run("plan")
        self.assertIn(
            row(
                "adopt",
                "~/.local/lib/cst/bt-ldac-watch",
                "same as bt-ldac-watch in CST; apply adds it to the install record",
            ),
            r.stdout,
        )
        self.assertIn(row("ok", f"{LW} (your session)", "enabled, active"), r.stdout)
        r = m.run("apply", "--no-restart")
        self.assertIn(
            f"unchanged: {m.lw_prog}\nunchanged: {m.lw_unit} (LDAC to 48000 Hz)\nunchanged: {LW} enabled and active\n",
            r.stdout,
        )
        self.assertEqual({path: (os.stat(path).st_ino, os.stat(path).st_mtime_ns) for path in stats}, stats)
        self.assertEqual(
            [c for c in r.calls if c.startswith("systemctl --user") and "is-" not in c and "show" not in c], []
        )
        files = json.loads(self.text(m.record))["files"]
        self.assertEqual(files[m.lw_prog]["src"], "bluetooth/bt-ldac-watch")
        self.assertEqual(files[m.lw_unit]["component"], "bluetooth:ldac")
        self.assertEqual({path: files[path]["sha256"] for path in before}, {p: f["sha256"] for p, f in before.items()})

    def test_plan_and_status_name_the_last_failure(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        m.put("lw-journal", JOURNAL)
        r = m.run("plan")
        self.assertRegex(
            r.stdout, r"  warn      LDAC rate watcher +its last attempt failed at 2026-11-11T14:11:58-03:00: "
        )
        m.run("apply", "--no-restart")
        r = m.run("status", env={"CST_PROG": "cst-t"})
        self.assertRegex(r.stdout, r"  warn      LDAC rate watcher +its last attempt failed at ")
        self.assertIn(
            row("", "", "to turn it off: remove LDAC_RATE from ~/.config/cst/bluetooth.conf, then cst-t apply"),
            r.stdout,
        )
        m.user_settings("")
        r = m.run("plan")
        self.assertNotIn("its last attempt failed", r.stdout)
        self.assertIn(
            row("warn", LW, "running although LDAC_RATE is empty: cst-t apply stops and removes it"),
            m.run("status", env={"CST_PROG": "cst-t"}).stdout,
        )

    def test_the_program_and_the_unit_as_links(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        m.run("apply", "--no-restart")
        for path, word in ((m.lw_prog, "~/.local/lib/cst/bt-ldac-watch"), (m.lw_unit, f"systemd/user/{LW}")):
            kept = path + ".kept"
            os.rename(path, kept)
            os.symlink(kept, path)
            r = m.run("plan")
            self.assertEqual(r.stderr, "")
            self.assertIn(
                row(
                    "ok", word, "same as bt-ldac-watch in CST" if path == m.lw_prog else "up to date (LDAC to 48000 Hz)"
                ),
                r.stdout,
            )
            with open(kept, "a", encoding="utf-8") as f:
                f.write("# changed by hand\n")
            r = m.run("plan")
            self.assertEqual(r.stderr, "")
            self.assertIn(
                row(
                    "update",
                    word,
                    "older copy (apply installs the one in CST)"
                    if path == m.lw_prog
                    else "differs (apply rewrites it)",
                ),
                r.stdout,
            )
            r = m.run("apply", "--no-restart")
            self.assertFalse(os.path.islink(path))
            with open(kept, "rb") as f:
                self.assertTrue(f.read().endswith(b"# changed by hand\n"), "the link was written through")

    def test_foreign_masked_and_folder_units_are_conflicts(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        os.makedirs(os.path.dirname(m.lw_unit))
        foreign = m.put("foreign.service", "[Service]\nExecStart=/bin/true\n")
        folder = os.path.join(m.home, "a-folder")
        os.mkdir(folder)
        for target in (foreign, os.devnull, folder):
            os.symlink(target, m.lw_unit)
            r = m.run("plan")
            self.assertEqual(r.stderr, "")
            self.assertIn(
                row("CONFLICT", f"systemd/user/{LW}", "not written by CST: apply refuses; move it away by hand"),
                r.stdout,
            )
            r = m.run("apply", env={"CST_PROG": "cst-t"})
            self.assertEqual(
                (r.returncode, r.stderr),
                (1, f"cst-t: ERROR: {m.lw_unit} was not written by CST: move it away, then run apply again.\n"),
            )
            os.unlink(m.lw_unit)
        m.put(m.lw_unit, "[Service]\nExecStart=/bin/true\n")
        r = m.run("revert", "--check")
        self.assertIn(f"WARNING: {m.lw_unit} was not written by CST: left alone\n", r.stderr)
        m.user_settings("")
        self.assertIn(row("CONFLICT", f"systemd/user/{LW}", "not written by CST: left alone"), m.run("plan").stdout)
        r = m.run("apply", "--no-restart")
        self.assertIn(f"WARNING: {m.lw_unit} was not written by CST: left alone\n", r.stderr)
        self.assertFalse([c for c in r.calls if "disable" in c])

    def test_a_foreign_program_with_the_watcher_off(self) -> None:
        # The watcher off (no LDAC_RATE): a program at its place that CST did not write is left alone.
        m = self.machine(DONE)
        m.put(m.lw_prog, "#!/bin/sh\necho not ours\n", mode=0o755)
        r = m.run("plan")
        self.assertIn(row("ok", "LDAC rate watcher", "off (LDAC_RATE empty; 48000 turns it on)"), r.stdout)
        self.assertIn(row("CONFLICT", "~/.local/lib/cst/bt-ldac-watch", "not written by CST: left alone"), r.stdout)

    def test_revert_removes_it_and_check_changes_nothing(self) -> None:
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        m.put("bt-journal", BAP_REGISTERED)
        m.run("apply", "--no-restart")
        r = m.run("revert", "--check")
        self.assertEqual(r.returncode, 10)
        self.assertIn(f"would stop, disable and remove {LW} and {m.lw_prog}\n", r.stdout)
        self.assertTrue(os.path.exists(m.lw_prog))
        r = m.run("revert", "--yes")
        self.assertIn(f"systemctl --user disable --now {LW}", r.calls)
        self.assertFalse(os.path.exists(m.lw_prog) or os.path.exists(m.lw_unit))

    def test_a_cancelled_restart_still_sets_up_the_watcher(self) -> None:
        m = self.machine(settings="LDAC_RATE=48000\n")
        r = m.run("apply", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 0, r.text)
        self.assertIn(f"systemctl --user start {LW}", r.calls)
        self.assertTrue(
            r.stdout.endswith("Not done yet: the restarts above and the checks.\nAfter restarting, run: cst-t apply\n"),
            r.stdout,
        )


class ApplyTest(SetupCase):
    def test_the_order_of_the_steps(self) -> None:
        m = self.machine(settings="LDAC_RATE=48000\nBEARER=bredr\n")
        m.put("bt-info", "\tPaired: yes\n\tPreferredBearer: le\n")
        r = m.run("apply", "--yes")
        sections = [ln for ln in r.stdout.splitlines() if ln.startswith("== ")]
        self.assertEqual(
            [s if s.startswith("== headset") else s.split(":")[0] for s in sections],
            [
                "== BlueZ",
                "== WirePlumber",
                "== LE Audio",
                "== restarts",
                "== LDAC rate watcher",
                "== preferred bearer",
                "== services",
                "== LE Audio",
                f"== headset {MAC} in PipeWire",
            ][: len(sections)],
        )
        self.order(
            r.calls,
            "sudo mv -f",
            "sudo systemctl restart bluetooth",
            "systemctl --user restart wireplumber",
            f"systemctl --user start {LW}",
            f"bluetoothctl bearer {MAC} bredr",
            f"journalctl --user -u {LW}",  # the checks (their services rows reuse the states read)
        )
        bearer = r.calls.index(f"bluetoothctl bearer {MAC} bredr")
        self.assertTrue(r.calls[bearer + 1].startswith("  stdin=/dev/null via=timeout 5 bluetoothctl bearer"), r.calls)
        self.assertIn(
            "set:       PreferredBearer = bredr (for connections the PC starts; disconnect and reconnect)\n", r.stdout
        )
        self.assertEqual(r.returncode, 1)  # no adapter on this machine
        self.assertTrue(r.stderr.endswith(": ERROR: verification found problems (see above).\n"), r.stderr)
        self.assertIn(row("BAD", "adapter", "no Bluetooth adapter in org.bluez"), r.stdout)

    def test_a_bearer_that_does_not_change(self) -> None:
        m = self.machine(DONE, "BEARER=bredr\n")
        m.put("bt-info", "\tPaired: yes\n\tPreferredBearer: le\n")
        m.flag("bearer-stuck")
        r = m.run("apply", "--no-restart", env={"BT_TIMEOUT": "0"})
        bearer = r.calls.index(f"bluetoothctl bearer {MAC} bredr")
        self.assertEqual(r.calls[bearer + 1], "  stdin=/dev/null via=direct")
        self.assertIn("WARNING: could not set the bearer: Failed to set bearer\n", r.stderr)
        m.put("bt-info", "\tPaired: no\n")
        r = m.run("apply", "--no-restart")
        self.assertIn(f"WARNING: {MAC} is not paired; bearer not changed\n", r.stderr)

    def test_preconditions(self) -> None:
        m = self.machine()
        m.flag("no-session")
        r = m.run("apply", env={"CST_PROG": "cst-t"})
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertEqual(
            r.stderr,
            "cst-t: ERROR: no systemd --user session (XDG_RUNTIME_DIR/D-Bus): run it in your desktop or login session\n",
        )
        r = m.run("revert", env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            r.stderr,
            "cst-t: ERROR: no systemd --user session (XDG_RUNTIME_DIR/D-Bus): run it in your desktop or login session\n",
        )
        self.assertEqual(m.run("revert", "--check").returncode, 0)


class StatusTest(SetupCase):
    def test_a_healthy_machine_in_a_toolbox(self) -> None:
        m = self.machine(DONE)
        m.run("apply", "--no-restart")
        m.services()
        os.unlink(m.pending)
        m.adapter()
        m.flag("iso-socket")
        m.put("wp-roles", CLASSIC)
        dump = pw_dump(rate=96000)
        m.put("pw-dump.json", json.dumps(dump))
        r = m.run("status", container=True, env={"CST_PROG": "cst-t"})
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.text)
        for line in (
            row("ok", "bluetooth.service", "active"),
            row("ok", "wireplumber (your session)", "active"),
            row("ok", "LE Audio", "off (the CST default; PipeWire offers only classic profiles)"),
            row("", "", "turn on with: cst-t le-audio on"),
            row("ok", "adapter ISO feature", "on for /org/bluez/hci0"),
            row("ok", "kernel ISO socket", "opens"),
            row("ok", "BAP in BlueZ", "Sink and Source PAC in Media1"),
            row("ok", "PipeWire BAP LC3 endpoint", "not registered (as wanted with LE Audio off)"),
            f"\n== headset {MAC} in PipeWire\n",
            row(
                "warn",
                "LDAC sample rate",
                "96000 Hz, set up by the headset: fewer bits per sample at the same bitrate; LDAC_RATE=48000 turns on the watcher (see bluetooth/README.md)",
            ),
            "\nAll checks passed, 1 warning(s).\n",
        ):
            self.assertIn(line, r.stdout)
        self.assertRegex(
            r.stdout,
            r"  ok        headset +profile a2dp-sink, rule ldac.quality=auto, node codec ldac, quality auto, rate 96000 Hz\n",
        )
        hosted = [c for c in r.calls if c.startswith("flatpak-spawn --host ")]
        self.assertIn("flatpak-spawn --host timeout 10 pw-dump", hosted)
        self.assertTrue(
            any(
                c.startswith(f"flatpak-spawn --host env XDG_CONFIG_HOME={m.home}/.config python3 -I -B ")
                and c.endswith("/bluetooth/bt-helper wp-roles")
                for c in hosted
            ),
            hosted,
        )
        self.assertNotIn("pactl", " ".join(r.calls))  # only apply's A2DP restore asks pactl

    def test_the_ldac_rate_row_with_the_watcher_on(self) -> None:
        # LDAC_RATE=48000 and LDAC above it: the row says what the watcher does, running or not;
        # at 48000 Hz there is no row.
        m = self.machine(DONE, "LDAC_RATE=48000\n")
        m.adapter()
        m.flag("iso-socket")
        m.put("wp-roles", CLASSIC)
        m.put("pw-dump.json", json.dumps(pw_dump(rate=96000)))
        r = m.run("status", container=True)
        self.assertIn(
            row(
                "warn",
                "LDAC sample rate",
                "96000 Hz, set up by the headset; the watcher (LDAC_RATE=48000) is not running",
            ),
            r.stdout,
        )
        m.unit("user", LW.removesuffix(".service"))
        r = m.run("status", container=True)
        self.assertIn(
            row(
                "warn",
                "LDAC sample rate",
                f"96000 Hz: the watcher switches it to 48000 within seconds, unless it gave up (journalctl --user -u {LW})",
            ),
            r.stdout,
        )
        m.put("pw-dump.json", json.dumps(pw_dump(rate=48000)))
        r = m.run("status", container=True)
        self.assertIn("rate 48000 Hz", r.stdout)
        self.assertNotIn("LDAC sample rate", r.stdout)

    def test_status_after_problems(self) -> None:
        m = self.machine(DONE)
        m.unit("user", "wireplumber", active=False)
        m.adapter(iso=False, bap=False)
        m.put("bt-journal", BAP_REGISTERED)
        m.unit("user", "wireplumber", active=False, pid=4242)
        r = m.run("status", container=True, env={"CST_PROG": "cst-t"})
        self.assertEqual(r.returncode, 1)
        self.assertIn(row("BAD", "wireplumber (your session)", "not active"), r.stdout)
        self.assertIn(
            row(
                "BAD",
                "adapter ISO feature",
                f"{ISO} not on for /org/bluez/hci0 (KernelExperimental not in effect? restart bluetooth)",
            ),
            r.stdout,
        )
        self.assertIn(
            row("BAD", "BAP in BlueZ", "not in Media1 (Experimental not in effect? restart bluetooth)"), r.stdout
        )
        self.assertIn(row("BAD", "kernel ISO socket", "not available"), r.stdout)
        self.assertIn(
            row("warn", "PipeWire BAP LC3 endpoint", "still registered: LE Audio off takes effect after cst-t apply"),
            r.stdout,
        )
        self.assertIn(
            row("warn", "pw-dump", "no answer: PipeWire stuck? (systemctl --user restart pipewire wireplumber)"),
            r.stdout,
        )
        self.assertRegex(r.stdout, r"\n4 check\(s\) failed, \d warning\(s\)\.\n$")


class EnvironmentTest(SetupCase):
    """The commands get the caller's environment: nothing added, LC_CTYPE as it was; only the
    `systemctl show` of a unit's state (cst_core's SystemdPort.show) and of the user manager's UnitPath
    (show_value of the manager itself, which CST reads) add LC_ALL=C."""

    def test_no_leak(self) -> None:
        m = self.machine(DONE)
        m.adapter(listed=False)
        for extra in ({}, {"LC_CTYPE": "C"}, {"LC_CTYPE": "pt_BR.UTF-8", "LANG": "C"}):
            log = os.path.join(m.state, "env-log")
            if os.path.exists(log):
                os.unlink(log)
            m.run("status", env=extra)
            m.run("apply", "--no-restart", env=extra)
            seen: set[str] = set()
            shows: list[set[str]] = []
            for line in m.env_log().splitlines():
                call, words = line.split("\t", 1)
                if call.startswith("systemctl") and ("show -p ActiveState" in call or "show -p UnitPath" in call):
                    shows.append(set(words.split()))
                else:
                    seen |= set(words.split())
            want = {"CST_BT_CONTAINER=0", f"CST_BT_ROOT={m.root}"} | {f"{k}={v}" for k, v in extra.items()}
            self.assertEqual(seen, want, extra)
            self.assertTrue(shows, extra)
            for words in shows:
                self.assertEqual(words, want | {"LC_ALL=C"}, extra)


class ScratchFileTest(SetupCase):
    """The scratch copy of main.conf (mktemp, in TMPDIR) is removed however the run ends, as the
    shell's EXIT trap removed it: a closed stdout (`| head`) and SIGTERM end the run killed by
    that signal, with nothing left behind."""

    def scratch(self, m: Machine) -> dict[str, str]:
        d = os.path.join(m.tmp, "scratch")
        os.mkdir(d)
        return {"TMPDIR": d, "CST_PROG": "cst-t"}

    def test_a_closed_stdout_ends_the_run_by_sigpipe(self) -> None:
        m = self.machine()
        env = self.scratch(m)
        for args in (("plan",), ("status",), ("revert", "--check"), ("le-audio",)):
            r = m.run(*args, closed_stdout=True, env=env)
            self.assertEqual((r.returncode, r.stderr), (-signal.SIGPIPE, ""), args)
            self.assertEqual(os.listdir(env["TMPDIR"]), [], args)
        self.assertEqual(self.text(m.main_conf), VENDOR)

    def test_the_help_on_a_closed_stdout_exits_141(self) -> None:
        # The help came from sed in a pipeline: sed died by SIGPIPE, the script exited 141.
        r = self.machine().run("help", closed_stdout=True)
        self.assertEqual((r.returncode, r.stderr), (141, ""))

    def test_sighup_ignored_at_start_stays_ignored(self) -> None:
        # As under nohup: a shell cannot trap a signal ignored on entry, so apply goes on.
        m = self.machine()
        m.put("fail-sudo-cp", "PARENT-HUP")
        r = m.run("apply", "--yes", "--no-restart", ignore_hup=True)
        self.assertNotEqual(r.returncode, -signal.SIGHUP, r.text)
        self.assertIn(ISO, self.text(m.main_conf))

    def test_every_command_needs_its_scratch_file(self) -> None:
        # bluetooth-setup.sh made it first, for every command but help: no TMPDIR, no run.
        m = self.machine(headset=False)
        env = {"TMPDIR": os.path.join(m.tmp, "missing")}
        for args in (
            ("plan",),
            ("status",),
            ("le-audio",),
            ("revert", "--check"),
            ("le-audio", "on"),
            ("apply", "--yes"),
        ):
            r = m.run(*args, env=env)
            self.assertEqual((r.returncode, r.stdout), (1, ""), args)
            self.assertTrue(r.stderr.startswith("mktemp: "), r.stderr)
            self.assertEqual(r.stderr.count("\n"), 1, r.stderr)
            self.assertEqual([c for c in r.calls if not c.startswith("  ")], [], args)
        self.assertFalse(os.path.exists(m.settings))

    def test_sigterm_ends_the_run_by_sigterm(self) -> None:
        m = self.machine()
        env = self.scratch(m)
        m.put("fail-sudo-cp", "PARENT-TERM")
        r = m.run("apply", "--yes", env=env)
        self.assertEqual(r.returncode, -signal.SIGTERM, r.text)
        self.assertEqual(os.listdir(env["TMPDIR"]), [])
        self.assertEqual(self.text(m.main_conf), VENDOR)


class ClosedOutputTest(SetupCase):
    """stdout closed by its reader (`| head`): a command that only reads ends quietly by SIGPIPE
    (ScratchFileTest); one that changes the machine stops with a message instead, its scratch
    file removed."""

    def test_changing_commands_stop_with_a_message(self) -> None:
        m = self.machine()
        scratch = os.path.join(m.tmp, "scratch")
        os.mkdir(scratch)
        env = {"TMPDIR": scratch, "CST_PROG": "cst-t"}
        for args in (("apply", "--yes"), ("revert", "--yes")):
            r = m.run(*args, closed_stdout=True, env=env)
            self.assertEqual(r.returncode, 1, r.text)
            self.assertEqual(
                r.stderr, "cst-t: stopped: its output was closed. Backup of main.conf: none taken. Undo: cst-t revert\n"
            )
            self.assertEqual(os.listdir(scratch), [], args)
        self.assertEqual(self.text(m.main_conf), VENDOR)
        r = m.run("le-audio", "on", closed_stdout=True, env=env)
        self.assertEqual((r.returncode, r.stderr), (1, "cst-t: stopped: its output was closed.\n"))
        self.assertEqual(os.listdir(scratch), [])


if __name__ == "__main__":
    unittest.main()
