# SPDX-License-Identifier: MPL-2.0
"""The scratch home of the btrfs-bulk tests (test_btrfs_bulk, test_btrfs_steam and the modules built on them).

BulkHome is the program put together as main() does (Wiring), over a scratch
home and scratch paths in a temporary folder; the fakes replace only the
primitives that need btrfs, root or the host (the methods of Primitives, the
busy check and the conversion of btrfs-snapshot): every step built on them is
the real code (classification, the copy with cp, the switch, the journal and
the recovery of interrupted runs). Its patches live in an ExitStack; close()
undoes them, then removes the folder.

A module of its own, not part of fakes.py: the importers of fakes.py do not
depend on btrfs.bulk. Not collected as a test module (unittest finds test*.py).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from unittest import mock

from btrfs.bulk.adapters.machine import Primitives
from btrfs.bulk.adapters.user import UserFiles
from btrfs.bulk.application import items as bulk_items
from btrfs.bulk.application.context import Bulk
from btrfs.bulk.domain import BulkPaths
from btrfs.bulk.wiring import Wiring
from cst_core.adapters.files import CounterFile
from cst_core.domain.shapes import json_object

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "btrfs-bulk")

type TreeEntry = tuple[str, str, str | bytes | int]


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

    def fake_primitives(self) -> None:
        """The fakes of what needs btrfs, root or the host, over the sets above."""

        def is_subvol(p: str) -> bool:
            try:
                return stat.S_ISDIR(os.lstat(p).st_mode) and inode(p) in self.subvols
            except OSError:
                return False

        def subvol_create(p: str) -> None:
            os.mkdir(p, 0o755)
            self.subvols.add(inode(p))

        def is_nocow(p: str) -> bool:
            return inode(p) in self.nocow

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

        self.fake(self.machine, "is_subvol", is_subvol)
        self.fake(self.machine, "subvol_create", subvol_create)
        self.fake(self.machine, "is_nocow", is_nocow)
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
            st = os.lstat(path)
            self.nocow.discard((st.st_dev, st.st_ino))
            self.subvols.discard((st.st_dev, st.st_ino))
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    st = os.lstat(os.path.join(root, n))
                    self.nocow.discard((st.st_dev, st.st_ino))
                    self.subvols.discard((st.st_dev, st.st_ino))
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
