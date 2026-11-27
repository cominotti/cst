# SPDX-License-Identifier: MPL-2.0
"""Pure state assessment, backup record codecs, plan validation, and formatters for offload."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import posixpath
from typing import Final

from btrfs.offload.domain import (
    OFFLOAD_STALE_BACKUP_DAYS,
    OffloadBackupEntryRecord,
    OffloadBackupStateRecord,
    OffloadConfig,
    OffloadEntry,
    OffloadEntryState,
    OffloadHealthState,
    OffloadMode,
)
from cst_core.domain.shapes import json_object, parse_json
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.domain.values import DAY

_STATE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class ScratchpadProbe:
    """Observed state of `~/Scratchpad`."""

    path: str
    exists: bool
    is_dir: bool
    is_symlink: bool
    mounted: bool = False
    fstype: str = ""
    mount_source: str = ""


@dataclass(frozen=True, slots=True)
class OffloadUnitState:
    """User systemd timer state for `cst-scratchpad-backup`."""

    active: str = ""
    enabled: str = ""
    service_masked: bool = False
    timer_masked: bool = False
    timer_checked: bool = True


@dataclass(frozen=True, slots=True)
class PathProbe:
    """Filesystem classification of one endpoint path."""

    path: str
    exists: bool
    is_dir: bool
    is_symlink: bool
    link_target: str = ""
    resolved_target: str = ""
    nonempty: bool = False
    nocow_enabled: bool | None = None

    @classmethod
    def link(cls, path: str, *, target: str, resolved: str) -> PathProbe:
        """Construct a `PathProbe` for an observed symbolic link."""
        return cls(path, exists=True, is_dir=False, is_symlink=True, link_target=target, resolved_target=resolved)


@dataclass(frozen=True, slots=True)
class OffloadEntryProbe:
    """Observed on-disk state for one `OffloadEntry`."""

    entry: OffloadEntry
    source: PathProbe
    scratchpad: PathProbe
    mirror: PathProbe
    busy: tuple[str, ...] = ()
    nocow_paths_missing: bool = False


@dataclass(frozen=True, slots=True)
class OffloadSnapshot:
    """Complete read-only state observed before planning, status, or doctor checks."""

    home: str
    config: OffloadConfig | None
    config_error: str | None
    scratchpad: ScratchpadProbe
    entries: tuple[OffloadEntryProbe, ...]
    backup_state: OffloadBackupStateRecord | None
    backup_state_error: str | None
    unit_state: OffloadUnitState = OffloadUnitState()
    alt_home: str = ""


def expected_scratchpad_abs(home: str, entry: OffloadEntry) -> str:
    """Absolute target path inside `~/Scratchpad` for an entry."""
    return posixpath.join(home, entry.scratchpad_rel)


def _link_matches_expected(src: PathProbe, home: str, entry: OffloadEntry, alt_home: str) -> bool:
    candidates = {posixpath.join(h, entry.scratchpad_rel) for h in (home, alt_home) if h}
    return src.resolved_target in candidates or src.link_target in candidates


def classify_entry_state(probe: OffloadEntryProbe, home: str, *, alt_home: str = "") -> OffloadEntryState:
    """Classify the current link/directory state of an offload entry."""
    src = probe.source
    sp = probe.scratchpad
    if src.is_symlink:
        if not _link_matches_expected(src, home, probe.entry, alt_home):
            return OffloadEntryState.WRONG_TARGET
        return OffloadEntryState.ACTIVE if sp.is_dir and not sp.is_symlink else OffloadEntryState.BROKEN_LINK
    if src.exists:
        if not src.is_dir or sp.is_symlink or (sp.exists and not sp.is_dir) or sp.nonempty:
            return OffloadEntryState.CONFLICT
        return OffloadEntryState.UNLINKED
    if sp.is_dir and not sp.is_symlink:
        return OffloadEntryState.PENDING
    return OffloadEntryState.CONFLICT if sp.exists and not sp.is_dir else OffloadEntryState.UNLINKED


def _entry_apply_blockers(probe: OffloadEntryProbe, home: str, *, allow_busy: bool, alt_home: str = "") -> list[str]:
    blockers: list[str] = []
    name = probe.entry.name
    state = classify_entry_state(probe, home, alt_home=alt_home)
    if state == OffloadEntryState.BROKEN_LINK:
        blockers.append(f"{name}: source `{probe.source.path}` is a broken symlink -> `{probe.source.link_target}`")
    elif state == OffloadEntryState.WRONG_TARGET:
        blockers.append(
            f"{name}: source `{probe.source.path}` points to `{probe.source.link_target}` instead of `{probe.scratchpad.path}`"
        )
    elif state == OffloadEntryState.CONFLICT:
        blockers.append(_conflict_message(probe))
    if not allow_busy and probe.busy:
        blockers.append(f"{name}: processes hold open files ({', '.join(probe.busy)}); close them or pass --allow-busy")
    return blockers


def _conflict_message(probe: OffloadEntryProbe) -> str:
    name = probe.entry.name
    if not probe.source.is_dir and probe.source.exists:
        return f"{name}: source `{probe.source.path}` exists and is not a directory"
    if probe.scratchpad.is_symlink or (probe.scratchpad.exists and not probe.scratchpad.is_dir):
        return f"{name}: scratchpad target `{probe.scratchpad.path}` is not a regular directory"
    return f"{name}: both `{probe.source.path}` and non-empty `{probe.scratchpad.path}` exist"


def _entry_revert_blocker(probe: OffloadEntryProbe, home: str, *, allow_busy: bool, alt_home: str = "") -> list[str]:
    blockers: list[str] = []
    state = classify_entry_state(probe, home, alt_home=alt_home)
    if state in (OffloadEntryState.BROKEN_LINK, OffloadEntryState.WRONG_TARGET, OffloadEntryState.CONFLICT):
        blockers.append(f"{probe.entry.name}: cannot safely revert from state `{state.value}`")
    if not allow_busy and probe.busy:
        blockers.append(f"{probe.entry.name}: processes hold open files ({', '.join(probe.busy)}); pass --allow-busy")
    return blockers


def _selected_probes(
    snap: OffloadSnapshot, selected: Sequence[OffloadEntry]
) -> list[tuple[OffloadEntry, OffloadEntryProbe]]:
    by_name = {p.entry.name: p for p in snap.entries}
    return [(entry, probe) for entry in selected if (probe := by_name.get(entry.name)) is not None]


def _validate_offload_base(
    snap: OffloadSnapshot,
    selected: Sequence[OffloadEntry],
    *,
    allow_busy: bool,
    for_apply: bool,
) -> list[str]:
    if snap.config is None:
        fallback = (
            "configuration is missing; run `cst btrfs cache offload init` first"
            if for_apply
            else "configuration is missing"
        )
        return [snap.config_error or fallback]
    blockers: list[str] = list(snap.config.problems)
    if for_apply:
        if not snap.scratchpad.exists or not snap.scratchpad.is_dir or snap.scratchpad.is_symlink:
            blockers.append(
                f"scratchpad `{snap.scratchpad.path}` is not a usable directory (attach cache or create ~/Scratchpad first)"
            )
        if snap.unit_state.service_masked or snap.unit_state.timer_masked:
            blockers.append("cst-scratchpad-backup user unit is masked (`systemctl --user unmask`)")
    for _entry, probe in _selected_probes(snap, selected):
        fn = _entry_apply_blockers if for_apply else _entry_revert_blocker
        blockers.extend(fn(probe, snap.home, allow_busy=allow_busy, alt_home=snap.alt_home))
    return blockers


def validate_offload_apply(snap: OffloadSnapshot, selected: Sequence[OffloadEntry], *, allow_busy: bool) -> list[str]:
    """Return human-readable blockers preventing safe `apply`."""
    return _validate_offload_base(snap, selected, allow_busy=allow_busy, for_apply=True)


def validate_offload_revert(snap: OffloadSnapshot, selected: Sequence[OffloadEntry], *, allow_busy: bool) -> list[str]:
    """Return blockers preventing safe `revert`."""
    return _validate_offload_base(snap, selected, allow_busy=allow_busy, for_apply=False)


def _parse_backup_entry(name: object, item: object) -> tuple[OffloadBackupEntryRecord | None, str | None]:
    if not isinstance(name, str) or not isinstance(item, dict):
        return None, "backup state entry must be an object"
    epoch = item.get("last_backup_epoch")
    sqlite_dbs = item.get("sqlite_dbs")
    ok = item.get("ok")
    detail = item.get("detail")
    if (
        not isinstance(epoch, (int, float))
        or isinstance(epoch, bool)
        or not isinstance(sqlite_dbs, int)
        or isinstance(sqlite_dbs, bool)
        or not isinstance(ok, bool)
        or not isinstance(detail, str)
    ):
        return None, f"backup state entry {name!r} has invalid field types"
    return {
        "last_backup_epoch": float(epoch),
        "sqlite_dbs": sqlite_dbs,
        "ok": ok,
        "detail": detail,
    }, None


def parse_backup_record(raw: str) -> tuple[OffloadBackupStateRecord | None, str | None]:
    """Parse `scratchpad-offload-backups.json` safely."""
    try:
        parsed = parse_json(raw)
    except ValueError as err:
        return None, f"backup state JSON invalid: {err}"
    if not json_object(parsed):
        return None, "backup state must be a JSON object"
    version = parsed.get("version")
    entries_raw = parsed.get("entries")
    if version != _STATE_VERSION or isinstance(version, bool) or not isinstance(entries_raw, dict):
        return None, "backup state has unsupported version or missing `entries` object"
    clean_entries: dict[str, OffloadBackupEntryRecord] = {}
    for name, item in entries_raw.items():
        rec, entry_err = _parse_backup_entry(name, item)
        if entry_err is not None or rec is None:
            return None, entry_err
        clean_entries[str(name)] = rec
    return {"version": _STATE_VERSION, "entries": clean_entries}, None


def render_backup_record(record: OffloadBackupStateRecord) -> str:
    """Serialize `OffloadBackupStateRecord` canonically with trailing newline."""
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def _active_backup_health(
    probe: OffloadEntryProbe,
    backups: dict[str, OffloadBackupEntryRecord],
    now_ts: float,
    prog: str,
) -> tuple[OffloadHealthState, str, str]:
    name = probe.entry.name
    mode_val = probe.entry.mode.value
    if not probe.entry.backup:
        return OffloadHealthState.OK, f"{name}: active ({mode_val}, cold backup disabled)", ""
    rec = backups.get(name)
    if rec is None:
        return (
            OffloadHealthState.INFO,
            f"{name}: active ({mode_val}), no cold backup yet",
            f"{prog} backup --only {name}",
        )
    if not rec["ok"]:
        return (
            OffloadHealthState.WARN,
            f"{name}: last cold backup failed ({rec['detail']})",
            f"{prog} backup --only {name}",
        )
    age_days = max(0.0, (now_ts - rec["last_backup_epoch"]) / DAY)
    if age_days > OFFLOAD_STALE_BACKUP_DAYS:
        return (
            OffloadHealthState.WARN,
            f"{name}: last cold backup is {age_days:.1f} days old",
            f"{prog} backup --only {name}",
        )
    return OffloadHealthState.OK, f"{name}: active ({mode_val}), backed up ({rec['sqlite_dbs']} sqlite db(s))", ""


def _entry_health(
    probe: OffloadEntryProbe,
    snap: OffloadSnapshot,
    backups: dict[str, OffloadBackupEntryRecord],
    now_ts: float,
    prog: str,
) -> tuple[OffloadHealthState, str, str]:
    name = probe.entry.name
    state = classify_entry_state(probe, snap.home, alt_home=snap.alt_home)
    if state in (OffloadEntryState.BROKEN_LINK, OffloadEntryState.WRONG_TARGET, OffloadEntryState.CONFLICT):
        return OffloadHealthState.WARN, f"{name}: {state.value} ({probe.source.path})", f"{prog} status"
    if state == OffloadEntryState.PENDING:
        return (
            OffloadHealthState.WARN,
            f"{name}: scratchpad directory exists without symlink",
            f"{prog} apply --only {name}",
        )
    if state == OffloadEntryState.UNLINKED:
        return OffloadHealthState.INFO, f"{name}: configured but not offloaded yet", f"{prog} apply --only {name}"
    if (probe.entry.mode == OffloadMode.NOCOW and probe.scratchpad.nocow_enabled is False) or probe.nocow_paths_missing:
        return (
            OffloadHealthState.WARN,
            f"{name}: active symlink lacks +C NOCOW attribute",
            f"{prog} apply --only {name}",
        )
    return _active_backup_health(probe, backups, now_ts, prog)


def _config_preflight(snap: OffloadSnapshot, prog: str) -> list[tuple[OffloadHealthState, str, str]] | None:
    cfg = snap.config
    if cfg is None:
        if snap.config_error is None:
            return []
        return [(OffloadHealthState.WARN, snap.config_error, f"{prog} init --force")]
    if cfg.problems:
        return [(OffloadHealthState.WARN, "; ".join(cfg.problems), "")]
    if not cfg.entries:
        return [(OffloadHealthState.INFO, f"configured ({cfg.config_path}) with 0 active entries", "")]
    return None


def _backup_timer_finding(snap: OffloadSnapshot, prog: str) -> tuple[OffloadHealthState, str, str] | None:
    has_active_backup = any(
        classify_entry_state(p, snap.home, alt_home=snap.alt_home) == OffloadEntryState.ACTIVE and p.entry.backup
        for p in snap.entries
    )
    if not has_active_backup or not snap.unit_state.timer_checked:
        return None
    if snap.unit_state.active == ActiveState.ACTIVE or snap.unit_state.enabled in (
        EnablementState.ENABLED,
        EnablementState.ENABLED_RUNTIME,
    ):
        return None
    return (OffloadHealthState.WARN, "cst-scratchpad-backup.timer is not active/enabled", f"{prog} apply")


def assess_offload_health(
    snap: OffloadSnapshot,
    *,
    now_ts: float,
    prog: str = "cst btrfs cache offload",
) -> list[tuple[OffloadHealthState, str, str]]:
    """Shared health evaluation for `status` and `cst doctor`."""
    if (preflight := _config_preflight(snap, prog)) is not None:
        return preflight

    findings: list[tuple[OffloadHealthState, str, str]] = []
    if not snap.scratchpad.is_dir or snap.scratchpad.is_symlink:
        findings.append((OffloadHealthState.WARN, f"{snap.scratchpad.path}: missing or invalid directory", ""))
    if snap.backup_state_error is not None:
        findings.append((OffloadHealthState.WARN, snap.backup_state_error, f"{prog} backup"))

    backups = {} if snap.backup_state is None else snap.backup_state["entries"]
    findings.extend(_entry_health(probe, snap, backups, now_ts, prog) for probe in snap.entries)
    if (timer_finding := _backup_timer_finding(snap, prog)) is not None:
        findings.append(timer_finding)
    return findings


def _nocow_label(probe: OffloadEntryProbe) -> str:
    if probe.scratchpad.nocow_enabled is True:
        return "yes (+C)"
    if probe.scratchpad.nocow_enabled is False:
        return "no"
    return "n/a"


def _scratchpad_desc(sp: ScratchpadProbe) -> str:
    if sp.mounted:
        return f"mounted ({sp.fstype or 'unknown'} from {sp.mount_source or 'unknown'})"
    if sp.is_symlink:
        return "symlink (blocked)"
    return "directory (unmounted local path)" if sp.is_dir else "missing"


def _backup_summary_label(entry: OffloadEntry, rec: OffloadBackupEntryRecord | None) -> str:
    if not entry.backup:
        return "disabled"
    if rec is None:
        return "never"
    status = "ok" if rec["ok"] else "err"
    return f"{status} ({rec['sqlite_dbs']} sqlite)"


def format_offload_status(snap: OffloadSnapshot, *, now_ts: float, prog: str) -> list[str]:
    """Render concise human-readable `status` output."""
    lines = [f"Scratchpad: {snap.scratchpad.path} ({_scratchpad_desc(snap.scratchpad)})"]
    if snap.config is None:
        lines.append(
            f"Config error: {snap.config_error}"
            if snap.config_error is not None
            else f"Config: not initialized (run `{prog} init`)"
        )
        return lines
    lines.append(
        f"Config: {snap.config.config_path} ({len(snap.config.entries)} entries, calendar={snap.config.backup_on_calendar})"
    )
    lines.extend(f"Config problem: {problem}" for problem in snap.config.problems)
    lines.append(
        f"Backup timer: enabled={snap.unit_state.enabled or 'unknown'}, active={snap.unit_state.active or 'unknown'}"
    )
    backups = {} if snap.backup_state is None else snap.backup_state["entries"]
    for probe in snap.entries:
        state = classify_entry_state(probe, snap.home, alt_home=snap.alt_home)
        backup_str = _backup_summary_label(probe.entry, backups.get(probe.entry.name))
        lines.append(
            f"  - {probe.entry.name}: {state.value} | ~/{probe.entry.source_rel} -> ~/{probe.entry.scratchpad_rel} "
            f"| mode={probe.entry.mode.value} | root_nocow={_nocow_label(probe)} | backup={backup_str}"
        )
    for sev, msg, fix in assess_offload_health(snap, now_ts=now_ts, prog=prog):
        if sev != OffloadHealthState.OK:
            suffix = f" [fix: {fix}]" if fix else ""
            lines.append(f"  {sev.value.upper()}: {msg}{suffix}")
    return lines


def format_offload_plan(
    snap: OffloadSnapshot,
    selected: Sequence[OffloadEntry],
    *,
    allow_busy: bool,
    prog: str,
) -> tuple[list[str], list[str]]:
    """Render dry-run plan lines and return `(lines, blockers)`."""
    lines: list[str] = [f"Scratchpad offload plan ({prog}):"]
    blockers = validate_offload_apply(snap, selected, allow_busy=allow_busy)
    by_name = {p.entry.name: p for p in snap.entries}
    for entry in selected:
        probe = by_name.get(entry.name)
        state = (
            OffloadEntryState.UNLINKED
            if probe is None
            else classify_entry_state(probe, snap.home, alt_home=snap.alt_home)
        )
        if state == OffloadEntryState.ACTIVE:
            lines.append(f"  - {entry.name}: already offloaded (`~/{entry.source_rel}` -> `~/{entry.scratchpad_rel}`)")
        elif state == OffloadEntryState.UNLINKED:
            lines.append(
                f"  - {entry.name}: move/copy `~/{entry.source_rel}` -> `~/{entry.scratchpad_rel}` "
                f"(mode={entry.mode.value}, nocow_paths={len(entry.nocow_paths)}) and replace source with symlink"
            )
        elif state == OffloadEntryState.PENDING:
            lines.append(f"  - {entry.name}: link `~/{entry.source_rel}` -> existing `~/{entry.scratchpad_rel}`")
        else:
            lines.append(f"  - {entry.name}: BLOCKED ({state.value})")
    if snap.config is not None and any(entry.backup for entry in selected):
        lines.append(
            f"  - timer: enable user `cst-scratchpad-backup.timer` (OnCalendar={snap.config.backup_on_calendar})"
        )
    if blockers:
        lines.append("Blockers:")
        lines.extend(f"  - {blocker}" for blocker in blockers)
    return lines, blockers
