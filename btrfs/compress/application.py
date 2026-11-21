# SPDX-License-Identifier: MPL-2.0
"""Use cases for `cst btrfs compress-force`: status, plan, apply, revert (no direct terminal I/O)."""

from __future__ import annotations

from dataclasses import dataclass

from btrfs.compress.domain import (
    FSTAB_PATH,
    PROC_MOUNTS_PATH,
    TOP_MOUNT_UNIT_PATH,
    CoherenceReport,
    CompressArgs,
    CompressCommand,
    CompressMode,
    CompressPlan,
    CompressSnapshot,
    CompressSpec,
    CompressStatusView,
    assess_coherence,
    build_plan,
    effective_compress_spec,
    extract_rootflags_tokens,
    matches_target_spec,
    parse_proc_mounts,
    pick_root_mounts,
    top_mount_options_from_var,
)
from btrfs.compress.ports import CompressHostPort
from cst_core.domain.errors import Refused
from cst_core.ports import CommandFailed, WriteFailed


@dataclass(slots=True)
class _ApplyProgress:
    fstab_written: bool = False
    kargs_applied: tuple[tuple[str, str], ...] = ()
    remounted: bool = False
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
    if boot_rf and next_rf and boot_rf != next_rf:
        return (boot_rf[0], next_rf[0])
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

    def _apply_top_mount(self, snap: CompressSnapshot, plan: CompressPlan, msgs: list[str]) -> str | None:
        if plan.new_top_mount_options is None or snap.top_mount is None:
            return None
        uuid = snap.top_mount.uuid or snap.root_uuid
        if not uuid:
            return f"Cannot update {TOP_MOUNT_UNIT_PATH}: root filesystem UUID unknown"
        live_after = pick_root_mounts(parse_proc_mounts(self._host.read_proc_mounts() or ""))
        var_entry = next((e for e in live_after if e.target == "/var"), None)
        top_opts = (
            top_mount_options_from_var(var_entry.options) if var_entry is not None else plan.new_top_mount_options
        )
        try:
            self._host.update_top_mount(uuid, top_opts)
        except (OSError, Refused, CommandFailed, WriteFailed, ValueError) as exc:
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
                return tuple(msgs), f"Failed to update {FSTAB_PATH}: {exc}"
            prog.fstab_written = True
            msgs.append(f"Updated {FSTAB_PATH} ({plan.target_option}) and reloaded systemd.")

        if plan.kargs_edits:
            ok, kerr = self._host.update_kargs(plan.kargs_edits)
            if not ok:
                return tuple(
                    msgs
                ), f"Failed to update rpm-ostree kargs: {kerr}; {self._rollback_layers(snap, plan, prog)}"
            prog.kargs_applied = plan.kargs_edits
            msgs.append(f"Updated rpm-ostree kargs ({plan.target_option} for next boot).")

        remount_err = self._apply_live_remount(plan, prog, msgs)
        if remount_err is not None:
            return tuple(msgs), f"{remount_err}; {self._rollback_layers(snap, plan, prog)}"

        top_err = self._apply_top_mount(snap, plan, msgs)
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
