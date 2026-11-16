"""Unit tests for tests/repo_files.py, the file list of the static gate tests.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest tests.test_repo_files -v
"""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.repo_files import REPO, repo_files


class RepoFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-repo-files-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        # git must not find a checkout above the temporary folder.
        env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": os.path.dirname(self.tmp)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", self.tmp, *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def test_lists_tracked_and_untracked_files_not_ignored_ones(self) -> None:
        self.git("init", "-q")
        for name, text in (("b.py", ""), ("a/tool", ""), (".gitignore", "junk\n"), ("junk", "")):
            os.makedirs(os.path.dirname(os.path.join(self.tmp, name)), exist_ok=True)
            with open(os.path.join(self.tmp, name), "w") as fh:
                fh.write(text)
        self.git("add", "b.py")
        self.assertEqual(repo_files(self.tmp), [".gitignore", "a/tool", "b.py"])

    def test_outside_a_git_checkout_the_gates_are_skipped(self) -> None:
        with self.assertRaises(unittest.SkipTest):
            repo_files(self.tmp)

    def test_a_checkout_git_cannot_read_fails_instead_of_skipping(self) -> None:
        # A .git that git does not accept stands for "dubious ownership" and
        # other refusals: the gates must not go quiet there.
        os.mkdir(os.path.join(self.tmp, ".git"))
        try:
            repo_files(self.tmp)
        except unittest.SkipTest as e:  # caught here: it would only mark this test skipped
            self.fail(f"skipped instead of failing: {e}")
        except AssertionError as e:
            self.assertIn("git ls-files failed in a git checkout", str(e))
        else:
            self.fail("repo_files() listed a checkout git cannot read")

    def test_this_checkout(self) -> None:
        files = repo_files()
        self.assertIn("tests/repo_files.py", files)
        self.assertEqual(files, sorted(files))
        self.assertTrue(all(os.path.isfile(os.path.join(REPO, rel)) for rel in files))


if __name__ == "__main__":
    unittest.main()
