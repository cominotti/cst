# SPDX-License-Identifier: MPL-2.0
"""Port interfaces (typing.Protocol) specific to the swap module."""

from typing import Protocol

from cst_core.ports import CommandResult


class SwapSystemPort(Protocol):
    """Low-level Linux system probes and actuators required by the swap module."""

    def blockdev_size(self, dev_path: str) -> str:
        """`blockdev --getsize64` as it printed it; raises CommandFailed when it fails."""
        ...

    def blkid_type(self, dev_path: str) -> str: ...

    def modprobe(self, module: str) -> None: ...

    def apply_sysctl_file(self, sysctl_conf: str, *, exclude_key: str | None = None) -> CommandResult:
        """`sysctl -q -p` on the file (one key left out); its exit status and output."""
        ...

    def reset_zram0(self) -> None: ...

    def swapon_show(self) -> CommandResult:
        """`swapon --show`: its exit status and output."""
        ...
