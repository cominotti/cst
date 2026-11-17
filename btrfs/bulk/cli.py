"""btrfs-bulk's commands: steam plan/apply, keep/unkeep, boot, the user
whose home is handled, the desktop alert, and main().
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import os
import pwd
import subprocess
import sys
import time
from btrfs.bulk.adapters import (
    CONF,
    Ctx,
    FLAG,
    KEEP,
    Program,
    ROOT,
    STATE,
    as_user,
    du_bytes,
    edit_keep,
    empty_dir,
    free_bytes,
    journals,
    kind,
    link_target,
    log,
    read_flag,
    read_keep,
    run_lock,
    u_flatpak_state,
    write_flag,
)
from btrfs.bulk.domain import (
    BY_ID,
    DECIMAL,
    MAKE_DEFAULT,
    MARGIN,
    NOTIFY,
    STEAM_APP,
    STEAM_MARGIN,
    STEAM_ROOT,
    STEAM_USAGE,
    SteamRunning,
    Unexpected,
    gib,
    hms,
)
from btrfs.bulk.items import apply, classify, exit_status, recover, report, row
from btrfs.bulk.steam import (
    ensure_library,
    lf_with_library,
    snapshot_guard,
    steam_app,
    steam_copy_floor,
    steam_copy_need,
    steam_manifests,
    steam_marker,
    steam_move,
    steam_move_lines,
    steam_nocow_files,
    steam_read_pair,
    steam_roots,
    steam_running,
    steam_target,
    u_reflink_probe,
)
from cst_core.adapters.files import write_atomic


def steam_survey(ctx: Ctx, ids: Sequence[str], allow_copy: bool = False) -> dict[str, Any]:
    """Everything plan prints and apply needs (read-only). allow_copy: --copy
    was given (games with NOCOW data may be copied byte by byte)."""
    sv: dict[str, Any] = {
        "running": steam_running(ctx),
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
        kept, _ = read_keep()
    except OSError as e:
        sv["fatal"] = f"{KEEP} cannot be read ({e})"
        return sv
    if "steam" in kept:
        sv["fatal"] = "steam is kept out of ~/Bulk by choice: 'cst btrfs bulk unkeep steam' first"
        return sv
    try:
        cid = steam_marker(ctx, T) if kind(T) == "dir" else None
        S = steam_read_pair(ctx, (lambda t: lf_with_library(t, T, cid)) if cid else None)
    except Unexpected as e:
        sv["fatal"] = str(e)
        return sv
    if S is None:
        sv["nothing"] = True
        sv["fatal"] = f"no Steam (Flatpak) library at ~/{STEAM_ROOT}: nothing to move"
        return sv
    state, detail = classify(ctx, BY_ID["steam"], set(), kept)
    if state != "ok":
        sv["fatal"] = f"~/Bulk/steam is not in place ({state} {detail}): run 'cst btrfs bulk apply' first"
        return sv
    try:
        f = as_user(ctx, u_flatpak_state, STEAM_APP, T)
    except (OSError, RuntimeError, Unexpected) as e:
        f = f"cannot be checked ({e})"
    if f != "ok":
        sv["fatal"] = (
            f"the Steam Flatpak may not use {T} yet ({'not installed' if f == 'absent' else f}): "
            "run 'cst btrfs bulk apply' first"
        )
        return sv
    try:
        sv["idx"] = steam_target(ctx, S, cid)
    except Unexpected as e:
        sv["fatal"] = str(e)
        return sv
    sv["S"], sv["cid"] = S, cid
    if ids:
        sel = [(i, True) for i in sorted(set(ids), key=int)]
    else:
        sel = [(i, False) for i in sorted(steam_manifests(src) | steam_manifests(T), key=int)]
    sv["apps"] = [steam_app(ctx, S, i, explicit) for i, explicit in sel]
    if any(a["state"] == "move" for a in sv["apps"]):
        sv["free"] = free_bytes(T)
        sv["copy_bytes"] = sum(a["copy_bytes"] for a in sv["apps"] if a["state"] == "move")
        if sv["free"] < STEAM_MARGIN:
            sv["space"] = f"less than {gib(STEAM_MARGIN)} free in ~/Bulk"
    return sv


def steam_print(ctx: Ctx, sv: Mapping[str, Any]) -> None:
    src, T = steam_roots(ctx)
    print("Steam (Flatpak): games move from the main library into ~/Bulk/steam (reflink where possible, Steam closed).")
    r = sv["running"]
    row("Steam running", "no" if not r else f"YES ({'; '.join(r[:3])}): close it (Steam > Exit) first")
    if sv["fatal"]:
        row(
            "Steam libraries",
            ("" if sv["nothing"] else "NEEDS A LOOK: ") + sv["fatal"] + ("" if sv["nothing"] else " (nothing touched)"),
        )
        return
    S = sv["S"]
    if S["repair"]:
        row("libraryfolders.vdf", "the two copies differ only by the ~/Bulk/steam entry (an interrupted run):")
        row("", "apply writes both again")
    if sv["idx"] is not None:
        row("library ~/Bulk/steam", f"ok (library {sv['idx']})")
    elif any(a["state"] == "move" for a in sv["apps"]):
        n = max(int(i) for i in S["tree"]["libraryfolders"]) + 1
        row(
            "library ~/Bulk/steam",
            f"add as library {n} (contentid {sv['cid'] or 'new'}; both libraryfolders.vdf, backed up first)",
        )
    else:
        row("library ~/Bulk/steam", "not a library yet (added with the first game that moves)")
    texts = {
        "ok": lambda a: "ok (in ~/Bulk/steam)",
        "skip": lambda a: f"skipped: a tool, not a game; move it with --app {a['id']}",
        "update": lambda a: f"NEEDS A LOOK: mid-update ({a['why']}): let Steam finish, close it, run again",
        "look": lambda a: f"NEEDS A LOOK: {a['why']} (nothing touched)",
    }
    for a in sv["apps"]:
        name = f"{a['id']} {a['name']}".strip()
        if a["state"] != "move":
            row(name, texts[a["state"]](a))
            continue
        lines = steam_move_lines(ctx, a, sv["copy"])
        row(name, lines[0])
        for line in lines[1:]:
            row("", line)
    if not sv["apps"]:
        row("games", "none installed")
    elif not any(a["state"] in ("move", "ok") for a in sv["apps"]):
        row("games", "nothing to move by default; name one with --app ID")
    for a in sv["apps"]:
        sc = os.path.join(src, "steamapps", "shadercache", a["id"])
        if a["state"] in ("move", "ok") and kind(sc) == "dir" and not empty_dir(sc):
            row(f"shader cache of {a['id']}", f"{gib(du_bytes(sc))} stays (steamapps/shadercache/{a['id']} of the main")
            row("", "library): Steam builds a new one in ~/Bulk/steam; then delete the old one")
    if sv["space"]:
        row("free space", f"NEEDS A LOOK: {sv['space']}")
    if sv["copy_bytes"]:
        g, floor = snapshot_guard(), steam_copy_floor()
        need, left = sv["copy_bytes"] + floor, sv["free"] - sv["copy_bytes"]
        row(
            "real copies",
            f"{gib(sv['copy_bytes'])} in all: {gib(need)} must be free in ~/Bulk, {gib(sv['free'])} "
            "free now" + ("" if sv["free"] >= need else " (NEEDS A LOOK: not enough for all)"),
        )
        row("", f"({gib(floor)} stays free: the snapshot emergency purge at {gib(g['emergency'])} plus {gib(MARGIN)};")
        row("", "the old copies free nothing while /home snapshots still hold them)")
        if sv["free"] >= need and left <= g["pause"]:
            row(
                "",
                f"NOTE: about {gib(max(left, 0))} free after all copies: /home snapshots pause at or below "
                f"{gib(g['pause'])}",
            )
            row(
                "", f"and resume above {gib(g['resume'])} (once old snapshots expire); fewer games per run may avoid it"
            )
        row("", "each copy is read back and compared byte for byte before the old one is deleted;")
        row("", "at idle priority: expect minutes per 10 GiB, more while the PC is busy")


def steam_apply(ctx: Ctx, ids: Sequence[str], allow_copy: bool = False) -> int:
    """Returns the exit status: 0 done, 1 needs a look, 5 Steam is running.
    allow_copy (--copy): games with NOCOW data may be copied byte by byte;
    without it they are refused (1), the others still move."""
    lock = run_lock()
    try:
        stuck = [s for s in recover(ctx) if s.startswith("steam-")]
        if stuck:
            log(f"interrupted Steam move(s) need a look first ({', '.join(stuck)}): nothing else done")
            return 1
        sv = steam_survey(ctx, ids, allow_copy)
        steam_print(ctx, sv)
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
        if not movers and sv["S"]["repair"]:
            try:
                ensure_library(ctx, sv)  # the interrupted library entry, in both copies
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
        sample = next((p["sample"] for a in movers for p in a["parts"] if p["mode"] == "reflink" and p["sample"]), "")
        try:
            if sample:  # games that are copied byte by byte need no reflink
                as_user(ctx, u_reflink_probe, sample, steam_roots(ctx)[1])
            ensure_library(ctx, sv)
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
            t0 = time.monotonic()
            try:
                res = steam_move(ctx, sv, a)
            except (OSError, RuntimeError, Unexpected) as e:
                log(f"NEEDS A LOOK: {name}: {e}; no further game moved this run")
                return 1
            if res == "ok":
                moved += 1
                log(f"{'done:':<10} {name}: in ~/Bulk/steam now ({hms(time.monotonic() - t0)})")
                continue
            if res == "space":
                log(
                    f"NEEDS A LOOK: {name}: not enough free space in ~/Bulk for a real copy of "
                    f"{gib(a['copy_bytes'])} (needs {gib(steam_copy_need(a))}: {gib(steam_copy_floor())} must stay "
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
            print(MAKE_DEFAULT)
            print("Then start Steam: Settings > Storage lists both libraries; the moved games are in ~/Bulk/steam.")
        return rc
    finally:
        os.close(lock)


def steam_cmd(args: Sequence[str]) -> int:
    """btrfs-bulk steam plan|apply [--app ID]... [--copy] [--user NAME]"""
    sub = args[0] if args else ""
    ids: list[str] = []
    user_args: list[str] = []
    i = 1
    allow_copy = False
    while i < len(args):
        a = args[i]
        if a == "--copy":
            allow_copy = True
            i += 1
            continue
        if a == "--app" and i + 1 < len(args) and DECIMAL.fullmatch(args[i + 1]) and len(args[i + 1]) <= 10:
            ids.append(str(int(args[i + 1])))
        elif a == "--user" and i + 1 < len(args):
            user_args = [a, args[i + 1]]
        else:
            print(
                f"btrfs-bulk steam: bad argument {a!r}"
                + (f" {args[i + 1]!r}" if a == "--app" and i + 1 < len(args) else ""),
                file=sys.stderr,
            )
            print(STEAM_USAGE, file=sys.stderr)
            return 2
        i += 2
    if sub not in ("plan", "apply"):
        print(STEAM_USAGE, file=sys.stderr)
        return 2
    ctx = user_ctx(user_args)
    if sub == "plan":
        sv = steam_survey(ctx, ids, allow_copy)
        steam_print(ctx, sv)
        if not sv["fatal"]:
            print(MAKE_DEFAULT)
        print(
            "Dry run, nothing changed. Do it with: cst btrfs bulk steam apply"
            + "".join(f" --app {i}" for i in ids)
            + (" --copy" if sv["copy_bytes"] else "")
            + " (Steam closed)"
        )
        return 0
    return steam_apply(ctx, ids, allow_copy)


def user_ctx(args: Sequence[str]) -> Ctx:
    """Ctx from --user NAME (root) or, not as root, the current user and
    BTRFS_BULK_HOME (tests, dry runs)."""
    name = None
    if "--user" in args:
        i = args.index("--user")
        name = args[i + 1] if i + 1 < len(args) else None
    if ROOT:
        if not name:
            try:
                with open(CONF) as f:
                    for line in f:
                        if line.startswith("BULK_USER="):
                            name = line.split("=", 1)[1].strip().strip("'\"")
            except OSError:
                pass
        if not name or name == "root":
            raise SystemExit("btrfs-bulk: needs --user NAME (not root)")
        pw = pwd.getpwnam(name)
        return Ctx(pw.pw_dir, pw)
    return Ctx(os.environ.get("BTRFS_BULK_HOME") or os.path.expanduser("~"))


def alert(title: str, body: str) -> None:
    log(f"<4>btrfs-bulk: {title}: {body}")
    if os.access(NOTIFY, os.X_OK):
        subprocess.run([NOTIFY, "-u", "critical", "-a", "btrfs-bulk", title, body], check=False, capture_output=True)


def boot(ctx: Ctx) -> int:
    """At boot, before any login: nothing of the session holds files open.
    Never blocks the login more than twice for the same leftovers."""
    if not os.path.exists(FLAG):
        if not journals():
            return 0
        # A move interrupted (power loss) before the flag got written back:
        # finish or undo it now, before any app recreates the old path.
        log("btrfs-bulk: recovering an interrupted move into ~/Bulk...")
        deferred, problems = apply(ctx)
        return exit_status(deferred, problems)
    attempts_file = os.path.join(STATE, "boot-attempts")
    try:
        with open(attempts_file) as f:
            attempts = int(f.read())
    except OSError, ValueError:
        attempts = 0
    if attempts >= 2:
        left = " ".join(read_flag())
        write_flag([])
        os.remove(attempts_file)
        alert(
            "~/Bulk: moving at boot gave up",
            f"Left: {left} - see 'journalctl -u btrfs-bulk-boot'; close what uses them and run 'cst btrfs bulk apply'.",
        )
        return 1
    # Durable before the moves start: a hang or hard reset must still count this boot.
    write_atomic(attempts_file, str(attempts + 1), mode=0o644)
    log("btrfs-bulk: moving the items the desktop session kept busy into ~/Bulk...")
    deferred, problems = apply(ctx)
    if not deferred:
        os.remove(attempts_file)
    return exit_status(deferred, problems)


def keep_cmd(cmd: str, args: Sequence[str]) -> int:
    """btrfs-bulk keep|unkeep ID... [--user NAME]"""
    if "--user" in args:
        i = args.index("--user")
        ids, user_args = list(args[:i]) + list(args[i + 2 :]), list(args[i : i + 2])
    else:
        ids, user_args = list(args), []
    bad = [i for i in ids if i not in BY_ID]
    if not ids or bad:
        if bad:
            print(f"btrfs-bulk: unknown item: {' '.join(bad)}", file=sys.stderr)
        print(f"usage: btrfs-bulk {cmd} ID... ; valid ids: {', '.join(BY_ID)}", file=sys.stderr)
        return 2
    lock = run_lock()
    try:
        if cmd == "keep":
            edit_keep(ids, set())
            # Busy items waiting for the boot (and those inside them): not any more.
            write_flag([i for i in read_flag() if i not in ids and (BY_ID[i].after if i in BY_ID else None) not in ids])
        else:
            edit_keep([], set(ids))
    except OSError as e:
        print(f"btrfs-bulk: cannot update {KEEP}: {e}", file=sys.stderr)
        return 1
    finally:
        os.close(lock)
    try:
        ctx = user_ctx(user_args) if ROOT and user_args else None if ROOT else user_ctx([])
    except SystemExit, KeyError:
        ctx = None
    for ident in ids:
        item = BY_ID[ident]
        if cmd == "unkeep":
            print(f"{ident}: no longer kept; the next 'cst btrfs bulk apply' moves it into ~/Bulk")
            continue
        print(
            f"{ident}: {'kept in /home' if item.link else 'left out of ~/Bulk'} by choice; "
            "bulk apply leaves it alone" + ("; its data is in /home snapshots" if item.link else "")
        )
        if ctx and item.link:
            L = ctx.link(item)
            if L and kind(L) == "link" and link_target(L) == ctx.target(item):
                print(f"  ~/{item.link} is still a link into ~/Bulk: to bring the data back, see")
                print("  btrfs/SNAPSHOTS.md, section 12 (Undo one Bulk link)")
    return 0


def main(argv: Sequence[str], entry_file: str) -> int:
    """btrfs-bulk with argv (argv[0] the program); entry_file is the running
    entry point's __file__ (where btrfs-snapshot is found)."""
    Program.entry_file = entry_file
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd in ("keep", "unkeep"):
        return keep_cmd(cmd, argv[2:])
    if cmd == "steam":
        return steam_cmd(argv[2:])
    if cmd not in ("plan", "apply", "status", "boot"):
        print(
            "usage: btrfs-bulk plan|apply|status [--user NAME] | boot | keep|unkeep ID... [--user NAME]"
            " | steam plan|apply [--app ID]... [--copy] [--user NAME]",
            file=sys.stderr,
        )
        return 2
    ctx = user_ctx(argv[2:])
    if cmd == "plan":
        report(ctx, "plan")
        return 0
    if cmd == "status":
        report(ctx, "status")
        return 0
    if cmd == "boot":
        return boot(ctx)
    try:
        os.remove(os.path.join(STATE, "boot-attempts"))  # a new request gets its two boots again
    except OSError:
        pass
    deferred, problems = apply(ctx)
    if deferred:
        print(
            f"{len(deferred)} item(s) in use now ({', '.join(deferred)}): moved at the next boot, before login "
            "(or close what uses them and run 'cst btrfs bulk apply' again)."
        )
    if problems:
        print(
            f"{len(problems)} item(s) need a look ({', '.join(problems)}): see 'cst btrfs bulk status'; "
            "nothing was changed there."
        )
    return exit_status(deferred, problems)
