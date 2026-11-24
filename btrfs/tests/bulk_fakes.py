# SPDX-License-Identifier: MPL-2.0
"""The scratch home of the btrfs-bulk tests (test_btrfs_bulk, test_btrfs_steam and the modules built on them).

BulkHome is the program put together as main() does (Wiring), over a scratch
home and scratch paths in a temporary folder; the fakes replace only the
primitives that need btrfs, root or the host (the methods of Primitives, the
busy check and the conversion of btrfs-snapshot): every step built on them is
the real code (classification, the copy with cp, the switch, the journal and
the recovery of interrupted runs). Its patches live in an ExitStack; close()
undoes them, then removes the folder.

For the fault injection tests (tests/fault_injection.py) and the state
machine of tests/mbt: arm_points makes the calls that change the home or the
state files points of a FaultPlan, and use_tick_clock replaces the user
adapter's two 0.05 s sleeps by a wait for the filesystem's next ctime tick.

A module of its own, not part of fakes.py: the importers of fakes.py do not
depend on btrfs.bulk. Not collected as a test module (unittest finds test*.py).
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import Final
from unittest import mock

from btrfs.bulk.adapters import state as bulk_state, user as bulk_user
from btrfs.bulk.adapters.machine import Primitives
from btrfs.bulk.adapters.user import UserFiles
from btrfs.bulk.application import items as bulk_items
from btrfs.bulk.application.context import Bulk
from btrfs.bulk.domain import BulkPaths
from btrfs.bulk.wiring import Wiring
from cst_core.adapters.files import CounterFile
from cst_core.domain.shapes import json_object
from tests.fault_injection import FaultPlan, FaultyOs, FaultyRunner

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "btrfs-bulk")

type TreeEntry = tuple[str, str, str | bytes | int]

# The os functions through which btrfs.bulk.adapters.user changes the home (os.open: in the moves
# of an item, every call creates a file: the clock's stamp probe and CACHEDIR.TAG).
USER_OS_POINTS: Final = ("rename", "symlink", "mkdir", "rmdir", "unlink", "open")
# The ones of btrfs.bulk.adapters.state (its writes go through write_atomic, patched apart).
STATE_OS_POINTS: Final = ("remove",)


def inode(path: str) -> tuple[int, int]:
    """(dev, ino) of path, not followed: the key of the fake subvolume and +C sets."""
    st = os.lstat(path)
    return st.st_dev, st.st_ino


class BulkHome:
    """A scratch home with btrfs-bulk wired over it, and the fakes' state: subvols and nocow
    ((dev, ino) of fake subvolumes and of what has +C), busy (paths in use), flatpaks (app ->
    its filesystem overrides) and convert_calls (what btrfs-snapshot was asked to convert)."""

    def __init__(self) -> None:
        self.stack = contextlib.ExitStack()
        self.out: str = ""
        self.tmp = tempfile.mkdtemp(prefix="bulk-test-")
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        etc = os.path.join(self.tmp, "etc")
        os.mkdir(etc)
        self.paths = BulkPaths.under(
            etc=etc,
            state=os.path.join(self.tmp, "state"),
            lock=os.path.join(self.tmp, "lock"),
            notify=os.path.join(self.tmp, "no-notify"),
        )
        self.alerts = os.path.join(self.tmp, "alerts", "pending")
        self.machine = Primitives()
        self.wiring = Wiring(
            TOOL, root=False, paths=self.paths, home=self.home, machine=self.machine, alerts=self.alerts
        )
        self.host = self.wiring.host
        self.state = self.wiring.state()
        self.ctx = self.wiring.home_ctx(self.home)
        self.user = UserFiles(self.wiring.tree, None, self.ctx.home)
        self.bulk = Bulk(
            ctx=self.ctx,
            paths=self.paths,
            user=self.user,
            tree=self.wiring.tree,
            state=self.state,
            boots=CounterFile(self.paths.attempts),
            host=self.host,
            out=self.wiring.out,
        )
        # Fakes for what needs btrfs, root or the host.
        self.subvols: set[tuple[int, int]] = set()  # (dev, ino) of fake subvolumes
        self.nocow: set[tuple[int, int]] = set()  # (dev, ino) with +C
        self.busy: set[str] = set()
        self.flatpaks: dict[str, set[str]] = {}  # app -> set of filesystem overrides
        self.convert_calls: list[str] = []
        try:
            self.fake_primitives()
        except BaseException:
            self.close()
            raise

    def fake(self, owner: object, name: str, value: object) -> None:
        """owner.name is value until close()."""
        self.stack.enter_context(mock.patch.object(owner, name, value))

    def is_subvol(self, p: str) -> bool:
        """The fake is_subvol: p a folder (not followed) in subvols."""
        try:
            return stat.S_ISDIR(os.lstat(p).st_mode) and inode(p) in self.subvols
        except OSError:
            return False

    def is_nocow(self, p: str) -> bool:
        """The fake is_nocow: p (not followed) in nocow."""
        return inode(p) in self.nocow

    def forget(self, p: str) -> None:
        """p (not followed) neither a fake subvolume nor +C: what a recycled inode number had."""
        key = inode(p)
        self.nocow.discard(key)
        self.subvols.discard(key)

    def fake_primitives(self) -> None:
        """The fakes of what needs btrfs, root or the host, over the sets above."""

        def subvol_create(p: str) -> None:
            os.mkdir(p, 0o755)
            self.subvols.add(inode(p))

        def set_nocow(p: str) -> None:
            self.nocow.add(inode(p))

        real_copy = self.machine.copy_tree

        def copy_tree(src: str, dst: str, nocow: bool) -> None:
            real_copy(src, dst, nocow)
            if nocow:  # btrfs: new files inherit +C from their folder
                for root, dirs, files in os.walk(dst):
                    for n in dirs + files:
                        self.nocow.add(inode(os.path.join(root, n)))

        def busy_paths(paths: Iterable[str], home: str) -> set[str]:
            return {
                p for p in paths if any(p == b or p.startswith(b + "/") or b.startswith(p + "/") for b in self.busy)
            }

        self.fake(self.machine, "is_subvol", self.is_subvol)
        self.fake(self.machine, "subvol_create", subvol_create)
        self.fake(self.machine, "is_nocow", self.is_nocow)
        self.fake(self.machine, "set_nocow", set_nocow)
        self.fake(self.machine, "copy_tree", copy_tree)
        self.fake(self.host, "busy_paths", busy_paths)
        self.fake(self.machine, "mounts", list)
        self.fake(self.machine, "free_bytes", lambda path: 10**15)
        self.fake(self.machine, "flatpak_installed", lambda app: app in self.flatpaks)
        self.fake(self.machine, "flatpak_has_fs", lambda app, path: path in self.flatpaks.get(app, set()))
        self.fake(self.machine, "flatpak_add_fs", lambda app, path: self.flatpaks[app].add(path))
        self.fake(self.host, "convert_bulk_dir", lambda bulk: self.convert_calls.append(bulk) or [])

    def close(self) -> None:
        """Undo the patches, then remove the folder (read-only folders made writable first)."""
        self.stack.close()
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        shutil.rmtree(self.tmp)

    def rel(self, path: str) -> str:
        """path as a point names it: relative to the home (the state files by their names)."""
        return os.path.relpath(path, self.home) if path.startswith(self.home + "/") else os.path.basename(path)

    def root_wiring(self) -> Wiring:
        """The program as root (the user from bulk.conf or --user), over the same scratch paths and fakes."""
        return Wiring(TOOL, root=True, paths=self.paths, machine=self.machine, host=self.host)

    # ---- helpers ----
    def home_path(self, rel: str) -> str:
        """~/rel."""
        return os.path.join(self.home, rel)

    def bulk_path(self, rel: str = "") -> str:
        """~/Bulk/rel (~/Bulk without rel)."""
        return os.path.join(self.home, "Bulk", rel) if rel else os.path.join(self.home, "Bulk")

    def write(self, rel: str, data: str = "x") -> str:
        p = self.home_path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
        return p

    def read(self, rel: str) -> str:
        with open(self.home_path(rel)) as f:
            return f.read()

    def purge_tree(self, path: str) -> None:
        # Clear fake inode sets before deleting so recycled inodes on ext4/tmpfs do not look +C.
        if os.path.exists(path):
            self.forget(path)
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    self.forget(os.path.join(root, n))
            shutil.rmtree(path)

    def apply(self) -> tuple[list[str], list[str]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = bulk_items.apply(self.bulk)
        self.out = out.getvalue()
        return res

    def report(self, mode: str = "status") -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            left = bulk_items.report(self.bulk, mode)
        return left, out.getvalue()

    def tree(self) -> list[TreeEntry]:
        """Every path below the temp dir with its type, link target and content."""
        out: list[TreeEntry] = []
        for root, dirs, files in os.walk(self.tmp):
            for n in sorted(dirs + files):
                p = os.path.join(root, n)
                if p == self.paths.lock:
                    continue
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    out.append((p, "link", os.readlink(p)))
                elif stat.S_ISREG(st.st_mode):
                    with open(p, "rb") as f:
                        out.append((p, "file", f.read()))
                else:
                    out.append((p, "dir", stat.S_IMODE(st.st_mode)))
        return sorted(out)

    def journal(self, ident: str) -> dict[str, object]:
        """The journal of ident, as JSON."""
        with open(self.state.jpath(ident)) as f:
            j = json.load(f)
        if not json_object(j):
            raise AssertionError(f"not a journal: {j!r}")
        return j

    def write_journal(self, ident: str, j: Mapping[str, object]) -> None:
        """Put back an edited journal (as an older version would have written it)."""
        with open(self.state.jpath(ident), "w") as f:
            json.dump(j, f)

    def journals(self) -> list[str]:
        return (
            [f for f in os.listdir(self.paths.state) if f.startswith("journal-")]
            if os.path.isdir(self.paths.state)
            else []
        )


def fresh_ctime(probe: str) -> int:
    """The ctime of a file created at probe now (the file is removed again)."""
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        return os.fstat(fd).st_ctime_ns
    finally:
        os.close(fd)
        os.unlink(probe)


def tick_wait(folder: str) -> None:
    """Return once the filesystem's ctime clock has moved on: a fresh file in folder has a ctime
    other than one made at the start. Nothing done before then shares a ctime with what comes
    after, which is what the user adapter's 0.05 s sleeps are for; exact on a coarse clock (a
    kernel tick) and on a fine one, with no measured constant."""
    probe = os.path.join(folder, ".tick-probe")
    first = fresh_ctime(probe)
    while fresh_ctime(probe) == first:
        pass


class TickTime:
    """The time module as btrfs.bulk.adapters.user sees it: sleep() waits for the next ctime tick of
    folder (tick_wait) instead of its 0.05 s; every other name is time's own."""

    def __init__(self, folder: str) -> None:
        self.folder = folder

    def sleep(self, _seconds: float) -> None:
        tick_wait(self.folder)

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


def use_tick_clock(home: BulkHome) -> None:
    """The user adapter's sleeps wait for one ctime tick of the home's scratch folder (outside the home)."""
    home.fake(bulk_user, "time", TickTime(home.tmp))


def arm_points(home: BulkHome, plan: FaultPlan, *, torn_rmtree: bool) -> None:
    """Every call by which btrfs-bulk changes the home or its state files goes through plan:

    - os.NAME of the user adapter (USER_OS_POINTS) and the state adapter (STATE_OS_POINTS);
    - "write NAME": a state file's write_atomic (a journal, the boot flag, the keep file, the
      convert plan), by its file name;
    - "cp DEST": the copy (a fault answers exit 1: copy_tree's "copy failed");
    - "rmtree PATH": the user adapter's u_rmtree, one call; with torn_rmtree, a folder holding two
      or more files is two points, "rmtree-part PATH" (its first file in sorted walk order) and
      "rmtree PATH" (the rest): a deletion cut half-way (FaultPlan keeps every other call atomic).

    The commands that only read (stat, du, flatpak) are no points. restorecon is not run (u_relabel
    answers True): it is best effort, its errors suppressed, and changes no data."""
    home.fake(bulk_user, "os", FaultyOs(plan, USER_OS_POINTS))
    home.fake(bulk_state, "os", FaultyOs(plan, STATE_OS_POINTS))
    real_write = bulk_state.write_atomic
    home.fake(
        bulk_state,
        "write_atomic",
        lambda path, data, **kw: plan.step(
            f"write {os.path.basename(path)}", functools.partial(real_write, path, data, **kw)
        ),
    )
    home.fake(
        home.machine,
        "runner",
        FaultyRunner(
            plan, home.machine.runner, reads=lambda argv: argv[0] != "cp", name=lambda argv: f"cp {home.rel(argv[-1])}"
        ),
    )
    home.fake(home.user, "u_relabel", lambda path: True)
    real_rmtree = home.user.u_rmtree

    def rmtree(path: str) -> None:
        name = home.rel(path)
        if torn_rmtree and os.path.isdir(path) and not os.path.islink(path):
            files = sorted(os.path.join(root, n) for root, _dirs, names in os.walk(path) for n in names)
            if len(files) > 1:
                plan.step(f"rmtree-part {name}", functools.partial(os.unlink, files[0]))
        plan.step(f"rmtree {name}", functools.partial(real_rmtree, path))

    home.fake(home.user, "u_rmtree", rmtree)
