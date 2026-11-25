# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs cache` domain, adapters, and CLI."""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import errno
import io
import json
import os
import shutil
import signal
import stat
import tempfile
from typing import override
import unittest
import unittest.mock

from btrfs.cache import adapters as cache_adapters
from btrfs.cache.cli import main as cache_mount_main, run_cache_mount_cli
from btrfs.cache.domain import (
    CACHE_BASE,
    SCRATCHPAD_BASE,
    CACHE_MOUNT_MARKER,
    CacheMountAction,
    CacheMountKind,
    CacheMountUser,
    LiveMount,
    build_user_bindings,
    mount_holding,
    mounts_by_target,
    parse_findmnt_mounts,
    parse_managed_fstab_entries,
    parse_cache_mount_args,
    plan_attach_fstab,
    plan_detach_fstab,
    unmanaged_fstab_targets,
)
from btrfs.cache.domain.health import (
    CacheMountFindingSeverity,
    assess_cache_mount_health,
    dm_device_names,
    parse_crypttab_cache_issues,
)
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS
from btrfs.snapshot.domain import CACHE_SIGNATURE, CACHE_TAG
from btrfs.tests.fakes import UUID_CACHE_DRIVE, CacheTestCtx, FakeMountRunner
from cst_core.adapters.runtime import ManagedCliEnv
from cst_core.ports import CommandResult
from tests.fault_injection import Killed
from tests.repo_files import REPO

MOUNT_UNIT_ARGS = [a for p in MOUNT_UNIT_PROPS for a in ("-p", p)]
MOUNT_UNITS_SHOW = ["systemctl", "show", "--state=loaded", *MOUNT_UNIT_ARGS, "*.mount"]
"""The one question cache asks systemd: every mount unit it has loaded, active or not."""


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

    def test_options_first_mean_status(self) -> None:
        p = parse_cache_mount_args(["--user", "alice"])
        self.assertEqual((p.action, p.user, p.error), (CacheMountAction.STATUS, "alice", None))
        p = parse_cache_mount_args(["--user=alice"])
        self.assertEqual((p.action, p.user, p.error), (CacheMountAction.STATUS, "alice", None))
        bare = parse_cache_mount_args(["--no-cache"])
        self.assertEqual((bare.action, bare.code), (CacheMountAction.STATUS, 2))
        self.assertEqual(bare.error, parse_cache_mount_args(["status", "--no-cache"]).error)
        self.assertEqual(parse_cache_mount_args(["--help"]).action, CacheMountAction.HELP)
        self.assertEqual(parse_cache_mount_args(["-h"]).action, CacheMountAction.HELP)

    def test_findmnt_json_keeps_source_when_uuid_is_null(self) -> None:
        text = json.dumps(
            {
                "filesystems": [
                    {
                        "source": "/dev/x",
                        "target": "/a b",
                        "fstype": "btrfs",
                        "options": "rw",
                        "uuid": None,
                        "fsroot": "/",
                    },
                    {
                        "source": "/dev/x[/c]",
                        "target": "/a b",
                        "fstype": "btrfs",
                        "options": "rw",
                        "uuid": "U",
                        "fsroot": "/c",
                    },
                ]
            }
        )
        mounts = parse_findmnt_mounts(text)
        assert mounts is not None
        self.assertEqual((mounts[0].source, mounts[0].uuid, mounts[0].target), ("/dev/x", "", "/a b"))
        self.assertEqual(mounts_by_target(mounts)["/a b"].fsroot, "/c")
        for bad in ("", "[]", '{"filesystems": [{"source": "/dev/x"}]}', '{"filesystems": 3}', "not json"):
            self.assertIsNone(parse_findmnt_mounts(bad), bad)

    def test_mount_holding_is_the_longest_leading_folder(self) -> None:
        def m(target: str, uuid: str) -> LiveMount:
            return LiveMount("/dev/x", target, "btrfs", "rw", uuid, "/")

        mounts = (m("/", "R"), m("/var/home", "H"), m("/var/homes", "X"), m("/var/home", "TOP"))
        for path, uuid in (("/var/home/u", "TOP"), ("/var/home", "TOP"), ("/var/homeu", "R"), ("/var/homes/x", "X")):
            held = mount_holding(mounts, path)
            self.assertEqual(held.uuid if held else "", uuid, path)
        self.assertIsNone(mount_holding((), "/x"))

    def test_fstab_fields_are_escaped_and_read_back(self) -> None:
        home = "/var/home/u\\x\ty\nz two"
        u = CacheMountUser("u", 1000, 1000, home)
        text = plan_attach_fstab("", build_user_bindings(u, include_scratchpad=False))
        self.assertEqual(
            text,
            "/var/mnt/cache/u /var/home/u\\134x\\011y\\012z\\040two/.cache none "
            "bind,nofail,x-systemd.requires-mounts-for=/var/mnt/cache 0 0 "
            f"{CACHE_MOUNT_MARKER} user=u kind=cache\n",
        )
        entries = parse_managed_fstab_entries(text)
        self.assertEqual([(e.source, e.target) for e in entries], [("/var/mnt/cache/u", f"{home}/.cache")])

    def test_only_a_newline_ends_an_fstab_line(self) -> None:
        # str.splitlines() also split at a carriage return, a form feed and other separators: a CRLF
        # line lost its CR, and the second half of a comment holding a form feed became a live entry.
        u = CacheMountUser("u", 1000, 1000, "/var/home/u")
        bindings = build_user_bindings(u, include_scratchpad=False)
        line = plan_attach_fstab("", bindings)
        other = (
            "UUID=r / btrfs defaults 0 0\r\n"
            "# old\x0c/dev/sdb1 /data ext4 defaults 0 0\n"
            "# a\u2028/dev/sdc1 /more ext4 defaults 0 0\x1c\x85\n"
        )
        attached = plan_attach_fstab(other, bindings)
        self.assertEqual(attached, other + line)
        self.assertEqual(plan_detach_fstab(attached, "u", {CacheMountKind.CACHE}), other)
        self.assertEqual(unmanaged_fstab_targets(attached), {"/"})
        self.assertEqual(
            [(e.user, e.target) for e in parse_managed_fstab_entries(attached)], [("u", "/var/home/u/.cache")]
        )

    def test_a_repeated_managed_line_goes(self) -> None:
        u = CacheMountUser("u", 1000, 1000, "/var/home/u")
        line = plan_attach_fstab("", build_user_bindings(u, include_scratchpad=False))
        stale = line.replace("/var/mnt/cache/u ", "/var/mnt/cache/old ", 1)
        fstab = f"UUID=r / btrfs defaults 0 0\n{stale}{line}"
        self.assertEqual(
            plan_attach_fstab(fstab, build_user_bindings(u, include_scratchpad=False)),
            f"UUID=r / btrfs defaults 0 0\n{line}",
        )
        self.assertEqual(plan_detach_fstab(fstab, "u", {CacheMountKind.CACHE}), "UUID=r / btrfs defaults 0 0\n")

    def test_the_cache_drives_crypttab_line_is_found_through_dm_and_luks_names(self) -> None:
        crypt = "luks-1234 UUID=abc none discard\nluks-abcd UUID=def none discard\n"
        warn = ["/etc/crypttab entry 'luks-1234' is missing 'nofail' (boot could stall if NVMe is absent)"]
        self.assertEqual(parse_crypttab_cache_issues(crypt, ["/dev/dm-3"], {"dm-3": "luks-1234"}), warn)
        self.assertEqual(parse_crypttab_cache_issues(crypt, ["/dev/mapper/luks-1234[/cache]"]), warn)
        self.assertEqual(parse_crypttab_cache_issues(crypt, ["/dev/dm-3"], {"dm-4": "luks-1234"}), [])
        self.assertEqual(parse_crypttab_cache_issues(crypt, ["/dev/mapper/other"]), [])
        defaults = "cache-nvme UUID=a none discard\nluks-cache2 UUID=b none discard\ncache UUID=c none nofail\n"
        self.assertEqual(
            [i.split("'")[1] for i in parse_crypttab_cache_issues(defaults, [])], ["cache-nvme", "luks-cache2"]
        )
        self.assertEqual(
            dm_device_names(["/dev/dm-3[/cache]", "/dev/dm-x", "/dev/mapper/a", "/dev/dm-12"]), {"dm-3", "dm-12"}
        )
        found = assess_cache_mount_health(
            "UUID=feed /var/mnt/cache btrfs subvol=cache,nofail 0 0\n",
            "/dev/dm-3 /var/mnt/cache btrfs rw,noatime 0 0\n",
            crypt,
            {"dm-3": "luks-1234"},
        )
        self.assertEqual([f.message for f in found if "crypttab" in f.message], warn)

    def test_crypttab_skips_comments_short_lines_and_lines_with_nofail(self) -> None:
        crypt = (
            "# cache UUID=x none discard\n"
            "\n"
            "cache\n"
            "cache-a UUID=x none luks\n"
            "luks-root UUID=y none luks\n"
            "cache-b UUID=z none luks,nofail\n"
            "mine UUID=q none luks\n"
        )
        self.assertEqual(
            parse_crypttab_cache_issues(crypt, ["/dev/mapper/mine"]),
            [
                "/etc/crypttab entry 'cache-a' is missing 'nofail' (boot could stall if NVMe is absent)",
                "/etc/crypttab entry 'mine' is missing 'nofail' (boot could stall if NVMe is absent)",
            ],
        )
        self.assertEqual(parse_crypttab_cache_issues("", []), [])
        self.assertEqual(parse_crypttab_cache_issues(None, []), [])

    def test_doctor_warns_when_a_managed_target_has_another_mount(self) -> None:
        # Any mount at the target counted as an active bind: a tmpfs, another drive or another user's
        # folder there gave the ok line, while status says "mounted (other source!)".
        fstab = (
            "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n"
            f"/var/mnt/cache/u /var/home/u/.cache none bind,nofail 0 0 {CACHE_MOUNT_MARKER} user=u kind=cache\n"
        )
        crypt = "cache UUID=abcd none nofail\n"
        base = "/dev/mapper/cache[/cache] /var/mnt/cache btrfs rw 0 0\n"
        warn = "/var/home/u/.cache (user=u) has a mount that is not CST's bind of /var/mnt/cache/u"
        for at_target, warned in (
            ("/dev/mapper/cache[/cache/u] /var/home/u/.cache btrfs rw 0 0\n", False),
            # No [FSROOT] under a base shown with one: findmnt's text, so the drive's top level, not /cache/u.
            ("/dev/mapper/cache /var/home/u/.cache btrfs rw 0 0\n", True),
            ("tmpfs /var/home/u/.cache tmpfs rw 0 0\n", True),
            ("/dev/sdb1 /var/home/u/.cache ext4 rw 0 0\n", True),
            ("/dev/mapper/cache[/cache/alice] /var/home/u/.cache btrfs rw 0 0\n", True),
        ):
            with self.subTest(at_target=at_target):
                found = assess_cache_mount_health(fstab, base + at_target, crypt)
                self.assertEqual([f.message for f in found if f.message == warn], [warn] if warned else [])
                if warned:
                    self.assertIn("btrfs cache status --user u", [f.fix for f in found])
                else:
                    self.assertEqual([f.severity for f in found], [CacheMountFindingSeverity.OK])

    def test_doctor_reads_a_base_without_a_root_as_the_top_of_the_drive(self) -> None:
        # findmnt shows "[FSROOT]" only for a root other than "/": a base mounted at the drive's top
        # level has none. "root unknown" made a bind of another user's folder there CST's bind (ok),
        # while status called it "mounted (other source!)".
        fstab = (
            "/dev/mapper/cache /var/mnt/cache btrfs nofail 0 0\n"
            f"/var/mnt/cache/u /var/home/u/.cache none bind,nofail 0 0 {CACHE_MOUNT_MARKER} user=u kind=cache\n"
        )
        crypt = "cache UUID=abcd none nofail\n"
        base = "/dev/mapper/cache /var/mnt/cache btrfs rw 0 0\n"
        warn = "/var/home/u/.cache (user=u) has a mount that is not CST's bind of /var/mnt/cache/u"
        for at_target, warned in (
            ("/dev/mapper/cache[/bob] /var/home/u/.cache btrfs rw 0 0\n", True),
            ("/dev/mapper/cache[/u] /var/home/u/.cache btrfs rw 0 0\n", False),
            ("/dev/mapper/cache /var/home/u/.cache btrfs rw 0 0\n", False),  # neither root named: cannot tell
        ):
            with self.subTest(at_target=at_target):
                found = assess_cache_mount_health(fstab, base + at_target, crypt)
                self.assertEqual([f.message for f in found if f.message == warn], [warn] if warned else [])
                if not warned:
                    self.assertEqual([f.severity for f in found], [CacheMountFindingSeverity.OK])

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
        self.runner = FakeMountRunner(self.tmp)
        self.runner.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
        self.runner.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")
        self.entry = os.path.join(REPO, "btrfs", "btrfs-cache")

    def _fstab(self) -> str:
        with open(os.path.join(self.tmp, "etc", "fstab"), encoding="utf-8") as fh:
            return fh.read()

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
                ctx=CacheTestCtx(container=container, root=root),
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

    def test_status_names_each_base_state_and_the_other_users(self) -> None:
        self.assertEqual(self._run(["attach", "--user", "alice"])[0], 0)
        self.runner.mounts = [m for m in self.runner.mounts if m["target"] not in (CACHE_BASE, SCRATCHPAD_BASE)]
        rc, out, err = self._run(["status"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            out.split("\n")[1:],
            [
                "Dedicated cache drive mounts:",
                "  /var/mnt/cache     in fstab (not mounted)",
                "  /var/mnt/scratchpad not provisioned",
                "  target user        danilo (uid=1000, home=/var/home/danilo)",
                "  cache              /var/mnt/cache/danilo -> /var/home/danilo/.cache [not attached]",
                "  scratchpad         /var/mnt/scratchpad/danilo -> /var/home/danilo/Scratchpad [not attached]",
                "  other fstab users  alice",
                "",
            ],
        )

    def test_status_of_a_user_that_does_not_exist(self) -> None:
        rc, out, err = self._run(["status", "--user", "cst-test-no-such-user"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            out.split("\n")[1:],
            [
                "Dedicated cache drive mounts:",
                f"  /var/mnt/cache     mounted (btrfs, uuid={UUID_CACHE_DRIVE})",
                f"  /var/mnt/scratchpad mounted (btrfs, uuid={UUID_CACHE_DRIVE})",
                "  target user        unavailable (user 'cst-test-no-such-user' does not exist)",
                "",
            ],
        )

    def test_user_option_alone_shows_that_users_status(self) -> None:
        rc, out, err = self._run(["--user", "alice"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("target user        alice (uid=1001, home=/var/home/alice)", out)
        self.assertIn("/var/mnt/cache/alice -> /var/home/alice/.cache [not attached]", out)

    def test_a_bind_of_another_folder_of_the_drive_is_not_attached(self) -> None:
        tgt = "/var/home/danilo/.cache"
        os.makedirs(os.path.join(self.tmp, tgt.lstrip("/")))
        self.runner.add_mount("/dev/mapper/cache", tgt, fsroot="/cache/alice", bind=True)
        _, out, _ = self._run(["status", "--user", "danilo"])
        self.assertIn(f"/var/mnt/cache/danilo -> {tgt} [mounted (other source!)]", out)
        rc_plan, out_plan, _ = self._run(["plan", "--no-scratchpad"])
        self.assertEqual(rc_plan, 0)
        self.assertIn(f"BLOCKED: {tgt} already has a mount CST did not make", out_plan)
        before = self._fstab()
        rc, _, err = self._run(["attach", "--no-scratchpad"])
        self.assertEqual(rc, 1)
        self.assertIn("a mount CST did not make", err)
        self.assertEqual(self._fstab(), before)
        self.assertFalse(any(c[:1] == ["mount"] for c in self.runner.calls))

    def test_the_mount_on_top_decides(self) -> None:
        tgt = "/var/home/danilo/.cache"
        self.runner.add_mount("/dev/mapper/cache", tgt, fsroot="/cache/danilo", bind=True)
        _, out, _ = self._run(["status"])
        self.assertIn(f"-> {tgt} [mounted (not in fstab)]", out)
        self.runner.add_mount("tmpfs", tgt, fsroot="/", uuid=None)
        _, out, _ = self._run(["status"])
        self.assertIn(f"-> {tgt} [mounted (other source on top of CST's bind!)]", out)

    def test_a_home_with_a_space_matches_its_escaped_fstab_line(self) -> None:
        with open(os.path.join(self.tmp, "etc", "passwd"), "a", encoding="utf-8") as fh:
            fh.write("bob:x:1002:1002:Bob:/var/home/bob two:/bin/bash\n")
        os.makedirs(os.path.join(self.tmp, "var", "home", "bob two", ".cache"))
        tgt = "/var/home/bob two/.cache"
        self.runner.add_mount("/dev/mapper/cache", tgt, fsroot="/cache/bob", bind=True)
        with open(os.path.join(self.tmp, "etc", "fstab"), "a", encoding="utf-8") as fh:
            fh.write(
                "/var/mnt/cache/bob /var/home/bob\\040two/.cache none bind,nofail 0 0 "
                f"{CACHE_MOUNT_MARKER} user=bob kind=cache\n"
            )
        _, out, _ = self._run(["status", "--user", "bob"])
        self.assertIn(f"/var/mnt/cache/bob -> {tgt} [active (fstab + mounted)]", out)

    def test_an_unmanaged_fstab_line_on_a_target_blocks(self) -> None:
        with open(os.path.join(self.tmp, "etc", "fstab"), "a", encoding="utf-8") as fh:
            fh.write("/x /var/home/danilo/.cache none bind 0 0\n")
        before = self._fstab()
        _, out_s, _ = self._run(["status"])
        self.assertIn("-> /var/home/danilo/.cache [fstab line CST does not manage (blocked)]", out_s)
        _, out_p, _ = self._run(["plan"])
        self.assertIn(
            "BLOCKED: /var/home/danilo/.cache has an /etc/fstab line CST does not manage; remove it first", out_p
        )
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn("an /etc/fstab line CST does not manage", err)
        self.assertEqual(self._fstab(), before)
        self.assertFalse(any(c[:1] == ["mount"] for c in self.runner.calls))
        rc_s, _, err_s = self._run(["attach", "--no-cache"])
        self.assertEqual(rc_s, 0, err_s)

    def test_a_symlinked_source_blocks_and_its_target_keeps_its_mode(self) -> None:
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere, 0o755)
        os.chmod(elsewhere, 0o755)
        os.symlink(elsewhere, os.path.join(self.tmp, "var", "mnt", "cache", "danilo"))
        before = self._fstab()
        _, out_s, _ = self._run(["status"])
        self.assertIn("-> /var/home/danilo/.cache [source is a symlink (blocked)]", out_s)
        _, out_p, _ = self._run(["plan"])
        self.assertIn("BLOCKED: /var/mnt/cache/danilo is a symlink (refusing to follow it)", out_p)
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn("is a symlink", err)
        self.assertEqual(stat.S_IMODE(os.stat(elsewhere).st_mode), 0o755)
        self.assertEqual(self._fstab(), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "var", "home", "danilo", ".cache")))

    def test_a_file_at_the_target_blocks_even_with_allow_nonempty(self) -> None:
        with open(os.path.join(self.tmp, "var", "home", "danilo", ".cache"), "w", encoding="utf-8") as fh:
            fh.write("not a folder")
        before = self._fstab()
        _, out_p, _ = self._run(["plan", "--allow-nonempty"])
        self.assertIn("BLOCKED: /var/home/danilo/.cache exists and is not a folder", out_p)
        rc, _, err = self._run(["attach", "--allow-nonempty"])
        self.assertEqual(rc, 1)
        self.assertIn("exists and is not a folder", err)
        self.assertEqual(self._fstab(), before)
        self.assertFalse(any(c[:1] == ["mount"] for c in self.runner.calls))

    def test_a_missing_home_blocks_and_nothing_is_created(self) -> None:
        with open(os.path.join(self.tmp, "etc", "passwd"), "a", encoding="utf-8") as fh:
            fh.write("carol:x:1003:1003:Carol:/var/home/carol:/bin/bash\n")
        _, out_p, _ = self._run(["plan", "--user", "carol"])
        self.assertIn("BLOCKED: home folder /var/home/carol does not exist", out_p)
        rc, _, err = self._run(["attach", "--user", "carol"])
        self.assertEqual(rc, 1)
        self.assertIn("home folder /var/home/carol does not exist", err)
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "var", "home", "carol")))
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "var", "mnt", "cache", "carol")))

    def test_other_fstab_bytes_survive_attach(self) -> None:
        path = os.path.join(self.tmp, "etc", "fstab")
        with open(path, "ab") as fh:
            fh.write(b"# caf\xe9 \xff written by an old tool\n")
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertIn(b"# caf\xe9 \xff written by an old tool\n", data)
        self.assertIn(b"user=danilo kind=cache", data)

    def test_a_link_put_in_place_of_a_folder_after_the_probe_fails_cleanly(self) -> None:
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere, 0o755)
        os.chmod(elsewhere, 0o755)
        real_ensure_dir = cache_adapters.ensure_dir

        def swap(path: str, mode: int, *, relabel: bool) -> list[str]:
            if path.endswith("/var/mnt/cache/danilo"):
                os.symlink(elsewhere, path)
                return []
            return real_ensure_dir(path, mode, relabel=relabel)

        before = self._fstab()
        with unittest.mock.patch.object(cache_adapters, "ensure_dir", side_effect=swap):
            rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 1, out)
        self.assertIn("ERROR: cannot prepare /var/mnt/cache/danilo and /var/home/danilo/.cache:", err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(stat.S_IMODE(os.stat(elsewhere).st_mode), 0o755)
        self.assertEqual(self._fstab(), before)

    def test_the_tags_owner_is_never_given_through_a_link(self) -> None:
        # The user owns /var/mnt/scratchpad/<user> by then: a CACHEDIR.TAG swapped for a link right
        # after the rename must not hand the link's target to the user (os.chown by path followed it).
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w", encoding="utf-8") as fh:
            fh.write("secret\n")
        tag = os.path.join(self.tmp, "var", "mnt", "scratchpad", "danilo", CACHE_TAG)
        real_replace = os.replace
        chowned: list[tuple[str, int, int]] = []

        def replace_then_swap(src: str, dst: str) -> None:
            real_replace(src, dst)
            if dst == tag:
                os.rename(tag, f"{tag}.moved")
                os.symlink(victim, tag)

        def fchown(fd: int, uid: int, gid: int) -> None:
            chowned.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid))

        def chown(path: str, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
            chowned.append((os.path.realpath(path) if follow_symlinks else path, uid, gid))

        with (
            unittest.mock.patch("os.geteuid", return_value=0),
            unittest.mock.patch("os.replace", side_effect=replace_then_swap),
            unittest.mock.patch("os.fchown", side_effect=fchown),
            unittest.mock.patch("os.chown", side_effect=chown),
            unittest.mock.patch("shutil.which", return_value=None),
        ):
            rc, _, err = self._run(["attach", "--no-cache"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn(victim, [path for path, _, _ in chowned])
        tag_tmp = [
            (uid, gid)
            for path, uid, gid in chowned
            if os.path.dirname(path) == os.path.dirname(tag) and os.path.basename(path).startswith(f".{CACHE_TAG}.")
        ]
        self.assertEqual(tag_tmp, [(1000, 1000)])

    def _mounted(self) -> list[str]:
        return [str(m["target"]) for m in self.runner.mounts if str(m["target"]).startswith("/var/home/")]

    def test_attach_mounts_before_it_writes_fstab(self) -> None:
        before = self._fstab()
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.runner.fstab_at_mount, [before, before])
        self.assertNotEqual(self._fstab(), before)
        calls = [c[0] if c[0] != "mount" else "mount" for c in self.runner.calls if c[0] in ("mount", "systemctl")]
        self.assertEqual(calls[-1], "systemctl")

    def test_a_failed_mount_takes_back_the_binds_of_the_run(self) -> None:
        self.runner.mount_fails.add("/var/home/danilo/Scratchpad")
        before = self._fstab()
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: mount --bind /var/mnt/scratchpad/danilo /var/home/danilo/Scratchpad failed: "
            "mount: permission denied; mounts and /etc/fstab restored.",
            err,
        )
        self.assertEqual(self._mounted(), [])
        self.assertIn(["umount", os.path.join(self.tmp, "var/home/danilo/.cache")], self.runner.calls)
        self.assertEqual(self._fstab(), before)

    def test_a_failed_reload_without_fstab_before_leaves_no_fstab(self) -> None:
        os.unlink(os.path.join(self.tmp, "etc", "fstab"))
        self.runner.reload_fails = True
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: systemctl daemon-reload failed: Failed to reload daemon; mounts and /etc/fstab restored.", err
        )
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "etc", "fstab")))
        self.assertEqual(self._mounted(), [])
        # Only the mounts and /etc/fstab are taken back: the folders and the tag it made stay.
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "var", "mnt", "scratchpad", "danilo", CACHE_TAG)))

    def test_an_fstab_already_gone_counts_as_put_back(self) -> None:
        # With no /etc/fstab before, putting it back is removing CST's. When it is already gone, the
        # unlink's ENOENT was an undo error: "ROLLBACK INCOMPLETE (cannot put /etc/fstab back: No such
        # file or directory)", although /etc/fstab was as it was.
        fstab = os.path.join(self.tmp, "etc", "fstab")
        os.unlink(fstab)
        self.runner.reload_fails = True
        self.runner.on_failed_reload = lambda: os.unlink(fstab)
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: systemctl daemon-reload failed: Failed to reload daemon; mounts and /etc/fstab restored.", err
        )
        self.assertNotIn("ROLLBACK INCOMPLETE", err)
        self.assertFalse(os.path.lexists(fstab))
        self.assertEqual(self._mounted(), [])

    def test_a_failed_reload_puts_the_old_fstab_back(self) -> None:
        self.runner.reload_fails = True
        before = self._fstab()
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn("mounts and /etc/fstab restored.", err)
        self.assertEqual(self._fstab(), before)
        self.assertEqual(self._mounted(), [])

    def _fstab_becomes_a_folder(self) -> None:
        """At the failed reload, /etc/fstab becomes a folder that is not empty: putting it back fails."""
        fstab = os.path.join(self.tmp, "etc", "fstab")

        def hook() -> None:
            if os.path.lexists(fstab):
                os.unlink(fstab)
            os.makedirs(os.path.join(fstab, "x"))

        self.runner.reload_fails = True
        self.runner.on_failed_reload = hook

    def test_an_fstab_that_cannot_be_put_back_is_rollback_incomplete(self) -> None:
        # The message said "mounts and /etc/fstab restored." while /etc/fstab still held CST's new
        # lines, which mount at the next boot.
        for had_fstab in (True, False):
            with self.subTest(had_fstab=had_fstab):
                self.setUp()
                if not had_fstab:
                    os.unlink(os.path.join(self.tmp, "etc", "fstab"))  # put back means unlink
                self._fstab_becomes_a_folder()
                rc, _, err = self._run(["attach"])
                self.assertEqual(rc, 1)
                self.assertIn(
                    "ERROR: systemctl daemon-reload failed: Failed to reload daemon; "
                    "ROLLBACK INCOMPLETE (cannot put /etc/fstab back: Is a directory)",
                    err,
                )
                self.assertNotIn("restored.", err)
                self.assertEqual(self._mounted(), [])  # the binds were still taken back

    def test_a_failed_detach_names_an_fstab_it_cannot_put_back(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self._fstab_becomes_a_folder()
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: systemctl daemon-reload failed: Failed to reload daemon; "
            "ROLLBACK INCOMPLETE (cannot put /etc/fstab back: Is a directory)",
            err,
        )
        self.assertNotIn("restored.", err)
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])

    def _second_fstab_write_fails(self) -> contextlib.AbstractContextManager[object]:
        """detach's put-back of /etc/fstab (its second write there) fails with ENOSPC."""
        fstab = os.path.join(self.tmp, "etc", "fstab")
        real_write = cache_adapters.write_atomic
        writes: list[str] = []

        def write_atomic(path: str, data: str | bytes, *, mode: int) -> None:
            if path == fstab:
                writes.append(path)
                if len(writes) == 2:
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real_write(path, data, mode=mode)

        return unittest.mock.patch.object(cache_adapters, "write_atomic", side_effect=write_atomic)

    def test_a_bind_and_an_fstab_not_undone_are_both_named(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.runner.umount_fails.add("/var/home/danilo/Scratchpad")  # after .cache's umount
        self.runner.mount_fails.add("/var/home/danilo/.cache")  # mounting it again fails
        with self._second_fstab_write_fails():
            rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/Scratchpad failed: umount: target is busy; ROLLBACK INCOMPLETE "
            "(mount --bind /var/mnt/cache/danilo /var/home/danilo/.cache failed: mount: permission denied; "
            "cannot put /etc/fstab back: No space left on device)",
            err,
        )

    def test_a_bind_not_mounted_again_gets_no_line_back(self) -> None:
        # The put-back after a busy umount wrote every line back, also .cache's, whose bind could not
        # be mounted again: the next boot mounted a bind detach had dropped.
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.umount_fails.add("/var/home/danilo/Scratchpad")  # after .cache's umount
        self.runner.mount_fails.add("/var/home/danilo/.cache")  # mounting it again fails
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/Scratchpad failed: umount: target is busy; ROLLBACK INCOMPLETE "
            "(mount --bind /var/mnt/cache/danilo /var/home/danilo/.cache failed: mount: permission denied)",
            err,
        )
        self.assertEqual(self._mounted(), ["/var/home/danilo/Scratchpad"])
        self.assertEqual(self._fstab(), plan_detach_fstab(attached, "danilo", {CacheMountKind.CACHE}))
        self.assertIn("user=danilo kind=scratchpad", self._fstab())
        self.runner.umount_fails.clear()
        self.runner.mount_fails.clear()
        rc_again, _, err_again = self._run(["detach"])
        self.assertEqual(rc_again, 0, err_again)
        self.assertEqual(self._mounted(), [])
        self.assertNotIn("user=danilo", self._fstab())

    def test_a_folder_that_cannot_be_made_is_an_error_not_a_traceback(self) -> None:
        with unittest.mock.patch.object(
            cache_adapters, "ensure_dir", side_effect=PermissionError(13, "Permission denied")
        ):
            rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: cannot prepare /var/mnt/cache/danilo and /var/home/danilo/.cache: Permission denied", err)
        self.assertNotIn("Traceback", out + err)
        self.assertFalse(any(c[:1] == ["mount"] for c in self.runner.calls))

    def test_detach_keeps_a_mount_cst_did_not_make(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        cache_tgt = os.path.join(self.tmp, "var/home/danilo/.cache")
        self.runner.run(["umount", cache_tgt])
        self.runner.add_mount("tmpfs", "/var/home/danilo/.cache", fsroot="/", uuid=None)
        self.runner.calls.clear()
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("kept: /var/home/danilo/.cache has a mount CST did not make", out)
        self.assertNotIn(["umount", cache_tgt], self.runner.calls)
        self.assertIn(["umount", os.path.join(self.tmp, "var/home/danilo/Scratchpad")], self.runner.calls)
        self.assertNotIn("user=danilo", self._fstab())

    def test_detach_refuses_when_another_mount_hides_csts_bind(self) -> None:
        # Only the top mount was looked at: detach said "kept", dropped the managed line and left CST's
        # bind mounted under the tmpfs, where no status could see it.
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.add_mount("tmpfs", "/var/home/danilo/.cache", fsroot="/", uuid=None)
        _, out_s, _ = self._run(["status"])
        self.assertIn("-> /var/home/danilo/.cache [mounted (other source on top of CST's bind!)]", out_s)
        self.runner.calls.clear()
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: /var/home/danilo/.cache has a mount CST did not make on top of CST's bind of "
            "/var/mnt/cache/danilo; unmount that one first, then run detach again",
            err,
        )
        self.assertEqual(self._fstab(), attached)
        self.assertFalse(any(c[:1] in (["mount"], ["umount"]) for c in self.runner.calls))

    def test_detach_refuses_while_the_base_is_not_mounted(self) -> None:
        # With /var/mnt/cache unmounted, CST's own bind was "a mount CST did not make": detach kept it
        # mounted and still removed its managed line.
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.mounts = [m for m in self.runner.mounts if m["target"] != CACHE_BASE]
        _, out_s, _ = self._run(["status"])
        self.assertIn("-> /var/home/danilo/.cache [mounted (source not checked: base not mounted)]", out_s)
        self.runner.calls.clear()
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: /var/home/danilo/.cache has a mount and /var/mnt/cache is not mounted, so CST cannot "
            "tell whether it is its bind; mount /var/mnt/cache and run detach again",
            err,
        )
        self.assertEqual(self._fstab(), attached)
        self.assertFalse(any(c[:1] in (["mount"], ["umount"]) for c in self.runner.calls))
        rc_s, _, err_s = self._run(["detach", "--no-cache"])
        self.assertEqual(rc_s, 0, err_s)

    def test_a_failed_detach_mounts_again_what_it_unmounted(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.reload_fails = True
        self.runner.calls.clear()
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn("mounts and /etc/fstab restored.", err)
        self.assertEqual(self._fstab(), attached)
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])
        # The reload comes before any umount: a failed one leaves the binds alone.
        self.assertFalse(any(c[:1] == ["umount"] for c in self.runner.calls))
        _, out, _ = self._run(["status"])
        self.assertIn("-> /var/home/danilo/.cache [active (fstab + mounted)]", out)

    def test_detach_writes_fstab_before_it_unmounts(self) -> None:
        # The binds were unmounted first: a detach killed before its fstab write left lines that
        # mount again at the next boot for binds it had already unmounted.
        self.assertEqual(self._run(["attach"])[0], 0)
        base = "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n"
        self.runner.calls.clear()
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.runner.fstab_at_umount, [base, base])
        steps = [c[0] if c[0] != "systemctl" else c[-1] for c in self.runner.calls if c[0] in ("umount", "systemctl")]
        self.assertEqual(steps, ["daemon-reload", "umount", "umount"])
        self.assertEqual(self._mounted(), [])
        self.assertIn("Unmounted /var/home/danilo/.cache.", out)

    def test_a_busy_target_puts_the_fstab_lines_back(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.umount_fails.add("/var/home/danilo/Scratchpad")
        self.runner.calls.clear()
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/Scratchpad failed: umount: target is busy; mounts and /etc/fstab restored.",
            err,
        )
        self.assertEqual(self._fstab(), attached)
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])
        steps = [
            c[0] if c[0] != "systemctl" else c[-1]
            for c in self.runner.calls
            if c[0] in ("mount", "umount", "systemctl")
        ]
        # The lines go and systemd reloads; .cache is unmounted, Scratchpad's umount fails; .cache is
        # mounted again before the lines come back, then systemd reloads again.
        self.assertEqual(steps, ["daemon-reload", "umount", "umount", "mount", "daemon-reload"])
        self.runner.umount_fails.clear()
        rc_again, _, err_again = self._run(["detach"])
        self.assertEqual(rc_again, 0, err_again)
        self.assertEqual(self._mounted(), [])
        self.assertNotIn("user=danilo", self._fstab())

    def test_a_fstab_that_cannot_be_put_back_after_a_busy_umount_is_rollback_incomplete(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.runner.umount_fails.add("/var/home/danilo/.cache")
        with self._second_fstab_write_fails():
            rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/.cache failed: umount: target is busy; "
            "ROLLBACK INCOMPLETE (cannot put /etc/fstab back: No space left on device)",
            err,
        )
        self.assertNotIn("restored.", err)
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])

    def test_a_failed_reload_after_putting_the_lines_back_is_rollback_incomplete(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.umount_fails.add("/var/home/danilo/.cache")
        real_run = self.runner.run
        reloads: list[int] = []

        def run(argv: Sequence[str], **kwargs: object) -> CommandResult:
            if list(argv)[-1:] == ["daemon-reload"]:
                reloads.append(1)
                if len(reloads) == 2:
                    return CommandResult(1, "", "Failed to reload daemon")
            return real_run(argv, **kwargs)

        with unittest.mock.patch.object(self.runner, "run", side_effect=run):
            rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/.cache failed: umount: target is busy; "
            "ROLLBACK INCOMPLETE (systemctl daemon-reload failed: Failed to reload daemon)",
            err,
        )
        self.assertEqual(self._fstab(), attached)

    # ---- F-C2: a run killed between its /etc/fstab write and its daemon-reload ------------------
    def _reloads(self) -> int:
        return sum(1 for c in self.runner.calls if c[-1:] == ["daemon-reload"])

    def _killed_at_reload(self, argv: list[str]) -> None:
        """Run argv killed at its daemon-reload (after its /etc/fstab write): systemd is left behind."""

        def kill() -> None:
            raise Killed

        self.runner.before_reload = kill
        with self.assertRaises(Killed):
            self._run(argv)
        self.runner.before_reload = None
        self.assertFalse(self.runner.view_current(), "the test cannot bite: systemd is not behind")
        self.runner.calls.clear()

    def test_attach_with_systemd_current_reloads_nothing(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.runner.calls.clear()
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Already attached for danilo; nothing to do.", out)
        self.assertNotIn("reloaded:", out)
        self.assertEqual(self._reloads(), 0)
        self.assertIn(MOUNT_UNITS_SHOW, self.runner.calls)

    def test_attach_again_after_a_kill_at_its_reload_reloads_once(self) -> None:
        self._killed_at_reload(["attach"])
        fstab, mounted = self._fstab(), sorted(self._mounted())
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("reloaded: systemd had not loaded /etc/fstab as it is on disk (an earlier run stopped)", out)
        self.assertNotIn("nothing to do", out)
        self.assertEqual(self._reloads(), 1)
        self.assertTrue(self.runner.view_current())
        self.assertEqual((self._fstab(), sorted(self._mounted())), (fstab, mounted))
        # A run with nothing pending reloads nothing.
        self.runner.calls.clear()
        rc, out, _ = self._run(["attach"])
        self.assertIn("Already attached for danilo; nothing to do.", out)
        self.assertEqual(self._reloads(), 0)

    def test_attach_reloads_when_systemctl_cannot_say(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.runner.show_fails = True
        self.runner.calls.clear()
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("reloaded: systemctl could not say whether systemd had loaded /etc/fstab as it is on disk", out)
        self.assertEqual(self._reloads(), 1)

    def test_no_answer_from_systemctl_and_a_failing_reload_warn_and_go_on(self) -> None:
        # A rescue shell, a PID 1 that does not answer on D-Bus: systemctl show and daemon-reload both
        # fail. Nothing points to systemd being behind: a warning, exit 0, and detach still unmounts.
        warning = (
            "warning: systemctl daemon-reload failed: Failed to reload daemon; systemctl could not say whether "
            "systemd had loaded /etc/fstab as it is on disk either (run systemctl daemon-reload once it works)"
        )
        self.assertEqual(self._run(["attach"])[0], 0)
        self.runner.show_fails = True
        self.runner.reload_fails = True
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn(warning, out)
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])
        # A detach killed after it took the lines out: the binds are still mounted, with no line.
        self.runner.show_fails = self.runner.reload_fails = False
        self._killed_at_reload(["detach"])
        self.runner.show_fails = True
        self.runner.reload_fails = True
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn(warning, out)
        self.assertIn("Unmounted /var/home/danilo/.cache.", out)
        self.assertEqual(self._mounted(), [])
        # A positive answer that systemd is behind with a reload that fails stays an error
        # (test_a_failed_catch_up_reload_of_detach_unmounts_nothing).

    def test_an_answer_that_lists_no_unit_reloads_nothing(self) -> None:
        # The systemd of test_160's test root sees neither its /etc/fstab nor its mounts.
        self._killed_at_reload(["attach"])
        self.runner.units_visible = False
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Already attached for danilo; nothing to do.", out)
        self.assertEqual(self._reloads(), 0)

    def test_a_bind_whose_unit_is_not_from_fstab_while_its_line_is_there_reloads(self) -> None:
        # The base is no /etc/fstab line (no unit of /etc/fstab is loaded): only the binds' units, which
        # systemd loaded from the mount table, tell that it has not read their lines.
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write("# no base line\n")
        self._killed_at_reload(["attach"])
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("reloaded: systemd had not loaded /etc/fstab", out)
        self.assertEqual(self._reloads(), 1)
        self.assertTrue(self.runner.view_current())

    def test_a_bind_attach_mounts_again_shows_that_systemd_is_behind(self) -> None:
        # The same, with the binds gone since (unmounted by hand): attach mounts them, then their units
        # tell.
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write("# no base line\n")
        self._killed_at_reload(["attach"])
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        self.runner.calls.clear()
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Mounted /var/mnt/cache/danilo -> /var/home/danilo/.cache.", out)
        self.assertIn("reloaded: systemd had not loaded /etc/fstab", out)
        self.assertTrue(self.runner.view_current())

    def test_a_bind_attach_mounts_with_no_unit_at_all_reloads(self) -> None:
        # The lines are in /etc/fstab but systemd never loaded them, and it lists no unit of the binds
        # attach just mounted (it has not handled the new mounts yet, or, as here, it does not see them):
        # had it read those lines it would have their units from /etc/fstab. No other unit says
        # NeedDaemonReload=yes (none of /etc/fstab is listed).
        self._killed_at_reload(["attach"])
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        self.runner.units_visible = False
        _, out, _ = self._run(["plan"])
        self.assertIn(
            "danilo's lines are in /etc/fstab; systemd has not loaded /etc/fstab as it is on disk: "
            "attach runs systemctl daemon-reload after the mounts.",
            out,
        )
        self.runner.calls.clear()
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Mounted /var/mnt/cache/danilo -> /var/home/danilo/.cache.", out)
        self.assertIn("reloaded: systemd had not loaded /etc/fstab as it is on disk (an earlier run stopped)", out)
        self.assertEqual(self._reloads(), 1)
        self.assertTrue(self.runner.view_current())
        # Binds mounted before this run with no unit at all prove nothing (test_160's test root): no reload.
        self.runner.calls.clear()
        rc, out, _ = self._run(["attach"])
        self.assertIn("Already attached for danilo; nothing to do.", out)
        self.assertEqual(self._reloads(), 0)

    def test_plan_with_mounts_to_make_and_systemd_current_shows_no_reload(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        _, out, _ = self._run(["plan"])
        self.assertIn("mount --bind", out)
        self.assertNotIn("daemon-reload", out)
        self.runner.calls.clear()
        rc, out, err = self._run(["attach"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("reloaded:", out)
        self.assertEqual(self._reloads(), 0)

    def test_plan_with_mounts_to_make_and_systemd_behind_shows_the_reload(self) -> None:
        # NeedDaemonReload=yes of the units systemd loaded from /etc/fstab (the base's), or no answer.
        self._killed_at_reload(["attach"])
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        self.runner.calls.clear()
        _, out, _ = self._run(["plan"])
        self.assertIn("attach runs systemctl daemon-reload after the mounts.", out)
        self.runner.show_fails = True
        _, out, _ = self._run(["plan"])
        self.assertIn(
            "danilo's lines are in /etc/fstab; systemctl cannot say whether systemd has loaded /etc/fstab: "
            "attach runs systemctl daemon-reload after the mounts.",
            out,
        )
        self.assertEqual(self._reloads(), 0)

    def test_status_says_when_systemd_has_not_loaded_fstab(self) -> None:
        line = (
            "  systemd            has not loaded /etc/fstab as it is on disk (an edit, or an attach or detach that "
            "stopped before its daemon-reload): run that attach or detach again, or systemctl daemon-reload"
        )
        self.assertEqual(self._run(["attach"])[0], 0)
        _, out, _ = self._run(["status"])
        self.assertNotIn("  systemd  ", out)
        self._killed_at_reload(["detach"])
        _, out, _ = self._run(["status"])
        self.assertIn(line, out)
        # The base is no /etc/fstab line (no unit of /etc/fstab is loaded): only the binds' units, which
        # systemd took from the mount table while their lines are in /etc/fstab, tell.
        self.assertEqual(self._run(["detach"])[0], 0)
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write("# no base line\n")
        self.assertEqual(self._run(["attach"])[0], 0)
        _, out, _ = self._run(["status"])
        self.assertNotIn("  systemd  ", out)
        self.runner.loaded_fstab = ((0, 0), "# no base line\n")  # what systemd read before the lines came
        _, out, _ = self._run(["status"])
        self.assertIn(line, out)
        self.runner.show_fails = True
        _, out, _ = self._run(["status"])
        self.assertIn(
            "  systemd            cannot say whether it has loaded /etc/fstab (systemctl gave no answer)", out
        )
        # status only reads.
        self.runner.calls.clear()
        self._run(["status"])
        self.assertEqual(self._reloads(), 0)

    def test_status_sees_a_stale_fstab_whose_units_are_all_unmounted(self) -> None:
        # The bases are no /etc/fstab lines and danilo's binds are unmounted: every unit systemd loaded from
        # /etc/fstab (the binds') is inactive. A rewrite of /etc/fstab (the same bytes, a new file) flags
        # them all; only a question that lists inactive units sees it.
        fstab = os.path.join(self.tmp, "etc", "fstab")
        with open(fstab, "w", encoding="utf-8") as fh:
            fh.write("# no base line\n")
        self.assertEqual(self._run(["attach"])[0], 0)
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        _, out, _ = self._run(["status"])
        self.assertNotIn("  systemd  ", out)
        with open(f"{fstab}.new", "w", encoding="utf-8") as fh:
            fh.write(self._fstab())
        os.replace(f"{fstab}.new", fstab)
        self.assertFalse(self.runner.view_current(), "the test cannot bite: systemd is not behind")
        _, out, _ = self._run(["status"])
        self.assertIn("  systemd            has not loaded /etc/fstab as it is on disk", out)
        _, out, _ = self._run(["plan"])
        self.assertIn("attach runs systemctl daemon-reload after the mounts.", out)
        self.runner.run(["systemctl", "daemon-reload"])
        _, out, _ = self._run(["status"])
        self.assertNotIn("  systemd  ", out)

    def test_status_asks_systemd_only_where_the_cache_is_set_up(self) -> None:
        # A machine without the cache drive (no base mounted, no line of the cache's in /etc/fstab) whose
        # /etc/fstab was edited by hand and not reloaded: systemd's unit of the NAS line says
        # NeedDaemonReload=yes, and status says nothing of systemd, nor asks it.
        self.runner = FakeMountRunner(self.tmp)
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write("nas:/export /var/mnt/nas nfs defaults,nofail 0 0\n")
        self.runner.loaded_fstab = ((0, 0), "nas:/old /var/mnt/nas nfs defaults 0 0\n")
        for show_fails in (False, True):
            with self.subTest(show_fails=show_fails):
                self.runner.show_fails = show_fails
                self.runner.calls.clear()
                rc, out, err = self._run(["status"])
                self.assertEqual(rc, 0, err)
                self.assertIn("  /var/mnt/cache     not provisioned", out)
                self.assertNotIn("  systemd  ", out)
                self.assertNotIn(MOUNT_UNITS_SHOW, self.runner.calls)
        # The cache drive's base line alone (no user attached yet) is a cache set up: status says it.
        self.runner.show_fails = False
        with open(os.path.join(self.tmp, "etc", "fstab"), "a", encoding="utf-8") as fh:
            fh.write("/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n")
        _, out, _ = self._run(["status"])
        self.assertIn("  systemd            has not loaded /etc/fstab as it is on disk", out)

    def test_a_catch_up_reload_that_fails_changes_nothing(self) -> None:
        self._killed_at_reload(["attach"])
        fstab, mounted = self._fstab(), sorted(self._mounted())
        self.runner.reload_fails = True
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: systemctl daemon-reload failed: Failed to reload daemon; /etc/fstab is as asked, systemd has "
            "not loaded it (run systemctl daemon-reload once it works)",
            err,
        )
        self.assertEqual((self._fstab(), sorted(self._mounted())), (fstab, mounted))
        self.assertFalse(any(c[:1] in (["mount"], ["umount"]) for c in self.runner.calls))
        # With binds this run mounted: they are taken back.
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        # Only the mounts are taken back: this run wrote no /etc/fstab (it says "/etc/fstab is as asked").
        self.assertIn("(run systemctl daemon-reload once it works); the mounts this run made were taken back.", err)
        self.assertNotIn("restored", err)
        self.assertEqual((self._fstab(), self._mounted()), (fstab, []))

    def test_detach_with_systemd_current_reloads_nothing(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.assertEqual(self._run(["detach"])[0], 0)
        self.runner.calls.clear()
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Nothing attached for danilo; nothing to do.", out)
        self.assertEqual(self._reloads(), 0)

    def test_detach_again_after_a_kill_at_its_reload_reloads_before_it_unmounts(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self._killed_at_reload(["detach"])
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("reloaded: systemd had not loaded /etc/fstab as it is on disk (an earlier run stopped)", out)
        steps = [c[0] if c[0] != "systemctl" else c[-1] for c in self.runner.calls if c[0] in ("umount", "systemctl")]
        self.assertEqual(steps, ["*.mount", "daemon-reload", "umount", "umount"])
        self.assertEqual(self._mounted(), [])
        self.assertTrue(self.runner.view_current())
        self.runner.calls.clear()
        rc, out, _ = self._run(["detach"])
        self.assertIn("Nothing attached for danilo; nothing to do.", out)
        self.assertEqual(self._reloads(), 0)

    def test_detach_again_with_its_binds_unmounted_after_a_kill_reloads(self) -> None:
        # A detach killed after it took the lines out, its binds unmounted since, and the bases no
        # /etc/fstab lines: the binds' units of the old /etc/fstab are loaded, inactive and flagged, and
        # only they tell that systemd is behind.
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write("# no base line\n")
        self.assertEqual(self._run(["attach"])[0], 0)
        self._killed_at_reload(["detach"])
        for b in build_user_bindings(CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")):
            self.runner.run(["umount", self.tmp + b.target])
        self.runner.calls.clear()
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("reloaded: systemd had not loaded /etc/fstab as it is on disk (an earlier run stopped)", out)
        self.assertEqual(self._reloads(), 1)
        self.assertTrue(self.runner.view_current())
        self.runner.calls.clear()
        rc, out, _ = self._run(["detach"])
        self.assertIn("Nothing attached for danilo; nothing to do.", out)
        self.assertEqual(self._reloads(), 0)

    def test_detach_reloads_when_systemctl_cannot_say(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self._killed_at_reload(["detach"])
        self.runner.show_fails = True
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("reloaded: systemctl could not say whether systemd had loaded /etc/fstab as it is on disk", out)
        self.assertEqual(self._reloads(), 1)
        self.assertEqual(self._mounted(), [])

    def test_detach_with_an_answer_that_lists_no_unit_reloads_nothing(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self._killed_at_reload(["detach"])
        self.runner.units_visible = False
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._reloads(), 0)
        self.assertEqual(self._mounted(), [])

    def test_a_failed_catch_up_reload_of_detach_unmounts_nothing(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self._killed_at_reload(["detach"])
        fstab, mounted = self._fstab(), sorted(self._mounted())
        self.runner.reload_fails = True
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: systemctl daemon-reload failed: Failed to reload daemon; /etc/fstab is as asked", err)
        self.assertEqual((self._fstab(), sorted(self._mounted())), (fstab, mounted))
        self.assertFalse(any(c[:1] == ["umount"] for c in self.runner.calls))

    def test_plan_shows_the_reload_attach_would_run(self) -> None:
        self._killed_at_reload(["attach"])
        _, out, _ = self._run(["plan"])
        self.assertIn(
            "Already attached for danilo; systemd has not loaded /etc/fstab as it is on disk: "
            "attach runs systemctl daemon-reload.",
            out,
        )
        self.runner.show_fails = True
        _, out, _ = self._run(["plan"])
        self.assertIn(
            "Already attached for danilo; systemctl cannot say whether systemd has loaded /etc/fstab: "
            "attach runs systemctl daemon-reload.",
            out,
        )
        self.assertEqual(self._reloads(), 0)
        self.assertFalse(self.runner.view_current())
        self.runner.show_fails = False
        self.assertEqual(self._run(["attach"])[0], 0)
        _, out, _ = self._run(["plan"])
        self.assertIn("Already attached for danilo; nothing to do.", out)
        self.assertNotIn("daemon-reload", out)

    def test_a_home_through_a_link_is_named_by_its_real_path(self) -> None:
        os.symlink("var/home", os.path.join(self.tmp, "home"))
        with open(os.path.join(self.tmp, "etc", "passwd"), "a", encoding="utf-8") as fh:
            fh.write("dave:x:1004:1004:Dave:/home/dave:/bin/bash\n")
        os.makedirs(os.path.join(self.tmp, "var", "home", "dave", ".cache"))
        os.makedirs(os.path.join(self.tmp, "var", "mnt", "cache", "dave"))
        tgt = "/var/home/dave/.cache"
        self.runner.add_mount("/dev/mapper/cache", tgt, fsroot="/cache/dave", bind=True)
        with open(os.path.join(self.tmp, "etc", "fstab"), "a", encoding="utf-8") as fh:
            fh.write(
                f"/var/mnt/cache/dave {tgt} none bind,nofail,x-systemd.requires-mounts-for=/var/mnt/cache 0 0 "
                f"{CACHE_MOUNT_MARKER} user=dave kind=cache\n"
            )
        _, out, _ = self._run(["status", "--user", "dave"])
        self.assertIn("target user        dave (uid=1004, home=/var/home/dave)", out)
        self.assertIn(f"/var/mnt/cache/dave -> {tgt} [active (fstab + mounted)]", out)
        self.runner.calls.clear()
        rc, out_a, err = self._run(["attach", "--user", "dave", "--no-scratchpad"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Already attached for dave; nothing to do.", out_a)
        self.assertFalse(any(c[:1] == ["mount"] for c in self.runner.calls))

    def test_an_fstab_that_cannot_be_read_blocks_every_write(self) -> None:
        # A file there that cannot be read (a link loop: ELOOP) was taken for a missing one: attach
        # wrote an /etc/fstab of CST's lines only, and a failed reload then deleted it.
        fstab = os.path.join(self.tmp, "etc", "fstab")
        os.unlink(fstab)
        os.symlink("fstab", fstab)
        self.runner.reload_fails = True
        _, out_s, _ = self._run(["status"])
        self.assertIn("  cannot read /etc/fstab: Too many levels of symbolic links", out_s)
        _, out_p, _ = self._run(["plan"])
        self.assertIn("BLOCKED: cannot read /etc/fstab: Too many levels of symbolic links", out_p)
        for argv in (["attach"], ["detach"]):
            rc, _, err = self._run(argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("ERROR: cannot read /etc/fstab: Too many levels of symbolic links", err)
        self.assertEqual(os.readlink(fstab), "fstab")
        self.assertFalse(any(c[:1] in (["mount"], ["umount"]) for c in self.runner.calls))

    def test_findmnt_failing_blocks_every_write(self) -> None:
        self.runner.findmnt_fails = True
        before = self._fstab()
        rc_s, out_s, _ = self._run(["status"])
        self.assertEqual(rc_s, 0)
        self.assertIn("cannot list mounts: findmnt failed: findmnt: cannot read the mount table", out_s)
        _, out_p, _ = self._run(["plan"])
        self.assertIn("BLOCKED: cannot list mounts", out_p)
        for argv in (["attach"], ["detach"]):
            rc, _, err = self._run(argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("ERROR: cannot list mounts", err)
        self.assertEqual(self._fstab(), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "var", "mnt", "cache", "danilo")))
        self.assertFalse(any(c[:1] in (["mount"], ["umount"]) for c in self.runner.calls))

    def test_a_findmnt_failure_is_worded_as_a_mount_failure(self) -> None:
        # One wording for every failed command (CommandResult.failure): its stderr, or the exit status;
        # findmnt's own copy also took its stdout.
        real_run = self.runner.run

        def run(argv: Sequence[str], **kwargs: object) -> CommandResult:
            if list(argv)[:1] == ["findmnt"]:
                return CommandResult(2, '{"filesystems": [', "")
            return real_run(argv, **kwargs)

        with unittest.mock.patch.object(self.runner, "run", side_effect=run):
            _, out, _ = self._run(["status"])
        self.assertIn("  cannot list mounts: findmnt failed: exit 2", out)

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
        # main() puts SIGPIPE and SIGXFSZ back at their default (undo_python_start): in-process, the rest of
        # this worker's tests would inherit that (a closed-stdout test then died of SIGPIPE).
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.assertEqual(cache_mount_main(["--help"]), 0)
