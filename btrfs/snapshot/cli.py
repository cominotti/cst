# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-core's command line: the argument parser and main(),
which checks the arguments, asks wiring.py for the time zone, the log and
the ports, runs one use case of application.py and renders its answer
(presenter.py).
"""

from __future__ import annotations

from collections.abc import Sequence
import argparse
import sys
from btrfs.snapshot import wiring
from btrfs.snapshot.application import cmd_caches, cmd_finish, cmd_latest, cmd_list, cmd_snapshot_or_prune
from btrfs.snapshot.domain import (
    DEFAULT_CONFIRM_HINT,
    DEFAULT_LIST_HINT,
    DEFAULT_LOCK,
    DEFAULT_PIN_FILE,
    DEFAULT_STATE_DIR,
    EXIT_USAGE,
    NAME_RE,
    LatestQuery,
    RunOptions,
    Source,
    parse_skip,
    parse_source,
    require_absolute,
)
from btrfs.snapshot.ports import LogFn, SnapshotHostPort
from btrfs.snapshot.presenter import render_caches, render_latest, render_listing
from cst_core.domain.errors import Usage
from cst_core.domain.files import checked_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="btrfs-snapshot-core", description="Local btrfs snapshots with btrbk-compatible retention."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--snapdir", required=True, help="directory holding the snapshots")
        sp.add_argument(
            "--source",
            action="append",
            required=True,
            dest="sources",
            metavar="NAME=PATH:POLICY",
            help="snapshot name, source subvolume and retention, e.g. home=/mnt/top/home:'72h 7d'",
        )
        sp.add_argument("--tz", help="time zone of the names and the retention (default: local; for tests)")
        sp.add_argument("--pin-file", default=DEFAULT_PIN_FILE, help=f"pinned paths (default {DEFAULT_PIN_FILE})")
        sp.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help=f"default {DEFAULT_STATE_DIR}")
        sp.add_argument(
            "--assume-clock-ok",
            action="store_true",
            help="skip the NTP-synchronized check (tests); the other clock guards stay",
        )
        # A caller that builds the --source arguments itself (btrfs-snapshot)
        # names its own runnable commands in the messages instead of ours.
        sp.add_argument(
            "--list-hint",
            default=DEFAULT_LIST_HINT,
            metavar="COMMAND",
            help="command named in messages to review the decisions",
        )
        sp.add_argument(
            "--confirm-hint",
            default=DEFAULT_CONFIRM_HINT,
            metavar="COMMAND",
            help="command named in messages to confirm a mass deletion",
        )

    def caches(sp: argparse.ArgumentParser, *, exclude: bool = True) -> None:
        if exclude:
            sp.add_argument(
                "--exclude-caches",
                action="store_true",
                help="take new snapshots writable, drop their CACHEDIR.TAG folders, then make them read-only",
            )
        sp.add_argument(
            "--caches-skip",
            action="append",
            default=[],
            metavar="NAME=REL",
            help="a path in source NAME (relative to its root, '.' = all of it) kept whole: "
            "never entered by the cache walk",
        )

    for name, helptext in (
        ("snapshot", "create changed snapshots, then prune"),
        ("prune", "prune only"),
        ("finish", "only finish snapshots an interrupted run left writable"),
    ):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
        sp.add_argument("--dry-run", action="store_true", help="show what would be created/deleted")
        if name != "finish":
            sp.add_argument("--allow-mass-delete", action="store_true", help="disable the circuit breaker")
        sp.add_argument("--lock", default=DEFAULT_LOCK, type=checked_path, help=f"lock file (default {DEFAULT_LOCK})")
        sp.add_argument("--no-lock", action="store_true", help="the caller already holds the lock")
        caches(sp)
        if name == "snapshot":
            sp.add_argument("--force", action="store_true", help="create even if unchanged")
    sp = sub.add_parser("list", help="list snapshots with the retention decision")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("caches", help="the cache folders --exclude-caches would drop from the next snapshots")
    common(sp)
    caches(sp, exclude=False)
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("latest", help="print the newest snapshot of NAME")
    sp.add_argument("--snapdir", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--source-path", help="also require the snapshot's Parent UUID to be this subvolume's UUID")
    return p


def parse_sources(args: argparse.Namespace) -> tuple[Source, ...]:
    """The --source values, each with its --caches-skip paths; Usage when two share a NAME or PATH."""
    sources = [parse_source(s) for s in args.sources]
    names = [s.name for s in sources]
    paths = [s.path for s in sources]
    if len(set(names)) != len(names) or len(set(paths)) != len(paths):
        raise Usage("each --source needs its own NAME and PATH")
    for text in getattr(args, "caches_skip", []):
        name, rel = parse_skip(text, names)
        sources[names.index(name)].skips.append(rel)
    return tuple(sources)


def run_options(args: argparse.Namespace, sources: tuple[Source, ...]) -> RunOptions:
    """The checked command line of snapshot, prune, finish, list and caches."""
    return RunOptions(
        cmd=args.cmd,
        snapdir=args.snapdir,
        sources=sources,
        pin_file=args.pin_file,
        state_dir=args.state_dir,
        assume_clock_ok=args.assume_clock_ok,
        list_hint=getattr(args, "list_hint", DEFAULT_LIST_HINT),
        confirm_hint=getattr(args, "confirm_hint", DEFAULT_CONFIRM_HINT),
        dry_run=getattr(args, "dry_run", False),
        allow_mass_delete=getattr(args, "allow_mass_delete", False),
        force=getattr(args, "force", False),
        exclude_caches=getattr(args, "exclude_caches", False),
        lock=getattr(args, "lock", DEFAULT_LOCK),
        no_lock=getattr(args, "no_lock", False),
    )


def latest(args: argparse.Namespace, host: SnapshotHostPort | None, log: LogFn) -> int:
    require_absolute("--source-path", args.source_path)
    if not NAME_RE.fullmatch(args.name):
        raise Usage("--name must match [A-Za-z0-9._-]+")
    machine = wiring.machine(host, log, state_dir=DEFAULT_STATE_DIR, pin_file=DEFAULT_PIN_FILE)
    status, path = cmd_latest(LatestQuery(args.snapdir, args.name, args.source_path), machine)
    render_latest(path)
    return status


def dispatch(args: argparse.Namespace, host: SnapshotHostPort | None, log: LogFn) -> int:
    require_absolute("--snapdir", args.snapdir)
    if args.cmd == "latest":
        return latest(args, host, log)
    tz = wiring.run_time_zone(args.tz)
    options = run_options(args, parse_sources(args))
    machine = wiring.machine(host, log, state_dir=options.state_dir, pin_file=options.pin_file)
    if args.cmd == "list":
        status, listing = cmd_list(options, machine, tz)
        render_listing(listing, as_json=args.json)
        return status
    if args.cmd == "caches":
        status, caches = cmd_caches(options, machine, tz)
        render_caches(caches, as_json=args.json)
        return status
    if args.cmd == "finish":
        return cmd_finish(options, machine, tz)
    return cmd_snapshot_or_prune(options, machine, tz, create=args.cmd == "snapshot")


def main(argv: Sequence[str] | None = None, host: SnapshotHostPort | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = wiring.journal(sys.stderr if args.cmd in ("latest", "caches") or getattr(args, "json", False) else sys.stdout)
    try:
        return dispatch(args, host, log)
    except Usage as e:
        parser.print_usage(sys.stderr)
        print(f"btrfs-snapshot-core: {e}", file=sys.stderr)
        return EXIT_USAGE
