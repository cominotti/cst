# SPDX-License-Identifier: MPL-2.0
"""Unit tests for btrfs/docker-volumes-nocow (Docker volumes without copy-on-write).

The helper runs as root at boot and rewrites Docker volumes. These tests
build its use cases over the real adapters (btrfs/docker_volumes_nocow
wiring.build_nocow) and never let them reach the machine:

- every path it writes or reads state from (NocowPaths: volumes, flag,
  attempts, postponed, notify) points into a temporary folder of the test,
  and so does proc, the /proc where it looks for a dockerd (a fake tree: no
  process unless a test adds one);
- the file attribute ioctls (FS_IOC_GETFLAGS / FS_IOC_SETFLAGS, under
  flags() and set_nocow()) go to a fake that keeps the flags of each inode in
  a dictionary, and refuses any file outside the temporary folder;
- the commands it runs (systemctl, chcon, cp, btrfs, btrfs-notify) go to a
  fake command runner; any other command fails the test. The fake cp
  copies the tree in Python and gives each new entry the NOCOW flag of its
  folder, as btrfs does when a file is created;
- os.sync() is replaced by a counter.

Starting the program (its package imported anew) reads and changes nothing;
its paths read only /etc/docker/daemon.json (ImportTest checks both).

A crash (a kill, a timeout or a power loss) is simulated by raising Crash, a
BaseException, at one step: migrate() catches Exception for each volume, so
Crash leaves the helper the way a dead process would. The next call of
migrate is the next boot.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_docker_volumes_nocow -v
"""

import builtins
import contextlib
import errno
import fcntl
import gc
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from collections.abc import Callable
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from typing import override
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_docker_volumes_nocow.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.docker_volumes_nocow import adapters as nocow_adapters
from btrfs.docker_volumes_nocow import cli as nocow_cli
from btrfs.docker_volumes_nocow import wiring as nocow_wiring
from btrfs.docker_volumes_nocow.application import DockerVolumesNocow
from btrfs.docker_volumes_nocow.domain import (
    NocowCommand,
    NocowPaths,
    TreeScan,
    VolumeLeftovers,
    old_copy_name,
    old_copy_stamp,
    oldest_first,
    postponed_line,
    recovery,
)
from btrfs.tests.fakes import NOCOW, OTHER, Crash, FakeSystem, carries_flags, entries, tree
from cst_core.adapters import nocow as core_nocow
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.domain.shapes import object_of, parse_json
from tests.probes import assert_replaced_durably, recorded_fsyncs
from tests.script_loader import load_script

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "docker-volumes-nocow")
PKG = "btrfs.docker_volumes_nocow"


def load_program_fresh() -> ModuleType:
    """Run the program file as a module with its package imported anew, as a
    start of the program does; return that fresh adapters module. The
    package modules the tests use are put back afterwards."""
    saved = {n: m for n, m in sys.modules.items() if n == PKG or n.startswith(PKG + ".")}
    for n in saved:
        del sys.modules[n]
    try:
        load_script(TOOL, "docker_volumes_nocow_import_check")
        return sys.modules[PKG + ".adapters"]
    finally:
        sys.modules.pop("docker_volumes_nocow_import_check", None)
        for n in [n for n in sys.modules if n == PKG or n.startswith(PKG + ".")]:
            del sys.modules[n]
        sys.modules.update(saved)
        vars(sys.modules["btrfs"])["docker_volumes_nocow"] = saved[PKG]


TAG = "docker-volumes-nocow"
GIB = 1024**3

# The volume content most tests use: nested folders, files with data, an empty
# file, a symlink, a fifo and an empty folder. 2 MiB of data in all, plus 13
# bytes, so the log and status show "2 MiB".
DATA = {
    "db/base/1": b"\x01" * (1 << 20),
    "db/base/empty": b"",
    "db/wal/000001": bytes(range(256)) * 4096,
    "README": b"hello volume\n",
}
LINKS = {"latest": "db/wal/000001"}
FIFOS = ("db/pipe",)
EMPTY_DIRS = ("cache",)
DATA_BYTES = sum(len(v) for v in DATA.values())
DATA_ENTRIES = len(DATA) + len(LINKS) + len(FIFOS) + len(EMPTY_DIRS) + 3  # + db, db/base, db/wal


def ino(path: str) -> int:
    return os.lstat(path).st_ino


def cp_fails(src: str, dst: str) -> tuple[int, str]:
    """A cp that fails at once, copying nothing."""
    return 1, "cp: boom"


def check_failed(entries_new: int, bytes_new: int, cow_files: int) -> str:
    """The helper's message when the copy of the DATA volume does not match."""
    return (
        f"copy check failed (entries {DATA_ENTRIES} vs {entries_new}, bytes {DATA_BYTES} vs {bytes_new}, "
        f"CoW files {cow_files})"
    )


class OtherDevice:
    """os.lstat() of a mount point: the same folder, on another filesystem."""

    def __init__(self, st: os.stat_result) -> None:
        self.st = st

    @property
    def st_dev(self) -> int:
        return self.st.st_dev + 1

    def __getattr__(self, name: str) -> object:
        return getattr(self.st, name)


class Commands:
    """The helper's commands as the program runs them (their exit status), and its recovery step."""

    def __init__(self, service: DockerVolumesNocow) -> None:
        self.service = service

    # Called directly, not through the cli: an OSError reaches the test (the cli would log it).
    def status(self) -> int:
        self.service.status()
        return 0

    def mark(self) -> int:
        return self.service.mark()

    def migrate(self) -> int:
        self.service.migrate()
        return 0

    def unmark(self) -> int:
        self.service.unmark()
        return 0

    def recover(self) -> None:
        self.service.recover()


class Base(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-nocow-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.vols = os.path.join(self.tmp, "var/lib/docker/volumes")
        self.flag = os.path.join(self.tmp, "etc/btrfs-setup/docker-nocow-pending")
        self.attempts = os.path.join(self.tmp, "var/lib/docker-volumes-nocow/attempts")
        self.postponed = os.path.join(self.tmp, "var/lib/docker-volumes-nocow/postponed")
        self.outcome = os.path.join(self.tmp, "var/lib/docker-volumes-nocow/outcome")
        self.notify = os.path.join(self.tmp, "usr/local/sbin/btrfs-notify")
        # The processes the helper looks at: none, unless a test adds one.
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        os.makedirs(self.vols)
        os.makedirs(os.path.dirname(self.flag))
        # Docker keeps its volume database next to the volumes: a file, not a volume.
        with open(os.path.join(self.vols, "metadata.db"), "wb") as f:
            f.write(b"bolt")
        self.fs = FakeSystem(self.tmp)
        self.fs.notify = self.notify
        self.paths = NocowPaths(
            volumes=self.vols,
            flag=self.flag,
            attempts=self.attempts,
            postponed=self.postponed,
            outcome=self.outcome,
            notify=self.notify,
            alerts=os.path.join(self.tmp, "alerts", "pending"),
            proc=self.proc,
        )
        self.cli = Commands(nocow_wiring.build_nocow(self.paths, runner=self.fs))
        self.enterContext(mock.patch.object(core_nocow, "fcntl", SimpleNamespace(ioctl=self.fs.ioctl)))
        self.enterContext(mock.patch.object(os, "sync", self.fs.sync))
        self.enterContext(mock.patch.object(os, "getxattr", self.fs.getxattr))
        # Nothing of the helper may point outside the test folder.
        paths = self.cli.service.paths
        for path in (
            paths.volumes,
            paths.flag,
            paths.attempts,
            paths.postponed,
            paths.outcome,
            paths.notify,
            paths.alerts,
            paths.proc,
        ):
            self.assertTrue(path.startswith(self.tmp + os.sep), path)

    # ---- set-up helpers ---------------------------------------------------

    def fill(self, top: str, nocow: bool = False, data: dict[str, bytes] | None = None) -> str:
        """Create folder top with DATA (or data); nocow: every folder and file has +C."""
        os.makedirs(top)
        os.chmod(top, 0o750)
        self.fs.set(top, nocow)
        for rel, content in (DATA if data is None else data).items():
            p = os.path.join(top, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(content)
            os.chmod(p, 0o640)
        if data is None:
            for rel, target in LINKS.items():
                os.symlink(target, os.path.join(top, rel))
            for rel in FIFOS:
                os.mkfifo(os.path.join(top, rel))
            for rel in EMPTY_DIRS:
                os.makedirs(os.path.join(top, rel))
        for p, st in entries(top):
            if carries_flags(st):
                self.fs.set(p, nocow)
        return top

    def volume(self, name: str, nocow: bool = False, data: dict[str, bytes] | None = None) -> str:
        """VOLUMES/NAME/_data, filled by fill(); returns the _data path."""
        os.mkdir(os.path.join(self.vols, name))
        self.fs.set(os.path.join(self.vols, name), nocow)
        return self.fill(os.path.join(self.vols, name, "_data"), nocow, data)

    def set_attempts(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.attempts), exist_ok=True)
        with open(self.attempts, "w") as f:
            f.write(text)

    def call(self, fn: Callable[[], int | None]) -> tuple[int | None, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fn()
        return rc, out.getvalue()

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", [TOOL, *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = nocow_cli.nocow_cli(service=self.cli.service)
        return rc, out.getvalue(), err.getvalue()

    # ---- checks -----------------------------------------------------------

    def assert_all_nocow(self, top: str) -> None:
        self.assertTrue(self.fs.nocow(top), top)
        for p, st in entries(top):
            if carries_flags(st):
                self.assertTrue(self.fs.nocow(p), p)

    def assert_no_leftovers(self, name: str) -> None:
        self.assertEqual(sorted(os.listdir(os.path.join(self.vols, name))), ["_data"])


def fresh(test: Base) -> None:
    """A new test folder and new fakes, for the next subTest."""
    test.doCleanups()
    test.setUp()


def read_attempts(test: Base) -> str | None:
    try:
        with open(test.attempts) as f:
            return f.read()
    except FileNotFoundError:
        return None


def schedule(test: Base) -> None:
    """What `cst btrfs install` leaves when mark finds CoW data."""
    open(test.flag, "a").close()


class ImportTest(unittest.TestCase):
    def trapped(self, opened: list[tuple[str, str]]) -> contextlib.ExitStack:
        """Every call that changes the machine or runs a command fails; open() is recorded and finds nothing."""

        def fake_open(path: str, mode: str = "r", *a: object, **kw: object) -> io.StringIO:
            opened.append((str(path), mode))
            raise FileNotFoundError(2, "No such file or directory", path)

        def trap(name: str) -> Callable[..., object]:
            def refuse(*a: object, **kw: object) -> object:
                raise AssertionError(f"loading the module called {name}")

            return refuse

        # Everything the helper uses to change the machine or run a command.
        changes = (
            (os, ("open", "mkdir", "makedirs", "remove", "unlink", "rename", "rmdir", "chown", "chmod", "sync")),
            (shutil, ("rmtree",)),
            (subprocess, ("run", "Popen")),
            (fcntl, ("ioctl",)),
        )
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(builtins, "open", fake_open))
        for module, names in changes:
            for name in names:
                stack.enter_context(mock.patch.object(module, name, trap(f"{module.__name__}.{name}")))
        return stack

    def test_import_reads_and_changes_nothing(self) -> None:
        opened: list[tuple[str, str]] = []
        with self.trapped(opened):
            load_program_fresh()
        self.assertEqual(opened, [])

    def test_the_paths_read_only_daemon_json(self) -> None:
        opened: list[tuple[str, str]] = []
        with self.trapped(opened):
            paths = nocow_wiring.nocow_paths()
        self.assertEqual(opened, [("/etc/docker/daemon.json", "r")])
        self.assertEqual(paths.volumes, "/var/lib/docker/volumes")
        self.assertEqual(paths.flag, "/etc/btrfs-setup/docker-nocow-pending")
        self.assertEqual(paths.attempts, "/var/lib/docker-volumes-nocow/attempts")
        self.assertEqual(paths.postponed, "/var/lib/docker-volumes-nocow/postponed")
        self.assertEqual(paths.outcome, "/var/lib/docker-volumes-nocow/outcome")
        self.assertEqual(paths.proc, "/proc")
        self.assertEqual(paths, NocowPaths())

    def test_docker_root_reads_data_root(self) -> None:
        dj = "/etc/docker/daemon.json"
        default = "/var/lib/docker/volumes"
        for text, want in (
            ('{"data-root": "/srv/docker", "cgroup-parent": "builds.slice"}', ("/srv/docker/volumes", "")),
            ('{"cgroup-parent": "builds.slice"}', (default, "")),
            # Not JSON, not an object, or not a path: dockerd refuses the file, so which folder
            # Docker uses is unknown (these read as its default: the migration ran there).
            ('{"data-root": ""}', (default, f"{dj}: data-root is not a folder name")),
            ("not json {", (default, f"{dj}: not JSON")),
            ('["data-root"]', (default, f"{dj}: not a JSON object")),
            ('{"data-root": 5}', (default, f"{dj}: data-root is not a folder name")),
        ):
            with self.subTest(text=text):

                def fake_open(path: str, mode: str = "r", *a: object, text: str = text, **kw: object) -> io.StringIO:
                    self.assertEqual(path, "/etc/docker/daemon.json")
                    return io.StringIO(text)

                with mock.patch.object(builtins, "open", fake_open):
                    self.assertEqual(nocow_adapters.docker_root(), want)

    def test_a_daemon_json_that_is_not_utf8_is_not_json(self) -> None:
        # A byte that is not UTF-8 fails the read itself (UnicodeDecodeError, a ValueError): the
        # file is not JSON, as for any other text dockerd cannot parse, never a crash.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daemon.json")
            with open(path, "wb") as f:
                f.write(b'{"data-root": "/srv/\xff"}')
            self.assertEqual(nocow_adapters.daemon_config(path), (None, f"{path}: not JSON"))
            with self.assertRaisesRegex(OSError, f"^{re.escape(path)}: not JSON$"):
                nocow_adapters.configured_root(path)

    def test_a_byte_order_mark_is_skipped_as_dockerd_skips_it(self) -> None:
        # An editor may save daemon.json with a UTF-8 byte order mark; dockerd reads such a file,
        # so its data-root counts, and the file is no problem.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daemon.json")
            with open(path, "wb") as f:
                f.write(b'\xef\xbb\xbf{"data-root": "/srv/docker"}')
            self.assertEqual(nocow_adapters.daemon_config(path), ("/srv/docker", ""))
            self.assertEqual(nocow_adapters.configured_root(path), "/srv/docker")


class StatusTest(Base):
    def test_volumes_folder_absent(self) -> None:
        shutil.rmtree(self.vols)
        rc, out = self.call(self.cli.status)
        self.assertEqual((rc, out), (0, f"{self.vols}: absent\n"))
        self.assertEqual(self.fs.calls, [])

    def test_counts_and_changes_nothing(self) -> None:
        self.fs.set(self.vols, True)
        self.volume("done", nocow=True)
        self.volume("cow")
        # Data files all +C, but one folder without it: new files there are CoW.
        mixed = self.volume("mixed", nocow=True)
        self.fs.set(os.path.join(mixed, "db/wal"), False)
        before = dict(self.fs.attrs)
        rc, out = self.call(self.cli.status)
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            f"{self.vols}: new volumes NOCOW; 1 of 3 volume(s) fully NOCOW\n"
            "  1 with copy-on-write data (2 MiB), migration not scheduled\n"
            "  no boot asks for it: run cst btrfs install\n",
        )
        schedule(self)
        rc, out = self.call(self.cli.status)
        self.assertIn("  1 with copy-on-write data (2 MiB), migration at the next boot\n", out)
        # status is read only: no flag set, no command run.
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(self.fs.attrs, before)
        self.assertEqual(self.fs.calls, [])

    def test_volumes_folder_without_nocow(self) -> None:
        self.volume("done", nocow=True)
        _rc, out = self.call(self.cli.status)
        self.assertEqual(out, f"{self.vols}: new volumes copy-on-write; 1 of 1 volume(s) fully NOCOW\n")


class NotVolumeDataTest(Base):
    """What the helper must neither count nor change: a _data that is a
    symlink, and a filesystem mounted below a volume."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.volume("done", nocow=True)
        self.all_done = f"{self.vols}: new volumes NOCOW; 1 of 1 volume(s) fully NOCOW\n"

    def test_symlinked_data_is_not_a_volume(self) -> None:
        elsewhere = self.fill(os.path.join(self.tmp, "elsewhere"))  # copy-on-write data
        os.mkdir(os.path.join(self.vols, "linked"))
        os.symlink(elsewhere, os.path.join(self.vols, "linked", "_data"))
        before = tree(elsewhere)
        self.assertEqual(self.call(self.cli.status), (0, self.all_done))
        schedule(self)
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<5>{TAG}: rewriting 0 volume(s)", out)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertEqual(self.fs.sets_below(elsewhere), [])
        self.assertEqual(tree(elsewhere), before)
        self.assertTrue(os.path.islink(os.path.join(self.vols, "linked", "_data")))

    def test_mount_below_a_volume_is_not_entered(self) -> None:
        mnt = self.fill(os.path.join(self.vols, "done", "_data", "mnt"))  # copy-on-write data
        before = tree(mnt)
        # Seen as part of the volume, the folder holds copy-on-write data.
        _rc, out = self.call(self.cli.status)
        self.assertIn("0 of 1 volume(s) fully NOCOW\n  1 with copy-on-write data (2 MiB)", out)
        real_lstat = os.lstat

        def lstat(path: str) -> object:
            st = real_lstat(path)
            return OtherDevice(st) if path == mnt else st

        with mock.patch.object(os, "lstat", lstat):
            self.assertEqual(self.call(self.cli.status), (0, self.all_done))
            self.assertEqual(self.call(self.cli.mark), (0, ""))
        self.assertEqual(self.fs.sets_below(mnt), [])
        self.assertEqual(tree(mnt), before)


class MarkUnmarkTest(Base):
    def test_mark_absent(self) -> None:
        shutil.rmtree(self.vols)
        rc, out = self.call(self.cli.mark)
        self.assertEqual((rc, out), (0, f"{self.vols}: absent (Docker not set up yet): nothing to mark\n"))
        self.assertEqual(self.fs.sets, [])

    def test_mark_fixes_what_it_can_in_place_and_reports_the_rest(self) -> None:
        self.fs.set(self.vols, False, other=OTHER)
        top = self.volume("cow")
        self.set_attempts("2")
        rc, out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        self.assertEqual(
            out,
            "1 existing volume(s) hold copy-on-write data (2 MiB): rewritten at the next boot, before Docker starts\n",
        )
        self.assertEqual(self.fs.flags_of(self.vols), NOCOW | OTHER)  # the other attribute kept
        # In place: the folders and the empty file. Files with data need the copy.
        for rel in ("", "db", "db/base", "db/wal", "cache", "db/base/empty"):
            self.assertTrue(self.fs.nocow(os.path.join(top, rel)), rel)
        for rel in ("db/base/1", "db/wal/000001", "README"):
            self.assertFalse(self.fs.nocow(os.path.join(top, rel)), rel)
        self.assertEqual(read_attempts(self), "0")  # a new request gets its two boots again, durably
        self.assertTrue(os.path.exists(self.flag))  # written by mark itself, before +C
        self.assertEqual(self.fs.calls, [])

    def test_mark_with_nothing_to_copy(self) -> None:
        self.volume("done", nocow=True)
        self.volume("empty", data={"a/b": b""})
        self.set_attempts("1")
        rc, out = self.call(self.cli.mark)
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.fs.nocow(self.vols))
        self.assert_all_nocow(os.path.join(self.vols, "empty/_data"))
        self.assertIsNone(read_attempts(self))
        self.assertFalse(os.path.exists(self.flag))

    def test_mark_with_nothing_to_copy_whose_old_count_cannot_go_sets_no_nocow(self) -> None:
        # forget swallowed the error: mark set +C and exited 0 beside the old count. Now the error is
        # mark's (a journal line, exit 2) before +C; the next boot sets +C (no data: no request).
        self.fs.set(self.vols, nocow=False)
        self.volume("empty", data={"a/b": b""})
        os.makedirs(self.attempts)
        rc, out, _err = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertEqual(out, f"<3>{TAG}: {self.vols}: [Errno 21] Is a directory: '{self.attempts}'\n")
        self.assertEqual(self.fs.sets, [])
        self.assertFalse(self.fs.nocow(self.vols))
        self.assertTrue(os.path.isdir(self.attempts))
        self.assertFalse(os.path.exists(self.flag))

    def test_mark_writes_the_flag_durably_before_nocow_is_set(self) -> None:
        # The install's request, as the boot's: the flag, its entry in its folder and the folder's
        # entry in /etc are flushed before +C is set on the volumes folder. A kill or a power loss
        # between the two must not leave +C, copy-on-write data and no flag: no boot would ask again.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        synced: list[tuple[str, list[str]]] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            synced.append((os.readlink(f"/proc/self/fd/{fd}"), list(self.fs.sets)))
            real_fsync(fd)

        with mock.patch.object(os, "fsync", fsync):
            rc, _out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        folder = os.path.dirname(self.flag)
        self.assertEqual(synced, [(self.flag, []), (folder, []), (os.path.dirname(folder), [])])
        self.assertEqual(self.fs.sets[0], self.vols)  # then +C, first on the volumes folder
        self.assertTrue(self.fs.nocow(self.vols))

    def test_mark_that_cannot_write_the_flag_leaves_the_folder_copy_on_write(self) -> None:
        # The flag's folder is a file: no request, so no +C either; the next boot sees the
        # folder without +C and asks again.
        shutil.rmtree(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.set(self.vols, nocow=False)
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(self.cli.mark)
        self.assertEqual(rc, 2)
        self.assertRegex(
            out,
            rf"^<3>{TAG}: cannot schedule the migration \(\[Errno 17\] File exists: '[^']*'\); "
            rf"{self.vols} keeps copy-on-write, the next boot tries again\n$",
        )
        self.assertEqual(self.fs.sets, [])
        self.assertFalse(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assertIsNone(read_attempts(self))
        self.assertFalse(os.path.exists(self.outcome))  # the next boot asks: nothing to record

    def test_mark_that_cannot_reset_the_count_makes_no_request(self) -> None:
        # The reset of an old count is part of the request: when it fails, no flag and no +C.
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        self.set_attempts("1")
        with mock.patch.object(
            self.cli.service.state, "save_attempts", side_effect=OSError(errno.EIO, os.strerror(errno.EIO))
        ):
            rc, out = self.call(self.cli.mark)
        self.assertEqual(rc, 2)
        self.assertIn("cannot schedule the migration ([Errno 5] Input/output error)", out)
        self.assertFalse(os.path.exists(self.flag))
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(read_attempts(self), "1")

    def test_mark_on_a_folder_with_nocow_already_that_cannot_reset_the_count_says_no_boot_asks(self) -> None:
        # The old state (an older install stopped between +C and its flag: +C, copy-on-write data, no
        # flag) and a count of 2 mark cannot reset: no boot asks again (a boot asks only for a folder
        # without +C). The line said "the next boot tries again"; it tells the truth now, and the
        # outcome record keeps it for status and cst doctor.
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        self.set_attempts("2")
        with (
            mock.patch.object(
                nocow_adapters.NocowStateFiles, "save_attempts", side_effect=OSError(errno.EIO, "Input/output error")
            ),
            mock.patch.object(time, "time", return_value=1_700_000_000.0),
        ):
            rc, out, _err = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertEqual(
            out,
            f"<3>{TAG}: cannot schedule the migration ([Errno 5] Input/output error); {self.vols} has +C already, "
            "so no boot asks again: fix that, then run cst btrfs install\n",
        )
        self.assertFalse(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "2")
        with open(self.outcome) as f:
            self.assertEqual(
                object_of(parse_json(f.read())),
                {
                    "v": 1,
                    "outcome": "not-scheduled",
                    "at": 1_700_000_000,
                    "volumes": self.vols,
                    "reason": "cannot schedule the migration: [Errno 5] Input/output error",
                },
            )
        self.assertEqual(stat.S_IMODE(os.stat(self.outcome).st_mode), 0o644)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(1_700_000_000))
        _rc, status = self.call(self.cli.status)
        self.assertEqual(
            status,
            f"{self.vols}: new volumes NOCOW; 0 of 1 volume(s) fully NOCOW\n"
            "  1 with copy-on-write data (2 MiB), migration not scheduled\n"
            f"  migration not scheduled by the install at {when} (cannot schedule the migration: [Errno 5] "
            "Input/output error); no boot asks for it: fix that, then run cst btrfs install\n",
        )
        # Fixed: the next install writes the request and removes the record.
        rc, _out, _err = self.main("mark")
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.flag))
        self.assertFalse(os.path.exists(self.outcome))

    def test_mark_beside_an_earlier_flag_that_cannot_reset_the_count_says_the_next_boot_tries(self) -> None:
        # A boot's copy failed (a count of 1, the flag kept) on a folder with +C, and the install's
        # reset of that count fails: the flag is still there, so the next boot runs the migration.
        # The line said no boot asks again, and a not-scheduled record was left beside the flag.
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        schedule(self)
        self.set_attempts("1")
        with mock.patch.object(
            nocow_adapters.NocowStateFiles, "save_attempts", side_effect=OSError(errno.EIO, "Input/output error")
        ):
            rc, out, _err = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertEqual(
            out,
            f"<3>{TAG}: cannot schedule the migration ([Errno 5] Input/output error); {self.vols} keeps "
            "copy-on-write, the next boot tries again\n",
        )
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "1")
        self.assertFalse(os.path.exists(self.outcome))

    def test_mark_on_a_folder_with_nocow_already_that_cannot_write_the_flag_says_no_boot_asks(self) -> None:
        shutil.rmtree(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        rc, out, _err = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertRegex(
            out,
            rf"^<3>{TAG}: cannot schedule the migration \(\[Errno 17\] File exists: '[^']*'\); "
            rf"{self.vols} has \+C already, so no boot asks again: fix that, then run cst btrfs install\n$",
        )
        with open(self.outcome) as f:
            self.assertEqual(object_of(parse_json(f.read()))["outcome"], "not-scheduled")

    def test_mark_whose_request_after_its_own_nocow_fails_says_no_boot_asks(self) -> None:
        # The second scan finds data the first did not (a container wrote between them): the request
        # comes after mark's own +C, so when it fails no boot asks either.
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        service = self.cli.service
        real_scan = service.tree.scan
        first: list[bool] = []

        def scan(top: str, *, fix: bool = False) -> TreeScan:
            if not fix and not first:
                first.append(True)
                return TreeScan()
            return real_scan(top, fix=fix)

        denied = PermissionError(errno.EACCES, "Permission denied")
        with (
            mock.patch.object(service.tree, "scan", scan),
            mock.patch.object(nocow_adapters.NocowStateFiles, "request_migration", side_effect=denied),
        ):
            rc, out = self.call(lambda: service.execute(NocowCommand.MARK))
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.vols} has +C already, so no boot asks again", out)
        self.assertTrue(self.fs.nocow(self.vols))
        with open(self.outcome) as f:
            self.assertEqual(object_of(parse_json(f.read()))["outcome"], "not-scheduled")

    def test_mark_with_a_flag_already_there_gives_two_boots_again(self) -> None:
        self.volume("cow")
        schedule(self)
        self.set_attempts("1")
        rc, _out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        self.assertEqual(read_attempts(self), "0")
        self.assertTrue(os.path.exists(self.flag))

    def test_mark_requests_when_the_second_scan_finds_data_the_first_did_not(self) -> None:
        # A container wrote between mark's two scans (install does not stop Docker): the second
        # one finds the data, and mark asks for the migration then, after +C.
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        service = self.cli.service
        real_scan = service.tree.scan
        first: list[bool] = []

        def scan(top: str, *, fix: bool = False) -> TreeScan:
            if not fix and not first:
                first.append(True)
                return TreeScan()  # nothing yet
            return real_scan(top, fix=fix)

        with mock.patch.object(service.tree, "scan", scan):
            rc, out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        self.assertIn("1 existing volume(s) hold copy-on-write data", out)
        self.assertTrue(os.path.exists(self.flag))
        self.assertTrue(self.fs.nocow(self.vols))

    def test_mark_after_an_old_install_left_nocow_without_its_flag(self) -> None:
        # An older install set +C first and the flag after mark: killed in between, it left +C,
        # copy-on-write data and no flag. No boot asks again; the next install does.
        self.fs.set(self.vols, nocow=True)
        top = self.volume("cow")
        _rc, out = self.call(self.cli.status)
        self.assertIn("1 with copy-on-write data (2 MiB), migration not scheduled", out)
        self.assertEqual(self.call(self.cli.migrate), (0, ""))  # a boot: nothing asks
        rc, _out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.flag))
        _rc, out = self.call(self.cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(self.flag))

    def test_unmark_clears_only_the_volumes_folder(self) -> None:
        self.fs.set(self.vols, True, other=OTHER)
        top = self.volume("done", nocow=True)
        rc, out = self.call(self.cli.unmark)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.fs.flags_of(self.vols), OTHER)
        self.assert_all_nocow(top)  # existing volumes stay NOCOW
        self.assertEqual(self.fs.sets, [self.vols])

    def test_unmark_absent(self) -> None:
        shutil.rmtree(self.vols)
        self.assertEqual(self.call(self.cli.unmark), (0, ""))
        self.assertEqual(self.fs.sets, [])


class MainTest(Base):
    def test_usage_and_default(self) -> None:
        rc, out, err = self.main("bogus")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage: docker-volumes-nocow status|mark|migrate|unmark", err)
        rc, out, err = self.main()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(f"{self.vols}: new volumes copy-on-write;"), out)

    def test_mark_exit_statuses(self) -> None:
        # 1: a migration is requested, the flag already written by mark; 2: it could not be.
        self.volume("cow")
        rc, out, _ = self.main("mark")
        self.assertEqual(rc, 1)
        self.assertIn("1 existing volume(s) hold copy-on-write data", out)
        self.assertTrue(os.path.exists(self.flag))
        fresh(self)
        shutil.rmtree(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        rc, out, _ = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertIn(f"<3>{TAG}: cannot schedule the migration", out)
        self.assertFalse(self.fs.nocow(self.vols))

    def test_oserror_exit_codes(self) -> None:
        # A data-root that is not on btrfs: the flag ioctls fail.
        def no_flags(fd: int, request: int, arg: bytes | int = 0, mutate_flag: bool = True) -> bytes | int:
            raise OSError(25, "Inappropriate ioctl for device")

        self.volume("cow")
        with mock.patch.object(core_nocow, "fcntl", SimpleNamespace(ioctl=no_flags)):
            for cmd, want in (("status", 2), ("mark", 2), ("migrate", 0), ("unmark", 0)):
                with self.subTest(cmd=cmd):
                    rc, out, _ = self.main(cmd)
                    self.assertEqual(rc, want)
                    self.assertIn(f"<3>{TAG}: {self.vols}: [Errno 25] Inappropriate ioctl for device\n", out)


class MissingProgramTest(Base):
    """A program the helper runs is missing: it stops as when subprocess.run raised."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        schedule(self)

    def test_btrfs_missing_stops_the_run_with_one_line(self) -> None:
        self.fs.missing.add("btrfs")
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith(f"<3>{TAG}: {self.vols}: [Errno 2] No such file or directory: 'btrfs'\n"), out)
        self.assertEqual(read_attempts(self), "1")  # counted before the rewrite
        self.assertTrue(os.path.exists(self.flag))

    def test_cp_missing_leaves_the_volume_as_it_was(self) -> None:
        self.fs.missing.add("cp")
        before = tree(self.top)
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertIn(f"<3>{TAG}: cow: [Errno 2] No such file or directory: 'cp'; left as it was\n", out)
        self.assertEqual(tree(self.top), before)
        # The empty twin stays, as before; recover() removes it at the next boot.
        self.assertEqual(sorted(os.listdir(os.path.join(self.vols, "cow"))), ["_data", "_data.nocow-new"])

    def test_notify_missing_after_giving_up(self) -> None:
        """Without btrfs-notify the alert is kept in the pending folder (cst doctor shows it), and the journal says so."""
        self.set_attempts("2")
        self.fs.missing.add(self.notify)
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        pending = os.path.join(self.tmp, "alerts", "pending")
        [name] = os.listdir(pending)
        kept = os.path.join(pending, name)
        self.assertEqual(
            out,
            f"<3>{TAG}: Gave up after two boots; see journalctl -u docker-volumes-nocow.\n"
            f"<3>{TAG}: alert not delivered ({self.notify} is not installed): "
            f"Docker volumes: NOCOW migration gave up; kept in {kept} (cst doctor shows it)\n",
        )
        with open(kept) as f:
            text = f.read()
        self.assertTrue(text.startswith("cst-alert 1\nurgency=normal\napp=btrfs-setup\n"), text)
        self.assertIn("\ntitle=Docker volumes: NOCOW migration gave up\n\nGave up after two boots", text)
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))


class LenientMessagesTest(unittest.TestCase):
    """LocalVolumeTree over the real runner and a real program: a cp or chcon message that names a
    file whose name is not UTF-8 is a message, not a UnicodeDecodeError."""

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-nocow-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        # A Python script, not sh: stdin is inherited, and bash with a socket there reads ~/.bashrc.
        for name in ("cp", "chcon"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(
                    "#!/usr/bin/python3 -I\nimport sys\n"
                    f"sys.stderr.buffer.write(b\"{name}: cannot create '\\xff': No space left on device\\n\")\n"
                    "sys.exit(1)\n"
                )
            os.chmod(path, 0o755)
        runner = SubprocessCommandRunner(base_env={"PATH": self.bin})
        self.tree = nocow_adapters.LocalVolumeTree(NocowPaths(volumes=self.tmp), runner)
        self.data, self.new = os.path.join(self.tmp, "_data"), os.path.join(self.tmp, "_data.nocow-new")
        os.mkdir(self.data)
        os.mkdir(self.new)

    def test_a_cp_message_with_bytes_that_are_not_utf8(self) -> None:
        r = self.tree.copy(self.data, self.new)
        self.assertEqual((r.returncode, r.stderr), (1, "cp: cannot create '\ufffd': No space left on device\n"))

    def test_a_chcon_message_with_bytes_that_are_not_utf8(self) -> None:
        with (
            mock.patch.object(nocow_adapters, "label", return_value=b"system_u:object_r:container_file_t:s0"),
            self.assertRaises(RuntimeError) as cm,
        ):
            self.tree.reference_label(self.data, self.new)
        self.assertEqual(
            str(cm.exception), "SELinux label not copied: chcon: cannot create '\ufffd': No space left on device"
        )
        self.assertFalse(os.path.exists(self.new))


class MigrateTest(Base):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)  # what install's mark left

    def test_happy_path(self) -> None:
        done = self.volume("done", nocow=True)
        done_ino = ino(done)
        top = self.volume("cow")
        before, old_ino = tree(top), ino(top)
        schedule(self)
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(top), before)  # same entries, modes, data and link targets
        self.assertNotEqual(ino(top), old_ino)  # a new copy
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertEqual(ino(done), done_ino)  # a NOCOW volume is not copied
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))
        new = top + ".nocow-new"
        self.assertEqual(self.fs.commands("chcon"), [["chcon", "--reference", top, new]])
        self.assertEqual(
            self.fs.commands("cp"), [["cp", "-a", "--reflink=never", "--sparse=auto", "--", top + "/.", new + "/"]]
        )
        self.assertEqual(self.fs.commands("btrfs"), [["btrfs", "filesystem", "usage", "-b", self.vols]])
        self.assertEqual(self.fs.syncs, 1)  # before the names are swapped (RecoverTest checks the order)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"<5>{TAG}: rewriting 1 volume(s) without copy-on-write before Docker starts")
        self.assertRegex(lines[1], rf"^<6>{TAG}: cow: 3 file\(s\), 2 MiB in \d+s$")
        self.assertEqual(lines[2], f"<5>{TAG}: all Docker volumes are NOCOW now")
        self.assertEqual(len(lines), 3)

    def test_volumes_folder_absent(self) -> None:
        shutil.rmtree(self.vols)
        schedule(self)
        self.assertEqual(self.call(self.cli.migrate), (0, ""))
        self.assertEqual(self.fs.calls, [])
        self.assertTrue(os.path.exists(self.flag))

    def test_not_scheduled_copies_nothing(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(self.cli.migrate)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(tree(top), before)
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        self.assertEqual([c[0] for c in self.fs.calls], ["systemctl"])
        self.assertIsNone(read_attempts(self))

    def test_volumes_folder_lost_its_flag_schedules_and_migrates_at_once(self) -> None:
        # Docker installed after `cst btrfs install`: its volumes folder has no +C.
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertTrue(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertFalse(os.path.exists(self.flag))  # created, then removed once done
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)

    def test_flag_folder_missing_is_created_and_the_volumes_migrated(self) -> None:
        # The same case as above on a machine where /etc/btrfs-setup does not
        # exist (install only creates it when mark found CoW data, or the
        # snapshots setup did). The helper creates the folder: before the fix,
        # open(FLAG) failed after +C was already set on the volumes folder, so
        # no later boot scheduled the migration again.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assertNotIn(f"<3>{TAG}", out)
        self.assertTrue(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertTrue(os.path.isdir(os.path.dirname(self.flag)))
        self.assertFalse(os.path.exists(self.flag))  # created, then removed once done
        rc, out, _ = self.main("status")
        self.assertNotIn("with copy-on-write data", out)

    def test_flag_is_durable_before_nocow_is_set(self) -> None:
        # The flag, its entry in its folder and the folder's entry in /etc are
        # flushed before +C is set: after a power loss, +C on the volumes
        # folder must not stay while the flag is lost.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        self.volume("cow")
        synced: list[tuple[str, list[str]]] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            synced.append((os.readlink(f"/proc/self/fd/{fd}"), list(self.fs.sets)))
            real_fsync(fd)

        with mock.patch.object(os, "fsync", fsync):
            _rc, out = self.call(self.cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        folder = os.path.dirname(self.flag)
        self.assertEqual(synced[:3], [(self.flag, []), (folder, []), (os.path.dirname(folder), [])])
        # Then, and only then, the attempts counter (write_atomic): its temporary file, then its folder.
        counter = os.path.dirname(self.attempts)
        self.assertEqual(len(synced), 5, synced)
        self.assertTrue(synced[3][0].startswith(os.path.join(counter, ".attempts.")), synced[3])
        self.assertEqual(synced[4][0], counter)

    def test_flag_not_durable_leaves_the_folder_copy_on_write(self) -> None:
        # fsync fails (an I/O error): treated like a flag that cannot be written.
        self.fs.set(self.vols, False)
        top = self.volume("cow")

        def fsync(fd: int) -> None:
            raise OSError(5, "Input/output error")

        with mock.patch.object(os, "fsync", fsync):
            _rc, out = self.call(self.cli.migrate)
        self.assertEqual(
            out,
            f"<3>{TAG}: cannot schedule the migration ([Errno 5] Input/output error); "
            f"{self.vols} keeps copy-on-write, the next boot tries again\n",
        )
        self.assertEqual(self.fs.sets, [])
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))

    def test_flag_not_written_leaves_the_folder_copy_on_write(self) -> None:
        # The flag cannot be written (here its folder is a file): nothing is
        # set, so the volumes folder still has no +C and the next boot sees
        # the request again.
        shutil.rmtree(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertRegex(
            out,
            rf"^<3>{TAG}: cannot schedule the migration \(\[Errno 17\] File exists: '[^']*'\); "
            rf"{self.vols} keeps copy-on-write, the next boot tries again\n$",
        )
        self.assertEqual(self.fs.sets, [])  # no +C anywhere, the volumes folder included
        self.assertFalse(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertIsNone(read_attempts(self))
        # The obstacle goes: the next boot schedules and migrates.
        os.remove(os.path.dirname(self.flag))
        rc, out, _ = self.main("migrate")
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(top)
        self.assertTrue(self.fs.nocow(self.vols))

    def test_folder_without_nocow_and_no_cow_data_needs_no_flag(self) -> None:
        # Only folders and empty files: fixed in place, nothing scheduled, and
        # no flag folder is created for it.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        top = self.volume("empty", data={"a/b": b""})
        rc, out = self.call(self.cli.migrate)
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.fs.nocow(self.vols))
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(os.path.dirname(self.flag)))

    def test_scheduled_with_nothing_left_to_copy(self) -> None:
        top = self.volume("empty", data={"a/b": b""})
        schedule(self)
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assert_all_nocow(top)  # fixed in place
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))
        self.assertIn(f"<5>{TAG}: rewriting 0 volume(s)", out)

    def migrate_refused(
        self, cp: Callable[[str, str], tuple[int, str]], why: str, make: Callable[[], str] | None = None
    ) -> None:
        """Migrate the CoW volume "cow" (made by make, if given) with this cp:
        the copy is refused, and the original stays as it was, to be tried
        again at the next boot."""
        top = make() if make else self.volume("cow")
        before, old_ino = tree(top), ino(top)
        schedule(self)
        self.fs.cp = cp
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(top), before)
        self.assertEqual(ino(top), old_ino)  # the original, untouched
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        self.assert_no_leftovers("cow")
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "1")
        self.assertIn(f"<3>{TAG}: cow: {why}; left as it was\n", out)
        self.assertNotIn("all Docker volumes are NOCOW now", out)
        self.assertEqual(self.fs.syncs, 0)

    def test_copy_check_refuses(self) -> None:
        for name, change, why in (
            (
                "drops a file",
                lambda dst: os.remove(os.path.join(dst, "README")),
                check_failed(DATA_ENTRIES - 1, DATA_BYTES - 13, 0),
            ),
            (
                "truncates a file",
                lambda dst: os.truncate(os.path.join(dst, "db/base/1"), 1000),
                check_failed(DATA_ENTRIES, DATA_BYTES - (1 << 20) + 1000, 0),
            ),
            (
                "leaves a CoW file",
                lambda dst: self.fs.set(os.path.join(dst, "db/wal/000001"), False),
                check_failed(DATA_ENTRIES, DATA_BYTES, 1),
            ),
            (
                "leaves a CoW folder",
                lambda dst: self.fs.set(os.path.join(dst, "cache"), False),
                check_failed(DATA_ENTRIES, DATA_BYTES, 0),
            ),
        ):
            with self.subTest(cp=name):
                fresh(self)
                self.migrate_refused(self.fs.copy_then(change), why)

    def test_copy_fails(self) -> None:
        def full_disk(src: str, dst: str) -> tuple[int, str]:
            with open(os.path.join(dst, "partial"), "wb") as f:
                f.write(b"x")
            return 1, "cp: error writing 'db/base/1': No space left on device\n"

        self.migrate_refused(full_disk, "copy failed: cp: error writing 'db/base/1': No space left on device")

    # ---- SELinux labels -----------------------------------------------------
    # Tier 2 cannot show real labels (CI's kernel has no SELinux); these use
    # the fake getxattr, which cp -a and chcon --reference copy across.
    CONTAINER = b"system_u:object_r:container_file_t:s0:c12,c34"

    def labelled_volume(self) -> str:
        top = self.volume("cow")
        self.fs.label(top, self.CONTAINER + b"\0")  # as the kernel returns it
        for p, _ in entries(top):
            self.fs.label(p, self.CONTAINER)
        return top

    def test_labels_kept(self) -> None:
        top = self.labelled_volume()
        schedule(self)
        _rc, out = self.call(self.cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assertEqual(self.fs.label_of(top), self.CONTAINER + b"\0")
        for p, _ in entries(top):
            self.assertEqual(self.fs.label_of(p), self.CONTAINER, p)

    def test_copy_check_refuses_a_lost_or_changed_label(self) -> None:
        other = b"system_u:object_r:var_lib_t:s0"
        old = self.CONTAINER.decode()
        for name, change, why in (
            (
                "drops a file's label",
                lambda dst: self.fs.label(os.path.join(dst, "db/base/1"), None),
                f"db/base/1: {old} vs none",
            ),
            (
                "changes a folder's label",
                lambda dst: self.fs.label(os.path.join(dst, "db/wal"), other),
                f"db/wal: {old} vs {other.decode()}",
            ),
            (
                "changes a link's own label",
                lambda dst: self.fs.label(os.path.join(dst, "latest"), other),
                f"latest: {old} vs {other.decode()}",
            ),
            ("loses the top label", lambda dst: self.fs.label(dst, None), f".: {old} vs none"),
        ):
            with self.subTest(cp=name):
                fresh(self)
                self.migrate_refused(
                    self.fs.copy_then(change), f"copy check failed (SELinux label of {why})", self.labelled_volume
                )

    def test_a_failed_chcon_refuses_the_copy_when_labels_are_kept(self) -> None:
        # chcon --reference labels the empty copy first; cp -a only fixes the
        # top folder, so a failed chcon is refused before anything is copied.
        self.fs.chcon_rc = 1
        new = os.path.join(self.vols, "cow", "_data.nocow-new")
        why = f"SELinux label not copied: chcon: failed to change context of '{new}': Operation not permitted"
        self.migrate_refused(self.fs.copy, why, self.labelled_volume)
        self.assertEqual(self.fs.commands("cp"), [])

    def test_a_failed_chcon_is_ignored_without_labels(self) -> None:
        # SELinux disabled: no labels to copy, chcon cannot work, nothing changes.
        top = self.volume("cow")
        self.fs.chcon_rc = 1
        schedule(self)
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(top)
        self.assertEqual(len(self.fs.commands("chcon")), 1)

    def test_labels_equal_up_to_the_trailing_nul(self) -> None:
        top = self.labelled_volume()
        schedule(self)
        # The copy's README reads back with a NUL, the original's without.
        self.fs.cp = self.fs.copy_then(lambda dst: self.fs.label(os.path.join(dst, "README"), self.CONTAINER + b"\0"))
        _rc, out = self.call(self.cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(top)

    def test_label_unsupported_or_unreadable(self) -> None:
        top = self.volume("cow")
        for err, want in ((95, None), (61, None)):  # EOPNOTSUPP, ENODATA: no label
            with self.subTest(errno=err):

                def getxattr(path: str, attribute: str, *, follow_symlinks: bool = True, err: int = err) -> bytes:
                    raise OSError(err, os.strerror(err), path)

                with mock.patch.object(os, "getxattr", getxattr):
                    self.assertEqual(nocow_adapters.label(top), want)
                    self.assertEqual(set(self.cli.service.tree.labels(top).values()), {None})

        def denied(path: str, attribute: str, *, follow_symlinks: bool = True) -> bytes:
            raise PermissionError(13, "Permission denied", path)

        with mock.patch.object(os, "getxattr", denied), self.assertRaises(PermissionError):
            nocow_adapters.label(top)

    def test_one_failing_volume_does_not_stop_the_others(self) -> None:
        bad = self.volume("a-bad")
        good = self.volume("b-good")
        bad_before, good_before = tree(bad), tree(good)
        schedule(self)

        def fails_for_a(src: str, dst: str) -> tuple[int, str]:
            return cp_fails(src, dst) if "/a-bad/" in src else self.fs.copy(src, dst)

        self.fs.cp = fails_for_a
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(bad), bad_before)
        self.assertFalse(self.fs.nocow(os.path.join(bad, "README")))
        self.assertEqual(tree(good), good_before)
        self.assert_all_nocow(good)
        self.assert_no_leftovers("a-bad")
        self.assert_no_leftovers("b-good")
        self.assertIn(f"<3>{TAG}: a-bad: copy failed: cp: boom; left as it was\n", out)
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "1")

    def test_free_space(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        schedule(self)
        # One byte short of the copy plus the 20 GiB margin: skipped.
        self.fs.free = DATA_BYTES + 20 * GIB - 1
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<4>{TAG}: cow: not enough free space for a 2 MiB copy; skipped\n", out)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertEqual(tree(top), before)
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "1")
        # Exactly enough: copied.
        self.fs.free = DATA_BYTES + 20 * GIB
        rc, out = self.call(self.cli.migrate)
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(self.flag))

    def test_free_space_unknown_is_a_skip(self) -> None:
        self.volume("cow")
        schedule(self)
        self.fs.btrfs_out = ""
        _rc, out = self.call(self.cli.migrate)
        self.assertIn(f"<4>{TAG}: cow: not enough free space for a 2 MiB copy; skipped\n", out)
        self.assertEqual(self.fs.commands("cp"), [])

    def test_gives_up_after_two_boots(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        schedule(self)
        self.fs.cp = cp_fails
        msg = "Gave up after two boots; see journalctl -u docker-volumes-nocow."
        for boot, attempts in ((1, "1"), (2, "2")):
            with self.subTest(boot=boot):
                rc, out = self.call(self.cli.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(read_attempts(self), attempts)
                self.assertTrue(os.path.exists(self.flag))
                self.assertNotIn(msg, out)
        # Third boot: no third try; the flag and the counter go, the user is told.
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"<3>{TAG}: {msg}\n")
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))
        self.assertEqual(len(self.fs.commands("cp")), 2)
        self.assertEqual(
            self.fs.commands(self.notify),
            [[self.notify, "-u", "normal", "-a", "btrfs-setup", "Docker volumes: NOCOW migration gave up", msg]],
        )
        self.assertEqual(tree(top), before)
        # Later boots: nothing scheduled, nothing tried, no second alert.
        self.assertEqual(self.call(self.cli.migrate), (0, ""))
        self.assertEqual(len(self.fs.commands("cp")), 2)
        self.assertEqual(len(self.fs.commands(self.notify)), 1)

    def test_a_flag_that_cannot_be_removed_does_not_start_the_request_over(self) -> None:
        # The third boot gives up, but the flag cannot be removed (/etc read-only): the count of boots must
        # stay with it. Without it the next boot started the request over (two more rewrites before Docker
        # starts, then this again), and the alert was never sent.
        top = self.volume("cow")
        before = tree(top)
        schedule(self)
        self.set_attempts("2")
        self.fs.cp = cp_fails
        erofs = OSError(errno.EROFS, "Read-only file system")
        with mock.patch.object(nocow_adapters.NocowStateFiles, "remove_flag", side_effect=erofs):
            for boot in (3, 4, 5):
                with self.subTest(boot=boot):
                    rc, _out = self.call(lambda: self.cli.service.execute(NocowCommand.MIGRATE))
                    self.assertEqual(rc, 0)  # the boot's run: logged, not failed
                    self.assertEqual(read_attempts(self), "2")
                    self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.fs.commands("cp"), [])  # no try started over
        self.assertEqual(tree(top), before)
        # /etc writable again: the next boot gives up and tells the user.
        self.call(self.cli.migrate)
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))
        self.assertEqual(len(self.fs.commands(self.notify)), 1)
        self.assertEqual(self.fs.commands("cp"), [])

    def test_attempts_counter_is_durable_before_the_rewrite_starts(self) -> None:
        # The counter was written with open("w") and no fsync, then the long rewrite
        # started; a hang or hard reset before the next btrfs commit lost the increment, so
        # "gave up after two boots" could never trip.
        self.volume("cow")
        schedule(self)
        events: list[str] = []
        real_fsync = os.fsync
        folder = os.path.dirname(self.attempts)

        def fsync(fd: int) -> None:
            path = os.readlink(f"/proc/self/fd/{fd}")
            events.append("fsync counter folder" if path == folder else f"fsync {os.path.dirname(path)}")
            real_fsync(fd)

        def cp(src: str, dst: str) -> tuple[int, str]:
            events.append("cp")
            return cp_fails(src, dst)

        self.fs.cp = cp
        with mock.patch.object(os, "fsync", fsync):
            self.call(self.cli.migrate)
        self.assertEqual(read_attempts(self), "1")
        first_cp = events.index("cp")
        self.assertIn(f"fsync {folder}", events[:first_cp], events)  # the counter's own bytes
        self.assertIn("fsync counter folder", events[:first_cp], events)  # its rename
        self.assertEqual(sorted(os.listdir(folder)), ["attempts"], "a temporary file was left")

    def test_mark_gives_two_boots_again(self) -> None:
        self.volume("cow")
        schedule(self)
        self.set_attempts("2")
        self.call(self.cli.mark)
        self.fs.cp = cp_fails
        self.call(self.cli.migrate)
        self.assertEqual(read_attempts(self), "1")  # tried, not given up
        self.assertEqual(len(self.fs.commands("cp")), 1)

    def test_a_count_left_without_its_flag_does_not_count_for_a_new_request(self) -> None:
        # An older helper removed the flag, then the count of boots, and could stop between the two.
        # Docker reinstalled later (the volumes folder without +C): the new request gets its two boots.
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        self.set_attempts("1")
        self.fs.cp = cp_fails
        for boot, attempts in ((1, "1"), (2, "2")):
            with self.subTest(boot=boot):
                rc, out = self.call(self.cli.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(read_attempts(self), attempts)
                self.assertTrue(os.path.exists(self.flag))
                self.assertNotIn("Gave up", out)
        self.assertEqual(len(self.fs.commands("cp")), 2)

    def test_a_count_left_without_its_flag_is_reset_durably_before_the_flag(self) -> None:
        # The flag is fsynced; the reset of the old count must be on disk before it, or a power loss
        # right after could keep the flag and bring the old count back (a give-up with no try).
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        self.set_attempts("2")
        self.fs.cp = cp_fails
        synced_at_flag: list[str] = []
        real_request = nocow_adapters.NocowStateFiles.request_migration

        def request(state: nocow_adapters.NocowStateFiles) -> None:
            synced_at_flag.extend(synced)
            real_request(state)

        with (
            recorded_fsyncs() as synced,
            mock.patch.object(nocow_adapters.NocowStateFiles, "request_migration", request),
        ):
            self.call(self.cli.migrate)
        assert_replaced_durably(self, self.attempts, synced_at_flag)
        self.assertEqual(read_attempts(self), "1")  # this request's first boot

    def test_a_count_that_cannot_be_reset_is_a_request_not_made(self) -> None:
        # The old count cannot be reset: the request is not made (no flag, no +C, a journal line), so the
        # next boot sees the same sign again; it never goes on with the old count.
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        self.set_attempts("2")
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(nocow_adapters.NocowStateFiles, "save_attempts", side_effect=denied):
            rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(
            f"cannot schedule the migration ([Errno 13] Permission denied); {self.vols} keeps copy-on-write, "
            "the next boot tries again\n",
            out,
        )
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(self.fs.nocow(self.vols))
        self.assertEqual(read_attempts(self), "2")
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertFalse(os.path.exists(self.outcome))  # migrate's request: the next boot asks again

    def test_a_request_already_scheduled_keeps_its_count(self) -> None:
        # The flag is there (the request is under way) but the volumes folder has no +C: the count stays.
        self.fs.set(self.vols, nocow=False)
        self.volume("cow")
        schedule(self)
        self.set_attempts("1")
        self.fs.cp = cp_fails
        self.call(self.cli.migrate)
        self.assertEqual(read_attempts(self), "2")

    def test_attempts_file_is_closed(self) -> None:
        # The counter is read and written inside `with open(...)`: no file is
        # left for the garbage collector to close (it warned "unclosed file").
        self.volume("cow")
        schedule(self)
        self.set_attempts("1")
        self.fs.cp = cp_fails
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.call(self.cli.migrate)
            gc.collect()
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])
        self.assertEqual(read_attempts(self), "2")

    def test_unreadable_counter_counts_as_zero(self) -> None:
        self.volume("cow")
        schedule(self)
        self.set_attempts("garbage")
        self.fs.cp = cp_fails
        self.call(self.cli.migrate)
        self.assertEqual(read_attempts(self), "1")


class PostponeBase(Base):
    """A scheduled CoW volume, one attempt made, and a leftover of an
    interrupted copy (recover() would remove it): what a postponed boot must
    leave as it is."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        # A leftover of an interrupted copy: recover() would remove it.
        os.mkdir(self.top + ".nocow-new")
        self.before = tree(os.path.join(self.vols, "cow"))
        schedule(self)
        self.set_attempts("1")

    def assert_migrated(self, out: str) -> None:
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(self.top)
        self.assert_no_leftovers("cow")

    def assert_postponed(self, out: str, why: str) -> None:
        self.assertEqual(out, f"<4>{TAG}: {why}: migration left for the next boot\n")
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(tree(os.path.join(self.vols, "cow")), self.before)  # recover() not run either
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "1")


class DaemonJsonProblemTest(PostponeBase):
    """/etc/docker/daemon.json that dockerd cannot read (NocowPaths.config_problem): which volumes
    folder Docker uses is unknown, so nothing is migrated, and mark writes only the request."""

    PROBLEM = "/etc/docker/daemon.json: not JSON"

    @override
    def setUp(self) -> None:
        super().setUp()
        self.paths = replace(self.paths, config_problem=self.PROBLEM)
        self.cli = Commands(nocow_wiring.build_nocow(self.paths, runner=self.fs))

    def test_the_migration_is_postponed_and_recorded(self) -> None:
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        why = f"{self.PROBLEM}, so the volumes folder Docker uses is unknown"
        self.assert_postponed(out, why)
        with open(self.postponed) as f:
            self.assertEqual(json.load(f)["reason"], why)
        self.assertIn(f"{self.PROBLEM}: the boot's migration waits until it is fixed\n", self.call(self.cli.status)[1])
        self.assertIn("migration postponed at 1 boot(s) in a row", self.call(self.cli.status)[1])

    def test_recorded_with_nothing_scheduled_and_no_default_folder(self) -> None:
        # The default folder says nothing about the one Docker uses (data-root may have pointed
        # elsewhere before the file broke): the boot is recorded all the same, for status and doctor.
        os.remove(self.flag)
        for vols in ("+C", "absent"):
            with self.subTest(vols=vols):
                if vols == "absent":
                    shutil.rmtree(self.vols)
                self.assertEqual(self.call(self.cli.migrate)[0], 0)
                with open(self.postponed) as f:
                    self.assertEqual(
                        json.load(f)["reason"], f"{self.PROBLEM}, so the volumes folder Docker uses is unknown"
                    )
                self.assertIn(
                    f"{self.PROBLEM}: the boot's migration waits until it is fixed\n", self.call(self.cli.status)[1]
                )

    def test_mark_writes_only_the_request(self) -> None:
        # No +C (the folder Docker uses is unknown), but the request, as mark's own: an old count
        # without its flag is reset first, so the first boot after the fix gets its two boots. A flag
        # written beside that count (btrfs-setup's touch, before) made that boot give up at once.
        os.remove(self.flag)
        self.set_attempts("2")
        self.fs.sets.clear()
        before = tree(self.vols)
        rc, out, err = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.PROBLEM}: nothing marked (fix the file, then run it again)", out + err)
        self.assertEqual((self.fs.sets, tree(self.vols)), ([], before))
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(read_attempts(self), "0")

    def test_mark_that_cannot_write_the_request_says_so(self) -> None:
        os.remove(self.flag)
        os.rmdir(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.sets.clear()
        before = tree(self.vols)
        rc, out, err = self.main("mark")
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.PROBLEM}: nothing marked (fix the file, then run it again)", out + err)
        self.assertIn(f"<3>{TAG}: cannot schedule the migration ([Errno 17] File exists", out + err)
        self.assertEqual((self.fs.sets, tree(self.vols)), ([], before))
        self.assertFalse(os.path.exists(self.flag))


class DockerRunningTest(PostponeBase):
    """docker_busy(): the ActiveState of docker.service, failing closed."""

    def test_running_postpones_without_touching_anything(self) -> None:
        for state in ("active", "activating", "reloading", "deactivating"):
            with self.subTest(state=state):
                self.fs.docker_state = state
                self.fs.calls.clear()
                rc, out = self.call(self.cli.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(out, f"<4>{TAG}: Docker is running: migration left for the next boot\n")
                self.assertEqual(self.fs.calls, [["systemctl", "show", "-P", "ActiveState", "docker.service"]])
                self.assertEqual(self.fs.sets, [])
                self.assertEqual(tree(os.path.join(self.vols, "cow")), self.before)  # recover() not run either
                self.assertTrue(os.path.exists(self.flag))
                # Not counted as an attempt and no alert; the boot is
                # recorded instead (PostponedRecordTest).
                self.assertEqual(read_attempts(self), "1")
                self.assertEqual(self.fs.commands(self.notify), [])

    def test_stopped_migrates(self) -> None:
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                fresh(self)
                self.fs.docker_state = state
                self.assert_migrated(self.call(self.cli.migrate)[1])

    def test_unknown_state_postpones(self) -> None:
        # Fails closed: only exit code 0 with "inactive" or "failed" lets the
        # migration run. systemctl failing (no manager on the bus) with empty
        # output, an unknown word, or a stopped state with a failing exit code
        # all postpone, with a line that is not "Docker is running".
        for rc, state, shown in (
            (1, "", "systemctl exit 1, ActiveState ''"),
            (0, "", "systemctl exit 0, ActiveState ''"),
            (0, "maintenance", "systemctl exit 0, ActiveState 'maintenance'"),
            (0, "refreshing", "systemctl exit 0, ActiveState 'refreshing'"),
            (1, "inactive", "systemctl exit 1, ActiveState 'inactive'"),
            (4, "active", "systemctl exit 4, ActiveState 'active'"),
        ):
            with self.subTest(rc=rc, state=state):
                fresh(self)
                self.fs.systemctl_rc, self.fs.docker_state = rc, state
                self.fs.systemctl_out_on_error = True
                rc_, out = self.call(self.cli.migrate)
                self.assertEqual(rc_, 0)
                self.assert_postponed(out, f"docker.service state unknown ({shown})")

    def test_an_empty_reason_still_postpones(self) -> None:
        # A reason is any text, "" too: it must not hand over to dockerd_busy() and migrate.
        with (
            mock.patch.object(nocow_adapters.DockerDaemons, "service_busy", autospec=True, return_value=""),
            mock.patch.object(nocow_adapters.DockerDaemons, "dockerd_busy", autospec=True, return_value=None),
        ):
            rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assert_postponed(out, "")

    def test_no_systemctl_postpones(self) -> None:
        # subprocess.run raises FileNotFoundError: postponed like an unknown
        # state, nothing touched, attempts not counted.
        self.fs.missing.add("systemctl")
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assert_postponed(
            out, "cannot ask systemd about docker.service ([Errno 2] No such file or directory: 'systemctl')"
        )


class DockerdTest(PostponeBase):
    """dockerd_busy(): a dockerd outside docker.service, in a fake /proc.

    Each process is PROC/PID with comm, cmdline, status (its uids), and the
    links exe, root (the / of its mount namespace) and cwd. root pointing at
    the test folder is a dockerd in our mount namespace (or in rootless
    Docker's, which keeps our /); pointing at another folder, one in a
    container."""

    @override
    def setUp(self) -> None:
        super().setUp()
        # Another mount namespace (a container): its own /var/lib/docker.
        self.other = os.path.join(self.tmp, "other-ns")
        os.makedirs(os.path.join(self.other, "var/lib/docker/volumes"))

    def process(
        self,
        pid: int,
        args: list[str],
        root: str | None = None,
        comm: str = "dockerd",
        exe: str | None = "/usr/bin/dockerd",
        cwd: str = "/",
        uid_line: str = "Uid:\t0\t0\t0\t0",
    ) -> str:
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in args))
        with open(os.path.join(d, "status"), "w") as f:
            f.write(f"Name:\t{comm}\nUmask:\t0022\nState:\tS (sleeping)\n{uid_line}\nGid:\t0\t0\t0\t0\n")
        if exe is not None:
            os.symlink(exe, os.path.join(d, "exe"))
        top = self.tmp if root is None else root
        os.symlink(top, os.path.join(d, "root"))
        os.symlink(os.path.join(top, cwd.lstrip("/")), os.path.join(d, "cwd"))
        return d

    def daemon_json(self, top: str, text: str, name: str = "daemon.json") -> None:
        os.makedirs(os.path.join(top, "etc/docker"), exist_ok=True)
        with open(os.path.join(top, "etc/docker", name), "w") as f:
            f.write(text)

    def decoy(self) -> None:
        """/etc/docker/daemon.json names /data/docker, a data root that is
        not ours: a dockerd whose flags were not read would look elsewhere."""
        os.makedirs(os.path.join(self.tmp, "data/docker/volumes"))
        self.daemon_json(self.tmp, '{"data-root": "/data/docker"}')

    def alias(self) -> str:
        """/srv/docker, another name for our data root, with the decoy: a
        case that postpones proves the path was read."""
        self.decoy()
        os.makedirs(os.path.join(self.tmp, "srv"))
        os.symlink("../var/lib/docker", os.path.join(self.tmp, "srv/docker"))
        return "/srv/docker"

    # ---- dockerd processes that use this data root -------------------------

    def ours_default(self) -> None:
        self.process(7, ["/usr/bin/dockerd"])

    def ours_data_root_equals(self) -> None:
        self.process(7, ["dockerd", f"--data-root={self.alias()}"])

    def ours_data_root_space(self) -> None:
        self.process(7, ["dockerd", "-H", "fd://", "--data-root", self.alias()])

    def ours_last_data_root_wins(self) -> None:
        self.process(7, ["dockerd", "--data-root=/data/docker", "--data-root", self.alias()])

    def ours_short_g(self) -> None:
        # -g and --graph: the old names of --data-root (removed in Docker 23).
        self.process(7, ["dockerd", "-g", self.alias()])

    def ours_short_g_attached(self) -> None:
        self.process(7, ["dockerd", f"-g{self.alias()}"])

    def ours_short_g_equals(self) -> None:
        self.process(7, ["dockerd", f"-g={self.alias()}"])

    def ours_graph(self) -> None:
        self.process(7, ["dockerd", "--graph", self.alias()])

    def ours_graph_equals(self) -> None:
        self.process(7, ["dockerd", f"--graph={self.alias()}"])

    def ours_relative(self) -> None:
        self.decoy()
        self.process(7, ["dockerd", "--data-root=docker"], cwd="/var/lib")

    def ours_daemon_json(self) -> None:
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}')
        self.process(7, ["dockerd"])

    def ours_config_file(self) -> None:
        # --config-file names ours; /etc/docker/daemon.json (the decoy) is not read then.
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}', "custom.json")
        self.process(7, ["dockerd", "--config-file", "/etc/docker/custom.json"])

    def ours_config_file_equals(self) -> None:
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}', "custom.json")
        self.process(7, ["dockerd", "--config-file=/etc/docker/custom.json"])

    def ours_by_program_file(self) -> None:
        # Renamed process, program file replaced by an upgrade.
        self.process(7, ["d"], comm="d", exe="/usr/bin/dockerd (deleted)")

    def ours_by_name(self) -> None:
        # Its program file cannot be read (exe missing): the name is enough.
        self.process(7, ["/opt/docker/bin/x"], exe=None)

    def ours_no_uid_line(self) -> None:
        # Whether it runs as root cannot be told: counted (fails closed).
        self.process(7, ["dockerd"], uid_line="")

    def ours_uid_garbled(self) -> None:
        self.process(7, ["dockerd"], uid_line="Uid:\t1000\tx\t1000\t1000")

    def ours_root_after_setuid(self) -> None:
        # The effective uid decides (the second field), not the real one.
        self.process(7, ["dockerd"], uid_line="Uid:\t1000\t0\t0\t0")

    def ours_bind_mounted(self) -> None:
        # A container that bind-mounts this data root (a link stands in for
        # the bind mount: the same directory, seen from another namespace).
        os.makedirs(os.path.join(self.other, "srv"))
        os.symlink("../../var/lib/docker", os.path.join(self.other, "srv/docker"))
        self.process(7, ["dockerd", "--data-root=/srv/docker"], root=self.other)

    def test_dockerd_on_this_data_root_postpones(self) -> None:
        for add in (
            self.ours_default,
            self.ours_data_root_equals,
            self.ours_data_root_space,
            self.ours_last_data_root_wins,
            self.ours_short_g,
            self.ours_short_g_attached,
            self.ours_short_g_equals,
            self.ours_graph,
            self.ours_graph_equals,
            self.ours_relative,
            self.ours_daemon_json,
            self.ours_config_file,
            self.ours_config_file_equals,
            self.ours_by_program_file,
            self.ours_by_name,
            self.ours_no_uid_line,
            self.ours_uid_garbled,
            self.ours_root_after_setuid,
            self.ours_bind_mounted,
        ):
            with self.subTest(case=add.__name__):
                fresh(self)
                add()
                rc, out = self.call(self.cli.migrate)
                self.assertEqual(rc, 0)
                self.assert_postponed(out, f"dockerd (pid 7) uses {self.vols} outside docker.service")

    def test_a_config_file_it_cannot_read_as_one_postpones(self) -> None:
        # Not an object: that dockerd refuses it, but which data root it would use is unknown
        # (read as its default before, which made it ours only by chance).
        d = self.process(7, ["dockerd"])
        self.daemon_json(self.tmp, '["data-root"]')
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        path = os.path.join(d, "root", "etc/docker/daemon.json")
        self.assert_postponed(
            out, f"dockerd (pid 7) is running and its data root cannot be read ({path}: not a JSON object)"
        )

    def test_unreadable_config_file_postpones(self) -> None:
        # Fails closed: a config file that exists but cannot be read may name
        # our data root (dockerd's default is not assumed).
        d = self.process(7, ["dockerd"])
        self.daemon_json(self.tmp, '{"data-root": "/data/docker"}')
        blocked = os.path.join(d, "root", "etc/docker/daemon.json")
        real_open = builtins.open

        def fake_open(path: str, mode: str = "r", encoding: str | None = None) -> object:
            if path == blocked:
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, mode, encoding=encoding)

        with mock.patch.object(builtins, "open", fake_open):
            rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        why = f"dockerd (pid 7) is running and its data root cannot be read ([Errno 13] Permission denied: '{blocked}')"
        self.assert_postponed(out, why)

    def test_unreadable_data_root_postpones(self) -> None:
        # Fails closed: a dockerd whose data root cannot be read may be ours.
        d = self.process(7, ["dockerd"])
        real_stat = os.stat
        blocked = os.path.join(d, "root", "var/lib/docker/volumes")

        def stat(path: str, *a: object, **kw: object) -> os.stat_result:
            if path == blocked:
                raise PermissionError(13, "Permission denied", path)
            return real_stat(path)

        with mock.patch.object(os, "stat", stat):
            rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        why = f"dockerd (pid 7) is running and its data root cannot be read ([Errno 13] Permission denied: '{blocked}')"
        self.assert_postponed(out, why)

    # ---- processes that must not hold the migration back -------------------

    def other_rootless(self) -> None:
        # Rootless Docker as dockerd-rootless.sh starts it: plain `dockerd`
        # (its data root comes from $XDG_DATA_HOME or $HOME, not the command
        # line) as the user, in a namespace that keeps our / (so our
        # /var/lib/docker is what its default path shows). And the script
        # that starts it, whose name begins with "dockerd" (comm keeps 15
        # characters).
        os.makedirs(os.path.join(self.tmp, "home/u/.local/share/docker/volumes"))
        self.process(7, ["dockerd"], uid_line="Uid:\t1000\t1000\t1000\t1000")
        self.process(
            8,
            ["/bin/sh", "/usr/bin/dockerd-rootless.sh"],
            comm="dockerd-rootles",
            exe="/usr/bin/bash",
            uid_line="Uid:\t1000\t1000\t1000\t1000",
        )

    def other_user_dockerd_in_a_userns(self) -> None:
        # The same, as root in its own user namespace that maps to uid
        # 100000 here (status shows the uid as seen from this namespace).
        self.process(7, ["dockerd", "--data-root=/var/lib/docker"], uid_line="Uid:\t100000\t100000\t100000\t100000")

    def other_config_file(self) -> None:
        # /etc/docker/daemon.json names our data root, but --config-file another.
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}')
        self.daemon_json(self.tmp, '{"data-root": "/data/docker"}', "custom.json")
        self.process(7, ["dockerd", "--config-file", "/etc/docker/custom.json"])

    def other_odd_program_name(self) -> None:
        # A program file name that is not valid UTF-8 (read as bytes, not decoded).
        self.process(7, ["x"], comm="caf", exe=os.fsdecode(b"/opt/x/caf\xe9"))

    def other_containerised(self) -> None:
        # In a container: the /var/lib/docker of its own mount namespace.
        self.process(7, ["dockerd"], root=self.other)

    def other_containerised_daemon_json(self) -> None:
        # Its own daemon.json is read, not ours (which names our data root).
        self.daemon_json(self.tmp, '{"data-root": "/var/lib/docker"}')
        self.daemon_json(self.other, '{"data-root": "/data/docker"}')
        os.makedirs(os.path.join(self.other, "data/docker/volumes"))
        self.process(7, ["dockerd"], root=self.other)

    def other_data_root(self) -> None:
        # Our namespace, another data root, with no volumes folder yet.
        self.process(7, ["dockerd", "--data-root=/srv/docker"])

    def other_not_dockerd(self) -> None:
        self.process(7, ["sleep", "--data-root=/var/lib/docker"], comm="sleep", exe=None)

    def other_ended(self) -> None:
        # Ended while the helper looked: only its empty folder is left.
        os.makedirs(os.path.join(self.proc, "7"))

    def other_not_processes(self) -> None:
        os.makedirs(os.path.join(self.proc, "sys/kernel"))
        os.symlink("8", os.path.join(self.proc, "self"))
        self.process(
            8,
            ["/usr/bin/python3", "-I", "/usr/local/sbin/docker-volumes-nocow", "migrate"],
            comm="python3",
            exe="/usr/bin/python3.14",
        )

    def test_other_dockerd_does_not_postpone(self) -> None:
        for add in (
            self.other_rootless,
            self.other_user_dockerd_in_a_userns,
            self.other_config_file,
            self.other_odd_program_name,
            self.other_containerised,
            self.other_containerised_daemon_json,
            self.other_data_root,
            self.other_not_dockerd,
            self.other_ended,
            self.other_not_processes,
        ):
            with self.subTest(case=add.__name__):
                fresh(self)
                add()
                rc, out = self.call(self.cli.migrate)
                self.assertEqual(rc, 0)
                self.assert_migrated(out)


class PostponedRecordTest(PostponeBase):
    """POSTPONED: each boot at which the migration had to wait, for status
    and `cst doctor` (a postponed boot counts no attempt and sends no alert)."""

    def boot(self, boot_id: str, at: int) -> tuple[int | None, str]:
        """migrate() during boot boot_id, at time at, with Docker running."""
        os.makedirs(os.path.join(self.proc, "sys/kernel/random"), exist_ok=True)
        with open(os.path.join(self.proc, "sys/kernel/random/boot_id"), "w") as f:
            f.write(boot_id + "\n")
        with mock.patch.object(time, "time", return_value=at + 0.5):
            return self.call(self.cli.migrate)

    def record(self) -> dict[str, object] | None:
        try:
            with open(self.postponed) as f:
                return object_of(parse_json(f.read()))
        except FileNotFoundError:
            return None

    def test_counts_boots_not_runs(self) -> None:
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        self.assertEqual(
            self.record(),
            {"reason": "Docker is running", "boots": 1, "first": 1000, "last": 1000, "boot_id": "b1"},
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.postponed).st_mode), 0o644)  # cst doctor reads it as the user
        # Run again in the same boot, for another reason: still one boot.
        self.fs.docker_state = "maintenance"
        self.boot("b1", 1100)
        rec = self.record()
        assert rec is not None
        self.assertEqual((rec["boots"], rec["first"], rec["last"]), (1, 1000, 1100))
        self.assertEqual(rec["reason"], "docker.service state unknown (systemctl exit 0, ActiveState 'maintenance')")
        # The next boots.
        self.fs.docker_state = "active"
        self.boot("b2", 2000)
        self.boot("b3", 3000)
        self.assertEqual(
            self.record(),
            {"reason": "Docker is running", "boots": 3, "first": 1000, "last": 3000, "boot_id": "b3"},
        )
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.postponed))), ["attempts", "postponed"])  # no .new
        self.assertEqual(read_attempts(self), "1")
        self.assertEqual(self.fs.commands(self.notify), [])
        # status shows it.
        _rc, out = self.call(self.cli.status)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(3000))
        self.assertIn(
            "  1 with copy-on-write data (2 MiB), migration at the next boot\n"
            f"  migration postponed at 3 boot(s) in a row, last {when}: Docker is running\n",
            out,
        )
        # A boot that is not held back removes the record.
        self.fs.docker_state = "inactive"
        _rc, out = self.boot("b4", 4000)
        self.assert_migrated(out)
        self.assertIsNone(self.record())
        self.assertNotIn("postponed", self.call(self.cli.status)[1])

    def test_recorded_when_the_folder_has_no_nocow_flag(self) -> None:
        # Nothing scheduled, but the volumes folder lost +C (Docker installed
        # after install): that is work the boot could not do either.
        os.remove(self.flag)
        self.fs.set(self.vols, False)
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        rec = self.record()
        assert rec is not None
        self.assertEqual(rec["boots"], 1)
        self.assertEqual(self.fs.sets, [])

    def test_nothing_to_do_records_nothing_and_drops_an_old_record(self) -> None:
        os.remove(self.flag)  # nothing scheduled, the folder has +C
        self.fs.docker_state = "active"
        os.makedirs(os.path.dirname(self.postponed), exist_ok=True)
        with open(self.postponed, "w") as f:
            f.write('{"reason": "Docker is running", "boots": 4, "first": 1, "last": 2, "boot_id": "old"}\n')
        _rc, out = self.boot("b1", 1000)
        self.assertEqual(out, f"<4>{TAG}: Docker is running: migration left for the next boot\n")
        self.assertIsNone(self.record())

    def test_volumes_folder_gone_drops_the_record(self) -> None:
        # Docker removed, or data-root moved: no volumes, nothing waits. The
        # old record must not keep `cst doctor` warning (status hides it).
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        self.assertIsNotNone(self.record())
        shutil.rmtree(self.vols)
        self.assertEqual(self.boot("b2", 2000), (0, ""))
        self.assertIsNone(self.record())

    def test_a_damaged_record_starts_afresh(self) -> None:
        self.fs.docker_state = "active"
        for text in ("not json", "[1, 2]", '{"boots": "3", "first": 1}', '{"boots": 2, "first": true}', ""):
            with self.subTest(text=text):
                os.makedirs(os.path.dirname(self.postponed), exist_ok=True)
                with open(self.postponed, "w") as f:
                    f.write(text)
                self.boot("b1", 1000)
                rec = self.record()
                assert rec is not None
                self.assertEqual((rec["boots"], rec["first"]), (1, 1000))
                self.assertIn("postponed at 1 boot(s) in a row", self.call(self.cli.status)[1])

    def test_without_a_boot_id(self) -> None:
        # No boot id to read: the first postponement is still recorded.
        self.fs.docker_state = "active"
        with mock.patch.object(time, "time", return_value=1000.0):
            self.call(self.cli.migrate)
        rec = self.record()
        assert rec is not None
        self.assertEqual((rec["boots"], rec["boot_id"]), (1, ""))

    def test_unmark_removes_the_record(self) -> None:
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        self.assertIsNotNone(self.record())
        self.call(self.cli.unmark)
        self.assertIsNone(self.record())
        self.assertEqual(self.call(self.cli.unmark), (0, ""))  # and again, with nothing to remove


class RecoverTest(Base):
    """A crash at each step of migrate_one(), then the next boot."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        self.vol = os.path.join(self.vols, "cow")
        self.before = tree(self.top)
        self.ino = ino(self.top)
        schedule(self)

    def second_rename_raises(self, exc: BaseException) -> contextlib.AbstractContextManager[object]:
        """rename(new, data), the second rename of the swap, raises exc; the others run."""
        real_rename = os.rename

        def rename(src: str, dst: str) -> None:
            if src.endswith(".nocow-new"):
                raise exc
            real_rename(src, dst)

        return mock.patch.object(os, "rename", rename)

    def crash(self) -> None:
        with self.assertRaises(Crash):
            self.call(self.cli.migrate)
        self.assertEqual(read_attempts(self), "1")
        self.assertTrue(os.path.exists(self.flag))

    def next_boot_finishes(self, restored: bool) -> None:
        rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(f"<4>{TAG}: cow: restored the original after an interrupted migration\n" in out, restored, out)
        self.assertEqual(tree(self.top), self.before)
        self.assert_all_nocow(self.top)
        self.assert_no_leftovers("cow")
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))

    def test_crash_during_the_copy(self) -> None:
        self.fs.cp = lambda src, dst: (self.fs.copy_into(src, dst, stop_after=4), "")
        self.crash()
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.nocow-new"])
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.fs.cp = self.fs.copy
        self.next_boot_finishes(restored=False)

    def test_crash_between_the_renames(self) -> None:
        with self.second_rename_raises(Crash("power loss between the two renames")):
            self.crash()
        self.assertEqual(self.fs.syncs, 1)  # the copy was on disk before the first rename
        names = sorted(os.listdir(self.vol))
        self.assertEqual(len(names), 2, names)
        self.assertRegex(names[0], r"^_data\.cow-old-\d+$")
        self.assertEqual(names[1], "_data.nocow-new")
        old = os.path.join(self.vol, names[0])
        self.assertEqual(ino(old), self.ino)
        # recover() alone: the original back under its name, the copy gone.
        _rc, out = self.call(self.cli.recover)
        self.assertEqual(out, f"<4>{TAG}: cow: restored the original after an interrupted migration\n")
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assert_no_leftovers("cow")
        self.assertFalse(self.fs.nocow(os.path.join(self.top, "README")))
        # The same state again, through the next boot: restored, then migrated.
        os.rename(self.top, old)
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.next_boot_finishes(restored=True)

    def test_crash_before_the_old_copy_is_deleted(self) -> None:
        real_rmtree = shutil.rmtree

        def rmtree(path: str, *a: object, **kw: object) -> None:
            if ".cow-old-" in path:
                os.remove(os.path.join(path, "README"))  # partly deleted
                raise Crash("power loss while deleting the old copy")
            real_rmtree(path)

        with mock.patch.object(shutil, "rmtree", rmtree):
            self.crash()
        names = sorted(os.listdir(self.vol))
        self.assertEqual(names[0], "_data")
        self.assertRegex(names[1], r"^_data\.cow-old-\d+$")
        self.assertEqual(len(names), 2, names)
        self.assert_all_nocow(self.top)  # the new copy is in place already
        self.next_boot_finishes(restored=False)

    def test_failed_second_rename_puts_the_original_back(self) -> None:
        # rename(new, data) fails with an OSError: the original goes back at
        # once; the copy stays until the next boot's recover() removes it.
        with self.second_rename_raises(OSError(16, "Device or resource busy")):
            rc, out = self.call(self.cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fs.syncs, 1)  # before the first rename
        self.assertIn(f"<3>{TAG}: cow: [Errno 16] Device or resource busy; left as it was\n", out)
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.nocow-new"])
        self.assertEqual(read_attempts(self), "1")
        self.next_boot_finishes(restored=False)

    def test_recover_cases(self) -> None:
        self.call(self.cli.recover)  # nothing to do: nothing changes
        self.assert_no_leftovers("cow")
        # _data there: every old copy and the new one go.
        for x in ("_data.cow-old-1700000000", "_data.cow-old-1700000100", "_data.nocow-new"):
            os.makedirs(os.path.join(self.vol, x, "sub"))
        _rc, out = self.call(self.cli.recover)
        self.assertEqual(out, "")  # removed without a log line
        self.assert_no_leftovers("cow")
        self.assertEqual(ino(self.top), self.ino)
        # _data missing, two old copies: the newest (by name) comes back.
        os.rename(self.top, os.path.join(self.vol, "_data.cow-old-1700000100"))
        os.makedirs(os.path.join(self.vol, "_data.cow-old-1700000000"))
        self.call(self.cli.recover)
        self.assertEqual(ino(self.top), self.ino)
        self.assert_no_leftovers("cow")
        # _data missing and only the new copy: kept, nothing is deleted.
        os.rename(self.top, os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(self.cli.recover), (None, ""))
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data.nocow-new"])

    def test_recover_puts_back_the_newest_old_copy_by_its_number(self) -> None:
        # The clock went past 10 digits' worth of seconds between two migrations (or had fewer
        # digits once): 1000000000 is newer than 999999999, although its text sorts first.
        os.rename(self.top, os.path.join(self.vol, "_data.cow-old-1000000000"))
        os.makedirs(os.path.join(self.vol, "_data.cow-old-999999999", "stale"))
        _rc, out = self.call(self.cli.recover)
        self.assertEqual(out, f"<4>{TAG}: cow: restored the original after an interrupted migration\n")
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assert_no_leftovers("cow")

    def test_a_clock_behind_an_old_copy_left_over_still_restores_the_original(self) -> None:
        # An earlier migration's old copy could not be deleted (a file in it is immutable, say) and
        # is named after a time ahead of the clock now (the clock went back): this migration names
        # its own old copy above it, so after a kill between the renames the original comes back.
        stale = os.path.join(self.vol, "_data.cow-old-9999999999")
        os.makedirs(os.path.join(stale, "stale"))
        real_rmtree = shutil.rmtree

        def rmtree(path: str, *, ignore_errors: bool = False) -> None:
            if path != stale:
                real_rmtree(path, ignore_errors=ignore_errors)

        with mock.patch.object(shutil, "rmtree", rmtree), self.second_rename_raises(Crash("power loss")):
            self.crash()
        names = sorted(os.listdir(self.vol))
        self.assertEqual(names, ["_data.cow-old-10000000000", "_data.cow-old-9999999999", "_data.nocow-new"])
        self.assertEqual(ino(os.path.join(self.vol, "_data.cow-old-10000000000")), self.ino)
        self.next_boot_finishes(restored=True)  # the original's content, migrated; the stale copy gone

    def test_recover_leaves_a_data_that_is_not_a_folder(self) -> None:
        # Something named _data that is not a folder (here a dangling link):
        # recover() neither puts the old copy over it nor deletes the copies.
        os.rename(self.top, os.path.join(self.vol, "_data.cow-old-1700000000"))
        os.symlink("gone", self.top)
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(self.cli.recover), (None, ""))
        self.assertEqual(os.readlink(self.top), "gone")
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.cow-old-1700000000", "_data.nocow-new"])
        self.assertEqual(ino(os.path.join(self.vol, "_data.cow-old-1700000000")), self.ino)

    def test_recover_skips_files_and_links(self) -> None:
        # metadata.db (a file) and a symlinked volume folder are not volumes:
        # what the link points to keeps its look-alike leftovers.
        elsewhere = os.path.join(self.tmp, "elsewhere")
        names = ["_data", "_data.cow-old-1700000000", "_data.nocow-new"]
        for x in names:
            os.makedirs(os.path.join(elsewhere, x))
        os.symlink(elsewhere, os.path.join(self.vols, "link"))
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(self.cli.recover), (None, ""))
        self.assertTrue(os.path.isfile(os.path.join(self.vols, "metadata.db")))
        self.assertTrue(os.path.islink(os.path.join(self.vols, "link")))
        self.assertEqual(sorted(os.listdir(elsewhere)), names)
        self.assert_no_leftovers("cow")  # the real volume is still cleaned


class RecoverEachVolumeTest(Base):
    """A volume that cannot be recovered (an OSError) does not stop the recovery of the others."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, nocow=True)
        schedule(self)
        self.before: dict[str, dict[str, tuple[object, ...]]] = {}
        self.ino: dict[str, int] = {}
        for name in ("a", "b"):
            top = self.volume(name)
            self.before[name], self.ino[name] = tree(top), ino(top)
            # Died between the two renames: no _data, the original is its old copy.
            os.rename(top, os.path.join(self.vols, name, "_data.cow-old-1700000000"))

    def boot_with(self, patch: Callable[[str], contextlib.AbstractContextManager[object]]) -> None:
        """For each volume as the one that fails (whichever the volumes folder lists first), the next boot."""
        for bad, good in (("a", "b"), ("b", "a")):
            with self.subTest(failing=bad):
                fresh(self)
                with patch(os.path.join(self.vols, bad)):
                    rc, out, _err = self.main("migrate")
                self.assertEqual(rc, 0)
                data = os.path.join(self.vols, good, "_data")
                self.assertEqual(ino(data), self.ino[good])
                self.assertEqual(tree(data), self.before[good])
                self.assert_no_leftovers(good)
                self.assertIn(f"<4>{TAG}: {good}: restored the original after an interrupted migration\n", out)
                self.assertIn(f"<3>{TAG}: {bad}: [Errno 5] Input/output error; recovery left for the next boot\n", out)
                self.assertNotIn(f"{bad}: restored", out)
                self.assertEqual(sorted(os.listdir(os.path.join(self.vols, bad))), ["_data.cow-old-1700000000"])
                self.assertIn(
                    f"<3>{TAG}: {self.vols}: 1 volume(s) not recovered: migration left for the next boot\n", out
                )
                # This boot migrates nothing: the request and the copy-on-write data wait for the next one.
                self.assertTrue(os.path.exists(self.flag))
                self.assertIsNone(read_attempts(self))
                self.assertFalse(self.fs.nocow(os.path.join(data, "README")))
                # The next boot, with no error, recovers the other one too and migrates both.
                rc, out, _err = self.main("migrate")
                self.assertEqual(rc, 0)
                self.assertIn(f"<4>{TAG}: {bad}: restored the original after an interrupted migration\n", out)
                for name in ("a", "b"):
                    self.assertEqual(tree(os.path.join(self.vols, name, "_data")), self.before[name])
                    self.assert_all_nocow(os.path.join(self.vols, name, "_data"))
                    self.assert_no_leftovers(name)
                self.assertFalse(os.path.exists(self.flag))

    def test_a_rename_that_fails(self) -> None:
        real_rename = os.rename

        def failing(folder: str) -> contextlib.AbstractContextManager[object]:
            def rename(src: str, dst: str) -> None:
                if src.startswith(folder + os.sep):
                    raise OSError(errno.EIO, "Input/output error")
                real_rename(src, dst)

            return mock.patch.object(os, "rename", rename)

        self.boot_with(failing)

    def test_a_volume_folder_that_cannot_be_listed(self) -> None:
        real_listdir = os.listdir

        def failing(folder: str) -> contextlib.AbstractContextManager[object]:
            def listdir(path: str) -> list[str]:
                if path == folder:
                    raise OSError(errno.EIO, "Input/output error")
                return real_listdir(path)

            return mock.patch.object(os, "listdir", listdir)

        self.boot_with(failing)


class OldCopyNameTest(unittest.TestCase):
    """The old copies' order and the name of a new one (no I/O)."""

    def test_old_copies_are_ordered_by_their_number(self) -> None:
        names = ["_data.cow-old-1000000000", "_data.cow-old-999999999", "_data.cow-old-x", "_data.cow-old-1700000000"]
        self.assertEqual(
            oldest_first(names),
            ("_data.cow-old-x", "_data.cow-old-999999999", "_data.cow-old-1000000000", "_data.cow-old-1700000000"),
        )
        self.assertEqual(old_copy_stamp("_data.cow-old-1700000000"), 1700000000)
        for name in (
            "_data.cow-old-",
            "_data.cow-old-17x",
            "_data.cow-old-\N{ARABIC-INDIC DIGIT THREE}",
            "_data.cow-old--1",
            "_data",
        ):
            with self.subTest(name=name):
                self.assertIsNone(old_copy_stamp(name))

    def test_a_new_old_copy_is_named_above_every_one_still_there(self) -> None:
        self.assertEqual(old_copy_name((), 1700000000), "_data.cow-old-1700000000")
        self.assertEqual(old_copy_name(("_data.cow-old-1600000000",), 1700000000), "_data.cow-old-1700000000")
        self.assertEqual(old_copy_name(("_data.cow-old-1700000000",), 1700000000), "_data.cow-old-1700000001")
        self.assertEqual(
            old_copy_name(("_data.cow-old-1800000000", "_data.cow-old-junk"), 1700000000), "_data.cow-old-1800000001"
        )


class RecoveryTest(unittest.TestCase):
    """recovery(): what recover does in one volume, from what its folder holds (no I/O)."""

    def test_the_choice_for_each_volume_folder(self) -> None:
        o1, o2, o3 = "_data.cow-old-1700000001", "_data.cow-old-1700000002", "_data.cow-old-1700000003"
        cases = [
            # (_data there, old copies oldest first, twin there) -> (put back, then removed)
            ((False, (), False), (None, ())),
            ((False, (), True), (None, ("_data.nocow-new",))),
            ((False, (o1,), False), (o1, ())),
            ((False, (o1, o2), True), (o2, (o1, "_data.nocow-new"))),
            ((False, (o1, o2, o3), False), (o3, (o1, o2))),
            ((True, (), False), (None, ())),
            ((True, (), True), (None, ("_data.nocow-new",))),
            ((True, (o1, o2), False), (None, (o1, o2))),
            ((True, (o1, o2, o3), True), (None, (o1, o2, o3, "_data.nocow-new"))),
        ]
        for (has_data, olds, twin), (restore, remove) in cases:
            with self.subTest(has_data=has_data, olds=olds, twin=twin):
                todo = recovery(VolumeLeftovers(has_data=has_data, olds=olds, twin=twin))
                self.assertEqual((todo.restore, todo.remove), (restore, remove))


class PostponedDurableTest(PostponeBase):
    """POSTPONED is a boot counter: its file and folder are fsynced, so a reset right after cannot lose a boot."""

    def test_file_and_folder_fsynced(self) -> None:
        self.fs.docker_state = "active"
        os.makedirs(os.path.join(self.proc, "sys/kernel/random"), exist_ok=True)
        with open(os.path.join(self.proc, "sys/kernel/random/boot_id"), "w") as f:
            f.write("b1\n")
        with recorded_fsyncs() as synced:
            self.call(self.cli.migrate)
        assert_replaced_durably(self, self.postponed, synced)
        self.assertTrue(os.path.exists(self.postponed))


class OutcomeRecordTest(Base):
    """The outcome record (/var/lib/docker-volumes-nocow/outcome): written by a give-up, shown by status,
    removed by a new request, by a migration that ends, by mark with nothing to migrate and by unmark."""

    def gave_up(self, at: int = 1_700_000_000, *, version: int = 1) -> None:
        """A record of a give-up, as give_up writes it."""
        os.makedirs(os.path.dirname(self.outcome), exist_ok=True)
        with open(self.outcome, "w") as f:
            json.dump(
                {"v": version, "outcome": "gave-up", "at": at, "volumes": self.vols, "reason": "it did not finish"}, f
            )

    def record(self) -> dict[str, object]:
        with open(self.outcome) as f:
            return object_of(parse_json(f.read()))

    def give_up_boot(self) -> str:
        self.volume("cow")
        schedule(self)
        self.set_attempts("2")
        _rc, out = self.call(self.cli.migrate)
        return out

    def test_a_give_up_is_recorded_durably_before_the_counter_and_the_flag_go(self) -> None:
        seen: list[tuple[str, bool]] = []
        state = nocow_adapters.NocowStateFiles
        real_remove_attempts, real_remove_flag = state.remove_attempts, state.remove_flag

        def remove_attempts(this: nocow_adapters.NocowStateFiles) -> None:
            seen.append(("remove_attempts", os.path.exists(self.outcome)))
            real_remove_attempts(this)

        def remove_flag(this: nocow_adapters.NocowStateFiles) -> None:
            seen.append(("remove_flag", os.path.exists(self.outcome)))
            real_remove_flag(this)

        with (
            mock.patch.object(state, "remove_attempts", remove_attempts),
            mock.patch.object(state, "remove_flag", remove_flag),
            recorded_fsyncs() as synced,
            mock.patch.object(time, "time", return_value=1_700_000_000.5),
        ):
            out = self.give_up_boot()
        self.assertEqual(seen, [("remove_attempts", True), ("remove_flag", True)])
        assert_replaced_durably(self, self.outcome, synced)
        self.assertEqual(stat.S_IMODE(os.stat(self.outcome).st_mode), 0o644)
        self.assertEqual(
            self.record(),
            {
                "v": 1,
                "outcome": "gave-up",
                "at": 1_700_000_000,
                "volumes": self.vols,
                "reason": "the migration did not finish after 2 boot(s); see journalctl -u docker-volumes-nocow",
            },
        )
        self.assertIn("Gave up after two boots", out)
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))
        self.assertEqual(len(self.fs.commands(self.notify)), 1)

    def test_a_record_that_cannot_be_written_changes_nothing_else(self) -> None:
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(nocow_adapters.NocowStateFiles, "write_outcome", side_effect=denied):
            out = self.give_up_boot()
        self.assertEqual(
            out,
            f"<4>{TAG}: cannot record the outcome (gave-up) in {self.outcome} for status: [Errno 13] Permission denied\n"
            f"<3>{TAG}: Gave up after two boots; see journalctl -u docker-volumes-nocow.\n",
        )
        self.assertFalse(os.path.exists(self.outcome))
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(read_attempts(self))
        self.assertEqual(len(self.fs.commands(self.notify)), 1)

    def test_a_give_up_whose_flag_stays_keeps_the_record_and_the_count(self) -> None:
        erofs = OSError(errno.EROFS, "Read-only file system")
        with mock.patch.object(nocow_adapters.NocowStateFiles, "remove_flag", side_effect=erofs):
            self.volume("cow")
            schedule(self)
            self.set_attempts("2")
            rc, _out = self.call(lambda: self.cli.service.execute(NocowCommand.MIGRATE))
        self.assertEqual(rc, 0)
        self.assertEqual(self.record()["outcome"], "gave-up")
        self.assertEqual(read_attempts(self), "2")
        self.assertTrue(os.path.exists(self.flag))

    def test_the_record_s_new_folder_is_0755_whatever_the_umask(self) -> None:
        # The install runs under sudo, which keeps the user's umask (077, say), and the state folder is
        # not there yet: the record's folder must still let cst doctor, as the user, read the record.
        # A makedirs of its own made it 0700 before write_atomic's ensure_dir saw it.
        shutil.rmtree(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        state = os.path.dirname(self.outcome)
        self.assertFalse(os.path.exists(state))
        old = os.umask(0o077)
        try:
            rc, _out = self.call(self.cli.mark)
        finally:
            os.umask(old)
        self.assertEqual(rc, 2)
        self.assertEqual(self.record()["outcome"], "not-scheduled")
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.outcome).st_mode), 0o644)

    def test_status_shows_a_give_up_without_the_flag(self) -> None:
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        self.gave_up()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(1_700_000_000))
        line = f"  migration gave up {when}: it did not finish; run cst btrfs install to try again\n"
        rc, out = self.call(self.cli.status)
        self.assertEqual(rc, 0)
        # Under the folder's line it belongs to (it came before it, as a detail of the section above).
        self.assertEqual(
            out,
            f"{self.vols}: new volumes NOCOW; 0 of 1 volume(s) fully NOCOW\n"
            "  1 with copy-on-write data (2 MiB), migration not scheduled\n" + line,
        )
        # The volumes folder gone: the record is still shown, under that line.
        shutil.rmtree(self.vols)
        self.assertEqual(self.call(self.cli.status), (0, f"{self.vols}: absent\n" + line))

    def test_status_hides_the_record_beside_the_flag(self) -> None:
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        self.gave_up()
        schedule(self)
        _rc, out = self.call(self.cli.status)
        self.assertNotIn("gave up", out)
        self.assertIn("migration at the next boot\n", out)
        self.assertNotIn("no boot asks", out)

    def test_status_of_a_record_it_cannot_read_or_of_a_newer_cst(self) -> None:
        # +C, copy-on-write data, no flag: no outcome is known, so the install hint stays (it was hidden
        # whenever the record's line was there, a problem line included).
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")

        def shown(problem: str) -> str:
            return (
                f"{self.vols}: new volumes NOCOW; 0 of 1 volume(s) fully NOCOW\n"
                "  1 with copy-on-write data (2 MiB), migration not scheduled\n"
                f"  no boot asks for it: run cst btrfs install\n  {problem}\n"
            )

        for text in (
            "not json",
            "[1]",
            '{"outcome": "gave-up"}',
            '{"outcome": "lost", "at": 1, "volumes": "v", "reason": "r"}',
            # A time past what the C library can show: OverflowError, and OSError EOVERFLOW (status
            # stopped with a traceback, or with "[Errno 75]" and exit 2).
            '{"v": 1, "outcome": "gave-up", "at": 100000000000000000000, "volumes": "v", "reason": "r"}',
            '{"v": 1, "outcome": "gave-up", "at": 1000000000000000000, "volumes": "v", "reason": "r"}',
        ):
            with self.subTest(text=text):
                os.makedirs(os.path.dirname(self.outcome), exist_ok=True)
                with open(self.outcome, "w") as f:
                    f.write(text)
                self.assertEqual(self.call(self.cli.status), (0, shown(f"cannot read {self.outcome}")))
        os.remove(self.outcome)
        os.mkdir(self.outcome)
        self.assertEqual(self.call(self.cli.status), (0, shown(f"cannot read {self.outcome}")))
        os.rmdir(self.outcome)
        self.gave_up(version=99)
        self.assertEqual(
            self.call(self.cli.status),
            (
                0,
                shown(
                    f"{self.outcome} is format 99 of the docker-nocow-outcome (this CST reads format 1 and older): "
                    "written by a newer CST"
                ),
            ),
        )

    def test_a_new_request_removes_the_record(self) -> None:
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        self.gave_up()
        rc, _out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.flag))
        self.assertFalse(os.path.exists(self.outcome))

    def test_mark_with_nothing_to_migrate_removes_the_record(self) -> None:
        self.volume("done", nocow=True)
        self.gave_up()
        self.assertEqual(self.call(self.cli.mark), (0, ""))
        self.assertFalse(os.path.exists(self.outcome))

    def test_the_end_of_a_migration_removes_the_record(self) -> None:
        # A record left beside the flag (a stop between give_up's write and its removals, then no counter).
        self.fs.set(self.vols, nocow=True)
        top = self.volume("cow")
        schedule(self)
        self.gave_up()
        _rc, out = self.call(self.cli.migrate)
        self.assertIn("all Docker volumes are NOCOW now", out)
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(self.outcome))

    def test_unmark_removes_the_record(self) -> None:
        self.gave_up()
        self.assertEqual(self.call(self.cli.unmark), (0, ""))
        self.assertFalse(os.path.exists(self.outcome))

    def test_a_removal_that_fails_is_one_line_and_the_request_stands(self) -> None:
        self.fs.set(self.vols, nocow=True)
        self.volume("cow")
        self.gave_up()
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(nocow_adapters.NocowStateFiles, "clear_outcome", side_effect=denied):
            rc, out = self.call(self.cli.mark)
        self.assertEqual(rc, 1)
        self.assertIn(f"<4>{TAG}: cannot remove the outcome record {self.outcome}: [Errno 13] Permission denied\n", out)
        self.assertTrue(os.path.exists(self.flag))
        self.assertTrue(self.fs.nocow(self.vols))


class PostponedLineTest(unittest.TestCase):
    """status's line of a postponed migration: a "last" that is no integer reads as "?"."""

    def test_last_must_be_an_integer(self) -> None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(3000))
        self.assertEqual(
            postponed_line({"boots": 2, "last": 3000, "reason": "r"}),
            f"  migration postponed at 2 boot(s) in a row, last {when}: r",
        )
        # A bool is no time (next_postponed and cst doctor refuse it too); before, True read as 1970.
        for last in (True, False, "3000", None):
            with self.subTest(last=last):
                self.assertEqual(
                    postponed_line({"boots": 2, "last": last, "reason": "r"}),
                    "  migration postponed at 2 boot(s) in a row, last ?: r",
                )


if __name__ == "__main__":
    unittest.main()
