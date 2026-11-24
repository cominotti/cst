# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs scrub-limit` (`btrfs/scrub_limit`)."""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import io
import os
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
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.adapters.runtime import lc_ctype_handover
from cst_core.application.install import InstallService, Placement
from cst_core.domain.install import GeneratedKind, Owner
from cst_core.ports import CommandResult
from tests.repo_files import REPO


class _FakeRunner:
    def __init__(self, *, installer_root: str = "") -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._installer_root = installer_root
        # a part of the command line ("systemctl daemon-reload", "installer.py text") whose calls exit 1
        self.fail_on: set[str] = set()

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        **_kw: object,
    ) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        self.inputs.append(input_text)
        if any(part in " ".join(cmd) for part in self.fail_on):
            return CommandResult(1, "", "Access denied", argv=tuple(cmd))
        if "installer.py" in " ".join(cmd):
            return self._installer(cmd, input_text)
        return CommandResult(0, "", "", argv=tuple(cmd))

    def _installer(self, cmd: list[str], input_text: str | None) -> CommandResult:
        if self._installer_root:
            real = subprocess.run(
                [sys.executable, "-I", "-B", cmd[3], cmd[4], "--root", self._installer_root, *cmd[5:]],
                input=input_text,
                capture_output=True,
                text=True,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                check=False,
            )
            return CommandResult(real.returncode, real.stdout, real.stderr, argv=tuple(cmd))
        if "text" in cmd:
            dest = cmd[cmd.index("text") + 1]
            existed = os.path.exists(dest)
            old = None
            if existed:
                with open(dest, encoding="utf-8") as fh:
                    old = fh.read()
            if old == input_text:
                return CommandResult(0, f"unchanged: {dest}\n", "", argv=tuple(cmd))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(input_text or "")
            return CommandResult(0, f"written:   {dest}\n", "", argv=tuple(cmd))
        if "remove-text" in cmd:
            dest = cmd[cmd.index("remove-text") + 1]
            if os.path.exists(dest):
                os.remove(dest)
                return CommandResult(0, f"removed:   {dest}\n", "", argv=tuple(cmd))
            return CommandResult(0, "", "", argv=tuple(cmd))
        return CommandResult(0, "", "", argv=tuple(cmd))


def _core_text(path: str, record: str) -> Placement:
    """A generated text of btrfs:core at path, listed in record (as installer.py text --component writes it)."""
    return Placement(
        path=path,
        dest=path,
        record=record,
        component="btrfs:core",
        src=None,
        kind=GeneratedKind.TEXT,
        owner=Owner.ROOT,
        mode=0o644,
    )


class _RecordingInstallerRunner(_FakeRunner):
    """installer.py text, remove-text and mark as the installer runs them as root: the files written and
    removed, and their lines and btrfs:core's state in one install record (a write sets it pending)."""

    def __init__(self, record: str) -> None:
        super().__init__()
        self.record = record
        self.svc = InstallService(LocalInstallTarget(), lambda _e: b"", record=JsonInstallRecord())
        self.marks = 0

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        if "installer.py" not in " ".join(cmd) or any(part in " ".join(cmd) for part in self.fail_on):
            return super().run(argv, input_text=input_text, **kw)
        self.calls.append(cmd)
        word = cmd[4]
        if word == "mark":
            self.marks += 1
            self.svc.mark(self.record, cmd[cmd.index("--component") + 1])
            return CommandResult(0, "", "", argv=tuple(cmd))
        place = _core_text(cmd[5], self.record)
        if word == "text":
            os.makedirs(os.path.dirname(place.path), exist_ok=True)
            written = self.svc.put(place, (input_text or "").encode(), as_root=False)
            return CommandResult(0, f"{'written:  ' if written else 'unchanged:'} {place.path}\n", "", argv=tuple(cmd))
        return CommandResult(0, "".join(f"{line}\n" for line in self.svc.remove([place])), "", argv=tuple(cmd))


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
            # never the real /var/lib/cst/installed.json: apply reads btrfs:core's state there
            record_path=os.path.join(self.tmp, "installed.json"),
        )

    def test_plan_set_idempotent_status_and_off_cycle(self) -> None:
        os.makedirs(os.path.dirname(self.conf), exist_ok=True)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write("NAS_ENABLED=1\n")

        rc_plan, plan_lines = scrub_limit_plan(self.paths, "500M")
        self.assertEqual(rc_plan, 0)
        self.assertIn("Dry run: nothing is changed.", plan_lines)
        self.assertTrue(any("500M" in line for line in plan_lines))
        self.assertFalse(os.path.exists(self.dropin))
        with open(self.conf, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "NAS_ENABLED=1\n")

        runner = _FakeRunner()
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
        rc_st, st_lines = scrub_limit_status(self.paths)
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
                runner = _FakeRunner()
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
        runner = _FakeRunner()
        runner.fail_on = {"installer.py"}
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=runner)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1], "ERROR: the installer failed: Access denied; previous configuration restored.")
        self.assertEqual(self._read(self.conf), "NAS_ENABLED=1\nSCRUB_LIMIT='1G'\n")
        self.assertFalse(any(c[:2] == ["systemctl", "daemon-reload"] for c in runner.calls))

    def test_a_rollback_that_fails_says_so(self) -> None:
        self._write(self.conf, "NAS_ENABLED=1\n")
        runner = _FakeRunner()
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

    def test_set_keeps_btrfs_core_done_in_the_install_record(self) -> None:
        for before in ("done", "pending"):
            with self.subTest(before=before):
                record = os.path.join(self.tmp, f"installed-{before}.json")
                paths = dataclasses.replace(self.paths, record_path=record)
                runner = _RecordingInstallerRunner(record)
                # btrfs:core as an install leaves it: one of its files recorded, then done (or not yet).
                runner.svc.put(_core_text(os.path.join(self.tmp, "core.conf"), record), b"x\n", as_root=False)
                if before == "done":
                    runner.svc.mark(record, "btrfs:core")
                rc, lines = scrub_limit_apply(paths, "500M", runner=runner)
                self.assertEqual(rc, 0, lines)
                self.assertIn(f"written:   {self.dropin}", lines)
                self.assertEqual(self._core_state(record), before)
                self.assertEqual(runner.marks, 1 if before == "done" else 0)

                rc_again, _ = scrub_limit_apply(paths, "500M", runner=runner)
                self.assertEqual(rc_again, 0)
                self.assertEqual(runner.marks, 1 if before == "done" else 0)
                rc_off, off_lines = scrub_limit_apply(paths, None, runner=runner)
                self.assertEqual(rc_off, 0, off_lines)
                self.assertIn(f"removed:   {self.dropin}", off_lines)
                self.assertEqual(self._core_state(record), before)
                shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)

    def test_a_rolled_back_change_keeps_btrfs_core_s_state(self) -> None:
        # A failed daemon-reload puts the 500M drop-in back through the installer, a second write: a
        # btrfs:core that was done before is marked done again; one that was pending stays pending.
        for before in ("done", "pending"):
            for target in ("1G", None):
                with self.subTest(before=before, target=target):
                    shutil.rmtree(os.path.join(self.tmp, "etc"), ignore_errors=True)
                    record = os.path.join(self.tmp, f"installed-{before}-{target}.json")
                    paths = dataclasses.replace(self.paths, record_path=record)
                    runner = _RecordingInstallerRunner(record)
                    runner.svc.put(_core_text(os.path.join(self.tmp, "core.conf"), record), b"x\n", as_root=False)
                    if before == "done":
                        runner.svc.mark(record, "btrfs:core")
                    rc_set, set_lines = scrub_limit_apply(paths, "500M", runner=runner)
                    self.assertEqual(rc_set, 0, set_lines)
                    self.assertEqual(self._core_state(record), before)

                    runner.fail_on = {"systemctl daemon-reload"}
                    rc, lines = scrub_limit_apply(paths, target, runner=runner)
                    self.assertEqual(rc, 1)
                    self.assertEqual(
                        lines[-1],
                        "ERROR: systemctl daemon-reload failed: Access denied; previous configuration restored."
                        " (run systemctl daemon-reload once it works)",
                    )
                    self.assertEqual(self._read(self.dropin), render_scrub_dropin("500M"))
                    self.assertEqual(self._core_state(record), before)

    def test_a_rollback_that_cannot_mark_btrfs_core_done_says_so(self) -> None:
        record = os.path.join(self.tmp, "installed.json")
        paths = dataclasses.replace(self.paths, record_path=record)
        runner = _RecordingInstallerRunner(record)
        runner.svc.put(_core_text(os.path.join(self.tmp, "core.conf"), record), b"x\n", as_root=False)
        runner.svc.mark(record, "btrfs:core")
        runner.fail_on = {"systemctl daemon-reload", "installer.py mark"}
        rc, lines = scrub_limit_apply(paths, "500M", runner=runner)
        self.assertEqual(rc, 1)
        self.assertEqual(
            lines[-1],
            "ERROR: systemctl daemon-reload failed: Access denied;"
            " ROLLBACK INCOMPLETE (btrfs:core not marked done again: Access denied)",
        )
        self.assertIsNone(self._read(self.dropin))

    def test_a_failed_mark_is_an_error(self) -> None:
        record = os.path.join(self.tmp, "installed.json")
        runner = _RecordingInstallerRunner(record)
        runner.svc.put(_core_text(os.path.join(self.tmp, "core.conf"), record), b"x\n", as_root=False)
        runner.svc.mark(record, "btrfs:core")
        runner.fail_on = {"installer.py mark"}
        rc, lines = scrub_limit_apply(dataclasses.replace(self.paths, record_path=record), "500M", runner=runner)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1], "ERROR: the installer could not mark btrfs:core done: Access denied")

    def test_an_unreadable_drop_in_stops_set_before_any_write(self) -> None:
        os.makedirs(self.dropin)  # a folder where the drop-in goes: it cannot be read back
        rc, lines = scrub_limit_apply(self.paths, "500M", runner=_FakeRunner())
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
