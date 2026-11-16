"""Pure domain model, configuration parsers, and pruning decision rules for `toolchains`."""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
import json
import os
import re
import time
import tomllib
from typing import Any, Final

MARKER: Final = "# managed-by: toolchains-setup.sh (CST)"
NAME: Final = "cst-toolchains"
CONF_REL: Final = ".config/cst/toolchains.conf"
OFF_REL: Final = ".config/cst/toolchains-auto.off"
LIB_REL: Final = ".local/lib/cst"
PROG_REL: Final = f"{LIB_REL}/{NAME}"
DEFAULTS_NAME: Final = "toolchains.conf"
DEFAULTS_REL: Final = f"{LIB_REL}/{DEFAULTS_NAME}"
UNIT_DIR_REL: Final = ".config/systemd/user"
STATE_DIR_REL: Final = ".local/state/cst"
SERVICE: Final = "cst-toolchains-prune.service"
TIMER: Final = "cst-toolchains-prune.timer"
WANTS_REL: Final = f"{UNIT_DIR_REL}/timers.target.wants/{TIMER}"
OLD_MARKER: Final = "# managed-by: btrfs-setup.sh (CST)"
OLD_TIMER: Final = "cst-cargo-build-gc.timer"
OLD_UNITS: Final[tuple[str, ...]] = ("cst-cargo-build-gc.timer", "cst-cargo-build-gc.service")
OLD_FILES: Final[tuple[str, ...]] = (".local/lib/cst/cargo-build-gc", ".config/cst/cargo-gc.disabled")
OLD_DATA: Final[tuple[str, ...]] = (".local/state/cst/cargo-build-gc.json", ".local/state/cst/cargo-build-gc.lock")
OLD_CONF_REL: Final = ".config/cst/cargo-gc.conf"

HASH_VAR: Final = "{workspace-path-hash}"
ROOT_REL: Final = ".cache/cargo-build"
MIN_KEEP_FLOOR: Final = 15 * 60
FUTURE_SLACK: Final = 300
HASH_TOP: Final = re.compile(r"[0-9a-f]{2}\Z")
HASH_LEAF: Final = re.compile(r"[0-9a-f]{14}\Z")
LOCK_NAMES: Final[tuple[str, ...]] = (".cargo-build-lock", ".cargo-lock")
TRASH_PREFIX: Final = ".cst-toolchains-trash-"
TRASH_NAME: Final = re.compile(re.escape(TRASH_PREFIX) + r"[0-9a-f]{16}(?:-inc\d+)?-\d+\Z")
INCREMENTAL: Final = "incremental"
GIB: Final = 1 << 30
RUST_TOOLS: Final = re.compile(r"(cargo|rustc|rustdoc|clippy-driver|rust-analyzer|build-script|build_script).*")

DURATION_UNITS: Final[dict[str, int]] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
SIZE_UNITS: Final[dict[str, int]] = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}

CARGO_COMM: Final = re.compile(r"cargo(-.+)?")
CARGO_ENV: Final[tuple[str, ...]] = ("CARGO_HOME", "CARGO_BUILD_BUILD_DIR", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN")

WORKSPACE_REL: Final = "Workspace"
SCAN_SKIP: Final[frozenset[str]] = frozenset({"node_modules", "target", "__pycache__", "venv"})
REPO_DEPTH: Final = 6
CRATE_DEPTH: Final = 6
ENUM_BUDGET: Final = 600
CMD_TIMEOUT: Final = 60
PIN_NAMES: Final[tuple[str, ...]] = ("rust-toolchain.toml", "rust-toolchain")
TOOL_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {"Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", ".justfile", "Taskfile.yml", "Taskfile.yaml"}
)
TOOL_FILE_SUFFIXES: Final[tuple[str, ...]] = (".mk", ".just", ".sh", ".bash")
TOOL_FILE_DIRS: Final[dict[str, tuple[str, ...]]] = {
    ".github/workflows": (".yml", ".yaml"),
    ".cargo": ("config", "config.toml"),
    ".circleci": (".yml", ".yaml"),
    ".buildkite": (".yml", ".yaml"),
}
TOOL_FILE_TOP: Final[tuple[str, ...]] = (".gitlab-ci.yml",)
TOOL_FILE_MAX: Final = 1 << 20
TOOL_FILES_MAX: Final = 100000
CACHE_TAG: Final = "CACHEDIR.TAG"
NOT_A_MEMBER: Final = "believes it's in a workspace when it's not"

UV_CACHE_REL: Final = ".cache/uv"
UV_TIMEOUT: Final = 10 * 60
UV_LOCK_WAIT: Final = 60
UV_LOCK_TIMED_OUT: Final = re.compile(r"Timeout \(\d+(?:\.\d+)?s\) when waiting for lock on ")
UV_REMOVED: Final = re.compile(r"Removed (\d+) files? \(([\d.]+)\s*([KMGT]?i?B)\)")
UV_UNITS: Final[dict[str, int]] = {
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

HOST_SUFFIX: Final = re.compile(r"-(?:x86_64|aarch64|i686|armv7|riscv64gc|powerpc64le|s390x|loongarch64)-[\w.-]+\Z")

PLANNED: Final[dict[str, tuple[str, str]]] = {
    "rust:registry": (
        "~/.cargo/registry, ~/.cargo/git",
        "cargo already deletes downloads unused for months by itself; maybe cargo clean gc limits",
    ),
    "go:build": ("~/.cache/go-build", "Go already trims entries unused for 5 days by itself"),
    "go:mod": ("~/go/pkg/mod", "by age"),
    "node:npm": ("~/.npm/_cacache", "npm cache verify / by age"),
    "jvm:maven": ("~/.m2/repository", "old -SNAPSHOT builds and superseded versions only, never by age"),
}

RUN_BUDGET: Final = 25 * 60
RM_MARGIN: Final = 2 * 60

CARGO_CFG_REL: Final = ".cargo/config.toml"
UV_CFG_REL: Final = ".config/uv/uv.toml"
_WS: Final = r"[^\S\n]"
CARGO_HAS_BUILD_DIR: Final = re.compile(rf"^{_WS}*build-dir{_WS}*=|^{_WS}*build\.build-dir", re.M)
CARGO_HAS_BUILD: Final = re.compile(rf"^{_WS}*\[{_WS}*build{_WS}*\]|^{_WS}*build\.", re.M)
UV_HAS_LINK_MODE: Final = re.compile(rf"^{_WS}*link-mode{_WS}*=", re.M)
UV_HAS_TABLE: Final = re.compile(rf"^{_WS}*\[", re.M)

UV_TEXT: Final = (
    "# Added by btrfs-setup.sh (CST): ~/.cache is its own subvolume and\n"
    "# hardlinks cannot cross subvolumes; reflink clones can.\n"
    'link-mode = "clone"\n'
)

LEAN_HEAD: Final = (
    "\n# Added by toolchains-setup.sh (CST) for RUST_LEAN_DEBUGINFO=yes: smaller dev builds.\n"
    "# Your own crates keep line tables (backtraces show file:line); dependencies get no\n"
    "# debug info. These lines take precedence over [profile.dev] in each project's Cargo.toml.\n"
)
LEAN_KEYS: Final[tuple[tuple[str, tuple[str, ...], str | bool, str], ...]] = (
    ("profile.dev", ("profile", "dev"), "line-tables-only", '[profile.dev]\ndebug = "line-tables-only"\n'),
    (
        'profile.dev.package."*"',
        ("profile", "dev", "package", "*"),
        False,
        '[profile.dev.package."*"]\ndebug = false\n',
    ),
)
LEAN_REBUILD: Final = (
    "turning it on makes the next dev build of every workspace compile everything once "
    "(all at once if many worktrees build together): apply it while builds are quiet"
)

WRAPPER_NAME: Final = "rustc-wrapper"
WRAPPER_REL: Final = ".local/libexec/cst/rustc-wrapper"
SCCACHE_DIR_REL: Final = ".cache/sccache"
SCCACHE_DEFAULT_SIZE: Final = "25G"
SCCACHE_HEAD: Final = (
    "# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes: rustc goes through sccache (~/.cache/sccache)."
)
SCCACHE_SIZE_LINE: Final = re.compile(r"(?m)^cache_size=\S*(?= # cst: RUST_SCCACHE_CACHE_SIZE)")
SCCACHE_WORKSPACE_LINE: Final = re.compile(r"(?m)^workspace=\S*(?= # cst: RUST_SCCACHE_WORKSPACE)")
SCCACHE_REBUILD: Final = (
    "turning it on rebuilds nothing: the cache fills as crates are compiled anew "
    "(a new worktree, cargo update, a new toolchain)"
)
BUILD_HEADER: Final = re.compile(rf"^{_WS}*\[{_WS}*build{_WS}*\]{_WS}*(?:#.*)?$", re.M)
KEY_LINE: Final = re.compile(rf"^{_WS}*[A-Za-z0-9_.\"'-]+{_WS}*=", re.M)
ANY_HEADER: Final = re.compile(rf"^{_WS}*\[", re.M)
SCCACHE_STATS: Final = re.compile(
    r"^(Compile requests|Cache hits|Cache misses|Cache size|Max cache size)\s{2,}(\S.*?)\s*$", re.M
)

USAGE: Final = """\
usage: cst-toolchains status [--brief]
       cst-toolchains prune plan|apply [TARGET...]
       cst-toolchains auto on [--unless-off] | auto off
       cst-toolchains config plan|apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache]
       (each also takes --home DIR; help: cst-toolchains help)"""


class Refused(Exception):
    """Raised when a safety precondition refuses to proceed."""


class ConfigError(Exception):
    """Raised when settings syntax or policy bounds are invalid."""


class Usage(Exception):
    """Raised on invalid CLI arguments."""


class Incomplete(Exception):
    """Raised when workspace/toolchain enumeration cannot complete safely."""


@dataclass(frozen=True, slots=True)
class WrapperValues:
    """The settings `config apply` writes into the installed sccache wrapper."""

    size: str = SCCACHE_DEFAULT_SIZE
    workspace: bool = True

    def __str__(self) -> str:
        """Render a concise summary of wrapper size and workspace behavior."""
        return f"cache size {self.size}" + ("" if self.workspace else "; local crates straight to rustc")


@dataclass(frozen=True, slots=True)
class RustBuildPolicy:
    """Validated pruning policy for the `rust:build` target."""

    max_idle: int
    max_total: int
    min_keep: int
    orphans: bool = True
    incremental_idle: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to the legacy dict representation."""
        return {
            "max_idle": self.max_idle,
            "max_total": self.max_total,
            "min_keep": self.min_keep,
            "orphans": self.orphans,
            "incremental_idle": self.incremental_idle,
        }

    @classmethod
    def from_raw(cls, vals: Mapping[str, str], prefix: str = "RUST_BUILD") -> RustBuildPolicy:
        """Parse and validate `RustBuildPolicy` from raw key/value settings."""
        max_idle = parse_duration(vals["MAX_IDLE"])
        max_total = parse_size(vals["MAX_TOTAL"])
        min_keep = parse_duration(vals["MIN_KEEP"])
        orphans = parse_bool(vals["ORPHANS"])
        incremental_idle = parse_off_or_duration(vals["INCREMENTAL_IDLE"])
        if min_keep < MIN_KEEP_FLOOR:
            raise ConfigError(f"{prefix}_MIN_KEEP={vals['MIN_KEEP']} is below the floor of 15m")
        if max_idle < min_keep:
            raise ConfigError(
                f"{prefix}_MAX_IDLE={vals['MAX_IDLE']} is shorter than {prefix}_MIN_KEEP={vals['MIN_KEEP']}"
            )
        if incremental_idle and incremental_idle < min_keep:
            raise ConfigError(
                f"{prefix}_INCREMENTAL_IDLE={vals['INCREMENTAL_IDLE']} is shorter than "
                f"{prefix}_MIN_KEEP={vals['MIN_KEEP']} (0 or off turns it off)"
            )
        return cls(
            max_idle=max_idle,
            max_total=max_total,
            min_keep=min_keep,
            orphans=orphans,
            incremental_idle=incremental_idle,
        )

    def policy_text(self) -> str:
        """Format the policy summary line."""
        cap = gib(self.max_total) if self.max_total else "off"
        inc = span(self.incremental_idle) if self.incremental_idle else "off"
        orph = "on" if self.orphans else "off"
        return (
            f"MAX_IDLE {span(self.max_idle)}, MAX_TOTAL {cap}, MIN_KEEP {span(self.min_keep)}, "
            f"ORPHANS {orph}, INCREMENTAL_IDLE {inc}"
        )


@dataclass(frozen=True, slots=True)
class PythonUvPolicy:
    """Validated pruning policy for the `python:uv` target."""

    interval: int

    def to_dict(self) -> dict[str, int]:
        """Convert to the legacy dict representation."""
        return {"interval": self.interval}

    @classmethod
    def from_raw(cls, vals: Mapping[str, str], prefix: str = "PYTHON_UV") -> PythonUvPolicy:
        """Parse `PythonUvPolicy` from raw key/value settings."""
        _ = prefix
        return cls(interval=parse_off_or_duration(vals["INTERVAL"]))

    def policy_text(self) -> str:
        """Format the policy summary line."""
        every = span(self.interval) if self.interval else "every run"
        return f"uv cache prune, at most once per INTERVAL ({every})"


@dataclass(frozen=True, slots=True)
class FolderSnapshot:
    """Immutable snapshot of a measured cargo build folder."""

    key: str
    path: str
    size: int = 0
    activity: float = 0.0
    entries: int = 0
    problem: str = ""
    future: int = 0
    foreign: int = 0
    inc: tuple[tuple[str, int], ...] = ()

    @property
    def inc_size(self) -> int:
        """Total bytes across incremental subdirectories."""
        return sum(n for _, n in self.inc)


@dataclass(frozen=True, slots=True)
class FolderDecision:
    """Domain pruning decision for a single cargo build folder."""

    folder: FolderSnapshot
    action: str
    reason: str


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Parsed and validated `cst-toolchains` CLI command."""

    cmd: str
    action: str | None
    targets: tuple[str, ...]
    brief: bool
    unless_off: bool
    home: str | None
    lean: bool | None
    sccache: bool | None


def gib(n: int | float) -> str:
    """Format byte counts in GiB (>= 1 GiB) or MiB."""
    return f"{n / GIB:.1f} GiB" if n >= GIB else f"{n / (1 << 20):.0f} MiB"


def ago(seconds: int | float) -> str:
    """Format elapsed seconds compactly as minutes, hours, or days."""
    secs = max(0, int(seconds))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 4 * 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def span(seconds: int) -> str:
    """Format a setting duration concisely (48h, 2h, 90m, 14d)."""
    if seconds % 3600 == 0 and seconds < 7 * 86400:
        return f"{seconds // 3600}h"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def when(ts: float) -> str:
    """Format a local UNIX timestamp as YYYY-MM-DD HH:MM."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def tilde(path: str, home: str) -> str:
    """Collapse leading `home` directory into `~` for display."""
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def label(key: str) -> str:
    """Display label for an installed file or user systemd unit."""
    return f"~/{key}" if "/" in key else f"~/{UNIT_DIR_REL}/{key}"


def users_text(users: Sequence[tuple[int | str, str]]) -> str:
    """Format up to 3 active process descriptors."""
    shown = ", ".join(f"{comm} (pid {pid})" for pid, comm in users[:3])
    return shown + (f" and {len(users) - 3} more" if len(users) > 3 else "")


def unknown_text(unknown: Sequence[tuple[int | str, str, str | None, str]]) -> str:
    """Format a description of the first cargo process with an unknown build folder."""
    pid, comm, cwd, why = unknown[0]
    more = f" (and {len(unknown) - 1} more)" if len(unknown) > 1 else ""
    return f"{comm} (pid {pid}) in {cwd or '?'}: its build folder is unknown ({why}){more}"


def merge_users(*maps: Mapping[str, Sequence[tuple[int | str, str]]]) -> dict[str, list[tuple[int | str, str]]]:
    """Merge per-folder process lists without duplicates while preserving order."""
    out: dict[str, list[tuple[int | str, str]]] = {}
    for m in maps:
        for k, v in m.items():
            lst = out.setdefault(k, [])
            lst += [u for u in v if u not in lst]
    return out


def parse_duration(text: str) -> int:
    """Parse a duration with suffix s, m, h, d, or w into seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*", text, re.I)
    if not m:
        raise ConfigError(f"'{text}' is not a duration (a number with s, m, h, d or w, e.g. 48h)")
    return int(float(m.group(1)) * DURATION_UNITS[m.group(2).lower()])


def parse_size(text: str) -> int:
    """Parse a byte size with required unit (K, M, G, T) or 0/off."""
    t = text.strip().lower()
    if re.fullmatch(r"0+(?:\.0*)?|off|none|no", t):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt])(?:i?b)?", t)
    if not m:
        raise ConfigError(f"'{text}' is not a size (a number with K, M, G or T, e.g. 150G; 0 or off for no cap)")
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)])


def parse_bool(text: str) -> bool:
    """Parse yes/true/on/1 vs no/false/off/0."""
    t = text.strip().lower()
    if t in ("yes", "true", "on", "1"):
        return True
    if t in ("no", "false", "off", "0"):
        return False
    raise ConfigError(f"'{text}' is not yes or no")


def parse_conf(text: str, known: AbstractSet[str]) -> dict[str, str]:
    """Parse KEY=value lines, rejecting unknown keys or malformed lines."""
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            raise ConfigError(f"line {n}: expected KEY=value, got '{s}'")
        key, val = (p.strip() for p in s.split("=", 1))
        val = re.sub(r"\s+#.*$", "", val).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key not in known:
            raise ConfigError(f"line {n}: unknown setting '{key}' (known: {', '.join(sorted(known))})")
        out[key] = val
    return out


def parse_sccache_size(text: str) -> str:
    """Format a positive size in sccache's spelling (whole number with K, M, G, or T)."""
    n = parse_size(text)
    if n <= 0:
        raise ConfigError(f"'{text}' is no cache size (sccache needs one, e.g. 25G)")
    for unit in "TGM":
        if n % SIZE_UNITS[unit.lower()] == 0:
            return f"{n // SIZE_UNITS[unit.lower()]}{unit}"
    return f"{-(-n // SIZE_UNITS['k'])}K"


def parse_off_or_duration(text: str) -> int:
    """Parse a duration in seconds or 0 for 0/off/no/none."""
    if text.strip().lower() in ("0", "off", "no", "none"):
        return 0
    return parse_duration(text)


def make_rust_build_policy(vals: Mapping[str, str], prefix: str) -> dict[str, Any]:
    """Validate and convert `rust:build` raw settings into a policy dict."""
    return RustBuildPolicy.from_raw(vals, prefix).to_dict()


def rust_build_policy_text(pol: Mapping[str, Any]) -> str:
    """Format a `rust:build` policy dictionary."""
    cap = gib(pol["max_total"]) if pol["max_total"] else "off"
    inc = span(pol["incremental_idle"]) if pol.get("incremental_idle") else "off"
    orph = "on" if pol.get("orphans", True) else "off"
    return (
        f"MAX_IDLE {span(pol['max_idle'])}, MAX_TOTAL {cap}, MIN_KEEP {span(pol['min_keep'])}, "
        f"ORPHANS {orph}, INCREMENTAL_IDLE {inc}"
    )


def make_python_uv_policy(vals: Mapping[str, str], prefix: str) -> dict[str, int]:
    """Validate and convert `python:uv` raw settings into a policy dict."""
    return PythonUvPolicy.from_raw(vals, prefix).to_dict()


def python_uv_policy_text(pol: Mapping[str, Any]) -> str:
    """Format a `python:uv` policy dictionary."""
    every = span(pol["interval"]) if pol["interval"] else "every run"
    return f"uv cache prune, at most once per INTERVAL ({every})"


def expected_template(home: str) -> str:
    """Return Cargo's expected `[build] build-dir` template path."""
    return f"{home}/{ROOT_REL}/{HASH_VAR}"


def prefixes(root: str) -> tuple[str, ...]:
    """Return both `/home/...` and `/var/home/...` forms when applicable."""
    out = {root}
    if root.startswith("/var/home/"):
        out.add(root[len("/var") :])
    elif root.startswith("/home/"):
        out.add("/var" + root)
    return tuple(sorted(out))


def manifest_arg(args: Sequence[str], cwd: str) -> str:
    """Extract normalized `--manifest-path` from a cargo command line, or ''."""
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


def is_tool_file(name: str) -> bool:
    """Return True if a filename is a build/script file scanned for toolchain references."""
    return name in TOOL_FILE_NAMES or name.endswith(TOOL_FILE_SUFFIXES)


def short_toolchain(name: str) -> str:
    """Strip host target triple suffix from a rustup toolchain name."""
    return HOST_SUFFIX.sub("", name)


def toolchain_pattern(name: str) -> re.Pattern[bytes]:
    """Compile a whole-token regex matching a rustup toolchain name in build files."""
    return re.compile(rb"(?<![\w.+-])\+?" + re.escape(name.encode()) + rb"(?![\w.]|-\d)")


def decide(
    folders: Sequence[FolderSnapshot],
    pol: Mapping[str, Any],
    now: float,
    in_use: Mapping[str, Sequence[tuple[int | str, str]]],
    claimed: Mapping[str, Sequence[str]] | None = None,
) -> list[tuple[FolderSnapshot, str, str]]:
    """Compute `(folder, action, reason)` decisions ordered least recently active first."""
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
    out: list[tuple[FolderSnapshot, str, str]] = []
    for f in order:
        a, why = act.get(f.key, ("keep", f"active {ago(now - f.activity)} ago"))
        out.append((f, a, why))
    return out


def after_total(plan: Sequence[tuple[FolderSnapshot, str, str]]) -> int:
    """Compute remaining bytes after executing `plan`."""
    return sum(f.size - (f.inc_size if a == "trim" else 0) for f, a, _ in plan if a != "delete")


def render_service() -> str:
    """Render the `cst-toolchains-prune.service` unit file."""
    return f"""\
{MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand.
# Prunes the toolchain caches enabled in ~/.config/cst/toolchains.conf
# (cargo's build folders in ~/.cache/cargo-build, ...). Why and how:
# toolchains/README.md in CST.
[Unit]
Description=CST: prune old toolchain caches (cst toolchains)
ConditionPathExists=%h/{PROG_REL}
ConditionPathExists=%h/{DEFAULTS_REL}

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I %h/{PROG_REL} prune apply
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=30min
NoNewPrivileges=yes
"""


def render_timer() -> str:
    """Render the `cst-toolchains-prune.timer` unit file."""
    return f"""\
{MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand.
[Unit]
Description=CST: prune old toolchain caches, hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
"""


def cargo_text(home: str) -> str:
    """Render CST's `[build] build-dir` snippet for `~/.cargo/config.toml`."""
    return (
        "\n# Added by btrfs-setup.sh (CST): intermediate build artifacts live in\n"
        "# ~/.cache (a subvolume excluded from /home snapshots); final ones stay in target/.\n"
        f'[build]\nbuild-dir = "{home}/{ROOT_REL}/{HASH_VAR}"\n'
    )


def dig(data: Mapping[str, Any], path: Sequence[str]) -> object:
    """Traverse nested dicts along `path`, returning None when missing."""
    cur: object = data
    for k in path:
        if not isinstance(cur, Mapping) or k not in cur:
            return None
        cur = cur[k]
    return cur


def same_debug(value: object, want: object) -> bool:
    """Compare Cargo's debug setting values treating False/'none'/0 identically."""
    if want is False:
        return value is False or value == "none" or (type(value) is int and value == 0)
    return value == want


def plain(data: object) -> dict[str, Any]:
    """Normalize decoded TOML structures via JSON round-trip."""
    loaded: object = json.loads(json.dumps(data))
    if not isinstance(loaded, dict):
        return {}
    return loaded


def parses_as(text: str, expect: Mapping[str, Any]) -> bool:
    """Return True if `text` parses as TOML whose normalized dict equals `expect`."""
    try:
        return plain(tomllib.loads(text)) == expect
    except tomllib.TOMLDecodeError:
        return False


def parse_args(argv: Sequence[str]) -> dict[str, Any]:
    """Parse CLI arguments into a dictionary, raising `Usage` on mistakes."""
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
            if not home:
                raise Usage("--home needs a folder")
        else:
            args.append(a)
    out: dict[str, Any] = {
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


def parse_command(argv: Sequence[str]) -> ParsedCommand:
    """Parse CLI arguments into an immutable `ParsedCommand` value object."""
    parsed = parse_args(argv)
    return ParsedCommand(
        cmd=str(parsed["cmd"]),
        action=parsed["action"],
        targets=tuple(parsed["targets"]),
        brief=bool(parsed["brief"]),
        unless_off=bool(parsed["unless_off"]),
        home=parsed["home"],
        lean=parsed["lean"],
        sccache=parsed["sccache"],
    )
