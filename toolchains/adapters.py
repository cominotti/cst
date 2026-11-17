# SPDX-License-Identifier: MPL-2.0
"""The host as cst-toolchains sees it: output lines (journal-aware), the
cargo build folders on disk, the processes that use them, cargo, git, uv,
rustup and sccache, the user manager (systemctl --user), and the files
CST writes in the home folder.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

from cst_core.adapters import SubprocessCommandRunner
from cst_core.adapters.files import owned_file_state_at, unit_file_state_at, write_atomic
from cst_core.adapters.runtime import program_path, restore_default_signals
from cst_core.domain.errors import Refused
from cst_core.domain.files import OwnedState, UnitFileState
from cst_core.ports import CommandRunnerPort
from toolchains.domain import (
    CACHE_TAG,
    CARGO_COMM,
    CARGO_ENV,
    CONF_REL,
    CMD_TIMEOUT,
    CRATE_DEPTH,
    ENUM_BUDGET,
    FUTURE_SLACK,
    HASH_LEAF,
    HASH_TOP,
    HASH_VAR,
    INCREMENTAL,
    LOCK_NAMES,
    MARKER,
    NAME,
    NOT_A_MEMBER,
    OFF_REL,
    OLD_CONF_REL,
    PIN_NAMES,
    REPO_DEPTH,
    RUST_TOOLS,
    SCAN_SKIP,
    SCCACHE_DIR_REL,
    SCCACHE_STATS,
    STATE_DIR_REL,
    TOOL_FILE_DIRS,
    TOOL_FILE_MAX,
    TOOL_FILE_TOP,
    TOOL_FILES_MAX,
    TRASH_NAME,
    TRASH_PREFIX,
    UV_LOCK_TIMED_OUT,
    UV_LOCK_WAIT,
    WORKSPACE_REL,
    WRAPPER_REL,
    Folder,
    Incomplete,
    Workspaces,
    classify,
    expected_template,
    is_tool_file,
    manifest_arg,
    prefixes,
    tilde,
    toolchain_pattern,
)

PROC = "/proc"
# How hints name the frontend ("cst toolchains" through cst).
PROG = os.environ.get("CST_PROG") or "cst toolchains"
# The commands cst-toolchains runs (each call names its stdin, environment
# and timeout; output that is not UTF-8 raises, as text=True did).
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


class Program:
    """The running cst-toolchains. main() sets entry_file (its entry point's
    __file__); toolchains.conf and rustc-wrapper are read next to the program:
    toolchains/ in CST, ~/.local/lib/cst for the timer's copy (a zipapp)."""

    entry_file: str = ""

    @classmethod
    def folder(cls) -> str:
        """The folder that holds the program (the zipapp's folder when installed)."""
        if not cls.entry_file:
            raise RuntimeError("Program.entry_file is not set: main() sets it")
        return os.path.dirname(program_path(cls.entry_file))


def journal_stdout() -> bool:
    """stdout is connected to the journal (systemd's documented check:
    JOURNAL_STREAM is "dev:inode" of that very stream; a variable inherited
    from elsewhere does not match)."""
    try:
        dev, ino = (int(x) for x in os.environ.get("JOURNAL_STREAM", "").split(":"))
        st = os.fstat(sys.stdout.fileno())
    except ValueError, OSError:
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


JOURNAL = journal_stdout()


def say(msg: str, prio: int = 6, err: bool = False) -> None:
    """One line; with the journal priority prefix when run by systemd."""
    line = f"<{prio}>{msg}" if JOURNAL else msg
    print(line, file=sys.stderr if err and not JOURNAL else sys.stdout, flush=True)


def where_text(path: str, home: str) -> str:
    """PATH (with ~), and where it really is when a symlink on the way leads
    elsewhere (the ~/Bulk links of `cst btrfs bulk`)."""
    real = os.path.realpath(path)
    shown = tilde(path, home)
    if real != path:
        shown += f" -> {tilde(real, home)}"
    return shown


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


def _own_processes(target_uid: int) -> Iterator[tuple[str, str, str]]:
    """(pid, comm, /proc/PID) of each process of target_uid but this one; a
    process that ended or cannot be read meanwhile is left out."""
    me = os.getpid()
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return
    for pid in pids:
        if int(pid) == me:
            continue
        base = os.path.join(PROC, pid)
        try:
            if os.stat(base).st_uid != target_uid:
                continue
            with open(os.path.join(base, "comm")) as f:
                comm = f.read().strip()
        except OSError:
            continue
        yield pid, comm, base


def proc_users(root: str, uid: int | None = None) -> tuple[dict[str, list[tuple[str, str]]], int]:
    """({folder key: [(pid, comm), ...]}, unreadable): the processes of the
    user that have their cwd, executable, an open file or a mapped file
    under ROOT. Keys are 'XX/YYYY' or a trash name. Processes of other users
    are not looked at; UNREADABLE counts the user's own that could not be
    inspected (from inside a container: those in other containers' user
    namespaces; the host sees them all) and that are Rust tools (cargo,
    rustc, clippy-driver, rust-analyzer, build scripts: by their name)."""
    target_uid = os.getuid() if uid is None else uid
    pre = prefixes(root)
    found: dict[str, list[tuple[str, str]]] = {}
    unreadable = 0

    def note(pid: str, comm: str, path: str) -> None:
        if path.endswith(" (deleted)"):
            path = path[: -len(" (deleted)")]
        for p in pre:
            if path.startswith(p + "/"):
                parts = path[len(p) + 1 :].split("/")
                key = parts[0] if parts[0].startswith(TRASH_PREFIX) else "/".join(parts[:2])
                lst = found.setdefault(key, [])
                if (pid, comm) not in lst:
                    lst.append((pid, comm))
                return

    for pid, comm, base in _own_processes(target_uid):
        try:
            note(pid, comm, os.readlink(os.path.join(base, "cwd")))
        except PermissionError:
            if RUST_TOOLS.fullmatch(comm):
                unreadable += 1
            continue
        except OSError:
            pass
        try:
            note(pid, comm, os.readlink(os.path.join(base, "exe")))
        except OSError:
            pass
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            fds = []
        for fd in fds:
            try:
                note(pid, comm, os.readlink(os.path.join(base, "fd", fd)))
            except OSError:
                pass
        try:
            with open(os.path.join(base, "maps")) as f:
                for line in f:
                    i = line.find("/")
                    if i > 0:
                        note(pid, comm, line[i:].rstrip("\n"))
        except OSError:
            pass
    return found, unreadable


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


def cargo_procs(uid: int | None = None) -> list[dict[str, Any]]:
    """The user's processes named cargo or cargo-* (cargo-nextest,
    cargo-clippy, ...): [{pid, comm, cwd, exe, args, env}]. cwd is None when
    it cannot be read."""
    target_uid = os.getuid() if uid is None else uid
    out: list[dict[str, Any]] = []
    for pid, comm, base in _own_processes(target_uid):
        if not CARGO_COMM.fullmatch(comm):
            continue
        p: dict[str, Any] = {"pid": pid, "comm": comm, "cwd": None, "exe": "", "args": [], "env": {}}
        try:
            p["cwd"] = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            pass
        try:
            p["exe"] = os.readlink(os.path.join(base, "exe"))
        except OSError:
            pass
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                p["args"] = [a.decode("utf-8", "replace") for a in f.read().split(b"\0")[:-1]]
            with open(os.path.join(base, "environ"), "rb") as f:
                for item in f.read().split(b"\0"):
                    k, sep, v = item.decode("utf-8", "replace").partition("=")
                    if sep and k in CARGO_ENV:
                        p["env"][k] = v
        except OSError:
            pass
        if not os.path.exists(base):
            continue  # ended meanwhile
        out.append(p)
    return out


def cargo_binary(p: Mapping[str, Any], home: str) -> str:
    """The cargo to ask: the process's own program when it is a cargo (the
    same version computes the same folder name), else the user's."""
    exe = str(p["exe"])
    if exe and not exe.endswith(" (deleted)") and os.path.basename(exe) == "cargo" and os.access(exe, os.X_OK):
        return exe
    ch = str(p["env"].get("CARGO_HOME") or os.environ.get("CARGO_HOME", ""))
    for c in (
        os.path.join(ch if os.path.isabs(ch) else os.path.join(home, ".cargo"), "bin", "cargo"),
        shutil.which("cargo"),
    ):
        if c and os.access(c, os.X_OK):
            return c
    return ""


def cargo_query(
    p: Mapping[str, Any], home: str
) -> (
    tuple[None, str]
    | tuple[
        tuple[tuple[str, ...], str, tuple[tuple[str, str], ...]],
        tuple[list[str], dict[str, str], str],
    ]
):
    """(cache key, command, env, cwd) of the `cargo metadata` that tells the
    build folder of process P; or (None, why) when it cannot be asked."""
    cwd = p["cwd"]
    if not isinstance(cwd, str) or not cwd or cwd.endswith(" (deleted)") or not os.path.isdir(cwd):
        return None, f"its working folder {cwd or '(unreadable)'} is not reachable"
    cargo = cargo_binary(p, home)
    if not cargo:
        return None, "no cargo program found to ask"
    cmd = [cargo]
    if len(p["args"]) > 1 and p["args"][1].startswith("+"):
        cmd.append(p["args"][1])  # rustup's +toolchain (the process is rustup's proxy)
    cmd += ["metadata", "--no-deps", "--offline", "--format-version", "1"]
    manifest = manifest_arg(p["args"], cwd)
    if manifest:
        cmd += ["--manifest-path", manifest]
    env = {k: v for k, v in os.environ.items() if k not in CARGO_ENV}
    env.update(p["env"])
    # Never download a toolchain or anything else; plain output.
    env.update(RUSTUP_AUTO_INSTALL="0", CARGO_NET_OFFLINE="true", CARGO_TERM_COLOR="never")
    key = (tuple(cmd), cwd, tuple(sorted(p["env"].items())))
    return key, (cmd, env, cwd)


def ask_cargo(cmd: Sequence[str], env: Mapping[str, str], cwd: str) -> tuple[str | None, str | None]:
    """(build folder or '', None) or (None, why). Read only: metadata
    --no-deps writes nothing and does not wait for the build lock."""
    r = RUNNER.run(cmd, cwd=cwd, base_env=env, timeout=60, decode_errors="strict")
    if r.cause is not None:
        return None, f"cargo metadata: {r.cause}"
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["failed"])[-1]
        return None, f"cargo metadata: {last}"
    try:
        bd = json.loads(r.stdout).get("build_directory")
    except ValueError, AttributeError:
        return None, "cargo metadata: unexpected output"
    # No build_directory: a cargo without build-dir support, so not this root.
    return (os.path.normpath(bd) if isinstance(bd, str) and bd else ""), None


def run_idle(cmd: Sequence[str], cwd: str, env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    """cmd at idle CPU and IO priority: ionice -c 3 when present, and nice 19
    in the child (the timer runs at Nice=19 anyway; a manual run should not
    slow builds either). A command that cannot start or does not finish
    within timeout raises OSError or subprocess.TimeoutExpired, as
    subprocess.run did."""
    io = shutil.which("ionice")
    full = ([io, "-c", "3"] if io else []) + list(cmd)
    r = RUNNER.run(full, cwd=cwd, base_env=env, timeout=timeout, nice=19, decode_errors="strict")
    if r.cause is not None:
        raise r.cause
    return subprocess.CompletedProcess(full, r.returncode, r.stdout, r.stderr)


def walk_dirs(
    top: str, depth_max: int, deadline: float, stop: Callable[[str, set[str]], bool] | None = None
) -> Iterator[tuple[str, set[str]]]:
    """Yield (dir, names) for TOP and the folders below it, at most DEPTH_MAX
    levels down; never follows symlinks; skips SCAN_SKIP and hidden folders;
    does not descend into a folder for which STOP(dir, names) is true (it is
    still yielded). A folder with a CACHEDIR.TAG (a cache, e.g. a cargo home
    or target/ folder) is neither yielded nor entered. Unreadable folders are skipped: a cargo or git of the
    user could not read them either."""
    stack = [(top, 0)]
    while stack:
        if time.monotonic() > deadline:
            raise Incomplete(f"the search of workspaces took longer than {ENUM_BUDGET}s")
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        names = {e.name for e in entries}
        if CACHE_TAG in names and d != top:
            continue
        yield d, names
        if depth >= depth_max or (stop and stop(d, names)):
            continue
        for e in sorted(entries, key=lambda x: x.name, reverse=True):
            if e.name.startswith(".") or e.name in SCAN_SKIP:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, depth + 1))
            except OSError:
                pass


def git_common_dir(wt: str) -> str | None:
    """The repository a worktree belongs to (its common git folder), or None
    when its .git is broken (a linked worktree whose repository is gone)."""
    g = os.path.join(wt, ".git")
    try:
        st = os.lstat(g)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return os.path.realpath(g)
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        with open(g, encoding="utf-8", errors="replace") as f:
            m = re.match(r"gitdir:\s*(.+)", f.read().strip())
    except OSError:
        return None
    if not m:
        return None
    gd = os.path.normpath(os.path.join(wt, m.group(1).strip()))
    if not os.path.isdir(gd):
        return None
    try:
        with open(os.path.join(gd, "commondir"), encoding="utf-8") as f:
            return os.path.realpath(os.path.join(gd, f.read().strip()))
    except FileNotFoundError:
        return os.path.realpath(gd)  # a submodule: its own repository
    except OSError:
        return None


def git_worktrees(wt: str, deadline: float) -> list[str]:
    """The worktree paths `git worktree list --porcelain` names for the
    repository of WT. Raises Incomplete when git fails."""
    git = shutil.which("git")
    if not git:
        raise Incomplete("git not found on PATH")
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GIT_TERMINAL_PROMPT="0")
    try:
        r = run_idle(
            [git, "-C", wt, "worktree", "list", "--porcelain"],
            wt,
            env,
            max(1, min(CMD_TIMEOUT, deadline - time.monotonic())),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Incomplete(f"git worktree list in {wt}: {e}") from None
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["failed"])[-1]
        raise Incomplete(f"git worktree list in {wt}: {last}")
    out: list[str] = []
    cur: str | None = None
    bare = False
    for line in [*r.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            cur, bare = line[len("worktree ") :], False
        elif line == "bare":
            bare = True
        elif line == "" and cur is not None:
            if not bare:
                out.append(os.path.normpath(cur))
            cur = None
    return out


def find_worktrees(home: str, deadline: float, ws: Workspaces) -> list[str]:
    """Every git worktree of the repositories under ~/Workspace (and those
    the repositories list elsewhere)."""
    top = os.path.join(home, WORKSPACE_REL)
    if not os.path.isdir(top):
        raise Incomplete(f"~/{WORKSPACE_REL} not found")
    found: list[str] = []
    for d, names in walk_dirs(top, REPO_DEPTH, deadline):
        if ".git" in names:
            found.append(d)
    ws.repos = len(found)
    roots: set[str] = set()
    groups: dict[str, str] = {}
    for d in found:
        common = git_common_dir(d)
        roots.add(os.path.realpath(d))
        if common:
            groups.setdefault(common, d)
    for _common, d in sorted(groups.items()):
        for wt in git_worktrees(d, deadline):
            if os.path.isdir(wt):  # a "prunable" one is gone: nothing to claim
                roots.add(os.path.realpath(wt))
    return sorted(roots)


def find_manifests(wt: str, deadline: float) -> tuple[list[str], list[str], list[str]]:
    """(Cargo.toml files, rust-toolchain files, files that may name a
    toolchain) in worktree WT; folders that are themselves a worktree or
    repository (they have .git) are left to their own walk."""
    manifests: list[str] = []
    pins: list[str] = []
    tools: list[str] = []

    def other_repo(d: str, names: set[str]) -> bool:
        return d != wt and ".git" in names

    for d, names in walk_dirs(wt, CRATE_DEPTH, deadline, stop=other_repo):
        if other_repo(d, names):
            continue
        if "Cargo.toml" in names:
            manifests.append(os.path.join(d, "Cargo.toml"))
        pins += [os.path.join(d, n) for n in PIN_NAMES if n in names]
        tools += [os.path.join(d, n) for n in sorted(names) if is_tool_file(n)]
    tools += [os.path.join(wt, n) for n in TOOL_FILE_TOP if os.path.isfile(os.path.join(wt, n))]
    for rel, ends in TOOL_FILE_DIRS.items():
        try:
            with os.scandir(os.path.join(wt, rel)) as it:
                tools += sorted(e.path for e in it if e.name.endswith(ends) and e.is_file(follow_symlinks=False))
        except OSError:
            pass
    return manifests, pins, tools


def metadata_env(home: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("RUSTUP_TOOLCHAIN", "CARGO_TARGET_DIR")}
    # Never download a toolchain or anything else; plain output; the home asked about.
    env.update(HOME=home, RUSTUP_AUTO_INSTALL="0", CARGO_NET_OFFLINE="true", CARGO_TERM_COLOR="never")
    return env


def user_cargo(home: str) -> str:
    ch = os.environ.get("CARGO_HOME", "")
    for c in (
        os.path.join(ch if os.path.isabs(ch) else os.path.join(home, ".cargo"), "bin", "cargo"),
        shutil.which("cargo"),
    ):
        if c and os.access(c, os.X_OK):
            return c
    return ""


def workspace_metadata(
    cargo: str, manifest: str, env: Mapping[str, str], deadline: float
) -> tuple[dict[str, Any] | None, str | None]:
    """cargo metadata of one Cargo.toml: (data, None) or (None, why)."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise Incomplete(f"the search of workspaces took longer than {ENUM_BUDGET}s")
    try:
        r = run_idle(
            [cargo, "metadata", "--offline", "--no-deps", "--format-version", "1", "--manifest-path", manifest],
            os.path.dirname(manifest),
            env,
            min(CMD_TIMEOUT, left),
        )
    except subprocess.TimeoutExpired:
        return None, f"cargo metadata took longer than {int(min(CMD_TIMEOUT, left))}s"
    except OSError as e:
        return None, f"cargo metadata: {e.strerror or e}"
    if r.returncode != 0:
        if NOT_A_MEMBER in r.stderr:
            # cargo refuses to build this package where it is (a crate under a
            # workspace that does not list it, e.g. a fuzz/ folder): it can
            # have no build folder, so it claims none.
            return {}, None
        last = (r.stderr.strip().splitlines() or ["failed"])[-1]
        return None, f"cargo metadata: {last}"
    try:
        data = json.loads(r.stdout)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return None, "cargo metadata: unexpected output"
    return data, None


def time_left(deadline: float | None) -> float | None:
    """Seconds until DEADLINE (time.monotonic()), or None without one."""
    return None if deadline is None else deadline - time.monotonic()


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
        decode_errors="strict",
    )
    if r.fault == "timeout":
        secs = int(effective_timeout) if effective_timeout is not None else 0
        return False, f"stopped after {secs}s (the run's time budget); the next run deletes the rest"
    if r.cause is not None:
        raise r.cause
    return r.returncode == 0, r.stdout.strip()


def state_path(home: str, tid: str) -> str:
    return os.path.join(home, STATE_DIR_REL, f"toolchains-{tid.replace(':', '-')}.json")


def write_state(home: str, tid: str, data: Mapping[str, Any]) -> None:
    path = state_path(home, tid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 0600: what the temporary file it used to rename into place had.
        write_atomic(path, json.dumps(data), mode=0o600, durable=False, relabel=False)
    except OSError as e:
        say(f"{tid}: could not save the run summary: {e}", 4)


def read_state(home: str, tid: str) -> dict[str, Any] | None:
    try:
        with open(state_path(home, tid)) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except OSError, ValueError:
        return None


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


def uv_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("UV_CACHE_DIR", "UV_NO_CACHE", "UV_LOCK_TIMEOUT")}
    env.update(UV_LOCK_TIMEOUT=str(UV_LOCK_WAIT), NO_COLOR="1")
    return env


def uv_lock_busy(cache: str) -> bool:
    """True while another uv holds its cache lock (<cache>/.lock: every uv
    command holds it shared; `uv cache prune` needs it alone). The test lock
    is let go at once."""
    try:
        fd = os.open(os.path.join(cache, ".lock"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def uv_prune_preview(
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]], cache: str
) -> tuple[list[str], bool]:
    """(lines, ok) of `uv cache prune --dry-run` on cache (run: uv with these
    arguments). A uv that fails, cannot start or does not end is a failure
    line, and plan exits 1; waiting in vain for the cache lock another uv
    holds is not a failure (apply skips that case too)."""
    try:
        r = run(["cache", "prune", "--dry-run", "--cache-dir", cache, "--no-progress"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"uv cache prune --dry-run failed: {e}"], False
    text = (r.stdout + r.stderr).strip()
    lines = [f"uv: {line}" for line in text.splitlines()]
    if r.returncode == 0:
        return lines, True
    if UV_LOCK_TIMED_OUT.search(text) or uv_lock_busy(cache):
        lines.append("uv cache prune --dry-run waited for the cache lock (another uv runs): no preview this time")
        return lines, True
    return lines + [f"uv cache prune --dry-run failed (exit {r.returncode})"], False


def rustup_home(home: str) -> str:
    rh = os.environ.get("RUSTUP_HOME", "")
    return rh if os.path.isabs(rh) else os.path.join(home, ".rustup")


def pinned_channel(path: str) -> str:
    """The toolchain a rust-toolchain(.toml) file pins, or ''."""
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    if path.endswith(".toml") or "[toolchain]" in text:
        try:
            tc = tomllib.loads(text).get("toolchain")
        except tomllib.TOMLDecodeError:
            return ""
        ch = tc.get("channel") if isinstance(tc, dict) else None
        return ch.strip() if isinstance(ch, str) else ""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    return lines[0] if len(lines) == 1 else ""


def rustup_default(home: str, rh: str) -> str:
    """The default toolchain's name, or ''. settings.toml first; else asks
    rustup (read only)."""
    try:
        with open(os.path.join(rh, "settings.toml"), "rb") as f:
            d = tomllib.load(f).get("default_toolchain")
        if isinstance(d, str) and d:
            return d
    except OSError, tomllib.TOMLDecodeError:
        pass
    rustup = shutil.which("rustup")
    if not rustup:
        return ""
    env = dict(os.environ, RUSTUP_HOME=rh, RUSTUP_AUTO_INSTALL="0", HOME=home)
    env.pop("RUSTUP_TOOLCHAIN", None)
    r = RUNNER.run([rustup, "default"], cwd=home, base_env=env, timeout=20, decode_errors="strict")
    if r.cause is not None:
        return ""
    words = r.stdout.split() if r.returncode == 0 else []
    return words[0] if words else ""


def sccache_stats(exe: str, env: Mapping[str, str]) -> dict[str, str]:
    """The fields of `EXE --show-stats` in ENV (stderr dropped, within 5 s);
    {} when it did not run or failed. Output that is not UTF-8 raises."""
    r = RUNNER.run([exe, "--show-stats"], base_env=env, timeout=5, output="stdout", decode_errors="strict")
    return dict(SCCACHE_STATS.findall(r.stdout)) if r.cause is None and r.returncode == 0 else {}


def named_in(files: Sequence[str], names: Sequence[str]) -> tuple[dict[str, str], bool]:
    """({name: first file that names it}, complete) for NAMES, read from
    FILES (TOOL_FILE_MAX bytes each; unreadable ones skipped). COMPLETE is
    False when there were more than TOOL_FILES_MAX files to read."""
    pats = {n: (n.encode(), toolchain_pattern(n)) for n in names}
    found: dict[str, str] = {}
    seen: set[str] = set()
    for path in files:
        if len(found) == len(pats):
            break
        if path in seen:
            continue
        if len(seen) >= TOOL_FILES_MAX:
            return found, False
        seen.add(path)
        try:
            with open(path, "rb") as f:
                data = f.read(TOOL_FILE_MAX)
        except OSError:
            continue
        for n, (raw, pat) in pats.items():
            # The plain substring test is fast; the word test only where it hits.
            if n not in found and raw in data and pat.search(data):
                found[n] = path
    return found, True


def in_container() -> bool:
    return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")


def run_context(home: str | None) -> tuple[str, float]:
    """(the home folder to work on: --home or the user's, now: CST_TOOLCHAINS_NOW in tests)."""
    return (
        os.path.abspath(pwd.getpwuid(os.getuid()).pw_dir if home is None else home),
        float(os.environ.get("CST_TOOLCHAINS_NOW") or time.time()),
    )


def output_mode(cmd: str, action: str | None) -> None:
    """A command that only reads (help, status, prune plan, config plan) gets
    SIGPIPE back at its default, so `cst toolchains status | head` ends
    quietly. The ones that change files (prune, auto, config apply) keep
    Python's handling: a closed pipe never cuts their lock release or file
    writes short."""
    if cmd in ("help", "status") or action == "plan":
        restore_default_signals()


def systemctl_cmd(*args: str) -> list[str]:
    # Tests point this at a stub: no test may reach the real user manager.
    stub = os.environ.get("CST_TOOLCHAINS_SYSTEMCTL")
    if stub:
        return [stub, "--user", *args]
    cmd = ["systemctl", "--user", *args]
    return ["flatpak-spawn", "--host", *cmd] if in_container() else cmd


def systemctl_env() -> dict[str, str]:
    env = dict(os.environ)
    # Run by root through runuser (snapshots setup): no session variables.
    rt = f"/run/user/{os.getuid()}"
    if not env.get("XDG_RUNTIME_DIR") and os.path.isdir(rt):
        env["XDG_RUNTIME_DIR"] = rt
    return env


def systemctl(*args: str, check: bool = False, ok: Sequence[int] = (0,)) -> str:
    """Output (stripped) of systemctl --user; '' when it could not run.
    check: Refused when it could not run or exited with a code not in ok."""
    r = RUNNER.run(systemctl_cmd(*args), base_env=systemctl_env(), timeout=60, stdin="inherit", decode_errors="strict")
    if r.cause is not None:
        if check:
            raise Refused(f"systemctl --user {' '.join(args)}: {r.cause}") from None
        return ""
    if check and r.returncode not in ok:
        raise Refused(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def before_removal(name: str, *args: str, ok: Sequence[int] = (0,)) -> str:
    """systemctl --user ARGS NAME for a timer, checked, before auto on/off
    removes any file. When the user manager refuses or cannot be reached,
    nothing has been removed yet: Refused, and a second run (once the
    manager answers) can finish the job. Removing the files anyway would
    leave a timer that keeps running with no file left to show it."""
    try:
        return systemctl(*args, name, check=True, ok=ok)
    except Refused as e:
        raise Refused(
            f"{e}; nothing was removed and {name} may still be running: run this again once your user "
            "manager answers (`systemctl --user status` shows whether it does)"
        ) from None


def reload_failed() -> bool:
    """daemon-reload after unit files were removed: True (and an error line)
    when it failed. The removal is not undone: the timer is already stopped,
    and the manager forgets the files at its next reload or restart."""
    try:
        systemctl("daemon-reload", check=True)
    except Refused as e:
        say(
            f"{NAME}: {e}; the files are removed anyway: run `systemctl --user daemon-reload` once your user "
            "manager answers, so that it forgets them",
            3,
            err=True,
        )
        return True
    return False


def disable_timer(name: str) -> None:
    """`disable --now` a timer, checked (see before_removal), then forget its
    last run: systemd keeps the stamp of a Persistent= timer
    (~/.local/share/systemd/timers/stamp-NAME) after the timer is gone.
    `clean` needs the timer loaded and stopped, so this runs before its file
    is removed; a failed clean only leaves that empty file behind."""
    before_removal(name, "disable", "--now")
    systemctl("clean", "--what=state", name)


def manager_path() -> str | None:
    """PATH of the user manager (what the service runs with), or None when
    it cannot be read."""
    for line in systemctl("show-environment").splitlines():
        if line.startswith("PATH="):
            val = line[len("PATH=") :]
            # systemd quotes a value with special characters ($'...'): not parsed here.
            return None if val[:1] in ("$", "'", '"') else val
    return None


def file_state(path: str, wanted: str | bytes | None, marker: str = MARKER) -> OwnedState:
    """ok | change (ours: it holds MARKER, and differs) | conflict (not ours) | missing.

    Every file here is in the user's home: a symbolic link is judged by what
    it points to (cst_core.domain.install.link_read_through), and a write
    replaces the link itself."""
    want = wanted.encode() if isinstance(wanted, str) else wanted
    return owned_file_state_at(path, want, lambda data: marker.encode() in data, owner="user")


def timer_unit_state(path: str, wanted: str) -> UnitFileState:
    """file_state of a unit file `auto on` writes: also "masked" when
    `systemctl --user mask` left a link to /dev/null there (see
    cst_core.adapters.files.unit_file_state_at)."""
    return unit_file_state_at(path, wanted.encode(), lambda data: MARKER.encode() in data, owner="user")


def old_conf_note(home: str) -> None:
    if os.path.lexists(os.path.join(home, OLD_CONF_REL)):
        say(
            f"note: ~/{OLD_CONF_REL} is no longer read: put its settings into ~/{CONF_REL} "
            "with the RUST_BUILD_ prefix (e.g. RUST_BUILD_MAX_IDLE=48h), then remove it",
            4,
            err=True,
        )


def turned_off(home: str) -> bool:
    return os.path.lexists(os.path.join(home, OFF_REL))


def read_cfg(path: str) -> str | None:
    """Text of a config file; '' when missing; None when it cannot be read."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def wrapper_path(home: str) -> str:
    return os.path.join(home, WRAPPER_REL)


def write_cfg(path: str, text: str) -> str:
    """Replace a config file's text (atomic, same mode). '' or the error."""
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except OSError:
        mode = 0o644
    try:
        write_atomic(real, text, mode=mode)
    except OSError as e:
        return str(e.strerror or e)
    return ""


def sccache_sockets(home: str) -> list[str]:
    """The per-environment server sockets the wrapper names (all
    environments share $XDG_RUNTIME_DIR, or the fallback folder)."""
    dirs: list[str] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and os.path.isdir(xdg):
        dirs.append(xdg)
    dirs.append(os.path.join(home, SCCACHE_DIR_REL, "run"))
    out: list[str] = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        out += [os.path.join(d, n) for n in names if n.startswith("cst-sccache-") and n.endswith(".sock")]
    return out


def socket_answers(path: str) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def append_text(path: str, text: str) -> str:
    """Append TEXT to PATH (made if missing); a file not ending in a newline
    gets one first. Returns '' or the error."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            # A file not ending in a newline would glue the comment onto its last line.
            if f.tell() > 0:
                with open(path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        f.write(b"\n")
            f.write(text.encode("utf-8"))
    except OSError as e:
        return str(e.strerror or e)
    return ""
