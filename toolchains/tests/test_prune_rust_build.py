# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the rust:build target of toolchains/cst-toolchains: the
pruning of cargo's build folders (no root, no btrfs, no systemd needed).

HOME is a temporary folder with its own ~/.cargo/config.toml and
~/.cache/cargo-build; "now" is set through CST_TOOLCHAINS_NOW; systemctl is
replaced by a recorder. Nothing outside the temporary folder is touched.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s toolchains/tests -t . -v
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator, Sequence
from typing import Protocol
from unittest import mock

# Run directly (python3 -I -B toolchains/tests/test_prune_rust_build.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from cst_core.adapters import SystemClockAdapter
from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.errors import Refused
from toolchains import cli as tc_cli, wiring as tc_wiring
from toolchains.application import rust_build as tca_rust_build
from toolchains.ports import RunOutputPort
from toolchains.domain import build as tcd_build
from toolchains.domain import common as tcd_common
from toolchains.domain import prune as tcd_prune
from toolchains.adapters import program as tc_program
from toolchains.adapters import home_files as tc_home_files
from toolchains.adapters import build_tree as tc_build_tree
from toolchains.adapters import processes as tc_processes
from toolchains.adapters import cargo as tc_cargo
from toolchains.adapters import units as tc_units

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLCHAINS = os.path.dirname(HERE)
TOOL = os.path.join(TOOLCHAINS, "cst-toolchains")
REPO = os.path.dirname(TOOLCHAINS)
with open(TOOL, encoding="utf-8") as _fh:
    HELP = ast.get_docstring(ast.parse(_fh.read()), clean=False) or ""
H = 3600
NOW = 2_000_000_000.0  # fixed "now" (2033); every mtime is set relative to it


class FakeUnits(tc_units.UserUnits):
    """The user manager of a test: FAKE answers every systemctl call."""

    def __init__(self, out: RunOutputPort, fake: Callable[..., str]) -> None:
        # Never the real user manager, also if a call misses the fake.
        super().__init__(out, "/nonexistent/systemctl")
        self.fake = fake

    def systemctl(self, *args: str, check: bool = False, ok: Sequence[int] = (0,)) -> str:
        return self.fake(*args, check=check, ok=ok)


def quiet_signals(test: unittest.TestCase) -> None:
    """main() runs in-process here: SIGPIPE at its default would stay so for the rest of the test run."""
    p = mock.patch.object(tc_program, "restore_default_signals", return_value=None)
    p.start()
    test.addCleanup(p.stop)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        quiet_signals(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-toolchains-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.home, ".cache/cargo-build")
        os.makedirs(self.root)
        self.write_cargo_config(f'[build]\nbuild-dir = "{self.home}/.cache/cargo-build/{{workspace-path-hash}}"\n')
        self.env = {
            k: os.environ.get(k)
            for k in (
                "CST_TOOLCHAINS_NOW",
                "CST_TOOLCHAINS_HERE",
                "CARGO_HOME",
                "CST_TOOLCHAINS_SYSTEMCTL",
                "XDG_RUNTIME_DIR",
            )
        }
        os.environ["CST_TOOLCHAINS_NOW"] = str(NOW)
        os.environ["CST_TOOLCHAINS_HERE"] = "1"
        # Never the real user manager, also if a test misses the fake below.
        os.environ["CST_TOOLCHAINS_SYSTEMCTL"] = "/nonexistent/systemctl"
        os.environ.pop("CARGO_HOME", None)
        # status must never reach a real sccache server (their sockets live there).
        os.environ["XDG_RUNTIME_DIR"] = os.path.join(self.tmp, "no-runtime-dir")
        self.calls: list[tuple[str, ...]] = []
        self.manager_env = ""
        # No real cargo process of the user may change a test's result:
        # tests that need some set self.cargo_running.
        self.real_cargo_procs = tc_processes.cargo_procs
        self.cargo_running: list[tcd_build.CargoProc] = []
        self.limits = tcd_prune.Limits()
        # A build tree of the test's own for main (None: the one wiring builds).
        self.tree_for_main: tc_build_tree.CargoBuildTree | None = None

    def tearDown(self) -> None:
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def fake_systemctl(self, *args: str, check: bool = False, ok: Sequence[int] = (0,)) -> str:
        if args == ("show-environment",):  # read only: the user manager's environment
            return self.manager_env
        if args[:1] == ("is-active",):  # read only: what systemctl says of a timer that is not running
            return "inactive"
        self.calls.append(args)
        return ""

    # helpers
    def write_cargo_config(self, text: str) -> None:
        os.makedirs(os.path.join(self.home, ".cargo"), exist_ok=True)
        with open(os.path.join(self.home, ".cargo/config.toml"), "w") as f:
            f.write(text)

    def write_conf(self, text: str) -> None:
        """~/.config/cst/toolchains.conf; MAX_IDLE etc. get the RUST_BUILD_ prefix."""
        text = re.sub(r"(?m)^(MAX_IDLE|MAX_TOTAL|MIN_KEEP|ENABLED|ORPHANS|INCREMENTAL_IDLE)=", r"RUST_BUILD_\1=", text)
        path = os.path.join(self.home, ".config/cst/toolchains.conf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_conf_raw(self, text: str) -> None:
        path = os.path.join(self.home, ".config/cst/toolchains.conf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def folder(self, key: str, age_h: float, kib: int = 64, profile: str = "debug") -> str:
        """A cargo-like build folder whose newest mtime is AGE_H hours before NOW."""
        path = os.path.join(self.root, key)
        files = [
            os.path.join(path, "CACHEDIR.TAG"),
            os.path.join(path, profile, ".cargo-build-lock"),
            os.path.join(path, profile, "deps", "libfoo-0123.rlib"),
            os.path.join(path, profile, ".fingerprint", "foo-0123", "lib-foo"),
        ]
        for f in files:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "wb") as fh:
                if f.endswith(".rlib"):
                    fh.write(os.urandom(kib * 1024))
        self.age(path, age_h)
        return path

    @staticmethod
    def age(path: str, age_h: float) -> None:
        t = NOW - age_h * H
        for d, dirs, files in os.walk(path):
            for n in files + dirs:
                os.utime(os.path.join(d, n), (t, t), follow_symlinks=False)
            os.utime(d, (t, t))

    def parts(self) -> tc_wiring.Parts:
        """The real adapters, but the user manager (fake_systemctl), the cargo
        processes (cargo_running), the limits and, when set, tree_for_main."""
        tree = self.tree_for_main
        return tc_wiring.Parts(
            limits=self.limits,
            units=lambda out: FakeUnits(out, self.fake_systemctl),
            procs=lambda: list(self.cargo_running),
            tree=tree,
            journal=False,
        )

    def session(self) -> tc_wiring.Session:
        return tc_wiring.session(self.home, TOOL, self.parts())

    def tree(self) -> tc_build_tree.CargoBuildTree:
        """A build tree as main builds it (the cargo processes: cargo_running)."""
        return tc_build_tree.CargoBuildTree(
            self.home, "cst toolchains", self.limits, SystemClockAdapter(), procs=lambda: list(self.cargo_running)
        )

    def run_main(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tc_cli.main([*args, "--home", self.home], HELP, TOOL, parts=self.parts())
        return rc, out.getvalue() + err.getvalue()

    def present(self, key: str) -> bool:
        return os.path.isdir(os.path.join(self.root, key))


class SettingsTest(Base):
    def test_durations(self) -> None:
        p = tcd_common.parse_duration
        self.assertEqual(p("48h"), 48 * H)
        self.assertEqual(p("2d"), 48 * H)
        self.assertEqual(p("90m"), 5400)
        self.assertEqual(p(" 1.5h "), 5400)
        self.assertEqual(p("1w"), 7 * 24 * H)
        for bad in ("48", "h", "-1h", "1y", "", "1 hours"):
            with self.assertRaises(tcd_common.ConfigError, msg=bad):
                p(bad)

    def test_sizes(self) -> None:
        p = tcd_common.parse_size
        self.assertEqual(p("150G"), 150 << 30)
        self.assertEqual(p("150GiB"), 150 << 30)
        self.assertEqual(p("150 gb"), 150 << 30)
        self.assertEqual(p("1.5T"), int(1.5 * (1 << 40)))
        self.assertEqual(p("512M"), 512 << 20)
        self.assertEqual(p("4K"), 4096)
        self.assertEqual(p("off"), 0)
        self.assertEqual(p("0"), 0)
        # The unit is required: a bare number is a likely typo for G, and as
        # bytes it would delete everything older than MIN_KEEP.
        for bad in ("lots", "G", "-1G", "10X", "150", "4096", "150b", "1.5"):
            with self.assertRaises(tcd_common.ConfigError, msg=bad):
                p(bad)

    def test_conf_parsing(self) -> None:
        known = {"MAX_IDLE", "MAX_TOTAL", "MIN_KEEP"}
        raw = tcd_common.parse_conf(
            "# comment\n\nMAX_IDLE = \"24h\"   # a day\nMAX_TOTAL='200G'\n  MIN_KEEP=1h\n", known
        )
        self.assertEqual(raw, {"MAX_IDLE": "24h", "MAX_TOTAL": "200G", "MIN_KEEP": "1h"})
        with self.assertRaises(tcd_common.ConfigError):
            tcd_common.parse_conf("MAX_IDEL=24h\n", known)  # a typo is not ignored
        with self.assertRaises(tcd_common.ConfigError):
            tcd_common.parse_conf("just words\n", known)
        # cst_core's one reader: `export` is an assignment; a value bash would expand is an error.
        self.assertEqual(tcd_common.parse_conf("export MIN_KEEP=1h\n", known), {"MIN_KEEP": "1h"})
        with self.assertRaisesRegex(tcd_common.ConfigError, r"^line 2: MAX_IDLE=\$IDLE: \$"):
            tcd_common.parse_conf("MIN_KEEP=1h\nMAX_IDLE=$IDLE\nMAX_IDEL=1\n", known)

    def test_policy_defaults_and_limits(self) -> None:
        s = tc_home_files.load_settings(self.home, TOOL)
        rb = tca_rust_build.RustBuild.SPEC
        self.assertEqual(s.source_of(rb), "defaults")
        self.assertTrue(s.enabled["rust:build"])
        self.assertEqual(
            s.rust,
            tcd_build.RustPolicy(
                max_idle=48 * H, max_total=150 << 30, min_keep=2 * H, orphans=True, incremental_idle=6 * H
            ),
        )
        self.write_conf("MAX_TOTAL=off\nMAX_IDLE=3d\n")
        s = tc_home_files.load_settings(self.home, TOOL)
        pol = s.rust
        self.assertEqual((pol.max_total, pol.max_idle), (0, 72 * H))
        self.assertEqual(s.source_of(rb), "~/.config/cst/toolchains.conf")
        for text in (
            "MIN_KEEP=5m\n",
            "MAX_IDLE=1h\nMIN_KEEP=2h\n",
            "MAX_TOTAL=lots\n",
            "MAX_TOTAL=150\n",
            "ENABLED=maybe\n",
            "ORPHANS=maybe\n",
            "INCREMENTAL_IDLE=1h\n",
            "INCREMENTAL_IDLE=6\n",
        ):
            self.write_conf(text)
            with self.assertRaises(tcd_common.ConfigError, msg=text):
                tc_home_files.load_settings(self.home, TOOL)
        # A key without the prefix (the old cargo-gc.conf) and a key of a
        # target that is not implemented are unknown keys.
        for text in ("MAX_IDLE=48h\n", "GO_BUILD_MAX_IDLE=48h\n"):
            self.write_conf_raw(text)
            with self.assertRaisesRegex(tcd_common.ConfigError, "unknown setting", msg=text):
                tc_home_files.load_settings(self.home, TOOL)

    def test_bad_settings_delete_nothing(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.write_conf("MAX_IDLE=1h\nMIN_KEEP=2h\n")
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 1)
        self.assertIn("shorter than RUST_BUILD_MIN_KEEP", out)
        self.assertTrue(self.present("aa/0123456789abcd"))


class RootTest(Base):
    def test_cst_template(self) -> None:
        self.assertEqual(tc_build_tree.resolve_root(self.home, "cst toolchains"), self.root)

    def test_other_template_is_refused(self) -> None:
        for text in (
            f'[build]\nbuild-dir = "{self.home}/.cache/cargo-build"\n',
            f'[build]\nbuild-dir = "{self.home}/elsewhere/{{workspace-path-hash}}"\n',
            '[build]\ntarget-dir = "/tmp/t"\n',
            "",
            "[build\n",
        ):
            self.write_cargo_config(text)
            with self.assertRaises(Refused, msg=text):
                tc_build_tree.resolve_root(self.home, "cst toolchains")

    def test_other_template_deletes_nothing(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.write_cargo_config('[build]\nbuild-dir = "{workspace-root}/target-build"\n')
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 1)
        self.assertIn("not the one CST writes", out)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_no_config(self) -> None:
        os.unlink(os.path.join(self.home, ".cargo/config.toml"))
        with self.assertRaises(Refused):
            tc_build_tree.resolve_root(self.home, "cst toolchains")

    def test_missing_root_is_nothing_to_do(self) -> None:
        shutil.rmtree(self.root)
        self.assertIsNone(tc_build_tree.resolve_root(self.home, "cst toolchains"))
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)

    def test_symlinked_root_is_refused(self) -> None:
        real = os.path.join(self.tmp, "real-root")
        os.rename(self.root, real)
        os.symlink(real, self.root)
        with self.assertRaises(Refused):
            tc_build_tree.resolve_root(self.home, "cst toolchains")
        # A symlink higher up (~/.cache) is refused too.
        os.unlink(self.root)
        cache = os.path.join(self.home, ".cache")
        os.rename(cache, os.path.join(self.tmp, "real-cache"))
        os.symlink(os.path.join(self.tmp, "real-cache"), cache)
        os.makedirs(os.path.join(self.tmp, "real-cache/cargo-build"), exist_ok=True)
        with self.assertRaises(Refused):
            tc_build_tree.resolve_root(self.home, "cst toolchains")


class ScanTest(Base):
    def test_name_filter_and_symlinks(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.folder("AA/0123456789abcd", 100)  # uppercase: not cargo's
        self.folder("aa/0123456789abc", 100)  # 13 digits
        self.folder("aaa/0123456789abcd", 100)
        self.folder("zz/0123456789abcd", 100)
        os.makedirs(os.path.join(self.root, "notes"))
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("mine")
        outside = os.path.join(self.tmp, "precious")
        os.makedirs(outside)
        with open(os.path.join(outside, "data"), "w") as f:
            f.write("keep me")
        self.age(outside, 500)
        os.symlink(outside, os.path.join(self.root, "bb"))  # top-level link
        os.makedirs(os.path.join(self.root, "cc"))
        os.symlink(outside, os.path.join(self.root, "cc/0123456789abcd"))  # leaf link
        folders, _trash, others = tc_build_tree.scan(self.root, NOW)
        self.assertEqual([f.key for f in folders], ["aa/0123456789abcd"])
        why = dict(others)
        self.assertIn("symlink", why["bb"])
        self.assertIn("symlink", why["cc/0123456789abcd"])
        self.assertIn("hash", why["AA"])
        self.assertIn("hash", why["aa/0123456789abc"])
        self.assertIn("hash", why["README"])
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0)
        self.assertFalse(self.present("aa/0123456789abcd"))
        for kept in ("AA/0123456789abcd", "aa/0123456789abc", "aaa/0123456789abcd", "zz/0123456789abcd", "notes"):
            self.assertTrue(self.present(kept), kept)
        self.assertTrue(os.path.islink(os.path.join(self.root, "bb")))
        self.assertTrue(os.path.islink(os.path.join(self.root, "cc/0123456789abcd")))
        self.assertTrue(os.path.exists(os.path.join(outside, "data")))
        self.assertIn("left alone: bb", out)

    def test_activity_is_newest_mtime_anywhere(self) -> None:
        path = self.folder("aa/0123456789abcd", 100)
        deep = os.path.join(path, "debug/incremental/foo-1/s-abc/query-cache.bin")
        os.makedirs(os.path.dirname(deep))
        with open(deep, "w") as f:
            f.write("x")
        os.utime(deep, (NOW - 3 * H, NOW - 3 * H))
        for d in ("debug/incremental/foo-1/s-abc", "debug/incremental/foo-1", "debug/incremental"):
            os.utime(os.path.join(path, d), (NOW - 100 * H, NOW - 100 * H))
        f = tc_build_tree.scan(self.root, NOW)[0][0]
        self.assertAlmostEqual(f.activity, NOW - 3 * H, delta=1)

    def test_future_mtimes_are_ignored(self) -> None:
        path = self.folder("aa/0123456789abcd", 100)
        lock = os.path.join(path, "debug/.cargo-build-lock")
        os.utime(lock, (NOW + 1000 * H, NOW + 1000 * H))
        f = tc_build_tree.scan(self.root, NOW)[0][0]
        self.assertAlmostEqual(f.activity, NOW - 100 * H, delta=1)
        self.assertEqual(f.future, 1)

    def test_size_does_not_follow_symlinks(self) -> None:
        path = self.folder("aa/0123456789abcd", 100, kib=64)
        big = os.path.join(self.tmp, "big")
        with open(big, "wb") as f:
            f.write(os.urandom(4 << 20))
        os.symlink(big, os.path.join(path, "debug/link-to-big"))
        f = tc_build_tree.scan(self.root, NOW)[0][0]
        self.assertLess(f.size, 1 << 20)
        self.assertGreaterEqual(f.size, 64 * 1024)

    def test_lock_files(self) -> None:
        path = self.folder("aa/0123456789abcd", 1)
        self.folder("aa/0123456789abcd", 1, profile="release")
        os.makedirs(os.path.join(path, "x86_64-unknown-linux-gnu/debug"))
        open(os.path.join(path, "x86_64-unknown-linux-gnu/debug/.cargo-build-lock"), "w").close()
        open(os.path.join(path, "release/.cargo-lock"), "w").close()
        rel = [os.path.relpath(p, path) for p in tc_build_tree.lock_files(path)]
        self.assertEqual(
            rel,
            [
                "debug/.cargo-build-lock",
                "release/.cargo-build-lock",
                "release/.cargo-lock",
                "x86_64-unknown-linux-gnu/debug/.cargo-build-lock",
            ],
        )


class DecideTest(Base):
    def f(self, key: str, age_h: float, gib_: float) -> tcd_build.Folder:
        x = tcd_build.Folder(self.root, *key.split("/"))
        x.size = int(gib_ * (1 << 30))
        x.activity = NOW - age_h * H
        return x

    def pol(self, idle: int = 48, total: float = 150, keep: int = 2) -> tcd_build.RustPolicy:
        return tcd_build.RustPolicy(max_idle=idle * H, max_total=int(total * (1 << 30)), min_keep=keep * H)

    def actions(self, plan: Sequence[tuple[tcd_build.Folder, str, str]]) -> dict[str, str]:
        return {f.key: a for f, a, _ in plan}

    def test_idle(self) -> None:
        plan = tcd_build.decide(
            [self.f("aa/00000000000001", 49, 1), self.f("aa/00000000000002", 47, 1)], self.pol(), NOW, {}
        )
        self.assertEqual(self.actions(plan), {"aa/00000000000001": "delete", "aa/00000000000002": "keep"})

    def test_cap_least_recent_first(self) -> None:
        fs = [
            self.f("aa/00000000000001", 10, 50),
            self.f("aa/00000000000002", 30, 50),
            self.f("aa/00000000000003", 20, 50),
            self.f("aa/00000000000004", 5, 50),
        ]
        plan = tcd_build.decide(fs, self.pol(total=120), NOW, {})
        # 200 GiB, cap 120: the two least recently active go (30h, then 20h).
        self.assertEqual(
            self.actions(plan),
            {
                "aa/00000000000002": "delete",
                "aa/00000000000003": "delete",
                "aa/00000000000001": "keep",
                "aa/00000000000004": "keep",
            },
        )
        self.assertEqual(next(f.key for f, _, _ in plan), "aa/00000000000002")

    def test_cap_never_touches_min_keep(self) -> None:
        fs = [self.f("aa/00000000000001", 1, 100), self.f("aa/00000000000002", 0.5, 100)]
        plan = tcd_build.decide(fs, self.pol(total=50), NOW, {})
        self.assertEqual(set(self.actions(plan).values()), {"keep"})
        self.assertTrue(all("MIN_KEEP" in why for _, _, why in plan))

    def test_cap_off(self) -> None:
        fs = [self.f("aa/00000000000001", 10, 500)]
        self.assertEqual(self.actions(tcd_build.decide(fs, self.pol(total=0), NOW, {})), {"aa/00000000000001": "keep"})

    def test_in_use_and_problem_are_kept(self) -> None:
        a, b = self.f("aa/00000000000001", 100, 1), self.f("aa/00000000000002", 100, 1)
        b.problem = "1 entry on another filesystem"
        plan = tcd_build.decide([a, b], self.pol(), NOW, {"aa/00000000000001": [(42, "cargo")]})
        self.assertEqual(set(self.actions(plan).values()), {"keep"})
        why = {f.key: w for f, _, w in plan}
        self.assertIn("cargo (pid 42)", why["aa/00000000000001"])
        self.assertIn("another filesystem", why["aa/00000000000002"])

    def test_cap_skips_busy_and_takes_next(self) -> None:
        fs = [self.f("aa/00000000000001", 30, 100), self.f("aa/00000000000002", 20, 100)]
        plan = tcd_build.decide(fs, self.pol(total=150), NOW, {"aa/00000000000001": [(1, "rustc")]})
        self.assertEqual(self.actions(plan), {"aa/00000000000001": "keep", "aa/00000000000002": "delete"})


class ApplyTest(Base):
    def test_plan_deletes_nothing(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        rc, out = self.run_main("prune", "plan")
        self.assertEqual(rc, 0)
        self.assertIn("apply would delete 1 folder(s)", out)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_idle_and_parent_cleanup(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 1)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "aa")))  # empty XX removed too
        self.assertTrue(self.present("bb/0123456789abcd"))
        self.assertEqual(sorted(os.listdir(self.root)), ["bb"])  # no trash left
        self.assertIn("deleted aa/0123456789abcd", out)
        rc, out = self.run_main("status", "--brief")
        self.assertIn("deleted 1 folder(s)", out)

    def test_cap_and_min_keep_end_to_end(self) -> None:
        for key, age in (
            ("a1/00000000000001", 30),
            ("a2/00000000000002", 20),
            ("a3/00000000000003", 10),
            ("a4/00000000000004", 1),
        ):
            self.folder(key, age, kib=256)
        sizes = {f.key: f.size for f in tc_build_tree.scan(self.root, NOW)[0]}
        # Room for two folders: the two least recently active must go.
        cap = sizes["a3/00000000000003"] + sizes["a4/00000000000004"] + 1024
        self.write_conf(f"MAX_TOTAL={cap // 1024}K\nMAX_IDLE=48h\n")
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual([k for k in sizes if self.present(k)], ["a3/00000000000003", "a4/00000000000004"])
        # A cap below even the newest folder: the one inside MIN_KEEP stays.
        self.write_conf("MAX_TOTAL=1K\n")
        rc, out = self.run_main("prune", "apply")
        self.assertEqual([k for k in sizes if self.present(k)], ["a4/00000000000004"])
        self.assertIn("still above MAX_TOTAL", out)

    def test_held_cargo_lock_is_skipped(self) -> None:
        path = self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 90)
        lock = os.path.join(path, "debug/.cargo-build-lock")
        # Another process holds it, as cargo does during a build.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1])\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(60)\n",
                lock,
            ],
            stdout=subprocess.PIPE,
            text=True,
            cwd=self.tmp,
        )
        assert holder.stdout is not None
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            rc, out = self.run_main("prune", "apply")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.present("aa/0123456789abcd"))
        self.assertFalse(self.present("bb/0123456789abcd"))
        self.assertIn("skipped aa/0123456789abcd", out)
        # The open file shows in /proc too, or the lock is seen: either way kept.
        self.assertTrue("cargo lock held" in out or "in use" in out, out)
        # Released: the next run deletes it.
        rc, out = self.run_main("prune", "apply")
        self.assertFalse(self.present("aa/0123456789abcd"))

    def test_lock_held_in_this_process_without_proc(self) -> None:
        """The flock alone (no /proc hit) keeps a folder."""
        path = self.folder("aa/0123456789abcd", 100)
        tree = self.tree()
        fd = os.open(os.path.join(path, "debug/.cargo-build-lock"), os.O_RDONLY)
        try:
            # flock locks belong to the open file: a second open in this
            # process conflicts like another process would.
            fcntl.flock(fd, fcntl.LOCK_SH)
            with mock.patch.object(tree, "open_by", lambda root: ({}, 0)):
                result, detail = tree.delete(self.root, tc_build_tree.scan(self.root, NOW)[0][0], NOW, {}, None, None)
        finally:
            os.close(fd)
        self.assertEqual(result, "busy")
        self.assertIn("debug/.cargo-build-lock", detail)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_cwd_in_folder_is_in_use(self) -> None:
        path = self.folder("aa/0123456789abcd", 100)
        p = subprocess.Popen(["sleep", "60"], cwd=os.path.join(path, "debug"))
        try:
            users, _ = tc_processes.proc_users(self.root)
            self.assertIn("aa/0123456789abcd", users)
            self.assertEqual(users["aa/0123456789abcd"][0][1], "sleep")
            _rc, out = self.run_main("prune", "apply")
        finally:
            p.kill()
            p.wait()
        self.assertTrue(self.present("aa/0123456789abcd"))
        self.assertIn("in use", out)

    def test_leftover_trash_is_deleted(self) -> None:
        trash = os.path.join(self.root, tcd_build.TRASH_PREFIX + "aa0123456789abcd-1")
        other = os.path.join(self.root, tcd_build.TRASH_PREFIX + "mine")  # not our name format
        os.makedirs(os.path.join(trash, "debug"))
        os.makedirs(other)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(trash))
        self.assertTrue(os.path.isdir(other))

    def test_prefixes(self) -> None:
        self.assertEqual(
            tcd_build.prefixes("/var/home/u/.cache/cargo-build"),
            ("/home/u/.cache/cargo-build", "/var/home/u/.cache/cargo-build"),
        )
        self.assertEqual(tcd_build.prefixes("/srv/x"), ("/srv/x",))


class TimerTest(Base):
    def unit(self, name: str) -> str:
        return os.path.join(self.home, ".config/systemd/user", name)

    def test_enable_disable(self) -> None:
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        prog = os.path.join(self.home, ".local/lib/cst/cst-toolchains")
        # The copy is the zipapp `install` builds from this checkout.
        with open(prog, "rb") as f:
            self.assertEqual(f.read(), build_python_zipapp(REPO, "toolchains/cst-toolchains"))
        self.assertTrue(os.access(prog, os.X_OK))
        for u in (tcd_common.SERVICE, tcd_common.TIMER):
            with open(self.unit(u)) as f:
                self.assertTrue(f.read().startswith(tcd_common.MARKER))
        with open(self.unit(tcd_common.SERVICE)) as f:
            self.assertIn("ExecStart=/usr/bin/python3 -I %h/.local/lib/cst/cst-toolchains prune apply", f.read())
        self.assertIn(("daemon-reload",), self.calls)
        self.assertIn(("enable", "--now", tcd_common.TIMER), self.calls)
        # Again: nothing rewritten, no reload.
        self.calls.clear()
        rc, out = self.run_main("auto", "on")
        self.assertNotIn(("daemon-reload",), self.calls)
        self.assertIn("unchanged", out)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertIn(("disable", "--now", tcd_common.TIMER), self.calls)
        for p in (prog, self.unit(tcd_common.SERVICE), self.unit(tcd_common.TIMER)):
            self.assertFalse(os.path.exists(p), p)

    def test_enable_refuses_foreign_files(self) -> None:
        os.makedirs(os.path.dirname(self.unit(tcd_common.TIMER)))
        with open(self.unit(tcd_common.TIMER), "w") as f:
            f.write("[Timer]\nOnCalendar=daily\n")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1)
        self.assertIn("not written by CST", out)
        self.assertEqual(self.calls, [])
        rc, out = self.run_main("auto", "off")
        with open(self.unit(tcd_common.TIMER)) as f:
            self.assertIn("daily", f.read())  # left alone

    def test_enable_refuses_other_template(self) -> None:
        self.write_cargo_config('[build]\nbuild-dir = "/tmp/elsewhere"\n')
        self.write_conf_raw("PYTHON_UV_ENABLED=no\n")  # rust:build alone: nothing left to run
        rc, _out = self.run_main("auto", "on")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.unit(tcd_common.TIMER)))
        self.assertEqual(self.calls, [])


class _ScandirIter(Protocol):
    def __enter__(self) -> Iterator[os.DirEntry[str]]: ...
    def __exit__(self, *args: object) -> None: ...
    def __iter__(self) -> Iterator[os.DirEntry[str]]: ...


class ScanRobustTest(Base):
    def test_unreadable_top_folder_is_reported(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 100)
        bb = os.path.join(self.root, "bb")
        os.chmod(bb, 0)
        try:
            if os.access(bb, os.R_OK):
                self.skipTest("running with permission to read anything (root?)")
            folders, _, others = tc_build_tree.scan(self.root, NOW)
        finally:
            os.chmod(bb, 0o755)
        self.assertEqual([f.key for f in folders], ["aa/0123456789abcd"])
        self.assertIn("cannot read", dict(others)["bb"])

    def test_top_folder_removed_meanwhile(self) -> None:
        """A concurrent apply removes an emptied XX/ after scan listed the root."""
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 100)
        real = os.scandir
        gone = os.path.join(self.root, "bb")

        def scandir(path: str = ".") -> _ScandirIter:
            if path == gone:
                raise FileNotFoundError(2, "No such file or directory", path)
            return real(path)

        with mock.patch.object(os, "scandir", scandir):
            folders, _, others = tc_build_tree.scan(self.root, NOW)
        self.assertEqual([f.key for f in folders], ["aa/0123456789abcd"])
        self.assertEqual(others, [])
        with mock.patch.object(os, "scandir", scandir):
            rc, out = self.run_main("prune", "plan")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)


class CargoTieTest(Base):
    """cargo test holds no lock while the test programs run: a running cargo
    keeps the folder it builds in (found out with cargo metadata)."""

    def fake_cargo(self, body: str) -> str:
        """An executable named cargo in its own folder."""
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "cargo")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def proc(
        self,
        exe: str,
        cwd: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        pid: str = "4242",
        comm: str = "cargo",
    ) -> tcd_build.CargoProc:
        cwd = cwd or self.tmp
        return tcd_build.CargoProc(pid, comm, cwd, exe, tuple(args or ["cargo", "test"]), env or {})

    def answering(self, build_dir: str) -> str:
        return self.fake_cargo(f"""test "$RUSTUP_AUTO_INSTALL" = 0 || exit 3
echo '{{"packages": [], "build_directory": "{build_dir}"}}'
""")

    def test_folder_of_running_cargo_test_is_kept(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 100)
        self.cargo_running = [self.proc(self.answering(os.path.join(self.root, "aa/0123456789abcd")))]
        rc, out = self.run_main("prune", "plan")
        self.assertIn("cargo (pid 4242)", out)
        self.assertIn("apply would delete 1 folder(s)", out)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.present("aa/0123456789abcd"))
        self.assertFalse(self.present("bb/0123456789abcd"))
        self.assertIn("skipped aa/0123456789abcd: in use: cargo (pid 4242)", out)

    def test_cargo_starting_during_the_run_is_seen(self) -> None:
        """The check before each deletion asks again (not only at the start)."""
        self.folder("aa/0123456789abcd", 100)
        f = tc_build_tree.scan(self.root, NOW)[0][0]
        self.cargo_running = [self.proc(self.answering(f.path))]
        result, detail = self.tree().delete(self.root, f, NOW, {}, None, None)
        self.assertEqual(result, "busy")
        self.assertIn("cargo runs on it", detail)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_other_build_dir_does_not_count(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.cargo_running = [self.proc(self.answering(os.path.join(self.tmp, "elsewhere/aa/0123456789abcd")))]
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.present("aa/0123456789abcd"))

    def test_unknown_build_folder_stops_deleting(self) -> None:
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 90)
        bad = self.fake_cargo("echo 'error: could not find `Cargo.toml`' >&2\nexit 101\n")
        self.cargo_running = [self.proc(bad)]
        rc, out = self.run_main("prune", "plan")
        self.assertIn("its build folder is unknown", out)
        self.assertIn("Cargo.toml", out)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.present("aa/0123456789abcd"))
        self.assertTrue(self.present("bb/0123456789abcd"))
        self.assertEqual(out.count("stopped before"), 1)
        # Its working folder gone: unknown too.
        self.cargo_running = [self.proc(self.answering("/x"), cwd="/nonexistent/ws (deleted)")]
        rc, out = self.run_main("prune", "apply")
        self.assertIn("not reachable", out)
        self.assertTrue(self.present("bb/0123456789abcd"))
        # It ended: the next run deletes.
        self.cargo_running = []
        rc, out = self.run_main("prune", "apply")
        self.assertFalse(self.present("aa/0123456789abcd"))

    def test_users_are_checked_while_the_lock_is_held(self) -> None:
        path = self.folder("aa/0123456789abcd", 100)
        lock = os.path.join(path, "debug/.cargo-build-lock")
        seen: list[str] = []

        def check(
            root: str, cache: tcd_build.CargoCache
        ) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str, str | None, str]]]:
            fd = os.open(lock, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
            finally:
                os.close(fd)
            return {}, []

        tree = self.tree()
        with mock.patch.object(tree, "cargo_runs", check):
            result, _ = tree.delete(self.root, tc_build_tree.scan(self.root, NOW)[0][0], NOW, {}, None, None)
        self.assertEqual(result, "deleted")
        self.assertEqual(seen, ["held"])

    def test_query_uses_the_process_settings(self) -> None:
        exe = self.fake_cargo("exit 0\n")
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        p = self.proc(
            exe,
            cwd=ws,
            args=["cargo", "+nightly", "test", "--manifest-path", "sub/Cargo.toml", "--", "--manifest-path=no"],
            env={"CARGO_BUILD_BUILD_DIR": "/b/{workspace-path-hash}"},
        )
        query = tc_cargo.cargo_query(p, self.home)
        assert not isinstance(query, str)
        cmd, env, cwd = list(query.cmd), query.env, query.cwd
        self.assertEqual(cmd[:3], [exe, "+nightly", "metadata"])
        self.assertIn("--offline", cmd)
        self.assertEqual(cmd[-2:], ["--manifest-path", os.path.join(ws, "sub/Cargo.toml")])
        self.assertEqual(cwd, ws)
        self.assertEqual(env["CARGO_BUILD_BUILD_DIR"], "/b/{workspace-path-hash}")
        self.assertEqual(env["RUSTUP_AUTO_INSTALL"], "0")
        self.assertEqual(
            tcd_build.manifest_arg(["cargo", "test", "--manifest-path=a/Cargo.toml"], "/w"), "/w/a/Cargo.toml"
        )
        # cargo-nextest is not a cargo: the user's cargo is asked.
        cargo_home_bin = os.path.join(self.home, ".cargo/bin")
        os.makedirs(cargo_home_bin)
        shutil.copy(exe, cargo_home_bin)
        p2 = self.proc("/usr/bin/cargo-nextest", cwd=ws, comm="cargo-nextest")
        query2 = tc_cargo.cargo_query(p2, self.home)
        assert not isinstance(query2, str)
        cmd2 = query2.cmd
        self.assertEqual(cmd2[0], os.path.join(cargo_home_bin, "cargo"))

    def test_cache_asks_once_per_project(self) -> None:
        log = os.path.join(self.tmp, "asked")
        exe = self.fake_cargo(f'echo x >> {log}\necho \'{{"build_directory": "{self.root}/aa/0123456789abcd"}}\'\n')
        self.cargo_running = [self.proc(exe, pid="1"), self.proc(exe, pid="2")]
        cache: tcd_build.CargoCache = {}
        found: dict[str, list[tuple[str, str]]] = {}
        unknown: list[tuple[str, str, str | None, str]] = []
        for _ in range(3):
            found, unknown = self.tree().cargo_runs(self.root, cache)
        self.assertEqual(unknown, [])
        self.assertEqual(found, {"aa/0123456789abcd": [("1", "cargo"), ("2", "cargo")]})
        with open(log) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_cargo_procs_reads_a_real_process(self) -> None:
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        # A script named cargo: its process is named cargo too. It writes a file once it runs, and
        # the test reads /proc only then: while the kernel is still in exec, environ can read empty.
        ready = os.path.join(self.tmp, "ready")
        exe = self.fake_cargo(f": > {shlex.quote(ready)}\nsleep 60\n")
        env = dict(os.environ, CARGO_BUILD_BUILD_DIR="/b/{workspace-path-hash}")
        p = subprocess.Popen([exe, "test", "--manifest-path=sub/Cargo.toml"], cwd=ws, env=env)
        try:
            deadline = time.monotonic() + 30
            while not os.path.exists(ready) and p.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            procs = [x for x in self.real_cargo_procs() if x.pid == str(p.pid)]
        finally:
            p.kill()
            p.wait()
        self.assertEqual(len(procs), 1)
        x = procs[0]
        self.assertEqual((x.comm, x.cwd), ("cargo", ws))
        self.assertEqual(x.env.get("CARGO_BUILD_BUILD_DIR"), "/b/{workspace-path-hash}")
        assert x.cwd is not None
        self.assertEqual(tcd_build.manifest_arg(x.args, x.cwd), os.path.join(ws, "sub/Cargo.toml"))

    @unittest.skipUnless(shutil.which("cargo"), "cargo not installed")
    def test_real_cargo_metadata(self) -> None:
        """The real cargo names the folder of a project (reads only)."""
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(ws, "src"))
        with open(os.path.join(ws, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "ws"\nversion = "0.1.0"\nedition = "2021"\n')
        open(os.path.join(ws, "src/lib.rs"), "w").close()
        cargo_bin = shutil.which("cargo")
        assert cargo_bin is not None
        p = self.proc(cargo_bin, cwd=ws, env={"CARGO_BUILD_BUILD_DIR": self.root + "/{workspace-path-hash}"})
        q = tc_cargo.cargo_query(p, self.home)
        assert not isinstance(q, str)
        bd, why = tc_cargo.ask_cargo(q.cmd, q.env, q.cwd)
        if why and ("toolchain" in why or "rustup" in why):
            self.skipTest(why)
        self.assertIsNone(why)
        assert bd is not None
        rel = os.path.relpath(bd, self.root)
        self.assertRegex(rel, r"\A[0-9a-f]{2}/[0-9a-f]{14}\Z")
        self.assertFalse(os.path.exists(bd))  # metadata created nothing
        self.assertEqual(sorted(os.listdir(ws)), ["Cargo.toml", "src"])

    def test_rebuild_meanwhile_is_noted(self) -> None:
        """A cargo that waited for the lock rebuilds into a new folder at the
        same path: deleted, with a note (not a failure)."""
        self.folder("aa/0123456789abcd", 100)
        f = tc_build_tree.scan(self.root, NOW)[0][0]
        real = tc_build_tree.remove_tree

        def remove_and_rebuild(path: str, timeout: float | None = None) -> tuple[bool, str]:
            os.makedirs(os.path.join(f.path, "debug"))
            return real(path, timeout)

        tree = self.tree()
        with mock.patch.object(tree, "remove", remove_and_rebuild):
            result, detail = tree.delete(self.root, f, NOW, {}, None, None)
        self.assertEqual(result, "deleted")
        self.assertIn("meanwhile", detail)


class TurnedOffTest(Base):
    def unit(self, name: str) -> str:
        return os.path.join(self.home, ".config/systemd/user", name)

    def test_disable_is_remembered_by_setup(self) -> None:
        off = os.path.join(self.home, ".config/cst/toolchains-auto.off")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(off))
        # What snapshots setup runs: stays off, says so, no systemctl.
        self.calls.clear()
        rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertEqual(rc, 0, out)
        self.assertIn("left off", out)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(self.unit(tcd_common.TIMER)))
        rc, out = self.run_main("status", "--brief")
        self.assertIn("turned off by you", out)
        # Disabling again is fine.
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        # A manual enable turns it on and forgets the choice.
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(off))
        self.assertTrue(os.path.exists(self.unit(tcd_common.TIMER)))
        self.calls.clear()
        rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertEqual(rc, 0, out)
        self.assertIn(("enable", "--now", tcd_common.TIMER), self.calls)

    def test_unless_off_without_marker_enables(self) -> None:
        rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.unit(tcd_common.TIMER)))

    def test_flag_only_for_auto_on(self) -> None:
        rc, _out = self.run_main("prune", "apply", "--unless-off")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
