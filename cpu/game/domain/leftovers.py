# SPDX-License-Identifier: MPL-2.0
"""game.leftovers: the records 'off' could not undo, kept for the next 'off' and the watchdog's tick while the
profile is off (cpu/game/adapters/leftovers.py reads and writes it). No I/O.

Its first line names its format, "cst-game-leftovers 1". Then the records whose undo failed, oldest first,
written as game.state's are (record_line); a record of the user manager comes after a line "user UID" that
names the user it is for (the one 'off' acted for), so a later 'off' or tick undoes it for that user even
with no game.state and another TARGET_USER.
"""

from collections.abc import Callable, Sequence
from typing import Final

from cst_core.domain.contracts import StateFormatKey, first_line_version, state_format
from cpu.game.domain import (
    CgroupUndo,
    EppUndo,
    GameRecordKind,
    PropUndo,
    Record,
    SysfsUndo,
    record_line,
    records,
    state_undo,
)

LEFTOVERS_MAGIC: Final = "cst-game-leftovers"
LEFTOVERS_FORMAT: Final = state_format(StateFormatKey.GAME_LEFTOVERS)

# One kept record: the uid of the user its user manager's record is for (None: a record of the system
# manager or a file, or the user 'off' acts for now), and the record.
type Entry = tuple[str | None, Record]


def leftovers_text(entries: Sequence[Entry]) -> str:
    """game.leftovers' text for the entries, in their order (oldest first)."""
    out = [record_line(LEFTOVERS_MAGIC, str(LEFTOVERS_FORMAT.version))]
    user: str | None = None
    for uid, rec in entries:
        if uid is not None and uid != user:
            out.append(record_line(GameRecordKind.USER, uid))
            user = uid
        out.append(record_line(*rec))
    return "".join(out)


def leftover_entries(text: str) -> list[Entry]:
    """The entries of game.leftovers' text, in file order (read as game.state is: records), each record of
    the user manager with the uid of the "user" line before it."""
    user: str | None = None
    out: list[Entry] = []
    for rec in records(text):
        if rec[0] == LEFTOVERS_MAGIC:
            continue
        if rec[0] == GameRecordKind.USER:
            user = rec[1] if len(rec) > 1 else None
            continue
        undo = state_undo(rec)
        out.append((user if isinstance(undo, PropUndo) and undo.mgr == "usr" else None, rec))
    return out


def newer_leftovers(path: str, text: str) -> str | None:
    """Why game.leftovers is not read (a format newer than this version reads, or a first line that names
    no version), None when it can be."""
    found = first_line_version(text, LEFTOVERS_MAGIC)
    if found is None:
        return f"{path}: its first line names no format version"
    return LEFTOVERS_FORMAT.newer_text(path, found) if found > LEFTOVERS_FORMAT.version else None


def merged_entries(kept: Sequence[Entry], state_recs: Sequence[Record]) -> list[Entry]:
    """What 'off' undoes, in file order (it undoes the last first): the kept entries (older than any record of
    this game.state), then game.state's records. A kept record that is a record of this game.state too is
    dropped: an 'off' killed after it wrote game.leftovers and before it removed game.state left both, and
    undoing it once keeps a property touched twice ending at its oldest value."""
    mine = set(state_recs)
    return [e for e in kept if e[1] not in mine] + [(None, r) for r in state_recs]


def undo_text(rec: Record, epp: Callable[[str], str], *, short: bool = False) -> str:
    """What undoing a record sets, for the journal: "UNIT PROP=OLD", "UNIT PROP (its runtime override)",
    "FILE = OLD"; short: "UNIT PROP" or FILE (epp: a CPU's EPP file)."""
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


def off_text(kept: int, path: str) -> str:
    """The journal line at the end of 'off' (kept: the settings it could not undo, kept in path)."""
    done = "off: previous CPU, weight, memory, zswap writeback and EPP settings restored"
    return f"{done}, except {kept} setting(s) kept in {path}" if kept else done


def leftovers_line(entries: Sequence[Entry], epp: Callable[[str], str]) -> str:
    """The line of 'state' for the kept entries."""
    what = ", ".join(undo_text(rec, epp, short=True) for _uid, rec in entries)
    return f"  leftovers: {len(entries)} setting(s) 'off' could not undo, retried by the next off and tick: {what}"
