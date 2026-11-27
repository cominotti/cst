# SPDX-License-Identifier: MPL-2.0
"""Output rendering for `cst btrfs compress-force` (`status`, `plan`, `apply`, `revert`)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TextIO

from btrfs.compress.application import CompressChangeResult, CompressPlanResult
from btrfs.compress.domain import (
    CompressPlan,
    CompressSnapshot,
    CompressStatusView,
    LayerName,
    LayerPlan,
    LayerStatus,
    extract_rootflags_tokens,
    parse_proc_mounts,
    pick_root_mounts,
)


def format_layer_plan_lines(header: str, layers: Sequence[LayerPlan], notes: Sequence[str] = ()) -> list[str]:
    """Render shared multi-layer plan header, per-layer status rows and a `Note:` line per note."""
    lines = [header]
    for lp in layers:
        match lp.status:
            case LayerStatus.CHANGE:
                marker = "CHANGE"
            case LayerStatus.OK:
                marker = "ok"
            case LayerStatus.NOT_APPLICABLE:
                marker = "skip"
            case _:
                marker = lp.status.value
        lines.append(f"  {lp.layer.value:<10} [{marker:<6}] {lp.current} -> {lp.target} ({lp.detail})")
    lines.extend(f"Note: {note}" for note in notes)
    return lines


def format_layer_status_lines(
    header: str,
    live_entries: Sequence[tuple[str, str]],
    layers: Sequence[LayerPlan],
    *,
    kargs_error: str = "",
    booted_diff: tuple[str, str] | None = None,
) -> list[str]:
    """Render shared multi-layer `status` header, live mounts, persistent layers, and boot diff."""
    lines = [header]
    for target, raw in live_entries:
        lines.append(f"  live {target:<10} {raw}")
    if not live_entries:
        lines.append("  live            (no root btrfs mount in /proc/mounts)")
    for lp in layers:
        if lp.layer == LayerName.LIVE:
            continue
        detail = f"(error: {kargs_error})" if lp.layer == LayerName.KARGS and kargs_error else lp.current
        lines.append(f"  {lp.layer.value:<15} {detail}")
    if booted_diff is not None:
        lines.append(
            f"  note            booted /proc/cmdline ({booted_diff[0]}) differs from next-boot kargs ({booted_diff[1]})"
        )
    return lines


def format_status_note_lines(notes: Sequence[str]) -> list[str]:
    """Render a `status` note line per note (after `format_layer_status_lines`)."""
    return [f"  note            {note}" for note in notes]


def format_seam_notice(var_name: str, seam_proc: str) -> str:
    """Format standard `/proc` test seam header line when `seam_proc` is non-empty."""
    return f"test seam ({var_name}): /proc in {seam_proc}, not the kernel's" if seam_proc else ""


@dataclass(frozen=True, slots=True)
class RootOptionStatusView:
    """Shared view model for root btrfs option `status` output."""

    header: str
    seam_line: str
    live_entries: Sequence[tuple[str, str]]
    layers: Sequence[LayerPlan]
    left_alone_notes: Sequence[str]
    overall_summary: str
    kargs_error: str = ""
    booted_diff: tuple[str, str] | None = None
    reload_pending: str = ""


@dataclass(frozen=True, slots=True)
class RootOptionStatusSpec:
    """Inputs for building a `RootOptionStatusView` from a snapshot."""

    header: str
    seam_var: str
    format_live_opts: Callable[[str], str]
    layers: Sequence[LayerPlan]
    left_alone_notes: Sequence[str]
    coherence_summary: str
    booted_diff: tuple[str, str] | None


def render_root_option_status(
    snap: CompressSnapshot,
    spec: RootOptionStatusSpec,
    *,
    kargs_ok: bool,
    kargs_error: str,
    seam_proc: str = "",
) -> list[str]:
    """Build live mount rows, ostree kargs completeness check, and render status lines."""
    live_roots = pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
    live_entries = [(entry.target, spec.format_live_opts(entry.options)) for entry in live_roots]
    if snap.is_ostree and not kargs_ok:
        overall = f"incomplete (rpm-ostree kargs error: {kargs_error})"
    elif snap.is_ostree and snap.kargs_text is not None and not extract_rootflags_tokens(snap.kargs_text):
        overall = "incomplete (no rootflags= in rpm-ostree kargs)"
    else:
        overall = spec.coherence_summary
    return render_root_option_status_lines(
        RootOptionStatusView(
            header=spec.header,
            seam_line=format_seam_notice(spec.seam_var, seam_proc),
            live_entries=live_entries,
            layers=spec.layers,
            left_alone_notes=spec.left_alone_notes,
            overall_summary=overall,
            kargs_error="" if kargs_ok else kargs_error,
            booted_diff=spec.booted_diff,
        )
    )


def render_root_option_status_lines(view: RootOptionStatusView) -> list[str]:
    """Render shared `status` output lines for root btrfs mount option tools."""
    out: list[str] = [view.seam_line] if view.seam_line else []
    out.extend(
        format_layer_status_lines(
            view.header,
            view.live_entries,
            view.layers,
            kargs_error=view.kargs_error,
            booted_diff=view.booted_diff,
        )
    )
    out.extend(format_status_note_lines(view.left_alone_notes))
    if view.reload_pending:
        out.append(f"  {view.reload_pending}")
    out.append(f"  overall         {view.overall_summary}")
    return out


@dataclass(frozen=True, slots=True)
class RootOptionPlanView:
    """Shared view model for root btrfs option `plan`/`apply`/`revert` header lines."""

    header: str
    seam_line: str
    layers: Sequence[LayerPlan]
    notes: Sequence[str]
    has_changes: bool
    already_msg: str
    dry_run_hint: str = ""
    reload_pending: str = ""


def render_root_option_plan_lines(view: RootOptionPlanView) -> list[str]:
    """Render shared `plan` / action header lines for root btrfs mount option tools."""
    out: list[str] = [view.seam_line] if view.seam_line else []
    out.extend(format_layer_plan_lines(view.header, view.layers, view.notes))
    if view.dry_run_hint and view.reload_pending:
        out.append(view.reload_pending)
    if not view.has_changes and not view.reload_pending:
        out.append(view.already_msg)
        return out
    if view.dry_run_hint:
        out.append(view.dry_run_hint)
    return out


def _emit_seam(seam_proc: str, out: TextIO) -> None:
    if seam_proc:
        print(f"test seam (CST_BTRFS_COMPRESS_PROC): /proc in {seam_proc}, not the kernel's", file=out)


def _render_plan(plan: CompressPlan, out: TextIO) -> None:
    for line in format_layer_plan_lines(
        f"Target compression: {plan.target_option}", plan.layers, plan.left_alone_notes
    ):
        print(line, file=out)


def render_compress_status(view: CompressStatusView, out: TextIO) -> int:
    """Render `status` output cleanly."""
    _emit_seam(view.seam_proc, out)
    for line in format_layer_status_lines(
        "Root btrfs compression:",
        view.live_entries,
        view.layers,
        kargs_error=view.kargs_error,
        booted_diff=view.booted_diff,
    ):
        print(line, file=out)
    for line in format_status_note_lines(view.left_alone_notes):
        print(line, file=out)
    if view.reload_pending:
        print(f"  {view.reload_pending}", file=out)
    print(f"  overall         {view.coherence.summary}", file=out)
    return 0


def render_compress_plan(res: CompressPlanResult, prog: str, out: TextIO, err: TextIO) -> int:
    """Render `plan` result and return process exit code."""
    _emit_seam(res.seam_proc, out)
    if res.error:
        print(res.error, file=err)
        return res.exit_code
    if res.plan is None:
        return res.exit_code
    _render_plan(res.plan, out)
    if res.reload_pending:
        print(res.reload_pending, file=out)
    if not res.plan.has_changes and not res.reload_pending:
        print(f"Already at {res.plan.target_option} across all active layers; nothing to do.", file=out)
        return 0
    if res.plan.has_changes:
        print("Note: remount affects new writes immediately; existing files are never auto-defragmented.", file=out)
    print(
        f"Dry run, nothing changed. Run `{prog} {res.hint_cmd.value} --level {res.plan.target_level}` to apply.",
        file=out,
    )
    return res.exit_code


def render_compress_change(res: CompressChangeResult, out: TextIO, err: TextIO) -> int:
    """Render `apply`/`revert` result and return process exit code."""
    _emit_seam(res.seam_proc, out)
    if res.plan is not None and res.plan.has_changes:
        _render_plan(res.plan, out)
        print("Note: remount applies immediately to new writes; existing files keep their current extents.", file=out)
    elif res.plan is not None:
        for note in res.plan.left_alone_notes:
            print(f"Note: {note}", file=out)
    for msg in res.messages:
        print(msg, file=out)
    if res.error:
        print(res.error, file=err)
    return res.exit_code
