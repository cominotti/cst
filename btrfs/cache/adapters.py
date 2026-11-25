# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs cache` (`btrfs/btrfs-cache`): the ports of ports.py over the
host's files and commands, as the host sees them (the test root of CST_BTRFS_CACHE_ROOT; /run/host for the
reads from a toolbox), and cache_runtime, what they run over (wiring.py builds the use cases on them)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import os
import pwd
import stat
from typing import Final

from btrfs.cache.domain import (
    CACHE_SEAM_ROOT_ENV,
    DIR_MODE_PRIVATE,
    FILE_MODE_TAG,
    FINDMNT_COLUMNS,
    MIN_HUMAN_UID,
    CacheMountBinding,
    CacheMountUser,
    LiveMount,
    parse_findmnt_mounts,
)
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS
from btrfs.cache.ports import BindingPaths
from btrfs.compress.domain import FSTAB_PATH
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import LocalFileSystemAdapter, ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import host_view_path
from cst_core.domain.values import ROOT_ACCOUNT
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, OutputMode

_CRYPTTAB_PATH: Final = "/etc/crypttab"
_ACCOUNT_DB_PATH: Final = "/etc/passwd"
_FINDMNT_TIMEOUT_S: Final = 15
_ACCOUNT_MIN_FIELDS: Final = 6
_FSTAB_MODE: Final = 0o644
_MOUNT_TIMEOUT_S: Final = 30


@dataclass(frozen=True, slots=True)
class CacheMountRuntime:
    """What the adapters run over: the test root (CST_BTRFS_CACHE_ROOT, or ""), the host or toolbox the
    command runs in, the command runner and the environment (the caller's name)."""

    seam_root: str
    ctx: ExecutionContextPort
    runner: CommandRunnerPort
    env_map: Mapping[str, str]


def _map_path(host_path: str, rt: CacheMountRuntime, *, read_only: bool) -> str:
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


def _resolve_target_user(requested: str | None, rt: CacheMountRuntime) -> tuple[CacheMountUser | None, str | None]:
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


def _real_home(home: str, rt: CacheMountRuntime) -> str:
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


def _from_toolbox(rt: CacheMountRuntime) -> bool:
    """Whether the probes go to the host: a run inside a toolbox with no seam root."""
    return not rt.seam_root and rt.ctx.is_container()


def _read_live_mounts(rt: CacheMountRuntime) -> tuple[tuple[LiveMount, ...], str | None]:
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


def _ensure_private_dir(host_path: str, user: CacheMountUser) -> None:
    """The folder at host_path (created 0700, and labelled as root, when missing), then made 0700 and
    the user's through a descriptor opened without following a link: a link put there after the
    probe fails (ENOTDIR, as O_NOFOLLOW with O_DIRECTORY gives) instead of having its target changed."""
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


def _run_mount(rt: CacheMountRuntime, argv: list[str], what: str) -> str | None:
    """Run mount or umount; None, or why it failed (what: the command as the message names it)."""
    res = rt.runner.run(argv, timeout=_MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE)
    return None if res.returncode == 0 else res.failure(what)


class FstabFile:
    """FstabPort over the host's /etc/fstab: read where the program sees the host, written where it writes."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def read(self) -> str | None:
        return LocalFileSystemAdapter().read_existing_text(_map_path(FSTAB_PATH, self._rt, read_only=True))

    def write(self, text: str) -> None:
        write_atomic(_map_path(FSTAB_PATH, self._rt, read_only=False), text, mode=_FSTAB_MODE)

    def remove(self) -> None:
        LocalFileSystemAdapter().remove_file(_map_path(FSTAB_PATH, self._rt, read_only=False))


class CacheMountTable:
    """MountTablePort over findmnt (on the host from a toolbox), mount --bind and umount (30 s, C locale)."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def read(self) -> tuple[tuple[LiveMount, ...], str | None]:
        return _read_live_mounts(self._rt)

    def bind(self, binding: CacheMountBinding) -> str | None:
        rt = self._rt
        argv = [
            "mount",
            "--bind",
            _map_path(binding.source, rt, read_only=False),
            _map_path(binding.target, rt, read_only=False),
        ]
        return _run_mount(rt, argv, f"mount --bind {binding.source} {binding.target}")

    def unbind(self, binding: CacheMountBinding) -> str | None:
        rt = self._rt
        return _run_mount(rt, ["umount", _map_path(binding.target, rt, read_only=False)], f"umount {binding.target}")


class CacheSystemd:
    """CacheSystemdPort over systemctl (SystemctlAdapter): the reload local, the mount units on the host from
    a toolbox as findmnt is, one `systemctl show --state=loaded -p ... '*.mount'`: without --state=loaded a
    glob hides the inactive units, and a stale /etc/fstab whose units are all inactive would look current."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def daemon_reload(self) -> str | None:
        res = SystemctlAdapter(self._rt.runner, on_host=SystemctlHost.LOCAL).daemon_reload_result()
        return None if res.returncode == 0 else res.failure("systemctl daemon-reload")

    def mount_units(self) -> list[dict[str, str]] | None:
        on_host = SystemctlHost.SHOW_ONLY if _from_toolbox(self._rt) else SystemctlHost.LOCAL
        return SystemctlAdapter(self._rt.runner, on_host=on_host).show_units("*.mount", MOUNT_UNIT_PROPS)


class CacheFolders:
    """CacheFoldersPort over the folders where the program writes."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def ensure_private(self, path: str, user: CacheMountUser) -> None:
        _ensure_private_dir(_map_path(path, self._rt, read_only=False), user)

    def ensure_tag(self, source: str, user: CacheMountUser) -> None:
        _ensure_scratchpad_tag(_map_path(source, self._rt, read_only=False), user)


class CacheHostView:
    """CacheHostViewPort over the files where the program sees the host."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def crypttab(self) -> str | None:
        return read_text(_map_path(_CRYPTTAB_PATH, self._rt, read_only=True))

    def binding_paths(self, binding: CacheMountBinding) -> BindingPaths:
        src = _map_path(binding.source, self._rt, read_only=True)
        tgt = _map_path(binding.target, self._rt, read_only=True)
        is_link = os.path.islink(tgt)
        exists = os.path.lexists(tgt)
        return BindingPaths(
            target_is_link=is_link,
            target_exists=exists,
            target_nonempty=False if is_link else _dir_is_nonempty(tgt),
            source_is_dir=os.path.isdir(src),
            source_is_link=os.path.islink(src),
            target_is_dir=exists and not is_link and os.path.isdir(tgt),
        )

    def home_exists(self, home: str) -> bool:
        return os.path.isdir(_map_path(home, self._rt, read_only=True))


class CacheAccounts:
    """CacheAccountsPort over the test root's passwd (when there is one) and the account database."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def resolve(self, requested: str | None) -> tuple[CacheMountUser | None, str | None]:
        return _resolve_target_user(requested, self._rt)


def cache_runtime(
    env_map: Mapping[str, str], ctx: ExecutionContextPort | None, runner: CommandRunnerPort | None
) -> CacheMountRuntime:
    """The runtime of one run: the test root from env_map, ctx and runner (None: the host's, a subprocess)."""
    return CacheMountRuntime(
        seam_root=(env_map.get(CACHE_SEAM_ROOT_ENV) or "").strip(),
        ctx=HostExecutionContext() if ctx is None else ctx,
        runner=SubprocessCommandRunner() if runner is None else runner,
        env_map=env_map,
    )
