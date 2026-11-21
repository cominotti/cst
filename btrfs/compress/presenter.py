# SPDX-License-Identifier: MPL-2.0
"""Output rendering for `cst btrfs compress-force` (`status`, `plan`, `apply`, `revert`)."""

from __future__ import annotations

from typing import TextIO

from btrfs.compress.application import CompressChangeResult, CompressPlanResult
from btrfs.compress.domain import CompressPlan, CompressStatusView, LayerName, LayerStatus


def _emit_seam(seam_proc: str, out: TextIO) -> None:
    if seam_proc:
        print(f"test seam (CST_BTRFS_COMPRESS_PROC): /proc in {seam_proc}, not the kernel's", file=out)


def _render_plan(plan: CompressPlan, out: TextIO) -> None:
    print(f"Target compression: {plan.target_option}", file=out)
    for lp in plan.layers:
        match lp.status:
            case LayerStatus.CHANGE:
                marker = "CHANGE"
            case LayerStatus.OK:
                marker = "ok"
            case LayerStatus.NOT_APPLICABLE:
                marker = "skip"
            case _:
                marker = lp.status.value
        print(f"  {lp.layer.value:<10} [{marker:<6}] {lp.current} -> {lp.target} ({lp.detail})", file=out)


def render_compress_status(view: CompressStatusView, out: TextIO) -> int:
    """Render `status` output cleanly."""
    _emit_seam(view.seam_proc, out)
    print("Root btrfs compression:", file=out)
    for target, raw in view.live_entries:
        print(f"  live {target:<10} {raw}", file=out)
    if not view.live_entries:
        print("  live            (no root btrfs mount in /proc/mounts)", file=out)
    for lp in view.layers:
        if lp.layer == LayerName.LIVE:
            continue
        detail = f"(error: {view.kargs_error})" if lp.layer == LayerName.KARGS and view.kargs_error else lp.current
        print(f"  {lp.layer.value:<15} {detail}", file=out)
    if view.booted_diff is not None:
        booted, staged = view.booted_diff
        print(f"  note            booted /proc/cmdline ({booted}) differs from next-boot kargs ({staged})", file=out)
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
