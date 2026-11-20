# SPDX-License-Identifier: MPL-2.0
"""Pure domain rules for comparing and updating managed files and marked lines."""

from dataclasses import dataclass
from enum import StrEnum
import posixpath
import re
from typing import Literal

from cst_core.domain.install import Owner, link_read_through
from cst_core.domain.text import file_lines


class OwnedState(StrEnum):
    """The state of a file, or a line, CST manages, against what CST wants there.

    ok: exactly what CST wants; adopt: the same settings written another way
    (CST takes it over); duplicate: the line more than once, all the same;
    change: CST's, but different; missing; conflict: someone else's;
    unreadable; masked: `systemctl mask` left a link to /dev/null at a unit
    file CST writes (cst_core.adapters.files.unit_file_state_at).
    """

    OK = "ok"
    ADOPT = "adopt"
    DUPLICATE = "duplicate"
    CHANGE = "change"
    MISSING = "missing"
    CONFLICT = "conflict"
    UNREADABLE = "unreadable"
    MASKED = "masked"


# A marked line or a managed file (MarkedLineSpec.inspect, inspect_managed_file).
type ManagedState = Literal[
    OwnedState.OK,
    OwnedState.ADOPT,
    OwnedState.DUPLICATE,
    OwnedState.CHANGE,
    OwnedState.MISSING,
    OwnedState.CONFLICT,
    OwnedState.UNREADABLE,
]
# A file CST writes whole (owned_file_state, cst_core.adapters.files.owned_file_state_at).
type WholeFileState = Literal[OwnedState.OK, OwnedState.CHANGE, OwnedState.CONFLICT, OwnedState.MISSING]
# A systemd unit file CST writes whole: a WholeFileState, or OwnedState.MASKED
# (cst_core.adapters.files.unit_file_state_at).
type UnitFileState = WholeFileState | Literal[OwnedState.MASKED]

_UNMARKED_KEY_RE = re.compile(r"^\s*([^\s#]\S*)")


@dataclass(frozen=True, slots=True)
class MarkedLineSpec:
    """Specification for a single CST-managed line inside a shared config file."""

    desired_line: str
    key: str
    current_marker: str
    legacy_markers: tuple[str, ...] = ()

    def all_markers(self) -> tuple[str, ...]:
        """Return current and legacy marker comments recognized for this line."""
        return (self.current_marker, *self.legacy_markers)

    def is_marked(self, line: str) -> bool:
        """Return True if line carries the current marker or any legacy marker."""
        return any(marker in line for marker in self.all_markers())

    def payload(self, line: str) -> str:
        """Return line without its marker comment suffix or trailing whitespace."""
        text = line
        for marker in self.all_markers():
            if marker in text:
                text = text.split(marker, 1)[0]
        return text.rstrip()

    def inspect(self, content: str | None, *, exists: bool, readable: bool) -> ManagedState:
        """Classify the state of this marked line inside a file's contents."""
        if exists and not readable:
            return OwnedState.UNREADABLE
        if not exists or content is None:
            return OwnedState.MISSING
        scan = self._scan(content)
        if scan is None:
            return OwnedState.CONFLICT
        count, first, all_same_payload = scan
        if count == 0:
            return OwnedState.MISSING
        if count > 1:
            return OwnedState.DUPLICATE if all_same_payload else OwnedState.CHANGE
        if first == self.desired_line:
            return OwnedState.OK
        if all_same_payload:
            return OwnedState.ADOPT
        return OwnedState.CHANGE

    def _scan(self, content: str) -> tuple[int, str, bool] | None:
        """(how many lines carry a marker, the first of them, whether all of them hold the desired
        line's payload); None at the first unmarked line for the same key (a conflict)."""
        want_payload = self.payload(self.desired_line)
        count = 0
        first = ""
        all_same_payload = True
        for line in file_lines(content):
            if self.is_marked(line):
                count += 1
                if count == 1:
                    first = line
                if self.payload(line) != want_payload:
                    all_same_payload = False
            else:
                match = _UNMARKED_KEY_RE.match(line)
                if match and match.group(1) == self.key:
                    return None
        return count, first, all_same_payload

    def render_updated(self, existing_content: str) -> str:
        """Return updated file content with the first marked line replaced and duplicates dropped."""
        out_lines: list[str] = []
        placed = False
        for line in file_lines(existing_content):
            if self.is_marked(line):
                if not placed:
                    out_lines.append(self.desired_line)
                    placed = True
            else:
                out_lines.append(line)
        if not placed:
            out_lines.append(self.desired_line)
        return "\n".join(out_lines) + "\n"


def owned_file_state(
    content: bytes | None, *, is_link: bool, wanted: bytes | None, ours: bool, owner: Owner
) -> WholeFileState:
    """State of a file CST writes whole (a unit, an installed program) against what it wants there.

    content None = no file (for a link read through: no target). ok: exactly
    wanted; change: CST's (ours: it carries CST's marker) but different;
    conflict: someone else's file; missing. A symbolic link follows
    install.link_read_through, the installer's rule: at a root destination
    it is "change" (a write replaces the link, never follows it); at a user
    destination content and ours are its target's, and a write replaces the
    link itself.
    """
    if is_link and not link_read_through(owner):
        return OwnedState.CHANGE
    if content is None:
        return OwnedState.MISSING
    if wanted is not None and content == wanted:
        return OwnedState.OK
    return OwnedState.CHANGE if ours else OwnedState.CONFLICT


def effective_lines(content: str) -> list[str]:
    """Return non-empty, non-comment lines in file order."""
    return [line for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def inspect_managed_file(
    desired_content: str,
    actual_content: str | None,
    *,
    exists: bool,
    is_symlink: bool,
    readable: bool,
) -> ManagedState:
    """Compare a managed file on disk with its desired content."""
    if is_symlink:
        return OwnedState.CHANGE
    if not exists:
        return OwnedState.MISSING
    if not readable or actual_content is None:
        return OwnedState.UNREADABLE
    if actual_content == desired_content:
        return OwnedState.OK
    if effective_lines(desired_content) == effective_lines(actual_content):
        return OwnedState.ADOPT
    return OwnedState.CHANGE


def checked_path(path: str) -> str:
    """`path` when it is absolute and already normal (no `..`, `.`, `//`, NUL), else ValueError.

    Programs that write as root take paths from the manifest, their settings
    or their command line: a path that is not plainly absolute is refused,
    never resolved into somewhere else.
    """
    if not path.startswith("/") or path.startswith("//") or "\0" in path or posixpath.normpath(path) != path:
        raise ValueError(f"{path!r}: not a plain absolute path")
    return path
