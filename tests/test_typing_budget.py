# SPDX-License-Identifier: MPL-2.0
"""Typing budget: `Any` and `cast()` may not go above their entries.

ruff's ANN401 already refuses `Any` as a whole annotation, so what is left
hides inside generics (`dict[str, Any]`, `Mapping[str, Any]`,
`Callable[..., Any]`), and `cast()` tells the type checker to trust a value
nobody checked. Both are counted per package and layer (tests/budgets.py,
the layers of tests/test_architecture.py), the production code and each
package's tests apart ("<package> tests"), in every Python file of the
repository:

- `Any`: every use of the name bound to `typing.Any` (`from typing import
  Any`, an alias, `typing.Any`), also inside string annotations;
- `cast`: every call of `typing.cast`.

ANY_BUDGETS and CAST_BUDGETS hold the highest counts allowed. A change that
adds one over its entry fails (the failure prints the measured table); a
change that removes some passes, and `invowk cmd fix budgets` lowers the
entries later. Type the value instead: a TypedDict for a
record read from JSON, a frozen dataclass for one built in code, a
`type Json = ...` alias for arbitrary JSON, isinstance narrowing instead of
a cast.

Standard library only.
"""

import ast
import os
from typing import Final
import unittest

from tests.budgets import budget_problems, format_table, is_test_file, package_of
from tests.code_graph import is_python
from tests.repo_files import REPO, repo_files
from tests.test_architecture import module_role

ANY_BUDGETS: Final[dict[str, int]] = {
    "bluetooth tests": 5,
    "btrfs tests": 65,
    "btrfs/stall_report adapters": 2,
    "doctor adapters": 2,
    "doctor domain": 1,
    "doctor ports": 2,
    "tests tests": 6,
    "toolchains tests": 2,
    "vm adapters": 10,
    "vm cli": 16,
    "vm domain": 8,
    "vm tests": 5,
}
CAST_BUDGETS: Final[dict[str, int]] = {
    "bluetooth tests": 1,
    "btrfs tests": 16,
    "tests tests": 4,
    "toolchains tests": 2,
    "vm tests": 1,
}


def layer_key(rel: str) -> str:
    """ "<package> <layer>" for production code, "<package> tests" for a test file."""
    if is_test_file(rel):
        return f"{package_of(rel)} tests"
    if rel.startswith(".github/"):
        return f"{package_of(rel)} ci"
    return f"{package_of(rel)} {module_role(rel) or 'unlayered'}"


def _typing_names(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """(names bound to typing.Any, names bound to typing.cast, names bound to the typing module)."""
    any_names: set[str] = set()
    cast_names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if alias.name == "Any":
                    any_names.add(alias.asname or "Any")
                elif alias.name == "cast":
                    cast_names.add(alias.asname or "cast")
        elif isinstance(node, ast.Import):
            modules |= {alias.asname or "typing" for alias in node.names if alias.name == "typing"}
    return any_names, cast_names, modules


def _is_named(node: ast.expr, names: set[str], modules: set[str], attr: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    )


def _annotation_strings(tree: ast.Module, cast_names: set[str], modules: set[str]) -> list[str]:
    """The string annotations of a module (`x: "dict[str, Any]"`), and the type given to cast() as a string."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            found.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            found.append(node.returns)
        elif isinstance(node, ast.Call) and node.args and _is_named(node.func, cast_names, modules, "cast"):
            found.append(node.args[0])
    out: list[str] = []
    for ann in found:
        out += [n.value for n in ast.walk(ann) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return out


def typing_counts(source: str, rel: str) -> tuple[int, int]:
    """(uses of Any, calls of cast) in one module."""
    tree = ast.parse(source, filename=rel)
    any_names, cast_names, modules = _typing_names(tree)
    n_any = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and _is_named(node, any_names, modules, "Any")
    )
    for text in _annotation_strings(tree, cast_names, modules):
        try:
            inner = ast.parse(text, mode="eval")
        except SyntaxError:
            continue
        n_any += sum(
            1 for node in ast.walk(inner) if isinstance(node, ast.expr) and _is_named(node, any_names, modules, "Any")
        )
    n_cast = sum(
        1 for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_named(node.func, cast_names, modules, "cast")
    )
    return n_any, n_cast


def measure() -> tuple[dict[str, int], dict[str, int]]:
    """Any and cast counts per layer key over the repository."""
    anys: dict[str, int] = {}
    casts: dict[str, int] = {}
    for rel in repo_files():
        if not is_python(rel):
            continue
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            n_any, n_cast = typing_counts(fh.read(), rel)
        key = layer_key(rel)
        anys[key] = anys.get(key, 0) + n_any
        casts[key] = casts.get(key, 0) + n_cast
    return anys, casts


class TypingBudgetTest(unittest.TestCase):
    def test_any_and_cast_stay_within_their_budgets(self) -> None:
        anys, casts = measure()
        problems = budget_problems(anys, ANY_BUDGETS, "uses of Any") + budget_problems(
            casts, CAST_BUDGETS, "calls of cast()"
        )
        self.assertEqual(
            problems,
            [],
            "type the value instead (see the module docstring); measured tables:\nANY_BUDGETS:\n"
            + format_table(anys)
            + "\nCAST_BUDGETS:\n"
            + format_table(casts),
        )

    def test_counts(self) -> None:
        src = (
            "import typing as t\nfrom typing import Any as A, cast\n"
            "x: dict[str, A] = {}\ny: 't.Any'\n"
            "def f(a: 'list[A]') -> t.Any:\n    return cast('A', a) or cast(int, a) or t.cast(str, a)\n"
            "z = 'Any is a word here'\nAny = 1\nprint('A')\n"
        )
        self.assertEqual(typing_counts(src, "x.py"), (5, 3))
        self.assertEqual(typing_counts("from typing import Any\n", "x.py"), (0, 0))

    def test_layer_keys(self) -> None:
        self.assertEqual(layer_key("vm/cli.py"), "vm cli")
        self.assertEqual(layer_key("btrfs/bulk/steam.py"), "btrfs/bulk unlayered")
        self.assertEqual(layer_key("cst_core/domain/values.py"), "cst_core domain")
        self.assertEqual(layer_key("vm/tests/test_vm.py"), "vm tests")
        self.assertEqual(layer_key("tests/test_docs.py"), "tests tests")
        self.assertEqual(layer_key(".github/scripts/pin_invowk.py"), ".github/scripts ci")


if __name__ == "__main__":
    unittest.main()
