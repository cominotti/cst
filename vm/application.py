# SPDX-License-Identifier: MPL-2.0
"""vm's use cases that change the machine in several steps: backup-timer on and off.

They reach the machine through the ports of vm/ports.py, raise Refused, and return or report
records of vm.domain; vm/cli prints them.
"""

from collections.abc import Callable, Mapping

from cst_core.domain.errors import Refused
from cst_core.domain.files import UnitFileState
from cst_core.domain.units import masked_refusal
from vm.domain import TimerStatus, TimerStep, instance
from vm.ports import TimerPort


def timer_status(timers: TimerPort, vm: str, instance_timer: str) -> TimerStatus:
    """backup-timer status of vm (instance_timer: the path of its own unit, see file_states)."""
    unit = instance(vm)
    files = timers.file_states(instance_timer)
    on = unit in timers.timers_on()
    next_run, last_result = timers.schedule(unit) if on else ("", "")
    return TimerStatus(unit, on, files, next_run, last_result)


def _refuse_foreign(states: Mapping[str, UnitFileState]) -> None:
    foreign = [p for p, st in states.items() if st == "conflict"]
    if foreign:
        raise Refused("not CST's files, left as they are: " + ", ".join(foreign))


def timer_on(
    timers: TimerPort,
    vm: str,
    instance_timer: str,
    nas_ready: Callable[[], object],
    report: Callable[[TimerStep], None],
) -> str:
    """Install what is not current, turn vm's timer on, record the component complete; its unit.
    Refused, before any change, for a file that is not CST's, a masked one, or a NAS backup that is
    not set up (nas_ready raises)."""
    unit = instance(vm)
    states = timers.file_states(instance_timer)
    _refuse_foreign(states)
    if why := masked_refusal([p for p, st in states.items() if st == "masked"], user=False):
        raise Refused(why + "; nothing was changed")
    nas_ready()
    timers.install(states, report)
    timers.turn_on(unit)
    timers.mark_complete()
    return unit


def timer_off(timers: TimerPort, vm: str, instance_timer: str, report: Callable[[TimerStep], None]) -> list[str]:
    """Turn vm's timer off; when no VM's timer is on any more, remove the files (a masked one is
    left) and forget them in the record. The timers still on (then nothing is removed)."""
    unit = instance(vm)
    states = timers.file_states(instance_timer)
    _refuse_foreign(states)
    why = timers.turn_off(unit)
    if why and unit in timers.timers_on():
        raise Refused(why)
    # Forget the timer's last run (Persistent=: /var/lib/systemd/timers/stamp-UNIT): needs the unit loaded
    # and stopped, so before its files go. A failure only leaves that small file behind.
    timers.forget_last_run(unit)
    report(TimerStep("off", unit))
    still = timers.timers_on()
    if still:
        return still
    for p, st in states.items():
        if st == "masked":
            report(TimerStep("left", p))
        elif st != "missing":
            timers.remove(p)
            report(TimerStep("removed", p))
    timers.forget([p for p, st in states.items() if st not in ("missing", "masked")])
    timers.reload()
    return []
