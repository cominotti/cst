"""What btrfs-snapshot-core reads and changes on the machine: the Host
(commands, clock, boot id, directory entries; tests replace it), the btrfs
commands, the scan of a snapshot directory, the cache-folder walk (file
descriptors only, no symlink followed), the local time zone, and the
safeguards, which resolve pinned paths on the filesystem.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
import dataclasses
import datetime as dt
import errno
import os
import stat
import subprocess
import time
import zoneinfo
from btrfs.snapshot.domain import (
    BOOT_ID_FILE,
    CACHE_SIGNATURE,
    CACHE_TAG,
    CLOCK_SLACK,
    CMD_TIMEOUT,
    Decision,
    KEEP_NEWEST,
    MIN_AGE,
    ParseError,
    Snap,
    WALK_PRUNE,
    SubvolInfo,
    err_text,
    name_regex,
    other_parent,
    parse_snapshot_name,
    parse_subvolume_show,
    plan,
    wall_to_epoch,
)


# ---- time ----------------------------------------------------------------------------
def local_tz(name: str | None = None) -> dt.tzinfo:
    """The zone used for names and retention: --tz, else $TZ, else /etc/localtime."""
    if name:
        return zoneinfo.ZoneInfo(name)
    env = os.environ.get("TZ", "").lstrip(":")
    if env:
        try:
            if env.startswith("/"):
                with open(env, "rb") as f:
                    return zoneinfo.ZoneInfo.from_file(f, key=env)
            return zoneinfo.ZoneInfo(env)
        except OSError, ValueError, zoneinfo.ZoneInfoNotFoundError:
            pass
    try:
        with open("/etc/localtime", "rb") as f:
            return zoneinfo.ZoneInfo.from_file(f, key="localtime")
    except OSError, ValueError:
        return dt.UTC


def apply_safeguards(
    decisions: list[Decision],
    now: float,
    tz: dt.tzinfo,
    pins: Collection[str],
) -> list[Decision]:
    """The decisions with policy deletions turned into keeps for the 3 newest
    (by generation and by name), anything younger than 24 h, and pinned paths."""
    snaps = [d.snap for d in decisions]
    protected: set[int] = set()
    for key in (lambda s: (s.cgen, s.wall, s.seq), lambda s: (wall_to_epoch(s.wall, tz), s.seq)):
        protected.update(id(s) for s in sorted(snaps, key=key, reverse=True)[:KEEP_NEWEST])
    out: list[Decision] = []
    for d in decisions:
        s = d.snap
        if d.action != "delete":
            out.append(d)
        elif id(s) in protected:
            out.append(
                dataclasses.replace(d, action="keep", reason=f"safeguard: one of the {KEEP_NEWEST} newest ({d.reason})")
            )
        elif now - wall_to_epoch(s.wall, tz) < MIN_AGE:
            out.append(dataclasses.replace(d, action="keep", reason=f"safeguard: younger than 24 h ({d.reason})"))
        elif is_pinned(s.path, pins):
            out.append(dataclasses.replace(d, action="keep", reason=f"safeguard: pinned ({d.reason})"))
        else:
            out.append(d)
    return out


def is_pinned(path: str, pins: Collection[str]) -> bool:
    if not path or not pins:
        return False
    return os.path.normpath(path) in pins or os.path.realpath(path) in pins


def unexplained_deletions(
    decisions: Sequence[Decision],
    snaps: Sequence[Snap],
    policy: Mapping[str, int | str],
    tz: dt.tzinfo,
    pins: Collection[str],
    since: int | None,
    ref: float | None = None,
) -> list[Decision]:
    """The deletions in DECISIONS (made now) that time passing does not explain.

    A deletion is explained when the snapshot was kept at SINCE (the last
    successful run: policy and safeguards replayed over the snapshots named up
    to then) and is due at REF (the trusted current time; None = the time the
    decisions were made). After the machine was off, whole days of hourly
    snapshots age out at once and all of them are explained, so the circuit
    breaker does not trip on them; a shrunken policy dooms snapshots that were
    already due at SINCE, and a clock that jumped ahead of REF dooms snapshots
    not yet due at REF - those stay unexplained. SINCE None: nothing is
    explained (no successful run recorded)."""
    doomed = [d for d in decisions if d.action == "delete" and not d.snap.virtual]
    if since is None or not doomed:
        return doomed
    real = [s for s in snaps if not s.virtual]
    before = [s for s in real if wall_to_epoch(s.wall, tz) <= since + CLOCK_SLACK]
    kept_then = {
        id(d.snap) for d in apply_safeguards(plan(before, since, policy, tz), since, tz, pins) if d.action == "keep"
    }
    if ref is None:
        due_now = {id(d.snap) for d in doomed}
    else:
        due_now = {
            id(d.snap) for d in apply_safeguards(plan(real, ref, policy, tz), ref, tz, pins) if d.action == "delete"
        }
    return [d for d in doomed if not (id(d.snap) in kept_then and id(d.snap) in due_now)]


# ---- cache folders ---------------------------------------------------------------------
@dataclasses.dataclass
class CacheDir:
    rel: str  # path relative to the walked root
    files: int = 0  # entries that are not folders (files, symlinks, ...)
    size: int = 0  # apparent size of the regular files, bytes


@dataclasses.dataclass
class CacheWalk:
    folders: list[CacheDir]  # tagged folders (dropped in "drop" mode)
    kept: list[str]  # rel: tagged, but holding a skipped path, so kept and walked
    skipped: list[str]  # rel: skip paths that exist (never entered)
    problems: list[tuple[str, str]]  # (rel, text): what could not be read or deleted
    seconds: float = 0.0

    @property
    def files(self) -> int:
        return sum(c.files for c in self.folders)

    @property
    def size(self) -> int:
        return sum(c.size for c in self.folders)


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
NOATIME = getattr(os, "O_NOATIME", 0)


def open_noatime(name: str, flags: int, dir_fd: int | None) -> int:
    """os.open with O_NOATIME: reading a folder or a tag must not write its
    access time (with relatime, the first read after a change, or after a
    day, would: one metadata write per folder in the snapshot, or a change of
    the source in a dry run). O_NOATIME needs the owner or root (CAP_FOWNER);
    without it (EPERM) the plain open."""
    try:
        return os.open(name, flags | NOATIME, dir_fd=dir_fd)
    except PermissionError as e:
        if not NOATIME or e.errno != errno.EPERM:
            raise
        return os.open(name, flags, dir_fd=dir_fd)


def has_cache_tag(dir_fd: int) -> bool:
    """The folder open as DIR_FD holds a regular file CACHEDIR.TAG that starts
    with the signature. Never follows a symlink and never opens anything
    but a regular file (a FIFO or device named CACHEDIR.TAG is no tag)."""
    try:
        st = os.stat(CACHE_TAG, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            return False
        fd = open_noatime(CACHE_TAG, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd)
    except OSError:
        return False
    try:
        # A regular file: a short read only at its end.
        return stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, len(CACHE_SIGNATURE)) == CACHE_SIGNATURE
    except OSError:
        return False
    finally:
        os.close(fd)


def subdirs(dir_fd: int) -> list[str]:
    """Names of the folders directly in DIR_FD (not symlinks to folders), sorted."""
    with os.scandir(dir_fd) as it:
        return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))


@dataclasses.dataclass
class Level:
    """One open folder on a walk's stack (tree, walk_caches)."""

    fd: int
    name: str  # its name in the folder below it on the stack
    rel: str  # its path relative to the walk's root
    todo: list[str] | None = None  # subfolders left to visit; None = not listed yet


def tree(
    parent_fd: int, name: str, top: int, dev: int, delete: bool, problems: list[tuple[str, str]], rel: str
) -> CacheDir:
    """Count (and with DELETE remove) the folder NAME below PARENT_FD, open
    as TOP (closed here), and everything in it. File descriptors only: no
    symlink is followed, and a
    folder on another device (a mount, another subvolume) is never entered;
    with DELETE it is only removed if empty. The CACHEDIR.TAG of the top
    folder goes last, so a run that stops halfway leaves a folder that is
    still recognised as a cache (it stays when anything below could not be
    deleted). Returns a CacheDir; failures go to PROBLEMS (rel, text) and the
    rest goes on."""
    out = CacheDir(rel)
    before = len(problems)
    stack = [Level(top, name, rel)]
    while stack:
        level = stack[-1]
        is_top = len(stack) == 1
        if level.todo is None:
            level.todo = []
            try:
                with os.scandir(level.fd) as it:
                    entries = list(it)
            except OSError as e:
                problems.append((level.rel, f"cannot list: {e.strerror}"))
                entries = []
            for e in sorted(entries, key=lambda x: x.name):
                try:
                    if e.is_dir(follow_symlinks=False):
                        level.todo.append(e.name)
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError as err:
                    problems.append((os.path.join(level.rel, e.name), err_text(err)))
                    continue
                out.files += 1
                if stat.S_ISREG(st.st_mode):
                    out.size += st.st_size
                if delete and not (is_top and e.name == CACHE_TAG):  # the top's tag goes last
                    try:
                        os.unlink(e.name, dir_fd=level.fd)
                    except OSError as err:
                        problems.append((os.path.join(level.rel, e.name), f"cannot delete: {err.strerror}"))
            level.todo.reverse()
        if level.todo:
            child = level.todo.pop()
            crel = os.path.join(level.rel, child)
            try:
                cfd = open_noatime(child, DIR_FLAGS, level.fd)
            except OSError as e:
                problems.append((crel, f"cannot open: {e.strerror}"))
                continue
            if os.fstat(cfd).st_dev != dev:
                os.close(cfd)
                if delete:
                    try:
                        os.rmdir(child, dir_fd=level.fd)  # an empty placeholder; never a mount's content
                    except OSError as e:
                        problems.append((crel, f"on another device, not entered: {e.strerror}"))
                continue
            stack.append(Level(cfd, child, crel))
            continue
        # Every subfolder handled: close this one and remove it from its parent.
        stack.pop()
        failed = len(problems) > before
        if delete and is_top and not failed:
            try:
                os.unlink(CACHE_TAG, dir_fd=level.fd)
            except FileNotFoundError:
                pass
            except OSError as e:
                problems.append((os.path.join(level.rel, CACHE_TAG), f"cannot delete: {e.strerror}"))
                failed = True
        os.close(level.fd)
        # The top stays when anything in it failed (still a cache, its tag kept).
        if delete and not (is_top and failed):
            try:
                os.rmdir(level.name, dir_fd=parent_fd if is_top else stack[-1].fd)
            except OSError as e:
                # Not empty because something below could not go: already reported.
                if not (failed and e.errno == errno.ENOTEMPTY):
                    problems.append((level.rel, f"cannot delete: {e.strerror}"))
    return out


def walk_caches(root: str, skips: Sequence[str] = (), mode: str = "count") -> CacheWalk:
    """The cache folders below ROOT (never ROOT itself): folders holding a
    CACHEDIR.TAG with the signature (has_cache_tag). Not followed: symlinks;
    not entered: .git, node_modules, the paths in SKIPS (relative to ROOT,
    "." = all of ROOT), folders on another device (a nested subvolume or a
    mount: checked before the tag), and anything below a cache folder. A
    tagged folder that holds a skipped path is kept and walked further.
    MODE: "count" finds them and counts their files and bytes, "drop"
    deletes them (counting as it goes). Returns a CacheWalk."""
    start = time.monotonic()
    skip_set = {os.path.normpath(s) for s in skips}
    holders: set[str] = set()  # folders above a skipped path
    for s in skip_set:
        parent = os.path.dirname(s)
        while parent:
            holders.add(parent)
            parent = os.path.dirname(parent)
    result = CacheWalk([], [], [], [])
    if "." in skip_set:
        result.skipped.append(".")
        return result
    root_fd = open_noatime(root, DIR_FLAGS, None)
    dev = os.fstat(root_fd).st_dev
    stack = [Level(root_fd, "", "")]
    try:
        while stack:
            level = stack[-1]
            if level.todo is None:
                try:
                    level.todo = subdirs(level.fd)[::-1]
                except OSError as e:
                    result.problems.append((level.rel or ".", f"cannot list: {e.strerror}"))
                    level.todo = []
            if not level.todo:
                os.close(stack.pop().fd)
                continue
            name = level.todo.pop()
            crel = os.path.join(level.rel, name) if level.rel else name
            if name in WALK_PRUNE:
                continue
            if crel in skip_set:
                result.skipped.append(crel)
                continue
            try:
                cfd = open_noatime(name, DIR_FLAGS, level.fd)
            except OSError as e:
                result.problems.append((crel, f"cannot open: {e.strerror}"))
                continue
            # The device first: a nested subvolume (~/Bulk, ~/.cache) with a
            # tag is an empty placeholder in a snapshot, nothing to drop.
            if os.fstat(cfd).st_dev != dev:
                os.close(cfd)
                continue
            if not has_cache_tag(cfd):
                stack.append(Level(cfd, name, crel))
                continue
            if crel in holders:
                result.kept.append(crel)
                stack.append(Level(cfd, name, crel))
                continue
            result.folders.append(tree(level.fd, name, cfd, dev, mode == "drop", result.problems, crel))
    finally:
        for level in stack:
            os.close(level.fd)
    result.seconds = time.monotonic() - start
    return result


class Host:
    """Everything that touches the system; tests replace it."""

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        env = dict(os.environ, LC_ALL="C")
        try:
            p = subprocess.run(argv, check=False, capture_output=True, text=True, env=env, timeout=CMD_TIMEOUT)
        except FileNotFoundError:
            return 127, "", f"{argv[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {CMD_TIMEOUT}s"
        return p.returncode, p.stdout, p.stderr

    def time(self) -> float:
        return time.time()

    def boot(self) -> tuple[str | None, float | None]:
        """(boot id, seconds since boot including suspend), or (None, None) if unknown.

        Real time elapsed between two runs of the same boot, whatever the wall
        clock did meanwhile."""
        try:
            with open(BOOT_ID_FILE) as f:
                boot_id = f.read().strip()
            return boot_id, time.clock_gettime(time.CLOCK_BOOTTIME)
        except OSError, AttributeError:
            return None, None

    def listdir(self, path: str) -> list[str]:
        return sorted(os.listdir(path))

    def kind(self, path: str) -> str:
        """'subvol' (a real subvolume root), 'symlink', 'missing' or 'other'."""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(st.st_mode):
            return "symlink"
        if stat.S_ISDIR(st.st_mode) and st.st_ino == 256:
            return "subvol"
        return "other"

    def mkdir(self, path: str, mode: int) -> None:
        os.mkdir(path, mode)

    def walk_caches(self, root: str, skips: Sequence[str], mode: str) -> CacheWalk:
        return walk_caches(root, skips, mode)


class Btrfs:
    def __init__(self, host: Host) -> None:
        self.host = host

    def show(self, path: str) -> SubvolInfo:
        rc, out, err = self.host.run(["btrfs", "subvolume", "show", path])
        if rc != 0:
            raise ParseError(f"btrfs subvolume show failed (rc={rc}): {err.strip()}")
        return parse_subvolume_show(out)

    def readonly(self, path: str) -> bool:
        rc, out, err = self.host.run(["btrfs", "property", "get", "-ts", path, "ro"])
        if rc != 0:
            raise ParseError(f"btrfs property get failed (rc={rc}): {err.strip()}")
        value = out.strip()
        if value == "ro=true":
            return True
        if value == "ro=false":
            return False
        raise ParseError(f"unexpected property output {value!r}")

    def snapshot(self, source: str, dest: str, readonly: bool = True) -> tuple[int, str, str]:
        return self.host.run(["btrfs", "subvolume", "snapshot"] + (["-r"] if readonly else []) + [source, dest])

    def set_readonly(self, path: str) -> tuple[int, str, str]:
        return self.host.run(["btrfs", "property", "set", "-ts", path, "ro", "true"])

    def delete(self, path: str) -> tuple[int, str, str]:
        return self.host.run(["btrfs", "subvolume", "delete", path])


@dataclasses.dataclass
class Scan:
    owned: list[Snap]
    ignored: list[tuple[str, str]]  # (path, reason): not ours, never touched
    unverifiable: list[tuple[str, str]]  # (path, reason): could be ours, blocks deletions
    writable: list[str] = dataclasses.field(default_factory=list)  # paths of the "not read-only" entries in ignored


def scan_snapshots(host: Host, btrfs: Btrfs, snapdir: str, name: str, source_uuid: str | None) -> Scan:
    """Owned snapshots of one source. source_uuid None = skip the parent check (latest)."""
    owned: list[Snap] = []
    ignored: list[tuple[str, str]] = []
    unverifiable: list[tuple[str, str]] = []
    writable: list[str] = []
    try:
        entries = host.listdir(snapdir)
    except FileNotFoundError:
        return Scan(owned, ignored, unverifiable)
    except OSError as e:
        raise ParseError(f"cannot list {snapdir}: {e.strerror}") from None
    pattern = name_regex(name)
    for entry in entries:
        if not pattern.fullmatch(entry):
            continue
        path = os.path.join(snapdir, entry)
        parsed = parse_snapshot_name(name, entry)
        if parsed is None:
            ignored.append((path, "invalid date in the name"))
            continue
        kind = host.kind(path)
        if kind != "subvol":
            ignored.append((path, "not a subvolume" if kind != "symlink" else "symlink"))
            continue
        try:
            info = btrfs.show(path)
            ro = btrfs.readonly(path)
        except ParseError as e:
            unverifiable.append((path, str(e)))
            continue
        if not ro:
            ignored.append((path, "not read-only"))
            writable.append(path)
            continue
        other = other_parent(info, source_uuid) if source_uuid is not None else None
        if other:
            ignored.append((path, other))
            continue
        owned.append(Snap(entry=entry, wall=parsed[0], seq=parsed[1], path=path, uuid=info["uuid"], cgen=info["cgen"]))
    return Scan(owned, ignored, unverifiable, writable)
