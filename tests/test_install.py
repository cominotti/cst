# SPDX-License-Identifier: MPL-2.0
"""Unit tests for installing manifest files: zipapps, the install use case, the
installer CLI, the bash library and the runtime resolver.

Everything is written under temporary folders (a staging root): nothing on
the machine is touched, no sudo, no runuser (a stub stands in for it).
"""

from collections.abc import Mapping, Sequence
import contextlib
import io
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock
import zipfile

from cst_core.adapters.install import LocalInstallTarget
from cst_core.adapters.runtime import (
    checkout_root,
    install_payload,
    program_argv,
    running_archive,
    sibling_program,
)
from cst_core.adapters.zipapp import build_python_zipapp, entry_payload, import_closure
from cst_core.application.install import InstallRefused, InstallRequest, InstallService
from cst_core.domain.dispatch import step_down_argv
from cst_core.domain import install as install_domain
from cst_core.domain.install import (
    COMPONENTS,
    INSTALL_MANIFEST,
    ArtifactKind,
    Component,
    InstallEntry,
    OnDisk,
    Owner,
    expand_dest,
    is_current,
    manifest_by_src,
    parse_manifest,
    render_template,
    select_entries,
    template_keys,
)
from cst_core.domain.record import parse_record
from cst_core.domain.values import DomainValidationError, OctalMode
from cst_core.installer import main as installer_main
from cst_core.ports import WriteFailed
from tests.budgets import is_test_file
from tests.shell_files import shell_files

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "cst_core", "installer.py")
LIB = os.path.join(REPO, "cst-install.bash")
SYSTEM_PYTHON = "/usr/bin/python3"
# main's (654bbda) btrfs/docker_gc/domain.py, its built-in registry replaced by an invented one.
LEGACY_GC_DOMAIN = os.path.join(REPO, "tests", "fixtures", "legacy", "btrfs-docker_gc-domain.py@654bbda")


def entry(
    src: str, dest: str, *, mode: int = 0o644, kind: ArtifactKind = ArtifactKind.DATA, owner: Owner = Owner.ROOT
) -> InstallEntry:
    return InstallEntry(
        src=src,
        dest=dest,
        mode=OctalMode(mode),
        owner=owner,
        kind=kind,
        runner=owner,
        component=f"{src.split('/', maxsplit=1)[0]}:t",
    )


class MemoryTarget:
    """InstallTargetPort in memory, recording writes; owners[path] = (uid, gid), links = paths that are links."""

    def __init__(self, files: Mapping[str, tuple[bytes, int]] | None = None) -> None:
        self.files: dict[str, tuple[bytes, int]] = dict(files or {})
        self.owners: dict[str, tuple[int, int]] = {}
        self.links: set[str] = set()
        self.writes: list[str] = []
        self.fail_on: str | None = None

    def current(self, path: str) -> OnDisk | None:
        if path not in self.files:
            return None
        data, mode = self.files[path]
        uid, gid = self.owners.get(path, (0, 0))
        return OnDisk(data, mode, uid, gid, path in self.links)

    def write(self, path: str, data: bytes, mode: int) -> None:
        if path == self.fail_on:
            raise WriteFailed(path, "Permission denied")
        self.files[path] = (data, mode)
        self.owners.pop(path, None)
        self.links.discard(path)
        self.writes.append(path)

    def remove(self, path: str) -> bool:
        if path == self.fail_on:
            raise WriteFailed(path, "Permission denied")
        self.links.discard(path)
        return self.files.pop(path, None) is not None


class DomainTest(unittest.TestCase):
    def test_render_template_replaces_literally_and_ends_in_one_newline(self) -> None:
        self.assertEqual(render_template("a=@A@ b=@B@\n\n", {"A": "x|&\\y", "B": "2"}), "a=x|&\\y b=2\n")
        self.assertEqual(render_template("no keys", {}), "no keys\n")
        self.assertEqual(render_template("@U@ @U@\n", {"U": "danilo"}), "danilo danilo\n")

    def test_render_template_refuses_a_placeholder_without_a_value(self) -> None:
        # Without --set UUID=..., top.mount.in was written with What=/dev/disk/by-uuid/@UUID@.
        with self.assertRaisesRegex(DomainValidationError, "@UUID@"):
            render_template("What=/dev/disk/by-uuid/@UUID@\nOptions=@OPTIONS@\n", {"OPTIONS": "o"})
        self.assertEqual(template_keys("@A@ @B_2@ @a@ user@host @@"), {"A", "B_2"})

    def test_render_template_is_one_pass(self) -> None:
        # A value holding another placeholder's spelling is written as it is, never replaced again.
        self.assertEqual(render_template("@A@ @B@", {"A": "@B@", "B": "x"}), "@B@ x\n")

    def test_is_current_link_and_owner_rules(self) -> None:
        file_ = OnDisk(b"x", 0o644, 0, 0, False)
        link = OnDisk(b"x", 0o644, 0, 0, True)
        self.assertTrue(is_current(file_, b"x", 0o644, Owner.ROOT, as_root=True))
        self.assertFalse(is_current(file_, b"x", 0o600, Owner.ROOT, as_root=True))
        self.assertFalse(is_current(None, b"x", 0o644, Owner.USER, as_root=False))
        self.assertFalse(is_current(OnDisk(None, 0, -1, -1, True), b"x", 0o644, Owner.USER, as_root=False))
        # One link rule: replaced at a root destination, read through at a user destination.
        self.assertFalse(is_current(link, b"x", 0o644, Owner.ROOT, as_root=True))
        self.assertFalse(is_current(link, b"x", 0o644, Owner.ROOT, as_root=False))
        self.assertTrue(is_current(link, b"x", 0o644, Owner.USER, as_root=False))
        # As root, a root entry must be owned by 0:0; a staged run as a user cannot check it.
        foreign = OnDisk(b"x", 0o644, 1000, 1000, False)
        self.assertFalse(is_current(foreign, b"x", 0o644, Owner.ROOT, as_root=True))
        self.assertTrue(is_current(foreign, b"x", 0o644, Owner.ROOT, as_root=False))
        self.assertTrue(is_current(foreign, b"x", 0o644, Owner.USER, as_root=True))

    def test_expand_dest(self) -> None:
        self.assertEqual(expand_dest("~/.config/x", "/home/u/"), "/home/u/.config/x")
        self.assertEqual(expand_dest("/etc/x", None), "/etc/x")
        self.assertEqual(expand_dest("~", "/home/u"), "/home/u")  # the home itself
        self.assertEqual(expand_dest("~x/y", "/home/u"), "~x/y")  # another user's ~: not expanded
        with self.assertRaises(ValueError):
            expand_dest("~/.config/x", None)

    def test_select_entries_by_module_and_kind(self) -> None:
        cpu = select_entries(INSTALL_MANIFEST, module="cpu", kind=ArtifactKind.PYTHON_ENTRY)
        self.assertEqual(
            {e.src for e in cpu},
            {"cpu/vm-pin", "cpu/cpu-irq", "cpu/cpu-power", "cpu/cpu-watch", "cpu/game-profile"},
        )
        self.assertTrue(all(e.module == "cpu" for e in select_entries(INSTALL_MANIFEST, module="cpu")))

    def test_select_entries_by_component(self) -> None:
        gc = select_entries(INSTALL_MANIFEST, component="btrfs:docker-gc")
        self.assertEqual(
            [e.src for e in gc],
            [
                "btrfs/docker-gc",
                "btrfs/units/docker-gc-recorder.service",
                "btrfs/units/docker-gc.service",
                "btrfs/units/docker-gc.timer",
            ],
        )

    def test_parse_manifest_reads_rows_and_skips_comments(self) -> None:
        table = "# header\n\na/b  /usr/local/bin/b  755 root python-entry user a:x  # why\nc/d ~/.x 644 user data user c:y\n"
        self.assertEqual(
            parse_manifest(table),
            (
                InstallEntry(
                    "a/b",
                    "/usr/local/bin/b",
                    OctalMode(0o755),
                    Owner.ROOT,
                    ArtifactKind.PYTHON_ENTRY,
                    Owner.USER,
                    "a:x",
                ),
                InstallEntry("c/d", "~/.x", OctalMode(0o644), Owner.USER, ArtifactKind.DATA, Owner.USER, "c:y"),
            ),
        )

    def test_parse_manifest_rejects_a_malformed_row(self) -> None:
        for row in (
            "a/b /x 755 root python-entry a:x",
            "a/b /x 755 root python-entry user",
            "a/b /x 9z root data root a:x",
            "a/b /x 644 admin data root a:x",
            "a/b /x 644 root binary root a:x",
            "a/b /x 644 root data root a:x extra",
        ):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "manifest row 2"):
                parse_manifest("# header\n" + row)

    def test_manifest_is_consistent(self) -> None:
        srcs = [e.src for e in INSTALL_MANIFEST]
        dests = [e.dest for e in INSTALL_MANIFEST]
        self.assertEqual(len(srcs), len(set(srcs)), "a src is listed twice")
        self.assertEqual(len(dests), len(set(dests)), "two entries write the same dest")
        for e in INSTALL_MANIFEST:
            with self.subTest(src=e.src):
                self.assertTrue(e.dest.startswith(("/", "~/")))
                self.assertEqual(e.owner == "user", e.dest.startswith("~/"))
                executable = e.kind in ("python-entry", "shell")
                self.assertEqual(bool(e.mode.bits & 0o111), executable)


class InstallServiceTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.payloads = {"m/a": b"A\n", "m/t.in": b"uuid=@UUID@\n", "m/u": b"U\n"}
        self.a = entry("m/a", "/etc/a")
        self.t = entry("m/t.in", "/etc/t")
        self.u = entry("m/u", "~/.config/u", owner=Owner.USER)
        self.target = MemoryTarget()
        self.svc = InstallService(self.target, lambda e: self.payloads[e.src])

    def test_plan_changes_nothing_then_install_then_second_run_is_a_no_op(self) -> None:
        req = InstallRequest(values={"UUID": "1234"})
        lines, todo = self.svc.plan([self.a, self.t], req)
        self.assertEqual(lines, ["would write: /etc/a", "would write: /etc/t"])
        self.assertEqual(todo, 2)
        self.assertEqual(self.target.writes, [])
        self.assertEqual(list(self.svc.install([self.a, self.t], req)), ["written:   /etc/a", "written:   /etc/t"])
        self.assertEqual(self.target.files["/etc/t"], (b"uuid=1234\n", 0o644))
        self.assertEqual(self.svc.plan([self.a, self.t], req), (["unchanged: /etc/a", "unchanged: /etc/t"], 0))
        self.assertEqual(list(self.svc.install([self.a, self.t], req)), ["unchanged: /etc/a", "unchanged: /etc/t"])
        self.assertEqual(self.target.writes, ["/etc/a", "/etc/t"])

    def test_a_changed_mode_or_content_is_rewritten(self) -> None:
        self.target.files["/etc/a"] = (b"A\n", 0o600)
        self.assertEqual(list(self.svc.install([self.a], InstallRequest())), ["written:   /etc/a"])
        self.target.files["/etc/a"] = (b"old\n", 0o644)
        self.assertEqual(list(self.svc.install([self.a], InstallRequest())), ["written:   /etc/a"])

    def test_as_root_a_root_file_owned_by_a_user_is_rewritten(self) -> None:
        self.target.files["/etc/a"] = (b"A\n", 0o644)
        self.target.owners["/etc/a"] = (1000, 1000)  # left by a hand copy
        self.assertEqual(self.svc.plan([self.a], InstallRequest(as_root=True)), (["would write: /etc/a"], 1))
        self.assertEqual(list(self.svc.install([self.a], InstallRequest(as_root=True))), ["written:   /etc/a"])
        self.assertEqual(list(self.svc.install([self.a], InstallRequest(as_root=True))), ["unchanged: /etc/a"])

    def test_a_link_is_rewritten_at_a_root_destination_and_kept_at_a_user_one(self) -> None:
        self.target.files.update({"/etc/a": (b"A\n", 0o644), "/home/u/.config/u": (b"U\n", 0o644)})
        self.target.links.update({"/etc/a", "/home/u/.config/u"})
        req = InstallRequest(home="/home/u")
        self.assertEqual(
            list(self.svc.install([self.a, self.u], req)), ["written:   /etc/a", "unchanged: /home/u/.config/u"]
        )
        self.assertEqual(self.target.links, {"/home/u/.config/u"})

    def test_a_template_value_missing_or_unused_writes_nothing(self) -> None:
        for values, why in (({}, "@UUID@"), ({"UUID": "1", "UIDD": "2"}, "--set UIDD")):
            with self.subTest(values=values):
                req = InstallRequest(values=values)
                # Raised when install is called, before any file (also /etc/a, listed first) is written.
                with self.assertRaisesRegex(DomainValidationError, why):
                    self.svc.install([self.a, self.t], req)
                with self.assertRaisesRegex(DomainValidationError, why):
                    self.svc.plan([self.a, self.t], req)
                with self.assertRaisesRegex(DomainValidationError, why):
                    self.svc.desired(self.t, values)
        # A value is used when any template of the call uses it; files with no template take no --set.
        with self.assertRaisesRegex(DomainValidationError, "--set UUID"):
            self.svc.install([self.a], InstallRequest(values={"UUID": "1"}))
        self.assertEqual(self.target.writes, [])

    def test_lines_come_as_files_are_written_and_a_failure_names_its_file(self) -> None:
        self.target.fail_on = "/etc/t"
        lines = self.svc.install([self.a, self.t, self.u], InstallRequest(values={"UUID": "1"}, home="/h"))
        self.assertEqual(next(lines), "written:   /etc/a")
        with self.assertRaisesRegex(WriteFailed, "cannot write /etc/t: Permission denied"):
            next(lines)
        self.assertEqual(self.target.writes, ["/etc/a"])

    def test_user_entries_go_to_the_given_home_and_never_as_root(self) -> None:
        self.assertEqual(
            list(self.svc.install([self.u], InstallRequest(home="/home/u"))), ["written:   /home/u/.config/u"]
        )
        with self.assertRaises(InstallRefused):
            self.svc.install([self.u], InstallRequest(home="/home/u", as_root=True))
        # Staged (tests, the distribution gate): allowed, nothing real is touched.
        self.assertEqual(
            list(self.svc.install([self.u], InstallRequest(home="/home/u", as_root=True, root="/stage"))),
            ["written:   /stage/home/u/.config/u"],
        )

    def test_staging_root_and_only_manifest_destinations(self) -> None:
        # The entry's own dest, under the staging root; there is no other destination to ask for.
        self.assertEqual(list(self.svc.install([self.a], InstallRequest(root="/stage/"))), ["written:   /stage/etc/a"])
        for root in ("stage", "/stage/../etc"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.svc.install([self.a], InstallRequest(root=root))
        with self.assertRaises(ValueError):
            self.svc.install([entry("m/b", "/etc/../root/b")], InstallRequest())


class ZipappTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-install-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        root = Path(self.tmp, "repo")
        self.root = root
        for rel, text in {
            "cst_core/__init__.py": "CORE_VAL = 7\n",
            "demo/__init__.py": "",
            "demo/domain.py": (
                "from dataclasses import dataclass\nimport other\n\n"
                "@dataclass(frozen=True, slots=True)\nclass Item:\n    v: int\n"
            ),
            "demo/tests/__init__.py": "",
            "demo/tests/test_x.py": "raise SystemExit('tests never ship')\n",
            "demo/data.conf": "KEY=1\n",
            "other/__init__.py": "OTHER = 1\n",
            "unused/__init__.py": "",
            "demo/demo-tool": (
                "#!/usr/bin/python3 -I\n"
                "# managed-by: demo-setup.sh (CST)\n"
                "def main() -> None:\n"
                "    from cst_core import CORE_VAL\n"
                "    from demo.domain import Item\n"
                "    print(Item(CORE_VAL).v)\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            ),
        }.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    def test_closure_follows_function_level_and_package_imports(self) -> None:
        src = (self.root / "demo" / "demo-tool").read_text(encoding="utf-8")
        self.assertEqual(import_closure(self.root, src), {"cst_core", "demo", "other"})

    def test_zipapp_is_deterministic_self_contained_and_runs_from_root(self) -> None:
        b1 = build_python_zipapp(self.root, "demo/demo-tool")
        b2 = build_python_zipapp(str(self.root), "/demo/demo-tool")
        self.assertEqual(b1, b2)
        self.assertTrue(b1.startswith(b"#!/usr/bin/python3 -I\n# managed-by: demo-setup.sh (CST)\n"))
        out = Path(self.tmp, "bin", "demo-tool")
        out.parent.mkdir()
        out.write_bytes(b1)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        self.assertEqual(
            names,
            sorted(names),
        )
        self.assertIn("other/__init__.py", names)
        self.assertNotIn("unused/__init__.py", names)
        self.assertFalse([n for n in names if "/tests/" in n])
        self.assertNotIn("demo/data.conf", names)  # only Python sources ship
        os.chmod(out, 0o755)
        shutil.rmtree(self.root)  # the zipapp must not need the sources
        res = subprocess.run(
            [sys.executable, "-I", "-B", str(out)],
            check=False,
            capture_output=True,
            text=True,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual((res.returncode, res.stdout.strip(), res.stderr), (0, "7", ""))

    def test_zipapp_keeps_the_managed_by_line_below_a_license_header(self) -> None:
        # The SPDX line takes line 2 of an entry, which moves its managed-by line to line 3:
        # it must still be copied in front of the archive, right after the shebang.
        (self.root / "demo" / "demo-tool").write_text(
            "#!/usr/bin/python3 -I\n"
            "# SPDX-License-Identifier: MPL-2.0\n"
            "# managed-by: demo-setup.sh (CST)\n"
            "def main() -> None:\n"
            "    pass\n",
            encoding="utf-8",
        )
        built = build_python_zipapp(self.root, "demo/demo-tool")
        self.assertTrue(
            built.startswith(b"#!/usr/bin/python3 -I\n# managed-by: demo-setup.sh (CST)\nPK\x03\x04"), built[:120]
        )

    def test_entry_payload_copies_other_kinds(self) -> None:
        e = entry("demo/data.conf", "/etc/x")
        self.assertEqual(entry_payload(self.root, e), b"KEY=1\n")


class LocalTargetTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-install-target-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.t = LocalInstallTarget()

    def test_write_creates_folders_sets_mode_and_replaces_a_link(self) -> None:
        victim = os.path.join(self.tmp, "victim")
        Path(victim).write_bytes(b"keep")
        dest = os.path.join(self.tmp, "a", "b", "file")
        os.makedirs(os.path.dirname(dest))
        os.symlink(victim, dest)
        os.chmod(victim, 0o640)
        uid, gid = os.getuid(), os.getgid()
        # A link is read through and reported as a link (domain.install.is_current decides what that means).
        self.assertEqual(self.t.current(dest), OnDisk(b"keep", 0o640, uid, gid, True))
        self.t.write(dest, b"new", 0o750)
        self.assertEqual(Path(victim).read_bytes(), b"keep", "the link was followed")
        self.assertFalse(os.path.islink(dest))
        self.assertEqual(self.t.current(dest), OnDisk(b"new", 0o750, uid, gid, False))
        self.assertIsNone(self.t.current(os.path.join(self.tmp, "missing")))
        folder = self.t.current(self.tmp)
        self.assertTrue(folder is not None and folder.data is None, "a folder is not a file")
        os.symlink(os.path.join(self.tmp, "missing"), os.path.join(self.tmp, "dangling"))
        self.assertEqual(self.t.current(os.path.join(self.tmp, "dangling")), OnDisk(None, 0, -1, -1, True))
        os.symlink(self.tmp, os.path.join(self.tmp, "to-a-folder"))
        to_folder = self.t.current(os.path.join(self.tmp, "to-a-folder"))
        self.assertTrue(to_folder is not None and to_folder.data is None and to_folder.is_link)

    def test_a_write_that_fails_names_the_destination(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro, mode=0o555)
        with self.assertRaises(WriteFailed) as cm:
            self.t.write(os.path.join(ro, "f"), b"x", 0o644)
        self.assertEqual(str(cm.exception), f"cannot write {ro}/f: Permission denied")

    def test_a_link_to_the_wanted_content_is_left_alone_at_a_user_destination(self) -> None:
        # A dotfiles link (~/.config/gamemode.ini -> a copy elsewhere) that already holds what
        # install writes stays a link, and the line says unchanged, as on main.
        real = os.path.join(self.tmp, "real")
        Path(real).write_bytes(b"x\n")
        os.chmod(real, 0o644)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        service = InstallService(self.t, lambda _e: b"")
        self.assertEqual(service.write_if_changed(link, b"x\n", 0o644, Owner.USER), f"unchanged: {link}")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(service.write_if_changed(link, b"y\n", 0o644, Owner.USER), f"written:   {link}")
        self.assertFalse(os.path.islink(link), "a write replaces the link, never writes through it")
        self.assertEqual(Path(real).read_bytes(), b"x\n")

    def test_a_link_at_a_root_destination_is_replaced_even_with_the_wanted_content(self) -> None:
        real = os.path.join(self.tmp, "real")
        Path(real).write_bytes(b"x\n")
        os.chmod(real, 0o644)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        service = InstallService(self.t, lambda _e: b"")
        self.assertEqual(service.write_if_changed(link, b"x\n", 0o644, Owner.ROOT), f"written:   {link}")
        self.assertFalse(os.path.islink(link))
        self.assertEqual(service.write_if_changed(link, b"x\n", 0o644, Owner.ROOT), f"unchanged: {link}")
        self.assertEqual(Path(real).read_bytes(), b"x\n")


def _run(
    argv: list[str], *, stdin: bytes = b"", env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        input=stdin,
        cwd="/",
        env=dict(env) if env is not None else {"PATH": "/usr/bin:/bin"},
    )


@unittest.skipUnless(os.access(SYSTEM_PYTHON, os.X_OK), "needs /usr/bin/python3")
class InstallerCliTest(unittest.TestCase):
    """The installer on the real manifest, into a staging root: the full install cycle."""

    @override
    def setUp(self) -> None:
        self.stage = tempfile.mkdtemp(prefix="cst-installer-cli-")
        self.addCleanup(shutil.rmtree, self.stage)

    def cli(self, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return _run([SYSTEM_PYTHON, "-I", "-B", INSTALLER, *args], stdin=stdin)

    def test_help_names_home_for_the_generated_files_removals(self) -> None:
        # cst_remove_text_as and cst_forget_text_as pass --home to pick the user's record: help says so.
        r = self.cli("help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for usage in ("remove-text DEST... [--home DIR] [--root DIR]", "forget-text DEST... [--home DIR] [--root DIR]"):
            self.assertIn(f"  installer.py {usage}\n", r.stdout.decode())

    def test_payload_into_a_closed_pipe_stops_quietly(self) -> None:
        # bluetooth-setup.sh runs `cmp -s <(cst_installer payload ...) DEST`: cmp stops reading at the first
        # difference, and the installer must then end without a BrokenPipeError on the user's terminal.
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                [SYSTEM_PYTHON, "-I", "-B", INSTALLER, "payload", "bluetooth/bt-ldac-watch"],
                check=False,
                stdout=write_end,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd="/",
                env={"PATH": "/usr/bin:/bin"},
            )
        finally:
            os.close(write_end)
        self.assertEqual(proc.stderr, b"")

    def files(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for dirpath, _dirs, names in os.walk(self.stage):
            for name in names:
                p = os.path.join(dirpath, name)
                out[os.path.relpath(p, self.stage)] = Path(p).read_bytes()
        return out

    def test_full_cycle_on_the_btrfs_module(self) -> None:
        sel = ["--root", self.stage, "--module", "btrfs", "--set", "UUID=u-1", "--set", "OPTIONS=o"]
        plan = self.cli("plan", *sel)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(self.files(), {}, "plan wrote something")
        n = len(select_entries(INSTALL_MANIFEST, module="btrfs"))
        # And docker-gc's settings, written before its program (nothing installed: none protected).
        conf = f"{self.stage}/etc/cst/docker-gc.conf"
        self.assertEqual(plan.stdout.decode().count("would write: "), n + 1)
        self.assertIn(f"would write: {conf} (from {self.stage}/usr/local/sbin/docker-gc, before", plan.stdout.decode())
        self.assertEqual(self.cli("check", *sel).returncode, 1)

        first = self.cli("install", *sel)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.decode().count("written:   "), n + 1)
        self.assertEqual(first.stdout.decode().splitlines()[0], f"written:   {conf} (DOCKER_GC_PROTECT=)")
        self.assertEqual(self.cli("check", *sel).returncode, 0, "status after apply is not current")
        second = self.cli("install", *sel)
        self.assertEqual(second.stdout.decode().count("unchanged: "), n, second.stdout)

        reclaim = Path(self.stage, "etc/tmpfiles.d/btrfs-automatic-reclaim.conf").read_text()
        self.assertIn("/sys/fs/btrfs/u-1/allocation", reclaim)
        self.assertNotIn("@", reclaim.replace("w-", ""))
        gc = Path(self.stage, "usr/local/sbin/docker-gc")
        self.assertTrue(zipfile.is_zipfile(gc))
        self.assertEqual(stat.S_IMODE(gc.stat().st_mode), 0o755)

        dests = self.cli("dests", "--module", "btrfs").stdout.decode().split()
        self.assertEqual(len(dests), n)
        for d in dests:
            os.unlink(self.stage + d)
        self.assertEqual(self.cli("check", *sel).returncode, 1, "uninstall left files current")

    def stage_old_docker_gc(self) -> str:
        """main's (654bbda) docker-gc as it installed it: a zipapp whose domain.py has a registry built in."""
        prog = os.path.join(self.stage, "usr/local/sbin/docker-gc")
        os.makedirs(os.path.dirname(prog))
        with zipfile.ZipFile(prog, "w") as z:
            z.writestr("__main__.py", "raise SystemExit(1)\n")
            z.write(LEGACY_GC_DOMAIN, "btrfs/docker_gc/domain.py")
        os.chmod(prog, 0o755)
        return prog

    def test_every_install_path_keeps_the_registry_of_an_older_docker_gc(self) -> None:
        # The installer replaces docker-gc by component, by module, by owner or by name: each time
        # the settings file is missing, it is first written from the installed copy.
        conf = os.path.join(self.stage, "etc/cst/docker-gc.conf")
        sets = ["--set", "UUID=u", "--set", "OPTIONS=o", "--set", "USER=u"]
        for sel in (
            ["--component", "btrfs:docker-gc"],
            ["--module", "btrfs", "--set", "UUID=u", "--set", "OPTIONS=o"],
            ["--owner", "root", *sets],
            ["btrfs/docker-gc"],
        ):
            with self.subTest(sel=sel):
                shutil.rmtree(self.stage)
                os.makedirs(self.stage)
                prog = self.stage_old_docker_gc()
                plan = self.cli("plan", "--root", self.stage, *sel)
                self.assertIn(f"would write: {conf} (from {prog}, before it is replaced)", plan.stdout.decode())
                self.assertFalse(os.path.exists(conf), "plan wrote the settings")
                r = self.cli("install", "--root", self.stage, *sel)
                self.assertEqual(r.returncode, 0, r.stderr)
                out = r.stdout.decode().splitlines()
                self.assertEqual(out[0], f"written:   {conf} (DOCKER_GC_PROTECT=registry.example.com)")
                self.assertIn(f"written:   {prog}", out)
                self.assertTrue(Path(conf).read_text().endswith("\nDOCKER_GC_PROTECT=registry.example.com\n"))
                self.assertEqual(stat.S_IMODE(os.stat(conf).st_mode), 0o644)
                # From now on the file is there: a second install runs no step and changes nothing.
                again = self.cli("install", "--root", self.stage, *sel).stdout.decode()
                self.assertNotIn(conf, again)
                self.assertEqual({line.split()[0] for line in again.splitlines()}, {"unchanged:"})

    def test_a_remove_keeps_the_registry_of_an_older_docker_gc(self) -> None:
        prog = self.stage_old_docker_gc()
        conf = os.path.join(self.stage, "etc/cst/docker-gc.conf")
        r = self.cli("remove", "--root", self.stage, "--component", "btrfs:docker-gc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.decode().splitlines()[:2],
            [f"written:   {conf} (DOCKER_GC_PROTECT=registry.example.com)", f"removed:   {prog}"],
        )
        self.assertTrue(Path(conf).read_text().endswith("\nDOCKER_GC_PROTECT=registry.example.com\n"))

    def test_an_installed_docker_gc_it_cannot_read_stops_the_install_before_any_write(self) -> None:
        prog = os.path.join(self.stage, "usr/local/sbin/docker-gc")
        os.makedirs(os.path.dirname(prog))
        Path(prog).write_text("print('not a docker-gc of CST')\n")
        for command in ("install", "remove"):
            r = self.cli(command, "--root", self.stage, "--component", "btrfs:docker-gc")
            self.assertEqual(r.returncode, 1, command)
            self.assertIn(
                f"{prog}: its settings {self.stage}/etc/cst/docker-gc.conf could not be written", r.stderr.decode()
            )
            self.assertEqual(r.stdout, b"")
            self.assertEqual(Path(prog).read_text(), "print('not a docker-gc of CST')\n")
            self.assertEqual(sorted(self.files()), ["usr/local/sbin/docker-gc"], "something was written")

    def test_component_selects_install_plan_dests_and_srcs(self) -> None:
        sel = ["--root", self.stage, "--component", "btrfs:snapshots"]
        want = [e for e in INSTALL_MANIFEST if e.component == "btrfs:snapshots"]
        plan = self.cli("plan", *sel)
        self.assertEqual(plan.stdout.decode().count("would write: "), len(want), plan.stdout)
        self.assertEqual(self.files(), {}, "plan wrote something")
        first = self.cli("install", *sel)
        self.assertEqual(first.returncode, 0, first.stderr)
        # The files, and the install record that lists them.
        self.assertEqual(
            sorted(self.files()),
            sorted(
                [*(e.dest.lstrip("/") for e in want), "var/lib/cst/installed.json", "var/lib/cst/installed.json.lock"]
            ),
        )
        self.assertEqual(self.cli("check", *sel).returncode, 0)
        dests = self.cli("dests", "--component", "btrfs:snapshots").stdout.decode().split()
        self.assertEqual(dests, [e.dest for e in want])
        srcs = self.cli("srcs", "--component", "btrfs:snapshots", "--kind", "shell").stdout.decode().split()
        self.assertEqual(srcs, ["btrfs/btrfs-snapshot"])

    def test_an_unknown_component_or_a_mix_with_names_is_a_usage_error(self) -> None:
        for args in (
            ["dests", "--component", "btrfs:nope"],
            ["install", "--component", "btrfs:core", "btrfs/btrfs-notify"],
        ):
            with self.subTest(args=args):
                r = self.cli(*args, "--root", self.stage)
                self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.files(), {})

    def test_builds_slice_installs_with_low_weights_and_no_allowed_cpus(self) -> None:
        want = Path(REPO, "cpu/units/builds.slice").read_text()
        self.assertIn("CPUWeight=20\n", want)
        self.assertIn("IOWeight=20\n", want)
        self.assertNotIn("AllowedCPUs=", want)
        r = self.cli("install", "--root", self.stage, "--component", "cpu:docker-slice")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(self.stage, "etc/systemd/system/builds.slice").read_text(), want)

    def test_upgrade_from_a_plain_script_install(self) -> None:
        dest = Path(self.stage, "usr/local/sbin/btrfs-stall-monitord")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(Path(REPO, "btrfs/btrfs-stall-monitord").read_bytes())  # what the old install_file copied
        dest.chmod(0o755)
        plan = self.cli("plan", "--root", self.stage, "btrfs/btrfs-stall-monitord")
        self.assertIn(b"would write: ", plan.stdout)
        self.cli("install", "--root", self.stage, "btrfs/btrfs-stall-monitord")
        self.assertTrue(zipfile.is_zipfile(dest))
        self.assertEqual(self.cli("check", "--root", self.stage, "btrfs/btrfs-stall-monitord").returncode, 0)

    def test_payload_is_what_install_writes_and_text_writes_generated_content(self) -> None:
        payload = self.cli("payload", "cpu/vm-pin").stdout
        self.cli("install", "--root", self.stage, "cpu/vm-pin")
        self.assertEqual(Path(self.stage, "usr/local/sbin/cpu-vm-pin").read_bytes(), payload)
        r = self.cli("text", "/etc/x.conf", "0640", "--root", self.stage, stdin=b"generated\n")
        self.assertEqual(r.stdout, f"written:   {self.stage}/etc/x.conf\n".encode())
        r = self.cli("text", "/etc/x.conf", "0640", "--root", self.stage, stdin=b"generated\n")
        self.assertEqual(r.stdout, f"unchanged: {self.stage}/etc/x.conf\n".encode())
        self.assertEqual(stat.S_IMODE(os.stat(f"{self.stage}/etc/x.conf").st_mode), 0o640)

    def test_text_plan_judges_generated_content_and_writes_nothing(self) -> None:
        dest = f"{self.stage}/etc/x.conf"
        r = self.cli("text-plan", "/etc/x.conf", "0640", "--root", self.stage, stdin=b"generated\n")
        self.assertEqual((r.returncode, r.stdout), (0, f"would write: {dest}\n".encode()))
        self.assertEqual(self.files(), {})
        self.cli("text", "/etc/x.conf", "0640", "--root", self.stage, stdin=b"generated\n")
        before = self.files()
        for label, data, mode, line in (
            ("same", b"generated\n", "0640", "unchanged"),
            ("other content", b"other\n", "0640", "would write"),
            ("other mode", b"generated\n", "0644", "would write"),
        ):
            with self.subTest(label):
                r = self.cli("text-plan", "/etc/x.conf", mode, "--root", self.stage, stdin=data)
                self.assertEqual(r.stdout, f"{line}: {dest}\n".encode(), r.stderr)
                self.assertEqual(self.files(), before)
        os.remove(dest)
        os.symlink(f"{self.stage}/etc/target", dest)
        Path(self.stage, "etc/target").write_bytes(b"generated\n")
        os.chmod(f"{self.stage}/etc/target", 0o640)
        # As root (a root destination) a link is never current; as a user (a home destination,
        # how CI runs) it is judged by its target, which holds the same bytes.
        with self.subTest("a link to the same bytes: current only at a user destination"):
            r = self.cli("text-plan", "/etc/x.conf", "0640", "--root", self.stage, stdin=b"generated\n")
            want = "would write" if os.geteuid() == 0 else "unchanged"
            self.assertEqual(r.stdout, f"{want}: {dest}\n".encode(), r.stderr)
            self.assertTrue(os.path.islink(dest))

    def test_user_entries_need_a_home(self) -> None:
        r = self.cli("install", "--root", self.stage, "cpu/gamemode.ini")
        self.assertEqual(r.returncode, 2)
        r = self.cli("install", "--root", self.stage, "--home", "/home/u", "cpu/gamemode.ini")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(Path(self.stage, "home/u/.config/gamemode.ini").is_file())

    def test_a_template_value_missing_or_unused_is_a_usage_error_and_writes_nothing(self) -> None:
        # No --set USER wrote a polkit rule with "@USER@" in it, and reported success.
        srcs = ("btrfs/btrfs-notify", "cpu/cpu-game.rules.in")
        for sets, why in (((), b"@USER@"), (("--set", "USER=u", "--set", "USR=u"), b"--set USR")):
            for command in ("install", "plan", "check"):
                with self.subTest(command=command, sets=sets):
                    r = self.cli(command, "--root", self.stage, *sets, *srcs)
                    self.assertEqual(r.returncode, 2, r)
                    self.assertIn(why, r.stderr)
                    self.assertEqual(r.stdout, b"")
        r = self.cli("payload", "cpu/cpu-game.rules.in")
        self.assertEqual((r.returncode, r.stdout), (2, b""))
        self.assertEqual(self.files(), {})
        r = self.cli("install", "--root", self.stage, "--set", "USER=u", *srcs)
        self.assertEqual(r.returncode, 0, r.stderr)
        rule = Path(self.stage, "etc/polkit-1/rules.d/50-cpu-setup-game.rules").read_text()
        self.assertIn('subject.user == "u"', rule)
        self.assertNotIn("@USER@", rule)

    def test_a_file_that_cannot_be_written_is_named_after_the_lines_already_printed(self) -> None:
        # A traceback, and no line for the file already written.
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        units = Path(self.stage, "etc/systemd/system")
        units.mkdir(parents=True)
        units.chmod(0o555)
        self.addCleanup(units.chmod, 0o755)
        r = self.cli("install", "--root", self.stage, "btrfs/btrfs-notify", "btrfs/units/btrfs-bulk-boot.service")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.decode(), f"written:   {self.stage}/usr/local/sbin/btrfs-notify\n")
        self.assertEqual(
            r.stderr.decode(),
            f"cst installer: cannot write {self.stage}/etc/systemd/system/btrfs-bulk-boot.service: Permission denied\n",
        )

    def test_usage_errors_exit_2_and_change_nothing(self) -> None:
        for args in (
            ("install", "--root", self.stage, "no/such-file"),
            ("install", "--root", self.stage),
            ("install", "--root", self.stage, "--module", "cpu", "cpu/vm-pin"),
            ("install", "--root", self.stage, "--dest", "/x", "cpu/vm-pin"),  # no free destination
            ("install", "--root", "relative/stage", "cpu/vm-pin"),
            ("text", "/etc/../tmp/x", "0644", "--root", self.stage),
            ("text", "etc/x", "0644", "--root", self.stage),
            ("install", "--root", self.stage, "--set", "NOEQUALS", "cpu/vm-pin"),
            ("text", "/etc/x", "9999", "--root", self.stage),
            ("text-plan", "etc/x", "0644", "--root", self.stage),
            ("text-plan", "/etc/x", "--root", self.stage),
            ("bogus",),
        ):
            with self.subTest(args=args):
                r = self.cli(*args)
                self.assertIn(r.returncode, (1, 2), r)
                self.assertTrue(r.stderr)
        self.assertEqual(self.files(), {})


@unittest.skipUnless(os.access(SYSTEM_PYTHON, os.X_OK), "needs /usr/bin/python3")
class BashLibraryTest(unittest.TestCase):
    """cst-install.bash, with runuser and getent stubbed (no privilege change happens)."""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-install-bash-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stage = os.path.join(self.tmp, "stage")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.log = os.path.join(self.tmp, "calls.log")
        stubs = {
            # runuser -u USER -- CMD...: logged, then CMD runs as the current user.
            "runuser": '#!/bin/sh\necho "runuser $*" >>"$LOG"\nshift 3\nexec "$@"\n',
            "getent": '#!/bin/sh\n[ "$2" = alice ] && echo "alice:x:1000:1000::$HOMEDIR:/bin/bash"\n',
        }
        for name, text in stubs.items():
            p = os.path.join(bindir, name)
            Path(p).write_text(text)
            os.chmod(p, 0o755)
        self.env = {"PATH": f"{bindir}:/usr/bin:/bin", "LOG": self.log, "HOMEDIR": "/home/alice"}

    def bash(self, script: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return _run(["bash", "-c", f'source "{LIB}"\n{script}'], stdin=stdin, env=self.env)

    def test_cst_install_and_text(self) -> None:
        r = self.bash(f'cst_install --root "{self.stage}" btrfs/btrfs-notify')
        self.assertEqual(r.stdout.decode(), f"written:   {self.stage}/usr/local/sbin/btrfs-notify\n", r.stderr)
        r = self.bash(f'cst_install_text "{self.stage}/etc/gen" 0644', stdin=b"x\n")
        self.assertEqual(r.stdout.decode(), f"written:   {self.stage}/etc/gen\n")
        r = self.bash(f'cst_installed_dests --module cpu --kind python-entry --root "{self.stage}"')
        self.assertEqual(
            r.stdout.decode().split(),
            [
                "/usr/local/sbin/cpu-vm-pin",
                "/usr/local/sbin/cpu-irq",
                "/usr/local/sbin/cpu-power",
                "/usr/local/bin/cpu-watch",
                "/usr/local/sbin/cpu-game-profile",
            ],
        )

    def test_cst_forget_text_drops_the_line_and_leaves_the_file(self) -> None:
        # Root's generated files in a setup script; here the installer runs as this account, so the
        # record is this account's, staged. No runuser: it runs as the caller.
        dest = "/etc/gen.rules"
        r = self.bash(
            f'cst_installer text {dest} 0644 --component btrfs:degraded-boot --root "{self.stage}"', stdin=b"x\n"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        [record] = Path(self.stage).rglob("installed.json")
        self.assertIn(dest, parse_record(record.read_text()).files)
        r = self.bash(f'cst_forget_text {dest} --root "{self.stage}"')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertNotIn(dest, parse_record(record.read_text()).files)
        self.assertEqual(Path(self.stage + dest).read_bytes(), b"x\n")
        self.assertFalse(os.path.exists(self.log), "ran through runuser")

    def test_user_files_are_written_through_runuser_with_the_users_home(self) -> None:
        r = self.bash(f'cst_install_as alice --root "{self.stage}" cpu/gamemode.ini')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode(), f"written:   {self.stage}/home/alice/.config/gamemode.ini\n")
        self.assertIn("runuser -u alice -- /usr/bin/python3 -I -B", Path(self.log).read_text())
        r = self.bash(f'cst_install_text "{self.stage}/u" 0600 alice', stdin=b"y\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(self.log).read_text().count("runuser -u alice"), 2)
        r = self.bash(f'cst_install_text_plan "{self.stage}/u" 0600 alice', stdin=b"y\n")
        self.assertEqual(r.stdout.decode(), f"unchanged: {self.stage}/u\n", r.stderr)
        self.assertEqual(Path(self.log).read_text().count("runuser -u alice"), 3)
        r = self.bash("cst_install_as nobody-here cpu/gamemode.ini")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"no home folder", r.stderr)

    def test_cst_install_text_plan_changes_nothing(self) -> None:
        r = self.bash(f'cst_install_text_plan "{self.stage}/etc/gen" 0644', stdin=b"x\n")
        self.assertEqual((r.returncode, r.stdout.decode()), (0, f"would write: {self.stage}/etc/gen\n"), r.stderr)
        self.assertFalse(os.path.exists(self.stage))
        self.bash(f'cst_install_text "{self.stage}/etc/gen" 0644', stdin=b"x\n")
        r = self.bash(f'cst_install_text_plan "{self.stage}/etc/gen" 0644', stdin=b"x\n")
        self.assertEqual(r.stdout.decode(), f"unchanged: {self.stage}/etc/gen\n", r.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_the_installer_gets_the_caller_s_lc_ctype(self) -> None:
        # Python sets LC_CTYPE=C.UTF-8 under a C locale (PEP 538); each of the three ways in hands the
        # caller's value over for installer.py to put back.
        probe = os.path.join(self.tmp, "probe.py")
        Path(probe).write_text("import os, sys\nprint(sys.argv[1], repr(os.environ.get('CST_LC_CTYPE')))\n")
        script = f'CST_INSTALLER="{probe}"\ncst_installer a\ncst_install_as alice b\ncst_install_text /x 0644 alice'
        for caller, handed in (({}, "''"), ({"LC_CTYPE": "C"}, "'=C'")):
            with self.subTest(caller=caller):
                self.env.update(caller)
                r = self.bash(script)
                self.assertEqual(r.stdout.decode(), f"a {handed}\ninstall {handed}\ntext {handed}\n", r.stderr)

    def test_installer_main_puts_the_caller_s_lc_ctype_back(self) -> None:
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGPIPE, signal.SIGXFSZ)}
        for sig, handler in saved.items():
            self.addCleanup(signal.signal, sig, handler)
        with (
            mock.patch.dict(os.environ, {"CST_LC_CTYPE": "", "LC_CTYPE": "C.UTF-8"}, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(installer_main(["help"]), 0)
            self.assertEqual(dict(os.environ), {}, "what the commands it runs inherit: LC_CTYPE unset, as the caller's")


class RuntimeResolverTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-runtime-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_checkout(self) -> None:
        main = os.path.join(REPO, "cpu", "cpu-watch")
        self.assertIsNone(running_archive(main))
        self.assertEqual(checkout_root(main), REPO)
        self.assertEqual(program_argv(main), ["/usr/bin/python3", "-I", "-B", main])
        self.assertEqual(sibling_program("cpu/vm-pin", main), os.path.join(REPO, "cpu", "vm-pin"))
        self.assertEqual(install_payload("cpu/vm-pin", main), entry_payload(REPO, manifest_by_src()["cpu/vm-pin"]))

    def test_installed_zipapp(self) -> None:
        archive = os.path.join(self.tmp, "cpu-vm-pin")
        data = build_python_zipapp(REPO, "cpu/vm-pin")
        Path(archive).write_bytes(data)
        main = os.path.join(archive, "__main__.py")
        self.assertEqual(running_archive(main), archive)
        self.assertIsNone(checkout_root(main))
        self.assertEqual(program_argv(main), ["/usr/bin/python3", "-I", "-B", archive])
        self.assertEqual(sibling_program("cpu/cpu-watch", main), "/usr/local/bin/cpu-watch")
        self.assertEqual(
            sibling_program("toolchains/cst-toolchains", main, home="/home/u"), "/home/u/.local/lib/cst/cst-toolchains"
        )
        self.assertEqual(install_payload("cpu/vm-pin", main), data)

    def test_neither(self) -> None:
        with self.assertRaises(FileNotFoundError):
            install_payload("cpu/vm-pin", os.path.join(self.tmp, "x", "y"))


class CstAsUserTest(unittest.TestCase):
    """cst_as_user (cst-install.bash) is the bash copy of cst_core.domain.dispatch.step_down_argv:
    runuser is a function that prints its argument list (NUL-separated) instead of running it."""

    def run_bash(self, script: str) -> subprocess.CompletedProcess[bytes]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # What sudo hands root: its own runtime folder and bus, and SUDO_USER.
            "XDG_RUNTIME_DIR": "/run/user/0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
            "SUDO_USER": "someone",
        }
        body = f'source "{LIB}"\nrunuser() {{ printf "%s\\0" runuser "$@"; }}\n{script}'
        return subprocess.run(
            ["bash", "-c", body], check=False, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=60
        )

    def test_the_argument_list_is_step_down_argvs(self) -> None:
        me = pwd.getpwuid(os.getuid())
        cmd = ["CST_PROG=cst toolchains", "python3", "-c", "a b", "--x", ""]
        r = self.run_bash('cst_as_user "$(id -un)" "CST_PROG=cst toolchains" python3 -c "a b" --x ""')
        self.assertEqual(r.returncode, 0, r.stderr)
        got = [w.decode() for w in r.stdout.split(b"\0")[:-1]]
        self.assertEqual(got, step_down_argv(me.pw_name, me.pw_uid, me.pw_dir, unset=(), then=cmd))

    def test_a_user_without_a_passwd_entry_runs_nothing(self) -> None:
        r = self.run_bash("cst_as_user no-such-user-cst-test true")
        self.assertEqual((r.returncode, r.stdout), (1, b""))
        self.assertIn(b"no home folder for user 'no-such-user-cst-test'", r.stderr)


# The cst-install.bash functions (and btrfs-setup.sh's check) that take a component, and how many of
# their first arguments come before it (None: every argument is one; --component C anywhere too).
_COMPONENT_CALLS: dict[str, int | None] = {
    "cst_mark": 0,
    "cst_mark_as": 1,
    "cst_component_units": 0,
    "cst_install_text": 3,
    "cst_install_text_plan": 3,
    "check_component_syntax": None,
}
_SHELL_END = ("|", ";", "&", "<", ">", ")")


_REDIRECTION = re.compile(r"[0-9]*[<>]|&>")


def _has_unquoted_comment(text: str) -> bool:
    """Whether text holds a # that bash reads as starting a comment (unquoted, at start or after delimiter)."""
    quote, escaped, word_start = "", False, True
    for ch in text:
        if escaped:
            escaped, word_start = False, False
        elif ch == "\\" and quote != "'":
            escaped, word_start = True, False
        elif quote:
            quote = "" if ch == quote else quote
            word_start = False
        elif ch in ("'", '"'):
            quote, word_start = ch, False
        elif ch == "#" and word_start:
            return True
        else:
            word_start = ch.isspace() or ch in ";|&()<>"
    return False


def _before_redirection(word: str) -> tuple[str, bool]:
    """The part of a word before a redirection glued to it (`C>/dev/null`, `C&>/dev/null`: C), and whether
    one was there."""
    cut = min((i for i in (word.find("<"), word.find(">")) if i >= 0), default=-1)
    if cut < 0:
        return word, False
    return word[: cut - 1 if word[cut - 1 : cut] == "&" else cut], True


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(number of its first line, text) of each command line, a line ending in an odd number of
    backslashes joined with the next one, unless those backslashes are in an unquoted comment."""
    out: list[tuple[int, str]] = []
    first, parts = 0, []
    for number, line in enumerate(text.splitlines(), 1):
        if not parts:
            first = number
        if (len(line) - len(line.rstrip("\\"))) % 2 == 1 and not _has_unquoted_comment("".join([*parts, line])):
            parts.append(line[:-1])
            continue
        out.append((first, " ".join([*parts, line])))
        parts = []
    if parts:
        out.append((first, " ".join(parts)))
    return out


def _command_component_args(words: Sequence[str], skip: int | None) -> list[str]:
    """The component argument words of one `_COMPONENT_CALLS` invocation."""
    rest: list[str] = []
    for w in words:
        if w.startswith(_SHELL_END) or w == "||" or _REDIRECTION.match(w):
            break
        head, redirected = _before_redirection(w)
        if redirected:  # `cst_mark C>/dev/null` or `cst_mark C;>/dev/null`: C, then end of args
            cleaned = head.split(";", 1)[0]
            if cleaned:
                rest.append(cleaned)
            break
        if ";" in w:  # `cst_mark C; fi`: the command ends there
            cleaned = w.split(";", 1)[0]
            if cleaned:
                rest.append(cleaned)
            break
        rest.append(w)
    return rest if skip is None else rest[skip : skip + 1]


def shell_component_words(text: str) -> list[tuple[int, str]]:
    """(line, word) of every component a shell text names as an argument: after --component, and in
    the component position of _COMPONENT_CALLS. Comment lines and words with a $ (variables) are none."""
    found: list[tuple[int, str]] = []
    for number, line in _logical_lines(text):
        if line.lstrip().startswith("#"):
            continue
        words = line.split()
        for i, word in enumerate(words):
            args: list[str] = []
            if word.startswith("--component="):
                args = [_before_redirection(word.removeprefix("--component="))[0].split(";", 1)[0]]
            elif word == "--component" and i + 1 < len(words):
                args = [_before_redirection(words[i + 1])[0].split(";", 1)[0]]
            elif word in _COMPONENT_CALLS:
                args = _command_component_args(words[i + 1 :], _COMPONENT_CALLS[word])
            found += [(number, a.strip("\"'")) for a in args if a and "$" not in a]
    return found


class ComponentTableTest(unittest.TestCase):
    """Component and the word table of the commands that install each component (_COMMANDS) name the
    same components: COMPONENTS is built from Component, and a row with another word would be ignored."""

    def test_every_member_has_one_command_row_and_no_row_is_extra(self) -> None:
        self.assertEqual(sorted(install_domain._COMMANDS), sorted(str(c) for c in Component))
        self.assertEqual(list(COMPONENTS), list(Component))


class ShellComponentWordsTest(unittest.TestCase):
    """Every component a shell setup script names (cst_install --component C, cst_mark C,
    cst_install_text DEST MODE OWNER C, ...) is a word of Component: a misspelt one would be a usage
    error at run time, or a record line under a component no command refreshes."""

    def test_every_component_word_of_the_shell_files_is_a_component(self) -> None:
        words = {str(c) for c in Component}
        seen: set[str] = set()
        for rel in shell_files():
            if is_test_file(rel):
                continue
            text = Path(REPO, rel).read_text(encoding="utf-8", errors="replace")
            for number, word in shell_component_words(text):
                seen.add(word)
                with self.subTest(file=rel, line=number):
                    self.assertIn(word, words)
        # The gate reads the calls it is meant for: the scripts name these components.
        self.assertLessEqual(
            {"btrfs:core", "btrfs:alerts", "btrfs:degraded-boot", "btrfs:report-launcher", "btrfs:snapshots"}, seen
        )

    def test_the_words_are_found_where_the_calls_take_them(self) -> None:
        text = (
            "cst_install --component btrfs:core\n"
            'cst_install_text "${f}" 0644 root btrfs:degraded-boot <"${x}"\n'
            'cst_install_text "${SNAP_CONF}" 0644 <"${tmp}" || rc=$?\n'
            'cst_mark_as "${user}" btrfs:report-launcher\n'
            "check_component_syntax btrfs:alerts btrfs:cor 2>/dev/null\n"
            'cst_install_text "${f}" 0644 \\\n'
            "    root btrfs:degraded-boo\n"
            "cst_installer dests --component=btrfs:snapshot\n"
            "# cst_mark btrfs:nope (a comment)\n"
            'cst_mark "$1"\n'
        )
        self.assertEqual(
            shell_component_words(text),
            [
                (1, "btrfs:core"),
                (2, "btrfs:degraded-boot"),
                (4, "btrfs:report-launcher"),
                (5, "btrfs:alerts"),
                (5, "btrfs:cor"),
                (6, "btrfs:degraded-boo"),
                (8, "btrfs:snapshot"),
            ],
        )
        # A planted misspelt word is reported by the gate's check.
        self.assertNotIn("btrfs:cor", {str(c) for c in Component})

    def test_a_comment_does_not_continue_and_a_glued_redirection_ends_the_word(self) -> None:
        # bash: a backslash at the end of a comment continues nothing, and `C>/dev/null` is the word C
        # with a redirection after it.
        text = (
            "# see cst_mark \\\n"
            "cst_mark btrfs:snapshot\n"
            "cst_install_text a 0644 root \\\n"
            "    btrfs:core # a note \\\n"
            "cst_mark btrfs:alerts\n"
            "cst_mark btrfs:cor>/dev/null\n"
            "check_component_syntax btrfs:core btrfs:x2>/dev/null 2>&1\n"
            "cst_mark btrfs:y&>/dev/null\n"
            "cst_installer dests --component=btrfs:z>/dev/null\n"
        )
        self.assertEqual(
            shell_component_words(text),
            [
                (2, "btrfs:snapshot"),
                (3, "btrfs:core"),
                (5, "btrfs:alerts"),
                (6, "btrfs:cor"),
                (7, "btrfs:core"),
                (7, "btrfs:x2"),
                (8, "btrfs:y"),
                (9, "btrfs:z"),
            ],
        )

    def test_quoted_hash_continues_line_and_semicolon_before_redirection_ends_word(self) -> None:
        self.assertEqual(
            _logical_lines(
                "echo \" # \" \\\nmore\necho ' # ' \\\nmore\n# comment \\\nnext\necho a;# comment \\\nnext\necho foo \\\\\ncst_mark btrfs:alerts\n"
            ),
            [
                (1, 'echo " # "  more'),
                (3, "echo ' # '  more"),
                (5, "# comment \\"),
                (6, "next"),
                (7, "echo a;# comment \\"),
                (8, "next"),
                (9, "echo foo \\\\"),
                (10, "cst_mark btrfs:alerts"),
            ],
        )
        text = (
            'echo " # "; cst_install_text a 0644 \\\n'
            "    root btrfs:core\n"
            "echo ' # '; cst_install_text b 0644 \\\n"
            "    root btrfs:alerts\n"
            "cst_install_text e 0644 \\#notacomment \\\n"
            "    btrfs:snapshot\n"
            "echo a;# note \\\n"
            "echo foo \\\\\n"
            "# cst_mark btrfs:bogus\n"
            "cst_mark btrfs:core;>/dev/null extra\n"
            "cst_mark btrfs:alerts;2>/dev/null extra\n"
            "cst_installer dests --component=btrfs:z;>/dev/null\n"
            "cst_installer dests --component btrfs:q;>/dev/null\n"
            "cst_mark btrfs:core;fi\n"
        )
        self.assertEqual(
            shell_component_words(text),
            [
                (1, "btrfs:core"),
                (3, "btrfs:alerts"),
                (5, "btrfs:snapshot"),
                (10, "btrfs:core"),
                (11, "btrfs:alerts"),
                (12, "btrfs:z"),
                (13, "btrfs:q"),
                (14, "btrfs:core"),
            ],
        )

    def test_a_comment_after_a_shell_delimiter_continues_no_line(self) -> None:
        # bash starts a comment at a # right after ; | & ( ) as after a blank, and a backslash ending
        # that comment continues nothing: the next line is a command of its own.
        text = (
            "echo a;# note \\\n"
            "cst_mark btrfs:core\n"
            "echo b|# note \\\n"
            "cst_mark btrfs:alerts\n"
            "(true)# note \\\n"
            "cst_mark btrfs:snapshot\n"
            "true&# note \\\n"
            "cst_mark btrfs:z\n"
        )
        self.assertEqual(
            shell_component_words(text),
            [(2, "btrfs:core"), (4, "btrfs:alerts"), (6, "btrfs:snapshot"), (8, "btrfs:z")],
        )

    def test_only_an_odd_run_of_trailing_backslashes_continues_the_line(self) -> None:
        # bash: \\ at the end of a line is one literal backslash, so the line ends there; \\\ is a
        # literal backslash and then a continuation.
        text = "echo foo \\\\\ncst_mark btrfs:core\necho foo \\\\\\\ncst_mark btrfs:alerts\n"
        self.assertEqual(
            _logical_lines(text),
            [(1, "echo foo \\\\"), (2, "cst_mark btrfs:core"), (3, "echo foo \\\\ cst_mark btrfs:alerts")],
        )
        self.assertEqual(shell_component_words(text), [(2, "btrfs:core"), (3, "btrfs:alerts")])


if __name__ == "__main__":
    unittest.main()
