# SPDX-License-Identifier: MPL-2.0
"""The user's cache folder, as the dev tools under tests/ find it (tests/run_unit.py's durations file).

Standard library only, and Python 3.12 code: the tools that import it run on /usr/bin/python3 without uv,
some on CI's runner host (3.12).
"""

from collections.abc import Mapping
import os
import pwd


def cache_base(environ: Mapping[str, str]) -> str | None:
    """The user's cache folder: an absolute XDG_CACHE_HOME, else ~/.cache (HOME, else the password database).

    The XDG Base Directory spec ignores a relative XDG_CACHE_HOME, and a relative or empty HOME is no home:
    the tools run from the top of the checkout, so either would put the cache inside it. None when neither
    is absolute and the password database has no entry for this user.
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
    return os.path.join(home, ".cache")
