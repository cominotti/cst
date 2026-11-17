# SPDX-License-Identifier: MPL-2.0
"""Steam's games into the ~/Bulk/steam library: reading Steam's library
files and app manifests, the move of one game (reflink, or a byte-by-byte
copy that is compared) with its journal, its recovery, and the space
limits taken from btrfs-snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import base64
import copy
import errno
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from btrfs.bulk.adapters import (
    Ctx,
    ETC,
    as_user,
    busy_paths,
    chain,
    changed_since,
    empty_dir,
    free_bytes,
    has_mount,
    is_nocow,
    is_subvol,
    jdrop,
    jpath,
    jread,
    jwrite,
    kind,
    log,
    snapshot_program,
    steam_journal_ids,
    tree_stats,
    u_clock,
    u_mkdirs,
    u_rmtree,
)
from btrfs.bulk.domain import (
    BIG,
    CHUNK,
    LF_KEYS,
    MARGIN,
    PROC,
    PROGRESS_BYTES,
    PROGRESS_EVERY,
    PROGRESS_POLL,
    RUN_USER,
    STEAM_APP,
    STEAM_BACKUPS,
    STEAM_READ_MAX,
    STEAM_ROOT,
    SteamRunning,
    Unexpected,
    acf_app,
    dec,
    gib,
    hms,
    is_tool,
    lf_files,
    vdf_dump,
    vdf_load,
    vdf_parse,
)
from cst_core.domain.values import Json


def lf_validate(tree: Mapping[str, Any]) -> dict[str, Any]:
    """Schema of libraryfolders.vdf; returns its "libraryfolders" dict."""
    if list(tree) != ["libraryfolders"] or not isinstance(tree["libraryfolders"], dict):
        raise Unexpected("the top level is not exactly one 'libraryfolders' block")
    lf = tree["libraryfolders"]
    if not lf or list(lf) != [str(i) for i in range(len(lf))]:
        raise Unexpected(f"library numbers are not 0, 1, 2... in order: {', '.join(lf) or 'none'}")
    real: list[tuple[str, str]] = []
    for idx, e in lf.items():
        if not isinstance(e, dict):
            raise Unexpected(f"library {idx} is not a block")
        for k, v in e.items():
            if k == "apps":
                if not isinstance(v, dict):
                    raise Unexpected(f"library {idx}: 'apps' is not a block")
                for a, s in v.items():
                    if not (dec(a) and dec(s)):
                        raise Unexpected(f"library {idx}: app {a!r} {s!r} is not decimal")
            elif k == "path":
                if not isinstance(v, str) or not os.path.isabs(v):
                    raise Unexpected(f"library {idx}: 'path' is not an absolute path")
            elif k in LF_KEYS:
                if not isinstance(v, str):
                    raise Unexpected(f"library {idx}: {k!r} is a block, not a value")
            else:
                raise Unexpected(f"library {idx}: unknown key {k!r}")
        if "path" not in e or "apps" not in e:
            raise Unexpected(f"library {idx} lacks 'path' or 'apps'")
        real.append((idx, os.path.realpath(e["path"])))
    for i, (ia, a) in enumerate(real):
        for ib, b in real[i + 1 :]:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                raise Unexpected(f"libraries {ia} and {ib} are the same folder, or one is inside the other")
    return lf


def lf_index(tree: Mapping[str, Any], path: str) -> str | None:
    """The number of the entry whose path is the same folder as PATH, or
    None. Compared after resolving symlinks, like lf_validate does, so that
    /home/u/Bulk/steam and /var/home/u/Bulk/steam (Silverblue: /home is a
    symlink to /var/home) count as one library, not two."""
    want = os.path.realpath(path)
    for idx, e in tree["libraryfolders"].items():
        if os.path.realpath(e["path"]) == want:
            return idx
    return None


def lf_with_library(tree: Mapping[str, Any], path: str, contentid: str) -> dict[str, Any]:
    """TREE with an entry for PATH appended (a new tree); unchanged if listed."""
    new = copy.deepcopy(dict(tree))
    if lf_index(tree, path) is not None:
        return new
    lf = new["libraryfolders"]
    lf[str(max(int(i) for i in lf) + 1)] = {
        "path": path,
        "label": "",
        "contentid": contentid,
        "totalsize": "0",
        "update_clean_bytes_tally": "0",
        "time_last_update_verified": "0",
        "apps": {},
    }
    return new


def lf_place_app(tree: Mapping[str, Any], appid: str, lib_path: str, size: str) -> dict[str, Any]:
    """A new tree where APPID is listed only under LIB_PATH's entry (with
    SIZE, the string as it was); idempotent."""
    new = copy.deepcopy(dict(tree))
    idx = lf_index(new, lib_path)
    if idx is None:
        raise Unexpected(f"{lib_path} is not a library in libraryfolders.vdf")
    for i, e in new["libraryfolders"].items():
        if i == idx:
            if e["apps"].get(appid) != size:
                e["apps"][appid] = size
        else:
            e["apps"].pop(appid, None)
    return new


# ---- Steam: reading and checking (as root: lstat only; file contents are read as the user) ----
def ctx_uid(ctx: Ctx) -> int:
    return ctx.pw.pw_uid if ctx.pw else os.getuid()


def steam_roots(ctx: Ctx) -> tuple[str, str]:
    return os.path.join(ctx.home, STEAM_ROOT), os.path.join(ctx.bulk, "steam")


def u_read(path: str) -> dict[str, Any] | None:
    """A regular file's bytes (base64), mtime, mode and owner; None if absent.
    A symlink or anything else is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Unexpected(f"{path} is a symlink") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unexpected(f"{path} is not a regular file")
        if st.st_size > STEAM_READ_MAX:
            raise Unexpected(f"{path} is larger than {STEAM_READ_MAX >> 20} MiB")
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
    finally:
        os.close(fd)
    return {
        "b64": base64.b64encode(b"".join(chunks)).decode(),
        "mtime": st.st_mtime_ns,
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
    }


def read_file(ctx: Ctx, path: str) -> dict[str, Any] | None:
    r = as_user(ctx, u_read, path)
    if r is not None:
        r["data"] = base64.b64decode(r["b64"])
    return r


def newest_backup(path: str) -> str | None:
    d, base = os.path.split(path)
    try:
        baks = sorted(n for n in os.listdir(d) if n.startswith(base + ".cst-bak-"))
    except OSError:
        baks = []
    return os.path.join(d, baks[-1]) if baks else None


def steam_read_pair(
    ctx: Ctx,
    settle: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Both libraryfolders.vdf, parsed and checked. They must be identical,
    except that SETTLE (a function tree -> tree: what an interrupted run of
    ours meant to write) makes them equal. Returns S, or None if Steam's
    folder is absent."""
    src, _ = steam_roots(ctx)
    c = chain(ctx.home, STEAM_ROOT)
    if c == "absent":
        return None
    if c != "ok":
        raise Unexpected(f"{ctx.short(c.split(':', 1)[1])} is a symlink or a file, not a folder")
    files = lf_files(src)
    raw: list[bytes] = []
    mtime: list[int] = []
    trees: list[dict[str, Any]] = []
    for f in files:
        r = read_file(ctx, f)
        if r is None:
            raise Unexpected(f"{ctx.short(f)} is missing")
        try:
            t = vdf_load(r["data"])
            lf_validate(t)
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
            bak = newest_backup(files[0])
            raise Unexpected(
                f"the two copies of libraryfolders.vdf ({ctx.short(files[0])} and steamapps/) differ: with Steam "
                f"closed, copy the right one over the other" + (f" (newest backup: {ctx.short(bak)})" if bak else "")
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


def steam_marker(ctx: Ctx, T: str) -> str | None:
    """contentid of ~/Bulk/steam/libraryfolder.vdf, or None if absent."""
    mk = os.path.join(T, "libraryfolder.vdf")
    k = kind(mk)
    if k == "absent":
        return None
    if k != "other":
        raise Unexpected(f"{ctx.short(mk)} is a {'symlink' if k == 'link' else 'folder'}")
    r = read_file(ctx, mk)
    if r is None:
        return None
    if r["uid"] != ctx_uid(ctx):
        raise Unexpected(f"{ctx.short(mk)} is not yours")
    try:
        t = vdf_parse(r["data"])
    except Unexpected as e:
        raise Unexpected(f"{ctx.short(mk)}: {e}") from None
    body = t.get("libraryfolder")
    if list(t) != ["libraryfolder"] or not isinstance(body, dict) or not dec(body.get("contentid")):
        raise Unexpected(f"{ctx.short(mk)} is not a Steam library marker")
    return body["contentid"]


def steam_target(ctx: Ctx, S: Mapping[str, Any], cid: str | None) -> str | None:
    """G4: ~/Bulk/steam as a library. Returns its entry number or None."""
    _, T = steam_roots(ctx)
    c = chain(T, "steamapps/common")
    if c.startswith(("symlink:", "file:")):
        raise Unexpected(f"{ctx.short(c.split(':', 1)[1])} is a symlink or a file, not a folder")
    sa = os.path.join(T, "steamapps")
    if kind(sa) == "dir" and os.lstat(sa).st_uid != ctx_uid(ctx):
        raise Unexpected(f"{ctx.short(sa)} is not yours")
    idx = lf_index(S["tree"], T)
    if idx is not None:
        if cid is None:
            raise Unexpected("~/Bulk/steam is listed as a library, but its libraryfolder.vdf is missing")
        ecid = S["tree"]["libraryfolders"][idx].get("contentid")
        if ecid != cid:
            raise Unexpected(f"~/Bulk/steam: contentid {ecid} in libraryfolders.vdf, {cid} in its libraryfolder.vdf")
    return idx


def nonempty(path: str) -> bool:
    k = kind(path)
    return k in ("other", "link") or (k == "dir" and not empty_dir(path))


def steam_manifests(folder: str) -> set[str]:
    try:
        names = os.listdir(os.path.join(folder, "steamapps"))
    except OSError:
        return set()
    return {m.group(1) for m in (re.fullmatch(r"appmanifest_([0-9]+)\.acf", n) for n in names) if m}


def steam_app(ctx: Ctx, S: Mapping[str, Any], appid: str, explicit: bool) -> dict[str, Any]:
    """One app: state move | ok | skip | update | look, with why, parts, size."""
    src, T = steam_roots(ctx)
    A: dict[str, Any] = {
        "id": appid,
        "name": "",
        "state": "look",
        "why": "",
        "parts": [],
        "bytes": 0,
        "size": "",
        "copy_bytes": 0,
        "reflink_bytes": 0,
        "nocow_files": 0,
        "nocow_sample": "",
    }
    lf = S["tree"]["libraryfolders"]
    s_idx, d_idx = lf_index(S["tree"], src), lf_index(S["tree"], T)
    listed = [i for i, e in lf.items() if appid in e["apps"]]
    m_src = os.path.join(src, "steamapps", f"appmanifest_{appid}.acf")
    m_dst = os.path.join(T, "steamapps", f"appmanifest_{appid}.acf")
    ks, kd = kind(m_src), kind(m_dst)
    if ks != "absent" and kd != "absent":
        A["why"] = "a manifest in both libraries"
        return A
    mpath = m_dst if kd != "absent" else m_src
    if kind(mpath) == "absent":
        A["why"] = "not installed in the main library"
        return A
    try:
        r = read_file(ctx, mpath)
        if r is None:
            raise Unexpected("is missing")
        info = acf_app(vdf_parse(r["data"]), appid)
    except Unexpected as e:
        A["why"] = f"{ctx.short(mpath)}: {e}"
        return A
    A["name"] = info["name"]
    if kd != "absent":
        if listed != [d_idx]:
            A["why"] = (
                f"its manifest is in ~/Bulk/steam, but libraryfolders.vdf lists it under {listed or 'no library'}"
            )
            return A
        A["state"] = "ok"
        return A
    if listed != [s_idx]:
        A["why"] = f"libraryfolders.vdf lists it under {listed or 'no library'}, its manifest is in library {s_idx}"
        return A
    assert s_idx is not None
    A["size"] = lf[s_idx]["apps"][appid]
    why = is_tool(appid, info["name"], info["installdir"])
    if why and not explicit:
        A["state"], A["why"] = "skip", why
        return A
    sa = os.path.join(src, "steamapps")
    busy = [info["mid"]] if info["mid"] else []
    for rel in (f"downloading/{appid}", f"temp/{appid}", f"workshop/downloads/{appid}", f"workshop/temp/{appid}"):
        if nonempty(os.path.join(sa, rel)):
            busy.append(f"steamapps/{rel} is not empty")
    if busy:
        A["state"], A["why"] = "update", "; ".join(busy)
        return A
    if nonempty(os.path.join(sa, "compatdata", appid)):
        A["why"] = (
            f"steamapps/compatdata/{appid} has data (a Proton prefix: saves may be there): move it with "
            "Steam's own Properties > Installed Files > Move install folder"
        )
        return A
    ts = time.strftime("%Y%m%dT%H%M%S")
    parts: list[dict[str, Any]] = []
    for name, rel, what, optional in (
        ("game", f"common/{info['installdir']}", "dir", False),
        ("workshop", f"workshop/content/{appid}", "dir", True),
        ("workshop-acf", f"workshop/appworkshop_{appid}.acf", "file", True),
        ("manifest", f"appmanifest_{appid}.acf", "file", False),
    ):
        p_src = os.path.join(sa, rel)
        k = kind(p_src)
        if k == "absent" and optional:
            continue
        want = "dir" if what == "dir" else "other"
        if k != want or (what == "file" and not stat.S_ISREG(os.lstat(p_src).st_mode)):
            A["why"] = f"{ctx.short(p_src)} is {'missing' if k == 'absent' else 'a ' + k + ', not a ' + what}"
            return A
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
    uid = ctx_uid(ctx)
    for p in parts:
        for x in (p["dst"], p["new"]):
            if kind(x) != "absent":
                A["why"] = f"{ctx.short(x)} exists already: remove it or move it back (nothing is merged)"
                return A
        c = chain(T, os.path.relpath(os.path.dirname(p["dst"]), T))
        if c.startswith(("symlink:", "file:")):
            A["why"] = f"{ctx.short(c.split(':', 1)[1])} is a symlink or a file, not a folder"
            return A
        if cow_problem := steam_target_cow(ctx, T, p["dst"]):
            A["why"] = cow_problem
            return A
        if p["kind"] == "file":
            if os.lstat(p["src"]).st_uid != uid:
                A["why"] = f"{ctx.short(p['src'])} is not yours"
                return A
            size = os.lstat(p["src"]).st_size
        else:
            if is_subvol(p["src"]) or has_mount(p["src"]):
                A["why"] = f"{ctx.short(p['src'])} is a subvolume or a mount point"
                return A
            _, size, foreign, others = tree_stats(p["src"], uid)
            if foreign:
                A["why"] = f"{ctx.short(p['src'])} contains another subvolume or a mount"
                return A
            if others or os.lstat(p["src"]).st_uid != uid:
                A["why"] = f"{ctx.short(p['src'])}: entries there are not yours (made by sudo or a container?)"
                return A
        nocow_files, first_nocow, sample = steam_nocow_scan(p["src"])
        # A NOCOW file cannot be cloned into copy-on-write ~/Bulk/steam: the
        # whole part is copied byte by byte instead (see steam_nocow_scan).
        # copy_bytes is the size of such parts, not the size of the +C files
        # (one empty +C file is enough): the messages give both.
        p["mode"], p["bytes"] = ("copy" if nocow_files else "reflink"), size
        p["sample"] = "" if nocow_files else sample
        A["bytes"] += size
        A["copy_bytes" if nocow_files else "reflink_bytes"] += size
        A["nocow_files"] += nocow_files
        A["nocow_sample"] = A["nocow_sample"] or first_nocow
    if workshop_problem := steam_workshop_check(ctx, parts, appid):
        A["why"] = workshop_problem
        return A
    A["state"], A["parts"] = "move", parts
    return A


def steam_nocow_scan(top: str) -> tuple[int, str, str]:
    """(number of regular files with +C, the first of them or '', a
    copy-on-write regular file with data or '') at or below TOP (a file or a
    folder; lstat, no link followed).

    btrfs refuses to clone (reflink) between a NOCOW (+C) file and a
    copy-on-write one (FICLONE fails with EINVAL, cp says 'Invalid
    argument'), and ~/Bulk/steam stays copy-on-write on purpose (checksums,
    so RAID1 scrub can repair, and compression). +C cannot be taken off a
    file that has data, so such a file can only move by a real copy. Any +C
    file counts, empty ones too (the clone is refused before its length is
    looked at). The sample is for the reflink test (u_reflink_probe)."""
    nocow, first, sample = 0, "", ""
    st = os.lstat(top)
    if not stat.S_ISDIR(st.st_mode):
        if stat.S_ISREG(st.st_mode):
            if is_nocow(top):
                return 1, top, ""
            return 0, "", (top if st.st_size else "")
        return 0, "", ""
    dev = st.st_dev
    for root, dirs, names in os.walk(top):
        dirs[:] = [d for d in dirs if os.lstat(os.path.join(root, d)).st_dev == dev]
        for n in names:
            p = os.path.join(root, n)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                continue
            if is_nocow(p):
                nocow += 1
                first = first or p
            elif st.st_size and not sample:
                sample = p
    return nocow, first, sample


def steam_target_cow(ctx: Ctx, T: str, dst: str) -> str:
    """'' if every existing folder from ~/Bulk/steam down to DST's folder is
    copy-on-write; else why not. New files inherit +C from their folder, and
    a +C folder would make every moved file NOCOW (no checksums, no
    compression), which ~/Bulk/steam must not have."""
    path = T
    for part in [""] + [x for x in os.path.relpath(os.path.dirname(dst), T).split("/") if x]:
        path = os.path.join(path, part) if part else path
        if kind(path) != "dir":
            break
        if is_nocow(path):
            return (
                f"{ctx.short(path)} has +C (NOCOW): what is created in it would be NOCOW too (no checksums, "
                f"no compression); ~/Bulk/steam must stay copy-on-write: 'chattr -C {path}' (on a folder it "
                "only changes what is created there afterwards), then run again"
            )
    return ""


def steam_workshop_check(ctx: Ctx, parts: Sequence[Mapping[str, Any]], appid: str) -> str:
    for p in parts:
        if p["name"] != "workshop-acf":
            continue
        try:
            r = read_file(ctx, p["src"])
            if r is None:
                raise Unexpected("is missing")
            t = vdf_parse(r["data"])
        except Unexpected as e:
            return f"{ctx.short(p['src'])}: {e}"
        body = t.get("AppWorkshop")
        if list(t) != ["AppWorkshop"] or not isinstance(body, dict) or body.get("appid") != appid:
            return f"{ctx.short(p['src'])} is not the workshop file of app {appid}"
    return ""


def steam_running(ctx: Ctx) -> list[str]:
    """Why Steam seems to run (empty: it does not). Every process of the
    host (a Flatpak's processes are visible there too), Flatpak instances of
    Steam, and Steam's own pid files."""
    why: list[str] = []
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            with open(os.path.join(PROC, pid, "comm")) as f:
                comm = f.read().strip()
        except OSError:
            continue
        try:
            exe = os.readlink(os.path.join(PROC, pid, "exe"))
        except OSError:
            exe = ""
        exe = exe.removesuffix(" (deleted)")
        if (
            comm in ("steam", "steamwebhelper", "steam.sh")
            or comm.startswith("pressure-vessel")
            or exe.endswith(("/ubuntu12_32/steam", "/ubuntu12_64/steamwebhelper", "/ubuntu12_32/reaper"))
        ):
            why.append(f"process {pid} ({comm})")
    try:
        uids = os.listdir(RUN_USER)
    except OSError:
        uids = []
    for uid in uids:
        base = os.path.join(RUN_USER, uid, ".flatpak")
        try:
            insts = os.listdir(base)
        except OSError:
            continue
        for inst in insts:
            try:
                with open(os.path.join(base, inst, "info")) as f:
                    info = f.read()
                with open(os.path.join(base, inst, "pid")) as f:
                    pid = f.read().strip()
            except OSError:
                continue
            if (
                f"\nname={STEAM_APP}\n" in "\n" + info + "\n"
                and pid.isdigit()
                and os.path.exists(os.path.join(PROC, pid))
            ):
                why.append(f"Flatpak instance {inst} (pid {pid})")
    for rel in (".steam/steam.pid", ".var/app/com.valvesoftware.Steam/.steam/steam.pid"):
        if (
            chain(ctx.home, os.path.dirname(rel)) != "ok"
            or kind(os.path.join(ctx.home, rel)) != "other"
            or not stat.S_ISREG(os.lstat(os.path.join(ctx.home, rel)).st_mode)
        ):
            continue
        try:
            with open(os.path.join(ctx.home, rel)) as f:
                pid = f.read(32).strip()
            with open(os.path.join(PROC, pid, "comm")) as f:
                comm = f.read().strip()
        except OSError, ValueError:
            continue
        if pid.isdigit() and comm.startswith("steam"):
            why.append(f"~/{rel} names the live process {pid}")
    return sorted(set(why))


# ---- Steam: changes (run as the user) ----
def copy_reflink(src: str, dst: str) -> None:
    """cp -a with --reflink=always: the data blocks are shared, none is
    written; fails (instead of copying bytes) where a clone is impossible."""
    r = subprocess.run(
        ["cp", "-a", "--reflink=always", "--no-target-directory", "--", src, dst],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"reflink copy failed: {r.stderr.strip()[:300]}")


def listing(top: str) -> list[tuple[str, str, int, int, int, str]]:
    """(relpath, type, size, mtime_ns, mode, link target) of top and all
    below it (lstat, no link followed)."""
    out: list[tuple[str, str, int, int, int, str]] = []
    stack = [(top, "")]
    while stack:
        path, rel = stack.pop()
        st = os.lstat(path)
        m, mode = st.st_mode, stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(m):
            out.append((rel, "l", 0, 0, mode, os.readlink(path)))
        elif stat.S_ISREG(m):
            out.append((rel, "f", st.st_size, st.st_mtime_ns, mode, ""))
        elif stat.S_ISDIR(m):
            out.append((rel, "d", 0, st.st_mtime_ns, mode, ""))
            for n in os.listdir(path):
                stack.append((os.path.join(path, n), f"{rel}/{n}" if rel else n))
        else:
            out.append((rel, "o", 0, st.st_mtime_ns, mode, str(stat.S_IFMT(m))))
    return sorted(out)


def u_reflink_probe(src_file: str, T: str) -> bool:
    """Clone SRC_FILE (a copy-on-write file of a part that moves by reflink)
    into ~/Bulk/steam and delete the clone again."""
    probe = os.path.join(T, ".bulk-probe")
    if kind(probe) != "absent":
        u_rmtree(probe)
    try:
        copy_reflink(src_file, probe)
    except RuntimeError as e:
        raise Unexpected(
            f"reflink of a copy-on-write file into ~/Bulk/steam failed (is ~/Bulk on another filesystem?): {e}"
        ) from None
    finally:
        if kind(probe) != "absent":
            u_rmtree(probe)
    return True


def idle_priority() -> None:
    """This process, and what it starts, at the lowest CPU priority and the
    idle IO class (the IO class only counts with the BFQ IO scheduler)."""
    try:
        os.nice(19)
    except OSError:
        pass
    if shutil.which("ionice"):
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False, capture_output=True)


def idle_cmd(cmd: list[str]) -> list[str]:
    """CMD run at idle IO and lowest CPU priority (where ionice/nice exist)."""
    pre = (["ionice", "-c3"] if shutil.which("ionice") else []) + (["nice", "-n19"] if shutil.which("nice") else [])
    return pre + cmd


def tree_bytes(top: str) -> int:
    """Bytes of the regular files at/below TOP so far (a copy in progress:
    entries may come and go; those are skipped)."""
    try:
        st = os.lstat(top)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size if stat.S_ISREG(st.st_mode) else 0
    n = 0
    for root, _, names in os.walk(top):
        for x in names:
            try:
                st = os.lstat(os.path.join(root, x))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                n += st.st_size
    return n


class Progress:
    """A line every PROGRESS_EVERY seconds or PROGRESS_BYTES, so a long copy
    or comparison shows it is alive."""

    def __init__(self, what: str, total: int) -> None:
        self.what, self.total = what, total
        self.start = self.last_t = time.monotonic()
        self.last_b = 0

    def update(self, done: int, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.last_t >= PROGRESS_EVERY or done - self.last_b >= PROGRESS_BYTES:
            log(f"    {self.what}: {gib(done)} of {gib(self.total)}, {hms(now - self.start)}")
            self.last_t, self.last_b = now, done


def copy_bytes(src: str, dst: str, label: str, total: int) -> None:
    """cp -a with --reflink=never: every byte is written again, at idle
    priority, into DST (inside ~/Bulk/steam, copy-on-write: the new files
    get checksums and compression). A progress line now and then. A kill or
    an error here also stops cp."""
    cmd = idle_cmd(["cp", "-a", "--reflink=never", "--sparse=auto", "--no-target-directory", "--", src, dst])
    prog = Progress(f"copying {label}", total)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        try:
            while True:
                try:
                    proc.wait(timeout=PROGRESS_POLL)
                    break
                except subprocess.TimeoutExpired:
                    prog.update(tree_bytes(dst))
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(f"copy failed: {err.read().decode(errors='replace').strip()[:300]}")


def read_full(fd: int, view: memoryview) -> int:
    """Fill VIEW from FD; fewer bytes only at the end of the file."""
    n = 0
    while n < len(view):
        k = os.readv(fd, [view[n:]])
        if k == 0:
            break
        n += k
    return n


def same_file(a: str, b: str, on_chunk: Callable[[int], None] | None = None) -> bool:
    """True if the regular files A and B have the same bytes. B (the new
    copy) is fsynced (an error raises OSError), then read from the disk, not
    from memory (only clean pages can be dropped), so btrfs checks its
    checksums; both are dropped from the page cache after. on_chunk(n): called
    after each CHUNK compared, with the bytes compared so far in this file."""
    fa = os.open(a, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fb = os.open(b, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fb)
            os.posix_fadvise(fb, 0, 0, os.POSIX_FADV_DONTNEED)
            ba, bb = bytearray(CHUNK), bytearray(CHUNK)
            va, vb = memoryview(ba), memoryview(bb)
            n = 0
            while True:
                na, nb = read_full(fa, va), read_full(fb, vb)
                if na != nb or va[:na] != vb[:nb]:
                    return False
                n += na
                if on_chunk:
                    on_chunk(n)
                if na < CHUNK:
                    return True
        finally:
            os.posix_fadvise(fb, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fb)
    finally:
        os.posix_fadvise(fa, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fa)


def u_verify_content(src: str, new: str, label: str, total: int) -> str:
    """Every regular file of NEW has the same bytes as in SRC (the tree
    itself, names, types, sizes, modes, links, was compared before). Returns
    the first differing path relative to SRC, or ''. The copy is written to
    the disk first and read back from there; if that write fails (sync or
    fsync reports an error), RuntimeError: the caller drops the copy and the
    original stays."""
    r = subprocess.run(["sync", "-f", new], check=False, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"the copy could not be written to the disk (sync: {(r.stderr.strip() or f'exit {r.returncode}')[:300]})"
        )
    prog = Progress(f"comparing {label}", total)
    done = 0
    for rel, typ, size, _, _, _ in listing(src):
        if typ != "f":
            continue
        a = os.path.join(src, rel) if rel else src
        b = os.path.join(new, rel) if rel else new
        try:
            same = same_file(a, b, lambda n, done=done: prog.update(done + n))
        except OSError as e:
            raise RuntimeError(f"{rel or os.path.basename(src)} could not be compared with its copy: {e}") from e
        if not same:
            return rel or os.path.basename(src)
        done += size
        prog.update(done)
    return ""


def nocow_entries(top: str) -> int:
    """Entries (folders and regular files) with +C at/below TOP."""
    st = os.lstat(top)
    if not stat.S_ISDIR(st.st_mode):
        return int(stat.S_ISREG(st.st_mode) and is_nocow(top))
    n = int(is_nocow(top))
    for root, dirs, names in os.walk(top):
        for x in dirs + names:
            p = os.path.join(root, x)
            m = os.lstat(p).st_mode
            if (stat.S_ISDIR(m) or stat.S_ISREG(m)) and is_nocow(p):
                n += 1
    return n


def u_steam_prepare(parts: Sequence[Mapping[str, Any]], idle: bool = False) -> int:
    """Copy every part to its .bulk-new name and check it: a reflink clone,
    or (mode 'copy': NOCOW data) a real copy whose every byte is compared
    afterwards. Returns the ctime stamp (ns) taken before the copy. idle:
    this is a child process of its own (as_user): it lowers its own
    priority for the copy and the comparison."""
    for p in parts:
        for x in (p["new"], p["dst"]):
            if kind(x) != "absent":
                raise Unexpected(f"{x} exists already: remove it or move it back")
    if idle and any(p.get("mode") == "copy" for p in parts):
        idle_priority()
    stamp = u_clock(parts[-1]["new"])
    uid = os.getuid()
    try:
        for p in parts:
            if p["kind"] == "dir":
                _, _, foreign, others = tree_stats(p["src"], uid)
                if foreign:
                    raise Unexpected(f"{p['src']} contains another subvolume or a mount")
                if others:
                    raise Unexpected(f"{others} entries in {p['src']} are not yours")
            label = f"{p['name']} ({os.path.basename(p['src'])})"
            if p.get("mode") == "copy":
                if is_nocow(os.path.dirname(p["new"])):
                    raise Unexpected(f"{os.path.dirname(p['new'])} has +C: the copy would be NOCOW too")
                copy_bytes(p["src"], p["new"], label, p.get("bytes", 0))
            else:
                copy_reflink(p["src"], p["new"])
            a, b = listing(p["src"]), listing(p["new"])
            if a != b:
                diff = next((x for x, y in zip(a, b, strict=False) if x != y), None) or f"{len(a)} vs {len(b)} entries"
                raise RuntimeError(f"copy check failed for {p['src']} (first difference: {diff})")
            if p.get("mode") == "copy":
                if n := nocow_entries(p["new"]):
                    raise RuntimeError(f"the copy of {p['src']} has {n} NOCOW (+C) entries; it must be copy-on-write")
                if bad := u_verify_content(p["src"], p["new"], label, p.get("bytes", 0)):
                    raise RuntimeError(f"copy check failed for {p['src']}: the content of {bad} differs")
    except BaseException:
        u_steam_drop_news({"parts": parts})
        raise
    return stamp


def u_steam_drop_news(j: Mapping[str, Any]) -> bool:
    for p in j["parts"]:
        if kind(p["new"]) != "absent":
            u_rmtree(p["new"])
    return True


def u_steam_changed(j: Mapping[str, Any]) -> bool:
    """Was a source written to since the copy started?"""
    return any(changed_since(p["src"], j["stamp"]) for p in j["parts"])


def u_steam_switch(j: Mapping[str, Any]) -> bool:
    """Per part, in order (the manifest last): src -> old, new -> dst."""
    for p in j["parts"]:
        if kind(p["dst"]) != "absent":
            raise Unexpected(f"{p['dst']} appeared meanwhile")
        os.rename(p["src"], p["old"])
        os.rename(p["new"], p["dst"])
    return True


def steam_all_switched(j: Mapping[str, Any]) -> bool:
    return all(
        kind(p["dst"]) != "absent"
        and kind(p["new"]) == "absent"
        and kind(p["old"]) != "absent"
        and kind(p["src"]) == "absent"
        for p in j["parts"]
    )


def written_since(path: str, stamp: int) -> bool:
    if kind(path) == "absent":
        return False
    if os.lstat(path).st_mtime_ns >= stamp:
        return True
    return kind(path) == "dir" and changed_since(path, stamp, mindepth=1)


def u_steam_late(j: Mapping[str, Any]) -> bool:
    """After the switch: was an old copy written to (see u_late_write)?"""
    return any(written_since(p["old"], j["stamp"]) for p in j["parts"])


def u_steam_new_written(j: Mapping[str, Any]) -> bool:
    """After the switch: was a moved part written to in the new library?"""
    return any(written_since(p["dst"], j["stamp2"]) for p in j["parts"])


def u_steam_rollback(j: Mapping[str, Any]) -> bool:
    """Undo a switch, or what an interruption left of one: the manifest
    leaves the new library first, the game comes back first. If a source
    path was created again meanwhile, nothing is deleted (Unexpected)."""
    parts = j["parts"]
    for p in parts:
        if kind(p["old"]) != "absent" and kind(p["src"]) != "absent":
            raise Unexpected(
                f"{p['src']} was created again while the data waited in {p['old']}: move what you "
                f"need into {p['old']}, delete {p['src']}, then run 'bulk steam apply' again"
            )
    for p in reversed(parts):
        if kind(p["old"]) != "absent" and kind(p["dst"]) != "absent":
            if kind(p["new"]) != "absent":
                raise Unexpected(f"both {p['dst']} and {p['new']} exist")
            os.rename(p["dst"], p["new"])
    for p in parts:
        if kind(p["old"]) != "absent":
            os.rename(p["old"], p["src"])
    for p in parts:
        if kind(p["new"]) != "absent":
            if "stamp2" in j and written_since(p["new"], j["stamp2"]):
                raise Unexpected(
                    f"the data is back at {p['src']}, but the copy {p['new']} was written to as well: "
                    f"copy what you need from it, delete it, then run 'bulk steam apply' again"
                )
            u_rmtree(p["new"])
    return True


def u_steam_finish(j: Mapping[str, Any]) -> bool:
    for p in j["parts"]:
        for x in (p["old"], p["new"]):
            if kind(x) != "absent":
                u_rmtree(x)
    return True


def fsync_dir(d: str) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def u_write_new(path: str, data: bytes, mode: int) -> None:
    """Create PATH (O_EXCL, no symlink followed) with DATA and MODE, synced."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def u_write_marker(path: str, contentid: str) -> bool:
    u_write_new(path, vdf_dump({"libraryfolder": {"contentid": contentid, "label": ""}}), 0o644)
    fsync_dir(os.path.dirname(path))
    return True


def u_write_vdf_pair(
    files: Sequence[str],
    expect_b64: Sequence[str],
    expect_mtime: Sequence[int],
    data_b64: str,
    ts: str,
    backup: bool,
) -> list[Json]:
    """Write DATA over both libraryfolders.vdf (config/ first): each must
    still be what was read; a backup of each (once per run, 5 kept); then a
    temp file, fsync, rename over it, fsync of the folder. Returns the new
    mtimes."""
    data = base64.b64decode(data_b64)
    olds = []
    for f, e, m in zip(files, expect_b64, expect_mtime, strict=False):
        r = u_read(f)
        if r is None or r["b64"] != e or r["mtime"] != m:
            raise Unexpected(f"{f} changed meanwhile (Steam started?): nothing written; close Steam, run again")
        olds.append(r)
    if backup:
        for f, r in zip(files, olds, strict=False):
            bak = f"{f}.cst-bak-{ts}"
            if kind(bak) == "absent":
                u_write_new(bak, base64.b64decode(r["b64"]), r["mode"])
                fsync_dir(os.path.dirname(f))
            d, base = os.path.split(f)
            baks = sorted(n for n in os.listdir(d) if n.startswith(base + ".cst-bak-"))
            for n in baks[:-STEAM_BACKUPS]:
                os.unlink(os.path.join(d, n))
    out: list[Json] = []  # mtimes (ints): they come back through as_user's JSON
    for f, r in zip(files, olds, strict=False):
        tmp = f + ".cst-tmp"
        if kind(tmp) != "absent":
            os.unlink(tmp)  # left by a kill
        u_write_new(tmp, data, r["mode"])
        os.rename(tmp, f)
        fsync_dir(os.path.dirname(f))
        out.append(os.lstat(f).st_mtime_ns)
    return out


def write_vdf_pair(ctx: Ctx, S: dict[str, Any], tree: Mapping[str, Any]) -> bool:
    """Write TREE into both copies if either differs from it. True if written.
    TREE must pass the same checks as a file we read: never write a pair
    that the next run would refuse."""
    try:
        lf_validate(tree)
    except Unexpected as e:
        raise Unexpected(f"refused to write libraryfolders.vdf: {e}") from None
    data = vdf_dump(tree)
    if all(r == data for r in S["raw"]):
        S["tree"] = tree
        return False
    running = steam_running(ctx)
    if running:
        raise SteamRunning("; ".join(running[:3]))
    S["mtime"] = as_user(
        ctx,
        u_write_vdf_pair,
        S["files"],
        [base64.b64encode(r).decode() for r in S["raw"]],
        S["mtime"],
        base64.b64encode(data).decode(),
        S["ts"],
        not S["backed_up"],
    )
    S.update(raw=[data, data], tree=tree, backed_up=True)
    return True


def steam_conflict_text(ctx: Ctx, j: Mapping[str, Any]) -> str:
    olds = ", ".join(ctx.short(p["old"]) for p in j["parts"] if kind(p["old"]) != "absent")
    return (
        f"app {j['appid']} was written to on both sides during its move: compare {olds} (the old copy) "
        f"with the same names in ~/Bulk/steam (in use now), copy what you need there, delete the old copy, "
        "then run 'cst btrfs bulk steam apply' again"
    )


def steam_settle_vdf(ctx: Ctx, j: Mapping[str, Any], S: dict[str, Any] | None = None) -> None:
    """Both libraryfolders.vdf: the library listed, the app under it only."""
    T, appid, size, cid = j["dst_lib"], j["appid"], j["size"], j["contentid"]

    def settle(t: Mapping[str, Any]) -> dict[str, Any]:
        return lf_place_app(lf_with_library(t, T, cid), appid, T, size)

    if S is None:
        S = steam_read_pair(ctx, settle)
        if S is None:
            raise Unexpected("Steam's folder is gone")
    try:
        write_vdf_pair(ctx, S, settle(S["tree"]))
    except SteamRunning as e:
        raise Unexpected(f"Steam is running ({e}): close it; the next run finishes this move") from None


def steam_finish(ctx: Ctx, j: dict[str, Any], S: dict[str, Any] | None = None) -> str:
    """The switch passed its checks: the VDF files, then the old copies go."""
    ident = "steam-" + j["appid"]
    if j["phase"] != "finishing":
        j["phase"] = "switched"
        jwrite(ident, j)
        steam_settle_vdf(ctx, j, S)
        j["phase"] = "finishing"  # a kill now: the next repair goes on deleting
        jwrite(ident, j)
    else:
        steam_settle_vdf(ctx, j, S)
    as_user(ctx, u_steam_finish, j)
    jdrop(ident)
    return "ok"


def steam_after_switch(ctx: Ctx, j: dict[str, Any], S: dict[str, Any] | None = None) -> str:
    """The last check after a complete switch, as in migrate(): a write into
    an old copy undoes the move; a write on both sides keeps both."""
    ident = "steam-" + j["appid"]
    if busy_paths([p["old"] for p in j["parts"]], ctx.home) or as_user(ctx, u_steam_late, j):
        if as_user(ctx, u_steam_new_written, j):
            j["conflict"] = True
            jwrite(ident, j)
            raise Unexpected(steam_conflict_text(ctx, j))
        j["undo"] = True
        jwrite(ident, j)
        as_user(ctx, u_steam_rollback, j)
        jdrop(ident)
        return "changed"
    return steam_finish(ctx, j, S)


def steam_recover(ctx: Ctx, j: dict[str, Any]) -> str:
    """Finish or undo what an interrupted Steam move left."""
    ident = "steam-" + j["appid"]
    phase = j.get("phase")
    if j.get("conflict"):
        if any(kind(p["old"]) != "absent" for p in j["parts"]):
            raise Unexpected(steam_conflict_text(ctx, j))
        j["phase"] = "finishing"
        steam_finish(ctx, j)
        return "the old copy is gone: the move is complete"
    if phase == "finishing":
        steam_finish(ctx, j)
        return "deleted the old copy left by an interrupted run"
    if phase == "switched":
        steam_finish(ctx, j)
        return "finished an interrupted move"
    if j.get("undo") or phase == "prepared" or (phase == "switching" and not steam_all_switched(j)):
        as_user(ctx, u_steam_rollback if phase != "prepared" else u_steam_drop_news, j)
        jdrop(ident)
        return "undid an interrupted move (the game is back in the main library)"
    if phase == "switching":
        if steam_after_switch(ctx, j) == "changed":
            return "undid an interrupted move: an old copy was written to during the switch"
        return "finished an interrupted move"
    raise Unexpected(f"unknown phase {phase!r}")


def steam_recover_all(ctx: Ctx) -> list[str]:
    stuck: list[str] = []
    for ident in steam_journal_ids():
        j = jread(ident)
        if not j or j.get("kind") != "steam":
            stuck.append(ident)
            log(f"NEEDS A LOOK: {ident}: unreadable journal {jpath(ident)}")
            continue
        try:
            log(f"  {ident}: {steam_recover(ctx, j)}")
        except (OSError, RuntimeError, Unexpected) as e:
            stuck.append(ident)
            log(
                f"NEEDS A LOOK: {ident}: an interrupted Steam game move: {e} (journal kept in {jpath(ident)}; "
                "snapshots wait until it is gone)"
            )
    return stuck


def snapshot_guard() -> dict[str, int]:
    """btrfs-snapshot's space guard, in bytes: {'pause': no new /home
    snapshot at or below it, 'resume': they come back only above it,
    'emergency': at or below it the hourly run purges most of the snapshot
    history}. Read the way btrfs-snapshot gets them, so the two cannot drift
    apart: its defaults (snapshot_program()), then snapshots.conf in ETC, which
    overrides them (a plain KEY=NUMBER line, quotes allowed; the last one
    wins). The built-in defaults if neither can be read. btrfs-snapshot
    measures btrfs' "Free (estimated)", this script statvfs: close, not
    the same, so MARGIN stays on top."""
    vals = {"pause": 250, "resume": 270, "emergency": 150}
    names = {"pause": "SKIP_AT_OR_BELOW_GIB", "resume": "RESUME_ABOVE_GIB", "emergency": "EMERGENCY_AT_OR_BELOW_GIB"}
    for path in (snapshot_program(), os.path.join(ETC, "snapshots.conf")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(1 << 20)
        except OSError:
            continue
        for key, name in names.items():
            found = re.findall(rf"""^[ \t]*{name}=(["']?)(\d{{1,6}})\1[ \t]*(?:#.*)?$""", text, re.M)
            if found:
                vals[key] = int(found[-1][1])
    return {k: v * BIG for k, v in vals.items()}


def steam_copy_floor() -> int:
    """Free space a real copy must leave behind: the snapshot emergency
    limit plus MARGIN. The old copy is deleted only after the new one is
    checked, and while /home snapshots still hold it, deleting it frees
    nothing: without this floor a large copy could push free space down to
    the emergency purge of btrfs-snapshot, which deletes most of the
    snapshot history."""
    return snapshot_guard()["emergency"] + MARGIN


def steam_copy_need(a: Mapping[str, Any]) -> int:
    """Free space a game's real copies need in ~/Bulk (0: reflinks only)."""
    return a["copy_bytes"] + steam_copy_floor() if a["copy_bytes"] else 0


def steam_move(ctx: Ctx, sv: Mapping[str, Any], a: Mapping[str, Any]) -> str:
    """Move one app. Returns ok | running | busy | changed | space."""
    S, (_, T) = sv["S"], steam_roots(ctx)
    ident, parts = "steam-" + a["id"], a["parts"]
    if steam_running(ctx):
        return "running"
    if busy_paths([p["src"] for p in parts], ctx.home):
        return "busy"
    # A real copy shares no blocks: its full size must be free (asked again
    # per game: an earlier game's old copy may still be held by snapshots).
    if steam_copy_need(a) and free_bytes(T) < steam_copy_need(a):
        return "space"
    j: dict[str, Any] = {
        "kind": "steam",
        "appid": a["id"],
        "src_lib": steam_roots(ctx)[0],
        "dst_lib": T,
        "size": a["size"],
        "contentid": sv["cid"],
        "parts": parts,
        "stamp": 0,
        "phase": "prepared",
        "conflict": False,
        "undo": False,
    }
    jwrite(ident, j)  # a kill during the copy: the repair deletes the copies
    try:
        for d in sorted({os.path.relpath(os.path.dirname(p["dst"]), T) for p in parts}):
            as_user(ctx, u_mkdirs, T, d, False)
        j["stamp"] = as_user(ctx, u_steam_prepare, parts, ctx.pw is not None)
        j["stamp2"] = as_user(ctx, u_clock, parts[-1]["new"])
        jwrite(ident, j)
        # Race guard, last moment before the switch.
        why = (
            "running"
            if steam_running(ctx)
            else "busy"
            if busy_paths([p["src"] for p in parts], ctx.home)
            else "changed"
            if as_user(ctx, u_steam_changed, j)
            else ""
        )
    except OSError, RuntimeError, Unexpected:
        as_user(ctx, u_steam_drop_news, j)
        jdrop(ident)
        raise
    if why:
        as_user(ctx, u_steam_drop_news, j)
        jdrop(ident)
        return why
    j["phase"] = "switching"
    jwrite(ident, j)
    try:
        as_user(ctx, u_steam_switch, j)
    except OSError, RuntimeError, Unexpected:
        as_user(ctx, u_steam_rollback, j)
        jdrop(ident)
        raise
    return steam_after_switch(ctx, j, S)


def ensure_library(ctx: Ctx, sv: dict[str, Any]) -> None:
    """~/Bulk/steam as a Steam library: steamapps/common, the marker
    libraryfolder.vdf (a new random contentid), the entry in both
    libraryfolders.vdf. Idempotent."""
    _, T = steam_roots(ctx)
    if steam_running(ctx):
        raise SteamRunning("Steam started")
    as_user(ctx, u_mkdirs, T, "steamapps/common", False)
    if sv["cid"] is None:
        sv["cid"] = str(secrets.randbits(63) or 1)
        as_user(ctx, u_write_marker, os.path.join(T, "libraryfolder.vdf"), sv["cid"])
        log(f"{'done:':<10} ~/Bulk/steam/libraryfolder.vdf (contentid {sv['cid']})")
    if write_vdf_pair(ctx, sv["S"], lf_with_library(sv["S"]["tree"], T, sv["cid"])):
        log(
            f"{'done:':<10} ~/Bulk/steam is library {lf_index(sv['S']['tree'], T)} in both libraryfolders.vdf "
            f"(backups: *.cst-bak-{sv['S']['ts']})"
        )
    sv["idx"] = lf_index(sv["S"]["tree"], T)


def steam_nocow_files(ctx: Ctx, a: Mapping[str, Any]) -> str:
    """'K file(s) ... have +C (NOCOW), for example PATH' for a game with a
    real copy."""
    k = a["nocow_files"]
    return (
        f"{k} file{'s' if k != 1 else ''} in it ha{'ve' if k != 1 else 's'} +C (NOCOW), for example "
        f"{ctx.short(a['nocow_sample'])}"
    )


def steam_move_lines(ctx: Ctx, a: Mapping[str, Any], allow_copy: bool) -> list[str]:
    """What plan says about a game that moves: reflink, real copy, or both."""
    c, r = a["copy_bytes"], a["reflink_bytes"]
    out: list[str] = []
    if r or not c:
        out.append(f"move {gib(r)} (reflink, no data written)")
    if c:
        out.append(f"copy {gib(c)} byte by byte: {steam_nocow_files(ctx, a)};")
        out.append("btrfs cannot reflink those, so the folders holding them are rewritten into COW ~/Bulk/steam")
        out.append("(gaining checksums and compression)")
        if not allow_copy:
            out.append("add --copy to allow it")
    if len(out) > 1 and r:
        out[0] += " and"
    return out
