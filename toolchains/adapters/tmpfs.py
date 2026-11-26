# SPDX-License-Identifier: MPL-2.0
"""Host adapters for `cst toolchains tmpfs` and shared active-tmpfs detection."""

from __future__ import annotations

import contextlib
import dataclasses
import grp
import os
import posixpath
import pwd
import stat
import tomllib
from typing import TYPE_CHECKING, Final

from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import (
    LocalFileSystemAdapter,
    ensure_dir,
    owned_file_state_at,
    read_text,
    unit_file_state_at,
)
from cst_core.adapters.install import program_installer
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.application.install import Placement, record_file
from cst_core.domain.contracts import CARGO_BUILD_TMPFS_MARKER, CARGO_BUILD_TMPFS_REL, CARGO_CONFIG_REL
from cst_core.domain.errors import Refused
from cst_core.domain.files import UnitFileState, WholeFileState
from cst_core.domain.install import Component, GeneratedKind, Owner
from cst_core.domain.shapes import json_object, parse_toml
from cst_core.domain.units import SYSTEM_UNIT_DIR
from cst_core.domain.values import ROOT_ACCOUNT
from cst_core.ports import UnitName
from toolchains.domain.build import expected_tmpfs_template
from toolchains.domain.tmpfs import (
    TMPFS_FSTYPE,
    TmpfsMountInfo,
    TmpfsSnapshot,
    TmpfsTargetUser,
    find_mount_in_proc_mounts,
    mem_total_from_meminfo,
    render_tmpfs_mount_unit,
    render_tmpfs_tmpfiles,
    tmpfiles_path_for,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cst_core.application.install import InstallService
    from cst_core.ports import CommandRunnerPort, SystemdPort


_SYSTEMD_TIMEOUT_S: Final = 30
_MOUNT_TIMEOUT_S: Final = 30
_MEMINFO_PATH: Final = "/proc/meminfo"
_PROC_MOUNTS_PATH: Final = "/proc/mounts"


def is_active_tmpfs_mount(
    path: str,
    *,
    require_uid: int | None = None,
    proc_mounts_path: str = _PROC_MOUNTS_PATH,
) -> int | None:
    """Total capacity in bytes when PATH is an active tmpfs mountpoint, else None."""
    mounts_text = read_text(proc_mounts_path)
    if mounts_text is None or not mounts_text:
        return None
    canonical = os.path.realpath(path)
    entry = find_mount_in_proc_mounts(mounts_text, canonical)
    if entry is None or entry[0] != TMPFS_FSTYPE:
        return None
    try:
        st = os.lstat(canonical)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return None
        if require_uid is not None and st.st_uid != require_uid:
            return None
        vfs = os.statvfs(canonical)
    except OSError:
        return None
    return vfs.f_blocks * vfs.f_frsize


def _resolve_tmpfs_user(requested: str | None) -> TmpfsTargetUser:
    """Resolve account metadata from REQUESTED or SUDO_USER/USER, refusing root."""
    name = (requested or os.environ.get("SUDO_USER") or os.environ.get("USER") or "").strip()
    if not name or name == ROOT_ACCOUNT:
        raise Refused("run as a non-root user (or pass --user NAME when running as root)")
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        raise Refused(f"user {name!r} does not exist") from None
    if pw.pw_uid == 0:
        raise Refused("refusing root account (cargo build-dir tmpfs belongs to a non-root user)")
    try:
        group = grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        group = str(pw.pw_gid)
    return TmpfsTargetUser.from_home(
        user=pw.pw_name,
        group=group,
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        real_home=os.path.realpath(pw.pw_dir),
    )


def _cargo_points_to_tmpfs(home: str, real_home: str) -> bool:
    """True when ~/.cargo/config.toml sets [build] build-dir to CST's tmpfs template."""
    cfg_path = os.path.join(real_home, CARGO_CONFIG_REL)
    try:
        with open(cfg_path, "rb") as fh:
            data = parse_toml(fh.read())
    except OSError, tomllib.TOMLDecodeError:
        return False
    build = data.get("build") if json_object(data) else None
    val = build.get("build-dir") if isinstance(build, dict) else None
    candidates = {expected_tmpfs_template(real_home), expected_tmpfs_template(posixpath.normpath(home))}
    return isinstance(val, str) and val in candidates


def _marker_in_bytes(data: bytes) -> bool:
    return CARGO_BUILD_TMPFS_MARKER.encode("utf-8") in data


@dataclasses.dataclass(frozen=True, slots=True)
class TmpfsTestSeams:
    """Optional test overrides for TmpfsHost (None: real system paths and adapters)."""

    systemd: SystemdPort | None = None
    runner: CommandRunnerPort | None = None
    unit_dir: str = SYSTEM_UNIT_DIR
    proc_meminfo_path: str = _MEMINFO_PATH
    proc_mounts_path: str = _PROC_MOUNTS_PATH
    installer: InstallService | None = None


class TmpfsHost:
    """TmpfsHostPort over local systemd, `/proc`, and CST's root install record."""

    def __init__(self, entry_file: str, *, seams: TmpfsTestSeams | None = None) -> None:
        opts = TmpfsTestSeams() if seams is None else seams
        self._entry_file = entry_file
        self._runner = SubprocessCommandRunner() if opts.runner is None else opts.runner
        self._systemd = (
            SystemctlAdapter(self._runner, timeout=_SYSTEMD_TIMEOUT_S, locale_c=True, on_host=SystemctlHost.LOCAL)
            if opts.systemd is None
            else opts.systemd
        )
        self._fs = LocalFileSystemAdapter()
        self._unit_dir = opts.unit_dir
        self._meminfo_path = opts.proc_meminfo_path
        self._mounts_path = opts.proc_mounts_path
        self._installer = opts.installer

    def _inst(self) -> InstallService:
        if self._installer is None:
            self._installer = program_installer(self._entry_file)
        return self._installer

    def snapshot(self, user: str | None, *, size_bytes: int | None) -> TmpfsSnapshot:
        """Inspect host and target-user state cleanly (no mutations)."""
        target = _resolve_tmpfs_user(user)
        unit_name = str(self._systemd.escape_path(target.mountpoint, suffix="mount"))
        unit_path = posixpath.join(self._unit_dir, unit_name)
        tmpfiles_path = tmpfiles_path_for(target.user)
        wanted_unit = render_tmpfs_mount_unit(target, size_bytes).encode("utf-8") if size_bytes is not None else None
        wanted_tf = render_tmpfs_tmpfiles(target).encode("utf-8")
        unit_state: UnitFileState = unit_file_state_at(unit_path, wanted_unit, _marker_in_bytes, owner=Owner.ROOT)
        tf_state: WholeFileState = owned_file_state_at(tmpfiles_path, wanted_tf, _marker_in_bytes, owner=Owner.ROOT)
        unit_uid = UnitName(unit_name)
        act_res = self._systemd.is_active(unit_uid)
        en_res = self._systemd.is_enabled(unit_uid)
        mem_text = read_text(self._meminfo_path, "") or ""
        mounts_text = read_text(self._mounts_path, "") or ""
        mount_entry = find_mount_in_proc_mounts(mounts_text, target.mountpoint)
        mount_info: TmpfsMountInfo | None = None
        if mount_entry is not None:
            cap = is_active_tmpfs_mount(target.mountpoint, proc_mounts_path=self._mounts_path)
            mount_info = TmpfsMountInfo(fstype=mount_entry[0], options=mount_entry[1], capacity_bytes=cap)
        raw_home = target.home
        with contextlib.suppress(KeyError):
            raw_home = pwd.getpwnam(target.user).pw_dir
        return TmpfsSnapshot(
            target=target,
            unit_name=unit_name,
            unit_path=unit_path,
            tmpfiles_path=tmpfiles_path,
            unit_state=unit_state,
            tmpfiles_state=tf_state,
            unit_active=act_res.stdout.strip() if act_res.cause is None else "",
            unit_enabled=en_res.stdout.strip() if en_res.cause is None else "",
            mount=mount_info,
            mem_total_bytes=mem_total_from_meminfo(mem_text),
            cargo_uses_tmpfs=_cargo_points_to_tmpfs(raw_home, target.home),
        )

    def _ensure_safe_dir(self, path: str, mode: int, *, uid: int, gid: int) -> list[str]:
        if os.path.realpath(path) != path:
            raise Refused(f"{path} goes through a symlink ({os.path.realpath(path)}): refused")
        if os.path.lexists(path):
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise Refused(f"{path} is a symlink or not a folder: refused")
        try:
            created = ensure_dir(path, mode, relabel=True)
        except OSError as e:
            raise Refused(f"cannot create {path}: {e.strerror or e}") from None
        for d in created:
            os.chown(d, uid, gid)
        return created

    def ensure_mountpoint(self, target: TmpfsTargetUser) -> None:
        """Create cache parent and mountpoint with 0700 ownership for TARGET."""
        self._ensure_safe_dir(target.cache_parent, 0o755, uid=target.uid, gid=target.gid)
        created = self._ensure_safe_dir(target.mountpoint, 0o700, uid=target.uid, gid=target.gid)
        # Only set ownership/mode when creating or on unmounted directory: when mounted, chmod/chown
        # affects the tmpfs root, which remount or tmpfs options manage directly.
        if created or os.path.ismount(target.mountpoint) is False:
            os.chmod(target.mountpoint, 0o700)
            os.chown(target.mountpoint, target.uid, target.gid)

    def put_managed_file(self, path: str, text: str) -> bool:
        """Write root-owned generated file via installer and record it under toolchains:tmpfs."""
        place = Placement(
            path=path,
            dest=path,
            record=record_file(Owner.ROOT, None),
            component=Component.TOOLCHAINS_TMPFS,
            src=None,
            kind=GeneratedKind.TEXT,
            owner=Owner.ROOT,
            mode=0o644,
        )
        return self._inst().put(place, text.encode("utf-8"), as_root=True)

    def mark_installed(self) -> None:
        self._inst().mark(record_file(Owner.ROOT, None), Component.TOOLCHAINS_TMPFS)

    def forget_managed_files(self, paths: Sequence[str]) -> None:
        self._inst().forget(record_file(Owner.ROOT, None), paths)

    def remove_file(self, path: str) -> bool:
        return self._fs.remove_file(path)

    def remove_empty_dir(self, path: str) -> None:
        with contextlib.suppress(OSError):
            os.rmdir(path)

    def daemon_reload(self) -> None:
        res = self._systemd.daemon_reload_result()
        if res.cause is not None or res.returncode != 0:
            raise Refused(res.failure("systemctl daemon-reload"))

    def apply_tmpfiles(self, path: str) -> None:
        res = self._runner.run(["systemd-tmpfiles", "--create", path], timeout=_MOUNT_TIMEOUT_S, locale_c=True)
        if res.cause is not None or res.returncode != 0:
            raise Refused(res.failure(f"systemd-tmpfiles --create {path}"))

    def enable_now(self, unit: UnitName) -> None:
        res = self._systemd.enable(unit, now=True)
        if res.cause is not None or res.returncode != 0:
            raise Refused(res.failure(f"systemctl enable --now {unit}"))

    def enable_unit(self, unit: UnitName) -> None:
        res = self._systemd.enable(unit, now=False)
        if res.cause is not None or res.returncode != 0:
            raise Refused(res.failure(f"systemctl enable {unit}"))

    def remount_tmpfs(self, mountpoint: str, options: str) -> None:
        res = self._runner.run(
            ["mount", "-o", f"remount,{options}", mountpoint],
            timeout=_MOUNT_TIMEOUT_S,
            locale_c=True,
        )
        if res.cause is not None or res.returncode != 0:
            raise Refused(res.failure(f"mount -o remount {mountpoint}"))

    def disable_now(self, unit: UnitName) -> None:
        res = self._systemd.disable(unit, now=True)
        if res.cause is not None or res.returncode != 0:
            raise Refused(
                f"{res.failure(f'systemctl disable --now {unit}')} "
                f"(stop any cargo/rustc process using {CARGO_BUILD_TMPFS_REL} and retry)"
            )
