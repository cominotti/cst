"""Unit tests for installing manifest files: zipapps, the install use case, the
installer CLI, the bash library and the runtime resolver.

Everything is written under temporary folders (a staging root): nothing on
the machine is touched, no sudo, no runuser (a stub stands in for it).
"""

from collections.abc import Mapping
import contextlib
import io
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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
from cst_core.domain.install import (
    INSTALL_MANIFEST,
    ArtifactKind,
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
from cst_core.domain.values import DomainValidationError, OctalMode
from cst_core.installer import main as installer_main
from cst_core.ports import WriteFailed

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "cst_core", "installer.py")
LIB = os.path.join(REPO, "cst-install.bash")
SYSTEM_PYTHON = "/usr/bin/python3"


def entry(
    src: str, dest: str, *, mode: int = 0o644, kind: ArtifactKind = "data", owner: Owner = "root"
) -> InstallEntry:
    return InstallEntry(src=src, dest=dest, mode=OctalMode(mode), owner=owner, kind=kind, runner=owner)


class _MemoryTarget:
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
        self.assertTrue(is_current(file_, b"x", 0o644, "root", as_root=True))
        self.assertFalse(is_current(file_, b"x", 0o600, "root", as_root=True))
        self.assertFalse(is_current(None, b"x", 0o644, "user", as_root=False))
        self.assertFalse(is_current(OnDisk(None, 0, -1, -1, True), b"x", 0o644, "user", as_root=False))
        # One link rule: replaced at a root destination, read through at a user destination.
        self.assertFalse(is_current(link, b"x", 0o644, "root", as_root=True))
        self.assertFalse(is_current(link, b"x", 0o644, "root", as_root=False))
        self.assertTrue(is_current(link, b"x", 0o644, "user", as_root=False))
        # As root, a root entry must be owned by 0:0; a staged run as a user cannot check it.
        foreign = OnDisk(b"x", 0o644, 1000, 1000, False)
        self.assertFalse(is_current(foreign, b"x", 0o644, "root", as_root=True))
        self.assertTrue(is_current(foreign, b"x", 0o644, "root", as_root=False))
        self.assertTrue(is_current(foreign, b"x", 0o644, "user", as_root=True))

    def test_expand_dest(self) -> None:
        self.assertEqual(expand_dest("~/.config/x", "/home/u/"), "/home/u/.config/x")
        self.assertEqual(expand_dest("/etc/x", None), "/etc/x")
        self.assertEqual(expand_dest("~", "/home/u"), "/home/u")  # the home itself
        self.assertEqual(expand_dest("~x/y", "/home/u"), "~x/y")  # another user's ~: not expanded
        with self.assertRaises(ValueError):
            expand_dest("~/.config/x", None)

    def test_select_entries_by_module_and_kind(self) -> None:
        cpu = select_entries(INSTALL_MANIFEST, module="cpu", kind="python-entry")
        self.assertEqual({e.src for e in cpu}, {"cpu/vm-pin", "cpu/cpu-watch"})
        self.assertTrue(all(e.module == "cpu" for e in select_entries(INSTALL_MANIFEST, module="cpu")))

    def test_parse_manifest_reads_rows_and_skips_comments(self) -> None:
        table = "# header\n\na/b  /usr/local/bin/b  755 root python-entry user  # why\nc/d ~/.x 644 user data user\n"
        self.assertEqual(
            parse_manifest(table),
            (
                InstallEntry("a/b", "/usr/local/bin/b", OctalMode(0o755), "root", "python-entry", "user"),
                InstallEntry("c/d", "~/.x", OctalMode(0o644), "user", "data", "user"),
            ),
        )

    def test_parse_manifest_rejects_a_malformed_row(self) -> None:
        for row in (
            "a/b /x 755 root python-entry",
            "a/b /x 9z root data root",
            "a/b /x 644 admin data root",
            "a/b /x 644 root binary root",
            "a/b /x 644 root data root extra",
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
    def setUp(self) -> None:
        self.payloads = {"m/a": b"A\n", "m/t.in": b"uuid=@UUID@\n", "m/u": b"U\n"}
        self.a = entry("m/a", "/etc/a")
        self.t = entry("m/t.in", "/etc/t")
        self.u = entry("m/u", "~/.config/u", owner="user")
        self.target = _MemoryTarget()
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

    def test_entry_payload_copies_other_kinds(self) -> None:
        e = entry("demo/data.conf", "/etc/x")
        self.assertEqual(entry_payload(self.root, e), b"KEY=1\n")


class LocalTargetTest(unittest.TestCase):
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
        self.assertEqual(service.write_if_changed(link, b"x\n", 0o644, "user"), f"unchanged: {link}")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(service.write_if_changed(link, b"y\n", 0o644, "user"), f"written:   {link}")
        self.assertFalse(os.path.islink(link), "a write replaces the link, never writes through it")
        self.assertEqual(Path(real).read_bytes(), b"x\n")

    def test_a_link_at_a_root_destination_is_replaced_even_with_the_wanted_content(self) -> None:
        real = os.path.join(self.tmp, "real")
        Path(real).write_bytes(b"x\n")
        os.chmod(real, 0o644)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        service = InstallService(self.t, lambda _e: b"")
        self.assertEqual(service.write_if_changed(link, b"x\n", 0o644, "root"), f"written:   {link}")
        self.assertFalse(os.path.islink(link))
        self.assertEqual(service.write_if_changed(link, b"x\n", 0o644, "root"), f"unchanged: {link}")
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

    def setUp(self) -> None:
        self.stage = tempfile.mkdtemp(prefix="cst-installer-cli-")
        self.addCleanup(shutil.rmtree, self.stage)

    def cli(self, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return _run([SYSTEM_PYTHON, "-I", "-B", INSTALLER, *args], stdin=stdin)

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
        self.assertEqual(plan.stdout.decode().count("would write: "), n)
        self.assertEqual(self.cli("check", *sel).returncode, 1)

        first = self.cli("install", *sel)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.decode().count("written:   "), n)
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
        self.assertEqual(r.stdout.decode().split(), ["/usr/local/sbin/cpu-vm-pin", "/usr/local/bin/cpu-watch"])

    def test_user_files_are_written_through_runuser_with_the_users_home(self) -> None:
        r = self.bash(f'cst_install_as alice --root "{self.stage}" cpu/gamemode.ini')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode(), f"written:   {self.stage}/home/alice/.config/gamemode.ini\n")
        self.assertIn("runuser -u alice -- /usr/bin/python3 -I -B", Path(self.log).read_text())
        r = self.bash(f'cst_install_text "{self.stage}/u" 0600 alice', stdin=b"y\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(self.log).read_text().count("runuser -u alice"), 2)
        r = self.bash("cst_install_as nobody-here cpu/gamemode.ini")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"no home folder", r.stderr)

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


if __name__ == "__main__":
    unittest.main()
