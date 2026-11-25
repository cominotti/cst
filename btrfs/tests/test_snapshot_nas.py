# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests for btrfs/btrfs-snapshot nas, the daily backup of the newest
/home snapshots to the NAS with restic over SFTP (no root, no NAS needed).

The whole bash program runs on the fake machine of snapshot_fakes.py: sftp
answers with an exit status, restic records its arguments, its RESTIC_*
variables and the pin file as each call saw it, and answers per command
(cat config: the repository's id; features: device-id-for-hardlinks as
alpha). The tests pin the order of the calls, the restic command lines and
environment, the pin file, the bind mounts, the state files under
/var/lib/btrfs-snapshot (nas-done-*, nas-last-ok, nas-last-prune,
nas-last-check, nas-check-failures, nas-repo-id, nas-summary,
nas-enabled-at), and the alerts.

Run with: invowk cmd test module btrfs.tests.test_snapshot_nas
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import time
from typing import override
import unittest
from unittest import mock

from btrfs.snapshot_runtime.adapters.hourly import RunStopped
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
from btrfs.snapshot_runtime.adapters.nas import Nas
from btrfs.tests.snapshot_fakes import SCRIPT, RuntimeTest, log, read
from cst_core.adapters.runner import SubprocessCommandRunner

ALIAS = "btrfs-setup-nas"
REPO_ID = "ab" * 32
SNAP = "home.20261118T0905"
DAY = 86400
PAST_64_BITS = "a number past 2**63 - 1 (bash would wrap it): not wrapped; ignored"


class Base(RuntimeTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_HOST="nas", NAS_USER="u", NAS_PORT="22")
        self.password = self.write("etc/btrfs-setup/restic-password", "secret\n")
        self.snap = os.path.join(self.snapdir, SNAP)
        self.machine(latest={"home": self.snap})
        self.mnt = self.path("run/btrfs-snapshot/nas/home")
        self.repo = f"sftp:{ALIAS}:/volume1/restic"
        self.restic_env = {
            "RESTIC_CACHE_DIR": self.path("var/cache/btrfs-snapshot-restic"),
            "RESTIC_PASSWORD_FILE": self.password,
            "RESTIC_REPOSITORY": self.repo,
        }

    def restic(self, **answers: object) -> None:
        """restic's answers per command: {"rc": .., "out": .., "err": .., "sleep": ..}, or a queue of them."""
        self.machine(restic=answers)

    def restic_calls(self) -> list[list[object]]:
        """restic's arguments, one list per call."""
        return [c[1:-2] for c in self.calls("restic")]

    def restic_commands(self) -> list[object]:
        return [c[1] for c in self.calls("restic")]

    def nas(self) -> tuple[int, list[str], str]:
        self.forget_calls()
        r = self.run_prog("nas")
        return r.returncode, r.stdout.splitlines(), r.stderr

    def days_ago(self, name: str, days: float) -> None:
        self.write(self.state_file(name), f"{int(time.time() - days * DAY)}\n")

    def backup_args(self, name: str = "home", when: str = "2026-11-18 09:05:00") -> list[object]:
        return [
            "backup",
            "--host",
            "testhost",
            "--tag",
            f"btrfs-setup,{name}",
            "--time",
            when,
            "--one-file-system",
            "--exclude-caches",
            "--skip-if-unchanged",
            self.path(f"run/btrfs-snapshot/nas/{name}"),
        ]

    def stale(self, body: str, days: int = 8) -> list[object]:
        return [
            "-u",
            "normal",
            "-a",
            "btrfs-snapshot",
            f"NAS backup is {days} days old",
            f"{body} Local snapshots are unaffected.",
        ]


class GateTest(Base):
    """What stops the run before it talks to the NAS."""

    def test_not_enabled(self) -> None:
        self.conf(NAS_ENABLED="0")
        self.assertEqual(self.nas(), (0, [log(6, "NAS backup not enabled")], ""))
        self.assertEqual(self.calls_but_hostname(), [])

    def test_restic_not_installed_yet(self) -> None:
        os.remove(os.path.join(self.bin, "restic"))
        self.assertEqual(
            self.nas(),
            (0, [log(5, "NAS: restic is not installed yet (rpm-ostree: active after the next reboot); skipped")], ""),
        )

    def test_not_configured_completely(self) -> None:
        for settings, password in (({"NAS_PATH": ""}, "secret\n"), ({"NAS_PATH": "/volume1/restic"}, "")):
            with self.subTest(settings=settings, password=password):
                self.conf(NAS_ENABLED="1", **settings)
                self.write(self.password, password)
                rc, lines, _ = self.nas()
                self.assertEqual(
                    (rc, lines),
                    (
                        0,
                        [
                            log(
                                4,
                                f"NAS: not configured completely (NAS_PATH, {self.password}); re-run 'cst btrfs snapshots nas setup'",
                            )
                        ],
                    ),
                )
                self.assertEqual(self.calls("restic"), [])

    def test_the_staleness_clock_starts_when_nothing_recorded_it(self) -> None:
        self.conf(NAS_ENABLED="1", NAS_PATH="")
        self.nas()
        self.assertLess(abs(int(self.read_state("nas-enabled-at")) - time.time()), 120)
        self.assertEqual(self.notified(), [])
        self.days_ago("nas-enabled-at", 8)
        self.nas()
        self.assertEqual(self.notified(), [self.stale("The NAS backup is not configured completely.")])

    def test_the_later_of_the_last_success_and_the_enabling_counts(self) -> None:
        self.conf(NAS_ENABLED="1", NAS_PATH="")
        self.days_ago("nas-enabled-at", 30)
        self.days_ago("nas-last-ok", 3)
        self.nas()
        self.assertEqual(self.notified(), [])
        self.days_ago("nas-last-ok", 9)
        self.nas()
        self.assertEqual(self.notified(), [self.stale("The NAS backup is not configured completely.", 9)])

    def test_a_last_success_past_int_s_digit_limit_wraps_as_bash(self) -> None:
        # isascii() and isdigit() took 5000 digits, and int() raised ValueError out of the staleness check.
        # bash's (( v > best )) reads them wrapped at 64 bits: 8198552921648689607, a date long after
        # now, so the backup is never stale (before this, the value was skipped and the enabling counted).
        self.conf(NAS_ENABLED="1", NAS_PATH="")
        self.days_ago("nas-enabled-at", 3)
        self.write(self.state_file("nas-last-ok"), "1" * 5000 + "\n")
        rc, _lines, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.notified(), [])
        self.days_ago("nas-enabled-at", 9)
        self.nas()
        self.assertEqual(self.notified(), [])

    def test_the_staleness_files_are_read_as_bash_arithmetic(self) -> None:
        # The old nas_since compared them in (( v > best )): a leading 0 is octal, 08 an error (skipped),
        # a value past 2**63 - 1 wraps around to a negative one (skipped). Main read them as decimal.
        body = "The NAS backup is not configured completely."
        self.conf(NAS_ENABLED="1", NAS_PATH="")
        with self.subTest("octal"):
            # 9 days ago, written in octal: bash's 9 days (main: the octal digits as a decimal, a date
            # after now, no alert).
            self.write(self.state_file("nas-last-ok"), "0" + format(int(time.time() - 9 * DAY), "o") + "\n")
            rc, _lines, err = self.nas()
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.notified(), [self.stale(body, 9)])
        for text in ("08", "9223372036854775808"):
            with self.subTest(text):
                # Skipped: the staleness clock starts now (main: 8, a stale alert from 1970; 2**63, no
                # alert and no start).
                self.forget_calls()
                self.write(self.state_file("nas-last-ok"), text + "\n")
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.state_file("nas-enabled-at"))
                rc, _lines, err = self.nas()
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.notified(), [])
                self.assertLess(abs(int(self.read_state("nas-enabled-at")) - time.time()), 120)

    def test_another_nas_run_holds_its_lock(self) -> None:
        with self.hold_lock("run/btrfs-snapshot-nas.lock"):
            self.assertEqual(self.nas(), (0, [log(5, "another NAS run is active; skipped")], ""))
        self.assertEqual(self.calls("restic"), [])

    def test_nas_unreachable(self) -> None:
        self.machine(sftp_rc=255)
        rc, lines, _ = self.nas()
        self.assertEqual((rc, lines), (0, [log(5, f"NAS u@nas:22 unreachable (ssh {ALIAS}); skipping this run")]))
        self.assertEqual(self.calls("sftp"), [["sftp", "-q", "-b", "-", ALIAS, "pwd\n"]])
        self.assertEqual(self.calls("restic"), [])


class BackupTest(Base):
    def test_a_whole_run(self) -> None:
        summary = [
            {
                "paths": [self.mnt],
                "time": "2026-11-18T09:05:00.123Z",
                "summary": {
                    "total_bytes_processed": 2 * 2**30,
                    "total_files_processed": 10,
                    "data_added_packed": 5 * 2**20,
                },
            }
        ]
        self.restic(
            backup={"out": "Files: 10 new\nWarning: at least one source file could not be read\n"},
            snapshots={"out": json.dumps(summary)},
        )
        rc, lines, err = self.nas()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            lines,
            [
                log(5, f"NAS: backing up {SNAP} (read at {self.mnt})"),
                log(6, "restic: Files: 10 new"),
                log(4, "restic: Warning: at least one source file could not be read"),
                log(5, f"NAS backup ok: {SNAP}"),
            ],
        )
        self.assertEqual(
            self.restic_calls(),
            [
                ["cat", "config"],
                ["unlock"],
                ["features"],
                self.backup_args(),
                [
                    "forget",
                    "--host",
                    "testhost",
                    "--tag",
                    "btrfs-setup",
                    "--keep-daily",
                    "14",
                    "--keep-weekly",
                    "8",
                    "--keep-monthly",
                    "12",
                    "--prune",
                ],
                ["check", "--read-data-subset=5%"],
                ["snapshots", "--json", "--latest", "1", "--host", "testhost", "--tag", "btrfs-setup"],
            ],
        )
        calls = self.calls("restic")
        for call in calls:
            with self.subTest(call[1]):
                env = call[-2]
                if not isinstance(env, dict):
                    self.fail(f"no environment recorded: {call}")
                extra = {"RESTIC_FEATURES": "device-id-for-hardlinks"} if call[1] == "backup" else {}
                # The check also gets the file its output is copied to (RESTIC_CAPTURE, a
                # variable of the script that bash exports to the commands of restic_run).
                if call[1] == "check":
                    capture = str(env["RESTIC_CAPTURE"])
                    self.assertTrue(capture.startswith(self.env["TMPDIR"] + "/tmp."), capture)
                    self.assertFalse(os.path.exists(capture))
                    extra = {"RESTIC_CAPTURE": capture}
                self.assertEqual(env, {**self.restic_env, **extra})
        # The snapshot was pinned while it was read, and unpinned after.
        self.assertEqual(calls[3][-1], self.snap + "\n")
        self.assertFalse(os.path.exists(self.path("run/btrfs-snapshot/pinned")))
        self.assertEqual(
            [c for c in self.calls() if c[0] in ("mount", "umount")],
            [["mount", "--bind", "-o", "ro,nosuid,nodev", self.snap, self.mnt], ["umount", self.mnt]],
        )
        self.assertFalse(os.path.exists(self.mnt))
        # The repository's variables are exported once the NAS part starts: every command after
        # that gets them (the core and mount too), nothing else is added.
        names = [c[0] for c in self.calls()]
        envs = self.call_envs()
        start = names.index("sftp")
        self.assertEqual([e for e in envs[:start] if e], [])
        for name, env in zip(names[start:], envs[start:], strict=True):
            if name != "restic":
                self.assertEqual(env, self.restic_env, name)
        # Modes under a service's umask: the run folder 0755, each mount folder 0700.
        self.assertEqual(self.mode("run/btrfs-snapshot"), 0o755)
        self.assertEqual(self.described().get("mount_modes"), {self.mnt: "700"})
        self.assertEqual(
            self.core_calls(),
            [["latest", "--snapdir", self.snapdir, "--name", "home", "--source-path", f"{self.top}/home"]],
        )
        self.assertEqual(self.read_state("nas-done-home"), f"{SNAP} {REPO_ID}\n")
        self.assertEqual(self.read_state("nas-repo-id"), REPO_ID + "\n")
        self.assertEqual(
            self.read_state("nas-summary"), "home: 2026-11-18 09:05, 2.0 GiB in 10 files, 5 MiB new on the NAS\n"
        )
        for name in ("nas-last-ok", "nas-last-prune", "nas-last-check"):
            self.assertLess(abs(int(self.read_state(name)) - time.time()), 120, name)

    def test_workspace_is_a_second_restic_snapshot(self) -> None:
        ws = os.path.join(self.home, "Workspace")
        os.makedirs(ws)
        self.mark_subvol(ws)
        self.machine(latest={"home": self.snap, "Workspace": os.path.join(self.snapdir, "Workspace.20261118T0905_1")})
        rc, lines, _ = self.nas()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP} Workspace.20261118T0905_1"))
        backups = [c for c in self.restic_calls() if c[0] == "backup"]
        self.assertEqual(backups, [self.backup_args(), self.backup_args("Workspace")])
        self.assertEqual(self.calls("restic")[3][-1], f"{self.snap}\n{self.snapdir}/Workspace.20261118T0905_1\n")

    def test_a_snapshot_already_in_this_repository_is_not_sent_again(self) -> None:
        self.write(self.state_file("nas-repo-id"), REPO_ID + "\n")
        self.write(self.state_file("nas-done-home"), f"{SNAP} {REPO_ID}\n")
        _rc, lines, _ = self.nas()
        self.assertEqual(lines[0], log(6, f"NAS: {SNAP} is already in the repository"))
        self.assertNotIn("backup", self.restic_commands())
        self.assertEqual(self.calls("mount"), [])

    def test_another_repository_starts_its_records_over(self) -> None:
        old = "cd" * 32
        self.write(self.state_file("nas-repo-id"), old + "\n")
        for name in ("nas-done-home", "nas-summary", "nas-last-prune", "nas-last-check", "nas-check-failures"):
            self.write(self.state_file(name), f"{SNAP} {old}\n" if name == "nas-done-home" else "1\n")
        # Pruned and checked just now, in the old repository: only forgetting it makes both due.
        self.days_ago("nas-last-prune", 0)
        self.days_ago("nas-last-check", 0)
        # The record of a source this run does not back up goes too.
        self.write(self.state_file("nas-done-Workspace"), f"Workspace.20261118T0905 {old}\n")
        _rc, lines, _ = self.nas()
        self.assertEqual(
            lines[0], log(5, "NAS: another repository than last time (id cdcdcdcd -> abababab); its records start over")
        )
        self.assertIn(self.backup_args(), self.restic_calls())
        self.assertIn("--prune", self.restic_calls()[4])
        self.assertIn("check", self.restic_commands())
        self.assertFalse(os.path.exists(self.state_file("nas-done-Workspace")))
        self.assertEqual(self.read_state("nas-done-home"), f"{SNAP} {REPO_ID}\n")
        self.assertFalse(os.path.exists(self.state_file("nas-check-failures")))

    def test_records_without_a_repository_id_start_over_with_a_line(self) -> None:
        # nas-repo-id gone (removed by hand, or a run killed in between) while records stay: they
        # cannot be told apart from another repository's, so they go, and now the journal says so.
        # Before, they went without a word. A first run (no records) says nothing (test_a_whole_run).
        self.write(self.state_file("nas-done-home"), f"{SNAP} {REPO_ID}\n")
        self.days_ago("nas-last-prune", 1)
        rc, lines, _ = self.nas()
        self.assertEqual(rc, 0)
        self.assertEqual(
            lines[0],
            log(
                5,
                f"NAS: {self.state}/nas-repo-id is missing, so the records of the NAS backup are not known to be "
                "this repository's; they start over",
            ),
        )
        self.assertIn(self.backup_args(), self.restic_calls())
        self.assertIn("--prune", next(c for c in self.restic_calls() if c[0] == "forget"))
        self.assertEqual(self.read_state("nas-repo-id"), REPO_ID + "\n")

    def test_a_repository_id_that_cannot_be_read_never_counts_a_record(self) -> None:
        self.write(self.state_file("nas-done-home"), f"{SNAP} \n")
        self.restic(cat={"out": "{}\n"})
        self.nas()
        self.assertIn("backup", self.restic_commands())
        self.assertEqual(self.read_state("nas-done-home"), f"{SNAP} \n")
        self.assertFalse(os.path.exists(self.state_file("nas-repo-id")))

    def test_restic_without_the_feature_flag(self) -> None:
        for out in ("Name  Type  Default  Description\ndevice-id-for-hardlinks  stable  true  x\n", ""):
            with self.subTest(out):
                self.restic(features={"out": out})
                for name in ("nas-done-home", "nas-repo-id"):
                    if os.path.exists(self.state_file(name)):
                        os.remove(self.state_file(name))
                self.nas()
                backup = next(c for c in self.calls("restic") if c[1] == "backup")
                self.assertEqual(backup[-2], self.restic_env)

    def test_a_failed_backup_is_retried_next_run(self) -> None:
        for rc, line in (
            (1, "NAS: home: restic backup failed (rc=1)"),
            (124, "NAS: home: stopped at the time limit (NAS_TIMEOUT=6h); the uploaded data is reused next run"),
        ):
            with self.subTest(rc):
                self.restic(backup={"rc": rc})
                status, lines, _ = self.nas()
                self.assertEqual(status, 0)
                self.assertEqual(
                    lines[1:],
                    [log(4, line), log(4, "NAS backup failed for 1 of 1 source(s); will retry next run")],
                )
                self.assertEqual(self.restic_commands(), ["cat", "unlock", "features", "backup"])
                self.assertFalse(os.path.exists(self.state_file("nas-last-ok")))
                self.assertFalse(os.path.exists(self.state_file("nas-done-home")))
                self.assertFalse(os.path.exists(self.path("run/btrfs-snapshot/pinned")))

    def test_files_that_could_not_be_read_still_count_as_backed_up(self) -> None:
        self.restic(backup={"rc": 3})
        _rc, lines, _ = self.nas()
        self.assertIn(log(4, "NAS: home: some files could not be read; the rest was backed up"), lines)
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))
        self.assertTrue(os.path.exists(self.state_file("nas-done-home")))

    def test_a_long_failure_alerts_as_stale(self) -> None:
        self.days_ago("nas-last-ok", 8)
        self.restic(backup={"rc": 1})
        self.nas()
        self.assertEqual(self.notified(), [self.stale("Last attempt failed; see journalctl -u btrfs-snapshot-nas.")])

    def test_a_success_clears_the_nas_alerts(self) -> None:
        for name in ("alerted-nas", "alerted-nas-password"):
            self.write(self.state_file(name), "")
        self.nas()
        self.assertFalse(os.path.exists(self.state_file("alerted-nas")))
        self.assertFalse(os.path.exists(self.state_file("alerted-nas-password")))

    def test_no_snapshot_to_back_up(self) -> None:
        self.machine(latest={})
        rc, lines, _ = self.nas()
        self.assertEqual((rc, lines), (0, [log(4, "NAS: no snapshot of home to back up")]))
        self.assertEqual(self.restic_commands(), ["cat", "unlock"])
        # The pin file (an empty line) went again with the run.
        self.assertEqual(self.calls("mount"), [])
        self.assertFalse(os.path.exists(self.path("run/btrfs-snapshot/pinned")))

    def test_the_time_budget(self) -> None:
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT="1m")
        _rc, lines, _ = self.nas()
        self.assertEqual(
            lines,
            [
                log(4, "NAS: home: no time left in this run (NAS_TIMEOUT=1m)"),
                log(4, "NAS backup failed for 1 of 1 source(s); will retry next run"),
            ],
        )
        # Unreadable: 6 hours.
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT="soon")
        _rc, lines, _ = self.nas()
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))

    def test_a_time_budget_with_a_blank_beside_it_is_named_and_reads_as_bash_reads_it(self) -> None:
        # The line keeps bash's value ("1m" and a no-break space), which is no duration: the 6 hours, as
        # for any value it cannot read, but after a warning that names the line.
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT="1m\N{NO-BREAK SPACE}")
        _rc, lines, err = self.nas()
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))
        self.assertIn(
            "NAS_TIMEOUT='1m\N{NO-BREAK SPACE}': a no-break space (U+00A0) at the end of the value: CST does not "
            "trim it (delete it); kept as bash reads it, with the blank (fix the line)",
            err,
        )

    def test_a_stale_limit_past_64_bits_is_a_problem_line_and_the_seven_days(self) -> None:
        # bash wrapped 2**63 around to -2**63, so every failed run alerted as stale; the runtime names the
        # line and holds at the default 7 days.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_STALE_DAYS="9223372036854775808")
        self.days_ago("nas-last-ok", 1)
        self.restic(backup={"rc": 1})
        _rc, _lines, err = self.nas()
        self.assertIn(f"{conf} line 6: NAS_STALE_DAYS='9223372036854775808': {PAST_64_BITS}", err)
        self.assertEqual(self.notified(), [])
        self.days_ago("nas-last-ok", 8)
        self.nas()
        self.assertEqual(self.notified(), [self.stale("Last attempt failed; see journalctl -u btrfs-snapshot-nas.")])

    def test_a_time_budget_past_64_bits_is_a_problem_line_and_the_six_hours(self) -> None:
        # Seconds past 2**63 - 1 (2562047788015216h), or a run's deadline past it (now + 2**63 - 1 seconds):
        # the line is named and the run has its 6 hours.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        for value in ("2562047788015216h", "9223372036854775807", "106751991167301d"):
            with self.subTest(value):
                self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT=value)
                _rc, lines, err = self.nas()
                self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))
                self.assertIn(f"{conf} line 6: NAS_TIMEOUT='{value}': {PAST_64_BITS}", err)

    def test_nas_numbers_that_fit_in_64_bits_are_no_problem_line(self) -> None:
        self.conf(
            NAS_ENABLED="1",
            NAS_PATH="/volume1/restic",
            NAS_STALE_DAYS="9223372036854775807",
            NAS_PRUNE_EVERY_DAYS="106751991167300",
            NAS_CHECK_EVERY_DAYS="106751991167300",
            NAS_TIMEOUT="100000d",
        )
        rc, lines, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))
        self.assertNotIn("2**63", err)

    def test_a_time_budget_bash_arithmetic_refuses_stops_the_run(self) -> None:
        # 08 is no number for bash (a leading 0 is octal): its run stopped there with exit 1, after
        # its exit trap had unmounted (found by the parity harness of the port; 010 is 8 seconds).
        for value in ("08", "08m"):
            with self.subTest(value):
                self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT=value)
                rc, lines, _ = self.nas()
                self.assertEqual((rc, lines), (1, []))
                self.assertEqual(self.calls_but_hostname(), [])
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT="010")
        rc, lines, _ = self.nas()
        self.assertEqual(
            lines[-2:],
            [
                log(4, "NAS: home: no time left in this run (NAS_TIMEOUT=010)"),
                log(4, "NAS backup failed for 1 of 1 source(s); will retry next run"),
            ],
        )

    def test_the_hourly_run_holding_the_lock(self) -> None:
        with self.hold_lock():
            p = self.start_prog("nas")
            time.sleep(1)
            self.assertIsNone(p.poll())
            self.assertEqual(self.core_calls(), [])
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out.splitlines()[-1], log(5, f"NAS backup ok: {SNAP}"))


class RepositoryTest(Base):
    def test_no_repository_is_never_created_on_its_own(self) -> None:
        self.restic(cat={"rc": 10, "out": "Fatal: repository does not exist\n"})
        _rc, lines, _ = self.nas()
        self.assertEqual(
            lines,
            [
                log(
                    4,
                    f"NAS: no restic repository at {self.repo}; not creating one on its own (re-run 'cst btrfs snapshots nas setup')",
                )
            ],
        )
        self.assertEqual(self.restic_commands(), ["cat"])

    def test_one_that_setup_asked_for_is_created(self) -> None:
        self.write(self.state_file("nas-init-pending"), "")
        config = json.dumps({"id": REPO_ID}) + "\n"
        self.restic(cat=[{"rc": 10}, {"out": config}], init={"out": "created restic repository\n"})
        _rc, lines, _ = self.nas()
        title = "NAS backup: repository created"
        body = (
            f"Encrypted with the password in {self.password}: store a copy in your password manager now "
            f"(sudo cat {self.password}); without it the backups cannot be read."
        )
        self.assertEqual(
            lines[:3],
            [
                log(5, f"NAS: creating the restic repository {self.repo} (requested by 'snapshots nas setup')"),
                log(6, "restic: created restic repository"),
                log(4, f"{title}: {body}"),
            ],
        )
        self.assertEqual(self.notified()[0], ["-u", "normal", "-a", "btrfs-snapshot", title, body])
        self.assertEqual(self.restic_commands()[:4], ["cat", "init", "cat", "unlock"])
        self.assertFalse(os.path.exists(self.state_file("nas-init-pending")))
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))

    def test_a_failed_creation(self) -> None:
        self.write(self.state_file("nas-init-pending"), "")
        self.restic(cat={"rc": 10}, init={"rc": 1, "out": "Fatal: create repository failed\n"})
        _rc, lines, _ = self.nas()
        self.assertEqual(
            lines[1:], [log(4, "restic: Fatal: create repository failed"), log(4, "NAS: restic init failed")]
        )
        self.assertTrue(os.path.exists(self.state_file("nas-init-pending")))

    def test_wrong_password(self) -> None:
        self.restic(cat={"rc": 12})
        _rc, lines, _ = self.nas()
        title, body = (
            "NAS backup: wrong repository password",
            f"{self.password} does not open {self.repo}; put the saved password there.",
        )
        self.assertEqual(lines, [log(4, f"{title}: {body}")])
        self.assertEqual(self.notified(), [["-u", "critical", "-a", "btrfs-snapshot", title, body]])

    def test_cannot_open(self) -> None:
        self.restic(cat={"rc": 1, "out": "Fatal: unable to open config file\n"})
        _rc, lines, _ = self.nas()
        self.assertEqual(
            lines, [log(4, "NAS: cannot open the repository (restic rc=1): Fatal: unable to open config file")]
        )


class LeftoverMountTest(Base):
    def test_a_leftover_mount_that_stays_blocks_the_run_and_keeps_the_pin(self) -> None:
        os.makedirs(self.mnt)
        self.write("run/btrfs-snapshot/pinned", "/old/snapshot\n")
        self.machine(mountpoints=[self.mnt], stuck=[self.mnt])
        rc, lines, _ = self.nas()
        self.assertEqual(
            (rc, lines),
            (
                0,
                [
                    log(
                        4,
                        f"NAS: cannot unmount {self.mnt}; the pinned snapshots stay pinned, and no backup starts until it is unmounted",
                    )
                ],
            ),
        )
        self.assertEqual(len(self.calls("umount")), 1)
        self.assertEqual(read(self.path("run/btrfs-snapshot/pinned")), "/old/snapshot\n")
        self.assertEqual(self.calls("sftp"), [])

    def test_a_leftover_mount_that_goes_is_cleaned_up_first(self) -> None:
        stacked = self.path("run/btrfs-snapshot/nas/Workspace")
        os.makedirs(stacked)
        self.machine(mountpoints=[stacked])
        _rc, lines, _ = self.nas()
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))
        self.assertEqual(self.calls("umount")[0], ["umount", stacked])
        self.assertFalse(os.path.exists(stacked))

    def test_a_mount_that_cannot_be_made(self) -> None:
        self.machine(mount_rc=32)
        _rc, lines, _ = self.nas()
        self.assertEqual(lines[0], log(4, f"NAS: home: cannot bind-mount {self.snap} at {self.mnt}"))
        self.assertNotIn("backup", self.restic_commands())

    def test_a_mount_that_cannot_be_undone_keeps_the_pin(self) -> None:
        self.machine(stuck=[self.mnt])
        rc, lines, _ = self.nas()
        self.assertEqual(rc, 0)
        self.assertIn(
            log(
                4,
                f"NAS: cannot unmount {self.mnt}; the pinned snapshots stay pinned, and no backup starts until it is unmounted",
            ),
            lines,
        )
        self.assertEqual(read(self.path("run/btrfs-snapshot/pinned")), self.snap + "\n")
        self.assertEqual(self.mode("run/btrfs-snapshot/pinned"), 0o644)

    def test_stopped_by_systemd_it_unmounts_and_unpins(self) -> None:
        self.restic(backup={"sleep": 2})
        p = self.start_prog("nas")
        for _ in range(100):
            if any(c[0] == "mount" for c in self.calls()):
                break
            time.sleep(0.05)
        else:
            self.fail("the run never reached its mount")
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=60)
        self.assertEqual(p.returncode, 143)
        self.assertEqual(self.calls("umount")[-1], ["umount", self.mnt])
        self.assertFalse(os.path.exists(self.path("run/btrfs-snapshot/pinned")))
        self.assertFalse(os.path.exists(self.state_file("nas-last-ok")))

    def test_ctrl_c_and_hangup_also_exit_143(self) -> None:
        # Bug (phase 1 list, minor): one trap for TERM, INT and HUP, so Ctrl-C and a hangup
        # end the run with 143 (TERM) instead of 130 and 129. The cleanup itself runs.
        for sig in (signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig):
                self.forget_calls()
                self.restic(backup={"sleep": 2})
                p = self.start_prog("nas")
                for _ in range(100):
                    if any(c[0] == "mount" for c in self.calls()):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("the run never reached its mount")
                p.send_signal(sig)
                p.communicate(timeout=60)
                self.assertEqual(p.returncode, 143)
                self.assertFalse(os.path.exists(self.path("run/btrfs-snapshot/pinned")))


class DueTest(unittest.TestCase):
    """Nas.due against the old bash's due() (git show 1fdafd7a^:btrfs/btrfs-snapshot, lines 1109-1115),
    copied below with $(date +%s) replaced by a fixed NOW, case by case: due, not due, or the run
    stopped (set -u: a name). One bash process; each case in a subshell, so a stop ends only its case."""

    NOW = 1764000000
    BASH = """set -Euo pipefail
NOW="$1"
due() {
    local last
    last="$(cat "$1" 2>/dev/null)" || return 0
    [[ "${last}" =~ ^[0-9]+$ ]] || return 0
    (( NOW - last >= $2 * 86400 ))
}
while IFS= read -r -d '' last && IFS= read -r -d '' days; do
    printf '%s' "${last}" > "$3"
    ( if due "$3" "${days}"; then echo due; else echo notdue; fi ) 2>/dev/null || echo stop
done < "$2"
"""
    LASTS = (
        str(NOW - 1000),
        "0",
        str(2**63),
        str(2**64 - 1),
        "99999999999999999999",
        "1" * 5000,
        "9" * 4301,
        "08",
        "0777",
        "\N{ARABIC-INDIC DIGIT THREE}",
        "",
        " 1",
    )
    DAYS = (
        "7",
        "0",
        "1",
        "106751991167300",
        "213503982334601",
        "08",
        "x",
        "9223372036854775807",
        "1+1",
        "",
        "3-1",
        "(1+1)",
        "1 + 1",
        " 7 ",
        "-1",
        "2#1",
        "1*2",
        "x+1",
        "1+x",
    )

    def test_due_decides_as_the_old_bash(self) -> None:
        cases = [(last, days) for last in self.LASTS for days in self.DAYS]
        with tempfile.TemporaryDirectory() as tmp:
            pairs = os.path.join(tmp, "pairs")
            with open(pairs, "w", encoding="utf-8") as f:
                f.write("".join(f"{last}\0{days}\0" for last, days in cases))
            r = subprocess.run(
                ["/usr/bin/bash", "-c", self.BASH, "_", str(self.NOW), pairs, os.path.join(tmp, "bash-last")],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                check=True,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                timeout=120,
            )
            bash = r.stdout.splitlines()
            self.assertEqual(len(bash), len(cases))
            m = SnapshotRun({"CST_SNAPSHOT_ROOT": tmp}, SubprocessCommandRunner())
            nas = Nas(m)
            path = os.path.join(tmp, "nas-last-prune")
            for (last, days), want in zip(cases, bash, strict=True):
                with self.subTest(last=last[:24], digits=len(last), days=days):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(last)
                    m.settings["NAS_PRUNE_EVERY_DAYS"] = days
                    with (
                        mock.patch.object(SnapshotRun, "now", return_value=self.NOW),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        try:
                            got = "due" if nas.due(path, "NAS_PRUNE_EVERY_DAYS") else "notdue"
                        except RunStopped:
                            got = "stop"
                    self.assertEqual(got, want)


class MaintenanceTest(Base):
    def known_repository(self) -> None:
        """The records are of this repository (else the run starts them over)."""
        self.write(self.state_file("nas-repo-id"), REPO_ID + "\n")

    def test_prune_and_check_only_when_due(self) -> None:
        self.known_repository()
        self.days_ago("nas-last-prune", 6.9)
        self.days_ago("nas-last-check", 29.9)
        self.nas()
        self.assertEqual(self.restic_commands()[3:], ["backup", "forget", "snapshots"])
        self.assertNotIn("--prune", self.restic_calls()[4])
        self.days_ago("nas-last-prune", 7)
        self.days_ago("nas-last-check", 30)
        self.nas()
        # The snapshot is in the repository now: no backup, the maintenance all the same.
        self.assertEqual(self.restic_commands()[3:], ["forget", "check", "snapshots"])
        self.assertIn("--prune", self.restic_calls()[3])

    def test_an_interval_is_put_into_the_arithmetic_as_text(self) -> None:
        # bash's (( NOW - last >= DAYS * 86400 )) took DAYS as text: "1+1" is 1 + 86400 seconds, so a
        # prune 1.5 days old is due (the runtime read 2 days: not due).
        self.known_repository()
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_PRUNE_EVERY_DAYS="1+1")
        self.days_ago("nas-last-prune", 1.5)
        rc, _, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertIn("--prune", self.restic_calls()[4])

    def test_an_interval_past_64_bits_is_a_problem_line_and_the_default(self) -> None:
        # bash's DAYS * 86400 wrapped past 2**63 - 1: 106751991167301 days to a negative number and 2**62
        # days to 0, so a prune and a check at every run. The runtime names each line and keeps the
        # defaults, 7 and 30 days: neither is due yet.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.known_repository()
        self.conf(
            NAS_ENABLED="1",
            NAS_PATH="/volume1/restic",
            NAS_PRUNE_EVERY_DAYS="106751991167301",
            NAS_CHECK_EVERY_DAYS="4611686018427387904",
        )
        self.days_ago("nas-last-prune", 6.9)
        self.days_ago("nas-last-check", 29.9)
        rc, _, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.restic_commands()[3:], ["backup", "forget", "snapshots"])
        self.assertNotIn("--prune", self.restic_calls()[4])
        self.assertIn(f"{conf} line 6: NAS_PRUNE_EVERY_DAYS='106751991167301': {PAST_64_BITS}", err)
        self.assertIn(f"{conf} line 7: NAS_CHECK_EVERY_DAYS='4611686018427387904': {PAST_64_BITS}", err)

    def test_a_check_interval_past_64_bits_alerts_after_two_default_intervals(self) -> None:
        # A check that keeps not completing alerts at twice the interval: 60 days with the default 30, not
        # never (twice 2**62 days).
        self.known_repository()
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_CHECK_EVERY_DAYS="4611686018427387904")
        self.days_ago("nas-last-check", 60)
        self.restic(check={"rc": 11})
        self.nas()
        self.assertEqual(
            [a[4:] for a in self.notified()],
            [
                [
                    "NAS backup: repository not checked for 60 days",
                    "restic check did not complete at the last attempts; see journalctl -u btrfs-snapshot-nas.",
                ]
            ],
        )

    def test_a_last_prune_bash_cannot_read_is_not_due_before_its_interval_is_read(self) -> None:
        # bash failed on NOW - 08 first, so a name in NAS_PRUNE_EVERY_DAYS was never read: no prune, and
        # the run goes on (the runtime read the interval first and stopped).
        self.known_repository()
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_PRUNE_EVERY_DAYS="x")
        self.write(self.state_file("nas-last-prune"), "08\n")
        rc, _, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.restic_commands()[3:], ["backup", "forget", "check", "snapshots"])
        self.assertNotIn("--prune", self.restic_calls()[4])

    def test_settings_of_the_maintenance(self) -> None:
        self.conf(
            NAS_ENABLED="1",
            NAS_PATH="/volume1/restic",
            NAS_KEEP_DAILY="3",
            NAS_KEEP_WEEKLY="2",
            NAS_KEEP_MONTHLY="1",
            NAS_CHECK_SUBSET="1/10",
            NAS_RESTIC_HOST="sophia",
        )
        self.nas()
        calls = self.restic_calls()
        self.assertEqual(
            calls[4],
            [
                "forget",
                "--host",
                "sophia",
                "--tag",
                "btrfs-setup",
                "--keep-daily",
                "3",
                "--keep-weekly",
                "2",
                "--keep-monthly",
                "1",
                "--prune",
            ],
        )
        self.assertEqual(calls[5], ["check", "--read-data-subset=1/10"])

    def test_a_failed_prune_is_retried_next_run(self) -> None:
        self.restic(forget={"rc": 1})
        _rc, lines, _ = self.nas()
        self.assertIn(log(4, "NAS: restic forget --prune failed (rc=1); retried next run"), lines)
        self.assertFalse(os.path.exists(self.state_file("nas-last-prune")))
        self.assertTrue(os.path.exists(self.state_file("nas-last-ok")))

    def test_check_outcomes(self) -> None:
        damaged = "<4>x\nerror for tree 1234: not found\nFatal: repository contains errors\n"
        for answer, sftp, line, alert in (
            ({"rc": 11}, [0], "NAS: restic check did not complete (rc=11); retried next run", None),
            ({"rc": 1, "out": damaged}, [0], None, "NAS backup: repository damaged"),
            (
                {"rc": 1, "out": damaged},
                [0, 255],
                "NAS: restic check stopped, the NAS went away (rc=1); retried next run",
                None,
            ),
            (
                {"rc": 1, "out": "Fatal: connection lost\n"},
                [0],
                "NAS: restic check failed (rc=1) without naming damaged data (connection lost?); retried next run",
                None,
            ),
        ):
            with self.subTest(line or alert):
                for name in os.listdir(self.state) if os.path.isdir(self.state) else []:
                    os.remove(self.state_file(name))
                self.machine(sftp_rc=sftp)
                self.restic(check=answer)
                rc, lines, _ = self.nas()
                self.assertEqual(rc, 0)
                if line is not None:
                    self.assertIn(log(4, line), lines)
                self.assertEqual([a[4] for a in self.notified()], [alert] if alert else [])
                self.assertFalse(os.path.exists(self.state_file("nas-last-check")))

    def test_a_capture_file_that_cannot_be_written_does_not_stop_the_run(self) -> None:
        # The check's output goes to a scratch file too (bash: tee -a). A file that cannot be opened
        # or written (a full /tmp; here mktemp names a folder) is said on stderr, as tee says it, and
        # the run goes on: the check is judged, the summary and the "ok" line follow. Before, the
        # OSError of the relay ended the run with a traceback (exit 1).
        folder = os.path.join(self.tmp, "tmpdir", "capture")
        self.write(os.path.join(self.bin, "mktemp"), f"#!/bin/sh\nmkdir -p '{folder}' && echo '{folder}'\n")
        os.chmod(os.path.join(self.bin, "mktemp"), 0o755)
        self.restic(check={"rc": 1, "out": "Fatal: connection lost\n"})
        rc, lines, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertIn(f"tee: {folder}: Is a directory", err.splitlines())
        self.assertNotIn("Traceback", err)
        self.assertIn(log(4, "restic: Fatal: connection lost"), lines)
        self.assertIn(
            log(4, "NAS: restic check failed (rc=1) without naming damaged data (connection lost?); retried next run"),
            lines,
        )
        self.assertEqual(self.read_state("nas-check-failures"), "1\n")
        self.assertTrue(any(ln.startswith(log(5, "NAS backup ok: ")) for ln in lines), lines)

    def test_a_failure_count_that_names_a_word_stops_the_run_and_says_why(self) -> None:
        # bash's $(( abc + 1 )) under set -u stopped the run; the runtime stops it too, and now
        # says which file holds what (before, exit 1 without a line).
        self.known_repository()
        self.write(self.state_file("nas-check-failures"), "abc\n")
        self.restic(check={"rc": 1, "out": "Fatal: connection lost\n"})
        rc, _, err = self.nas()
        self.assertEqual(rc, 1)
        failures = self.state_file("nas-check-failures")
        self.assertIn(f"{SCRIPT}: {failures} holds 'abc': not a number", err.splitlines())
        self.assertEqual(self.read_state("nas-check-failures"), "abc\n")
        self.assertFalse(os.path.exists(self.path("run/btrfs-snapshot/pinned")))

    def test_a_check_that_keeps_failing_alerts_on_the_third_run(self) -> None:
        self.restic(check={"rc": 1, "out": "Fatal: connection lost\n"})
        for n in (1, 2, 3):
            self.nas()
            self.assertEqual(self.read_state("nas-check-failures"), f"{n}\n")
        self.assertEqual(
            self.notified(),
            [
                [
                    "-u",
                    "normal",
                    "-a",
                    "btrfs-snapshot",
                    "NAS backup: repository check keeps failing",
                    (
                        "restic check failed 3 runs in a row (rc=1) with the NAS reachable, without naming damaged data; "
                        "see journalctl -u btrfs-snapshot-nas."
                    ),
                ]
            ],
        )
        self.restic()
        self.nas()
        self.assertFalse(os.path.exists(self.state_file("nas-check-failures")))

    def test_the_failure_count_wraps_at_64_bits_as_bash(self) -> None:
        # bash's $(( 9223372036854775807 + 1 )) is -2**63: below 3, no alert (main: 2**63 and the alert).
        self.known_repository()
        self.write(self.state_file("nas-check-failures"), "9223372036854775807\n")
        self.restic(check={"rc": 1, "out": "Fatal: connection lost\n"})
        rc, _lines, err = self.nas()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_state("nas-check-failures"), "-9223372036854775808\n")
        self.assertNotIn("NAS backup: repository check keeps failing", [a[4] for a in self.notified()])

    def test_a_check_overdue_by_another_interval_alerts(self) -> None:
        self.known_repository()
        self.days_ago("nas-last-check", 60)
        self.restic(check={"rc": 11})
        self.nas()
        self.assertEqual(
            [a[4:] for a in self.notified()],
            [
                [
                    "NAS backup: repository not checked for 60 days",
                    "restic check did not complete at the last attempts; see journalctl -u btrfs-snapshot-nas.",
                ]
            ],
        )

    def test_a_summary_that_cannot_be_read_keeps_the_last_one(self) -> None:
        self.write(self.state_file("nas-repo-id"), REPO_ID + "\n")
        self.write(self.state_file("nas-summary"), "home: old\n")
        for answer in ({"rc": 1}, {"out": "not json"}):
            with self.subTest(answer):
                self.restic(snapshots=answer)
                self.nas()
                self.assertEqual(self.read_state("nas-summary"), "home: old\n")
                self.assertFalse(os.path.exists(self.state_file("nas-summary.tmp")))


if __name__ == "__main__":
    unittest.main()
