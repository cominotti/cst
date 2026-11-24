# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection for unit tests: every mutating call of a run, in turn, is where it fails.

A test wraps the mutating calls of a use case's ports (or the module functions an adapter calls)
in FaultPlan.step. A first run with no fault records the calls (cut_points): their number N is
the number of points. Then, for each mode and each point k in 1..N (every_fault), a fresh fixture
runs again with the plan armed at k:

- KILL: the k-th call takes effect, then the process dies (Killed, a BaseException: an
  `except Exception` of the code under test does not catch it, as a kill is not caught);
- FAIL: the k-th call fails (OSError ENOSPC) with no effect, and the run goes on;
- FAIL_FROM: the k-th call and every later one fail (a full disk stays full).

A plan can hold several faults: a FAIL at k and a KILL at a later j is a run that fails, then dies
while it rolls back.

A port whose contract reports a failure in another way (a command result with a non-zero exit, an
`ignore_errors` removal) passes `failed`, which gives the port's answer for that failure.
FaultyRunner does that for a command runner: the commands that only read go to the inner runner
as they are, the others are points of the plan and answer exit 1 at a fault.

stub_fsync makes os.fsync a no-op for one test: these tests check what a kill or a failure leaves,
not durability (KILL keeps every finished call), and an fsync per write makes them many times
slower on a btrfs home. tests/probes.py keeps the fsync probes for the tests that check durability.

What it cannot model: a power loss and writes not yet flushed, a call torn half-way (each call is
atomic here), threads and timing. docs/dev-tools.md, "Fault injection tests", says how to add a
target.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import errno
import functools
import os
from typing import Protocol
import unittest
from unittest import mock

from cst_core.ports import CommandResult


class Killed(BaseException):
    """The process dies here (SIGKILL, a timeout, a power loss after the last durable write)."""


class FaultMode(StrEnum):
    """How the armed point fails."""

    KILL = "kill"
    FAIL = "fail"
    FAIL_FROM = "fail-from"


ALL_MODES = tuple(FaultMode)


def no_space() -> OSError:
    """The error a full disk gives."""
    return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


type Fault = tuple[FaultMode, int]  # (how, the 1-based number of the mutating call)


@dataclass
class FaultPlan:
    """The mutating calls of one run, and where (if anywhere) the run fails."""

    faults: tuple[Fault, ...] = ()
    calls: list[str] = field(default_factory=list)

    def arm(self, *faults: Fault) -> FaultPlan:
        """A new run: no call seen yet; these faults (none: a run that records only)."""
        self.faults, self.calls = faults, []
        return self

    def fault_at(self, n: int) -> FaultMode | None:
        """How the n-th mutating call fails (None: it runs)."""
        for mode, at in self.faults:
            if n == at or (mode == FaultMode.FAIL_FROM and n >= at):
                return mode
        return None

    def step[T](self, name: str, effect: Callable[[], T], failed: Callable[[OSError], T] | None = None) -> T:
        """Run one mutating call (effect) as the plan says; name is what the trace records."""
        self.calls.append(name)
        n = len(self.calls)
        mode = self.fault_at(n)
        if mode in {FaultMode.FAIL, FaultMode.FAIL_FROM}:
            err = no_space()
            if failed is None:
                raise err
            return failed(err)
        result = effect()
        if mode == FaultMode.KILL:
            raise Killed(f"killed after call {n}: {name}")
        return result


def ignore_error(_err: OSError) -> None:
    """A failed call whose contract swallows the error (`ignore_errors=True`): no effect, no exception."""


def cut_points(plan: FaultPlan, run: Callable[[], object]) -> list[str]:
    """The mutating calls of one run with no fault (run on a fresh fixture): the points to try."""
    plan.arm()
    run()
    return list(plan.calls)


def every_fault(points: int, modes: tuple[FaultMode, ...] = ALL_MODES, *, after: int = 0) -> list[Fault]:
    """(mode, k) for every mode and every point k in after+1..points, mode by mode."""
    return [(mode, k) for mode in modes for k in range(after + 1, points + 1)]


def _no_fsync(fd: int) -> None:
    """os.fsync that flushes nothing; a bad descriptor still raises (EBADF), as the real one does."""
    os.fstat(fd)


def stub_fsync(test: unittest.TestCase) -> None:
    """os.fsync flushes nothing until test's cleanups run (call it in setUp)."""
    test.enterContext(mock.patch.object(os, "fsync", _no_fsync))


class FaultyOs:
    """The os module as an adapter module sees it, with the functions named in names through plan
    (the point is "os.NAME"); every other attribute is os's own.

    Patch it over the module's name (`mock.patch.object(module, "os", FaultyOs(plan, ("rename",)))`):
    only that module's calls become points, not the ones the standard library makes for it."""

    def __init__(self, plan: FaultPlan, names: Collection[str]) -> None:
        for name in names:
            if not callable(getattr(os, name, None)):
                raise AttributeError(f"os has no function {name!r}")
        self.plan = plan
        self.names = frozenset(names)
        self.path = os.path

    def __getattr__(self, name: str) -> object:
        real = getattr(os, name)
        if name not in self.names:
            return real

        def faulty(*args: object, **kw: object) -> object:
            return self.plan.step(f"os.{name}", functools.partial(real, *args, **kw))

        return faulty


def _joined(argv: Sequence[str]) -> str:
    return " ".join(argv)


class InnerRunner(Protocol):
    """The runner FaultyRunner wraps: a test's fake, which takes the port's options as they come."""

    @property
    def run(self) -> Callable[..., CommandResult]:
        """CommandRunnerPort.run of the fake (any signature that takes the options the code passes)."""
        ...


class FaultyRunner:
    """A command runner over inner whose changing commands are points of plan.

    reads(argv) true: the command only reads; it goes to inner as it is and is no point. Any other
    command goes through plan.step under name(argv); at a fault inner never sees it and the answer
    is exit 1 with the error on stderr (a command reports a failure by its exit status)."""

    def __init__(
        self,
        plan: FaultPlan,
        inner: InnerRunner,
        *,
        reads: Callable[[Sequence[str]], bool],
        name: Callable[[Sequence[str]], str] = _joined,
    ) -> None:
        self.plan = plan
        self.inner = inner
        self.reads = reads
        self.name = name

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        """CommandRunnerPort.run, with the plan's faults on the changing commands."""
        if self.reads(argv):
            return self.inner.run(argv, **kw)

        def failed(err: OSError) -> CommandResult:
            return CommandResult(1, "", f"{err.strerror}\n", argv=tuple(argv))

        return self.plan.step(self.name(argv), lambda: self.inner.run(argv, **kw), failed)
