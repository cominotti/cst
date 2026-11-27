# SPDX-License-Identifier: MPL-2.0
"""Fakes that several of btrfs's test modules share, one section per module they stand in for.

- any module: read and write, a text file of a test's folder (read: None for one that is not there);
- docker-volumes-nocow (test_docker_volumes_nocow and its fault tests): FakeSystem, the inode-flag
  ioctls, getxattr, the commands, os.sync and the fake cp; Crash, which its copy_into raises; the
  flag bits NOCOW and OTHER; entries, carries_flags and tree, to walk and compare a folder;
- systemd (scrub-limit's and iocost's fakes): SystemdState, what systemd has loaded of one unit
  (its files' text at the last reload that worked, whether they changed since) and whether the unit
  is enabled and active; one model, not a copy per package;
- scrub-limit (test_scrub_limit, test_scrub_limit_ports): ScrubFakeRunner, the installer and
  systemctl; RecordingInstallerRunner, the installer's text, remove-text and mark through the real
  InstallService and one install record; SystemdModel, the same and what systemd has loaded of the
  drop-in; core_text, a generated text of btrfs:core; (its fault tests, tests/mbt/mbt_scrub_rollback
  and test_scrub_limit_ports) ScrubWorld and FakeScrubSettings, FakeScrubDropin, FakeScrubSystemd and
  FakeScrubSysfs over it, the in-memory fakes of btrfs/scrub_limit/ports.py;
- iocost (its fault tests, test_iocost_ports and tests/mbt/mbt_iocost_apply): IocostWorld and
  FakeQosControl, FakeManagedDevices, FakeIocostSysfs, FakeUdev, FakeIocostSystemd,
  FakeIocostInstaller, FakeIocostUninstall and FakeIocostLock over it, the in-memory fakes of
  btrfs/iocost/ports.py; IOCOST_ENTRIES, btrfs:iocost's manifest entries;
- cache (test_cache, its fault tests, test_cache_ports and tests/mbt/mbt_cache_rollback): MountTable, the
  mounts and the kernel's rules for them (a bind's device and FSROOT, umount of the top mount, findmnt's
  JSON); FstabSystemdView, what systemd has loaded of /etc/fstab, and mount_units_listing, its answer to
  `systemctl show '*.mount'`; FakeMountRunner, findmnt, mount, umount and systemctl over them and a test
  root's /etc/fstab; CacheTestCtx, the host or toolbox the command runs in (commit-interval's tests use it
  too); the UUIDs the mounts carry; CacheWorld and FakeFstab, FakeCacheMountTable, FakeCacheSystemd,
  FakeCacheFolders, FakeCacheHostView and FakeCacheAccounts over it, the in-memory fakes of
  btrfs/cache/ports.py (over the same MountTable and FstabSystemdView);
- compress-force and commit-interval: (test_btrfs_compress, test_commit_interval) LayerHostState and
  LayerHost, CompressHostPort over the layers' texts, which can tear one step; (test_compress_faults)
  CompressWorld, CompressHostPort in memory (/etc/fstab, the staged and booted kernel arguments, the live
  mounts, the top-mount unit), and kernel_remount_options, a mount's options as the kernel shows them
  after a remount.

Not collected as a test module (unittest finds test*.py).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import posixpath
import shutil
import stat
import struct
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

from btrfs.cache.application.mounts import CacheMounts
from btrfs.cache.domain import (
    FINDMNT_COLUMNS,
    MIN_HUMAN_UID,
    CacheMountBinding,
    CacheMountUser,
    LiveMount,
    parse_findmnt_mounts,
)
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS
from btrfs.cache.ports import BindingPaths
from btrfs.compress.domain import KARGS_STILL_RUNNING, TopMountInfo
from btrfs.iocost.application import Iocost
from btrfs.iocost.domain import (
    IOCOST_COMPONENT_LOCK_NAME,
    IOCOST_LOCK,
    IOCOST_MANAGED_FILE,
    IOCOST_QOS_PATH,
    IOCOST_SERVICE,
    USEC_INITIALIZED_PROP,
    BlockDisk,
    UdevProperties,
)
from btrfs.scrub_limit.application import ScrubLimit
from btrfs.scrub_limit.domain import SCRUB_CONF_PATH, SCRUB_DROPIN_PATH
from btrfs.snapshot.domain import CACHE_TAG
from cst_core.adapters import nocow as core_nocow
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, Placement, record_file
from cst_core.domain.errors import Refused
from cst_core.domain.install import (
    INSTALL_MANIFEST,
    ArtifactKind,
    Component,
    GeneratedKind,
    InstallReportWord,
    Owner,
    select_entries,
)
from cst_core.domain.record import ComponentState
from cst_core.domain.units import parse_show_units
from cst_core.domain.values import ExitAndLines
from cst_core.installer import NAME as INSTALLER_NAME
from cst_core.ports import CommandFault, CommandResult, ReplacedNotSynced, WriteFailed
from tests.fault_injection import FaultPlan
from tests.test_install import MemoryTarget
from tests.test_install_record import MemoryRecord

# ---- files ---------------------------------------------------------------


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---- docker-volumes-nocow ------------------------------------------------

NOCOW = 0x00800000  # FS_NOCOW_FL, what chattr +C sets
OTHER = 0x00000080  # FS_NOATIME_FL: another attribute the helper must leave alone


class Crash(BaseException):
    """The process dies at this point (kill, timeout, power loss)."""


def entries(top: str) -> Iterator[tuple[str, os.stat_result]]:
    """(path, lstat) of every entry below top. Like the helper, os.walk lists
    a symlink to a folder but does not enter it."""
    for root, dirs, files in os.walk(top):
        for name in dirs + files:
            p = os.path.join(root, name)
            yield p, os.lstat(p)


def carries_flags(st: os.stat_result) -> bool:
    """Folders and regular files: the entries whose NOCOW flag matters."""
    return stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)


def tree(top: str) -> dict[str, tuple[object, ...]]:
    """Everything below top (and top's own mode), as comparable values."""
    out: dict[str, tuple[object, ...]] = {".": ("dir", stat.S_IMODE(os.lstat(top).st_mode))}
    for p, st in entries(top):
        rel = os.path.relpath(p, top)
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
        self.labels: dict[tuple[int, int], bytes] = {}  # security.selinux of each inode; none by default
        self.sets: list[str] = []  # paths whose flags the helper set (FS_IOC_SETFLAGS)
        self.calls: list[list[str]] = []
        self.docker_state = "inactive"
        self.systemctl_rc = 0
        self.systemctl_out_on_error = False  # a failing systemctl still prints docker_state
        self.free = 1 << 50
        self.btrfs_out: str | None = None  # None: a normal `btrfs filesystem usage -b` answer
        self.chcon_rc = 0
        self.syncs = 0
        self.cp: Callable[[str, str], tuple[int, str]] = self.copy
        self.notify = ""  # btrfs-notify's path in the test folder
        self.missing: set[str] = set()  # programs that are not installed

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

    def sets_below(self, top: str) -> list[str]:
        """The paths at or below top whose flags the helper set."""
        return [p for p in self.sets if p == top or p.startswith(top + os.sep)]

    def ioctl(self, fd: int, request: int, arg: bytes | int = 0, mutate_flag: bool = True) -> bytes | int:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target != self.root and not target.startswith(self.root + os.sep):
            raise AssertionError(f"ioctl on {target}, outside the test folder {self.root}")
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        if request == core_nocow.FS_IOC_GETFLAGS:
            return struct.pack("l", self.attrs.get(key, 0))
        if request == core_nocow.FS_IOC_SETFLAGS:
            if not isinstance(arg, bytes):
                raise AssertionError(f"SETFLAGS without a buffer: {arg!r}")
            self.attrs[key] = struct.unpack("l", arg)[0]
            self.sets.append(target)
            return 0
        raise AssertionError(f"unexpected ioctl request {request:#x}")

    # ---- SELinux labels (os.getxattr) ---------------------------------------

    def label(self, path: str, value: bytes | None) -> None:
        """Give path's inode this label (None: no label)."""
        if value is None:
            self.labels.pop(self.key(path), None)
        else:
            self.labels[self.key(path)] = value

    def label_of(self, path: str) -> bytes | None:
        return self.labels.get(self.key(path))

    def getxattr(self, path: str, attribute: str, *, follow_symlinks: bool = True) -> bytes:
        if attribute != "security.selinux":
            raise AssertionError(f"unexpected xattr {attribute} of {path}")
        if follow_symlinks:
            raise AssertionError(f"getxattr follows the link {path}")
        if path != self.root and not path.startswith(self.root + os.sep):
            raise AssertionError(f"getxattr on {path}, outside the test folder {self.root}")
        value = self.labels.get(self.key(path))
        if value is None:
            raise OSError(61, "No data available", path)  # ENODATA
        return value

    def copy_label(self, src: str, dst: str) -> None:
        """What cp -a and chcon --reference do with the label."""
        self.label(dst, self.label_of(src))

    # ---- the commands -----------------------------------------------------

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        """CommandRunnerPort.run: each command the helper may run, answered by the fake."""
        args = list(argv)
        self.calls.append(args)
        not_found = self.not_found(args, kw)
        if not_found is not None:
            return not_found

        def done(rc: int = 0, out: str = "", err: str = "") -> CommandResult:
            return CommandResult(rc, out, err, argv=tuple(args))

        if args[0] == "systemctl":
            if args != ["systemctl", "show", "-P", "ActiveState", "docker.service"]:
                raise AssertionError(f"unexpected systemctl call: {args}")
            answers = self.systemctl_rc == 0 or self.systemctl_out_on_error
            return done(self.systemctl_rc, self.docker_state + "\n" if answers else "")
        if args[0] == "chcon":
            if args[1:2] != ["--reference"] or len(args) != 4:
                raise AssertionError(f"unexpected chcon call: {args}")
            if self.chcon_rc == 0:
                self.copy_label(args[2], args[3])
                return done()
            return done(self.chcon_rc, "", f"chcon: failed to change context of '{args[3]}': Operation not permitted\n")
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
        if args[0] == self.notify:
            return done()
        raise AssertionError(f"unexpected command: {args}")

    def not_found(self, args: list[str], kw: Mapping[str, object]) -> CommandResult | None:
        """Checks the options of a command, and answers for one that is missing (None: it is installed).

        stdin ours; systemctl's answer decoded strictly, cp's, chcon's and btrfs's leniently
        (U+FFFD); btrfs-notify's output was never decoded."""
        want = (
            {"timeout": 60, "new_session": True}
            if args[0] == self.notify
            else {"stdin": "inherit", "decode_errors": "strict" if args[0] == "systemctl" else "replace"}
        )
        if kw != want:
            raise AssertionError(f"{args[0]} run with {kw}, not {want}")
        if args[0] not in self.missing:
            return None
        missing = FileNotFoundError(2, "No such file or directory", args[0])
        return CommandResult(127, "", str(missing), argv=tuple(args), fault=CommandFault.MISSING, cause=missing)

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
        """`cp -a SRC/. DST/`: the contents and SRC's own mode, times and label."""
        self.copy_into(src, dst)
        self.copy_label(src, dst)
        st = os.lstat(src)
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        return 0, ""

    def copy_then(self, change: Callable[[str], object]) -> Callable[[str, str], tuple[int, str]]:
        """A cp that copies, then spoils the copy with change(dst), and reports success."""

        def cp(src: str, dst: str) -> tuple[int, str]:
            self.copy(src, dst)
            change(dst)
            return 0, ""

        return cp

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
            self.copy_label(s, d)
            n += 1
            if stop_after is not None and n >= stop_after:
                raise Crash(f"killed during the copy, after {n} entries")
        return n


# ---- systemd (scrub-limit and iocost) -------------------------------------


class SystemdState:
    """What systemd has of one unit, for the fakes of every package that reloads it: the text of its
    files at the last daemon-reload that worked (loaded), whether a write or a removal of them came since
    (dirty: what NeedDaemonReload follows), and whether the unit is enabled and active."""

    def __init__(self, *, loaded: str | None = None, dirty: bool = False) -> None:
        self.loaded = loaded
        self.dirty = dirty
        self.enabled = False
        self.active = False

    def changed(self) -> None:
        """A write or a removal of the unit's files: systemd is behind until the next reload."""
        self.dirty = True

    def reloaded(self, text: str | None) -> None:
        """A daemon-reload that worked: systemd has text, as the files are now."""
        self.loaded, self.dirty = text, False


# ---- scrub-limit ---------------------------------------------------------


class ScrubFakeRunner:
    def __init__(self, *, installer_root: str = "") -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._installer_root = installer_root
        # a part of the command line ("systemctl daemon-reload", "installer.py text") whose calls exit 1
        self.fail_on: set[str] = set()
        # the value `systemctl show -p NeedDaemonReload UNIT` prints after "NeedDaemonReload=" ("": none)
        self.need_daemon_reload = ""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        **_kw: object,
    ) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        self.inputs.append(input_text)
        if any(part in " ".join(cmd) for part in self.fail_on):
            return CommandResult(1, "", "Access denied", argv=tuple(cmd))
        if "installer.py" in " ".join(cmd):
            return self._installer(cmd, input_text)
        if cmd[:4] == ["systemctl", "show", "-p", "NeedDaemonReload"]:
            return CommandResult(0, f"NeedDaemonReload={self.need_daemon_reload}", "", argv=tuple(cmd))
        return CommandResult(0, "", "", argv=tuple(cmd))

    def _installer(self, cmd: list[str], input_text: str | None) -> CommandResult:
        if self._installer_root:
            real = subprocess.run(
                [sys.executable, "-I", "-B", cmd[3], cmd[4], "--root", self._installer_root, *cmd[5:]],
                input=input_text,
                capture_output=True,
                text=True,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                check=False,
            )
            return CommandResult(real.returncode, real.stdout, real.stderr, argv=tuple(cmd))
        if "text" in cmd:
            dest = cmd[cmd.index("text") + 1]
            existed = os.path.exists(dest)
            old = None
            if existed:
                with open(dest, encoding="utf-8") as fh:
                    old = fh.read()
            if old == input_text:
                return CommandResult(0, f"unchanged: {dest}\n", "", argv=tuple(cmd))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(input_text or "")
            return CommandResult(0, f"written:   {dest}\n", "", argv=tuple(cmd))
        if "remove-text" in cmd:
            dest = cmd[cmd.index("remove-text") + 1]
            if os.path.exists(dest):
                os.remove(dest)
                return CommandResult(0, f"removed:   {dest}\n", "", argv=tuple(cmd))
            return CommandResult(0, "", "", argv=tuple(cmd))
        return CommandResult(0, "", "", argv=tuple(cmd))


def core_text(path: str, record: str) -> Placement:
    """A generated text of btrfs:core at path, listed in record (as installer.py text --component writes it)."""
    return Placement(
        path=path,
        dest=path,
        record=record,
        component=Component.BTRFS_CORE.value,
        src=None,
        kind=GeneratedKind.TEXT,
        owner=Owner.ROOT,
        mode=0o644,
    )


class RecordingInstallerRunner(ScrubFakeRunner):
    """installer.py text and remove-text as the installer runs them as root: the files written and
    removed, and their lines and btrfs:core's state in one install record (a write sets it pending,
    unless --keep-state keeps the state the record has). scrub-limit runs no other installer command: any
    other (a mark of btrfs:core, a text-plan, a forget-text) fails the test."""

    def __init__(self, record: str) -> None:
        super().__init__()
        self.record = record
        self.svc = InstallService(LocalInstallTarget(), lambda _e: b"", record=JsonInstallRecord())

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        joined = " ".join(cmd)
        if "installer.py" not in joined or any(part in joined for part in self.fail_on):
            return super().run(argv, input_text=input_text, **kw)
        self.calls.append(cmd)
        word = cmd[4]
        if word == "mark":
            raise AssertionError(f"scrub-limit ran the installer's mark: {cmd}")
        place = core_text(cmd[5], self.record)
        if word == "text":
            os.makedirs(os.path.dirname(place.path), exist_ok=True)
            data = (input_text or "").encode()
            written = self.svc.put(place, data, as_root=False, keep_state="--keep-state" in cmd)
            word_out = InstallReportWord.WRITTEN if written else InstallReportWord.UNCHANGED
            return CommandResult(0, word_out.line(place.path) + "\n", "", argv=tuple(cmd))
        if word == "remove-text":
            return CommandResult(0, "".join(f"{line}\n" for line in self.svc.remove([place])), "", argv=tuple(cmd))
        raise AssertionError(f"scrub-limit ran an installer command this fake does not model: {cmd}")


class SystemdModel(RecordingInstallerRunner):
    """The installer (its writes and their record through the real InstallService) and systemctl, and
    what systemd has loaded of the drop-in."""

    def __init__(self, dropin: str, record: str, *, loaded: str | None = None, behind: bool = False) -> None:
        super().__init__(record)
        self.dropin = dropin
        # dirty: a write or a removal of the drop-in since the last reload that worked
        self.systemd = SystemdState(loaded=loaded, dirty=behind)

    @property
    def loaded(self) -> str | None:
        """The drop-in's text at the last daemon-reload that worked."""
        return self.systemd.loaded

    @property
    def dirty(self) -> bool:
        """A write or a removal of the drop-in since the last reload that worked."""
        return self.systemd.dirty

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        self.need_daemon_reload = "yes\n" if self.dirty else "no\n"
        r = super().run(argv, input_text=input_text, **kw)
        if "installer.py" in " ".join(cmd) and r.stdout.startswith(("written:", "removed:")):
            self.systemd.changed()
        if cmd == ["systemctl", "daemon-reload"] and r.returncode == 0:
            self.systemd.reloaded(read(self.dropin))
        return r


class FaultPoints:
    """Every mutating call of a world's port fakes as one named point: recorded in calls, then run through
    plan (tests/fault_injection.py) and failed by fail() when one is armed there; answered lists the
    points where an armed fail() answered (so a test can tell a fault that fired from one that did not)."""

    def __init__(self) -> None:
        self.calls: list[str] = []  # every mutating call, by its point name
        self.plan = FaultPlan()
        self.failing: dict[str, tuple[OSError, int | None]] = {}
        self.answered: list[str] = []

    def fail(self, point: str, err: OSError, *, times: int | None = None) -> None:
        """The next times calls of point fail with err (None: every one)."""
        self.failing[point] = (err, times)

    def step[T](self, point: str, effect: Callable[[], T], failed: Callable[[OSError], T] | None = None) -> T:
        """One mutating call: recorded, then run through plan, its effect failed by fail() when one is armed
        there (so plan records it too, and a process the plan has killed runs it no more)."""
        self.calls.append(point)
        err, left = self.failing.get(point, (None, 0))
        if err is None or left == 0:
            return self.plan.step(point, effect, failed)
        armed = err

        def answers() -> None:
            self.failing[point] = (armed, None if left is None else left - 1)
            self.answered.append(point)

        if failed is None:
            # The failure is recorded inside the plan's step and raised only after it returned, so a KILL
            # armed at this call fires after the failure was recorded, as at any other call.
            self.plan.step(point, answers)
            raise armed

        def fails() -> T:
            answers()
            return failed(armed)

        return self.plan.step(point, fails, failed)


class ScrubWorld(FaultPoints):
    """What scrub-limit's ports see, in memory, for the use cases of btrfs/scrub_limit/application.py:
    snapshots.conf and its folder, the drop-in (in an in-memory install target, written and removed
    through the real InstallService with an in-memory install record, as installer.py text --keep-state and
    remove-text do) and its folder, and what systemd has loaded of it.

    loaded is the drop-in's text at the last daemon-reload that worked; dirty, a write or a removal of the
    drop-in since (NeedDaemonReload follows writes and the list of drop-ins, not their text, as
    SystemdModel models it). Every mutating call of a port is one named point (`calls`), through plan
    (tests/fault_injection.py) and through fail(): snapshots.conf's write is three, "settings folder" (its
    folder made, on every write), "settings replace" (the text in place) and "settings sync" (the folder's
    sync after the rename: a failure there leaves the new text in place); "settings remove", "settings
    rmdir", "drop-in text", "drop-in remove-text", "drop-in rmdir" and "systemctl daemon-reload". A failed
    rmdir is no error (the port's contract; as os.rmdir, one of a folder that is not there or not empty
    raises inside the call, before a kill armed there fires), a failed installer call or daemon-reload is an
    exit 1 with the error on stderr, a failed settings call raises it."""

    def __init__(
        self,
        *,
        conf_path: str = SCRUB_CONF_PATH,
        dropin_path: str = SCRUB_DROPIN_PATH,
        record: str = "/var/lib/cst/installed.json",
    ) -> None:
        super().__init__()
        self.conf_path = conf_path
        self.dropin_path = dropin_path
        self.record = record
        self.conf: str | None = None
        self.conf_folder = False
        self.conf_unreadable = False
        self.dropin_folder = False
        self.dropin_unreadable = False
        self.systemd = SystemdState()  # what systemd has loaded of the drop-in (loaded, dirty)
        self.show_fails = False  # systemctl cannot say whether systemd loaded the files
        self.live: tuple[str, ...] = ()  # sysfs's lines (ScrubSysfsPort)
        self.reload_timeouts: list[float | None] = []  # the time limit of each daemon-reload asked for
        self.built_for: list[bool] = []  # the read_only of each use case build() gave
        self.target = MemoryTarget()
        self.records = MemoryRecord()
        self.svc = InstallService(self.target, lambda _e: b"", record=self.records)

    @property
    def loaded(self) -> str | None:
        """The drop-in's text at the last daemon-reload that worked."""
        return self.systemd.loaded

    @loaded.setter
    def loaded(self, text: str | None) -> None:
        self.systemd.loaded = text

    @property
    def dirty(self) -> bool:
        """A write or a removal of the drop-in since the last reload that worked."""
        return self.systemd.dirty

    @dirty.setter
    def dirty(self, behind: bool) -> None:
        self.systemd.dirty = behind

    @property
    def dropin(self) -> str | None:
        """The drop-in's text (None: not there)."""
        held = self.target.files.get(self.dropin_path)
        return None if held is None else held[0].decode()

    def put_conf(self, text: str | None) -> None:
        """snapshots.conf as a test's start state (None: not there, its folder neither)."""
        self.conf, self.conf_folder = text, text is not None

    def put_dropin(self, text: str | None) -> None:
        """The drop-in on disk as a test's start state, not recorded and not loaded (None: not there)."""
        if text is None:
            self.target.files.pop(self.dropin_path, None)
        else:
            self.target.files[self.dropin_path] = (text.encode(), 0o644)
        self.dropin_folder = text is not None

    def core_state(self) -> ComponentState | None:
        """btrfs:core's state in the install record (None: no record, or none for it)."""
        rec = self.records.read(self.record)
        return None if rec is None else rec.components.get(Component.BTRFS_CORE.value)

    def core_has_a_file(self) -> bool:
        rec = self.records.read(self.record)
        return rec is not None and any(f.component == Component.BTRFS_CORE.value for f in rec.files.values())

    def install_core(self, *, done: bool) -> None:
        """btrfs:core as an install leaves it: a seed text and the drop-in (when there) recorded, then done
        (or still pending)."""
        seed = posixpath.join(posixpath.dirname(self.record), "core-seed")
        self.svc.put(core_text(seed, self.record), b"seed\n", as_root=False)
        if self.dropin is not None:
            self.svc.put(core_text(self.dropin_path, self.record), self.dropin.encode(), as_root=False)
        if done:
            self.svc.mark(self.record, Component.BTRFS_CORE.value)

    def build(self) -> Callable[[bool], ScrubLimit]:
        """What run_scrub_limit_cli's build takes: this world's use cases, whatever read_only it is asked
        for, which built_for records."""
        return lambda read_only: self._built(read_only=read_only)

    def _built(self, *, read_only: bool) -> ScrubLimit:
        self.built_for.append(read_only)
        return self.service()

    def service(self) -> ScrubLimit:
        """The use cases over this world's ports."""
        return ScrubLimit(
            settings=FakeScrubSettings(self),
            dropin=FakeScrubDropin(self),
            systemd=FakeScrubSystemd(self),
            sysfs=FakeScrubSysfs(self),
        )


def _rmdir(*, exists: bool, empty: bool) -> None:
    """What os.rmdir raises for a folder that is not there or not empty."""
    if not exists:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
    if not empty:
        raise OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY))


def _installer_failed(err: OSError) -> tuple[int, list[str], str]:
    return 1, [], err.strerror or str(err)


class FakeScrubSettings:
    """ScrubSettingsPort over a ScrubWorld."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.conf_path

    def read(self) -> tuple[str | None, bool]:
        w = self.world
        return (None, True) if w.conf_unreadable else (w.conf, False)

    def folder_exists(self) -> bool:
        return self.world.conf_folder

    def write(self, text: str) -> None:
        w = self.world

        def made() -> None:
            w.conf_folder = True

        def replaced() -> None:
            w.conf = text

        w.step("settings folder", made)
        w.step("settings replace", replaced)
        w.step("settings sync", lambda: None)

    def remove(self) -> None:
        w = self.world

        def removed() -> None:
            w.conf = None

        w.step("settings remove", removed)

    def remove_folder_if_empty(self) -> None:
        w = self.world

        def rmdir() -> None:
            _rmdir(exists=w.conf_folder, empty=w.conf is None)
            w.conf_folder = False

        with contextlib.suppress(OSError):  # as the adapter's: a folder not there, or not empty, stays
            w.step("settings rmdir", rmdir)


class FakeScrubDropin:
    """ScrubDropinPort over a ScrubWorld: the installer's text --keep-state and remove-text through its
    InstallService, with the installer's report lines."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.dropin_path

    def read(self) -> tuple[str | None, bool]:
        w = self.world
        return (None, True) if w.dropin_unreadable else (w.dropin, False)

    def write(self, text: str) -> tuple[int, list[str], str]:
        w = self.world

        def write_text() -> tuple[int, list[str], str]:
            place = core_text(w.dropin_path, w.record)
            written = w.svc.put(place, text.encode(), as_root=False, keep_state=True)
            w.dropin_folder = True
            if written:
                w.systemd.changed()
            word = InstallReportWord.WRITTEN if written else InstallReportWord.UNCHANGED
            return 0, [word.line(w.dropin_path)], ""

        return w.step("drop-in text", write_text, _installer_failed)

    def remove(self) -> tuple[int, list[str], str]:
        w = self.world

        def remove_text() -> tuple[int, list[str], str]:
            lines = list(w.svc.remove([core_text(w.dropin_path, w.record)]))
            if lines:
                w.systemd.changed()
            return 0, lines, ""

        return w.step("drop-in remove-text", remove_text, _installer_failed)

    def remove_folder_if_empty(self) -> None:
        w = self.world

        def rmdir() -> None:
            _rmdir(exists=w.dropin_folder, empty=w.dropin is None)
            w.dropin_folder = False

        with contextlib.suppress(OSError):  # as the adapter's: a folder not there, or not empty, stays
            w.step("drop-in rmdir", rmdir)


class FakeScrubSystemd:
    """ScrubSystemdPort over a ScrubWorld."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    def need_daemon_reload(self) -> bool | None:
        w = self.world
        return None if w.show_fails else w.dirty

    def daemon_reload(self, *, timeout: float | None) -> CommandResult:
        w = self.world
        argv = ("systemctl", "daemon-reload")

        def reload() -> CommandResult:
            w.reload_timeouts.append(timeout)
            w.systemd.reloaded(w.dropin)
            return CommandResult(0, "", "", argv=argv)

        def failed(err: OSError) -> CommandResult:
            w.reload_timeouts.append(timeout)  # asked for, and it failed
            return CommandResult(1, "", f"{err.strerror}\n", argv=argv)

        return w.step("systemctl daemon-reload", reload, failed)


class FakeScrubSysfs:
    """ScrubSysfsPort over a ScrubWorld."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    def live_limits(self) -> tuple[str, ...]:
        return self.world.live


# ---- iocost --------------------------------------------------------------

IOCOST_ENTRIES: Final = tuple(select_entries(INSTALL_MANIFEST, component=Component.BTRFS_IOCOST))
UDEV_PROCESSED: Final = UdevProperties({USEC_INITIALIZED_PROP: "1234"})  # udev processed it, no hwdb solution


class IocostWorld(FaultPoints):
    """What iocost's ports see, in memory, for the use cases of btrfs/iocost/application.py: the kernel's
    io.cost.qos (one line per device, a write replacing its device's line), the cgroup controllers,
    io.cost.model and the block disks, udev's answer for each disk and whether udevd runs, the
    managed-devices set (or a folder at its path), btrfs:iocost's files in an in-memory install target and
    its state in an in-memory install record (installed, marked and removed through the real
    InstallService, as installer.py install and mark and the in-process removal do; release: the CST whose
    payload an install writes, so a new one rewrites every file), what systemd has loaded of the unit
    (SystemdState, shared with scrub-limit's fakes), the rule as udev loaded it, and the two locks.

    Every mutating call is one point (FaultPoints): "installer install", "installer mark done",
    "managed save", "qos MAJ:MIN ..." (the line), "systemd daemon-reload", "systemd enable --now",
    "systemd disable --now", "udev reload-rules", "uninstall mark pending|done" and "uninstall remove". A
    failed command is an exit 1 with the error on stderr; a failed save, write, mark or removal raises it.
    The fakes check the locks as the protocol must hold them (an AssertionError names the call): the
    io.cost.qos lock at every io.cost.qos write and save of the managed set; the component lock at every
    install, mark and removal of btrfs:iocost and at enable --now, which also needs the io.cost.qos lock
    free (the service it starts takes it). Reads are not points; each read gives a new object, never the
    one the world holds, so a use case that compares by identity is caught."""

    def __init__(self, *, install_root: str = "") -> None:
        super().__init__()
        self.qos_path = IOCOST_QOS_PATH
        self.managed_path = IOCOST_MANAGED_FILE
        self.lock_paths = {
            "qos": IOCOST_LOCK,
            "component": posixpath.join(posixpath.dirname(IOCOST_LOCK), IOCOST_COMPONENT_LOCK_NAME),
        }
        self.qos: dict[str, str] | None = {}  # MAJ:MIN -> its line; None: no io.cost.qos (no blk-iocost)
        self.qos_refusal: OSError | None = None  # the kernel refuses every write with it
        self.controllers: str | None = "cpuset cpu io memory pids"
        self.model = ""
        self.disks: list[BlockDisk] = []
        self.udev_answers: dict[str, UdevProperties] = {}  # by disk name; UDEV_PROCESSED by default
        self.udevd = True
        self.managed: set[str] = set()
        self.managed_folder = False  # a folder where the managed-devices file belongs
        self.systemd = SystemdState()  # loaded: the unit file's text at its last daemon-reload that worked
        self.show_fails = False  # systemctl cannot say what the unit is
        self.udev_loaded: str | None = None  # the rule's text as udev loaded it at its last reload
        self.installer_available = True
        self.plan_answer: list[str] | None = []  # the installer plan's changes (None: it cannot tell)
        self.release = 0
        self.target = MemoryTarget()
        self.records = MemoryRecord()
        self.req = InstallRequest(root=install_root, as_root=True)
        self.record = record_file(Owner.ROOT, None, install_root)
        self.svc = InstallService(self.target, lambda e: f"{e.dest} {self.release}\n".encode(), record=self.records)
        self.held = {"qos": False, "component": False}
        self.busy: set[str] = set()  # the locks another run holds
        self.lock_errors: dict[str, OSError] = {}  # the locks whose folder or file cannot be made
        self.events: list[str] = []  # what worked, in order: install, reload, enable, udev, mark, disable, ...
        self.before_mark: Callable[[], None] | None = None  # runs before a mark done of btrfs:iocost

    # ---- the files ---------------------------------------------------------------------------------
    def dest_path(self, dest: str) -> str:
        """Where btrfs:iocost's file dest is in the target (under the install root)."""
        entry = next(e for e in IOCOST_ENTRIES if e.dest == dest)
        return self.svc.placement(entry, self.req).path

    @property
    def unit_path(self) -> str:
        return self.dest_path(next(e.dest for e in IOCOST_ENTRIES if e.kind == ArtifactKind.UNIT))

    @property
    def rule_path(self) -> str:
        return self.dest_path(next(e.dest for e in IOCOST_ENTRIES if e.dest.endswith(".rules")))

    def file(self, path: str) -> str | None:
        """A file's text in the target (None: not there)."""
        held = self.target.files.get(path)
        return None if held is None else held[0].decode()

    def in_place(self) -> list[str]:
        """btrfs:iocost's destinations whose file is in the target."""
        return [e.dest for e in IOCOST_ENTRIES if self.dest_path(e.dest) in self.target.files]

    def state(self) -> ComponentState | None:
        """btrfs:iocost's state in the install record (None: no record, or none for it)."""
        rec = self.records.read(self.record)
        return None if rec is None else rec.components.get(Component.BTRFS_IOCOST.value)

    def recorded(self) -> set[str]:
        """The destinations the install record lists for btrfs:iocost."""
        rec = self.records.read(self.record)
        return (
            set() if rec is None else {d for d, f in rec.files.items() if f.component == Component.BTRFS_IOCOST.value}
        )

    def qos_text(self) -> str | None:
        """io.cost.qos as the kernel shows it (None: not there)."""
        return None if self.qos is None else "".join(f"{line}\n" for line in self.qos.values())

    # ---- start states ------------------------------------------------------------------------------
    def put_disk(
        self, name: str, devno: str, *, rotational: bool = False, scheduler: str = "none", model: str = ""
    ) -> None:
        """A whole block disk the scan finds."""
        self.disks.append(BlockDisk(name=name, devno=devno, rotational=rotational, scheduler=scheduler, model=model))

    def install(self, *, done: bool) -> None:
        """btrfs:iocost as an apply leaves it: its files and their lines, systemd reloaded, the service
        enabled and the rule loaded (done), or still pending with the service off."""
        list(self.svc.install(IOCOST_ENTRIES, self.req))
        self.systemd.reloaded(self.file(self.unit_path))
        if done:
            self.svc.mark(self.record, Component.BTRFS_IOCOST.value)
            self.systemd.enabled = self.systemd.active = True
            self.udev_loaded = self.file(self.rule_path)

    # ---- the locks the fakes check -----------------------------------------------------------------
    def holds(self, lock: str, what: str) -> None:
        if not self.held[lock]:
            raise AssertionError(f"{what} without the {lock} lock")

    def service(self) -> Iocost:
        """The use cases over this world's ports."""
        return Iocost(
            sysfs=FakeIocostSysfs(self),
            qos=FakeQosControl(self),
            managed=FakeManagedDevices(self),
            udev=FakeUdev(self),
            systemd=FakeIocostSystemd(self),
            installer=FakeIocostInstaller(self),
            qos_lock=FakeIocostLock(self, "qos"),
            component_lock=FakeIocostLock(self, "component"),
            uninstall=FakeIocostUninstall(self),
        )


def _command_failed(argv: tuple[str, ...]) -> Callable[[OSError], CommandResult]:
    def failed(err: OSError) -> CommandResult:
        return CommandResult(1, "", f"{err.strerror}\n", argv=argv)

    return failed


def _installer_failed_lines(err: OSError) -> ExitAndLines:
    return 1, [err.strerror or str(err)]


class FakeQosControl:
    """QosControlPort over an IocostWorld."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.qos_path

    def read(self) -> str | None:
        return self.world.qos_text()

    def write(self, line: str) -> None:
        w = self.world
        w.holds("qos", f"io.cost.qos <- {line!r}")
        if w.qos is None:
            raise AssertionError(f"io.cost.qos <- {line!r} with no io.cost.qos (the kernel has none to write)")

        def merged() -> None:
            if w.qos_refusal is not None:
                w.events.append("qos failed")
                raise w.qos_refusal
            assert w.qos is not None
            w.qos[line.split(maxsplit=1)[0]] = line

        w.step(f"qos {line}", merged)


class FakeManagedDevices:
    """ManagedDevicesPort over an IocostWorld: a folder at its path cannot be written or removed (EISDIR)."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.managed_path

    def read(self) -> frozenset[str]:
        w = self.world
        return frozenset() if w.managed_folder else frozenset(list(w.managed))

    def save(self, devnos: frozenset[str]) -> None:
        w = self.world
        w.holds("qos", "the managed-devices file's save")

        def saved() -> None:
            if w.managed_folder:
                raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR))
            w.managed = set(devnos)

        w.step("managed save", saved)


class FakeIocostSysfs:
    """IocostSysfsPort over an IocostWorld."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def disks(self) -> tuple[BlockDisk, ...]:
        return tuple(self.world.disks)

    def io_controller(self) -> bool | None:
        ctrl = self.world.controllers
        return None if ctrl is None else "io" in ctrl.split()

    def model(self) -> str:
        return self.world.model.strip()


class FakeUdev:
    """UdevPort over an IocostWorld."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def running(self) -> bool:
        return self.world.udevd

    def rule_installed(self) -> bool:
        w = self.world
        return w.rule_path in w.target.files

    def properties(self, name: str) -> UdevProperties:
        answer = self.world.udev_answers.get(name, UDEV_PROCESSED)
        return UdevProperties(None if answer.props is None else dict(answer.props), answer.error)

    def reload_rules(self) -> CommandResult:
        w = self.world
        argv = ("udevadm", "control", "--reload-rules")

        def reload() -> CommandResult:
            w.udev_loaded = w.file(w.rule_path)
            w.events.append("udev")
            return CommandResult(0, "", "", argv=argv)

        return w.step("udev reload-rules", reload, _command_failed(argv))


class FakeIocostSystemd:
    """IocostSystemdPort over an IocostWorld: the unit is loaded while its file is in the target."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def probe(self) -> tuple[bool, str, str]:
        w = self.world
        if w.show_fails:
            return False, "unknown", "unknown"
        s = w.systemd
        return (
            w.unit_path in w.target.files,
            "active" if s.active else "inactive",
            "enabled" if s.enabled else "disabled",
        )

    def daemon_reload(self) -> CommandResult:
        w = self.world
        argv = ("systemctl", "daemon-reload")

        def reload() -> CommandResult:
            w.systemd.reloaded(w.file(w.unit_path))
            w.events.append("reload")
            return CommandResult(0, "", "", argv=argv)

        return w.step("systemd daemon-reload", reload, _command_failed(argv))

    def enable_now(self) -> CommandResult:
        w = self.world
        argv = ("systemctl", "enable", "--now", IOCOST_SERVICE)
        w.holds("component", "enable --now")
        if w.held["qos"]:
            raise AssertionError("enable --now while this run holds the io.cost.qos lock the service takes")

        def enable() -> CommandResult:
            if w.unit_path not in w.target.files:
                return CommandResult(
                    1, "", f"Failed to enable unit: Unit file {IOCOST_SERVICE} does not exist\n", argv=argv
                )
            w.systemd.enabled = w.systemd.active = True
            w.events.append("enable")
            return CommandResult(0, "", "", argv=argv)

        return w.step("systemd enable --now", enable, _command_failed(argv))

    def disable_now(self) -> CommandResult:
        w = self.world
        argv = ("systemctl", "disable", "--now", IOCOST_SERVICE)
        w.holds("component", "disable --now")

        def disable() -> CommandResult:
            w.systemd.enabled = w.systemd.active = False
            w.events.append("disable")
            return CommandResult(0, "", "", argv=argv)

        return w.step("systemd disable --now", disable, _command_failed(argv))


class FakeIocostInstaller:
    """IocostInstallerPort over an IocostWorld: install and mark through its InstallService, as
    installer.py does (its report lines, its error line on a failure)."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def available(self) -> bool:
        return self.world.installer_available

    def install(self) -> ExitAndLines:
        w = self.world
        w.holds("component", "the installer's install")

        def install() -> ExitAndLines:
            lines: list[str] = []
            try:
                lines.extend(w.svc.install(IOCOST_ENTRIES, w.req))
            except (WriteFailed, InstallRefused) as e:
                w.events.append("install failed")
                return 1, [*lines, f"{INSTALLER_NAME}: {e}"]
            if any(not line.startswith(InstallReportWord.UNCHANGED) for line in lines):
                w.systemd.changed()
            w.events.append("install")
            return 0, lines

        return w.step("installer install", install, _installer_failed_lines)

    def mark_done(self) -> ExitAndLines:
        w = self.world
        w.holds("component", "the installer's mark done")

        def mark() -> ExitAndLines:
            if w.before_mark is not None:
                w.before_mark()
            w.svc.mark(w.record, Component.BTRFS_IOCOST.value)
            w.events.append("mark")
            return 0, []

        return w.step("installer mark done", mark, _installer_failed_lines)

    def changes(self) -> tuple[str, ...] | None:
        answer = self.world.plan_answer
        return None if answer is None else tuple(answer)


class FakeIocostUninstall:
    """IocostUninstallPort over an IocostWorld's InstallService (its target's fail_on fails a removal)."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    @property
    def record_path(self) -> str:
        return self.world.record

    def mark(self, state: ComponentState) -> ComponentState | None:
        w = self.world
        w.holds("component", f"the uninstall's mark {state}")
        return w.step(f"uninstall mark {state}", lambda: w.svc.mark(w.record, Component.BTRFS_IOCOST.value, state))

    def remove(self) -> list[str]:
        w = self.world
        w.holds("component", "the uninstall's removal")

        def remove() -> list[str]:
            lines = list(w.svc.remove([w.svc.placement(e, w.req) for e in IOCOST_ENTRIES]))
            if lines:
                w.systemd.changed()
            return lines

        return w.step("uninstall remove", remove)


class FakeIocostLock:
    """IocostLockPort over an IocostWorld: busy, another run holds it; lock_errors, its folder or file
    cannot be made. A run never takes a lock it already holds."""

    def __init__(self, world: IocostWorld, name: str) -> None:
        self.world = world
        self.name = name

    @property
    def path(self) -> str:
        return self.world.lock_paths[self.name]

    def take(self) -> bool:
        w = self.world
        err = w.lock_errors.get(self.name)
        if err is not None:
            raise err
        if self.name in w.busy:
            return False
        if w.held[self.name]:
            raise AssertionError(f"the {self.name} lock taken twice")
        w.held[self.name] = True
        return True

    def release(self) -> None:
        self.world.held[self.name] = False


# ---- cache ---------------------------------------------------------------

UUID_ROOT = "11111111-1111-1111-1111-111111111111"
UUID_CACHE_DRIVE = "22222222-2222-2222-2222-222222222222"


class CacheTestCtx:
    def __init__(self, *, container: bool = False, root: bool = True) -> None:
        self._container = container
        self._root = root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def path(self, host_path: str) -> str:
        return f"/run/host/{host_path.lstrip('/')}" if self._container and host_path.startswith("/") else host_path

    def etc_dir(self) -> str:
        return "/run/host/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        cleaned = subpath.strip("/")
        base = "/run/host/run" if self._container else "/run"
        return f"{base}/{cleaned}" if cleaned else base


class MountTable:
    """A list of mounts by the paths the user sees, and the kernel's rules for them: a bind mount takes the
    device of the mount holding its source and shows the folder it binds as its FSROOT ("DEVICE[FSROOT]" in
    findmnt's source), an umount takes the top mount of its target off, and findmnt answers `-J -l -o FINDMNT_COLUMNS`.
    The root and /var/home of the root filesystem are mounted from the start."""

    def __init__(self, *, same_uuid: bool = False) -> None:
        self.same_uuid = same_uuid  # findmnt shows every btrfs mount with the root's UUID
        self.mounts: list[dict[str, str | None]] = []
        self.add_mount("/dev/dm-0", "/", fsroot="/root", uuid=UUID_ROOT)
        self.add_mount("/dev/dm-0", "/var/home", fsroot="/home", uuid=UUID_ROOT)

    def add_mount(
        self,
        dev: str,
        tgt: str,
        *,
        fsroot: str,
        uuid: str | None = UUID_CACHE_DRIVE,
        bind: bool = False,
    ) -> None:
        """One mount: on btrfs with its UUID, or a tmpfs (no UUID); a bind shows its FSROOT in its source."""
        self.mounts.append(
            {
                "source": f"{dev}[{fsroot}]" if bind else dev,
                "target": tgt,
                "fstype": "btrfs" if uuid is not None else "tmpfs",
                "options": "rw,noatime,compress=zstd:1",
                "uuid": uuid,
                "fsroot": fsroot,
            }
        )

    def set_options(self, tgt: str, options: str) -> None:
        """Update mount options recorded for `tgt`."""
        for m in self.mounts:
            if m["target"] == tgt:
                m["options"] = options

    def holding(self, path: str) -> dict[str, str | None]:
        """The mount whose target is the longest leading folder of path (the last one of equal length)."""
        best = self.mounts[0]
        for m in self.mounts:
            tgt = str(m["target"]).rstrip("/") or "/"
            if (tgt in (path, "/") or path.startswith(f"{tgt}/")) and len(tgt) >= len(str(best["target"]).rstrip("/")):
                best = m
        return best

    def bind(self, src: str, tgt: str) -> None:
        """mount --bind src tgt: a mount of src's device at tgt, its FSROOT the folder src is on it."""
        held = self.holding(src)
        rel = src.removeprefix(str(held["target"]).rstrip("/"))
        dev = str(held["source"]).split("[", 1)[0]
        fsroot = posixpath.normpath(f"{held['fsroot']}/{rel}")
        self.add_mount(dev, tgt, fsroot=fsroot, uuid=held["uuid"], bind=True)

    def mounted(self, tgt: str) -> bool:
        """Whether anything is mounted at tgt (an umount of tgt has a mount to take off)."""
        return any(m["target"] == tgt for m in self.mounts)

    def unbind(self, tgt: str) -> None:
        """umount tgt: its top mount taken off (nothing when nothing is mounted there: the fakes answer that
        umount with NOT_MOUNTED_TEXT before they call this)."""
        for i in range(len(self.mounts) - 1, -1, -1):
            if self.mounts[i]["target"] == tgt:
                del self.mounts[i]
                break

    def targets(self) -> list[str]:
        """Every mount's target, in the order they were mounted."""
        return [str(m["target"]) for m in self.mounts]

    def findmnt_json(self) -> str:
        fss = []
        for m in self.mounts:
            uuid = UUID_ROOT if self.same_uuid and m["uuid"] is not None else m["uuid"]
            fss.append({**m, "uuid": uuid})
        return json.dumps({"filesystems": fss}, indent=3)


class FstabSystemdView:
    """What systemd has loaded of /etc/fstab: the file's identity and its text at the last daemon-reload that
    worked, taken at the first look (boot) as a systemd that booted with what was there then. The identity
    is whatever tells one write of the file from another (a real file's inode and mtime, a fake's write
    number; None for no file): NeedDaemonReload goes by the file, not its text, so a rewrite of the same
    bytes counts."""

    def __init__(self) -> None:
        self.loaded: tuple[object, str | None] | None = None  # (identity, text); None before the first look

    def boot(self, identity: object, text: str | None) -> None:
        """The first look: systemd has the file as it is now (nothing when it has looked already)."""
        if self.loaded is None:
            self.loaded = (identity, text)

    def current(self, identity: object) -> bool:
        """Whether systemd has loaded the file whose identity is now identity."""
        return self.loaded is None or self.loaded[0] == identity

    def reloaded(self, identity: object, text: str | None) -> None:
        """A daemon-reload that worked: systemd has the file as it is now."""
        self.loaded = (identity, text)

    def text(self) -> str | None:
        """The text systemd loaded (None: no file, or no look yet)."""
        return None if self.loaded is None else self.loaded[1]


def mount_units_listing(loaded: str | None, *, need_reload: bool, mounted: Sequence[str], every_loaded: bool) -> str:
    """`systemctl show [--state=loaded] -p ... '*.mount'`'s answer: a block per loaded unit. A unit of each
    line of the loaded /etc/fstab (SourcePath=/etc/fstab, NeedDaemonReload=yes once the file changed since),
    mounted or not, and of each other mount (SourcePath=/proc/self/mountinfo, as systemd loads a mount it did
    not start). As the real systemctl: only with every_loaded (--state=loaded or --all) does a glob list the
    inactive units too; without it systemctl hides the inactive units that have no job queued (a failed or
    activating one is still listed). These fakes have no job and no failed or activating unit: their inactive
    units are those whose Where is not mounted, so without it only the units whose Where is mounted are listed."""
    need = "yes" if need_reload else "no"
    units: dict[str, tuple[str, str]] = {}
    for line in (loaded or "").splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#") and len(fields) > 1:
            units[fields[1]] = ("/etc/fstab", need)
    for tgt in mounted:
        units.setdefault(tgt, ("/proc/self/mountinfo", "no"))
    blocks = [
        f"Id={t.strip('/').replace('/', '-') or '-'}.mount\nLoadState=loaded\nSourcePath={src}\n"
        f"NeedDaemonReload={nd}\nWhere={t}\n"
        for t, (src, nd) in units.items()
        if every_loaded or t in mounted
    ]
    return "\n".join(blocks)


# What FakeMountRunner answers for a refused mount --bind, a busy umount and a failed daemon-reload, and the
# same answers as the errors a CacheWorld point fails with (the cache fakes word "WHAT failed: REASON" from the
# error's text): the contract, the fault matrix and the machine expect the same ERROR lines.
MOUNT_DENIED_TEXT: Final = "mount: permission denied"
UMOUNT_BUSY_TEXT: Final = "umount: target is busy"
# What util-linux's umount and mount answer (exit 32) for an umount of a target nothing is mounted at and a
# mount --bind onto a target folder that does not exist; {} is the target as the user sees it (FakeMountRunner
# leaves its test root out, so the fakes and the real adapters word it the same).
NOT_MOUNTED_TEXT: Final = "umount: {}: not mounted."
NO_MOUNT_POINT_TEXT: Final = "mount: {}: mount point does not exist."
RELOAD_FAILED_TEXT: Final = "Failed to reload daemon"
MOUNT_DENIED: Final = OSError(errno.EPERM, MOUNT_DENIED_TEXT)
UMOUNT_BUSY: Final = OSError(errno.EBUSY, UMOUNT_BUSY_TEXT)
RELOAD_FAILED: Final = OSError(errno.EIO, RELOAD_FAILED_TEXT)


class FakeMountRunner:
    """findmnt, mount and umount over a MountTable, and systemctl over an FstabSystemdView of the test root's
    /etc/fstab. findmnt answers `-J -l -o FINDMNT_COLUMNS` as the real one does: a bind mount's source is
    "DEVICE[FSROOT]", its fsroot the folder it shows. As util-linux, an umount of a target nothing is mounted at
    and a mount --bind onto a target folder that does not exist fail with exit 32."""

    def __init__(self, root: str, *, same_uuid: bool = False) -> None:
        self.root = root
        self.table = MountTable(same_uuid=same_uuid)
        self.findmnt_fails = False
        self.reload_fails = False
        self.on_failed_reload: Callable[[], None] | None = None  # runs when a reload fails, before the answer
        self.mount_fails: set[str] = set()
        self.umount_fails: set[str] = set()  # targets whose umount answers "target is busy" (exit 32)
        self.fstab_at_mount: list[str | None] = []
        self.fstab_at_umount: list[str | None] = []
        self.calls: list[list[str]] = []
        # systemd's view of the test root's /etc/fstab: the file (its inode and mtime: a rewrite of the same
        # bytes counts, as NeedDaemonReload goes by the file's time) and its text as the last daemon-reload
        # loaded them; taken at the first command, as a systemd that booted with what is there then.
        self.systemd = FstabSystemdView()
        self.reloads = 0  # the daemon-reloads that worked
        self.show_fails = False  # systemctl show answers exit 1: no answer
        self.units_visible = True  # False: systemctl show lists no unit (test_160's systemd, which sees none)
        self.before_reload: Callable[[], None] | None = None  # runs before a reload that works (a kill there)

    @property
    def mounts(self) -> list[dict[str, str | None]]:
        """The mounts, in the order they were mounted."""
        return self.table.mounts

    @mounts.setter
    def mounts(self, mounts: list[dict[str, str | None]]) -> None:
        self.table.mounts = mounts

    @property
    def same_uuid(self) -> bool:
        return self.table.same_uuid

    @same_uuid.setter
    def same_uuid(self, same: bool) -> None:
        self.table.same_uuid = same

    @property
    def loaded_fstab(self) -> tuple[object, str | None] | None:
        """(the identity, the text) of /etc/fstab at the last daemon-reload that worked; None before the first
        command."""
        return self.systemd.loaded

    @loaded_fstab.setter
    def loaded_fstab(self, loaded: tuple[object, str | None] | None) -> None:
        self.systemd.loaded = loaded

    def add_mount(
        self,
        dev: str,
        tgt: str,
        *,
        fsroot: str,
        uuid: str | None = UUID_CACHE_DRIVE,
        bind: bool = False,
    ) -> None:
        """One mount: on btrfs with its UUID, or a tmpfs (no UUID); a bind shows its FSROOT in its source."""
        self.table.add_mount(dev, tgt, fsroot=fsroot, uuid=uuid, bind=bind)

    def _fstab_now(self) -> str | None:
        fstab = os.path.join(self.root, "etc", "fstab")
        return Path(fstab).read_text(encoding="utf-8", errors="surrogateescape") if os.path.isfile(fstab) else None

    def _bind(self, src_path: str, tgt_path: str) -> CommandResult:
        self.fstab_at_mount.append(self._fstab_now())
        if tgt_path.removeprefix(self.root) in self.mount_fails:
            return CommandResult(32, "", MOUNT_DENIED_TEXT)
        if not os.path.isdir(tgt_path):
            return CommandResult(32, "", NO_MOUNT_POINT_TEXT.format(tgt_path.removeprefix(self.root)))
        self.table.bind(src_path.removeprefix(self.root), tgt_path.removeprefix(self.root))
        return CommandResult(0, "", "")

    def _unbind(self, tgt_path: str) -> CommandResult:
        self.fstab_at_umount.append(self._fstab_now())
        logical_tgt = tgt_path.removeprefix(self.root)
        if logical_tgt in self.umount_fails:
            return CommandResult(32, "", UMOUNT_BUSY_TEXT)
        if not self.table.mounted(logical_tgt):
            return CommandResult(32, "", NOT_MOUNTED_TEXT.format(logical_tgt))
        self.table.unbind(logical_tgt)
        return CommandResult(0, "", "")

    def _fstab_file(self) -> tuple[tuple[int, int] | None, str | None]:
        """(inode and mtime, text) of the test root's /etc/fstab; (None, None) without one, and no text for
        one that cannot be read (a test's)."""
        try:
            st = os.stat(os.path.join(self.root, "etc", "fstab"))
            return (st.st_ino, st.st_mtime_ns), self._fstab_now()
        except OSError:
            return None, None

    def view_current(self) -> bool:
        """Whether systemd has loaded the test root's /etc/fstab as it is on disk."""
        return self.systemd.current(self._fstab_file()[0])

    def _mount_units(self, *, every_loaded: bool) -> CommandResult:
        """`systemctl show [--state=loaded] -p ... '*.mount'` (mount_units_listing)."""
        if self.show_fails:
            return CommandResult(1, "", "Failed to connect to bus")
        if not self.units_visible:
            return CommandResult(0, "", "")
        listing = mount_units_listing(
            self.systemd.text(),
            need_reload=not self.view_current(),
            mounted=self.table.targets(),
            every_loaded=every_loaded,
        )
        return CommandResult(0, listing, "")

    def _findmnt(self) -> CommandResult:
        if self.findmnt_fails:
            return CommandResult(1, "", "findmnt: cannot read the mount table")
        return CommandResult(0, self.table.findmnt_json(), "")

    def _daemon_reload(self) -> CommandResult:
        """A reload that fails (reload_fails, after on_failed_reload), or one that works: before_reload runs
        (a kill there), then systemd has loaded the test root's /etc/fstab as it is now."""
        if self.reload_fails:
            if self.on_failed_reload is not None:
                self.on_failed_reload()
            return CommandResult(1, "", RELOAD_FAILED_TEXT)
        if self.before_reload is not None:
            self.before_reload()
        self.systemd.reloaded(*self._fstab_file())
        self.reloads += 1
        return CommandResult(0, "", "")

    def run(self, argv: Sequence[str], **_kwargs: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if self.systemd.loaded is None:
            self.systemd.boot(*self._fstab_file())
        if cmd == ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS]:
            return self._findmnt()
        if cmd[:2] == ["systemctl", "show"] and cmd[-1:] == ["*.mount"]:
            return self._mount_units(every_loaded=bool({"--state=loaded", "--all"} & set(cmd)))
        if cmd[-1:] == ["daemon-reload"]:
            return self._daemon_reload()
        if cmd[:2] == ["mount", "--bind"]:
            return self._bind(cmd[2], cmd[3])
        if cmd[:1] == ["umount"]:
            return self._unbind(cmd[1])
        return CommandResult(0, "", "")


def _os_error(code: int, path: str) -> OSError:
    return OSError(code, os.strerror(code), path)


def _fresh(text: str | None) -> str | None:
    """A copy of text that is not the same object (a read never hands out the text the world holds)."""
    return None if text is None else text.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")


class CacheWorld(FaultPoints):
    """What cache's ports see, in memory, for the use cases of btrfs/cache/application.py: /etc/fstab (its
    text, None for no file, or a folder at its path, which every read, write and removal fails on with
    EISDIR), /etc/crypttab, the accounts (passwd entries by name and the caller's name, and the folder links
    the home is resolved through, as /home -> /var/home on Silverblue), the file system as paths (folders,
    files and links, each named by where it is: entry_path follows the links among its folders, as the
    kernel walks a path, for every store and every check; the folders ensure_private made with their
    owner, the tags ensure_tag wrote), the
    mounts (a MountTable, shared with FakeMountRunner) and what systemd has loaded of /etc/fstab (an
    FstabSystemdView, shared with FakeMountRunner, fed a new identity per write and None after a removal,
    taken at the first call of any fake as a systemd that booted with what was there).

    Every mutating call is one point (FaultPoints), named as main's fault matrix named its call:
    "folder PATH" (each ensure_private), "tag SOURCE" (a CACHEDIR.TAG written: none when one is there),
    "mount --bind SOURCE TARGET", "umount TARGET", two for each /etc/fstab write (the write and the put-back):
    "fstab replace" (the new text in place) and "fstab sync" (its folder synced: no effect; a failure there
    raises ReplacedNotSynced, with the new text in place, as write_atomic does),
    "fstab remove" (the put-back when there was no file) and "systemctl daemon-reload". A failed command
    answers "WHAT failed: REASON" (the error's text); a failed folder, tag or /etc/fstab call raises it.
    Every error the fakes model (a link at a private folder: ENOTDIR, as O_NOFOLLOW with O_DIRECTORY gives;
    a file or a dangling link in its way: EEXIST; a file at or above a tag's folder: EEXIST, as write_atomic's
    ensure_dir gives; /etc/fstab a folder: EISDIR) raises inside the call, before a
    kill armed there fires; an umount of a target nothing is mounted at and a mount --bind onto a target that
    is not a folder answer as util-linux does (NOT_MOUNTED_TEXT, NO_MOUNT_POINT_TEXT), inside the call too. before_reload runs before a reload takes effect (a kill there leaves systemd
    behind); a KILL of the plan at the reload point is one after it took effect. Reads are not points;
    each read gives a new object, never the one the world holds."""

    def __init__(self) -> None:
        super().__init__()
        self.fstab: str | None = None
        self.fstab_folder = False  # a folder where /etc/fstab belongs
        self.fstab_writes = 0  # the identity of the file now: a new number per write (NeedDaemonReload)
        self.fstab_id: int | None = None  # None: no file
        self.crypttab: str | None = None
        self.users: dict[str, CacheMountUser] = {}
        self.caller = ""
        # The paths below are each entry's own place (entry_path: the links among its folders followed), so
        # an entry put or asked for through a linked folder is the one at the end of the link.
        self.links: dict[str, str] = {}  # path -> the path it points to
        self.dirs: set[str] = {"/"}
        self.files: dict[str, int] = {}  # path -> its write's number (a new one per write, as a new inode)
        self.file_writes = 0
        self.made: dict[str, tuple[int, int]] = {}  # the folders ensure_private made private: path -> (uid, gid)
        self.tags: dict[str, tuple[int, int]] = {}  # the folders ensure_tag wrote a CACHEDIR.TAG in -> its owner
        self.table = MountTable()
        self.systemd = FstabSystemdView()
        self.reloads = 0  # the daemon-reloads that took effect
        self.findmnt_fails = False
        self.findmnt_bad_json = False
        self.show_fails = False  # systemctl show gives no answer
        self.units_visible = True  # False: systemctl show lists no unit
        self.before_reload: Callable[[], None] | None = None  # runs before a reload takes effect (a kill there)
        self.failing_at: dict[str, tuple[int, OSError]] = {}  # point -> (the n-th call from now, its error)
        self.seen: dict[str, int] = {}

    def fail_at(self, point: str, n: int, err: OSError) -> None:
        """The n-th call of point from now fails with err (once); a failure fail() armed there answers the
        other calls, before and after it, as it would have without this one (the n-th call, when it runs,
        uses up one call of a counted one, as a call that failure answered would)."""
        self.failing_at[point] = (n, err)
        self.seen[point] = 0

    @override
    def step[T](self, point: str, effect: Callable[[], T], failed: Callable[[OSError], T] | None = None) -> T:
        at = self.failing_at.get(point)
        if at is not None:
            self.seen[point] += 1
            if self.seen[point] == at[0]:
                del self.failing_at[point]
                kept = self.failing.get(point)
                self.fail(point, at[1], times=1)
                try:
                    return super().step(point, effect, failed)
                finally:
                    ran = self.failing[point][1] == 0  # the call ran (not a plan's FAIL there, nor a dead run)
                    if kept is None:
                        del self.failing[point]
                    else:
                        err, left = kept
                        self.failing[point] = (err, left - 1 if ran and left is not None and left > 0 else left)
        return super().step(point, effect, failed)

    # ---- the start state ---------------------------------------------------------------------------
    def put_fstab(self, text: str | None) -> None:
        """/etc/fstab as a test's start state (None: no file), as systemd booted with it."""
        self.fstab = text
        self.fstab_writes += 1
        self.fstab_id = None if text is None else self.fstab_writes

    def add_user(self, name: str, uid: int, home: str) -> None:
        """An account (its gid its uid), and its home folder."""
        self.users[name] = CacheMountUser(name, uid, uid, home)
        self.mkdirs(home)

    def real_path(self, path: str, *, follow_last: bool = True) -> str:
        """path with each link among its folders followed, and its last part's too when follow_last, as the
        kernel walks a path: /home/carol is /var/home/carol where /home is a link to /var/home."""
        parts = [part for part in posixpath.normpath(path).split("/") if part]
        out = "/"
        for i, part in enumerate(parts):
            out = posixpath.join(out, part)
            hops = 0
            while (follow_last or i < len(parts) - 1) and out in self.links and hops <= len(self.links):
                out, hops = posixpath.normpath(self.links[out]), hops + 1
        return out

    def entry_path(self, path: str) -> str:
        """Where the entry named path is: the links among its folders followed, its last part not (what
        os.lstat looks at). Every store and every check of the world names an entry by it."""
        return self.real_path(path, follow_last=False)

    def mkdirs(self, path: str) -> None:
        """The folder path and its parents, made where its links lead (os.makedirs)."""
        p = self.real_path(path)
        while p not in self.dirs:
            self.dirs.add(p)
            p = posixpath.dirname(p)

    def put_file(self, path: str) -> None:
        """A file written at path (a new one, as a new inode, where one was), its folders made."""
        self.mkdirs(posixpath.dirname(path))
        self.file_writes += 1
        self.files[self.entry_path(path)] = self.file_writes

    def put_link(self, path: str, to: str) -> None:
        self.mkdirs(posixpath.dirname(path))
        self.links[self.entry_path(path)] = to

    def mount_bind(self, source: str, target: str) -> None:
        """A bind of source at target in place (its target folder made), recorded as no call."""
        self.mkdirs(target)
        self.table.bind(source, target)

    # ---- what the state is -------------------------------------------------------------------------
    def view_current(self) -> bool:
        """Whether systemd has loaded /etc/fstab as it is now."""
        return self.systemd.current(self.fstab_id)

    def commands(self) -> list[str]:
        """The points that ran a command (mount, umount, daemon-reload), in order."""
        return [c for c in self.calls if c.startswith(("mount ", "umount ", "systemctl "))]

    def exists(self, path: str) -> bool:
        """Anything at path, a dangling link too (os.path.lexists), through the links among its folders."""
        p = self.entry_path(path)
        return p in self.dirs or p in self.files or p in self.links

    def is_link(self, path: str) -> bool:
        """A link at path, through the links among its folders (os.path.islink)."""
        return self.entry_path(path) in self.links

    def is_dir(self, path: str) -> bool:
        """A folder at path, through its links (os.path.isdir)."""
        return self.real_path(path) in self.dirs

    def nonempty(self, path: str) -> bool:
        """_dir_is_nonempty's answer: not there or a link is False (asked only for a non-link), a file
        is True, a folder whether anything is in it (os.lstat, then os.scandir)."""
        p = self.entry_path(path)
        if p in self.files:
            return True
        if p not in self.dirs:
            return False
        inside = f"{p.rstrip('/')}/"
        return any(p.startswith(inside) for p in (*self.dirs, *self.files, *self.links))

    def look(self) -> None:
        """Any call of a fake: systemd has booted with /etc/fstab as it is at the first one."""
        if self.systemd.loaded is None:
            self.systemd.boot(self.fstab_id, self.fstab)

    def service(self) -> CacheMounts:
        """The use cases over this world's ports."""
        return CacheMounts(
            fstab=FakeFstab(self),
            mounts=FakeCacheMountTable(self),
            systemd=FakeCacheSystemd(self),
            folders=FakeCacheFolders(self),
            host=FakeCacheHostView(self),
            accounts=FakeCacheAccounts(self),
        )


class FakeFstab:
    """FstabPort over a CacheWorld."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def read(self) -> str | None:
        w = self.world
        w.look()
        if w.fstab_folder:
            raise _os_error(errno.EISDIR, "/etc/fstab")
        return _fresh(w.fstab)

    def write(self, text: str) -> None:
        w = self.world
        w.look()
        copy = _fresh(text)

        def write() -> None:
            if w.fstab_folder:
                raise _os_error(errno.EISDIR, "/etc/fstab")
            w.fstab_writes += 1
            w.fstab, w.fstab_id = copy, w.fstab_writes

        def not_synced(err: OSError) -> None:
            raise ReplacedNotSynced(err.errno, err.strerror, err.filename, "/etc/fstab") from err

        w.step("fstab replace", write)
        w.step("fstab sync", lambda: None, not_synced)

    def remove(self) -> None:
        w = self.world
        w.look()

        def remove() -> None:
            if w.fstab_folder:
                raise _os_error(errno.EISDIR, "/etc/fstab")
            w.fstab, w.fstab_id = None, None

        w.step("fstab remove", remove)


class FakeCacheMountTable:
    """MountTablePort over a CacheWorld's MountTable."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def read(self) -> tuple[tuple[LiveMount, ...], str | None]:
        w = self.world
        w.look()
        if w.findmnt_fails:
            return (), "findmnt failed: findmnt: cannot read the mount table"
        mounts = parse_findmnt_mounts('{"filesystems": 3}' if w.findmnt_bad_json else w.table.findmnt_json())
        if mounts is None:
            return (), "findmnt's answer is not the JSON it should be"
        return mounts, None

    def bind(self, binding: CacheMountBinding) -> str | None:
        w = self.world
        w.look()
        what = f"mount --bind {binding.source} {binding.target}"

        def bind() -> str | None:
            if not w.is_dir(binding.target):
                return f"{what} failed: {NO_MOUNT_POINT_TEXT.format(binding.target)}"
            w.table.bind(binding.source, binding.target)
            return None

        return w.step(what, bind, lambda err: f"{what} failed: {err.strerror}")

    def unbind(self, binding: CacheMountBinding) -> str | None:
        w = self.world
        w.look()
        what = f"umount {binding.target}"

        def unbind() -> str | None:
            if not w.table.mounted(binding.target):
                return f"{what} failed: {NOT_MOUNTED_TEXT.format(binding.target)}"
            w.table.unbind(binding.target)
            return None

        return w.step(what, unbind, lambda err: f"{what} failed: {err.strerror}")


class FakeCacheSystemd:
    """CacheSystemdPort over a CacheWorld's FstabSystemdView."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def daemon_reload(self) -> str | None:
        w = self.world
        w.look()
        what = "systemctl daemon-reload"

        def reload() -> str | None:
            if w.before_reload is not None:
                w.before_reload()
            w.systemd.reloaded(w.fstab_id, _fresh(w.fstab))
            w.reloads += 1
            return None

        return w.step(what, reload, lambda err: f"{what} failed: {err.strerror}")

    def mount_units(self) -> list[dict[str, str]] | None:
        """The units mount_units_listing gives with --state=loaded, as SystemctlAdapter.show_units parses them."""
        w = self.world
        w.look()
        if w.show_fails:
            return None
        if not w.units_visible:
            return []
        listing = mount_units_listing(
            w.systemd.text(), need_reload=not w.view_current(), mounted=w.table.targets(), every_loaded=True
        )
        return [{p: unit[p] for p in MOUNT_UNIT_PROPS if p in unit} for unit in parse_show_units(listing)]


class FakeCacheFolders:
    """CacheFoldersPort over a CacheWorld's paths."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def ensure_private(self, path: str, user: CacheMountUser) -> None:
        w = self.world
        w.look()

        def ensure() -> None:
            p = posixpath.normpath(path)
            in_the_way = [
                q for q in _self_and_parents(p) if w.entry_path(q) in w.files or (w.is_link(q) and not w.is_dir(q))
            ]
            if in_the_way:
                raise _os_error(errno.EEXIST, in_the_way[-1])
            if w.is_link(p):
                raise _os_error(errno.ENOTDIR, p)  # O_NOFOLLOW with O_DIRECTORY on a link
            w.mkdirs(p)
            w.made[w.real_path(p)] = (user.uid, user.gid)

        w.step(f"folder {path}", ensure)

    def ensure_tag(self, source: str, user: CacheMountUser) -> None:
        w = self.world
        w.look()
        tag = posixpath.join(source, CACHE_TAG)
        if w.exists(tag):
            return

        def write() -> None:
            in_the_way = [q for q in _self_and_parents(source) if w.entry_path(q) in w.files]
            if in_the_way:
                raise _os_error(errno.EEXIST, in_the_way[-1])  # ensure_dir's mkdir of a file's path
            w.mkdirs(source)  # write_atomic makes the folder it writes in
            w.put_file(tag)
            w.tags[w.real_path(source)] = (user.uid, user.gid)

        w.step(f"tag {source}", write)


def _self_and_parents(path: str) -> list[str]:
    """path and each folder above it, the root last."""
    out = [path]
    while out[-1] != posixpath.dirname(out[-1]):
        out.append(posixpath.dirname(out[-1]))
    return out


class FakeCacheHostView:
    """CacheHostViewPort over a CacheWorld's paths."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def crypttab(self) -> str | None:
        self.world.look()
        return _fresh(self.world.crypttab)

    def binding_paths(self, binding: CacheMountBinding) -> BindingPaths:
        w = self.world
        w.look()
        src, tgt = binding.source, binding.target
        is_link = w.is_link(tgt)
        exists = w.exists(tgt)
        return BindingPaths(
            target_is_link=is_link,
            target_exists=exists,
            target_nonempty=False if is_link else w.nonempty(tgt),
            source_is_dir=w.is_dir(src),
            source_is_link=w.is_link(src),
            target_is_dir=exists and not is_link and w.is_dir(tgt),
        )

    def home_exists(self, home: str) -> bool:
        self.world.look()
        return self.world.is_dir(home)


class FakeCacheAccounts:
    """CacheAccountsPort over a CacheWorld's accounts: the home resolved through its folder links."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def resolve(self, requested: str | None) -> tuple[CacheMountUser | None, str | None]:
        w = self.world
        w.look()
        name = requested or w.caller
        user = w.users.get(name)
        if user is None:
            return None, f"user {name!r} does not exist"
        if user.uid < MIN_HUMAN_UID:
            return None, f"refusing system user {user.name!r} (uid={user.uid} < {MIN_HUMAN_UID})"
        return CacheMountUser(user.name, user.uid, user.gid, w.real_path(user.home)), None  # os.path.realpath


# ---- compress-force and commit-interval ------------------------------------------------------------------


@dataclass
class LayerHostState:
    """What a LayerHost holds: whether the host is rpm-ostree booted, the root UUID, /etc/fstab's text, the live
    mounts (/proc/mounts), the booted and the staged kernel arguments, the top-mount unit (None: a layer that is
    not there), the answer to a confirmation (None: no terminal), and the failures to answer. A test module
    declares the layers again with its own sample texts as their defaults."""

    is_ostree: bool = True
    root_uuid: str = ""
    fstab: str | None = None
    proc_mounts: str | None = None
    proc_cmdline: str | None = None
    kargs_ok: bool = True
    kargs_text: str = ""
    kargs_err: str = ""
    top_mount: TopMountInfo | None = None
    confirm_answer: bool | None = True
    fail_kargs: str | None = None
    fail_remount: str | None = None
    fake_remount_effect: bool = True
    fail_top_mount: Exception | None = None
    fstab_behind: bool | None = False  # systemd has not loaded /etc/fstab as it is on disk; None: no answer
    reload_error: str | None = None  # what a daemon-reload answers (None: it works)


class LayerHost(ABC):
    """compress-force's and commit-interval's port (CompressHostPort) over a LayerHostState (s), for their unit
    tests (test_btrfs_compress, test_commit_interval); calls holds each mutating call. torn names the step whose
    first call changes its layer and then reports a failure, as the adapter can (test_compress_faults.py,
    AdapterFailureTest): "fstab", "kargs" or "top"; None for none. A module says how a remount changes the
    live mounts (remounted) and how an update of the top mount is written down in calls (top_mount_call)."""

    def __init__(self, state: LayerHostState, *, torn: str | None = None) -> None:
        self.s = state
        self.torn = torn
        self.calls: list[str] = []

    @abstractmethod
    def remounted(self, mounts: str, target: str, option: str) -> str:
        """The live mounts after a remount of target with option."""

    def top_mount_call(self, uuid: str, options: str) -> str:
        return f"update_top_mount:{uuid}:{options}"

    def _first(self, prefix: str) -> bool:
        return sum(1 for c in self.calls if c.startswith(prefix)) == 1

    def is_ostree(self) -> bool:
        return self.s.is_ostree

    def root_uuid(self) -> str:
        return self.s.root_uuid

    def read_fstab(self) -> str | None:
        return self.s.fstab

    def read_proc_mounts(self) -> str | None:
        return self.s.proc_mounts

    def read_proc_cmdline(self) -> str | None:
        return self.s.proc_cmdline

    def read_kargs(self) -> tuple[bool, str, str]:
        return (self.s.kargs_ok, self.s.kargs_text, self.s.kargs_err)

    def read_settled_kargs(self) -> tuple[bool, str, str]:
        return self.read_kargs()

    def read_top_mount(self) -> TopMountInfo | None:
        return self.s.top_mount

    def mount_units(self) -> list[dict[str, str]] | None:
        """One unit of /etc/fstab's root line, NeedDaemonReload=yes when systemd is behind; None: no answer."""
        if self.s.fstab_behind is None:
            return None
        need = "yes" if self.s.fstab_behind else "no"
        return [
            {"Id": "-.mount", "LoadState": "loaded", "SourcePath": "/etc/fstab", "NeedDaemonReload": need, "Where": "/"}
        ]

    def daemon_reload(self) -> str | None:
        self.calls.append("daemon_reload")
        if self.s.reload_error is None:
            self.s.fstab_behind = False
        return self.s.reload_error

    def write_fstab(self, text: str, *, put_back: bool = True) -> None:
        del put_back  # the fake puts nothing back after a failure
        self.calls.append("write_fstab")
        self.s.fstab = text
        if self.torn == "fstab" and self._first("write_fstab"):
            raise OSError(errno.EIO, "Input/output error")

    def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
        self.calls.append(f"update_kargs:{edits}")
        if self.s.fail_kargs is not None:
            answer = (False, self.s.fail_kargs)
        else:
            for old_tok, new_tok in edits:
                self.s.kargs_text = self.s.kargs_text.replace(old_tok, new_tok, 1)
            answer = (True, "")
        if self.torn == "kargs" and self._first("update_kargs"):
            return False, "timed out"
        return answer

    def remount(self, target: str, option: str) -> tuple[bool, str]:
        self.calls.append(f"remount:{target}:{option}")
        if self.s.fail_remount is not None:
            return False, self.s.fail_remount
        if self.s.fake_remount_effect and self.s.proc_mounts is not None:
            self.s.proc_mounts = self.remounted(self.s.proc_mounts, target, option)
        return True, ""

    def update_top_mount(self, uuid: str, options: str, *, put_back: bool = True) -> None:
        del put_back  # the fake puts nothing back after a failure
        self.calls.append(self.top_mount_call(uuid, options))
        if self.s.fail_top_mount is not None:
            raise self.s.fail_top_mount
        self.s.top_mount = TopMountInfo(installed=True, uuid=uuid, options=options)
        if self.torn == "top" and self._first("update_top_mount"):
            raise OSError(errno.ENOSPC, "No space left on device")

    def confirm(self, prompt: str) -> bool:
        self.calls.append(f"confirm:{prompt}")
        if self.s.confirm_answer is None:
            raise Refused("apply asks for confirmation: run it in a terminal")
        return self.s.confirm_answer


_HIDDEN_OPTIONS: Final = frozenset({"commit=30", "compress=no"})  # what the kernel never shows in /proc/mounts


def _option_family(token: str) -> str:
    """The option a token sets: compress and compress-force are one (each replaces the other)."""
    key = token.split("=", 1)[0]
    return "compress" if key in {"compress", "compress-force"} else key


def kernel_remount_options(options: str, option: str) -> str:
    """A mount's options as /proc/mounts shows them after `mount -o remount,OPTION`: the token of that
    option's family replaced in its place (or added at the end), and dropped where the kernel hides it
    (commit=30, btrfs's default; compress=no). Written out here, not through the domain's rewrite
    helpers, so the fake does not share their bugs."""
    family = _option_family(option)
    out: list[str] = []
    placed = False
    for tok in options.split(","):
        if tok and _option_family(tok) == family:
            if not placed and option not in _HIDDEN_OPTIONS:
                out.append(option)
            placed = True
        elif tok:
            out.append(tok)
    if not placed and option not in _HIDDEN_OPTIONS:
        out.append(option)
    return ",".join(out)


class CompressWorld(FaultPoints):
    """What compress-force's and commit-interval's port (CompressHostPort, btrfs/compress/ports.py) sees, in
    memory: whether the host is rpm-ostree booted, the root UUID, /etc/fstab's text (None: no file), the staged
    deployment's kernel arguments (`rpm-ostree kargs`'s answer) and the booted ones (/proc/cmdline), the live
    mounts (/proc/mounts's text), the top-mount unit (TopMountInfo, None: not installed) and the answer to a
    confirmation. The world implements the port itself.

    Every mutating call is one point (FaultPoints): "fstab write" (write_fstab, its daemon-reload included),
    "kargs" (update_kargs), "remount" (remount), "top-mount" (update_top_mount) and "systemctl daemon-reload"
    (the catch-up reload of a run that writes no /etc/fstab). What systemd has loaded of /etc/fstab is an
    FstabSystemdView: a write reloads it, a torn one leaves it behind, show_fails makes mount_units answer None. A failed fstab write or top-mount update raises the
    error with no effect, a failed kargs update or remount answers (False, the error's text) with no effect.
    The points named in torn take effect first and then fail, as the real adapter can (btrfs/tests/
    test_compress_faults.py, AdapterFailureTest): /etc/fstab renamed into place before its folder's sync
    failed, or a put-back after a failed reload that failed too; an `rpm-ostree kargs` that timed out after
    its transaction started; a remount that the kernel made before mount(8) failed (it could not update its
    mount table, or it was killed after the call); a top-mount unit whose put-back failed. With lagging_kargs, a torn kargs change
    is staged only when its transaction ends: at read_settled_kargs (which, with stuck_kargs, answers that one
    still runs and stages nothing) or settle(). After a failed point named in unreadable_after, its layer's
    read cannot answer (read_fstab, read_proc_mounts, read_top_mount: None; read_kargs: ok False) until the
    test clears
    unreadable. Reads are no points.

    update_kargs is rpm-ostree's: each `--delete=OLD` removes that exact argument and each `--append=NEW`
    adds one at the end, all or nothing (an OLD that is not there fails the whole call, with no effect). A
    remount changes every mount of the remounted device, and only those, as the kernel shows them
    (kernel_remount_options); remounts lists each (target, option) asked for, a failed one too; the next
    ignored_remounts remounts answer that they worked and change nothing, as a kernel whose mounts do not
    show the option asked for (the use cases' verification after the remount catches it)."""

    def __init__(self, *, is_ostree: bool = True, root_uuid: str = "") -> None:
        super().__init__()
        self.ostree = is_ostree
        self.uuid = root_uuid
        self.fstab: str | None = None
        self.kargs = ""  # the staged deployment's kernel arguments, as `rpm-ostree kargs` prints them
        self.cmdline: str | None = None  # the booted ones (/proc/cmdline)
        self.mounts: str | None = None  # /proc/mounts
        self.top: TopMountInfo | None = None
        self.confirm_answer = True
        self.torn: frozenset[str] = frozenset()
        # The points after whose failure their layer's read cannot answer (read_fstab, read_top_mount: None),
        # and the points whose failure did that so far.
        self.unreadable_after: frozenset[str] = frozenset()
        self.unreadable: set[str] = set()
        # A torn kargs change staged only when its transaction ends (read_settled_kargs or settle()), and a
        # transaction that does not end while read_settled_kargs waits.
        self.lagging_kargs = False
        self.stuck_kargs = False
        self.pending_kargs: Callable[[], object] | None = None
        self.remounts: list[tuple[str, str]] = []
        self.ignored_remounts = 0  # the next ones answer (True, "") and change nothing (the verification's case)
        # What systemd has loaded of /etc/fstab (taken at the first call, as a systemd that booted with the start
        # text), a new identity per write: a write reloads it, a torn one leaves it behind.
        self.systemd = FstabSystemdView()
        self.fstab_id = 0
        self.show_fails = False  # systemctl show gives no answer

    # ---- CompressHostPort: reads ---------------------------------------------------------------------
    def is_ostree(self) -> bool:
        return self.ostree

    def root_uuid(self) -> str:
        return self.uuid

    def read_fstab(self) -> str | None:
        return None if "fstab write" in self.unreadable else _fresh(self.fstab)

    def read_proc_mounts(self) -> str | None:
        return None if "remount" in self.unreadable else self.mounts

    def read_proc_cmdline(self) -> str | None:
        return self.cmdline

    def read_kargs(self) -> tuple[bool, str, str]:
        if "kargs" in self.unreadable:
            return False, "", "error: cannot read the kernel arguments"
        return True, self.kargs, ""

    def read_settled_kargs(self) -> tuple[bool, str, str]:
        if self.stuck_kargs:
            return False, "", KARGS_STILL_RUNNING
        self.settle()
        return self.read_kargs()

    def settle(self) -> None:
        """The transaction of a lagging kargs change ends: the change is staged."""
        if self.pending_kargs is not None:
            self.pending_kargs()
            self.pending_kargs = None

    def read_top_mount(self) -> TopMountInfo | None:
        return None if "top-mount" in self.unreadable else self.top

    def look(self) -> None:
        """Any call: systemd has booted with /etc/fstab as it is at the first one."""
        self.systemd.boot(self.fstab_id, self.fstab)

    def mount_units(self) -> list[dict[str, str]] | None:
        """The units mount_units_listing gives with --state=loaded, of the root mounts of /proc/mounts."""
        self.look()
        if self.show_fails:
            return None
        mounted = [f[1] for f in (line.split() for line in (self.mounts or "").splitlines()) if len(f) > 1]
        listing = mount_units_listing(
            self.systemd.text(), need_reload=not self.systemd.current(self.fstab_id), mounted=mounted, every_loaded=True
        )
        return [{p: unit[p] for p in MOUNT_UNIT_PROPS if p in unit} for unit in parse_show_units(listing)]

    def daemon_reload(self) -> str | None:
        """The catch-up reload: point "systemctl daemon-reload"."""
        self.look()

        def reload() -> str | None:
            self.systemd.reloaded(self.fstab_id, self.fstab)
            return None

        return self.step(
            "systemctl daemon-reload", reload, lambda err: f"systemctl daemon-reload failed: {err.strerror}"
        )

    def confirm(self, prompt: str) -> bool:
        del prompt
        return self.confirm_answer

    # ---- CompressHostPort: the mutating calls ---------------------------------------------------------
    def _torn(self, point: str, effect: Callable[[], object]) -> None:
        if point in self.torn:
            effect()
        if point in self.unreadable_after:
            self.unreadable.add(point)

    def write_fstab(self, text: str, *, put_back: bool = True) -> None:
        del put_back  # a failure has no effect here and a torn one keeps it: nothing is put back
        self.look()

        def replace() -> None:
            self.fstab, self.fstab_id = text, self.fstab_id + 1

        def write() -> None:
            replace()
            self.systemd.reloaded(self.fstab_id, self.fstab)

        def failed(err: OSError) -> None:
            self._torn("fstab write", replace)  # in place, systemd not reloaded
            raise err

        self.step("fstab write", write, failed)

    def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
        def staged() -> tuple[bool, str]:
            args = self.kargs.split()
            for old, _new in edits:
                if old not in args:
                    return False, f"error: No karg '{old}' found"
                args.remove(old)
            args.extend(new for _old, new in edits)
            self.kargs = " ".join(args) + "\n"
            return True, ""

        def later() -> None:
            self.pending_kargs = staged

        def failed(err: OSError) -> tuple[bool, str]:
            self._torn("kargs", later if self.lagging_kargs else staged)
            return False, str(err.strerror)

        return self.step("kargs", staged, failed)

    def update_top_mount(self, uuid: str, options: str, *, put_back: bool = True) -> None:
        del put_back  # as write_fstab's

        def install() -> None:
            self.top = TopMountInfo(installed=True, uuid=uuid, options=options)

        def failed(err: OSError) -> None:
            self._torn("top-mount", install)
            raise err

        self.step("top-mount", install, failed)

    def remount(self, target: str, option: str) -> tuple[bool, str]:
        self.remounts.append((target, option))
        rows = [line.split() for line in (self.mounts or "").splitlines()]
        device = next((f[0] for f in rows if f[1:2] == [target]), None)

        def remounted() -> tuple[bool, str]:
            if device is None:
                return False, f"mount: {target}: not mounted."
            if self.ignored_remounts > 0:
                self.ignored_remounts -= 1
                return True, ""
            self.mounts = "".join(
                " ".join([f[0], f[1], f[2], kernel_remount_options(f[3], option), *f[4:]]) + "\n"
                if f[0] == device
                else " ".join(f) + "\n"
                for f in rows
            )
            return True, ""

        def failed(err: OSError) -> tuple[bool, str]:
            self._torn("remount", remounted)
            return False, str(err.strerror)

        return self.step("remount", remounted, failed)
