#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""The content digest of an E2E image's sources: what the image label org.cst.e2e.sources holds.

An image is current when its label equals the digest of the checkout's sources: its Dockerfile
(tests/e2e/images/NAME.Dockerfile), every file under tests/e2e/scripts (path, exec bit and bytes:
the Dockerfiles copy from there) and, for an image built FROM another E2E image (priv FROM
systemd), that image's digest. tests/e2e/run.py rebuilds an image whose label is missing or
differs; .github/workflows/e2e-images.yml writes the label when it builds the cache images, and
.github/actions/prep-e2e-runner says when a pulled one differs.

Use: python3 -I -B tests/e2e/image_digest.py NAME   (prints the digest; NAME: systemd, nas, priv)

Standard library only: the images job runs it with the runner's python3, before anything else.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys

LABEL = "org.cst.e2e.sources"
IMAGES = ("systemd", "nas", "priv")
# The images built FROM another E2E image (FROM cst-e2e-BASE:local).
BASES = {"priv": "systemd"}
_EXEC = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def image_digest(name: str, repo: Path) -> str:
    """The sha256 (hex) of image NAME's sources in the checkout REPO."""
    if name not in IMAGES:
        raise ValueError(f"no E2E image {name!r} (known: {', '.join(IMAGES)})")
    e2e = repo / "tests" / "e2e"
    h = hashlib.sha256()
    h.update(f"dockerfile\0{name}\0".encode())
    h.update((e2e / "images" / f"{name}.Dockerfile").read_bytes())
    scripts = e2e / "scripts"
    for path in sorted(p for p in scripts.rglob("*") if p.is_file()):
        rel = path.relative_to(repo).as_posix()
        mode = "x" if os.stat(path).st_mode & _EXEC else "-"
        data = path.read_bytes()
        h.update(f"\0file\0{rel}\0{mode}\0{len(data)}\0".encode())
        h.update(data)
    base = BASES.get(name)
    if base is not None:
        h.update(f"\0base\0{base}\0{image_digest(base, repo)}".encode())
    return h.hexdigest()


def main(argv: list[str]) -> int:
    """Print the digest of the image argv names (exit 2 for a usage error)."""
    if len(argv) != 1 or argv[0] not in IMAGES:
        print(f"usage: image_digest.py {'|'.join(IMAGES)}", file=sys.stderr)
        return 2
    print(image_digest(argv[0], Path(__file__).resolve().parents[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
