"""Unit tests for btrfs/docker-gc (no root, no Docker engine).

The program is a thin entry over the btrfs.docker_gc package; the tests use
the package through a PackageView, with fakes for docker and the state paths.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest btrfs.tests.test_docker_gc -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_docker_gc.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.docker_gc import adapters as gc_adapters
from btrfs.docker_gc import cli as gc_cli
from btrfs.docker_gc import domain as gc_domain
from tests.package_view import PackageView

sys.dont_write_bytecode = True

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-gc")


def view() -> PackageView:
    return PackageView(gc_cli, gc_adapters, gc_domain)


class DomainTest(unittest.TestCase):
    def test_auto_named_only_for_docker_generated_names(self) -> None:
        self.assertTrue(gc_domain.auto_named("admiring_turing"))
        self.assertTrue(gc_domain.auto_named("admiring_turing2"))  # a collision adds one digit
        self.assertFalse(gc_domain.auto_named("admiring_turing12"))
        self.assertFalse(gc_domain.auto_named("redis_cache"))
        self.assertFalse(gc_domain.auto_named("boring_wozniak"))  # never generated

    def test_ts_reads_docker_time_stamps(self) -> None:
        self.assertEqual(gc_domain.ts("2020-01-02T00:00:00.5Z"), 1577923200.5)
        self.assertEqual(gc_domain.ts("2020-01-02T03:00:00.123456789+03:00"), 1577923200.123456)
        for unset in (None, "", "0001-01-01T00:00:00Z", "yesterday"):
            with self.subTest(value=unset):
                self.assertEqual(gc_domain.ts(unset), 0)

    def test_ephemeral_needs_a_generated_name_or_testcontainers_and_no_owner(self) -> None:
        def c(name: str, labels: dict[str, str] | None = None, restart: str = "no") -> dict[str, Any]:
            return {
                "Name": "/" + name,
                "Config": {"Labels": labels},
                "HostConfig": {"RestartPolicy": {"Name": restart}},
            }

        self.assertTrue(gc_domain.ephemeral(c("admiring_turing")))
        self.assertTrue(gc_domain.ephemeral(c("db", {"org.testcontainers": "true"})))
        self.assertFalse(gc_domain.ephemeral(c("db")))
        self.assertFalse(gc_domain.ephemeral(c("admiring_turing", {"com.docker.compose.project": "x"})))
        self.assertFalse(gc_domain.ephemeral(c("admiring_turing", restart="always")))


class ProgramTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v = view()
        self.addCleanup(self.v.restore)
        self.tmp = tempfile.mkdtemp(prefix="docker-gc-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.v.LAST_USED = os.path.join(self.tmp, "last-used.json")
        self.v.LAST_RUN = os.path.join(self.tmp, "last-run.json")
        self.v.LOCK = os.path.join(self.tmp, "docker-gc.lock")

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["docker-gc", *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = gc_cli.main()
        return rc, out.getvalue(), err.getvalue()

    def test_log_lines_carry_the_priority_and_the_program_name(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gc_adapters.log(5, "kept image x")
        self.assertEqual(out.getvalue(), "<5>docker-gc: kept image x\n")

    def test_a_second_run_while_the_lock_is_held_exits_0(self) -> None:
        fd = gc_adapters.locked()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                gc_adapters.locked()
        finally:
            os.close(fd)
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue(), "<5>docker-gc: another docker-gc run is active; skipped\n")
        os.close(gc_adapters.locked())  # released with the fd: taken again

    def test_unknown_command_is_a_usage_error(self) -> None:
        rc, out, err = self.run_main("bogus")
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(err, "usage: docker-gc record|run [--dry-run]|cleanup-now|status\n")

    def test_a_docker_error_is_logged_and_exits_1(self) -> None:
        def fail(*_args: str, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise RuntimeError("docker info: no answer in 60s")

        self.v.docker = fail
        rc, out, _ = self.run_main("run", "--dry-run")
        self.assertEqual((rc, out), (1, "<3>docker-gc: docker info: no answer in 60s\n"))

    def test_status_without_records_and_without_docker(self) -> None:
        self.v.docker_up = lambda: False
        rc, out, _ = self.run_main("status")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "image use not recorded yet (recorder not started)\nlast run: never\n")

    def test_status_shows_the_last_run_in_decimal_units(self) -> None:
        self.v.docker_up = lambda: False
        with open(gc_cli.LAST_RUN, "w") as f:
            json.dump({"at": 0, "containers": 1, "volumes": 2, "images": 3, "image_bytes": 1234567, "failed": 0}, f)
        _, out, _ = self.run_main("status")
        self.assertRegex(out, r"last run [0-9-]+ [0-9:]+: 1 container\(s\), 2 volume\(s\), 3 image\(s\) \(1\.2 MB\)")

    def test_show_lists_images_by_size_and_marks_the_private_ones(self) -> None:
        plan = {
            "at": 10 * gc_domain.DAY,
            "containers": [],
            "volumes": [],
            "images": [
                {"id": "sha256:aaaaaaaaaaaaaaaaaa", "tags": [], "size": 999, "used": 0, "protected": False},
                {"id": "sha256:b", "tags": ["registry/x:1"], "size": 5_000_000, "used": 0, "protected": True},
            ],
            "held": 0,
            "recorder_ok": True,
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gc_cli.show(plan)
        lines = out.getvalue().splitlines()
        self.assertIn("unused images: 2, up to 5.0 MB (shared layers counted per image)", lines)
        images = [line for line in lines if "last use never" in line]
        self.assertEqual(
            images,
            [
                "     5.0 MB  registry/x:1  (last use never)  [protected registry]",
                "      999 B  <untagged> aaaaaaaaaaaa  (last use never)",
            ],
        )

    def test_is_protected_reads_tags_and_digests(self) -> None:
        self.v.PROTECTED = ("registry.example.com/",)
        self.assertTrue(gc_cli.is_protected({"RepoTags": ["registry.example.com/org/app:1"]}))
        self.assertTrue(gc_cli.is_protected({"RepoTags": None, "RepoDigests": ["registry.example.com/o/a@sha256:1"]}))
        self.assertFalse(gc_cli.is_protected({"RepoTags": ["alpine:latest"]}))


class EntryTest(unittest.TestCase):
    def test_the_program_runs_from_the_checkout(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", "-B", TOOL, "bogus"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            cwd="/",
        )
        self.assertEqual((r.returncode, r.stderr), (2, "usage: docker-gc record|run [--dry-run]|cleanup-now|status\n"))


if __name__ == "__main__":
    unittest.main()
