# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs cache` (`btrfs/btrfs-cache`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import os
import pwd
import stat
from typing import Final

from btrfs.cache.domain import (
    BASE_FOR_KIND,
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
    format_cache_mount_plan,
    format_cache_mount_status,
    has_managed_line,
    is_bind_from,
    mount_holding,
    mounts_by_target,
    parse_all_mounts,
    parse_findmnt_mounts,
    parse_managed_fstab_entries,
    plan_attach_fstab,
    plan_detach_fstab,
    selected_kinds,
    unmanaged_fstab_targets,
    validate_attach_snapshot,
)
from btrfs.compress.domain import FSTAB_PATH, MountEntry
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import FileOwner, LocalFileSystemAdapter, ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, host_view_path
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, OutputMode

_SEAM_ROOT_ENV: Final = "CST_BTRFS_CACHE_ROOT"
_CRYPTTAB_PATH: Final = "/etc/crypttab"
_ACCOUNT_DB_PATH: Final = "/etc/passwd"
_FINDMNT_TIMEOUT_S: Final = 15
_ACCOUNT_MIN_FIELDS: Final = 6
_FSTAB_MODE: Final = 0o644
_MOUNT_TIMEOUT_S: Final = 30


@dataclass(frozen=True, slots=True)
class _CacheMountRuntime:
    seam_root: str
    ctx: ExecutionContextPort
    runner: CommandRunnerPort
    env_map: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _MountTables:
    live: dict[str, LiveMount]
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
        if cand and cand != FileOwner.ROOT:
            return cand
    return pwd.getpwuid(os.getuid()).pw_name


def _resolve_target_user(requested: str | None, rt: _CacheMountRuntime) -> tuple[CacheMountUser | None, str | None]:
    username = requested or _default_username(rt.env_map)
    record: CacheMountUser | None = None
    if rt.seam_root:
        passwd_path = f"{rt.seam_root.rstrip('/')}{_ACCOUNT_DB_PATH}"
        if os.path.isfile(passwd_path):
            record = _lookup_passwd_file(passwd_path, username)
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


def _read_live_mounts(rt: _CacheMountRuntime) -> tuple[tuple[LiveMount, ...], str | None]:
    """Every live mount, from one `findmnt -J -l` call (on the host from a toolbox); (), and why,
    when findmnt cannot answer: nothing is guessed from another table."""
    res = rt.runner.run(
        ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS],
        on_host=(not rt.seam_root and rt.ctx.is_container()),
        timeout=_FINDMNT_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    if res.returncode != 0:
        why = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
        return (), f"findmnt failed: {why}"
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
    return BaseMountInfo(
        path=path,
        mounted=live is not None,
        fstype=live.fstype if live is not None else "",
        uuid=live.uuid if live is not None else "",
        options=live.options if live is not None else "",
        in_fstab=fst is not None,
        fstab_options=fst.options if fst is not None else "",
        source=live.source if live is not None else "",
        fsroot=live.fsroot if live is not None else "",
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
    live = tables.live.get(binding.target)
    is_link = os.path.islink(mapped_tgt)
    exists = os.path.lexists(mapped_tgt)
    nonempty = False if is_link else _dir_is_nonempty(mapped_tgt)
    from_source = live is not None and is_bind_from(live, base_info, user.name)
    return BindingProbe(
        binding,
        has_managed_line(tables.managed, binding),
        from_source,
        live is not None and not from_source,
        is_link,
        exists,
        nonempty,
        os.path.isdir(mapped_src),
        fstab_unmanaged=binding.target in tables.unmanaged_targets,
        source_is_symlink=os.path.islink(mapped_src),
        target_not_dir=exists and not is_link and not os.path.isdir(mapped_tgt),
    )


def _probe_snapshot(parsed: CacheMountParsedArgs, rt: _CacheMountRuntime) -> CacheMountSnapshot:
    # Byte-transparent: the text is written back, and the bytes of the lines CST does not manage stay.
    fstab_text = LocalFileSystemAdapter().read_text(_map_path(FSTAB_PATH, rt, read_only=True))
    crypttab_text = read_text(_map_path(_CRYPTTAB_PATH, rt, read_only=True))
    mounts, mounts_error = _read_live_mounts(rt)

    user, user_err = _resolve_target_user(parsed.user, rt)
    home_mount = mount_holding(mounts, user.home) if user is not None else None
    managed = parse_managed_fstab_entries(fstab_text or "")
    tables = _MountTables(
        live=mounts_by_target(mounts),
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
    if os.path.exists(tag_path) or os.path.islink(tag_path):
        return
    payload = CACHE_SIGNATURE + b"\n# Directory tag written by cst btrfs cache (excluded from backups).\n"
    write_atomic(tag_path, payload, mode=FILE_MODE_TAG, relabel=(os.geteuid() == 0))
    if os.geteuid() == 0:
        os.chown(tag_path, user.uid, user.gid)


def _bind(rt: _CacheMountRuntime, b: CacheMountBinding) -> str | None:
    """mount --bind of one binding; None, or why it failed."""
    res = rt.runner.run(
        ["mount", "--bind", _map_path(b.source, rt, read_only=False), _map_path(b.target, rt, read_only=False)],
        timeout=_MOUNT_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    return None if res.returncode == 0 else res.failure(f"mount --bind {b.source} {b.target}")


def _unbind(rt: _CacheMountRuntime, b: CacheMountBinding) -> str | None:
    """umount of one binding's target; None, or why it failed."""
    res = rt.runner.run(
        ["umount", _map_path(b.target, rt, read_only=False)],
        timeout=_MOUNT_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    return None if res.returncode == 0 else res.failure(f"umount {b.target}")


def _commit_fstab(fstab_path: str, previous: str | None, new_text: str, rt: _CacheMountRuntime) -> str | None:
    """Write /etc/fstab and reload systemd; None, or why it failed after /etc/fstab was put back
    as it was (the previous text, or no file when there was none)."""
    relabel = os.geteuid() == 0
    try:
        write_atomic(fstab_path, new_text, mode=_FSTAB_MODE, relabel=relabel)
    except OSError as exc:
        return f"cannot write {FSTAB_PATH}: {exc.strerror or exc}"
    res = SystemctlAdapter(rt.runner, on_host=SystemctlHost.LOCAL).daemon_reload_result()
    if res.returncode == 0:
        return None
    why = res.failure("systemctl daemon-reload")
    try:
        if previous is None:
            os.unlink(fstab_path)
        else:
            write_atomic(fstab_path, previous, mode=_FSTAB_MODE, relabel=relabel)
    except OSError as exc:
        return f"{why}; cannot put {FSTAB_PATH} back: {exc.strerror or exc}"
    return why


def _error_after_undo(cause: str, undo_errors: Sequence[str]) -> str:
    if undo_errors:
        return f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(undo_errors)})"
    return f"ERROR: {cause}; previous state restored."


def _undo(rt: _CacheMountRuntime, done: Sequence[CacheMountBinding], *, mounted: bool) -> list[str]:
    """Take back what this run did, last first: unmount the binds it mounted (mounted=True) or
    mount again the ones it unmounted; the failures."""
    step = _unbind if mounted else _bind
    return [err for b in reversed(done) if (err := step(rt, b)) is not None]


def _apply_attach(
    snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, rt: _CacheMountRuntime
) -> tuple[int, list[str]]:
    """Folders, then mount --bind, then /etc/fstab and daemon-reload: a failure takes back the
    binds this run mounted (and /etc/fstab), so no line is left that would mount at the next boot."""
    blockers = validate_attach_snapshot(snap, allow_nonempty=parsed.allow_nonempty)
    if blockers or snap.user is None:
        return 1, [f"ERROR: {msg}" for msg in (blockers or [snap.user_error or "invalid state"])]

    user = snap.user
    lines: list[str] = [f"test seam (CST_BTRFS_CACHE_ROOT): {rt.seam_root}"] if rt.seam_root else []
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
            return 1, [*lines, f"ERROR: cannot prepare {b.source} and {b.target}: {exc.strerror or exc}"]

    mounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if probe.mounted_from_source:
            continue
        err = _bind(rt, probe.binding)
        if err is not None:
            return 1, [*lines, _error_after_undo(err, _undo(rt, mounted, mounted=True))]
        mounted.append(probe.binding)

    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_attach_fstab(cur_fstab, [p.binding for p in snap.bindings])
    if new_fstab != cur_fstab:
        err = _commit_fstab(_map_path(FSTAB_PATH, rt, read_only=False), snap.fstab_text, new_fstab, rt)
        if err is not None:
            return 1, [*lines, _error_after_undo(err, _undo(rt, mounted, mounted=True))]
    lines.extend(f"Mounted {b.source} -> {b.target}." for b in mounted)
    if new_fstab != cur_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    if new_fstab == cur_fstab and not mounted:
        lines.append(f"Already attached for {user.name}; nothing to do.")
    return 0, lines


def _apply_detach(
    snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, rt: _CacheMountRuntime
) -> tuple[int, list[str]]:
    """Unmount only the binds CST made, then drop the user's managed lines; a failure mounts again
    what this run unmounted. A mount CST did not make stays (its managed line still goes)."""
    if snap.user is None:
        return 1, [f"ERROR: {snap.user_error or 'cannot resolve target user'}"]
    if snap.mounts_error is not None:
        return 1, [f"ERROR: cannot list mounts: {snap.mounts_error}"]
    user = snap.user
    kinds = selected_kinds(include_cache=parsed.include_cache, include_scratchpad=parsed.include_scratchpad)
    lines: list[str] = [f"test seam (CST_BTRFS_CACHE_ROOT): {rt.seam_root}"] if rt.seam_root else []

    unmounted: list[CacheMountBinding] = []
    for probe in snap.bindings:
        if probe.mounted_other:
            lines.append(f"kept: {probe.binding.target} has a mount CST did not make")
        if not probe.mounted_from_source:
            continue
        err = _unbind(rt, probe.binding)
        if err is not None:
            return 1, [*lines, _error_after_undo(err, _undo(rt, unmounted, mounted=False))]
        unmounted.append(probe.binding)

    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_detach_fstab(cur_fstab, user.name, set(kinds))
    if new_fstab != cur_fstab:
        err = _commit_fstab(_map_path(FSTAB_PATH, rt, read_only=False), snap.fstab_text, new_fstab, rt)
        if err is not None:
            return 1, [*lines, _error_after_undo(err, _undo(rt, unmounted, mounted=False))]
    lines.extend(f"Unmounted {b.target}." for b in unmounted)
    if new_fstab != cur_fstab:
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    if not unmounted and new_fstab == cur_fstab:
        lines.append(f"Nothing attached for {user.name}; nothing to do.")
    else:
        lines.append("Data in /var/mnt/{cache,scratchpad} kept untouched.")
    return 0, lines


def dispatch_cache_mount_cli_action(
    parsed: CacheMountParsedArgs, env: ManagedCliEnv, *, read_only: bool
) -> tuple[int, list[str]]:
    """Resolve runtime dependencies and execute the requested `cache` subcommand."""
    del read_only
    env_map = os.environ if env.env is None else env.env
    rt = _CacheMountRuntime(
        seam_root=(env_map.get(_SEAM_ROOT_ENV) or "").strip(),
        ctx=HostExecutionContext() if env.ctx is None else env.ctx,
        runner=SubprocessCommandRunner() if env.runner is None else env.runner,
        env_map=env_map,
    )
    prog = env_map.get("CST_PROG") or "cst btrfs cache"

    snap = _probe_snapshot(parsed, rt)
    match parsed.action:
        case CacheMountAction.STATUS:
            return 0, format_cache_mount_status(snap, seam_root=rt.seam_root)
        case CacheMountAction.PLAN:
            return 0, format_cache_mount_plan(snap, prog, allow_nonempty=parsed.allow_nonempty, seam_root=rt.seam_root)
        case CacheMountAction.ATTACH:
            return _apply_attach(snap, parsed, rt)
        case CacheMountAction.DETACH:
            return _apply_detach(snap, parsed, rt)
        case _:
            return 0, format_cache_mount_status(snap, seam_root=rt.seam_root)
