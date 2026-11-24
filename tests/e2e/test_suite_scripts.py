# SPDX-License-Identifier: MPL-2.0
"""Gate: the bash scripts of the E2E suites check what they claim to check.

The suites run their scripts under `bash -euo pipefail`, and set -e does not stop a script on a
negation (`! grep ...`) or on the first command of an `A && B` list: such a line checks nothing when
it fails. This gate reads the suite files as text and names, file:line, each such check: a line that
starts with `!`, and a line that starts with a check (`[[`, `[`, `test`, `grep`, `cmp`, `!`) followed by
`&&` and a second check. A line with `||` on it handles the failure itself and passes. The forms that
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


def unchecked_line(line: str) -> bool:
    """True for a line whose check set -e never stops on: a bare negation, or a check followed by
    `&&` and a second check, with no `||` on the line."""
    if "||" in line or _FUNCTION_LINE.match(line):
        return False
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
            "rm -rf w && mkdir -p w",
            "[[ $rc == 1 ]] || { cat out >&2; exit 1; }",
            'grep -qF a "$f" && grep -qF b "$f" || exit 1',
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
