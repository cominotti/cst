# SPDX-License-Identifier: MPL-2.0
"""What prune knows of every target, no I/O: the registry (TargetSpec,
SPECS) and the selection of a run's targets, the checked settings
(Settings), the saved summary of a run and its status line, the planned
targets and the bounds of a run (Limits).
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence, Set as AbstractSet

from cst_core.domain.errors import Usage
from cst_core.domain.values import Json
from toolchains.domain.build import CMD_TIMEOUT, TOOL_FILES_MAX, RustPolicy
from toolchains.domain.common import CONF_REL, ConfigError, OPTIONS, gib, parse_bool, parse_conf, tilde, when
from toolchains.domain.uv import UV_TIMEOUT, UvPolicy


@dataclasses.dataclass(frozen=True, slots=True)
class TargetSpec:
    """What a prune target is, apart from how it prunes: its id ("lang:cache", as typed on
    the command line), its settings prefix and its own setting names (ENABLED is common),
    one line for status, the most seconds one apply can take (None: only the run's
    budget; prune apply runs the bounded targets first), and whether it looks for a
    program on PATH (auto on then checks the user manager's PATH, which the timer runs
    with)."""

    id: str
    prefix: str
    what: str
    settings: tuple[str, ...]
    max_seconds: int | None = None
    uses_path: bool = False


RUST_BUILD = TargetSpec(
    "rust:build",
    "RUST_BUILD",
    "cargo's intermediate build files (build-dir), one folder per workspace path",
    ("MAX_IDLE", "MAX_TOTAL", "MIN_KEEP", "ORPHANS", "INCREMENTAL_IDLE"),
)
PYTHON_UV = TargetSpec(
    "python:uv",
    "PYTHON_UV",
    "uv's package cache (uv cache prune: only entries uv no longer uses)",
    ("INTERVAL",),
    max_seconds=UV_TIMEOUT,
    uses_path=True,
)
# The registry, in the order prune, status and the settings take them. Adding a
# target: its TargetSpec here, its policy in Settings, its use case, its keys in
# toolchains/toolchains.conf and a line in toolchains/README.md.
SPECS: dict[str, TargetSpec] = {s.id: s for s in (RUST_BUILD, PYTHON_UV)}


def known_keys() -> set[str]:
    keys = set(OPTIONS)
    for spec in SPECS.values():
        keys.add(f"{spec.prefix}_ENABLED")
        keys.update(f"{spec.prefix}_{k}" for k in spec.settings)
    return keys


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    """The checked settings of every target: the defaults, then the user's file
    (parse_defaults, then checked_settings). source: the user's file as shown (~/...);
    user_keys: the keys it sets."""

    source: str
    user_keys: frozenset[str]
    enabled: Mapping[str, bool]
    options: Mapping[str, bool | str]
    rust: RustPolicy
    uv: UvPolicy

    def source_of(self, spec: TargetSpec) -> str:
        return key_source(self.source, self.user_keys, *(f"{spec.prefix}_{k}" for k in ("ENABLED", *spec.settings)))

    def policy_text(self, tid: str) -> str:
        """Target TID's policy as plan and status show it; KeyError for an id of no target here."""
        if tid == RUST_BUILD.id:
            return self.rust.text()
        if tid == PYTHON_UV.id:
            return self.uv.text()
        raise KeyError(tid)


def key_source(source: str, user_keys: AbstractSet[str], *keys: str) -> str:
    """Where KEYS are set: SOURCE (the user's file, as shown) when it sets any of them, else "defaults"."""
    return source if any(k in user_keys for k in keys) else "defaults"


def parse_defaults(path: str, text: str | None) -> dict[str, str]:
    """The defaults file (toolchains.conf next to the program; TEXT None: it is missing),
    checked: every key known, none lacking. Raises ConfigError (nothing runs then)."""
    known = known_keys()
    if text is None:
        raise ConfigError(f"the defaults file {path} is missing")
    try:
        defaults = parse_conf(text, known)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None
    lacking = sorted(known - set(defaults))
    if lacking:
        raise ConfigError(f"{path} lacks {', '.join(lacking)}")
    return defaults


def checked_settings(home: str, defaults: Mapping[str, str], user_text: str) -> Settings:
    """The settings: DEFAULTS (parse_defaults) with the user's file (USER_TEXT, '' when
    there is none) on top. Raises ConfigError on any bad line or value."""
    known = known_keys()
    source = tilde(os.path.join(home, CONF_REL), home)
    try:
        user = parse_conf(user_text, known)
    except ConfigError as e:
        raise ConfigError(f"{source}: {e}") from None
    user_keys = frozenset(user)
    raw = {**defaults, **user}

    def where(*keys: str) -> str:
        return key_source(source, user_keys, *keys)

    options: dict[str, bool | str] = {}
    for key, parse in OPTIONS.items():
        try:
            options[key] = parse(raw[key])
        except ConfigError as e:
            raise ConfigError(f"{where(key)}: {key}: {e}") from None
    enabled: dict[str, bool] = {}

    def checked[P](spec: TargetSpec, parse_policy: Callable[[Mapping[str, str], str], P]) -> P:
        key = f"{spec.prefix}_ENABLED"
        try:
            enabled[spec.id] = parse_bool(raw[key])
        except ConfigError as e:
            raise ConfigError(f"{where(key)}: {key}: {e}") from None
        vals = {k: raw[f"{spec.prefix}_{k}"] for k in spec.settings}
        try:
            return parse_policy(vals, spec.prefix)
        except ConfigError as e:
            raise ConfigError(f"{where(*(f'{spec.prefix}_{k}' for k in spec.settings))}: {e}") from None

    rust = checked(RUST_BUILD, RustPolicy.parse)
    uv = checked(PYTHON_UV, UvPolicy.parse)
    return Settings(source, user_keys, enabled, options, rust, uv)


class RustRun(TypedDict):
    """What rust:build's apply saves of its run (~/.local/state/cst/toolchains-rust-build.json)."""

    time: float
    deleted: int
    freed: int
    total: int
    folders: int
    busy: int
    failed: int
    trimmed: int
    trim_freed: int


class UvRun(TypedDict):
    """What python:uv's apply saves of its run (~/.local/state/cst/toolchains-python-uv.json)."""

    time: float
    removed: int
    freed: int


type RunRecord = RustRun | UvRun


def json_number(value: Json) -> float:
    """A number of a saved run summary (a bool counts, as it did); TypeError for another
    value, as the arithmetic on it raised."""
    if isinstance(value, int | float):
        return value
    raise TypeError(f"not a number: {value!r}")


def rust_last_run(s: Mapping[str, Json]) -> str:
    """One line of status from rust:build's saved summary (an older one may lack keys)."""
    extra = f", {s['busy']} busy" if s.get("busy") else ""
    extra += f", {s['failed']} failed" if s.get("failed") else ""
    trim = (
        f", incremental of {s['trimmed']} folder(s) ({gib(json_number(s.get('trim_freed', 0)))})"
        if s.get("trimmed")
        else ""
    )
    return (
        f"{when(json_number(s['time']))}, deleted {s['deleted']} folder(s) ({gib(json_number(s['freed']))})"
        f"{trim}{extra}; left {gib(json_number(s['total']))} in {s['folders']} folder(s)"
    )


def last_run_text(tid: str, s: Mapping[str, Json]) -> str:
    """One line of status from target TID's saved summary; KeyError for an id of no target here."""
    if tid == RUST_BUILD.id:
        return rust_last_run(s)
    if tid == PYTHON_UV.id:
        return uv_last_run(s)
    raise KeyError(tid)


def uv_last_run(s: Mapping[str, Json]) -> str:
    """One line of status from python:uv's saved summary."""
    return (
        f"{when(json_number(s['time']))}, removed {s.get('removed', 0)} file(s) ({gib(json_number(s.get('freed', 0)))})"
    )


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


@dataclasses.dataclass(frozen=True, slots=True)
class Limits:
    """The bounds of one run: the time budget of `prune apply` (RUN_BUDGET), the
    seconds one git or cargo call of the workspace search may take (CMD_TIMEOUT)
    and the build files the toolchains report reads at most (TOOL_FILES_MAX).
    The program runs with the defaults; tests make them small."""

    run_budget: int = RUN_BUDGET
    cmd_timeout: int = CMD_TIMEOUT
    tool_files_max: int = TOOL_FILES_MAX


def select_targets(names: Sequence[str], enabled: Mapping[str, bool]) -> list[tuple[str, bool]]:
    """[(target id, named)]: the named targets (LANG:CACHE or LANG), or,
    without names, every enabled one. Raises Usage for an unknown or a
    planned target."""
    if not names:
        return [(tid, False) for tid in SPECS if enabled[tid]]
    out: list[tuple[str, bool]] = []
    for name in names:
        if ":" in name:
            if name in SPECS:
                found = [name]
            elif name in PLANNED:
                raise Usage(f"target {name} is not implemented yet (implemented: {', '.join(SPECS)})")
            else:
                raise Usage(f"unknown target '{name}' (implemented: {', '.join(SPECS)})")
        else:
            found = [tid for tid in SPECS if tid.split(":")[0] == name]
            if not found:
                planned = [t for t in PLANNED if t.split(":")[0] == name]
                if planned:
                    raise Usage(f"no target of {name} is implemented yet (planned: {', '.join(planned)})")
                raise Usage(f"unknown target '{name}' (implemented: {', '.join(SPECS)})")
        for tid in found:
            if all(tid != c for c, _ in out):
                out.append((tid, True))
    return out
