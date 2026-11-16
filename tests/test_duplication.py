"""Duplication ratchet: copied code in production may only go down.

CEILINGS holds today's measurements (tests/duplication.py says how each is
taken). A change that copies code over a ceiling fails here; a change that
removes copies lowers the ceiling to the new number in the same change, so
the copies cannot come back. The run prints the numbers measured.

Targets of the hexagonal conversion (the ceilings move towards them):
- python type 1, W=8, cross-file lines: 0 (no block of 8 lines in two files);
- python type 2, W=8: at most what main had before the conversion: 2.52 %
  with this method (3.50 % when type 2 also renamed the names in imports);
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
    # 0.37: the same 32 duplicated lines as before an import statement became
    # one line; only the total of code lines went down.
    "python type-1 W=8 duplicated %": 0.37,
    "python type-1 W=8 cross-file lines": 32,
    "python type-2 W=8 duplicated %": 6.2,
    "shell W=5 duplicated %": 0.47,
    # Includes the canonical homes in cst_core (Usage, Refused, read_text,
    # read_int, read_bytes, log, take_lock, set_nocow, inode_flags,
    # render_timer, VirshPort, SubprocessVirshAdapter, first_line) next to
    # the copies the unconverted modules still hold; each stage removes its copies.
    "same-name definitions in >1 file": 34,
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

    def test_a_wrapped_import_list_is_one_line(self) -> None:
        names = "".join(f"    name{i},\n" for i in range(12))
        a = f"from pkg.one import (\n{names})\nimport os\n"
        b = f"from pkg.two import (\n{names.replace('name', 'other')})\n"
        self.assertEqual(
            normalized_python(a, type2=False)[0],
            "from pkg . one import ( " + " ".join(f"name{i} ," for i in range(12)) + " )",
        )
        self.assertEqual(len(normalized_python(a, type2=True)), 2)
        self.assertEqual(normalized_python(a, type2=True)[1], "import os")
        # Type 2 keeps an import's names: eight different imports are no renamed copy of eight others.
        imports = ["os", "re", "sys", "json", "time", "stat", "errno", "shutil", "subprocess"]
        blocks = {p: "".join(f"import {m}\n" for m in mods) for p, mods in (("x", imports[:8]), ("y", imports[1:]))}
        self.assertEqual(clones({p: normalized_python(src, type2=True) for p, src in blocks.items()}, 8).duplicated, 0)
        self.assertEqual(
            clones({p: normalized_python(src, type2=True) for p, src in (("a", a), ("b", b))}, 8).duplicated, 0
        )
        # Only a statement that starts with `from`/`import`: `raise ... from` and a copied body still count.
        self.assertEqual(
            normalized_python("raise E(\n    x\n) from None\n", type2=False), ["raise E (", "x", ") from None"]
        )
        body = "".join(f"    x{i} = f(a, {i})\n" for i in range(8))
        copied = {
            p: normalized_python(f"{imports}def {p}(a):\n{body}", type2=True) for p, imports in (("one", a), ("two", b))
        }
        self.assertEqual(clones(copied, 8).cross_file, 18)

    def test_comments_docstrings_and_trivial_lines_do_not_count(self) -> None:
        src = 'def f():\n    """doc"""\n    # comment\n    return (\n        1\n    )\n'
        self.assertEqual(normalized_python(src, type2=False), ["def f ( ) :", "return (", "1"])
        self.assertEqual(normalized_shell("# c\nif x; then\n  a  b\nfi\n"), ["if x; then", "a b"])

    def test_same_name_definitions_skip_allowed_names(self) -> None:
        names = same_name_definitions(REPO, production_python(), ALLOWED_SAME_NAMES)
        self.assertNotIn("main", names)


if __name__ == "__main__":
    unittest.main()
