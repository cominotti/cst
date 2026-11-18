# SPDX-License-Identifier: MPL-2.0
"""rustup: its folder, the default toolchain, the toolchain a rust-toolchain
file pins, the build files that name a toolchain, and the report of the installed
toolchains (InstalledTools, the InstalledToolsPort, with the sccache lines).
"""

from __future__ import annotations

import os
import shutil
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from toolchains.domain.prune import Limits

from toolchains.domain.build import TOOL_FILE_MAX, WORKSPACE_REL, short_toolchain, toolchain_pattern
from toolchains.domain.common import gib, tilde
from toolchains.adapters.build_tree import tree_size
from toolchains.adapters.cargo import claimed_folders
from toolchains.adapters.program import RUNNER, where_text
from toolchains.adapters.sccache import sccache_status_lines


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


def named_in(files: Sequence[str], names: Sequence[str], max_files: int) -> tuple[dict[str, str], bool]:
    """({name: first file that names it}, complete) for NAMES, read from
    FILES (TOOL_FILE_MAX bytes each; unreadable ones skipped). COMPLETE is
    False when there were more than MAX_FILES files to read."""
    pats = {n: (n.encode(), toolchain_pattern(n)) for n in names}
    found: dict[str, str] = {}
    seen: set[str] = set()
    for path in files:
        if len(found) == len(pats):
            break
        if path in seen:
            continue
        if len(seen) >= max_files:
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


def toolchain_lines(home: str, limits: Limits) -> list[str]:
    """The report of `status`: installed rustup toolchains, their size, and
    which rust-toolchain files under ~/Workspace pin them (LIMITS: how many
    build files it reads at most, and the seconds one git call may take)."""
    rh = rustup_home(home)
    tdir = os.path.join(rh, "toolchains")
    try:
        names = sorted(e.name for e in os.scandir(tdir) if e.is_dir())
    except OSError:
        return [f"{where_text(tdir, home)}: none installed (or not readable)"]
    ws = claimed_folders(None, home, limits, with_cargo=False)
    pins: dict[str, list[str]] = {}
    for p in ws.pins:
        ch = pinned_channel(p)
        if ch:
            pins.setdefault(ch, []).append(p)
    try:
        with open(os.path.join(rh, "settings.toml"), "rb") as f:
            overrides = tomllib.load(f).get("overrides") or {}
    except OSError, tomllib.TOMLDecodeError:
        overrides = {}
    for path, ch in overrides.items() if isinstance(overrides, dict) else ():
        if isinstance(ch, str):
            pins.setdefault(short_toolchain(ch), []).append(f"rustup override for {path}")
    default = rustup_default(home, rh)
    out = [f"where:    {where_text(tdir, home)}", f"default:  {short_toolchain(default) if default else 'unknown'}"]
    if ws.incomplete:
        out.append(f"note:     the search for rust-toolchain files was incomplete ({ws.incomplete})")
    rows: list[str] = []
    total = 0
    unused: list[str] = []
    is_default = {n: bool(default) and short_toolchain(n) == short_toolchain(default) for n in names}
    loose = [
        short_toolchain(n) for n in names if not is_default[n] and not pins.get(short_toolchain(n)) and not pins.get(n)
    ]
    # Files in the home first (a linked worktree elsewhere, e.g. in /tmp, is shown only when it is the only one).
    files = sorted(ws.tool_files, key=lambda p: not (p == home or p.startswith(home + "/")))
    named, read_all = named_in(files, loose, limits.tool_files_max) if loose else ({}, True)
    if not read_all:
        ws.incomplete = ws.incomplete or f"more than {limits.tool_files_max} build files to read"
    for n in names:
        short = short_toolchain(n)
        path = os.path.join(tdir, n)
        size = 0 if os.path.islink(path) else tree_size(path)
        total += size
        by = pins.get(short, []) + (pins.get(n, []) if n != short else [])
        if by:
            first = tilde(by[0], home)
            pin = f"pinned by {len(by)} file(s), e.g. {first}" if len(by) > 1 else f"pinned by {first}"
        elif is_default[n]:
            pin = "the default"
        elif short in named:
            pin = f"named in {tilde(named[short], home)} (not offered for removal)"
        else:
            pin = "not pinned or named under ~/Workspace"
            unused.append(short)
        rows.append(
            f"  {gib(size):>10}  {short:<22} {pin}"
            + ("  (a link: rustup toolchain link)" if os.path.islink(path) else "")
        )
    out.append(f"{len(names)} toolchain(s), {gib(total)} (as du shows it):")
    out += rows
    known = {short_toolchain(n) for n in names} | set(names)
    missing = sorted(ch for ch in pins if ch not in known)
    if missing:
        out.append("pinned but not installed (the next build there downloads it): " + ", ".join(missing))
    out.append(
        f"(read: rust-toolchain.toml and rust-toolchain files, Makefiles, justfiles, *.mk and *.sh "
        f"files, CI workflows and .cargo/config.toml of the git worktrees under ~/{WORKSPACE_REL}, and "
        "rustup overrides)"
    )
    if unused and ws.incomplete:
        out.append(
            f"not pinned, not the default and not named in the files read: {', '.join(unused)}; no "
            "removal command, because the search was incomplete"
        )
    elif unused:
        out.append(
            "not pinned, not the default and not named in those files. NOT CHECKED: projects outside "
            f"~/{WORKSPACE_REL}, other scripts and programs, RUSTUP_TOOLCHAIN. If none of those needs "
            "them, you can remove them yourself (CST never does):"
        )
        out.append(f"  rustup toolchain uninstall {' '.join(unused)}")
    return out


class InstalledTools:
    """InstalledToolsPort: the reports of status on what is installed in HOME besides the caches."""

    def __init__(self, home: str, limits: Limits) -> None:
        self.home = home
        self.limits = limits

    def rust_toolchains(self) -> list[str]:
        return toolchain_lines(self.home, self.limits)

    def sccache(self) -> list[str]:
        return sccache_status_lines(self.home)
