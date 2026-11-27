# SPDX-License-Identifier: MPL-2.0
"""Application use cases for `cst btrfs cache offload`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import posixpath
from typing import TYPE_CHECKING

from btrfs.offload.domain import (
    OFFLOAD_CONF_REL,
    OFFLOAD_LOCK_REL,
    OFFLOAD_STATE_REL,
    OffloadAction,
    OffloadBackupEntryRecord,
    OffloadBackupStateRecord,
    OffloadConfig,
    OffloadEntry,
    OffloadEntryState,
    OffloadMode,
    OffloadParsedArgs,
    parse_offload_config,
    render_starter_config,
    select_offload_entries,
)
from btrfs.offload.domain.plan import (
    OffloadEntryProbe,
    OffloadSnapshot,
    OffloadUnitState,
    classify_entry_state,
    format_offload_plan,
    format_offload_status,
    parse_backup_record,
    render_backup_record,
    validate_offload_apply,
    validate_offload_revert,
)
from cst_core.domain.values import ExitAndLines

if TYPE_CHECKING:
    from btrfs.offload.ports import OffloadHostPort, OffloadInstallerPort, OffloadUnitsPort


@dataclass(frozen=True, slots=True)
class _PreparedSelection:
    snap: OffloadSnapshot
    selected: tuple[OffloadEntry, ...]
    by_name: dict[str, OffloadEntryProbe]


@dataclass(frozen=True, slots=True)
class OffloadService:
    """Orchestrates `init`, `status`, `plan`, `apply`, `backup`, `revert`, and `refresh`."""

    host: OffloadHostPort
    units: OffloadUnitsPort
    installer: OffloadInstallerPort

    def run(self, parsed: OffloadParsedArgs) -> ExitAndLines:
        """Execute one parsed `cst btrfs cache offload` command."""
        dispatch = {
            OffloadAction.STATUS: lambda: self.cmd_status(parsed.config_path),
            OffloadAction.INIT: lambda: self.cmd_init(parsed.config_path, force=parsed.force),
            OffloadAction.PLAN: lambda: self.cmd_plan(parsed.config_path, parsed.only, allow_busy=parsed.allow_busy),
            OffloadAction.APPLY: lambda: self._run_locked(lambda home: self._apply_locked(home, parsed)),
            OffloadAction.BACKUP: lambda: self._run_locked(lambda home: self._backup_locked(home, parsed)),
            OffloadAction.REVERT: lambda: self._run_locked(lambda home: self._revert_locked(home, parsed)),
            OffloadAction.REFRESH: lambda: self.cmd_refresh(parsed.config_path),
        }
        handler = dispatch.get(parsed.action)
        return handler() if handler is not None else (0, [])

    def _resolve_config_path(self, config_path: str | None) -> str:
        home = self.host.home_dir()
        return posixpath.join(home, OFFLOAD_CONF_REL) if config_path is None else self.host.resolve_path(config_path)

    def collect_snapshot(self, config_path: str | None = None) -> OffloadSnapshot:
        """Probe config, ~/Scratchpad, entry paths/NOCOW/busy PIDs, backup state, and systemd units."""
        home = self.host.home_dir()
        alt_home = self.host.resolve_realpath(home)
        cfg_file = self._resolve_config_path(config_path)
        raw_cfg = self.host.read_text_opt(cfg_file)
        cfg: OffloadConfig | None = None
        cfg_err: str | None = None
        if raw_cfg is not None:
            cfg = parse_offload_config(raw_cfg, cfg_file)
            cfg_err = "; ".join(cfg.problems) if cfg.problems else None
        elif self.host.path_exists(cfg_file):
            cfg_err = f"{cfg_file}: exists but could not be read as regular UTF-8 text"

        scratchpad = self.host.probe_scratchpad(home)
        entries: tuple[OffloadEntryProbe, ...] = (
            tuple(self.host.probe_entry(home, entry) for entry in cfg.entries) if cfg is not None else ()
        )

        state_file = posixpath.join(home, OFFLOAD_STATE_REL)
        raw_state = self.host.read_text_opt(state_file)
        backup_state: OffloadBackupStateRecord | None = None
        state_err: str | None = None
        if raw_state is not None:
            backup_state, state_err = parse_backup_record(raw_state)
        elif self.host.path_exists(state_file):
            state_err = f"{state_file}: unreadable backup state"

        timer_active, timer_enabled, svc_masked, timer_masked = self.units.probe_units(home)
        return OffloadSnapshot(
            home=home,
            alt_home="" if alt_home == home else alt_home,
            config=cfg,
            config_error=cfg_err,
            scratchpad=scratchpad,
            entries=entries,
            backup_state=backup_state,
            backup_state_error=state_err,
            unit_state=OffloadUnitState(
                active=timer_active,
                enabled=timer_enabled,
                service_masked=svc_masked,
                timer_masked=timer_masked,
            ),
        )

    def _prepare_selection(self, config_path: str | None, only: tuple[str, ...]) -> _PreparedSelection | ExitAndLines:
        snap = self.collect_snapshot(config_path)
        if snap.config is None:
            msg = snap.config_error or f"configuration missing; run `{self.host.prog_name()} init`"
            return (1, [f"ERROR: {msg}"])
        selected, unknown = select_offload_entries(snap.config.entries, only)
        if unknown:
            return (1, [f"ERROR: unknown entry name(s): {', '.join(unknown)}"])
        return _PreparedSelection(snap=snap, selected=selected, by_name={p.entry.name: p for p in snap.entries})

    def _run_locked(self, fn: Callable[[str], ExitAndLines]) -> ExitAndLines:
        home = self.host.home_dir()
        lock_path = posixpath.join(home, OFFLOAD_LOCK_REL)
        self.host.ensure_dir(posixpath.dirname(lock_path), mode=0o700)
        fd = self.host.acquire_lock(lock_path)
        if fd is None:
            return 1, [f"ERROR: another `{self.host.prog_name()}` operation holds {lock_path}"]
        try:
            return fn(home)
        finally:
            self.host.release_lock(fd)

    def cmd_status(self, config_path: str | None = None) -> ExitAndLines:
        """Read-only status report."""
        snap = self.collect_snapshot(config_path)
        return 0, format_offload_status(snap, now_ts=self.host.now_epoch(), prog=self.host.prog_name())

    def cmd_init(self, config_path: str | None = None, *, force: bool = False) -> ExitAndLines:
        """Write starter TOML configuration with all presets commented out."""
        cfg_file = self._resolve_config_path(config_path)
        if self.host.path_exists(cfg_file) and not force:
            return 1, [f"ERROR: {cfg_file} already exists (pass --force to overwrite)"]
        self.host.write_text_atomic(cfg_file, render_starter_config(), mode=0o644)
        return 0, [
            f"Wrote starter offload config: {cfg_file} (edit and uncomment entries, then run `plan` and `apply`)"
        ]

    def cmd_plan(
        self, config_path: str | None = None, only: tuple[str, ...] = (), *, allow_busy: bool = False
    ) -> ExitAndLines:
        """Read-only dry run for `apply`."""
        prep = self._prepare_selection(config_path, only)
        if not isinstance(prep, _PreparedSelection):
            return prep
        lines, blockers = format_offload_plan(
            prep.snap, prep.selected, allow_busy=allow_busy, prog=self.host.prog_name()
        )
        return (1 if blockers else 0), lines

    def _entry_already_current(self, probe: OffloadEntryProbe, home: str, *, alt_home: str) -> bool:
        if classify_entry_state(probe, home, alt_home=alt_home) != OffloadEntryState.ACTIVE:
            return False
        if probe.entry.mode == OffloadMode.NOCOW and probe.scratchpad.nocow_enabled is False:
            return False
        return not probe.nocow_paths_missing

    def _apply_one_entry(
        self,
        probe: OffloadEntryProbe,
        home: str,
        *,
        alt_home: str,
        applied: list[tuple[str, str, bool]],
    ) -> tuple[str | None, str]:
        entry = probe.entry
        if self._entry_already_current(probe, home, alt_home=alt_home):
            return None, f"unchanged: {entry.name} (~/{entry.source_rel} -> ~/{entry.scratchpad_rel})"
        state = classify_entry_state(probe, home, alt_home=alt_home)
        err = self.host.apply_offload_tree(
            source_abs=probe.source.path,
            scratchpad_abs=probe.scratchpad.path,
            mode=entry.mode,
            nocow_paths=entry.nocow_paths,
        )
        if err is not None:
            return f"ERROR: {entry.name}: {err}", ""
        if state != OffloadEntryState.ACTIVE:
            applied.append((probe.source.path, probe.scratchpad.path, state == OffloadEntryState.PENDING))
        return (
            None,
            f"offloaded: {entry.name} (~/{entry.source_rel} -> ~/{entry.scratchpad_rel}, mode={entry.mode.value})",
        )

    def _apply_locked(self, home: str, parsed: OffloadParsedArgs) -> ExitAndLines:
        prep = self._prepare_selection(parsed.config_path, parsed.only)
        if not isinstance(prep, _PreparedSelection):
            return prep
        blockers = validate_offload_apply(prep.snap, prep.selected, allow_busy=parsed.allow_busy)
        if blockers:
            return 1, [f"ERROR: {blocker}" for blocker in blockers]

        applied: list[tuple[str, str, bool]] = []
        lines: list[str] = []
        for entry in prep.selected:
            err_msg, ok_line = self._apply_one_entry(
                prep.by_name[entry.name], home, alt_home=prep.snap.alt_home, applied=applied
            )
            if err_msg is not None:
                self._rollback_applied(applied)
                return 1, [err_msg]
            lines.append(ok_line)

        cfg = prep.snap.config
        if cfg is not None and not parsed.no_backup_timer and any(e.backup for e in cfg.entries):
            unit_err = self._ensure_backup_units(home, cfg.backup_on_calendar)
            if unit_err is not None:
                self._rollback_applied(applied)
                self.installer.forget_backup_units(home, self.units.unit_dir(home))
                return 1, [f"ERROR: {unit_err}"]
            lines.append(f"timer: enabled cst-scratchpad-backup.timer ({cfg.backup_on_calendar})")
        return 0, lines or ["No configured entries to offload."]

    def _rollback_applied(self, applied: list[tuple[str, str, bool]]) -> None:
        for src_abs, sp_abs, was_pending in reversed(applied):
            self.host.rollback_offload_tree(source_abs=src_abs, scratchpad_abs=sp_abs, was_pending=was_pending)

    def _ensure_backup_units(self, home: str, on_calendar: str) -> str | None:
        unit_dir = self.units.unit_dir(home)
        changed, inst_err = self.installer.install_backup_units(home, unit_dir, on_calendar)
        if inst_err is not None:
            return inst_err
        if changed and (reload_err := self.units.reload_daemon()) is not None:
            return reload_err
        return self.units.enable_backup_timer()

    def _backup_locked(self, home: str, parsed: OffloadParsedArgs) -> ExitAndLines:
        prep = self._prepare_selection(parsed.config_path, parsed.only)
        if not isinstance(prep, _PreparedSelection):
            return prep
        existing = prep.snap.backup_state["entries"] if prep.snap.backup_state is not None else {}
        entries_state: dict[str, OffloadBackupEntryRecord] = dict(existing)
        lines: list[str] = []
        failed = False
        now_ts = self.host.now_epoch()

        for entry in prep.selected:
            if not entry.backup:
                lines.append(f"skipped: {entry.name} (backup = false)")
                continue
            probe = prep.by_name[entry.name]
            if classify_entry_state(probe, home, alt_home=prep.snap.alt_home) != OffloadEntryState.ACTIVE:
                lines.append(f"skipped: {entry.name} (not currently active)")
                continue
            db_count, err = self.host.backup_offload_tree(
                entry,
                scratchpad_abs=probe.scratchpad.path,
                mirror_abs=probe.mirror.path,
            )
            ok = err is None
            failed = failed or not ok
            lines.append(
                f"backed up: {entry.name} -> ~/{entry.mirror_rel} ({db_count} sqlite db(s))"
                if ok
                else f"ERROR: {entry.name} backup failed: {err}"
            )
            entries_state[entry.name] = {
                "last_backup_epoch": now_ts,
                "sqlite_dbs": db_count,
                "ok": ok,
                "detail": "ok" if err is None else err,
            }

        state_record: OffloadBackupStateRecord = {"version": 1, "entries": entries_state}
        self.host.write_text_atomic(
            posixpath.join(home, OFFLOAD_STATE_REL),
            render_backup_record(state_record),
            mode=0o600,
        )
        return (1 if failed else 0), lines or ["No active backup entries."]

    def _disable_and_forget_timer(self, home: str) -> str | None:
        unit_dir = self.units.unit_dir(home)
        dis_err = self.units.disable_backup_timer()
        if dis_err is not None:
            return dis_err
        forget_err = self.installer.forget_backup_units(home, unit_dir)
        if forget_err is not None:
            return forget_err
        return self.units.reload_daemon()

    def _revert_one_entry(
        self,
        probe: OffloadEntryProbe,
        home: str,
        *,
        alt_home: str,
        keep_mirror: bool,
    ) -> tuple[str | None, str]:
        entry = probe.entry
        state = classify_entry_state(probe, home, alt_home=alt_home)
        if state in (OffloadEntryState.ACTIVE, OffloadEntryState.PENDING):
            err = self.host.revert_offload_tree(source_abs=probe.source.path, scratchpad_abs=probe.scratchpad.path)
            if err is not None:
                return f"ERROR: {entry.name}: {err}", ""
            line = f"reverted: {entry.name} (~/{entry.source_rel} restored onto /home)"
        else:
            line = f"unchanged: {entry.name} (already on /home)"
        if not keep_mirror and probe.mirror.exists and (rm_err := self.host.remove_tree(probe.mirror.path)) is not None:
            return f"ERROR: {entry.name}: could not remove mirror: {rm_err}", ""
        return None, line

    def _prune_reverted_backup_state(
        self, home: str, snap: OffloadSnapshot, selected_names: set[str], *, keep_mirror: bool
    ) -> None:
        if keep_mirror or snap.backup_state is None:
            return
        remaining = {k: v for k, v in snap.backup_state["entries"].items() if k not in selected_names}
        if len(remaining) != len(snap.backup_state["entries"]):
            self.host.write_text_atomic(
                posixpath.join(home, OFFLOAD_STATE_REL),
                render_backup_record({"version": 1, "entries": remaining}),
                mode=0o600,
            )

    def _revert_locked(self, home: str, parsed: OffloadParsedArgs) -> ExitAndLines:
        prep = self._prepare_selection(parsed.config_path, parsed.only)
        if not isinstance(prep, _PreparedSelection):
            return prep
        blockers = validate_offload_revert(prep.snap, prep.selected, allow_busy=parsed.allow_busy)
        if blockers:
            return 1, [f"ERROR: {blocker}" for blocker in blockers]

        selected_names = {entry.name for entry in prep.selected}
        lines: list[str] = []
        for entry in prep.selected:
            err_msg, ok_line = self._revert_one_entry(
                prep.by_name[entry.name],
                home,
                alt_home=prep.snap.alt_home,
                keep_mirror=parsed.keep_mirror,
            )
            if err_msg is not None:
                return 1, [err_msg]
            lines.append(ok_line)

        self._prune_reverted_backup_state(home, prep.snap, selected_names, keep_mirror=parsed.keep_mirror)
        remaining_backups = [
            probe.entry
            for probe in prep.snap.entries
            if probe.entry.name not in selected_names
            and probe.entry.backup
            and classify_entry_state(probe, home, alt_home=prep.snap.alt_home) == OffloadEntryState.ACTIVE
        ]
        if not remaining_backups:
            if (timer_err := self._disable_and_forget_timer(home)) is not None:
                return 1, [f"ERROR: {timer_err}"]
            lines.append("timer: removed cst-scratchpad-backup.{service,timer} and helper")
        return 0, lines

    def cmd_refresh(self, config_path: str | None = None) -> ExitAndLines:
        """Idempotent refresh for `cst sync`: refresh helper and backup units without moving unmigrated data."""
        snap = self.collect_snapshot(config_path)
        home = self.host.home_dir()
        if snap.config is None:
            return 0, ["offload: not configured (nothing to refresh)"]
        if snap.config.problems:
            return 1, [f"ERROR: {p}" for p in snap.config.problems]
        has_active_backup = any(
            probe.entry.backup and classify_entry_state(probe, home, alt_home=snap.alt_home) == OffloadEntryState.ACTIVE
            for probe in snap.entries
        )
        if not has_active_backup:
            return 0, ["offload: no active backup entries to refresh"]
        err = self._ensure_backup_units(home, snap.config.backup_on_calendar)
        if err is not None:
            return 1, [f"ERROR: {err}"]
        return 0, ["refreshed: btrfs-cache-offload helper and cst-scratchpad-backup timer"]
