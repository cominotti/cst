# SPDX-License-Identifier: MPL-2.0
"""The settings `config` writes, no I/O: cargo's build-dir, uv's link-mode, the
opt-in lean debug info and the opt-in sccache wrapper (its rustc-wrapper key,
the values written into it, the server statistics).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import tomllib
from enum import StrEnum
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Sequence

from cst_core.domain.install import manifest_by_src
from toolchains.domain.build import HASH_VAR, ROOT_REL
from toolchains.domain.common import parses_as, plain


# The note of a plan or status line whose file CST cannot read.
CANNOT_READ = "cannot be read"


class ConfigMark(StrEnum):
    """The first column of a plan or status line of config: what apply does or did
    (UNKNOWN and BY_HAND: what CST could not do)."""

    OK = "ok"
    KEEP = "keep"
    WRITE = "write"
    REMOVE = "remove"
    ON = "on"
    OFF = "off"
    UNKNOWN = "?"
    BY_HAND = "BY HAND"


# One plan or status line of config: (mark, the file as shown, the note).
type ConfigLine = tuple[ConfigMark, str, str]


class ConfigView(StrEnum):
    """The command that shows config's lines: config plan or status."""

    PLAN = "plan"
    STATUS = "status"


class ToolSettingState(StrEnum):
    """One tool's setting (cargo's build-dir, uv's link-mode) in its file: CST's line is
    there, the table is there set by hand, apply writes the line, or the file cannot be
    read."""

    SET = "set"
    BY_HAND = "by-hand"
    WRITE = "write"
    UNREADABLE = "unreadable"


# Exactly what `cst btrfs snapshots setup` always wrote (same lines, same
# comments, same rules): a setting already there is never changed.
CARGO_CFG_REL = ".cargo/config.toml"
UV_CFG_REL = ".config/uv/uv.toml"
WS = r"[^\S\n]"  # a blank that is not a line break (grep's [[:space:]] within a line)
CARGO_HAS_BUILD_DIR = re.compile(rf"^{WS}*build-dir{WS}*=|^{WS}*build\.build-dir", re.MULTILINE)
CARGO_HAS_BUILD = re.compile(rf"^{WS}*\[{WS}*build{WS}*\]|^{WS}*build\.", re.MULTILINE)
UV_HAS_LINK_MODE = re.compile(rf"^{WS}*link-mode{WS}*=", re.MULTILINE)
UV_HAS_TABLE = re.compile(rf"^{WS}*\[", re.MULTILINE)


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


def lean_state(path: str, text: str | None) -> LeanState:
    """{path, add: [(label, block)], have: [label], conflicts: [text],
    append: text or '' (when appending is safe), error: ''} of cargo's config PATH
    with TEXT (None: it cannot be read)."""
    st: LeanState = {"path": path, "add": [], "have": [], "conflicts": [], "append": "", "error": ""}
    if text is None:
        st["error"] = CANNOT_READ
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


# The opt-in sccache item. Off unless RUST_SCCACHE=yes (or config apply
# --sccache): a small wrapper (toolchains/rustc-wrapper, copied to
# ~/.local/libexec/cst/) named as rustc-wrapper in [build] of
# ~/.cargo/config.toml hands rustc to sccache. Unlike lean debug info,
# --no-sccache removes CST's line again: a wrapper left behind is riskier
# than a debug setting. RUST_SCCACHE=no alone never removes it.
WRAPPER_NAME = "rustc-wrapper"
WRAPPER_REL = manifest_by_src()["toolchains/rustc-wrapper"].dest.removeprefix("~/")
SCCACHE_DIR_REL = ".cache/sccache"


def wrapper_path(home: str) -> str:
    return os.path.join(home, WRAPPER_REL)


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
BUILD_HEADER = re.compile(rf"^{WS}*\[{WS}*build{WS}*\]{WS}*(?:#.*)?$", re.MULTILINE)
KEY_LINE = re.compile(rf"^{WS}*[A-Za-z0-9_.\"'-]+{WS}*=", re.MULTILINE)
ANY_HEADER = re.compile(rf"^{WS}*\[", re.MULTILINE)
SCCACHE_STATS = re.compile(
    # The value: from its first to its last non-blank (no lazy .*? before \s*: that backtracks).
    r"^(Compile requests|Cache hits|Cache misses|Cache size|Max cache size)\s{2,}(\S(?:.*\S)?)[^\S\n]*$",
    re.MULTILINE,
)


@dataclasses.dataclass(frozen=True, slots=True)
class ScopeFacts:
    """What `status` can see of why the wrapper did not start a server in its
    scope (rustc-wrapper's header lists when it cannot). The defaults: none."""

    run_dir_fallback: bool = False  # the socket is in ~/.cache/sccache/run: no usable $XDG_RUNTIME_DIR
    manager_socket: str = ""  # <the socket's folder>/systemd/private, when that is not a socket
    start_failed: bool = False  # <socket>.failed is not empty: the wrapper's last start failed
    missing_programs: tuple[str, ...] = ()  # of systemd-run and flock, those not on the server's PATH


def server_place(env_id: str, cgroup_text: str | None, sock: str, facts: ScopeFacts | None = None) -> str:
    """Where the running sccache server of ENV_ID is, as `status` shows it.

    CGROUP_TEXT is its /proc/PID/cgroup (None: not known). The wrapper starts
    the server in cst-sccache-<env>.scope; one anywhere else (started by an
    older wrapper, or by the client in a compile's own scope) runs every
    compile of that environment under that cgroup's limits, so it is a WARN.
    Not when FACTS (or the cgroup itself: no user manager above it) show that
    the wrapper cannot use a scope there: stopping that server would change
    nothing, so the line says why instead.
    """
    facts = facts or ScopeFacts()
    scope = f"cst-sccache-{env_id}.scope"
    path = next((ln[3:] for ln in (cgroup_text or "").splitlines() if ln.startswith("0::")), "")
    if not path:
        return f"sccache server {env_id}: cgroup unknown (its process could not be read)"
    shown = re.sub(r"^.*?/user@\d+\.service/", "", path)
    if path.rsplit("/", 1)[-1] == scope:
        return f"sccache server {env_id}: in its own scope ({shown})"
    if not re.search(r"/user@\d+\.service(?:/|$)", path):
        reason = "the compile that started it ran outside the user manager (an ssh session, docker exec)"
    elif facts.run_dir_fallback:
        reason = "no usable $XDG_RUNTIME_DIR, so its socket is in ~/.cache/sccache/run"
    elif facts.manager_socket:
        reason = f"no user manager socket at {facts.manager_socket}"
    elif facts.missing_programs:
        reason = f"no {' and no '.join(facts.missing_programs)} on the PATH it started with"
    else:
        reason = ""
    if reason:
        return f"sccache server {env_id}: runs in {shown} (the wrapper could not use a scope here: {reason})"
    head = (
        f"sccache server {env_id}: WARN: runs in {shown}, not in {scope}: it and every compile through it "
        "share that cgroup's limits until it stops"
    )
    if facts.start_failed:
        return (
            f"{head} (the wrapper's last start of its scope failed, see {shlex.quote(sock + '.failed')}; "
            "it tries again 10 minutes after that)"
        )
    return (
        f"{head} (the wrapper of `cst toolchains config apply` starts the next one in its scope where "
        "systemd-run and flock are installed; to stop this one, wherever sccache is installed: "
        f"SCCACHE_SERVER_UDS={shlex.quote(sock)} sccache --stop-server)"
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


def sccache_state(path: str, text: str | None, want: str) -> SccacheState:
    """{path, error, value (build.rustc-wrapper now, or None), ours, add,
    remove}: add / remove are the whole new file text, '' when CST cannot
    make that change safely (each candidate is parsed again and must equal
    the file with only that one key added or removed). PATH: cargo's config, TEXT its
    text (None: it cannot be read), WANT the wrapper's path."""
    st: SccacheState = {"path": path, "error": "", "value": None, "ours": False, "add": "", "remove": ""}
    if text is None:
        st["error"] = CANNOT_READ
        return st
    try:
        data = plain(tomllib.loads(text))
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    build = data.get("build")
    if build is not None and not isinstance(build, dict):
        return st  # build = 1: no add, no remove
    value = (build or {}).get("rustc-wrapper")
    st["value"], st["ours"] = value, value == want
    keyline = f"rustc-wrapper = {json.dumps(want)}"
    if value is None:
        expect = plain(data)
        table = expect.setdefault("build", {})
        if isinstance(table, dict):  # always: build is a table or absent here
            table["rustc-wrapper"] = want
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
                cands.append(text[:pos] + "\n" + SCCACHE_HEAD + "\n" + keyline + (rest or "\n"))
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
            table = expect["build"]
            if isinstance(table, dict):  # always: the line CST wrote is in [build]
                del table["rustc-wrapper"]
            alt = {k: v for k, v in expect.items() if not (k == "build" and v == {})}
            if parses_as(new, expect) or parses_as(new, alt):
                st["remove"] = new
    return st
