# SPDX-License-Identifier: MPL-2.0
"""The Python that CI runs on its runner's own python3, not in a container.

CI's E2E jobs run tests/e2e/run.py (`invowk cmd test e2e tier1|tier2`) and
tests/e2e/image_digest.py, and its quint jobs tests/quint/cst-quint (`invowk
cmd test quint`), on the python3 of the ubuntu-24.04 runner, Python 3.12,
while every other gate runs on /usr/bin/python3 of Fedora (3.15) and ruff's
target follows requires-python (>= 3.15). So these files, the ones they import
and the files run.py loads by path are 3.12 code: `except A, B:` (3.14) or
os.process_cpu_count (3.13) there passes every local gate and breaks every
E2E shard in CI.

host_closure() finds that set of files from the code: the entries
(HOST_ENTRIES), every repository module they import, by an absolute or a
relative import (and its packages' __init__.py), and the files run.py loads
by path (the load rules in _path_loads). A load the gate cannot follow is a
problem, so it cannot slip out of the set: a load by path of another form
(runpy.run_path, SourceFileLoader, loads_code_by_path's list), and an import by a name or text given at run time (importlib.import_module,
__import__, exec, eval, runpy.run_module, found by what the call names through the
file's imports, not by the method's name: obj.exec() is none; a call through a
name bound other than by an import, or through getattr, is not seen).
problems() checks:

- the entries against CI: every python3 command line of a job without a
  container (and of the composite actions), and of the invowk task each such
  job runs, names a file of HOST_ENTRIES: the program it runs, read as python3
  reads its own options (python_program: `python3 -I -B PROGRAM`, with or
  without .py, or `python3 -m NAME` of a repository module; a program named
  by a variable is a problem where python3 runs as a command, _is_command);
- the runner of each job that runs one of them is in HOST_PYTHON_BY_RUNNER
  (the oldest python3 found is the target);
- each file of the set parses with the target's grammar
  (ast.parse(..., feature_version=...)) and uses no name of NEWER_STDLIB
  newer than the target. The table holds the cheap, known cases, not the
  whole standard library: a new method or keyword argument passes it, and
  CI's E2E job stays the real check. A use behind a hasattr() is still
  reported (the gate does not read guards): write
  getattr(os, "process_cpu_count", None) instead;
- every file of the set is formatted and linted by ruff as the target
  (pyproject.toml, [tool.ruff] per-file-target-version, ruff_target_problems),
  so `invowk cmd fmt` cannot rewrite it into newer syntax.

tests/test_e2e_host_python.py runs these checks. Standard library only: the
unit tests run on /usr/bin/python3 without uv.
"""

import ast
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
import os
from pathlib import PurePosixPath
import posixpath
import re
import tomllib
from typing import Final

from cst_core.domain.imports import imported_modules, loads_code_by_path
from cst_core.domain.text import group, group_or_none
from tests.code_graph import import_bindings
from tests.test_workflow_audit import INVOWKFILE, task_script, uncommented_lines, workflow_jobs

# The files CI starts with its runner's python3 (the rest of the closure is found from them).
HOST_ENTRIES: Final = ("tests/e2e/image_digest.py", "tests/e2e/run.py", "tests/quint/cst-quint")

# The python3 of each runner image a host job runs on: its (major, minor).
HOST_PYTHON_BY_RUNNER: Final[Mapping[str, tuple[int, int]]] = {"ubuntu-24.04": (3, 12)}

# Standard-library names newer than 3.12, each with the version that added it ("What's New in
# Python 3.13 / 3.14"). Modules and module attributes by dotted name; a builtin as builtins.NAME.
NEWER_STDLIB: Final[Mapping[str, tuple[int, int]]] = {
    "base64.z85decode": (3, 13),
    "base64.z85encode": (3, 13),
    "builtins.PythonFinalizationError": (3, 13),
    "copy.replace": (3, 13),
    "dbm.sqlite3": (3, 13),
    "math.fma": (3, 13),
    "os.process_cpu_count": (3, 13),
    "typing.NoDefault": (3, 13),
    "typing.ReadOnly": (3, 13),
    "typing.TypeIs": (3, 13),
    "typing.get_protocol_members": (3, 13),
    "typing.is_protocol": (3, 13),
    "warnings.deprecated": (3, 13),
    "annotationlib": (3, 14),
    "compression": (3, 14),
    "concurrent.interpreters": (3, 14),
    "os.reload_environ": (3, 14),
    "string.templatelib": (3, 14),
    "uuid.uuid6": (3, 14),
    "uuid.uuid7": (3, 14),
    "uuid.uuid8": (3, 14),
}

WORKFLOWS: Final = ".github/workflows"
ACTIONS: Final = ".github/actions"
PYPROJECT: Final = "pyproject.toml"
_BUILTINS: Final = "builtins"

# python3 (or python3.N, by any path) as a command word: at a blank, or right after shell punctuation ("$(", "<(",
# ";", "&&", a quote), not inside another word (libpython3.12, python3-pip).
_PYTHON_WORD: Final = re.compile(r"(?<![\w./-])(?:[\w./-]*/)?python3(?:\.\d+)?(?![\w.-])")
# python3's options that take an argument (the rest of their cluster, or the next word); -m and -c end the options.
_OPTION_WITH_ARGUMENT: Final = frozenset("mcXW")
_MODULE_OPTION: Final = "m"
_CODE_OPTION: Final = "c"
# python3's long options that take the next word as their argument (or one after "="; the others take none).
_LONG_OPTIONS_WITH_ARGUMENT: Final = frozenset({"--check-hash-based-pycs"})
# The shell's tokens after python3, in order: a redirection (its target is the next word: >log, 2>&1, &>log, <in),
# a word (its quoted parts, $(...), ${...} and ${{ ... }} kept whole, blanks and all), or what ends the command
# (; | & ( ), or a quote no word closes: the end of the "..." python3 is in).
_REDIRECT: Final = "redirect"
_WORD: Final = "word"
_END: Final = "end"
_SHELL_TOKEN: Final = re.compile(
    rf"(?P<{_REDIRECT}>\d*(?:&>>?|[<>]+&?))"
    rf"|(?P<{_WORD}>(?:\"[^\"]*\"|'[^']*'|\$\{{\{{.*?\}}\}}|\$\([^()]*\)|\$\{{[^}}]*\}}|[^\s;|&()<>\"'])+)"
    rf"|(?P<{_END}>[;|&()\"'])"
)
_QUOTES: Final = "\"'"
_UNQUOTE: Final = str.maketrans("", "", _QUOTES)
_STDIN: Final = "-"
# The checkout's folder in a step, read as the top of the repository: ${{ github.workspace }}/ (GitHub's expression,
# put in before the shell runs) or $GITHUB_WORKSPACE/.
_WORKSPACE: Final = re.compile(r"^(?:\$\{\{\s*github\.workspace\s*\}\}|\$\{?GITHUB_WORKSPACE\}?)/")
# The commands whose words are a message: a python3 among them is text, not a program run.
_TEXT_COMMANDS: Final = frozenset({"echo", "printf"})
# The words before a command on its line that are not the command: shell keywords, exec and time.
_BEFORE_COMMAND: Final = frozenset({"!", "{", "if", "then", "elif", "else", "while", "until", "do", "exec", "time"})
_ASSIGNMENT: Final = re.compile(r"^\w+=")
# A YAML key at the start of a line (- name: ..., run: ...): only run:'s value is shell.
_YAML_KEY: Final = re.compile(r"^\s*(?:-\s+)?([\w-]+):(?:\s|$)")
_RUN_KEY: Final = "run"
# What opens a context of a shell line (_is_command): a quote, a command substitution, a subshell.
_SINGLE_QUOTE: Final = "'"
_DOUBLE_QUOTE: Final = '"'
_BACKQUOTE: Final = "`"
_SUBSTITUTION: Final = "$("
_SUBSHELL: Final = "("
_SUBSHELL_END: Final = ")"
_COMMAND_END: Final = ";|&"
_END_OF_OPTIONS: Final = "--"
# The words of an invowk task a step runs, up to the first flag, quote or variable: `invowk cmd` where a
# command starts (a line, a run: key, after ; & | or an opening parenthesis), not in a message's text.
_INVOWK_RUN: Final = re.compile(r"(?:^\s*(?:-\s+)?(?:run:\s*)?|[;&|(]\s*)(?:exec\s+)?invowk cmd((?: [a-z0-9][\w-]*)+)")
_JOB_KEY: Final = re.compile(r"^    ([a-z-]+):\s*(.*?)\s*(?:#.*)?$")
_LOCAL_ACTION: Final = re.compile(r"\buses:\s*\./(\.github/actions/[\w.-]+)\s*$")
# Path's attributes that name a folder above a path, and its methods that give the same file's path.
_FOLDER_OF_FILE: Final = "parent"
_PARENTS: Final = "parents"
_SAME_PATH: Final = frozenset({"resolve", "absolute"})
# Calls that import or run code the import closure cannot see, by the dotted name the call resolves to through
# the file's imports (_dotted: a name no import binds is builtins.NAME).
_RUN_TIME_IMPORTS: Final = frozenset(
    {
        "builtins.eval",
        "builtins.exec",
        "builtins.__import__",
        "importlib.import_module",
        "importlib.__import__",
        "runpy.run_module",
    }
)


class ProgramKind(StrEnum):
    """What a python3 command line runs (python_program): a module by its name, or a script by its path."""

    MODULE = "module"
    SCRIPT = "script"


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


def _module_files(repo: str, name: str) -> list[str]:
    """The repository files a module of that dotted name is: module.py or package/__init__.py, plus each
    parent package's __init__.py (empty: not a module of the repository, as the standard library's)."""
    parts = name.split(".")
    for candidate in ("/".join(parts) + ".py", "/".join([*parts, "__init__.py"])):
        if os.path.isfile(os.path.join(repo, candidate)):
            parents = ["/".join([*parts[:i], "__init__.py"]) for i in range(1, len(parts))]
            return [p for p in parents if os.path.isfile(os.path.join(repo, p))] + [candidate]
    return []


def _folders(tree: ast.Module, rel: str) -> dict[str, str]:
    """Module-level names bound to a folder of the repository: NAME = Path(__file__).resolve().parent
    and its longer forms (_file_folder) and NAME = OTHER / "sub" (a folder below a known one)."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name, value = node.targets[0].id, node.value
        if (folder := _file_folder(value, rel)) is not None:
            out[name] = folder
        elif (below := _below(value, out)) is not None:
            out[name] = below
    return out


def _file_folder(node: ast.expr, rel: str) -> str | None:
    """The repository folder Path(__file__) [.resolve() or .absolute()] followed by N .parent (or
    .parents[N - 1]) names: N folders up from the file rel ("" for the top of the repository). None for
    another form, or one that goes above the repository."""
    ups = 0
    while True:
        if isinstance(node, ast.Attribute) and node.attr == _FOLDER_OF_FILE:
            ups, node = ups + 1, node.value
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == _PARENTS
            and isinstance(node.slice, ast.Constant)
            and type(node.slice.value) is int
            and node.slice.value >= 0
        ):
            ups, node = ups + node.slice.value + 1, node.value.value
        else:
            break
    if (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SAME_PATH
    ):
        node = node.func.value
    is_file_path = (
        isinstance(node, ast.Call)
        and _called_name(node.func) == "Path"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "__file__"
    )
    parts = rel.split("/")
    if not is_file_path or ups == 0 or ups > len(parts):  # len(parts) folders up: the top ("")
        return None
    return "/".join(parts[: len(parts) - ups])


def _below(node: ast.expr, folders: Mapping[str, str]) -> str | None:
    """FOLDER / "name" as a repository path, when FOLDER is a known folder name and "name" a string."""
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Name)
        and node.left.id in folders
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
    ):
        return posixpath.join(folders[node.left.id], node.right.value)
    return None


def _string_tables(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level dicts whose values are all strings, by name: NAME = {...} or NAME: T = {...}."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Dict):
            values = [v.value for v in value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            if len(values) == len(value.values):
                out[target.id] = values
    return out


def _path_loads(tree: ast.Module, rel: str) -> dict[int, list[str]]:
    """The files each spec_from_file_location call of the file loads, by the call's line.

    Two forms are followed: a path FOLDER / "file.py", and a path variable set to FOLDER / PARAM in a
    function whose every caller passes TABLE[...] (a module-level dict of file names, as run.py's
    load_suite(SUITE_FILES[tier])): every file of the table. A load of another form is left out, and
    problems() reports its line.
    """
    folders = _folders(tree, rel)
    tables = _string_tables(tree)
    out: dict[int, list[str]] = {}
    for func in ast.walk(tree):
        body = func.body if isinstance(func, ast.FunctionDef | ast.Module) else []
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if not (isinstance(node, ast.Call) and _called_name(node.func) == "spec_from_file_location"):
                continue
            if len(node.args) < 2 or node.func.lineno in out:
                continue
            path = node.args[1]
            if isinstance(path, ast.Call) and _called_name(path.func) == "str" and len(path.args) == 1:
                path = path.args[0]
            if (file := _below(path, folders)) is not None:
                out[node.func.lineno] = [file]
            elif isinstance(func, ast.FunctionDef) and isinstance(path, ast.Name):
                files = _table_files(tree, func, path.id, folders, tables)
                if files:
                    out[node.func.lineno] = files
    return out


def _table_files(
    tree: ast.Module, func: ast.FunctionDef, var: str, folders: Mapping[str, str], tables: Mapping[str, list[str]]
) -> list[str]:
    """The files var names in func, when func sets var = FOLDER / PARAM and every call of func passes
    TABLE[...] as PARAM (empty: another form)."""
    params = [a.arg for a in func.args.args]
    for node in ast.walk(func):
        if not (isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == [var]):
            continue
        value = node.value
        if not (
            isinstance(value, ast.BinOp)
            and isinstance(value.op, ast.Div)
            and isinstance(value.left, ast.Name)
            and value.left.id in folders
            and isinstance(value.right, ast.Name)
            and value.right.id in params
        ):
            return []
        index = params.index(value.right.id)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _called_name(n.func) == func.name]
        names: set[str] = set()
        for call in calls:
            arg = call.args[index] if index < len(call.args) else None
            if not (isinstance(arg, ast.Subscript) and isinstance(arg.value, ast.Name) and arg.value.id in tables):
                return []
            names.add(arg.value.id)
        return [posixpath.join(folders[value.left.id], name) for table in sorted(names) for name in tables[table]]
    return []


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _relative_modules(tree: ast.Module, rel: str) -> tuple[set[str], list[str]]:
    """The dotted names the relative imports of the file rel name (from . import x: the package's x;
    from .h import y: its h and h.y), as imported_modules() gives the absolute ones, and the problems: a
    relative import that goes above the repository."""
    package = rel.split("/")[:-1]
    names: set[str] = set()
    problems: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.level):
            continue
        if node.level - 1 > len(package):
            problems.append(f"{rel}:{node.lineno}: a relative import above the top of the repository")
            continue
        base = package[: len(package) - (node.level - 1)] + (node.module.split(".") if node.module is not None else [])
        if base:
            names.add(".".join(base))
        names.update(".".join([*base, alias.name]) for alias in node.names if alias.name != "*")
    return names, problems


def _run_time_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Each call that imports or runs code by a name or a text the import closure cannot see: (line, the
    function called, as its dotted name).

    The callee is resolved through the file's imports, wherever they are (import importlib as il:
    il.import_module is importlib.import_module; from runpy import run_module as rm: rm is
    runpy.run_module; a bare exec or __import__ is the builtin), so obj.exec(...), self.run_module()
    or a repository function named run_module is none. Not reported: a call through a name bound
    other than by an import (m = importlib; m.import_module(x)) or through getattr."""
    bound = dict(binding for node in ast.walk(tree) for binding in import_bindings(node))
    return sorted(
        (node.lineno, name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _dotted(node.func, bound)) in _RUN_TIME_IMPORTS
    )


def host_closure(repo: str, entries: Iterable[str] = HOST_ENTRIES) -> tuple[list[str], list[str]]:
    """The files CI runs on its runner's python3 (sorted), and the problems met while finding them: a
    load by path the gate cannot follow, a file that is not there."""
    seen: set[str] = set()
    problems: list[str] = []
    todo = list(entries)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        try:
            text = _read(repo, rel)
            tree = ast.parse(text, filename=rel)
        except (OSError, SyntaxError) as err:
            problems.append(f"{rel}: cannot read it as Python ({err})")
            continue
        names, relative_problems = _relative_modules(tree, rel)
        problems += relative_problems
        for name in sorted(imported_modules(text, rel) | names):
            todo += _module_files(repo, name)
        problems += [
            f"{rel}:{line}: imports code by a name or text given at run time ({how}), which "
            "tests/e2e_host_python.py cannot follow: import the module by its name"
            for line, how in _run_time_imports(tree)
        ]
        loads = _path_loads(tree, rel)
        problems += [
            f"{rel}:{line}: loads code by path in a form tests/e2e_host_python.py cannot follow: "
            "teach the gate this load (_path_loads), so the file it loads is checked too"
            for line in loads_code_by_path(text, rel)
            if line not in loads
        ]
        for files in loads.values():
            todo += files
    return sorted(seen), problems


def _job_key(body: Sequence[tuple[int, str]], key: str) -> str | None:
    """The value of a job's own key (indented four spaces), None when the job has none."""
    for _, line in body:
        found = _JOB_KEY.match(line)
        if found is not None and group(found, 1) == key:
            return group(found, 2)
    return None


def _entries(repo: str, folder: str) -> list[str]:
    """The names in a folder of the repository, sorted (none when it is not there)."""
    path = os.path.join(repo, folder)
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def _workflow_files(repo: str) -> list[str]:
    return [f"{WORKFLOWS}/{e}" for e in _entries(repo, WORKFLOWS) if e.endswith((".yml", ".yaml"))]


def _action_files(repo: str) -> list[str]:
    """Each composite action's action.yml or action.yaml."""
    return [
        f"{ACTIONS}/{e}/{n}"
        for e in _entries(repo, ACTIONS)
        for n in ("action.yml", "action.yaml")
        if os.path.isfile(os.path.join(repo, ACTIONS, e, n))
    ]


def python_program(text: str) -> tuple[ProgramKind, str] | None:
    """What a python3 command line runs, read from the text after python3 as the shell splits it
    (_command_words) and as Python reads its own options: (MODULE, NAME) for -m NAME, (SCRIPT, WORD)
    for the first word that is no option, or None (-c's inline code, "-" for stdin, no program). An
    option cluster (-IB) goes on; -m, -c, -X and -W take the rest of their cluster or the next word;
    "--" ends the options; another word that starts with "--" is a long option, which takes the next word
    when it is one of _LONG_OPTIONS_WITH_ARGUMENT (--check-hash-based-pycs MODE). Words after the
    program are the program's own (a -m there is not python3's)."""
    words = _command_words(text)
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        following = words[i] if i < len(words) else None
        if word == _END_OF_OPTIONS:
            return None if following is None else (ProgramKind.SCRIPT, following)
        if word in {"", _STDIN}:
            return None
        if not word.startswith("-"):
            return ProgramKind.SCRIPT, word
        if word.startswith("--"):
            if word in _LONG_OPTIONS_WITH_ARGUMENT:
                i += 1
            continue
        option = _option_with_argument(word)
        if option is None:
            continue
        letter, argument = option
        if not argument and following is not None:
            argument, i = following, i + 1
        if letter == _MODULE_OPTION:
            return (ProgramKind.MODULE, argument) if argument else None
        if letter == _CODE_OPTION:
            return None
    return None


def _command_words(text: str) -> list[str]:
    """The words of the shell command text starts, without their quotes, up to what ends it (; | & ( ),
    or the quote it is inside of: _SHELL_TOKEN). A redirection and its target are no words (>log,
    2>/dev/null, &>log), wherever they are."""
    words: list[str] = []
    target = False
    for token in _SHELL_TOKEN.finditer(text):
        if group_or_none(token, _END) is not None:
            break
        if group_or_none(token, _REDIRECT) is not None:
            target = True
        elif target:
            target = False
        else:
            words.append(group(token, _WORD).translate(_UNQUOTE))
    return words


def _option_with_argument(cluster: str) -> tuple[str, str] | None:
    """The first option of a cluster (-IBm, -Xdev) that takes an argument, and the rest of the cluster
    after it (its argument when not empty); None when no option of the cluster takes one."""
    for at, letter in enumerate(cluster[1:], 2):
        if letter in _OPTION_WITH_ARGUMENT:
            return letter, cluster[at:]
    return None


def _python_runs(repo: str, line: str) -> tuple[list[str], list[str]]:
    """The repository files a line runs with python3, and the program words it cannot name.

    A script inside the repository counts when it ends in .py or is a file there without .py
    (tests/quint/cst-quint); python3 -m NAME's module counts when it is one (NAME.py, or its __main__.py for a package; pip or
    venv are none). A script that starts with the checkout's folder (${{ github.workspace }}/,
    $GITHUB_WORKSPACE/) is that path from the top of the repository. Another script word with a "$"
    or a backquote is a path given at run time: it is returned in the second list, so the caller
    reports it, when python3 runs there as a command (_is_command: not in quoted text, an echo or a
    YAML name:; a python3 in bash -c "..." is quoted text too, so its $ word is not reported, while
    a file it names still is). Any other word (a system program, an absolute path, python3 in a
    message or a package list) is left out."""
    files: list[str] = []
    unnamed: list[str] = []
    for found in _PYTHON_WORD.finditer(line):
        rest = line[found.end() :]
        rest = rest[1:] if rest[:1] in _QUOTES else rest  # "python3" x.py: the quote closes the command word
        program = python_program(rest) if rest[:1].isspace() else None
        match program:
            case (ProgramKind.MODULE, name):
                parts = name.split(".")
                for candidate in ("/".join(parts) + ".py", "/".join([*parts, "__main__.py"])):
                    if os.path.isfile(os.path.join(repo, candidate)):
                        files.append(candidate)
                        break
            case (ProgramKind.SCRIPT, word):
                path = _WORKSPACE.sub("", word, count=1)
                rel = posixpath.normpath(path)
                inside = not rel.startswith(("/", "../")) and rel != ".."
                if "$" in path or "`" in path:
                    if _is_command(line[: found.start()]):
                        unnamed.append(word)
                elif inside and (rel.endswith(".py") or os.path.isfile(os.path.join(repo, rel))):
                    files.append(rel)
            case _:
                pass
    return files, unnamed


def _is_command(before: str) -> bool:
    """Whether a python3 word after this text of its line is a command the shell runs: not text inside
    quotes (a $(...) or a backquote there is a command again), not a word of an echo or printf, and
    not in the value of a YAML key other than run:."""
    key = _YAML_KEY.match(before)
    if key is not None and group(key, 1) != _RUN_KEY:
        return False
    opener, start = _innermost_context(before, 0 if key is None else key.end())
    if opener in {_SINGLE_QUOTE, _DOUBLE_QUOTE}:
        return False
    words = [w for w in before[start:].split() if w not in _BEFORE_COMMAND and _ASSIGNMENT.match(w) is None]
    return not (words and words[0] in _TEXT_COMMANDS)


def _innermost_context(text: str, start: int) -> tuple[str, int]:
    """The context of the shell line text that is still open at its end, read from start: what opened it
    ("" the line, a quote, "$(", "(" or a backquote) and where its current command starts."""
    contexts: list[tuple[str, int]] = [("", start)]  # innermost last
    i = start
    while i < len(text):
        c, opener = text[i], contexts[-1][0]
        if opener == _SINGLE_QUOTE:
            if c == _SINGLE_QUOTE:
                contexts.pop()
        elif c == "\\":
            i += 1  # the next character is escaped
        elif text.startswith(_SUBSTITUTION, i):
            contexts.append((_SUBSTITUTION, i + len(_SUBSTITUTION)))
            i += 1
        elif c in {_BACKQUOTE, _DOUBLE_QUOTE}:
            if opener == c:
                contexts.pop()
            else:
                contexts.append((c, i + 1))
        elif opener == _DOUBLE_QUOTE:
            pass
        elif c in {_SINGLE_QUOTE, _SUBSHELL}:
            contexts.append((c, i + 1))
        elif c == _SUBSHELL_END and opener in {_SUBSTITUTION, _SUBSHELL}:
            contexts.pop()
        elif c in _COMMAND_END:
            contexts[-1] = (opener, i + 1)
        i += 1
    return contexts[-1]


def _runs(repo: str, lines: Iterable[tuple[int, str]], cue: str, where: str) -> tuple[list[str], list[str]]:
    """The repository files these lines run with python3, directly or through an invowk task, and the
    problems (an invowk task that is not in the invowkfile, a program the gate cannot name)."""
    files: list[str] = []
    problems: list[str] = []

    def scan(text: str, at: str) -> None:
        found_files, unnamed = _python_runs(repo, text)
        files.extend(found_files)
        problems.extend(_unnamed_problem(at, word) for word in unnamed)

    for n, line in lines:
        scan(line, f"{where}:{n}")
        for found in _INVOWK_RUN.finditer(line):
            task = group(found, 1).strip()
            script = task_script(cue, task)
            if script is None:
                problems.append(f"{where}:{n}: runs `invowk cmd {task}`, which is no task of {INVOWKFILE}")
                continue
            for script_line in script.splitlines():
                if not script_line.lstrip().startswith("#"):
                    scan(script_line, f"{where}:{n} (task {task})")
    return files, problems


def _unnamed_problem(where: str, word: str) -> str:
    return (
        f"{where}: runs python3 with a program the gate cannot name ({word}): "
        "name the file in the command line, so tests/e2e_host_python.py can check it"
    )


def host_runs(repo: str) -> tuple[dict[str, list[str]], list[str]]:
    """What CI runs on a runner's own python3: the runner of each job that runs Python on its host, with
    the files it runs, and the problems (a job's python3 file that is not an entry, a runner with no
    recorded python3).

    A job runs on its runner's host when it has no container: key. Composite actions (.github/actions)
    run where the job that uses them runs, and every user of one today is a host job: their python3
    lines count as host lines.
    """
    cue = _read(repo, INVOWKFILE)
    problems: list[str] = []
    action_files: dict[str, list[str]] = {}
    for rel in _action_files(repo):
        files, found = _runs(repo, uncommented_lines(_read(repo, rel)), cue, rel)
        action_files[os.path.dirname(rel)] = files
        problems += found
        problems += _not_entries(rel, files)
    by_runner: dict[str, list[str]] = {}
    for rel in _workflow_files(repo):
        for job, body in workflow_jobs(uncommented_lines(_read(repo, rel))).items():
            if _job_key(body, "container") is not None:
                continue
            files, found = _runs(repo, body, cue, f"{rel} (job {job})")
            problems += found
            problems += _not_entries(f"{rel} (job {job})", files)
            for _, line in body:  # an action's own files are reported once, at the action
                if (uses := _LOCAL_ACTION.search(line)) is not None:
                    files += action_files.get(group(uses, 1), [])
            if not files:
                continue
            runner = (_job_key(body, "runs-on") or "").strip("\"'")
            if runner not in HOST_PYTHON_BY_RUNNER:
                problems.append(
                    f"{rel} (job {job}): runs Python on the runner {runner or '(none named)'}, whose python3 "
                    "tests/e2e_host_python.py does not know: add it to HOST_PYTHON_BY_RUNNER"
                )
            by_runner.setdefault(runner, []).extend(files)
    return by_runner, problems


def _not_entries(where: str, files: Iterable[str]) -> list[str]:
    return [
        f"{where}: runs {f} on the runner's python3, which is not in HOST_ENTRIES of tests/e2e_host_python.py: "
        "add it there, so the gate checks it and what it imports on that Python"
        for f in sorted(set(files))
        if f not in HOST_ENTRIES
    ]


def target_version(by_runner: Mapping[str, Sequence[str]]) -> tuple[int, int]:
    """The oldest python3 of the runners that run Python on their host (3.12 when none is known)."""
    known = [HOST_PYTHON_BY_RUNNER[r] for r in by_runner if r in HOST_PYTHON_BY_RUNNER]
    return min(known, default=min(HOST_PYTHON_BY_RUNNER.values()))


def newer_names(tree: ast.AST, target: tuple[int, int]) -> list[tuple[int, str, tuple[int, int]]]:
    """Each use of a NEWER_STDLIB name newer than target: (line, dotted name, version).

    Imports (import os as o, from os import process_cpu_count, import annotationlib) and the attribute
    chains on an imported name (o.process_cpu_count) count, an import under `if TYPE_CHECKING:` too;
    text in strings and comments does not.
    """
    newer = {name: v for name, v in NEWER_STDLIB.items() if v > target}
    bound: dict[str, str] = {}
    out: list[tuple[int, str, tuple[int, int]]] = []

    def hit(line: int, dotted: str) -> None:
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in newer:
                out.append((line, prefix, newer[prefix]))
                return

    nodes = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom | ast.Attribute | ast.Name)),
        key=lambda n: (n.lineno, n.col_offset),
    )
    attribute_parts = {id(n.value) for n in nodes if isinstance(n, ast.Attribute)}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit(node.lineno, alias.name)
            bound.update(import_bindings(node))  # import os.path binds os
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            hit(node.lineno, node.module)
            for alias in node.names:
                hit(node.lineno, f"{node.module}.{alias.name}")
            bound.update(import_bindings(node))
        elif id(node) not in attribute_parts:
            dotted = _dotted(node, bound)
            if dotted is not None:
                hit(node.lineno, dotted)
    return out


def _dotted(node: ast.expr, bound: Mapping[str, str]) -> str | None:
    """The dotted name an attribute chain or a name stands for (o.process_cpu_count after import os as o:
    os.process_cpu_count; a name no import binds: builtins.NAME), None for anything else."""
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    base = bound.get(node.id, f"{_BUILTINS}.{node.id}")
    return ".".join([base, *reversed(attrs)])


def _version(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


def file_problems(rel: str, text: str, target: tuple[int, int], runner: str) -> list[str]:
    """What in one file of the closure the target python3 cannot run: its grammar, the newer names."""
    why = f"CI runs this file on the python3 of {runner}, {_version(target)}"
    try:
        tree = ast.parse(text, filename=rel, feature_version=target)
    except SyntaxError as err:
        return [f"{rel}:{err.lineno}: {err.msg} ({why})"]
    return [
        f"{rel}:{line}: {name} is new in Python {_version(added)} ({why}; tests/e2e_host_python.py, NEWER_STDLIB)"
        for line, name, added in newer_names(tree, target)
    ]


def ruff_target_problems(pyproject: str, closure: Iterable[str], target: tuple[int, int]) -> list[str]:
    """Each file of the closure ruff does not format and lint as the target: no [tool.ruff]
    per-file-target-version pattern of pyproject.toml's text matches it, or the first that does names
    another version. Patterns are matched as ruff's globs (** for any folders)."""
    want = f"py{target[0]}{target[1]}"
    raw = tomllib.loads(pyproject).get("tool", {}).get("ruff", {}).get("per-file-target-version", {})
    table = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    out: list[str] = []
    for rel in closure:
        got = next((v for pattern, v in table.items() if PurePosixPath(rel).full_match(pattern)), None)
        if got != want:
            out.append(
                f"{PYPROJECT}: {rel} is run by CI on Python {_version(target)}, but ruff's "
                f"per-file-target-version gives it {got or 'no entry'}: add a {want!r} entry that covers it, "
                "so ruff never formats it into newer syntax"
            )
    return out


def problems(repo: str) -> list[str]:
    """Every problem of the host-run Python, as text lines (empty: all fine)."""
    by_runner, out = host_runs(repo)
    target = target_version(by_runner)
    runner = next((r for r in sorted(by_runner) if HOST_PYTHON_BY_RUNNER.get(r) == target), "the CI runner")
    closure, found = host_closure(repo)
    out += found
    for rel in closure:
        if os.path.isfile(os.path.join(repo, rel)):
            out += file_problems(rel, _read(repo, rel), target, runner)
    return out + ruff_target_problems(_read(repo, PYPROJECT), closure, target)
