"""Reading small files (sysfs, procfs, state files) the way CST programs need it."""

from typing import overload


@overload
def read_text(path: str) -> str | None: ...


@overload
def read_text(path: str, default: str, *, limit: int = -1) -> str: ...


def read_text(path: str, default: str | None = None, *, limit: int = -1) -> str | None:
    """The file as UTF-8 text (undecodable bytes replaced), at most limit characters; default on OSError."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return default


def read_bytes(path: str, *, limit: int = -1) -> bytes | None:
    """The file's bytes (at most limit), or None on OSError."""
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


def read_int(path: str, default: int | None = None) -> int | None:
    """The integer the file holds (surrounding blanks ignored; a leading minus allowed), else default."""
    text = (read_text(path, "", limit=64)).strip()
    return int(text) if text.lstrip("-").isdigit() else default
