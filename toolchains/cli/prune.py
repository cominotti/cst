# SPDX-License-Identifier: MPL-2.0
"""`prune plan|apply`: the checked settings of every target, the registry of
targets and the selection of the ones a run prunes.
"""

from __future__ import annotations

import os
import time
from typing import Any

from collections.abc import Sequence

from cst_core.domain.errors import Refused, Usage
from toolchains.domain.common import CONF_REL, ConfigError, DEFAULTS_NAME, OPTIONS, parse_bool, parse_conf, tilde
from toolchains.domain.prune import PLANNED, RUN_BUDGET
from toolchains.adapters.program import Program, say, where_text
from toolchains.cli.python_uv import PythonUv
from toolchains.cli.rust_build import RustBuild
from toolchains.cli.targets import Target


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
