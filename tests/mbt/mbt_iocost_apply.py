# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst btrfs iocost` apply and revert, live or not, with any one step failing (Hypothesis).

It runs the real apply_iocost and revert_iocost (btrfs/iocost/adapters.py)
on a folder of its own: one NVMe disk (259:1), maybe a second one whose
vendor line (`259:2 enable=1 ctrl=user`) CST must keep, with udevd running
or not. The runner is IocostHost, below: the installer's install, mark and
plan through one real InstallService over the unit tests' in-memory target
and record (tests.test_install.MemoryTarget, tests.test_install_record
.MemoryRecord), which revert also removes through; and systemctl and udevadm,
with what systemd and udev have loaded. Its answers follow the unit tests'
fake (btrfs/tests/test_iocost.py). The kernel's io.cost.qos keeps one line
per device: the machine replaces the module's _write_qos_line (the seam
btrfs/tests/test_iocost_faults.py uses) with a write that merges the line by
MAJ:MIN. os.fsync is stubbed (the TestCase's setUp).

Each step arms at most one fault: a write or a removal of one of the
component's three files, the io.cost.qos write, daemon-reload, enable --now,
disable --now, the udev rules' reload, or the installer's mark. Each fault
records whether it fired (the step made that call).

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
   and the service is enabled; except after a full revert that failed after
   its disable and left the component done (the out-of-scope finding F-I1 of
   this change's review notes: a removal that fails after the disable),
   until the next full apply or revert that exits 0. The io.cost.qos line is
   not part of it: revert --live-only leaves the component done with
   enable=0 (the boot service writes it again).
4. A step whose fault fired exits non-zero, with an ERROR line or the
   installer's own error line; a step with no fault, or whose fault did not
   fire, exits 0 from every state (apply again, and revert again, converge).

Not modelled: a failed write of the managed-devices file, kills (the fault
tests cover them for one apply), --device, a reboot.
"""

from collections.abc import Callable, Sequence
from enum import StrEnum
import errno
import functools
import os
import shutil
import tempfile
from typing import Final, override
import unittest
from unittest import mock

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from btrfs.iocost import adapters as iocost_adapters
from btrfs.iocost.adapters import IocostPaths, apply_iocost, revert_iocost
from btrfs.iocost.domain import IOCOST_DISABLE_CTRL_AUTO, IOCOST_ENABLE_CTRL_AUTO, IOCOST_SERVICE
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, record_file
from cst_core.domain.install import INSTALL_MANIFEST, ArtifactKind, Component, Owner, select_entries
from cst_core.domain.record import ComponentState
from cst_core.ports import CommandResult, WriteFailed
from tests.fault_injection import stub_fsync
from tests.mbt import profiles
from tests.test_install import MemoryTarget
from tests.test_install_record import MemoryRecord

profiles.load()

COMPONENT: Final = Component.BTRFS_IOCOST.value
ENTRIES: Final = tuple(select_entries(INSTALL_MANIFEST, component=Component.BTRFS_IOCOST))
DESTS: Final = tuple(e.dest for e in ENTRIES)
DISK: Final = "259:1"
ENABLED_LINE: Final = f"{DISK} {IOCOST_ENABLE_CTRL_AUTO}"
DISABLED_LINE: Final = f"{DISK} {IOCOST_DISABLE_CTRL_AUTO}"
VENDOR_DISK: Final = "259:2"
VENDOR_LINE: Final = f"{VENDOR_DISK} enable=1 ctrl=user"
INSTALLER_NAME: Final = "cst installer"  # how installer.py's errors begin (cst_core/installer.py NAME)


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


def _done(rc: int = 0, out: str = "", err: str = "", *, argv: Sequence[str]) -> CommandResult:
    return CommandResult(rc, out, err, argv=tuple(argv))


class IocostHost:
    """installer.py, systemctl and udevadm for btrfs:iocost, and what they changed.

    events: what this step did that worked, in order ("install", "reload", "enable", "udev", ...);
    violations: the marks of btrfs:iocost done that came before what the component stands for."""

    def __init__(self, svc: InstallService, target: FiringTarget, req: InstallRequest, record: str) -> None:
        self.svc = svc
        self.target = target
        self.req = req
        self.record = record
        self.paths = {e.dest: svc.placement(e, req).path for e in ENTRIES}
        self.unit = next(self.paths[e.dest] for e in ENTRIES if e.kind == ArtifactKind.UNIT)
        self.rule = next(self.paths[e.dest] for e in ENTRIES if e.dest.endswith(".rules"))
        self.enabled = False
        self.loaded_unit: bytes | None = None  # the unit file as systemd loaded it at its last reload
        self.loaded_rule: bytes | None = None  # the rule as udev loaded it at its last reload
        self.udevd = True
        self.fail: IocostStepFault = IocostStepFault.NONE
        self.fired = False
        self.events: list[str] = []
        self.violations: list[str] = []
        # Set by the machine: whether io.cost.qos holds 259:1 enable=1 now.
        self.disk_enabled: Callable[[], bool] = lambda: False

    def file(self, path: str) -> bytes | None:
        held = self.target.files.get(path)
        return None if held is None else held[0]

    def _failing(self, fault: IocostStepFault) -> bool:
        if self.fail != fault:
            return False
        self.fired = True
        return True

    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        cmd = list(argv)
        if len(cmd) > 4 and cmd[3].endswith("installer.py"):
            return self._installer(cmd)
        handler = {
            ("systemctl", "show"): self._show,
            ("systemctl", "daemon-reload"): self._daemon_reload,
            ("systemctl", "enable"): self._enable,
            ("systemctl", "disable"): self._disable,
            ("udevadm", "control"): self._udev_reload,
            ("udevadm", "info"): self._udev_info,
        }.get((cmd[0], cmd[1]) if len(cmd) > 1 else ("", ""))
        if handler is None:
            raise AssertionError(f"command not modelled: {cmd}")
        return handler(cmd)

    def _show(self, cmd: list[str]) -> CommandResult:
        load = "loaded" if self.unit in self.target.files else "not-found"
        active, state = ("active", "enabled") if self.enabled else ("inactive", "disabled")
        return _done(out=f"LoadState={load}\nActiveState={active}\nUnitFileState={state}\n", argv=cmd)

    def _daemon_reload(self, cmd: list[str]) -> CommandResult:
        if self._failing(IocostStepFault.RELOAD):
            return _done(1, err="Failed to reload daemon: Access denied", argv=cmd)
        self.loaded_unit = self.file(self.unit)
        self.events.append("reload")
        return _done(argv=cmd)

    def _enable(self, cmd: list[str]) -> CommandResult:
        if cmd != ["systemctl", "enable", "--now", IOCOST_SERVICE]:
            raise AssertionError(f"command not modelled: {cmd}")
        if self._failing(IocostStepFault.ENABLE):
            return _done(1, err="Failed to enable unit: Access denied", argv=cmd)
        if self.unit not in self.target.files:
            return _done(1, err=f"Failed to enable unit: Unit file {IOCOST_SERVICE} does not exist", argv=cmd)
        self.enabled = True
        self.events.append("enable")
        return _done(argv=cmd)

    def _disable(self, cmd: list[str]) -> CommandResult:
        if cmd != ["systemctl", "disable", "--now", IOCOST_SERVICE]:
            raise AssertionError(f"command not modelled: {cmd}")
        if self._failing(IocostStepFault.DISABLE):
            return _done(1, err="Failed to disable unit: Access denied", argv=cmd)
        self.enabled = False
        self.events.append("disable")
        return _done(argv=cmd)

    def _udev_reload(self, cmd: list[str]) -> CommandResult:
        if cmd != ["udevadm", "control", "--reload-rules"]:
            raise AssertionError(f"command not modelled: {cmd}")
        if self._failing(IocostStepFault.UDEV):
            return _done(1, err="Failed to send reload request: Access denied", argv=cmd)
        self.loaded_rule = self.file(self.rule)
        self.events.append("udev")
        return _done(argv=cmd)

    def _udev_info(self, cmd: list[str]) -> CommandResult:
        return _done(out="USEC_INITIALIZED=1234\n", argv=cmd)  # processed by udev, no hwdb solution

    def _installer(self, cmd: list[str]) -> CommandResult:
        match cmd[4:]:
            case ["install", "--component", c] if c == COMPONENT:
                lines: list[str] = []
                try:
                    lines.extend(self.svc.install(ENTRIES, self.req))
                except (WriteFailed, InstallRefused) as e:
                    self.events.append("install failed")
                    return _done(1, "".join(f"{x}\n" for x in lines), f"{INSTALLER_NAME}: {e}", argv=cmd)
                self.events.append("install")
                return _done(out="".join(f"{x}\n" for x in lines), argv=cmd)
            case ["mark", "--component", c, "done"] if c == COMPONENT:
                if self._failing(IocostStepFault.MARK):
                    return _done(1, err=f"{INSTALLER_NAME}: cannot write {self.record}: Access denied", argv=cmd)
                missing = self._marked_too_soon()
                if missing:
                    self.violations.append(f"mark done before {', '.join(missing)}: this step did {self.events}")
                self.svc.mark(self.record, COMPONENT)
                self.events.append("mark")
                return _done(argv=cmd)
            case ["plan", *_]:
                return _done(argv=cmd)
            case _:
                raise AssertionError(f"installer command not modelled: {cmd}")

    def _marked_too_soon(self) -> list[str]:
        """What a mark done now comes before (a7c5e92's contract), from this step's events."""
        ev = self.events
        if "install" not in ev:
            return ["the install worked"]
        after = ev[ev.index("install") + 1 :]
        missing = [] if self.disk_enabled() and "qos failed" not in ev else ["io.cost.qos was written"]
        if "reload" not in after:
            missing.append("systemd reloaded")
        if "enable" not in after:
            missing.append("the service was enabled")
        if self.udevd and "udev" not in after:
            missing.append("the udev rules were reloaded")
        return missing


class IocostMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-mbt-iocost-"))
        sys_fs = os.path.join(self.tmp, "sys", "fs", "cgroup")
        root = os.path.join(self.tmp, "root")
        # The machine's own install root, record path and udevd socket (an older IocostPaths has none of them).
        self.record = record_file(Owner.ROOT, None, root)
        self.udev_control = os.path.join(self.tmp, "run", "udev", "control")
        self.paths = IocostPaths(
            qos_path=os.path.join(sys_fs, "io.cost.qos"),
            model_path=os.path.join(sys_fs, "io.cost.model"),
            controllers_path=os.path.join(sys_fs, "cgroup.controllers"),
            sys_block=os.path.join(self.tmp, "sys", "block"),
            managed_file=os.path.join(self.tmp, "run", "btrfs-setup", "iocost-managed"),
            rule_dest=os.path.join(self.tmp, "etc", "udev", "rules.d", "91-cst-iocost.rules"),
            installer_script="/opt/fake/cst_core/installer.py",
            udev_control=self.udev_control,
            lock_path=os.path.join(self.tmp, "run", "btrfs-setup", "iocost-managed.lock"),
            install_root=root,
        )
        self.target = FiringTarget()
        self.records = MemoryRecord()
        self.svc = InstallService(self.target, lambda _e: b"payload\n", record=self.records)
        self.host = IocostHost(self.svc, self.target, InstallRequest(root=root, as_root=True), self.record)
        self.host.disk_enabled = lambda: self._disk_line() == ENABLED_LINE
        self.vendor = False
        self.qos_fault = False
        self.qos_fired = False
        # F-I1 (see the module's docstring): a full revert stopped after its disable, the component still done.
        self.revert_left_done = False

    @override
    def teardown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------------------------
    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _qos(self) -> dict[str, str]:
        """io.cost.qos as {MAJ:MIN: its line}."""
        with open(self.paths.qos_path, encoding="utf-8") as fh:
            return {line.split()[0]: line for line in fh.read().splitlines() if line.strip()}

    def _write_qos_line(self, qos_path: str, payload: str) -> OSError | None:
        """The kernel's io.cost.qos: a write replaces the line of its device, the others stay."""
        if self.qos_fault:
            self.qos_fired = True
            self.host.events.append("qos failed")
            return OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        lines = self._qos()
        lines[payload.split(maxsplit=1)[0]] = payload
        self._write(qos_path, "".join(f"{line}\n" for line in lines.values()))
        return None

    def _component(self) -> tuple[ComponentState | None, set[str]]:
        """btrfs:iocost's state in the install record, and the destinations it lists for it."""
        rec = self.records.read(self.record)
        if rec is None:
            return None, set()
        return rec.components.get(COMPONENT), {d for d, f in rec.files.items() if f.component == COMPONENT}

    def _in_place(self) -> list[str]:
        """The component's files that are in the target."""
        return [d for d in DESTS if self.host.paths[d] in self.target.files]

    def _disk_line(self) -> str | None:
        return self._qos().get(DISK)

    def _managed(self) -> bool:
        if not os.path.exists(self.paths.managed_file):
            return False
        with open(self.paths.managed_file, encoding="utf-8") as fh:
            return DISK in fh.read().split()

    def _step(
        self, run: Callable[[], tuple[int, list[str]]], what: str, fault: IocostStepFault, dest: str
    ) -> tuple[int, list[str]]:
        self.host.events, self.host.fired, self.target.fired, self.qos_fired = [], False, False, False
        self.host.fail = fault
        self.qos_fault = fault == IocostStepFault.QOS
        self.target.fail_on = self.host.paths[dest] if fault == IocostStepFault.FILE else None
        try:
            with mock.patch.object(iocost_adapters, "_write_qos_line", self._write_qos_line):
                rc, lines = run()
        finally:
            self.host.fail, self.qos_fault, self.target.fail_on = IocostStepFault.NONE, False, None
        fired = self.host.fired or self.target.fired or self.qos_fired
        label = f"{what} ({fault}{' ' + dest if fault == IocostStepFault.FILE else ''})"
        if not fired:
            assert rc == 0, f"{label}: no fault fired, exit {rc}: {lines}"
        else:
            assert rc != 0, f"{label}: the fault fired and the step exited 0: {lines}"
            said = any(line.startswith(("ERROR:", f"{INSTALLER_NAME}:")) for line in lines)
            assert said, f"{label}: exit {rc} with no error line: {lines}"
        return rc, lines

    def _assert_applied(self, lines: list[str]) -> None:
        h = self.host
        state, recorded = self._component()
        assert state == ComponentState.DONE, f"apply exit 0, btrfs:iocost {state}: {lines}"
        assert self._in_place() == list(DESTS), f"apply exit 0, files in place: {self._in_place()}"
        assert recorded == set(DESTS), f"apply exit 0, recorded: {sorted(recorded)}"
        assert h.loaded_unit is not None and h.loaded_unit == h.file(h.unit), "apply exit 0, unit not loaded"
        assert not h.udevd or h.loaded_rule == h.file(h.rule), "apply exit 0, udev rule not loaded"
        assert h.enabled, f"apply exit 0, service not enabled: {lines}"
        assert self._disk_line() == ENABLED_LINE, f"apply exit 0, io.cost.qos: {self._qos()}"

    def _assert_reset(self, *, managed: bool, what: str) -> None:
        assert not os.path.exists(self.paths.managed_file), f"{what} exit 0, the managed-devices file is still there"
        if managed:
            assert self._disk_line() == DISABLED_LINE, f"{what} exit 0, io.cost.qos: {self._qos()}"

    # ---- rules -------------------------------------------------------------------------------
    @initialize(udevd=st.booleans(), vendor=st.booleans())
    def start(self, *, udevd: bool, vendor: bool) -> None:
        """One NVMe disk (and maybe a vendor's), io.cost.qos without CST's line, nothing installed."""
        self._write(self.paths.controllers_path, "cpuset cpu io memory pids\n")
        self._write(self.paths.qos_path, f"{VENDOR_LINE}\n" if vendor else "")
        self._write(self.paths.model_path, "")
        disks = [("nvme0n1", DISK), *([("nvme1n1", VENDOR_DISK)] if vendor else [])]
        for name, devno in disks:
            base = os.path.join(self.paths.sys_block, name)
            for rel, text in (("dev", devno), ("queue/rotational", "0"), ("queue/scheduler", "[none]")):
                self._write(os.path.join(base, rel), text + "\n")
        if udevd:
            self._write(self.udev_control, "")
        self.host.udevd = udevd
        self.vendor = vendor

    @rule(fault=st.sampled_from(APPLY_FAULTS), dest=st.sampled_from(DESTS))
    def apply(self, fault: IocostStepFault, dest: str) -> None:
        rc, lines = self._step(functools.partial(apply_iocost, self.paths, runner=self.host), "apply", fault, dest)
        if rc == 0:
            self.revert_left_done = False
            self._assert_applied(lines)

    @rule(fault=st.sampled_from(LIVE_FAULTS))
    def apply_live_only(self, fault: IocostStepFault) -> None:
        run = functools.partial(apply_iocost, self.paths, runner=self.host, live_only=True)
        rc, _ = self._step(run, "apply --live-only", fault, DESTS[0])
        if rc == 0:
            assert self._disk_line() == ENABLED_LINE, f"apply --live-only exit 0, io.cost.qos: {self._qos()}"

    @rule(fault=st.sampled_from(REVERT_FAULTS), dest=st.sampled_from(DESTS))
    def revert(self, fault: IocostStepFault, dest: str) -> None:
        managed = self._managed()
        run = functools.partial(revert_iocost, self.paths, runner=self.host, installer=self.svc)
        rc, lines = self._step(run, "revert", fault, dest)
        state, recorded = self._component()
        if rc == 0:
            self.revert_left_done = False
            assert (state, recorded, self._in_place()) == (None, set(), []), f"revert exit 0 left {state} {recorded}"
            assert not self.host.enabled, f"revert exit 0, the service is enabled: {lines}"
            self._assert_reset(managed=managed, what="revert")
        elif "disable" in self.host.events and state == ComponentState.DONE:
            self.revert_left_done = True  # F-I1

    @rule(fault=st.sampled_from(LIVE_FAULTS))
    def revert_live_only(self, fault: IocostStepFault) -> None:
        managed = self._managed()
        run = functools.partial(revert_iocost, self.paths, runner=self.host, live_only=True)
        rc, _ = self._step(run, "revert --live-only", fault, DESTS[0])
        if rc == 0:
            self._assert_reset(managed=managed, what="revert --live-only")

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def marked_done_only_after_what_it_stands_for(self) -> None:
        assert not self.host.violations, self.host.violations

    @invariant()
    def the_vendor_line_stays(self) -> None:
        if self.vendor:
            assert self._qos().get(VENDOR_DISK) == VENDOR_LINE, f"io.cost.qos: {self._qos()}"

    @invariant()
    def done_means_installed_loaded_and_enabled(self) -> None:
        state, _ = self._component()
        if state != ComponentState.DONE or self.revert_left_done:
            return
        h = self.host
        assert self._in_place() == list(DESTS), f"btrfs:iocost done, files in place: {self._in_place()}"
        assert h.loaded_unit is not None and h.loaded_unit == h.file(h.unit), "btrfs:iocost done, unit not loaded"
        assert h.enabled, "btrfs:iocost done, the service is not enabled"


class IocostMachineTest(profiles.MbtTestCase):
    # Not `IocostMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(IocostMachine)


if __name__ == "__main__":
    unittest.main()
