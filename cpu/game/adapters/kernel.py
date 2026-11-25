# SPDX-License-Identifier: MPL-2.0
"""The cgroup tree and /proc as the game profile reads and writes them (cpu.game.ports.GameKernelPort), and the
kernel's one-value files the power adapter reads and writes the same way (GameSysValues)."""

from collections.abc import Iterator
import glob
import locale
import os

from cst_core.adapters.files import read_text
from cpu.game.adapters.host import holds, read_stripped, write_sysfs_line


class GameSysValues:
    """The kernel's files that hold one value (the cgroup tree, /proc, sysfs) as files
    (cpu.game.ports.GameSysValuesPort)."""

    def read_text(self, path: str) -> str | None:
        """The file's text; None when it cannot be read."""
        return read_text(path)

    def read_stripped(self, path: str) -> str | None:
        """The file's text without its last newlines; None when it cannot be read."""
        return read_stripped(path)

    def is_dir(self, path: str) -> bool:
        """The folder is there."""
        return os.path.isdir(path)

    def writable(self, path: str) -> bool:
        """The file can be written."""
        return os.access(path, os.W_OK)

    def write(self, path: str, value: str) -> bool:
        """value and a newline written to the file; False when the write failed."""
        return write_sysfs_line(path, value)


class GameKernelFiles(GameSysValues):
    """The cgroup tree and /proc as files."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        return os.path.exists(path)

    def lexists(self, path: str) -> bool:
        """The path is there (a link counts)."""
        return os.path.lexists(path)

    def holds(self, path: str, needle: str) -> bool:
        """The file holds needle (grep -qsF)."""
        return holds(path, needle)

    def process_maps(self, proc: str) -> list[str]:
        """Every PROC/PID/maps there is, sorted."""
        return sorted(glob.glob(os.path.join(glob.escape(proc), "[0-9]*", "maps")))

    def boxes(self, folder: str, pattern: str) -> list[str]:
        """The folders FOLDER/PATTERN matches, in the order of the locale's collation."""
        found = sorted(glob.glob(f"{glob.escape(folder)}/{pattern}"), key=locale.strxfrm)
        return [d for d in found if os.path.isdir(d)]

    def procs_texts(self, folder: str, depth: int) -> Iterator[str]:
        """The text of each cgroup.procs in the folder and below it, depth levels below at most."""
        top = folder.rstrip("/").count("/")
        for d, subdirs, files in os.walk(folder):
            if d.rstrip("/").count("/") - top >= depth:
                subdirs.clear()
            if "cgroup.procs" in files:
                yield read_text(os.path.join(d, "cgroup.procs")) or ""
