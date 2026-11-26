# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst toolchains tmpfs`, `RUST_BUILD_TMPFS`, and tmpfs prune defaults."""

from __future__ import annotations

import contextlib
import io
import os
import pwd
import shutil
import tempfile
import unittest
from collections.abc import Sequence
from typing import override
from unittest import mock

from cst_core.domain.errors import Refused, Usage
from cst_core.domain.files import OwnedState, UnitFileState, WholeFileState
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.domain.values import GIB, HOUR, MIB, MINUTE
from cst_core.ports import UnitName
from toolchains import cli as tc_cli, wiring as tc_wiring
from toolchains.adapters import build_tree as tc_build_tree, sccache as tc_sccache, tmpfs as tc_tmpfs
from toolchains.application import config as tca_config, rust_build as tca_rust_build, tmpfs as tca_tmpfs
from toolchains.domain import build as tcd_build, common as tcd_common, config as tcd_config, tmpfs as tcd_tmpfs
from toolchains.domain.prune import Limits, Settings
from toolchains.tests.test_prune_rust_build import NOW, TOOL, quiet_signals


class _NoopSccacheInstaller(tc_sccache.SccacheInstaller):
    @override
    def install(self) -> tuple[bool, str]:
        return False, "stubbed in unit tests"


class _MemOut:
    def __init__(self) -> None:
        self.lines: list[tuple[str, bool]] = []

    def say(self, msg: str, prio: int = 6, *, err: bool = False) -> None:
        del prio
        self.lines.append((msg, err))

    def where(self, path: str) -> str:
        return path

    @property
    def text(self) -> str:
        return "\n".join(msg for msg, _ in self.lines)


class _FakeTmpfsHost:
    def __init__(self, *, mem_total_bytes: int | None = 96 * GIB) -> None:
        self.mem_total_bytes = mem_total_bytes
        self.target = tcd_tmpfs.TmpfsTargetUser.from_home(
            user="alice",
            group="alice",
            uid=1000,
            gid=1000,
            real_home="/var/home/alice",
        )
        self.unit_name = "var-home-alice-.cache-cargo\\x2dbuild\\x2dtmpfs.mount"
        self.unit_path = f"/etc/systemd/system/{self.unit_name}"
        self.tmpfiles_path = "/etc/tmpfiles.d/cst-cargo-build-tmpfs-alice.conf"
        self.files: dict[str, str] = {}
        self.unit_masked = False
        self.unit_active = ActiveState.INACTIVE
        self.unit_enabled = EnablementState.DISABLED
        self.mount: tcd_tmpfs.TmpfsMountInfo | None = None
        self.cargo_uses_tmpfs = False
        self.dirs_ensured: list[str] = []
        self.dirs_removed: list[str] = []
        self.reloads = 0
        self.tmpfiles_applied: list[str] = []
        self.enabled_now: list[str] = []
        self.enabled_only: list[str] = []
        self.remounts: list[tuple[str, str]] = []
        self.disabled_now: list[str] = []
        self.marked = 0
        self.forgotten: list[list[str]] = []
        self.fail_disable: str | None = None

    def _state(self, path: str, wanted: str | None, *, allow_masked: bool) -> UnitFileState:
        if allow_masked and self.unit_masked:
            return OwnedState.MASKED
        cur = self.files.get(path)
        if cur is None:
            return OwnedState.MISSING
        if "managed-by: toolchains-setup.sh (CST) [toolchains:tmpfs]" not in cur:
            return OwnedState.CONFLICT
        if wanted is not None and cur == wanted:
            return OwnedState.OK
        return OwnedState.CHANGE

    def snapshot(self, user: str | None, *, size_bytes: int | None) -> tcd_tmpfs.TmpfsSnapshot:
        if user is not None and user != self.target.user:
            raise Refused(f"user {user!r} does not exist")
        wanted_u = tcd_tmpfs.render_tmpfs_mount_unit(self.target, size_bytes) if size_bytes is not None else None
        wanted_tf = tcd_tmpfs.render_tmpfs_tmpfiles(self.target)
        tf_raw = self._state(self.tmpfiles_path, wanted_tf, allow_masked=False)
        tf_state: WholeFileState = OwnedState.CHANGE if tf_raw == OwnedState.MASKED else tf_raw
        return tcd_tmpfs.TmpfsSnapshot(
            target=self.target,
            unit_name=self.unit_name,
            unit_path=self.unit_path,
            tmpfiles_path=self.tmpfiles_path,
            unit_state=self._state(self.unit_path, wanted_u, allow_masked=True),
            tmpfiles_state=tf_state,
            unit_active=self.unit_active,
            unit_enabled=self.unit_enabled,
            mount=self.mount,
            mem_total_bytes=self.mem_total_bytes,
            cargo_uses_tmpfs=self.cargo_uses_tmpfs,
        )

    def ensure_mountpoint(self, target: tcd_tmpfs.TmpfsTargetUser) -> None:
        self.dirs_ensured.append(target.mountpoint)

    def put_managed_file(self, path: str, text: str) -> bool:
        changed = self.files.get(path) != text
        self.files[path] = text
        return changed

    def mark_installed(self) -> None:
        self.marked += 1

    def forget_managed_files(self, paths: Sequence[str]) -> None:
        self.forgotten.append(list(paths))

    def remove_file(self, path: str) -> bool:
        return self.files.pop(path, None) is not None

    def remove_empty_dir(self, path: str) -> None:
        self.dirs_removed.append(path)

    def daemon_reload(self) -> None:
        self.reloads += 1

    def apply_tmpfiles(self, path: str) -> None:
        self.tmpfiles_applied.append(path)

    def _mount_from_options(self, options: str) -> None:
        opts: dict[str, str] = {}
        for tok in options.split(","):
            k, sep, v = tok.partition("=")
            opts[k] = v if sep else ""
        self.mount = tcd_tmpfs.TmpfsMountInfo(fstype="tmpfs", options=opts, capacity_bytes=None)

    def enable_now(self, unit: UnitName) -> None:
        self.enabled_now.append(str(unit))
        self.unit_enabled = EnablementState.ENABLED
        self.unit_active = ActiveState.ACTIVE
        body = self.files[self.unit_path]
        opt_line = next(ln for ln in body.splitlines() if ln.startswith("Options="))
        self._mount_from_options(opt_line.split("=", 1)[1])

    def enable_unit(self, unit: UnitName) -> None:
        self.enabled_only.append(str(unit))
        self.unit_enabled = EnablementState.ENABLED

    def remount_tmpfs(self, mountpoint: str, options: str) -> None:
        self.remounts.append((mountpoint, options))
        self._mount_from_options(options)

    def disable_now(self, unit: UnitName) -> None:
        if self.fail_disable is not None:
            raise Refused(self.fail_disable)
        self.disabled_now.append(str(unit))
        self.unit_enabled = EnablementState.DISABLED
        self.unit_active = ActiveState.INACTIVE
        self.mount = None


class TmpfsDomainTest(unittest.TestCase):
    def test_meminfo_and_adaptive_cap_formula(self) -> None:
        self.assertEqual(tcd_tmpfs.mem_total_from_meminfo("MemTotal:       98183224 kB\n"), 98183224 * 1024)
        self.assertIsNone(tcd_tmpfs.mem_total_from_meminfo("MemAvailable: 1024 kB\n"))
        self.assertIsNone(tcd_tmpfs.mem_total_from_meminfo("MemTotal: 0 kB\n"))
        self.assertIsNone(tcd_tmpfs.mem_total_from_meminfo("MemTotal: ² kB\n"))
        self.assertEqual(tcd_tmpfs.recommended_tmpfs_bytes(94 * GIB), 16 * GIB)
        self.assertEqual(tcd_tmpfs.recommended_tmpfs_bytes(32 * GIB), 6 * GIB)
        self.assertEqual(tcd_tmpfs.recommended_tmpfs_bytes(16 * GIB), 3 * GIB)
        self.assertEqual(tcd_tmpfs.recommended_tmpfs_bytes(4 * GIB), 819 * MIB)
        with self.assertRaises(Refused):
            tcd_tmpfs.recommended_tmpfs_bytes(2 * GIB)
        with self.assertRaises(Refused):
            tcd_tmpfs.recommended_tmpfs_bytes(None)

    def test_validate_tmpfs_size_bounds(self) -> None:
        self.assertEqual(tcd_tmpfs.validate_tmpfs_size(512 * MIB, 16 * GIB), 512 * MIB)
        self.assertEqual(tcd_tmpfs.validate_tmpfs_size(8 * GIB, 16 * GIB), 8 * GIB)
        with self.assertRaises(Refused):
            tcd_tmpfs.validate_tmpfs_size(256 * MIB, 16 * GIB)
        with self.assertRaises(Refused):
            tcd_tmpfs.validate_tmpfs_size(12 * GIB, 16 * GIB)

    def test_render_units_and_mount_matching(self) -> None:
        target = tcd_tmpfs.TmpfsTargetUser.from_home(
            user="danilo",
            group="danilo",
            uid=1000,
            gid=1000,
            real_home="/var/home/danilo",
        )
        unit = tcd_tmpfs.render_tmpfs_mount_unit(target, 16 * GIB)
        self.assertIn("What=tmpfs\n", unit)
        self.assertIn("Where=/var/home/danilo/.cache/cargo-build-tmpfs\n", unit)
        self.assertIn("Options=mode=0700,uid=1000,gid=1000,size=16G,nosuid,nodev,noatime\n", unit)
        self.assertIn("RequiresMountsFor=/var/home/danilo/.cache\n", unit)
        self.assertIn("WantedBy=local-fs.target\n", unit)
        tf = tcd_tmpfs.render_tmpfs_tmpfiles(target)
        self.assertIn("d /var/home/danilo/.cache/cargo-build-tmpfs 0700 danilo danilo -\n", tf)

        mounts = (
            "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0\n"
            f"tmpfs {target.mountpoint} tmpfs rw,nosuid,nodev,noatime,size=16777216k,mode=700,uid=1000,gid=1000 0 0\n"
        )
        found = tcd_tmpfs.find_mount_in_proc_mounts(mounts, target.mountpoint)
        self.assertIsNotNone(found)
        assert found is not None
        info = tcd_tmpfs.TmpfsMountInfo(fstype=found[0], options=found[1], capacity_bytes=16 * GIB)
        self.assertTrue(tcd_tmpfs.mount_matches_desired(info, target, 16 * GIB))
        self.assertTrue(tcd_tmpfs.mount_matches_desired(info, target, 8 * GIB) is False)
        loose = tcd_tmpfs.TmpfsMountInfo(
            fstype="tmpfs",
            options={
                "nosuid": "",
                "nodev": "",
                "noatime": "",
                "uid": "1000",
                "gid": "1000",
                "size": "16G",
                "mode": "1777",
            },
        )
        self.assertTrue(tcd_tmpfs.mount_matches_desired(loose, target, 16 * GIB) is False)
        nomode = tcd_tmpfs.TmpfsMountInfo(
            fstype="tmpfs",
            options={"nosuid": "", "nodev": "", "noatime": "", "uid": "1000", "gid": "1000", "size": "16G"},
        )
        self.assertTrue(tcd_tmpfs.mount_matches_desired(nomode, target, 16 * GIB) is False)

    def test_effective_rust_policy_adjusts_unoverridden_defaults_on_tmpfs(self) -> None:
        base = tcd_build.RustPolicy(
            max_idle=48 * HOUR, max_total=150 * GIB, min_keep=2 * HOUR, incremental_idle=6 * HOUR
        )
        nvme, on_nvme = tcd_build.effective_rust_policy(base, frozenset(), tmpfs_bytes=None)
        self.assertEqual((nvme, on_nvme), (base, False))

        ram16, on_ram = tcd_build.effective_rust_policy(base, frozenset(), tmpfs_bytes=16 * GIB)
        self.assertTrue(on_ram)
        self.assertEqual(ram16.max_total, 12 * GIB)
        self.assertEqual(ram16.max_idle, 4 * HOUR)
        self.assertEqual(ram16.incremental_idle, 1 * HOUR)
        self.assertEqual(ram16.min_keep, 30 * MINUTE)

        small, _ = tcd_build.effective_rust_policy(base, frozenset(), tmpfs_bytes=4 * GIB)
        self.assertEqual(small.max_total, 3 * GIB)

        overridden = tcd_build.RustPolicy(
            max_idle=48 * HOUR, max_total=6 * GIB, min_keep=90 * MINUTE, incremental_idle=6 * HOUR
        )
        kept, _ = tcd_build.effective_rust_policy(
            overridden,
            frozenset({"RUST_BUILD_MAX_TOTAL", "RUST_BUILD_MIN_KEEP"}),
            tmpfs_bytes=16 * GIB,
        )
        self.assertEqual(kept.max_total, 6 * GIB)
        self.assertEqual(kept.min_keep, 90 * MINUTE)
        self.assertEqual(kept.max_idle, 4 * HOUR)
        self.assertEqual(kept.incremental_idle, 90 * MINUTE)  # clamped >= user MIN_KEEP

    def test_build_dir_switch_rewrite_only_touches_csts_value(self) -> None:
        home = "/var/home/alice"
        nvme = tcd_build.expected_template(home)
        tmpfs = tcd_build.expected_tmpfs_template(home)
        src = f'[build]\nbuild-dir = "{nvme}"\nrustc-wrapper = "/var/home/alice/.local/libexec/cst/rustc-wrapper"\n'
        rewritten = tcd_config.build_dir_switch_rewrite(src, nvme, tmpfs)
        self.assertIn(f'build-dir = "{tmpfs}"', rewritten)
        self.assertIn("rustc-wrapper =", rewritten)
        back = tcd_config.build_dir_switch_rewrite(rewritten, tmpfs, nvme)
        self.assertEqual(back, src)
        foreign = '[build]\nbuild-dir = "/var/home/alice/custom"\n'
        self.assertEqual(tcd_config.build_dir_switch_rewrite(foreign, nvme, tmpfs), "")


class TmpfsCliAndManagerTest(unittest.TestCase):
    def test_parse_args_for_tmpfs(self) -> None:
        a = tcd_common.parse_args(["tmpfs", "apply", "--size=8G", "--user", "danilo"])
        self.assertEqual((a["cmd"], a["action"], a["size"], a["user"]), ("tmpfs", "apply", "8G", "danilo"))
        for bad in (
            ["tmpfs"],
            ["tmpfs", "status", "--size", "4G"],
            ["tmpfs", "revert", "--size", "4G"],
            ["tmpfs", "apply", "--home", "/tmp/x"],
            ["tmpfs", "apply", "--user", ""],
        ):
            with self.subTest(bad=bad), self.assertRaises(Usage):
                tcd_common.parse_args(bad)

    def test_manager_full_cycle_live_resize_and_refusals(self) -> None:
        host = _FakeTmpfsHost()
        out = _MemOut()
        mgr = tca_tmpfs.TmpfsManager(host, out, "cst toolchains")

        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.PLAN), 0)
        self.assertEqual(host.files, {})
        self.assertIn("would write:", out.text)
        self.assertIn("16.0 GiB", out.text)

        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.PLAN, size_text="8G"), 0)
        self.assertIn("--size 8G", out.text)

        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.APPLY), 0)
        self.assertIn(host.unit_path, host.files)
        self.assertIn(host.tmpfiles_path, host.files)
        self.assertEqual(host.reloads, 1)
        self.assertEqual(host.enabled_now, [host.unit_name])
        self.assertEqual(host.marked, 1)

        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.PLAN, size_text="8G"), 0)
        self.assertIn("--size 8G", out.text)

        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.APPLY), 0)
        self.assertIn("unchanged:", out.text)
        self.assertEqual(host.enabled_now, [host.unit_name])

        # Live resize remounts cleanly without stop/start.
        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.APPLY, size_text="8G"), 0)
        self.assertEqual(host.remounts, [(host.target.mountpoint, tcd_tmpfs.tmpfs_mount_options(host.target, 8 * GIB))])
        self.assertEqual(host.enabled_only, [host.unit_name])

        # Status reports active mount & cargo config state.
        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.STATUS), 0)
        self.assertIn("mounted (tmpfs, cap 8G", out.text)

        # Busy revert fails before deleting unit/tmpfiles files.
        host.fail_disable = "mount is busy"
        with self.assertRaises(Refused):
            mgr.run(tcd_tmpfs.TmpfsAction.REVERT)
        self.assertIn(host.unit_path, host.files)
        self.assertIn(host.tmpfiles_path, host.files)

        host.fail_disable = None
        host.cargo_uses_tmpfs = True
        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.REVERT), 0)
        self.assertEqual(host.files, {})
        self.assertEqual(host.disabled_now, [host.unit_name])
        self.assertEqual(host.dirs_removed, [host.target.mountpoint])
        self.assertIn("switch ~/.cargo/config.toml back to NVMe", out.text)

        # Idempotent 2nd revert.
        out.lines.clear()
        self.assertEqual(mgr.run(tcd_tmpfs.TmpfsAction.REVERT), 0)
        self.assertIn("already reverted:", out.text)

    def test_manager_refuses_masked_or_foreign_files(self) -> None:
        host = _FakeTmpfsHost()
        mgr = tca_tmpfs.TmpfsManager(host, _MemOut(), "cst toolchains")
        host.unit_masked = True
        with self.assertRaisesRegex(Refused, "masked"):
            mgr.run(tcd_tmpfs.TmpfsAction.APPLY)
        # revert leaves masked symlink untouched and never calls disable_now.
        host.files[host.unit_path] = "/dev/null"
        out_mask = _MemOut()
        self.assertEqual(tca_tmpfs.TmpfsManager(host, out_mask, "cst toolchains").run(tcd_tmpfs.TmpfsAction.REVERT), 0)
        self.assertIn(host.unit_path, host.files)
        self.assertIn("left alone:", out_mask.text)
        host.files.pop(host.unit_path, None)

        host.unit_masked = False
        host.files[host.unit_path] = "[Unit]\nDescription=custom user unit\n"
        with self.assertRaisesRegex(Refused, "not written by CST"):
            mgr.run(tcd_tmpfs.TmpfsAction.APPLY)
        with self.assertRaisesRegex(Refused, "not written by CST"):
            mgr.run(tcd_tmpfs.TmpfsAction.REVERT)

    def test_ensure_mountpoint_refuses_symlinks_and_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-tmpfs-sym-") as td:
            real_td = os.path.realpath(td)
            sym_parent = os.path.join(real_td, "symhome")
            real_dir = os.path.join(real_td, "real")
            os.makedirs(real_dir)
            os.symlink(real_dir, sym_parent)
            target = tcd_tmpfs.TmpfsTargetUser.from_home(
                user="alice", group="alice", uid=os.getuid(), gid=os.getgid(), real_home=sym_parent
            )
            host = tc_tmpfs.TmpfsHost(TOOL)
            with self.assertRaisesRegex(Refused, "symlink"):
                host.ensure_mountpoint(target)

            file_home = os.path.join(real_td, "filehome")
            os.makedirs(file_home)
            with open(os.path.join(file_home, ".cache"), "w", encoding="utf-8") as fh:
                fh.write("x")
            bad_target = tcd_tmpfs.TmpfsTargetUser.from_home(
                user="alice", group="alice", uid=os.getuid(), gid=os.getgid(), real_home=file_home
            )
            with self.assertRaisesRegex(Refused, "not a folder|symlink|cannot create"):
                host.ensure_mountpoint(bad_target)

    def test_cli_routes_tmpfs_via_wiring_without_root_rejection(self) -> None:
        quiet_signals(self)
        host = _FakeTmpfsHost()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tc_cli.main(
                ["tmpfs", "status"], doc="", entry_file=TOOL, parts=tc_wiring.Parts(tmpfs=host, journal=False)
            )
        self.assertEqual(rc, 0)
        self.assertIn("mount:      not mounted", buf.getvalue())


class ToolConfigAndBuildTreeTmpfsTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        quiet_signals(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-tmpfs-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config/cst"), exist_ok=True)
        os.makedirs(os.path.join(self.home, ".cache/cargo-build-tmpfs"), exist_ok=True)

    def _settings(self, mode: str, user_keys: frozenset[str] = frozenset()) -> Settings:
        with open(os.path.join(os.path.dirname(TOOL), "toolchains.conf"), encoding="utf-8") as fh:
            from toolchains.domain.prune import checked_settings, parse_defaults

            defaults = parse_defaults(TOOL, fh.read())
        user_text = f"RUST_BUILD_TMPFS={mode}\n" + "".join(f"{k}=4h\n" for k in user_keys if "IDLE" in k)
        return checked_settings(self.home, defaults, user_text)

    def test_config_switches_between_nvme_and_tmpfs_on_auto(self) -> None:
        from toolchains.adapters import home_files as tc_home_files, program as tc_program

        files = tc_home_files.HomeFiles(self.home, TOOL)
        out = tc_program.RunOutput(self.home, journal=False)
        mounted = False

        with mock.patch.object(files, "tmpfs_mounted", side_effect=lambda: mounted):
            cfg = tca_config.ToolConfig(
                self.home,
                "cst toolchains",
                files,
                out,
                lambda: self._settings("auto"),
                _NoopSccacheInstaller(self.home),
            )
            # Apply without tmpfs -> NVMe build-dir.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cfg.run(tcd_common.ToolchainsAction.APPLY, lean=False, sccache=False)
            self.assertEqual(rc, 0)
            cargo_cfg = os.path.join(self.home, ".cargo/config.toml")
            with open(cargo_cfg, encoding="utf-8") as fh:
                self.assertIn(tcd_build.expected_template(self.home), fh.read())

            # Mount tmpfs -> plan proposes switch, apply switches cleanly.
            mounted = True
            plan_buf = io.StringIO()
            with contextlib.redirect_stdout(plan_buf):
                cfg.run(tcd_common.ToolchainsAction.PLAN, lean=False, sccache=False)
            self.assertIn("build-dir ->", plan_buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cfg.run(tcd_common.ToolchainsAction.APPLY, lean=False, sccache=False)
            self.assertEqual(rc, 0)
            with open(cargo_cfg, encoding="utf-8") as fh:
                self.assertIn(tcd_build.expected_tmpfs_template(self.home), fh.read())

            # Revert tmpfs -> config apply falls back to NVMe cleanly.
            mounted = False
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cfg.run(tcd_common.ToolchainsAction.APPLY, lean=False, sccache=False)
            self.assertEqual(rc, 0)
            with open(cargo_cfg, encoding="utf-8") as fh:
                self.assertIn(tcd_build.expected_template(self.home), fh.read())

    def test_config_requires_mount_for_yes_and_preserves_foreign_build_dir(self) -> None:
        from toolchains.adapters import home_files as tc_home_files, program as tc_program

        files = tc_home_files.HomeFiles(self.home, TOOL)
        out = tc_program.RunOutput(self.home, journal=False)
        with mock.patch.object(files, "tmpfs_mounted", return_value=False):
            cfg = tca_config.ToolConfig(
                self.home,
                "cst toolchains",
                files,
                out,
                lambda: self._settings("yes"),
                _NoopSccacheInstaller(self.home),
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cfg.run(tcd_common.ToolchainsAction.APPLY, lean=False, sccache=False)
            self.assertEqual(rc, 1)
            self.assertIn("RUST_BUILD_TMPFS=yes requires", err.getvalue())

        cargo_cfg = os.path.join(self.home, ".cargo/config.toml")
        os.makedirs(os.path.dirname(cargo_cfg), exist_ok=True)
        # Already pointing to unmounted tmpfs + RUST_BUILD_TMPFS=yes still fails both plan and apply.
        with open(cargo_cfg, "w", encoding="utf-8") as fh:
            fh.write(f'[build]\nbuild-dir = "{tcd_build.expected_tmpfs_template(self.home)}"\n')
        with mock.patch.object(files, "tmpfs_mounted", return_value=False):
            cfg_yes = tca_config.ToolConfig(
                self.home,
                "cst toolchains",
                files,
                out,
                lambda: self._settings("yes"),
                _NoopSccacheInstaller(self.home),
            )
            plan_lines = cfg_yes.lines(tcd_config.ConfigView.PLAN, self._settings("yes"), lean=False, sccache=False)
            self.assertEqual(plan_lines[0][0], tcd_config.ConfigMark.UNKNOWN)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cfg_yes.run(tcd_common.ToolchainsAction.APPLY, lean=False, sccache=False)
            self.assertEqual(rc, 1)
            self.assertIn("RUST_BUILD_TMPFS=yes requires", err.getvalue())

        with open(cargo_cfg, "w", encoding="utf-8") as fh:
            fh.write('[build]\nbuild-dir = "/custom/target"\n')
        with mock.patch.object(files, "tmpfs_mounted", return_value=True):
            cfg_auto = tca_config.ToolConfig(
                self.home,
                "cst toolchains",
                files,
                out,
                lambda: self._settings("auto"),
                _NoopSccacheInstaller(self.home),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cfg_auto.run(tcd_common.ToolchainsAction.APPLY, lean=False, sccache=False)
            self.assertEqual(rc, 0)
            with open(cargo_cfg, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '[build]\nbuild-dir = "/custom/target"\n')

    def test_is_active_tmpfs_mount_and_rust_build_ram_policy(self) -> None:
        mp = os.path.join(self.home, ".cache/cargo-build-tmpfs")
        mounts_file = os.path.join(self.tmp, "mounts")
        uid = os.getuid()
        with open(mounts_file, "w", encoding="utf-8") as fh:
            fh.write(f"tmpfs {mp} tmpfs rw,nosuid,nodev,noatime,uid={uid},gid={os.getgid()} 0 0\n")
        cap = tc_tmpfs.is_active_tmpfs_mount(mp, require_uid=uid, proc_mounts_path=mounts_file)
        self.assertIsNotNone(cap)
        self.assertIsNone(
            tc_tmpfs.is_active_tmpfs_mount(os.path.join(self.home, ".cache/cargo-build"), proc_mounts_path=mounts_file)
        )
        self.assertIsNone(tc_tmpfs.is_active_tmpfs_mount(mp, require_uid=uid + 999, proc_mounts_path=mounts_file))

        # RustBuild uses tmpfs location and RAM defaults when tmpfs_capacity returns cap.
        from cst_core.adapters import SystemClockAdapter
        from toolchains.adapters import home_files as tc_home_files, program as tc_program

        cargo_cfg = os.path.join(self.home, ".cargo/config.toml")
        os.makedirs(os.path.dirname(cargo_cfg), exist_ok=True)
        with open(cargo_cfg, "w", encoding="utf-8") as fh:
            fh.write(f'[build]\nbuild-dir = "{tcd_build.expected_tmpfs_template(self.home)}"\n')
        out = tc_program.RunOutput(self.home, journal=False)
        from toolchains.application.prune import TargetPorts
        from toolchains.adapters.uv import UvTool

        tree = tc_build_tree.CargoBuildTree(self.home, "cst toolchains", Limits(), SystemClockAdapter(), procs=list)
        machine = TargetPorts(
            tree, UvTool(self.home), tc_home_files.RunSummaries(self.home, out), out, SystemClockAdapter()
        )
        s = self._settings("auto")
        rb = tca_rust_build.RustBuild(self.home, NOW, s.rust, machine, Limits())
        rb.user_keys = s.user_keys
        self.assertEqual(rb.location(), mp)
        with mock.patch.object(tree, "tmpfs_capacity", return_value=16 * GIB):
            lines = "\n".join(rb.status_lines())
        self.assertIn("effective RAM policy: MAX_IDLE 4h, MAX_TOTAL 12.0 GiB", lines)

    def test_host_snapshot_reads_meminfo_and_mounts(self) -> None:
        from cst_core.adapters import SystemctlAdapter
        from cst_core.ports import CommandResult, SystemctlHost

        class _StubRunner:
            def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
                if "is-active" in argv:
                    return CommandResult(4, "inactive\n", "")
                if "is-enabled" in argv:
                    return CommandResult(1, "disabled\n", "")
                return CommandResult(0, "", "")

        me = pwd.getpwuid(os.getuid()).pw_name
        meminfo = os.path.join(self.tmp, "meminfo")
        mounts = os.path.join(self.tmp, "mounts")
        with open(meminfo, "w", encoding="utf-8") as fh:
            fh.write("MemTotal: 67108864 kB\n")
        with open(mounts, "w", encoding="utf-8") as fh:
            fh.write("")
        systemd = SystemctlAdapter(_StubRunner(), on_host=SystemctlHost.LOCAL)
        host = tc_tmpfs.TmpfsHost(
            TOOL,
            seams=tc_tmpfs.TmpfsTestSeams(
                systemd=systemd,
                runner=_StubRunner(),
                unit_dir=os.path.join(self.tmp, "units"),
                proc_meminfo_path=meminfo,
                proc_mounts_path=mounts,
            ),
        )
        os.makedirs(os.path.join(self.tmp, "units"), exist_ok=True)
        with mock.patch.object(systemd, "escape_path", return_value=UnitName("u.mount")):
            snap = host.snapshot(me, size_bytes=12 * GIB)
        self.assertEqual(snap.mem_total_bytes, 64 * GIB)
        self.assertIsNone(snap.mount)
        self.assertEqual(snap.unit_state, OwnedState.MISSING)
