"""Run locks: one run at a time, through flock(2) on a lock file."""

import fcntl
import os


def take_lock(path: str, *, wait: bool, mode: int = 0o600, nofollow: bool = True) -> int | None:
    """An open fd holding an exclusive flock on path (close it to unlock).

    wait=False: None when another process holds the lock. The file is
    created with mode when missing; nofollow refuses a symbolic link there
    (OSError), so a planted link cannot point the lock elsewhere.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | (os.O_NOFOLLOW if nofollow else 0)
    fd = os.open(path, flags, mode)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd
