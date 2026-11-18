# SPDX-License-Identifier: MPL-2.0
"""Small text rules shared by CST programs."""

from collections.abc import Mapping
from dataclasses import dataclass
import re
import unicodedata
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


def _value(raw: str, *, bash_sourced: bool, exported: bool = False) -> tuple[str | None, str]:
    """(value, "") or (None, why the line is a problem) for the text after the = sign.

    exported: the line starts with `export `, so bash reads the value as an
    argument of a command, which is brace-expanded (`export K={a,b}` sets b).
    """
    blanks = _BLANKS if bash_sourced else " \t\f\v\r"
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end < 0:
            return None, f"no closing {quote}"
        inner, rest = raw[1:end], raw[end + 1 :]
        if quote == '"' and not _EXPANDING.isdisjoint(inner):
            return None, "$, ` or \\ inside double quotes: bash would expand it (use single quotes)"
        if not _rest_is_comment(rest, blanks):
            return None, "text after the closing quote (only a # comment may follow)"
        return inner, ""
    if bash_sourced:
        cut = next((i for i, ch in enumerate(raw) if ch in _BLANKS), len(raw))
        word, after = raw[:cut], raw[cut:]
        # A $, ` or \ before the blank is named below (`a\ b` is one escaped word, not a command).
        if _EXPANDING.isdisjoint(word) and not _rest_is_comment(after, _BLANKS):
            return None, "a blank in an unquoted value: bash runs the rest as a command (quote the value)"
    else:
        word = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if not _EXPANDING.isdisjoint(word):
        return None, "$, ` or \\ outside single quotes: bash would expand it (use single quotes)"
    if "'" in word or '"' in word:
        return None, "a quote inside an unquoted value (quote the whole value)"
    if bash_sourced:
        if not _SHELL_SYNTAX.isdisjoint(word):
            return None, "shell syntax (; & | < > ( )) in an unquoted value (quote the value)"
        if word.startswith("~") or ":~" in word:
            return None, "an unquoted ~ that bash would expand to a home folder (quote the value)"
        if exported and "{" in word:
            return None, "a { in an unquoted value after export: bash brace-expands it (quote the value)"
    return word, ""


def _assignment(line: str, *, bash_sourced: bool) -> tuple[str, str] | str | None:
    """(key, value), a problem's reason, or None for a blank or comment line."""
    s = line.lstrip(_BLANKS) if bash_sourced else line.strip()
    if not s or s.startswith("#"):
        return None
    if "\r" in line and bash_sourced:
        return "a carriage return (Windows line end): bash keeps it in the value"
    exported = s.startswith("export") and s[6:7] != "" and s[6] in _BLANKS
    if exported:
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
    value, why = _value(raw, bash_sourced=bash_sourced, exported=exported)
    return why if value is None else (key, value)


def describe_problems(path: str, problems: tuple[SettingsProblem, ...]) -> str:
    """One line per problem: `PATH line N: TEXT: REASON`."""
    return "\n".join(f"{path} {p}" for p in problems)


def parse_shell_assignments(text: str, *, bash_sourced: bool = True) -> ShellAssignments:
    """The KEY=VALUE lines of a settings file, read as bash reads the subset CST's files use.

    A line is blank, a # comment, or `[export ]KEY=VALUE [# comment]` with
    KEY [A-Za-z_][A-Za-z0-9_]* and VALUE one of: '...' (literal), "..."
    (without $, ` or \\), or one unquoted word (without blanks, quotes, $, `,
    \\, ; & | < > ( ), a ~ bash would expand, or after `export` a { bash
    would brace-expand). Every other line is a
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


# What printf %q writes, as an older CST's snapshots.conf writer (snap_conf_set in
# btrfs/setup-snapshots.bash) left values there: plain characters and backslash-escaped
# ones, or $'...' with the escapes %q uses. Neither form expands anything in bash (no
# unescaped $, backtick, quote, blank or command syntax; the ~ rule is checked apart).
_OLD_WRITER_LINE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
_PRINTF_Q_WORD: Final = re.compile(r"(?:[^ \t\n\r\f\v'\"\\$`;&|<>()]|\\.)+", re.S)
# Possessive quantifiers: an escape takes as many octal or hex digits as bash takes (up to
# 3 or 2) and is never split again, so a line with no closing quote fails in linear time
# instead of trying every way to cut each \NNN (exponential backtracking).
_PRINTF_Q_ANSI_C: Final = re.compile(r"\$'((?:[^'\\]|\\(?:[abeEfnrtv\\'\"?]|[0-7]{1,3}+|x[0-9A-Fa-f]{1,2}+))*+)'", re.S)
_ANSI_C_ESCAPE: Final = re.compile(r"\\(?:([abeEfnrtv\\'\"?])|([0-7]{1,3})|x([0-9A-Fa-f]{1,2}))")
_ANSI_C_CHARS: Final = {
    "a": 7, "b": 8, "e": 27, "E": 27, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, "'": 39, '"': 34, "?": 63,
}  # fmt: skip


def _ansi_c_value(body: str) -> str:
    """The value bash gives $'BODY' (BODY already matched _PRINTF_Q_ANSI_C). The escapes
    give bytes (an octal one above \\377 keeps its low 8 bits, as in bash); the result is
    read as UTF-8 with bad bytes replaced, as read_text reads the rest of the file. A NUL
    ends the value, as in bash."""
    out = bytearray()
    pos = 0
    for m in _ANSI_C_ESCAPE.finditer(body):
        out += body[pos : m.start()].encode("utf-8", "surrogatepass")
        char, octal, hexa = m.groups()
        out.append(_ANSI_C_CHARS[char] if char else int(octal or hexa, 8 if octal else 16) & 0xFF)
        pos = m.end()
    out += body[pos:].encode("utf-8", "surrogatepass")
    return bytes(out).split(b"\0", 1)[0].decode("utf-8", "replace")


def _printf_q_value(token: str) -> str | None:
    """The value bash gives an assignment of TOKEN when TOKEN is what printf %q writes (a word
    of plain and backslash-escaped characters, or $'...' with %q's escapes), else None. A word
    that starts with ~ or holds :~ is None too: bash would put a home folder there."""
    m = _PRINTF_Q_ANSI_C.fullmatch(token)
    if m:
        return _ansi_c_value(m.group(1))
    if not _PRINTF_Q_WORD.fullmatch(token) or token.startswith("~") or ":~" in token:
        return None
    return re.sub(r"\\(.)", lambda e: e.group(1), token, flags=re.S)


# printf %q's rules (bash 5): the characters it puts a backslash before, and the ones that make it
# write the $'...' form instead (any character that is not printable; in a UTF-8 locale, letters
# such as é stay as they are).
_Q_BACKSLASHED: Final = frozenset(" \t\n!\"$&'()*,;<>?[\\]^`{|}")
_Q_ANSI_C_NAMES: Final = {7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r", 27: "E", 39: "'", 92: "\\"}


def _q_printable(ch: str) -> bool:
    """A character printf %q leaves as it is in a UTF-8 locale (glibc's iswprint): what Python counts as
    printable, and also the spaces (NBSP, U+2000...), format characters (soft hyphen, U+200B...) and
    private-use characters Python does not. A code point that one of the two Unicode versions has not
    assigned yet may still differ."""
    return ch.isprintable() or unicodedata.category(ch) in ("Zs", "Cf", "Co")


def printf_q(value: str) -> str:
    """value as bash's `printf %q` writes it (in a UTF-8 locale): '' when empty; $'...' when it holds
    a character that is not printable (a tab, a newline, a control character); else value with a
    backslash before each character the shell would read as syntax (and before a # or ~ that starts
    it, and a ~ after : or =). bash reads the result back (`eval`, a sourced line) as value; the
    tests check it against the real bash, character by character. Exact for ASCII, which is all the
    game profile writes (unit names, cgroup paths, values); see _q_printable for other characters."""
    if value == "":
        return "''"
    if not all(_q_printable(ch) for ch in value):
        out: list[str] = []
        for ch in value:
            code = ord(ch)
            if code in _Q_ANSI_C_NAMES:
                out.append("\\" + _Q_ANSI_C_NAMES[code])
            elif _q_printable(ch):
                out.append(ch)
            else:
                out.extend(f"\\{b:03o}" for b in ch.encode("utf-8", "surrogateescape"))
        return "$'" + "".join(out) + "'"
    out = []
    for i, ch in enumerate(value):
        tilde = ch == "~" and (i == 0 or value[i - 1] in ":=")
        if ch in _Q_BACKSLASHED or tilde or (ch == "#" and i == 0):
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _word_part(line: str, i: int) -> tuple[str, int] | None:
    """One quoted or unquoted part of a word at line[i] (not a blank): (its value, where it ends)."""
    ch = line[i]
    if line.startswith("$'", i):
        m = _PRINTF_Q_ANSI_C.match(line, i)
        return None if m is None else (_ansi_c_value(m.group(1)), m.end())
    if ch == "'":
        end = line.find("'", i + 1)
        return None if end < 0 else (line[i + 1 : end], end + 1)
    if ch == '"':
        m = re.compile(r'"((?:[^"\\$`]|\\["\\$`])*)"').match(line, i)
        return None if m is None else (re.sub(r'\\(["\\$`])', r"\1", m.group(1)), m.end())
    if ch == "\\":
        return None if i + 1 >= len(line) else (line[i + 1], i + 2)
    if ch in "$`;&|<>()":
        return None
    return ch, i + 1


def printf_q_words(line: str) -> list[str] | None:
    """The words of a line whose fields bash's `printf %q` wrote, as `eval "rec=(${line})"` reads
    them back: blanks between words; each word of plain and backslash-escaped characters, '...',
    "..." (no expansion) and $'...' parts. None for a line bash would not read as plain words
    (an unclosed quote, an unescaped $, backtick or command syntax)."""
    words: list[str] = []
    i = 0
    while i < len(line):
        if line[i] in _BLANKS:
            i += 1
            continue
        word: list[str] = []
        while i < len(line) and line[i] not in _BLANKS:
            part = _word_part(line, i)
            if part is None:
                return None
            word.append(part[0])
            i = part[1]
        words.append("".join(word))
    return words


def parse_snapshots_conf(text: str) -> ShellAssignments:
    """/etc/btrfs-setup/snapshots.conf, which bash sources: parse_shell_assignments, and also
    the KEY=VALUE lines an older CST wrote there with printf %q (`NAS_PATH=/a\\ b`,
    `K=$'a\\tb'`), read as bash reads them. Only that file: in every other file a
    backslash outside single quotes stays a problem. `cst btrfs snapshots setup` rewrites
    these lines in single quotes."""
    strict = parse_shell_assignments(text)
    entries = list(strict.entries)
    problems: list[SettingsProblem] = []
    for p in strict.problems:
        m = _OLD_WRITER_LINE.fullmatch(p.text)
        value = _printf_q_value(m.group(2)) if m and "\\" in m.group(2) else None
        if m is None or value is None:
            problems.append(p)
        else:
            entries.append(Assignment(p.line, m.group(1), value))
    entries.sort(key=lambda a: a.line)
    return ShellAssignments(tuple(entries), tuple(problems))
