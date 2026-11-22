# SPDX-License-Identifier: MPL-2.0
"""Ports for `perf` telemetry probing and root-agnostic snapshot assembly."""

from collections.abc import Sequence
from typing import Protocol

from perf.domain.models import ALL_PERF_AXES, PerfAxis, PerfPathKey, PerfProbeSnapshot, PerfRootKind


class PerfFsReader(Protocol):
    """Read-only abstraction over host procfs/sysfs/cgroup/etc trees."""

    def read_key(self, key: PerfPathKey) -> str | None:
        """Read one standard `PerfPathKey` file, returning `None` if absent or unreadable."""
        ...

    def read_preempt(self) -> tuple[str | None, bool]:
        """Read `sched/preempt`, returning `(text, unreadable_due_to_permission)`."""
        ...

    def read_rel(self, root: PerfRootKind, rel: str) -> str | None:
        """Read relative path under `root`, or `None` if absent or unreadable."""
        ...

    def list_rel(self, root: PerfRootKind, rel: str) -> tuple[str, ...]:
        """Return sorted entry names inside `root/rel`, or `()` if not a directory."""
        ...

    def exists_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` exists."""
        ...

    def is_dir_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` is a directory."""
        ...


class PerfProbePort(Protocol):
    """Collect read-only kernel, cgroup, IRQ, storage, and GPU measurements."""

    def collect_snapshot(self, axes: Sequence[PerfAxis] = ALL_PERF_AXES) -> PerfProbeSnapshot:
        """Sample requested responsiveness axes without mutating host state."""
        ...
