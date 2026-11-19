# SPDX-License-Identifier: MPL-2.0
"""Cut one function out of a shell script's text, for tests that run it alone in bash.

Shared by the btrfs setup tests and tests/unit_files.py (which renders
btrfs-setup.sh's scrub limit drop-in with it). Standard library only.
"""

import re


def shell_function(path: str, name: str) -> str:
    """The definition of the shell function NAME in PATH ('name() {' to '}')."""
    with open(path) as f:
        text = f.read()
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    if m is None:
        raise AssertionError(f"{name}() not found in {path}")
    return m.group(0)
