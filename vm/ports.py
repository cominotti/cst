"""Ports (interfaces) for the `vm` bounded context."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class VirshPort(Protocol):
    """Interface to `virsh` commands for libvirt VM inspection and snapshots."""

    def run(self, *args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
        """Execute `virsh` with `args` and return `(returncode, stdout, stderr)`."""
        ...


class VmRuntimePort(Protocol):
    """Execution port delegating CLI subcommands to `vm/cst-vm`."""

    def main(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments to `vm/cst-vm`."""
        ...
