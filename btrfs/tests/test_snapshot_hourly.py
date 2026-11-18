# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests for btrfs/btrfs-snapshot: the command line, the settings,
hourly, prune, list, caches and purge (no root, no btrfs needed).

The whole bash program runs on the fake machine of snapshot_fakes.py
(CST_SNAPSHOT_ROOT, fake btrfs and btrfs-snapshot-core, a closed PATH). The
tests pin what a caller, the journal and the next run see: the exit status,
stdout (the journal lines) and stderr, btrfs-snapshot-core's command line,
the alerts btrfs-notify gets, and the state files.

Run with: invowk cmd test module btrfs.tests.test_snapshot_hourly
"""

import json
import os
import shutil
import time
import unittest

from btrfs.tests.snapshot_fakes import GIB, USAGE, RuntimeTest

TAG = "<{}>btrfs-snapshot: "
HOME_POLICY = "72h 7d 4w 3m"
WS_POLICY = "48h 7d"
PODMAN = ["--caches-skip", "home=u/.local/share/containers"]


def log(level: int, text: str) -> str:
    return TAG.format(level) + text


class Base(RuntimeTest):
    def sources(self, home: str = HOME_POLICY, ws: str | None = None) -> list[str]:
        args = ["--source", f"home={self.top}/home:{home}"]
        if ws is not None:
            args += ["--source", f"Workspace={self.top}/home/u/Workspace:{ws}"]
        return args

    def snapshot_call(self) -> list[object]:
        return [*self.common("snapshot"), *self.sources(), "--exclude-caches", *PODMAN]

    def prune_call(self, *extra: str, policy: str = HOME_POLICY) -> list[object]:
        return [*self.common("prune"), *self.sources(policy), "--exclude-caches", *PODMAN, *extra]

    def alert(self, urgency: str, title: str, body: str) -> list[object]:
        return ["-u", urgency, "-a", "btrfs-snapshot", title, body]

    def lines(self, text: str) -> list[str]:
        return text.splitlines()

    def calls_but_hostname(self) -> list[list[object]]:
        """The calls of the fakes besides hostname -s, which every start runs (NAS_RESTIC_HOST's default)."""
        return [c for c in self.calls() if c[0] != "hostname"]

    def recent(self, name: str) -> None:
        """STATE/name holds an epoch of the last two minutes."""
        self.assertLess(abs(int(self.read_state(name)) - time.time()), 120)


class CommandLineTest(Base):
    def test_no_or_an_unknown_command_is_a_usage_error(self) -> None:
        for args in ((), ("",), ("bogus",), ("--help",)):
            with self.subTest(args):
                r = self.run_prog(*args)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (2, "", USAGE))
        self.assertEqual(self.core_calls(), [])

    def test_configure_is_a_no_op(self) -> None:
        r = self.run_prog("configure", "extra")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.calls_but_hostname(), [])

    def test_every_command_creates_the_state_folder_first(self) -> None:
        shutil.rmtree(self.state, ignore_errors=True)
        r = self.run_prog("bogus")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(os.path.isdir(self.state))


class SettingsTest(Base):
    def test_policies_and_sources_from_snapshots_conf(self) -> None:
        self.conf(
            "HOME_PRESERVE='24h 2d'  # shorter for now\nWS_PRESERVE=\"12h\"\nHOME_SUBVOL=@home\nSNAP_DIR_NAME=snaps\n"
        )
        os.makedirs(os.path.join(self.top, "snaps"))
        self.run_prog("list")
        self.assertEqual(
            self.core_calls(),
            [
                [
                    "list",
                    "--snapdir",
                    f"{self.top}/snaps",
                    "--pin-file",
                    self.path("run/btrfs-snapshot/pinned"),
                    "--state-dir",
                    self.state,
                    "--list-hint",
                    "cst btrfs snapshots list",
                    "--confirm-hint",
                    "cst btrfs snapshots prune --confirm",
                    "--source",
                    f"home={self.top}/@home:24h 2d",
                ]
            ],
        )

    def test_home_dir_from_the_environment_wins_over_the_settings(self) -> None:
        other = os.path.join(self.home_mount, "other")
        os.makedirs(other)
        self.run_prog("hourly", env={"HOME_DIR": other})
        call = self.core_calls()[0]
        self.assertEqual(call[-2:], ["--caches-skip", "home=other/.local/share/containers"])

    def test_a_home_dir_an_older_cst_wrote_with_printf_q(self) -> None:
        spaced = os.path.join(self.home_mount, "u v")
        os.makedirs(spaced)
        self.write(
            "etc/btrfs-setup/snapshots.conf",
            f"HOME_DIR={spaced.replace(' ', chr(92) + ' ')}\nHOME_MOUNT={self.home_mount}\nTOP={self.top}\n",
        )
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.core_calls()[0][-2:], ["--caches-skip", "home=u v/.local/share/containers"])

    def test_the_settings_file_is_sourced_by_bash(self) -> None:
        # Not parsed: bash runs it, as root. A line that is a command runs.
        marker = os.path.join(self.tmp, "ran")
        self.conf(f"touch {marker}\n")
        self.run_prog("configure")
        self.assertTrue(os.path.exists(marker))

    def test_without_a_settings_file_it_is_not_set_up(self) -> None:
        os.remove(self.path("etc/btrfs-setup/snapshots.conf"))
        r = self.run_prog("hourly")
        self.assertEqual((r.returncode, r.stdout), (1, log(3, "not set up; run 'cst btrfs snapshots setup'") + "\n"))


class CacheArgsTest(Base):
    """SNAPSHOT_EXCLUDE_CACHES and SNAPSHOT_CACHES_SKIP become the core's cache options
    (hourly) and the skips of the listing (caches)."""

    def hourly_and_caches(self, **settings: str) -> tuple[list[object], list[object], list[str]]:
        """(the cache options of hourly's snapshot call, caches' arguments after the source, hourly's log lines
        that are not the alert-free run's)."""
        self.conf(**settings)
        # Files in the working folder: a '*' in the skip list must stay a '*'.
        for name in ("a-file", "b-file"):
            self.write(os.path.join(self.tmp, name), "")
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snap = self.core_calls()[-1]
        self.forget_calls()
        self.run_prog("caches")
        caches = self.core_calls()[-1]
        n = 2 * snap.count("--source")
        return snap[len(self.common("snapshot")) + n :], caches[3 + n :], self.lines(r.stdout)

    def test_default_is_on_with_podman_kept_whole(self) -> None:
        args, skips, logs = self.hourly_and_caches()
        self.assertEqual(args, ["--exclude-caches", *PODMAN])
        self.assertEqual(logs, [])
        # What caches gets (it takes no --exclude-caches).
        self.assertEqual(skips, PODMAN)

    def test_off_keeps_the_skips_for_the_listing(self) -> None:
        args, skips, _ = self.hourly_and_caches(SNAPSHOT_EXCLUDE_CACHES="no")
        self.assertEqual(args, PODMAN)
        self.assertEqual(skips, args)

    def test_a_typo_warns_and_stays_on(self) -> None:
        args, _, logs = self.hourly_and_caches(SNAPSHOT_EXCLUDE_CACHES="nope")
        self.assertEqual(args[0], "--exclude-caches")
        self.assertEqual(logs, [log(4, "SNAPSHOT_EXCLUDE_CACHES='nope' is not yes or no; using yes")])

    def test_paths_go_to_the_source_that_holds_them(self) -> None:
        os.makedirs(os.path.join(self.home, "Workspace"))
        self.mark_subvol(os.path.join(self.home, "Workspace"))
        skips = "Workspace/big/target Workspace .local/share/containers/ ./notes"
        args, listed, _ = self.hourly_and_caches(SNAPSHOT_CACHES_SKIP=skips)
        expected = [
            "--caches-skip",
            "Workspace=big/target",
            "--caches-skip",
            "Workspace=.",
            "--caches-skip",
            "home=u/.local/share/containers",
            "--caches-skip",
            "home=u/notes",
        ]
        self.assertEqual(args, ["--exclude-caches", *expected])
        self.assertEqual(listed, expected)

    def test_until_workspace_is_a_subvolume_it_is_part_of_the_home_snapshots(self) -> None:
        os.makedirs(os.path.join(self.home, "Workspace"))
        args, _, _ = self.hourly_and_caches(SNAPSHOT_CACHES_SKIP="Workspace/big/target")
        self.assertEqual(args, ["--exclude-caches", "--caches-skip", "home=u/Workspace/big/target"])

    def test_paths_outside_the_home_are_ignored_with_a_warning(self) -> None:
        args, _, logs = self.hourly_and_caches(SNAPSHOT_CACHES_SKIP="/etc ../other a/../../b x/./y ok *")
        self.assertEqual(args, ["--exclude-caches", "--caches-skip", "home=u/ok", "--caches-skip", "home=u/*"])
        self.assertEqual(
            logs,
            [
                log(4, f"SNAPSHOT_CACHES_SKIP: '{p}' is not a path inside your home; ignored")
                for p in ("/etc", "../other", "a/../../b", "x/./y")
            ],
        )

    def test_empty_skip_list(self) -> None:
        args, skips, logs = self.hourly_and_caches(SNAPSHOT_CACHES_SKIP="")
        self.assertEqual((args, skips, logs), (["--exclude-caches"], [], []))


class ListAndCachesTest(Base):
    def test_list_passes_its_arguments_output_and_status_through(self) -> None:
        self.core(list={"out": '{"sources": []}\n', "err": "note\n", "rc": 1})
        r = self.run_prog("list", "--json")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (1, '{"sources": []}\n', "note\n"))
        self.assertEqual(
            self.core_calls(),
            [
                [
                    "list",
                    "--snapdir",
                    self.snapdir,
                    "--pin-file",
                    self.path("run/btrfs-snapshot/pinned"),
                    "--state-dir",
                    self.state,
                    "--list-hint",
                    "cst btrfs snapshots list",
                    "--confirm-hint",
                    "cst btrfs snapshots prune --confirm",
                    *self.sources(),
                    "--json",
                ]
            ],
        )

    def test_list_and_caches_take_no_lock(self) -> None:
        with self.hold_lock():
            self.assertEqual(self.run_prog("list").returncode, 0)
            self.assertEqual(self.run_prog("caches").returncode, 0)
        self.assertEqual([c[0] for c in self.core_calls()], ["list", "caches"])

    def test_caches_json_gets_only_the_cores_output_on_stdout(self) -> None:
        self.conf(SNAPSHOT_EXCLUDE_CACHES="nope", SNAPSHOT_CACHES_SKIP="/etc ok")
        self.core(caches={"out": '{"sources": []}\n'})
        r = self.run_prog("caches", "--json")
        self.assertEqual((r.returncode, r.stdout), (0, '{"sources": []}\n'))
        self.assertEqual(
            self.lines(r.stderr),
            [
                log(4, "SNAPSHOT_EXCLUDE_CACHES='nope' is not yes or no; using yes"),
                log(4, "SNAPSHOT_CACHES_SKIP: '/etc' is not a path inside your home; ignored"),
            ],
        )
        self.assertEqual(
            self.core_calls(),
            [["caches", "--snapdir", self.snapdir, *self.sources(), "--caches-skip", "home=u/ok", "--json"]],
        )

    def test_caches_says_when_new_snapshots_keep_them(self) -> None:
        self.conf(SNAPSHOT_EXCLUDE_CACHES="no")
        r = self.run_prog("caches")
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.assertEqual(
            r.stderr,
            f"SNAPSHOT_EXCLUDE_CACHES=no in {conf}: new snapshots keep their cache folders; with yes they would drop:\n",
        )

    def test_not_set_up(self) -> None:
        self.conf(HOME_DIR="")
        for cmd in ("list", "caches", "prune"):
            with self.subTest(cmd):
                r = self.run_prog(cmd)
                self.assertEqual(
                    (r.returncode, r.stdout, r.stderr), (1, "", "not set up; run 'cst btrfs snapshots setup'\n")
                )
        self.assertEqual(self.core_calls(), [])


class ManualPruneTest(Base):
    def test_arguments_and_status(self) -> None:
        self.core(prune={"out": log(5, "home: nothing to delete") + "\n", "rc": 4})
        r = self.run_prog("prune", "--dry-run", "--allow-mass-delete")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (4, log(5, "home: nothing to delete") + "\n", ""))
        self.assertEqual(self.core_calls(), [self.prune_call("--dry-run", "--allow-mass-delete")])

    def test_a_bad_argument_is_a_usage_error(self) -> None:
        r = self.run_prog("prune", "--confirm")
        self.assertEqual(
            (r.returncode, r.stdout, r.stderr),
            (2, "", "usage: btrfs-snapshot prune [--dry-run] [--allow-mass-delete]\n"),
        )
        self.assertEqual(self.core_calls(), [])

    def test_a_real_successful_prune_clears_the_blocked_alert(self) -> None:
        stamp = self.write(self.state_file("alerted-blocked"), "")
        self.assertEqual(self.run_prog("prune", "--dry-run").returncode, 0)
        self.assertTrue(os.path.exists(stamp))
        self.core(prune={"rc": 4})
        self.assertEqual(self.run_prog("prune").returncode, 4)
        self.assertTrue(os.path.exists(stamp))
        self.core(prune={"rc": 0})
        self.assertEqual(self.run_prog("prune", "--allow-mass-delete").returncode, 0)
        self.assertFalse(os.path.exists(stamp))

    def test_core_missing(self) -> None:
        os.remove(self.path("usr/local/sbin/btrfs-snapshot-core"))
        r = self.run_prog("prune")
        core = self.path("usr/local/sbin/btrfs-snapshot-core")
        self.assertEqual((r.returncode, r.stderr), (1, f"{core} is missing; re-run 'cst btrfs snapshots setup'\n"))


class HourlyTest(Base):
    def test_a_snapshot_run(self) -> None:
        out = log(5, "home: created home.20261118T1000") + "\n" + log(6, "home: summary: 1 kept") + "\n"
        self.core(snapshot={"out": out})
        for name in ("paused", "emergency", "alerted-prune-failed", "alerted-blocked", "alerted-paused"):
            self.write(self.state_file(name), "")
        self.machine(free=300 * GIB)
        r = self.run_prog("hourly")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, out, ""))
        self.assertEqual(self.core_calls(), [self.snapshot_call()])
        self.recent("last-ok")
        self.assertEqual(self.notified(), [])
        self.assertEqual(
            sorted(os.listdir(self.state)), ["last-ok"], "the pause, the emergency and their alerts are cleared"
        )
        self.assertEqual([c for c in self.calls() if c[0] == "btrfs"], [["btrfs", "filesystem", "usage", "-b", "/var"]])
        # It exports nothing: every command gets the environment the unit gave it.
        self.assertEqual([e for e in self.call_envs() if e], [])
        # Modes under a service's umask (0022).
        self.assertEqual(
            (self.mode("run/btrfs-snapshot.lock"), self.mode(self.state), self.mode(self.state_file("last-ok"))),
            (0o644, 0o755, 0o644),
        )

    def test_workspace_becomes_a_second_source_once_it_is_a_subvolume(self) -> None:
        ws = os.path.join(self.home, "Workspace")
        os.makedirs(ws)
        self.mark_subvol(ws)
        self.assertEqual(self.run_prog("hourly").returncode, 0)
        self.assertEqual(
            self.core_calls(),
            [[*self.common("snapshot"), *self.sources(ws=WS_POLICY), "--exclude-caches", *PODMAN]],
        )

    def test_not_set_up_or_no_core(self) -> None:
        self.conf(HOME_DIR="")
        r = self.run_prog("hourly")
        self.assertEqual(
            (r.returncode, r.stdout, r.stderr), (1, log(3, "not set up; run 'cst btrfs snapshots setup'") + "\n", "")
        )
        self.conf()
        core = self.path("usr/local/sbin/btrfs-snapshot-core")
        os.remove(core)
        r = self.run_prog("hourly")
        self.assertEqual(
            (r.returncode, r.stdout), (1, log(3, f"{core} is missing; re-run 'cst btrfs snapshots setup'") + "\n")
        )

    def test_free_space_unreadable(self) -> None:
        self.machine(free=None)
        r = self.run_prog("hourly")
        self.assertEqual((r.returncode, r.stdout), (1, log(3, "cannot read free space") + "\n"))
        self.assertEqual(self.core_calls(), [])

    def test_core_exit_statuses(self) -> None:
        for rc, status, line in (
            (3, 0, log(5, f"snapshot: another run holds {self.path('run/btrfs-snapshot.lock')}; skipped")),
            (1, 1, log(3, "snapshot: btrfs-snapshot-core failed (rc=1)")),
            (2, 1, log(3, "snapshot: btrfs-snapshot-core failed (rc=2)")),
        ):
            with self.subTest(rc):
                self.core(snapshot={"rc": rc})
                r = self.run_prog("hourly")
                self.assertEqual((r.returncode, r.stdout), (status, line + "\n"))
                self.assertFalse(os.path.exists(self.state_file("last-ok")))
                self.assertEqual(self.notified(), [])

    def test_lock_busy_skips_the_run(self) -> None:
        with self.hold_lock():
            r = self.run_prog("hourly")
        self.assertEqual(
            (r.returncode, r.stdout), (0, log(5, "another conversion/snapshot run is active; skipped") + "\n")
        )
        self.assertEqual(self.calls_but_hostname(), [])


class BlockedTest(Base):
    """Exit 4 of the core: a safety check blocked a creation or deletion."""

    def blocked(self, out: str) -> list[object]:
        self.core(snapshot={"out": out, "rc": 4})
        r = self.run_prog("hourly")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertFalse(os.path.exists(self.state_file("last-ok")))
        self.assertTrue(os.path.exists(self.state_file("alerted-blocked")))
        (alert,) = self.notified()
        return alert

    def test_circuit_breaker(self) -> None:
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        reason = "home: circuit breaker: 6 deletions at once (limit 5); not deleting"
        out = "".join(
            [
                log(5, "home: note: home.20991231T0000 is dated in the future") + "\n",
                log(4, f"{reason}; check with btrfs-snapshot-core list --source ...") + "\n",
            ]
        )
        self.assertEqual(
            self.blocked(out),
            self.alert(
                "normal",
                "Snapshots: safety check blocked deletions",
                f"{reason} - review: cst btrfs snapshots list; if those deletions are right (e.g. a shorter retention "
                f"in {conf}): cst btrfs snapshots prune --confirm; details: journalctl -u btrfs-snapshot.",
            ),
        )

    def test_a_new_snapshot_blocked(self) -> None:
        reason = "home: the clock is behind the newest snapshot; not creating a snapshot"
        out = log(4, "home: summary: 0 created") + "\n" + log(3, reason) + "\n"
        self.assertEqual(
            self.blocked(out),
            self.alert(
                "normal",
                "Snapshots: safety check blocked a new snapshot",
                f"{reason} - review: cst btrfs snapshots list; details: journalctl -u btrfs-snapshot.",
            ),
        )

    def test_without_a_block_line_the_first_warning_is_the_reason(self) -> None:
        out = log(6, "home: fine") + "\n" + log(4, "home: odd") + "\n" + log(4, "home: odder") + "\n"
        alert = self.blocked(out)
        self.assertEqual(
            alert[5], "home: odd - review: cst btrfs snapshots list; details: journalctl -u btrfs-snapshot."
        )

    def test_the_reason_is_cut_at_300_characters(self) -> None:
        alert = self.blocked(log(4, "x" * 400) + "\n")
        self.assertEqual(
            alert[5], "x" * 300 + " - review: cst btrfs snapshots list; details: journalctl -u btrfs-snapshot."
        )

    def test_a_good_run_clears_the_blocked_alert(self) -> None:
        self.write(self.state_file("alerted-blocked"), "")
        self.assertEqual(self.run_prog("hourly").returncode, 0)
        self.assertFalse(os.path.exists(self.state_file("alerted-blocked")))


class AlertStampTest(Base):
    """At most one desktop alert a day per kind; the journal line every time."""

    def paused_run(self) -> tuple[str, list[list[object]]]:
        self.machine(free=200 * GIB)
        self.forget_calls()
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0)
        return r.stdout, self.notified()

    def test_once_a_day(self) -> None:
        out, sent = self.paused_run()
        self.assertEqual(len(sent), 1)
        line = log(
            4,
            "Snapshots paused: low space: 200 GiB free; hourly snapshots resume above 270 GiB. Older snapshots still expire normally.",
        )
        self.assertEqual(out.splitlines()[0], line)
        out, sent = self.paused_run()
        self.assertEqual((out.splitlines()[0], sent), (line, []))
        stamp = self.state_file("alerted-paused")
        os.utime(stamp, (time.time() - 86401, time.time() - 86401))
        _, sent = self.paused_run()
        self.assertEqual(len(sent), 1)
        self.assertLess(abs(os.stat(stamp).st_mtime - time.time()), 120)

    def test_an_alert_btrfs_notify_could_not_take_is_tried_again(self) -> None:
        self.machine(notify_rc=1)
        _, sent = self.paused_run()
        self.assertEqual(len(sent), 1)
        self.assertFalse(os.path.exists(self.state_file("alerted-paused")))
        _, sent = self.paused_run()
        self.assertEqual(len(sent), 1)

    def test_without_btrfs_notify_only_the_journal_line(self) -> None:
        os.remove(self.path("usr/local/sbin/btrfs-notify"))
        out, sent = self.paused_run()
        self.assertEqual(sent, [])
        self.assertTrue(out.startswith(log(4, "Snapshots paused: low space: ")))
        self.assertFalse(os.path.exists(self.state_file("alerted-paused")))


class SpaceGuardTest(Base):
    def test_emergency_purge(self) -> None:
        self.machine(free=[150 * GIB, 160 * GIB])
        self.core(prune={"out": log(5, "home: deleted 3") + "\n"})
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        title, body = (
            "Snapshots: emergency purge",
            "Only 150 GiB free (limit 150): keeping only the last day and 3 dailies. Free space on /home or /var.",
        )
        self.assertEqual(
            self.lines(r.stdout),
            [
                log(4, f"{title}: {body}"),
                log(5, "home: deleted 3"),
                log(4, "emergency purge done (rc=0); free now 160 GiB"),
            ],
        )
        self.assertEqual(self.notified(), [self.alert("critical", title, body)])
        self.assertEqual(self.core_calls(), [self.prune_call("--allow-mass-delete", policy="24h 3d")])
        self.assertEqual(
            [c for c in self.calls() if c[0] == "btrfs"],
            [
                ["btrfs", "filesystem", "usage", "-b", "/var"],
                ["btrfs", "subvolume", "sync", "/var"],
                ["btrfs", "filesystem", "usage", "-b", "/var"],
            ],
        )
        self.assertEqual(sorted(os.listdir(self.state)), ["alerted-emergency", "emergency", "paused"])

    def test_free_space_unreadable_after_the_emergency_purge(self) -> None:
        # Bug (phase 1 list): the purge ran, but the log line after it divides an empty
        # reading: bash's arithmetic error ends the run with exit 1 and no "done" line.
        self.machine(free=[150 * GIB, None])
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 1)
        self.assertEqual([c[0] for c in self.core_calls()], ["prune"])
        self.assertFalse(any("emergency purge done" in ln for ln in self.lines(r.stdout)))
        self.assertIn("arithmetic syntax error", r.stderr)

    def test_emergency_purge_failure_alerts(self) -> None:
        self.machine(free=10 * GIB)
        self.core(prune={"rc": 1})
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0)
        self.assertIn(log(3, "emergency purge: btrfs-snapshot-core failed (rc=1)"), self.lines(r.stdout))
        self.assertEqual(
            [a[4] for a in self.notified()], ["Snapshots: emergency purge", "Snapshots: emergency purge failed"]
        )
        self.assertEqual(self.notified()[1][5], "btrfs-snapshot-core rc=1; see journalctl -u btrfs-snapshot.")

    def test_emergency_purge_blocked_or_busy_is_no_failure_alert(self) -> None:
        for rc in (3, 4):
            with self.subTest(rc):
                shutil.rmtree(self.state, ignore_errors=True)
                self.machine(free=10 * GIB)
                self.core(prune={"rc": rc})
                self.assertEqual(self.run_prog("hourly").returncode, 0)
                self.assertNotIn("alerted-prune-failed", os.listdir(self.state))

    def test_low_space_pauses_new_snapshots_and_keeps_the_retention(self) -> None:
        self.machine(free=250 * GIB)
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.core_calls(), [self.prune_call()])
        self.assertEqual(
            self.notified(),
            [
                self.alert(
                    "normal",
                    "Snapshots paused: low space",
                    "250 GiB free; hourly snapshots resume above 270 GiB. Older snapshots still expire normally.",
                )
            ],
        )
        self.assertEqual(sorted(os.listdir(self.state)), ["alerted-paused", "paused"])

    def test_paused_resumes_only_above_the_resume_limit(self) -> None:
        self.write(self.state_file("paused"), "")
        for free, call in ((270, "prune"), (271, "snapshot")):
            with self.subTest(free):
                self.forget_calls()
                self.machine(free=free * GIB)
                self.assertEqual(self.run_prog("hourly").returncode, 0)
                self.assertEqual([c[0] for c in self.core_calls()], [call])
        self.assertFalse(os.path.exists(self.state_file("paused")))

    def test_a_failed_prune_while_paused_alerts(self) -> None:
        self.machine(free=260 * GIB)
        self.write(self.state_file("paused"), "")
        self.core(prune={"rc": 1})
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            self.notified()[-1],
            self.alert(
                "critical", "Snapshots: purge failed", "btrfs-snapshot-core rc=1; see journalctl -u btrfs-snapshot."
            ),
        )


class WaitingTest(Base):
    """No new snapshot while data about to leave /home is still in it; the retention still runs."""

    def wait_run(self, since: str) -> tuple[list[str], list[list[object]]]:
        self.forget_calls()
        r = self.run_prog("hourly")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual([c[0] for c in self.core_calls()], ["prune"])
        self.assertTrue(os.path.exists(self.state_file(since)))
        self.assertFalse(os.path.exists(self.state_file("last-ok")))
        return self.lines(r.stdout), self.notified()

    def a_day_ago(self, name: str) -> None:
        t = time.time() - 86400 - 120
        os.utime(self.state_file(name), (t, t))

    def test_old_trees_still_being_deleted(self) -> None:
        # A tree with a mount below it is never deleted, so it stays listed.
        old = os.path.join(self.home, ".cache.pre-subvol-20261118T100000")
        os.makedirs(os.path.join(old, "m"))
        self.machine(mounts=["/", f"{old}/m"])
        self.write(self.state_file("drop-pending"), old + "\n")
        lines, sent = self.wait_run("pending-since")
        self.assertEqual(lines, [log(5, "old trees still being deleted (1); no snapshot this hour")])
        self.assertEqual(sent, [])
        self.a_day_ago("pending-since")
        _, sent = self.wait_run("pending-since")
        self.assertEqual(
            sent,
            [self.alert("normal", "Snapshots: waiting for old trees to be deleted", f"For over a day: {old} ")],
        )
        self.assertTrue(os.path.isdir(old))

    def test_conversions_waiting_for_the_next_boot(self) -> None:
        ws = os.path.join(self.home, "Workspace")
        os.makedirs(ws)
        self.write("etc/btrfs-setup/convert-plan", f"copy {ws}\n")
        self.write("etc/btrfs-setup/convert-at-boot", "")
        self.busy(os.path.join(ws, "file"))
        lines, sent = self.wait_run("boot-wait-since")
        self.assertEqual(
            lines,
            [
                log(5, f"convert: {ws}: in use, left for later"),
                log(5, "planned dirs: 0 done, 1 left for later (in use or failed)"),
                log(5, "1 planned conversion(s) wait for the next boot; no snapshot this hour"),
            ],
        )
        self.a_day_ago("boot-wait-since")
        _, sent = self.wait_run("boot-wait-since")
        self.assertEqual(
            sent,
            [
                self.alert(
                    "normal",
                    "Snapshots wait for a reboot",
                    "Busy directories are converted at the next boot, before login; snapshots start after that. "
                    f"Left: {ws} ",
                )
            ],
        )

    def test_the_boot_flag_goes_once_nothing_is_left(self) -> None:
        self.write("etc/btrfs-setup/convert-plan", f"copy {self.home}/absent/x\n")
        self.write("etc/btrfs-setup/convert-at-boot", "")
        self.write(self.state_file("boot-attempts"), "1\n")
        self.write(self.state_file("boot-wait-since"), "")
        self.write(self.state_file("alerted-boot-wait"), "")
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.lines(r.stdout)[0], log(5, "all planned conversions done; snapshots start"))
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
        self.assertFalse(os.path.exists(self.path("etc/btrfs-setup/convert-at-boot")))
        self.assertEqual(sorted(os.listdir(self.state)), ["last-ok"])

    def test_an_unfinished_move_into_bulk(self) -> None:
        self.write("var/lib/btrfs-bulk/journal-1.json", '{"id": 1}\n')
        lines, _ = self.wait_run("bulk-journal-since")
        self.assertEqual(
            lines,
            [log(5, f"an unfinished move into ~/Bulk ({self.path('var/lib/btrfs-bulk')}); no snapshot this hour")],
        )
        self.a_day_ago("bulk-journal-since")
        _, sent = self.wait_run("bulk-journal-since")
        self.assertEqual(
            sent,
            [
                self.alert(
                    "normal",
                    "Snapshots wait for ~/Bulk",
                    "A move into ~/Bulk was interrupted or needs a look; snapshots start after that. Run: cst btrfs bulk status",
                )
            ],
        )

    def test_a_journal_of_a_rewrite_inside_bulk_does_not_wait(self) -> None:
        self.write("var/lib/btrfs-bulk/journal-1.json", '{"id": 1, "bulk_only": true}\n')
        self.write(self.state_file("bulk-journal-since"), "")
        self.assertEqual(self.run_prog("hourly").returncode, 0)
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
        self.assertFalse(os.path.exists(self.state_file("bulk-journal-since")))

    def test_items_waiting_to_move_into_bulk_at_the_next_boot(self) -> None:
        self.write("etc/btrfs-setup/bulk-at-boot", "apps\nsteam\n")
        lines, _ = self.wait_run("bulk-wait-since")
        self.assertEqual(
            lines, [log(5, "items still to move into ~/Bulk at the next boot (apps steam ); no snapshot this hour")]
        )
        self.a_day_ago("bulk-wait-since")
        _, sent = self.wait_run("bulk-wait-since")
        self.assertEqual(
            sent,
            [
                self.alert(
                    "normal",
                    "Snapshots wait for a reboot",
                    "Busy items are moved into ~/Bulk at the next boot, before login; snapshots start after that. "
                    "Left: apps steam ",
                )
            ],
        )

    def test_the_order_of_the_waits(self) -> None:
        # The space guard first, then old trees, the boot flag, a bulk journal, the bulk flag.
        old = os.path.join(self.home, ".x.pre-subvol-1")
        os.makedirs(os.path.join(old, "m"))
        self.machine(mounts=[f"{old}/m"])
        self.write(self.state_file("drop-pending"), old + "\n")
        self.write("etc/btrfs-setup/bulk-at-boot", "apps\n")
        self.write("var/lib/btrfs-bulk/journal-1.json", "{}\n")
        self.wait_run("pending-since")
        self.assertFalse(os.path.exists(self.state_file("bulk-journal-since")))
        os.remove(self.state_file("drop-pending"))
        self.wait_run("bulk-journal-since")
        self.assertFalse(os.path.exists(self.state_file("pending-since")))
        os.remove(self.path("var/lib/btrfs-bulk/journal-1.json"))
        self.wait_run("bulk-wait-since")
        self.assertFalse(os.path.exists(self.state_file("bulk-journal-since")))


class PurgeTest(Base):
    def listing(self, *names: str) -> str:
        snaps = [{"path": os.path.join(self.snapdir, n)} for n in names]
        return json.dumps({"sources": [{"name": "home", "snapshots": snaps}, {"name": "Workspace"}]}) + "\n"

    def make(self, *names: str) -> list[str]:
        paths = [os.path.join(self.snapdir, n) for n in names]
        for p in paths:
            os.makedirs(p)
        return paths

    def test_no_snapshot_folder(self) -> None:
        os.rmdir(self.snapdir)
        r = self.run_prog("purge")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "No snapshot directory: nothing to delete.\n", ""))
        self.assertEqual(self.core_calls(), [])
        self.conf(HOME_DIR="")
        os.makedirs(self.snapdir)
        r = self.run_prog("purge")
        self.assertEqual((r.returncode, r.stdout), (0, "No snapshot directory: nothing to delete.\n"))

    def test_deletes_in_batches_of_ten_and_keeps_the_pinned_one(self) -> None:
        names = [f"home.202611{d:02d}T0000" for d in range(1, 13)]
        paths = self.make(*names)
        self.write("run/btrfs-snapshot/pinned", paths[4] + "\n")
        self.write(self.state_file("core-writable"), "x\n")
        self.write(self.state_file("core-unfinished"), "")
        self.core(list={"out": self.listing(*names)})
        r = self.run_prog("purge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            self.lines(r.stdout),
            [
                log(4, f"purge: {paths[4]} is pinned (NAS backup running?); kept"),
                "Deleting 11 snapshot(s) in batches of 10...",
                f"1 entry left in {self.snapdir} (not owned by btrfs-snapshot-core, or pinned): not deleted.",
            ],
        )
        rest = [p for p in paths if p != paths[4]]
        self.assertEqual(
            [c for c in self.calls() if c[0] == "btrfs"],
            [
                ["btrfs", "subvolume", "delete", *rest[:10]],
                ["btrfs", "subvolume", "sync", "/var"],
                ["btrfs", "subvolume", "delete", *rest[10:]],
                ["btrfs", "subvolume", "sync", "/var"],
            ],
        )
        self.assertEqual(
            self.core_calls(),
            [
                [
                    "list",
                    "--json",
                    "--snapdir",
                    self.snapdir,
                    "--pin-file",
                    self.path("run/btrfs-snapshot/pinned"),
                    "--state-dir",
                    self.state,
                    "--source",
                    f"home={self.top}/home:1h",
                ]
            ],
        )
        self.assertEqual(os.listdir(self.snapdir), [names[4]])
        self.assertEqual(os.listdir(self.state), [])

    def test_finishes_interrupted_snapshots_first_and_removes_the_empty_folder(self) -> None:
        ws = os.path.join(self.home, "Workspace")
        os.makedirs(ws)
        self.mark_subvol(ws)
        self.make("home.20261101T0000", "Workspace.20261101T0000")
        self.write(self.state_file("core-unfinished"), "Workspace.20261101T0000\n")
        self.core(list={"out": self.listing("home.20261101T0000", "Workspace.20261101T0000")}, finish={"rc": 1})
        r = self.run_prog("purge")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            self.lines(r.stdout),
            [log(4, "purge: finishing interrupted snapshots failed"), "Deleting 2 snapshot(s) in batches of 10..."],
        )
        sources = [*self.sources("1h"), "--source", f"Workspace={self.top}/home/u/Workspace:1h"]
        self.assertEqual(self.core_calls()[0], [*self.common("finish"), *sources])
        self.assertFalse(os.path.exists(self.snapdir))

    def test_a_listing_failure_deletes_nothing(self) -> None:
        self.make("home.20261101T0000")
        self.core(list={"rc": 1})
        r = self.run_prog("purge")
        self.assertEqual(
            (r.returncode, r.stdout), (1, log(3, "purge: cannot list the snapshots; nothing deleted") + "\n")
        )
        self.assertEqual(self.calls("btrfs"), [])

    def test_a_failed_deletion_is_logged_and_the_run_goes_on(self) -> None:
        self.make("home.20261101T0000")
        self.machine(delete_rc=1)
        self.core(list={"out": self.listing("home.20261101T0000")})
        r = self.run_prog("purge")
        self.assertEqual(r.returncode, 0)
        self.assertIn(log(3, "purge: a deletion failed"), self.lines(r.stdout))
        self.assertIn(f"1 entry left in {self.snapdir}", r.stdout)

    def test_a_listing_that_is_not_json_deletes_nothing_but_says_it_did_well(self) -> None:
        # Bug (phase 1 list): the listing's JSON is read in a process substitution, whose
        # failure nobody sees: purge says it deletes 0 snapshots, exits 0 and forgets the
        # state lists, as if the folder had been emptied.
        self.make("home.20261101T0000")
        self.write(self.state_file("core-unfinished"), "")
        self.write(self.state_file("core-writable"), "home.x\n")
        self.core(list={"out": "not json\n"})
        r = self.run_prog("purge")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            self.lines(r.stdout),
            [
                "Deleting 0 snapshot(s) in batches of 10...",
                f"1 entry left in {self.snapdir} (not owned by btrfs-snapshot-core, or pinned): not deleted.",
            ],
        )
        self.assertIn("json", r.stderr.lower())
        self.assertEqual(self.calls("btrfs"), [])
        self.assertEqual(os.listdir(self.state), [])


if __name__ == "__main__":
    unittest.main()
