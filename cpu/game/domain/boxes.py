# SPDX-License-Identifier: MPL-2.0
"""The watchdog's check of the boxes it handled (Docker and Steam scopes, the boxes next to the game): whether
a recorded setting still holds, as systemd shows it, and the journal's words when the tick sets it again.
No I/O."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from cst_core.domain.text import enum_member
from cst_core.domain.values import ByteSize, CpuSet, format_cpulist, parse_whole_number
from cpu.game.domain import INTENDED_PROP_FIELDS, BoxProp, GameRecordKind, Manager, Record

# The words systemd shows for a CPUWeight of the idle class and a MemoryLow with no limit, as the profile writes them.
_IDLE: Final = "idle"
_INFINITY: Final = "infinity"


def _size_key(text: str) -> int | str | None:
    """A MemoryLow as systemd reads it: its bytes ("16G" and "17179869184" are the same), "infinity", or None."""
    if text == _INFINITY:
        return _INFINITY
    size = ByteSize.parse(text)
    return None if size is None else size.bytes_count


def _cpus_key(text: str) -> frozenset[int] | None:
    """A CPU list as CST writes it (commas) or as systemd shows it (a blank between ranges: "12-15 20-23"), as
    a set of CPUs; None for no CPU at all or a list CpuSet cannot read."""
    spec = ",".join(text.replace(",", " ").split())
    if not spec:
        return None
    try:
        return CpuSet.parse(spec).cpus
    except ValueError:
        return None


def _weight_key(text: str) -> int | str | None:
    """A CPUWeight as systemd reads it: "idle", a whole number, or None."""
    return _IDLE if text == _IDLE else parse_whole_number(text)


def setting_holds(prop: str, value: str, now: str) -> bool | None:
    """Whether the property prop, which systemd shows as now, holds the value the profile sets (prop=value),
    judged as systemd reads both: a MemoryLow by its bytes, an AllowedCPUs as a set of CPUs, a CPUWeight as
    "idle" or a whole number. An empty now holds nothing. None when it cannot judge: another property, or a
    value or a shown form it does not read (a MemoryLow given as a percentage); the tick then leaves it alone,
    so a form it does not know never makes it set a box every 30 s."""
    match enum_member(BoxProp, prop):
        case BoxProp.MEMORY_LOW:
            key: Callable[[str], object] = _size_key
        case BoxProp.ALLOWED_CPUS:
            key = _cpus_key
        case BoxProp.CPU_WEIGHT:
            key = _weight_key
        case _:
            return None
    want = key(value)
    if want is None:
        return None
    if not now:
        return False
    got = key(now)
    return None if got is None else got == want


def _shown_form(prop: str, value: str) -> str:
    """The value the profile sets, as systemd shows it (16G in bytes, a CPU list with blanks), for the journal."""
    match enum_member(BoxProp, prop):
        case BoxProp.MEMORY_LOW:
            size = ByteSize.parse(value)
            return value if size is None else str(size.bytes_count)
        case BoxProp.ALLOWED_CPUS:
            cpus = _cpus_key(value)
            return value if cpus is None else format_cpulist(cpus).replace(",", " ")
        case _:
            return value


@dataclass(frozen=True, slots=True)
class Unset:
    """A recorded setting of a box the tick handles that systemd no longer shows as the profile sets it:
    the property, the value the profile sets, what systemd shows now."""

    prop: str
    value: str
    now: str


def unset_settings(settings: Sequence[str], now: Mapping[str, str]) -> list[Unset]:
    """The settings ("PROP=VALUE") of a box that do not hold (setting_holds False) by what systemd shows (now:
    show_values of the box), in order; one it cannot judge is none."""
    out: list[Unset] = []
    for kv in settings:
        prop, _, value = kv.partition("=")
        shown_now = now.get(prop, "")
        if setting_holds(prop, value, shown_now) is False:
            out.append(Unset(prop, value, shown_now))
    return out


def set_again_text(unit: str, unset: Sequence[Unset], *, ok: bool) -> tuple[int, str]:
    """The journal line (priority, text) for a box the tick set again (ok) or could not (unset_settings)."""
    what = "; ".join(f"{u.prop} is '{u.now}', not '{_shown_form(u.prop, u.value)}'" for u in unset)
    settings = " ".join(f"{u.prop}={u.value}" for u in unset)
    if ok:
        return 5, f"{unit}: {what} (its set failed, or something undid it): set {settings} again"
    return 4, f"{unit}: {what}: could not set {settings} again"


def intended_covers(recs: Sequence[Record], mgr: Manager, unit: str) -> frozenset[str]:
    """The properties of unit under manager mgr that the drift repair compares (repair_drift): the prop
    records of game.intended (intended_records) with a value systemd showed (a record with an empty SHOWN
    is never compared)."""
    return frozenset(
        r[3]
        for r in recs
        if r[0] == GameRecordKind.PROP and len(r) >= INTENDED_PROP_FIELDS and (r[1], r[2]) == (mgr, unit) and r[5]
    )
