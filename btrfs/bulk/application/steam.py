# SPDX-License-Identifier: MPL-2.0
"""Steam's games into the ~/Bulk/steam library: reading Steam's library
files and app manifests (as the user), the plan of one game, the move of
one game (reflink, or a byte-by-byte copy that is compared) with its
journal (SteamJournal: prepared, switching, switched, finishing), its
recovery, and the space limits taken from btrfs-snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
import os
import posixpath
import time

from btrfs.bulk.application.context import Bulk
from btrfs.bulk.domain import MARGIN, BulkJournalKind, Ctx, LstatKind, PathChain, Unexpected, present
from btrfs.bulk.domain.steam import (
    STEAM_ROOT,
    SteamApp,
    SteamAppState,
    SteamJournal,
    SteamMoveResult,
    SteamPart,
    SteamPartKind,
    SteamPartMode,
    SteamPhase,
    SteamRunning,
    SteamSurvey,
    Vdf,
    VdfPair,
    acf_app,
    dec,
    is_tool,
    lf_apps,
    lf_files,
    lf_index,
    lf_libraries,
    lf_place_app,
    lf_validate,
    lf_with_library,
    vdf_dump,
    vdf_load,
    vdf_parse,
    vdf_block,
    vdf_text,
)
from btrfs.bulk.ports import BulkStatePort


# ---- Steam: reading and checking (as root: lstat only; file contents are read as the user) ----
def ctx_uid(b: Bulk) -> int:
    return b.ctx.uid if b.ctx.uid is not None else b.tree.my_uid()


def steam_roots(ctx: Ctx) -> tuple[str, str]:
    return os.path.join(ctx.home, STEAM_ROOT), os.path.join(ctx.bulk, "steam")


def steam_read_pair(b: Bulk, settle: Callable[[Vdf], Vdf] | None = None) -> VdfPair | None:
    """Both libraryfolders.vdf, parsed and checked. They must be identical,
    except that SETTLE (a function tree -> tree: what an interrupted run of
    ours meant to write) makes them equal. Returns S, or None if Steam's
    folder is absent."""
    ctx = b.ctx
    src, _ = steam_roots(ctx)
    c = b.tree.chain(ctx.home, STEAM_ROOT)
    if c == PathChain.ABSENT:
        return None
    if c != PathChain.OK:
        raise Unexpected(f"{ctx.short(c.split(':', 1)[1])} is a symlink or a file, not a folder")
    files = lf_files(src)
    raw: list[bytes] = []
    mtime: list[int] = []
    trees: list[Vdf] = []
    for f in files:
        r = b.user.read(f)
        if r is None:
            raise Unexpected(f"{ctx.short(f)} is missing")
        try:
            t = vdf_load(r["data"])
            lf_validate(t, b.tree.realpath)
        except Unexpected as e:
            raise Unexpected(f"{ctx.short(f)}: {e}") from None
        raw.append(r["data"])
        mtime.append(r["mtime"])
        trees.append(t)
    tree, repair = trees[0], False
    if raw[0] != raw[1]:
        if settle and settle(trees[0]) == settle(trees[1]):
            tree, repair = settle(trees[0]), True
        else:
            bak = b.tree.newest_backup(files[0])
            raise Unexpected(
                f"the two copies of libraryfolders.vdf ({ctx.short(files[0])} and steamapps/) differ: with Steam "
                f"closed, copy the right one over the other"
                + (f" (newest backup: {ctx.short(bak)})" if bak is not None else "")
            )
    return {
        "files": files,
        "raw": raw,
        "mtime": mtime,
        "tree": tree,
        "repair": repair,
        "ts": time.strftime("%Y%m%dT%H%M%S"),
        "backed_up": False,
    }


def steam_marker(b: Bulk, library: str) -> str | None:
    """contentid of ~/Bulk/steam/libraryfolder.vdf, or None if absent."""
    ctx = b.ctx
    mk = os.path.join(library, "libraryfolder.vdf")
    k = b.tree.kind(mk)
    if k == LstatKind.ABSENT:
        return None
    if k != LstatKind.OTHER:
        raise Unexpected(f"{ctx.short(mk)} is a {'symlink' if k == LstatKind.LINK else 'folder'}")
    r = b.user.read(mk)
    if r is None:
        return None
    if r["uid"] != ctx_uid(b):
        raise Unexpected(f"{ctx.short(mk)} is not yours")
    try:
        t = vdf_parse(r["data"])
    except Unexpected as e:
        raise Unexpected(f"{ctx.short(mk)}: {e}") from None
    body = t.get("libraryfolder")
    if list(t) != ["libraryfolder"] or not isinstance(body, dict) or not dec(body.get("contentid")):
        raise Unexpected(f"{ctx.short(mk)} is not a Steam library marker")
    return vdf_text(body["contentid"], "contentid")


def steam_target(b: Bulk, pair: VdfPair, cid: str | None) -> str | None:
    """G4: ~/Bulk/steam as a library. Returns its entry number or None."""
    ctx = b.ctx
    _, T = steam_roots(ctx)
    c = b.tree.chain(T, "steamapps/common")
    if c.startswith(("symlink:", "file:")):
        raise Unexpected(f"{ctx.short(c.split(':', 1)[1])} is a symlink or a file, not a folder")
    sa = os.path.join(T, "steamapps")
    if b.tree.kind(sa) == LstatKind.DIR and b.tree.owner(sa) != ctx_uid(b):
        raise Unexpected(f"{ctx.short(sa)} is not yours")
    idx = lf_index(pair["tree"], T, b.tree.realpath)
    if idx is not None:
        if cid is None:
            raise Unexpected("~/Bulk/steam is listed as a library, but its libraryfolder.vdf is missing")
        ecid = vdf_block(lf_libraries(pair["tree"]).get(idx), f"library {idx}").get("contentid")
        if ecid != cid:
            raise Unexpected(f"~/Bulk/steam: contentid {ecid} in libraryfolders.vdf, {cid} in its libraryfolder.vdf")
    return idx


def nonempty(b: Bulk, path: str) -> bool:
    k = b.tree.kind(path)
    return k in (LstatKind.OTHER, LstatKind.LINK) or (k == LstatKind.DIR and not b.tree.empty_dir(path))


def new_app(appid: str) -> SteamApp:
    return {
        "id": appid,
        "name": "",
        "state": SteamAppState.LOOK,
        "why": "",
        "parts": [],
        "bytes": 0,
        "size": "",
        "copy_bytes": 0,
        "reflink_bytes": 0,
        "nocow_files": 0,
        "nocow_sample": "",
    }


def steam_app(b: Bulk, pair: VdfPair, appid: str, explicit: bool) -> SteamApp:
    """One app: state move | ok | skip | update | look, with why, parts, size."""
    A = new_app(appid)
    info = app_in_main_library(b, pair, appid, A)
    if info is None or app_held(b, A, info, explicit=explicit):
        return A
    parts = app_parts(b, A, info)
    if parts is None or not measure_parts(b, A, parts):
        return A
    if workshop_problem := steam_workshop_check(b, parts, appid):
        A["why"] = workshop_problem
        return A
    A["state"], A["parts"] = SteamAppState.MOVE, parts
    return A


def app_in_main_library(b: Bulk, pair: VdfPair, appid: str, app: SteamApp) -> dict[str, str] | None:
    """The app's manifest (acf_app) when it is installed in the main library
    and listed there (its name and size noted in app); else None, with app's
    state and why set."""
    ctx, t = b.ctx, b.tree
    src, T = steam_roots(ctx)
    tree = pair["tree"]
    lf = lf_libraries(tree)
    s_idx, d_idx = lf_index(tree, src, t.realpath), lf_index(tree, T, t.realpath)
    listed = [i for i in lf if appid in lf_apps(tree, i)]
    m_src = os.path.join(src, "steamapps", f"appmanifest_{appid}.acf")
    m_dst = os.path.join(T, "steamapps", f"appmanifest_{appid}.acf")
    ks, kd = t.kind(m_src), t.kind(m_dst)
    if ks != LstatKind.ABSENT and kd != LstatKind.ABSENT:
        app["why"] = "a manifest in both libraries"
        return None
    mpath = m_dst if kd != LstatKind.ABSENT else m_src
    if t.kind(mpath) == LstatKind.ABSENT:
        app["why"] = "not installed in the main library"
        return None
    try:
        r = b.user.read(mpath)
        if r is None:
            raise Unexpected("is missing")
        info = acf_app(vdf_parse(r["data"]), appid)
    except Unexpected as e:
        app["why"] = f"{ctx.short(mpath)}: {e}"
        return None
    app["name"] = info["name"]
    if kd != LstatKind.ABSENT:
        if listed != [d_idx]:
            app["why"] = (
                f"its manifest is in ~/Bulk/steam, but libraryfolders.vdf lists it under {listed or 'no library'}"
            )
            return None
        app["state"] = SteamAppState.OK
        return None
    if listed != [s_idx]:
        app["why"] = f"libraryfolders.vdf lists it under {listed or 'no library'}, its manifest is in library {s_idx}"
        return None
    s_idx = present(s_idx, "the main library's entry")
    app["size"] = vdf_text(lf_apps(tree, s_idx)[appid], f"app {appid}")
    return info


def app_held(b: Bulk, app: SteamApp, info: dict[str, str], *, explicit: bool) -> bool:
    """A tool not named with --app, a game mid-update, or one with a Proton
    prefix stays (True, app's state and why set)."""
    appid = app["id"]
    why = is_tool(appid, info["name"], info["installdir"])
    if why and not explicit:
        app["state"], app["why"] = SteamAppState.SKIP, why
        return True
    sa = os.path.join(steam_roots(b.ctx)[0], "steamapps")
    busy = [info["mid"]] if info["mid"] else []
    busy.extend(
        f"steamapps/{rel} is not empty"
        for rel in (f"downloading/{appid}", f"temp/{appid}", f"workshop/downloads/{appid}", f"workshop/temp/{appid}")
        if nonempty(b, os.path.join(sa, rel))
    )
    if busy:
        app["state"], app["why"] = SteamAppState.UPDATE, "; ".join(busy)
        return True
    if nonempty(b, os.path.join(sa, "compatdata", appid)):
        app["why"] = (
            f"steamapps/compatdata/{appid} has data (a Proton prefix: saves may be there): move it with "
            "Steam's own Properties > Installed Files > Move install folder"
        )
        return True
    return False


def app_parts(b: Bulk, app: SteamApp, info: dict[str, str]) -> list[SteamPart] | None:
    """The parts that move (game folder, workshop content and file, manifest),
    each with its names in both libraries; None (app's why set) when one is not
    what it must be."""
    ctx, t = b.ctx, b.tree
    src, T = steam_roots(ctx)
    appid = app["id"]
    sa = os.path.join(src, "steamapps")
    ts = time.strftime("%Y%m%dT%H%M%S")
    parts: list[SteamPart] = []
    for name, rel, what, optional in (
        ("game", f"common/{info['installdir']}", SteamPartKind.DIR, False),
        ("workshop", f"workshop/content/{appid}", SteamPartKind.DIR, True),
        ("workshop-acf", f"workshop/appworkshop_{appid}.acf", SteamPartKind.FILE, True),
        ("manifest", f"appmanifest_{appid}.acf", SteamPartKind.FILE, False),
    ):
        p_src = os.path.join(sa, rel)
        k = t.kind(p_src)
        if k == LstatKind.ABSENT and optional:
            continue
        want = LstatKind.DIR if what == SteamPartKind.DIR else LstatKind.OTHER
        if k != want or (what == SteamPartKind.FILE and not t.is_regular(p_src)):
            app["why"] = f"{ctx.short(p_src)} is {'missing' if k == LstatKind.ABSENT else 'a ' + k + ', not a ' + what}"
            return None
        dst = os.path.join(T, "steamapps", rel)
        parts.append(
            {
                "name": name,
                "src": p_src,
                "dst": dst,
                "new": dst + ".bulk-new",
                "old": f"{p_src}.bulk-old-{ts}",
                "kind": what,
            }
        )
    return parts


def measure_parts(b: Bulk, app: SteamApp, parts: Sequence[SteamPart]) -> bool:
    """Each part's checks, size and how it moves (reflink, or a real copy for
    NOCOW data), added up in app; False (app's why set) at the first that cannot move."""
    _, T = steam_roots(b.ctx)
    uid = ctx_uid(b)
    for p in parts:
        problem, size = part_size(b, p, uid, T)
        if problem:
            app["why"] = problem
            return False
        nocow_files, first_nocow, sample = b.tree.steam_nocow_scan(p["src"])
        # A NOCOW file cannot be cloned into copy-on-write ~/Bulk/steam: the
        # whole part is copied byte by byte instead (see steam_nocow_scan).
        # copy_bytes is the size of such parts, not the size of the +C files
        # (one empty +C file is enough): the messages give both.
        p["mode"], p["bytes"] = (SteamPartMode.COPY if nocow_files else SteamPartMode.REFLINK), size
        p["sample"] = "" if nocow_files else sample
        app["bytes"] += size
        app["copy_bytes" if nocow_files else "reflink_bytes"] += size
        app["nocow_files"] += nocow_files
        app["nocow_sample"] = app["nocow_sample"] or first_nocow
    return True


def part_size(b: Bulk, p: SteamPart, uid: int, library: str) -> tuple[str, int]:
    """('', its size) for a part that may move into library, else (why not, 0)."""
    ctx, t = b.ctx, b.tree
    for x in (p["dst"], p["new"]):
        if t.kind(x) != LstatKind.ABSENT:
            return f"{ctx.short(x)} exists already: remove it or move it back (nothing is merged)", 0
    c = t.chain(library, posixpath.relpath(os.path.dirname(p["dst"]), library))
    if c.startswith(("symlink:", "file:")):
        return f"{ctx.short(c.split(':', 1)[1])} is a symlink or a file, not a folder", 0
    if cow_problem := steam_target_cow(b, library, p["dst"]):
        return cow_problem, 0
    if p["kind"] == SteamPartKind.FILE:
        if t.owner(p["src"]) != uid:
            return f"{ctx.short(p['src'])} is not yours", 0
        return "", t.size(p["src"])
    if t.is_subvol(p["src"]) or t.has_mount(p["src"]):
        return f"{ctx.short(p['src'])} is a subvolume or a mount point", 0
    _, size, foreign, others = t.tree_stats(p["src"], uid)
    if foreign:
        return f"{ctx.short(p['src'])} contains another subvolume or a mount", 0
    if others or t.owner(p["src"]) != uid:
        return f"{ctx.short(p['src'])}: entries there are not yours (made by sudo or a container?)", 0
    return "", size


def steam_target_cow(b: Bulk, library: str, dst: str) -> str:
    """'' if every existing folder from ~/Bulk/steam down to DST's folder is
    copy-on-write; else why not. New files inherit +C from their folder, and
    a +C folder would make every moved file NOCOW (no checksums, no
    compression), which ~/Bulk/steam must not have."""
    path = library
    for part in [""] + [x for x in posixpath.relpath(os.path.dirname(dst), library).split("/") if x]:
        path = os.path.join(path, part) if part else path
        if b.tree.kind(path) != LstatKind.DIR:
            break
        if b.tree.is_nocow(path):
            return (
                f"{b.ctx.short(path)} has +C (NOCOW): what is created in it would be NOCOW too (no checksums, "
                f"no compression); ~/Bulk/steam must stay copy-on-write: 'chattr -C {path}' (on a folder it "
                "only changes what is created there afterwards), then run again"
            )
    return ""


def steam_workshop_check(b: Bulk, parts: Sequence[SteamPart], appid: str) -> str:
    ctx = b.ctx
    for p in parts:
        if p["name"] != "workshop-acf":
            continue
        try:
            r = b.user.read(p["src"])
            if r is None:
                raise Unexpected("is missing")
            t = vdf_parse(r["data"])
        except Unexpected as e:
            return f"{ctx.short(p['src'])}: {e}"
        body = t.get("AppWorkshop")
        if list(t) != ["AppWorkshop"] or not isinstance(body, dict) or body.get("appid") != appid:
            return f"{ctx.short(p['src'])} is not the workshop file of app {appid}"
    return ""


# ---- Steam: the changes ----
def write_vdf_pair(b: Bulk, pair: VdfPair, tree: Vdf) -> bool:
    """Write TREE into both copies if either differs from it. True if written.
    TREE must pass the same checks as a file we read: never write a pair
    that the next run would refuse."""
    try:
        lf_validate(tree, b.tree.realpath)
    except Unexpected as e:
        raise Unexpected(f"refused to write libraryfolders.vdf: {e}") from None
    data = vdf_dump(tree)
    if all(r == data for r in pair["raw"]):
        pair["tree"] = tree
        return False
    running = b.host.steam_running(b.ctx)
    if running:
        raise SteamRunning("; ".join(running[:3]))
    pair["mtime"] = b.user.write_vdf_pair(
        pair["files"], pair["raw"], pair["mtime"], data, pair["ts"], backup=not pair["backed_up"]
    )
    pair["raw"], pair["tree"], pair["backed_up"] = [data, data], tree, True
    return True


def steam_conflict_text(b: Bulk, j: SteamJournal) -> str:
    olds = ", ".join(b.ctx.short(p["old"]) for p in j["parts"] if b.tree.kind(p["old"]) != LstatKind.ABSENT)
    return (
        f"app {j['appid']} was written to on both sides during its move: compare {olds} (the old copy) "
        f"with the same names in ~/Bulk/steam (in use now), copy what you need there, delete the old copy, "
        "then run 'cst btrfs bulk steam apply' again"
    )


def steam_settle_vdf(b: Bulk, j: SteamJournal, pair: VdfPair | None = None) -> None:
    """Both libraryfolders.vdf: the library listed, the app under it only."""
    T, appid, size, cid = j["dst_lib"], j["appid"], j["size"], j["contentid"]
    resolve = b.tree.realpath

    def settle(t: Vdf) -> Vdf:
        return lf_place_app(lf_with_library(t, T, cid, resolve), appid, T, size, resolve)

    if pair is None:
        pair = steam_read_pair(b, settle)
        if pair is None:
            raise Unexpected("Steam's folder is gone")
    try:
        write_vdf_pair(b, pair, settle(pair["tree"]))
    except SteamRunning as e:
        raise Unexpected(f"Steam is running ({e}): close it; the next run finishes this move") from None


def steam_finish(b: Bulk, j: SteamJournal, pair: VdfPair | None = None) -> SteamMoveResult:
    """The switch passed its checks: the VDF files, then the old copies go."""
    ident = "steam-" + j["appid"]
    if j["phase"] != SteamPhase.FINISHING:
        j["phase"] = SteamPhase.SWITCHED
        b.state.jwrite(ident, j)
        steam_settle_vdf(b, j, pair)
        j["phase"] = SteamPhase.FINISHING  # a kill now: the next repair goes on deleting
        b.state.jwrite(ident, j)
    else:
        steam_settle_vdf(b, j, pair)
    b.user.steam_finish(j)
    b.state.jdrop(ident)
    return SteamMoveResult.OK


def steam_after_switch(b: Bulk, j: SteamJournal, pair: VdfPair | None = None) -> SteamMoveResult:
    """The last check after a complete switch, as in migrate(): a write into
    an old copy undoes the move; a write on both sides keeps both."""
    ident = "steam-" + j["appid"]
    if b.host.busy_paths([p["old"] for p in j["parts"]], b.ctx.home) or b.user.steam_late(j):
        if b.user.steam_new_written(j):
            j["conflict"] = True
            b.state.jwrite(ident, j)
            raise Unexpected(steam_conflict_text(b, j))
        j["undo"] = True
        b.state.jwrite(ident, j)
        b.user.steam_rollback(j)
        b.state.jdrop(ident)
        return SteamMoveResult.CHANGED
    return steam_finish(b, j, pair)


def steam_all_switched(b: Bulk, j: SteamJournal) -> bool:
    kind = b.tree.kind
    return all(
        kind(p["dst"]) != LstatKind.ABSENT
        and kind(p["new"]) == LstatKind.ABSENT
        and kind(p["old"]) != LstatKind.ABSENT
        and kind(p["src"]) == LstatKind.ABSENT
        for p in j["parts"]
    )


def steam_recover(b: Bulk, j: SteamJournal) -> str:
    """Finish or undo what an interrupted Steam move left."""
    ident = "steam-" + j["appid"]
    phase = j.get("phase")
    if j.get("conflict", False):
        if any(b.tree.kind(p["old"]) != LstatKind.ABSENT for p in j["parts"]):
            raise Unexpected(steam_conflict_text(b, j))
        j["phase"] = SteamPhase.FINISHING
        steam_finish(b, j)
        return "the old copy is gone: the move is complete"
    if phase == SteamPhase.FINISHING:
        steam_finish(b, j)
        return "deleted the old copy left by an interrupted run"
    if phase == SteamPhase.SWITCHED:
        steam_finish(b, j)
        return "finished an interrupted move"
    if (
        j.get("undo", False)
        or phase == SteamPhase.PREPARED
        or (phase == SteamPhase.SWITCHING and not steam_all_switched(b, j))
    ):
        if phase != SteamPhase.PREPARED:
            b.user.steam_rollback(j)
        else:
            b.user.steam_drop_news(j)
        b.state.jdrop(ident)
        return "undid an interrupted move (the game is back in the main library)"
    if phase == SteamPhase.SWITCHING:
        if steam_after_switch(b, j) == SteamMoveResult.CHANGED:
            return "undid an interrupted move: an old copy was written to during the switch"
        return "finished an interrupted move"
    raise Unexpected(f"unknown phase {phase!r}")


# How a damaged journal is reported (recovery and report, of items and Steam games alike).
DAMAGED = "unreadable, or not as btrfs-bulk writes it: left in place, nothing it names was touched"


def damaged(state: BulkStatePort, ident: str) -> str:
    """What NEEDS A LOOK says of a journal that is not as btrfs-bulk writes it."""
    return f"damaged journal {state.jpath(ident)} ({DAMAGED})"


def steam_recover_all(b: Bulk) -> list[str]:
    state, log = b.state, b.out.log
    stuck: list[str] = []
    for ident in state.steam_journal_ids():
        j = state.jread_steam(ident)
        if not j:
            stuck.append(ident)
            log(f"NEEDS A LOOK: {ident}: {state.newer_journal(ident) or damaged(state, ident)}")
            continue
        try:
            log(f"  {ident}: {steam_recover(b, j)}")
        except (OSError, RuntimeError, Unexpected) as e:
            stuck.append(ident)
            log(
                f"NEEDS A LOOK: {ident}: an interrupted Steam game move: {e} (journal kept in {state.jpath(ident)}; "
                "snapshots wait until it is gone)"
            )
    return stuck


def steam_copy_floor(b: Bulk) -> int:
    """Free space a real copy must leave behind: the snapshot emergency
    limit plus MARGIN. The old copy is deleted only after the new one is
    checked, and while /home snapshots still hold it, deleting it frees
    nothing: without this floor a large copy could push free space down to
    the emergency purge of btrfs-snapshot, which deletes most of the
    snapshot history."""
    return b.host.snapshot_guard()["emergency"] + MARGIN


def steam_copy_need(b: Bulk, a: SteamApp) -> int:
    """Free space a game's real copies need in ~/Bulk (0: reflinks only)."""
    return a["copy_bytes"] + steam_copy_floor(b) if a["copy_bytes"] else 0


def steam_race_guard(b: Bulk, j: SteamJournal) -> SteamMoveResult | None:
    """Why the switch must not happen now (running | busy | changed), or None."""
    if b.host.steam_running(b.ctx):
        return SteamMoveResult.RUNNING
    if b.host.busy_paths([p["src"] for p in j["parts"]], b.ctx.home):
        return SteamMoveResult.BUSY
    if b.user.steam_changed(j):
        return SteamMoveResult.CHANGED
    return None


def steam_move(b: Bulk, sv: SteamSurvey, a: SteamApp) -> SteamMoveResult:
    """Move one app. Returns ok | running | busy | changed | space."""
    ctx, user, state, host = b.ctx, b.user, b.state, b.host
    pair, (_, T) = sv["S"], steam_roots(ctx)
    ident, parts = "steam-" + a["id"], a["parts"]
    if host.steam_running(ctx):
        return SteamMoveResult.RUNNING
    if host.busy_paths([p["src"] for p in parts], ctx.home):
        return SteamMoveResult.BUSY
    # A real copy shares no blocks: its full size must be free (asked again
    # per game: an earlier game's old copy may still be held by snapshots).
    if steam_copy_need(b, a) and b.tree.free_bytes(T) < steam_copy_need(b, a):
        return SteamMoveResult.SPACE
    j: SteamJournal = {
        "kind": BulkJournalKind.STEAM,
        "appid": a["id"],
        "src_lib": steam_roots(ctx)[0],
        "dst_lib": T,
        "size": a["size"],
        "contentid": present(sv["cid"], "the library's contentid"),  # ensure_library has set it
        "parts": parts,
        "stamp": 0,
        "phase": SteamPhase.PREPARED,
        "conflict": False,
        "undo": False,
    }
    state.jwrite(ident, j)  # a kill during the copy: the repair deletes the copies
    try:
        for d in sorted({posixpath.relpath(os.path.dirname(p["dst"]), T) for p in parts}):
            user.mkdirs(T, d, nocow=False)
        j["stamp"] = user.steam_prepare(parts)
        j["stamp2"] = user.clock(parts[-1]["new"])
        state.jwrite(ident, j)
        why = steam_race_guard(b, j)  # the last moment before the switch
    except OSError, RuntimeError, Unexpected:
        user.steam_drop_news(j)
        state.jdrop(ident)
        raise
    if why is not None:
        user.steam_drop_news(j)
        state.jdrop(ident)
        return why
    j["phase"] = SteamPhase.SWITCHING
    state.jwrite(ident, j)
    try:
        user.steam_switch(j)
    except OSError, RuntimeError, Unexpected:
        user.steam_rollback(j)
        state.jdrop(ident)
        raise
    return steam_after_switch(b, j, pair)


def ensure_library(b: Bulk, sv: SteamSurvey) -> None:
    """~/Bulk/steam as a Steam library: steamapps/common, the marker
    libraryfolder.vdf (a new random contentid), the entry in both
    libraryfolders.vdf. Idempotent."""
    _, T = steam_roots(b.ctx)
    resolve = b.tree.realpath
    if b.host.steam_running(b.ctx):
        raise SteamRunning("Steam started")
    b.user.mkdirs(T, "steamapps/common", nocow=False)
    pair = present(sv["S"], "the libraryfolders.vdf pair")
    if sv["cid"] is None:
        sv["cid"] = b.host.new_contentid()
        b.user.write_marker(os.path.join(T, "libraryfolder.vdf"), sv["cid"])
        b.out.log(f"{'done:':<10} ~/Bulk/steam/libraryfolder.vdf (contentid {sv['cid']})")
    if write_vdf_pair(b, pair, lf_with_library(pair["tree"], T, sv["cid"], resolve)):
        b.out.log(
            f"{'done:':<10} ~/Bulk/steam is library {lf_index(pair['tree'], T, resolve)} in both libraryfolders.vdf "
            f"(backups: *.cst-bak-{pair['ts']})"
        )
    sv["idx"] = lf_index(pair["tree"], T, resolve)
