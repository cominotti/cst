# SPDX-License-Identifier: MPL-2.0
"""The commands of cst-toolchains (`cst toolchains ...`): the settings,
the targets that prune (rust:build, python:uv), status, auto on|off (the
user timer and the copies it runs), config, and main(). The program
toolchains/cst-toolchains only hands its arguments to main().
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

from cst_core.adapters.files import read_bytes, read_text, write_atomic
from cst_core.adapters.runtime import install_payload, program_argv
from cst_core.domain.errors import Refused, Usage
from cst_core.domain.files import UnitFileState
from cst_core.domain.units import masked_refusal
from toolchains.adapters import (
    PROG,
    where_text,
    Program,
    append_text,
    ask_cargo,
    before_removal,
    cargo_procs,
    cargo_query,
    disable_timer,
    file_state,
    old_conf_note,
    find_manifests,
    find_worktrees,
    in_container,
    output_mode,
    manager_path,
    measure,
    metadata_env,
    named_in,
    pinned_channel,
    proc_users,
    read_cfg,
    read_state,
    reload_failed,
    release,
    remove_tree,
    resolve_root,
    run_context,
    run_idle,
    rustup_default,
    rustup_home,
    say,
    scan,
    sccache_sockets,
    socket_answers,
    state_path,
    systemctl,
    take_locks,
    time_left,
    tree_size,
    turned_off,
    timer_unit_state,
    user_cargo,
    uv_env,
    uv_lock_busy,
    uv_prune_preview,
    workspace_metadata,
    wrapper_path,
    write_cfg,
    write_state,
)
from toolchains.domain import (
    ANY_HEADER,
    BUILD_HEADER,
    CARGO_CFG_REL,
    CARGO_HAS_BUILD,
    CARGO_HAS_BUILD_DIR,
    CONF_REL,
    DEFAULTS_NAME,
    DEFAULTS_REL,
    ENUM_BUDGET,
    HASH_VAR,
    KEY_LINE,
    LEAN_HEAD,
    LEAN_KEYS,
    LEAN_REBUILD,
    LIB_REL,
    MARKER,
    MIN_KEEP_FLOOR,
    NAME,
    OFF_REL,
    OLD_DATA,
    OLD_FILES,
    OLD_MARKER,
    OLD_TIMER,
    OLD_UNITS,
    OPTIONS,
    PLANNED,
    PROG_REL,
    RM_MARGIN,
    ROOT_REL,
    RUN_BUDGET,
    SCCACHE_DIR_REL,
    SCCACHE_HEAD,
    SCCACHE_REBUILD,
    SCCACHE_SIZE_LINE,
    SCCACHE_STATS,
    SCCACHE_WORKSPACE_LINE,
    SERVICE,
    STATE_DIR_REL,
    TIMER,
    TOOL_FILES_MAX,
    TRASH_PREFIX,
    UNIT_DIR_REL,
    USAGE,
    UV_CACHE_REL,
    UV_CFG_REL,
    UV_HAS_LINK_MODE,
    UV_HAS_TABLE,
    UV_LOCK_TIMED_OUT,
    UV_LOCK_WAIT,
    UV_REMOVED,
    UV_TEXT,
    UV_TIMEOUT,
    UV_UNITS,
    WANTS_REL,
    WORKSPACE_REL,
    WRAPPER_NAME,
    WRAPPER_REL,
    WS,
    ConfigError,
    Folder,
    Incomplete,
    LeanState,
    SccacheState,
    Workspaces,
    WrapperValues,
    after_total,
    ago,
    cargo_text,
    decide,
    dig,
    expected_template,
    gib,
    label,
    merge_users,
    parse_args,
    parse_bool,
    parse_conf,
    parse_duration,
    parse_off_or_duration,
    parse_size,
    parses_as,
    plain,
    prefixes,
    render_service,
    render_timer,
    same_debug,
    short_toolchain,
    span,
    tilde,
    unknown_text,
    users_text,
    when,
)


def known_keys() -> set[str]:
    keys = set(OPTIONS)
    for cls in TARGETS.values():
        keys.add(f"{cls.PREFIX}_ENABLED")
        keys.update(f"{cls.PREFIX}_{k}" for k in cls.SETTINGS)
    return keys


def defaults_path() -> str:
    """toolchains.conf next to this program: toolchains/toolchains.conf in
    CST, ~/.local/lib/cst/toolchains.conf for the timer's copy."""
    return os.path.join(Program.folder(), DEFAULTS_NAME)


class Settings:
    """The checked settings of every target: defaults, then the user's file.
    Raises ConfigError on any bad line or value (nothing runs then)."""

    def __init__(self, home: str) -> None:
        known = known_keys()
        path = defaults_path()
        try:
            with open(path, encoding="utf-8") as f:
                defaults = parse_conf(f.read(), known)
        except FileNotFoundError:
            raise ConfigError(f"the defaults file {path} is missing") from None
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}") from None
        lacking = sorted(known - set(defaults))
        if lacking:
            raise ConfigError(f"{path} lacks {', '.join(lacking)}")
        self.user_path: str = os.path.join(home, CONF_REL)
        self.source: str = tilde(self.user_path, home)
        try:
            with open(self.user_path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        try:
            user = parse_conf(text, known)
        except ConfigError as e:
            raise ConfigError(f"{self.source}: {e}") from None
        self.user_keys: set[str] = set(user)
        self.raw: dict[str, str] = {**defaults, **user}
        self.enabled: dict[str, bool] = {}
        self.policy: dict[str, dict[str, Any]] = {}
        self.options: dict[str, Any] = {}
        for key, parse in OPTIONS.items():
            try:
                self.options[key] = parse(self.raw[key])
            except ConfigError as e:
                raise ConfigError(f"{self.where(key)}: {key}: {e}") from None
        for tid, cls in TARGETS.items():
            key = f"{cls.PREFIX}_ENABLED"
            try:
                self.enabled[tid] = parse_bool(self.raw[key])
            except ConfigError as e:
                raise ConfigError(f"{self.where(key)}: {key}: {e}") from None
            vals = {k: self.raw[f"{cls.PREFIX}_{k}"] for k in cls.SETTINGS}
            try:
                self.policy[tid] = cls.make_policy(vals, cls.PREFIX)
            except ConfigError as e:
                raise ConfigError(f"{self.where(*(f'{cls.PREFIX}_{k}' for k in cls.SETTINGS))}: {e}") from None

    def where(self, *keys: str) -> str:
        return self.source if any(k in self.user_keys for k in keys) else "defaults"

    def source_of(self, cls: type[Target]) -> str:
        keys = [f"{cls.PREFIX}_{k}" for k in ("ENABLED", *cls.SETTINGS)]
        return self.where(*keys)


def cargo_users(
    root: str,
    home: str,
    cache: dict[tuple[Any, ...], tuple[str | None, str | None]],
) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str, str | None, str]]]:
    """({folder key: [(pid, comm)]}, [(pid, comm, cwd, why)]): the folders
    running cargo processes build in, and the cargo processes whose folder
    could not be found out. CACHE (a dict) keeps the answers for one run."""
    pre = prefixes(root)
    found: dict[str, list[tuple[str, str]]] = {}
    unknown: list[tuple[str, str, str | None, str]] = []
    for p in cargo_procs():
        key, q = cargo_query(p, home)
        if key is None or isinstance(q, str):
            unknown.append((p["pid"], p["comm"], p["cwd"], cast(str, q)))
            continue
        if key not in cache:
            cache[key] = ask_cargo(*q)
        bd, why = cache[key]
        if why or bd is None:
            unknown.append((p["pid"], p["comm"], p["cwd"], why or "unknown"))
            continue
        for pr in pre:
            if bd.startswith(pr + "/"):
                k = "/".join(bd[len(pr) + 1 :].split("/")[:2])
                lst = found.setdefault(k, [])
                if (p["pid"], p["comm"]) not in lst:
                    lst.append((p["pid"], p["comm"]))
                break
    return found, unknown


def claimed_folders(root: str | None, home: str, with_cargo: bool = True) -> Workspaces:
    """Workspaces: which folders of ROOT a workspace on disk still claims.
    WITH_CARGO=False only lists worktrees and rust-toolchain files (status)."""
    ws = Workspaces()
    start = time.monotonic()
    deadline = start + ENUM_BUDGET
    pre = prefixes(root) if root else ()
    try:
        ws.worktrees = find_worktrees(home, deadline, ws)
        cargo = user_cargo(home) if with_cargo else ""
        if with_cargo and not cargo:
            raise Incomplete("no cargo program found to ask")
        env = metadata_env(home)
        for wt in ws.worktrees:
            manifests, pins, tools = find_manifests(wt, deadline)
            ws.pins += pins
            ws.tool_files += tools
            ws.manifests += len(manifests)
            if not with_cargo:
                continue
            covered: set[str] = set()
            # The shallowest first: a workspace's root answers for its members.
            for m in sorted(manifests, key=lambda p: (p.count("/"), p)):
                if m in covered:
                    continue
                data, why = workspace_metadata(cargo, m, env, deadline)
                ws.asked += 1
                if why or data is None:
                    ws.failures.append((m, why or "unknown"))
                    continue
                covered.add(m)
                for p in data.get("packages") or []:
                    mp = p.get("manifest_path") if isinstance(p, dict) else None
                    if isinstance(mp, str):
                        covered.add(os.path.normpath(mp))
                wr = data.get("workspace_root")
                if isinstance(wr, str):
                    covered.add(os.path.join(os.path.normpath(wr), "Cargo.toml"))
                bd = data.get("build_directory")
                if not isinstance(bd, str) or not bd:
                    continue  # a cargo without build-dir support: not in this root
                bd = os.path.normpath(bd)
                for pr in pre:
                    if bd.startswith(pr + "/"):
                        key = "/".join(bd[len(pr) + 1 :].split("/")[:2])
                        lst = ws.claimed.setdefault(key, [])
                        shown = wr if isinstance(wr, str) else os.path.dirname(m)
                        if shown not in lst:
                            lst.append(shown)
                        break
        if ws.failures:
            m, why = ws.failures[0]
            more = f" (and {len(ws.failures) - 1} more)" if len(ws.failures) > 1 else ""
            raise Incomplete(f"{tilde(m, home)}: {why}{more}")
    except Incomplete as e:
        ws.incomplete = str(e)
    ws.seconds = time.monotonic() - start
    return ws


def guarded(
    root: str,
    folder: Folder,
    home: str,
    cache: dict[tuple[Any, ...], tuple[str | None, str | None]],
    step: Callable[[], tuple[str, str]],
    recheck: Callable[[], str] | None = None,
) -> tuple[str, str]:
    """Run STEP() while holding every cargo lock of FOLDER, after checking
    that no process uses it and, with RECHECK, that the rule that chose the
    action still holds (RECHECK() measures the folder again and returns ''
    or why not). STEP returns (result, detail). Returns that, or
    ('busy', why), ('changed', why) from RECHECK, or ('stop', why): a cargo
    process whose build folder is unknown runs; nothing more is deleted in
    this run."""
    fds, busy = take_locks(folder.path)
    if busy is not None:
        return "busy", f"cargo lock held: {os.path.relpath(busy, folder.path)}"
    # The users are checked while the locks are held: a cargo that starts
    # after this check must first compile, and that waits for the lock.
    try:
        users = proc_users(root)[0].get(folder.key)
        if users:
            return "busy", "in use: " + users_text(users)
        ties, unknown = cargo_users(root, home, cache)
        if ties.get(folder.key):
            return "busy", "cargo runs on it: " + users_text(ties[folder.key])
        if unknown:
            return "stop", unknown_text(unknown)
        # The plan was made from the activity measured when the run started;
        # a build may have written there since (and ended: no lock, no
        # process). Measured again here, while the locks are held: a cargo
        # that starts now waits for them.
        why = recheck() if recheck else ""
        if why:
            return "changed", why
        return step()
    finally:
        release(fds)


def delete_folder(
    root: str,
    folder: Folder,
    now: float,
    home: str,
    cache: dict[tuple[Any, ...], tuple[str | None, str | None]],
    recheck: Callable[[], str] | None = None,
    rm_deadline: float | None = None,
) -> tuple[str, str]:
    """Lock, check for users (and RECHECK, see guarded), rename away,
    unlock, delete. ('deleted'|'busy'|'changed'|'stop'|'failed', detail)."""
    try:
        st = os.lstat(folder.path)
    except FileNotFoundError:
        return "failed", "already gone"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return "failed", "no longer a folder (refused)"
    top = os.path.dirname(folder.path)
    trash = os.path.join(root, f"{TRASH_PREFIX}{folder.key.replace('/', '')}-{int(now)}")

    def step() -> tuple[str, str]:
        try:
            os.rename(folder.path, trash)
        except OSError as e:
            return "failed", f"rename: {e.strerror}"
        return "moved", ""

    result, detail = guarded(root, folder, home, cache, step, recheck)
    if result != "moved":
        return result, detail
    try:
        os.rmdir(top)  # the XX folder, when this was its last hash
    except OSError:
        pass
    ok, msg = remove_tree(trash, time_left(rm_deadline))
    if not ok:
        return "failed", f"rm: {msg}"
    # A cargo that was waiting for the lock during the rename builds again,
    # from scratch, into a new folder at the same path (it does not fail).
    if os.path.lexists(folder.path):
        return "deleted", "a build started meanwhile and is filling a new folder at the same path"
    return "deleted", ""


def trim_folder(
    root: str,
    folder: Folder,
    now: float,
    home: str,
    cache: dict[tuple[Any, ...], tuple[str | None, str | None]],
    recheck: Callable[[], str] | None = None,
    rm_deadline: float | None = None,
) -> tuple[str, str]:
    """Delete only the incremental folders of FOLDER, with the same locks and
    checks as delete_folder (RECHECK may update folder.inc). Each is renamed away while the locks are held;
    the modification time of its parent is put back afterwards, so that the
    folder's last activity (MAX_IDLE) does not move. ('trimmed'|'busy'|
    'stop'|'failed', detail)."""
    moved: list[str] = []
    base = folder.key.replace("/", "")

    def step() -> tuple[str, str]:
        for i, (path, _size) in enumerate(folder.inc):
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
                return "failed", f"rename {os.path.relpath(path, folder.path)}: {e.strerror}"
            moved.append(trash)
            try:
                os.utime(parent, ns=(pst.st_atime_ns, pst.st_mtime_ns), follow_symlinks=False)
            except OSError:
                pass
        return "moved", ""

    result, detail = guarded(root, folder, home, cache, step, recheck)
    # What was renamed away is deleted also after a later failure (else the
    # next run deletes it as a leftover).
    errors: list[str] = []
    for t in moved:
        ok, msg = remove_tree(t, time_left(rm_deadline))
        if not ok:
            errors.append(msg)
    if result != "moved":
        return result, detail
    if errors:
        return "failed", f"rm: {errors[0]}"
    return "trimmed", ""


class Target:
    """One cache of one toolchain. A new target is a subclass with these
    attributes and methods, added to TARGETS (and its keys to
    toolchains/toolchains.conf):
      ID        "lang:cache", as typed on the command line
      PREFIX    the settings prefix, e.g. RUST_BUILD
      WHAT      one line for status
      SETTINGS  its own setting names (without PREFIX_; ENABLED is common)
      make_policy(vals, prefix)  checked policy from {SETTING: raw value}
      location()                 the folder it prunes (for status)
      check()                    raise Refused when it would refuse to run
      plan(), apply()            exit status; apply also calls write_state
      status_lines()             [line] for status (size, oldest, ...)
      last_run_text(state)       one line from the saved summary"""

    ID: str = ""
    PREFIX: str = ""
    WHAT: str = ""
    SETTINGS: tuple[str, ...] = ()
    # time.monotonic() by which an apply must be done (None: no limit);
    # cmd_prune sets it from RUN_BUDGET.
    deadline: float | None = None
    # PATH to look for programs in (None: this process's PATH).
    path: str | None = None
    # The most seconds one apply can take (None: no fixed bound, only the
    # run's budget). prune apply runs the bounded targets first.
    MAX_SECONDS: int | None = None
    # check() looks for a program on PATH (auto on then checks the user
    # manager's PATH, which the timer runs with).
    USES_PATH: bool = False
    named: bool = False

    def __init__(self, home: str, now: float, policy: dict[str, Any] | None) -> None:
        self.home: str = home
        self.now: float = now
        self.policy: dict[str, Any] = {} if policy is None else policy
        self.started: float = time.monotonic()

    @staticmethod
    def make_policy(vals: Mapping[str, str], prefix: str) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def policy_text(pol: Mapping[str, Any]) -> str:
        raise NotImplementedError

    @staticmethod
    def last_run_text(s: Mapping[str, Any]) -> str:
        raise NotImplementedError

    def location(self) -> str:
        raise NotImplementedError

    def check(self) -> None:
        raise NotImplementedError

    def plan(self) -> int:
        raise NotImplementedError

    def apply(self) -> int:
        raise NotImplementedError

    def status_lines(self) -> list[str]:
        raise NotImplementedError

    def tsay(self, msg: str, prio: int = 6) -> None:
        say(f"{self.ID}: {msg}", prio)

    def clock(self) -> float:
        """Now, on the run's clock: NOW plus the time since the start."""
        return self.now + (time.monotonic() - self.started)

    def time_left(self) -> float | None:
        """Seconds left of the run's time budget (None: no limit)."""
        return time_left(self.deadline)


class RustBuild(Target):
    ID = "rust:build"
    PREFIX = "RUST_BUILD"
    WHAT = "cargo's intermediate build files (build-dir), one folder per workspace path"
    SETTINGS = ("MAX_IDLE", "MAX_TOTAL", "MIN_KEEP", "ORPHANS", "INCREMENTAL_IDLE")

    @staticmethod
    def make_policy(vals: Mapping[str, str], prefix: str) -> dict[str, Any]:
        pol: dict[str, Any] = {
            "max_idle": parse_duration(vals["MAX_IDLE"]),
            "max_total": parse_size(vals["MAX_TOTAL"]),
            "min_keep": parse_duration(vals["MIN_KEEP"]),
            "orphans": parse_bool(vals["ORPHANS"]),
            "incremental_idle": parse_off_or_duration(vals["INCREMENTAL_IDLE"]),
        }
        if pol["min_keep"] < MIN_KEEP_FLOOR:
            raise ConfigError(f"{prefix}_MIN_KEEP={vals['MIN_KEEP']} is below the floor of 15m")
        if pol["max_idle"] < pol["min_keep"]:
            raise ConfigError(
                f"{prefix}_MAX_IDLE={vals['MAX_IDLE']} is shorter than {prefix}_MIN_KEEP={vals['MIN_KEEP']}"
            )
        if pol["incremental_idle"] and pol["incremental_idle"] < pol["min_keep"]:
            raise ConfigError(
                f"{prefix}_INCREMENTAL_IDLE={vals['INCREMENTAL_IDLE']} is shorter than "
                f"{prefix}_MIN_KEEP={vals['MIN_KEEP']} (0 or off turns it off)"
            )
        return pol

    @staticmethod
    def policy_text(pol: Mapping[str, Any]) -> str:
        cap = gib(pol["max_total"]) if pol["max_total"] else "off"
        inc = span(pol["incremental_idle"]) if pol.get("incremental_idle") else "off"
        orph = "on" if pol.get("orphans", True) else "off"
        return (
            f"MAX_IDLE {span(pol['max_idle'])}, MAX_TOTAL {cap}, MIN_KEEP {span(pol['min_keep'])}, "
            f"ORPHANS {orph}, INCREMENTAL_IDLE {inc}"
        )

    def location(self) -> str:
        return os.path.join(self.home, ROOT_REL)

    def check(self) -> None:
        resolve_root(self.home)

    def gather(self) -> tuple[str | None, list[Folder], list[Folder], list[tuple[str, str]]]:
        root = resolve_root(self.home)
        if root is None:
            return None, [], [], []
        folders, trash, others = scan(root, self.now)
        return root, folders, trash, others

    def workspaces(self, root: str) -> tuple[dict[str, list[str]] | None, Workspaces | None]:
        """(claimed or None, Workspaces or None): the folders claimed by a
        workspace; None when the orphan rule is off or the search is
        incomplete (then nothing is an orphan)."""
        if not self.policy.get("orphans", True):
            return None, None
        ws = claimed_folders(root, self.home)
        if ws.incomplete:
            return None, ws
        if not ws.claimed:
            ws.incomplete = "no workspace claims any folder (a search that finds nothing is not trusted)"
            return None, ws
        return ws.claimed, ws

    def workspace_lines(
        self, ws: Workspaces | None, claimed: Mapping[str, Sequence[str]] | None, folders: Sequence[Folder]
    ) -> list[str]:
        if ws is None:
            return ["orphans: the rule is off (RUST_BUILD_ORPHANS=no)"]
        out = [
            f"workspaces: {ws.repos} repositories under ~/{WORKSPACE_REL}, {len(ws.worktrees)} worktree(s), "
            f"{ws.manifests} Cargo.toml, {ws.asked} asked (cargo metadata --offline --no-deps), "
            f"{ws.seconds:.0f}s"
        ]
        if claimed is None:
            out.append(f"orphans: NOT looked for in this run: {ws.incomplete}")
        else:
            unclaimed = [f for f in folders if f.key not in claimed]
            out.append(
                f"orphans: {len(claimed)} folder(s) claimed by a workspace, {len(unclaimed)} not claimed "
                f"({gib(sum(f.size for f in unclaimed))})"
            )
        return out

    def plan(self) -> int:
        home, now, pol = self.home, self.now, self.policy
        root, folders, trash, others = self.gather()
        if root is None:
            say(f"  ~/{ROOT_REL} does not exist yet: nothing to do")
            return 0
        say(f"  root: {tilde(root, home)}/XX/YYYYYYYYYYYYYY (cargo build-dir)")
        say(
            f"  total: {gib(sum(f.size for f in folders))} in {len(folders)} folder(s) "
            "(sizes as du shows them: before btrfs compression; on disk usually less, twice with RAID1)"
        )
        claimed, ws = self.workspaces(root)
        for line in self.workspace_lines(ws, claimed, folders):
            say(f"  {line}")
        users_map, unreadable = proc_users(root)
        ties, unknown = cargo_users(root, home, {})
        users = merge_users(users_map, ties)
        plan = decide(folders, pol, now, users, claimed)
        if plan:
            say(f"  {'ACTION':<7} {'SIZE':>10}  {'LAST ACTIVE':<16}  FOLDER            WHY")
        for f, a, why in plan:
            say(f"  {a:<7} {gib(f.size):>10}  {when(f.activity):<16}  {f.key:<17} {why}")
        print_others(others, trash)
        gone = [f for f, a, _ in plan if a == "delete"]
        trims = [f for f, a, _ in plan if a == "trim"]
        say(
            f"  apply would delete {len(gone)} folder(s), {gib(sum(f.size for f in gone))}"
            + (
                f", and the incremental folders of {len(trims)} more, {gib(sum(f.inc_size for f in trims))}"
                if trims
                else ""
            )
            + f"; left: {gib(after_total(plan))} in {len(plan) - len(gone)} folder(s)"
        )
        if pol["max_total"] and after_total(plan) > pol["max_total"]:
            say("  still above MAX_TOTAL afterwards: the rest is active within MIN_KEEP, in use or left alone")
        if unreadable:
            say(
                f"  note: {unreadable} Rust tool process(es) of yours could not be inspected (run on the host to "
                "see all); cargo's own lock is still checked before each deletion"
            )
        if unknown and (gone or trims):
            say(f"  note: {unknown_text(unknown)}; apply deletes nothing while it runs")
        return 0

    def apply(self) -> int:
        home, now, pol = self.home, self.now, self.policy
        root, folders, trash, others = self.gather()
        if root is None:
            self.tsay(f"~/{ROOT_REL} does not exist yet: nothing to do")
            write_state(
                home,
                self.ID,
                {
                    "time": now,
                    "deleted": 0,
                    "freed": 0,
                    "total": 0,
                    "folders": 0,
                    "busy": 0,
                    "failed": 0,
                    "trimmed": 0,
                    "trim_freed": 0,
                },
            )
            return 0
        before = sum(f.size for f in folders)
        self.tsay(f"{len(folders)} folder(s), {gib(before)}; policy {self.policy_text(pol)}")
        for name, why in others:
            self.tsay(f"left alone: {name} ({why})", 5)
        claimed, ws = self.workspaces(root)
        for line in self.workspace_lines(ws, claimed, folders):
            self.tsay(line, 5 if claimed is None and ws is not None else 6)
        rc = 0
        cache: dict[tuple[Any, ...], tuple[str | None, str | None]] = {}
        users_map, unreadable = proc_users(root)
        users: dict[str, list[tuple[int | str, str]]] = merge_users(users_map, cargo_users(root, home, cache)[0])
        if unreadable:
            self.tsay(
                f"{unreadable} Rust tool process(es) of yours could not be inspected; cargo's lock is still checked", 5
            )
        for t in trash:
            if users.get(t.key):
                self.tsay(f"leftover {t.key} in use ({users_text(users[t.key])}): kept for now", 5)
                continue
            ok, msg = remove_tree(t.path, self.time_left())
            self.tsay(
                f"deleted leftover {t.key} ({gib(t.size)})" if ok else f"could not delete leftover {t.key}: {msg}",
                6 if ok else 3,
            )
        deleted: dict[str, Folder] = {}
        trimmed: dict[str, int] = {}
        nbusy = failed = 0
        left = list(folders)
        # Said once: what the policy alone would delete but a process uses now.
        for f, a, _ in decide(left, pol, now, {}, claimed):
            if a in ("delete", "trim") and f.key in users:
                nbusy += 1
                self.tsay(f"skipped {f.key}: in use: {users_text(users[f.key])}", 5)
        dev = os.lstat(root).st_dev
        rechecked: dict[str, int] = {}
        stopped_by_budget = False
        root_dir = root

        def recheck(f: Folder, a: str) -> Callable[[], str]:
            """Measure F again (the locks are held) and plan again with it:
            '' when the rule still gives action A (or a delete when A is a
            trim), else why not. F takes the fresh measurement either way."""

            def check() -> str:
                cur = self.clock()
                top, leaf = f.key.split("/")
                fresh = measure(Folder(root_dir, top, leaf), dev, cur)
                got, gwhy = next(
                    (x, w)
                    for g, x, w in decide([fresh if g is f else g for g in left], pol, cur, users, claimed)
                    if g is fresh
                )
                f.__dict__.update(fresh.__dict__)
                if got == a or (a == "trim" and got == "delete" and f.inc):
                    return ""
                return f"measured again: {gwhy}" if got == "keep" else f"measured again: now {got}: {gwhy}"

            return check

        # One folder at a time, planned again after each: a folder found busy (or
        # failing) stays counted and kept, so the cap picks the next one instead.
        while True:
            cur = self.clock()
            plan = decide(left, pol, cur, users, claimed)
            nxt = next(((f, a, why) for f, a, why in plan if a == "delete"), None)
            nxt = nxt or next(((f, a, why) for f, a, why in plan if a == "trim"), None)
            if nxt is None:
                break
            f, a, why = nxt
            left_s = self.time_left()
            if left_s is not None and left_s < RM_MARGIN:
                stopped_by_budget = True
                self.tsay(
                    f"stopped before {f.key}: the run's time budget ({span(RUN_BUDGET)}) is nearly used up; "
                    "the next run goes on",
                    5,
                )
                break
            if a == "delete":
                result, detail = delete_folder(root_dir, f, now, home, cache, recheck(f, a), self.deadline)
            else:
                result, detail = trim_folder(root_dir, f, now, home, cache, recheck(f, a), self.deadline)
            if result == "changed":
                # Planned again with the fresh measurement; a folder that keeps
                # changing is left for the next run.
                rechecked[f.key] = rechecked.get(f.key, 0) + 1
                self.tsay(f"not {'deleted' if a == 'delete' else 'trimmed'}: {f.key}: {detail}", 5)
                if rechecked[f.key] < 3:
                    continue
                result = "busy"
            if result == "deleted":
                left.remove(f)
                deleted[f.key] = f
                self.tsay(f"deleted {f.key} ({gib(f.size)}, last active {when(f.activity)}): {why}")
                if detail:
                    self.tsay(f"note: {f.key}: {detail}", 5)
                continue
            if result == "trimmed":
                trimmed[f.key] = f.inc_size
                self.tsay(
                    f"deleted the incremental folders of {f.key} ({gib(f.inc_size)}, last active "
                    f"{when(f.activity)}): {why}"
                )
                f.size -= f.inc_size  # planned again without them (the cap counts what is left)
                f.inc = []
                continue
            if result == "stop":
                nbusy += 1
                self.tsay(f"stopped before {f.key}: {detail}; nothing more is deleted in this run", 5)
                break
            if result == "busy":
                nbusy += 1
                self.tsay(f"skipped {f.key}: {detail}", 5)
            else:
                failed += 1
                rc = 1
                self.tsay(
                    f"could not delete {'the incremental folders of ' if a == 'trim' else ''}{f.key}: {detail}", 3
                )
            if a == "delete" and not os.path.lexists(f.path):
                left.remove(f)  # renamed away; the next run deletes the leftover
            users = dict(users)
            users[f.key] = [("-", detail)]
        freed = sum(f.size for f in deleted.values())
        trim_freed = sum(trimmed.values())
        total = before - freed - trim_freed
        self.tsay(
            f"done: deleted {len(deleted)} folder(s), freed {gib(freed)}"
            + (f"; incremental of {len(trimmed)} folder(s), freed {gib(trim_freed)}" if trimmed else "")
            + f"; left {gib(total)} in {len(folders) - len(deleted)} folder(s)"
            + (f"; {nbusy} busy" if nbusy else "")
            + ("; stopped at the time budget" if stopped_by_budget else "")
        )
        if pol["max_total"] and total > pol["max_total"] and not stopped_by_budget:
            self.tsay(
                f"still above MAX_TOTAL ({gib(total)} > {gib(pol['max_total'])}): "
                "the rest is active within MIN_KEEP, in use or left alone",
                4,
            )
        write_state(
            home,
            self.ID,
            {
                "time": now,
                "deleted": len(deleted),
                "freed": freed,
                "total": total,
                "folders": len(folders) - len(deleted),
                "busy": nbusy,
                "failed": failed,
                "trimmed": len(trimmed),
                "trim_freed": trim_freed,
            },
        )
        return rc

    @staticmethod
    def last_run_text(s: Mapping[str, Any]) -> str:
        extra = f", {s['busy']} busy" if s.get("busy") else ""
        extra += f", {s['failed']} failed" if s.get("failed") else ""
        trim = f", incremental of {s['trimmed']} folder(s) ({gib(s.get('trim_freed', 0))})" if s.get("trimmed") else ""
        return (
            f"{when(s['time'])}, deleted {s['deleted']} folder(s) ({gib(s['freed'])}){trim}{extra}; "
            f"left {gib(s['total'])} in {s['folders']} folder(s)"
        )

    def status_lines(self) -> list[str]:
        """Size and age of the folders, or why they are not looked at."""
        try:
            root = resolve_root(self.home)
        except Refused as e:
            return [f"not pruned: {e}"]
        if root is None:
            return [f"size:     ~/{ROOT_REL} does not exist yet"]
        folders, trash, others = scan(root, self.now)
        out = [
            f"size:     {gib(sum(f.size for f in folders))} in {len(folders)} folder(s) "
            "(as du shows it: before btrfs compression)"
        ]
        if folders:
            old = min(folders, key=lambda f: f.activity)
            new = max(folders, key=lambda f: f.activity)
            out.append(
                f"oldest:   {when(old.activity)} ({ago(self.now - old.activity)} ago, {old.key}, {gib(old.size)})"
            )
            out.append(
                f"newest:   {when(new.activity)} ({ago(self.now - new.activity)} ago, {new.key}, {gib(new.size)})"
            )
        out += [f"left alone: {name} ({why})" for name, why in others]
        out += [f"leftover of an interrupted run: {t.key} ({gib(t.size)}): deleted by apply" for t in trash]
        return out


def print_others(others: Sequence[tuple[str, str]], trash: Sequence[Folder]) -> None:
    for name, why in others:
        say(f"  left alone: {name} ({why})")
    for t in trash:
        say(f"  leftover of an interrupted run: {t.key} ({gib(t.size)}): deleted by apply")


class PythonUv(Target):
    ID = "python:uv"
    PREFIX = "PYTHON_UV"
    WHAT = "uv's package cache (uv cache prune: only entries uv no longer uses)"
    SETTINGS = ("INTERVAL",)
    named = False
    MAX_SECONDS = UV_TIMEOUT
    USES_PATH = True

    @staticmethod
    def make_policy(vals: Mapping[str, str], prefix: str) -> dict[str, int]:
        _ = prefix
        return {"interval": parse_off_or_duration(vals["INTERVAL"])}

    @staticmethod
    def policy_text(pol: Mapping[str, Any]) -> str:
        every = span(pol["interval"]) if pol["interval"] else "every run"
        return f"uv cache prune, at most once per INTERVAL ({every})"

    def location(self) -> str:
        return os.path.join(self.home, UV_CACHE_REL)

    def uv(self) -> str:
        uv = shutil.which("uv", path=self.path)
        if not uv:
            raise Refused("uv not found on PATH: nothing is done (install uv, or PYTHON_UV_ENABLED=no)")
        return uv

    def check(self) -> None:
        self.uv()

    def run_uv(self, uv: str, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return run_idle([uv, *args], self.home, uv_env(), timeout)

    def uv_facts(self, uv: str) -> tuple[str, bool]:
        """(version text, has --dry-run): read only, from uv's own help."""
        try:
            v = self.run_uv(uv, ["--version"], 20).stdout.strip()
            h = self.run_uv(uv, ["cache", "prune", "--help"], 20).stdout
        except OSError, subprocess.TimeoutExpired:
            return "?", False
        return v or "?", "--dry-run" in h

    def cache(self) -> str | None:
        """The cache folder, or None when it does not exist (nothing to do)."""
        c = self.location()
        try:
            st = os.lstat(os.path.realpath(c))
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{tilde(c, self.home)} is not a folder: refused")
        return c

    def due(self) -> str:
        """'' when an automatic run is due, else why not."""
        last = read_state(self.home, self.ID)
        iv = self.policy["interval"]
        if self.named or not iv or not last or not isinstance(last.get("time"), (int, float)):
            return ""
        if self.now - last["time"] < iv:
            return f"last run {ago(self.now - last['time'])} ago (INTERVAL {span(iv)})"
        return ""

    def plan(self) -> int:
        c = self.cache()
        if c is None:
            say(f"  ~/{UV_CACHE_REL} does not exist: nothing to do")
            return 0
        uv = self.uv()
        version, dry = self.uv_facts(uv)
        say(f"  uv: {uv} ({version}); cache {gib(tree_size(c))} (as du shows it)")
        if uv_lock_busy(c):
            say("  a uv command is using the cache now (its lock is held): apply would skip it this time")
        wait = self.due()
        if wait:
            say(f"  the timer would skip it: {wait}; named on the command line it runs")
        if dry:
            lines, ok = uv_prune_preview(lambda args: self.run_uv(uv, args, UV_TIMEOUT), c)
            for line in lines:
                say(f"  {line}")
            return 0 if ok else 1
        say(
            "  apply runs `uv cache prune`: it removes only what uv itself no longer uses (old versions of "
            "cache entries, unused temporary environments); this uv has no --dry-run, so how much is "
            "only known afterwards"
        )
        return 0

    def apply(self) -> int:
        c = self.cache()
        if c is None:
            self.tsay(f"~/{UV_CACHE_REL} does not exist: nothing to do")
            return 0
        wait = self.due()
        if wait:
            self.tsay(f"not due: {wait}")
            return 0
        uv = self.uv()
        if uv_lock_busy(c):
            self.tsay("skipped: a uv command is using the cache (its lock is held); the next run tries again", 5)
            return 0
        timeout: float = UV_TIMEOUT
        left = self.time_left()
        if left is not None:
            if left < 2 * UV_LOCK_WAIT:
                self.tsay(
                    f"skipped: the run's time budget ({span(RUN_BUDGET)}) is used up; the next run tries again", 5
                )
                return 0
            timeout = min(timeout, left)
        try:
            r = self.run_uv(uv, ["cache", "prune", "--cache-dir", c, "--no-progress"], timeout)
        except subprocess.TimeoutExpired:
            self.tsay(f"uv cache prune took longer than {ago(timeout)}: stopped", 3)
            return 1
        except OSError as e:
            self.tsay(f"could not run uv: {e.strerror or e}", 3)
            return 1
        text = (r.stdout + "\n" + r.stderr).strip()
        if r.returncode != 0:
            if UV_LOCK_TIMED_OUT.search(text) or uv_lock_busy(c):
                self.tsay(
                    f"skipped: uv waited {UV_LOCK_WAIT}s for its cache lock (another uv runs); "
                    "the next run tries again",
                    5,
                )
                return 0
            last = (text.splitlines() or ["failed"])[-1]
            self.tsay(f"uv cache prune failed: {last}", 3)
            return 1
        removed, freed = 0, 0
        m = UV_REMOVED.search(text)
        if m:
            removed = int(m.group(1))
            freed = int(float(m.group(2)) * UV_UNITS.get(m.group(3).lower(), 1))
        for line in text.splitlines():
            if line.strip():
                self.tsay(f"uv: {line.strip()}")
        write_state(self.home, self.ID, {"time": self.now, "removed": removed, "freed": freed})
        return 0

    @staticmethod
    def last_run_text(s: Mapping[str, Any]) -> str:
        return f"{when(s['time'])}, removed {s.get('removed', 0)} file(s) ({gib(s.get('freed', 0))})"

    def status_lines(self) -> list[str]:
        c = self.location()
        if not os.path.isdir(c):
            return [f"size:     ~/{UV_CACHE_REL} does not exist"]
        out = [f"size:     {gib(tree_size(c))} (as du shows it)"]
        uv = shutil.which("uv")
        out.append(f"uv:       {uv}" if uv else "uv:       not found on PATH (prune refuses)")
        if uv_lock_busy(c):
            out.append("in use:   a uv command holds the cache lock now")
        return out


def toolchain_lines(home: str) -> list[str]:
    """The report of `status`: installed rustup toolchains, their size, and
    which rust-toolchain files under ~/Workspace pin them."""
    rh = rustup_home(home)
    tdir = os.path.join(rh, "toolchains")
    try:
        names = sorted(e.name for e in os.scandir(tdir) if e.is_dir())
    except OSError:
        return [f"{where_text(tdir, home)}: none installed (or not readable)"]
    ws = claimed_folders(None, home, with_cargo=False)
    pins: dict[str, list[str]] = {}
    for p in ws.pins:
        ch = pinned_channel(p)
        if ch:
            pins.setdefault(ch, []).append(p)
    try:
        with open(os.path.join(rh, "settings.toml"), "rb") as f:
            overrides = tomllib.load(f).get("overrides") or {}
    except OSError, tomllib.TOMLDecodeError:
        overrides = {}
    for path, ch in overrides.items() if isinstance(overrides, dict) else ():
        if isinstance(ch, str):
            pins.setdefault(short_toolchain(ch), []).append(f"rustup override for {path}")
    default = rustup_default(home, rh)
    out = [f"where:    {where_text(tdir, home)}", f"default:  {short_toolchain(default) if default else 'unknown'}"]
    if ws.incomplete:
        out.append(f"note:     the search for rust-toolchain files was incomplete ({ws.incomplete})")
    rows: list[str] = []
    total = 0
    unused: list[str] = []
    is_default = {n: bool(default) and short_toolchain(n) == short_toolchain(default) for n in names}
    loose = [
        short_toolchain(n) for n in names if not is_default[n] and not pins.get(short_toolchain(n)) and not pins.get(n)
    ]
    # Files in the home first (a linked worktree elsewhere, e.g. in /tmp, is shown only when it is the only one).
    files = sorted(ws.tool_files, key=lambda p: not (p == home or p.startswith(home + "/")))
    named, read_all = named_in(files, loose) if loose else ({}, True)
    if not read_all:
        ws.incomplete = ws.incomplete or f"more than {TOOL_FILES_MAX} build files to read"
    for n in names:
        short = short_toolchain(n)
        path = os.path.join(tdir, n)
        size = 0 if os.path.islink(path) else tree_size(path)
        total += size
        by = pins.get(short, []) + (pins.get(n, []) if n != short else [])
        if by:
            first = tilde(by[0], home)
            pin = f"pinned by {len(by)} file(s), e.g. {first}" if len(by) > 1 else f"pinned by {first}"
        elif is_default[n]:
            pin = "the default"
        elif short in named:
            pin = f"named in {tilde(named[short], home)} (not offered for removal)"
        else:
            pin = "not pinned or named under ~/Workspace"
            unused.append(short)
        rows.append(
            f"  {gib(size):>10}  {short:<22} {pin}"
            + ("  (a link: rustup toolchain link)" if os.path.islink(path) else "")
        )
    out.append(f"{len(names)} toolchain(s), {gib(total)} (as du shows it):")
    out += rows
    known = {short_toolchain(n) for n in names} | set(names)
    missing = sorted(ch for ch in pins if ch not in known)
    if missing:
        out.append("pinned but not installed (the next build there downloads it): " + ", ".join(missing))
    out.append(
        f"(read: rust-toolchain.toml and rust-toolchain files, Makefiles, justfiles, *.mk and *.sh "
        f"files, CI workflows and .cargo/config.toml of the git worktrees under ~/{WORKSPACE_REL}, and "
        "rustup overrides)"
    )
    if unused and ws.incomplete:
        out.append(
            f"not pinned, not the default and not named in the files read: {', '.join(unused)}; no "
            "removal command, because the search was incomplete"
        )
    elif unused:
        out.append(
            "not pinned, not the default and not named in those files. NOT CHECKED: projects outside "
            f"~/{WORKSPACE_REL}, other scripts and programs, RUSTUP_TOOLCHAIN. If none of those needs "
            "them, you can remove them yourself (CST never does):"
        )
        out.append(f"  rustup toolchain uninstall {' '.join(unused)}")
    return out


# The registry. Adding a target: a Target subclass here, its keys in
# toolchains/toolchains.conf, a line in toolchains/README.md.
TARGETS: dict[str, type[Target]] = {cls.ID: cls for cls in (RustBuild, PythonUv)}


def select_targets(names: Sequence[str], settings: Settings) -> list[tuple[type[Target], bool]]:
    """[(Target class, named)]: the named targets (LANG:CACHE or LANG), or,
    without names, every enabled one. Raises Usage for an unknown or a
    planned target."""
    if not names:
        return [(cls, False) for tid, cls in TARGETS.items() if settings.enabled[tid]]
    out: list[tuple[type[Target], bool]] = []
    for name in names:
        if ":" in name:
            if name in TARGETS:
                found = [TARGETS[name]]
            elif name in PLANNED:
                raise Usage(f"target {name} is not implemented yet (implemented: {', '.join(TARGETS)})")
            else:
                raise Usage(f"unknown target '{name}' (implemented: {', '.join(TARGETS)})")
        else:
            found = [cls for tid, cls in TARGETS.items() if tid.split(":")[0] == name]
            if not found:
                planned = [t for t in PLANNED if t.split(":")[0] == name]
                if planned:
                    raise Usage(f"no target of {name} is implemented yet (planned: {', '.join(planned)})")
                raise Usage(f"unknown target '{name}' (implemented: {', '.join(TARGETS)})")
        for cls in found:
            if all(cls is not c for c, _ in out):
                out.append((cls, True))
    return out


def cmd_prune(home: str, now: float, action: str, names: Sequence[str]) -> int:
    settings = Settings(home)
    chosen = select_targets(names, settings)
    if not chosen:
        say(f"no target is enabled (settings: {settings.source}): nothing to do")
        return 0
    rc = 0
    deadline = None
    if action == "apply":
        deadline = time.monotonic() + RUN_BUDGET
        chosen = sorted(chosen, key=lambda c: c[0].MAX_SECONDS is None)  # stable: bounded ones first
    for cls, named in chosen:
        t = cls(home, now, settings.policy[cls.ID])
        t.named = named
        t.deadline = deadline
        off = "" if settings.enabled[cls.ID] else " (disabled in the settings: runs only because it was named)"
        if action == "plan":
            say(f"== {cls.ID}: {where_text(t.location(), home)}{off}")
            say(f"  policy: {cls.policy_text(t.policy)} ({settings.source_of(cls)})")
        elif off:
            t.tsay(off.strip(" ()"), 5)
        try:
            r = t.plan() if action == "plan" else t.apply()
        except Refused as e:
            if action == "plan":
                say(f"  refused: {e}")
            else:
                t.tsay(f"refused: {e}", 3)
            r = 1
        rc = max(rc, r)
    if action == "plan":
        say("(plan: nothing deleted)")
    return rc


UNITS: dict[str, Callable[[], str]] = {SERVICE: render_service, TIMER: render_timer}


def copies() -> dict[str, bytes]:
    """{installed path (relative to home): bytes}: this program as `install`
    writes it (a zipapp) and its defaults, as the timer runs them."""
    defaults = read_bytes(defaults_path())
    if defaults is None:
        raise ConfigError(f"the defaults file {defaults_path()} is missing")
    return {PROG_REL: install_payload("toolchains/cst-toolchains", Program.entry_file), DEFAULTS_REL: defaults}


def unit_states(home: str) -> dict[str, UnitFileState]:
    out: dict[str, UnitFileState] = {rel: file_state(os.path.join(home, rel), text) for rel, text in copies().items()}
    for name, render in UNITS.items():
        out[name] = timer_unit_state(os.path.join(home, UNIT_DIR_REL, name), render())
    return out


def stale_files(home: str) -> list[tuple[str, bool]]:
    """[(path, ours)] of what `cst btrfs cargo-gc enable|disable` left
    (the first version of this cleanup)."""
    out: list[tuple[str, bool]] = []
    for rel in [f"{UNIT_DIR_REL}/{u}" for u in OLD_UNITS] + list(OLD_FILES):
        path = os.path.join(home, rel)
        st = file_state(path, None, OLD_MARKER)
        if st != "missing":
            out.append((path, st == "change"))
    for rel in OLD_DATA:
        path = os.path.join(home, rel)
        if os.path.lexists(path):
            out.append((path, not os.path.islink(path)))
    return out


def remove_stale(home: str) -> bool:
    """Remove what the earlier `cst btrfs cargo-gc` left. True when all went
    well; False when the daemon-reload after the removal failed (reported).
    Refused (nothing removed) when its timer could not be stopped."""
    found = stale_files(home)
    if not found:
        return True
    timer = os.path.join(home, UNIT_DIR_REL, OLD_TIMER)
    if (timer, True) in found:
        disable_timer(OLD_TIMER)
    reload = False
    for path, ours in found:
        if not ours:
            say(f"left alone: {tilde(path, home)} (not written by CST)", 4, err=True)
            continue
        os.unlink(path)
        say(f"removed:   {tilde(path, home)} (left by the earlier `cst btrfs cargo-gc`)")
        reload = reload or path.startswith(os.path.join(home, UNIT_DIR_REL) + "/")
    return not (reload and reload_failed())


def cmd_auto_on(home: str, now: float, unless_off: bool = False) -> int:
    rc = 0 if remove_stale(home) else 1
    if unless_off and turned_off(home):
        say(
            f"left off: you turned the automatic pruning off ({PROG} auto off left ~/{OFF_REL}); "
            f"'{PROG} auto on' turns it on again"
        )
        return rc
    settings = Settings(home)
    enabled = [cls for tid, cls in TARGETS.items() if settings.enabled[tid]]
    if not enabled:
        raise Refused(f"no target is enabled in the settings ({settings.source}): the timer would do nothing")
    # Checked as the timer will run: programs are looked for in the PATH of
    # the user manager (the service's PATH), not in this command's (run by
    # snapshots setup through runuser, it is root's). If no target would run,
    # no timer.
    mpath: str | None = None
    asked = False
    refused: list[str] = []
    for cls in enabled:
        t = cls(home, now, settings.policy[cls.ID])
        if cls.USES_PATH:
            if not asked:
                mpath, asked = manager_path(), True
            t.path = mpath
        try:
            t.check()
        except Refused as e:
            refused.append(f"{cls.ID}: {e}")
    if len(refused) == len(enabled):
        raise Refused("; ".join(refused))
    where = (
        "in the PATH of your user manager, which the timer uses"
        if mpath is not None
        else "in the PATH of this command; the timer uses your user manager's PATH (systemctl --user "
        "show-environment), which could not be read"
    )
    for r in refused:
        say(
            f"note: {r} (looked for {where}): each timer run tries it and reports it as refused (an error) "
            f"until it is fixed; or set its _ENABLED=no in ~/{CONF_REL}",
            4,
            err=True,
        )
    st = unit_states(home)
    if why := masked_refusal([os.path.join(home, UNIT_DIR_REL, k) for k in UNITS if st[k] == "masked"], user=True):
        raise Refused(why + "; nothing was written")
    conflicts = [k for k, v in st.items() if v == "conflict"]
    if conflicts:
        raise Refused("not written by CST, left alone: " + ", ".join(label(k) for k in conflicts))
    changed = False
    for rel, text in copies().items():
        if st[rel] != "ok":
            write_atomic(os.path.join(home, rel), text, mode=0o755 if rel == PROG_REL else 0o644)
            say(f"written:   ~/{rel}")
        else:
            say(f"unchanged: ~/{rel}")
    for name, render in UNITS.items():
        if st[name] != "ok":
            write_atomic(os.path.join(home, UNIT_DIR_REL, name), render(), mode=0o644)
            say(f"written:   {label(name)}")
            changed = True
        else:
            say(f"unchanged: {label(name)}")
    if changed:
        systemctl("daemon-reload", check=True)
    systemctl("enable", "--now", TIMER, check=True)
    say(f"enabled:   {TIMER} (hourly; logs: journalctl --user -u {SERVICE})")
    if turned_off(home):
        os.unlink(os.path.join(home, OFF_REL))
        say(f"removed:   ~/{OFF_REL} (snapshots setup keeps it enabled again)")
    old_conf_note(home)
    return rc


def stop_orphan_timer() -> None:
    """The timer file is gone, but the user manager may still run the
    timer: what `auto off` left before it checked its `disable` (it removed
    the files anyway). Stopped when the manager does not say it is stopped;
    Refused when the manager gives no answer."""
    # is-active prints the state; it exits 3 when the unit is not active,
    # and 4 when the manager does not know it (nothing loaded it).
    if before_removal(TIMER, "is-active", ok=(0, 3, 4)) in ("inactive", "failed"):
        return
    before_removal(TIMER, "stop")
    say(f"stopped:   {TIMER} (it was still running although its file was gone)")


def cmd_auto_off(home: str, now: float) -> int:
    st = unit_states(home)
    # Both timers are stopped, checked, before anything is removed (see
    # before_removal); remove_stale stops the earlier one first.
    if st[TIMER] in ("ok", "change"):
        disable_timer(TIMER)
    elif st[TIMER] == "missing":
        stop_orphan_timer()
    rc = 0 if remove_stale(home) else 1
    removed = False
    for name in UNITS:
        if st[name] in ("ok", "change"):
            os.unlink(os.path.join(home, UNIT_DIR_REL, name))
            say(f"removed:   {label(name)}")
            removed = True
        elif st[name] in ("conflict", "masked"):
            why = "masked with systemctl --user mask" if st[name] == "masked" else "not written by CST"
            say(f"left alone: {label(name)} ({why})", 4, err=True)
    # `disable` removes the link that enables the timer. One left pointing
    # at nothing (also left by the earlier unchecked `auto off`) goes too.
    wants = os.path.join(home, WANTS_REL)
    if os.path.islink(wants) and not os.path.exists(wants):
        os.unlink(wants)
        say(f"removed:   ~/{WANTS_REL} (it pointed at the removed timer file)")
        removed = True
    if removed and reload_failed():
        rc = 1
    for rel in copies():
        if st[rel] in ("ok", "change"):
            os.unlink(os.path.join(home, rel))
            say(f"removed:   ~/{rel}")
        elif st[rel] == "conflict":
            say(f"left alone: ~/{rel} (not written by CST)", 4, err=True)
    try:
        os.rmdir(os.path.join(home, LIB_REL))  # only when empty (bluetooth keeps its watcher there)
    except OSError:
        pass
    for tid in TARGETS:
        try:
            os.unlink(state_path(home, tid))
        except OSError:
            pass
    if not turned_off(home):
        write_atomic(
            os.path.join(home, OFF_REL),
            f"{MARKER}: written by `cst toolchains auto off`.\n"
            "# While this file exists, `cst btrfs snapshots setup` does not turn the hourly\n"
            "# pruning of toolchain caches back on. `cst toolchains auto on` removes it.\n",
            mode=0o644,
        )
        say(f"written:   ~/{OFF_REL} (snapshots setup leaves the pruning off)")
    say(f"toolchain caches are no longer pruned automatically (settings in ~/{CONF_REL} kept, if any)")
    return rc


def timer_text(home: str) -> str:
    if file_state(os.path.join(home, UNIT_DIR_REL, TIMER), render_timer()) == "missing":
        if systemctl("is-active", TIMER) == "active":
            return (
                f"timer {TIMER}: still running although its file is gone (left by an earlier auto off); "
                f"{PROG} auto off stops it"
            )
        if turned_off(home):
            return (
                f"timer {TIMER}: turned off by you ({PROG} auto off; "
                f"snapshots setup leaves it off; {PROG} auto on turns it on)"
            )
        return f"timer {TIMER}: not installed ({PROG} auto on)"
    en = systemctl("is-enabled", TIMER)
    ac = systemctl("is-active", TIMER)
    txt = f"timer {TIMER}: {en or 'unknown'}, {ac or 'unknown'}"
    if ac == "active":
        nxt = systemctl("show", "-p", "NextElapseUSecRealtime", "--value", TIMER)
        if nxt and nxt not in ("n/a", "0"):
            txt += f", next {nxt}"
    return txt


def config_items(home: str) -> list[tuple[str, str, str, str]]:
    """[(name, path, state, text to append)]; state: set | by-hand | write |
    unreadable."""
    out: list[tuple[str, str, str, str]] = []
    # (name, file, "our setting is there", "the table is there, set by hand", what apply appends)
    for name, rel, ours, by_hand, append in (
        ("cargo", CARGO_CFG_REL, CARGO_HAS_BUILD_DIR, CARGO_HAS_BUILD, cargo_text(home)),
        ("uv", UV_CFG_REL, UV_HAS_LINK_MODE, UV_HAS_TABLE, UV_TEXT),
    ):
        path = os.path.join(home, rel)
        text = read_cfg(path)
        if text is None:
            st = "unreadable"
        elif ours.search(text):
            st = "set"
        elif by_hand.search(text):
            st = "by-hand"
        else:
            st = "write"
        out.append((name, path, st, append))
    return out


def cargo_build_dir_is_csts(home: str) -> bool:
    try:
        with open(os.path.join(home, CARGO_CFG_REL), "rb") as f:
            build = tomllib.load(f).get("build")
    except OSError, tomllib.TOMLDecodeError:
        return False
    return isinstance(build, dict) and build.get("build-dir") == expected_template(home)


def lean_state(home: str) -> LeanState:
    """{path, add: [(label, block)], have: [label], conflicts: [text],
    append: text or '' (when appending is safe), error: ''}."""
    path = os.path.join(home, CARGO_CFG_REL)
    st: LeanState = {"path": path, "add": [], "have": [], "conflicts": [], "append": "", "error": ""}
    text = read_cfg(path)
    if text is None:
        st["error"] = "cannot be read"
        return st
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    for label_, tpath, want, block in LEAN_KEYS:
        table = dig(data, tpath)
        if isinstance(table, dict) and "debug" in table:
            if same_debug(table["debug"], want):
                st["have"].append(label_)
            else:
                st["conflicts"].append(f"[{label_}] debug = {json.dumps(table['debug'])} is yours: left as it is")
        else:
            st["add"].append((label_, block))
    if st["add"]:
        extra = LEAN_HEAD + "\n".join(b for _, b in st["add"])
        new = text + ("" if not text or text.endswith("\n") else "\n") + extra
        # Appended tables must not clash with the file's (TOML forbids a
        # table twice): parse the result and compare.
        try:
            nd = tomllib.loads(new)
        except tomllib.TOMLDecodeError:
            nd = None
        expect = json.loads(json.dumps(data))
        for label_, tpath, want, _ in LEAN_KEYS:
            if any(label_ == a for a, _ in st["add"]):
                d = expect
                for k in tpath:
                    d = d.setdefault(k, {})
                d["debug"] = want
        if nd is not None and json.loads(json.dumps(nd)) == expect:
            st["append"] = extra
    return st


def _flag_wanted(settings: Settings | None, flag: bool | None, option: str, name: str) -> tuple[bool, str]:
    """(wanted, why) of a yes/no setting: the --NAME / --no-NAME option when given, else OPTION."""
    if flag is not None:
        return flag, (f"--{name}" if flag else f"--no-{name}")
    if settings is None:
        return False, "bad settings"
    val = bool(settings.options[option])
    return val, f"{option}=" + ("yes" if val else "no")


def lean_wanted(settings: Settings | None, lean: bool | None) -> tuple[bool, str]:
    return _flag_wanted(settings, lean, "RUST_LEAN_DEBUGINFO", "lean-debuginfo")


def lean_lines(home: str, verb: str, settings: Settings | None, lean: bool | None) -> list[tuple[str, str, str]]:
    st = lean_state(home)
    shown = tilde(st["path"], home)
    want, why = lean_wanted(settings, lean)
    if st["error"]:
        return [("?", shown, f"lean debug info: the file {st['error']}")]
    if not want:
        if not st["add"] and not st["conflicts"]:
            return [("on", shown, f"lean debug info is set ({why} does not remove it: delete the lines by hand)")]
        return [
            (
                "off",
                shown,
                "lean debug info: off (opt-in; RUST_LEAN_DEBUGINFO=yes in ~/"
                + CONF_REL
                + f", or config apply --lean-debuginfo); {LEAN_REBUILD}",
            )
        ]
    out: list[tuple[str, str, str]] = [("keep", shown, c) for c in st["conflicts"]]
    if not st["add"]:
        if not st["conflicts"]:
            out.append(("ok", shown, f"lean debug info set ({why})"))
        return out
    what = "; ".join(b.replace("\n", " ").strip() for _, b in st["add"])
    if st["append"]:
        out.append(
            (
                "write",
                shown,
                f"lean debug info ({why}): {what}; {LEAN_REBUILD}"
                + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
            )
        )
    else:
        out.append(
            ("BY HAND", shown, f"lean debug info ({why}): the file already has these tables: add yourself: {what}")
        )
    return out


def lean_apply(home: str, settings: Settings | None, lean: bool | None) -> int:
    want, why = lean_wanted(settings, lean)
    if not want:
        return 0
    st = lean_state(home)
    path = st["path"]
    if st["error"]:
        say(f"{NAME}: lean debug info: {path} {st['error']}: left as it is", 3, err=True)
        return 1
    for c in st["conflicts"]:
        say(f"NOTE: lean debug info: {path}: {c}", 5, err=True)
    if not st["add"]:
        if not st["conflicts"]:
            say(f"kept:      {path} (lean debug info already set)")
        return 0
    if not st["append"]:
        say(f"NOTE: lean debug info: {path} already has these tables: add by hand:", 5, err=True)
        for _, block in st["add"]:
            for line in block.splitlines():
                say(f"      {line}", 5, err=True)
        return 0
    err = append_text(path, st["append"])
    if err:
        say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
        return 1
    say(f"written:   {path} (lean debug info, {why}; the next dev build of each workspace compiles everything once)")
    return 0


def wrapper_text(values: WrapperValues) -> str | None:
    """The wrapper as installed (the settings written in), or None when its
    source is not next to this program (the timer's copy has none)."""
    text = read_text(os.path.join(Program.folder(), WRAPPER_NAME))
    if text is None:
        return None
    text, n = SCCACHE_SIZE_LINE.subn(f"cache_size={values.size}", text, count=1)
    text, m = SCCACHE_WORKSPACE_LINE.subn(f"workspace={'yes' if values.workspace else 'no'}", text, count=1)
    return text if n == m == 1 else None


def wrapper_state(home: str, text: str) -> str:
    """ok | change | missing | conflict (not CST's; a symlink counts by what it points to)."""
    path = wrapper_path(home)
    st = file_state(path, text)
    if st == "ok":
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != 0o755:
                st = "change"
        except OSError:
            st = "change"
    return st


def wrapper_values(settings: Settings | None) -> WrapperValues:
    """No settings (a bad settings file; only --sccache/--no-sccache counts then): the defaults."""
    if not settings:
        return WrapperValues()
    return WrapperValues(
        str(settings.options["RUST_SCCACHE_CACHE_SIZE"]), bool(settings.options["RUST_SCCACHE_WORKSPACE"])
    )


def sccache_state(home: str) -> SccacheState:
    """{path, error, value (build.rustc-wrapper now, or None), ours, add,
    remove}: add / remove are the whole new file text, '' when CST cannot
    make that change safely (each candidate is parsed again and must equal
    the file with only that one key added or removed)."""
    path = os.path.join(home, CARGO_CFG_REL)
    st: SccacheState = {"path": path, "error": "", "value": None, "ours": False, "add": "", "remove": ""}
    text = read_cfg(path)
    if text is None:
        st["error"] = "cannot be read"
        return st
    try:
        data = plain(tomllib.loads(text))
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    want = wrapper_path(home)
    build = data.get("build")
    if build is not None and not isinstance(build, dict):
        return st  # build = 1: no add, no remove
    value = (build or {}).get("rustc-wrapper")
    st["value"], st["ours"] = value, value == want
    keyline = f"rustc-wrapper = {json.dumps(want)}"
    if value is None:
        expect = plain(data)
        expect.setdefault("build", {})["rustc-wrapper"] = want
        headers = list(BUILD_HEADER.finditer(text))
        cands: list[str] = []
        if len(headers) == 1:
            h = headers[0]
            nxt = ANY_HEADER.search(text, h.end())
            section_end = nxt.start() if nxt else len(text)
            keys = list(KEY_LINE.finditer(text, h.end(), section_end))
            # After the table's last key (the build-dir line CST writes),
            # else right after the header.
            spots = ([text.find("\n", keys[-1].start())] if keys else []) + [h.end()]
            for pos in spots:
                if pos < 0:
                    pos = len(text)
                rest = text[pos:]
                cands.append(text[:pos] + "\n" + SCCACHE_HEAD + "\n" + keyline + (rest if rest else "\n"))
        elif not headers:
            sep = "" if not text or text.endswith("\n") else "\n"
            cands.append(text + sep + ("\n" if text else "") + SCCACHE_HEAD + "\n[build]\n" + keyline + "\n")
        st["add"] = next((c for c in cands if parses_as(c, expect)), "")
    elif st["ours"]:
        lines = text.splitlines(keepends=True)
        pat = re.compile(rf"{WS}*rustc-wrapper{WS}*={WS}*{re.escape(json.dumps(want))}{WS}*(?:#.*)?\n?\Z")
        idx = [i for i, ln in enumerate(lines) if pat.match(ln)]
        if len(idx) == 1:
            i = idx[0]
            drop = {i}
            if i > 0 and lines[i - 1].startswith(SCCACHE_HEAD[:48]):
                drop.add(i - 1)
            # CST's own appended block (a [build] header whose own section
            # holds only this key; [build.x] subtables may follow): all of it.
            after: list[str] = []
            for ln in lines[i + 1 :]:
                if ANY_HEADER.match(ln):
                    break
                after.append(ln)
            if (
                i > 1
                and BUILD_HEADER.match(lines[i - 1].rstrip("\r\n"))
                and lines[i - 2].startswith(SCCACHE_HEAD[:48])
                and not any(KEY_LINE.match(ln) for ln in after)
            ):
                drop |= {i - 1, i - 2}
                if i > 2 and not lines[i - 3].strip():
                    drop.add(i - 3)
            new = "".join(ln for n, ln in enumerate(lines) if n not in drop)
            expect = plain(data)
            del expect["build"]["rustc-wrapper"]
            alt = {k: v for k, v in expect.items() if not (k == "build" and v == {})}
            if parses_as(new, expect) or parses_as(new, alt):
                st["remove"] = new
    return st


def sccache_wanted(settings: Settings | None, flag: bool | None) -> tuple[bool, str]:
    return _flag_wanted(settings, flag, "RUST_SCCACHE", "sccache")


def wrapper_lines(home: str, verb: str, text: str | None, values: WrapperValues) -> list[tuple[str, str, str]]:
    shown = tilde(wrapper_path(home), home)
    if text is None:
        return [
            (
                "?",
                shown,
                f"sccache wrapper: its source ({WRAPPER_NAME} next to this program) is missing: "
                f"run {PROG} config apply from CST",
            )
        ]
    st = wrapper_state(home, text)
    if st == "ok":
        return [("ok", shown, f"sccache wrapper up to date ({values})")]
    if st == "conflict":
        return [("keep", shown, "not CST's file: left alone, so CST writes no rustc-wrapper line")]
    return [
        (
            "write",
            shown,
            f"sccache wrapper ({'missing' if st == 'missing' else 'outdated'}; {values})"
            + ("" if verb == "plan" else f" (fix: {PROG} config apply)"),
        )
    ]


def sccache_lines(home: str, verb: str, settings: Settings | None, flag: bool | None) -> list[tuple[str, str, str]]:
    st = sccache_state(home)
    shown = tilde(st["path"], home)
    want, why = sccache_wanted(settings, flag)
    values = wrapper_values(settings)
    off = f"sccache: off (opt-in; RUST_SCCACHE=yes in ~/{CONF_REL}, or config apply --sccache); {SCCACHE_REBUILD}"
    if st["error"]:
        return [("?", shown, f"sccache: the file {st['error']}")]
    foreign = st["value"] is not None and not st["ours"]
    yours = f"rustc-wrapper = {json.dumps(st['value'])} is yours: left as it is"
    if flag is False:
        if st["ours"]:
            if st["remove"]:
                return [
                    (
                        "remove",
                        shown,
                        f"sccache: CST's rustc-wrapper line ({why}); {SCCACHE_REBUILD}; "
                        f"~/{WRAPPER_REL} stays (unused, harmless)",
                    )
                ]
            return [
                (
                    "BY HAND",
                    shown,
                    f"sccache ({why}): CST cannot remove its rustc-wrapper line safely: delete it yourself",
                )
            ]
        return [("keep", shown, f"sccache ({why}): {yours}")] if foreign else [("off", shown, "sccache: off")]
    text = wrapper_text(values)
    if not want:
        if st["ours"]:
            out = [
                (
                    "on",
                    shown,
                    f"sccache is on (CST's rustc-wrapper; {why} does not remove it: "
                    f"{PROG} config apply --no-sccache does)",
                )
            ]
            return out + [ln for ln in wrapper_lines(home, verb, text, values) if ln[0] != "ok"]
        return [("off", shown, off + (f"; {yours}" if foreign else ""))]
    if foreign:
        return [("keep", shown, f"sccache ({why}): {yours} (CST never overwrites it)")]
    out = wrapper_lines(home, verb, text, values)
    if st["ours"]:
        out.append(("ok", shown, f"sccache: rustc-wrapper set ({why})"))
    elif st["add"]:
        out.append(
            (
                "write",
                shown,
                f'sccache ({why}): [build] rustc-wrapper = "~/{WRAPPER_REL}"; {SCCACHE_REBUILD}'
                + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
            )
        )
    else:
        out.append(
            (
                "BY HAND",
                shown,
                f"sccache ({why}): CST cannot add the key safely here: put "
                f'rustc-wrapper = "{wrapper_path(home)}" into [build] yourself',
            )
        )
    return out


def install_wrapper(home: str, values: WrapperValues) -> int:
    """0 when the wrapper is in place (written or unchanged), else 1."""
    text = wrapper_text(values)
    path = wrapper_path(home)
    if text is None:
        say(
            f"{NAME}: sccache: {WRAPPER_NAME} is missing next to this program: run {PROG} config apply from CST",
            3,
            err=True,
        )
        return 1
    st = wrapper_state(home, text)
    if st == "conflict":
        say(f"{NAME}: sccache: {path} is not CST's file: left alone", 3, err=True)
        return 1
    if st == "ok":
        say(f"kept:      {path} (sccache wrapper)")
        return 0
    try:
        write_atomic(path, text, mode=0o755)
    except OSError as e:
        say(f"{NAME}: cannot write {path}: {e.strerror or e}", 3, err=True)
        return 1
    say(f"written:   {path} (sccache wrapper, {values})")
    return 0


def sccache_apply(home: str, settings: Settings | None, flag: bool | None) -> int:
    want, why = sccache_wanted(settings, flag)
    st = sccache_state(home)
    path = st["path"]
    if st["error"]:
        if want or flag is False or st["ours"]:
            say(f"{NAME}: sccache: {path} {st['error']}: left as it is", 3, err=True)
            return 1
        return 0
    if flag is False:
        if not st["ours"]:
            return 0
        if not st["remove"]:
            say(
                f"NOTE: sccache: {path}: CST cannot remove its rustc-wrapper line safely: delete it yourself",
                5,
                err=True,
            )
            return 0
        err = write_cfg(path, st["remove"])
        if err:
            say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
            return 1
        say(f"removed:   {path} (CST's rustc-wrapper line, {why}; ~/{WRAPPER_REL} stays, unused)")
        return 0
    if not want:
        # The line is there (set earlier): keep its wrapper current, so cargo
        # never names a missing program.
        return install_wrapper(home, wrapper_values(settings)) if st["ours"] else 0
    if st["value"] is not None and not st["ours"]:
        say(
            f"NOTE: sccache: {path}: rustc-wrapper = {json.dumps(st['value'])} is yours: left as it is "
            f"(sccache not set up)",
            5,
            err=True,
        )
        return 0
    # The wrapper first: the line must never name a missing program.
    if install_wrapper(home, wrapper_values(settings)):
        return 1
    if st["ours"]:
        say(f"kept:      {path} (rustc-wrapper already set)")
        return 0
    if not st["add"]:
        say(f"NOTE: sccache: {path}: CST cannot add the key safely: put this into [build] by hand:", 5, err=True)
        say(f"      rustc-wrapper = {json.dumps(wrapper_path(home))}", 5, err=True)
        return 0
    err = write_cfg(path, st["add"])
    if err:
        say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
        return 1
    say(f"written:   {path} (rustc-wrapper, {why}: rustc goes through sccache where it is on PATH)")
    return 0


def sccache_status_lines(home: str) -> list[str]:
    """The cache's size and, with sccache on this PATH, one line of stats
    per running server (status runs on the host: its PATH)."""
    cache = os.path.join(home, SCCACHE_DIR_REL)
    out: list[str] = []
    if os.path.isdir(cache):
        out.append(
            f"sccache cache: {where_text(cache, home)} {gib(tree_size(cache))} (sccache keeps it under "
            f"its size cap itself, least recently used first; ~/.cache is not in /home snapshots)"
        )
    else:
        out.append(f"sccache cache: ~/{SCCACHE_DIR_REL} not there yet")
    exe = shutil.which("sccache") or ""
    if not exe:
        # `cargo install sccache` lands in ~/.cargo/bin, which a PATH may lack.
        ch = os.environ.get("CARGO_HOME", "")
        c = os.path.join(ch if os.path.isabs(ch) else os.path.join(home, ".cargo"), "bin", "sccache")
        exe = c if os.access(c, os.X_OK) else ""
    socks = [p for p in sccache_sockets(home) if socket_answers(p)]
    if not exe:
        if socks:
            out.append(f"sccache servers running: {len(socks)} (no sccache on the host's PATH to ask them)")
        return out
    if not socks:
        out.append("sccache: no server running (each environment starts its own on its first build)")
    for sock in socks:
        env_id = os.path.basename(sock)[len("cst-sccache-") : -len(".sock")]
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCCACHE_")}
        # TMPDIR=/tmp as in rustc-wrapper: if the server stops right after the
        # socket check, this client call starts a new one, which keeps its TMPDIR.
        env.update(SCCACHE_SERVER_UDS=sock, NO_COLOR="1", TMPDIR="/tmp")
        try:
            r = subprocess.run(
                [exe, "--show-stats"],
                check=False,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            stats = dict(SCCACHE_STATS.findall(r.stdout)) if r.returncode == 0 else {}
        except OSError, subprocess.TimeoutExpired:
            stats = {}
        if not stats:
            out.append(f"sccache server {env_id}: no answer")
            continue
        shown = ", ".join(
            f"{k.lower()} {stats[k]}"
            for k in ("Compile requests", "Cache hits", "Cache misses", "Cache size", "Max cache size")
            if k in stats
        )
        out.append(f"sccache server {env_id}: {shown}")
    return out


def config_lines(
    home: str,
    verb: str,
    settings: Settings | None = None,
    lean: bool | None = None,
    sccache: bool | None = None,
) -> list[tuple[str, str, str]]:
    """The plan or status lines of the settings (lean, sccache: the explicit
    flags)."""
    out: list[tuple[str, str, str]] = []
    for name, path, st, _ in config_items(home):
        shown = tilde(path, home)
        if name == "cargo":
            if st == "set":
                if cargo_build_dir_is_csts(home):
                    out.append(("ok", shown, "build-dir = CST's line (rust:build prunes that folder)"))
                else:
                    out.append(
                        (
                            "keep",
                            shown,
                            "build-dir set to your own value: left as it is (rust:build then prunes nothing)",
                        )
                    )
            elif st == "by-hand":
                out.append(
                    (
                        "BY HAND",
                        shown,
                        f'has a [build] table: add build-dir = "{home}/{ROOT_REL}/{HASH_VAR}" there yourself',
                    )
                )
            elif st == "write":
                out.append(
                    (
                        "write",
                        shown,
                        f'[build] build-dir = "~/{ROOT_REL}/{HASH_VAR}"'
                        + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
                    )
                )
            else:
                out.append(("?", shown, "cannot be read"))
        else:
            if st == "set":
                out.append(("ok", shown, "link-mode already set"))
            elif st == "by-hand":
                out.append(("BY HAND", shown, 'has tables: add link-mode = "clone" at its top yourself'))
            elif st == "write":
                out.append(
                    (
                        "write",
                        shown,
                        'link-mode = "clone" (hardlinks cannot cross subvolumes)'
                        + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
                    )
                )
            else:
                out.append(("?", shown, "cannot be read"))
    out += lean_lines(home, verb, settings, lean)
    out += sccache_lines(home, verb, settings, sccache)
    return out


def cmd_config(home: str, action: str, lean: bool | None = None, sccache: bool | None = None) -> int:
    settings: Settings | None
    try:
        settings = Settings(home)
    except ConfigError as e:
        settings = None
        off = [w for w, flag in (("lean debug info", lean), ("sccache", sccache)) if flag is None]
        if off:
            say(f"note: BAD SETTINGS ({e}): {' and '.join(off)} treated as off", 4, err=True)
    if action == "plan":
        lines = config_lines(home, "plan", settings, lean, sccache)
        for st, shown, note in lines:
            say(f"  {st:<8} {shown:<24} {note}")
        if any(st in ("write", "remove") for st, _, _ in lines):
            flags = {True: " --lean-debuginfo", False: " --no-lean-debuginfo"}.get(lean, "")
            flags += {True: " --sccache", False: " --no-sccache"}.get(sccache, "")
            say(f"Apply with: {PROG} config apply" + flags)
        return 0
    rc = 0
    for name, path, st, text in config_items(home):
        if st == "unreadable":
            say(f"{NAME}: cannot read {path}: left as it is", 3, err=True)
            rc = 1
        elif name == "cargo" and st == "set":
            say(f"kept:      {path} (build-dir already set)")
        elif name == "cargo" and st == "by-hand":
            say(f"NOTE: {path} already configures [build]: add there by hand:", 5, err=True)
            say(f'      build-dir = "{home}/{ROOT_REL}/{HASH_VAR}"', 5, err=True)
        elif name == "uv" and st == "set":
            say(f"kept:      {path} (link-mode already set)")
        elif name == "uv" and st == "by-hand":
            say(f'NOTE: {path} has tables: add link-mode = "clone" at its top by hand', 5, err=True)
        else:
            err = append_text(path, text)
            if err:
                # For example a ~/.cargo owned by root after a `sudo cargo`:
                # report it and go on with the other setting.
                say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
                rc = 1
                continue
            say(f"written:   {path}")
    rc = max(rc, lean_apply(home, settings, lean))
    return max(rc, sccache_apply(home, settings, sccache))


def cmd_status(home: str, now: float, brief: bool) -> int:
    say(f"auto: {timer_text(home)}")
    settings: Settings | None
    try:
        settings = Settings(home)
    except ConfigError as e:
        settings = None
        say(f"BAD SETTINGS: {e} (prune and the timer do nothing until it is fixed)")
    for tid, cls in TARGETS.items():
        s = read_state(home, tid)
        last = cls.last_run_text(s) if s else "none recorded"
        if brief:
            say(f"{tid}: last prune: {last}")
            continue
        say(f"== {tid}: {cls.WHAT}")
        t = cls(home, now, settings.policy[tid] if settings else None)
        say(f"  where:    {where_text(t.location(), home)}")
        if settings:
            state = "enabled" if settings.enabled[tid] else "disabled (prune runs it only when named)"
            say(f"  policy:   {state}; {cls.policy_text(settings.policy[tid])} ({settings.source_of(cls)})")
        else:
            say("  policy:   BAD SETTINGS (see above)")
        say(f"  last prune: {last}")
        for line in t.status_lines():
            say(f"  {line}")
    if brief:
        return 0
    say("== not implemented yet (nothing pruned there by CST):")
    for tid, (where, how) in PLANNED.items():
        shown = ", ".join(where_text(os.path.join(home, p.strip()[2:]), home) for p in where.split(","))
        say(f"  {tid:<14} {shown} ({how})")
    say("== rust toolchains (a report: CST never removes one)")
    for line in toolchain_lines(home):
        say(f"  {line}")
    say(f"== toolchain settings ({PROG} config)")
    for st, shown, note in config_lines(home, "status", settings):
        say(f"  {st:<8} {shown:<24} {note}")
    for line in sccache_status_lines(home):
        say(f"  {line}")
    st = unit_states(home)
    say("== files of auto on: " + ", ".join(f"{label(k)} {v}" for k, v in st.items()))
    for path, ours in stale_files(home):
        say(
            f"  stale: {tilde(path, home)} (of the earlier `cst btrfs cargo-gc`"
            + ("; auto on or auto off removes it)" if ours else "; not CST's: left alone)")
        )
    old_conf_note(home)
    return 0


def run_locked(home: str, fn: Callable[[], int], busy_refused: bool = False) -> int:
    """One run at a time (the timer and a manual apply, auto on/off). When
    another run holds the lock: nothing done, exit 0 (a prune that finds
    another prune running), or Refused with busy_refused (auto on/off: what
    was asked did not happen)."""
    path = os.path.join(home, STATE_DIR_REL, "toolchains.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if busy_refused:
                raise Refused(
                    "another run is in progress (the hourly prune, or another auto on/off): nothing done; "
                    f"run this again once it has finished (journalctl --user -u {SERVICE} shows the prune)"
                ) from None
            say("another run is in progress: nothing done", 5)
            return 0
        return fn()
    finally:
        os.close(fd)


def main(argv: Sequence[str], doc: str, entry_file: str) -> int:
    """`cst toolchains ...` (argv without the program name). doc is the
    program's help; entry_file the running cst-toolchains."""
    Program.entry_file = entry_file
    try:
        a = parse_args(argv)
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    output_mode(a["cmd"], a["action"])
    if a["cmd"] == "help":
        print(doc)
        return 0
    if os.getuid() == 0:
        print(f"{NAME}: run as your user, not as root", file=sys.stderr)
        return 1
    if in_container() and not os.environ.get("CST_TOOLCHAINS_HERE"):
        # From a toolbox, the processes of other containers cannot be
        # inspected: the same program runs on the host (same home path).
        try:
            os.execvp(
                "flatpak-spawn", ["flatpak-spawn", "--host", f"--env=CST_PROG={PROG}", *program_argv(entry_file), *argv]
            )
        except OSError as e:
            print(f"{NAME}: cannot run on the host (flatpak-spawn: {e.strerror}); run it on the host", file=sys.stderr)
            return 1
    home, now = run_context(a["home"])
    cmd, action = a["cmd"], a["action"]
    try:
        if cmd == "status":
            return cmd_status(home, now, a["brief"])
        assert action is not None
        if cmd == "config":
            return cmd_config(home, action, a["lean"], a["sccache"])
        if cmd == "prune" and action == "plan":
            return cmd_prune(home, now, "plan", a["targets"])
        if cmd == "prune":
            return run_locked(home, lambda: cmd_prune(home, now, "apply", a["targets"]))
        if action == "on":
            return run_locked(home, lambda: cmd_auto_on(home, now, a["unless_off"]), busy_refused=True)
        return run_locked(home, lambda: cmd_auto_off(home, now), busy_refused=True)
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    except (Refused, ConfigError) as e:
        say(f"{NAME}: {e}", 3, err=True)
        return 1
