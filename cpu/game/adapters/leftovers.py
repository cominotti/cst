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
from cpu.game.domain import Manager, PropUndo, Record, show_values, state_undo, transient_unit
from cpu.game.domain.leftovers import (
    LEFTOVER_DROPINS_TEXT,
    Entry,
    UserSide,
    entry_key,
    leftover_entries,
    leftovers_text,
    newer_leftovers,
    of_user_manager,
    reload_entry,
    reload_of,
    undo_text,
)

if TYPE_CHECKING:
    from cpu.game.adapters.profile import GameProfile


@dataclasses.dataclass
class Reloads:
    """The managers an undo removed a runtime override in, each due a daemon-reload: the system manager
    (system), and each user's manager by its uid, with the name of the user acted for there (users, in the
    order they were first acted in)."""

    system: bool = False
    users: dict[str, str] = dataclasses.field(default_factory=dict)

    def add(self, mgr: Manager, uid: str, user: str) -> None:
        """An override of that manager was removed (uid and user: the user acted for, for the user manager)."""
        if mgr == "sys":
            self.system = True
        else:
            self.users.setdefault(uid, user)


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


def undo_entries(
    profile: GameProfile, entries: Sequence[Entry], reload: Reloads, *, say: bool
) -> tuple[list[Entry], set[Record]]:
    """Undo the entries, the last first (a property touched twice ends at its oldest value), each record of
    the user manager for the user its entry names; the ones whose undo failed, in their order, each with the
    uid it was for (keep says them), and the records dropped undone (no_user_manager). An entry whose uid no
    account has now is never undone in another user's manager: it is done when that uid's user manager is
    gone, else kept. An entry of a uid whose user manager is not running is done: what it held went with it.
    say: see GameProfile.undo_record ('off' says it; the tick does not). Each entry starts from the user acted
    for when the pass began (an entry of no user's is that user's, whatever entry of a uid came before it), and
    the profile acts for that user again afterwards."""
    failed: list[Entry] = []
    dropped: set[Record] = set()
    settings = profile.s
    try:
        for uid, rec in reversed(entries):
            profile.s = settings
            if uid is not None and user_manager_gone(profile, uid):
                continue
            if uid is not None and not profile.act_for(uid, say=False):
                user: str | None = uid
            elif uid is None and no_user_manager(profile, rec, say=say):
                who = "off: " if say else ""
                what = undo_text(rec, profile.paths.epp)
                profile.log(
                    4,
                    f"{who}could not undo {what}: TARGET_USER names no account, no user manager to undo it in: dropped",
                )
                dropped.add(rec)
                continue
            elif profile.undo_record(rec, reload, say=say):
                continue
            else:
                user = (uid or profile.uid(say=say) or None) if of_user_manager(rec) else None
            failed.append((user, rec))
    finally:
        profile.s = settings
    failed.reverse()
    return failed, dropped


def no_user_manager(profile: GameProfile, rec: Record, *, say: bool) -> bool:
    """The record is a new runtime override of the user manager while TARGET_USER names no account (the entry
    names no user): no user's runtime folder holds it and no user manager can be reloaded, so it is dropped,
    never undone (say: see GameProfile.undo_record)."""
    return _new_override(rec, "usr") and not profile.uid(say=say)


def _new_override(rec: Record, mgr: Manager) -> bool:
    """The record's undo removes a runtime override of that manager (mode new): a daemon-reload is due after."""
    undo = state_undo(rec)
    return isinstance(undo, PropUndo) and undo.mgr == mgr and undo.mode == "new"


def user_side(profile: GameProfile, uid: str | None, *, say: bool) -> UserSide:
    """Where game.state's records of the user manager are undone, for the uid it records (None: none, they are
    TARGET_USER's, as before): in that uid's manager when it runs and an account has the uid (the profile acts
    for that user from now on: act_for, say its line); GONE when that manager is not running (no runtime folder:
    its runtime overrides went with it); ORPHAN when it runs but no account has the uid now (no name to reach
    it by: never undone in TARGET_USER's manager instead)."""
    if uid is None:
        return UserSide.OWN
    if user_manager_gone(profile, uid):
        return UserSide.GONE
    return UserSide.OWN if profile.act_for(uid, say=say) else UserSide.ORPHAN


def _write_leftovers(profile: GameProfile, entries: Sequence[Entry]) -> None:
    """game.leftovers holds the entries (an OSError when it cannot be written: the caller decides)."""
    write_atomic(profile.paths.leftovers, leftovers_text(entries), mode=0o644, durable=False, relabel=False)


def keep(profile: GameProfile, failed: Sequence[Entry], kept: Sequence[Entry] | None, *, off: bool = True) -> int:
    """game.leftovers holds the failed entries afterwards (removed when there are none); how many of them it
    holds, each said kept in a warning line, after the write worked (off: 'off' says them; the tick does not).
    kept: the entries the file held before (None: a file this CST does not read, left as it is). When the file
    cannot be written (a full /run), the old one is left as it is while it holds no entry done since (a tick
    that undid nothing, an 'off' whose own records failed too): its entries are still kept and retried. Else
    it goes: it would have an entry done now done and said again at every tick. Each failed entry the file
    does not hold is said lost instead, once (off: in 'off''s words, else the tick's)."""
    path = profile.paths.leftovers
    if kept is not None and not failed:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return 0
    acting = profile.uid() or None  # what an entry of no user is for (entry_key)
    held: set[Entry] = set()
    if kept is not None:
        try:
            _write_leftovers(profile, failed)
        except OSError:
            still = {entry_key(e, acting) for e in failed}
            if all(entry_key(e, acting) in still for e in kept):
                held = {entry_key(e, acting) for e in kept}
            else:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        else:
            held = {entry_key(e, acting) for e in failed}
    who = "off: " if off else ""
    for uid, rec in failed:
        what = undo_text(rec, profile.paths.epp, uid=uid)
        due = reload_of(rec) is not None
        if entry_key((uid, rec), acting) not in held:
            profile.log(3, f"{who}could not keep {what} in {path}: " + ("not done" if due else "not undone"))
        elif off and due:
            profile.log(4, f"off: {what} failed: kept in {path} for the next off and tick")
        elif off:
            profile.log(4, f"off: could not undo {what}: kept in {path} for the next off and tick")
    return sum(1 for e in failed if entry_key(e, acting) in held)


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


def _name_for(profile: GameProfile, uid: str) -> str:
    """The name of the user with that uid that a user manager's call goes to ("" when no account has it),
    leaving the user the profile acts for as it was."""
    settings = profile.s
    try:
        return profile.s.target_user if profile.act_for(uid, say=False) else ""
    finally:
        profile.s = settings


def _premark(profile: GameProfile, kept: Sequence[Entry], todo: Sequence[Entry], *, dropins: bool, say: bool) -> None:
    """Before an undo pass removes a drop-in or a runtime override: game.leftovers holds the kept entries and a
    reload due in each manager it may remove a file in (the system manager for the drop-ins and its new
    overrides, the user manager of each uid with a new override to undo, when that manager runs), so a pass
    killed after a removal leaves the reload for the next 'off' or tick. Best effort and quiet: a failed write
    (a full /run) is said by the closing keep, if anything is still due then."""
    own = profile.uid(say=say) or None  # TARGET_USER's (or the user acted for): looked up as the undos would
    marks: list[Entry] = []
    if dropins or any(_new_override(rec, "sys") for _uid, rec in todo):
        marks.append(reload_entry("sys", None))
    for uid, rec in todo:
        user = own if uid is None else uid
        mark = reload_entry("usr", user)
        if (
            user is not None
            and _new_override(rec, "usr")
            and mark not in marks
            and not user_manager_gone(profile, user)
            and (uid is None or _name_for(profile, uid))
        ):
            marks.append(mark)
    marks = [m for m in marks if m not in kept]
    if not marks:
        return
    with contextlib.suppress(OSError):
        _write_leftovers(profile, [*kept, *marks])


def _run_reloads(profile: GameProfile, reload: Reloads, due: Sequence[Entry]) -> tuple[list[Entry], set[Entry]]:
    """daemon-reload each manager an undo removed a file in (reload) and each one a kept entry says is due
    (due): the system manager, then each user's; the reloads that failed, as reload-due entries, and the
    reload-due entries of the user managers that are not running (they need none: their entries go). One
    of a uid no account has now is not reached by name: kept while it runs."""
    failed: list[Entry] = []
    skipped: set[Entry] = set()
    system = reload.system or any(reload_of(rec) == "sys" for _uid, rec in due)
    if system and profile.ctl("sys", "daemon-reload") != 0:
        failed.append(reload_entry("sys", None))
    users = dict(reload.users)
    for uid, rec in due:
        if uid is not None and reload_of(rec) == "usr":
            users.setdefault(uid, "")
    for uid, name in users.items():
        entry = reload_entry("usr", uid)
        if user_manager_gone(profile, uid):
            skipped.add(entry)
            continue
        user = name or _name_for(profile, uid)
        if not user or profile.ctl("usr", "daemon-reload", user=user) != 0:
            failed.append(entry)
    return failed, skipped


def undo_pass(
    profile: GameProfile, entries: Sequence[Entry], kept: Sequence[Entry] | None, *, say: bool
) -> tuple[list[Entry], set[Record], set[Entry]]:
    """One pass of 'off' and of the retry: the reloads due go to game.leftovers first (_premark, when it can
    be read: kept not None), then the drop-ins are removed, the entries undone (undo_entries, which skips
    reload-due ones), and each manager due a reload reloaded (_run_reloads, also each reload kept entries
    name). The entries that failed (records and reloads), the records dropped undone, and the reloads kept
    entries name that were not run (their manager is gone)."""
    todo = [e for e in entries if reload_of(e[1]) is None]
    due = [e for e in kept or [] if reload_of(e[1]) is not None]
    dropins = profile.game_dropins()
    if kept is not None:
        _premark(profile, kept, todo, dropins=bool(dropins), say=say)
    reload = Reloads(system=profile.remove_game_dropins())
    failed, dropped = undo_entries(profile, todo, reload, say=say)
    not_run, skipped = _run_reloads(profile, reload, due)
    return failed + not_run, dropped, skipped


def retry_kept(profile: GameProfile, *, say: bool) -> None:
    """With the profile off ('off' with no game.state, say: each one that fails again is said; the tick, quietly):
    leftover drop-ins go, and game.leftovers' entries are undone again, each kept record of the user manager in
    the manager of the user it names: one notice line per entry done now, then the file holds the rest (removed
    when none is left), each manager due a daemon-reload reloaded (undo_pass). A file this CST does not read
    is left alone; drop-ins still go then (remove_leftover_dropins). The profile acts for TARGET_USER again
    afterwards, so what the tick decides next (switching on) is TARGET_USER's."""
    kept = read_kept(profile, say=say)
    if kept is None:
        profile.remove_leftover_dropins()
        return
    dropins = bool(profile.game_dropins())
    if not kept and not dropins:
        return
    settings = profile.s
    try:
        failed, dropped, skipped = undo_pass(profile, kept, kept, say=say)
        if dropins:
            profile.log(4, LEFTOVER_DROPINS_TEXT)
        acting = profile.uid() or None  # what an entry of no user is for (entry_key)
        still = {entry_key(e, acting) for e in [*failed, *((None, r) for r in dropped)]} | skipped
        for uid, rec in kept:
            if entry_key((uid, rec), acting) in still:
                continue
            what = undo_text(rec, profile.paths.epp, uid=uid)
            done = "ran" if reload_of(rec) is not None else "undid"
            profile.log(5, f"{done} {what}, kept in {profile.paths.leftovers} by an earlier off")
        keep(profile, failed, kept, off=say)
    finally:
        profile.s = settings
