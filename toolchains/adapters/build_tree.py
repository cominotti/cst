# SPDX-License-Identifier: MPL-2.0
"""cargo's build folders on disk: the root from build-dir, their size and
last activity, cargo's locks in them, and their removal; CargoBuildTree
(BuildTreePort) puts them together with the processes and workspaces that use
them, and deletes a folder only while it holds its locks and nothing uses it.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import time
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cst_core.ports import ClockPort
    from toolchains.domain.prune import Limits
    from toolchains.ports import Scan, Users

from cst_core.domain.errors import Refused
from cst_core.domain.shapes import json_object, parse_toml
from toolchains.domain.build import (
    TRASH_PREFIX,
    BuildTreeResult,
    CargoCache,
    CargoProc,
    Workspaces,
    unknown_text,
    users_text,
    FUTURE_SLACK,
    Folder,
    HASH_LEAF,
    HASH_TOP,
    HASH_VAR,
    INCREMENTAL,
    IncrementalDir,
    LOCK_NAMES,
    TRASH_NAME,
    classify,
    expected_template,
)
from toolchains.domain.common import tilde
from toolchains.adapters.cargo import cargo_users, claimed_folders
from toolchains.adapters.processes import cargo_procs, proc_users
from toolchains.adapters.program import RUNNER
from cst_core.ports import CommandFault, OutputMode


def cargo_config_path(home: str) -> str:
    ch = os.environ.get("CARGO_HOME", "")
    base = ch if os.path.isabs(ch) else os.path.join(home, ".cargo")
    return os.path.join(base, "config.toml")


def resolve_root(home: str, prog: str) -> str | None:
    """The root folder, from build-dir in the user's cargo config. Raises
    Refused when the setting is not the one CST writes, or the folder is odd
    (PROG: how the hint names the program). Returns None when the folder does
    not exist yet (nothing to do)."""
    cfg = cargo_config_path(home)
    want = expected_template(home)
    try:
        with open(cfg, "rb") as f:
            data = parse_toml(f.read())
    except FileNotFoundError:
        raise Refused(f"{tilde(cfg, home)} not found: build-dir is not set ({prog} config apply sets it)") from None
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise Refused(f"cannot read {tilde(cfg, home)}: {e}") from None
    build = data.get("build") if json_object(data) else None  # tomllib gives a table
    value = build.get("build-dir") if isinstance(build, dict) else None
    if value != want:
        raise Refused(
            f"build-dir in {tilde(cfg, home)} is {value!r}, not the one CST writes ({want!r}): nothing is done"
        )
    root = want[: -len("/" + HASH_VAR)]
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise Refused(f"{root} is a symlink: refused")
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{root} is not a folder: refused")
    if st.st_uid != os.getuid():
        raise Refused(f"{root} belongs to uid {st.st_uid}, not to you: refused")
    if os.path.realpath(root) != root:
        raise Refused(f"{root} goes through a symlink ({os.path.realpath(root)}): refused")
    return root


def measure(folder: Folder, dev: int, now: float) -> Folder:
    """Size (st_blocks, hardlinks once), newest mtime and entry count of a
    folder; never follows symlinks, never leaves the filesystem. On btrfs,
    st_blocks is the size before compression (what du shows), not the
    compressed extents actually on disk. Also sizes the incremental
    folders (folder.inc) on the way."""
    seen: set[int] = set()
    newest = 0.0
    try:
        st = os.lstat(folder.path)
    except OSError as e:
        folder.problem = f"cannot read: {e.strerror}"
        return folder
    # (path, depth below the folder, index into folder.inc or -1)
    stack = [(folder.path, 0, -1)]
    if st.st_mtime <= now + FUTURE_SLACK:
        newest = st.st_mtime
    folder.size += st.st_blocks * 512
    while stack:
        d, depth, inc = stack.pop()
        try:
            it = os.scandir(d)
        except FileNotFoundError:
            continue
        except OSError as e:
            folder.problem = f"cannot read {d}: {e.strerror}"
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as err:
                    folder.problem = f"cannot read {e.path}: {err.strerror}"
                    continue
                folder.entries += 1
                if s.st_dev != dev:
                    folder.foreign += 1
                    continue
                if s.st_mtime > now + FUTURE_SLACK:
                    folder.future += 1
                elif s.st_mtime > newest:
                    newest = s.st_mtime
                if s.st_nlink > 1 and not stat.S_ISDIR(s.st_mode):
                    if s.st_ino in seen:
                        continue
                    seen.add(s.st_ino)
                sub = inc
                if sub < 0 and depth + 1 in (2, 3) and e.name == INCREMENTAL and stat.S_ISDIR(s.st_mode):
                    folder.inc.append(IncrementalDir(e.path))
                    sub = len(folder.inc) - 1
                folder.size += s.st_blocks * 512
                if sub >= 0:
                    folder.inc[sub].size += s.st_blocks * 512
                if stat.S_ISDIR(s.st_mode):
                    stack.append((e.path, depth + 1, sub))
    folder.activity = newest or now
    if folder.foreign:
        folder.problem = folder.problem or f"{folder.foreign} entr(y/ies) on another filesystem"
    return folder


def scan(root: str, now: float) -> tuple[list[Folder], list[Folder], list[tuple[str, str]]]:
    """(folders, trash, others): the hash folders (measured), leftovers of an
    interrupted run, and anything else in the root (name, why left alone)."""
    folders: list[Folder] = []
    trash: list[Folder] = []
    others: list[tuple[str, str]] = []
    rst = os.lstat(root)
    dev = rst.st_dev
    try:
        with os.scandir(root) as it:
            tops = sorted(it, key=lambda e: e.name)
    except FileNotFoundError:
        tops = []
    for e in tops:
        try:
            s = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if TRASH_NAME.match(e.name):
            if stat.S_ISDIR(s.st_mode) and s.st_dev == dev:
                t = Folder(root, "", "")
                t.key, t.path = e.name, e.path
                trash.append(measure(t, dev, now))
            else:
                others.append((e.name, "not a folder on this filesystem"))
            continue
        why = classify(e.name, s, dev, HASH_TOP)
        if why:
            others.append((e.name, why))
            continue
        try:
            with os.scandir(e.path) as it2:
                leaves = sorted(it2, key=lambda x: x.name)
        except FileNotFoundError:
            continue  # removed meanwhile (apply removes an XX/ it emptied)
        except OSError as err:
            others.append((e.name, f"cannot read: {err.strerror}"))
            continue
        for leaf in leaves:
            try:
                ls = leaf.stat(follow_symlinks=False)
            except OSError:
                continue
            why = classify(leaf.name, ls, dev, HASH_LEAF)
            if why:
                others.append((f"{e.name}/{leaf.name}", why))
                continue
            folders.append(measure(Folder(root, e.name, leaf.name), dev, now))
    return folders, trash, others


def lock_files(path: str) -> list[str]:
    """Cargo's lock files in a build folder: <profile>/<lock> and
    <triple>/<profile>/<lock> (regular files only)."""
    out: list[str] = []
    try:
        with os.scandir(path) as it:
            level1 = [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out
    dirs = [e.path for e in level1]
    for e in level1:
        try:
            with os.scandir(e.path) as it:
                dirs += [x.path for x in it if x.is_dir(follow_symlinks=False)]
        except OSError:
            pass
    for d in dirs:
        for n in LOCK_NAMES:
            p = os.path.join(d, n)
            try:
                if stat.S_ISREG(os.lstat(p).st_mode):
                    out.append(p)
            except OSError:
                pass
    return sorted(out)


def take_locks(path: str) -> tuple[list[int], str | None]:
    """Exclusive non-blocking flocks on every cargo lock in PATH:
    (fds, None) or ([], busy lock path) with nothing held."""
    fds: list[int] = []
    for p in lock_files(path):
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        except OSError:
            release(fds)
            return [], p
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            release(fds)
            return [], p
        fds.append(fd)
    return fds, None


def release(fds: Sequence[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def remove_tree(path: str, timeout: float | None = None) -> tuple[bool, str]:
    """rm -rf --one-file-system (never follows symlinks). TIMEOUT (seconds,
    None: none): rm is stopped then; what is left of PATH (an already
    renamed-away folder) is a leftover that the next run deletes."""
    effective_timeout = None if timeout is None else max(1.0, float(timeout))
    r = RUNNER.run(
        ["rm", "-rf", "--one-file-system", "--", path],
        timeout=effective_timeout,
        stdin="inherit",
        output=OutputMode.MERGE,
    )
    if r.fault == CommandFault.TIMEOUT:
        secs = int(effective_timeout) if effective_timeout is not None else 0
        return False, f"stopped after {secs}s (the run's time budget); the next run deletes the rest"
    if r.cause is not None:
        raise r.cause
    return r.returncode == 0, r.stdout.strip()


def tree_size(path: str) -> int:
    """Size of a tree as du shows it (st_blocks, hardlinks once); symlinks
    not followed, other filesystems not entered."""
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return 0
    f = Folder("", "", "")
    f.path = path
    return measure(f, dev, time.time()).size


class CargoBuildTree:
    """BuildTreePort over HOME's cargo config and build folders, the user's
    processes (PROCS: the cargo ones; a test gives its own) and the workspaces
    under ~/Workspace. A test replaces open_by, cargo_runs or remove on an
    instance: delete and trim call them, as the use case does."""

    def __init__(
        self,
        home: str,
        prog: str,
        limits: Limits,
        clock: ClockPort,
        procs: Callable[[], list[CargoProc]] = cargo_procs,
    ) -> None:
        self.home = home
        self.prog = prog
        self.limits = limits
        self.clock = clock
        self.procs = procs

    def time_left(self, deadline: float | None) -> float | None:
        """Seconds until DEADLINE (the clock's monotonic time), or None without one."""
        return None if deadline is None else deadline - self.clock.monotonic()

    def root(self) -> str | None:
        return resolve_root(self.home, self.prog)

    def scan(self, root: str, now: float) -> Scan:
        return scan(root, now)

    def device(self, root: str) -> int:
        return os.lstat(root).st_dev

    def measure(self, folder: Folder, dev: int, now: float) -> Folder:
        return measure(folder, dev, now)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def remove(self, path: str, timeout: float | None) -> tuple[bool, str]:
        return remove_tree(path, timeout)

    def open_by(self, root: str) -> tuple[Users, int]:
        return proc_users(root)

    def cargo_runs(self, root: str, cache: CargoCache) -> tuple[Users, list[tuple[str, str, str | None, str]]]:
        return cargo_users(root, self.home, cache, self.procs())

    def workspaces(self, root: str) -> Workspaces:
        return claimed_folders(root, self.home, self.limits)

    def guarded(
        self,
        root: str,
        folder: Folder,
        cache: CargoCache,
        step: Callable[[], tuple[BuildTreeResult, str]],
        recheck: Callable[[], str] | None,
    ) -> tuple[BuildTreeResult, str]:
        """Run STEP() while holding every cargo lock of FOLDER, after checking
        that no process uses it and, with RECHECK, that the rule that chose the
        action still holds (RECHECK() measures the folder again and returns ''
        or why not). STEP returns (result, detail). Returns that, or
        ('busy', why), ('changed', why) from RECHECK, or ('stop', why): a cargo
        process whose build folder is unknown runs; nothing more is deleted in
        this run."""
        fds, busy = take_locks(folder.path)
        if busy is not None:
            return BuildTreeResult.BUSY, f"cargo lock held: {os.path.relpath(busy, folder.path)}"
        # The users are checked while the locks are held: a cargo that starts
        # after this check must first compile, and that waits for the lock.
        try:
            users = self.open_by(root)[0].get(folder.key)
            if users:
                return BuildTreeResult.BUSY, "in use: " + users_text(users)
            ties, unknown = self.cargo_runs(root, cache)
            if ties.get(folder.key):
                return BuildTreeResult.BUSY, "cargo runs on it: " + users_text(ties[folder.key])
            if unknown:
                return BuildTreeResult.STOP, unknown_text(unknown)
            # The plan was made from the activity measured when the run started;
            # a build may have written there since (and ended: no lock, no
            # process). Measured again here, while the locks are held: a cargo
            # that starts now waits for them.
            why = recheck() if recheck else ""
            if why:
                return BuildTreeResult.CHANGED, why
            return step()
        finally:
            release(fds)

    def delete(
        self,
        root: str,
        folder: Folder,
        now: float,
        cache: CargoCache,
        recheck: Callable[[], str] | None,
        deadline: float | None,
    ) -> tuple[BuildTreeResult, str]:
        """Lock, check for users (and RECHECK, see guarded), rename away,
        unlock, delete (the rm stopped at DEADLINE, the clock's monotonic time).
        ('deleted'|'busy'|'changed'|'stop'|'failed', detail)."""
        try:
            st = os.lstat(folder.path)
        except FileNotFoundError:
            return BuildTreeResult.FAILED, "already gone"
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return BuildTreeResult.FAILED, "no longer a folder (refused)"
        top = os.path.dirname(folder.path)
        trash = os.path.join(root, f"{TRASH_PREFIX}{folder.key.replace('/', '')}-{int(now)}")

        def step() -> tuple[BuildTreeResult, str]:
            try:
                os.rename(folder.path, trash)
            except OSError as e:
                return BuildTreeResult.FAILED, f"rename: {e.strerror}"
            return BuildTreeResult.MOVED, ""

        result, detail = self.guarded(root, folder, cache, step, recheck)
        if result != BuildTreeResult.MOVED:
            return result, detail
        with contextlib.suppress(OSError):
            os.rmdir(top)  # the XX folder, when this was its last hash
        ok, msg = self.remove(trash, self.time_left(deadline))
        if not ok:
            return BuildTreeResult.FAILED, f"rm: {msg}"
        # A cargo that was waiting for the lock during the rename builds again,
        # from scratch, into a new folder at the same path (it does not fail).
        if os.path.lexists(folder.path):
            return BuildTreeResult.DELETED, "a build started meanwhile and is filling a new folder at the same path"
        return BuildTreeResult.DELETED, ""

    def trim(
        self,
        root: str,
        folder: Folder,
        now: float,
        cache: CargoCache,
        recheck: Callable[[], str] | None,
        deadline: float | None,
    ) -> tuple[BuildTreeResult, str]:
        """Delete only the incremental folders of FOLDER, with the same locks and
        checks as delete (RECHECK may update folder.inc). Each is renamed away while the locks are held;
        the modification time of its parent is put back afterwards, so that the
        folder's last activity (MAX_IDLE) does not move. ('trimmed'|'busy'|
        'stop'|'failed', detail)."""
        moved: list[str] = []
        base = folder.key.replace("/", "")

        def step() -> tuple[BuildTreeResult, str]:
            for i, d in enumerate(folder.inc):
                path = d.path
                parent = os.path.dirname(path)
                try:
                    pst = os.lstat(parent)
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(pst.st_mode):
                    continue
                trash = os.path.join(root, f"{TRASH_PREFIX}{base}-inc{i}-{int(now)}")
                try:
                    os.rename(path, trash)
                except OSError as e:
                    return BuildTreeResult.FAILED, f"rename {os.path.relpath(path, folder.path)}: {e.strerror}"
                moved.append(trash)
                with contextlib.suppress(OSError):
                    os.utime(parent, ns=(pst.st_atime_ns, pst.st_mtime_ns), follow_symlinks=False)
            return BuildTreeResult.MOVED, ""

        result, detail = self.guarded(root, folder, cache, step, recheck)
        # What was renamed away is deleted also after a later failure (else the
        # next run deletes it as a leftover).
        errors: list[str] = []
        for t in moved:
            ok, msg = self.remove(t, self.time_left(deadline))
            if not ok:
                errors.append(msg)
        if result != BuildTreeResult.MOVED:
            return result, detail
        if errors:
            return BuildTreeResult.FAILED, f"rm: {errors[0]}"
        return BuildTreeResult.TRIMMED, ""
