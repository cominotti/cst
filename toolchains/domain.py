# SPDX-License-Identifier: MPL-2.0
"""The rules of cst-toolchains (the program behind `cst toolchains`): names
and paths, the settings syntax, the pruning decision for cargo's build
folders, the texts CST writes (unit files, cargo and uv settings, the
sccache lines) and the command line. No I/O: the host is in
toolchains/adapters.py, the commands in toolchains/cli.py.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import time
import tomllib
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

from cst_core.domain.errors import Usage
from cst_core.domain.text import parse_shell_assignments
from cst_core.domain.units import render_timer as unit_timer, render_unit_file
from cst_core.domain.values import format_bytes_gib_mib

MARKER = "# managed-by: toolchains-setup.sh (CST)"
NAME = "cst-toolchains"
CONF_REL = ".config/cst/toolchains.conf"
# Left by `auto off` (and removed by `auto on`): `snapshots setup` then does
# not turn the timer back on (auto on --unless-off).
OFF_REL = ".config/cst/toolchains-auto.off"
LIB_REL = ".local/lib/cst"
PROG_REL = f"{LIB_REL}/{NAME}"
DEFAULTS_NAME = "toolchains.conf"
DEFAULTS_REL = f"{LIB_REL}/{DEFAULTS_NAME}"
UNIT_DIR_REL = ".config/systemd/user"
STATE_DIR_REL = ".local/state/cst"
SERVICE = "cst-toolchains-prune.service"
TIMER = "cst-toolchains-prune.timer"
# The link `enable` makes for the timer's WantedBy=timers.target.
WANTS_REL = f"{UNIT_DIR_REL}/timers.target.wants/{TIMER}"
# What the first version (`cst btrfs cargo-gc`, never deployed) installed:
# auto on and auto off remove it when its marker shows it is CST's.
OLD_MARKER = "# managed-by: btrfs-setup.sh (CST)"
OLD_TIMER = "cst-cargo-build-gc.timer"
OLD_UNITS = ("cst-cargo-build-gc.timer", "cst-cargo-build-gc.service")
OLD_FILES = (".local/lib/cst/cargo-build-gc", ".config/cst/cargo-gc.disabled")
OLD_DATA = (".local/state/cst/cargo-build-gc.json", ".local/state/cst/cargo-build-gc.lock")
OLD_CONF_REL = ".config/cst/cargo-gc.conf"
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


class ConfigError(Exception):
    pass


def gib(n: int | float) -> str:
    """GiB from 1 GiB on, else rounded MiB."""
    return format_bytes_gib_mib(n, mib_floor=False)


def ago(seconds: int | float) -> str:
    secs = max(0, int(seconds))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 4 * 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def span(seconds: int) -> str:
    """A setting's duration, short: 48h, 2h, 90m, 14d."""
    if seconds % 3600 == 0 and seconds < 7 * 86400:
        return f"{seconds // 3600}h"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def tilde(path: str, home: str) -> str:
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
SIZE_UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def parse_duration(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*", text, re.I)
    if not m:
        raise ConfigError(f"'{text}' is not a duration (a number with s, m, h, d or w, e.g. 48h)")
    return int(float(m.group(1)) * DURATION_UNITS[m.group(2).lower()])


def parse_size(text: str) -> int:
    """Bytes. The unit is required (only 0 may go without): MAX_TOTAL=150
    meant as 150G would otherwise be 150 bytes, a cap that deletes
    everything older than MIN_KEEP."""
    t = text.strip().lower()
    if re.fullmatch(r"0+(?:\.0*)?|off|none|no", t):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt])(?:i?b)?", t)
    if not m:
        raise ConfigError(f"'{text}' is not a size (a number with K, M, G or T, e.g. 150G; 0 or off for no cap)")
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)])


def parse_bool(text: str) -> bool:
    t = text.strip().lower()
    if t in ("yes", "true", "on", "1"):
        return True
    if t in ("no", "false", "off", "0"):
        return False
    raise ConfigError(f"'{text}' is not yes or no")


def parse_conf(text: str, known: AbstractSet[str]) -> dict[str, str]:
    """{KEY: raw value} through cst_core's one settings reader (a file only
    Python reads: blanks around = allowed; '#' starts a comment, also after a
    value behind a blank; quotes around the value dropped). The first line
    that is not an assignment, or that sets a key not in KNOWN, is an error:
    a typo must not silently fall back to a default."""
    parsed = parse_shell_assignments(text, bash_sourced=False)
    issues = [(p.line, str(p)) for p in parsed.problems]
    issues += [
        (a.line, f"line {a.line}: unknown setting '{a.key}' (known: {', '.join(sorted(known))})")
        for a in parsed.entries
        if a.key not in known
    ]
    if issues:
        raise ConfigError(min(issues)[1])
    return dict(parsed.values)


def parse_sccache_size(text: str) -> str:
    """A size for SCCACHE_CACHE_SIZE, in sccache's own spelling: a whole
    number with K, M, G or T (sccache counts them in 1024s and knows no
    fractions); 0 is refused (sccache needs a cap)."""
    n = parse_size(text)
    if n <= 0:
        raise ConfigError(f"'{text}' is no cache size (sccache needs one, e.g. 25G)")
    for unit in "TGM":
        if n % SIZE_UNITS[unit.lower()] == 0:
            return f"{n // SIZE_UNITS[unit.lower()]}{unit}"
    return f"{-(-n // SIZE_UNITS['k'])}K"


# Settings that belong to no prune target: {KEY: parser}.
OPTIONS: dict[str, Callable[[str], bool | str]] = {
    "RUST_LEAN_DEBUGINFO": parse_bool,
    "RUST_SCCACHE": parse_bool,
    "RUST_SCCACHE_CACHE_SIZE": parse_sccache_size,
    "RUST_SCCACHE_WORKSPACE": parse_bool,
}


def parse_off_or_duration(text: str) -> int:
    """Seconds, or 0 for 0/off/no (the rule is off)."""
    if text.strip().lower() in ("0", "off", "no", "none"):
        return 0
    return parse_duration(text)


def expected_template(home: str) -> str:
    return f"{home}/{ROOT_REL}/{HASH_VAR}"


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
        # Cargo's incremental folders inside: [[path, size]], <profile>/incremental
        # and <triple>/<profile>/incremental.
        self.inc: list[Any] = []

    @property
    def inc_size(self) -> int:
        return sum(int(n) for _, n in self.inc)

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
TOOL_FILE_DIRS = {
    ".github/workflows": (".yml", ".yaml"),
    ".cargo": ("config", "config.toml"),
    ".circleci": (".yml", ".yaml"),
    ".buildkite": (".yml", ".yaml"),
}
TOOL_FILE_TOP = (".gitlab-ci.yml",)
TOOL_FILE_MAX = 1 << 20  # bytes read of one file
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


def decide(
    folders: Sequence[Folder],
    pol: Mapping[str, Any],
    now: float,
    in_use: Mapping[str, Sequence[tuple[int | str, str]]],
    claimed: Mapping[str, Sequence[str]] | None = None,
) -> list[tuple[Folder, str, str]]:
    """[(folder, action, reason)] sorted least recently active first.
    action: 'delete', 'trim' (delete only its incremental folders) or
    'keep'. CLAIMED: {folder key: ...} of a complete workspace search, or
    None (no orphan rule). Pure: no file access."""
    order = sorted(folders, key=lambda f: (f.activity, f.key))
    act: dict[str, tuple[str, str]] = {}
    total = sum(f.size for f in folders)
    inc_idle = pol.get("incremental_idle", 0)
    for f in order:
        idle = now - f.activity
        if f.problem:
            act[f.key] = ("keep", f"left alone: {f.problem}")
        elif f.key in in_use:
            act[f.key] = ("keep", "in use: " + users_text(in_use[f.key]))
        elif idle < pol["min_keep"]:
            act[f.key] = ("keep", f"active {ago(idle)} ago (within MIN_KEEP)")
        elif idle > pol["max_idle"]:
            act[f.key] = ("delete", f"idle {ago(idle)} > MAX_IDLE")
            total -= f.size
        elif claimed is not None and pol.get("orphans", True) and f.key not in claimed:
            act[f.key] = ("delete", f"orphan: no workspace claims it (idle {ago(idle)})")
            total -= f.size
        elif inc_idle and idle > inc_idle and f.inc_size:
            act[f.key] = ("trim", f"incremental of an idle folder (idle {ago(idle)}; frees {gib(f.inc_size)})")
            total -= f.inc_size
    if pol["max_total"] and total > pol["max_total"]:
        for f in order:
            if total <= pol["max_total"]:
                break
            a = act.get(f.key, ("", ""))[0]
            if a in ("keep", "delete"):
                continue
            act[f.key] = ("delete", f"over MAX_TOTAL ({gib(total)} > {gib(pol['max_total'])}), least recently active")
            total -= f.size - (f.inc_size if a == "trim" else 0)
    out: list[tuple[Folder, str, str]] = []
    for f in order:
        a, why = act.get(f.key, ("keep", f"active {ago(now - f.activity)} ago"))
        out.append((f, a, why))
    return out


def users_text(users: Sequence[tuple[int | str, str]]) -> str:
    shown = ", ".join(f"{comm} (pid {pid})" for pid, comm in users[:3])
    return shown + (f" and {len(users) - 3} more" if len(users) > 3 else "")


def after_total(plan: Sequence[tuple[Folder, str, str]]) -> int:
    return sum(f.size - (f.inc_size if a == "trim" else 0) for f, a, _ in plan if a != "delete")


UV_CACHE_REL = ".cache/uv"
UV_TIMEOUT = 10 * 60  # seconds for one `uv cache prune` (also bounded by the run's budget)
UV_LOCK_WAIT = 60  # seconds uv may wait for its cache lock (UV_LOCK_TIMEOUT)
# What uv says when UV_LOCK_TIMEOUT ran out ("Timeout (60s) when waiting for
# lock on `...` at `.../.lock`, is another uv process running?"). Any other
# error, also one that names the lock file (permission denied, ...), is a
# failure.
UV_LOCK_TIMED_OUT = re.compile(r"Timeout \(\d+(?:\.\d+)?s\) when waiting for lock on ")
UV_REMOVED = re.compile(r"Removed (\d+) files? \(([\d.]+)\s*([KMGT]?i?B)\)")
UV_UNITS = {
    "b": 1,
    "kib": 1 << 10,
    "mib": 1 << 20,
    "gib": 1 << 30,
    "tib": 1 << 40,
    "kb": 1000,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
}
HOST_SUFFIX = re.compile(r"-(?:x86_64|aarch64|i686|armv7|riscv64gc|powerpc64le|s390x|loongarch64)-[\w.-]+\Z")


def short_toolchain(name: str) -> str:
    return HOST_SUFFIX.sub("", name)


def toolchain_pattern(name: str) -> re.Pattern[bytes]:
    """A toolchain name as a word (bytes): `+nightly`, `?= nightly-2026-06-01`,
    `toolchain: 1.98.0`; not `nightly` inside `nightly-2026-06-01`, not
    `1.98.0` inside `1.98.0.1`."""
    return re.compile(rb"(?<![\w.+-])\+?" + re.escape(name.encode()) + rb"(?![\w.]|-\d)")


# Planned, not implemented yet: (where, how).
PLANNED = {
    "rust:registry": (
        "~/.cargo/registry, ~/.cargo/git",
        "cargo already deletes downloads unused for months by itself; maybe cargo clean gc limits",
    ),
    "go:build": ("~/.cache/go-build", "Go already trims entries unused for 5 days by itself"),
    "go:mod": ("~/go/pkg/mod", "by age"),
    "node:npm": ("~/.npm/_cacache", "npm cache verify / by age"),
    "jvm:maven": ("~/.m2/repository", "old -SNAPSHOT builds and superseded versions only, never by age"),
}
# The time one `prune apply` may take in all. The service is stopped by
# systemd after 30 minutes (TimeoutStartSec): the run stops its own work
# before that. Targets with a fixed upper bound (MAX_SECONDS) run first, so a
# long rust:build run never leaves python:uv without time; rust:build starts
# no deletion with less than RM_MARGIN left and stops an rm at the end of the
# budget (what is left is a leftover that the next run deletes).
RUN_BUDGET = 25 * 60
RM_MARGIN = 2 * 60


UNIT_HEADER = f"{MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand."


def render_service() -> str:
    return render_unit_file(
        [
            UNIT_HEADER,
            "# Prunes the toolchain caches enabled in ~/.config/cst/toolchains.conf",
            "# (cargo's build folders in ~/.cache/cargo-build, ...). Why and how:",
            "# toolchains/README.md in CST.",
        ],
        [
            (
                "Unit",
                [
                    "Description=CST: prune old toolchain caches (cst toolchains)",
                    f"ConditionPathExists=%h/{PROG_REL}",
                    f"ConditionPathExists=%h/{DEFAULTS_REL}",
                ],
            ),
            (
                "Service",
                [
                    "Type=oneshot",
                    f"ExecStart=/usr/bin/python3 -I %h/{PROG_REL} prune apply",
                    "SyslogIdentifier=cst-toolchains",
                    "Nice=19",
                    "IOSchedulingClass=idle",
                    "TimeoutStartSec=30min",
                    "NoNewPrivileges=yes",
                ],
            ),
        ],
    )


def render_timer() -> str:
    return unit_timer([UNIT_HEADER], "CST: prune old toolchain caches, hourly", "hourly", "10min")


def label(key: str) -> str:
    return f"~/{key}" if "/" in key else f"~/{UNIT_DIR_REL}/{key}"


# Exactly what `cst btrfs snapshots setup` always wrote (same lines, same
# comments, same rules): a setting already there is never changed.
CARGO_CFG_REL = ".cargo/config.toml"
UV_CFG_REL = ".config/uv/uv.toml"
WS = r"[^\S\n]"  # a blank that is not a line break (grep's [[:space:]] within a line)
CARGO_HAS_BUILD_DIR = re.compile(rf"^{WS}*build-dir{WS}*=|^{WS}*build\.build-dir", re.M)
CARGO_HAS_BUILD = re.compile(rf"^{WS}*\[{WS}*build{WS}*\]|^{WS}*build\.", re.M)
UV_HAS_LINK_MODE = re.compile(rf"^{WS}*link-mode{WS}*=", re.M)
UV_HAS_TABLE = re.compile(rf"^{WS}*\[", re.M)


def cargo_text(home: str) -> str:
    return (
        "\n# Added by btrfs-setup.sh (CST): intermediate build artifacts live in\n"
        "# ~/.cache (a subvolume excluded from /home snapshots); final ones stay in target/.\n"
        f'[build]\nbuild-dir = "{home}/{ROOT_REL}/{HASH_VAR}"\n'
    )


UV_TEXT = (
    "# Added by btrfs-setup.sh (CST): ~/.cache is its own subvolume and\n"
    "# hardlinks cannot cross subvolumes; reflink clones can.\n"
    'link-mode = "clone"\n'
)
# The opt-in item: lean debug info for dev builds. Off unless
# RUST_LEAN_DEBUGINFO=yes (or config apply --lean-debuginfo); `cst btrfs
# snapshots setup` passes --no-lean-debuginfo, so it never turns it on.
# Changing debug info changes every dev build's fingerprint: the first build of
# each workspace afterwards compiles everything again.
LEAN_HEAD = (
    "\n# Added by toolchains-setup.sh (CST) for RUST_LEAN_DEBUGINFO=yes: smaller dev builds.\n"
    "# Your own crates keep line tables (backtraces show file:line); dependencies get no\n"
    "# debug info. These lines take precedence over [profile.dev] in each project's Cargo.toml.\n"
)
LEAN_KEYS = (
    ("profile.dev", ("profile", "dev"), "line-tables-only", '[profile.dev]\ndebug = "line-tables-only"\n'),
    (
        'profile.dev.package."*"',
        ("profile", "dev", "package", "*"),
        False,
        '[profile.dev.package."*"]\ndebug = false\n',
    ),
)
LEAN_REBUILD = (
    "turning it on makes the next dev build of every workspace compile everything once "
    "(all at once if many worktrees build together): apply it while builds are quiet"
)


class LeanState(TypedDict):
    path: str
    add: list[tuple[str, str]]
    have: list[str]
    conflicts: list[str]
    append: str
    error: str


def dig(data: object, path: Sequence[str]) -> object:
    for k in path:
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


def same_debug(value: object, want: object) -> bool:
    if want is False:
        return value is False or value == "none" or (type(value) is int and value == 0)
    return value == want


# The opt-in sccache item. Off unless RUST_SCCACHE=yes (or config apply
# --sccache): a small wrapper (toolchains/rustc-wrapper, copied to
# ~/.local/libexec/cst/) named as rustc-wrapper in [build] of
# ~/.cargo/config.toml hands rustc to sccache. Unlike lean debug info,
# --no-sccache removes CST's line again: a wrapper left behind is riskier
# than a debug setting. RUST_SCCACHE=no alone never removes it.
WRAPPER_NAME = "rustc-wrapper"
WRAPPER_REL = ".local/libexec/cst/rustc-wrapper"
SCCACHE_DIR_REL = ".cache/sccache"
SCCACHE_DEFAULT_SIZE = "25G"
SCCACHE_HEAD = (
    "# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes: rustc goes through sccache (~/.cache/sccache)."
)
SCCACHE_SIZE_LINE = re.compile(r"(?m)^cache_size=\S*(?= # cst: RUST_SCCACHE_CACHE_SIZE)")
SCCACHE_WORKSPACE_LINE = re.compile(r"(?m)^workspace=\S*(?= # cst: RUST_SCCACHE_WORKSPACE)")
SCCACHE_REBUILD = (
    "turning it on rebuilds nothing: the cache fills as crates are compiled anew "
    "(a new worktree, cargo update, a new toolchain)"
)
BUILD_HEADER = re.compile(rf"^{WS}*\[{WS}*build{WS}*\]{WS}*(?:#.*)?$", re.M)
KEY_LINE = re.compile(rf"^{WS}*[A-Za-z0-9_.\"'-]+{WS}*=", re.M)
ANY_HEADER = re.compile(rf"^{WS}*\[", re.M)
SCCACHE_STATS = re.compile(
    r"^(Compile requests|Cache hits|Cache misses|Cache size|Max cache size)\s{2,}(\S.*?)\s*$", re.M
)


@dataclasses.dataclass(frozen=True, slots=True)
class WrapperValues:
    """The settings `config apply` writes into the installed wrapper."""

    size: str = SCCACHE_DEFAULT_SIZE
    workspace: bool = True  # local crates through sccache too

    def __str__(self) -> str:
        return f"cache size {self.size}" + ("" if self.workspace else "; local crates straight to rustc")


def plain(data: Mapping[str, object]) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(json.dumps(data)))


def parses_as(text: str, expect: Mapping[str, object]) -> bool:
    try:
        return plain(tomllib.loads(text)) == expect
    except tomllib.TOMLDecodeError:
        return False


class SccacheState(TypedDict):
    path: str
    error: str
    value: object
    ours: bool
    add: str
    remove: str


USAGE = """\
usage: cst-toolchains status [--brief]
       cst-toolchains prune plan|apply [TARGET...]
       cst-toolchains auto on [--unless-off] | auto off
       cst-toolchains config plan|apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache]
       (each also takes --home DIR; help: cst-toolchains help)"""


class ParsedArgs(TypedDict):
    cmd: str
    action: str | None
    targets: list[str]
    brief: bool
    unless_off: bool
    home: str | None
    lean: bool | None
    sccache: bool | None


def parse_args(argv: Sequence[str]) -> ParsedArgs:
    """{cmd, action, targets, brief, unless_off, home}; raises Usage."""
    args: list[str] = []
    home: str | None = None
    it = iter(argv)
    for a in it:
        if a == "--home":
            home = next(it, None)
            if not home:
                raise Usage("--home needs a folder")
        elif a.startswith("--home="):
            home = a.split("=", 1)[1]
            if not home:  # an empty value must never mean "your real home"
                raise Usage("--home needs a folder")
        else:
            args.append(a)
    out: ParsedArgs = {
        "cmd": "help",
        "action": None,
        "targets": [],
        "brief": False,
        "unless_off": False,
        "home": home,
        "lean": None,
        "sccache": None,
    }
    if not args or args[0] in ("help", "-h", "--help"):
        return out
    cmd, rest = args[0], args[1:]
    out["cmd"] = cmd
    if cmd == "status":
        for a in rest:
            if a != "--brief":
                raise Usage(f"status: unknown argument '{a}' (only --brief)")
            out["brief"] = True
    elif cmd == "prune":
        if not rest or rest[0] not in ("plan", "apply"):
            raise Usage("prune: plan or apply, then the targets (none: every enabled one)")
        out["action"] = rest[0]
        for a in rest[1:]:
            if a.startswith("-"):
                raise Usage(f"prune: unknown option '{a}'")
            out["targets"].append(a)
    elif cmd == "auto":
        if not rest or rest[0] not in ("on", "off"):
            raise Usage("auto: on or off")
        out["action"] = rest[0]
        for a in rest[1:]:
            if a == "--unless-off" and rest[0] == "on":
                out["unless_off"] = True
            else:
                raise Usage(f"auto {rest[0]}: unknown argument '{a}'")
    elif cmd == "config":
        if not rest or rest[0] not in ("plan", "apply"):
            raise Usage("config: plan or apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache]")
        out["action"] = rest[0]
        for a in rest[1:]:
            if a in ("--lean-debuginfo", "--no-lean-debuginfo") and out["lean"] is None:
                out["lean"] = a == "--lean-debuginfo"
            elif a in ("--sccache", "--no-sccache") and out["sccache"] is None:
                out["sccache"] = a == "--sccache"
            else:
                raise Usage(f"config {rest[0]}: unknown argument '{a}'")
    else:
        raise Usage(f"unknown command '{cmd}'")
    return out
