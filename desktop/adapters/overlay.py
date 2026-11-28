# SPDX-License-Identifier: MPL-2.0
"""Overlayfs upper-to-lower merge and crash-recovery snapshot helpers for `cst desktop psd`."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import errno
import json
import os
import shutil
import stat
import time
from typing import Final

from cst_core.adapters.files import (
    LocalFileSystemAdapter,
    copy_file_atomic,
    copy_file_staged,
    ensure_dir,
    open_regular_text,
    swap_dir_staged,
    sync_filesystem,
    write_atomic,
)
from cst_core.domain.errors import Refused
from desktop.domain.psd import (
    PSD_WHITEOUT_FORMAT,
    WHITEOUT_MANIFEST_NAME,
    PsdWhiteoutManifest,
    parse_whiteout_manifest,
)

_OVERLAY_OPAQUE_XATTR: Final = "trusted.overlay.opaque"
_FS_LINKS: Final = LocalFileSystemAdapter()
_REUSE_MTIME_SAFETY_NS: Final = 2_000_000_000
_SINGLE_HARDLINK: Final = 1


@dataclass(frozen=True, slots=True)
class OverlayProbes:
    """Injectable predicates for whiteout and opaque-dir detection (testable without root `mknod`)."""

    is_whiteout: Callable[[str, os.stat_result], bool]
    is_opaque_dir: Callable[[str], bool]


def default_is_whiteout(_path: str, st: os.stat_result) -> bool:
    """True for an overlayfs whiteout node (character device `0:0`)."""
    return stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == 0 and os.minor(st.st_rdev) == 0


def default_is_opaque_dir(path: str) -> bool:
    """True when `trusted.overlay.opaque == b'y'` on `path`."""
    try:
        return os.getxattr(path, _OVERLAY_OPAQUE_XATTR) == b"y"
    except OSError:
        return False


DEFAULT_OVERLAY_PROBES: Final = OverlayProbes(default_is_whiteout, default_is_opaque_dir)


def remove_path(path: str) -> None:
    """Remove a file, symlink, or directory tree cleanly without following symlinks."""
    if os.path.islink(path) or not os.path.isdir(path):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return
    shutil.rmtree(path)


def _copy_entry_atomic(src: str, dst: str, st: os.stat_result, *, uid: int, gid: int) -> None:
    if stat.S_ISLNK(st.st_mode):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        _FS_LINKS.force_symlink(os.readlink(src), dst)
        with contextlib.suppress(OSError):
            os.lchown(dst, uid, gid)
        return
    if stat.S_ISREG(st.st_mode):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        copy_file_atomic(src, dst, mode=stat.S_IMODE(st.st_mode), durable=False, owner=(uid, gid))
        with contextlib.suppress(OSError):
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        return
    remove_path(dst)


def _ensure_user_dir(path: str, *, mode: int, parent_mode: int, owner: tuple[int, int], boundary: str = "") -> None:
    abs_path = os.path.abspath(path)
    created = ensure_dir(abs_path, mode=parent_mode)
    norm_bound = os.path.abspath(boundary).rstrip("/") if boundary else os.path.dirname(abs_path).rstrip("/")
    os.chmod(abs_path, mode)
    for d in created:
        if d == norm_bound or d.startswith(f"{norm_bound}/"):
            with contextlib.suppress(OSError):
                os.chown(d, *owner)
    with contextlib.suppress(OSError):
        os.chown(abs_path, *owner)


def _ensure_lower_dir(dst_dir: str, src_st: os.stat_result, *, uid: int, gid: int) -> None:
    if os.path.lexists(dst_dir) and (os.path.islink(dst_dir) or not os.path.isdir(dst_dir)):
        os.unlink(dst_dir)
    mode = stat.S_IMODE(src_st.st_mode) or 0o700
    _ensure_user_dir(dst_dir, mode=mode, parent_mode=mode, owner=(uid, gid))


def _prune_absent_children(src_dir: str, dst_dir: str, probes: OverlayProbes) -> None:
    if not os.path.isdir(dst_dir):
        return
    present = set()
    with os.scandir(src_dir) as src_it:
        for entry in src_it:
            st = os.lstat(entry.path)
            if not probes.is_whiteout(entry.path, st):
                present.add(entry.name)
    with os.scandir(dst_dir) as dst_it:
        for entry in dst_it:
            if entry.name not in present:
                remove_path(entry.path)


@dataclass(frozen=True, slots=True)
class _WalkTarget:
    uid: int
    gid: int
    probes: OverlayProbes
    recovery_records: tuple[list[str], list[str]] | None = None
    staging_dev: int | None = None
    snap_start_ns: int = 0


def _can_reuse_prev_file(src_st: os.stat_result, prev_st: os.stat_result, target: _WalkTarget) -> bool:
    # MAP_SHARED mmaps on tmpfs (SQLite -shm, BrowserMetrics .pma) may defer mtime updates until msync/munmap;
    # SQLite ignores -shm on crash recovery and .pma files are telemetry, while durable DB/prefs use write/rename.
    return (
        stat.S_ISREG(prev_st.st_mode)
        and prev_st.st_dev == target.staging_dev
        and prev_st.st_nlink == _SINGLE_HARDLINK
        and prev_st.st_size == src_st.st_size
        and prev_st.st_mtime_ns == src_st.st_mtime_ns
        and stat.S_IMODE(prev_st.st_mode) == stat.S_IMODE(src_st.st_mode)
        and (os.geteuid() != 0 or (prev_st.st_uid == target.uid and prev_st.st_gid == target.gid))
        and prev_st.st_ctime_ns - src_st.st_mtime_ns >= _REUSE_MTIME_SAFETY_NS
        and target.snap_start_ns - src_st.st_mtime_ns >= _REUSE_MTIME_SAFETY_NS
    )


def _next_prev_dir(prev_dir: str | None, name: str, staging_dev: int | None) -> str | None:
    if prev_dir is None or staging_dev is None:
        return None
    cand = os.path.join(prev_dir, name)
    try:
        pst = os.lstat(cand)
    except OSError:
        return None
    if not stat.S_ISDIR(pst.st_mode) or pst.st_dev != staging_dev:
        return None
    return cand


def _stage_entry(src: str, dst: str, st: os.stat_result, target: _WalkTarget, prev_path: str | None) -> None:
    if stat.S_ISLNK(st.st_mode):
        _FS_LINKS.force_symlink(os.readlink(src), dst)
        with contextlib.suppress(OSError):
            os.lchown(dst, target.uid, target.gid)
        return
    if not stat.S_ISREG(st.st_mode):
        return
    if prev_path is not None:
        try:
            prev_st = os.lstat(prev_path)
        except OSError:
            prev_st = None
        if prev_st is not None and _can_reuse_prev_file(st, prev_st, target):
            try:
                os.link(prev_path, dst, follow_symlinks=False)
                return
            except OSError:
                pass
    copy_file_staged(src, dst, mode=stat.S_IMODE(st.st_mode), owner=(target.uid, target.gid))


def _handle_walk_entry(
    entry: os.DirEntry[str],
    dst: str,
    rel: str,
    target: _WalkTarget,
    prev_dir: str | None = None,
) -> None:
    tolerate_missing = target.recovery_records is not None
    try:
        st = os.lstat(entry.path)
    except FileNotFoundError:
        if tolerate_missing:
            return
        raise
    if target.probes.is_whiteout(entry.path, st):
        if target.recovery_records is None:
            remove_path(dst)
        else:
            target.recovery_records[0].append(rel)
    elif stat.S_ISDIR(st.st_mode):
        _ensure_lower_dir(dst, st, uid=target.uid, gid=target.gid)
        next_prev = _next_prev_dir(prev_dir, entry.name, target.staging_dev) if tolerate_missing else None
        _walk_upper_delta(entry.path, dst, target, rel, next_prev)
    elif target.recovery_records is None:
        _copy_entry_atomic(entry.path, dst, st, uid=target.uid, gid=target.gid)
    else:
        prev_path = os.path.join(prev_dir, entry.name) if prev_dir is not None else None
        try:
            _stage_entry(entry.path, dst, st, target, prev_path)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.EINVAL, errno.ELOOP):
                return
            raise


def _walk_upper_delta(
    src_dir: str,
    dst_dir: str,
    target: _WalkTarget,
    rel_prefix: str = "",
    prev_dir: str | None = None,
) -> None:
    if target.probes.is_opaque_dir(src_dir):
        if target.recovery_records is None:
            _prune_absent_children(src_dir, dst_dir, target.probes)
        else:
            target.recovery_records[1].append(rel_prefix)
    try:
        with os.scandir(src_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except FileNotFoundError:
        if target.recovery_records is not None:
            return
        raise
    for entry in entries:
        dst = os.path.join(dst_dir, entry.name)
        rel = os.path.join(rel_prefix, entry.name) if rel_prefix else entry.name
        _handle_walk_entry(entry, dst, rel, target, prev_dir)


def merge_upper_to_lower(
    upper: str, lower: str, *, uid: int, gid: int, probes: OverlayProbes = DEFAULT_OVERLAY_PROBES
) -> None:
    """Merge overlayfs `upper` delta (files, symlinks, dirs, whiteouts, opaque dirs) atomically into `lower`."""
    if os.path.isdir(upper):
        _walk_upper_delta(upper, lower, _WalkTarget(uid=uid, gid=gid, probes=probes))
        sync_filesystem(lower)


def has_valid_recovery(recovery_dir: str) -> bool:
    """Return True when `recovery_dir` is a real directory carrying a valid whiteout manifest."""
    if os.path.islink(recovery_dir) or not os.path.isdir(recovery_dir):
        return False
    manifest_path = os.path.join(recovery_dir, WHITEOUT_MANIFEST_NAME)
    try:
        with open_regular_text(manifest_path, errors="strict", newline=None) as fh:
            parse_whiteout_manifest(fh.read())
    except OSError, ValueError:
        return False
    return True


def _validated_prev_recovery(recovery_dir: str, staging_dev: int) -> str | None:
    try:
        st = os.lstat(recovery_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_dev != staging_dev:
        return None
    return recovery_dir if has_valid_recovery(recovery_dir) else None


def snapshot_upper_to_recovery(
    upper: str,
    recovery_dir: str,
    *,
    owner: tuple[int, int],
    home_boundary: str = "",
    probes: OverlayProbes = DEFAULT_OVERLAY_PROBES,
) -> None:
    """Copy `upper` + whiteout manifest into `recovery_dir` atomically via staging, reusing unchanged files."""
    if not os.path.isdir(upper):
        if os.path.lexists(recovery_dir):
            remove_path(recovery_dir)
        return
    uid, gid = owner
    snap_start_ns = time.time_ns()
    staging_dir = f"{os.path.abspath(recovery_dir)}.staging"
    if os.path.lexists(staging_dir):
        remove_path(staging_dir)
    try:
        _ensure_user_dir(
            staging_dir,
            mode=0o700,
            parent_mode=0o755,
            owner=owner,
            boundary=home_boundary or os.path.dirname(os.path.abspath(recovery_dir)),
        )
        staging_dev = os.lstat(staging_dir).st_dev
        prev_root = _validated_prev_recovery(recovery_dir, staging_dev)
        whiteouts: list[str] = []
        opaque_dirs: list[str] = []
        _walk_upper_delta(
            upper,
            staging_dir,
            _WalkTarget(
                uid=uid,
                gid=gid,
                probes=probes,
                recovery_records=(whiteouts, opaque_dirs),
                staging_dev=staging_dev,
                snap_start_ns=snap_start_ns,
            ),
            prev_dir=prev_root,
        )
        manifest: PsdWhiteoutManifest = {
            "format": PSD_WHITEOUT_FORMAT,
            "whiteouts": sorted(whiteouts),
            "opaque_dirs": sorted(opaque_dirs),
        }
        write_atomic(
            os.path.join(staging_dir, WHITEOUT_MANIFEST_NAME),
            json.dumps(manifest, indent=2) + "\n",
            mode=0o600,
            durable=False,
            owner=owner,
            relabel=False,
        )
        sync_filesystem(staging_dir)
        swap_dir_staged(staging_dir, recovery_dir, durable=True)
    except BaseException:
        if os.path.lexists(staging_dir):
            remove_path(staging_dir)
        raise


def apply_recovery_to_lower(recovery_dir: str, lower: str, *, uid: int, gid: int) -> None:
    """Replay a crash-recovery snapshot (files + whiteouts/opaque manifests) cleanly onto `lower`."""
    if not os.path.lexists(recovery_dir):
        return
    if os.path.islink(recovery_dir) or not os.path.isdir(recovery_dir):
        raise Refused(f"invalid crash-recovery snapshot at {recovery_dir} (not a directory); inspect or pass --discard")
    manifest_path = os.path.join(recovery_dir, WHITEOUT_MANIFEST_NAME)
    try:
        with open_regular_text(manifest_path, errors="strict", newline=None) as fh:
            manifest = parse_whiteout_manifest(fh.read())
    except OSError:
        raise Refused(
            f"incomplete crash-recovery snapshot in {recovery_dir} (missing or unreadable {WHITEOUT_MANIFEST_NAME}); "
            "inspect or pass --discard"
        ) from None
    except ValueError as exc:
        raise Refused(f"invalid recovery manifest in {recovery_dir}: {exc}") from None

    opaque_set = set(manifest["opaque_dirs"])

    def _walk_copy(src_dir: str, dst_dir: str, rel_prefix: str) -> None:
        with os.scandir(src_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
        if rel_prefix in opaque_set and os.path.isdir(dst_dir):
            present = {e.name for e in entries if rel_prefix or e.name != WHITEOUT_MANIFEST_NAME}
            with os.scandir(dst_dir) as dst_it:
                for dst_entry in dst_it:
                    if dst_entry.name not in present:
                        remove_path(dst_entry.path)
        for entry in entries:
            if not rel_prefix and entry.name == WHITEOUT_MANIFEST_NAME:
                continue
            rel = os.path.join(rel_prefix, entry.name) if rel_prefix else entry.name
            dst = os.path.join(dst_dir, entry.name)
            st = os.lstat(entry.path)
            if stat.S_ISDIR(st.st_mode):
                _ensure_lower_dir(dst, st, uid=uid, gid=gid)
                _walk_copy(entry.path, dst, rel)
            else:
                _copy_entry_atomic(entry.path, dst, st, uid=uid, gid=gid)

    _walk_copy(recovery_dir, lower, "")
    for rel in manifest["whiteouts"]:
        remove_path(os.path.join(lower, rel))
    sync_filesystem(lower)


def dir_size_bytes(path: str) -> int:
    """Compute allocated size in bytes under `path` without crossing mount boundaries."""
    if not os.path.isdir(path):
        return 0
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                    elif stat.S_ISREG(st.st_mode):
                        total += st.st_size
        except OSError:
            continue
    return total
