"""Unit tests for btrfs/btrfs-bulk (no root, no btrfs needed).

HOME is a temporary folder; subvolumes, NOCOW flags, the busy check and
flatpak are fakes (the real ones need btrfs, root and the host). The rest is
the real code: classification, the copy (cp), the switch, the journal and
the recovery of interrupted runs.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s btrfs/tests -v
or directly:
    python3 -I -B btrfs/tests/test_btrfs_bulk.py
"""

from __future__ import annotations

import contextlib
import io
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, cast
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_btrfs_bulk.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.bulk import adapters as bulk_adapters, cli as bulk_cli, domain as bulk_domain, items as bulk_items
from btrfs.bulk import steam as bulk_steam
from tests.package_view import PackageView

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "btrfs-bulk")


class _ItemProto(Protocol):
    id: str
    link: str | None
    target: str
    nocow: bool


class _CtxProto(Protocol):
    home: str
    bulk: str


CtxProto = _CtxProto


class _CopyProto(Protocol):
    def deepcopy[T](self, x: T) -> T: ...


class _BtrfsBulkModule(Protocol):
    copy: _CopyProto
    ETC: str
    CONF: str
    FLAG: str
    KEEP: str
    CONVERT_PLAN: str
    STATE: str
    LOCK: str
    NOTIFY: str
    BIG: int
    MARGIN: int
    PROGRESS_EVERY: int
    BTRFS_MAGIC: int
    PROC: str
    RUN_USER: str
    SNAPSHOT: str | None
    STEAM_ROOT: str
    ROOT: bool
    user_ctx: Callable[[Sequence[str]], _CtxProto]
    NOCOW_TOP: str
    ITEMS: Sequence[_ItemProto]
    BY_ID: Mapping[str, _ItemProto]
    Unexpected: type[Exception]
    Ctx: Callable[[str], _CtxProto]
    is_subvol: Callable[[str], bool]
    subvol_create: Callable[[str], None]
    is_nocow: Callable[[str], bool]
    set_nocow: Callable[[str], None]
    copy_tree: Callable[[str, str, bool], None]
    busy_paths: Callable[[Iterable[str], str], set[str]]
    mounts: Callable[[], Sequence[object]]
    free_bytes: Callable[[str], int]
    flatpak_installed: Callable[[str], bool]
    flatpak_has_fs: Callable[[str, str], bool]
    flatpak_add_fs: Callable[[str, str], None]
    convert_bulk_dir: Callable[[_CtxProto], bool]
    apply: Callable[[_CtxProto], tuple[list[str], list[str]]]
    report: Callable[[_CtxProto, str], int]
    boot: Callable[[_CtxProto], int]
    alert: Callable[[str, str], object]
    classify: Callable[[_CtxProto, _ItemProto, set[str]], tuple[str, str]]
    cow_data: Callable[[str], tuple[int, bool]]
    nocow_parents: Callable[[_CtxProto, _ItemProto], list[str]]
    exit_status: Callable[[Sequence[str], Sequence[str]], int]
    journals: Callable[[], list[str]]
    jpath: Callable[[str], str]
    jread: Callable[[str], dict[str, Any]]
    jwrite: Callable[[str, dict[str, Any]], None]
    u_prepare: Callable[[str, str, bool], object]
    u_late_write: Callable[[dict[str, Any]], bool]
    u_finish: Callable[[dict[str, Any]], None]
    u_rollback: Callable[[dict[str, Any]], None]
    u_move_subvol: Callable[[str, str], None]
    u_rmtree: Callable[[str], None]
    _fs_magic: Callable[[str], int]
    steam_running: Callable[[_CtxProto], list[str]]
    copy_reflink: Callable[[str, str], None]
    lf_files: Callable[[str], list[str]]
    lf_index: Callable[[dict[str, Any], str], str | None]
    lf_validate: Callable[[dict[str, Any]], None]
    lf_with_library: Callable[[dict[str, Any], str, str], dict[str, Any]]
    lf_place_app: Callable[[dict[str, Any], str, str, str], dict[str, Any]]
    vdf_dump: Callable[[dict[str, Any]], bytes]
    vdf_load: Callable[[bytes], dict[str, Any]]
    vdf_parse: Callable[[bytes], dict[str, Any]]
    write_vdf_pair: Callable[[_CtxProto, dict[str, Any], dict[str, Any]], None]
    steam_read_pair: Callable[[_CtxProto], dict[str, Any]]
    steam_apply: Callable[..., int]
    steam_cmd: Callable[[list[str]], int]
    steam_print: Callable[[_CtxProto, dict[str, Any]], None]
    steam_survey: Callable[[_CtxProto, list[str], bool], dict[str, Any]]
    steam_copy_floor: Callable[[], int]
    snapshot_guard: Callable[[], dict[str, int]]
    u_steam_drop_news: Callable[[dict[str, Any]], bool]
    u_steam_late: Callable[[dict[str, Any]], bool]
    u_steam_switch: Callable[[dict[str, Any]], object]
    u_verify_content: Callable[[str, str, str, int], str]
    u_write_marker: Callable[[str, str], None]
    u_clock: Callable[[str], object]
    copy_bytes: Callable[[str, str, str, int], None]
    edit_keep: Callable[[Sequence[str], Iterable[str]], object]
    recover: Callable[[_CtxProto], list[str]]
    CHUNK: int
    PROGRESS_BYTES: int
    same_file: Callable[..., bool]
    user_ctx: Callable[[Sequence[str]], _CtxProto]
    u_steam_finish: Callable[[dict[str, Any]], None]


def load() -> _BtrfsBulkModule:
    """btrfs-bulk's package as one namespace, back to its real code (the
    program is a thin entry over it; a new view undoes the last test's fakes)."""
    view = PackageView(bulk_cli, bulk_items, bulk_steam, bulk_adapters, bulk_domain)
    # What a fresh run starts with: no ids from an earlier apply(), and the
    # checkout's btrfs-bulk as the running program (btrfs-snapshot next to it).
    bulk_domain.BULK_ONLY.clear()
    bulk_adapters.Program.entry_file = TOOL
    return cast("_BtrfsBulkModule", cast("object", view))


class Crash(BaseException):
    """Simulated kill/power loss: not caught by the tool's error handling."""


class BulkBase(unittest.TestCase):
    """Scratch home and fakes (shared with test_btrfs_steam.py); no tests here."""

    def setUp(self) -> None:
        self.m = load()
        self.out: str = ""
        self.tmp = tempfile.mkdtemp(prefix="bulk-test-")
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        etc = os.path.join(self.tmp, "etc")
        os.mkdir(etc)
        m = self.m
        m.ETC = etc
        m.CONF = os.path.join(etc, "bulk.conf")
        m.FLAG = os.path.join(etc, "bulk-at-boot")
        m.KEEP = os.path.join(etc, "bulk-keep")
        m.CONVERT_PLAN = os.path.join(etc, "convert-plan")
        m.STATE = os.path.join(self.tmp, "state")
        m.LOCK = os.path.join(self.tmp, "lock")
        m.NOTIFY = os.path.join(self.tmp, "no-notify")
        # Fakes for what needs btrfs, root or the host.
        self.subvols: set[tuple[int, int]] = set()  # (dev, ino) of fake subvolumes
        self.nocow: set[tuple[int, int]] = set()  # (dev, ino) with +C
        self.busy: set[str] = set()
        self.flatpaks: dict[str, set[str]] = {}  # app -> set of filesystem overrides
        self.convert_calls: list[str] = []

        def key(p: str) -> tuple[int, int]:
            st = os.lstat(p)
            return st.st_dev, st.st_ino

        def is_subvol(p: str) -> bool:
            try:
                return stat.S_ISDIR(os.lstat(p).st_mode) and key(p) in self.subvols
            except OSError:
                return False

        def subvol_create(p: str) -> None:
            os.mkdir(p, 0o755)
            self.subvols.add(key(p))

        def is_nocow(p: str) -> bool:
            return key(p) in self.nocow

        def set_nocow(p: str) -> None:
            self.nocow.add(key(p))

        real_copy = m.copy_tree

        def copy_tree(src: str, dst: str, nocow: bool) -> None:
            real_copy(src, dst, nocow)
            if nocow:  # btrfs: new files inherit +C from their folder
                for root, dirs, files in os.walk(dst):
                    for n in dirs + files:
                        self.nocow.add(key(os.path.join(root, n)))

        def busy_paths(paths: Iterable[str], home: str) -> set[str]:
            return {
                p for p in paths if any(p == b or p.startswith(b + "/") or b.startswith(p + "/") for b in self.busy)
            }

        m.is_subvol = is_subvol
        m.subvol_create = subvol_create
        m.is_nocow = is_nocow
        m.set_nocow = set_nocow
        m.copy_tree = copy_tree
        m.busy_paths = busy_paths
        m.mounts = lambda: []
        m.free_bytes = lambda path: 10**15
        m.flatpak_installed = lambda app: app in self.flatpaks
        m.flatpak_has_fs = lambda app, path: path in self.flatpaks.get(app, set())
        m.flatpak_add_fs = lambda app, path: self.flatpaks[app].add(path)
        m.convert_bulk_dir = lambda ctx: self.convert_calls.append(ctx.bulk) or False
        self.ctx = m.Ctx(self.home)

    def tearDown(self) -> None:
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        shutil.rmtree(self.tmp)

    # ---- helpers ----
    def H(self, rel: str) -> str:
        return os.path.join(self.home, rel)

    def B(self, rel: str = "") -> str:
        return os.path.join(self.home, "Bulk", rel) if rel else os.path.join(self.home, "Bulk")

    def write(self, rel: str, data: str = "x") -> str:
        p = self.H(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
        return p

    def read(self, rel: str) -> str:
        with open(self.H(rel)) as f:
            return f.read()

    def purge_tree(self, path: str) -> None:
        # Clear fake inode sets before deleting so recycled inodes on ext4/tmpfs do not look +C.
        if os.path.exists(path):
            st = os.lstat(path)
            self.nocow.discard((st.st_dev, st.st_ino))
            self.subvols.discard((st.st_dev, st.st_ino))
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    st = os.lstat(os.path.join(root, n))
                    self.nocow.discard((st.st_dev, st.st_ino))
                    self.subvols.discard((st.st_dev, st.st_ino))
            shutil.rmtree(path)

    def apply(self) -> tuple[list[str], list[str]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = self.m.apply(self.ctx)
        self.out = out.getvalue()
        return res

    def report(self, mode: str = "status") -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            left = self.m.report(self.ctx, mode)
        return left, out.getvalue()

    def tree(self) -> list[tuple[str, str, str | bytes | int]]:
        """Every path below the temp dir with its type, link target and content."""
        out: list[tuple[str, str, str | bytes | int]] = []
        for root, dirs, files in os.walk(self.tmp):
            for n in sorted(dirs + files):
                p = os.path.join(root, n)
                if p == self.m.LOCK:
                    continue
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    out.append((p, "link", os.readlink(p)))
                elif stat.S_ISREG(st.st_mode):
                    with open(p, "rb") as f:
                        out.append((p, "file", f.read()))
                else:
                    out.append((p, "dir", stat.S_IMODE(st.st_mode)))
        return sorted(out)

    def assertLink(self, rel: str, target: str) -> None:
        p = self.H(rel)
        self.assertTrue(os.path.islink(p), f"{rel} is not a symlink")
        self.assertEqual(os.readlink(p), self.B(target))

    def journals(self) -> list[str]:
        return [f for f in os.listdir(self.m.STATE) if f.startswith("journal-")] if os.path.isdir(self.m.STATE) else []


class BulkTest(BulkBase):
    def test_btrfs_snapshot_is_found_from_the_running_entry(self) -> None:
        # main() names the entry point; from a checkout, btrfs-snapshot is the one next to it.
        snapshot = os.path.join(os.path.dirname(TOOL), "btrfs-snapshot")
        bulk_adapters.Program.entry_file = ""
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bulk_cli.main(["btrfs-bulk", "bogus"], entry_file=TOOL), 2)
        self.assertEqual(bulk_adapters.Program.entry_file, TOOL)
        self.assertEqual(bulk_adapters.snapshot_program(), snapshot)
        # BTRFS_BULK_SNAPSHOT (SNAPSHOT, not as root) wins.
        self.m.SNAPSHOT = "/elsewhere/btrfs-snapshot"
        self.assertEqual(bulk_adapters.snapshot_program(), "/elsewhere/btrfs-snapshot")
        # Never a guess: without an entry point it refuses.
        self.m.SNAPSHOT = None
        bulk_adapters.Program.entry_file = ""
        with self.assertRaises(RuntimeError):
            bulk_adapters.snapshot_program()

    def test_fresh_home_creates_layout_and_is_idempotent(self) -> None:
        os.makedirs(self.H("Downloads"))
        deferred, problems = self.apply()
        self.assertEqual((deferred, problems), ([], []))
        self.assertTrue(self.m.is_subvol(self.B()))
        with open(self.B("CACHEDIR.TAG")) as f:
            self.assertTrue(f.read().startswith("Signature: 8a477f597d28d172789f06886806bc55\n"))
        for d in (
            "apps",
            "apps/jetbrains",
            "models",
            "toolchains/android/sdk",
            "emulators/android/avd",
            "iso",
            "steam",
            "toolchains/jvm/m2/repository",
            "toolchains/go/pkg/mod",
            "toolchains/rust/rustup/toolchains",
            "toolchains/rust/rustup/tmp",
            "toolchains/rust/rustup/downloads",
            "toolchains/rust/cargo/registry",
            "toolchains/rust/cargo/git",
            "toolchains/jvm/gradle/caches",
            "toolchains/jvm/gradle/wrapper",
            "toolchains/node/npm/_cacache",
        ):
            self.assertTrue(os.path.isdir(self.B(d)), d)
        # Parent there -> link; parent absent (tool not installed) -> folder only.
        self.assertLink("Programs", "apps")
        self.assertLink("Downloads/iso", "iso")
        self.assertFalse(os.path.lexists(self.H(".m2")))
        self.assertFalse(os.path.lexists(self.H(".android")))
        self.assertFalse(os.path.lexists(self.H(".npm")))
        # emulators/ and everything below it are +C; nothing else in ~/Bulk is.
        for d in ("emulators", "emulators/android", "emulators/android/avd"):
            self.assertTrue(self.m.is_nocow(self.B(d)), d)
        for d in ("", "toolchains", "toolchains/android", "toolchains/android/sdk", "apps", "models"):
            self.assertFalse(self.m.is_nocow(self.B(d)), d)
        self.assertFalse(os.path.lexists(self.B("android")))
        before = self.tree()
        self.assertEqual(self.apply(), ([], []))
        self.assertEqual(self.tree(), before, "second apply changed something")
        self.assertNotIn("done:", self.out)
        left, text = self.report()
        self.assertEqual(left, 0, text)

    def test_tool_installed_later_gets_its_link_on_rerun(self) -> None:
        self.apply()
        self.assertFalse(os.path.lexists(self.H(".cargo/registry")))
        self.write(".cargo/config.toml", "[build]\n")
        self.write(".cargo/registry/index/a", "crate")
        self.apply()
        self.assertLink(".cargo/registry", "toolchains/rust/cargo/registry")
        self.assertEqual(self.read(".cargo/config.toml"), "[build]\n")  # config stays
        self.assertEqual(self.read(".cargo/registry/index/a"), "crate")  # through the link

    def test_moves_data_and_keeps_config(self) -> None:
        self.write("Programs/App/app.bin", "binary")
        self.write(".local/share/JetBrains/Toolbox/apps/idea/bin/idea.sh", "#!/bin/sh")
        self.write(".local/share/JetBrains/Toolbox/.settings.json", "{}")
        self.write(".m2/repository/org/x.jar", "jar")
        self.write(".m2/settings.xml", "<settings/>")
        self.write("go/pkg/mod/example.com/m@v1/go.mod", "module m")
        self.write("go/bin/tool", "bin")
        # Go makes its module cache read-only.
        for root, _dirs, files in os.walk(self.H("go/pkg/mod"), topdown=False):
            for f in files:
                os.chmod(os.path.join(root, f), 0o444)
            os.chmod(root, 0o555)
        self.write(".rustup/toolchains/stable/bin/rustc", "rustc")
        self.write(".rustup/settings.toml", "default")
        os.makedirs(self.H(".rustup/tmp"))
        self.write(".android/avd/pixel.avd/userdata.img", "disk" * 100)
        self.write(".android/avd/pixel.ini", "path")
        self.write(".var/app/ai.lmstudio.lm-studio/.lmstudio/models/m.gguf", "weights")
        self.write(".var/app/ai.lmstudio.lm-studio/.lmstudio/conversations/c.json", "chat")
        self.write(".npm/_cacache/index-v5/aa/bb", "index")
        self.write(".npm/_logs/debug.log", "log")
        self.write(".npm/_npx/1/package.json", "{}")
        self.flatpaks["ai.lmstudio.lm-studio"] = set()
        self.assertEqual(self.apply(), ([], []))
        self.assertLink("Programs", "apps")
        self.assertLink(".local/share/JetBrains/Toolbox/apps", "apps/jetbrains")
        self.assertLink(".m2/repository", "toolchains/jvm/m2/repository")
        self.assertLink("go/pkg/mod", "toolchains/go/pkg/mod")
        self.assertLink(".rustup/toolchains", "toolchains/rust/rustup/toolchains")
        # rustup renames from tmp into the toolchain: same subvolume.
        self.assertLink(".rustup/tmp", "toolchains/rust/rustup/tmp")
        self.assertLink(".rustup/downloads", "toolchains/rust/rustup/downloads")
        self.assertLink(".android/avd", "emulators/android/avd")
        self.assertLink(".var/app/ai.lmstudio.lm-studio/.lmstudio/models", "models")
        self.assertLink(".npm/_cacache", "toolchains/node/npm/_cacache")
        self.assertEqual(self.read(".npm/_cacache/index-v5/aa/bb"), "index")
        self.assertEqual(self.read("Programs/App/app.bin"), "binary")
        self.assertEqual(self.read(".local/share/JetBrains/Toolbox/apps/idea/bin/idea.sh"), "#!/bin/sh")
        self.assertEqual(self.read("go/pkg/mod/example.com/m@v1/go.mod"), "module m")
        self.assertEqual(self.read(".android/avd/pixel.avd/userdata.img"), "disk" * 100)
        # What stays in /home.
        for rel in (
            ".m2/settings.xml",
            "go/bin/tool",
            ".rustup/settings.toml",
            ".npm/_logs/debug.log",
            ".npm/_npx/1/package.json",
            ".local/share/JetBrains/Toolbox/.settings.json",
            ".var/app/ai.lmstudio.lm-studio/.lmstudio/conversations/c.json",
        ):
            self.assertFalse(os.path.islink(self.H(rel)), rel)
            self.assertTrue(os.path.isfile(self.H(rel)), rel)
        # AVD: every file inherited +C (a real copy); old trees are gone.
        files, dirs_cow = self.m.cow_data(self.B("emulators/android/avd"))
        self.assertEqual((files, dirs_cow), (0, False))
        leftovers = [n for r, d, f in os.walk(self.home) for n in d + f if ".bulk-" in n]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.flatpaks["ai.lmstudio.lm-studio"], {self.B("models")})
        before = self.tree()
        self.assertEqual(self.apply(), ([], []))
        self.assertEqual(self.tree(), before)

    def test_unexpected_states_are_reported_and_untouched(self) -> None:
        os.makedirs(self.H("elsewhere"))
        os.symlink(self.H("elsewhere"), self.H("Programs"))  # link to somewhere else
        self.write("Android/Sdk", "a file")  # file where a folder belongs
        os.makedirs(self.H("real-cargo/registry"))
        os.symlink(self.H("real-cargo"), self.H(".cargo"))  # parent is a symlink
        self.write("Downloads/iso/a.iso", "iso")  # data on both sides
        self.apply()
        self.write("Bulk/iso/b.iso", "other")
        os.unlink(self.H("Downloads/iso"))
        shutil.rmtree(self.H("Bulk/iso"))
        self.write("Downloads/iso/a.iso", "iso")
        self.write("Bulk/iso/b.iso", "other")
        before = {p: v for p, *v in self.tree() if "/Bulk/" not in p or "/Bulk/iso" in p}
        deferred, problems = self.apply()
        self.assertEqual(deferred, [])
        self.assertEqual(sorted(problems), sorted(["android-sdk", "cargo-registry", "cargo-git", "iso", "apps"]))
        after = {p: v for p, *v in self.tree() if "/Bulk/" not in p or "/Bulk/iso" in p}
        self.assertEqual(after, before)
        self.assertIn("NEEDS A LOOK", self.out)
        # Items depending on a problem item wait, and are not touched either.
        self.assertIn("waits:", self.out)
        _, text = self.report()
        self.assertIn("symlink to", text)
        self.assertIn("a file, not a folder", text)
        self.assertIn("data in both", text)

    def test_bulk_is_a_file_stops_everything(self) -> None:
        self.write("Bulk", "oops")
        self.write("Programs/a", "a")
        before = self.tree()
        self.assertEqual(self.apply(), ([], ["bulk"]))
        self.assertEqual(self.tree(), before)

    def test_plain_bulk_folder_is_converted_first_or_deferred(self) -> None:
        os.makedirs(self.B())
        self.write("Programs/a", "a")
        deferred, _ = self.apply()
        self.assertEqual(self.convert_calls, [self.B()])
        self.assertEqual(deferred, ["bulk"])
        with open(self.m.FLAG) as f:
            self.assertEqual(f.read(), "bulk\n")
        self.assertTrue(os.path.isdir(self.H("Programs")) and not os.path.islink(self.H("Programs")))

    def test_busy_item_waits_for_boot_then_moves(self) -> None:
        self.write("Programs/a", "a")
        self.write(".local/share/JetBrains/Toolbox/apps/idea/x", "x")
        self.write(".m2/repository/x.jar", "jar")
        self.busy = {self.H("Programs/a")}
        deferred, problems = self.apply()
        self.assertEqual((deferred, problems), (["apps", "jetbrains"], []))
        with open(self.m.FLAG) as f:
            self.assertEqual(f.read().split(), ["apps", "jetbrains"])
        self.assertFalse(os.path.islink(self.H("Programs")))
        self.assertFalse(os.path.islink(self.H(".local/share/JetBrains/Toolbox/apps")))
        self.assertLink(".m2/repository", "toolchains/jvm/m2/repository")  # others go on
        self.assertFalse(os.path.exists(self.B("apps.bulk-new")))
        _, text = self.report()
        self.assertIn("waits for the next boot", text)
        # At boot nothing is busy.
        self.busy = set()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.m.boot(self.ctx)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertFalse(os.path.exists(self.m.FLAG))
        self.assertLink("Programs", "apps")
        self.assertLink(".local/share/JetBrains/Toolbox/apps", "apps/jetbrains")
        self.assertEqual(self.read(".local/share/JetBrains/Toolbox/apps/idea/x"), "x")

    def test_boot_gives_up_after_two_attempts(self) -> None:
        self.write("Programs/a", "a")
        self.busy = {self.H("Programs")}
        self.apply()
        sent: list[str] = []
        self.m.alert = lambda title, body: sent.append(title)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.boot(self.ctx), 3)  # still busy: waits again
            self.assertEqual(self.m.boot(self.ctx), 3)
            self.assertTrue(os.path.exists(self.m.FLAG))
            self.assertEqual(self.m.boot(self.ctx), 1)  # gave up
        self.assertFalse(os.path.exists(self.m.FLAG))
        self.assertEqual(len(sent), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.boot(self.ctx), 0)  # no flag: nothing to do

    @unittest.skipIf(os.geteuid() == 0, "as root the current user is root, which user_ctx refuses")
    def test_root_reads_bulk_user_with_the_one_settings_reader(self) -> None:
        # A hand-rolled reader took "BULK_USER=" lines only, quotes stripped by hand; a
        # value bash would expand ended in a KeyError traceback from pwd as root.
        me = pwd.getpwuid(os.getuid()).pw_name
        self.m.ROOT = True
        for text in (f"BULK_USER={me}\n", f"# CST\nBULK_USER='{me}'\n", f"BULK_USER = {me}  # who\n"):
            with self.subTest(text=text):
                with open(self.m.CONF, "w") as f:
                    f.write(text)
                self.assertEqual(self.m.user_ctx([]).home, os.path.realpath(pwd.getpwnam(me).pw_dir))
        with open(self.m.CONF, "w") as f:
            f.write("# CST\nBULK_USER=$USER\n")
        with self.assertRaises(SystemExit) as cm:
            self.m.user_ctx([])
        self.assertIn(f"{self.m.CONF} line 2: BULK_USER=$USER: $", str(cm.exception.code))
        os.remove(self.m.CONF)
        with self.assertRaisesRegex(SystemExit, "needs --user NAME"):
            self.m.user_ctx([])

    def test_boot_counter_is_durable_before_the_moves_start(self) -> None:
        # The counter was written with open("w") and no fsync before the moves; a hang
        # or hard reset before the next btrfs commit lost it, and the give-up never tripped.
        self.write("Programs/a", "a")
        self.busy = {self.H("Programs")}
        self.apply()
        events: list[str] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            events.append(f"fsync {os.readlink(f'/proc/self/fd/{fd}')}")
            real_fsync(fd)

        def moves(ctx: object) -> tuple[list[str], list[str]]:
            events.append("apply")
            return ["apps"], []

        self.m.apply = moves
        with mock.patch.object(os, "fsync", fsync), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.boot(self.ctx), 3)
        counter = os.path.join(self.m.STATE, "boot-attempts")
        with open(counter) as f:
            self.assertEqual(f.read(), "1")
        before = events[: events.index("apply")]
        self.assertTrue(any(e.startswith(f"fsync {self.m.STATE}/.boot-attempts.") for e in before), events)
        self.assertIn(f"fsync {self.m.STATE}", before)  # the rename's folder entry
        self.assertEqual(os.stat(counter).st_mode & 0o777, 0o644)

    def test_written_during_switch_is_rolled_back(self) -> None:
        self.write("Programs/a", "a")
        self.m.u_late_write = lambda j: True
        deferred, _ = self.apply()
        self.assertEqual(deferred[0], "apps")
        self.assertFalse(os.path.islink(self.H("Programs")))
        self.assertEqual(self.read("Programs/a"), "a")
        self.assertTrue(os.path.isdir(self.B("apps")))
        self.assertEqual(os.listdir(self.B("apps")), [])
        self.assertEqual(self.journals(), [])
        self.assertEqual([n for n in os.listdir(self.home) if ".bulk-" in n], [])

    def test_written_during_copy_is_retried(self) -> None:
        self.write("Programs/a", "a")
        real = self.m.u_prepare

        def prepare(src: str, new: str, nocow: bool) -> object:
            stamp = real(src, new, nocow)
            self.write("Programs/late", "late")  # a write after the copy started
            return stamp

        self.m.u_prepare = prepare
        deferred, _ = self.apply()
        self.assertEqual(deferred[0], "apps")
        self.assertEqual(self.read("Programs/late"), "late")
        self.assertFalse(os.path.islink(self.H("Programs")))
        self.assertFalse(os.path.exists(self.B("apps.bulk-new")))

    def crash_in(self, name: str, after_calls: int) -> tuple[Callable[..., object], Callable[..., object]]:
        """Make os.<name> raise Crash after `after_calls` successful calls
        inside u_commit/u_finish, like a kill at that instant."""
        real = cast("Callable[..., object]", getattr(os, name))
        calls = {"n": 0}

        def wrapped(*a: object, **k: object) -> object:
            if calls["n"] >= after_calls:
                raise Crash()
            calls["n"] += 1
            return real(*a, **k)

        return real, wrapped

    def run_crash(self, fn_name: str, os_name: str, after_calls: int) -> None:
        m = self.m
        real_fn = cast("Callable[..., object]", getattr(m, fn_name))
        real_os, wrapped = self.crash_in(os_name, after_calls)

        def patched(*a: object) -> object:
            setattr(os, os_name, wrapped)
            try:
                return real_fn(*a)
            finally:
                setattr(os, os_name, real_os)

        setattr(m, fn_name, patched)
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            m.apply(self.ctx)
        setattr(m, fn_name, real_fn)

    def check_recovered(self) -> None:
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertLink("Programs", "apps")
        self.assertEqual(self.read("Programs/a"), "a")
        self.assertEqual(self.read("Programs/sub/b"), "b")
        self.assertEqual(self.journals(), [])
        self.assertEqual([n for r, d, f in os.walk(self.home) for n in d + f if ".bulk-" in n], [])

    def setup_programs(self) -> None:
        self.write("Programs/a", "a")
        self.write("Programs/sub/b", "b")

    def test_crash_after_first_rename_is_undone(self) -> None:
        self.setup_programs()
        self.run_crash("u_commit", "rename", 1)  # Programs -> old done, new -> T not
        self.assertFalse(os.path.lexists(self.H("Programs")))
        self.assertEqual(len(self.journals()), 1)
        self.check_recovered()
        self.assertIn("undid an interrupted move", self.out)

    def test_crash_before_symlink_is_undone(self) -> None:
        self.setup_programs()
        self.run_crash("u_commit", "symlink", 0)  # both renames done, no link
        self.assertFalse(os.path.lexists(self.H("Programs")))
        self.assertTrue(os.path.isfile(self.B("apps/a")))
        self.check_recovered()

    def test_crash_while_deleting_old_tree_is_finished(self) -> None:
        self.setup_programs()
        self.run_crash("u_finish", "unlink", 0)  # switched, old tree not deleted yet
        self.assertLink("Programs", "apps")
        self.assertTrue([n for n in os.listdir(self.home) if ".bulk-old-" in n])
        self.check_recovered()
        self.assertIn("deleted the old copy", self.out)

    def test_crash_during_copy_leaves_no_trace(self) -> None:
        self.setup_programs()
        real = self.m.copy_tree

        def copy(src: str, dst: str, nocow: bool) -> None:
            real(src, dst, nocow)
            raise Crash()

        self.m.copy_tree = copy
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            self.m.apply(self.ctx)
        self.m.copy_tree = real
        self.assertEqual(self.journals(), [])
        self.check_recovered()

    def test_crash_in_nocow_rewrite_is_undone(self) -> None:
        # AVD already linked, but holding copy-on-write files (e.g. an old
        # subvolume renamed in, or +C removed by hand).
        self.write(".android/x", "x")
        self.apply()
        self.write(".android/avd/disk.img", "disk")
        st = os.lstat(self.B("emulators/android/avd/disk.img"))
        self.nocow.discard((st.st_dev, st.st_ino))
        state, detail = self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"})
        self.assertEqual(state, "nocow", detail)
        self.run_crash("u_commit", "rename", 1)
        self.assertFalse(os.path.lexists(self.B("emulators/android/avd")))
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertEqual(self.read(".android/avd/disk.img"), "disk")
        self.assertEqual(self.m.cow_data(self.B("emulators/android/avd")), (0, False))

    def test_old_subvolume_is_renamed_in(self) -> None:
        os.makedirs(self.H("Downloads/iso"))
        self.write("Downloads/iso/f.iso", "iso")
        st = os.lstat(self.H("Downloads/iso"))
        self.subvols.add((st.st_dev, st.st_ino))
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["iso"], set())[0], "move-subvol")
        self.assertEqual(self.apply(), ([], []))
        self.assertLink("Downloads/iso", "iso")
        self.assertTrue(self.m.is_subvol(self.B("iso")))  # same subvolume, now nested in Bulk
        self.assertEqual(self.read("Downloads/iso/f.iso"), "iso")

    def test_old_avd_subvolume_is_copied_into_nocow(self) -> None:
        self.write(".android/avd/disk.img", "disk")
        st = os.lstat(self.H(".android/avd"))
        self.subvols.add((st.st_dev, st.st_ino))
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["avd"], set()), ("move", "subvolume"))
        self.assertEqual(self.apply(), ([], []))
        self.assertLink(".android/avd", "emulators/android/avd")
        self.assertEqual(self.m.cow_data(self.B("emulators/android/avd")), (0, False))

    def unset_nocow(self, rel: str) -> None:
        st = os.lstat(self.B(rel))
        self.nocow.discard((st.st_dev, st.st_ino))

    def test_emulators_parent_without_nocow_is_repaired_in_place(self) -> None:
        # +C belongs to emulators/ and emulators/android too (future emulator
        # disk folders inherit it): a parent that lost it is not "ok".
        self.write(".android/x", "x")
        self.apply()
        self.write(".android/avd/disk.img", "disk")  # btrfs: inherits +C from its folder (the fake needs it set)
        st = os.lstat(self.B("emulators/android/avd/disk.img"))
        self.nocow.add((st.st_dev, st.st_ino))
        self.unset_nocow("emulators")
        self.unset_nocow("emulators/android")
        state, detail = self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"})
        self.assertEqual((state, detail), ("nocow", "folder ~/Bulk/emulators without +C"))
        _, text = self.report()
        self.assertIn("+C (NOCOW): yes; NO on ~/Bulk/emulators, ~/Bulk/emulators/android", text)
        self.assertEqual(self.apply(), ([], []), self.out)
        for d in ("emulators", "emulators/android", "emulators/android/avd"):
            self.assertTrue(self.m.is_nocow(self.B(d)), d)
        self.assertEqual(self.read(".android/avd/disk.img"), "disk")  # no rewrite: data untouched in place
        self.assertEqual(self.journals(), [])
        left, text = self.report()
        self.assertEqual(left, 0, text)
        self.assertIn("+C (NOCOW): yes\n", text)

    def test_avd_without_link_is_still_checked_for_nocow(self) -> None:
        # No ~/.android yet: the AVD folder exists in ~/Bulk without its link.
        # A parent without +C or a copy-on-write file in it is not "ok" there either.
        self.apply()
        self.assertFalse(os.path.lexists(self.H(".android")))
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"}), ("ok", "nolink"))
        self.unset_nocow("emulators")
        self.write("Bulk/emulators/android/avd/disk.img", "old")  # copy-on-write (fake: no +C)
        self.assertEqual(
            self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"}), ("nocow", "1 file(s) still copy-on-write")
        )
        left, text = self.report()
        self.assertGreater(left, 0, text)
        self.assertIn("+C (NOCOW): NO (1 copy-on-write file(s)); NO on ~/Bulk/emulators", text)
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("done:", self.out)
        self.assertTrue(self.m.is_nocow(self.B("emulators")))
        self.assertEqual(self.m.cow_data(self.B("emulators/android/avd")), (0, False))
        self.assertEqual(self.read("Bulk/emulators/android/avd/disk.img"), "old")
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"}), ("ok", "nolink"))
        left, text = self.report()
        self.assertIn("+C (NOCOW): yes\n", text)

    def test_symlinked_emulators_gets_no_nocow_line(self) -> None:
        # ~/Bulk/emulators is a symlink: NEEDS A LOOK, and no +C reading of
        # the folders it points to (outside ~/Bulk).
        self.apply()
        self.purge_tree(self.B("emulators"))
        os.makedirs(self.H("elsewhere/android/avd"))
        os.symlink(self.H("elsewhere"), self.B("emulators"))
        state, detail = self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"})
        self.assertEqual(state, "unexpected")
        _, text = self.report()
        self.assertIn("~/Bulk/emulators is a symlink", text)
        self.assertNotIn("+C (NOCOW)", text)
        self.assertEqual(self.m.nocow_parents(self.ctx, self.m.BY_ID["avd"]), [])

    def test_nocow_rewrite_also_sets_parents(self) -> None:
        self.make_avd_cow()
        self.unset_nocow("emulators")
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertTrue(self.m.is_nocow(self.B("emulators")))
        self.assertEqual(self.m.cow_data(self.B("emulators/android/avd")), (0, False))
        self.assertEqual(self.read(".android/avd/disk.img"), "old")

    def test_existing_cow_emulators_folder_gets_nocow(self) -> None:
        # A ~/Bulk/emulators made by hand before (copy-on-write, with other
        # data): it gets +C (only files created later are affected), nothing moves.
        self.apply()
        self.purge_tree(self.B("emulators"))
        self.write("Bulk/emulators/other/x.img", "x")
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertTrue(self.m.is_nocow(self.B("emulators")))
        self.assertTrue(self.m.is_nocow(self.B("emulators/android/avd")))
        self.assertFalse(self.m.is_nocow(self.B("emulators/other")))  # not ours: left alone
        self.assertEqual(self.read("Bulk/emulators/other/x.img"), "x")

    def test_empty_old_folder_is_replaced_by_link(self) -> None:
        os.makedirs(self.H(".var/app/ai.lmstudio.lm-studio/.lmstudio/models"))
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["models"], set()), ("move", "empty"))
        self.apply()
        self.assertLink(".var/app/ai.lmstudio.lm-studio/.lmstudio/models", "models")
        # LM Studio not installed: no override.
        self.assertEqual(self.flatpaks, {})

    def test_steam_library_and_override(self) -> None:
        self.flatpaks["com.valvesoftware.Steam"] = set()
        self.write(".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/G/g", "game")
        self.apply()
        self.assertTrue(os.path.isdir(self.B("steam")))
        self.assertEqual(self.flatpaks["com.valvesoftware.Steam"], {self.B("steam")})
        # steamapps is never linked or moved.
        self.assertFalse(os.path.islink(self.H(".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps")))
        self.m.BIG = 1
        _, text = self.report()
        self.assertIn("of games here, in /home snapshots", text)

    def test_plan_changes_nothing(self) -> None:
        self.write("Programs/a", "a")
        self.write(".android/avd/d.img", "d")
        before = self.tree()
        left, text = self.report("plan")
        self.assertEqual(self.tree(), before)
        self.assertIn("create (a subvolume, yours)", text)
        self.assertIn("real copy into a +C folder", text)
        self.assertGreater(left, 0)

    def test_convert_plan_drops_bulk_paths(self) -> None:
        with open(self.m.CONVERT_PLAN, "w") as f:
            f.write(
                f"# header\ncopy {self.H('.cache')}\ncopy {self.H('.android/avd')}\n"
                f"copy {self.H('Downloads/iso')}\ncopy {self.H('Workspace')}\n"
            )
        self.apply()
        with open(self.m.CONVERT_PLAN) as f:
            self.assertEqual(f.read(), f"# header\ncopy {self.H('.cache')}\ncopy {self.H('Workspace')}\n")

    def test_rmtree_handles_read_only_trees(self) -> None:
        self.write("ro/a/b", "b")
        os.chmod(self.H("ro/a/b"), 0o444)
        os.chmod(self.H("ro/a"), 0o555)
        os.chmod(self.H("ro"), 0o555)
        self.m.u_rmtree(self.H("ro"))
        self.assertFalse(os.path.lexists(self.H("ro")))

    # ---- review round 2 ----
    def make_avd_cow(self) -> None:
        """AVD linked, but holding a copy-on-write file: state nocow."""
        self.write(".android/x", "x")
        self.apply()
        self.write(".android/avd/disk.img", "old")
        st = os.lstat(self.B("emulators/android/avd/disk.img"))
        self.nocow.discard((st.st_dev, st.st_ino))
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["avd"], {"apps"})[0], "nocow")

    def test_nocow_rewrite_written_during_switch_is_rolled_back(self) -> None:
        self.make_avd_cow()
        m = self.m

        def late(j: dict[str, Any]) -> bool:
            # A still-open fd writes the latest state into the old tree.
            with open(os.path.join(j["old"], "disk.img"), "w") as f:
                f.write("latest")
            return True

        m.u_late_write = late
        deferred, problems = self.apply()
        self.assertEqual((deferred, problems), (["avd"], []), self.out)
        self.assertEqual(self.read(".android/avd/disk.img"), "latest")
        self.assertEqual([n for n in os.listdir(self.B("emulators/android")) if ".bulk-" in n], [])
        self.assertEqual(self.journals(), [])

    def test_written_on_both_sides_keeps_both(self) -> None:
        self.write("Programs/a", "a")
        m = self.m

        def late(j: dict[str, Any]) -> bool:
            self.write("Programs/through-link", "new")  # through the fresh link, into the copy
            with open(os.path.join(j["old"], "a"), "w") as f:
                f.write("late")  # and into the old tree
            return True

        m.u_late_write = late
        deferred, problems = self.apply()
        self.assertEqual(problems, ["apps"], self.out)
        self.assertIn("written to on both sides", self.out)
        old = [n for n in os.listdir(self.home) if n.startswith("Programs.bulk-old-")]
        self.assertEqual(len(old), 1)
        with open(os.path.join(self.home, old[0], "a")) as f:
            self.assertEqual(f.read(), "late")
        self.assertEqual(self.read("Programs/through-link"), "new")
        self.assertEqual(self.journals(), ["journal-apps.json"])
        left, text = self.report()
        self.assertIn("NEEDS A LOOK", text)
        # Still stuck on the next run; resolved once the old tree is deleted.
        m.u_late_write = lambda j: False
        self.assertIn("apps", self.apply()[1])
        shutil.rmtree(os.path.join(self.home, old[0]))
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertEqual(self.journals(), [])

    def test_recreated_old_path_after_crash_is_not_deleted(self) -> None:
        self.setup_programs()
        self.run_crash("u_commit", "rename", 1)  # Programs -> old, power loss
        self.assertTrue(os.path.exists(self.m.FLAG))  # the boot service will run
        os.makedirs(self.H("Programs"))  # an app recreates it
        self.write("Programs/fresh", "fresh")
        deferred, problems = self.apply()
        self.assertIn("apps", problems, self.out)
        self.assertIn("was created again", self.out)
        old = [n for n in os.listdir(self.home) if n.startswith("Programs.bulk-old-")]
        self.assertEqual(len(old), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.home, old[0], "sub/b")))
        self.assertTrue(os.path.isfile(self.B("apps.bulk-new/a")))  # the copy is kept too
        self.assertEqual(self.journals(), ["journal-apps.json"])
        # As the message says: keep what you need, delete the new folder, run again.
        shutil.rmtree(self.H("Programs"))
        self.check_recovered()

    def test_boot_recovers_without_flag(self) -> None:
        self.setup_programs()
        self.run_crash("u_commit", "rename", 1)
        os.remove(self.m.FLAG)  # e.g. an older run's state
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.boot(self.ctx), 0)
        self.assertLink("Programs", "apps")
        self.assertEqual(self.read("Programs/sub/b"), "b")
        self.assertEqual(self.journals(), [])

    def test_failed_recovery_is_a_problem(self) -> None:
        self.setup_programs()
        self.run_crash("u_finish", "unlink", 0)
        real = self.m.u_finish

        def fail(j: dict[str, Any]) -> None:
            raise PermissionError(13, "Permission denied", j["old"])

        self.m.u_finish = fail
        deferred, problems = self.apply()
        self.assertIn("apps", problems)
        self.assertEqual(self.journals(), ["journal-apps.json"])
        left, _ = self.report()
        self.assertGreater(left, 0)
        self.m.u_finish = real
        self.check_recovered()

    def test_entries_not_owned_are_not_moved(self) -> None:
        self.write(".m2/repository/org/x.jar", "jar")
        with mock.patch.object(os, "getuid", return_value=0):  # "every file belongs to someone else"
            deferred, problems = self.apply()
        self.assertIn("m2", problems)
        self.assertIn("sudo chown -R root:", self.out)
        self.assertFalse(os.path.islink(self.H(".m2/repository")))
        self.assertEqual(self.read(".m2/repository/org/x.jar"), "jar")
        self.assertFalse(os.path.exists(self.B("toolchains/jvm/m2/repository.bulk-new")))

    def test_empty_old_folder_next_to_filled_target_gets_the_link(self) -> None:
        self.write(".m2/repository/x.jar", "jar")
        self.apply()
        os.unlink(self.H(".m2/repository"))
        os.makedirs(self.H(".m2/repository"))
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["m2"], set()), ("move", "empty"))
        self.assertEqual(self.apply(), ([], []))
        self.assertEqual(self.read(".m2/repository/x.jar"), "jar")

    def test_convert_plan_with_blank_lines(self) -> None:
        with open(self.m.CONVERT_PLAN, "w") as f:
            f.write(f"# header\n\ncopy {self.H('.android/avd')}\n   \ncopy {self.H('Workspace')}\n")
        self.assertEqual(self.apply(), ([], []), self.out)
        with open(self.m.CONVERT_PLAN) as f:
            self.assertEqual(f.read(), f"# header\n\n   \ncopy {self.H('Workspace')}\n")

    def test_cli_plan_as_user(self) -> None:
        env = dict(
            os.environ,
            BTRFS_BULK_HOME=self.home,
            BTRFS_BULK_ETC=self.m.ETC,
            BTRFS_BULK_STATE=self.m.STATE,
            BTRFS_BULK_LOCK=self.m.LOCK,
        )
        self.write("Programs/a", "a")
        before = self.tree()
        r = subprocess.run(
            [sys.executable, "-I", "-B", TOOL, "plan"], check=False, env=env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("~/Programs -> ~/Bulk/apps", r.stdout)
        self.assertEqual(self.tree(), before)
        r = subprocess.run(
            [sys.executable, "-I", "-B", TOOL, "bogus"], check=False, env=env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 2)

    # ---- review round 3 ----
    def crash_after_late_write(self, through_link: str | None = None) -> None:
        """Something writes into the old tree right after the switch, then a
        kill/power loss hits before the last check finished."""
        m = self.m
        real = m.u_late_write

        def late(j: dict[str, Any]) -> bool:
            names = os.listdir(j["old"])
            name = "disk.img" if "disk.img" in names else "a"
            with open(os.path.join(j["old"], name), "w") as f:
                f.write("late")
            if through_link:
                self.write(through_link, "new")
            raise Crash()

        m.u_late_write = late
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            m.apply(self.ctx)
        m.u_late_write = real
        self.assertEqual(len(self.journals()), 1)

    def test_crash_after_late_write_is_rolled_back(self) -> None:
        self.setup_programs()
        self.crash_after_late_write()
        self.assertLink("Programs", "apps")  # the switch was complete
        self.assertEqual(self.m.jread("apps")["bulk_only"], False)
        self.assertIn("stamp2", self.m.jread("apps"))
        # The repair puts the old tree back (with the late write), then moves it again.
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("the old folder was written to during the switch", self.out)
        self.assertLink("Programs", "apps")
        self.assertEqual(self.read("Programs/a"), "late")
        self.assertEqual(self.read("Programs/sub/b"), "b")
        self.assertEqual(self.journals(), [])
        self.assertEqual([n for r, d, f in os.walk(self.home) for n in d + f if ".bulk-" in n], [])

    def test_crash_after_late_write_in_nocow_rewrite_is_rolled_back(self) -> None:
        self.make_avd_cow()
        self.crash_after_late_write()
        self.assertTrue(self.m.jread("avd")["bulk_only"])
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("the old folder was written to during the switch", self.out)
        self.assertEqual(self.read(".android/avd/disk.img"), "late")
        self.assertEqual(self.m.cow_data(self.B("emulators/android/avd")), (0, False))
        self.assertEqual(self.journals(), [])
        self.assertEqual([n for n in os.listdir(self.B("emulators/android")) if ".bulk-" in n], [])

    def test_crash_after_writes_on_both_sides_keeps_both(self) -> None:
        self.setup_programs()
        self.crash_after_late_write(through_link="Programs/through-link")
        deferred, problems = self.apply()
        self.assertEqual(problems, ["apps"], self.out)
        self.assertIn("written to on both sides", self.out)
        self.assertEqual(self.m.exit_status(deferred, problems), 1)
        old = [n for n in os.listdir(self.home) if n.startswith("Programs.bulk-old-")]
        self.assertEqual(len(old), 1)
        with open(os.path.join(self.home, old[0], "a")) as f:
            self.assertEqual(f.read(), "late")
        self.assertEqual(self.read("Programs/through-link"), "new")
        self.assertTrue(self.m.jread("apps")["conflict"])
        shutil.rmtree(os.path.join(self.home, old[0]))
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertEqual(self.journals(), [])

    def test_crash_with_journal_without_stamp2_keeps_both(self) -> None:
        # A journal of an older version: no stamp2, the copy's side is unknown.
        self.setup_programs()
        self.crash_after_late_write()
        j = self.m.jread("apps")
        del j["stamp2"]
        self.m.jwrite("apps", j)
        deferred, problems = self.apply()
        self.assertEqual(problems, ["apps"], self.out)
        self.assertIn("written to on both sides", self.out)
        self.assertTrue([n for n in os.listdir(self.home) if n.startswith("Programs.bulk-old-")])

    def test_conflict_inside_bulk_does_not_block(self) -> None:
        self.make_avd_cow()
        m = self.m

        def late(j: dict[str, Any]) -> bool:
            with open(os.path.join(j["old"], "disk.img"), "w") as f:
                f.write("late")
            self.write(".android/avd/new.img", "new")  # into the copy, in use now
            return True

        m.u_late_write = late
        deferred, problems = self.apply()
        self.assertEqual(problems, ["avd"], self.out)
        self.assertTrue(m.jread("avd")["bulk_only"])
        self.assertIn("snapshots do not wait for it", self.out)
        self.assertEqual(m.exit_status(deferred, problems), 4)
        self.assertEqual(m.exit_status(["apps"], problems), 4)
        self.assertEqual(m.exit_status([], problems + ["iso"]), 1)
        _, text = self.report()
        self.assertIn("NEEDS A LOOK", text)
        self.assertIn("snapshots do not wait for it", text)

    def keep_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            BTRFS_BULK_HOME=self.home,
            BTRFS_BULK_ETC=self.m.ETC,
            BTRFS_BULK_STATE=self.m.STATE,
            BTRFS_BULK_LOCK=self.m.LOCK,
        )
        return subprocess.run(
            [sys.executable, "-I", "-B", TOOL, *args], check=False, env=env, capture_output=True, text=True
        )

    def test_keep_and_unkeep(self) -> None:
        self.write(".m2/repository/x.jar", "jar")
        r = self.keep_cli("keep", "m2", "bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown item: bogus", r.stderr)
        self.assertIn("npm-cache", r.stderr)  # the list of valid ids
        self.assertFalse(os.path.exists(self.m.KEEP))
        self.assertEqual(self.keep_cli("keep").returncode, 2)
        r = self.keep_cli("keep", "m2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.keep_cli("keep", "m2").returncode, 0)  # twice: one line
        with open(self.m.KEEP) as f:
            self.assertEqual([ln for ln in f.read().splitlines() if not ln.startswith("#")], ["m2"])
        with open(self.m.KEEP, "a") as f:
            f.write("# my note\n  iso   # kept on purpose\n")
        os.makedirs(self.H("Downloads/iso"))
        self.write("Downloads/iso/a.iso", "iso")
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("kept:", self.out)
        self.assertFalse(os.path.islink(self.H(".m2/repository")))
        self.assertFalse(os.path.islink(self.H("Downloads/iso")))
        self.assertEqual(self.read(".m2/repository/x.jar"), "jar")
        left, text = self.report()
        self.assertEqual(left, 0, text)
        self.assertIn("kept in /home (by choice)", text)
        self.assertNotIn("NEEDS A LOOK", text)
        r = self.keep_cli("unkeep", "m2")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.m.KEEP) as f:
            content = f.read()
        self.assertIn("# my note", content)
        self.assertIn("iso", content)
        self.assertNotIn("m2", content.replace("#", "\n#").split("#")[0])
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertLink(".m2/repository", "toolchains/jvm/m2/repository")
        self.assertFalse(os.path.islink(self.H("Downloads/iso")))

    def test_keep_a_linked_item_says_so_and_drops_the_boot_flag(self) -> None:
        self.write(".m2/repository/x.jar", "jar")
        self.write("Programs/a", "a")
        self.busy = {self.H("Programs")}
        self.apply()
        self.assertLink(".m2/repository", "toolchains/jvm/m2/repository")
        with open(self.m.FLAG) as f:
            self.assertIn("apps", f.read().split())
        r = self.keep_cli("keep", "m2", "apps")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still a link into ~/Bulk", r.stdout)
        self.assertFalse(os.path.exists(self.m.FLAG))  # nothing waits for the boot any more
        left, text = self.report()
        self.assertIn("but still a link into ~/Bulk", text)
        # JetBrains lives inside ~/Bulk/apps: it waits, with the reason.
        self.assertIn("~/Programs is kept in /home (by choice)", text)
        self.busy = set()
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertFalse(os.path.islink(self.H("Programs")))

    # ---- review round 3, second pass ----
    def test_crash_partway_through_deleting_old_tree_is_finished(self) -> None:
        # Some files of the old tree are gone already: their folders' ctimes
        # changed, which must not look like a late write (a rollback onto the
        # half-deleted tree would delete the only full copy).
        self.write("Programs/top", "top")
        for i in range(5):
            self.write(f"Programs/sub/f{i}", str(i))
        self.run_crash("u_finish", "unlink", 2)
        self.assertTrue(self.m.jread("apps")["finishing"])
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("deleted the old copy", self.out)
        self.assertNotIn("undid", self.out)
        self.assertLink("Programs", "apps")
        self.assertEqual(self.read("Programs/top"), "top")
        for i in range(5):
            self.assertEqual(self.read(f"Programs/sub/f{i}"), str(i))
        self.assertEqual(self.journals(), [])
        self.assertEqual([n for r, d, f in os.walk(self.home) for n in d + f if ".bulk-" in n], [])

    def test_crash_partway_through_deleting_old_tree_in_nocow_rewrite(self) -> None:
        self.make_avd_cow()
        self.write(".android/avd/img3", "img3")
        for i in range(4):
            self.write(f".android/avd/a.avd/part{i}", str(i))
        self.run_crash("u_finish", "unlink", 2)
        self.assertTrue(self.m.jread("avd")["finishing"])
        deferred, problems = self.apply()
        self.assertEqual((deferred, problems), ([], []), self.out)
        self.assertNotIn("undid", self.out)
        self.assertEqual(self.read(".android/avd/disk.img"), "old")
        self.assertEqual(self.read(".android/avd/img3"), "img3")
        for i in range(4):
            self.assertEqual(self.read(f".android/avd/a.avd/part{i}"), str(i))
        self.assertEqual(self.m.cow_data(self.B("emulators/android/avd")), (0, False))
        self.assertEqual([n for n in os.listdir(self.B("emulators/android")) if ".bulk-" in n], [])

    def test_crash_while_repair_deletes_old_tree_is_finished(self) -> None:
        # A journal of an older version (no "finishing"): the repair checks,
        # marks it, deletes; a second kill there is finished by the next run.
        self.write("Programs/top", "top")
        for i in range(3):
            self.write(f"Programs/sub/f{i}", str(i))
        self.run_crash("u_finish", "unlink", 0)
        j = self.m.jread("apps")
        del j["finishing"]
        self.m.jwrite("apps", j)
        self.run_crash("u_finish", "unlink", 1)
        self.assertTrue(self.m.jread("apps")["finishing"])
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertNotIn("undid", self.out)
        self.assertEqual(self.read("Programs/top"), "top")
        for i in range(3):
            self.assertEqual(self.read(f"Programs/sub/f{i}"), str(i))
        self.assertEqual(self.journals(), [])

    def test_kept_parent_with_data_in_child_needs_a_look(self) -> None:
        self.write("Programs/a", "a")
        r = self.keep_cli("keep", "apps")
        self.assertEqual(r.returncode, 0, r.stderr)
        # No JetBrains data in /home: it just waits.
        os.makedirs(self.H(".local/share/JetBrains/Toolbox"))
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("waits:", self.out)
        # IDE builds in /home that would stay in snapshots unnoticed: a problem.
        self.write(".local/share/JetBrains/Toolbox/apps/idea/bin", "x")
        deferred, problems = self.apply()
        self.assertEqual(problems, ["jetbrains"], self.out)
        self.assertEqual(self.m.exit_status(deferred, problems), 1)
        self.assertIn("'bulk keep jetbrains' too", self.out)
        self.assertEqual(self.keep_cli("keep", "jetbrains").returncode, 0)
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertFalse(os.path.islink(self.H(".local/share/JetBrains/Toolbox/apps")))

    def test_keep_file_not_utf8_or_unreadable(self) -> None:
        self.write(".m2/repository/x.jar", "jar")
        with open(self.m.KEEP, "wb") as f:
            f.write(b"m2\n# caf\xe9 note\nbad\xff\n")
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("unknown item", self.out)
        self.assertFalse(os.path.islink(self.H(".m2/repository")))  # m2 still kept
        r = self.keep_cli("keep", "iso")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.m.KEEP, "rb") as f:
            data = f.read()
        self.assertIn(b"caf\xe9", data)  # unreadable bytes kept as they were
        self.assertIn(b"iso\n", data)
        # A folder where the file belongs: nothing is moved, and it is a problem.
        os.remove(self.m.KEEP)
        os.mkdir(self.m.KEEP)
        deferred, problems = self.apply()
        self.assertEqual(problems, ["bulk-keep"], self.out)
        self.assertEqual(self.m.exit_status(deferred, problems), 1)
        self.assertFalse(os.path.islink(self.H(".m2/repository")))
        left, text = self.report()
        self.assertIn("cannot be read", text)
        r = self.keep_cli("keep", "iso")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot update", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_npm_cache_clean_removes_link_and_apply_repairs(self) -> None:
        self.write(".npm/_cacache/content-v2/a", "a")
        self.write(".npm/_logs/debug.log", "log")
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertLink(".npm/_cacache", "toolchains/node/npm/_cacache")
        self.assertFalse(os.path.islink(self.H(".npm/_logs")))
        # npm cache clean --force: fs.rm on the link removes the link only;
        # the next npm command fills a new real folder.
        os.unlink(self.H(".npm/_cacache"))
        self.write(".npm/_cacache/content-v2/b", "b")
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["npm-cache"], {"apps"}), ("move", "stale"))
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertIn("deleting the stale copy", self.out)
        self.assertLink(".npm/_cacache", "toolchains/node/npm/_cacache")
        self.assertEqual(self.read(".npm/_cacache/content-v2/b"), "b")
        self.assertFalse(os.path.exists(self.B("toolchains/node/npm/_cacache/content-v2/a")))
        self.assertEqual(self.read(".npm/_logs/debug.log"), "log")
        # Not a pure cache (Maven's repository): data in both is still left alone.
        self.write(".m2/repository/x.jar", "jar")
        self.write(".m2/x", "x")
        os.makedirs(self.B("toolchains/jvm/m2/repository"), exist_ok=True)
        with open(self.B("toolchains/jvm/m2/repository/y.jar"), "w") as f:
            f.write("y")
        self.assertEqual(self.m.classify(self.ctx, self.m.BY_ID["m2"], {"apps"})[0], "unexpected")

    def test_go_clean_modcache_read_only_stale_copy(self) -> None:
        self.write("go/pkg/mod/m@v1/a.go", "a")
        self.assertEqual(self.apply(), ([], []), self.out)
        os.chmod(self.B("toolchains/go/pkg/mod/m@v1"), 0o555)  # Go's module cache is read-only
        os.unlink(self.H("go/pkg/mod"))
        self.write("go/pkg/mod/m@v2/b.go", "b")
        _, text = self.report("plan")
        self.assertIn("delete the stale", text)
        self.assertEqual(self.apply(), ([], []), self.out)
        self.assertLink("go/pkg/mod", "toolchains/go/pkg/mod")
        self.assertEqual(self.read("go/pkg/mod/m@v2/b.go"), "b")
        self.assertFalse(os.path.exists(self.B("toolchains/go/pkg/mod/m@v1")))

    def test_nocow_rewrite_failure_without_journal_is_bulk_only(self) -> None:
        self.make_avd_cow()
        self.m.free_bytes = lambda path: 0
        deferred, problems = self.apply()
        self.assertEqual(problems, ["avd"], self.out)
        self.assertIn("not enough free space", self.out)
        self.assertIn("snapshots do not wait for it", self.out)
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.m.exit_status(deferred, problems), 4)
        self.assertEqual(self.m.exit_status(deferred, problems + ["iso"]), 1)

    # ---- crash and race points found by the adversarial review of recover() ----

    def test_delete_directly_in_old_top_folder_is_a_late_write(self) -> None:
        self.write("Programs/a", "a")
        self.write("Programs/lock", "L")
        real = self.m.u_late_write

        def late(j: dict[str, Any]) -> bool:
            os.unlink(os.path.join(j["old"], "lock"))  # an app removes its lock file
            return real(j)

        self.m.u_late_write = late
        self.apply()
        # Undone: the old tree (without the lock) is back, nothing resurrected.
        self.assertFalse(os.path.islink(self.H("Programs")))
        self.assertFalse(os.path.exists(self.H("Programs/lock")))
        self.assertTrue(os.path.exists(self.H("Programs/a")))

    def test_undo_after_kill_keeps_writes_made_through_the_link(self) -> None:
        self.write("Programs/a", "a")
        m = self.m
        m.u_late_write = lambda j: True
        real_rb = m.u_rollback

        def rb(j: dict[str, Any]) -> None:
            raise Crash()

        m.u_rollback = rb
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            m.apply(self.ctx)
        m.u_rollback = real_rb
        self.assertTrue(m.jread("apps").get("undo"))
        self.write("Programs/user-work", "important")  # through the live link
        deferred, problems = self.apply()
        self.assertIn("apps", problems)
        found = [os.path.join(r, f) for r, _, fs in os.walk(self.home) for f in fs if f == "user-work"]
        self.assertEqual(len(found), 1, found)
        self.assertTrue(os.path.exists(self.m.jpath("apps")))

    def test_journal_ids_stay_in_the_state_folder(self) -> None:
        for ident in ("apps", "steam-570", "steam-1.2"):
            self.assertEqual(os.path.dirname(self.m.jpath(ident)), os.path.dirname(self.m.jpath("apps")))
        for ident in ("", "../x", "steam-1/../../etc", "a/b", ".hidden", "steam-1..2"):
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                self.m.jpath(ident)

    def test_link_removed_after_commit_keeps_the_written_copy(self) -> None:
        self.write("Programs/a", "a")
        m = self.m
        real = m.u_late_write

        def boom(j: dict[str, Any]) -> bool:
            raise Crash()

        m.u_late_write = boom
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            m.apply(self.ctx)
        m.u_late_write = real
        self.write("Programs/new-work", "w")  # into the copy, through the link
        os.unlink(self.H("Programs"))  # a tool removes the link
        deferred, problems = self.apply()
        self.assertIn("apps", problems)
        found = [os.path.join(r, f) for r, _, fs in os.walk(self.home) for f in fs if f == "new-work"]
        self.assertEqual(len(found), 1, found)
        self.assertTrue(os.path.exists(self.H("Programs/a")))

    def test_subvol_moved_then_old_path_recreated_is_not_deleted(self) -> None:
        self.write("go/pkg/mod/a", "data")
        L = self.H("go/pkg/mod")
        st = os.lstat(L)
        self.subvols.add((st.st_dev, st.st_ino))
        m = self.m
        real = m.u_move_subvol

        def mv(Lp: str, Tp: str) -> None:
            if os.path.isdir(Tp):
                os.rmdir(Tp)
            os.rename(Lp, Tp)
            raise Crash()

        m.u_move_subvol = mv
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            m.apply(self.ctx)
        m.u_move_subvol = real
        self.write("go/pkg/mod/b", "new")  # go recreates the folder
        deferred, problems = self.apply()
        self.assertIn("go-mod", problems)
        self.assertTrue(os.path.exists(self.B("toolchains/go/pkg/mod/a")))
        self.assertTrue(os.path.exists(self.H("go/pkg/mod/b")))

    def test_subvol_target_turned_into_a_symlink_is_not_followed(self) -> None:
        # Recovery runs as root on a path under the user's ~/Bulk: a symbolic
        # link there is reported (journal kept), never listed through.
        self.write("go/pkg/mod/a", "data")
        L = self.H("go/pkg/mod")
        st = os.lstat(L)
        self.subvols.add((st.st_dev, st.st_ino))
        m = self.m

        def mv(Lp: str, Tp: str) -> None:
            if os.path.isdir(Tp):
                os.rmdir(Tp)
            os.rename(Lp, Tp)
            raise Crash()

        m.u_move_subvol = mv
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            m.apply(self.ctx)
        T = self.B("toolchains/go/pkg/mod")
        os.rename(T, T + ".elsewhere")
        empty = os.path.join(self.tmp, "empty-elsewhere")
        os.mkdir(empty)
        os.symlink(empty, T)
        _deferred, problems = self.apply()
        self.assertIn("go-mod", problems)
        self.assertTrue(os.path.exists(self.m.jpath("go-mod")), "the journal stays")
        self.assertIn(f"{T} is a symbolic link (not followed)", self.out)
        self.assertTrue(os.path.islink(T))

    def test_a_partly_unreadable_tree_is_at_least_its_sum(self) -> None:
        # What du -sxb does then: the sum of what it read on stdout, exit 1.
        answers = {
            "partial": subprocess.CompletedProcess([], 1, "5368709120\t/x\n", "du: cannot read directory '/x/b'\n"),
            "whole": subprocess.CompletedProcess([], 0, "5368709120\t/x\n", ""),
            "nothing": subprocess.CompletedProcess([], 1, "", "du: cannot access '/x': No such file or directory\n"),
        }
        answer = answers["partial"]

        def du(args: Sequence[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(list(args[:2]), ["du", "-sxb"])
            return answer

        with mock.patch.object(bulk_adapters.subprocess, "run", du):
            self.assertEqual(bulk_adapters.du_bytes("/x"), bulk_domain.TreeSize(5368709120, False))
            item = next(i for i in bulk_domain.ITEMS if i.id == "apps")
            ctx = bulk_adapters.Ctx(self.home)
            text = bulk_items.item_text(ctx, item, "nocow", "folder without +C", "plan")
            self.assertEqual(text, "rewrite without copy-on-write (folder without +C, at least 5.0 GiB)")
            answer = answers["whole"]
            text = bulk_items.item_text(ctx, item, "nocow", "folder without +C", "plan")
            self.assertEqual(text, "rewrite without copy-on-write (folder without +C, 5.0 GiB)")
            answer = answers["nothing"]
            self.assertEqual(bulk_adapters.du_bytes("/x"), bulk_domain.TreeSize(0, False))
        self.assertEqual(bulk_domain.du_text(bulk_domain.TreeSize(0, False)), "at least 0 MiB")

    def test_entries_nofollow(self) -> None:
        d = os.path.join(self.tmp, "real")
        os.makedirs(os.path.join(d, "sub"))
        open(os.path.join(d, "f"), "w").close()
        os.symlink(d, os.path.join(self.tmp, "link"))
        self.assertEqual(sorted(bulk_adapters.entries_nofollow(d)), ["f", "sub"])
        self.assertEqual(bulk_adapters.entries_nofollow(os.path.join(self.tmp, "absent")), [])
        self.assertEqual(bulk_adapters.entries_nofollow(os.path.join(d, "f")), [])
        with self.assertRaises(bulk_domain.Unexpected) as cm:
            bulk_adapters.entries_nofollow(os.path.join(self.tmp, "link"))
        self.assertIn("is a symbolic link (not followed)", str(cm.exception))

    def test_toolchain_targets_are_nested_and_their_parents_made_on_the_way(self) -> None:
        # toolchains/<language>/<tool>/<the folder's own name>, all copy-on-write;
        # the Android SDK is the tool itself (toolchains/android/sdk).
        tc = [i for i in self.m.ITEMS if i.target.startswith("toolchains/")]
        self.assertEqual(
            sorted(i.id for i in tc),
            sorted(
                [
                    "android-sdk",
                    "m2",
                    "gradle-caches",
                    "gradle-wrapper",
                    "go-mod",
                    "rustup",
                    "rustup-tmp",
                    "rustup-downloads",
                    "cargo-registry",
                    "cargo-git",
                    "npm-cache",
                ]
            ),
        )
        self.assertEqual(self.m.BY_ID["android-sdk"].target, "toolchains/android/sdk")
        for i in tc:
            self.assertFalse(i.nocow, i.id)
            if i.id == "android-sdk":
                continue
            self.assertEqual(len(i.target.split("/")), 4, i.target)
            assert i.link is not None
            self.assertEqual(os.path.basename(i.target), os.path.basename(i.link), i.id)
        # Grouped by treatment: exactly the NOCOW items live below emulators/.
        self.assertEqual(self.m.NOCOW_TOP, "emulators")
        self.assertEqual([i.id for i in self.m.ITEMS if i.nocow], ["avd"])
        self.assertEqual([i.id for i in self.m.ITEMS if i.target.startswith("emulators/")], ["avd"])
        self.assertEqual(self.m.BY_ID["avd"].target, "emulators/android/avd")
        self.assertFalse(any(i.target.split("/")[0] == "android" for i in self.m.ITEMS))
        # No target lies inside another one (each would be moved into the other).
        targets = [i.target for i in self.m.ITEMS]
        for a in targets:
            for b in targets:
                if a != b and b.startswith(a + "/"):
                    self.assertEqual((a, b), ("apps", "apps/jetbrains"))
        self.write(".m2/repository/org/x.jar", "jar")
        self.write(".gradle/caches/c", "c")
        self.write(".rustup/toolchains/stable/rustc", "rustc")
        self.write(".cargo/git/db/a", "git")
        self.assertEqual(self.apply(), ([], []))
        for d in (
            "toolchains",
            "toolchains/jvm",
            "toolchains/jvm/m2",
            "toolchains/jvm/gradle",
            "toolchains/go",
            "toolchains/go/pkg",
            "toolchains/rust",
            "toolchains/rust/rustup",
            "toolchains/rust/cargo",
            "toolchains/node",
            "toolchains/node/npm",
        ):
            st = os.lstat(self.B(d))
            self.assertTrue(stat.S_ISDIR(st.st_mode), d)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o755, d)
            self.assertEqual(st.st_uid, os.getuid(), d)
        self.assertLink(".m2/repository", "toolchains/jvm/m2/repository")
        self.assertLink(".gradle/caches", "toolchains/jvm/gradle/caches")
        self.assertLink(".gradle/wrapper", "toolchains/jvm/gradle/wrapper")
        self.assertLink(".cargo/git", "toolchains/rust/cargo/git")
        self.assertLink(".cargo/registry", "toolchains/rust/cargo/registry")
        self.assertEqual(self.read(".rustup/toolchains/stable/rustc"), "rustc")
        # Nothing of the move is left next to the targets (.bulk-new, .bulk-stamp).
        leftovers = [p for p, *_ in self.tree() if ".bulk-" in p]
        self.assertEqual(leftovers, [])
        before = self.tree()
        self.assertEqual(self.apply(), ([], []))
        self.assertEqual(self.tree(), before)
        left, text = self.report()
        self.assertEqual(left, 0, text)
        self.assertIn("~/.m2/repository -> ~/Bulk/toolchains/jvm/m2/repository", text)

    def test_symlink_or_file_in_a_toolchain_parent_is_untouched(self) -> None:
        self.apply()
        os.makedirs(self.H("elsewhere"))
        shutil.rmtree(self.B("toolchains/jvm"))
        os.symlink(self.H("elsewhere"), self.B("toolchains/jvm"))  # a parent is a symlink
        shutil.rmtree(self.B("toolchains/rust/cargo"))
        self.write("Bulk/toolchains/rust/cargo", "a file")  # a parent is a file
        self.write(".m2/repository/x.jar", "jar")
        self.write(".cargo/registry/index/a", "crate")
        before = self.tree()
        deferred, problems = self.apply()
        self.assertEqual(deferred, [])
        self.assertEqual(
            sorted(problems), sorted(["m2", "gradle-caches", "gradle-wrapper", "cargo-registry", "cargo-git"])
        )
        self.assertEqual(self.tree(), before)
        self.assertEqual(os.listdir(self.H("elsewhere")), [])
        _, text = self.report()
        self.assertIn("~/Bulk/toolchains/jvm is a symlink", text)
        self.assertIn("~/Bulk/toolchains/rust/cargo is a file", text)

    def test_old_toolchain_subvolume_is_renamed_into_a_nested_target(self) -> None:
        self.write("go/pkg/mod/a", "data")
        st = os.lstat(self.H("go/pkg/mod"))
        self.subvols.add((st.st_dev, st.st_ino))
        self.assertEqual(self.apply(), ([], []))
        self.assertLink("go/pkg/mod", "toolchains/go/pkg/mod")
        self.assertTrue(self.m.is_subvol(self.B("toolchains/go/pkg/mod")))
        self.assertEqual(self.read("go/pkg/mod/a"), "data")


class GibTest(unittest.TestCase):
    """bulk shows GiB from a tenth of a GiB on (old data worth pointing out), else whole MiB."""

    def test_threshold_and_units(self) -> None:
        tenth = bulk_domain.BIG // 10 + 1
        self.assertEqual(bulk_domain.gib(tenth), "0.1 GiB")
        self.assertEqual(bulk_domain.gib(tenth - 2), "102 MiB")
        self.assertEqual(bulk_domain.gib(bulk_domain.BIG // 10), "102 MiB")  # just under a tenth
        self.assertEqual(bulk_domain.gib(3 * bulk_domain.BIG), "3.0 GiB")
        self.assertEqual(bulk_domain.gib((5 << 20) + (1 << 19)), "5 MiB")  # floored


if __name__ == "__main__":
    unittest.main()
