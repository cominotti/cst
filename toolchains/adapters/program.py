# SPDX-License-Identifier: MPL-2.0
"""The running cst-toolchains: its name in hints, its files, its output lines
(journal-aware), the commands it runs at idle priority, and the host or
toolbox it runs in.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import program_path, restore_default_signals
from cst_core.ports import CommandRunnerPort
from toolchains.domain.common import tilde


# How hints name the frontend ("cst toolchains" through cst).
PROG = os.environ.get("CST_PROG") or "cst toolchains"


# The commands cst-toolchains runs (each call names its stdin, environment
# and timeout; output that is not UTF-8 raises, as text=True did).
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


class Program:
    """The running cst-toolchains. main() sets entry_file (its entry point's
    __file__); toolchains.conf and rustc-wrapper are read next to the program:
    toolchains/ in CST, ~/.local/lib/cst for the timer's copy (a zipapp)."""

    entry_file: str = ""

    @classmethod
    def folder(cls) -> str:
        """The folder that holds the program (the zipapp's folder when installed)."""
        if not cls.entry_file:
            raise RuntimeError("Program.entry_file is not set: main() sets it")
        return os.path.dirname(program_path(cls.entry_file))


def journal_stdout() -> bool:
    """stdout is connected to the journal (systemd's documented check:
    JOURNAL_STREAM is "dev:inode" of that very stream; a variable inherited
    from elsewhere does not match)."""
    try:
        dev, ino = (int(x) for x in os.environ.get("JOURNAL_STREAM", "").split(":"))
        st = os.fstat(sys.stdout.fileno())
    except ValueError, OSError:
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


JOURNAL = journal_stdout()


def say(msg: str, prio: int = 6, err: bool = False) -> None:
    """One line; with the journal priority prefix when run by systemd."""
    line = f"<{prio}>{msg}" if JOURNAL else msg
    print(line, file=sys.stderr if err and not JOURNAL else sys.stdout, flush=True)


def where_text(path: str, home: str) -> str:
    """PATH (with ~), and where it really is when a symlink on the way leads
    elsewhere (the ~/Bulk links of `cst btrfs bulk`)."""
    real = os.path.realpath(path)
    shown = tilde(path, home)
    if real != path:
        shown += f" -> {tilde(real, home)}"
    return shown


def run_idle(cmd: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    """cmd at idle CPU and IO priority: ionice -c 3 when present, and nice 19
    in the child (the timer runs at Nice=19 anyway; a manual run should not
    slow builds either). A command that cannot start or does not finish
    within timeout raises OSError or subprocess.TimeoutExpired, as
    subprocess.run did."""
    io = shutil.which("ionice")
    full = ([io, "-c", "3"] if io else []) + list(cmd)
    r = RUNNER.run(full, cwd=cwd, base_env=env, timeout=timeout, nice=19)
    if r.cause is not None:
        raise r.cause
    return subprocess.CompletedProcess(full, r.returncode, r.stdout, r.stderr)


def time_left(deadline: float | None) -> float | None:
    """Seconds until DEADLINE (time.monotonic()), or None without one."""
    return None if deadline is None else deadline - time.monotonic()


def in_container() -> bool:
    """Inside a toolbox or container (cst_core's one detection, HostExecutionContext)."""
    return HostExecutionContext().is_container()


def run_context(home: str | None) -> tuple[str, float]:
    """(the home folder to work on: --home or the user's, now: CST_TOOLCHAINS_NOW in tests)."""
    return (
        os.path.abspath(pwd.getpwuid(os.getuid()).pw_dir if home is None else home),
        float(os.environ.get("CST_TOOLCHAINS_NOW") or time.time()),
    )


def output_mode(cmd: str, action: str | None) -> None:
    """A command that only reads (help, status, prune plan, config plan) gets
    SIGPIPE back at its default, so `cst toolchains status | head` ends
    quietly. The ones that change files (prune, auto, config apply) keep
    Python's handling: a closed pipe never cuts their lock release or file
    writes short."""
    if cmd in ("help", "status") or action == "plan":
        restore_default_signals()
