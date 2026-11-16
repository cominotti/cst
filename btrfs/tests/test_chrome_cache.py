"""Unit tests for btrfs/chrome-cache and the SNAP_EXCLUDE list (no root needed).

HOME is a temporary folder; "Chrome installed" is a fake system install dir.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s btrfs/tests -v
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from typing import Protocol, cast
from tests.script_loader import load_script

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BTRFS = os.path.dirname(HERE)
TOOL = os.path.join(BTRFS, "chrome-cache")


class _ChromeCacheModule(Protocol):
    SYSTEM_INSTALLS: list[str]
    RUNTIME_DIR: str
    MARK: str
    ON_LINE: str
    OFF_LINE: str
    FLAG_DIR: str
    FLAG_SIZE: str
    CACHE_SIZE: int
    Refused: type[Exception]
    decide: Callable[[str], tuple[str, str | None, str]]
    is_subvol: Callable[[str], bool]

    def installed(self, home: str) -> str | None: ...

    def apply(self, home: str) -> tuple[str, str, bool]: ...

    def off(self, home: str) -> tuple[str, bool, bool]: ...

    def plan_line(self, home: str) -> str: ...

    def status(self, home: str) -> int: ...

    def main(self, argv: Sequence[str]) -> int: ...


def load() -> _ChromeCacheModule:
    mod = load_script(TOOL, "chrome_cache")
    return cast("_ChromeCacheModule", cast("object", mod))


class ChromeCacheTest(unittest.TestCase):
    m: _ChromeCacheModule
    tmp: str
    home: str
    sys: str
    cfg: str
    path: str

    def setUp(self) -> None:
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="chrome-cache-test-")
        self.home = os.path.join(self.tmp, "home")
        self.sys = os.path.join(self.tmp, "system-flatpak")
        os.mkdir(self.home)
        self.m.SYSTEM_INSTALLS = [self.sys]
        self.m.RUNTIME_DIR = os.path.join(self.tmp, "run-user")
        self.cfg = os.path.join(self.home, ".var/app/com.google.Chrome/config")
        self.path = os.path.join(self.cfg, "chrome-flags.conf")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    # helpers
    def install(self, where: str = "system") -> None:
        base = self.sys if where == "system" else os.path.join(self.home, ".local/share/flatpak")
        os.makedirs(os.path.join(base, "app/com.google.Chrome/current/active"))

    def write(self, text: str, mode: int = 0o644) -> None:
        os.makedirs(self.cfg, exist_ok=True)
        with open(self.path, "w") as f:
            f.write(text)
        os.chmod(self.path, mode)

    def read(self) -> str:
        with open(self.path) as f:
            return f.read()

    def block(self) -> str:
        return f"{self.m.ON_LINE}\n--disk-cache-dir=/tmp/chrome-cache\n--disk-cache-size=536870912\n"

    def status(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.m.status(self.home)
        return rc, out.getvalue()

    # values the design fixed
    def test_constants(self) -> None:
        self.assertEqual(self.m.FLAG_DIR, "--disk-cache-dir=/tmp/chrome-cache")
        self.assertEqual(self.m.FLAG_SIZE, "--disk-cache-size=536870912")
        self.assertEqual(self.m.CACHE_SIZE, 512 * 1024 * 1024)
        # cobalt parses each line as shell words: no quotes in our comments.
        for line in (self.m.ON_LINE, self.m.OFF_LINE):
            self.assertTrue(line.startswith("#"))
            self.assertNotIn("'", line)
            self.assertNotIn('"', line)

    # apply
    def test_not_installed_does_nothing(self) -> None:
        state, _, changed = self.m.apply(self.home)
        self.assertEqual(state, "no-app")
        self.assertFalse(changed)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".var")))

    def test_absent_file_created(self) -> None:
        self.install()
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("absent", True))
        self.assertEqual(self.read(), self.block())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        self.assertEqual(os.listdir(self.cfg), ["chrome-flags.conf"])  # no temp file left

    def test_user_install_counts(self) -> None:
        self.install("user")
        self.assertTrue(self.m.apply(self.home)[2])
        self.assertEqual(self.m.installed(self.home), "user")

    def test_unrelated_flags_kept_in_order(self) -> None:
        self.install()
        user = "# my flags\n--ozone-platform-hint=auto\n\n--enable-features=VaapiVideoDecoder"  # no final newline
        self.write(user, 0o600)
        self.assertTrue(self.m.apply(self.home)[2])
        self.assertEqual(self.read(), user + "\n" + self.block())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_idempotent(self) -> None:
        self.install()
        self.write("--foo\n")
        self.m.apply(self.home)
        first = self.read()
        st = os.stat(self.path)
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("managed", False))
        self.assertEqual(self.read(), first)
        self.assertEqual(os.stat(self.path).st_ino, st.st_ino)  # not even rewritten

    def test_user_set_dir_not_overwritten(self) -> None:
        self.install()
        text = "--disk-cache-dir=/home/me/somewhere\n"
        self.write(text)
        state, msg, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("user", False))
        self.assertIn("set by you", msg)
        self.assertEqual(self.read(), text)
        rc, out = self.status()
        self.assertEqual(rc, 0)
        self.assertIn("set by you (--disk-cache-dir=/home/me/somewhere)", out)

    def test_user_set_size_only_not_touched(self) -> None:
        self.install()
        text = "--disk-cache-size=104857600\n"
        self.write(text)
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("user", False))
        self.assertEqual(self.read(), text)

    def test_user_same_dir_other_size_kept_with_warning(self) -> None:
        self.install()
        text = "--disk-cache-dir=/tmp/chrome-cache --disk-cache-size=4294967296\n"  # two flags on one line
        self.write(text)
        state, msg, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("user", False))
        self.assertIn("WARNING", msg)
        self.assertEqual(self.read(), text)

    def test_user_same_values_without_marker(self) -> None:
        self.install()
        text = "--disk-cache-dir=/tmp/chrome-cache\n--disk-cache-size=536870912\n"
        self.write(text)
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("user-same", False))
        self.assertEqual(self.read(), text)

    def test_commented_flag_is_not_a_setting(self) -> None:
        self.install()
        self.write("# --disk-cache-dir=/elsewhere\n")
        self.assertEqual(self.m.apply(self.home)[0], "absent")
        self.assertEqual(self.read(), "# --disk-cache-dir=/elsewhere\n" + self.block())

    def test_cap_line_missing_under_marker_added_back(self) -> None:
        self.install()
        self.write(f"--a\n{self.m.ON_LINE}\n--disk-cache-dir=/tmp/chrome-cache\n--b\n")
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("cap-missing", True))
        self.assertEqual(
            self.read(),
            f"--a\n{self.m.ON_LINE}\n--disk-cache-dir=/tmp/chrome-cache\n--disk-cache-size=536870912\n--b\n",
        )

    def test_marker_with_non_exact_dir_line_is_user_not_crash(self) -> None:
        self.install()
        for line in (
            "--disk-cache-dir=/tmp/chrome-cache  # mine",
            '"--disk-cache-dir=/tmp/chrome-cache"',
            "--disk-cache-dir=/tmp/chrome-cache --x",
        ):
            with self.subTest(line=line):
                text = f"{self.m.ON_LINE}\n{line}\n"
                state, new, msg = self.m.decide(text)
                self.assertEqual((state, new), ("user", None))
                self.assertIn("no size cap", msg)
                self.write(text)
                self.assertEqual(self.m.apply(self.home)[:1], ("user",))
                self.assertTrue(self.m.plan_line(self.home).startswith("nothing to do"))
                self.assertEqual(self.read(), text)

    def test_symlinked_file_refused(self) -> None:
        self.install()
        os.makedirs(self.cfg)
        elsewhere = os.path.join(self.tmp, "elsewhere.conf")
        with open(elsewhere, "w") as f:
            f.write("--x\n")
        os.symlink(elsewhere, self.path)
        with self.assertRaises(self.m.Refused):
            self.m.apply(self.home)
        with open(elsewhere) as f:
            self.assertEqual(f.read(), "--x\n")
        self.assertTrue(os.path.islink(self.path))
        rc, out = self.status()
        self.assertEqual(rc, 1)
        self.assertIn("NEEDS A LOOK", out)
        # The command line says so and exits 1.
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.main(["chrome-cache", "apply", "--home", self.home]), 1)
        self.assertIn("symlink", err.getvalue())

    def test_symlinked_parent_refused(self) -> None:
        self.install()
        real = os.path.join(self.tmp, "real-config")
        os.makedirs(real)
        os.makedirs(os.path.dirname(self.cfg))
        os.symlink(real, self.cfg)
        with self.assertRaises(self.m.Refused):
            self.m.apply(self.home)
        self.assertEqual(os.listdir(real), [])

    def test_not_a_regular_file_refused(self) -> None:
        self.install()
        os.makedirs(self.path)
        with self.assertRaises(self.m.Refused):
            self.m.apply(self.home)

    def test_changed_meanwhile_refused(self) -> None:
        self.install()
        self.write("--a\n")
        real_decide = self.m.decide

        def racing(text: str) -> tuple[str, str | None, str]:
            r = real_decide(text)
            with open(self.path, "a") as f:
                f.write("--written-meanwhile=1\n")
            return r

        self.m.decide = racing
        with self.assertRaises(self.m.Refused):
            self.m.apply(self.home)
        self.assertEqual(self.read(), "--a\n--written-meanwhile=1\n")
        self.assertEqual(os.listdir(self.cfg), ["chrome-flags.conf"])

    def test_non_utf8_bytes_kept(self) -> None:
        self.install()
        os.makedirs(self.cfg)
        with open(self.path, "wb") as f:
            f.write(b"# caf\xe9\n--a\n")
        self.m.apply(self.home)
        with open(self.path, "rb") as f:
            self.assertTrue(f.read().startswith(b"# caf\xe9\n--a\n"))

    # off
    def test_off_removes_only_ours_and_stays_off(self) -> None:
        self.install()
        self.write("--a\n")
        self.m.apply(self.home)
        with open(self.path, "a") as f:
            f.write("--b\n")
        msg, changed, still_set = self.m.off(self.home)
        self.assertEqual((changed, still_set), (True, False))
        self.assertIn("our flag lines removed", msg)
        self.assertEqual(self.read(), f"--a\n{self.m.OFF_LINE}\n--b\n")
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("off", False))
        self.assertEqual(self.m.off(self.home), ("already off: nothing changed", False, False))

    def test_manual_undo_keeps_marker_means_off(self) -> None:
        self.install()
        self.write(f"--a\n{self.m.ON_LINE}\n")  # flag lines deleted by hand, comment kept
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("off", False))

    # files written by versions named f44-tools
    def old_line(self, line: str) -> str:
        return line.replace(self.m.MARK, "# btrfs-setup.sh (f44-tools) chrome-cache")

    def test_old_marker_is_still_ours(self) -> None:
        self.install()
        old = self.old_line(self.block())
        self.assertNotEqual(old, self.block())
        self.write("--a\n" + old)
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("managed", False))
        self.assertEqual(self.read(), "--a\n" + old)  # not rewritten for the new name
        msg, changed, still_set = self.m.off(self.home)
        self.assertEqual((changed, still_set), (True, False))
        self.assertEqual(self.read(), f"--a\n{self.m.OFF_LINE}\n")

    def test_old_off_marker_still_off(self) -> None:
        self.install()
        self.write(self.old_line(self.m.OFF_LINE) + "\n")
        state, _, changed = self.m.apply(self.home)
        self.assertEqual((state, changed), ("off", False))

    def test_off_before_any_apply(self) -> None:
        msg, changed, still_set = self.m.off(self.home)
        self.assertEqual((changed, still_set), (True, False))
        self.assertEqual(self.read(), self.m.OFF_LINE + "\n")
        self.install()
        self.assertEqual(self.m.apply(self.home)[0], "off")

    def test_off_leaves_user_values(self) -> None:
        self.write("--disk-cache-dir=/x\n")
        msg, changed, still_set = self.m.off(self.home)
        self.assertEqual((changed, still_set), (True, True))
        self.assertNotIn("removed", msg)
        self.assertIn("by hand", msg)
        self.assertEqual(self.read(), f"--disk-cache-dir=/x\n{self.m.OFF_LINE}\n")

    def test_off_with_user_same_values_says_they_stay(self) -> None:
        text = "--disk-cache-dir=/tmp/chrome-cache\n--disk-cache-size=536870912\n"
        self.write(text)
        msg, changed, still_set = self.m.off(self.home)
        self.assertEqual((changed, still_set), (True, True))
        self.assertNotIn("removed", msg)
        self.assertIn("--disk-cache-dir=/tmp/chrome-cache", msg)
        self.assertEqual(self.read(), text + self.m.OFF_LINE + "\n")

    # plan / status
    def test_plan_line(self) -> None:
        self.assertIn("not installed", self.m.plan_line(self.home))
        self.install()
        self.assertTrue(self.m.plan_line(self.home).startswith("add the cache flags"))
        self.m.apply(self.home)
        self.assertTrue(self.m.plan_line(self.home).startswith("nothing to do"))

    def test_status_old_cache_and_subvolume(self) -> None:
        self.install()
        prof = os.path.join(self.home, ".var/app/com.google.Chrome/cache/google-chrome")
        for p in ("Default", "Profile 1"):
            os.makedirs(os.path.join(prof, p, "Cache", "Cache_Data"))
            with open(os.path.join(prof, p, "Cache", "Cache_Data", "data_1"), "wb") as f:
                f.write(os.urandom(2 * 1024 * 1024))
            os.makedirs(os.path.join(prof, p, "Code Cache"))
            with open(os.path.join(prof, p, "Code Cache", "js"), "wb") as f:
                f.write(b"x" * 8192)
        rc, out = self.status()
        self.assertIn("absent; 'snapshots setup'", out)
        self.assertIn("on-disk HTTP cache:", out)
        self.assertNotIn("rm -rf", out)  # not before the flags are there
        self.m.apply(self.home)
        rc, out = self.status()
        self.assertEqual(rc, 0)
        self.assertIn("flags present (managed by CST)", out)
        self.assertIn("in 2 profile(s), in /home snapshots", out)
        self.assertIn("rm -rf ~/.var/app/com.google.Chrome/cache/google-chrome/*/Cache", out)
        self.assertIn("Code Cache on disk", out)
        self.assertNotIn("nested subvolume", out)
        # Nothing was deleted.
        self.assertTrue(os.path.exists(os.path.join(prof, "Default", "Cache", "Cache_Data", "data_1")))

        # A subvolume from an earlier setup: pointed out, never touched.
        def _fake_is_subvol(path: str) -> bool:
            return path.endswith("/.var/app/com.google.Chrome/cache")

        self.m.is_subvol = _fake_is_subvol
        rc, out = self.status()
        self.assertIn("a nested subvolume from an earlier setup", out)
        self.assertNotIn("in /home snapshots", out)

    def test_status_ram_usage(self) -> None:
        ram = os.path.join(self.m.RUNTIME_DIR, ".flatpak/com.google.Chrome/tmp/chrome-cache")
        os.makedirs(ram)
        _, out = self.status()
        self.assertIn("in RAM now:", out)

    def test_main_refuses_foreign_home_and_bad_usage(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.m.main(["chrome-cache", "bogus"]), 2)
            self.assertEqual(self.m.main(["chrome-cache", "apply", "--home"]), 2)
            if os.getuid() != 0 and os.path.isdir("/") and os.stat("/").st_uid != os.getuid():
                self.assertEqual(self.m.main(["chrome-cache", "apply", "--home", "/"]), 2)


class SnapExcludeTest(unittest.TestCase):
    """The list setup-snapshots.bash converts (sourced the way the frontend does)."""

    def entries(self) -> list[str]:
        r = subprocess.run(
            ["bash", "-c", 'SRC="$1"; . "$1/setup-snapshots.bash"; printf "%s\\n" "${SNAP_EXCLUDE[@]}"', "_", BTRFS],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return r.stdout.splitlines()

    def test_chrome_cache_no_longer_a_subvolume(self) -> None:
        self.assertEqual(
            self.entries(),
            ["aside .cache/uv", "copy .cache", "copy .local/share/flatpak", "copy Workspace", "copy Bulk"],
        )

    def stale(self, plan_lines: Sequence[str]) -> str:
        with tempfile.TemporaryDirectory() as d:
            plan = os.path.join(d, "convert-plan")
            with open(plan, "w") as f:
                f.write("# Managed by btrfs-setup.sh (CST): <aside|copy> <absolute path>\n")
                f.write("".join(line + "\n" for line in plan_lines))
            r = subprocess.run(
                [
                    "bash",
                    "-c",
                    'SRC="$1"; . "$1/setup-snapshots.bash"; SNAP_PLAN="$2"; snap_plan_stale /home/u',
                    "_",
                    BTRFS,
                    plan,
                ],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            return r.stdout

    def test_old_plan_with_chrome_is_pointed_out(self) -> None:
        current = [
            "aside /home/u/.cache/uv",
            "copy /home/u/.cache",
            "copy /home/u/.local/share/flatpak",
            "copy /home/u/Workspace",
            "copy /home/u/Bulk",
        ]
        self.assertEqual(self.stale(current), "")
        old = current[:3] + ["copy /home/u/.var/app/com.google.Chrome/cache"] + current[3:]
        out = self.stale(old)
        self.assertIn("from an older version", out)
        self.assertIn("/home/u/.var/app/com.google.Chrome/cache", out)
        self.assertIn("snapshots setup", out)

    def test_snapshot_relevant_folders_never_excluded(self) -> None:
        # The user wants these in /home snapshots: neither them nor a parent excluded.
        for rel in (".claude", ".codex", "Videos"):
            for e in self.entries():
                path = e.split(" ", 1)[1]
                self.assertFalse(rel == path or rel.startswith(path + "/"), (rel, e))


if __name__ == "__main__":
    unittest.main()
