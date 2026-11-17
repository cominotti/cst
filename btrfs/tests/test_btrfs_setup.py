"""Unit tests for the timer removal paths of btrfs-setup.sh and
btrfs/setup-snapshots.bash (bash, so the functions are taken from the
scripts and run with systemctl and rm as shell functions that log; rm
removes only files under the test's folder). systemd keeps the last run of
a Persistent= timer (a stamp in /var/lib/systemd/timers) after the timer is
gone; `systemctl clean --what=state` removes it, and needs the unit loaded
and stopped: after `disable --now`, before the unit file goes.

Nothing reaches the real host: no sudo, no systemctl, writes only in a
temporary folder.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_btrfs_setup -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BTRFS = os.path.dirname(HERE)
REPO = os.path.dirname(BTRFS)
SETUP = os.path.join(REPO, "btrfs-setup.sh")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from btrfs.tests.test_snapshot_runtime import shell_function  # noqa: E402

# The log says, for each clean, whether the unit file was still there.
STUBS = """
set -Eeuo pipefail
systemctl() {
    echo "systemctl $*" >>"$LOG"
    if [[ "$1" == clean && -e "${UNIT_DIR}/${!#}" ]]; then echo "  (${!#} file present)" >>"$LOG"; fi
    [[ " $* " != *" ${FAIL:-none} "* ]]
}
rm() {
    echo "rm $*" >>"$LOG"
    local a
    for a in "$@"; do
        if [[ "$a" == "$T"/* ]]; then command rm -f -- "$a"; fi
    done
}
"""


def assignment(name: str) -> str:
    """The line (or lines, for an array) that sets NAME at the top of btrfs-setup.sh."""
    with open(SETUP) as f:
        text = f.read()
    m = re.search(rf"^{name}=(\(.*?\)|.*)$", text, re.M | re.S)
    if m is None:
        raise AssertionError(f"{name}= not found in {SETUP}")
    return m.group(0)


class TimerRemovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-btrfs-setup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.units = os.path.join(self.tmp, "units")
        self.stamps = os.path.join(self.tmp, "timers")
        os.makedirs(self.units)
        os.makedirs(self.stamps)
        self.log = os.path.join(self.tmp, "log")

    def touch(self, *paths: str) -> None:
        for p in paths:
            open(p, "w").close()

    def run_bash(self, body: str, fail: str = "none") -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                STUBS,
                shell_function(SETUP, "forget_timer_state"),
                f'UNIT_DIR="$T/units"; TIMER_STAMP_DIR="$T/timers"; SRC={BTRFS}; PROG=cst-btrfs-test',
                body,
            ]
        )
        r = subprocess.run(
            ["bash", "-c", script, "_"],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LOG": self.log,
                "T": self.tmp,
                "FAIL": fail,
                "TMPDIR": self.tmp,
            },
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def calls(self) -> list[str]:
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_old_units_are_stopped_forgotten_then_removed(self) -> None:
        service, timer = (os.path.join(self.units, f"btrfs-scrub-monthly.{k}") for k in ("service", "timer"))
        stamp = os.path.join(self.stamps, "stamp-btrfs-scrub-monthly.timer")
        self.touch(service, timer, stamp)
        self.run_bash(
            "\n".join([assignment("OLD_UNITS"), shell_function(SETUP, "remove_old_units"), "remove_old_units"])
        )
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now btrfs-scrub-monthly.service",
                f"rm -fv {service}",
                "systemctl disable --now btrfs-scrub-monthly.timer",
                "systemctl clean --what=state btrfs-scrub-monthly.timer",
                "  (btrfs-scrub-monthly.timer file present)",
                f"rm -fv {timer}",
                f"rm -fv {stamp}",
            ],
        )
        for p in (service, timer, stamp):
            self.assertFalse(os.path.exists(p), p)

    def test_a_stamp_whose_unit_is_already_gone_is_removed(self) -> None:
        # What an older install left on this machine: the unit files removed, the stamp not.
        stamp = os.path.join(self.stamps, "stamp-btrfs-scrub-monthly.timer")
        self.touch(stamp)
        body = "\n".join([assignment("OLD_UNITS"), shell_function(SETUP, "remove_old_units"), "remove_old_units"])
        self.run_bash(body)
        self.assertEqual(self.calls(), [f"rm -fv {stamp}"])
        self.assertFalse(os.path.exists(stamp))
        # A second run finds nothing left to change.
        os.unlink(self.log)
        self.run_bash(body)
        self.assertNotIn("systemctl", " ".join(self.calls()))

    def test_forget_timer_state_cleans_only_timers_and_ignores_a_failure(self) -> None:
        # On a service, --what=state would delete its StateDirectory.
        r = self.run_bash("forget_timer_state a.service b.timer docker-gc-recorder.service\necho went-on", fail="clean")
        self.assertEqual(self.calls(), ["systemctl clean --what=state b.timer"])
        self.assertIn("went-on", r.stdout)

    def test_uninstall_stops_every_timer_before_it_forgets_them(self) -> None:
        timers = re.findall(r"[\w.-]+\.timer", assignment("TIMERS"))
        self.assertGreaterEqual(len(timers), 2)
        self.touch(*(os.path.join(self.units, t) for t in timers))
        self.run_bash("\n".join([assignment("TIMERS"), shell_function(SETUP, "stop_monitoring"), "stop_monitoring"]))
        want = [f"systemctl disable --now {u}" for u in [*timers, "btrfs-stall-monitor.service"]]
        for t in timers:
            want += [f"systemctl clean --what=state {t}", f"  ({t} file present)"]
        want.append("systemctl stop btrfs-stall-alert.service")
        self.assertEqual(self.calls(), want)

    def test_docker_gc_disable_forgets_its_timer(self) -> None:
        self.touch(os.path.join(self.units, "docker-gc.timer"))
        body = [assignment("DOCKER_GC_UNITS"), shell_function(SETUP, "docker_gc"), "docker_gc disable"]
        r = self.run_bash("\n".join(body))
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now docker-gc-recorder.service docker-gc.service docker-gc.timer",
                "systemctl clean --what=state docker-gc.timer",
                "  (docker-gc.timer file present)",
            ],
        )
        self.assertIn("docker-gc disabled", r.stdout)

    def snapshots(self, call: str) -> list[str]:
        # Sourced the way the frontend does; the paths it writes point into the test's folder.
        self.touch(*(os.path.join(self.units, t) for t in ("btrfs-snapshot.timer", "btrfs-snapshot-nas.timer")))
        self.run_bash(
            "\n".join(
                [
                    '. "$SRC/setup-snapshots.bash"',
                    'SNAP_BOOT_FLAG="$T/convert-at-boot"; BULK_FLAG="$T/bulk-at-boot"; SNAP_CONF="$T/snapshots.conf"',
                    "target_user=u",
                    call,
                ]
            )
        )
        return [c for c in self.calls() if not c.startswith("rm ")]

    def test_snapshots_disable_forgets_both_timers(self) -> None:
        self.assertEqual(
            self.snapshots("snap_disable"),
            [
                "systemctl disable --now btrfs-snapshot.timer btrfs-snapshot-nas.timer btrfs-subvol-convert.service",
                "systemctl clean --what=state btrfs-snapshot.timer",
                "  (btrfs-snapshot.timer file present)",
                "systemctl clean --what=state btrfs-snapshot-nas.timer",
                "  (btrfs-snapshot-nas.timer file present)",
            ],
        )

    def test_nas_disable_forgets_its_timer(self) -> None:
        self.assertEqual(
            self.snapshots("snap_nas disable"),
            [
                "systemctl disable --now btrfs-snapshot-nas.timer",
                "systemctl clean --what=state btrfs-snapshot-nas.timer",
                "  (btrfs-snapshot-nas.timer file present)",
            ],
        )
        with open(os.path.join(self.tmp, "snapshots.conf")) as f:
            self.assertIn("NAS_ENABLED=0\n", f.read())


class StallMonitorUnitTest(unittest.TestCase):
    """btrfs/units/btrfs-stall-monitor.service as shipped."""

    def service_lines(self) -> list[str]:
        with open(os.path.join(BTRFS, "units", "btrfs-stall-monitor.service")) as f:
            text = f.read()
        m = re.search(r"^\[Service\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
        assert m is not None, text
        return [ln for ln in m.group(1).splitlines() if ln and not ln.startswith("#")]

    def test_journal_lines_carry_the_daemon_name(self) -> None:
        # Not python3, the program systemd starts.
        self.assertIn("SyslogIdentifier=btrfs-stall-monitord", self.service_lines())

    def test_queued_alerts_survive_a_restart(self) -> None:
        # alert-*.txt waits in the RuntimeDirectory for btrfs-stall-alert.service; with the default
        # (no) a restart after a crash removed it.
        lines = self.service_lines()
        self.assertIn("RuntimeDirectory=btrfs-stall-monitor", lines)
        self.assertIn("Restart=on-failure", lines)
        self.assertIn("RuntimeDirectoryPreserve=restart", lines)


if __name__ == "__main__":
    unittest.main()
