# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst btrfs scrub-limit` set and off, with a failing installer or reload (Hypothesis).

It runs the real scrub_limit_apply (btrfs/scrub_limit/adapters.py) on a
folder of its own, through any sequence of set SIZE and off from four
starting snapshots.conf files, where the installer or the daemon-reload
fails. The runner is the unit tests' SystemdModel (btrfs/tests/fakes.py):
the installer's text and remove-text through the real InstallService and an
install record in the example's folder, and what systemd has loaded of the
drop-in, which outlives a run. btrfs:core is absent from the record at the
start, or done in it (a seed text and the drop-in recorded). os.fsync is
stubbed (the TestCase's setUp), so the machine does not depend on TMPDIR.

After every step: snapshots.conf's SCRUB_LIMIT and the drop-in's limit
agree; btrfs:core keeps the state it had at the start (with none at the
start: none, or pending with a file).

Each step: exit 0 means systemd loaded the limit (what it has loaded is the
drop-in on disk, and it is not behind). A step that exits non-zero had its
fault fire (it ran the installer or the daemon-reload made to fail), says
ERROR and never ROLLBACK INCOMPLETE, and left snapshots.conf, the drop-in,
whether each of their folders exists, and what systemd has loaded as they
were (systemd may now be behind: the rollback rewrote the drop-in, and the
next run's catch-up reload is part of converging). A step with no fault, or
whose fault did not fire (no daemon-reload was needed), exits 0.

Not modelled: a failed write of snapshots.conf, kills
(btrfs/tests/test_scrub_limit_faults.py covers both), a systemctl that
cannot say whether it is behind.
"""

from enum import StrEnum
import os
import shutil
import tempfile
from typing import Final, override
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from btrfs.scrub_limit.adapters import ScrubPaths, scrub_limit_apply
from btrfs.scrub_limit.domain import parse_scrub_dropin_limit, read_scrub_conf_limit, render_scrub_dropin
from btrfs.tests.fakes import SystemdModel, core_text, read, write
from cst_core.adapters.install import JsonInstallRecord
from cst_core.domain.install import Component
from cst_core.domain.record import ComponentState
from tests.fault_injection import stub_fsync
from tests.mbt import profiles
from tests.repo_files import REPO

profiles.load()

LIMITS: Final = ("500M", "1G", "750m")
START_CONFS: Final = (None, "NAS_ENABLED=1\n", "SCRUB_LIMIT='1G'\n", "NAS_ENABLED=1\nSCRUB_LIMIT='500M'\n")
CORE: Final = Component.BTRFS_CORE.value


class ScrubStepFault(StrEnum):
    """What fails in one step: nothing, the installer, or daemon-reload (a part of the command line, as
    SystemdModel's fail_on takes it)."""

    NONE = "none"
    INSTALLER = "installer.py"
    RELOAD = "systemctl daemon-reload"


type Files = tuple[str | None, str | None, bool, bool]


class ScrubMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-mbt-scrub-"))
        self.conf = os.path.join(self.tmp, "etc", "btrfs-setup", "snapshots.conf")
        self.dropin = os.path.join(
            self.tmp, "etc", "systemd", "system", "btrfs-scrub-weekly.service.d", "10-cst-limit.conf"
        )
        self.paths = ScrubPaths(
            conf_path=self.conf,
            dropin_path=self.dropin,
            btrfs_sysfs=os.path.join(self.tmp, "sys"),
            installer_script=os.path.join(REPO, "cst_core", "installer.py"),
        )
        self.record = os.path.join(self.tmp, "installed.json")  # never the real install record
        self.model = SystemdModel(self.dropin, self.record)
        self.core: ComponentState | None = None  # btrfs:core's state at the start

    @override
    def teardown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------------------------
    def _files(self) -> Files:
        return (
            read(self.conf),
            read(self.dropin),
            os.path.isdir(os.path.dirname(self.conf)),
            os.path.isdir(os.path.dirname(self.dropin)),
        )

    def _apply(self, limit: str | None, fault: ScrubStepFault) -> None:
        what = limit or "off"
        before, loaded_before = self._files(), self.model.loaded
        calls_before = len(self.model.calls)
        if fault != ScrubStepFault.NONE:
            self.model.fail_on = {fault.value}
        try:
            rc, lines = scrub_limit_apply(self.paths, limit, runner=self.model)
        finally:
            self.model.fail_on = set()
        fired = fault != ScrubStepFault.NONE and any(
            fault.value in " ".join(c) for c in self.model.calls[calls_before:]
        )
        if rc == 0:
            assert self.model.loaded == read(self.dropin), f"{what}: exit 0, systemd has not loaded the drop-in"
            assert not self.model.dirty, f"{what}: exit 0, systemd is behind the files: {lines}"
            assert not fired, f"{what}: {fault} fired and the run exited 0: {lines}"
            return
        assert fired, f"{what}: exit {rc} with no fault that fired ({fault}): {lines}"
        assert self._files() == before, f"{what} failed ({fault}) and changed {before} -> {self._files()}: {lines}"
        assert self.model.loaded == loaded_before, f"{what} failed ({fault}) and changed what systemd loaded"
        assert any(line.startswith("ERROR:") for line in lines), f"{what}: exit {rc} with no ERROR line: {lines}"
        assert not any("ROLLBACK INCOMPLETE" in line for line in lines), f"{what} ({fault}): {lines}"

    # ---- rules -------------------------------------------------------------------------------
    @initialize(conf=st.sampled_from(START_CONFS), core=st.sampled_from([None, ComponentState.DONE]))
    def start(self, conf: str | None, core: ComponentState | None) -> None:
        dropin = None
        if conf is not None:
            write(self.conf, conf)
            limit, _ = read_scrub_conf_limit(conf)
            if limit:  # as a set left it: the drop-in of that limit, loaded
                dropin = render_scrub_dropin(limit)
                write(self.dropin, dropin)
                self.model.loaded = dropin
        self.core = core
        if core is not None:
            # btrfs:core as an install leaves it: a seed text and the drop-in recorded, then done.
            svc = self.model.svc
            svc.put(core_text(os.path.join(self.tmp, "core-seed"), self.record), b"seed\n", as_root=False)
            if dropin is not None:
                svc.put(core_text(self.dropin, self.record), dropin.encode(), as_root=False)
            svc.mark(self.record, CORE)

    @rule(limit=st.sampled_from(LIMITS), fault=st.sampled_from(ScrubStepFault))
    def set_limit(self, limit: str, fault: ScrubStepFault) -> None:
        self._apply(limit, fault)

    @rule(fault=st.sampled_from(ScrubStepFault))
    def off(self, fault: ScrubStepFault) -> None:
        self._apply(None, fault)

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def the_conf_and_the_drop_in_agree(self) -> None:
        conf_limit, _ = read_scrub_conf_limit(read(self.conf))
        dropin_limit = parse_scrub_dropin_limit(read(self.dropin))
        assert conf_limit == dropin_limit, f"snapshots.conf says {conf_limit!r}, the drop-in {dropin_limit!r}"

    @invariant()
    def btrfs_core_keeps_its_state(self) -> None:
        rec = JsonInstallRecord().read(self.record)
        state = None if rec is None else rec.components.get(CORE)
        if self.core is not None:
            assert state == self.core, f"btrfs:core {self.core} at the start, now {state}"
            return
        assert state in {None, ComponentState.PENDING}, f"btrfs:core had no state, now {state}"
        if state is not None and rec is not None:
            assert any(f.component == CORE for f in rec.files.values()), f"btrfs:core {state} with no file: {rec}"


class ScrubMachineTest(profiles.MbtTestCase):
    # Not `ScrubMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(ScrubMachine)


if __name__ == "__main__":
    unittest.main()
