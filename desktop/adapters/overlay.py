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
from typing import Final

from cst_core.adapters.files import (
    LocalFileSystemAdapter,
    copy_file_atomic,
    ensure_dir,
    swap_dir_staged,
    write_atomic,
)
from cst_core.domain.errors import Refused
from desktop.domain.psd import PSD_WHITEOUT_FORMAT, PsdWhiteoutManifest, parse_whiteout_manifest

WHITEOUT_MANIFEST_NAME: Final = ".cst-psd-whiteouts.json"
_OVERLAY_OPAQUE_XATTR: Final = "trusted.overlay.opaque"
_FS_LINKS: Final = LocalFileSystemAdapter()


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
        copy_file_atomic(src, dst, mode=stat.S_IMODE(st.st_mode), durable=True, owner=(uid, gid))
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


def _handle_walk_entry(entry: os.DirEntry[str], dst: str, rel: str, target: _WalkTarget) -> None:
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
        _walk_upper_delta(entry.path, dst, target, rel)
    else:
        try:
            _copy_entry_atomic(entry.path, dst, st, uid=target.uid, gid=target.gid)
        except OSError as exc:
            if tolerate_missing and exc.errno in (errno.ENOENT, errno.EINVAL):
                return
            raise


def _walk_upper_delta(src_dir: str, dst_dir: str, target: _WalkTarget, rel_prefix: str = "") -> None:
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
        _handle_walk_entry(entry, dst, rel, target)


def merge_upper_to_lower(
    upper: str, lower: str, *, uid: int, gid: int, probes: OverlayProbes = DEFAULT_OVERLAY_PROBES
) -> None:
    """Merge overlayfs `upper` delta (files, symlinks, dirs, whiteouts, opaque dirs) atomically into `lower`."""
    if os.path.isdir(upper):
        _walk_upper_delta(upper, lower, _WalkTarget(uid=uid, gid=gid, probes=probes))


def has_valid_recovery(recovery_dir: str) -> bool:
    """Return True when `recovery_dir` exists and carries a whiteout manifest."""
    return os.path.isfile(os.path.join(recovery_dir, WHITEOUT_MANIFEST_NAME))


def snapshot_upper_to_recovery(
    upper: str,
    recovery_dir: str,
    *,
    owner: tuple[int, int],
    home_boundary: str = "",
    probes: OverlayProbes = DEFAULT_OVERLAY_PROBES,
) -> None:
    """Copy `upper` + whiteout manifest into `recovery_dir` atomically via staging (for busy periodic sync)."""
    if not os.path.isdir(upper):
        if os.path.lexists(recovery_dir):
            remove_path(recovery_dir)
        return
    uid, gid = owner
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
        whiteouts: list[str] = []
        opaque_dirs: list[str] = []
        _walk_upper_delta(
            upper, staging_dir, _WalkTarget(uid=uid, gid=gid, probes=probes, recovery_records=(whiteouts, opaque_dirs))
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
            durable=True,
            owner=owner,
            relabel=False,
        )
        swap_dir_staged(staging_dir, recovery_dir, durable=True)
    except BaseException:
        if os.path.lexists(staging_dir):
            remove_path(staging_dir)
        raise


def apply_recovery_to_lower(recovery_dir: str, lower: str, *, uid: int, gid: int) -> None:
    """Replay a crash-recovery snapshot (files + whiteouts/opaque manifests) cleanly onto `lower`."""
    if not os.path.isdir(recovery_dir):
        return
    manifest_path = os.path.join(recovery_dir, WHITEOUT_MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise Refused(
            f"incomplete crash-recovery snapshot in {recovery_dir} (missing {WHITEOUT_MANIFEST_NAME}); "
            "inspect or pass --discard"
        )
    with open(manifest_path, encoding="utf-8") as fh:
        try:
            manifest = parse_whiteout_manifest(fh.read())
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
