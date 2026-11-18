# SPDX-License-Identifier: MPL-2.0
"""The base of a prune target (one cache of one toolchain)."""

from __future__ import annotations

import time
from typing import Any

from collections.abc import Mapping

from toolchains.adapters.program import say, time_left


class Target:
    """One cache of one toolchain. A new target is a subclass with these
    attributes and methods, added to TARGETS (and its keys to
    toolchains/toolchains.conf):
      ID        "lang:cache", as typed on the command line
      PREFIX    the settings prefix, e.g. RUST_BUILD
      WHAT      one line for status
      SETTINGS  its own setting names (without PREFIX_; ENABLED is common)
      make_policy(vals, prefix)  checked policy from {SETTING: raw value}
      location()                 the folder it prunes (for status)
      check()                    raise Refused when it would refuse to run
      plan(), apply()            exit status; apply also calls write_state
      status_lines()             [line] for status (size, oldest, ...)
      last_run_text(state)       one line from the saved summary"""

    ID: str = ""
    PREFIX: str = ""
    WHAT: str = ""
    SETTINGS: tuple[str, ...] = ()
    # time.monotonic() by which an apply must be done (None: no limit);
    # cmd_prune sets it from RUN_BUDGET.
    deadline: float | None = None
    # PATH to look for programs in (None: this process's PATH).
    path: str | None = None
    # The most seconds one apply can take (None: no fixed bound, only the
    # run's budget). prune apply runs the bounded targets first.
    MAX_SECONDS: int | None = None
    # check() looks for a program on PATH (auto on then checks the user
    # manager's PATH, which the timer runs with).
    USES_PATH: bool = False
    named: bool = False

    def __init__(self, home: str, now: float, policy: dict[str, Any] | None) -> None:
        self.home: str = home
        self.now: float = now
        self.policy: dict[str, Any] = {} if policy is None else policy
        self.started: float = time.monotonic()

    @staticmethod
    def make_policy(vals: Mapping[str, str], prefix: str) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def policy_text(pol: Mapping[str, Any]) -> str:
        raise NotImplementedError

    @staticmethod
    def last_run_text(s: Mapping[str, Any]) -> str:
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
        say(f"{self.ID}: {msg}", prio)

    def clock(self) -> float:
        """Now, on the run's clock: NOW plus the time since the start."""
        return self.now + (time.monotonic() - self.started)

    def time_left(self) -> float | None:
        """Seconds left of the run's time budget (None: no limit)."""
        return time_left(self.deadline)
