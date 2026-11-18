# SPDX-License-Identifier: MPL-2.0
"""The terminal and the user's files of `cst bluetooth`: ConsolePort and SetupFilesPort on this machine."""

import contextlib
import glob
import os
import pwd
import shutil
import signal
import stat
import sys
from collections.abc import Callable
from types import FrameType
from typing import NoReturn

from cst_core.adapters.files import LocalFileSystemAdapter, write_atomic


class SetupTerminal:
    """SetupConsolePort on this process's stdout, stderr and stdin; every write is flushed at once,
    so it comes before the output of a command that runs next."""

    def out(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def err(self, text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    def out_closed(self) -> None:
        drop_stdout()

    def ask(self, prompt: str) -> str | None:
        """bash's read -rp: the prompt on stderr, one line, blanks around it dropped; None without a terminal."""
        if not sys.stdin.isatty():
            return None
        self.err(prompt)
        return sys.stdin.readline().rstrip("\n").strip(" \t")


class LocalSetupFiles(LocalFileSystemAdapter):
    """SetupFilesPort on this machine's files, with the rights of this process."""

    def contains(self, path: str, text: str, *, quiet: bool) -> bool:
        try:
            with open(path, "rb") as f:
                return text.encode("utf-8") in f.read()
        except OSError as e:
            if not quiet:
                sys.stderr.write(f"grep: {path}: {e.strerror}\n")
            return False

    def which(self, name: str) -> bool:
        return shutil.which(name) is not None

    def write_user_file(self, path: str, text: str, mode: int) -> None:
        write_atomic(path, text, mode=mode, durable=False, relabel=False)

    def write_through(self, path: str, text: str) -> None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
        write_atomic(path, text, mode=mode, durable=False, relabel=False)

    def copy(self, src: str, dest: str) -> None:
        shutil.copy2(src, dest)

    def remove_empty_dir(self, path: str) -> None:
        with contextlib.suppress(OSError):
            os.rmdir(path)

    def make_dirs(self, path: str) -> None:
        # mkdir -p: the folders it creates get the umask's mode, as the shell's did.
        os.makedirs(path, exist_ok=True)

    def backups(self, path: str) -> list[tuple[float, str]]:
        return [(os.lstat(p).st_mtime, p) for p in glob.glob(glob.escape(path) + ".bak-*")]

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)


def effective_uid() -> int:
    """This process's effective user ID (0: root)."""
    return os.geteuid()


def account(user: str) -> tuple[int, str] | None:
    """(uid, home folder) of user from the password database, or None when there is no such user."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_dir


def die_by_signal(signum: int) -> NoReturn:
    """End killed by the signal, as the shell did (Ctrl-C: SIGINT; a closed stdout: SIGPIPE)."""
    with contextlib.suppress(OSError):
        sys.stdout.flush()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    raise SystemExit(128 + signum)


def drop_stdout() -> None:
    """stdout from here on is /dev/null (its reader went away)."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)


def print_exit(out: str, err: str, status: int) -> int:
    """out on stdout, err on stderr; status, or 141 when stdout's reader went away (as the shell's
    help, whose sed died by SIGPIPE in a pipeline under set -e and pipefail)."""
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        sys.stdout.write(out)
        sys.stdout.flush()
    except BrokenPipeError:
        drop_stdout()
        status = 128 + signal.SIGPIPE
    sys.stderr.write(err)
    return status


def on_fatal_signals(then: Callable[[], None]) -> None:
    """SIGHUP and SIGTERM: then(), and the program ends killed by that signal at once (a
    command it runs goes on to its end); a write to a closed stdout raises BrokenPipeError
    instead of killing it (SIGPIPE ignored here; the commands it runs get the default)."""

    def fatal(signum: int, _frame: FrameType | None) -> None:
        then()
        die_by_signal(signum)

    for sig in (signal.SIGHUP, signal.SIGTERM):
        if signal.getsignal(sig) is not signal.SIG_IGN:  # ignored on entry (nohup) stays ignored, as in bash
            signal.signal(sig, fatal)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def guarded[T](command: Callable[[], T], cleanup: Callable[[], None]) -> T:
    """command(), with cleanup() however it ends, as a shell's EXIT trap: Ctrl-C, SIGHUP and
    SIGTERM end the program killed by that signal, and so does a closed stdout (SIGPIPE, as
    the shell died at its next write there)."""
    on_fatal_signals(cleanup)
    try:
        return command()
    except KeyboardInterrupt:
        cleanup()
        die_by_signal(signal.SIGINT)
    except BrokenPipeError:
        cleanup()
        die_by_signal(signal.SIGPIPE)
    finally:
        cleanup()
