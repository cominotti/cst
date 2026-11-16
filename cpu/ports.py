"""Ports (interfaces) for the `cpu` bounded context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class VirshPort(Protocol):
    """Interface to `virsh` commands for VM CPU pinning and status."""

    def run(self, *args: str, check: bool = True) -> str:
        """Execute `virsh` with `args` and return stdout."""
        ...


class CpuWatchRuntimePort(Protocol):
    """Execution port delegating telemetry and quiet checks to `cpu/cpu-watch`."""

    def main(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments to `cpu-watch`."""
        ...

    def watch(self, argv: Sequence[str]) -> int:
        """Run `cpu-watch watch` with `argv`."""
        ...

    def report(self, argv: Sequence[str]) -> int:
        """Run `cpu-watch report` with `argv`."""
        ...

    def quiet(self, argv: Sequence[str], *, environ: Mapping[str, str] | None = None) -> int:
        """Run `cpu-watch quiet` with `argv` and optional environment."""
        ...


class VmPinRuntimePort(Protocol):
    """Execution port delegating VM pinning commands to `cpu/vm-pin`."""

    def main(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments to `cpu-vm-pin`."""
        ...
