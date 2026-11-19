# SPDX-License-Identifier: MPL-2.0
"""The rules of python:uv, no I/O: uv's cache, its lock wait and its prune output."""

from __future__ import annotations

import dataclasses
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from cst_core.domain.values import MINUTE

if TYPE_CHECKING:
    from collections.abc import Mapping

from toolchains.domain.common import parse_off_or_duration, span


@dataclasses.dataclass(frozen=True, slots=True)
class UvPolicy:
    """python:uv's checked setting: at most one `uv cache prune` per INTERVAL seconds (0:
    every run)."""

    interval: int

    @staticmethod
    def parse(vals: Mapping[str, str], prefix: str) -> UvPolicy:
        _ = prefix
        return UvPolicy(interval=parse_off_or_duration(vals["INTERVAL"]))

    def text(self) -> str:
        every = span(self.interval) if self.interval else "every run"
        return f"uv cache prune, at most once per INTERVAL ({every})"


UV_CACHE_REL = ".cache/uv"


class UvCacheKind(StrEnum):
    """What is at uv's cache path (followed through links)."""

    MISSING = "missing"
    FOLDER = "folder"
    OTHER = "other"


UV_TIMEOUT: Final = 10 * MINUTE  # seconds for one `uv cache prune` (also bounded by the run's budget)
UV_LOCK_WAIT: Final = MINUTE  # seconds uv may wait for its cache lock (UV_LOCK_TIMEOUT)
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
