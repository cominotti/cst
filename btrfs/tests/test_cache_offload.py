# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs cache offload` domain, application, adapters, and CLI wiring."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import io
import json
import os
import sqlite3
import tempfile
from typing import override
import unittest
from unittest import mock

from btrfs.offload import adapters, wiring
from btrfs.offload.application import OffloadService
from btrfs.offload.domain import (
    OFFLOAD_CONF_REL,
    OFFLOAD_STATE_REL,
    OffloadAction,
    OffloadBackupStateRecord,
    OffloadEntry,
    OffloadEntryState,
    OffloadMode,
    OffloadParsedArgs,
    format_offload_usage,
    parse_offload_args,
    parse_offload_config,
    render_starter_config,
    select_offload_entries,
)
from btrfs.offload.domain.plan import (
    OffloadEntryProbe,
    PathProbe,
    ScratchpadProbe,
    classify_entry_state,
    parse_backup_record,
    render_backup_record,
)
from btrfs.offload.domain.units import OFFLOAD_SERVICE, OFFLOAD_TIMER, render_offload_service, render_offload_timer
from cst_core.adapters.nocow import FS_NOCOW_FL
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.ports import CommandResult, CommandRunnerPort, DecodeErrors, OutputMode, StdinMode


class _FakeCtx:
    def __init__(self, *, container: bool = False) -> None:
        self._container = container

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return False

    def path(self, host_path: str) -> str:
        return host_path

    def etc_dir(self) -> str:
        return "/etc"

    def host_run_dir(self, subpath: str) -> str:
        return os.path.join("/run/host", subpath.lstrip("/"))


class _FakeRunner(CommandRunnerPort):
    def __init__(self, *, findmnt_mounted: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._findmnt_mounted = findmnt_mounted

    @override
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        unset: Sequence[str] = (),
        base_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        locale_c: bool = False,
        new_session: bool = False,
        on_host: bool = False,
        stdin: StdinMode = "devnull",
        output: OutputMode = OutputMode.CAPTURE,
        decode_errors: DecodeErrors = "replace",
        nice: int | None = None,
        pass_fds: Sequence[int] = (),
        on_line: Callable[[str], None] | None = None,
    ) -> CommandResult:
        del check, timeout, input_text, env, unset, base_env, cwd, locale_c
        del new_session, on_host, stdin, output, decode_errors, nice, pass_fds, on_line
        self.calls.append(list(argv))
        if argv and argv[0] == "findmnt" and self._findmnt_mounted:
            target = argv[-1]
            payload = {
                "filesystems": [{"target": target, "fstype": "btrfs", "source": "/dev/mapper/cache[/scratchpad]"}]
            }
            return CommandResult(0, json.dumps(payload), "", argv=tuple(argv))
        if argv and argv[0] == "systemctl":
            if "is-active" in argv:
                return CommandResult(3, "inactive\n", "", argv=tuple(argv))
            if "is-enabled" in argv:
                return CommandResult(1, "disabled\n", "", argv=tuple(argv))
            return CommandResult(0, "", "", argv=tuple(argv))
        return CommandResult(0, "", "", argv=tuple(argv))


class _FakeHost:
    def __init__(self, home: str, *, busy: tuple[str, ...] = (), apply_fail_on: str | None = None) -> None:
        self._home = home
        self._busy = busy
        self._apply_fail_on = apply_fail_on
        self.files: dict[str, str] = {}
        self.applied: list[str] = []
        self.rolled_back: list[str] = []
        self.reverted: list[str] = []
        self.backed_up: list[str] = []
        self.backup_fail_on: str | None = None
        self.removed_trees: list[str] = []
        self.symlinks: set[str] = set()
        self.dirs: set[str] = {home, os.path.join(home, "Scratchpad")}
        self.lock_busy = False
        self.now = 1_700_000_000.0

    def home_dir(self) -> str:
        return self._home

    def resolve_path(self, path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))

    def resolve_realpath(self, path: str) -> str:
        return path

    def prog_name(self) -> str:
        return "cst btrfs cache offload"

    def now_epoch(self) -> float:
        return self.now

    def read_text_opt(self, path: str, limit: int = 65536) -> str | None:
        del limit
        return self.files.get(path)

    def path_exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs or path in self.symlinks

    def write_text_atomic(self, path: str, content: str, *, mode: int = 0o644) -> None:
        del mode
        self.files[path] = content

    def ensure_dir(self, path: str, *, mode: int = 0o700) -> None:
        del mode
        self.dirs.add(path)

    def probe_scratchpad(self, home: str) -> ScratchpadProbe:
        sp = os.path.join(home, "Scratchpad")
        return ScratchpadProbe(
            path=sp, exists=sp in self.dirs, is_dir=sp in self.dirs, is_symlink=False, mounted=True, fstype="btrfs"
        )

    def probe_entry(self, home: str, entry: OffloadEntry) -> OffloadEntryProbe:
        src = os.path.join(home, entry.source_rel)
        sp = os.path.join(home, entry.scratchpad_rel)
        mir = os.path.join(home, entry.mirror_rel)
        is_link = src in self.symlinks
        return OffloadEntryProbe(
            entry=entry,
            source=PathProbe(
                path=src,
                exists=is_link or src in self.dirs,
                is_dir=(src in self.dirs) and not is_link,
                is_symlink=is_link,
                link_target=sp if is_link else "",
                resolved_target=sp if is_link else "",
                nonempty=src in self.dirs,
            ),
            scratchpad=PathProbe(
                path=sp,
                exists=sp in self.dirs,
                is_dir=sp in self.dirs,
                is_symlink=False,
                nonempty=sp in self.dirs,
                nocow_enabled=entry.mode == OffloadMode.NOCOW,
            ),
            mirror=PathProbe(path=mir, exists=mir in self.dirs, is_dir=mir in self.dirs, is_symlink=False),
            busy=self._busy,
        )

    def apply_offload_tree(
        self,
        *,
        source_abs: str,
        scratchpad_abs: str,
        mode: OffloadMode,
        nocow_paths: Sequence[str],
    ) -> str | None:
        del mode, nocow_paths
        if self._apply_fail_on is not None and self._apply_fail_on in source_abs:
            return "simulated copy failure"
        self.dirs.discard(source_abs)
        self.dirs.add(scratchpad_abs)
        self.symlinks.add(source_abs)
        self.applied.append(source_abs)
        return None

    def rollback_offload_tree(self, *, source_abs: str, scratchpad_abs: str, was_pending: bool = False) -> str | None:
        del was_pending
        self.symlinks.discard(source_abs)
        self.dirs.discard(scratchpad_abs)
        self.dirs.add(source_abs)
        self.rolled_back.append(source_abs)
        return None

    def revert_offload_tree(self, *, source_abs: str, scratchpad_abs: str) -> str | None:
        self.symlinks.discard(source_abs)
        self.dirs.discard(scratchpad_abs)
        self.dirs.add(source_abs)
        self.reverted.append(source_abs)
        return None

    def backup_offload_tree(
        self,
        entry: OffloadEntry,
        *,
        scratchpad_abs: str,
        mirror_abs: str,
    ) -> tuple[int, str | None]:
        del scratchpad_abs
        if self.backup_fail_on == entry.name:
            return 0, "sqlite backup error"
        self.dirs.add(mirror_abs)
        self.backed_up.append(entry.name)
        return 2, None

    def remove_tree(self, path: str) -> str | None:
        self.dirs.discard(path)
        self.removed_trees.append(path)
        return None

    def acquire_lock(self, path: str) -> int | None:
        del path
        return None if self.lock_busy else 42

    def release_lock(self, fd: int) -> None:
        del fd


class _FakeUnits:
    def __init__(self, *, unit_err: str | None = None) -> None:
        self.reloads = 0
        self.enabled = False
        self.disabled = False
        self.unit_err = unit_err

    def unit_dir(self, home: str) -> str:
        return os.path.join(home, ".config/systemd/user")

    def probe_units(self, home: str) -> tuple[str, str, bool, bool]:
        del home
        return ("active" if self.enabled else "inactive", "enabled" if self.enabled else "disabled", False, False)

    def enable_backup_timer(self) -> str | None:
        if self.unit_err is not None:
            return self.unit_err
        self.enabled = True
        return None

    def disable_backup_timer(self) -> str | None:
        self.enabled = False
        self.disabled = True
        return None

    def reload_daemon(self) -> str | None:
        self.reloads += 1
        return None


class _FakeInstaller:
    def __init__(self) -> None:
        self.installed: list[str] = []
        self.forgotten: list[str] = []

    def install_backup_units(self, home: str, unit_dir: str, on_calendar: str) -> tuple[bool, str | None]:
        del home, on_calendar
        self.installed.append(unit_dir)
        return True, None

    def forget_backup_units(self, home: str, unit_dir: str) -> str | None:
        del home
        self.forgotten.append(unit_dir)
        return None


class OffloadDomainTests(unittest.TestCase):
    """Test pure TOML validation, CLI args, presets, unit rendering, and classification."""

    def test_starter_config_is_valid_with_zero_active_entries(self) -> None:
        cfg = parse_offload_config(render_starter_config(), "/work/test.toml")
        self.assertEqual(cfg.problems, ())
        self.assertEqual(cfg.entries, ())
        self.assertEqual(cfg.backup_on_calendar, "daily")

    def test_rejects_toolboxes_and_reserved_roots(self) -> None:
        for forbidden in (
            ".local/share/containers",
            "~/.local/share/containers/storage",
            ".local/share",
            ".local",
            ".config",
            ".local/share/cst",
            ".local/share/cst/scratchpad-mirrors",
            "~",
            "Scratchpad/foo",
            ".cache/pip",
            "Bulk",
            ".config/cst",
            "../escape",
            "/abs/outside",
        ):
            raw = f'version = 1\n[entries.bad]\nsource = "{forbidden}"\n'
            cfg = parse_offload_config(raw, "/work/bad.toml")
            self.assertTrue(cfg.problems, f"expected rejection for {forbidden}")

    def test_rejects_overlapping_sources_or_scratchpad_subpaths(self) -> None:
        raw = """
version = 1
[entries.parent]
source = ".gemini"
[entries.child]
source = ".gemini/antigravity-cli"
"""
        cfg = parse_offload_config(raw, "/work/overlap.toml")
        self.assertTrue(any("overlap" in p for p in cfg.problems))

        raw_sp = """
version = 1
[entries.one]
source = ".claude"
scratchpad_subpath = "offload/shared"
[entries.two]
source = ".codex"
scratchpad_subpath = "offload/shared/nested"
"""
        cfg2 = parse_offload_config(raw_sp, "/work/sp.toml")
        self.assertTrue(any("overlap" in p for p in cfg2.problems))

    def test_parses_valid_modes_and_selects_subset(self) -> None:
        raw = """
version = 1
backup_on_calendar = "weekly"
[entries.gemini]
source = "~/.gemini/antigravity-cli"
mode = "zstd"
nocow_paths = ["*.db", "wal"]
[entries.codex]
source = ".codex"
mode = "nocow"
backup = false
"""
        cfg = parse_offload_config(raw, "/work/ok.toml")
        self.assertEqual(cfg.problems, ())
        self.assertEqual(len(cfg.entries), 2)
        self.assertEqual(cfg.entries[0].source_rel, ".gemini/antigravity-cli")
        self.assertEqual(cfg.entries[1].mode, OffloadMode.NOCOW)
        subset, missing = select_offload_entries(cfg.entries, ("codex", "missing"))
        self.assertEqual([e.name for e in subset], ["codex"])
        self.assertEqual(missing, ["missing"])

    def test_cli_args_and_unit_templates(self) -> None:
        self.assertEqual(parse_offload_args([]).action, OffloadAction.STATUS)
        self.assertTrue(parse_offload_args(["init", "--force"]).force)
        self.assertIsNone(parse_offload_args(["apply", "--no-backup-timer"]).error)
        self.assertIsNotNone(parse_offload_args(["status", "--force"]).error)
        self.assertIn("backup", render_offload_service())
        self.assertIn("Persistent=true", render_offload_timer("daily"))
        self.assertIn(OFFLOAD_SERVICE, render_offload_service() + OFFLOAD_SERVICE)
        self.assertIn("cst-scratchpad-backup.timer", render_offload_timer("daily") + OFFLOAD_TIMER)
        self.assertIn("apply", format_offload_usage("cst btrfs cache offload"))

    def test_backup_record_roundtrip_and_state_classification(self) -> None:
        rec: OffloadBackupStateRecord = {
            "version": 1,
            "entries": {
                "gemini": {"last_backup_epoch": 123.5, "sqlite_dbs": 2, "ok": True, "detail": "ok"},
            },
        }
        parsed, err = parse_backup_record(render_backup_record(rec))
        self.assertIsNone(err)
        self.assertIsNotNone(parsed)
        entry = OffloadEntry(
            name="gemini",
            source_rel=".gemini/cli",
            scratchpad_rel="Scratchpad/offload/gemini",
            mirror_rel=".local/share/cst/scratchpad-mirrors/gemini",
            mode=OffloadMode.ZSTD,
            backup=True,
            nocow_paths=(),
            sqlite_globs=("*.db",),
        )
        broken = OffloadEntryProbe(
            entry=entry,
            source=PathProbe(
                path="/h/.gemini/cli",
                exists=True,
                is_dir=False,
                is_symlink=True,
                link_target="/h/Scratchpad/offload/gemini",
                resolved_target="/h/Scratchpad/offload/gemini",
            ),
            scratchpad=PathProbe(path="/h/Scratchpad/offload/gemini", exists=False, is_dir=False, is_symlink=False),
            mirror=PathProbe(path="/h/mir", exists=False, is_dir=False, is_symlink=False),
        )
        self.assertEqual(classify_entry_state(broken, "/h"), OffloadEntryState.BROKEN_LINK)


class OffloadApplicationTests(unittest.TestCase):
    """Test orchestration, rollback, backup recording, revert, and sync refresh."""

    def _service(
        self, host: _FakeHost, *, unit_err: str | None = None
    ) -> tuple[OffloadService, _FakeUnits, _FakeInstaller]:
        units = _FakeUnits(unit_err=unit_err)
        installer = _FakeInstaller()
        return OffloadService(host=host, units=units, installer=installer), units, installer

    def test_init_plan_apply_backup_refresh_revert_cycle(self) -> None:
        home = "/home/tester"
        host = _FakeHost(home)
        svc, units, installer = self._service(host)

        rc, lines = svc.run(OffloadParsedArgs(action=OffloadAction.INIT))
        self.assertEqual(rc, 0, lines)
        rc_dup, _ = svc.run(OffloadParsedArgs(action=OffloadAction.INIT))
        self.assertEqual(rc_dup, 1)

        conf_path = os.path.join(home, OFFLOAD_CONF_REL)
        host.files[conf_path] = 'version = 1\n[entries.gemini]\nsource = ".gemini/cli"\nmode = "zstd"\nbackup = true\n'
        host.dirs.add(os.path.join(home, ".gemini/cli"))

        rc_plan, plan_lines = svc.run(OffloadParsedArgs(action=OffloadAction.PLAN))
        self.assertEqual(rc_plan, 0, plan_lines)
        self.assertTrue(any("move/copy" in line for line in plan_lines))

        rc_apply, apply_lines = svc.run(OffloadParsedArgs(action=OffloadAction.APPLY))
        self.assertEqual(rc_apply, 0, apply_lines)
        self.assertTrue(units.enabled)
        self.assertEqual(len(installer.installed), 1)

        rc_again, again_lines = svc.run(OffloadParsedArgs(action=OffloadAction.APPLY))
        self.assertEqual(rc_again, 0, again_lines)
        self.assertTrue(any("unchanged:" in line for line in again_lines))

        rc_bak, bak_lines = svc.run(OffloadParsedArgs(action=OffloadAction.BACKUP))
        self.assertEqual(rc_bak, 0, bak_lines)
        self.assertIn(os.path.join(home, OFFLOAD_STATE_REL), host.files)

        rc_ref, _ = svc.run(OffloadParsedArgs(action=OffloadAction.REFRESH))
        self.assertEqual(rc_ref, 0)

        rc_rev, rev_lines = svc.run(OffloadParsedArgs(action=OffloadAction.REVERT))
        self.assertEqual(rc_rev, 0, rev_lines)
        self.assertTrue(units.disabled)
        self.assertEqual(len(installer.forgotten), 1)
        self.assertEqual(len(host.reverted), 1)

    def test_apply_rolls_back_previous_entries_on_second_entry_failure(self) -> None:
        home = "/home/tester"
        host = _FakeHost(home, apply_fail_on=".codex")
        conf_path = os.path.join(home, OFFLOAD_CONF_REL)
        host.files[conf_path] = (
            'version = 1\n[entries.claude]\nsource = ".claude"\n[entries.codex]\nsource = ".codex"\n'
        )
        host.dirs.add(os.path.join(home, ".claude"))
        host.dirs.add(os.path.join(home, ".codex"))
        svc, units, _ = self._service(host)
        rc, lines = svc.run(OffloadParsedArgs(action=OffloadAction.APPLY))
        self.assertEqual(rc, 1)
        self.assertTrue(any("ERROR:" in line for line in lines))
        self.assertEqual(host.rolled_back, [os.path.join(home, ".claude")])
        self.assertFalse(units.enabled)


class OffloadAdapterAndWiringTests(unittest.TestCase):
    """Test real sqlite WAL online backup, nocow seam calls, busy proc detection, and CLI wiring."""

    def test_real_apply_backup_with_wal_sqlite_and_revert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home/tester")
            os.makedirs(os.path.join(home, "Scratchpad"))
            src_dir = os.path.join(home, ".gemini/antigravity-cli")
            os.makedirs(os.path.join(src_dir, "nested"))
            with open(os.path.join(src_dir, "nested/config.json"), "w", encoding="utf-8") as fh:
                fh.write('{"ok": true}\n')

            db_path = os.path.join(src_dir, "conversations.db")
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE msgs (id INTEGER PRIMARY KEY, body TEXT)")
                conn.execute("INSERT INTO msgs (body) VALUES ('hello wal')")
                conn.commit()
                # Open another writer connection holding WAL dirty page until closed after backup
                with contextlib.closing(sqlite3.connect(db_path)) as live_writer:
                    live_writer.execute("INSERT INTO msgs (body) VALUES ('committed wal')")
                    live_writer.commit()

            host = adapters.LocalOffloadHost(
                home=home,
                prog="cst btrfs cache offload",
                runner=SubprocessCommandRunner(),
                proc_root=os.path.join(tmp, "proc"),
            )

            nocow_calls: list[tuple[str, bool]] = []

            def fake_set_nocow(path: str, *, on: bool = True, directory: bool = False) -> bool:
                del on
                nocow_calls.append((path, directory))
                return True

            with (
                mock.patch("btrfs.offload.adapters.set_nocow", side_effect=fake_set_nocow),
                mock.patch("btrfs.offload.adapters.inode_flags", return_value=FS_NOCOW_FL),
            ):
                err = host.apply_offload_tree(
                    source_abs=src_dir,
                    scratchpad_abs=os.path.join(home, "Scratchpad/offload/gemini"),
                    mode=OffloadMode.ZSTD,
                    nocow_paths=("*.db", "nested"),
                )
            self.assertIsNone(err)
            self.assertTrue(os.path.islink(src_dir))
            self.assertTrue(any(p.endswith("conversations.db") for p, _ in nocow_calls))
            self.assertTrue(any(d and p.endswith("nested") for p, d in nocow_calls))

            entry = OffloadEntry(
                name="gemini",
                source_rel=".gemini/antigravity-cli",
                scratchpad_rel="Scratchpad/offload/gemini",
                mirror_rel=".local/share/cst/scratchpad-mirrors/gemini",
                mode=OffloadMode.ZSTD,
                backup=True,
                nocow_paths=("*.db",),
                sqlite_globs=("*.db",),
            )
            sp_abs = os.path.join(home, entry.scratchpad_rel)
            mir_abs = os.path.join(home, entry.mirror_rel)
            db_count, bak_err = host.backup_offload_tree(entry, scratchpad_abs=sp_abs, mirror_abs=mir_abs)
            self.assertIsNone(bak_err)
            self.assertEqual(db_count, 1)
            self.assertTrue(os.path.isfile(os.path.join(mir_abs, "nested/config.json")))
            mir_db = os.path.join(mir_abs, "conversations.db")
            with contextlib.closing(sqlite3.connect(mir_db)) as mir_conn:
                rows = mir_conn.execute("SELECT body FROM msgs ORDER BY id").fetchall()
            self.assertEqual(rows, [("hello wal",), ("committed wal",)])
            self.assertFalse(os.path.exists(f"{mir_db}-wal"))

            rev_err = host.revert_offload_tree(source_abs=src_dir, scratchpad_abs=sp_abs)
            self.assertIsNone(rev_err)
            self.assertFalse(os.path.islink(src_dir))
            self.assertTrue(os.path.isdir(src_dir))
            self.assertFalse(os.path.exists(sp_abs))

    def test_wiring_refuses_container_and_root_without_seam(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc_cont = wiring.run_offload(
            ["init"],
            deps=wiring.OffloadCliDeps(
                env={"HOME": "/home/tester"},
                out=out,
                err=err,
                ctx=_FakeCtx(container=True),
                runner=SubprocessCommandRunner(),
            ),
        )
        self.assertEqual(rc_cont, 1)
        self.assertIn("container", err.getvalue())

        out2, err2 = io.StringIO(), io.StringIO()
        with mock.patch("btrfs.offload.wiring.is_effective_root", return_value=True):
            rc_root = wiring.run_offload(
                ["apply"],
                deps=wiring.OffloadCliDeps(
                    env={"HOME": "/home/tester"},
                    out=out2,
                    err=err2,
                    ctx=_FakeCtx(container=False),
                    runner=SubprocessCommandRunner(),
                ),
            )
        self.assertEqual(rc_root, 1)
        self.assertIn("as root", err2.getvalue())

    def test_handles_fifo_readonly_dir_and_keeps_symlink_when_revert_swap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home/tester")
            os.makedirs(os.path.join(home, "Scratchpad"))
            src_dir = os.path.join(home, ".agent")
            ro_dir = os.path.join(src_dir, "locked")
            os.makedirs(ro_dir)
            with open(os.path.join(ro_dir, "data.txt"), "w", encoding="utf-8") as fh:
                fh.write("read-only dir file\n")
            os.mkfifo(os.path.join(src_dir, "ipc.fifo"))
            os.chmod(ro_dir, 0o500)

            host = adapters.LocalOffloadHost(
                home=home, prog="offload", runner=SubprocessCommandRunner(), proc_root=os.path.join(tmp, "proc")
            )
            sp_dir = os.path.join(home, "Scratchpad/offload/agent")
            err = host.apply_offload_tree(
                source_abs=src_dir,
                scratchpad_abs=sp_dir,
                mode=OffloadMode.NOCOW,
                nocow_paths=("locked",),
            )
            self.assertIsNone(err)
            self.assertTrue(os.path.islink(src_dir))
            self.assertFalse(os.path.exists(os.path.join(sp_dir, "ipc.fifo")))
            self.assertEqual([n for n in os.listdir(home) if ".cst-pre-offload-" in n], [])

            # Revert swap failure must leave source symlink intact
            with mock.patch("btrfs.offload.adapters._stage_copy_and_swap", side_effect=OSError("disk full")):
                rev_err = host.revert_offload_tree(source_abs=src_dir, scratchpad_abs=sp_dir)
            self.assertIn("disk full", rev_err or "")
            self.assertTrue(os.path.islink(src_dir))

            # Cleanly revert afterwards even while `locked` still has 0o500 mode
            self.assertIsNone(host.revert_offload_tree(source_abs=src_dir, scratchpad_abs=sp_dir))
            os.chmod(ro_dir, 0o700)
            self.assertFalse(os.path.islink(src_dir))
            self.assertTrue(os.path.isfile(os.path.join(src_dir, "locked/data.txt")))

    def test_empty_source_with_nonempty_scratchpad_conflicts_and_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home/tester")
            cfg_path = os.path.join(home, ".config/cst/scratchpad-offload.toml")
            src_dir = os.path.join(home, ".agent")
            sp_dir = os.path.join(home, "Scratchpad/offload/agent")
            os.makedirs(src_dir)
            os.makedirs(sp_dir)
            payload = os.path.join(sp_dir, "important.txt")
            with open(payload, "w", encoding="utf-8") as fh:
                fh.write("keep me\n")

            host = adapters.LocalOffloadHost(
                home=home, prog="offload", runner=_FakeRunner(), proc_root=os.path.join(tmp, "proc")
            )
            cfg_text = 'version = 1\n[entries.agent]\nsource = ".agent"\nscratchpad_subpath = "offload/agent"\n'
            host.write_text_atomic(cfg_path, cfg_text)
            cfg = parse_offload_config(cfg_text, cfg_path)
            self.assertEqual(
                classify_entry_state(host.probe_entry(home, cfg.entries[0]), home), OffloadEntryState.CONFLICT
            )

            svc = OffloadService(host=host, units=_FakeUnits(), installer=_FakeInstaller())
            plan_rc, _ = svc.run(parse_offload_args(["plan", "--config", cfg_path]))
            apply_rc, _ = svc.run(parse_offload_args(["apply", "--config", cfg_path]))
            self.assertEqual(plan_rc, 1)
            self.assertEqual(apply_rc, 1)
            self.assertFalse(os.path.islink(src_dir))
            with open(payload, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "keep me\n")

    def test_in_place_nocow_rewrites_nonempty_files_and_nested_subpaths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "sp")
            os.makedirs(os.path.join(root, "sub/deep"))
            f_nested = os.path.join(root, "sub/deep/blob.bin")
            with open(f_nested, "wb") as fh:
                fh.write(b"non-empty-payload")
            set_calls: list[str] = []

            def fake_set(path: str, *, on: bool = True, directory: bool = False) -> bool:
                del on, directory
                set_calls.append(path)
                return True

            with (
                mock.patch("btrfs.offload.adapters.inode_flags", return_value=0),
                mock.patch("btrfs.offload.adapters.set_nocow", side_effect=fake_set),
            ):
                host = adapters.LocalOffloadHost(
                    home=tmp, prog="offload", runner=SubprocessCommandRunner(), proc_root=os.path.join(tmp, "proc")
                )
                err = host.apply_offload_tree(
                    source_abs=os.path.join(tmp, ".pending_src"),
                    scratchpad_abs=root,
                    mode=OffloadMode.ZSTD,
                    nocow_paths=("sub",),
                )
            self.assertIsNone(err)
            self.assertTrue(os.path.islink(os.path.join(tmp, ".pending_src")))
            self.assertTrue(any(p.endswith("sub") for p in set_calls))
            self.assertTrue(any(p.endswith("blob.bin") for p in set_calls))
            with open(f_nested, "rb") as fh:
                self.assertEqual(fh.read(), b"non-empty-payload")

    def test_fake_proc_busy_pid_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = os.path.join(tmp, "work")
            os.makedirs(target_dir)
            proc_root = os.path.join(tmp, "proc/99999")
            os.makedirs(os.path.join(proc_root, "fd"))
            with open(os.path.join(proc_root, "comm"), "w", encoding="utf-8") as fh:
                fh.write("fake-ide\n")
            os.symlink(os.path.join(target_dir, "state.db"), os.path.join(proc_root, "fd/3"))
            runner = _FakeRunner()
            host = adapters.LocalOffloadHost(
                home=tmp, prog="offload", runner=runner, proc_root=os.path.join(tmp, "proc")
            )
            entry = OffloadEntry(
                name="work",
                source_rel="work",
                scratchpad_rel="Scratchpad/offload/work",
                mirror_rel="mirrors/work",
                mode=OffloadMode.ZSTD,
                backup=True,
                nocow_paths=(),
                sqlite_globs=("*.db",),
            )
            probe = host.probe_entry(tmp, entry)
            self.assertEqual(probe.busy, ("fake-ide(99999)",))


if __name__ == "__main__":
    unittest.main()
