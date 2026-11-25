# SPDX-License-Identifier: MPL-2.0
"""Static gate: no package module is over MODULE_LINE_CAP lines.

A module's size is its physical lines (what `wc -l` counts), over every
production module of the packages (tests/test_architecture.py,
`package_modules`: tests aside). The layer gates say where code goes, not
how big one use case or one command file may grow; this one caps that.

OVER_CAP names today's larger modules with their size when the cap came in.
Like every table of the gates (tests/budgets.py) it fails only above: a
module over its entry, or over the cap with no entry, fails; a module that
shrinks passes, and `invowk cmd fix budgets` lowers its entry, deleting it
once the module is at the cap or under it. Nothing is added here: split a
module that grows past the cap by command group or check area, not by layer
(docs/architecture.md, "Package profile").

Why 800: of the 131 package modules (2026-11-17, after #43 split btrfs/bulk)
half are under 140 lines and nine in ten under 720; the eight over 800 are
the ones the architecture review named as too big to read (toolchains/cli.py
2164, doctor/application.py 1791, vm/cli.py 1471, ...), and the next five
(753-776) keep some room.

Standard library only.
"""

import os
from typing import Final
import unittest

from tests.budgets import budget_problems, lowered
from tests.repo_files import REPO
from tests.test_architecture import package_modules

MODULE_LINE_CAP: Final = 800

# Modules over the cap, with their lines (tests/budgets.py: never above; lowered by `invowk cmd fix budgets`).
OVER_CAP: Final[dict[str, int]] = {
    "btrfs/bulk/adapters/user.py": 809,
    "btrfs/snapshot/application.py": 1115,
}


def line_count(source: str) -> int:
    """Physical lines of a source: its newlines, and a last line without one.

    Not str.splitlines, which also breaks at a form feed, U+2028 and other
    characters that end no line in a source file.
    """
    return source.count("\n") + (1 if source and not source.endswith("\n") else 0)


def module_sizes() -> dict[str, int]:
    """Lines of every package module: what OVER_CAP and MODULE_LINE_CAP cap."""
    sizes: dict[str, int] = {}
    for rel in package_modules():
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            sizes[rel] = line_count(fh.read())
    return sizes


class ModuleSizeTest(unittest.TestCase):
    def test_no_module_is_over_the_cap(self) -> None:
        problems = budget_problems(module_sizes(), OVER_CAP, "lines", default=MODULE_LINE_CAP)
        self.assertEqual(
            problems,
            [],
            f"a module over its OVER_CAP entry, or over {MODULE_LINE_CAP} lines: split it by command group or "
            'check area, not by layer (docs/architecture.md, "Package profile")',
        )

    def test_the_rule(self) -> None:
        cap = {"big.py": 900}
        self.assertEqual(budget_problems({"big.py": 900, "a.py": 800}, cap, "lines", default=800), [])
        self.assertEqual(budget_problems({"big.py": 850}, cap, "lines", default=800), [], "shrinking passes")
        self.assertEqual(
            budget_problems({"big.py": 901, "new.py": 801}, cap, "lines", default=800),
            ["big.py: 901 lines, budget 900: the change added 1", "new.py: 801 lines, budget 800: the change added 1"],
        )
        # The fix lowers an entry, and deletes it at the cap or under it (or when the module is gone).
        self.assertEqual(lowered({"big.py": 850}, cap, default=800), {"big.py": 850})
        self.assertEqual(lowered({"big.py": 800}, cap, default=800), {})
        self.assertEqual(lowered({}, cap, default=800), {})

    def test_line_count(self) -> None:
        self.assertEqual(line_count("a\nb\n"), 2)
        self.assertEqual(line_count("a\nb"), 2)
        self.assertEqual(line_count(""), 0)
        self.assertEqual(line_count("\n\n"), 2)
        self.assertEqual(line_count("a\x0cb\u2028c\x1cd\n"), 1, "a form feed or U+2028 ends no line")


if __name__ == "__main__":
    unittest.main()
