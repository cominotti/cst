"""Build the self-contained zipapp that `install` writes for a python-entry.

The archive holds the entry as `__main__.py`, every repository package it
imports (directly or through another package: the static import closure).
It is uncompressed
and every member has a fixed timestamp, so the same sources always give the
same bytes, whichever Python builds it.
"""

import io
import os
from pathlib import Path
import zipfile

from cst_core.domain.imports import top_level_packages
from cst_core.domain.install import InstallEntry

ZIP_FIXED_TIME = (2026, 1, 1, 0, 0, 0)
ZIPAPP_SHEBANG = b"#!/usr/bin/python3 -I\n"
# Folders inside a package that never ship.
_SKIPPED_PARTS = frozenset({"tests", "__pycache__"})


def repo_packages(root: Path) -> frozenset[str]:
    """Top-level folders of the checkout that are Python packages (tests excluded)."""
    return frozenset(
        p.name for p in root.iterdir() if p.is_dir() and p.name != "tests" and (p / "__init__.py").is_file()
    )


def package_sources(root: Path, package: str) -> list[Path]:
    """The package's .py files that ship (no tests, no caches), sorted."""
    return [
        path
        for path in sorted((root / package).rglob("*.py"))
        if not _SKIPPED_PARTS.intersection(path.relative_to(root).parts)
    ]


def import_closure(root: Path, entry_source: str, filename: str = "<entry>") -> frozenset[str]:
    """The repository packages the entry needs: those it imports, and theirs."""
    known = repo_packages(root)
    todo = sorted(top_level_packages(entry_source, filename) & known)
    seen: set[str] = set()
    while todo:
        pkg = todo.pop()
        if pkg in seen:
            continue
        seen.add(pkg)
        for path in package_sources(root, pkg):
            todo.extend(sorted((top_level_packages(path.read_text(encoding="utf-8"), str(path)) & known) - seen))
    return frozenset(seen)


def zipapp_members(root: Path, src_rel: str) -> dict[str, bytes]:
    """{archive name: bytes} of the zipapp for the entry at `src_rel`."""
    entry_path = root / src_rel
    if not entry_path.is_file():
        raise FileNotFoundError(f"zipapp source not found: {entry_path}")
    raw_entry = entry_path.read_bytes()
    members: dict[str, bytes] = {"__main__.py": raw_entry}
    for pkg in sorted(import_closure(root, raw_entry.decode("utf-8"), str(entry_path))):
        for path in package_sources(root, pkg):
            members[path.relative_to(root).as_posix()] = path.read_bytes()
    return members


def build_python_zipapp(repo_root: str | os.PathLike[str], src_rel: str) -> bytes:
    """The deterministic zipapp bytes for one python-entry.

    The shebang runs it on the system Python in isolated mode; the entry's
    `# managed-by:` line (when it has one in its first lines) is copied
    after the shebang, so `grep` still finds who installed the file.
    """
    root = Path(repo_root).resolve()
    members = zipapp_members(root, src_rel.strip("/"))
    preamble = ZIPAPP_SHEBANG
    for line in members["__main__.py"].splitlines(keepends=True)[1:4]:
        if line.startswith(b"# managed-by:"):
            preamble += line if line.endswith(b"\n") else line + b"\n"
            break
    buf = io.BytesIO()
    buf.write(preamble)
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(filename=name, date_time=ZIP_FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            zf.writestr(info, members[name])
    return buf.getvalue()


def entry_payload(repo_root: str | os.PathLike[str], entry: InstallEntry) -> bytes:
    """The bytes `install` writes for a non-template entry: a zipapp, or the file as it is."""
    if entry.kind == "python-entry":
        return build_python_zipapp(repo_root, entry.src)
    return (Path(repo_root) / entry.src).read_bytes()
