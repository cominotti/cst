"""Small text rules shared by CST programs."""


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


def parse_assignments(text: str, *, strip_keys: bool = False) -> dict[str, str]:
    """The KEY=VALUE lines of a shell settings file, {KEY: value}.

    Quotes around the value are removed and `#` comment lines skipped; a later
    assignment wins, as in the shell. A line with a blank before `=` is not an
    assignment in the shell either: its key keeps the blank, so no lookup
    finds it, unless strip_keys (vm.conf, which only Python reads, has always
    allowed `VM_NOCOW = 0`); the last line still wins then. Lines as
    file_lines reads them.
    """
    vals: dict[str, str] = {}
    for line in file_lines(text):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            vals[k.strip() if strip_keys else k] = v.strip().strip("'\"")
    return vals
