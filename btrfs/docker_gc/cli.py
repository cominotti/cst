# SPDX-License-Identifier: MPL-2.0
"""docker-gc's command line: record, run [--dry-run], cleanup-now, status (the default), settings [--write]."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import sys

from btrfs.docker_gc import wiring
from btrfs.docker_gc.application import COMMANDS, DockerGc

USAGE = "usage: docker-gc record|run [--dry-run]|cleanup-now|status|settings [--write]"


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    service: DockerGc | None = None,
    restore_signals: Callable[[], None] | None = None,
) -> int:
    """Run docker-gc with argv (default: this process's); its exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    dry, write = "--dry-run" in args[1:], "--write" in args[1:]
    if (cmd in ("status", "settings") and not write) or (cmd == "run" and dry):
        # status, settings and a dry run only read: `| head` ends them quietly
        (wiring.quiet_sigpipe if restore_signals is None else restore_signals)()
    known = next((c for c in COMMANDS if c == cmd), None)
    if known is None:  # before the settings are read: a bad KEEP_* value never hides the usage line
        print(USAGE, file=sys.stderr)
        return 2
    gc = wiring.build_docker_gc() if service is None else service
    return gc.execute(known, dry=dry, write=write)


def main() -> int:
    """docker-gc with this program's arguments."""
    return run_cli()
