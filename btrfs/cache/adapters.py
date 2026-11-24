# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs cache` (`btrfs/btrfs-cache`)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
import os
import pwd
import stat
from typing import Final

from btrfs.cache.domain import (
    BASE_FOR_KIND,
    CACHE_SEAM_ROOT_ENV,
    DIR_MODE_PRIVATE,
    FILE_MODE_TAG,
    FINDMNT_COLUMNS,
    MIN_HUMAN_UID,
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
    format_cache_mount_plan,
    format_cache_mount_status,
    has_managed_line,
    is_bind_from,
    detach_blockers,
    mount_holding,
    mount_stacks,
    mounts_by_target,
    parse_all_mounts,
    parse_findmnt_mounts,
    parse_managed_fstab_entries,
    plan_attach_fstab,
    plan_detach_fstab,
    selected_kinds,
    snapshot_error,
    unmanaged_fstab_targets,
    validate_attach_snapshot,
)
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS, fstab_lines_unread, systemd_behind_fstab
from btrfs.compress.domain import FSTAB_PATH, MountEntry
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import LocalFileSystemAdapter, ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, host_view_path
from cst_core.domain.values import ROOT_ACCOUNT
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, OutputMode

_CRYPTTAB_PATH: Final = "/etc/crypttab"
_ACCOUNT_DB_PATH: Final = "/etc/passwd"
_FINDMNT_TIMEOUT_S: Final = 15
_ACCOUNT_MIN_FIELDS: Final = 6
_FSTAB_MODE: Final = 0o644
_MOUNT_TIMEOUT_S: Final = 30
_NOT_MOUNTED: Final = LiveMount(source="", target="", fstype="", options="", uuid="", fsroot="")


@dataclass(frozen=True, slots=True)
class _CacheMountRuntime:
    seam_root: str
    ctx: ExecutionContextPort
    runner: CommandRunnerPort
    env_map: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _MountTables:
    live: dict[str, LiveMount]
    stacks: dict[str, tuple[LiveMount, ...]]
    fstab: dict[str, MountEntry]
    managed: tuple[ManagedFstabEntry, ...]
    unmanaged_targets: frozenset[str]


def _map_path(host_path: str, rt: _CacheMountRuntime, *, read_only: bool) -> str:
    if rt.seam_root:
        return f"{rt.seam_root.rstrip('/')}{host_path}"
    return host_view_path(rt.ctx, host_path) if read_only else host_path


def _lookup_passwd_file(passwd_path: str, name: str) -> CacheMountUser | None:
    text = read_text(passwd_path)
    if text is None or not text:
        return None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) >= _ACCOUNT_MIN_FIELDS and fields[0] == name:
            try:
                return CacheMountUser(name=fields[0], uid=int(fields[2]), gid=int(fields[3]), home=fields[5])
            except ValueError:
                return None
    return None


def _default_username(env_map: Mapping[str, str]) -> str:
    for key in ("SUDO_USER", "USER"):
        cand = (env_map.get(key) or "").strip()
        if cand and cand != ROOT_ACCOUNT:
            return cand
    return pwd.getpwuid(os.getuid()).pw_name


def _resolve_target_user(requested: str | None, rt: _CacheMountRuntime) -> tuple[CacheMountUser | None, str | None]:
    username = requested or _default_username(rt.env_map)
    record: CacheMountUser | None = None
    if rt.seam_root:
        account_db_path = f"{rt.seam_root.rstrip('/')}{_ACCOUNT_DB_PATH}"
        if os.path.isfile(account_db_path):
            record = _lookup_passwd_file(account_db_path, username)
    if record is None:
        try:
            pw = pwd.getpwnam(username)
            record = CacheMountUser(name=pw.pw_name, uid=pw.pw_uid, gid=pw.pw_gid, home=pw.pw_dir)
        except KeyError:
            return None, f"user {username!r} does not exist"
    if record.uid < MIN_HUMAN_UID:
        return None, f"refusing system user {record.name!r} (uid={record.uid} < {MIN_HUMAN_UID})"
    return replace(record, home=_real_home(record.home, rt)), None


def _real_home(home: str, rt: _CacheMountRuntime) -> str:
    """The home as the kernel and findmnt name it: passwd's /home/u is /var/home/u on Silverblue
    (/home is a link). Resolved where the program sees the host (the test root, /run/host); a path
    that leaves that view, or a home that cannot be resolved, stays as passwd has it."""
    view = _map_path(home, rt, read_only=True)
    if not view.endswith(home):
        return home
    prefix = view.removesuffix(home)
    real = os.path.realpath(view)
    if not prefix:
        return real
    return real.removeprefix(prefix) if real.startswith(f"{prefix}/") else home


def _from_toolbox(rt: _CacheMountRuntime) -> bool:
    """Whether the probes go to the host: a run inside a toolbox with no seam root."""
    return not rt.seam_root and rt.ctx.is_container()


def _read_live_mounts(rt: _CacheMountRuntime) -> tuple[tuple[LiveMount, ...], str | None]:
    """Every live mount, from one `findmnt -J -l` call (on the host from a toolbox); (), and why,
    when findmnt cannot answer: nothing is guessed from another table."""
    res = rt.runner.run(
        ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS],
        on_host=_from_toolbox(rt),
        timeout=_FINDMNT_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    if res.returncode != 0:
        return (), res.failure("findmnt")
    mounts = parse_findmnt_mounts(res.stdout)
    if mounts is None:
        return (), "findmnt's answer is not the JSON it should be"
    return mounts, None


def _dir_is_nonempty(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return True
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


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
    rt: _CacheMountRuntime,
) -> BindingProbe:
    binding = build_user_bindings(
        user,
        include_cache=(kind == CacheMountKind.CACHE),
        include_scratchpad=(kind == CacheMountKind.SCRATCHPAD),
    )[0]
    mapped_src = _map_path(binding.source, rt, read_only=True)
    mapped_tgt = _map_path(binding.target, rt, read_only=True)
    stack = tables.stacks.get(binding.target, ())
    live = stack[-1] if stack else None
    is_link = os.path.islink(mapped_tgt)
    exists = os.path.lexists(mapped_tgt)
    nonempty = False if is_link else _dir_is_nonempty(mapped_tgt)
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
        nonempty,
        os.path.isdir(mapped_src),
        fstab_unmanaged=binding.target in tables.unmanaged_targets,
        source_is_symlink=os.path.islink(mapped_src),
        target_not_dir=exists and not is_link and not os.path.isdir(mapped_tgt),
        source_unchecked=live is not None and not base_info.mounted,
        bind_below=mounted_other and any(is_bind_from(m, base_info, user.name) for m in stack[:-1]),
    )


def _probe_snapshot(parsed: CacheMountParsedArgs, rt: _CacheMountRuntime) -> CacheMountSnapshot:
    # Byte-transparent: the text is written back, and the bytes of the lines CST does not manage stay.
    # A file there that cannot be read is no missing file: a fstab built from "" would drop every line.
    fstab_error: str | None = None
    try:
        fstab_text = LocalFileSystemAdapter().read_existing_text(_map_path(FSTAB_PATH, rt, read_only=True))
    except OSError as exc:
        fstab_text, fstab_error = None, f"cannot read {FSTAB_PATH}: {exc.strerror or exc}"
    crypttab_text = read_text(_map_path(_CRYPTTAB_PATH, rt, read_only=True))
    mounts, mounts_error = _read_live_mounts(rt)

    user, user_err = _resolve_target_user(parsed.user, rt)
    home_mount = mount_holding(mounts, user.home) if user is not None else None
    managed = parse_managed_fstab_entries(fstab_text or "")
    tables = _MountTables(
        live=mounts_by_target(mounts),
        stacks=mount_stacks(mounts),
        fstab={m.target: m for m in parse_all_mounts(fstab_text or "")},
        managed=managed,
        unmanaged_targets=unmanaged_fstab_targets(fstab_text or ""),
    )
    bases = {kind: _probe_base(kind, tables) for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD)}
    kinds = selected_kinds(include_cache=parsed.include_cache, include_scratchpad=parsed.include_scratchpad)
    probes = tuple(_probe_binding(k, user, bases[k], tables, rt) for k in kinds) if user is not None else ()
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
        home_missing=user is not None and not os.path.isdir(_map_path(user.home, rt, read_only=True)),
        fstab_error=fstab_error,
    )


def _ensure_private_dir(host_path: str, user: CacheMountUser) -> None:
    """The folder at host_path (created 0700, and labelled as root, when missing), then made 0700 and
    the user's through a descriptor opened without following a link: a link put there after the
    probe fails (ELOOP) instead of having its target changed."""
    ensure_dir(host_path, DIR_MODE_PRIVATE, relabel=True)
    fd = os.open(host_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, DIR_MODE_PRIVATE)
        if os.geteuid() == 0:
            os.fchown(fd, user.uid, user.gid)
    finally:
        os.close(fd)


def _ensure_scratchpad_tag(source_path: str, user: CacheMountUser) -> None:
    tag_path = os.path.join(source_path, CACHE_TAG)
    if os.path.lexists(tag_path):
        return
    payload = CACHE_SIGNATURE + b"\n# Directory tag written by cst btrfs cache (excluded from backups).\n"
    write_atomic(tag_path, payload, mode=FILE_MODE_TAG, owner=(user.uid, user.gid))


def _run_mount(rt: _CacheMountRuntime, argv: list[str], what: str) -> str | None:
    """Run mount or umount; None, or why it failed (what: the command as the message names it)."""
    res = rt.runner.run(argv, timeout=_MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE)
    return None if res.returncode == 0 else res.failure(what)


def _bind(rt: _CacheMountRuntime, b: CacheMountBinding) -> str | None:
    """mount --bind of one binding; None, or why it failed."""
    argv = ["mount", "--bind", _map_path(b.source, rt, read_only=False), _map_path(b.target, rt, read_only=False)]
    return _run_mount(rt, argv, f"mount --bind {b.source} {b.target}")


def _unbind(rt: _CacheMountRuntime, b: CacheMountBinding) -> str | None:
    """umount of one binding's target; None, or why it failed."""
    return _run_mount(rt, ["umount", _map_path(b.target, rt, read_only=False)], f"umount {b.target}")


@dataclass(frozen=True, slots=True)
class _FstabFailure:
    """Why writing /etc/fstab and reloading systemd failed, and what of /etc/fstab could not be undone."""

    cause: str
    undo_errors: tuple[str, ...] = ()  # /etc/fstab could not be put back: its new lines stay


def _commit_fstab(fstab_path: str, previous: str | None, new_text: str, rt: _CacheMountRuntime) -> _FstabFailure | None:
    """Write /etc/fstab and reload systemd; None, or why it failed, after /etc/fstab was put back as
    it was (the previous text, or no file when there was none) or, in undo_errors, why it could not be.
    A failed first write leaves the file as it was (write_atomic)."""
    try:
        write_atomic(fstab_path, new_text, mode=_FSTAB_MODE)
    except OSError as exc:
        return _FstabFailure(f"cannot write {FSTAB_PATH}: {exc.strerror or exc}")
    why = _reload_systemd(rt)
    if why is None:
        return None
    put_back = _put_fstab_back(fstab_path, previous)
    return _FstabFailure(why, () if put_back is None else (put_back,))


def _reload_systemd(rt: _CacheMountRuntime) -> str | None:
    """systemctl daemon-reload; None, or why it failed."""
    res = SystemctlAdapter(rt.runner, on_host=SystemctlHost.LOCAL).daemon_reload_result()
    return None if res.returncode == 0 else res.failure("systemctl daemon-reload")


def _fstab_behind(snap: CacheMountSnapshot, rt: _CacheMountRuntime, mounting: Collection[str] = ()) -> bool | None:
    """Whether systemd has not loaded /etc/fstab as it is on disk (systemd_behind_fstab over the mount
    units it has loaded, one `systemctl show '*.mount'`, on the host from a toolbox as findmnt is; mounting:
    the targets attach mounts); None when systemctl gives no answer."""
    return systemd_behind_fstab(_mount_units(rt), snap.bindings, mounting)


def _mount_units(rt: _CacheMountRuntime) -> list[dict[str, str]] | None:
    """The mount units systemd has loaded (MOUNT_UNIT_PROPS), one `systemctl show '*.mount'`, on the host
    from a toolbox as findmnt is; None when systemctl gives no answer."""
    on_host = SystemctlHost.SHOW_ONLY if _from_toolbox(rt) else SystemctlHost.LOCAL
    return SystemctlAdapter(rt.runner, on_host=on_host).show_units("*.mount", MOUNT_UNIT_PROPS)


def _cache_set_up(snap: CacheMountSnapshot) -> bool:
    """Whether status asks systemd about /etc/fstab: a managed line, or a base in /etc/fstab or mounted (an
    attach or detach may have stopped before its reload); not for a machine without the cache, where a hand
    edit of /etc/fstab is no business of the cache's."""
    return bool(snap.all_managed) or any(b.in_fstab or b.mounted for b in snap.bases.values())


def _catch_up_reload(
    snap: CacheMountSnapshot, rt: _CacheMountRuntime, mounting: Collection[str] = ()
) -> tuple[str | None, list[str]]:
    """For a run that writes no /etc/fstab: daemon-reload when systemd has not loaded /etc/fstab as it is
    on disk (an earlier run stopped between its write and its reload), or when systemctl cannot say:
    (why the reload failed, or None; the lines to add). A run with nothing pending reloads nothing. When
    systemctl cannot say and the reload fails too, no systemd answers (a rescue shell, a PID 1 that does
    not answer on D-Bus): nothing points to it being behind, so a warning line, and the run goes on (a
    detach still unmounts)."""
    behind = _fstab_behind(snap, rt, mounting)
    if behind is False:
        return None, []
    why = _reload_systemd(rt)
    if why is not None and behind is None:
        warning = (
            f"warning: {why}; systemctl could not say whether systemd had loaded {FSTAB_PATH} as it is on disk "
            "either (run systemctl daemon-reload once it works)"
        )
        return None, [warning]
    if why is not None:
        return (
            f"{why}; {FSTAB_PATH} is as asked, systemd has not loaded it (run systemctl daemon-reload once it works)",
            [],
        )
    said = (
        f"systemd had not loaded {FSTAB_PATH} as it is on disk (an earlier run stopped)"
        if behind
        else f"systemctl could not say whether systemd had loaded {FSTAB_PATH} as it is on disk"
    )
    return None, [f"reloaded: {said}"]


def _attach_fstab(snap: CacheMountSnapshot) -> tuple[str, str]:
    """/etc/fstab as it is (empty when there is none) and as attach would write it."""
    cur = snap.fstab_text or ""
    return cur, plan_attach_fstab(cur, [p.binding for p in snap.bindings])


def _put_fstab_back(fstab_path: str, previous: str | None) -> str | None:
    """/etc/fstab as it was (the previous text, or no file when there was none); None, or why not."""
    try:
        if previous is None:
            LocalFileSystemAdapter().remove_file(fstab_path)  # already gone: as it was
        else:
            write_atomic(fstab_path, previous, mode=_FSTAB_MODE)
    except OSError as exc:
        return f"cannot put {FSTAB_PATH} back: {exc.strerror or exc}"
    return None


def _restore_fstab(fstab_path: str, previous: str | None, rt: _CacheMountRuntime) -> list[str]:
    """Put /etc/fstab back as it was and reload systemd; what of it failed."""
    failed = _put_fstab_back(fstab_path, previous) or _reload_systemd(rt)
    return [] if failed is None else [failed]


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
    rt: _CacheMountRuntime, done: Sequence[CacheMountBinding], *, mounted: bool
) -> list[tuple[CacheMountBinding, str]]:
    """Take back what this run did, last first: unmount the binds it mounted (mounted=True) or
    mount again the ones it unmounted; each binding that could not be taken back, with why."""
    step = _unbind if mounted else _bind
    return [(b, err) for b in reversed(done) if (err := step(rt, b)) is not None]


def _undo(rt: _CacheMountRuntime, done: Sequence[CacheMountBinding], *, mounted: bool) -> list[str]:
    """_undo_each's failures, the reasons only."""
    return [err for _, err in _undo_each(rt, done, mounted=mounted)]


def _prepare_attach_dirs(snap: CacheMountSnapshot, user: CacheMountUser, rt: _CacheMountRuntime) -> str | None:
    """Make each binding's folders (and the scratchpad's tag); the ERROR line of the first failure."""
    for probe in snap.bindings:
        b = probe.binding
        h_src = _map_path(b.source, rt, read_only=False)
        h_tgt = _map_path(b.target, rt, read_only=False)
        try:
            _ensure_private_dir(h_src, user)
            if b.kind == CacheMountKind.SCRATCHPAD:
                _ensure_scratchpad_tag(h_src, user)
            if not probe.target_exists:
                _ensure_private_dir(h_tgt, user)
        except OSError as exc:
            return f"ERROR: cannot prepare {b.source} and {b.target}: {exc.strerror or exc}"
    return None


def _bind_missing(snap: CacheMountSnapshot, rt: _CacheMountRuntime) -> tuple[list[CacheMountBinding], str | None]:
    """mount --bind each binding not mounted yet; on a failure, the binds this run mounted are taken
    back and the ERROR line comes with them."""
    mounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if probe.mounted_from_source:
            continue
        err = _bind(rt, probe.binding)
        if err is not None:
            return mounted, _error_after_undo(err, _undo(rt, mounted, mounted=True))
        mounted.append(probe.binding)
    return mounted, None


def _unbind_cst_mounts(snap: CacheMountSnapshot, rt: _CacheMountRuntime) -> tuple[list[CacheMountBinding], str | None]:
    """Unmount the binds CST made, until one fails: the binds unmounted, and why the one failed."""
    unmounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if not probe.mounted_from_source:
            continue
        err = _unbind(rt, probe.binding)
        if err is not None:
            return unmounted, err
        unmounted.append(probe.binding)
    return unmounted, None


def _commit_fstab_or_undo(
    snap: CacheMountSnapshot, new_fstab: str, rt: _CacheMountRuntime, mounted: Sequence[CacheMountBinding]
) -> str | None:
    """Write /etc/fstab and reload systemd; on a failure, unmount the binds this run mounted and give
    the ERROR line."""
    failed = _commit_fstab(_map_path(FSTAB_PATH, rt, read_only=False), snap.fstab_text, new_fstab, rt)
    if failed is None:
        return None
    undo_errors = [*_undo(rt, mounted, mounted=True), *failed.undo_errors]
    return _error_after_undo(failed.cause, undo_errors)


def _apply_attach(
    snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, rt: _CacheMountRuntime
) -> tuple[int, list[str]]:
    """Folders, then mount --bind, then /etc/fstab and daemon-reload: a failure takes back the
    binds this run mounted (and /etc/fstab), so no line is left that would mount at the next boot."""
    blockers = validate_attach_snapshot(snap, allow_nonempty=parsed.allow_nonempty)
    if blockers or snap.user is None:
        return 1, [f"ERROR: {msg}" for msg in (blockers or [snap.user_error or "invalid state"])]

    user = snap.user
    lines = cache_seam_lines(rt.seam_root)
    error = _prepare_attach_dirs(snap, user, rt)
    if error is not None:
        return 1, [*lines, error]

    cur_fstab, new_fstab = _attach_fstab(snap)
    mounted, error = _bind_missing(snap, rt)
    if error is not None:
        return 1, [*lines, error]

    reloaded: list[str] = []
    if new_fstab == cur_fstab:
        # After the mounts: a bind this run mounted whose line is in /etc/fstab shows whether systemd
        # read that line (systemd_behind_fstab). A catch-up reload that fails takes them back.
        now = tuple(replace(p, mounted_from_source=True) if p.binding in mounted else p for p in snap.bindings)
        cause, reloaded = _catch_up_reload(replace(snap, bindings=now), rt, {b.target for b in mounted})
        if cause is not None:
            return 1, [*lines, _error_after_catch_up(cause, _undo(rt, mounted, mounted=True), mounted)]
    else:
        error = _commit_fstab_or_undo(snap, new_fstab, rt, mounted)
        if error is not None:
            return 1, [*lines, error]
    lines.extend(f"Mounted {b.source} -> {b.target}." for b in mounted)
    lines.extend(reloaded)
    if new_fstab != cur_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    if new_fstab == cur_fstab and not mounted and not reloaded:
        lines.append(f"Already attached for {user.name}; nothing to do.")
    return 0, lines


def _apply_detach(
    snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, rt: _CacheMountRuntime
) -> tuple[int, list[str]]:
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
    lines = cache_seam_lines(rt.seam_root)
    blockers = detach_blockers(snap)
    if blockers:
        return 1, [*lines, *(f"ERROR: {msg}" for msg in blockers)]

    lines.extend(f"kept: {p.binding.target} has a mount CST did not make" for p in snap.bindings if p.mounted_other)

    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_detach_fstab(cur_fstab, user.name, set(kinds))
    fstab_path = _map_path(FSTAB_PATH, rt, read_only=False)
    reloaded: list[str] = []
    if new_fstab != cur_fstab:
        failed = _commit_fstab(fstab_path, snap.fstab_text, new_fstab, rt)
        if failed is not None:
            return 1, [*lines, _error_after_undo(failed.cause, failed.undo_errors)]
    else:
        # Before any umount: a catch-up reload that fails leaves nothing to take back.
        cause, reloaded = _catch_up_reload(snap, rt)
        if cause is not None:
            return 1, [*lines, f"ERROR: {cause}"]
    lines.extend(reloaded)

    unmounted, cause = _unbind_cst_mounts(snap, rt)
    if cause is not None:
        not_back = _undo_each(rt, unmounted, mounted=False)
        undo_errors = [err for _, err in not_back]
        # The lines of the binds that are not mounted again stay out of /etc/fstab.
        put_back = (
            snap.fstab_text if not not_back else plan_detach_fstab(cur_fstab, user.name, {b.kind for b, _ in not_back})
        )
        if new_fstab not in (cur_fstab, put_back):
            undo_errors += _restore_fstab(fstab_path, put_back, rt)
        return 1, [*lines, _error_after_undo(cause, undo_errors)]
    lines.extend(f"Unmounted {b.target}." for b in unmounted)
    if new_fstab != cur_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    if unmounted or new_fstab != cur_fstab:
        lines.append("Data in /var/mnt/{cache,scratchpad} kept untouched.")
    elif not reloaded:
        lines.append(f"Nothing attached for {user.name}; nothing to do.")
    return 0, lines


def dispatch_cache_mount_cli_action(
    parsed: CacheMountParsedArgs, env: ManagedCliEnv, *, read_only: bool
) -> tuple[int, list[str]]:
    """Resolve runtime dependencies and execute the requested `cache` subcommand."""
    del read_only
    env_map = os.environ if env.env is None else env.env
    rt = _CacheMountRuntime(
        seam_root=(env_map.get(CACHE_SEAM_ROOT_ENV) or "").strip(),
        ctx=HostExecutionContext() if env.ctx is None else env.ctx,
        runner=SubprocessCommandRunner() if env.runner is None else env.runner,
        env_map=env_map,
    )
    prog = env_map.get("CST_PROG") or "cst btrfs cache"

    snap = _probe_snapshot(parsed, rt)
    match parsed.action:
        case CacheMountAction.STATUS:
            if snap.mounts_error is None and snap.fstab_error is None and _cache_set_up(snap):
                targets = {e.target for e in snap.all_managed}
                snap = replace(snap, fstab_behind=fstab_lines_unread(_mount_units(rt), targets))
            return 0, format_cache_mount_status(snap, seam_root=rt.seam_root)
        case CacheMountAction.PLAN:
            if snap.user is not None:
                cur_fstab, new_fstab = _attach_fstab(snap)
                if new_fstab == cur_fstab:  # attach would only mount and reload
                    mounting = {p.binding.target for p in snap.bindings if not p.mounted_from_source}
                    snap = replace(snap, fstab_behind=_fstab_behind(snap, rt, mounting))
            return 0, format_cache_mount_plan(snap, prog, allow_nonempty=parsed.allow_nonempty, seam_root=rt.seam_root)
        case CacheMountAction.ATTACH:
            return _apply_attach(snap, parsed, rt)
        case CacheMountAction.DETACH:
            return _apply_detach(snap, parsed, rt)
        case _:
            return 0, format_cache_mount_status(snap, seam_root=rt.seam_root)
