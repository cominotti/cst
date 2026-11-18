# SPDX-License-Identifier: MPL-2.0
"""The base of a prune target (one cache of one toolchain)."""

from __future__ import annotations

import time

from collections.abc import Mapping

from cst_core.domain.values import Json
from toolchains.adapters.program import say, time_left
from toolchains.domain.prune import TargetSpec


class Target[P]:
    """One cache of one toolchain, pruned by its policy P. A new target is a
    subclass with these attributes and methods, added to TARGETS (its
    TargetSpec to toolchains.domain.prune.SPECS, its policy to Settings, its
    keys to toolchains/toolchains.conf):
      SPEC      what it is: id ("lang:cache"), settings prefix, own setting
                names, status line, max_seconds, uses_path
      make(home, now, settings)  the target with its policy from the settings
                                 (None: bad settings; only location, check
                                 and status_lines work then)
      location()                 the folder it prunes (for status)
      check()                    raise Refused when it would refuse to run
      plan(), apply()            exit status; apply also calls write_state
      status_lines()             [line] for status (size, oldest, ...)
      last_run_text(state)       one line from the saved summary"""

    SPEC: TargetSpec
    # time.monotonic() by which an apply must be done (None: no limit);
    # cmd_prune sets it from RUN_BUDGET.
    deadline: float | None = None
    # PATH to look for programs in (None: this process's PATH).
    path: str | None = None
    named: bool = False

    def __init__(self, home: str, now: float, policy: P | None) -> None:
        self.home: str = home
        self.now: float = now
        self.policy: P | None = policy
        self.started: float = time.monotonic()

    def required_policy(self) -> P:
        """The policy plan and apply prune by (only status runs without one: bad settings)."""
        if self.policy is None:
            raise RuntimeError(f"{self.SPEC.id}: no policy (the settings could not be read)")
        return self.policy

    @staticmethod
    def last_run_text(s: Mapping[str, Json]) -> str:
        raise NotImplementedError

    def location(self) -> str:
        raise NotImplementedError

    def check(self) -> None:
        raise NotImplementedError

    def plan(self) -> int:
        raise NotImplementedError

    def apply(self) -> int:
        raise NotImplementedError

    def status_lines(self) -> list[str]:
        raise NotImplementedError

    def tsay(self, msg: str, prio: int = 6) -> None:
        say(f"{self.SPEC.id}: {msg}", prio)

    def clock(self) -> float:
        """Now, on the run's clock: NOW plus the time since the start."""
        return self.now + (time.monotonic() - self.started)

    def time_left(self) -> float | None:
        """Seconds left of the run's time budget (None: no limit)."""
        return time_left(self.deadline)
