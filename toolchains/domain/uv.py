# SPDX-License-Identifier: MPL-2.0
"""The rules of python:uv, no I/O: uv's cache, its lock wait and its prune output."""

from __future__ import annotations

import re


UV_CACHE_REL = ".cache/uv"
UV_TIMEOUT = 10 * 60  # seconds for one `uv cache prune` (also bounded by the run's budget)
UV_LOCK_WAIT = 60  # seconds uv may wait for its cache lock (UV_LOCK_TIMEOUT)
# What uv says when UV_LOCK_TIMEOUT ran out ("Timeout (60s) when waiting for
# lock on `...` at `.../.lock`, is another uv process running?"). Any other
# error, also one that names the lock file (permission denied, ...), is a
# failure.
UV_LOCK_TIMED_OUT = re.compile(r"Timeout \(\d+(?:\.\d+)?s\) when waiting for lock on ")
UV_REMOVED = re.compile(r"Removed (\d+) files? \(([\d.]+)\s*([KMGT]?i?B)\)")
UV_UNITS = {
    "b": 1,
    "kib": 1 << 10,
    "mib": 1 << 20,
    "gib": 1 << 30,
    "tib": 1 << 40,
    "kb": 1000,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
}
