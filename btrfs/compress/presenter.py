# SPDX-License-Identifier: MPL-2.0
"""Output rendering for `cst btrfs compress-force` (`status`, `plan`, `apply`, `revert`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO

from btrfs.compress.application import CompressChangeResult, CompressPlanResult
from btrfs.compress.domain import (
    CompressPlan,
    CompressStatusView,
    LayerName,
    LayerPlan,
    LayerStatus,
)


def format_layer_plan_lines(header: str, layers: Sequence[LayerPlan]) -> list[str]:
    """Render shared multi-layer plan header and per-layer status rows."""
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


def _emit_seam(seam_proc: str, out: TextIO) -> None:
    if seam_proc:
        print(f"test seam (CST_BTRFS_COMPRESS_PROC): /proc in {seam_proc}, not the kernel's", file=out)


def _render_plan(plan: CompressPlan, out: TextIO) -> None:
    for line in format_layer_plan_lines(f"Target compression: {plan.target_option}", plan.layers):
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
    if not res.plan.has_changes:
        print(f"Already at {res.plan.target_option} across all active layers; nothing to do.", file=out)
        return 0
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
    for msg in res.messages:
        print(msg, file=out)
    if res.error:
        print(res.error, file=err)
    return res.exit_code
