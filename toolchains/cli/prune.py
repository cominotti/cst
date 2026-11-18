# SPDX-License-Identifier: MPL-2.0
"""`prune plan|apply`: the checked settings of every target, the registry of
targets and the selection of the ones a run prunes.
"""

from __future__ import annotations

import os
import time

from collections.abc import Sequence

from cst_core.domain.errors import Refused, Usage
from toolchains.domain.common import CONF_REL, DEFAULTS_NAME
from toolchains.domain.prune import PLANNED, RUN_BUDGET, Settings, parse_defaults, checked_settings
from toolchains.adapters.program import Program, say, where_text
from toolchains.cli.python_uv import PythonUv
from toolchains.cli.rust_build import RustBuild


def defaults_path() -> str:
    """toolchains.conf next to this program: toolchains/toolchains.conf in
    CST, ~/.local/lib/cst/toolchains.conf for the timer's copy."""
    return os.path.join(Program.folder(), DEFAULTS_NAME)


def load_settings(home: str) -> Settings:
    """The checked settings of every target: defaults, then the user's file.
    Raises ConfigError on any bad line or value (nothing runs then)."""
    path = defaults_path()
    try:
        with open(path, encoding="utf-8") as f:
            text: str | None = f.read()
    except FileNotFoundError:
        text = None
    defaults = parse_defaults(path, text)
    try:
        with open(os.path.join(home, CONF_REL), encoding="utf-8") as f:
            user_text = f.read()
    except FileNotFoundError:
        user_text = ""
    return checked_settings(home, defaults, user_text)


# The registry. Adding a target: a Target subclass here (see
# toolchains/domain.py, SPECS, for the rest).
TARGETS: dict[str, type[RustBuild] | type[PythonUv]] = {cls.SPEC.id: cls for cls in (RustBuild, PythonUv)}


def select_targets(names: Sequence[str], settings: Settings) -> list[tuple[type[RustBuild] | type[PythonUv], bool]]:
    """[(Target class, named)]: the named targets (LANG:CACHE or LANG), or,
    without names, every enabled one. Raises Usage for an unknown or a
    planned target."""
    if not names:
        return [(cls, False) for tid, cls in TARGETS.items() if settings.enabled[tid]]
    out: list[tuple[type[RustBuild] | type[PythonUv], bool]] = []
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
    settings = load_settings(home)
    chosen = select_targets(names, settings)
    if not chosen:
        say(f"no target is enabled (settings: {settings.source}): nothing to do")
        return 0
    rc = 0
    deadline = None
    if action == "apply":
        deadline = time.monotonic() + RUN_BUDGET
        chosen = sorted(chosen, key=lambda c: c[0].SPEC.max_seconds is None)  # stable: bounded ones first
    for cls, named in chosen:
        t = cls.make(home, now, settings)
        t.named = named
        t.deadline = deadline
        off = "" if settings.enabled[cls.SPEC.id] else " (disabled in the settings: runs only because it was named)"
        if action == "plan":
            say(f"== {cls.SPEC.id}: {where_text(t.location(), home)}{off}")
            say(f"  policy: {settings.policy_text(cls.SPEC.id)} ({settings.source_of(cls.SPEC)})")
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
