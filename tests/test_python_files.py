# SPDX-License-Identifier: MPL-2.0
"""Static gate: every Python file of the repository is under the Python gates.

ruff finds *.py files by itself but not the extensionless helpers (such as
btrfs/btrfs-bulk, run as `/usr/bin/python3 -I`): pyproject.toml names each
one in [tool.ruff] extend-include. A new helper missing from that list would
never be linted. This test derives the Python files from the repository
(*.py, plus any file whose first line is a python shebang) and checks:

- the extensionless ones are exactly ruff's extend-include list;
- every Python file is either in the ty gate ([tool.ty.src] include) or in
  NOT_YET_CLEAN below, never both, and both name only Python files. A new
  file therefore has to join the ty gate, unless someone adds it to
  NOT_YET_CLEAN, which is an edit of this file that review can see;
- nothing else takes a file or a diagnostic out of ty's check: no ty.toml
  (ty would read it instead of pyproject.toml), no [tool.ty] setting other
  than src.include, terminal.error-on-warning (which must be true) and
  rules raised to "error" (rules.<name> = "error"; "warn" or "ignore"
  fails), and no ignore comment in a ty-gated file;
- ruff's annotation rules (ANN, which `invowk cmd test types` runs on every
  Python file) are switched off for no file: no entry of
  [tool.ruff.lint.per-file-ignores] or extend-per-file-ignores, and no
  [tool.ruff.lint] ignore or extend-ignore, names an ANN rule or ALL; nor
  does the older top-level [tool.ruff] form of these settings, which ruff
  still reads.

Standard library only: the unit tests run on /usr/bin/python3 without uv.
See docs/dev-tools.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import os
import re
import tempfile
import tokenize
import tomllib
from typing import Any
import unittest

from tests.repo_files import REPO, repo_files

PYTHON_SHEBANG = re.compile(rb"^#!.*\bpython[0-9.]*\b")
IGNORE_COMMENT = re.compile(r"#\s*(?:ty|type)\s*:\s*ignore\b")
# The [tool.ty] settings the gate allows, as "table.key". Any other one could
# take files or diagnostics out of ty's check (src.exclude, overrides, a rule
# set to "ignore", analysis.allowed-unresolved-imports, ...).
TY_SETTINGS = frozenset({"src.include", "terminal.error-on-warning"})

# The Python files ty is not yet clean on. Every other Python file is in the ty
# gate ([tool.ty.src] include). This set only shrinks: when ty becomes clean on
# a file, move the file to the ty gate and delete it here (the test says to).
# A renamed file keeps its entry under the new name. A new file never goes
# here: make ty clean on it and add it to the ty gate.
NOT_YET_CLEAN: frozenset[str] = frozenset()


def is_python(path: str) -> bool:
    if path.endswith(".py"):
        return True
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return bool(PYTHON_SHEBANG.match(first))


def python_files(repo: str, files: Iterable[str]) -> set[str]:
    return {rel for rel in files if is_python(os.path.join(repo, rel))}


def load_lists(conf: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(ruff extend-include, ty gate list) from the parsed pyproject.toml."""
    tool = conf.get("tool", {})
    ruff = tool.get("ruff", {})
    extend = list(ruff.get("extend-include", []))
    ty_gate = list(tool.get("ty", {}).get("src", {}).get("include", []))
    return extend, ty_gate


def _ignores_ann(rules: object) -> bool:
    return isinstance(rules, list) and any(r == "ALL" or str(r).startswith("ANN") for r in rules)


def _sub(table: object, key: str) -> dict[str, object]:
    """The sub-table `key` of a parsed TOML table ({} when there is none)."""
    value = table.get(key) if isinstance(table, dict) else None
    return value if isinstance(value, dict) else {}


def ann_ignore_problems(conf: Mapping[str, object]) -> list[str]:
    """Every ruff setting that switches an ANN rule off, as text lines (empty: ANN applies to every file)."""
    ruff = _sub(_sub(dict(conf), "tool"), "ruff")
    out: list[str] = []
    # ruff still reads the older top-level [tool.ruff] form of these settings.
    for section, settings in (("tool.ruff", ruff), ("tool.ruff.lint", _sub(ruff, "lint"))):
        for table in ("per-file-ignores", "extend-per-file-ignores"):
            for key, rules in sorted(_sub(settings, table).items()):
                if _ignores_ann(rules):
                    out.append(
                        f"[{section}.{table}] {key!r} = {rules!r}: ignores ANN; annotate the files instead "
                        "(every Python file is fully annotated)"
                    )
        for key in ("ignore", "extend-ignore"):
            if _ignores_ann(settings.get(key)):
                out.append(f"[{section}] {key} = {settings[key]!r}: ignores ANN for every file; annotate instead")
    return out


def problems(
    py_files: set[str],
    extend: Sequence[str],
    ty_gate: Sequence[str],
    not_clean: frozenset[str] = NOT_YET_CLEAN,
) -> list[str]:
    """What is wrong with the lists, as text lines (empty: all fine)."""
    out: list[str] = []
    extensionless = {p for p in py_files if not p.endswith(".py")}
    for p in sorted(extensionless - set(extend)):
        out.append(f"{p}: a Python file ruff does not see; add it to [tool.ruff] extend-include")
    for p in sorted(set(extend) - extensionless):
        out.append(f"{p}: in [tool.ruff] extend-include but not an extensionless Python file of the repository")
    for name, items in (("extend-include", extend), ("[tool.ty.src] include", ty_gate)):
        dups = sorted({p for p in items if items.count(p) > 1})
        out += [f"{p}: listed twice in {name}" for p in dups]
    for p in sorted(set(ty_gate) - py_files):
        out.append(f"{p}: in [tool.ty.src] include but not a Python file of the repository")
    for p in sorted(not_clean - py_files):
        out.append(
            f"{p}: in NOT_YET_CLEAN (tests/test_python_files.py) but not a Python file of the "
            "repository; delete it there, or rename it there if the file was renamed"
        )
    for p in sorted(not_clean & set(ty_gate)):
        out.append(
            f"{p}: in both [tool.ty.src] include and NOT_YET_CLEAN; delete it from NOT_YET_CLEAN "
            "in tests/test_python_files.py"
        )
    for p in sorted(py_files - set(ty_gate) - not_clean):
        out.append(
            f"{p}: a Python file outside the ty gate; make ty clean on it and add it to "
            "[tool.ty.src] include (a new file never goes to NOT_YET_CLEAN)"
        )
    return out


def ty_setting_problems(conf: Mapping[str, Any], files: Iterable[str]) -> list[str]:
    """What could take files or diagnostics out of ty's check, as text lines (empty: all fine)."""
    out: list[str] = []
    if "ty.toml" in files:
        out.append(
            "ty.toml: ty reads it instead of [tool.ty] in pyproject.toml, so the gate there "
            "no longer applies; delete it"
        )
    ty = conf.get("tool", {}).get("ty", {})
    names: set[str] = set()
    for table, value in ty.items():
        names |= {f"{table}.{key}" for key in value} if isinstance(value, dict) else {table}
    rules = ty.get("rules", {})
    for name, level in sorted(rules.items()) if isinstance(rules, dict) else ():
        names.discard(f"rules.{name}")
        if level != "error":
            out.append(
                f'[tool.ty] rules.{name} = {level!r}: a rule may only be raised to "error" here; '
                "a lower level takes its diagnostics out of the gate"
            )
    for name in sorted(names - TY_SETTINGS):
        out.append(
            f"[tool.ty] {name}: a ty setting the gate does not allow, since it could take files or "
            "diagnostics out of ty's check; allow it in TY_SETTINGS of tests/test_python_files.py "
            "only once it is known not to"
        )
    if ty.get("terminal", {}).get("error-on-warning") is not True:
        out.append("[tool.ty.terminal] error-on-warning must be true, or a ty warning no longer fails the gate")
    return out


def ignore_comments(repo: str, paths: Iterable[str]) -> list[str]:
    """The ty/type ignore comments in the given files, as text lines (empty: none)."""
    out: list[str] = []
    for rel in sorted(paths):
        with open(os.path.join(repo, rel), "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT and IGNORE_COMMENT.search(tok.string):
                    out.append(
                        f"{rel}:{tok.start[0]}: an ignore comment in a ty-gated file; fix what ty reports instead"
                    )
    return out


class PythonFilesTest(unittest.TestCase):
    def test_detection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-pyfiles-") as tmp:
            for name, first in (
                ("a.py", b""),
                ("helper", b"#!/usr/bin/python3 -I\n"),
                ("envpy", b"#!/usr/bin/env python3\n"),
                ("script.sh", b"#!/bin/bash\n"),
                ("data", b"python3 is mentioned here\n"),
                ("bin", b"\x00\xff"),
            ):
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(first + b"pass\n")
            got = python_files(tmp, sorted(os.listdir(tmp)))
        self.assertEqual(got, {"a.py", "helper", "envpy"})

    def test_problems_catch_each_mistake(self) -> None:
        py = {"a.py", "tool", "other"}

        def check(
            extend: Sequence[str] = ("other", "tool"),
            ty_gate: Sequence[str] = ("a.py", "tool"),
            not_clean: Sequence[str] = ("other",),
        ) -> list[str]:
            return problems(py, list(extend), list(ty_gate), frozenset(not_clean))

        self.assertEqual(check(), [])
        self.assertEqual(
            check(extend=["tool"]), ["other: a Python file ruff does not see; add it to [tool.ruff] extend-include"]
        )
        self.assertIn(
            "gone: in [tool.ruff] extend-include but not an extensionless Python file of the repository",
            check(extend=["other", "tool", "gone"]),
        )
        self.assertIn(
            "a.py: in [tool.ruff] extend-include but not an extensionless Python file of the repository",
            check(extend=["other", "tool", "a.py"]),
        )
        self.assertIn(
            "x.py: in [tool.ty.src] include but not a Python file of the repository",
            check(ty_gate=["a.py", "tool", "x.py"]),
        )
        self.assertIn("tool: listed twice in extend-include", check(extend=["other", "tool", "tool"]))

    def test_every_python_file_is_ty_gated_or_not_yet_clean(self) -> None:
        def check(py: Iterable[str], ty_gate: Iterable[str], not_clean: Iterable[str]) -> list[str]:
            return problems(set(py), ["tool"], list(ty_gate), frozenset(not_clean))

        outside = (
            "{}: a Python file outside the ty gate; make ty clean on it and add it to "
            "[tool.ty.src] include (a new file never goes to NOT_YET_CLEAN)"
        )
        self.assertEqual(check(["a.py", "tool", "old.py"], ["a.py", "tool"], ["old.py"]), [])
        # A new file in neither list fails.
        self.assertEqual(
            check(["a.py", "tool", "old.py", "new.py"], ["a.py", "tool"], ["old.py"]), [outside.format("new.py")]
        )
        # A file that leaves the not-yet-clean set frees no slot for a new one: old.py moved to the
        # gate and new.py came in, but new.py still has to be in the gate (or visibly added here).
        self.assertEqual(
            check(["a.py", "tool", "old.py", "new.py"], ["a.py", "tool", "old.py"], ["old.py"]),
            [
                "old.py: in both [tool.ty.src] include and NOT_YET_CLEAN; delete it from "
                "NOT_YET_CLEAN in tests/test_python_files.py",
                outside.format("new.py"),
            ],
        )
        # Same when the not-yet-clean file was deleted instead.
        self.assertEqual(
            check(["a.py", "tool", "new.py"], ["a.py", "tool"], ["old.py"]),
            [
                "old.py: in NOT_YET_CLEAN (tests/test_python_files.py) but not a Python file of the "
                "repository; delete it there, or rename it there if the file was renamed",
                outside.format("new.py"),
            ],
        )

    def test_ty_settings_that_bypass_the_gate(self) -> None:
        def conf(
            terminal: Mapping[str, object] | None = None,
            src: Mapping[str, object] | None = None,
            **more: object,
        ) -> dict[str, Any]:
            terminal = {"error-on-warning": True} if terminal is None else terminal
            return {"tool": {"ty": {"terminal": terminal, "src": src or {"include": ["a.py"]}, **more}}}

        self.assertEqual(ty_setting_problems(conf(), ["a.py", "pyproject.toml"]), [])
        self.assertEqual(
            ty_setting_problems(conf(), ["a.py", "ty.toml"]),
            [
                "ty.toml: ty reads it instead of [tool.ty] in pyproject.toml, so the gate there "
                "no longer applies; delete it"
            ],
        )
        for name, bad in (
            ("src.exclude", conf(src={"include": ["a.py"], "exclude": ["a.py"]})),
            ("overrides", conf(overrides=[{"include": ["a.py"], "rules": {"all": "ignore"}}])),
            ("analysis.allowed-unresolved-imports", conf(analysis={"allowed-unresolved-imports": ["x"]})),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    ty_setting_problems(bad, ["a.py"]),
                    [
                        f"[tool.ty] {name}: a ty setting the gate does not allow, since it could take "
                        "files or diagnostics out of ty's check; allow it in TY_SETTINGS of "
                        "tests/test_python_files.py only once it is known not to"
                    ],
                )
        # A rule may be raised to "error", never lowered.
        self.assertEqual(ty_setting_problems(conf(rules={"division-by-zero": "error"}), ["a.py"]), [])
        for level in ("warn", "ignore", "info"):
            with self.subTest(level=level):
                self.assertEqual(
                    ty_setting_problems(
                        conf(rules={"division-by-zero": "error", "invalid-assignment": level}), ["a.py"]
                    ),
                    [
                        f"[tool.ty] rules.invalid-assignment = {level!r}: a rule may only be raised to "
                        '"error" here; a lower level takes its diagnostics out of the gate'
                    ],
                )
        for terminal in ({"error-on-warning": False}, {}):
            with self.subTest(terminal=terminal):
                self.assertEqual(
                    ty_setting_problems(conf(terminal=terminal), ["a.py"]),
                    ["[tool.ty.terminal] error-on-warning must be true, or a ty warning no longer fails the gate"],
                )

    def test_ignore_comments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-pyfiles-") as tmp:
            # Built from pieces so that this file holds no ignore comment of its own.
            ty_ignore, type_ignore = "# ty" + ": ignore[invalid-assignment]", "#type" + ":ignore"
            files = {
                "clean": f'#!/usr/bin/python3 -I\ns = "{ty_ignore}"\n',
                "a.py": f"x: int = 'a'  {ty_ignore}\n",
                "b.py": f"import os\ny = 1  # noqa {type_ignore}\n",
            }
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w") as fh:
                    fh.write(text)
            self.assertEqual(
                ignore_comments(tmp, files),
                [
                    "a.py:1: an ignore comment in a ty-gated file; fix what ty reports instead",
                    "b.py:2: an ignore comment in a ty-gated file; fix what ty reports instead",
                ],
            )

    def test_no_setting_switches_ann_off(self) -> None:
        conf = tomllib.loads(
            '[tool.ruff]\nextend-include = ["a/b"]\n[tool.ruff.lint]\nignore = ["E501"]\n'
            '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["B"]\n[tool.ty.src]\ninclude = ["a/b", "c.py"]\n'
        )
        self.assertEqual(load_lists(conf), (["a/b"], ["a/b", "c.py"]))
        self.assertEqual(ann_ignore_problems(conf), [])
        # Each of these would switch ANN off for some files without ruff saying so.
        for bad in (
            '[tool.ruff.lint.per-file-ignores]\n"!{a/b}" = ["ANN"]\n',  # the old typed-list form
            '[tool.ruff.lint.per-file-ignores]\n"a/*" = ["B", "ANN001"]\n',
            '[tool.ruff.lint.extend-per-file-ignores]\n"c.py" = ["ALL"]\n',
            '[tool.ruff.lint]\nignore = ["ANN401"]\n',
            '[tool.ruff.lint]\nextend-ignore = ["ANN"]\n',
            '[tool.ruff]\nper-file-ignores = {"a.py" = ["ANN"]}\n',  # the older top-level form
            '[tool.ruff]\nignore = ["ANN001"]\n',
        ):
            with self.subTest(bad=bad):
                self.assertEqual(len(ann_ignore_problems(tomllib.loads(bad))), 1)

    def test_every_python_file_is_under_the_gates(self) -> None:
        files = repo_files()
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
            conf = tomllib.load(fh)
        py_files = python_files(REPO, files)
        extend, ty_gate = load_lists(conf)
        found = (
            problems(py_files, extend, ty_gate)
            + ann_ignore_problems(conf)
            + ty_setting_problems(conf, files)
            + ignore_comments(REPO, set(ty_gate) & py_files)
        )
        self.assertEqual(found, [], "the Python gates miss something (see docs/dev-tools.md):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
