"""Unit tests for the rust:build target of toolchains/cst-toolchains: the
pruning of cargo's build folders (no root, no btrfs, no systemd needed).

HOME is a temporary folder with its own ~/.cargo/config.toml and
~/.cache/cargo-build; "now" is set through CST_TOOLCHAINS_NOW; systemctl is
replaced by a recorder. Nothing outside the temporary folder is touched.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s toolchains/tests -t . -v
"""

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLCHAINS = os.path.dirname(HERE)
TOOL = os.path.join(TOOLCHAINS, "cst-toolchains")
H = 3600
NOW = 2_000_000_000.0          # fixed "now" (2033); every mtime is set relative to it


def load():
    loader = importlib.machinery.SourceFileLoader("cst_toolchains", TOOL)
    spec = importlib.util.spec_from_loader("cst_toolchains", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.m.JOURNAL = False
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-toolchains-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.home, ".cache/cargo-build")
        os.makedirs(self.root)
        self.write_cargo_config(f'[build]\nbuild-dir = "{self.home}/.cache/cargo-build/{{workspace-path-hash}}"\n')
        self.env = {k: os.environ.get(k) for k in ("CST_TOOLCHAINS_NOW", "CST_TOOLCHAINS_HERE", "CARGO_HOME",
                                                  "CST_TOOLCHAINS_SYSTEMCTL", "XDG_RUNTIME_DIR")}
        os.environ["CST_TOOLCHAINS_NOW"] = str(NOW)
        os.environ["CST_TOOLCHAINS_HERE"] = "1"
        # Never the real user manager, also if a test misses the fake below.
        os.environ["CST_TOOLCHAINS_SYSTEMCTL"] = "/nonexistent/systemctl"
        os.environ.pop("CARGO_HOME", None)
        # status must never reach a real sccache server (their sockets live there).
        os.environ["XDG_RUNTIME_DIR"] = os.path.join(self.tmp, "no-runtime-dir")
        self.calls = []
        self.manager_env = ""
        self.m.systemctl = self.fake_systemctl
        # No real cargo process of the user may change a test's result:
        # tests that need some set self.cargo_running.
        self.real_cargo_procs = self.m.cargo_procs
        self.cargo_running = []
        self.m.cargo_procs = lambda uid=None: list(self.cargo_running)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def fake_systemctl(self, *args, check=False):
        if args == ("show-environment",):       # read only: the user manager's environment
            return self.manager_env
        self.calls.append(args)
        return ""

    # helpers
    def write_cargo_config(self, text):
        os.makedirs(os.path.join(self.home, ".cargo"), exist_ok=True)
        with open(os.path.join(self.home, ".cargo/config.toml"), "w") as f:
            f.write(text)

    def write_conf(self, text):
        """~/.config/cst/toolchains.conf; MAX_IDLE etc. get the RUST_BUILD_ prefix."""
        text = re.sub(r"(?m)^(MAX_IDLE|MAX_TOTAL|MIN_KEEP|ENABLED|ORPHANS|INCREMENTAL_IDLE)=", r"RUST_BUILD_\1=", text)
        path = os.path.join(self.home, ".config/cst/toolchains.conf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_conf_raw(self, text):
        path = os.path.join(self.home, ".config/cst/toolchains.conf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def folder(self, key, age_h, kib=64, profile="debug"):
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
    def age(path, age_h):
        t = NOW - age_h * H
        for d, dirs, files in os.walk(path):
            for n in files + dirs:
                os.utime(os.path.join(d, n), (t, t), follow_symlinks=False)
            os.utime(d, (t, t))

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main([*args, "--home", self.home])
        return rc, out.getvalue() + err.getvalue()

    def present(self, key):
        return os.path.isdir(os.path.join(self.root, key))


class SettingsTest(Base):
    def test_durations(self):
        p = self.m.parse_duration
        self.assertEqual(p("48h"), 48 * H)
        self.assertEqual(p("2d"), 48 * H)
        self.assertEqual(p("90m"), 5400)
        self.assertEqual(p(" 1.5h "), 5400)
        self.assertEqual(p("1w"), 7 * 24 * H)
        for bad in ("48", "h", "-1h", "1y", "", "1 hours"):
            with self.assertRaises(self.m.ConfigError, msg=bad):
                p(bad)

    def test_sizes(self):
        p = self.m.parse_size
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
            with self.assertRaises(self.m.ConfigError, msg=bad):
                p(bad)

    def test_conf_parsing(self):
        known = {"MAX_IDLE", "MAX_TOTAL", "MIN_KEEP"}
        raw = self.m.parse_conf('# comment\n\nMAX_IDLE = "24h"   # a day\nMAX_TOTAL=\'200G\'\n  MIN_KEEP=1h\n', known)
        self.assertEqual(raw, {"MAX_IDLE": "24h", "MAX_TOTAL": "200G", "MIN_KEEP": "1h"})
        with self.assertRaises(self.m.ConfigError):
            self.m.parse_conf("MAX_IDEL=24h\n", known)          # a typo is not ignored
        with self.assertRaises(self.m.ConfigError):
            self.m.parse_conf("just words\n", known)

    def test_policy_defaults_and_limits(self):
        s = self.m.Settings(self.home)
        rb = self.m.RustBuild
        self.assertEqual(s.source_of(rb), "defaults")
        self.assertTrue(s.enabled["rust:build"])
        self.assertEqual(s.policy["rust:build"], {"max_idle": 48 * H, "max_total": 150 << 30, "min_keep": 2 * H,
                                                  "orphans": True, "incremental_idle": 6 * H})
        self.write_conf("MAX_TOTAL=off\nMAX_IDLE=3d\n")
        s = self.m.Settings(self.home)
        pol = s.policy["rust:build"]
        self.assertEqual((pol["max_total"], pol["max_idle"]), (0, 72 * H))
        self.assertEqual(s.source_of(rb), "~/.config/cst/toolchains.conf")
        for text in ("MIN_KEEP=5m\n", "MAX_IDLE=1h\nMIN_KEEP=2h\n", "MAX_TOTAL=lots\n", "MAX_TOTAL=150\n",
                     "ENABLED=maybe\n", "ORPHANS=maybe\n", "INCREMENTAL_IDLE=1h\n", "INCREMENTAL_IDLE=6\n"):
            self.write_conf(text)
            with self.assertRaises(self.m.ConfigError, msg=text):
                self.m.Settings(self.home)
        # A key without the prefix (the old cargo-gc.conf) and a key of a
        # target that is not implemented are unknown keys.
        for text in ("MAX_IDLE=48h\n", "GO_BUILD_MAX_IDLE=48h\n"):
            self.write_conf_raw(text)
            with self.assertRaisesRegex(self.m.ConfigError, "unknown setting", msg=text):
                self.m.Settings(self.home)

    def test_bad_settings_delete_nothing(self):
        self.folder("aa/0123456789abcd", 100)
        self.write_conf("MAX_IDLE=1h\nMIN_KEEP=2h\n")
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 1)
        self.assertIn("shorter than RUST_BUILD_MIN_KEEP", out)
        self.assertTrue(self.present("aa/0123456789abcd"))


class RootTest(Base):
    def test_cst_template(self):
        self.assertEqual(self.m.resolve_root(self.home), self.root)

    def test_other_template_is_refused(self):
        for text in (f'[build]\nbuild-dir = "{self.home}/.cache/cargo-build"\n',
                     f'[build]\nbuild-dir = "{self.home}/elsewhere/{{workspace-path-hash}}"\n',
                     '[build]\ntarget-dir = "/tmp/t"\n',
                     "",
                     "[build\n"):
            self.write_cargo_config(text)
            with self.assertRaises(self.m.Refused, msg=text):
                self.m.resolve_root(self.home)

    def test_other_template_deletes_nothing(self):
        self.folder("aa/0123456789abcd", 100)
        self.write_cargo_config('[build]\nbuild-dir = "{workspace-root}/target-build"\n')
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 1)
        self.assertIn("not the one CST writes", out)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_no_config(self):
        os.unlink(os.path.join(self.home, ".cargo/config.toml"))
        with self.assertRaises(self.m.Refused):
            self.m.resolve_root(self.home)

    def test_missing_root_is_nothing_to_do(self):
        shutil.rmtree(self.root)
        self.assertIsNone(self.m.resolve_root(self.home))
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)

    def test_symlinked_root_is_refused(self):
        real = os.path.join(self.tmp, "real-root")
        os.rename(self.root, real)
        os.symlink(real, self.root)
        with self.assertRaises(self.m.Refused):
            self.m.resolve_root(self.home)
        # A symlink higher up (~/.cache) is refused too.
        os.unlink(self.root)
        cache = os.path.join(self.home, ".cache")
        os.rename(cache, os.path.join(self.tmp, "real-cache"))
        os.symlink(os.path.join(self.tmp, "real-cache"), cache)
        os.makedirs(os.path.join(self.tmp, "real-cache/cargo-build"), exist_ok=True)
        with self.assertRaises(self.m.Refused):
            self.m.resolve_root(self.home)


class ScanTest(Base):
    def test_name_filter_and_symlinks(self):
        self.folder("aa/0123456789abcd", 100)
        self.folder("AA/0123456789abcd", 100)          # uppercase: not cargo's
        self.folder("aa/0123456789abc", 100)           # 13 digits
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
        os.symlink(outside, os.path.join(self.root, "bb"))                   # top-level link
        os.makedirs(os.path.join(self.root, "cc"))
        os.symlink(outside, os.path.join(self.root, "cc/0123456789abcd"))   # leaf link
        folders, trash, others = self.m.scan(self.root, NOW)
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

    def test_activity_is_newest_mtime_anywhere(self):
        path = self.folder("aa/0123456789abcd", 100)
        deep = os.path.join(path, "debug/incremental/foo-1/s-abc/query-cache.bin")
        os.makedirs(os.path.dirname(deep))
        with open(deep, "w") as f:
            f.write("x")
        os.utime(deep, (NOW - 3 * H, NOW - 3 * H))
        for d in ("debug/incremental/foo-1/s-abc", "debug/incremental/foo-1", "debug/incremental"):
            os.utime(os.path.join(path, d), (NOW - 100 * H, NOW - 100 * H))
        f = self.m.scan(self.root, NOW)[0][0]
        self.assertAlmostEqual(f.activity, NOW - 3 * H, delta=1)

    def test_future_mtimes_are_ignored(self):
        path = self.folder("aa/0123456789abcd", 100)
        lock = os.path.join(path, "debug/.cargo-build-lock")
        os.utime(lock, (NOW + 1000 * H, NOW + 1000 * H))
        f = self.m.scan(self.root, NOW)[0][0]
        self.assertAlmostEqual(f.activity, NOW - 100 * H, delta=1)
        self.assertEqual(f.future, 1)

    def test_size_does_not_follow_symlinks(self):
        path = self.folder("aa/0123456789abcd", 100, kib=64)
        big = os.path.join(self.tmp, "big")
        with open(big, "wb") as f:
            f.write(os.urandom(4 << 20))
        os.symlink(big, os.path.join(path, "debug/link-to-big"))
        f = self.m.scan(self.root, NOW)[0][0]
        self.assertLess(f.size, 1 << 20)
        self.assertGreaterEqual(f.size, 64 * 1024)

    def test_lock_files(self):
        path = self.folder("aa/0123456789abcd", 1)
        self.folder("aa/0123456789abcd", 1, profile="release")
        os.makedirs(os.path.join(path, "x86_64-unknown-linux-gnu/debug"))
        open(os.path.join(path, "x86_64-unknown-linux-gnu/debug/.cargo-build-lock"), "w").close()
        open(os.path.join(path, "release/.cargo-lock"), "w").close()
        rel = [os.path.relpath(p, path) for p in self.m.lock_files(path)]
        self.assertEqual(rel, ["debug/.cargo-build-lock", "release/.cargo-build-lock", "release/.cargo-lock",
                               "x86_64-unknown-linux-gnu/debug/.cargo-build-lock"])


class DecideTest(Base):
    def f(self, key, age_h, gib_):
        x = self.m.Folder(self.root, *key.split("/"))
        x.size = int(gib_ * (1 << 30))
        x.activity = NOW - age_h * H
        return x

    def pol(self, idle=48, total=150, keep=2):
        return {"max_idle": idle * H, "max_total": int(total * (1 << 30)), "min_keep": keep * H}

    def actions(self, plan):
        return {f.key: a for f, a, _ in plan}

    def test_idle(self):
        plan = self.m.decide([self.f("aa/00000000000001", 49, 1), self.f("aa/00000000000002", 47, 1)],
                             self.pol(), NOW, {})
        self.assertEqual(self.actions(plan), {"aa/00000000000001": "delete", "aa/00000000000002": "keep"})

    def test_cap_least_recent_first(self):
        fs = [self.f("aa/00000000000001", 10, 50), self.f("aa/00000000000002", 30, 50),
              self.f("aa/00000000000003", 20, 50), self.f("aa/00000000000004", 5, 50)]
        plan = self.m.decide(fs, self.pol(total=120), NOW, {})
        # 200 GiB, cap 120: the two least recently active go (30h, then 20h).
        self.assertEqual(self.actions(plan), {"aa/00000000000002": "delete", "aa/00000000000003": "delete",
                                              "aa/00000000000001": "keep", "aa/00000000000004": "keep"})
        self.assertEqual([f.key for f, _, _ in plan][0], "aa/00000000000002")

    def test_cap_never_touches_min_keep(self):
        fs = [self.f("aa/00000000000001", 1, 100), self.f("aa/00000000000002", 0.5, 100)]
        plan = self.m.decide(fs, self.pol(total=50), NOW, {})
        self.assertEqual(set(self.actions(plan).values()), {"keep"})
        self.assertTrue(all("MIN_KEEP" in why for _, _, why in plan))

    def test_cap_off(self):
        fs = [self.f("aa/00000000000001", 10, 500)]
        self.assertEqual(self.actions(self.m.decide(fs, self.pol(total=0), NOW, {})), {"aa/00000000000001": "keep"})

    def test_in_use_and_problem_are_kept(self):
        a, b = self.f("aa/00000000000001", 100, 1), self.f("aa/00000000000002", 100, 1)
        b.problem = "1 entry on another filesystem"
        plan = self.m.decide([a, b], self.pol(), NOW, {"aa/00000000000001": [(42, "cargo")]})
        self.assertEqual(set(self.actions(plan).values()), {"keep"})
        why = {f.key: w for f, _, w in plan}
        self.assertIn("cargo (pid 42)", why["aa/00000000000001"])
        self.assertIn("another filesystem", why["aa/00000000000002"])

    def test_cap_skips_busy_and_takes_next(self):
        fs = [self.f("aa/00000000000001", 30, 100), self.f("aa/00000000000002", 20, 100)]
        plan = self.m.decide(fs, self.pol(total=150), NOW, {"aa/00000000000001": [(1, "rustc")]})
        self.assertEqual(self.actions(plan), {"aa/00000000000001": "keep", "aa/00000000000002": "delete"})


class ApplyTest(Base):
    def test_plan_deletes_nothing(self):
        self.folder("aa/0123456789abcd", 100)
        rc, out = self.run_main("prune", "plan")
        self.assertEqual(rc, 0)
        self.assertIn("apply would delete 1 folder(s)", out)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_idle_and_parent_cleanup(self):
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 1)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "aa")))      # empty XX removed too
        self.assertTrue(self.present("bb/0123456789abcd"))
        self.assertEqual(sorted(os.listdir(self.root)), ["bb"])               # no trash left
        self.assertIn("deleted aa/0123456789abcd", out)
        rc, out = self.run_main("status", "--brief")
        self.assertIn("deleted 1 folder(s)", out)

    def test_cap_and_min_keep_end_to_end(self):
        for key, age in (("a1/00000000000001", 30), ("a2/00000000000002", 20), ("a3/00000000000003", 10),
                         ("a4/00000000000004", 1)):
            self.folder(key, age, kib=256)
        sizes = {f.key: f.size for f in self.m.scan(self.root, NOW)[0]}
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

    def test_held_cargo_lock_is_skipped(self):
        path = self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 90)
        lock = os.path.join(path, "debug/.cargo-build-lock")
        # Another process holds it, as cargo does during a build.
        holder = subprocess.Popen([sys.executable, "-I", "-c",
                                   "import fcntl, sys, time\n"
                                   "f = open(sys.argv[1])\n"
                                   "fcntl.flock(f, fcntl.LOCK_EX)\n"
                                   "print('held', flush=True)\n"
                                   "time.sleep(60)\n", lock],
                                  stdout=subprocess.PIPE, text=True, cwd=self.tmp)
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

    def test_lock_held_in_this_process_without_proc(self):
        """The flock alone (no /proc hit) keeps a folder."""
        path = self.folder("aa/0123456789abcd", 100)
        self.m.proc_users = lambda root, uid=None: ({}, 0)
        fd = os.open(os.path.join(path, "debug/.cargo-build-lock"), os.O_RDONLY)
        try:
            # flock locks belong to the open file: a second open in this
            # process conflicts like another process would.
            fcntl.flock(fd, fcntl.LOCK_SH)
            result, detail = self.m.delete_folder(self.root, self.m.scan(self.root, NOW)[0][0], NOW, self.home, {})
        finally:
            os.close(fd)
        self.assertEqual(result, "busy")
        self.assertIn("debug/.cargo-build-lock", detail)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_cwd_in_folder_is_in_use(self):
        path = self.folder("aa/0123456789abcd", 100)
        p = subprocess.Popen(["sleep", "60"], cwd=os.path.join(path, "debug"))
        try:
            users, _ = self.m.proc_users(self.root)
            self.assertIn("aa/0123456789abcd", users)
            self.assertEqual(users["aa/0123456789abcd"][0][1], "sleep")
            rc, out = self.run_main("prune", "apply")
        finally:
            p.kill()
            p.wait()
        self.assertTrue(self.present("aa/0123456789abcd"))
        self.assertIn("in use", out)

    def test_leftover_trash_is_deleted(self):
        trash = os.path.join(self.root, self.m.TRASH_PREFIX + "aa0123456789abcd-1")
        other = os.path.join(self.root, self.m.TRASH_PREFIX + "mine")   # not our name format
        os.makedirs(os.path.join(trash, "debug"))
        os.makedirs(other)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(trash))
        self.assertTrue(os.path.isdir(other))

    def test_prefixes(self):
        self.assertEqual(self.m.prefixes("/var/home/u/.cache/cargo-build"),
                         ("/home/u/.cache/cargo-build", "/var/home/u/.cache/cargo-build"))
        self.assertEqual(self.m.prefixes("/srv/x"), ("/srv/x",))


class TimerTest(Base):
    def unit(self, name):
        return os.path.join(self.home, ".config/systemd/user", name)

    def test_enable_disable(self):
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        prog = os.path.join(self.home, ".local/lib/cst/cst-toolchains")
        with open(prog) as f, open(TOOL) as g:
            self.assertEqual(f.read(), g.read())
        self.assertTrue(os.access(prog, os.X_OK))
        for u in (self.m.SERVICE, self.m.TIMER):
            with open(self.unit(u)) as f:
                self.assertTrue(f.read().startswith(self.m.MARKER))
        with open(self.unit(self.m.SERVICE)) as f:
            self.assertIn("ExecStart=/usr/bin/python3 -I %h/.local/lib/cst/cst-toolchains prune apply", f.read())
        self.assertIn(("daemon-reload",), self.calls)
        self.assertIn(("enable", "--now", self.m.TIMER), self.calls)
        # Again: nothing rewritten, no reload.
        self.calls.clear()
        rc, out = self.run_main("auto", "on")
        self.assertNotIn(("daemon-reload",), self.calls)
        self.assertIn("unchanged", out)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertIn(("disable", "--now", self.m.TIMER), self.calls)
        for p in (prog, self.unit(self.m.SERVICE), self.unit(self.m.TIMER)):
            self.assertFalse(os.path.exists(p), p)

    def test_enable_refuses_foreign_files(self):
        os.makedirs(os.path.dirname(self.unit(self.m.TIMER)))
        with open(self.unit(self.m.TIMER), "w") as f:
            f.write("[Timer]\nOnCalendar=daily\n")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1)
        self.assertIn("not written by CST", out)
        self.assertEqual(self.calls, [])
        rc, out = self.run_main("auto", "off")
        with open(self.unit(self.m.TIMER)) as f:
            self.assertIn("daily", f.read())                  # left alone

    def test_enable_refuses_other_template(self):
        self.write_cargo_config('[build]\nbuild-dir = "/tmp/elsewhere"\n')
        self.write_conf_raw("PYTHON_UV_ENABLED=no\n")      # rust:build alone: nothing left to run
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.unit(self.m.TIMER)))
        self.assertEqual(self.calls, [])


class ScanRobustTest(Base):
    def test_unreadable_top_folder_is_reported(self):
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 100)
        bb = os.path.join(self.root, "bb")
        os.chmod(bb, 0)
        try:
            if os.access(bb, os.R_OK):
                self.skipTest("running with permission to read anything (root?)")
            folders, _, others = self.m.scan(self.root, NOW)
        finally:
            os.chmod(bb, 0o755)
        self.assertEqual([f.key for f in folders], ["aa/0123456789abcd"])
        self.assertIn("cannot read", dict(others)["bb"])

    def test_top_folder_removed_meanwhile(self):
        """A concurrent apply removes an emptied XX/ after scan listed the root."""
        self.folder("aa/0123456789abcd", 100)
        self.folder("bb/0123456789abcd", 100)
        real = os.scandir
        gone = os.path.join(self.root, "bb")

        def scandir(path="."):
            if path == gone:
                raise FileNotFoundError(2, "No such file or directory", path)
            return real(path)
        with mock.patch.object(self.m.os, "scandir", scandir):
            folders, _, others = self.m.scan(self.root, NOW)
        self.assertEqual([f.key for f in folders], ["aa/0123456789abcd"])
        self.assertEqual(others, [])
        with mock.patch.object(self.m.os, "scandir", scandir):
            rc, out = self.run_main("prune", "plan")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)


class CargoTieTest(Base):
    """cargo test holds no lock while the test programs run: a running cargo
    keeps the folder it builds in (found out with cargo metadata)."""

    def fake_cargo(self, body):
        """An executable named cargo in its own folder."""
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "cargo")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def proc(self, exe, cwd=None, args=None, env=None, pid="4242", comm="cargo"):
        cwd = cwd or self.tmp
        return {"pid": pid, "comm": comm, "cwd": cwd, "exe": exe,
                "args": args or ["cargo", "test"], "env": env or {}}

    def answering(self, build_dir):
        return self.fake_cargo(f"""test "$RUSTUP_AUTO_INSTALL" = 0 || exit 3
echo '{{"packages": [], "build_directory": "{build_dir}"}}'
""")

    def test_folder_of_running_cargo_test_is_kept(self):
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

    def test_cargo_starting_during_the_run_is_seen(self):
        """The check before each deletion asks again (not only at the start)."""
        self.folder("aa/0123456789abcd", 100)
        f = self.m.scan(self.root, NOW)[0][0]
        self.cargo_running = [self.proc(self.answering(f.path))]
        result, detail = self.m.delete_folder(self.root, f, NOW, self.home, {})
        self.assertEqual(result, "busy")
        self.assertIn("cargo runs on it", detail)
        self.assertTrue(self.present("aa/0123456789abcd"))

    def test_other_build_dir_does_not_count(self):
        self.folder("aa/0123456789abcd", 100)
        self.cargo_running = [self.proc(self.answering(os.path.join(self.tmp, "elsewhere/aa/0123456789abcd")))]
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.present("aa/0123456789abcd"))

    def test_unknown_build_folder_stops_deleting(self):
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

    def test_users_are_checked_while_the_lock_is_held(self):
        path = self.folder("aa/0123456789abcd", 100)
        lock = os.path.join(path, "debug/.cargo-build-lock")
        seen = []

        def check(root, home, cache):
            fd = os.open(lock, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
            finally:
                os.close(fd)
            return {}, []
        self.m.cargo_users = check
        result, _ = self.m.delete_folder(self.root, self.m.scan(self.root, NOW)[0][0], NOW, self.home, {})
        self.assertEqual(result, "deleted")
        self.assertEqual(seen, ["held"])

    def test_query_uses_the_process_settings(self):
        exe = self.fake_cargo("exit 0\n")
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        p = self.proc(exe, cwd=ws, args=["cargo", "+nightly", "test", "--manifest-path", "sub/Cargo.toml", "--",
                                         "--manifest-path=no"],
                      env={"CARGO_BUILD_BUILD_DIR": "/b/{workspace-path-hash}"})
        key, (cmd, env, cwd) = self.m.cargo_query(p, self.home)
        self.assertEqual(cmd[:3], [exe, "+nightly", "metadata"])
        self.assertIn("--offline", cmd)
        self.assertEqual(cmd[-2:], ["--manifest-path", os.path.join(ws, "sub/Cargo.toml")])
        self.assertEqual(cwd, ws)
        self.assertEqual(env["CARGO_BUILD_BUILD_DIR"], "/b/{workspace-path-hash}")
        self.assertEqual(env["RUSTUP_AUTO_INSTALL"], "0")
        self.assertEqual(self.m.manifest_arg(["cargo", "test", "--manifest-path=a/Cargo.toml"], "/w"),
                         "/w/a/Cargo.toml")
        # cargo-nextest is not a cargo: the user's cargo is asked.
        cargo_home_bin = os.path.join(self.home, ".cargo/bin")
        os.makedirs(cargo_home_bin)
        shutil.copy(exe, cargo_home_bin)
        p2 = self.proc("/usr/bin/cargo-nextest", cwd=ws, comm="cargo-nextest")
        _, (cmd2, _, _) = self.m.cargo_query(p2, self.home)
        self.assertEqual(cmd2[0], os.path.join(cargo_home_bin, "cargo"))

    def test_cache_asks_once_per_project(self):
        log = os.path.join(self.tmp, "asked")
        exe = self.fake_cargo(f"echo x >> {log}\necho '{{\"build_directory\": \"{self.root}/aa/0123456789abcd\"}}'\n")
        self.cargo_running = [self.proc(exe, pid="1"), self.proc(exe, pid="2")]
        cache = {}
        for _ in range(3):
            found, unknown = self.m.cargo_users(self.root, self.home, cache)
        self.assertEqual(unknown, [])
        self.assertEqual(found, {"aa/0123456789abcd": [("1", "cargo"), ("2", "cargo")]})
        with open(log) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_cargo_procs_reads_a_real_process(self):
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        # A script named cargo: its process is named cargo too.
        exe = self.fake_cargo("sleep 60\n")
        env = dict(os.environ, CARGO_BUILD_BUILD_DIR="/b/{workspace-path-hash}")
        p = subprocess.Popen([exe, "test", "--manifest-path=sub/Cargo.toml"], cwd=ws, env=env)
        try:
            procs = [x for x in self.real_cargo_procs() if x["pid"] == str(p.pid)]
        finally:
            p.kill()
            p.wait()
        self.assertEqual(len(procs), 1)
        x = procs[0]
        self.assertEqual((x["comm"], x["cwd"]), ("cargo", ws))
        self.assertEqual(x["env"].get("CARGO_BUILD_BUILD_DIR"), "/b/{workspace-path-hash}")
        self.assertEqual(self.m.manifest_arg(x["args"], x["cwd"]), os.path.join(ws, "sub/Cargo.toml"))

    @unittest.skipUnless(shutil.which("cargo"), "cargo not installed")
    def test_real_cargo_metadata(self):
        """The real cargo names the folder of a project (reads only)."""
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(ws, "src"))
        with open(os.path.join(ws, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "ws"\nversion = "0.1.0"\nedition = "2021"\n')
        open(os.path.join(ws, "src/lib.rs"), "w").close()
        p = self.proc(shutil.which("cargo"), cwd=ws,
                      env={"CARGO_BUILD_BUILD_DIR": self.root + "/{workspace-path-hash}"})
        key, q = self.m.cargo_query(p, self.home)
        bd, why = self.m.ask_cargo(*q)
        if why and ("toolchain" in why or "rustup" in why):
            self.skipTest(why)
        self.assertIsNone(why)
        rel = os.path.relpath(bd, self.root)
        self.assertRegex(rel, r"\A[0-9a-f]{2}/[0-9a-f]{14}\Z")
        self.assertFalse(os.path.exists(bd))                # metadata created nothing
        self.assertEqual(sorted(os.listdir(ws)), ["Cargo.toml", "src"])

    def test_rebuild_meanwhile_is_noted(self):
        """A cargo that waited for the lock rebuilds into a new folder at the
        same path: deleted, with a note (not a failure)."""
        self.folder("aa/0123456789abcd", 100)
        f = self.m.scan(self.root, NOW)[0][0]
        real = self.m.remove_tree

        def remove_and_rebuild(path, timeout=None):
            os.makedirs(os.path.join(f.path, "debug"))
            return real(path, timeout)
        self.m.remove_tree = remove_and_rebuild
        result, detail = self.m.delete_folder(self.root, f, NOW, self.home, {})
        self.assertEqual(result, "deleted")
        self.assertIn("meanwhile", detail)


class TurnedOffTest(Base):
    def unit(self, name):
        return os.path.join(self.home, ".config/systemd/user", name)

    def test_disable_is_remembered_by_setup(self):
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
        self.assertFalse(os.path.exists(self.unit(self.m.TIMER)))
        rc, out = self.run_main("status", "--brief")
        self.assertIn("turned off by you", out)
        # Disabling again is fine.
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        # A manual enable turns it on and forgets the choice.
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(off))
        self.assertTrue(os.path.exists(self.unit(self.m.TIMER)))
        self.calls.clear()
        rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertEqual(rc, 0, out)
        self.assertIn(("enable", "--now", self.m.TIMER), self.calls)

    def test_unless_off_without_marker_enables(self):
        rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.unit(self.m.TIMER)))

    def test_flag_only_for_auto_on(self):
        rc, out = self.run_main("prune", "apply", "--unless-off")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
