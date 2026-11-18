# SPDX-License-Identifier: MPL-2.0
"""The python:uv target: `uv cache prune` on uv's cache."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

from collections.abc import Mapping, Sequence

from cst_core.domain.errors import Refused
from cst_core.domain.values import Json
from toolchains.domain.common import ago, gib, span, tilde
from toolchains.domain.prune import PYTHON_UV, RUN_BUDGET, Settings, uv_last_run
from toolchains.domain.uv import (
    UV_CACHE_REL,
    UV_LOCK_TIMED_OUT,
    UV_LOCK_WAIT,
    UV_REMOVED,
    UV_TIMEOUT,
    UV_UNITS,
    UvPolicy,
)
from toolchains.adapters.build_tree import tree_size
from toolchains.adapters.home_files import read_state, write_state
from toolchains.adapters.program import run_idle, say
from toolchains.adapters.uv import uv_env, uv_lock_busy, uv_prune_preview
from toolchains.cli.targets import Target


class PythonUv(Target[UvPolicy]):
    SPEC = PYTHON_UV

    @classmethod
    def make(cls, home: str, now: float, settings: Settings | None) -> PythonUv:
        return cls(home, now, settings.uv if settings else None)

    def location(self) -> str:
        return os.path.join(self.home, UV_CACHE_REL)

    def uv(self) -> str:
        uv = shutil.which("uv", path=self.path)
        if not uv:
            raise Refused("uv not found on PATH: nothing is done (install uv, or PYTHON_UV_ENABLED=no)")
        return uv

    def check(self) -> None:
        self.uv()

    def run_uv(self, uv: str, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return run_idle([uv, *args], self.home, uv_env(), timeout)

    def uv_facts(self, uv: str) -> tuple[str, bool]:
        """(version text, has --dry-run): read only, from uv's own help."""
        try:
            v = self.run_uv(uv, ["--version"], 20).stdout.strip()
            h = self.run_uv(uv, ["cache", "prune", "--help"], 20).stdout
        except OSError, subprocess.TimeoutExpired:
            return "?", False
        return v or "?", "--dry-run" in h

    def cache(self) -> str | None:
        """The cache folder, or None when it does not exist (nothing to do)."""
        c = self.location()
        try:
            st = os.lstat(os.path.realpath(c))
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{tilde(c, self.home)} is not a folder: refused")
        return c

    def due(self) -> str:
        """'' when an automatic run is due, else why not."""
        last = read_state(self.home, self.SPEC.id)
        iv = self.required_policy().interval
        t = last.get("time") if last else None
        if self.named or not iv or not last or not isinstance(t, (int, float)):
            return ""
        if self.now - t < iv:
            return f"last run {ago(self.now - t)} ago (INTERVAL {span(iv)})"
        return ""

    def plan(self) -> int:
        c = self.cache()
        if c is None:
            say(f"  ~/{UV_CACHE_REL} does not exist: nothing to do")
            return 0
        uv = self.uv()
        version, dry = self.uv_facts(uv)
        say(f"  uv: {uv} ({version}); cache {gib(tree_size(c))} (as du shows it)")
        if uv_lock_busy(c):
            say("  a uv command is using the cache now (its lock is held): apply would skip it this time")
        wait = self.due()
        if wait:
            say(f"  the timer would skip it: {wait}; named on the command line it runs")
        if dry:
            lines, ok = uv_prune_preview(lambda args: self.run_uv(uv, args, UV_TIMEOUT), c)
            for line in lines:
                say(f"  {line}")
            return 0 if ok else 1
        say(
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
        if uv_lock_busy(c):
            self.tsay("skipped: a uv command is using the cache (its lock is held); the next run tries again", 5)
            return 0
        timeout: float = UV_TIMEOUT
        left = self.time_left()
        if left is not None:
            if left < 2 * UV_LOCK_WAIT:
                self.tsay(
                    f"skipped: the run's time budget ({span(RUN_BUDGET)}) is used up; the next run tries again", 5
                )
                return 0
            timeout = min(timeout, left)
        try:
            r = self.run_uv(uv, ["cache", "prune", "--cache-dir", c, "--no-progress"], timeout)
        except subprocess.TimeoutExpired:
            self.tsay(f"uv cache prune took longer than {ago(timeout)}: stopped", 3)
            return 1
        except OSError as e:
            self.tsay(f"could not run uv: {e.strerror or e}", 3)
            return 1
        text = (r.stdout + "\n" + r.stderr).strip()
        if r.returncode != 0:
            if UV_LOCK_TIMED_OUT.search(text) or uv_lock_busy(c):
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
        write_state(self.home, self.SPEC.id, {"time": self.now, "removed": removed, "freed": freed})
        return 0

    @staticmethod
    def last_run_text(s: Mapping[str, Json]) -> str:
        return uv_last_run(s)

    def status_lines(self) -> list[str]:
        c = self.location()
        if not os.path.isdir(c):
            return [f"size:     ~/{UV_CACHE_REL} does not exist"]
        out = [f"size:     {gib(tree_size(c))} (as du shows it)"]
        uv = shutil.which("uv")
        out.append(f"uv:       {uv}" if uv else "uv:       not found on PATH (prune refuses)")
        if uv_lock_busy(c):
            out.append("in use:   a uv command holds the cache lock now")
        return out
