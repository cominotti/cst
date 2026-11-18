# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup-timer on|off|status [VM]`: the weekly NAS backup's units and program copy."""

import os

from cst_core.domain.errors import Refused
from vm import application, wiring
from vm.adapters import nas_settings, read_conf
from vm.adapters.backup import describe_entry, read_index
from vm.domain import ON_CALENDAR, TimerStep, VmSettings, instance, timer_file_words
from vm.ports import TimerPort


def cmd_timer(cfg: VmSettings, sub: str, vm: str, entry_file: str, timers: TimerPort | None = None) -> None:
    """backup-timer SUB VM; timers: a test's, else this machine's (wiring.timer_port)."""
    unit = instance(vm)
    if sub != "status" and os.geteuid() != 0 and not cfg.nonroot_ok:
        raise Refused(f"backup-timer {sub} needs root: run it as `{cfg.prog} backup-timer {sub}` (it calls sudo)")
    port = wiring.timer_port(cfg, entry_file) if timers is None else timers
    if sub == "status":
        st = application.timer_status(port, vm)
        print(f"weekly NAS backup of {vm}: {'on' if st.on else 'off'} ({unit}; {ON_CALENDAR}, up to 30 min later)")
        words = timer_file_words(cfg.prog, vm)
        for p, state in st.files.items():
            print(f"  {p}: {words[state]}")
        if st.next_run:
            print(f"  next run: {st.next_run}")
        if st.last_result:
            print(f"  last run of the service: {st.last_result}")
        nas = [e for e in read_index(cfg) if e["vm"] == vm and e.get("mode") == "nas"]
        print(f"  last NAS backup: {describe_entry(max(nas, key=lambda e: e.get('epoch') or 0)) if nas else 'none'}")
        conf = read_conf(cfg.paths.nas_conf, nas_conf=cfg.paths.nas_conf, bash_sourced=True)
        if conf.get("NAS_ENABLED") != "1" or not conf.get("NAS_PATH"):
            print(f"  the NAS backup is not set up: {cfg.cst} btrfs snapshots nas setup")
        return
    if sub == "on":
        application.timer_on(port, vm, lambda: nas_settings(cfg), show_step)
        print(
            f"On: {unit} ({ON_CALENDAR}, then {cfg.cst} vm backup {vm} --nas). Run one now with: "
            f"sudo systemctl start {unit.replace('.timer', '.service')}"
        )
        return
    still = application.timer_off(port, vm, show_step)
    if still:
        print(f"  kept the units and {cfg.paths.sbin_copy}: still used by {', '.join(still)}")
    print(f"The backups on the NAS stay (list: {cfg.cst} vm list).")


def show_step(step: TimerStep) -> None:
    """One line for what backup-timer on or off just did."""
    match step.kind:
        case "installed" | "updated" | "removed":
            print(f"  {step.kind}: {step.path}")
        case "off":
            print(f"Off: {step.path}")
        case "left":
            print(f"  left as it is: {step.path} (masked with systemctl mask)")
