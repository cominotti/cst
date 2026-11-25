# SPDX-License-Identifier: MPL-2.0
"""Use cases for `cst btrfs compress-force`: status, plan, apply, revert (no direct terminal I/O)."""

from __future__ import annotations

from dataclasses import dataclass

from btrfs.compress.domain import (
    FSTAB_PATH,
    PROC_MOUNTS_PATH,
    TOP_MOUNT_UNIT_PATH,
    CoherenceReport,
    CompressMode,
    CompressPlan,
    CompressSnapshot,
    CompressSpec,
    CompressStatusView,
    assess_coherence,
    build_plan,
    effective_compress_spec,
    extract_rootflags_tokens,
    kargs_edits_staged,
    matches_target_spec,
    parse_proc_mounts,
    pick_root_mounts,
    top_mount_changed,
)
from btrfs.compress.domain.args import CompressArgs, CompressCommand
from btrfs.compress.ports import TOP_MOUNT_ERRORS, CompressHostPort
from cst_core.domain.errors import Refused
from cst_core.ports import CommandFailed


@dataclass(slots=True)
class _ApplyProgress:
    fstab_written: bool = False
    kargs_applied: tuple[tuple[str, str], ...] = ()
    remounted: bool = False
    top_written: bool = False
    orig_live_spec: CompressSpec | None = None


@dataclass(frozen=True, slots=True)
class CompressPlanResult:
    """Outcome of `plan`."""

    exit_code: int
    seam_proc: str
    plan: CompressPlan | None = None
    error: str = ""
    hint_cmd: CompressCommand = CompressCommand.APPLY


@dataclass(frozen=True, slots=True)
class CompressChangeResult:
    """Outcome of `apply` or `revert`."""

    exit_code: int
    seam_proc: str
    plan: CompressPlan | None = None
    messages: tuple[str, ...] = ()
    error: str = ""


def _booted_kargs_diff(
    snap: CompressSnapshot,
    proc_cmdline: str | None,
    *,
    kargs_ok: bool,
) -> tuple[str, str] | None:
    if not (snap.is_ostree and kargs_ok and proc_cmdline is not None and snap.kargs_text is not None):
        return None
    boot_rf = extract_rootflags_tokens(proc_cmdline)
    next_rf = extract_rootflags_tokens(snap.kargs_text)
    # The kernel uses the last rootflags= of its command line.
    if boot_rf and next_rf and boot_rf[-1] != next_rf[-1]:
        return (boot_rf[-1], next_rf[-1])
    return None


def _apply_kargs_coherence(
    snap: CompressSnapshot,
    coherence: CoherenceReport,
    kargs_err: str,
    *,
    kargs_ok: bool,
) -> CoherenceReport:
    incomplete_summary: str | None = None
    if snap.is_ostree and not kargs_ok:
        incomplete_summary = f"incomplete (rpm-ostree kargs error: {kargs_err})"
    elif snap.is_ostree and snap.kargs_text is not None and not extract_rootflags_tokens(snap.kargs_text):
        incomplete_summary = "incomplete (no rootflags= in rpm-ostree kargs)"
    if incomplete_summary is None:
        return coherence
    return CoherenceReport(
        coherent=False,
        mode=coherence.mode,
        level=coherence.level,
        summary=incomplete_summary,
        drift_details=coherence.drift_details,
        has_compression=coherence.has_compression,
    )


class CompressService:
    """Orchestrates reading and updating root btrfs compression across all config layers."""

    def __init__(self, host: CompressHostPort, *, seam_proc: str = "") -> None:
        self._host = host
        self._seam_proc = seam_proc

    def _snapshot(self) -> tuple[CompressSnapshot, bool, str]:
        is_ostree = self._host.is_ostree()
        kargs_ok, kargs_text, kargs_err = self._host.read_kargs() if is_ostree else (True, "", "")
        snap = CompressSnapshot(
            is_ostree=is_ostree,
            root_uuid=self._host.root_uuid(),
            fstab_text=self._host.read_fstab(),
            kargs_text=kargs_text if (is_ostree and kargs_ok) else None,
            proc_mounts_text=self._host.read_proc_mounts(),
            top_mount=self._host.read_top_mount(),
        )
        ok = True if not is_ostree else kargs_ok
        return snap, ok, kargs_err

    def gather_status(self) -> CompressStatusView:
        """Collect read-only status across live mounts, fstab, kargs, and top-mount."""
        snap, kargs_ok, kargs_err = self._snapshot()
        coherence = _apply_kargs_coherence(snap, assess_coherence(snap), kargs_err, kargs_ok=kargs_ok)
        live_roots = pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
        live_entries = tuple(
            (e.target, sp.raw if (sp := effective_compress_spec(e.options)) is not None else "(none)")
            for e in live_roots
        )
        plan_view = build_plan(
            snap,
            target_mode=coherence.mode or CompressMode.COMPRESS_FORCE,
            level_override=coherence.level,
        )
        booted_diff = _booted_kargs_diff(snap, self._host.read_proc_cmdline(), kargs_ok=kargs_ok)
        kargs_err_msg = kargs_err if (snap.is_ostree and not kargs_ok) else ""
        return CompressStatusView(
            seam_proc=self._seam_proc,
            live_entries=live_entries,
            layers=plan_view.layers,
            kargs_error=kargs_err_msg,
            booted_diff=booted_diff,
            coherence=coherence,
            left_alone_notes=plan_view.left_alone_notes,
        )

    def build_change_plan(self, args: CompressArgs) -> CompressPlanResult:
        """Compute the transition plan without mutating the machine."""
        snap, kargs_ok, kargs_err = self._snapshot()
        hint_cmd = CompressCommand.REVERT if args.revert else CompressCommand.APPLY
        if not kargs_ok:
            return CompressPlanResult(1, self._seam_proc, error=f"cannot inspect rpm-ostree kargs: {kargs_err}")
        target_mode = CompressMode.COMPRESS if args.revert else CompressMode.COMPRESS_FORCE
        plan = build_plan(snap, target_mode=target_mode, level_override=args.level)
        return CompressPlanResult(0, self._seam_proc, plan=plan, hint_cmd=hint_cmd)

    def _apply_top_mount(
        self, snap: CompressSnapshot, plan: CompressPlan, prog: _ApplyProgress, msgs: list[str]
    ) -> str | None:
        if plan.new_top_mount_options is None or snap.top_mount is None:
            return None
        uuid = snap.top_mount.uuid or snap.root_uuid
        if not uuid:
            return f"Cannot update {TOP_MOUNT_UNIT_PATH}: root filesystem UUID unknown"
        try:
            self._host.update_top_mount(uuid, plan.new_top_mount_options)
        except TOP_MOUNT_ERRORS as exc:
            # A failure whose put-back failed too leaves the new unit: it is undone with the rest.
            prog.top_written = top_mount_changed(self._host.read_top_mount(), plan.new_top_mount_options)
            return f"Failed to update {TOP_MOUNT_UNIT_PATH}: {exc}"
        msgs.append(f"Updated {TOP_MOUNT_UNIT_PATH} and reloaded systemd.")
        return None

    def _apply_live_remount(self, plan: CompressPlan, prog: _ApplyProgress, msgs: list[str]) -> str | None:
        if plan.remount_target is None:
            return None
        ok, rerr = self._host.remount(plan.remount_target, plan.target_option)
        if not ok:
            return f"Failed to remount {plan.remount_target} with {plan.target_option}: {rerr}"
        prog.remounted = True
        after_roots = pick_root_mounts(parse_proc_mounts(self._host.read_proc_mounts() or ""))
        if not after_roots or not all(
            matches_target_spec(effective_compress_spec(e.options), plan.target_mode, plan.target_level)
            for e in after_roots
        ):
            return f"Post-remount verification failed in {PROC_MOUNTS_PATH}: expected {plan.target_option}"
        msgs.append(f"Remounted {plan.remount_target} with {plan.target_option} (verified in {PROC_MOUNTS_PATH}).")
        return None

    def _rollback_layers(self, snap: CompressSnapshot, plan: CompressPlan, prog: _ApplyProgress) -> str:
        errors: list[str] = []
        if prog.top_written and snap.top_mount is not None:
            try:
                self._host.update_top_mount(snap.top_mount.uuid or snap.root_uuid, snap.top_mount.options)
            except TOP_MOUNT_ERRORS as exc:
                errors.append(f"top-mount: {exc}")
        if prog.remounted and plan.remount_target is not None:
            revert_opt = prog.orig_live_spec.raw if prog.orig_live_spec is not None else "compress=no"
            ok, rerr = self._host.remount(plan.remount_target, revert_opt)
            if not ok:
                errors.append(f"remount: {rerr}")
        if prog.kargs_applied:
            ok, kerr = self._host.update_kargs([(new, old) for old, new in prog.kargs_applied])
            if not ok:
                errors.append(f"kargs: {kerr}")
        if prog.fstab_written and snap.fstab_text is not None:
            try:
                self._host.write_fstab(snap.fstab_text)
            except (OSError, CommandFailed) as exc:
                errors.append(f"fstab: {exc}")
        return f"ROLLBACK INCOMPLETE ({'; '.join(errors)})" if errors else "previous configuration restored."

    def _apply_layers(self, snap: CompressSnapshot, plan: CompressPlan) -> tuple[tuple[str, ...], str | None]:
        msgs: list[str] = []
        prog = _ApplyProgress(
            orig_live_spec=next(
                (
                    effective_compress_spec(e.options)
                    for e in pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
                    if e.target == plan.remount_target
                ),
                None,
            )
        )

        if plan.new_fstab_text is not None and snap.fstab_text is not None:
            try:
                self._host.write_fstab(plan.new_fstab_text)
            except (OSError, CommandFailed) as exc:
                failure = f"Failed to update {FSTAB_PATH}: {exc}"
                # A write that failed after the new text was in place is undone like one that worked.
                if self._host.read_fstab() != plan.new_fstab_text:
                    return tuple(msgs), failure
                prog.fstab_written = True
                return tuple(msgs), f"{failure}; {self._rollback_layers(snap, plan, prog)}"
            prog.fstab_written = True
            msgs.append(f"Updated {FSTAB_PATH} ({plan.target_option}) and reloaded systemd.")

        if plan.kargs_edits:
            ok, kerr = self._host.update_kargs(plan.kargs_edits)
            if not ok:
                # rpm-ostree can stage the change and still answer a failure (a timed-out client).
                if kargs_edits_staged(self._host.read_kargs(), plan.kargs_edits):
                    prog.kargs_applied = plan.kargs_edits
                return tuple(
                    msgs
                ), f"Failed to update rpm-ostree kargs: {kerr}; {self._rollback_layers(snap, plan, prog)}"
            prog.kargs_applied = plan.kargs_edits
            msgs.append(f"Updated rpm-ostree kargs ({plan.target_option} for next boot).")

        remount_err = self._apply_live_remount(plan, prog, msgs)
        if remount_err is not None:
            return tuple(msgs), f"{remount_err}; {self._rollback_layers(snap, plan, prog)}"

        top_err = self._apply_top_mount(snap, plan, prog, msgs)
        if top_err is not None:
            return tuple(msgs), f"{top_err}; {self._rollback_layers(snap, plan, prog)}"
        return tuple(msgs), None

    def execute_change(self, args: CompressArgs) -> CompressChangeResult:
        """Run `apply` or `revert` atomically across all active layers."""
        snap, kargs_ok, kargs_err = self._snapshot()
        if not kargs_ok:
            return CompressChangeResult(1, self._seam_proc, error=f"cannot inspect rpm-ostree kargs: {kargs_err}")
        target_mode = CompressMode.COMPRESS if args.command == CompressCommand.REVERT else CompressMode.COMPRESS_FORCE
        plan = build_plan(snap, target_mode=target_mode, level_override=args.level)
        if not plan.has_changes:
            return CompressChangeResult(
                0,
                self._seam_proc,
                plan=plan,
                messages=(f"Already at {plan.target_option} across all active layers; nothing to do.",),
            )
        if not args.yes:
            try:
                confirmed = self._host.confirm(f"Apply {plan.target_option} across active layers?")
            except Refused as exc:
                return CompressChangeResult(
                    1,
                    self._seam_proc,
                    plan=plan,
                    error=f"{exc} (pass --yes to apply non-interactively).",
                )
            if not confirmed:
                return CompressChangeResult(1, self._seam_proc, plan=plan, messages=("Aborted; nothing changed.",))
        msgs, err_msg = self._apply_layers(snap, plan)
        return CompressChangeResult(
            1 if err_msg is not None else 0,
            self._seam_proc,
            plan=plan,
            messages=msgs,
            error=err_msg or "",
        )
