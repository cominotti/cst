# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests of `cst bluetooth` (bluetooth-setup.sh): the whole program, run
as a user runs it, on the fake machine of setup_fakes.py (stub commands on
PATH, a scratch HOME and a scratch host root). They pin what a user or a
caller sees: the output, the exit status, the files written and the commands
run, in order.

Run from the repository root:
    invowk cmd test module bluetooth.tests.test_setup
"""

import os
import pwd
import unittest

from bluetooth.tests.setup_fakes import ISO, SETUP, Machine
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

    def test_a_rule_file_it_cannot_read_stops_before_any_change(self) -> None:
        m = Machine(self)
        m.put(m.main_conf, GENERAL)
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


if __name__ == "__main__":
    unittest.main()
