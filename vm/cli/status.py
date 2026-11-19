# SPDX-License-Identifier: MPL-2.0
"""`cst vm list [VM]` and `cst vm status [VM]`: the backups, and the VM's state and hardware."""

import contextlib
import xml.etree.ElementTree as ET

from cst_core.domain.errors import ExitStatus, Refused
from cst_core.domain.text import first_line
from vm import wiring
from vm.domain import instance, nocow_lines, version_changes, VmSettings
from vm.domain.tune import cpu_status_line, tune_line


def cmd_list(cfg: VmSettings, vm: str | None) -> None:
    store = wiring.backup_store(cfg)
    entries = [e for e in store.read_index() if vm is None or e["vm"] == vm]
    if not entries:
        print(f"no VM backups{f' of {vm}' if vm else ''} in {cfg.paths.index} (make one: {cfg.cst} vm backup)")
        return
    for e in sorted(entries, key=lambda e: (e["vm"], e.get("epoch") or 0)):
        print(store.describe(e))


def cmd_status(cfg: VmSettings, vm: str, entry_file: str) -> None:
    commands, store, conf = wiring.vm_commands(), wiring.backup_store(cfg), wiring.settings_port(cfg)
    rc, so, err = commands.virsh(("domstate", vm), 60, detach=None)
    if rc == ExitStatus.NOT_FOUND:
        print(f"{vm}: virsh not found (libvirt not installed here)")
    elif rc != 0:
        print(f"{vm}: state not readable ({first_line(err) or f'exit {rc}'})")
    else:
        print(f"{vm}: {first_line(so)}")
        rc, xml_text, err = commands.virsh(("dumpxml", "--inactive", vm), 60, detach=None)
        try:
            cpu = ET.fromstring(xml_text).find("cpu") if rc == 0 else None
        except ET.ParseError:
            cpu = None
        print(f"  {cpu_status_line(cpu, conf.cpu_disable(), cfg.cst)}")
        if rc == 0:
            print(f"  {tune_line(xml_text, conf.knobs(), cfg.cst)}")
    entries = sorted((e for e in store.read_index() if e["vm"] == vm), key=lambda e: e.get("epoch") or 0)
    if not entries:
        print(
            f"  last backup: none (to the NAS, also while it runs: {cfg.cst} vm backup --nas; "
            f"a local copy with the VM shut off: {cfg.cst} vm backup)"
        )
    else:
        last = entries[-1]
        print(f"  last backup: {store.describe(last)}")
        changes = version_changes(last.get("versions"), commands.package_versions())
        if changes:
            print("  virtualization packages changed since then (back up again once the VM works on them):")
            for c in changes:
                print(f"    {c}")
        else:
            print("  virtualization packages: unchanged since the last backup")
    nas = [e for e in entries if e.get("mode") == "nas"]
    if nas and nas[-1] is not entries[-1]:
        print(f"  last NAS backup: {store.describe(nas[-1])}")
    with contextlib.suppress(Refused):
        print(
            f"  weekly NAS backup timer: {'on' if instance(vm) in wiring.timer_port(cfg, entry_file).timers_on() else 'off'}"
            f" ({cfg.cst} vm backup-timer status {vm})"
        )
    if conf.knobs()["VM_NOCOW"]:
        print("VM image folders (NOCOW):")
        for d in wiring.images(cfg).folders():
            for ln in nocow_lines(d, cfg.prog, verbose=False):
                print(f"  {ln}")
