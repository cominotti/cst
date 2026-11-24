# SPDX-License-Identifier: MPL-2.0
"""Properties of the install record (cst_core/domain/record.py), with Hypothesis.

The unit tests (tests/test_install_record.py RecordDomainTest) check chosen
records; these generate them: what render_record writes, parse_record reads
back, and the record's pure updates (with_files, without_files, with_state)
never leave a component state that no recorded file names.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
import unittest

from hypothesis import given
from hypothesis import strategies as st

from cst_core.domain.install import Owner
from cst_core.domain.record import (
    EMPTY_RECORD,
    ComponentState,
    InstallRecord,
    RecordedFile,
    parse_record,
    render_record,
    with_files,
    with_state,
    without_files,
)
from tests.mbt import profiles

profiles.load()

words = st.text(min_size=1, max_size=12)
sha256s = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
values = st.one_of(st.none(), st.dictionaries(words, st.text(max_size=12), max_size=3))
# Not empty: the reader takes a missing or empty commit for "unknown" (a boundary of the format, not a
# bug: the installer records the checkout's commit or "unknown", never "").
commits = st.one_of(st.just("unknown"), words)


def recorded_files(component: st.SearchStrategy[str] = words) -> st.SearchStrategy[RecordedFile]:
    return st.builds(
        RecordedFile,
        src=st.one_of(st.none(), words),
        component=component,
        kind=words,
        sha256=sha256s,
        mode=st.integers(min_value=0, max_value=0o7777),
        owner=st.sampled_from(Owner),
        values=values,
        commit=commits,
        tree=st.text(max_size=12),
    )


@st.composite
def records(draw: st.DrawFn) -> InstallRecord:
    """A record as the installer keeps it: a state for each component some file names, none for others."""
    files = draw(st.dictionaries(words, recorded_files(), max_size=6))
    names = sorted({f.component for f in files.values()})
    states = {name: draw(st.sampled_from(ComponentState)) for name in names}
    return InstallRecord(files, states)


# A small pool of destinations and components. Each step draws a destination's component, so a
# destination can move to another component from one step to the next (a manifest move between
# versions, or the same generated text recorded under another --component).
DESTS: Final = ("/etc/a", "/etc/b", "/etc/c", "/usr/local/sbin/d")
COMPONENTS: Final = ("m:a", "m:b", "n:c")
dests = st.sampled_from(DESTS)


@st.composite
def recordings(draw: st.DrawFn) -> dict[str, tuple[RecordedFile, bool]]:
    """What one install records: {dest: (file, written)} for some destinations of DESTS, each under a
    component of COMPONENTS."""
    chosen = draw(st.lists(dests, unique=True, max_size=len(DESTS)))
    component = st.sampled_from(COMPONENTS)
    return {dest: (draw(recorded_files(component)), draw(st.booleans())) for dest in chosen}


@dataclass(frozen=True, slots=True)
class Record:
    """An install records these files (with_files)."""

    files: Mapping[str, tuple[RecordedFile, bool]]


@dataclass(frozen=True, slots=True)
class Drop:
    """A remove or a forget drops these destinations (without_files)."""

    dests: list[str]


@dataclass(frozen=True, slots=True)
class Mark:
    """A setup command marks a component (with_state)."""

    component: str
    state: ComponentState


type Step = Record | Drop | Mark

steps: st.SearchStrategy[Step] = st.one_of(
    st.builds(Record, recordings()),
    st.builds(Drop, st.lists(dests, unique=True)),
    st.builds(Mark, st.sampled_from(COMPONENTS), st.sampled_from(ComponentState)),
)


def _apply(rec: InstallRecord, step: Step) -> InstallRecord:
    match step:
        case Record(files):
            return with_files(rec, files)
        case Drop(gone):
            return without_files(rec, gone)
        case Mark(component, state):
            return with_state(rec, component, state)


class RecordTextTest(profiles.MbtTestCase):
    @given(records())
    def test_parse_reads_back_what_render_writes(self, rec: InstallRecord) -> None:
        self.assertEqual(parse_record(render_record(rec)), rec)

    @given(records())
    def test_render_is_a_fixed_point(self, rec: InstallRecord) -> None:
        text = render_record(rec)
        self.assertEqual(render_record(parse_record(text)), text)


class RecordUpdateTest(profiles.MbtTestCase):
    @given(st.lists(steps, max_size=12))
    def test_every_component_state_names_a_recorded_component(self, sequence: Sequence[Step]) -> None:
        rec = EMPTY_RECORD
        for step in sequence:
            rec = _apply(rec, step)
            self.assertEqual(set(rec.components), {f.component for f in rec.files.values()}, (step, rec))

    @given(
        st.lists(steps, max_size=8),
        st.dictionaries(st.sampled_from(COMPONENTS), st.sampled_from(ComponentState)),
        steps,
    )
    def test_a_write_drops_the_states_an_older_cst_left_without_a_file(
        self, sequence: Sequence[Step], orphans: Mapping[str, ComponentState], step: Step
    ) -> None:
        # An older CST's with_files() could leave a state that no recorded file names: the next update of
        # the record drops it; only a mark of a component with no file changes nothing (no write).
        rec = EMPTY_RECORD
        for earlier in sequence:
            rec = _apply(rec, earlier)
        named = {f.component for f in rec.files.values()}
        old = InstallRecord(rec.files, {**{c: s for c, s in orphans.items() if c not in named}, **rec.components})
        # The drawn step, and a mark of each component a file names (the setup command's last step).
        for update in [step, *(Mark(c, ComponentState.DONE) for c in sorted(named))]:
            new = _apply(old, update)
            if isinstance(update, Mark) and update.component not in named:
                self.assertEqual(new, old)
            else:
                self.assertEqual(set(new.components), {f.component for f in new.files.values()}, (update, new))

    @given(st.lists(steps, max_size=8), recordings())
    def test_recording_files_found_as_recorded_changes_nothing(
        self, sequence: Sequence[Step], files: dict[str, tuple[RecordedFile, bool]]
    ) -> None:
        rec = EMPTY_RECORD
        for step in sequence:
            rec = _apply(rec, step)
        rec = with_files(rec, files)
        # A second install that writes nothing and finds every file as recorded.
        again = {dest: (f, False) for dest, (f, _written) in files.items()}
        self.assertEqual(with_files(rec, again), rec)


if __name__ == "__main__":
    unittest.main()
