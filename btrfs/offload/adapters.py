# SPDX-License-Identifier: MPL-2.0
"""Host filesystem, NOCOW ioctl, online sqlite3 + rsync mirror, install record, and user systemd adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import errno
import fnmatch
import os
from pathlib import Path
import posixpath
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Final

from btrfs.offload.domain import OffloadEntry, OffloadMode
from btrfs.offload.domain.plan import OffloadEntryProbe, PathProbe, ScratchpadProbe
from btrfs.offload.domain.units import OFFLOAD_SERVICE, OFFLOAD_TIMER, render_offload_service, render_offload_timer
from cst_core.adapters import SystemctlAdapter
from cst_core.adapters.files import (
    LocalFileSystemAdapter,
    copy_file_atomic,
    ensure_dir,
    is_masked_unit,
    open_regular_text,
    read_text,
    swap_dir_staged,
    write_atomic,
)
from cst_core.adapters.install import program_installer
from cst_core.adapters.locks import take_lock
from cst_core.adapters.nocow import FS_NOCOW_FL, inode_flags, set_nocow
from cst_core.adapters.runtime import install_payload
from cst_core.application.install import Placement, record_file
from cst_core.application.user_units import manager_unit_path
from cst_core.domain.install import Component, GeneratedKind, Owner, expand_dest, manifest_by_src
from cst_core.domain.shapes import parse_json
from cst_core.domain.units import user_unit_dir
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode, UnitName, WriteFailed

_RSYNC_TIMEOUT_S: Final = 600
_OFFLOAD_FINDMNT_TIMEOUT_S: Final = 10
_MAX_BUSY_PIDS: Final = 8
_BTRFS_MAGIC: Final = 0x9123683E
_OFFLOAD_COMPONENT: Final = Component.BTRFS_OFFLOAD
_HELPER_ENTRY: Final = manifest_by_src()["btrfs/btrfs-cache-offload"]
_SIDECAR_SUFFIXES: Final = ("", "-wal", "-shm", "-journal")
_UNSUPPORTED_NOCOW_ERRNOS: Final = (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL)


def _probe_path(path: str, *, inspect_nocow: bool = False) -> PathProbe:
    if os.path.islink(path):
        return PathProbe.link(path, target=os.readlink(path), resolved=os.path.realpath(path))
    exists = os.path.lexists(path)
    is_dir = exists and os.path.isdir(path)
    nonempty = False
    if is_dir:
        with contextlib.suppress(OSError), os.scandir(path) as it:
            nonempty = next(it, None) is not None
    nocow: bool | None = None
    if inspect_nocow and is_dir:
        with contextlib.suppress(OSError):
            nocow = bool(inode_flags(path, directory=True) & FS_NOCOW_FL)
    return PathProbe(path=path, exists=exists, is_dir=is_dir, is_symlink=False, nonempty=nonempty, nocow_enabled=nocow)


def _matches_any(rel_path: str, patterns: Sequence[str]) -> bool:
    base = posixpath.basename(rel_path)
    return any(fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(base, pat) for pat in patterns)


def _matches_nocow(rel_path: str, patterns: Sequence[str]) -> bool:
    if _matches_any(rel_path, patterns):
        return True
    parts = rel_path.split("/")
    return any(_matches_any("/".join(parts[:idx]), patterns) for idx in range(1, len(parts)))


def _try_set_nocow(path: str, *, directory: bool) -> None:
    try:
        set_nocow(path, on=True, directory=directory)
    except OSError as err:
        if err.errno not in _UNSUPPORTED_NOCOW_ERRNOS:
            raise


def _copy_file_with_nocow(src: str, dst: str, *, nocow: bool) -> None:
    st = os.stat(src)
    mode = stat.S_IMODE(st.st_mode)
    if not nocow:
        copy_file_atomic(src, dst, mode=mode, durable=False)
        with contextlib.suppress(OSError):
            shutil.copystat(src, dst, follow_symlinks=False)
        return
    ensure_dir(os.path.dirname(dst), 0o755)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    _try_set_nocow(dst, directory=False)
    with open(src, "rb") as in_fh, open(dst, "wb") as out_fh:
        shutil.copyfileobj(in_fh, out_fh, length=65536)
    with contextlib.suppress(OSError):
        shutil.copystat(src, dst, follow_symlinks=False)


def _copy_walk_file(src: str, dst: str, *, nocow: bool) -> None:
    st_mode = os.lstat(src).st_mode
    if stat.S_ISLNK(st_mode):
        os.symlink(os.readlink(src), dst)
    elif stat.S_ISREG(st_mode):
        _copy_file_with_nocow(src, dst, nocow=nocow)


def _rel_posix(rel_dir: str, name: str) -> str:
    return name if rel_dir == "." else posixpath.join(rel_dir, name)


def _walk_rel_nocow(cur_dir: str, root: str, *, force_nocow: bool, nocow_paths: Sequence[str]) -> tuple[str, bool]:
    rel_dir = os.path.relpath(cur_dir, root).replace(os.sep, "/")
    return rel_dir, force_nocow or (rel_dir != "." and _matches_nocow(rel_dir, nocow_paths))


def _link_symlink_subdirs(cur_dir: str, target_dir: str, dirs: list[str]) -> None:
    kept: list[str] = []
    for name in dirs:
        src_sub = os.path.join(cur_dir, name)
        if os.path.islink(src_sub):
            os.symlink(os.readlink(src_sub), os.path.join(target_dir, name))
        else:
            kept.append(name)
    dirs[:] = kept


def _copy_tree_prepared(
    src_root: str,
    dst_root: str,
    *,
    force_nocow: bool,
    nocow_paths: Sequence[str],
) -> None:
    ensure_dir(dst_root, 0o700)
    if force_nocow:
        _try_set_nocow(dst_root, directory=True)
    dir_modes: list[tuple[str, int]] = []
    for cur_dir, dirs, files in os.walk(src_root, topdown=True, followlinks=False):
        rel_dir, dir_nocow = _walk_rel_nocow(cur_dir, src_root, force_nocow=force_nocow, nocow_paths=nocow_paths)
        target_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)
        ensure_dir(target_dir, 0o700)
        dir_modes.append((target_dir, stat.S_IMODE(os.lstat(cur_dir).st_mode)))
        if dir_nocow:
            _try_set_nocow(target_dir, directory=True)
        _link_symlink_subdirs(cur_dir, target_dir, dirs)
        for name in files:
            rel_file = _rel_posix(rel_dir, name)
            _copy_walk_file(
                os.path.join(cur_dir, name),
                os.path.join(target_dir, name),
                nocow=dir_nocow or _matches_nocow(rel_file, nocow_paths),
            )
    for target_dir, mode in reversed(dir_modes):
        os.chmod(target_dir, mode)


def _ensure_file_nocow(fpath: str) -> None:
    st = os.lstat(fpath)
    if not stat.S_ISREG(st.st_mode):
        return
    try:
        if inode_flags(fpath, directory=False) & FS_NOCOW_FL:
            return
    except OSError:
        return
    if st.st_size == 0:
        _try_set_nocow(fpath, directory=False)
        return
    parent = os.path.dirname(fpath)
    with tempfile.TemporaryDirectory(dir=parent, prefix=".cst-nocow-") as tmpdir:
        tmp_dst = os.path.join(tmpdir, os.path.basename(fpath))
        _copy_file_with_nocow(fpath, tmp_dst, nocow=True)
        os.rename(tmp_dst, fpath)


def _apply_nocow_in_place(root: str, *, force_nocow: bool, nocow_paths: Sequence[str]) -> None:
    if force_nocow:
        _try_set_nocow(root, directory=True)
    for cur_dir, _dirs, files in os.walk(root, followlinks=False):
        rel_dir, dir_nocow = _walk_rel_nocow(cur_dir, root, force_nocow=force_nocow, nocow_paths=nocow_paths)
        if dir_nocow:
            _try_set_nocow(cur_dir, directory=True)
        for name in files:
            if dir_nocow or _matches_nocow(_rel_posix(rel_dir, name), nocow_paths):
                _ensure_file_nocow(os.path.join(cur_dir, name))


def _file_needs_nocow(fpath: str) -> bool:
    if not stat.S_ISREG(os.lstat(fpath).st_mode):
        return False
    try:
        return not (inode_flags(fpath, directory=False) & FS_NOCOW_FL)
    except OSError:
        return False


def _any_missing_nocow(root: str, *, force_nocow: bool, nocow_paths: Sequence[str]) -> bool:
    if not os.path.isdir(root) or os.path.islink(root) or (not force_nocow and not nocow_paths):
        return False
    for cur_dir, _dirs, files in os.walk(root, followlinks=False):
        rel_dir, dir_nocow = _walk_rel_nocow(cur_dir, root, force_nocow=force_nocow, nocow_paths=nocow_paths)
        for name in files:
            need_flag = dir_nocow or _matches_nocow(_rel_posix(rel_dir, name), nocow_paths)
            if need_flag and _file_needs_nocow(os.path.join(cur_dir, name)):
                return True
    return False


def _collect_sqlite_relpaths(root: str, patterns: Sequence[str]) -> list[str]:
    if not patterns:
        return []
    matched: list[str] = []
    for cur_dir, _dirs, files in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(cur_dir, root).replace(os.sep, "/")
        for name in sorted(files):
            rel = _rel_posix(rel_dir, name)
            fpath = os.path.join(cur_dir, name)
            if stat.S_ISREG(os.lstat(fpath).st_mode) and _matches_any(rel, patterns):
                matched.append(rel)
    return matched


def _backup_one_sqlite(src_db: str, dst_db: str) -> None:
    ensure_dir(os.path.dirname(dst_db), 0o700)
    mode = stat.S_IMODE(os.stat(src_db).st_mode)
    parent = os.path.dirname(dst_db)
    with tempfile.TemporaryDirectory(dir=parent, prefix=".cst-sqlite-") as tmpdir:
        tmp_db = os.path.join(tmpdir, os.path.basename(dst_db))
        src_uri = f"{Path(os.path.abspath(src_db)).as_uri()}?mode=ro"
        with (
            contextlib.closing(sqlite3.connect(src_uri, uri=True, timeout=30.0)) as src_conn,
            contextlib.closing(sqlite3.connect(tmp_db, timeout=30.0)) as dst_conn,
        ):
            src_conn.backup(dst_conn)
        os.chmod(tmp_db, mode)
        os.rename(tmp_db, dst_db)


def _rmtree_make_writable(func: Callable[[str], object], target: str, exc: BaseException) -> None:
    if not isinstance(exc, PermissionError):
        raise exc
    parent = os.path.dirname(target)
    with contextlib.suppress(OSError):
        if parent:
            os.chmod(parent, 0o700)
        os.chmod(target, 0o700)
    func(target)


def _rmtree_writable(path: str) -> None:
    try:
        shutil.rmtree(path, onexc=_rmtree_make_writable)
    except OSError:
        if not os.path.isdir(path):
            raise
        shutil.rmtree(path, onexc=_rmtree_make_writable)


def _rel_os(rel_dir: str, name: str) -> str:
    return name if rel_dir == "." else os.path.join(rel_dir, name)


def _prune_mirror_dest(dst_root: str, expected_dirs: set[str], expected_files: set[str]) -> None:
    for cur_dir, dirs, files in os.walk(dst_root, topdown=False, followlinks=False):
        rel_dir = os.path.relpath(cur_dir, dst_root)
        for name in files:
            if _rel_os(rel_dir, name) not in expected_files:
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(cur_dir, name))
        for name in dirs:
            if _rel_os(rel_dir, name) not in expected_dirs:
                with contextlib.suppress(OSError):
                    _rmtree_writable(os.path.join(cur_dir, name))


def _mirror_walk_file(src_f: str, dst_f: str, fs: LocalFileSystemAdapter) -> bool:
    st_mode = os.lstat(src_f).st_mode
    if stat.S_ISLNK(st_mode):
        fs.force_symlink(os.readlink(src_f), dst_f)
        return True
    if stat.S_ISREG(st_mode):
        copy_file_atomic(src_f, dst_f, mode=stat.S_IMODE(st_mode), durable=False)
        return True
    return False


def _mirror_tree_python(src_root: str, dst_root: str, excluded_rels: set[str]) -> None:
    ensure_dir(dst_root, 0o700)
    expected_dirs: set[str] = {"."}
    expected_files: set[str] = set()
    fs = LocalFileSystemAdapter()
    for cur_dir, dirs, files in os.walk(src_root, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(cur_dir, src_root)
        target_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)
        ensure_dir(target_dir, 0o700)
        expected_dirs.add(rel_dir)
        expected_dirs.update(_rel_os(rel_dir, d) for d in dirs)
        for name in files:
            rel_file = _rel_os(rel_dir, name)
            if rel_file.replace(os.sep, "/") in excluded_rels:
                continue
            if _mirror_walk_file(os.path.join(cur_dir, name), os.path.join(target_dir, name), fs):
                expected_files.add(rel_file)
    _prune_mirror_dest(dst_root, expected_dirs, expected_files)


def _pid_matches_targets(proc_root: str, pid: str, targets: tuple[str, ...]) -> bool:
    fd_dir = os.path.join(proc_root, pid, "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return False
    for fd in fds:
        with contextlib.suppress(OSError):
            target = os.readlink(os.path.join(fd_dir, fd))
            if any(target == t or target.startswith(t + "/") for t in targets):
                return True
    return False


def _find_busy_pids(paths: Sequence[str], *, proc_root: str = "/proc") -> tuple[str, ...]:
    targets = tuple(os.path.realpath(p) for p in paths if os.path.lexists(p))
    if not targets or not os.path.isdir(proc_root):
        return ()
    self_pid = str(os.getpid())
    found: list[str] = []
    try:
        proc_entries = os.listdir(proc_root)
    except OSError:
        return ()
    for pid in proc_entries:
        if pid.isdigit() and pid != self_pid and _pid_matches_targets(proc_root, pid, targets):
            comm = read_text(os.path.join(proc_root, pid, "comm"), "").strip() or pid
            found.append(f"{comm}({pid})")
            if len(found) >= _MAX_BUSY_PIDS:
                break
    return tuple(found)


def _parse_findmnt_target(raw: str, target: str) -> tuple[bool, str, str]:
    try:
        doc = parse_json(raw)
    except ValueError:
        return False, "", ""
    if not isinstance(doc, dict):
        return False, "", ""
    filesystems = doc.get("filesystems")
    if not isinstance(filesystems, list):
        return False, "", ""
    for row in filesystems:
        if isinstance(row, dict) and row.get("target") == target:
            raw_fs = row.get("fstype")
            raw_src = row.get("source")
            fstype: str = raw_fs if isinstance(raw_fs, str) else ""
            source: str = raw_src if isinstance(raw_src, str) else ""
            return True, fstype, source
    return False, "", ""


def _stage_copy_and_swap(
    src_dir: str,
    dst_dir: str,
    *,
    prefix: str,
    force_nocow: bool,
    nocow_paths: Sequence[str],
) -> None:
    with tempfile.TemporaryDirectory(
        dir=os.path.dirname(dst_dir),
        prefix=f"{prefix}-{os.path.basename(dst_dir)}-",
    ) as tmp_stage:
        staged_tree = os.path.join(tmp_stage, "data")
        _copy_tree_prepared(
            src_dir,
            staged_tree,
            force_nocow=force_nocow,
            nocow_paths=nocow_paths,
        )
        swap_dir_staged(staged_tree, dst_dir, durable=False)


def _escape_rsync_literal(rel: str) -> str:
    return "".join(f"\\{ch}" if ch in "\\[]*?" else ch for ch in rel)


class LocalOffloadHost:
    """Host adapter for filesystem, NOCOW ioctl, symlinks, sqlite/rsync mirror, and flock."""

    def __init__(
        self,
        *,
        home: str,
        prog: str,
        runner: CommandRunnerPort,
        proc_root: str = "/proc",
    ) -> None:
        self._home = home
        self._prog = prog
        self._runner = runner
        self._proc_root = proc_root
        self._fs = LocalFileSystemAdapter()

    def home_dir(self) -> str:
        return self._home

    def resolve_path(self, path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))

    def resolve_realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def prog_name(self) -> str:
        return self._prog

    def now_epoch(self) -> float:
        return time.time()

    def read_text_opt(self, path: str, limit: int = 65536) -> str | None:
        try:
            with open_regular_text(path, errors="replace", newline=None) as fh:
                return fh.read(limit)
        except OSError:
            return None

    def path_exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def write_text_atomic(self, path: str, content: str, *, mode: int = 0o644) -> None:
        ensure_dir(os.path.dirname(os.path.abspath(path)), 0o755)
        write_atomic(path, content, mode=mode, durable=False, relabel=False)

    def ensure_dir(self, path: str, *, mode: int = 0o700) -> None:
        ensure_dir(path, mode)

    def _capture_cmd(self, argv: Sequence[str], *, timeout: int = _OFFLOAD_FINDMNT_TIMEOUT_S) -> CommandResult:
        return self._runner.run(argv, timeout=timeout, locale_c=True, output=OutputMode.CAPTURE)

    def _is_btrfs_fs(self, path: str) -> bool:
        if not os.path.isdir(path) or os.path.islink(path):
            return False
        res = self._capture_cmd(["stat", "-f", "-c", "%t", path])
        if res.cause is not None or res.returncode != 0:
            return False
        return res.stdout.strip().lower().removeprefix("0x").lstrip("0") == f"{_BTRFS_MAGIC:x}"

    def probe_scratchpad(self, home: str) -> ScratchpadProbe:
        sp = os.path.join(home, "Scratchpad")
        exists = os.path.lexists(sp)
        is_link = os.path.islink(sp)
        is_dir = exists and not is_link and os.path.isdir(sp)
        mounted, fstype, source = (False, "", "")
        if is_dir:
            res = self._capture_cmd(["findmnt", "-J", "-T", sp])
            if res.cause is None and res.returncode == 0:
                mounted, fstype, source = _parse_findmnt_target(res.stdout, os.path.realpath(sp))
        if mounted:
            return ScratchpadProbe(
                path=sp,
                exists=exists,
                is_dir=is_dir,
                is_symlink=is_link,
                mounted=True,
                fstype=fstype,
                mount_source=source,
            )
        return ScratchpadProbe(path=sp, exists=exists, is_dir=is_dir, is_symlink=is_link)

    def probe_entry(self, home: str, entry: OffloadEntry) -> OffloadEntryProbe:
        src = os.path.join(home, entry.source_rel)
        sp = os.path.join(home, entry.scratchpad_rel)
        mirror = os.path.join(home, entry.mirror_rel)
        is_btrfs = self._is_btrfs_fs(sp)
        return OffloadEntryProbe(
            entry=entry,
            source=_probe_path(src),
            scratchpad=_probe_path(sp, inspect_nocow=is_btrfs),
            mirror=_probe_path(mirror),
            busy=_find_busy_pids((src, sp), proc_root=self._proc_root),
            nocow_paths_missing=is_btrfs
            and _any_missing_nocow(
                sp,
                force_nocow=entry.mode == OffloadMode.NOCOW,
                nocow_paths=entry.nocow_paths,
            ),
        )

    def apply_offload_tree(
        self,
        *,
        source_abs: str,
        scratchpad_abs: str,
        mode: OffloadMode,
        nocow_paths: Sequence[str],
    ) -> str | None:
        force_nocow = mode == OffloadMode.NOCOW
        src_is_dir = os.path.lexists(source_abs) and not os.path.islink(source_abs)
        try:
            ensure_dir(os.path.dirname(scratchpad_abs), 0o700)
            if not src_is_dir:
                if not os.path.lexists(scratchpad_abs):
                    ensure_dir(scratchpad_abs, 0o700)
                _apply_nocow_in_place(scratchpad_abs, force_nocow=force_nocow, nocow_paths=nocow_paths)
                self._fs.force_symlink(scratchpad_abs, source_abs)
                return None

            _stage_copy_and_swap(
                source_abs,
                scratchpad_abs,
                prefix=".cst-stage",
                force_nocow=force_nocow,
                nocow_paths=nocow_paths,
            )
            backup_src = f"{source_abs}.cst-pre-offload-{os.urandom(4).hex()}"
            os.rename(source_abs, backup_src)
            try:
                self._fs.force_symlink(scratchpad_abs, source_abs)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.rename(backup_src, source_abs)
                raise
            with contextlib.suppress(OSError):
                _rmtree_writable(backup_src)
        except OSError as err:
            return str(err)
        return None

    def rollback_offload_tree(self, *, source_abs: str, scratchpad_abs: str, was_pending: bool = False) -> str | None:
        if was_pending:
            try:
                if os.path.islink(source_abs):
                    os.unlink(source_abs)
            except OSError as err:
                return str(err)
            return None
        return self.revert_offload_tree(source_abs=source_abs, scratchpad_abs=scratchpad_abs)

    def revert_offload_tree(self, *, source_abs: str, scratchpad_abs: str) -> str | None:
        try:
            if os.path.isdir(scratchpad_abs) and not os.path.islink(scratchpad_abs):
                ensure_dir(os.path.dirname(source_abs), 0o755)
                _stage_copy_and_swap(
                    scratchpad_abs,
                    source_abs,
                    prefix=".cst-restore",
                    force_nocow=False,
                    nocow_paths=(),
                )
                _rmtree_writable(scratchpad_abs)
            elif os.path.islink(source_abs):
                os.unlink(source_abs)
        except OSError as err:
            return str(err)
        return None

    def _run_mirror_sync(self, scratchpad_abs: str, mirror_abs: str, excluded: set[str]) -> str | None:
        rsync_bin = shutil.which("rsync")
        if rsync_bin is None:
            _mirror_tree_python(scratchpad_abs, mirror_abs, excluded)
            return None
        argv = [rsync_bin, "-a", "--delete", "--delete-excluded"]
        for pat in sorted(excluded):
            argv.extend(["--exclude", f"/{_escape_rsync_literal(pat)}"])
        argv.extend([f"{scratchpad_abs.rstrip('/')}/", f"{mirror_abs.rstrip('/')}/"])
        res = self._capture_cmd(argv, timeout=_RSYNC_TIMEOUT_S)
        if res.cause is not None or res.returncode != 0:
            return res.failure("rsync mirror")
        return None

    def backup_offload_tree(
        self,
        entry: OffloadEntry,
        *,
        scratchpad_abs: str,
        mirror_abs: str,
    ) -> tuple[int, str | None]:
        try:
            ensure_dir(mirror_abs, 0o700)
            sqlite_rels = _collect_sqlite_relpaths(scratchpad_abs, entry.sqlite_globs)
            excluded = {f"{rel}{suffix}" for rel in sqlite_rels for suffix in _SIDECAR_SUFFIXES}
            if (sync_err := self._run_mirror_sync(scratchpad_abs, mirror_abs, excluded)) is not None:
                return 0, sync_err
            for rel in sqlite_rels:
                _backup_one_sqlite(os.path.join(scratchpad_abs, rel), os.path.join(mirror_abs, rel))
            return len(sqlite_rels), None
        except (OSError, sqlite3.Error) as err:
            return 0, str(err)

    def remove_tree(self, path: str) -> str | None:
        try:
            if os.path.islink(path):
                os.unlink(path)
            elif os.path.exists(path):
                _rmtree_writable(path)
        except OSError as err:
            return str(err)
        return None

    def acquire_lock(self, path: str) -> int | None:
        try:
            return take_lock(path, wait=False, mode=0o600)
        except OSError:
            return None

    def release_lock(self, fd: int) -> None:
        with contextlib.suppress(OSError):
            os.close(fd)


class OffloadInstallerAdapter:
    """Writes `~/.local/lib/cst/btrfs-cache-offload` and generated user backup units."""

    def __init__(self, entry_file: str) -> None:
        self._entry_file = entry_file

    def install_backup_units(self, home: str, unit_dir: str, on_calendar: str) -> tuple[bool, str | None]:
        if not self._entry_file:
            return False, None
        svc = program_installer(self._entry_file)
        rec = record_file(Owner.USER, home)
        changed = False
        try:
            helper_dest = expand_dest(_HELPER_ENTRY.dest, home)
            payload = install_payload(_HELPER_ENTRY.src, self._entry_file)
            helper_place = Placement.of(_HELPER_ENTRY, path=helper_dest, dest=helper_dest, record=rec)
            changed |= svc.put(helper_place, payload, as_root=False)
            for name, text in (
                (OFFLOAD_SERVICE, render_offload_service()),
                (OFFLOAD_TIMER, render_offload_timer(on_calendar)),
            ):
                upath = os.path.join(unit_dir, name)
                uplace = Placement(
                    path=upath,
                    dest=upath,
                    record=rec,
                    component=_OFFLOAD_COMPONENT,
                    src=None,
                    kind=GeneratedKind.TEXT,
                    owner=Owner.USER,
                    mode=0o644,
                    values={"on_calendar": on_calendar} if name == OFFLOAD_TIMER else None,
                )
                changed |= svc.put(uplace, text.encode("utf-8"), as_root=False)
            svc.mark(rec, _OFFLOAD_COMPONENT)
        except (OSError, WriteFailed) as err:
            return changed, str(err)
        return changed, None

    def forget_backup_units(self, home: str, unit_dir: str) -> str | None:
        rec = record_file(Owner.USER, home)
        svc = program_installer(self._entry_file)
        removed_paths = [
            *(os.path.join(unit_dir, name) for name in (OFFLOAD_TIMER, OFFLOAD_SERVICE)),
            expand_dest(_HELPER_ENTRY.dest, home),
        ]
        try:
            for path in removed_paths:
                if os.path.lexists(path):
                    os.unlink(path)
            svc.forget(rec, removed_paths)
        except OSError as err:
            return str(err)
        return None


class OffloadUserSystemd:
    """`systemctl --user` control for `cst-scratchpad-backup.timer`."""

    def __init__(self, manager_factory: Callable[[], SystemctlAdapter]) -> None:
        self._manager_factory = manager_factory

    def unit_dir(self, home: str) -> str:
        return user_unit_dir(manager_unit_path(self._manager_factory()), home)

    def probe_units(self, home: str) -> tuple[str, str, bool, bool]:
        mgr = self._manager_factory()
        udir = self.unit_dir(home)
        svc_masked = is_masked_unit(os.path.join(udir, OFFLOAD_SERVICE))
        timer_masked = is_masked_unit(os.path.join(udir, OFFLOAD_TIMER))
        active_res = mgr.is_active(UnitName(OFFLOAD_TIMER))
        enabled_res = mgr.is_enabled(UnitName(OFFLOAD_TIMER))
        active = active_res.stdout.strip() if active_res.cause is None else ""
        enabled = enabled_res.stdout.strip() if enabled_res.cause is None else ""
        return active, enabled, svc_masked, timer_masked

    def enable_backup_timer(self) -> str | None:
        res = self._manager_factory().enable(UnitName(OFFLOAD_TIMER), now=True)
        return None if res.cause is None and res.returncode == 0 else res.failure(f"enable {OFFLOAD_TIMER}")

    def disable_backup_timer(self) -> str | None:
        mgr = self._manager_factory()
        res = mgr.disable(UnitName(OFFLOAD_TIMER), now=True)
        if res.cause is not None:
            return res.failure(f"disable {OFFLOAD_TIMER}")
        mgr.clean(UnitName(OFFLOAD_TIMER), what="state")
        return None

    def reload_daemon(self) -> str | None:
        res = self._manager_factory().daemon_reload_result()
        return None if res.cause is None and res.returncode == 0 else res.failure("systemctl --user daemon-reload")


def is_effective_root() -> bool:
    """True when running with effective UID 0."""
    return os.geteuid() == 0


def resolve_home_and_proc(env_map: Mapping[str, str], seam_root: str) -> tuple[str, str]:
    """Resolve user home and proc root honoring test seam overrides."""
    home_raw = env_map.get("HOME") or os.path.expanduser("~")
    home = os.path.join(seam_root, home_raw.lstrip("/")) if seam_root else home_raw
    proc_root = env_map.get("CST_BTRFS_OFFLOAD_PROC") or (os.path.join(seam_root, "proc") if seam_root else "/proc")
    return home, proc_root
