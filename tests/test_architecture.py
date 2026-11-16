"""Static gate: enforce Hexagonal Architecture and DDD invariants via AST.

Checks every Python file under a `domain`, `ports`, or `application` layer:
- `domain` modules must not import side-effecting/I/O standard modules
  (`subprocess`, `shutil`, `socket`, `fcntl`, `ctypes`, `sqlite3`, `urllib`)
  or outer layers (`adapters`, `cli`).
- `ports` and `application` modules must not import `adapters` or `cli`.
- Every `@dataclass` defined inside a `domain` module must declare
  `frozen=True` and `slots=True`.

Standard library only.
"""

import ast
import os
from typing import Final, Literal
import unittest

from tests.repo_files import REPO, repo_files

type LayerRole = Literal["domain", "ports", "application"]

FORBIDDEN_DOMAIN_STDLIB: Final[frozenset[str]] = frozenset(
    {"subprocess", "shutil", "socket", "fcntl", "ctypes", "sqlite3", "urllib"}
)
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

    return problems


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
