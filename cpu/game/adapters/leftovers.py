# SPDX-License-Identifier: MPL-2.0
"""game.leftovers on the machine: the file the profile keeps what 'off' could not undo in (its steps are
cpu.game.application.leftovers, its format cpu.game.domain.leftovers)."""

import os

from cst_core.adapters.files import LocalFileSystemAdapter, write_atomic


class GameLeftoversFile:
    """game.leftovers at path, read byte for byte (as it is written back)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def read(self) -> str | None:
        """Its text; None when it is not there, the OSError of a file there that cannot be read."""
        return LocalFileSystemAdapter().read_existing_text(self._path)

    def write(self, text: str) -> None:
        """The file replaced whole, mode 0644 (in /run: no fsync, no relabel)."""
        write_atomic(self._path, text, mode=0o644, durable=False, relabel=False)

    def remove(self) -> None:
        """The file removed."""
        os.unlink(self._path)
