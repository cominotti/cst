"""Unit tests for btrfs/docker-volumes-nocow (Docker volumes without copy-on-write).

The helper runs as root at boot and rewrites Docker volumes. These tests load
it as a module and never let it reach the machine:

- every path it writes or reads state from (VOLUMES, FLAG, ATTEMPTS, NOTIFY)
  points into a temporary folder of the test;
- the file attribute ioctls (FS_IOC_GETFLAGS / FS_IOC_SETFLAGS, under
  flags() and set_nocow()) go to a fake that keeps the flags of each inode in
  a dictionary, and refuses any file outside the temporary folder;
- the commands it runs (systemctl, chcon, cp, btrfs, btrfs-notify) go to a
  fake subprocess module; any other command fails the test. The fake cp
  copies the tree in Python and gives each new entry the NOCOW flag of its
  folder, as btrfs does when a file is created;
- os.sync() is replaced by a counter.

Loading the module only reads /etc/docker/daemon.json (ImportTest checks it).

A crash (a kill, a timeout or a power loss) is simulated by raising Crash, a
BaseException, at one step: migrate() catches Exception for each volume, so
Crash leaves the helper the way a dead process would. The next call of
migrate() is the next boot.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_docker_volumes_nocow -v
"""

import builtins
import contextlib
import importlib.machinery
import io
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "docker-volumes-nocow")


def load(name: str = "docker_volumes_nocow") -> ModuleType:
    # The helper has no .py extension, so it is loaded from its path.
    mod = ModuleType(name)
    mod.__file__ = TOOL
    importlib.machinery.SourceFileLoader(name, TOOL).exec_module(mod)
    return mod


M = load()

NOCOW = 0x00800000  # FS_NOCOW_FL, what chattr +C sets
OTHER = 0x00000080  # FS_NOATIME_FL: another attribute the helper must leave alone
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


class Crash(BaseException):
    """The process dies at this point (kill, timeout, power loss)."""


def tree(top: str) -> dict[str, tuple]:
    """Everything below top (and top's own mode), as comparable values."""
    out: dict[str, tuple] = {".": ("dir", stat.S_IMODE(os.lstat(top).st_mode))}
    for root, dirs, files in os.walk(top):
        for name in dirs + files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, top)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            elif stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    out[rel] = ("file", stat.S_IMODE(st.st_mode), f.read())
            else:
                out[rel] = ("other", stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode))
    return out


class FakeSystem:
    """The inode flags and the commands the helper runs, for one test."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.attrs: dict[tuple[int, int], int] = {}
        self.sets: list[str] = []  # paths whose flags the helper set (FS_IOC_SETFLAGS)
        self.calls: list[list[str]] = []
        self.docker_state = "inactive"
        self.systemctl_rc = 0
        self.systemctl_missing = False
        self.free = 1 << 50
        self.btrfs_out: str | None = None  # None: a normal `btrfs filesystem usage -b` answer
        self.chcon_rc = 0
        self.syncs = 0
        self.cp: Callable[[str, str], tuple[int, str]] = self.copy

    # ---- the ioctls -------------------------------------------------------

    def key(self, path: str) -> tuple[int, int]:
        st = os.lstat(path)
        return st.st_dev, st.st_ino

    def set(self, path: str, nocow: bool, other: int = 0) -> None:
        """Give path's inode these flags (the test's own setup, not recorded)."""
        self.attrs[self.key(path)] = (NOCOW if nocow else 0) | other

    def flags_of(self, path: str) -> int:
        return self.attrs.get(self.key(path), 0)

    def nocow(self, path: str) -> bool:
        return bool(self.flags_of(path) & NOCOW)

    def ioctl(self, fd: int, request: int, arg: bytes | int = 0, mutate_flag: bool = True) -> bytes | int:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target != self.root and not target.startswith(self.root + os.sep):
            raise AssertionError(f"ioctl on {target}, outside the test folder {self.root}")
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        if request == M.GET:
            return struct.pack("l", self.attrs.get(key, 0))
        if request == M.SET:
            if not isinstance(arg, bytes):
                raise AssertionError(f"SETFLAGS without a buffer: {arg!r}")
            self.attrs[key] = struct.unpack("l", arg)[0]
            self.sets.append(target)
            return 0
        raise AssertionError(f"unexpected ioctl request {request:#x}")

    # ---- the commands -----------------------------------------------------

    def run(self, args: list[str], **kw: object) -> subprocess.CompletedProcess:
        args = list(args)
        self.calls.append(args)
        text = bool(kw.get("text"))

        def done(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
            if text:
                return subprocess.CompletedProcess(args, rc, out, err)
            return subprocess.CompletedProcess(args, rc, out.encode(), err.encode())

        if args[0] == "systemctl":
            if args != ["systemctl", "show", "-P", "ActiveState", "docker.service"]:
                raise AssertionError(f"unexpected systemctl call: {args}")
            if self.systemctl_missing:
                raise FileNotFoundError(2, "No such file or directory", "systemctl")
            return done(self.systemctl_rc, self.docker_state + "\n" if self.systemctl_rc == 0 else "")
        if args[0] == "chcon":
            return done(self.chcon_rc)
        if args[0] == "btrfs":
            if args[:4] != ["btrfs", "filesystem", "usage", "-b"]:
                raise AssertionError(f"unexpected btrfs call: {args}")
            if self.btrfs_out is not None:
                return done(1, self.btrfs_out, "ERROR: not a btrfs filesystem")
            return done(
                0,
                "Overall:\n"
                "    Device size:\t\t 2000398934016\n"
                f"    Free (estimated):\t\t {self.free}\t(min: {self.free})\n"
                "    Data ratio:\t\t\t      2.00\n",
            )
        if args[0] == "cp":
            if args[:5] != ["cp", "-a", "--reflink=never", "--sparse=auto", "--"] or len(args) != 7:
                raise AssertionError(f"unexpected cp call: {args}")
            src, dst = args[5], args[6]
            if not (src.endswith("/.") and dst.endswith("/")):
                raise AssertionError(f"cp must copy the contents: {args}")
            rc, err = self.cp(src[:-2], dst[:-1])
            return done(rc, "", err)
        if args[0] == M.NOTIFY:
            return done()
        raise AssertionError(f"unexpected command: {args}")

    def sync(self) -> None:
        self.syncs += 1

    def commands(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]

    # ---- the fake cp ------------------------------------------------------

    def inherit(self, path: str) -> None:
        """A new file or folder: btrfs gives it +C when its folder has +C."""
        parent = self.nocow(os.path.dirname(path))
        self.attrs[self.key(path)] = NOCOW if parent else 0

    def copy(self, src: str, dst: str) -> tuple[int, str]:
        """`cp -a SRC/. DST/`: the contents and SRC's own mode and times."""
        self.copy_into(src, dst)
        st = os.lstat(src)
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        return 0, ""

    def copy_into(self, src: str, dst: str, stop_after: int | None = None) -> int:
        """Copy the entries of src into dst; with stop_after, raise Crash after that many."""
        n = 0
        for name in sorted(os.listdir(src)):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            st = os.lstat(s)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(s), d)
            elif stat.S_ISDIR(st.st_mode):
                os.mkdir(d)
                self.inherit(d)
                n += self.copy_into(s, d, None if stop_after is None else stop_after - n)
            elif stat.S_ISREG(st.st_mode):
                open(d, "wb").close()
                self.inherit(d)  # +C is set while the file is still empty
                shutil.copyfile(s, d)
            elif stat.S_ISFIFO(st.st_mode):
                os.mkfifo(d)
            else:
                raise AssertionError(f"fake cp: unexpected entry {s}")
            if not stat.S_ISLNK(st.st_mode):
                os.chmod(d, stat.S_IMODE(st.st_mode))
            os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
            n += 1
            if stop_after is not None and n >= stop_after:
                raise Crash(f"killed during the copy, after {n} entries")
        return n


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-nocow-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.vols = os.path.join(self.tmp, "var/lib/docker/volumes")
        self.flag = os.path.join(self.tmp, "etc/btrfs-setup/docker-nocow-pending")
        self.attempts = os.path.join(self.tmp, "var/lib/docker-volumes-nocow/attempts")
        self.notify = os.path.join(self.tmp, "usr/local/sbin/btrfs-notify")
        os.makedirs(self.vols)
        os.makedirs(os.path.dirname(self.flag))
        # Docker keeps its volume database next to the volumes: a file, not a volume.
        with open(os.path.join(self.vols, "metadata.db"), "wb") as f:
            f.write(b"bolt")
        self.fs = FakeSystem(self.tmp)
        for name, value in (
            ("VOLUMES", self.vols),
            ("FLAG", self.flag),
            ("ATTEMPTS", self.attempts),
            ("NOTIFY", self.notify),
            ("fcntl", SimpleNamespace(ioctl=self.fs.ioctl)),
            ("subprocess", SimpleNamespace(run=self.fs.run)),
        ):
            p = mock.patch.object(M, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(os, "sync", self.fs.sync)
        p.start()
        self.addCleanup(p.stop)
        # The helper opens the attempts file without closing it (bugs.md,
        # "attempts file left open"); CPython closes it at once, but the
        # ResourceWarning would fill the test output.
        quiet = warnings.catch_warnings()
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        warnings.filterwarnings("ignore", r"unclosed file <_io\.TextIOWrapper name='[^']*/attempts'", ResourceWarning)
        # Nothing of the helper may point outside the test folder.
        for path in (M.VOLUMES, M.FLAG, M.ATTEMPTS, M.NOTIFY):
            self.assertTrue(path.startswith(self.tmp + os.sep), path)

    # ---- set-up helpers ---------------------------------------------------

    def volume(self, name: str, nocow: bool = False, data: dict[str, bytes] | None = None) -> str:
        """VOLUMES/NAME/_data with DATA (or data); nocow: every entry has +C."""
        top = os.path.join(self.vols, name, "_data")
        os.makedirs(top)
        os.chmod(top, 0o750)
        self.fs.set(os.path.join(self.vols, name), nocow)
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
        for root, dirs, files in os.walk(top):
            for n in dirs + files:
                p = os.path.join(root, n)
                st = os.lstat(p)
                if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):
                    self.fs.set(p, nocow)
        return top

    def set_attempts(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.attempts), exist_ok=True)
        with open(self.attempts, "w") as f:
            f.write(text)

    def read_attempts(self) -> str | None:
        try:
            with open(self.attempts) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def schedule(self) -> None:
        """What `cst btrfs install` leaves when mark finds CoW data."""
        open(self.flag, "a").close()

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
            rc = M.main()
        return rc, out.getvalue(), err.getvalue()

    # ---- checks -----------------------------------------------------------

    def assert_all_nocow(self, top: str) -> None:
        self.assertTrue(self.fs.nocow(top), top)
        for root, dirs, files in os.walk(top):
            for n in dirs + files:
                p = os.path.join(root, n)
                st = os.lstat(p)
                if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):
                    self.assertTrue(self.fs.nocow(p), p)

    def assert_no_leftovers(self, name: str) -> None:
        self.assertEqual(sorted(os.listdir(os.path.join(self.vols, name))), ["_data"])


class ImportTest(unittest.TestCase):
    def test_import_only_reads_daemon_json(self) -> None:
        opened: list[tuple[str, str]] = []

        def fake_open(path: str, mode: str = "r", *a: object, **kw: object) -> io.StringIO:
            opened.append((str(path), mode))
            raise FileNotFoundError(2, "No such file or directory", path)

        with mock.patch.object(builtins, "open", fake_open):
            mod = load("docker_volumes_nocow_import_check")
        self.assertEqual(opened, [("/etc/docker/daemon.json", "r")])
        self.assertEqual(mod.VOLUMES, "/var/lib/docker/volumes")
        self.assertEqual(mod.FLAG, "/etc/btrfs-setup/docker-nocow-pending")
        self.assertEqual(mod.ATTEMPTS, "/var/lib/docker-volumes-nocow/attempts")

    def test_docker_root_reads_data_root(self) -> None:
        for text, want in (
            ('{"data-root": "/srv/docker", "cgroup-parent": "builds.slice"}', "/srv/docker/volumes"),
            ('{"cgroup-parent": "builds.slice"}', "/var/lib/docker/volumes"),
            ('{"data-root": ""}', "/var/lib/docker/volumes"),
            ("not json {", "/var/lib/docker/volumes"),
        ):
            with self.subTest(text=text):

                def fake_open(path: str, mode: str = "r", *a: object, text: str = text, **kw: object) -> io.StringIO:
                    self.assertEqual(path, "/etc/docker/daemon.json")
                    return io.StringIO(text)

                with mock.patch.object(builtins, "open", fake_open):
                    self.assertEqual(M.docker_root(), want)


class StatusTest(Base):
    def test_volumes_folder_absent(self) -> None:
        shutil.rmtree(self.vols)
        rc, out = self.call(M.status)
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
        rc, out = self.call(M.status)
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            f"{self.vols}: new volumes NOCOW; 1 of 3 volume(s) fully NOCOW\n"
            "  1 with copy-on-write data (2 MiB), migration not scheduled\n",
        )
        self.schedule()
        rc, out = self.call(M.status)
        self.assertIn("  1 with copy-on-write data (2 MiB), migration at the next boot\n", out)
        # status is read only: no flag set, no command run.
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(self.fs.attrs, before)
        self.assertEqual(self.fs.calls, [])

    def test_volumes_folder_without_nocow(self) -> None:
        self.volume("done", nocow=True)
        rc, out = self.call(M.status)
        self.assertEqual(out, f"{self.vols}: new volumes copy-on-write; 1 of 1 volume(s) fully NOCOW\n")


class MarkUnmarkTest(Base):
    def test_mark_absent(self) -> None:
        shutil.rmtree(self.vols)
        rc, out = self.call(M.mark)
        self.assertEqual((rc, out), (0, f"{self.vols}: absent (Docker not set up yet): nothing to mark\n"))
        self.assertEqual(self.fs.sets, [])

    def test_mark_fixes_what_it_can_in_place_and_reports_the_rest(self) -> None:
        self.fs.set(self.vols, False, other=OTHER)
        top = self.volume("cow")
        self.set_attempts("2")
        rc, out = self.call(M.mark)
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
        self.assertIsNone(self.read_attempts())  # a new request gets its two boots again
        self.assertFalse(os.path.exists(self.flag))  # the flag is install's job
        self.assertEqual(self.fs.calls, [])

    def test_mark_with_nothing_to_copy(self) -> None:
        self.volume("done", nocow=True)
        self.volume("empty", data={"a/b": b""})
        self.set_attempts("1")
        rc, out = self.call(M.mark)
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.fs.nocow(self.vols))
        self.assert_all_nocow(os.path.join(self.vols, "empty/_data"))
        self.assertIsNone(self.read_attempts())

    def test_unmark_clears_only_the_volumes_folder(self) -> None:
        self.fs.set(self.vols, True, other=OTHER)
        top = self.volume("done", nocow=True)
        rc, out = self.call(M.unmark)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.fs.flags_of(self.vols), OTHER)
        self.assert_all_nocow(top)  # existing volumes stay NOCOW
        self.assertEqual(self.fs.sets, [self.vols])

    def test_unmark_absent(self) -> None:
        shutil.rmtree(self.vols)
        self.assertEqual(self.call(M.unmark), (0, ""))
        self.assertEqual(self.fs.sets, [])


class MainTest(Base):
    def test_usage_and_default(self) -> None:
        rc, out, err = self.main("bogus")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage: docker-volumes-nocow status|mark|migrate|unmark", err)
        rc, out, err = self.main()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(f"{self.vols}: new volumes copy-on-write;"), out)

    def test_oserror_exit_codes(self) -> None:
        # A data-root that is not on btrfs: the flag ioctls fail.
        def no_flags(fd: int, request: int, arg: bytes | int = 0, mutate_flag: bool = True) -> bytes | int:
            raise OSError(25, "Inappropriate ioctl for device")

        self.volume("cow")
        with mock.patch.object(M, "fcntl", SimpleNamespace(ioctl=no_flags)):
            for cmd, want in (("status", 2), ("mark", 2), ("migrate", 0), ("unmark", 0)):
                with self.subTest(cmd=cmd):
                    rc, out, _ = self.main(cmd)
                    self.assertEqual(rc, want)
                    self.assertIn(f"<3>{TAG}: {self.vols}: [Errno 25] Inappropriate ioctl for device\n", out)


class MigrateTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)  # what install's mark left

    def test_happy_path(self) -> None:
        done = self.volume("done", nocow=True)
        done_ino = os.lstat(done).st_ino
        top = self.volume("cow")
        before = tree(top)
        old_ino = os.lstat(top).st_ino
        self.schedule()
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(top), before)  # same entries, modes, data and link targets
        self.assertNotEqual(os.lstat(top).st_ino, old_ino)  # a new copy
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertEqual(os.lstat(done).st_ino, done_ino)  # a NOCOW volume is not copied
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())
        new = top + ".nocow-new"
        self.assertEqual(self.fs.commands("chcon"), [["chcon", "--reference", top, new]])
        self.assertEqual(
            self.fs.commands("cp"), [["cp", "-a", "--reflink=never", "--sparse=auto", "--", top + "/.", new + "/"]]
        )
        self.assertEqual(self.fs.commands("btrfs"), [["btrfs", "filesystem", "usage", "-b", self.vols]])
        self.assertEqual(self.fs.syncs, 1)  # before the names are swapped
        lines = out.splitlines()
        self.assertEqual(lines[0], f"<5>{TAG}: rewriting 1 volume(s) without copy-on-write before Docker starts")
        self.assertRegex(lines[1], rf"^<6>{TAG}: cow: 3 file\(s\), 2 MiB in \d+s$")
        self.assertEqual(lines[2], f"<5>{TAG}: all Docker volumes are NOCOW now")
        self.assertEqual(len(lines), 3)

    def test_volumes_folder_absent(self) -> None:
        shutil.rmtree(self.vols)
        self.schedule()
        self.assertEqual(self.call(M.migrate), (0, ""))
        self.assertEqual(self.fs.calls, [])
        self.assertTrue(os.path.exists(self.flag))

    def test_not_scheduled_copies_nothing(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(M.migrate)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(tree(top), before)
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        self.assertEqual([c[0] for c in self.fs.calls], ["systemctl"])
        self.assertIsNone(self.read_attempts())

    def test_volumes_folder_lost_its_flag_schedules_and_migrates_at_once(self) -> None:
        # Docker installed after `cst btrfs install`: its volumes folder has no +C.
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertTrue(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertFalse(os.path.exists(self.flag))  # created, then removed once done
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)

    def test_flag_folder_missing_loses_the_request(self) -> None:
        # BUG (bugs.md, "FLAG folder missing"): the same case as above on a
        # machine where /etc/btrfs-setup does not exist (install only creates
        # it when mark found CoW data, or the snapshots setup did). open(FLAG)
        # fails, main() logs the OSError and exits 0; the volumes folder has
        # +C by then, so no later boot schedules the migration again. B2 fixes
        # it; this test pins today's behaviour.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"<3>{TAG}: {self.vols}: [Errno 2] No such file or directory: '{self.flag}'\n")
        self.assertTrue(self.fs.nocow(self.vols))
        self.assertFalse(os.path.exists(self.flag))
        # The next boot: nothing scheduled, nothing copied, the data stays CoW.
        rc, out, _ = self.main("migrate")
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        rc, out, _ = self.main("status")
        self.assertIn("migration not scheduled", out)

    def test_scheduled_with_nothing_left_to_copy(self) -> None:
        top = self.volume("empty", data={"a/b": b""})
        self.schedule()
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assert_all_nocow(top)  # fixed in place
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())
        self.assertIn(f"<5>{TAG}: rewriting 0 volume(s)", out)

    def check_refused(self, top: str, before: dict[str, tuple], ino: int, out: str, why: str) -> None:
        self.assertEqual(tree(top), before)
        self.assertEqual(os.lstat(top).st_ino, ino)  # the original, untouched
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        self.assert_no_leftovers("cow")
        self.assertTrue(os.path.exists(self.flag))  # tried again at the next boot
        self.assertEqual(self.read_attempts(), "1")
        self.assertIn(f"<3>{TAG}: cow: {why}; left as it was\n", out)
        self.assertNotIn("all Docker volumes are NOCOW now", out)
        self.assertEqual(self.fs.syncs, 0)

    def test_copy_check_refuses(self) -> None:
        entries = len(DATA) + len(LINKS) + len(FIFOS) + len(EMPTY_DIRS) + 3  # + db, db/base, db/wal
        size = sum(len(v) for v in DATA.values())

        def drops_a_file(src: str, dst: str) -> tuple[int, str]:
            self.fs.copy(src, dst)
            os.remove(os.path.join(dst, "README"))
            return 0, ""

        def truncates_a_file(src: str, dst: str) -> tuple[int, str]:
            self.fs.copy(src, dst)
            os.truncate(os.path.join(dst, "db/base/1"), 1000)
            return 0, ""

        def leaves_a_cow_file(src: str, dst: str) -> tuple[int, str]:
            self.fs.copy(src, dst)
            self.fs.set(os.path.join(dst, "db/wal/000001"), False)
            return 0, ""

        def leaves_a_cow_folder(src: str, dst: str) -> tuple[int, str]:
            self.fs.copy(src, dst)
            self.fs.set(os.path.join(dst, "cache"), False)
            return 0, ""

        for cp, why in (
            (
                drops_a_file,
                f"copy check failed (entries {entries} vs {entries - 1}, bytes {size} vs {size - 13}, CoW files 0)",
            ),
            (
                truncates_a_file,
                f"copy check failed (entries {entries} vs {entries}, bytes {size} vs {size - (1 << 20) + 1000}, "
                "CoW files 0)",
            ),
            (
                leaves_a_cow_file,
                f"copy check failed (entries {entries} vs {entries}, bytes {size} vs {size}, CoW files 1)",
            ),
            (
                leaves_a_cow_folder,
                f"copy check failed (entries {entries} vs {entries}, bytes {size} vs {size}, CoW files 0)",
            ),
        ):
            with self.subTest(cp=cp.__name__):
                self.setUp_again()
                top = self.volume("cow")
                before, ino = tree(top), os.lstat(top).st_ino
                self.schedule()
                self.fs.cp = cp
                rc, out = self.call(M.migrate)
                self.assertEqual(rc, 0)
                self.check_refused(top, before, ino, out, why)

    def setUp_again(self) -> None:
        """A fresh test folder for the next subTest."""
        self.doCleanups()
        self.setUp()

    def test_copy_fails(self) -> None:
        top = self.volume("cow")
        before, ino = tree(top), os.lstat(top).st_ino
        self.schedule()

        def full_disk(src: str, dst: str) -> tuple[int, str]:
            with open(os.path.join(dst, "partial"), "wb") as f:
                f.write(b"x")
            return 1, "cp: error writing 'db/base/1': No space left on device\n"

        self.fs.cp = full_disk
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.check_refused(top, before, ino, out, "copy failed: cp: error writing 'db/base/1': No space left on device")

    def test_one_failing_volume_does_not_stop_the_others(self) -> None:
        bad = self.volume("a-bad")
        good = self.volume("b-good")
        bad_before, good_before = tree(bad), tree(good)
        self.schedule()

        def fails_for_a(src: str, dst: str) -> tuple[int, str]:
            if "/a-bad/" in src:
                return 1, "cp: boom"
            return self.fs.copy(src, dst)

        self.fs.cp = fails_for_a
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(bad), bad_before)
        self.assertFalse(self.fs.nocow(os.path.join(bad, "README")))
        self.assertEqual(tree(good), good_before)
        self.assert_all_nocow(good)
        self.assert_no_leftovers("a-bad")
        self.assert_no_leftovers("b-good")
        self.assertIn(f"<3>{TAG}: a-bad: copy failed: cp: boom; left as it was\n", out)
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.read_attempts(), "1")

    def test_free_space(self) -> None:
        top = self.volume("cow")
        size = sum(len(v) for v in DATA.values())
        before = tree(top)
        self.schedule()
        # One byte short of the copy plus the 20 GiB margin: skipped.
        self.fs.free = size + 20 * GIB - 1
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<4>{TAG}: cow: not enough free space for a 2 MiB copy; skipped\n", out)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertEqual(tree(top), before)
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.read_attempts(), "1")
        # Exactly enough: copied.
        self.fs.free = size + 20 * GIB
        rc, out = self.call(M.migrate)
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(self.flag))

    def test_free_space_unknown_is_a_skip(self) -> None:
        self.volume("cow")
        self.schedule()
        self.fs.btrfs_out = ""
        rc, out = self.call(M.migrate)
        self.assertIn(f"<4>{TAG}: cow: not enough free space for a 2 MiB copy; skipped\n", out)
        self.assertEqual(self.fs.commands("cp"), [])

    def test_gives_up_after_two_boots(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        self.schedule()
        self.fs.cp = lambda src, dst: (1, "cp: boom")
        msg = "Gave up after two boots; see journalctl -u docker-volumes-nocow."
        for boot, attempts in ((1, "1"), (2, "2")):
            with self.subTest(boot=boot):
                rc, out = self.call(M.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(self.read_attempts(), attempts)
                self.assertTrue(os.path.exists(self.flag))
                self.assertNotIn(msg, out)
        # Third boot: no third try; the flag and the counter go, the user is told.
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"<3>{TAG}: {msg}\n")
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())
        self.assertEqual(len(self.fs.commands("cp")), 2)
        self.assertEqual(
            self.fs.commands(self.notify),
            [[self.notify, "-u", "normal", "-a", "btrfs-setup", "Docker volumes: NOCOW migration gave up", msg]],
        )
        self.assertEqual(tree(top), before)
        # Later boots: nothing scheduled, nothing tried, no second alert.
        self.assertEqual(self.call(M.migrate), (0, ""))
        self.assertEqual(len(self.fs.commands("cp")), 2)
        self.assertEqual(len(self.fs.commands(self.notify)), 1)

    def test_mark_gives_two_boots_again(self) -> None:
        self.volume("cow")
        self.schedule()
        self.set_attempts("2")
        self.call(M.mark)
        self.fs.cp = lambda src, dst: (1, "cp: boom")
        rc, out = self.call(M.migrate)
        self.assertEqual(self.read_attempts(), "1")  # tried, not given up
        self.assertEqual(len(self.fs.commands("cp")), 1)

    def test_unreadable_counter_counts_as_zero(self) -> None:
        self.volume("cow")
        self.schedule()
        self.set_attempts("garbage")
        self.fs.cp = lambda src, dst: (1, "cp: boom")
        self.call(M.migrate)
        self.assertEqual(self.read_attempts(), "1")


class DockerRunningTest(Base):
    """docker_active() reads ActiveState of docker.service only."""

    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        # A leftover of an interrupted copy: recover() would remove it.
        os.mkdir(self.top + ".nocow-new")
        self.before = tree(os.path.join(self.vols, "cow"))
        self.schedule()
        self.set_attempts("1")

    def test_running_postpones_without_touching_anything(self) -> None:
        for state in ("active", "activating", "reloading", "deactivating"):
            with self.subTest(state=state):
                self.fs.docker_state = state
                self.fs.calls.clear()
                rc, out = self.call(M.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(out, f"<4>{TAG}: Docker is running: migration left for the next boot\n")
                self.assertEqual(self.fs.calls, [["systemctl", "show", "-P", "ActiveState", "docker.service"]])
                self.assertEqual(self.fs.sets, [])
                self.assertEqual(tree(os.path.join(self.vols, "cow")), self.before)  # recover() not run either
                self.assertTrue(os.path.exists(self.flag))
                # Not counted as an attempt, and nobody is told: a migration
                # postponed at every boot stays pending without a notice
                # (design B2 makes that visible).
                self.assertEqual(self.read_attempts(), "1")
                self.assertEqual(self.fs.commands(self.notify), [])

    def test_stopped_migrates(self) -> None:
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                self.setUp_again()
                self.fs.docker_state = state
                rc, out = self.call(M.migrate)
                self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
                self.assert_all_nocow(self.top)
                self.assert_no_leftovers("cow")

    def setUp_again(self) -> None:
        self.doCleanups()
        self.setUp()

    def test_unknown_state_fails_open_today(self) -> None:
        # Today an answer that is not a running state counts as "not running":
        # systemctl failing (no manager on the bus) with empty output, or an
        # unknown word. Hardening in B2 (design triage 2): migrate only on
        # "inactive" or "failed" with exit code 0. This pins today's behaviour.
        for rc_, state in ((1, ""), (0, "maintenance")):
            with self.subTest(rc=rc_, state=state):
                self.setUp_again()
                self.fs.systemctl_rc, self.fs.docker_state = rc_, state
                rc, out = self.call(M.migrate)
                self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
                self.assert_all_nocow(self.top)

    def test_no_systemctl_postpones_through_main(self) -> None:
        # subprocess.run raises FileNotFoundError; main() logs it and exits 0
        # with nothing touched (attempts not counted).
        self.fs.systemctl_missing = True
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"<3>{TAG}: {self.vols}: [Errno 2] No such file or directory: 'systemctl'\n")
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(tree(os.path.join(self.vols, "cow")), self.before)
        self.assertEqual(self.read_attempts(), "1")


class RecoverTest(Base):
    """A crash at each step of migrate_one(), then the next boot."""

    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        self.vol = os.path.join(self.vols, "cow")
        self.before = tree(self.top)
        self.ino = os.lstat(self.top).st_ino
        self.schedule()

    def crash(self) -> None:
        with self.assertRaises(Crash):
            self.call(M.migrate)
        self.assertEqual(self.read_attempts(), "1")
        self.assertTrue(os.path.exists(self.flag))

    def next_boot_finishes(self, restored: bool) -> None:
        rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(f"<4>{TAG}: cow: restored the original after an interrupted migration\n" in out, restored, out)
        self.assertEqual(tree(self.top), self.before)
        self.assert_all_nocow(self.top)
        self.assert_no_leftovers("cow")
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())

    def test_crash_during_the_copy(self) -> None:
        self.fs.cp = lambda src, dst: (self.fs.copy_into(src, dst, stop_after=4), "")
        self.crash()
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.nocow-new"])
        self.assertEqual(os.lstat(self.top).st_ino, self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.fs.cp = self.fs.copy
        self.next_boot_finishes(restored=False)

    def test_crash_between_the_renames(self) -> None:
        real_rename = os.rename

        def rename(src: str, dst: str) -> None:
            if src.endswith(".nocow-new"):
                raise Crash("power loss between the two renames")
            real_rename(src, dst)

        with mock.patch.object(os, "rename", rename):
            self.crash()
        names = sorted(os.listdir(self.vol))
        self.assertEqual(len(names), 2, names)
        self.assertRegex(names[0], r"^_data\.cow-old-\d+$")
        self.assertEqual(names[1], "_data.nocow-new")
        old = os.path.join(self.vol, names[0])
        self.assertEqual(os.lstat(old).st_ino, self.ino)
        # recover() alone: the original back under its name, the copy gone.
        rc, out = self.call(M.recover)
        self.assertEqual(out, f"<4>{TAG}: cow: restored the original after an interrupted migration\n")
        self.assertEqual(os.lstat(self.top).st_ino, self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assert_no_leftovers("cow")
        self.assertFalse(self.fs.nocow(os.path.join(self.top, "README")))
        # The same state again, through the next boot: restored, then migrated.
        real_rename(self.top, old)
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
        real_rename = os.rename

        def rename(src: str, dst: str) -> None:
            if src.endswith(".nocow-new"):
                raise OSError(16, "Device or resource busy")
            real_rename(src, dst)

        with mock.patch.object(os, "rename", rename):
            rc, out = self.call(M.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<3>{TAG}: cow: [Errno 16] Device or resource busy; left as it was\n", out)
        self.assertEqual(os.lstat(self.top).st_ino, self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.nocow-new"])
        self.assertEqual(self.read_attempts(), "1")
        self.next_boot_finishes(restored=False)

    def test_recover_cases(self) -> None:
        self.call(M.recover)  # nothing to do: nothing changes
        self.assert_no_leftovers("cow")
        # _data there: every old copy and the new one go.
        for x in ("_data.cow-old-1700000000", "_data.cow-old-1700000100", "_data.nocow-new"):
            os.makedirs(os.path.join(self.vol, x, "sub"))
        rc, out = self.call(M.recover)
        self.assertEqual(out, "")  # removed without a log line
        self.assert_no_leftovers("cow")
        self.assertEqual(os.lstat(self.top).st_ino, self.ino)
        # _data missing, two old copies: the newest (by name) comes back.
        os.rename(self.top, os.path.join(self.vol, "_data.cow-old-1700000100"))
        os.makedirs(os.path.join(self.vol, "_data.cow-old-1700000000"))
        self.call(M.recover)
        self.assertEqual(os.lstat(self.top).st_ino, self.ino)
        self.assert_no_leftovers("cow")
        # _data missing and only the new copy: kept, nothing is deleted.
        os.rename(self.top, os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(M.recover), (None, ""))
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data.nocow-new"])

    def test_recover_skips_files_and_links(self) -> None:
        # metadata.db (a file) and a symlinked volume folder are not volumes.
        os.symlink(self.vol, os.path.join(self.vols, "link"))
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.call(M.recover)
        self.assertTrue(os.path.isfile(os.path.join(self.vols, "metadata.db")))
        self.assertTrue(os.path.islink(os.path.join(self.vols, "link")))
        self.assert_no_leftovers("cow")  # the real folder was cleaned once, by its own name


if __name__ == "__main__":
    unittest.main()
