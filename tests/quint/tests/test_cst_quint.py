# SPDX-License-Identifier: MPL-2.0
"""Unit tests for tests/quint/cst-quint, the `invowk cmd test quint` helper.

Standard library only: no node, npm, java, curl or network. A fake runner stands in for every
command: curl writes an archive the test made, npm ci writes a quint program, quint answers its
version and the verdicts the test gives. Every run works in a temporary folder of the test's own
(a scratch checkout and cache), never in the real checkout or the developer's home.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest tests.quint.tests.test_cst_quint -v
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import dataclasses
import hashlib
import io
import json
import os
import pwd
import re
import subprocess
import sys
import tarfile
import tempfile
from types import ModuleType
from typing import override
import unittest
from unittest import mock

# Run directly (python3 -I -B tests/quint/tests/test_cst_quint.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from tests.script_loader import load_script
from tests.xdg_cache import NO_CACHE_BASE

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "cst-quint")


def load() -> ModuleType:
    # The helper has no .py extension, so it is loaded from its path.
    return load_script(TOOL, "cst_quint")


M = load()

OK = "261510 states generated, 63344 distinct states found, 0 states left on queue.\n"
OK += "The depth of the complete state graph search is 68.\n\n[ok] No violation found (1340ms).\n"
VIOLATED = "State 28: ...\n100048 states generated, 25520 distinct states found, 1926 states left on queue.\n"
VIOLATED += "The depth of the complete state graph search is 28.\n\n[violation] Found an issue (984ms).\n"
VIOLATED += "error: found a counterexample\n"
TRACE = {
    "#meta": {
        "format": "ITF",
        "format-description": "https://apalache-mc.org/docs/adr/015adr-trace.html",
        "source": "/home/someone/checkout/spec.qnt",
        "status": "ok",
        "description": "Created by Quint on Tue Nov 24 2026",
        "timestamp": 1795537680,
    },
    "vars": ["s", "step"],
    "states": [{"#meta": {"index": 0}, "s": {"#bigint": "0"}}, {"#meta": {"index": 1}, "s": {"#bigint": "1"}}],
}


def archive(folder: str, members: Mapping[str, bytes]) -> tuple[str, str]:
    """A .tar.gz in folder holding members; its path and SHA-256."""
    path = os.path.join(folder, "tool.tgz")
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    with open(path, "rb") as f:
        return path, hashlib.sha256(f.read()).hexdigest()


class FakeRun:
    """The runner: records each command and plays its part."""

    def __init__(self, archives: Mapping[str, str]) -> None:
        self.archives = archives  # url -> the archive curl "downloads"
        self.calls: list[tuple[list[str], str, dict[str, str]]] = []
        self.verdicts: dict[str, tuple[int, str]] = {}  # instance -> (exit status, output)
        self.version = "0.33.1"
        self.replay_status = 0
        self.trace = json.dumps(TRACE)
        self.witness = (1, "[violation] Found an issue (412ms).\nerror: found a counterexample\n")
        self.walk_status = 0

    def __call__(self, argv: Sequence[str], cwd: str, env: Mapping[str, str]) -> object:
        args = list(argv)
        self.calls.append((args, cwd, dict(env)))
        if "curl" in args:
            url, out = args[-1], args[args.index("-o") + 1]
            with open(self.archives[url], "rb") as src, open(out, "wb") as dst:
                dst.write(src.read())
            return M.Ran(0, "", 1000, 0.1)
        if "npm" in args:
            os.makedirs(os.path.join(cwd, "node_modules", ".bin"))
            with open(os.path.join(cwd, "node_modules", ".bin", "quint"), "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")
            return M.Ran(0, "added 79 packages\n", 1000, 2.0)
        if args[-1] == "--version":
            return M.Ran(0, self.version + "\n", 1000, 0.2)
        if "tests/run_unit.py" in args:
            return M.Ran(self.replay_status, "Ran 2 tests\n", 1000, 0.5)
        if "--out-itf" in args:
            with open(args[args.index("--out-itf") + 1], "w", encoding="utf-8") as f:
                f.write(self.trace)
            # A witness's run ends with a violation of its negation; a walk's with walk_status.
            code, out = self.witness if "--invariant" in args else (self.walk_status, "")
            return M.Ran(code, out, 200_000, 1.5)
        main = args[args.index("--main") + 1]
        code, out = self.verdicts.get(main, (0, OK))
        return M.Ran(code, out, 900_000, 9.4)

    def commands(self, word: str) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if word in argv]


class Base(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-quint-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.checkout = os.path.join(self.tmp, "checkout")
        self.home = os.path.join(self.tmp, "home")
        self.cache = os.path.join(self.home, ".cache", "cst-quint")
        os.makedirs(os.path.join(self.checkout, "tests", "quint"))
        os.makedirs(os.path.join(self.checkout, "specs"))
        with open(os.path.join(self.checkout, "specs", "s.qnt"), "w", encoding="utf-8") as f:
            f.write("module s {}\n")
        for name, text in (("package.json", '{"name": "x"}\n'), ("package-lock.json", '{"lockfileVersion": 3}\n')):
            with open(os.path.join(self.checkout, "tests", "quint", name), "w", encoding="utf-8") as f:
                f.write(text)
        downloads = os.path.join(self.tmp, "downloads")
        os.makedirs(os.path.join(downloads, "a"))
        os.makedirs(os.path.join(downloads, "e"))
        a_path, a_sha = archive(os.path.join(downloads, "a"), {"apalache/lib/apalache.jar": b"jar"})
        e_path, e_sha = archive(os.path.join(downloads, "e"), {"quint_evaluator": b"\x7fELF"})
        self.tools = (
            M.Tool(
                url="https://example.invalid/apalache.tgz",
                sha256=a_sha,
                folder="apalache-x",
                marker="apalache/lib/apalache.jar",
            ),
            M.Tool(
                url="https://example.invalid/evaluator.tgz",
                sha256=e_sha,
                folder="evaluator-x",
                marker="quint_evaluator",
            ),
        )
        self.enterContext(mock.patch.object(M, "TOOLS", self.tools))
        self.run_ = FakeRun({self.tools[0].url: a_path, self.tools[1].url: e_path})
        self.lines: list[str] = []
        self.checks = (
            M.Check(spec="specs/s.qnt", main="inst_a", invariants=("p", "q")),
            M.Check(spec="specs/s.qnt", main="inst_b", invariants=("p", "q")),
        )

    def main(self, *argv: str, which: Callable[[str], str | None] | None = None, **kw: object) -> int:
        seams = M.Seams(
            checkout=self.checkout,
            env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            run=self.run_,
            which=which or (lambda name: "/usr/bin/" + name),
            out=self.lines.append,
            checks=self.checks,
        )
        rc = M.main(list(argv), dataclasses.replace(seams, **kw))
        self.assertIsInstance(rc, int)
        return int(rc)

    def output(self) -> str:
        return "\n".join(self.lines)


class FetchTest(Base):
    def test_a_download_that_matches_is_unpacked_and_stamped_then_reused(self) -> None:
        home = os.path.join(self.cache, "quint-home")
        dest = M.fetch(self.tools[0], home, self.run_, {})
        self.assertEqual(dest, os.path.join(home, "apalache-x"))
        with open(os.path.join(dest, "apalache", "lib", "apalache.jar"), "rb") as f:
            self.assertEqual(f.read(), b"jar")
        with open(os.path.join(dest, M.STAMP), encoding="utf-8") as f:
            self.assertEqual(f.read(), self.tools[0].sha256 + "\n")
        self.assertEqual(sorted(os.listdir(home)), ["apalache-x"])  # no download or staging folder left
        M.fetch(self.tools[0], home, self.run_, {})
        self.assertEqual(len(self.run_.commands("curl")), 1, "a stamped tool is used as it is")

    def test_a_download_that_does_not_match_is_removed_and_names_both_hashes(self) -> None:
        home = os.path.join(self.cache, "quint-home")
        wrong = M.Tool(url=self.tools[0].url, sha256="0" * 64, folder="apalache-x", marker="apalache/lib/apalache.jar")
        with self.assertRaises(M.StepError) as caught:
            M.fetch(wrong, home, self.run_, {})
        self.assertEqual(
            str(caught.exception),
            f"{wrong.url}: SHA-256 {self.tools[0].sha256}, want {'0' * 64}: the download was removed",
        )
        self.assertEqual(os.listdir(home), [])

    def test_a_folder_without_the_checked_hash_is_fetched_again(self) -> None:
        home = os.path.join(self.cache, "quint-home")
        dest = os.path.join(home, "apalache-x", "apalache", "lib")
        os.makedirs(dest)
        with open(os.path.join(dest, "apalache.jar"), "wb") as f:
            f.write(b"something else")  # no stamp: not what was checked
        M.fetch(self.tools[0], home, self.run_, {})
        self.assertEqual(len(self.run_.commands("curl")), 1)
        with open(os.path.join(dest, "apalache.jar"), "rb") as f:
            self.assertEqual(f.read(), b"jar")

    def test_a_failed_download_stops_with_its_exit_status(self) -> None:
        def failing(argv: Sequence[str], cwd: str, env: Mapping[str, str]) -> object:
            return M.Ran(22, "curl: (22) The requested URL returned error: 404\n", 0, 0.1)

        home = os.path.join(self.cache, "quint-home")
        with self.assertRaises(M.StepError) as caught:
            M.fetch(self.tools[0], home, failing, {})
        self.assertIn("download failed (exit 22): curl: (22)", str(caught.exception))
        self.assertEqual(os.listdir(home), [])


class InstallQuintTest(Base):
    def test_npm_ci_without_scripts_in_a_folder_named_after_the_lock(self) -> None:
        pin = os.path.join(self.checkout, "tests", "quint")
        program = M.install_quint(self.cache, pin, self.run_, {"HOME": self.home})
        with open(os.path.join(pin, "package-lock.json"), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        prefix = os.path.join(self.cache, f"npm-{digest[:16]}")
        self.assertEqual(program, os.path.join(prefix, "node_modules", ".bin", "quint"))
        [(argv, cwd, env)] = self.run_.calls
        self.assertEqual(argv, ["timeout", "600", "npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"])
        self.assertTrue(os.path.basename(cwd).startswith(".npm-"), cwd)  # a staging folder, moved in place after
        self.assertEqual(env["npm_config_cache"], os.path.join(self.cache, "npm-cache"))
        self.assertEqual(sorted(os.listdir(prefix)), [M.STAMP, "node_modules", "package-lock.json", "package.json"])
        M.install_quint(self.cache, pin, self.run_, {})
        self.assertEqual(len(self.run_.calls), 1, "installed once")


class ArgvTest(unittest.TestCase):
    def test_tlc_runs_with_its_config_under_nice_and_a_time_limit(self) -> None:
        check = M.Check(spec="s.qnt", main="nocow_ab", invariants=("a", "b"))
        self.assertEqual(
            M.tlc_argv("quint", "/x/s.qnt", check, "/w/tlc.json"),
            [
                "timeout",
                "300",
                "nice",
                "-n",
                "10",
                "quint",
                "verify",
                "/x/s.qnt",
                "--backend",
                "tlc",
                "--tlc-config",
                "/w/tlc.json",
                "--main",
                "nocow_ab",
                "--step",
                "step_",
                "--invariant",
                "a,b",
            ],
        )
        self.assertEqual(M.TLC_CONFIG, {"maxHeap": "-Xmx4g", "workers": 4})
        env = M.quint_env({"PATH": "/usr/bin"}, "/c/quint-home", "/c/work/run-1")
        self.assertEqual(
            env, {"PATH": "/usr/bin", "QUINT_HOME": "/c/quint-home", "JVM_ARGS": "-Xmx4g", "TMPDIR": "/c/work/run-1"}
        )

    def test_a_simulator_trace_uses_one_thread_and_its_seed(self) -> None:
        job = M.TraceJob(name="walk", spec="s.qnt", main="m", backend="simulator", max_steps=80, seed=11, samples=3)
        argv = M.trace_argv("quint", "/x/s.qnt", job, "/w/walk.itf.json")
        self.assertEqual(argv[:8], ["timeout", "300", "nice", "-n", "10", "quint", "run", "/x/s.qnt"])
        for pair in (
            ("--n-threads", "1"),
            ("--seed", "11"),
            ("--max-samples", "3"),
            ("--max-steps", "80"),
            ("--out-itf", "/w/walk.itf.json"),
            ("--main", "m"),
            ("--n-traces", "1"),
        ):
            self.assertEqual(argv[argv.index(pair[0]) + 1], pair[1], pair)
        self.assertNotIn("--invariant", argv)

    def test_an_apalache_witness_negates_the_invariant(self) -> None:
        job = M.TraceJob(name="w", spec="s.qnt", main="m", backend="apalache", max_steps=18, invariant="neverX")
        argv = M.trace_argv("quint", "/x/s.qnt", job, "/w/w.itf.json")
        self.assertEqual(argv[:8], ["timeout", "600", "nice", "-n", "10", "quint", "verify", "/x/s.qnt"])
        self.assertEqual(argv[-2:], ["--invariant", "neverX"])
        self.assertNotIn("--backend", argv)
        self.assertNotIn("--seed", argv)


# Names the standard library of Python 3.12 lacks, which cst-quint and what it imports must not need: the quint
# job of .github/workflows/ci.yml runs it on the ubuntu-24.04 runner's own python3 (3.12).
_SIMULATE_312 = """
import os, runpy, sys, typing
del typing.TypeIs
del os.process_cpu_count
sys.argv = [sys.argv[1], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


class HostPythonTest(unittest.TestCase):
    def test_help_runs_without_the_names_python_3_12_lacks(self) -> None:
        ran = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _SIMULATE_312, TOOL],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("usage", ran.stdout.lower())


class CheckoutTest(unittest.TestCase):
    def test_run_by_a_link_it_finds_the_checkout_of_its_real_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-quint-test-") as tmp:
            link = os.path.join(tmp, "bin", "cst-quint")
            os.makedirs(os.path.dirname(link))
            os.symlink(TOOL, link)
            self.addCleanup(sys.modules.pop, "cst_quint_by_link", None)
            linked = load_script(link, "cst_quint_by_link")
            self.assertEqual(linked.checkout_root(), os.path.realpath(_CHECKOUT))
            self.assertEqual(M.checkout_root(), os.path.realpath(_CHECKOUT))


class ExecuteTest(unittest.TestCase):
    def test_output_that_is_not_utf8_is_read_with_replacement_characters(self) -> None:
        # A child (curl, npm, the JVM, the replay) may print bytes that are not UTF-8: a path or a
        # message in Latin-1. The run still gives its verdict, as cst_core's runner would.
        with tempfile.TemporaryDirectory(prefix="cst-quint-test-") as cwd:
            script = "import sys; sys.stdout.buffer.write(b'ok \\xff done\\n'); sys.exit(3)"
            ran = M.execute([sys.executable, "-I", "-B", "-c", script], cwd, {"PATH": "/usr/bin:/bin"})
            self.assertEqual((ran.returncode, ran.output), (3, "ok \N{REPLACEMENT CHARACTER} done\n"))
            self.assertEqual(os.listdir(cwd), [], "the log file is gone")


class TraceJobsTest(unittest.TestCase):
    def test_the_checked_in_traces_are_the_jobs_and_come_from_both_backends(self) -> None:
        # Each checked-in trace is one job's, written by that job's backend (its description says
        # which), and both backends the help names write some.
        self.assertEqual({job.backend for job in M.TRACE_JOBS}, {"simulator", "apalache"})
        for spec, folder in M.TRACES_DIR.items():
            jobs = {job.name: job for job in M.TRACE_JOBS if job.spec == spec}
            files = sorted(f for f in os.listdir(os.path.join(_CHECKOUT, folder)) if f.endswith(".itf.json"))
            self.assertEqual(files, sorted(f"{name}.itf.json" for name in jobs))
            for name, job in jobs.items():
                with open(os.path.join(_CHECKOUT, folder, f"{name}.itf.json"), encoding="utf-8") as f:
                    meta = json.load(f)["#meta"]
                self.assertEqual(meta["description"], f"{job.backend} trace {name} of {spec} ({job.main})")


class VerdictTest(unittest.TestCase):
    def test_each_answer(self) -> None:
        cases = {
            (0, OK): ("ok", 63344, 68),
            (1, VIOLATED): ("violated", 25520, 28),
            (124, "[TLC] Running TLC model checker...\n"): ("timed out", None, None),
            (1, "error: could not connect\n"): ("failed", None, None),
            (0, "nothing said\n"): ("failed", None, None),
            (
                0,
                "Progress(25) at 2026-11-24: 4821 states generated, 861 distinct states found, 77 states left\n" + OK,
            ): (
                "ok",
                63344,
                68,
            ),
        }
        for (code, out), want in cases.items():
            with self.subTest(code=code, out=out):
                v = M.verdict(M.Ran(code, out, 0, 0.0))
                self.assertEqual((v.result, v.states, v.depth), want)


class NormalizeTest(unittest.TestCase):
    def test_no_path_or_time_one_state_per_line_and_the_same_bytes_twice(self) -> None:
        text = M.normalize(json.dumps(TRACE), "simulator trace walk of s.qnt (m)")
        self.assertNotIn("/home/", text)
        self.assertNotIn("timestamp", text)
        self.assertNotIn("Created by Quint", text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "{")
        self.assertEqual(
            lines[1],
            '"#meta": {"description": "simulator trace walk of s.qnt (m)", "format": "ITF", '
            '"format-description": "https://apalache-mc.org/docs/adr/015adr-trace.html"},',
        )
        self.assertEqual(lines[2], '"vars": ["s", "step"],')
        self.assertEqual(
            lines[3:],
            [
                '"states": [',
                '{"#meta": {"index": 0}, "s": {"#bigint": "0"}},',
                '{"#meta": {"index": 1}, "s": {"#bigint": "1"}}',
                "]",
                "}",
            ],
        )
        parsed = json.loads(text)
        self.assertEqual(parsed["states"], TRACE["states"])
        self.assertEqual(M.normalize(json.dumps(TRACE), "simulator trace walk of s.qnt (m)"), text)
        self.assertEqual(M.normalize(text, "simulator trace walk of s.qnt (m)"), text)

    def test_not_a_trace(self) -> None:
        with self.assertRaises(M.StepError):
            M.normalize("[]", "x")


class ChecksTest(unittest.TestCase):
    """The checks cst-quint runs name real files and modules: a typo fails here, not only in CI's quint job."""

    def test_every_check_is_a_module_of_its_spec(self) -> None:
        self.assertEqual({c.spec for c in M.CHECKS}, {M.NOCOW_SPEC, M.NAS_SPEC})
        for check in M.CHECKS:
            with self.subTest(check=check.main):
                with open(os.path.join(_CHECKOUT, check.spec), encoding="utf-8") as f:
                    modules = re.findall(r"^module (\w+) \{", f.read(), re.MULTILINE)
                self.assertIn(check.main, modules)
                self.assertTrue(check.invariants)

    def test_the_nas_checks_name_the_full_properties_and_the_instance_without_atomic(self) -> None:
        nas = [c for c in M.CHECKS if c.spec == M.NAS_SPEC]
        self.assertIn("nas_partial", [c.main for c in nas])
        for check in nas:
            with self.subTest(check=check.main):
                for invariant in ("tornHandledFull", "goodKeptFull", "tornHandled", "goodKept", "consistentRead"):
                    self.assertIn(invariant, check.invariants)

    def test_every_spec_has_its_traces_folder_and_its_replay(self) -> None:
        specs = {c.spec for c in M.CHECKS} | {job.spec for job in M.TRACE_JOBS}
        self.assertEqual(set(M.TRACES_DIR), specs)
        self.assertEqual(set(M.REPLAYS), specs)
        for spec, module in M.REPLAYS.items():
            with self.subTest(spec=spec):
                self.assertTrue(os.path.isfile(os.path.join(_CHECKOUT, *module.split(".")) + ".py"), module)
        names = [(job.spec, job.name) for job in M.TRACE_JOBS]
        self.assertEqual(len(names), len(set(names)), "two trace jobs of one spec write one file")

    def test_a_check_of_a_missing_module_is_found(self) -> None:
        # The test above reads the spec's modules with this pattern: it finds a module that is there, and
        # no other.
        modules = re.findall(
            r"^module (\w+) \{", "module nas {\n}\nmodule nas_a {\n  // module nas_b {\n}\n", re.MULTILINE
        )
        self.assertEqual(modules, ["nas", "nas_a"])


class CacheRootTest(Base):
    def test_a_relative_xdg_cache_home_and_an_empty_home_are_no_cache_folder(self) -> None:
        # XDG_CACHE_HOME=rel was used as it is (rel/cst-quint, inside the checkout the task runs from), and an
        # empty HOME fell to os.path.expanduser, which read os.environ's HOME: "" gave /.cache/cst-quint.
        self.assertEqual(M.cache_root({"XDG_CACHE_HOME": "/c", "HOME": "/h"}), "/c/cst-quint")
        self.assertEqual(M.cache_root({"XDG_CACHE_HOME": "rel", "HOME": "/h"}), "/h/.cache/cst-quint")
        entry = pwd.struct_passwd(("u", "x", 1000, 1000, "", "/pw/home", "/bin/sh"))
        with (
            mock.patch.dict(os.environ, {"HOME": ""}),
            mock.patch("tests.xdg_cache.pwd.getpwuid", return_value=entry),
        ):
            self.assertEqual(M.cache_root({"HOME": ""}), "/pw/home/.cache/cst-quint")

    def test_no_cache_folder_is_a_usage_error_that_runs_nothing(self) -> None:
        env = {"XDG_CACHE_HOME": "rel", "HOME": "", "PATH": "/usr/bin:/bin"}
        with mock.patch("tests.xdg_cache.pwd.getpwuid", side_effect=KeyError("no entry")):
            self.assertEqual(self.main(env=env), 2)
        self.assertIn(f"cst-quint: {NO_CACHE_BASE}", self.lines)
        self.assertEqual(self.run_.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.checkout, "rel")))


class MainTest(Base):
    def test_every_instance_holds(self) -> None:
        self.assertEqual(self.main(), 0)
        tlc = self.run_.commands("tlc")
        self.assertEqual([argv[argv.index("--main") + 1] for argv in tlc], ["inst_a", "inst_b"])
        argv, cwd, env = next(c for c in self.run_.calls if "tlc" in c[0])
        self.assertEqual(argv[argv.index("verify") + 1], os.path.join(self.checkout, "specs", "s.qnt"))
        self.assertTrue(cwd.startswith(os.path.join(self.cache, "work") + os.sep), cwd)
        self.assertEqual(env["QUINT_HOME"], os.path.join(self.cache, "quint-home"))
        self.assertEqual((env["JVM_ARGS"], env["TMPDIR"]), ("-Xmx4g", cwd))
        self.assertIn("inst_a: ok, 63344 states, depth 68, 9.4 s, largest RSS 878.9 MiB (p, q)", self.lines)
        self.assertIn("2 instance(s) in 18.8 s, largest RSS of any run 878.9 MiB", self.lines)
        self.assertEqual(os.listdir(os.path.join(self.cache, "work")), [], "the work folder is removed")
        self.assertEqual(sorted(os.listdir(self.checkout)), ["specs", "tests"], "nothing written into the checkout")

    def test_a_violation_fails_and_prints_the_counterexample(self) -> None:
        self.run_.verdicts["inst_b"] = (1, VIOLATED)
        self.assertEqual(self.main(), 1)
        self.assertIn("inst_b: violated, 25520 states, depth 28, 9.4 s, largest RSS 878.9 MiB (p, q)", self.lines)
        self.assertIn("error: found a counterexample", self.output())

    def test_a_run_over_its_time_fails(self) -> None:
        self.run_.verdicts["inst_a"] = (124, "")
        self.assertEqual(self.main(), 1)
        self.assertIn("inst_a: timed out, ? states, depth ?", self.output())

    def test_one_instance_and_a_planted_spec(self) -> None:
        planted = os.path.join(self.tmp, "planted.qnt")
        self.assertEqual(self.main("--instance", "inst_b", "--spec", planted), 0)
        [argv] = self.run_.commands("tlc")
        self.assertEqual(argv[argv.index("--main") + 1], "inst_b")
        self.assertEqual(argv[argv.index("verify") + 1], planted)
        self.assertEqual(self.main("--instance", "nope"), 2)
        self.assertIn("cst-quint: no instance 'nope'", self.lines)

    def test_a_planted_spec_over_two_specs_needs_an_instance(self) -> None:
        checks = (*self.checks, M.Check(spec="specs/t.qnt", main="inst_t", invariants=("r",)))
        planted = os.path.join(self.tmp, "planted.qnt")
        self.assertEqual(self.main("--spec", planted, checks=checks), 2)
        self.assertEqual(
            self.lines, ["cst-quint: --spec needs --instance: the checks come from specs/s.qnt, specs/t.qnt"]
        )
        self.assertEqual(self.run_.commands("tlc"), [])

    def test_a_planted_spec_with_an_instance_replaces_only_its_spec(self) -> None:
        checks = (*self.checks, M.Check(spec="specs/t.qnt", main="inst_t", invariants=("r",)))
        planted = os.path.join(self.tmp, "planted.qnt")
        self.assertEqual(self.main("--instance", "inst_t", "--spec", planted, checks=checks), 0)
        [argv] = self.run_.commands("tlc")
        self.assertEqual(argv[argv.index("--main") + 1], "inst_t")
        self.assertEqual(argv[argv.index("verify") + 1], planted)
        self.assertEqual(argv[argv.index("--invariant") + 1], "r")
        self.run_.calls.clear()
        self.assertEqual(self.main(checks=checks), 0, "without --spec every check runs against its own spec")
        verified = [argv[argv.index("verify") + 1] for argv in self.run_.commands("tlc")]
        self.assertEqual(verified, [os.path.join(self.checkout, "specs", n) for n in ("s.qnt", "s.qnt", "t.qnt")])

    def test_the_tlc_config_and_keep(self) -> None:
        self.assertEqual(self.main("--keep"), 0)
        [work] = os.listdir(os.path.join(self.cache, "work"))
        with open(os.path.join(self.cache, "work", work, "tlc.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"maxHeap": "-Xmx4g", "workers": 4})
        self.assertIn(f"work folder kept: {os.path.join(self.cache, 'work', work)}", self.lines)

    def test_a_missing_tool_or_a_wrong_quint(self) -> None:
        self.assertEqual(self.main(which=lambda name: None if name == "java" else "/usr/bin/" + name), 2)
        self.assertEqual(self.lines, ["cst-quint: needs java on PATH"])
        self.run_.version = "0.34.0"
        self.assertEqual(self.main(), 1)
        self.assertIn("--version: '0.34.0', want 0.33.1", self.output())
        self.assertEqual(self.run_.commands("tlc"), [])

    def test_traces_are_written_normalized_into_the_checkout_then_replayed(self) -> None:
        jobs = (M.TraceJob(name="walk", spec="specs/s.qnt", main="m", backend="simulator", max_steps=10, seed=3),)
        with (
            mock.patch.object(M, "TRACES_DIR", {"specs/s.qnt": "specs/traces"}),
            mock.patch.object(M, "REPLAYS", {"specs/s.qnt": "specs.tests.test_replay"}),
        ):
            self.assertEqual(self.main("--traces", jobs=jobs), 0)
            with open(os.path.join(self.checkout, "specs", "traces", "walk.itf.json"), encoding="utf-8") as f:
                self.assertEqual(f.read(), M.normalize(json.dumps(TRACE), "simulator trace walk of specs/s.qnt (m)"))
            [replay] = self.run_.commands("tests/run_unit.py")
            self.assertEqual(replay[-4:], ["-I", "-B", "tests/run_unit.py", "specs.tests.test_replay"])
            self.run_.replay_status = 1
            self.assertEqual(self.main("--traces", jobs=jobs), 1)
        self.assertEqual(self.main("--traces", "--instance", "x"), 2)

    def test_a_trace_run_that_did_not_reach_its_end_is_not_kept(self) -> None:
        # A witness whose run found no violation wrote a random walk, not a trace to the witness; a walk
        # whose run failed wrote what it had. Neither replaces the checked-in trace.
        witness = M.TraceJob(
            name="w", spec="specs/s.qnt", main="m", backend="simulator", max_steps=10, invariant="neverX", samples=9
        )
        walk = M.TraceJob(name="walk", spec="specs/s.qnt", main="m", backend="simulator", max_steps=10, seed=3)
        dest = os.path.join(self.checkout, "specs", "traces")
        with (
            mock.patch.object(M, "TRACES_DIR", {"specs/s.qnt": "specs/traces"}),
            mock.patch.object(M, "REPLAYS", {"specs/s.qnt": "specs.tests.test_replay"}),
        ):
            self.assertEqual(self.main("--traces", jobs=(witness,)), 0)
            self.assertEqual(os.listdir(dest), ["w.itf.json"])
            os.remove(os.path.join(dest, "w.itf.json"))
            for code, out in ((0, "[ok] No violation found (2051ms).\n"), (124, ""), (1, "error: parse error\n")):
                with self.subTest(code=code, out=out):
                    self.lines.clear()
                    self.run_.calls.clear()
                    self.run_.witness = (code, out)
                    self.assertEqual(self.main("--traces", jobs=(witness,)), 1)
                    self.assertIn(
                        f"cst-quint: w: no trace reaches the witness (neverX): quint exit {code}, no violation found",
                        self.lines,
                    )
                    self.assertEqual(os.listdir(dest), [])
                    self.assertEqual(self.run_.commands("tests/run_unit.py"), [])
            self.run_.walk_status = 1
            self.assertEqual(self.main("--traces", jobs=(walk,)), 1)
            self.assertIn("cst-quint: walk: the walk failed (exit 1)", self.lines)
            self.assertEqual(os.listdir(dest), [])


if __name__ == "__main__":
    unittest.main()
