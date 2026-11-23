# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs cache` (`btrfs/btrfs-cache`)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import pwd
import stat
from typing import Final

from btrfs.compress.domain import FSTAB_PATH, MountEntry, PROC_MOUNTS_PATH
from btrfs.cache.domain import (
    BASE_FOR_KIND,
    DIR_MODE_PRIVATE,
    FILE_MODE_TAG,
    MIN_HUMAN_UID,
    BindingProbe,
    BaseMountInfo,
    CacheMountAction,
    CacheMountKind,
    CacheMountParsedArgs,
    CacheMountSnapshot,
    CacheMountUser,
    build_user_bindings,
    format_cache_mount_plan,
    format_cache_mount_status,
    parse_all_mounts,
    parse_managed_fstab_entries,
    plan_attach_fstab,
    plan_detach_fstab,
    selected_kinds,
    validate_attach_snapshot,
)
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import FileOwner, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, host_view_path
from cst_core.ports import CommandFailed, CommandRunnerPort, ExecutionContextPort, OutputMode

_SEAM_ROOT_ENV: Final = "CST_BTRFS_CACHE_ROOT"
_CRYPTTAB_PATH: Final = "/etc/crypttab"
_ACCOUNT_DB_PATH: Final = "/etc/passwd"
_FINDMNT_TIMEOUT_S: Final = 15
_ACCOUNT_MIN_FIELDS: Final = 6
_FSTAB_MODE: Final = 0o644
_MOUNT_TIMEOUT_S: Final = 30
_FINDMNT_PARTS_COUNT: Final = 3
_MOUNT_ARGS_MIN: Final = 4


@dataclass(frozen=True, slots=True)
class _CacheMountRuntime:
    seam_root: str
    ctx: ExecutionContextPort
    runner: CommandRunnerPort
    env_map: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _MountTables:
    live: dict[str, MountEntry]
    fstab: dict[str, MountEntry]
    home_uuid: str


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
    return record, None


def _read_mounts_text(rt: _CacheMountRuntime) -> str:
    if rt.seam_root:
        return read_text(f"{rt.seam_root.rstrip('/')}{PROC_MOUNTS_PATH}", "")
    res = rt.runner.run(
        ["findmnt", "-rn", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"],
        on_host=rt.ctx.is_container(),
        timeout=_FINDMNT_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    if res.returncode == 0:
        out_lines = []
        for line in res.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) == _MOUNT_ARGS_MIN:
                out_lines.append(f"{parts[0]} {parts[1]} {parts[2]} {parts[3]} 0 0")
        return "\n".join(out_lines) + ("\n" if out_lines else "")
    return read_text(PROC_MOUNTS_PATH, "")


def _probe_findmnt_target(
    runner: CommandRunnerPort,
    target_path: str,
    *,
    on_host: bool,
) -> tuple[str, str, str]:
    res = runner.run(
        ["findmnt", "-rn", "-o", "FSTYPE,UUID,SOURCE", "--target", target_path],
        on_host=on_host,
        timeout=_FINDMNT_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return "", "", ""
    parts = res.stdout.splitlines()[0].split(None, 2)
    if len(parts) < _FINDMNT_PARTS_COUNT:
        return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else ""), ""
    return parts[0], parts[1], parts[2]


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


def _probe_base(kind: CacheMountKind, tables: _MountTables, rt: _CacheMountRuntime) -> BaseMountInfo:
    path = BASE_FOR_KIND[kind]
    live = tables.live.get(path)
    fst = tables.fstab.get(path)
    mapped = _map_path(path, rt, read_only=True)
    uuid = ""
    fstype = live.fstype if live is not None else ""
    if live is not None and os.path.exists(mapped):
        q_target = mapped if rt.seam_root else path
        f_type, uuid, _ = _probe_findmnt_target(
            rt.runner, q_target, on_host=(not rt.seam_root and rt.ctx.is_container())
        )
        if not fstype:
            fstype = f_type
    return BaseMountInfo(
        path,
        live is not None,
        fstype,
        uuid,
        live.options if live is not None else "",
        fst is not None,
        fst.options if fst is not None else "",
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
    fst = tables.fstab.get(binding.target)
    is_link = os.path.islink(mapped_tgt)
    exists = os.path.exists(mapped_tgt)
    nonempty = False if is_link else _dir_is_nonempty(mapped_tgt)

    mounted_from_source = False
    mounted_other = False
    if live is not None:
        if live.source in (binding.source, mapped_src):
            mounted_from_source = True
        elif base_info.uuid and base_info.uuid != tables.home_uuid:
            q_tgt = mapped_tgt if rt.seam_root else binding.target
            _, tgt_uuid, _ = _probe_findmnt_target(
                rt.runner, q_tgt, on_host=(not rt.seam_root and rt.ctx.is_container())
            )
            mounted_from_source = tgt_uuid == base_info.uuid
            mounted_other = not mounted_from_source
        else:
            mounted_other = True
    return BindingProbe(
        binding,
        fst is not None,
        mounted_from_source,
        mounted_other,
        is_link,
        exists,
        nonempty,
        os.path.isdir(mapped_src),
    )


def _probe_snapshot(parsed: CacheMountParsedArgs, rt: _CacheMountRuntime) -> CacheMountSnapshot:
    fstab_text = read_text(_map_path(FSTAB_PATH, rt, read_only=True))
    crypttab_text = read_text(_map_path(_CRYPTTAB_PATH, rt, read_only=True))
    proc_mounts_text = _read_mounts_text(rt)

    user, user_err = _resolve_target_user(parsed.user, rt)
    home_uuid = ""
    if user is not None:
        mapped_home = _map_path(user.home, rt, read_only=True)
        q_home = mapped_home if rt.seam_root else user.home
        if os.path.exists(mapped_home):
            _, home_uuid, _ = _probe_findmnt_target(
                rt.runner, q_home, on_host=(not rt.seam_root and rt.ctx.is_container())
            )

    tables = _MountTables(
        live={m.target: m for m in parse_all_mounts(proc_mounts_text)},
        fstab={m.target: m for m in parse_all_mounts(fstab_text or "")},
        home_uuid=home_uuid,
    )
    bases = {kind: _probe_base(kind, tables, rt) for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD)}
    kinds = selected_kinds(include_cache=parsed.include_cache, include_scratchpad=parsed.include_scratchpad)
    probes = tuple(_probe_binding(k, user, bases[k], tables, rt) for k in kinds) if user is not None else ()
    managed = parse_managed_fstab_entries(fstab_text or "")
    return CacheMountSnapshot(
        user, user_err, home_uuid, bases, probes, managed, crypttab_text, fstab_text, proc_mounts_text
    )


def _ensure_private_dir(host_path: str, user: CacheMountUser) -> None:
    os.makedirs(host_path, mode=DIR_MODE_PRIVATE, exist_ok=True)
    os.chmod(host_path, DIR_MODE_PRIVATE)
    if os.geteuid() == 0:
        os.chown(host_path, user.uid, user.gid)


def _ensure_scratchpad_tag(source_path: str, user: CacheMountUser) -> None:
    tag_path = os.path.join(source_path, CACHE_TAG)
    if os.path.exists(tag_path) or os.path.islink(tag_path):
        return
    payload = CACHE_SIGNATURE + b"\n# Directory tag written by cst btrfs cache (excluded from backups).\n"
    write_atomic(tag_path, payload, mode=FILE_MODE_TAG, relabel=(os.geteuid() == 0))
    if os.geteuid() == 0:
        os.chown(tag_path, user.uid, user.gid)


def _write_fstab_and_reload(fstab_path: str, previous: str | None, new_text: str, runner: CommandRunnerPort) -> None:
    write_atomic(fstab_path, new_text, mode=_FSTAB_MODE, relabel=(os.geteuid() == 0))
    try:
        SystemctlAdapter(runner, on_host=SystemctlHost.LOCAL).daemon_reload()
    except CommandFailed:
        if previous is not None:
            write_atomic(fstab_path, previous, mode=_FSTAB_MODE, relabel=(os.geteuid() == 0))
        raise


def _apply_attach(
    snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, rt: _CacheMountRuntime
) -> tuple[int, list[str]]:
    blockers = validate_attach_snapshot(snap, allow_nonempty=parsed.allow_nonempty)
    if blockers or snap.user is None:
        return 1, [f"ERROR: {msg}" for msg in (blockers or [snap.user_error or "invalid state"])]

    user = snap.user
    lines: list[str] = [f"test seam (CST_BTRFS_CACHE_ROOT): {rt.seam_root}"] if rt.seam_root else []
    for probe in snap.bindings:
        b = probe.binding
        h_src = _map_path(b.source, rt, read_only=False)
        h_tgt = _map_path(b.target, rt, read_only=False)
        _ensure_private_dir(h_src, user)
        if b.kind == CacheMountKind.SCRATCHPAD:
            _ensure_scratchpad_tag(h_src, user)
        if not probe.target_exists:
            _ensure_private_dir(h_tgt, user)

    fstab_path = _map_path(FSTAB_PATH, rt, read_only=False)
    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_attach_fstab(cur_fstab, [p.binding for p in snap.bindings])
    if new_fstab != cur_fstab:
        _write_fstab_and_reload(fstab_path, snap.fstab_text, new_fstab, rt.runner)
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")

    mounted_any = False
    for probe in snap.bindings:
        if probe.mounted_from_source:
            continue
        b = probe.binding
        h_src = _map_path(b.source, rt, read_only=False)
        h_tgt = _map_path(b.target, rt, read_only=False)
        res = rt.runner.run(
            ["mount", "--bind", h_src, h_tgt], timeout=_MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE
        )
        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
            return 1, [*lines, f"ERROR: mount --bind {b.source} {b.target} failed: {err_msg}"]
        lines.append(f"Mounted {b.source} -> {b.target}.")
        mounted_any = True

    if new_fstab == cur_fstab and not mounted_any:
        lines.append(f"Already attached for {user.name}; nothing to do.")
    return 0, lines


def _apply_detach(
    snap: CacheMountSnapshot, parsed: CacheMountParsedArgs, rt: _CacheMountRuntime
) -> tuple[int, list[str]]:
    if snap.user is None:
        return 1, [f"ERROR: {snap.user_error or 'cannot resolve target user'}"]
    user = snap.user
    kinds = selected_kinds(include_cache=parsed.include_cache, include_scratchpad=parsed.include_scratchpad)
    lines: list[str] = [f"test seam (CST_BTRFS_CACHE_ROOT): {rt.seam_root}"] if rt.seam_root else []

    unmounted_any = False
    for probe in snap.bindings:
        if not (probe.mounted_from_source or probe.mounted_other):
            continue
        h_tgt = _map_path(probe.binding.target, rt, read_only=False)
        res = rt.runner.run(["umount", h_tgt], timeout=_MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE)
        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
            return 1, [*lines, f"ERROR: umount {probe.binding.target} failed: {err_msg}"]
        lines.append(f"Unmounted {probe.binding.target}.")
        unmounted_any = True

    fstab_path = _map_path(FSTAB_PATH, rt, read_only=False)
    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_detach_fstab(cur_fstab, user.name, set(kinds))
    if new_fstab != cur_fstab:
        _write_fstab_and_reload(fstab_path, snap.fstab_text, new_fstab, rt.runner)
        lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")

    if not unmounted_any and new_fstab == cur_fstab:
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
