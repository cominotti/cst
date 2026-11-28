# SPDX-License-Identifier: MPL-2.0
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
from collections.abc import Iterator
import os
from typing import Final
import unittest

from tests.budgets import budget_problems, lowered
from tests.repo_files import REPO, repo_files

CALLS: Final = frozenset({"run", "Popen", "call", "check_call", "check_output"})
SHELLS: Final = frozenset({"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"})
_SUBPROCESS_BUDGET_EXEMPT_PREFIXES: Final = ("tests/e2e/", "tests/mutation/")
SUBPROCESS_CALL_CAP: Final = 4

# Existing unit test files above SUBPROCESS_CALL_CAP (ratcheting budget: never
# above; lowered with `invowk cmd fix budgets --apply`). See .agents/rules/fast-tests-and-ci.md.
SUBPROCESS_BUDGETS: Final[dict[str, int]] = {
    "bluetooth/tests/test_bluetooth.py": 22,
    "bluetooth/tests/test_ldac_watch.py": 5,
    "btrfs/tests/test_btrfs_setup.py": 19,
    "btrfs/tests/test_pending_alerts.py": 5,
    "btrfs/tests/test_stall_report.py": 5,
    "tests/test_cst_core.py": 12,
    "tests/test_cst_core_shared.py": 8,
    "tests/test_run_unit.py": 15,
    "toolchains/tests/test_rules.py": 13,
    "toolchains/tests/test_toolchains.py": 9,
    "vm/tests/test_vm.py": 9,
    "vm/tests/test_vm_nas.py": 11,
}


def test_files() -> Iterator[tuple[str, str]]:
    for rel in repo_files():
        parts = rel.split("/")
        if rel.endswith(".py") and "tests" in parts[:-1]:
            yield rel, os.path.join(REPO, rel)


def runs_a_shell(call: ast.Call) -> bool:
    """True for subprocess.X([...bash...], ...) or subprocess.X(..., shell=True)."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr in CALLS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    ):
        return False
    for kw in call.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)) and call.args[0].elts:
        first = call.args[0].elts[0]
        return isinstance(first, ast.Constant) and first.value in SHELLS
    return False


def offenders(source: str, rel: str) -> list[str]:
    return [
        f"{rel}:{node.lineno}"
        for node in ast.walk(ast.parse(source, rel))
        if isinstance(node, ast.Call)
        and runs_a_shell(node)
        and not any(kw.arg in ("stdin", "input") for kw in node.keywords)
    ]


def subprocess_call_count(source: str, rel: str) -> int:
    """Count direct `subprocess.<call>` AST calls in `source`."""
    return sum(
        1
        for node in ast.walk(ast.parse(source, rel))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CALLS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    )


def measure_subprocess_calls() -> dict[str, int]:
    """Direct subprocess call counts per unit test file (`tests/e2e/` and `tests/mutation/` excluded)."""
    counts: dict[str, int] = {}
    for rel, path in test_files():
        if rel.startswith(_SUBPROCESS_BUDGET_EXEMPT_PREFIXES):
            continue
        with open(path, encoding="utf-8") as f:
            counts[rel] = subprocess_call_count(f.read(), rel)
    return counts


class SubprocessHygieneTest(unittest.TestCase):
    def test_shell_calls_in_tests_set_stdin(self) -> None:
        bad: list[str] = []
        for rel, path in test_files():
            with open(path, encoding="utf-8") as f:
                bad += offenders(f.read(), rel)
        self.assertEqual(
            bad,
            [],
            "these calls run bash/sh with an inherited stdin; add "
            "stdin=subprocess.DEVNULL (or input=...). Why: see the docstring of "
            "tests/test_subprocess_hygiene.py and AGENTS.md",
        )

    def test_the_check_finds_the_wrong_form(self) -> None:
        src = (
            "import subprocess\n"
            "subprocess.run(['bash', '-c', 'x'], env={})\n"
            "subprocess.run('x', shell=True)\n"
            "subprocess.run(['bash', '-c', 'x'], stdin=subprocess.DEVNULL)\n"
            "subprocess.run(['sh', '-c', 'x'], input='y', text=True)\n"
            "subprocess.run(['python3', '-c', 'x'])\n"
        )
        self.assertEqual(offenders(src, "t.py"), ["t.py:2", "t.py:3"])

    def test_subprocess_calls_within_budget(self) -> None:
        problems = budget_problems(
            measure_subprocess_calls(), SUBPROCESS_BUDGETS, "subprocess calls", default=SUBPROCESS_CALL_CAP
        )
        self.assertEqual(
            problems,
            [],
            f"a test file above its SUBPROCESS_BUDGETS entry, or above {SUBPROCESS_CALL_CAP} subprocess calls: "
            "run behaviour tests in-process through port fakes (.agents/rules/fast-tests-and-ci.md)",
        )

    def test_subprocess_budget_rule(self) -> None:
        budget = {"legacy.py": 8}
        self.assertEqual(budget_problems({"legacy.py": 8, "new.py": 4}, budget, "subprocess calls", default=4), [])
        self.assertEqual(budget_problems({"legacy.py": 5}, budget, "subprocess calls", default=4), [])
        self.assertEqual(
            budget_problems({"legacy.py": 9, "new.py": 5}, budget, "subprocess calls", default=4),
            [
                "legacy.py: 9 subprocess calls, budget 8: the change added 1",
                "new.py: 5 subprocess calls, budget 4: the change added 1",
            ],
        )
        self.assertEqual(lowered({"legacy.py": 5, "dropped.py": 4}, budget, default=4), {"legacy.py": 5})
        self.assertEqual(lowered({"legacy.py": 4}, budget, default=4), {})


if __name__ == "__main__":
    unittest.main()
