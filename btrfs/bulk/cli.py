# SPDX-License-Identifier: MPL-2.0
"""btrfs-bulk's command line: plan, apply, status, boot, keep/unkeep and
steam plan/apply; main()."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.bulk.application.commands import boot, keep_ids, keep_report, steam_apply, steam_print, steam_survey
from btrfs.bulk.application.items import apply, exit_status, report
from btrfs.bulk.domain import BY_ID, DECIMAL, MAKE_DEFAULT, STEAM_USAGE, BadSettings
from btrfs.bulk.wiring import Wiring

USAGE = (
    "usage: btrfs-bulk plan|apply|status [--user NAME] | boot | keep|unkeep ID... [--user NAME]"
    " | steam plan|apply [--app ID]... [--copy] [--user NAME]"
)


def steam_cmd(w: Wiring, args: Sequence[str]) -> int:
    """btrfs-bulk steam plan|apply [--app ID]... [--copy] [--user NAME]"""
    sub = args[0] if args else ""
    ids: list[str] = []
    user_args: list[str] = []
    i = 1
    allow_copy = False
    while i < len(args):
        a = args[i]
        if a == "--copy":
            allow_copy = True
            i += 1
            continue
        if a == "--app" and i + 1 < len(args) and DECIMAL.fullmatch(args[i + 1]) and len(args[i + 1]) <= 10:
            ids.append(str(int(args[i + 1])))
        elif a == "--user" and i + 1 < len(args):
            user_args = [a, args[i + 1]]
        else:
            print(
                f"btrfs-bulk steam: bad argument {a!r}"
                + (f" {args[i + 1]!r}" if a == "--app" and i + 1 < len(args) else ""),
                file=sys.stderr,
            )
            print(STEAM_USAGE, file=sys.stderr)
            return 2
        i += 2
    if sub not in ("plan", "apply"):
        print(STEAM_USAGE, file=sys.stderr)
        return 2
    b = w.bulk(user_args)
    if sub == "plan":
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
    if "--user" in args:
        i = args.index("--user")
        ids, user_args = list(args[:i]) + list(args[i + 2 :]), list(args[i : i + 2])
    else:
        ids, user_args = list(args), []
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
    entry point's __file__ (where btrfs-snapshot is found)."""
    w = Wiring.from_environment(entry_file) if wiring is None else wiring
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd in ("keep", "unkeep"):
        return keep_cmd(w, cmd, argv[2:])
    if cmd == "steam":
        return steam_cmd(w, argv[2:])
    if cmd not in ("plan", "apply", "status", "boot"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        b = w.bulk(argv[2:])
    except BadSettings as e:
        if cmd == "boot":  # before any login: the alert waits for it (btrfs-notify keeps it)
            w.host.alert(
                "~/Bulk: nothing moved at boot",
                f"{e.lines}. Fix the line (cst btrfs install writes the file); log: journalctl -u btrfs-bulk-boot",
            )
        raise
    if cmd in ("plan", "status"):
        report(b, cmd)
        return 0
    if cmd == "boot":
        return boot(b)
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
