# SPDX-License-Identifier: MPL-2.0
"""Duplicated code in the checkout, measured (for tests/test_duplication.py).

- Python clones: sliding windows of W normalized code lines (comments,
  blank lines and docstrings dropped; trivial lines such as `)` or
  `return` skipped). Type 1 keeps the tokens as written; type 2 replaces
  every identifier, string and number by a placeholder, so a copy with
  renamed variables still counts. A line is duplicated when it is in a
  window that occurs more than once; `cross_file` counts only windows that
  occur in two or more files.
- Shell clones: the same on shell scripts (comments, blank lines and bare
  `fi`/`done`/... dropped, whitespace collapsed), type 1.
- Concepts: top-level functions and classes defined under the same name in
  more than one production file (the usual sign of a helper copied instead
  of shared), and the same for shell functions across the shell files.

The method is the one of the reference scripts the conversion was measured
with, except for three things that make the numbers a little lower:
- function and class docstrings are dropped too (the reference dropped only
  the module's);
- type 2 skips data rows, lines of literals only (`"rd_ios",`,
  `"a": 1,`): with every literal replaced, any two lists of eight strings
  were the same "renamed copy". Type 1 still measures them as written, so
  a copied table is still found;
- an import statement is one code line however the formatter wraps it, and
  type 2 keeps the names in it as written. A long `from pkg.mod import (`
  list is one name per line, and type 2 turned those lines, and any run of
  `import os` / `import re` / ..., into identical `N ,` or `import N`
  lines: two such blocks anywhere looked like copied code, and moving a
  monolith into a package "duplicated" its imports. Renaming what an
  import names is not a renamed copy: it imports something else. The
  statement is still measured, as one line, and an identical import block
  still counts.

Some code is the same in several files by design; the measure leaves it
out:
- the entry-point bootstrap, which tests/test_entrypoints.py requires to be
  byte for byte the same in every entry point (tests/test_duplication.py
  removes it before measuring);
- the body of a `Protocol` class (a port): its method stubs restate the
  signatures its adapters implement, and the type checker compares the two.
  The adapter's code still counts;
- type 2 only, declarations (`declared_lines`): the fields of a record
  class (a `@dataclass`, a `TypedDict`, a `NamedTuple`: `name: type` and
  `name: type = default` lines) and an `__init__` whose body only assigns
  its parameters to attributes. With every name replaced, any two records of
  eight fields read the same (`N : N` eight times), which says nothing about
  copied code. Type 1 still measures them as written, so a field list copied
  word for word is still found.
A left-out line is a barrier, not a deletion (`BARRIER`): no window runs
across it and it is not a code line, so the lines before and after it do not
join into a block the file does not have.
"""

import ast
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Set as AbstractSet
from dataclasses import dataclass
import io
import keyword
import os
import re
import tokenize
from typing import Final

_SKIP_TOKENS: Final = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)
_TRIVIAL_LINES: Final = frozenset({")", "]", "}", "),", "],", "},", "else :", "try :", "return", "pass"})
# Type 2: what a data row (a line of literals only, `"rd_ios",`) becomes.
_DATA_TOKENS: Final = frozenset({"S", "0", "True", "False", "None", ",", ":", "(", ")", "[", "]", "{", "}", "-"})
_SHELL_TRIVIAL: Final = frozenset({"fi", "done", "esac", "}", "{", ";;", "else", "then", "do"})
# Stands for left-out lines in a normalized file: no window holds it, and it is no code line.
BARRIER: Final = "\0"
_RECORD_BASES: Final = frozenset({"TypedDict", "NamedTuple"})


@dataclass(frozen=True, slots=True)
class CloneReport:
    """Duplicated lines out of the code lines of a set of files."""

    files: int
    code_lines: int
    duplicated: int
    cross_file: int
    per_file: Mapping[str, int]

    @property
    def percent(self) -> float:
        """Duplicated lines as a percentage of the code lines."""
        return 100 * self.duplicated / max(self.code_lines, 1)


def normalized_python(source: str, *, type2: bool, skip: AbstractSet[int] = frozenset()) -> list[str]:
    """The code lines of `source`, normalized, in order ([] when it does not tokenize).

    The lines in `skip` (1-based) become one BARRIER per run of them.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError, SyntaxError, IndentationError:
        return []
    lines: dict[int, list[str]] = defaultdict(list)
    prev: int | None = None
    import_line: int | None = None  # first line of the import statement being read
    for tk in toks:
        if tk.type in _SKIP_TOKENS:
            if tk.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                prev = tk.type  # a docstring is the first token of its line
            if tk.type == tokenize.NEWLINE:
                import_line = None
            continue
        if (
            tk.type == tokenize.NAME
            and tk.string in ("import", "from")
            and prev in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
        ):
            import_line = tk.start[0]  # `from` at the start of a statement is an import
        if (
            tk.type == tokenize.STRING
            and prev in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
            and tk.start[1] == tk.line.find(tk.string[:1])
            and tk.line.strip().startswith(tk.string[:3])
            and tk.string[:3] in ('"""', "'''")
        ):
            prev = tk.type
            continue  # a docstring
        if type2 and tk.type == tokenize.NAME and not keyword.iskeyword(tk.string) and import_line is None:
            text = "N"
        elif type2 and tk.type == tokenize.STRING:
            text = "S"
        elif type2 and tk.type == tokenize.NUMBER:
            text = "0"
        else:
            text = tk.string
        lines[tk.start[0] if import_line is None else import_line].append(text)
        prev = tk.type
    out: list[str] = []
    for ln in sorted(lines):
        if ln in skip:
            if not out or out[-1] != BARRIER:
                out.append(BARRIER)
            continue
        s = " ".join(lines[ln])
        if s not in _TRIVIAL_LINES and not (type2 and set(s.split()) <= _DATA_TOKENS):
            out.append(s)
    return out


def _simple_name(node: ast.expr) -> str | None:
    """`X` for `X`, `mod.X`, `X[T]` or `X(...)` (a base class or a decorator), else None."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Subscript):  # Protocol[T]
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _is_record(node: ast.ClassDef) -> bool:
    """A `@dataclass`, `TypedDict` or `NamedTuple` class."""
    return any(_simple_name(b) in _RECORD_BASES for b in node.bases) or any(
        _simple_name(d) == "dataclass" for d in node.decorator_list
    )


def _assigns_parameters_only(node: ast.FunctionDef) -> bool:
    """An `__init__` whose body (its docstring aside) is only `self.x = x` for its parameters."""
    if node.name != "__init__":
        return False
    params = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if not body:
        return False
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            return False
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
            return False
        if not (isinstance(value, ast.Name) and value.id in params):
            return False
    return True


def _lines(node: ast.stmt) -> range:
    """The 1-based lines a statement spans."""
    return range(node.lineno, (node.end_lineno or node.lineno) + 1)


def declared_lines(source: str, *, records: bool) -> set[int]:
    """The lines the measure leaves out (1-based): `Protocol` bodies; with `records`, also record
    fields and `__init__` bodies that only assign their parameters (see the module docstring)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if any(_simple_name(b) == "Protocol" for b in node.bases):
                out.update(range(node.body[0].lineno, _lines(node).stop))
            elif records and _is_record(node):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        out.update(_lines(stmt))
        elif records and isinstance(node, ast.FunctionDef) and _assigns_parameters_only(node):
            out.update(_lines(node))
    return out


def measured_python(source: str, *, type2: bool) -> list[str]:
    """What the measure reads of one source: normalized, with its `declared_lines` as barriers."""
    return normalized_python(source, type2=type2, skip=declared_lines(source, records=type2))


def normalized_shell(source: str) -> list[str]:
    """The code lines of a shell script, normalized."""
    out = []
    for line in source.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s in _SHELL_TRIVIAL:
            continue
        out.append(re.sub(r"\s+", " ", s))
    return out


def clones(norm: Mapping[str, list[str]], window: int) -> CloneReport:
    """Duplicated lines across `norm` ({file: normalized lines}) for windows of `window` lines."""
    index: dict[tuple[str, ...], list[tuple[str, int]]] = defaultdict(list)
    for path, lines in norm.items():
        for i in range(len(lines) - window + 1):
            block = tuple(lines[i : i + window])
            if BARRIER not in block:
                index[block].append((path, i))
    dup: dict[str, set[int]] = defaultdict(set)
    cross: dict[str, set[int]] = defaultdict(set)
    for occ in index.values():
        if len(occ) < 2:
            continue
        files = {p for p, _ in occ}
        for p, i in occ:
            dup[p].update(range(i, i + window))
            if len(files) > 1:
                cross[p].update(range(i, i + window))
    return CloneReport(
        files=len(norm),
        code_lines=sum(len(v) - v.count(BARRIER) for v in norm.values()),
        duplicated=sum(len(v) for v in dup.values()),
        cross_file=sum(len(v) for v in cross.values()),
        per_file={p: len(v) for p, v in sorted(dup.items(), key=lambda kv: -len(kv[1]))},
    )


def python_clones(
    repo: str,
    files: Iterable[str],
    window: int,
    *,
    type2: bool,
    prepare: Callable[[str], str] = lambda source: source,
) -> CloneReport:
    """Clone report of the given Python files (`prepare` edits each source first; `declared_lines` are left out)."""
    norm: dict[str, list[str]] = {}
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            norm[rel] = measured_python(prepare(fh.read()), type2=type2)
    return clones(norm, window)


def shell_clones(repo: str, files: Iterable[str], window: int) -> CloneReport:
    """Clone report of the given shell files."""
    norm: dict[str, list[str]] = {}
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            norm[rel] = normalized_shell(fh.read())
    return clones(norm, window)


def same_name_definitions(repo: str, files: Iterable[str], allowed: Iterable[str]) -> dict[str, list[str]]:
    """{name: files} for top-level functions/classes defined in more than one file (allowed names aside)."""
    skip = set(allowed)
    where: dict[str, set[str]] = defaultdict(set)
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            try:
                tree = ast.parse(fh.read(), filename=rel)
            except SyntaxError:
                continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name not in skip:
                where[node.name].add(rel)
    return {name: sorted(paths) for name, paths in sorted(where.items()) if len(paths) > 1}


_SHELL_FUNCTION = re.compile(r"^\s*(?:function\s+([A-Za-z_][\w:-]*)\b|([A-Za-z_][\w:-]*)\s*\(\s*\))")


def shell_function_names(source: str) -> set[str]:
    """The functions a shell script defines (`name() {`, `function name`)."""
    out: set[str] = set()
    for line in source.splitlines():
        m = _SHELL_FUNCTION.match(line)
        if m:
            out.add(m.group(1) or m.group(2))
    return out


def shell_same_name_definitions(repo: str, files: Iterable[str], allowed: Iterable[str]) -> dict[str, list[str]]:
    """{name: files} for shell functions defined in more than one shell file (allowed names aside)."""
    skip = set(allowed)
    where: dict[str, set[str]] = defaultdict(set)
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            for name in shell_function_names(fh.read()) - skip:
                where[name].add(rel)
    return {name: sorted(paths) for name, paths in sorted(where.items()) if len(paths) > 1}


_SHELL_FIRST_LINES: Final = (
    "#!/usr/bin/env bash",
    "#!/bin/bash",
    "#!/usr/bin/bash",
    "#!/bin/sh",
    "#!/usr/bin/sh",
    "# shellcheck shell=",
)


def is_shell(rel: str, repo: str) -> bool:
    """A .sh/.bash file, a file with a bash/sh shebang, or one whose first line is `# shellcheck shell=`."""
    if rel.endswith((".sh", ".bash")):
        return True
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith(_SHELL_FIRST_LINES)
