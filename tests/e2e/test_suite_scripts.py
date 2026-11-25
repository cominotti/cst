# SPDX-License-Identifier: MPL-2.0
"""Gate: the bash scripts of the E2E suites check what they claim to check.

The suites run their scripts under `bash -euo pipefail`, and set -e does not stop a script on a
negation (`! grep ...`) or on the first command of an `A && B` list: such a line checks nothing when
it fails. This gate reads the suite files as text and names, file:line, each such check: a line that
starts with `!`, and a line that starts with a check (`[[`, `[`, `test`, `grep`, `cmp`, `!`) followed by
`&&` and a second check. It also names a line that holds `rm -rf X && mkdir ...` anywhere (a script line, or
a Python string an exec_bash or docker_exec call runs): a rm that fails goes on to the mkdir, and the script
goes on over the old folder. A line with an `||` list operator on it (not one in quotes, in `[[ ... ]]` or in
parentheses) handles the failure itself and passes. The forms that
do stop the script: `absent NEEDLE FILE` (a function, whose status is its last command's),
`if grep ...; then exit 1; fi`, one check per line, or one `[[ ... && ... ]]`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re
from typing import Final
import unittest

SUITES: Final = Path(__file__).resolve().parent / "suites"
SUITE_FILES: Final = ("suite_tier1_systemd.py", "suite_tier2_storage_cpu.py")

# The first words of a check: a command whose failure is the finding.
_CHECK_WORDS: Final = frozenset({"[[", "[", "test", "grep", "cmp", "!"})
# A one-line function body (`name() { a && b; }`, braces doubled in an f-string): its status is the
# chain's, and the line that calls it stops the script.
_FUNCTION_LINE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{")
# rm -rf, then && and mkdir: set -e does not stop on the rm (tests/e2e/README.md).
_RM_THEN_MKDIR: Final = re.compile(r"\brm -rf\b[^;&|]*&&\s*mkdir\b")
_CONTINUED: Final = "\\"
_AND: Final = "&&"


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """(line number, text) of each line of text, stripped; a line that ends in a backslash or in
    `&&` is joined with the next."""
    start, parts = 0, list[str]()
    for number, raw in enumerate(text.splitlines(), 1):
        if not parts:
            start = number
        line = raw.strip()
        if line.endswith(_CONTINUED):
            parts.append(line.removesuffix(_CONTINUED).strip())
            continue
        parts.append(line)
        if not line.endswith(_AND):
            yield start, " ".join(parts)
            parts = []
    if parts:
        yield start, " ".join(parts)


def _first_word(text: str) -> str:
    words = text.split(maxsplit=1)
    return words[0] if words else ""


def _word_edge(line: str, i: int) -> bool:
    """Whether position i of line is the start or end of the line, or a blank or `;` there."""
    return i < 0 or i >= len(line) or line[i].isspace() or line[i] == ";"


def handles_failure(line: str) -> bool:
    """True when the line has an `||` list operator: outside quotes, outside `[[ ... ]]` (where `||` is a
    test's own or) and outside parentheses (a subshell or a command substitution)."""
    quote, depth, i = "", 0, 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif line.startswith("[[", i) and _word_edge(line, i - 1) and _word_edge(line, i + 2):
            depth += 1
            i += 1
        elif line.startswith("]]", i) and _word_edge(line, i - 1) and _word_edge(line, i + 2):
            depth = max(depth - 1, 0)
            i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(depth - 1, 0)
        elif line.startswith("||", i) and depth == 0:
            return True
        i += 1
    return False


def unchecked_line(line: str) -> bool:
    """True for a line whose check set -e never stops on: a bare negation, a check followed by `&&` and a
    second check, or `rm -rf X && mkdir ...` anywhere on it, with no `||` list operator on the line (one in
    quotes or in `[[ ... ]]` handles nothing)."""
    if handles_failure(line) or _FUNCTION_LINE.match(line):
        return False
    if _RM_THEN_MKDIR.search(line):
        return True
    first = _first_word(line)
    if first == "!":
        return True
    if first not in _CHECK_WORDS:
        return False
    head, sep, rest = "", " && ", line
    while sep in rest:
        part, rest = rest.split(sep, 1)
        head += part + sep
        if head.count("[[") > head.count("]]"):
            continue  # an && inside one [[ ... ]]
        if _first_word(rest) in _CHECK_WORDS:
            return True
    return False


def unchecked_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) of each check in text that never stops its script."""
    return [(n, line) for n, line in _logical_lines(text) if unchecked_line(line)]


class SuiteScriptsGateTest(unittest.TestCase):
    def test_it_reports_checks_that_never_stop_the_script(self) -> None:
        for source in (
            '! grep -qF x "$f"',
            "! systemctl is-active -q a.timer",
            'grep -qF a "$f" && ! grep -qF b "$f"',
            "[[ $rc == 1 ]] && grep -qF restored out",
            "test -L l && test ! -e l",
            "[ ! -e a ] && [ ! -e b ]",
            "[[ -e a && -e b ]] && cmp -s a b",
            'grep -qF a "$f" && \\\n    grep -qF b "$f"',
            'grep -qF a "$f" &&\n    grep -qF b "$f"',
            "! grep -qF 'a||b' \"$f\"",
            '! grep -qF "a || b" "$f"',
            '[[ $x == 1 || $y == 2 ]] && grep -qF z "$f"',
            '! grep -qF "$(a || b)" "$f"',
            'grep -qF a\\|\\|b "$f" && grep -qF c "$f"',
            "rm -rf w && mkdir -p w",
            'rm -rf "$w" && mkdir -p "$w/bin"',
            'exec_bash(c, f"rm -rf {w} && mkdir -p {w}")',
            '["bash", "-c", f"rm -rf {w} && mkdir -p {w} && cat >f"]',
        ):
            with self.subTest(source=source):
                self.assertEqual([n for n, _ in unchecked_lines(f"set -e\n    {source}\necho ok\n")], [2])

    def test_it_leaves_checks_that_stop_the_script_alone(self) -> None:
        for source in (
            '! grep -qF x "$f" || { echo "x in $f" >&2; exit 1; }',
            "[[ -e a && -e b ]]",
            'if ! grep -qF x "$f"; then exit 1; fi',
            'if grep -qF x "$f"; then exit 1; fi',
            "for f in a b; do [ -e $f ] && break; done",
            "x=0; [[ -e f ]] && x=1",
            "[[ -e f ]] && was_off=1",
            "[ -e f ] && break",
            "unchanged() { cmp -s a b && cmp -s c d; }",
            "unchanged() {{ cmp -s a b && cmp -s c d; }}",
            'absent() {{ ! grep -qF -- "$1" "$2"; }}',
            'absent x "$f"',
            "rm -rf w\n    mkdir -p w",
            "rm -f x || true",
            "rm -rf w && mkdir -p w || exit 1",
            "[[ $rc == 1 ]] || { cat out >&2; exit 1; }",
            'grep -qF a "$f" && grep -qF b "$f" || exit 1',
            '[[ $x == 1 || $y == 2 ]] && grep -qF z "$f" || exit 1',
            "! grep -qF 'a||b' \"$f\" || { echo found >&2; exit 1; }",
            "[[ -e a ]]||exit 1",
        ):
            with self.subTest(source=source):
                self.assertEqual(unchecked_lines(f"set -e\n    {source}\necho ok\n"), [])

    def test_every_check_of_the_suites_stops_its_script(self) -> None:
        found = [
            f"{name}:{n}: {line}"
            for name in SUITE_FILES
            for n, line in unchecked_lines((SUITES / name).read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [], "checks that set -e never stops on (tests/e2e/README.md)")
