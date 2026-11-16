"""Static gate: enforce Hexagonal Architecture and DDD invariants via AST.

Checks every Python file under a `domain`, `ports`, or `application` layer:
- `domain` modules must not import side-effecting/I/O standard modules
  (`subprocess`, `shutil`, `socket`, `fcntl`, `ctypes`, `sqlite3`, `urllib`)
  or outer layers (`adapters`, `cli`).
- `ports` and `application` modules must not import `adapters` or `cli`.
- Every `@dataclass` defined inside a `domain` module must declare
  `frozen=True` and `slots=True`.
- `domain` and `application` modules do no I/O themselves: no call to
  `open`, `print`, `input`, the file and process functions of `os` and
  `os.path`, `subprocess`, `shutil`, `time.time/sleep`, `sys.exit`/streams,
  nor `Path(...)` file methods (DIRECT_IO_CALLS lists them). They reach the
  machine through ports, which adapters implement.
- Every package module is imported (directly or not) by an entry point:
  a module nothing runs is a shadow that drifts from the real code.
  ORPHAN_ALLOWED names the packages a later stage of the conversion wires in.

Standard library only.
"""

import ast
import os
from typing import Final, Literal
import unittest

from tests.code_graph import entry_points, module_closure, module_of, production_python
from tests.repo_files import REPO, repo_files

type LayerRole = Literal["domain", "ports", "application"]

FORBIDDEN_DOMAIN_STDLIB: Final[frozenset[str]] = frozenset(
    {"subprocess", "shutil", "socket", "fcntl", "ctypes", "sqlite3", "urllib"}
)

# Calls that reach the machine, by the name they are called through.
_OS_IO: Final = frozenset(
    {
        "access", "chdir", "chmod", "chown", "close", "environ", "execv", "execvp", "fdopen", "fsync",
        "getcwd", "getenv", "geteuid", "getpid", "getuid", "kill", "link", "listdir", "lstat", "makedirs",
        "mkdir", "open", "popen", "read", "readlink", "remove", "rename", "replace", "rmdir", "scandir",
        "stat", "statvfs", "symlink", "system", "umask", "unlink", "utime", "walk", "write",
    }
)  # fmt: skip
_OS_PATH_IO: Final = frozenset(
    {
        "abspath", "exists", "expanduser", "getatime", "getctime", "getmtime", "getsize", "isdir",
        "isfile", "islink", "ismount", "lexists", "realpath", "samefile",
    }
)  # fmt: skip
_PATH_IO: Final = frozenset(
    {
        "chmod", "exists", "glob", "is_dir", "is_file", "is_symlink", "iterdir", "lstat", "mkdir", "open",
        "read_bytes", "read_text", "readlink", "rename", "replace", "resolve", "rglob", "rmdir", "stat",
        "symlink_to", "touch", "unlink", "write_bytes", "write_text",
    }
)  # fmt: skip
DIRECT_IO_CALLS: Final[dict[str, frozenset[str]]] = {
    "": frozenset({"open", "print", "input", "exec", "eval"}),
    "os": _OS_IO,
    "os.path": _OS_PATH_IO,
    "subprocess": frozenset({"run", "Popen", "call", "check_call", "check_output"}),
    "shutil": frozenset({"copy", "copy2", "copyfile", "copytree", "move", "rmtree", "which", "disk_usage"}),
    "time": frozenset({"time", "sleep", "monotonic", "time_ns"}),
    "sys": frozenset({"exit", "stdout", "stderr", "stdin", "argv"}),
}
IO_LAYERS: Final[frozenset[str]] = frozenset({"domain", "application"})

# Packages whose modules no entry point imports yet (their programs are still
# monoliths). Each later stage of the conversion wires its package in and
# removes it from here.
ORPHAN_ALLOWED: Final[frozenset[str]] = frozenset({"btrfs", "cpu", "toolchains", "vm"})
# Shared cst_core modules made for the modules a later stage converts, with
# the programs that will import them. A stage that wires one in removes it.
ORPHAN_ALLOWED_FILES: Final[dict[str, str]] = {
    "cst_core/adapters/files.py": "btrfs-stall-monitord, cst-toolchains (read_text/read_int/read_bytes)",
    "cst_core/adapters/journal.py": "btrfs-bulk, btrfs-snapshot-core, btrfs-stall-monitord, docker-gc, "
    "docker-volumes-nocow, bt-ldac-watch (log)",
    "cst_core/adapters/locks.py": "btrfs-bulk, btrfs-snapshot-core, docker-gc (take_lock)",
    "cst_core/adapters/nocow.py": "btrfs-bulk, docker-volumes-nocow, cst-vm (set_nocow/inode_flags)",
    "cst_core/domain/units.py": "cst-vm, cst-toolchains (render_timer/render_unit_file)",
}
OUTER_LAYERS: Final[frozenset[str]] = frozenset({"adapters", "cli"})


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


def direct_io_problems(source: str, rel: str) -> list[str]:
    """Calls (and stream/env uses) of DIRECT_IO_CALLS in one module."""
    tree = ast.parse(source, filename=rel)
    problems: list[str] = []
    for node in ast.walk(tree):
        target: ast.expr | None = None
        if isinstance(node, ast.Call):
            target = node.func
            # Path(...).read_text() and the like.
            if (
                isinstance(target, ast.Attribute)
                and target.attr in _PATH_IO
                and isinstance(target.value, ast.Call)
                and _dotted(target.value.func) in {"Path", "pathlib.Path"}
            ):
                problems.append(f"{rel}:{node.lineno}: Path(...).{target.attr}() does I/O (use a port)")
                continue
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            target = node  # os.environ, sys.stdout, sys.argv: used without a call
        if target is None:
            continue
        name = _dotted(target)
        if name is None:
            continue
        mod, _, attr = name.rpartition(".")
        if attr in DIRECT_IO_CALLS.get(mod, frozenset()) and (isinstance(node, ast.Call) or mod in {"os", "sys"}):
            problems.append(f"{rel}:{target.lineno}: {name} does I/O (use a port)")
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


def orphan_modules(package_files: list[str], reachable: set[str]) -> list[str]:
    """Package modules no entry point reaches, outside ORPHAN_ALLOWED."""
    return [
        rel
        for rel in package_files
        if rel not in reachable and rel.split("/", 1)[0] not in ORPHAN_ALLOWED and rel not in ORPHAN_ALLOWED_FILES
    ]


class ArchitectureGateTest(unittest.TestCase):
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
        )
        for src in bad:
            with self.subTest(src=src):
                self.assertTrue(architecture_problems(src, "pkg/application.py", "application"))
                self.assertTrue(direct_io_problems(src, "pkg/domain.py"))
        good = (
            "import os\ndef f(a, b):\n    return os.path.join(a, b), os.path.basename(a), os.sep\n",
            "def f(host, p):\n    return host.read_text(p), host.listdir(p)\n",
            "import time\ndef f(t):\n    return time.strftime('%Y', time.gmtime(t))\n",
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
        self.assertEqual(orphan_modules(["swap/x.py", "btrfs/y.py"], set()), ["swap/x.py"])

    def test_orphan_allowlist_only_holds_packages_with_orphans(self) -> None:
        files = [rel for rel in production_python() if module_of(rel) is not None]
        reach = module_closure(entry_points())
        stale = sorted(p for p in ORPHAN_ALLOWED if all(r in reach for r in files if r.startswith(p + "/")))
        self.assertEqual(stale, [], "packages wired in since: remove them from ORPHAN_ALLOWED")
        wired = sorted(rel for rel in ORPHAN_ALLOWED_FILES if rel in reach or rel not in files)
        self.assertEqual(wired, [], "files wired in (or gone) since: remove them from ORPHAN_ALLOWED_FILES")

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
