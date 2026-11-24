# SPDX-License-Identifier: MPL-2.0
"""A state machine over btrfs-bulk's classify, apply and recover, with faults at every step (Hypothesis).

It runs the program over the unit tests' scratch home (btrfs/tests/bulk_fakes.py BulkHome: the
real moves, cp included, with fakes only for subvolumes, +C, the busy check, flatpak and
btrfs-snapshot's convert), with os.fsync stubbed (the TestCase's setUp) and the user adapter's
0.05 s sleeps replaced by a wait for the next ctime tick (bulk_fakes.use_tick_clock). Four items:
apps (~/Programs, a plain move), jetbrains (inside apps' folder, `after` apps), avd (NOCOW: a
real copy into +C, or the in-place rewrite) and iso (an old subvolume renamed in); the others only
get their folders. The calls by which a run changes the home or the state files are points of a
FaultPlan (bulk_fakes.arm_points, each call atomic), dead after a kill.

The start: ~/Bulk absent, or in place (an empty subvolume and its CACHEDIR.TAG); every link's
folder there; each item absent, a folder with data, an empty folder or in place (its link, its
data in ~/Bulk), iso also an old subvolume, avd also in place with copy-on-write files, and
both also a folder with data while other data is in ~/Bulk (jetbrains in place only with apps
in place, ~/Bulk in place for any of these); a keep set.

Rules: apply and recover (the boot's first step), each with a kill, a failure or a full disk at
one of its first 20 points (a point past the run's end: no fault); an app's write into an item's
folder (real or through its link; where the path is absent the app makes it, and while the
item's interrupted move waits in its journal the model marks the item stuck); busy and idle;
keep and unkeep; settle.

After every step:
- no data lost: every file the model holds for an item (its latest write) is at the item's path,
  or, while its journal exists, at the journal's old, new or target path;
- no data parked without a record: each `.bulk-old-` tree is the old path of a journal;
- classify agrees with the disk: an oracle over lstat and the fake sets names each item's state
  (kept, wait, unexpected, link, mkdir, move, move-subvol, nocow, ok);
- an apply or recover leaves a kept item's paths (with no journal) as they were.

settle: every item idle, apply with no fault; then each item is ok, kept, waits on a kept item
or (jetbrains with data while apps is kept, an item with data on both sides) needs a look, except the stuck ones, whose journal
stays and whose apply line names them "created again"; and an apply and a recover change no file.

Not modelled: calls torn half-way (the fault module, btrfs/tests/test_btrfs_bulk_faults.py, has
them, and the one known gap there), writes during a call (the switch instant: the fault
module's LateWrite and Conflict), Steam's journals, the child processes of as_user, real btrfs
and +C, flatpak, the boot counter.
"""

from collections.abc import Mapping
from enum import StrEnum
import os
import stat
from typing import Final, override
import unittest

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from btrfs.bulk.application import items as bulk_items
from btrfs.bulk.domain import BY_ID
from btrfs.tests.bulk_fakes import (
    OLD_MARK,
    BulkHome,
    TreeEntry,
    arm_points,
    data_places,
    inode,
    read_or_none,
    run_armed,
    tick_wait,
    use_tick_clock,
)
from tests.fault_injection import Fault, FaultMode, FaultPlan, stub_fsync
from tests.mbt import profiles

profiles.load()

ITEM_IDS: Final = ("apps", "jetbrains", "avd", "iso")
MAX_POINT: Final = 20


class BulkStartShape(StrEnum):
    """An item's shape at the start."""

    ABSENT = "absent"
    DATA = "data"  # a real folder with data
    EMPTY = "empty"  # a real empty folder
    IN_PLACE = "in-place"  # the link, the data in ~/Bulk
    SUBVOLUME = "subvolume"  # iso: an old subvolume with data
    IN_PLACE_COW = "in-place-cow"  # avd: in place, its files copy-on-write
    BOTH = "both"  # avd, iso: a real folder with data and other data in ~/Bulk (copied by hand)


# The shapes that leave a move to make come first, and twice as often: a start with nothing to
# move leaves the faults nothing to cut.
SHAPES: Final[Mapping[str, tuple[BulkStartShape, ...]]] = {
    "apps": (
        BulkStartShape.DATA,
        BulkStartShape.DATA,
        BulkStartShape.ABSENT,
        BulkStartShape.EMPTY,
        BulkStartShape.IN_PLACE,
    ),
    "jetbrains": (
        BulkStartShape.DATA,
        BulkStartShape.DATA,
        BulkStartShape.ABSENT,
        BulkStartShape.EMPTY,
        BulkStartShape.IN_PLACE,
    ),
    "avd": (
        BulkStartShape.DATA,
        BulkStartShape.IN_PLACE_COW,
        BulkStartShape.DATA,
        BulkStartShape.IN_PLACE_COW,
        BulkStartShape.ABSENT,
        BulkStartShape.EMPTY,
        BulkStartShape.IN_PLACE,
        BulkStartShape.BOTH,
    ),
    "iso": (
        BulkStartShape.SUBVOLUME,
        BulkStartShape.DATA,
        BulkStartShape.SUBVOLUME,
        BulkStartShape.DATA,
        BulkStartShape.ABSENT,
        BulkStartShape.EMPTY,
        BulkStartShape.IN_PLACE,
        BulkStartShape.BOTH,
    ),
}
# What the start puts in an item's folder (avd: files only, no subfolder).
START_FILES: Final[Mapping[str, tuple[str, ...]]] = {
    "apps": ("a", "sub/b"),
    "jetbrains": ("ide",),
    "avd": ("disk.img",),
    "iso": ("f.iso", "sub/g"),
}
# The states an item may be left in by settle (unexpected also for jetbrains while apps is kept,
# and for an item that started with data on both sides).
SETTLED: Final = frozenset({"ok", "kept", "wait"})

# A fault at one of the first MAX_POINT points; a point past the run's end is a run with no fault
# (most runs after the first have few points: a fault on every rule keeps the kills frequent).
points = st.sampled_from(range(1, MAX_POINT + 1))  # each point as likely
item_ids = st.sampled_from(ITEM_IDS)


@settings(stateful_step_count=40)
class BulkItemsMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.fx = BulkHome()
        self.plan = FaultPlan(dead_after_kill=True)
        self.model: dict[str, dict[str, str]] = {i: {} for i in ITEM_IDS}
        self.kept: set[str] = set()
        self.stuck: set[str] = set()
        self.both: set[str] = set()
        self.writes = 0

    @override
    def teardown(self) -> None:
        self.fx.close()

    # ---- paths and the fakes ------------------------------------------------------------------
    def link(self, ident: str) -> str:
        link = self.fx.ctx.link(BY_ID[ident])
        assert link is not None
        return link

    def target(self, ident: str) -> str:
        return self.fx.ctx.target(BY_ID[ident])

    def fresh(self, path: str, *, nocow: bool) -> None:
        """A file or folder an app or the start just made: +C as btrfs gives it (inherited from its
        folder), not what a recycled inode number had in the fake sets."""
        self.fx.forget(path)
        if nocow:
            self.fx.nocow.add(inode(path))

    def put_file(self, top: str, rel: str, data: str, *, nocow: bool) -> None:
        p = os.path.join(top, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
        self.fresh(p, nocow=nocow)

    # ---- the start ----------------------------------------------------------------------------
    @initialize(
        bulk=st.booleans(),
        shapes=st.tuples(*(st.sampled_from(SHAPES[i]) for i in ITEM_IDS)),
        keep=st.sets(item_ids, max_size=1),
    )
    def start(self, *, bulk: bool, shapes: tuple[BulkStartShape, ...], keep: set[str]) -> None:
        fx = self.fx
        use_tick_clock(fx)
        for ident in ITEM_IDS:
            os.makedirs(os.path.dirname(self.link(ident)), exist_ok=True)
        if bulk:
            fx.machine.subvol_create(fx.bulk_path())
            fx.user.u_write_tag(fx.bulk_path())
        chosen = dict(zip(ITEM_IDS, shapes, strict=True))
        for ident in ITEM_IDS:
            shape = chosen[ident]
            in_place = shape in {BulkStartShape.IN_PLACE, BulkStartShape.IN_PLACE_COW}
            if in_place and (not bulk or (ident == "jetbrains" and chosen["apps"] != BulkStartShape.IN_PLACE)):
                shape = BulkStartShape.ABSENT
            if shape == BulkStartShape.BOTH and not bulk:
                shape = BulkStartShape.DATA
            chosen[ident] = shape
            self.make(ident, shape)
        if keep:
            fx.state.edit_keep(sorted(keep), [])
        self.kept = set(keep)
        tick_wait(fx.tmp)
        arm_points(fx, self.plan, torn_rmtree=False)

    def make(self, ident: str, shape: BulkStartShape) -> None:
        item, link = BY_ID[ident], self.link(ident)
        files = {rel: f"{ident}:{rel}" for rel in START_FILES[ident]}
        match shape:
            case BulkStartShape.ABSENT:
                return
            case BulkStartShape.EMPTY:
                os.mkdir(link)
                self.fresh(link, nocow=False)
                return
            case BulkStartShape.BOTH:
                elsewhere = self.fx.user.u_mkdirs(self.fx.ctx.bulk, item.target, item.nocow)
                self.put_file(elsewhere, "copied", f"{ident}:copied", nocow=item.nocow)
                self.both.add(ident)
                os.mkdir(link)
                self.fresh(link, nocow=False)
                top, nocow = link, False
            case BulkStartShape.DATA | BulkStartShape.SUBVOLUME:
                os.mkdir(link)
                self.fresh(link, nocow=False)
                if shape == BulkStartShape.SUBVOLUME:
                    self.fx.subvols.add(inode(link))
                top, nocow = link, False
            case BulkStartShape.IN_PLACE | BulkStartShape.IN_PLACE_COW:
                top = self.fx.user.u_mkdirs(self.fx.ctx.bulk, item.target, item.nocow)
                os.symlink(top, link)
                nocow = shape == BulkStartShape.IN_PLACE and item.nocow
        for rel, data in files.items():
            self.put_file(top, rel, data, nocow=nocow)
        self.model[ident] = files

    # ---- rules --------------------------------------------------------------------------------
    def kept_paths(self) -> dict[str, tuple[TreeEntry, ...]]:
        """The paths of each kept item with no journal, as they are."""
        return {i: self.shape_of(i) for i in self.kept if not self.fx.state.jexists(i)}

    def assert_kept_unchanged(self, before: Mapping[str, tuple[TreeEntry, ...]], what: str) -> None:
        """Each item of before (kept, no journal) still has no journal and the same paths. A kept
        item whose interrupted move the run finished or rolled back is not in before."""
        after = {i: None if self.fx.state.jexists(i) else self.shape_of(i) for i in before}
        assert after == before, f"{what} changed a kept item"

    def shape_of(self, ident: str) -> tuple[TreeEntry, ...]:
        """The item's path and target as they are, without what another item keeps inside them
        (jetbrains' folder, its copy and its stamp probe in ~/Bulk/apps)."""
        others = [self.target(i) for i in ITEM_IDS if i != ident]

        def own(path: str) -> bool:
            return not any(path == t or path.startswith((t + "/", t + ".")) for t in others)

        out: list[TreeEntry] = []
        for top in (self.link(ident), self.target(ident)):
            if os.path.islink(top):
                out.append((top, "link", os.readlink(top)))
                continue
            for root, dirs, names in os.walk(top):
                dirs[:] = [d for d in dirs if own(os.path.join(root, d))]
                out.extend((os.path.join(root, d), "dir", 0) for d in dirs)
                for n in names:
                    p = os.path.join(root, n)
                    if own(p):
                        with open(p) as f:
                            out.append((p, "file", f.read()))
        return tuple(sorted(out))

    def run_apply(self, fault: Fault) -> None:
        kept = self.kept_paths()
        run_armed(self.plan, lambda: bulk_items.apply(self.fx.bulk), fault)
        self.assert_kept_unchanged(kept, f"apply ({fault})")

    # One rule per mode: Hypothesis turns a random subset of the rules off in each example (swarm
    # testing), and three rules keep an apply in most of them.
    @rule(k=points)
    def apply_killed(self, k: int) -> None:
        self.run_apply((FaultMode.KILL, k))

    @rule(k=points)
    def apply_failing(self, k: int) -> None:
        self.run_apply((FaultMode.FAIL, k))

    @rule(k=points)
    def apply_disk_full(self, k: int) -> None:
        self.run_apply((FaultMode.FAIL_FROM, k))

    def run_recover(self, fault: Fault) -> None:
        kept = self.kept_paths()
        run_armed(self.plan, lambda: bulk_items.recover(self.fx.bulk), fault)
        self.assert_kept_unchanged(kept, f"recover ({fault})")

    # The same for recover: a recover right after a killed apply is the boot's first step, which
    # no later apply's own recover-first stands in for (apply goes on to the item's steps).
    @rule(k=points)
    def recover_killed(self, k: int) -> None:
        self.run_recover((FaultMode.KILL, k))

    @rule(k=points)
    def recover_failing(self, k: int) -> None:
        self.run_recover((FaultMode.FAIL, k))

    @rule(k=points)
    def recover_disk_full(self, k: int) -> None:
        self.run_recover((FaultMode.FAIL_FROM, k))

    @rule(ident=item_ids, name=st.sampled_from(("n1", "n2")))
    def app_write(self, ident: str, name: str) -> None:
        """An app writes a file into the item's folder, real or through its link; where the path is
        absent, the app makes the folder first. While the item's data waits in an interrupted move
        (its journal), that path made again is a conflict only a person can settle: the model
        marks the item stuck."""
        top = self.link(ident)
        if os.path.islink(top) and not os.path.isdir(top):
            return  # a link to a folder not there (yet): the write fails
        if not os.path.lexists(top):
            os.mkdir(top)
            self.fresh(top, nocow=False)
            if self.fx.item_journal(ident) is not None:
                self.stuck.add(ident)
        self.writes += 1
        data = f"{ident}:{name}:{self.writes}"
        self.put_file(top, name, data, nocow=self.fx.is_nocow(os.path.realpath(top)))
        self.model[ident][name] = data

    @rule(ident=item_ids, on=st.booleans())
    def busy(self, ident: str, *, on: bool) -> None:
        """The item's path in use by a process (on), or no longer."""
        if on:
            self.fx.busy.add(self.link(ident))
        else:
            self.fx.busy.discard(self.link(ident))

    @rule(ident=item_ids, on=st.booleans())
    def keep(self, ident: str, *, on: bool) -> None:
        """bulk keep (on) or bulk unkeep of the item: its line in the keep file."""
        if on:
            self.fx.state.edit_keep([ident], [])
            self.kept.add(ident)
        else:
            self.fx.state.edit_keep([], [ident])
            self.kept.discard(ident)

    @rule()
    def settle(self) -> None:
        fx = self.fx
        fx.busy.clear()
        self.plan.arm()
        fx.apply()
        out = fx.out
        for ident in ITEM_IDS:
            if ident in self.stuck:
                assert self.fx.item_journal(ident) is not None, f"{ident}: stuck, but its journal is gone: {out}"
                assert f"NEEDS A LOOK: {ident}: an interrupted move" in out, out
                assert "created again" in out, out
                continue
            assert self.fx.item_journal(ident) is None, f"{ident}: journal left: {out}"
            state = self.oracle(ident)
            after = BY_ID[ident].after
            waits = after is not None and after in self.kept
            allowed = SETTLED | ({"unexpected"} if waits or ident in self.both else set())
            assert state in allowed, f"{ident}: {state} after settle: {out}"
        before = fx.tree()
        fx.apply()
        again = fx.out
        run_armed(self.plan, lambda: bulk_items.recover(fx.bulk))
        assert fx.tree() == before, "a further apply or recover changed files"
        assert "done:" not in again, again

    # ---- the oracle ---------------------------------------------------------------------------
    def cow(self, ident: str) -> bool:
        """nocow_check's answer for a NOCOW item whose target is a folder: something lacks +C."""
        top = self.target(ident)
        for root, _dirs, names in os.walk(top):
            if not self.fx.is_nocow(root):
                return True
            for n in names:
                p = os.path.join(root, n)
                st_ = os.lstat(p)
                if stat.S_ISREG(st_.st_mode) and st_.st_size and not self.fx.is_nocow(p):
                    return True
        parent = self.fx.ctx.bulk
        for part in os.path.dirname(BY_ID[ident].target).split("/"):
            parent = os.path.join(parent, part)
            if not os.path.isdir(parent) or os.path.islink(parent):
                break
            if not self.fx.is_nocow(parent):
                return True
        return False

    def oracle(self, ident: str) -> str:
        """The state classify must give the item, from the disk and the fakes alone."""
        item, link, target = BY_ID[ident], self.link(ident), self.target(ident)
        if ident in self.kept:
            return "kept"
        if item.after is not None and item.after in self.kept:
            real = os.path.isdir(link) and not os.path.islink(link)
            data = real and not self.fx.is_subvol(link) and bool(os.listdir(link))
            return "unexpected" if data else "wait"
        if not os.path.lexists(link):
            return "link"
        has_target = os.path.isdir(target) and not os.path.islink(target)
        if os.path.islink(link):
            assert os.readlink(link) == target, f"{ident}: a link the machine never makes"
            if not has_target:
                return "mkdir"
            return "nocow" if item.nocow and self.cow(ident) else "ok"
        assert os.path.isdir(link), f"{ident}: a file the machine never makes"
        return self.folder_oracle(ident, target_data=has_target and bool(os.listdir(target)))

    def folder_oracle(self, ident: str, *, target_data: bool) -> str:
        """The oracle for an item whose path is a real folder."""
        link = self.link(ident)
        if not self.fx.is_subvol(link) and not os.listdir(link):
            return "move"
        if target_data:
            return "unexpected"  # data on both sides (none of these items is disposable)
        if self.fx.is_subvol(link):
            return "move" if BY_ID[ident].nocow else "move-subvol"
        return "move"

    # ---- invariants ---------------------------------------------------------------------------
    @invariant()
    def no_data_lost(self) -> None:
        for ident in ITEM_IDS:
            places = data_places(self.link(ident), self.fx.item_journal(ident))
            for rel, data in self.model[ident].items():
                found = any(read_or_none(os.path.join(top, rel)) == data for top in places)
                assert found, f"{ident}: {rel} lost (looked in {places})"

    @invariant()
    def nothing_parked_without_a_journal(self) -> None:
        olds = {j["old"] for i in ITEM_IDS if (j := self.fx.item_journal(i)) is not None}
        for root, dirs, names in os.walk(self.fx.home):
            for n in dirs + names:
                p = os.path.join(root, n)
                assert OLD_MARK not in n or p in olds, f"{p}: parked without a journal"

    @invariant()
    def classify_agrees_with_the_disk(self) -> None:
        every = set(BY_ID)
        for ident in ITEM_IDS:
            state, detail = bulk_items.classify(self.fx.bulk, BY_ID[ident], every, set(self.kept))
            assert state == self.oracle(ident), (
                f"{ident}: classify says {state} ({detail}), the disk {self.oracle(ident)}"
            )


class BulkItemsMachineTest(profiles.MbtTestCase):
    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(BulkItemsMachine)


if __name__ == "__main__":
    unittest.main()
