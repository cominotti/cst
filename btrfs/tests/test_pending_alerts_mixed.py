# SPDX-License-Identifier: MPL-2.0
"""cst-pending-alerts of two versions on one machine: the record works both ways.

The installed copy (/usr/local/bin/cst-pending-alerts, run at each login)
and the checkout's (run by cst btrfs status) can be of different versions
after a git pull, and both read and write ~/.local/state/cst-alerts/delivered.
These tests run the bash version installed before the port to Python
(tests/fixtures/legacy/cst-pending-alerts@d4d8c91, the copy of commit d4d8c91
on main) and this checkout's program on the same alerts and the same record:
what one shows, the other knows as seen, and both list it alike.

Run with: invowk cmd test module btrfs.tests.test_pending_alerts_mixed
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Final, override
import unittest

from btrfs.tests import test_pending_alerts as base

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY: Final = os.path.join(REPO, "tests", "fixtures", "legacy", "cst-pending-alerts@d4d8c91")
OLD: Final[list[str]] = ["bash", LEGACY]
NEW: Final[list[str]] = base.RUN


class MixedVersionsTest(unittest.TestCase):
    h: base.PendingAlertsTest

    @override
    def setUp(self) -> None:
        # The behaviour tests' alerts folder, stubs and helpers.
        self.h = base.PendingAlertsTest("test_usage")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def run_as(self, prog: list[str], *args: str) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            [*prog, *args],
            env=self.h.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def alerts(self) -> None:
        """Eleven alerts: one login shows eight and sums three up (both kinds of record lines)."""
        for n in range(1, 12):
            urgency = "critical" if n in (2, 11) else "normal"
            self.h.alert(n, urgency=urgency, title=f"a{n:02d}", age=2000 - n, count=n)

    def check_one_then_other(self, first: list[str], second: list[str]) -> None:
        self.alerts()
        self.run_as(first)
        self.assertEqual(len(self.h.notifications()), 9)
        record = self.h.read_record()
        listed = self.run_as(first, "--list").stdout
        self.assertEqual(self.run_as(second, "--list").stdout, listed)
        self.assertIn(base.SUMMARIZED, listed)
        r = self.run_as(second)
        self.assertEqual(r.stderr, "")
        self.assertEqual(len(self.h.notifications()), 9, "the other version showed an alert again")
        self.assertEqual(self.h.read_record(), record)
        # A repeat of one alert: the other version shows it, counting only the new occurrence.
        self.h.alert(2, urgency="critical", title="a02", age=10, count=3)
        self.run_as(second)
        calls = self.h.notifications()
        self.assertEqual(len(calls), 10)
        self.assertTrue(calls[-1][-1].startswith("Happened again "), calls[-1][-1])
        self.assertEqual(self.run_as(first).stderr, "")
        self.assertEqual(len(self.h.notifications()), 10)

    def test_installed_bash_shows_this_checkout_knows(self) -> None:
        self.check_one_then_other(OLD, NEW)

    def test_this_checkout_shows_installed_bash_knows(self) -> None:
        self.check_one_then_other(NEW, OLD)


if __name__ == "__main__":
    unittest.main()
