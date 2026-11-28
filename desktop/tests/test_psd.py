# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst desktop psd`: config, tmpfs caps, overlay merge, recovery, and service lifecycle."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import stat
import tempfile
from typing import override
import unittest
from unittest import mock

from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.domain.errors import Refused
from cst_core.domain.values import GIB, MIB, DomainValidationError
from desktop.adapters.overlay import (
    WHITEOUT_MANIFEST_NAME,
    OverlayProbes,
    apply_recovery_to_lower,
    has_valid_recovery,
    merge_upper_to_lower,
    snapshot_upper_to_recovery,
)
from desktop.adapters.psd import LinuxPsdHost, scan_busy_processes
from desktop.application.psd import PsdRequest, PsdService
from desktop.domain import PsdAction, parse_desktop_config
from desktop.domain.psd import (
    KNOWN_PROFILES,
    PROFILE_BY_ID,
    PsdBusyProcess,
    PsdProfileProbe,
    PsdProfileState,
    PsdSnapshot,
    PsdTargetUser,
    clamp_tmpfs_bytes,
    parse_meminfo_total_bytes,
    parse_psd_mounts,
    parse_whiteout_manifest,
    resolve_profile_selection,
)


class PsdDomainTest(unittest.TestCase):
    """Test pure domain functions for `cst desktop psd`."""

    def test_config_validation_and_tmpfs_clamp(self) -> None:
        cfg = parse_desktop_config("PSD_PROFILES=chrome-flatpak,discord-flatpak\nPSD_TMPFS_SIZE=6G\n")
        self.assertEqual(cfg.profiles_raw, "chrome-flatpak,discord-flatpak")
        self.assertEqual(cfg.tmpfs_bytes, 6 * GIB)

        with self.assertRaises(DomainValidationError):
            parse_desktop_config("UNKNOWN=1\n")
        with self.assertRaises(DomainValidationError):
            parse_desktop_config("PSD_TMPFS_SIZE=16M\n")

        mem_16g = parse_meminfo_total_bytes("MemTotal:       16777216 kB\n")
        self.assertEqual(mem_16g, 16 * GIB)
        self.assertEqual(clamp_tmpfs_bytes(8 * GIB, mem_16g), 4 * GIB)
        self.assertEqual(clamp_tmpfs_bytes(2 * GIB, mem_16g), 2 * GIB)
        self.assertEqual(clamp_tmpfs_bytes(256 * MIB, 512 * MIB), 256 * MIB)

    def test_parse_mounts_and_profile_selection(self) -> None:
        raw = (
            "tmpfs /run/cst-psd/1000 tmpfs rw,nosuid,nodev,noexec,size=4194304k 0 0\n"
            "/dev/mapper/root /run/cst-psd/1000/chrome-flatpak/lower btrfs rw 0 0\n"
            "cst-psd-chrome-flatpak /home/u/.var/app/com.google.Chrome/config/google-chrome "
            "overlay rw,lowerdir=/run/cst-psd/1000/chrome-flatpak/lower,"
            "upperdir=/run/cst-psd/1000/chrome-flatpak/upper 0 0\n"
            "/dev/sda1 /home/u/.config/google-chrome ext4 rw 0 0\n"
        )
        mounts = parse_psd_mounts(raw, run_base="/run/cst-psd/1000")
        self.assertTrue(mounts.tmpfs_mounted)
        self.assertIn("/run/cst-psd/1000/chrome-flatpak/lower", mounts.lower_binds)
        self.assertIn("/home/u/.var/app/com.google.Chrome/config/google-chrome", mounts.overlay_targets)
        self.assertIn("/home/u/.config/google-chrome", mounts.other_mounts)

        auto_sel = resolve_profile_selection("auto", existing_rels={PROFILE_BY_ID["firefox"].rel_path})
        self.assertEqual([p.profile_id for p in auto_sel], ["firefox"])
        explicit_sel = resolve_profile_selection("chrome-flatpak,discord-flatpak", existing_rels=set())
        self.assertEqual([p.profile_id for p in explicit_sel], ["chrome-flatpak", "discord-flatpak"])
        with self.assertRaises(DomainValidationError):
            resolve_profile_selection("no-such-profile", existing_rels=set())


class OverlayMergeAndRecoveryTest(unittest.TestCase):
    """Test atomic pure-Python upper->lower merging, whiteouts, opaques, and crash recovery."""

    def test_merge_whiteout_opaque_and_recovery_roundtrip(self) -> None:
        uid, gid = os.getuid(), os.getgid()
        with tempfile.TemporaryDirectory() as tmp:
            upper = os.path.join(tmp, "upper")
            lower = os.path.join(tmp, "lower")
            rec = os.path.join(tmp, "recovery")
            os.makedirs(upper)
            os.makedirs(lower)

            # Seed lower with files that will be updated, deleted via whiteout, and replaced via opaque dir.
            with open(os.path.join(lower, "keep.txt"), "w", encoding="utf-8") as fh:
                fh.write("old-keep")
            with open(os.path.join(lower, "delete-me.txt"), "w", encoding="utf-8") as fh:
                fh.write("gone")
            os.makedirs(os.path.join(lower, "cache"))
            with open(os.path.join(lower, "cache", "stale.bin"), "w", encoding="utf-8") as fh:
                fh.write("stale")

            # Seed upper: updated file, whiteout marker file, opaque directory with fresh child, and symlink.
            with open(os.path.join(upper, "keep.txt"), "w", encoding="utf-8") as fh:
                fh.write("new-keep")
            with open(os.path.join(upper, "delete-me.txt"), "w", encoding="utf-8") as fh:
                fh.write("")
            os.makedirs(os.path.join(upper, "cache"))
            with open(os.path.join(upper, "cache", "fresh.bin"), "w", encoding="utf-8") as fh:
                fh.write("fresh")
            os.symlink("keep.txt", os.path.join(upper, "SingletonLock"))

            probes = OverlayProbes(
                is_whiteout=lambda p, _st: os.path.basename(p) == "delete-me.txt",
                is_opaque_dir=lambda p: os.path.basename(p) == "cache",
            )

            # 1. Crash recovery snapshot while running
            snapshot_upper_to_recovery(upper, rec, owner=(uid, gid), probes=probes)
            manifest = parse_whiteout_manifest(Path(rec, WHITEOUT_MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["whiteouts"], ["delete-me.txt"])
            self.assertEqual(manifest["opaque_dirs"], ["cache"])

            # Simulate reboot: apply recovery snapshot to untouched lower copy
            lower_boot = os.path.join(tmp, "lower-boot")
            os.makedirs(os.path.join(lower_boot, "cache"))
            with open(os.path.join(lower_boot, "delete-me.txt"), "w", encoding="utf-8") as fh:
                fh.write("gone")
            with open(os.path.join(lower_boot, "cache", "stale.bin"), "w", encoding="utf-8") as fh:
                fh.write("stale")

            apply_recovery_to_lower(rec, lower_boot, uid=uid, gid=gid)
            self.assertFalse(os.path.exists(os.path.join(lower_boot, "delete-me.txt")))
            self.assertFalse(os.path.exists(os.path.join(lower_boot, "cache", "stale.bin")))
            self.assertEqual(Path(lower_boot, "keep.txt").read_text(encoding="utf-8"), "new-keep")
            self.assertEqual(Path(lower_boot, "cache", "fresh.bin").read_text(encoding="utf-8"), "fresh")
            self.assertEqual(os.readlink(os.path.join(lower_boot, "SingletonLock")), "keep.txt")

            # 2. Direct upper->lower merge (unmounted shutdown path)
            merge_upper_to_lower(upper, lower, uid=uid, gid=gid, probes=probes)
            self.assertFalse(os.path.exists(os.path.join(lower, "delete-me.txt")))
            self.assertFalse(os.path.exists(os.path.join(lower, "cache", "stale.bin")))
            self.assertEqual(Path(lower, "keep.txt").read_text(encoding="utf-8"), "new-keep")
            self.assertEqual(Path(lower, "cache", "fresh.bin").read_text(encoding="utf-8"), "fresh")


class _FakePsdHost:
    def __init__(self, profiles: tuple[PsdProfileProbe, ...], *, tmpfs_mounted: bool = False) -> None:
        self.user = PsdTargetUser(name="alice", group="alice", uid=1000, gid=1000, home="/home/alice")
        self.profiles = list(profiles)
        self.tmpfs_mounted = tmpfs_mounted
        self.events: list[str] = []

    def read_snapshot(
        self,
        *,
        user: str | None = None,
        profiles_override: str | None = None,
        size_override: str | None = None,
    ) -> PsdSnapshot:
        return PsdSnapshot(
            user=self.user,
            user_error="",
            config_raw=profiles_override or "auto",
            config_error="",
            requested_tmpfs_bytes=4 * GIB,
            effective_tmpfs_bytes=4 * GIB,
            mem_total_bytes=64 * GIB,
            tmpfs_mounted=self.tmpfs_mounted,
            tmpfs_used_bytes=0 if self.tmpfs_mounted else None,
            tmpfs_avail_bytes=4 * GIB if self.tmpfs_mounted else None,
            systemd_installed=True,
            service_enabled=True,
            service_active=self.tmpfs_mounted,
            timer_enabled=True,
            timer_active=True,
            profiles=tuple(self.profiles),
        )

    def install_units(self) -> list[str]:
        self.events.append("install_units")
        return ["written:  /etc/systemd/system/cst-desktop-psd@.service"]

    def remove_units(self) -> list[str]:
        self.events.append("remove_units")
        return ["removed:  /etc/systemd/system/cst-desktop-psd@.service"]

    def enable_systemd_units(self, username: str) -> None:
        self.events.append(f"enable:{username}")

    def disable_systemd_units(self, username: str) -> None:
        self.events.append(f"disable:{username}")

    def ensure_tmpfs_mounted(self, user: PsdTargetUser, size_bytes: int) -> None:
        self.events.append(f"tmpfs:{size_bytes}")
        self.tmpfs_mounted = True

    def unmount_tmpfs_if_unused(self, user: PsdTargetUser) -> bool:
        self.events.append("unmount_tmpfs")
        self.tmpfs_mounted = False
        return True

    def mount_overlay(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        self.events.append(f"mount:{probe.spec.profile_id}")
        self._replace(probe.spec.profile_id, state=PsdProfileState.ACTIVE, overlay_active=True)

    def unmount_overlay(self, probe: PsdProfileProbe) -> None:
        self.events.append(f"unmount:{probe.spec.profile_id}")
        self._replace(probe.spec.profile_id, state=PsdProfileState.INACTIVE, overlay_active=False)

    def merge_upper(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        self.events.append(f"merge:{probe.spec.profile_id}")

    def clear_upper_and_work(self, probe: PsdProfileProbe) -> None:
        self.events.append(f"clear:{probe.spec.profile_id}")

    def snapshot_recovery(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        self.events.append(f"snapshot:{probe.spec.profile_id}")
        self._replace(probe.spec.profile_id, recovery_exists=True)

    def apply_recovery(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        self.events.append(f"apply_recovery:{probe.spec.profile_id}")
        self._replace(probe.spec.profile_id, recovery_exists=False)

    def discard_recovery(self, probe: PsdProfileProbe) -> None:
        self.events.append(f"discard_recovery:{probe.spec.profile_id}")
        self._replace(probe.spec.profile_id, recovery_exists=False)

    def _replace(
        self,
        profile_id: str,
        *,
        state: PsdProfileState | None = None,
        overlay_active: bool | None = None,
        recovery_exists: bool | None = None,
    ) -> None:
        for i, p in enumerate(self.profiles):
            if p.spec.profile_id == profile_id:
                self.profiles[i] = replace(
                    p,
                    state=p.state if state is None else state,
                    overlay_active=p.overlay_active if overlay_active is None else overlay_active,
                    recovery_exists=p.recovery_exists if recovery_exists is None else recovery_exists,
                )


class PsdServiceLifecycleTest(unittest.TestCase):
    """Test `PsdService` plan, apply ordering, busy guards, sync modes, recovery, and revert."""

    @staticmethod
    def _probe(profile_id: str, *, active: bool = False, busy: bool = False, recovery: bool = False) -> PsdProfileProbe:
        spec = PROFILE_BY_ID[profile_id]
        state = (
            PsdProfileState.ACTIVE
            if active
            else (PsdProfileState.RECOVERY_PENDING if recovery else PsdProfileState.INACTIVE)
        )
        return PsdProfileProbe(
            spec=spec,
            profile_path=f"/home/alice/{spec.rel_path}",
            lower_dir=f"/run/cst-psd/1000/{profile_id}/lower",
            upper_dir=f"/run/cst-psd/1000/{profile_id}/upper",
            work_dir=f"/run/cst-psd/1000/{profile_id}/work",
            recovery_dir=f"/home/alice/.local/share/cst/psd/recovery/{profile_id}",
            state=state,
            overlay_active=active,
            lower_bind_active=active,
            recovery_exists=recovery,
            upper_used_bytes=0,
            busy_procs=(PsdBusyProcess(pid=4242, comm="chrome", path=f"/home/alice/{spec.rel_path}"),) if busy else (),
        )

    def test_apply_installs_mounts_idle_before_enable_and_skips_busy(self) -> None:
        p_idle = self._probe("firefox")
        p_busy = self._probe("chrome-flatpak", busy=True)
        host = _FakePsdHost((p_idle, p_busy))
        svc = PsdService(host, prog="cst desktop")

        rc_plan, plan_lines = svc.run(PsdRequest(PsdAction.PLAN, user="alice"))
        self.assertEqual(rc_plan, 0)
        self.assertIn("mount", "\n".join(plan_lines))
        self.assertEqual(host.events, [])

        rc_app, app_lines = svc.run(PsdRequest(PsdAction.APPLY, user="alice"))
        self.assertEqual(rc_app, 0)
        # Idle profile must be mounted AFTER install_units and BEFORE enable_systemd_units starts the unit.
        self.assertEqual(
            host.events,
            ["install_units", f"tmpfs:{4 * GIB}", "mount:firefox", "enable:alice"],
        )
        self.assertIn("deferred: chrome-flatpak", "\n".join(app_lines))

    def test_start_stop_sync_and_revert_guards(self) -> None:
        # Busy unmounted profile refuses explicit `start`
        p_busy = self._probe("chrome-flatpak", busy=True)
        rc_start, lines_start = PsdService(_FakePsdHost((p_busy,)), prog="cst desktop").run(
            PsdRequest(PsdAction.START, user="alice")
        )
        self.assertEqual(rc_start, 1)
        self.assertIn("close the application", "\n".join(lines_start))

        # Live `sync`: busy overlay snapshots recovery; idle overlay cycle-merges
        p_live_busy = self._probe("chrome-flatpak", active=True, busy=True)
        p_live_idle = self._probe("firefox", active=True, busy=False)
        host_sync = _FakePsdHost((p_live_busy, p_live_idle), tmpfs_mounted=True)
        rc_sync, sync_lines = PsdService(host_sync, prog="cst desktop").run(PsdRequest(PsdAction.SYNC, user="alice"))
        self.assertEqual(rc_sync, 0)
        self.assertIn("snapshot: chrome-flatpak", "\n".join(sync_lines))
        self.assertIn("synced:   firefox", "\n".join(sync_lines))
        self.assertEqual(
            host_sync.events,
            [
                "snapshot:chrome-flatpak",
                "unmount:firefox",
                "merge:firefox",
                "clear:firefox",
                "discard_recovery:firefox",
                "mount:firefox",
            ],
        )

        # `stop` refuses while browser busy
        rc_stop_busy, _ = PsdService(host_sync, prog="cst desktop").run(PsdRequest(PsdAction.STOP, user="alice"))
        self.assertEqual(rc_stop_busy, 1)

        # `revert` succeeds cleanly when active overlays are idle
        host_rev = _FakePsdHost((self._probe("firefox", active=True, busy=False),), tmpfs_mounted=True)
        rc_rev, _ = PsdService(host_rev, prog="cst desktop").run(PsdRequest(PsdAction.REVERT, user="alice"))
        self.assertEqual(rc_rev, 0)
        self.assertEqual(
            host_rev.events,
            [
                "unmount:firefox",
                "merge:firefox",
                "clear:firefox",
                "discard_recovery:firefox",
                "unmount_tmpfs",
                "disable:alice",
                "remove_units",
            ],
        )

    def test_recover_replay_and_discard(self) -> None:
        host_rep = _FakePsdHost((self._probe("firefox", recovery=True),))
        rc_rep, lines_rep = PsdService(host_rep, prog="cst desktop").run(PsdRequest(PsdAction.RECOVER, user="alice"))
        self.assertEqual(rc_rep, 0)
        self.assertEqual(host_rep.events, ["apply_recovery:firefox"])
        self.assertIn("recovered:firefox", "\n".join(lines_rep))

        host_dis = _FakePsdHost((self._probe("firefox", recovery=True),))
        rc_dis, lines_dis = PsdService(host_dis, prog="cst desktop").run(
            PsdRequest(PsdAction.RECOVER, user="alice", discard=True)
        )
        self.assertEqual(rc_dis, 0)
        self.assertEqual(host_dis.events, ["discard_recovery:firefox"])
        self.assertIn("discarded:firefox", "\n".join(lines_dis))
        self.assertGreaterEqual(len(KNOWN_PROFILES), 7)

    def test_start_skip_busy_defers_busy_and_mounts_idle(self) -> None:
        p_busy = self._probe("chrome-flatpak", busy=True)
        p_idle = self._probe("firefox", busy=False)
        host = _FakePsdHost((p_busy, p_idle))
        rc, lines = PsdService(host, prog="cst desktop").run(PsdRequest(PsdAction.START, user="alice", skip_busy=True))
        self.assertEqual(rc, 0)
        joined = "\n".join(lines)
        self.assertIn("deferred: chrome-flatpak", joined)
        self.assertIn("mounted:  firefox", joined)
        self.assertEqual(host.events, [f"tmpfs:{4 * GIB}", "mount:firefox"])


def _mount_stub_runner(tmp: str, *, fail_substr: str = "", log_path: Path | None = None) -> SubprocessCommandRunner:
    bin_dir = Path(tmp, "bin")
    bin_dir.mkdir(exist_ok=True)
    log_target = str(log_path) if log_path is not None else "/dev/null"
    for cmd_name in ("mount", "umount"):
        script = bin_dir / cmd_name
        script.write_text(
            f"""#!/usr/bin/env bash
printf '%s %s\\n' {cmd_name!r} "$*" >> {log_target!r}
case " $* " in
  *{fail_substr!r}*) exit 1 ;;
esac
exit 0
"""
            if fail_substr
            else f"""#!/usr/bin/env bash
printf '%s %s\\n' {cmd_name!r} "$*" >> {log_target!r}
exit 0
""",
            encoding="utf-8",
        )
        os.chmod(script, stat.S_IRWXU)
    return SubprocessCommandRunner(base_env={"PATH": f"{bin_dir}:/usr/bin:/bin"})


class PsdReviewFixesTest(unittest.TestCase):
    """Cover P0/P1 safety fixes: clear_upper_and_work, atomic recovery, Silverblue aliases, mounts, symlinks."""

    def test_foreign_overlays_do_not_block_tmpfs_unmount_and_block_profile(self) -> None:
        raw = (
            "tmpfs /run/cst-psd/1000 tmpfs rw 0 0\n"
            "overlay /var/lib/containers/storage/overlay/merged overlay rw,lowerdir=/a,upperdir=/b,workdir=/w 0 0\n"
            "overlay /home/u/.config/google-chrome overlay rw,lowerdir=/other/low,upperdir=/other/up,workdir=/other/w 0 0\n"
        )
        mounts = parse_psd_mounts(raw, run_base="/run/cst-psd/1000")
        self.assertTrue(mounts.tmpfs_mounted)
        self.assertEqual(mounts.overlay_targets, {})
        self.assertIn("/home/u/.config/google-chrome", mounts.other_mounts)

    def test_clear_upper_and_work_refuses_when_lower_still_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seam_mounts = Path(tmp, "proc", "mounts")
            seam_mounts.parent.mkdir(parents=True)
            low = "/run/cst-psd/1000/firefox/lower"
            up = "/run/cst-psd/1000/firefox/upper"
            wrk = "/run/cst-psd/1000/firefox/work"
            for rel in (low, up, wrk):
                Path(tmp, rel.lstrip("/")).mkdir(parents=True, exist_ok=True)
            Path(tmp, low.lstrip("/"), "prefs.js").write_text("user-data=keep\n", encoding="utf-8")
            seam_mounts.write_text(f"/dev/sda1 {low} btrfs rw 0 0\n", encoding="utf-8")

            probe = PsdProfileProbe(
                PROFILE_BY_ID["firefox"],
                "/home/alice/.config/mozilla/firefox",
                low,
                up,
                wrk,
                "/home/alice/.local/share/cst/psd/recovery/firefox",
                state=PsdProfileState.INACTIVE,
                overlay_active=False,
                lower_bind_active=True,
                recovery_exists=False,
                upper_used_bytes=0,
            )
            host = LinuxPsdHost(
                __file__,
                runner=_mount_stub_runner(tmp),
                seam_root=tmp,
                proc_root=str(seam_mounts.parent),
            )
            with self.assertRaises(Refused):
                host.clear_upper_and_work(probe)
            self.assertTrue(Path(tmp, low.lstrip("/"), "prefs.js").is_file())

            seam_mounts.write_text("", encoding="utf-8")
            Path(tmp, low.lstrip("/"), "prefs.js").unlink()
            Path(tmp, up.lstrip("/"), "dirty.tmp").write_text("x", encoding="utf-8")
            host.clear_upper_and_work(probe)
            self.assertFalse(Path(tmp, up.lstrip("/")).exists())
            self.assertFalse(Path(tmp, wrk.lstrip("/")).exists())
            self.assertFalse(Path(tmp, low.lstrip("/")).exists())

    def test_atomic_recovery_missing_manifest_root_opaque_and_manifest_validation(self) -> None:
        uid, gid = os.getuid(), os.getgid()
        with tempfile.TemporaryDirectory() as tmp:
            rec_no_manifest = Path(tmp, "rec-broken")
            rec_no_manifest.mkdir()
            Path(rec_no_manifest, "prefs.js").write_text("x", encoding="utf-8")
            lower = Path(tmp, "lower")
            lower.mkdir()
            self.assertFalse(has_valid_recovery(str(rec_no_manifest)))
            with self.assertRaises(Refused):
                apply_recovery_to_lower(str(rec_no_manifest), str(lower), uid=uid, gid=gid)

            with self.assertRaises(ValueError):
                parse_whiteout_manifest('{"format": 1, "whiteouts": ["../escape"], "opaque_dirs": []}')
            with self.assertRaises(ValueError):
                parse_whiteout_manifest('{"format": 1, "whiteouts": ["/abs"], "opaque_dirs": []}')

            upper = Path(tmp, "upper")
            upper.mkdir()
            (upper / "fresh.txt").write_text("new", encoding="utf-8")
            (lower / "stale_root.txt").write_text("old", encoding="utf-8")
            rec = Path(tmp, "home", ".local", "share", "cst", "psd", "recovery", "firefox")
            home_bound = Path(tmp, "home")
            probes = OverlayProbes(is_whiteout=lambda _p, _st: False, is_opaque_dir=lambda p: Path(p) == upper)
            snapshot_upper_to_recovery(
                str(upper), str(rec), owner=(uid, gid), home_boundary=str(home_bound), probes=probes
            )
            self.assertTrue(has_valid_recovery(str(rec)))
            manifest = parse_whiteout_manifest((rec / WHITEOUT_MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertIn("", manifest["opaque_dirs"])

            apply_recovery_to_lower(str(rec), str(lower), uid=uid, gid=gid)
            self.assertFalse((lower / "stale_root.txt").exists())
            self.assertEqual((lower / "fresh.txt").read_text(encoding="utf-8"), "new")

    def test_scan_busy_processes_aliases_stat_error_and_maps(self) -> None:
        uid = os.getuid()
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp, "proc")
            (proc / "2001" / "fd").mkdir(parents=True)
            (proc / "2001" / "comm").write_text("chrome\n", encoding="utf-8")
            (proc / "2001" / "maps").write_text(
                "7f00-7f01 r--p 0000 00:00 12 /home/u/.var/app/com.google.Chrome/config/google-chrome/Cache\n",
                encoding="utf-8",
            )
            hits = scan_busy_processes(
                "/var/home/u/.var/app/com.google.Chrome/config/google-chrome",
                uid,
                proc_root=str(proc),
                extra_paths=("/home/u/.var/app/com.google.Chrome/config/google-chrome",),
            )
            self.assertEqual([(h.pid, h.comm) for h in hits], [(2001, "chrome")])

            (proc / "2002" / "fd").mkdir(parents=True)
            (proc / "2002" / "cwd").symlink_to("/var/home/u/.var/app/com.google.Chrome/config/google-chrome")
            orig_stat = os.stat

            def flaky_stat(path: str | bytes | os.PathLike[str]) -> os.stat_result:
                if os.fspath(path) == str(proc / "2002"):
                    raise FileNotFoundError(path)
                return orig_stat(path)

            with mock.patch("desktop.adapters.psd.os.stat", side_effect=flaky_stat):
                hits2 = scan_busy_processes(
                    "/var/home/u/.var/app/com.google.Chrome/config/google-chrome",
                    uid,
                    proc_root=str(proc),
                )
            self.assertEqual(hits2, ())

    def test_mount_overlay_rolls_back_on_make_private_failure_and_ancestor_symlink_blocked(self) -> None:
        uid, gid = os.getuid(), os.getgid()
        user = PsdTargetUser("u", "u", uid, gid, "/home/u")
        probe = PsdProfileProbe(
            PROFILE_BY_ID["chrome-flatpak"],
            "/home/u/.var/app/com.google.Chrome/config/google-chrome",
            f"/run/cst-psd/{uid}/chrome-flatpak/lower",
            f"/run/cst-psd/{uid}/chrome-flatpak/upper",
            f"/run/cst-psd/{uid}/chrome-flatpak/work",
            "/home/u/.local/share/cst/psd/recovery/chrome-flatpak",
            state=PsdProfileState.INACTIVE,
            overlay_active=False,
            lower_bind_active=False,
            recovery_exists=False,
            upper_used_bytes=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "home/u/.var/app/com.google.Chrome/config/google-chrome").mkdir(parents=True)
            Path(tmp, "proc").mkdir()
            Path(tmp, "proc/mounts").write_text("", encoding="utf-8")
            Path(tmp, "proc/meminfo").write_text("MemTotal: 16777216 kB\n", encoding="utf-8")
            log_path = Path(tmp, "mount.log")
            runner = _mount_stub_runner(tmp, fail_substr="--make-private", log_path=log_path)
            host = LinuxPsdHost(__file__, runner=runner, seam_root=tmp, proc_root=str(Path(tmp, "proc")))
            with self.assertRaises(Refused):
                host.mount_overlay(probe, user)
            logged = log_path.read_text(encoding="utf-8")
            self.assertIn("umount ", logged)
            self.assertIn("/lower", logged)

            Path(tmp, "elsewhere").mkdir()
            Path(tmp, "home/u2").mkdir(parents=True)
            Path(tmp, "home/u2/.var").symlink_to(Path(tmp, "elsewhere"))
            user2 = PsdTargetUser("u2", "u2", uid, gid, "/home/u2")
            with mock.patch("desktop.adapters.psd._resolve_psd_user", return_value=user2):
                snap = host.read_snapshot(user="u2", profiles_override="chrome-flatpak")
            self.assertEqual(snap.profiles[0].state, PsdProfileState.SYMLINK_BLOCKED)

    def test_missing_dir_orphan_recovery_fifo_and_vanished_live_file(self) -> None:
        uid, gid = os.getuid(), os.getgid()
        user = PsdTargetUser("u", "u", uid, gid, "/home/u")
        p_missing = PsdProfileProbe(
            PROFILE_BY_ID["discord-flatpak"],
            "/home/u/.var/app/com.discordapp.Discord/config/discord",
            f"/run/cst-psd/{uid}/discord-flatpak/lower",
            f"/run/cst-psd/{uid}/discord-flatpak/upper",
            f"/run/cst-psd/{uid}/discord-flatpak/work",
            "/home/u/.local/share/cst/psd/recovery/discord-flatpak",
            state=PsdProfileState.MISSING_DIR,
            overlay_active=False,
            lower_bind_active=False,
            recovery_exists=False,
            upper_used_bytes=0,
            profile_dir_exists=False,
        )
        host_miss = _FakePsdHost((p_missing,))
        rc_app, lines_app = PsdService(host_miss, prog="cst desktop").run(PsdRequest(PsdAction.APPLY, user="u"))
        self.assertEqual(rc_app, 0)
        self.assertIn("skipped:  discord-flatpak", "\n".join(lines_app))
        self.assertNotIn("mount:discord-flatpak", host_miss.events)

        # Orphan recovery outside PSD_PROFILES is recovered cleanly when lower exists, refused on symlink/missing dir.
        p_orphan_missing = replace(p_missing, state=PsdProfileState.RECOVERY_PENDING, recovery_exists=True)
        snap_orphan = PsdSnapshot(
            user=user,
            user_error="",
            config_raw="firefox",
            config_error="",
            requested_tmpfs_bytes=4 * GIB,
            effective_tmpfs_bytes=4 * GIB,
            mem_total_bytes=16 * GIB,
            tmpfs_mounted=False,
            tmpfs_used_bytes=None,
            tmpfs_avail_bytes=None,
            systemd_installed=True,
            service_enabled=True,
            service_active=False,
            timer_enabled=True,
            timer_active=False,
            profiles=(),
            orphan_recoveries=(p_orphan_missing,),
        )

        class _OrphanHost(_FakePsdHost):
            @override
            def read_snapshot(
                self,
                *,
                user: str | None = None,
                profiles_override: str | None = None,
                size_override: str | None = None,
            ) -> PsdSnapshot:
                _ = (user, profiles_override, size_override)
                return self._override_snap

            def __init__(self, snap_val: PsdSnapshot) -> None:
                super().__init__(())
                self._override_snap = snap_val

        rc_ref, _ = PsdService(_OrphanHost(snap_orphan), prog="cst desktop").run(
            PsdRequest(PsdAction.RECOVER, user="u")
        )
        self.assertEqual(rc_ref, 1)

        p_orphan_ok = replace(p_orphan_missing, profile_dir_exists=True)
        ok_orphan_host = _OrphanHost(replace(snap_orphan, orphan_recoveries=(p_orphan_ok,)))
        rc_ok, lines_ok = PsdService(ok_orphan_host, prog="cst desktop").run(PsdRequest(PsdAction.RECOVER, user="u"))
        self.assertEqual(rc_ok, 0)
        self.assertEqual(ok_orphan_host.events, ["apply_recovery:discord-flatpak"])
        self.assertIn("recovered:discord-flatpak", "\n".join(lines_ok))

        # FIFO in upper removes stale lower entry without hanging; vanished file during live snapshot is skipped.
        with tempfile.TemporaryDirectory() as tmp:
            upper = Path(tmp, "upper")
            lower = Path(tmp, "lower")
            rec = Path(tmp, "rec")
            upper.mkdir()
            lower.mkdir()
            os.mkfifo(upper / "pipe.sock")
            (lower / "pipe.sock").write_text("stale", encoding="utf-8")
            (upper / "vanished.txt").write_text("temp", encoding="utf-8")
            merge_upper_to_lower(str(upper), str(lower), uid=uid, gid=gid)
            self.assertFalse((lower / "pipe.sock").exists())

            orig_lstat = os.lstat

            def vanish_lstat(path: str | bytes | os.PathLike[str]) -> os.stat_result:
                if os.fspath(path) == str(upper / "vanished.txt"):
                    raise FileNotFoundError(path)
                return orig_lstat(path)

            with mock.patch("desktop.adapters.overlay.os.lstat", side_effect=vanish_lstat):
                snapshot_upper_to_recovery(str(upper), str(rec), owner=(uid, gid), home_boundary=tmp)
            self.assertTrue(has_valid_recovery(str(rec)))
            self.assertFalse((rec / "vanished.txt").exists())

            # `discard_recovery` removes a symlink rather than silently leaving it behind.
            sym_rec = Path(tmp, "home/u/.local/share/cst/psd/recovery/firefox")
            sym_rec.parent.mkdir(parents=True)
            sym_rec.symlink_to("/tmp")
            LinuxPsdHost(__file__, runner=_mount_stub_runner(tmp), seam_root=tmp).discard_recovery(
                PsdProfileProbe(
                    PROFILE_BY_ID["firefox"],
                    "/home/u/.config/mozilla/firefox",
                    f"/run/cst-psd/{uid}/firefox/lower",
                    f"/run/cst-psd/{uid}/firefox/upper",
                    f"/run/cst-psd/{uid}/firefox/work",
                    "/home/u/.local/share/cst/psd/recovery/firefox",
                    state=PsdProfileState.RECOVERY_PENDING,
                    overlay_active=False,
                    lower_bind_active=False,
                    recovery_exists=True,
                    upper_used_bytes=0,
                )
            )
            self.assertFalse(sym_rec.is_symlink())


class PsdSystemdUnitTest(unittest.TestCase):
    """Verify shipped `cst-desktop-psd` systemd template ordering and resource weights."""

    UNITS_DIR = Path(__file__).resolve().parents[1] / "units"

    @staticmethod
    def _unit_lines(path: Path) -> list[str]:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_psd_service_orders_before_user_sessions_and_requires_var_home(self) -> None:
        lines = self._unit_lines(self.UNITS_DIR / "cst-desktop-psd@.service")
        self.assertIn("RequiresMountsFor=/var/home", lines)
        self.assertIn("After=local-fs.target", lines)
        self.assertIn("Before=systemd-user-sessions.service display-manager.service", lines)
        before_lines = [line for line in lines if line.startswith("Before=")]
        self.assertEqual(len(before_lines), 1)
        for target in ("shutdown.target", "reboot.target", "halt.target"):
            self.assertNotIn(target, before_lines[0])
        self.assertIn("Type=oneshot", lines)
        self.assertIn("RemainAfterExit=yes", lines)
        self.assertIn("TimeoutStopSec=120", lines)
        self.assertIn("WantedBy=multi-user.target", lines)

    def test_psd_resync_service_sets_low_cpu_and_io_weights(self) -> None:
        lines = self._unit_lines(self.UNITS_DIR / "cst-desktop-psd-resync@.service")
        self.assertIn("CPUWeight=20", lines)
        self.assertIn("IOWeight=20", lines)
        self.assertIn("IOSchedulingClass=idle", lines)
        self.assertIn("Nice=10", lines)
        self.assertIn("BindsTo=cst-desktop-psd@%i.service", lines)
        self.assertIn("After=cst-desktop-psd@%i.service", lines)
