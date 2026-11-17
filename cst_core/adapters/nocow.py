# SPDX-License-Identifier: MPL-2.0
"""The btrfs NOCOW attribute (chattr +C) of files and folders, through ioctl.

A folder marked NOCOW makes the files created in it later NOCOW too; an
existing file with data does not change. Paths are opened without following
a final symbolic link.
"""

import fcntl
import os
import struct

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_NOCOW_FL = 0x00800000


def _open(path: str, directory: bool) -> int:
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0))


def inode_flags(path: str, *, directory: bool = False) -> int:
    """The inode attribute flags of path (lsattr). Raises OSError."""
    fd = _open(path, directory)
    try:
        return struct.unpack("l", fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack("l", 0)))[0]
    finally:
        os.close(fd)


def set_nocow(path: str, on: bool = True, *, directory: bool = False) -> bool:
    """Set (or clear) NOCOW on path; True when the flags changed. Raises OSError."""
    fd = _open(path, directory)
    try:
        cur = struct.unpack("l", fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack("l", 0)))[0]
        new = (cur | FS_NOCOW_FL) if on else (cur & ~FS_NOCOW_FL)
        if new == cur:
            return False
        fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("l", new))
        return True
    finally:
        os.close(fd)
