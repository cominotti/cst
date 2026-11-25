# SPDX-License-Identifier: MPL-2.0
"""The watchdog's drift repair: each setting of game.intended that systemd no longer shows as 'on' set it is
set again (GameProfile.drift lists them; cpu.game.domain words the journal's lines).

GameProfile.tick calls repair_drift while the profile is on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cpu.game.adapters.host import write_sysfs_line
from cpu.game.domain import manager_of, records, repaired_text, unheld

if TYPE_CHECKING:
    from cpu.game.adapters.profile import GameProfile


def repair_drift(profile: GameProfile, *, user_manager: bool = True) -> None:
    """Set again what drift() lists, straight with set-property: not with setp, which would add
    a second record to game.state and make 'off' restore the profile's own value. A game.intended
    of a newer CST is not read (its records may mean something else), and a setting game.state holds
    no record of is not set again ('off' could not undo it): each said in the journal once per boot
    (again when the text changes, and when it comes back after a tick that had nothing to say:
    GameProfile.say_unheld). Without user_manager, the user manager's settings are left alone (see
    GameProfile.drift)."""
    text, newer = profile.intended()
    if newer is not None:
        said = f"drift not repaired: {newer}"
        profile.say_once(profile.paths.drift_warned, said, lambda: profile.log(4, said))
        return
    drifts = profile.drift(text, user_manager=user_manager)
    skipped = unheld(drifts, records(profile.state_text()))
    profile.say_unheld(skipped)
    for d in drifts:
        if d in skipped:
            continue
        r = d.record
        if d.is_cgroup_file:
            ok = write_sysfs_line(r[1], r[2])
        else:
            mgr = manager_of(r[1]) or "usr"
            ok = profile.ctl(mgr, "set-property", "--runtime", r[2], f"{r[3]}={r[4]}", quiet=True) == 0
        profile.log(*repaired_text(d, ok=ok))
