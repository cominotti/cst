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
from typing import TYPE_CHECKING, TypedDict, override

if TYPE_CHECKING:
    from collections.abc import Sequence

from cst_core.domain.contracts import CARGO_CONFIG_REL, SCCACHE_WRAPPER_REL
from cst_core.domain.shapes import json_object, object_of, parse_json, parse_toml
from cst_core.domain.text import cgroup_v2_path
from toolchains.domain.build import HASH_VAR, ROOT_REL, expected_templates, expected_tmpfs_template
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


class ConfigTool(StrEnum):
    """The tool whose settings file config edits (cargo's build-dir, uv's link-mode)."""

    CARGO = "cargo"
    UV = "uv"


# Exactly what `cst btrfs snapshots setup` always wrote (same lines, same
# comments, same rules): a setting already there is never changed.
CARGO_CFG_REL = CARGO_CONFIG_REL
UV_CFG_REL = ".config/uv/uv.toml"
WS = r"[^\S\n]"  # a blank that is not a line break (grep's [[:space:]] within a line)
CARGO_HAS_BUILD_DIR = re.compile(rf"^{WS}*build-dir{WS}*=|^{WS}*build\.build-dir", re.MULTILINE)
CARGO_HAS_BUILD = re.compile(rf"^{WS}*\[{WS}*build{WS}*\]|^{WS}*build\.", re.MULTILINE)
UV_HAS_LINK_MODE = re.compile(rf"^{WS}*link-mode{WS}*=", re.MULTILINE)
UV_HAS_TABLE = re.compile(rf"^{WS}*\[", re.MULTILINE)


def cargo_text(home: str, *, tmpfs: bool = False) -> str:
    """[build] build-dir snippet appended to ~/.cargo/config.toml (NVMe default or dedicated tmpfs)."""
    if tmpfs:
        return (
            "\n# Added by toolchains-setup.sh (CST): intermediate build artifacts live in\n"
            "# ~/.cache/cargo-build-tmpfs (dedicated RAM tmpfs; cuts SSD wear).\n"
            f'[build]\nbuild-dir = "{expected_tmpfs_template(home)}"\n'
        )
    return (
        "\n# Added by btrfs-setup.sh (CST): intermediate build artifacts live in\n"
        "# ~/.cache (a subvolume excluded from /home snapshots); final ones stay in target/.\n"
        f'[build]\nbuild-dir = "{home}/{ROOT_REL}/{HASH_VAR}"\n'
    )


class BuildDirState(TypedDict):
    """Inspection of [build] build-dir in ~/.cargo/config.toml."""

    path: str
    error: str
    value: object
    ours: bool
    matches_want: bool
    by_hand: bool
    rewrite: str


def build_dir_switch_rewrite(text: str, current_template: str, target_template: str) -> str:
    """Rewrite CST's build-dir line from CURRENT_TEMPLATE to TARGET_TEMPLATE safely, or ''."""
    try:
        parsed = parse_toml(text)
    except tomllib.TOMLDecodeError:
        return ""
    if not json_object(parsed):
        return ""
    build = parsed.get("build")
    if not isinstance(build, dict) or build.get("build-dir") != current_template:
        return ""
    expect = plain(parsed)
    expect_build = expect["build"]
    if not isinstance(expect_build, dict):
        return ""
    expect_build["build-dir"] = target_template
    for old_lit, new_lit in (
        (json.dumps(current_template), json.dumps(target_template)),
        (f"'{current_template}'", f"'{target_template}'"),
    ):
        pat = re.compile(
            rf"^({WS}*(?:build\.)?build-dir{WS}*={WS}*){re.escape(old_lit)}({WS}*(?:#.*)?)$",
            re.MULTILINE,
        )
        matches = list(pat.finditer(text))
        if len(matches) == 1:
            candidate = pat.sub(lambda m, rep=new_lit: f"{m.group(1)}{rep}{m.group(2)}", text, count=1)
            if parses_as(candidate, expect):
                return candidate
    return ""


def build_dir_state(path: str, text: str | None, *, home: str, want_template: str) -> BuildDirState:
    """Inspect [build] build-dir in PATH against CST's templates and WANT_TEMPLATE."""
    st: BuildDirState = {
        "path": path,
        "error": "",
        "value": None,
        "ours": False,
        "matches_want": False,
        "by_hand": False,
        "rewrite": "",
    }
    if text is None:
        st["error"] = CANNOT_READ
        return st
    try:
        parsed = parse_toml(text)
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    build = parsed.get("build") if json_object(parsed) else None
    val = build.get("build-dir") if isinstance(build, dict) else None
    st["value"] = val
    ours_templates = set(expected_templates(home))
    if isinstance(val, str) and val in ours_templates:
        st["ours"] = True
        st["matches_want"] = val == want_template
        if val != want_template:
            st["rewrite"] = build_dir_switch_rewrite(text, val, want_template)
        return st
    if val is None and CARGO_HAS_BUILD.search(text):
        st["by_hand"] = True
    return st


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
    "# Your own crates keep line tables (backtraces show file:line) with unpacked debuginfo;\n"
    "# dependencies get no debug info. These lines take precedence over [profile.dev] in each project's Cargo.toml.\n"
)
LEAN_SHORT_HEAD = "# Added by toolchains-setup.sh (CST) for RUST_LEAN_DEBUGINFO=yes"
LEAN_KEYS: tuple[tuple[str, tuple[str, ...], str, object, str], ...] = (
    ("profile.dev", ("profile", "dev"), "debug", "line-tables-only", 'debug = "line-tables-only"'),
    ("profile.dev", ("profile", "dev"), "split-debuginfo", "unpacked", 'split-debuginfo = "unpacked"'),
    ('profile.dev.package."*"', ("profile", "dev", "package", "*"), "debug", False, "debug = false"),
)
LEAN_REBUILD = (
    "turning it on makes the next dev build of every workspace compile everything once "
    "(all at once if many worktrees build together): apply it while builds are quiet"
)


class SccacheInstallMethod(StrEnum):
    """How CST tries to install a missing sccache binary (in preference order)."""

    BINSTALL = "cargo-binstall"
    CARGO = "cargo"


class LeanState(TypedDict):
    path: str
    add: list[tuple[str, str]]
    have: list[str]
    conflicts: list[str]
    rewrite: str
    error: str


def _table(d: object, key: str) -> object:
    """d.setdefault(key, {}) of a JSON object; for any other value the AttributeError calling
    it raised."""
    if not json_object(d):
        raise AttributeError(f"{type(d).__name__!r} object has no attribute 'setdefault'")
    return d.setdefault(key, {})


def _set_item(d: object, key: str, value: object) -> None:
    """d[key] = value of a JSON object; for any other value the TypeError the assignment raised."""
    if not json_object(d):
        raise TypeError(f"{type(d).__name__!r} object does not support item assignment")
    d[key] = value


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


def same_lean_value(key: str, value: object, want: object) -> bool:
    """Compare one TOML setting value for lean debug info (debug accepts 0/'none' for False)."""
    return same_debug(value, want) if key == "debug" else value == want


def lean_state(path: str, text: str | None) -> LeanState:
    """{path, add: [(label, display_line)], have: [display_line], conflicts: [text],
    rewrite: new file text or '' (when rewriting safely is not possible), error: ''}."""
    st: LeanState = {"path": path, "add": [], "have": [], "conflicts": [], "rewrite": "", "error": ""}
    if text is None:
        st["error"] = CANNOT_READ
        return st
    try:
        data = parse_toml(text)
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    missing: list[tuple[str, tuple[str, ...], str, object, str]] = []
    for label_, tpath, key, want, keyline in LEAN_KEYS:
        table = dig(data, tpath)
        item = f"[{label_}] {keyline}"
        if isinstance(table, dict) and key in table:
            if same_lean_value(key, table[key], want):
                st["have"].append(item)
            else:
                st["conflicts"].append(f"[{label_}] {key} = {json.dumps(table[key])} is yours: left as it is")
        else:
            st["add"].append((label_, item))
            missing.append((label_, tpath, key, want, keyline))
    if missing:
        st["rewrite"] = _lean_rewrite(text, data, missing)
    return st


def _append_table_blocks(text: str, head: str, blocks: Sequence[str]) -> str:
    """TEXT with new TOML [table] BLOCKS appended cleanly after HEAD."""
    sep = "" if not text or text.endswith("\n") else "\n"
    return text + sep + head + "\n".join(blocks)


def _header_has_cst_comment(text: str, pos: int) -> bool:
    """True when one of the comment lines right above POS already carries LEAN_SHORT_HEAD."""
    above = text[:pos].splitlines()[-4:]
    return any(ln.startswith(LEAN_SHORT_HEAD) for ln in above)


def _lean_rewrite(text: str, data: object, missing: Sequence[tuple[str, tuple[str, ...], str, object, str]]) -> str:
    """Whole new TEXT with all MISSING lean settings applied, or '' if not safe."""
    expect = plain(object_of(parse_json(json.dumps(data))))
    by_table: dict[str, list[tuple[str, object, str]]] = {}
    try:
        for label_, tpath, key, want, keyline in missing:
            d: object = expect
            for k in tpath:
                d = _table(d, k)
            _set_item(d, key, want)
            by_table.setdefault(label_, []).append((key, want, keyline))
    except AttributeError, TypeError:
        return ""
    cands = [text]
    append_blocks: list[str] = []
    for label_, items in by_table.items():
        header_re = PROFILE_DEV_HEADER if label_ == "profile.dev" else PROFILE_DEV_PKG_HEADER
        matches = list(header_re.finditer(text))
        if len(matches) > 1:
            return ""
        lines = "\n".join(kl for _, _, kl in items)
        if len(matches) == 1:
            snippet = lines if _header_has_cst_comment(text, matches[0].start()) else f"{LEAN_SHORT_HEAD}\n{lines}"
            cands = [n for c in cands for h in list(header_re.finditer(c))[:1] for n in _after_header(c, h, snippet)]
            if not cands:
                return ""
        else:
            append_blocks.append(f"[{label_}]\n{lines}\n")
    if append_blocks:
        cands = [_append_table_blocks(c, LEAN_HEAD, append_blocks) for c in cands]
    return next((c for c in cands if parses_as(c, expect)), "")


# The opt-in sccache item. Off unless RUST_SCCACHE=yes (or config apply
# --sccache): a small wrapper (toolchains/rustc-wrapper, copied to
# ~/.local/libexec/cst/) named as rustc-wrapper in [build] of
# ~/.cargo/config.toml hands rustc to sccache. Unlike lean debug info,
# --no-sccache removes CST's line again: a wrapper left behind is riskier
# than a debug setting. RUST_SCCACHE=no alone never removes it.
WRAPPER_NAME = "rustc-wrapper"
WRAPPER_REL = SCCACHE_WRAPPER_REL
SCCACHE_DIR_REL = ".cache/sccache"


def wrapper_path(home: str) -> str:
    return os.path.join(home, WRAPPER_REL)


SCCACHE_DEFAULT_SIZE = "25G"
SCCACHE_HEAD = (
    "# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes: rustc goes through sccache (~/.cache/sccache)."
)
INCREMENTAL_HEAD = (
    "# Added by toolchains-setup.sh (CST) for RUST_INCREMENTAL=off: disable incremental compilation to cut disk writes."
)
SCCACHE_SIZE_LINE = re.compile(r"(?m)^cache_size=\S*(?= # cst: RUST_SCCACHE_CACHE_SIZE)")
SCCACHE_WORKSPACE_LINE = re.compile(r"(?m)^workspace=\S*(?= # cst: RUST_SCCACHE_WORKSPACE)")
SCCACHE_REBUILD = (
    "turning it on rebuilds nothing: the cache fills as crates are compiled anew "
    "(a new worktree, cargo update, a new toolchain)"
)
BUILD_HEADER = re.compile(rf"^{WS}*\[{WS}*build{WS}*\]{WS}*(?:#.*)?$", re.MULTILINE)
PROFILE_DEV_HEADER = re.compile(rf"^{WS}*\[{WS}*profile\.dev{WS}*\]{WS}*(?:#.*)?$", re.MULTILINE)
PROFILE_DEV_PKG_HEADER = re.compile(
    rf"^{WS}*\[{WS}*profile\.dev\.package\.{WS}*(?:\"\*\"|'\*'){WS}*\]{WS}*(?:#.*)?$",
    re.MULTILINE,
)
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
    path = cgroup_v2_path(cgroup_text or "") or ""
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
class SettingsRewrite:
    """What a rewrite of a tool's settings file did: error is '' when it worked. replaced: the new text
    is in place although the write failed (it failed after the rename, at the sync of its folder)."""

    error: str = ""
    replaced: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class WrapperValues:
    """The settings `config apply` writes into the installed wrapper."""

    size: str = SCCACHE_DEFAULT_SIZE
    workspace: bool = False  # local crates straight to rustc by default

    @override
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


class IncrementalState(TypedDict):
    path: str
    error: str
    value: object
    ours: bool
    add: str
    remove: str


@dataclasses.dataclass(frozen=True, slots=True)
class _BuildKeySpec:
    """One boolean/string setting managed inside cargo's [build] table."""

    key: str
    val: str | bool
    head: str
    toml_literal: str

    @property
    def keyline(self) -> str:
        return f"{self.key} = {self.toml_literal}"

    def line_pattern(self) -> re.Pattern[str]:
        return re.compile(rf"{WS}*{re.escape(self.key)}{WS}*={WS}*{re.escape(self.toml_literal)}{WS}*(?:#.*)?\n?\Z")


def _has_preceding_head(text: str, line_pat: re.Pattern[str], head: str) -> bool:
    """True when LINE_PAT matches one line in TEXT immediately preceded by HEAD
    (directly or after CST's appended [build] header)."""
    lines = text.splitlines(keepends=True)
    idx = [i for i, ln in enumerate(lines) if line_pat.match(ln)]
    if len(idx) != 1 or idx[0] == 0:
        return False
    i = idx[0]
    if lines[i - 1].startswith(head[:48]):
        return True
    return i > 1 and bool(BUILD_HEADER.match(lines[i - 1].rstrip("\r\n"))) and lines[i - 2].startswith(head[:48])


def sccache_state(path: str, text: str | None, want: str) -> SccacheState:
    """{path, error, value (build.rustc-wrapper now, or None), ours, add,
    remove}: add / remove are the whole new file text, '' when CST cannot
    make that change safely."""
    spec = _BuildKeySpec("rustc-wrapper", want, SCCACHE_HEAD, json.dumps(want))
    st = _build_key_state(path, text, spec)
    return {
        "path": st["path"],
        "error": st["error"],
        "value": st["value"],
        "ours": st["ours"],
        "add": st["add"],
        "remove": st["remove"],
    }


def incremental_state(path: str, text: str | None) -> IncrementalState:
    """{path, error, value (build.incremental now, or None), ours, add,
    remove} for RUST_INCREMENTAL=off/auto."""
    spec = _BuildKeySpec("incremental", val=False, head=INCREMENTAL_HEAD, toml_literal="false")
    return _build_key_state(path, text, spec, require_head_for_ours=True)


def _build_key_state(
    path: str, text: str | None, spec: _BuildKeySpec, *, require_head_for_ours: bool = False
) -> IncrementalState:
    st: IncrementalState = {"path": path, "error": "", "value": None, "ours": False, "add": "", "remove": ""}
    if text is None:
        st["error"] = CANNOT_READ
        return st
    try:
        parsed = parse_toml(text)
        data = plain(parsed if json_object(parsed) else {})
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    build = data.get("build")
    if build is not None and not isinstance(build, dict):
        return st
    value = (build or {}).get(spec.key)
    st["value"] = value
    line_pat = spec.line_pattern()
    is_ours = value == spec.val and (not require_head_for_ours or _has_preceding_head(text, line_pat, spec.head))
    st["ours"] = is_ours
    if value is None:
        st["add"] = _build_key_added(text, data, spec)
    elif is_ours:
        st["remove"] = _build_key_removed(text, data, spec, line_pat)
    return st


def _build_key_added(text: str, data: object, spec: _BuildKeySpec) -> str:
    """TEXT (DATA: parsed, with key absent) with [build] KEY = VAL added safely, or ''."""
    expect = plain(object_of(data))
    table = expect.setdefault("build", {})
    if isinstance(table, dict):
        table[spec.key] = spec.val
    headers = list(BUILD_HEADER.finditer(text))
    snippet = f"{spec.head}\n{spec.keyline}"
    cands: list[str] = []
    if len(headers) == 1:
        cands = _after_header(text, headers[0], snippet)
    elif not headers:
        sep = "" if not text or text.endswith("\n") else "\n"
        cands.append(text + sep + ("\n" if text else "") + f"{spec.head}\n[build]\n{spec.keyline}\n")
    return next((c for c in cands if parses_as(c, expect)), "")


def _after_header(text: str, h: re.Match[str], snippet: str) -> list[str]:
    """TEXT with SNIPPET inserted into the TOML section whose header is H."""
    nxt = ANY_HEADER.search(text, h.end())
    section_end = nxt.start() if nxt else len(text)
    keys = list(KEY_LINE.finditer(text, h.end(), section_end))
    spots = ([text.find("\n", keys[-1].start())] if keys else []) + [h.end()]
    cands: list[str] = []
    for spot in spots:
        pos = len(text) if spot < 0 else spot
        rest = text[pos:]
        cands.append(text[:pos] + "\n" + snippet + (rest or "\n"))
    return cands


def _csts_build_block(lines: Sequence[str], i: int, head: str) -> set[int]:
    """The other lines of CST's own appended [build] block around LINES[i], or empty."""
    after: list[str] = []
    for ln in lines[i + 1 :]:
        if ANY_HEADER.match(ln):
            break
        after.append(ln)
    if not (
        i > 1
        and BUILD_HEADER.match(lines[i - 1].rstrip("\r\n"))
        and lines[i - 2].startswith(head[:48])
        and not any(KEY_LINE.match(ln) for ln in after)
    ):
        return set()
    drop = {i - 1, i - 2}
    if i > 2 and not lines[i - 3].strip():
        drop.add(i - 3)
    return drop


def _build_key_removed(text: str, data: object, spec: _BuildKeySpec, pat: re.Pattern[str]) -> str:
    """TEXT without CST's key line (and its comment/empty [build] block), or ''."""
    lines = text.splitlines(keepends=True)
    idx = [i for i, ln in enumerate(lines) if pat.match(ln)]
    if len(idx) != 1:
        return ""
    i = idx[0]
    drop = {i}
    if i > 0 and lines[i - 1].startswith(spec.head[:48]):
        drop.add(i - 1)
    drop |= _csts_build_block(lines, i, spec.head)
    new = "".join(ln for n, ln in enumerate(lines) if n not in drop)
    expect = plain(object_of(data))
    table = expect["build"]
    if isinstance(table, dict):
        del table[spec.key]
    alt = {k: v for k, v in expect.items() if not (k == "build" and v == {})}
    return new if parses_as(new, expect) or parses_as(new, alt) else ""
