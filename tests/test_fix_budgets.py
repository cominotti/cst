# SPDX-License-Identifier: MPL-2.0
"""Tests of the budget rule (tests/budgets.py) and of `invowk cmd fix budgets` (tests/fix_budgets.py).

A gate fails only when a measured value is above its entry; the fix lowers
the tables to the measured values, never raises one and never adds one. The
fix is tested on temporary files with fake measurements: it never runs a
real measurement here (RUFF_BUDGETS would need ruff).
"""

from collections.abc import Mapping
import contextlib
import io
import os
import tempfile
import unittest

from tests.budgets import budget_problems, lowered
from tests.fix_budgets import TABLES, Measured, Table, main, plan_table, read_table, rewrite, table_node
from tests.repo_files import REPO

SOURCE = '''"""A gate."""
from typing import Final

# Hard I/O per package.
IO: Final[dict[str, int]] = {
    "a cli": 3,
    "b cli": 2,  # the b program
    "c cli": 1,
}
CEIL: Final[dict[str, float]] = {
    "type-2 %": 0.9,
    "cross-file lines": 4,
}
ALLOWED: Final[dict[str, str]] = {
    "keep": "still needed",
    # Grouped under a comment.
    "gone": "a reason written "
    "over two lines",
}
'''


def _fixed(measured: Measured) -> Table:
    return Table("gate.py", "IO", "budget", lambda: measured)


class BudgetRuleTest(unittest.TestCase):
    def test_only_a_count_above_its_entry_fails(self) -> None:
        budget = {"a": 3, "b": 2}
        self.assertEqual(budget_problems({"a": 3, "b": 2}, budget, "hits"), [])
        self.assertEqual(budget_problems({"a": 1, "b": 0}, budget, "hits"), [], "below its entry passes")
        self.assertEqual(budget_problems({}, budget, "hits"), [], "an entry nothing measures passes")
        self.assertEqual(budget_problems({"a": 4}, budget, "hits"), ["a: 4 hits, budget 3: the change added 1"])
        # A key missing from the table is 0, or the table's default.
        self.assertEqual(budget_problems({"new": 1}, budget, "hits"), ["new: 1 hits, budget 0: the change added 1"])
        self.assertEqual(budget_problems({"new": 0}, budget, "hits"), [])
        self.assertEqual(budget_problems({"big.py": 800, "x.py": 10}, {}, "lines", default=800), [])
        self.assertEqual(
            budget_problems({"big.py": 801}, {}, "lines", default=800),
            ["big.py: 801 lines, budget 800: the change added 1"],
        )

    def test_lowered_never_raises_and_never_adds(self) -> None:
        budget = {"a": 3, "b": 2, "c": 1}
        self.assertEqual(lowered({"a": 2, "b": 5, "new": 9}, budget), {"a": 2, "b": 2})
        self.assertEqual(lowered({"a": 3, "b": 2, "c": 1}, budget), budget)
        # A ceiling of a fixed metric keeps its key, also at 0.
        self.assertEqual(lowered({"a": 0, "b": 1.5}, {"a": 0, "b": 2.0}, keep_keys=True), {"a": 0, "b": 1.5})
        # An entry at the default or below is dropped (a missing key means the default).
        self.assertEqual(lowered({"x.py": 900, "y.py": 700}, {"x.py": 950, "y.py": 820}, default=800), {"x.py": 900})


class RewriteTest(unittest.TestCase):
    def test_lowers_a_number_and_keeps_the_rest_of_the_line(self) -> None:
        out = rewrite(SOURCE, "IO", {"a cli": 3, "b cli": 1, "c cli": 1})
        self.assertIn('    "b cli": 1,  # the b program\n', out)
        self.assertEqual(out.replace('"b cli": 1,', '"b cli": 2,'), SOURCE)
        lower = {"type-2 %": 0.61, "cross-file lines": 0}
        self.assertEqual(read_table(rewrite(SOURCE, "CEIL", lower), "CEIL"), lower)

    def test_deletes_entries_but_keeps_the_comments_around_them(self) -> None:
        out = rewrite(SOURCE, "ALLOWED", {"keep": "still needed"})
        self.assertIn("    # Grouped under a comment.\n}", out)
        self.assertNotIn("over two lines", out)
        out = rewrite(SOURCE, "IO", {"a cli": 3})
        self.assertNotIn("b program", out)
        self.assertEqual(read_table(out, "IO"), {"a cli": 3})

    def test_an_empty_table_becomes_a_pair_of_braces(self) -> None:
        out = rewrite(SOURCE, "IO", {})
        self.assertIn("IO: Final[dict[str, int]] = {}\nCEIL", out)
        self.assertEqual(read_table(out, "CEIL"), {"type-2 %": 0.9, "cross-file lines": 4})

    def test_refuses_to_raise_or_add(self) -> None:
        with self.assertRaisesRegex(ValueError, "only lowers"):
            rewrite(SOURCE, "IO", {"a cli": 4, "b cli": 2, "c cli": 1})
        with self.assertRaisesRegex(ValueError, "never adds"):
            rewrite(SOURCE, "IO", {"a cli": 3, "b cli": 2, "c cli": 1, "d cli": 1})
        with self.assertRaisesRegex(ValueError, "one entry per line"):
            rewrite('X = {"a": 1, "b": 2}\n', "X", {"a": 1})
        with self.assertRaisesRegex(ValueError, "no module-level dict"):
            table_node(SOURCE, "MISSING")


class PlanTest(unittest.TestCase):
    def test_budget_table(self) -> None:
        plan = plan_table(_fixed({}), {"a cli": 3, "b cli": 2}, {"a cli": 1, "b cli": 4, "new cli": 2})
        self.assertEqual(plan.new, {"a cli": 1, "b cli": 2})
        self.assertEqual(plan.changes, ["'a cli': 3 -> 1"])
        self.assertEqual(
            plan.above,
            [
                "'b cli': measured 4, above its entry 2 (left as is; the gate fails)",
                "'new cli': measured 2, above its entry 0 (left as is; the gate fails)",
            ],
        )
        plan = plan_table(_fixed({}), {"a cli": 3}, {})
        self.assertEqual((plan.new, plan.changes), ({}, ["'a cli': 3 -> deleted (measured 0)"]))

    def test_names_table(self) -> None:
        table = Table("gate.py", "ALLOWED", "names", lambda: set())
        plan = plan_table(table, {"keep": "r1", "gone": "r2"}, {"keep", "new"})
        self.assertEqual(plan.new, {"keep": "r1"})
        self.assertEqual(plan.changes, ["'gone': deleted (no longer needed)"])
        self.assertEqual(plan.above, ["'new': needed but not in the table (the gate fails)"])


class MainTest(unittest.TestCase):
    def _run(self, repo: str, argv: list[str], measured: Mapping[str, Measured]) -> tuple[int, str, str]:
        tables = [
            Table("gate.py", "IO", "budget", lambda: measured["IO"]),
            Table("gate.py", "CEIL", "ceiling", lambda: measured["CEIL"]),
            Table("gate.py", "ALLOWED", "names", lambda: measured["ALLOWED"]),
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv, repo=repo, tables=tables)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing_and_apply_lowers_once(self) -> None:
        measured: dict[str, Measured] = {
            "IO": {"a cli": 3, "b cli": 1},
            "CEIL": {"type-2 %": 0.61, "cross-file lines": 4},
            "ALLOWED": {"keep"},
        }
        with tempfile.TemporaryDirectory(prefix="cst-fix-budgets-") as repo:
            path = os.path.join(repo, "gate.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(SOURCE)
            rc, out, _ = self._run(repo, [], measured)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), SOURCE, "the dry run wrote the file")
            self.assertEqual(rc, 0)
            self.assertIn("would lower: gate.py IO 'b cli': 2 -> 1\n", out)
            self.assertIn("would lower: gate.py IO 'c cli': 1 -> deleted (measured 0)\n", out)
            self.assertIn("would lower: gate.py CEIL 'type-2 %': 0.9 -> 0.61\n", out)
            self.assertIn("would lower: gate.py ALLOWED 'gone': deleted (no longer needed)\n", out)
            self.assertIn("fix budgets: 4 entries can go down; --apply writes them", out)

            rc, out, _ = self._run(repo, ["--apply"], measured)
            self.assertEqual(rc, 0)
            self.assertIn("lowered: gate.py IO 'b cli': 2 -> 1\n", out)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertEqual(read_table(text, "IO"), {"a cli": 3, "b cli": 1})
            self.assertEqual(read_table(text, "CEIL"), {"type-2 %": 0.61, "cross-file lines": 4})
            self.assertEqual(read_table(text, "ALLOWED"), {"keep": "still needed"})

            rc, out, _ = self._run(repo, ["--apply"], measured)
            self.assertEqual((rc, out), (0, "fix budgets: every table is at its measured values\n"))
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), text, "a second run changed the file")

    def test_a_value_above_its_entry_is_left_and_reported(self) -> None:
        measured: dict[str, Measured] = {
            "IO": {"a cli": 5, "b cli": 2, "c cli": 1},
            "CEIL": {"type-2 %": 0.95, "cross-file lines": 4},
            "ALLOWED": {"keep", "gone"},
        }
        with tempfile.TemporaryDirectory(prefix="cst-fix-budgets-") as repo:
            path = os.path.join(repo, "gate.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(SOURCE)
            rc, out, err = self._run(repo, ["--apply"], measured)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), SOURCE, "a value above its entry was written")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "fix budgets: every table is at its measured values\n")
        self.assertIn("above:   gate.py IO 'a cli': measured 5, above its entry 3", err)
        self.assertIn("above:   gate.py CEIL 'type-2 %': measured 0.95, above its entry 0.9", err)

    def test_usage(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--force"], tables=[]), 2)
        self.assertIn("usage", err.getvalue())


class RegistryTest(unittest.TestCase):
    def test_every_table_exists_in_its_file(self) -> None:
        for table in TABLES:
            with self.subTest(table=table.name):
                with open(os.path.join(REPO, table.path), encoding="utf-8") as fh:
                    entries = read_table(fh.read(), table.name)
                if table.kind != "names":
                    self.assertTrue(all(isinstance(v, int | float) for v in entries.values()))


if __name__ == "__main__":
    unittest.main()
