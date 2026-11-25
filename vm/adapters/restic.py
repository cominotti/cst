# SPDX-License-Identifier: MPL-2.0
"""restic, against the repository its RESTIC_* variables name: a command and its answer, and a
`restic backup --json` whose progress is shown as it runs. Generic: nothing here is about VMs (the
/home NAS backup can use it as it is); the caller gives the repository's variables at each call.
"""

from collections.abc import Callable, Mapping, Sequence
import os
import select
import signal
import subprocess
import threading
import time
from typing import IO, Final, Protocol

from cst_core.adapters.runner import c_locale
from cst_core.domain.errors import ExitStatus
from cst_core.domain.shapes import json_object, number_or_none, parse_json
from cst_core.domain.values import MINUTE, format_bytes_gib_mib
from vm.adapters.backup import signals_held
from vm.domain.backup import ResticMessage, ResticSummary, Stop, StopAfterSnapshot, is_restic_summary

# Seconds restic has to end once a stop asks it to (then it is killed), and the most the reading of the
# lines it left takes after that.
RESTIC_STOP_WAIT: Final = MINUTE
# The most of restic's output read at a time.
_CHUNK: Final = 64 * 1024

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


def progress_text(status: object) -> str:
    """One status line of restic backup --json as shown: "50 % (5 MiB of 10 MiB read)"; a field
    that is missing or null counts as 0, one of another type shows as "?"."""
    line = status if json_object(status) else {}
    pct, done, total = (line.get(k) or 0 for k in ("percent_done", "bytes_done", "total_bytes"))
    shown_pct = f"{100 * p:.0f}" if (p := number_or_none(pct)) is not None else "?"
    done_text, total_text = (
        format_bytes_gib_mib(n, mib_decimals=1) if (n := number_or_none(v)) is not None else "?" for v in (done, total)
    )
    return f"{shown_pct} % ({done_text} of {total_text} read)"


def _backup_json_line(
    line: str, summary: ResticSummary, shown: float, out: ResticOutput
) -> tuple[ResticSummary, float]:
    """One line of restic backup --json: shown as it is when it is not JSON, a status line every
    minute (shown: when the last one was), an error on stderr, the summary kept. (summary, shown)."""
    try:
        msg = parse_json(line)
    except ValueError, RecursionError:
        out.say(f"  restic: {line.rstrip()}", flush=True)
        return summary, shown
    if not json_object(msg):
        return summary, shown
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
    return summary, shown


def _summary_of(line: str) -> ResticSummary:
    """The summary of restic backup --json when line is its summary line, else {}."""
    try:
        msg = parse_json(line)
    except ValueError, RecursionError:
        return {}
    if json_object(msg) and msg.get("message_type") == ResticMessage.SUMMARY and is_restic_summary(msg):
        return msg
    return {}


class _Output:
    """restic's output, read in chunks with the stops held, so that a stop (Stop, which the signal
    handler raises wherever the stops are let in: while restic is waited for) never loses what was
    read. Each line stays in pending until it was handled (drop_line), so one handled when the stop
    came is still there. The lines are text: bytes that are not UTF-8 become U+FFFD."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        self.pending = bytearray()
        self.ended = False
        try:
            self.fd: int | None = stream.fileno()
        except OSError, ValueError:  # not a pipe (a test's bytes)
            self.fd = None

    def _line_end(self) -> int | None:
        """Where the first whole line of pending ends (the rest, at the end of the output), or None."""
        end = self.pending.find(b"\n")
        if end >= 0:
            return end + 1
        return len(self.pending) if self.ended and self.pending else None

    def line(self) -> str | None:
        """The first whole line of pending (the rest at the end of the output), or None."""
        end = self._line_end()
        return None if end is None else self.pending[:end].decode(errors="replace")

    def drop_line(self) -> None:
        del self.pending[: self._line_end() or len(self.pending)]

    def fill(self, timeout: float | None = None) -> bool:
        """More of the output into pending, once there is some (or its end); False when timeout seconds
        (None: no limit) passed first. The wait lets the stops in; the read holds them."""
        if self.fd is not None and not select.select([self.fd], [], [], timeout)[0]:
            return False
        with signals_held():
            chunk = self.stream.read(_CHUNK)
            self.pending += chunk
            self.ended = not chunk
        return True

    def summary_left(self) -> ResticSummary:
        """The first summary among the lines pending and those left in the output once restic is gone,
        read until its end for at most RESTIC_STOP_WAIT: a process of restic's that keeps the pipe open
        ends the reading then. Only the summary: those lines are not shown."""
        deadline = time.monotonic() + RESTIC_STOP_WAIT
        while True:
            while (line := self.line()) is not None:
                if found := _summary_of(line):
                    return found
                self.drop_line()
            if self.ended or not self.fill(max(0.0, deadline - time.monotonic())):
                return {}


def _ended(p: subprocess.Popen[bytes]) -> int:
    """restic's exit status once it is gone: its own when it had exited already, else after a
    terminate (and a kill when it is still there RESTIC_STOP_WAIT later)."""
    rc = p.poll()
    if rc is not None:
        return rc
    p.terminate()
    try:
        return p.wait(RESTIC_STOP_WAIT)
    except subprocess.TimeoutExpired:
        p.kill()
        return p.wait()


class Restic:
    """restic through run (a command and its answer: the caller's runner, in the C locale) and,
    for a backup, a child of its own whose JSON lines are read as they come."""

    def __init__(self, run: RunCommand) -> None:
        self._run = run

    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        """(exit status, stdout, stderr) of restic ARGS with the repository's variables env."""
        return self._run(["restic", *args], timeout, env)

    def backup(
        self,
        env: Mapping[str, str],
        args: Sequence[str],
        timeout: int,
        out: ResticOutput,
        *,
        answered: Callable[[], None] | None = None,
    ) -> tuple[int, ResticSummary]:
        """restic backup --json: stderr goes through, the status lines become a
        progress line every minute, the summary is kept. (rc, summary or {}).

        A stop (Stop) while restic runs ends restic. When restic had written its snapshot by then (its
        summary, kept or still in the pipe, names it), StopAfterSnapshot carries restic's exit status
        and the summary, so the caller can record the snapshot; else the stop is raised as it came.
        Assumed, not covered: restic ended between saving its snapshot and printing its summary.

        answered is called once restic has exited, when its summary names its snapshot: the caller
        holds the stops from there (restic is never waited for with the stops held); a stop it raises
        is carried as one that came after the snapshot."""
        try:
            # Unbuffered: each read takes what restic wrote so far (_Output's chunks).
            p = subprocess.Popen(
                ["restic", *args], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0, env=restic_env(env)
            )
        except FileNotFoundError:
            return ExitStatus.NOT_FOUND, {}
        timer = threading.Timer(timeout, p.kill)
        # A thread starts with its creator's signal mask: the timer's blocks the stops for good, so a stop
        # always goes to the main thread, and never while it holds them (signals_held: _Output's reads).
        with signals_held():
            timer.start()
        summary: ResticSummary = {}
        shown = 0.0
        assert p.stdout is not None
        output = _Output(p.stdout)
        try:
            while True:
                while (line := output.line()) is not None:
                    summary, shown = _backup_json_line(line, summary, shown, out)
                    output.drop_line()
                if output.ended:
                    break
                output.fill()
            rc = p.wait()
            if answered is not None and "snapshot_id" in summary:
                answered()
        except Stop as e:
            # The stops are ignored from here on (on_signal): the reading of the last lines is not cut.
            rc = _ended(p)
            found = summary if "snapshot_id" in summary else output.summary_left()
            if "snapshot_id" in found:
                raise StopAfterSnapshot(str(e), rc, found) from None
            raise
        except BaseException:
            _ended(p)
            raise
        finally:
            # cancel() only asks: joined, so no thread of this process outlives the backup. A stop signal
            # can go to any thread that does not block it, and Python then runs its handler in the main
            # thread even inside signals_held (whose mask is the main thread's only): a thread that let the
            # stops in would cut a read of _Output or the torn record short.
            timer.cancel()
            timer.join()
            p.stdout.close()
        if rc == -signal.SIGKILL:
            rc = ExitStatus.TIMED_OUT
        return rc, summary
