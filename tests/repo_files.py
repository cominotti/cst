"""The files of the repository, for the static gate tests.

tests/test_repo_paths.py, tests/test_python_files.py,
tests/test_subprocess_hygiene.py and tests/test_docs.py read every file of
the checkout. They all
take the list from here, so that they handle a git failure the same way.
"""

import os
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_files(repo: str = REPO) -> list[str]:
    """Tracked and untracked (not ignored) files of the checkout, relative to it, sorted.

    Outside a git checkout (a source tarball) the gate tests have nothing to
    read and are skipped. In a checkout git refuses to read (for example
    "dubious ownership", when the checkout belongs to another user), this
    fails: skipping there would turn the gates off without anyone noticing.
    """
    out = subprocess.run(
        ["git", "-C", repo, "ls-files", "-co", "--exclude-standard"],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if out.returncode != 0:
        if not os.path.exists(os.path.join(repo, ".git")):
            raise unittest.SkipTest("not a git checkout: " + out.stderr.strip())
        raise AssertionError("git ls-files failed in a git checkout: " + out.stderr.strip())
    return sorted(rel for rel in out.stdout.splitlines() if os.path.isfile(os.path.join(repo, rel)))
