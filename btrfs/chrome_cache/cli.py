# SPDX-License-Identifier: MPL-2.0
"""chrome-cache's commands (status, plan, apply, off) and main()."""

from __future__ import annotations

from collections.abc import Sequence
import os
import sys
from typing import assert_never
from btrfs.chrome_cache.adapters import (
    allocated_bytes,
    apply,
    flags_text,
    installed,
    is_subvol,
    off,
    old_cache,
    owned_by_caller,
    plan_line,
    ram_cache,
    real_home,
)
from btrfs.chrome_cache.domain import (
    ABSENT_ADDS,
    CACHE_SUBVOL_REL,
    FLAGS_REL,
    OLD_CACHE_REL,
    ChromeCacheCommand,
    ChromeFlagsState,
    decide,
    parse_chrome_args,
    size_text,
)
from cst_core.adapters.runner import HostExecutionContext
from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.errors import Refused
from cst_core.domain.values import MIB


def status(home: str) -> int:
    print("Chrome (Flatpak): HTTP cache in the sandbox /tmp (RAM, emptied at reboot), at most 512 MiB")
    inst = installed(home)
    print(f"  Chrome Flatpak:        {'not installed (nothing to do)' if inst is None else f'installed ({inst})'}")
    rc = 0
    try:
        state, _, msg = decide(flags_text(home))
        if state == ChromeFlagsState.ABSENT:
            msg = "absent" if inst is None else ABSENT_ADDS
        elif state == ChromeFlagsState.CAP_MISSING:
            msg = "the 512 MiB cap line is missing: 'snapshots chrome-cache apply' adds it back"
        print(f"  ~/{FLAGS_REL}:")
        print(f"                         {msg}")
    except (Refused, OSError, ValueError) as e:
        print(f"  ~/{FLAGS_REL}: NEEDS A LOOK: {e}")
        state, rc = None, 1
    ram = ram_cache()
    if ram is not None:
        print(f"  in RAM now:            {size_text(allocated_bytes(ram))} ({ram})")
    http, code, profiles = old_cache(home)
    sub = is_subvol(os.path.join(home, CACHE_SUBVOL_REL))
    where = "" if sub else ", in /home snapshots"
    if state in (ChromeFlagsState.MANAGED, ChromeFlagsState.USER_SAME) and http >= MIB:
        print(f"  old on-disk HTTP cache: {size_text(http)} in {profiles} profile(s){where}. Quit Chrome")
        print("                         (all windows), start it again, then delete it (never done automatically):")
        print(f"                         rm -rf ~/{OLD_CACHE_REL}/*/Cache")
    elif http:
        print(f"  on-disk HTTP cache:    {size_text(http)} in {profiles} profile(s) (~/{OLD_CACHE_REL}/*/Cache)")
    if code:
        print(f"  Code Cache on disk:    {size_text(code)} (~/{OLD_CACHE_REL}/*/Code Cache; whether the flag")
        print("                         moves it too is not verified: btrfs/SNAPSHOTS.md, section 3, Chrome)")
    if sub:
        print(f"  ~/{CACHE_SUBVOL_REL}: a nested subvolume from an earlier setup: harmless (left out")
        print("                         of snapshots), no longer needed; to make it a plain folder again see")
        print("                         btrfs/SNAPSHOTS.md, section 3, Chrome")
    return rc


def _run_apply(home: str) -> int:
    state, msg, changed = apply(home)
    where = f"~/{FLAGS_REL}: " if state != ChromeFlagsState.NO_APP else ""
    print(f"{'written:' if changed else 'kept:':<10} {where}{msg}")
    if changed:
        print("           Takes effect when Chrome starts again (quit all its windows first).")
    return 0


def _run_off(home: str) -> int:
    msg, changed, still_set = off(home)
    print(f"{'written:' if changed else 'kept:':<10} ~/{FLAGS_REL}: {msg}")
    if changed and not still_set:
        print("           Takes effect when Chrome starts again.")
    return 0


def main(argv: Sequence[str]) -> int:
    parsed = parse_chrome_args(argv[1:])
    if parsed is None:
        print("usage: chrome-cache status|plan|apply|off [--home DIR]", file=sys.stderr)
        return 2
    cmd, home = parsed
    if HostExecutionContext().is_root():
        print("chrome-cache: runs as the user, not root (btrfs-setup.sh calls it through runuser)", file=sys.stderr)
        return 2
    home = real_home(home)
    try:
        if not owned_by_caller(home):
            print(f"chrome-cache: {home} is not yours", file=sys.stderr)
            return 2
    except OSError as e:
        print(f"chrome-cache: {home}: {e}", file=sys.stderr)
        return 2
    if cmd in (ChromeCacheCommand.STATUS, ChromeCacheCommand.PLAN):
        restore_default_signals()  # they only read: `| head` ends them quietly; apply and off keep Python's handling
    try:
        match cmd:
            case ChromeCacheCommand.STATUS:
                return status(home)
            case ChromeCacheCommand.PLAN:
                print(plan_line(home))
                return 0
            case ChromeCacheCommand.APPLY:
                return _run_apply(home)
            case ChromeCacheCommand.OFF | ChromeCacheCommand.UNDO:
                return _run_off(home)
            case _:
                assert_never(cmd)
    except (Refused, OSError, ValueError) as e:
        print(f"NEEDS A LOOK: chrome-cache: {e}", file=sys.stderr)
        return 1
