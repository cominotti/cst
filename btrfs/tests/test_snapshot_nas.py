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

import json
import os
import signal
import time
import unittest

from btrfs.tests.snapshot_fakes import RuntimeTest, log, read

ALIAS = "btrfs-setup-nas"
REPO_ID = "ab" * 32
SNAP = "home.20261118T0905"
DAY = 86400


class Base(RuntimeTest):
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
        rc, lines, _ = self.nas()
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
        rc, lines, _ = self.nas()
        self.assertEqual(
            lines[0], log(5, "NAS: another repository than last time (id cdcdcdcd -> abababab); its records start over")
        )
        self.assertIn(self.backup_args(), self.restic_calls())
        self.assertIn("--prune", self.restic_calls()[4])
        self.assertIn("check", self.restic_commands())
        self.assertFalse(os.path.exists(self.state_file("nas-done-Workspace")))
        self.assertEqual(self.read_state("nas-done-home"), f"{SNAP} {REPO_ID}\n")
        self.assertFalse(os.path.exists(self.state_file("nas-check-failures")))

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
        rc, lines, _ = self.nas()
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
        rc, lines, _ = self.nas()
        self.assertEqual(
            lines,
            [
                log(4, "NAS: home: no time left in this run (NAS_TIMEOUT=1m)"),
                log(4, "NAS backup failed for 1 of 1 source(s); will retry next run"),
            ],
        )
        # Unreadable: 6 hours.
        self.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_TIMEOUT="soon")
        rc, lines, _ = self.nas()
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))

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
        rc, lines, _ = self.nas()
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
        rc, lines, _ = self.nas()
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
        rc, lines, _ = self.nas()
        self.assertEqual(
            lines[1:], [log(4, "restic: Fatal: create repository failed"), log(4, "NAS: restic init failed")]
        )
        self.assertTrue(os.path.exists(self.state_file("nas-init-pending")))

    def test_wrong_password(self) -> None:
        self.restic(cat={"rc": 12})
        rc, lines, _ = self.nas()
        title, body = (
            "NAS backup: wrong repository password",
            f"{self.password} does not open {self.repo}; put the saved password there.",
        )
        self.assertEqual(lines, [log(4, f"{title}: {body}")])
        self.assertEqual(self.notified(), [["-u", "critical", "-a", "btrfs-snapshot", title, body]])

    def test_cannot_open(self) -> None:
        self.restic(cat={"rc": 1, "out": "Fatal: unable to open config file\n"})
        rc, lines, _ = self.nas()
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
        rc, lines, _ = self.nas()
        self.assertEqual(lines[-1], log(5, f"NAS backup ok: {SNAP}"))
        self.assertEqual(self.calls("umount")[0], ["umount", stacked])
        self.assertFalse(os.path.exists(stacked))

    def test_a_mount_that_cannot_be_made(self) -> None:
        self.machine(mount_rc=32)
        rc, lines, _ = self.nas()
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
        rc, lines, _ = self.nas()
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
                    "restic check failed 3 runs in a row (rc=1) with the NAS reachable, without naming damaged data; "
                    "see journalctl -u btrfs-snapshot-nas.",
                ]
            ],
        )
        self.restic()
        self.nas()
        self.assertFalse(os.path.exists(self.state_file("nas-check-failures")))

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
