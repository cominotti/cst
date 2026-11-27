# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs flushoncommit` (`btrfs/btrfs-flushoncommit`)."""

from __future__ import annotations

from typing import Final

from btrfs.compress.adapters import RootOptionTool
from btrfs.compress.application import LayerRun, LayerStep, RemountStep, make_layer_steps
from btrfs.compress.domain import FSTAB_PATH, CompressSnapshot
from btrfs.compress.domain.root_option import OPT_FLUSHONCOMMIT
from btrfs.compress.presenter import RootOptionPlanView, RootOptionStatusSpec
from btrfs.flushoncommit.domain import (
    FLUSH_PROG,
    OPT_NOFLUSHONCOMMIT,
    FlushParsedArgs,
    FlushPlan,
    assess_flush_coherence,
    booted_flush_diff,
    build_flush_plan,
    effective_flushoncommit,
    format_flushoncommit_label,
    format_flush_usage,
    has_flushoncommit_token,
    parse_flush_args,
)

SEAM_FLUSH_PROC: Final = "CST_BTRFS_FLUSH_PROC"
SEAM_FLUSH_ROOT: Final = "CST_BTRFS_FLUSH_ROOT"


class _FlushDriver:
    def build_plan(self, snap: CompressSnapshot, parsed: FlushParsedArgs) -> FlushPlan:
        return build_flush_plan(snap, revert=parsed.revert)

    def confirm_prompt(self, plan: FlushPlan) -> str:
        verb = "Enable" if plan.revert else "Remove"
        return f"{verb} flushoncommit across active root btrfs layers?"

    def build_steps(self, run: LayerRun, plan: FlushPlan) -> tuple[LayerStep, ...]:
        remount_opt = OPT_FLUSHONCOMMIT if plan.enable else OPT_NOFLUSHONCOMMIT
        undo_opt = OPT_NOFLUSHONCOMMIT if plan.enable else OPT_FLUSHONCOMMIT
        state_word = "enabled" if plan.enable else "disabled"
        remount = (
            None
            if plan.remount_target is None
            else RemountStep(
                plan.remount_target,
                remount_opt,
                undo_opt,
                lambda m: effective_flushoncommit(m.options) == plan.enable,
                remount_opt,
            )
        )
        return make_layer_steps(
            run,
            plan.core,
            remount,
            fstab_done=f"Updated {FSTAB_PATH} (flushoncommit {state_word}) and reloaded systemd.",
            kargs_done=f"Updated rpm-ostree kargs (flushoncommit {state_word} for next boot).",
        )

    def status_spec(self, snap: CompressSnapshot, proc_cmdline: str | None, *, kargs_ok: bool) -> RootOptionStatusSpec:
        coherence = assess_flush_coherence(snap)
        plan_view = build_flush_plan(snap, revert=bool(coherence.enabled))
        return RootOptionStatusSpec(
            header="Root btrfs flushoncommit:",
            seam_var=SEAM_FLUSH_PROC,
            format_live_opts=lambda o: format_flushoncommit_label(
                enabled=effective_flushoncommit(o), explicit=has_flushoncommit_token(o)
            ),
            layers=plan_view.layers,
            left_alone_notes=plan_view.left_alone_notes,
            coherence_summary=coherence.summary,
            booted_diff=booted_flush_diff(snap, proc_cmdline, kargs_ok=kargs_ok),
        )

    def plan_view(self, plan: FlushPlan, prog: str, *, dry_run: bool, reload_pending: str) -> RootOptionPlanView:
        target_desc = OPT_FLUSHONCOMMIT if plan.enable else f"no {OPT_FLUSHONCOMMIT} (CST/kernel default)"
        hint_word = "revert" if plan.revert else "apply"
        hint = f"Dry run, nothing changed. Run `{prog} {hint_word}` to apply." if dry_run else ""
        return RootOptionPlanView(
            header=f"Target root btrfs flushoncommit: {target_desc}",
            seam_line="",
            layers=plan.layers,
            notes=plan.left_alone_notes,
            has_changes=plan.has_changes,
            already_msg=f"Already {target_desc} across all active layers; nothing to do.",
            dry_run_hint=hint,
            reload_pending=reload_pending,
        )


FLUSH_TOOL = RootOptionTool(FLUSH_PROG, SEAM_FLUSH_PROC, SEAM_FLUSH_ROOT, _FlushDriver())
FLUSH_SPEC = FLUSH_TOOL.cli_spec(
    parse_flush_args,
    format_flush_usage,
    "modifies /etc/fstab, rpm-ostree kargs, live mount options, and top-mount unit",
)
