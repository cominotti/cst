"""Duplication ratchet: copied code in production may only go down.

CEILINGS holds today's measurements (tests/duplication.py says how each is
taken). A change that copies code over a ceiling fails here; a change that
removes copies lowers the ceiling to the new number in the same change, so
the copies cannot come back. The run prints the numbers measured.

Targets of the hexagonal conversion (the ceilings move towards them):
- python type 1, W=8, cross-file lines: 0 (no block of 8 lines in two files);
- python type 2, W=8: under 3.5 % (what main had before the conversion);
- shell, W=5: at most 1.1 %;
- same-name definitions: only the names in ALLOWED_SAME_NAMES, each with its reason.
"""

import sys
from typing import Final
import unittest

from tests.code_graph import production_python
from tests.duplication import (
    clones,
    is_shell,
    normalized_python,
    normalized_shell,
    python_clones,
    same_name_definitions,
    shell_clones,
)
from tests.repo_files import REPO, repo_files

# One table: metric -> ceiling. Lower a number when a change brings it down.
CEILINGS: Final[dict[str, float]] = {
    "python type-1 W=8 duplicated %": 3.97,
    "python type-1 W=8 cross-file lines": 742,
    "python type-2 W=8 duplicated %": 12.36,
    "shell W=5 duplicated %": 0.47,
    # Includes the canonical homes in cst_core (Usage, Refused, read_text,
    # read_int, read_bytes, log, take_lock, set_nocow, inode_flags,
    # render_timer, VirshPort, SubprocessVirshAdapter, first_line) next to
    # the copies the unconverted modules still hold; each stage removes its copies.
    "same-name definitions in >1 file": 74,
}

# Names that several files may define, and why.
ALLOWED_SAME_NAMES: Final[dict[str, str]] = {
    "main": "every program has its own main(): the entry point convention",
}


def _shell_files() -> list[str]:
    return [
        rel
        for rel in repo_files()
        if not rel.startswith((".idea/", "tests/")) and "/tests/" not in rel and is_shell(rel, REPO)
    ]


def measure() -> dict[str, float]:
    """The current value of every metric in CEILINGS."""
    prod = production_python()
    t1 = python_clones(REPO, prod, 8, type2=False)
    t2 = python_clones(REPO, prod, 8, type2=True)
    sh = shell_clones(REPO, _shell_files(), 5)
    names = same_name_definitions(REPO, prod, ALLOWED_SAME_NAMES)
    return {
        "python type-1 W=8 duplicated %": round(t1.percent, 2),
        "python type-1 W=8 cross-file lines": t1.cross_file,
        "python type-2 W=8 duplicated %": round(t2.percent, 2),
        "shell W=5 duplicated %": round(sh.percent, 2),
        "same-name definitions in >1 file": len(names),
    }


class DuplicationRatchetTest(unittest.TestCase):
    def test_no_metric_above_its_ceiling(self) -> None:
        measured = measure()
        print("\nduplication (measured / ceiling):", file=sys.stderr)
        for key, ceiling in CEILINGS.items():
            print(f"  {key:<38} {measured[key]:>8} / {ceiling}", file=sys.stderr)
        over = {k: (measured[k], c) for k, c in CEILINGS.items() if measured[k] > c}
        self.assertEqual(over, {}, "duplication went up (measured, ceiling); share the code instead of copying it")
        self.assertEqual(set(measured), set(CEILINGS))

    def test_type1_finds_a_copied_block_and_type2_a_renamed_copy(self) -> None:
        body = "".join(f"    x{i} = f(a, {i})\n" for i in range(8))
        a = f"def one(a):\n{body}"
        b = f"def two(a):\n{body}"
        renamed = "def three(b):\n" + "".join(f"    y{i} = g(b, {i + 1})\n" for i in range(8))
        norm1 = {p: normalized_python(src, type2=False) for p, src in (("a", a), ("b", b), ("c", renamed))}
        rep = clones(norm1, 8)
        self.assertEqual((rep.cross_file, rep.per_file.get("c", 0)), (16, 0))
        norm2 = {p: normalized_python(src, type2=True) for p, src in (("a", a), ("c", renamed))}
        self.assertEqual(clones(norm2, 8).cross_file, 18)

    def test_comments_docstrings_and_trivial_lines_do_not_count(self) -> None:
        src = 'def f():\n    """doc"""\n    # comment\n    return (\n        1\n    )\n'
        self.assertEqual(normalized_python(src, type2=False), ["def f ( ) :", "return (", "1"])
        self.assertEqual(normalized_shell("# c\nif x; then\n  a  b\nfi\n"), ["if x; then", "a b"])

    def test_same_name_definitions_skip_allowed_names(self) -> None:
        names = same_name_definitions(REPO, production_python(), ALLOWED_SAME_NAMES)
        self.assertNotIn("main", names)


if __name__ == "__main__":
    unittest.main()
