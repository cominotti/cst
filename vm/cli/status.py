# SPDX-License-Identifier: MPL-2.0
"""`cst vm list [VM]` and `cst vm status [VM]`: the backups, and the VM's state and hardware."""

import xml.etree.ElementTree as ET

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from vm.adapters import cpu_disable_list, knobs, package_versions, virsh
from vm.adapters.backup import describe_entry, read_index
from vm.adapters.images import image_dirs, probe_dir
from vm.adapters.timer import wants_links
from vm.domain import instance, nocow_lines, version_changes, VmSettings
from vm.domain.tune import cpu_status_line, tune_line


def cmd_list(cfg: VmSettings, vm: str | None) -> int:
    entries = [e for e in read_index(cfg) if vm is None or e["vm"] == vm]
    if not entries:
        print(f"no VM backups{f' of {vm}' if vm else ''} in {cfg.paths.index} (make one: {cfg.cst} vm backup)")
        return 0
    for e in sorted(entries, key=lambda e: (e["vm"], e.get("epoch") or 0)):
        print(describe_entry(e))
    return 0


def cmd_status(cfg: VmSettings, vm: str) -> int:
    rc, so, err = virsh("domstate", vm)
    if rc == 127:
        print(f"{vm}: virsh not found (libvirt not installed here)")
    elif rc != 0:
        print(f"{vm}: state not readable ({first_line(err) or f'exit {rc}'})")
    else:
        print(f"{vm}: {first_line(so)}")
        rc, xml_text, err = virsh("dumpxml", "--inactive", vm)
        try:
            cpu = ET.fromstring(xml_text).find("cpu") if rc == 0 else None
        except ET.ParseError:
            cpu = None
        print(f"  {cpu_status_line(cpu, cpu_disable_list(cfg.paths), cfg.cst)}")
        if rc == 0:
            print(f"  {tune_line(xml_text, knobs(cfg.paths), cfg.cst)}")
    entries = sorted((e for e in read_index(cfg) if e["vm"] == vm), key=lambda e: e.get("epoch") or 0)
    if not entries:
        print(
            f"  last backup: none (to the NAS, also while it runs: {cfg.cst} vm backup --nas; "
            f"a local copy with the VM shut off: {cfg.cst} vm backup)"
        )
    else:
        last = entries[-1]
        print(f"  last backup: {describe_entry(last)}")
        changes = version_changes(last.get("versions"), package_versions())
        if changes:
            print("  virtualization packages changed since then (back up again once the VM works on them):")
            for c in changes:
                print(f"    {c}")
        else:
            print("  virtualization packages: unchanged since the last backup")
    nas = [e for e in entries if e.get("mode") == "nas"]
    if nas and nas[-1] is not entries[-1]:
        print(f"  last NAS backup: {describe_entry(nas[-1])}")
    try:
        print(
            f"  weekly NAS backup timer: {'on' if instance(vm) in wants_links(cfg.paths.unit_dir) else 'off'}"
            f" ({cfg.cst} vm backup-timer status {vm})"
        )
    except Refused:
        pass
    if knobs(cfg.paths)["VM_NOCOW"]:
        print("VM image folders (NOCOW):")
        for d in image_dirs(cfg):
            for ln in nocow_lines(probe_dir(d), cfg.prog, verbose=False):
                print(f"  {ln}")
    return 0
