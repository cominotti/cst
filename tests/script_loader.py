# SPDX-License-Identifier: MPL-2.0
"""Load an extension-less helper script (or any Python file) as a module, for tests.

The module is registered in `sys.modules` before its body runs: `@dataclass`
(with slots or `from __future__ import annotations`) and `typing` look the
module up there while the class is being built, and crash on Python 3.14
when it is missing. Every call returns a fresh module object, so a test's
`setUp` can load the helper again to reset its globals.
"""

import importlib.machinery
import sys
from types import ModuleType


def load_script(path: str, name: str) -> ModuleType:
    """Execute the file at `path` as a new module named `name` and return it."""
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = ModuleType(name)
    mod.__file__ = path
    sys.modules[name] = mod
    try:
        loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod
