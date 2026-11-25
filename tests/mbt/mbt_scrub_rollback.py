# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst btrfs scrub-limit` set and off, with a failing installer or reload (Hypothesis).

It runs ScrubLimit.apply (btrfs/scrub_limit/application.py) over the
in-memory fakes of its ports (ScrubWorld, btrfs/tests/fakes.py), through
any sequence of set SIZE and off from four starting snapshots.conf files,
where the installer or the daemon-reload fails: the drop-in's text and
remove-text through the real InstallService and an in-memory install
record, and what systemd has loaded of the drop-in, which outlives a run.
btrfs:core is absent from the record at the start, or done in it (a seed
text and the drop-in recorded). What the real adapters run for each port is
pinned in btrfs/tests/test_scrub_limit_ports.py.

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
cannot say whether it is behind, and a fault that fails only some of the
calls it matches. A fault fails every installer call, or every
daemon-reload, of its step, so the rollback's installer call never fails
after a forward one that worked, and a first reload never fails before a
later one works: the never-ROLLBACK-INCOMPLETE and left-as-they-were checks
see only that all-or-nothing pattern. A failed undo is covered by
btrfs/tests/test_scrub_limit.py (test_a_rollback_that_fails_says_so) and
the combinations of btrfs/tests/test_scrub_limit_faults.py.
"""

from enum import StrEnum
import errno
from typing import Final
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from btrfs.scrub_limit.domain import parse_scrub_dropin_limit, read_scrub_conf_limit, render_scrub_dropin
from btrfs.tests.fakes import ScrubWorld
from cst_core.domain.record import ComponentState
from tests.mbt import profiles

profiles.load()

LIMITS: Final = ("500M", "1G", "750m")
START_CONFS: Final = (None, "NAS_ENABLED=1\n", "SCRUB_LIMIT='1G'\n", "NAS_ENABLED=1\nSCRUB_LIMIT='500M'\n")


class ScrubStepFault(StrEnum):
    """What fails in one step: nothing, the installer, or daemon-reload."""

    NONE = "none"
    INSTALLER = "installer"
    RELOAD = "reload"


# The points of ScrubWorld that each fault fails, every call of them in its step.
FAILING: Final[dict[ScrubStepFault, tuple[str, ...]]] = {
    ScrubStepFault.NONE: (),
    ScrubStepFault.INSTALLER: ("drop-in text", "drop-in remove-text"),
    ScrubStepFault.RELOAD: ("systemctl daemon-reload",),
}


type Files = tuple[str | None, str | None, bool, bool]


class ScrubMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.world = ScrubWorld()
        self.core: ComponentState | None = None  # btrfs:core's state at the start

    # ---- helpers -----------------------------------------------------------------------------
    def _files(self) -> Files:
        w = self.world
        return (w.conf, w.dropin, w.conf_folder, w.dropin_folder)

    def _apply(self, limit: str | None, fault: ScrubStepFault) -> None:
        what = limit or "off"
        w = self.world
        before, loaded_before = self._files(), w.loaded
        calls_before = len(w.calls)
        for point in FAILING[fault]:
            w.fail(point, OSError(errno.EACCES, "Access denied"))
        try:
            rc, lines = w.service().apply(limit)
        finally:
            w.failing.clear()
        fired = any(c in FAILING[fault] for c in w.calls[calls_before:])
        if rc == 0:
            assert w.loaded == w.dropin, f"{what}: exit 0, systemd has not loaded the drop-in"
            assert not w.dirty, f"{what}: exit 0, systemd is behind the files: {lines}"
            assert not fired, f"{what}: {fault} fired and the run exited 0: {lines}"
            return
        assert fired, f"{what}: exit {rc} with no fault that fired ({fault}): {lines}"
        assert self._files() == before, f"{what} failed ({fault}) and changed {before} -> {self._files()}: {lines}"
        assert w.loaded == loaded_before, f"{what} failed ({fault}) and changed what systemd loaded"
        assert any(line.startswith("ERROR:") for line in lines), f"{what}: exit {rc} with no ERROR line: {lines}"
        assert not any("ROLLBACK INCOMPLETE" in line for line in lines), f"{what} ({fault}): {lines}"

    # ---- rules -------------------------------------------------------------------------------
    @initialize(conf=st.sampled_from(START_CONFS), core=st.sampled_from([None, ComponentState.DONE]))
    def start(self, conf: str | None, core: ComponentState | None) -> None:
        w = self.world
        w.put_conf(conf)
        if conf is not None:
            limit, _ = read_scrub_conf_limit(conf)
            if limit:  # as a set left it: the drop-in of that limit, loaded
                w.put_dropin(render_scrub_dropin(limit))
                w.loaded = w.dropin
        self.core = core
        if core is not None:
            w.install_core(done=True)

    @rule(limit=st.sampled_from(LIMITS), fault=st.sampled_from(ScrubStepFault))
    def set_limit(self, limit: str, fault: ScrubStepFault) -> None:
        self._apply(limit, fault)

    @rule(fault=st.sampled_from(ScrubStepFault))
    def off(self, fault: ScrubStepFault) -> None:
        self._apply(None, fault)

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def the_conf_and_the_drop_in_agree(self) -> None:
        conf_limit, _ = read_scrub_conf_limit(self.world.conf)
        dropin_limit = parse_scrub_dropin_limit(self.world.dropin)
        assert conf_limit == dropin_limit, f"snapshots.conf says {conf_limit!r}, the drop-in {dropin_limit!r}"

    @invariant()
    def btrfs_core_keeps_its_state(self) -> None:
        w = self.world
        state = w.core_state()
        if self.core is not None:
            assert state == self.core, f"btrfs:core {self.core} at the start, now {state}"
            return
        assert state in {None, ComponentState.PENDING}, f"btrfs:core had no state, now {state}"
        if state is not None:
            assert w.core_has_a_file(), f"btrfs:core {state} with no file: {w.records.texts}"


class ScrubMachineTest(profiles.MbtTestCase):
    # Not `ScrubMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(ScrubMachine)


if __name__ == "__main__":
    unittest.main()
