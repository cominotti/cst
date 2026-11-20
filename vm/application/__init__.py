# SPDX-License-Identifier: MPL-2.0
"""vm's use cases that change the machine in several steps: backup-timer on and off, tune apply
(and its plan), with the libvirt reads they share with the backups.

They reach the machine through the ports of vm/ports.py, raise Refused, and return or report
records of vm.domain; vm/cli prints them.
"""

from collections.abc import Callable, Mapping
import difflib
import xml.etree.ElementTree as ET

from cst_core.domain.errors import Refused
from cst_core.domain.libvirt_xml import keep_prefixes
from cst_core.domain.text import first_line
from cst_core.domain.files import OwnedState, UnitFileState
from cst_core.domain.units import masked_refusal
from cst_core.ports import CommandFailed, CommandResult, SystemdPort, UnitName
from vm.domain import SHUT_OFF, URI, TimerStatus, TimerStep, TimerStepKind, instance
from vm.domain.tune import GuestKind, TunePlan, guest_kind, normalized, tune_domain
from vm.ports import DefinitionStorePort, TimerFilesPort, VmCommandsPort


def _why(r: CommandResult | CommandFailed) -> str:
    """systemctl's first line of stderr, else its exit status."""
    return first_line(r.stderr) or f"exit {r.returncode}"


def _reload(systemd: SystemdPort) -> str:
    """systemctl daemon-reload; why it failed ("" when it did not)."""
    try:
        systemd.daemon_reload()
    except CommandFailed as e:
        return f"systemctl daemon-reload: {_why(e)}"
    return ""


def _shown(systemd: SystemdPort, unit: str, prop: str) -> str:
    """One property of unit, from the systemd cst-vm runs under; "" when it cannot answer."""
    return systemd.show(UnitName(unit), (prop,)).get(prop, "").strip()


def timer_status(timers: TimerFilesPort, systemd: SystemdPort, vm: str) -> TimerStatus:
    """backup-timer status of vm."""
    unit = instance(vm)
    files = timers.file_states(unit)
    on = unit in timers.timers_on()
    next_run, last_result = "", ""
    if on:
        next_run = _shown(systemd, unit, "NextElapseUSecRealtime")
        last_result = _shown(systemd, unit.replace(".timer", ".service"), "Result")
    return TimerStatus(unit, on, files, next_run, last_result)


def _refuse_foreign(states: Mapping[str, UnitFileState]) -> None:
    foreign = [p for p, st in states.items() if st == OwnedState.CONFLICT]
    if foreign:
        raise Refused("not CST's files, left as they are: " + ", ".join(foreign))


def timer_on(
    timers: TimerFilesPort,
    systemd: SystemdPort,
    vm: str,
    nas_ready: Callable[[], object],
    report: Callable[[TimerStep], None],
) -> str:
    """Install what is not current, turn vm's timer on (systemd reloads the units and starts it,
    enabled), record the component complete; its unit. Refused, before any change, for a file that
    is not CST's, a masked one, or a NAS backup that is not set up (nas_ready raises)."""
    unit = instance(vm)
    states = timers.file_states(unit)
    _refuse_foreign(states)
    if (why := masked_refusal([p for p, st in states.items() if st == OwnedState.MASKED], user=False)) is not None:
        raise Refused(why + "; nothing was changed")
    nas_ready()
    timers.install(states, report)
    if why := _reload(systemd):
        raise Refused(why)
    r = systemd.enable(UnitName(unit), now=True)
    if r.returncode != 0:
        raise Refused(f"systemctl enable --now {unit}: {_why(r)}")
    timers.mark_complete()
    return unit


def timer_off(timers: TimerFilesPort, systemd: SystemdPort, vm: str, report: Callable[[TimerStep], None]) -> list[str]:
    """Turn vm's timer off (stopped and disabled); when no VM's timer is on any more, remove the
    files (a masked one is left) and forget them in the record. The timers still on (then nothing
    is removed)."""
    unit = instance(vm)
    states = timers.file_states(unit)
    _refuse_foreign(states)
    r = systemd.disable(UnitName(unit), now=True)
    if r.returncode != 0 and unit in timers.timers_on():
        raise Refused(f"systemctl disable --now {unit}: {_why(r)}")
    # Forget the timer's last run (Persistent=: /var/lib/systemd/timers/stamp-UNIT): needs the unit loaded
    # and stopped, so before its files go. Best effort: a failure only leaves that small file behind.
    systemd.clean(UnitName(unit), what="state")
    report(TimerStep(TimerStepKind.OFF, unit))
    still = timers.timers_on()
    if still:
        return still
    for p, st in states.items():
        if st == OwnedState.MASKED:
            report(TimerStep(TimerStepKind.LEFT, p))
        elif st != OwnedState.MISSING:
            timers.remove(p)
            report(TimerStep(TimerStepKind.REMOVED, p))
    # A file already missing is forgotten too: removed by hand, or by an earlier off that stopped before
    # its forget (a rerun then clears the record).
    timers.forget([p for p, st in states.items() if st != OwnedState.MASKED])
    _reload(systemd)  # best effort
    return []


def domain_state(commands: VmCommandsPort, vm: str) -> str:
    rc, so, err = commands.virsh(("domstate", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh domstate {vm}: {first_line(err) or f'exit {rc}'}")
    return first_line(so)


def has_managed_save(commands: VmCommandsPort, vm: str) -> bool:
    rc, so, err = commands.virsh(("list", "--all", "--with-managed-save", "--name"), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh list --with-managed-save: {first_line(err) or f'exit {rc}'}")
    return vm in (ln.strip() for ln in so.splitlines())


def plan_tune(commands: VmCommandsPort, vm: str, kn: Mapping[str, bool], *, guest_virtio: bool) -> TunePlan:
    """What tune would change in vm's definition (read without --security-info: what is shown
    holds no graphics passwords)."""
    state = domain_state(commands, vm)
    rc, plain, err = commands.virsh(("dumpxml", "--inactive", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    keep_prefixes(plain)
    root = ET.fromstring(plain)
    kind, why = guest_kind(root)
    allow_bus = kind == GuestKind.LINUX or guest_virtio
    after, changes, held, notes = tune_domain(plain, kn, allow_bus)
    if after is None:
        raise Refused(f"{vm}: {notes[0]}")
    diff = list(difflib.unified_diff(normalized(plain).splitlines(True), after.splitlines(True), "current", "tuned"))
    return TunePlan(vm, state, kind, why, root, dict(kn), allow_bus, changes, held, notes, diff)


def tune_blocked(commands: VmCommandsPort, plan: TunePlan) -> str:
    """Why the plan may not be applied now ("" when it may): a guest not known to be Linux gets the
    bus and network card changes only while shut off, and not from a managed save."""
    vm, state = plan.vm, plan.state
    if plan.kind == GuestKind.LINUX or not any(" bus " in c or "network card" in c for c in plan.changes):
        return ""
    if state != SHUT_OFF:
        return (
            f"{vm} is {state}: for a guest that is not known to be Linux, the bus and network card "
            f"changes are applied only with the VM shut off (shut Windows down first)"
        )
    if has_managed_save(commands, vm):
        return (
            f"{vm} was saved (managed save), not shut down: start it and shut Windows down fully "
            f"(`shutdown /s /t 0`), then apply; or drop the saved memory: "
            f"virsh -c {URI} managedsave-remove {vm}"
        )
    return ""


def apply_tune(
    commands: VmCommandsPort,
    store: DefinitionStorePort,
    confirm: Callable[[str], bool],
    stamp: Callable[[], str],
    plan: TunePlan,
) -> str | None:
    """Define vm with the plan's changes, from the definition WITH its secrets (else graphics
    passwords are lost), after the question, keeping the old one first; the path of that copy, or
    None when the answer was no. Refused when the definition changed since the plan."""
    vm = plan.vm
    rc, secret, err = commands.virsh(("dumpxml", "--inactive", "--security-info", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh dumpxml --security-info {vm}: {first_line(err) or f'exit {rc}'}")
    s_after, s_changes, _, _ = tune_domain(secret, plan.knobs, plan.allow_bus)
    if s_after is None or s_changes != plan.changes:
        raise Refused(f"{vm}: the definition changed while planning: run it again")
    if not confirm(f"\nDefine {vm} with these changes (effective at its next start)?"):
        return None
    saved = store.save(vm, stamp(), secret)
    rc, _, err = commands.define(s_after)
    if rc != 0:
        raise Refused(
            f"virsh define: {first_line(err) or f'exit {rc}'} (nothing changed; "
            f"the definition is as before, also saved in {saved})"
        )
    return saved


def tune_left(commands: VmCommandsPort, plan: TunePlan) -> list[str]:
    """What still differs from tune in the definition libvirt now has ([] when it cannot be read)."""
    rc, now, _ = commands.virsh(("dumpxml", "--inactive", plan.vm), 60, detach=None)
    if rc != 0:
        return []
    _, left, _, _ = tune_domain(now, plan.knobs, plan.allow_bus)
    return left
