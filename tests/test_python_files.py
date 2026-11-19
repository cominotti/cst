# SPDX-License-Identifier: MPL-2.0
"""Static gate: every Python file of the repository is under the Python gates.

ruff finds *.py files by itself but not the extensionless helpers (such as
btrfs/btrfs-bulk, run as `/usr/bin/python3 -I`): pyproject.toml names each
one in [tool.ruff] extend-include. A new helper missing from that list would
never be linted. This test derives the Python files from the repository
(*.py, plus any file whose first line is a python shebang) and checks:

- the extensionless ones are exactly ruff's extend-include list;
- every Python file is either in the ty gate ([tool.ty.src] include) or in
  NOT_YET_CLEAN below, never both. The ty gate lists folders and the
  extensionless helpers: a *.py file is in it when it is below a listed
  folder (ty finds those by itself) or named, an extensionless helper only
  when it is named (ty, like ruff, does not find those). Each entry is a
  plain path, no pattern, to a folder with Python files or to a Python file
  of the repository. A new file therefore joins the ty gate by itself (or,
  for a helper, by its entry), unless someone adds it to NOT_YET_CLEAN,
  which is an edit of this file that review can see;
- no tracked Python file matches a git ignore rule, and there is no .ignore
  file: ty leaves out the files those rules match (named in the ty gate or
  not), while git keeps tracking them, so such a file would leave the gate
  unseen;
- nothing else takes a file or a diagnostic out of ty's check: no ty.toml
  (ty would read it instead of pyproject.toml), no [tool.ty] setting other
  than src.include, terminal.error-on-warning (which must be true),
  environment.python-platform (which must be "linux") and rules raised to
  "error" (rules.<name> = "error"; "warn" or "ignore"
  fails), and no ignore comment in a ty-gated file;
- [tool.ruff.lint] typing-extensions is false, so no ruff fix or message
  reaches for the third-party typing_extensions module;
- [tool.ruff] required-version is "==" and the version of the ruff pin of
  the dev group, so a ruff other than the pinned one refuses to run on this
  repository (an editor's, or one on PATH);
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

from cst_core.domain.shapes import json_object
from tests.repo_files import REPO, ignored_tracked_files, repo_files

PYTHON_SHEBANG = re.compile(rb"^#!.*\bpython[0-9.]*\b")
IGNORE_COMMENT = re.compile(r"#\s*(?:ty|type)\s*:\s*ignore\b")
# The [tool.ty] settings the gate allows, as "table.key". Any other one could
# take files or diagnostics out of ty's check (src.exclude, overrides, a rule
# set to "ignore", analysis.allowed-unresolved-imports, ...).
TY_SETTINGS = frozenset({"environment.python-platform", "src.include", "terminal.error-on-warning"})
# The allowed settings that may hold one value only.
TY_VALUES: dict[str, object] = {"environment.python-platform": "linux"}

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
    extend = _strings(ruff.get("extend-include", []))
    ty_gate = _strings(tool.get("ty", {}).get("src", {}).get("include", []))
    return extend, ty_gate


def _strings(value: object) -> list[str]:
    """A TOML array of strings, as a list."""
    assert isinstance(value, list) and all(isinstance(v, str) for v in value), value
    return [v for v in value if isinstance(v, str)]


def _ignores_ann(rules: object) -> bool:
    return isinstance(rules, list) and any(r == "ALL" or str(r).startswith("ANN") for r in rules)


def _sub(table: object, key: str) -> dict[str, object]:
    """The sub-table `key` of a parsed TOML table ({} when there is none)."""
    value = table.get(key) if isinstance(table, dict) else None
    return value if json_object(value) else {}


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


def _is_folder_entry(entry: str, py_files: set[str]) -> bool:
    return any(p.startswith(entry + "/") for p in py_files)


def ty_gated(py_files: set[str], ty_gate: Sequence[str], ignored: frozenset[str] = frozenset()) -> set[str]:
    """The Python files ty checks: each one named, and each *.py file below a listed folder.

    A file in `ignored` (tracked, but a git ignore rule matches it) is left
    out: ty skips it, even when it is named.
    """
    folders = [e for e in ty_gate if _is_folder_entry(e, py_files)]
    return {
        p
        for p in py_files - ignored
        if p in ty_gate or (p.endswith(".py") and any(p.startswith(f + "/") for f in folders))
    }


PLAIN_ENTRY = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*")


def ty_entry_problems(py_files: set[str], ty_gate: Sequence[str]) -> list[str]:
    """Every [tool.ty.src] include entry that is not a plain path to a folder with Python files or to a Python file."""
    out: list[str] = []
    for entry in ty_gate:
        plain = PLAIN_ENTRY.fullmatch(entry) is not None and entry != "." and not entry.endswith("/")
        if not plain or ".." in entry.split("/") or "." in entry.split("/"):
            out.append(
                f"{entry}: in [tool.ty.src] include but not a plain path (no pattern, no '.', '..' or "
                "trailing '/'); name a folder or a Python file of the repository"
            )
        elif entry not in py_files and not _is_folder_entry(entry, py_files):
            out.append(f"{entry}: in [tool.ty.src] include but not a folder or a Python file of the repository")
    return out


def ruff_version_problems(conf: Mapping[str, object]) -> list[str]:
    """Whether [tool.ruff] required-version names exactly the ruff pin of the dev group, as text lines."""
    groups = _sub(dict(conf), "dependency-groups")
    dev = groups.get("dev") if isinstance(groups, dict) else None
    pins = [d for d in dev if isinstance(d, str) and d.startswith("ruff==")] if isinstance(dev, list) else []
    if len(pins) != 1:
        return ['[dependency-groups] dev: no exact "ruff==VERSION" pin']
    want = pins[0].removeprefix("ruff")
    got = _sub(_sub(dict(conf), "tool"), "ruff").get("required-version")
    if got != want:
        return [
            f"[tool.ruff] required-version = {got!r}: must be {want!r}, the ruff pin of the dev group "
            "(change both together)"
        ]
    return []


def typing_extensions_problems(conf: Mapping[str, object]) -> list[str]:
    """Whether [tool.ruff.lint] typing-extensions is false, as text lines (empty: it is)."""
    got = _sub(_sub(_sub(dict(conf), "tool"), "ruff"), "lint").get("typing-extensions")
    if got is not False:
        return [
            f"[tool.ruff.lint] typing-extensions = {got!r}: must be false (standard library only: "
            "no ruff fix may import typing_extensions)"
        ]
    return []


def problems(
    py_files: set[str],
    extend: Sequence[str],
    ty_gate: Sequence[str],
    not_clean: frozenset[str] = NOT_YET_CLEAN,
    ignored: frozenset[str] = frozenset(),
) -> list[str]:
    """What is wrong with the lists, as text lines (empty: all fine).

    ignored: the tracked files a git ignore rule matches (ignored_tracked_files()).
    """
    out: list[str] = []
    gated = ty_gated(py_files, ty_gate, ignored)
    extensionless = {p for p in py_files if not p.endswith(".py")}
    for p in sorted(extensionless - set(extend)):
        out.append(f"{p}: a Python file ruff does not see; add it to [tool.ruff] extend-include")
    for p in sorted(set(extend) - extensionless):
        out.append(f"{p}: in [tool.ruff] extend-include but not an extensionless Python file of the repository")
    for name, items in (("extend-include", extend), ("[tool.ty.src] include", ty_gate)):
        dups = sorted({p for p in items if items.count(p) > 1})
        out += [f"{p}: listed twice in {name}" for p in dups]
    out += ty_entry_problems(py_files, ty_gate)
    for p in sorted(not_clean - py_files):
        out.append(
            f"{p}: in NOT_YET_CLEAN (tests/test_python_files.py) but not a Python file of the "
            "repository; delete it there, or rename it there if the file was renamed"
        )
    for p in sorted(not_clean & gated):
        out.append(
            f"{p}: in both [tool.ty.src] include and NOT_YET_CLEAN; delete it from NOT_YET_CLEAN "
            "in tests/test_python_files.py"
        )
    for p in sorted(py_files & ignored):
        out.append(
            f"{p}: tracked, but a git ignore rule matches it, and ty leaves such a file out of its check "
            "(named in [tool.ty.src] include or not); change the rule so that it no longer matches the file"
        )
    for p in sorted(py_files - gated - not_clean - ignored):
        if not p.endswith(".py"):
            where = "its path (ty finds no extensionless file by itself)"
        else:
            where = "its folder" if "/" in p else "its path"
        out.append(
            f"{p}: a Python file outside the ty gate; make ty clean on it and add {where} to "
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
    for rel in sorted(f for f in files if os.path.basename(f) == ".ignore"):
        out.append(
            f"{rel}: ty leaves the files an .ignore file matches out of its check, and git does not see "
            "the rule; delete it"
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
    for name, want in sorted(TY_VALUES.items()):
        table, key = name.split(".", 1)
        section = ty.get(table, {})
        if isinstance(section, dict) and key in section and section[key] != want:
            out.append(
                f"[tool.ty] {name} = {section[key]!r}: only {want!r} is allowed (CST runs on Linux only; "
                "another platform changes what ty checks)"
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
            "x.py: in [tool.ty.src] include but not a folder or a Python file of the repository",
            check(ty_gate=["a.py", "tool", "x.py"]),
        )
        self.assertIn("tool: listed twice in extend-include", check(extend=["other", "tool", "tool"]))

    def test_every_python_file_is_ty_gated_or_not_yet_clean(self) -> None:
        def check(py: Iterable[str], ty_gate: Iterable[str], not_clean: Iterable[str]) -> list[str]:
            return problems(set(py), ["tool"], list(ty_gate), frozenset(not_clean))

        outside = (
            "{}: a Python file outside the ty gate; make ty clean on it and add its path to "
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

    def test_the_ty_gate_by_folder(self) -> None:
        py = {"pkg/a.py", "pkg/sub/b.py", "pkg/helper", "top.py", ".github/scripts/c.py", "other/d.py"}

        def check(ty_gate: Sequence[str], not_clean: Sequence[str] = ()) -> list[str]:
            return problems(py, ["pkg/helper"], list(ty_gate), frozenset(not_clean))

        gate = ["pkg", "pkg/helper", "top.py", ".github/scripts", "other"]
        self.assertEqual(check(gate), [])
        self.assertEqual(ty_gated(py, gate), py)
        # A folder covers the *.py files below it, at any depth, but not an extensionless helper.
        self.assertEqual(
            check(["pkg", "top.py", ".github/scripts", "other"]),
            [
                "pkg/helper: a Python file outside the ty gate; make ty clean on it and add its path "
                "(ty finds no extensionless file by itself) to [tool.ty.src] include (a new file never "
                "goes to NOT_YET_CLEAN)"
            ],
        )
        # A file below no listed folder is outside, a dot folder included.
        self.assertEqual(
            check(["pkg", "pkg/helper", "top.py", "other"]),
            [
                ".github/scripts/c.py: a Python file outside the ty gate; make ty clean on it and add its "
                "folder to [tool.ty.src] include (a new file never goes to NOT_YET_CLEAN)"
            ],
        )
        # "pkg/s" is no folder of pkg/sub/b.py: a prefix of a name covers nothing.
        self.assertEqual(ty_gated(py, ["pkg/s"]), set())
        # NOT_YET_CLEAN cannot hold a file a folder entry covers: ty checks it anyway.
        self.assertEqual(
            check(gate, ["pkg/sub/b.py"]),
            [
                "pkg/sub/b.py: in both [tool.ty.src] include and NOT_YET_CLEAN; delete it from "
                "NOT_YET_CLEAN in tests/test_python_files.py"
            ],
        )
        # Entries are plain paths to a folder with Python files or to a Python file.
        plain = (
            "{}: in [tool.ty.src] include but not a plain path (no pattern, no '.', '..' or trailing '/'); "
            "name a folder or a Python file of the repository"
        )
        for bad in (".", "**", "pkg/*.py", "!pkg", "pkg/", "/pkg", "pkg/../other", "./pkg", "{pkg,other}"):
            with self.subTest(entry=bad):
                self.assertEqual(ty_entry_problems(py, [bad]), [plain.format(bad)])
        self.assertEqual(
            ty_entry_problems(py, ["docs", "pkg/missing.py"]),
            [
                "docs: in [tool.ty.src] include but not a folder or a Python file of the repository",
                "pkg/missing.py: in [tool.ty.src] include but not a folder or a Python file of the repository",
            ],
        )

    def test_a_git_ignored_python_file_leaves_the_ty_gate(self) -> None:
        py = {"pkg/a.py", "pkg/b_local.py", "pkg/helper"}
        gate = ["pkg", "pkg/helper"]
        self.assertEqual(problems(py, ["pkg/helper"], gate, frozenset(), frozenset()), [])
        skipped = (
            "{}: tracked, but a git ignore rule matches it, and ty leaves such a file out of its check "
            "(named in [tool.ty.src] include or not); change the rule so that it no longer matches the file"
        )
        self.assertEqual(
            problems(py, ["pkg/helper"], gate, frozenset(), frozenset({"pkg/b_local.py", "pkg/helper", "notes.txt"})),
            [skipped.format("pkg/b_local.py"), skipped.format("pkg/helper")],
        )
        self.assertNotIn("pkg/b_local.py", ty_gated(py, gate, frozenset({"pkg/b_local.py"})))

    def test_ty_settings_that_bypass_the_gate(self) -> None:
        def conf(
            terminal: Mapping[str, object] | None = None,
            src: Mapping[str, object] | None = None,
            **more: object,
        ) -> dict[str, Any]:
            terminal = {"error-on-warning": True} if terminal is None else terminal
            return {"tool": {"ty": {"terminal": terminal, "src": src or {"include": ["a.py"]}, **more}}}

        self.assertEqual(ty_setting_problems(conf(), ["a.py", "pyproject.toml"]), [])
        # ty's folder walk honours .ignore files, which git knows nothing of.
        for where in (".ignore", "tests/.ignore"):
            with self.subTest(ignore_file=where):
                self.assertEqual(
                    ty_setting_problems(conf(), ["a.py", where]),
                    [
                        f"{where}: ty leaves the files an .ignore file matches out of its check, and git "
                        "does not see the rule; delete it"
                    ],
                )
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
        # The platform may only be Linux, the one CST runs on.
        self.assertEqual(ty_setting_problems(conf(environment={"python-platform": "linux"}), ["a.py"]), [])
        for platform in ("all", "darwin", "win32"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    ty_setting_problems(conf(environment={"python-platform": platform}), ["a.py"]),
                    [
                        f"[tool.ty] environment.python-platform = {platform!r}: only 'linux' is allowed "
                        "(CST runs on Linux only; another platform changes what ty checks)"
                    ],
                )
        # Another environment setting is still refused.
        self.assertEqual(
            len(ty_setting_problems(conf(environment={"python-platform": "linux", "root": ["x"]}), ["a.py"])), 1
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

    def test_ruff_refuses_to_run_unless_it_is_the_pinned_one(self) -> None:
        def conf(required: str | None, dev: Sequence[str] = ("ruff==1.2.3", "ty==0.0.1")) -> dict[str, object]:
            ruff: dict[str, object] = {} if required is None else {"required-version": required}
            return {"dependency-groups": {"dev": list(dev)}, "tool": {"ruff": ruff}}

        self.assertEqual(ruff_version_problems(conf("==1.2.3")), [])
        for bad in (None, ">=1.2.3", "==1.2.4", "1.2.3"):
            with self.subTest(required=bad):
                self.assertEqual(
                    ruff_version_problems(conf(bad)),
                    [
                        f"[tool.ruff] required-version = {bad!r}: must be '==1.2.3', the ruff pin of the "
                        "dev group (change both together)"
                    ],
                )
        self.assertEqual(
            ruff_version_problems(conf("==1.2.3", dev=["ruff>=1.2", "ty==0.0.1"])),
            ['[dependency-groups] dev: no exact "ruff==VERSION" pin'],
        )

    def test_ruff_never_reaches_for_typing_extensions(self) -> None:
        self.assertEqual(typing_extensions_problems({"tool": {"ruff": {"lint": {"typing-extensions": False}}}}), [])
        for conf, got in (
            ({"tool": {"ruff": {"lint": {"typing-extensions": True}}}}, True),
            ({"tool": {"ruff": {"lint": {}}}}, None),
            ({}, None),
        ):
            with self.subTest(got=got, conf=conf):
                self.assertEqual(
                    typing_extensions_problems(conf),
                    [
                        f"[tool.ruff.lint] typing-extensions = {got!r}: must be false (standard library "
                        "only: no ruff fix may import typing_extensions)"
                    ],
                )

    def test_every_python_file_is_under_the_gates(self) -> None:
        files = repo_files()
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
            conf = tomllib.load(fh)
        py_files = python_files(REPO, files)
        extend, ty_gate = load_lists(conf)
        found = (
            problems(py_files, extend, ty_gate, ignored=frozenset(ignored_tracked_files()))
            + ann_ignore_problems(conf)
            + ruff_version_problems(conf)
            + typing_extensions_problems(conf)
            + ty_setting_problems(conf, files)
            + ignore_comments(REPO, ty_gated(py_files, ty_gate))
        )
        self.assertEqual(found, [], "the Python gates miss something (see docs/dev-tools.md):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
