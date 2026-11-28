# SPDX-License-Identifier: MPL-2.0
"""Application use cases for `cst desktop psd`: apply, start, stop, sync, recover, revert."""

from __future__ import annotations

from dataclasses import dataclass

from cst_core.domain.errors import Refused
from cst_core.domain.values import ExitAndLines
from cst_core.ports import CommandFailed, WriteFailed
from desktop.domain import PsdAction
from desktop.domain.psd import (
    PsdProfileProbe,
    PsdProfileState,
    PsdSnapshot,
    format_busy_procs,
    format_psd_plan,
    format_psd_status,
)
from desktop.ports import PsdHostPort


@dataclass(frozen=True, slots=True)
class PsdRequest:
    """Normalized request parameters for `PsdService.run()`."""

    action: PsdAction
    user: str | None = None
    profiles: str | None = None
    size_text: str | None = None
    discard: bool = False
    skip_busy: bool = False


def _snapshot_or_fail(host: PsdHostPort, req: PsdRequest) -> PsdSnapshot:
    snap = host.read_snapshot(user=req.user, profiles_override=req.profiles, size_override=req.size_text)
    if snap.user is None:
        raise Refused(snap.user_error)
    if snap.config_error:
        raise Refused(snap.config_error)
    return snap


def _blocked_profile_reason(probe: PsdProfileProbe) -> str | None:
    if probe.state == PsdProfileState.SYMLINK_BLOCKED:
        return f"{probe.profile_path} is a symbolic link (refusing to mount overlay over symlink)"
    if probe.state == PsdProfileState.CONFLICT_MOUNT:
        return f"{probe.profile_path} already has a non-CST mount; unmount it first"
    return None


class PsdService:
    """Orchestrates `cst desktop psd` subcommands cleanly over `PsdHostPort`."""

    def __init__(self, host: PsdHostPort, *, prog: str) -> None:
        self._host = host
        self._prog = prog

    def status_lines(self, *, user: str | None = None) -> list[str]:
        """Return `psd status` output lines."""
        return format_psd_status(self._host.read_snapshot(user=user))

    def _mount_idle_profiles(self, snap: PsdSnapshot, *, strict_busy: bool) -> list[str]:
        assert snap.user is not None
        user = snap.user
        out: list[str] = []
        mount_candidates = [
            p
            for p in snap.profiles
            if not p.overlay_active
            and _blocked_profile_reason(p) is None
            and p.state != PsdProfileState.MISSING_DIR
            and not p.busy_procs
        ]
        for p in snap.profiles:
            if (blocked := _blocked_profile_reason(p)) is not None:
                if strict_busy:
                    raise Refused(blocked)
                out.append(f"skipped:  {p.spec.profile_id} ({blocked})")
            elif p.state == PsdProfileState.MISSING_DIR:
                out.append(f"skipped:  {p.spec.profile_id} (profile directory does not exist: {p.profile_path})")
            elif p.overlay_active:
                out.append(f"unchanged:{p.spec.profile_id} (overlay already active)")
            elif p.busy_procs:
                msg = f"{p.spec.profile_id} in use by {format_busy_procs(p.busy_procs)}"
                if strict_busy:
                    raise Refused(f"{msg}; close the application before starting overlayfs")
                out.append(f"deferred: {p.spec.profile_id} ({msg})")
        if not mount_candidates:
            return out
        self._host.ensure_tmpfs_mounted(user, snap.effective_tmpfs_bytes)
        for p in mount_candidates:
            if p.recovery_exists:
                self._host.apply_recovery(p, user)
                out.append(f"recovered:{p.spec.profile_id} (replayed crash snapshot -> {p.profile_path})")
            self._host.mount_overlay(p, user)
            out.append(f"mounted:  {p.spec.profile_id} -> {p.profile_path}")
        return out

    def _stop_profiles(self, snap: PsdSnapshot) -> list[str]:
        assert snap.user is not None
        user = snap.user
        active = [p for p in snap.profiles if p.overlay_active]
        for p in active:
            if p.busy_procs:
                raise Refused(
                    f"{p.spec.profile_id} still in use by {format_busy_procs(p.busy_procs)}; "
                    "close the application before stopping overlayfs"
                )
        out: list[str] = []
        for p in active:
            self._host.unmount_overlay(p)
            self._host.merge_upper(p, user)
            self._host.clear_upper_and_work(p)
            self._host.discard_recovery(p)
            out.append(f"merged:   {p.spec.profile_id} (upper -> {p.profile_path})")
        if snap.tmpfs_mounted and self._host.unmount_tmpfs_if_unused(user):
            out.append(f"unmounted:{user.run_dir}")
        return out or ["No active psd overlays; nothing to stop."]

    def _sync_profiles(self, snap: PsdSnapshot) -> list[str]:
        assert snap.user is not None
        user = snap.user
        out: list[str] = []
        for p in snap.profiles:
            if not p.overlay_active:
                continue
            if p.busy_procs:
                self._host.snapshot_recovery(p, user)
                out.append(
                    f"snapshot: {p.spec.profile_id} in use by {format_busy_procs(p.busy_procs)} "
                    "-> saved crash-recovery snapshot"
                )
            else:
                self._host.unmount_overlay(p)
                self._host.merge_upper(p, user)
                self._host.clear_upper_and_work(p)
                self._host.discard_recovery(p)
                self._host.mount_overlay(p, user)
                out.append(f"synced:   {p.spec.profile_id} (idle merge -> {p.profile_path})")
        return out or ["No active psd overlays to sync."]

    def _recover_profiles(self, snap: PsdSnapshot, *, discard: bool) -> list[str]:
        assert snap.user is not None
        user = snap.user
        pending = [p for p in (*snap.profiles, *snap.orphan_recoveries) if p.recovery_exists]
        if not pending:
            return ["No pending psd crash-recovery snapshots."]
        actionable = [p for p in pending if not p.overlay_active]
        active_live = [p for p in pending if p.overlay_active]
        if not actionable:
            raise Refused(f"{active_live[0].spec.profile_id} overlay is active; stop it before recovering")
        for p in actionable:
            if not discard:
                if (blocked := _blocked_profile_reason(p)) is not None:
                    raise Refused(blocked)
                if not p.profile_dir_exists:
                    raise Refused(f"{p.profile_path} does not exist; create the profile directory or pass --discard")
            if p.busy_procs:
                raise Refused(f"{p.spec.profile_id} in use by {format_busy_procs(p.busy_procs)}; close it first")
        out: list[str] = []
        for p in actionable:
            if discard:
                self._host.discard_recovery(p)
                out.append(f"discarded:{p.spec.profile_id} crash-recovery snapshot")
            else:
                self._host.apply_recovery(p, user)
                out.append(f"recovered:{p.spec.profile_id} crash-recovery snapshot -> {p.profile_path}")
        out.extend(f"skipped:  {p.spec.profile_id} (overlay active; merged on stop or idle sync)" for p in active_live)
        return out

    def _apply(self, snap: PsdSnapshot) -> list[str]:
        assert snap.user is not None
        lines = self._host.install_units()
        mounted_lines = self._mount_idle_profiles(snap, strict_busy=False)
        self._host.enable_systemd_units(snap.user.name)
        lines.append(f"enabled:  cst-desktop-psd@{snap.user.name}.service + resync timer (hourly)")
        lines.extend(mounted_lines)
        return lines

    def _revert(self, snap: PsdSnapshot) -> list[str]:
        assert snap.user is not None
        lines = self._stop_profiles(snap)
        self._host.disable_systemd_units(snap.user.name)
        lines.append(f"disabled: cst-desktop-psd@{snap.user.name}.service + resync timer")
        lines.extend(self._host.remove_units())
        return lines

    def run(self, req: PsdRequest) -> ExitAndLines:
        """Execute one `cst desktop psd` subcommand."""
        if req.action == PsdAction.STATUS:
            return 0, self.status_lines(user=req.user)
        try:
            snap = _snapshot_or_fail(self._host, req)
        except Refused as exc:
            return 1, [f"ERROR: {exc}"]
        if req.action == PsdAction.PLAN:
            return 0, format_psd_plan(snap, verb="apply", prog=self._prog)
        handlers = {
            PsdAction.APPLY: lambda: self._apply(snap),
            PsdAction.START: lambda: self._mount_idle_profiles(snap, strict_busy=not req.skip_busy),
            PsdAction.STOP: lambda: self._stop_profiles(snap),
            PsdAction.SYNC: lambda: self._sync_profiles(snap),
            PsdAction.RECOVER: lambda: self._recover_profiles(snap, discard=req.discard),
            PsdAction.REVERT: lambda: self._revert(snap),
        }
        handler = handlers.get(req.action)
        if handler is None:
            return 1, [f"ERROR: unsupported psd action: {req.action.value}"]
        try:
            return 0, handler()
        except (Refused, WriteFailed, CommandFailed, OSError) as exc:
            return 1, [f"ERROR: {exc}"]
