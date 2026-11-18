# SPDX-License-Identifier: MPL-2.0
"""cargo's build folders on disk: the root from build-dir, their size and
last activity, cargo's locks in them, and their removal.
"""

from __future__ import annotations

import fcntl
import os
import stat
import time
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from cst_core.domain.errors import Refused
from toolchains.domain.build import (
    FUTURE_SLACK,
    Folder,
    HASH_LEAF,
    HASH_TOP,
    HASH_VAR,
    INCREMENTAL,
    LOCK_NAMES,
    TRASH_NAME,
    classify,
    expected_template,
)
from toolchains.domain.common import tilde
from toolchains.adapters.program import PROG, RUNNER


def cargo_config_path(home: str) -> str:
    ch = os.environ.get("CARGO_HOME", "")
    base = ch if os.path.isabs(ch) else os.path.join(home, ".cargo")
    return os.path.join(base, "config.toml")


def resolve_root(home: str) -> str | None:
    """The root folder, from build-dir in the user's cargo config. Raises
    Refused when the setting is not the one CST writes, or the folder is odd.
    Returns None when the folder does not exist yet (nothing to do)."""
    cfg = cargo_config_path(home)
    want = expected_template(home)
    try:
        with open(cfg, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise Refused(f"{tilde(cfg, home)} not found: build-dir is not set ({PROG} config apply sets it)") from None
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise Refused(f"cannot read {tilde(cfg, home)}: {e}") from None
    build = data.get("build")
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
                    folder.inc.append([e.path, 0])
                    sub = len(folder.inc) - 1
                folder.size += s.st_blocks * 512
                if sub >= 0:
                    folder.inc[sub][1] += s.st_blocks * 512
                if stat.S_ISDIR(s.st_mode):
                    stack.append((e.path, depth + 1, sub))
    folder.activity = newest if newest else now
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
        try:
            os.close(fd)
        except OSError:
            pass


def remove_tree(path: str, timeout: float | None = None) -> tuple[bool, str]:
    """rm -rf --one-file-system (never follows symlinks). TIMEOUT (seconds,
    None: none): rm is stopped then; what is left of PATH (an already
    renamed-away folder) is a leftover that the next run deletes."""
    effective_timeout = None if timeout is None else max(1.0, float(timeout))
    r = RUNNER.run(
        ["rm", "-rf", "--one-file-system", "--", path],
        timeout=effective_timeout,
        stdin="inherit",
        output="merge",
    )
    if r.fault == "timeout":
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
