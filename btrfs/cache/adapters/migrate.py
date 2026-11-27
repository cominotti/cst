# SPDX-License-Identifier: MPL-2.0
"""Host adapter for `attach --migrate`: open-fd/cwd `/proc` inspection, `cp -a`, atomic rename, rollback."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
from typing import Final

from btrfs.cache.adapters.mounts import (
    CacheMountRuntime,
    dir_is_nonempty,
    ensure_private_dir,
    from_toolbox,
    map_path,
)
from btrfs.cache.domain import BusyProcess, CacheMountUser
from btrfs.cache.domain.migrate import BTRFS_SUBVOL_ROOT_INO
from cst_core.adapters.files import read_text
from cst_core.domain.values import parse_whole_number
from cst_core.ports import OutputMode

_BTRFS_TIMEOUT_S: Final = 15
_CP_TIMEOUT_S: Final = 1800
_COMM_LIMIT: Final = 64
_DELETED_SUFFIX: Final = " (deleted)"


def _match_target(link: str, prefixes: tuple[str, ...]) -> str | None:
    clean = link.removesuffix(_DELETED_SUFFIX)
    for pref in prefixes:
        if clean == pref or clean.startswith(f"{pref}/"):
            return clean
    return None


def _pid_hit(pid: int, proc_dir: str, prefixes: tuple[str, ...]) -> BusyProcess | None:
    comm = (read_text(f"{proc_dir}/comm", "", limit=_COMM_LIMIT) or "?").strip()
    try:
        cwd = os.readlink(f"{proc_dir}/cwd")
    except OSError:
        cwd = ""
    matched = _match_target(cwd, prefixes) if cwd else None
    if matched is None:
        try:
            with os.scandir(f"{proc_dir}/fd") as fds:
                for fd_entry in fds:
                    try:
                        target = os.readlink(fd_entry.path)
                    except OSError:
                        continue
                    matched = _match_target(target, prefixes)
                    if matched is not None:
                        break
        except OSError:
            return None
    if matched is None:
        return None
    return BusyProcess(pid=pid, comm=comm, path=matched)


def copy_dir_contents(rt: CacheMountRuntime, src: str, dst: str) -> str | None:
    """Copy all entries from `src` into `dst` preserving attributes (`cp -a`)."""
    msrc = map_path(src, rt, read_only=False)
    mdst = map_path(dst, rt, read_only=False)
    try:
        with os.scandir(msrc) as it:
            if next(it, None) is None:
                return None
    except OSError as exc:
        return f"cannot read source {src}: {exc.strerror or exc}"
    res = rt.runner.run(
        ["cp", "-a", f"{msrc.rstrip('/')}/.", mdst],
        timeout=_CP_TIMEOUT_S,
        locale_c=True,
        output=OutputMode.CAPTURE,
    )
    return None if res.returncode == 0 else res.failure(f"cp -a {src}/. {dst}")


def clear_dir_entries(rt: CacheMountRuntime, path: str) -> None:
    """Best-effort cleanup of entries directly under `path`."""
    mapped = map_path(path, rt, read_only=False)
    try:
        with os.scandir(mapped) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)
    except OSError:
        pass


class CacheMigrationAdapter:
    """CacheMigrationPort over local filesystem, `/proc`, and `cp -a`."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def backup_exists(self, backup_path: str) -> bool:
        return os.path.lexists(map_path(backup_path, self._rt, read_only=True))

    def source_nonempty(self, source_path: str) -> bool:
        return dir_is_nonempty(map_path(source_path, self._rt, read_only=True))

    def is_btrfs_subvol(self, target_path: str) -> bool:
        mapped = map_path(target_path, self._rt, read_only=True)
        try:
            st = os.lstat(mapped)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_ino != BTRFS_SUBVOL_ROOT_INO:
            return False
        on_host = from_toolbox(self._rt)
        target_arg = target_path if on_host else mapped
        res = self._rt.runner.run(
            ["btrfs", "subvolume", "show", target_arg],
            on_host=on_host,
            timeout=_BTRFS_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return res.returncode == 0

    def busy_processes(self, target_path: str) -> tuple[tuple[BusyProcess, ...], str | None]:
        mapped_proc = map_path("/proc", self._rt, read_only=True)
        proc_root = mapped_proc if os.path.isdir(mapped_proc) else "/proc"
        mapped_target = map_path(target_path, self._rt, read_only=True)
        real_target = os.path.realpath(mapped_target)
        prefixes = tuple(dict.fromkeys((mapped_target.rstrip("/"), real_target.rstrip("/"), target_path.rstrip("/"))))
        my_pid = os.getpid()
        found: list[BusyProcess] = []
        try:
            with os.scandir(proc_root) as it:
                for entry in it:
                    pid = parse_whole_number(entry.name)
                    if pid is None or pid == my_pid:
                        continue
                    hit = _pid_hit(pid, entry.path, prefixes)
                    if hit is not None:
                        found.append(hit)
        except OSError as exc:
            return (), f"cannot scan {proc_root}: {exc.strerror or exc}"
        found.sort(key=lambda p: p.pid)
        return tuple(found), None

    def copy_tree(self, src: str, dst: str) -> str | None:
        return copy_dir_contents(self._rt, src, dst)

    def clear_dir_contents(self, path: str) -> None:
        clear_dir_entries(self._rt, path)

    def rename(self, old: str, new: str) -> None:
        os.rename(map_path(old, self._rt, read_only=False), map_path(new, self._rt, read_only=False))

    def ensure_empty_dir(self, path: str, user: CacheMountUser) -> None:
        ensure_private_dir(map_path(path, self._rt, read_only=False), user)

    def remove_empty_dir(self, path: str) -> None:
        try:
            os.rmdir(map_path(path, self._rt, read_only=False))
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
