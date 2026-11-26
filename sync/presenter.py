# SPDX-License-Identifier: MPL-2.0
"""Plain-text rendering for `cst sync plan` and `cst sync apply`."""

from __future__ import annotations

from sync.domain import (
    SYNC_PROG,
    StepOutcome,
    SyncApplyReport,
    SyncPlan,
    SyncStep,
)
from cst_core.domain.text import DRY_RUN_LINE


def _step_files(step: SyncStep) -> list[str]:
    return [f"{fs.dest} ({fs.drift.value})" for c in step.components for fs in c.files]


def _component_list(step: SyncStep) -> str:
    return ", ".join(c.component.value for c in step.components)


def format_sync_plan(plan: SyncPlan, prog: str = SYNC_PROG) -> str:
    """Format `cst sync plan` output."""
    lines: list[str] = ["cst sync plan: read-only check of installed CST components", ""]

    if plan.steps_to_run:
        lines.append("== would sync (safe defaults)")
        for step in plan.steps_to_run:
            scope = "root" if step.needs_root else "user"
            lines.append(f"  sync   {step.command_label} [{scope}] ({_component_list(step)})")
            lines.extend(f"         - {desc}" for desc in _step_files(step))
        lines.append("")

    if plan.skipped_risky:
        lines.append("== skipped by precaution (re-run with --include-risky to run these too)")
        for step in plan.skipped_risky:
            lines.append(f"  skip   {step.command_label} ({_component_list(step)}): {step.reason}")
            lines.extend(f"         - {desc}" for desc in _step_files(step))
        lines.append(f"  flag:  {prog} apply --include-risky")
        lines.append("")

    if plan.skipped_modified:
        lines.append("== skipped: differs on disk from install record (--overwrite-modified to overwrite)")
        for step in plan.skipped_modified:
            lines.append(f"  skip   {step.command_label} ({_component_list(step)})")
            lines.extend(f"         - {desc}" for desc in _step_files(step))
        lines.append(f"  flag:  {prog} apply --overwrite-modified")
        lines.append("")

    if plan.unreadable_files:
        lines.append("== not readable as this user (judged from install record only)")
        lines.extend(f"  info   {dest}" for dest in plan.unreadable_files)
        lines.append("")

    if plan.notes:
        lines.extend(f"NOTE: {n}" for n in plan.notes)

    lines.append(
        f"summary: {len(plan.steps_to_run)} command(s) to sync, "
        f"{len(plan.skipped_risky)} skipped (risky), "
        f"{len(plan.skipped_modified)} skipped (modified), "
        f"{plan.up_to_date_components} component(s) up to date."
    )
    if plan.steps_to_run:
        lines.append(f"Run `{prog} apply` to refresh the safe component(s).")
    lines.append(DRY_RUN_LINE)
    return "\n".join(lines)


def format_apply_report(report: SyncApplyReport, prog: str = SYNC_PROG) -> str:
    """Format `cst sync apply` summary output."""
    lines: list[str] = []
    if not report.root_only and report.plan.notes:
        lines.extend(f"NOTE: {n}" for n in report.plan.notes)
    if report.warnings:
        lines.extend(f"WARNING: {w}" for w in report.warnings)

    updated = sum(1 for r in report.results if r.outcome == StepOutcome.UPDATED)
    failed = [r for r in report.results if r.outcome == StepOutcome.FAILED]
    failed_count = len(failed)
    root_steps = [s for s in report.plan.steps_to_run if s.needs_root]
    if report.root_subpass_rc == 0:
        updated += len(root_steps)
    elif report.root_subpass_rc is not None:
        failed_count += 1

    lines.append("")
    if report.root_only:
        lines.append(f"cst sync apply (root): {updated} updated, {failed_count} failed.")
        lines.extend(f"  FAILED ({r.returncode}): {r.step.command_label}" for r in failed)
        return "\n".join(lines)

    lines.append(
        f"cst sync apply: {updated} updated, {failed_count} failed, "
        f"{len(report.plan.skipped_risky)} skipped (risky), "
        f"{len(report.plan.skipped_modified)} skipped (modified), "
        f"{report.plan.up_to_date_components} already up to date."
    )
    lines.extend(f"  FAILED ({r.returncode}): {r.step.command_label}" for r in failed)
    if report.root_subpass_rc not in (None, 0):
        root_labels = ", ".join(s.command_label for s in root_steps)
        lines.append(f"  FAILED ({report.root_subpass_rc}): root subpass ({root_labels})")
    if report.plan.skipped_risky:
        lines.append("Skipped by precaution (run with --include-risky to include):")
        lines.extend(
            f"  - {step.command_label} ({_component_list(step)}): {step.reason}" for step in report.plan.skipped_risky
        )
        lines.append(f"  Force all of these with: {prog} apply --include-risky")
    if report.plan.skipped_modified:
        lines.append(
            "Skipped because file(s) differ on disk from the install record (--overwrite-modified to overwrite):"
        )
        lines.extend(f"  - {step.command_label} ({_component_list(step)})" for step in report.plan.skipped_modified)
        lines.append(f"  Overwrite these too with: {prog} apply --overwrite-modified")
    return "\n".join(lines)
