# SPDX-License-Identifier: MPL-2.0
"""restic, against the repository its RESTIC_* variables name: a command and its answer, and a
`restic backup --json` whose progress is shown as it runs. Generic: nothing here is about VMs (the
/home NAS backup can use it as it is); the caller gives the repository's variables at each call.
"""

from collections.abc import Callable, Mapping, Sequence
import os
import signal
import subprocess
import threading
import time
from typing import Protocol

from cst_core.adapters.runner import c_locale
from cst_core.domain.errors import ExitStatus
from cst_core.domain.shapes import json_object, parse_json
from cst_core.domain.values import MINUTE, format_bytes_gib_mib
from vm.domain.backup import ResticMessage, ResticSummary, is_restic_summary

# (argv, timeout, the variables set on top of this process's) -> (exit status, stdout, stderr).
type RunCommand = Callable[[Sequence[str], int, Mapping[str, str] | None], tuple[int, str, str]]


class ResticOutput(Protocol):
    """Where restic's progress goes: a line on stdout (flushed or not), a line on stderr."""

    def say(self, text: str, *, flush: bool = False) -> None: ...

    def warn(self, text: str) -> None: ...


def restic_env(extra: Mapping[str, str]) -> dict[str, str]:
    """The environment of a restic run that streams its output (restic backup --json): this
    process's, in the C locale, with extra (the repository's variables) on top."""
    return {**c_locale(os.environ), **extra}


def _number(value: object) -> int | float | None:
    """A JSON number (not a bool), else None."""
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def progress_text(status: object) -> str:
    """One status line of restic backup --json as shown: "50 % (5 MiB of 10 MiB read)"; a field
    that is missing or null counts as 0, one of another type shows as "?"."""
    line = status if json_object(status) else {}
    pct, done, total = (line.get(k) or 0 for k in ("percent_done", "bytes_done", "total_bytes"))
    shown_pct = f"{100 * p:.0f}" if (p := _number(pct)) is not None else "?"
    done_text, total_text = (
        format_bytes_gib_mib(n, mib_decimals=1) if (n := _number(v)) is not None else "?" for v in (done, total)
    )
    return f"{shown_pct} % ({done_text} of {total_text} read)"


class Restic:
    """restic through run (a command and its answer: the caller's runner, in the C locale) and,
    for a backup, a child of its own whose JSON lines are read as they come."""

    def __init__(self, run: RunCommand) -> None:
        self._run = run

    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        """(exit status, stdout, stderr) of restic ARGS with the repository's variables env."""
        return self._run(["restic", *args], timeout, env)

    def backup(
        self, env: Mapping[str, str], args: Sequence[str], timeout: int, out: ResticOutput
    ) -> tuple[int, ResticSummary]:
        """restic backup --json: stderr goes through, the status lines become a
        progress line every minute, the summary is kept. (rc, summary or {})."""
        try:
            p = subprocess.Popen(
                ["restic", *args], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=restic_env(env)
            )
        except FileNotFoundError:
            return ExitStatus.NOT_FOUND, {}
        timer = threading.Timer(timeout, p.kill)
        timer.start()
        summary: ResticSummary = {}
        shown = 0.0
        try:
            assert p.stdout is not None
            for line in p.stdout:
                try:
                    msg = parse_json(line)
                except ValueError, RecursionError:
                    out.say(f"  restic: {line.rstrip()}", flush=True)
                    continue
                if not json_object(msg):
                    continue
                # A line's shape never stops the backup: a field of another type shows as "?", a
                # summary of another shape counts as no summary (no snapshot: the run refuses).
                match msg.get("message_type"):
                    case ResticMessage.SUMMARY:
                        summary = msg if is_restic_summary(msg) else {}
                    case ResticMessage.STATUS if time.monotonic() - shown >= MINUTE:
                        shown = time.monotonic()
                        out.say(f"  restic: {progress_text(msg)}", flush=True)
                    case ResticMessage.ERROR:
                        err = msg.get("error") or {}
                        text = (err.get("message") or err) if json_object(err) else err
                        out.warn(f"  restic error: {msg.get('item') or ''} {text}")
                    case _:
                        pass
            rc = p.wait()
        except BaseException:
            p.terminate()
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            raise
        finally:
            timer.cancel()
        if rc == -signal.SIGKILL:
            rc = ExitStatus.TIMED_OUT
        return rc, summary
