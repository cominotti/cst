# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst desktop gnome localsearch`: gsettings values, unit masking, state backup."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Final, TypedDict

from cst_core.domain.shapes import json_object, list_of, parse_json
from cst_core.domain.units import ActiveState, EnablementState, LoadState
from desktop.domain import LocalsearchAction, LocalsearchMode, is_str_val

TRACKER_SCHEMA: Final = "org.freedesktop.Tracker3.Miner.Files"
KEY_RECURSIVE: Final = "index-recursive-directories"
KEY_SINGLE: Final = "index-single-directories"
KEY_MONITORS: Final = "enable-monitors"

LOCALSEARCH_UNITS: Final[tuple[str, ...]] = (
    "localsearch-3.service",
    "localsearch-control-3.service",
    "localsearch-writeback-3.service",
)
RESTRICT_RECURSIVE_DIRS: Final[tuple[str, ...]] = (
    "&DESKTOP",
    "&DOCUMENTS",
    "&DOWNLOAD",
    "&MUSIC",
    "&PICTURES",
    "&VIDEOS",
)
HOME_INDEX_DIRS: Final[frozenset[str]] = frozenset({"$HOME", "&HOME"})
LOCALSEARCH_STATE_FORMAT: Final = 1
_GVARIANT_UNESCAPES: Final = {"n": "\n", "t": "\t", "r": "\r"}


class LocalsearchHealth(StrEnum):
    """Summary assessment shared by `status` and `cst doctor`."""

    UNAVAILABLE = "unavailable"
    HOME_DEFAULT = "home-default"
    RESTRICTED_OK = "restricted-ok"
    DISABLED_OK = "disabled-ok"
    DRIFT = "drift"


class LocalsearchStateRecord(TypedDict):
    """Pre-CST gsettings backup persisted on first `apply` in `~/.config/cst/desktop-gnome-localsearch.json`."""

    format: int
    mode: str
    recursive_dirs: list[str] | None
    single_dirs: list[str] | None
    enable_monitors: bool | None


@dataclass(frozen=True, slots=True)
class LocalsearchUnitProbe:
    """Loaded/masked/active status of one GNOME localsearch user systemd unit."""

    unit: str
    present: bool
    active_state: str
    unit_file_state: str


@dataclass(frozen=True, slots=True)
class LocalsearchSnapshot:
    """Observed state of GNOME localsearch settings and user units."""

    gsettings_available: bool
    schema_installed: bool
    recursive_dirs: tuple[str, ...] | None
    single_dirs: tuple[str, ...] | None
    enable_monitors: bool | None
    units: tuple[LocalsearchUnitProbe, ...]
    saved_state: LocalsearchStateRecord | None
    state_error: str = ""
    systemd_reachable: bool = True


@dataclass(frozen=True, slots=True)
class LocalsearchChangeItem:
    """One planned settings or unit transition for `plan`/`apply`/`revert`."""

    label: str
    current: str
    target: str
    changed: bool


@dataclass(frozen=True, slots=True)
class LocalsearchPlan:
    """Complete plan for applying `restrict`/`disable` or reverting to pre-CST state."""

    action: LocalsearchAction
    mode: LocalsearchMode | None
    items: tuple[LocalsearchChangeItem, ...]
    has_changes: bool
    unmask_units: tuple[str, ...]
    mask_units: tuple[str, ...]
    set_recursive: tuple[str, ...] | None
    set_single: tuple[str, ...] | None
    set_monitors: bool | None
    reset_keys: tuple[str, ...]
    remove_state_file: bool


def _scan_gvariant_quoted(inner: str, start: int) -> tuple[str, int] | None:
    quote = inner[start]
    idx = start + 1
    limit = len(inner)
    chars: list[str] = []
    while idx < limit:
        ch = inner[idx]
        if ch == "\\":
            idx += 1
            if idx >= limit:
                return None
            esc = inner[idx]
            chars.append(_GVARIANT_UNESCAPES.get(esc, esc))
            idx += 1
            continue
        if ch == quote:
            return "".join(chars), idx + 1
        chars.append(ch)
        idx += 1
    return None


def _skip_ws(inner: str, idx: int) -> int:
    while idx < len(inner) and inner[idx].isspace():
        idx += 1
    return idx


def _parse_gvariant_items(inner: str) -> tuple[str, ...] | None:
    out: list[str] = []
    idx = 0
    limit = len(inner)
    while idx < limit:
        idx = _skip_ws(inner, idx)
        if idx >= limit or inner[idx] not in ("'", '"'):
            return None
        parsed = _scan_gvariant_quoted(inner, idx)
        if parsed is None:
            return None
        item, idx = parsed
        out.append(item)
        idx = _skip_ws(inner, idx)
        if idx < limit:
            if inner[idx] != ",":
                return None
            idx = _skip_ws(inner, idx + 1)
            if idx >= limit:
                return None
    return tuple(out)


def parse_gvariant_str_list(raw: str) -> tuple[str, ...] | None:
    """Parse gsettings output such as `['&DESKTOP', '$HOME']` or `@as []` into strings, else None."""
    text = raw.strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    if text == "[]":
        return ()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    inner = text[1:-1].strip()
    return _parse_gvariant_items(inner) if inner else ()


def _quote_gvariant_str(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )
    return f"'{escaped}'"


def format_gvariant_str_list(items: Sequence[str]) -> str:
    """Format a Python string sequence into GVariant `as` literal syntax for `gsettings set`."""
    if not items:
        return "@as []"
    quoted = ", ".join(_quote_gvariant_str(item) for item in items)
    return f"[{quoted}]"


def parse_gvariant_bool(raw: str) -> bool | None:
    """Parse gsettings boolean output (`'true'` / `'false'`)."""
    cleaned = raw.strip()
    if cleaned == "true":
        return True
    if cleaned == "false":
        return False
    return None


def parse_localsearch_state_json(text: str) -> LocalsearchStateRecord:
    """Validate `~/.config/cst/desktop-gnome-localsearch.json` via `cst_core.domain.shapes`."""
    doc = parse_json(text)
    if not json_object(doc):
        raise ValueError("localsearch state is not a JSON object")
    fmt = doc.get("format")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt != LOCALSEARCH_STATE_FORMAT:
        raise ValueError(f"unsupported localsearch state format {fmt!r}")
    mode_raw = doc.get("mode")
    if not isinstance(mode_raw, str) or mode_raw not in (LocalsearchMode.RESTRICT, LocalsearchMode.DISABLE):
        raise ValueError(f"invalid localsearch mode {mode_raw!r}")
    rec_raw = doc.get("recursive_dirs")
    if rec_raw is not None and not list_of(rec_raw, is_str_val):
        raise ValueError("invalid recursive_dirs in localsearch state")
    sgl_raw = doc.get("single_dirs")
    if sgl_raw is not None and not list_of(sgl_raw, is_str_val):
        raise ValueError("invalid single_dirs in localsearch state")
    mon_raw = doc.get("enable_monitors")
    if mon_raw is not None and not isinstance(mon_raw, bool):
        raise ValueError("invalid enable_monitors in localsearch state")
    return LocalsearchStateRecord(
        format=fmt,
        mode=mode_raw,
        recursive_dirs=list(rec_raw) if rec_raw is not None else None,
        single_dirs=list(sgl_raw) if sgl_raw is not None else None,
        enable_monitors=mon_raw,
    )


def render_localsearch_state_json(rec: LocalsearchStateRecord) -> str:
    """Serialize `LocalsearchStateRecord` deterministically."""
    return json.dumps(rec, indent=2) + "\n"


def update_state_record_mode(snap: LocalsearchSnapshot, mode: LocalsearchMode) -> LocalsearchStateRecord:
    """Build updated state record preserving original pre-CST settings across mode switches."""
    if snap.saved_state is not None:
        return LocalsearchStateRecord(
            format=LOCALSEARCH_STATE_FORMAT,
            mode=mode.value,
            recursive_dirs=snap.saved_state["recursive_dirs"],
            single_dirs=snap.saved_state["single_dirs"],
            enable_monitors=snap.saved_state["enable_monitors"],
        )
    return LocalsearchStateRecord(
        format=LOCALSEARCH_STATE_FORMAT,
        mode=mode.value,
        recursive_dirs=list(snap.recursive_dirs) if snap.recursive_dirs is not None else None,
        single_dirs=list(snap.single_dirs) if snap.single_dirs is not None else None,
        enable_monitors=snap.enable_monitors,
    )


def _classify_saved_localsearch(snap: LocalsearchSnapshot, masked: Sequence[str]) -> tuple[LocalsearchHealth, str]:
    assert snap.saved_state is not None
    mode = snap.saved_state["mode"]
    rec = snap.recursive_dirs
    if rec is None:
        return LocalsearchHealth.DRIFT, f"saved mode is {mode}, but gsettings could not be read"
    if mode == LocalsearchMode.RESTRICT:
        ok = rec == RESTRICT_RECURSIVE_DIRS and snap.enable_monitors is True and not masked
        return (
            (LocalsearchHealth.RESTRICTED_OK, "restricted to standard XDG user folders")
            if ok
            else (LocalsearchHealth.DRIFT, "saved mode is restrict, but gsettings or unit masks drifted")
        )
    if not snap.systemd_reachable:
        return LocalsearchHealth.DRIFT, "saved mode is disable, but user systemd manager is unreachable"
    present_units = [u for u in snap.units if u.present]
    units_masked = all(u.unit_file_state == EnablementState.MASKED for u in present_units)
    ok = rec == () and snap.single_dirs == () and snap.enable_monitors is False and units_masked
    return (
        (LocalsearchHealth.DISABLED_OK, "disabled and user units masked")
        if ok
        else (LocalsearchHealth.DRIFT, "saved mode is disable, but gsettings or units drifted")
    )


def classify_localsearch_health(snap: LocalsearchSnapshot) -> tuple[LocalsearchHealth, str]:
    """Classify current GNOME localsearch state for both `status` and `cst doctor`."""
    if not snap.gsettings_available or not snap.schema_installed:
        return LocalsearchHealth.UNAVAILABLE, "GNOME localsearch schema/gsettings not installed"
    masked = [u.unit for u in snap.units if u.unit_file_state == EnablementState.MASKED]
    if snap.saved_state is not None:
        return _classify_saved_localsearch(snap, masked)
    rec = snap.recursive_dirs
    if rec is None:
        return LocalsearchHealth.UNAVAILABLE, "cannot read gsettings values"
    if any(d in HOME_INDEX_DIRS for d in rec):
        return LocalsearchHealth.HOME_DEFAULT, "indexing entire $HOME recursively (high write amplification)"
    if masked:
        return LocalsearchHealth.DRIFT, f"units masked ({', '.join(masked)}) without CST state file"
    return LocalsearchHealth.RESTRICTED_OK, "does not index whole $HOME recursively"


def _fmt_list_val(vals: Sequence[str] | None) -> str:
    return "(unknown)" if vals is None else format_gvariant_str_list(vals)


def _fmt_bool_val(*, val: bool | None) -> str:
    if val is None:
        return "(unknown)"
    return "true" if val else "false"


def _build_restrict_plan(snap: LocalsearchSnapshot) -> LocalsearchPlan:
    masked = tuple(u.unit for u in snap.units if u.unit_file_state == EnablementState.MASKED)
    rec_changed = snap.recursive_dirs != RESTRICT_RECURSIVE_DIRS
    mon_changed = snap.enable_monitors is not True
    items = [
        LocalsearchChangeItem(
            KEY_RECURSIVE,
            _fmt_list_val(snap.recursive_dirs),
            format_gvariant_str_list(RESTRICT_RECURSIVE_DIRS),
            changed=rec_changed,
        ),
        LocalsearchChangeItem(KEY_MONITORS, _fmt_bool_val(val=snap.enable_monitors), "true", changed=mon_changed),
        LocalsearchChangeItem(
            "systemd units",
            f"masked: {', '.join(masked)}" if masked else "unmasked",
            "unmasked",
            changed=bool(masked),
        ),
    ]
    return LocalsearchPlan(
        action=LocalsearchAction.APPLY,
        mode=LocalsearchMode.RESTRICT,
        items=tuple(items),
        has_changes=any(i.changed for i in items),
        unmask_units=masked,
        mask_units=(),
        set_recursive=RESTRICT_RECURSIVE_DIRS if rec_changed else None,
        set_single=None,
        set_monitors=True if mon_changed else None,
        reset_keys=(),
        remove_state_file=False,
    )


def _build_disable_plan(snap: LocalsearchSnapshot) -> LocalsearchPlan:
    present_units = [u for u in snap.units if u.present]
    to_mask = tuple(
        u.unit
        for u in present_units
        if u.unit_file_state != EnablementState.MASKED or u.active_state in (ActiveState.ACTIVE, ActiveState.ACTIVATING)
    )
    rec_changed = snap.recursive_dirs != ()
    sgl_changed = snap.single_dirs != ()
    mon_changed = snap.enable_monitors is not False
    if not snap.systemd_reachable:
        unit_cur = "unreachable (systemctl --user)"
        unit_changed = True
    elif not present_units:
        unit_cur = "no units installed"
        unit_changed = False
    else:
        unit_cur = "active/unmasked" if to_mask else "all masked + stopped"
        unit_changed = bool(to_mask)
    items = [
        LocalsearchChangeItem(KEY_RECURSIVE, _fmt_list_val(snap.recursive_dirs), "@as []", changed=rec_changed),
        LocalsearchChangeItem(KEY_SINGLE, _fmt_list_val(snap.single_dirs), "@as []", changed=sgl_changed),
        LocalsearchChangeItem(KEY_MONITORS, _fmt_bool_val(val=snap.enable_monitors), "false", changed=mon_changed),
        LocalsearchChangeItem(
            "systemd units",
            unit_cur,
            f"mask --now ({len(present_units)} unit(s))",
            changed=unit_changed,
        ),
    ]
    return LocalsearchPlan(
        action=LocalsearchAction.APPLY,
        mode=LocalsearchMode.DISABLE,
        items=tuple(items),
        has_changes=any(i.changed for i in items),
        unmask_units=(),
        mask_units=to_mask,
        set_recursive=() if rec_changed else None,
        set_single=() if sgl_changed else None,
        set_monitors=False if mon_changed else None,
        reset_keys=(),
        remove_state_file=False,
    )


def _plan_list_restore(
    key: str,
    current: tuple[str, ...] | None,
    saved_vals: list[str] | None,
    items: list[LocalsearchChangeItem],
    reset_keys: list[str],
) -> tuple[str, ...] | None:
    if saved_vals is None:
        reset_keys.append(key)
        items.append(LocalsearchChangeItem(key, _fmt_list_val(current), "(schema default)", changed=True))
        return None
    want = tuple(saved_vals)
    changed = current != want
    items.append(LocalsearchChangeItem(key, _fmt_list_val(current), _fmt_list_val(want), changed=changed))
    return want if changed else None


def build_localsearch_plan(
    snap: LocalsearchSnapshot, *, mode: LocalsearchMode = LocalsearchMode.RESTRICT, revert: bool = False
) -> LocalsearchPlan:
    """Compute exact changes for `apply` (`restrict`/`disable`) or `revert`."""
    if not revert:
        return _build_restrict_plan(snap) if mode == LocalsearchMode.RESTRICT else _build_disable_plan(snap)

    unmask = tuple(u.unit for u in snap.units if u.unit_file_state == EnablementState.MASKED)
    saved = snap.saved_state
    items: list[LocalsearchChangeItem] = []
    set_rec: tuple[str, ...] | None = None
    set_sgl: tuple[str, ...] | None = None
    set_mon: bool | None = None
    reset_keys: list[str] = []

    if saved is not None:
        set_rec = _plan_list_restore(KEY_RECURSIVE, snap.recursive_dirs, saved["recursive_dirs"], items, reset_keys)
        set_sgl = _plan_list_restore(KEY_SINGLE, snap.single_dirs, saved["single_dirs"], items, reset_keys)
        if saved["enable_monitors"] is not None:
            want_mon = saved["enable_monitors"]
            chg_m = snap.enable_monitors != want_mon
            set_mon = want_mon if chg_m else None
            items.append(
                LocalsearchChangeItem(
                    KEY_MONITORS, _fmt_bool_val(val=snap.enable_monitors), _fmt_bool_val(val=want_mon), changed=chg_m
                )
            )
        else:
            reset_keys.append(KEY_MONITORS)
            items.append(
                LocalsearchChangeItem(
                    KEY_MONITORS, _fmt_bool_val(val=snap.enable_monitors), "(schema default)", changed=True
                )
            )
    else:
        reset_keys.extend((KEY_RECURSIVE, KEY_SINGLE, KEY_MONITORS))
        items.append(
            LocalsearchChangeItem(KEY_RECURSIVE, _fmt_list_val(snap.recursive_dirs), "(schema default)", changed=True)
        )
        items.append(
            LocalsearchChangeItem(KEY_SINGLE, _fmt_list_val(snap.single_dirs), "(schema default)", changed=True)
        )
        items.append(
            LocalsearchChangeItem(
                KEY_MONITORS, _fmt_bool_val(val=snap.enable_monitors), "(schema default)", changed=True
            )
        )

    items.append(
        LocalsearchChangeItem(
            "systemd units",
            f"masked: {', '.join(unmask)}" if unmask else "unmasked",
            "unmasked",
            changed=bool(unmask),
        )
    )
    remove_state = saved is not None
    return LocalsearchPlan(
        action=LocalsearchAction.REVERT,
        mode=None,
        items=tuple(items),
        has_changes=any(i.changed for i in items) or remove_state,
        unmask_units=unmask,
        mask_units=(),
        set_recursive=set_rec,
        set_single=set_sgl,
        set_monitors=set_mon,
        reset_keys=tuple(reset_keys),
        remove_state_file=remove_state,
    )


def format_localsearch_status(snap: LocalsearchSnapshot) -> list[str]:
    """Render lines for `cst desktop gnome localsearch status`."""
    health, summary = classify_localsearch_health(snap)
    lines = ["GNOME localsearch-3:"]
    if not snap.gsettings_available:
        lines.append("  gsettings            not installed")
        lines.append(f"  summary              {summary}")
        return lines
    if not snap.schema_installed:
        lines.append(f"  schema               {TRACKER_SCHEMA} not installed")
        lines.append(f"  summary              {summary}")
        return lines
    lines.append(f"  recursive dirs       {_fmt_list_val(snap.recursive_dirs)}")
    lines.append(f"  single dirs          {_fmt_list_val(snap.single_dirs)}")
    lines.append(f"  enable monitors      {_fmt_bool_val(val=snap.enable_monitors)}")
    for u in snap.units:
        state_desc = f"{u.active_state}, {u.unit_file_state}" if u.present else "not found"
        lines.append(f"  {u.unit:<29} {state_desc}")
    if snap.saved_state is not None:
        lines.append(f"  cst backup state     mode={snap.saved_state['mode']}")
    elif snap.state_error:
        lines.append(f"  cst backup state     unreadable ({snap.state_error})")
    lines.append(f"  summary              {health.value} ({summary})")
    return lines


def format_localsearch_plan(plan: LocalsearchPlan, prog: str, *, dry_run: bool = True) -> list[str]:
    """Render lines for `plan` / `apply` / `revert`."""
    heading = (
        f"Plan for GNOME localsearch-3 (mode={plan.mode.value}):"
        if plan.mode is not None
        else "Plan for GNOME localsearch-3 revert:"
    )
    lines = [heading]
    for item in plan.items:
        tag = "CHANGE" if item.changed else "ok    "
        lines.append(f"  {item.label:<20} [{tag}] {item.current} -> {item.target}")
    if not plan.has_changes:
        lines.append("Already at target state; nothing to do.")
        return lines
    if dry_run:
        cmd = (
            "revert"
            if plan.action == LocalsearchAction.REVERT
            else f"apply --mode {plan.mode.value if plan.mode is not None else 'restrict'}"
        )
        lines.append(f"Dry run, nothing changed. Run `{prog} gnome localsearch {cmd}` to apply.")
    return lines


def parse_systemd_user_show_map(raw_units: Sequence[tuple[str, Mapping[str, str]]]) -> tuple[LocalsearchUnitProbe, ...]:
    """Build unit probes from `(unit_name, show_props)` tuples."""
    out: list[LocalsearchUnitProbe] = []
    for unit, props in raw_units:
        present = bool(props) and props.get("LoadState", LoadState.NOT_FOUND) != LoadState.NOT_FOUND
        out.append(
            LocalsearchUnitProbe(
                unit=unit,
                present=present,
                active_state=props.get("ActiveState", "unknown"),
                unit_file_state=props.get("UnitFileState", "unknown"),
            )
        )
    return tuple(out)
