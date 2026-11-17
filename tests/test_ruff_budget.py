# SPDX-License-Identifier: MPL-2.0
"""tests/ruff_budget.py counts ruff's hits per package (no ruff needed here)."""

import unittest

from tests.ruff_budget import RULES, RUFF_BUDGETS, rule_counts, unused_noqa


def rec(code: str, path: str, row: int = 1) -> dict[str, object]:
    return {"code": code, "filename": f"/repo/{path}", "location": {"row": row}, "message": "m"}


class RuffBudgetTest(unittest.TestCase):
    def test_counts_per_package_and_tests_apart(self) -> None:
        got = rule_counts(
            [
                rec("FBT001", "vm/cli.py"),
                rec("FBT001", "vm/adapters.py"),
                rec("FBT001", "vm/tests/test_vm.py"),
                rec("C901", "btrfs/bulk/steam.py"),
                rec("SLF001", "doctor/application.py"),
                rec("SLF001", "doctor/tests/test_doctor.py"),  # production only
                rec("F401", "vm/cli.py"),  # not a budgeted rule
            ],
            repo="/repo",
        )
        self.assertEqual(
            got,
            {"vm FBT001": 2, "vm tests FBT001": 1, "btrfs/bulk C901": 1, "doctor SLF001": 1},
        )

    def test_unused_noqa_lines(self) -> None:
        self.assertEqual(
            unused_noqa([rec("RUF100", "a/b.py", 7), rec("BLE001", "a/b.py")], repo="/repo"),
            ["a/b.py:7: m"],
        )

    def test_the_table_names_budgeted_rules(self) -> None:
        for key in RUFF_BUDGETS:
            with self.subTest(key=key):
                self.assertIn(key.rsplit(" ", 1)[1], RULES)


if __name__ == "__main__":
    unittest.main()
