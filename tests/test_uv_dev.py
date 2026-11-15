"""Unit tests for tests/uv-dev, the wrapper the invowk tasks `test pylint`
and `test types` run ruff and ty through.

A fake `uv` on PATH records its arguments, working directory and the
environment it got; no real uv runs and nothing is downloaded.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest tests.test_uv_dev -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UV_DEV = os.path.join(REPO, "tests", "uv-dev")

FAKE_UV = """#!/usr/bin/python3 -I
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                  "venv": os.environ.get("UV_PROJECT_ENVIRONMENT"),
                  "ruff_cache": os.environ.get("RUFF_CACHE_DIR")}))
sys.exit(int(os.environ.get("FAKE_UV_RC", "0")))
"""


def key(path):
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:12]


class UvDevTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cst-uv-dev-")
        self.addCleanup(self.tmp.cleanup)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.mkdir(self.bin)
        self.home = os.path.join(self.tmp.name, "home")
        os.mkdir(self.home)
        # Only the programs uv-dev needs, so that a uv installed on this
        # machine is never found.
        self.tools = os.path.join(self.tmp.name, "tools")
        os.mkdir(self.tools)
        for prog in ("bash", "dirname", "readlink", "sha1sum", "cut"):
            found = shutil.which(prog)
            if found is None:
                self.fail(f"{prog} is not on PATH")
            os.symlink(found, os.path.join(self.tools, prog))

    def fake_uv(self):
        path = os.path.join(self.bin, "uv")
        with open(path, "w") as fh:
            fh.write(FAKE_UV)
        os.chmod(path, 0o755)

    def run_uv_dev(self, *args, script=UV_DEV, env=None, cwd=None):
        base = {"PATH": f"{self.bin}:{self.tools}", "HOME": self.home}
        return subprocess.run(
            [os.path.join(self.tools, "bash"), script, *args],
            check=False,
            capture_output=True,
            text=True,
            env={**base, **(env or {})},
            cwd=cwd or self.tmp.name,
            stdin=subprocess.DEVNULL,
        )

    def test_runs_the_locked_dev_group_from_the_repository(self):
        self.fake_uv()
        r = self.run_uv_dev("ruff", "check", "--select", "ANN")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        real = os.path.realpath(REPO)
        self.assertEqual(
            got["argv"], ["run", "--locked", "--project", real, "--group", "dev", "ruff", "check", "--select", "ANN"]
        )
        self.assertEqual(got["cwd"], real)  # started elsewhere, runs at the top of the checkout

    def test_environment_is_outside_the_checkout_one_per_checkout(self):
        self.fake_uv()
        got = json.loads(self.run_uv_dev("ty", "check").stdout)
        cache = os.path.join(self.home, ".cache")
        self.assertEqual(got["venv"], os.path.join(cache, "cst-dev-venv", key(REPO)))
        self.assertEqual(got["ruff_cache"], os.path.join(cache, "cst-dev-ruff", key(REPO)))
        self.assertFalse(got["venv"].startswith(os.path.realpath(REPO) + os.sep))
        # XDG_CACHE_HOME wins over ~/.cache.
        xdg = os.path.join(self.tmp.name, "xdg")
        got = json.loads(self.run_uv_dev("ty", "check", env={"XDG_CACHE_HOME": xdg}).stdout)
        self.assertEqual(got["venv"], os.path.join(xdg, "cst-dev-venv", key(REPO)))
        self.assertEqual(got["ruff_cache"], os.path.join(xdg, "cst-dev-ruff", key(REPO)))

    def test_a_symlinked_path_to_the_checkout_gets_the_same_environment(self):
        # /home and /var/home name the same folder on Silverblue: one venv, not two.
        self.fake_uv()
        link = os.path.join(self.tmp.name, "link")
        os.symlink(REPO, link)
        direct = json.loads(self.run_uv_dev("ty").stdout)
        via_link = json.loads(self.run_uv_dev("ty", script=os.path.join(link, "tests", "uv-dev")).stdout)
        self.assertEqual(via_link["venv"], direct["venv"])
        self.assertEqual(via_link["cwd"], os.path.realpath(REPO))

    def test_group_option_adds_groups_to_dev(self):
        self.fake_uv()
        real = os.path.realpath(REPO)
        head = ["run", "--locked", "--project", real, "--group", "dev"]
        got = json.loads(self.run_uv_dev("--group", "mutation", "cosmic-ray", "--version").stdout)
        self.assertEqual(got["argv"], [*head, "--group", "mutation", "cosmic-ray", "--version"])
        got = json.loads(self.run_uv_dev("--group", "a", "--group", "b", "tool").stdout)
        self.assertEqual(got["argv"], [*head, "--group", "a", "--group", "b", "tool"])

    def test_group_after_the_tool_belongs_to_the_tool(self):
        # Only leading --group options are uv-dev's; the tool's own arguments are left alone.
        self.fake_uv()
        real = os.path.realpath(REPO)
        got = json.loads(self.run_uv_dev("tool", "--group", "x").stdout)
        self.assertEqual(got["argv"], ["run", "--locked", "--project", real, "--group", "dev", "tool", "--group", "x"])

    def test_group_without_a_name_is_a_usage_error(self):
        self.fake_uv()
        for args in (("--group",), ("--group", ""), ("--group", "--group", "x", "tool")):
            with self.subTest(args=args):
                r = self.run_uv_dev(*args)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")  # uv never ran
                self.assertIn("uv-dev: --group needs the name of a dependency group", r.stderr)

    def test_exit_code_of_the_tool_is_kept(self):
        self.fake_uv()
        self.assertEqual(self.run_uv_dev("ruff", "check", env={"FAKE_UV_RC": "1"}).returncode, 1)

    def test_without_uv_it_says_so(self):
        r = self.run_uv_dev("ruff", "check")  # no fake uv this time
        self.assertEqual(r.returncode, 127)
        self.assertIn("uv-dev: uv is not installed", r.stderr)


if __name__ == "__main__":
    unittest.main()
