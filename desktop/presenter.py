# SPDX-License-Identifier: MPL-2.0
"""Output presenter for `cst desktop`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO


def render_desktop_output(lines: Sequence[str], exit_code: int, out: TextIO, err: TextIO) -> int:
    """Write output lines cleanly to `out` (or `err` when exit code is non-zero and explicit errors occur)."""
    for line in lines:
        stream = err if exit_code != 0 and line.startswith("ERROR:") else out
        stream.write(f"{line}\n")
    return exit_code
