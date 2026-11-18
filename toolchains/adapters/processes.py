# SPDX-License-Identifier: MPL-2.0
"""The user's processes: the ones that use a build folder, and the cargo
processes (their folder, program, arguments and settings).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from toolchains.domain.build import CARGO_COMM, CARGO_ENV, CargoProc, RUST_TOOLS, TRASH_PREFIX, prefixes


PROC = "/proc"


def own_processes(target_uid: int) -> Iterator[tuple[str, str, str]]:
    """(pid, comm, /proc/PID) of each process of target_uid but this one; a
    process that ended or cannot be read meanwhile is left out."""
    me = os.getpid()
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return
    for pid in pids:
        if int(pid) == me:
            continue
        base = os.path.join(PROC, pid)
        try:
            if os.stat(base).st_uid != target_uid:
                continue
            with open(os.path.join(base, "comm")) as f:
                comm = f.read().strip()
        except OSError:
            continue
        yield pid, comm, base


def proc_users(root: str, uid: int | None = None) -> tuple[dict[str, list[tuple[str, str]]], int]:
    """({folder key: [(pid, comm), ...]}, unreadable): the processes of the
    user that have their cwd, executable, an open file or a mapped file
    under ROOT. Keys are 'XX/YYYY' or a trash name. Processes of other users
    are not looked at; UNREADABLE counts the user's own that could not be
    inspected (from inside a container: those in other containers' user
    namespaces; the host sees them all) and that are Rust tools (cargo,
    rustc, clippy-driver, rust-analyzer, build scripts: by their name)."""
    target_uid = os.getuid() if uid is None else uid
    pre = prefixes(root)
    found: dict[str, list[tuple[str, str]]] = {}
    unreadable = 0

    def note(pid: str, comm: str, path: str) -> None:
        if path.endswith(" (deleted)"):
            path = path[: -len(" (deleted)")]
        for p in pre:
            if path.startswith(p + "/"):
                parts = path[len(p) + 1 :].split("/")
                key = parts[0] if parts[0].startswith(TRASH_PREFIX) else "/".join(parts[:2])
                lst = found.setdefault(key, [])
                if (pid, comm) not in lst:
                    lst.append((pid, comm))
                return

    for pid, comm, base in own_processes(target_uid):
        try:
            note(pid, comm, os.readlink(os.path.join(base, "cwd")))
        except PermissionError:
            if RUST_TOOLS.fullmatch(comm):
                unreadable += 1
            continue
        except OSError:
            pass
        try:
            note(pid, comm, os.readlink(os.path.join(base, "exe")))
        except OSError:
            pass
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            fds = []
        for fd in fds:
            try:
                note(pid, comm, os.readlink(os.path.join(base, "fd", fd)))
            except OSError:
                pass
        try:
            with open(os.path.join(base, "maps")) as f:
                for line in f:
                    i = line.find("/")
                    if i > 0:
                        note(pid, comm, line[i:].rstrip("\n"))
        except OSError:
            pass
    return found, unreadable


def cargo_procs(uid: int | None = None) -> list[CargoProc]:
    """The user's processes named cargo or cargo-* (cargo-nextest,
    cargo-clippy, ...). cwd is None when it cannot be read."""
    target_uid = os.getuid() if uid is None else uid
    out: list[CargoProc] = []
    for pid, comm, base in own_processes(target_uid):
        if not CARGO_COMM.fullmatch(comm):
            continue
        cwd: str | None = None
        exe = ""
        args: tuple[str, ...] = ()
        env: dict[str, str] = {}
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            pass
        try:
            exe = os.readlink(os.path.join(base, "exe"))
        except OSError:
            pass
        raw = b""
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                args = tuple(a.decode("utf-8", "replace") for a in f.read().split(b"\0")[:-1])
            with open(os.path.join(base, "environ"), "rb") as f:
                raw = f.read()
        except OSError:
            pass
        for item in raw.split(b"\0"):
            k, sep, v = item.decode("utf-8", "replace").partition("=")
            if sep and k in CARGO_ENV:
                env[k] = v
        if not os.path.exists(base):
            continue  # ended meanwhile
        # Empty (the kernel gives nothing while a process starts or ends; also a cargo started with
        # no environment, which one read cannot tell apart) or unreadable: its settings are not
        # known, so neither is its folder (never guessed as cargo's defaults).
        out.append(CargoProc(pid, comm, cwd, exe, args, env, read=bool(args) and bool(raw)))
    return out
