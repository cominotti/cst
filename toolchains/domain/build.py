# SPDX-License-Identifier: MPL-2.0
"""The rules of rust:build, no I/O: cargo's build folders (their names, the
leftovers of a run, the incremental folders), cargo processes, the workspaces
that claim a folder, the pruning decision (decide), and the names of rust
toolchains for the status report.
"""

from __future__ import annotations

import dataclasses
import os
import re
import stat
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from cst_core.domain.values import MIB
from toolchains.domain.common import (
    ConfigError,
    ago,
    gib,
    parse_bool,
    parse_duration,
    parse_off_or_duration,
    parse_size,
    span,
)


HASH_VAR = "{workspace-path-hash}"
ROOT_REL = ".cache/cargo-build"
MIN_KEEP_FLOOR = 15 * 60
FUTURE_SLACK = 300
HASH_TOP = re.compile(r"[0-9a-f]{2}\Z")
HASH_LEAF = re.compile(r"[0-9a-f]{14}\Z")
LOCK_NAMES = (".cargo-build-lock", ".cargo-lock")
TRASH_PREFIX = ".cst-toolchains-trash-"
# A whole folder: <prefix><16 hex>-<time>; its incremental dirs: <prefix><16 hex>-inc<n>-<time>.
TRASH_NAME = re.compile(re.escape(TRASH_PREFIX) + r"[0-9a-f]{16}(?:-inc\d+)?-\d+\Z")
INCREMENTAL = "incremental"
# Processes whose state we must know (comm is readable even when the rest is not).
RUST_TOOLS = re.compile(r"(cargo|rustc|rustdoc|clippy-driver|rust-analyzer|build-script|build_script).*")


def expected_template(home: str) -> str:
    return f"{home}/{ROOT_REL}/{HASH_VAR}"


@dataclasses.dataclass(frozen=True, slots=True)
class RustPolicy:
    """rust:build's checked settings, in seconds and bytes: MAX_IDLE, MAX_TOTAL (0: no
    cap), MIN_KEEP, ORPHANS and INCREMENTAL_IDLE (0: off)."""

    max_idle: int
    max_total: int
    min_keep: int
    orphans: bool = True
    incremental_idle: int = 0

    @staticmethod
    def parse(vals: Mapping[str, str], prefix: str) -> RustPolicy:
        """The policy from {SETTING: raw value}; ConfigError for a bad value, or for a rule
        that contradicts another."""
        pol = RustPolicy(
            max_idle=parse_duration(vals["MAX_IDLE"]),
            max_total=parse_size(vals["MAX_TOTAL"]),
            min_keep=parse_duration(vals["MIN_KEEP"]),
            orphans=parse_bool(vals["ORPHANS"]),
            incremental_idle=parse_off_or_duration(vals["INCREMENTAL_IDLE"]),
        )
        if pol.min_keep < MIN_KEEP_FLOOR:
            raise ConfigError(f"{prefix}_MIN_KEEP={vals['MIN_KEEP']} is below the floor of 15m")
        if pol.max_idle < pol.min_keep:
            raise ConfigError(
                f"{prefix}_MAX_IDLE={vals['MAX_IDLE']} is shorter than {prefix}_MIN_KEEP={vals['MIN_KEEP']}"
            )
        if pol.incremental_idle and pol.incremental_idle < pol.min_keep:
            raise ConfigError(
                f"{prefix}_INCREMENTAL_IDLE={vals['INCREMENTAL_IDLE']} is shorter than "
                f"{prefix}_MIN_KEEP={vals['MIN_KEEP']} (0 or off turns it off)"
            )
        return pol

    def text(self) -> str:
        cap = gib(self.max_total) if self.max_total else "off"
        inc = span(self.incremental_idle) if self.incremental_idle else "off"
        orph = "on" if self.orphans else "off"
        return (
            f"MAX_IDLE {span(self.max_idle)}, MAX_TOTAL {cap}, MIN_KEEP {span(self.min_keep)}, "
            f"ORPHANS {orph}, INCREMENTAL_IDLE {inc}"
        )


class IncrementalDir:
    """One of cargo's incremental folders in a build folder (<profile>/incremental or
    <triple>/<profile>/incremental) and its size, summed while the folder is measured."""

    __slots__ = ("path", "size")

    def __init__(self, path: str, size: int = 0) -> None:
        self.path: str = path
        self.size: int = size


class Folder:
    def __init__(self, root: str, top: str, leaf: str) -> None:
        self.key: str = f"{top}/{leaf}"
        self.path: str = os.path.join(root, top, leaf)
        self.size: int = 0
        self.activity: float = 0.0
        self.entries: int = 0
        self.problem: str = ""
        self.future: int = 0
        self.foreign: int = 0
        # Cargo's incremental folders inside: <profile>/incremental and
        # <triple>/<profile>/incremental.
        self.inc: list[IncrementalDir] = []

    @property
    def inc_size(self) -> int:
        return sum(d.size for d in self.inc)

    def __repr__(self) -> str:
        return f"Folder({self.key}, {self.size}, {self.activity})"


def classify(name: str, st: os.stat_result, dev: int, pattern: re.Pattern[str]) -> str:
    """Why an entry is not a hash folder, or "" if it is one."""
    if not pattern.match(name):
        return "not a cargo hash name"
    if stat.S_ISLNK(st.st_mode):
        return "a symlink (refused)"
    if not stat.S_ISDIR(st.st_mode):
        return "not a folder"
    if st.st_dev != dev:
        return "on another filesystem"
    return ""


def prefixes(root: str) -> tuple[str, ...]:
    """The root as a process may see it: /home is a symlink to /var/home on
    Silverblue and in toolbox containers."""
    out = {root}
    if root.startswith("/var/home/"):
        out.add(root[len("/var") :])
    elif root.startswith("/home/"):
        out.add("/var" + root)
    return tuple(sorted(out))


# Cargo holds its build-dir lock only while it compiles. While `cargo test`,
# `cargo bench` or `cargo nextest` run the test binaries, the lock is free and
# cargo itself has nothing open in the build folder; between two test
# binaries nothing points there at all. So every running cargo process is
# asked about (`cargo metadata`, the same cargo, folder and settings): the
# build folder it uses counts as in use while it runs.
CARGO_COMM = re.compile(r"cargo(-.+)?")
CARGO_ENV = ("CARGO_HOME", "CARGO_BUILD_BUILD_DIR", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN")


@dataclasses.dataclass(frozen=True, slots=True)
class CargoProc:
    """A cargo (or cargo-*) process of the user, as /proc shows it: cwd None when it
    cannot be read; env holds only the CARGO_ENV variables it was started with. read:
    False when its command line or its environment came back empty or could not be
    read (a process that is starting or ending, or one started with no environment):
    then which folder it builds in is unknown (its --manifest-path and its cargo
    settings are not known)."""

    pid: str
    comm: str
    cwd: str | None = None
    exe: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)
    read: bool = True


# The answers of one run are kept under (command, folder, settings).
type CargoKey = tuple[tuple[str, ...], str, tuple[tuple[str, str], ...]]
type CargoCache = dict[CargoKey, tuple[str | None, str | None]]


@dataclasses.dataclass(frozen=True, slots=True)
class CargoQuery:
    """The read-only `cargo metadata` that tells which build folder a cargo process uses."""

    key: CargoKey
    cmd: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str


def manifest_arg(args: Sequence[str], cwd: str) -> str:
    """--manifest-path of a cargo command line (absolute), or ''."""
    for i, a in enumerate(args):
        if a == "--":
            break
        val = None
        if a == "--manifest-path" and i + 1 < len(args):
            val = args[i + 1]
        elif a.startswith("--manifest-path="):
            val = a.split("=", 1)[1]
        if val:
            return os.path.normpath(os.path.join(cwd, val))
    return ""


def unknown_text(unknown: Sequence[tuple[int | str, str, str | None, str]]) -> str:
    pid, comm, cwd, why = unknown[0]
    more = f" (and {len(unknown) - 1} more)" if len(unknown) > 1 else ""
    return f"{comm} (pid {pid}) in {cwd or '?'}: its build folder is unknown ({why}){more}"


def merge_users(*maps: Mapping[str, Sequence[tuple[int | str, str]]]) -> dict[str, list[tuple[int | str, str]]]:
    out: dict[str, list[tuple[int | str, str]]] = {}
    for m in maps:
        for k, v in m.items():
            lst = out.setdefault(k, [])
            lst += [u for u in v if u not in lst]
    return out


# A build folder is named after the path of a workspace's Cargo.toml. When no
# workspace on disk computes that name any more (its worktree was removed),
# the folder is an orphan: nothing will ever build there again. The
# workspaces are the git worktrees of every repository under ~/Workspace
# (`git worktree list` also names linked worktrees elsewhere, e.g. inside a
# target/ folder); each Cargo.toml in them is asked, read only, which build
# folder it uses (`cargo metadata --offline --no-deps`, never a download, never
# a dependency resolution). If any step fails, nothing is an orphan in that run.
WORKSPACE_REL = "Workspace"
# Folders never walked (neither for repositories nor for Cargo.toml files).
# Hidden folders are skipped too; worktrees inside them (.claude/worktrees/...)
# are still found, through `git worktree list`.
SCAN_SKIP = frozenset({"node_modules", "target", "__pycache__", "venv"})
REPO_DEPTH = 6  # levels below ~/Workspace searched for repositories
CRATE_DEPTH = 6  # levels below a worktree searched for Cargo.toml
CARGO_MANIFEST = "Cargo.toml"  # a crate's or a workspace's manifest
ENUM_BUDGET = 600  # seconds for the whole search; longer: incomplete
CMD_TIMEOUT = 60  # seconds for one git or cargo call
PIN_NAMES = ("rust-toolchain.toml", "rust-toolchain")
# Files that may name a toolchain without pinning it (`cargo +nightly fuzz`,
# TOOLCHAIN ?= nightly-2026-06-01, a CI job): the toolchains report reads
# them so that it never offers to uninstall a toolchain named there.
TOOL_FILE_NAMES = frozenset(
    {"Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", ".justfile", "Taskfile.yml", "Taskfile.yaml"}
)
TOOL_FILE_SUFFIXES = (".mk", ".just", ".sh", ".bash")
# Read too, from a worktree's top (walk_dirs skips hidden folders): CI jobs
# and cargo aliases. {folder: suffixes}
YAML = (".yml", ".yaml")
TOOL_FILE_DIRS = {
    ".github/workflows": YAML,
    ".cargo": ("config", "config.toml"),
    ".circleci": YAML,
    ".buildkite": YAML,
}
TOOL_FILE_TOP = (".gitlab-ci.yml",)
TOOL_FILE_MAX = MIB  # bytes read of one file
TOOL_FILES_MAX = 100000  # files read in all; more: the report counts as incomplete
# A folder with this file is a cache (cargo's target/ and registry, ...): not searched.
CACHE_TAG = "CACHEDIR.TAG"
NOT_A_MEMBER = "believes it's in a workspace when it's not"


class Incomplete(Exception):
    pass


class Workspaces:
    """What the search found. claimed: {folder key: [workspace root, ...]};
    incomplete: why the orphan rule must not run ('' when complete)."""

    def __init__(self) -> None:
        self.repos: int = 0
        self.worktrees: list[str] = []
        self.manifests: int = 0
        self.asked: int = 0
        self.claimed: dict[str, list[str]] = {}
        self.pins: list[str] = []  # rust-toolchain(.toml) files
        self.tool_files: list[str] = []  # files that may name a toolchain (TOOL_FILE_NAMES, ...)
        self.failures: list[tuple[str, str]] = []  # [(manifest, why)]
        self.incomplete: str = ""
        self.seconds: float = 0.0


def is_tool_file(name: str) -> bool:
    return name in TOOL_FILE_NAMES or name.endswith(TOOL_FILE_SUFFIXES)


class FolderAction(StrEnum):
    """What decide plans for one build folder: delete it, trim it (delete only its
    incremental folders) or keep it."""

    DELETE = "delete"
    TRIM = "trim"
    KEEP = "keep"


def decide(
    folders: Sequence[Folder],
    pol: RustPolicy,
    now: float,
    in_use: Mapping[str, Sequence[tuple[int | str, str]]],
    claimed: Mapping[str, Sequence[str]] | None = None,
) -> list[tuple[Folder, FolderAction, str]]:
    """[(folder, action, reason)] sorted least recently active first.
    action: 'delete', 'trim' (delete only its incremental folders) or
    'keep'. CLAIMED: {folder key: ...} of a complete workspace search, or
    None (no orphan rule). Pure: no file access."""
    order = sorted(folders, key=lambda f: (f.activity, f.key))
    act: dict[str, tuple[FolderAction, str]] = {}
    total = sum(f.size for f in folders)
    inc_idle = pol.incremental_idle
    for f in order:
        idle = now - f.activity
        if f.problem:
            act[f.key] = (FolderAction.KEEP, f"left alone: {f.problem}")
        elif f.key in in_use:
            act[f.key] = (FolderAction.KEEP, "in use: " + users_text(in_use[f.key]))
        elif idle < pol.min_keep:
            act[f.key] = (FolderAction.KEEP, f"active {ago(idle)} ago (within MIN_KEEP)")
        elif idle > pol.max_idle:
            act[f.key] = (FolderAction.DELETE, f"idle {ago(idle)} > MAX_IDLE")
            total -= f.size
        elif claimed is not None and pol.orphans and f.key not in claimed:
            act[f.key] = (FolderAction.DELETE, f"orphan: no workspace claims it (idle {ago(idle)})")
            total -= f.size
        elif inc_idle and idle > inc_idle and f.inc_size:
            act[f.key] = (
                FolderAction.TRIM,
                f"incremental of an idle folder (idle {ago(idle)}; frees {gib(f.inc_size)})",
            )
            total -= f.inc_size
    if pol.max_total and total > pol.max_total:
        for f in order:
            if total <= pol.max_total:
                break
            a = act.get(f.key, ("", ""))[0]
            if a in (FolderAction.KEEP, FolderAction.DELETE):
                continue
            act[f.key] = (
                FolderAction.DELETE,
                f"over MAX_TOTAL ({gib(total)} > {gib(pol.max_total)}), least recently active",
            )
            total -= f.size - (f.inc_size if a == FolderAction.TRIM else 0)
    out: list[tuple[Folder, FolderAction, str]] = []
    for f in order:
        a, why = act.get(f.key, (FolderAction.KEEP, f"active {ago(now - f.activity)} ago"))
        out.append((f, a, why))
    return out


def users_text(users: Sequence[tuple[int | str, str]]) -> str:
    shown = ", ".join(f"{comm} (pid {pid})" for pid, comm in users[:3])
    return shown + (f" and {len(users) - 3} more" if len(users) > 3 else "")


def after_total(plan: Sequence[tuple[Folder, FolderAction, str]]) -> int:
    return sum(f.size - (f.inc_size if a == FolderAction.TRIM else 0) for f, a, _ in plan if a != FolderAction.DELETE)


HOST_SUFFIX = re.compile(r"-(?:x86_64|aarch64|i686|armv7|riscv64gc|powerpc64le|s390x|loongarch64)-[\w.-]+\Z")


def short_toolchain(name: str) -> str:
    return HOST_SUFFIX.sub("", name)


def toolchain_pattern(name: str) -> re.Pattern[bytes]:
    """A toolchain name as a word (bytes): `+nightly`, `?= nightly-2026-06-01`,
    `toolchain: 1.98.0`; not `nightly` inside `nightly-2026-06-01`, not
    `1.98.0` inside `1.98.0.1`."""
    return re.compile(rb"(?<![\w.+-])\+?" + re.escape(name.encode()) + rb"(?![\w.]|-\d)")
