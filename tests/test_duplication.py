# SPDX-License-Identifier: MPL-2.0
"""Duplication ceilings: copied code in production may not go above them.

CEILINGS holds the highest value allowed for each measurement
(tests/duplication.py says how each is taken). A change that copies code
over a ceiling fails here; a value below its ceiling passes, and
`invowk cmd fix budgets` lowers the ceilings to the measured values (never
raising one), so the copies cannot come back once it has run. The run prints
the numbers measured.

Where the hexagonal conversion left them (main before it, same method):
- python type 1, W=8, cross-file lines: 0 (main: 0);
- python type 2, W=8: 1.04 % (main: 0.85 %; 1.37 % before the /proc walk and
  the flag and config-state checks of toolchains were merged; the rest is
  the shape of thin adapters, ports' implementations and property lists, see
  the PR); 0.57 % (from 0.83 %) once record fields and plain `__init__`
  bodies were left out of type 2 (2026-11; the `__init__` rule moved
  nothing: no such body was in a duplicated window);
- shell, W=5: 0.47 % (main: 1.28 %);
- same-name definitions: only the names in ALLOWED_SAME_NAMES, each with its
  reason (main: 43 names, none allowed).
"""

import sys
from typing import Final
import unittest

from tests.code_graph import production_python
from tests.duplication import (
    BARRIER,
    clones,
    declared_lines,
    measured_python,
    normalized_python,
    normalized_shell,
    python_clones,
    same_name_definitions,
    shell_clones,
    shell_function_names,
    shell_same_name_definitions,
)
from tests.budgets import is_test_file
from tests.repo_files import REPO
from tests.shell_files import shell_files
from tests.test_entrypoints import BOOTSTRAP, without_bootstrap

# One table: metric -> ceiling. `invowk cmd fix budgets` lowers a number the code is below.
CEILINGS: Final[dict[str, float]] = {
    "python type-1 W=8 duplicated %": 0.05,
    "python type-1 W=8 cross-file lines": 0,
    "python type-2 W=8 duplicated %": 0.37,
    "shell W=5 duplicated %": 0.0,
    "same-name definitions in >1 file": 0,
}

# Names that several files may define, and why. An entry no longer defined
# twice passes; `invowk cmd fix budgets` deletes it.
# Each was compared body to body: none is a copy of another (a copy goes to
# cst_core instead). Most are the same verb in different programs.
ALLOWED_SAME_NAMES: Final[dict[str, str]] = {
    "main": "every program has its own main(): the entry point convention",
    # Subcommands and use cases: each program's own verb, different work.
    "apply": "the apply step of bulk items and chrome-cache: different machines changes",
    "cmd_list": "`btrfs-snapshot-core list` vs `cst-vm list`",
    "cmd_status": "`cst-toolchains status` vs `cst-vm status`",
    "report": "bulk's per-item report vs cpu-watch's run comparison",
    "run": "bluetooth's and vm's bindings over cst_core's SubprocessCommandRunner (their own options and words)",
    "parse_args": "each program's own argv grammar (doctor, toolchains, vm)",
    "classify": "a bulk folder, a kernel stack, a toolchain cache folder: three different classifications",
    "decide": "the LDAC bitrate, the Chrome cache place, a cache folder's fate: three different rules",
    "label": "a Docker volume's label vs a toolchain cache's display label",
    # Types with the same short name in different programs.
    "Episode": "the daemon's live episode object vs the report's record of it (a TypedDict)",
    "Host": "the snapshot run's host adapter vs doctor's read-only host adapter vs bulk's HostPort "
    "(btrfs-snapshot's busy check, Steam's processes, the alert): different ports",
    "Result": "a snapshot step's outcome vs a doctor check line",
    "Stop": "bt-ldac-watch's signal flag vs the vm backup's cancellation",
    # The same word, different measurements or rules.
    "gib": "each program's display threshold, both over cst_core format_bytes_gib_mib",
    "parse_size": "swap takes systemd sizes (plain numbers are bytes); toolchains requires a unit (150 is refused); "
    "both over cst_core ByteSize (parse, parse_iec)",
    "render_service": "the toolchains and vm units' own text, both through cst_core.domain.units",
    "render_timer": "the toolchains and vm timers, both over cst_core.domain.units.render_timer",
    "virsh": "bindings over cst_core's SubprocessVirshAdapter: cpu raises Refused on a failure; doctor is read-only (-r); vm returns (rc, out, err)",
    "log": "each program's log binding (plain stdout for bulk, journal tags) over cst_core.adapters.journal.log",
    # Left for a follow-up (see the stage report): same idea, kept apart for now.
    "read_bytes": "btrfs-stall-monitord reads /proc with os.read until EOF and its tests fake os.read; "
    "cst_core's reads with open()",
    "read_text": "doctor reads through its host port (stripped, limited); the daemon over its read_bytes",
    "read_int": "doctor reads through its host port; the daemon returns 0 for a missing file",
}


# Shell functions that several shell files may define, and why. A copy of a
# helper is listed as one, with where it should live once; the installed
# programs (btrfs-notify, btrfs-snapshot) run without the
# checkout and cannot source a file of it. An entry no longer defined twice
# passes; `invowk cmd fix budgets` deletes it.
ALLOWED_SHELL_SAME_NAMES: Final[dict[str, str]] = {
    "is_subvol": "the installed btrfs-snapshot's check and setup-snapshots.bash's, which runs from the checkout",
    "lock": "btrfs-notify's queue lock vs btrfs-snapshot's run lock",
    "log": "each program's journal tag; the installed programs cannot source a shared file",
    "root_fs_uuid": "a copy in btrfs-setup.sh and the degraded-boot setup: the same query; to share in a sourced file",
    "status": "each setup script's own status subcommand",
    "uninstall": "each setup script's own uninstall subcommand",
    "usage": "each script's help, printed from its own header",
}


def _shell_files() -> list[str]:
    return [rel for rel in shell_files() if not is_test_file(rel)]


def measured_lines(source: str, *, type2: bool) -> list[str]:
    """What the measure reads of one source: the bootstrap removed, the declared lines as barriers."""
    return measured_python(without_bootstrap(source), type2=type2)


def same_names_defined() -> set[str]:
    """The top-level names defined in more than one production file, and "main": what ALLOWED_SAME_NAMES may name."""
    return {*same_name_definitions(REPO, production_python(), ["main"]), "main"}


def shell_same_names_defined() -> set[str]:
    """The shell functions defined in more than one shell file: what ALLOWED_SHELL_SAME_NAMES may name."""
    return set(shell_same_name_definitions(REPO, _shell_files(), []))


def measure() -> dict[str, float]:
    """The current value of every metric in CEILINGS."""
    prod = production_python()
    t1 = python_clones(REPO, prod, 8, type2=False, prepare=without_bootstrap)
    t2 = python_clones(REPO, prod, 8, type2=True, prepare=without_bootstrap)
    sh = shell_clones(REPO, _shell_files(), 5)
    names = same_name_definitions(REPO, prod, ALLOWED_SAME_NAMES)
    return {
        "python type-1 W=8 duplicated %": round(t1.percent, 2),
        "python type-1 W=8 cross-file lines": t1.cross_file,
        "python type-2 W=8 duplicated %": round(t2.percent, 2),
        "shell W=5 duplicated %": round(sh.percent, 2),
        "same-name definitions in >1 file": len(names),
    }


class DuplicationRatchetTest(unittest.TestCase):
    def test_no_metric_above_its_ceiling(self) -> None:
        measured = measure()
        print("\nduplication (measured / ceiling):", file=sys.stderr)
        for key, ceiling in CEILINGS.items():
            print(f"  {key:<38} {measured[key]:>8} / {ceiling}", file=sys.stderr)
        over = {k: (measured[k], c) for k, c in CEILINGS.items() if measured[k] > c}
        self.assertEqual(over, {}, "duplication went up (measured, ceiling); share the code instead of copying it")
        self.assertEqual(set(measured), set(CEILINGS), "one ceiling per measurement")

    def test_type1_finds_a_copied_block_and_type2_a_renamed_copy(self) -> None:
        body = "".join(f"    x{i} = f(a, {i})\n" for i in range(8))
        a = f"def one(a):\n{body}"
        b = f"def two(a):\n{body}"
        renamed = "def three(b):\n" + "".join(f"    y{i} = g(b, {i + 1})\n" for i in range(8))
        norm1 = {p: normalized_python(src, type2=False) for p, src in (("a", a), ("b", b), ("c", renamed))}
        rep = clones(norm1, 8)
        self.assertEqual((rep.cross_file, rep.per_file.get("c", 0)), (16, 0))
        norm2 = {p: normalized_python(src, type2=True) for p, src in (("a", a), ("c", renamed))}
        self.assertEqual(clones(norm2, 8).cross_file, 18)

    def test_a_wrapped_import_list_is_one_line(self) -> None:
        names = "".join(f"    name{i},\n" for i in range(12))
        a = f"from pkg.one import (\n{names})\nimport os\n"
        b = f"from pkg.two import (\n{names.replace('name', 'other')})\n"
        self.assertEqual(
            normalized_python(a, type2=False)[0],
            "from pkg . one import ( " + " ".join(f"name{i} ," for i in range(12)) + " )",
        )
        self.assertEqual(len(normalized_python(a, type2=True)), 2)
        self.assertEqual(normalized_python(a, type2=True)[1], "import os")
        # Type 2 keeps an import's names: eight different imports are no renamed copy of eight others.
        imports = ["os", "re", "sys", "json", "time", "stat", "errno", "shutil", "subprocess"]
        blocks = {p: "".join(f"import {m}\n" for m in mods) for p, mods in (("x", imports[:8]), ("y", imports[1:]))}
        self.assertEqual(clones({p: normalized_python(src, type2=True) for p, src in blocks.items()}, 8).duplicated, 0)
        self.assertEqual(
            clones({p: normalized_python(src, type2=True) for p, src in (("a", a), ("b", b))}, 8).duplicated, 0
        )
        # Only a statement that starts with `from`/`import`: `raise ... from` and a copied body still count.
        self.assertEqual(
            normalized_python("raise E(\n    x\n) from None\n", type2=False), ["raise E (", "x", ") from None"]
        )
        body = "".join(f"    x{i} = f(a, {i})\n" for i in range(8))
        copied = {
            p: normalized_python(f"{imports}def {p}(a):\n{body}", type2=True) for p, imports in (("one", a), ("two", b))
        }
        self.assertEqual(clones(copied, 8).cross_file, 18)

    def test_the_bootstrap_and_protocol_stubs_do_not_count(self) -> None:
        sig = "".join(f"        a{i}: int,\n" for i in range(8))
        port = (
            f"from typing import Protocol\n\nclass P(Protocol):\n    def run(\n        self,\n{sig}    ) -> int: ...\n"
        )
        impl = f"class A:\n    def run(\n        self,\n{sig}    ) -> int:\n        return 1\n"
        entry = f"{BOOTSTRAP}\nimport sys\nfrom cpu import cli\n\ndef main() -> int:\n    return 0\n"
        files = {"port": port, "impl": impl, "e1": entry, "e2": entry}
        raw = clones({p: normalized_python(s, type2=False) for p, s in files.items()}, 8)
        self.assertEqual(set(raw.per_file), {"port", "impl", "e1", "e2"})
        kept = clones({p: measured_lines(s, type2=False) for p, s in files.items()}, 8)
        self.assertEqual(kept.duplicated, 0)
        # The adapter's own code still counts: a copied body is still found.
        body = "".join(f"        x{i} = f({i})\n" for i in range(8))
        two = {p: measured_lines(f"class {p}:\n    def g(self):\n{body}", type2=False) for p in ("B", "C")}
        self.assertEqual(clones(two, 8).cross_file, 18)

    def test_type2_leaves_out_record_fields_and_plain_inits_and_type1_keeps_them(self) -> None:
        fields = "".join(f"    f{i}: int\n" for i in range(9))
        records = {
            "dc": f"from dataclasses import dataclass\n\n@dataclass(frozen=True, slots=True)\nclass A:\n{fields}",
            "td": f"from typing import TypedDict\n\nclass B(TypedDict):\n{fields.replace('f', 'g')}",
            "nt": f"import typing\n\nclass C(typing.NamedTuple):\n{fields.replace('f', 'h')}",
        }
        defaults = "from dataclasses import dataclass, field\n\n@dataclass\nclass D:\n    a: int = 0\n    b: list[int] = field(\n        default_factory=list\n    )\n    C = 1\n"
        self.assertEqual(
            declared_lines(defaults, records=True), {5, 6, 7, 8}, "fields with defaults, not a plain assignment"
        )
        self.assertEqual(clones({p: normalized_python(s, type2=True) for p, s in records.items()}, 8).cross_file, 27)
        report = clones({p: measured_lines(s, type2=True) for p, s in records.items()}, 8)
        self.assertEqual(report.duplicated, 0)
        self.assertEqual(report.code_lines, 7, "fields are no code lines")
        # Type 1 still finds a field list copied word for word.
        same = {p: measured_lines(records["dc"].replace("class A", f"class {p}"), type2=False) for p in ("X", "Y")}
        self.assertEqual(clones(same, 8).cross_file, 18)

        # An __init__ that only stores its parameters is a declaration; one that computes is code.
        def init(cls: str, op: str) -> str:
            args = ", ".join(f"a{i}" for i in range(8))
            body = "".join(f"        self.a{i} = {op.format(i)}\n" for i in range(8))
            return f"class {cls}:\n    def __init__(self, {args}):\n{body}"

        plain = {p: measured_lines(init(p, "a{}"), type2=True) for p in ("P", "Q")}
        self.assertEqual(clones(plain, 8).duplicated, 0)
        computed = {p: measured_lines(init(p, "a{} + 1"), type2=True) for p in ("P", "Q")}
        self.assertEqual(clones(computed, 8).cross_file, 20)
        self.assertEqual(declared_lines(records["dc"], records=False), set())

    def test_type2_leaves_out_enum_members_and_type1_keeps_them(self) -> None:
        def enum(cls: str, base: str, prefix: str) -> str:
            members = "".join(f'    {prefix}{i} = "{prefix}{i}"\n' for i in range(9))
            return f"import enum\nfrom enum import StrEnum\n\nclass {cls}({base}):\n{members}"

        enums = {"a": enum("A", "StrEnum", "x"), "b": enum("B", "StrEnum", "y"), "c": enum("C", "enum.IntEnum", "z")}
        self.assertEqual(declared_lines(enums["a"], records=True), set(range(5, 14)))
        self.assertEqual(declared_lines(enums["a"], records=False), set())
        self.assertEqual(clones({p: normalized_python(s, type2=True) for p, s in enums.items()}, 8).cross_file, 33)
        self.assertEqual(clones({p: measured_lines(s, type2=True) for p, s in enums.items()}, 8).duplicated, 0)
        # Type 1 still finds a member list copied word for word.
        same = {p: measured_lines(enums["a"].replace("class A", f"class {p}"), type2=False) for p in ("X", "Y")}
        self.assertEqual(clones(same, 8).cross_file, 18)
        # A plain class's assignments are code.
        plain = {p: measured_lines(enums[p].replace("(StrEnum)", ""), type2=True) for p in ("a", "b")}
        self.assertEqual(clones(plain, 8).cross_file, 24)

    def test_type2_leaves_out_final_literal_constants_and_type1_keeps_them(self) -> None:
        def consts(prefix: str, annotation: str = ": Final", value: str = '"{}"') -> str:
            rows = "".join(f"{prefix}_{i}{annotation} = {value.format(prefix + str(i))}\n" for i in range(9))
            return f"from typing import Final\nimport typing\n\n{rows}"

        files = {"a": consts("A"), "b": consts("B")}
        self.assertEqual(declared_lines(files["a"], records=True), set(range(4, 13)))
        self.assertEqual(declared_lines(files["a"], records=False), set())
        self.assertEqual(declared_lines(consts("C", ": Final[str]"), records=True), set(range(4, 13)))
        self.assertEqual(
            declared_lines(consts("D", ": typing.Final", "({!r}, 1, -2)"), records=True), set(range(4, 13))
        )
        self.assertEqual(clones({p: normalized_python(s, type2=True) for p, s in files.items()}, 8).cross_file, 22)
        self.assertEqual(clones({p: measured_lines(s, type2=True) for p, s in files.items()}, 8).duplicated, 0)
        # Type 1 still finds a list of constants copied word for word.
        same = {p: measured_lines(files["a"], type2=False) for p in ("X", "Y")}
        self.assertEqual(clones(same, 8).cross_file, 22)
        # Only module-level Final constants whose value is a literal: a plain
        # assignment, a computed value and a Final inside a function are code.
        plain = {p: measured_lines(consts(p, ""), type2=True) for p in ("a", "b")}
        self.assertEqual(clones(plain, 8).cross_file, 22)
        computed = {p: measured_lines(consts(p, value='f("{}")'), type2=True) for p in ("a", "b")}
        self.assertEqual(clones(computed, 8).cross_file, 22)
        inner = "def f():\n" + "".join(f'    X_{i}: Final = "x{i}"\n' for i in range(9))
        self.assertEqual(declared_lines(inner, records=True), set())

    def test_a_generic_protocol_body_is_left_out(self) -> None:
        port = "from typing import Protocol\n\nclass P[T](Protocol):\n    def f(self) -> T: ...\n"
        old_style = (
            "import typing\n\nclass Q(typing.Protocol[T]):\n    def f(self) -> T: ...\n    def g(self) -> T: ...\n"
        )
        self.assertEqual(declared_lines(port, records=False), {4})
        self.assertEqual(declared_lines(old_style, records=False), {4, 5})

    def test_a_left_out_block_does_not_join_the_lines_around_it(self) -> None:
        fields = "".join(f"    f{i}: int\n" for i in range(9))
        code = "    def m(self):\n" + "".join(f"        x{i} = self.f{i} + {i}\n" for i in range(5))
        files = {
            "a": f"@dataclass\nclass A:\n{fields}{code}",
            "b": f"@dataclass\nclass B:\n{code}",
        }
        # Deleting a's fields would make its class line and method read as b's eight lines.
        self.assertEqual(clones({p: measured_lines(s, type2=True) for p, s in files.items()}, 8).duplicated, 0)
        self.assertIn(BARRIER, measured_lines(files["a"], type2=True))
        deleted = {p: [x for x in measured_lines(s, type2=True) if x != BARRIER] for p, s in files.items()}
        self.assertEqual(clones(deleted, 8).cross_file, 16)

    def test_type2_skips_data_rows_and_type1_keeps_them(self) -> None:
        rows = {p: "X = (\n" + "".join(f'    "{p}{i}",\n' for i in range(9)) + ")\n" for p in ("a", "b")}
        self.assertEqual(clones({p: normalized_python(s, type2=True) for p, s in rows.items()}, 8).duplicated, 0)
        same = dict.fromkeys(("a", "b"), rows["a"])
        self.assertEqual(clones({p: normalized_python(s, type2=False) for p, s in same.items()}, 8).cross_file, 20)
        self.assertEqual(normalized_python('f("a", 1)\n', type2=True), ["N ( S , 0 )"])

    def test_comments_docstrings_and_trivial_lines_do_not_count(self) -> None:
        src = 'def f():\n    """doc"""\n    # comment\n    return (\n        1\n    )\n'
        self.assertEqual(normalized_python(src, type2=False), ["def f ( ) :", "return (", "1"])
        self.assertEqual(normalized_shell("# c\nif x; then\n  a  b\nfi\n"), ["if x; then", "a b"])

    def test_shell_functions_are_not_defined_twice(self) -> None:
        every = shell_same_name_definitions(REPO, _shell_files(), [])
        self.assertEqual(
            {name: files for name, files in every.items() if name not in ALLOWED_SHELL_SAME_NAMES},
            {},
            "a shell function defined in several files: share it, or add it to ALLOWED_SHELL_SAME_NAMES with the reason",
        )

    def test_shell_function_names(self) -> None:
        src = "f() {\n  :\n}\nfunction g {\n:\n}\n  h_2 ()\n{ :; }\nfunction k() { :; }\necho x() # not one\n"
        self.assertEqual(shell_function_names(src), {"f", "g", "h_2", "k"})

    def test_same_name_definitions_skip_allowed_names(self) -> None:
        names = same_name_definitions(REPO, production_python(), ALLOWED_SAME_NAMES)
        self.assertNotIn("main", names)


if __name__ == "__main__":
    unittest.main()
