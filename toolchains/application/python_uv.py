# SPDX-License-Identifier: MPL-2.0
"""python:uv: `uv cache prune` on uv's cache (it removes only what uv itself no longer
uses), at most once per INTERVAL when the timer runs it, skipped while another uv holds
the cache's lock or when the run's time budget is used up.
"""

from __future__ import annotations

import os
from cst_core.domain.errors import Refused
from toolchains.application.prune import BaseTarget
from toolchains.domain.common import ago, gib, span, tilde
from toolchains.domain.prune import PYTHON_UV
from toolchains.domain.uv import (
    UV_CACHE_REL,
    UV_LOCK_TIMED_OUT,
    UV_LOCK_WAIT,
    UV_REMOVED,
    UV_TIMEOUT,
    UV_UNITS,
    UvPolicy,
)


class PythonUv(BaseTarget[UvPolicy]):
    """python:uv: uv's package cache."""

    SPEC = PYTHON_UV

    def location(self) -> str:
        return os.path.join(self.home, UV_CACHE_REL)

    def uv(self) -> str:
        uv = self.machine.uv.find(self.path)
        if not uv:
            raise Refused("uv not found on PATH: nothing is done (install uv, or PYTHON_UV_ENABLED=no)")
        return uv

    def check(self) -> None:
        self.uv()

    def cache(self) -> str | None:
        """The cache folder, or None when it does not exist (nothing to do)."""
        c = self.location()
        kind = self.machine.uv.cache_kind(c)
        if kind == "missing":
            return None
        if kind != "folder":
            raise Refused(f"{tilde(c, self.home)} is not a folder: refused")
        return c

    def due(self) -> str:
        """'' when an automatic run is due, else why not."""
        last = self.machine.state.read(self.SPEC.id)
        iv = self.required_policy().interval
        t = last.get("time") if last else None
        if self.named or not iv or not last or not isinstance(t, (int, float)):
            return ""
        if self.now - t < iv:
            return f"last run {ago(self.now - t)} ago (INTERVAL {span(iv)})"
        return ""

    def plan(self) -> int:
        out = self.machine.out
        c = self.cache()
        if c is None:
            out.say(f"  ~/{UV_CACHE_REL} does not exist: nothing to do")
            return 0
        uv = self.uv()
        version, dry = self.machine.uv.facts(uv)
        out.say(f"  uv: {uv} ({version}); cache {gib(self.machine.uv.size(c))} (as du shows it)")
        if self.machine.uv.lock_busy(c):
            out.say("  a uv command is using the cache now (its lock is held): apply would skip it this time")
        wait = self.due()
        if wait:
            out.say(f"  the timer would skip it: {wait}; named on the command line it runs")
        if dry:
            lines, ok = self.machine.uv.preview(uv, c)
            for line in lines:
                out.say(f"  {line}")
            return 0 if ok else 1
        out.say(
            "  apply runs `uv cache prune`: it removes only what uv itself no longer uses (old versions of "
            "cache entries, unused temporary environments); this uv has no --dry-run, so how much is "
            "only known afterwards"
        )
        return 0

    def apply(self) -> int:
        c = self.cache()
        if c is None:
            self.tsay(f"~/{UV_CACHE_REL} does not exist: nothing to do")
            return 0
        wait = self.due()
        if wait:
            self.tsay(f"not due: {wait}")
            return 0
        uv = self.uv()
        if self.machine.uv.lock_busy(c):
            self.tsay("skipped: a uv command is using the cache (its lock is held); the next run tries again", 5)
            return 0
        timeout: float = UV_TIMEOUT
        left = self.time_left()
        if left is not None:
            if left < 2 * UV_LOCK_WAIT:
                self.tsay(
                    f"skipped: the run's time budget ({span(self.limits.run_budget)}) is used up; "
                    "the next run tries again",
                    5,
                )
                return 0
            timeout = min(timeout, left)
        r = self.machine.uv.prune(uv, c, timeout)
        if r.fault == "timeout":
            self.tsay(f"uv cache prune took longer than {ago(timeout)}: stopped", 3)
            return 1
        if r.cause is not None:
            self.tsay(f"could not run uv: {(r.cause.strerror if isinstance(r.cause, OSError) else None) or r.cause}", 3)
            return 1
        text = (r.stdout + "\n" + r.stderr).strip()
        if r.returncode != 0:
            if UV_LOCK_TIMED_OUT.search(text) or self.machine.uv.lock_busy(c):
                self.tsay(
                    f"skipped: uv waited {UV_LOCK_WAIT}s for its cache lock (another uv runs); "
                    "the next run tries again",
                    5,
                )
                return 0
            last = (text.splitlines() or ["failed"])[-1]
            self.tsay(f"uv cache prune failed: {last}", 3)
            return 1
        removed, freed = 0, 0
        m = UV_REMOVED.search(text)
        if m:
            removed = int(m.group(1))
            freed = int(float(m.group(2)) * UV_UNITS.get(m.group(3).lower(), 1))
        for line in text.splitlines():
            if line.strip():
                self.tsay(f"uv: {line.strip()}")
        self.machine.state.write(self.SPEC.id, {"time": self.now, "removed": removed, "freed": freed})
        return 0

    def status_lines(self) -> list[str]:
        c = self.location()
        if not self.machine.uv.is_dir(c):
            return [f"size:     ~/{UV_CACHE_REL} does not exist"]
        out = [f"size:     {gib(self.machine.uv.size(c))} (as du shows it)"]
        uv = self.machine.uv.find(None)
        out.append(f"uv:       {uv}" if uv else "uv:       not found on PATH (prune refuses)")
        if self.machine.uv.lock_busy(c):
            out.append("in use:   a uv command holds the cache lock now")
        return out
