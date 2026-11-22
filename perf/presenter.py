# SPDX-License-Identifier: MPL-2.0
"""Output formatting for `cst perf` and module `== diagnosis` blocks."""

from collections.abc import Sequence
import json
import sys
from typing import Final, TextIO

from perf.domain.evaluate import perf_report_to_json, verdict_exit_code
from perf.domain.models import PerfAxisReport, PerfFinding, PerfReport

_METRIC_LABEL_WIDTH: Final = 24
_SECONDS_PER_MINUTE: Final = 60
_MINUTES_PER_HOUR: Final = 60


def _format_uptime(uptime_seconds: float | None) -> str:
    if uptime_seconds is None:
        return "uptime ?"
    total_mins = int(uptime_seconds // _SECONDS_PER_MINUTE)
    hours, mins = divmod(total_mins, _MINUTES_PER_HOUR)
    return f"uptime {hours}h{mins:02d}m"


def _write_finding_lines(findings: Sequence[PerfFinding], out: TextIO, *, indent: str = "  ") -> None:
    lines: list[str] = []
    for f in findings:
        lines.append(f"{indent}[{f.verdict.value}] {f.summary}\n")
        if f.detail:
            lines.append(f"{indent}  {f.detail}\n")
        if f.recommendation:
            lines.append(f"{indent}  action: {f.recommendation}\n")
        if f.fix_command:
            lines.append(f"{indent}  fix: {f.fix_command}\n")
    out.writelines(lines)


def render_perf_findings_block(findings: Sequence[PerfFinding], out: TextIO) -> None:
    """Render concise diagnosis lines for embedding inside a module `status` report."""
    if not findings:
        out.write("  [OPTIMIZED] no bottlenecks detected on sampled knobs\n")
        return
    _write_finding_lines(findings, out, indent="  ")


def _render_compact(report: PerfReport, out: TextIO) -> None:
    axes_summary = ", ".join(f"{ax.axis.value}={ax.verdict.value}" for ax in report.axes)
    out.write(f"perf: {report.verdict.value} ({_format_uptime(report.uptime_seconds)}; {axes_summary})\n")
    all_findings = [f for ax in report.axes for f in ax.findings]
    if not all_findings:
        out.write("  [OPTIMIZED] no responsiveness bottlenecks detected\n")
        return
    for ax in report.axes:
        if ax.findings:
            _write_finding_lines(ax.findings, out, indent="  ")


def _render_axis(ax: PerfAxisReport, out: TextIO) -> None:
    out.write(f"\n== {ax.axis.value} [{ax.verdict.value}]\n")
    if not ax.metrics and not ax.findings:
        out.write("  no telemetry available on this environment\n")
        return
    out.writelines(f"  {metric.label:<{_METRIC_LABEL_WIDTH}} {metric.value}\n" for metric in ax.metrics)
    if ax.findings:
        _write_finding_lines(ax.findings, out, indent="  ")


def _render_dashboard(report: PerfReport, out: TextIO) -> None:
    axes_summary = "  ".join(f"{ax.axis.value}:{ax.verdict.value}" for ax in report.axes)
    out.write(f"perf: {report.verdict.value} ({_format_uptime(report.uptime_seconds)})\n")
    out.write(f"axes: {axes_summary}\n")
    for ax in report.axes:
        _render_axis(ax, out)
    out.write("\n== diagnosis\n")
    if not report.top_actions:
        out.write("  [OPTIMIZED] All sampled responsiveness checks look healthy.\n")
        return
    _write_finding_lines(report.top_actions, out, indent="  ")


def render_perf_report(
    report: PerfReport,
    *,
    as_json: bool = False,
    compact: bool = False,
    out: TextIO | None = None,
) -> int:
    """Render `PerfReport` to `out` and return exit code (`0` OK, `1` findings)."""
    stream = sys.stdout if out is None else out
    if as_json:
        stream.write(json.dumps(perf_report_to_json(report), indent=2) + "\n")
    elif compact:
        _render_compact(report, stream)
    else:
        _render_dashboard(report, stream)
    return verdict_exit_code(report.verdict)
