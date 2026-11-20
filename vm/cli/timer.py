# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup-timer on|off|status [VM]`: the weekly NAS backup's units and program copy."""

from cst_core.domain.errors import Refused
from vm import application, wiring
from vm.domain import ON_CALENDAR, TimerStep, TimerStepKind, VmSettings, VmSub, instance
from vm.domain.backup import timer_status_lines
from vm.ports import TimerFilesPort


def cmd_timer(cfg: VmSettings, sub: VmSub, vm: str, entry_file: str, timers: TimerFilesPort | None = None) -> None:
    """backup-timer SUB VM; timers: a test's, else this machine's (wiring.timer_port)."""
    unit = instance(vm)
    if sub != VmSub.STATUS and not wiring.running_as_root() and not cfg.nonroot_ok:
        raise Refused(f"backup-timer {sub} needs root: run it as `{cfg.prog} backup-timer {sub}` (it calls sudo)")
    port, sysd = (wiring.timer_port(cfg, entry_file) if timers is None else timers), wiring.systemd()
    if sub == VmSub.STATUS:
        for line in timer_status_lines(application.timer_status(port, sysd, vm), vm, cfg.prog):
            print(line)
        _print_nas_state(cfg, vm)
        return
    if sub == VmSub.ON:
        application.timer_on(port, sysd, vm, wiring.settings_port(cfg).nas_repo, show_step)
        print(
            f"On: {unit} ({ON_CALENDAR}, then {cfg.cst} vm backup {vm} --nas). Run one now with: "
            f"sudo systemctl start {unit.replace('.timer', '.service')}"
        )
        return
    still = application.timer_off(port, sysd, vm, show_step)
    if still:
        print(f"  kept the units and {cfg.paths.sbin_copy}: still used by {', '.join(still)}")
    print(f"The backups on the NAS stay (list: {cfg.cst} vm list).")


def _print_nas_state(cfg: VmSettings, vm: str) -> None:
    """backup-timer status's lines about the NAS: the VM's last NAS backup, and whether it is set up."""
    store = wiring.backup_store(cfg)
    nas = [e for e in store.read_index() if e["vm"] == vm and e.get("mode") == "nas"]
    print(f"  last NAS backup: {store.describe(max(nas, key=lambda e: e.get('epoch') or 0)) if nas else 'none'}")
    if not wiring.settings_port(cfg).nas_set_up():
        print(f"  the NAS backup is not set up: {cfg.cst} btrfs snapshots nas setup")


def show_step(step: TimerStep) -> None:
    """One line for what backup-timer on or off just did."""
    match step.kind:
        case TimerStepKind.INSTALLED | TimerStepKind.UPDATED | TimerStepKind.REMOVED:
            print(f"  {step.kind}: {step.path}")
        case TimerStepKind.OFF:
            print(f"Off: {step.path}")
        case TimerStepKind.LEFT:
            print(f"  left as it is: {step.path} (masked with systemctl mask)")
