# SPDX-License-Identifier: MPL-2.0
"""rustup: its folder, the default toolchain, the toolchain a rust-toolchain
file pins, and the build files that name a toolchain.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from toolchains.domain import TOOL_FILES_MAX, TOOL_FILE_MAX, toolchain_pattern
from toolchains.adapters.program import RUNNER


def rustup_home(home: str) -> str:
    rh = os.environ.get("RUSTUP_HOME", "")
    return rh if os.path.isabs(rh) else os.path.join(home, ".rustup")


def pinned_channel(path: str) -> str:
    """The toolchain a rust-toolchain(.toml) file pins, or ''."""
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    if path.endswith(".toml") or "[toolchain]" in text:
        try:
            tc = tomllib.loads(text).get("toolchain")
        except tomllib.TOMLDecodeError:
            return ""
        ch = tc.get("channel") if isinstance(tc, dict) else None
        return ch.strip() if isinstance(ch, str) else ""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    return lines[0] if len(lines) == 1 else ""


def rustup_default(home: str, rh: str) -> str:
    """The default toolchain's name, or ''. settings.toml first; else asks
    rustup (read only)."""
    try:
        with open(os.path.join(rh, "settings.toml"), "rb") as f:
            d = tomllib.load(f).get("default_toolchain")
        if isinstance(d, str) and d:
            return d
    except OSError, tomllib.TOMLDecodeError:
        pass
    rustup = shutil.which("rustup")
    if not rustup:
        return ""
    env = dict(os.environ, RUSTUP_HOME=rh, RUSTUP_AUTO_INSTALL="0", HOME=home)
    env.pop("RUSTUP_TOOLCHAIN", None)
    r = RUNNER.run([rustup, "default"], cwd=home, base_env=env, timeout=20)
    if r.cause is not None:
        return ""
    words = r.stdout.split() if r.returncode == 0 else []
    return words[0] if words else ""


def named_in(files: Sequence[str], names: Sequence[str]) -> tuple[dict[str, str], bool]:
    """({name: first file that names it}, complete) for NAMES, read from
    FILES (TOOL_FILE_MAX bytes each; unreadable ones skipped). COMPLETE is
    False when there were more than TOOL_FILES_MAX files to read."""
    pats = {n: (n.encode(), toolchain_pattern(n)) for n in names}
    found: dict[str, str] = {}
    seen: set[str] = set()
    for path in files:
        if len(found) == len(pats):
            break
        if path in seen:
            continue
        if len(seen) >= TOOL_FILES_MAX:
            return found, False
        seen.add(path)
        try:
            with open(path, "rb") as f:
                data = f.read(TOOL_FILE_MAX)
        except OSError:
            continue
        for n, (raw, pat) in pats.items():
            # The plain substring test is fast; the word test only where it hits.
            if n not in found and raw in data and pat.search(data):
                found[n] = path
    return found, True
