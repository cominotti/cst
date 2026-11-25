# SPDX-License-Identifier: MPL-2.0
"""The changes btrfs-bulk makes inside the home: each runs in a child
process that dropped to the user (as_user), so a symlink in the home can
never make root write somewhere else. UserFiles holds them: its u_*
methods are the steps themselves, its port methods run one in the child.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
import base64
import contextlib
import errno
import os
import pwd
import shutil
import stat
import time
from typing import TypedDict

from btrfs.bulk.adapters.child import (
    Account,
    as_user,
    is_bool,
    is_flatpak_access,
    is_int,
    is_list,
    is_none,
    is_str,
    is_user_read,
)
from btrfs.bulk.adapters.machine import HomeTree, Primitives, Progress, kind, listing
from btrfs.bulk.domain import DROP_MARK, NOCOW_TOP, TAG_TEXT, FlatpakAccess, ItemJournal, LstatKind, Unexpected
from btrfs.bulk.domain.steam import (
    CHUNK,
    STEAM_BACKUPS,
    STEAM_READ_MAX,
    FileRead,
    SteamJournal,
    SteamPart,
    SteamPartKind,
    SteamPartMode,
    vdf_dump,
)
from cst_core.domain.values import Json


def idle_priority(machine: Primitives) -> None:
    """This process, and what it starts, at the lowest CPU priority and the
    idle IO class (the IO class only counts with the BFQ IO scheduler); machine runs ionice."""
    with contextlib.suppress(OSError):
        os.nice(19)
    if shutil.which("ionice") is not None:
        machine.run(["ionice", "-c3", "-p", str(os.getpid())])


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


def fsync_dir(d: str) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PartsOnly(TypedDict):
    """What u_steam_drop_news needs of a journal: its parts."""

    parts: list[SteamPart]


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


def u_read(path: str) -> dict[str, Json] | None:
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


def back_up_pair(files: Sequence[str], olds: Sequence[FileRead], ts: str) -> None:
    """u_write_vdf_pair's backups: FILE.cst-bak-TS of each (once per run), the newest STEAM_BACKUPS kept."""
    for f, r in zip(files, olds, strict=False):
        bak = f"{f}.cst-bak-{ts}"
        if kind(bak) == LstatKind.ABSENT:
            u_write_new(bak, base64.b64decode(r["b64"]), r["mode"])
            fsync_dir(os.path.dirname(f))
        d, base = os.path.split(f)
        baks = sorted(n for n in os.listdir(d) if n.startswith(base + ".cst-bak-"))
        for n in baks[:-STEAM_BACKUPS]:
            os.unlink(os.path.join(d, n))


def json_int(value: Json) -> int:
    """An int that crossed as_user's JSON (bool is not one)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"not a number from the child process: {value!r}")
    return value


def json_str(value: Json) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"not a string from the child process: {value!r}")
    return value


def file_read(r: Mapping[str, Json]) -> FileRead:
    """u_read's answer with its bytes decoded."""
    b64 = json_str(r["b64"])
    return {
        "b64": b64,
        "data": base64.b64decode(b64),
        "mtime": json_int(r["mtime"]),
        "mode": json_int(r["mode"]),
        "uid": json_int(r["uid"]),
    }


class UserFiles:
    """UserFilesPort. Each step (u_*, the code that runs as the user) is
    followed by the port method that runs it through as_user (pw None: here,
    already the user). The steps use tree for their looks and its machine
    for the btrfs primitives."""

    def __init__(self, tree: HomeTree, pw: Account | None, home: str) -> None:
        self.tree, self.machine, self.pw, self.home = tree, tree.machine, pw, home

    def read(self, path: str) -> FileRead | None:
        r = as_user(self.pw, self.home, is_user_read, u_read, path)
        return None if r is None else file_read(r)

    def u_mkdirs(self, bulk: str, rel: str, nocow: bool) -> str:
        """Create bulk/rel folder by folder; a symlink or file on the way stops it.
        nocow: every folder from bulk/NOCOW_TOP down to bulk/rel gets +C (also
        one that exists already: it affects only files created later)."""
        path = bulk
        for n, part in enumerate([p for p in rel.split("/") if p]):
            path = os.path.join(path, part)
            k = kind(path)
            if k == LstatKind.ABSENT:
                os.mkdir(path, 0o755)
            elif k != LstatKind.DIR:
                raise Unexpected(f"{path} is not a folder")
            if nocow and (n > 0 or part == NOCOW_TOP):
                self.machine.set_nocow(path)
        return path

    def mkdirs(self, bulk: str, rel: str, *, nocow: bool) -> str:
        return as_user(self.pw, self.home, is_str, self.u_mkdirs, bulk, rel, nocow)

    def u_rmtree(self, path: str) -> None:
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

        if kind(path) == LstatKind.DIR:
            shutil.rmtree(path, onexc=fix)
        elif kind(path) != LstatKind.ABSENT:
            os.unlink(path)

    def rmtree(self, path: str) -> None:
        as_user(self.pw, self.home, is_none, self.u_rmtree, path)

    def rmdir(self, path: str) -> None:
        as_user(self.pw, self.home, is_none, os.rmdir, path)

    def u_create_bulk(self, bulk: str) -> bool:
        self.machine.subvol_create(bulk)
        return True

    def create_bulk(self, bulk: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_create_bulk, bulk)

    def u_relabel(self, path: str) -> bool:
        """A new subvolume root has no SELinux label (unlabeled_t), and files
        created in it would inherit that: give it the policy's label (as the
        user; ~/Bulk only, not recursive). Best effort: no SELinux, no error."""
        with contextlib.suppress(OSError):
            self.machine.run(["restorecon", path])
        return True

    def relabel(self, path: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_relabel, path)

    def u_write_tag(self, bulk: str) -> bool:
        p = os.path.join(bulk, "CACHEDIR.TAG")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(TAG_TEXT)
        return True

    def write_tag(self, bulk: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_write_tag, bulk)

    def u_symlink(self, link: str, target: str) -> bool:
        if kind(link) != LstatKind.ABSENT:
            raise Unexpected(f"{link} appeared meanwhile")
        os.symlink(target, link)
        return True

    def symlink(self, link: str, target: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_symlink, link, target)

    def u_prepare(self, src: str, new: str, nocow: bool) -> int:
        """Copy src into new (a fresh folder next to the target) and check it.
        Returns the ctime stamp (ns) taken before the copy."""
        if kind(new) != LstatKind.ABSENT:
            self.u_rmtree(new)
        self.drop_marked(new)
        # Timestamps advance in clock ticks (a few ms): wait one out, so nothing
        # done before the stamp (by us, e.g. a recovery rename) shares its tick,
        # and ">=" below then means "changed after the copy started".
        time.sleep(0.05)
        os.mkdir(new, 0o700)
        if nocow:
            self.machine.set_nocow(new)
        stamp = os.lstat(new).st_ctime_ns
        e_src, b_src, foreign, others = self.tree.tree_stats(src, os.getuid())
        if foreign or others:
            self.u_rmtree(new)
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
            self.machine.copy_tree(src, new, nocow)
            e_new, b_new, _, _ = self.tree.tree_stats(new)
            if (e_new, b_new) != (e_src, b_src):
                raise RuntimeError(f"copy check failed (entries {e_src} vs {e_new}, bytes {b_src} vs {b_new})")
            if nocow:
                files, dirs_cow = self.tree.cow_data(new)
                if files or dirs_cow:
                    raise RuntimeError(f"copy is not NOCOW ({files} copy-on-write file(s))")
        except BaseException:
            self.u_rmtree(new)
            raise
        return stamp

    def prepare(self, src: str, new: str, *, nocow: bool) -> int:
        return as_user(self.pw, self.home, is_int, self.u_prepare, src, new, nocow)

    def u_clock(self, near: str) -> int:
        """A ctime stamp (ns) of the filesystem's clock, taken one clock tick
        after anything done before (see u_prepare)."""
        time.sleep(0.05)
        probe = near + ".bulk-stamp"
        if kind(probe) != LstatKind.ABSENT:
            os.unlink(probe)  # left by a kill
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            return os.fstat(fd).st_ctime_ns
        finally:
            os.close(fd)
            os.unlink(probe)

    def clock(self, near: str) -> int:
        return as_user(self.pw, self.home, is_int, self.u_clock, near)

    def u_commit(self, j: ItemJournal) -> bool:
        """The switch: src -> old, new -> target, link. j["stamp2"] (taken just
        before, and in the journal already) is for u_new_written. Returns False
        (nothing changed) if src was written to since the copy started."""
        src, target, new, old, link = j["src"], j["target"], j["new"], j["old"], j["link"]
        if self.tree.changed_since(src, j["stamp"]):
            self.u_drop(new)  # after the journal with stamp2: a cut deletion must not read as a write
            return False
        os.rename(src, old)
        try:
            os.rename(new, target)  # target: absent or an empty folder (replaced atomically)
        except OSError:
            os.rename(old, src)
            raise
        if link is not None and link != "":
            try:
                os.symlink(target, link)
            except OSError:
                os.rename(target, new)
                os.rename(old, src)
                raise
        return True

    def commit(self, j: ItemJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_commit, j)

    def u_late_write(self, j: ItemJournal) -> bool:
        """After the switch: was the old tree written to meanwhile? Entries below
        it, or its own list of entries (mtime: an entry added or deleted directly
        in it; the rename to the old name changes only its ctime). Not covered: a
        process that resolved a path before the rename and opens the file after
        the busy check and this walk (microseconds)."""
        old = j["old"]
        return os.lstat(old).st_mtime_ns >= j["stamp"] or self.tree.changed_since(old, j["stamp"], mindepth=1)

    def late_write(self, j: ItemJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_late_write, j)

    def u_new_written(self, j: ItemJournal) -> bool:
        """After the switch: was the target (the copy, now in use through the
        link) written to? Entries below it, or its own list of entries (mtime;
        the rename onto the target changes only its ctime)."""
        target, stamp2 = j["target"], j["stamp2"]
        if kind(target) != LstatKind.DIR:
            return False
        return os.lstat(target).st_mtime_ns >= stamp2 or self.tree.changed_since(target, stamp2, mindepth=1)

    def new_written(self, j: ItemJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_new_written, j)

    def u_rollback(self, j: ItemJournal) -> bool:
        """Undo a switch, or the part of one an interruption left. Also the
        in-place NOCOW rewrite (src is the target: no link). If the old path
        was created again meanwhile (by an app, while the data waited in old),
        nothing is deleted: Unexpected, and the journal stays."""
        src, target, new, old, link = j["src"], j["target"], j["new"], j["old"], j["link"]
        self.drop_marked(new)
        if link is not None and link != "" and kind(link) == LstatKind.LINK and self.tree.link_target(link) == target:
            os.unlink(link)
        if kind(old) == LstatKind.DIR:
            self.put_old_back(src, target, new, old)
        if kind(new) != LstatKind.ABSENT:
            self.drop_copy(j, src, new)
        return True

    def put_old_back(self, src: str, target: str, new: str, old: str) -> None:
        """u_rollback while the data waits in old: the copy goes out of the way, old back to src."""
        if src == target:
            # In place: the target holds the copy (if the switch got that far).
            if kind(target) == LstatKind.DIR and kind(new) == LstatKind.ABSENT:
                os.rename(target, new)  # the copy goes back out of the way
        elif kind(src) == LstatKind.ABSENT and kind(new) == LstatKind.ABSENT and kind(target) == LstatKind.DIR:
            os.rename(target, new)
        if kind(src) != LstatKind.ABSENT:
            raise Unexpected(
                f"{src} was created again while the data waited in {old}: move what you need from "
                f"{src} into {old}, delete {src}, then run bulk apply again (it puts the data back)"
            )
        os.rename(old, src)

    def drop_copy(self, j: ItemJournal, src: str, new: str) -> None:
        """u_rollback's last step: the copy goes, unless it was written to."""
        # The copy may have been written to through the link (a kill after
        # the undo was decided while the user kept working, or a tool that
        # removed the link): then it is kept, not deleted.
        if (
            "stamp2" in j
            and kind(new) == LstatKind.DIR
            and (os.lstat(new).st_mtime_ns >= j["stamp2"] or self.tree.changed_since(new, j["stamp2"], mindepth=1))
        ):
            raise Unexpected(
                f"the data is back at {src}, but the copy {new} was written to as well: copy what you "
                f"need from it into {src}, delete {new}, then run bulk apply again"
            )
        self.u_drop(new)

    def u_drop(self, path: str) -> None:
        """Delete a checked copy: renamed to PATH + DROP_MARK first (on disk before the first
        deletion), so a deletion cut short (a kill, a power loss, a failed deletion) leaves a
        folder known by its name, which the next repair deletes (drop_marked), not a partly
        deleted copy whose times say it was written to."""
        marked = path + DROP_MARK
        self.drop_marked(path)  # a rename onto a folder that is not empty fails
        os.rename(path, marked)
        fsync_dir(os.path.dirname(marked))
        self.u_rmtree(marked)

    def drop_marked(self, new: str) -> None:
        """What is left of a copy u_drop was deleting: it passed its check, so it goes unchecked."""
        if new != "" and kind(new + DROP_MARK) != LstatKind.ABSENT:
            self.u_rmtree(new + DROP_MARK)

    def rollback(self, j: ItemJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_rollback, j)

    def u_finish(self, j: ItemJournal) -> bool:
        """Delete the old tree of a completed switch, and a copy still there (marked or not)."""
        if kind(j["old"]) != LstatKind.ABSENT:
            self.u_rmtree(j["old"])
        if kind(j["new"]) != LstatKind.ABSENT:
            self.u_rmtree(j["new"])
        self.drop_marked(j["new"])
        return True

    def finish(self, j: ItemJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_finish, j)

    def u_move_subvol(self, link: str, target: str) -> bool:
        """An old subvolume (from an earlier snapshots setup) is renamed into
        ~/Bulk: instant, same data; it stays a nested subvolume there."""
        if kind(target) == LstatKind.DIR:
            os.rmdir(target)  # the empty placeholder
        os.rename(link, target)
        os.symlink(target, link)
        return True

    def move_subvol(self, link: str, target: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_move_subvol, link, target)

    def u_flatpak(self, app: str, path: str) -> FlatpakAccess:
        if not self.machine.flatpak_installed(app):
            return FlatpakAccess.ABSENT
        if self.machine.flatpak_has_fs(app, path):
            return FlatpakAccess.OK
        self.machine.flatpak_add_fs(app, path)
        return FlatpakAccess.ADDED

    def flatpak(self, app: str, path: str) -> str:
        return as_user(self.pw, self.home, is_flatpak_access, self.u_flatpak, app, path)

    def u_flatpak_state(self, app: str, path: str) -> FlatpakAccess:
        if not self.machine.flatpak_installed(app):
            return FlatpakAccess.ABSENT
        return FlatpakAccess.OK if self.machine.flatpak_has_fs(app, path) else FlatpakAccess.MISSING

    def flatpak_state(self, app: str, path: str) -> str:
        return as_user(self.pw, self.home, is_flatpak_access, self.u_flatpak_state, app, path)

    def u_set_nocow_dirs(self, top: str) -> bool:
        self.machine.set_nocow(top)
        dev = os.lstat(top).st_dev
        for root, dirs, _ in os.walk(top):
            dirs[:] = [
                d
                for d in dirs
                if os.lstat(os.path.join(root, d)).st_dev == dev
                and stat.S_ISDIR(os.lstat(os.path.join(root, d)).st_mode)
            ]
            for d in dirs:
                self.machine.set_nocow(os.path.join(root, d))
        return True

    def set_nocow_dirs(self, top: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_set_nocow_dirs, top)

    def u_reflink_probe(self, src_file: str, library: str) -> bool:
        """Clone SRC_FILE (a copy-on-write file of a part that moves by reflink)
        into ~/Bulk/steam and delete the clone again."""
        probe = os.path.join(library, ".bulk-probe")
        if kind(probe) != LstatKind.ABSENT:
            self.u_rmtree(probe)
        try:
            self.machine.copy_reflink(src_file, probe)
        except RuntimeError as e:
            raise Unexpected(
                f"reflink of a copy-on-write file into ~/Bulk/steam failed (is ~/Bulk on another filesystem?): {e}"
            ) from None
        finally:
            if kind(probe) != LstatKind.ABSENT:
                self.u_rmtree(probe)
        return True

    def reflink_probe(self, src_file: str, library: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_reflink_probe, src_file, library)

    def u_verify_content(self, src: str, new: str, label: str, total: int) -> str:
        """Every regular file of NEW has the same bytes as in SRC (the tree
        itself, names, types, sizes, modes, links, was compared before). Returns
        the first differing path relative to SRC, or ''. The copy is written to
        the disk first and read back from there; if that write fails (sync or
        fsync reports an error), RuntimeError: the caller drops the copy and the
        original stays."""
        r = self.machine.run(["sync", "-f", new])
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

    def nocow_entries(self, top: str) -> int:
        """Entries (folders and regular files) with +C at/below TOP."""
        is_nocow = self.machine.is_nocow
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

    def u_steam_prepare(self, parts: Sequence[SteamPart], idle: bool = False) -> int:
        """Copy every part to its .bulk-new name and check it: a reflink clone,
        or (mode 'copy': NOCOW data) a real copy whose every byte is compared
        afterwards. Returns the ctime stamp (ns) taken before the copy. idle:
        this is a child process of its own (as_user): it lowers its own
        priority for the copy and the comparison."""
        for p in parts:
            self.drop_marked(p["new"])  # what a cut u_drop left, without its journal (as u_prepare)
            for x in (p["new"], p["dst"]):
                if kind(x) != LstatKind.ABSENT:
                    raise Unexpected(f"{x} exists already: remove it or move it back")
        if idle and any(p.get("mode") == SteamPartMode.COPY for p in parts):
            idle_priority(self.machine)
        stamp = self.u_clock(parts[-1]["new"])
        uid = os.getuid()
        try:
            for p in parts:
                self.prepare_part(p, uid)
        except BaseException:
            self.u_steam_drop_news({"parts": list(parts)})
            raise
        return stamp

    def prepare_part(self, p: SteamPart, uid: int) -> None:
        """One part of u_steam_prepare: its copy, then the checks of the copy."""
        if p["kind"] == SteamPartKind.DIR:
            _, _, foreign, others = self.tree.tree_stats(p["src"], uid)
            if foreign:
                raise Unexpected(f"{p['src']} contains another subvolume or a mount")
            if others:
                raise Unexpected(f"{others} entries in {p['src']} are not yours")
        label = f"{p['name']} ({os.path.basename(p['src'])})"
        if p.get("mode") == SteamPartMode.COPY:
            if self.machine.is_nocow(os.path.dirname(p["new"])):
                raise Unexpected(f"{os.path.dirname(p['new'])} has +C: the copy would be NOCOW too")
            self.machine.copy_bytes(p["src"], p["new"], label, p.get("bytes", 0))
        else:
            self.machine.copy_reflink(p["src"], p["new"])
        self.check_part_copy(p, label)

    def check_part_copy(self, p: SteamPart, label: str) -> None:
        """The copy of a part has the same tree; a real copy also no +C and the same bytes."""
        a, b = listing(p["src"]), listing(p["new"])
        if a != b:
            diff = next((x for x, y in zip(a, b, strict=False) if x != y), None) or f"{len(a)} vs {len(b)} entries"
            raise RuntimeError(f"copy check failed for {p['src']} (first difference: {diff})")
        if p.get("mode") == SteamPartMode.COPY:
            if n := self.nocow_entries(p["new"]):
                raise RuntimeError(f"the copy of {p['src']} has {n} NOCOW (+C) entries; it must be copy-on-write")
            if bad := self.u_verify_content(p["src"], p["new"], label, p.get("bytes", 0)):
                raise RuntimeError(f"copy check failed for {p['src']}: the content of {bad} differs")

    def steam_prepare(self, parts: Sequence[SteamPart]) -> int:
        # idle: only a child of its own (pw set) may lower its own priority.
        return as_user(self.pw, self.home, is_int, self.u_steam_prepare, parts, self.pw is not None)

    def u_steam_drop_news(self, j: SteamJournal | PartsOnly) -> bool:
        for p in j["parts"]:
            if kind(p["new"]) != LstatKind.ABSENT:
                self.u_rmtree(p["new"])
        return True

    def steam_drop_news(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_drop_news, j)

    def u_steam_changed(self, j: SteamJournal) -> bool:
        """Was a source written to since the copy started?"""
        return any(self.tree.changed_since(p["src"], j["stamp"]) for p in j["parts"])

    def steam_changed(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_changed, j)

    def u_steam_switch(self, j: SteamJournal) -> bool:
        """Per part, in order (the manifest last): src -> old, new -> dst."""
        for p in j["parts"]:
            if kind(p["dst"]) != LstatKind.ABSENT:
                raise Unexpected(f"{p['dst']} appeared meanwhile")
            os.rename(p["src"], p["old"])
            os.rename(p["new"], p["dst"])
        return True

    def steam_switch(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_switch, j)

    def written_since(self, path: str, stamp: int) -> bool:
        if kind(path) == LstatKind.ABSENT:
            return False
        if os.lstat(path).st_mtime_ns >= stamp:
            return True
        return kind(path) == LstatKind.DIR and self.tree.changed_since(path, stamp, mindepth=1)

    def u_steam_late(self, j: SteamJournal) -> bool:
        """After the switch: was an old copy written to (see u_late_write)?"""
        return any(self.written_since(p["old"], j["stamp"]) for p in j["parts"])

    def steam_late(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_late, j)

    def u_steam_new_written(self, j: SteamJournal) -> bool:
        """After the switch: was a moved part written to in the new library?"""
        return any(self.written_since(p["dst"], j["stamp2"]) for p in j["parts"])

    def steam_new_written(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_new_written, j)

    def u_steam_rollback(self, j: SteamJournal) -> bool:
        """Undo a switch, or what an interruption left of one: the manifest leaves the new library
        first, the game comes back first; a copy a cut u_drop left (marked) goes first, unchecked. If
        a source path was created again meanwhile, nothing is deleted (Unexpected)."""
        parts = j["parts"]
        for p in parts:
            self.drop_marked(p["new"])
            if kind(p["old"]) != LstatKind.ABSENT and kind(p["src"]) != LstatKind.ABSENT:
                raise Unexpected(
                    f"{p['src']} was created again while the data waited in {p['old']}: move what you "
                    f"need into {p['old']}, delete {p['src']}, then run 'bulk steam apply' again"
                )
        for p in reversed(parts):
            if kind(p["old"]) != LstatKind.ABSENT and kind(p["dst"]) != LstatKind.ABSENT:
                if kind(p["new"]) != LstatKind.ABSENT:
                    raise Unexpected(f"both {p['dst']} and {p['new']} exist")
                os.rename(p["dst"], p["new"])
        for p in parts:
            if kind(p["old"]) != LstatKind.ABSENT:
                os.rename(p["old"], p["src"])
        self.drop_steam_copies(j)
        return True

    def drop_steam_copies(self, j: SteamJournal) -> None:
        """u_steam_rollback's last step: the copies go (u_drop), unless one was written to."""
        for p in j["parts"]:
            if kind(p["new"]) != LstatKind.ABSENT:
                if "stamp2" in j and self.written_since(p["new"], j["stamp2"]):
                    raise Unexpected(
                        f"the data is back at {p['src']}, but the copy {p['new']} was written to as well: "
                        f"copy what you need from it, delete it, then run 'bulk steam apply' again"
                    )
                self.u_drop(p["new"])

    def steam_rollback(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_rollback, j)

    def u_steam_finish(self, j: SteamJournal) -> bool:
        for p in j["parts"]:  # the old copies, and a copy still there (marked or not)
            for x in (p["old"], p["new"], p["new"] + DROP_MARK):
                if kind(x) != LstatKind.ABSENT:
                    self.u_rmtree(x)
        return True

    def steam_finish(self, j: SteamJournal) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_steam_finish, j)

    def u_write_marker(self, path: str, contentid: str) -> bool:
        u_write_new(path, vdf_dump({"libraryfolder": {"contentid": contentid, "label": ""}}), 0o644)
        fsync_dir(os.path.dirname(path))
        return True

    def write_marker(self, path: str, contentid: str) -> bool:
        return as_user(self.pw, self.home, is_bool, self.u_write_marker, path, contentid)

    def u_write_vdf_pair(
        self,
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
            olds.append(file_read(r))
        if backup:
            back_up_pair(files, olds, ts)
        out: list[Json] = []  # mtimes (ints): they come back through as_user's JSON
        for f, r in zip(files, olds, strict=False):
            tmp = f + ".cst-tmp"
            if kind(tmp) != LstatKind.ABSENT:
                os.unlink(tmp)  # left by a kill
            u_write_new(tmp, data, r["mode"])
            os.rename(tmp, f)
            fsync_dir(os.path.dirname(f))
            out.append(os.lstat(f).st_mtime_ns)
        return out

    def write_vdf_pair(
        self,
        files: Sequence[str],
        expect: Sequence[bytes],
        expect_mtime: Sequence[int],
        data: bytes,
        ts: str,
        *,
        backup: bool,
    ) -> list[int]:
        mtimes = as_user(
            self.pw,
            self.home,
            is_list,
            self.u_write_vdf_pair,
            files,
            [base64.b64encode(r).decode() for r in expect],
            expect_mtime,
            base64.b64encode(data).decode(),
            ts,
            backup,
        )
        return [json_int(m) for m in mtimes]
