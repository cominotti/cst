# SPDX-License-Identifier: MPL-2.0
"""Command-line entry logic for `perf/cst-perf` and `cst perf`."""

from collections.abc import Sequence
import sys
from typing import TextIO

from perf.application import build_perf_report
from perf.domain.models import EXIT_PERF_OK, EXIT_PERF_USAGE, PerfCliError, format_perf_usage
from perf.domain.parsers import parse_perf_args
from perf.presenter import render_perf_report
from perf.wiring import start_perf, start_perf_probe


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
    prog = start_perf()
    stdout, stderr = sys.stdout if out is None else out, sys.stderr if err is None else err
    try:
        opts = parse_perf_args(sys.argv[1:] if argv is None else argv)
    except PerfCliError as exc:
        stderr.write(f"{prog}: error: {exc}\n")
        return EXIT_PERF_USAGE
    if opts.help_mode:
        stdout.write(format_perf_usage(prog))
        return EXIT_PERF_OK
    rep = build_perf_report(start_perf_probe(), opts.axes)
    return render_perf_report(rep, as_json=opts.as_json, compact=opts.compact, out=stdout)
