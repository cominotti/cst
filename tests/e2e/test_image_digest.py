# SPDX-License-Identifier: MPL-2.0
"""tests/e2e/image_digest.py and run.py's docker_build: an image is used only when its sources label is
the checkout's digest. No Docker: run.py's run is replaced by a fake that answers `docker image inspect`
and records every command."""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import ModuleType
from typing import override
import unittest
from unittest import mock

E2E = os.path.dirname(os.path.abspath(__file__))


def _load(name: str, file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, os.path.join(E2E, file))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ImageDigestTest(unittest.TestCase):
    """The digest follows every source of an image and nothing else."""

    @override
    def setUp(self) -> None:
        self.mod = _load("cst_e2e_image_digest", "image_digest.py")
        self.repo = Path(tempfile.mkdtemp(prefix="cst-e2e-digest-"))
        self.addCleanup(shutil.rmtree, self.repo)
        for name in ("systemd", "nas", "priv"):
            self.put(f"tests/e2e/images/{name}.Dockerfile", f"FROM x\n# {name}\n")
        self.put("tests/e2e/scripts/entry.sh", "#!/bin/sh\n", mode=0o755)

    def put(self, rel: str, text: str, mode: int = 0o644) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, mode)

    def digests(self) -> dict[str, str]:
        found = {n: vars(self.mod)["image_digest"](n, self.repo) for n in ("systemd", "nas", "priv")}
        self.assertTrue(all(isinstance(d, str) for d in found.values()), found)
        return {n: str(d) for n, d in found.items()}

    def test_each_source_changes_the_digest_of_the_images_built_from_it(self) -> None:
        before = self.digests()
        self.assertEqual(len(set(before.values())), 3)
        self.assertEqual(self.digests(), before, "the digest is not stable")
        self.put("tests/e2e/images/systemd.Dockerfile", "FROM x\n# systemd, changed\n")
        after = self.digests()
        # priv is built FROM systemd: it changes with it; nas does not.
        self.assertEqual([n for n in before if before[n] != after[n]], ["systemd", "priv"])
        for change in (
            lambda: self.put("tests/e2e/scripts/entry.sh", "#!/bin/sh\nexit 0\n", mode=0o755),
            lambda: os.chmod(self.repo / "tests/e2e/scripts/entry.sh", 0o644),
            lambda: self.put("tests/e2e/scripts/new.sh", "x\n"),
        ):
            with self.subTest(change=change):
                was = self.digests()
                change()
                now = self.digests()
                self.assertTrue(all(was[n] != now[n] for n in was), (was, now))
        was = self.digests()
        self.put("tests/e2e/suites/suite_x.py", "x\n")  # not a source of any image
        self.assertEqual(self.digests(), was)

    def test_the_command_line(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(vars(self.mod)["main"](["nope"]), 2)
            self.assertEqual(vars(self.mod)["main"](["nas"]), 0)
        self.assertEqual(err.getvalue(), "usage: image_digest.py systemd|nas|priv\n")
        self.assertRegex(out.getvalue(), r"^[0-9a-f]{64}\n$")


class DockerBuildTest(unittest.TestCase):
    """run.py's docker_build: the local image is used when its label is the digest; built (with the
    label) when it is missing, has no label or another one, or with --rebuild."""

    @override
    def setUp(self) -> None:
        self.mod = _load("cst_e2e_run_for_build", "run.py")
        self.calls: list[list[str]] = []
        self.label: str | None = None  # None: no image

        def fake_run(cmd: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(cmd))
            if list(cmd[:3]) == ["docker", "image", "inspect"]:
                if self.label is None:
                    return subprocess.CompletedProcess(list(cmd), 1, "", "No such image\n")
                return subprocess.CompletedProcess(list(cmd), 0, (self.label or "<no value>") + "\n", "")
            return subprocess.CompletedProcess(list(cmd), 0, "", "")

        vars(self.mod)["run"] = fake_run
        self.env = mock.patch.dict(os.environ, {"CST_E2E_BUILD_CACHE_FROM": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def build(self, *, force: bool = False) -> tuple[list[list[str]], str]:
        self.calls.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            vars(self.mod)["docker_build"]("img:local", Path("/x/img.Dockerfile"), digest="d1", force=force)
        return [c for c in self.calls if c[:2] == ["docker", "build"]], out.getvalue()

    def test_an_image_with_the_digest_is_used(self) -> None:
        self.label = "d1"
        builds, out = self.build()
        self.assertEqual(builds, [])
        self.assertIn("using cached image img:local", out)

    def test_an_image_without_the_digest_is_rebuilt_with_it(self) -> None:
        for label, why in ((None, "no image"), ("", "no sources label"), ("d0", "other sources")):
            with self.subTest(label=label):
                self.label = label
                builds, out = self.build()
                self.assertIn(f"img:local: {why}: rebuilding", out)
                self.assertEqual(len(builds), 1)
                self.assertEqual(builds[0][:4], ["docker", "build", "--label", "org.cst.e2e.sources=d1"])
                self.assertEqual(builds[0][-5:], ["-t", "img:local", "-f", "/x/img.Dockerfile", "."])

    def test_rebuild_builds_without_asking(self) -> None:
        self.label = "d1"
        builds, _ = self.build(force=True)
        self.assertEqual(len(builds), 1)
        self.assertFalse([c for c in self.calls if c[:3] == ["docker", "image", "inspect"]])
