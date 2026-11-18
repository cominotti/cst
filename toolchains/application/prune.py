# SPDX-License-Identifier: MPL-2.0
"""The prune use case: the chosen targets one after the other, the bounded ones first,
under the run's time budget, each planning or applying its own rules; and what every
target shares (its policy, its lines, the run's clock).

A target deletes in steps that must survive an interruption (a folder is renamed away
first; what an interrupted run left is deleted by the next one), so it reaches the
machine only through the ports (toolchains/ports.py).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Protocol

from cst_core.domain.errors import Refused
from toolchains.domain.prune import SPECS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cst_core.ports import ClockPort
    from toolchains.domain.prune import Limits, Settings, TargetSpec
    from toolchains.ports import BuildTreePort, RunOutputPort, RunStatePort, UvPort


@dataclasses.dataclass(frozen=True)
class TargetPorts:
    """How the targets reach the machine (the composition root builds it)."""

    tree: BuildTreePort
    uv: UvPort
    state: RunStatePort
    out: RunOutputPort
    clock: ClockPort


class Target(Protocol):
    """One cache of one toolchain, as prune, status and auto on use it."""

    SPEC: TargetSpec
    # time.monotonic() by which an apply must be done (None: no limit).
    deadline: float | None
    # PATH to look for programs in (None: this process's PATH).
    path: str | None
    # Named on the command line (a target the timer would skip then runs).
    named: bool

    def location(self) -> str:
        """The folder it prunes (for status and plan)."""
        ...

    def check(self) -> None:
        """Raise Refused when it would refuse to run."""
        ...

    def plan(self) -> int | None:
        """The exit status (None: 0, a plan that cannot fail)."""
        ...

    def apply(self) -> int:
        """The exit status; the run's summary is saved."""
        ...

    def status_lines(self) -> list[str]:
        """[line] for status (size, oldest, ...)."""
        ...

    def tsay(self, msg: str, prio: int = 6) -> None:
        """One line of the run, named after the target."""
        ...


class BaseTarget[P]:
    """What every target shares: its policy P (None: bad settings; only location, check
    and status_lines work then), its lines and the run's clock."""

    SPEC: TargetSpec
    deadline: float | None = None
    path: str | None = None
    named: bool = False

    def __init__(self, home: str, now: float, policy: P | None, machine: TargetPorts, limits: Limits) -> None:
        self.home: str = home
        self.now: float = now
        self.policy: P | None = policy
        self.machine: TargetPorts = machine
        self.limits: Limits = limits
        self.started: float = machine.clock.monotonic()

    def required_policy(self) -> P:
        """The policy plan and apply prune by (only status runs without one: bad settings)."""
        if self.policy is None:
            raise RuntimeError(f"{self.SPEC.id}: no policy (the settings could not be read)")
        return self.policy

    def tsay(self, msg: str, prio: int = 6) -> None:
        self.machine.out.say(f"{self.SPEC.id}: {msg}", prio)

    def clock(self) -> float:
        """Now, on the run's clock: NOW plus the time since the start."""
        return self.now + (self.machine.clock.monotonic() - self.started)

    def time_left(self) -> float | None:
        """Seconds left of the run's time budget (None: no limit)."""
        return None if self.deadline is None else self.deadline - self.machine.clock.monotonic()


def prune(
    chosen: Sequence[tuple[str, bool]],
    make: Callable[[str], Target],
    settings: Settings,
    action: str,
    *,
    out: RunOutputPort,
    clock: ClockPort,
    limits: Limits,
) -> int:
    """`prune plan|apply` of the CHOSEN targets ([(id, named)]); MAKE(id) builds each
    one right before it runs (its clock starts then). The highest exit status."""
    if not chosen:
        out.say(f"no target is enabled (settings: {settings.source}): nothing to do")
        return 0
    rc = 0
    deadline = None
    if action == "apply":
        deadline = clock.monotonic() + limits.run_budget
        chosen = sorted(chosen, key=lambda c: SPECS[c[0]].max_seconds is None)  # stable: bounded ones first
    for tid, named in chosen:
        t = make(tid)
        t.named = named
        t.deadline = deadline
        off = "" if settings.enabled[tid] else " (disabled in the settings: runs only because it was named)"
        if action == "plan":
            out.say(f"== {tid}: {out.where(t.location())}{off}")
            out.say(f"  policy: {settings.policy_text(tid)} ({settings.source_of(SPECS[tid])})")
        elif off:
            t.tsay(off.strip(" ()"), 5)
        try:
            r = (t.plan() or 0) if action == "plan" else t.apply()
        except Refused as e:
            if action == "plan":
                out.say(f"  refused: {e}")
            else:
                t.tsay(f"refused: {e}", 3)
            r = 1
        rc = max(rc, r)
    if action == "plan":
        out.say("(plan: nothing deleted)")
    return rc
