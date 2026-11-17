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
from btrfs.bulk.application.steam import steam_conflict_text, steam_recover_all
from btrfs.bulk.domain import (
    BIG,
    BY_ID,
    ITEMS,
    MARGIN,
    STATE_TEXT,
    STEAMAPPS,
    Item,
    ItemJournal,
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

THE_LINK = "the link"  # what present() names when an item has no link


def classify(b: Bulk, item: Item, done: set[str], kept: set[str] | None = None) -> tuple[str, str]:
    """(state, detail). done: ids already in place this run (for `after`);
    kept: ids kept in /home by choice (None: read from the keep file).
    States: kept, ok, mkdir, link, move, move-subvol, nocow, wait, unexpected;
    ok and mkdir carry 'nolink' in detail when the tool's parent folder is
    absent; kept carries 'linked' when the link into ~/Bulk is still there."""
    ctx, t = b.ctx, b.tree
    if kept is None:
        kept = b.state.read_keep()[0]
    if item.id in kept:
        L = ctx.link(item)
        linked = L is not None and t.kind(L) == "link" and t.link_target(L) == ctx.target(item)
        return "kept", "linked" if linked else ""
    if item.after and item.after in kept:
        why = f"~/{BY_ID[item.after].link} is kept in /home (by choice)"
        L = ctx.link(item)
        if L and item.link and t.chain(ctx.home, item.link) == "ok" and not t.is_subvol(L) and not t.empty_dir(L):
            # Its data would stay in /home snapshots without anyone deciding so.
            return "unexpected", (
                f"{why}, so this cannot move into ~/Bulk and its data stays in /home: "
                f"'bulk keep {item.id}' too, or 'bulk unkeep {item.after}'"
            )
        return "wait", f"{why}: keep {item.id} too, or 'bulk unkeep {item.after}'"
    if item.after and item.after not in done:
        return "wait", f"after ~/{BY_ID[item.after].link} is in place"
    ts = t.target_state(ctx, item)
    if ts.startswith("bad:"):
        return "unexpected", ts[4:]
    T = ctx.target(item)
    if item.link is None:
        return ("mkdir" if ts == "absent" else "ok"), ""
    L = present(ctx.link(item), THE_LINK)
    parent = os.path.dirname(item.link)
    c = t.chain(ctx.home, parent)
    if c == "absent":
        if ts == "absent":
            return "mkdir", "nolink"
        return nocow_check(b, item, ts) or ("ok", "nolink")
    if c != "ok":
        what, p = c.split(":", 1)
        return "unexpected", f"{ctx.short(p)} is a {'symlink' if what == 'symlink' else 'file'}, not a folder"
    k = t.kind(L)
    if k == "absent":
        return "link", ""
    if k == "link":
        if t.link_target(L) != T:
            return "unexpected", f"symlink to {t.readlink(L)} (expected {ctx.short(T)})"
        if ts == "absent":
            return "mkdir", ""
        return nocow_check(b, item, ts) or ("ok", "")
    if k == "other":
        return "unexpected", "a file, not a folder"
    if t.has_mount(L):
        return "unexpected", "is or contains a mount point"
    if not t.is_subvol(L) and t.empty_dir(L):
        return "move", "empty"  # nothing to lose, also next to a filled target
    if ts == "data":
        if item.disposable and not t.is_subvol(L):
            return "move", "stale"  # the tool replaced its link: the ~/Bulk side is stale
        return "unexpected", f"data in both ~/{item.link} and {ctx.short(T)}"
    if t.is_subvol(L):
        return ("move", "subvolume") if item.nocow else ("move-subvol", "")
    return "move", ""


def nocow_check(b: Bulk, item: Item, ts: str) -> tuple[str, str] | None:
    """For a NOCOW item whose target exists (ts: empty or data): ('nocow',
    why) when it, a folder or file in it, or a folder above it up to
    ~/Bulk/NOCOW_TOP lacks +C; None when all is in place (or not NOCOW)."""
    if not item.nocow:
        return None
    ctx = b.ctx
    T = ctx.target(item)
    if ts == "data":
        files, dirs_cow = b.tree.cow_data(T)
        if files:
            return "nocow", f"{files} file(s) still copy-on-write"
        if dirs_cow:
            return "nocow", "folders without +C"
    elif not b.tree.is_nocow(T):
        return "nocow", "folder without +C"
    parents = b.tree.nocow_parents(ctx, item)
    if parents:
        return "nocow", f"folder {ctx.short(parents[0])} without +C"
    return None


def committed(b: Bulk, j: ItemJournal) -> bool:
    """Did the switch of journal j complete (the target holds the data)?"""
    kind = b.tree.kind
    if j["kind"] == "subvol":
        return kind(present(j["link"], "the journal's link")) == "link"
    if j["link"]:
        return kind(j["link"]) == "link" and b.tree.link_target(j["link"]) == j["target"]
    # In-place rewrite of the target (no link): new renamed onto it, old kept.
    return kind(j["new"]) == "absent" and kind(j["target"]) == "dir" and kind(j["old"]) != "absent"


def recover(b: Bulk) -> list[str]:
    """Finish or undo what an interrupted run left (kill, power loss).
    Returns the ids whose journal stays (they need a look)."""
    ctx, kind, state, user, log = b.ctx, b.tree.kind, b.state, b.user, b.out.log
    stuck: list[str] = []
    for item in ITEMS:
        j = state.jread_item(item.id)
        if not j:
            if state.jexists(item.id):
                stuck.append(item.id)
                log(f"NEEDS A LOOK: {item.id}: unreadable journal {state.jpath(item.id)}")
            continue
        try:
            if j.get("conflict"):
                if kind(j["old"]) != "absent":
                    raise Unexpected(conflict_text(ctx, j))
                log(f"  {item.id}: the old tree is gone: the move is complete")
            elif j.get("finishing"):
                # The last check had passed and the old tree was being deleted:
                # what is left of it is a partial copy (each deletion changes
                # ctimes there, so the late-write check cannot be made again).
                user.finish(j)
                log(f"  {item.id}: deleted the old copy left by an interrupted run (its deletion was cut short)")
            elif j["kind"] == "subvol":
                L, T = present(j["link"], "the journal's link"), j["target"]
                if kind(L) == "absent" and kind(T) == "dir":
                    user.symlink(L, T)
                    log(f"  {item.id}: finished an interrupted move")
                elif not (kind(L) == "link" and b.tree.link_target(L) == T) and b.tree.entries_nofollow(T):
                    # Moved, then the old path was created again (an app)
                    # before the link: the moved subvolume is the full data.
                    raise Unexpected(
                        f"{L} was created again after its data was moved to {T}: move what you need "
                        f"from {L} into {T}, delete {L}, then run bulk apply again (it makes the link)"
                    )
            elif not j.get("undo") and committed(b, j):
                # The same last check as right after a switch (migrate): a
                # write into the old tree in the instant of the switch (or the
                # crash) must not be deleted with it.
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
            else:
                user.rollback(j)
                log(f"  {item.id}: undid an interrupted move (the data is back at {ctx.short(j['src'])})")
            state.jdrop(item.id)
        except (OSError, RuntimeError, Unexpected) as e:
            stuck.append(item.id)
            wait = "" if j.get("bulk_only") else "; snapshots wait until it is gone"
            log(f"NEEDS A LOOK: {item.id}: an interrupted move: {e} (journal kept in {state.jpath(item.id)}{wait})")
    return stuck + steam_recover_all(b)


# ---- the actions ------------------------------------------------------------------------------
def migrate(b: Bulk, item: Item, src: str, link: str | None) -> str:
    """Move src into the item's target by copy + switch; with link, src then
    becomes a symlink to it. Returns ok | busy | changed."""
    ctx, user, state, busy = b.ctx, b.user, b.state, b.host.busy_paths
    T = ctx.target(item)
    new = T + ".bulk-new"
    old = f"{src}.bulk-old-{time.strftime('%Y%m%dT%H%M%S')}"
    if busy([src], ctx.home):
        return "busy"
    if item.nocow:
        # A real copy: the data exists twice until the old tree is deleted.
        size = b.tree.du_bytes(src)
        if b.tree.free_bytes(ctx.bulk) < size.total + MARGIN:
            raise RuntimeError(f"not enough free space for a {du_text(size)} copy (plus {gib(MARGIN)} kept free)")
    user.mkdirs(ctx.bulk, os.path.dirname(item.target), nocow=item.nocow)
    stamp = user.prepare(src, new, nocow=item.nocow)
    j: ItemJournal = {
        "kind": "copy",
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
        return "busy"
    # The boot service must run if this is interrupted (it recovers before
    # login, before apps recreate the old path), and snapshots wait meanwhile.
    state.flag_add(item.id)
    # The clock before the switch, kept in the journal: a repair after a crash
    # makes the same last check as below (was the copy written to since?).
    j["stamp2"] = user.clock(new)
    state.jwrite(item.id, j)
    if not user.commit(j):
        state.jdrop(item.id)
        return "changed"
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
        return "changed"
    # Past the last check: from here on the old tree is only being deleted (a
    # kill in the middle must not make a repair take the half-deleted tree for
    # a late write and roll back onto it).
    j["finishing"] = True
    state.jwrite(item.id, j)
    user.finish(j)
    state.jdrop(item.id)
    return "ok"


def act(b: Bulk, item: Item, state: str, detail: str) -> str:
    """Apply one item; returns ok | busy | changed | unexpected:<why> | wait."""
    ctx, user, busy = b.ctx, b.user, b.host.busy_paths
    T, L = ctx.target(item), ctx.link(item)
    if state == "kept":
        return "kept"
    if state in ("ok", "mkdir"):
        user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
        return "ok"
    if state == "link":
        L = present(L, THE_LINK)
        user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
        user.symlink(L, T)
        return "ok"
    if state == "move":
        L = present(L, THE_LINK)
        if detail == "empty":
            if busy([L], ctx.home):
                return "busy"
            user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
            user.rmdir(L)
            user.symlink(L, T)
            return "ok"
        if detail == "stale":
            if busy([L, T], ctx.home):
                return "busy"
            b.out.log(
                f"  {item.id}: ~/{item.link} is a real folder again (its tool's clean command removed the "
                f"link): deleting the stale copy in {ctx.short(T)} (a cache: downloaded again when needed)"
            )
            user.rmtree(T)
        user.mkdirs(ctx.bulk, item.target, nocow=item.nocow)
        return migrate(b, item, L, L)
    if state == "move-subvol":
        L = present(L, THE_LINK)
        if busy([L], ctx.home):
            return "busy"
        user.mkdirs(ctx.bulk, os.path.dirname(item.target), nocow=False)
        b.state.flag_add(item.id)
        b.state.jwrite(item.id, {"kind": "subvol", "src": L, "target": T, "new": "", "old": "", "link": L, "stamp": 0})
        user.move_subvol(L, T)
        b.state.jdrop(item.id)
        return "ok"
    if state == "nocow":
        # The folders above it (~/Bulk/emulators...) get +C in any case.
        user.mkdirs(ctx.bulk, os.path.dirname(item.target), nocow=True)
        if detail.startswith("folder"):  # no data: +C set in place
            user.set_nocow_dirs(T)
            return "ok"
        return migrate(b, item, T, None)
    if state == "wait":
        return "wait"
    return "unexpected:" + detail


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
    ctx, user, log = b.ctx, b.user, b.out.log
    deferred: list[str] = []
    problems: list[str] = []
    b.bulk_only.clear()
    # Before taking the lock: btrfs-snapshot convert-one takes it itself.
    if b.tree.bulk_state(ctx) == "plain":
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
        bs = b.tree.bulk_state(ctx)
        if bs == "bad":
            log(f"{ctx.short(ctx.bulk)} is a file or a symlink: nothing done (move it away, then run again)")
            return [], ["bulk"]
        if bs == "create":
            try:
                user.create_bulk(ctx.bulk)
            except (OSError, RuntimeError) as e:
                log(f"cannot create ~/Bulk as a subvolume ({e}): nothing done")
                return [], ["bulk"]
            log("created:   ~/Bulk (a subvolume, yours: left out of /home snapshots)")
        # Every run (cheap): also a ~/Bulk made by an older run or by hand.
        user.relabel(ctx.bulk)
        if not b.tree.is_subvol(ctx.bulk):
            log("~/Bulk is not a subvolume after creating it: stopped")
            return [], ["bulk"]
        t = b.tree.tag_state(ctx)
        if t == "create":
            try:
                user.write_tag(ctx.bulk)
                log("created:   ~/Bulk/CACHEDIR.TAG")
            except (OSError, RuntimeError) as e:
                problems.append("CACHEDIR.TAG")
                log(f"NEEDS A LOOK: ~/Bulk/CACHEDIR.TAG: {e}")
        elif t == "bad":
            problems.append("CACHEDIR.TAG")
            log("NEEDS A LOOK: ~/Bulk/CACHEDIR.TAG is not a cache tag (left as it is)")
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
            state, detail = "", ""
            if item.id in stuck:
                log(f"{'skipped:':<10} {describe_paths(ctx, item)}: its interrupted move needs a look (above)")
                continue
            try:
                state, detail = classify(b, item, done, kept)
                res = act(b, item, state, detail)
            except Unexpected as e:
                res = f"unexpected:{e}"
            except (OSError, RuntimeError) as e:
                res = f"failed:{e}"
            name = describe_paths(ctx, item)
            if res == "kept":
                log(f"{'kept:':<10} {name}: {kept_text(item, detail)}")
                continue
            if res == "ok":
                after, _ = classify(b, item, done, kept)
                if after == "ok":
                    done.add(item.id)
                if state != "ok":
                    log(
                        f"{'done:':<10} {name}{'  (folder only: ~/' + os.path.dirname(item.link or '') + ' absent)' if detail == 'nolink' else ''}"
                    )
            elif res == "busy":
                deferred.append(item.id)
                log(f"{'busy:':<10} {name}: moved at the next boot, before login")
            elif res == "changed":
                deferred.append(item.id)
                log(f"{'busy:':<10} {name}: written to during the move; undone, moved at the next boot")
            elif res == "wait":
                if item.after in deferred:
                    deferred.append(item.id)
                log(f"{'waits:':<10} {name}: {detail}")
            else:
                why = res.split(":", 1)[1]
                problems.append(item.id)
                if state == "nocow":
                    b.bulk_only.add(item.id)
                    why += "; inside ~/Bulk only, no data of it in /home: snapshots do not wait for it"
                log(f"NEEDS A LOOK: {name}: {why} (nothing touched)")
            if item.flatpak and b.tree.kind(ctx.target(item)) == "dir":
                try:
                    f = user.flatpak(item.flatpak, ctx.target(item))
                    if f == "added":
                        log(f"{'done:':<10} flatpak override --user --filesystem={ctx.target(item)} {item.flatpak}")
                except (OSError, RuntimeError) as e:
                    problems.append(item.id)
                    log(f"NEEDS A LOOK: {item.flatpak}: {e}")
    finally:
        b.state.release(lock)
    b.state.write_flag(deferred)
    return deferred, problems


def row(b: Bulk, name: str, text: str) -> None:
    """One line of plan/status; a long name gets its own line."""
    b.out.say(row_text(name, text))


def item_text(b: Bulk, item: Item, state: str, detail: str, mode: str) -> str:
    ctx, du = b.ctx, b.tree.du_bytes
    T, L = ctx.target(item), ctx.link(item)
    parent = os.path.dirname(item.link or "")
    if state == "ok":
        return "ok" + (f"  (no link: ~/{parent} absent; linked once it exists)" if detail == "nolink" else "")
    if state == "mkdir" and detail == "nolink":
        return f"create the folder (no link: ~/{parent} absent)"
    if state == "move":
        L = present(L, THE_LINK)
        if detail == "empty":
            return "empty: replaced by the link"
        how = "real copy into a +C folder" if item.nocow else "reflink clone, no data written"
        stale = (
            f"a real folder again (its tool's clean command removed the link): delete the stale "
            f"{du_text(du(T))} in ~/Bulk, then "
            if detail == "stale"
            else ""
        )
        return f"{stale}move {du_text(du(L))} into ~/Bulk ({how}), then the link"
    if state == "nocow":
        return f"rewrite without copy-on-write ({detail}, {du_text(du(T))})"
    if state == "unexpected":
        size = du(L) if L and b.tree.kind(L) == "dir" else TreeSize(0, True)
        there = f"; {du_text(size)} there, still in /home snapshots" if size.total >= BIG else ""
        return f"NEEDS A LOOK: {detail} (nothing touched{there})"
    return STATE_TEXT.get(state, state) + (f" ({detail})" if detail else "")


def report(b: Bulk, mode: str) -> int:
    """plan / status text. Returns the number of items not in place."""
    ctx, t, state, say = b.ctx, b.tree, b.state, b.out.say
    left = 0
    flagged = set(state.read_flag())
    bs = t.bulk_state(ctx)
    say("~/Bulk: everything in it can be downloaded again: no snapshots, no NAS backup.")
    btxt = {
        "ok": "ok (subvolume)",
        "create": "create (a subvolume, yours)",
        "plain": "a plain folder: converted into a subvolume first",
        "bad": "NEEDS A LOOK: a file or symlink (nothing done)",
    }[bs]
    left += bs != "ok"
    row(b, "~/Bulk", btxt)
    if bs in ("ok", "plain"):
        tag = t.tag_state(ctx)
        ttxt = {"ok": "ok", "create": "create", "bad": "NEEDS A LOOK: not a cache tag"}[tag]
        row(b, "~/Bulk/CACHEDIR.TAG", ttxt)
    for item in ITEMS:
        if not state.jexists(item.id):
            continue
        left += 1
        j = state.jread_item(item.id)
        if j and j.get("conflict") and t.kind(j["old"]) != "absent":
            say(f"  {item.id}: NEEDS A LOOK: {conflict_text(ctx, j)}")
        elif j:
            say(f"  {item.id}: an interrupted move is recorded ({ctx.short(j['old'] or j['src'])}): the next")
            say(
                "      apply (or boot) finishes or undoes it; "
                + ("inside ~/Bulk only, snapshots do not wait" if j.get("bulk_only") else "snapshots wait until then")
            )
        else:
            say(f"  {item.id}: NEEDS A LOOK: unreadable journal {state.jpath(item.id)}")
    for ident in state.steam_journal_ids():
        left += 1
        sj = state.jread_steam(ident)
        if sj and sj.get("conflict"):
            say(f"  {ident}: NEEDS A LOOK: {steam_conflict_text(b, sj)}")
        elif sj:
            say(f"  {ident}: an interrupted Steam game move is recorded (phase {sj.get('phase')}): the next")
            say("      apply, 'bulk steam apply' or boot finishes or undoes it; snapshots wait until then")
        else:
            say(f"  {ident}: NEEDS A LOOK: unreadable journal {state.jpath(ident)}")
    try:
        kept, unknown = state.read_keep()
    except OSError as e:
        left += 1
        kept, unknown = set(), []
        say(f"  NEEDS A LOOK: {b.paths.keep} cannot be read ({e}): apply touches no item until it is fixed")
    for ident in unknown:
        say(f"  {b.paths.keep}: unknown item '{ident}' (ignored)")
    # Before ~/Bulk exists nothing waits for anything.
    done: set[str] = set() if bs == "ok" else {i.id for i in ITEMS}
    for item in ITEMS:
        st, detail = classify(b, item, done, kept)
        if st == "kept":
            row(b, describe_paths(ctx, item), kept_text(item, detail))
            continue
        if st == "ok":
            done.add(item.id)
        else:
            left += 1
        text = item_text(b, item, st, detail, mode)
        if item.id in flagged and st != "ok":
            text += "; waits for the next boot (busy)"
        row(b, describe_paths(ctx, item), text)
        T = ctx.target(item)
        if item.nocow and mode == "status" and t.chain(ctx.bulk, item.target) == "ok":
            files, dirs_cow = t.cow_data(T)
            parents = t.nocow_parents(ctx, item)
            nc = ("yes" if not files and not dirs_cow else f"NO ({files} copy-on-write file(s))") + (
                f"; NO on {', '.join(ctx.short(p) for p in parents)}" if parents else ""
            )
            row(b, "", f"+C (NOCOW): {nc}")
        if item.flatpak:
            try:
                f = b.user.flatpak_state(item.flatpak, T)
            except OSError, RuntimeError:
                f = "?"
            ftxt = {
                "absent": "app not installed (nothing to do)",
                "ok": "ok",
                "?": "? (flatpak not usable here)",
                "missing": "add" if mode == "plan" else "MISSING (run bulk apply)",
            }[f]
            row(b, f"  flatpak override: {item.flatpak} may use it", ftxt)
    if "steam" not in kept:
        old_steam(b)
    if "bulk" in flagged:
        say("  ~/Bulk itself was busy: converted at the next boot, before login")
    return left


def old_steam(b: Bulk) -> None:
    """The default Steam library stays in /home (snapshotted) until its games
    move into the Bulk library (`bulk steam apply`, or Steam's settings)."""
    ctx, t = b.ctx, b.tree
    p = os.path.join(ctx.home, STEAMAPPS, "common")
    if t.kind(p) != "dir" or t.chain(ctx.home, STEAMAPPS + "/common") != "ok":
        return
    if t.is_subvol(os.path.dirname(p)):
        row(b, "~/" + STEAMAPPS, "a subvolume (earlier setup): left out of snapshots")
        return
    size = t.du_bytes(p)
    if size.total >= BIG:
        row(b, "~/" + STEAMAPPS, f"still {du_text(size)} of games here, in /home snapshots: move them to")
        row(b, "", "~/Bulk/steam with Steam closed: 'cst btrfs bulk steam plan', then 'bulk steam apply'")
        row(b, "", "(btrfs/SNAPSHOTS.md, section 3)")
