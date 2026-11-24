# SPDX-License-Identifier: MPL-2.0
"""Properties of btrfs/snapshot/domain.py (retention plan, safeguards, round-trips), with Hypothesis.

The example tests (btrfs/tests/test_btrfs_snapshot_core.py) pin btrbk's
decisions on chosen timelines and a fixed list of policies; these generate
the policies, zones, timelines and clocks, and check what must hold for all
of them. The decision words ("keep") stay raw, as in the unit tests.
"""

from collections.abc import Sequence
import datetime as dt
import unittest
import zoneinfo

from hypothesis import given
from hypothesis import strategies as st

from btrfs.snapshot import domain as core
from cst_core.domain.values import DAY, HOUR
from tests.mbt import profiles

profiles.load()

# Zones with and without DST, a southern one, a half-hour one and one with a 30-minute DST shift.
ZONES = ("UTC", "Europe/Berlin", "America/New_York", "Australia/Sydney", "Asia/Kolkata", "Australia/Lord_Howe")
LOW = dt.datetime(2024, 1, 1)
HIGH = dt.datetime(2028, 12, 31)
LAST_EPOCH = 253_402_128_000  # 9999-12-29 00:00 UTC

counts = st.integers(min_value=0, max_value=120)
policies = st.dictionaries(st.sampled_from(core.POLICY_UNITS), st.one_of(counts, st.just("all")), min_size=1)
# Policies with no "all" unit, the ones policy_max_kept bounds: an "all" half the time would throw most away.
bounded_policies = st.dictionaries(st.sampled_from(core.POLICY_UNITS), counts, min_size=1)
zones = st.sampled_from(ZONES).map(zoneinfo.ZoneInfo)
minutes = st.datetimes(min_value=LOW, max_value=HIGH).map(lambda w: w.replace(second=0, microsecond=0))


@st.composite
def timelines(draw: st.DrawFn, max_size: int = 40) -> list[core.Snap]:
    """Snapshots of one source: distinct names, any order, several in one minute (the _N suffix)."""
    keys = draw(st.lists(st.tuples(minutes, st.integers(min_value=0, max_value=2)), max_size=max_size, unique=True))
    return [core.Snap(core.snapshot_name("home", w, seq), w, seq) for w, seq in keys]


def newest_epoch(snaps: Sequence[core.Snap], tz: dt.tzinfo) -> int:
    return max((core.wall_to_epoch(s.wall, tz) for s in snaps), default=core.wall_to_epoch(LOW, tz))


def kept(decisions: Sequence[core.Decision]) -> set[str]:
    return {d.snap.entry for d in decisions if d.action == "keep"}


class PolicyTextTest(profiles.MbtTestCase):
    @given(policies)
    def test_format_then_parse_gives_the_policy_back(self, policy: dict[str, int | str]) -> None:
        self.assertEqual(core.parse_policy(core.format_policy(policy)), policy)

    @given(policy=policies, sep=st.sampled_from([" ", ",", " , ", "\t"]), upper=st.booleans())
    def test_parse_takes_any_separator_and_case(self, *, policy: dict[str, int | str], sep: str, upper: bool) -> None:
        text = sep.join(core.format_policy(policy).split(" "))
        self.assertEqual(core.parse_policy(text.upper() if upper else text), policy)


class NameTest(profiles.MbtTestCase):
    @given(st.from_regex(core.NAME_RE, fullmatch=True), minutes, st.integers(min_value=0, max_value=10**6))
    def test_a_snapshot_name_parses_back(self, name: str, wall: dt.datetime, seq: int) -> None:
        self.assertEqual(core.parse_snapshot_name(name, core.snapshot_name(name, wall, seq)), (wall, seq))

    @given(st.integers(min_value=0, max_value=2**40), st.uuids(), st.floats(min_value=0, max_value=10**9))
    def test_the_state_line_parses_back(self, epoch: int, boot: object, boottime: float) -> None:
        line = f"{epoch} {boot} {boottime:.3f}"
        self.assertEqual(core.parse_state(line), core.LastRun(epoch, str(boot), float(f"{boottime:.3f}")))

    # Up to the last day of year 9999: datetime cannot hold a later wall time (fromtimestamp raises).
    @given(st.integers(min_value=0, max_value=LAST_EPOCH), zones)
    def test_wall_time_round_trips_through_the_epoch(self, epoch: int, tz: zoneinfo.ZoneInfo) -> None:
        wall = core.epoch_to_wall(epoch, tz)
        self.assertEqual(core.epoch_to_wall(core.wall_to_epoch(wall, tz), tz), wall)


class PlanTest(profiles.MbtTestCase):
    @given(timelines(), policies, zones, st.integers(min_value=-6 * HOUR, max_value=400 * DAY))
    def test_every_snapshot_gets_one_decision_oldest_first_and_the_latest_is_kept(
        self, snaps: list[core.Snap], policy: dict[str, int | str], tz: zoneinfo.ZoneInfo, ahead: int
    ) -> None:
        now = newest_epoch(snaps, tz) + ahead
        rows = core.plan(snaps, now, policy, tz)
        self.assertEqual(sorted(r.snap.entry for r in rows), sorted(s.entry for s in snaps))
        keys = [(core.wall_to_epoch(r.snap.wall, tz), r.snap.seq) for r in rows]
        self.assertEqual(keys, sorted(keys))
        if rows:
            self.assertEqual(rows[-1].action, "keep")
        for r in rows:
            if core.wall_to_epoch(r.snap.wall, tz) > now + HOUR:
                self.assertEqual(r.action, "keep", "a future-dated snapshot is never deleted")

    @given(timelines(), policies, policies, zones, st.integers(min_value=0, max_value=400 * DAY))
    def test_a_larger_policy_keeps_a_superset(
        self,
        snaps: list[core.Snap],
        small: dict[str, int | str],
        extra: dict[str, int | str],
        tz: zoneinfo.ZoneInfo,
        ahead: int,
    ) -> None:
        def bigger(u: str) -> int | str:
            a, b = small.get(u, 0), extra.get(u, 0)
            if a == "all" or b == "all":
                return "all"
            return max(int(a), int(b))

        large = {u: bigger(u) for u in core.POLICY_UNITS if u in small or u in extra}
        now = newest_epoch(snaps, tz) + ahead
        self.assertLessEqual(kept(core.plan(snaps, now, small, tz)), kept(core.plan(snaps, now, large, tz)))

    @given(timelines(), bounded_policies, zones, st.integers(min_value=0, max_value=400 * DAY))
    def test_the_kept_count_stays_within_policy_max_kept(
        self, snaps: list[core.Snap], policy: dict[str, int | str], tz: zoneinfo.ZoneInfo, ahead: int
    ) -> None:
        limit = core.policy_max_kept(policy)
        self.assertIsNotNone(limit)
        assert limit is not None  # for ty: assertIsNotNone does not narrow
        now = newest_epoch(snaps, tz) + ahead
        rows = core.plan(snaps, now, policy, tz)
        self.assertLessEqual(len(kept(rows)), limit)

    @given(timelines(), policies, zones, st.integers(min_value=0, max_value=400 * DAY), st.data())
    def test_pruning_twice_at_the_same_time_deletes_nothing_more(
        self,
        snaps: list[core.Snap],
        policy: dict[str, int | str],
        tz: zoneinfo.ZoneInfo,
        ahead: int,
        data: st.DataObject,
    ) -> None:
        now = newest_epoch(snaps, tz) + ahead
        paths = [s.entry for s in snaps]
        pinned = set(data.draw(st.lists(st.sampled_from(paths), unique=True))) if paths else set[str]()
        snaps = [core.Snap(s.entry, s.wall, s.seq, path=s.entry, cgen=i) for i, s in enumerate(snaps)]
        first = core.apply_safeguards(core.plan(snaps, now, policy, tz), now, tz, pinned)
        left = [d.snap for d in first if d.action == "keep"]
        again = core.apply_safeguards(core.plan(left, now, policy, tz), now, tz, pinned)
        self.assertEqual([d.action for d in again], ["keep"] * len(left))


class SafeguardTest(profiles.MbtTestCase):
    @given(timelines(), policies, zones, st.integers(min_value=0, max_value=400 * DAY), st.data())
    def test_safeguards_only_turn_deletions_into_keeps(
        self,
        snaps: list[core.Snap],
        policy: dict[str, int | str],
        tz: zoneinfo.ZoneInfo,
        ahead: int,
        data: st.DataObject,
    ) -> None:
        now = newest_epoch(snaps, tz) + ahead
        cgens = data.draw(st.permutations(range(len(snaps))))
        snaps = [core.Snap(s.entry, s.wall, s.seq, path=s.entry, cgen=g) for s, g in zip(snaps, cgens, strict=True)]
        pinned = set(data.draw(st.lists(st.sampled_from([s.entry for s in snaps]), unique=True))) if snaps else set()
        rows = core.plan(snaps, now, policy, tz)
        out = core.apply_safeguards(rows, now, tz, pinned)
        self.assertEqual([d.snap.entry for d in out], [d.snap.entry for d in rows])
        self.assertLessEqual(kept(rows), kept(out))
        by_gen = sorted(snaps, key=lambda s: s.cgen, reverse=True)[: core.KEEP_NEWEST]
        by_name = sorted(snaps, key=lambda s: (core.wall_to_epoch(s.wall, tz), s.seq), reverse=True)
        must = {s.entry for s in by_gen + by_name[: core.KEEP_NEWEST]} | pinned
        must |= {s.entry for s in snaps if now - core.wall_to_epoch(s.wall, tz) < core.MIN_AGE}
        self.assertLessEqual(must, kept(out))
        for d in out:
            if d.snap.entry in kept(out) - kept(rows):
                self.assertTrue(d.reason.startswith("safeguard: "), d.reason)

    @given(st.integers(min_value=0, max_value=10**6), st.integers(min_value=0, max_value=10**6))
    def test_the_breaker_trips_exactly_above_its_limit(self, count: int, owned: int) -> None:
        self.assertEqual(core.is_mass_delete(count, owned), count > core.mass_delete_limit(owned))


class ShowTest(profiles.MbtTestCase):
    @given(st.uuids(), st.one_of(st.none(), st.uuids()), st.integers(min_value=0), st.integers(min_value=0))
    def test_subvolume_show_parses_what_btrfs_prints(
        self, uuid: object, parent: object | None, gen: int, cgen: int
    ) -> None:
        text = (
            "home\n"
            f"\tName: \t\t\thome\n\tUUID: \t\t\t{uuid}\n\tParent UUID: \t\t{parent or '-'}\n"
            f"\tGeneration: \t\t{gen}\n\tGen at creation: \t{cgen}\n\tSnapshot(s):\n\t\t\t\tx\n"
        )
        want = {"uuid": str(uuid), "parent_uuid": None if parent is None else str(parent), "generation": gen}
        self.assertEqual(core.parse_subvolume_show(text), {**want, "cgen": cgen})


if __name__ == "__main__":
    unittest.main()
