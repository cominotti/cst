# SPDX-License-Identifier: MPL-2.0
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
- Every package module has one role (`module_role`): domain, ports,
  application, adapters, presenter, wiring, cli or entry. A module with
  none is in UNLAYERED_ALLOWED with its reason; that table only shrinks.
- The I/O a module does itself outside its adapters ("hard I/O": the lines
  direct_io_problems reports, with print, sys.stdout/stderr/argv/exit and
  argparse allowed, the bootstrap left out) has a budget per package and
  layer, IO_BUDGETS, that only goes down (tests/budgets.py). Moving code to
  an adapter lowers it; moving it from cli.py into another unlayered module
  does not hide it.
- A package in HEXAGONAL_PACKAGES has a cli.py and a presenter.py with no
  hard I/O at all, and only its composition point (wiring.py) and its
  adapters import an adapter.
- No module imports a private name (`_x`) from another module of the
  repository: a name another module needs is public.
- Shared primitives have one owner each (PRIMITIVE_OWNERS: running a
  command, replacing or syncing a file, temporary files, locks, the NOCOW
  ioctl), and so do the literals that betray a copy of one
  (PRIMITIVE_LITERALS: the container markers, flatpak-spawn, the libvirt
  URI, LC_ALL, restorecon, systemd-escape). Other modules use them through
  cst_core; today's other sites have a budget per package
  (PRIMITIVE_BUDGETS, LITERAL_BUDGETS) that only goes down.
- Every public top-level name of cst_core's shared layers has a production
  user outside its file; CORE_NAMES_WITHOUT_USER lists the exceptions with
  their reason, and only shrinks.

Standard library only.
"""

import ast
from collections.abc import Callable, Mapping
import os
from typing import Final, Literal
import unittest

from cst_core.domain.imports import loads_code_by_path
from tests.budgets import budget_problems, format_table, package_of
from tests.code_graph import entry_points, is_python, module_closure, module_of, production_python
from tests.repo_files import REPO, repo_files
from tests.test_entrypoints import BOOTSTRAP

type LayerRole = Literal["domain", "ports", "application"]
type ModuleRole = Literal["domain", "ports", "application", "adapters", "presenter", "wiring", "cli", "entry"]

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
    "btrfs/bulk/cli.py": 404,
    "btrfs/chrome_cache/cli.py": 125,
    "btrfs/docker_gc/cli.py": 322,
    "btrfs/docker_volumes_nocow/cli.py": 193,
    "btrfs/snapshot/cli.py": 1069,
    "btrfs/stall_monitord/cli.py": 11,
    "btrfs/stall_report/cli.py": 158,
    "cpu/cli.py": 245,
    "cst_core/cli.py": 165,
    "doctor/cli.py": 35,
    "swap/cli.py": 112,
    "toolchains/cli.py": 1910,
    "vm/cli.py": 1288,
}


# Package modules that are programs of their own (`python3 -I -B cst_core/installer.py`).
ENTRY_MODULES: Final[frozenset[str]] = frozenset({"cst_core/installer.py"})

# Package modules with no role yet, and why. Each holds use cases and their
# I/O together; splitting one into its layers removes its line. Only shrinks.
UNLAYERED_ALLOWED: Final[dict[str, str]] = {
    "bluetooth/ldac_watch.py": "the LDAC watcher: its loop, the pw-dump reads and the bitrate changes in one module",
    "btrfs/bulk/items.py": "the move and recovery of ~/Bulk items: the journal steps with their file I/O",
    "btrfs/bulk/steam.py": "the Steam library move: pure VDF edits, the journal steps and the copies together",
    "btrfs/stall_monitord/monitor.py": "the stall daemon's sampling loop: its /proc and /sys reads with the episode rules",
    "btrfs/stall_report/analysis.py": "the report's analysis of the recorded episodes, with a few reads of its own",
}

# What a cli.py or presenter.py may use although direct_io_problems reports
# it: the terminal, argv, the exit status and argparse.
CLI_ALLOWED: Final[frozenset[str]] = frozenset(
    {"print", "sys.stdout", "sys.stderr", "sys.argv", "sys.exit", "argparse"}
)

# The roles whose hard I/O has a budget. adapters do I/O by design; domain
# and application have none (test_repository_layers_obey_hexagonal_boundaries).
IO_BUDGETED_ROLES: Final[frozenset[str]] = frozenset({"ports", "presenter", "wiring", "cli", "entry", "unlayered"})

# Hard I/O lines per "<package> <role>" (see the module docstring and
# tests/budgets.py). Only goes down; a missing key is 0.
IO_BUDGETS: Final[dict[str, int]] = {
    "bluetooth unlayered": 29,
    "btrfs/bulk cli": 9,
    "btrfs/bulk unlayered": 110,
    "btrfs/chrome_cache cli": 7,
    "btrfs/docker_gc cli": 29,
    "btrfs/docker_volumes_nocow cli": 39,
    "btrfs/snapshot cli": 19,
    "btrfs/stall_monitord cli": 3,
    "btrfs/stall_monitord unlayered": 52,
    "btrfs/stall_report cli": 1,
    "btrfs/stall_report unlayered": 4,
    "cpu cli": 36,
    "cst_core cli": 6,
    "cst_core entry": 3,
    "doctor cli": 1,
    "swap cli": 3,
    "toolchains cli": 62,
    "vm cli": 82,
}

# Packages whose cli.py and presenter.py do no I/O, and their composition
# point: the one module, besides the adapters, that imports adapters and
# builds them. A package joins once hexagonal_problems() finds nothing in it.
HEXAGONAL_PACKAGES: Final[dict[str, str]] = {}


# A shared primitive -> the cst_core modules that own it. Anywhere else, a
# call is a second implementation: use the owner's function or adapter.
# "fcntl.ioctl" counts only with an FS_IOC_* request (the NOCOW flags).
_RUNNER: Final = frozenset({"cst_core/adapters/__init__.py"})
_FILES: Final = frozenset({"cst_core/adapters/files.py"})
PRIMITIVE_OWNERS: Final[dict[str, frozenset[str]]] = {
    "subprocess.run": _RUNNER,
    "subprocess.Popen": _RUNNER | {"cst_core/adapters/dispatch.py"},
    "subprocess.call": _RUNNER,
    "subprocess.check_call": _RUNNER,
    "subprocess.check_output": _RUNNER,
    "os.replace": _FILES,
    "os.fsync": _FILES,
    "tempfile.mkstemp": _FILES,
    "tempfile.NamedTemporaryFile": _FILES,
    "fcntl.flock": frozenset({"cst_core/adapters/locks.py"}),
    "fcntl.ioctl": frozenset({"cst_core/adapters/nocow.py"}),
}

# String literals that betray a re-implementation of a primitive -> owners.
PRIMITIVE_LITERALS: Final[dict[str, frozenset[str]]] = {
    "/run/.containerenv": _RUNNER,
    "/.dockerenv": _RUNNER,
    "flatpak-spawn": _RUNNER,
    "qemu:///system": _RUNNER,
    "LC_ALL": _RUNNER,
    "restorecon": _FILES,
    "systemd-escape": _RUNNER,
}

# Calls of a primitive outside its owners, per "<package> <primitive>"
# (tests/budgets.py). Only goes down.
PRIMITIVE_BUDGETS: Final[dict[str, int]] = {
    "bluetooth subprocess.Popen": 1,
    "bluetooth subprocess.run": 1,
    "btrfs/bulk os.fsync": 7,
    "btrfs/bulk subprocess.Popen": 1,
    "btrfs/bulk subprocess.run": 13,
    "btrfs/chrome_cache os.fsync": 1,
    "btrfs/docker_gc os.fsync": 1,
    "btrfs/docker_gc os.replace": 1,
    "btrfs/docker_gc subprocess.Popen": 1,
    "btrfs/docker_gc subprocess.run": 2,
    "btrfs/docker_volumes_nocow os.fsync": 1,
    "btrfs/docker_volumes_nocow os.replace": 1,
    "btrfs/docker_volumes_nocow subprocess.run": 5,
    "btrfs/snapshot os.fsync": 2,
    "btrfs/snapshot os.replace": 1,
    "btrfs/snapshot subprocess.run": 1,
    "btrfs/stall_monitord subprocess.Popen": 1,
    "btrfs/stall_report subprocess.run": 3,
    "cpu subprocess.Popen": 1,
    "cpu subprocess.run": 1,
    "cpu tempfile.NamedTemporaryFile": 1,
    "cst_core subprocess.run": 1,
    "toolchains fcntl.flock": 3,
    "toolchains os.replace": 1,
    "toolchains subprocess.run": 6,
    "toolchains tempfile.mkstemp": 1,
    "vm os.replace": 1,
    "vm subprocess.Popen": 1,
    "vm tempfile.mkstemp": 2,
}

# Uses of a primitive's literal outside its owners (docstrings aside), per
# "<package> <literal>". Only goes down.
LITERAL_BUDGETS: Final[dict[str, int]] = {
    "bluetooth LC_ALL": 1,
    "btrfs/bulk restorecon": 1,
    "btrfs/stall_report flatpak-spawn": 1,
    "cpu qemu:///system": 1,
    "cst_core /.dockerenv": 1,
    "cst_core /run/.containerenv": 1,
    "doctor /.dockerenv": 1,
    "doctor /run/.containerenv": 1,
    "doctor flatpak-spawn": 1,
    "doctor qemu:///system": 1,
    "toolchains /.dockerenv": 1,
    "toolchains /run/.containerenv": 1,
    "toolchains flatpak-spawn": 4,
    "vm qemu:///system": 3,
    "vm restorecon": 2,
}

# The cst_core layers whose public names are shared primitives.
CORE_SHARED_PREFIXES: Final = ("cst_core/domain/", "cst_core/ports/", "cst_core/adapters/", "cst_core/application/")

# Public top-level names of those layers with no production user outside
# their file ("path:name" -> why). Adopt the name where a module keeps its
# own copy, make it private when only its file uses it, or delete it; then
# remove its line. Only shrinks.
CORE_NAMES_WITHOUT_USER: Final[dict[str, str]] = {
    "cst_core/adapters/__init__.py:SystemClockAdapter": "the adapter of ClockPort; no program takes a clock through the port yet",
    "cst_core/adapters/files.py:FileOwner": "the type of write_atomic's owner argument; callers pass the literal",
    "cst_core/adapters/nocow.py:FS_IOC_GETFLAGS": "an ioctl request used inside nocow.py and by its tests",
    "cst_core/adapters/nocow.py:FS_IOC_SETFLAGS": "an ioctl request used inside nocow.py and by its tests",
    "cst_core/adapters/runtime.py:SYSTEM_PYTHON": "used inside runtime.py and by the tests",
    "cst_core/adapters/runtime.py:running_archive": "used inside runtime.py and by the tests",
    "cst_core/adapters/zipapp.py:ZIP_FIXED_TIME": "used only inside zipapp.py",
    "cst_core/adapters/zipapp.py:ZIPAPP_SHEBANG": "used only inside zipapp.py",
    "cst_core/adapters/zipapp.py:repo_packages": "used only inside zipapp.py",
    "cst_core/adapters/zipapp.py:package_sources": "used only inside zipapp.py",
    "cst_core/adapters/zipapp.py:import_closure": "used inside zipapp.py and by the tests",
    "cst_core/adapters/zipapp.py:zipapp_members": "used only inside zipapp.py",
    "cst_core/application/install.py:PayloadFn": "the type of InstallService's payload function, used only inside install.py",
    "cst_core/domain/dispatch.py:ModuleSpec": "the record of MODULES, used only inside dispatch.py",
    "cst_core/domain/dispatch.py:USAGE_HEADER": "used only inside dispatch.py (format_cst_usage)",
    "cst_core/domain/imports.py:imported_modules": "read by the static gates in tests/ (code_graph, distribution)",
    "cst_core/domain/imports.py:loads_code_by_path": "read by the static gates in tests/ (architecture, distribution)",
    "cst_core/domain/install.py:parse_manifest": "builds INSTALL_MANIFEST inside install.py; the tests parse other tables with it",
    "cst_core/domain/install.py:SETUP_ENTRYPOINTS": "read by the static gates in tests/",
    "cst_core/domain/install.py:CHECKOUT_ONLY_EXECUTABLES": "read by the static gates in tests/",
    "cst_core/domain/install.py:CHECKOUT_ONLY_FILES": "read by the static gates in tests/",
    "cst_core/domain/install.py:manifest_by_dest": "no user: adopt it where a module looks a dest up by hand, or delete it",
    "cst_core/domain/install.py:python_entry_srcs": "read by the static gates in tests/",
    "cst_core/domain/plan.py:Ok": "a result type nothing returns yet: adopt it or delete it",
    "cst_core/domain/plan.py:Err": "a result type nothing returns yet: adopt it or delete it",
    "cst_core/domain/plan.py:Outcome": "a result type nothing returns yet: adopt it or delete it",
    "cst_core/domain/text.py:Assignment": "the record parse_shell_assignments returns, used only inside text.py",
    "cst_core/domain/values.py:KIB": "no user (MIB and GIB are the units in use)",
    "cst_core/domain/values.py:MIB": "used by the tests only",
    "cst_core/domain/values.py:Duration": "toolchains parses durations itself (parse_duration): adopt it there or delete it",
    "cst_core/domain/values.py:CpuSet": "cpu parses CPU lists itself (cpus_of): adopt it there or delete it",
    "cst_core/ports/__init__.py:HostPathPort": "a port no program takes yet: adopt it or delete it",
    "cst_core/ports/__init__.py:ClockPort": "a port no program takes yet: adopt it or delete it",
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


def module_role(rel: str) -> ModuleRole | None:
    """The role of a production module of a package (None: it has none, see UNLAYERED_ALLOWED)."""
    inner = layer_role(rel)
    if inner is not None:
        return inner
    parts = rel.split("/")
    filename, dirs = parts[-1], parts[:-1]
    if filename == "adapters.py" or "adapters" in dirs:
        return "adapters"
    if not filename.endswith(".py") or filename == "__init__.py" or rel in ENTRY_MODULES:
        return "entry"
    named: dict[str, ModuleRole] = {"presenter.py": "presenter", "wiring.py": "wiring", "cli.py": "cli"}
    return named.get(filename)


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


def _extra_allowed(dotted: str, allowed: frozenset[str]) -> bool:
    """`dotted` is one of the `allowed` names, or inside one of them."""
    return any(dotted == a or dotted.startswith(a + ".") for a in allowed)


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


def direct_io_problems(source: str, rel: str, allowed: frozenset[str] = frozenset()) -> list[str]:
    """Imports and uses outside the inner layers' allow-list, and I/O builtins, in one module.

    `allowed` adds names (builtins such as "print", modules, "sys.argv") that
    are not reported, with their attributes; importing their module is fine.
    """
    tree = ast.parse(source, filename=rel)
    allowed_modules = {a.split(".", 1)[0] for a in allowed}
    problems: list[str] = []
    bound = _bindings(tree)  # local name -> fully qualified name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed_module(alias.name) and alias.name not in allowed_modules:
                    problems.append(f"{rel}:{node.lineno}: imports {alias.name} (reach the machine through a port)")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if _extra_allowed(full, allowed):
                    pass
                elif not (_allowed_module(node.module) and (_allowed_use(full) or _allowed_module(full))):
                    problems.append(f"{rel}:{node.lineno}: imports {full} (reach the machine through a port)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in IO_BUILTINS:
            if node.func.id not in bound and node.func.id not in allowed:
                problems.append(f"{rel}:{node.lineno}: {node.func.id}() does I/O (use a port)")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = _dotted(node)
            if name is None:
                continue
            head, _, rest = name.partition(".")
            if head not in bound:
                continue
            full = f"{bound[head]}.{rest}"
            if not _allowed_use(full) and not _extra_allowed(full, allowed):
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


def hard_io_lines(source: str, rel: str) -> list[int]:
    """The lines where a module does I/O itself (CLI_ALLOWED aside, the bootstrap left out)."""
    blanked = source.replace(BOOTSTRAP, "\n" * BOOTSTRAP.count("\n"), 1)  # same line numbers
    found = direct_io_problems(blanked, rel, CLI_ALLOWED)
    return sorted({int(p.split(":")[1]) for p in found})


def _read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def package_modules() -> list[str]:
    """Production Python files of the packages: package modules and their extensionless entries."""
    return [rel for rel in production_python() if "/" in rel and not rel.startswith(".")]


def io_measure(sources: dict[str, str]) -> dict[str, int]:
    """Hard I/O lines per "<package> <role>" for the budgeted roles."""
    out: dict[str, int] = {}
    for rel, source in sources.items():
        role = module_role(rel) or "unlayered"
        if role not in IO_BUDGETED_ROLES:
            continue
        key = f"{package_of(rel)} {role}"
        out[key] = out.get(key, 0) + len(hard_io_lines(source, rel))
    return out


def adapter_import_lines(source: str, rel: str) -> list[int]:
    """Lines that import an adapters module (any package's) or a name from one."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source, filename=rel)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            names = [base, *(f"{base}.{a.name}" for a in node.names)]
        else:
            continue
        if any("adapters" in name.replace(".", " ").split() for name in names):
            lines.append(node.lineno)
    return sorted(set(lines))


def hexagonal_problems(package: str, composition: str, sources: dict[str, str]) -> list[str]:
    """What keeps `package` (its modules' sources by path) from the hexagonal rules, as text lines."""
    problems: list[str] = []
    if composition not in sources:
        problems.append(f"{composition}: the composition point of {package} does not exist")
    for rel, source in sorted(sources.items()):
        role = module_role(rel)
        if role == "adapters" or rel == composition:
            continue
        problems += [
            f"{rel}:{n}: imports an adapter; only {composition} and the adapters may"
            for n in adapter_import_lines(source, rel)
        ]
        if role in ("cli", "presenter"):
            problems += [f"{rel}:{n}: does I/O itself (move it into an adapter)" for n in hard_io_lines(source, rel)]
    return problems


def private_imports(source: str, rel: str) -> list[str]:
    """`from <repository module> import _name`: a private name used outside its module."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename=rel)):
        if not isinstance(node, ast.ImportFrom) or node.module in ("__future__",):
            continue
        ours = node.level > 0 or (node.module or "").split(".", 1)[0] in {*REPO_PACKAGES, "tests"}
        for alias in node.names:
            if ours and alias.name.startswith("_") and not alias.name.startswith("__"):
                problems.append(f"{rel}:{node.lineno}: imports the private name {alias.name} from {node.module}")
    return problems


def _bindings(tree: ast.Module) -> dict[str, str]:
    """Local name -> fully qualified name, for every import of a module."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".", 1)[0]] = (
                    alias.name if alias.asname else alias.name.split(".", 1)[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def primitive_calls(source: str, rel: str) -> list[tuple[int, str]]:
    """(line, primitive) for every call of a PRIMITIVE_OWNERS primitive, through aliases and from-imports."""
    tree = ast.parse(source, filename=rel)
    bound = _bindings(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        head, _, rest = name.partition(".")
        if head not in bound:
            continue
        full = f"{bound[head]}.{rest}" if rest else bound[head]
        if full not in PRIMITIVE_OWNERS:
            continue
        if full == "fcntl.ioctl" and not any(
            "FS_IOC_" in (_dotted(arg) or "") for arg in node.args[1:2] if isinstance(arg, ast.expr)
        ):
            continue
        out.append((node.lineno, full))
    return sorted(out)


def _docstrings(tree: ast.Module) -> set[int]:
    """id() of the docstring constants of a module, its classes and its functions."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def primitive_literals(source: str, rel: str) -> list[tuple[int, str]]:
    """(line, literal) for every string constant (docstrings aside) holding a PRIMITIVE_LITERALS literal."""
    tree = ast.parse(source, filename=rel)
    docs = _docstrings(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out += [(node.lineno, lit) for lit in PRIMITIVE_LITERALS if lit in node.value]
    return sorted(out)


def primitive_measure(
    sources: dict[str, str],
    find: Callable[[str, str], list[tuple[int, str]]],
    owners: Mapping[str, frozenset[str]],
) -> dict[str, int]:
    """Sites outside their owners per "<package> <primitive>"."""
    out: dict[str, int] = {}
    for rel, source in sources.items():
        for _, what in find(source, rel):
            if rel not in owners[what]:
                key = f"{package_of(rel)} {what}"
                out[key] = out.get(key, 0) + 1
    return out


def top_level_names(source: str, rel: str) -> list[str]:
    """Public names a module defines at its top level (functions, classes, assignments, type aliases)."""
    names: list[str] = []
    for node in ast.parse(source, filename=rel).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.TypeAlias):
            names.append(node.name.id)
    return [n for n in names if not n.startswith("_")]


def referenced_names(source: str, rel: str) -> set[str]:
    """Every name a module refers to: names, attributes and the names it imports."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=rel)):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            out |= {alias.name for alias in node.names}
    return out


def core_names_without_user(sources: dict[str, str]) -> list[str]:
    """ "path:name" of every public cst_core shared name no other production module refers to."""
    refs = {rel: referenced_names(src, rel) for rel, src in sources.items()}
    out: list[str] = []
    for rel, src in sorted(sources.items()):
        if not rel.startswith(CORE_SHARED_PREFIXES):
            continue
        for name in top_level_names(src, rel):
            if not any(name in names for other, names in refs.items() if other != rel):
                out.append(f"{rel}:{name}")
    return out


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
        self.assertEqual(over, [], "cli.py grew: put new logic in domain, application (with ports) or adapters")
        self.assertEqual(
            cli_code_lines('"""Doc."""\nimport os\nfrom a import (\n    b,\n)\n\n\ndef f():\n    return 1\n'), 2
        )

    def test_module_roles(self) -> None:
        for rel, role in (
            ("swap/domain.py", "domain"),
            ("cst_core/adapters/files.py", "adapters"),
            ("swap/adapters.py", "adapters"),
            ("swap/presenter.py", "presenter"),
            ("vm/wiring.py", "wiring"),
            ("vm/cli.py", "cli"),
            ("vm/__init__.py", "entry"),
            ("vm/cst-vm", "entry"),
            ("cst_core/installer.py", "entry"),
            ("btrfs/bulk/steam.py", None),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(module_role(rel), role)

    def test_every_package_module_has_a_role(self) -> None:
        files = package_modules()
        unlayered = sorted(rel for rel in files if module_role(rel) is None)
        problems = [rel for rel in unlayered if rel not in UNLAYERED_ALLOWED]
        self.assertEqual(
            problems,
            [],
            "modules with no role: name them domain.py, ports.py, application.py, adapters.py, presenter.py, "
            "wiring.py or cli.py (or put them in such a folder):\n" + "\n".join(problems),
        )
        stale = sorted(set(UNLAYERED_ALLOWED) - set(unlayered))
        self.assertEqual(stale, [], "these have a role now (or are gone): remove them from UNLAYERED_ALLOWED")

    def test_cli_allowed_names_are_not_io(self) -> None:
        src = (
            "import argparse\nimport sys\nfrom sys import argv\n"
            "def main():\n    p = argparse.ArgumentParser()\n    print('x', file=sys.stderr)\n"
            "    sys.stdout.write(argv[0])\n    sys.exit(2)\n"
        )
        self.assertEqual(hard_io_lines(src, "pkg/cli.py"), [])
        self.assertTrue(direct_io_problems(src, "pkg/application.py"), "the inner layers still refuse them")
        bad = "import os\nimport sys\ndef main():\n    sys.stdin.read()\n    return os.environ['A']\n"
        self.assertEqual(hard_io_lines(bad, "pkg/cli.py"), [4, 5])

    def test_the_bootstrap_is_not_counted(self) -> None:
        self.assertEqual(hard_io_lines(BOOTSTRAP + "import os\nX = os.getcwd()\n", "pkg/cli.py"), [11])

    def test_io_budgets(self) -> None:
        measured = io_measure({rel: _read(rel) for rel in package_modules()})
        problems = budget_problems(measured, IO_BUDGETS, "hard I/O lines")
        self.assertEqual(
            problems,
            [],
            "move the I/O into an adapter; measured table for IO_BUDGETS:\n" + format_table(measured),
        )

    def test_io_budget_keys(self) -> None:
        got = io_measure(
            {
                "vm/cli.py": "import os\nos.getcwd()\nos.listdir('.')\n",
                "vm/adapters.py": "import os\nos.getcwd()\n",
                "btrfs/bulk/steam.py": "import os\nos.getcwd()\n",
                "cst_core/installer.py": "import os\nos.geteuid()\n",
            }
        )
        self.assertEqual(got, {"vm cli": 2, "btrfs/bulk unlayered": 1, "cst_core entry": 1})

    def test_hexagonal_problems(self) -> None:
        good = {
            "vm/cli.py": "import sys\nfrom vm.wiring import build\ndef main():\n    print(build().run(sys.argv))\n",
            "vm/wiring.py": "from vm.adapters import Host\nfrom vm.application import App\ndef build():\n    return App(Host())\n",
            "vm/adapters.py": "import os\nfrom cst_core.adapters import files\nclass Host:\n    pass\n",
            "vm/application.py": "class App:\n    pass\n",
            "vm/presenter.py": "def render(x):\n    print(x)\n",
        }
        self.assertEqual(hexagonal_problems("p", "vm/wiring.py", good), [])
        bad = dict(good)
        bad["vm/cli.py"] = "import os\nfrom vm.adapters import Host\ndef main():\n    return os.getcwd()\n"
        bad["vm/presenter.py"] = "from cst_core.adapters.files import read_text\n"
        self.assertEqual(
            hexagonal_problems("p", "vm/wiring.py", bad),
            [
                "vm/cli.py:2: imports an adapter; only vm/wiring.py and the adapters may",
                "vm/cli.py:4: does I/O itself (move it into an adapter)",
                "vm/presenter.py:1: imports an adapter; only vm/wiring.py and the adapters may",
            ],
        )
        self.assertEqual(
            hexagonal_problems("p", "vm/wiring.py", {"vm/cli.py": "from . import adapters\n"}),
            [
                "vm/wiring.py: the composition point of p does not exist",
                "vm/cli.py:1: imports an adapter; only vm/wiring.py and the adapters may",
            ],
        )

    def test_hexagonal_packages_follow_the_rules(self) -> None:
        by_package: dict[str, dict[str, str]] = {}
        for rel in package_modules():
            by_package.setdefault(package_of(rel), {})[rel] = _read(rel)
        problems = [
            p
            for pkg, comp in sorted(HEXAGONAL_PACKAGES.items())
            for p in hexagonal_problems(pkg, comp, by_package.get(pkg, {}))
        ]
        self.assertEqual(problems, [], "\n".join(problems))
        ready = sorted(
            pkg
            for pkg, sources in by_package.items()
            if pkg not in HEXAGONAL_PACKAGES
            and f"{pkg}/cli.py" in sources
            and not hexagonal_problems(pkg, f"{pkg}/wiring.py", sources)
        )
        self.assertEqual(ready, [], "these packages follow the rules now: add them to HEXAGONAL_PACKAGES")

    def test_no_private_name_crosses_a_module(self) -> None:
        problems = [p for rel in repo_files() if is_python(rel) for p in private_imports(_read(rel), rel)]
        self.assertEqual(problems, [], "make the name public (drop the underscore):\n" + "\n".join(problems))
        self.assertEqual(
            private_imports("from swap.domain import _x, y\nfrom . import _z\nfrom os import _exit\n", "a.py"),
            [
                "a.py:1: imports the private name _x from swap.domain",
                "a.py:2: imports the private name _z from None",
            ],
        )

    def test_primitive_calls_are_resolved(self) -> None:
        src = (
            "import subprocess as sp\nfrom os import replace\nimport fcntl\nfrom tempfile import mkstemp\n"
            "sp.run(['x'], check=False)\nreplace('a', 'b')\nfcntl.ioctl(fd, FS_IOC_SETFLAGS, b)\n"
            "fcntl.ioctl(fd, SUBVOL_CREATE, b)\nmkstemp()\nrun(['y'])\n"
        )
        self.assertEqual(
            primitive_calls(src, "x.py"),
            [(5, "subprocess.run"), (6, "os.replace"), (7, "fcntl.ioctl"), (9, "tempfile.mkstemp")],
        )
        self.assertEqual(
            primitive_measure({"vm/cli.py": src, "cst_core/adapters/files.py": src}, primitive_calls, PRIMITIVE_OWNERS),
            {
                "vm subprocess.run": 1,
                "vm os.replace": 1,
                "vm fcntl.ioctl": 1,
                "vm tempfile.mkstemp": 1,
                "cst_core subprocess.run": 1,
                "cst_core fcntl.ioctl": 1,
            },
        )

    def test_primitive_literals_skip_docstrings(self) -> None:
        src = (
            '"""Runs flatpak-spawn."""\ndef f():\n    """LC_ALL=C."""\n'
            "    return ['flatpak-spawn', '--host'], {'LC_ALL': 'C'}, f'virsh -c qemu:///system {x}'\n"
        )
        self.assertEqual(primitive_literals(src, "x.py"), [(4, "LC_ALL"), (4, "flatpak-spawn"), (4, "qemu:///system")])

    def test_primitives_have_one_owner(self) -> None:
        sources = {rel: _read(rel) for rel in production_python()}
        calls = primitive_measure(sources, primitive_calls, PRIMITIVE_OWNERS)
        lits = primitive_measure(sources, primitive_literals, PRIMITIVE_LITERALS)
        problems = budget_problems(calls, PRIMITIVE_BUDGETS, "calls") + budget_problems(
            lits, LITERAL_BUDGETS, "literals"
        )
        self.assertEqual(
            problems,
            [],
            "use the cst_core owner (PRIMITIVE_OWNERS, .agents/rules/reuse-primitives.md) instead of a copy; "
            "measured tables:\nPRIMITIVE_BUDGETS:\n"
            + format_table(calls)
            + "\nLITERAL_BUDGETS:\n"
            + format_table(lits),
        )
        for owners in (*PRIMITIVE_OWNERS.values(), *PRIMITIVE_LITERALS.values()):
            for rel in owners:
                with self.subTest(owner=rel):
                    self.assertIn(rel, sources, "an owner that does not exist")

    def test_core_names_have_a_user(self) -> None:
        missing = core_names_without_user({rel: _read(rel) for rel in production_python()})
        new = [name for name in missing if name not in CORE_NAMES_WITHOUT_USER]
        self.assertEqual(
            new,
            [],
            "public cst_core names nothing outside their file uses: use them, make them private or delete them",
        )
        stale = sorted(set(CORE_NAMES_WITHOUT_USER) - set(missing))
        self.assertEqual(stale, [], "these have a user now (or are gone): remove them from CORE_NAMES_WITHOUT_USER")
        self.assertEqual(
            core_names_without_user(
                {
                    "cst_core/domain/a.py": "X = 1\n_P = 2\ndef used(): pass\nclass Lone: pass\ntype T = int\n",
                    "vm/cli.py": "from cst_core.domain.a import used\nimport cst_core.domain.a as a\na.X\n",
                }
            ),
            ["cst_core/domain/a.py:Lone", "cst_core/domain/a.py:T"],
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
