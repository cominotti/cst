# SPDX-License-Identifier: MPL-2.0
"""The use cases of `cst btrfs cache`: status, plan, attach and detach of a user's cache and scratchpad binds.

attach makes the folders (and the scratchpad's tag), mounts each bind not mounted yet, then writes
/etc/fstab and reloads systemd: a failure takes back the binds this run mounted and /etc/fstab, so no
line is left that would mount at the next boot. detach drops the user's lines and reloads first, then
unmounts only the binds CST made; a failed umount mounts again what this run unmounted and puts back
the lines of the binds that are mounted again. A run that writes no /etc/fstab reloads systemd when it
has not loaded /etc/fstab as it is on disk (an earlier run stopped between its write and its reload).
Every read and write goes through the ports (ports.py).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Final

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
    cache_seam_lines,
    detach_blockers,
    format_cache_mount_plan,
    format_cache_mount_status,
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
from btrfs.cache.domain.systemd_view import catch_up_words, fstab_lines_unread, systemd_behind_fstab
from btrfs.cache.ports import (
    CacheAccountsPort,
    CacheFoldersPort,
    CacheHostViewPort,
    CacheSystemdPort,
    FstabPort,
    MountTablePort,
)
from btrfs.compress.domain import FSTAB_PATH, MountEntry
from cst_core.domain.values import ExitAndLines
from cst_core.ports import ReplacedNotSynced

_NOT_MOUNTED: Final = LiveMount(source="", target="", fstype="", options="", uuid="", fsroot="")


@dataclass(frozen=True, slots=True)
class CacheMounts:
    """The use cases over the ports; seam_root: the test root (CST_BTRFS_CACHE_ROOT) the messages name, or ""."""

    fstab: FstabPort
    mounts: MountTablePort
    systemd: CacheSystemdPort
    folders: CacheFoldersPort
    host: CacheHostViewPort
    accounts: CacheAccountsPort
    seam_root: str = ""

    def snapshot(self, parsed: CacheMountParsedArgs) -> CacheMountSnapshot:
        """What the machine shows of the selected binds (read only)."""
        return _probe_snapshot(parsed, self)

    def run(self, parsed: CacheMountParsedArgs, prog: str) -> ExitAndLines:
        """The requested subcommand: (exit status, its lines); prog: the command's name in plan's lines."""
        snap = self.snapshot(parsed)
        match parsed.action:
            case CacheMountAction.STATUS:
                return self.status(snap)
            case CacheMountAction.PLAN:
                return self.plan(snap, parsed, prog)
            case CacheMountAction.ATTACH:
                return _apply_attach(snap, parsed, self)
            case CacheMountAction.DETACH:
                return _apply_detach(snap, parsed, self)
            case _:
                return 0, format_cache_mount_status(snap, seam_root=self.seam_root)

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
        return 0, format_cache_mount_plan(snap, prog, allow_nonempty=parsed.allow_nonempty, seam_root=self.seam_root)


@dataclass(frozen=True, slots=True)
class _MountTables:
    live: dict[str, LiveMount]
    stacks: dict[str, tuple[LiveMount, ...]]
    fstab: dict[str, MountEntry]
    managed: tuple[ManagedFstabEntry, ...]
    unmanaged_targets: frozenset[str]


def _probe_base(kind: CacheMountKind, tables: _MountTables) -> BaseMountInfo:
    path = BASE_FOR_KIND[kind]
    live = tables.live.get(path)
    fst = tables.fstab.get(path)
    seen = live if live is not None else _NOT_MOUNTED
    return BaseMountInfo(
        path=path,
        mounted=live is not None,
        fstype=seen.fstype,
        uuid=seen.uuid,
        options=seen.options,
        in_fstab=fst is not None,
        fstab_options=fst.options if fst is not None else "",
        source=seen.source,
        fsroot=seen.fsroot,
    )


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
    )


@dataclass(frozen=True, slots=True)
class _FstabFailure:
    """Why writing /etc/fstab and reloading systemd failed, and what of /etc/fstab could not be undone."""

    cause: str
    undo_errors: tuple[str, ...] = ()  # /etc/fstab could not be put back: its new lines stay


def _commit_fstab(previous: str | None, new_text: str, m: CacheMounts) -> _FstabFailure | None:
    """Write /etc/fstab and reload systemd; None, or why it failed, after /etc/fstab was put back as
    it was (the previous text, or no file when there was none) or, in undo_errors, why it could not be.
    A first write that failed before its rename leaves the file as it was (write_atomic); one whose
    folder sync failed after it (ReplacedNotSynced) may have left the new text in place: it is read
    back and put back when it is there. systemd is not reloaded then: it still has the old text loaded."""
    try:
        m.fstab.write(new_text)
    except ReplacedNotSynced as exc:
        return _FstabFailure(f"cannot write {FSTAB_PATH}: {exc.strerror or exc}", _undo_replaced(previous, new_text, m))
    except OSError as exc:
        return _FstabFailure(f"cannot write {FSTAB_PATH}: {exc.strerror or exc}")
    why = m.systemd.daemon_reload()
    if why is None:
        return None
    return _FstabFailure(why, _put_fstab_back(previous, m).errors)


def _undo_replaced(previous: str | None, new_text: str, m: CacheMounts) -> tuple[str, ...]:
    """After a write of new_text that raised once its rename was done: /etc/fstab read back, and put back
    as it was when it holds new_text (nothing when it holds something else); what of it failed."""
    try:
        now = m.fstab.read()
    except OSError as exc:
        return (f"cannot read {FSTAB_PATH} back: {exc.strerror or exc}",)
    return _put_fstab_back(previous, m).errors if now == new_text else ()


def _fstab_behind(snap: CacheMountSnapshot, m: CacheMounts, mounting: Collection[str] = ()) -> bool | None:
    """Whether systemd has not loaded /etc/fstab as it is on disk (systemd_behind_fstab over the mount
    units it has loaded, active or not; mounting: the targets attach mounts); None when systemctl gives
    no answer."""
    return systemd_behind_fstab(m.systemd.mount_units(), snap.bindings, mounting)


def _cache_set_up(snap: CacheMountSnapshot) -> bool:
    """Whether status asks systemd about /etc/fstab: a managed line, or a base in /etc/fstab or mounted (an
    attach or detach may have stopped before its reload); not for a machine without the cache, where a hand
    edit of /etc/fstab is no business of the cache's."""
    return bool(snap.all_managed) or any(b.in_fstab or b.mounted for b in snap.bases.values())


def _catch_up_reload(
    snap: CacheMountSnapshot, m: CacheMounts, mounting: Collection[str] = ()
) -> tuple[str | None, list[str]]:
    """For a run that writes no /etc/fstab: daemon-reload when systemd has not loaded /etc/fstab as it is
    on disk (an earlier run stopped between its write and its reload), or when systemctl cannot say:
    (why the reload failed, or None; the lines to add), worded by catch_up_words. A run with nothing
    pending reloads nothing; a failed reload when systemctl cannot say is a warning, and the run goes on
    (a detach still unmounts)."""
    behind = _fstab_behind(snap, m, mounting)
    if behind is False:
        return None, []
    return catch_up_words(behind=behind, reload_error=m.systemd.daemon_reload())


def _attach_fstab(snap: CacheMountSnapshot) -> tuple[str, str]:
    """/etc/fstab as it is (empty when there is none) and as attach would write it."""
    cur = snap.fstab_text or ""
    return cur, plan_attach_fstab(cur, [p.binding for p in snap.bindings])


@dataclass(frozen=True, slots=True)
class _PutBack:
    """What putting /etc/fstab back did: whether the old text (or no file) is in place, and what failed."""

    in_place: bool
    error: str | None = None

    @property
    def errors(self) -> tuple[str, ...]:
        """The error, if any, as the undo errors it adds."""
        return () if self.error is None else (self.error,)


def _put_fstab_back(previous: str | None, m: CacheMounts) -> _PutBack:
    """/etc/fstab as it was (the previous text, or no file when there was none). A write whose folder sync
    failed after its rename (ReplacedNotSynced) put the old text in place, not synced: an error that says
    so."""
    try:
        if previous is None:
            m.fstab.remove()  # already gone: as it was
        else:
            m.fstab.write(previous)
    except ReplacedNotSynced as exc:
        return _PutBack(in_place=True, error=f"{FSTAB_PATH} put back, its folder not synced: {exc.strerror or exc}")
    except OSError as exc:
        return _PutBack(in_place=False, error=f"cannot put {FSTAB_PATH} back: {exc.strerror or exc}")
    return _PutBack(in_place=True)


def _restore_fstab(previous: str | None, m: CacheMounts) -> list[str]:
    """Put /etc/fstab back as it was and reload systemd when the old text is in place; what of it failed."""
    put_back = _put_fstab_back(previous, m)
    why = m.systemd.daemon_reload() if put_back.in_place else None
    return [*put_back.errors] if why is None else [*put_back.errors, why]


def _error_after_undo(cause: str, undo_errors: Sequence[str]) -> str:
    if undo_errors:
        return f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(undo_errors)})"
    return f"ERROR: {cause}; mounts and {FSTAB_PATH} restored."


def _error_after_catch_up(cause: str, undo_errors: Sequence[str], mounted: Sequence[CacheMountBinding]) -> str:
    """The ERROR line of attach's failed catch-up reload, a step that wrote no /etc/fstab: only the mounts
    this run made are taken back."""
    if undo_errors:
        return _error_after_undo(cause, undo_errors)
    return f"ERROR: {cause}; the mounts this run made were taken back." if mounted else f"ERROR: {cause}"


def _undo_each(
    m: CacheMounts, done: Sequence[CacheMountBinding], *, mounted: bool
) -> list[tuple[CacheMountBinding, str]]:
    """Take back what this run did, last first: unmount the binds it mounted (mounted=True) or
    mount again the ones it unmounted; each binding that could not be taken back, with why."""
    step = m.mounts.unbind if mounted else m.mounts.bind
    return [(b, err) for b in reversed(done) if (err := step(b)) is not None]


def _undo(m: CacheMounts, done: Sequence[CacheMountBinding], *, mounted: bool) -> list[str]:
    """_undo_each's failures, the reasons only."""
    return [err for _, err in _undo_each(m, done, mounted=mounted)]


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


def _bind_missing(snap: CacheMountSnapshot, m: CacheMounts) -> tuple[list[CacheMountBinding], str | None]:
    """mount --bind each binding not mounted yet; on a failure, the binds this run mounted are taken
    back and the ERROR line comes with them."""
    mounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if probe.mounted_from_source:
            continue
        err = m.mounts.bind(probe.binding)
        if err is not None:
            return mounted, _error_after_undo(err, _undo(m, mounted, mounted=True))
        mounted.append(probe.binding)
    return mounted, None


def _unbind_cst_mounts(snap: CacheMountSnapshot, m: CacheMounts) -> tuple[list[CacheMountBinding], str | None]:
    """Unmount the binds CST made, until one fails: the binds unmounted, and why the one failed."""
    unmounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if not probe.mounted_from_source:
            continue
        err = m.mounts.unbind(probe.binding)
        if err is not None:
            return unmounted, err
        unmounted.append(probe.binding)
    return unmounted, None


def _commit_fstab_or_undo(
    snap: CacheMountSnapshot, new_fstab: str, m: CacheMounts, mounted: Sequence[CacheMountBinding]
) -> str | None:
    """Write /etc/fstab and reload systemd; on a failure, unmount the binds this run mounted and give
    the ERROR line."""
    failed = _commit_fstab(snap.fstab_text, new_fstab, m)
    if failed is None:
        return None
    undo_errors = [*_undo(m, mounted, mounted=True), *failed.undo_errors]
    return _error_after_undo(failed.cause, undo_errors)


def _apply_attach(snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, m: CacheMounts) -> ExitAndLines:
    """Folders, then mount --bind, then /etc/fstab and daemon-reload: a failure takes back the
    binds this run mounted (and /etc/fstab), so no line is left that would mount at the next boot."""
    blockers = validate_attach_snapshot(snap, allow_nonempty=parsed.allow_nonempty)
    if blockers or snap.user is None:
        return 1, [f"ERROR: {msg}" for msg in (blockers or [snap.user_error or "invalid state"])]

    user = snap.user
    lines = cache_seam_lines(m.seam_root)
    error = _prepare_attach_dirs(snap, user, m)
    if error is not None:
        return 1, [*lines, error]

    cur_fstab, new_fstab = _attach_fstab(snap)
    mounted, error = _bind_missing(snap, m)
    if error is not None:
        return 1, [*lines, error]

    reloaded: list[str] = []
    if new_fstab == cur_fstab:
        # After the mounts: a bind this run mounted whose line is in /etc/fstab shows whether systemd
        # read that line (systemd_behind_fstab). A catch-up reload that fails takes them back.
        now = tuple(replace(p, mounted_from_source=True) if p.binding in mounted else p for p in snap.bindings)
        cause, reloaded = _catch_up_reload(replace(snap, bindings=now), m, {b.target for b in mounted})
        if cause is not None:
            return 1, [*lines, _error_after_catch_up(cause, _undo(m, mounted, mounted=True), mounted)]
    else:
        error = _commit_fstab_or_undo(snap, new_fstab, m, mounted)
        if error is not None:
            return 1, [*lines, error]
    lines.extend(f"Mounted {b.source} -> {b.target}." for b in mounted)
    lines.extend(reloaded)
    if new_fstab != cur_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    if new_fstab == cur_fstab and not mounted and not reloaded:
        lines.append(f"Already attached for {user.name}; nothing to do.")
    return 0, lines


def _apply_detach(snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, m: CacheMounts) -> ExitAndLines:
    """Drop the user's managed lines from /etc/fstab and reload systemd first, then unmount only the
    binds CST made (the mirror of attach), so no line is left for a bind that is not mounted, even
    when the run dies halfway. A failed write or reload puts /etc/fstab back; a failed umount mounts
    again what this run unmounted, then puts the lines back and reloads (only those of the binds
    that are mounted again: a bind that cannot be mounted again keeps no line). A mount CST did not make
    stays (its managed line still goes); a target whose mount cannot be told (base not mounted) or
    that hides CST's bind under another mount makes detach change nothing."""
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
        # Before any umount: a catch-up reload that fails leaves nothing to take back.
        cause, reloaded = _catch_up_reload(snap, m)
        if cause is not None:
            return 1, [*lines, f"ERROR: {cause}"]
    lines.extend(reloaded)

    unmounted, cause = _unbind_cst_mounts(snap, m)
    if cause is not None:
        not_back = _undo_each(m, unmounted, mounted=False)
        undo_errors = [err for _, err in not_back]
        # The lines of the binds that are not mounted again stay out of /etc/fstab.
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
