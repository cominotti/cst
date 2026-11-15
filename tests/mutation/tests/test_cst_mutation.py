"""Unit tests for tests/mutation/cst-mutation, the `invowk cmd test mutation` helper.

Standard library only: no uv, no cosmic-ray and (except the last test class)
no bubblewrap. A fake bwrap records its arguments and then runs the command
with the environment and folder it was given, and a fake cosmic-ray tool set
records each call and writes a small session database. Every run works on a
temporary git repository of the test's own, never on the real checkout or
the developer's home.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest tests.mutation.tests.test_cst_mutation -v
"""

import contextlib
import hashlib
import importlib.machinery
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from types import ModuleType
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "cst-mutation")


def load() -> ModuleType:
    # The helper has no .py extension, so it is loaded from its path.
    mod = ModuleType("cst_mutation")
    mod.__file__ = TOOL
    importlib.machinery.SourceFileLoader("cst_mutation", TOOL).exec_module(mod)
    return mod


M = load()

# Line numbers matter: FunctionRangesTest checks them.
HELPER_SOURCE = '''#!/usr/bin/python3 -I
"""A helper for the tests."""


def double(x):
    return x * 2


class Box:
    @staticmethod
    def size(n):
        def inner(m):
            return m + 1

        return inner(n)


async def later():
    return 1
'''

# git with no global or system config: no signing, hooks or aliases of the developer's.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

FAKE_BWRAP = """#!{python} -I
# Fake bwrap: records its arguments, then runs the command after the options
# with the environment (--clearenv, --setenv) and folder (--chdir) it was given.
import json, os, sys
args = sys.argv[1:]
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"bwrap": args}}) + "\\n")
with open({control!r}) as fh:
    control = json.load(fh)
two = {{"--ro-bind", "--bind", "--setenv"}}
one = {{"--dev", "--proc", "--tmpfs", "--chdir"}}
env, cwd, i = dict(os.environ), None, 0
while i < len(args) and args[i].startswith("--"):
    opt = args[i]
    if opt == "--clearenv":
        env = {{}}
    elif opt == "--setenv":
        env[args[i + 1]] = args[i + 2]
    elif opt == "--chdir":
        cwd = args[i + 1]
    i += 1 + (2 if opt in two else 1 if opt in one else 0)
cmd = args[i:]
if cmd == ["/usr/bin/true"] and control.get("fail_probe"):
    sys.exit(1)
if cwd:
    os.chdir(cwd)
os.execve(cmd[0], cmd, env)
"""

FAKE_TOOL = """#!{python} -I
# Fake cosmic-ray, cr-filter-lines, cr-filter-pragma and cr-report: records the
# call, then does a small part of the real job on a session database.
import json, os, sqlite3, sys, tomllib
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open({control!r}) as fh:
    control = json.load(fh)
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"tool": name, "args": args, "cwd": os.getcwd(), "env": dict(os.environ)}}) + "\\n")
step = args[0] if name == "cosmic-ray" else name
if control.get("fail") == step:
    sys.exit(3)
outcomes = control.get("outcomes", [])
if step == "baseline":
    sys.exit(1 if control.get("fail_baseline") else 0)
elif step == "init":
    with sqlite3.connect(args[2]) as db:
        db.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE work_results (job_id TEXT PRIMARY KEY, worker_outcome TEXT, "
                   "test_outcome TEXT, output TEXT, diff TEXT)")
        for n in range(len(outcomes)):
            db.execute("INSERT INTO work_items VALUES (?)", ("job%d" % n,))
elif step == "cr-filter-lines":
    with sqlite3.connect(args[-1]) as db:
        for n, o in enumerate(outcomes):
            if o == "skipped":
                db.execute("INSERT INTO work_results VALUES (?, 'SKIPPED', NULL, 'Filtered', NULL)", ("job%d" % n,))
elif step == "exec":
    with open(args[1], "rb") as fh:
        module = tomllib.load(fh)["cosmic-ray"]["module-path"]
    with open(module) as fh:
        original = fh.read()
    with open(module, "w") as fh:  # a mutant, written through the .py link, then put back
        fh.write(original + "# mutated\\n")
    with open(module, "w") as fh:
        fh.write(original)
    table = {{"killed": ("KILLED", "FAIL"), "timeout": ("KILLED", "timeout"), "survived": ("SURVIVED", "ok"),
             "incompetent": ("INCOMPETENT", "Traceback")}}
    with sqlite3.connect(args[2]) as db:
        for n, o in enumerate(outcomes):
            if o in table:
                test, output = table[o]
                db.execute("INSERT INTO work_results VALUES (?, 'NORMAL', ?, ?, ?)",
                           ("job%d" % n, test, output, "--- diff %d" % n))
elif step == "cr-report":
    with sqlite3.connect(args[-1]) as db:
        rows = db.execute("SELECT job_id, test_outcome, diff FROM work_results").fetchall()
    for job, test, diff in rows:
        if "--surviving-only" in args and test != "SURVIVED":
            continue
        print("[job-id]", job)
        print(diff)
    print("total jobs:", len(outcomes))
"""


def git(repo: str, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        stdin=subprocess.DEVNULL,
    )
    return r.stdout


def make_repo(root: str) -> str:
    """A small git checkout: a Python helper, its tests, a symlink, untracked and ignored files."""
    repo = os.path.join(root, "repo")
    os.mkdir(repo)
    git(repo, "init", "-q", "-b", "main")
    files = {
        "pkg/__init__.py": "",
        "pkg/tool": HELPER_SOURCE,
        "pkg/tests/__init__.py": "",
        "pkg/tests/test_tool.py": "import unittest\n",
        "script.sh": "#!/bin/bash\necho hi\n",
        ".gitignore": "ignored.txt\n",
    }
    for rel, text in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    os.chmod(os.path.join(repo, "pkg/tool"), 0o755)
    os.symlink("pkg/tool", os.path.join(repo, "link"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    with open(os.path.join(repo, "notes.txt"), "w") as fh:
        fh.write("untracked, not ignored\n")
    with open(os.path.join(repo, "ignored.txt"), "w") as fh:
        fh.write("ignored\n")
    return repo


def snapshot(repo: str) -> tuple[dict[str, tuple[int, int, str]], str]:
    """Every file outside .git (mode, mtime, content hash or link target) and git's status."""
    files = {}
    for folder, dirs, names in os.walk(repo):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in names + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
            path = os.path.join(folder, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                what = "-> " + os.readlink(path)
            else:
                with open(path, "rb") as fh:
                    what = hashlib.sha256(fh.read()).hexdigest()
            files[os.path.relpath(path, repo)] = (st.st_mode, st.st_mtime_ns, what)
    return files, git(repo, "status", "--porcelain=v1", "--ignored")


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        o = M.parse_args(["swap/swap-status", "swap.tests.test_swap"])
        self.assertEqual(o.helper, "swap/swap-status")
        self.assertEqual(o.test_module, "swap.tests.test_swap")
        self.assertIsNone(o.base)  # run() uses main
        self.assertFalse(o.whole_file)
        self.assertEqual(o.functions, ())
        self.assertIsNone(o.out)
        self.assertEqual(o.timeout, 60.0)

    def test_options(self):
        o = M.parse_args(["./a/b", "a.t", "--functions", "f, g", "--functions", "g,h", "--out", "/x", "--timeout", "5"])
        self.assertEqual(o.helper, "a/b")
        self.assertEqual(o.functions, ("f", "g", "h"))
        self.assertEqual((o.out, o.timeout), ("/x", 5.0))
        self.assertTrue(M.parse_args(["a/b", "a.t", "--all"]).whole_file)
        self.assertEqual(M.parse_args(["a/b", "a.t", "--base", "HEAD~2"]).base, "HEAD~2")

    def test_usage_errors(self):
        for argv in (
            [],
            ["a/b"],
            ["a/b", "a.t", "--all", "--base", "main"],
            ["a/b", "a.t", "--all", "--functions", "f"],
            ["a/b", "a.t", "--base", "x", "--functions", "f"],
            ["/abs/b", "a.t"],
            ["../b", "a.t"],
            [".", "a.t"],
            ["a/b", "a/t.py"],
            ["a/b", "a..t"],
            ["a/b", "a.t", "--functions", "f,"],
            ["a/b", "a.t", "--functions", "f-g"],
            ["a/b", "a.t", "--base", " "],
            ["a/b", "a.t", "--timeout", "0"],
            ["a/b", "a.t", "--timeout", "-1"],
            ["a/b", "a.t", "--timeout", "nan"],
            ["a/b", "a.t", "--timeout", "x"],
            ["a/b", "a.t", "--jobs", "4"],
        ):
            with self.subTest(argv=argv), self.assertRaises(M.UsageError):
                M.parse_args(argv)


class RangesTest(unittest.TestCase):
    def test_ranges_from_diff(self):
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
            "@@ -3 +3 @@ def f():\n-a\n+b\n"  # one line, count left out
            "@@ -10,0 +11,2 @@\n+c\n+d\n"  # two added lines
            "@@ -20,3 +22,0 @@\n-e\n-f\n-g\n"  # a pure deletion: nothing on the new side
            "@@ -30,2 +30,3 @@\n"
        )
        self.assertEqual(M.ranges_from_diff(diff), [(3, 3), (11, 12), (30, 32)])

    def test_ranges_from_diff_edges(self):
        self.assertEqual(M.ranges_from_diff(""), [])
        self.assertEqual(M.ranges_from_diff("@@ -1,2 +1,0 @@\n-a\n-b\n"), [])
        self.assertEqual(M.ranges_from_diff("+@@ -1 +1 @@ in a line\n"), [])  # not a hunk header
        self.assertEqual(M.ranges_from_diff("@@ -1 +1,2 @@\n@@ -5 +3,2 @@\n"), [(1, 4)])  # touching: joined

    def test_merge_ranges(self):
        self.assertEqual(M.merge_ranges([]), [])
        self.assertEqual(M.merge_ranges([(8, 9), (1, 3), (2, 5), (7, 7)]), [(1, 5), (7, 9)])
        self.assertEqual(M.merge_ranges([(1, 10), (2, 3)]), [(1, 10)])

    def test_whole_file_range(self):
        self.assertEqual(M.whole_file_range("a\nb\nc\n"), [(1, 3)])
        self.assertEqual(M.whole_file_range(""), [(1, 1)])


class FunctionRangesTest(unittest.TestCase):
    def setUp(self):
        lines = HELPER_SOURCE.splitlines()
        self.assertTrue(lines[4].startswith("def double"))
        self.assertTrue(lines[9].strip().startswith("@staticmethod"))
        self.assertTrue(lines[17].startswith("async def later"))

    def test_top_level_method_nested_async(self):
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["double"]), [(5, 6)])
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["size"]), [(10, 15)])  # a method, with its decorator
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["inner"]), [(12, 13)])  # a nested def
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["later"]), [(18, 19)])  # async
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["later", "double"]), [(5, 6), (18, 19)])
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["size", "inner"]), [(10, 15)])

    def test_every_def_with_the_name(self):
        src = "def read(a): ...\n\n\ndef read(a, b): ...\n\n\ndef read(a, b, c):\n    return a\n"
        self.assertEqual(M.function_ranges(src, ["read"]), [(1, 1), (4, 4), (7, 8)])

    def test_unknown_name_is_a_usage_error(self):
        with self.assertRaisesRegex(M.UsageError, "no function named Box, nope"):
            M.function_ranges(HELPER_SOURCE, ["double", "Box", "nope"])  # a class is not a function


class ConfigTest(unittest.TestCase):
    def test_config_reads_back(self):
        text = M.config_text("swap/swap-status.py", "swap.tests.test_swap", 30, [(3, 5), (9, 9)])
        conf = tomllib.loads(text)["cosmic-ray"]
        self.assertEqual(conf["module-path"], "swap/swap-status.py")
        self.assertEqual(conf["timeout"], 30.0)
        self.assertEqual(conf["excluded-modules"], [])
        self.assertEqual(conf["test-command"], "python3 -B -m unittest swap.tests.test_swap")
        self.assertEqual(conf["distributor"], {"name": "local"})
        # The line filter's key must be the module path, or it filters out everything.
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"swap/swap-status.py": ["3-5", "9-9"]})

    def test_toml_str(self):
        for value in ("plain", 'qu"ote', "back\\slash", "tab\there", "new\nline", "del\x7f", "ünïcode", ""):
            with self.subTest(value=value):
                self.assertEqual(tomllib.loads("k = " + M.toml_str(value))["k"], value)


class SandboxTest(unittest.TestCase):
    COPY, OUT, STUBS, VENV = "/tmp/w/copy", "/var/home/u/.cache/cst-mutation/x", "/tmp/w/stubs", "/var/home/u/venv"

    def argv(self, hidden=("/run", "/tmp", "/var/home")):
        return M.sandbox_argv("/usr/bin/bwrap", self.COPY, self.OUT, self.STUBS, self.VENV, list(hidden))

    def test_the_profile(self):
        home = self.COPY + "/.home"
        self.assertEqual(
            self.argv(),
            [
                "/usr/bin/bwrap",
                *("--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"),
                *("--tmpfs", "/run", "--tmpfs", "/tmp", "--tmpfs", "/var/home"),
                *("--ro-bind", self.VENV, self.VENV, "--ro-bind", self.STUBS, self.STUBS),
                *("--bind", self.COPY, self.COPY, "--bind", self.OUT, self.OUT),
                *("--unshare-net", "--unshare-pid", "--unshare-ipc", "--new-session", "--die-with-parent"),
                "--clearenv",
                *("--setenv", "HOME", home),
                *("--setenv", "XDG_CONFIG_HOME", home + "/.config"),
                *("--setenv", "XDG_CACHE_HOME", home + "/.cache"),
                *("--setenv", "XDG_DATA_HOME", home + "/.local/share"),
                *("--setenv", "XDG_STATE_HOME", home + "/.local/state"),
                *("--setenv", "XDG_RUNTIME_DIR", "/tmp/run"),
                *("--setenv", "TMPDIR", "/tmp"),
                *("--setenv", "PYTHONDONTWRITEBYTECODE", "1"),
                *("--setenv", "PATH", self.STUBS + ":/usr/bin"),
                *("--chdir", self.COPY),
            ],
        )

    def test_binds_come_after_every_tmpfs_and_only_copy_and_out_are_writable(self):
        argv = self.argv(hidden=("/run", "/tmp", "/var/home", "/home"))
        last_tmpfs = max(i for i, a in enumerate(argv) if a == "--tmpfs")
        for path in (self.VENV, self.STUBS, self.COPY, self.OUT):
            self.assertGreater(argv.index(path), last_tmpfs, path)  # else the tmpfs would hide it
        writable = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        self.assertEqual(writable, [self.COPY, self.OUT])
        self.assertEqual(argv[1:4], ["--ro-bind", "/", "/"])
        self.assertNotIn("--share-net", argv)
        self.assertLess(argv.index("--clearenv"), argv.index("--setenv"))  # nothing of ours is cleared

    def test_hidden_trees(self):
        def fake(dirs, links):
            return M.hidden_trees(isdir=lambda p: p in dirs, islink=lambda p: p in links)

        # Silverblue: /home is a symlink to /var/home.
        self.assertEqual(fake({"/run", "/tmp", "/var/home", "/home"}, {"/home"}), ["/run", "/tmp", "/var/home"])
        # A plain Fedora (or CI): /home is a real folder and /var/home does not exist.
        self.assertEqual(fake({"/run", "/tmp", "/home"}, set()), ["/run", "/tmp", "/home"])

    def test_stubs_fail_and_say_why(self):
        with tempfile.TemporaryDirectory(prefix="cst-mutation-test-") as tmp:
            M.write_stubs(tmp)
            self.assertEqual(sorted(os.listdir(tmp)), sorted(M.STUBBED))
            for name in ("sudo", "systemctl", "flatpak-spawn"):
                r = subprocess.run(
                    [os.path.join(tmp, name), "daemon-reload"],
                    check=False,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                )
                self.assertEqual(r.returncode, 97)
                self.assertIn(f"{name} is not available during mutation testing", r.stderr)


class FindToolsTest(unittest.TestCase):
    def test_outside_a_virtual_environment(self):
        with self.assertRaisesRegex(M.UsageError, "cosmic-ray is not installed for this Python"):
            M.find_tools(which=lambda _: "/usr/bin/bwrap", prefix="/usr", base_prefix="/usr")

    def test_tools_from_the_venv_and_bwrap_from_path(self):
        with tempfile.TemporaryDirectory(prefix="cst-mutation-test-") as tmp:
            venv = os.path.realpath(tmp)
            os.mkdir(os.path.join(venv, "bin"))
            with self.assertRaisesRegex(M.UsageError, "cosmic-ray, cr-filter-lines, cr-filter-pragma, cr-report"):
                M.find_tools(which=lambda _: None, prefix=venv, base_prefix="/usr")
            for name in M.TOOLS:
                path = os.path.join(venv, "bin", name)
                with open(path, "w") as fh:
                    fh.write("")
                os.chmod(path, 0o755)
            tools = M.find_tools(which=lambda _: None, prefix=venv, base_prefix="/usr")
            self.assertIsNone(tools.bwrap)
            self.assertEqual(tools.python, os.path.join(venv, "bin", "python3"))
            self.assertEqual(tools.tool("cr-report"), os.path.join(venv, "bin", "cr-report"))
            self.assertEqual(
                M.find_tools(which=lambda _: "/x/bwrap", prefix=venv, base_prefix="/usr").bwrap, "/x/bwrap"
            )


class SessionCountsTest(unittest.TestCase):
    def test_counts_both_enum_spellings(self):
        with tempfile.TemporaryDirectory(prefix="cst-mutation-test-") as tmp:
            path = os.path.join(tmp, "s.sqlite")
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE work_items (job_id TEXT)")
                db.execute(
                    "CREATE TABLE work_results (job_id TEXT, worker_outcome TEXT, test_outcome TEXT, output TEXT)"
                )
                rows = [
                    ("a", "SKIPPED", None, "Filtered"),
                    ("b", "skipped", None, "Filtered"),
                    ("c", "NORMAL", "KILLED", "FAIL"),
                    ("d", "normal", "killed", "timeout"),
                    ("e", "NORMAL", "SURVIVED", "ok"),
                    ("f", "NORMAL", "survived", "ok"),
                    ("g", "NORMAL", "INCOMPETENT", "Traceback"),
                    ("h", "EXCEPTION", None, "Traceback"),
                ]
                for job, *_ in rows + [("pending",)]:
                    db.execute("INSERT INTO work_items VALUES (?)", (job,))
                db.executemany("INSERT INTO work_results VALUES (?, ?, ?, ?)", rows)
                db.commit()
            c = M.session_counts(path)
        self.assertEqual(
            (c.total, c.skipped, c.killed, c.timeouts, c.survived, c.incompetent, c.not_run), (9, 2, 1, 1, 2, 1, 2)
        )
        self.assertEqual(c.mutants, 7)


class RunTest(unittest.TestCase):
    """The whole run with a fake bwrap and fake cosmic-ray tools."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="cst-mutation-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.repo = make_repo(self.tmp)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.control = os.path.join(self.tmp, "control.json")
        self.set_control(outcomes=["skipped", "killed", "timeout", "survived", "survived"])
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        self.bwrap = os.path.join(bin_dir, "bwrap")
        self.write_exec(self.bwrap, FAKE_BWRAP.format(python=sys.executable, log=self.log, control=self.control))
        self.venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(self.venv, "bin"))
        for name in M.TOOLS:
            self.write_exec(
                os.path.join(self.venv, "bin", name),
                FAKE_TOOL.format(python=sys.executable, log=self.log, control=self.control),
            )
        self.tools = M.Tools(bwrap=self.bwrap, python=sys.executable, venv=self.venv)
        self.out = os.path.join(self.tmp, "out")
        # The copy goes into this folder, so that the test can see it is removed afterwards.
        self.scratch = os.path.join(self.tmp, "scratch")
        os.mkdir(self.scratch)
        for patch in (mock.patch.object(tempfile, "tempdir", self.scratch), mock.patch.dict(os.environ, GIT_ENV)):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def write_exec(path, text):
        with open(path, "w") as fh:
            fh.write(text)
        os.chmod(path, 0o755)

    def set_control(self, **control):
        with open(self.control, "w") as fh:
            json.dump(control, fh)

    def execute(self, *args, tools=None, out=True):
        argv = ["pkg/tool", "pkg.tests.test_tool", *args] + (["--out", self.out] if out else [])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = M.execute(argv, repo=lambda: self.repo, tools=lambda: tools or self.tools, env={"HOME": self.tmp})
        return rc, stdout.getvalue(), stderr.getvalue()

    def config(self):
        with open(os.path.join(self.out, "cosmic-ray.toml"), "rb") as fh:
            return tomllib.load(fh)["cosmic-ray"]

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def steps(self):
        out = []
        for c in self.calls():
            if "bwrap" in c:
                continue
            out.append(c["args"][0] if c["tool"] == "cosmic-ray" else c["tool"])
        return out

    def test_steps_in_order_all_in_the_sandbox_survivors_exit_0(self):
        before = snapshot(self.repo)
        rc, stdout, stderr = self.execute("--all", "--timeout", "7")
        self.assertEqual(rc, 0, stdout + stderr)
        self.assertEqual(
            self.steps(), ["baseline", "init", "cr-filter-lines", "cr-filter-pragma", "exec", "cr-report", "cr-report"]
        )
        calls = self.calls()
        # Every step, and the probe before them, went through bwrap: a bwrap call right before each tool call.
        self.assertEqual([("bwrap" in c) for c in calls], [True] + [True, False] * 7)
        self.assertEqual(calls[0]["bwrap"][-1], "/usr/bin/true")
        copy = None
        for c in calls:
            if "bwrap" in c:
                continue
            copy = copy or c["cwd"]
            self.assertEqual(c["cwd"], copy)
            self.assertEqual(c["env"]["HOME"], os.path.join(copy, ".home"))
            self.assertTrue(c["env"]["PATH"].split(":")[0].endswith("stubs"))
            for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
                self.assertTrue(c["env"][key].startswith(os.path.join(copy, ".home") + "/"), key)
        self.assertIsNotNone(copy)
        self.assertFalse(str(copy).startswith(self.repo))
        # The report and the numbers.
        with open(os.path.join(self.out, "survivors.txt")) as fh:
            survivors = fh.read()
        self.assertIn("--- diff 3", survivors)
        self.assertIn("--- diff 4", survivors)
        self.assertNotIn("--- diff 1", survivors)
        with open(os.path.join(self.out, "counts.json")) as fh:
            counts = json.load(fh)
        self.assertEqual(
            {k: counts[k] for k in ("total", "skipped", "mutants", "killed", "timeouts", "survived", "not_run")},
            {"total": 5, "skipped": 1, "mutants": 4, "killed": 1, "timeouts": 1, "survived": 2, "not_run": 0},
        )
        self.assertIn("4 mutants in the chosen lines", stdout)
        self.assertIn("survived 2", stdout)
        conf = self.config()
        self.assertEqual(conf["module-path"], "pkg/tool.py")
        self.assertEqual(conf["timeout"], 7.0)
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"pkg/tool.py": ["1-19"]})
        # The checkout is untouched (the fake exec wrote a mutant through the .py link of the copy),
        # and the copy is gone.
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_the_copy_is_a_faithful_copy(self):
        dest = os.path.join(self.tmp, "dest")
        os.mkdir(dest)
        os.remove(os.path.join(self.repo, "script.sh"))  # tracked, deleted in the working tree
        copied = M.copy_checkout(self.repo, dest)
        expected = [".gitignore", "link", "notes.txt", "pkg/__init__.py", "pkg/tests/__init__.py"]
        self.assertEqual(copied, [*expected, "pkg/tests/test_tool.py", "pkg/tool"])
        self.assertFalse(os.path.exists(os.path.join(dest, "ignored.txt")))
        self.assertFalse(os.path.exists(os.path.join(dest, ".git")))
        self.assertEqual(os.readlink(os.path.join(dest, "link")), "pkg/tool")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dest, "pkg/tool")).st_mode), 0o755)
        with open(os.path.join(dest, "pkg/tool")) as fh:
            self.assertEqual(fh.read(), HELPER_SOURCE)

    def test_baseline_failure_stops_before_init_with_exit_2(self):
        self.set_control(outcomes=["killed"], fail_baseline=True)
        rc, stdout, _ = self.execute("--all")
        self.assertEqual(rc, 2)
        self.assertEqual(self.steps(), ["baseline"])
        self.assertIn("the tests fail (or cannot run) without any mutation", stdout)
        self.assertFalse(os.path.exists(os.path.join(self.out, "session.sqlite")))
        self.assertEqual(os.listdir(self.scratch), [])

    def test_a_failed_step_exits_1(self):
        for failing, done in (
            ("init", ["baseline", "init"]),
            ("cr-filter-lines", ["baseline", "init", "cr-filter-lines"]),
            ("exec", ["baseline", "init", "cr-filter-lines", "cr-filter-pragma", "exec"]),
        ):
            with self.subTest(step=failing):
                shutil.rmtree(self.out, ignore_errors=True)
                if os.path.exists(self.log):
                    os.remove(self.log)
                self.set_control(outcomes=["killed"], fail=failing)
                rc, _, stderr = self.execute("--all")
                self.assertEqual(rc, 1)
                self.assertEqual(self.steps(), done)
                self.assertIn("failed with exit status 3", stderr)

    def test_mutants_that_could_not_run_exit_1(self):
        self.set_control(outcomes=["killed", "incompetent"])
        rc, stdout, _ = self.execute("--all")
        self.assertEqual(rc, 1)
        self.assertIn("some mutants did not run normally", stdout)

    def test_without_bwrap_it_refuses_and_runs_nothing(self):
        tools = M.Tools(bwrap=None, python=sys.executable, venv=self.venv)
        before = snapshot(self.repo)
        rc, stdout, stderr = self.execute("--all", tools=tools)
        self.assertEqual(rc, 2)
        self.assertIn("bubblewrap (bwrap) is not installed", stderr)
        self.assertIn("does not run without it", stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(snapshot(self.repo), before)

    def test_a_bwrap_that_cannot_build_the_sandbox_runs_nothing(self):
        self.set_control(outcomes=["killed"], fail_probe=True)
        rc, _, stderr = self.execute("--all")
        self.assertEqual(rc, 2)
        self.assertIn("bwrap cannot build the sandbox here", stderr)
        self.assertEqual(self.steps(), [])

    def test_base_mutates_the_changed_lines(self):
        with open(os.path.join(self.repo, "pkg/tool"), "a") as fh:
            fh.write("\n\ndef added():\n    return 2\n")
        before = snapshot(self.repo)
        rc, stdout, stderr = self.execute("--base", "main")
        self.assertEqual(rc, 0, stdout + stderr)
        conf = self.config()
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"pkg/tool.py": ["20-23"]})
        self.assertEqual(snapshot(self.repo), before)

    def test_functions_choose_the_lines(self):
        rc, stdout, stderr = self.execute("--functions", "double,inner")
        self.assertEqual(rc, 0, stdout + stderr)
        conf = self.config()
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"pkg/tool.py": ["5-6", "12-13"]})

    def test_no_changes_means_nothing_to_mutate(self):
        rc, stdout, _ = self.execute()  # --base main by default, and the helper is unchanged
        self.assertEqual(rc, 0)
        self.assertIn("nothing to mutate", stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.out))

    def test_default_output_folder_is_under_the_cache(self):
        rc, stdout, stderr = self.execute("--all", out=False)
        self.assertEqual(rc, 0, stdout + stderr)
        made = os.listdir(os.path.join(self.tmp, ".cache", "cst-mutation"))
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].startswith("pkg-tool-"))
        self.assertEqual(
            M.default_out("a/b", {"XDG_CACHE_HOME": "/c", "HOME": "/h"}, 0).rsplit("-", 2)[0], "/c/cst-mutation/a-b"
        )

    def test_usage_errors_run_nothing(self):
        cases = [
            (["--functions", "nope"], "no function named nope"),
            (["--base", "no-such-ref"], "git diff no-such-ref failed"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                rc, _, stderr = self.execute(*args)
                self.assertEqual(rc, 2)
                self.assertIn(message, stderr)
        for helper, message in (
            ("script.sh", "not a Python file"),
            ("link", "not a file of the checkout"),
            ("ignored.txt", "not a file of the checkout"),
            ("pkg/missing", "not a file of the checkout"),
        ):
            with self.subTest(helper=helper):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    rc = M.execute([helper, "t", "--all"], repo=lambda: self.repo, tools=lambda: self.tools)
                self.assertEqual(rc, 2)
                self.assertIn(message, stderr.getvalue())
        self.assertEqual(self.calls(), [])

    def test_output_folder_must_be_new_and_outside_the_checkout(self):
        os.makedirs(self.out)
        with open(os.path.join(self.out, "old"), "w") as fh:
            fh.write("x")
        rc, _, stderr = self.execute("--all")
        self.assertEqual(rc, 2)
        self.assertIn("is not empty", stderr)
        rc, _, stderr = self.execute("--all", "--out", os.path.join(self.repo, "out"), out=False)
        self.assertEqual(rc, 2)
        self.assertIn("must be outside the checkout", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "out")))
        self.assertEqual(self.calls(), [])


def usable_bwrap() -> tuple[str | None, str]:
    """(bwrap's path, "") when it can build a sandbox here, else (None, the reason)."""
    path = shutil.which("bwrap")
    if path is None:
        return None, "bwrap is not installed"
    r = subprocess.run(
        [path, "--ro-bind", "/", "/", "/usr/bin/true"],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        return None, "bwrap cannot build a sandbox here: " + r.stderr.strip()
    return path, ""


PROBE = r"""
import json, os, stat, sys
outside, copy, real_home = sys.argv[1:4]
def attempt(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "probe"), "w") as fh:
            fh.write("x")
        return "written"
    except OSError as e:
        return "refused: " + str(e.strerror)
sockets = []
for root in ("/run", "/var/run", "/tmp", "/var/home", "/home"):
    for folder, dirs, files in os.walk(root):
        for name in files + dirs:
            path = os.path.join(folder, name)
            try:
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    sockets.append(path)
            except OSError:
                pass
print(json.dumps({"outside": attempt(outside), "copy": attempt(copy), "sockets": sockets,
                  "home": os.environ.get("HOME"), "real_home_visible": os.path.exists(real_home)}))
"""


class RealBwrapTest(unittest.TestCase):
    """The real sandbox, when this machine can build one (CI's container usually cannot)."""

    def test_only_the_copy_is_writable_and_no_socket_is_visible(self):
        bwrap, reason = usable_bwrap()
        if bwrap is None:
            self.skipTest(reason)
        hidden = M.hidden_trees()
        python = next(
            (
                p
                for p in (os.path.realpath(sys.executable), "/usr/bin/python3")
                if os.path.exists(p) and not any(M.inside(p, h) for h in hidden)
            ),
            None,
        )
        if python is None:
            self.skipTest("no Python outside the folders the sandbox hides")
        with (
            tempfile.TemporaryDirectory(prefix="cst-mutation-test-") as work,
            tempfile.TemporaryDirectory(prefix="cst-mutation-outside-") as outside,
        ):
            work = os.path.realpath(work)
            copy, out, stubs, venv = (os.path.join(work, d) for d in ("copy", "out", "stubs", "venv"))
            for d in (copy, out, venv):
                os.mkdir(d)
            M.write_stubs(stubs)
            box = M.sandbox_argv(bwrap, copy, out, stubs, venv, hidden)
            real_home = os.path.expanduser("~")
            r = subprocess.run(
                [*box, python, "-I", "-c", PROBE, os.path.realpath(outside), copy, real_home],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            got = json.loads(r.stdout)
            self.assertEqual(os.listdir(outside), [])  # whatever happened inside, nothing reached it
            self.assertEqual(got["copy"], "written")
            self.assertEqual(os.listdir(copy), ["probe"])
            self.assertEqual(got["sockets"], [])
            self.assertEqual(got["home"], os.path.join(copy, ".home"))
            if any(M.inside(real_home, h) for h in hidden):
                self.assertFalse(got["real_home_visible"])


if __name__ == "__main__":
    unittest.main()
