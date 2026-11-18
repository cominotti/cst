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
from vm.adapters import cpu_disable_list, domain_state, has_managed_save, knobs, virsh
from vm.adapters.backup import utc_now
from vm.domain import KNOBS, URI, VmSettings
from vm.domain.tune import WINDOWS_STEPS, cpu_owner_line, guest_kind, normalized, tune_domain


def cmd_tune(cfg: VmSettings, vm: str, apply: bool, guest_virtio: bool) -> int:
    if apply and os.geteuid() != 0 and not cfg.nonroot_ok:
        raise Refused(f"tune apply needs root: run it as `{cfg.prog} tune apply` (it calls sudo by itself)")
    state = domain_state(vm)
    # Without --security-info: what is printed holds no graphics passwords.
    rc, plain, err = virsh("dumpxml", "--inactive", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    keep_prefixes(plain)
    root = ET.fromstring(plain)
    kind, why = guest_kind(root)
    allow_bus = kind == "linux" or guest_virtio
    kn = knobs(cfg.paths)
    after, changes, held, notes = tune_domain(plain, kn, allow_bus)
    if after is None:
        raise Refused(f"{vm}: {notes[0]}")
    label = {"windows": "Windows", "linux": "Linux", "unknown": "not known"}[kind]
    print(f"{vm}: {state}; guest: {label} ({why})")
    off = [k for k in KNOBS[1:] if not kn[k]]
    if off:
        print(f"  skipped by {cfg.paths.vm_conf}: {', '.join(k + '=no' for k in off)}")
    for c in changes:
        print(f"  change: {c}")
    for h in held:
        print(f"  held back: {h}")
    for n in notes:
        print(f"  note: {n}")
    print(f"  {cpu_owner_line(root, cpu_disable_list(cfg.paths), cfg.cst)}")
    if kind == "linux" and changes and any("bus" in c or "network card" in c for c in changes):
        print(
            "  note: Linux has the virtio drivers in its kernel; an initramfs built with only the needed "
            "drivers (hostonly) may lack virtio_scsi: check `lsinitrd | grep virtio` in the guest first."
        )
    if held:
        print(f"\nHeld back: the guest is {label}, and its virtio drivers are not known to be installed.")
        print(WINDOWS_STEPS.format(cst=cfg.cst, vm=vm))
    diff = list(difflib.unified_diff(normalized(plain).splitlines(True), after.splitlines(True), "current", "tuned"))
    if not diff:
        print(
            f"\n{vm}: disks and network card already as {cfg.cst} vm tune recommends"
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
            print(f"\nPlan only. {msg}; then: {cfg.cst} vm tune apply {vm}{flag}")
            return 0
        raise Refused(msg)
    if not apply:
        print(f"\nPlan only. Apply with: {cfg.cst} vm tune apply {vm}{flag}   (effective at the VM's next start)")
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
    _, stamp = utc_now(cfg.now)
    keep = os.path.join(cfg.paths.backup_dir, vm, "definitions")
    for d in (cfg.paths.backup_dir, os.path.join(cfg.paths.backup_dir, vm), keep):
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
