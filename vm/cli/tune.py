# SPDX-License-Identifier: MPL-2.0
"""`cst vm tune plan|apply [VM] [--guest-has-virtio]`: the recommended virtual hardware."""

from collections.abc import Callable
import shlex
import sys

from cst_core.domain.errors import Refused
from vm import application, wiring
from vm.application.backup import backup_time
from vm.domain import KNOBS, URI, VmSettings
from vm.domain.tune import WINDOWS_STEPS, GuestKind, cpu_owner_line
from vm.ports import DefinitionStorePort, VmCommandsPort


def cmd_tune(
    cfg: VmSettings,
    vm: str,
    apply: bool,
    guest_virtio: bool,
    *,
    commands: VmCommandsPort | None = None,
    store: DefinitionStorePort | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    """tune plan or apply; commands, store and confirm: a test's, else this machine's (wiring)."""
    if apply and not wiring.running_as_root() and not cfg.nonroot_ok:
        raise Refused(f"tune apply needs root: run it as `{cfg.prog} tune apply` (it calls sudo by itself)")
    virsh = wiring.vm_commands() if commands is None else commands
    conf = wiring.settings_port(cfg)
    kn = conf.knobs()
    plan = application.plan_tune(virsh, vm, kn, guest_virtio=guest_virtio)
    label = {GuestKind.WINDOWS: "Windows", GuestKind.LINUX: "Linux", GuestKind.UNKNOWN: "not known"}[plan.kind]
    print(f"{vm}: {plan.state}; guest: {label} ({plan.why})")
    off = [k for k in KNOBS[1:] if not kn[k]]
    if off:
        print(f"  skipped by {cfg.paths.vm_conf}: {', '.join(k + '=no' for k in off)}")
    for c in plan.changes:
        print(f"  change: {c}")
    for h in plan.held:
        print(f"  held back: {h}")
    for n in plan.notes:
        print(f"  note: {n}")
    print(f"  {cpu_owner_line(plan.root, conf.cpu_disable(), cfg.cst)}")
    if plan.kind == GuestKind.LINUX and plan.changes and any("bus" in c or "network card" in c for c in plan.changes):
        print(
            "  note: Linux has the virtio drivers in its kernel; an initramfs built with only the needed "
            "drivers (hostonly) may lack virtio_scsi: check `lsinitrd | grep virtio` in the guest first."
        )
    if plan.held:
        print(f"\nHeld back: the guest is {label}, and its virtio drivers are not known to be installed.")
        print(WINDOWS_STEPS.format(cst=cfg.cst, vm=vm))
    if not plan.diff:
        print(
            f"\n{vm}: disks and network card already as {cfg.cst} vm tune recommends"
            + (" (except what is held back above)." if plan.held else ".")
        )
        return 0
    print()
    sys.stdout.writelines(plan.diff)
    flag = " --guest-has-virtio" if guest_virtio else ""
    if msg := application.tune_blocked(virsh, plan):
        if not apply:
            print(f"\nPlan only. {msg}; then: {cfg.cst} vm tune apply {vm}{flag}")
            return 0
        raise Refused(msg)
    if not apply:
        print(f"\nPlan only. Apply with: {cfg.cst} vm tune apply {vm}{flag}   (effective at the VM's next start)")
        return 0
    saved = application.apply_tune(
        virsh,
        wiring.definition_store(cfg) if store is None else store,
        wiring.ask if confirm is None else confirm,
        lambda: backup_time(cfg, wiring.clock())[1],
        plan,
    )
    if saved is None:
        print("Nothing changed.")
        return 0
    print(f"Defined. Effective at the next start of {vm}.")
    print(f"Previous definition saved in {saved} (restore: sudo virsh -c {URI} define {shlex.quote(saved)}).")
    if left := application.tune_left(virsh, plan):
        print("Note: libvirt changed the definition again: still different: " + "; ".join(left))
    return 0
