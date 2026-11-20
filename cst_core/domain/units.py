# SPDX-License-Identifier: MPL-2.0
"""systemd units: their files as CST writes them (header comment lines, then sections), and
systemd's ActiveState words (ActiveState).

    render_unit_file(["# Managed by ...", "# why"], [("Unit", ["Description=x"]), ("Service", [...])])

gives the header lines, then each `[Section]` with its lines (comment lines
allowed), sections separated by one blank line, ending in a newline.
"""

from collections.abc import Sequence
from enum import StrEnum
import posixpath

# `systemctl mask NAME` leaves a link to this file where the unit file goes.
MASK_TARGET = "/dev/null"


def render_unit_file(header: Sequence[str], sections: Sequence[tuple[str, Sequence[str]]]) -> str:
    """The unit file text."""
    body = "\n\n".join("\n".join([f"[{name}]", *lines]) for name, lines in sections)
    return "\n".join([*header, body]) + "\n"


def render_timer(
    header: Sequence[str],
    description: str,
    on_calendar: str,
    randomized_delay: str,
    *,
    persistent: bool = True,
) -> str:
    """A timer unit started by timers.target: OnCalendar with a random delay."""
    timer = [f"OnCalendar={on_calendar}", f"RandomizedDelaySec={randomized_delay}"]
    if persistent:
        timer.append("Persistent=true")
    return render_unit_file(
        header,
        [("Unit", [f"Description={description}"]), ("Timer", timer), ("Install", ["WantedBy=timers.target"])],
    )


def masked_refusal(paths: Sequence[str], *, user: bool) -> str | None:
    """Why a command that would write or enable these unit files refuses: each
    one is masked (`systemctl mask`: a link to /dev/null). None when the list is
    empty. CST never unmasks a unit by itself: the mask is the user's choice, and
    writing the file would silently undo it. user: a unit of the user manager."""
    if not paths:
        return None
    unmask = "systemctl --user unmask" if user else "sudo systemctl unmask"
    return "; ".join(
        f"{name} is masked (systemctl mask); unmask it first: {unmask} {name}"
        for name in (posixpath.basename(p) for p in paths)
    )


class ActiveState(StrEnum):
    """A unit's ActiveState as `systemctl show` prints it: systemd's eight words.

    Each module keeps its own subset of what counts as running or stopped for it, a frozenset of
    these members, since they do not agree (reloading, refreshing, maintenance). The text from
    systemctl stays a plain str and is compared with members (==, in a frozenset of them); it is
    never parsed with ActiveState(text), which would raise on a word a newer systemd adds."""

    ACTIVE = "active"
    RELOADING = "reloading"
    INACTIVE = "inactive"
    FAILED = "failed"
    ACTIVATING = "activating"
    DEACTIVATING = "deactivating"
    MAINTENANCE = "maintenance"
    REFRESHING = "refreshing"
