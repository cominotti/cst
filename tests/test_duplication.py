# SPDX-License-Identifier: MPL-2.0
"""Duplication ratchet: copied code in production may only go down.

CEILINGS holds today's measurements (tests/duplication.py says how each is
taken). A change that copies code over a ceiling fails here; a change that
removes copies lowers the ceiling to the new number in the same change, so
the copies cannot come back. The run prints the numbers measured.

Where the hexagonal conversion left them (main before it, same method):
- python type 1, W=8, cross-file lines: 0 (main: 0);
- python type 2, W=8: 1.04 % (main: 0.85 %; 1.37 % before the /proc walk and
  the flag and config-state checks of toolchains were merged; the rest is
  the shape of thin adapters, ports' implementations and property lists, see
  the PR);
- shell, W=5: 0.47 % (main: 1.28 %);
- same-name definitions: only the names in ALLOWED_SAME_NAMES, each with its
  reason (main: 43 names, none allowed).
"""

import sys
from typing import Final
import unittest

from tests.code_graph import production_python
from tests.duplication import (
    clones,
    normalized_python,
    normalized_shell,
    python_clones,
    same_name_definitions,
    shell_clones,
    shell_function_names,
    shell_same_name_definitions,
    without_protocol_bodies,
)
from tests.budgets import is_test_file
from tests.repo_files import REPO
from tests.shell_files import shell_files
from tests.test_entrypoints import BOOTSTRAP, without_bootstrap

# One table: metric -> ceiling. Lower a number when a change brings it down.
CEILINGS: Final[dict[str, float]] = {
    "python type-1 W=8 duplicated %": 0.09,
    "python type-1 W=8 cross-file lines": 0,
    "python type-2 W=8 duplicated %": 1.02,
    "shell W=5 duplicated %": 0.47,
    "same-name definitions in >1 file": 0,
}

# Names that several files may define, and why.
# Each was compared body to body: none is a copy of another (a copy goes to
# cst_core instead). Most are the same verb in different programs.
ALLOWED_SAME_NAMES: Final[dict[str, str]] = {
    "main": "every program has its own main(): the entry point convention",
    # Subcommands and use cases: each program's own verb, different work.
    "apply": "the apply step of bulk items, chrome-cache and docker-gc: different machines changes",
    "plan": "docker-gc's plan subcommand vs the snapshot retention plan",
    "status": "the status subcommand of chrome-cache, docker-gc and docker-volumes-nocow",
    "cmd_list": "`btrfs-snapshot-core list` vs `cst-vm list`",
    "cmd_status": "`cst-toolchains status` vs `cst-vm status`",
    "migrate": "bulk moves a folder into ~/Bulk; docker-volumes-nocow re-creates a volume NOCOW",
    "recover": "bulk finishes an interrupted move; docker-volumes-nocow an interrupted volume copy",
    "report": "bulk's per-item report vs cpu-watch's run comparison",
    "run": "bluetooth and vm run commands with their own environment (LC_ALL, sessions); docker-gc's run loop",
    "parse_args": "each program's own argv grammar (doctor, toolchains, vm)",
    "classify": "a bulk folder, a kernel stack, a toolchain cache folder: three different classifications",
    "decide": "the LDAC bitrate, the Chrome cache place, a cache folder's fate: three different rules",
    "label": "a Docker volume's label vs a toolchain cache's display label",
    "locked": "docker-gc exits when busy; the snapshot run returns EXIT_BUSY; both over cst_core take_lock",
    # Types with the same short name in different programs.
    "Episode": "the daemon's live episode object vs the report's record of it (a TypedDict)",
    "Host": "the snapshot run's host adapter vs doctor's read-only host adapter (different ports)",
    "Program": "the installed bulk helper vs the installed cst-toolchains: what each program knows of itself",
    "Result": "a snapshot step's outcome vs a doctor check line",
    "Stop": "bt-ldac-watch's signal flag vs the vm backup's cancellation",
    # The same word, different measurements or rules.
    "free_bytes": "statvfs free space of a folder vs btrfs's estimated free space of the Docker volumes",
    "is_subvol": "bulk also checks the btrfs magic; chrome-cache only needs the subvolume inode",
    "gib": "each program's display threshold, both over cst_core format_bytes_gib_mib",
    "parse_size": "swap takes systemd sizes (plain numbers are bytes); toolchains requires a unit (150 is refused)",
    "render_service": "the toolchains and vm units' own text, both through cst_core.domain.units",
    "render_timer": "the toolchains and vm timers, both over cst_core.domain.units.render_timer",
    "systemctl": "toolchains runs systemctl --user and raises Refused; vm runs system systemctl for a status",
    "virsh": "bindings over cst_core's SubprocessVirshAdapter: cpu exits on a failure; doctor is read-only (-r); vm returns (rc, out, err)",
    "log": "each program's log binding (plain stdout for bulk, journal tags) over cst_core.adapters.journal.log",
    # Left for a follow-up (see the stage report): same idea, kept apart for now.
    "read_bytes": "btrfs-stall-monitord reads /proc with os.read until EOF and its tests fake os.read; "
    "cst_core's reads with open()",
    "read_text": "doctor reads through its host port (stripped, limited); the daemon over its read_bytes",
    "read_int": "doctor reads through its host port; the daemon returns 0 for a missing file",
}


# Shell functions that several shell files may define, and why. A copy of a
# helper is listed as one, with where it should live once; the installed
# programs (btrfs-notify, btrfs-snapshot, cpu/game-profile) run without the
# checkout and cannot source a file of it.
ALLOWED_SHELL_SAME_NAMES: Final[dict[str, str]] = {
    "check_syntax": "a copy in btrfs-setup.sh and cpu-setup.sh: the same check; to share in a sourced file",
    "cpuset_delegated": "cpu-setup.sh's check and the installed cpu/game-profile's, which cannot source the checkout",
    "die": "each script's own error exit with its own program name",
    "in_container": "a copy in four setup scripts: the same toolbox check; to share in a sourced file",
    "install_all": "the install step of btrfs-setup.sh and of cpu-setup.sh: different files",
    "is_subvol": "the installed btrfs-snapshot's check and setup-snapshots.bash's, which runs from the checkout",
    "lock": "btrfs-notify's queue lock vs btrfs-snapshot's run lock",
    "log": "each program's journal tag; the installed programs cannot source a shared file",
    "remove_game_dropins": "cpu-setup.sh's uninstall vs the installed game-profile's off",
    "root_fs_uuid": "a copy in btrfs-setup.sh and the degraded-boot setup: the same query; to share in a sourced file",
    "section": "each setup script's heading for its status output",
    "status": "each setup script's own status subcommand",
    "uninstall": "each setup script's own uninstall subcommand",
    "usage": "each script's help, printed from its own header",
}


def _shell_files() -> list[str]:
    return [rel for rel in shell_files() if not is_test_file(rel)]


def without_declared_copies(source: str) -> str:
    """The source without the code that is the same everywhere by design (see tests/duplication.py)."""
    return without_protocol_bodies(without_bootstrap(source))


def measure() -> dict[str, float]:
    """The current value of every metric in CEILINGS."""
    prod = production_python()
    t1 = python_clones(REPO, prod, 8, type2=False, prepare=without_declared_copies)
    t2 = python_clones(REPO, prod, 8, type2=True, prepare=without_declared_copies)
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
        self.assertEqual(set(measured), set(CEILINGS))

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
        kept = clones({p: normalized_python(without_declared_copies(s), type2=False) for p, s in files.items()}, 8)
        self.assertEqual(kept.duplicated, 0)
        # The adapter's own code still counts: a copied body is still found.
        body = "".join(f"        x{i} = f({i})\n" for i in range(8))
        two = {p: without_declared_copies(f"class {p}:\n    def g(self):\n{body}") for p in ("B", "C")}
        self.assertEqual(clones({p: normalized_python(s, type2=False) for p, s in two.items()}, 8).cross_file, 18)

    def test_type2_skips_data_rows_and_type1_keeps_them(self) -> None:
        rows = {p: "X = (\n" + "".join(f'    "{p}{i}",\n' for i in range(9)) + ")\n" for p in ("a", "b")}
        self.assertEqual(clones({p: normalized_python(s, type2=True) for p, s in rows.items()}, 8).duplicated, 0)
        same = {p: rows["a"] for p in ("a", "b")}
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
        stale = sorted(set(ALLOWED_SHELL_SAME_NAMES) - set(every))
        self.assertEqual(stale, [], "no longer defined in two files: remove from ALLOWED_SHELL_SAME_NAMES")

    def test_shell_function_names(self) -> None:
        src = "f() {\n  :\n}\nfunction g {\n:\n}\n  h_2 ()\n{ :; }\nfunction k() { :; }\necho x() # not one\n"
        self.assertEqual(shell_function_names(src), {"f", "g", "h_2", "k"})

    def test_every_allowed_name_is_still_defined_twice(self) -> None:
        defined = same_name_definitions(REPO, production_python(), ["main"])
        stale = sorted(set(ALLOWED_SAME_NAMES) - set(defined) - {"main"})
        self.assertEqual(stale, [], "no longer defined in two files: remove from ALLOWED_SAME_NAMES")

    def test_same_name_definitions_skip_allowed_names(self) -> None:
        names = same_name_definitions(REPO, production_python(), ALLOWED_SAME_NAMES)
        self.assertNotIn("main", names)


if __name__ == "__main__":
    unittest.main()
