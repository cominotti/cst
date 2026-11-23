# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests for btrfs/btrfs-snapshot: convert, convert-boot, convert-one,
planned-left and busy (no root, no btrfs needed).

The whole bash program runs on the fake machine of snapshot_fakes.py. A folder
becomes a "subvolume" when the fake btrfs creates it (stat then answers 256
for its inode, also after a rename); the clone is the real cp without
--reflink. The open files, working folders and mapped files of processes, and
the mount tables of other mount namespaces, are links and files below the
root's /proc.

Not reachable from a test folder: a nested subvolume inside a planned folder
(find -inum 256 reads the real inode numbers), a late write between the switch
and the last check (no hook between two mv calls), and the entry count of the
clone differing (cp copies all).

Run with: invowk cmd test module btrfs.tests.test_snapshot_convert
"""

import fcntl
import os
import re
import stat
import subprocess
import time
from typing import override
import unittest

from btrfs.tests.snapshot_fakes import SCRIPT, RuntimeTest, log, read

PLAN = "etc/btrfs-setup/convert-plan"
BOOT_FLAG = "etc/btrfs-setup/convert-at-boot"
FIRST = "Converting planned directories (busy ones wait for the next boot)..."
BOOT_FIRST = "btrfs-snapshot: converting the directories the desktop session kept busy..."
# Names findmnt -r writes escaped (\xHH): a blank, an accented letter, a backslash, a tab.
ESCAPED_NAMES = ("Work space", "Música", "back\\slash", "Work\tspace")


def findmnt_raw(path: str) -> str:
    """PATH as `findmnt -rn -o TARGET` prints it: every byte that is not printable ASCII, a
    blank or a backslash as \\xHH (tier 1 test_116 checks the real findmnt)."""
    return "".join(chr(b) if 0x21 <= b < 0x7F and b != 0x5C else f"\\x{b:02x}" for b in os.fsencode(path))


class Base(RuntimeTest):
    def is_subvol(self, path: str) -> bool:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        found = self.described().get("subvols")
        return isinstance(found, list) and f"{st.st_dev}:{st.st_ino}" in found

    def tree(self, rel: str) -> str:
        """A plain folder below the home with a file and a sub-folder."""
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.join(p, "sub"))
        self.write(os.path.join(p, "file"), "data\n")
        self.write(os.path.join(p, "sub", "inner"), "inner\n")
        os.chmod(p, 0o750)
        return p

    def plan(self, *lines: str) -> None:
        self.write(PLAN, "".join(ln + "\n" for ln in lines))

    def leftovers(self) -> list[str]:
        """Names in the home that a conversion leaves next to a folder."""
        return sorted(n for n in os.listdir(self.home) if ".subvol-new" in n or ".pre-subvol-" in n or "aside" in n)

    def assert_converted(self, path: str) -> None:
        self.assertTrue(self.is_subvol(path), path)
        self.assertEqual(read(os.path.join(path, "file")), "data\n")
        self.assertEqual(read(os.path.join(path, "sub", "inner")), "inner\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o750)


class PlannedLeftTest(Base):
    def test_no_plan(self) -> None:
        r = self.run_prog("planned-left")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_what_is_left(self) -> None:
        plain = self.tree("plain")
        sub = self.tree("sub")
        self.mark_subvol(sub)
        skipped = self.tree("skipped")
        self.write(self.state_file("skipped"), f"{skipped}\tis or contains a mountpoint\n")
        under_plain = os.path.join(plain, "cache")
        os.makedirs(under_plain)
        under_sub = os.path.join(sub, "cache")
        os.makedirs(under_sub)
        self.plan(
            "# copy /commented",
            "",
            f"copy {plain}",
            f"copy {sub}",
            f"copy {skipped}",
            f"copy {self.home}/absent",
            f"copy {self.home}/no-parent/absent",
            f"aside {under_plain}",
            f"aside {under_sub}",
            f"aside {self.home}/no-aside",
        )
        r = self.run_prog("planned-left")
        self.assertEqual(
            (r.returncode, self.lines(r.stdout), r.stderr), (0, [plain, f"{self.home}/absent", under_plain], "")
        )
        self.assertEqual(self.calls("btrfs"), [])


class ConvertTest(Base):
    def test_a_plain_folder_becomes_a_subvolume_and_the_old_tree_goes(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.write(BOOT_FLAG, "")
        r = self.run_prog("convert")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            self.lines(r.stdout),
            [
                FIRST,
                log(5, f"convert: {ws}: now a subvolume, old tree removed"),
                log(5, "planned dirs: 1 done, 0 left for later (in use or failed)"),
            ],
        )
        self.assertEqual(r.stderr, "")
        self.assert_converted(ws)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(read(self.state_file("drop-pending")), "")
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))
        new = f"{ws}.subvol-new"
        self.assertEqual(
            [c for c in self.calls() if c[0] in ("btrfs", "cp", "chcon")],
            [
                ["btrfs", "subvolume", "create", new],
                ["chcon", f"--reference={ws}", new],
                ["cp", "-a", "--reflink=always", f"{ws}/.", f"{new}/"],
            ],
        )

    def test_a_busy_folder_waits_for_the_next_boot(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.busy(os.path.join(ws, "sub", "inner"))
        r = self.run_prog("convert")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            self.lines(r.stdout),
            [
                FIRST,
                log(5, f"convert: {ws}: in use, left for later"),
                log(5, "planned dirs: 0 done, 1 left for later (in use or failed)"),
            ],
        )
        self.assertFalse(self.is_subvol(ws))
        self.assertTrue(os.path.exists(self.path(BOOT_FLAG)))
        self.assertEqual(self.calls("btrfs"), [])

    def test_every_kind_of_use_counts(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        for link in ("cwd", "root", "exe", "fd/7"):
            with self.subTest(link):
                self.busy(f"{ws}/sub (deleted)" if link == "exe" else f"{ws}/sub", pid=200, link=link)
                self.run_prog("convert")
                self.assertFalse(self.is_subvol(ws))
                os.remove(self.path(f"proc/200/{link}"))
        # A folder whose name only starts like the planned one is not it.
        self.busy(f"{ws}-other/x")
        self.run_prog("convert")
        self.assertTrue(self.is_subvol(ws))

    def test_a_bind_mount_of_the_tree_in_another_mount_namespace_counts(self) -> None:
        ws = self.tree("Work space")
        self.plan(f"copy {ws}")
        self.mountinfo(f"30 1 0:42 /home {self.home_mount} rw,relatime - btrfs /dev/dm-0 rw\n")
        self.write("proc/300/mountinfo", "50 40 0:42 /home/u/Work\\040space/sub /src rw - btrfs /dev/dm-0 rw\n")
        os.makedirs(self.path("proc/300/ns"))
        os.symlink("mnt:[4026532001]", self.path("proc/300/ns/mnt"))
        r = self.run_prog("convert")
        self.assertIn(log(5, f"convert: {ws}: in use, left for later"), self.lines(r.stdout))
        self.assertFalse(self.is_subvol(ws))

    def test_a_cache_is_put_aside_before_its_parent_is_cloned(self) -> None:
        cache = self.tree(".cache")
        uv = os.path.join(cache, "uv")
        os.makedirs(os.path.join(uv, "wheels"))
        self.plan(f"aside {uv}", f"copy {cache}")
        r = self.run_prog("convert")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            self.lines(r.stdout),
            [
                FIRST,
                log(5, f"aside: {uv}: moved out of the way, deleted in the background"),
                log(5, f"convert: {cache}: now a subvolume, old tree removed"),
                log(5, "planned dirs: 1 done, 0 left for later (in use or failed)"),
            ],
        )
        self.assert_converted(cache)
        self.assertFalse(os.path.exists(uv))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(read(self.state_file("drop-pending")), "")

    def test_put_aside_leaves_cache_alone_when_parent_is_an_external_mountpoint(self) -> None:
        cache = self.tree(".cache")
        uv = os.path.join(cache, "uv")
        os.makedirs(os.path.join(uv, "wheels"))
        self.machine(mountpoints=[cache])
        self.plan(f"aside {uv}", f"copy {cache}")
        r = self.run_prog("convert")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(uv, "wheels")))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))
        left = self.run_prog("planned-left")
        self.assertEqual((left.returncode, left.stdout), (0, ""))

    def test_a_symlink_to_a_mountpoint_is_planned_until_it_is_skipped(self) -> None:
        real = self.tree("real-cache")
        link = os.path.join(self.home, ".cache")
        os.symlink(real, link)
        # The fake mountpoint answers for the path it is given, as the real one does through a link.
        self.machine(mountpoints=[link])
        self.plan(f"copy {link}")
        left = self.run_prog("planned-left")
        self.assertEqual((left.returncode, left.stdout), (0, f"{link}\n"))
        self.assertEqual(self.run_prog("convert").returncode, 0)
        self.assertEqual(read(self.state_file("skipped")), f"{link}\tnot a directory (a symlink or file)\n")
        left = self.run_prog("planned-left")
        self.assertEqual((left.returncode, left.stdout), (0, ""))

    def test_an_aside_link_is_left_alone_and_never_left(self) -> None:
        # put_aside skips a link, but planned_left listed it (test -d follows it): the boot flag stayed
        # and convert-boot gave up after its retries, for a cache already somewhere else.
        bulk_uv = self.tree("Bulk-uv")
        cache = os.path.join(self.home, ".cache")
        os.makedirs(cache)
        link = os.path.join(cache, "uv")
        os.symlink(bulk_uv, link)
        self.plan(f"aside {link}")
        self.write(BOOT_FLAG, "")
        left = self.run_prog("planned-left")
        self.assertEqual((left.returncode, left.stdout), (0, ""))
        self.assertEqual(self.run_prog("convert").returncode, 0)
        self.assertEqual(os.readlink(link), bulk_uv)
        self.assertEqual(read(os.path.join(bulk_uv, "file")), "data\n")
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))

    def test_a_busy_cache_stays_and_its_parent_is_still_cloned(self) -> None:
        cache = self.tree(".cache")
        uv = os.path.join(cache, "uv")
        os.makedirs(uv)
        self.busy(uv)
        self.plan(f"aside {uv}", f"copy {cache}")
        r = self.run_prog("convert")
        self.assertEqual(
            self.lines(r.stdout)[1:],
            [
                log(5, f"aside: {uv}: in use, left for later"),
                log(5, f"convert: {cache}: in use, left for later"),
                log(5, "planned dirs: 0 done, 2 left for later (in use or failed)"),
            ],
        )
        self.assertTrue(os.path.exists(self.path(BOOT_FLAG)))

    def test_an_absent_folder_becomes_an_empty_subvolume(self) -> None:
        target = os.path.join(self.home, ".local", "share", "new")
        os.makedirs(os.path.dirname(target))
        os.chmod(os.path.dirname(target), 0o700)
        self.plan(f"copy {target}", f"copy {self.home}/no-parent/x")
        r = self.run_prog("convert")
        self.assertEqual(
            self.lines(r.stdout),
            [
                FIRST,
                log(5, f"convert: {target}: created as an empty subvolume"),
                log(5, "planned dirs: 1 done, 0 left for later (in use or failed)"),
            ],
        )
        self.assertTrue(self.is_subvol(target))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
        self.assertIn(["restorecon", target], self.calls())
        self.assertFalse(os.path.exists(f"{self.home}/no-parent"))
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))

    def test_what_cannot_be_converted_is_skipped_and_remembered(self) -> None:
        real = self.tree("real")
        link = os.path.join(self.home, "link")
        os.symlink(real, link)
        mounted = self.tree("mounted")
        holds = self.tree("holds")
        self.machine(mountpoints=[mounted], mounts=["/", f"{holds}/sub"])
        self.plan(f"copy {link}", f"copy {mounted}", f"copy {holds}")
        r = self.run_prog("convert")
        self.assertEqual(
            self.lines(r.stdout),
            [
                FIRST,
                log(4, f"convert: {link}: not a directory (a symlink or file), skipped"),
                log(4, f"convert: {mounted}: is or contains a mountpoint, skipped"),
                log(4, f"convert: {holds}: is or contains a mountpoint, skipped"),
            ],
        )
        self.assertEqual(
            read(self.state_file("skipped")),
            f"{link}\tnot a directory (a symlink or file)\n{mounted}\tis or contains a mountpoint\n"
            f"{holds}\tis or contains a mountpoint\n",
        )
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))
        self.assertEqual(self.mode(self.state_file("skipped")), 0o600, "rewritten through mktemp at each skip")
        # The cause went away: the next run converts it and forgets the skip.
        self.machine(mountpoints=[], mounts=["/"])
        self.run_prog("convert")
        self.assertTrue(self.is_subvol(mounted))
        self.assertEqual(read(self.state_file("skipped")), f"{link}\tnot a directory (a symlink or file)\n")

    def test_an_interrupted_conversion_is_finished(self) -> None:
        ws = os.path.join(self.home, "Workspace")
        new = self.tree("Workspace.subvol-new")
        self.mark_subvol(new)
        self.plan(f"copy {ws}")
        r = self.run_prog("convert")
        self.assertEqual(
            self.lines(r.stdout),
            [
                FIRST,
                log(4, f"convert: {ws}: finished an interrupted conversion"),
                log(5, "planned dirs: 1 done, 0 left for later (in use or failed)"),
            ],
        )
        self.assert_converted(ws)
        self.assertEqual(self.calls("btrfs"), [])

    def test_a_failed_clone_leaves_the_folder_unchanged(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.machine(cp_err="cp: failed to clone 'x' from 'y': Operation not supported\n")
        r = self.run_prog("convert")
        self.assertEqual(
            self.lines(r.stdout)[1:],
            [
                log(3, f"clone {ws}: cp: failed to clone 'x' from 'y': Operation not supported"),
                log(3, f"convert: {ws}: clone failed; left unchanged"),
                log(5, "planned dirs: 0 done, 1 left for later (in use or failed)"),
            ],
        )
        self.assertFalse(self.is_subvol(ws))
        self.assertEqual(self.leftovers(), [])
        self.assertIn(["btrfs", "subvolume", "delete", f"{ws}.subvol-new"], self.calls())
        self.assertTrue(os.path.exists(self.path(BOOT_FLAG)))

    def test_a_warning_of_cp_is_a_failed_clone(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.machine(cp_err="cp: cannot preserve security context\n", cp_rc=0)
        r = self.run_prog("convert")
        self.assertIn(log(3, f"convert: {ws}: clone failed; left unchanged"), self.lines(r.stdout))
        self.assertFalse(self.is_subvol(ws))

    def test_a_write_during_the_clone_discards_it(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.machine(cp_write=os.path.join(ws, "file"))
        time.sleep(0.01)
        r = self.run_prog("convert")
        self.assertIn(
            log(4, f"convert: {ws}: written to during the clone, skipped (retried later)"), self.lines(r.stdout)
        )
        self.assertFalse(self.is_subvol(ws))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(read(os.path.join(ws, "file")), "data\nlate")

    def test_an_entry_created_during_the_clone_discards_it(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.machine(cp_write=os.path.join(ws, "new"))
        r = self.run_prog("convert")
        self.assertIn(log(3, f"convert: {ws}: entry count differs (5 vs 4); left unchanged"), self.lines(r.stdout))
        self.assertFalse(self.is_subvol(ws))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(read(os.path.join(ws, "new")), "late")

    def test_a_process_that_starts_using_it_during_the_clone_discards_it(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.machine(cp_open=[self.path("proc/400/cwd"), f"{ws}/sub"])
        r = self.run_prog("convert")
        self.assertIn(log(4, f"convert: {ws}: started being used, skipped"), self.lines(r.stdout))
        self.assertFalse(self.is_subvol(ws))
        self.assertEqual(self.leftovers(), [])

    def test_a_subvolume_cannot_be_created(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.machine(create_rc=1)
        r = self.run_prog("convert")
        self.assertIn(log(3, f"convert: {ws}: cannot create subvolume"), self.lines(r.stdout))
        self.assertEqual(self.leftovers(), [])

    def test_old_trees_left_by_an_earlier_run_are_deleted(self) -> None:
        # An old tree is deleted; one with a mount below it stays listed; an entry that is no old tree
        # (not named *.pre-subvol-*, a file, a link) is forgotten with a line and never deleted, so it
        # no longer keeps new snapshots waiting for ever (before, it stayed listed); a gone one goes.
        old = self.tree("x.pre-subvol-20261101T000000")
        mounted = self.tree("y.pre-subvol-20261101T000000")
        other = self.tree("not-an-old-tree")
        named = self.write(os.path.join(self.home, "f.pre-subvol-20261101T000000"), "a file\n")
        link = os.path.join(self.home, "l.pre-subvol-20261101T000000")
        os.symlink(other, link)
        self.machine(mounts=[f"{mounted}/sub"])
        entries = [old, mounted, other, named, link, f"{self.home}/gone", ""]
        self.write(self.state_file("drop-pending"), "\n".join(entries) + "\n")
        r = self.run_prog("convert")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            [ln for ln in self.lines(r.stdout) if "drop-pending" in ln],
            [
                log(4, f"convert: drop-pending: {p} is not an old tree (*.pre-subvol-*); forgotten, not deleted")
                for p in (other, named, link)
            ],
        )
        self.assertFalse(os.path.exists(old))
        self.assertEqual(read(self.state_file("drop-pending")), f"{mounted}\n")
        self.assertTrue(os.path.isdir(other))
        self.assertEqual(read(named), "a file\n")
        self.assertTrue(os.path.islink(link))

    def test_an_old_tree_with_a_mount_below_an_escaped_name_is_kept(self) -> None:
        # findmnt -r writes the mount point escaped (\xHH). Before, has_mount_below compared that
        # text with the path, never matched, and rm --one-file-system emptied the old tree, which
        # does not stop at a bind mount of the same file system.
        for name in ESCAPED_NAMES:
            with self.subTest(name=name):
                old = self.tree(f"{name}.pre-subvol-20261101T000000")
                self.machine(mounts=["/", findmnt_raw(f"{old}/sub")])
                self.write(self.state_file("drop-pending"), f"{old}\n")
                r = self.run_prog("convert")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(read(os.path.join(old, "sub", "inner")), "inner\n")
                self.assertEqual(read(self.state_file("drop-pending")), f"{old}\n")


class ConvertBootTest(Base):
    def test_without_the_flag_nothing_happens(self) -> None:
        self.plan(f"copy {self.tree('Workspace')}")
        r = self.run_prog("convert-boot")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.calls_but_hostname(), [])

    def test_converts_and_leaves_the_old_tree_to_the_hourly_run(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.write(BOOT_FLAG, "")
        r = self.run_prog("convert-boot")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            self.lines(r.stdout),
            [
                BOOT_FIRST,
                log(5, f"convert: {ws}: now a subvolume; the old tree is deleted by the hourly run"),
                log(5, "planned dirs: 1 done, 0 left for later (in use or failed)"),
            ],
        )
        self.assert_converted(ws)
        self.assertEqual(self.mode(self.state_file("drop-pending")), 0o644)
        (old,) = read(self.state_file("drop-pending")).splitlines()
        self.assertRegex(old, "^" + re.escape(ws) + r"\.pre-subvol-\d{8}T\d{6}$")
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))
        self.assertFalse(os.path.exists(self.state_file("boot-attempts")))

    def test_counts_its_attempts_and_gives_up_after_two_boots(self) -> None:
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.write(BOOT_FLAG, "")
        self.busy(ws)
        for attempt in ("1", "2"):
            r = self.run_prog("convert-boot")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(self.read_state("boot-attempts"), attempt + "\n")
            self.assertTrue(os.path.exists(self.path(BOOT_FLAG)))
        self.forget_calls()
        r = self.run_prog("convert-boot")
        title, body = (
            "Subvolume conversion at boot gave up",
            f"Left: {ws}  - see 'journalctl -u btrfs-subvol-convert'.",
        )
        self.assertEqual((r.returncode, self.lines(r.stdout)), (0, [log(4, f"{title}: {body}")]))
        self.assertEqual(self.notified(), [["-u", "critical", "-a", "btrfs-snapshot", title, body]])
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))
        self.assertFalse(os.path.exists(self.state_file("boot-attempts")))
        # planned_left() checks mountpoint -q; no findmnt/btrfs proves convert_one() did not run.
        self.assertEqual(self.calls("findmnt"), [])
        self.assertEqual(self.calls("btrfs"), [])

    def test_an_empty_attempts_file_counts_as_none(self) -> None:
        self.plan(f"copy {self.tree('Workspace')}")
        self.write(BOOT_FLAG, "")
        self.write(self.state_file("boot-attempts"), "")
        self.busy(os.path.join(self.home, "Workspace"))
        self.assertEqual(self.run_prog("convert-boot").returncode, 0)
        self.assertEqual(self.read_state("boot-attempts"), "1\n")

    def test_an_attempts_file_that_is_not_a_number_counts_as_none(self) -> None:
        # Counted as 0 with a journal line and rewritten, so the counting and the give-up after
        # two boots work again. Before, bash read the word as a variable name (unbound under
        # set -u): the unit failed at every boot, never counted and never gave up.
        ws = self.tree("Workspace")
        self.plan(f"copy {ws}")
        self.write(BOOT_FLAG, "")
        self.busy(ws)
        for text, shown in (("x\n", "x"), ("1 2\n", "1 2"), ("-1\n", "-1"), ("1e9\n", "1e9")):
            with self.subTest(text):
                self.write(self.state_file("boot-attempts"), text)
                r = self.run_prog("convert-boot")
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                self.assertEqual(
                    self.lines(r.stdout)[:2],
                    [
                        log(4, f"convert-boot: {self.state}/boot-attempts holds '{shown}', not a number; counted as 0"),
                        BOOT_FIRST,
                    ],
                )
                self.assertEqual(self.read_state("boot-attempts"), "1\n")
        # The count goes on from there: one more boot, then the give-up.
        self.assertEqual(self.run_prog("convert-boot").returncode, 0)
        self.assertEqual(self.read_state("boot-attempts"), "2\n")
        r = self.run_prog("convert-boot")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith(log(4, "Subvolume conversion at boot gave up: ")), r.stdout)
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))

    def test_a_count_with_leading_zeros_is_decimal(self) -> None:
        self.plan(f"copy {self.tree('Workspace')}")
        self.write(BOOT_FLAG, "")
        self.write(self.state_file("boot-attempts"), "08\n")
        r = self.run_prog("convert-boot")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        # 8 attempts: the give-up, without bash's "value too great for base".
        self.assertTrue(r.stdout.startswith(log(4, "Subvolume conversion at boot gave up: ")), r.stdout)
        self.assertFalse(os.path.exists(self.path(BOOT_FLAG)))


class EngineStepTest(Base):
    """convert-planned --lock-held FD: the first step of hourly (the planned conversions, the old
    trees) for the Python runtime, which holds the lock and hands it on as fd FD. Refused unless FD
    is that lock and it is held; no hostname call and no settings warnings (the caller gave them)."""

    def step(self, fd: int | None, *, locked: bool = True, other: bool = False) -> subprocess.CompletedProcess[str]:
        path = os.path.join(self.tmp, "other-lock") if other else self.path("run/btrfs-snapshot.lock")
        with open(path, "a", encoding="utf-8") as f:
            if locked:
                fcntl.flock(f, fcntl.LOCK_EX)
            n = f.fileno() if fd is None else fd
            return subprocess.run(
                ["bash", SCRIPT, "convert-planned", "--lock-held", str(n)],
                check=False,
                capture_output=True,
                text=True,
                env=self.env,
                cwd=self.tmp,
                pass_fds=(f.fileno(),),
                stdin=subprocess.DEVNULL,
                timeout=120,
            )

    @override
    def setUp(self) -> None:
        super().setUp()
        self.ws = self.tree("Workspace")
        self.plan(f"copy {self.ws}")
        old = self.tree("x.pre-subvol-20261101T000000")
        self.write(self.state_file("drop-pending"), old + "\n")
        self.conf("touch ./ran\n")

    def test_runs_with_the_lock_handed_on(self) -> None:
        r = self.step(None)
        self.assertEqual(
            (r.returncode, self.lines(r.stdout), r.stderr),
            (
                0,
                [
                    log(5, f"convert: {self.ws}: now a subvolume, old tree removed"),
                    log(5, "planned dirs: 1 done, 0 left for later (in use or failed)"),
                ],
                "",
            ),
        )
        self.assert_converted(self.ws)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.calls("hostname"), [])

    def test_refused_when_nobody_holds_the_lock(self) -> None:
        lock = self.path("run/btrfs-snapshot.lock")
        for name, run in (
            ("open, not locked", lambda: self.step(None, locked=False)),
            ("another file, locked", lambda: self.step(None, other=True)),
            ("an fd that is not open", lambda: self.step(63)),
        ):
            with self.subTest(name):
                r = run()
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertTrue(r.stdout.endswith(" is not a held " + lock + "; nothing converted\n"), r.stdout)
                self.assertFalse(self.is_subvol(self.ws))
                self.assertEqual(self.calls("btrfs"), [])
                self.assertTrue(os.path.exists(self.state_file("drop-pending")))

    def test_refused_for_an_fd_that_does_not_hold_the_lock_another_run_holds(self) -> None:
        # Another run (btrfs-bulk, convert-one) holds the lock; the caller hands on its own open of
        # the file, read-only (the file is 0644: any user can open it), which holds no lock. Before,
        # the check only asked whether someone held it, and the engine ran beside the real holder.
        lock = self.path("run/btrfs-snapshot.lock")
        with self.hold_lock(), open(lock, encoding="utf-8") as mine:
            r = subprocess.run(
                ["bash", SCRIPT, "convert-planned", "--lock-held", str(mine.fileno())],
                check=False,
                capture_output=True,
                text=True,
                env=self.env,
                cwd=self.tmp,
                pass_fds=(mine.fileno(),),
                stdin=subprocess.DEVNULL,
                timeout=120,
            )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(r.stdout.endswith(" is not a held " + lock + "; nothing converted\n"), r.stdout)
        self.assertFalse(self.is_subvol(self.ws))
        self.assertEqual(self.calls("btrfs"), [])
        self.assertTrue(os.path.exists(self.state_file("drop-pending")))

    def test_usage(self) -> None:
        for args in ((), ("--lock-held",), ("9",), ("--lock-held", "9", "x")):
            with self.subTest(args):
                r = self.run_prog("convert-planned", *args)
                self.assertEqual(
                    (r.returncode, r.stdout, r.stderr),
                    (2, "", "usage: btrfs-snapshot convert-planned --lock-held FD\n"),
                )


class ConvertOneTest(Base):
    def test_one_folder_outside_the_plan(self) -> None:
        bulk = self.tree("Bulk")
        r = self.run_prog("convert-one", bulk)
        self.assertEqual(
            (r.returncode, self.lines(r.stdout), r.stderr),
            (0, [log(5, f"convert: {bulk}: now a subvolume, old tree removed")], ""),
        )
        self.assert_converted(bulk)
        self.assertEqual(self.leftovers(), [])
        # Already one: nothing to do, still success.
        r = self.run_prog("convert-one", bulk)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_busy_or_impossible_is_a_failure(self) -> None:
        bulk = self.tree("Bulk")
        self.busy(bulk)
        r = self.run_prog("convert-one", bulk)
        self.assertEqual(
            (r.returncode, self.lines(r.stdout)), (1, [log(5, f"convert: {bulk}: in use, left for later")])
        )
        r = self.run_prog("convert-one", f"{self.home}/no-parent/Bulk")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        link = os.path.join(self.home, "link")
        os.symlink(bulk, link)
        r = self.run_prog("convert-one", link)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            self.lines(r.stdout), [log(4, f"convert: {link}: not a directory (a symlink or file), skipped")]
        )

    def test_a_mount_below_an_escaped_name_is_found(self) -> None:
        # findmnt -r writes the mount point escaped (\xHH). Before, has_mount_below compared that
        # text with the path and never matched: the folder was converted with a mount below it.
        for name in ESCAPED_NAMES:
            with self.subTest(name=name):
                p = self.tree(name)
                self.machine(mounts=["/", findmnt_raw(f"{p}/sub")])
                r = self.run_prog("convert-one", p)
                self.assertEqual(
                    (r.returncode, self.lines(r.stdout)),
                    (1, [log(4, f"convert: {p}: is or contains a mountpoint, skipped")]),
                )
                self.assertFalse(self.is_subvol(p))
                self.assertEqual(self.calls("btrfs"), [])

    def test_a_name_with_a_line_break_is_in_use(self) -> None:
        # A path with a line break is split across two lines of the open paths (a /proc link, a
        # bind mount's source), so its use cannot be seen there: it counts as in use, even with
        # nothing using it, as here. Before, it was converted, also while a container's bind mount
        # of it was in use (BusyTest.test_a_bind_mount_of_a_name_with_a_line_break_is_busy).
        p = self.tree("a\nb")
        r = self.run_prog("convert-one", p)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, log(5, f"convert: {p}: in use, left for later") + "\n")
        self.assertFalse(self.is_subvol(p))

    def test_without_a_path(self) -> None:
        # A usage error (exit 2), told before the lock is taken: it does not wait for another run.
        # Before, bash's own "${2:?}" message and exit 1, after the lock.
        with self.hold_lock():
            for args in ((), ("",)):
                with self.subTest(args):
                    p = self.start_prog("convert-one", *args)
                    out, err = p.communicate(timeout=30)  # the old code waited here for the lock
                    self.assertEqual((p.returncode, out, err), (2, "", "usage: btrfs-snapshot convert-one PATH\n"))

    def test_waits_for_the_lock(self) -> None:
        bulk = self.tree("Bulk")
        with self.hold_lock():
            p = self.start_prog("convert-one", bulk)
            time.sleep(1)
            self.assertIsNone(p.poll())
            self.assertEqual(self.calls("btrfs"), [])
        _, err = p.communicate(timeout=60)
        self.assertEqual((p.returncode, err), (0, ""))
        self.assert_converted(bulk)


class BusyTest(Base):
    def test_the_paths_some_process_uses(self) -> None:
        a, b, c, d, e = (os.path.join(self.home, n) for n in ("a", "b", "c", "d", "e"))
        self.busy(f"{a}/x", pid=10)
        self.busy(b, pid=11, link="cwd")
        self.busy(f"{c}/bin/tool (deleted)", pid=12, link="exe")
        self.write(
            "proc/13/maps",
            f"7f00-7f10 r--p 00000000 00:2a 1234    {d}/model.gguf (deleted)\n7f10-7f20 rw-p 0 0 0 [heap]\n",
        )
        r = self.run_prog("busy", e, d, c, b, a, f"{a}-other")
        self.assertEqual((r.returncode, self.lines(r.stdout), r.stderr), (0, [d, c, b, a], ""))

    def test_no_paths(self) -> None:
        self.busy(self.home)
        r = self.run_prog("busy")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_bind_mounts_in_other_mount_namespaces(self) -> None:
        self.mountinfo(
            f"30 1 0:42 /home {self.home_mount} rw - btrfs /dev/dm-0 rw\n31 1 0:43 / /other rw - ext4 x rw\n"
        )
        os.makedirs(self.path("proc/300/ns"))
        os.symlink("mnt:[4026532001]", self.path("proc/300/ns/mnt"))
        self.write(
            "proc/300/mountinfo",
            "50 40 0:42 /home/u/.m2 /root/.m2 rw - btrfs x rw\n"
            "51 40 0:42 /home/u /var/home/u rw - btrfs x rw\n"
            "52 40 0:43 /home/u/elsewhere /e rw - ext4 x rw\n",
        )
        m2 = os.path.join(self.home, ".m2")
        repo = os.path.join(m2, "repository")
        mounted = os.path.join(self.home, "elsewhere")
        # Without --home: only a mount of the path itself or of something below it.
        r = self.run_prog("busy", repo, m2, mounted)
        self.assertEqual((r.returncode, self.lines(r.stdout)), (0, [m2]))
        # With --home: also a mount of a folder between the home and the path, not the home itself.
        r = self.run_prog("busy", "--home", self.home + "/", repo, os.path.join(self.home, "Documents"))
        self.assertEqual((r.returncode, self.lines(r.stdout)), (0, [repo]))

    def mounted_elsewhere(self, *roots: str) -> None:
        """Another mount namespace (pid 300) bind-mounts each root (below the home subvolume, in
        mountinfo's escaped form) somewhere; the program's own table has the subvolume at HOME_MOUNT."""
        os.makedirs(self.path("proc/300/ns"), exist_ok=True)
        if not os.path.lexists(self.path("proc/300/ns/mnt")):
            os.symlink("mnt:[4026532001]", self.path("proc/300/ns/mnt"))
        self.write(
            "proc/300/mountinfo",
            "".join(f"{50 + i} 40 0:42 {root} /src{i} rw - btrfs x rw\n" for i, root in enumerate(roots)),
        )

    def test_bind_mounts_of_paths_with_every_escaped_character(self) -> None:
        # The kernel escapes a blank, a tab, a line break and a backslash as \ooo. Before, only
        # \040 was decoded: a bind mount of a folder whose name held a tab or a backslash was not
        # seen, and the folder was converted while a container wrote into it.
        self.mountinfo(f"30 1 0:42 /home {self.home_mount} rw - btrfs /dev/dm-0 rw\n")
        for name, escaped in (
            ("Work space", "Work\\040space"),
            ("Work\tspace", "Work\\011space"),
            ("Work\\space", "Work\\134space"),
        ):
            with self.subTest(name=name):
                self.mounted_elsewhere(f"/home/u/{escaped}/sub")
                p = os.path.join(self.home, name, "sub")
                r = self.run_prog("busy", p, os.path.join(self.home, "Work"))
                self.assertEqual((r.returncode, self.lines(r.stdout), r.stderr), (0, [p], ""))

    def test_an_escaped_backslash_before_040_is_a_backslash_not_a_blank(self) -> None:
        # One pass, left to right: \134040 is a backslash and the text 040 (the name a\040b), as
        # cst_core's unescape_mount_field reads it; a sibling "a b" is not busy.
        self.mountinfo(f"30 1 0:42 /home {self.home_mount} rw - btrfs /dev/dm-0 rw\n")
        self.mounted_elsewhere("/home/u/a\\134040b")
        literal, blank = os.path.join(self.home, "a\\040b"), os.path.join(self.home, "a b")
        r = self.run_prog("busy", literal, blank)
        self.assertEqual((r.returncode, self.lines(r.stdout), r.stderr), (0, [literal], ""))

    def test_a_bind_mount_of_a_name_with_a_line_break_is_busy(self) -> None:
        # The source /home/u/a\012b decodes to two lines of the open paths, neither of which is the
        # path: a path with a line break counts as busy. Before, it was not listed.
        self.mountinfo(f"30 1 0:42 /home {self.home_mount} rw - btrfs /dev/dm-0 rw\n")
        self.mounted_elsewhere("/home/u/a\\012b")
        p = os.path.join(self.home, "a\nb")
        r = self.run_prog("busy", p, os.path.join(self.home, "c"))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, f"{p}\n", ""))

    def test_a_home_mount_point_with_a_blank_is_found(self) -> None:
        # The mount point field is escaped too: HOME_MOUNT ".../home mnt" is ".../home\040mnt" there.
        # Before, it never matched, and no bind mount of the home counted.
        home_mount = os.path.join(self.tmp, "home mnt")
        self.conf(HOME_MOUNT=home_mount)
        self.mountinfo(f"30 1 0:42 /home {home_mount.replace(' ', '\\040')} rw - btrfs /dev/dm-0 rw\n")
        self.mounted_elsewhere("/home/u/.m2")
        m2 = os.path.join(home_mount, "u", ".m2")
        r = self.run_prog("busy", m2)
        self.assertEqual((r.returncode, self.lines(r.stdout), r.stderr), (0, [m2], ""))

    def test_home_without_a_folder(self) -> None:
        # A usage error (exit 2) with the usage line, also for an empty folder (it would match
        # every bind mount). Before, bash's own "unbound variable" and exit 1.
        for args in (("--home",), ("--home", ""), ("--home", "", self.home)):
            with self.subTest(args):
                r = self.run_prog("busy", *args)
                self.assertEqual(
                    (r.returncode, r.stdout, r.stderr), (2, "", "usage: btrfs-snapshot busy [--home DIR] PATH...\n")
                )


if __name__ == "__main__":
    unittest.main()
