# SPDX-License-Identifier: MPL-2.0
"""The use cases of `cst btrfs iocost`: status, plan, apply and revert, live-only or not.

apply (not live-only) installs btrfs:iocost, writes io.cost.qos, reloads systemd, enables the service,
reloads the udev rules and only then marks the component done; the first step that fails stops there.
revert (not live-only) marks it pending, stops the service, removes the component, reloads systemd and
udev, then resets the devices CST manages. A full apply or revert holds the component lock from its
first step to its last; their reads, writes and saves of io.cost.qos and the managed-devices file
happen under the io.cost.qos lock (status and plan read both without it: they change nothing). Every
read and write goes through the ports (ports.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from btrfs.iocost.domain import (
    IOCOST_SERVICE,
    IOCOST_SOLUTIONS_PROP,
    USEC_INITIALIZED_PROP,
    BlockDisk,
    IocostPlan,
    IocostSnapshot,
    UdevProperties,
    format_iocost_plan,
    format_iocost_status,
    is_auto_eligible_disk,
    parse_iocost_qos,
    plan_iocost_apply,
    plan_iocost_revert,
)
from btrfs.iocost.ports import (
    IocostInstallerPort,
    IocostLockPort,
    IocostSysfsPort,
    IocostSystemdPort,
    IocostUninstallPort,
    ManagedDevicesPort,
    QosControlPort,
    UdevPort,
)
from cst_core.application.install import InstallRefused
from cst_core.domain.install import Component
from cst_core.domain.record import ComponentState
from cst_core.domain.text import enum_member
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.ports import CommandResult, WriteFailed

_UNINSTALL_ERRORS: Final = (InstallRefused, WriteFailed, OSError)

type Outcome = tuple[int, list[str]]


def _with_udev_fields(disk: BlockDisk, *, hwdb_solution: str, udev_ready: bool, udev_error: str) -> BlockDisk:
    """disk with its three udev fields given: what dataclasses.replace would build (Sonar types replace's
    result as DataclassInstance, python:S5886)."""
    return BlockDisk(
        name=disk.name,
        devno=disk.devno,
        rotational=disk.rotational,
        scheduler=disk.scheduler,
        model=disk.model,
        hwdb_solution=hwdb_solution,
        udev_ready=udev_ready,
        udev_error=udev_error,
    )


def with_udev_answer(disk: BlockDisk, answer: UdevProperties) -> BlockDisk:
    """disk with what udev knows of it: its hwdb solution, whether udev has processed it, or why
    `udevadm info` could not tell (missing, failed: the disk is judged as before)."""
    if answer.props is None:
        return _with_udev_fields(
            disk, hwdb_solution=disk.hwdb_solution, udev_ready=disk.udev_ready, udev_error=answer.error
        )
    props = answer.props
    return _with_udev_fields(
        disk,
        hwdb_solution=props.get(IOCOST_SOLUTIONS_PROP, "").strip(),
        udev_ready=USEC_INITIALIZED_PROP in props,
        udev_error=disk.udev_error,
    )


def _locked(lock: IocostLockPort, work: Callable[[], Outcome]) -> Outcome:
    """work() while this run holds lock; an error, with nothing done, when it cannot take it."""
    try:
        held = lock.take()
    except OSError as exc:
        return 1, [f"ERROR: cannot take {lock.path}: {exc.strerror or exc}"]
    if not held:
        return 1, [f"ERROR: another btrfs-iocost run holds {lock.path}"]
    try:
        return work()
    finally:
        lock.release()


def _failed(what: str, result: CommandResult) -> Outcome:
    return (result.returncode or 1), [f"ERROR: {result.failure(what)}"]


@dataclass(frozen=True, slots=True)
class Iocost:
    """status, plan, apply and revert of blk-iocost, over the machine's ports. uninstall: the in-process
    removal of btrfs:iocost, given only for a full revert (None otherwise)."""

    sysfs: IocostSysfsPort
    qos: QosControlPort
    managed: ManagedDevicesPort
    udev: UdevPort
    systemd: IocostSystemdPort
    installer: IocostInstallerPort
    qos_lock: IocostLockPort
    component_lock: IocostLockPort
    uninstall: IocostUninstallPort | None = None

    # ---- reads -----------------------------------------------------------------------------------
    def snapshot(self, *, udev: bool = True) -> IocostSnapshot:
        """Kernel `io.cost.qos`, block devices, runtime state, and systemd unit state.

        udev: ask udev about each disk the scan of every disk would write (its hwdb solution); a --device
        run and revert do not need it."""
        disks = self.sysfs.disks()
        if udev:
            disks = tuple(
                with_udev_answer(d, self.udev.properties(d.name)) if is_auto_eligible_disk(d) else d for d in disks
            )
        io_present = self.sysfs.io_controller()
        qos_raw = self.qos.read()
        svc_present, active, enabled = self.systemd.probe()
        return IocostSnapshot(
            io_controller_present=io_present,
            qos_exists=qos_raw is not None,
            qos_entries=parse_iocost_qos(qos_raw or ""),
            model_raw=self.sysfs.model(),
            disks=disks,
            managed_devnos=self.managed.read(),
            service_present=svc_present,
            service_active=active,
            service_enabled=enabled,
            rule_installed=self.udev.rule_installed(),
        )

    def status(self) -> Outcome:
        """`cst btrfs iocost status`."""
        return 0, format_iocost_status(self.snapshot())

    def plan(self, *, live_only: bool, target_device: str | None) -> Outcome:
        """`cst btrfs iocost plan`: what apply would write, changing nothing."""
        snap = self.snapshot(udev=target_device is None)
        pl = plan_iocost_apply(snap, target_device=target_device)
        # The installer's plan builds the zipapp: ask it only when the unit and the rule are there, since
        # otherwise the plan says "would install:" whatever it answers.
        installed = snap.service_present and snap.rule_installed
        changes = self.installer.changes() if not live_only and pl.error is None and installed else None
        return (1 if pl.error is not None else 0), format_iocost_plan(
            snap, pl, live_only=live_only, component_changes=changes
        )

    # ---- io.cost.qos and the managed-devices file ---------------------------------------------------
    def _saved(self, devnos: frozenset[str]) -> list[str]:
        """The managed-devices file saved, or its ERROR line when it cannot be written or removed (ENOSPC, a
        read-only /run): `cannot write PATH: REASON`, as every write of CST says it."""
        try:
            self.managed.save(devnos)
        except OSError as exc:
            return [f"ERROR: {WriteFailed(self.managed.path, exc.strerror or str(exc))}"]
        return []

    def _apply_qos_writes(self, snap: IocostSnapshot, plan: IocostPlan) -> Outcome:
        """Write the plan's io.cost.qos lines and save the devices CST manages: (1 when a write or the save
        failed, lines). apply saves the devices it is about to enable before it writes their lines, and
        then the set without the ones whose write failed."""
        if not snap.qos_exists:
            # No blk-iocost here: nothing to reset, and nothing stays CST's.
            err = self._saved(frozenset()) if plan.revert_mode else []
            return (1 if err else 0), [f"skipped:   {self.qos.path} unavailable", *err]
        managed = set(plan.next_managed)
        if not plan.revert_mode and plan.writes and frozenset(managed) != snap.managed_devnos:
            # Intent first: the devices apply is about to enable are CST's before their lines are written, so
            # a save that fails (or a kill) after a write cannot leave a line CST wrote and no longer knows
            # as its own (the next apply would keep it as a vendor's, and revert would never reset it).
            err = self._saved(frozenset(managed))
            if err:
                return 1, [*err, "nothing was written to io.cost.qos"]
        out: list[str] = []
        rc = 0
        verb = "reverted:" if plan.revert_mode else "set:"
        for op in plan.writes:
            try:
                self.qos.write(op.line)
            except OSError as err:
                rc = 1
                out.append(f"ERROR: {self.qos.path} {op.device_name}: {err.strerror or err}")
                if plan.revert_mode:
                    managed.add(op.devno)
                else:
                    managed.discard(op.devno)
                continue
            out.append(f"{verb:<10} {self.qos.path} <- '{op.line}' ({op.device_name})")
        out.extend(f"unchanged: {item}" for item in plan.unchanged)
        out.extend(f"kept:      {item}" for item in plan.preexisting)
        out.extend(f"note:      {item}" for item in plan.notes)
        err = self._saved(frozenset(managed))
        return (1 if err else rc), [*out, *err]

    # ---- apply -------------------------------------------------------------------------------------
    def _reload_udev_rules(self) -> Outcome:
        """`udevadm control --reload-rules` when udevd runs; without it there is nothing to reload."""
        if not self.udev.running():
            return 0, ["note:      udevd not running; rules are read when it starts"]
        res = self.udev.reload_rules()
        return _failed("udevadm control --reload-rules", res) if res.returncode != 0 else (0, [])

    def _enable_iocost_service(self) -> Outcome:
        dr = self.systemd.daemon_reload()
        if dr.returncode != 0:
            return _failed("systemctl daemon-reload", dr)
        en = self.systemd.enable_now()
        if en.returncode != 0:
            return _failed(f"systemctl enable --now {IOCOST_SERVICE}", en)
        rc_u, lines_u = self._reload_udev_rules()
        return rc_u, [f"enabled:   {IOCOST_SERVICE}", *lines_u]

    def _install_and_write(self, *, live_only: bool, target_device: str | None) -> Outcome:
        """Read, plan, install btrfs:iocost (not live-only) and write io.cost.qos: the part under the lock."""
        snap = self.snapshot(udev=target_device is None)
        plan = plan_iocost_apply(snap, target_device=target_device)
        if plan.error is not None:
            return (
                (0, [f"skipped:   {target_device} ({plan.error})"])
                if (live_only and target_device is not None)
                else (1, [f"ERROR: {plan.error}"])
            )
        out: list[str] = []
        if not live_only:
            rc_i, lines_i = self.installer.install()
            out.extend(lines_i)
            if rc_i != 0:
                return rc_i, out
        rc_w, lines_w = self._apply_qos_writes(snap, plan)
        return rc_w, [*out, *lines_w]

    def apply(self, *, live_only: bool = False, target_device: str | None = None) -> Outcome:
        """Enable `blk-iocost` in `io.cost.qos` and optionally install unit + udev persistence.

        Not live-only: install btrfs:iocost, write, daemon-reload, enable --now, reload the udev rules, and
        only then mark the component done; the first step that fails stops there (rc != 0, still pending).
        It holds the component lock from the first step to the mark, as a full revert does."""
        if not live_only and not self.installer.available():
            # The installed zipapp has no installer.py, unit verifier or payload to install from.
            return 1, [
                "ERROR: apply installs btrfs:iocost from a CST checkout: run it from one (cst btrfs iocost apply)"
            ]

        def install_and_write() -> Outcome:
            return _locked(
                self.qos_lock, lambda: self._install_and_write(live_only=live_only, target_device=target_device)
            )

        if live_only:
            return install_and_write()

        def full() -> Outcome:
            rc_w, out = install_and_write()
            if rc_w != 0:
                return rc_w, out
            # The iocost lock is free again: enable --now starts the service, which takes it.
            rc_s, lines_s = self._enable_iocost_service()
            out.extend(lines_s)
            if rc_s != 0:
                return rc_s, out
            rc_m, lines_m = self.installer.mark_done()
            return rc_m, [*out, *lines_m]

        return _locked(self.component_lock, full)

    # ---- revert ------------------------------------------------------------------------------------
    def _remove_iocost_files(self, uninstall: IocostUninstallPort) -> Outcome:
        try:
            return 0, uninstall.remove()
        except _UNINSTALL_ERRORS as exc:
            return 1, [f"ERROR: {exc}"]

    def _unchanged_since(self, before: tuple[bool, str, str]) -> bool:
        """The service as the probe before found it: both probes answered with systemd's words (not "unknown":
        a probe that cannot say proves nothing) and they agree on ActiveState and UnitFileState."""
        now = self.systemd.probe()
        known = enum_member(ActiveState, before[1]) is not None and enum_member(EnablementState, before[2]) is not None
        return known and now == before

    def _put_state_back(
        self, uninstall: IocostUninstallPort, before: tuple[bool, str, str], prior: ComponentState | None
    ) -> list[str]:
        """After a failed disable: btrfs:iocost back to done when it was done and the disable changed nothing
        (_unchanged_since). Otherwise it stays pending, the truth: disable --now can disable the unit and then
        fail to stop it. The ERROR line when the record cannot be written again (pending stays: it only
        overstates what is left to do)."""
        if prior != ComponentState.DONE or not self._unchanged_since(before):
            return []
        try:
            uninstall.mark(ComponentState.DONE)
        except _UNINSTALL_ERRORS as exc:
            return [f"ERROR: cannot put {Component.BTRFS_IOCOST.value} back to done: {exc}"]
        return []

    def _remove_iocost_component(self, uninstall: IocostUninstallPort) -> Outcome:
        """Mark btrfs:iocost pending, stop the service, remove the component and reload systemd and udev; the
        first failure stops there. A failure after the disable leaves pending (or no state, once its last file
        and line are gone), never done; a record that cannot be marked stops revert before anything changes."""
        out: list[str] = []
        try:
            prior = uninstall.mark(ComponentState.PENDING)
        except _UNINSTALL_ERRORS as exc:
            return 1, [f"ERROR: {exc}", "nothing was removed"]
        before = self.systemd.probe()
        if before[0]:
            dis = self.systemd.disable_now()
            if dis.returncode != 0:
                rc_d, lines_d = _failed(f"systemctl disable --now {IOCOST_SERVICE}", dis)
                back = self._put_state_back(uninstall, before, prior)
                return rc_d, [*lines_d, "nothing was removed", *back]
            out.append(f"disabled:  {IOCOST_SERVICE}")
        rc_r, lines_r = self._remove_iocost_files(uninstall)
        out.extend(lines_r)
        if rc_r != 0:
            return rc_r, out
        dr = self.systemd.daemon_reload()
        if dr.returncode != 0:
            rc_dr, lines_dr = _failed("systemctl daemon-reload", dr)
            return rc_dr, [*out, *lines_dr]
        rc_u, lines_u = self._reload_udev_rules()
        return rc_u, [*out, *lines_u]

    def revert(self, *, live_only: bool = False) -> Outcome:
        """Disable CST-managed `blk-iocost` entries in `io.cost.qos` and optionally uninstall unit/rule.

        Not live-only, the service and the rule go first (so no udev event enables a disk again after its
        reset), then the managed devices are reset, all under the component lock (as a full apply)."""

        def reset() -> Outcome:
            snap = self.snapshot(udev=False)
            return self._apply_qos_writes(snap, plan_iocost_revert(snap))

        if live_only:
            return _locked(self.qos_lock, reset)
        uninstall = self.uninstall
        if uninstall is None:
            raise AssertionError("a full revert needs the in-process removal of btrfs:iocost (uninstall)")

        def full() -> Outcome:
            rc_c, out = self._remove_iocost_component(uninstall)
            if rc_c != 0:
                return rc_c, out
            rc_w, lines_w = _locked(self.qos_lock, reset)
            return rc_w, [*out, *lines_w]

        return _locked(self.component_lock, full)
