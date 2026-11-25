# SPDX-License-Identifier: MPL-2.0
"""What stands in the way of a path apply creates or replaces: apply refuses it before its first write."""

import posixpath

from cst_core.ports import FileSystemPort


def non_folder_above(fs: FileSystemPort, path: str) -> str | None:
    """The nearest name above path that is there, when it is not a folder (a file, a link that leads to none):
    the installer cannot create path below it. None when that name is a folder, or nothing above is there."""
    folder = posixpath.dirname(path)
    while folder not in ("", "/"):
        if fs.lexists(folder):
            return None if fs.is_dir(folder) else folder
        folder = posixpath.dirname(folder)
    return None


def folder_in_place(fs: FileSystemPort, path: str) -> bool:
    """A folder (not a link to one) is at path, where apply renames a link over it or unlinks it."""
    return fs.is_dir(path) and not fs.is_symlink(path)
