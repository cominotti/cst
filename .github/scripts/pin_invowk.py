#!/usr/bin/env python3
"""Compute the SHA256 pins that .github/actions/setup-invowk needs for an invowk release.

Run it through invowk from the repository root:

    invowk cmd ci pin-invowk v0.17.0

For each Linux architecture CI uses (amd64, arm64) it downloads the release
archive, plus the release's checksums.txt and its Sigstore bundle, into a
temporary directory that is removed on exit. Then it:

1. verifies the cosign keyless signature of checksums.txt, with the same
   certificate identity and issuer the action uses (refuses to go on without
   cosign on PATH, unless --skip-cosign is given, which prints a warning);
2. checks that each archive's SHA256 is the one checksums.txt lists;
3. reads the single `invowk` binary out of each archive and hashes it;
4. prints the `case` lines to paste into the action's pin table.

It only reads from github.com and writes nothing outside the temporary directory.
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

REPO = "invowk/invowk"
ARCHES = ("amd64", "arm64")
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
SHA_RE = re.compile(r"[0-9a-f]{64}")
# setup-invowk finds the binary with `find <dir> -maxdepth 3`, and the archive
# is extracted one level below <dir>, so a member path has at most 3 parts.
MAX_MEMBER_PARTS = 3


class PinError(Exception):
    pass


def certificate_identity(tag: str) -> str:
    return f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/{tag}"


def asset_name(tag: str, arch: str) -> str:
    return f"invowk_{tag.removeprefix('v')}_linux_{arch}.tar.gz"


def download(url: str, dest: Path, attempts: int = 3) -> None:
    if not url.startswith("https://"):
        raise PinError(f"refusing a non-HTTPS URL: {url}")
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cst-pin-invowk"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise PinError(f"{url}: not found (is the tag right?)") from exc
            err = exc
        except (urllib.error.URLError, OSError) as exc:
            err = exc
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise PinError(f"{url}: download failed: {err}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksums(text: str) -> dict:
    """Map file name -> SHA256 from a goreleaser checksums.txt."""
    listed = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*")
        if SHA_RE.fullmatch(digest):
            listed[name] = digest
    return listed


def binary_sha256(archive: Path) -> str:
    """SHA256 of the one regular file named `invowk` in the archive."""
    with tarfile.open(archive, "r:gz") as tar:
        found = []
        for m in tar.getmembers():
            parts = PurePosixPath(m.name).parts
            if parts and parts[0] == ".":
                parts = parts[1:]
            if parts and parts[-1] == "invowk" and len(parts) <= MAX_MEMBER_PARTS and m.isfile():
                found.append(m)
        if len(found) != 1:
            names = ", ".join(m.name for m in found) or "none"
            raise PinError(f"{archive.name}: expected exactly one invowk binary, found {names}")
        fh = tar.extractfile(found[0])
        if fh is None:
            raise PinError(f"{archive.name}: cannot read {found[0].name}")
        h = hashlib.sha256()
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()


def verify_cosign(cosign: str, workdir: Path, tag: str) -> None:
    cmd = [
        cosign, "verify-blob",
        "--bundle", str(workdir / "checksums.txt.sigstore.json"),
        "--certificate-identity", certificate_identity(tag),
        "--certificate-oidc-issuer", OIDC_ISSUER,
        str(workdir / "checksums.txt"),
    ]
    if subprocess.run(cmd, check=False).returncode != 0:
        raise PinError("cosign could not verify checksums.txt; do not pin this release")


def case_line(tag: str, arch: str, archive_sha: str, bin_sha: str) -> str:
    return f'          {tag}/{arch}) pinned="{archive_sha}"; pinned_bin="{bin_sha}" ;;'


def warn_skip_cosign(tag: str) -> None:
    bar = "!" * 76
    print(
        f"{bar}\n"
        f"WARNING: --skip-cosign: the signature of checksums.txt for {tag} was NOT checked.\n"
        "The hashes below only prove that the archives match checksums.txt as served\n"
        "by GitHub right now, not that invowk's release workflow produced them.\n"
        "Verify with cosign before committing pins, or compare with a trusted run.\n"
        f"{bar}",
        file=sys.stderr,
    )


def run(tag: str, skip_cosign: bool) -> list:
    if not TAG_RE.fullmatch(tag):
        raise PinError(f"'{tag}' is not a release tag like v0.16.0")
    cosign = shutil.which("cosign")
    if cosign is None and not skip_cosign:
        raise PinError("cosign is not on PATH; install it, or pass --skip-cosign (invowk: --skip_cosign; not recommended)")

    base = f"https://github.com/{REPO}/releases/download/{tag}"
    with tempfile.TemporaryDirectory(prefix="pin-invowk-") as tmp:
        workdir = Path(tmp)
        for name in ("checksums.txt", "checksums.txt.sigstore.json"):
            download(f"{base}/{name}", workdir / name)
        if skip_cosign:
            warn_skip_cosign(tag)
        else:
            verify_cosign(cosign, workdir, tag)
            print(f"cosign: checksums.txt for {tag} is signed by {certificate_identity(tag)}", file=sys.stderr)

        listed = parse_checksums((workdir / "checksums.txt").read_text(encoding="utf-8"))
        lines = []
        for arch in ARCHES:
            asset = asset_name(tag, arch)
            if asset not in listed:
                raise PinError(f"checksums.txt does not list {asset}")
            path = workdir / asset
            download(f"{base}/{asset}", path)
            archive_sha = sha256_file(path)
            if archive_sha != listed[asset]:
                raise PinError(f"{asset}: SHA256 {archive_sha} differs from checksums.txt ({listed[asset]})")
            bin_sha = binary_sha256(path)
            print(f"{asset}: archive {archive_sha}, binary {bin_sha}", file=sys.stderr)
            lines.append(case_line(tag, arch, archive_sha, bin_sha))
        return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Print setup-invowk SHA256 pins for an invowk release.")
    parser.add_argument("version", help="exact release tag, e.g. v0.16.0")
    parser.add_argument("--skip-cosign", action="store_true",
                        help="do not verify the cosign signature of checksums.txt (prints a warning)")
    args = parser.parse_args(argv)
    try:
        lines = run(args.version, args.skip_cosign)
    except PinError as exc:
        print(f"pin-invowk: error: {exc}", file=sys.stderr)
        return 1
    print("# Paste into the case in .github/actions/setup-invowk/action.yml:")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
