"""Local files for the install use case: atomic writes, modes, root ownership, SELinux labels."""

import os
import stat

from cst_core.adapters import write_bytes_atomic


class LocalInstallTarget:
    """InstallTargetPort on this machine's files.

    Run as root, written files are owned by root and relabelled
    (restorecon). Files in a user's home are written by a process running
    as that user (cst_install_as drops privileges first), never by root.
    """

    def current(self, path: str) -> tuple[bytes, int] | None:
        """(content, permission bits) of a regular file; None when absent, a link or not a file."""
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            with open(path, "rb") as fh:
                return fh.read(), stat.S_IMODE(st.st_mode)
        except OSError:
            return None

    def write(self, path: str, data: bytes, mode: int) -> None:
        """Atomic replace (a link at path is replaced, not followed)."""
        write_bytes_atomic(path, data, mode=mode)
