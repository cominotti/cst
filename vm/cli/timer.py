# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup-timer on|off|status [VM]`: the weekly NAS backup's units and program copy."""

import os

from cst_core.domain.errors import Refused
from cst_core.domain.units import masked_refusal
from vm.adapters import read_conf, systemctl, nas_settings
from vm.adapters.backup import describe_entry, read_index
from vm.adapters.timer import (
    forget_timer_files,
    install_timer_files,
    timer_file_states,
    timer_files,
    timer_record_done,
    wants_links,
)
from vm.domain import ON_CALENDAR, instance, timer_file_words, VmSettings


def cmd_timer(cfg: VmSettings, sub: str, vm: str, entry_file: str) -> int:
    unit = instance(vm)
    if sub != "status" and os.geteuid() != 0 and not cfg.nonroot_ok:
        raise Refused(f"backup-timer {sub} needs root: run it as `{cfg.prog} backup-timer {sub}` (it calls sudo)")
    files = timer_files(cfg, entry_file)
    states = timer_file_states(cfg.paths, files, os.path.join(cfg.paths.unit_dir, unit))
    if sub == "status":
        on = unit in wants_links(cfg.paths.unit_dir)
        print(f"weekly NAS backup of {vm}: {'on' if on else 'off'} ({unit}; {ON_CALENDAR}, up to 30 min later)")
        words = timer_file_words(cfg.prog, vm)
        for p, st in states.items():
            print(f"  {p}: {words[st]}")
        if on:
            rc, nxt, _ = systemctl("show", "-p", "NextElapseUSecRealtime", "--value", unit)
            rc2, last, _ = systemctl("show", "-p", "Result", "--value", unit.replace(".timer", ".service"))
            if rc == 0 and nxt:
                print(f"  next run: {nxt}")
            if rc2 == 0 and last:
                print(f"  last run of the service: {last}")
        nas = [e for e in read_index(cfg) if e["vm"] == vm and e.get("mode") == "nas"]
        print(f"  last NAS backup: {describe_entry(max(nas, key=lambda e: e.get('epoch') or 0)) if nas else 'none'}")
        conf = read_conf(cfg.paths.nas_conf, nas_conf=cfg.paths.nas_conf, bash_sourced=True)
        if conf.get("NAS_ENABLED") != "1" or not conf.get("NAS_PATH"):
            print(f"  the NAS backup is not set up: {cfg.cst} btrfs snapshots nas setup")
        return 0
    foreign = [p for p, st in states.items() if st == "conflict"]
    if foreign:
        raise Refused("not CST's files, left as they are: " + ", ".join(foreign))
    if sub == "on":
        if why := masked_refusal([p for p, st in states.items() if st == "masked"], user=False):
            raise Refused(why + "; nothing was changed")
        nas_settings(cfg)
        install_timer_files(cfg.paths, files, states, entry_file)
        for args in (("daemon-reload",), ("enable", "--now", unit)):
            rc, _, err = systemctl(*args)
            if rc != 0:
                raise Refused(f"systemctl {' '.join(args)}: {err or f'exit {rc}'}")
        timer_record_done(cfg.paths, entry_file)
        print(
            f"On: {unit} ({ON_CALENDAR}, then {cfg.cst} vm backup {vm} --nas). Run one now with: "
            f"sudo systemctl start {unit.replace('.timer', '.service')}"
        )
        return 0
    rc, _, err = systemctl("disable", "--now", unit)
    if rc != 0 and unit in wants_links(cfg.paths.unit_dir):
        raise Refused(f"systemctl disable --now {unit}: {err or f'exit {rc}'}")
    # Forget the timer's last run (Persistent=: /var/lib/systemd/timers/stamp-UNIT): needs the unit loaded
    # and stopped, so before its files go. A failure only leaves that small file behind.
    systemctl("clean", "--what=state", unit)
    print(f"Off: {unit}")
    if not wants_links(cfg.paths.unit_dir):
        for p, st in states.items():
            if st == "masked":
                print(f"  left as it is: {p} (masked with systemctl mask)")
            elif st != "missing":
                os.unlink(p)
                print(f"  removed: {p}")
        forget_timer_files(cfg.paths, entry_file, [p for p, st in states.items() if st not in ("missing", "masked")])
        systemctl("daemon-reload")
    else:
        print(f"  kept the units and {cfg.paths.sbin_copy}: still used by {', '.join(wants_links(cfg.paths.unit_dir))}")
    print(f"The backups on the NAS stay (list: {cfg.cst} vm list).")
    return 0
