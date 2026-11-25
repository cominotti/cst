# SPDX-License-Identifier: MPL-2.0
"""btrfs-bulk's own files (root-owned; the user cannot fake them): the
settings (bulk.conf), the journals of interrupted moves, the boot flag, the
keep list, convert-plan and the run lock. (The boot counter is cst_core's
CounterFile, which wiring.py builds.)
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Sequence
import contextlib
import json
import os
import re
from typing import TypeIs

from btrfs.bulk.domain import (
    BY_ID,
    ITEMS,
    KEEP_HEADER,
    BadSettings,
    BulkJournalKind,
    BulkPaths,
    ItemJournal,
    KeepLineError,
)
from btrfs.bulk.domain.steam import SteamJournal, SteamPhase, dec
from cst_core.adapters.files import locale_bytes, read_text, write_atomic
from cst_core.adapters.locks import take_lock
from cst_core.domain.contracts import StateFormatKey, state_format
from cst_core.domain.shapes import KeyTypes, has_shape, json_object, parse_json
from cst_core.domain.text import (
    SettingsProblem,
    blank_edge_problem,
    describe_problems,
    parse_shell_assignments,
    strip_ascii_blanks,
)

# The journals of moves not finished yet (read back by any later btrfs-bulk).
JOURNAL = state_format(StateFormatKey.BULK_JOURNAL)


def bulk_user(conf: str) -> str | None:
    """BULK_USER of bulk.conf (setup-bulk.bash writes it), or None when unset or
    the file cannot be read. A line that is not a plain assignment stops the
    run, naming the line: root must never guess whose home it moves."""
    parsed = parse_shell_assignments(read_text(conf) or "", bash_sourced=False)
    if parsed.problems:
        raise BadSettings(describe_problems(conf, parsed.problems))
    return parsed.values.get("BULK_USER") or None


# The shape recovery may trust: each key it reads without .get, of its type, and the optional
# ones of their type when present (journals of older versions lack stamp2, conflict, undo).
# A journal that fails it is damaged: named, kept, and nothing it names is touched.
_ITEM_REQUIRED: KeyTypes = {
    "kind": (str,),
    "src": (str,),
    "target": (str,),
    "new": (str,),
    "old": (str,),
    "stamp": (int,),
    "link": (str, type(None)),
}
_ITEM_OPTIONAL: KeyTypes = {
    "stamp2": (int,),
    "bulk_only": (bool,),
    "conflict": (bool,),
    "undo": (bool,),
    "finishing": (bool,),
}
_PART_REQUIRED: KeyTypes = dict.fromkeys(("name", "kind", "src", "dst", "new", "old"), (str,))
_PART_OPTIONAL: KeyTypes = {"mode": (str,), "bytes": (int,), "sample": (str,)}
_STEAM_REQUIRED: KeyTypes = {
    **dict.fromkeys(("kind", "appid", "src_lib", "dst_lib", "size", "contentid", "phase"), (str,)),
    "stamp": (int,),
    "parts": (list,),
}
_STEAM_OPTIONAL: KeyTypes = {"stamp2": (int,), "conflict": (bool,), "undo": (bool,)}
STEAM_PHASES = tuple(SteamPhase)


def is_item_journal(obj: object) -> TypeIs[ItemJournal]:
    """A journal-<id>.json's JSON has the shape btrfs-bulk writes (kind copy or subvol)."""
    return (
        isinstance(obj, dict)
        and has_shape(obj, _ITEM_REQUIRED, _ITEM_OPTIONAL)
        and obj["kind"] in (BulkJournalKind.COPY, BulkJournalKind.SUBVOL)
    )


def is_steam_journal(obj: object) -> TypeIs[SteamJournal]:
    """A journal-steam-<appid>.json's JSON has the shape btrfs-bulk writes: kind steam, a known
    phase, every part whole, and stamp2 once the copies are made (any phase after prepared)."""
    if not json_object(obj) or not has_shape(obj, _STEAM_REQUIRED, _STEAM_OPTIONAL):
        return False
    appid, phase, parts = obj["appid"], obj["phase"], obj["parts"]
    return (
        obj["kind"] == BulkJournalKind.STEAM
        and dec(appid)
        and phase in STEAM_PHASES
        and (phase == SteamPhase.PREPARED or "stamp2" in obj)
        and isinstance(parts, list)
        and all(has_shape(p, _PART_REQUIRED, _PART_OPTIONAL) for p in parts)
    )


class StateFiles:
    """BulkStatePort over the files in paths."""

    def __init__(self, paths: BulkPaths) -> None:
        self.paths = paths

    # ---- journals ----
    def jpath(self, ident: str) -> str:
        """The journal file of an item or a Steam game ("steam-<appid>"); the id
        comes from ITEMS or a Steam manifest, so anything but a plain name
        (a `/` or `..` that would leave the state folder) is refused."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", ident) or ".." in ident:
            raise ValueError(f"{ident!r}: not a journal id")
        return os.path.join(self.paths.state, f"journal-{ident}.json")

    def jexists(self, ident: str) -> bool:
        return os.path.exists(self.jpath(ident))

    def jwrite(self, ident: str, j: ItemJournal | SteamJournal) -> None:
        os.makedirs(self.paths.state, mode=0o755, exist_ok=True)
        # durable: the rename itself is on disk before anything it protects is done.
        # "v": its format (JOURNAL). A btrfs-bulk from this format on leaves a journal of a newer one alone;
        # one from before the field reads any journal as its own (format 0).
        write_atomic(self.jpath(ident), json.dumps({**j, "v": JOURNAL.version}), mode="umask", relabel=False)

    def _jload(self, ident: str) -> object:
        """The journal's JSON; None when it is missing or not JSON."""
        try:
            with open(self.jpath(ident)) as f:
                return parse_json(f.read())
        except OSError, ValueError:
            return None

    def jread_item(self, ident: str) -> ItemJournal | None:
        """The item's journal; None when missing, not JSON, not the shape btrfs-bulk writes, or of a
        format this btrfs-bulk does not read ("v": newer_journal says when a newer one wrote it)."""
        j = self._jload(ident)
        return j if is_item_journal(j) and JOURNAL.readable(j, "v") else None

    def jread_steam(self, ident: str) -> SteamJournal | None:
        """The Steam game's journal; None when missing, not JSON, not the shape btrfs-bulk writes,
        or of another game than its file's (recovery would act on that game and drop its journal)."""
        j = self._jload(ident)
        return j if is_steam_journal(j) and JOURNAL.readable(j, "v") and ident == "steam-" + j["appid"] else None

    def newer_journal(self, ident: str) -> str | None:
        """Why the journal of ident is not used when a newer btrfs-bulk wrote it; None otherwise."""
        path = self.jpath(ident)
        found = JOURNAL.newer(self._jload(ident), "v")
        if found is None:
            return None
        return (
            f"{JOURNAL.newer_text(path, found)}; left as it is: the btrfs-bulk of that CST finishes or "
            "undoes the move (cst btrfs snapshots setup in that checkout installs it)"
        )

    def jdrop(self, ident: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.jpath(ident))

    def journals(self) -> list[str]:
        """Ids with a journal (a move not finished or undone yet); a Steam game's
        is "steam-<appid>"."""
        return [i.id for i in ITEMS if os.path.exists(self.jpath(i.id))] + self.steam_journal_ids()

    def steam_journal_ids(self) -> list[str]:
        try:
            names = os.listdir(self.paths.state)
        except OSError:
            return []
        ids = [m.group(1) for m in (re.fullmatch(r"journal-(steam-\d+)\.json", n, re.ASCII) for n in names) if m]
        return sorted(ids, key=lambda i: int(i[6:]))

    # ---- the boot flag ----
    def read_flag(self) -> list[str]:
        try:
            with open(self.paths.flag) as f:
                return [x.strip() for x in f if x.strip()]
        except OSError:
            return []

    def flag_exists(self) -> bool:
        return os.path.exists(self.paths.flag)

    def flag_add(self, ident: str) -> None:
        ids = self.read_flag()
        if ident not in ids:
            self.write_flag([*ids, ident])

    def write_flag(self, ids: Sequence[str]) -> None:
        if ids:
            os.makedirs(self.paths.etc, mode=0o755, exist_ok=True)
            # Durable (file and folder): the next boot acts on it.
            write_atomic(self.paths.flag, "".join(i + "\n" for i in ids), mode="umask", relabel=False)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.paths.flag)

    # ---- the keep list ----
    def read_keep(self) -> tuple[set[str], list[str]]:
        """(ids kept in /home by choice, unknown ids) from the keep file: one id
        per line, '#' starts a comment. Bytes that are not UTF-8 make an unknown
        id. Raises OSError if it exists but cannot be read (a folder, say), and
        KeepLineError (an OSError) for an id with a blank other than space or tab
        beside it (a no-break space): then nobody knows what is meant to stay,
        and apply moves nothing. ASCII blanks around an id are trimmed."""
        known: set[str] = set()
        unknown: list[str] = []
        try:
            with open(self.paths.keep, encoding="utf-8", errors="replace") as f:
                for number, line in enumerate(f, 1):
                    ident = strip_ascii_blanks(line.split("#", 1)[0])
                    if (why := blank_edge_problem(ident)) is not None:
                        raise KeepLineError(SettingsProblem(number, line, why))
                    if ident in BY_ID:
                        known.add(ident)
                    elif ident:
                        unknown.append(ident)
        except FileNotFoundError:
            pass
        return known, unknown

    def edit_keep(self, add: Sequence[str], remove: Collection[str]) -> None:
        """Add/remove ids in the keep file; other lines (comments) stay as they are."""
        keep = self.paths.keep
        # surrogateescape: lines this tool cannot read are written back unchanged.
        try:
            with open(keep, encoding="utf-8", errors="surrogateescape") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = [KEEP_HEADER]
        lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
        lines = [ln for ln in lines if ln.split("#", 1)[0].strip() not in remove]
        have = {ln.split("#", 1)[0].strip() for ln in lines}
        lines += [i + "\n" for i in add if i not in have]
        os.makedirs(self.paths.etc, mode=0o755, exist_ok=True)
        # UTF-8, surrogateescape; durable (file and folder): apply and the boot step act on it.
        write_atomic(keep, "".join(lines), mode=0o644, relabel=False)

    # ---- convert-plan ----
    def prune_convert_plan(self, drop: Collection[str]) -> bool:
        plan = self.paths.convert_plan
        try:
            with open(plan) as f:
                lines = f.readlines()
        except OSError:
            return False
        keep = [
            ln for ln in lines if ln.startswith("#") or not ln.strip() or ln.split(maxsplit=1)[-1].strip() not in drop
        ]
        if keep == lines:
            return False
        # Encoded as it was read: in the locale's encoding (open()'s default).
        # Durable (file and folder): the boot conversion acts on it.
        write_atomic(plan, locale_bytes("".join(keep)), mode=0o644, relabel=False)
        return True

    # ---- the run lock ----
    def run_lock(self) -> int:
        """Wait for the run lock (shared with btrfs-snapshot); its fd (release() closes it)."""
        fd = take_lock(self.paths.lock, wait=True, mode=0o644, nofollow=False)
        if fd is None:  # only a non-blocking take returns None
            raise RuntimeError(f"{self.paths.lock}: not locked")
        return fd

    def release(self, lock: int) -> None:
        os.close(lock)
