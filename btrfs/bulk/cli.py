# SPDX-License-Identifier: MPL-2.0
"""btrfs-bulk's command line: plan, apply, status, boot, keep/unkeep and
steam plan/apply; main()."""

from __future__ import annotations

from collections.abc import Sequence
import sys
from typing import assert_never

from btrfs.bulk.application.commands import boot_run, keep_ids, keep_report, steam_apply, steam_print, steam_survey
from btrfs.bulk.application.items import apply, exit_status, report
from btrfs.bulk.domain import BY_ID, BadSettings, BulkCommand, NoUser, bulk_command, split_user_args
from btrfs.bulk.domain.steam import MAKE_DEFAULT, SteamArgs, parse_steam_args
from btrfs.bulk.wiring import Wiring

USAGE = (
    "usage: btrfs-bulk plan|apply|status [--user NAME] | boot | keep|unkeep ID... [--user NAME]"
    " | steam plan|apply [--app ID]... [--copy] [--user NAME]"
)


def steam_cmd(w: Wiring, args: Sequence[str]) -> int:
    """btrfs-bulk steam plan|apply [--app ID]... [--copy] [--user NAME]"""
    parsed = parse_steam_args(args)
    if not isinstance(parsed, SteamArgs):
        for line in parsed:
            print(line, file=sys.stderr)
        return 2
    sub, ids, allow_copy = parsed.sub, list(parsed.ids), parsed.allow_copy
    b = w.bulk(parsed.user_args)
    if sub == BulkCommand.PLAN:
        sv = steam_survey(b, ids, allow_copy)
        steam_print(b, sv)
        if not sv["fatal"]:
            print(MAKE_DEFAULT)
        print(
            "Dry run, nothing changed. Do it with: cst btrfs bulk steam apply"
            + "".join(f" --app {i}" for i in ids)
            + (" --copy" if sv["copy_bytes"] else "")
            + " (Steam closed)"
        )
        return 0
    return steam_apply(b, ids, allow_copy)


def keep_cmd(w: Wiring, cmd: str, args: Sequence[str]) -> int:
    """btrfs-bulk keep|unkeep ID... [--user NAME]"""
    ids, user_args = split_user_args(args)
    bad = [i for i in ids if i not in BY_ID]
    if not ids or bad:
        if bad:
            print(f"btrfs-bulk: unknown item: {' '.join(bad)}", file=sys.stderr)
        print(f"usage: btrfs-bulk {cmd} ID... ; valid ids: {', '.join(BY_ID)}", file=sys.stderr)
        return 2
    error = keep_ids(w.state(), cmd, ids)
    if error is not None:
        print(f"btrfs-bulk: cannot update {w.paths.keep}: {error}", file=sys.stderr)
        return 1
    keep_report(w.tree, w.out, w.keep_ctx(user_args), cmd, ids)
    return 0


def main(argv: Sequence[str], entry_file: str, *, wiring: Wiring | None = None) -> int:
    """btrfs-bulk with argv (argv[0] the program); entry_file is the running
    entry point's __file__ (where btrfs-snapshot is found). Whose home it is
    unknown (a bad bulk.conf line, no user as root): the message on stderr,
    exit 1 (BOOT_REFUSED_EXIT at boot, from boot_run)."""
    w = Wiring.from_environment(entry_file) if wiring is None else wiring
    try:
        return run(w, argv)
    except (BadSettings, NoUser) as e:
        print(e, file=sys.stderr)
        return 1


def run(w: Wiring, argv: Sequence[str]) -> int:
    """One command of btrfs-bulk (argv[0] the program)."""
    cmd = bulk_command(argv[1] if len(argv) > 1 else BulkCommand.STATUS)
    match cmd:
        case BulkCommand.KEEP | BulkCommand.UNKEEP:
            return keep_cmd(w, cmd, argv[2:])
        case BulkCommand.STEAM:
            return steam_cmd(w, argv[2:])
        case BulkCommand.BOOT:
            return boot_run(lambda: w.bulk(argv[2:]), w.host, lambda s: print(s, file=sys.stderr))
        case None:
            print(USAGE, file=sys.stderr)
            return 2
    b = w.bulk(argv[2:])
    match cmd:
        case BulkCommand.PLAN | BulkCommand.STATUS:
            report(b, cmd)
            return 0
        case BulkCommand.APPLY:
            pass  # below
        case _:
            assert_never(cmd)
    b.boots.forget()  # a new request gets its two boots again
    deferred, problems = apply(b)
    if deferred:
        print(
            f"{len(deferred)} item(s) in use now ({', '.join(deferred)}): moved at the next boot, before login "
            "(or close what uses them and run 'cst btrfs bulk apply' again)."
        )
    if problems:
        print(
            f"{len(problems)} item(s) need a look ({', '.join(problems)}): see 'cst btrfs bulk status'; "
            "nothing was changed there."
        )
    return exit_status(b, deferred, problems)
