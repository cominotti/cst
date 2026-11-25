# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs scrub-limit` (`btrfs/scrub_limit`)."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import io
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn, override
import unittest
from unittest import mock

from btrfs.scrub_limit.adapters import (
    ScrubPaths,
    scrub_limit_apply,
    scrub_limit_plan,
    scrub_limit_status,
)
from btrfs.scrub_limit.cli import ScrubCliDeps, run_scrub_limit_cli
from btrfs.scrub_limit.domain import (
    DEFAULT_SCRUB_LIMIT,
    ScrubLimitAction,
    ScrubParsedArgs,
    parse_scrub_dropin_limit,
    parse_scrub_limit_args,
    ScrubStatusSnapshot,
    render_scrub_dropin,
    scrub_limit_status_lines,
    update_snapshots_conf_scrub_limit,
    validate_scrub_size,
)
from btrfs.tests.fakes import RecordingInstallerRunner, ScrubFakeRunner, ScrubWorld, core_text
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.runtime import lc_ctype_handover
from cst_core.domain.install import GeneratedKind, Owner
from cst_core.domain.record import (
    ROOT_RECORD,
    USER_RECORD_REL,
    ComponentState,
    InstallRecord,
    RecordedFile,
    render_record,
)
from cst_core.ports import CommandFault, CommandResult
from tests.fault_injection import Killed, no_space
from tests.repo_files import REPO


class _KilledAfterInstaller:
    """A runner over inner that dies (Killed) right after the installer's text or remove-text call."""

    def __init__(self, inner: ScrubFakeRunner) -> None:
        self.inner = inner

    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        r = self.inner.run(argv, input_text=input_text, **kw)
        if "installer.py" in " ".join(argv):
            raise Killed(f"killed after {list(argv)}")
        return r


class _FakeCtx:
    def __init__(self, *, container: bool = False, root: bool = True, host_root: str = "/run/host") -> None:
        self._container = container
        self._root = root
        self._host_root = host_root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def path(self, host_path: str) -> str:
        if self._container and host_path.startswith("/"):
            return f"{self._host_root}/{host_path.lstrip('/')}"
        return host_path

    def etc_dir(self) -> str:
        return f"{self._host_root}/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        base = f"{self._host_root}/run" if self._container else "/run"
        return os.path.join(base, subpath.strip("/")) if subpath.strip("/") else base


class ScrubLimitDomainTest(unittest.TestCase):
    def test_parse_args_and_validate_size(self) -> None:
        self.assertEqual(parse_scrub_limit_args([]), parse_scrub_limit_args(["status"]))
        self.assertEqual(parse_scrub_limit_args(["help"]).action, ScrubLimitAction.HELP)
        self.assertEqual(parse_scrub_limit_args(["--help"]).action, ScrubLimitAction.HELP)
        self.assertEqual(parse_scrub_limit_args(["plan"]).limit, DEFAULT_SCRUB_LIMIT)
        self.assertEqual(parse_scrub_limit_args(["plan", "1G"]).limit, "1G")
        self.assertIsNone(parse_scrub_limit_args(["plan", "off"]).limit)
        self.assertEqual(parse_scrub_limit_args(["set"]).limit, DEFAULT_SCRUB_LIMIT)
        self.assertEqual(parse_scrub_limit_args(["set", "750m"]).limit, "750m")
        self.assertEqual(parse_scrub_limit_args(["off"]).action, ScrubLimitAction.OFF)
        self.assertIsNotNone(parse_scrub_limit_args(["set", "0"]).error)
        self.assertIsNotNone(parse_scrub_limit_args(["set", "1.5G"]).error)
        self.assertIsNotNone(parse_scrub_limit_args(["bogus"]).error)
        self.assertEqual(validate_scrub_size("500M"), ("500M", None))
        self.assertIsNotNone(validate_scrub_size("0G")[1])

    def test_a_help_word_after_the_subcommand_asks_for_help(self) -> None:
        for argv in (["set", "--help"], ["set", "500M", "-h"], ["off", "--help"], ["plan", "off", "help"]):
            with self.subTest(argv=argv):
                self.assertEqual(parse_scrub_limit_args(argv), ScrubParsedArgs(ScrubLimitAction.HELP))

    def test_status_names_the_one_command_that_fixes_a_drift(self) -> None:
        for conf, want in (
            ("NAS_ENABLED=1\n", "(run: cst btrfs scrub-limit off)"),
            (None, "(run: cst btrfs scrub-limit off)"),
            ("SCRUB_LIMIT='1G'\n", "(run: cst btrfs scrub-limit set 1G)"),
        ):
            with self.subTest(conf=conf):
                lines = scrub_limit_status_lines(
                    ScrubStatusSnapshot(
                        conf_path="/etc/btrfs-setup/snapshots.conf",
                        conf_text=conf,
                        conf_unreadable=False,
                        dropin_path="/etc/x/10-cst-limit.conf",
                        dropin_text=render_scrub_dropin("500M"),
                        live_sysfs=(),
                    )
                )
                self.assertEqual(lines[1], f"  service: 500M, DIFFERS from setting {want}")

    def test_render_dropin_matches_btrfs_setup_script_byte_for_byte(self) -> None:
        setup = os.path.join(REPO, "btrfs-setup.sh")
        # Compare directly against extracting scrub_limit_dropin from btrfs-setup.sh.
        with open(setup, encoding="utf-8") as fh:
            body = fh.read()
        start = body.index("scrub_limit_dropin() {")
        end = body.index("\n}\n", start) + 3
        script = "BTRFS_CONF=/etc/btrfs-setup/snapshots.conf\n" + body[start:end] + "\nscrub_limit_dropin 500M\n"
        res = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            check=True,
        )
        self.assertEqual(render_scrub_dropin("500M"), res.stdout)
        self.assertEqual(parse_scrub_dropin_limit(res.stdout), "500M")

    def test_update_snapshots_conf_preserves_other_keys_and_comments(self) -> None:
        fresh = update_snapshots_conf_scrub_limit(None, "500M")
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertIn("SCRUB_LIMIT='500M'\n", fresh)
        existing = "# keep me\nHOME_DIR='/var/home/u'\n#SCRUB_LIMIT=9G\nSCRUB_LIMIT='1G'\nNAS_ENABLED=1\n"
        updated = update_snapshots_conf_scrub_limit(existing, "500M")
        self.assertEqual(
            updated, "# keep me\nHOME_DIR='/var/home/u'\n#SCRUB_LIMIT=9G\nSCRUB_LIMIT='500M'\nNAS_ENABLED=1\n"
        )
        cleared = update_snapshots_conf_scrub_limit(updated, None)
        self.assertEqual(cleared, "# keep me\nHOME_DIR='/var/home/u'\n#SCRUB_LIMIT=9G\nNAS_ENABLED=1\n")
        self.assertIsNone(update_snapshots_conf_scrub_limit(None, None))


class ScrubLimitAdapterAndCliTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-scrub-limit-py-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.conf = os.path.join(self.tmp, "etc", "btrfs-setup", "snapshots.conf")
        self.dropin = os.path.join(
            self.tmp, "etc", "systemd", "system", "btrfs-scrub-weekly.service.d", "10-cst-limit.conf"
        )
        self.sysfs = os.path.join(self.tmp, "sys", "fs", "btrfs")
        self.paths = ScrubPaths(
            conf_path=self.conf,
            dropin_path=self.dropin,
            btrfs_sysfs=self.sysfs,
            installer_script=os.path.join(REPO, "cst_core", "installer.py"),
        )

    def test_plan_set_idempotent_status_and_off_cycle(self) -> None:
        os.makedirs(os.path.dirname(self.conf), exist_ok=True)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write("NAS_ENABLED=1\n")

        rc_plan, plan_lines = scrub_limit_plan(self.paths, "500M", runner=ScrubFakeRunner())
        self.assertEqual(rc_plan, 0)
        self.assertIn("Dry run: nothing is changed.", plan_lines)
        self.assertTrue(any("500M" in line for line in plan_lines))
        self.assertFalse(os.path.exists(self.dropin))
        with open(self.conf, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "NAS_ENABLED=1\n")

        runner = ScrubFakeRunner()
        rc_set, set_lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc_set, 0, set_lines)
        self.assertIn(f"written:   {self.dropin}", set_lines)
        self.assertIn(["systemctl", "daemon-reload"], runner.calls)
        with open(self.conf, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "NAS_ENABLED=1\nSCRUB_LIMIT='500M'\n")
        with open(self.dropin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), render_scrub_dropin("500M"))

        runner.calls.clear()
        rc_again, again_lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc_again, 0)
        self.assertIn(f"unchanged: {self.conf}", again_lines)
        self.assertIn(f"unchanged: {self.dropin}", again_lines)
        self.assertNotIn(["systemctl", "daemon-reload"], runner.calls)

        dev_sysfs = os.path.join(self.sysfs, "12345678-aaaa-bbbb", "devinfo", "1")
        os.makedirs(dev_sysfs, exist_ok=True)
        with open(os.path.join(dev_sysfs, "scrub_speed_max"), "w", encoding="utf-8") as fh:
            fh.write("524288000\n")
        rc_st, st_lines = scrub_limit_status(self.paths, runner=ScrubFakeRunner())
        self.assertEqual(rc_st, 0)
        self.assertIn("setting: 500M", "\n".join(st_lines))
        self.assertIn("service: 500M, as set", "\n".join(st_lines))
        self.assertIn("12345678 device 1 524288000", "\n".join(st_lines))

        runner.calls.clear()
        rc_off, off_lines = scrub_limit_apply(self.paths, None, runner=runner)
        self.assertEqual(rc_off, 0)
        self.assertIn(f"removed:   {self.dropin}", off_lines)
        self.assertIn(["systemctl", "daemon-reload"], runner.calls)
        self.assertFalse(os.path.exists(os.path.dirname(self.dropin)))
        with open(self.conf, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "NAS_ENABLED=1\n")

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _read(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def test_a_failed_daemon_reload_restores_both_files(self) -> None:
        restored = (
            "ERROR: systemctl daemon-reload failed: Access denied; previous configuration restored."
            " (run systemctl daemon-reload once it works)"
        )
        old_dropin = render_scrub_dropin("500M")
        for name, conf, dropin, target in (
            ("set, both new", None, None, "500M"),
            ("set, conf kept a key", "NAS_ENABLED=1\n", None, "500M"),
            ("set, from 500M to 1G", "SCRUB_LIMIT='500M'\n", old_dropin, "1G"),
            ("off", "SCRUB_LIMIT='500M'\n", old_dropin, None),
        ):
            with self.subTest(name=name):
                shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)
                if conf is not None:
                    self._write(self.conf, conf)
                if dropin is not None:
                    self._write(self.dropin, dropin)
                runner = ScrubFakeRunner()
                runner.fail_on = {"systemctl daemon-reload"}
                rc, lines = scrub_limit_apply(self.paths, target, runner=runner)
                self.assertEqual(rc, 1)
                self.assertEqual(lines[-1], restored)
                self.assertEqual(self._read(self.conf), conf)
                self.assertEqual(self._read(self.dropin), dropin)
                if conf is None:
                    self.assertFalse(os.path.exists(os.path.dirname(self.conf)))
                if dropin is None:
                    self.assertFalse(os.path.exists(os.path.dirname(self.dropin)))

    def test_a_failed_installer_restores_snapshots_conf(self) -> None:
        self._write(self.conf, "NAS_ENABLED=1\nSCRUB_LIMIT='1G'\n")
        runner = ScrubFakeRunner()
        runner.fail_on = {"installer.py"}
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1], "ERROR: the installer failed: Access denied; previous configuration restored.")
        self.assertEqual(self._read(self.conf), "NAS_ENABLED=1\nSCRUB_LIMIT='1G'\n")
        self.assertFalse(any(c[:2] == ["systemctl", "daemon-reload"] for c in runner.calls))

    def test_a_rollback_that_fails_says_so(self) -> None:
        self._write(self.conf, "NAS_ENABLED=1\n")
        runner = ScrubFakeRunner()
        runner.fail_on = {"systemctl daemon-reload", "installer.py remove-text"}
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 1)
        self.assertEqual(
            lines[-1],
            f"ERROR: systemctl daemon-reload failed: Access denied; ROLLBACK INCOMPLETE ({self.dropin}: Access denied)",
        )
        self.assertEqual(self._read(self.conf), "NAS_ENABLED=1\n")

    def _core_state(self, record: str) -> str:
        rec = JsonInstallRecord().read(record)
        assert rec is not None
        return rec.components.get("btrfs:core", "-")

    def _seeded(self, record: str, state: str) -> RecordingInstallerRunner:
        """A recording runner whose record has btrfs:core as an install leaves it: one of its files recorded,
        then done (or not yet)."""
        runner = RecordingInstallerRunner(record)
        runner.svc.put(core_text(os.path.join(self.tmp, "core.conf"), record), b"x\n", as_root=False)
        if state == "done":
            runner.svc.mark(record, "btrfs:core")
        return runner

    def _assert_keep_state_writes(self, runner: ScrubFakeRunner) -> None:
        """Every installer text call of scrub-limit passes --keep-state (the recording runner fails on a mark)."""
        texts = [c for c in runner.calls if "installer.py" in " ".join(c) and c[4] == "text"]
        self.assertTrue(texts)
        for c in texts:
            self.assertIn("--keep-state", c)

    def test_set_keeps_btrfs_core_s_state_in_the_install_record(self) -> None:
        for before in ("done", "pending"):
            with self.subTest(before=before):
                record = os.path.join(self.tmp, f"installed-{before}.json")
                runner = self._seeded(record, before)
                rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
                self.assertEqual(rc, 0, lines)
                self.assertIn(f"written:   {self.dropin}", lines)
                self.assertEqual(self._core_state(record), before)

                rc_again, _ = scrub_limit_apply(self.paths, "500M", runner=runner)
                self.assertEqual(rc_again, 0)
                self.assertEqual(self._core_state(record), before)
                rc_off, off_lines = scrub_limit_apply(self.paths, None, runner=runner)
                self.assertEqual(rc_off, 0, off_lines)
                self.assertIn(f"removed:   {self.dropin}", off_lines)
                self.assertEqual(self._core_state(record), before)
                self._assert_keep_state_writes(runner)
                shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)

    def test_a_rolled_back_change_keeps_btrfs_core_s_state(self) -> None:
        # A failed daemon-reload puts the 500M drop-in back through the installer, a second write: both
        # writes keep btrfs:core's state, done or pending.
        for before in ("done", "pending"):
            for target in ("1G", None):
                with self.subTest(before=before, target=target):
                    shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)
                    record = os.path.join(self.tmp, f"installed-{before}-{target}.json")
                    runner = self._seeded(record, before)
                    rc_set, set_lines = scrub_limit_apply(self.paths, "500M", runner=runner)
                    self.assertEqual(rc_set, 0, set_lines)
                    self.assertEqual(self._core_state(record), before)

                    runner.fail_on = {"systemctl daemon-reload"}
                    rc, lines = scrub_limit_apply(self.paths, target, runner=runner)
                    self.assertEqual(rc, 1)
                    self.assertEqual(
                        lines[-1],
                        "ERROR: systemctl daemon-reload failed: Access denied; previous configuration restored."
                        " (run systemctl daemon-reload once it works)",
                    )
                    self.assertEqual(self._read(self.dropin), render_scrub_dropin("500M"))
                    self.assertEqual(self._core_state(record), before)
                    self._assert_keep_state_writes(runner)

    def test_a_run_killed_after_its_installer_then_run_again_keeps_btrfs_core_s_state(self) -> None:
        # Run 1 dies right after the installer wrote the drop-in (before its daemon-reload); the command
        # again finds both files as asked, reloads systemd and exits 0. Before, btrfs:core stayed pending.
        for before in ("done", "pending"):
            for target in ("500M", None):
                with self.subTest(before=before, target=target):
                    shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)
                    record = os.path.join(self.tmp, f"installed-killed-{before}-{target}.json")
                    runner = self._seeded(record, before)
                    if target is None:  # off: from a 500M set that finished
                        self.assertEqual(scrub_limit_apply(self.paths, "500M", runner=runner)[0], 0)
                    killing = _KilledAfterInstaller(runner)
                    with self.assertRaises(Killed):
                        scrub_limit_apply(self.paths, target, runner=killing)
                    runner.need_daemon_reload = "yes"
                    rc, lines = scrub_limit_apply(self.paths, target, runner=runner)
                    self.assertEqual(rc, 0, lines)
                    self.assertIn(["systemctl", "daemon-reload"], runner.calls)
                    self.assertEqual(self._core_state(record), before)

    def test_a_drop_in_whose_record_line_was_lost_keeps_btrfs_core_done(self) -> None:
        # An earlier installer call wrote the drop-in and died before its record update: the line has the
        # old sha256, btrfs:core is done, both files are as asked. Before, the run left btrfs:core pending.
        record = os.path.join(self.tmp, "installed.json")
        runner = self._seeded(record, "done")
        runner.svc.put(core_text(self.dropin, record), render_scrub_dropin("1G").encode(), as_root=False)
        runner.svc.mark(record, "btrfs:core")
        with open(self.dropin, "w", encoding="utf-8") as fh:
            fh.write(render_scrub_dropin("500M"))
        os.makedirs(os.path.dirname(self.conf), exist_ok=True)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write("SCRUB_LIMIT='500M'\n")
        runner.need_daemon_reload = "no"
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual((rc, lines), (0, [f"unchanged: {self.conf}", f"unchanged: {self.dropin}"]))
        rec = JsonInstallRecord().read(record)
        assert rec is not None
        self.assertEqual(
            rec.files[self.dropin].sha256, hashlib.sha256(render_scrub_dropin("500M").encode()).hexdigest()
        )
        self.assertEqual(self._core_state(record), "done")

    def test_the_real_installer_takes_the_drop_in_write_and_keeps_btrfs_core_done(self) -> None:
        # The argv scrub-limit builds, through cst_core/installer.py's own parser (--root STAGE). The
        # installer writes under its stage, so scrub-limit's own reads of the drop-in find none: what is
        # checked is the installer's answer and its record (the user's under the stage, run as a user;
        # root's when the tests run as root).
        stage = os.path.join(self.tmp, "stage")
        if os.geteuid() == 0:
            record = stage + ROOT_RECORD
        else:
            record = stage + pwd.getpwuid(os.getuid()).pw_dir.rstrip("/") + "/" + USER_RECORD_REL
        os.makedirs(os.path.dirname(record))
        seed = RecordedFile(None, "btrfs:core", GeneratedKind.TEXT, "0" * 64, 0o644, Owner.USER)
        with open(record, "w", encoding="utf-8") as fh:
            fh.write(render_record(InstallRecord({"/etc/e2e-core-seed": seed}, {"btrfs:core": ComponentState.DONE})))
        runner = ScrubFakeRunner(installer_root=stage)
        with self.assertRaises(Killed):
            scrub_limit_apply(self.paths, "500M", runner=_KilledAfterInstaller(runner))
        self.assertEqual(self._core_state(record), "done")
        runner.need_daemon_reload = "yes"
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 0, lines)
        self.assertIn(f"unchanged: {stage}{self.dropin}", lines)
        rec = JsonInstallRecord().read(record)
        assert rec is not None
        self.assertEqual(rec.files[self.dropin].component, "btrfs:core")
        self.assertEqual(self._core_state(record), "done")

    def test_the_cli_says_a_full_disk_at_snapshots_conf_with_no_traceback(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        deps = ScrubCliDeps(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-scrub-limit"),
            env={"CST_PROG": "cst btrfs scrub-limit"},
            out=out,
            err=err,
            ctx=_FakeCtx(container=False, root=True),
            runner=ScrubFakeRunner(),
        )
        world = ScrubWorld(conf_path=self.conf, dropin_path=self.dropin)
        world.fail("settings replace", no_space())
        rc = run_scrub_limit_cli(["set", "500M"], deps=deps, build=world.build())
        said = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn(f"ERROR: cannot write {self.conf}: No space left on device", said)
        self.assertNotIn("Traceback", said)
        self.assertIsNone(world.conf)
        self.assertEqual(world.built_for, [False])  # set writes

    def _as_asked(self, limit: str | None) -> None:
        """snapshots.conf and the drop-in as set LIMIT (off: None) writes them, as a run killed before its
        daemon-reload left them."""
        self._write(self.conf, update_snapshots_conf_scrub_limit("NAS_ENABLED=1\n", limit) or "")
        if limit is not None:
            self._write(self.dropin, render_scrub_dropin(limit))
        elif os.path.exists(self.dropin):
            os.remove(self.dropin)

    def test_files_as_asked_and_systemd_behind_reloads_systemd(self) -> None:
        for limit in ("500M", None):
            with self.subTest(limit=limit):
                self._as_asked(limit)
                runner = ScrubFakeRunner()
                runner.need_daemon_reload = "yes\n"
                rc, lines = scrub_limit_apply(self.paths, limit, runner=runner)
                self.assertEqual(rc, 0, lines)
                self.assertIn(f"unchanged: {self.conf}", lines)
                show = ["systemctl", "show", "-p", "NeedDaemonReload", "btrfs-scrub-weekly.service"]
                self.assertIn(show, runner.calls)
                self.assertEqual(runner.calls[runner.calls.index(show) + 1 :], [["systemctl", "daemon-reload"]])
                self.assertEqual(
                    lines[-1],
                    "reloaded:  systemd had not loaded btrfs-scrub-weekly.service as it is on disk"
                    " (an earlier run stopped)",
                )

    def test_files_as_asked_and_systemd_in_step_runs_no_daemon_reload(self) -> None:
        self._as_asked("500M")
        runner = ScrubFakeRunner()
        runner.need_daemon_reload = "no\n"
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 0, lines)
        self.assertNotIn(["systemctl", "daemon-reload"], runner.calls)
        self.assertFalse(any(line.startswith("reloaded:") for line in lines), lines)

    def test_files_as_asked_and_systemctl_cannot_say_reloads_systemd(self) -> None:
        self._as_asked("500M")
        runner = ScrubFakeRunner()
        runner.fail_on = {"systemctl show"}
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(runner.calls[-1], ["systemctl", "daemon-reload"])
        # Not "systemd had not loaded": nobody could say so.
        self.assertEqual(
            lines[-1],
            "reloaded:  systemctl could not say whether systemd had loaded btrfs-scrub-weekly.service as it is on disk",
        )

    def _pid1_not_answering(self) -> tuple[ScrubFakeRunner, list[object]]:
        """A runner whose systemctl show and daemon-reload time out (PID 1 does not answer): each call
        with a time limit comes back as timed out; a daemon-reload with none would wait for ever, so the
        test fails there. The list gets the timeout each daemon-reload was given."""
        reloads: list[object] = []

        class Stuck(ScrubFakeRunner):
            @override
            def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
                if list(argv[:2]) in (["systemctl", "show"], ["systemctl", "daemon-reload"]):
                    self.calls.append(list(argv))
                    if argv[1] == "daemon-reload":
                        reloads.append(kw.get("timeout"))
                    if kw.get("timeout") is None:
                        raise AssertionError(f"{' '.join(argv)} with no time limit: the run would hang")
                    return CommandResult(
                        124, "", f"timed out after {kw['timeout']}s", argv=tuple(argv), fault=CommandFault.TIMEOUT
                    )
                return super().run(argv, input_text=input_text, **kw)

        return Stuck(), reloads

    def test_a_pid_1_that_does_not_answer_does_not_hang_a_run_with_nothing_to_change(self) -> None:
        self._as_asked("500M")
        conf_before, dropin_before = self._read(self.conf), self._read(self.dropin)
        runner, reloads = self._pid1_not_answering()
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(reloads, [60])
        self.assertEqual(
            lines[-1],
            "ERROR: systemctl daemon-reload failed: timed out after 60s; snapshots.conf and the drop-in are as"
            " asked, systemctl cannot say whether systemd has loaded them (run systemctl daemon-reload once it"
            " works)",
        )
        self.assertEqual((self._read(self.conf), self._read(self.dropin)), (conf_before, dropin_before))

    def test_a_pid_1_that_does_not_answer_does_not_hang_a_run_that_changes_only_snapshots_conf(self) -> None:
        # The drop-in as asked, snapshots.conf without SCRUB_LIMIT: the reload is still this run's catch-up,
        # with its time limit; when it fails, snapshots.conf goes back.
        self._as_asked("500M")
        self._write(self.conf, "NAS_ENABLED=1\n")
        runner, reloads = self._pid1_not_answering()
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 1, lines)
        self.assertEqual(reloads, [60])
        self.assertEqual(
            lines[-1],
            "ERROR: systemctl daemon-reload failed: timed out after 60s; previous configuration restored."
            " (run systemctl daemon-reload once it works)",
        )
        self.assertEqual(self._read(self.conf), "NAS_ENABLED=1\n")

    def test_a_reload_this_run_s_own_write_asks_for_keeps_no_time_limit(self) -> None:
        # The drop-in written by this run: its daemon-reload is the one of every change, as before.
        seen: list[object] = []

        class Recording(ScrubFakeRunner):
            @override
            def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
                if list(argv) == ["systemctl", "daemon-reload"]:
                    seen.append(kw.get("timeout"))
                return super().run(argv, input_text=input_text, **kw)

        rc, lines = scrub_limit_apply(self.paths, "500M", runner=Recording())
        self.assertEqual(rc, 0, lines)
        self.assertEqual(seen, [None])

    def test_plan_cannot_say_and_set_gets_an_answer_runs_no_reload(self) -> None:
        # plan from a toolbox may not reach the host's systemctl while set on the host does: plan's line
        # says the reload depends on that answer, and set with "no" runs none.
        self._as_asked("500M")
        asking = ScrubFakeRunner()
        asking.fail_on = {"systemctl show"}
        _, plan = scrub_limit_plan(self.paths, "500M", runner=asking)
        self.assertIn("unless set|off then gets an answer that it is", plan[-1])
        applied = ScrubFakeRunner()
        applied.need_daemon_reload = "no\n"
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=applied)
        self.assertEqual(rc, 0, lines)
        self.assertNotIn(["systemctl", "daemon-reload"], applied.calls)

    def test_plan_shows_the_daemon_reload_apply_runs(self) -> None:
        # The drop-in as asked: apply reloads systemd only when NeedDaemonReload is yes or systemctl
        # cannot say; plan shows the same reload, and with the same answers.
        reload_line = {
            "yes\n": "  reload:    btrfs-scrub-weekly.service (systemd has not loaded it as it is on disk)"
            " + systemctl daemon-reload",
            "no\n": None,
            "fails": "  reload:    btrfs-scrub-weekly.service (systemctl cannot say whether it is loaded)"
            " + systemctl daemon-reload, unless set|off then gets an answer that it is",
        }
        for limit in ("500M", None):
            for answer, want in reload_line.items():
                with self.subTest(limit=limit, answer=answer):
                    self._as_asked(limit)

                    def runner(answer: str = answer) -> ScrubFakeRunner:
                        r = ScrubFakeRunner()
                        if answer == "fails":
                            r.fail_on = {"systemctl show"}
                        else:
                            r.need_daemon_reload = answer
                        return r

                    rc, plan = scrub_limit_plan(self.paths, limit, runner=runner())
                    self.assertEqual(rc, 0, plan)
                    self.assertEqual(
                        [line for line in plan if line.startswith("  reload:")], [want] if want is not None else []
                    )
                    applied = runner()
                    rc, lines = scrub_limit_apply(self.paths, limit, runner=applied)
                    self.assertEqual(rc, 0, lines)
                    self.assertEqual(["systemctl", "daemon-reload"] in applied.calls, want is not None)

    def test_status_says_systemd_has_not_loaded_the_drop_in(self) -> None:
        self._as_asked("500M")
        line = (
            "  systemd: has not loaded btrfs-scrub-weekly.service as it is on disk (run: sudo systemctl daemon-reload)"
        )
        for answer, shown in (("yes\n", True), ("no\n", False), ("fails", False)):
            with self.subTest(answer=answer):
                runner = ScrubFakeRunner()
                if answer == "fails":
                    runner.fail_on = {"systemctl show"}
                else:
                    runner.need_daemon_reload = answer
                rc, lines = scrub_limit_status(self.paths, runner=runner)
                self.assertEqual(rc, 0, lines)
                self.assertEqual(line in lines, shown, lines)
                self.assertNotIn(["systemctl", "daemon-reload"], runner.calls)  # status changes nothing

    def test_asking_systemd_whether_it_loaded_the_files_has_a_time_limit(self) -> None:
        self._as_asked("500M")
        seen: list[object] = []

        class Recording(ScrubFakeRunner):
            @override
            def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
                if list(argv[:2]) == ["systemctl", "show"]:
                    seen.append(kw.get("timeout"))
                return super().run(argv, input_text=input_text, **kw)

        rc, lines = scrub_limit_apply(self.paths, "500M", runner=Recording())
        self.assertEqual(rc, 0, lines)
        self.assertEqual(seen, [20])

    def test_the_question_to_systemd_is_read_in_the_c_locale_on_the_host_for_plan_and_status(self) -> None:
        # SystemctlAdapter.show's read: the C locale (the answer is parsed), on the host from a toolbox for
        # the read-only commands, never for set|off (which run on the host).
        self._as_asked("500M")
        seen: list[tuple[object, object]] = []

        class Recording(ScrubFakeRunner):
            @override
            def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
                if list(argv[:2]) == ["systemctl", "show"]:
                    seen.append((kw.get("locale_c"), kw.get("on_host")))
                return super().run(argv, input_text=input_text, **kw)

        for name, call in (
            ("status", lambda r: scrub_limit_status(self.paths, runner=r)),
            ("plan", lambda r: scrub_limit_plan(self.paths, "500M", runner=r)),
            ("set", lambda r: scrub_limit_apply(self.paths, "500M", runner=r)),
        ):
            with self.subTest(command=name):
                seen.clear()
                rc, lines = call(Recording())
                self.assertEqual(rc, 0, lines)
                self.assertEqual(seen, [(True, name != "set")])

    def test_files_as_asked_and_a_failed_reload_says_systemd_has_not_loaded_them(self) -> None:
        self._as_asked("500M")
        conf_before, dropin_before = self._read(self.conf), self._read(self.dropin)
        runner = ScrubFakeRunner()
        runner.need_daemon_reload = "yes\n"
        runner.fail_on = {"systemctl daemon-reload"}
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 1)
        self.assertEqual(
            lines[-1],
            "ERROR: systemctl daemon-reload failed: Access denied; snapshots.conf and the drop-in are as asked,"
            " systemd has not loaded them (run systemctl daemon-reload once it works)",
        )
        self.assertEqual((self._read(self.conf), self._read(self.dropin)), (conf_before, dropin_before))
        self.assertEqual(sum("installer.py" in " ".join(c) for c in runner.calls), 1)  # its first call only

    def test_an_unreadable_drop_in_stops_set_before_any_write(self) -> None:
        os.makedirs(self.dropin)  # a folder where the drop-in goes: it cannot be read back
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=ScrubFakeRunner())
        self.assertEqual((rc, lines), (1, [f"ERROR: cannot read {self.dropin}"]))
        self.assertIsNone(self._read(self.conf))

    def test_container_refusal_and_non_root_elevation(self) -> None:
        entry = os.path.join(REPO, "btrfs", "btrfs-scrub-limit")
        out, err = io.StringIO(), io.StringIO()
        rc = run_scrub_limit_cli(
            ["set", "500M"],
            deps=ScrubCliDeps(
                entry_file=entry,
                env={"CST_PROG": "cst btrfs scrub-limit"},
                out=out,
                err=err,
                ctx=_FakeCtx(container=True, root=False),
            ),
        )
        self.assertEqual(rc, 1)
        self.assertIn("must run on the host", err.getvalue())

        elevated: list[list[str]] = []

        def _elevate(argv: list[str]) -> NoReturn:
            elevated.append(argv)
            raise SystemExit(99)

        deps = ScrubCliDeps(
            entry_file=entry,
            env={"CST_PROG": "cst btrfs scrub-limit"},
            out=io.StringIO(),
            err=io.StringIO(),
            ctx=_FakeCtx(container=False, root=False),
            elevate=_elevate,
        )
        with self.assertRaises(SystemExit):
            run_scrub_limit_cli(["set", "500M"], deps=deps)
        self.assertEqual(len(elevated), 1)
        self.assertEqual(elevated[0][0], "sudo")

        # The command line root runs, as the bash front's "sudo -- env CST_PROG=... SELF ARGS" ran it: the
        # system Python on this entry, every argument passed on.
        for args in (["set", "500M"], ["off"]):
            deps = ScrubCliDeps(
                entry_file=entry,
                env={"CST_PROG": "cst btrfs scrub-limit"},
                out=io.StringIO(),
                err=io.StringIO(),
                ctx=_FakeCtx(container=False, root=False),
                elevate=_elevate,
            )
            with (
                self.subTest(args=args),
                mock.patch.object(sys, "executable", "/home/u/.venv/bin/python3"),
                self.assertRaises(SystemExit),
            ):
                run_scrub_limit_cli(args, deps=deps)
            self.assertEqual(
                elevated[-1],
                [
                    "sudo",
                    "--",
                    "env",
                    "CST_PROG=cst btrfs scrub-limit",
                    lc_ctype_handover(),
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    os.path.realpath(entry),
                    *args,
                ],
            )

    def test_cli_help_and_btrfs_setup_dispatch(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = run_scrub_limit_cli(
            ["help"], deps=ScrubCliDeps(env={"CST_PROG": "cst btrfs scrub-limit"}, out=out, err=err)
        )
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertIn("SCRUB_LIMIT", out.getvalue())

        r = subprocess.run(
            ["bash", os.path.join(REPO, "btrfs-setup.sh"), "scrub-limit", "--help"],
            env={"PATH": "/usr/bin:/bin", "HOME": self.tmp},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scrub-limit: manage the weekly btrfs scrub", r.stdout)

        r_cst = subprocess.run(
            ["bash", os.path.join(REPO, "btrfs-setup.sh"), "scrub-limit", "--help"],
            env={"PATH": "/usr/bin:/bin", "HOME": self.tmp, "CST_PROG": "cst btrfs"},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r_cst.returncode, 0, r_cst.stderr)
        self.assertIn("Usage: cst btrfs scrub-limit", r_cst.stdout)

        r_bin = subprocess.run(
            [os.path.join(REPO, "btrfs", "btrfs-scrub-limit"), "set", "fast"],
            env={"PATH": "/usr/bin:/bin", "HOME": self.tmp},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r_bin.returncode, 2)
        self.assertIn("is not a valid size", r_bin.stderr)


if __name__ == "__main__":
    unittest.main()
