# SPDX-License-Identifier: MPL-2.0
"""Mixed-version tests of the btrfs-snapshot port: the bash program of the main branch before the
port (tests/fixtures/legacy/btrfs-snapshot@47038aa, the file of main at 47038aa, this branch's base, which ran everything itself) and the current
front and Python runtime, one after the other on the same fake machine (snapshot_fakes.py), both
ways: what one version leaves in /var/lib/btrfs-snapshot, /run and the pin file, the other reads
and goes on with, and the lock of a run of one version keeps a run of the other out.

An upgrade can land between two hourly runs, during a NAS backup, or be undone (an older checkout
installed again), so each direction is a real case.

Run with: invowk cmd test module btrfs.tests.test_snapshot_mixed
"""

import os
import subprocess
import time
import unittest

from btrfs.tests.snapshot_fakes import GIB, SCRIPT, UMASK, RuntimeTest, log

LEGACY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests",
    "fixtures",
    "legacy",
    "btrfs-snapshot@47038aa",
)
SNAP = "home.20261118T0905"
REPO_ID = "ab" * 32
BOTH = (("old->new", LEGACY, SCRIPT), ("new->old", SCRIPT, LEGACY))


class Base(RuntimeTest):
    def run_with(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", script, *args],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.tmp,
            umask=UMASK,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def start_with(self, script: str, *args: str) -> subprocess.Popen[str]:
        p = subprocess.Popen(
            ["bash", script, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            cwd=self.tmp,
            umask=UMASK,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self.end_prog, p)
        return p

    def fresh(self) -> None:
        """A new machine for the next direction (the same scratch folder, state and calls emptied)."""
        if os.path.isdir(self.state):
            for name in os.listdir(self.state):
                os.remove(os.path.join(self.state, name))
        self.forget_calls()


class HourlyTest(Base):
    def test_a_pause_and_its_alert_end_in_the_other_version(self) -> None:
        for name, first, second in BOTH:
            with self.subTest(name):
                self.fresh()
                self.machine(free=245 * GIB)
                r = self.run_with(first, "hourly")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue(os.path.exists(self.state_file("paused")))
                self.assertTrue(os.path.exists(self.state_file("alerted-paused")))
                # Still below RESUME_ABOVE_GIB: the other version keeps the pause (hysteresis).
                self.machine(free=265 * GIB)
                self.forget_calls()
                r = self.run_with(second, "hourly")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual([c[0] for c in self.core_calls()], ["prune"])
                self.assertEqual(self.notified(), [])  # alerted today already: not again
                self.machine(free=400 * GIB)
                self.forget_calls()
                r = self.run_with(second, "hourly")
                self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
                self.assertFalse(os.path.exists(self.state_file("paused")))
                self.assertFalse(os.path.exists(self.state_file("alerted-paused")))
                self.assertTrue(self.read_state("last-ok").strip().isdigit())

    def test_a_wait_started_by_one_version_alerts_after_a_day_in_the_other(self) -> None:
        old = os.path.join(self.home, ".cache.pre-subvol-20261118T100000")
        os.makedirs(os.path.join(old, "m"))
        self.machine(mounts=["/", f"{old}/m"])
        for name, first, second in BOTH:
            with self.subTest(name):
                self.fresh()
                self.write(self.state_file("drop-pending"), old + "\n")
                r = self.run_with(first, "hourly")
                self.assertIn(log(5, "old trees still being deleted (1); no snapshot this hour"), r.stdout)
                since = self.state_file("pending-since")
                t = time.time() - 2 * 86400
                os.utime(since, (t, t))
                self.forget_calls()
                r = self.run_with(second, "hourly")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(len(self.notified()), 1)
                self.assertTrue(os.path.isdir(old))


class LockTest(Base):
    def test_a_run_of_one_version_keeps_the_other_out(self) -> None:
        for name, first, second in BOTH:
            with self.subTest(name):
                self.fresh()
                self.core(snapshot=[{"sleep": 3}, {}])
                p = self.start_with(first, "hourly")
                for _ in range(200):
                    if self.core_calls():
                        break
                    time.sleep(0.05)
                else:
                    self.fail("the first run never reached btrfs-snapshot-core")
                self.forget_calls()
                r = self.run_with(second, "hourly")
                self.assertEqual(
                    (r.returncode, r.stdout), (0, log(5, "another conversion/snapshot run is active; skipped") + "\n")
                )
                self.assertEqual(self.core_calls(), [])
                p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0)


class NasTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_HOST="nas", NAS_USER="u", NAS_PORT="22")
        self.write("etc/btrfs-setup/restic-password", "secret\n")
        self.snap = os.path.join(self.snapdir, SNAP)
        os.makedirs(self.snap)
        self.machine(latest={"home": self.snap})

    def restic_commands(self) -> list[object]:
        return [c[1] for c in self.calls("restic")]

    def test_the_records_of_one_version_count_in_the_other(self) -> None:
        for name, first, second in BOTH:
            with self.subTest(name):
                self.fresh()
                r = self.run_with(first, "nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.read_state("nas-done-home"), f"{SNAP} {REPO_ID}\n")
                self.assertEqual(self.read_state("nas-repo-id"), REPO_ID + "\n")
                self.forget_calls()
                r = self.run_with(second, "nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(log(6, f"NAS: {SNAP} is already in the repository"), r.stdout)
                # Prune and check ran in the first run: not due again, no records started over.
                self.assertEqual(self.restic_commands(), ["cat", "unlock", "features", "forget", "snapshots"])
                self.assertNotIn("records start over", r.stdout)

    def test_a_snapshot_pinned_by_one_version_is_kept_by_the_other(self) -> None:
        for name, first, second in BOTH:
            with self.subTest(name):
                self.fresh()
                self.machine(restic={"backup": {"sleep": 3}})
                p = self.start_with(first, "nas")
                pin = self.path("run/btrfs-snapshot/pinned")
                for _ in range(200):
                    if os.path.exists(pin) and any(c[:2] == ["restic", "backup"] for c in self.calls()):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("the NAS run never started its backup")
                self.core(list={"out": '{"sources": [{"name": "home", "snapshots": [{"path": "' + self.snap + '"}]}]}'})
                r = self.run_with(second, "purge")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(log(4, f"purge: {self.snap} is pinned (NAS backup running?); kept"), r.stdout)
                self.assertTrue(os.path.isdir(self.snap))
                p.communicate(timeout=60)
                self.assertFalse(os.path.exists(pin))
                self.machine(restic={})


if __name__ == "__main__":
    unittest.main()
