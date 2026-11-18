# SPDX-License-Identifier: MPL-2.0
"""The host side of the `cst` dispatcher: files, users, and running a module the way bash did."""

from collections.abc import Mapping, Sequence
import os
import pwd
import shutil
import signal
import subprocess
import sys
from typing import Literal

from cst_core.adapters.runner import HostExecutionContext, can_spawn_on_host, on_host_argv
from cst_core.domain.errors import shell_exit_status
from cst_core.ports import ContainerProbePort

# The signals a bash script waiting for a foreground command leaves to that
# command: the command may handle them (a cleanup trap). bash dies of SIGINT
# only if it got it too and the command died of it; SIGQUIT bash ignores
# itself, so a command killed by it is only a failure (128+3).
_FOREGROUND_SIGNALS = (signal.SIGINT, signal.SIGQUIT)


def ignore_quit() -> None:
    """cst itself ignores Ctrl-\\ (SIGQUIT) the whole time, as bash does; the programs it starts get it at its default.

    A Python handler that does nothing, not SIG_IGN: exec resets a handler to
    the default but keeps an ignore. A SIGQUIT cst started with ignored stays
    ignored (and so for its programs), as in bash.
    """
    if signal.getsignal(signal.SIGQUIT) is not signal.SIG_IGN:
        signal.signal(signal.SIGQUIT, lambda _sig, _frame: None)


def default_ctrl_c() -> None:
    """Ctrl-C ends this process the way it ended the bash script it replaces (killed by
    SIGINT, no KeyboardInterrupt traceback). A Ctrl-C ignored by the caller stays ignored."""
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, signal.SIG_DFL)


def _start_failure(argv: Sequence[str], exc: OSError) -> int:
    """Report a command that could not start, as bash does: 127 when it is not found, else 126."""
    print(f"{argv[0]}: {exc.strerror}", file=sys.stderr, flush=True)
    return 127 if isinstance(exc, FileNotFoundError) else 126


class LocalCstDispatchAdapter:
    """Concrete `CstDispatchPort` backed by the host OS, with bash's exec and foreground semantics."""

    def __init__(self, probe: ContainerProbePort | None = None) -> None:
        """probe: host or container (None: HostExecutionContext, the one detection)."""
        self._probe = HostExecutionContext() if probe is None else probe

    def is_file(self, path: str) -> bool:
        """Return True when `path` exists as a regular file."""
        return os.path.isfile(path)

    def is_container(self) -> bool:
        """Return True inside a Podman toolbox or Docker container."""
        return self._probe.is_container()

    def euid(self) -> int:
        """Return effective user ID."""
        return os.geteuid()

    def environ(self) -> Mapping[str, str]:
        """Return a snapshot of the process environment."""
        return dict(os.environ)

    def user_passwd(self, user: str) -> tuple[int, str]:
        """Return `(uid, home_dir)` for `user`."""
        entry = pwd.getpwnam(user)
        return entry.pw_uid, entry.pw_dir

    def which(self, name: str, path: str | None) -> str | None:
        """shutil.which(name, path=path)."""
        return shutil.which(name, path=path)

    def realpath(self, path: str) -> str:
        """os.path.realpath."""
        return os.path.realpath(path)

    def exec_on_host(self, argv: Sequence[str], *, settings: Mapping[str, str]) -> int | None:
        """exec argv on the host through flatpak-spawn (with settings), or None when it is not on PATH."""
        if not can_spawn_on_host(os.environ.get("PATH")):
            return None
        return self.exec_replace(on_host_argv(argv, settings))

    def read_text(self, path: str) -> str:
        """The file's bytes, decoded as the file system's names are (every byte kept)."""
        with open(path, "rb") as fh:
            return os.fsdecode(fh.read())

    def say(self, stream: Literal["out", "err"], text: str) -> None:
        """text encoded back as read_text decoded it, written to stdout or stderr and flushed."""
        out = sys.stdout if stream == "out" else sys.stderr
        out.flush()
        out.buffer.write(os.fsencode(text))
        out.buffer.flush()

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Run `argv` in the foreground on the terminal's streams, as bash runs a command; its exit status.

        While it runs, Ctrl-C and Ctrl-\\ go to the command only (it may handle
        them). If it died of SIGINT and cst got it too (Ctrl-C reaches both),
        cst dies of SIGINT, as bash does; otherwise a fatal signal N gives
        128+N (Ctrl-\\ included: bash ignores SIGQUIT itself).
        """
        previous = {sig: signal.getsignal(sig) for sig in _FOREGROUND_SIGNALS}
        received: set[int] = set()
        # A Python-level handler, not SIG_IGN: the command must not inherit an
        # ignored signal (exec resets a handler to the default, not an ignore).
        # A signal cst itself started with ignored stays ignored, as in bash.
        for sig, handler in previous.items():
            if handler is not signal.SIG_IGN:
                signal.signal(sig, lambda got, _frame: received.add(got))
        try:
            try:
                proc = subprocess.Popen(list(argv), env=None if env is None else dict(env))
            except OSError as exc:
                return _start_failure(argv, exc)
            rc = proc.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        if -rc == signal.SIGINT and signal.SIGINT in received:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
        return shell_exit_status(rc)

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Replace cst with `argv` (bash's exec); returns only when it cannot start (127 or 126)."""
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execvpe(argv[0], list(argv), os.environ if env is None else dict(env))
        except OSError as exc:
            return _start_failure(argv, exc)
