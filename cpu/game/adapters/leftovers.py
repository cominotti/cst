# SPDX-License-Identifier: MPL-2.0
"""game.leftovers on the machine: the settings 'off' could not undo, kept for the next 'off' and the watchdog's
tick while the profile is off, and undone again by them (cpu.game.domain.leftovers is its format).

GameProfile.off undoes game.state's records and the kept ones through undo_entries, then writes the file
again with keep; retry_kept is the same for an 'off' with no game.state and for the tick with the profile off.
"""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import dataclasses
import os
from typing import TYPE_CHECKING

from cst_core.adapters.files import LocalFileSystemAdapter, write_atomic
from cst_core.domain.units import LoadState
from cpu.game.domain import Manager, Record, show_values, transient_unit
from cpu.game.domain.leftovers import (
    Entry,
    leftover_entries,
    leftovers_text,
    newer_leftovers,
    of_user_manager,
    undo_text,
)

if TYPE_CHECKING:
    from cpu.game.adapters.profile import GameProfile


@dataclasses.dataclass
class Reloads:
    """The managers an undo removed a runtime override in, each due a daemon-reload: the system manager
    (system), and each user's manager by the user's name (users, in the order they were first acted in)."""

    system: bool = False
    users: list[str] = dataclasses.field(default_factory=list)

    def add(self, mgr: Manager, user: str) -> None:
        """An override of that manager was removed (user: the user acted for, for the user manager)."""
        if mgr == "sys":
            self.system = True
        elif user not in self.users:
            self.users.append(user)


def read_kept(profile: GameProfile, *, say: bool) -> list[Entry] | None:
    """game.leftovers' entries ([] when it is not there); None for one this CST does not read (a newer
    format, or a first line that names no version) or cannot read (a folder in its place, EIO, a denied
    read), which is left alone (say: in one warning line). It is read byte for byte, as keep writes it
    back: a file there that cannot be read is never taken for a missing one."""
    path = profile.paths.leftovers
    try:
        text = LocalFileSystemAdapter().read_existing_text(path)
    except OSError as e:
        if say:
            profile.log(4, f"{path}: cannot be read ({e.strerror}): left alone, neither undone nor removed")
        return None
    if text is None:
        return []
    newer = newer_leftovers(path, text)
    if newer is None:
        return leftover_entries(text)
    if say:
        profile.log(4, f"{newer}: left alone, neither undone nor removed")
    return None


def undo_entries(profile: GameProfile, entries: Sequence[Entry], reload: Reloads, *, say: bool) -> list[Entry]:
    """Undo the entries, the last first (a property touched twice ends at its oldest value), each record of
    the user manager for the user its entry names; the ones whose undo failed, in their order, each with the
    uid it was for (keep says them). An entry whose uid no account has now is never undone in another
    user's manager: it is done when that uid's user manager is gone, else kept. say: see
    GameProfile.undo_record."""
    failed: list[Entry] = []
    for uid, rec in reversed(entries):
        if uid is not None and not profile.act_for(uid, say=False):
            if user_manager_gone(profile, uid):
                continue
            user: str | None = uid
        elif profile.undo_record(rec, reload, say=say):
            continue
        else:
            user = (profile.uid(say=say) or None) if of_user_manager(rec) else None
        failed.append((user, rec))
    failed.reverse()
    return failed


def keep(profile: GameProfile, failed: Sequence[Entry], kept: Sequence[Entry] | None, *, off: bool = True) -> int:
    """game.leftovers holds the failed entries afterwards (removed when there are none); how many of them it
    holds, each said kept in a warning line (off: 'off' says them; the tick does not). kept: the entries the
    file held before (None: a file this CST does not read, left as it is). When the file cannot be written (a
    full /run), the old one is left as it is while it holds no entry undone since (a tick that undid nothing,
    an 'off' whose own records failed too): its entries are still kept and retried. Else it goes: it would
    have an entry undone now undone and said again at every tick. Each failed entry the file does not hold is
    said lost instead, once (off: in 'off''s words, else the tick's)."""
    path = profile.paths.leftovers
    if kept is not None and not failed:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return 0
    held: set[Record] = set()
    if kept is not None:
        try:
            write_atomic(path, leftovers_text(failed), mode=0o644, durable=False, relabel=False)
        except OSError:
            still = {rec for _uid, rec in failed}
            if all(rec in still for _uid, rec in kept):
                held = {rec for _uid, rec in kept}
            else:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        else:
            held = {rec for _uid, rec in failed}
    who = "off: " if off else ""
    for _uid, rec in failed:
        what = undo_text(rec, profile.paths.epp)
        if rec not in held:
            profile.log(3, f"{who}could not keep {what} in {path}: not undone")
        elif off:
            profile.log(4, f"off: could not undo {what}: kept in {path} for the next off and tick")
    return sum(1 for _uid, rec in failed if rec in held)


def user_manager_gone(profile: GameProfile, uid: str) -> bool:
    """The user manager of that uid is gone with its runtime overrides: its runtime folder is (a logout)."""
    return not os.path.isdir(os.path.join(profile.paths.run_user, uid, "systemd"))


def unit_gone(profile: GameProfile, mgr: Manager, unit: str) -> bool:
    """The unit of a set-property that failed in 'off' is gone, with its runtime overrides, so nothing is
    left to undo: the user manager's runtime folder is gone (a logout), or a transient unit (its overrides
    live beside it) is not loaded. A fixed unit's override outlives the unit: it is never gone."""
    if mgr == "usr" and user_manager_gone(profile, profile.uid()):
        return True
    if not transient_unit(unit):
        return False
    res = profile.ask(mgr, "show", "-p", "LoadState", unit)
    return res.returncode == 0 and show_values(res.stdout).get("LoadState", LoadState.LOADED) != LoadState.LOADED


def retry_kept(profile: GameProfile, *, say: bool) -> None:
    """Undo game.leftovers' entries again ('off' with no game.state, say: each one that fails again is said;
    the tick with the profile off, quietly), each kept record of the user manager in the manager of the user
    it names: one notice line per entry undone now, then a daemon-reload of each manager an undo removed an
    override in (the system manager, and each of those users' managers), then the file holds the rest
    (removed when none is left). The profile acts for TARGET_USER again afterwards, so what the tick decides
    next (switching on) is TARGET_USER's."""
    kept = read_kept(profile, say=say)
    if kept is None or not kept:
        return
    settings = profile.s
    try:
        reload = Reloads()
        failed = undo_entries(profile, kept, reload, say=say)
        still = {rec for _uid, rec in failed}
        for _uid, rec in kept:
            if rec not in still:
                what = undo_text(rec, profile.paths.epp)
                profile.log(5, f"undid {what}, kept in {profile.paths.leftovers} by an earlier off")
        profile.reload_managers(reload)
        keep(profile, failed, kept, off=say)
    finally:
        profile.s = settings
