# SPDX-License-Identifier: MPL-2.0
"""What a caller of cpu-setup.sh (`cst cpu`) sees, whatever the script is written in: the help and
its forms, an unknown command, a closed stdout pipe, no bytecode in the checkout. The script runs
from a scratch copy of the files it needs, as a subprocess.

Run from the repository root:
    invowk cmd test module cpu.tests.test_cpu_setup
"""

from collections.abc import Mapping
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests.repo_files import REPO

# What cpu-setup.sh reads or runs for these commands (a module that is not there fails the copy).
NEEDED = ("cpu-setup.sh", "cst-install.bash", "cst-link.bash", "cst", "cpu", "cst_core")


def help_text(script: str, prog: str) -> str:
    """The help a script prints from its header: lines 2 to the first empty one, the license line
    left out, "# " taken off, then the usage line (the test's own reading, not the program's)."""
    with open(script, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    out: list[str] = []
    for number, line in enumerate(lines, start=1):
        if number < 2:
            continue
        if "SPDX-License-Identifier:" not in line:
            out.append(re.sub(r"^# ?", "", line))
        if number > 2 and line == "":
            break
    return "\n".join(out) + f"\nUsage: {prog} <command> [args]\n"


class CpuSetupCommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-setup-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "cst")
        os.mkdir(self.root)
        for name in NEEDED:
            src = os.path.join(REPO, name)
            if os.path.isdir(src):
                shutil.copytree(
                    src, os.path.join(self.root, name), ignore=shutil.ignore_patterns("__pycache__", "tests")
                )
            else:
                shutil.copy2(src, os.path.join(self.root, name))
        self.script = os.path.join(self.root, "cpu-setup.sh")

    def run_setup(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", self.script, *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
            cwd=self.tmp,
        )

    def test_every_form_of_help_prints_the_header_and_the_usage_line(self) -> None:
        for args in ((), ("help",), ("-h",), ("--help",), ("",), ("help", "extra")):
            with self.subTest(args=args):
                r = self.run_setup(*args)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, help_text(self.script, self.script), ""))
        r = self.run_setup("help", env={"CST_PROG": "cst cpu"})
        self.assertEqual(r.stdout, help_text(self.script, "cst cpu"))
        self.assertIn("Usage: cst cpu <command> [args]\n", r.stdout)
        self.assertIn("quiet [--for SECONDS] [--wait MAX] [--json]", r.stdout)

    def test_an_unknown_command_exits_2_with_the_help_on_stderr(self) -> None:
        r = self.run_setup("not-a-command", env={"CST_PROG": "cst cpu"})
        self.assertEqual(
            (r.returncode, r.stdout, r.stderr),
            (2, "", "Unknown command: not-a-command\n" + help_text(self.script, "cst cpu")),
        )

    def test_help_into_a_closed_pipe_stops_quietly(self) -> None:
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            p = subprocess.run(
                ["bash", "-c", '"$@"; echo "status=$?" >&2', "_", "bash", self.script, "help"],
                stdout=write_end,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
                timeout=60,
            )
        finally:
            os.close(write_end)
        # What the calling shell sees: 141 (128 + SIGPIPE), and nothing else on stderr.
        self.assertEqual(p.stderr, "status=141\n")

    def test_no_bytecode_is_written_into_the_checkout(self) -> None:
        for args in (("help",), ("bogus",)):
            self.run_setup(*args)
        found = [
            os.path.join(d, n)
            for d, dirs, files in os.walk(self.root)
            for n in dirs + files
            if "__pycache__" in n or n.endswith(".pyc")
        ]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
