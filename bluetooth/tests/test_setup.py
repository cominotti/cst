# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests of `cst bluetooth` (bluetooth-setup.sh): the whole program, run
as a user runs it, on the fake machine of setup_fakes.py (stub commands on
PATH, a scratch HOME and a scratch host root). They pin what a user or a
caller sees: the output, the exit status, the files written and the commands
run, in order.

Run from the repository root:
    invowk cmd test module bluetooth.tests.test_setup
"""

import unittest

from bluetooth.tests.setup_fakes import ISO, SETUP, Machine

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


if __name__ == "__main__":
    unittest.main()
