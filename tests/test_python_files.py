"""Static gate: every Python file of the repository is under the Python gates.

ruff finds *.py files by itself but not the extensionless helpers (such as
btrfs/btrfs-bulk, run as `/usr/bin/python3 -I`): pyproject.toml names each
one in [tool.ruff] extend-include. A new helper missing from that list would
never be linted. This test derives the Python files from the repository
(*.py, plus any file whose first line is a python shebang) and checks:

- the extensionless ones are exactly ruff's extend-include list;
- the ty gate list ([tool.ty.src] include) names only Python files;
- every Python file is either in the ty gate or in [tool.cst.ty]
  not-yet-clean, never both. That list only shrinks: its length is capped by
  NOT_YET_CLEAN_MAX, so a new file must be ty-clean and join the gate;
- the typed list (the "!{...}" ANN entry of [tool.ruff.lint.per-file-ignores],
  whose functions must all be annotated) names only files of the ty gate, and
  no other entry ignores ANN (that would switch it off for the typed files).

Standard library only: the unit tests run on /usr/bin/python3 without uv.
See docs/dev-tools.md.
"""

import os
import re
import tempfile
import tomllib
import unittest

from tests.repo_files import REPO, repo_files

PYTHON_SHEBANG = re.compile(rb"^#!.*\bpython[0-9.]*\b")
# Length of [tool.cst.ty] not-yet-clean when it was created. Lower it when a
# file leaves the list; never raise it (a new file joins the ty gate instead).
NOT_YET_CLEAN_MAX = 29
TYPED_KEY = re.compile(r"^!\{([^{}]+)\}$")


def is_python(path):
    if path.endswith(".py"):
        return True
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return bool(PYTHON_SHEBANG.match(first))


def python_files(repo, files):
    return {rel for rel in files if is_python(os.path.join(repo, rel))}


def load_lists(pyproject):
    """(ruff extend-include, ty gate list, typed list, not-yet-clean list) from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        conf = tomllib.load(fh)
    tool = conf.get("tool", {})
    ruff = tool.get("ruff", {})
    extend = list(ruff.get("extend-include", []))
    ty_gate = list(tool.get("ty", {}).get("src", {}).get("include", []))
    not_clean = list(tool.get("cst", {}).get("ty", {}).get("not-yet-clean", []))
    lint = ruff.get("lint", {})
    # Any entry that ignores an ANN rule counts. A second one would silence
    # ANN for the typed files too (two "!{...}" entries each ignore ANN for
    # the other's files) while ruff still reports "All checks passed".
    ann = [(key, rules) for table in ("per-file-ignores", "extend-per-file-ignores")
           for key, rules in lint.get(table, {}).items()
           if any(r == "ALL" or r.startswith("ANN") for r in rules)]
    if len(ann) > 1:
        raise AssertionError("only one per-file-ignores entry may ignore ANN (the typed list), not "
                             + ", ".join(repr(k) for k, _ in ann))
    typed = []
    for key, rules in ann:
        m = TYPED_KEY.match(key)
        if rules != ["ANN"] or not m:
            raise AssertionError(f"the ANN entry of per-file-ignores must read \"!{{file,...}}\" = [\"ANN\"], "
                                 f"not {key!r} = {rules!r}")
        typed += [p.strip() for p in m.group(1).split(",")]
    return extend, ty_gate, typed, not_clean


def problems(py_files, extend, ty_gate, typed, not_clean, not_clean_max=NOT_YET_CLEAN_MAX):
    """What is wrong with the four lists, as text lines (empty: all fine)."""
    out = []
    extensionless = {p for p in py_files if not p.endswith(".py")}
    for p in sorted(extensionless - set(extend)):
        out.append(f"{p}: a Python file ruff does not see; add it to [tool.ruff] extend-include")
    for p in sorted(set(extend) - extensionless):
        out.append(f"{p}: in [tool.ruff] extend-include but not an extensionless Python file of the repository")
    for name, items in (("extend-include", extend), ("[tool.ty.src] include", ty_gate), ("typed list", typed),
                        ("[tool.cst.ty] not-yet-clean", not_clean)):
        dups = sorted({p for p in items if items.count(p) > 1})
        out += [f"{p}: listed twice in {name}" for p in dups]
    for p in sorted(set(ty_gate) - py_files):
        out.append(f"{p}: in [tool.ty.src] include but not a Python file of the repository")
    for p in sorted(set(not_clean) - py_files):
        out.append(f"{p}: in [tool.cst.ty] not-yet-clean but not a Python file of the repository; remove it")
    for p in sorted(set(not_clean) & set(ty_gate)):
        out.append(f"{p}: in both [tool.ty.src] include and [tool.cst.ty] not-yet-clean; "
                   "remove it from not-yet-clean")
    for p in sorted(py_files - set(ty_gate) - set(not_clean)):
        out.append(f"{p}: a Python file outside the ty gate; make ty clean on it and add it to "
                   "[tool.ty.src] include (new files never go to not-yet-clean)")
    if len(set(not_clean)) > not_clean_max:
        out.append(f"[tool.cst.ty] not-yet-clean has {len(set(not_clean))} files, more than {not_clean_max}: "
                   "the list only shrinks")
    for p in sorted(set(typed) - set(ty_gate)):
        out.append(f"{p}: in the typed list but not in the ty gate ([tool.ty.src] include)")
    if not typed:
        out.append("no typed list: per-file-ignores has no \"!{...}\" = [\"ANN\"] entry")
    return out


class PythonFilesTest(unittest.TestCase):
    def test_detection(self):
        with tempfile.TemporaryDirectory(prefix="cst-pyfiles-") as tmp:
            for name, first in (("a.py", b""), ("helper", b"#!/usr/bin/python3 -I\n"),
                                ("envpy", b"#!/usr/bin/env python3\n"), ("script.sh", b"#!/bin/bash\n"),
                                ("data", b"python3 is mentioned here\n"), ("bin", b"\x00\xff")):
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(first + b"pass\n")
            got = python_files(tmp, sorted(os.listdir(tmp)))
        self.assertEqual(got, {"a.py", "helper", "envpy"})

    def test_problems_catch_each_mistake(self):
        py = {"a.py", "tool", "other"}

        def check(extend=("other", "tool"), ty_gate=("a.py", "tool"), typed=("tool",), not_clean=("other",),
                  not_clean_max=1):
            return problems(py, list(extend), list(ty_gate), list(typed), list(not_clean), not_clean_max)

        self.assertEqual(check(), [])
        self.assertEqual(check(extend=["tool"]),
                         ["other: a Python file ruff does not see; add it to [tool.ruff] extend-include"])
        self.assertIn("gone: in [tool.ruff] extend-include but not an extensionless Python file of the repository",
                      check(extend=["other", "tool", "gone"]))
        self.assertIn("a.py: in [tool.ruff] extend-include but not an extensionless Python file of the repository",
                      check(extend=["other", "tool", "a.py"]))
        self.assertIn("x.py: in [tool.ty.src] include but not a Python file of the repository",
                      check(ty_gate=["a.py", "tool", "x.py"]))
        self.assertIn("other: in the typed list but not in the ty gate ([tool.ty.src] include)",
                      check(typed=["other"]))
        self.assertIn("tool: listed twice in extend-include", check(extend=["other", "tool", "tool"]))
        self.assertIn("no typed list: per-file-ignores has no \"!{...}\" = [\"ANN\"] entry", check(typed=[]))

    def test_every_python_file_is_ty_gated_or_listed_as_not_yet_clean(self):
        py = {"a.py", "tool", "other"}

        def check(ty_gate=("a.py", "tool"), not_clean=("other",), not_clean_max=1):
            return problems(py, ["other", "tool"], list(ty_gate), ["tool"], list(not_clean), not_clean_max)

        # A new file in neither list fails, and so does parking it in not-yet-clean beyond the cap.
        self.assertEqual(check(not_clean=[]),
                         ["other: a Python file outside the ty gate; make ty clean on it and add it to "
                          "[tool.ty.src] include (new files never go to not-yet-clean)"])
        self.assertEqual(check(ty_gate=["tool"], not_clean=["other", "a.py"]),
                         ["[tool.cst.ty] not-yet-clean has 2 files, more than 1: the list only shrinks"])
        # Shrinking is always fine: the file moved to the ty gate, and the cap still holds.
        self.assertEqual(check(ty_gate=["a.py", "tool", "other"], not_clean=[]), [])
        self.assertIn("other: in both [tool.ty.src] include and [tool.cst.ty] not-yet-clean; "
                      "remove it from not-yet-clean",
                      check(ty_gate=["a.py", "tool", "other"]))
        self.assertIn("gone.py: in [tool.cst.ty] not-yet-clean but not a Python file of the repository; remove it",
                      check(not_clean=["other", "gone.py"], not_clean_max=2))
        self.assertIn("other: listed twice in [tool.cst.ty] not-yet-clean", check(not_clean=["other", "other"]))

    def test_typed_list_parsing(self):
        with tempfile.TemporaryDirectory(prefix="cst-pyfiles-") as tmp:
            path = os.path.join(tmp, "pyproject.toml")
            with open(path, "w") as fh:
                fh.write('[tool.ruff]\nextend-include = ["a/b"]\n[tool.ruff.lint.per-file-ignores]\n'
                         '"tests/*" = ["B"]\n"!{a/b, c.py}" = ["ANN"]\n[tool.ty.src]\ninclude = ["a/b", "c.py"]\n'
                         '[tool.cst.ty]\nnot-yet-clean = ["d.py"]\n')
            self.assertEqual(load_lists(path), (["a/b"], ["a/b", "c.py"], ["a/b", "c.py"], ["d.py"]))
            # Each of these would switch ANN off for a typed file without ruff saying so.
            for bad in ('"!a/b" = ["ANN"]\n',                                   # not the "!{...}" form
                        '"!{a/b}" = ["ANN"]\n"!{c.py}" = ["ANN"]\n',            # two typed entries
                        '"!{a/b}" = ["ANN"]\n"a/*" = ["B", "ANN001"]\n',        # another entry ignoring ANN
                        '"!{a/b}" = ["ANN", "B"]\n'):                           # more than ANN
                with self.subTest(bad=bad):
                    with open(path, "w") as fh:
                        fh.write("[tool.ruff.lint.per-file-ignores]\n" + bad)
                    with self.assertRaises(AssertionError):
                        load_lists(path)
            with open(path, "w") as fh:
                fh.write('[tool.ruff.lint.per-file-ignores]\n"!{a/b}" = ["ANN"]\n'
                         '[tool.ruff.lint.extend-per-file-ignores]\n"c.py" = ["ALL"]\n')
            with self.assertRaises(AssertionError):
                load_lists(path)

    def test_every_python_file_is_under_the_gates(self):
        files = repo_files()
        found = problems(python_files(REPO, files), *load_lists(os.path.join(REPO, "pyproject.toml")))
        self.assertEqual(found, [], "Python files and the lists in pyproject.toml disagree "
                         "(see docs/dev-tools.md):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
