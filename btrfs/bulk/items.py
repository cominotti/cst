# SPDX-License-Identifier: MPL-2.0
"""The ~/Bulk items: the state of each one (classify), the move of its
data into ~/Bulk and the link left behind, the recovery of interrupted
moves, apply and the plan/status report.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import os
import subprocess
import time

from cst_core.adapters.files import locale_bytes, write_atomic
from btrfs.bulk.adapters import (
    CONVERT_PLAN,
    Ctx,
    KEEP,
    as_user,
    bulk_state,
    busy_paths,
    chain,
    cow_data,
    du_bytes,
    entries_nofollow,
    empty_dir,
    flag_add,
    free_bytes,
    has_mount,
    is_nocow,
    is_subvol,
    jdrop,
    jpath,
    jread,
    jwrite,
    kind,
    link_target,
    log,
    nocow_parents,
    read_flag,
    read_keep,
    run_lock,
    snapshot_program,
    steam_journal_ids,
    tag_state,
    target_state,
    u_clock,
    u_commit,
    u_create_bulk,
    u_finish,
    u_flatpak,
    u_flatpak_state,
    u_late_write,
    u_mkdirs,
    u_move_subvol,
    u_new_written,
    u_prepare,
    u_relabel,
    u_rmtree,
    u_rollback,
    u_set_nocow_dirs,
    u_symlink,
    u_write_tag,
    write_flag,
)
from btrfs.bulk.domain import (
    BIG,
    BULK_ONLY,
    BY_ID,
    ITEMS,
    Item,
    MARGIN,
    STATE_TEXT,
    STEAMAPPS,
    TreeSize,
    Unexpected,
    gib,
    kept_text,
    du_text,
)
from btrfs.bulk.steam import steam_conflict_text, steam_recover_all


def classify(ctx: Ctx, item: Item, done: set[str], kept: set[str] | None = None) -> tuple[str, str]:
    """(state, detail). done: ids already in place this run (for `after`);
    kept: ids kept in /home by choice (None: read from KEEP).
    States: kept, ok, mkdir, link, move, move-subvol, nocow, wait, unexpected;
    ok and mkdir carry 'nolink' in detail when the tool's parent folder is
    absent; kept carries 'linked' when the link into ~/Bulk is still there."""
    if kept is None:
        kept = read_keep()[0]
    if item.id in kept:
        L = ctx.link(item)
        linked = L is not None and kind(L) == "link" and link_target(L) == ctx.target(item)
        return "kept", "linked" if linked else ""
    if item.after and item.after in kept:
        why = f"~/{BY_ID[item.after].link} is kept in /home (by choice)"
        L = ctx.link(item)
        if L and item.link and chain(ctx.home, item.link) == "ok" and not is_subvol(L) and not empty_dir(L):
            # Its data would stay in /home snapshots without anyone deciding so.
            return "unexpected", (
                f"{why}, so this cannot move into ~/Bulk and its data stays in /home: "
                f"'bulk keep {item.id}' too, or 'bulk unkeep {item.after}'"
            )
        return "wait", f"{why}: keep {item.id} too, or 'bulk unkeep {item.after}'"
    if item.after and item.after not in done:
        return "wait", f"after ~/{BY_ID[item.after].link} is in place"
    ts = target_state(ctx, item)
    if ts.startswith("bad:"):
        return "unexpected", ts[4:]
    T = ctx.target(item)
    if item.link is None:
        return ("mkdir" if ts == "absent" else "ok"), ""
    L = ctx.link(item)
    assert L is not None
    parent = os.path.dirname(item.link)
    c = chain(ctx.home, parent)
    if c == "absent":
        if ts == "absent":
            return "mkdir", "nolink"
        return nocow_check(ctx, item, ts) or ("ok", "nolink")
    if c != "ok":
        what, p = c.split(":", 1)
        return "unexpected", f"{ctx.short(p)} is a {'symlink' if what == 'symlink' else 'file'}, not a folder"
    k = kind(L)
    if k == "absent":
        return "link", ""
    if k == "link":
        if link_target(L) != T:
            return "unexpected", f"symlink to {os.readlink(L)} (expected {ctx.short(T)})"
        if ts == "absent":
            return "mkdir", ""
        return nocow_check(ctx, item, ts) or ("ok", "")
    if k == "other":
        return "unexpected", "a file, not a folder"
    if has_mount(L):
        return "unexpected", "is or contains a mount point"
    if not is_subvol(L) and empty_dir(L):
        return "move", "empty"  # nothing to lose, also next to a filled target
    if ts == "data":
        if item.disposable and not is_subvol(L):
            return "move", "stale"  # the tool replaced its link: the ~/Bulk side is stale
        return "unexpected", f"data in both ~/{item.link} and {ctx.short(T)}"
    if is_subvol(L):
        return ("move", "subvolume") if item.nocow else ("move-subvol", "")
    return "move", ""


def nocow_check(ctx: Ctx, item: Item, ts: str) -> tuple[str, str] | None:
    """For a NOCOW item whose target exists (ts: empty or data): ('nocow',
    why) when it, a folder or file in it, or a folder above it up to
    ~/Bulk/NOCOW_TOP lacks +C; None when all is in place (or not NOCOW)."""
    if not item.nocow:
        return None
    T = ctx.target(item)
    if ts == "data":
        files, dirs_cow = cow_data(T)
        if files:
            return "nocow", f"{files} file(s) still copy-on-write"
        if dirs_cow:
            return "nocow", "folders without +C"
    elif not is_nocow(T):
        return "nocow", "folder without +C"
    parents = nocow_parents(ctx, item)
    if parents:
        return "nocow", f"folder {ctx.short(parents[0])} without +C"
    return None


def committed(j: Mapping[str, Any]) -> bool:
    """Did the switch of journal j complete (the target holds the data)?"""
    if j["kind"] == "subvol":
        return kind(j["link"]) == "link"
    if j["link"]:
        return kind(j["link"]) == "link" and link_target(j["link"]) == j["target"]
    # In-place rewrite of the target (no link): new renamed onto it, old kept.
    return kind(j["new"]) == "absent" and kind(j["target"]) == "dir" and kind(j["old"]) != "absent"


def conflict_text(ctx: Ctx, j: Mapping[str, Any]) -> str:
    return (
        f"written to on both sides during the move: compare {ctx.short(j['old'])} (the old "
        f"tree) with {ctx.short(j['target'])} (in use now), copy what you need into the "
        f"second, delete the first, then run bulk apply again"
        + ("; both inside ~/Bulk, no data of it in /home: snapshots do not wait for it" if j.get("bulk_only") else "")
    )


def recover(ctx: Ctx) -> list[str]:
    """Finish or undo what an interrupted run left (kill, power loss).
    Returns the ids whose journal stays (they need a look)."""
    stuck: list[str] = []
    for item in ITEMS:
        j = jread(item.id)
        if not j:
            if os.path.exists(jpath(item.id)):
                stuck.append(item.id)
                log(f"NEEDS A LOOK: {item.id}: unreadable journal {jpath(item.id)}")
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
                as_user(ctx, u_finish, j)
                log(f"  {item.id}: deleted the old copy left by an interrupted run (its deletion was cut short)")
            elif j["kind"] == "subvol":
                L, T = j["link"], j["target"]
                if kind(L) == "absent" and kind(T) == "dir":
                    as_user(ctx, u_symlink, L, T)
                    log(f"  {item.id}: finished an interrupted move")
                elif not (kind(L) == "link" and link_target(L) == T) and entries_nofollow(T):
                    # Moved, then the old path was created again (an app)
                    # before the link: the moved subvolume is the full data.
                    raise Unexpected(
                        f"{L} was created again after its data was moved to {T}: move what you need "
                        f"from {L} into {T}, delete {L}, then run bulk apply again (it makes the link)"
                    )
            elif not j.get("undo") and committed(j):
                # The same last check as right after a switch (migrate): a
                # write into the old tree in the instant of the switch (or the
                # crash) must not be deleted with it.
                if busy_paths([j["old"]], ctx.home) or as_user(ctx, u_late_write, j):
                    if "stamp2" not in j or as_user(ctx, u_new_written, j):
                        # Both sides written (or a journal of an older
                        # version, without stamp2: cannot tell): keep both.
                        j["conflict"] = True
                        jwrite(item.id, j)
                        raise Unexpected(conflict_text(ctx, j))
                    j["undo"] = True
                    jwrite(item.id, j)
                    as_user(ctx, u_rollback, j)
                    log(
                        f"  {item.id}: undid an interrupted move: the old folder was written to during the "
                        f"switch (the data is back at {ctx.short(j['src'])})"
                    )
                else:
                    j["finishing"] = True  # a kill now: the next repair goes on deleting
                    jwrite(item.id, j)
                    as_user(ctx, u_finish, j)
                    log(f"  {item.id}: deleted the old copy left by an interrupted run")
            else:
                as_user(ctx, u_rollback, j)
                log(f"  {item.id}: undid an interrupted move (the data is back at {ctx.short(j['src'])})")
            jdrop(item.id)
        except (OSError, RuntimeError, Unexpected) as e:
            stuck.append(item.id)
            wait = "" if j.get("bulk_only") else "; snapshots wait until it is gone"
            log(f"NEEDS A LOOK: {item.id}: an interrupted move: {e} (journal kept in {jpath(item.id)}{wait})")
    return stuck + steam_recover_all(ctx)


# ---- the actions ------------------------------------------------------------------------------
def migrate(ctx: Ctx, item: Item, src: str, link: str | None) -> str:
    """Move src into the item's target by copy + switch; with link, src then
    becomes a symlink to it. Returns ok | busy | changed."""
    T = ctx.target(item)
    new = T + ".bulk-new"
    old = f"{src}.bulk-old-{time.strftime('%Y%m%dT%H%M%S')}"
    if busy_paths([src], ctx.home):
        return "busy"
    if item.nocow:
        # A real copy: the data exists twice until the old tree is deleted.
        size = du_bytes(src)
        if free_bytes(ctx.bulk) < size.total + MARGIN:
            raise RuntimeError(f"not enough free space for a {du_text(size)} copy (plus {gib(MARGIN)} kept free)")
    as_user(ctx, u_mkdirs, ctx.bulk, os.path.dirname(item.target), item.nocow)
    stamp = as_user(ctx, u_prepare, src, new, item.nocow)
    j: dict[str, Any] = {
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
    if busy_paths([src], ctx.home):
        as_user(ctx, u_rmtree, new)
        return "busy"
    # The boot service must run if this is interrupted (it recovers before
    # login, before apps recreate the old path), and snapshots wait meanwhile.
    flag_add(item.id)
    # The clock before the switch, kept in the journal: a repair after a crash
    # makes the same last check as below (was the copy written to since?).
    j["stamp2"] = as_user(ctx, u_clock, new)
    jwrite(item.id, j)
    if not as_user(ctx, u_commit, j):
        jdrop(item.id)
        return "changed"
    # Anything that wrote into the old tree in the instant of the switch: undo,
    # unless the copy (in use through the link now) was written to as well.
    if busy_paths([old], ctx.home) or as_user(ctx, u_late_write, j):
        if as_user(ctx, u_new_written, j):
            j["conflict"] = True
            jwrite(item.id, j)
            raise Unexpected(conflict_text(ctx, j))
        j["undo"] = True  # a kill now: the recovery goes on undoing
        jwrite(item.id, j)
        as_user(ctx, u_rollback, j)
        jdrop(item.id)
        as_user(ctx, u_mkdirs, ctx.bulk, item.target, item.nocow)
        return "changed"
    # Past the last check: from here on the old tree is only being deleted (a
    # kill in the middle must not make a repair take the half-deleted tree for
    # a late write and roll back onto it).
    j["finishing"] = True
    jwrite(item.id, j)
    as_user(ctx, u_finish, j)
    jdrop(item.id)
    return "ok"


def act(ctx: Ctx, item: Item, state: str, detail: str) -> str:
    """Apply one item; returns ok | busy | changed | unexpected:<why> | wait."""
    T, L = ctx.target(item), ctx.link(item)
    if state == "kept":
        return "kept"
    if state in ("ok", "mkdir"):
        as_user(ctx, u_mkdirs, ctx.bulk, item.target, item.nocow)
        return "ok"
    if state == "link":
        assert L is not None
        as_user(ctx, u_mkdirs, ctx.bulk, item.target, item.nocow)
        as_user(ctx, u_symlink, L, T)
        return "ok"
    if state == "move":
        assert L is not None
        if detail == "empty":
            if busy_paths([L], ctx.home):
                return "busy"
            as_user(ctx, u_mkdirs, ctx.bulk, item.target, item.nocow)
            as_user(ctx, os.rmdir, L)
            as_user(ctx, u_symlink, L, T)
            return "ok"
        if detail == "stale":
            if busy_paths([L, T], ctx.home):
                return "busy"
            log(
                f"  {item.id}: ~/{item.link} is a real folder again (its tool's clean command removed the "
                f"link): deleting the stale copy in {ctx.short(T)} (a cache: downloaded again when needed)"
            )
            as_user(ctx, u_rmtree, T)
        as_user(ctx, u_mkdirs, ctx.bulk, item.target, item.nocow)
        return migrate(ctx, item, L, L)
    if state == "move-subvol":
        assert L is not None
        if busy_paths([L], ctx.home):
            return "busy"
        as_user(ctx, u_mkdirs, ctx.bulk, os.path.dirname(item.target), False)
        flag_add(item.id)
        jwrite(item.id, {"kind": "subvol", "src": L, "target": T, "new": "", "old": "", "link": L, "stamp": 0})
        as_user(ctx, u_move_subvol, L, T)
        jdrop(item.id)
        return "ok"
    if state == "nocow":
        # The folders above it (~/Bulk/emulators...) get +C in any case.
        as_user(ctx, u_mkdirs, ctx.bulk, os.path.dirname(item.target), True)
        if detail.startswith("folder"):  # no data: +C set in place
            as_user(ctx, u_set_nocow_dirs, T)
            return "ok"
        return migrate(ctx, item, T, None)
    if state == "wait":
        return "wait"
    return "unexpected:" + detail


def convert_bulk_dir(ctx: Ctx) -> bool:
    """~/Bulk exists as a plain folder: btrfs-snapshot turns it into a
    subvolume (its convert_one: reflink clone + switch, busy -> later)."""
    r = subprocess.run(
        ["bash", snapshot_program(), "convert-one", ctx.bulk], check=False, capture_output=True, text=True
    )
    for line in (r.stdout + r.stderr).splitlines():
        log("  " + line)
    return is_subvol(ctx.bulk)


def prune_convert_plan(ctx: Ctx) -> None:
    """An older snapshots setup planned some of these paths as nested
    subvolumes: they are symlinks into ~/Bulk now, never to be converted."""
    try:
        with open(CONVERT_PLAN) as f:
            lines = f.readlines()
    except OSError:
        return
    drop = {link for i in ITEMS if (link := ctx.link(i)) is not None}
    keep = [ln for ln in lines if ln.startswith("#") or not ln.strip() or ln.split(maxsplit=1)[-1].strip() not in drop]
    if keep != lines:
        # Encoded as it was read: in the locale's encoding (open()'s default).
        # Durable (file and folder): the boot conversion acts on it.
        write_atomic(CONVERT_PLAN, locale_bytes("".join(keep)), mode=0o644, relabel=False)
        log("  convert-plan: dropped the paths that now live in ~/Bulk")


def bulk_only_journal(ident: str) -> bool:
    j = jread(ident)
    return bool(j and j.get("bulk_only"))


def exit_status(deferred: Sequence[str], problems: Sequence[str]) -> int:
    """1 something needs a look, 4 only something inside ~/Bulk (the in-place
    +C rewrite: its journal, or a failure of it; no data of it in /home),
    3 only busy items, 0 done."""
    hard = [p for p in problems if not (problems.count(p) == 1 and (p in BULK_ONLY or bulk_only_journal(p)))]
    if hard:
        return 1
    if problems:
        return 4
    return 3 if deferred else 0


def apply(ctx: Ctx) -> tuple[list[str], list[str]]:
    """Bring every item to its declared state. Returns (deferred, problems)."""
    deferred: list[str] = []
    problems: list[str] = []
    BULK_ONLY.clear()
    # Before taking the lock: btrfs-snapshot convert-one takes it itself.
    if bulk_state(ctx) == "plain":
        log("~/Bulk is a plain folder: turning it into a subvolume first")
        if not convert_bulk_dir(ctx):
            log("~/Bulk: in use or failed (see above): everything waits for the next boot")
            write_flag(["bulk"])
            return ["bulk"], []
    lock = run_lock()
    try:
        stuck = recover(ctx)
        problems += stuck
        prune_convert_plan(ctx)
        b = bulk_state(ctx)
        if b == "bad":
            log(f"{ctx.short(ctx.bulk)} is a file or a symlink: nothing done (move it away, then run again)")
            return [], ["bulk"]
        if b == "create":
            try:
                as_user(ctx, u_create_bulk, ctx.bulk)
            except (OSError, RuntimeError) as e:
                log(f"cannot create ~/Bulk as a subvolume ({e}): nothing done")
                return [], ["bulk"]
            log("created:   ~/Bulk (a subvolume, yours: left out of /home snapshots)")
        # Every run (cheap): also a ~/Bulk made by an older run or by hand.
        as_user(ctx, u_relabel, ctx.bulk)
        if not is_subvol(ctx.bulk):
            log("~/Bulk is not a subvolume after creating it: stopped")
            return [], ["bulk"]
        t = tag_state(ctx)
        if t == "create":
            try:
                as_user(ctx, u_write_tag, ctx.bulk)
                log("created:   ~/Bulk/CACHEDIR.TAG")
            except (OSError, RuntimeError) as e:
                problems.append("CACHEDIR.TAG")
                log(f"NEEDS A LOOK: ~/Bulk/CACHEDIR.TAG: {e}")
        elif t == "bad":
            problems.append("CACHEDIR.TAG")
            log("NEEDS A LOOK: ~/Bulk/CACHEDIR.TAG is not a cache tag (left as it is)")
        done: set[str] = set()
        try:
            kept, unknown = read_keep()
        except OSError as e:
            problems.append("bulk-keep")
            log(
                f"NEEDS A LOOK: {KEEP} cannot be read ({e}): no item touched, since what you keep in "
                "/home is unknown (fix or delete that file, then run bulk apply again)"
            )
            return deferred, problems
        for ident in unknown:
            log(f"{KEEP}: unknown item '{ident}' (ignored; valid: {', '.join(BY_ID)})")
        for item in ITEMS:
            state, detail = "", ""
            if item.id in stuck:
                log(f"{'skipped:':<10} {describe_paths(ctx, item)}: its interrupted move needs a look (above)")
                continue
            try:
                state, detail = classify(ctx, item, done, kept)
                res = act(ctx, item, state, detail)
            except Unexpected as e:
                res = f"unexpected:{e}"
            except (OSError, RuntimeError) as e:
                res = f"failed:{e}"
            name = describe_paths(ctx, item)
            if res == "kept":
                log(f"{'kept:':<10} {name}: {kept_text(item, detail)}")
                continue
            if res == "ok":
                after, _ = classify(ctx, item, done, kept)
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
                    BULK_ONLY.add(item.id)
                    why += "; inside ~/Bulk only, no data of it in /home: snapshots do not wait for it"
                log(f"NEEDS A LOOK: {name}: {why} (nothing touched)")
            if item.flatpak and kind(ctx.target(item)) == "dir":
                try:
                    f = as_user(ctx, u_flatpak, item.flatpak, ctx.target(item))
                    if f == "added":
                        log(f"{'done:':<10} flatpak override --user --filesystem={ctx.target(item)} {item.flatpak}")
                except (OSError, RuntimeError) as e:
                    problems.append(item.id)
                    log(f"NEEDS A LOOK: {item.flatpak}: {e}")
    finally:
        os.close(lock)
    write_flag(deferred)
    return deferred, problems


def row(name: str, text: str) -> None:
    """One line of plan/status; a long name gets its own line."""
    if len(name) > 56:
        print(f"  {name}\n  {'':<56} {text}")
    else:
        print(f"  {name:<56} {text}")


def describe_paths(ctx: Ctx, item: Item) -> str:
    t = ctx.short(ctx.target(item))
    return f"~/{item.link} -> {t}" if item.link else t


def item_text(ctx: Ctx, item: Item, state: str, detail: str, mode: str) -> str:
    T, L = ctx.target(item), ctx.link(item)
    parent = os.path.dirname(item.link or "")
    if state == "ok":
        return "ok" + (f"  (no link: ~/{parent} absent; linked once it exists)" if detail == "nolink" else "")
    if state == "mkdir" and detail == "nolink":
        return f"create the folder (no link: ~/{parent} absent)"
    if state == "move":
        assert L is not None
        if detail == "empty":
            return "empty: replaced by the link"
        how = "real copy into a +C folder" if item.nocow else "reflink clone, no data written"
        stale = (
            f"a real folder again (its tool's clean command removed the link): delete the stale "
            f"{du_text(du_bytes(T))} in ~/Bulk, then "
            if detail == "stale"
            else ""
        )
        return f"{stale}move {du_text(du_bytes(L))} into ~/Bulk ({how}), then the link"
    if state == "nocow":
        return f"rewrite without copy-on-write ({detail}, {du_text(du_bytes(T))})"
    if state == "unexpected":
        size = du_bytes(L) if L and kind(L) == "dir" else TreeSize(0, True)
        there = f"; {du_text(size)} there, still in /home snapshots" if size.total >= BIG else ""
        return f"NEEDS A LOOK: {detail} (nothing touched{there})"
    return STATE_TEXT.get(state, state) + (f" ({detail})" if detail else "")


def report(ctx: Ctx, mode: str) -> int:
    """plan / status text. Returns the number of items not in place."""
    left = 0
    flagged = set(read_flag())
    b = bulk_state(ctx)
    print("~/Bulk: everything in it can be downloaded again: no snapshots, no NAS backup.")
    btxt = {
        "ok": "ok (subvolume)",
        "create": "create (a subvolume, yours)",
        "plain": "a plain folder: converted into a subvolume first",
        "bad": "NEEDS A LOOK: a file or symlink (nothing done)",
    }[b]
    left += b != "ok"
    row("~/Bulk", btxt)
    if b in ("ok", "plain"):
        t = tag_state(ctx)
        ttxt = {"ok": "ok", "create": "create", "bad": "NEEDS A LOOK: not a cache tag"}[t]
        row("~/Bulk/CACHEDIR.TAG", ttxt)
    for item in ITEMS:
        if not os.path.exists(jpath(item.id)):
            continue
        left += 1
        j = jread(item.id)
        if j and j.get("conflict") and kind(j["old"]) != "absent":
            print(f"  {item.id}: NEEDS A LOOK: {conflict_text(ctx, j)}")
        elif j:
            print(f"  {item.id}: an interrupted move is recorded ({ctx.short(j['old'] or j['src'])}): the next")
            print(
                "      apply (or boot) finishes or undoes it; "
                + ("inside ~/Bulk only, snapshots do not wait" if j.get("bulk_only") else "snapshots wait until then")
            )
        else:
            print(f"  {item.id}: NEEDS A LOOK: unreadable journal {jpath(item.id)}")
    for ident in steam_journal_ids():
        left += 1
        j = jread(ident)
        if j and j.get("conflict"):
            print(f"  {ident}: NEEDS A LOOK: {steam_conflict_text(ctx, j)}")
        elif j:
            print(f"  {ident}: an interrupted Steam game move is recorded (phase {j.get('phase')}): the next")
            print("      apply, 'bulk steam apply' or boot finishes or undoes it; snapshots wait until then")
        else:
            print(f"  {ident}: NEEDS A LOOK: unreadable journal {jpath(ident)}")
    try:
        kept, unknown = read_keep()
    except OSError as e:
        left += 1
        kept, unknown = set(), []
        print(f"  NEEDS A LOOK: {KEEP} cannot be read ({e}): apply touches no item until it is fixed")
    for ident in unknown:
        print(f"  {KEEP}: unknown item '{ident}' (ignored)")
    # Before ~/Bulk exists nothing waits for anything.
    done: set[str] = set() if b == "ok" else {i.id for i in ITEMS}
    for item in ITEMS:
        state, detail = classify(ctx, item, done, kept)
        if state == "kept":
            row(describe_paths(ctx, item), kept_text(item, detail))
            continue
        if state == "ok":
            done.add(item.id)
        else:
            left += 1
        text = item_text(ctx, item, state, detail, mode)
        if item.id in flagged and state != "ok":
            text += "; waits for the next boot (busy)"
        row(describe_paths(ctx, item), text)
        T = ctx.target(item)
        if item.nocow and mode == "status" and chain(ctx.bulk, item.target) == "ok":
            files, dirs_cow = cow_data(T)
            parents = nocow_parents(ctx, item)
            nc = ("yes" if not files and not dirs_cow else f"NO ({files} copy-on-write file(s))") + (
                f"; NO on {', '.join(ctx.short(p) for p in parents)}" if parents else ""
            )
            row("", f"+C (NOCOW): {nc}")
        if item.flatpak:
            try:
                f = as_user(ctx, u_flatpak_state, item.flatpak, T)
            except OSError, RuntimeError:
                f = "?"
            ftxt = {
                "absent": "app not installed (nothing to do)",
                "ok": "ok",
                "?": "? (flatpak not usable here)",
                "missing": "add" if mode == "plan" else "MISSING (run bulk apply)",
            }[f]
            row(f"  flatpak override: {item.flatpak} may use it", ftxt)
    if "steam" not in kept:
        old_steam(ctx)
    if "bulk" in flagged:
        print("  ~/Bulk itself was busy: converted at the next boot, before login")
    return left


def old_steam(ctx: Ctx) -> None:
    """The default Steam library stays in /home (snapshotted) until its games
    move into the Bulk library (`bulk steam apply`, or Steam's settings)."""
    p = os.path.join(ctx.home, STEAMAPPS, "common")
    if kind(p) != "dir" or chain(ctx.home, STEAMAPPS + "/common") != "ok":
        return
    if is_subvol(os.path.dirname(p)):
        row("~/" + STEAMAPPS, "a subvolume (earlier setup): left out of snapshots")
        return
    size = du_bytes(p)
    if size.total >= BIG:
        row("~/" + STEAMAPPS, f"still {du_text(size)} of games here, in /home snapshots: move them to")
        row("", "~/Bulk/steam with Steam closed: 'cst btrfs bulk steam plan', then 'bulk steam apply'")
        row("", "(btrfs/SNAPSHOTS.md, section 3)")
