# SPDX-License-Identifier: MPL-2.0
"""Programs run from a checkout write no bytecode into it.

On main every helper was one file that imported only the standard library,
so running it from the checkout wrote nothing there. Now an entry imports its
package and cst_core from the checkout, and Python writes __pycache__ folders
next to them unless it runs with -B. Run as root (`cst status`, `cst vm
backup`, ...), those folders are root's, and the user can no longer delete
the checkout (`git clean -xfd`, `rm -rf` fail).

Rule: every `python3 -I` that starts a CST program, in the shell scripts and
in the Python argv lists, passes -B too (`python3 -I -c` and `python3 -I -`
run inline code and import nothing of CST). An entry run by its shebang
(`#!/usr/bin/python3 -I`, no -B) is covered by the bootstrap
(tests/test_entrypoints.py), which sets sys.dont_write_bytecode from a
checkout before any CST import.
"""

import ast
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from cst_core.adapters.runtime import SYSTEM_PYTHON
from tests.duplication import is_shell
from tests.repo_files import REPO, repo_files
from tests.run_unit import HOST_GUARD_ENV
from tests.test_distribution import HELP_MODES

_SHELL_CALL = re.compile(r"python3 -I(?! -B)(?! -c)(?! - )(?! -$)")


def _production(rel: str) -> bool:
    parts = rel.split("/")
    return parts[0] != "tests" and "tests" not in parts[:-1]


def shell_calls_without_b(text: str) -> list[str]:
    """The lines of a shell script that start python3 -I without -B (inline code excepted)."""
    return [
        line.strip() for line in text.splitlines() if _SHELL_CALL.search(line) and not line.lstrip().startswith("#")
    ]


def python_argv_without_b(source: str) -> list[int]:
    """Line numbers of list and tuple literals in Python code that hold "-I" not followed by "-B"."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        words = [elt.value if isinstance(elt, ast.Constant) else None for elt in node.elts]
        for i, word in enumerate(words):
            if word == "-I" and (i + 1 >= len(words) or words[i + 1] not in ("-B", "-c", "-")):
                found.append(node.lineno)
    return found


def copy_checkout(dest: str) -> None:
    """Copy the checkout's own files (no .git, caches or virtual environments) to dest, modes kept."""
    for rel in repo_files():
        os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), os.path.join(dest, rel))


def bytecode_in(root: str) -> list[str]:
    """The .pyc files under root, relative to it."""
    return sorted(
        os.path.relpath(os.path.join(d, n), root) for d, _, names in os.walk(root) for n in names if n.endswith(".pyc")
    )


class NoBytecodeInTheCheckoutTest(unittest.TestCase):
    def test_the_rule_reads_what_it_should(self) -> None:
        self.assertEqual(
            shell_calls_without_b('python3 -I "${SRC}/vm-pin" status\npython3 -I -B "${X}"\npython3 -I -c "1"\n'),
            ['python3 -I "${SRC}/vm-pin" status'],
        )
        self.assertEqual(shell_calls_without_b("python3 -I - \"${F}\" <<'PY'\n"), [])
        self.assertEqual(python_argv_without_b('a = ["python3", "-I", p]\nb = ["python3", "-I", "-B", p]\n'), [1])
        self.assertEqual(python_argv_without_b('os.execv(py, (py, "-I", prog))\nc = ("python3", "-I", "-B", p)\n'), [1])

    def test_shell_scripts_pass_b(self) -> None:
        bad: list[str] = []
        for rel in repo_files():
            if _production(rel) and is_shell(rel, REPO):
                with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                    bad.extend(f"{rel}: {line}" for line in shell_calls_without_b(fh.read()))
        self.assertEqual(bad, [], "python3 -I without -B writes __pycache__ into the checkout")

    def test_python_argv_lists_pass_b(self) -> None:
        bad: list[str] = []
        for rel in repo_files():
            if _production(rel) and rel.endswith(".py"):
                with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                    bad.extend(f"{rel}:{n}" for n in python_argv_without_b(fh.read()))
        self.assertEqual(bad, [], '"-I" without "-B" writes __pycache__ into the checkout')

    def test_a_module_run_from_a_checkout_leaves_no_bytecode(self) -> None:
        # toolchains-setup.sh execs its program from the checkout; a usage error still imports it all.
        with tempfile.TemporaryDirectory(prefix="cst-pyc-") as tmp:
            copy = os.path.join(tmp, "cst")
            copy_checkout(copy)
            proc = subprocess.run(
                ["bash", os.path.join(copy, "toolchains-setup.sh"), "config", "--no-such-option"],
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "HOME": tmp, "LC_ALL": "C.UTF-8"},
                timeout=60,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertEqual(bytecode_in(copy), [])

    @unittest.skipUnless(os.access(SYSTEM_PYTHON, os.X_OK), "needs /usr/bin/python3")
    def test_entries_run_by_their_shebang_leave_no_bytecode(self) -> None:
        # The shebang is `#!/usr/bin/python3 -I`, without -B: the bootstrap keeps bytecode out.
        guard = os.environ.get(HOST_GUARD_ENV)  # the runner's refusing stubs stay first on PATH
        path = ("" if guard is None else guard + os.pathsep) + "/usr/bin:/bin"
        with tempfile.TemporaryDirectory(prefix="cst-pyc-") as tmp:
            copy = os.path.join(tmp, "cst")
            copy_checkout(copy)
            for src, (argv, rc) in sorted(HELP_MODES.items()):
                entry = os.path.join(copy, src)
                with self.subTest(src=src):
                    self.assertTrue(os.access(entry, os.X_OK), f"{src}: the copy lost its execute bit")
                    proc = subprocess.run(
                        [entry, *argv],
                        capture_output=True,
                        text=True,
                        check=False,
                        cwd=tmp,
                        stdin=subprocess.DEVNULL,
                        env={"PATH": path, "HOME": tmp, "LC_ALL": "C.UTF-8"},
                        timeout=60,
                    )
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
            self.assertEqual(bytecode_in(copy), [])


if __name__ == "__main__":
    unittest.main()
