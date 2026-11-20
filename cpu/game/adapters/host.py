# SPDX-License-Identifier: MPL-2.0
"""What the game profile reads on the machine besides systemd and its state files, and cst cpu reads too:
the settings file, the accounts, a file's text, the locale's collation."""

import contextlib
import locale
import pwd

from cst_core.adapters.files import read_bytes, read_text
from cst_core.domain.text import SettingsProblem, parse_shell_assignments
from cpu.game.domain import GameSettings, game_settings


def collate_as_bash() -> None:
    """Sort names as bash's globs do: by the collation of the caller's locale (LC_ALL, LC_COLLATE,
    LANG), C when it has none or an unknown one. The order the boxes next to the game are handled
    in, and listed by 'state', is then the bash version's."""
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_COLLATE, "")


def read_settings(conf: str) -> tuple[GameSettings, tuple[SettingsProblem, ...]]:
    """cpu-setup.conf's settings over the defaults (a file that cannot be read: the defaults), and its
    problem lines: lines bash would not read as plain assignments, which set nothing."""
    text = read_text(conf)
    if text is None:
        return game_settings({}), ()
    parsed = parse_shell_assignments(text)
    return game_settings(parsed.values), parsed.problems


def read_snapshot(path: str) -> str | None:
    """The file's text for a snapshot 'state' judges, in one look: None when it is not there; "" when it
    is there but cannot be read (it still counts as there, as os.path.exists said before the snapshot).

    "Not there" is only no such file (ENOENT) or a path through a file (ENOTDIR). Any other error reads
    as there but unreadable, so the profile shows ON: EACCES on a parent folder, ELOOP, ENAMETOOLONG.
    os.path.exists said off for those. It matters only for a user who cannot search the state folder
    (/run/cpu-setup); root and a user who can search it see a missing file as absent."""
    return read_text(path, unreadable="")


def user_id(user: str) -> str:
    """The user's uid as `id -u USER` prints it, "" for an unknown user (quietly: GameProfile says id's
    message where it is wanted)."""
    try:
        return str(pwd.getpwnam(user).pw_uid)
    except KeyError:
        return ""


def user_name(uid: str) -> str:
    """The name of the user with that uid, "" for none."""
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except KeyError, ValueError, OverflowError:
        return ""


def read_stripped(path: str) -> str | None:
    """A file's text without its last newlines (a shell's $(cat FILE)), None when it cannot be read."""
    text = read_text(path)
    return None if text is None else text.rstrip("\n")


def holds(path: str, needle: str) -> bool:
    """The file holds needle (grep -qsF): False when it cannot be read."""
    data = read_bytes(path)
    return data is not None and needle.encode() in data
