# SPDX-License-Identifier: MPL-2.0
"""The watchdog's check of a box it handled already (a Docker or Steam scope, a box next to the game): each
recorded setting systemd no longer shows as the profile sets it is set again, with no new record.

GameProfile's protect_steam, pin_docker and demote_escapes call set_again for a box game.state holds a
record of; cpu.game.domain.boxes judges the values and words the journal line.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from cst_core.adapters.files import read_text
from cst_core.domain.units import LoadState
from cpu.game.domain import Manager, intended_records, newer_intended, show_values, transient_unit
from cpu.game.domain.boxes import intended_covers, set_again_text, unset_settings

if TYPE_CHECKING:
    from cpu.game.adapters.profile import GameProfile


def set_again(
    profile: GameProfile, mgr: Manager, unit: str, settings: Sequence[str], folder: str | None = None
) -> None:
    """A box handled already (game.state holds a record of each of settings, "PROP=VALUE"): each setting
    systemd no longer shows as the profile sets it (its set-property failed, or an 'off' that was killed
    undid it) is set again, in one set-property and with no new record (the first one says how 'off' undoes
    it), and said in the journal. Left alone: a setting the drift repair compares (intended_covers:
    repair_drift sets it), every setting of a fixed unit when game.intended is of a newer format (the drift
    repair leaves it too), a unit systemd has not loaded, a box that holds the game by now (folder, looked
    at only when there is something to set) and a value it cannot judge (setting_holds). With every setting
    holding it writes nothing."""
    path = profile.paths.intended
    text = read_text(path) or ""
    newer = newer_intended(path, text)
    if newer is not None and not transient_unit(unit):
        return
    covered = intended_covers(intended_records(text), mgr, unit) if newer is None else frozenset()
    todo = [kv for kv in settings if kv.split("=", 1)[0] not in covered]
    if not todo:
        return
    args = ["show", "-p", "LoadState"] + [a for kv in todo for a in ("-p", kv.split("=", 1)[0])]
    res = profile.ask(mgr, *args, unit)
    vals = show_values(res.stdout) if res.returncode == 0 else {}
    if vals.get("LoadState") != LoadState.LOADED:
        return
    unset = unset_settings(todo, vals)
    if not unset or (folder is not None and profile.gamemode_in(folder)):
        return
    rc = profile.ctl(mgr, "set-property", "--runtime", unit, *(f"{u.prop}={u.value}" for u in unset), quiet=True)
    profile.log(*set_again_text(unit, unset, ok=rc == 0))
