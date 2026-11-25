# SPDX-License-Identifier: MPL-2.0
"""The user's cache folder, as the dev tools under tests/ find it.

tests/run_unit.py (its durations file), tests/mbt/profiles.py (Hypothesis's folder), tests/quint/cst-quint
(its tools), tests/mutation/cst-mutation (its output folder) and the coverage task of invowkfile.cue (its
default folder) use it; tests/uv-dev, a bash script, follows the same rule.

Standard library only, and Python 3.12 code: the tools that import it run on /usr/bin/python3 without uv,
some on CI's runner host (3.12).
"""

from collections.abc import Mapping
import os
import pwd
from typing import Final

# What a tool says when cache_base finds no folder (each puts its own name first).
NO_CACHE_BASE: Final = (
    "no cache folder: XDG_CACHE_HOME is not an absolute path, nor is HOME, "
    "and the password database has no absolute home folder for this user"
)


def cache_base(environ: Mapping[str, str]) -> str | None:
    """The user's cache folder: an absolute XDG_CACHE_HOME, else ~/.cache (HOME, else the password database).

    The XDG Base Directory spec ignores a relative XDG_CACHE_HOME, and a relative or empty HOME is no home:
    the tools run from the top of the checkout, so either would put the cache inside it; so would a relative
    or empty home folder in the password database. None when neither is absolute and the password database
    has no absolute home folder for this user (no entry, or a home field that is not an absolute path).
    """
    base = environ.get("XDG_CACHE_HOME", "")
    if os.path.isabs(base):
        return base
    home = environ.get("HOME", "")
    if not os.path.isabs(home):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            return None
        if not os.path.isabs(home):
            return None
    return os.path.join(home, ".cache")
