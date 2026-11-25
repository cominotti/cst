# SPDX-License-Identifier: MPL-2.0
"""game.leftovers: the records 'off' could not undo, kept for the next 'off' and the watchdog's tick while the
profile is off (cpu/game/adapters/files.py reads and writes it). No I/O.

Its first line names its format, "cst-game-leftovers 2". Then its entries, oldest first: the records whose
undo failed, written as game.state's are (record_line), and "reload MGR" when a daemon-reload of that
manager is due (the system manager, sys, or the user manager, usr, of the uid of its user line): 'off'
writes it before it removes a drop-in or a runtime override, and it goes once that reload worked, so a
reload that failed or was cut short by a kill is run by the next 'off' or tick. Every entry comes after a
line "user UID" that names the user its user manager's record or reload is for (the one 'off' acted for),
or "user -" for none, so a later 'off' or tick undoes it for that user even with no game.state and another
TARGET_USER. Format 1 (no reload entries, a user line only when the uid changed to another one) is read
with the same rules ("user -" is none); every write is format 2.
"""

from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Final

from cst_core.domain.contracts import StateFormatKey, first_line_version, state_format
from cpu.game.domain import (
    GAME_DROPIN,
    CgroupUndo,
    EppUndo,
    GameRecordKind,
    Manager,
    PropUndo,
    Record,
    SysfsUndo,
    manager_of,
    record_line,
    records,
    state_undo,
)

LEFTOVERS_MAGIC: Final = "cst-game-leftovers"
LEFTOVERS_FORMAT: Final = state_format(StateFormatKey.GAME_LEFTOVERS)

# The uid of a user line that names no user.
NO_USER: Final = "-"
# The fields of a reload-due entry: "reload MGR".
RELOAD_FIELDS: Final = 2
# The journal line of an 'off' or a tick that removed drop-ins left behind with the profile off.
LEFTOVER_DROPINS_TEXT: Final = f"profile off, but its MemoryLow drop-ins ({GAME_DROPIN}) were still there: removed them"

# One kept entry: the uid of the user its user manager's record or reload is for (None: a record of the
# system manager or a file, or the user 'off' acts for now), and the record ("reload MGR" for a reload due).
type Entry = tuple[str | None, Record]


def reload_entry(mgr: Manager, uid: str | None) -> Entry:
    """The entry of a daemon-reload due in the system manager (sys) or in the user manager of uid (usr)."""
    return (uid if mgr == "usr" else None, (GameRecordKind.RELOAD.value, mgr))


def reload_of(rec: Record) -> Manager | None:
    """The manager a reload-due record names (reload_entry), None for any other record."""
    return manager_of(rec[1]) if len(rec) >= RELOAD_FIELDS and rec[0] == GameRecordKind.RELOAD else None


def leftovers_text(entries: Sequence[Entry]) -> str:
    """game.leftovers' text for the entries, in their order (oldest first): a user line before the first
    entry and before each entry whose user differs from the one before it."""
    out = [record_line(LEFTOVERS_MAGIC, str(LEFTOVERS_FORMAT.version))]
    for n, (uid, rec) in enumerate(entries):
        if n == 0 or uid != entries[n - 1][0]:
            out.append(record_line(GameRecordKind.USER, NO_USER if uid is None else uid))
        out.append(record_line(*rec))
    return "".join(out)


def leftover_entries(text: str) -> list[Entry]:
    """The entries of game.leftovers' text (format 1 or 2), in file order (read as game.state is: records),
    each record of the user manager and each reload of a user manager with the uid of the "user" line before
    it; a user manager's reload with no user ("user -", or none) is no entry: no manager to reload."""
    user: str | None = None
    out: list[Entry] = []
    for rec in records(text):
        if rec[0] == LEFTOVERS_MAGIC:
            continue
        if rec[0] == GameRecordKind.USER:
            user = rec[1] if len(rec) > 1 and rec[1] != NO_USER else None
            continue
        mgr = reload_of(rec)
        if mgr == "usr" and user is None:
            continue
        if mgr is not None:
            out.append(reload_entry(mgr, user))
            continue
        out.append((user if of_user_manager(rec) else None, rec))
    return out


def of_user_manager(rec: Record) -> bool:
    """Whether undoing the record sets a property in the user manager: its entry names the user it is for."""
    undo = state_undo(rec)
    return isinstance(undo, PropUndo) and undo.mgr == "usr"


def newer_leftovers(path: str, text: str) -> str | None:
    """Why game.leftovers is not read (a format newer than this version reads, or a first line that names
    no version), None when it can be."""
    found = first_line_version(text, LEFTOVERS_MAGIC)
    if found is None:
        return f"{path}: its first line names no format version"
    return LEFTOVERS_FORMAT.newer_text(path, found) if found > LEFTOVERS_FORMAT.version else None


def entry_key(entry: Entry, acting: str | None) -> Entry:
    """What tells two entries apart: the record with the uid it is for, an entry of the user manager that names
    no uid being for the user acted for (acting: that user's uid, None for none). Two users' entries of one
    record are two entries."""
    uid, rec = entry
    return (acting, rec) if uid is None and of_user_manager(rec) else entry


def merged_entries(
    kept: Sequence[Entry], state_recs: Sequence[Record], *, owner: str | None = None, acting: str | None = None
) -> list[Entry]:
    """What 'off' undoes, in file order (it undoes the last first): the kept entries (older than any record of
    this game.state, its reload-due entries left out), then game.state's records, those of the user manager
    for the uid owner (None: the user 'off' acts for, whose uid is acting). A kept entry that is an entry of
    this game.state too, for the same uid (entry_key), is dropped: an 'off' killed after it wrote
    game.leftovers and before it removed game.state left both, and undoing it once keeps a property touched
    twice ending at its oldest value. Another uid's entry of the same record is kept."""
    mine = [(owner if of_user_manager(r) else None, r) for r in state_recs]
    keys = {entry_key(e, acting) for e in mine}
    old = [e for e in kept if entry_key(e, acting) not in keys and reload_of(e[1]) is None]
    return old + mine


class UserSide(StrEnum):
    """Where 'off' undoes game.state's records of the user manager, for the uid game.state records (the user
    'on' acted for: state_uid)."""

    OWN = "own"  # in that uid's manager, which runs (or TARGET_USER's, when game.state records no uid)
    GONE = "gone"  # that manager is not running: what it held went with it
    ORPHAN = "orphan"  # no account has the uid now and its manager still runs: kept, never undone in another's


def user_side_text(side: UserSide, uid: str, count: int) -> tuple[int, str] | None:
    """The journal line (priority, text) of an 'off' whose game.state holds count records of the user manager
    of uid that it does not undo there (side GONE or ORPHAN); None for none."""
    if not count:
        return None
    match side:
        case UserSide.GONE:
            return 4, (
                f"off: the user manager of uid {uid}, the user 'on' acted for, is not running: {count} setting(s)"
                " of it went with it, dropped"
            )
        case UserSide.ORPHAN:
            return 5, (
                f"off: no account has uid {uid}, the user 'on' acted for, now, and its user manager still runs:"
                f" its {count} setting(s) are not undone in another user's manager"
            )
        case UserSide.OWN:
            return None


def undo_text(rec: Record, epp: Callable[[str], str], *, short: bool = False, uid: str | None = None) -> str:
    """What undoing a record sets, for the journal: "UNIT PROP=OLD", "UNIT PROP (its runtime override)",
    "FILE = OLD"; short: "UNIT PROP" or FILE (epp: a CPU's EPP file). A reload due: "the daemon-reload of the
    system manager" or "... of uid UID's user manager" (uid: its entry's), short without "the"."""
    mgr = reload_of(rec)
    if mgr is not None:
        what = "daemon-reload of " + ("the system manager" if mgr == "sys" else f"uid {uid}'s user manager")
        return what if short else f"the {what}"
    match state_undo(rec):
        case PropUndo(unit=unit, prop=prop, mode=mode, old=old):
            if short:
                return f"{unit} {prop}"
            return f"{unit} {prop} (its runtime override)" if mode == "new" else f"{unit} {prop}={old}"
        case CgroupUndo(file=f, old=old) | SysfsUndo(file=f, old=old):
            return f if short else f"{f} = {old}"
        case EppUndo(cpu=cpu, old=old):
            return epp(cpu) if short else f"{epp(cpu)} = {old}"
        case None:
            return " ".join(rec)


def kept_count_text(entries: Sequence[Entry]) -> str:
    """How many entries there are, settings and daemon-reloads apart (a reload due is no setting): "2 setting(s)",
    "1 daemon-reload(s)", "2 setting(s) and 1 daemon-reload(s)"."""
    reloads = sum(1 for _uid, rec in entries if reload_of(rec) is not None)
    settings = len(entries) - reloads
    parts = [f"{settings} setting(s)"] if settings or not reloads else []
    if reloads:
        parts.append(f"{reloads} daemon-reload(s)")
    return " and ".join(parts)


def _has_reload(entries: Sequence[Entry]) -> bool:
    return any(reload_of(rec) is not None for _uid, rec in entries)


def off_text(failed: Sequence[Entry], path: str, *, kept: int) -> str:
    """The journal line at the end of 'off' (failed: the entries it could not undo or run, settings and
    daemon-reloads counted apart: kept_count_text; kept: how many of them path holds now, fewer when it could
    not be written or is of a format this CST does not read). What is neither kept nor done is "not undone",
    or "not done" when a daemon-reload is among the entries."""
    done = "off: previous CPU, weight, memory, zswap writeback and EPP settings restored"
    if not failed:
        return done
    what = kept_count_text(failed)
    lost = "not done" if _has_reload(failed) else "not undone"
    if kept == len(failed):
        return f"{done}, except {what} kept in {path}"
    if not kept:
        return f"{done}, except {what} {lost}"
    return f"{done}, except {what}: {kept} kept in {path}, {len(failed) - kept} {lost}"


def leftovers_words(entries: Sequence[Entry]) -> str:
    """The kept entries' count and what 'off' did not do with them: "2 setting(s) 'off' could not undo", or with
    a daemon-reload among them "2 setting(s) and 1 daemon-reload(s) 'off' could not finish" ('state', doctor)."""
    verb = "could not finish" if _has_reload(entries) else "could not undo"
    return f"{kept_count_text(entries)} 'off' {verb}"


def leftovers_line(entries: Sequence[Entry], epp: Callable[[str], str]) -> str:
    """The line of 'state' for the kept entries."""
    what = ", ".join(undo_text(rec, epp, short=True, uid=uid) for uid, rec in entries)
    return f"  leftovers: {leftovers_words(entries)}, retried by the next off and tick: {what}"
