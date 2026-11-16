"""Small text rules shared by CST programs."""


def first_line(text: str | None) -> str:
    """The first non-blank line of text, stripped ("" when there is none)."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def is_yes(answer: str) -> bool:
    """A [y/N] prompt's answer is yes: it starts with y or s (sim), any case."""
    return answer.strip().lower().startswith(("y", "s"))


def parse_assignments(text: str) -> dict[str, str]:
    """The KEY=VALUE lines of a shell settings file, {KEY: value}.

    Quotes around the value are removed and `#` comment lines skipped; a later
    assignment wins, as in the shell. A line with a blank before `=` is not an
    assignment in the shell either: its key keeps the blank, so no lookup
    finds it.
    """
    vals: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            vals[k] = v.strip().strip("'\"")
    return vals
