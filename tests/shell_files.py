#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Every shell file of the checkout, one per line (the list `invowk cmd test lint` lints).

Use: python3 -I -B tests/shell_files.py   (from anywhere)

A shell file is a .sh or .bash file, a file whose shebang runs bash or sh,
or one whose first line is `# shellcheck shell=...` (tests/duplication.py,
`is_shell`). The list is derived from the repository, so a new script is
linted without anyone adding it to a list; tests/test_distribution.py checks
that each one is a known kind (SHELL_TEST_SCRIPTS below is the kind that
only the E2E tests run).

Standard library only.
"""

import os
import sys
from typing import Final

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.duplication import is_shell
from tests.repo_files import repo_files

# Shell scripts that only the E2E tests run, inside their containers.
SHELL_TEST_SCRIPTS: Final[frozenset[str]] = frozenset(
    {
        "tests/e2e/scripts/entrypoint-host-sim.sh",
        "tests/e2e/scripts/setup-dind.sh",
        "tests/e2e/scripts/setup-loop-btrfs.sh",
        # The previous version's programs (tests/fixtures/legacy), installed by tier 1 next to this one.
        "tests/fixtures/legacy/cpu-setup.sh@cfb496a",
        "tests/fixtures/legacy/game-profile@cfb496a",
    }
)


def shell_files(repo: str = REPO) -> list[str]:
    """The shell files of the checkout, repository-relative, sorted."""
    return [rel for rel in repo_files(repo) if is_shell(rel, repo)]


def main() -> int:
    for rel in shell_files():
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
