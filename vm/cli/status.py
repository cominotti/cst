# SPDX-License-Identifier: MPL-2.0
"""`cst vm list [VM]` and `cst vm status [VM]`: the backups, and the VM's state and hardware."""

import xml.etree.ElementTree as ET

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from vm.adapters import (
    CST,
    INDEX,
    cpu_disable_list,
    describe_entry,
    image_dirs,
    knobs,
    package_versions,
    probe_dir,
    read_index,
    virsh,
    wants_links,
)
from vm.cli.nocow import nocow_lines
from vm.cli.tune import tune_line
from vm.domain import instance, version_changes


def cmd_list(vm: str | None) -> int:
    entries = [e for e in read_index() if vm is None or e["vm"] == vm]
    if not entries:
        print(f"no VM backups{f' of {vm}' if vm else ''} in {INDEX} (make one: {CST} vm backup)")
        return 0
    for e in sorted(entries, key=lambda e: (e["vm"], e.get("epoch") or 0)):
        print(describe_entry(e))
    return 0


def cmd_status(vm: str) -> int:
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
        names = cpu_disable_list()
        if cpu is None:
            print("  cpu: no <cpu> element (or not readable)")
        else:
            mode = cpu.get("mode", "custom")
            off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"}
            missing = [n for n in names if n not in off]
            if mode != "host-passthrough":
                print(f"  cpu mode {mode}: the CET/MBEC disables are only for host-passthrough")
            elif not names:
                print(f"  cpu mode {mode}: CPU features not managed (VM_CPU_DISABLE is empty)")
            elif missing:
                print(
                    f"  cpu mode {mode}: NOT disabled: {' '.join(missing)} (Windows may bootloop under "
                    f"QEMU 11; fix: {CST} cpu vm-pin plan)"
                )
            else:
                print(f"  cpu mode {mode}: disabled {' '.join(names)}")
        if rc == 0:
            print(f"  {tune_line(xml_text)}")
    entries = sorted((e for e in read_index() if e["vm"] == vm), key=lambda e: e.get("epoch") or 0)
    if not entries:
        print(
            f"  last backup: none (to the NAS, also while it runs: {CST} vm backup --nas; "
            f"a local copy with the VM shut off: {CST} vm backup)"
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
            f"  weekly NAS backup timer: {'on' if instance(vm) in wants_links() else 'off'}"
            f" ({CST} vm backup-timer status {vm})"
        )
    except Refused:
        pass
    if knobs()["VM_NOCOW"]:
        print("VM image folders (NOCOW):")
        for d in image_dirs():
            for ln in nocow_lines(probe_dir(d), verbose=False):
                print(f"  {ln}")
    return 0
