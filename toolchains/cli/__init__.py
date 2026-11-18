# SPDX-License-Identifier: MPL-2.0
"""The commands of cst-toolchains (`cst toolchains ...`): main() parses the
command line and runs one command group (prune, auto, config, status) under
the run lock where it changes files. The program toolchains/cst-toolchains
only hands its arguments to main().
"""

from __future__ import annotations

import fcntl
import os
import sys

from collections.abc import Callable, Sequence

from cst_core.adapters.runner import on_host_argv
from cst_core.adapters.runtime import program_argv
from cst_core.domain.errors import Refused, Usage
from cst_core.ports import WriteFailed
from toolchains.domain.common import ConfigError, NAME, SERVICE, STATE_DIR_REL, USAGE, parse_args
from toolchains.adapters.program import PROG, Program, in_container, output_mode, run_context, say
from toolchains.cli import auto, config, prune, status


def run_locked(home: str, fn: Callable[[], int], busy_refused: bool = False) -> int:
    """One run at a time (the timer and a manual apply, auto on/off). When
    another run holds the lock: nothing done, exit 0 (a prune that finds
    another prune running), or Refused with busy_refused (auto on/off: what
    was asked did not happen)."""
    path = os.path.join(home, STATE_DIR_REL, "toolchains.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if busy_refused:
                raise Refused(
                    "another run is in progress (the hourly prune, or another auto on/off): nothing done; "
                    f"run this again once it has finished (journalctl --user -u {SERVICE} shows the prune)"
                ) from None
            say("another run is in progress: nothing done", 5)
            return 0
        return fn()
    finally:
        os.close(fd)


def main(argv: Sequence[str], doc: str, entry_file: str) -> int:
    """`cst toolchains ...` (argv without the program name). doc is the
    program's help; entry_file the running cst-toolchains."""
    Program.entry_file = entry_file
    try:
        a = parse_args(argv)
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    output_mode(a["cmd"], a["action"])
    if a["cmd"] == "help":
        print(doc)
        return 0
    if os.getuid() == 0:
        print(f"{NAME}: run as your user, not as root", file=sys.stderr)
        return 1
    if in_container() and not os.environ.get("CST_TOOLCHAINS_HERE"):
        # From a toolbox, the processes of other containers cannot be
        # inspected: the same program runs on the host (same home path).
        try:
            spawn = on_host_argv([*program_argv(entry_file), *argv], {"CST_PROG": PROG})
            os.execvp(spawn[0], spawn)
        except OSError as e:
            print(f"{NAME}: cannot run on the host (flatpak-spawn: {e.strerror}); run it on the host", file=sys.stderr)
            return 1
    home, now = run_context(a["home"])
    cmd, action = a["cmd"], a["action"]
    try:
        if cmd == "status":
            return status.cmd_status(home, now, a["brief"])
        assert action is not None
        if cmd == "config":
            return config.cmd_config(home, action, a["lean"], a["sccache"])
        if cmd == "prune" and action == "plan":
            return prune.cmd_prune(home, now, "plan", a["targets"])
        if cmd == "prune":
            return run_locked(home, lambda: prune.cmd_prune(home, now, "apply", a["targets"]))
        if action == "on":
            return run_locked(home, lambda: auto.cmd_auto_on(home, now, a["unless_off"]), busy_refused=True)
        return run_locked(home, lambda: auto.cmd_auto_off(home, now), busy_refused=True)
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    except (Refused, ConfigError, WriteFailed) as e:
        say(f"{NAME}: {e}", 3, err=True)
        return 1
