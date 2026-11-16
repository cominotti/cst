"""Pure domain model, value objects, and policy rules for the `btrfs` bounded context."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChromeCachePolicy:
    """Bounded `/tmp` HTTP cache policy keeping Flatpak Chrome caches off `/home` snapshots."""

    app_id: str = "com.google.Chrome"
    cache_dir: str = "/tmp/chrome-cache"
    cache_size_bytes: int = 536870912

    def flags(self) -> tuple[str, str]:
        """Return the two cobalt launcher flags enforcing the cache path and cap."""
        return (f"--disk-cache-dir={self.cache_dir}", f"--disk-cache-size={self.cache_size_bytes}")


@dataclass(frozen=True, slots=True)
class DockerGcPolicy:
    """Conservative Docker retention and protected-registry policy."""

    keep_container_days: int = 7
    keep_image_days: int = 14
    keep_build_cache_days: int = 14
    protected_registries: tuple[str, ...] = ("registry.example.com/",)

    def is_protected_image(self, repo_tag: str) -> bool:
        """Return whether `repo_tag` belongs to a registry shielded from automatic GC."""
        return any(repo_tag.startswith(prefix) for prefix in self.protected_registries)


@dataclass(frozen=True, slots=True)
class StallMonitorThresholds:
    """Episode entry, quiet exit, and hung-task thresholds for `btrfs-stall-monitord`."""

    enter_io_some_pct: float = 10.0
    enter_commit_ms: int = 1000
    enter_d_tasks: int = 3
    quiet_io_some_pct: float = 5.0
    quiet_d_tasks: int = 1
    quiet_seconds_to_close: int = 10
    hung_seconds: int = 30

    def should_enter_episode(self, io_some_pct: float, max_commit_ms: int, d_storage_tasks: int) -> bool:
        """Return True when IO pressure, commit latency, or storage D-state count opens an episode."""
        return (
            io_some_pct >= self.enter_io_some_pct
            or max_commit_ms >= self.enter_commit_ms
            or d_storage_tasks >= self.enter_d_tasks
        )

    def is_quiet(self, io_some_pct: float, max_commit_ms: int, d_storage_tasks: int) -> bool:
        """Return True when metrics are below quiet thresholds for episode closing."""
        return (
            io_some_pct < self.quiet_io_some_pct
            and max_commit_ms < self.enter_commit_ms
            and d_storage_tasks <= self.quiet_d_tasks
        )
