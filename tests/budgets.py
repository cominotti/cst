# SPDX-License-Identifier: MPL-2.0
"""Budgets: counts the static gates allow, never above their entry.

A budget table maps a key, usually "<package> <layer>", to the highest count
allowed. A gate measures the same count and compares:

- a count above its entry fails: the change added what the budget counts;
- a count at or below its entry passes. A change that removes some need not
  touch the table, so parallel changes do not conflict on it;
  `invowk cmd fix budgets` (tests/fix_budgets.py, with `lowered`) brings
  the entries down to the measured counts later, and never raises one;
- a key missing from the table counts as 0 (or as the table's `default`),
  so a new package or layer starts with none; an entry that would be 0 is
  deleted instead.

(User decision 2026-11-17: before it, a count below its entry failed too,
and every parallel change had to rewrite the numbers.)

The packages and layers are the ones of tests/test_architecture.py
(`package_of`, `module_role`): moving code between files of the same layer
of a package changes no budget. On a failure the gate prints the measured
table (`format_table`); `invowk cmd fix budgets --apply` writes the lower values.

Standard library only.
"""

from collections.abc import Mapping

# Folders that are a layer of a package, not a package of their own
# (cst_core/adapters/files.py belongs to the package cst_core, vm/cli/net.py
# to vm).
LAYER_DIRS = frozenset({"domain", "ports", "application", "adapters", "cli"})


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


def budget_problems[N: (int, float)](
    measured: Mapping[str, N], budget: Mapping[str, N], what: str, *, default: N = 0
) -> list[str]:
    """Every key whose measured count is above its budget, as text lines (empty: none is).

    A key missing from `budget` has the budget `default`. A count below its
    entry is no problem (`lowered` gives the table at the measured counts).
    """
    out: list[str] = []
    for key in sorted(measured):
        got, cap = measured[key], budget.get(key, default)
        if got > cap:
            out.append(f"{key}: {got} {what}, budget {cap}: the change added {got - cap}")
    return out


def lowered[N: (int, float)](
    measured: Mapping[str, N], budget: Mapping[str, N], *, default: N = 0, keep_keys: bool = False
) -> dict[str, N]:
    """`budget` with each entry lowered to its measured count (never raised, no key added).

    A key not measured counts as 0. An entry that falls to `default` or below
    is dropped (a missing key means `default`), unless `keep_keys` (a table
    of fixed metrics, such as the duplication ceilings, keeps every key).
    """
    out: dict[str, N] = {}
    for key, cap in budget.items():
        new = min(cap, measured.get(key, 0))
        if keep_keys or new > default:
            out[key] = new
    return out


def format_table(measured: Mapping[str, int]) -> str:
    """The measured counts as the body of a budget table (keys sorted, zeros left out)."""
    return "\n".join(f"    {key!r}: {n}," for key, n in sorted(measured.items()) if n)
