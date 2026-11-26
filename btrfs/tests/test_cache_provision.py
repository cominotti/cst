# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs cache provision`, `unprovision`, `--migrate`, and their domain/adapters."""

from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
from dataclasses import dataclass, field
import errno
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock

from btrfs.cache.adapters.migrate import CacheMigrationAdapter
from btrfs.cache.adapters.mounts import CacheMountRuntime
from btrfs.cache.adapters.provision import CacheProvisionAdapter
from btrfs.cache.application.mounts import CacheMounts
from btrfs.cache.domain import (
    CACHE_BASE,
    OPT_COMPRESS_FORCE,
    SCRATCHPAD_BASE,
    BusyProcess,
    CacheMountAction,
    CacheMountKind,
    CacheMountUser,
    ProvisionSubcommand,
    parse_cache_mount_args,
)
from btrfs.cache.domain.migrate import MIGRATE_BACKUP_SUFFIX, backup_path_for, cleanup_hint
from btrfs.cache.domain.provision import (
    BASE_MOUNT_OPTIONS,
    BTRFS_CACHE_LABEL,
    CRYPTTAB_PATH,
    PROVISION_BASE_MARKER,
    SUBVOL_FOR_KIND,
    BlockDeviceFacts,
    CacheHardwareSpec,
    LuksMapperState,
    ProvisionBaseState,
    ProvisionProbe,
    parse_cache_conf,
    plan_provision_crypttab,
    plan_provision_fstab,
    plan_unprovision_crypttab,
    plan_unprovision_fstab,
    render_base_fstab_line,
    render_cache_conf,
    validate_device_for_init,
    validate_provision_apply,
    validate_unprovision,
)
from btrfs.cache.ports import CachePathKind
from btrfs.tests.fakes import (
    RELOAD_FAILED,
    UUID_CACHE_DRIVE,
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
from cst_core.ports import CommandResult, ReplacedNotSynced

SAMPLE_PARTUUID = "12345678-1234-1234-1234-123456789abc"
SAMPLE_LUKS_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SAMPLE_SIZE = 500107862016


def _sample_spec(mapper: str = "cache") -> CacheHardwareSpec:
    return CacheHardwareSpec(partuuid=SAMPLE_PARTUUID, size_bytes=SAMPLE_SIZE, mapper_name=mapper)


def _blank_dev(*, dev_type: str = "part", partuuid: str = SAMPLE_PARTUUID, size: int = SAMPLE_SIZE) -> BlockDeviceFacts:
    return BlockDeviceFacts(
        dev_path="/dev/nvme2n1p1",
        real_path="/dev/nvme2n1p1",
        dev_type=dev_type,
        partuuid=partuuid,
        size_bytes=size,
        model="WD Black SN850X",
    )


def _luks_dev() -> BlockDeviceFacts:
    return BlockDeviceFacts(
        dev_path=f"/dev/disk/by-partuuid/{SAMPLE_PARTUUID}",
        real_path="/dev/nvme2n1p1",
        dev_type="part",
        partuuid=SAMPLE_PARTUUID,
        size_bytes=SAMPLE_SIZE,
        fstype="crypto_LUKS",
        uuid=SAMPLE_LUKS_UUID,
    )


def _empty_bases() -> dict[CacheMountKind, ProvisionBaseState]:
    return {
        k: ProvisionBaseState(kind=k, dir_exists=False, dir_is_symlink=False, dir_not_dir=False)
        for k in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD)
    }


def _probe(
    *,
    spec: CacheHardwareSpec | None = None,
    dev: BlockDeviceFacts | None = None,
    luks: LuksMapperState | None = None,
) -> ProvisionProbe:
    return ProvisionProbe(
        conf_text=None if spec is None else render_cache_conf(spec),
        conf_error=None,
        spec=spec,
        conf_problems=(),
        device=dev,
        device_error=None,
        luks=LuksMapperState() if luks is None else luks,
        crypttab_text="",
        crypttab_error=None,
        fstab_text="",
        fstab_error=None,
        bases=_empty_bases(),
        user_managed_entries=(),
        user_mounts_active=(),
        fstab_behind=False,
    )


class FakeProvisionPort:
    """In-memory CacheProvisionPort supporting fault injection at every step."""

    def __init__(self, world: CacheWorld | None = None) -> None:
        self.world = world
        self.conf_text: str | None = None
        self.crypttab_text: str | None = ""
        self.dev: BlockDeviceFacts | None = _blank_dev()
        self.dev_err: str | None = None
        self.keyfiles: set[str] = set()
        self.non_regular_keyfile = False
        self.luks_formatted = False
        self.mapper_open = False
        self.keyfile_unlocks = True
        self.mapper_fstype = ""
        self.mapper_label = ""
        self.mapper_luks_uuid: str | None = SAMPLE_LUKS_UUID
        self.subvols: set[str] = set()
        self.base_dirs: set[str] = set()
        self.mounts: dict[str, tuple[str, CacheMountKind]] = {}
        self.confirm_answer: tuple[bool, str | None] = (True, None)
        self.fail_on: str | None = None
        self.fail_crypttab_not_synced = False
        self.close_calls = 0
        self.scrub_calls: list[str] = []
        self.remount_calls: list[str] = []

    def read_conf(self) -> tuple[str | None, str | None]:
        return self.conf_text, None

    def write_conf(self, text: str) -> None:
        self.conf_text = text

    def read_crypttab_exact(self) -> tuple[str | None, str | None]:
        return self.crypttab_text, None

    def write_crypttab(self, text: str) -> None:
        if self.fail_on == "write_crypttab":
            raise OSError(errno.EIO, "write crypttab failed")
        if self.fail_crypttab_not_synced:
            self.crypttab_text = text
            self.fail_crypttab_not_synced = False
            raise ReplacedNotSynced(errno.EIO, "dir fsync failed", CRYPTTAB_PATH, CRYPTTAB_PATH)
        self.crypttab_text = text

    def remove_crypttab(self) -> None:
        self.crypttab_text = None

    def inspect_device(self, device_path: str) -> tuple[BlockDeviceFacts | None, str | None]:
        del device_path
        return self.dev, self.dev_err

    def stat_path(self, path: str) -> CachePathKind:
        if path.endswith(".key"):
            if self.non_regular_keyfile:
                return CachePathKind(exists=True, is_dir=False, is_symlink=True, is_regular=False)
            ex = path in self.keyfiles
            return CachePathKind(exists=ex, is_dir=False, is_symlink=False, is_regular=ex)
        if path.startswith("/dev/mapper/"):
            return CachePathKind(exists=self.mapper_open, is_dir=False, is_symlink=False, is_regular=False)
        ex = path in self.base_dirs
        return CachePathKind(exists=ex, is_dir=ex, is_symlink=False, is_regular=False)

    def mapper_fs_info(self, mapper_path: str) -> tuple[str, str, str, str | None]:
        del mapper_path
        if not self.mapper_open:
            return "", "", "", None
        uuid = UUID_CACHE_DRIVE if self.mapper_fstype == "btrfs" else ""
        return self.mapper_fstype, uuid, self.mapper_label, self.mapper_luks_uuid

    def luks_uuid(self, device_path: str) -> str | None:
        del device_path
        return (
            SAMPLE_LUKS_UUID
            if self.luks_formatted or (self.dev is not None and self.dev.fstype == "crypto_LUKS")
            else None
        )

    def test_keyfile(self, device_path: str, keyfile_path: str) -> bool:
        del device_path, keyfile_path
        return self.keyfile_unlocks

    def create_keyfile(self, keyfile_path: str) -> None:
        self.keyfiles.add(keyfile_path)

    def remove_file(self, path: str) -> None:
        self.keyfiles.discard(path)

    def luks_format(self, device_path: str, keyfile_path: str) -> str | None:
        del device_path, keyfile_path
        if self.fail_on == "luks_format":
            return "cryptsetup luksFormat failed: injected"
        self.luks_formatted = True
        if self.dev is not None:
            self.dev = _luks_dev()
        return None

    def luks_open(self, device_path: str, mapper_name: str, keyfile_path: str) -> str | None:
        del device_path, mapper_name, keyfile_path
        if self.fail_on == "luks_open":
            return "cryptsetup open failed: injected"
        self.mapper_open = True
        return None

    def luks_close(self, mapper_name: str) -> str | None:
        del mapper_name
        self.close_calls += 1
        if self.fail_on == "luks_close":
            return "cryptsetup close failed: injected"
        self.mapper_open = False
        return None

    def mkfs_btrfs(self, mapper_path: str, label: str) -> str | None:
        del mapper_path
        if self.fail_on == "mkfs_btrfs":
            return "mkfs.btrfs failed: injected"
        self.mapper_fstype = "btrfs"
        self.mapper_label = label
        return None

    @contextlib.contextmanager
    def top_mount(self, mapper_path: str) -> Generator[tuple[str | None, str | None]]:
        del mapper_path
        yield "/run/fake-top", None

    def list_top_subvolumes(self, mapper_path: str) -> tuple[tuple[str, ...], str | None]:
        del mapper_path
        return tuple(sorted(self.subvols)), None

    def ensure_subvolumes(self, mapper_path: str, names: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
        del mapper_path
        if self.fail_on == "ensure_subvolumes":
            return (), "btrfs subvolume create failed: injected"
        created = tuple(n for n in names if n not in self.subvols)
        self.subvols.update(created)
        return created, None

    def ensure_base_dir(self, path: str) -> None:
        self.base_dirs.add(path)

    def remove_empty_dir(self, path: str) -> None:
        self.base_dirs.discard(path)

    def mount_base(self, mapper_path: str, kind: CacheMountKind, target: str) -> str | None:
        if self.fail_on == f"mount:{target}":
            return f"mount {mapper_path} {target} failed: injected"
        self.mounts[target] = (mapper_path, kind)
        if self.world is not None:
            self.world.table.unbind(target)
            self.world.table.add_mount(
                mapper_path,
                target,
                fsroot=f"/{SUBVOL_FOR_KIND[kind]}",
                uuid=UUID_CACHE_DRIVE,
            )
            self.world.table.set_options(target, f"rw,{BASE_MOUNT_OPTIONS}")
        return None

    def remount_base(self, target: str) -> str | None:
        self.remount_calls.append(target)
        if self.fail_on == f"remount:{target}":
            return f"mount -o remount,{OPT_COMPRESS_FORCE} {target} failed: injected"
        if self.world is not None:
            self.world.table.set_options(target, f"rw,{BASE_MOUNT_OPTIONS}")
        return None

    def umount_base(self, target: str) -> str | None:
        if self.fail_on == f"umount:{target}":
            return f"umount {target} failed: target is busy"
        self.mounts.pop(target, None)
        if self.world is not None:
            self.world.table.unbind(target)
        return None

    def scrub_base(self, target: str) -> str | None:
        self.scrub_calls.append(target)
        if self.fail_on == "scrub_base":
            return f"btrfs scrub start -B {target} failed: injected"
        return None

    def confirm_format(self, prompt: str, *, assume_yes: bool) -> tuple[bool, str | None]:
        del prompt
        if assume_yes:
            return True, None
        return self.confirm_answer


@dataclass
class FakeMigrationPort:
    backup_present: bool = False
    dest_nonempty: bool = False
    is_subvol: bool = False
    busy: tuple[BusyProcess, ...] = ()
    busy_err: str | None = None
    copy_err: str | None = None
    fail_on: str | None = None
    events: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    target_dir_exists: bool = True

    def backup_exists(self, backup_path: str) -> bool:
        del backup_path
        return self.backup_present

    def source_nonempty(self, source_path: str) -> bool:
        del source_path
        return self.dest_nonempty

    def is_btrfs_subvol(self, target_path: str) -> bool:
        del target_path
        return self.is_subvol

    def busy_processes(self, target_path: str) -> tuple[tuple[BusyProcess, ...], str | None]:
        del target_path
        return self.busy, self.busy_err

    def copy_tree(self, src: str, dst: str) -> str | None:
        self.events.append(f"copy:{src}->{dst}")
        return self.copy_err

    def clear_dir_contents(self, path: str) -> None:
        self.events.append(f"clear:{path}")
        self.cleared.append(path)

    def rename(self, old: str, new: str) -> None:
        if self.fail_on == "rename":
            raise OSError(errno.EIO, "rename failed")
        self.events.append(f"rename:{old}->{new}")
        if new.endswith(MIGRATE_BACKUP_SUFFIX):
            self.backup_present = True
            self.target_dir_exists = False
        elif old.endswith(MIGRATE_BACKUP_SUFFIX):
            self.backup_present = False
            self.target_dir_exists = True

    def ensure_empty_dir(self, path: str, user: CacheMountUser) -> None:
        del user
        if self.fail_on == "ensure_empty_dir":
            raise OSError(errno.EACCES, "mkdir failed")
        self.events.append(f"empty_dir:{path}")
        self.target_dir_exists = True

    def remove_empty_dir(self, path: str) -> None:
        if self.fail_on == "remove_empty_dir":
            raise OSError(errno.EBUSY, "rmdir failed")
        self.events.append(f"rmdir:{path}")
        self.target_dir_exists = False


class ProvisionDomainTests(unittest.TestCase):
    def test_parse_args_for_provision_unprovision_and_migrate(self) -> None:
        p = parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1", "--mapper", "cache2"])
        self.assertEqual(
            (p.action, p.provision_action, p.device, p.mapper, p.error),
            (CacheMountAction.PROVISION, ProvisionSubcommand.INIT, "/dev/nvme2n1p1", "cache2", None),
        )
        self.assertEqual(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]).do_format, True)
        self.assertEqual(parse_cache_mount_args(["provision", "init"]).code, 2)
        self.assertEqual(parse_cache_mount_args(["provision", "apply", "--device", "/dev/x"]).code, 2)
        self.assertEqual(parse_cache_mount_args(["unprovision", "--yes"]).code, 2)
        self.assertEqual(parse_cache_mount_args(["plan", "--migrate", "--allow-busy"]).allow_busy, True)
        self.assertEqual(parse_cache_mount_args(["detach", "--migrate"]).code, 2)
        self.assertEqual(parse_cache_mount_args(["provision", "--help"]).provision_action, ProvisionSubcommand.HELP)

    def test_parse_cache_conf_validation(self) -> None:
        good = render_cache_conf(_sample_spec())
        res = parse_cache_conf(good)
        self.assertEqual(res.problems, ())
        self.assertEqual(res.spec, _sample_spec())

        for bad_text in (
            "",
            "CACHE_PARTUUID=BAD-UPPERCASE\nCACHE_SIZE_BYTES=100\n",
            f"CACHE_PARTUUID={SAMPLE_PARTUUID}\nCACHE_SIZE_BYTES=0\n",
            f"CACHE_PARTUUID={SAMPLE_PARTUUID}\nCACHE_SIZE_BYTES=500\nUNKNOWN_KEY=1\n",
            f"CACHE_PARTUUID={SAMPLE_PARTUUID}\nCACHE_SIZE_BYTES=500\nCACHE_MAPPER_NAME=bad/name\n",
        ):
            self.assertGreater(len(parse_cache_conf(bad_text).problems), 0, bad_text)

    def test_byte_transparent_crypttab_and_base_fstab_edits(self) -> None:
        spec = _sample_spec()
        other_crypt = b"luks-root UUID=1111 none discard\n\xff\n".decode("utf-8", "surrogateescape")
        with_cache = plan_provision_crypttab(other_crypt, spec, SAMPLE_LUKS_UUID)
        self.assertEqual(with_cache.count("cache UUID="), 1)
        self.assertIn(other_crypt, with_cache)
        self.assertEqual(plan_provision_crypttab(with_cache, spec, SAMPLE_LUKS_UUID), with_cache)
        self.assertEqual(plan_unprovision_crypttab(with_cache, "cache"), other_crypt)

        user_fstab = (
            "UUID=root / btrfs defaults 0 0\n"
            "/var/mnt/cache/u /var/home/u/.cache none bind,nofail 0 0 # managed-by: btrfs-cache (CST) user=u kind=cache\n"
        )
        with_bases = plan_provision_fstab(user_fstab, spec)
        self.assertIn(OPT_COMPRESS_FORCE, render_base_fstab_line(spec, CacheMountKind.CACHE))
        self.assertIn(render_base_fstab_line(spec, CacheMountKind.CACHE), with_bases)
        self.assertIn(render_base_fstab_line(spec, CacheMountKind.SCRATCHPAD), with_bases)
        self.assertEqual(plan_unprovision_fstab(with_bases), user_fstab)

    def test_validate_device_for_init_safety_guards(self) -> None:
        spec = _sample_spec()
        self.assertEqual(validate_device_for_init(_blank_dev(), existing_conf=None, mapper_name="cache"), [])
        self.assertIn(
            "type disk",
            validate_device_for_init(_blank_dev(dev_type="disk"), existing_conf=None, mapper_name="cache")[0],
        )
        self.assertIn(
            "no PARTUUID", validate_device_for_init(_blank_dev(partuuid=""), existing_conf=None, mapper_name="cache")[0]
        )
        swap_dev = BlockDeviceFacts(
            "/dev/nvme2n1p2", "/dev/nvme2n1p2", "part", SAMPLE_PARTUUID, SAMPLE_SIZE, fstype="swap", is_swap=True
        )
        self.assertIn("swap", validate_device_for_init(swap_dev, existing_conf=None, mapper_name="cache")[0])
        sys_dev = BlockDeviceFacts(
            "/dev/nvme0n1p3", "/dev/nvme0n1p3", "part", SAMPLE_PARTUUID, SAMPLE_SIZE, mountpoints=("/", "/sysroot")
        )
        self.assertIn(
            "system filesystem", validate_device_for_init(sys_dev, existing_conf=None, mapper_name="cache")[0]
        )
        lvm_dev = BlockDeviceFacts(
            "/dev/nvme2n1p1",
            "/dev/nvme2n1p1",
            "part",
            SAMPLE_PARTUUID,
            SAMPLE_SIZE,
            holders=("dm-0",),
            holder_dm_names=("vg-lv",),
        )
        self.assertIn("active holder", validate_device_for_init(lvm_dev, existing_conf=None, mapper_name="cache")[0])
        self.assertIn(
            "already names", validate_device_for_init(_blank_dev(size=123), existing_conf=spec, mapper_name="cache")[0]
        )

    def test_validate_provision_apply_anti_wipe_checks(self) -> None:
        spec = _sample_spec()
        self.assertIn("not found", validate_provision_apply(_probe(), do_format=False)[0])
        self.assertIn("--format", validate_provision_apply(_probe(spec=spec, dev=_blank_dev()), do_format=False)[0])
        wrong_sz = _blank_dev(size=SAMPLE_SIZE - 1)
        self.assertIn("does not match", validate_provision_apply(_probe(spec=spec, dev=wrong_sz), do_format=True)[0])
        ext4 = BlockDeviceFacts(spec.device_path, "/dev/nvme2n1p1", "part", SAMPLE_PARTUUID, SAMPLE_SIZE, fstype="ext4")
        self.assertIn("refusing to wipe", validate_provision_apply(_probe(spec=spec, dev=ext4), do_format=True)[0])
        wrong_pu = _blank_dev(partuuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertIn("PARTUUID", validate_provision_apply(_probe(spec=spec, dev=wrong_pu), do_format=True)[0])
        bad_luks = LuksMapperState(keyfile_exists=True, keyfile_unlocks_luks=False)
        self.assertIn(
            "does not accept",
            validate_provision_apply(_probe(spec=spec, dev=_luks_dev(), luks=bad_luks), do_format=True)[0],
        )
        non_luks = LuksMapperState(keyfile_exists=True, mapper_exists=True, mapper_luks_uuid=None)
        self.assertIn(
            "not an open LUKS mapping",
            validate_provision_apply(_probe(spec=spec, dev=_luks_dev(), luks=non_luks), do_format=True)[0],
        )
        bad_label = LuksMapperState(
            keyfile_exists=True,
            mapper_exists=True,
            luks_uuid=SAMPLE_LUKS_UUID,
            mapper_luks_uuid=SAMPLE_LUKS_UUID,
            mapper_fstype="btrfs",
            mapper_label="fedora",
        )
        self.assertIn(
            "label 'fedora'",
            validate_provision_apply(_probe(spec=spec, dev=_luks_dev(), luks=bad_label), do_format=False)[0],
        )
        foreign_base = {
            CacheMountKind.CACHE: ProvisionBaseState(
                CacheMountKind.CACHE,
                dir_exists=True,
                dir_is_symlink=False,
                dir_not_dir=False,
                mounted_source="/dev/nvme0n1p3[/cache]",
                mounted_fstype="btrfs",
                mounted_uuid="foreign-uuid",
                mounted_fsroot="/cache",
            ),
            CacheMountKind.SCRATCHPAD: ProvisionBaseState(
                CacheMountKind.SCRATCHPAD,
                dir_exists=False,
                dir_is_symlink=False,
                dir_not_dir=False,
            ),
        }
        p_foreign = ProvisionProbe(
            render_cache_conf(spec),
            None,
            spec,
            (),
            _blank_dev(),
            None,
            LuksMapperState(),
            "",
            None,
            "",
            None,
            foreign_base,
            user_managed_entries=(),
        )
        self.assertIn("already mounted from", validate_provision_apply(p_foreign, do_format=True)[0])

    def test_validate_unprovision_and_migration(self) -> None:
        base_p = _probe(spec=_sample_spec())
        p_with_user = ProvisionProbe(
            base_p.conf_text,
            None,
            base_p.spec,
            (),
            base_p.device,
            None,
            base_p.luks,
            "",
            None,
            "",
            None,
            base_p.bases,
            user_managed_entries=(mock.MagicMock(user="alice"),),
        )
        self.assertIn("detach --user alice", validate_unprovision(p_with_user)[0])
        self.assertIn("btrfs subvolume delete", cleanup_hint("/home/u/.cache.pre-cst-cache", is_subvol=True))
        self.assertIn("rm -rf", cleanup_hint("/home/u/.cache.pre-cst-cache", is_subvol=False))
        self.assertEqual(backup_path_for("/var/home/u/.cache/"), "/var/home/u/.cache.pre-cst-cache")


class ProvisionApplicationRollbackTests(unittest.TestCase):
    def _world(self) -> tuple[CacheMounts, CacheWorld, FakeProvisionPort, FakeMigrationPort]:
        w = CacheWorld()
        w.caller = "tester"
        w.put_fstab("UUID=root / btrfs defaults 0 0\n")
        w.add_user("tester", 1000, "/var/home/tester")
        w.put_file("/var/home/tester/.cache/existing.bin")
        prov = FakeProvisionPort(world=w)
        mig = FakeMigrationPort()
        app = CacheMounts(
            fstab=FakeFstab(w),
            mounts=FakeCacheMountTable(w),
            systemd=FakeCacheSystemd(w),
            folders=FakeCacheFolders(w),
            host=FakeCacheHostView(w),
            accounts=FakeCacheAccounts(w),
            provision=prov,
            migration=mig,
        )
        return app, w, prov, mig

    def test_provision_three_step_cycle_idempotency_and_unprovision(self) -> None:
        app, w, prov, _ = self._world()
        rc0, out0 = app.run(
            parse_cache_mount_args(["provision", "plan", "--device", "/dev/nvme2n1p1"]), "cst btrfs cache"
        )
        self.assertEqual(rc0, 0)
        self.assertIn("CACHE_PARTUUID=", "\n".join(out0))
        self.assertIsNone(prov.conf_text)

        rc_i, out_i = app.run(
            parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "cst btrfs cache"
        )
        self.assertEqual((rc_i, prov.luks_formatted), (0, False), out_i)
        self.assertIn("Wrote /etc/cst/btrfs-cache.conf", "\n".join(out_i))
        rc_i2, out_i2 = app.run(
            parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "cst btrfs cache"
        )
        self.assertEqual(rc_i2, 0)
        self.assertIn("nothing to do", "\n".join(out_i2))

        rc_no_fmt, err_no_fmt = app.run(parse_cache_mount_args(["provision", "apply"]), "cst btrfs cache")
        self.assertEqual(rc_no_fmt, 1)
        self.assertIn("--format", "\n".join(err_no_fmt))

        prov.confirm_answer = (False, "stdin is not a terminal; pass --yes")
        rc_no_yes, err_no_yes = app.run(parse_cache_mount_args(["provision", "apply", "--format"]), "cst btrfs cache")
        self.assertEqual(rc_no_yes, 1)
        self.assertIn("--yes", "\n".join(err_no_yes))

        rc_pf, out_pf = app.run(parse_cache_mount_args(["provision", "plan", "--format"]), "cst btrfs cache")
        self.assertEqual(rc_pf, 0)
        self.assertIn("run initial btrfs scrub on /var/mnt/cache", "\n".join(out_pf))

        rc_a, out_a = app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "cst btrfs cache")
        self.assertEqual(rc_a, 0, out_a)
        self.assertTrue(prov.luks_formatted and prov.mapper_open)
        self.assertEqual(prov.subvols, {"cache", "scratchpad"})
        self.assertIn(CACHE_BASE, prov.mounts)
        self.assertIn(SCRATCHPAD_BASE, prov.mounts)
        self.assertIn(SAMPLE_LUKS_UUID, prov.crypttab_text or "")
        self.assertIn(BTRFS_CACHE_LABEL, w.fstab or "")
        self.assertEqual(prov.scrub_calls, [CACHE_BASE])
        self.assertIn("Completed initial scrub on /var/mnt/cache.", "\n".join(out_a))

        rc_a2, out_a2 = app.run(parse_cache_mount_args(["provision", "apply"]), "cst btrfs cache")
        self.assertEqual(rc_a2, 0)
        self.assertIn("nothing to do", "\n".join(out_a2))
        self.assertEqual(prov.scrub_calls, [CACHE_BASE])

        rc_u, out_u = app.run(parse_cache_mount_args(["unprovision"]), "cst btrfs cache")
        self.assertEqual(rc_u, 0, out_u)
        self.assertFalse(prov.mapper_open)
        self.assertEqual(prov.mounts, {})
        self.assertEqual(prov.crypttab_text, "")
        self.assertNotIn("/var/mnt/cache", w.fstab or "")
        self.assertIsNotNone(prov.conf_text)
        self.assertEqual(len(prov.keyfiles), 1)

    def test_provision_apply_rollback_on_each_failure(self) -> None:
        app, w, prov, _ = self._world()
        self.assertEqual(
            app.run(parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "p")[0], 0
        )

        prov.fail_on = "luks_format"
        rc1, err1 = app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")
        self.assertEqual(rc1, 1)
        self.assertIn("rolled back cleanly", "\n".join(err1))
        self.assertEqual(prov.keyfiles, set())

        prov.fail_on = "mkfs_btrfs"
        rc2, err2 = app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")
        self.assertEqual(rc2, 1)
        self.assertFalse(prov.mapper_open)
        self.assertEqual(len(prov.keyfiles), 1)
        self.assertIn("rolled back cleanly", "\n".join(err2))

        prov.fail_on = f"mount:{SCRATCHPAD_BASE}"
        rc3, _err3 = app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")
        self.assertEqual(rc3, 1)
        self.assertEqual(prov.mounts, {})
        self.assertFalse(prov.mapper_open)
        self.assertEqual(prov.crypttab_text, "")

        prov.fail_on = None
        before_fstab = w.fstab
        w.fail("systemctl daemon-reload", RELOAD_FAILED, times=1)
        rc4, err4 = app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")
        self.assertEqual(rc4, 1)
        self.assertIn("rolled back cleanly", "\n".join(err4))
        self.assertEqual(w.fstab, before_fstab)
        self.assertEqual(prov.crypttab_text, "")
        self.assertEqual(prov.mounts, {})
        self.assertFalse(prov.mapper_open)
        self.assertEqual(prov.scrub_calls, [])

    def test_provision_apply_initial_scrub_failure_warns_without_rollback(self) -> None:
        app, w, prov, _ = self._world()
        self.assertEqual(
            app.run(parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "p")[0], 0
        )
        prov.fail_on = "scrub_base"
        rc, out = app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")
        self.assertEqual(rc, 0, out)
        joined = "\n".join(out)
        self.assertIn("WARNING: initial scrub on /var/mnt/cache failed", joined)
        self.assertIn("sudo btrfs scrub start -B /var/mnt/cache", joined)
        self.assertIn(CACHE_BASE, prov.mounts)
        self.assertIn(SCRATCHPAD_BASE, prov.mounts)
        self.assertIn("provision=base", w.fstab or "")

    def test_migrate_busy_blocking_and_rollback(self) -> None:
        app, w, _prov, mig = self._world()
        self.assertEqual(
            app.run(parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "p")[0], 0
        )
        self.assertEqual(app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")[0], 0)

        mig.busy = (BusyProcess(pid=4321, comm="rustc", path="/var/home/tester/.cache/cargo"),)
        rc_plan, out_plan = app.run(parse_cache_mount_args(["plan", "--migrate"]), "cst btrfs cache")
        self.assertEqual(rc_plan, 0)
        self.assertIn("BLOCKED:", "\n".join(out_plan))
        self.assertIn("rustc (pid 4321)", "\n".join(out_plan))

        rc_busy, err_busy = app.run(parse_cache_mount_args(["attach", "--migrate"]), "cst btrfs cache")
        self.assertEqual(rc_busy, 1)
        self.assertIn("--allow-busy", "\n".join(err_busy))

        mig.copy_err = "cp -a failed"
        rc_cp, err_cp = app.run(parse_cache_mount_args(["attach", "--migrate", "--allow-busy"]), "p")
        self.assertEqual(rc_cp, 1)
        self.assertIn("left untouched", "\n".join(err_cp))
        self.assertEqual(mig.cleared, ["/var/mnt/cache/tester"])

        mig.copy_err = None
        mig.cleared.clear()
        w.fail("systemctl daemon-reload", RELOAD_FAILED, times=1)
        rc_rel, err_rel = app.run(parse_cache_mount_args(["attach", "--migrate", "--allow-busy"]), "p")
        self.assertEqual(rc_rel, 1)
        self.assertIn("restored.", "\n".join(err_rel))
        self.assertFalse(mig.backup_present)
        self.assertTrue(mig.target_dir_exists)
        self.assertIn("/var/mnt/cache/tester", mig.cleared)

        mig.is_subvol = True
        mig.busy = ()
        rc_ok, out_ok = app.run(parse_cache_mount_args(["attach", "--migrate"]), "cst btrfs cache")
        self.assertEqual(rc_ok, 0, out_ok)
        self.assertIn("sudo btrfs subvolume delete /var/home/tester/.cache.pre-cst-cache", "\n".join(out_ok))
        self.assertIn("user=tester kind=cache", w.fstab or "")

    def test_unprovision_blocks_orphaned_user_bind_and_rolls_back_luks_close(self) -> None:
        app, w, prov, _ = self._world()
        self.assertEqual(
            app.run(parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "p")[0], 0
        )
        self.assertEqual(app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")[0], 0)

        w.table.add_mount(
            "/dev/mapper/cache[/cache/orphan]",
            "/var/home/orphan/.cache",
            fsroot="/cache/orphan",
            uuid=UUID_CACHE_DRIVE,
        )
        rc_b, err_b = app.run(parse_cache_mount_args(["unprovision"]), "p")
        self.assertEqual(rc_b, 1)
        self.assertIn("/var/home/orphan/.cache", "\n".join(err_b))
        w.table.unbind("/var/home/orphan/.cache")

        prov.fail_on = "luks_close"
        rc_u, err_u = app.run(parse_cache_mount_args(["unprovision"]), "p")
        self.assertEqual(rc_u, 1)
        self.assertIn("mounts and configs restored", "\n".join(err_u))
        self.assertIn(CACHE_BASE, prov.mounts)
        self.assertIn(SCRATCHPAD_BASE, prov.mounts)
        self.assertIn("provision=base", w.fstab or "")
        self.assertIn("provision=crypttab", prov.crypttab_text or "")

    def test_migrate_rollback_keeps_mounted_cache_when_umount_fails(self) -> None:
        app, w, _prov, mig = self._world()
        self.assertEqual(
            app.run(parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "p")[0], 0
        )
        self.assertEqual(app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")[0], 0)

        w.fail(
            "mount --bind /var/mnt/scratchpad/tester /var/home/tester/Scratchpad",
            OSError(errno.EIO, "bind scratchpad failed"),
            times=1,
        )
        w.fail("umount /var/home/tester/.cache", OSError(errno.EBUSY, "umount target is busy"), times=1)
        rc, err = app.run(parse_cache_mount_args(["attach", "--migrate"]), "p")
        self.assertEqual(rc, 1)
        self.assertIn("ROLLBACK INCOMPLETE", "\n".join(err))
        self.assertIn("kept /var/home/tester/.cache mounted over", "\n".join(err))
        self.assertEqual(mig.cleared, [])

    def test_provision_upgrade_from_compress_zstd_to_compress_force_remounts_and_rolls_back(self) -> None:
        app, w, prov, _ = self._world()
        self.assertEqual(
            app.run(parse_cache_mount_args(["provision", "init", "--device", "/dev/nvme2n1p1"]), "p")[0], 0
        )
        self.assertEqual(app.run(parse_cache_mount_args(["provision", "apply", "--format", "--yes"]), "p")[0], 0)
        prov.remount_calls.clear()

        # Simulate an older provisioned host still using compress=zstd:1 in /etc/fstab and live mounts.
        old_fstab = (
            "UUID=root / btrfs defaults 0 0\n"
            f"/dev/mapper/cache {CACHE_BASE} btrfs subvol=cache,noatime,discard=async,compress=zstd:1,commit=60,nofail 0 0 {PROVISION_BASE_MARKER} kind=cache\n"
            f"/dev/mapper/cache {SCRATCHPAD_BASE} btrfs subvol=scratchpad,noatime,discard=async,compress=zstd:1,commit=60,nofail 0 0 {PROVISION_BASE_MARKER} kind=scratchpad\n"
        )
        w.put_fstab(old_fstab)
        w.systemd.loaded = (old_fstab, None)
        for target in (CACHE_BASE, SCRATCHPAD_BASE):
            for m in w.table.mounts:
                if m["target"] == target:
                    m["options"] = "rw,noatime,discard=async,compress=zstd:1,commit=60"

        rc_plan, out_plan = app.run(parse_cache_mount_args(["provision", "plan"]), "cst btrfs cache")
        self.assertEqual(rc_plan, 0)
        plan_txt = "\n".join(out_plan)
        self.assertIn("upsert base mount lines in /etc/fstab and reload systemd", plan_txt)
        self.assertIn(f"remount base subvolume(s) with {OPT_COMPRESS_FORCE}: {CACHE_BASE}, {SCRATCHPAD_BASE}", plan_txt)
        self.assertEqual(w.fstab, old_fstab)

        # Remount failure rolls back /etc/fstab and systemd reload cleanly.
        prov.fail_on = f"remount:{CACHE_BASE}"
        rc_fail, err_fail = app.run(parse_cache_mount_args(["provision", "apply"]), "cst btrfs cache")
        self.assertEqual(rc_fail, 1)
        self.assertIn("rolled back cleanly", "\n".join(err_fail))
        self.assertEqual(w.fstab, old_fstab)
        prov.fail_on = None
        prov.remount_calls.clear()

        # Successful upgrade updates /etc/fstab, reloads systemd, and remounts both bases live.
        rc_ok, out_ok = app.run(parse_cache_mount_args(["provision", "apply"]), "cst btrfs cache")
        self.assertEqual(rc_ok, 0, out_ok)
        ok_txt = "\n".join(out_ok)
        self.assertIn(f"Remounted {CACHE_BASE} with {OPT_COMPRESS_FORCE}.", ok_txt)
        self.assertIn(f"Remounted {SCRATCHPAD_BASE} with {OPT_COMPRESS_FORCE}.", ok_txt)
        self.assertEqual(prov.remount_calls, [CACHE_BASE, SCRATCHPAD_BASE])
        self.assertEqual((w.fstab or "").count(OPT_COMPRESS_FORCE), 2)

        # Second run is a no-op.
        prov.remount_calls.clear()
        rc_noop, out_noop = app.run(parse_cache_mount_args(["provision", "apply"]), "cst btrfs cache")
        self.assertEqual(rc_noop, 0, out_noop)
        self.assertIn("nothing to do", "\n".join(out_noop))
        self.assertEqual(prov.remount_calls, [])


class ProvisionAndMigrationAdapterTests(unittest.TestCase):
    def test_real_migration_adapter_busy_processes_and_copy(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-mig-adp-")
        self.addCleanup(shutil.rmtree, tmp)
        home_cache = os.path.join(tmp, "var", "home", "tester", ".cache")
        os.makedirs(os.path.join(home_cache, "sub"), exist_ok=True)
        with open(os.path.join(home_cache, "sub", "data.bin"), "wb") as fh:
            fh.write(b"hello-cache")

        rt = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=mock.MagicMock(), env_map={})
        # Use real subprocess runner only for copy_tree test below; for busy_processes use real /proc!
        real_rt = CacheMountRuntime(seam_root="", ctx=CacheTestCtx(), runner=rt.runner, env_map={})
        real_mig = CacheMigrationAdapter(real_rt)

        # Spawn a child holding an open fd inside home_cache (stdin=DEVNULL per subprocess hygiene)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys, time; fh=open(sys.argv[1]); print('READY', flush=True); time.sleep(30)",
                os.path.join(home_cache, "sub", "data.bin"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        self.addCleanup(proc.stdout.close)
        ready = proc.stdout.readline().strip()
        self.assertEqual(ready, "READY")

        busy, err = real_mig.busy_processes(home_cache)
        self.assertIsNone(err)
        self.assertTrue(any(p.pid == proc.pid for p in busy), f"expected pid {proc.pid} in {busy}")

        # seam_root without <seam>/proc falls back to host /proc and matches mapped target path.
        seam_mig = CacheMigrationAdapter(rt)
        seam_busy, seam_err = seam_mig.busy_processes("/var/home/tester/.cache")
        self.assertIsNone(seam_err)
        self.assertTrue(any(p.pid == proc.pid for p in seam_busy), f"expected pid {proc.pid} in {seam_busy}")

    def test_provision_adapter_guards_fifo_keyfile_and_lsblk(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-prov-adp-")
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "etc", "cst"), exist_ok=True)
        conf_fifo = os.path.join(tmp, "etc", "cst", "btrfs-cache.conf")
        os.mkfifo(conf_fifo)

        runner = FakeMountRunner(tmp)

        def fake_run(argv: Sequence[str], **_kwargs: object) -> CommandResult:
            if argv[0] == "lsblk":
                payload = {
                    "blockdevices": [
                        {
                            "name": "/dev/nvme2n1p1",
                            "type": "part",
                            "partuuid": SAMPLE_PARTUUID,
                            "size": SAMPLE_SIZE,
                            "model": "WD Black",
                            "fstype": "crypto_LUKS",
                            "pttype": None,
                            "uuid": SAMPLE_LUKS_UUID,
                            "mountpoints": [None],
                        }
                    ]
                }
                return CommandResult(0, json.dumps(payload), "")
            if argv[0] == "blkid":
                return CommandResult(0, "TYPE=btrfs\nUUID=u1\nLABEL=cache\n", "")
            if argv[0] == "dmsetup":
                hex_u = SAMPLE_LUKS_UUID.replace("-", "")
                return CommandResult(0, f"CRYPT-LUKS2-{hex_u}-cache\n", "")
            return CommandResult(0, "", "")

        with mock.patch.object(runner, "run", side_effect=fake_run):
            rt = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=runner, env_map={})
            prov = CacheProvisionAdapter(rt)
            _, fifo_err = prov.read_conf()
            self.assertIn("not a regular file", fifo_err or "")

            kf = "/etc/cryptsetup-keys.d/cache.key"
            prov.create_keyfile(kf)
            real_kf = os.path.join(tmp, kf.lstrip("/"))
            self.assertEqual(stat.S_IMODE(os.stat(real_kf).st_mode), 0o400)
            with self.assertRaises(FileExistsError):
                prov.create_keyfile(kf)

            facts, dev_err = prov.inspect_device("/dev/nvme2n1p1")
            self.assertIsNone(dev_err)
            assert facts is not None
            self.assertEqual(
                (facts.partuuid, facts.size_bytes, facts.fstype), (SAMPLE_PARTUUID, SAMPLE_SIZE, "crypto_LUKS")
            )
            mapper_dev = os.path.join(tmp, "dev", "mapper", "cache")
            os.makedirs(os.path.dirname(mapper_dev), exist_ok=True)
            with open(mapper_dev, "w", encoding="utf-8") as mfh:
                mfh.write("")
            fstype, uuid, label, luks_u = prov.mapper_fs_info("/dev/mapper/cache")
            self.assertEqual((fstype, uuid, label, luks_u), ("btrfs", "u1", "cache", SAMPLE_LUKS_UUID))

    def test_provision_adapter_blkid_partuuid_and_ambivalent(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-prov-blkid-")
        self.addCleanup(shutil.rmtree, tmp)
        runner = FakeMountRunner(tmp)

        # Without udev (lsblk partuuid=None), inspect_device reads PART_ENTRY_UUID from blkid and lowercases it.
        def no_udev_run(argv: Sequence[str], **_kwargs: object) -> CommandResult:
            if argv[0] == "lsblk":
                payload = {
                    "blockdevices": [
                        {
                            "name": "/dev/loop0p1",
                            "type": "part",
                            "partuuid": None,
                            "size": SAMPLE_SIZE,
                            "model": None,
                            "fstype": None,
                            "pttype": None,
                            "uuid": None,
                            "mountpoints": [None],
                        }
                    ]
                }
                return CommandResult(0, json.dumps(payload), "")
            if argv[0] == "blkid":
                return CommandResult(0, f"PART_ENTRY_UUID={SAMPLE_PARTUUID.upper()}\n", "")
            return CommandResult(0, "", "")

        with mock.patch.object(runner, "run", side_effect=no_udev_run):
            rt2 = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=runner, env_map={})
            facts2, err2 = CacheProvisionAdapter(rt2).inspect_device("/dev/loop0p1")
            self.assertIsNone(err2)
            assert facts2 is not None
            self.assertEqual(facts2.partuuid, SAMPLE_PARTUUID)

        def ambivalent_run(argv: Sequence[str], **_kwargs: object) -> CommandResult:
            if argv[0] == "lsblk":
                return no_udev_run(argv)
            if argv[0] == "blkid":
                return CommandResult(8, "", "blkid: ambivalent result")
            return CommandResult(0, "", "")

        with mock.patch.object(runner, "run", side_effect=ambivalent_run):
            rt3 = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=runner, env_map={})
            facts3, err3 = CacheProvisionAdapter(rt3).inspect_device("/dev/loop0p1")
            self.assertIsNone(facts3)
            self.assertIn("ambivalent", err3 or "")

    def test_migration_adapter_copy_clear_rename_empty_dir(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-mig-ops-")
        self.addCleanup(shutil.rmtree, tmp)
        rt = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=FakeMountRunner(tmp), env_map={})
        mig = CacheMigrationAdapter(rt)
        src = "/var/home/u/.cache"
        dst = "/var/mnt/cache/u"
        msrc = os.path.join(tmp, src.lstrip("/"))
        mdst = os.path.join(tmp, dst.lstrip("/"))
        os.makedirs(os.path.join(msrc, "nested"), exist_ok=True)
        os.makedirs(mdst, exist_ok=True)
        with open(os.path.join(msrc, ".dot"), "w", encoding="utf-8") as fh:
            fh.write("dot")
        with open(os.path.join(msrc, "nested", "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("val")
        user = CacheMountUser("u", os.getuid(), os.getgid(), "/var/home/u")

        def real_cp_run(argv: Sequence[str], **_kwargs: object) -> CommandResult:
            cp = subprocess.run(list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
            return CommandResult(cp.returncode, cp.stdout, cp.stderr)

        with mock.patch.object(rt.runner, "run", side_effect=real_cp_run):
            self.assertIsNone(mig.copy_tree(src, dst))
        self.assertTrue(os.path.isfile(os.path.join(mdst, ".dot")))
        self.assertTrue(os.path.isfile(os.path.join(mdst, "nested", "f.txt")))

        mig.clear_dir_contents(dst)
        self.assertEqual(os.listdir(mdst), [])

        backup = backup_path_for(src)
        mig.rename(src, backup)
        self.assertFalse(os.path.exists(msrc))
        self.assertTrue(mig.backup_exists(backup))
        mig.ensure_empty_dir(src, user)
        self.assertEqual(os.listdir(msrc), [])
        mig.remove_empty_dir(src)
        self.assertFalse(os.path.exists(msrc))

    def test_provision_adapter_ignores_parent_lsblk_pttype_on_partition(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-prov-pttype-")
        self.addCleanup(shutil.rmtree, tmp)
        runner = FakeMountRunner(tmp)
        spec = _sample_spec()

        def inspect_case(dev_type: str, lsblk_pttype: str | None, blkid_out: str) -> BlockDeviceFacts:
            def run_fn(argv: Sequence[str], **_kwargs: object) -> CommandResult:
                if argv[0] == "lsblk":
                    payload = {
                        "blockdevices": [
                            {
                                "name": "/dev/nvme2n1p1",
                                "type": dev_type,
                                "partuuid": SAMPLE_PARTUUID,
                                "size": SAMPLE_SIZE,
                                "model": "WD Black",
                                "fstype": None,
                                "pttype": lsblk_pttype,
                                "uuid": None,
                                "mountpoints": [],
                            }
                        ]
                    }
                    return CommandResult(0, json.dumps(payload), "")
                if argv[0] == "blkid":
                    return CommandResult(0 if blkid_out else 2, blkid_out, "")
                return CommandResult(0, "", "")

            with mock.patch.object(runner, "run", side_effect=run_fn):
                rt = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=runner, env_map={})
                facts, err = CacheProvisionAdapter(rt).inspect_device(spec.device_path)
            self.assertIsNone(err)
            assert facts is not None
            return facts

        blank_gpt_part = inspect_case("part", "gpt", f"PART_ENTRY_SCHEME=gpt\nPART_ENTRY_UUID={SAMPLE_PARTUUID}\n")
        self.assertEqual((blank_gpt_part.pttype, blank_gpt_part.fstype), ("", ""))
        self.assertEqual(validate_provision_apply(_probe(spec=spec, dev=blank_gpt_part), do_format=True), [])

        nested_pt_part = inspect_case(
            "part", "gpt", f"PART_ENTRY_SCHEME=gpt\nPTTYPE=dos\nPART_ENTRY_UUID={SAMPLE_PARTUUID}\n"
        )
        self.assertEqual(nested_pt_part.pttype, "dos")
        self.assertIn(
            "refusing to wipe", validate_provision_apply(_probe(spec=spec, dev=nested_pt_part), do_format=True)[0]
        )

        whole_disk = inspect_case("disk", "gpt", "")
        self.assertEqual(whole_disk.pttype, "gpt")

    def test_confirm_format_accepts_y_yes_s_and_declines_n(self) -> None:
        class _FakeTty(io.StringIO):
            @override
            def isatty(self) -> bool:
                return True

        rt = CacheMountRuntime(seam_root="", ctx=CacheTestCtx(), runner=FakeMountRunner("/"), env_map={})
        prov = CacheProvisionAdapter(rt)

        self.assertEqual(prov.confirm_format("Continue? [y/N]: ", assume_yes=True), (True, None))
        with mock.patch.object(sys, "stdin", io.StringIO("y\n")):
            ok_non_tty, err_non_tty = prov.confirm_format("Continue? [y/N]: ", assume_yes=False)
        self.assertFalse(ok_non_tty)
        self.assertIn("--yes", err_non_tty or "")

        for answer, want in (
            ("y\n", True),
            ("Y\n", True),
            ("yes\n", True),
            ("s\n", True),
            ("n\n", False),
            ("\n", False),
        ):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", _FakeTty(answer)), mock.patch.object(sys, "stdout", out):
                res = prov.confirm_format("Continue? [y/N]: ", assume_yes=False)
            self.assertEqual(res[0], want, answer)
            if not want:
                self.assertEqual(res[1], "aborted by user (confirmation declined)")

    def test_provision_adapter_scrub_base_success_and_failure(self) -> None:
        runner = FakeMountRunner("/")
        rt = CacheMountRuntime(seam_root="", ctx=CacheTestCtx(), runner=runner, env_map={})
        prov = CacheProvisionAdapter(rt)

        with mock.patch.object(runner, "run", return_value=CommandResult(0, "", "")) as m_ok:
            self.assertIsNone(prov.scrub_base(CACHE_BASE))
        self.assertEqual(m_ok.call_args.args[0], ["btrfs", "scrub", "start", "-B", CACHE_BASE])

        with mock.patch.object(runner, "run", return_value=CommandResult(1, "", "io error")):
            err = prov.scrub_base(CACHE_BASE)
        self.assertIn("btrfs scrub start -B /var/mnt/cache", err or "")

    def test_provision_adapter_remount_base_argv_and_failure(self) -> None:
        runner = FakeMountRunner("/")
        rt = CacheMountRuntime(seam_root="", ctx=CacheTestCtx(), runner=runner, env_map={})
        prov = CacheProvisionAdapter(rt)

        with mock.patch.object(runner, "run", return_value=CommandResult(0, "", "")) as m_ok:
            self.assertIsNone(prov.remount_base(CACHE_BASE))
        self.assertEqual(m_ok.call_args.args[0], ["mount", "-o", f"remount,{OPT_COMPRESS_FORCE}", CACHE_BASE])

        with mock.patch.object(runner, "run", return_value=CommandResult(1, "", "busy")):
            err = prov.remount_base(CACHE_BASE)
        self.assertIn(f"mount -o remount,{OPT_COMPRESS_FORCE} {CACHE_BASE}", err or "")
