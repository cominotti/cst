"""Unit tests for the newer rules of toolchains/cst-toolchains, all in
scratch homes with fake programs (no real cache, cargo config or rustup
folder is touched):

- rust:build orphans: build folders that no workspace under ~/Workspace
  claims any more (real git in scratch repositories, a fake cargo that
  answers `cargo metadata`), and the fail-safe when the search is incomplete;
- rust:build incremental pruning: only <profile>/incremental of idle folders;
- python:uv: `uv cache prune` through a fake uv;
- the rust toolchains report of `status` (nothing is removed);
- the opt-in lean debug info of `config`;
- the opt-in sccache item of `config` and `status` (the rustc-wrapper key in
  [build], the wrapper file, the per-server statistics) and the wrapper
  script itself (toolchains/rustc-wrapper) with a fake sccache and fake
  compilers.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s toolchains/tests -t . -v
"""

import contextlib
import fcntl
import hashlib
import io
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from toolchains.tests.test_prune_rust_build import NOW, H, Base  # noqa: E402

FAKE_CARGO = r"""#!/bin/sh
# A fake cargo: `metadata ... --manifest-path M` answers with a build folder
# derived from M's folder (sha256, 2 + 14 hex digits, like cargo's layout).
echo "$*" >>"$FAKE_CARGO_LOG"
m=""; prev=""
for a in "$@"; do [ "$prev" = "--manifest-path" ] && m="$a"; prev="$a"; done
d=$(dirname "$m")
case "$d" in
    *fail*) echo "error: failed to parse manifest at $m" >&2; exit 101 ;;
    *fuzz*) printf "error: current package believes it's in a workspace when it's not:\nAlternatively, add an empty [workspace] table\n" >&2; exit 101 ;;
    *slow*) sleep 5 ;;
esac
h=$(printf %s "$d" | sha256sum | cut -c1-16)
pk="{\"manifest_path\": \"$m\"}"
if [ -f "$d/members" ]; then
    while read -r x; do pk="$pk, {\"manifest_path\": \"$d/$x/Cargo.toml\"}"; done <"$d/members"
fi
printf '{"packages": [%s], "workspace_root": "%s", "build_directory": "%s/%s/%s"}\n' \
    "$pk" "$d" "$FAKE_ROOT" "$(echo "$h" | cut -c1-2)" "$(echo "$h" | cut -c3-16)"
"""

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def key_of(folder):
    h = hashlib.sha256(folder.encode()).hexdigest()[:16]
    return f"{h[:2]}/{h[2:]}"


class RulesBase(Base):
    def setUp(self):
        super().setUp()
        self.ws = os.path.join(self.home, "Workspace")
        self.cargo_log = os.path.join(self.tmp, "cargo.log")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        env = dict(GIT_ENV, FAKE_ROOT=self.root, FAKE_CARGO_LOG=self.cargo_log,
                   PATH=self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("RUSTUP_HOME", None)
        os.environ.pop("UV_CACHE_DIR", None)
        cargo_bin = os.path.join(self.home, ".cargo/bin")
        os.makedirs(cargo_bin, exist_ok=True)
        self.exe(os.path.join(cargo_bin, "cargo"), FAKE_CARGO)

    @staticmethod
    def exe(path, text):
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)
        return path

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def repo(self, rel, crates=("",)):
        """A git repository under ~/Workspace with one commit and a
        Cargo.toml in each of CRATES (relative folders)."""
        path = os.path.join(self.ws, rel)
        os.makedirs(path)
        for c in crates:
            os.makedirs(os.path.join(path, c), exist_ok=True)
            with open(os.path.join(path, c, "Cargo.toml"), "w") as f:
                f.write('[package]\nname = "x"\nversion = "0.1.0"\n')
        self.git("init", "-q", path)
        self.git("add", "-A", cwd=path)
        self.git("commit", "-q", "-m", "init", cwd=path)
        return path

    def worktree(self, repo, path):
        self.git("worktree", "add", "-q", "--detach", path, cwd=repo)
        return path

    def asked(self):
        try:
            with open(self.cargo_log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []


class OrphanTest(RulesBase):
    def test_orphans_are_deleted_claimed_kept(self):
        main = self.repo("proj")
        wt = self.worktree(main, os.path.join(self.ws, "proj-wt"))
        # A linked worktree inside target/: never walked, but git names it.
        nested = self.worktree(main, os.path.join(main, "target/proof/wt"))
        for d in (main, wt, nested):
            self.folder(key_of(d), 10)
        self.folder("ee/0123456789abcd", 10)             # nobody's
        self.folder("ef/0123456789abcd", 1)              # nobody's, but within MIN_KEEP
        rc, out = self.run_main("prune", "plan")
        self.assertEqual(rc, 0, out)
        self.assertIn("3 folder(s) claimed by a workspace, 2 not claimed", out)
        self.assertRegex(out, r"delete .*ee/0123456789abcd orphan: no workspace claims it")
        self.assertRegex(out, r"keep .*ef/0123456789abcd active .* \(within MIN_KEEP\)")
        self.assertTrue(all(f"--offline --no-deps" in a for a in self.asked()))
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.present("ee/0123456789abcd"))
        for k in (key_of(main), key_of(wt), key_of(nested), "ef/0123456789abcd"):
            self.assertTrue(self.present(k), k)
        # The worktree is removed: its folder is an orphan from now on.
        self.git("worktree", "remove", "--force", wt, cwd=main)
        rc, out = self.run_main("prune", "apply")
        self.assertFalse(self.present(key_of(wt)))
        self.assertTrue(self.present(key_of(nested)))

    def test_members_are_covered_and_caches_skipped(self):
        main = self.repo("ws", crates=("", "crates/a", "crates/b", "fuzz", "vendor-home/registry/src/x"))
        with open(os.path.join(main, "members"), "w") as f:
            f.write("crates/a\ncrates/b\n")
        open(os.path.join(main, "vendor-home/registry/CACHEDIR.TAG"), "w").close()
        self.folder(key_of(main), 10)
        self.folder("ee/0123456789abcd", 10)
        rc, out = self.run_main("prune", "plan")
        self.assertEqual(rc, 0, out)
        asked = "\n".join(self.asked())
        self.assertNotIn("crates/a", asked)                   # answered by the workspace root
        self.assertNotIn("vendor-home", asked)                # a cache folder: not searched
        self.assertIn("fuzz", asked)                           # asked, builds nothing: not a failure
        self.assertIn("1 folder(s) claimed by a workspace, 1 not claimed", out)
        self.assertNotIn("NOT looked for", out)

    def assert_no_orphans(self, out, why):
        self.assertIn("orphans: NOT looked for in this run", out)
        self.assertIn(why, out)
        self.assertNotIn("orphan: no workspace claims it", out)

    def test_failed_metadata_means_no_orphans(self):
        main = self.repo("proj", crates=("", "tools/fail"))
        self.folder(key_of(main), 10)
        self.folder("ee/0123456789abcd", 10)
        rc, out = self.run_main("prune", "plan")
        self.assert_no_orphans(out, "failed to parse manifest")
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.present("ee/0123456789abcd"))
        self.assertIn("NOT looked for", out)

    def test_timeout_means_no_orphans(self):
        main = self.repo("slow")
        self.folder(key_of(main), 10)
        self.folder("ee/0123456789abcd", 10)
        self.m.CMD_TIMEOUT = 1
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("took longer than", out)
        self.assertTrue(self.present("ee/0123456789abcd"))

    def test_repository_that_git_cannot_list_means_no_orphans(self):
        main = self.repo("proj")
        os.makedirs(os.path.join(self.ws, "broken/.git"))           # an empty .git folder
        self.folder(key_of(main), 10)
        self.folder("ee/0123456789abcd", 10)
        rc, out = self.run_main("prune", "apply")
        self.assertIn("git worktree list", out)
        self.assertTrue(self.present("ee/0123456789abcd"))

    def test_no_workspace_or_no_claim_means_no_orphans(self):
        self.folder("ee/0123456789abcd", 10)
        rc, out = self.run_main("prune", "plan")
        self.assert_no_orphans(out, "~/Workspace not found")
        os.makedirs(self.ws)
        rc, out = self.run_main("prune", "plan")
        self.assert_no_orphans(out, "no workspace claims any folder")
        rc, out = self.run_main("prune", "apply")
        self.assertTrue(self.present("ee/0123456789abcd"))

    def test_rule_can_be_turned_off(self):
        main = self.repo("proj")
        self.folder(key_of(main), 10)
        self.folder("ee/0123456789abcd", 10)
        self.write_conf("ORPHANS=no\n")
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("orphans: the rule is off", out)
        self.assertTrue(self.present("ee/0123456789abcd"))
        self.assertEqual(self.asked(), [])

    def test_in_use_orphan_is_kept(self):
        main = self.repo("proj")
        self.folder(key_of(main), 10)
        self.folder("ee/0123456789abcd", 10)
        plan = self.m.decide(self.m.scan(self.root, NOW)[0], self.m.Settings(self.home).policy["rust:build"], NOW,
                             {"ee/0123456789abcd": [(7, "rustc")]}, {key_of(main): [main]})
        self.assertEqual({f.key: a for f, a, _ in plan}["ee/0123456789abcd"], "keep")

    @unittest.skipUnless(shutil.which("cargo") and shutil.which("rustup"), "cargo not installed")
    def test_real_cargo_names_the_same_folder(self):
        """The real cargo (read only) answers with a folder in the root."""
        real = os.path.expanduser("~/.rustup")
        os.environ["RUSTUP_HOME"] = real          # read only: the installed toolchains
        os.unlink(os.path.join(self.home, ".cargo/bin/cargo"))
        main = self.repo("real")
        os.makedirs(os.path.join(main, "src"))
        open(os.path.join(main, "src/lib.rs"), "w").close()
        ws = self.m.claimed_folders(self.root, self.home)
        if ws.incomplete and ("toolchain" in ws.incomplete or "rustup" in ws.incomplete):
            self.skipTest(ws.incomplete)
        self.assertEqual(ws.incomplete, "")
        self.assertEqual(len(ws.claimed), 1)
        self.assertRegex(next(iter(ws.claimed)), r"\A[0-9a-f]{2}/[0-9a-f]{14}\Z")
        self.assertEqual(os.listdir(self.root), [])              # metadata created nothing


class IncrementalTest(RulesBase):
    def build_folder(self, key, age_h):
        path = self.folder(key, age_h)
        for rel in ("debug/incremental/foo-1/s-abc/dep-graph.bin",
                    "x86_64-unknown-linux-gnu/debug/incremental/bar-2/s-def/query-cache.bin",
                    "x86_64-unknown-linux-gnu/debug/deps/libbar.rlib"):
            p = os.path.join(path, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(os.urandom(32 * 1024))
        self.age(path, age_h)
        return path

    def test_measure_finds_incremental(self):
        self.build_folder("aa/0123456789abcd", 10)
        f = self.m.scan(self.root, NOW)[0][0]
        self.assertEqual(sorted(os.path.relpath(p, f.path) for p, _ in f.inc),
                         ["debug/incremental", "x86_64-unknown-linux-gnu/debug/incremental"])
        self.assertGreaterEqual(f.inc_size, 64 * 1024)
        self.assertLess(f.inc_size, f.size)

    def test_idle_folder_loses_only_incremental(self):
        path = self.build_folder("aa/0123456789abcd", 10)
        self.build_folder("bb/0123456789abcd", 3)            # used 3h ago: untouched
        before = self.m.scan(self.root, NOW)[0]
        activity = {f.key: f.activity for f in before}
        rc, out = self.run_main("prune", "plan")
        self.assertRegex(out, r"trim .*aa/0123456789abcd incremental of an idle folder \(idle 10.0h; frees")
        self.assertIn("and the incremental folders of 1 more", out)
        self.assertTrue(os.path.isdir(os.path.join(path, "debug/incremental")))
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("deleted the incremental folders of aa/0123456789abcd", out)
        self.assertFalse(os.path.exists(os.path.join(path, "debug/incremental")))
        self.assertFalse(os.path.exists(os.path.join(path, "x86_64-unknown-linux-gnu/debug/incremental")))
        self.assertTrue(os.path.isfile(os.path.join(path, "debug/deps/libfoo-0123.rlib")))
        self.assertTrue(os.path.isfile(os.path.join(path, "x86_64-unknown-linux-gnu/debug/deps/libbar.rlib")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "bb/0123456789abcd/debug/incremental")))
        after = {f.key: f for f in self.m.scan(self.root, NOW)[0]}
        # The folder's last activity did not move: MAX_IDLE still counts from the last build.
        self.assertEqual(after["aa/0123456789abcd"].activity, activity["aa/0123456789abcd"])
        self.assertEqual(after["aa/0123456789abcd"].inc, [])
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".")], [])     # no trash left
        rc, out = self.run_main("status", "--brief")
        self.assertIn("incremental of 1 folder(s)", out)

    def test_off_and_locked(self):
        path = self.build_folder("aa/0123456789abcd", 10)
        self.write_conf("INCREMENTAL_IDLE=off\n")
        rc, out = self.run_main("prune", "apply")
        self.assertTrue(os.path.isdir(os.path.join(path, "debug/incremental")))
        self.write_conf("INCREMENTAL_IDLE=6h\n")
        fd = os.open(os.path.join(path, "debug/.cargo-build-lock"), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            rc, out = self.run_main("prune", "apply")
        finally:
            os.close(fd)
        self.assertEqual(rc, 0, out)
        self.assertIn("skipped aa/0123456789abcd: cargo lock held", out)
        self.assertTrue(os.path.isdir(os.path.join(path, "debug/incremental")))

    def test_leftover_of_a_trim_is_deleted(self):
        name = ".cst-toolchains-trash-aa0123456789abcd-inc0-123"
        self.assertTrue(self.m.TRASH_NAME.match(name))
        os.makedirs(os.path.join(self.root, name, "s-x"))
        rc, out = self.run_main("prune", "apply")
        self.assertIn(f"deleted leftover {name}", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, name)))

    def test_cap_counts_the_trim(self):
        f = self.m.Folder(self.root, "aa", "00000000000001")
        f.size, f.activity, f.inc = 100 << 30, NOW - 10 * H, [["x", 60 << 30]]
        g = self.m.Folder(self.root, "aa", "00000000000002")
        g.size, g.activity = 30 << 30, NOW - 20 * H
        pol = {"max_idle": 48 * H, "max_total": 80 << 30, "min_keep": 2 * H, "incremental_idle": 6 * H}
        # 130 GiB, trim frees 60: 70 <= 80, nothing else needed.
        acts = {x.key: a for x, a, _ in self.m.decide([f, g], pol, NOW, {})}
        self.assertEqual(acts, {"aa/00000000000001": "trim", "aa/00000000000002": "keep"})
        pol["max_total"] = 20 << 30
        acts = {x.key: a for x, a, _ in self.m.decide([f, g], pol, NOW, {})}
        self.assertEqual(acts, {"aa/00000000000001": "delete", "aa/00000000000002": "delete"})


class UvBase(RulesBase):
    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.home, ".cache/uv")
        self.uv_log = os.path.join(self.tmp, "uv.log")
        os.environ["FAKE_UV_LOG"] = self.uv_log
        self.fake_uv()

    def fake_uv(self, prune='echo "Removed 3 files (1.5MiB)"', help_extra=""):
        self.exe(os.path.join(self.bin, "uv"), f"""#!/bin/sh
echo "$* | $UV_LOCK_TIMEOUT" >>"$FAKE_UV_LOG"
case "$*" in
    --version) echo "uv 0.0.0-test" ;;
    "cache prune --help") echo "Usage: uv cache prune [OPTIONS]"; echo "      --force"; echo "{help_extra}" ;;
    "cache prune --dry-run"*) echo "Would remove 2 files (1.0MiB)" ;;
    "cache prune"*) {prune} ;;
esac
""")

    def uv_calls(self):
        try:
            with open(self.uv_log) as f:
                return [c for c in f.read().splitlines() if "--help" not in c and "--version" not in c]
        except FileNotFoundError:
            return []



class UvTest(UvBase):
    def test_missing_cache_is_nothing_to_do(self):
        rc, out = self.run_main("prune", "apply", "python:uv")
        self.assertEqual(rc, 0, out)
        self.assertIn("does not exist: nothing to do", out)
        self.assertEqual(self.uv_calls(), [])

    def test_prune_runs_on_this_cache_once_per_interval(self):
        os.makedirs(self.cache)
        rc, out = self.run_main("prune", "plan", "python:uv")
        self.assertEqual(rc, 0, out)
        self.assertIn("this uv has no --dry-run", out)
        self.assertEqual(self.uv_calls(), [])
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.uv_calls(), [f"cache prune --cache-dir {self.cache} --no-progress | 60"])
        self.assertIn("python:uv: uv: Removed 3 files (1.5MiB)", out)
        rc, out = self.run_main("status", "--brief")
        self.assertIn("python:uv: last prune: ", out)
        self.assertIn("removed 3 file(s) (2 MiB)", out)
        # The timer again within INTERVAL: not due. Named: runs.
        rc, out = self.run_main("prune", "apply")
        self.assertIn("not due", out)
        self.assertEqual(len(self.uv_calls()), 1)
        rc, out = self.run_main("prune", "apply", "python:uv")
        self.assertEqual(len(self.uv_calls()), 2)

    def test_dry_run_when_uv_has_one(self):
        os.makedirs(self.cache)
        self.fake_uv(help_extra="      --dry-run")
        rc, out = self.run_main("prune", "plan", "python")
        self.assertIn("uv: Would remove 2 files", out)
        self.assertTrue(self.uv_calls()[0].startswith("cache prune --dry-run"))

    def test_held_lock_skips(self):
        os.makedirs(self.cache)
        lock = os.path.join(self.cache, ".lock")
        open(lock, "w").close()
        fd = os.open(lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)          # what every running uv command holds
            rc, out = self.run_main("prune", "apply", "python:uv")
            rc2, out2 = self.run_main("prune", "plan", "python:uv")
        finally:
            os.close(fd)
        self.assertEqual(rc, 0, out)
        self.assertIn("a uv command is using the cache", out)
        self.assertIn("apply would skip it", out2)
        self.assertEqual(self.uv_calls(), [])

    def test_failures(self):
        os.makedirs(self.cache)
        # uv 0.12's own words when UV_LOCK_TIMEOUT runs out: a skip, retried next run.
        self.fake_uv(prune="echo 'Cache is currently in-use, waiting for other uv processes to finish'; "
                           "echo 'error: Timeout (60s) when waiting for lock on `/c` at `/c/.lock`, is another "
                           "uv process running? You can set `UV_LOCK_TIMEOUT` to increase the timeout.' >&2; exit 2")
        rc, out = self.run_main("prune", "apply", "python:uv")
        self.assertEqual(rc, 0, out)
        self.assertIn("uv waited 60s for its cache lock", out)
        self.assertIsNone(self.m.read_state(self.home, "python:uv"))
        self.fake_uv(prune="echo 'error: something else' >&2; exit 2")
        rc, out = self.run_main("prune", "apply", "python:uv")
        self.assertEqual(rc, 1, out)
        self.assertIn("uv cache prune failed: error: something else", out)
        # An error that names the lock but is not the timeout (a root-owned
        # cache after `sudo uv`) is a failure, not a lock wait.
        for err in ("error: failed to open file `/c/.lock`: Permission denied (os error 13)",
                    "error: Could not acquire lock: Read-only file system (os error 30)",
                    "error: the cache is in use by root"):
            self.fake_uv(prune=f"echo '{err}' >&2; exit 2")
            rc, out = self.run_main("prune", "apply", "python:uv")
            self.assertEqual(rc, 1, (err, out))
            self.assertIn(f"uv cache prune failed: {err}", out)
            self.assertNotIn("waited", out)

    def test_no_uv_is_refused(self):
        os.makedirs(self.cache)
        with mock.patch.dict(os.environ, {"PATH": self.tmp}):
            rc, out = self.run_main("prune", "apply", "python:uv")
            self.assertEqual(rc, 1, out)
            self.assertIn("uv not found on PATH", out)
            rc, out = self.run_main("status")
            self.assertIn("not found on PATH (prune refuses)", out)


class UvBudgetTest(UvBase):
    """The run's time budget (RUN_BUDGET): bounded targets first, and
    nothing started that cannot end before it."""

    def test_uv_runs_before_rust_build(self):
        os.makedirs(self.cache)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertLess(out.index("python:uv:"), out.index("rust:build:"))
        rc, out = self.run_main("prune", "plan")
        self.assertLess(out.index("== rust:build"), out.index("== python:uv"))      # plan keeps the order

    def test_used_up_budget_starts_nothing(self):
        os.makedirs(self.cache)
        self.folder("aa/0123456789abcd", 100)           # MAX_IDLE: would be deleted
        self.m.RUN_BUDGET = 0
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("python:uv: skipped: the run's time budget", out)
        self.assertIn("rust:build: stopped before aa/0123456789abcd: the run's time budget", out)
        self.assertIn("stopped at the time budget", out)
        self.assertTrue(self.present("aa/0123456789abcd"))
        self.assertEqual(self.uv_calls(), [])
        self.assertIsNone(self.m.read_state(self.home, "python:uv"))         # tried again next run

    def test_uv_timeout_follows_the_budget(self):
        os.makedirs(self.cache)
        seen = []
        t = self.m.PythonUv(self.home, NOW, {"interval": 0})
        t.deadline = time.monotonic() + 200
        t.run_uv = lambda uv, args, timeout: seen.append(timeout) or subprocess.CompletedProcess(args, 0, "", "")
        self.assertEqual(t.apply(), 0)
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 200)
        self.assertGreater(seen[0], 150)

    def test_rm_is_stopped_at_the_deadline(self):
        d = os.path.join(self.tmp, "big")
        os.makedirs(d)
        with mock.patch.object(self.m.subprocess, "run", side_effect=subprocess.TimeoutExpired("rm", 1)):
            ok, msg = self.m.remove_tree(d, 1)
        self.assertFalse(ok)
        self.assertIn("next run deletes the rest", msg)


class ManagerPathTest(UvBase):
    """auto on checks python:uv with the PATH the timer runs with (the user
    manager's), not with this command's (root's, from snapshots setup)."""

    def test_uv_on_the_managers_path_only(self):
        self.write_cargo_config('[build]\nbuild-dir = "/tmp/elsewhere"\n')     # rust:build refuses
        self.manager_env = f"LANG=C\nPATH={self.bin}:/usr/bin\n"
        with mock.patch.dict(os.environ, {"PATH": self.tmp}):
            rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("python:uv", out)
        self.assertIn("rust:build: build-dir", out)
        self.assertIn("each timer run tries it and reports it as refused", out)
        self.assertNotIn("the timer skips it", out)

    def test_uv_missing_on_the_managers_path(self):
        self.manager_env = "PATH=/usr/bin:/bin\n"
        rc, out = self.run_main("auto", "on")          # uv is on this command's PATH (self.bin)
        self.assertEqual(rc, 0, out)
        self.assertIn("python:uv: uv not found on PATH", out)
        self.assertIn("looked for in the PATH of your user manager, which the timer uses", out)

    def test_unreadable_manager_path_says_so(self):
        self.write_cargo_config('[build]\nbuild-dir = "/tmp/elsewhere"\n')
        self.manager_env = ""
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertIn("rust:build: build-dir", out)
        self.assertIn("which could not be read", out)
        with mock.patch.dict(os.environ, {"PATH": self.tmp}):
            self.write_conf_raw("RUST_BUILD_ENABLED=no\n")
            rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1, out)
        self.assertIn("uv not found on PATH", out)

    def test_quoted_value_is_not_parsed(self):
        self.manager_env = "PATH=$'/a\\nb'\n"
        self.assertIsNone(self.m.manager_path())
        self.manager_env = "PATH=/x:/y\n"
        self.assertEqual(self.m.manager_path(), "/x:/y")


class RecheckTest(RulesBase):
    """The plan uses the activity measured when the run started; a build can
    write to a folder (and end) before apply gets to it. Each folder is
    measured again while its locks are held, right before it is renamed."""

    build_folder = IncrementalTest.build_folder

    def touch_after_scan(self, rel, age_h):
        """Write REL (in the root) AGE_H hours before NOW, after apply has
        scanned and planned (at its first look at the processes)."""
        real = self.m.proc_users
        done = []

        def hooked(root, uid=None):
            if not done:
                done.append(1)
                p = os.path.join(self.root, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(b"x" * 4096)
                os.utime(p, (NOW - age_h * H, NOW - age_h * H))
            return real(root, uid)
        self.m.proc_users = hooked

    def test_built_after_the_scan_is_kept(self):
        self.folder("aa/0123456789abcd", 100)                      # MAX_IDLE: planned for deletion
        self.touch_after_scan("aa/0123456789abcd/debug/deps/libnew.rlib", 0.1)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.present("aa/0123456789abcd"), out)
        self.assertIn("not deleted: aa/0123456789abcd: measured again: active 6m ago (within MIN_KEEP)", out)
        self.assertIn("deleted 0 folder(s)", out)
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".")], [])

    def test_still_idle_after_the_scan_is_deleted(self):
        self.folder("aa/0123456789abcd", 100)
        self.touch_after_scan("aa/0123456789abcd/debug/deps/libnew.rlib", 60)      # still > MAX_IDLE
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.present("aa/0123456789abcd"), out)
        self.assertIn("deleted aa/0123456789abcd", out)

    def test_trim_rechecked(self):
        path = self.build_folder("aa/0123456789abcd", 10)          # INCREMENTAL_IDLE: planned trim
        self.touch_after_scan("aa/0123456789abcd/debug/incremental/foo-1/s-new/dep-graph.bin", 0.5)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("not trimmed: aa/0123456789abcd: measured again", out)
        self.assertTrue(os.path.isdir(os.path.join(path, "debug/incremental")))
        self.assertTrue(os.path.isdir(os.path.join(path, "x86_64-unknown-linux-gnu/debug/incremental")))

    def test_trim_uses_the_fresh_incremental_list(self):
        path = self.build_folder("aa/0123456789abcd", 10)
        # A new incremental folder (another profile) that is itself old: the
        # folder is still idle; the trim takes it too.
        self.touch_after_scan("aa/0123456789abcd/release/incremental/x/s/q.bin", 9)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        for rel in ("debug/incremental", "release/incremental", "x86_64-unknown-linux-gnu/debug/incremental"):
            self.assertFalse(os.path.exists(os.path.join(path, rel)), rel)
        self.assertTrue(os.path.isfile(os.path.join(path, "debug/deps/libfoo-0123.rlib")))

    def test_cap_takes_the_next_after_a_recheck(self):
        self.write_conf("MAX_TOTAL=100K\nINCREMENTAL_IDLE=off\n")
        self.folder("aa/00000000000001", 20, kib=96)                # least recently active: the cap's first pick
        self.folder("aa/00000000000002", 10, kib=96)
        self.touch_after_scan("aa/00000000000001/debug/deps/libnew.rlib", 0.2)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("not deleted: aa/00000000000001: measured again", out)
        self.assertTrue(self.present("aa/00000000000001"))
        self.assertFalse(self.present("aa/00000000000002"), out)

    def test_delete_folder_honours_a_failed_recheck(self):
        self.folder("aa/0123456789abcd", 100)
        f = self.m.scan(self.root, NOW)[0][0]
        result, detail = self.m.delete_folder(self.root, f, NOW, self.home, {}, lambda: "measured again: x")
        self.assertEqual((result, detail), ("changed", "measured again: x"))
        self.assertTrue(self.present("aa/0123456789abcd"))


class ToolchainReportTest(RulesBase):
    def test_report(self):
        rh = os.path.join(self.tmp, "rustup")
        os.environ["RUSTUP_HOME"] = rh
        host = "x86_64-unknown-linux-gnu"
        for n in ("1.95.0", "1.98.1", "nightly-2026-06-01", "nightly", "stable"):
            p = os.path.join(rh, "toolchains", f"{n}-{host}", "bin")
            os.makedirs(p)
            with open(os.path.join(p, "rustc"), "wb") as f:
                f.write(b"x" * 4096)
        with open(os.path.join(rh, "settings.toml"), "w") as f:
            f.write(f'version = "12"\ndefault_toolchain = "stable-{host}"\n\n[overrides]\n')
        a = self.repo("a")
        with open(os.path.join(a, "rust-toolchain.toml"), "w") as f:
            f.write('[toolchain]\nchannel = "1.98.1"\ncomponents = ["clippy"]\n')
        b = self.repo("b")
        with open(os.path.join(b, "rust-toolchain"), "w") as f:
            f.write("nightly-2026-06-01\n")
        c = self.repo("c")
        with open(os.path.join(c, "rust-toolchain.toml"), "w") as f:
            f.write('[toolchain]\nchannel = "1.80.0"\n')
        lines = "\n".join(self.m.toolchain_lines(self.home))
        self.assertIn("default:  stable", lines)
        self.assertRegex(lines, r"1\.98\.1 +pinned by ~/Workspace/a/rust-toolchain.toml")
        self.assertRegex(lines, r"nightly-2026-06-01 +pinned by ~/Workspace/b/rust-toolchain")
        self.assertRegex(lines, r"stable +the default")
        self.assertIn("rustup toolchain uninstall 1.95.0 nightly\n", lines + "\n")
        self.assertIn("pinned but not installed (the next build there downloads it): 1.80.0", lines)
        self.assertEqual(len(os.listdir(os.path.join(rh, "toolchains"))), 5)        # nothing removed
        rc, out = self.run_main("status")
        self.assertIn("== rust toolchains (a report: CST never removes one)", out)

    def test_named_in_build_files_is_not_offered(self):
        rh = os.path.join(self.tmp, "rustup")
        os.environ["RUSTUP_HOME"] = rh
        host = "x86_64-unknown-linux-gnu"
        for n in ("1.95.0", "1.98.0", "1.99.1", "nightly-2026-06-01", "nightly-2026-08-21", "nightly", "stable"):
            os.makedirs(os.path.join(rh, "toolchains", f"{n}-{host}", "bin"))
        with open(os.path.join(rh, "settings.toml"), "w") as f:
            f.write(f'default_toolchain = "stable-{host}"\n')
        a = self.repo("lush")
        with open(os.path.join(a, "Makefile"), "w") as f:
            f.write("GTK_LUSH_PUBLIC_API_TOOLCHAIN ?= nightly-2026-06-01\nCARGO_FUZZ ?= cargo +nightly fuzz\n")
        os.makedirs(os.path.join(a, ".github/workflows"))
        with open(os.path.join(a, ".github/workflows/ci.yml"), "w") as f:
            f.write("jobs:\n  msrv:\n    steps:\n      - uses: dtolnay/rust-toolchain@1.98.0\n")
        os.makedirs(os.path.join(a, "scripts"))
        with open(os.path.join(a, "scripts/bench.sh"), "w") as f:
            f.write("RUSTUP_TOOLCHAIN=1.99.1 cargo bench   # not 1.95.0.1, not 21.95.0\n")
        lines = "\n".join(self.m.toolchain_lines(self.home))
        self.assertRegex(lines, r"nightly-2026-06-01 +named in ~/Workspace/lush/Makefile")
        self.assertRegex(lines, r"nightly +named in ~/Workspace/lush/Makefile \(not offered for removal\)")
        self.assertRegex(lines, r"1\.98\.0 +named in ~/Workspace/lush/.github/workflows/ci.yml")
        self.assertRegex(lines, r"1\.99\.1 +named in ~/Workspace/lush/scripts/bench.sh")
        self.assertRegex(lines, r"1\.95\.0 +not pinned or named")
        self.assertIn("rustup toolchain uninstall 1.95.0 nightly-2026-08-21\n", lines + "\n")
        # The warning comes before the command, not after it.
        self.assertLess(lines.index("NOT CHECKED"), lines.index("rustup toolchain uninstall"))

    def test_too_many_files_is_incomplete(self):
        d = os.path.join(self.tmp, "f")
        os.makedirs(d)
        files = []
        for i in range(3):
            files.append(os.path.join(d, f"{i}.sh"))
            with open(files[-1], "w") as f:
                f.write("cargo +nightly build\n" if i == 2 else "true\n")
        self.assertEqual(self.m.named_in(files, ["nightly"]), ({"nightly": files[2]}, True))
        self.m.TOOL_FILES_MAX = 2
        self.assertEqual(self.m.named_in(files, ["nightly"]), ({}, False))

    def test_incomplete_search_offers_no_command(self):
        rh = os.path.join(self.tmp, "rustup")
        os.environ["RUSTUP_HOME"] = rh
        os.makedirs(os.path.join(rh, "toolchains", "1.95.0-x86_64-unknown-linux-gnu"))
        lines = "\n".join(self.m.toolchain_lines(self.home))       # no ~/Workspace: incomplete
        self.assertIn("no removal command, because the search was incomplete", lines)
        self.assertNotIn("rustup toolchain uninstall", lines)

    def test_toolchain_pattern(self):
        p = self.m.toolchain_pattern
        for name, text in (("nightly", "cargo +nightly fuzz"), ("nightly", "toolchain: nightly\n"),
                           ("nightly-2026-06-01", "TC ?= nightly-2026-06-01"), ("1.98.0", "@1.98.0\n"),
                           ("1.98.0", "cargo +1.98.0-x86_64-unknown-linux-gnu build")):
            self.assertTrue(p(name).search(text.encode()), (name, text))
        for name, text in (("nightly", "nightly-2026-06-01"), ("1.98.0", "1.98.0.1"), ("1.98.0", "21.98.0"),
                           ("nightly", "nightlyx"), ("stable", "unstable")):
            self.assertFalse(p(name).search(text.encode()), (name, text))

    def test_pinned_channel(self):
        d = os.path.join(self.tmp, "p")
        os.makedirs(d)
        cases = {"rust-toolchain.toml": ('[toolchain]\nchannel = "1.99.1"\n', "1.99.1"),
                 "rust-toolchain": ("stable\n", "stable")}
        for name, (text, want) in cases.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
            self.assertEqual(self.m.pinned_channel(os.path.join(d, name)), want)
        with open(os.path.join(d, "rust-toolchain.toml"), "w") as f:
            f.write("not = [toml\n")
        self.assertEqual(self.m.pinned_channel(os.path.join(d, "rust-toolchain.toml")), "")


class LeanDebugInfoTest(RulesBase):
    def cfg(self):
        with open(os.path.join(self.home, ".cargo/config.toml")) as f:
            return f.read()

    def test_off_by_default(self):
        before = self.cfg()
        rc, out = self.run_main("config", "plan")
        self.assertIn("lean debug info: off (opt-in", out)
        self.assertIn("compile everything once", out)
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cfg(), before)

    def test_setting_writes_once_and_flag_wins(self):
        self.write_conf_raw("RUST_LEAN_DEBUGINFO=yes\n")
        before = self.cfg()
        rc, out = self.run_main("config", "apply", "--no-lean-debuginfo")
        self.assertEqual(self.cfg(), before)
        rc, out = self.run_main("config", "plan")
        self.assertIn("write", out)
        self.assertIn('[profile.dev] debug = "line-tables-only"', out)
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        text = self.cfg()
        self.assertTrue(text.startswith(before))
        self.assertIn("# Added by toolchains-setup.sh (CST) for RUST_LEAN_DEBUGINFO=yes", text)
        data = tomllib.loads(text)
        self.assertEqual(data["profile"]["dev"]["debug"], "line-tables-only")
        self.assertIs(data["profile"]["dev"]["package"]["*"]["debug"], False)
        self.assertEqual(self.m.resolve_root(self.home), self.root)        # build-dir still read
        rc, out = self.run_main("config", "apply")
        self.assertIn("lean debug info already set", out)
        self.assertEqual(self.cfg(), text)
        # Turned off again: nothing is removed, and status says so.
        self.write_conf_raw("RUST_LEAN_DEBUGINFO=no\n")
        rc, out = self.run_main("config", "plan")
        self.assertIn("lean debug info is set", out)

    def test_flag_alone_turns_it_on(self):
        rc, out = self.run_main("config", "apply", "--lean-debuginfo")
        self.assertEqual(rc, 0, out)
        self.assertIn("compiles everything once", out)
        self.assertEqual(tomllib.loads(self.cfg())["profile"]["dev"]["debug"], "line-tables-only")

    def test_users_key_is_never_overwritten(self):
        with open(os.path.join(self.home, ".cargo/config.toml"), "a") as f:
            f.write("\n[profile.dev]\ndebug = true\n")
        rc, out = self.run_main("config", "apply", "--lean-debuginfo")
        self.assertEqual(rc, 0, out)
        self.assertIn("[profile.dev] debug = true is yours: left as it is", out)
        data = tomllib.loads(self.cfg())
        self.assertIs(data["profile"]["dev"]["debug"], True)
        self.assertIs(data["profile"]["dev"]["package"]["*"]["debug"], False)    # the other key: added

    def test_existing_table_needs_a_hand(self):
        with open(os.path.join(self.home, ".cargo/config.toml"), "a") as f:
            f.write("\n[profile.dev]\nopt-level = 1\n\n[profile.dev.package.\"*\"]\nopt-level = 2\n")
        before = self.cfg()
        rc, out = self.run_main("config", "plan", "--lean-debuginfo")
        self.assertIn("BY HAND", out)
        rc, out = self.run_main("config", "apply", "--lean-debuginfo")
        self.assertEqual(rc, 0, out)
        self.assertIn("add by hand", out)
        self.assertEqual(self.cfg(), before)

    def test_parse_args(self):
        p = self.m.parse_args
        self.assertIs(p(["config", "apply", "--lean-debuginfo"])["lean"], True)
        self.assertIs(p(["config", "plan", "--no-lean-debuginfo"])["lean"], False)
        self.assertIsNone(p(["config", "plan"])["lean"])
        for bad in (["config", "apply", "--lean"], ["config", "apply", "--lean-debuginfo", "--no-lean-debuginfo"]):
            with self.assertRaises(self.m.Usage, msg=bad):
                p(bad)

    def test_snapshots_setup_never_turns_it_on(self):
        self.write_conf_raw("RUST_LEAN_DEBUGINFO=yes\n")
        os.unlink(os.path.join(self.home, ".cargo/config.toml"))
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        script = ('set -Eeuo pipefail; SRC="$1"; H="$2"; PROG="$3"; . "${SRC}/setup-snapshots.bash"; target_user=u\n'
                  'runuser() { shift 3; "$@"; }\n'
                  'snap_home() { echo "$H"; }\n'
                  'snap_chrome() { :; }\n'
                  'snap_toolchains_config_preview\n'
                  'snap_user_configs "$H"\n')
        env = dict(os.environ, CST_TOOLCHAINS_SYSTEMCTL=os.path.join(self.bin, "systemctl"))
        self.exe(env["CST_TOOLCHAINS_SYSTEMCTL"], "#!/bin/sh\nexit 0\n")
        r = subprocess.run(["bash", "-c", script, "_", os.path.join(repo, "btrfs"), self.home, "cst btrfs"],
                           capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("lean debug info: off", r.stdout)
        self.assertNotIn("profile", self.cfg())
        self.assertIn("sccache: off", r.stdout)                    # nor sccache (RUST_SCCACHE=no)
        self.assertNotIn("rustc-wrapper", self.cfg())
        self.assertIn("build-dir", self.cfg())


if __name__ == "__main__":
    unittest.main()


class SccacheConfigTest(RulesBase):
    """The opt-in sccache item of `config`: the rustc-wrapper key in [build]
    and the wrapper file."""

    def setUp(self):
        super().setUp()
        self.cfg_path = os.path.join(self.home, ".cargo/config.toml")
        self.wrapper = os.path.join(self.home, ".local/libexec/cst/rustc-wrapper")

    def cfg(self):
        with open(self.cfg_path) as f:
            return f.read()

    def data(self):
        return tomllib.loads(self.cfg())

    def wrapper_value(self):
        return (self.data().get("build") or {}).get("rustc-wrapper")

    def test_off_by_default_and_plain_apply_changes_nothing(self):
        before = self.cfg()
        rc, out = self.run_main("config", "plan")
        self.assertIn("sccache: off (opt-in", out)
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cfg(), before)
        self.assertFalse(os.path.lexists(self.wrapper))

    def test_flag_inserts_into_the_existing_build_table(self):
        self.write_cargo_config('# mine\n[build]\nbuild-dir = "%s/.cache/cargo-build/{workspace-path-hash}"\n\n'
                                '[profile.dev]\ndebug = 1\n' % self.home)
        before = tomllib.loads(self.cfg())
        rc, out = self.run_main("config", "plan", "--sccache")
        self.assertIn('[build] rustc-wrapper = "~/.local/libexec/cst/rustc-wrapper"', out)
        self.assertIn("Apply with: cst toolchains config apply --sccache", out)
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 0, out)
        text = self.cfg()
        self.assertEqual(text.count("[build]"), 1)
        self.assertIn("# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes", text)
        data = tomllib.loads(text)
        self.assertEqual(data["build"]["rustc-wrapper"], self.wrapper)
        del data["build"]["rustc-wrapper"]
        self.assertEqual(data, before)
        # Right after build-dir, inside [build].
        lines = text.splitlines()
        self.assertTrue(lines[lines.index("[build]") + 3].startswith("rustc-wrapper = "), text)
        self.assertEqual(self.m.resolve_root(self.home), self.root)
        st = os.stat(self.wrapper)
        self.assertEqual(stat_mode(st), 0o755)
        with open(self.wrapper) as f:
            self.assertIn("cache_size=25G # cst:", f.read())
        # Idempotent.
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertIn("rustc-wrapper already set", out)
        self.assertIn("(sccache wrapper)", out)
        self.assertEqual(self.cfg(), text)
        rc, out = self.run_main("config", "plan", "--sccache")
        self.assertIn("sccache: rustc-wrapper set (--sccache)", out)
        self.assertNotIn("Apply with", out)

    def test_header_with_a_comment_and_no_keys(self):
        self.write_cargo_config("[build] # empty for now\n\n[net]\noffline = true\n")
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 0, out)
        data = self.data()
        self.assertEqual(data["build"], {"rustc-wrapper": self.wrapper})
        self.assertIs(data["net"]["offline"], True)

    def test_no_build_table_and_no_file(self):
        self.write_cargo_config('[net]\noffline = true')          # no final newline
        rc, out = self.run_main("config", "apply", "--sccache", "--lean-debuginfo")
        self.assertEqual(rc, 0, out)
        data = self.data()
        self.assertEqual(data["build"]["rustc-wrapper"], self.wrapper)
        self.assertIs(data["net"]["offline"], True)
        os.unlink(self.cfg_path)
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 0, out)
        data = self.data()
        self.assertEqual(data["build"]["rustc-wrapper"], self.wrapper)
        self.assertIn("build-dir", data["build"])                  # CST's build-dir came first

    def test_dotted_keys_need_a_hand(self):
        self.write_cargo_config("build.jobs = 4\n")
        before = self.cfg()
        rc, out = self.run_main("config", "plan", "--sccache")
        self.assertIn("BY HAND", out)
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 0, out)
        self.assertIn("put this into [build] by hand", out)
        self.assertEqual(self.cfg(), before)

    def test_foreign_wrapper_is_refused(self):
        self.write_cargo_config('[build]\nrustc-wrapper = "/usr/bin/sccache"\n')
        before = self.cfg()
        rc, out = self.run_main("config", "plan", "--sccache")
        self.assertIn('rustc-wrapper = "/usr/bin/sccache" is yours', out)
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 0, out)
        self.assertIn("is yours: left as it is", out)
        self.assertEqual(self.cfg(), before)
        self.assertFalse(os.path.lexists(self.wrapper))
        rc, out = self.run_main("config", "apply", "--no-sccache")
        self.assertEqual(self.cfg(), before)                       # never removed either

    def test_setting_turns_it_on_no_keeps_it_flag_removes_it(self):
        before = self.cfg()
        self.write_conf_raw("RUST_SCCACHE=yes\nRUST_SCCACHE_CACHE_SIZE=40GiB\n")
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.wrapper_value(), self.wrapper)
        with open(self.wrapper) as f:
            self.assertIn("cache_size=40G # cst:", f.read())
        # Back to no: the line stays, status says "on", the wrapper is kept current.
        self.write_conf_raw("RUST_SCCACHE=no\nRUST_SCCACHE_CACHE_SIZE=10G\n")
        rc, out = self.run_main("config", "plan")
        self.assertIn("sccache is on", out)
        self.assertIn("config apply --no-sccache does", out)
        self.assertIn("outdated", out)
        rc, out = self.run_main("config", "apply")
        self.assertEqual(self.wrapper_value(), self.wrapper)
        with open(self.wrapper) as f:
            self.assertIn("cache_size=10G # cst:", f.read())
        os.unlink(self.wrapper)                                    # a missing wrapper is put back
        rc, out = self.run_main("config", "apply")
        self.assertTrue(os.path.isfile(self.wrapper))
        rc, out = self.run_main("status")
        self.assertIn("sccache is on", out)
        # Removed only with the flag; the wrapper file stays (harmless).
        rc, out = self.run_main("config", "plan", "--no-sccache")
        self.assertIn("remove", out)
        self.assertIn("Apply with: cst toolchains config apply --no-sccache", out)
        rc, out = self.run_main("config", "apply", "--no-sccache")
        self.assertEqual(rc, 0, out)
        self.assertIn("removed:", out)
        self.assertEqual(self.cfg(), before)
        self.assertTrue(os.path.isfile(self.wrapper))
        rc, out = self.run_main("config", "apply", "--no-sccache")
        self.assertEqual(self.cfg(), before)

    def test_removal_of_csts_own_build_table(self):
        # Only the sccache step (config apply would add build-dir's [build] first).
        self.write_cargo_config("[net]\noffline = true\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.sccache_apply(self.home, None, True), 0)
        self.assertIn("\n\n# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes", self.cfg())
        self.assertEqual(self.data()["build"], {"rustc-wrapper": self.wrapper})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.sccache_apply(self.home, None, False), 0)
        self.assertEqual(self.cfg(), "[net]\noffline = true\n")

    def test_removal_of_csts_own_build_table_beside_a_subtable(self):
        # Only a [build.x] subtable: CST appends its own [build]; removal
        # takes the whole block back, the subtable stays.
        original = '[build.foo]\nbar = 1\n'
        self.write_cargo_config(original)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.sccache_apply(self.home, None, True), 0)
        self.assertEqual(self.data()["build"], {"foo": {"bar": 1}, "rustc-wrapper": self.wrapper})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.sccache_apply(self.home, None, False), 0)
        self.assertEqual(self.cfg(), original)
        self.assertNotIn("Added by", self.cfg())

    def test_removal_keeps_a_build_table_with_other_keys(self):
        self.write_cargo_config('[build]\njobs = 2\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.sccache_apply(self.home, None, True), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.sccache_apply(self.home, None, False), 0)
        self.assertEqual(self.cfg(), '[build]\njobs = 2\n')

    def test_removal_leaves_a_changed_line_alone(self):
        # Our value, but not CST's line (the user wrote it as a dotted key).
        self.write_cargo_config(f'build.build-dir = "{self.home}/.cache/cargo-build/{{workspace-path-hash}}"\n'
                                f'build.rustc-wrapper = "{self.wrapper}"\n')
        before = self.cfg()
        rc, out = self.run_main("config", "plan", "--no-sccache")
        self.assertIn("BY HAND", out)
        rc, out = self.run_main("config", "apply", "--no-sccache")
        self.assertEqual(rc, 0, out)
        self.assertIn("delete it yourself", out)
        self.assertEqual(self.cfg(), before)

    def test_foreign_wrapper_file_blocks_the_line(self):
        os.makedirs(os.path.dirname(self.wrapper))
        with open(self.wrapper, "w") as f:
            f.write("#!/bin/sh\nexec \"$@\"\n")
        before = self.cfg()
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 1, out)
        self.assertIn("not CST's file", out)
        self.assertEqual(self.cfg(), before)

    def test_missing_wrapper_source_blocks_the_line(self):
        self.m.wrapper_text = lambda size: None
        before = self.cfg()
        rc, out = self.run_main("config", "apply", "--sccache")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.cfg(), before)

    def test_sizes(self):
        p = self.m.parse_sccache_size
        self.assertEqual(p("25G"), "25G")
        self.assertEqual(p("25GiB"), "25G")
        self.assertEqual(p("1.5G"), "1536M")
        self.assertEqual(p("2T"), "2T")
        self.assertEqual(p("100K"), "100K")
        for bad in ("0", "off", "25"):
            with self.assertRaises(self.m.ConfigError, msg=bad):
                p(bad)
        self.write_conf_raw("RUST_SCCACHE_CACHE_SIZE=0\n")
        rc, out = self.run_main("config", "plan", "--sccache")
        self.assertIn("BAD SETTINGS", self.run_main("status")[1])

    def test_parse_args(self):
        p = self.m.parse_args
        self.assertIs(p(["config", "apply", "--sccache"])["sccache"], True)
        self.assertIs(p(["config", "plan", "--no-sccache", "--lean-debuginfo"])["sccache"], False)
        self.assertIsNone(p(["config", "plan"])["sccache"])
        for bad in (["config", "apply", "--sccache", "--no-sccache"], ["config", "apply", "--sccache=yes"]):
            with self.assertRaises(self.m.Usage, msg=bad):
                p(bad)

    def test_status_lines(self):
        os.environ["PATH"] = self.bin + os.pathsep + "/usr/bin:/bin"     # no real sccache
        rc, out = self.run_main("status")
        self.assertIn("sccache: off (opt-in", out)
        self.assertIn("sccache cache: ~/.cache/sccache not there yet", out)
        os.makedirs(os.path.join(self.home, ".cache/sccache/a"))
        with open(os.path.join(self.home, ".cache/sccache/a/x"), "wb") as f:
            f.write(b"x" * (3 << 20))
        rc, out = self.run_main("status")
        self.assertIn("sccache cache: ~/.cache/sccache 3 MiB", out)
        self.assertNotIn("sccache server", out)                    # no sccache on PATH, no socket
        self.assertNotIn("no server running", out)

    def test_status_finds_sccache_in_cargo_bin(self):
        os.environ["PATH"] = self.bin + os.pathsep + "/usr/bin:/bin"
        self.exe(os.path.join(self.home, ".cargo/bin/sccache"), "#!/bin/sh\nexit 0\n")
        rc, out = self.run_main("status")
        self.assertIn("sccache: no server running", out)

    def test_status_asks_each_running_server(self):
        run = tempfile.mkdtemp(prefix="cs")
        self.addCleanup(shutil.rmtree, run)
        sock_path = os.path.join(run, "cst-sccache-devbox.sock")
        if len(sock_path) > 100:
            self.skipTest("temporary folder too long for a unix socket")
        os.environ["XDG_RUNTIME_DIR"] = run
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(4)
        self.addCleanup(srv.close)
        # A stale socket of another environment: nobody listens, not asked.
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.path.join(run, "cst-sccache-host.sock"))
        stale.close()
        log = os.path.join(self.tmp, "sccache.log")
        self.exe(os.path.join(self.bin, "sccache"),
                 '#!/bin/sh\necho "$SCCACHE_SERVER_UDS $*" >>"%s"\n'
                 'printf "Compile requests                    12\\nCache hits                           7\\n'
                 'Cache hits (Rust)                    7\\nCache misses                         5\\n'
                 'Cache size                       1 GiB\\nMax cache size                  25 GiB\\n"\n' % log)
        rc, out = self.run_main("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("sccache server devbox: compile requests 12, cache hits 7, cache misses 5, "
                      "cache size 1 GiB, max cache size 25 GiB", out)
        self.assertNotIn("server host", out)
        with open(log) as f:
            self.assertEqual(f.read(), f"{sock_path} --show-stats\n")


def stat_mode(st):
    return stat.S_IMODE(st.st_mode)


WRAPPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rustc-wrapper")


class RustcWrapperTest(unittest.TestCase):
    """toolchains/rustc-wrapper with a fake sccache and fake compilers on a
    clean environment (no SCCACHE_*, CARGO_INCREMENTAL or XDG_RUNTIME_DIR
    inherited)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-wrapper-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.tools = os.path.join(self.tmp, "tools")
        self.home = os.path.join(self.tmp, "home")
        self.run_dir = os.path.join(self.tmp, "run")
        for d in (self.bin, self.tools, self.home, self.run_dir):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "log")
        # Each fake prints who ran and the variables that matter.
        report = ('printf "%s|uds=%s|dir=%s|size=%s|io=%s|inc=%s|binc=%s|allow=%s|args=%s\\n" "{who}" '
                  '"${{SCCACHE_SERVER_UDS-}}" "${{SCCACHE_DIR-}}" "${{SCCACHE_CACHE_SIZE-}}" '
                  '"${{SCCACHE_IGNORE_SERVER_IO_ERROR-}}" "${{CARGO_INCREMENTAL-unset}}" '
                  '"${{CARGO_BUILD_INCREMENTAL-unset}}" "${{SCCACHE_ALLOW_INCREMENTAL-unset}}" "$*" >>"{log}"\n')
        for name in ("rustc", "kani-compiler", "clippy-driver"):
            self.fake(os.path.join(self.tools, name), report.format(who=name, log=self.log))
        self.sccache = os.path.join(self.bin, "sccache")
        self.fake(self.sccache, report.format(who="sccache", log=self.log) + 'exec "$@"\n')
        self.containerenv = os.path.join(self.tmp, "containerenv")
        # Only test-owned programs on PATH (a real sccache must never be found):
        # the two the wrapper runs itself are linked in.
        self.sys = os.path.join(self.tmp, "sys")
        os.makedirs(self.sys)
        for tool in ("mkdir", "chmod"):
            os.symlink(shutil.which(tool), os.path.join(self.sys, tool))
        self.sh = shutil.which("sh")

    @staticmethod
    def fake(path, body):
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def run_wrapper(self, *args, **env):
        base = {"PATH": f"{self.bin}:{self.sys}", "HOME": self.home, "XDG_RUNTIME_DIR": self.run_dir,
                "CST_SCCACHE_CONTAINERENV": self.containerenv}
        base.update(env)
        base = {k: v for k, v in base.items() if v is not None}
        open(self.log, "w").close()
        r = subprocess.run([self.sh, WRAPPER, *args], env=base, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.log) as f:
            return [dict(kv.split("=", 1) for kv in ln.split("|")[1:]) | {"who": ln.split("|")[0]}
                    for ln in f.read().splitlines()]

    def rustc(self):
        return os.path.join(self.tools, "rustc")

    def test_rustc_goes_through_sccache_with_defaults(self):
        calls = self.run_wrapper(self.rustc(), "--crate-name", "x", "src/lib.rs")
        self.assertEqual([c["who"] for c in calls], ["sccache", "rustc"])
        c = calls[0]
        self.assertEqual(c["args"], f"{self.rustc()} --crate-name x src/lib.rs")
        self.assertEqual(c["uds"], os.path.join(self.run_dir, "cst-sccache-host.sock"))
        self.assertEqual(c["dir"], os.path.join(self.home, ".cache/sccache"))
        self.assertEqual(c["size"], "25G")
        self.assertEqual(c["io"], "1")
        self.assertEqual(c["allow"], "unset")

    def test_bare_rustc_name_counts(self):
        calls = self.run_wrapper("rustc", "-vV", PATH=f"{self.bin}:{self.tools}:{self.sys}")
        self.assertEqual([c["who"] for c in calls], ["sccache", "rustc"])

    def test_other_compilers_pass_straight_through(self):
        for name in ("kani-compiler", "clippy-driver"):
            calls = self.run_wrapper(os.path.join(self.tools, name), self.rustc(), "--crate-name", "x")
            self.assertEqual([c["who"] for c in calls], [name], name)
            self.assertEqual(calls[0]["uds"], "", name)        # nothing set for them

    def test_escape_hatch_and_missing_sccache(self):
        calls = self.run_wrapper(self.rustc(), "a", CST_SCCACHE="0")
        self.assertEqual([c["who"] for c in calls], ["rustc"])
        os.unlink(self.sccache)
        calls = self.run_wrapper(self.rustc(), "a")
        self.assertEqual([c["who"] for c in calls], ["rustc"])
        self.assertEqual(calls[0]["uds"], "")

    def test_incremental(self):
        # An incremental compile: sccache cannot cache it, straight to rustc.
        for args in (["-C", "incremental=/t/inc"], ["-Cincremental=/t/inc"]):
            calls = self.run_wrapper(self.rustc(), *args)
            self.assertEqual([c["who"] for c in calls], ["rustc"], args)
        # CARGO_INCREMENTAL=1: allowed, and dropped for sccache (up to 0.19 it refuses otherwise).
        for var in ("CARGO_INCREMENTAL", "CARGO_BUILD_INCREMENTAL"):
            calls = self.run_wrapper(self.rustc(), "--crate-name", "dep", **{var: "1"})
            self.assertEqual([c["who"] for c in calls], ["sccache", "rustc"], var)
            self.assertEqual(calls[0]["allow"], "1")
            self.assertEqual(calls[0]["inc"], "unset")
            self.assertEqual(calls[0]["binc"], "unset")
        calls = self.run_wrapper(self.rustc(), "x", CARGO_INCREMENTAL="0")
        self.assertEqual(calls[0]["allow"], "unset")
        self.assertEqual(calls[0]["inc"], "0")

    def test_server_per_environment(self):
        with open(self.containerenv, "w") as f:
            f.write('engine="podman-6.1.3"\nname="hypatia-f45"\nid="abc"\n')
        calls = self.run_wrapper(self.rustc(), "x")
        self.assertEqual(calls[0]["uds"], os.path.join(self.run_dir, "cst-sccache-hypatia-f45.sock"))
        with open(self.containerenv, "w") as f:
            f.write('name="we ird/../x"\n')
        calls = self.run_wrapper(self.rustc(), "x")
        self.assertEqual(calls[0]["uds"], os.path.join(self.run_dir, "cst-sccache-container.sock"))
        # Yours win: an address already set is kept (a port too).
        calls = self.run_wrapper(self.rustc(), "x", SCCACHE_SERVER_UDS="/x.sock", SCCACHE_DIR="/c",
                                 SCCACHE_CACHE_SIZE="1G", SCCACHE_IGNORE_SERVER_IO_ERROR="0")
        self.assertEqual((calls[0]["uds"], calls[0]["dir"], calls[0]["size"], calls[0]["io"]),
                         ("/x.sock", "/c", "1G", "0"))
        calls = self.run_wrapper(self.rustc(), "x", SCCACHE_SERVER_PORT="4300")
        self.assertEqual(calls[0]["uds"], "")

    def test_no_runtime_dir_falls_back_under_the_cache(self):
        calls = self.run_wrapper(self.rustc(), "x", XDG_RUNTIME_DIR=None)
        run = os.path.join(self.home, ".cache/sccache/run")
        self.assertEqual(calls[0]["uds"], os.path.join(run, "cst-sccache-host.sock"))
        self.assertEqual(stat_mode(os.stat(run)), 0o700)

    def test_shell_lint(self):
        r = subprocess.run(["sh", "-n", WRAPPER], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-S", "warning", WRAPPER], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)
