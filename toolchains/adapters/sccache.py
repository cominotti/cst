# SPDX-License-Identifier: MPL-2.0
"""sccache: the per-environment servers (their sockets), their statistics, and the cache's status lines."""

from __future__ import annotations

import os
import shutil
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

from toolchains.domain import SCCACHE_DIR_REL, SCCACHE_STATS, gib
from toolchains.adapters.build_tree import tree_size
from toolchains.adapters.program import RUNNER, where_text


def sccache_stats(exe: str, env: Mapping[str, str]) -> dict[str, str]:
    """The fields of `EXE --show-stats` in ENV (stderr dropped, within 5 s);
    {} when it did not run or failed. Bytes that are not UTF-8 become U+FFFD."""
    r = RUNNER.run([exe, "--show-stats"], base_env=env, timeout=5, output="stdout")
    return dict(SCCACHE_STATS.findall(r.stdout)) if r.cause is None and r.returncode == 0 else {}


def sccache_sockets(home: str) -> list[str]:
    """The per-environment server sockets the wrapper names (all
    environments share $XDG_RUNTIME_DIR, or the fallback folder)."""
    dirs: list[str] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and os.path.isdir(xdg):
        dirs.append(xdg)
    dirs.append(os.path.join(home, SCCACHE_DIR_REL, "run"))
    out: list[str] = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        out += [os.path.join(d, n) for n in names if n.startswith("cst-sccache-") and n.endswith(".sock")]
    return out


def socket_answers(path: str) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def sccache_status_lines(home: str) -> list[str]:
    """The cache's size and, with sccache on this PATH, one line of stats
    per running server (status runs on the host: its PATH)."""
    cache = os.path.join(home, SCCACHE_DIR_REL)
    out: list[str] = []
    if os.path.isdir(cache):
        out.append(
            f"sccache cache: {where_text(cache, home)} {gib(tree_size(cache))} (sccache keeps it under "
            f"its size cap itself, least recently used first; ~/.cache is not in /home snapshots)"
        )
    else:
        out.append(f"sccache cache: ~/{SCCACHE_DIR_REL} not there yet")
    exe = shutil.which("sccache") or ""
    if not exe:
        # `cargo install sccache` lands in ~/.cargo/bin, which a PATH may lack.
        ch = os.environ.get("CARGO_HOME", "")
        c = os.path.join(ch if os.path.isabs(ch) else os.path.join(home, ".cargo"), "bin", "sccache")
        exe = c if os.access(c, os.X_OK) else ""
    socks = [p for p in sccache_sockets(home) if socket_answers(p)]
    if not exe:
        if socks:
            out.append(f"sccache servers running: {len(socks)} (no sccache on the host's PATH to ask them)")
        return out
    if not socks:
        out.append("sccache: no server running (each environment starts its own on its first build)")
    for sock in socks:
        env_id = os.path.basename(sock)[len("cst-sccache-") : -len(".sock")]
        env = {k: v for k, v in os.environ.items() if not k.startswith("SCCACHE_")}
        # TMPDIR=/tmp as in rustc-wrapper: if the server stops right after the
        # socket check, this client call starts a new one, which keeps its TMPDIR.
        env.update(SCCACHE_SERVER_UDS=sock, NO_COLOR="1", TMPDIR="/tmp")
        stats = sccache_stats(exe, env)
        if not stats:
            out.append(f"sccache server {env_id}: no answer")
            continue
        shown = ", ".join(
            f"{k.lower()} {stats[k]}"
            for k in ("Compile requests", "Cache hits", "Cache misses", "Cache size", "Max cache size")
            if k in stats
        )
        out.append(f"sccache server {env_id}: {shown}")
    return out
