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
  still reads;
- ruff's preview is on only for the rules select names by their full code
  ([tool.ruff.lint] explicit-preview-rules is true whenever preview is),
  never for PLC1901, and never for the formatter ([tool.ruff.format]
  preview, or the top-level [tool.ruff] preview that would switch it on);
- no Python file keeps ruff format away from its code with a comment
  (# fmt: skip, # fmt: off/on, or the yapf: disable/enable ruff also reads).

Standard library only: the unit tests run on /usr/bin/python3 without uv.
See docs/dev-tools.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
import os
import re
import tempfile
import tokenize
import tomllib
from typing import Any
import unittest

from tests.repo_files import REPO, ignored_tracked_files, repo_files

PYTHON_SHEBANG = re.compile(rb"^#!.*\bpython[0-9.]*\b")
IGNORE_COMMENT = re.compile(r"#\s*(?:ty|type)\s*:\s*ignore\b")
# A comment that keeps ruff format away from code: fmt with skip, off or on, and yapf with disable
# or enable, which ruff also reads (the pattern below spells them out).
FORMAT_SUPPRESSION = re.compile(r"#\s*(?:fmt\s*:\s*(?:skip|off|on)|yapf\s*:\s*(?:disable|enable))\b")
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
        out.extend(
            f"[{section}] {key} = {settings[key]!r}: ignores ANN for every file; annotate instead"
            for key in ("ignore", "extend-ignore")
            if _ignores_ann(settings.get(key))
        )
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
            (
                f"[tool.ruff] required-version = {got!r}: must be {want!r}, the ruff pin of the dev group "
                "(change both together)"
            )
        ]
    return []


def typing_extensions_problems(conf: Mapping[str, object]) -> list[str]:
    """Whether [tool.ruff.lint] typing-extensions is false, as text lines (empty: it is)."""
    got = _sub(_sub(_sub(dict(conf), "tool"), "ruff"), "lint").get("typing-extensions")
    if got is not False:
        return [
            (
                f"[tool.ruff.lint] typing-extensions = {got!r}: must be false (standard library only: "
                "no ruff fix may import typing_extensions)"
            )
        ]
    return []


def ruff_preview_problems(conf: Mapping[str, object]) -> list[str]:
    """Whether ruff's preview reaches only the rules named one by one, as text lines (empty: it does).

    With [tool.ruff.lint] preview on, a family in select (PLC, RUF, ...) would
    turn on every preview rule of that family unless explicit-preview-rules is
    on too. PLC1901 asks for `not x` instead of `x == ""`, the opposite of the
    house style. The formatter's preview style can change the layout in any
    release: it stays off, and so does the top-level [tool.ruff] preview, which
    would switch it on.
    """
    ruff = _sub(_sub(dict(conf), "tool"), "ruff")
    lint = _sub(ruff, "lint")
    out: list[str] = []
    if ruff.get("preview") is True:
        out.append(
            "[tool.ruff] preview = true: it also turns on the formatter's preview style; "
            "set preview in [tool.ruff.lint] only"
        )
    if _sub(ruff, "format").get("preview") is True:
        out.append("[tool.ruff.format] preview = true: the preview layout can change in any ruff release; leave it off")
    explicit = lint.get("explicit-preview-rules", ruff.get("explicit-preview-rules"))
    if lint.get("preview") is True and explicit is not True:
        out.append(
            "[tool.ruff.lint] preview = true needs explicit-preview-rules = true: otherwise a family in "
            "select turns on every preview rule of that family; name each preview rule instead"
        )
    for section, settings in (("tool.ruff", ruff), ("tool.ruff.lint", lint)):
        for key in ("select", "extend-select"):
            rules = settings.get(key)
            if isinstance(rules, list) and "PLC1901" in rules:
                out.append(
                    f'[{section}] {key} names PLC1901: it asks for `not x` instead of `x == ""`, '
                    "the opposite of the house style; never select it"
                )
    return out


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
    gated = ty_gated(py_files, ty_gate, ignored)
    extensionless = {p for p in py_files if not p.endswith(".py")}
    out = [
        f"{p}: a Python file ruff does not see; add it to [tool.ruff] extend-include"
        for p in sorted(extensionless - set(extend))
    ]
    out.extend(
        f"{p}: in [tool.ruff] extend-include but not an extensionless Python file of the repository"
        for p in sorted(set(extend) - extensionless)
    )
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
    out.extend(
        f"{rel}: ty leaves the files an .ignore file matches out of its check, and git does not see the rule; delete it"
        for rel in sorted(f for f in files if os.path.basename(f) == ".ignore")
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
    out.extend(
        f"[tool.ty] {name}: a ty setting the gate does not allow, since it could take files or "
        "diagnostics out of ty's check; allow it in TY_SETTINGS of tests/test_python_files.py "
        "only once it is known not to"
        for name in sorted(names - TY_SETTINGS)
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


def _comments_matching(repo: str, paths: Iterable[str], pattern: re.Pattern[str]) -> Iterator[tuple[str, int, str]]:
    """The comments PATTERN finds in the given files, as (path, line number, comment), in path order."""
    for rel in sorted(paths):
        with open(os.path.join(repo, rel), "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT and pattern.search(tok.string):
                    yield rel, tok.start[0], tok.string


def ignore_comments(repo: str, paths: Iterable[str]) -> list[str]:
    """The ty/type ignore comments in the given files, as text lines (empty: none)."""
    return [
        f"{rel}:{line}: an ignore comment in a ty-gated file; fix what ty reports instead"
        for rel, line, _ in _comments_matching(repo, paths, IGNORE_COMMENT)
    ]


def format_suppressions(repo: str, paths: Iterable[str]) -> list[str]:
    """The comments that keep ruff format away from code in the given files, as text lines (empty: none)."""
    return [
        f"{rel}:{line}: {comment.strip()!r} keeps ruff format away; let it lay the code out"
        for rel, line, comment in _comments_matching(repo, paths, FORMAT_SUPPRESSION)
    ]


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
                (
                    "old.py: in both [tool.ty.src] include and NOT_YET_CLEAN; delete it from "
                    "NOT_YET_CLEAN in tests/test_python_files.py"
                ),
                outside.format("new.py"),
            ],
        )
        # Same when the not-yet-clean file was deleted instead.
        self.assertEqual(
            check(["a.py", "tool", "new.py"], ["a.py", "tool"], ["old.py"]),
            [
                (
                    "old.py: in NOT_YET_CLEAN (tests/test_python_files.py) but not a Python file of the "
                    "repository; delete it there, or rename it there if the file was renamed"
                ),
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
                (
                    "pkg/helper: a Python file outside the ty gate; make ty clean on it and add its path "
                    "(ty finds no extensionless file by itself) to [tool.ty.src] include (a new file never "
                    "goes to NOT_YET_CLEAN)"
                )
            ],
        )
        # A file below no listed folder is outside, a dot folder included.
        self.assertEqual(
            check(["pkg", "pkg/helper", "top.py", "other"]),
            [
                (
                    ".github/scripts/c.py: a Python file outside the ty gate; make ty clean on it and add its "
                    "folder to [tool.ty.src] include (a new file never goes to NOT_YET_CLEAN)"
                )
            ],
        )
        # "pkg/s" is no folder of pkg/sub/b.py: a prefix of a name covers nothing.
        self.assertEqual(ty_gated(py, ["pkg/s"]), set())
        # NOT_YET_CLEAN cannot hold a file a folder entry covers: ty checks it anyway.
        self.assertEqual(
            check(gate, ["pkg/sub/b.py"]),
            [
                (
                    "pkg/sub/b.py: in both [tool.ty.src] include and NOT_YET_CLEAN; delete it from "
                    "NOT_YET_CLEAN in tests/test_python_files.py"
                )
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
                        (
                            f"{where}: ty leaves the files an .ignore file matches out of its check, and git "
                            "does not see the rule; delete it"
                        )
                    ],
                )
        self.assertEqual(
            ty_setting_problems(conf(), ["a.py", "ty.toml"]),
            [
                (
                    "ty.toml: ty reads it instead of [tool.ty] in pyproject.toml, so the gate there "
                    "no longer applies; delete it"
                )
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
                        (
                            f"[tool.ty] {name}: a ty setting the gate does not allow, since it could take "
                            "files or diagnostics out of ty's check; allow it in TY_SETTINGS of "
                            "tests/test_python_files.py only once it is known not to"
                        )
                    ],
                )
        # The platform may only be Linux, the one CST runs on.
        self.assertEqual(ty_setting_problems(conf(environment={"python-platform": "linux"}), ["a.py"]), [])
        for platform in ("all", "darwin", "win32"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    ty_setting_problems(conf(environment={"python-platform": platform}), ["a.py"]),
                    [
                        (
                            f"[tool.ty] environment.python-platform = {platform!r}: only 'linux' is allowed "
                            "(CST runs on Linux only; another platform changes what ty checks)"
                        )
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
                        (
                            f"[tool.ty] rules.invalid-assignment = {level!r}: a rule may only be raised to "
                            '"error" here; a lower level takes its diagnostics out of the gate'
                        )
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
                        (
                            f"[tool.ruff] required-version = {bad!r}: must be '==1.2.3', the ruff pin of the "
                            "dev group (change both together)"
                        )
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
                        (
                            f"[tool.ruff.lint] typing-extensions = {got!r}: must be false (standard library "
                            "only: no ruff fix may import typing_extensions)"
                        )
                    ],
                )

    def test_format_suppressions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-pyfiles-") as tmp:
            # Built from pieces so that this file holds no such comment of its own.
            fmt, yapf = "# fmt" + ": ", "# yapf" + ": "
            files = {
                "clean.py": f's = "{fmt}skip"\nx = [1, 2]  # format: compact\n',
                "a.py": f"x = [1,2]  {fmt}skip\n",
                "b.py": f"{fmt}off\nx = [1,2]\n{fmt}on\n",
                "c.py": "y = 1  #fmt:skip\n",
                "d.py": f"{yapf}disable\nz = 2\n",
            }
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w") as fh:
                    fh.write(text)
            self.assertEqual(
                format_suppressions(tmp, files),
                [
                    f"a.py:1: '{fmt}skip' keeps ruff format away; let it lay the code out",
                    f"b.py:1: '{fmt}off' keeps ruff format away; let it lay the code out",
                    f"b.py:3: '{fmt}on' keeps ruff format away; let it lay the code out",
                    "c.py:1: '#fmt:skip' keeps ruff format away; let it lay the code out",
                    f"d.py:1: '{yapf}disable' keeps ruff format away; let it lay the code out",
                ],
            )

    def test_ruff_preview_only_for_the_rules_named(self) -> None:
        def conf(lint: Mapping[str, object], **ruff: object) -> dict[str, object]:
            return {"tool": {"ruff": {"lint": dict(lint), **ruff}}}

        good = {"preview": True, "explicit-preview-rules": True, "select": ["F", "RUF039"]}
        self.assertEqual(ruff_preview_problems(conf(good)), [])
        self.assertEqual(ruff_preview_problems(conf({"select": ["F"]})), [])
        self.assertEqual(ruff_preview_problems(conf(good, format={"preview": False})), [])
        needs_explicit = (
            "[tool.ruff.lint] preview = true needs explicit-preview-rules = true: otherwise a family in "
            "select turns on every preview rule of that family; name each preview rule instead"
        )
        for lint in ({"preview": True}, {"preview": True, "explicit-preview-rules": False}):
            with self.subTest(lint=lint):
                self.assertEqual(ruff_preview_problems(conf(lint)), [needs_explicit])
        # The older top-level form of explicit-preview-rules still counts.
        self.assertEqual(ruff_preview_problems(conf({"preview": True}, **{"explicit-preview-rules": True})), [])
        self.assertEqual(
            ruff_preview_problems(conf(good, format={"preview": True})),
            ["[tool.ruff.format] preview = true: the preview layout can change in any ruff release; leave it off"],
        )
        self.assertEqual(
            ruff_preview_problems(conf(good, preview=True)),
            [
                (
                    "[tool.ruff] preview = true: it also turns on the formatter's preview style; "
                    "set preview in [tool.ruff.lint] only"
                )
            ],
        )
        for where, ruff in (
            ("tool.ruff.lint] select", conf({**good, "select": ["F", "PLC1901"]})),
            ("tool.ruff.lint] extend-select", conf({**good, "extend-select": ["PLC1901"]})),
            ("tool.ruff] select", conf(good, select=["PLC1901"])),
        ):
            with self.subTest(where=where):
                self.assertEqual(
                    ruff_preview_problems(ruff),
                    [
                        (
                            f'[{where} names PLC1901: it asks for `not x` instead of `x == ""`, '
                            "the opposite of the house style; never select it"
                        )
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
            + ruff_preview_problems(conf)
            + ty_setting_problems(conf, files)
            + ignore_comments(REPO, ty_gated(py_files, ty_gate))
            + format_suppressions(REPO, py_files)
        )
        self.assertEqual(found, [], "the Python gates miss something (see docs/dev-tools.md):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
