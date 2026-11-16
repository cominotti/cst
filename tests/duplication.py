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
  of shared).

The method is the one of the reference scripts the conversion was measured
with, except for two things that make the numbers a little lower:
- function and class docstrings are dropped too (the reference dropped only
  the module's);
- an import statement is one code line however the formatter wraps it. A
  long `from pkg.mod import (` list is one name per line, which type 2
  turns into runs of identical `N ,` lines: two such lists anywhere looked
  like copied code, and moving a monolith into a package "duplicated" its
  imports. The statement is still measured, as one line.
"""

import ast
from collections import defaultdict
from collections.abc import Iterable, Mapping
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
_SHELL_TRIVIAL: Final = frozenset({"fi", "done", "esac", "}", "{", ";;", "else", "then", "do"})


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


def normalized_python(source: str, *, type2: bool) -> list[str]:
    """The code lines of `source`, normalized, in order ([] when it does not tokenize)."""
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
        if type2 and tk.type == tokenize.NAME and not keyword.iskeyword(tk.string):
            text = "N"
        elif type2 and tk.type == tokenize.STRING:
            text = "S"
        elif type2 and tk.type == tokenize.NUMBER:
            text = "0"
        else:
            text = tk.string
        lines[tk.start[0] if import_line is None else import_line].append(text)
        prev = tk.type
    out = []
    for ln in sorted(lines):
        s = " ".join(lines[ln])
        if s not in _TRIVIAL_LINES:
            out.append(s)
    return out


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
            index[tuple(lines[i : i + window])].append((path, i))
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
        code_lines=sum(len(v) for v in norm.values()),
        duplicated=sum(len(v) for v in dup.values()),
        cross_file=sum(len(v) for v in cross.values()),
        per_file={p: len(v) for p, v in sorted(dup.items(), key=lambda kv: -len(kv[1]))},
    )


def python_clones(repo: str, files: Iterable[str], window: int, *, type2: bool) -> CloneReport:
    """Clone report of the given Python files."""
    norm: dict[str, list[str]] = {}
    for rel in files:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            norm[rel] = normalized_python(fh.read(), type2=type2)
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


def is_shell(rel: str, repo: str) -> bool:
    """A .sh/.bash file or a file with a bash/sh shebang."""
    if rel.endswith((".sh", ".bash")):
        return True
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as fh:
            return fh.readline().startswith(("#!/usr/bin/env bash", "#!/bin/bash", "#!/bin/sh"))
    except OSError:
        return False
