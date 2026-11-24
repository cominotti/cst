# SPDX-License-Identifier: MPL-2.0
"""The production Python code of the checkout as a graph, for the static gates.

- `production_python()`: every Python file that ships or runs from the
  checkout (no tests, no CI scripts, no dev tools).
- `entry_points()`: the programs something starts (a setup script, systemd,
  the user): the manifest's python-entries, the checkout-only Python
  programs, and the `cli.py` modules a setup script runs.
- `module_closure(entries)`: every repository module those programs import,
  directly or through another module, by AST (function-level imports too).
- `import_bindings(node)`: the names one import statement binds, each with
  the dotted name it stands for (the architecture gate's primitive calls,
  the host-Python gate's run-time imports and newer names).
"""

import ast
from collections.abc import Iterable
import os
from typing import Final

from cst_core.domain.imports import imported_modules
from cst_core.domain.install import CHECKOUT_ONLY_EXECUTABLES, INSTALL_MANIFEST
from tests.repo_files import REPO, repo_files

# Programs that are development tools, not part of what CST runs.
_DEV_ONLY: Final = ("tests/", ".github/", "docs/")


def is_python(rel: str, repo: str = REPO) -> bool:
    """A .py file, or an extension-less file with a python shebang."""
    if rel.endswith(".py"):
        return True
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith("#!") and "python" in first


def production_python(files: Iterable[str] | None = None) -> list[str]:
    """Production Python files, repository-relative, sorted."""
    return sorted(
        rel
        for rel in (repo_files() if files is None else files)
        if not rel.startswith(_DEV_ONLY) and "/tests/" not in rel and is_python(rel)
    )


def entry_points() -> list[str]:
    """The Python programs of CST that something starts."""
    out = {e.src for e in INSTALL_MANIFEST if e.kind == "python-entry"}
    out |= {rel for rel in CHECKOUT_ONLY_EXECUTABLES if not rel.startswith(_DEV_ONLY) and is_python(rel)}
    return sorted(out)


def module_of(rel: str) -> str | None:
    """The dotted module name of a package file (None for a script or a top-level file)."""
    if not rel.endswith(".py") or "/" not in rel:
        return None
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _file_of(dotted: str, repo: str = REPO) -> str | None:
    base = dotted.replace(".", "/")
    for rel in (f"{base}.py", f"{base}/__init__.py"):
        if os.path.isfile(os.path.join(repo, rel)):
            return rel
    return None


def _with_parents(rel: str) -> list[str]:
    """rel and the __init__.py of every package above it."""
    out = [rel]
    parts = rel.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        init = "/".join(parts[:i]) + "/__init__.py"
        if os.path.isfile(os.path.join(REPO, init)):
            out.append(init)
    return out


def imported_files(rel: str) -> set[str]:
    """Repository files that `rel` imports (resolving `from pkg import mod` to pkg/mod.py)."""
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        names = imported_modules(fh.read(), rel)
    out: set[str] = set()
    for name in names:
        target = _file_of(name)
        if target is None and "." in name:
            target = _file_of(name.rsplit(".", 1)[0])  # `from pkg.mod import attr`
        if target is not None:
            out.update(_with_parents(target))
    return out


def module_closure(entries: Iterable[str]) -> set[str]:
    """Every repository file reachable by imports from the given files (themselves included)."""
    seen: set[str] = set()
    todo = list(entries)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        todo.extend(sorted(imported_files(rel) - seen))
    return seen


def top_level_nodes(source: str, rel: str) -> list[ast.stmt]:
    """The statements at the top of a module."""
    return list(ast.parse(source, filename=rel).body)


def import_bindings(node: ast.AST) -> list[tuple[str, str]]:
    """The names an import statement binds and the dotted name each stands for (import os.path: os is
    os; import os as o: o is os; from os import sep as s: s is os.sep); none for another node or a
    relative import."""
    if isinstance(node, ast.Import):
        bindings: list[tuple[str, str]] = []
        for alias in node.names:
            top = alias.name.split(".", 1)[0]
            bindings.append((top, top) if alias.asname is None else (alias.asname, alias.name))
        return bindings
    if isinstance(node, ast.ImportFrom) and not node.level and node.module is not None:
        return [(alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names]
    return []
