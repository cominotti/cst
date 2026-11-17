"""Offline tests for .github/scripts/pin_invowk.py (no network, no cosign)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from typing import Protocol, cast
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "pin_invowk.py"
ACTION = ROOT / ".github" / "actions" / "setup-invowk" / "action.yml"


class _PinInvowkModule(Protocol):
    PinError: type[Exception]
    shutil: object
    binary_sha256: Callable[[Path], str]
    parse_checksums: Callable[[str], dict[str, str]]
    case_line: Callable[[str, str, str, str], str]

    def download(self, url: str, dest: Path, attempts: int = 3) -> None: ...

    def run(self, tag: str, skip_cosign: bool) -> list[str]: ...


_spec = importlib.util.spec_from_file_location("pin_invowk", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
pin = cast("_PinInvowkModule", cast("object", _mod))


def make_tar(path: Path, members: Mapping[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))


class PinInvowkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pin-invowk-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_download_with_no_attempt_left_fails_with_pin_error(self) -> None:
        # No attempt runs, so nothing is downloaded; the error names the URL.
        with self.assertRaisesRegex(pin.PinError, "download failed"):
            pin.download("https://example.invalid/x", self.tmp / "x", 0)

    def test_binary_sha256_reads_the_single_invowk(self) -> None:
        archive = self.tmp / "a.tar.gz"
        make_tar(archive, {"LICENSE": b"l", "invowk": b"BIN"})
        self.assertEqual(pin.binary_sha256(archive), hashlib.sha256(b"BIN").hexdigest())

    def test_binary_sha256_rejects_two_binaries(self) -> None:
        archive = self.tmp / "a.tar.gz"
        make_tar(archive, {"invowk": b"A", "sub/invowk": b"B"})
        with self.assertRaises(pin.PinError):
            pin.binary_sha256(archive)

    def test_binary_sha256_ignores_too_deep_paths(self) -> None:
        archive = self.tmp / "a.tar.gz"
        make_tar(archive, {"a/b/c/invowk": b"deep"})
        with self.assertRaises(pin.PinError):
            pin.binary_sha256(archive)

    def test_parse_checksums(self) -> None:
        a, b = "a" * 64, "B" * 64
        listed = pin.parse_checksums(f"{a}  one.tar.gz\n{b} *two.tar.gz\njunk\n")
        self.assertEqual(listed, {"one.tar.gz": a, "two.tar.gz": "b" * 64})

    def test_rejects_bad_tag_and_missing_cosign(self) -> None:
        with self.assertRaises(pin.PinError):
            pin.run("0.16.0", skip_cosign=True)
        with mock.patch.object(pin.shutil, "which", return_value=None):
            with self.assertRaisesRegex(pin.PinError, "cosign"):
                pin.run("v0.16.0", skip_cosign=False)

    def test_case_line_matches_the_action_pin_table(self) -> None:
        line = pin.case_line("v0.16.0", "amd64", "1" * 64, "2" * 64)
        self.assertRegex(line, r'^ {10}v0\.16\.0/amd64\) pinned="1{64}"; pinned_bin="2{64}" ;;$')
        # Every pin line in the action has exactly that shape.
        rows = [l for l in ACTION.read_text(encoding="utf-8").splitlines() if 'pinned_bin="' in l and "/" in l]
        self.assertTrue(rows)
        shape = re.compile(
            r'^ {10}(v[0-9.]+[^/]*)/(amd64|arm64)\) pinned="([0-9a-f]{64})"; pinned_bin="([0-9a-f]{64})" ;;$'
        )
        for row in rows:
            m = shape.match(row)
            self.assertIsNotNone(m, row)
            assert m is not None
            self.assertEqual(row, pin.case_line(*m.groups()))


if __name__ == "__main__":
    unittest.main()
