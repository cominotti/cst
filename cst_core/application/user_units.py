# SPDX-License-Identifier: MPL-2.0
"""A user unit file (or drop-in) CST writes where the user manager loads it from: the manager's UnitPath,
and the copies an older CST wrote and recorded elsewhere (under a shell's XDG_CONFIG_HOME, say), found in
the user's install record. bluetooth's LDAC watcher, cpu-setup's drop-ins and toolchains' units use it."""

from collections.abc import Callable
import posixpath

from cst_core.application.install import record_file
from cst_core.domain.install import Component, Owner
from cst_core.domain.record import RecordFormatError, component_dests
from cst_core.domain.units import UNIT_PATH_PROP, named_user_unit_dir
from cst_core.ports import InstallRecordPort, SystemdPort


def manager_unit_path(user_manager: SystemdPort) -> str | None:
    """The user manager's UnitPath (show -p UnitPath --value); None when it does not answer."""
    r = user_manager.show_value(None, UNIT_PATH_PROP)
    return r.stdout if r.returncode == 0 else None


def _recorded_unit_copies(
    records: InstallRecordPort, home: str, component: Component, name: str, current: str
) -> tuple[str, ...]:
    """The files named name that the user's install record lists for component elsewhere than current;
    none when the record cannot be read, also when its path, the installer's for this home (record_file),
    is not plain (a HOME with a "." in it)."""
    try:
        rec = records.read(record_file(Owner.USER, home))
    except OSError, RecordFormatError, ValueError:
        return ()
    if rec is None:
        return ()
    dests = component_dests(rec, component)
    return tuple(d for d in dests if posixpath.basename(d) == name and d != current)


def older_unit_copies(
    records: InstallRecordPort, home: str, component: Component, name: str, current: str, answer: str | None
) -> tuple[str, ...]:
    """The recorded copies (_recorded_unit_copies) a write at current replaces. None when the user
    manager's UnitPath (answer) names no folder of the user's own units (named_user_unit_dir): current
    is then only systemd's default, and a recorded copy elsewhere may be the one the manager loads, so
    every recorded copy is kept (kept_unit_copies)."""
    if named_user_unit_dir(answer) is None:
        return ()
    return _recorded_unit_copies(records, home, component, name, current)


def kept_unit_copies(
    exists: Callable[[str], bool],
    realpath: Callable[[str], str],
    records: InstallRecordPort,
    home: str,
    component: Component,
    name: str,
    current: str,
    answer: str | None,
) -> tuple[str, ...]:
    """The recorded copies older_unit_copies keeps because the user manager's UnitPath (answer) names no
    folder: those on disk (exists) that are not current by another path (realpath). One of them may be
    the unit the manager runs. None when the manager names its folder."""
    if named_user_unit_dir(answer) is not None:
        return ()
    cur = realpath(current)
    copies = _recorded_unit_copies(records, home, component, name, current)
    return tuple(p for p in copies if exists(p) and realpath(p) != cur)
