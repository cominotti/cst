# SPDX-License-Identifier: MPL-2.0
"""The settings `config` writes, no I/O: cargo's build-dir, uv's link-mode, the
opt-in lean debug info and the opt-in sccache wrapper (its rustc-wrapper key,
the values written into it, the server statistics).
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Sequence

from cst_core.domain.install import manifest_by_src
from toolchains.domain.build import HASH_VAR, ROOT_REL


# The words of the plan and status lines that name what CST could not do.
CANNOT_READ = "cannot be read"
BY_HAND = "BY HAND"
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
WRAPPER_REL = manifest_by_src()["toolchains/rustc-wrapper"].dest.removeprefix("~/")
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
    # The value: from its first to its last non-blank (no lazy .*? before \s*: that backtracks).
    r"^(Compile requests|Cache hits|Cache misses|Cache size|Max cache size)\s{2,}(\S(?:.*\S)?)[^\S\n]*$",
    re.M,
)


def server_place(env_id: str, cgroup_text: str | None, sock: str) -> str:
    """Where the running sccache server of ENV_ID is, as `status` shows it.

    CGROUP_TEXT is its /proc/PID/cgroup (None: not known). The wrapper starts
    the server in cst-sccache-<env>.scope; one anywhere else (started by an
    older wrapper, or by the client in a compile's own scope) runs every
    compile of that environment under that cgroup's limits, so it is a WARN.
    """
    scope = f"cst-sccache-{env_id}.scope"
    path = next((ln[3:] for ln in (cgroup_text or "").splitlines() if ln.startswith("0::")), "")
    if not path:
        return f"sccache server {env_id}: cgroup unknown (its process could not be read)"
    shown = re.sub(r"^.*?/user@\d+\.service/", "", path)
    if path.rsplit("/", 1)[-1] == scope:
        return f"sccache server {env_id}: in its own scope ({shown})"
    return (
        f"sccache server {env_id}: WARN: runs in {shown}, not in {scope}: it and every compile through it "
        "share that cgroup's limits until it stops (the wrapper of `cst toolchains config apply` starts "
        f"the next one in its scope; to stop this one: SCCACHE_SERVER_UDS={sock} sccache --stop-server)"
    )


@dataclasses.dataclass(frozen=True, slots=True)
class WrapperValues:
    """The settings `config apply` writes into the installed wrapper."""

    size: str = SCCACHE_DEFAULT_SIZE
    workspace: bool = True  # local crates through sccache too

    def __str__(self) -> str:
        return f"cache size {self.size}" + ("" if self.workspace else "; local crates straight to rustc")

    def record(self) -> dict[str, str]:
        """The settings as the install record lists them (the wrapper's values)."""
        return {"cache_size": self.size, "workspace": "yes" if self.workspace else "no"}


class SccacheState(TypedDict):
    path: str
    error: str
    value: object
    ours: bool
    add: str
    remove: str
