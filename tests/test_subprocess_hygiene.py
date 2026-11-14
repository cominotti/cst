"""Static gate: tests must not let bash inherit stdin.

bash has a check for "started by sshd/rshd": when SHLVL is unset (or 0) and
stdin is a socket, it reads ~/.bashrc even for `bash -c`. Tests often run
bash with a clean environment (env={"PATH": ...}, so no SHLVL) and inherit
stdin from whoever started the test run. Agents, IDEs and task runners
sometimes give that run a socket as stdin. bash then sources the
developer's ~/.bashrc, its output lands in the test's stderr, and the test
fails only in some environments.

Rule: every subprocess call in a test that runs bash or sh (an argument
list that starts with bash/sh, or shell=True) passes stdin= or input=.
Use stdin=subprocess.DEVNULL when the command reads nothing.

tests/run_unit.py checks the same thing at run time: it runs the whole
suite with a socket as stdin, which catches the cases this static check
cannot see (a script with a bash shebang run directly, a command list built
in a variable).
"""

import ast
import os
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLS = {"run", "Popen", "call", "check_call", "check_output"}
SHELLS = {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"}


def test_files():
    out = subprocess.run(["git", "-C", REPO, "ls-files", "-co", "--exclude-standard", "*.py"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout: " + out.stderr.strip())
    for rel in out.stdout.splitlines():
        parts = rel.split("/")
        if "tests" in parts[:-1] or parts[0] == "tests":
            path = os.path.join(REPO, rel)
            if os.path.isfile(path):
                yield rel, path


def runs_a_shell(call):
    """True for subprocess.X([...bash...], ...) or subprocess.X(..., shell=True)."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in CALLS
            and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
        return False
    for kw in call.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)) and call.args[0].elts:
        first = call.args[0].elts[0]
        return isinstance(first, ast.Constant) and first.value in SHELLS
    return False


def offenders(source, rel):
    found = []
    for node in ast.walk(ast.parse(source, rel)):
        if isinstance(node, ast.Call) and runs_a_shell(node):
            if not any(kw.arg in ("stdin", "input") for kw in node.keywords):
                found.append(f"{rel}:{node.lineno}")
    return found


class SubprocessHygieneTest(unittest.TestCase):
    def test_shell_calls_in_tests_set_stdin(self):
        bad = []
        for rel, path in test_files():
            with open(path, encoding="utf-8") as f:
                bad += offenders(f.read(), rel)
        self.assertEqual(bad, [], "these calls run bash/sh with an inherited stdin; add "
                         "stdin=subprocess.DEVNULL (or input=...). Why: see the docstring of "
                         "tests/test_subprocess_hygiene.py and AGENTS.md")

    def test_the_check_finds_the_wrong_form(self):
        src = ("import subprocess\n"
               "subprocess.run(['bash', '-c', 'x'], env={})\n"
               "subprocess.run('x', shell=True)\n"
               "subprocess.run(['bash', '-c', 'x'], stdin=subprocess.DEVNULL)\n"
               "subprocess.run(['sh', '-c', 'x'], input='y', text=True)\n"
               "subprocess.run(['python3', '-c', 'x'])\n")
        self.assertEqual(offenders(src, "t.py"), ["t.py:2", "t.py:3"])


if __name__ == "__main__":
    unittest.main()
