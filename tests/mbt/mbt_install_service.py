# SPDX-License-Identifier: MPL-2.0
"""A state machine over cst_core's InstallService (cst_core/application/install.py), with Hypothesis.

It drives the real service over the unit tests' in-memory target and
record (tests/test_install.py MemoryTarget, tests/test_install_record.py
MemoryRecord) through any sequence of plan, install, put, mark, remove and
forget, with failed writes, files edited or deleted behind CST's back, a
checkout whose payloads change, and a record written by a newer CST. After
every step it checks the record against the disk; each step checks what
the call itself promises (plan changes nothing and predicts install; a
refusal changes nothing, a remove's too).
"""

from collections.abc import Sequence
import hashlib
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from cst_core.application.install import InstallRefused, InstallRequest, InstallService, Placement
from cst_core.domain.contracts import StateFormatKey, state_format
from cst_core.domain.install import ArtifactKind, InstallEntry, Owner
from cst_core.domain.record import ROOT_RECORD, USER_RECORD_REL, ComponentState, parse_record
from cst_core.ports import WriteFailed
from tests.mbt import profiles
from tests.test_install import MemoryTarget, entry
from tests.test_install_record import MemoryRecord

profiles.load()

HOME = "/var/home/u"
USER_RECORD = f"{HOME}/{USER_RECORD_REL}"
# One format above the one this CST reads: a newer CST wrote it, whatever the current format is.
NEWER_RECORD = (
    f'{{"format": {state_format(StateFormatKey.INSTALL_RECORD).version + 1}, "files": {{}}, "components": {{}}}}\n'
)

ENTRIES: tuple[InstallEntry, ...] = (
    entry("m/a", "/usr/local/sbin/a", mode=0o755, kind=ArtifactKind.SHELL),
    entry("m/d", "/etc/m/d"),
    entry("m/s.service", "/etc/systemd/system/s.service", kind=ArtifactKind.UNIT),
    entry("n/b", "/usr/local/sbin/b", mode=0o755, kind=ArtifactKind.SHELL),
    entry("n/t.in", "/etc/n/t"),
    entry("n/u", "~/.config/u", owner=Owner.USER),
)
COMPONENTS = sorted({e.component for e in ENTRIES})

subsets = st.lists(st.sampled_from(ENTRIES), min_size=1, unique_by=lambda e: e.src)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InstallMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.payloads = {e.src: f"{e.src} v0\n".encode() for e in ENTRIES}
        self.payloads["n/t.in"] = b"uuid=@UUID@ v0\n"
        self.uuid = "u-0"
        self.target = MemoryTarget()
        self.record = MemoryRecord()
        self.svc = InstallService(self.target, lambda e: self.payloads[e.src], record=self.record, commit="c0")
        # Destinations changed behind CST's back since CST last recorded them: their record lines may lie.
        self.tainted: set[str] = set()
        self.newer = False
        self.saved: str | None = None  # the root record a newer CST replaced

    # ---- helpers -----------------------------------------------------------------------------
    def _req(self, entries: Sequence[InstallEntry]) -> InstallRequest:
        values = {"UUID": self.uuid} if any(e.is_template for e in entries) else None
        return InstallRequest(home=HOME, values=values)

    def _dest(self, e: InstallEntry) -> str:
        return self.svc.destination(e, InstallRequest(home=HOME))

    def _state(self) -> tuple[dict[str, tuple[bytes, int]], dict[str, str]]:
        return dict(self.target.files), dict(self.record.texts)

    def _records_of(self, entries: Sequence[InstallEntry]) -> set[str]:
        return {USER_RECORD if e.owner == Owner.USER else ROOT_RECORD for e in entries}

    def _refused(self, entries: Sequence[InstallEntry]) -> bool:
        """A call that reads the root record refuses while a newer CST wrote it."""
        return self.newer and ROOT_RECORD in self._records_of(entries)

    # ---- rules -------------------------------------------------------------------------------
    @rule(entries=subsets, fail=st.one_of(st.none(), st.sampled_from(ENTRIES)))
    def install(self, entries: list[InstallEntry], fail: InstallEntry | None) -> None:
        req = self._req(entries)
        before = self._state()
        try:
            lines, _ = self.svc.plan(entries, req)
        except InstallRefused:
            assert self._refused(entries), "plan refused without a newer record"
            lines = []
        assert self._state() == before, "plan changed the disk or the record"
        would_write = {line.split(None, 2)[-1] for line in lines if line.startswith("would write:")}
        self.target.fail_on = self._dest(fail) if fail is not None else None
        writes_before = len(self.target.writes)
        try:
            list(self.svc.install(entries, req))
        except InstallRefused:
            assert self._refused(entries), "install refused without a newer record"
            assert self._state() == before, "a refused install changed something"
            return
        except WriteFailed:
            assert fail is not None and fail in entries, "a write failed that was not made to fail"
            return
        finally:
            self.target.fail_on = None
        assert not self._refused(entries), "install ran over a record of a newer CST"
        written = self.target.writes[writes_before:]
        assert set(written) == would_write, f"plan said {sorted(would_write)}, install wrote {written}"
        kinds = [next(e.kind for e in entries if self._dest(e) == w) for w in written]
        assert kinds == sorted(kinds, key=lambda k: k == ArtifactKind.UNIT), f"a unit before a program: {kinds}"
        for e in entries:
            self.tainted.discard(self._dest(e))
        again, n = self.svc.plan(entries, req)
        assert n == 0 and all(line.startswith("unchanged:") for line in again), f"not converged: {again}"

    @rule(e=st.sampled_from(ENTRIES))
    def put(self, e: InstallEntry) -> None:
        req = self._req([e])
        place = self.svc.placement(e, req, req.values)
        before = self._state()
        try:
            self.svc.put(place, self.svc.desired(e, req.values), as_root=False)
        except InstallRefused:
            assert self._refused([e]), "put refused without a newer record"
            assert self._state() == before, "a refused put changed something"
            return
        self.tainted.discard(place.dest)

    @rule(component=st.sampled_from(COMPONENTS), user=st.booleans())
    def mark(self, *, component: str, user: bool) -> None:
        record = USER_RECORD if user else ROOT_RECORD
        before = self._state()
        try:
            self.svc.mark(record, component)
        except InstallRefused:
            assert self.newer and record == ROOT_RECORD, "mark refused without a newer record"
            assert self._state() == before, "a refused mark changed something"
            return
        if record in self.record.texts:
            rec = parse_record(self.record.texts[record])
            if any(f.component == component for f in rec.files.values()):
                assert rec.components[component] == ComponentState.DONE, "mark did not mark"

    @rule(entries=subsets, fail=st.one_of(st.none(), st.sampled_from(ENTRIES)))
    def remove(self, entries: list[InstallEntry], fail: InstallEntry | None) -> None:
        places: list[Placement] = [self.svc.placement(e, InstallRequest(home=HOME)) for e in entries]
        before = self._state()
        self.target.fail_on = self._dest(fail) if fail is not None else None
        try:
            list(self.svc.remove(places))
        except InstallRefused:
            assert self._refused(entries), "remove refused without a newer record"
            assert self._state() == before, "a refused remove changed something"
            return
        except WriteFailed:
            assert fail is not None and fail in entries, "a removal failed that was not made to fail"
            return
        finally:
            self.target.fail_on = None
        for p in places:
            assert p.path not in self.target.files, f"{p.path} still there"
            self.tainted.discard(p.dest)

    @rule(entries=subsets)
    def forget(self, entries: list[InstallEntry]) -> None:
        try:
            for record in sorted(self._records_of(entries)):
                dests = [self._dest(e) for e in entries if record in self._records_of([e])]
                self.svc.forget(record, dests)
        except InstallRefused:
            assert self._refused(entries), "forget refused without a newer record"

    @rule(e=st.sampled_from(ENTRIES), delete=st.booleans())
    def change_behind_csts_back(self, *, e: InstallEntry, delete: bool) -> None:
        dest = self._dest(e)
        if delete:
            self.target.files.pop(dest, None)
        else:
            data, mode = self.target.files.get(dest, (b"", 0o644))
            self.target.files[dest] = (data + b"# edited\n", mode)
        self.tainted.add(dest)

    @rule(e=st.sampled_from(ENTRIES))
    def checkout_changes(self, e: InstallEntry) -> None:
        if e.is_template:
            self.uuid += "+"
        else:
            self.payloads[e.src] += b"more\n"

    @precondition(lambda self: not self.newer)
    @rule()
    def a_newer_cst_writes_the_record(self) -> None:
        self.saved = self.record.texts.get(ROOT_RECORD)
        self.record.texts[ROOT_RECORD] = NEWER_RECORD
        self.newer = True

    @precondition(lambda self: self.newer)
    @rule()
    def the_newer_record_goes_away(self) -> None:
        if self.saved is None:
            self.record.texts.pop(ROOT_RECORD, None)
        else:
            self.record.texts[ROOT_RECORD] = self.saved
        self.newer = False

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def every_component_state_has_files_and_every_file_a_state(self) -> None:
        for path, text in self.record.texts.items():
            if self.newer and path == ROOT_RECORD:
                continue
            rec = parse_record(text)
            assert set(rec.components) == {f.component for f in rec.files.values()}, f"{path}: {rec}"

    @invariant()
    def the_record_tells_the_truth_about_untouched_files(self) -> None:
        for path, text in self.record.texts.items():
            if self.newer and path == ROOT_RECORD:
                continue
            for dest, f in parse_record(text).files.items():
                if dest in self.tainted:
                    continue
                assert dest in self.target.files, f"{path} lists {dest}, which is not there"
                data, mode = self.target.files[dest]
                assert (_sha(data), mode) == (f.sha256, f.mode), f"{path}: {dest} is not what it lists"


class InstallMachineTest(profiles.MbtTestCase):
    # Not `InstallMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(InstallMachine)


if __name__ == "__main__":
    unittest.main()
