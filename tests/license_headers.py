#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""License headers: the one SPDX line every file of the checkout carries.

Use: python3 -I -B tests/license_headers.py [--apply]   (from anywhere;
`invowk cmd fix license`). Without --apply it only lists the files that lack
the header; with --apply it inserts it. Running it again changes nothing.

The header is one line, "SPDX-License-Identifier: MPL-2.0", in the comment
syntax of the file (header_line). It goes on line 1, or right after a line
that has to stay first (header_index): a shebang, a `# shellcheck shell=`
directive (tests/duplication.py finds shell files by it), an XML
declaration, a Dockerfile `# syntax=` directive, or a Markdown file's YAML
frontmatter (agent skills and rules: the frontmatter has to open the file).
The full notice of the license, its Exhibit A, stays in LICENSE, which the
Exhibit allows, and README's License section points there.

LICENSE_EXEMPT names the files that carry no header, each with its reason:
formats without comments and captured data. tests/test_license_headers.py
(`invowk cmd test license`) checks every file against these rules.

Standard library only: the unit tests run on /usr/bin/python3 without uv.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import fnmatch
import os
import sys
from typing import Final
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.repo_files import repo_files

IDENTIFIER: Final = "SPDX-License-Identifier: MPL-2.0"

# Files (fnmatch patterns, repository-relative) that carry no header, with the
# reason. Every pattern must match a file of the checkout.
LICENSE_EXEMPT: Final[dict[str, str]] = {
    "LICENSE": "the license itself; its Exhibit A is the full notice",
    "uv.lock": "generated: `uv lock` writes the whole file",
    "tests/fixtures/state-formats/*.json": "JSON has no comments (the exact bytes of state files CST's writers produce)",
    "tests/fixtures/legacy/bluetooth-setup.sh@d4d8c91": (
        "captured: bluetooth-setup.sh of commit d4d8c91 (main before the port to Python: the program installed workstations run), byte for byte; "
        "tier 1 test_95 installs it as the previous version"
    ),
    "doctor/tests/fixtures/*.json": "JSON has no comments (captured `rpm-ostree status --json` output)",
    "btrfs/tests/fixtures/steam/*.acf": "Steam's own app manifests, as Steam writes them, for the parser under test",
    "btrfs/tests/fixtures/steam/*.vdf": "Steam's own library files, as Steam writes them, for the parser under test",
    "tests/fixtures/legacy/*": (
        "captured data: programs of an older commit (named after the @), copied byte for byte, that tier 1 "
        "installs as the previous version (tests/e2e/README.md, Mixed-version tests), except that a personal or "
        "private value in one is replaced by an invented one"
    ),
    "tests/quint/package.json": "JSON has no comments (npm's manifest of the quint pin, for npm ci)",
    "tests/quint/package-lock.json": "JSON has no comments (npm's lock of the quint pin: every package and its hash)",
    "btrfs/tests/quint/traces/*.itf.json": "JSON has no comments (ITF traces quint writes; the replay reads them)",
    "doctor/tests/fixtures/db-booted.txt": (
        "captured `rpm-ostree db list` output, copied whole into a fake package database"
    ),
}

# Comment syntax: (start, end) of a one-line comment.
_HASH: Final = ("# ", "")
_SLASHES: Final = ("// ", "")
_SEMICOLON: Final = ("; ", "")
_MARKUP: Final = ("<!-- ", " -->")
_BY_SUFFIX: Final[dict[str, tuple[str, str]]] = {
    ".automount": _HASH,
    ".bash": _HASH,
    ".conf": _HASH,
    ".cue": _SLASHES,
    ".Dockerfile": _HASH,
    ".gitignore": _HASH,
    ".git-blame-ignore-revs": _HASH,
    ".ini": _SEMICOLON,
    ".js": _SLASHES,
    ".md": _MARKUP,
    ".mount": _HASH,
    ".py": _HASH,
    ".qnt": _SLASHES,  # Quint specs
    ".rules": _SLASHES,  # polkit rules are JavaScript
    ".service": _HASH,
    ".sh": _HASH,
    ".slice": _HASH,
    ".timer": _HASH,
    ".toml": _HASH,
    ".xml": _MARKUP,
    ".yml": _HASH,
    ".yaml": _HASH,
}
# First lines the header goes after: they have to stay first.
_KEEP_FIRST: Final = ("#!", "# shellcheck shell=", "<?xml", "# syntax=")
_FRONTMATTER: Final = "---"


@dataclass(frozen=True, slots=True)
class Change:
    """One file the fixer looked at: the header it would add (text), or why it cannot (refused)."""

    rel: str
    text: str | None = None
    refused: str | None = None


def is_exempt(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in LICENSE_EXEMPT)


def comment_syntax(rel: str, first_line: str) -> tuple[str, str] | None:
    """The one-line comment syntax of a file, None for a kind these rules do not know.

    A template (`x.rules.in`) has the syntax of what it renders (`x.rules`).
    udev `.rules` files use `#`; polkit `.rules` files use `//`.
    An extensionless program is known by its shebang.
    """
    name = os.path.basename(rel).removesuffix(".in")
    if name.endswith(".rules") and "/udev/" in f"/{rel}":
        return _HASH
    for suffix, syntax in _BY_SUFFIX.items():
        if name.endswith(suffix):
            return syntax
    if first_line.startswith("#!"):
        return _HASH
    return None


def header_line(rel: str, first_line: str) -> str | None:
    """The header as it is written in this file (no newline), None for an unknown kind."""
    syntax = comment_syntax(rel, first_line)
    if syntax is None:
        return None
    return syntax[0] + IDENTIFIER + syntax[1]


def _locate(rel: str, lines: Sequence[str]) -> tuple[str, list[int], int] | None:
    """For the lines of `rel` (no line ends): the header, the lines that hold it and the
    line it belongs on; None for an unknown kind."""
    syntax = comment_syntax(rel, lines[0] if lines else "")
    if syntax is None:
        return None
    header = syntax[0] + IDENTIFIER + syntax[1]
    at = [i for i, line in enumerate(lines) if line == header]
    # Where the header goes in the file without it: a header above a frontmatter
    # hides the frontmatter from header_index, and is still misplaced.
    want = header_index([line for line in lines if line != header], syntax)
    return header, at, want


def header_index(lines: Sequence[str], syntax: tuple[str, str]) -> int:
    """The 0-based line the header belongs on (lines as from str.splitlines())."""
    if not lines:
        return 0
    if lines[0].startswith(_KEEP_FIRST):
        return 1
    if syntax == _MARKUP and lines[0].rstrip() == _FRONTMATTER:
        for i in range(1, len(lines)):
            if lines[i].rstrip() == _FRONTMATTER:
                return i + 1
    return 0


def header_problem(rel: str, text: str) -> str | None:
    """Why `text` (the content of `rel`) does not carry the header as these rules want it; None when it does."""
    found = _locate(rel, text.splitlines())
    if found is None:
        return f"{rel}: unknown file kind: add its comment syntax to tests/license_headers.py, or an exemption"
    header, at, want = found
    if not at:
        return f"{rel}: no license header (want line {want + 1}: {header!r})"
    if at != [want]:
        return f"{rel}: the license header is on line(s) {[i + 1 for i in at]}, want only line {want + 1}"
    return None


def with_header(rel: str, text: str) -> str:
    """`text` with the header inserted where it belongs; unchanged when it is already there.

    Raises ValueError for an unknown kind or a header in the wrong place
    (that needs a look, not a second header).
    """
    problem = header_problem(rel, text)
    if problem is None:
        return text
    lines = text.splitlines(keepends=True)
    found = _locate(rel, [line.rstrip("\r\n") for line in lines])
    if found is None or found[1]:
        raise ValueError(problem)
    header, _, at = found
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, header + "\n")
    return "".join(lines)


def checked_files(repo: str, files: Iterable[str]) -> list[str]:
    """The files the rules apply to: not exempt, not a symbolic link (.claude/* point into .agents/)."""
    return [rel for rel in files if not is_exempt(rel) and not os.path.islink(os.path.join(repo, rel))]


def _read(repo: str, rel: str) -> str | None:
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError:
        return None


def problems(repo: str, files: Iterable[str]) -> list[str]:
    """One line per file that does not carry the header as the rules want it."""
    found: list[str] = []
    for rel in checked_files(repo, files):
        text = _read(repo, rel)
        problem = f"{rel}: not UTF-8 text" if text is None else header_problem(rel, text)
        if problem is not None:
            found.append(problem)
    return found


def unmatched_exemptions(files: Iterable[str]) -> list[str]:
    """The LICENSE_EXEMPT patterns that match no file (a stale exemption hides nothing, so it goes)."""
    names = list(files)
    return [pattern for pattern in LICENSE_EXEMPT if not fnmatch.filter(names, pattern)]


def plan(repo: str, files: Iterable[str]) -> list[Change]:
    """The files that lack the header: the new text of each, or why it cannot be added."""
    changes: list[Change] = []
    for rel in checked_files(repo, files):
        text = _read(repo, rel)
        if text is None:
            changes.append(Change(rel, refused=f"{rel}: not UTF-8 text"))
            continue
        try:
            new = with_header(rel, text)
        except ValueError as exc:
            changes.append(Change(rel, refused=str(exc)))
            continue
        if new != text:
            changes.append(Change(rel, text=new))
    return changes


def apply(repo: str, changes: Iterable[Change]) -> None:
    """Write the new texts in place (same file, so its mode stays)."""
    for change in changes:
        if change.text is not None:
            with open(os.path.join(repo, change.rel), "w", encoding="utf-8", newline="") as fh:
                fh.write(change.text)


def main(argv: Sequence[str] = (), repo: str = REPO, files: Sequence[str] | None = None) -> int:
    """Exit 0: done or nothing to do; 1: a file needs a look (refused); 2: bad arguments, or no file list.

    `files` defaults to the files of the checkout at `repo` (tests pass their own).
    """
    args = list(argv)
    if args not in ([], ["--apply"]):
        print("usage: license_headers.py [--apply]", file=sys.stderr)
        return 2
    if files is None:
        try:
            files = repo_files(repo)
        except (unittest.SkipTest, AssertionError) as exc:  # not a checkout, or git refused to read it
            print(f"license_headers.py: {exc}", file=sys.stderr)
            return 2
    changes = plan(repo, files)
    todo = [c for c in changes if c.text is not None]
    refused = [c.refused for c in changes if c.refused is not None]
    if args:
        apply(repo, todo)
    for change in todo:
        print(("added:      " if args else "would add:  ") + change.rel)
    for reason in refused:
        print("refused:    " + reason, file=sys.stderr)
    if not args and todo:
        print(f"{len(todo)} file(s) lack the license header; --apply adds it")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
