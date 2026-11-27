# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs commit-interval` (`btrfs/btrfs-commit-interval`)."""

from __future__ import annotations

from typing import Final

from btrfs.commit.domain import (
    COMMIT_PROG,
    DEFAULT_TARGET_COMMIT_S,
    KERNEL_DEFAULT_COMMIT_S,
    CommitParsedArgs,
    CommitPlan,
    assess_commit_coherence,
    booted_commit_diff,
    build_commit_plan,
    effective_commit_seconds,
    format_commit_label,
    has_explicit_commit,
)
from btrfs.compress.adapters import (
    RootOptionTool,
    make_root_option_host,
    read_root_option_snapshot,
)
from btrfs.compress.application import LayerRun, LayerStep, RemountStep, make_layer_steps
from btrfs.compress.domain import FSTAB_PATH, CompressSnapshot, parse_proc_mounts, pick_root_mounts
from btrfs.compress.ports import CompressHostPort
from btrfs.compress.presenter import RootOptionPlanView, RootOptionStatusSpec
from cst_core.adapters.runtime import ManagedCliEnv
from cst_core.domain.values import ExitAndLines

SEAM_COMMIT_PROC: Final = "CST_BTRFS_COMMIT_PROC"
SEAM_COMMIT_ROOT: Final = "CST_BTRFS_COMMIT_ROOT"


def _host_for_env(env: ManagedCliEnv) -> tuple[CompressHostPort, str, dict[str, str]]:
    return make_root_option_host(env, proc_seam_var=SEAM_COMMIT_PROC, root_seam_var=SEAM_COMMIT_ROOT)


def read_commit_snapshot(host: CompressHostPort) -> tuple[CompressSnapshot, bool, str]:
    """Read current state across fstab, rpm-ostree kargs, live mounts, and top-mount."""
    return read_root_option_snapshot(host)


class _CommitDriver:
    def build_plan(self, snap: CompressSnapshot, parsed: CommitParsedArgs) -> CommitPlan:
        return build_commit_plan(snap, target_seconds=parsed.seconds, revert=parsed.revert)

    def confirm_prompt(self, plan: CommitPlan) -> str:
        return f"Apply commit={plan.target_seconds}s across active layers?"

    def build_steps(self, run: LayerRun, plan: CommitPlan) -> tuple[LayerStep, ...]:
        seconds = plan.target_seconds
        remount: RemountStep | None = None
        if plan.remount_target is not None:
            prior_roots = pick_root_mounts(parse_proc_mounts(run.snap.proc_mounts_text or ""))
            prior = next((m for m in prior_roots if m.target == plan.remount_target), None)
            prev_sec = effective_commit_seconds(prior.options) if prior is not None else KERNEL_DEFAULT_COMMIT_S
            remount = RemountStep(
                plan.remount_target,
                f"commit={seconds}",
                f"commit={prev_sec}",
                lambda m: effective_commit_seconds(m.options) == seconds,
                f"commit={seconds}s",
            )
        return make_layer_steps(
            run,
            plan.core,
            remount,
            fstab_done=f"Updated {FSTAB_PATH} (commit={seconds}s) and reloaded systemd.",
            kargs_done=f"Updated rpm-ostree kargs (commit={seconds}s for next boot).",
        )

    def status_spec(self, snap: CompressSnapshot, proc_cmdline: str | None, *, kargs_ok: bool) -> RootOptionStatusSpec:
        coherence = assess_commit_coherence(snap)
        plan_view = build_commit_plan(snap, target_seconds=coherence.seconds or DEFAULT_TARGET_COMMIT_S)
        return RootOptionStatusSpec(
            header="Root btrfs commit interval:",
            seam_var=SEAM_COMMIT_PROC,
            format_live_opts=lambda o: format_commit_label(
                effective_commit_seconds(o), explicit=has_explicit_commit(o)
            ),
            layers=plan_view.layers,
            left_alone_notes=plan_view.left_alone_notes,
            coherence_summary=coherence.summary,
            booted_diff=booted_commit_diff(snap, proc_cmdline, kargs_ok=kargs_ok),
        )

    def plan_view(self, plan: CommitPlan, prog: str, *, dry_run: bool, reload_pending: str) -> RootOptionPlanView:
        hint_word = "revert" if plan.revert else f"apply {plan.target_seconds}"
        hint = f"Dry run, nothing changed. Run `{prog} {hint_word}` to apply." if dry_run else ""
        return RootOptionPlanView(
            header=f"Target root btrfs commit interval: {plan.target_seconds}s",
            seam_line="",
            layers=plan.layers,
            notes=plan.left_alone_notes,
            has_changes=plan.has_changes,
            already_msg=f"Already at {plan.target_seconds}s across all active layers; nothing to do.",
            dry_run_hint=hint,
            reload_pending=reload_pending,
        )


_COMMIT_TOOL = RootOptionTool(COMMIT_PROG, SEAM_COMMIT_PROC, SEAM_COMMIT_ROOT, _CommitDriver())


def execute_commit_change(
    host: CompressHostPort, parsed: CommitParsedArgs, *, prog: str = COMMIT_PROG, seam_proc: str = ""
) -> ExitAndLines:
    """Run transactional `apply` or `revert` against `host` (`CompressHostPort`)."""
    return _COMMIT_TOOL.execute(host, parsed, prog=prog, seam_proc=seam_proc)


def format_commit_status(
    snap: CompressSnapshot, *, kargs_ok: bool, kargs_error: str, proc_cmdline: str | None, seam_proc: str = ""
) -> list[str]:
    """Render status lines for `cst btrfs commit-interval status`."""
    return _COMMIT_TOOL.status_lines(snap, proc_cmdline, kargs=(kargs_ok, kargs_error), seam_proc=seam_proc)


def format_commit_plan_lines(
    plan: CommitPlan, prog: str, *, dry_run: bool, seam_proc: str = "", reload_pending: str = ""
) -> list[str]:
    """Render plan lines for `plan`, `apply`, and `revert`."""
    return _COMMIT_TOOL.plan_lines(plan, prog if dry_run else "", seam_proc=seam_proc, reload_pending=reload_pending)


def dispatch_commit_cli_action(parsed: CommitParsedArgs, env: ManagedCliEnv, *, read_only: bool = True) -> ExitAndLines:
    """Resolve host adapter and execute the requested `commit-interval` subcommand."""
    del read_only
    return _COMMIT_TOOL.dispatch(parsed, env, host_for_env=_host_for_env)
