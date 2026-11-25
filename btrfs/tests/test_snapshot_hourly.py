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

import contextlib
import json
import os
import shutil
import subprocess
import time
import unittest

from btrfs.snapshot_runtime.domain import snapshots_resume_above
from btrfs.tests.snapshot_fakes import GIB, SCRIPT, UMASK, USAGE, RuntimeTest, log, read
from tests.test_module_routing import userns_root

HOME_POLICY = "72h 7d 4w 3m"
WS_POLICY = "48h 7d"
PODMAN = ["--caches-skip", "home=u/.local/share/containers"]


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

    def test_as_root_the_test_seam_is_ignored(self) -> None:
        # CST_SNAPSHOT_ROOT moves every file of the program: a run as root never obeys it. In a user
        # namespace where this user is root, it reads the real /etc and /var/lib (only reads; the
        # state folder cannot be created there) and stops at the usage error; the scratch root stays
        # untouched. Before, a root run with the variable set read its settings, state and lock there.
        if not userns_root():
            self.skipTest("needs an unprivileged user namespace (unshare -r); tier 1 test_108 runs it as root")
        unshare = shutil.which("unshare", path="/usr/bin:/bin") or "unshare"
        r = subprocess.run(
            [unshare, "-r", "bash", SCRIPT, "bogus"],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(r.stderr.endswith(USAGE), r.stderr)
        self.assertFalse(os.path.exists(self.state))
        # As this user, the same run uses the seam.
        self.assertEqual(self.run_prog("bogus").returncode, 2)
        self.assertTrue(os.path.isdir(self.state))

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

    def test_the_settings_file_is_read_as_data_never_run(self) -> None:
        # Only plain assignments of its settings are applied (as cst_core's parse_snapshots_conf
        # reads them; test_snapshot_settings.py checks the two agree on a corpus). A line that is a
        # command, or a value bash would expand, gets a warning on stderr and is ignored; a key that
        # is no setting (STATE) never moves anything; "export" in front is dropped. Before, bash
        # sourced the file as root: every line ran.
        marker = os.path.join(self.tmp, "ran")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        self.conf(
            f"touch {marker}\nHOME_PRESERVE='24h 2d'  # kept\nNAS_PATH=$(touch {marker})\n"
            f"STATE='{elsewhere}'\nexport WS_PRESERVE='12h'\nSCRUB_LIMIT='100M'\n"
        )
        r = self.run_prog("list")
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.assertEqual(
            (r.returncode, self.lines(r.stderr)),
            (
                0,
                [
                    log(4, f"{conf} line 4: touch {marker}: not a KEY=VALUE line; ignored"),
                    log(
                        4,
                        f"{conf} line 6: NAS_PATH=$(touch {marker}): $, ` or \\ outside single quotes: bash would "
                        "expand it (use single quotes); ignored",
                    ),
                ],
            ),
        )
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(elsewhere))
        call = self.core_calls()[0]
        self.assertEqual(call[call.index("--state-dir") + 1], self.state)
        self.assertEqual(call[-2:], ["--source", f"home={self.top}/home:24h 2d"])
        self.assertEqual(self.call_envs("btrfs-snapshot-core"), [{}])

    def test_a_blank_beside_a_value_is_kept_as_bash_reads_it_and_named(self) -> None:
        # The file is bash's too: the line keeps the value bash gives it (the bash front's engine reads the
        # same), and the runtime names it on stderr; it claims nothing about whether the value works.
        self.conf("HOME_PRESERVE='24h 2d\N{NO-BREAK SPACE}'\n")
        r = self.run_prog("list")
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.assertEqual(
            (r.returncode, self.lines(r.stderr)),
            (
                0,
                [
                    log(
                        4,
                        f"{conf} line 4: HOME_PRESERVE='24h 2d\N{NO-BREAK SPACE}': a no-break space (U+00A0) at the "
                        "end of the value: CST does not trim it (delete it); kept as bash reads it, with the blank "
                        "(fix the line)",
                    )
                ],
            ),
        )
        call = self.core_calls()[0]
        self.assertEqual(call[-2:], ["--source", f"home={self.top}/home:24h 2d\N{NO-BREAK SPACE}"])

    def test_a_line_with_a_carriage_return_is_ignored(self) -> None:
        # A Windows line end: bash would keep the \r in the value, so the line is warned on and
        # ignored, and the line after it still counts (found by the parity harness of the port: a
        # reader that turns \r\n into \n applied it).
        self.conf("HOME_PRESERVE='24h'\r\nWS_PRESERVE='12h'\n")
        r = self.run_prog("list")
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.assertEqual(
            (r.returncode, self.lines(r.stderr)),
            (
                0,
                [
                    log(
                        4,
                        f"{conf} line 4: HOME_PRESERVE='24h': a carriage return (Windows line end): bash keeps it "
                        "in the value; ignored",
                    )
                ],
            ),
        )
        call = self.core_calls()[0]
        self.assertEqual(call[-2:], ["--source", f"home={self.top}/home:72h 7d 4w 3m"])

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

    def test_a_home_at_the_subvolume_root_gets_paths_without_a_prefix(self) -> None:
        # HOME_DIR is the home subvolume's root: a skip is relative to that root itself.
        args, listed, logs = self.hourly_and_caches(HOME_DIR=self.home_mount, SNAPSHOT_CACHES_SKIP="notes x/y")
        expected = ["--caches-skip", "home=notes", "--caches-skip", "home=x/y"]
        self.assertEqual((args, listed, logs), (["--exclude-caches", *expected], expected, []))


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

    def test_external_mountpoint_workspace_is_not_added_as_snapshot_source(self) -> None:
        ws = os.path.join(self.home, "Workspace")
        os.makedirs(ws)
        self.mark_subvol(ws)
        self.machine(mountpoints=[ws])
        self.assertEqual(self.run_prog("hourly").returncode, 0)
        self.assertEqual(
            self.core_calls(),
            [[*self.common("snapshot"), *self.sources(), "--exclude-caches", *PODMAN]],
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


class StateRecordTest(Base):
    def test_last_ok_is_replaced_whole_never_rewritten_in_place(self) -> None:
        # The records (last-ok, nas-last-ok, nas-repo-id, ...) were truncated and rewritten in place:
        # a power loss or ENOSPC in between left an empty file (health-check then stopped checking
        # staleness; an empty nas-repo-id starts every NAS record over). Now a new file is renamed
        # over the old one: another link to the old file keeps the old content.
        last_ok = self.write(self.state_file("last-ok"), "1\n")
        kept = os.path.join(self.tmp, "old-last-ok")
        os.link(last_ok, kept)
        self.core(snapshot={})
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.read_state("last-ok").strip().isdigit())
        self.assertNotEqual(self.read_state("last-ok"), "1\n")
        self.assertEqual(read(kept), "1\n")
        self.assertEqual(self.mode(last_ok), 0o666 & ~UMASK)


class ClosedStdoutTest(Base):
    """systemd runs the service with SIGPIPE ignored: when stdout is gone (journald restarted), a
    line that cannot be written fails with EPIPE and is dropped without a word, and the run goes on
    (bash's echo and printf did so): the snapshot is still taken and last-ok written."""

    def closed_run(self, *, stderr_too: bool = False) -> subprocess.CompletedProcess[str]:
        """hourly with stdout (and stderr, with stderr_too) a pipe nobody reads, SIGPIPE ignored."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            return subprocess.run(
                ["bash", SCRIPT, "hourly"],
                stdout=write_end,
                stderr=write_end if stderr_too else subprocess.PIPE,
                text=True,
                env=self.env,
                cwd=self.tmp,
                stdin=subprocess.DEVNULL,
                restore_signals=False,  # SIGPIPE stays ignored, as this runner has it
                timeout=120,
                check=False,
            )
        finally:
            os.close(write_end)

    def test_hourly_goes_on_without_its_stdout(self) -> None:
        self.core(snapshot={"out": "<6>btrfs-snapshot: home: created\n<6>btrfs-snapshot: summary\n"})
        self.write(self.state_file("alerted-paused"), "")
        r = self.closed_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
        self.assertTrue(self.read_state("last-ok").strip().isdigit())
        self.assertEqual(r.stderr, "")

    def test_the_commands_it_runs_keep_sigpipe_ignored(self) -> None:
        # The engine step (bash) logs a line: an entry of drop-pending that is no old tree is
        # forgotten. Started with SIGPIPE at its default (what subprocess gives a child unless told
        # otherwise), it died of SIGPIPE on that line, and hourly failed without a snapshot.
        other = os.path.join(self.home, "not-an-old-tree")
        os.makedirs(other)
        self.write(self.state_file("drop-pending"), f"{other}\n")
        self.core(snapshot={"out": "<6>btrfs-snapshot: home: created\n"})
        r = self.closed_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
        self.assertTrue(self.read_state("last-ok").strip().isdigit())
        self.assertEqual(self.read_state("drop-pending"), "")
        self.assertTrue(os.path.isdir(other))

    def test_hourly_goes_on_without_its_stdout_and_stderr(self) -> None:
        # One journal stream under systemd: stdout and stderr go together. A settings line that gets
        # a warning (on stderr, from the runtime and from the engine step) stopped the runtime.
        self.conf("NAS_PATH=$(echo x)\n")
        self.core(snapshot={"out": "<6>btrfs-snapshot: home: created\n"})
        r = self.closed_run(stderr_too=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
        self.assertTrue(self.read_state("last-ok").strip().isdigit())


class LockLifetimeTest(Base):
    """The lock of a run (fd 9 on /run/btrfs-snapshot.lock) is inherited by everything the run
    starts: when the run itself is killed, a child still working (here btrfs-snapshot-core, in the
    middle of its snapshot) keeps the lock, and a second run waits for it to end (DESIGN 8.1: the
    port must keep that)."""

    def test_a_killed_run_with_a_child_still_working_keeps_the_lock(self) -> None:
        self.core(snapshot=[{"sleep": 4}, {}])
        p = self.start_prog("hourly")
        for _ in range(200):
            if self.core_calls():
                break
            time.sleep(0.05)
        else:
            self.fail("the run never reached btrfs-snapshot-core")
        p.kill()  # the run's own process only: its child keeps working
        p.wait(timeout=30)
        self.forget_calls()
        r = self.run_prog("hourly")
        self.assertEqual(
            (r.returncode, r.stdout), (0, log(5, "another conversion/snapshot run is active; skipped") + "\n")
        )
        self.assertEqual(self.core_calls(), [])
        # Once the child has ended, the next run goes ahead.
        for _ in range(200):
            r = self.run_prog("hourly")
            if self.core_calls():
                break
            time.sleep(0.1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])


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

    def test_bytes_that_are_not_utf_8_reach_the_journal_and_the_alert_as_they_were(self) -> None:
        # A home folder with a Latin-1 name: the core's lines and CORE_REASON name it with the byte
        # 0xE9. They were decoded with U+FFFD on the way, so the journal and the alert named a path
        # that does not exist; bash passed the bytes on.
        reason = "home: not deleting /home/u/caf\udce9.20261101T0000: blocked"
        self.core(snapshot={"out": log(4, reason) + "\n", "rc": 4})
        r = subprocess.run(
            ["bash", SCRIPT, "hourly"],
            capture_output=True,
            env=self.env,
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertIn(b"<4>btrfs-snapshot: home: not deleting /home/u/caf\xe9.20261101T0000: blocked\n", r.stdout)
        (alert,) = self.notified()
        self.assertTrue(str(alert[5]).startswith("home: not deleting /home/u/caf\udce9.20261101T0000: blocked - "))

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
        # Still within the day: the journal line only.
        stamp = self.state_file("alerted-paused")
        os.utime(stamp, (time.time() - 86400 + 120, time.time() - 86400 + 120))
        out, sent = self.paused_run()
        self.assertEqual((out.splitlines()[0], sent), (line, []))
        os.utime(stamp, (time.time() - 86401, time.time() - 86401))
        _, sent = self.paused_run()
        self.assertEqual(len(sent), 1)
        self.assertLess(abs(os.stat(stamp).st_mtime - time.time()), 120)

    def test_an_alert_btrfs_notify_could_not_take_is_kept_and_not_sent_again_that_day(self) -> None:
        # Kept in the pending alerts folder, it reaches cst doctor and the next login: sent for the day.
        self.machine(notify_rc=1)
        _, sent = self.paused_run()
        self.assertEqual(len(sent), 1)
        self.assertTrue(os.path.exists(self.state_file("alerted-paused")))
        _, sent = self.paused_run()
        self.assertEqual(sent, [])

    def test_an_alert_kept_nowhere_is_tried_again(self) -> None:
        # btrfs-notify fails and the pending alerts folder cannot be made: only the journal has the
        # alert, so the stamp stays unset and the next run sends it again.
        self.machine(notify_rc=1)
        shutil.rmtree(self.path("var/lib/cst-alerts"), ignore_errors=True)
        self.write(self.path("var/lib/cst-alerts"), "")
        out, sent = self.paused_run()
        self.assertEqual(len(sent), 1)
        self.assertIn("it is only in this journal", out)
        self.assertFalse(os.path.exists(self.state_file("alerted-paused")))
        _, sent = self.paused_run()
        self.assertEqual(len(sent), 1)

    def test_an_alert_btrfs_notify_cannot_take_is_kept_for_cst_doctor(self) -> None:
        # btrfs-notify missing (btrfs:alerts half removed) or failing: the alert went only to the
        # journal. cst_core's Notifier keeps it in the pending alerts folder (cst doctor and the next
        # login show it) and says so; that sets the once-a-day stamp.
        pending = self.path("var/lib/cst-alerts/pending")
        for name, setup, why in (
            ("failing", lambda: self.machine(notify_rc=1), "exited 1"),
            ("missing", lambda: os.remove(self.path("usr/local/sbin/btrfs-notify")), "is not installed"),
        ):
            with self.subTest(name):
                shutil.rmtree(pending, ignore_errors=True)
                setup()
                out, _ = self.paused_run()
                (kept,) = os.listdir(pending)
                path = os.path.join(pending, kept)
                text = read(path)
                self.assertTrue(text.startswith("cst-alert 1\nurgency=normal\napp=btrfs-snapshot\n"), text)
                self.assertIn("\ntitle=Snapshots paused: low space\n", text)
                notify = self.path("usr/local/sbin/btrfs-notify")
                self.assertIn(
                    log(
                        3,
                        f"alert not delivered ({notify} {why}): Snapshots paused: low space; kept in {path} "
                        "(cst doctor shows it)",
                    ),
                    out.splitlines(),
                )
                self.assertTrue(os.path.exists(self.state_file("alerted-paused")))
                os.remove(self.state_file("alerted-paused"))

    def test_without_btrfs_notify_nothing_is_sent_and_the_kept_alert_sets_the_stamp(self) -> None:
        os.remove(self.path("usr/local/sbin/btrfs-notify"))
        out, sent = self.paused_run()
        self.assertEqual(sent, [])
        self.assertTrue(out.startswith(log(4, "Snapshots paused: low space: ")))
        self.assertTrue(os.path.exists(self.state_file("alerted-paused")))


class SpaceGuardTest(Base):
    def test_paused_snapshots_resume_just_above_what_btrfs_bulk_says(self) -> None:
        # btrfs-bulk's steam plan tells when paused snapshots come back (snapshots_resume_above): at that
        # free space the paused hourly run stays paused, one byte above it the snapshot is made.
        for skip, resume in ((250, 100), (250, 270), (250, 0), (40, 45), (90, 45)):
            above = snapshots_resume_above({"pause": skip * GIB, "resume": resume * GIB})
            self.conf(SKIP_AT_OR_BELOW_GIB=str(skip), RESUME_ABOVE_GIB=str(resume), EMERGENCY_AT_OR_BELOW_GIB="10")
            for free, paused in ((above, True), (above + 1, False)):
                with self.subTest(skip=skip, resume=resume, free=free):
                    self.write(self.state_file("paused"), "")
                    self.machine(free=free)
                    self.forget_calls()
                    r = self.run_prog("hourly")
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertEqual(os.path.exists(self.state_file("paused")), paused)
                    self.assertEqual("snapshot" in [c[0] for c in self.core_calls()], not paused)

    def paused_body(self, free: int, resume: int | str) -> str:
        return f"{free} GiB free; hourly snapshots resume above {resume} GiB. Older snapshots still expire normally."

    def test_the_paused_alert_names_where_snapshots_resume(self) -> None:
        # The larger of the two limits (snapshots_resume_above), not RESUME_ABOVE_GIB as written: the
        # bash program printed that since it was written, and the port kept it.
        for skip, resume in ((250, 100), (250, 270), (250, 0), (40, 45), (90, 45)):
            above = max(skip, resume)
            with self.subTest(skip=skip, resume=resume):
                self.conf(SKIP_AT_OR_BELOW_GIB=str(skip), RESUME_ABOVE_GIB=str(resume), EMERGENCY_AT_OR_BELOW_GIB="10")
                self.write(self.state_file("paused"), "")
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.state_file("alerted-paused"))
                self.machine(free=above * GIB)
                self.forget_calls()
                r = self.run_prog("hourly")
                self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
                body = self.paused_body(above, above)
                self.assertEqual(self.notified(), [self.alert("normal", "Snapshots paused: low space", body)])
                self.assertIn(log(4, f"Snapshots paused: low space: {body}"), self.lines(r.stdout))

    def test_the_paused_alert_reads_the_resume_limit_as_the_guard_does(self) -> None:
        # An expression is its value. (A RESUME_ABOVE_GIB bash cannot compute, abc or 1.5, no longer lets
        # the run go on with no limit while SKIP holds: test_an_invalid_guard_setting_stops_the_hourly_run.)
        for resume, free, paused, said in (
            ("2*150", 280, True, 300),
            ("-5", 200, False, 250),  # a negative limit never holds them
        ):
            with self.subTest(resume=resume):
                shutil.rmtree(self.state, ignore_errors=True)
                self.conf(SKIP_AT_OR_BELOW_GIB="250", RESUME_ABOVE_GIB=resume)
                if paused:
                    self.write(self.state_file("paused"), "")
                self.machine(free=free * GIB)
                self.forget_calls()
                r = self.run_prog("hourly")
                self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
                self.assertEqual(
                    self.notified(),
                    [self.alert("normal", "Snapshots paused: low space", self.paused_body(free, said))],
                )
                self.assertEqual(self.core_calls(), [self.prune_call()])

    def test_an_invalid_guard_setting_stops_the_hourly_run(self) -> None:
        # A space guard setting bash arithmetic cannot compute stops the run, naming the setting and its
        # value: no snapshot, no purge and no paused mark; the normal retention still prunes (the guard holds
        # back the snapshot, not the expiry). Before, 250GiB was bash's error line and a false test, so the
        # run went on with no limit; abc stopped it only where the comparison read it.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        for key in ("SKIP_AT_OR_BELOW_GIB", "RESUME_ABOVE_GIB", "EMERGENCY_AT_OR_BELOW_GIB"):
            for value in ("250GiB", "abc", "1 x", "x y]", "~5", "1<<3", "\N{NO-BREAK SPACE}250"):
                for free, paused in ((200, False), (300, False), (300, True)):
                    with self.subTest(key=key, value=value, free=free, paused=paused):
                        shutil.rmtree(self.state, ignore_errors=True)
                        self.conf(**{key: value})
                        if paused:
                            self.write(self.state_file("paused"), "")
                        self.machine(free=free * GIB)
                        self.forget_calls()
                        r = self.run_prog("hourly")
                        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                        self.assertIn(
                            log(
                                3,
                                f"{key}={value!r} in {conf}: not a number of GiB the space guard can hold at: no "
                                "snapshot until it is fixed",
                            ),
                            self.lines(r.stdout),
                        )
                        self.assertEqual(self.core_calls(), [self.prune_call()])
                        self.assertEqual(os.path.exists(self.state_file("paused")), paused)
                        self.assertFalse(os.path.exists(self.state_file("emergency")))
                        self.assertEqual(self.notified(), [])

    def test_a_guard_setting_past_64_bits_stops_the_hourly_run(self) -> None:
        # bash wrapped it around: 9223372036854775808 to -2**63 (no limit), 18446744073709551866 to 250. The
        # run stops after the normal retention's prune, as for any setting it cannot hold at.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        for key in ("SKIP_AT_OR_BELOW_GIB", "RESUME_ABOVE_GIB", "EMERGENCY_AT_OR_BELOW_GIB"):
            for value in ("9223372036854775808", "18446744073709551866", "2**63"):
                with self.subTest(key=key, value=value):
                    shutil.rmtree(self.state, ignore_errors=True)
                    self.conf(**{key: value})
                    self.machine(free=300 * GIB)
                    self.forget_calls()
                    r = self.run_prog("hourly")
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(
                        log(
                            3,
                            f"{key}={value!r} in {conf}: not a number of GiB the space guard can hold at: no "
                            "snapshot until it is fixed",
                        ),
                        self.lines(r.stdout),
                    )
                    self.assertEqual(self.core_calls(), [self.prune_call()])

    def test_above_a_valid_emergency_limit_an_invalid_guard_setting_ends_the_emergency(self) -> None:
        # The purge at 100 GiB free set the emergency mark and its alert's stamp. Once it has freed the disk,
        # the next run is above the valid EMERGENCY_AT_OR_BELOW_GIB: the mark and the stamp go, as on a run
        # with valid settings, so status no longer says EMERGENCY, and the normal retention prunes before the
        # run stops for RESUME_ABOVE_GIB (exit 1, its line). Before, the run stopped first: the mark stayed
        # and nothing expired until the setting was fixed.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        line = log(
            3,
            f"RESUME_ABOVE_GIB='abc' in {conf}: not a number of GiB the space guard can hold at: no snapshot until "
            "it is fixed",
        )
        self.conf(RESUME_ABOVE_GIB="abc", EMERGENCY_AT_OR_BELOW_GIB="150")
        self.machine(free=[100 * GIB, 200 * GIB])
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.state_file("emergency")))
        self.assertTrue(os.path.exists(self.state_file("alerted-emergency")))
        self.machine(free=200 * GIB)
        self.forget_calls()
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(line, self.lines(r.stdout))
        self.assertFalse(os.path.exists(self.state_file("emergency")))
        self.assertFalse(os.path.exists(self.state_file("alerted-emergency")))
        self.assertEqual(self.core_calls(), [self.prune_call()])
        # An invalid emergency limit gives no number to be above: its mark stays; the retention still prunes.
        self.conf(EMERGENCY_AT_OR_BELOW_GIB="150GiB")
        self.write(self.state_file("emergency"), "")
        self.forget_calls()
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.core_calls(), [self.prune_call()])
        self.assertTrue(os.path.exists(self.state_file("emergency")))

    def test_a_valid_emergency_limit_still_purges_when_another_guard_setting_is_invalid(self) -> None:
        # At or below a valid EMERGENCY_AT_OR_BELOW_GIB the purge runs, as it did before the guard's
        # settings were checked first; the run then stops for the invalid setting (exit 1, its line). An
        # invalid emergency limit itself stops the run with no purge (no number to purge at); the normal
        # retention still prunes.
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        for key, value, purged in (
            ("SKIP_AT_OR_BELOW_GIB", "250GiB", True),
            ("RESUME_ABOVE_GIB", "abc", True),
            ("RESUME_ABOVE_GIB", "\N{NO-BREAK SPACE}270", True),
            ("EMERGENCY_AT_OR_BELOW_GIB", "150GiB", False),
        ):
            with self.subTest(key=key, value=value):
                shutil.rmtree(self.state, ignore_errors=True)
                self.conf(**{key: value})
                self.machine(free=[100 * GIB, 110 * GIB])
                self.forget_calls()
                r = self.run_prog("hourly")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(
                    log(
                        3,
                        f"{key}={value!r} in {conf}: not a number of GiB the space guard can hold at: no "
                        "snapshot until it is fixed",
                    ),
                    self.lines(r.stdout),
                )
                want = [self.prune_call("--allow-mass-delete", policy="24h 3d")] if purged else [self.prune_call()]
                self.assertEqual(self.core_calls(), want)
                self.assertEqual(os.path.exists(self.state_file("emergency")), purged)
                if purged:
                    self.assertIn(log(4, "emergency purge done (rc=0); free now 110 GiB"), self.lines(r.stdout))

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
        # The purge ran: a clear line instead of the free space, and the run ends as every
        # emergency run ends (exit 0). Before, bash's arithmetic error on the empty reading
        # ended it with exit 1 and no "done" line. A lasting failure fails the next run
        # ("cannot read free space", test_free_space_unreadable).
        title = "Snapshots: emergency purge"
        body = "Only 150 GiB free (limit 150): keeping only the last day and 3 dailies. Free space on /home or /var."
        for second in (None, "garbage"):
            with self.subTest(second):
                self.forget_calls()
                self.machine(free=[150 * GIB, second])
                r = self.run_prog("hourly")
                self.assertEqual(
                    (r.returncode, self.lines(r.stdout), r.stderr),
                    (
                        0,
                        [
                            log(4, f"{title}: {body}"),
                            log(4, "emergency purge done (rc=0); free space cannot be read now"),
                        ],
                        "",
                    ),
                )
                self.assertEqual([c[0] for c in self.core_calls()], ["prune"])

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
        # Waiting for almost a day: still no alert.
        t = time.time() - 86400 + 120
        os.utime(self.state_file("pending-since"), (t, t))
        _, sent = self.wait_run("pending-since")
        self.assertEqual(sent, [])
        self.a_day_ago("pending-since")
        _, sent = self.wait_run("pending-since")
        # The alert names the mount that keeps it (before, only the tree).
        self.assertEqual(
            sent,
            [
                self.alert(
                    "normal",
                    "Snapshots: waiting for old trees to be deleted",
                    f"For over a day: {old} - mounted below them, so kept until unmounted: {old}/m ",
                )
            ],
        )
        self.assertTrue(os.path.isdir(old))

    def test_the_mount_table_is_read_once_for_every_old_tree(self) -> None:
        # The hourly run asked findmnt once per drop-pending entry; one reading serves them all (the
        # engine step still asks once per tree it keeps, as before).
        olds = [os.path.join(self.home, f".c{i}.pre-subvol-20261118T100000") for i in range(3)]
        for old in olds:
            os.makedirs(os.path.join(old, "m"))
        self.machine(mounts=["/", *(f"{old}/m" for old in olds)])
        self.write(self.state_file("drop-pending"), "".join(f"{old}\n" for old in olds))
        self.wait_run("pending-since")
        self.a_day_ago("pending-since")
        self.forget_calls()
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        held = " ".join(f"{old}/m" for old in olds)
        self.assertEqual(
            [str(a[5]) for a in self.notified()],
            [f"For over a day: {' '.join(olds)} - mounted below them, so kept until unmounted: {held} "],
        )
        self.assertEqual(len(self.calls("findmnt")), len(olds) + 1, self.calls("findmnt"))

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

    def test_an_aside_link_does_not_block_hourly_snapshots(self) -> None:
        # The Python planned_left listed a planned aside path that is a link (os.path.isdir follows
        # it), which put_aside never moves: snapshots waited for a reboot at every run.
        bulk_uv = os.path.join(self.home, "Bulk", "uv")
        os.makedirs(bulk_uv)
        cache = os.path.join(self.home, ".cache")
        os.makedirs(cache)
        link = os.path.join(cache, "uv")
        os.symlink(bulk_uv, link)
        self.write("etc/btrfs-setup/convert-plan", f"aside {link}\n")
        self.write("etc/btrfs-setup/convert-at-boot", "")
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(log(5, "all planned conversions done; snapshots start"), self.lines(r.stdout))
        self.assertEqual(os.readlink(link), bulk_uv)
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])

    def test_external_mountpoint_cache_does_not_block_hourly_snapshots(self) -> None:
        cache = os.path.join(self.home, ".cache")
        uv = os.path.join(cache, "uv")
        os.makedirs(uv)
        self.machine(mountpoints=[cache])
        self.write("etc/btrfs-setup/convert-plan", f"aside {uv}\ncopy {cache}\n")
        self.write("etc/btrfs-setup/convert-at-boot", "")
        r = self.run_prog("hourly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(log(5, "all planned conversions done; snapshots start"), self.lines(r.stdout))
        self.assertTrue(os.path.isdir(uv))
        self.assertEqual([c[0] for c in self.core_calls()], ["snapshot"])
        self.assertFalse(os.path.exists(self.path("etc/btrfs-setup/convert-at-boot")))

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

    def test_not_set_up_deletes_nothing_and_fails(self) -> None:
        # No HOME_DIR: nothing can be told apart as ours. Exit 1 with a journal line, as for a list
        # that cannot be read, so disable --purge does not report success. Before: "No snapshot
        # directory: nothing to delete." and exit 0, with the snapshots still there.
        self.make("home.20261101T0000")
        self.conf(HOME_DIR="")
        r = self.run_prog("purge")
        conf = self.path("etc/btrfs-setup/snapshots.conf")
        self.assertEqual(
            (r.returncode, self.lines(r.stdout), r.stderr),
            (1, [log(3, f"purge: not set up (no HOME_DIR in {conf}); nothing deleted")], ""),
        )
        self.assertEqual(self.core_calls(), [])
        self.assertEqual(self.calls("btrfs"), [])
        self.assertEqual(os.listdir(self.snapdir), ["home.20261101T0000"])

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

    def test_an_empty_listing_deletes_nothing_and_succeeds(self) -> None:
        self.core(list={"out": '{"sources": [{"name": "home", "snapshots": []}]}\n'})
        r = self.run_prog("purge")
        self.assertEqual(
            (r.returncode, self.lines(r.stdout), r.stderr), (0, ["Deleting 0 snapshot(s) in batches of 10..."], "")
        )
        self.assertFalse(os.path.exists(self.snapdir))

    def test_a_listing_that_cannot_be_read_deletes_nothing_and_fails(self) -> None:
        # Not JSON, or not the core's JSON: nothing deleted, exit 1 (so disable --purge does not
        # report success), and the state lists stay (they still describe the snapshots).
        # Before, the listing was read in a process substitution whose failure nobody saw:
        # "Deleting 0 snapshot(s)", exit 0, and both lists forgotten.
        self.make("home.20261101T0000")
        self.write(self.state_file("core-unfinished"), "")
        self.write(self.state_file("core-writable"), "home.x\n")
        for listing in ("not json\n", '{"other": 1}\n', '{"sources": [{"snapshots": [{"name": "x"}]}]}\n'):
            with self.subTest(listing):
                self.forget_calls()
                self.core(list={"out": listing})
                r = self.run_prog("purge")
                self.assertEqual(
                    (r.returncode, self.lines(r.stdout), r.stderr),
                    (
                        1,
                        [log(3, "purge: btrfs-snapshot-core's list is not readable (not its JSON); nothing deleted")],
                        "",
                    ),
                )
                self.assertEqual(self.calls("btrfs"), [])
                self.assertEqual(os.listdir(self.snapdir), ["home.20261101T0000"])
                self.assertEqual(sorted(os.listdir(self.state)), ["core-unfinished", "core-writable"])
                self.assertEqual(self.read_state("core-writable"), "home.x\n")


if __name__ == "__main__":
    unittest.main()
