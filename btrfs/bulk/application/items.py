# SPDX-License-Identifier: MPL-2.0
"""The ~/Bulk items: the state of each one (classify), the move of its
data into ~/Bulk and the link left behind (migrate: the journal of one
move), the recovery of interrupted moves (recover), apply, the plan/status
report and the boot step.
"""

from __future__ import annotations

from collections.abc import Sequence
import os
import time

from btrfs.bulk.application.context import Bulk
from btrfs.bulk.application.steam import damaged, steam_conflict_text, steam_recover_all
from btrfs.bulk.domain import (
    BIG,
    BY_ID,
    ITEMS,
    MARGIN,
    STATE_TEXT,
    BulkCommand,
    BulkDirState,
    BulkItemDetail,
    BulkItemResult,
    BulkItemState,
    BulkJournalKind,
    BulkTargetState,
    CacheTagState,
    FlatpakAccess,
    Item,
    ItemJournal,
    LstatKind,
    PathChain,
    TreeSize,
    Unexpected,
    conflict_text,
    describe_paths,
    du_text,
    gib,
    kept_text,
    present,
    row_text,
)
from btrfs.bulk.domain.steam import STEAMAPPS

THE_LINK = "the link"  # what present() names when an item has no link


def classify(b: Bulk, item: Item, done: set[str], kept: set[str] | None = None) -> tuple[BulkItemState, str]:
    """(state, detail). done: ids already in place this run (for `after`);
    kept: ids kept in /home by choice (None: read from the keep file).
    States: kept, ok, mkdir, link, move, move-subvol, nocow, wait, unexpected;
    ok and mkdir carry 'nolink' in detail when the tool's parent folder is
    absent; kept carries 'linked' when the link into ~/Bulk is still there."""
    if kept is None:
        kept = b.state.read_keep()[0]
    chosen = classify_kept(b, item, done, kept)
    if chosen is not None:
        return chosen
    ts = b.tree.target_state(b.ctx, item)
    if ts.startswith("bad:"):
        return BulkItemState.UNEXPECTED, ts[4:]
    if item.link is None:
        return (BulkItemState.MKDIR if ts == BulkTargetState.ABSENT else BulkItemState.OK), ""
    return classify_link(b, item, item.link, ts)


def classify_kept(b: Bulk, item: Item, done: set[str], kept: set[str]) -> tuple[BulkItemState, str] | None:
    """classify for an item kept in /home, or one that waits for the item it
    is `after`; None for the others."""
    ctx, t = b.ctx, b.tree
    if item.id in kept:
        L = ctx.link(item)
        linked = L is not None and t.kind(L) == LstatKind.LINK and t.link_target(L) == ctx.target(item)
        return BulkItemState.KEPT, BulkItemDetail.LINKED if linked else ""
    if item.after is not None and item.after in kept:
        why = f"~/{BY_ID[item.after].link} is kept in /home (by choice)"
        L = ctx.link(item)
        if (
            L is not None
            and item.link is not None
            and t.chain(ctx.home, item.link) == PathChain.OK
            and not t.is_subvol(L)
            and not t.empty_dir(L)
        ):
            # Its data would stay in /home snapshots without anyone deciding so.
            return BulkItemState.UNEXPECTED, (
                f"{why}, so this cannot move into ~/Bulk and its data stays in /home: "
                f"'bulk keep {item.id}' too, or 'bulk unkeep {item.after}'"
            )
        return BulkItemState.WAIT, f"{why}: keep {item.id} too, or 'bulk unkeep {item.after}'"
    if item.after is not None and item.after not in done:
        return BulkItemState.WAIT, f"after ~/{BY_ID[item.after].link} is in place"
    return None


def classify_link(b: Bulk, item: Item, link: str, ts: str) -> tuple[BulkItemState, str]:
    """classify for an item with a link (link: item.link), its target in state ts."""
    ctx, t = b.ctx, b.tree
    T = ctx.target(item)
    L = present(ctx.link(item), THE_LINK)
    c = t.chain(ctx.home, os.path.dirname(link))
    if c == PathChain.ABSENT:
        if ts == BulkTargetState.ABSENT:
            return BulkItemState.MKDIR, BulkItemDetail.NOLINK
        return nocow_check(b, item, ts) or (BulkItemState.OK, BulkItemDetail.NOLINK)
    if c != PathChain.OK:
        what, p = c.split(":", 1)
        return (
            BulkItemState.UNEXPECTED,
            f"{ctx.short(p)} is a {'symlink' if what == 'symlink' else 'file'}, not a folder",
        )
    k = t.kind(L)
    if k == LstatKind.ABSENT:
        return BulkItemState.LINK, ""
    if k == LstatKind.LINK:
        if t.link_target(L) != T:
            return BulkItemState.UNEXPECTED, f"symlink to {t.readlink(L)} (expected {ctx.short(T)})"
        if ts == BulkTargetState.ABSENT:
            return BulkItemState.MKDIR, ""
        return nocow_check(b, item, ts) or (BulkItemState.OK, "")
    if k == LstatKind.OTHER:
        return BulkItemState.UNEXPECTED, "a file, not a folder"
    return classify_folder(b, item, L, ts)


def classify_folder(b: Bulk, item: Item, path: str, ts: str) -> tuple[BulkItemState, str]:
    """classify for an item whose old path (path) is a real folder."""
    t = b.tree
    if t.has_mount(path):
        return BulkItemState.UNEXPECTED, "is or contains a mount point"
    if not t.is_subvol(path) and t.empty_dir(path):
        return BulkItemState.MOVE, BulkItemDetail.EMPTY  # nothing to lose, also next to a filled target
    if ts == BulkTargetState.DATA:
        if item.disposable and not t.is_subvol(path):
            return BulkItemState.MOVE, BulkItemDetail.STALE  # the tool replaced its link: the ~/Bulk side is stale
        return BulkItemState.UNEXPECTED, f"data in both ~/{item.link} and {b.ctx.short(b.ctx.target(item))}"
    if t.is_subvol(path):
        return (BulkItemState.MOVE, BulkItemDetail.SUBVOLUME) if item.nocow else (BulkItemState.MOVE_SUBVOL, "")
    return BulkItemState.MOVE, ""


def nocow_check(b: Bulk, item: Item, ts: str) -> tuple[BulkItemState, str] | None:
    """For a NOCOW item whose target exists (ts: empty or data): ('nocow',
    why) when it, a folder or file in it, or a folder above it up to
    ~/Bulk/NOCOW_TOP lacks +C; None when all is in place (or not NOCOW)."""
    if not item.nocow:
        return None
    ctx = b.ctx
    T = ctx.target(item)
    if ts == BulkTargetState.DATA:
        files, dirs_cow = b.tree.cow_data(T)
        if files:
            return BulkItemState.NOCOW, f"{files} file(s) still copy-on-write"
        if dirs_cow:
            return BulkItemState.NOCOW, "folders without +C"
    elif not b.tree.is_nocow(T):
        return BulkItemState.NOCOW, "folder without +C"
    parents = b.tree.nocow_parents(ctx, item)
    if parents:
        return BulkItemState.NOCOW, f"folder {ctx.short(parents[0])} without +C"
    return None


def committed(b: Bulk, j: ItemJournal) -> bool:
    """Did the switch of journal j complete (the target holds the data)?"""
    kind = b.tree.kind
    if j["kind"] == BulkJournalKind.SUBVOL:
        return kind(present(j["link"], "the journal's link")) == LstatKind.LINK
    if j["link"] is not None and j["link"] != "":
        return kind(j["link"]) == LstatKind.LINK and b.tree.link_target(j["link"]) == j["target"]
    # In-place rewrite of the target (no link): new renamed onto it, old kept.
    return (
        kind(j["new"]) == LstatKind.ABSENT and kind(j["target"]) == LstatKind.DIR and kind(j["old"]) != LstatKind.ABSENT
    )


def recover(b: Bulk) -> list[str]:
    """Finish or undo what an interrupted run left (kill, power loss).
    Returns the ids whose journal stays (they need a look)."""
    state, log = b.state, b.out.log
    stuck: list[str] = []
    for item in ITEMS:
        j = state.jread_item(item.id)
        if not j:
            if state.jexists(item.id):
                stuck.append(item.id)
                log(f"NEEDS A LOOK: {item.id}: {state.newer_journal(item.id) or damaged(state, item.id)}")
            continue
        try:
            recover_item(b, item, j)
            state.jdrop(item.id)
        except (OSError, RuntimeError, Unexpected) as e:
            stuck.append(item.id)
            wait = "" if j.get("bulk_only", False) else "; snapshots wait until it is gone"
            log(f"NEEDS A LOOK: {item.id}: an interrupted move: {e} (journal kept in {state.jpath(item.id)}{wait})")
    return stuck + steam_recover_all(b)


def recover_item(b: Bulk, item: Item, j: ItemJournal) -> None:
    """Finish or undo the move journal j recorded (ItemRecover); an exception: it stays."""
    ctx, user, log = b.ctx, b.user, b.out.log
    if j.get("conflict", False):
        if b.tree.kind(j["old"]) != LstatKind.ABSENT:
            raise Unexpected(conflict_text(ctx, j))
        log(f"  {item.id}: the old tree is gone: the move is complete")
    elif j.get("finishing", False):
        # The last check had passed and the old tree was being deleted:
        # what is left of it is a partial copy (each deletion changes
        # ctimes there, so the late-write check cannot be made again).
        user.finish(j)
        log(f"  {item.id}: deleted the old copy left by an interrupted run (its deletion was cut short)")
    elif j["kind"] == BulkJournalKind.SUBVOL:
        recover_subvol(b, item, j)
    elif not j.get("undo", False) and committed(b, j):
        recover_switched(b, item, j)
    else:
        user.rollback(j)
        log(f"  {item.id}: undid an interrupted move (the data is back at {ctx.short(j['src'])})")


def recover_subvol(b: Bulk, item: Item, j: ItemJournal) -> None:
    """recover_item for a subvolume renamed into ~/Bulk: the link, if it is missing."""
    kind = b.tree.kind
    L, T = present(j["link"], "the journal's link"), j["target"]
    if kind(L) == LstatKind.ABSENT and kind(T) == LstatKind.DIR:
        b.user.symlink(L, T)
        b.out.log(f"  {item.id}: finished an interrupted move")
    elif not (kind(L) == LstatKind.LINK and b.tree.link_target(L) == T) and b.tree.entries_nofollow(T):
        # Moved, then the old path was created again (an app)
        # before the link: the moved subvolume is the full data.
        raise Unexpected(
            f"{L} was created again after its data was moved to {T}: move what you need "
            f"from {L} into {T}, delete {L}, then run bulk apply again (it makes the link)"
        )


def recover_switched(b: Bulk, item: Item, j: ItemJournal) -> None:
    """recover_item for a switch that completed: the same last check as right
    after a switch (migrate), then the old tree goes, or the switch is undone."""
    ctx, user, state, log = b.ctx, b.user, b.state, b.out.log
    # A write into the old tree in the instant of the switch (or the crash)
    # must not be deleted with it.
    if b.host.busy_paths([j["old"]], ctx.home) or user.late_write(j):
        if "stamp2" not in j or user.new_written(j):
            # Both sides written (or a journal of an older
            # version, without stamp2: cannot tell): keep both.
            j["conflict"] = True
            state.jwrite(item.id, j)
            raise Unexpected(conflict_text(ctx, j))
        j["undo"] = True
        state.jwrite(item.id, j)
        user.rollback(j)
        log(
            f"  {item.id}: undid an interrupted move: the old folder was written to during the "
            f"switch (the data is back at {ctx.short(j['src'])})"
        )
    else:
        j["finishing"] = True  # a kill now: the next repair goes on deleting
        state.jwrite(item.id, j)
        user.finish(j)
        log(f"  {item.id}: deleted the old copy left by an interrupted run")


# ---- the actions ------------------------------------------------------------------------------
def migrate(b: Bulk, item: Item, src: str, link: str | None) -> BulkItemResult:
    """Move src into the item's target by copy + switch; with link, src then
    becomes a symlink to it. Returns ok | busy | changed."""
    ctx, user, state, busy = b.ctx, b.user, b.state, b.host.busy_paths
    T = ctx.target(item)
    new = T + ".bulk-new"
    old = f"{src}.bulk-old-{time.strftime('%Y%m%dT%H%M%S')}"
    if busy([src], ctx.home):
        return BulkItemResult.BUSY
    if item.nocow:
        # A real copy: the data exists twice until the old tree is deleted.
        size = b.tree.du_bytes(src)
        if b.tree.free_bytes(ctx.bulk) < size.total + MARGIN:
            raise RuntimeError(f"not enough free space for a {du_text(size)} copy (plus {gib(MARGIN)} kept free)")
    user.mkdirs(ctx.bulk, os.path.dirname(item.target), nocow=item.nocow)
    stamp = user.prepare(src, new, nocow=item.nocow)
    j: ItemJournal = {
        "kind": BulkJournalKind.COPY,
        "src": src,
        "target": T,
        "new": new,
        "old": old,
        "link": link,
        "stamp": stamp,
        # All inside ~/Bulk (the in-place NOCOW rewrite): no data of it in /home.
        "bulk_only": all(p.startswith(ctx.bulk + "/") for p in (src, T, new, old)),
    }
    # Race guard, last moment before the switch (as convert_one in btrfs-snapshot).
    if busy([src], ctx.home):
        user.rmtree(new)
        return BulkItemResult.BUSY
    # The boot service must run if this is interrupted (it recovers before
    # login, before apps recreate the old path), and snapshots wait meanwhile.
    state.flag_add(item.id)
    # The clock before the switch, kept in the journal: a repair after a crash
    # makes the same last check as below (was the copy written to since?).
    j["stamp2"] = user.clock(new)
    state.jwrite(item.id, j)
    if not user.commit(j):
        state.jdrop(item.id)
        return BulkItemResult.CHANGED
    # Anything that wrote into the old tree in the instant of the switch: undo,
    # unless the copy (in use through the link now) was written to as well.
    if busy([old], ctx.home) or user.late_write(j):
        if user.new_written(j):
            j["conflict"] = True
            state.jwrite(item.id, j)
            raise Unexpected(conflict_text(ctx, j))
        j["undo"] = True  # a kill now: the recovery goes on undoing
        state.jwrite(item.id, j)
        user.rollback(j)
        state.jdrop(item.id)
        user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
        return BulkItemResult.CHANGED
    # Past the last check: from here on the old tree is only being deleted (a
    # kill in the middle must not make a repair take the half-deleted tree for
    # a late write and roll back onto it).
    j["finishing"] = True
    state.jwrite(item.id, j)
    user.finish(j)
    state.jdrop(item.id)
    return BulkItemResult.OK


def act(b: Bulk, item: Item, state: str, detail: str) -> str:
    """Apply one item; returns a BulkItemResult (kept, ok, busy, changed, wait) or unexpected:<why>."""
    ctx, user = b.ctx, b.user
    T, L = ctx.target(item), ctx.link(item)
    match state:
        case BulkItemState.KEPT:
            return BulkItemResult.KEPT
        case BulkItemState.OK | BulkItemState.MKDIR:
            user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
            return BulkItemResult.OK
        case BulkItemState.LINK:
            L = present(L, THE_LINK)
            user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
            user.symlink(L, T)
            return BulkItemResult.OK
        case BulkItemState.MOVE:
            return act_move(b, item, detail, present(L, THE_LINK))
        case BulkItemState.MOVE_SUBVOL:
            return act_move_subvol(b, item, present(L, THE_LINK))
        case BulkItemState.NOCOW:
            # The folders above it (~/Bulk/emulators...) get +C in any case.
            user.mkdirs(ctx.bulk, os.path.dirname(item.target), nocow=True)
            if detail.startswith("folder"):  # no data: +C set in place
                user.set_nocow_dirs(T)
                return BulkItemResult.OK
            return migrate(b, item, T, None)
        case BulkItemState.WAIT:
            return BulkItemResult.WAIT
        case _:
            return "unexpected:" + detail


def act_move(b: Bulk, item: Item, detail: str, path: str) -> BulkItemResult:
    """act for a move of the real folder at path (detail: empty, stale, subvolume or '')."""
    ctx, user, busy = b.ctx, b.user, b.host.busy_paths
    T = ctx.target(item)
    if detail == BulkItemDetail.EMPTY:
        if busy([path], ctx.home):
            return BulkItemResult.BUSY
        user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
        user.rmdir(path)
        user.symlink(path, T)
        return BulkItemResult.OK
    if detail == BulkItemDetail.STALE:
        if busy([path, T], ctx.home):
            return BulkItemResult.BUSY
        b.out.log(
            f"  {item.id}: ~/{item.link} is a real folder again (its tool's clean command removed the "
            f"link): deleting the stale copy in {ctx.short(T)} (a cache: downloaded again when needed)"
        )
        user.rmtree(T)
    user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
    return migrate(b, item, path, path)


def act_move_subvol(b: Bulk, item: Item, path: str) -> BulkItemResult:
    """act for an old subvolume at path: renamed into ~/Bulk, with its journal."""
    ctx, user = b.ctx, b.user
    T = ctx.target(item)
    if b.host.busy_paths([path], ctx.home):
        return BulkItemResult.BUSY
    user.mkdirs(ctx.bulk, os.path.dirname(item.target), nocow=False)
    b.state.flag_add(item.id)
    b.state.jwrite(
        item.id,
        {"kind": BulkJournalKind.SUBVOL, "src": path, "target": T, "new": "", "old": "", "link": path, "stamp": 0},
    )
    user.move_subvol(path, T)
    b.state.jdrop(item.id)
    return BulkItemResult.OK


def convert_bulk_dir(b: Bulk) -> bool:
    """~/Bulk exists as a plain folder: btrfs-snapshot turns it into a
    subvolume (its convert_one: reflink clone + switch, busy -> later)."""
    for line in b.host.convert_bulk_dir(b.ctx.bulk):
        b.out.log("  " + line)
    return b.tree.is_subvol(b.ctx.bulk)


def prune_convert_plan(b: Bulk) -> None:
    """An older snapshots setup planned some of these paths as nested
    subvolumes: they are symlinks into ~/Bulk now, never to be converted."""
    drop = {link for i in ITEMS if (link := b.ctx.link(i)) is not None}
    if b.state.prune_convert_plan(drop):
        b.out.log("  convert-plan: dropped the paths that now live in ~/Bulk")


def bulk_only_journal(b: Bulk, ident: str) -> bool:
    j = b.state.jread_item(ident)
    return bool(j and j.get("bulk_only"))


def exit_status(b: Bulk, deferred: Sequence[str], problems: Sequence[str]) -> int:
    """1 something needs a look, 4 only something inside ~/Bulk (the in-place
    +C rewrite: its journal, or a failure of it; no data of it in /home),
    3 only busy items, 0 done."""
    hard = [p for p in problems if not (problems.count(p) == 1 and (p in b.bulk_only or bulk_only_journal(b, p)))]
    if hard:
        return 1
    if problems:
        return 4
    return 3 if deferred else 0


def apply(b: Bulk) -> tuple[list[str], list[str]]:
    """Bring every item to its declared state. Returns (deferred, problems)."""
    log = b.out.log
    deferred: list[str] = []
    problems: list[str] = []
    b.bulk_only.clear()
    # Before taking the lock: btrfs-snapshot convert-one takes it itself.
    if b.tree.bulk_state(b.ctx) == BulkDirState.PLAIN:
        log("~/Bulk is a plain folder: turning it into a subvolume first")
        if not convert_bulk_dir(b):
            log("~/Bulk: in use or failed (see above): everything waits for the next boot")
            b.state.write_flag(["bulk"])
            return ["bulk"], []
    lock = b.state.run_lock()
    try:
        stuck = recover(b)
        problems += stuck
        prune_convert_plan(b)
        if not bulk_in_place(b, problems):
            return [], ["bulk"]
        done: set[str] = set()
        try:
            kept, unknown = b.state.read_keep()
        except OSError as e:
            problems.append("bulk-keep")
            log(
                f"NEEDS A LOOK: {b.paths.keep} cannot be read ({e}): no item touched, since what you keep in "
                "/home is unknown (fix or delete that file, then run bulk apply again)"
            )
            return deferred, problems
        for ident in unknown:
            log(f"{b.paths.keep}: unknown item '{ident}' (ignored; valid: {', '.join(BY_ID)})")
        for item in ITEMS:
            apply_item(b, item, stuck, done, kept, deferred, problems)
    finally:
        b.state.release(lock)
    b.state.write_flag(deferred)
    return deferred, problems


def bulk_in_place(b: Bulk, problems: list[str]) -> bool:
    """~/Bulk as a subvolume (created if absent, relabelled) and its
    CACHEDIR.TAG (a problem added when that fails); False: nothing can be done."""
    ctx, user, log = b.ctx, b.user, b.out.log
    bs = b.tree.bulk_state(ctx)
    if bs == BulkDirState.BAD:
        log(f"{ctx.short(ctx.bulk)} is a file or a symlink: nothing done (move it away, then run again)")
        return False
    if bs == BulkDirState.CREATE:
        try:
            user.create_bulk(ctx.bulk)
        except (OSError, RuntimeError) as e:
            log(f"cannot create ~/Bulk as a subvolume ({e}): nothing done")
            return False
        log("created:   ~/Bulk (a subvolume, yours: left out of /home snapshots)")
    # Every run (cheap): also a ~/Bulk made by an older run or by hand.
    user.relabel(ctx.bulk)
    if not b.tree.is_subvol(ctx.bulk):
        log("~/Bulk is not a subvolume after creating it: stopped")
        return False
    t = b.tree.tag_state(ctx)
    if t == CacheTagState.CREATE:
        try:
            user.write_tag(ctx.bulk)
            log("created:   ~/Bulk/CACHEDIR.TAG")
        except (OSError, RuntimeError) as e:
            problems.append("CACHEDIR.TAG")
            log(f"NEEDS A LOOK: ~/Bulk/CACHEDIR.TAG: {e}")
    elif t == CacheTagState.BAD:
        problems.append("CACHEDIR.TAG")
        log("NEEDS A LOOK: ~/Bulk/CACHEDIR.TAG is not a cache tag (left as it is)")
    return True


def apply_item(
    b: Bulk,
    item: Item,
    stuck: Sequence[str],
    done: set[str],
    kept: set[str],
    deferred: list[str],
    problems: list[str],
) -> None:
    """One item of apply(): its step, what came of it, its Flatpak permission."""
    log = b.out.log
    state, detail = "", ""
    if item.id in stuck:
        log(f"{'skipped:':<10} {describe_paths(b.ctx, item)}: its interrupted move needs a look (above)")
        return
    try:
        state, detail = classify(b, item, done, kept)
        res = act(b, item, state, detail)
    except Unexpected as e:
        res = f"unexpected:{e}"
    except (OSError, RuntimeError) as e:
        res = f"failed:{e}"
    if res == BulkItemResult.KEPT:
        log(f"{'kept:':<10} {describe_paths(b.ctx, item)}: {kept_text(item, detail)}")
        return
    record_result(b, item, (state, detail), res, done, kept, deferred, problems)
    if item.flatpak is not None and b.tree.kind(b.ctx.target(item)) == LstatKind.DIR:
        allow_flatpak(b, item, item.flatpak, problems)


def record_result(
    b: Bulk,
    item: Item,
    classified: tuple[str, str],
    res: str,
    done: set[str],
    kept: set[str],
    deferred: list[str],
    problems: list[str],
) -> None:
    """What act() did with an item (classified: its state and detail): logged, and noted in done, deferred or problems."""
    state, detail = classified
    log, name = b.out.log, describe_paths(b.ctx, item)
    match res:
        case BulkItemResult.OK:
            note_item_done(b, item, classified, done, kept)
        case BulkItemResult.BUSY:
            deferred.append(item.id)
            log(f"{'busy:':<10} {name}: moved at the next boot, before login")
        case BulkItemResult.CHANGED:
            deferred.append(item.id)
            log(f"{'busy:':<10} {name}: written to during the move; undone, moved at the next boot")
        case BulkItemResult.WAIT:
            if item.after in deferred:
                deferred.append(item.id)
            log(f"{'waits:':<10} {name}: {detail}")
        case _:
            why = res.split(":", 1)[1]
            problems.append(item.id)
            if state == BulkItemState.NOCOW:
                b.bulk_only.add(item.id)
                why += "; inside ~/Bulk only, no data of it in /home: snapshots do not wait for it"
            log(f"NEEDS A LOOK: {name}: {why} (nothing touched)")


def note_item_done(b: Bulk, item: Item, classified: tuple[str, str], done: set[str], kept: set[str]) -> None:
    """record_result for an item act() brought in place: in done once classify agrees; logged unless it was already."""
    state, detail = classified
    after, _ = classify(b, item, done, kept)
    if after == BulkItemState.OK:
        done.add(item.id)
    if state != BulkItemState.OK:
        absent = (
            f"  (folder only: ~/{os.path.dirname(item.link or '')} absent)" if detail == BulkItemDetail.NOLINK else ""
        )
        b.out.log(f"{'done:':<10} {describe_paths(b.ctx, item)}{absent}")


def allow_flatpak(b: Bulk, item: Item, app: str, problems: list[str]) -> None:
    """The Flatpak app of an item may use its folder in ~/Bulk (a problem added when that fails)."""
    T = b.ctx.target(item)
    try:
        if b.user.flatpak(app, T) == FlatpakAccess.ADDED:
            b.out.log(f"{'done:':<10} flatpak override --user --filesystem={T} {app}")
    except (OSError, RuntimeError) as e:
        problems.append(item.id)
        b.out.log(f"NEEDS A LOOK: {app}: {e}")


def row(b: Bulk, name: str, text: str) -> None:
    """One line of plan/status; a long name gets its own line."""
    b.out.say(row_text(name, text))


def item_text(b: Bulk, item: Item, state: str, detail: str) -> str:
    ctx, du = b.ctx, b.tree.du_bytes
    T, L = ctx.target(item), ctx.link(item)
    parent = os.path.dirname(item.link or "")
    if state == BulkItemState.OK:
        return "ok" + (
            f"  (no link: ~/{parent} absent; linked once it exists)" if detail == BulkItemDetail.NOLINK else ""
        )
    if state == BulkItemState.MKDIR and detail == BulkItemDetail.NOLINK:
        return f"create the folder (no link: ~/{parent} absent)"
    if state == BulkItemState.MOVE:
        return move_text(b, item, detail, present(L, THE_LINK))
    if state == BulkItemState.NOCOW:
        return f"rewrite without copy-on-write ({detail}, {du_text(du(T))})"
    if state == BulkItemState.UNEXPECTED:
        size = du(L) if L is not None and b.tree.kind(L) == LstatKind.DIR else TreeSize(0, True)
        there = f"; {du_text(size)} there, still in /home snapshots" if size.total >= BIG else ""
        return f"NEEDS A LOOK: {detail} (nothing touched{there})"
    return STATE_TEXT.get(state, state) + (f" ({detail})" if detail else "")


def move_text(b: Bulk, item: Item, detail: str, path: str) -> str:
    """item_text for an item whose folder (at path) moves into ~/Bulk."""
    du = b.tree.du_bytes
    if detail == BulkItemDetail.EMPTY:
        return "empty: replaced by the link"
    how = "real copy into a +C folder" if item.nocow else "reflink clone, no data written"
    stale = (
        f"a real folder again (its tool's clean command removed the link): delete the stale "
        f"{du_text(du(b.ctx.target(item)))} in ~/Bulk, then "
        if detail == BulkItemDetail.STALE
        else ""
    )
    return f"{stale}move {du_text(du(path))} into ~/Bulk ({how}), then the link"


def report(b: Bulk, mode: str) -> int:
    """plan / status text. Returns the number of items not in place."""
    ctx, state, say = b.ctx, b.state, b.out.say
    flagged = set(state.read_flag())
    bs = b.tree.bulk_state(ctx)
    left = report_bulk(b, bs) + report_journals(b)
    try:
        kept, unknown = state.read_keep()
    except OSError as e:
        left += 1
        kept, unknown = set(), []
        say(f"  NEEDS A LOOK: {b.paths.keep} cannot be read ({e}): apply touches no item until it is fixed")
    for ident in unknown:
        say(f"  {b.paths.keep}: unknown item '{ident}' (ignored)")
    # Before ~/Bulk exists nothing waits for anything.
    done: set[str] = set() if bs == BulkDirState.OK else {i.id for i in ITEMS}
    for item in ITEMS:
        left += report_item(b, item, mode, (done, kept, flagged))
    if "steam" not in kept:
        old_steam(b)
    if "bulk" in flagged:
        say("  ~/Bulk itself was busy: converted at the next boot, before login")
    return left


def report_bulk(b: Bulk, bs: str) -> int:
    """report's lines on ~/Bulk itself (bs: its bulk_state) and its CACHEDIR.TAG; 1 if it is not in place."""
    b.out.say("~/Bulk: everything in it can be downloaded again: no snapshots, no NAS backup.")
    btxt: dict[str, str] = {
        BulkDirState.OK: "ok (subvolume)",
        BulkDirState.CREATE: "create (a subvolume, yours)",
        BulkDirState.PLAIN: "a plain folder: converted into a subvolume first",
        BulkDirState.BAD: "NEEDS A LOOK: a file or symlink (nothing done)",
    }
    row(b, "~/Bulk", btxt[bs])
    if bs in (BulkDirState.OK, BulkDirState.PLAIN):
        tag = b.tree.tag_state(b.ctx)
        ttxt: dict[str, str] = {
            CacheTagState.OK: "ok",
            CacheTagState.CREATE: "create",
            CacheTagState.BAD: "NEEDS A LOOK: not a cache tag",
        }
        row(b, "~/Bulk/CACHEDIR.TAG", ttxt[tag])
    return int(bs != BulkDirState.OK)


def report_journals(b: Bulk) -> int:
    """report's lines on the journals of interrupted moves; how many there are."""
    ctx, state, say = b.ctx, b.state, b.out.say
    left = 0
    for item in ITEMS:
        if not state.jexists(item.id):
            continue
        left += 1
        j = state.jread_item(item.id)
        if j and j.get("conflict", False) and b.tree.kind(j["old"]) != LstatKind.ABSENT:
            say(f"  {item.id}: NEEDS A LOOK: {conflict_text(ctx, j)}")
        elif j:
            say(f"  {item.id}: an interrupted move is recorded ({ctx.short(j['old'] or j['src'])}): the next")
            say(
                "      apply (or boot) finishes or undoes it; "
                + (
                    "inside ~/Bulk only, snapshots do not wait"
                    if j.get("bulk_only", False)
                    else "snapshots wait until then"
                )
            )
        else:
            say(f"  {item.id}: NEEDS A LOOK: {state.newer_journal(item.id) or damaged(state, item.id)}")
    return left + report_steam_journals(b)


def report_steam_journals(b: Bulk) -> int:
    """report's lines on the journals of interrupted Steam game moves; how many there are."""
    state, say = b.state, b.out.say
    left = 0
    for ident in state.steam_journal_ids():
        left += 1
        sj = state.jread_steam(ident)
        if sj and sj.get("conflict", False):
            say(f"  {ident}: NEEDS A LOOK: {steam_conflict_text(b, sj)}")
        elif sj:
            say(f"  {ident}: an interrupted Steam game move is recorded (phase {sj.get('phase')}): the next")
            say("      apply, 'bulk steam apply' or boot finishes or undoes it; snapshots wait until then")
        else:
            say(f"  {ident}: NEEDS A LOOK: {state.newer_journal(ident) or damaged(state, ident)}")
    return left


def report_item(b: Bulk, item: Item, mode: str, seen: tuple[set[str], set[str], set[str]]) -> int:
    """report's lines on one item; 1 if it is not in place. seen: the ids in
    place so far (done, added to), kept by choice, and waiting for the boot."""
    done, kept, flagged = seen
    ctx = b.ctx
    st, detail = classify(b, item, done, kept)
    if st == BulkItemState.KEPT:
        row(b, describe_paths(ctx, item), kept_text(item, detail))
        return 0
    if st == BulkItemState.OK:
        done.add(item.id)
    text = item_text(b, item, st, detail)
    if item.id in flagged and st != BulkItemState.OK:
        text += "; waits for the next boot (busy)"
    row(b, describe_paths(ctx, item), text)
    T = ctx.target(item)
    if item.nocow and mode == BulkCommand.STATUS and b.tree.chain(ctx.bulk, item.target) == PathChain.OK:
        report_nocow(b, item, T)
    if item.flatpak is not None:
        report_flatpak(b, item.flatpak, T, mode)
    return int(st != BulkItemState.OK)


def report_nocow(b: Bulk, item: Item, target: str) -> None:
    """status's +C line of a NOCOW item whose target exists."""
    files, dirs_cow = b.tree.cow_data(target)
    parents = b.tree.nocow_parents(b.ctx, item)
    nc = ("yes" if not files and not dirs_cow else f"NO ({files} copy-on-write file(s))") + (
        f"; NO on {', '.join(b.ctx.short(p) for p in parents)}" if parents else ""
    )
    row(b, "", f"+C (NOCOW): {nc}")


def report_flatpak(b: Bulk, app: str, target: str, mode: str) -> None:
    """report's line on whether the Flatpak app may use target."""
    try:
        f = b.user.flatpak_state(app, target)
    except OSError, RuntimeError:
        f = "?"
    ftxt = {
        FlatpakAccess.ABSENT: "app not installed (nothing to do)",
        FlatpakAccess.OK: "ok",
        "?": "? (flatpak not usable here)",
        FlatpakAccess.MISSING: "add" if mode == BulkCommand.PLAN else "MISSING (run bulk apply)",
    }[f]
    row(b, f"  flatpak override: {app} may use it", ftxt)


def old_steam(b: Bulk) -> None:
    """The default Steam library stays in /home (snapshotted) until its games
    move into the Bulk library (`bulk steam apply`, or Steam's settings)."""
    ctx, t = b.ctx, b.tree
    p = os.path.join(ctx.home, STEAMAPPS, "common")
    if t.kind(p) != LstatKind.DIR or t.chain(ctx.home, STEAMAPPS + "/common") != PathChain.OK:
        return
    if t.is_subvol(os.path.dirname(p)):
        row(b, "~/" + STEAMAPPS, "a subvolume (earlier setup): left out of snapshots")
        return
    size = t.du_bytes(p)
    if size.total >= BIG:
        row(b, "~/" + STEAMAPPS, f"still {du_text(size)} of games here, in /home snapshots: move them to")
        row(b, "", "~/Bulk/steam with Steam closed: 'cst btrfs bulk steam plan', then 'bulk steam apply'")
        row(b, "", "(btrfs/SNAPSHOTS.md, section 3)")
