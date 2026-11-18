# SPDX-License-Identifier: MPL-2.0
"""btrfs-bulk's looks at the machine: the btrfs and filesystem primitives
(Primitives: subvolumes, +C, mounts, free space, copies, flatpak overrides;
tests replace these) and the read-only inspection of the home built on them
(HomeTree; fine as root: lstat never follows a link).
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time

from btrfs.bulk.domain import (
    BTRFS_MAGIC,
    PROGRESS_BYTES,
    PROGRESS_EVERY,
    PROGRESS_POLL,
    SUBVOL_CREATE,
    TAG_SIGNATURE,
    Ctx,
    Item,
    TreeSize,
    Unexpected,
    gib,
    hms,
)
from cst_core.adapters.nocow import FS_NOCOW_FL, inode_flags, set_nocow


def log(msg: str) -> None:
    print(msg, flush=True)


def unescape_mountinfo(field: str) -> str:
    """A path field of /proc/self/mountinfo as the path it is: the kernel writes a space, a tab,
    a line break and a backslash as \\ and three octal digits (\\040 \\011 \\012 \\134). One
    pass, so an escaped backslash before "040" stays a backslash and the text 040."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


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


def kind(path: str) -> str:
    """absent | link | dir | other (lstat)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        return "link"
    return "dir" if stat.S_ISDIR(st.st_mode) else "other"


class Primitives:
    """The primitives that need btrfs, root or the host (the tests' fakes
    replace these methods; everything built on them stays real)."""

    def is_nocow(self, path: str) -> bool:
        return bool(inode_flags(path) & FS_NOCOW_FL)

    def set_nocow(self, path: str) -> None:
        set_nocow(path)

    def is_subvol(self, path: str) -> bool:
        """A btrfs subvolume root: inode 256 on btrfs (lstat: a symlink is not one)."""
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_ino != 256:
                return False
            return self.fs_magic(path) == BTRFS_MAGIC
        except OSError:
            return False

    def fs_magic(self, path: str) -> int:
        # statfs f_type is not in os.statvfs: ask stat(1).
        r = subprocess.run(["stat", "-f", "-c", "%t", path], check=False, capture_output=True, text=True)
        try:
            return int(r.stdout.strip(), 16)
        except ValueError:
            return 0

    def subvol_create(self, path: str) -> None:
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

    def mounts(self) -> list[str]:
        """Mount points of this namespace."""
        out: list[str] = []
        try:
            with open("/proc/self/mountinfo") as f:
                for line in f:
                    p = line.split()[4]
                    out.append(unescape_mountinfo(p))
        except OSError:
            pass
        return out

    def free_bytes(self, path: str) -> int:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize

    def du_bytes(self, path: str) -> TreeSize:
        """du -sxb: the apparent size of the tree at path (this filesystem only).
        A part du cannot read is left out of its sum, and it exits 1: the size
        is then incomplete, a lower bound (also when du printed no sum at all)."""
        r = subprocess.run(["du", "-sxb", "--", path], check=False, capture_output=True, text=True)
        try:
            return TreeSize(int(r.stdout.split()[0]), r.returncode == 0)
        except IndexError, ValueError:
            return TreeSize(0, False)

    def flatpak_installed(self, app: str) -> bool:
        r = subprocess.run(["flatpak", "info", app], check=False, capture_output=True, text=True)
        return r.returncode == 0

    def flatpak_has_fs(self, app: str, path: str) -> bool:
        r = subprocess.run(
            ["flatpak", "override", "--user", "--show", app], check=False, capture_output=True, text=True
        )
        for line in r.stdout.splitlines():
            if line.startswith("filesystems="):
                return path in line.split("=", 1)[1].split(";")
        return False

    def flatpak_add_fs(self, app: str, path: str) -> None:
        r = subprocess.run(
            ["flatpak", "override", "--user", f"--filesystem={path}", app], check=False, capture_output=True, text=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"flatpak override failed: {r.stderr.strip()[:200]}")

    def copy_tree(self, src: str, dst: str, nocow: bool) -> None:
        """cp src/. into the empty folder dst. NOCOW: a real copy (+C applies
        only to empty files: every file is created inside dst, which has +C, and
        inherits it before data is written). Otherwise reflink clones where
        possible (same filesystem: no data written)."""
        reflink = "--reflink=never" if nocow else "--reflink=auto"
        r = subprocess.run(
            ["cp", "-a", reflink, "--sparse=auto", "--", src + "/.", dst + "/"],
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"copy failed: {r.stderr.strip()[:300]}")

    def copy_reflink(self, src: str, dst: str) -> None:
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

    def copy_bytes(self, src: str, dst: str, label: str, total: int) -> None:
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


def stat_files(root: str, names: list[str], uid: int | None) -> tuple[int, int]:
    """tree_stats for the files of one level: (how many are not uid's, bytes of the regular ones)."""
    others = size = 0
    for n in names:
        st = os.lstat(os.path.join(root, n))
        if uid is not None and st.st_uid != uid:
            others += 1
        if stat.S_ISREG(st.st_mode):
            size += st.st_size
    return others, size


class HomeTree:
    """HomeTreePort over the Primitives: lstat-only looks at the home."""

    def __init__(self, machine: Primitives) -> None:
        self.machine = machine

    def kind(self, path: str) -> str:
        return kind(path)

    def entries_nofollow(self, path: str) -> list[str]:
        """The names in the folder path; [] when it is absent or not a folder.
        A symbolic link at path is not followed (root reads paths under the
        user's ~/Bulk): Unexpected."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        except OSError as e:
            if e.errno not in (errno.ENOTDIR, errno.ELOOP):
                raise
            if kind(path) == "link":
                raise Unexpected(f"{path} is a symbolic link (not followed)") from None
            return []
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)

    def chain(self, base: str, rel: str) -> str:
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

    def empty_dir(self, path: str) -> bool:
        with os.scandir(path) as it:
            return next(it, None) is None

    def link_target(self, path: str) -> str:
        """Where the symlink PATH points, made absolute and normalized (no lookup)."""
        t = os.readlink(path)
        return os.path.normpath(t if os.path.isabs(t) else os.path.join(os.path.dirname(path), t))

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def has_mount(self, path: str) -> bool:
        return any(m == path or m.startswith(path + "/") for m in self.machine.mounts())

    def owner(self, path: str) -> int:
        return os.lstat(path).st_uid

    def size(self, path: str) -> int:
        return os.lstat(path).st_size

    def is_regular(self, path: str) -> bool:
        return stat.S_ISREG(os.lstat(path).st_mode)

    def my_uid(self) -> int:
        return os.getuid()

    def is_nocow(self, path: str) -> bool:
        return self.machine.is_nocow(path)

    def cow_data(self, top: str) -> tuple[int, bool]:
        """Regular files with data and without +C below top, and whether top or a
        folder below it lacks +C (new files there would be copy-on-write)."""
        is_nocow = self.machine.is_nocow
        files, dirs_cow = 0, not is_nocow(top)
        dev = os.lstat(top).st_dev
        for root, dirs, names in os.walk(top):
            keep, cow = self.same_dev_dirs_cow(root, dirs, dev)
            dirs[:] = keep
            dirs_cow = dirs_cow or cow
            for n in names:
                p = os.path.join(root, n)
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode) and st.st_size and not is_nocow(p):
                    files += 1
        return files, dirs_cow

    def same_dev_dirs_cow(self, root: str, dirs: list[str], dev: int) -> tuple[list[str], bool]:
        """The folders of one level on dev (to walk into), and whether one of them lacks +C."""
        keep: list[str] = []
        cow = False
        for d in dirs:
            st = os.lstat(os.path.join(root, d))
            if stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
                keep.append(d)
                if not self.machine.is_nocow(os.path.join(root, d)):
                    cow = True
        return keep, cow

    def is_subvol(self, path: str) -> bool:
        return self.machine.is_subvol(path)

    def tree_stats(self, top: str, uid: int | None = None) -> tuple[int, int, bool, int]:
        """(entries, bytes of regular files, foreign, others): foreign is True if
        a folder below is another subvolume or filesystem (cp would copy it);
        others counts entries not owned by uid (None: not counted)."""
        entries = size = others = 0
        foreign = False
        dev = os.lstat(top).st_dev
        for root, dirs, names in os.walk(top):
            entries += len(dirs) + len(names)
            keep, dir_others, crossed = self.stat_dirs(root, dirs, dev, uid)
            dirs[:] = keep
            file_others, file_bytes = stat_files(root, names, uid)
            others += dir_others + file_others
            size += file_bytes
            foreign = foreign or crossed
        return entries, size, foreign, others

    def stat_dirs(self, root: str, dirs: list[str], dev: int, uid: int | None) -> tuple[list[str], int, bool]:
        """tree_stats for the folders of one level: (those to walk into, how
        many are not uid's, whether one is another subvolume or filesystem)."""
        keep: list[str] = []
        others = 0
        foreign = False
        for d in dirs:
            st = os.lstat(os.path.join(root, d))
            if uid is not None and st.st_uid != uid:
                others += 1
            if not stat.S_ISDIR(st.st_mode):
                continue
            if st.st_dev != dev or self.machine.is_subvol(os.path.join(root, d)):
                foreign = True
                continue
            keep.append(d)
        return keep, others, foreign

    def changed_since(self, top: str, stamp: int, mindepth: int = 0) -> bool:
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

    def du_bytes(self, path: str) -> TreeSize:
        return self.machine.du_bytes(path)

    def target_state(self, ctx: Ctx, item: Item) -> str:
        """absent | empty | data | bad:<reason> for ~/Bulk/<target>."""
        c = self.chain(ctx.bulk, item.target)
        if c.startswith(("symlink:", "file:")):
            what, p = c.split(":", 1)
            return f"bad:{ctx.short(p)} is a {'symlink' if what == 'symlink' else 'file'}"
        if c == "absent":
            return "absent"
        return "empty" if self.empty_dir(ctx.target(item)) else "data"

    def bulk_state(self, ctx: Ctx) -> str:
        k = kind(ctx.bulk)
        if k == "absent":
            return "create"
        if k != "dir":
            return "bad"
        return "ok" if self.machine.is_subvol(ctx.bulk) else "plain"

    def tag_state(self, ctx: Ctx) -> str:
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

    def free_bytes(self, path: str) -> int:
        return self.machine.free_bytes(path)

    def nocow_parents(self, ctx: Ctx, item: Item) -> list[str]:
        """The folders between ~/Bulk/NOCOW_TOP (included) and the item's target
        (excluded) that exist without +C."""
        out: list[str] = []
        path = ctx.bulk
        for part in os.path.dirname(item.target).split("/"):
            path = os.path.join(path, part)
            if kind(path) != "dir":
                break  # absent, or a symlink/file: nothing real below it
            if not self.machine.is_nocow(path):
                out.append(path)
        return out

    def steam_manifests(self, folder: str) -> set[str]:
        try:
            names = os.listdir(os.path.join(folder, "steamapps"))
        except OSError:
            return set()
        return {m.group(1) for m in (re.fullmatch(r"appmanifest_(\d+)\.acf", n, re.ASCII) for n in names) if m}

    def newest_backup(self, path: str) -> str | None:
        d, base = os.path.split(path)
        try:
            baks = sorted(n for n in os.listdir(d) if n.startswith(base + ".cst-bak-"))
        except OSError:
            baks = []
        return os.path.join(d, baks[-1]) if baks else None

    def steam_nocow_scan(self, top: str) -> tuple[int, str, str]:
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
        st = os.lstat(top)
        if not stat.S_ISDIR(st.st_mode):
            return self.nocow_scan_file(top, st)
        nocow, first, sample = 0, "", ""
        dev = st.st_dev
        for root, dirs, names in os.walk(top):
            dirs[:] = [d for d in dirs if os.lstat(os.path.join(root, d)).st_dev == dev]
            for n in names:
                p = os.path.join(root, n)
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if self.machine.is_nocow(p):
                    nocow += 1
                    first = first or p
                elif st.st_size and not sample:
                    sample = p
        return nocow, first, sample

    def nocow_scan_file(self, top: str, st: os.stat_result) -> tuple[int, str, str]:
        """steam_nocow_scan of a TOP that is not a folder."""
        if stat.S_ISREG(st.st_mode):
            if self.machine.is_nocow(top):
                return 1, top, ""
            return 0, "", (top if st.st_size else "")
        return 0, "", ""
