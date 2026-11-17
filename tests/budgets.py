# SPDX-License-Identifier: MPL-2.0
"""Budgets: counts the static gates allow today, which may only go down.

A budget table maps a key, usually "<package> <layer>", to the count found
when the table was last written. A gate measures the same count and compares:

- a count above its entry fails: the change added what the budget counts;
- a count below its entry fails too, with the new number: write the lower
  number in the same change, so that what was removed cannot come back;
- a key missing from the table counts as 0, so a new package or layer starts
  with none; an entry that would be 0 is deleted instead.

The packages and layers are the ones of tests/test_architecture.py
(`package_of`, `module_role`): moving code between files of the same layer
of a package changes no budget. On a failure the gate prints the measured
table (`format_table`), ready to paste over the old one.

Standard library only.
"""

from collections.abc import Mapping

# Folders that are a layer of a package, not a package of their own
# (cst_core/adapters/files.py belongs to the package cst_core).
LAYER_DIRS = frozenset({"domain", "ports", "application", "adapters"})


def is_test_file(rel: str) -> bool:
    """A file of a test suite (tests/ at the top or inside a module)."""
    return rel.startswith("tests/") or "/tests/" in rel


def package_of(rel: str) -> str:
    """The package of a repository file: its folder, without a layer folder or a tests/ folder.

    btrfs/bulk/cli.py and btrfs/bulk/tests/test_x.py are in btrfs/bulk,
    cst_core/adapters/files.py in cst_core, vm/cst-vm in vm, tests/e2e/run.py
    in tests, a file at the top of the checkout in ".".
    """
    dirs = rel.split("/")[:-1]
    if dirs[:1] == ["tests"]:
        return "tests"
    out: list[str] = []
    for part in dirs:
        if part in LAYER_DIRS or part == "tests":
            break
        out.append(part)
    return "/".join(out) or "."


def budget_problems(measured: Mapping[str, int], budget: Mapping[str, int], what: str) -> list[str]:
    """Every key whose measured count differs from its budget, as text lines (empty: all match)."""
    out: list[str] = []
    for key in sorted(set(measured) | set(budget)):
        got, cap = measured.get(key, 0), budget.get(key, 0)
        if got > cap:
            out.append(f"{key}: {got} {what}, budget {cap}: the change added {got - cap}")
        elif got < cap and got == 0:
            out.append(f"{key}: no {what} left, budget {cap}: delete the entry")
        elif got < cap:
            out.append(f"{key}: {got} {what}, budget {cap}: lower the budget to {got}")
    return out


def format_table(measured: Mapping[str, int]) -> str:
    """The measured counts as the body of a budget table (keys sorted, zeros left out)."""
    return "\n".join(f"    {key!r}: {n}," for key, n in sorted(measured.items()) if n)
