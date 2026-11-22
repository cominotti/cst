# SPDX-License-Identifier: MPL-2.0
"""Small text rules shared by CST programs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata
from typing import Final, override


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


def read_fields(line: str, n: int) -> list[str]:
    """The n fields bash's `read -r` assigns from one line with the default IFS: blanks (space, tab,
    newline) around the line dropped, fields split at runs of blanks, the last field taking the rest
    of the line as it is (its inner blanks kept); missing fields are ""."""
    rest = line.strip(" \t\n")
    out: list[str] = []
    while rest and len(out) < n - 1:
        parts = _READ_BLANKS.split(rest, maxsplit=1)
        out.append(parts[0])
        rest = parts[1] if len(parts) > 1 else ""
    if rest:
        out.append(rest)
    return out + [""] * (n - len(out))


def group(m: re.Match[str], n: int | str = 0) -> str:
    """Group n of the match. ValueError when the group did not take part in it (an optional
    group or one side of an alternation: use group_or_none there)."""
    found = m.group(n)
    if isinstance(found, str):
        return found
    raise ValueError(f"group {n!r} of {m.re.pattern!r} did not take part in the match")


def group_or_none(m: re.Match[str], n: int | str) -> str | None:
    """Group n of the match, or None when it did not take part in it."""
    found = m.group(n)
    return found if isinstance(found, str) else None


def split(pattern: re.Pattern[str] | str, text: str, maxsplit: int = 0) -> list[str]:
    """re.split(pattern, text, maxsplit). ValueError when an item is None (a group of the
    pattern that did not take part in a match): every item is a str otherwise."""
    parts = re.split(pattern, text, maxsplit=maxsplit)
    out = [p for p in parts if isinstance(p, str)]
    if len(out) != len(parts):
        raise ValueError(f"{pattern!r}: a group that did not take part in a match split the text")
    return out


def cgroup_v2_path(text: str) -> str | None:
    """The cgroup v2 ("unified") path in the text of a /proc/PID/cgroup: the rest of the first line
    that starts with "0::/", from its "/" on (on a hybrid system the cgroup v1 lines,
    "N:controllers:/path", come before it). None when no line does (a cgroup v1 only file, an
    empty text, a "0::" line with no path). The rustc wrapper's `read -r` loop takes the same
    line (its `0::/*` case), and only a newline ends a line on both sides (file_lines): a cgroup
    name may hold a carriage return or another separator that str.splitlines() would cut the
    path at."""
    return next((ln[3:] for ln in file_lines(text) if ln.startswith("0::/")), None)


def is_yes(answer: str) -> bool:
    """A [y/N] prompt's answer is yes: it starts with y or s (sim), any case."""
    return answer.strip().lower().startswith(("y", "s"))


# A yes/no setting's words (any case, blanks around them dropped): toolchains' settings take one of
# each; a vm knob is off for a NO_WORDS word and on for anything else.
YES_WORDS: Final = frozenset({"yes", "true", "on", "1"})
NO_WORDS: Final = frozenset({"no", "false", "off", "0"})

# The words that ask a command for its help (cst and every module).
HELP_WORDS: Final = frozenset({"help", "-h", "--help"})


def enum_member[E: StrEnum](cls: type[E], word: str) -> E | None:
    """The member of cls whose value is word (a word from outside: argv, a file, another program's
    answer), or None for any other word: the caller keeps its own fall-through (never cls(word),
    which raises)."""
    return next((m for m in cls if m == word), None)


def parse_subcommand_head[E: StrEnum](
    argv: Sequence[str],
    enum_cls: type[E],
    *,
    default: E,
    help_action: E,
) -> tuple[E, Sequence[str], str | None]:
    """Resolve the leading subcommand word against `enum_cls` and `HELP_WORDS` without side effects."""
    if not argv:
        return default, (), None
    head = argv[0]
    if head in HELP_WORDS:
        return help_action, argv[1:], None
    action = enum_member(enum_cls, head)
    if action is None:
        return default, (), f"unknown subcommand {head!r}"
    return action, argv[1:], None


_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_READ_BLANKS: Final = re.compile(r"[ \t\n]+")
_BLANKS: Final = " \t"  # what bash splits words at (not \f, \v or NBSP: bash keeps them in a value)
# Characters bash treats as syntax in an unquoted word; any of them makes the line a problem.
_SHELL_SYNTAX: Final = frozenset(";&|<>()")
_EXPANDING: Final = frozenset("$`\\")
# The problem of a line that is no assignment at all (no key, or no = after it).
_NOT_AN_ASSIGNMENT: Final = "not a KEY=VALUE line"


@dataclass(frozen=True, slots=True)
class SettingsProblem:
    """A line of a settings file that is not a plain KEY=VALUE assignment; it sets nothing."""

    line: int
    text: str
    reason: str

    @override
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
        return _quoted_value(raw, blanks)
    if bash_sourced:
        cut = next((i for i, ch in enumerate(raw) if ch in _BLANKS), len(raw))
        word, after = raw[:cut], raw[cut:]
        # A $, ` or \ before the blank is named below (`a\ b` is one escaped word, not a command).
        if _EXPANDING.isdisjoint(word) and not _rest_is_comment(after, _BLANKS):
            return None, "a blank in an unquoted value: bash runs the rest as a command (quote the value)"
    else:
        word = split(r"\s+#", raw, maxsplit=1)[0].strip()
    why = _unquoted_problem(word, bash_sourced=bash_sourced, exported=exported)
    return (None, why) if why else (word, "")


def _quoted_value(raw: str, blanks: str) -> tuple[str | None, str]:
    """_value for a value in single or double quotes (raw starts with the quote); blanks: what may
    stand before a # comment after the closing quote."""
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


def _unquoted_problem(word: str, *, bash_sourced: bool, exported: bool) -> str:
    """Why an unquoted value is a problem, "" when it is none; the first check that fails names it.
    The last three are bash's own: a file only Python reads keeps those characters."""
    if not _EXPANDING.isdisjoint(word):
        return "$, ` or \\ outside single quotes: bash would expand it (use single quotes)"
    if "'" in word or '"' in word:
        return "a quote inside an unquoted value (quote the whole value)"
    if bash_sourced:
        if not _SHELL_SYNTAX.isdisjoint(word):
            return "shell syntax (; & | < > ( )) in an unquoted value (quote the value)"
        if word.startswith("~") or ":~" in word:
            return "an unquoted ~ that bash would expand to a home folder (quote the value)"
        if exported and "{" in word:
            return "a { in an unquoted value after export: bash brace-expands it (quote the value)"
    return ""


def _assignment(line: str, *, bash_sourced: bool) -> tuple[str, str] | str | None:
    """(key, value), a problem's reason, or None for a blank or comment line."""
    s = line.lstrip(_BLANKS) if bash_sourced else line.strip()
    if not s or s.startswith("#"):
        return None
    if "\r" in line and bash_sourced:
        return "a carriage return (Windows line end): bash keeps it in the value"
    s, exported = _strip_export(s)
    m = _KEY.match(s)
    if not m:
        return _NOT_AN_ASSIGNMENT
    key, rest = m.group(0), s[m.end() :]
    if bash_sourced:
        return _bash_assignment(key, rest, exported=exported)
    return _plain_assignment(key, rest, exported=exported)


def _starts_with_blank(s: str) -> bool:
    """Whether s starts with a blank. Not `s[:1] in _BLANKS`: "" is in every string, so that is
    also True for an empty s."""
    return s != "" and s[0] in _BLANKS


def _strip_export(s: str) -> tuple[str, bool]:
    """s without a leading `export` and the blanks after it, and whether it had one."""
    if s.startswith("export") and _starts_with_blank(s[6:]):
        return s[6:].lstrip(_BLANKS), True
    return s, False


def _bash_assignment(key: str, rest: str, *, exported: bool) -> tuple[str, str] | str:
    """_assignment of a line bash also sources, after its key (rest): no blank around the = sign."""
    if not rest.startswith("="):
        if rest.lstrip(_BLANKS).startswith("="):
            return "blanks around =: bash runs it as a command and sets nothing"
        return _NOT_AN_ASSIGNMENT
    raw = rest[1:]  # "" for `K=`: _value gives its empty value
    if _starts_with_blank(raw):
        if _rest_is_comment(raw, _BLANKS):
            return key, ""
        return "blanks around =: bash runs it as a command and sets nothing"
    value, why = _value(raw, bash_sourced=True, exported=exported)
    return why if value is None else (key, value)


def _plain_assignment(key: str, rest: str, *, exported: bool) -> tuple[str, str] | str:
    """_assignment of a line only Python reads, after its key (rest): blanks around = are fine."""
    rest = rest.lstrip()
    if not rest.startswith("="):
        return _NOT_AN_ASSIGNMENT
    value, why = _value(rest[1:].strip(), bash_sourced=False, exported=exported)
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
_OLD_WRITER_LINE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_PRINTF_Q_WORD: Final = re.compile(r"(?:[^ \t\n\r\f\v'\"\\$`;&|<>()]|\\.)+", re.DOTALL)
# Possessive quantifiers: an escape takes as many octal or hex digits as bash takes (up to
# 3 or 2) and is never split again, so a line with no closing quote fails in linear time
# instead of trying every way to cut each \NNN (exponential backtracking).
_PRINTF_Q_ANSI_C: Final = re.compile(
    r"\$'((?:[^'\\]|\\(?:[abeEfnrtv\\'\"?]|[0-7]{1,3}+|x[0-9A-Fa-f]{1,2}+))*+)'", re.DOTALL
)
_ANSI_C_ESCAPE: Final = re.compile(r"\\(?:([abeEfnrtv\\'\"?])|([0-7]{1,3})|x([0-9A-Fa-f]{1,2}))")
_ANSI_C_CHARS: Final = {
    "a": 7,
    "b": 8,
    "e": 27,
    "E": 27,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
    "\\": 92,
    "'": 39,
    '"': 34,
    "?": 63,
}


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
        if char:
            out.append(_ANSI_C_CHARS[char])
        elif octal:
            out.append(int(octal, 8) & 0xFF)
        else:
            out.append(int(hexa, 16) & 0xFF)
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
    return re.sub(r"\\(.)", lambda e: e.group(1), token, flags=re.DOTALL)


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
        return _ansi_c_quoted(value)
    return _backslashed(value)


def _ansi_c_quoted(value: str) -> str:
    """value in printf %q's $'...' form: bash's name for a control character that has one (\\t,
    \\n, ...), a printable character as it is, any other as the octal escapes of its UTF-8 bytes."""
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


def _backslashed(value: str) -> str:
    """value (all printable) as printf %q writes it: a backslash before each character the shell
    reads as syntax, before a # or ~ that starts it, and before a ~ after : or =."""
    out: list[str] = []
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
