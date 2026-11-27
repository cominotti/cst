# SPDX-License-Identifier: MPL-2.0
"""The use cases of `cst btrfs cache`: status, plan, attach, detach, provision, and unprovision.

attach makes the folders (and the scratchpad's tag), optionally migrates existing ~/.cache contents when
--migrate is given, mounts each bind not mounted yet, then writes /etc/fstab and reloads systemd: a failure
takes back the binds this run mounted, any migration rename, and /etc/fstab, so no line is left that would
mount at the next boot. detach drops the user's lines and reloads first, then unmounts only the binds CST made.
Every read and write goes through the ports (ports.py).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from btrfs.cache.application.fstab_ops import (
    FstabFailure,
    commit_fstab,
    format_undo_error,
    probe_base_mount,
    restore_fstab,
)
from btrfs.cache.application.provision import ProvisionEnv, run_provision, run_unprovision
from btrfs.cache.application.system import SystemEnv, run_system_action
from btrfs.cache.domain.system_spec import SystemAction, parse_system_args
from btrfs.cache.domain import (
    BASE_FOR_KIND,
    BaseMountInfo,
    BindingProbe,
    CacheMountAction,
    CacheMountBinding,
    CacheMountKind,
    CacheMountParsedArgs,
    CacheMountSnapshot,
    CacheMountUser,
    LiveMount,
    ManagedFstabEntry,
    build_user_bindings,
    detach_blockers,
    has_managed_line,
    is_bind_from,
    mount_holding,
    mount_stacks,
    mounts_by_target,
    parse_all_mounts,
    parse_managed_fstab_entries,
    plan_attach_fstab,
    plan_detach_fstab,
    selected_kinds,
    snapshot_error,
    unmanaged_fstab_targets,
    validate_attach_snapshot,
)
from btrfs.cache.domain.format import (
    CachePlanOptions,
    cache_seam_lines,
    format_cache_mount_plan,
    format_cache_mount_status,
    format_cache_mount_usage,
)
from btrfs.cache.domain.migrate import (
    MigrationProbe,
    backup_path_for,
    cleanup_hint,
    needs_migration,
    validate_migration,
)
from btrfs.cache.domain.systemd_view import catch_up_words, fstab_lines_unread, systemd_behind_fstab
from btrfs.cache.ports import (
    CacheAccountsPort,
    CacheFoldersPort,
    CacheHostViewPort,
    CacheMigrationPort,
    CacheProvisionPort,
    CacheSystemBindPort,
    CacheSystemdPort,
    FstabPort,
    MountTablePort,
)
from btrfs.compress.domain import FSTAB_PATH, MountEntry
from cst_core.domain.values import ExitAndLines


@dataclass(frozen=True, slots=True)
class CacheMounts:
    """The use cases over the ports; seam_root: the test root (CST_BTRFS_CACHE_ROOT) the messages name, or ""."""

    fstab: FstabPort
    mounts: MountTablePort
    systemd: CacheSystemdPort
    folders: CacheFoldersPort
    host: CacheHostViewPort
    accounts: CacheAccountsPort
    provision: CacheProvisionPort | None = None
    migration: CacheMigrationPort | None = None
    system_binds: CacheSystemBindPort | None = None
    seam_root: str = ""

    def snapshot(self, parsed: CacheMountParsedArgs) -> CacheMountSnapshot:
        """What the machine shows of the selected binds (read only)."""
        return _probe_snapshot(parsed, self)

    def run(self, parsed: CacheMountParsedArgs, prog: str) -> ExitAndLines:
        """The requested subcommand: (exit status, its lines); prog: the command's name in plan's lines."""
        if parsed.action in (CacheMountAction.PROVISION, CacheMountAction.UNPROVISION):
            return _run_provision_action(parsed, self, prog)
        if parsed.action == CacheMountAction.SYSTEM:
            sys_args = parse_system_args(parsed.system_argv)
            if sys_args.error is not None:
                return sys_args.code, [sys_args.error]
            if sys_args.action == SystemAction.HELP:
                return 0, format_cache_mount_usage(prog).splitlines()
            env = SystemEnv(
                binds=self.system_binds,
                fstab=self.fstab,
                mounts=self.mounts,
                systemd=self.systemd,
                seam_root=self.seam_root,
            )
            return run_system_action(sys_args, env, prog)
        snap = self.snapshot(parsed)
        match parsed.action:
            case CacheMountAction.STATUS:
                res = self.status(snap)
            case CacheMountAction.PLAN:
                res = self.plan(snap, parsed, prog)
            case CacheMountAction.ATTACH:
                res = _apply_attach(snap, parsed, self)
            case CacheMountAction.DETACH:
                res = _apply_detach(snap, parsed, self)
            case _:
                res = (0, format_cache_mount_status(snap, seam_root=self.seam_root))
        return res

    def status(self, snap: CacheMountSnapshot) -> ExitAndLines:
        """The status lines; systemd is asked about /etc/fstab only where the cache is set up."""
        if snap.mounts_error is None and snap.fstab_error is None and _cache_set_up(snap):
            targets = {e.target for e in snap.all_managed}
            snap = replace(snap, fstab_behind=fstab_lines_unread(self.systemd.mount_units(), targets))
        return 0, format_cache_mount_status(snap, seam_root=self.seam_root)

    def plan(self, snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, prog: str) -> ExitAndLines:
        """What attach would do, changing nothing."""
        if snap.user is not None:
            cur_fstab, new_fstab = _attach_fstab(snap)
            if new_fstab == cur_fstab:  # attach would only mount and reload
                mounting = {p.binding.target for p in snap.bindings if not p.mounted_from_source}
                snap = replace(snap, fstab_behind=_fstab_behind(snap, self, mounting))
        return 0, format_cache_mount_plan(
            snap,
            prog,
            options=CachePlanOptions(
                allow_nonempty=parsed.allow_nonempty,
                migrate=parsed.migrate,
                allow_busy=parsed.allow_busy,
                seam_root=self.seam_root,
            ),
        )


def _run_provision_action(parsed: CacheMountParsedArgs, m: CacheMounts, prog: str) -> ExitAndLines:
    env = ProvisionEnv(prov=m.provision, fstab=m.fstab, mounts=m.mounts, systemd=m.systemd, seam_root=m.seam_root)
    return run_provision(parsed, env, prog) if parsed.action == CacheMountAction.PROVISION else run_unprovision(env)


@dataclass(frozen=True, slots=True)
class _MountTables:
    live: dict[str, LiveMount]
    stacks: dict[str, tuple[LiveMount, ...]]
    fstab: dict[str, MountEntry]
    managed: tuple[ManagedFstabEntry, ...]
    unmanaged_targets: frozenset[str]


def _probe_base(kind: CacheMountKind, tables: _MountTables) -> BaseMountInfo:
    return probe_base_mount(BASE_FOR_KIND[kind], tables.live, tables.fstab)


def _probe_binding(
    kind: CacheMountKind,
    user: CacheMountUser,
    base_info: BaseMountInfo,
    tables: _MountTables,
    m: CacheMounts,
) -> BindingProbe:
    binding = build_user_bindings(
        user,
        include_cache=(kind == CacheMountKind.CACHE),
        include_scratchpad=(kind == CacheMountKind.SCRATCHPAD),
    )[0]
    stack = tables.stacks.get(binding.target, ())
    live = stack[-1] if stack else None
    paths = m.host.binding_paths(binding)
    is_link = paths.target_is_link
    exists = paths.target_exists
    from_source = live is not None and is_bind_from(live, base_info, user.name)
    # Without its base mounted, no mount at the target can be told to be CST's bind or another one.
    mounted_other = live is not None and base_info.mounted and not from_source
    return BindingProbe(
        binding,
        has_managed_line(tables.managed, binding),
        from_source,
        mounted_other,
        is_link,
        exists,
        paths.target_nonempty,
        paths.source_is_dir,
        fstab_unmanaged=binding.target in tables.unmanaged_targets,
        source_is_symlink=paths.source_is_link,
        target_not_dir=exists and not is_link and not paths.target_is_dir,
        source_unchecked=live is not None and not base_info.mounted,
        bind_below=mounted_other and any(is_bind_from(below, base_info, user.name) for below in stack[:-1]),
    )


def _probe_migration(probes: tuple[BindingProbe, ...], m: CacheMounts) -> MigrationProbe | None:
    if m.migration is None:
        return None
    cache_probe = next((p for p in probes if p.binding.kind == CacheMountKind.CACHE), None)
    if cache_probe is None or not needs_migration(cache_probe):
        return None
    target = cache_probe.binding.target
    backup = backup_path_for(target)
    busy_procs, busy_error = m.migration.busy_processes(target)
    return MigrationProbe(
        backup_path=backup,
        backup_exists=m.migration.backup_exists(backup),
        source_nonempty=m.migration.source_nonempty(cache_probe.binding.source),
        target_is_subvol=m.migration.is_btrfs_subvol(target),
        busy_procs=busy_procs,
        busy_error=busy_error,
    )


def _probe_snapshot(parsed: CacheMountParsedArgs, m: CacheMounts) -> CacheMountSnapshot:
    # Byte-transparent: the text is written back, and the bytes of the lines CST does not manage stay.
    # A file there that cannot be read is no missing file: a fstab built from "" would drop every line.
    fstab_error: str | None = None
    try:
        fstab_text = m.fstab.read()
    except OSError as exc:
        fstab_text, fstab_error = None, f"cannot read {FSTAB_PATH}: {exc.strerror or exc}"
    crypttab_text = m.host.crypttab()
    mounts, mounts_error = m.mounts.read()

    user, user_err = m.accounts.resolve(parsed.user)
    home_mount = mount_holding(mounts, user.home) if user is not None else None
    managed = parse_managed_fstab_entries(fstab_text or "")
    tables = _MountTables(
        live=mounts_by_target(mounts),
        stacks=mount_stacks(mounts),
        fstab={e.target: e for e in parse_all_mounts(fstab_text or "")},
        managed=managed,
        unmanaged_targets=unmanaged_fstab_targets(fstab_text or ""),
    )
    bases = {kind: _probe_base(kind, tables) for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD)}
    kinds = selected_kinds(include_cache=parsed.include_cache, include_scratchpad=parsed.include_scratchpad)
    probes = tuple(_probe_binding(k, user, bases[k], tables, m) for k in kinds) if user is not None else ()
    mig = _probe_migration(probes, m) if parsed.migrate else None
    return CacheMountSnapshot(
        user,
        user_err,
        home_mount.uuid if home_mount is not None else "",
        bases,
        probes,
        managed,
        crypttab_text,
        fstab_text,
        mounts_error,
        home_missing=user is not None and not m.host.home_exists(user.home),
        fstab_error=fstab_error,
        migration=mig,
    )


def _commit_fstab(previous: str | None, new_text: str, m: CacheMounts) -> FstabFailure | None:
    return commit_fstab(previous, new_text, m.fstab, m.systemd)


def _fstab_behind(snap: CacheMountSnapshot, m: CacheMounts, mounting: Collection[str] = ()) -> bool | None:
    """Whether systemd has not loaded /etc/fstab as it is on disk."""
    return systemd_behind_fstab(m.systemd.mount_units(), snap.bindings, mounting)


def _cache_set_up(snap: CacheMountSnapshot) -> bool:
    """Whether status asks systemd about /etc/fstab."""
    return bool(snap.all_managed) or any(b.in_fstab or b.mounted for b in snap.bases.values())


def _catch_up_reload(
    snap: CacheMountSnapshot, m: CacheMounts, mounting: Collection[str] = ()
) -> tuple[str | None, list[str]]:
    """For a run that writes no /etc/fstab: daemon-reload when systemd has not loaded /etc/fstab as it is."""
    behind = _fstab_behind(snap, m, mounting)
    if behind is False:
        return None, []
    return catch_up_words(behind=behind, reload_error=m.systemd.daemon_reload())


def _attach_fstab(snap: CacheMountSnapshot) -> tuple[str, str]:
    """/etc/fstab as it is (empty when there is none) and as attach would write it."""
    cur = snap.fstab_text or ""
    return cur, plan_attach_fstab(cur, [p.binding for p in snap.bindings])


def _restore_fstab(previous: str | None, m: CacheMounts) -> list[str]:
    return restore_fstab(previous, m.fstab, m.systemd)


def _error_after_undo(cause: str, undo_errors: Sequence[str]) -> str:
    return format_undo_error(cause, undo_errors)


def _error_after_catch_up(cause: str, undo_errors: Sequence[str], mounted: Sequence[CacheMountBinding]) -> str:
    """The ERROR line of attach's failed catch-up reload."""
    if undo_errors:
        return _error_after_undo(cause, undo_errors)
    return f"ERROR: {cause}; the mounts this run made were taken back." if mounted else f"ERROR: {cause}"


def _undo_each(
    m: CacheMounts, done: Sequence[CacheMountBinding], *, mounted: bool
) -> list[tuple[CacheMountBinding, str]]:
    """Take back what this run did, last first."""
    step = m.mounts.unbind if mounted else m.mounts.bind
    return [(b, err) for b in reversed(done) if (err := step(b)) is not None]


def _undo(m: CacheMounts, done: Sequence[CacheMountBinding], *, mounted: bool) -> list[str]:
    """_undo_each's failures, the reasons only."""
    return [err for _, err in _undo_each(m, done, mounted=mounted)]


@dataclass(frozen=True, slots=True)
class _PerformedMigration:
    binding: CacheMountBinding
    backup_path: str
    target_is_subvol: bool


def _rollback_migration(mig_done: _PerformedMigration | None, m: CacheMounts) -> list[str]:
    if mig_done is None or m.migration is None:
        return []
    errors: list[str] = []
    b = mig_done.binding
    try:
        m.migration.remove_empty_dir(b.target)
    except OSError as exc:
        errors.append(f"cannot remove empty {b.target}: {exc.strerror or exc}")
    try:
        m.migration.rename(mig_done.backup_path, b.target)
    except OSError as exc:
        errors.append(f"cannot restore {mig_done.backup_path} -> {b.target}: {exc.strerror or exc}")
    m.migration.clear_dir_contents(b.source)
    return errors


def _swap_cache_target(b: CacheMountBinding, backup: str, user: CacheMountUser, m: CacheMounts) -> str | None:
    assert m.migration is not None
    try:
        m.migration.rename(b.target, backup)
    except OSError as exc:
        m.migration.clear_dir_contents(b.source)
        return f"ERROR: cannot move {b.target} to {backup}: {exc.strerror or exc}"
    try:
        m.migration.ensure_empty_dir(b.target, user)
    except OSError as exc:
        undo_errs: list[str] = []
        try:
            m.migration.rename(backup, b.target)
        except OSError as r_exc:
            undo_errs.append(f"cannot restore {backup} -> {b.target}: {r_exc.strerror or r_exc}")
        m.migration.clear_dir_contents(b.source)
        return _error_after_undo(f"cannot recreate empty {b.target}: {exc.strerror or exc}", undo_errs)
    return None


def _perform_migration(
    snap: CacheMountSnapshot, user: CacheMountUser, m: CacheMounts
) -> tuple[_PerformedMigration | None, str | None]:
    if m.migration is None or snap.migration is None:
        return None, None
    cache_probe = next((p for p in snap.bindings if p.binding.kind == CacheMountKind.CACHE), None)
    if cache_probe is None or not needs_migration(cache_probe):
        return None, None
    b = cache_probe.binding
    backup = snap.migration.backup_path
    try:
        m.folders.ensure_private(b.source, user)
    except OSError as exc:
        return None, f"ERROR: cannot prepare migration destination {b.source}: {exc.strerror or exc}"
    copy_err = m.migration.copy_tree(b.target, b.source)
    if copy_err is not None:
        m.migration.clear_dir_contents(b.source)
        return None, f"ERROR: {copy_err}; original {b.target} left untouched."
    swap_err = _swap_cache_target(b, backup, user, m)
    if swap_err is not None:
        return None, swap_err
    return _PerformedMigration(b, backup, snap.migration.target_is_subvol), None


def _prepare_attach_dirs(snap: CacheMountSnapshot, user: CacheMountUser, m: CacheMounts) -> str | None:
    """Make each binding's folders (and the scratchpad's tag); the ERROR line of the first failure."""
    for probe in snap.bindings:
        b = probe.binding
        try:
            m.folders.ensure_private(b.source, user)
            if b.kind == CacheMountKind.SCRATCHPAD:
                m.folders.ensure_tag(b.source, user)
            if not probe.target_exists:
                m.folders.ensure_private(b.target, user)
        except OSError as exc:
            return f"ERROR: cannot prepare {b.source} and {b.target}: {exc.strerror or exc}"
    return None


def _bind_missing(snap: CacheMountSnapshot, m: CacheMounts) -> tuple[list[CacheMountBinding], str | None, list[str]]:
    """mount --bind each binding not mounted yet; returns `(mounted, error_cause, undo_errors)`."""
    mounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if probe.mounted_from_source:
            continue
        err = m.mounts.bind(probe.binding)
        if err is not None:
            return mounted, err, _undo(m, mounted, mounted=True)
        mounted.append(probe.binding)
    return mounted, None, []


def _unbind_cst_mounts(snap: CacheMountSnapshot, m: CacheMounts) -> tuple[list[CacheMountBinding], str | None]:
    """Unmount the binds CST made, until one fails."""
    unmounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if not probe.mounted_from_source:
            continue
        err = m.mounts.unbind(probe.binding)
        if err is not None:
            return unmounted, err
        unmounted.append(probe.binding)
    return unmounted, None


def _validate_attach_request(snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, m: CacheMounts) -> list[str]:
    if parsed.migrate and m.migration is None:
        return ["migration adapter is unavailable"]
    blockers = validate_attach_snapshot(
        snap,
        allow_nonempty=parsed.allow_nonempty,
        allow_nonempty_kinds=(CacheMountKind.CACHE,) if parsed.migrate else (),
    )
    if parsed.migrate and snap.migration is not None:
        cache_probe = next((p for p in snap.bindings if p.binding.kind == CacheMountKind.CACHE), None)
        if cache_probe is not None:
            for msg in validate_migration(cache_probe, snap.migration, allow_busy=parsed.allow_busy):
                if msg not in blockers:
                    blockers.append(msg)
    return blockers


def _rollback_mig_if_unmounted(
    m: CacheMounts, undo_msgs: Sequence[str], mig_done: _PerformedMigration | None
) -> list[str]:
    # Only restore the renamed target folder if its mountpoint was actually unmounted.
    target_still_mounted = mig_done is not None and any(mig_done.binding.target in err for err in undo_msgs)
    return (
        [f"kept {mig_done.binding.target} mounted over {mig_done.backup_path}"]
        if mig_done is not None and target_still_mounted
        else _rollback_migration(mig_done, m)
    )


def _undo_attach_failure(
    m: CacheMounts,
    mounted: Sequence[CacheMountBinding],
    extra_undo: Sequence[str],
    mig_done: _PerformedMigration | None,
) -> list[str]:
    mount_undo = _undo(m, mounted, mounted=True)
    return [*mount_undo, *extra_undo, *_rollback_mig_if_unmounted(m, mount_undo, mig_done)]


def _commit_attach_fstab(
    snap: CacheMountSnapshot,
    m: CacheMounts,
    mounted: Sequence[CacheMountBinding],
    mig_done: _PerformedMigration | None,
) -> tuple[bool, list[str], str | None]:
    cur_fstab, new_fstab = _attach_fstab(snap)
    if new_fstab == cur_fstab:
        now = tuple(replace(p, mounted_from_source=True) if p.binding in mounted else p for p in snap.bindings)
        cause, reloaded = _catch_up_reload(replace(snap, bindings=now), m, {b.target for b in mounted})
        if cause is None:
            return False, reloaded, None
        undo_errs = _undo_attach_failure(m, mounted, (), mig_done)
        msg = (
            _error_after_undo(cause, undo_errs)
            if undo_errs or mig_done is not None
            else _error_after_catch_up(cause, [], mounted)
        )
        return False, [], msg
    failed = _commit_fstab(snap.fstab_text, new_fstab, m)
    if failed is not None:
        return True, [], _error_after_undo(failed.cause, _undo_attach_failure(m, mounted, failed.undo_errors, mig_done))
    return True, [], None


def _apply_attach(snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, m: CacheMounts) -> ExitAndLines:
    """Optionally migrate ~/.cache, make folders, mount --bind, then write /etc/fstab and daemon-reload."""
    blockers = _validate_attach_request(snap, parsed, m)
    if blockers or snap.user is None:
        return 1, [f"ERROR: {msg}" for msg in (blockers or [snap.user_error or "invalid state"])]

    user = snap.user
    lines = cache_seam_lines(m.seam_root)
    mig_done, err = _perform_migration(snap, user, m) if parsed.migrate else (None, None)
    if err is not None:
        return 1, [*lines, err]

    prep_err = _prepare_attach_dirs(snap, user, m)
    if prep_err is not None:
        msg = (
            prep_err
            if mig_done is None
            else _error_after_undo(prep_err.removeprefix("ERROR: "), _rollback_migration(mig_done, m))
        )
        return 1, [*lines, msg]

    mounted, mount_err, mount_undo = _bind_missing(snap, m)
    if mount_err is not None:
        return 1, [
            *lines,
            _error_after_undo(mount_err, [*mount_undo, *_rollback_mig_if_unmounted(m, mount_undo, mig_done)]),
        ]

    wrote_fstab, reloaded, fstab_err = _commit_attach_fstab(snap, m, mounted, mig_done)
    if fstab_err is not None:
        return 1, [*lines, fstab_err]

    if mig_done is not None:
        hint = cleanup_hint(mig_done.backup_path, is_subvol=mig_done.target_is_subvol)
        lines.append(
            f"Migrated {mig_done.binding.target} -> {mig_done.binding.source} "
            f"(previous files kept at {mig_done.backup_path}; remove manually after checking: `{hint}`)."
        )
    lines.extend(f"Mounted {b.source} -> {b.target}." for b in mounted)
    lines.extend(reloaded)
    if wrote_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    elif not mounted and not reloaded:
        lines.append(f"Already attached for {user.name}; nothing to do.")
    return 0, lines


def _apply_detach(snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, m: CacheMounts) -> ExitAndLines:
    """Drop the user's managed lines from /etc/fstab and reload systemd first, then unmount only CST binds."""
    err = snapshot_error(snap)
    if err is not None or snap.user is None:
        return 1, [f"ERROR: {err or 'cannot resolve target user'}"]
    user = snap.user
    kinds = selected_kinds(include_cache=parsed.include_cache, include_scratchpad=parsed.include_scratchpad)
    lines = cache_seam_lines(m.seam_root)
    blockers = detach_blockers(snap)
    if blockers:
        return 1, [*lines, *(f"ERROR: {msg}" for msg in blockers)]

    lines.extend(f"kept: {p.binding.target} has a mount CST did not make" for p in snap.bindings if p.mounted_other)

    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_detach_fstab(cur_fstab, user.name, set(kinds))
    reloaded: list[str] = []
    if new_fstab != cur_fstab:
        failed = _commit_fstab(snap.fstab_text, new_fstab, m)
        if failed is not None:
            return 1, [*lines, _error_after_undo(failed.cause, failed.undo_errors)]
    else:
        cause, reloaded = _catch_up_reload(snap, m)
        if cause is not None:
            return 1, [*lines, f"ERROR: {cause}"]
    lines.extend(reloaded)

    unmounted, cause = _unbind_cst_mounts(snap, m)
    if cause is not None:
        not_back = _undo_each(m, unmounted, mounted=False)
        undo_errors = [err for _, err in not_back]
        put_back = (
            snap.fstab_text if not not_back else plan_detach_fstab(cur_fstab, user.name, {b.kind for b, _ in not_back})
        )
        if new_fstab not in (cur_fstab, put_back):
            undo_errors += _restore_fstab(put_back, m)
        return 1, [*lines, _error_after_undo(cause, undo_errors)]
    lines.extend(f"Unmounted {b.target}." for b in unmounted)
    if new_fstab != cur_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    if unmounted or new_fstab != cur_fstab:
        lines.append("Data in /var/mnt/{cache,scratchpad} kept untouched.")
    elif not reloaded:
        lines.append(f"Nothing attached for {user.name}; nothing to do.")
    return 0, lines
