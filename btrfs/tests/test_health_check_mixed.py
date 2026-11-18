# SPDX-License-Identifier: MPL-2.0
"""btrfs-health-check of two versions on one machine: its state works both ways.

The unit runs the installed copy every hour; after a git pull and
cst btrfs install the next run is a new version, and a downgrade is the
reverse. Both read and write /var/lib/btrfs-health-check (the reclaim error
counts, the kernel log cursor) and the degraded-boot markers. These tests
alternate the last bash version (tests/fixtures/legacy/btrfs-health-check@d4d8c91+seam,
main's d4d8c91 bash with the CST_HEALTH_ROOT test seam) and this
checkout's program on the same test root, in both orders.

Run with: invowk cmd test module btrfs.tests.test_health_check_mixed
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Final, override
import unittest

from btrfs.tests import test_health_check as base

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY: Final = os.path.join(REPO, "tests", "fixtures", "legacy", "btrfs-health-check@d4d8c91+seam")
OLD: Final[list[str]] = ["bash", LEGACY]
NEW: Final[list[str]] = list(base.RUN)


class MixedVersionsTest(unittest.TestCase):
    h: base.HealthCheckTest

    @override
    def setUp(self) -> None:
        self.h = base.HealthCheckTest("test_healthy")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(base.RUN.__setitem__, slice(None), NEW)

    def run_as(self, prog: list[str]) -> subprocess.CompletedProcess[str]:
        base.RUN[:] = prog
        return self.h.run_check()

    def check_state_across(self, first: list[str], second: list[str]) -> None:
        h = self.h
        alloc = f"sys/fs/btrfs/{base.UUID}/allocation"
        state = f"var/lib/btrfs-health-check/reclaim-errors-{base.UUID}-data"
        cursor = h.path("var/lib/btrfs-health-check/kernel.cursor")
        # The reclaim count one version recorded is the other's baseline.
        h.write(f"{alloc}/data/reclaim_errors", "2\n")
        self.assertEqual(self.run_as(first).returncode, 1)
        self.assertEqual(h.read(state), f"{base.BOOT} 2\n")
        h.assert_healthy(self.run_as(second))
        self.assertEqual(h.read(state), f"{base.BOOT} 2\n")
        # The cursor one version recorded is the one the other reads from.
        self.assertEqual(
            [c[1:] for c in h.calls("journalctl")],
            [["-k", "-q", "--no-pager", "-o", "short-iso", f"--cursor-file={cursor}"]],
        )
        # A degraded-boot marker: complete_since= written by one, honoured by the other, which clears it.
        since = h.now - 5 * 86400
        marker = h.write(f"var/lib/btrfs-degraded-boot/degraded-{base.UUID}", f"since={since}\nsince_human=x\n")
        h.fs["scrub"] = h.scrub(since - 3600)
        self.assertEqual(self.run_as(first).returncode, 1)
        with open(marker, encoding="utf-8") as f:
            complete = int(f.read().splitlines()[2].split("=")[1])
        self.assertLessEqual(abs(complete - int(time.time())), 120)
        h.fs["scrub"] = h.scrub(complete + 60)
        r = self.run_as(second)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn(f"cleared {marker}", r.stdout)
        self.assertFalse(os.path.exists(marker))

    def test_bash_then_python(self) -> None:
        self.check_state_across(OLD, NEW)

    def test_python_then_bash(self) -> None:
        self.check_state_across(NEW, OLD)


if __name__ == "__main__":
    unittest.main()
