# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on btrfs-bulk's moves of an item into ~/Bulk and their recovery (tests/fault_injection.py).

The program runs over the unit tests' scratch home (btrfs/tests/bulk_fakes.py BulkHome: the real
code, with fakes only for subvolumes, +C, the busy check, flatpak and btrfs-snapshot's convert),
with os.fsync stubbed (what a kill leaves is checked here, not durability) and the user adapter's
two 0.05 s sleeps replaced by a wait for the filesystem's next ctime tick (bulk_fakes.tick_wait).
Every call by which a run changes the home or the state files is a point of a FaultPlan
(bulk_fakes.arm_points): the user adapter's renames, links, mkdirs, rmdirs, unlinks and created
files, the state files' writes and removals, cp, +C on a folder, the creation of ~/Bulk, and each
deletion of a tree, torn in two when it holds two or more files (a kill half-way through it, the
case the journal's "finishing" is for).
The plan is dead after a kill (FaultPlan(dead_after_kill=True)): the copy's
`except BaseException` cleanup does not run after a kill, as it does not on a real machine.

Each start state is one item's shape in a home whose layout is in place (put_layout: what a
first apply makes, without its cost; LayoutTest checks the two agree). One apply there has N
points; for every mode and every k in 1..N the apply fails at the k-th (a kill, or an OSError
that ends the run, as the program's traceback or the boot's exit 70 does). Then the next boot's
recover() runs, alone and with a second fault at each of its own points; then recover() and
apply() with no fault, and apply() once more.

To bound the module's time: after a kill the second fault is of every mode, after a failure or a
full disk a kill only (a kill during the repair is the hazard; a second failure after a full disk
is what FAIL_FROM's first fault covers already); and LateWrite and Conflict put their first fault
from the switch on (before it their apply is MoveBase's, which its own classes cover).

Checked after the fault, after the recovery and at the end:
- no data lost: every file of the item, with its content, is at its path in /home (through the
  link), or, while the item's journal exists, at the journal's old, new or target path;
- no data parked without a record: no `.bulk-old-` tree exists without the item's journal;
- the end converges: classify says ok, the item holds exactly its files, no journal, no
  `.bulk-` leftover, no boot flag, status names nothing that needs a look; and the last apply
  changes no file and logs no "done:".

The Conflict start state ends as documented instead: both sides kept, the journal names the old
tree, status says "written to on both sides", and a further apply changes nothing.

A rollback's deletion of the copy is cut like any other point: the copy is renamed to
`<target>.bulk-new.bulk-drop` first (UserFiles.u_drop), so what a cut deletion leaves is known by
its name, and the next repair goes on deleting it: those cases converge too.

Not modelled: Steam's journals, the child process of as_user (no user to drop to), real btrfs and
+C, and writes during a call other than the late writes of LateWrite and Conflict.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
import contextlib
import io
import os
from typing import ClassVar, Final, override
import unittest

from btrfs.bulk.application import items as bulk_items
from btrfs.bulk.domain import BY_ID, ITEMS, ItemJournal
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
from tests.fault_injection import ALL_MODES, FaultMode, FaultPlan, cut_points, every_fault, stub_fsync

EVERY_ID: Final = frozenset(i.id for i in ITEMS)
CONFLICT: Final = "written to on both sides"
# The second fault's modes, in recover(), after a first fault of each mode (the module's docstring).
SECOND_MODES: Final[Mapping[FaultMode, tuple[FaultMode, ...]]] = {
    FaultMode.KILL: ALL_MODES,
    FaultMode.FAIL: (FaultMode.KILL,),
    FaultMode.FAIL_FROM: (FaultMode.KILL,),
}

# The cases each class runs (a change of the protocol's calls moves them: pinned so it is seen).
CASES_MOVE_KILL = 103
CASES_MOVE_FAIL = 44
CASES_MOVE_FAIL_FROM = 47
CASES_NOCOW_REWRITE_KILL = 81
CASES_NOCOW_REWRITE_FAIL = 38
CASES_NOCOW_REWRITE_FAIL_FROM = 39
CASES_NOCOW_FROM_HOME_KILL = 104
CASES_NOCOW_FROM_HOME_FAIL = 45
CASES_NOCOW_FROM_HOME_FAIL_FROM = 48
CASES_SUBVOLUME_KILL = 25
CASES_SUBVOLUME_FAIL = 15
CASES_SUBVOLUME_FAIL_FROM = 15
CASES_STALE_KILL = 103
CASES_STALE_FAIL = 44
CASES_STALE_FAIL_FROM = 47
CASES_EMPTY_KILL = 6
CASES_EMPTY_FAIL = 6
CASES_EMPTY_FAIL_FROM = 6
CASES_LATE_WRITE_KILL = 188
CASES_LATE_WRITE_FAIL = 76
CASES_LATE_WRITE_FAIL_FROM = 79
CASES_CONFLICT_KILL = 50
CASES_CONFLICT_FAIL = 24
CASES_CONFLICT_FAIL_FROM = 27


def quiet(fn: Callable[[], object]) -> str:
    """What fn prints."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue()


def files_under(top: str) -> dict[str, str] | None:
    """The regular files below top (through a link at top), relative to it, with their text; None
    when top is no folder."""
    if not os.path.isdir(top):
        return None
    found: dict[str, str] = {}
    for root, _dirs, names in os.walk(top):
        for n in names:
            p = os.path.join(root, n)
            with open(p) as f:
                found[os.path.relpath(p, top)] = f.read()
    return found


def put_layout(fx: BulkHome) -> None:
    """What a first apply makes in an empty home, made directly: ~/Bulk (a fake subvolume) and its
    CACHEDIR.TAG, every item's folder (+C for the NOCOW ones), and the links whose folder exists
    (~/Programs). LayoutTest checks that it is what the apply makes, and that an apply there
    finds nothing to do."""
    fx.machine.subvol_create(fx.bulk_path())
    fx.user.u_write_tag(fx.bulk_path())
    for item in ITEMS:
        fx.user.u_mkdirs(fx.ctx.bulk, item.target, item.nocow)
        link = fx.ctx.link(item)
        if link is not None and os.path.isdir(os.path.dirname(link)):
            os.symlink(fx.ctx.target(item), link)


class BulkFaultBase(unittest.TestCase):
    """One start state: the item and its files; build() makes it. No tests here."""

    ITEM: ClassVar[str] = "apps"
    FILES: ClassVar[Mapping[str, str]] = {"a": "a", "sub/b": "b"}
    ENDS_IN_CONFLICT: ClassVar[bool] = False
    # The applies the end takes: one, but two where a late write may first come in the end's own
    # apply (LateWrite: that apply undoes the move, as documented, and the next one moves it).
    SETTLE_APPLIES: ClassVar[int] = 1
    # The first point a first fault is put at, by name (None: the first of all). LateWrite and
    # Conflict start at the switch: before it their apply is MoveBase's, which its classes cover.
    FIRST_FAULT_AT: ClassVar[str | None] = None

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.extra: dict[str, str] = {}  # files the hooks wrote, relative to the item's path

    # ---- the start state ----------------------------------------------------------------------
    def link(self) -> str:
        link = BY_ID[self.ITEM].link
        assert link is not None
        return link

    def build(self, fx: BulkHome) -> None:
        """The item's start state, over the layout (put_layout)."""
        raise NotImplementedError

    def hook(self, fx: BulkHome) -> None:
        """Late writes, for the start states that have them."""

    def put_files(self, fx: BulkHome, top: str) -> None:
        """The item's files below top (copy-on-write: no fake +C)."""
        for rel, data in self.FILES.items():
            p = os.path.join(top, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(data)
            fx.nocow.discard(inode(p))

    @contextlib.contextmanager
    def home(self) -> Generator[tuple[BulkHome, FaultPlan]]:
        """A fresh scratch home in the start state, its points armed on a plan."""
        fx = BulkHome()
        try:
            use_tick_clock(fx)
            put_layout(fx)
            self.build(fx)
            self.extra = {}
            self.hook(fx)
            tick_wait(fx.tmp)
            plan = FaultPlan(dead_after_kill=True)
            arm_points(fx, plan, torn_rmtree=True)
            yield fx, plan
        finally:
            fx.close()

    # ---- runs ---------------------------------------------------------------------------------
    def expected(self) -> dict[str, str]:
        return dict(self.FILES) | self.extra

    # ---- the invariants -----------------------------------------------------------------------
    def assert_safe(self, fx: BulkHome, when: str) -> None:
        j = fx.item_journal(self.ITEM)
        places = data_places(fx.home_path(self.link()), j)
        for rel, data in self.expected().items():
            if not any(read_or_none(os.path.join(p, rel)) == data for p in places):
                self.fail(f"{when}: {rel} lost (looked in {places})")
        if j is None:
            parked = [r for r, d, f in os.walk(fx.home) for n in d + f if OLD_MARK in n]
            self.assertEqual(parked, [], f"{when}: data parked without a journal")

    def assert_converged(self, fx: BulkHome, when: str) -> None:
        state = bulk_items.classify(fx.bulk, BY_ID[self.ITEM], set(EVERY_ID))
        self.assertEqual(state, ("ok", ""), f"{when}: {fx.out}")
        self.assertEqual(files_under(fx.home_path(self.link())), self.expected(), when)
        self.assertEqual(fx.journals(), [], f"{when}: {fx.out}")
        left = [n for _r, d, f in os.walk(fx.home) for n in d + f if ".bulk-" in n]
        self.assertEqual(left, [], f"{when}: leftovers")
        self.assertFalse(os.path.exists(fx.paths.flag), f"{when}: boot flag left")
        self.assertNotIn("NEEDS A LOOK", fx.report()[1], when)

    def assert_conflict(self, fx: BulkHome, when: str) -> None:
        j = fx.item_journal(self.ITEM)
        assert j is not None, f"{when}: no conflict journal: {fx.out}"
        self.assertTrue(j.get("conflict", False), when)
        self.assertEqual(files_under(j["old"]), dict(self.FILES) | {"late": "late"}, when)
        self.assertEqual(files_under(fx.home_path(self.link())), dict(self.FILES) | {"linked": "linked"}, when)
        self.assertIn(CONFLICT, fx.report()[1], when)

    def settle(self, fx: BulkHome, plan: FaultPlan, when: str) -> None:
        """recover() and apply() with no fault, the end checks, apply() again."""
        plan.arm()
        quiet(lambda: bulk_items.recover(fx.bulk))
        for _ in range(self.SETTLE_APPLIES):
            fx.apply()
        if self.ENDS_IN_CONFLICT:
            self.assert_conflict(fx, when)
        else:
            self.assert_converged(fx, when)
        before = fx.tree()
        fx.apply()
        self.assertEqual(fx.tree(), before, f"{when}: a further apply changed files")
        self.assertNotIn("done:", fx.out, when)

    def every_fault_then_recover(self, mode: FaultMode) -> int:
        """Each point of one apply failing in mode, then recover() (alone, and with a second fault
        of every mode at each of its points), then the end; the number of cases run."""
        with self.home() as (fx, plan):
            points = cut_points(plan, fx.apply)
            self.settle(fx, plan, "no fault")
        after = 0 if self.FIRST_FAULT_AT is None else points.index(self.FIRST_FAULT_AT)
        cases = 0
        recover = bulk_items.recover
        for _mode, k in every_fault(len(points), (mode,), after=after):
            steps: list[str] = []
            when = f"{mode} at {k}"
            with self.subTest(k=k), self.home() as (fx, plan):
                cases += 1
                run_armed(plan, fx.apply, (mode, k))
                self.assert_safe(fx, when)
                steps = run_armed(plan, lambda fx=fx: recover(fx.bulk))
                self.assert_safe(fx, f"{when}, recovered")
                self.settle(fx, plan, when)
            for mode2, j in every_fault(len(steps), SECOND_MODES[mode]):
                when2 = f"{when}, then {mode2} at recover step {j} ({steps[j - 1]})"
                with self.subTest(k=k, then=mode2.value, j=j), self.home() as (fx, plan):
                    cases += 1
                    run_armed(plan, fx.apply, (mode, k))
                    run_armed(plan, lambda fx=fx: recover(fx.bulk), (mode2, j))
                    self.assert_safe(fx, when2)
                    self.settle(fx, plan, when2)
        return cases


# ---- the start states ---------------------------------------------------------------------------
class MoveBase(BulkFaultBase):
    """~/Programs a real folder with two files (one in a subfolder), ~/Bulk/apps absent: copy, switch,
    link, the journal's finishing, the old tree's deletion; then jetbrains' folder (`after` apps)."""

    @override
    def build(self, fx: BulkHome) -> None:
        os.unlink(fx.home_path("Programs"))
        os.rmdir(fx.bulk_path("apps/jetbrains"))
        os.rmdir(fx.bulk_path("apps"))
        self.put_files(fx, fx.home_path("Programs"))


class NocowRewriteBase(BulkFaultBase):
    """~/.android/avd linked, ~/Bulk/emulators/android/avd holding copy-on-write files: the in-place
    rewrite into +C (a journal inside ~/Bulk only, no link)."""

    ITEM = "avd"
    FILES: ClassVar[Mapping[str, str]] = {"disk.img": "disk", "d2": "d2"}

    @override
    def build(self, fx: BulkHome) -> None:
        os.mkdir(fx.home_path(".android"))
        os.symlink(fx.bulk_path("emulators/android/avd"), fx.home_path(".android/avd"))
        self.put_files(fx, fx.bulk_path("emulators/android/avd"))


class NocowFromHomeBase(BulkFaultBase):
    """~/.android/avd a real folder with data: a real copy into the +C target (du and the free space read)."""

    ITEM = "avd"
    FILES: ClassVar[Mapping[str, str]] = {"disk.img": "disk", "d2": "d2"}

    @override
    def build(self, fx: BulkHome) -> None:
        self.put_files(fx, fx.home_path(".android/avd"))


class SubvolumeBase(BulkFaultBase):
    """~/Downloads/iso an old (fake) subvolume: its journal, the rename into ~/Bulk, the link."""

    ITEM = "iso"
    FILES: ClassVar[Mapping[str, str]] = {"f.iso": "iso", "sub/g": "g"}

    @override
    def build(self, fx: BulkHome) -> None:
        self.put_files(fx, fx.home_path("Downloads/iso"))
        fx.subvols.add(inode(fx.home_path("Downloads/iso")))


class StaleBase(BulkFaultBase):
    """~/go/pkg/mod a real folder again (its tool's clean removed the link), stale data in ~/Bulk:
    the stale side deleted, then the move."""

    ITEM = "go-mod"
    FILES: ClassVar[Mapping[str, str]] = {"cache/x": "x", "y": "y"}

    @override
    def build(self, fx: BulkHome) -> None:
        stale = fx.bulk_path("toolchains/go/pkg/mod/stale")
        with open(stale, "w") as f:
            f.write("stale")
        self.put_files(fx, fx.home_path("go/pkg/mod"))


class EmptyBase(BulkFaultBase):
    """~/Programs an empty folder: removed and replaced by the link, no journal."""

    FILES: ClassVar[Mapping[str, str]] = dict[str, str]()

    @override
    def build(self, fx: BulkHome) -> None:
        os.unlink(fx.home_path("Programs"))
        os.mkdir(fx.home_path("Programs"))


class LateWriteBase(MoveBase):
    """MoveBase, and a write into the old tree right after the switch (once): migrate's own undo
    (the journal's undo, the rollback, the journal dropped, the target made again)."""

    SETTLE_APPLIES = 2
    FIRST_FAULT_AT = "os.rename"

    @override
    def hook(self, fx: BulkHome) -> None:
        real = fx.user.u_late_write

        def late_write(j: ItemJournal) -> bool:
            if "late" not in self.extra and os.path.isdir(j["old"]):
                self.write_once(j)
            return real(j)

        fx.fake(fx.user, "u_late_write", late_write)

    def write_once(self, j: ItemJournal) -> None:
        with open(os.path.join(j["old"], "late"), "w") as f:
            f.write("late")
        self.extra["late"] = "late"


class ConflictBase(LateWriteBase):
    """LateWriteBase, and a write through the link as well: the conflict journal, both sides kept."""

    ENDS_IN_CONFLICT = True
    SETTLE_APPLIES = 1

    @override
    def write_once(self, j: ItemJournal) -> None:
        super().write_once(j)
        with open(os.path.join(j["target"], "linked"), "w") as f:
            f.write("linked")
        self.extra["linked"] = "linked"


# ---- the classes: one per start state and mode --------------------------------------------------
class MoveCutPointsTest(MoveBase):
    """The fail-fast step: the points of one move, and what a kill at its copy leaves."""

    def test_the_points_of_one_move(self) -> None:
        with self.home() as (fx, plan):
            calls = cut_points(plan, fx.apply)
            self.assert_converged(fx, "no fault")
        stamp = calls[11].rsplit(OLD_MARK, 1)[1]  # the old tree's name ends with the time
        want = [
            "os.mkdir",  # ~/Bulk/apps
            "os.mkdir",  # ~/Bulk/apps.bulk-new
            "cp Bulk/apps.bulk-new",
            "write bulk-at-boot",
            "os.open",  # the clock's stamp probe
            "os.unlink",
            "write journal-apps.json",
            "os.rename",  # ~/Programs -> its old name
            "os.rename",  # the copy -> ~/Bulk/apps
            "os.symlink",
            "write journal-apps.json",  # finishing
            f"rmtree-part Programs{OLD_MARK}{stamp}",
            f"rmtree Programs{OLD_MARK}{stamp}",
            "os.remove",  # the journal
            "os.mkdir",  # ~/Bulk/apps/jetbrains, `after` apps
            "chattr +C Bulk/emulators",  # every apply sets +C on the NOCOW folders again
            "chattr +C Bulk/emulators/android",
            "chattr +C Bulk/emulators/android/avd",
            "os.remove",  # the boot flag
        ]
        self.assertEqual(calls, want)

    def test_a_kill_at_the_copy_leaves_the_whole_copy_and_no_journal(self) -> None:
        with self.home() as (fx, plan):
            run_armed(plan, fx.apply, (FaultMode.KILL, 3))
            # A dead process runs no cleanup: the copy stays, with no journal (the next prepare deletes it).
            self.assertEqual(files_under(fx.bulk_path("apps.bulk-new")), dict(self.FILES))
            self.assertEqual(fx.journals(), [])
            self.settle(fx, plan, "kill at cp")


class LayoutTest(unittest.TestCase):
    """put_layout makes what the layout apply makes: an apply there changes nothing and creates nothing."""

    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_an_apply_on_the_layout_changes_nothing(self) -> None:
        fx = BulkHome()
        self.addCleanup(fx.close)
        put_layout(fx)
        before, nocow, subvols = fx.tree(), set(fx.nocow), set(fx.subvols)
        self.assertEqual(fx.apply(), ([], []), fx.out)
        self.assertEqual(fx.tree(), before)
        self.assertEqual((fx.nocow, fx.subvols), (nocow, subvols))
        self.assertNotIn("created", fx.out)
        self.assertNotIn("done:", fx.out)

    def test_the_layout_apply_makes_the_same_tree(self) -> None:
        made, applied = BulkHome(), BulkHome()
        self.addCleanup(made.close)
        self.addCleanup(applied.close)
        put_layout(made)
        applied.apply()

        def shape(fx: BulkHome) -> list[TreeEntry]:
            return [
                (os.path.relpath(p, fx.tmp), k, v if k != "link" else os.path.relpath(str(v), fx.tmp))
                for p, k, v in fx.tree()
            ]

        def flagged(fx: BulkHome, keys: set[tuple[int, int]]) -> list[str]:
            return sorted(
                os.path.relpath(os.path.join(r, n), fx.home)
                for r, d, _f in os.walk(fx.home)
                for n in d
                if inode(os.path.join(r, n)) in keys
            )

        self.assertEqual(shape(made), shape(applied))
        self.assertEqual(flagged(made, made.nocow), flagged(applied, applied.nocow))
        self.assertEqual(inode(made.bulk_path()) in made.subvols, inode(applied.bulk_path()) in applied.subvols)


class MoveKillTest(MoveBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_MOVE_KILL)


class MoveFailTest(MoveBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_MOVE_FAIL)


class MoveFailFromTest(MoveBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_MOVE_FAIL_FROM)


class NocowRewriteKillTest(NocowRewriteBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_NOCOW_REWRITE_KILL)


class NocowRewriteFailTest(NocowRewriteBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_NOCOW_REWRITE_FAIL)


class NocowRewriteFailFromTest(NocowRewriteBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_NOCOW_REWRITE_FAIL_FROM)


class NocowFromHomeKillTest(NocowFromHomeBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_NOCOW_FROM_HOME_KILL)


class NocowFromHomeFailTest(NocowFromHomeBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_NOCOW_FROM_HOME_FAIL)


class NocowFromHomeFailFromTest(NocowFromHomeBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_NOCOW_FROM_HOME_FAIL_FROM)


class SubvolumeKillTest(SubvolumeBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_SUBVOLUME_KILL)


class SubvolumeFailTest(SubvolumeBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_SUBVOLUME_FAIL)


class SubvolumeFailFromTest(SubvolumeBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_SUBVOLUME_FAIL_FROM)


class StaleKillTest(StaleBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_STALE_KILL)


class StaleFailTest(StaleBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_STALE_FAIL)


class StaleFailFromTest(StaleBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_STALE_FAIL_FROM)


class EmptyKillTest(EmptyBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_EMPTY_KILL)


class EmptyFailTest(EmptyBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_EMPTY_FAIL)


class EmptyFailFromTest(EmptyBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_EMPTY_FAIL_FROM)


class LateWriteKillTest(LateWriteBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_LATE_WRITE_KILL)


class LateWriteFailTest(LateWriteBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_LATE_WRITE_FAIL)


class LateWriteFailFromTest(LateWriteBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_LATE_WRITE_FAIL_FROM)


class ConflictKillTest(ConflictBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_CONFLICT_KILL)


class ConflictFailTest(ConflictBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_CONFLICT_FAIL)


class ConflictFailFromTest(ConflictBase):
    def test_every_fault_then_recover(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_CONFLICT_FAIL_FROM)
