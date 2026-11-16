"""What btrfs-bulk reads and changes on the machine: its settings and state
paths, btrfs and the filesystem (subvolumes, +C, mounts, busy files,
copies), the child process that runs as the user (Ctx, as_user), the
inspection of the home, the steps run as the user (u_*), the journals of
interrupted moves, the boot flag, the keep list and the run lock.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from cst_core.adapters.runtime import sibling_program
from typing import Any
from typing import cast
import fcntl
import json
import os
import pwd
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
from btrfs.bulk.domain import (
    BTRFS_MAGIC,
    BY_ID,
    ITEMS,
    Item,
    KEEP_HEADER,
    NOCOW_TOP,
    SUBVOL_CREATE,
    TAG_SIGNATURE,
    TAG_TEXT,
    Unexpected,
)
from cst_core.adapters.locks import take_lock
from cst_core.adapters.nocow import FS_NOCOW_FL, inode_flags, set_nocow

ROOT = os.geteuid() == 0


def _env(name: str, default: str) -> str:
    # Overrides for tests and dry runs; never honoured as root.
    return default if ROOT else os.environ.get(name, default)


ETC = _env("BTRFS_BULK_ETC", "/etc/btrfs-setup")
STATE = _env("BTRFS_BULK_STATE", "/var/lib/btrfs-bulk")
LOCK = _env("BTRFS_BULK_LOCK", "/run/btrfs-snapshot.lock")  # shared with btrfs-snapshot
# btrfs-snapshot (the busy check, the conversions, the space limits):
# BTRFS_BULK_SNAPSHOT when set (never as root), else snapshot_program().
SNAPSHOT: str | None = None if ROOT else os.environ.get("BTRFS_BULK_SNAPSHOT")
CONF = os.path.join(ETC, "bulk.conf")
FLAG = os.path.join(ETC, "bulk-at-boot")
KEEP = os.path.join(ETC, "bulk-keep")
CONVERT_PLAN = os.path.join(ETC, "convert-plan")


def log(msg: str) -> None:
    print(msg, flush=True)


class Program:
    """The running btrfs-bulk: main() sets entry_file (its entry point's __file__)."""

    entry_file: str = ""


def snapshot_program() -> str:
    """btrfs-snapshot: SNAPSHOT when set; else the checkout's copy from a
    checkout, the installed one (its manifest dest) from the zipapp."""
    if SNAPSHOT is not None:
        return SNAPSHOT
    if not Program.entry_file:
        raise RuntimeError("Program.entry_file is not set: main() sets it")
    return sibling_program("btrfs/btrfs-snapshot", Program.entry_file)


def run_lock() -> int:
    """Wait for the run lock (LOCK, shared with btrfs-snapshot); its fd (close it to unlock)."""
    fd = take_lock(LOCK, wait=True, mode=0o644, nofollow=False)
    if fd is None:  # only a non-blocking take returns None
        raise RuntimeError(f"{LOCK}: not locked")
    return fd


def is_nocow(path: str) -> bool:
    return bool(inode_flags(path) & FS_NOCOW_FL)


def is_subvol(path: str) -> bool:
    """A btrfs subvolume root: inode 256 on btrfs (lstat: a symlink is not one)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_ino != 256:
            return False
        return _fs_magic(path) == BTRFS_MAGIC
    except OSError:
        return False


def _fs_magic(path: str) -> int:
    # statfs f_type is not in os.statvfs: ask stat(1).
    r = subprocess.run(["stat", "-f", "-c", "%t", path], check=False, capture_output=True, text=True)
    try:
        return int(r.stdout.strip(), 16)
    except ValueError:
        return 0


def subvol_create(path: str) -> None:
    """Create PATH as a subvolume, as the calling (unprivileged) user: btrfs
    allows that wherever the user may create a folder."""
    parent, name = os.path.split(path)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # struct btrfs_ioctl_vol_args is 4096 bytes: a mutable buffer (an
        # immutable argument is limited to 1024 bytes by fcntl.ioctl).
        fcntl.ioctl(fd, SUBVOL_CREATE, bytearray(struct.pack("q4088s", 0, name.encode())))
    finally:
        os.close(fd)
    os.chmod(path, 0o755)


def mounts() -> list[str]:
    """Mount points of this namespace."""
    out: list[str] = []
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                p = line.split()[4]
                out.append(p.replace("\\040", " "))
    except OSError:
        pass
    return out


def busy_paths(paths: Sequence[str], home: str) -> set[str]:
    """The subset of PATHS some process uses (open file, cwd, root, binary,
    mapped file, or a container's bind mount of the path, of something below
    it, or of a folder between it and HOME, e.g. -v ~/.m2:/root/.m2), from
    btrfs-snapshot's own detection. If it cannot be asked, all are busy."""
    if not paths:
        return set()
    try:
        r = subprocess.run(
            ["bash", snapshot_program(), "busy", "--home", home, *paths],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  cannot check open files ({e}): treated as busy")
        return set(paths)
    if r.returncode != 0:
        log(f"  cannot check open files ({r.stderr.strip()[:200]}): treated as busy")
        return set(paths)
    return {line for line in r.stdout.splitlines() if line}


def du_bytes(path: str) -> int:
    r = subprocess.run(["du", "-sxb", "--", path], check=False, capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0])
    except IndexError, ValueError:
        return 0


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def flatpak_installed(app: str) -> bool:
    r = subprocess.run(["flatpak", "info", app], check=False, capture_output=True, text=True)
    return r.returncode == 0


def flatpak_has_fs(app: str, path: str) -> bool:
    r = subprocess.run(["flatpak", "override", "--user", "--show", app], check=False, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("filesystems="):
            return path in line.split("=", 1)[1].split(";")
    return False


def flatpak_add_fs(app: str, path: str) -> None:
    r = subprocess.run(
        ["flatpak", "override", "--user", f"--filesystem={path}", app], check=False, capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"flatpak override failed: {r.stderr.strip()[:200]}")


def copy_tree(src: str, dst: str, nocow: bool) -> None:
    """cp src/. into the empty folder dst. NOCOW: a real copy (+C applies
    only to empty files: every file is created inside dst, which has +C, and
    inherits it before data is written). Otherwise reflink clones where
    possible (same filesystem: no data written)."""
    reflink = "--reflink=never" if nocow else "--reflink=auto"
    r = subprocess.run(
        ["cp", "-a", reflink, "--sparse=auto", "--", src + "/.", dst + "/"], check=False, capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"copy failed: {r.stderr.strip()[:300]}")


# ---- running as the user --------------------------------------------------------------
class Ctx:
    def __init__(self, home: str, pw: pwd.struct_passwd | None = None) -> None:
        self.home = os.path.realpath(home)
        self.bulk = os.path.join(self.home, "Bulk")
        self.pw = pw  # None: already the user (tests, non-root runs)

    def target(self, item: Item) -> str:
        return os.path.join(self.bulk, item.target)

    def link(self, item: Item) -> str | None:
        return os.path.join(self.home, item.link) if item.link else None

    def short(self, path: str) -> str:
        return "~" + path[len(self.home) :] if path.startswith(self.home + "/") else path


def as_user[R](ctx: Ctx, fn: Callable[..., R], *args: object) -> R:
    """fn(*args) in a child process with the user's uid, gids and HOME.
    Returns its JSON-able result; an exception there is raised here again."""
    if ctx.pw is None:
        return fn(*args)
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            pw = ctx.pw
            os.setgroups(os.getgrouplist(pw.pw_name, pw.pw_gid))
            os.setresgid(pw.pw_gid, pw.pw_gid, pw.pw_gid)
            os.setresuid(pw.pw_uid, pw.pw_uid, pw.pw_uid)
            os.umask(0o022)
            os.chdir(ctx.home)
            for k in [k for k in os.environ if k.startswith("XDG_")]:
                del os.environ[k]
            os.environ.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
            out = {"ok": fn(*args)}
        except Unexpected as e:
            out = {"unexpected": str(e)}
        except BaseException as e:  # noqa: BLE001 - reported to the parent
            out = {"error": f"{type(e).__name__}: {e}"}
            code = 1
        try:
            os.write(w, json.dumps(out).encode())
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    try:
        out = json.loads(data or b"{}")
    except ValueError:
        out = {}
    if "unexpected" in out:
        raise Unexpected(out["unexpected"])
    if "ok" not in out:
        raise RuntimeError(out.get("error", "child process died"))
    return cast("R", out["ok"])


# ---- inspection (read-only; fine as root: lstat never follows a link) -------------------
def kind(path: str) -> str:
    """absent | link | dir | other (lstat)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        return "link"
    return "dir" if stat.S_ISDIR(st.st_mode) else "other"


def chain(base: str, rel: str) -> str:
    """Every component of base/rel: ok (real folders), absent (one is
    missing), or 'symlink:<path>' / 'file:<path>'."""
    path = base
    for part in [p for p in rel.split("/") if p]:
        path = os.path.join(path, part)
        k = kind(path)
        if k == "absent":
            return "absent"
        if k == "link":
            return "symlink:" + path
        if k == "other":
            return "file:" + path
    return "ok"


def empty_dir(path: str) -> bool:
    with os.scandir(path) as it:
        return next(it, None) is None


def link_target(path: str) -> str:
    """Where the symlink PATH points, made absolute and normalized (no lookup)."""
    t = os.readlink(path)
    return os.path.normpath(t if os.path.isabs(t) else os.path.join(os.path.dirname(path), t))


def has_mount(path: str) -> bool:
    return any(m == path or m.startswith(path + "/") for m in mounts())


def cow_data(top: str) -> tuple[int, bool]:
    """Regular files with data and without +C below top, and whether top or a
    folder below it lacks +C (new files there would be copy-on-write)."""
    files, dirs_cow = 0, not is_nocow(top)
    dev = os.lstat(top).st_dev
    for root, dirs, names in os.walk(top):
        keep = []
        for d in dirs:
            st = os.lstat(os.path.join(root, d))
            if stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
                keep.append(d)
                if not is_nocow(os.path.join(root, d)):
                    dirs_cow = True
        dirs[:] = keep
        for n in names:
            p = os.path.join(root, n)
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode) and st.st_size and not is_nocow(p):
                files += 1
    return files, dirs_cow


def tree_stats(top: str, uid: int | None = None) -> tuple[int, int, bool, int]:
    """(entries, bytes of regular files, foreign, others): foreign is True if
    a folder below is another subvolume or filesystem (cp would copy it);
    others counts entries not owned by uid (None: not counted)."""
    entries = size = others = 0
    foreign = False
    dev = os.lstat(top).st_dev
    for root, dirs, names in os.walk(top):
        entries += len(dirs) + len(names)
        keep = []
        for d in dirs:
            st = os.lstat(os.path.join(root, d))
            if uid is not None and st.st_uid != uid:
                others += 1
            if not stat.S_ISDIR(st.st_mode):
                continue
            if st.st_dev != dev or is_subvol(os.path.join(root, d)):
                foreign = True
                continue
            keep.append(d)
        dirs[:] = keep
        for n in names:
            st = os.lstat(os.path.join(root, n))
            if uid is not None and st.st_uid != uid:
                others += 1
            if stat.S_ISREG(st.st_mode):
                size += st.st_size
    return entries, size, foreign, others


def changed_since(top: str, stamp: int, mindepth: int = 0) -> bool:
    """True if anything at/below top has a ctime >= stamp (ns). ctime cannot
    be set back by programs; >= also counts a change in the same clock tick."""
    if mindepth == 0 and os.lstat(top).st_ctime_ns >= stamp:
        return True
    for root, dirs, names in os.walk(top):
        for n in dirs + names:
            try:
                if os.lstat(os.path.join(root, n)).st_ctime_ns >= stamp:
                    return True
            except FileNotFoundError:
                return True
    return False


def target_state(ctx: Ctx, item: Item) -> str:
    """absent | empty | data | bad:<reason> for ~/Bulk/<target>."""
    c = chain(ctx.bulk, item.target)
    if c.startswith(("symlink:", "file:")):
        what, p = c.split(":", 1)
        return f"bad:{ctx.short(p)} is a {'symlink' if what == 'symlink' else 'file'}"
    if c == "absent":
        return "absent"
    return "empty" if empty_dir(ctx.target(item)) else "data"


def bulk_state(ctx: Ctx) -> str:
    k = kind(ctx.bulk)
    if k == "absent":
        return "create"
    if k != "dir":
        return "bad"
    return "ok" if is_subvol(ctx.bulk) else "plain"


def tag_state(ctx: Ctx) -> str:
    p = os.path.join(ctx.bulk, "CACHEDIR.TAG")
    k = kind(p)
    if k == "absent":
        return "create"
    if k != "other" or not stat.S_ISREG(os.lstat(p).st_mode):
        return "bad"
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        head = os.read(fd, len(TAG_SIGNATURE))
    finally:
        os.close(fd)
    return "ok" if head == TAG_SIGNATURE.encode() else "bad"


# ---- changes (run as the user) -----------------------------------------------------------
def u_mkdirs(bulk: str, rel: str, nocow: bool) -> str:
    """Create bulk/rel folder by folder; a symlink or file on the way stops it.
    nocow: every folder from bulk/NOCOW_TOP down to bulk/rel gets +C (also
    one that exists already: it affects only files created later)."""
    path = bulk
    for n, part in enumerate([p for p in rel.split("/") if p]):
        path = os.path.join(path, part)
        k = kind(path)
        if k == "absent":
            os.mkdir(path, 0o755)
        elif k != "dir":
            raise Unexpected(f"{path} is not a folder")
        if nocow and (n > 0 or part == NOCOW_TOP):
            set_nocow(path)
    return path


def nocow_parents(ctx: Ctx, item: Item) -> list[str]:
    """The folders between ~/Bulk/NOCOW_TOP (included) and the item's target
    (excluded) that exist without +C."""
    out: list[str] = []
    path = ctx.bulk
    for part in os.path.dirname(item.target).split("/"):
        path = os.path.join(path, part)
        if kind(path) != "dir":
            break  # absent, or a symlink/file: nothing real below it
        if not is_nocow(path):
            out.append(path)
    return out


def u_rmtree(path: str) -> None:
    """Delete a tree we own; read-only folders (Go's module cache) are made
    writable first. Never follows symlinks (shutil.rmtree works on fds)."""

    def fix(func: Callable[..., object], p: str, exc: BaseException) -> None:
        if isinstance(exc, PermissionError):
            parent = os.path.dirname(p)
            os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | stat.S_IRWXU)
                shutil.rmtree(p, onexc=fix)
            else:
                func(p)
        elif not isinstance(exc, FileNotFoundError):
            raise exc

    if kind(path) == "dir":
        shutil.rmtree(path, onexc=fix)
    elif kind(path) != "absent":
        os.unlink(path)


def u_create_bulk(bulk: str) -> bool:
    subvol_create(bulk)
    return True


def u_relabel(path: str) -> bool:
    """A new subvolume root has no SELinux label (unlabeled_t), and files
    created in it would inherit that: give it the policy's label (as the
    user; ~/Bulk only, not recursive). Best effort: no SELinux, no error."""
    try:
        subprocess.run(["restorecon", path], check=False, capture_output=True)
    except OSError:
        pass
    return True


def u_write_tag(bulk: str) -> bool:
    p = os.path.join(bulk, "CACHEDIR.TAG")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(TAG_TEXT)
    return True


def u_symlink(L: str, T: str) -> bool:
    if kind(L) != "absent":
        raise Unexpected(f"{L} appeared meanwhile")
    os.symlink(T, L)
    return True


def u_prepare(src: str, new: str, nocow: bool) -> int:
    """Copy src into new (a fresh folder next to the target) and check it.
    Returns the ctime stamp (ns) taken before the copy."""
    if kind(new) != "absent":
        u_rmtree(new)
    # Timestamps advance in clock ticks (a few ms): wait one out, so nothing
    # done before the stamp (by us, e.g. a recovery rename) shares its tick,
    # and ">=" below then means "changed after the copy started".
    time.sleep(0.05)
    os.mkdir(new, 0o700)
    if nocow:
        set_nocow(new)
    stamp = os.lstat(new).st_ctime_ns
    e_src, b_src, foreign, others = tree_stats(src, os.getuid())
    if foreign or others:
        u_rmtree(new)
    if foreign:
        raise Unexpected("contains another subvolume or a mount")
    if others:
        # cp would make them yours, but the old tree could not be deleted
        # afterwards (a folder owned by root, e.g. from a container's -v).
        me = pwd.getpwuid(os.getuid()).pw_name
        raise Unexpected(
            f"{others} entries there are not yours (made by a container or sudo?): "
            f"'sudo chown -R {me}: {src}', then run bulk apply again"
        )
    try:
        copy_tree(src, new, nocow)
        e_new, b_new, _, _ = tree_stats(new)
        if (e_new, b_new) != (e_src, b_src):
            raise RuntimeError(f"copy check failed (entries {e_src} vs {e_new}, bytes {b_src} vs {b_new})")
        if nocow:
            files, dirs_cow = cow_data(new)
            if files or dirs_cow:
                raise RuntimeError(f"copy is not NOCOW ({files} copy-on-write file(s))")
    except BaseException:
        u_rmtree(new)
        raise
    return stamp


def u_clock(near: str) -> int:
    """A ctime stamp (ns) of the filesystem's clock, taken one clock tick
    after anything done before (see u_prepare)."""
    time.sleep(0.05)
    probe = near + ".bulk-stamp"
    if kind(probe) != "absent":
        os.unlink(probe)  # left by a kill
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        return os.fstat(fd).st_ctime_ns
    finally:
        os.close(fd)
        os.unlink(probe)


def u_commit(j: Mapping[str, Any]) -> bool:
    """The switch: src -> old, new -> target, link. j["stamp2"] (taken just
    before, and in the journal already) is for u_new_written. Returns False
    (nothing changed) if src was written to since the copy started."""
    src, T, new, old, L = j["src"], j["target"], j["new"], j["old"], j["link"]
    if changed_since(src, j["stamp"]):
        u_rmtree(new)
        return False
    os.rename(src, old)
    try:
        os.rename(new, T)  # T: absent or an empty folder (replaced atomically)
    except OSError:
        os.rename(old, src)
        raise
    if L:
        try:
            os.symlink(T, L)
        except OSError:
            os.rename(T, new)
            os.rename(old, src)
            raise
    return True


def u_late_write(j: Mapping[str, Any]) -> bool:
    """After the switch: was the old tree written to meanwhile? Entries below
    it, or its own list of entries (mtime: an entry added or deleted directly
    in it; the rename to the old name changes only its ctime). Not covered: a
    process that resolved a path before the rename and opens the file after
    the busy check and this walk (microseconds)."""
    old = j["old"]
    return os.lstat(old).st_mtime_ns >= j["stamp"] or changed_since(old, j["stamp"], mindepth=1)


def u_new_written(j: Mapping[str, Any]) -> bool:
    """After the switch: was the target (the copy, now in use through the
    link) written to? Entries below it, or its own list of entries (mtime;
    the rename onto the target changes only its ctime)."""
    T, stamp2 = j["target"], j["stamp2"]
    if kind(T) != "dir":
        return False
    return os.lstat(T).st_mtime_ns >= stamp2 or changed_since(T, stamp2, mindepth=1)


def u_rollback(j: Mapping[str, Any]) -> bool:
    """Undo a switch, or the part of one an interruption left. Also the
    in-place NOCOW rewrite (src is the target: no link). If the old path
    was created again meanwhile (by an app, while the data waited in old),
    nothing is deleted: Unexpected, and the journal stays."""
    src, T, new, old, L = j["src"], j["target"], j["new"], j["old"], j["link"]
    if L and kind(L) == "link" and link_target(L) == T:
        os.unlink(L)
    if kind(old) == "dir":
        if src == T:
            # In place: the target holds the copy (if the switch got that far).
            if kind(T) == "dir" and kind(new) == "absent":
                os.rename(T, new)  # the copy goes back out of the way
        elif kind(src) == "absent" and kind(new) == "absent" and kind(T) == "dir":
            os.rename(T, new)
        if kind(src) != "absent":
            raise Unexpected(
                f"{src} was created again while the data waited in {old}: move what you need from "
                f"{src} into {old}, delete {src}, then run bulk apply again (it puts the data back)"
            )
        os.rename(old, src)
    if kind(new) != "absent":
        # The copy may have been written to through the link (a kill after
        # the undo was decided while the user kept working, or a tool that
        # removed the link): then it is kept, not deleted.
        if (
            "stamp2" in j
            and kind(new) == "dir"
            and (os.lstat(new).st_mtime_ns >= j["stamp2"] or changed_since(new, j["stamp2"], mindepth=1))
        ):
            raise Unexpected(
                f"the data is back at {src}, but the copy {new} was written to as well: copy what you "
                f"need from it into {src}, delete {new}, then run bulk apply again"
            )
        u_rmtree(new)
    return True


def u_finish(j: Mapping[str, Any]) -> bool:
    """Delete the old tree of a completed switch."""
    if kind(j["old"]) != "absent":
        u_rmtree(j["old"])
    if kind(j["new"]) != "absent":
        u_rmtree(j["new"])
    return True


def u_move_subvol(L: str, T: str) -> bool:
    """An old subvolume (from an earlier snapshots setup) is renamed into
    ~/Bulk: instant, same data; it stays a nested subvolume there."""
    if kind(T) == "dir":
        os.rmdir(T)  # the empty placeholder
    os.rename(L, T)
    os.symlink(T, L)
    return True


def u_flatpak(app: str, path: str) -> str:
    if not flatpak_installed(app):
        return "absent"
    if flatpak_has_fs(app, path):
        return "ok"
    flatpak_add_fs(app, path)
    return "added"


def u_flatpak_state(app: str, path: str) -> str:
    if not flatpak_installed(app):
        return "absent"
    return "ok" if flatpak_has_fs(app, path) else "missing"


# ---- journal (root-owned; the user cannot fake it) ----------------------------------------
def jpath(ident: str) -> str:
    return os.path.join(STATE, f"journal-{ident}.json")


def jwrite(ident: str, j: Mapping[str, Any]) -> None:
    os.makedirs(STATE, mode=0o755, exist_ok=True)
    tmp = jpath(ident) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(j, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, jpath(ident))
    # The rename itself is durable before anything it protects is done.
    fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def jread(ident: str) -> dict[str, Any] | None:
    try:
        with open(jpath(ident)) as f:
            return json.load(f)
    except OSError, ValueError:
        return None


def jdrop(ident: str) -> None:
    try:
        os.remove(jpath(ident))
    except FileNotFoundError:
        pass


def journals() -> list[str]:
    """Ids with a journal (a move not finished or undone yet); a Steam game's
    is "steam-<appid>"."""
    return [i.id for i in ITEMS if os.path.exists(jpath(i.id))] + steam_journal_ids()


def u_set_nocow_dirs(top: str) -> bool:
    set_nocow(top)
    dev = os.lstat(top).st_dev
    for root, dirs, _ in os.walk(top):
        dirs[:] = [
            d
            for d in dirs
            if os.lstat(os.path.join(root, d)).st_dev == dev and stat.S_ISDIR(os.lstat(os.path.join(root, d)).st_mode)
        ]
        for d in dirs:
            set_nocow(os.path.join(root, d))
    return True


def read_flag() -> list[str]:
    try:
        with open(FLAG) as f:
            return [x.strip() for x in f if x.strip()]
    except OSError:
        return []


def flag_add(ident: str) -> None:
    ids = read_flag()
    if ident not in ids:
        write_flag(ids + [ident])


def write_flag(ids: Sequence[str]) -> None:
    if ids:
        os.makedirs(ETC, mode=0o755, exist_ok=True)
        tmp = FLAG + ".tmp"
        with open(tmp, "w") as f:
            f.write("".join(i + "\n" for i in ids))
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, FLAG)
    else:
        try:
            os.remove(FLAG)
        except FileNotFoundError:
            pass


def read_keep() -> tuple[set[str], list[str]]:
    """(ids kept in /home by choice, unknown ids) from KEEP: one id per line,
    '#' starts a comment. Bytes that are not UTF-8 make an unknown id.
    Raises OSError if KEEP exists but cannot be read (a folder, say): then
    nobody knows what is meant to stay, and apply moves nothing."""
    known: set[str] = set()
    unknown: list[str] = []
    try:
        with open(KEEP, encoding="utf-8", errors="replace") as f:
            for line in f:
                ident = line.split("#", 1)[0].strip()
                if ident in BY_ID:
                    known.add(ident)
                elif ident:
                    unknown.append(ident)
    except FileNotFoundError:
        pass
    return known, unknown


def edit_keep(add: Sequence[str], remove: Collection[str]) -> None:
    """Add/remove ids in KEEP; other lines (comments) stay as they are."""
    # surrogateescape: lines this tool cannot read are written back unchanged.
    try:
        with open(KEEP, encoding="utf-8", errors="surrogateescape") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = [KEEP_HEADER]
    lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
    lines = [ln for ln in lines if ln.split("#", 1)[0].strip() not in remove]
    have = {ln.split("#", 1)[0].strip() for ln in lines}
    lines += [i + "\n" for i in add if i not in have]
    os.makedirs(ETC, mode=0o755, exist_ok=True)
    tmp = KEEP + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.rename(tmp, KEEP)


# ---- Steam: journal (journal-steam-<appid>.json) and the move ----
def steam_journal_ids() -> list[str]:
    try:
        names = os.listdir(STATE)
    except OSError:
        return []
    ids = [m.group(1) for m in (re.fullmatch(r"journal-(steam-[0-9]+)\.json", n) for n in names) if m]
    return sorted(ids, key=lambda i: int(i[6:]))
