# SPDX-License-Identifier: MPL-2.0
"""uv: its environment, its cache lock, the `cache prune --dry-run` preview, and UvTool
(UvPort: uv found on a PATH, asked, and run to prune).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cst_core.ports import CommandResult

from toolchains.adapters.build_tree import tree_size
from toolchains.adapters.program import run_idle, run_idle_result
from toolchains.domain.uv import UV_LOCK_TIMED_OUT, UV_LOCK_WAIT, UV_TIMEOUT


def uv_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("UV_CACHE_DIR", "UV_NO_CACHE", "UV_LOCK_TIMEOUT")}
    env.update(UV_LOCK_TIMEOUT=str(UV_LOCK_WAIT), NO_COLOR="1")
    return env


def uv_lock_busy(cache: str) -> bool:
    """True while another uv holds its cache lock (<cache>/.lock: every uv
    command holds it shared; `uv cache prune` needs it alone). The test lock
    is let go at once."""
    try:
        fd = os.open(os.path.join(cache, ".lock"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def uv_prune_preview(
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]], cache: str
) -> tuple[list[str], bool]:
    """(lines, ok) of `uv cache prune --dry-run` on cache (run: uv with these
    arguments). A uv that fails, cannot start or does not end is a failure
    line, and plan exits 1; waiting in vain for the cache lock another uv
    holds is not a failure (apply skips that case too)."""
    try:
        r = run(["cache", "prune", "--dry-run", "--cache-dir", cache, "--no-progress"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"uv cache prune --dry-run failed: {e}"], False
    text = (r.stdout + r.stderr).strip()
    lines = [f"uv: {line}" for line in text.splitlines()]
    if r.returncode == 0:
        return lines, True
    if UV_LOCK_TIMED_OUT.search(text) or uv_lock_busy(cache):
        lines.append("uv cache prune --dry-run waited for the cache lock (another uv runs): no preview this time")
        return lines, True
    return lines + [f"uv cache prune --dry-run failed (exit {r.returncode})"], False


class UvTool:
    """UvPort: uv run at idle priority from HOME, with its own environment (uv_env)."""

    def __init__(self, home: str) -> None:
        self.home = home

    def run(self, uv: str, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return run_idle([uv, *args], self.home, uv_env(), timeout)

    def find(self, path: str | None) -> str | None:
        return shutil.which("uv", path=path)

    def facts(self, uv: str) -> tuple[str, bool]:
        """(version text, has --dry-run): read only, from uv's own help."""
        try:
            v = self.run(uv, ["--version"], 20).stdout.strip()
            h = self.run(uv, ["cache", "prune", "--help"], 20).stdout
        except OSError, subprocess.TimeoutExpired:
            return "?", False
        return v or "?", "--dry-run" in h

    def preview(self, uv: str, cache: str) -> tuple[list[str], bool]:
        return uv_prune_preview(lambda args: self.run(uv, args, UV_TIMEOUT), cache)

    def prune(self, uv: str, cache: str, timeout: float) -> CommandResult:
        return run_idle_result(
            [uv, "cache", "prune", "--cache-dir", cache, "--no-progress"], self.home, uv_env(), timeout
        )

    def cache_kind(self, path: str) -> str:
        try:
            st = os.lstat(os.path.realpath(path))
        except FileNotFoundError:
            return "missing"
        return "folder" if stat.S_ISDIR(st.st_mode) else "other"

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def lock_busy(self, cache: str) -> bool:
        return uv_lock_busy(cache)

    def size(self, path: str) -> int:
        return tree_size(path)
