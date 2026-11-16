"""Static gate for CST's Python entry points (the programs something starts).

- **One bootstrap.** A program run from the checkout under `python3 -I` sees
  only its own folder and the standard library, so before it can import
  cst_core or its package it adds the checkout root to sys.path. Every entry
  that imports repository code does that with BOOTSTRAP below, byte for byte,
  and nothing else in production code changes sys.path. Inside an installed
  zipapp the stanza does nothing (the archive is already on sys.path).
  Everything else about paths (the program's own path, siblings, data
  files, the installed bytes) goes through cst_core.adapters.runtime.
- **Entry points only wire argv.** An extension-less program holds a
  docstring, the bootstrap, imports, one `main()` and the `__main__` guard:
  its logic lives once, in its package, where tests import it. Rule chosen
  over a size cap: a cap lets logic creep back a few lines at a time.
- **No re-export facades.** `from m import a as a` (the explicit re-export
  idiom) is not used in production code: tests import the package that
  defines a name and inject fakes through ports; a patch on a re-exported
  name changes a copy that nothing calls.
"""

import ast
import os
from typing import Final
import unittest

from tests.code_graph import entry_points, module_closure, production_python
from tests.repo_files import REPO

BOOTSTRAP: Final = """\
import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file; inside an installed zipapp, sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.path.insert(0, _cst_root)
"""

# Entry points of modules not converted yet (their logic still lives in the
# program itself). Each later stage of the conversion removes its own.
NOT_YET_THIN: Final[frozenset[str]] = frozenset(
    {
        "btrfs/btrfs-bulk",
        "btrfs/btrfs-snapshot-core",
        "btrfs/btrfs-stall-monitord",
        "btrfs/btrfs-stall-report",
        "btrfs/chrome-cache",
        "btrfs/docker-gc",
        "btrfs/docker-volumes-nocow",
        "toolchains/cst-toolchains",
    }
)


def _read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _is_sys_path(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "path" and isinstance(node.value, ast.Name)


def sys_path_mutations(source: str, rel: str) -> list[int]:
    """Lines that change sys.path (insert/append/extend/remove, assignment, item assignment)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source, filename=rel)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append", "extend", "remove", "clear", "pop"}
            and _is_sys_path(node.func.value)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id in {"sys", "_cst_sys"}
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Assign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                base = t.value if isinstance(t, ast.Subscript) else t
                if isinstance(base, ast.Attribute) and base.attr == "path" and isinstance(base.value, ast.Name):
                    if base.value.id in {"sys", "_cst_sys"}:
                        lines.append(node.lineno)
    return lines


def without_bootstrap(source: str) -> str:
    """The source with the canonical bootstrap removed (once)."""
    return source.replace(BOOTSTRAP, "", 1)


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def thin_entry_problems(source: str, rel: str) -> list[str]:
    """Top-level statements other than docstring, bootstrap, imports, main() and the guard."""
    problems: list[str] = []
    body = ast.parse(without_bootstrap(source), filename=rel).body
    mains = 0
    for i, node in enumerate(body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom) or _is_main_guard(node):
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            mains += 1
            continue
        what = getattr(node, "name", type(node).__name__)
        problems.append(f"{rel}:{node.lineno}: {what} at the top of an entry point (move it into the package)")
    if mains != 1:
        problems.append(f"{rel}: an entry point defines exactly one main() (found {mains})")
    return problems


# The cst_core.adapters.runtime functions (and an entry's hand-over to its
# package's main) that may receive __file__: everything else that needs a
# path asks them.
FILE_CONSUMERS: Final[frozenset[str]] = frozenset(
    {"checkout_root", "install_payload", "main", "program_argv", "program_path", "running_archive", "sibling_program"}
)
# Programs that still compute paths from __file__ themselves (their stage
# moves them onto cst_core.adapters.runtime).
FILE_PATHS_NOT_YET: Final[frozenset[str]] = frozenset(
    {
        "btrfs/adapters.py",
        "toolchains/adapters.py",
        "toolchains/cst-toolchains",
    }
)


def file_path_problems(source: str, rel: str) -> list[str]:
    """Uses of __file__ outside the bootstrap that are not an argument of a FILE_CONSUMERS call."""
    tree = ast.parse(without_bootstrap(source), filename=rel)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in FILE_CONSUMERS:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    allowed.add(id(arg))
    return [
        f"{rel}:{node.lineno}: computes a path from __file__ (ask cst_core.adapters.runtime)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "__file__" and id(node) not in allowed
    ]


def reexport_problems(source: str, rel: str) -> list[str]:
    """`import a as a` / `from m import a as a`: the explicit re-export idiom."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename=rel)):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.asname is not None and alias.asname == alias.name.rsplit(".", 1)[-1]:
                    problems.append(f"{rel}:{node.lineno}: re-exports {alias.name} (import it where it is defined)")
    return problems


class EntryPointGateTest(unittest.TestCase):
    def test_entries_that_import_repository_code_carry_the_bootstrap(self) -> None:
        problems: list[str] = []
        for rel in entry_points():
            reaches = module_closure([rel]) - {rel}
            if reaches and BOOTSTRAP not in _read(rel):
                problems.append(f"{rel}: imports {sorted(reaches)[0]} but lacks the canonical bootstrap")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_only_the_bootstrap_changes_sys_path(self) -> None:
        problems: list[str] = []
        for rel in production_python():
            for line in sys_path_mutations(without_bootstrap(_read(rel)), rel):
                problems.append(f"{rel}:{line}: changes sys.path (use the bootstrap in an entry point only)")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_entry_points_only_wire_argv(self) -> None:
        problems: list[str] = []
        for rel in entry_points():
            if rel.endswith(".py") or rel in NOT_YET_THIN:
                continue
            problems.extend(thin_entry_problems(_read(rel), rel))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_paths_come_from_the_runtime_resolver(self) -> None:
        problems: list[str] = []
        for rel in production_python():
            if rel not in FILE_PATHS_NOT_YET:
                problems.extend(file_path_problems(_read(rel), rel))
        self.assertEqual(problems, [], "\n".join(problems))
        stale = sorted(rel for rel in FILE_PATHS_NOT_YET if not file_path_problems(_read(rel), rel))
        self.assertEqual(stale, [], "no longer computes paths from __file__: remove from FILE_PATHS_NOT_YET")
        self.assertTrue(file_path_problems("import os\nHERE = os.path.dirname(__file__)\n", "x"))
        self.assertEqual(file_path_problems("X = sibling_program('a/b', __file__)\n", "x"), [])
        self.assertEqual(file_path_problems(BOOTSTRAP, "x"), [])

    def test_not_yet_thin_names_real_entry_points(self) -> None:
        self.assertLessEqual(NOT_YET_THIN, set(entry_points()))

    def test_no_reexport_facades(self) -> None:
        problems = [p for rel in production_python() for p in reexport_problems(_read(rel), rel)]
        self.assertEqual(problems, [], "\n".join(problems))

    def test_checkers(self) -> None:
        self.assertEqual(sys_path_mutations("import sys\nsys.path.insert(0, 'x')\n", "a"), [2])
        self.assertEqual(sys_path_mutations("import sys\nsys.path[0:0] = ['x']\n", "a"), [2])
        self.assertEqual(sys_path_mutations(BOOTSTRAP, "a"), [9])
        self.assertEqual(sys_path_mutations(without_bootstrap(BOOTSTRAP), "a"), [])
        self.assertEqual(
            reexport_problems("from m import a as a\nimport b as c\n", "x"),
            ["x:1: re-exports a (import it where it is defined)"],
        )
        thin = (
            '"""doc"""\n'
            + BOOTSTRAP
            + "import sys\nfrom p.cli import run\n\ndef main():\n    return run(sys.argv[1:])\n\nif __name__ == '__main__':\n    main()\n"
        )
        self.assertEqual(thin_entry_problems(thin, "x"), [])
        self.assertTrue(thin_entry_problems(thin + "X = 1\n", "x"))
        self.assertTrue(thin_entry_problems(thin + "def helper():\n    pass\n", "x"))


if __name__ == "__main__":
    unittest.main()
