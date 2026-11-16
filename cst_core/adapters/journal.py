"""One journal line per message: stdout with an sd-daemon priority prefix.

systemd reads `<N>` at the start of a line a service prints as its syslog
priority (3 error, 4 warning, 5 notice, 6 info), so a plain print is a
journal entry with the right level.
"""

import sys
from typing import TextIO


def log(prio: int, msg: str, *, tag: str = "", stream: TextIO | None = None) -> None:
    """Print `<prio>tag: msg` (or `<prio>msg` without a tag), flushed."""
    text = f"<{prio}>{tag}: {msg}" if tag else f"<{prio}>{msg}"
    print(text, file=sys.stdout if stream is None else stream, flush=True)
