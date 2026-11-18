# SPDX-License-Identifier: MPL-2.0
"""uv: its environment, its cache lock and the `cache prune --dry-run` preview."""

from __future__ import annotations

import fcntl
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from toolchains.domain import UV_LOCK_TIMED_OUT, UV_LOCK_WAIT


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
