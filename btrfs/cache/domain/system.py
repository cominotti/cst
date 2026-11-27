# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic, fstab planning, validation, and rendering for `cst btrfs cache system`."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from btrfs.cache.domain import (
    CACHE_BASE,
    CACHE_MOUNT_BIND_OPTS,
    CACHE_MOUNT_MARKER,
    FSTYPE_BTRFS,
    BaseMountInfo,
    CacheMountAction,
    LiveMount,
    bind_matches_fsroot,
    escape_mount_field,
    split_tagged_fstab_line,
    upsert_marked_lines,
)
from btrfs.cache.domain.format import (
    cache_seam_lines,
    format_base_status_line,
    format_bind_state_label,
    format_ready_tail,
    systemd_behind_status_lines,
)
from btrfs.cache.domain.system_spec import SYSTEM_BIND_SPECS, SystemBindId, SystemBindSpec
from btrfs.compress.domain import FSTAB_PATH
from cst_core.domain.text import enum_member, file_lines, unescape_mount_field

_SYSTEM_SCOPE: Final = CacheMountAction.SYSTEM.value


@dataclass(frozen=True, slots=True)
class ManagedSystemEntry:
    """Parsed `/etc/fstab` line managed by `cst btrfs cache system`."""

    bind_id: SystemBindId
    source: str
    target: str
    raw_line: str


@dataclass(frozen=True, slots=True)
class SystemBindProbe:
    """Read-only host probe for one `cst btrfs cache system` bind target."""

    spec: SystemBindSpec
    in_fstab: bool
    mounted_from_source: bool
    mounted_other: bool
    target_is_symlink: bool
    target_exists: bool
    target_is_dir: bool
    target_nonempty: bool
    source_is_symlink: bool
    source_exists: bool
    source_is_dir: bool
    source_nonempty: bool
    fstab_unmanaged: bool = False
    source_unchecked: bool = False
    bind_below: bool = False
    service_active: bool | None = None


@dataclass(frozen=True, slots=True)
class SystemCacheSnapshot:
    """Complete read-only state collected for `cst btrfs cache system`."""

    cache_base: BaseMountInfo
    var_uuid: str
    probes: tuple[SystemBindProbe, ...]
    managed_entries: tuple[ManagedSystemEntry, ...]
    fstab_text: str | None
    mounts_error: str | None = None
    fstab_error: str | None = None
    fstab_behind: bool | None = False


@dataclass(frozen=True, slots=True)
class SystemPlanOptions:
    """Formatting options for `format_system_plan`."""

    allow_nonempty: bool = False
    mount_var_tmp_now: bool = False
    seam_root: str = ""


def is_system_bind_from(live: LiveMount, base: BaseMountInfo, rel_dir: str) -> bool:
    """Return True when `live` is CST's `/var/mnt/cache/system/<rel_dir>` bind mount from `base`."""
    return bind_matches_fsroot(live, base, f"system/{rel_dir}")


def _parse_system_fstab_line(raw: str) -> ManagedSystemEntry | None:
    parsed = split_tagged_fstab_line(raw, CACHE_MOUNT_MARKER)
    if parsed is None:
        return None
    fields, tags = parsed
    if tags.get("scope") != _SYSTEM_SCOPE:
        return None
    bind_id = enum_member(SystemBindId, tags.get("id", ""))
    if bind_id is None:
        return None
    return ManagedSystemEntry(
        bind_id=bind_id,
        source=unescape_mount_field(fields[0]),
        target=unescape_mount_field(fields[1]),
        raw_line=raw.rstrip("\n"),
    )


def parse_system_fstab_entries(fstab_text: str) -> tuple[ManagedSystemEntry, ...]:
    """Return every `btrfs-cache` system bind entry (`scope=system`) in `fstab_text`."""
    return tuple(entry for raw in file_lines(fstab_text) if (entry := _parse_system_fstab_line(raw)) is not None)


def render_system_fstab_line(spec: SystemBindSpec) -> str:
    """Format one managed `/etc/fstab` system bind-mount entry."""
    return (
        f"{escape_mount_field(spec.source)} {escape_mount_field(spec.target)} none "
        f"{CACHE_MOUNT_BIND_OPTS},x-systemd.requires-mounts-for={escape_mount_field(CACHE_BASE)} 0 0 "
        f"{CACHE_MOUNT_MARKER} scope={_SYSTEM_SCOPE} id={spec.bind_id.value}"
    )


def _system_key(raw: str) -> SystemBindId | None:
    entry = _parse_system_fstab_line(raw)
    return entry.bind_id if entry is not None else None


def plan_system_apply_fstab(fstab_text: str, specs: Sequence[SystemBindSpec]) -> str:
    """Upsert `scope=system` bind lines for `specs` while preserving all user and unrelated lines."""
    wanted = {s.bind_id: render_system_fstab_line(s) for s in specs}
    return upsert_marked_lines(fstab_text, wanted, _system_key)


def plan_system_revert_fstab(fstab_text: str, ids: Collection[SystemBindId]) -> str:
    """Remove `scope=system` bind lines matching `ids` while preserving all other lines."""
    out_lines: list[str] = []
    for line in file_lines(fstab_text):
        entry = _parse_system_fstab_line(line)
        if entry is not None and entry.bind_id in ids:
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n" if out_lines else ""


def selected_system_specs(only: Collection[SystemBindId]) -> tuple[SystemBindSpec, ...]:
    """Return specs in canonical order filtered by `only` (or all when `only` is empty)."""
    if not only:
        return SYSTEM_BIND_SPECS
    wanted = set(only)
    return tuple(spec for spec in SYSTEM_BIND_SPECS if spec.bind_id in wanted)


def should_mount_live(probe: SystemBindProbe, *, mount_var_tmp_now: bool) -> bool:
    """Return True when `apply` should bind-mount `probe` immediately rather than staging for reboot."""
    if probe.mounted_from_source:
        return False
    if probe.spec.mount_live_by_default:
        return True
    return mount_var_tmp_now or not probe.target_nonempty


def _system_base_blockers(base: BaseMountInfo, var_uuid: str) -> list[str]:
    if not base.mounted:
        return [f"{CACHE_BASE} is not mounted (provision & mount the cache drive first)"]
    if base.fstype != FSTYPE_BTRFS:
        return [f"{CACHE_BASE} filesystem is {base.fstype or 'unknown'}, expected {FSTYPE_BTRFS}"]
    if not base.uuid or not var_uuid:
        return [f"cannot verify filesystem UUID separation between {CACHE_BASE} and /var"]
    if base.uuid == var_uuid:
        return [f"{CACHE_BASE} shares filesystem UUID {base.uuid} with /var (refusing same-filesystem bind)"]
    return []


def _copy_conflict_blocker(probe: SystemBindProbe, *, allow_nonempty: bool, mount_var_tmp_now: bool) -> str | None:
    spec = probe.spec
    if probe.mounted_from_source:
        return None
    if spec.mount_live_by_default:
        if probe.target_nonempty and probe.source_nonempty and not allow_nonempty:
            return f"{spec.source} and {spec.target} are both non-empty; resolve manually or pass --allow-nonempty"
        return None
    if mount_var_tmp_now and probe.target_nonempty and not allow_nonempty:
        return (
            f"{spec.target} is non-empty; omitting --mount-var-tmp-now stages it cleanly for next boot "
            "(or pass --allow-nonempty)"
        )
    return None


def _probe_apply_blocker(probe: SystemBindProbe, *, allow_nonempty: bool, mount_var_tmp_now: bool) -> str | None:
    spec = probe.spec
    checks = (
        (probe.target_is_symlink, f"{spec.target} is a symlink (refusing to follow or overwrite symlink)"),
        (probe.source_is_symlink, f"{spec.source} is a symlink (refusing to follow it)"),
        (probe.target_exists and not probe.target_is_dir, f"{spec.target} exists and is not a folder"),
        (probe.source_exists and not probe.source_is_dir, f"{spec.source} exists and is not a folder"),
        (probe.fstab_unmanaged, f"{spec.target} has an {FSTAB_PATH} line CST does not manage; remove it first"),
        (
            probe.mounted_other,
            f"{spec.target} already has a mount CST did not make (not {spec.source}); unmount it first",
        ),
    )
    hit = next((msg for on, msg in checks if on), None)
    if hit is not None:
        return hit
    return _copy_conflict_blocker(probe, allow_nonempty=allow_nonempty, mount_var_tmp_now=mount_var_tmp_now)


def validate_system_apply(
    snap: SystemCacheSnapshot, *, allow_nonempty: bool = False, mount_var_tmp_now: bool = False
) -> list[str]:
    """Return blocking error reasons preventing safe `cst btrfs cache system apply`, else empty list."""
    if snap.mounts_error is not None:
        return [f"cannot list mounts: {snap.mounts_error}"]
    if snap.fstab_error is not None:
        return [snap.fstab_error]
    errors = _system_base_blockers(snap.cache_base, snap.var_uuid)
    for probe in snap.probes:
        blocker = _probe_apply_blocker(probe, allow_nonempty=allow_nonempty, mount_var_tmp_now=mount_var_tmp_now)
        if blocker is not None:
            errors.append(blocker)
    return errors


def validate_system_revert(snap: SystemCacheSnapshot) -> list[str]:
    """Return blocking error reasons preventing safe `cst btrfs cache system revert`, else empty list."""
    if snap.mounts_error is not None:
        return [f"cannot list mounts: {snap.mounts_error}"]
    if snap.fstab_error is not None:
        return [snap.fstab_error]
    errors: list[str] = []
    for probe in snap.probes:
        spec = probe.spec
        if probe.source_unchecked:
            errors.append(
                f"{spec.target} has a mount and {CACHE_BASE} is not mounted, so CST cannot tell whether it is its "
                f"bind; mount {CACHE_BASE} and run revert again"
            )
        elif probe.bind_below:
            errors.append(
                f"{spec.target} has a mount CST did not make on top of CST's bind of {spec.source}; "
                "unmount that one first, then run revert again"
            )
    return errors


def _system_state_label(probe: SystemBindProbe) -> str:
    return format_bind_state_label(
        probe,
        unchecked_base="cache base",
        in_fstab_label="in fstab (activates on reboot)",
        extra=(
            (probe.target_exists and not probe.target_is_dir, "target is not a folder (blocked)"),
            (probe.source_exists and not probe.source_is_dir, "source is not a folder (blocked)"),
        ),
        fallback="not configured",
    )


def format_system_status(snap: SystemCacheSnapshot, *, seam_root: str = "") -> list[str]:
    """Render lines for `cst btrfs cache system status`."""
    lines = [*cache_seam_lines(seam_root), "System NVMe cache bind mounts (/var/mnt/cache/system):"]
    if snap.mounts_error is not None:
        return [*lines, f"  cannot list mounts: {snap.mounts_error}"]
    if snap.fstab_error is not None:
        return [*lines, f"  {snap.fstab_error}"]
    lines.append(format_base_status_line(snap.cache_base))
    for probe in snap.probes:
        s = probe.spec
        svc = f", unit={s.service}" if s.service else ""
        lines.append(
            f"  {s.bind_id.value:<18} {s.source} -> {s.target} [{_system_state_label(probe)}] (mode={oct(s.mode)}{svc})"
        )
    lines.extend(systemd_behind_status_lines(fstab_behind=snap.fstab_behind, detail="; run apply or daemon-reload"))
    lines.append("  toolboxes          always stay on RAID1 (never managed by cache system)")
    return lines


def _plan_probe_actions(probe: SystemBindProbe, *, mount_var_tmp_now: bool) -> list[str]:
    s = probe.spec
    actions: list[str] = []
    if not probe.source_exists or not probe.source_is_dir:
        actions.append(f"mkdir {oct(s.mode)} {s.source}")
    will_copy = (
        s.mount_live_by_default
        and not probe.mounted_from_source
        and probe.target_nonempty
        and not probe.source_nonempty
    )
    will_stop = bool(s.service and not probe.mounted_from_source and probe.service_active is True)
    if will_stop:
        actions.append(f"stop {s.service}, copy {s.target} -> {s.source}" if will_copy else f"stop {s.service}")
    elif will_copy:
        actions.append(f"copy {s.target} -> {s.source}")
    if should_mount_live(probe, mount_var_tmp_now=mount_var_tmp_now):
        actions.append("mount --bind")
    elif not probe.mounted_from_source and not s.mount_live_by_default and not probe.in_fstab:
        actions.append("stage in fstab (activates next boot; target non-empty)")
    if not probe.in_fstab:
        actions.append("add /etc/fstab entry")
    if will_stop:
        actions.append(f"restart {s.service}")
    return actions


def format_system_plan(snap: SystemCacheSnapshot, prog: str, *, options: SystemPlanOptions | None = None) -> list[str]:
    """Render dry-run lines for `cst btrfs cache system plan`."""
    opts = SystemPlanOptions() if options is None else options
    lines = [*cache_seam_lines(opts.seam_root), "Plan for system NVMe cache bind mounts:"]
    if snap.mounts_error is not None or snap.fstab_error is not None:
        err = f"cannot list mounts: {snap.mounts_error}" if snap.mounts_error is not None else (snap.fstab_error or "")
        return [*lines, f"BLOCKED: {err}"]
    for probe in snap.probes:
        s = probe.spec
        actions = _plan_probe_actions(probe, mount_var_tmp_now=opts.mount_var_tmp_now)
        summary = ", ".join(actions) if actions else "already active"
        lines.append(f"  {s.bind_id.value:<14} {s.source} -> {s.target}: {summary}")
    blockers = validate_system_apply(snap, allow_nonempty=opts.allow_nonempty, mount_var_tmp_now=opts.mount_var_tmp_now)
    if blockers:
        lines.extend(f"BLOCKED: {msg}" for msg in blockers)
        return lines
    if all(p.in_fstab and not should_mount_live(p, mount_var_tmp_now=opts.mount_var_tmp_now) for p in snap.probes):
        why = (
            f"systemd has not reloaded {FSTAB_PATH}"
            if snap.fstab_behind is True
            else f"systemctl cannot say whether systemd has loaded {FSTAB_PATH}"
        )
        lines.append(
            format_ready_tail(
                done_msg="System cache bind mounts already configured; nothing to do.",
                behind_msg=f"System cache bind mounts configured; {why}, apply runs daemon-reload.",
                fstab_behind=snap.fstab_behind,
            )
        )
        return lines
    flags: list[str] = []
    if len(snap.probes) < len(SYSTEM_BIND_SPECS):
        only_ids = ",".join(p.spec.bind_id.value for p in snap.probes)
        flags.append(f"--only {only_ids}")
    if opts.allow_nonempty:
        flags.append("--allow-nonempty")
    if opts.mount_var_tmp_now:
        flags.append("--mount-var-tmp-now")
    suffix = f" {' '.join(flags)}" if flags else ""
    lines.append(f"Dry run, nothing changed. Run `{prog} system apply{suffix}` to apply.")
    return lines
