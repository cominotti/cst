# SPDX-License-Identifier: MPL-2.0
"""`cst doctor`'s check of the cache drive (`cst btrfs cache`): its base mounts, `flushoncommit`, the
`nofail` of its /etc/crypttab line and the managed binds. Pure rules, no I/O."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import posixpath
import re
from typing import Final

from btrfs.cache.domain import (
    BASE_FOR_KIND,
    CACHE_BASE,
    FSTYPE_BTRFS,
    OPT_FLUSHONCOMMIT,
    SCRATCHPAD_BASE,
    ManagedFstabEntry,
    has_mount_option,
    mount_device,
    parse_all_mounts,
    parse_managed_fstab_entries,
    source_fsroot,
)
from btrfs.compress.domain import MountEntry
from cst_core.domain.text import split_ascii_blanks
from cst_core.domain.units import SYSTEMD_WHITESPACE, systemd_lines


_NOFAIL: Final = "nofail"
_DEFAULT_MAPPER: Final = "cache"
_DEFAULT_MAPPER_PREFIXES: Final = ("cache-", "luks-cache")
_CRYPTTAB_MIN_FIELDS: Final = 2
_CRYPTTAB_OPT_IDX: Final = 3
_DM_DEVICE_RE: Final = re.compile(r"/dev/dm-\d+", re.ASCII)
_MAPPER_PREFIX: Final = "/dev/mapper/"


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


def dm_device_names(sources: Collection[str]) -> frozenset[str]:
    """The kernel names (`dm-3`) of the `/dev/dm-N` devices among mount sources, whose mapping
    names a caller reads from `/sys/class/block/dm-N/dm/name`."""
    return frozenset(
        dev.removeprefix("/dev/") for dev in (mount_device(s) for s in sources) if _DM_DEVICE_RE.fullmatch(dev)
    )


def mapper_names(sources: Collection[str], dm_names: Mapping[str, str]) -> frozenset[str]:
    """The dm-crypt mapping names behind mount sources: `/dev/mapper/NAME` gives NAME, `/dev/dm-N`
    the name dm_names has for `dm-N`; findmnt's `[FSROOT]` suffix is dropped first."""
    names: set[str] = set()
    for src in sources:
        dev = mount_device(src)
        if dev.startswith(_MAPPER_PREFIX):
            names.add(dev.removeprefix(_MAPPER_PREFIX))
        elif _DM_DEVICE_RE.fullmatch(dev) and (name := dm_names.get(dev.removeprefix("/dev/"), "")):
            names.add(name)
    return frozenset(names)


def _crypttab_issue(raw: str, mappers: frozenset[str]) -> str | None:
    """The missing-`nofail` warning of one /etc/crypttab line of the cache drive, else None."""
    # As systemd-cryptsetup-generator (add_crypttab_devices): the line stripped of SYSTEMD_WHITESPACE
    # (read_stripped_line), "" and "#" lines skipped, the fields split by sscanf's %s, at C isspace: the six
    # ASCII blanks.
    stripped = raw.strip(SYSTEMD_WHITESPACE)
    if not stripped or stripped.startswith("#"):
        return None
    fields = split_ascii_blanks(stripped)
    if len(fields) < _CRYPTTAB_MIN_FIELDS:
        return None
    name = fields[0]
    is_cache_drive = name == _DEFAULT_MAPPER or name.startswith(_DEFAULT_MAPPER_PREFIXES) or name in mappers
    if not is_cache_drive:
        return None
    opts = fields[_CRYPTTAB_OPT_IDX] if len(fields) > _CRYPTTAB_OPT_IDX else ""
    if has_mount_option(opts, _NOFAIL):
        return None
    return f"/etc/crypttab entry {name!r} is missing '{_NOFAIL}' (boot could stall if NVMe is absent)"


def parse_crypttab_cache_issues(
    crypttab_text: str | None, backing_sources: Collection[str], dm_names: Mapping[str, str] | None = None
) -> list[str]:
    """Return missing-`nofail` warnings for any cache drive dm-crypt mapping in `/etc/crypttab`: one
    with a default name (`cache`, `cache-*`, `luks-cache*`) or the mapping behind a base's source
    (`/dev/mapper/NAME`, or `/dev/dm-N` through dm_names). Another `luks-*` line is not the cache
    drive's: Fedora names the root disks so, and they have no `nofail` on purpose."""
    if crypttab_text is None or not crypttab_text:
        return []
    mappers = mapper_names(backing_sources, dm_names or {})
    return [issue for raw in systemd_lines(crypttab_text) if (issue := _crypttab_issue(raw, mappers)) is not None]


def _collect_active_bases(
    live_by_target: dict[str, MountEntry],
    fstab_by_target: dict[str, MountEntry],
    managed: Sequence[ManagedFstabEntry],
) -> tuple[str, ...]:
    referenced = {BASE_FOR_KIND[m.kind] for m in managed}
    return tuple(
        base
        for base in (CACHE_BASE, SCRATCHPAD_BASE)
        if base in live_by_target or base in fstab_by_target or base in referenced
    )


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


def _is_cst_bind(live: MountEntry, base_live: MountEntry | None, user: str) -> bool:
    """live, the mount at a managed target, is CST's bind as far as the mount table tells: on the
    base's device and, when findmnt names its root (SOURCE[FSROOT]), at `<base root>/<user>`.
    findmnt adds "[FSROOT]" only when the root is not the filesystem's top, so a base without one
    under a bind with one is at "/", and a bind without one under a base with one is the drive's top
    level (not CST's bind). With neither root named (/proc/mounts alone) it cannot tell.
    With the base not mounted there is nothing to compare with (it has a warning of its own)."""
    if base_live is None:
        return True
    if mount_device(live.source) != mount_device(base_live.source):
        return False
    base_root = source_fsroot(base_live.source)
    root = source_fsroot(live.source)
    if root is None:
        return base_root is None
    return root == posixpath.join("/" if base_root is None else base_root, user)


def assess_cache_mount_health(
    fstab_text: str | None,
    proc_mounts_text: str | None,
    crypttab_text: str | None,
    dm_names: Mapping[str, str] | None = None,
) -> list[CacheMountFinding]:
    """Evaluate the dedicated NVMe cache drive's mounts and crypttab for `cst doctor`. dm_names: the
    mapping name of each `dm-N` among the mount sources (`/sys/class/block/dm-N/dm/name`)."""
    live_all = parse_all_mounts(proc_mounts_text or "")
    live_by_target = {m.target: m for m in live_all}
    fstab_by_target = {m.target: m for m in parse_all_mounts(fstab_text or "")}
    managed = parse_managed_fstab_entries(fstab_text or "")
    active_bases = _collect_active_bases(live_by_target, fstab_by_target, managed)
    if not active_bases and not managed:
        return []

    findings: list[CacheMountFinding] = []
    backing_sources: list[str] = []
    for base in active_bases:
        live, fst = live_by_target.get(base), fstab_by_target.get(base)
        backing_sources.extend(m.source for m in live_all if m.target == base)
        if fst is not None:
            backing_sources.append(fst.source)
        _check_base_mount(base, live, fst, findings)

    findings.extend(
        CacheMountFinding(CacheMountFindingSeverity.WARN, issue)
        for issue in parse_crypttab_cache_issues(crypttab_text, backing_sources, dm_names)
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
    findings.extend(
        CacheMountFinding(
            CacheMountFindingSeverity.WARN,
            f"{entry.target} (user={entry.user}) has a mount that is not CST's bind of {entry.source}",
            fix=f"btrfs cache status --user {entry.user}",
        )
        for entry in managed
        if (live := live_by_target.get(entry.target)) is not None
        and not _is_cst_bind(live, live_by_target.get(BASE_FOR_KIND[entry.kind]), entry.user)
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
