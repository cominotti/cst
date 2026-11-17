"""Pure domain rules for comparing and updating managed files and marked lines."""

from dataclasses import dataclass
import posixpath
import re
from typing import Literal

from cst_core.domain.install import Owner, link_read_through
from cst_core.domain.text import file_lines

type ManagedState = Literal["ok", "adopt", "duplicate", "change", "missing", "conflict", "unreadable"]
type OwnedState = Literal["ok", "change", "conflict", "missing"]
# A systemd unit file CST writes whole: an OwnedState, or "masked" (systemctl mask left a link to
# /dev/null there; cst_core.adapters.files.unit_file_state_at).
type UnitFileState = OwnedState | Literal["masked"]

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
            return "unreadable"
        if not exists or content is None:
            return "missing"

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
                    return "conflict"

        if count == 0:
            return "missing"
        if count > 1:
            return "duplicate" if all_same_payload else "change"
        if first == self.desired_line:
            return "ok"
        if all_same_payload:
            return "adopt"
        return "change"

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
) -> OwnedState:
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
        return "change"
    if content is None:
        return "missing"
    if wanted is not None and content == wanted:
        return "ok"
    return "change" if ours else "conflict"


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
        return "change"
    if not exists:
        return "missing"
    if not readable or actual_content is None:
        return "unreadable"
    if actual_content == desired_content:
        return "ok"
    if effective_lines(desired_content) == effective_lines(actual_content):
        return "adopt"
    return "change"


def checked_path(path: str) -> str:
    """`path` when it is absolute and already normal (no `..`, `.`, `//`, NUL), else ValueError.

    Programs that write as root take paths from the manifest, their settings
    or their command line: a path that is not plainly absolute is refused,
    never resolved into somewhere else.
    """
    if not path.startswith("/") or path.startswith("//") or "\0" in path or posixpath.normpath(path) != path:
        raise ValueError(f"{path!r}: not a plain absolute path")
    return path
