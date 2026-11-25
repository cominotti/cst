# SPDX-License-Identifier: MPL-2.0
"""The LDAC watcher's unit file, for plan, status, apply and revert: the folder the user manager reads
the user's own units from (its UnitPath), not the shell's XDG_CONFIG_HOME, which the manager does not
read; and the copies an older CST wrote and recorded elsewhere (under the shell's XDG_CONFIG_HOME of its
run), found in the user's install record."""

import posixpath

from collections.abc import Callable

from bluetooth.domain import LDAC_UNIT, MARKER
from bluetooth.setup.domain import (
    LW_STALE_FOREIGN_NOTE,
    LW_STALE_LINK_NOTE,
    LW_STALE_NOTE,
    LW_STALE_SAME_NOTE,
    LW_WANTS_DIR,
    OlderCopy,
)
from bluetooth.setup.ports import SetupFilesPort
from cst_core.application.install import record_file
from cst_core.domain.install import Component, Owner
from cst_core.domain.record import RecordFormatError, component_dests
from cst_core.domain.units import UNIT_PATH_PROP, named_user_unit_dir, user_unit_dir
from cst_core.ports import InstallRecordPort, SystemdPort


def unit_path(user_manager: SystemdPort) -> str | None:
    """The user manager's UnitPath (show -p UnitPath --value); None when it does not answer."""
    r = user_manager.show_value(None, UNIT_PATH_PROP)
    return r.stdout if r.returncode == 0 else None


def unit_file(answer: str | None, home: str) -> str:
    """The watcher's unit file in the folder the user manager names first in its UnitPath (cst_core's
    user_unit_dir: ~/.config/systemd/user when it cannot be asked)."""
    return posixpath.join(user_unit_dir(answer, home), LDAC_UNIT)


def older_copies(records: InstallRecordPort, home: str, current: str, answer: str | None) -> tuple[str, ...]:
    """The watcher's unit files the user's install record lists elsewhere than current (component
    bluetooth:ldac, the unit's name); none when the record cannot be read (as before: no row), also when
    its path, the installer's for this home (record_file), is not plain (a HOME with a "." in it). None
    either when the user manager's UnitPath (answer) names no folder of the user's own units
    (named_user_unit_dir): current is then only systemd's default, and a recorded copy elsewhere may be
    the one the manager loads, so every recorded copy is kept (kept_copies)."""
    if named_user_unit_dir(answer) is None:
        return ()
    return _recorded_elsewhere(records, home, current)


def kept_copies(
    files: SetupFilesPort, records: InstallRecordPort, home: str, current: str, answer: str | None
) -> tuple[str, ...]:
    """The recorded copies older_copies keeps because the user manager's UnitPath (answer) names no
    folder: those on disk that are not current by another path. One of them may be the unit the manager
    runs, so revert keeps the watcher's program with them. None when the manager names its folder."""
    if named_user_unit_dir(answer) is not None:
        return ()
    cur = files.realpath(current)
    return tuple(p for p in _recorded_elsewhere(records, home, current) if files.exists(p) and files.realpath(p) != cur)


def _recorded_elsewhere(records: InstallRecordPort, home: str, current: str) -> tuple[str, ...]:
    """The watcher's unit files the user's install record lists elsewhere than current; none when the
    record cannot be read."""
    try:
        rec = records.read(record_file(Owner.USER, home))
    except OSError, RecordFormatError, ValueError:
        return ()
    if rec is None:
        return ()
    dests = component_dests(rec, Component.BLUETOOTH_LDAC)
    return tuple(d for d in dests if posixpath.basename(d) == LDAC_UNIT and d != current)


def copy_kind(files: SetupFilesPort, path: str, current: str) -> OlderCopy:
    """What apply does with a recorded copy at path: the current unit file by another path (a link in
    its folders, /home and /var/home) only loses its record line; CST's (its marker), or one gone
    already, is removed; any other file there is left alone."""
    if files.exists(path) and files.realpath(path) == files.realpath(current):
        return OlderCopy.SAME
    if not files.exists(path) or files.contains(path, MARKER, quiet=True):
        return OlderCopy.REMOVE
    return OlderCopy.FOREIGN


def enable_link(files: SetupFilesPort, path: str) -> str | None:
    """The link `systemctl --user enable` made for the copy at path, in its folder's
    default.target.wants, when it is there and points to that copy (a manager that read that folder
    then); None otherwise."""
    link = posixpath.join(posixpath.dirname(path), LW_WANTS_DIR, posixpath.basename(path))
    if files.is_symlink(link) and files.realpath(link) == files.realpath(path):
        return link
    return None


def stale_rows(
    files: SetupFilesPort, copies: tuple[str, ...], current: str, tilde: Callable[[str], str]
) -> list[tuple[str, str, str]]:
    """plan's row of each older copy (word, label, note): apply removes it, and its enable link, or drops
    only its record line (one CST did not write, left alone; another path to the current file)."""
    notes = {
        OlderCopy.REMOVE: ("remove", LW_STALE_NOTE),
        OlderCopy.FOREIGN: ("forget", LW_STALE_FOREIGN_NOTE),
        OlderCopy.SAME: ("forget", LW_STALE_SAME_NOTE),
    }
    rows = []
    for p in copies:
        kind = copy_kind(files, p, current)
        word, note = notes[kind]
        rows.append((word, tilde(p), note))
        if kind == OlderCopy.REMOVE and (link := enable_link(files, p)) is not None:
            rows.append(("remove", tilde(link), LW_STALE_LINK_NOTE))
    return rows
