# SPDX-License-Identifier: MPL-2.0
"""Port interfaces (typing.Protocol) specific to the swap module."""

from typing import Protocol

from cst_core.ports import CommandResult


class SwapSystemPort(Protocol):
    """Low-level Linux system probes and actuators required by the swap module."""

    def blockdev_size(self, dev_path: str) -> str:
        """`blockdev --getsize64` as it printed it; raises CommandFailed when it fails."""
        ...

    def blkid_type(self, dev_path: str, *, cache: bool = True) -> str:
        """`blkid -o value -s TYPE` of dev_path, '' when it has none; cache False: no cache file
        (`-c /dev/null`), so a plan run as root writes nothing to /run/blkid."""
        ...

    def modprobe(self, module: str) -> None: ...

    def apply_sysctl_file(self, sysctl_conf: str, *, exclude_key: str | None = None) -> CommandResult:
        """`sysctl -q -p` on the file (one key left out); its exit status and output."""
        ...

    def reset_zram0(self) -> None: ...

    def swapon_show(self) -> CommandResult:
        """`swapon --show`: its exit status and output."""
        ...

    def sleep(self, seconds: float) -> None:
        """Pause `seconds` while waiting for an asynchronous unit update."""
        ...


class SwapSettingsFacts(Protocol):
    """What swap.domain's resolve_settings asks of the machine about /etc/cst/swap.conf, as the reader
    sees it (cst swap over its file system, cst doctor over its view of the host). It asks each only
    when it needs it."""

    def settings_text(self) -> str | None:
        """swap.conf's text; None when it could not be read (not there included)."""
        ...

    def settings_seen(self) -> bool:
        """Something is at swap.conf's path (a link too, even one that points nowhere)."""
        ...

    def settings_is_file(self) -> bool:
        """swap.conf is a regular file (through a link)."""
        ...

    def settings_readable(self) -> bool:
        """This user may read swap.conf."""
        ...

    def settings_folder_closed(self) -> bool:
        """The nearest folder above swap.conf that is there is one this user may not search: whether
        swap.conf is there cannot be seen."""
        ...

    def crypttab_closed(self) -> bool:
        """/etc/crypttab is there and this user may not read it."""
        ...

    def crypttab_not_regular(self) -> bool:
        """/etc/crypttab is there (a link that leads nowhere too) and is not a regular file after links (a FIFO,
        a device, a folder): no reader can take its lines."""
        ...

    def crypttab_text(self) -> str:
        """/etc/crypttab's text; "" when it is not there or cannot be read."""
        ...

    def fstab_text(self) -> str:
        """/etc/fstab's text; "" when it is not there or cannot be read."""
        ...

    def partition_size(self, device_path: str) -> int | None:
        """The size in bytes of the block device device_path leads to; None when it leads to none."""
        ...
