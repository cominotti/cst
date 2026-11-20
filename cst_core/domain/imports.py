# SPDX-License-Identifier: MPL-2.0
"""Pure rules about which modules a Python source imports (by AST, without running it)."""

import ast
from typing import Final

# The file name ast.parse puts in a SyntaxError when the caller names none.
_SOURCE_PLACEHOLDER: Final = "<source>"


def imported_modules(source: str, filename: str = _SOURCE_PLACEHOLDER) -> frozenset[str]:
    """Dotted names of every absolute import in `source`, at any depth.

    Function-level imports count too: a helper that imports inside main()
    still needs the module at run time. Relative imports (`from . import x`)
    are left out: they stay inside the package that holds the file.
    """
    tree = ast.parse(source, filename=filename)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")
    return frozenset(names)


def top_level_packages(source: str, filename: str = _SOURCE_PLACEHOLDER) -> frozenset[str]:
    """The first component of every absolute import in `source`."""
    return frozenset(name.split(".", 1)[0] for name in imported_modules(source, filename))


def loads_code_by_path(source: str, filename: str = _SOURCE_PLACEHOLDER) -> list[int]:
    """Line numbers where `source` loads Python code from a file path at run time.

    Such loads (SourceFileLoader, spec_from_file_location, runpy.run_path,
    exec of a file) are invisible to the import closure, so a program that
    uses them may reach into the checkout once installed.
    """
    tree = ast.parse(source, filename=filename)
    loaders = {"SourceFileLoader", "spec_from_file_location", "run_path"}
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr in loaders)
        or (isinstance(node, ast.Name) and node.id in loaders)
    ]
    return sorted(set(lines))
