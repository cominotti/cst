# SPDX-License-Identifier: MPL-2.0
"""btrfs-bulk's commands over the use cases: steam plan/apply (the survey
of the libraries, its text, the moves), the boot step and keep/unkeep.
"""

from __future__ import annotations

from collections.abc import Sequence
import os

from btrfs.bulk.application.context import Bulk
from btrfs.bulk.application.items import apply, classify, exit_status, recover, row
from btrfs.bulk.application.steam import (
    ensure_library,
    steam_app,
    steam_copy_floor,
    steam_copy_need,
    steam_marker,
    steam_move,
    steam_read_pair,
    steam_roots,
    steam_target,
)
from btrfs.bulk.domain import (
    BY_ID,
    MAKE_DEFAULT,
    MARGIN,
    STEAM_APP,
    STEAM_MARGIN,
    STEAM_ROOT,
    Ctx,
    SteamRunning,
    SteamSurvey,
    Unexpected,
    Vdf,
    du_text,
    gib,
    hms,
    lf_libraries,
    lf_with_library,
    present,
    steam_move_lines,
    steam_nocow_files,
)
from btrfs.bulk.ports import BulkStatePort, HomeTreePort, OutputPort


def steam_survey(b: Bulk, ids: Sequence[str], allow_copy: bool = False) -> SteamSurvey:
    """Everything plan prints and apply needs (read-only). allow_copy: --copy
    was given (games with NOCOW data may be copied byte by byte)."""
    ctx = b.ctx
    sv: SteamSurvey = {
        "running": b.host.steam_running(ctx),
        "fatal": "",
        "nothing": False,
        "S": None,
        "idx": None,
        "cid": None,
        "apps": [],
        "space": "",
        "copy": allow_copy,
        "copy_bytes": 0,
        "free": 0,
    }
    src, T = steam_roots(ctx)
    try:
        kept, _ = b.state.read_keep()
    except OSError as e:
        sv["fatal"] = f"{b.paths.keep} cannot be read ({e})"
        return sv
    if "steam" in kept:
        sv["fatal"] = "steam is kept out of ~/Bulk by choice: 'cst btrfs bulk unkeep steam' first"
        return sv
    try:
        cid = steam_marker(b, T) if b.tree.kind(T) == "dir" else None

        def settle(t: Vdf) -> Vdf:
            return lf_with_library(t, T, present(cid, "the marker's contentid"), b.tree.realpath)

        S = steam_read_pair(b, settle if cid else None)
    except Unexpected as e:
        sv["fatal"] = str(e)
        return sv
    if S is None:
        sv["nothing"] = True
        sv["fatal"] = f"no Steam (Flatpak) library at ~/{STEAM_ROOT}: nothing to move"
        return sv
    state, detail = classify(b, BY_ID["steam"], set(), kept)
    if state != "ok":
        sv["fatal"] = f"~/Bulk/steam is not in place ({state} {detail}): run 'cst btrfs bulk apply' first"
        return sv
    try:
        f = b.user.flatpak_state(STEAM_APP, T)
    except (OSError, RuntimeError, Unexpected) as e:
        f = f"cannot be checked ({e})"
    if f != "ok":
        sv["fatal"] = (
            f"the Steam Flatpak may not use {T} yet ({'not installed' if f == 'absent' else f}): "
            "run 'cst btrfs bulk apply' first"
        )
        return sv
    try:
        sv["idx"] = steam_target(b, S, cid)
    except Unexpected as e:
        sv["fatal"] = str(e)
        return sv
    sv["S"], sv["cid"] = S, cid
    if ids:
        sel = [(i, True) for i in sorted(set(ids), key=int)]
    else:
        sel = [(i, False) for i in sorted(b.tree.steam_manifests(src) | b.tree.steam_manifests(T), key=int)]
    sv["apps"] = [steam_app(b, S, i, explicit) for i, explicit in sel]
    if any(a["state"] == "move" for a in sv["apps"]):
        sv["free"] = b.tree.free_bytes(T)
        sv["copy_bytes"] = sum(a["copy_bytes"] for a in sv["apps"] if a["state"] == "move")
        if sv["free"] < STEAM_MARGIN:
            sv["space"] = f"less than {gib(STEAM_MARGIN)} free in ~/Bulk"
    return sv


def steam_print(b: Bulk, sv: SteamSurvey) -> None:
    ctx = b.ctx
    src, _ = steam_roots(ctx)
    b.out.say(
        "Steam (Flatpak): games move from the main library into ~/Bulk/steam (reflink where possible, Steam closed)."
    )
    r = sv["running"]
    row(b, "Steam running", "no" if not r else f"YES ({'; '.join(r[:3])}): close it (Steam > Exit) first")
    if sv["fatal"]:
        row(
            b,
            "Steam libraries",
            ("" if sv["nothing"] else "NEEDS A LOOK: ") + sv["fatal"] + ("" if sv["nothing"] else " (nothing touched)"),
        )
        return
    S = present(sv["S"], "the libraryfolders.vdf pair")
    if S["repair"]:
        row(b, "libraryfolders.vdf", "the two copies differ only by the ~/Bulk/steam entry (an interrupted run):")
        row(b, "", "apply writes both again")
    if sv["idx"] is not None:
        row(b, "library ~/Bulk/steam", f"ok (library {sv['idx']})")
    elif any(a["state"] == "move" for a in sv["apps"]):
        n = max(int(i) for i in lf_libraries(S["tree"])) + 1
        row(
            b,
            "library ~/Bulk/steam",
            f"add as library {n} (contentid {sv['cid'] or 'new'}; both libraryfolders.vdf, backed up first)",
        )
    else:
        row(b, "library ~/Bulk/steam", "not a library yet (added with the first game that moves)")
    texts = {
        "ok": lambda a: "ok (in ~/Bulk/steam)",
        "skip": lambda a: f"skipped: a tool, not a game; move it with --app {a['id']}",
        "update": lambda a: f"NEEDS A LOOK: mid-update ({a['why']}): let Steam finish, close it, run again",
        "look": lambda a: f"NEEDS A LOOK: {a['why']} (nothing touched)",
    }
    for a in sv["apps"]:
        name = f"{a['id']} {a['name']}".strip()
        if a["state"] != "move":
            row(b, name, texts[a["state"]](a))
            continue
        lines = steam_move_lines(ctx, a, sv["copy"])
        row(b, name, lines[0])
        for line in lines[1:]:
            row(b, "", line)
    if not sv["apps"]:
        row(b, "games", "none installed")
    elif not any(a["state"] in ("move", "ok") for a in sv["apps"]):
        row(b, "games", "nothing to move by default; name one with --app ID")
    for a in sv["apps"]:
        sc = os.path.join(src, "steamapps", "shadercache", a["id"])
        if a["state"] in ("move", "ok") and b.tree.kind(sc) == "dir" and not b.tree.empty_dir(sc):
            row(
                b,
                f"shader cache of {a['id']}",
                f"{du_text(b.tree.du_bytes(sc))} stays (steamapps/shadercache/{a['id']}",
            )
            row(b, "", "of the main library): Steam builds a new one in ~/Bulk/steam; then delete the old one")
    if sv["space"]:
        row(b, "free space", f"NEEDS A LOOK: {sv['space']}")
    if sv["copy_bytes"]:
        g, floor = b.host.snapshot_guard(), steam_copy_floor(b)
        need, left = sv["copy_bytes"] + floor, sv["free"] - sv["copy_bytes"]
        row(
            b,
            "real copies",
            f"{gib(sv['copy_bytes'])} in all: {gib(need)} must be free in ~/Bulk, {gib(sv['free'])} "
            "free now" + ("" if sv["free"] >= need else " (NEEDS A LOOK: not enough for all)"),
        )
        row(
            b,
            "",
            f"({gib(floor)} stays free: the snapshot emergency purge at {gib(g['emergency'])} plus {gib(MARGIN)};",
        )
        row(b, "", "the old copies free nothing while /home snapshots still hold them)")
        if sv["free"] >= need and left <= g["pause"]:
            row(
                b,
                "",
                f"NOTE: about {gib(max(left, 0))} free after all copies: /home snapshots pause at or below "
                f"{gib(g['pause'])}",
            )
            row(
                b,
                "",
                f"and resume above {gib(g['resume'])} (once old snapshots expire); fewer games per run may avoid it",
            )
        row(b, "", "each copy is read back and compared byte for byte before the old one is deleted;")
        row(b, "", "at idle priority: expect minutes per 10 GiB, more while the PC is busy")


def steam_apply(b: Bulk, ids: Sequence[str], allow_copy: bool = False) -> int:
    """Returns the exit status: 0 done, 1 needs a look, 5 Steam is running.
    allow_copy (--copy): games with NOCOW data may be copied byte by byte;
    without it they are refused (1), the others still move."""
    ctx, log = b.ctx, b.out.log
    lock = b.state.run_lock()
    try:
        stuck = [s for s in recover(b) if s.startswith("steam-")]
        if stuck:
            log(f"interrupted Steam move(s) need a look first ({', '.join(stuck)}): nothing else done")
            return 1
        sv = steam_survey(b, ids, allow_copy)
        steam_print(b, sv)
        if sv["running"]:
            log("Steam is running: nothing done. Close it (Steam > Exit) and run again.")
            return 5
        if sv["fatal"]:
            return 0 if sv["nothing"] else 1
        movers = [a for a in sv["apps"] if a["state"] == "move"]
        rc = 1 if any(a["state"] in ("look", "update") for a in sv["apps"]) else 0
        for a in [a for a in movers if a["copy_bytes"] and not allow_copy]:
            log(
                f"NEEDS A LOOK: {a['id']} {a['name']}: {gib(a['copy_bytes'])} must be copied byte by byte because "
                f"{steam_nocow_files(ctx, a)}, which btrfs cannot reflink into copy-on-write ~/Bulk/steam: run "
                "again with --copy to allow it (if the +C files are few and small, recreating them without +C "
                "lets the game move by reflink instead)"
            )
            rc = 1
        movers = [a for a in movers if allow_copy or not a["copy_bytes"]]
        if not movers and present(sv["S"], "the libraryfolders.vdf pair")["repair"]:
            try:
                ensure_library(b, sv)  # the interrupted library entry, in both copies
            except SteamRunning as e:
                log(f"Steam is running ({e}): nothing done")
                return 5
            except (OSError, RuntimeError, Unexpected) as e:
                log(f"NEEDS A LOOK: {e}")
                return 1
        if not movers:
            if rc == 0 and sv["idx"] is not None:
                log("Everything asked for is in ~/Bulk/steam already.")
            return rc
        if sv["space"]:
            log("not enough free space in ~/Bulk: nothing done")
            return 1
        sample = next(
            (p.get("sample", "") for a in movers for p in a["parts"] if p.get("mode") == "reflink" and p.get("sample")),
            "",
        )
        try:
            if sample:  # games that are copied byte by byte need no reflink
                b.user.reflink_probe(sample, steam_roots(ctx)[1])
            ensure_library(b, sv)
        except SteamRunning as e:
            log(f"Steam is running ({e}): nothing done")
            return 5
        except (OSError, RuntimeError, Unexpected) as e:
            log(f"NEEDS A LOOK: {e} (no game moved)")
            return 1
        moved = 0
        for a in sorted(movers, key=lambda x: int(x["id"])):
            name = f"{a['id']} {a['name']}"
            how = [
                f"reflink {gib(a['reflink_bytes'])}" if a["reflink_bytes"] else "",
                f"real copy {gib(a['copy_bytes'])}" if a["copy_bytes"] else "",
            ]
            log(f"{'start:':<10} {name}: {', '.join(x for x in how if x) or 'reflink'}")
            t0 = b.host.now()
            try:
                res = steam_move(b, sv, a)
            except (OSError, RuntimeError, Unexpected) as e:
                log(f"NEEDS A LOOK: {name}: {e}; no further game moved this run")
                return 1
            if res == "ok":
                moved += 1
                log(f"{'done:':<10} {name}: in ~/Bulk/steam now ({hms(b.host.now() - t0)})")
                continue
            if res == "space":
                log(
                    f"NEEDS A LOOK: {name}: not enough free space in ~/Bulk for a real copy of "
                    f"{gib(a['copy_bytes'])} (needs {gib(steam_copy_need(b, a))}: {gib(steam_copy_floor(b))} must stay "
                    "free so the /home snapshots are not purged): skipped, nothing changed for it"
                )
                rc = 1
                continue
            log(
                f"{'stopped:':<10} {name}: "
                + {
                    "running": "Steam started: undone, nothing moved; close it and run again",
                    "busy": "in use (open files): undone; close what uses it and run again",
                    "changed": "written to during the move: undone; run again",
                }[res]
            )
            return 5 if res in ("running", "busy") else 1
        if moved:
            b.out.say(MAKE_DEFAULT)
            b.out.say("Then start Steam: Settings > Storage lists both libraries; the moved games are in ~/Bulk/steam.")
        return rc

    finally:
        b.state.release(lock)


def boot(b: Bulk) -> int:
    """At boot, before any login: nothing of the session holds files open.
    Never blocks the login more than twice for the same leftovers."""
    state, log = b.state, b.out.log
    if not state.flag_exists():
        if not state.journals():
            return 0
        # A move interrupted (power loss) before the flag got written back:
        # finish or undo it now, before any app recreates the old path.
        log("btrfs-bulk: recovering an interrupted move into ~/Bulk...")
        deferred, problems = apply(b)
        return exit_status(b, deferred, problems)
    attempts = b.boots.count()
    if attempts >= 2:
        left = " ".join(state.read_flag())
        state.write_flag([])
        b.boots.remove()
        b.host.alert(
            "~/Bulk: moving at boot gave up",
            f"Left: {left} - see 'journalctl -u btrfs-bulk-boot'; close what uses them and run 'cst btrfs bulk apply'.",
        )
        return 1
    # Durable before the moves start: a hang or hard reset must still count this boot.
    b.boots.save(attempts + 1)
    log("btrfs-bulk: moving the items the desktop session kept busy into ~/Bulk...")
    deferred, problems = apply(b)
    if not deferred:
        b.boots.remove()
    return exit_status(b, deferred, problems)


def keep_ids(state: BulkStatePort, cmd: str, ids: Sequence[str]) -> OSError | None:
    """keep: the ids go into the keep file, and leave the boot flag (with the
    items inside them); unkeep: they leave the keep file. The error when the
    keep file (or the flag) could not be written, else None."""
    lock = state.run_lock()
    try:
        if cmd == "keep":
            state.edit_keep(ids, set())
            # Busy items waiting for the boot (and those inside them): not any more.
            state.write_flag(
                [i for i in state.read_flag() if i not in ids and (BY_ID[i].after if i in BY_ID else None) not in ids]
            )
        else:
            state.edit_keep([], set(ids))
    except OSError as e:
        return e
    finally:
        state.release(lock)
    return None


def keep_report(tree: HomeTreePort, out: OutputPort, ctx: Ctx | None, cmd: str, ids: Sequence[str]) -> None:
    """What keep/unkeep did, item by item (ctx None: the home is unknown)."""
    for ident in ids:
        item = BY_ID[ident]
        if cmd == "unkeep":
            out.say(f"{ident}: no longer kept; the next 'cst btrfs bulk apply' moves it into ~/Bulk")
            continue
        out.say(
            f"{ident}: {'kept in /home' if item.link else 'left out of ~/Bulk'} by choice; "
            "bulk apply leaves it alone" + ("; its data is in /home snapshots" if item.link else "")
        )
        if ctx and item.link:
            L = ctx.link(item)
            if L and tree.kind(L) == "link" and tree.link_target(L) == ctx.target(item):
                out.say(f"  ~/{item.link} is still a link into ~/Bulk: to bring the data back, see")
                out.say("  btrfs/SNAPSHOTS.md, section 12 (Undo one Bulk link)")
