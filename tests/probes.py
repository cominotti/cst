# SPDX-License-Identifier: MPL-2.0
"""Test probes that several modules' tests share.

- recorded_fsyncs and assert_replaced_durably: which paths a write fsyncs,
  and whether a file was replaced through cst_core's write_atomic with its
  file and its folder fsynced (durable=True);
- python_program: a program made of given Python lines, run by this
  interpreter (a Python program, not sh: a test that hands on its stdin
  would run bash with a socket there, and bash then reads ~/.bashrc);
- bytes_program: a program that prints given bytes, which need not be UTF-8;
- NoUnitQueries: the SystemdPort calls a module's fake of systemd never gets;
- time_bound: a test that may block (a FIFO's open) fails after a few
  seconds instead of hanging the run;
- open_fds: how many descriptors this process has open.
"""

import os
import signal
import sys
import unittest
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from types import FrameType
from unittest import mock

from cst_core.ports import CommandResult, OutputMode, UnitName


@contextmanager
def recorded_fsyncs() -> Generator[list[str]]:
    """os.fsync still syncs, and also notes the path of each fd it syncs, in order."""
    synced: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    with mock.patch.object(os, "fsync", fsync):
        yield synced


def assert_replaced_durably(test: unittest.TestCase, path: str, synced: Sequence[str]) -> None:
    """PATH's temporary file (write_atomic's ".NAME.*") was fsynced once, then its folder after it."""
    folder = os.path.realpath(os.path.dirname(path))
    prefix = os.path.join(folder, "." + os.path.basename(path) + ".")
    temps = [i for i, p in enumerate(synced) if p.startswith(prefix)]
    test.assertEqual(len(temps), 1, synced)  # the new bytes, before the rename
    test.assertIn(folder, synced[temps[0] + 1 :], synced)  # the rename's folder entry


def python_program(folder: str, name: str, body: str) -> str:
    """An executable FOLDER/NAME that runs BODY (Python, after "import sys"); its path."""
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable} -I\nimport sys\n{body}")
    os.chmod(path, 0o755)
    return path


def bytes_program(folder: str, name: str, out: bytes) -> str:
    """An executable FOLDER/NAME that writes OUT to its stdout as it is; its path."""
    return python_program(folder, name, f"sys.stdout.buffer.write({out!r})\n")


class CallBlocked(BaseException):
    """time_bound's error: not an OSError (TimeoutError is one, and the code under test may take it for
    a file it cannot read and go on) nor an Exception, so only unittest's own handler catches it."""


@contextmanager
def time_bound(seconds: float) -> Generator[None]:
    """While the with block runs, a SIGALRM after seconds, and again every second after that, raises
    CallBlocked in the main thread: a call that blocks (an open that waits) ends with it, as an error,
    and does not hang the run (a handler that raises ends the call's retry, PEP 475). Leaving the block
    stops the timer, then puts SIGALRM's disposition back (tests/run_unit.py fails a test that leaves one
    changed): nothing after it, such as the test's cleanups, runs with the timer armed. Bound each
    subtest on its own, so the bound does not grow with their number."""

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise CallBlocked(f"still running after {seconds} s: a call blocked")

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds, 1)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def open_fds() -> int:
    """How many file descriptors this process has open (/proc/self/fd; the listing's own is in both
    counts of a before and after)."""
    return len(os.listdir("/proc/self/fd"))


class NoUnitQueries:
    """The SystemdPort calls a fake of a module that never makes them (vm's timer use cases, swap):
    systemctl's queries and daemon-reload's result; each one fails the test."""

    def daemon_reload_result(self, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        raise AssertionError("not asked here: daemon-reload's result")

    def is_active(self, unit: UnitName, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        raise AssertionError(f"not asked here: is-active {unit}")

    def is_enabled(self, unit: UnitName, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        raise AssertionError(f"not asked here: is-enabled {unit}")

    def show_environment(self, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        raise AssertionError("not asked here: show-environment")

    def show_value(self, unit: UnitName, prop: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        raise AssertionError(f"not asked here: show -p {prop} --value {unit}")
