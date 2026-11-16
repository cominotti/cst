"""Application use cases for the `btrfs` bounded context."""

from __future__ import annotations

from collections.abc import Sequence

from btrfs.domain import (
    ChromeCachePolicy,
    DockerGcPolicy,
    SnapshotRetentionSpec,
    StallMonitorThresholds,
)
from btrfs.ports import BtrfsHelperRuntimePort


class BtrfsService:
    """Coordinates `btrfs` domain policy evaluation and helper CLI delegation."""

    def __init__(self, runtime: BtrfsHelperRuntimePort) -> None:
        self._runtime = runtime

    def parse_retention(self, spec: str) -> SnapshotRetentionSpec:
        """Validate and parse a snapshot retention specification."""
        return SnapshotRetentionSpec.parse(spec)

    def evaluate_stall(
        self,
        io_some_pct: float,
        max_commit_ms: int,
        d_storage_tasks: int,
        thresholds: StallMonitorThresholds | None = None,
    ) -> bool:
        """Evaluate whether instantaneous IO/commit/D-state metrics open a stall episode."""
        active = StallMonitorThresholds() if thresholds is None else thresholds
        return active.should_enter_episode(io_some_pct, max_commit_ms, d_storage_tasks)

    def chrome_cache_flags(self, policy: ChromeCachePolicy | None = None) -> tuple[str, str]:
        """Return Chrome cache flags derived from `policy`."""
        resolved = ChromeCachePolicy() if policy is None else policy
        return resolved.flags()

    def is_protected_image(self, repo_tag: str, policy: DockerGcPolicy | None = None) -> bool:
        """Check whether `repo_tag` matches a protected Docker registry prefix."""
        resolved = DockerGcPolicy() if policy is None else policy
        return resolved.is_protected_image(repo_tag)

    def run_helper(self, helper_name: str, argv: Sequence[str]) -> int:
        """Execute a named `btrfs/` standalone helper via the runtime port."""
        return self._runtime.run_helper(helper_name, argv)
