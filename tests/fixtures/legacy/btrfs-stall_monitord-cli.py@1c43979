# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-monitord's arguments and main()."""

from __future__ import annotations

from collections.abc import Sequence
import os

from btrfs.stall_monitord.adapters.monitor import Monitor
from btrfs.stall_monitord.adapters.proc import prepare_log_dir


def main(argv: Sequence[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="btrfs-stall-monitord", description="btrfs/IO stall telemetry daemon (read-only)")
    ap.add_argument("--log-dir", default=os.environ.get("LOGS_DIRECTORY", "/var/log/btrfs-stall-monitor"))
    ap.add_argument("--run-dir", default=os.environ.get("RUNTIME_DIRECTORY", ""))
    ap.add_argument("--duration", type=float, help="stop after N seconds (testing)")
    args = ap.parse_args(argv)
    prepare_log_dir(args.log_dir)
    Monitor(args.log_dir, args.run_dir, args.duration).run()
    return 0
