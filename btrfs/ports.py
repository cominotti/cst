"""Typed boundary protocols (ports) for the `btrfs` bounded context."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class BtrfsHelperRuntimePort(Protocol):
    """Port for executing a named `btrfs/` standalone helper with CLI arguments."""

    def run_helper(self, helper_name: str, argv: Sequence[str]) -> int: ...


class BtrfsCommandPort(Protocol):
    """Port for executing external system commands (`btrfs`, `systemctl`, `docker`, etc.)."""

    def run(self, argv: Sequence[str], *, timeout: int = 60) -> tuple[int, str, str]: ...
