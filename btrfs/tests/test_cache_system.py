# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs cache system` domain, application lifecycle, rollbacks, and adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import os
import stat
import tempfile
import unittest
from unittest import mock

from btrfs.cache.adapters.mounts import CacheMountRuntime
from btrfs.cache.adapters.system import CacheSystemBindAdapter
from btrfs.cache.application.mounts import CacheMounts
from btrfs.cache.application.system import SystemEnv, run_system_action
from btrfs.cache.domain import (
    CACHE_BASE,
    CACHE_MOUNT_MARKER,
    BaseMountInfo,
    CacheMountAction,
    CacheMountKind,
    CacheMountParsedArgs,
    LiveMount,
    parse_cache_mount_args,
    unmanaged_fstab_targets,
)
from btrfs.cache.domain.health import CacheMountFindingSeverity, assess_cache_mount_health
from btrfs.cache.domain.provision import ProvisionBaseState, ProvisionProbe, validate_unprovision
from btrfs.cache.domain.system import (
    SystemBindProbe,
    SystemCacheSnapshot,
    format_system_plan,
    format_system_status,
    is_system_bind_from,
    parse_system_fstab_entries,
    plan_system_apply_fstab,
    plan_system_revert_fstab,
    render_system_fstab_line,
    should_mount_live,
    validate_system_apply,
    validate_system_revert,
)
from btrfs.cache.domain.system_spec import (
    SYSTEM_SPEC_BY_ID,
    SystemAction,
    SystemArgsResult,
    SystemBindId,
    SystemBindSpec,
    parse_system_args,
)
from btrfs.cache.ports import SystemBindPaths
from btrfs.cache.wiring import run_cache_mount
from btrfs.tests.fakes import (
    RELOAD_FAILED,
    RELOAD_FAILED_TEXT,
    UMOUNT_BUSY,
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
from cst_core.adapters.runtime import ManagedCliEnv
from cst_core.ports import CommandResult

_VAR_UUID = "uuid-root-var-0001"


def _make_base(
    *,
    mounted: bool = True,
    fstype: str = "btrfs",
    uuid: str = UUID_CACHE_DRIVE,
    in_fstab: bool = True,
) -> BaseMountInfo:
    return BaseMountInfo(
        path=CACHE_BASE,
        mounted=mounted,
        fstype=fstype,
        uuid=uuid,
        options="rw",
        in_fstab=in_fstab,
        fstab_options="nofail",
        source="/dev/mapper/cache",
        fsroot="/cache",
    )


def _make_probe(spec: SystemBindSpec, *flags: str) -> SystemBindProbe:
    flag_set = set(flags)
    svc_active: bool | None = None
    if "service_active" in flag_set:
        svc_active = True
    elif "service_inactive" in flag_set:
        svc_active = False
    return SystemBindProbe(
        spec=spec,
        in_fstab="in_fstab" in flag_set,
        mounted_from_source="mounted_from_source" in flag_set,
        mounted_other="mounted_other" in flag_set,
        target_is_symlink="target_is_symlink" in flag_set,
        target_exists="target_missing" not in flag_set,
        target_is_dir="target_missing" not in flag_set and "target_not_dir" not in flag_set,
        target_nonempty="target_nonempty" in flag_set,
        source_is_symlink="source_is_symlink" in flag_set,
        source_exists="source_exists" in flag_set,
        source_is_dir="source_is_dir" in flag_set,
        source_nonempty="source_nonempty" in flag_set,
        fstab_unmanaged="fstab_unmanaged" in flag_set,
        source_unchecked="source_unchecked" in flag_set,
        bind_below="bind_below" in flag_set,
        service_active=svc_active,
    )


@dataclass
class _FakeSystemBinds:
    """In-memory `CacheSystemBindPort` fake wired into a shared `CacheWorld` mount table."""

    world: CacheWorld
    nonempty_dirs: set[str] = field(default_factory=set)
    copied_into: set[str] = field(default_factory=set)
    cleared: list[str] = field(default_factory=list)
    active_services: set[str] = field(default_factory=set)
    stopped_log: list[str] = field(default_factory=list)
    started_log: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    dir_modes: dict[str, int] = field(default_factory=dict)
    fail_copy_for: str = ""
    fail_bind_for: str = ""
    fail_unbind_for: str = ""
    fail_stop_for: str = ""
    fail_start_for: str = ""
    fail_mkdir_for: str = ""
    busy_unbind_for: str = ""

    def probe_paths(self, spec: SystemBindSpec) -> SystemBindPaths:
        tgt_link = spec.target in self.world.links
        tgt_exists = tgt_link or spec.target in self.world.dirs or spec.target in self.world.files
        tgt_dir = spec.target in self.world.dirs and not tgt_link
        src_link = spec.source in self.world.links
        src_exists = src_link or spec.source in self.world.dirs or spec.source in self.world.files
        src_dir = spec.source in self.world.dirs and not src_link
        return SystemBindPaths(
            target_is_link=tgt_link,
            target_exists=tgt_exists,
            target_is_dir=tgt_dir,
            target_nonempty=spec.target in self.nonempty_dirs,
            source_is_link=src_link,
            source_exists=src_exists,
            source_is_dir=src_dir,
            source_nonempty=spec.source in self.nonempty_dirs or spec.source in self.copied_into,
        )

    def ensure_root_dir(self, path: str, mode: int, *, reference_selinux: str = "") -> None:
        del reference_selinux
        if path == self.fail_mkdir_for:
            raise PermissionError(13, "permission denied", path)
        self.world.mkdirs(path)
        self.dir_modes[path] = mode

    def copy_tree(self, src: str, dst: str) -> str | None:
        if self.fail_copy_for in (dst, src):
            return f"cp -a {src}/. {dst} failed: I/O error"
        self.copied_into.add(dst)
        return None

    def clear_dir_contents(self, path: str) -> None:
        self.cleared.append(path)
        self.copied_into.discard(path)

    def service_is_active(self, unit: str) -> bool | None:
        return unit in self.active_services

    def stop_service(self, unit: str) -> str | None:
        if unit == self.fail_stop_for:
            return f"systemctl stop {unit} failed: refused"
        self.events.append(f"stop:{unit}")
        self.stopped_log.append(unit)
        self.active_services.discard(unit)
        return None

    def start_service(self, unit: str) -> str | None:
        if unit == self.fail_start_for:
            return f"systemctl start {unit} failed: refused"
        self.events.append(f"start:{unit}")
        self.started_log.append(unit)
        self.active_services.add(unit)
        return None

    def bind(self, source: str, target: str) -> str | None:
        if target == self.fail_bind_for:
            return f"mount --bind {source} {target} failed: denied"
        self.events.append(f"bind:{target}")
        self.world.table.bind(source, target)
        return None

    def unbind(self, target: str) -> str | None:
        if target == self.busy_unbind_for:
            return f"umount {target} failed: {UMOUNT_BUSY}"
        if target == self.fail_unbind_for:
            return f"umount {target} failed: {UMOUNT_BUSY}"
        self.events.append(f"unbind:{target}")
        self.world.table.unbind(target)
        return None


def _seeded_world() -> tuple[CacheWorld, _FakeSystemBinds, SystemEnv]:
    world = CacheWorld()
    world.table.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache", uuid=UUID_CACHE_DRIVE)
    world.table.add_mount("/dev/dm-0", "/var", fsroot="/var", uuid=_VAR_UUID)
    world.mkdirs("/var/log/below")
    world.mkdirs("/var/log/btrfs-stall-monitor")
    world.mkdirs("/var/tmp")
    world.put_fstab("/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,noatime,compress-force=zstd:1,nofail 0 0\n")
    binds = _FakeSystemBinds(world=world)
    binds.nonempty_dirs.update({"/var/log/below", "/var/log/btrfs-stall-monitor", "/var/tmp"})
    binds.active_services.add("below.service")
    env = SystemEnv(
        binds=binds,
        fstab=FakeFstab(world),
        mounts=FakeCacheMountTable(world),
        systemd=FakeCacheSystemd(world),
    )
    return world, binds, env


def _sys_args(
    action: SystemAction,
    only: tuple[SystemBindId, ...] = (),
    *,
    allow_nonempty: bool = False,
    mount_var_tmp_now: bool = False,
) -> SystemArgsResult:
    return SystemArgsResult(
        action=action,
        only=only,
        allow_nonempty=allow_nonempty,
        mount_var_tmp_now=mount_var_tmp_now,
    )


class SystemSpecAndDomainTest(unittest.TestCase):
    def test_parse_system_args_variants(self) -> None:
        top = parse_cache_mount_args(["system", "apply", "--only", "below"])
        self.assertEqual(top.action, CacheMountAction.SYSTEM)
        self.assertEqual(top.system_argv, ("apply", "--only", "below"))

        default_st = parse_system_args([])
        self.assertEqual(default_st.action, SystemAction.STATUS)
        self.assertIsNone(default_st.error)

        apply_parsed = parse_system_args(
            ["apply", "--only", "below,var-tmp", "--mount-var-tmp-now", "--allow-nonempty"]
        )
        self.assertEqual(apply_parsed.action, SystemAction.APPLY)
        self.assertEqual(apply_parsed.only, (SystemBindId.BELOW, SystemBindId.VAR_TMP))
        self.assertTrue(apply_parsed.mount_var_tmp_now)
        self.assertTrue(apply_parsed.allow_nonempty)

        bad_only = parse_system_args(["plan", "--only=toolboxes"])
        self.assertIsNotNone(bad_only.error)
        self.assertEqual(bad_only.code, 2)

        bad_flag = parse_system_args(["revert", "--mount-var-tmp-now"])
        self.assertIsNotNone(bad_flag.error)

        help_parsed = parse_system_args(["--help"])
        self.assertEqual(help_parsed.action, SystemAction.HELP)

    def test_fstab_plan_parse_and_unmanaged_skip(self) -> None:
        below = SYSTEM_SPEC_BY_ID[SystemBindId.BELOW]
        var_tmp = SYSTEM_SPEC_BY_ID[SystemBindId.VAR_TMP]
        user_line = (
            "/var/mnt/cache/u1 /var/home/u1/.cache none bind,nofail 0 0 "
            "# managed-by: btrfs-cache (CST) user=u1 kind=cache\n"
        )
        other_line = "UUID=abc /boot ext4 defaults 0 2\n"
        planned = plan_system_apply_fstab(other_line + user_line, [below, var_tmp])

        entries = parse_system_fstab_entries(planned)
        self.assertEqual(len(entries), 2)
        self.assertIn(render_system_fstab_line(below), planned)
        self.assertIn("scope=system id=var-tmp", planned)
        self.assertIn(user_line.strip(), planned)
        known = frozenset(e.raw_line for e in entries)
        self.assertEqual(unmanaged_fstab_targets(planned, known_lines=known), frozenset({"/boot"}))

        reverted = plan_system_revert_fstab(planned, {SystemBindId.BELOW})
        self.assertEqual([e.bind_id for e in parse_system_fstab_entries(reverted)], [SystemBindId.VAR_TMP])
        self.assertIn(user_line.strip(), reverted)

    def test_is_system_bind_from_and_should_mount_live(self) -> None:
        base = _make_base()
        live = LiveMount("/dev/mapper/cache", "/var/log/below", "btrfs", "rw", UUID_CACHE_DRIVE, "/cache/system/below")
        wrong = LiveMount("/dev/mapper/cache", "/var/log/below", "btrfs", "rw", UUID_CACHE_DRIVE, "/cache/system/other")
        self.assertTrue(is_system_bind_from(live, base, "below"))
        self.assertFalse(is_system_bind_from(wrong, base, "below"))

        var_tmp_spec = SYSTEM_SPEC_BY_ID[SystemBindId.VAR_TMP]
        nonempty_vt = _make_probe(var_tmp_spec, "target_nonempty")
        empty_vt = _make_probe(var_tmp_spec)
        self.assertFalse(should_mount_live(nonempty_vt, mount_var_tmp_now=False))
        self.assertTrue(should_mount_live(nonempty_vt, mount_var_tmp_now=True))
        self.assertTrue(should_mount_live(empty_vt, mount_var_tmp_now=False))

    def test_validation_and_unprovision_blockers(self) -> None:
        below = SYSTEM_SPEC_BY_ID[SystemBindId.BELOW]
        unmounted_base = _make_base(mounted=False, fstype="", uuid="", in_fstab=False)
        bad_snap = SystemCacheSnapshot(unmounted_base, _VAR_UUID, (), (), "")
        self.assertTrue(validate_system_apply(bad_snap))

        same_uuid_base = _make_base(uuid=_VAR_UUID)
        self.assertIn(
            "shares filesystem UUID",
            validate_system_apply(SystemCacheSnapshot(same_uuid_base, _VAR_UUID, (), (), ""))[0],
        )

        ok_base = _make_base()
        conflict = _make_probe(below, "target_nonempty", "source_exists", "source_is_dir", "source_nonempty")
        snap_conflict = SystemCacheSnapshot(ok_base, _VAR_UUID, (conflict,), (), "")
        self.assertIn("both non-empty", validate_system_apply(snap_conflict, allow_nonempty=False)[0])
        self.assertEqual(validate_system_apply(snap_conflict, allow_nonempty=True), [])

        unchecked = _make_probe(below, "in_fstab", "source_exists", "source_is_dir", "source_unchecked")
        self.assertIn(
            "cannot tell", validate_system_revert(SystemCacheSnapshot(unmounted_base, "", (unchecked,), (), ""))[0]
        )

        unprov = ProvisionProbe(
            conf_text=None,
            conf_error=None,
            spec=None,
            conf_problems=(),
            device=None,
            device_error=None,
            luks=mock.MagicMock(mapper_exists=False),
            crypttab_text=None,
            crypttab_error=None,
            fstab_text="",
            fstab_error=None,
            bases={
                CacheMountKind.CACHE: ProvisionBaseState(
                    CacheMountKind.CACHE, dir_exists=True, dir_is_symlink=False, dir_not_dir=False
                ),
                CacheMountKind.SCRATCHPAD: ProvisionBaseState(
                    CacheMountKind.SCRATCHPAD, dir_exists=True, dir_is_symlink=False, dir_not_dir=False
                ),
            },
            user_managed_entries=(),
            system_managed_entries=parse_system_fstab_entries(render_system_fstab_line(below) + "\n"),
        )
        self.assertTrue(any("system revert --only below" in e for e in validate_unprovision(unprov)))

    def test_health_diagnostics_for_system_binds(self) -> None:
        base_fstab = (
            "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,compress-force=zstd:1,nofail 0 0\n"
            f"{render_system_fstab_line(SYSTEM_SPEC_BY_ID[SystemBindId.BELOW])}\n"
            f"{render_system_fstab_line(SYSTEM_SPEC_BY_ID[SystemBindId.VAR_TMP])}\n"
        )
        base_mounts = (
            "/dev/mapper/cache /var/mnt/cache btrfs rw,compress-force=zstd:1,subvol=/cache\n"
            "/dev/mapper/cache /var/log/below btrfs rw,compress-force=zstd:1,subvol=/cache/system/below\n"
        )
        findings = assess_cache_mount_health(base_fstab, base_mounts, None)
        self.assertEqual([f.severity for f in findings], [CacheMountFindingSeverity.INFO, CacheMountFindingSeverity.OK])
        self.assertIn("next reboot", findings[0].message)
        self.assertIn("2 system bind mount(s) configured", findings[1].message)

        unmounted_below = assess_cache_mount_health(
            base_fstab,
            "/dev/mapper/cache /var/mnt/cache btrfs rw,compress-force=zstd:1,subvol=/cache\n",
            None,
        )
        self.assertEqual(unmounted_below[0].severity, CacheMountFindingSeverity.WARN)
        self.assertEqual(unmounted_below[0].fix, "btrfs cache system apply --only below")

        wrong_mount = assess_cache_mount_health(
            base_fstab,
            "/dev/mapper/cache[/cache] /var/mnt/cache btrfs rw,compress-force=zstd:1\n"
            "/dev/mapper/cache[/cache/wrong] /var/log/below btrfs rw,compress-force=zstd:1\n",
            None,
        )
        self.assertEqual(wrong_mount[0].severity, CacheMountFindingSeverity.WARN)
        self.assertIn("not CST's bind", wrong_mount[0].message)

        user_only = assess_cache_mount_health(
            "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,compress-force=zstd:1,nofail 0 0\n"
            f"/var/mnt/cache/u1 /var/home/u1/.cache none bind,nofail 0 0 {CACHE_MOUNT_MARKER} user=u1 kind=cache\n",
            "/dev/mapper/cache /var/mnt/cache btrfs rw,compress-force=zstd:1,subvol=/cache\n"
            "/dev/mapper/cache /var/home/u1/.cache btrfs rw,subvol=/cache/u1\n",
            None,
        )
        self.assertEqual(
            user_only[0].message,
            "/var/mnt/cache mounted (btrfs, no flushoncommit); 1 user bind mount(s) active",
        )


class SystemLifecycleApplicationTest(unittest.TestCase):
    def test_plan_apply_idempotent_status_and_revert(self) -> None:
        world, binds, env = _seeded_world()
        before_fstab = world.fstab

        rc_plan, plan_lines = run_system_action(_sys_args(SystemAction.PLAN), env, "cst btrfs cache")
        self.assertEqual(rc_plan, 0)
        self.assertEqual(world.fstab, before_fstab)
        self.assertIn("stop below.service, copy /var/log/below -> /var/mnt/cache/system/below", "\n".join(plan_lines))
        self.assertIn("stage in fstab (activates next boot; target non-empty)", "\n".join(plan_lines))

        rc_apply, apply_lines = run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")
        self.assertEqual(rc_apply, 0, apply_lines)
        self.assertEqual(binds.stopped_log, ["below.service"])
        self.assertEqual(binds.started_log, ["below.service"])
        self.assertIn("/var/mnt/cache/system/below", binds.copied_into)
        self.assertIn("/var/mnt/cache/system/btrfs-stall-monitor", binds.copied_into)
        self.assertEqual(binds.dir_modes["/var/mnt/cache/system/below"], 0o755)
        self.assertEqual(binds.dir_modes["/var/mnt/cache/system/btrfs-stall-monitor"], 0o750)
        self.assertEqual(binds.dir_modes["/var/mnt/cache/system/var-tmp"], 0o1777)
        self.assertTrue(world.table.mounted("/var/log/below"))
        self.assertTrue(world.table.mounted("/var/log/btrfs-stall-monitor"))
        self.assertFalse(world.table.mounted("/var/tmp"))
        self.assertIn("Staged /var/tmp in /etc/fstab", "\n".join(apply_lines))

        rc_again, again_lines = run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")
        self.assertEqual(rc_again, 0)
        self.assertEqual(again_lines, ["System cache bind mounts already configured; nothing to do."])

        _, plan_after = run_system_action(_sys_args(SystemAction.PLAN), env, "cst btrfs cache")
        self.assertIn("already configured; nothing to do.", plan_after[-1])

        rc_st, st_lines = run_system_action(_sys_args(SystemAction.STATUS), env, "cst btrfs cache")
        self.assertEqual(rc_st, 0)
        self.assertIn("toolboxes", "\n".join(st_lines))
        self.assertIn("[in fstab (activates on reboot)]", "\n".join(st_lines))

        world.table.add_mount(
            "/dev/mapper/cache", "/var/tmp", fsroot="/cache/system/var-tmp", uuid=UUID_CACHE_DRIVE, bind=True
        )
        binds.busy_unbind_for = "/var/tmp"
        rc_rev, rev_lines = run_system_action(_sys_args(SystemAction.REVERT), env, "cst btrfs cache")
        self.assertEqual(rc_rev, 0, rev_lines)
        self.assertEqual(parse_system_fstab_entries(world.fstab or ""), ())
        self.assertFalse(world.table.mounted("/var/log/below"))
        self.assertFalse(world.table.mounted("/var/log/btrfs-stall-monitor"))
        self.assertIn("stays unmounted on next boot", "\n".join(rev_lines))
        self.assertIn("/var/mnt/cache/system/below", binds.copied_into)

    def test_rollbacks_on_mount_and_reload_and_revert_failures(self) -> None:
        world, binds, env = _seeded_world()
        before_fstab = world.fstab
        binds.fail_bind_for = "/var/log/btrfs-stall-monitor"

        rc_m, lines_m = run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")
        self.assertEqual(rc_m, 1)
        self.assertIn("restored.", lines_m[-1])
        self.assertFalse(world.table.mounted("/var/log/below"))
        self.assertIn("/var/mnt/cache/system/below", binds.cleared)
        self.assertIn("below.service", binds.active_services)
        self.assertEqual(world.fstab, before_fstab)

        binds.fail_bind_for = ""
        binds.cleared.clear()
        world.fail("systemctl daemon-reload", OSError(1, RELOAD_FAILED), times=1)
        rc_r, lines_r = run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")
        self.assertEqual(rc_r, 1)
        self.assertIn(RELOAD_FAILED_TEXT, lines_r[-1])
        self.assertFalse(world.table.mounted("/var/log/below"))
        self.assertEqual(world.fstab, before_fstab)
        self.assertIn("below.service", binds.active_services)

        rc_ok, _ = run_system_action(_sys_args(SystemAction.APPLY, (SystemBindId.BELOW,)), env, "cst btrfs cache")
        self.assertEqual(rc_ok, 0)
        binds.fail_unbind_for = "/var/log/below"
        rc_rf, lines_rf = run_system_action(
            _sys_args(SystemAction.REVERT, (SystemBindId.BELOW,)), env, "cst btrfs cache"
        )
        self.assertEqual(rc_rf, 1)
        self.assertIn("restored.", lines_rf[-1])
        self.assertEqual([e.bind_id for e in parse_system_fstab_entries(world.fstab or "")], [SystemBindId.BELOW])
        self.assertTrue(world.table.mounted("/var/log/below"))

    def test_cache_mounts_dispatch_and_pure_formatting(self) -> None:
        world, binds, env = _seeded_world()
        mounts_svc = CacheMounts(
            fstab=FakeFstab(world),
            mounts=FakeCacheMountTable(world),
            systemd=FakeCacheSystemd(world),
            folders=FakeCacheFolders(world),
            host=FakeCacheHostView(world),
            accounts=FakeCacheAccounts(world),
            system_binds=binds,
        )
        parsed = CacheMountParsedArgs(action=CacheMountAction.SYSTEM, system_argv=("plan",))
        rc, out = mounts_svc.run(parsed, "cst btrfs cache")
        self.assertEqual(rc, 0)
        self.assertTrue(any("below" in line for line in out))

        rc_help, out_help = mounts_svc.run(
            CacheMountParsedArgs(action=CacheMountAction.SYSTEM, system_argv=("--help",)), "cst btrfs cache"
        )
        self.assertEqual(rc_help, 0)
        self.assertIn("system <status|plan|apply|revert>", "\n".join(out_help))

        no_adapter = SystemEnv(binds=None, fstab=env.fstab, mounts=env.mounts, systemd=env.systemd)
        self.assertEqual(run_system_action(_sys_args(SystemAction.STATUS), no_adapter, "cst btrfs cache")[0], 1)

        snap = SystemCacheSnapshot(
            _make_base(),
            _VAR_UUID,
            (_make_probe(SYSTEM_SPEC_BY_ID[SystemBindId.BELOW], "target_not_dir", "service_active"),),
            (),
            "",
        )
        self.assertIn("target is not a folder (blocked)", "\n".join(format_system_status(snap)))
        self.assertIn("BLOCKED:", "\n".join(format_system_plan(snap, "cst btrfs cache")))

        ready_unknown = SystemCacheSnapshot(
            _make_base(),
            _VAR_UUID,
            (_make_probe(SYSTEM_SPEC_BY_ID[SystemBindId.VAR_TMP], "in_fstab", "target_nonempty"),),
            (),
            "",
            fstab_behind=None,
        )
        plan_unknown = "\n".join(format_system_plan(ready_unknown, "cst btrfs cache"))
        self.assertIn("systemctl cannot say whether systemd has loaded /etc/fstab", plan_unknown)
        self.assertNotIn("stage in fstab", plan_unknown)

        err_snap = SystemCacheSnapshot(
            _make_base(),
            _VAR_UUID,
            (_make_probe(SYSTEM_SPEC_BY_ID[SystemBindId.BELOW]),),
            (),
            "",
            mounts_error="findmnt broken",
        )
        self.assertEqual(
            format_system_plan(err_snap, "cst btrfs cache"),
            ["Plan for system NVMe cache bind mounts:", "BLOCKED: cannot list mounts: findmnt broken"],
        )

    def test_validation_edges_partial_only_and_multi_service_failures(self) -> None:
        below = SYSTEM_SPEC_BY_ID[SystemBindId.BELOW]
        vt = SYSTEM_SPEC_BY_ID[SystemBindId.VAR_TMP]
        ok_base = _make_base()
        for flag, needle in (
            ("target_is_symlink", "symlink"),
            ("source_is_symlink", "symlink"),
            ("target_not_dir", "not a folder"),
            ("fstab_unmanaged", "does not manage"),
            ("mounted_other", "mount CST did not make"),
        ):
            msg = validate_system_apply(SystemCacheSnapshot(ok_base, _VAR_UUID, (_make_probe(below, flag),), (), ""))
            self.assertIn(needle, msg[0], flag)

        self.assertIn(
            "--mount-var-tmp-now",
            validate_system_apply(
                SystemCacheSnapshot(ok_base, _VAR_UUID, (_make_probe(vt, "target_nonempty"),), (), ""),
                mount_var_tmp_now=True,
            )[0],
        )
        self.assertEqual(
            validate_system_apply(
                SystemCacheSnapshot(ok_base, _VAR_UUID, (_make_probe(vt, "target_nonempty"),), (), ""),
                mount_var_tmp_now=True,
                allow_nonempty=True,
            ),
            [],
        )
        self.assertIn(
            "on top of CST's bind",
            validate_system_revert(
                SystemCacheSnapshot(ok_base, _VAR_UUID, (_make_probe(below, "bind_below"),), (), "")
            )[0],
        )

        # 2nd service copy failure cleans 1st source and restarts 1st service
        _, binds, env = _seeded_world()
        binds.fail_copy_for = "/var/mnt/cache/system/btrfs-stall-monitor"
        rc_c, _ = run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")
        self.assertEqual(rc_c, 1)
        self.assertIn("/var/mnt/cache/system/below", binds.cleared)
        self.assertIn("below.service", binds.active_services)

        # 2nd service ensure_root_dir failure restarts 1st service & clears copied data
        _, binds, env = _seeded_world()
        binds.fail_mkdir_for = "/var/mnt/cache/system/btrfs-stall-monitor"
        self.assertEqual(run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")[0], 1)
        self.assertIn("/var/mnt/cache/system/below", binds.cleared)
        self.assertIn("below.service", binds.active_services)

        # 2nd service stop_service failure during apply restarts 1st service without double ERROR:
        _, binds, env = _seeded_world()
        binds.active_services.add("btrfs-stall-monitor.service")
        binds.fail_stop_for = "btrfs-stall-monitor.service"
        rc_sa, out_sa = run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")
        self.assertEqual(rc_sa, 1)
        self.assertFalse(out_sa[-1].startswith("ERROR: ERROR:"))
        self.assertIn("below.service", binds.active_services)

        # Revert ordering: unbind failure on 2nd active mount re-binds 1st BEFORE restarting stopped services
        _, binds, env = _seeded_world()
        binds.active_services.add("btrfs-stall-monitor.service")
        self.assertEqual(run_system_action(_sys_args(SystemAction.APPLY), env, "cst btrfs cache")[0], 0)
        binds.events.clear()
        binds.fail_unbind_for = "/var/log/btrfs-stall-monitor"
        rc_rv, out_rv = run_system_action(_sys_args(SystemAction.REVERT), env, "cst btrfs cache")
        self.assertEqual(rc_rv, 1)
        self.assertLess(binds.events.index("bind:/var/log/below"), binds.events.index("start:below.service"))
        self.assertNotIn("ERROR: ERROR:", out_rv[-1])

        # Stop failure during revert also does not double-wrap ERROR:
        binds.fail_unbind_for = ""
        binds.fail_stop_for = "btrfs-stall-monitor.service"
        rc_rs, out_rs = run_system_action(_sys_args(SystemAction.REVERT), env, "cst btrfs cache")
        self.assertEqual(rc_rs, 1)
        self.assertFalse(out_rs[-1].startswith("ERROR: ERROR:"))
        self.assertIn("below.service", binds.active_services)

        # Busy /var/tmp on revert when already absent from /etc/fstab omits "nothing to do"
        world_vt, binds_vt, env_vt = _seeded_world()
        world_vt.table.add_mount(
            "/dev/mapper/cache", "/var/tmp", fsroot="/cache/system/var-tmp", uuid=UUID_CACHE_DRIVE, bind=True
        )
        binds_vt.busy_unbind_for = "/var/tmp"
        rc_vt, out_vt = run_system_action(
            _sys_args(SystemAction.REVERT, (SystemBindId.VAR_TMP,)), env_vt, "cst btrfs cache"
        )
        self.assertEqual(rc_vt, 0)
        self.assertIn("already absent from /etc/fstab", "\n".join(out_vt))
        self.assertNotIn("nothing to do", "\n".join(out_vt))

    def test_wiring_container_refusal_and_read_only_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-syswiring-") as tmp:
            os.makedirs(os.path.join(tmp, "etc"), exist_ok=True)
            runner = FakeMountRunner(tmp)
            env_map = {"CST_BTRFS_CACHE_ROOT": tmp}
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            rc_plan = run_cache_mount(
                ["system", "plan"],
                deps=ManagedCliEnv(
                    env=env_map, out=out_buf, err=err_buf, ctx=CacheTestCtx(container=True, root=False), runner=runner
                ),
            )
            self.assertEqual(rc_plan, 0, err_buf.getvalue())

            err_buf = io.StringIO()
            rc_apply = run_cache_mount(
                ["system", "apply"],
                deps=ManagedCliEnv(
                    env=env_map,
                    out=io.StringIO(),
                    err=err_buf,
                    ctx=CacheTestCtx(container=True, root=False),
                    runner=runner,
                ),
            )
            self.assertNotEqual(rc_apply, 0)
            self.assertIn("toolbox", err_buf.getvalue().lower())


class SystemBindAdapterTest(unittest.TestCase):
    def test_adapter_dirs_symlinks_services_and_mounts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-sysbind-test-") as tmp:
            runner = FakeMountRunner(tmp)
            rt = CacheMountRuntime(seam_root=tmp, ctx=CacheTestCtx(), runner=runner, env_map={})
            adapter = CacheSystemBindAdapter(rt)

            spec_vt = SYSTEM_SPEC_BY_ID[SystemBindId.VAR_TMP]
            spec_below = SYSTEM_SPEC_BY_ID[SystemBindId.BELOW]
            os.makedirs(os.path.join(tmp, "var/log/below"))
            with open(os.path.join(tmp, "var/log/below/a.log"), "w", encoding="utf-8") as fh:
                fh.write("entry\n")

            before = adapter.probe_paths(spec_below)
            self.assertTrue(before.target_nonempty)
            self.assertFalse(before.source_exists)

            adapter.ensure_root_dir("/var/mnt/cache/system/var-tmp", spec_vt.mode)
            vt_mode = stat.S_IMODE(os.lstat(os.path.join(tmp, "var/mnt/cache/system/var-tmp")).st_mode)
            self.assertEqual(vt_mode, 0o1777)

            link_path = os.path.join(tmp, "var/mnt/cache/system/linkdir")
            os.symlink("/etc", link_path)
            with self.assertRaises(OSError):
                adapter.ensure_root_dir("/var/mnt/cache/system/linkdir", 0o755)
            link_probe = adapter.probe_paths(
                SystemBindSpec(SystemBindId.BELOW, "linkdir", "/var/mnt/cache/system/linkdir", 0o755)
            )
            self.assertTrue(link_probe.source_is_link)
            self.assertTrue(link_probe.target_is_link)

            adapter.ensure_root_dir(spec_below.source, spec_below.mode)
            with mock.patch.object(runner, "run", return_value=CommandResult(0, "", "")) as m_cp:
                self.assertIsNone(adapter.copy_tree(spec_below.target, spec_below.source))
            self.assertEqual(m_cp.call_args.args[0][0], "cp")

            with mock.patch.object(
                runner,
                "run",
                side_effect=[
                    CommandResult(0, "ActiveState=active\n", ""),
                    CommandResult(0, "ActiveState=inactive\n", ""),
                    CommandResult(1, "", "err"),
                ],
            ):
                self.assertTrue(adapter.service_is_active("below.service"))
                self.assertFalse(adapter.service_is_active("below.service"))
                self.assertIsNone(adapter.service_is_active("below.service"))

            # Verify toolbox runtime queries systemd with on_host=True via SystemctlHost.SHOW_ONLY
            tb_rt = CacheMountRuntime(seam_root="", ctx=CacheTestCtx(container=True), runner=runner, env_map={})
            tb_adapter = CacheSystemBindAdapter(tb_rt)
            with mock.patch.object(runner, "run", return_value=CommandResult(0, "ActiveState=active\n", "")) as m_show:
                self.assertTrue(tb_adapter.service_is_active("below.service"))
            self.assertTrue(m_show.call_args.kwargs.get("on_host"))

            with mock.patch.object(runner, "run", return_value=CommandResult(0, "", "")) as m_mnt:
                self.assertIsNone(adapter.bind(spec_below.source, spec_below.target))
            self.assertEqual(m_mnt.call_args.args[0][0:2], ["mount", "--bind"])


if __name__ == "__main__":
    unittest.main()
