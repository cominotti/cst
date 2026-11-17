"""Static gate: enforce Hexagonal Architecture and DDD invariants via AST.

Checks every Python file under a `domain`, `ports`, or `application` layer:
- `domain` modules must not import side-effecting/I/O standard modules
  (`subprocess`, `shutil`, `socket`, `fcntl`, `ctypes`, `sqlite3`, `urllib`)
  or outer layers (`adapters`, `cli`).
- `ports` and `application` modules must not import `adapters` or `cli`.
- Every `@dataclass` defined inside a `domain` module must declare
  `frozen=True` and `slots=True`.
- `domain` and `application` modules do no I/O themselves. An allow-list
  (PURE_MODULES, PURE_NAMES) says what they may import and use, resolved
  through aliases and `from m import x`; anything else (os.listdir, pwd,
  time.time, sys.stdout, os.readv...) and the I/O builtins (`open`, `print`,
  `input`, `exec`...) fail. They reach the machine through ports, which
  adapters implement.
- Every package module is imported (directly or not) by an entry point:
  a module nothing runs is a shadow that drifts from the real code.
  ORPHAN_ALLOWED and ORPHAN_ALLOWED_FILES stay empty.
- A package's `cli.py` is the outer layer that orchestrates a program's
  commands; in the modules without an application layer (all but swap,
  doctor and cst_core) it still does I/O directly. CLI_CEILINGS caps the
  size of each one so that layer only shrinks: new logic goes into domain
  (pure) or adapters (I/O), or into an application layer with ports.

Standard library only.
"""

import ast
import os
from typing import Final, Literal
import unittest

from cst_core.domain.imports import loads_code_by_path
from tests.code_graph import entry_points, module_closure, module_of, production_python
from tests.repo_files import REPO, repo_files

type LayerRole = Literal["domain", "ports", "application"]

FORBIDDEN_DOMAIN_STDLIB: Final[frozenset[str]] = frozenset(
    {"subprocess", "shutil", "socket", "fcntl", "ctypes", "sqlite3", "urllib"}
)

# What domain and application code may import and use, an allow-list (a
# deny-list missed os.readv): pure standard modules whole, a few modules only
# for the names listed (the rest of os, time, io... reaches the machine), and
# the repository's own packages (the layer rules above check those). Imports
# are resolved through aliases and `from m import x`.
PURE_MODULES: Final[frozenset[str]] = frozenset(
    {
        "__future__", "ast", "collections", "collections.abc", "dataclasses", "enum", "fnmatch",
        "functools", "itertools", "json", "math", "operator", "posixpath", "re", "shlex", "stat",
        "string", "textwrap", "tomllib", "typing", "xml.etree.ElementTree",
    }
)  # fmt: skip
PURE_NAMES: Final[dict[str, frozenset[str]]] = {
    "os": frozenset({"path", "sep", "stat_result"}),
    "os.path": frozenset({"basename", "commonpath", "dirname", "isabs", "join", "normpath", "split", "splitext"}),
    "time": frozenset({"gmtime", "localtime", "strftime", "struct_time"}),
    "io": frozenset({"StringIO"}),
    "datetime": frozenset({"date", "datetime", "timedelta", "timezone", "tzinfo"}),
    "datetime.datetime": frozenset({"fromisoformat", "fromtimestamp", "strptime"}),
}
# Builtins that reach the terminal, files or run code.
IO_BUILTINS: Final[frozenset[str]] = frozenset({"open", "print", "input", "exec", "eval", "compile", "__import__"})
REPO_PACKAGES: Final[frozenset[str]] = frozenset(
    {"bluetooth", "btrfs", "cpu", "cst_core", "doctor", "swap", "toolchains", "vm"}
)
IO_LAYERS: Final[frozenset[str]] = frozenset({"domain", "application"})

# Packages, and single files, that no entry point imports. Every module is
# converted and wired in: both stay empty (a module nothing runs is a shadow
# that drifts from the real code).
ORPHAN_ALLOWED: Final[frozenset[str]] = frozenset()
ORPHAN_ALLOWED_FILES: Final[dict[str, str]] = {}
OUTER_LAYERS: Final[frozenset[str]] = frozenset({"adapters", "cli"})

# Code lines of each package's cli.py (cli_code_lines), a ceiling that only goes down (see the
# module docstring). A new cli.py is added here on purpose.
CLI_CEILINGS: Final[dict[str, int]] = {
    "bluetooth/cli.py": 129,
    "btrfs/bulk/cli.py": 415,
    "btrfs/chrome_cache/cli.py": 131,
    "btrfs/docker_gc/cli.py": 347,
    "btrfs/docker_volumes_nocow/cli.py": 193,
    "btrfs/snapshot/cli.py": 1069,
    "btrfs/stall_monitord/cli.py": 11,
    "btrfs/stall_report/cli.py": 158,
    "cpu/cli.py": 257,
    "cst_core/cli.py": 165,
    "doctor/cli.py": 35,
    "swap/cli.py": 112,
    "toolchains/cli.py": 1946,
    "vm/cli.py": 1299,
}


def layer_role(rel: str) -> LayerRole | None:
    """Classify a repository-relative path into its Hexagonal layer, or None."""
    if not rel.endswith(".py"):
        return None
    parts = rel.split("/")
    if "tests" in parts[:-1]:
        return None
    filename = parts[-1]
    dirs = parts[:-1]
    if filename == "domain.py" or "domain" in dirs:
        return "domain"
    if filename == "ports.py" or "ports" in dirs:
        return "ports"
    if filename == "application.py" or "application" in dirs:
        return "application"
    return None


def _is_dataclass_func(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "dataclass") or (
        isinstance(func, ast.Attribute) and func.attr == "dataclass"
    )


def _has_true_keyword(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in call.keywords)


def _forbidden_import_reason(target: str, role: LayerRole) -> str | None:
    parts = [p for p in target.split(".") if p]
    if not parts:
        return None
    if role == "domain" and parts[0] in FORBIDDEN_DOMAIN_STDLIB:
        return f"domain must not import {parts[0]!r}"
    for layer in OUTER_LAYERS:
        if layer in parts:
            return f"{role} must not import {layer!r} ({target})"
    return None


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for a Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _allowed_module(module: str) -> bool:
    return module in PURE_MODULES or module in PURE_NAMES or module.split(".", 1)[0] in REPO_PACKAGES


def _allowed_use(dotted: str) -> bool:
    """A fully qualified name (os.path.join, time.time) inside the allow-list."""
    if dotted.split(".", 1)[0] in REPO_PACKAGES:
        return True
    for module in sorted({*PURE_MODULES, *PURE_NAMES}, key=len, reverse=True):
        if dotted == module:
            return True
        if dotted.startswith(module + "."):
            first = dotted[len(module) + 1 :].split(".", 1)[0]
            if module in PURE_MODULES:
                return True
            if first in PURE_NAMES[module]:
                nested = f"{module}.{first}"
                rest = dotted[len(nested) + 1 :].split(".", 1)[0] if dotted != nested else ""
                return not rest or nested not in PURE_NAMES or rest in PURE_NAMES[nested]
            return False
    return False


def direct_io_problems(source: str, rel: str) -> list[str]:
    """Imports and uses outside the inner layers' allow-list, and I/O builtins, in one module."""
    tree = ast.parse(source, filename=rel)
    problems: list[str] = []
    bound: dict[str, str] = {}  # local name -> fully qualified name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed_module(alias.name):
                    problems.append(f"{rel}:{node.lineno}: imports {alias.name} (reach the machine through a port)")
                bound[alias.asname or alias.name.split(".", 1)[0]] = (
                    alias.name if alias.asname else alias.name.split(".", 1)[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if not (_allowed_module(node.module) and (_allowed_use(full) or _allowed_module(full))):
                    problems.append(f"{rel}:{node.lineno}: imports {full} (reach the machine through a port)")
                bound[alias.asname or alias.name] = full
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in IO_BUILTINS:
            if node.func.id not in bound:
                problems.append(f"{rel}:{node.lineno}: {node.func.id}() does I/O (use a port)")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = _dotted(node)
            if name is None:
                continue
            head, _, rest = name.partition(".")
            if head not in bound:
                continue
            full = f"{bound[head]}.{rest}"
            if not _allowed_use(full):
                problems.append(f"{rel}:{node.lineno}: {full} does I/O (use a port)")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and _dotted(node.func.value.func) in {"Path", "pathlib.Path"}
        ):
            problems.append(f"{rel}:{node.lineno}: Path(...).{node.func.attr}() does I/O (use a port)")
    return sorted(set(problems), key=lambda p: (int(p.split(":")[1]), p))


def architecture_problems(source: str, rel: str, role: LayerRole) -> list[str]:
    """Return Hexagonal/DDD architectural violations for one Python source module."""
    tree = ast.parse(source, filename=rel)
    problems: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                reason = _forbidden_import_reason(alias.name, role)
                if reason is not None:
                    problems.append(f"{rel}:{node.lineno}: {reason}")
        elif isinstance(node, ast.ImportFrom):
            targets: list[str] = []
            if node.module:
                targets.append(node.module)
            for alias in node.names:
                if alias.name != "*":
                    targets.append(f"{node.module}.{alias.name}" if node.module else alias.name)
            seen: set[str] = set()
            for target in targets:
                reason = _forbidden_import_reason(target, role)
                if reason is not None and reason not in seen:
                    seen.add(reason)
                    problems.append(f"{rel}:{node.lineno}: {reason}")
        elif role == "domain" and isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                if _is_dataclass_func(dec):
                    problems.append(
                        f"{rel}:{node.lineno}: @dataclass {node.name} in domain must set frozen=True, slots=True"
                    )
                elif isinstance(dec, ast.Call) and _is_dataclass_func(dec.func):
                    if not (_has_true_keyword(dec, "frozen") and _has_true_keyword(dec, "slots")):
                        problems.append(
                            f"{rel}:{node.lineno}: @dataclass {node.name} in domain must set frozen=True and slots=True"
                        )

    if role in IO_LAYERS:
        problems.extend(direct_io_problems(source, rel))
    return problems


def cli_code_lines(source: str) -> int:
    """Lines of a module's top-level statements, imports and the docstring left out."""
    body = ast.parse(source).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return sum(
        (node.end_lineno or node.lineno) - node.lineno + 1
        for node in body
        if not isinstance(node, (ast.Import, ast.ImportFrom))
    )


def orphan_modules(package_files: list[str], reachable: set[str]) -> list[str]:
    """Package modules no entry point reaches, outside ORPHAN_ALLOWED."""
    return [
        rel
        for rel in package_files
        if rel not in reachable and rel.split("/", 1)[0] not in ORPHAN_ALLOWED and rel not in ORPHAN_ALLOWED_FILES
    ]


# Modules that still load a monolith by its path (SourceFileLoader); each
# stage that turns the monolith into a package import removes its line.
PATH_LOADS_NOT_YET: Final[frozenset[str]] = frozenset()


class ArchitectureGateTest(unittest.TestCase):
    def test_production_code_loads_no_code_by_path(self) -> None:
        loads: dict[str, list[int]] = {}
        for rel in production_python():
            with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                lines = loads_code_by_path(fh.read(), rel)
            if lines:
                loads[rel] = lines
        problems = [
            f"{rel}:{lines[0]}: loads code by path" for rel, lines in loads.items() if rel not in PATH_LOADS_NOT_YET
        ]
        self.assertEqual(problems, [], "import the package instead:\n" + "\n".join(problems))
        stale = sorted(PATH_LOADS_NOT_YET - set(loads))
        self.assertEqual(stale, [], "no longer loads by path: remove from PATH_LOADS_NOT_YET")

    def test_layer_role_classification(self) -> None:
        self.assertEqual(layer_role("cst_core/domain/values.py"), "domain")
        self.assertEqual(layer_role("swap/domain.py"), "domain")
        self.assertEqual(layer_role("swap/ports.py"), "ports")
        self.assertEqual(layer_role("cst_core/ports/__init__.py"), "ports")
        self.assertEqual(layer_role("swap/application.py"), "application")
        self.assertIsNone(layer_role("swap/adapters.py"))
        self.assertIsNone(layer_role("swap/cli.py"))
        self.assertIsNone(layer_role("tests/test_architecture.py"))
        self.assertIsNone(layer_role("swap/tests/domain.py"))

    def test_domain_rejects_io_and_outer_layers(self) -> None:
        bad_sources = (
            "import subprocess\n",
            "from shutil import copy2\n",
            "import socket\n",
            "import fcntl\n",
            "import ctypes\n",
            "import sqlite3\n",
            "import urllib.request\n",
            "from cst_core import adapters\n",
            "from swap.adapters import LinuxSwapSystemAdapter\n",
            "from . import adapters\n",
            "from . import cli\n",
            "import swap.cli\n",
        )
        for src in bad_sources:
            with self.subTest(src=src.strip()):
                self.assertTrue(architecture_problems(src, "pkg/domain.py", "domain"))

        good_src = "from dataclasses import dataclass\nfrom typing import Literal\nimport re\n"
        self.assertEqual(architecture_problems(good_src, "pkg/domain.py", "domain"), [])

    def test_domain_requires_frozen_and_slots_dataclasses(self) -> None:
        bad_cases = (
            "from dataclasses import dataclass\n@dataclass\nclass Bad: x: int\n",
            "import dataclasses\n@dataclasses.dataclass\nclass Bad: x: int\n",
            "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass Bad: x: int\n",
            "from dataclasses import dataclass\n@dataclass(slots=True)\nclass Bad: x: int\n",
            "from dataclasses import dataclass\n@dataclass(frozen=False, slots=True)\nclass Bad: x: int\n",
        )
        for src in bad_cases:
            with self.subTest(src=src):
                self.assertTrue(architecture_problems(src, "pkg/domain.py", "domain"))

        good_cases = (
            "from dataclasses import dataclass\n@dataclass(frozen=True, slots=True)\nclass Good: x: int\n",
            "import dataclasses\n@dataclasses.dataclass(frozen=True, slots=True)\nclass Good: x: int\n",
        )
        for src in good_cases:
            with self.subTest(src=src):
                self.assertEqual(architecture_problems(src, "pkg/domain.py", "domain"), [])

    def test_ports_and_application_reject_adapters_and_cli(self) -> None:
        for role in ("ports", "application"):
            for src in (
                "from cst_core import adapters\n",
                "import swap.adapters\n",
                "from swap.cli import main\n",
                "from . import cli\n",
            ):
                with self.subTest(role=role, src=src.strip()):
                    self.assertTrue(architecture_problems(src, f"pkg/{role}.py", role))

        good_app = "from cst_core.ports import FileSystemPort\nfrom swap.domain import parse_kv\n"
        self.assertEqual(architecture_problems(good_app, "swap/application.py", "application"), [])

    def test_domain_and_application_do_no_direct_io(self) -> None:
        bad = (
            "def f(p):\n    return open(p).read()\n",
            "def f():\n    print('x')\n",
            "import os\ndef f(p):\n    return os.listdir(p)\n",
            "import os\ndef f(p):\n    return os.path.exists(p)\n",
            "import os\nX = os.environ.get('A')\n",
            "import sys\ndef f():\n    sys.stdout.write('x')\n",
            "import time\ndef f():\n    return time.time()\n",
            "from pathlib import Path\ndef f(p):\n    return Path(p).read_text()\n",
            "import subprocess\ndef f():\n    subprocess.run(['x'], check=False)\n",
            # Forms a deny-list of call names missed: other os functions, aliases, from-imports, pwd.
            "import os\ndef f(fd):\n    return os.readv(fd, [])\n",
            "import os as o\ndef f(p):\n    return o.listdir(p)\n",
            "from os import listdir\n",
            "from os.path import exists\n",
            "import os.path as osp\ndef f(p):\n    return osp.isfile(p)\n",
            "import pwd\n",
            "from datetime import datetime\ndef f():\n    return datetime.now()\n",
            "import time as t\ndef f():\n    return t.monotonic()\n",
            "def f(p):\n    return __import__('os').listdir(p)\n",
        )
        for src in bad:
            with self.subTest(src=src):
                self.assertTrue(architecture_problems(src, "pkg/application.py", "application"))
                self.assertTrue(direct_io_problems(src, "pkg/domain.py"))
        good = (
            "import os\ndef f(a, b):\n    return os.path.join(a, b), os.path.basename(a), os.sep\n",
            "def f(host, p):\n    return host.read_text(p), host.listdir(p)\n",
            "import time\ndef f(t):\n    return time.strftime('%Y', time.gmtime(t))\n",
            "from os.path import join as j\nimport xml.etree.ElementTree as ET\ndef f(a):\n    return ET.fromstring(j(a, 'b'))\n",
            "from datetime import datetime, timezone\ndef f(s):\n    return datetime.fromisoformat(s).astimezone(timezone.utc)\n",
            "from collections.abc import Mapping\nfrom cst_core.domain.values import GIB\nimport stat\nX = stat.S_ISDIR\n",
        )
        for src in good:
            with self.subTest(src=src):
                self.assertEqual(direct_io_problems(src, "pkg/application.py"), [])
        # Adapters may do I/O.
        self.assertEqual(architecture_problems(bad[0], "pkg/adapters.py", "domain")[:0], [])

    def test_every_package_module_is_reached_from_an_entry_point(self) -> None:
        files = [rel for rel in production_python() if module_of(rel) is not None]
        problems = orphan_modules(files, module_closure(entry_points()))
        self.assertEqual(problems, [], "modules no entry point imports:\n" + "\n".join(problems))
        self.assertEqual(orphan_modules(["swap/x.py", *ORPHAN_ALLOWED_FILES], set()), ["swap/x.py"])

    def test_orphan_allowlist_only_holds_packages_with_orphans(self) -> None:
        files = [rel for rel in production_python() if module_of(rel) is not None]
        reach = module_closure(entry_points())
        stale = sorted(p for p in ORPHAN_ALLOWED if all(r in reach for r in files if r.startswith(p + "/")))
        self.assertEqual(stale, [], "packages wired in since: remove them from ORPHAN_ALLOWED")
        wired = sorted(rel for rel in ORPHAN_ALLOWED_FILES if rel in reach or rel not in files)
        self.assertEqual(wired, [], "files wired in (or gone) since: remove them from ORPHAN_ALLOWED_FILES")

    def test_cli_modules_do_not_grow(self) -> None:
        sizes: dict[str, int] = {}
        for rel in production_python():
            if rel.endswith("/cli.py"):
                with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                    sizes[rel] = cli_code_lines(fh.read())
        self.assertEqual(sorted(sizes), sorted(CLI_CEILINGS), "every package cli.py has a ceiling, and only those")
        over = [
            f"{rel}: {n} lines, ceiling {CLI_CEILINGS[rel]}"
            for rel, n in sorted(sizes.items())
            if n > CLI_CEILINGS[rel]
        ]
        self.assertEqual(over, [], "cli.py grew: put new logic in domain or adapters")
        self.assertEqual(
            cli_code_lines('"""Doc."""\nimport os\nfrom a import (\n    b,\n)\n\n\ndef f():\n    return 1\n'), 2
        )

    def test_repository_layers_obey_hexagonal_boundaries(self) -> None:
        problems: list[str] = []
        for rel in repo_files():
            role = layer_role(rel)
            if role is None:
                continue
            with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                problems.extend(architecture_problems(fh.read(), rel, role))
        self.assertEqual(problems, [], "Hexagonal architecture violations:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
