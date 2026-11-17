# SPDX-License-Identifier: MPL-2.0
"""What btrfs-snapshot-core reads and changes on the machine, behind the
ports of btrfs/snapshot/ports.py: the Host (commands, clock, boot id,
directory entries, the cache-folder walk with file descriptors only, no
symlink followed; tests replace it), the btrfs commands, the state
directory, the pin file (which resolves pinned paths on the filesystem),
the run lock, the journal lines, and the local time zone.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Sequence
from typing import Literal, TextIO
import dataclasses
import datetime as dt
import errno
import os
import stat
import time
import zoneinfo
from btrfs.snapshot.domain import (
    BOOT_ID_FILE,
    CACHE_SIGNATURE,
    CACHE_TAG,
    CMD_TIMEOUT,
    WALK_PRUNE,
    CacheDir,
    CacheWalk,
    ParseError,
    SubvolInfo,
    SubvolPath,
    err_text,
    parse_subvolume_show,
)
from btrfs.snapshot.ports import SnapshotHostPort
from cst_core.adapters import SubprocessCommandRunner
from cst_core.adapters.files import locale_bytes, write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.ports import CommandRunnerPort


# ---- time ----------------------------------------------------------------------------
def local_tz(name: str | None, tz_env: str) -> dt.tzinfo:
    """The zone used for names and retention: --tz (NAME), else $TZ (TZ_ENV,
    which wiring reads), else /etc/localtime. An unknown NAME raises
    zoneinfo.ZoneInfoNotFoundError or ValueError, as zoneinfo does."""
    if name:
        return zoneinfo.ZoneInfo(name)
    env = tz_env.lstrip(":")
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


def known_zone(name: str) -> dt.tzinfo | None:
    """The zone NAME (a --tz value), or None when zoneinfo does not know it."""
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError, ValueError:
        return None


# ---- cache folders ---------------------------------------------------------------------
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
    files = size = 0
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
                files += 1
                if stat.S_ISREG(st.st_mode):
                    size += st.st_size
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
    return CacheDir(rel, files, size)


def walk_caches(root: str, skips: Sequence[str] = (), mode: Literal["count", "drop"] = "count") -> CacheWalk:
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
    return dataclasses.replace(result, seconds=time.monotonic() - start)


# The commands of a snapshot run: where it runs, never moved to the host.
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


class Host:
    """Everything that touches the system; tests replace it."""

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        """(rc, stdout, stderr) of argv in the C locale, within CMD_TIMEOUT; stdin
        is ours; bytes that are not UTF-8 become U+FFFD. A command that cannot
        start raises (as subprocess.run did)."""
        r = RUNNER.run(argv, timeout=CMD_TIMEOUT, locale_c=True, stdin="inherit")
        if r.fault == "missing":
            return 127, "", f"{argv[0]}: not found"
        if r.fault == "timeout":
            return 124, "", f"timed out after {CMD_TIMEOUT}s"
        if r.cause is not None:
            raise r.cause
        return r.returncode, r.stdout, r.stderr

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

    def walk_caches(self, root: str, skips: Sequence[str], mode: Literal["count", "drop"]) -> CacheWalk:
        return walk_caches(root, skips, mode)


class Btrfs:
    """The btrfs commands, run through a host (BtrfsPort)."""

    def __init__(self, host: SnapshotHostPort) -> None:
        self.host = host

    def show(self, path: SubvolPath) -> SubvolInfo:
        rc, out, err = self.host.run(["btrfs", "subvolume", "show", path])
        if rc != 0:
            raise ParseError(f"btrfs subvolume show failed (rc={rc}): {err.strip()}")
        return parse_subvolume_show(out)

    def readonly(self, path: SubvolPath) -> bool:
        rc, out, err = self.host.run(["btrfs", "property", "get", "-ts", path, "ro"])
        if rc != 0:
            raise ParseError(f"btrfs property get failed (rc={rc}): {err.strip()}")
        value = out.strip()
        if value == "ro=true":
            return True
        if value == "ro=false":
            return False
        raise ParseError(f"unexpected property output {value!r}")

    def snapshot(self, source: SubvolPath, dest: str, *, readonly: bool = True) -> tuple[int, str, str]:
        return self.host.run(["btrfs", "subvolume", "snapshot"] + (["-r"] if readonly else []) + [source, dest])

    def set_readonly(self, path: SubvolPath) -> tuple[int, str, str]:
        return self.host.run(["btrfs", "property", "set", "-ts", path, "ro", "true"])

    def delete(self, path: SubvolPath) -> tuple[int, str, str]:
        return self.host.run(["btrfs", "subvolume", "delete", path])


# ---- the state directory, the pin file, the run lock, the journal ----------------------
class StateDir:
    """The lists and the last-run record in --state-dir (StateStorePort)."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir

    def path(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def read_list(self, name: str) -> list[str]:
        """The entries (one per line) of STATE/name; [] if it does not exist.
        OSError if it cannot be read. Bytes that are not UTF-8 become U+FFFD
        (such a line names no snapshot, so it is left alone with a warning)."""
        try:
            with open(self.path(name), encoding="utf-8", errors="replace") as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def write_list(self, name: str, entries: Sequence[str], *, sync_dir: bool = True) -> None:
        """Replace STATE/name with ENTRIES, durably (fsync of the file and,
        with SYNC_DIR, the folder); removed when ENTRIES is empty.
        World-readable: cst doctor runs as the user."""
        path = self.path(name)
        if not entries:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return
        os.makedirs(self.state_dir, mode=0o755, exist_ok=True)
        # 0644 whatever the umask (sudo keeps a user's 077).
        # The locale's encoding, as the file object it used to write through (open()'s default).
        text = locale_bytes("".join(f"{e}\n" for e in entries))
        write_atomic(path, text, mode=0o644, durable=sync_dir or "file", relabel=False)

    def read_record(self, name: str) -> str | None:
        """STATE/name's text (the locale's encoding), stripped; None if it does not exist."""
        try:
            with open(self.path(name)) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None


class PinFile:
    """The pin file (PinsPort): one path per line, each normalised and resolved."""

    def __init__(self, pin_file: str) -> None:
        self.pin_file = pin_file

    def read(self) -> tuple[set[str], str | None]:
        """(the pinned paths, problem or None); a file that breaks halfway keeps the lines read before."""
        pins: set[str] = set()
        try:
            with open(self.pin_file) as f:
                for raw in f:
                    line = raw.strip()
                    if line:
                        pins.add(os.path.normpath(line))
                        pins.add(os.path.realpath(line))
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError) as e:
            return pins, f"cannot read the pin file {self.pin_file}: {e}"
        return pins, None

    def covers(self, path: str, pins: Collection[str]) -> bool:
        if not path or not pins:
            return False
        return os.path.normpath(path) in pins or os.path.realpath(path) in pins


class RunLock:
    """The run lock (RunLockPort): an flock on the lock file, never waited for."""

    def acquire(self, path: str) -> Callable[[], None] | None:
        fd = take_lock(path, wait=False)
        if fd is None:
            return None
        return lambda: os.close(fd)


class Log:
    """btrfs-snapshot's journal lines ("<prio>btrfs-snapshot: msg") on the
    stream main chose: stdout, or stderr for the commands whose stdout is
    their answer (latest, caches, --json)."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def __call__(self, level: int, msg: str) -> None:
        journal_log(level, msg, tag="btrfs-snapshot", stream=self.stream)
