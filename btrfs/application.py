"""Application use cases for the `btrfs` bounded context."""

from __future__ import annotations

from btrfs.domain import ChromeCachePolicy, DockerGcPolicy, StallMonitorThresholds


class BtrfsService:
    """Coordinates `btrfs` domain policy evaluation."""

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
