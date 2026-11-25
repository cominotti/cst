# SPDX-License-Identifier: MPL-2.0
"""The contract of btrfs/cache/ports.py, run on the in-memory fakes (CacheWorld) and on the real adapters
(btrfs/cache/adapters.py in a temporary test root, FakeMountRunner as the runner), and the pins of what only
the real adapters do: each command's argv and options, /etc/fstab's mode, the tag's mode and payload, the
read view from a toolbox.

The fault matrix (test_cache_faults.py) and the machine (tests/mbt/mbt_cache_rollback.py) run over the
fakes; this file keeps them equal to the adapters, every error a fake models included.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
import dataclasses
import errno
import os
import shutil
import stat
import tempfile
from typing import Protocol, override
import unittest
from unittest import mock

from btrfs.cache.adapters import (
    CacheAccounts,
    CacheFolders,
    CacheHostView,
    CacheMountTable,
    CacheSystemd,
    FstabFile,
    cache_runtime,
)
from btrfs.cache.domain import (
    CACHE_BASE,
    FILE_MODE_TAG,
    FINDMNT_COLUMNS,
    SCRATCHPAD_BASE,
    CacheMountBinding,
    CacheMountUser,
    build_user_bindings,
)
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS
from btrfs.cache.ports import (
    BindingPaths,
    CacheAccountsPort,
    CacheFoldersPort,
    CacheHostViewPort,
    CacheSystemdPort,
    FstabPort,
    MountTablePort,
)
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from btrfs.tests.fakes import (
    MOUNT_DENIED,
    RELOAD_FAILED,
    UMOUNT_BUSY,
    CacheTestCtx,
    CacheWorld,
    FakeCacheAccounts,
    FakeCacheFolders,
    FakeCacheHostView,
    FakeCacheMountTable,
    FakeCacheSystemd,
    FakeFstab,
    FakeMountRunner,
)
from cst_core.adapters import files as core_files
from cst_core.ports import CommandResult, OutputMode, ReplacedNotSynced
from tests.fault_injection import no_file_may_grow, no_space

USER = CacheMountUser("alice", 1000, 1000, "/var/home/alice")
CACHE, SCRATCHPAD = build_user_bindings(USER)
NAS_LINE = "nas:/share /var/mnt/nas nfs defaults,nofail 0 0\n"
NOT_UTF8 = b"# caf\xe9 written by an old tool\n".decode("utf-8", "surrogateescape")
MOUNT_UNITS_SHOW = ["systemctl", "show", "--state=loaded", *(a for p in MOUNT_UNIT_PROPS for a in ("-p", p)), "*.mount"]


def _eio() -> OSError:
    return OSError(errno.EIO, os.strerror(errno.EIO))


class Side(Protocol):
    """One implementation of the ports under one contract: the ports, and how a test sets their state."""

    def fstab(self) -> FstabPort: ...
    def mounts(self) -> MountTablePort: ...
    def systemd(self) -> CacheSystemdPort: ...
    def folders(self) -> CacheFoldersPort: ...
    def host(self) -> CacheHostViewPort: ...
    def accounts(self) -> CacheAccountsPort: ...
    def put_fstab(self, text: str | None) -> None: ...
    def fstab_is_a_folder(self) -> None: ...
    def disk_full(self) -> contextlib.AbstractContextManager[None]: ...
    def fstab_sync_fails(self) -> contextlib.AbstractContextManager[None]: ...
    def put_crypttab(self, text: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def put_file(self, path: str) -> None: ...
    def put_link(self, path: str, to: str) -> None: ...
    def is_dir(self, path: str) -> bool: ...
    def is_link(self, path: str) -> bool: ...
    def exists(self, path: str) -> bool: ...
    def file_id(self, path: str) -> object: ...
    def add_user(self, name: str, uid: int, home: str) -> None: ...
    def caller(self, name: str) -> None: ...
    def mount_bases(self) -> None: ...
    def findmnt_fails(self) -> None: ...
    def findmnt_bad_json(self) -> None: ...
    def mount_fails(self, binding: CacheMountBinding) -> None: ...
    def umount_fails(self, binding: CacheMountBinding) -> None: ...
    def reload_fails(self) -> None: ...
    def show_fails(self) -> None: ...
    def no_units(self) -> None: ...
    def systemd_current(self) -> bool: ...


class FakeSide:
    """The in-memory fakes over one CacheWorld."""

    def __init__(self) -> None:
        self.world = CacheWorld()

    def fstab(self) -> FstabPort:
        return FakeFstab(self.world)

    def mounts(self) -> MountTablePort:
        return FakeCacheMountTable(self.world)

    def systemd(self) -> CacheSystemdPort:
        return FakeCacheSystemd(self.world)

    def folders(self) -> CacheFoldersPort:
        return FakeCacheFolders(self.world)

    def host(self) -> CacheHostViewPort:
        return FakeCacheHostView(self.world)

    def accounts(self) -> CacheAccountsPort:
        return FakeCacheAccounts(self.world)

    def put_fstab(self, text: str | None) -> None:
        self.world.put_fstab(text)

    def fstab_is_a_folder(self) -> None:
        self.world.fstab_folder = True

    @contextlib.contextmanager
    def disk_full(self) -> Generator[None]:
        self.world.fail("fstab replace", no_space())
        try:
            yield
        finally:
            self.world.failing.clear()

    @contextlib.contextmanager
    def fstab_sync_fails(self) -> Generator[None]:
        self.world.fail("fstab sync", _eio())
        try:
            yield
        finally:
            self.world.failing.clear()

    def put_crypttab(self, text: str) -> None:
        self.world.crypttab = text

    def mkdir(self, path: str) -> None:
        self.world.mkdirs(path)

    def put_file(self, path: str) -> None:
        self.world.put_file(path)

    def put_link(self, path: str, to: str) -> None:
        self.world.put_link(path, to)

    def is_dir(self, path: str) -> bool:
        return self.world.is_dir(path)

    def is_link(self, path: str) -> bool:
        return self.world.is_link(path)

    def exists(self, path: str) -> bool:
        return self.world.exists(path)

    def file_id(self, path: str) -> object:
        """The write that put the file at path there (a new one per write, as an inode)."""
        return self.world.files[self.world.entry_path(path)]

    def add_user(self, name: str, uid: int, home: str) -> None:
        self.world.add_user(name, uid, home)

    def caller(self, name: str) -> None:
        self.world.caller = name

    def mount_bases(self) -> None:
        self.world.table.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
        self.world.table.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")

    def findmnt_fails(self) -> None:
        self.world.findmnt_fails = True

    def findmnt_bad_json(self) -> None:
        self.world.findmnt_bad_json = True

    def mount_fails(self, binding: CacheMountBinding) -> None:
        self.world.fail(f"mount --bind {binding.source} {binding.target}", MOUNT_DENIED)

    def umount_fails(self, binding: CacheMountBinding) -> None:
        self.world.fail(f"umount {binding.target}", UMOUNT_BUSY)

    def reload_fails(self) -> None:
        self.world.fail("systemctl daemon-reload", RELOAD_FAILED)

    def show_fails(self) -> None:
        self.world.show_fails = True

    def no_units(self) -> None:
        self.world.units_visible = False

    def systemd_current(self) -> bool:
        return self.world.view_current()


class ContractMountRunner(FakeMountRunner):
    """FakeMountRunner, and a findmnt that answers what is not JSON."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.bad_json = False

    @override
    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        if self.bad_json and list(argv[:1]) == ["findmnt"]:
            return CommandResult(0, '{"filesystems": 3}', "")
        return super().run(argv, **kwargs)


class RealSide:
    """The real adapters over a test root (CST_BTRFS_CACHE_ROOT), FakeMountRunner as the runner."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.runner = ContractMountRunner(root)
        self.user = ""
        os.makedirs(self.at("/etc"))

    def at(self, path: str) -> str:
        return f"{self.root}{path}"

    def _env(self) -> dict[str, str]:
        return {"CST_BTRFS_CACHE_ROOT": self.root, "USER": self.user}

    def fstab(self) -> FstabPort:
        return FstabFile(cache_runtime(self._env(), CacheTestCtx(), self.runner))

    def mounts(self) -> MountTablePort:
        return CacheMountTable(cache_runtime(self._env(), CacheTestCtx(), self.runner))

    def systemd(self) -> CacheSystemdPort:
        return CacheSystemd(cache_runtime(self._env(), CacheTestCtx(), self.runner))

    def folders(self) -> CacheFoldersPort:
        return CacheFolders(cache_runtime(self._env(), CacheTestCtx(), self.runner))

    def host(self) -> CacheHostViewPort:
        return CacheHostView(cache_runtime(self._env(), CacheTestCtx(), self.runner))

    def accounts(self) -> CacheAccountsPort:
        return CacheAccounts(cache_runtime(self._env(), CacheTestCtx(), self.runner))

    def put_fstab(self, text: str | None) -> None:
        if text is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.at("/etc/fstab"))
            return
        with open(self.at("/etc/fstab"), "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(text)

    def fstab_is_a_folder(self) -> None:
        os.makedirs(self.at("/etc/fstab"))

    def disk_full(self) -> contextlib.AbstractContextManager[None]:
        return no_file_may_grow()

    @contextlib.contextmanager
    def fstab_sync_fails(self) -> Generator[None]:
        """write_atomic's fsync of a folder fails with EIO (its file's fsync works)."""
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise _eio()
            real_fsync(fd)

        with mock.patch.object(core_files.os, "fsync", fsync):
            yield

    def put_crypttab(self, text: str) -> None:
        with open(self.at("/etc/crypttab"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def mkdir(self, path: str) -> None:
        os.makedirs(self.at(path), exist_ok=True)

    def put_file(self, path: str) -> None:
        os.makedirs(os.path.dirname(self.at(path)), exist_ok=True)
        with open(self.at(path), "w", encoding="utf-8") as fh:
            fh.write("x\n")

    def put_link(self, path: str, to: str) -> None:
        os.makedirs(os.path.dirname(self.at(path)), exist_ok=True)
        os.symlink(self.at(to), self.at(path))

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(self.at(path))

    def is_link(self, path: str) -> bool:
        return os.path.islink(self.at(path))

    def exists(self, path: str) -> bool:
        return os.path.lexists(self.at(path))

    def file_id(self, path: str) -> object:
        """The file's inode (write_atomic's rename puts a new one there)."""
        st = os.lstat(self.at(path))
        return (st.st_dev, st.st_ino)

    def add_user(self, name: str, uid: int, home: str) -> None:
        with open(self.at("/etc/passwd"), "a", encoding="utf-8") as fh:
            fh.write(f"{name}:x:{uid}:{uid}:{name}:{home}:/bin/bash\n")
        os.makedirs(self.at(home), exist_ok=True)

    def caller(self, name: str) -> None:
        self.user = name

    def mount_bases(self) -> None:
        self.runner.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
        self.runner.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")

    def findmnt_fails(self) -> None:
        self.runner.findmnt_fails = True

    def findmnt_bad_json(self) -> None:
        self.runner.bad_json = True

    def mount_fails(self, binding: CacheMountBinding) -> None:
        self.runner.mount_fails.add(binding.target)

    def umount_fails(self, binding: CacheMountBinding) -> None:
        self.runner.umount_fails.add(binding.target)

    def reload_fails(self) -> None:
        self.runner.reload_fails = True

    def show_fails(self) -> None:
        self.runner.show_fails = True

    def no_units(self) -> None:
        self.runner.units_visible = False

    def systemd_current(self) -> bool:
        return self.runner.view_current()


def _paths(*true: str) -> BindingPaths:
    """BindingPaths with the facts named true, every other one false."""
    return BindingPaths(**{f.name: f.name in true for f in dataclasses.fields(BindingPaths)})


def _unit(units: list[dict[str, str]] | None, where: str) -> dict[str, str]:
    assert units is not None
    found = [u for u in units if u.get("Where") == where]
    assert len(found) == 1, units
    return found[0]


class _PortsContract(unittest.TestCase):
    """The contract of each port; FakePortsContractTest and RealPortsContractTest give the side."""

    def make_side(self, root: str) -> Side:
        raise NotImplementedError

    @override
    def setUp(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cst-cache-ports-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.side = self.make_side(self.root)

    # /etc/fstab
    def test_no_fstab_reads_as_none_and_its_removal_is_no_error(self) -> None:
        self.assertIsNone(self.side.fstab().read())
        self.side.fstab().remove()
        self.assertIsNone(self.side.fstab().read())

    def test_fstab_is_written_and_read_back_byte_for_byte(self) -> None:
        f = self.side.fstab()
        text = NAS_LINE + NOT_UTF8
        f.write(text)
        self.assertEqual(f.read(), text)
        first, second = f.read(), f.read()
        self.assertIsNot(first, second)
        f.remove()
        self.assertIsNone(f.read())

    def test_a_folder_at_the_fstab_path_fails_every_call(self) -> None:
        self.side.fstab_is_a_folder()
        f = self.side.fstab()
        for name, call in (("read", f.read), ("write", lambda: f.write(NAS_LINE)), ("remove", f.remove)):
            with self.subTest(call=name), self.assertRaises(OSError) as caught:
                call()
            self.assertEqual(caught.exception.errno, errno.EISDIR, name)

    def test_a_write_on_a_full_disk_raises_and_keeps_the_file(self) -> None:
        self.side.put_fstab(NAS_LINE)
        with self.side.disk_full(), self.assertRaises(OSError):
            self.side.fstab().write(NAS_LINE + NAS_LINE)
        self.assertEqual(self.side.fstab().read(), NAS_LINE)

    def test_a_write_whose_folder_sync_fails_is_in_place_and_says_so(self) -> None:
        self.side.put_fstab(NAS_LINE)
        with self.side.fstab_sync_fails(), self.assertRaises(ReplacedNotSynced) as caught:
            self.side.fstab().write(NAS_LINE + NOT_UTF8)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(self.side.fstab().read(), NAS_LINE + NOT_UTF8)

    # the mount table
    def test_a_bind_shows_its_source_device_and_folder(self) -> None:
        self.side.mount_bases()
        self.side.mkdir(CACHE.source)
        self.side.mkdir(CACHE.target)
        m = self.side.mounts()
        self.assertIsNone(m.bind(CACHE))
        mounts, error = m.read()
        self.assertIsNone(error)
        top = [x for x in mounts if x.target == CACHE.target]
        self.assertEqual([(x.source, x.fsroot) for x in top], [("/dev/mapper/cache[/cache/alice]", "/cache/alice")])
        first, second = m.read(), m.read()
        self.assertIsNot(first[0], second[0])
        self.assertIsNone(m.unbind(CACHE))
        self.assertEqual([x for x in m.read()[0] if x.target == CACHE.target], [])

    def test_a_failed_bind_and_a_busy_umount_say_why(self) -> None:
        self.side.mount_bases()
        for b in (CACHE, SCRATCHPAD):
            self.side.mkdir(b.source)
            self.side.mkdir(b.target)
        self.side.mount_fails(CACHE)
        m = self.side.mounts()
        self.assertEqual(
            m.bind(CACHE), "mount --bind /var/mnt/cache/alice /var/home/alice/.cache failed: mount: permission denied"
        )
        self.assertIsNone(m.bind(SCRATCHPAD))
        self.side.umount_fails(SCRATCHPAD)
        self.assertEqual(m.unbind(SCRATCHPAD), "umount /var/home/alice/Scratchpad failed: umount: target is busy")

    def test_an_umount_of_nothing_and_a_bind_onto_no_folder_fail_as_the_commands_do(self) -> None:
        self.side.mount_bases()
        self.side.mkdir(CACHE.source)
        m = self.side.mounts()
        cases = (
            (
                "umount of nothing",
                lambda: m.unbind(CACHE),
                "umount /var/home/alice/.cache failed: umount: /var/home/alice/.cache: not mounted.",
            ),
            (
                "bind onto no folder",
                lambda: m.bind(CACHE),
                (
                    "mount --bind /var/mnt/cache/alice /var/home/alice/.cache failed: "
                    "mount: /var/home/alice/.cache: mount point does not exist."
                ),
            ),
        )
        for name, call, want in cases:
            with self.subTest(name):
                self.assertEqual(call(), want)
        mounts, error = m.read()
        self.assertIsNone(error)
        self.assertEqual([x for x in mounts if x.target == CACHE.target], [])

    def test_findmnt_failing_is_no_mount_and_a_reason(self) -> None:
        self.side.findmnt_fails()
        self.assertEqual(self.side.mounts().read(), ((), "findmnt failed: findmnt: cannot read the mount table"))

    def test_findmnt_answering_no_json_is_no_mount_and_a_reason(self) -> None:
        self.side.findmnt_bad_json()
        self.assertEqual(self.side.mounts().read(), ((), "findmnt's answer is not the JSON it should be"))

    # systemd
    def test_the_mount_units_include_an_fstab_unit_that_is_not_mounted(self) -> None:
        # R23: without --state=loaded a glob hides the inactive units, and a stale /etc/fstab whose units
        # are all inactive looked current. On the real side only the adapter's argv makes this pass.
        self.side.put_fstab(NAS_LINE)
        unit = _unit(self.side.systemd().mount_units(), "/var/mnt/nas")
        self.assertEqual((unit["SourcePath"], unit["NeedDaemonReload"]), ("/etc/fstab", "no"))
        self.assertEqual(_unit(self.side.systemd().mount_units(), "/")["SourcePath"], "/proc/self/mountinfo")

    def test_need_daemon_reload_follows_a_write_and_a_reload(self) -> None:
        self.side.put_fstab(NAS_LINE)
        s = self.side.systemd()
        self.assertEqual(_unit(s.mount_units(), "/var/mnt/nas")["NeedDaemonReload"], "no")
        self.side.fstab().write(NAS_LINE + "# another line\n")
        self.assertEqual(_unit(s.mount_units(), "/var/mnt/nas")["NeedDaemonReload"], "yes")
        self.assertFalse(self.side.systemd_current())
        self.assertIsNone(s.daemon_reload())
        self.assertEqual(_unit(s.mount_units(), "/var/mnt/nas")["NeedDaemonReload"], "no")
        self.assertTrue(self.side.systemd_current())

    def test_a_write_then_a_removal_back_to_no_file_is_current_again(self) -> None:
        # attach's put-back from no /etc/fstab: systemd booted with none, and none is there again.
        s = self.side.systemd()
        self.assertEqual([u for u in s.mount_units() or [] if u["SourcePath"] == "/etc/fstab"], [])
        self.side.fstab().write(NAS_LINE)
        self.assertFalse(self.side.systemd_current())
        self.side.fstab().remove()
        self.assertTrue(self.side.systemd_current())

    def test_a_failed_reload_says_why_and_loads_nothing(self) -> None:
        self.side.put_fstab(NAS_LINE)
        self.side.systemd().mount_units()
        self.side.fstab().write("")
        self.side.reload_fails()
        self.assertEqual(self.side.systemd().daemon_reload(), "systemctl daemon-reload failed: Failed to reload daemon")
        self.assertFalse(self.side.systemd_current())

    def test_no_answer_and_no_units(self) -> None:
        self.side.no_units()
        self.assertEqual(self.side.systemd().mount_units(), [])
        self.side.show_fails()
        self.assertIsNone(self.side.systemd().mount_units())

    # the folders and the tag
    def test_a_private_folder_is_made_and_an_existing_one_kept(self) -> None:
        f = self.side.folders()
        f.ensure_private(CACHE.source, USER)
        self.assertTrue(self.side.is_dir(CACHE.source))
        self.side.put_file(f"{CACHE.source}/kept")
        f.ensure_private(CACHE.source, USER)
        self.assertTrue(self.side.exists(f"{CACHE.source}/kept"))

    def test_a_link_at_a_private_folder_fails_without_following_it(self) -> None:
        self.side.mkdir("/var/elsewhere")
        self.side.put_link(CACHE.source, "/var/elsewhere")
        with self.assertRaises(OSError) as caught:
            self.side.folders().ensure_private(CACHE.source, USER)
        self.assertEqual(caught.exception.errno, errno.ENOTDIR)  # O_NOFOLLOW with O_DIRECTORY on a link
        self.assertTrue(self.side.is_link(CACHE.source))

    def test_a_file_in_the_way_of_a_private_folder_fails(self) -> None:
        self.side.put_file("/var/mnt/cache")
        with self.assertRaises(OSError) as above:
            self.side.folders().ensure_private(CACHE.source, USER)
        self.assertEqual(above.exception.errno, errno.EEXIST)
        self.side.put_file(SCRATCHPAD.target)
        with self.assertRaises(OSError) as at:
            self.side.folders().ensure_private(SCRATCHPAD.target, USER)
        self.assertEqual(at.exception.errno, errno.EEXIST)

    def test_the_tag_is_written_once_and_a_link_there_left_alone(self) -> None:
        f = self.side.folders()
        f.ensure_private(SCRATCHPAD.source, USER)
        f.ensure_tag(SCRATCHPAD.source, USER)
        tag = f"{SCRATCHPAD.source}/{CACHE_TAG}"
        self.assertTrue(self.side.exists(tag))
        f.ensure_tag(SCRATCHPAD.source, USER)  # there already: nothing
        other = "/var/mnt/scratchpad/bob"
        self.side.mkdir(other)
        self.side.mkdir("/var/elsewhere")
        self.side.put_link(f"{other}/{CACHE_TAG}", "/var/elsewhere")
        f.ensure_tag(other, USER)
        self.assertTrue(self.side.is_link(f"{other}/{CACHE_TAG}"))

    def test_the_tag_through_a_linked_folder_is_written_once(self) -> None:
        self.side.mkdir("/var/elsewhere")
        self.side.put_link(SCRATCHPAD.source, "/var/elsewhere")
        f = self.side.folders()
        f.ensure_tag(SCRATCHPAD.source, USER)
        tag = f"{SCRATCHPAD.source}/{CACHE_TAG}"
        self.assertTrue(self.side.exists(tag))
        self.assertTrue(self.side.exists(f"/var/elsewhere/{CACHE_TAG}"))
        first = self.side.file_id(tag)
        f.ensure_tag(SCRATCHPAD.source, USER)  # there already, through the link: nothing
        self.assertEqual(self.side.file_id(tag), first)
        self.assertEqual(self.side.file_id(f"/var/elsewhere/{CACHE_TAG}"), first)

    def test_a_tag_in_a_missing_folder_makes_the_folder(self) -> None:
        self.side.folders().ensure_tag(SCRATCHPAD.source, USER)
        self.assertTrue(self.side.exists(f"{SCRATCHPAD.source}/{CACHE_TAG}"))
        self.assertTrue(self.side.is_dir(SCRATCHPAD.source))

    def test_a_tag_under_a_file_fails(self) -> None:
        self.side.put_file("/var/mnt/scratchpad")
        with self.assertRaises(OSError) as above:
            self.side.folders().ensure_tag(SCRATCHPAD.source, USER)
        self.assertEqual(above.exception.errno, errno.EEXIST)  # write_atomic's ensure_dir: mkdir of a file

    # the host's facts
    def test_binding_paths_for_each_kind_of_path(self) -> None:
        h = self.side.host()
        self.assertEqual(h.binding_paths(CACHE), _paths())
        self.side.mkdir(CACHE.source)
        self.side.mkdir(CACHE.target)
        self.assertEqual(h.binding_paths(CACHE), _paths("target_exists", "source_is_dir", "target_is_dir"))
        self.side.put_file(f"{CACHE.target}/old")
        self.assertEqual(
            h.binding_paths(CACHE),
            _paths("target_exists", "target_nonempty", "source_is_dir", "target_is_dir"),
        )
        self.side.put_file(SCRATCHPAD.target)
        self.side.mkdir("/var/elsewhere")
        self.side.put_link(SCRATCHPAD.source, "/var/elsewhere")
        self.assertEqual(
            h.binding_paths(SCRATCHPAD),
            _paths("target_exists", "target_nonempty", "source_is_dir", "source_is_link"),
        )
        link = build_user_bindings(CacheMountUser("bob", 1001, 1001, "/var/home/bob"), include_scratchpad=False)[0]
        self.side.put_link(link.target, "/var/elsewhere")
        self.assertEqual(h.binding_paths(link), _paths("target_is_link", "target_exists"))
        first, second = h.binding_paths(CACHE), h.binding_paths(CACHE)
        self.assertIsNot(first, second)

    def test_crypttab_and_the_home(self) -> None:
        h = self.side.host()
        self.assertIsNone(h.crypttab())
        self.side.put_crypttab("cache UUID=x none luks\n")
        self.assertEqual(h.crypttab(), "cache UUID=x none luks\n")
        self.assertFalse(h.home_exists("/var/home/alice"))
        self.side.mkdir("/var/home/alice")
        self.assertTrue(h.home_exists("/var/home/alice"))

    # the accounts
    def test_resolve_the_caller_a_named_user_and_the_refusals(self) -> None:
        self.side.add_user("alice", 1000, "/var/home/alice")
        self.side.add_user("bob", 1001, "/var/home/bob")
        self.side.add_user("svc", 2, "/var/lib/svc")
        self.side.caller("alice")
        a = self.side.accounts()
        self.assertEqual(a.resolve(None), (USER, None))
        self.assertEqual(a.resolve("bob"), (CacheMountUser("bob", 1001, 1001, "/var/home/bob"), None))
        self.assertEqual(a.resolve("cst-test-no-such-user"), (None, "user 'cst-test-no-such-user' does not exist"))
        self.assertEqual(a.resolve("svc"), (None, "refusing system user 'svc' (uid=2 < 1000)"))

    def test_a_home_under_a_link_is_named_by_its_real_path(self) -> None:
        self.side.mkdir("/var/home")
        self.side.put_link("/home", "/var/home")
        self.side.add_user("carol", 1002, "/home/carol")
        self.assertEqual(
            self.side.accounts().resolve("carol")[0], CacheMountUser("carol", 1002, 1002, "/var/home/carol")
        )
        self.assertTrue(self.side.host().home_exists("/var/home/carol"))
        self.assertTrue(self.side.host().home_exists("/home/carol"))

    def test_what_is_put_through_a_linked_home_is_there_by_both_names(self) -> None:
        self.side.mkdir("/var/home")
        self.side.put_link("/home", "/var/home")  # Silverblue
        self.side.put_file("/home/carol/x")
        self.assertTrue(self.side.exists("/home/carol/x"))
        self.assertTrue(self.side.exists("/var/home/carol/x"))
        self.side.mkdir("/var/elsewhere")
        self.side.put_link("/home/carol/l", "/var/elsewhere")
        self.assertTrue(self.side.is_link("/home/carol/l"))
        self.assertTrue(self.side.is_link("/var/home/carol/l"))
        carol = build_user_bindings(CacheMountUser("carol", 1002, 1002, "/home/carol"), include_scratchpad=False)[0]
        self.side.put_file(f"/var{carol.target}/old")
        self.assertEqual(
            self.side.host().binding_paths(carol),
            _paths("target_exists", "target_nonempty", "target_is_dir"),
        )
        dave = build_user_bindings(CacheMountUser("dave", 1003, 1003, "/home/dave"), include_scratchpad=False)[0]
        self.side.put_link(f"/var{dave.target}", "/var/elsewhere")
        self.assertEqual(self.side.host().binding_paths(dave), _paths("target_is_link", "target_exists"))


class FakePortsContractTest(_PortsContract):
    @override
    def make_side(self, root: str) -> Side:
        return FakeSide()


class RealPortsContractTest(_PortsContract):
    @override
    def make_side(self, root: str) -> Side:
        return RealSide(root)


del _PortsContract  # a base only: unittest runs its subclasses


class RecordingRunner(FakeMountRunner):
    """FakeMountRunner that keeps each command's options too."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.options: list[tuple[list[str], dict[str, object]]] = []

    @override
    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        self.options.append((list(argv), dict(kwargs)))
        return super().run(argv, **kwargs)


class ToolboxCtx(CacheTestCtx):
    """A toolbox that sees the host under host (not /run/host)."""

    def __init__(self, host: str) -> None:
        super().__init__(container=True)
        self.host = host

    @override
    def path(self, host_path: str) -> str:
        return f"{self.host}/{host_path.lstrip('/')}"


class CacheAdapterPinsTest(unittest.TestCase):
    """What only the real adapters do: the commands' argv and options, the files' modes, the read view."""

    @override
    def setUp(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cst-cache-pins-"))
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(f"{self.root}/etc")
        self.runner = RecordingRunner(self.root)
        self.rt = cache_runtime({"CST_BTRFS_CACHE_ROOT": self.root}, CacheTestCtx(), self.runner)

    def test_findmnt_argv_and_options_on_the_host_and_from_a_toolbox(self) -> None:
        want = ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS]
        options = {"timeout": 15, "locale_c": True, "output": OutputMode.CAPTURE}
        for ctx, seam, on_host in (
            (CacheTestCtx(), True, False),
            (CacheTestCtx(container=True), True, False),  # a test root is no host to reach
            (ToolboxCtx(self.root), False, True),
        ):
            with self.subTest(container=ctx.is_container(), seam=seam):
                self.runner.options.clear()
                env = {"CST_BTRFS_CACHE_ROOT": self.root} if seam else {}
                CacheMountTable(cache_runtime(env, ctx, self.runner)).read()
                self.assertEqual(self.runner.options, [(want, {"on_host": on_host, **options})])

    def test_mount_and_umount_argv_on_the_test_root_and_their_options(self) -> None:
        os.makedirs(f"{self.root}{CACHE.source}")
        os.makedirs(f"{self.root}{CACHE.target}")
        m = CacheMountTable(self.rt)
        m.bind(CACHE)
        m.unbind(CACHE)
        options = {"timeout": 30, "locale_c": True, "output": OutputMode.CAPTURE}
        self.assertEqual(
            self.runner.options,
            [
                (["mount", "--bind", f"{self.root}{CACHE.source}", f"{self.root}{CACHE.target}"], options),
                (["umount", f"{self.root}{CACHE.target}"], options),
            ],
        )

    def test_systemctl_argv_local_reload_and_loaded_units_on_the_host_from_a_toolbox(self) -> None:
        CacheSystemd(self.rt).daemon_reload()
        CacheSystemd(self.rt).mount_units()
        box = cache_runtime({}, ToolboxCtx(self.root), self.runner)
        CacheSystemd(box).daemon_reload()
        CacheSystemd(box).mount_units()
        reload = (["systemctl", "daemon-reload"], {"check": False, "timeout": None, "locale_c": False})
        show = {"check": False, "timeout": 20, "locale_c": True}
        self.assertEqual(
            self.runner.options,
            [
                reload,
                (MOUNT_UNITS_SHOW, {**show, "on_host": False}),
                reload,  # the reload stays local, also from a toolbox
                (MOUNT_UNITS_SHOW, {**show, "on_host": True}),
            ],
        )

    def test_fstab_is_written_0644_with_no_temporary_file_left(self) -> None:
        FstabFile(self.rt).write(NAS_LINE)
        self.assertEqual(stat.S_IMODE(os.stat(f"{self.root}/etc/fstab").st_mode), 0o644)
        self.assertEqual(os.listdir(f"{self.root}/etc"), ["fstab"])

    def test_the_tag_s_payload_and_mode_and_a_private_folder_s_mode(self) -> None:
        CacheFolders(self.rt).ensure_private(SCRATCHPAD.source, USER)
        CacheFolders(self.rt).ensure_tag(SCRATCHPAD.source, USER)
        src = f"{self.root}{SCRATCHPAD.source}"
        self.assertEqual(stat.S_IMODE(os.stat(src).st_mode), 0o700)
        with open(f"{src}/{CACHE_TAG}", "rb") as fh:
            self.assertTrue(fh.read().startswith(CACHE_SIGNATURE))
        self.assertEqual(stat.S_IMODE(os.stat(f"{src}/{CACHE_TAG}").st_mode), FILE_MODE_TAG)

    def test_a_link_at_a_private_folder_keeps_its_target_s_mode(self) -> None:
        elsewhere = f"{self.root}/elsewhere"
        os.mkdir(elsewhere)
        os.chmod(elsewhere, 0o755)
        os.makedirs(f"{self.root}/var/mnt/cache")
        os.symlink(elsewhere, f"{self.root}{CACHE.source}")
        with self.assertRaises(OSError):
            CacheFolders(self.rt).ensure_private(CACHE.source, USER)
        self.assertEqual(stat.S_IMODE(os.stat(elsewhere).st_mode), 0o755)

    def test_the_reads_go_through_the_toolbox_s_view_of_the_host(self) -> None:
        host = f"{self.root}/host"
        os.makedirs(f"{host}/etc")
        with open(f"{host}/etc/fstab", "w", encoding="utf-8") as fh:
            fh.write(NAS_LINE)
        box = cache_runtime({}, ToolboxCtx(host), self.runner)
        self.assertEqual(FstabFile(box).read(), NAS_LINE)
        os.makedirs(f"{host}{CACHE.target}")
        self.assertTrue(CacheHostView(box).binding_paths(CACHE).target_exists)
        self.assertTrue(CacheHostView(box).home_exists(USER.home))


if __name__ == "__main__":
    unittest.main()
