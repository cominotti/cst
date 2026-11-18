# SPDX-License-Identifier: MPL-2.0
"""The running cst-toolchains: its files, its output lines (journal-aware), the
commands it runs at idle priority, the user it runs as and the host or toolbox it
runs in.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import program_path, restore_default_signals
from cst_core.ports import CommandResult, CommandRunnerPort
from toolchains.domain.common import tilde


# The commands cst-toolchains runs (each call names its stdin, environment
# and timeout; bytes of the output that are not UTF-8 become U+FFFD, the
# runner's default).
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


def program_folder(entry_file: str) -> str:
    """The folder that holds the running program (ENTRY_FILE: its entry point's
    __file__; the zipapp's folder when installed): toolchains.conf and
    rustc-wrapper are read next to it (toolchains/ in CST, ~/.local/lib/cst for
    the timer's copy). RuntimeError for an empty ENTRY_FILE (it would name the
    parent of the current folder)."""
    if not entry_file:
        raise RuntimeError("entry_file is empty: main() passes the running program's __file__")
    return os.path.dirname(program_path(entry_file))


def journal_stdout(journal_stream: str) -> bool:
    """stdout is connected to the journal (systemd's documented check:
    JOURNAL_STREAM is "dev:inode" of that very stream; a variable inherited
    from elsewhere does not match)."""
    try:
        dev, ino = (int(x) for x in journal_stream.split(":"))
        st = os.fstat(sys.stdout.fileno())
    except ValueError, OSError:
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


class RunOutput:
    """RunOutputPort: one line at a time, flushed; with the journal priority prefix
    when run by systemd (then everything goes to stdout, the journal's stream)."""

    def __init__(self, home: str, *, journal: bool) -> None:
        self.home = home
        self.journal = journal

    def say(self, msg: str, prio: int = 6, *, err: bool = False) -> None:
        line = f"<{prio}>{msg}" if self.journal else msg
        print(line, file=sys.stderr if err and not self.journal else sys.stdout, flush=True)

    def where(self, path: str) -> str:
        return where_text(path, self.home)


def where_text(path: str, home: str) -> str:
    """PATH (with ~), and where it really is when a symlink on the way leads
    elsewhere (the ~/Bulk links of `cst btrfs bulk`)."""
    real = os.path.realpath(path)
    shown = tilde(path, home)
    if real != path:
        shown += f" -> {tilde(real, home)}"
    return shown


def run_idle_result(cmd: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float) -> CommandResult:
    """CMD at idle CPU and IO priority: ionice -c 3 when present, and nice 19
    in the child (the timer runs at Nice=19 anyway; a manual run should not
    slow builds either). A command that cannot start or does not finish within
    TIMEOUT comes back with fault and cause set."""
    io = shutil.which("ionice")
    full = ([io, "-c", "3"] if io else []) + list(cmd)
    return RUNNER.run(full, cwd=cwd, base_env=env, timeout=timeout, nice=19)


def run_idle(cmd: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    """run_idle_result, where a command that cannot start or does not finish
    within timeout raises OSError or subprocess.TimeoutExpired, as
    subprocess.run did."""
    r = run_idle_result(cmd, cwd, env, timeout)
    if r.cause is not None:
        raise r.cause
    return subprocess.CompletedProcess(list(r.argv), r.returncode, r.stdout, r.stderr)


def in_container() -> bool:
    """Inside a toolbox or container (cst_core's one detection, HostExecutionContext)."""
    return HostExecutionContext().is_container()


def real_user_is_root() -> bool:
    """The real user is root (the program refuses: it works on a user's home)."""
    return os.getuid() == 0


def home_folder(home: str | None) -> str:
    """The home folder to work on: --home HOME, or (None) the running user's."""
    return os.path.abspath(pwd.getpwuid(os.getuid()).pw_dir if home is None else home)


def output_mode(cmd: str, action: str | None) -> None:
    """A command that only reads (help, status, prune plan, config plan) gets
    SIGPIPE back at its default, so `cst toolchains status | head` ends
    quietly. The ones that change files (prune, auto, config apply) keep
    Python's handling: a closed pipe never cuts their lock release or file
    writes short."""
    if cmd in ("help", "status") or action == "plan":
        restore_default_signals()
