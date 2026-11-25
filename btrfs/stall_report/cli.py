# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report: printing the text report, the arguments and main()."""

import argparse
import json
import sys

from btrfs.stall_report.adapters import LOG_DIR, analyse, container_names, load, report_refusal, report_window
from btrfs.stall_report.domain.records import UNITS, AnalyseResult, MetaRecord
from btrfs.stall_report.domain.report import report_lines
from btrfs.stall_report.domain.verdict import verdict
from cst_core.adapters.runtime import restore_default_signals


def print_report(res: AnalyseResult, meta: MetaRecord, *, verbose: bool) -> None:
    for line in report_lines(res, meta, container_names, verbose=verbose):
        print(line)


def main() -> None:
    restore_default_signals()  # a read-only report (and --help): `| head` ends it quietly
    ap = argparse.ArgumentParser(
        prog="btrfs-stall-report", description="Summarise recorded IO/btrfs stalls for a time window."
    )
    ap.add_argument(
        "--since",
        "-s",
        default="30 min ago",
        help="start: date -d syntax ('20 min ago', '10:31') or @epoch; default 30 min ago",
    )
    ap.add_argument("--until", "-u", default=None, help="end; default now")
    ap.add_argument("--json", action="store_true", help="machine-readable output (verdict first)")
    ap.add_argument("-v", "--verbose", action="store_true", help="more episodes, dm-crypt rows, example stacks")
    ap.add_argument("--no-below", action="store_true", help="skip below's per-process section (faster)")
    ap.add_argument("--log-dir", default=LOG_DIR)
    args = ap.parse_args()
    since, until = report_window(args.since, args.until)
    loaded = load(args.log_dir, since, until)
    if (refusal := report_refusal(args.log_dir, loaded)) is not None:
        sys.exit(refusal)
    res, meta = analyse(loaded, since, until, not args.no_below, args.verbose)
    if args.json:
        out: dict[str, object] = {"verdict": verdict(res, meta), "coverage": res.pop("coverage"), "units": UNITS}
        out.update(res)
        json.dump(out, sys.stdout, indent=1, default=str)
        print()
    else:
        print_report(res, meta, verbose=args.verbose)
