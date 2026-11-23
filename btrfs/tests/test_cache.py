# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs cache` domain, adapters, and CLI."""

from __future__ import annotations

from collections.abc import Sequence
import io
import os
import shutil
import stat
import tempfile
from typing import override
import unittest
import unittest.mock

from btrfs.cache.cli import main as cache_mount_main, run_cache_mount_cli
from btrfs.cache.domain import (
    CACHE_BASE,
    SCRATCHPAD_BASE,
    CACHE_MOUNT_MARKER,
    CacheMountAction,
    CacheMountFindingSeverity,
    CacheMountKind,
    CacheMountUser,
    assess_cache_mount_health,
    build_user_bindings,
    parse_managed_fstab_entries,
    parse_cache_mount_args,
    plan_attach_fstab,
    plan_detach_fstab,
)
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from cst_core.adapters.runtime import ManagedCliEnv
from cst_core.ports import CommandResult
from tests.repo_files import REPO

UUID_ROOT = "11111111-1111-1111-1111-111111111111"
UUID_CACHE_DRIVE = "22222222-2222-2222-2222-222222222222"


class _TestCtx:
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
        return f"/run/host/{subpath}" if self._container else f"/run/{subpath}"


class _FakeMountRunner:
    def __init__(self, root: str, *, same_uuid: bool = False) -> None:
        self.root = root
        self.same_uuid = same_uuid
        self.calls: list[list[str]] = []
        self.mounts: list[tuple[str, str, str, str]] = []

    def _sync_proc_mounts(self) -> None:
        proc_dir = os.path.join(self.root, "proc")
        os.makedirs(proc_dir, exist_ok=True)
        with open(os.path.join(proc_dir, "mounts"), "w", encoding="utf-8") as fh:
            fh.writelines(f"{src} {tgt} {fstype} {opts} 0 0\n" for src, tgt, fstype, opts in self.mounts)

    def add_mount(self, src: str, tgt: str, fstype: str = "btrfs", opts: str = "rw,noatime,compress=zstd:1") -> None:
        self.mounts.append((src, tgt, fstype, opts))
        self._sync_proc_mounts()

    def _handle_findmnt(self, tgt: str) -> CommandResult:
        if f"{self.root}/var/mnt/" in tgt:
            uuid = UUID_ROOT if self.same_uuid else UUID_CACHE_DRIVE
            return CommandResult(0, f"btrfs {uuid} /dev/mapper/cache\n", "")
        logical = tgt.removeprefix(self.root)
        for src, m_tgt, fstype, _ in reversed(self.mounts):
            if logical == m_tgt and m_tgt.endswith(("/.cache", "/Scratchpad")):
                uuid = UUID_ROOT if self.same_uuid else UUID_CACHE_DRIVE
                return CommandResult(0, f"{fstype} {uuid} {src}\n", "")
        return CommandResult(0, f"btrfs {UUID_ROOT} /dev/dm-0\n", "")

    def run(self, argv: Sequence[str], **_kwargs: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd[:4] == ["findmnt", "-rn", "-o", "FSTYPE,UUID,SOURCE"]:
            return self._handle_findmnt(cmd[-1])
        if cmd[:2] == ["mount", "--bind"]:
            self.add_mount(cmd[2].removeprefix(self.root), cmd[3].removeprefix(self.root))
            return CommandResult(0, "", "")
        if cmd and cmd[0] == "umount":
            logical_tgt = cmd[1].removeprefix(self.root)
            self.mounts = [m for m in self.mounts if m[1] != logical_tgt]
            self._sync_proc_mounts()
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


class CacheMountDomainTests(unittest.TestCase):
    def test_parse_cache_mount_args_flags_and_errors(self) -> None:
        self.assertEqual(parse_cache_mount_args([]).action, CacheMountAction.STATUS)
        self.assertEqual(parse_cache_mount_args(["--help"]).action, CacheMountAction.HELP)
        p = parse_cache_mount_args(["attach", "--user", "alice", "--no-scratchpad", "--allow-nonempty"])
        self.assertEqual(
            (p.action, p.user, p.include_cache, p.include_scratchpad, p.allow_nonempty),
            (CacheMountAction.ATTACH, "alice", True, False, True),
        )
        self.assertIsNotNone(parse_cache_mount_args(["attach", "--no-cache", "--no-scratchpad"]).error)
        self.assertIsNotNone(parse_cache_mount_args(["status", "--allow-nonempty"]).error)
        self.assertIsNotNone(parse_cache_mount_args(["attach", "--user", "bad/user"]).error)

    def test_the_old_scratch_flag_is_a_usage_error(self) -> None:
        for argv in (["plan", "--no-scratch"], ["attach", "--no-scratch"], ["detach", "--no-scratch"]):
            p = parse_cache_mount_args(argv)
            self.assertEqual(p.code, 2, argv)
            self.assertIn("--no-scratch", p.error or "")
        p = parse_cache_mount_args(["plan", "--no-scratchpad"])
        self.assertEqual((p.error, p.include_cache, p.include_scratchpad), (None, True, False))

    def test_names_after_the_rename(self) -> None:
        u = CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")
        line = plan_attach_fstab("", build_user_bindings(u))
        self.assertEqual(
            line.splitlines(),
            [
                (
                    "/var/mnt/cache/danilo /var/home/danilo/.cache none "
                    "bind,nofail,x-systemd.requires-mounts-for=/var/mnt/cache 0 0 "
                    "# managed-by: btrfs-cache (CST) user=danilo kind=cache"
                ),
                (
                    "/var/mnt/scratchpad/danilo /var/home/danilo/Scratchpad none "
                    "bind,nofail,x-systemd.requires-mounts-for=/var/mnt/scratchpad 0 0 "
                    "# managed-by: btrfs-cache (CST) user=danilo kind=scratchpad"
                ),
            ],
        )

    def test_a_line_of_the_old_names_is_not_managed(self) -> None:
        old = (
            "/var/mnt/scratch/danilo /var/home/danilo/Scratch none bind,nofail 0 0 "
            "# managed-by: btrfs-scratch (CST) user=danilo kind=scratch\n"
            "/var/mnt/cache/danilo /var/home/danilo/Scratch2 none bind,nofail 0 0 "
            "# managed-by: btrfs-cache (CST) user=danilo kind=scratch\n"
        )
        self.assertEqual(parse_managed_fstab_entries(old), ())
        self.assertEqual(
            plan_detach_fstab(old, "danilo", {CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD}),
            old,
        )

    def test_multi_user_fstab_plan_coexists_and_detaches_cleanly(self) -> None:
        base_fstab = (
            "UUID=root-id / btrfs subvol=root 0 0\n/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n"
        )
        u1 = CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")
        u2 = CacheMountUser("alice", 1001, 1001, "/var/home/alice")
        f1 = plan_attach_fstab(base_fstab, build_user_bindings(u1))
        f2 = plan_attach_fstab(f1, build_user_bindings(u2))
        entries = parse_managed_fstab_entries(f2)
        self.assertEqual(
            [(e.user, e.kind, e.source, e.target) for e in entries],
            [
                ("danilo", CacheMountKind.CACHE, "/var/mnt/cache/danilo", "/var/home/danilo/.cache"),
                ("danilo", CacheMountKind.SCRATCHPAD, "/var/mnt/scratchpad/danilo", "/var/home/danilo/Scratchpad"),
                ("alice", CacheMountKind.CACHE, "/var/mnt/cache/alice", "/var/home/alice/.cache"),
                ("alice", CacheMountKind.SCRATCHPAD, "/var/mnt/scratchpad/alice", "/var/home/alice/Scratchpad"),
            ],
        )
        self.assertEqual(plan_attach_fstab(f2, build_user_bindings(u1)), f2)

        f3 = plan_detach_fstab(f2, "alice", {CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD})
        self.assertEqual(
            [(e.user, e.kind) for e in parse_managed_fstab_entries(f3)],
            [("danilo", CacheMountKind.CACHE), ("danilo", CacheMountKind.SCRATCHPAD)],
        )
        self.assertIn("UUID=root-id / btrfs subvol=root 0 0", f3)

    def test_assess_cache_mount_health_checks_flushoncommit_crypttab_and_mounts(self) -> None:
        self.assertEqual(
            assess_cache_mount_health("UUID=root / btrfs defaults 0 0\n", "/dev/dm-0 / btrfs rw 0 0\n", ""), []
        )

        good_fstab = (
            "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,noatime,compress=zstd:1,nofail 0 0\n"
            "/dev/mapper/cache /var/mnt/scratchpad btrfs subvol=scratchpad,noatime,compress=zstd:1,nofail 0 0\n"
            f"/var/mnt/cache/danilo /var/home/danilo/.cache none bind,nofail 0 0 {CACHE_MOUNT_MARKER} user=danilo kind=cache\n"
        )
        good_mounts = (
            f"/dev/mapper/cache {CACHE_BASE} btrfs rw,noatime,compress=zstd:1 0 0\n"
            f"/dev/mapper/cache {SCRATCHPAD_BASE} btrfs rw,noatime,compress=zstd:1 0 0\n"
            "/dev/mapper/cache /var/home/danilo/.cache btrfs rw,noatime,compress=zstd:1 0 0\n"
        )
        good_crypt = "cache UUID=abcd /etc/cryptsetup-keys.d/cache.key nofail,discard,no-read-workqueue\n"
        ok_res = assess_cache_mount_health(good_fstab, good_mounts, good_crypt)
        self.assertEqual(len(ok_res), 1)
        self.assertEqual(ok_res[0].severity, CacheMountFindingSeverity.OK)

        bad_mounts = good_mounts.replace("compress=zstd:1", "compress=zstd:1,flushoncommit", 1).replace(
            "/dev/mapper/cache /var/home/danilo/.cache btrfs rw,noatime,compress=zstd:1 0 0\n", ""
        )
        bad_crypt = "cache UUID=abcd /etc/cryptsetup-keys.d/cache.key discard\n"
        warns = assess_cache_mount_health(good_fstab, bad_mounts, bad_crypt)
        msgs = "\n".join(f.message for f in warns)
        self.assertIn("flushoncommit", msgs)
        self.assertIn("missing 'nofail'", msgs)
        self.assertIn("in /etc/fstab but not mounted", msgs)
        self.assertTrue(any(f.fix == "btrfs cache attach --user danilo" for f in warns))


class CacheMountAdapterAndCliTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-cache-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "etc"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "var", "mnt", "cache"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "var", "mnt", "scratchpad"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "var", "home", "danilo"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "var", "home", "alice"), exist_ok=True)
        with open(os.path.join(self.tmp, "etc", "passwd"), "w", encoding="utf-8") as fh:
            fh.write(
                "root:x:0:0:root:/root:/bin/bash\n"
                "danilo:x:1000:1000:Danilo:/var/home/danilo:/bin/bash\n"
                "alice:x:1001:1001:Alice:/var/home/alice:/bin/bash\n"
            )
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write("/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n")
        self.runner = _FakeMountRunner(self.tmp)
        self.runner.add_mount("/dev/mapper/cache", CACHE_BASE)
        self.runner.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE)
        self.entry = os.path.join(REPO, "btrfs", "btrfs-cache")

    def _run(
        self, argv: Sequence[str], *, user: str = "danilo", container: bool = False, root: bool = True
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        rc = run_cache_mount_cli(
            argv,
            deps=ManagedCliEnv(
                entry_file=self.entry,
                env={"CST_BTRFS_CACHE_ROOT": self.tmp, "USER": user},
                out=out,
                err=err,
                ctx=_TestCtx(container=container, root=root),
                runner=self.runner,
            ),
        )
        return rc, out.getvalue(), err.getvalue()

    def test_multi_user_attach_idempotent_and_detach(self) -> None:
        rc_ok, out_ok, err_ok = self._run(["attach"])
        self.assertEqual(rc_ok, 0, err_ok)
        self.assertIn("Mounted /var/mnt/cache/danilo -> /var/home/danilo/.cache.", out_ok)
        src_c = os.path.join(self.tmp, "var", "mnt", "cache", "danilo")
        src_s = os.path.join(self.tmp, "var", "mnt", "scratchpad", "danilo")
        self.assertEqual(stat.S_IMODE(os.stat(src_c).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(src_s).st_mode), 0o700)
        with open(os.path.join(src_s, CACHE_TAG), "rb") as fh:
            self.assertTrue(fh.read().startswith(CACHE_SIGNATURE))

        self.runner.calls.clear()
        rc_again, out_again, _ = self._run(["attach"])
        self.assertEqual(rc_again, 0)
        self.assertIn("Already attached for danilo; nothing to do.", out_again)

        rc_alice, _, err_alice = self._run(["attach", "--user", "alice"])
        self.assertEqual(rc_alice, 0, err_alice)
        with open(os.path.join(self.tmp, "etc", "fstab"), encoding="utf-8") as fh:
            fstab = fh.read()
        self.assertIn("user=danilo kind=cache", fstab)
        self.assertIn("user=alice kind=scratchpad", fstab)

        marker_file = os.path.join(self.tmp, "var", "mnt", "scratchpad", "alice", "build.log")
        with open(marker_file, "w", encoding="utf-8") as fh:
            fh.write("ok")
        rc_det, out_det, err_det = self._run(["detach", "--user", "alice"])
        self.assertEqual(rc_det, 0, err_det)
        self.assertIn("Unmounted /var/home/alice/.cache.", out_det)
        self.assertTrue(os.path.isfile(marker_file))
        with open(os.path.join(self.tmp, "etc", "fstab"), encoding="utf-8") as fh:
            fstab_after = fh.read()
        self.assertNotIn("user=alice", fstab_after)
        self.assertIn("user=danilo kind=cache", fstab_after)

    def test_safety_guards_nonempty_symlink_uuid_and_container(self) -> None:
        cache_d = os.path.join(self.tmp, "var", "home", "danilo", ".cache")
        os.makedirs(cache_d, exist_ok=True)
        with open(os.path.join(cache_d, "keep.bin"), "w", encoding="utf-8") as fh:
            fh.write("x")
        rc_plan, out_plan, _ = self._run(["plan"])
        self.assertEqual(rc_plan, 0)
        self.assertIn("BLOCKED:", out_plan)
        rc_fail, _, err_fail = self._run(["attach"])
        self.assertEqual(rc_fail, 1)
        self.assertIn("non-empty", err_fail)

        rc_allow, out_allow, err_allow = self._run(["attach", "--allow-nonempty"])
        self.assertEqual(rc_allow, 0, err_allow)
        self.assertIn("Mounted /var/mnt/cache/danilo -> /var/home/danilo/.cache.", out_allow)

        link_tgt = os.path.join(self.tmp, "var", "home", "alice", ".cache")
        os.symlink("/tmp/other", link_tgt)
        rc_sym, _, err_sym = self._run(["attach", "--user", "alice", "--no-scratchpad"])
        self.assertEqual(rc_sym, 1)
        self.assertIn("symlink", err_sym)
        os.unlink(link_tgt)

        self.runner.same_uuid = True
        rc_uuid, _, err_uuid = self._run(["attach", "--user", "alice", "--no-scratchpad"])
        self.assertEqual(rc_uuid, 1)
        self.assertIn("same-filesystem", err_uuid)

        self.runner.same_uuid = False
        self.assertEqual(self._run(["status"], container=True, root=False)[0], 0)
        with unittest.mock.patch("sys.exit"):
            rc_box, _, err_box = self._run(["attach"], container=True, root=False)
        self.assertEqual(rc_box, 1)
        self.assertIn("toolbox/container", err_box)
        self.assertEqual(cache_mount_main(["--help"]), 0)
