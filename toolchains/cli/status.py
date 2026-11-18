# SPDX-License-Identifier: MPL-2.0
"""`status`: the targets, the timer, the rust toolchains report, the toolchain
settings and the sccache servers.
"""

from __future__ import annotations

import os
import tomllib

from toolchains.domain import (
    ConfigError,
    PLANNED,
    TOOL_FILES_MAX,
    WORKSPACE_REL,
    gib,
    label,
    short_toolchain,
    tilde,
)
from toolchains.adapters.build_tree import tree_size
from toolchains.adapters.home_files import old_conf_note, read_state
from toolchains.adapters.program import PROG, say, where_text
from toolchains.adapters.rustup import named_in, pinned_channel, rustup_default, rustup_home
from toolchains.adapters.sccache import sccache_status_lines
from toolchains.cli.auto import stale_files, timer_text, unit_states
from toolchains.cli.config import config_lines
from toolchains.cli.prune import Settings, TARGETS
from toolchains.cli.rust_build import claimed_folders


def toolchain_lines(home: str) -> list[str]:
    """The report of `status`: installed rustup toolchains, their size, and
    which rust-toolchain files under ~/Workspace pin them."""
    rh = rustup_home(home)
    tdir = os.path.join(rh, "toolchains")
    try:
        names = sorted(e.name for e in os.scandir(tdir) if e.is_dir())
    except OSError:
        return [f"{where_text(tdir, home)}: none installed (or not readable)"]
    ws = claimed_folders(None, home, with_cargo=False)
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
    named, read_all = named_in(files, loose) if loose else ({}, True)
    if not read_all:
        ws.incomplete = ws.incomplete or f"more than {TOOL_FILES_MAX} build files to read"
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


def cmd_status(home: str, now: float, brief: bool) -> int:
    say(f"auto: {timer_text(home)}")
    settings: Settings | None
    try:
        settings = Settings(home)
    except ConfigError as e:
        settings = None
        say(f"BAD SETTINGS: {e} (prune and the timer do nothing until it is fixed)")
    for tid, cls in TARGETS.items():
        s = read_state(home, tid)
        last = cls.last_run_text(s) if s else "none recorded"
        if brief:
            say(f"{tid}: last prune: {last}")
            continue
        say(f"== {tid}: {cls.WHAT}")
        t = cls(home, now, settings.policy[tid] if settings else None)
        say(f"  where:    {where_text(t.location(), home)}")
        if settings:
            state = "enabled" if settings.enabled[tid] else "disabled (prune runs it only when named)"
            say(f"  policy:   {state}; {cls.policy_text(settings.policy[tid])} ({settings.source_of(cls)})")
        else:
            say("  policy:   BAD SETTINGS (see above)")
        say(f"  last prune: {last}")
        for line in t.status_lines():
            say(f"  {line}")
    if brief:
        return 0
    say("== not implemented yet (nothing pruned there by CST):")
    for tid, (where, how) in PLANNED.items():
        shown = ", ".join(where_text(os.path.join(home, p.strip()[2:]), home) for p in where.split(","))
        say(f"  {tid:<14} {shown} ({how})")
    say("== rust toolchains (a report: CST never removes one)")
    for line in toolchain_lines(home):
        say(f"  {line}")
    say(f"== toolchain settings ({PROG} config)")
    for st, shown, note in config_lines(home, "status", settings):
        say(f"  {st:<8} {shown:<24} {note}")
    for line in sccache_status_lines(home):
        say(f"  {line}")
    st = unit_states(home)
    say("== files of auto on: " + ", ".join(f"{label(k)} {v}" for k, v in st.items()))
    for path, ours in stale_files(home):
        say(
            f"  stale: {tilde(path, home)} (of the earlier `cst btrfs cargo-gc`"
            + ("; auto on or auto off removes it)" if ours else "; not CST's: left alone)")
        )
    old_conf_note(home)
    return 0
