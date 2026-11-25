# SPDX-License-Identifier: MPL-2.0
"""The game profile's `state`: what is applied now, and which settings drifted from what 'on' set.

It reads, never writes, and takes no lock (it never waits); GameProfileSteps.state runs show_state over the
profile's paths, settings, ports and the steps it shares with on, off and tick. No I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from cpu.game.application.leftovers import read_kept
from cpu.game.domain import (
    APP_SLICE,
    BUILDS_SLICE,
    USER_SLICE,
    Record,
    escapes_in_state,
    first_cpu,
    newer_intended,
    records,
    state_head,
    unreadable_report,
)

from cpu.game.domain.leftovers import leftovers_line

if TYPE_CHECKING:
    from cpu.game.application.profile import GameProfileSteps


def show_state(profile: GameProfileSteps) -> int:
    """What is applied now, and which settings drifted from what 'on' set (with the profile on, in the
    user manager of the user 'on' acted for)."""
    # One snapshot of both files (None: not there; an empty game.state is on), with no lock: never waits.
    files = profile.ports.state
    state_text, intended_text = files.snapshot(profile.paths.state), files.snapshot(profile.paths.intended)
    on, manual = state_text is not None, files.exists(profile.paths.manual)
    state_recs = records(state_text or "")
    why = None if profile.problems else profile.target_refusal()  # as 'on' reads TARGET_USER
    owned = not on or profile.user_from_state(state_recs, say=False)
    p, s = profile.paths, profile.s
    profile.say_lines(state_head(on=on, manual=manual, problem_lines=profile.problem_lines(), why=why))
    _slice_line(profile, "machine.slice")
    if profile.uses_builds_slice():
        _slice_line(profile, BUILDS_SLICE)
    _user_manager_lines(profile, state_recs if on else None)
    newer = None if intended_text is None else newer_intended(p.intended, intended_text)
    if on:
        profile.say_lines(unreadable_report(p, state_text or "", intended_text or "", judged=newer is None))
    first = first_cpu(s.build_cpus)
    epp0, epp_first = profile.ports.power.read_stripped(p.epp("0")), profile.ports.power.read_stripped(p.epp(first))
    profile.say(f"  EPP cpu0 {epp0 or ''}, cpu{first} {epp_first or ''}")
    kept = read_kept(profile, say=False)
    if kept is not None and kept:
        profile.say(leftovers_line(kept, p.epp))
    if on:
        profile.say_lines(profile.drift_report(state_recs, intended_text, newer, manual=manual, user_manager=owned))
    return 0


def _user_manager_lines(profile: GameProfileSteps, state_recs: Sequence[Record] | None) -> None:
    """The lines of the user manager 'on' acted for (none without one), with game.state's idle boxes when on."""
    if profile.target_refusal() is not None:
        return
    profile.say(f"  toolboxes       weight={profile.prop('usr', USER_SLICE, 'CPUWeight')}")
    profile.say(f"  app.slice       zswap writeback={profile.prop('usr', APP_SLICE, 'MemoryZSwapWriteback')}")
    cg = profile.ask("usr", "show", "-P", "ControlGroup", USER_SLICE).stdout.rstrip("\n")
    effective = profile.ports.kernel.read_stripped(f"{profile.paths.cgroot}{cg}/cpuset.cpus.effective") or ""
    profile.say(f"  toolboxes       cpus effective={effective}")
    if state_recs is not None:
        profile.say(f"  other boxes     idle: {escapes_in_state(state_recs)}")


def _slice_line(profile: GameProfileSteps, unit: str) -> None:
    cpus, weight, io = (profile.prop("sys", unit, n) for n in ("AllowedCPUs", "CPUWeight", "IOWeight"))
    profile.say(f"  {unit:<15} cpus={cpus:<8} weight={weight:<4} io={io}")
