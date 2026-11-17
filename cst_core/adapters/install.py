"""Local files for the install use case: atomic writes, modes, root ownership, SELinux labels."""

import os
import stat

from cst_core.adapters import write_bytes_atomic
from cst_core.domain.files import checked_path
from cst_core.domain.install import OnDisk
from cst_core.ports import WriteFailed


class LocalInstallTarget:
    """InstallTargetPort on this machine's files.

    Run as root, written files are owned by root and relabelled
    (restorecon). Files in a user's home are written by a process running
    as that user (cst_install_as drops privileges first), never by root.
    """

    def current(self, path: str) -> OnDisk | None:
        """What is at path: the regular file read through a link (data None when there is none to read).

        None when nothing is there at all. Whether a link counts as current
        is domain.install.link_read_through's rule; write() replaces it.
        """
        path = checked_path(path)
        try:
            is_link = os.path.islink(path)
            st = os.stat(path)
        except OSError:
            return OnDisk(None, 0, -1, -1, True) if os.path.lexists(path) else None
        data: bytes | None = None
        if stat.S_ISREG(st.st_mode):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None
        return OnDisk(data, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, is_link)

    def write(self, path: str, data: bytes, mode: int) -> None:
        """Atomic replace (a link at path is replaced, not followed); WriteFailed naming path on an error."""
        path = checked_path(path)
        try:
            write_bytes_atomic(path, data, mode=mode)
        except OSError as e:
            raise WriteFailed(path, e.strerror or str(e)) from None
