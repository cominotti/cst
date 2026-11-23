# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs cache` domain, adapters, and CLI."""

from __future__ import annotations

from collections.abc import Sequence
import io
import json
import os
from pathlib import Path
import posixpath
import shutil
import stat
import tempfile
from typing import override
import unittest
import unittest.mock

from btrfs.cache import adapters as cache_adapters
from btrfs.cache.cli import main as cache_mount_main, run_cache_mount_cli
from btrfs.cache.domain import (
    CACHE_BASE,
    FINDMNT_COLUMNS,
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
    unescape_mount_field,
)
from btrfs.cache.domain.health import (
    CacheMountFindingSeverity,
    assess_cache_mount_health,
    dm_device_names,
    parse_crypttab_cache_issues,
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
    """findmnt, mount and umount over a list of mounts. findmnt answers `-J -l -o FINDMNT_COLUMNS` as the
    real one does: a bind mount's source is "DEVICE[FSROOT]", its fsroot the folder it shows."""

    def __init__(self, root: str, *, same_uuid: bool = False) -> None:
        self.root = root
        self.same_uuid = same_uuid
        self.findmnt_fails = False
        self.reload_fails = False
        self.mount_fails: set[str] = set()
        self.fstab_at_mount: list[str | None] = []
        self.calls: list[list[str]] = []
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

    def _holding(self, path: str) -> dict[str, str | None]:
        best = self.mounts[0]
        for m in self.mounts:
            tgt = str(m["target"]).rstrip("/") or "/"
            if (tgt in (path, "/") or path.startswith(f"{tgt}/")) and len(tgt) >= len(str(best["target"]).rstrip("/")):
                best = m
        return best

    def _findmnt_json(self) -> str:
        fss = []
        for m in self.mounts:
            uuid = UUID_ROOT if self.same_uuid and m["uuid"] is not None else m["uuid"]
            fss.append({**m, "uuid": uuid})
        return json.dumps({"filesystems": fss}, indent=3)

    def _bind(self, src_path: str, tgt_path: str) -> CommandResult:
        fstab = os.path.join(self.root, "etc", "fstab")
        self.fstab_at_mount.append(
            Path(fstab).read_text(encoding="utf-8", errors="surrogateescape") if os.path.exists(fstab) else None
        )
        if tgt_path.removeprefix(self.root) in self.mount_fails:
            return CommandResult(32, "", "mount: permission denied")
        src = src_path.removeprefix(self.root)
        held = self._holding(src)
        rel = src.removeprefix(str(held["target"]).rstrip("/"))
        dev = str(held["source"]).split("[", 1)[0]
        fsroot = posixpath.normpath(f"{held['fsroot']}/{rel}")
        self.add_mount(dev, tgt_path.removeprefix(self.root), fsroot=fsroot, uuid=held["uuid"], bind=True)
        return CommandResult(0, "", "")

    def _unbind(self, tgt_path: str) -> None:
        logical_tgt = tgt_path.removeprefix(self.root)
        for i in range(len(self.mounts) - 1, -1, -1):
            if self.mounts[i]["target"] == logical_tgt:
                del self.mounts[i]
                break

    def run(self, argv: Sequence[str], **_kwargs: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd == ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS]:
            if self.findmnt_fails:
                return CommandResult(1, "", "findmnt: cannot read the mount table")
            return CommandResult(0, self._findmnt_json(), "")
        if cmd[-1:] == ["daemon-reload"] and self.reload_fails:
            return CommandResult(1, "", "Failed to reload daemon")
        if cmd[:2] == ["mount", "--bind"]:
            return self._bind(cmd[2], cmd[3])
        if cmd[:1] == ["umount"]:
            self._unbind(cmd[1])
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
        self.assertEqual(unescape_mount_field("a\\134b\\011c\\012d\\040e"), "a\\b\tc\nd e")

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
        self.assertIn(f"-> {tgt} [mounted (other source!)]", out)

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
            "mount: permission denied; previous state restored.",
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
        self.assertIn("ERROR: systemctl daemon-reload failed: Failed to reload daemon; previous state restored.", err)
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "etc", "fstab")))
        self.assertEqual(self._mounted(), [])

    def test_a_failed_reload_puts_the_old_fstab_back(self) -> None:
        self.runner.reload_fails = True
        before = self._fstab()
        rc, _, err = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn("previous state restored.", err)
        self.assertEqual(self._fstab(), before)
        self.assertEqual(self._mounted(), [])

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
        self.runner.add_mount("tmpfs", "/var/home/danilo/.cache", fsroot="/", uuid=None)
        self.runner.calls.clear()
        rc, out, err = self._run(["detach"])
        self.assertEqual(rc, 0, err)
        self.assertIn("kept: /var/home/danilo/.cache has a mount CST did not make", out)
        self.assertNotIn(["umount", os.path.join(self.tmp, "var/home/danilo/.cache")], self.runner.calls)
        self.assertIn(["umount", os.path.join(self.tmp, "var/home/danilo/Scratchpad")], self.runner.calls)
        self.assertNotIn("user=danilo", self._fstab())

    def test_a_failed_detach_mounts_again_what_it_unmounted(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self._fstab()
        self.runner.reload_fails = True
        rc, _, err = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn("previous state restored.", err)
        self.assertEqual(self._fstab(), attached)
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])
        _, out, _ = self._run(["status"])
        self.assertIn("-> /var/home/danilo/.cache [active (fstab + mounted)]", out)

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
