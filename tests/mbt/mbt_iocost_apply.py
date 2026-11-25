# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst btrfs iocost` apply and revert, live or not, with any one step failing (Hypothesis).

It runs iocost's use cases (btrfs/iocost/application.py, Iocost.apply and
Iocost.revert) over IocostWorld (btrfs/tests/fakes.py), the in-memory fakes
of their ports, which btrfs/tests/test_iocost_ports.py keeps equal to the
real adapters: one NVMe disk (259:1), maybe a second one whose vendor line
(`259:2 enable=1 ctrl=user`) CST must keep, with udevd running or not. The
installer's install and mark and revert's removal go through the world's
real InstallService over the unit tests' in-memory target and record
(tests.test_install.MemoryTarget, here FiringTarget, and
tests.test_install_record.MemoryRecord); the world keeps what systemd and
udev have loaded, and its io.cost.qos keeps one line per device, as the
kernel's does.

Each step arms at most one fault: a write or a removal of one of the
component's three files, the io.cost.qos write, daemon-reload, enable --now,
disable --now, the udev rules' reload, or the installer's mark. Each fault
records whether it fired (the step made that call). An apply may come with
an upgrade (a new CST: every file's payload changes), so its install
rewrites the files that are there and a file's fault hits a rewrite too, not
only a first install.

Checked:
1. a7c5e92's contract: at every mark of btrfs:iocost done, this apply has
   installed the component with no failure, written its io.cost.qos lines
   with none failing (259:1 is enable=1), reloaded systemd after the
   install, enabled the service and reloaded the udev rules (or udevd is not
   running).
2. apply exit 0 (not live-only): btrfs:iocost done, its files in place and
   recorded, systemd and udev loaded them, the service enabled, 259:1
   enable=1. revert exit 0 (not live-only): no file and no state of
   btrfs:iocost, the service disabled, the managed-devices file gone, 259:1
   reset to enable=0 when CST managed it. Live-only: the io.cost.qos lines
   only. The vendor line never changes.
3. btrfs:iocost done means its files are in place, systemd loaded the unit
   and the service is enabled, after every step. The io.cost.qos line is not
   part of it: revert --live-only leaves the component done with enable=0
   (the boot service writes it again). A full revert marks the component
   pending before its disable --now: a revert whose disable failed leaves
   the state as it was (it changed nothing), and one that failed after its
   disable worked leaves pending, or no state once its last file is gone.
4. A step whose fault fired exits non-zero, with an ERROR line or the
   installer's own error line; a step with no fault, or whose fault did not
   fire, exits 0 from every state (apply again, and revert again, converge).

Not modelled: a failed write of the managed-devices file, kills (the fault
tests cover them for one apply), --device, a reboot.
"""

from collections.abc import Callable
from enum import StrEnum
import errno
import functools
import os
from typing import Final, override
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from btrfs.iocost.domain import IOCOST_DISABLE_CTRL_AUTO, IOCOST_ENABLE_CTRL_AUTO
from btrfs.tests.fakes import IOCOST_ENTRIES, IocostWorld
from cst_core.application.install import InstallService
from cst_core.domain.record import ComponentState
from cst_core.installer import NAME as INSTALLER_NAME
from tests.mbt import profiles
from tests.test_install import MemoryTarget

profiles.load()

DESTS: Final = tuple(e.dest for e in IOCOST_ENTRIES)
DISK: Final = "259:1"
ENABLED_LINE: Final = f"{DISK} {IOCOST_ENABLE_CTRL_AUTO}"
DISABLED_LINE: Final = f"{DISK} {IOCOST_DISABLE_CTRL_AUTO}"
VENDOR_DISK: Final = "259:2"
VENDOR_LINE: Final = f"{VENDOR_DISK} enable=1 ctrl=user"


class IocostStepFault(StrEnum):
    """What fails in one step (FILE: the write or the removal of the file the rule draws)."""

    NONE = "none"
    FILE = "file"
    QOS = "qos"
    RELOAD = "daemon-reload"
    ENABLE = "enable"
    DISABLE = "disable"
    UDEV = "udev-reload"
    MARK = "mark"


APPLY_FAULTS: Final = (
    IocostStepFault.NONE,
    IocostStepFault.FILE,
    IocostStepFault.QOS,
    IocostStepFault.RELOAD,
    IocostStepFault.ENABLE,
    IocostStepFault.UDEV,
    IocostStepFault.MARK,
)
REVERT_FAULTS: Final = (
    IocostStepFault.NONE,
    IocostStepFault.DISABLE,
    IocostStepFault.FILE,
    IocostStepFault.RELOAD,
    IocostStepFault.UDEV,
    IocostStepFault.QOS,
)
LIVE_FAULTS: Final = (IocostStepFault.NONE, IocostStepFault.QOS)
# The world's point each command fault fails: the command answers exit 1, "Access denied" (the installer's
# mark: its own error line).
POINT_OF: Final = {
    IocostStepFault.RELOAD: "systemd daemon-reload",
    IocostStepFault.ENABLE: "systemd enable --now",
    IocostStepFault.DISABLE: "systemd disable --now",
    IocostStepFault.UDEV: "udev reload-rules",
    IocostStepFault.MARK: "installer mark done",
}


class FiringTarget(MemoryTarget):
    """MemoryTarget whose fail_on also records that it answered a failure (fired)."""

    def __init__(self) -> None:
        super().__init__()
        self.fired = False

    @override
    def write(self, path: str, data: bytes, mode: int) -> None:
        self.fired = self.fired or path == self.fail_on
        super().write(path, data, mode)

    @override
    def remove(self, path: str) -> bool:
        self.fired = self.fired or path == self.fail_on
        return super().remove(path)


class IocostMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        w = IocostWorld()
        self.target = FiringTarget()
        w.target = self.target
        w.svc = InstallService(self.target, lambda e: f"{e.dest} {w.release}\n".encode(), record=w.records)
        w.before_mark = self._check_mark
        self.world = w
        # violations: the marks of btrfs:iocost done that came before what the component stands for.
        self.violations: list[str] = []
        self.vendor = False

    # ---- helpers -----------------------------------------------------------------------------
    def _qos(self) -> dict[str, str]:
        """io.cost.qos as {MAJ:MIN: its line}."""
        return dict(self.world.qos or {})

    def _check_mark(self) -> None:
        missing = self._marked_too_soon()
        if missing:
            self.violations.append(f"mark done before {', '.join(missing)}: this step did {self.world.events}")

    def _marked_too_soon(self) -> list[str]:
        """What a mark done now comes before (a7c5e92's contract), from this step's events."""
        ev = self.world.events
        if "install" not in ev:
            return ["the install worked"]
        after = ev[ev.index("install") + 1 :]
        missing = [] if self._disk_line() == ENABLED_LINE and "qos failed" not in ev else ["io.cost.qos was written"]
        if "reload" not in after:
            missing.append("systemd reloaded")
        if "enable" not in after:
            missing.append("the service was enabled")
        if self.world.udevd and "udev" not in after:
            missing.append("the udev rules were reloaded")
        return missing

    def _component(self) -> tuple[ComponentState | None, set[str]]:
        """btrfs:iocost's state in the install record, and the destinations it lists for it."""
        return self.world.state(), self.world.recorded()

    def _in_place(self) -> list[str]:
        """The component's files that are in the target."""
        return self.world.in_place()

    def _disk_line(self) -> str | None:
        return self._qos().get(DISK)

    def _managed(self) -> bool:
        return DISK in self.world.managed

    def _unit_loaded(self) -> bool:
        w = self.world
        return w.systemd.loaded is not None and w.systemd.loaded == w.file(w.unit_path)

    def _step(
        self, run: Callable[[], tuple[int, list[str]]], what: str, fault: IocostStepFault, dest: str
    ) -> tuple[int, list[str]]:
        w = self.world
        w.events, w.answered, self.target.fired = [], [], False
        point = POINT_OF.get(fault)
        if point is not None:
            said = f"{INSTALLER_NAME}: cannot write {w.record}: Access denied"  # installer.py's own error line
            w.fail(point, OSError(errno.EACCES, said if fault == IocostStepFault.MARK else "Access denied"))
        if fault == IocostStepFault.QOS:
            w.qos_refusal = OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        self.target.fail_on = w.dest_path(dest) if fault == IocostStepFault.FILE else None
        try:
            rc, lines = run()
        finally:
            w.failing.clear()
            w.qos_refusal, self.target.fail_on = None, None
        fired = bool(w.answered) or self.target.fired or "qos failed" in w.events
        label = f"{what} ({fault}{' ' + dest if fault == IocostStepFault.FILE else ''})"
        if not fired:
            assert rc == 0, f"{label}: no fault fired, exit {rc}: {lines}"
        else:
            assert rc != 0, f"{label}: the fault fired and the step exited 0: {lines}"
            said = any(line.startswith(("ERROR:", f"{INSTALLER_NAME}:")) for line in lines)
            assert said, f"{label}: exit {rc} with no error line: {lines}"
        return rc, lines

    def _assert_applied(self, lines: list[str]) -> None:
        w = self.world
        state, recorded = self._component()
        assert state == ComponentState.DONE, f"apply exit 0, btrfs:iocost {state}: {lines}"
        assert self._in_place() == list(DESTS), f"apply exit 0, files in place: {self._in_place()}"
        assert recorded == set(DESTS), f"apply exit 0, recorded: {sorted(recorded)}"
        assert self._unit_loaded(), "apply exit 0, unit not loaded"
        assert not w.udevd or w.udev_loaded == w.file(w.rule_path), "apply exit 0, udev rule not loaded"
        assert w.systemd.enabled, f"apply exit 0, service not enabled: {lines}"
        assert self._disk_line() == ENABLED_LINE, f"apply exit 0, io.cost.qos: {self._qos()}"

    def _assert_reset(self, *, managed: bool, what: str) -> None:
        assert not self.world.managed, f"{what} exit 0, the managed-devices file is still there"
        if managed:
            assert self._disk_line() == DISABLED_LINE, f"{what} exit 0, io.cost.qos: {self._qos()}"

    # ---- rules -------------------------------------------------------------------------------
    @initialize(udevd=st.booleans(), vendor=st.booleans())
    def start(self, *, udevd: bool, vendor: bool) -> None:
        """One NVMe disk (and maybe a vendor's), io.cost.qos without CST's line, nothing installed."""
        w = self.world
        w.qos = {VENDOR_DISK: VENDOR_LINE} if vendor else {}
        w.put_disk("nvme0n1", DISK)
        if vendor:
            w.put_disk("nvme1n1", VENDOR_DISK)
        w.udevd = udevd
        self.vendor = vendor

    @rule(fault=st.sampled_from(APPLY_FAULTS), dest=st.sampled_from(DESTS), upgrade=st.booleans())
    def apply(self, fault: IocostStepFault, dest: str, *, upgrade: bool) -> None:
        if upgrade:
            self.world.release += 1
        what = "apply (upgrade)" if upgrade else "apply"
        rc, lines = self._step(self.world.service().apply, what, fault, dest)
        if rc == 0:
            self._assert_applied(lines)

    @rule(fault=st.sampled_from(LIVE_FAULTS))
    def apply_live_only(self, fault: IocostStepFault) -> None:
        run = functools.partial(self.world.service().apply, live_only=True)
        rc, _ = self._step(run, "apply --live-only", fault, DESTS[0])
        if rc == 0:
            assert self._disk_line() == ENABLED_LINE, f"apply --live-only exit 0, io.cost.qos: {self._qos()}"

    @rule(fault=st.sampled_from(REVERT_FAULTS), dest=st.sampled_from(DESTS))
    def revert(self, fault: IocostStepFault, dest: str) -> None:
        managed = self._managed()
        before, _ = self._component()
        run = self.world.service().revert
        rc, lines = self._step(run, "revert", fault, dest)
        state, recorded = self._component()
        if rc == 0:
            assert (state, recorded, self._in_place()) == (None, set(), []), f"revert exit 0 left {state} {recorded}"
            assert not self.world.systemd.enabled, f"revert exit 0, the service is enabled: {lines}"
            self._assert_reset(managed=managed, what="revert")
        elif fault == IocostStepFault.DISABLE and self.world.answered:
            assert state == before, f"revert ({fault}): the disable failed, btrfs:iocost {before} -> {state}: {lines}"
        elif "disable" in self.world.events:
            assert state in {ComponentState.PENDING, None}, f"revert ({fault} {dest}) after its disable left {state}"

    @rule(fault=st.sampled_from(LIVE_FAULTS))
    def revert_live_only(self, fault: IocostStepFault) -> None:
        managed = self._managed()
        run = functools.partial(self.world.service().revert, live_only=True)
        rc, _ = self._step(run, "revert --live-only", fault, DESTS[0])
        if rc == 0:
            self._assert_reset(managed=managed, what="revert --live-only")

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def marked_done_only_after_what_it_stands_for(self) -> None:
        assert not self.violations, self.violations

    @invariant()
    def the_vendor_line_stays(self) -> None:
        if self.vendor:
            assert self._qos().get(VENDOR_DISK) == VENDOR_LINE, f"io.cost.qos: {self._qos()}"

    @invariant()
    def done_means_installed_loaded_and_enabled(self) -> None:
        state, _ = self._component()
        if state != ComponentState.DONE:
            return
        assert self._in_place() == list(DESTS), f"btrfs:iocost done, files in place: {self._in_place()}"
        assert self._unit_loaded(), "btrfs:iocost done, unit not loaded"
        assert self.world.systemd.enabled, "btrfs:iocost done, the service is not enabled"


class IocostMachineTest(profiles.MbtTestCase):
    # Not `IocostMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(IocostMachine)


if __name__ == "__main__":
    unittest.main()
