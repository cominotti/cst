"""Local files for the install use case: atomic writes, modes, root ownership, SELinux labels."""

import os
import stat

from cst_core.adapters import write_bytes_atomic
from cst_core.domain.files import checked_path


class LocalInstallTarget:
    """InstallTargetPort on this machine's files.

    Run as root, written files are owned by root and relabelled
    (restorecon). Files in a user's home are written by a process running
    as that user (cst_install_as drops privileges first), never by root.
    """

    def current(self, path: str) -> tuple[bytes, int] | None:
        """(content, permission bits) of the regular file at path, read through a link; None when absent or not a file.

        A link to the wanted content is current, as the old install_file's
        `[[ -f ]] && cmp -s` saw it: it stays a link. write() replaces it.
        """
        path = checked_path(path)
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            with open(path, "rb") as fh:
                return fh.read(), stat.S_IMODE(st.st_mode)
        except OSError:
            return None

    def write(self, path: str, data: bytes, mode: int) -> None:
        """Atomic replace (a link at path is replaced, not followed)."""
        write_bytes_atomic(checked_path(path), data, mode=mode)
