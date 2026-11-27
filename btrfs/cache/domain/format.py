# SPDX-License-Identifier: MPL-2.0
"""Usage, status, and dry-run plan formatting for `cst btrfs cache`. Pure, no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from btrfs.cache.domain import (
    CACHE_SEAM_ROOT_ENV,
    BaseMountInfo,
    BindingProbe,
    CacheMountKind,
    CacheMountSnapshot,
    has_mount_option,
    validate_attach_snapshot,
)
from btrfs.cache.domain.migrate import cleanup_hint, format_busy_blocker, needs_migration, validate_migration
from btrfs.compress.domain import FSTAB_PATH
from btrfs.compress.domain.root_option import OPT_FLUSHONCOMMIT


def format_cache_mount_usage(prog: str) -> str:
    """Usage and help text for `cst btrfs cache`."""
    return (
        "Dedicated NVMe cache drive provisioning and multi-user bind mounts (~/.cache and ~/Scratchpad).\n"
        "\n"
        "Commands:\n"
        "  status [--user USER]                 read-only: show base mounts, user bindings, and managed fstab\n"
        "  plan [OPTIONS]                       dry run showing directories, fstab edits, migration, and blockers\n"
        "  attach [OPTIONS]                     (root, host only) create 0700 dirs, CACHEDIR.TAG, mount, then update fstab\n"
        "  detach [OPTIONS]                     (root, host only) remove its fstab lines, then unmount its binds; keeps data\n"
        "  system <status|plan|apply|revert>    (root apply/revert) bind /var/log/below, btrfs-stall-monitor, /var/tmp to NVMe\n"
        "  offload <cmd> [OPTIONS]              (user on host) offload hot user dirs to ~/Scratchpad + daily /home backups\n"
        "  provision plan [--device DEV]        read-only: inspect a partition or saved /etc/cst/btrfs-cache.conf\n"
        "  provision init --device DEV          (root, host only) write /etc/cst/btrfs-cache.conf (does not touch disk)\n"
        "  provision apply [--format] [--yes]   (root, host only) LUKS2 + Btrfs + subvols + crypttab + fstab + mounts\n"
        "  unprovision                          (root, host only) unmount bases, remove base fstab/crypttab, close mapper\n"
        "  help                                 show this help\n"
        "\n"
        "Options:\n"
        "  --user NAME                          target user account (default: $SUDO_USER or current user)\n"
        "  --no-cache                           skip ~/.cache binding (/var/mnt/cache/<user>)\n"
        "  --no-scratchpad                      skip ~/Scratchpad binding (/var/mnt/scratchpad/<user>)\n"
        "  --only ID[,ID...]                    (system plan/apply/revert) select below, stall-monitor, and/or var-tmp\n"
        "  --mount-var-tmp-now                  (system plan/apply) mount /var/tmp live immediately even if non-empty\n"
        "  --allow-nonempty                     (plan/attach, system) allow bind-mounting over non-empty target directory\n"
        "  --migrate                            (plan/attach) copy existing ~/.cache to /var/mnt/cache/<user> first\n"
        "  --allow-busy                         (plan/attach with --migrate) proceed even if processes use ~/.cache\n"
        "  --device PATH                        (provision plan/init) block partition to inspect/record\n"
        "  --mapper NAME                        (provision plan/init) dm-crypt mapper name (default: cache)\n"
        "  --format                             (provision plan/apply) initialize blank partition with LUKS2 + Btrfs\n"
        "  --yes                                (provision apply --format) confirm non-interactive formatting\n"
        "\n"
        f"Usage: {prog} <status|plan|attach|detach|system|provision|unprovision|help> [options]\n"
    )


class _StateProbe(Protocol):
    @property
    def mounted_from_source(self) -> bool: ...
    @property
    def in_fstab(self) -> bool: ...
    @property
    def source_unchecked(self) -> bool: ...
    @property
    def bind_below(self) -> bool: ...
    @property
    def mounted_other(self) -> bool: ...
    @property
    def fstab_unmanaged(self) -> bool: ...
    @property
    def target_is_symlink(self) -> bool: ...
    @property
    def source_is_symlink(self) -> bool: ...


def format_bind_state_label(
    probe: _StateProbe,
    *,
    unchecked_base: str,
    in_fstab_label: str,
    extra: Sequence[tuple[bool, str]] = (),
    fallback: str,
) -> str:
    """Format a shared bind-mount probe status badge."""
    if probe.mounted_from_source:
        return "active (fstab + mounted)" if probe.in_fstab else "mounted (not in fstab)"
    states = (
        (probe.source_unchecked, f"mounted (source not checked: {unchecked_base} not mounted)"),
        (probe.bind_below, "mounted (other source on top of CST's bind!)"),
        (probe.mounted_other, "mounted (other source!)"),
        (probe.fstab_unmanaged, "fstab line CST does not manage (blocked)"),
        (probe.in_fstab, in_fstab_label),
        (probe.target_is_symlink, "symlink (blocked)"),
        (probe.source_is_symlink, "source is a symlink (blocked)"),
        *extra,
    )
    return next((label for on, label in states if on), fallback)


def _binding_state_label(probe: BindingProbe) -> str:
    return format_bind_state_label(
        probe,
        unchecked_base="base",
        in_fstab_label="in fstab (not mounted)",
        extra=(
            (probe.target_not_dir, "target is not a folder (blocked)"),
            (probe.target_nonempty, "unattached (target non-empty)"),
        ),
        fallback="not attached",
    )


def cache_seam_lines(seam_root: str) -> list[str]:
    """The line every command prints first under a test root (CACHE_SEAM_ROOT_ENV); none without one."""
    return [f"test seam ({CACHE_SEAM_ROOT_ENV}): {seam_root}"] if seam_root else []


def format_base_status_line(base: BaseMountInfo) -> str:
    """Format one status line for `/var/mnt/cache` or `/var/mnt/scratchpad`."""
    if base.mounted:
        flush_note = " [WARN: flushoncommit]" if has_mount_option(base.options, OPT_FLUSHONCOMMIT) else ""
        return f"  {base.path:<18} mounted ({base.fstype}, uuid={base.uuid or 'unknown'}){flush_note}"
    if base.in_fstab:
        return f"  {base.path:<18} in fstab (not mounted)"
    return f"  {base.path:<18} not provisioned"


def _base_status_line(base: BaseMountInfo) -> str:
    return format_base_status_line(base)


def _user_status_lines(snap: CacheMountSnapshot) -> list[str]:
    if snap.user is None:
        return [f"  target user        unavailable ({snap.user_error or 'unknown'})"]
    lines = [f"  target user        {snap.user.name} (uid={snap.user.uid}, home={snap.user.home})"]
    for probe in snap.bindings:
        b = probe.binding
        lines.append(f"  {b.kind.value:<18} {b.source} -> {b.target} [{_binding_state_label(probe)}]")
    return lines


def systemd_behind_status_lines(*, fstab_behind: bool | None, detail: str) -> list[str]:
    """Render systemd daemon-reload lag status lines when `/etc/fstab` differs from loaded units."""
    if fstab_behind is True:
        return [f"  systemd            has not loaded {FSTAB_PATH} as it is on disk{detail}"]
    if fstab_behind is None:
        return [f"  systemd            cannot say whether it has loaded {FSTAB_PATH} (systemctl gave no answer)"]
    return []


def format_cache_mount_status(snap: CacheMountSnapshot, *, seam_root: str = "") -> list[str]:
    """Render lines for `cst btrfs cache status`."""
    lines = [*cache_seam_lines(seam_root), "Dedicated cache drive mounts:"]
    if snap.mounts_error is not None:
        return [*lines, f"  cannot list mounts: {snap.mounts_error}"]
    if snap.fstab_error is not None:
        return [*lines, f"  {snap.fstab_error}"]
    lines.extend(_base_status_line(snap.bases[kind]) for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD))
    lines.extend(_user_status_lines(snap))
    other_users = sorted({e.user for e in snap.all_managed if snap.user is None or e.user != snap.user.name})
    if other_users:
        lines.append(f"  other fstab users  {', '.join(other_users)}")
    lines.extend(
        systemd_behind_status_lines(
            fstab_behind=snap.fstab_behind,
            detail=(
                " (an edit, or an attach or detach that stopped before its daemon-reload): "
                "run that attach or detach again, or systemctl daemon-reload"
            ),
        )
    )
    return lines


def _plan_binding_line(probe: BindingProbe, snap: CacheMountSnapshot, *, migrate: bool) -> str:
    b = probe.binding
    actions: list[str] = []
    if not probe.source_exists:
        actions.append(f"mkdir 0700 {b.source}")
    if b.kind == CacheMountKind.CACHE and migrate and needs_migration(probe) and snap.migration is not None:
        actions.append(f"copy {b.target} -> {b.source}, rename {b.target} -> {snap.migration.backup_path}")
    if b.kind == CacheMountKind.SCRATCHPAD:
        actions.append("ensure CACHEDIR.TAG")
    if not probe.in_fstab:
        actions.append("add /etc/fstab entry")
    if not probe.mounted_from_source:
        actions.append("mount --bind")
    summary = ", ".join(actions) if actions else "already attached"
    return f"  {b.kind.value:<10} {b.source} -> {b.target}: {summary}"


def _migration_blockers(snap: CacheMountSnapshot, *, migrate: bool, allow_busy: bool) -> list[str]:
    if not migrate or snap.migration is None:
        return []
    cache_probe = next((p for p in snap.bindings if p.binding.kind == CacheMountKind.CACHE), None)
    if cache_probe is None:
        return []
    return validate_migration(cache_probe, snap.migration, allow_busy=allow_busy)


def _plan_migration_notes(snap: CacheMountSnapshot, *, migrate: bool, allow_busy: bool) -> list[str]:
    if not migrate or snap.migration is None:
        return []
    cache_probe = next((p for p in snap.bindings if p.binding.kind == CacheMountKind.CACHE), None)
    if cache_probe is None or not needs_migration(cache_probe):
        return []
    notes: list[str] = []
    if allow_busy and snap.migration.busy_procs:
        notes.append(
            f"NOTE: --allow-busy set; {format_busy_blocker(snap.migration.busy_procs, cache_probe.binding.target)}"
        )
    notes.append(
        f"After verifying the mount, clean up {snap.migration.backup_path} manually: "
        f"`{cleanup_hint(snap.migration.backup_path, is_subvol=snap.migration.target_is_subvol)}`"
    )
    return notes


@dataclass(frozen=True, slots=True)
class CachePlanOptions:
    """Options controlling dry-run `cst btrfs cache plan` output."""

    allow_nonempty: bool = False
    migrate: bool = False
    allow_busy: bool = False
    seam_root: str = ""


def format_ready_tail(*, done_msg: str, behind_msg: str, fstab_behind: bool | None) -> str:
    """Return final plan line when all targets are already configured/attached."""
    return done_msg if fstab_behind is False else behind_msg


def format_cache_mount_plan(
    snap: CacheMountSnapshot, prog: str, *, options: CachePlanOptions | None = None
) -> list[str]:
    """Render dry-run lines for `cst btrfs cache plan`."""
    opts = CachePlanOptions() if options is None else options
    lines = cache_seam_lines(opts.seam_root)
    if snap.user is None:
        return [*lines, f"BLOCKED: {snap.user_error or 'cannot resolve target user'}"]
    lines.append(f"Target user: {snap.user.name} (uid={snap.user.uid}, home={snap.user.home})")
    lines.extend(_plan_binding_line(probe, snap, migrate=opts.migrate) for probe in snap.bindings)
    mig_blockers = _migration_blockers(snap, migrate=opts.migrate, allow_busy=opts.allow_busy)
    blockers = validate_attach_snapshot(
        snap,
        allow_nonempty=opts.allow_nonempty,
        allow_nonempty_kinds=(CacheMountKind.CACHE,) if opts.migrate else (),
    )
    if blockers or mig_blockers:
        seen: list[str] = []
        for msg in [*blockers, *mig_blockers]:
            if msg not in seen:
                seen.append(msg)
                lines.append(f"BLOCKED: {msg}")
        return lines
    lines.extend(_plan_migration_notes(snap, migrate=opts.migrate, allow_busy=opts.allow_busy))
    why = (
        f"systemd has not loaded {FSTAB_PATH} as it is on disk"
        if snap.fstab_behind is True
        else f"systemctl cannot say whether systemd has loaded {FSTAB_PATH}"
    )
    if all(p.in_fstab and p.mounted_from_source for p in snap.bindings):
        lines.append(
            format_ready_tail(
                done_msg=f"Already attached for {snap.user.name}; nothing to do.",
                behind_msg=f"Already attached for {snap.user.name}; {why}: attach runs systemctl daemon-reload.",
                fstab_behind=snap.fstab_behind,
            )
        )
        return lines
    if snap.fstab_behind is not False:
        lines.append(
            f"{snap.user.name}'s lines are in {FSTAB_PATH}; {why}: attach runs systemctl daemon-reload after the mounts."
        )
    flags: list[str] = [f"--user {snap.user.name}"]
    if opts.allow_nonempty:
        flags.append("--allow-nonempty")
    if opts.migrate:
        flags.append("--migrate")
    if opts.allow_busy:
        flags.append("--allow-busy")
    lines.append(f"Dry run, nothing changed. Run `{prog} attach {' '.join(flags)}` to apply.")
    return lines
