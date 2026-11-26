# SPDX-License-Identifier: MPL-2.0
"""Use case for `cst toolchains tmpfs status|plan|apply|revert`."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from cst_core.domain.errors import Refused, Usage
from cst_core.domain.files import OwnedState
from cst_core.domain.text import DRY_RUN_LINE
from cst_core.domain.units import ActiveState, EnablementState, masked_refusal
from cst_core.ports import UnitName
from toolchains.domain.common import gib, parse_size
from toolchains.domain.tmpfs import (
    TMPFS_FSTYPE,
    TmpfsAction,
    TmpfsSnapshot,
    format_tmpfs_size_opt,
    mount_matches_desired,
    recommended_tmpfs_bytes,
    render_tmpfs_mount_unit,
    render_tmpfs_tmpfiles,
    tmpfs_mount_options,
    validate_tmpfs_size,
)

if TYPE_CHECKING:
    from toolchains.ports import RunOutputPort, TmpfsHostPort


def _resolve_size(mem_total_bytes: int | None, parsed_size: int | None) -> int:
    if parsed_size is not None:
        return validate_tmpfs_size(parsed_size, mem_total_bytes)
    return recommended_tmpfs_bytes(mem_total_bytes)


def _check_conflicts(snap: TmpfsSnapshot) -> None:
    if snap.unit_state == OwnedState.MASKED:
        why = masked_refusal([snap.unit_path], user=False)
        raise Refused((why or f"{snap.unit_name} is masked") + "; nothing was written")
    conflicts = [
        p
        for p, st in ((snap.unit_path, snap.unit_state), (snap.tmpfiles_path, snap.tmpfiles_state))
        if st == OwnedState.CONFLICT
    ]
    if conflicts:
        raise Refused("not written by CST, left alone: " + ", ".join(conflicts))


@dataclasses.dataclass(frozen=True)
class TmpfsManager:
    """Manage CST's dedicated per-user tmpfs mount for cargo's `build-dir`."""

    host: TmpfsHostPort
    out: RunOutputPort
    prog: str

    def run(self, action: TmpfsAction, *, user: str | None = None, size_text: str | None = None) -> int:
        """Dispatch `tmpfs` status, plan, apply, or revert."""
        if size_text is not None and action in (TmpfsAction.STATUS, TmpfsAction.REVERT):
            raise Usage(f"tmpfs {action}: --size only applies to plan or apply")
        explicit = parse_size(size_text) if size_text is not None else None
        if action == TmpfsAction.STATUS:
            return self._status(user)
        if action == TmpfsAction.PLAN:
            return self._plan(user, explicit)
        if action == TmpfsAction.APPLY:
            return self._apply(user, explicit)
        return self._revert(user)

    def _status(self, user: str | None) -> int:
        say = self.out.say
        probe = self.host.snapshot(user, size_bytes=None)
        rec_bytes: int | None = None
        if probe.mem_total_bytes is not None:
            try:
                rec_bytes = recommended_tmpfs_bytes(probe.mem_total_bytes)
            except Refused:
                rec_bytes = None
        snap = self.host.snapshot(user, size_bytes=rec_bytes)
        say(f"user:       {snap.target.user} (uid {snap.target.uid}, gid {snap.target.gid})")
        say(f"mountpoint: {snap.target.mountpoint}")
        if snap.mount is not None and snap.mount.fstype == TMPFS_FSTYPE:
            cap = snap.mount.options.get("size") or (
                gib(snap.mount.capacity_bytes) if snap.mount.capacity_bytes is not None else "unknown"
            )
            say(f"mount:      mounted (tmpfs, cap {cap}, uid={snap.mount.options.get('uid', '?')})")
        elif snap.mount is not None:
            say(f"mount:      other filesystem ({snap.mount.fstype})")
        else:
            say("mount:      not mounted")
        say(
            f"unit:       {snap.unit_path} ({snap.unit_state}; active={snap.unit_active or 'unknown'}, "
            f"enabled={snap.unit_enabled or 'unknown'})"
        )
        say(f"tmpfiles:   {snap.tmpfiles_path} ({snap.tmpfiles_state})")
        if rec_bytes is not None and snap.mem_total_bytes is not None:
            say(f"recommended cap: {gib(rec_bytes)} (min(16 GiB, 20% of {gib(snap.mem_total_bytes)} RAM))")
        say(
            "cargo:      uses tmpfs build-dir"
            if snap.cargo_uses_tmpfs
            else f"cargo:      does not point to cargo-build-tmpfs (run `{self.prog} config apply` to switch)"
        )
        return 0

    def _plan(self, user: str | None, explicit: int | None) -> int:
        say = self.out.say
        probe = self.host.snapshot(user, size_bytes=None)
        target_bytes = _resolve_size(probe.mem_total_bytes, explicit)
        snap = self.host.snapshot(user, size_bytes=target_bytes)
        _check_conflicts(snap)
        mounted_ok = mount_matches_desired(snap.mount, snap.target, target_bytes)
        say(DRY_RUN_LINE)
        say(f"user:       {snap.target.user} -> {snap.target.mountpoint}")
        say(f"size cap:   {gib(target_bytes)} (tmpfs in RAM; isolated from /tmp)")
        say(f"unit:       {'unchanged' if snap.unit_state == OwnedState.OK else 'would write'}: {snap.unit_path}")
        say(
            f"tmpfiles:   {'unchanged' if snap.tmpfiles_state == OwnedState.OK else 'would write'}: {snap.tmpfiles_path}"
        )
        if snap.mount is not None and snap.mount.fstype != TMPFS_FSTYPE:
            raise Refused(f"{snap.target.mountpoint} is already mounted as {snap.mount.fstype}, not tmpfs")
        if mounted_ok and snap.unit_enabled == EnablementState.ENABLED:
            say("mount:      unchanged (mounted tmpfs and enabled unit already match)")
        elif snap.mount is not None:
            say(f"mount:      would live-remount {snap.target.mountpoint} with size={gib(target_bytes)}")
        else:
            say(f"mount:      would mount tmpfs and enable {snap.unit_name}")
        size_flag = f" --size {format_tmpfs_size_opt(explicit)}" if explicit is not None else ""
        say(f"Apply with: {self.prog} tmpfs apply --user {snap.target.user}{size_flag}")
        if not snap.cargo_uses_tmpfs:
            say(f"Then run:   {self.prog} config apply (without sudo) to point cargo at tmpfs")
        return 0

    def _apply(self, user: str | None, explicit: int | None) -> int:
        say = self.out.say
        probe = self.host.snapshot(user, size_bytes=None)
        target_bytes = _resolve_size(probe.mem_total_bytes, explicit)
        snap = self.host.snapshot(user, size_bytes=target_bytes)
        _check_conflicts(snap)
        if snap.mount is not None and snap.mount.fstype != TMPFS_FSTYPE:
            raise Refused(f"{snap.target.mountpoint} is mounted as {snap.mount.fstype}, not tmpfs: refused")
        already_mounted = mount_matches_desired(snap.mount, snap.target, target_bytes)
        if (
            snap.unit_state == OwnedState.OK
            and snap.tmpfiles_state == OwnedState.OK
            and already_mounted
            and snap.unit_enabled == EnablementState.ENABLED
        ):
            say(f"unchanged: {snap.unit_path}")
            say(f"unchanged: {snap.tmpfiles_path}")
            say(f"unchanged: {snap.target.mountpoint} (mounted tmpfs {gib(target_bytes)}, {snap.unit_name} enabled)")
            self.host.mark_installed()
            return 0
        base_values = {
            "user": snap.target.user,
            "group": snap.target.group,
            "uid": str(snap.target.uid),
            "gid": str(snap.target.gid),
            "home": snap.target.home,
        }
        self.host.ensure_mountpoint(snap.target)
        unit_changed = self.host.put_managed_file(
            snap.unit_path,
            render_tmpfs_mount_unit(snap.target, target_bytes),
            {**base_values, "size_bytes": str(target_bytes)},
        )
        say(f"{'written:  ' if unit_changed else 'unchanged:'} {snap.unit_path}")
        tf_changed = self.host.put_managed_file(snap.tmpfiles_path, render_tmpfs_tmpfiles(snap.target), base_values)
        say(f"{'written:  ' if tf_changed else 'unchanged:'} {snap.tmpfiles_path}")
        self.host.daemon_reload()
        self.host.apply_tmpfiles(snap.tmpfiles_path)
        if snap.mount is not None:
            if not already_mounted:
                self.host.remount_tmpfs(snap.target.mountpoint, tmpfs_mount_options(snap.target, target_bytes))
            self.host.enable_unit(UnitName(snap.unit_name))
        else:
            self.host.enable_now(UnitName(snap.unit_name))
        self.host.mark_installed()
        say(f"mounted:   {snap.target.mountpoint} (tmpfs {gib(target_bytes)}, {snap.unit_name} enabled)")
        if not snap.cargo_uses_tmpfs:
            say(f"Next: run `{self.prog} config apply` (without sudo) to point cargo build-dir at tmpfs")
        return 0

    def _revert(self, user: str | None) -> int:
        say = self.out.say
        snap = self.host.snapshot(user, size_bytes=None)
        conflicts = [
            p
            for p, st in ((snap.unit_path, snap.unit_state), (snap.tmpfiles_path, snap.tmpfiles_state))
            if st == OwnedState.CONFLICT
        ]
        if conflicts:
            raise Refused("not written by CST, left alone: " + ", ".join(conflicts))
        mounted_tmpfs = snap.mount is not None and snap.mount.fstype == TMPFS_FSTYPE
        unit_present = snap.unit_state != OwnedState.MISSING
        tf_present = snap.tmpfiles_state != OwnedState.MISSING
        if not unit_present and not tf_present and not mounted_tmpfs:
            self.host.forget_managed_files((snap.unit_path, snap.tmpfiles_path))
            say(f"already reverted: no {snap.unit_name} or tmpfs mount for {snap.target.user}")
            return 0
        if mounted_tmpfs or snap.unit_active == ActiveState.ACTIVE or snap.unit_enabled == EnablementState.ENABLED:
            # Stop and unmount first: if a build holds the mount busy, fail before deleting files.
            self.host.disable_now(UnitName(snap.unit_name))
            say(f"unmounted: {snap.target.mountpoint} ({snap.unit_name} stopped and disabled)")
        removed_unit = False
        forget_paths: list[str] = []
        if snap.unit_state == OwnedState.MASKED:
            say(f"left alone: {snap.unit_path} (masked with systemctl mask)", 4, err=True)
        else:
            removed_unit = self.host.remove_file(snap.unit_path)
            if removed_unit:
                say(f"removed:   {snap.unit_path}")
            forget_paths.append(snap.unit_path)
        if self.host.remove_file(snap.tmpfiles_path):
            say(f"removed:   {snap.tmpfiles_path}")
        forget_paths.append(snap.tmpfiles_path)
        self.host.forget_managed_files(forget_paths)
        if removed_unit or snap.unit_state in (OwnedState.OK, OwnedState.CHANGE):
            self.host.daemon_reload()
        self.host.remove_empty_dir(snap.target.mountpoint)
        if snap.cargo_uses_tmpfs:
            say(f"Next: run `{self.prog} config apply` (without sudo) to switch ~/.cargo/config.toml back to NVMe")
        return 0
