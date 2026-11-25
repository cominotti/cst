# SPDX-License-Identifier: MPL-2.0
"""systemd units: their files as CST writes them (header comment lines, then sections), the answer
of `systemctl show` (parse_show_props, parse_show_units), the question of every loaded unit of a glob
(show_units_argv) and of a property of the manager itself (show_manager_value_argv), the folder a user
manager reads the user's own units from (user_unit_dir), and systemd's ActiveState words (ActiveState).

    render_unit_file(["# Managed by ...", "# why"], [("Unit", ["Description=x"]), ("Service", [...])])

gives the header lines, then each `[Section]` with its lines (comment lines
allowed), sections separated by one blank line, ending in a newline.
"""

from collections.abc import Sequence
from enum import StrEnum
import posixpath
from typing import Final

# `systemctl mask NAME` leaves a link to this file where the unit file goes.
MASK_TARGET = "/dev/null"

# The folders systemd loads the unit files and drop-ins CST writes from: the system manager's, the one
# every user manager reads (root writes it), and a user's own, relative to the home.
SYSTEM_UNIT_DIR: Final = "/etc/systemd/system"
GLOBAL_USER_UNIT_DIR: Final = "/etc/systemd/user"
CONFIG_UNIT_DIR_REL: Final = "systemd/user"  # below the user's config folder ($XDG_CONFIG_HOME or ~/.config)
USER_UNIT_DIR_REL: Final = f".config/{CONFIG_UNIT_DIR_REL}"  # systemd's default, relative to the home
# A manager's own property: the folders it loads unit files from, highest priority first.
UNIT_PATH_PROP: Final = "UnitPath"
# Folders of systemd's own and the system's, never the user's own: where a UnitPath split in the wrong place
# (a folder with a space) could land.
_SYSTEM_UNIT_ROOTS: Final = ("/etc/", "/run/", "/usr/")  # not /var/: homes live in /var/home on Fedora Atomic


def parse_show_props(text: str) -> dict[str, str]:
    """One unit's PROP=VALUE lines of `systemctl show` as {prop: value} (a line without = is skipped)."""
    return {key: value for key, sep, value in (line.partition("=") for line in text.splitlines()) if sep}


def parse_show_units(text: str) -> list[dict[str, str]]:
    """The answer of `systemctl show -p PROP... PATTERN` over several units: one {prop: value} per unit
    (parse_show_props of each block, blocks parted by an empty line), in systemctl's order; a block with
    no PROP=VALUE line is skipped."""
    return [unit for block in text.split("\n\n") if (unit := parse_show_props(block))]


def show_units_argv(head: Sequence[str], pattern: str, props: Sequence[str]) -> list[str]:
    """`HEAD... show --state=loaded -p PROP... PATTERN` (HEAD: `systemctl`, or `systemctl --user`): every
    unit the manager has loaded whose name matches the glob, active or not (without --state=loaded a glob
    hides the inactive units that have no job queued), and it loads none. SystemctlAdapter.show_units runs
    it; a caller that runs systemctl through another runner (cst doctor's host) builds it here, so both ask
    the same question."""
    return [*head, "show", f"--state={LoadState.LOADED}", *(a for p in props for a in ("-p", p)), pattern]


def show_manager_value_argv(head: Sequence[str], prop: str) -> list[str]:
    """`HEAD... show -p PROP --value`: a property of the manager itself (no unit named), such as its
    UnitPath. SystemctlAdapter.show_value(None, PROP) runs it; a caller that runs systemctl through
    another runner (cst doctor's host) builds it here, so both ask the same question."""
    return [*head, "show", "-p", prop, "--value"]


def user_unit_dir(unit_path: str | None, home: str) -> str:
    """The folder a user manager loads the user's own unit files from, by its UnitPath (the answer of
    show -p UnitPath --value: absolute folders, space separated): the first entry that ends in
    /systemd/user, which is $XDG_CONFIG_HOME/systemd/user only when the manager itself has that variable
    (one set in a shell, in environment.d or with set-environment does not move it), else
    ~/.config/systemd/user. HOME/.config/systemd/user, systemd's default, when there is no answer
    (None), no such entry, or the answer cannot be split safely (systemd does not quote the entries: a
    folder with a space gives a relative piece, or lands on a folder of the system's)."""
    default = posixpath.join(home, USER_UNIT_DIR_REL)
    if unit_path is None:
        return default
    entries = unit_path.split()
    if any(not e.startswith("/") for e in entries):
        return default
    found = next((e for e in entries if e.endswith("/" + CONFIG_UNIT_DIR_REL)), None)
    if found is None or found.startswith(_SYSTEM_UNIT_ROOTS):
        return default
    return posixpath.normpath(found)


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


class LoadState(StrEnum):
    """A unit's LoadState as `systemctl show` prints it: the words of `systemctl --state=help`'s load
    states. Compared as ActiveState is, never parsed with LoadState(text)."""

    STUB = "stub"
    LOADED = "loaded"
    NOT_FOUND = "not-found"
    BAD_SETTING = "bad-setting"
    ERROR = "error"
    MERGED = "merged"
    MASKED = "masked"


class EnablementState(StrEnum):
    """A unit file's enablement as `systemctl is-enabled` prints it (and `systemctl show`'s
    UnitFileState): the words of systemctl(1)'s is-enabled table.

    As with ActiveState, the text from systemctl stays a plain str compared with members; it is never
    parsed with EnablementState(text), which would raise on a word a newer systemd adds."""

    ENABLED = "enabled"
    ENABLED_RUNTIME = "enabled-runtime"
    LINKED = "linked"
    LINKED_RUNTIME = "linked-runtime"
    ALIAS = "alias"
    MASKED = "masked"
    MASKED_RUNTIME = "masked-runtime"
    STATIC = "static"
    INDIRECT = "indirect"
    DISABLED = "disabled"
    GENERATED = "generated"
    TRANSIENT = "transient"
    BAD = "bad"
    NOT_FOUND = "not-found"
