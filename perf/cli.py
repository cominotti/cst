# SPDX-License-Identifier: MPL-2.0
"""Command-line entry logic for `perf/cst-perf` and `cst perf`."""

from collections.abc import Sequence
import sys
from typing import TextIO

from perf.application import build_perf_report
from perf.domain.models import EXIT_PERF_OK, EXIT_PERF_USAGE, PERF_USAGE_TEXT, PerfCliError
from perf.domain.parsers import parse_perf_args
from perf.presenter import render_perf_report
from perf.wiring import start_perf_probe


def main(
    argv: Sequence[str] | None = None,
    *,
    doc: str = "",
    entry_file: str = "",
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run `perf` CLI with read-only probes and exit 0 (OPTIMIZED/SKIP) or 1 (findings)."""
    del doc, entry_file
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    args = sys.argv[1:] if argv is None else argv
    try:
        opts = parse_perf_args(args)
    except PerfCliError as exc:
        stderr.write(f"cst perf: error: {exc}\n")
        return EXIT_PERF_USAGE
    if opts.help_mode:
        stdout.write(PERF_USAGE_TEXT)
        return EXIT_PERF_OK
    report = build_perf_report(start_perf_probe(), opts.axes)
    return render_perf_report(report, as_json=opts.as_json, compact=opts.compact, out=stdout)
