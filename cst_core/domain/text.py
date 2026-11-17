"""Small text rules shared by CST programs."""

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Final


def first_line(text: str | None) -> str:
    """The first non-blank line of text, stripped ("" when there is none)."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def file_lines(content: str) -> list[str]:
    """The lines of a file as bash's `while read -r` sees them: only a newline ends one, and
    a last line without a newline is still a line (str.splitlines() would also split at a
    carriage return, a form feed and other separators, and so change lines that are not ours)."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def is_yes(answer: str) -> bool:
    """A [y/N] prompt's answer is yes: it starts with y or s (sim), any case."""
    return answer.strip().lower().startswith(("y", "s"))


_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BLANKS: Final = " \t"  # what bash splits words at (not \f, \v or NBSP: bash keeps them in a value)
# Characters bash treats as syntax in an unquoted word; any of them makes the line a problem.
_SHELL_SYNTAX: Final = frozenset(";&|<>()")
_EXPANDING: Final = frozenset("$`\\")


@dataclass(frozen=True, slots=True)
class SettingsProblem:
    """A line of a settings file that is not a plain KEY=VALUE assignment; it sets nothing."""

    line: int
    text: str
    reason: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.text.strip()}: {self.reason}"


@dataclass(frozen=True, slots=True)
class ShellAssignments:
    """The assignments of a settings file in file order, and the lines that are problems."""

    entries: tuple[Assignment, ...]
    problems: tuple[SettingsProblem, ...]

    @property
    def values(self) -> Mapping[str, str]:
        """{KEY: value}; the last assignment of a key wins, as in bash."""
        return {a.key: a.value for a in self.entries}


@dataclass(frozen=True, slots=True)
class Assignment:
    """One KEY=VALUE line of a settings file: its line number (1-based), key and value."""

    line: int
    key: str
    value: str


def _rest_is_comment(rest: str, blanks: str) -> bool:
    """After a value: nothing, or blanks and then a # comment."""
    stripped = rest.lstrip(blanks)
    return stripped == "" or (stripped != rest and stripped.startswith("#"))


def _value(raw: str, *, bash_sourced: bool) -> tuple[str | None, str]:
    """(value, "") or (None, why the line is a problem) for the text after the = sign."""
    blanks = _BLANKS if bash_sourced else None
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end < 0:
            return None, f"no closing {quote}"
        inner, rest = raw[1:end], raw[end + 1 :]
        if quote == '"' and _EXPANDING & set(inner):
            return None, "$, ` or \\ inside double quotes: bash would expand it (use single quotes)"
        if not _rest_is_comment(rest, blanks or " \t\f\v\r"):
            return None, "text after the closing quote (only a # comment may follow)"
        return inner, ""
    if bash_sourced:
        word = raw
        for i, ch in enumerate(raw):
            if ch in _BLANKS:
                word = raw[:i]
                if not _rest_is_comment(raw[i:], _BLANKS):
                    return None, "a blank in an unquoted value: bash runs the rest as a command (quote the value)"
                break
    else:
        word = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if _EXPANDING & set(word):
        return None, "$, ` or \\ outside single quotes: bash would expand it (use single quotes)"
    if "'" in word or '"' in word:
        return None, "a quote inside an unquoted value (quote the whole value)"
    if bash_sourced:
        if _SHELL_SYNTAX & set(word):
            return None, "shell syntax (; & | < > ( )) in an unquoted value (quote the value)"
        if word.startswith("~") or ":~" in word:
            return None, "an unquoted ~ that bash would expand to a home folder (quote the value)"
    return word, ""


def _assignment(line: str, *, bash_sourced: bool) -> tuple[str, str] | str | None:
    """(key, value), a problem's reason, or None for a blank or comment line."""
    blanks = _BLANKS if bash_sourced else None
    s = line.lstrip(blanks) if blanks else line.strip()
    if not s or s.startswith("#"):
        return None
    if "\r" in line and bash_sourced:
        return "a carriage return (Windows line end): bash keeps it in the value"
    if s.startswith("export") and s[6:7] and s[6] in _BLANKS:
        s = s[6:].lstrip(_BLANKS)
    m = _KEY.match(s)
    if not m:
        return "not a KEY=VALUE line"
    key, rest = m.group(0), s[m.end() :]
    if bash_sourced:
        if not rest.startswith("="):
            if rest.lstrip(_BLANKS).startswith("="):
                return "blanks around =: bash runs it as a command and sets nothing"
            return "not a KEY=VALUE line"
        raw = rest[1:]
        if raw[:1] in _BLANKS:
            if _rest_is_comment(raw, _BLANKS):
                return key, ""
            return "blanks around =: bash runs it as a command and sets nothing"
    else:
        rest = rest.lstrip()
        if not rest.startswith("="):
            return "not a KEY=VALUE line"
        raw = rest[1:].strip()
    value, why = _value(raw, bash_sourced=bash_sourced)
    return why if value is None else (key, value)


def describe_problems(path: str, problems: tuple[SettingsProblem, ...]) -> str:
    """One line per problem: `PATH line N: TEXT: REASON`."""
    return "\n".join(f"{path} {p}" for p in problems)


def parse_shell_assignments(text: str, *, bash_sourced: bool = True) -> ShellAssignments:
    """The KEY=VALUE lines of a settings file, read as bash reads the subset CST's files use.

    A line is blank, a # comment, or `[export ]KEY=VALUE [# comment]` with
    KEY [A-Za-z_][A-Za-z0-9_]* and VALUE one of: '...' (literal), "..."
    (without $, ` or \\), or one unquoted word (without blanks, quotes, $, `,
    \\, ; & | < > ( ), or a ~ bash would expand). Every other line is a
    problem (SettingsProblem, with its line number) and sets nothing: never a
    guessed value. With bash_sourced (cpu-setup.conf, snapshots.conf, which
    bash also sources) the values are exactly bash's. bash_sourced=False is
    for files only Python reads (vm.conf, toolchains.conf, bluetooth.conf):
    it also accepts what their readers always did, blanks around = and an
    unquoted value of several words (up to a blank and #), with blanks
    trimmed. Lines as file_lines reads them; the last assignment wins.
    """
    entries: list[Assignment] = []
    problems: list[SettingsProblem] = []
    for number, line in enumerate(file_lines(text), 1):
        got = _assignment(line, bash_sourced=bash_sourced)
        if isinstance(got, tuple):
            entries.append(Assignment(number, got[0], got[1]))
        elif isinstance(got, str):
            problems.append(SettingsProblem(number, line, got))
    return ShellAssignments(tuple(entries), tuple(problems))
