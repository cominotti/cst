"""systemd unit files as CST writes them: header comment lines, then sections.

    render_unit_file(["# Managed by ...", "# why"], [("Unit", ["Description=x"]), ("Service", [...])])

gives the header lines, then each `[Section]` with its lines (comment lines
allowed), sections separated by one blank line, ending in a newline.
"""

from collections.abc import Sequence
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
