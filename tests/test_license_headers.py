# SPDX-License-Identifier: MPL-2.0
"""Static gate: every file of the checkout carries the SPDX license header.

The rules live in tests/license_headers.py, which also inserts the header
(`invowk cmd fix license`): the comment syntax of each kind of file, the
line the header goes on, and LICENSE_EXEMPT, the files without one and why.
This module checks:

- every file of tests/repo_files.repo_files() (not the .claude links, which
  point into .agents/) carries the header, once, where the rules want it;
- every LICENSE_EXEMPT pattern matches a file and gives a reason;
- LICENSE keeps the full notice (Exhibit A), and README's License section
  points to LICENSE;
- the rules themselves and the fixer (dry run, --apply, a second run);
- what the header must not change: the help of the scripts that print
  their own header comment (HELP_FROM_HEADER, every shell file with that
  sed) shows no license line, and every `.in` template renders with the
  header in its destination's syntax.

Standard library only: the unit tests run on /usr/bin/python3 without uv.
"""

import contextlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from cst_core.domain.install import INSTALL_MANIFEST, render_template, template_keys
from tests.license_headers import (
    IDENTIFIER,
    LICENSE_EXEMPT,
    apply,
    header_line,
    header_problem,
    main,
    plan,
    problems,
    unmatched_exemptions,
    with_header,
)
from tests.repo_files import REPO, repo_files
from tests.shell_files import shell_files

FIXER = os.path.join(REPO, "tests", "license_headers.py")
# The scripts whose help is their header comment (lines 2 up to the first
# blank line): (script, arguments that print it, exit status).
HELP_FROM_HEADER: tuple[tuple[str, tuple[str, ...], int | None], ...] = (
    ("bluetooth-setup.sh", ("help",), 0),
    ("btrfs-setup.sh", ("help",), 0),
    ("cpu-setup.sh", ("help",), 0),
    ("toolchains-setup.sh", ("help",), 0),
    ("vm-setup.sh", ("help",), 0),
    # Its usage is the bad-argument answer, printed only after the script has re-run itself
    # through sudo; a unit test must never run sudo, so only its help pipeline runs (None).
    ("btrfs/btrfs-degraded-boot-setup.sh", (), None),
    ("btrfs/btrfs-stall-watch", ("--help",), 0),
)


class LicenseGateTest(unittest.TestCase):
    def test_every_file_carries_the_header(self) -> None:
        found = problems(REPO, repo_files())
        self.assertEqual(found, [], "\n".join(found) + "\n(python3 -I -B tests/license_headers.py --apply adds it)")

    def test_exemptions_match_files_and_give_reasons(self) -> None:
        self.assertEqual(unmatched_exemptions(repo_files()), [], "exemptions that match no file: remove them")
        self.assertEqual([p for p, why in LICENSE_EXEMPT.items() if not why.strip()], [])
        self.assertEqual(unmatched_exemptions(["README.md"]), list(LICENSE_EXEMPT))

    def test_license_keeps_the_full_notice(self) -> None:
        text = Path(REPO, "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Exhibit A - Source Code Form License Notice", text)
        self.assertIn("This Source Code Form is subject to the terms of the Mozilla Public", text)
        readme = Path(REPO, "README.md").read_text(encoding="utf-8")
        section = readme.split("\n## License\n", 1)[1]
        self.assertIn("(LICENSE)", section)
        self.assertIn(IDENTIFIER, section)

    def test_rendered_templates_carry_the_header_in_their_destination_syntax(self) -> None:
        templates = [e for e in INSTALL_MANIFEST if e.src.endswith(".in")]
        self.assertTrue(templates)
        for e in templates:
            with self.subTest(src=e.src):
                text = Path(REPO, e.src).read_text(encoding="utf-8")
                rendered = render_template(text, dict.fromkeys(template_keys(text), "x"))
                self.assertIsNone(header_problem(e.dest, rendered))

    def test_every_script_that_prints_its_header_comment_is_listed(self) -> None:
        # A script copied from these (same sed) is checked by the test below only once it is listed;
        # so is a shim whose help cst_core/route.py prints from its header (script_help, the same rule).
        idioms = ("sed -n '2,/^$/", "/cst_core/route.py")
        using = {rel for rel in shell_files() if any(i in Path(REPO, rel).read_text(encoding="utf-8") for i in idioms)}
        self.assertEqual(using, {script for script, _, _ in HELP_FROM_HEADER})

    def test_help_from_the_header_comment_shows_no_license_line(self) -> None:
        home = tempfile.mkdtemp(prefix="cst-license-help-")
        self.addCleanup(shutil.rmtree, home)
        for script, args, status in HELP_FROM_HEADER:
            with self.subTest(script=script):
                path = os.path.join(REPO, script)
                # Not vacuous: the script does carry the header the help must skip.
                self.assertIsNone(header_problem(script, Path(path).read_text(encoding="utf-8")))
                argv = ["bash", path, *args]
                if status is None:
                    line = next(x for x in Path(path).read_text(encoding="utf-8").splitlines() if "sed -n '2,/^$/" in x)
                    argv = ["bash", "-c", line.strip().replace('"$0"', '"$1"'), "help", path]
                r = subprocess.run(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    env={"PATH": "/usr/bin:/bin", "HOME": home, "LC_ALL": "C.UTF-8"},
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
                self.assertEqual(r.returncode, 0 if status is None else status, r.stdout + r.stderr)
                self.assertNotIn("SPDX-License-Identifier", r.stdout + r.stderr)
                first = next(line for line in r.stdout.splitlines() if line.strip())
                self.assertIn(os.path.basename(script), first)


class RulesTest(unittest.TestCase):
    def test_comment_syntax_by_kind(self) -> None:
        cases = {
            ("swap/domain.py", '"""Doc."""'): "# " + IDENTIFIER,
            ("cst", "#!/usr/bin/env bash"): "# " + IDENTIFIER,
            ("cpu/units/builds.slice", "[Slice]"): "# " + IDENTIFIER,
            ("btrfs/snapshots/top.mount.in", "[Unit]"): "# " + IDENTIFIER,
            ("tests/e2e/images/nas.Dockerfile", "FROM x"): "# " + IDENTIFIER,
            (".gitignore", "*.pyc"): "# " + IDENTIFIER,
            ("cpu/cpu-game.rules.in", "polkit.addRule(...);"): "// " + IDENTIFIER,
            ("cst.invowkmod/invowkfile.cue", 'workdir: ".."'): "// " + IDENTIFIER,
            ("cpu/gamemode.ini", "[custom]"): "; " + IDENTIFIER,
            ("README.md", "# CST"): "<!-- " + IDENTIFIER + " -->",
            ("vm/tests/fixtures/domain.xml", "<domain/>"): "<!-- " + IDENTIFIER + " -->",
        }
        for (rel, first), want in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(header_line(rel, first), want)
        self.assertIsNone(header_line("x/data.bin", "\x00"))

    def test_an_unknown_kind_is_a_problem_not_a_skip(self) -> None:
        self.assertIn("unknown file kind", header_problem("x/data.bin", "abc\n") or "")
        with self.assertRaises(ValueError):
            with_header("x/data.bin", "abc\n")

    def test_the_header_goes_after_the_lines_that_stay_first(self) -> None:
        h = "# " + IDENTIFIER + "\n"
        cases = {
            ("a.py", ""): h,
            ("a.py", '"""Doc."""\n'): h + '"""Doc."""\n',
            ("bin/tool", "#!/usr/bin/python3 -I\n# managed-by: x\n"): f"#!/usr/bin/python3 -I\n{h}# managed-by: x\n",
            ("bin/tool", "#!/bin/sh"): "#!/bin/sh\n" + h,
            ("lib.bash", "# shellcheck shell=bash\n#\n"): "# shellcheck shell=bash\n" + h + "#\n",
            ("x.Dockerfile", "# syntax=docker/dockerfile:1\nFROM x\n"): f"# syntax=docker/dockerfile:1\n{h}FROM x\n",
            ("d.xml", "<?xml version='1.0'?>\n<d/>\n"): "<?xml version='1.0'?>\n<!-- " + IDENTIFIER + " -->\n<d/>\n",
            ("SKILL.md", "---\nname: x\n---\n\n# X\n"): "---\nname: x\n---\n<!-- " + IDENTIFIER + " -->\n\n# X\n",
        }
        for (rel, text), want in cases.items():
            with self.subTest(rel=rel, text=text):
                got = with_header(rel, text)
                self.assertEqual(got, want)
                self.assertIsNone(header_problem(rel, got))
                self.assertEqual(with_header(rel, got), got, "a second run changes nothing")

    def test_a_misplaced_or_second_header_is_refused(self) -> None:
        h = "# " + IDENTIFIER + "\n"
        misplaced = "#!/bin/sh\necho hi\n" + h
        self.assertIn("line(s) [3]", header_problem("t.sh", misplaced) or "")
        with self.assertRaises(ValueError):
            with_header("t.sh", misplaced)
        self.assertIn("line(s) [1, 2]", header_problem("a.py", h + h) or "")
        # Before the frontmatter it is misplaced too: the frontmatter has to open the file.
        md = "<!-- " + IDENTIFIER + " -->\n---\nname: x\n---\n"
        self.assertIsNotNone(header_problem("SKILL.md", md))


class FixerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = tempfile.mkdtemp(prefix="cst-license-fix-")
        self.addCleanup(shutil.rmtree, self.repo)
        self.files = {
            "a.py": '"""Doc."""\n',
            "run.sh": "#!/usr/bin/env bash\necho hi\n",
            "skill/SKILL.md": "---\nname: skill\n---\n# Skill\n",
            "LICENSE": "Mozilla Public License Version 2.0\n",
            "done.toml": "# " + IDENTIFIER + "\nx = 1\n",
        }
        for rel, text in self.files.items():
            p = Path(self.repo, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        os.chmod(os.path.join(self.repo, "run.sh"), 0o755)
        os.symlink("a.py", os.path.join(self.repo, "link.py"))

    def snapshot(self) -> dict[str, bytes]:
        return {rel: Path(self.repo, rel).read_bytes() for rel in self.files}

    def test_dry_run_changes_nothing_and_apply_inserts_once(self) -> None:
        names = [*self.files, "link.py"]
        before = self.snapshot()
        changes = plan(self.repo, names)
        self.assertEqual(sorted(c.rel for c in changes), ["a.py", "run.sh", "skill/SKILL.md"])
        self.assertEqual(self.snapshot(), before, "plan wrote a file")
        apply(self.repo, changes)
        self.assertEqual(problems(self.repo, names), [])
        self.assertEqual(Path(self.repo, "LICENSE").read_bytes(), before["LICENSE"], "an exempt file was changed")
        self.assertEqual(Path(self.repo, "done.toml").read_bytes(), before["done.toml"])
        self.assertTrue(os.path.islink(os.path.join(self.repo, "link.py")))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.repo, "run.sh")).st_mode), 0o755)
        self.assertEqual(plan(self.repo, names), [], "a second run has nothing to do")

    def test_a_file_it_cannot_fix_is_refused_not_written(self) -> None:
        Path(self.repo, "blob.bin").write_bytes(b"\xff\xfe")
        Path(self.repo, "odd.dat").write_text("data\n", encoding="utf-8")
        changes = plan(self.repo, ["blob.bin", "odd.dat"])
        self.assertEqual([(c.rel, c.text is None) for c in changes], [("blob.bin", True), ("odd.dat", True)])
        self.assertIn("not UTF-8", changes[0].refused or "")
        self.assertIn("unknown file kind", changes[1].refused or "")

    def run_main(self, *argv: str, files: list[str] | None = None, repo: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv, repo=repo or self.repo, files=files)
            except unittest.SkipTest as exc:  # would only skip this test: the command must not raise it
                self.fail(f"main() raised SkipTest: {exc}")
        return code, out.getvalue(), err.getvalue()

    def test_the_command_lists_then_adds_then_has_nothing_to_do(self) -> None:
        names = [*self.files, "link.py"]
        before = self.snapshot()
        listed = ["a.py", "run.sh", "skill/SKILL.md"]
        want = (
            "".join(f"would add:  {rel}\n" for rel in listed) + "3 file(s) lack the license header; --apply adds it\n"
        )
        self.assertEqual(self.run_main(files=names), (0, want, ""))
        self.assertEqual(self.snapshot(), before, "the dry run wrote a file")
        self.assertEqual(self.run_main("--apply", files=names), (0, "".join(f"added:      {r}\n" for r in listed), ""))
        self.assertEqual(problems(self.repo, names), [])
        self.assertEqual(self.run_main("--apply", files=names), (0, "", ""), "a second run changes nothing")

    def test_the_command_refuses_and_rejects_without_a_traceback(self) -> None:
        Path(self.repo, "odd.dat").write_text("data\n", encoding="utf-8")
        code, out, err = self.run_main("--apply", files=["odd.dat"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("refused:    odd.dat: unknown file kind", err)
        self.assertEqual(Path(self.repo, "odd.dat").read_text(encoding="utf-8"), "data\n")
        self.assertEqual(self.run_main("--bogus")[0], 2)
        # No file list: a folder that is not a git checkout is one line on stderr, not a traceback.
        code, out, err = self.run_main(repo=os.path.join(self.repo, "missing"))
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("license_headers.py: not a git checkout"), err)

    def test_the_checkout_has_nothing_to_add(self) -> None:
        repo_files()  # outside a git checkout (a source tarball) this skips, as the gate does
        r = subprocess.run(
            [sys.executable, "-I", "-B", FIXER],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
