# SPDX-License-Identifier: MPL-2.0
"""`cst vm tune plan|apply [VM] [--guest-has-virtio]`: the recommended virtual hardware."""

import difflib
import os
import shlex
import sys
import tempfile
import xml.etree.ElementTree as ET

from cst_core.adapters.terminal import confirm
from cst_core.domain.errors import Refused
from cst_core.domain.libvirt_xml import keep_prefixes
from cst_core.domain.text import first_line
from vm.adapters import BACKUP_DIR, CST, PROG, VM_CONF, cpu_disable_list, domain_state, has_managed_save, knobs, virsh
from vm.adapters.backup import utc_now
from vm.domain import KNOBS, URI
from vm.domain.tune import WINDOWS_STEPS, guest_kind, normalized, tune_domain


def cpu_owner_line(root: ET.Element) -> str:
    """How cst cpu vm-pin's part of the definition stands (reported, never changed here)."""
    cpu = root.find("cpu")
    mode = cpu.get("mode", "custom") if cpu is not None else "not set"
    names = cpu_disable_list()
    off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"} if cpu is not None else set()
    missing = [n for n in names if n not in off]
    pins = {c for p in root.findall("cputune/vcpupin") if (c := p.get("cpuset")) is not None}
    pin = f"vCPUs pinned to {', '.join(sorted(pins))}" if pins else "vCPUs not pinned"
    feat = (
        f"NOT disabled: {' '.join(missing)}"
        if mode == "host-passthrough" and missing
        else "CET/MBEC disabled"
        if mode == "host-passthrough" and names
        else f"cpu mode {mode}"
    )
    return f"CPU (owned by {CST} cpu vm-pin, not by tune): {feat}; {pin}; check: {CST} cpu vm-pin plan"


def cmd_tune(vm: str, apply: bool, guest_virtio: bool) -> int:
    if apply and os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused(f"tune apply needs root: run it as `{PROG} tune apply` (it calls sudo by itself)")
    state = domain_state(vm)
    # Without --security-info: what is printed holds no graphics passwords.
    rc, plain, err = virsh("dumpxml", "--inactive", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    keep_prefixes(plain)
    root = ET.fromstring(plain)
    kind, why = guest_kind(root)
    allow_bus = kind == "linux" or guest_virtio
    kn = knobs()
    after, changes, held, notes = tune_domain(plain, kn, allow_bus)
    if after is None:
        raise Refused(f"{vm}: {notes[0]}")
    label = {"windows": "Windows", "linux": "Linux", "unknown": "not known"}[kind]
    print(f"{vm}: {state}; guest: {label} ({why})")
    off = [k for k in KNOBS[1:] if not kn[k]]
    if off:
        print(f"  skipped by {VM_CONF}: {', '.join(k + '=no' for k in off)}")
    for c in changes:
        print(f"  change: {c}")
    for h in held:
        print(f"  held back: {h}")
    for n in notes:
        print(f"  note: {n}")
    print(f"  {cpu_owner_line(root)}")
    if kind == "linux" and changes and any("bus" in c or "network card" in c for c in changes):
        print(
            "  note: Linux has the virtio drivers in its kernel; an initramfs built with only the needed "
            "drivers (hostonly) may lack virtio_scsi: check `lsinitrd | grep virtio` in the guest first."
        )
    if held:
        print(f"\nHeld back: the guest is {label}, and its virtio drivers are not known to be installed.")
        print(WINDOWS_STEPS.format(cst=CST, vm=vm))
    diff = list(difflib.unified_diff(normalized(plain).splitlines(True), after.splitlines(True), "current", "tuned"))
    if not diff:
        print(
            f"\n{vm}: disks and network card already as {CST} vm tune recommends"
            + (" (except what is held back above)." if held else ".")
        )
        return 0
    print()
    sys.stdout.writelines(diff)
    flag = " --guest-has-virtio" if guest_virtio else ""
    bus_change = any(" bus " in c or "network card" in c for c in changes)
    msaved = kind != "linux" and bus_change and state == "shut off" and has_managed_save(vm)
    if kind != "linux" and bus_change and (state != "shut off" or msaved):
        msg = (
            f"{vm} is {state}: for a guest that is not known to be Linux, the bus and network card "
            f"changes are applied only with the VM shut off (shut Windows down first)"
        )
        if msaved:
            msg = (
                f"{vm} was saved (managed save), not shut down: start it and shut Windows down fully "
                f"(`shutdown /s /t 0`), then apply; or drop the saved memory: "
                f"virsh -c {URI} managedsave-remove {vm}"
            )
        if not apply:
            print(f"\nPlan only. {msg}; then: {CST} vm tune apply {vm}{flag}")
            return 0
        raise Refused(msg)
    if not apply:
        print(f"\nPlan only. Apply with: {CST} vm tune apply {vm}{flag}   (effective at the VM's next start)")
        return 0
    # Define from the definition WITH its secrets (else graphics passwords are lost).
    rc, secret, err = virsh("dumpxml", "--inactive", "--security-info", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml --security-info {vm}: {first_line(err) or f'exit {rc}'}")
    s_after, s_changes, _, _ = tune_domain(secret, kn, allow_bus)
    if s_after is None or s_changes != changes:
        raise Refused(f"{vm}: the definition changed while planning: run it again")
    if not confirm(f"\nDefine {vm} with these changes (effective at its next start)?"):
        print("Nothing changed.")
        return 0
    _, stamp = utc_now()
    keep = os.path.join(BACKUP_DIR, vm, "definitions")
    for d in (BACKUP_DIR, os.path.join(BACKUP_DIR, vm), keep):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    saved = os.path.join(keep, f"{stamp}.before-tune.xml")
    # Holds --security-info secrets (graphics passwords): 0600 from the start.
    with os.fdopen(os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(secret)
    fd, tmp = tempfile.mkstemp(prefix="cst-vm-tune.", suffix=".xml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(s_after)
        rc, _, err = virsh("define", tmp)
    finally:
        os.unlink(tmp)
    if rc != 0:
        raise Refused(
            f"virsh define: {first_line(err) or f'exit {rc}'} (nothing changed; "
            f"the definition is as before, also saved in {saved})"
        )
    print(f"Defined. Effective at the next start of {vm}.")
    print(f"Previous definition saved in {saved} (restore: sudo virsh -c {URI} define {shlex.quote(saved)}).")
    rc, now, _ = virsh("dumpxml", "--inactive", vm)
    if rc == 0:
        _, left, _, _ = tune_domain(now, kn, allow_bus)
        if left:
            print("Note: libvirt changed the definition again: still different: " + "; ".join(left))
    return 0


def tune_line(xml_text: str) -> str:
    """One status line: whether the hardware matches tune (all changes, as if
    the drivers were there; Windows gets the hint to read the plan)."""
    try:
        _, changes, _, _ = tune_domain(xml_text, knobs(), True)
        kind, _ = guest_kind(ET.fromstring(xml_text))
    except ET.ParseError:
        return "hardware: definition not readable"
    if not changes:
        return "hardware: disks and network card as tune recommends"
    extra = " (Windows: virtio drivers first, the plan shows how)" if kind != "linux" else ""
    return f"hardware: {len(changes)} difference(s) from tune{extra}: {CST} vm tune plan"
