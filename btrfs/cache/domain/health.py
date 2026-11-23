# SPDX-License-Identifier: MPL-2.0
"""`cst doctor`'s check of the cache drive (`cst btrfs cache`): its base mounts, `flushoncommit`, the
`nofail` of its /etc/crypttab line and the managed binds. Pure rules, no I/O."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from btrfs.cache.domain import (
    BASE_FOR_KIND,
    CACHE_BASE,
    FSTYPE_BTRFS,
    OPT_FLUSHONCOMMIT,
    SCRATCHPAD_BASE,
    ManagedFstabEntry,
    has_mount_option,
    parse_all_mounts,
    parse_managed_fstab_entries,
)
from btrfs.compress.domain import MountEntry


_NOFAIL: Final = "nofail"
_DEFAULT_MAPPER: Final = "cache"
_DEFAULT_MAPPER_PREFIXES: Final = ("cache-", "luks-cache")
_CRYPTTAB_MIN_FIELDS: Final = 2
_CRYPTTAB_OPT_IDX: Final = 3


class CacheMountFindingSeverity(StrEnum):
    """Severity levels emitted by `assess_cache_mount_health` for `cst doctor`."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class CacheMountFinding:
    """One diagnostic item produced by `assess_cache_mount_health`."""

    severity: CacheMountFindingSeverity
    message: str
    fix: str = ""


def parse_crypttab_cache_issues(crypttab_text: str | None, backing_sources: Collection[str]) -> list[str]:
    """Return missing-`nofail` warnings for any cache drive dm-crypt mapping in `/etc/crypttab`."""
    if crypttab_text is None or not crypttab_text:
        return []
    mappers = {src.removeprefix("/dev/mapper/") for src in backing_sources if src.startswith("/dev/mapper/")}
    issues: list[str] = []
    for raw in crypttab_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < _CRYPTTAB_MIN_FIELDS:
            continue
        name = fields[0]
        is_cache_drive = name == _DEFAULT_MAPPER or name.startswith(_DEFAULT_MAPPER_PREFIXES) or name in mappers
        if not is_cache_drive:
            continue
        opts = fields[_CRYPTTAB_OPT_IDX] if len(fields) > _CRYPTTAB_OPT_IDX else ""
        if not has_mount_option(opts, _NOFAIL):
            issues.append(f"/etc/crypttab entry {name!r} is missing '{_NOFAIL}' (boot could stall if NVMe is absent)")
    return issues


def _collect_active_bases(
    live_by_target: dict[str, MountEntry],
    fstab_by_target: dict[str, MountEntry],
    managed: Sequence[ManagedFstabEntry],
) -> tuple[str, ...]:
    active: list[str] = []
    for base in (CACHE_BASE, SCRATCHPAD_BASE):
        referenced = any(e.base_match for e in _managed_for_base(managed, base))
        if base in live_by_target or base in fstab_by_target or referenced:
            active.append(base)
    return tuple(active)


@dataclass(frozen=True, slots=True)
class _ManagedRef:
    entry: ManagedFstabEntry
    base_match: bool


def _managed_for_base(managed: Sequence[ManagedFstabEntry], base: str) -> list[_ManagedRef]:
    return [_ManagedRef(entry=m, base_match=(BASE_FOR_KIND[m.kind] == base)) for m in managed]


def _check_base_mount(
    base: str,
    live: MountEntry | None,
    fst: MountEntry | None,
    findings: list[CacheMountFinding],
) -> None:
    if live is None:
        findings.append(CacheMountFinding(CacheMountFindingSeverity.WARN, f"{base} is configured but not mounted"))
    elif live.fstype != FSTYPE_BTRFS:
        findings.append(
            CacheMountFinding(CacheMountFindingSeverity.WARN, f"{base} filesystem is {live.fstype}, expected btrfs")
        )
    has_flush = (live is not None and has_mount_option(live.options, OPT_FLUSHONCOMMIT)) or (
        fst is not None and has_mount_option(fst.options, OPT_FLUSHONCOMMIT)
    )
    if has_flush:
        findings.append(
            CacheMountFinding(
                CacheMountFindingSeverity.WARN,
                f"{base} has 'flushoncommit' (disable it on the dedicated cache drive to reduce NVMe writes)",
            )
        )


def assess_cache_mount_health(
    fstab_text: str | None,
    proc_mounts_text: str | None,
    crypttab_text: str | None,
) -> list[CacheMountFinding]:
    """Evaluate the dedicated NVMe cache drive's mounts and crypttab for `cst doctor`."""
    live_by_target = {m.target: m for m in parse_all_mounts(proc_mounts_text or "")}
    fstab_by_target = {m.target: m for m in parse_all_mounts(fstab_text or "")}
    managed = parse_managed_fstab_entries(fstab_text or "")
    active_bases = _collect_active_bases(live_by_target, fstab_by_target, managed)
    if not active_bases and not managed:
        return []

    findings: list[CacheMountFinding] = []
    backing_sources: list[str] = []
    for base in active_bases:
        live, fst = live_by_target.get(base), fstab_by_target.get(base)
        backing_sources.extend(entry.source for entry in (live, fst) if entry is not None)
        _check_base_mount(base, live, fst, findings)

    findings.extend(
        CacheMountFinding(CacheMountFindingSeverity.WARN, issue)
        for issue in parse_crypttab_cache_issues(crypttab_text, backing_sources)
    )

    findings.extend(
        CacheMountFinding(
            CacheMountFindingSeverity.WARN,
            f"{entry.target} (user={entry.user}) is in /etc/fstab but not mounted",
            fix=f"btrfs cache attach --user {entry.user}",
        )
        for entry in managed
        if entry.target not in live_by_target
    )

    if not findings:
        bases_str = ", ".join(active_bases)
        findings.append(
            CacheMountFinding(
                CacheMountFindingSeverity.OK,
                f"{bases_str} mounted (btrfs, no flushoncommit); {len(managed)} user bind mount(s) active",
            )
        )
    return findings
