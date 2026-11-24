# SPDX-License-Identifier: MPL-2.0
"""Static gate: the Python CI runs on its runner's python3 is code that Python can run.

The rules live in tests/e2e_host_python.py: which files CI runs on the
ubuntu-24.04 runner's python3 (3.12) and not in a container (the E2E driver,
what it imports and loads, image_digest.py), how the gate finds them from the
code and CI's files, and what it checks in each (the grammar of that Python,
a table of newer standard-library names). This module runs those checks on
the checkout and on temporary copies with a planted problem.

Standard library only: the unit tests run on /usr/bin/python3 without uv.
"""

import ast
import importlib
import os
import shutil
import tempfile
from typing import override
import unittest

from tests import e2e_host_python as gate
from tests.repo_files import REPO

# The files of the checkout the gate reads: CI's workflows and actions, the invowkfile, the closure.
_READ_TREES = (".github",)
_READ_FILES = (
    "cst.invowkmod/invowkfile.cue",
    "cst_core/__init__.py",
    "cst_core/domain/__init__.py",
    "cst_core/domain/errors.py",
    "cst_core/domain/values.py",
    "pyproject.toml",
    "tests/__init__.py",
    "tests/e2e/__init__.py",
    "tests/e2e/image_digest.py",
    "tests/e2e/run.py",
    "tests/e2e/selection.py",
    "tests/e2e/suites/suite_tier1_systemd.py",
    "tests/e2e/suites/suite_tier2_storage_cpu.py",
    "tests/quint/cst-quint",
)


class CheckoutTest(unittest.TestCase):
    def test_the_checkout_has_no_problem(self) -> None:
        self.assertEqual(gate.problems(REPO), [])

    def test_the_closure_is_found_from_the_code(self) -> None:
        closure, problems = gate.host_closure(REPO)
        self.assertEqual(problems, [])
        self.assertEqual(
            closure,
            [
                "cst_core/__init__.py",
                "cst_core/domain/__init__.py",
                "cst_core/domain/errors.py",
                "cst_core/domain/values.py",
                "tests/__init__.py",
                "tests/e2e/__init__.py",
                "tests/e2e/image_digest.py",
                "tests/e2e/run.py",
                "tests/e2e/selection.py",
                "tests/e2e/suites/suite_tier1_systemd.py",
                "tests/e2e/suites/suite_tier2_storage_cpu.py",
                "tests/quint/cst-quint",
            ],
        )

    def test_ci_runs_the_entries_on_the_known_runner(self) -> None:
        by_runner, problems = gate.host_runs(REPO)
        self.assertEqual(problems, [])
        self.assertEqual(set(by_runner), {"ubuntu-24.04"})
        self.assertEqual(set(by_runner["ubuntu-24.04"]), set(gate.HOST_ENTRIES))
        self.assertEqual(gate.target_version(by_runner), (3, 12))

    def test_every_table_name_exists_on_this_python(self) -> None:
        # A typo in NEWER_STDLIB would name nothing and catch nothing: each name is a module, or an
        # attribute of one, on the interpreter the unit tests run on (newer than every entry).
        for name in gate.NEWER_STDLIB:
            with self.subTest(name=name):
                try:
                    importlib.import_module(name)
                except ImportError:
                    module, _, attr = name.rpartition(".")
                    self.assertTrue(hasattr(importlib.import_module(module), attr), name)


class PlantedTest(unittest.TestCase):
    """A temporary copy of the files the gate reads, with one problem planted."""

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        for tree in _READ_TREES:
            shutil.copytree(os.path.join(REPO, tree), os.path.join(self.repo, tree))
        for rel in _READ_FILES:
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(REPO, rel), os.path.join(self.repo, rel))
        self.assertEqual(gate.problems(self.repo), [])

    def append(self, rel: str, text: str) -> None:
        """Append text to the copy's file rel (created when it is not there)."""
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def line_of(self, rel: str, text: str) -> int:
        """The number of the last line of the copy's file rel that holds text."""
        with open(os.path.join(self.repo, rel), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        return max(n for n, line in enumerate(lines, 1) if text in line)

    def replace(self, rel: str, old: str, new: str) -> None:
        path = os.path.join(self.repo, rel)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count(old), 1, old)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old, new))

    def assert_one_problem(self, *words: str) -> None:
        problems = gate.problems(self.repo)
        self.assertEqual(len(problems), 1, problems)
        for word in words:
            self.assertIn(word, problems[0])

    def assert_checked_at(self, where: str, *, not_at: str) -> None:
        """A problem starts with where (the file was checked there), and none names not_at."""
        problems = gate.problems(self.repo)
        self.assertTrue(any(p.startswith(where) for p in problems), problems)
        self.assertFalse(any(not_at in p for p in problems), problems)

    def test_an_unparenthesized_except_is_reported_with_its_file_and_line(self) -> None:
        # Body on the handler's line: Python names the line where the handler ends.
        self.append("tests/e2e/selection.py", "\ntry:\n    pass\nexcept OSError, ValueError: pass\n")
        line = self.line_of("tests/e2e/selection.py", "except OSError, ValueError:")
        self.assert_one_problem(f"tests/e2e/selection.py:{line}:", "ubuntu-24.04, 3.12")

    def test_a_newer_os_function_is_reported(self) -> None:
        self.append("tests/e2e/run.py", "\nCPUS = os.process_cpu_count()\n")
        line = self.line_of("tests/e2e/run.py", "CPUS = os.process_cpu_count()")
        self.assert_one_problem(f"tests/e2e/run.py:{line}:", "os.process_cpu_count is new in Python 3.13")

    def test_a_newer_typing_name_in_a_suite_is_reported(self) -> None:
        self.append("tests/e2e/suites/suite_tier2_storage_cpu.py", "\nfrom typing import TypeIs\n")
        self.assert_one_problem("suite_tier2_storage_cpu.py", "typing.TypeIs is new in Python 3.13")

    def test_a_newer_module_is_reported(self) -> None:
        self.append("tests/e2e/image_digest.py", "\nimport annotationlib\n")
        self.assert_one_problem("tests/e2e/image_digest.py", "annotationlib is new in Python 3.14")

    def test_a_newer_name_through_an_alias_is_reported(self) -> None:
        self.append("tests/e2e/run.py", "\nimport os as o\n\nCPUS = o.process_cpu_count()\n")
        self.assert_one_problem("tests/e2e/run.py", "os.process_cpu_count")

    def test_a_newer_name_in_a_docstring_or_comment_is_not_reported(self) -> None:
        self.append(
            "tests/e2e/run.py",
            '\n\ndef cpus() -> int:\n    """Like os.process_cpu_count()."""\n    return 1  # not os.process_cpu_count\n',
        )
        self.assertEqual(gate.problems(self.repo), [])

    def test_a_module_the_closure_imports_is_checked_too(self) -> None:
        self.append("tests/e2e/helper.py", "try:\n    pass\nexcept OSError, ValueError: pass\n")
        self.append("tests/e2e/selection.py", "\nimport tests.e2e.helper\n")
        self.assert_one_problem("tests/e2e/helper.py:3:")

    def test_a_load_by_path_the_gate_cannot_follow_is_reported(self) -> None:
        self.append(
            "tests/e2e/run.py",
            "\n\ndef other(path: str) -> None:\n"
            "    import importlib.util\n\n"
            '    importlib.util.spec_from_file_location("x", path + ".py")\n',
        )
        line = self.line_of("tests/e2e/run.py", 'spec_from_file_location("x", path')
        self.assert_one_problem(f"tests/e2e/run.py:{line}:", "teach the gate this load")

    def test_a_load_from_a_folder_two_levels_up_is_checked_in_that_folder(self) -> None:
        # .parent.parent was taken as the file's own folder: tests/e2e/helper.py, not tests/helper.py.
        self.append("tests/helper.py", "try:\n    pass\nexcept OSError, ValueError: pass\n")
        self.append(
            "tests/e2e/run.py",
            "\nTESTS_DIR = Path(__file__).resolve().parent.parent\n"
            "\n\ndef helper() -> None:\n"
            '    importlib.util.spec_from_file_location("helper", TESTS_DIR / "helper.py")\n',
        )
        self.assert_checked_at("tests/helper.py:3:", not_at="tests/e2e/helper.py")

    def test_a_load_from_the_parents_of_the_file_is_checked_in_that_folder(self) -> None:
        self.append("tests/helper.py", "try:\n    pass\nexcept OSError, ValueError: pass\n")
        self.append(
            "tests/e2e/run.py",
            "\nUP = Path(__file__).parents[1]\n"
            "\n\ndef helper() -> None:\n"
            '    importlib.util.spec_from_file_location("helper", UP / "helper.py")\n',
        )
        self.assert_checked_at("tests/helper.py:3:", not_at="tests/e2e/helper.py")

    def test_a_load_from_the_top_of_the_repository_is_checked_there(self) -> None:
        # Three folders up from tests/e2e/run.py is the top of the repository (""): the old gate took it
        # for a folder above it and reported a load it cannot follow instead of checking m.py.
        self.append("m.py", "try:\n    pass\nexcept OSError, ValueError: pass\n")
        self.append(
            "tests/e2e/run.py",
            "\nROOT = Path(__file__).resolve().parent.parent.parent\n"
            "\n\ndef top() -> None:\n"
            '    importlib.util.spec_from_file_location("m", ROOT / "m.py")\n',
        )
        self.assert_checked_at("m.py:3:", not_at="cannot follow")

    def test_a_folder_above_the_repository_is_a_load_the_gate_cannot_follow(self) -> None:
        self.append(
            "tests/e2e/run.py",
            "\nOUT = Path(__file__).parents[3]\n"
            "\n\ndef helper() -> None:\n"
            '    importlib.util.spec_from_file_location("helper", OUT / "helper.py")\n',
        )
        self.assert_one_problem("tests/e2e/run.py:", "teach the gate this load")

    def test_a_module_a_relative_import_names_is_checked_too(self) -> None:
        # imported_modules() leaves relative imports out: helper.py went unchecked on 3.12.
        self.append("tests/e2e/helper.py", "try:\n    pass\nexcept OSError, ValueError: pass\n")
        self.append("tests/e2e/selection.py", "\nfrom .helper import f\n")
        self.assert_one_problem("tests/e2e/helper.py:3:")

    def test_a_module_a_relative_package_import_names_is_checked_too(self) -> None:
        self.append("tests/e2e/helper.py", "try:\n    pass\nexcept OSError, ValueError: pass\n")
        self.append("tests/e2e/suites/suite_tier1_systemd.py", "\nfrom .. import helper\n")
        self.assert_one_problem("tests/e2e/helper.py:3:")

    def test_a_relative_import_above_the_repository_is_reported(self) -> None:
        self.append("tests/e2e/selection.py", "\nfrom .... import x\n")
        self.assert_one_problem("tests/e2e/selection.py:", "above the top of the repository")

    def test_an_import_by_a_name_given_at_run_time_is_reported(self) -> None:
        for imports, code in (
            ("import importlib\n", 'importlib.import_module("tests.e2e.helper")'),
            ("", '__import__("tests.e2e.helper")'),
            ("", 'exec(open("tests/e2e/helper.py").read())'),
            ("import runpy\n", 'runpy.run_module("tests.e2e.helper")'),
            ("import importlib as il\n", 'il.import_module("tests.e2e.helper")'),
            ("from importlib import import_module\n", 'import_module("tests.e2e.helper")'),
            ("from runpy import run_module as rm\n", 'rm("tests.e2e.helper")'),
        ):
            with self.subTest(code=code):
                self.setUp()
                self.append("tests/e2e/selection.py", f"\n{imports}\n\ndef load() -> None:\n    {code}\n")
                line = self.line_of("tests/e2e/selection.py", code)
                self.assert_one_problem(f"tests/e2e/selection.py:{line}:", "given at run time")

    def test_a_call_that_only_shares_a_run_time_import_s_name_is_no_problem(self) -> None:
        # The old gate matched the method's last name: obj.exec(), self.run_module() and a repository
        # function named run_module were false problems.
        for imports, code in (
            ("", "obj.exec(x)"),
            ("", "self.run_module()"),
            ("from tests.e2e.helper import run_module\n", "run_module()"),
        ):
            with self.subTest(code=code):
                self.setUp()
                self.append("tests/e2e/helper.py", "def run_module() -> None:\n    pass\n")
                self.append("tests/e2e/selection.py", f"\n{imports}\n\ndef load() -> None:\n    {code}\n")
                self.assertEqual(gate.problems(self.repo), [])

    def test_a_new_suite_file_is_checked_too(self) -> None:
        self.replace(
            "tests/e2e/run.py",
            '    2: "suite_tier2_storage_cpu.py",\n',
            '    2: "suite_tier2_storage_cpu.py",\n    3: "suite_new.py",\n',
        )
        self.append("tests/e2e/suites/suite_new.py", "import annotationlib\n")
        self.assert_one_problem("tests/e2e/suites/suite_new.py:1:", "annotationlib")

    def test_a_new_python_module_an_action_runs_with_dash_m_is_reported(self) -> None:
        # Only python3 ... FILE.py was read: python3 -m added a host entry the gate neither followed nor reported.
        self.append("tests/e2e/new.py", "")
        self.replace(
            ".github/actions/prep-e2e-runner/action.yml",
            'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"',
            'want="$(python3 -I -B tests/e2e/image_digest.py "${name}"; python3 -I -B -m tests.e2e.new)"',
        )
        self.assert_one_problem("prep-e2e-runner/action.yml", "tests/e2e/new.py", "HOST_ENTRIES")

    def test_an_entry_run_with_dash_m_and_a_standard_module_are_no_problem(self) -> None:
        self.replace(
            ".github/actions/prep-e2e-runner/action.yml",
            'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"',
            'want="$(python3 -m venv /tmp/v; python3 -I -B -m tests.e2e.image_digest "${name}")"',
        )
        self.assertEqual(gate.problems(self.repo), [])
        self.assertEqual(set(gate.host_runs(self.repo)[0]["ubuntu-24.04"]), set(gate.HOST_ENTRIES))

    def test_an_extensionless_program_a_host_job_runs_is_checked(self) -> None:
        # The quint jobs run tests/quint/cst-quint through `invowk cmd test quint` on the runner's
        # python3 (3.12): only python3 ... FILE.py was read, so a 3.14 line there passed the gate.
        self.append("tests/quint/cst-quint", "\ntry:\n    pass\nexcept OSError, ValueError: pass\n")
        line = self.line_of("tests/quint/cst-quint", "except OSError, ValueError:")
        self.assert_one_problem(f"tests/quint/cst-quint:{line}:", "ubuntu-24.04, 3.12")

    def test_a_new_extensionless_program_a_host_job_s_task_runs_is_reported(self) -> None:
        self.append("tests/quint/cst-new", "import os\n")
        self.replace(
            "cst.invowkmod/invowkfile.cue",
            "/usr/bin/python3 -I -B tests/quint/cst-quint ",
            "/usr/bin/python3 -I -B tests/quint/cst-new ",
        )
        problems = gate.problems(self.repo)
        self.assertEqual(len(problems), 2, problems)  # the quint job of ci.yml and of nightly.yml
        for problem in problems:
            self.assertIn("runs tests/quint/cst-new", problem)
            self.assertIn("HOST_ENTRIES", problem)

    def test_a_dash_m_after_the_script_is_the_script_s_argument(self) -> None:
        # python3 stops reading its options at the script: the old gate took this -m as python3's.
        self.append("tests/e2e/new.py", "")
        self.replace(
            ".github/actions/prep-e2e-runner/action.yml",
            'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"',
            'want="$(python3 -I -B tests/e2e/image_digest.py -m tests.e2e.new "${name}")"',
        )
        self.assertEqual(gate.problems(self.repo), [])

    def test_options_with_an_argument_are_read_as_python3_reads_them(self) -> None:
        self.append("tests/e2e/new.py", "")
        action = ".github/actions/prep-e2e-runner/action.yml"
        old = 'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"'
        cases = {
            'want="$(python3 -X dev -W error tests/e2e/new.py "${name}")"': "tests/e2e/new.py",
            "want=\"$(python3 -I -c 'print(1)'; python3 -m venv /tmp/v)\"": None,
            'want="$(python3 -IBm tests.e2e.image_digest "${name}")"': None,
            'want="$(python3 --check-hash-based-pycs always tests/e2e/new.py)"': "tests/e2e/new.py",
        }
        for new, reported in cases.items():
            with self.subTest(line=new):
                self.setUp()
                self.replace(action, old, new)
                if reported is None:
                    self.assertEqual(gate.problems(self.repo), [])
                else:
                    self.assert_one_problem("prep-e2e-runner/action.yml", reported, "HOST_ENTRIES")

    def test_a_program_next_to_shell_punctuation_or_a_redirection_is_read(self) -> None:
        # The words after python3 were split at blanks only: a program word with ; | > glued to it, one
        # after a redirection or a quoted "python3" was no program, and the file went unchecked.
        self.append("tests/e2e/new.py", "")
        action = ".github/actions/prep-e2e-runner/action.yml"
        old = 'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"'
        for form in (
            "python3 tests/e2e/new.py|tee log",
            "python3 tests/e2e/new.py>log",
            "python3 tests/e2e/new.py;python3 -I tests/e2e/image_digest.py",
            "python3 2>/dev/null tests/e2e/new.py",
            "'python3' tests/e2e/new.py",
        ):
            with self.subTest(form=form):
                self.setUp()
                self.replace(action, old, f'want="$(python3 -I -B tests/e2e/image_digest.py "${{name}}"; {form})"')
                self.assert_one_problem("prep-e2e-runner/action.yml", "tests/e2e/new.py", "HOST_ENTRIES")

    def test_a_program_named_at_run_time_is_reported(self) -> None:
        self.replace(
            ".github/actions/prep-e2e-runner/action.yml",
            'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"',
            'want="$(python3 -I -B "$tool" "${name}")"',
        )
        self.assert_one_problem("prep-e2e-runner/action.yml", "a program the gate cannot name", "$tool")

    def test_python3_in_a_message_is_no_program_named_at_run_time(self) -> None:
        # Any python3 word followed by a word with a "$" was a program the gate cannot name: in a message, a
        # step's name: and a ${{ github.workspace }} path, which names the checkout, failed the unit suite.
        action = ".github/actions/prep-e2e-runner/action.yml"
        old = 'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"'
        for extra in (
            '; echo "install python3 $VERSION"',
            "; echo install python3 $VERSION",
            "; printf '%s\\n' python3 \"$v\"",
            "\n- name: Set up python3 ${{ matrix.python }}",
        ):
            with self.subTest(extra=extra):
                self.setUp()
                self.replace(action, old, old + extra)
                self.assertEqual(gate.problems(self.repo), [])

    def test_a_program_under_the_checkout_s_folder_is_named(self) -> None:
        self.append("tests/e2e/new.py", "")
        action = ".github/actions/prep-e2e-runner/action.yml"
        old = 'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"'
        for path in ("${{ github.workspace }}/tests/e2e/new.py", '"$GITHUB_WORKSPACE/tests/e2e/new.py"'):
            with self.subTest(path=path):
                self.setUp()
                self.replace(action, old, f"{old}; python3 -I {path}")
                self.assert_one_problem("prep-e2e-runner/action.yml", "runs tests/e2e/new.py", "HOST_ENTRIES")

    def test_a_program_named_at_run_time_by_a_command_is_still_reported(self) -> None:
        action = ".github/actions/prep-e2e-runner/action.yml"
        old = 'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"'
        for extra in (
            '; if true; then X=1 python3 "$tool"; fi',
            "; echo x | python3 $tool",
            '; echo "$(python3 $tool)"',
            "; python3 ${{ inputs.tool }}",
        ):
            with self.subTest(extra=extra):
                self.setUp()
                self.replace(action, old, old + extra)
                self.assert_one_problem("prep-e2e-runner/action.yml", "a program the gate cannot name")

    def test_a_new_python_file_an_action_runs_is_reported(self) -> None:
        self.replace(
            ".github/actions/prep-e2e-runner/action.yml",
            'want="$(python3 -I -B tests/e2e/image_digest.py "${name}")"',
            'want="$(python3 -I -B tests/e2e/new.py "${name}")"',
        )
        self.assert_one_problem("prep-e2e-runner/action.yml", "tests/e2e/new.py", "HOST_ENTRIES")

    def test_a_host_job_that_runs_another_invowk_task_with_python_is_reported(self) -> None:
        self.replace(
            ".github/workflows/ci.yml",
            '          invowk cmd test e2e tier1 "${flags[@]}"\n',
            '          invowk cmd test e2e tier1 "${flags[@]}"\n          invowk cmd test unit\n',
        )
        problems = gate.problems(self.repo)
        self.assertTrue(problems)
        self.assertTrue(all("(job e2e-tier1)" in p and "tests/run_unit.py" in p for p in problems), problems)

    def test_a_runner_with_no_recorded_python_is_reported(self) -> None:
        path = os.path.join(self.repo, ".github/workflows/ci.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        head, job, tail = text.partition("  e2e-tier1:\n")
        tail = tail.replace("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-26.04\n", 1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(head + job + tail)
        self.assert_one_problem("(job e2e-tier1)", "ubuntu-26.04", "HOST_PYTHON_BY_RUNNER")


class CommandLineTest(unittest.TestCase):
    def test_python3_s_options_are_read_as_python_reads_them(self) -> None:
        for line, want in (
            ("-I -B tests/e2e/run.py --tier 1", ("script", "tests/e2e/run.py")),
            ("-IB tests/e2e/run.py -m x", ("script", "tests/e2e/run.py")),
            ("-Xdev -W error -- -odd.py", ("script", "-odd.py")),
            ("--version", None),
            ("-I -m tests.e2e.run -c x", ("module", "tests.e2e.run")),
            ("-Im tests.e2e.run", ("module", "tests.e2e.run")),
            ("-c 'import x' tests/e2e/run.py", None),
            ("- < tests/e2e/run.py", None),
            ("-I; tests/e2e/run.py", None),
            ('-I -B "tests/e2e/run.py")', ("script", "tests/e2e/run.py")),
            ("", None),
            # Shell punctuation inside a word, and redirections before or after the program.
            ("tests/e2e/run.py|tee log", ("script", "tests/e2e/run.py")),
            ("tests/e2e/run.py>log", ("script", "tests/e2e/run.py")),
            ("tests/e2e/run.py;python3 x.py", ("script", "tests/e2e/run.py")),
            ("2>/dev/null tests/e2e/run.py", ("script", "tests/e2e/run.py")),
            (">log 2>&1 -I tests/e2e/run.py", ("script", "tests/e2e/run.py")),
            ("&>log tests/e2e/run.py", ("script", "tests/e2e/run.py")),
            ("<in -m tests.e2e.run", ("module", "tests.e2e.run")),
            ('-I "tests/e2e/run.py"&&true', ("script", "tests/e2e/run.py")),
            ("-I|tee log", None),
            ("--check-hash-based-pycs always tests/e2e/run.py", ("script", "tests/e2e/run.py")),
            ("--check-hash-based-pycs=never tests/e2e/run.py", ("script", "tests/e2e/run.py")),
            ("--version tests/e2e/run.py", ("script", "tests/e2e/run.py")),
        ):
            with self.subTest(line=line):
                self.assertEqual(gate.python_program(line), want)


class RuffTargetTest(unittest.TestCase):
    CLOSURE = ("tests/__init__.py", "tests/e2e/run.py", "tests/e2e/suites/suite_tier1_systemd.py")

    def test_the_checkout_formats_the_closure_as_its_python(self) -> None:
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as fh:
            pyproject = fh.read()
        closure, _ = gate.host_closure(REPO)
        self.assertEqual(gate.ruff_target_problems(pyproject, closure, (3, 12)), [])

    def test_a_file_with_no_entry_is_reported(self) -> None:
        pyproject = '[tool.ruff]\nper-file-target-version = { "tests/e2e/**/*.py" = "py312" }\n'
        problems = gate.ruff_target_problems(pyproject, self.CLOSURE, (3, 12))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tests/__init__.py", problems[0])
        self.assertIn("no entry", problems[0])

    def test_no_table_reports_every_file(self) -> None:
        problems = gate.ruff_target_problems("[tool.ruff]\nline-length = 120\n", self.CLOSURE, (3, 12))
        self.assertEqual(len(problems), len(self.CLOSURE), problems)

    def test_an_entry_of_another_version_is_reported(self) -> None:
        pyproject = (
            '[tool.ruff]\nper-file-target-version = { "tests/__init__.py" = "py312", "tests/e2e/**/*.py" = "py313" }\n'
        )
        problems = gate.ruff_target_problems(pyproject, self.CLOSURE, (3, 12))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("py313" in p for p in problems), problems)

    def test_a_pyproject_without_the_entry_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for tree in _READ_TREES:
                shutil.copytree(os.path.join(REPO, tree), os.path.join(tmp, tree))
            for rel in _READ_FILES:
                os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
                shutil.copyfile(os.path.join(REPO, rel), os.path.join(tmp, rel))
            path = os.path.join(tmp, "pyproject.toml")
            with open(path, encoding="utf-8") as fh:
                lines = [line for line in fh if not line.startswith("per-file-target-version")]
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            problems = gate.problems(tmp)
        closure, _ = gate.host_closure(REPO)
        self.assertEqual(len(problems), len(closure), problems)
        self.assertTrue(all("per-file-target-version" in p for p in problems), problems)


class NamesTest(unittest.TestCase):
    def names(self, source: str, target: tuple[int, int] = (3, 12)) -> list[str]:
        return [name for _, name, _ in gate.newer_names(ast.parse(source), target)]

    def test_imports_and_attributes(self) -> None:
        self.assertEqual(self.names("from os import process_cpu_count\n"), ["os.process_cpu_count"])
        self.assertEqual(self.names("import string.templatelib\n"), ["string.templatelib"])
        self.assertEqual(self.names("import os\nos.process_cpu_count()\n"), ["os.process_cpu_count"])
        self.assertEqual(self.names("raise PythonFinalizationError\n"), ["builtins.PythonFinalizationError"])
        self.assertEqual(self.names("import os\nos.cpu_count()\nos.path.join('a')\n"), [])

    def test_a_name_the_target_has_is_not_reported(self) -> None:
        self.assertEqual(self.names("import os\nos.process_cpu_count()\n", (3, 13)), [])
        self.assertEqual(self.names("import annotationlib\n", (3, 13)), ["annotationlib"])

    def test_the_oldest_runner_python_is_the_target(self) -> None:
        self.assertEqual(gate.target_version({"ubuntu-24.04": ["tests/e2e/run.py"]}), (3, 12))
        self.assertEqual(gate.target_version({}), (3, 12))


if __name__ == "__main__":
    unittest.main()
