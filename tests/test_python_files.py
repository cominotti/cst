"""Static gate: every Python file of the repository is under the Python gates.

ruff finds *.py files by itself but not the extensionless helpers (such as
btrfs/btrfs-bulk, run as `/usr/bin/python3 -I`): pyproject.toml names each
one in [tool.ruff] extend-include. A new helper missing from that list would
never be linted. This test derives the Python files from the repository
(*.py, plus any file whose first line is a python shebang) and checks:

- the extensionless ones are exactly ruff's extend-include list;
- the ty gate list ([tool.ty.src] include) names only Python files;
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
    """(ruff extend-include, ty gate list, typed list) from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        conf = tomllib.load(fh)
    tool = conf.get("tool", {})
    ruff = tool.get("ruff", {})
    extend = list(ruff.get("extend-include", []))
    ty_gate = list(tool.get("ty", {}).get("src", {}).get("include", []))
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
    return extend, ty_gate, typed


def problems(py_files, extend, ty_gate, typed):
    """What is wrong with the three lists, as text lines (empty: all fine)."""
    out = []
    extensionless = {p for p in py_files if not p.endswith(".py")}
    for p in sorted(extensionless - set(extend)):
        out.append(f"{p}: a Python file ruff does not see; add it to [tool.ruff] extend-include")
    for p in sorted(set(extend) - extensionless):
        out.append(f"{p}: in [tool.ruff] extend-include but not an extensionless Python file of the repository")
    for name, items in (("extend-include", extend), ("[tool.ty.src] include", ty_gate), ("typed list", typed)):
        dups = sorted({p for p in items if items.count(p) > 1})
        out += [f"{p}: listed twice in {name}" for p in dups]
    for p in sorted(set(ty_gate) - py_files):
        out.append(f"{p}: in [tool.ty.src] include but not a Python file of the repository")
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
        self.assertEqual(problems(py, ["other", "tool"], ["a.py", "tool"], ["tool"]), [])
        self.assertEqual(problems(py, ["tool"], ["tool"], ["tool"]),
                         ["other: a Python file ruff does not see; add it to [tool.ruff] extend-include"])
        self.assertIn("gone: in [tool.ruff] extend-include but not an extensionless Python file of the repository",
                      problems(py, ["other", "tool", "gone"], ["tool"], ["tool"]))
        self.assertIn("a.py: in [tool.ruff] extend-include but not an extensionless Python file of the repository",
                      problems(py, ["other", "tool", "a.py"], ["tool"], ["tool"]))
        self.assertIn("x.py: in [tool.ty.src] include but not a Python file of the repository",
                      problems(py, ["other", "tool"], ["x.py"], []))
        self.assertIn("other: in the typed list but not in the ty gate ([tool.ty.src] include)",
                      problems(py, ["other", "tool"], ["tool"], ["other"]))
        self.assertIn("tool: listed twice in extend-include", problems(py, ["other", "tool", "tool"], ["tool"], ["tool"]))
        self.assertIn("no typed list: per-file-ignores has no \"!{...}\" = [\"ANN\"] entry",
                      problems(py, ["other", "tool"], ["tool"], []))

    def test_typed_list_parsing(self):
        with tempfile.TemporaryDirectory(prefix="cst-pyfiles-") as tmp:
            path = os.path.join(tmp, "pyproject.toml")
            with open(path, "w") as fh:
                fh.write('[tool.ruff]\nextend-include = ["a/b"]\n[tool.ruff.lint.per-file-ignores]\n'
                         '"tests/*" = ["B"]\n"!{a/b, c.py}" = ["ANN"]\n[tool.ty.src]\ninclude = ["a/b", "c.py"]\n')
            self.assertEqual(load_lists(path), (["a/b"], ["a/b", "c.py"], ["a/b", "c.py"]))
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
        extend, ty_gate, typed = load_lists(os.path.join(REPO, "pyproject.toml"))
        found = problems(python_files(REPO, files), extend, ty_gate, typed)
        self.assertEqual(found, [], "Python files and the lists in pyproject.toml disagree "
                         "(see docs/dev-tools.md):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
