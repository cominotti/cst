# SPDX-License-Identifier: MPL-2.0
"""Run locks: one run at a time, through flock(2) on a lock file."""

import fcntl
import os
import time

from cst_core.domain.files import checked_path


def take_lock(
    path: str, *, wait: bool, mode: int = 0o600, nofollow: bool = True, timeout: float | None = None
) -> int | None:
    """An open fd holding an exclusive flock on path (close it to unlock).

    wait=False: None when another process holds the lock. A lock file this
    call creates gets exactly mode (fchmod: the umask does not reduce it, so
    a reader such as cst doctor can open a 0644 lock under sudo with a 077
    umask); an existing one keeps its mode. nofollow refuses a symbolic link
    there (OSError), so a planted link cannot point the lock elsewhere. path
    is a plain absolute path (ValueError otherwise). timeout (with wait=True):
    None when the lock is still held after that many seconds.
    """
    path = checked_path(path)
    flags = os.O_WRONLY | os.O_CLOEXEC | (os.O_NOFOLLOW if nofollow else 0)
    fd = _open_lock_file(path, flags, mode)
    deadline = None if timeout is None or not wait else time.monotonic() + timeout
    try:
        locked = _flock(fd, wait=wait, deadline=deadline)
    except BaseException:
        os.close(fd)
        raise
    if not locked:
        os.close(fd)
        return None
    return fd


def _open_lock_file(path: str, flags: int, mode: int) -> int:
    """An open fd of the lock file at path: created with exactly mode, or the one already there
    (it keeps its mode). The fd of a new file is closed when its fchmod fails."""
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        # Already there (or a link: refused here with nofollow). One plain open, no retry loop:
        # a file removed in between is created by it, with the umask applied, as before.
        return os.open(path, flags | os.O_CREAT, mode)
    try:
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _flock(fd: int, *, wait: bool, deadline: float | None) -> bool:
    """An exclusive flock on fd: True once held; False when another process holds it (wait=False,
    or still at the deadline). Without a deadline, wait=True blocks until the lock is free."""
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait and deadline is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if deadline is None or time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        else:
            return True
