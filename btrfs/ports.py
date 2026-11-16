"""Typed boundary protocols (ports) for the `btrfs` bounded context."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class BtrfsCommandPort(Protocol):
    """Port for executing external system commands (`btrfs`, `systemctl`, `docker`, etc.)."""

    def run(self, argv: Sequence[str], *, timeout: int = 60) -> tuple[int, str, str]: ...
