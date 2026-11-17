#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Fix budgets: lower every budget, ceiling and allow-list to what the code measures.

Use: python3 -I -B tests/fix_budgets.py [--apply]   (from anywhere;
`invowk cmd fix budgets`; RUFF_BUDGETS is measured with ruff, through uv).

The static gates fail only when the code is above a table: a count over its
budget or ceiling, a module or name missing from its allow-list
(tests/budgets.py). A change that removes something passes without touching
the tables, so parallel changes do not conflict on them. This script brings
the tables down afterwards. It measures each table with the same function
its gate calls (TABLES), and:

- lowers each number to the measured value; a budget entry that falls to 0
  (or to its table's default) is deleted, while a ceiling of a fixed metric
  keeps its key;
- deletes each allow-list entry the code no longer needs (its reason goes
  with it);
- never raises a number and never adds an entry: a value above its entry is
  printed and left for the change that caused it (exit 1).

Without --apply it only prints what it would change (writes nothing). It
edits only the lines of the entries it changes, so the comments and the
reasons around them stay. A second run changes nothing.

Standard library only.
"""

import ast
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
import os
import re
import sys
from typing import Final, Literal

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests import (
    ruff_budget,
    test_architecture as arch,
    test_duplication as dup,
    test_module_size as size,
    test_typing_budget as typing_budget,
)
from tests.budgets import lowered

type Kind = Literal["budget", "ceiling", "names"]
type Measured = Mapping[str, float] | Set[str]

# What may follow the value of an entry on its line: its comma and a comment.
_ENTRY_END: Final = re.compile(rb"\s*,?\s*(#.*)?$")


@dataclass(frozen=True, slots=True)
class Table:
    """One table of a gate and how to measure it.

    kind: "budget" (counts; a missing key is `default`, an entry at
    `default` or below is deleted), "ceiling" (fixed metrics; every key
    stays) or "names" (an allow-list; `measure` gives the names the code
    needs, the other entries are deleted).
    """

    path: str
    name: str
    kind: Kind
    measure: Callable[[], Measured]
    default: float = 0


@dataclass(frozen=True, slots=True)
class Plan:
    """What the fix would do to one table: the new entries and the lines to print."""

    table: Table
    new: dict[str, object]
    changes: list[str]
    above: list[str]


ARCH: Final = "tests/test_architecture.py"
DUP: Final = "tests/test_duplication.py"
TYPING: Final = "tests/test_typing_budget.py"
TABLES: Final[tuple[Table, ...]] = (
    Table(ARCH, "CLI_CEILINGS", "budget", arch.cli_sizes),
    Table(ARCH, "IO_BUDGETS", "budget", arch.measure_io),
    Table(ARCH, "PRIMITIVE_BUDGETS", "budget", lambda: arch.measure_primitives()[0]),
    Table(ARCH, "LITERAL_BUDGETS", "budget", lambda: arch.measure_primitives()[1]),
    Table(ARCH, "UNLAYERED_ALLOWED", "names", arch.unlayered_modules),
    Table(ARCH, "CORE_NAMES_WITHOUT_USER", "names", arch.core_names_missing_a_user),
    Table(TYPING, "ANY_BUDGETS", "budget", lambda: typing_budget.measure()[0]),
    Table(TYPING, "CAST_BUDGETS", "budget", lambda: typing_budget.measure()[1]),
    Table(DUP, "CEILINGS", "ceiling", dup.measure),
    Table(DUP, "ALLOWED_SAME_NAMES", "names", dup.same_names_defined),
    Table(DUP, "ALLOWED_SHELL_SAME_NAMES", "names", dup.shell_same_names_defined),
    Table("tests/ruff_budget.py", "RUFF_BUDGETS", "budget", ruff_budget.measure),
    Table("tests/test_module_size.py", "OVER_CAP", "budget", size.module_sizes, default=size.MODULE_LINE_CAP),
)


def table_node(source: str, name: str) -> ast.Dict:
    """The dict literal assigned to the module-level `name` in `source`."""
    for node in ast.parse(source).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == name and isinstance(value, ast.Dict):
            return value
    raise ValueError(f"{name}: no module-level dict literal of that name")


def read_table(source: str, name: str) -> dict[str, object]:
    """The entries of the table `name` as written in `source`."""
    value = ast.literal_eval(table_node(source, name))
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"{name}: not a dict with string keys")
    return {str(k): v for k, v in value.items()}


def _numbers(name: str, entries: Mapping[str, object]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in entries.items():
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValueError(f"{name}: {key!r} is not a number")
        out[key] = value
    return out


def plan_table(table: Table, current: Mapping[str, object], measured: Measured) -> Plan:
    """The lowered table and the lines that say what changes (and what is above its entry)."""
    changes: list[str] = []
    above: list[str] = []
    if table.kind == "names":
        need = set(measured)
        new = {k: v for k, v in current.items() if k in need}
        changes = [f"{k!r}: deleted (no longer needed)" for k in current if k not in need]
        above = [f"{k!r}: needed but not in the table (the gate fails)" for k in sorted(need - set(current))]
        return Plan(table, new, changes, above)
    if not isinstance(measured, Mapping):
        raise ValueError(f"{table.name}: a {table.kind} table needs measured numbers")
    numbers = _numbers(table.name, current)
    low = lowered(measured, numbers, default=table.default, keep_keys=table.kind == "ceiling")
    for key, cap in numbers.items():
        if key not in low:
            changes.append(f"{key!r}: {cap} -> deleted (measured {measured.get(key, 0)})")
        elif low[key] != cap:
            changes.append(f"{key!r}: {cap} -> {low[key]}")
    for key, got in sorted(measured.items()):
        cap = numbers.get(key, table.default)
        if got > cap:
            above.append(f"{key!r}: measured {got}, above its entry {cap} (left as is; the gate fails)")
    return Plan(table, dict(low), changes, above)


def _entry_span(lines: Sequence[bytes], key: ast.expr, value: ast.expr) -> tuple[int, int]:
    """0-based (first, last) line of one entry, which must sit on lines of its own."""
    first, last = key.lineno - 1, (value.end_lineno or value.lineno) - 1
    if lines[first][: key.col_offset].strip() or not _ENTRY_END.match(lines[last][value.end_col_offset or 0 :]):
        raise ValueError(f"line {key.lineno}: an entry to delete shares its lines with another; one entry per line")
    return first, last


def rewrite(source: str, name: str, new: Mapping[str, object]) -> str:
    """`source` with the table `name` holding `new`: values lowered and entries deleted, nothing raised or added."""
    node = table_node(source, name)
    old = read_table(source, name)
    if set(new) - set(old):
        raise ValueError(f"{name}: the fix never adds an entry: {sorted(set(new) - set(old))}")
    lines = source.encode("utf-8").splitlines(keepends=True)
    if not new:
        first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        head, tail = lines[first][: node.col_offset], lines[last][node.end_col_offset or 0 :]
        lines[first : last + 1] = [head + b"{}" + tail]
        return b"".join(lines).decode("utf-8")
    edits: list[tuple[int, int, list[bytes]]] = []  # (first, last) lines -> replacement lines
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            raise ValueError(f"{name}: line {value_node.lineno}: a key that is not a string literal")
        key = key_node.value
        if key not in new:
            first, last = _entry_span(lines, key_node, value_node)
            edits.append((first, last, []))
            continue
        to, was = new[key], old[key]
        if to == was:
            continue
        if not isinstance(to, int | float) or not isinstance(was, int | float) or to > was:
            raise ValueError(f"{name}: {key!r}: the fix only lowers numbers ({was!r} -> {to!r})")
        row = value_node.lineno - 1
        if value_node.end_lineno != value_node.lineno:
            raise ValueError(f"{name}: {key!r}: a number written over several lines")
        line = lines[row]
        text = repr(new[key]).encode()
        edits.append((row, row, [line[: value_node.col_offset] + text + line[value_node.end_col_offset or 0 :]]))
    for first, last, repl in sorted(edits, reverse=True):
        lines[first : last + 1] = repl
    out = b"".join(lines).decode("utf-8")
    if read_table(out, name) != dict(new):
        raise ValueError(f"{name}: the rewritten table does not read back as planned")
    return out


def main(argv: Sequence[str] = (), repo: str = REPO, tables: Sequence[Table] = TABLES) -> int:
    """Exit 0: done or nothing to do; 1: a value is above its entry; 2: bad arguments."""
    args = list(argv)
    if args not in ([], ["--apply"]):
        print("usage: fix_budgets.py [--apply]", file=sys.stderr)
        return 2
    apply = bool(args)
    sources: dict[str, str] = {}
    plans: list[Plan] = []
    for table in tables:
        if table.path not in sources:
            with open(os.path.join(repo, table.path), encoding="utf-8") as fh:
                sources[table.path] = fh.read()
        plan = plan_table(table, read_table(sources[table.path], table.name), table.measure())
        if plan.changes:
            sources[table.path] = rewrite(sources[table.path], table.name, plan.new)
        plans.append(plan)
    changed = sorted({p.table.path for p in plans if p.changes})
    if apply:
        for path in changed:
            with open(os.path.join(repo, path), "w", encoding="utf-8", newline="") as fh:
                fh.write(sources[path])
    for plan in plans:
        where = f"{plan.table.path} {plan.table.name}"
        for line in plan.changes:
            print(f"{'lowered: ' if apply else 'would lower: '}{where} {line}")
        for line in plan.above:
            print(f"above:   {where} {line}", file=sys.stderr)
    count = sum(len(p.changes) for p in plans)
    if not count:
        print("fix budgets: every table is at its measured values")
    elif not apply:
        print(f"fix budgets: {count} entr{'y' if count == 1 else 'ies'} can go down; --apply writes them")
    return 1 if any(p.above for p in plans) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
