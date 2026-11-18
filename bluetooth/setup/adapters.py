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
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
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


def die_by_interrupt() -> NoReturn:
    """End killed by SIGINT, as the shell did on Ctrl-C."""
    sys.stdout.flush()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)
    raise SystemExit(130)
