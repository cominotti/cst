# SPDX-License-Identifier: MPL-2.0
"""`cst vm tune plan|apply [VM] [--guest-has-virtio]`: the recommended virtual hardware."""

from collections.abc import Callable
import shlex
import sys

from cst_core.domain.errors import Refused
from vm import application, wiring
from vm.application.backup import backup_time
from vm.domain import URI, VmSettings
from vm.domain.tune import TunePlan, tune_plan_lines
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
    for line in tune_plan_lines(plan, conf.cpu_disable(), vm_conf=cfg.paths.vm_conf, cst=cfg.cst):
        print(line)
    if not plan.diff:
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
    _apply_plan(cfg, virsh, plan, store, confirm)
    return 0


def _apply_plan(
    cfg: VmSettings,
    virsh: VmCommandsPort,
    plan: TunePlan,
    store: DefinitionStorePort | None,
    confirm: Callable[[str], bool] | None,
) -> None:
    """tune apply once the plan may be applied: asks, defines, and says where the old definition is."""
    saved = application.apply_tune(
        virsh,
        wiring.definition_store(cfg) if store is None else store,
        wiring.ask if confirm is None else confirm,
        lambda: backup_time(cfg, wiring.clock())[1],
        plan,
    )
    if saved is None:
        print("Nothing changed.")
        return
    print(f"Defined. Effective at the next start of {plan.vm}.")
    print(f"Previous definition saved in {saved} (restore: sudo virsh -c {URI} define {shlex.quote(saved)}).")
    if left := application.tune_left(virsh, plan):
        print("Note: libvirt changed the definition again: still different: " + "; ".join(left))
