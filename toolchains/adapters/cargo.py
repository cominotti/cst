# SPDX-License-Identifier: MPL-2.0
"""cargo and git, read only: which build folder a cargo process uses, and
which build folders the workspaces under ~/Workspace still claim.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import time
import tomllib
from typing import TYPE_CHECKING, Final, TypedDict, TypeIs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from toolchains.domain.prune import Limits

from cst_core.domain.shapes import json_object, parse_json, parse_toml
from cst_core.domain.values import Json
from toolchains.domain.build import (
    CACHE_TAG,
    CARGO_ENV,
    CARGO_MANIFEST,
    CMD_TIMEOUT,
    CRATE_DEPTH,
    CargoCache,
    CargoProc,
    CargoQuery,
    ENUM_BUDGET,
    HAKARI_MIN_MEMBERS,
    Incomplete,
    NOT_A_MEMBER,
    PIN_NAMES,
    REPO_DEPTH,
    SCAN_SKIP,
    TOOL_FILE_DIRS,
    TOOL_FILE_TOP,
    WORKSPACE_REL,
    Workspaces,
    folder_key,
    is_tool_file,
    manifest_arg,
    prefixes,
)
from toolchains.domain.common import json_items, tilde
from toolchains.adapters.program import RUNNER, run_idle


class CargoMetadata(TypedDict, total=False):
    """What claimed_folders reads of `cargo metadata --format-version 1` (any JSON
    object; the values are as cargo gave them, each checked where it is used)."""

    packages: Json
    workspace_root: Json
    build_directory: Json


def is_cargo_metadata(data: object) -> TypeIs[CargoMetadata]:
    """DATA (what json.loads gave) is cargo's answer: a JSON object; its keys are not checked."""
    return isinstance(data, dict)


CARGO_PROGRAM: Final = "cargo"  # the file name of cargo's program (not a ConfigTool)
# Why cargo metadata could not be used: its output is not the JSON object it writes.
METADATA_UNEXPECTED: Final = "cargo metadata: unexpected output"


def cargo_binary(p: CargoProc, home: str) -> str:
    """The cargo to ask: the process's own program when it is a cargo (the
    same version computes the same folder name), else the user's."""
    exe = str(p.exe)
    if exe and not exe.endswith(" (deleted)") and os.path.basename(exe) == CARGO_PROGRAM and os.access(exe, os.X_OK):
        return exe
    ch = str(p.env.get("CARGO_HOME") or os.environ.get("CARGO_HOME", ""))
    for c in (
        os.path.join(ch if os.path.isabs(ch) else os.path.join(home, ".cargo"), "bin", "cargo"),
        shutil.which("cargo"),
    ):
        if c is not None and os.access(c, os.X_OK):
            return c
    return ""


def cargo_query(p: CargoProc, home: str) -> CargoQuery | str:
    """The `cargo metadata` that tells the build folder of process P (with
    its cache key, command, env and cwd); or why it cannot be asked."""
    if not p.read:
        return "its command line or environment in /proc came back empty or could not be read: as while it starts or ends, or when it was started with no environment at all"
    cwd = p.cwd
    if not isinstance(cwd, str) or not cwd or cwd.endswith(" (deleted)") or not os.path.isdir(cwd):
        return f"its working folder {cwd or '(unreadable)'} is not reachable"
    cargo = cargo_binary(p, home)
    if not cargo:
        return "no cargo program found to ask"
    cmd = [cargo]
    if len(p.args) > 1 and p.args[1].startswith("+"):
        cmd.append(p.args[1])  # rustup's +toolchain (the process is rustup's proxy)
    cmd += ["metadata", "--no-deps", "--offline", "--format-version", "1"]
    manifest = manifest_arg(p.args, cwd)
    if manifest:
        cmd += ["--manifest-path", manifest]
    env = {k: v for k, v in os.environ.items() if k not in CARGO_ENV}
    env.update(p.env)
    # Never download a toolchain or anything else; plain output.
    env.update(RUSTUP_AUTO_INSTALL="0", CARGO_NET_OFFLINE="true", CARGO_TERM_COLOR="never")
    key = (tuple(cmd), cwd, tuple(sorted(p.env.items())))
    return CargoQuery(key, tuple(cmd), env, cwd)


def ask_cargo(cmd: Sequence[str], env: Mapping[str, str], cwd: str) -> tuple[str | None, str | None]:
    """(build folder or '', None) or (None, why). Read only: metadata
    --no-deps writes nothing and does not wait for the build lock."""
    r = RUNNER.run(cmd, cwd=cwd, base_env=env, timeout=CMD_TIMEOUT)
    if r.cause is not None:
        return None, f"cargo metadata: {r.cause}"
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["failed"])[-1]
        return None, f"cargo metadata: {last}"
    try:
        data = parse_json(r.stdout)
    except ValueError:
        return None, METADATA_UNEXPECTED
    if not json_object(data):  # no .get: it raised AttributeError
        return None, METADATA_UNEXPECTED
    bd = data.get("build_directory")
    # No build_directory: a cargo without build-dir support, so not this root.
    return (os.path.normpath(bd) if isinstance(bd, str) and bd else ""), None


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
        entries = _dir_listing_or_none(d)
        if entries is None:
            continue
        names = {e.name for e in entries}
        if CACHE_TAG in names and d != top:
            continue
        yield d, names
        if depth >= depth_max or (stop and stop(d, names)):
            continue
        stack.extend((p, depth + 1) for p in _walked_into(entries))


def _dir_listing_or_none(d: str) -> list[os.DirEntry[str]] | None:
    """The entries of folder D, or None when it cannot be read."""
    try:
        with os.scandir(d) as it:
            return list(it)
    except OSError:
        return None


def _walked_into(entries: Sequence[os.DirEntry[str]]) -> list[str]:
    """The folders of ENTRIES walk_dirs goes into, the last name first (the
    stack takes the first name first): not hidden, not in SCAN_SKIP, never a
    symlink; an entry whose type cannot be read is left out."""
    out: list[str] = []
    for e in sorted(entries, key=lambda x: x.name, reverse=True):
        if e.name.startswith(".") or e.name in SCAN_SKIP:
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                out.append(e.path)
        except OSError:
            pass
    return out


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


def git_worktrees(wt: str, deadline: float, cmd_timeout: float) -> list[str]:
    """The worktree paths `git worktree list --porcelain` names for the
    repository of WT (CMD_TIMEOUT: the most seconds git may take). Raises
    Incomplete when git fails."""
    git = shutil.which("git")
    if git is None:
        raise Incomplete("git not found on PATH")
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GIT_TERMINAL_PROMPT="0")
    try:
        r = run_idle(
            [git, "-C", wt, "worktree", "list", "--porcelain"],
            wt,
            env,
            max(1, min(cmd_timeout, deadline - time.monotonic())),
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


def find_worktrees(home: str, deadline: float, ws: Workspaces, cmd_timeout: float) -> list[str]:
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
        if common is not None and common != "":
            groups.setdefault(common, d)
    for _common, d in sorted(groups.items()):
        for wt in git_worktrees(d, deadline, cmd_timeout):
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
        if CARGO_MANIFEST in names:
            manifests.append(os.path.join(d, CARGO_MANIFEST))
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
        if c is not None and os.access(c, os.X_OK):
            return c
    return ""


def workspace_metadata(
    cargo: str, manifest: str, env: Mapping[str, str], deadline: float, cmd_timeout: float
) -> tuple[CargoMetadata | None, str | None]:
    """cargo metadata of one Cargo.toml: (data, None) or (None, why)."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise Incomplete(f"the search of workspaces took longer than {ENUM_BUDGET}s")
    try:
        r = run_idle(
            [cargo, "metadata", "--offline", "--no-deps", "--format-version", "1", "--manifest-path", manifest],
            os.path.dirname(manifest),
            env,
            min(cmd_timeout, left),
        )
    except subprocess.TimeoutExpired:
        return None, f"cargo metadata took longer than {int(min(cmd_timeout, left))}s"
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
        data = parse_json(r.stdout)
        if not is_cargo_metadata(data):
            raise ValueError
    except ValueError:
        return None, METADATA_UNEXPECTED
    return data, None


def cargo_users(
    root: str,
    home: str,
    cache: CargoCache,
    procs: Sequence[CargoProc],
) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str, str | None, str]]]:
    """({folder key: [(pid, comm)]}, [(pid, comm, cwd, why)]): the folders
    the running cargo processes PROCS build in, and the cargo processes whose
    folder could not be found out. CACHE (a dict) keeps the answers for one run."""
    pre = prefixes(root)
    found: dict[str, list[tuple[str, str]]] = {}
    unknown: list[tuple[str, str, str | None, str]] = []
    for p in procs:
        q = cargo_query(p, home)
        if isinstance(q, str):
            unknown.append((p.pid, p.comm, p.cwd, q))
            continue
        if q.key not in cache:
            cache[q.key] = ask_cargo(q.cmd, q.env, q.cwd)
        bd, why = cache[q.key]
        if why is not None or bd is None:
            unknown.append((p.pid, p.comm, p.cwd, why or "unknown"))
            continue
        k = folder_key(pre, bd)
        if k is not None:
            lst = found.setdefault(k, [])
            if (p.pid, p.comm) not in lst:
                lst.append((p.pid, p.comm))
    return found, unknown


@dataclasses.dataclass(frozen=True, slots=True)
class _MetadataSearch:
    """What each cargo metadata call of one claimed_folders run takes: the
    cargo to ask, its environment, the run's deadline, the seconds one call
    may take, and the root's paths (PRE) the build folders are keyed under."""

    cargo: str
    env: Mapping[str, str]
    deadline: float
    cmd_timeout: float
    pre: tuple[str, ...]


def _ask_manifests(search: _MetadataSearch, ws: Workspaces, manifests: Sequence[str]) -> None:
    """Ask cargo metadata about the MANIFESTS of one worktree, into WS (asked,
    failures, claimed). The shallowest first: a workspace's root answers for
    its members."""
    covered: set[str] = set()
    for m in sorted(manifests, key=lambda p: (p.count("/"), p)):
        if m in covered:
            continue
        data, why = workspace_metadata(search.cargo, m, search.env, search.deadline, search.cmd_timeout)
        ws.asked += 1
        if why is not None or data is None:
            ws.failures.append((m, why or "unknown"))
            continue
        covered.add(m)
        covered |= _manifests_answered(data)
        _claim_build_folder(ws, search.pre, data, m)


def _manifests_answered(data: CargoMetadata) -> set[str]:
    """The Cargo.toml files cargo's answer DATA speaks for: its packages' and
    its workspace root's."""
    out: set[str] = set()
    for p in json_items(data.get("packages")):
        mp = p.get("manifest_path") if isinstance(p, dict) else None
        if isinstance(mp, str):
            out.add(os.path.normpath(mp))
    wr = data.get("workspace_root")
    if isinstance(wr, str):
        out.add(os.path.join(os.path.normpath(wr), CARGO_MANIFEST))
    return out


def _claim_build_folder(ws: Workspaces, pre: Sequence[str], data: CargoMetadata, m: str) -> None:
    """The build folder of cargo's answer DATA (for Cargo.toml M), when it is
    under the root (paths PRE), is claimed by its workspace in WS."""
    bd = data.get("build_directory")
    if not isinstance(bd, str) or not bd:
        return  # a cargo without build-dir support: not in this root
    key = folder_key(pre, os.path.normpath(bd))
    if key is None:
        return
    wr = data.get("workspace_root")
    lst = ws.claimed.setdefault(key, [])
    shown = wr if isinstance(wr, str) else os.path.dirname(m)
    if shown not in lst:
        lst.append(shown)


def claimed_folders(root: str | None, home: str, limits: Limits, *, with_cargo: bool = True) -> Workspaces:
    """Workspaces: which folders of ROOT a workspace on disk still claims.
    WITH_CARGO=False only lists worktrees and rust-toolchain files (status).
    LIMITS: the seconds one git or cargo call may take."""
    ws = Workspaces()
    start = time.monotonic()
    deadline = start + ENUM_BUDGET
    pre = prefixes(root) if root is not None and root != "" else ()
    try:
        ws.worktrees = find_worktrees(home, deadline, ws, limits.cmd_timeout)
        cargo = user_cargo(home) if with_cargo else ""
        if with_cargo and not cargo:
            raise Incomplete("no cargo program found to ask")
        search = _MetadataSearch(cargo, metadata_env(home), deadline, limits.cmd_timeout, pre)
        for wt in ws.worktrees:
            manifests, pins, tools = find_manifests(wt, deadline)
            ws.pins += pins
            ws.tool_files += tools
            ws.manifests += len(manifests)
            ws.manifest_paths += manifests
            if with_cargo:
                _ask_manifests(search, ws, manifests)
        if ws.failures:
            m, why = ws.failures[0]
            more = f" (and {len(ws.failures) - 1} more)" if len(ws.failures) > 1 else ""
            raise Incomplete(f"{tilde(m, home)}: {why}{more}")
    except Incomplete as e:
        ws.incomplete = str(e)
    ws.seconds = time.monotonic() - start
    return ws


def _canonical_worktrees(worktrees: Sequence[str], home: str) -> list[str]:
    """One representative worktree per git repository (preferring main checkouts under HOME)."""
    ordered = sorted(
        worktrees,
        key=lambda w: (0 if w == home or w.startswith(home + "/") else 1, w.count("/"), len(w), w),
    )
    seen_repos: set[str] = set()
    kept: list[str] = []
    for wt in ordered:
        key = git_common_dir(wt) or os.path.realpath(wt)
        if key not in seen_repos:
            seen_repos.add(key)
            kept.append(wt)
    return kept


def _step_glob(dirs: Sequence[str], seg: str) -> list[str]:
    """Advance DIRS one path segment matching SEG without following symlinks."""
    out: list[str] = []
    for d in dirs:
        if seg == "**":
            out.append(d)
            for root_dir, subdirs, _files in os.walk(d, followlinks=False):
                subdirs[:] = sorted(s for s in subdirs if not s.startswith(".") and s not in SCAN_SKIP)
                if root_dir.removeprefix(d).count("/") >= CRATE_DEPTH:
                    subdirs.clear()
                    continue
                out.extend(os.path.join(root_dir, s) for s in subdirs)
            continue
        entries = _dir_listing_or_none(d) or ()
        for e in sorted(entries, key=lambda x: x.name):
            try:
                if e.is_dir(follow_symlinks=False) and fnmatch.fnmatchcase(e.name, seg):
                    out.append(e.path)
            except OSError:
                continue
    return out


def _expand_member_globs(root: str, patterns: Sequence[object]) -> set[str]:
    """Real paths of subdirectories under ROOT matching PATTERNS that contain Cargo.toml."""
    matched: set[str] = set()
    real_root = os.path.realpath(root)
    for raw in patterns:
        if not isinstance(raw, str) or not raw or raw.startswith("/"):
            continue
        parts = [p for p in raw.strip("/").split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            continue
        cur = [root]
        for seg in parts:
            cur = _step_glob(cur, seg)
            if not cur:
                break
        for cand in cur:
            if os.path.isfile(os.path.join(cand, CARGO_MANIFEST)):
                real = os.path.realpath(cand)
                if real == real_root or real.startswith(real_root + "/"):
                    matched.add(real)
    return matched


def _workspace_member_count(manifest: str) -> int | None:
    """Member count of a root Cargo.toml without hakari config, or None when not applicable."""
    root = os.path.dirname(manifest)
    if any(os.path.exists(os.path.join(root, rel)) for rel in (".config/hakari.toml", "hakari.toml")):
        return None
    try:
        with open(manifest, "rb") as f:
            data = parse_toml(f.read())
    except OSError, tomllib.TOMLDecodeError:
        return None
    ws_table = data.get("workspace") if json_object(data) else None
    members = ws_table.get("members") if json_object(ws_table) else None
    if not isinstance(members, list):
        return None
    exclude = ws_table.get("exclude") if json_object(ws_table) else None
    found = _expand_member_globs(root, members)
    if isinstance(exclude, list):
        found -= _expand_member_globs(root, exclude)
    return len(found) if len(found) >= HAKARI_MIN_MEMBERS else None


def hakari_candidates(ws: Workspaces, home: str) -> list[tuple[str, int]]:
    """[(workspace_root, member_count)] for large workspaces lacking .config/hakari.toml."""
    kept = set(_canonical_worktrees(ws.worktrees, home))
    all_wts = sorted(ws.worktrees, key=len, reverse=True)
    seen_roots: set[str] = set()
    out: list[tuple[str, int]] = []
    for m in sorted(ws.manifest_paths):
        owner = next((wt for wt in all_wts if m == f"{wt}/{CARGO_MANIFEST}" or m.startswith(f"{wt}/")), None)
        if owner is not None and owner not in kept:
            continue
        root = os.path.realpath(os.path.dirname(m))
        if root in seen_roots:
            continue
        seen_roots.add(root)
        count = _workspace_member_count(m)
        if count is not None:
            out.append((os.path.dirname(m), count))
    return sorted(out, key=lambda item: (-item[1], item[0]))
