# SPDX-License-Identifier: MPL-2.0
"""Magic values: closed vocabularies, unit numbers and loose records may not go above their ceilings.

A word or a number that means something (a state, a kind, a subcommand, a
number of seconds, an exit status) is named once and used by name. ty then
catches a typing mistake (`state == "okk"` passes ty; `OwnedState.OKK` does
not) and a `match` that forgets a case. See .agents/rules/magic-values.md.

Five rules, on the tracked production Python files (tests are left out: they
keep the raw words on purpose, they are what pins the values):

1. leak: a comparison (`==`, `!=`, `in` a tuple, list or set) or a match
   `case` with a string literal equal to a member value of a StrEnum declared
   in the same package or in cst_core. Compare with the member instead.
2. chain: an if/elif chain with 3 or more arms that compare one subject with
   string literals (counted once, at its first `if`), or a match with 3 or
   more string-literal cases. Declare a StrEnum and match on it.
3. unit-number: a literal of UNIT_NUMBERS (3600, 86400, 604800 and the exit
   statuses 124, 126, 127, 137; also written as a product, `24 * 60 * 60`)
   outside cst_core/domain/values.py and cst_core/domain/errors.py. Use
   HOUR, DAY, WEEK (cst_core.domain.values) or ExitStatus
   (cst_core.domain.errors).
4. loose-record: a function parameter or return annotated
   dict/Mapping/MutableMapping[str, Any | object | Json] (also through a type
   alias of the same file). Declare a TypedDict for the format.
5. literal-vocabulary: a `Literal[...]` with 3 or more strings. A vocabulary
   the code compares or matches is a StrEnum; a Literal stays only for a
   fixed external format (an exemption with its reason).

Each rule is counted per package (tests/budgets.py: `package_of`) in
MAGIC_CEILINGS, keys "<package> <rule>": a count above its entry fails, a
count below it passes, and `invowk cmd fix budgets` lowers the entry. The
target is 0. A hit that has to stay (a literal another program reads from the
source text, a parser at the border) goes to MAGIC_EXEMPT, keyed
"path:qualname" (the function, class or module-level name it is in; a key
covers the scopes nested in it), with its reason. An exemption that no
longer covers a hit fails, like LICENSE_EXEMPT. There are no ignore
comments.

Standard library only.
"""

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import os
import tempfile
from typing import Final
import unittest

from tests.budgets import budget_problems, format_table, package_of
from tests.code_graph import production_python
from tests.repo_files import REPO

CORE_PACKAGE: Final = "cst_core"
# The numbers rule 3 names, and the name to use instead.
UNIT_NUMBERS: Final[dict[int, str]] = {
    3600: "HOUR (cst_core.domain.values)",
    86400: "DAY (cst_core.domain.values)",
    604800: "WEEK (cst_core.domain.values)",
    124: "ExitStatus.TIMED_OUT (cst_core.domain.errors)",
    126: "ExitStatus.CANNOT_RUN (cst_core.domain.errors)",
    127: "ExitStatus.NOT_FOUND (cst_core.domain.errors)",
    137: "ExitStatus.KILLED (cst_core.domain.errors)",
}
# Where those numbers are defined.
UNIT_HOMES: Final = frozenset({"cst_core/domain/values.py", "cst_core/domain/errors.py"})
CHAIN_ARMS: Final = 3
LITERAL_WORDS: Final = 3
_MAPPINGS: Final = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_MAPPING_ARGS: Final = 2  # Mapping[key, value]
_LOOSE_VALUES: Final = frozenset({"Any", "object", "Json"})

# The highest count per "<package> <rule>" (tests/budgets.py; a missing key is 0).
MAGIC_CEILINGS: Final[dict[str, int]] = {
    "bluetooth leak": 3,
    "bluetooth/setup chain": 5,
    "bluetooth/setup leak": 44,
    "bluetooth/setup literal-vocabulary": 1,
    "btrfs/bulk chain": 1,
    "btrfs/bulk leak": 34,
    "btrfs/bulk loose-record": 3,
    "btrfs/bulk unit-number": 3,
    "btrfs/chrome_cache leak": 3,
    "btrfs/docker_gc leak": 2,
    "btrfs/docker_gc literal-vocabulary": 1,
    "btrfs/docker_gc loose-record": 5,
    "btrfs/docker_volumes_nocow leak": 1,
    "btrfs/docker_volumes_nocow literal-vocabulary": 1,
    "btrfs/docker_volumes_nocow loose-record": 7,
    "btrfs/health_check leak": 2,
    "btrfs/health_check literal-vocabulary": 1,
    "btrfs/health_check unit-number": 3,
    "btrfs/pending_alerts leak": 4,
    "btrfs/pending_alerts unit-number": 1,
    "btrfs/snapshot leak": 5,
    "btrfs/snapshot literal-vocabulary": 3,
    "btrfs/snapshot loose-record": 4,
    "btrfs/snapshot unit-number": 9,
    "btrfs/stall_monitord unit-number": 1,
    "btrfs/stall_report chain": 1,
    "btrfs/stall_report loose-record": 1,
    "btrfs/stall_report unit-number": 2,
    "cpu chain": 1,
    "cpu leak": 2,
    "cpu unit-number": 1,
    "cpu/setup leak": 9,
    "cpu/setup literal-vocabulary": 1,
    "cpu/setup loose-record": 3,
    "cst_core chain": 1,
    "cst_core leak": 1,
    "cst_core literal-vocabulary": 2,
    "cst_core unit-number": 3,
    "doctor chain": 1,
    "doctor leak": 9,
    "doctor loose-record": 1,
    "doctor unit-number": 5,
    "swap leak": 21,
    "swap literal-vocabulary": 3,
    "toolchains chain": 3,
    "toolchains leak": 42,
    "toolchains loose-record": 10,
    "toolchains unit-number": 9,
    "vm chain": 3,
    "vm leak": 12,
    "vm literal-vocabulary": 3,
    "vm loose-record": 3,
    "vm unit-number": 14,
}
# "path:qualname" -> why the hits in that scope stay.
MAGIC_EXEMPT: Final[dict[str, str]] = {
    "cst_core/application/dispatch.py:CstDispatcher.run_doctor": (
        'compares SUDO_USER with the account name "root", not a FileOwner or Owner'
    ),
    "cst_core/application/dispatch.py:ModuleRouter._as_user": (
        'compares SUDO_USER with the account name "root", not a FileOwner or Owner'
    ),
    "cst_core/domain/record.py:_string": (
        "the record's validator: it reads one key of a JSON object nobody has checked yet, and checks it"
    ),
}


@dataclass(frozen=True, slots=True, order=True)
class Hit:
    """One magic value: where it is, which rule, and what to write instead."""

    rel: str
    line: int
    rule: str
    scope: str
    text: str

    @property
    def key(self) -> str:
        """The MAGIC_CEILINGS key it counts under."""
        return f"{package_of(self.rel)} {self.rule}"

    @property
    def where(self) -> str:
        """The MAGIC_EXEMPT key of its scope."""
        return f"{self.rel}:{self.scope}"

    def __str__(self) -> str:
        return f"{self.rel}:{self.line}: {self.text}"


def _name_of(node: ast.expr) -> str | None:
    """The last name of `x` or `a.b.x`, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _module_name(stmt: ast.stmt) -> str | None:
    """The name a module-level statement defines (an assignment, a type alias), else None."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target: ast.expr = stmt.targets[0]
    elif isinstance(stmt, ast.AnnAssign | ast.TypeAlias):
        target = stmt.target if isinstance(stmt, ast.AnnAssign) else stmt.name
    else:
        return None
    return target.id if isinstance(target, ast.Name) else None


def scoped(node: ast.AST, scope: str = "") -> Iterator[tuple[ast.AST, str]]:
    """Every node under `node` with the qualname of its scope ("" at module level)."""
    for child in ast.iter_child_nodes(node):
        inner = scope
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            inner = f"{scope}.{child.name}" if scope else child.name
        elif not scope and isinstance(child, ast.stmt):
            inner = _module_name(child) or ""
        yield child, inner
        yield from scoped(child, inner)


def _str_constant(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _compared_strings(node: ast.Compare) -> list[tuple[ast.expr, str]]:
    """(subject, word) for each string literal the comparison checks a subject against."""
    out: list[tuple[ast.expr, str]] = []
    left = node.left
    for op, right in zip(node.ops, node.comparators, strict=True):
        if isinstance(op, ast.Eq | ast.NotEq):
            for subject, other in ((left, right), (right, left)):
                word = _str_constant(other)
                if word is not None and _str_constant(subject) is None:
                    out.append((subject, word))
        elif isinstance(op, ast.In | ast.NotIn) and isinstance(right, ast.Tuple | ast.List | ast.Set):
            out += [(left, w) for w in map(_str_constant, right.elts) if w is not None]
        left = right
    return out


def enum_values(tree: ast.Module) -> dict[str, str]:
    """value -> "Class.MEMBER" for every StrEnum (or str, Enum) class of a module."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {_name_of(b) for b in node.bases}
        if "StrEnum" not in bases and not {"str", "Enum"} <= bases:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                word = _str_constant(stmt.value)
                if word is not None and not stmt.targets[0].id.startswith("_"):
                    out.setdefault(word, f"{node.name}.{stmt.targets[0].id}")
    return out


def _leaks(node: ast.AST, vocabulary: Mapping[str, str]) -> list[tuple[int, str]]:
    words: list[tuple[int, str]] = []
    if isinstance(node, ast.Compare):
        words = [(node.lineno, w) for _, w in _compared_strings(node)]
    elif isinstance(node, ast.MatchValue) and (w := _str_constant(node.value)) is not None:
        words = [(node.value.lineno, w)]
    return [(line, f"{w!r} is {vocabulary[w]}: compare with the enum member") for line, w in words if w in vocabulary]


def _arm_subjects(test: ast.expr) -> set[str]:
    """The subjects (as source text) an if test compares with string literals (inside any and/or)."""
    if isinstance(test, ast.BoolOp):
        return set().union(*(_arm_subjects(v) for v in test.values))
    if isinstance(test, ast.Compare):
        return {ast.unparse(subject) for subject, _ in _compared_strings(test)}
    return set()


def _chain(node: ast.If) -> list[ast.If]:
    out = [node]
    while len(out[-1].orelse) == 1 and isinstance(out[-1].orelse[0], ast.If):
        out.append(out[-1].orelse[0])
    return out


def _string_case(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchOr):
        return any(_string_case(p) for p in pattern.patterns)
    return isinstance(pattern, ast.MatchValue) and _str_constant(pattern.value) is not None


def _chains(tree: ast.Module) -> Iterator[tuple[ast.stmt, str]]:
    """(first statement, message) for each chain of rule 2."""
    elifs = {id(n.orelse[0]) for n in ast.walk(tree) if isinstance(n, ast.If) and _chain(n)[1:]}
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and id(node) not in elifs:
            counts: dict[str, int] = {}
            for arm in _chain(node):
                for subject in _arm_subjects(arm.test):
                    counts[subject] = counts.get(subject, 0) + 1
            best = max(counts.items(), key=lambda kv: kv[1], default=("", 0))
            if best[1] >= CHAIN_ARMS:
                yield node, f"{best[1]} string arms on {best[0]!r}: declare a StrEnum and match on it"
        elif isinstance(node, ast.Match):
            n = sum(1 for case in node.cases if _string_case(case.pattern))
            if n >= CHAIN_ARMS:
                yield node, f"match with {n} string cases on {ast.unparse(node.subject)!r}: declare a StrEnum"


def _number(node: ast.expr) -> tuple[float, list[float]] | None:
    """(value, leaves) of a number or a product of numbers, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return node.value, [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _number(node.left), _number(node.right)
        if left is not None and right is not None:
            return left[0] * right[0], left[1] + right[1]
    return None


def _unit_numbers(tree: ast.Module) -> Iterator[tuple[ast.expr, str]]:
    """(node, message) for each number of rule 3 (a product counts once)."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in inside or not isinstance(node, ast.expr):
            continue
        found = _number(node)
        if found is None:
            continue
        inside |= {id(n) for n in ast.walk(node)}
        value, leaves = found
        unit = next((n for n in (value, *leaves) if n in UNIT_NUMBERS), None)
        if unit is not None:
            yield node, f"{ast.unparse(node)} is {UNIT_NUMBERS[int(unit)]}"


def _is_loose(annotation: ast.expr, aliases: frozenset[str]) -> bool:
    """True when the annotation holds dict/Mapping[str, Any | object | Json] (or an alias of one)."""
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id in aliases:
            return True
        text = _str_constant(node) if isinstance(node, ast.expr) else None
        if text is not None:
            try:
                inner = ast.parse(text, mode="eval").body
            except SyntaxError:
                continue
            if _is_loose(inner, aliases):
                return True
        if isinstance(node, ast.Subscript) and _name_of(node.value) in _MAPPINGS:
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else []
            if (
                [_name_of(a) for a in args[:1]] == ["str"]
                and len(args) == _MAPPING_ARGS
                and _name_of(args[-1]) in _LOOSE_VALUES
            ):
                return True
    return False


def _loose_aliases(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to a loose mapping type (`type Rec = dict[str, Json]`)."""
    out: set[str] = set()
    for stmt in tree.body:
        name = _module_name(stmt)
        value = getattr(stmt, "value", None)
        if name and isinstance(value, ast.expr) and _is_loose(value, frozenset()):
            out.add(name)
    return frozenset(out)


def _loose_records(tree: ast.Module) -> Iterator[tuple[ast.expr, str]]:
    aliases = _loose_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        a = node.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *(p for p in (a.vararg, a.kwarg) if p is not None)]
        sites = [(p.annotation, f"parameter {p.arg!r}") for p in params if p.annotation is not None]
        if node.returns is not None:
            sites.append((node.returns, "return"))
        for ann, what in sites:
            if _is_loose(ann, aliases):
                yield ann, f"{node.name}: {what} is a loose dict ({ast.unparse(ann)}): declare a TypedDict"


def _literal_vocabularies(tree: ast.Module) -> Iterator[tuple[ast.expr, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _name_of(node.value) == "Literal":
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            words = [w for w in map(_str_constant, elts) if w is not None]
            if len(words) >= LITERAL_WORDS:
                yield node, f"Literal of {len(words)} strings ({', '.join(map(repr, words))}): declare a StrEnum"


def file_hits(tree: ast.Module, rel: str, vocabulary: Mapping[str, str]) -> list[Hit]:
    """The hits of every rule in one production module; vocabulary: its package's and cst_core's enum values."""
    scopes = {id(node): scope or "<module>" for node, scope in scoped(tree)}
    found: list[tuple[str, ast.AST, int, str]] = []
    for node in ast.walk(tree):
        found += [("leak", node, line, text) for line, text in _leaks(node, vocabulary)]
    found += [("chain", n, n.lineno, t) for n, t in _chains(tree)]
    if rel not in UNIT_HOMES:
        found += [("unit-number", n, n.lineno, t) for n, t in _unit_numbers(tree)]
    found += [("loose-record", n, n.lineno, t) for n, t in _loose_records(tree)]
    found += [("literal-vocabulary", n, n.lineno, t) for n, t in _literal_vocabularies(tree)]
    return sorted(Hit(rel, line, rule, scopes.get(id(n), "<module>"), text) for rule, n, line, text in found)


def scan(root: str, rels: Sequence[str]) -> list[Hit]:
    """Every hit in the files rels (relative to root), the exempt ones included."""
    trees: dict[str, ast.Module] = {}
    for rel in rels:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            trees[rel] = ast.parse(fh.read(), filename=rel)
    vocab: dict[str, dict[str, str]] = {}
    for rel, tree in trees.items():
        for word, member in enum_values(tree).items():
            vocab.setdefault(package_of(rel), {}).setdefault(word, member)
    core = vocab.get(CORE_PACKAGE, {})
    hits: list[Hit] = []
    for rel, tree in trees.items():
        hits += file_hits(tree, rel, {**core, **vocab.get(package_of(rel), {})})
    return hits


def is_exempt(hit: Hit, exempt: Mapping[str, str]) -> bool:
    """True when an MAGIC_EXEMPT key names the hit's scope or a scope around it."""
    return any(hit.where == key or hit.where.startswith(key + ".") for key in exempt)


def counts(hits: Sequence[Hit], exempt: Mapping[str, str] = MAGIC_EXEMPT) -> dict[str, int]:
    """Hits per MAGIC_CEILINGS key, the exempt ones left out."""
    out: dict[str, int] = {}
    for hit in hits:
        if not is_exempt(hit, exempt):
            out[hit.key] = out.get(hit.key, 0) + 1
    return out


def stale_exemptions(hits: Sequence[Hit], exempt: Mapping[str, str]) -> list[str]:
    """The MAGIC_EXEMPT keys that cover no hit."""
    return sorted(key for key in exempt if not any(is_exempt(h, {key: ""}) for h in hits))


def repo_hits() -> list[Hit]:
    """Every hit in the repository's production Python."""
    return scan(REPO, production_python())


def measure() -> dict[str, int]:
    """The counts MAGIC_CEILINGS caps (for tests/fix_budgets.py)."""
    return counts(repo_hits())


class MagicValuesGateTest(unittest.TestCase):
    def test_counts_stay_within_their_ceilings(self) -> None:
        hits = repo_hits()
        measured = counts(hits)
        problems = budget_problems(measured, MAGIC_CEILINGS, "magic values")
        over = {line.split(":")[0] for line in problems}
        detail = [str(h) for h in hits if h.key in over and not is_exempt(h, MAGIC_EXEMPT)]
        self.assertEqual(
            problems,
            [],
            "name the value (.agents/rules/magic-values.md):\n"
            + "\n".join(detail)
            + "\nmeasured table for MAGIC_CEILINGS:\n"
            + format_table(measured),
        )

    def test_every_exemption_still_covers_a_hit(self) -> None:
        self.assertEqual(stale_exemptions(repo_hits(), MAGIC_EXEMPT), [], "delete these from MAGIC_EXEMPT")
        for key, reason in MAGIC_EXEMPT.items():
            self.assertIn(":", key)
            self.assertTrue(reason.strip(), key)


class PlantedTest(unittest.TestCase):
    """Each rule on files planted in a temporary checkout."""

    CORE: Final = "from enum import StrEnum\n\n\nclass OwnedState(StrEnum):\n    OK = 'ok'\n    MISSING = 'missing'\n"

    def plant(self, files: Mapping[str, str]) -> list[Hit]:
        with tempfile.TemporaryDirectory() as root:
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                    fh.write(text)
            return scan(root, sorted(files))

    def rules(self, hits: Sequence[Hit]) -> list[tuple[str, int, str]]:
        return [(h.rel, h.line, h.rule) for h in hits]

    def test_leak_of_a_cst_core_or_package_member(self) -> None:
        hits = self.plant(
            {
                "cst_core/domain/files.py": self.CORE,
                "pkg/domain.py": "from enum import StrEnum\n\n\nclass Mode(StrEnum):\n    FAST = 'fast'\n",
                "pkg/cli.py": (
                    "def f(s: str) -> int:\n"
                    "    if s == 'ok' or 'missing' != s or s in ('fast', 'other'):\n"
                    "        return 1\n"
                    "    match s:\n"
                    "        case 'ok':\n"
                    "            return 2\n"
                    "    return 'slow' == s\n"
                ),
                "other/cli.py": "def g(s: str) -> bool:\n    return s == 'fast'\n",
            }
        )
        self.assertEqual(
            self.rules(hits),
            [
                ("pkg/cli.py", 2, "leak"),
                ("pkg/cli.py", 2, "leak"),
                ("pkg/cli.py", 2, "leak"),
                ("pkg/cli.py", 5, "leak"),
            ],
            "'fast' is pkg's own word, not other's; 'slow' and 'other' are nobody's",
        )
        self.assertIn("pkg/cli.py:2: 'ok' is OwnedState.OK: compare with the enum member", [str(h) for h in hits])
        self.assertEqual(counts(hits), {"pkg leak": 4})

    def test_chain_once_per_chain_and_match(self) -> None:
        src = (
            "def f(cmd: str, x: int) -> int:\n"
            "    if cmd == 'a':\n        return 1\n"
            "    elif cmd in ('b', 'c'):\n        return 2\n"
            "    elif x == 3:\n        return 3\n"
            "    elif 'd' == cmd:\n        return 4\n"
            "    if cmd == 'a':\n        return 1\n"
            "    elif cmd == 'b':\n        return 2\n"
            "    match cmd:\n"
            "        case 'a' | 'b':\n            return 1\n"
            "        case 'c':\n            return 2\n"
            "        case 'd':\n            return 3\n"
            "    return 0\n"
        )
        hits = self.plant({"pkg/cli.py": src})
        self.assertEqual(self.rules(hits), [("pkg/cli.py", 2, "chain"), ("pkg/cli.py", 14, "chain")])
        self.assertIn("3 string arms on 'cmd'", str(hits[0]))
        self.assertEqual(hits[0].scope, "f")

    def test_chain_arms_inside_and_or_count(self) -> None:
        src = (
            "def f(cmd: str, x: int, y: bool) -> int:\n"
            "    if cmd == 'a' and x:\n        return 1\n"
            "    elif (cmd == 'b' and y) or x == 2:\n        return 2\n"
            "    elif cmd == 'c':\n        return 3\n"
            "    return 0\n"
        )
        hits = self.plant({"pkg/cli.py": src})
        self.assertEqual(self.rules(hits), [("pkg/cli.py", 2, "chain")])
        self.assertIn("3 string arms on 'cmd'", str(hits[0]))

    def test_unit_numbers_outside_their_home(self) -> None:
        src = "A = 86400\nB = 24 * 60 * 60\nC = 2 * 3600\nD = 60\n\n\ndef f(rc: int) -> bool:\n    return rc == 127\n"
        hits = self.plant({"pkg/domain.py": src, "cst_core/domain/values.py": "DAY = 86400\n"})
        self.assertEqual(
            [(h.line, h.rule, h.scope) for h in hits],
            [(1, "unit-number", "A"), (2, "unit-number", "B"), (3, "unit-number", "C"), (8, "unit-number", "f")],
        )
        self.assertIn("24 * 60 * 60 is DAY (cst_core.domain.values)", str(hits[1]))

    def test_loose_records_in_signatures(self) -> None:
        src = (
            "from collections.abc import Mapping\nfrom typing import Any\n\ntype Json = object\n"
            "type Rec = dict[str, Json]\n\n\n"
            "def f(a: Mapping[str, Any], b: dict[str, int], *, c: 'dict[str, object] | None' = None) -> Rec:\n"
            "    x: dict[str, Any] = {}\n    return x\n"
        )
        hits = self.plant({"pkg/adapters.py": src})
        self.assertEqual([(h.line, h.rule) for h in hits], [(8, "loose-record")] * 3)
        self.assertEqual(
            sorted(h.text.split(" is ")[0] for h in hits), ["f: parameter 'a'", "f: parameter 'c'", "f: return"]
        )

    def test_literal_vocabularies(self) -> None:
        src = "from typing import Literal\n\ntype Mode = Literal['a', 'b', 'c']\ntype Two = Literal['a', 'b']\nN: Literal[1, 2, 3] = 1\n"
        hits = self.plant({"pkg/domain.py": src})
        self.assertEqual([(h.line, h.rule, h.scope) for h in hits], [(3, "literal-vocabulary", "Mode")])

    def test_a_clean_file_passes(self) -> None:
        clean = (
            "from enum import StrEnum\nfrom typing import TypedDict, assert_never\n\nfrom cst_core.domain.values import DAY\n\n\n"
            "class Mode(StrEnum):\n    A = 'a'\n    B = 'b'\n    C = 'c'\n\n\n"
            "class Rec(TypedDict):\n    mode: str\n\n\n"
            "def f(m: Mode, r: Rec) -> int:\n"
            "    match m:\n"
            "        case Mode.A:\n            return DAY\n"
            "        case Mode.B | Mode.C:\n            return 2\n"
            "        case _:\n            assert_never(m)\n"
        )
        self.assertEqual(self.plant({"cst_core/domain/files.py": self.CORE, "pkg/domain.py": clean}), [])

    def test_exemptions_cover_nested_scopes_and_go_stale(self) -> None:
        hits = self.plant({"pkg/cli.py": "class K:\n    def f(self) -> int:\n        return 86400\n\n\nT = 3600\n"})
        self.assertEqual([h.where for h in hits], ["pkg/cli.py:K.f", "pkg/cli.py:T"])
        exempt = {"pkg/cli.py:K": "read from the source", "pkg/cli.py:gone": "nothing there"}
        self.assertEqual(counts(hits, exempt), {"pkg unit-number": 1})
        self.assertEqual(stale_exemptions(hits, exempt), ["pkg/cli.py:gone"])
        self.assertEqual(stale_exemptions(hits, {"pkg/cli.py:K.f": "x"}), [])
        self.assertEqual(
            budget_problems(counts(hits), {"pkg unit-number": 1}, "magic values"),
            ["pkg unit-number: 2 magic values, budget 1: the change added 1"],
        )


if __name__ == "__main__":
    unittest.main()
