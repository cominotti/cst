# SPDX-License-Identifier: MPL-2.0
"""game.leftovers on the machine: the settings 'off' could not undo, kept for the next 'off' and the watchdog's
tick while the profile is off, and undone again by them (cpu.game.domain.leftovers is its format).

GameProfile.off undoes game.state's records and the kept ones through undo_entries, then writes the file
again with keep; retry_kept is the same for an 'off' with no game.state and for the tick with the profile off.
"""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import os
from typing import TYPE_CHECKING

from cst_core.adapters.files import read_text, write_atomic
from cst_core.domain.units import LoadState
from cpu.game.domain import Manager, show_values, transient_unit
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


def read_kept(profile: GameProfile, *, say: bool) -> list[Entry] | None:
    """game.leftovers' entries ([] when it is not there); None for one this CST does not read (a newer
    format, or a first line that names no version), which is left alone (say: in one warning line)."""
    path = profile.paths.leftovers
    text = read_text(path)
    if text is None:
        return []
    newer = newer_leftovers(path, text)
    if newer is None:
        return leftover_entries(text)
    if say:
        profile.log(4, f"{newer}: left alone, neither undone nor removed")
    return None


def undo_entries(profile: GameProfile, entries: Sequence[Entry], reload: dict[str, bool], *, say: bool) -> list[Entry]:
    """Undo the entries, the last first (a property touched twice ends at its oldest value), each record of
    the user manager for the user its entry names; the ones whose undo failed, in their order, each with the
    uid it was for (say: each named in a warning line)."""
    failed: list[Entry] = []
    for uid, rec in reversed(entries):
        if uid is not None:
            profile.act_for(uid, say=False)
        if profile.undo_record(rec, reload):
            continue
        user = (profile.uid(say=say) or None) if of_user_manager(rec) else None
        failed.append((user, rec))
        if say:
            what = undo_text(rec, profile.paths.epp)
            profile.log(4, f"off: could not undo {what}: kept in {profile.paths.leftovers} for the next off and tick")
    failed.reverse()
    return failed


def keep(profile: GameProfile, failed: Sequence[Entry], *, readable: bool) -> None:
    """game.leftovers holds the failed entries afterwards (removed when there are none); with a file this CST
    does not read (readable False) or one it cannot write (a full /run), each failed one is said lost."""
    path = profile.paths.leftovers
    if readable and not failed:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return
    if readable:
        try:
            write_atomic(path, leftovers_text(failed), mode=0o644, durable=False, relabel=False)
        except OSError:
            pass
        else:
            return
    for _uid, rec in failed:
        profile.log(3, f"off: could not keep {undo_text(rec, profile.paths.epp)} in {path}: not undone")


def unit_gone(profile: GameProfile, mgr: Manager, unit: str) -> bool:
    """The unit of a set-property that failed in 'off' is gone, with its runtime overrides, so nothing is
    left to undo: the user manager's runtime folder is gone (a logout), or a transient unit (its overrides
    live beside it) is not loaded. A fixed unit's override outlives the unit: it is never gone."""
    if mgr == "usr" and not os.path.isdir(os.path.join(profile.paths.run_user, profile.uid(), "systemd")):
        return True
    if not transient_unit(unit):
        return False
    res = profile.ask(mgr, "show", "-p", "LoadState", unit)
    return res.returncode == 0 and show_values(res.stdout).get("LoadState", LoadState.LOADED) != LoadState.LOADED


def retry_kept(profile: GameProfile, *, say: bool) -> None:
    """Undo game.leftovers' entries again ('off' with no game.state, say: each one that fails again is said;
    the tick with the profile off, quietly): one notice line per entry undone now, then the file holds the
    rest (removed when none is left). The profile acts for TARGET_USER again afterwards: each kept record of
    the user manager is undone for the user it names, and what the tick decides next (switching on) is
    TARGET_USER's."""
    kept = read_kept(profile, say=say)
    if kept is None or not kept:
        return
    settings = profile.s
    try:
        reload = {"sys": False, "usr": False}
        failed = undo_entries(profile, kept, reload, say=say)
        still = {rec for _uid, rec in failed}
        for _uid, rec in kept:
            if rec not in still:
                what = undo_text(rec, profile.paths.epp)
                profile.log(5, f"undid {what}, kept in {profile.paths.leftovers} by an earlier off")
        profile.reload_managers(reload)
        keep(profile, failed, readable=True)
    finally:
        profile.s = settings
