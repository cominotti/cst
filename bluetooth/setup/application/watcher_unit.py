# SPDX-License-Identifier: MPL-2.0
"""The LDAC watcher's unit file, for plan, status, apply and revert: the folder the user manager reads
the user's own units from (its UnitPath), not the shell's XDG_CONFIG_HOME, which the manager does not
read; and the copies an older CST wrote and recorded elsewhere (under the shell's XDG_CONFIG_HOME of its
run), found in the user's install record."""

import posixpath

from collections.abc import Callable

from bluetooth.domain import LDAC_UNIT, MARKER
from bluetooth.setup.domain import LW_STALE_FOREIGN_NOTE, LW_STALE_NOTE
from bluetooth.setup.ports import SetupFilesPort
from cst_core.application.install import record_file
from cst_core.domain.install import Component, Owner
from cst_core.domain.record import RecordFormatError, component_dests
from cst_core.domain.units import UNIT_PATH_PROP, user_unit_dir
from cst_core.ports import InstallRecordPort, SystemdPort


def unit_file(user_manager: SystemdPort, home: str) -> str:
    """The watcher's unit file in the folder the user manager names first in its UnitPath (cst_core's
    user_unit_dir: ~/.config/systemd/user when it cannot be asked)."""
    r = user_manager.show_value(None, UNIT_PATH_PROP)
    return posixpath.join(user_unit_dir(r.stdout if r.returncode == 0 else None, home), LDAC_UNIT)


def older_copies(records: InstallRecordPort, home: str, current: str) -> tuple[str, ...]:
    """The watcher's unit files the user's install record lists elsewhere than current (component
    bluetooth:ldac, the unit's name); none when the record cannot be read (as before: no row), also when
    its path, the installer's for this home (record_file), is not plain (a HOME with a "." in it)."""
    try:
        rec = records.read(record_file(Owner.USER, home))
    except OSError, RecordFormatError, ValueError:
        return ()
    if rec is None:
        return ()
    dests = component_dests(rec, Component.BLUETOOTH_LDAC)
    return tuple(d for d in dests if posixpath.basename(d) == LDAC_UNIT and d != current)


def is_ours_or_gone(files: SetupFilesPort, path: str) -> bool:
    """An older copy apply removes: CST's (its marker), or gone already (only its record line is left);
    any other file there is left alone."""
    return not files.exists(path) or files.contains(path, MARKER, quiet=True)


def stale_rows(
    files: SetupFilesPort, copies: tuple[str, ...], tilde: Callable[[str], str]
) -> list[tuple[str, str, str]]:
    """plan's row of each older copy (word, label, note): apply removes it, or, one CST did not write,
    leaves it alone and drops its record line."""
    return [
        ("remove", tilde(p), LW_STALE_NOTE)
        if is_ours_or_gone(files, p)
        else ("forget", tilde(p), LW_STALE_FOREIGN_NOTE)
        for p in copies
    ]
