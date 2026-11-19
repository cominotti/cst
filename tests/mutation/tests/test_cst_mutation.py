# SPDX-License-Identifier: MPL-2.0
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

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import hashlib
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
from types import ModuleType
from typing import Any
import unittest
from unittest import mock

# Run directly (python3 -I -B tests/mutation/tests/test_cst_mutation.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from tests.script_loader import load_script

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "cst-mutation")


def load() -> ModuleType:
    # The helper has no .py extension, so it is loaded from its path.
    return load_script(TOOL, "cst_mutation")


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

# Both fakes record whether their stdin is /dev/null.
STDIN_IS_NULL = """def stdin_is_null():
    try:
        st, null = os.fstat(0), os.stat(os.devnull)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == null.st_rdev
"""

FAKE_BWRAP = """#!{python} -I
# Fake bwrap: records its arguments, then runs the command after the options
# with the environment (--clearenv, --setenv) and folder (--chdir) it was given.
import json, os, stat, sys
{stdin_is_null}args = sys.argv[1:]
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"bwrap": args, "stdin_null": stdin_is_null()}}) + "\\n")
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
import json, os, sqlite3, stat, sys, tomllib
{stdin_is_null}name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open({control!r}) as fh:
    control = json.load(fh)
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"tool": name, "args": args, "cwd": os.getcwd(), "env": dict(os.environ),
                         "stdin_null": stdin_is_null()}}) + "\\n")
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
    if control.get("plant_links"):  # what a mutant could do: links in the output folder to files elsewhere
        for report in ("survivors.txt", "report.txt", "counts.json"):
            os.symlink(os.path.join(control["plant_links"], report), os.path.join(os.path.dirname(args[2]), report))
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
    def test_defaults(self) -> None:
        o = M.parse_args(["swap/domain.py", "swap.tests.test_swap"])
        self.assertEqual(o.helper, "swap/domain.py")
        self.assertEqual(o.test_module, "swap.tests.test_swap")
        self.assertIsNone(o.base)  # run() uses main
        self.assertFalse(o.whole_file)
        self.assertEqual(o.functions, ())
        self.assertIsNone(o.out)
        self.assertEqual(o.timeout, 60.0)

    def test_options(self) -> None:
        o = M.parse_args(["./a/b", "a.t", "--functions", "f, g", "--functions", "g,h", "--out", "/x", "--timeout", "5"])
        self.assertEqual(o.helper, "a/b")
        self.assertEqual(o.functions, ("f", "g", "h"))
        self.assertEqual((o.out, o.timeout), ("/x", 5.0))
        self.assertTrue(M.parse_args(["a/b", "a.t", "--all"]).whole_file)
        self.assertEqual(M.parse_args(["a/b", "a.t", "--base", "HEAD~2"]).base, "HEAD~2")

    def test_usage_errors(self) -> None:
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
            ["a/b", "a.t", "--base=--output=/tmp/x"],  # git would take it as an option and write that file
            ["a/b", "a.t", "--base=-p"],
            ["a/b", "a.t", "--timeout", "0"],
            ["a/b", "a.t", "--timeout", "-1"],
            ["a/b", "a.t", "--timeout", "nan"],
            ["a/b", "a.t", "--timeout", "x"],
            ["a/b", "a.t", "--jobs", "4"],
        ):
            with self.subTest(argv=argv), self.assertRaises(M.UsageError):
                M.parse_args(argv)


class RangesTest(unittest.TestCase):
    def test_ranges_from_diff(self) -> None:
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
            "@@ -3 +3 @@ def f():\n-a\n+b\n"  # one line, count left out
            "@@ -10,0 +11,2 @@\n+c\n+d\n"  # two added lines
            "@@ -20,3 +22,0 @@\n-e\n-f\n-g\n"  # a pure deletion: nothing on the new side
            "@@ -30,2 +30,3 @@\n"
        )
        self.assertEqual(M.ranges_from_diff(diff), [(3, 3), (11, 12), (30, 32)])

    def test_ranges_from_diff_edges(self) -> None:
        self.assertEqual(M.ranges_from_diff(""), [])
        self.assertEqual(M.ranges_from_diff("@@ -1,2 +1,0 @@\n-a\n-b\n"), [])
        self.assertEqual(M.ranges_from_diff("+@@ -1 +1 @@ in a line\n"), [])  # not a hunk header
        self.assertEqual(M.ranges_from_diff("@@ -1 +1,2 @@\n@@ -5 +3,2 @@\n"), [(1, 4)])  # touching: joined

    def test_merge_ranges(self) -> None:
        self.assertEqual(M.merge_ranges([]), [])
        self.assertEqual(M.merge_ranges([(8, 9), (1, 3), (2, 5), (7, 7)]), [(1, 5), (7, 9)])
        self.assertEqual(M.merge_ranges([(1, 10), (2, 3)]), [(1, 10)])

    def test_whole_file_range(self) -> None:
        self.assertEqual(M.whole_file_range("a\nb\nc\n"), [(1, 3)])
        self.assertEqual(M.whole_file_range(""), [(1, 1)])


class FunctionRangesTest(unittest.TestCase):
    def setUp(self) -> None:
        lines = HELPER_SOURCE.splitlines()
        self.assertTrue(lines[4].startswith("def double"))
        self.assertTrue(lines[9].strip().startswith("@staticmethod"))
        self.assertTrue(lines[17].startswith("async def later"))

    def test_top_level_method_nested_async(self) -> None:
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["double"]), [(5, 6)])
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["size"]), [(10, 15)])  # a method, with its decorator
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["inner"]), [(12, 13)])  # a nested def
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["later"]), [(18, 19)])  # async
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["later", "double"]), [(5, 6), (18, 19)])
        self.assertEqual(M.function_ranges(HELPER_SOURCE, ["size", "inner"]), [(10, 15)])

    def test_every_def_with_the_name(self) -> None:
        src = "def read(a): ...\n\n\ndef read(a, b): ...\n\n\ndef read(a, b, c):\n    return a\n"
        self.assertEqual(M.function_ranges(src, ["read"]), [(1, 1), (4, 4), (7, 8)])

    def test_unknown_name_is_a_usage_error(self) -> None:
        with self.assertRaisesRegex(M.UsageError, "no function named Box, nope"):
            M.function_ranges(HELPER_SOURCE, ["double", "Box", "nope"])  # a class is not a function


class ConfigTest(unittest.TestCase):
    def test_config_reads_back(self) -> None:
        text = M.config_text("swap/domain.py", "swap.tests.test_swap", 30, [(3, 5), (9, 9)])
        conf = tomllib.loads(text)["cosmic-ray"]
        self.assertEqual(conf["module-path"], "swap/domain.py")
        self.assertEqual(conf["timeout"], 30.0)
        self.assertEqual(conf["excluded-modules"], [])
        self.assertEqual(conf["test-command"], "python3 -B -m unittest swap.tests.test_swap")
        self.assertEqual(conf["distributor"], {"name": "local"})
        # The line filter's key must be the module path, or it filters out everything.
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"swap/domain.py": ["3-5", "9-9"]})

    def test_toml_str(self) -> None:
        for value in ("plain", 'qu"ote', "back\\slash", "tab\there", "new\nline", "del\x7f", "ünïcode", ""):
            with self.subTest(value=value):
                self.assertEqual(tomllib.loads("k = " + M.toml_str(value))["k"], value)


class SandboxTest(unittest.TestCase):
    COPY, OUT, STUBS, VENV = "/tmp/w/copy", "/var/home/u/.cache/cst-mutation/x", "/tmp/w/stubs", "/var/home/u/venv"

    def argv(self, hidden: Sequence[str] = ("/run", "/tmp", "/var/home")) -> list[str]:
        return M.sandbox_argv("/usr/bin/bwrap", self.COPY, self.OUT, self.STUBS, self.VENV, list(hidden))

    def test_the_profile(self) -> None:
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

    def test_binds_come_after_every_tmpfs_and_only_copy_and_out_are_writable(self) -> None:
        argv = self.argv(hidden=("/run", "/tmp", "/var/home", "/home"))
        last_tmpfs = max(i for i, a in enumerate(argv) if a == "--tmpfs")
        for path in (self.VENV, self.STUBS, self.COPY, self.OUT):
            self.assertGreater(argv.index(path), last_tmpfs, path)  # else the tmpfs would hide it
        writable = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        self.assertEqual(writable, [self.COPY, self.OUT])
        self.assertEqual(argv[1:4], ["--ro-bind", "/", "/"])
        self.assertNotIn("--share-net", argv)
        self.assertLess(argv.index("--clearenv"), argv.index("--setenv"))  # nothing of ours is cleared

    def test_hidden_trees(self) -> None:
        def fake(dirs: set[str], links: set[str]) -> list[str]:
            return M.hidden_trees([], isdir=lambda p: p in dirs, islink=lambda p: p in links)

        # Silverblue: /home, /mnt and /media are symlinks (to /var/home, /var/mnt and /run/media).
        dirs = {"/run", "/tmp", "/var/home", "/home", "/var/mnt", "/mnt", "/media"}
        self.assertEqual(
            fake(dirs, {"/home", "/mnt", "/media"}), ["/run", "/tmp", "/var/home", "/var/mnt"]
        )  # /var/mnt: CST's btrfs top level, with the home subvolume in it, is mounted there
        # A plain Fedora (or CI): /home, /mnt and /media are real folders, /var/home and /var/mnt do not exist.
        self.assertEqual(
            fake({"/run", "/tmp", "/home", "/mnt", "/media"}, set()), ["/run", "/tmp", "/home", "/mnt", "/media"]
        )

    def test_stubs_fail_and_say_why(self) -> None:
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


def mountinfo(*rows: str) -> list[Any]:
    """Mounts from short rows "DEV ROOT POINT TYPE", in the format of /proc/self/mountinfo."""
    lines = []
    for n, row in enumerate(rows, start=20):
        dev, root, point, fstype = row.split()
        lines.append(f"{n} 1 {dev} {root} {point} rw,relatime shared:{n} - {fstype} /dev/x rw")
    return M.parse_mountinfo("\n".join(lines) + "\n")


class MountsTest(unittest.TestCase):
    # This workstation: Silverblue, the home subvolume at /var/home, the whole
    # btrfs (subvolid 5) automounted at /var/mnt/btrfs-top by CST's snapshot module.
    HOST = (
        "0:44 / / overlay",
        "0:39 /root /sysroot btrfs",
        "0:39 /var /var btrfs",
        "0:29 / /run tmpfs",
        "0:67 / /tmp tmpfs",
        "0:39 /home /var/home btrfs",
        "0:60 / /var/mnt/btrfs-top autofs",
        "0:39 / /var/mnt/btrfs-top btrfs",
        "0:35 / /proc/sys/fs/binfmt_misc autofs",
    )

    @staticmethod
    def views(rows: Sequence[str], hidden: Sequence[str], dirs: set[str] | None = None) -> list[str]:
        return M.other_views(mountinfo(*rows), hidden, lambda p: dirs is None or p in dirs)

    def test_parse_mountinfo(self) -> None:
        text = (
            "29 1 0:29 / /run rw,nosuid shared:5 - tmpfs tmpfs rw\n"
            "30 29 0:39 /home/a\\040b /mnt/with\\040space rw - btrfs /dev/dm-1 rw,subvol=/home\n"  # no tags
            "31 29 0:40 / /x\\134y rw master:1 propagate_from:2 - ext4 /dev/sda1 rw\n"
            "garbage\n"
        )
        self.assertEqual(
            M.parse_mountinfo(text),
            [
                M.Mount("0:29", "/", "/run", "tmpfs"),
                M.Mount("0:39", "/home/a b", "/mnt/with space", "btrfs"),
                M.Mount("0:40", "/", "/x\\y", "ext4"),
            ],
        )

    def test_the_whole_btrfs_mounted_elsewhere_shows_the_home(self) -> None:
        # With /var/mnt not hidden, the automount point is hidden itself (what it mounts is unknown),
        # and so is the home inside the top level when it is mounted (it is under the automount point).
        self.assertEqual(self.views(self.HOST, ["/run", "/tmp", "/var/home"]), ["/var/mnt/btrfs-top"])
        no_autofs = [r for r in self.HOST if "autofs" not in r]
        self.assertEqual(self.views(no_autofs, ["/run", "/tmp", "/var/home"]), ["/var/mnt/btrfs-top/home"])
        # With /var/mnt hidden (the default), nothing more; /proc gets a fresh mount anyway.
        self.assertEqual(self.views(self.HOST, ["/run", "/tmp", "/var/home", "/var/mnt"]), [])

    def test_a_toolbox(self) -> None:
        # The toolbox's / is one of the overlays of the container store in the home; the user's home is a bind
        # of the host's /home/USER; the host's /var/mnt is bound with the top level mounted in it.
        rows = (
            "0:182 / / overlay",
            "0:29 / /run tmpfs",
            "0:44 / /run/host overlay",
            "0:39 /home /run/host/var/home btrfs",
            "0:39 /home/u /var/home/u btrfs",
            "0:39 /home/u/.local/share/containers/storage/overlay /var/home/u/.local/share/containers/storage/overlay btrfs",
            "0:182 / /var/home/u/.local/share/containers/storage/overlay/abc/merged overlay",
            "0:182 /usr/share/empty /sys/fs/selinux overlay",
            "0:39 /var/lib/flatpak /var/lib/flatpak btrfs",
            "0:39 /var/mnt /var/mnt btrfs",
            "0:39 / /var/mnt/btrfs-top btrfs",
        )
        self.assertEqual(self.views(rows, ["/run", "/tmp", "/var/home"]), ["/var/mnt/btrfs-top/home/u"])
        self.assertEqual(self.views(rows, ["/run", "/tmp", "/var/home", "/var/mnt"]), [])

    def test_a_home_partition_mounted_twice(self) -> None:
        rows = ("8:1 / / ext4", "8:2 / /home ext4", "8:2 / /srv/homes ext4", "8:2 /alice /srv/alice ext4")
        # /srv/homes shows the whole partition, /srv/alice one folder of it.
        self.assertEqual(self.views(rows, ["/run", "/tmp", "/home"]), ["/srv/homes", "/srv/alice"])

    def test_a_folder_on_the_root_filesystem_and_missing_views(self) -> None:
        # /home is a plain folder of /: the only other view is / itself, which is never hidden.
        self.assertEqual(self.views(("8:1 / / ext4", "0:29 / /run tmpfs"), ["/run", "/tmp", "/home"]), [])
        # A view that is not a folder here is skipped (bwrap could not mount over it).
        rows = ("0:39 /home /var/home btrfs", "0:39 / /pool btrfs")
        self.assertEqual(self.views(rows, ["/var/home"], dirs={"/pool/home"}), ["/pool/home"])
        self.assertEqual(self.views(rows, ["/var/home"], dirs=set()), [])

    def test_hidden_trees_adds_the_other_views(self) -> None:
        # The whole btrfs mounted at /pool (not in a folder hidden anyway): its home is hidden too.
        rows = ("0:39 /home /var/home btrfs", "0:39 / /pool btrfs")
        dirs = {"/run", "/tmp", "/var/home", "/pool/home"}
        self.assertEqual(
            M.hidden_trees(mountinfo(*rows), isdir=lambda p: p in dirs, islink=lambda p: False),
            ["/run", "/tmp", "/var/home", "/pool/home"],
        )

    def test_outermost_only_and_no_repeats(self) -> None:
        rows = (
            "0:60 / /a autofs",
            "0:39 /home /var/home btrfs",
            "0:39 / /a btrfs",
            "0:39 / /b btrfs",
            "0:61 / /a/c autofs",
        )
        self.assertEqual(self.views(rows, ["/var/home"]), ["/a", "/b/home"])


class FindToolsTest(unittest.TestCase):
    def test_outside_a_virtual_environment(self) -> None:
        with self.assertRaisesRegex(M.UsageError, "cosmic-ray is not installed for this Python"):
            M.find_tools(which=lambda _: "/usr/bin/bwrap", prefix="/usr", base_prefix="/usr")

    def test_tools_from_the_venv_and_bwrap_from_path(self) -> None:
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
    def test_counts_both_enum_spellings(self) -> None:
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
                for job, *_ in [*rows, ("pending",)]:
                    db.execute("INSERT INTO work_items VALUES (?)", (job,))
                db.executemany("INSERT INTO work_results VALUES (?, ?, ?, ?)", rows)
                db.commit()
            c = M.session_counts(path)
        self.assertEqual(
            (c.total, c.skipped, c.killed, c.timeouts, c.survived, c.incompetent, c.not_run), (9, 2, 1, 1, 2, 1, 2)
        )
        self.assertEqual(c.mutants, 7)

    def test_a_session_cosmic_ray_wrote_differently_is_a_failed_step(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-mutation-test-") as tmp:
            session = os.path.join(tmp, "other.sqlite")
            with contextlib.closing(sqlite3.connect(session)) as db:
                db.execute("CREATE TABLE jobs (id TEXT)")
                db.commit()
            with self.assertRaisesRegex(M.StepError, "cannot read the numbers from"):
                M.session_counts(session)


class RunTest(unittest.TestCase):
    """The whole run with a fake bwrap and fake cosmic-ray tools."""

    def setUp(self) -> None:
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
        self.write_exec(
            self.bwrap,
            FAKE_BWRAP.format(python=sys.executable, log=self.log, control=self.control, stdin_is_null=STDIN_IS_NULL),
        )
        self.venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(self.venv, "bin"))
        for name in M.TOOLS:
            self.write_exec(
                os.path.join(self.venv, "bin", name),
                FAKE_TOOL.format(
                    python=sys.executable, log=self.log, control=self.control, stdin_is_null=STDIN_IS_NULL
                ),
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
    def write_exec(path: str, text: str) -> None:
        with open(path, "w") as fh:
            fh.write(text)
        os.chmod(path, 0o755)

    def set_control(self, **control: object) -> None:
        with open(self.control, "w") as fh:
            json.dump(control, fh)

    def execute(
        self,
        *args: str,
        tools: object = None,
        out: bool = True,
        helper: str = "pkg/tool",
    ) -> tuple[int, str, str]:
        argv = [helper, "pkg.tests.test_tool", *args] + (["--out", self.out] if out else [])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = M.execute(argv, repo=lambda: self.repo, tools=lambda: tools or self.tools, env={"HOME": self.tmp})
        return rc, stdout.getvalue(), stderr.getvalue()

    def config(self) -> dict[str, Any]:
        with open(os.path.join(self.out, "cosmic-ray.toml"), "rb") as fh:
            return tomllib.load(fh)["cosmic-ray"]

    def calls(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def steps(self) -> list[str]:
        out = []
        for c in self.calls():
            if "bwrap" in c:
                continue
            out.append(c["args"][0] if c["tool"] == "cosmic-ray" else c["tool"])
        return out

    def test_steps_in_order_all_in_the_sandbox_survivors_exit_0(self) -> None:
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

    def test_the_copy_is_a_faithful_copy(self) -> None:
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

    def test_baseline_failure_stops_before_init_with_exit_2(self) -> None:
        self.set_control(outcomes=["killed"], fail_baseline=True)
        rc, stdout, _ = self.execute("--all")
        self.assertEqual(rc, 2)
        self.assertEqual(self.steps(), ["baseline"])
        self.assertIn("the tests fail (or cannot run) without any mutation", stdout)
        self.assertFalse(os.path.exists(os.path.join(self.out, "session.sqlite")))
        self.assertEqual(os.listdir(self.scratch), [])

    def test_a_failed_step_exits_1(self) -> None:
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

    def test_mutants_that_could_not_run_exit_1(self) -> None:
        self.set_control(outcomes=["killed", "incompetent"])
        rc, stdout, _ = self.execute("--all")
        self.assertEqual(rc, 1)
        self.assertIn("some mutants did not run normally", stdout)

    def test_without_bwrap_it_refuses_and_runs_nothing(self) -> None:
        tools = M.Tools(bwrap=None, python=sys.executable, venv=self.venv)
        before = snapshot(self.repo)
        rc, _stdout, stderr = self.execute("--all", tools=tools)
        self.assertEqual(rc, 2)
        self.assertIn("bubblewrap (bwrap) is not installed", stderr)
        self.assertIn("does not run without it", stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(snapshot(self.repo), before)

    def test_a_bwrap_that_cannot_build_the_sandbox_runs_nothing(self) -> None:
        self.set_control(outcomes=["killed"], fail_probe=True)
        rc, _, stderr = self.execute("--all")
        self.assertEqual(rc, 2)
        self.assertIn("bwrap cannot build the sandbox here", stderr)
        self.assertEqual(self.steps(), [])

    def test_base_mutates_the_changed_lines(self) -> None:
        with open(os.path.join(self.repo, "pkg/tool"), "a") as fh:
            fh.write("\n\ndef added():\n    return 2\n")
        before = snapshot(self.repo)
        rc, stdout, stderr = self.execute("--base", "main")
        self.assertEqual(rc, 0, stdout + stderr)
        conf = self.config()
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"pkg/tool.py": ["20-23"]})
        self.assertEqual(snapshot(self.repo), before)

    def test_functions_choose_the_lines(self) -> None:
        rc, stdout, stderr = self.execute("--functions", "double,inner")
        self.assertEqual(rc, 0, stdout + stderr)
        conf = self.config()
        self.assertEqual(conf["filters"]["line-filter"]["lines"], {"pkg/tool.py": ["5-6", "12-13"]})

    def test_no_changes_means_nothing_to_mutate(self) -> None:
        rc, stdout, _ = self.execute()  # --base main by default, and the helper is unchanged
        self.assertEqual(rc, 0)
        self.assertIn("nothing to mutate", stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.out))

    def test_default_output_folder_is_under_the_cache(self) -> None:
        rc, stdout, stderr = self.execute("--all", out=False)
        self.assertEqual(rc, 0, stdout + stderr)
        made = os.listdir(os.path.join(self.tmp, ".cache", "cst-mutation"))
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].startswith("pkg-tool-"))
        self.assertEqual(
            M.default_out("a/b", {"XDG_CACHE_HOME": "/c", "HOME": "/h"}, 0).rsplit("-", 2)[0], "/c/cst-mutation/a-b"
        )

    def test_usage_errors_run_nothing(self) -> None:
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
                rc, _, stderr = self.execute("--all", helper=helper)
                self.assertEqual(rc, 2)
                self.assertIn(message, stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.out))

    def test_a_helper_whose_py_name_is_taken_is_refused(self) -> None:
        with open(os.path.join(self.repo, "pkg/tool.py"), "w") as fh:
            fh.write("")
        rc, _, stderr = self.execute("--all")
        self.assertEqual(rc, 2)
        self.assertIn("pkg/tool.py exists, so the .py link cosmic-ray needs cannot be made", stderr)
        self.assertEqual(self.calls(), [])

    def test_functions_of_a_helper_that_is_not_valid_python(self) -> None:
        with open(os.path.join(self.repo, "pkg/tool"), "a") as fh:
            fh.write("def broken(:\n")
        rc, _, stderr = self.execute("--functions", "double")
        self.assertEqual(rc, 2)
        self.assertIn("the helper is not valid Python", stderr)
        self.assertEqual(self.calls(), [])

    def test_an_untracked_helper_counts_as_all_new(self) -> None:
        shutil.copy2(os.path.join(self.repo, "pkg/tool"), os.path.join(self.repo, "pkg/new_tool"))
        rc, stdout, stderr = self.execute(helper="pkg/new_tool")  # --base main by default
        self.assertEqual(rc, 0, stdout + stderr)
        self.assertIn("git does not track it yet", stdout)
        self.assertEqual(self.config()["filters"]["line-filter"]["lines"], {"pkg/new_tool.py": ["1-19"]})

    def test_every_step_reads_nothing_from_the_terminal(self) -> None:
        # A mutant must not read (or wait for) the terminal the run started from: stdin is /dev/null.
        # The run gets a pipe as stdin here, so that a step that inherits it shows up whatever
        # stdin the test suite itself has (it can be /dev/null already).
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        try:
            os.dup2(read_end, 0)
            rc, stdout, stderr = self.execute("--all")
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        self.assertEqual(rc, 0, stdout + stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 15)
        self.assertEqual([c for c in calls if not c["stdin_null"]], [])

    def test_links_a_mutant_leaves_in_the_output_folder_are_not_followed(self) -> None:
        victims = os.path.join(self.tmp, "victims")
        os.mkdir(victims)
        for name in ("survivors.txt", "report.txt", "counts.json"):
            with open(os.path.join(victims, name), "w") as fh:
                fh.write("precious\n")
        self.set_control(outcomes=["killed", "survived"], plant_links=victims)
        rc, stdout, stderr = self.execute("--all")
        self.assertEqual(rc, 0, stdout + stderr)
        for name in ("survivors.txt", "report.txt", "counts.json"):
            with self.subTest(name=name):
                with open(os.path.join(victims, name)) as fh:
                    self.assertEqual(fh.read(), "precious\n")
                self.assertFalse(os.path.islink(os.path.join(self.out, name)))
        with open(os.path.join(self.out, "counts.json")) as fh:
            self.assertEqual(json.load(fh)["survived"], 1)
        with open(os.path.join(self.out, "survivors.txt")) as fh:
            self.assertIn("--- diff 1", fh.read())

    def test_output_folder_must_be_new_and_outside_the_checkout(self) -> None:
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


def sandbox_folders(work: str) -> tuple[str, str, str, str]:
    """Empty copy, out, stubs (with the stubs) and venv folders in work, for M.sandbox_argv."""
    copy, out, stubs, venv = (os.path.join(work, d) for d in ("copy", "out", "stubs", "venv"))
    for d in (copy, out, venv):
        os.mkdir(d)
    M.write_stubs(stubs)
    return copy, out, stubs, venv


def usable_bwrap(hidden: list[str]) -> tuple[str | None, str]:
    """(bwrap's path, "") when it can build the sandbox's profile here, else (None, the reason).

    The probe is the whole profile (--proc, --dev, the namespaces), as the
    helper's own first step: some containers allow a user namespace but not
    a new /proc.
    """
    path = shutil.which("bwrap")
    if path is None:
        return None, "bwrap is not installed"
    with tempfile.TemporaryDirectory(prefix="cst-mutation-probe-") as work:
        box = M.sandbox_argv(path, *sandbox_folders(os.path.realpath(work)), hidden)
        r = subprocess.run(
            [*box, "/usr/bin/true"], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
    if r.returncode != 0:
        return None, "bwrap cannot build the sandbox here: " + r.stderr.strip()
    return path, ""


# Run inside the sandbox. It tries to write in the copy and in two folders
# outside (one under /tmp, one under /var/tmp), and walks the folders where
# sockets or the home could be, six levels deep: enough for
# ~/.cache/ibus/SOCKET under /var/mnt/btrfs-top/home/USER.
PROBE = r"""
import json, os, stat, sys
copy, home_dev, home_ino, *outside = sys.argv[1:]
home_id = (int(home_dev), int(home_ino))
def attempt(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "probe"), "w") as fh:
            fh.write("x")
        return "written"
    except OSError as e:
        return "refused: " + str(e.strerror)
sockets, home_views = [], []
for root in ("/run", "/var/run", "/tmp", "/var/home", "/home", "/var/mnt", "/mnt", "/media"):
    for folder, dirs, files in os.walk(root):
        if folder.count("/") - root.count("/") >= 6:
            dirs[:] = []
        for path in [folder] + [os.path.join(folder, name) for name in files + dirs]:
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISSOCK(st.st_mode):
                sockets.append(path)
            elif stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == home_id:
                home_views.append(path)
print(json.dumps({"copy": attempt(copy), "outside": [attempt(o) for o in outside], "sockets": sockets,
                  "home_views": home_views, "home": os.environ.get("HOME")}))
"""


class RealBwrapTest(unittest.TestCase):
    """The real sandbox, when this machine can build one (CI's container usually cannot)."""

    def test_only_the_copy_is_writable_and_no_socket_or_home_is_visible(self) -> None:
        hidden = M.hidden_trees(M.read_mounts())
        bwrap, reason = usable_bwrap(hidden)
        if bwrap is None:
            self.skipTest(reason)
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
        home = os.stat(os.path.expanduser("~"))
        with contextlib.ExitStack() as stack:
            work = os.path.realpath(stack.enter_context(tempfile.TemporaryDirectory(prefix="cst-mutation-test-")))
            # Outside the sandbox's writable folders: one under /tmp (hidden inside, so a write lands in
            # the sandbox's own tmpfs), one under /var/tmp (visible inside, read-only).
            outside = [stack.enter_context(tempfile.TemporaryDirectory(prefix="cst-mutation-outside-"))]
            with contextlib.suppress(OSError):
                outside.append(
                    stack.enter_context(tempfile.TemporaryDirectory(prefix="cst-mutation-outside-", dir="/var/tmp"))
                )
            outside = [os.path.realpath(o) for o in outside]
            copy, out, stubs, venv = sandbox_folders(work)
            box = M.sandbox_argv(bwrap, copy, out, stubs, venv, hidden)
            r = subprocess.run(
                [*box, python, "-I", "-c", PROBE, copy, str(home.st_dev), str(home.st_ino), *outside],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            got = json.loads(r.stdout)
            for folder in outside:
                self.assertEqual(os.listdir(folder), [], folder)  # whatever happened inside, nothing reached it
            if len(outside) > 1:
                self.assertTrue(got["outside"][1].startswith("refused: "), got["outside"])  # the root is read-only
            self.assertEqual(got["copy"], "written")
            self.assertEqual(os.listdir(copy), ["probe"])
            self.assertEqual(got["sockets"], [])
            self.assertEqual(got["home_views"], [])
            self.assertEqual(got["home"], os.path.join(copy, ".home"))


if __name__ == "__main__":
    unittest.main()
