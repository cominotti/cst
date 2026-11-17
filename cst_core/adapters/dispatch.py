"""The host side of the `cst` dispatcher: files, users, and running a module the way bash did."""

from collections.abc import Mapping, Sequence
import os
import pwd
import signal
import subprocess
import sys

from cst_core.domain.errors import shell_exit_status

# The signals a bash script waiting for a foreground command leaves to that
# command: the command may handle them (a cleanup trap), and bash dies with
# the same signal only if the command died of it.
_FOREGROUND_SIGNALS = (signal.SIGINT, signal.SIGQUIT)


def _start_failure(argv: Sequence[str], exc: OSError) -> int:
    """Report a command that could not start, as bash does: 127 when it is not found, else 126."""
    print(f"{argv[0]}: {exc.strerror}", file=sys.stderr, flush=True)
    return 127 if isinstance(exc, FileNotFoundError) else 126


class LocalCstDispatchAdapter:
    """Concrete `CstDispatchPort` backed by the host OS, with bash's exec and foreground semantics."""

    def is_file(self, path: str) -> bool:
        """Return True when `path` exists as a regular file."""
        return os.path.isfile(path)

    def is_container(self) -> bool:
        """Return True inside a Podman toolbox or Docker container."""
        return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")

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

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Run `argv` in the foreground on the terminal's streams, as bash runs a command; its exit status.

        While it runs, Ctrl-C and Ctrl-\\ go to the command only (it may handle
        them). If it died of one of them and cst got that signal too (Ctrl-C
        reaches both), cst dies of the same signal, as bash does; otherwise a
        fatal signal N gives 128+N.
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
        if -rc in received:
            signal.signal(-rc, signal.SIG_DFL)
            os.kill(os.getpid(), -rc)
        return shell_exit_status(rc)

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Replace cst with `argv` (bash's exec); returns only when it cannot start (127 or 126)."""
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execvpe(argv[0], list(argv), os.environ if env is None else dict(env))
        except OSError as exc:
            return _start_failure(argv, exc)
