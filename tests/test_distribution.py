"""Distribution gate: what `install` writes works where it is written.

For INSTALL_MANIFEST (cst_core/domain/install.py), the one list of installed
files:
- the manifest and the repository agree: every source exists, and every
  file that looks installable is in the manifest or named as checkout-only;
- every python-entry's zipapp is self-contained: each import of each member
  (function-level ones too, by AST) is the standard library or a package in
  the archive, and no member loads code from a file path at run time;
- modes let the runner read (and run) the file;
- where an entry has a known safe help mode, its zipapp runs it from `/`
  with `/usr/bin/python3 -I` and no PYTHONPATH (exit as expected, no
  ImportError), staged under a temporary root;
- shell files parse (bash -n) and every file a shell script sources is
  named by a `# shellcheck source=` line and exists in the checkout.

Nothing here touches the machine: zipapps are built in memory or staged
under temporary folders.
"""

import ast
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Final
import unittest
import zipfile

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.imports import imported_modules, loads_code_by_path
from cst_core.domain.install import (
    CHECKOUT_ONLY_EXECUTABLES,
    CHECKOUT_ONLY_FILES,
    INSTALL_MANIFEST,
    SETUP_ENTRYPOINTS,
    InstallEntry,
    python_entry_srcs,
)
from cst_core.domain.values import OctalMode
from tests.repo_files import REPO, repo_files

SYSTEM_PYTHON: Final = "/usr/bin/python3"

# A safe help mode per python-entry: argv that returns before the program
# touches the machine (read from each main()), and the exit status it gives.
# An entry without one is checked statically only.
HELP_MODES: Final[dict[str, tuple[tuple[str, ...], int]]] = {
    "btrfs/btrfs-bulk": (("--help",), 2),
    "btrfs/btrfs-snapshot-core": (("--help",), 0),
    "btrfs/btrfs-stall-monitord": (("--help",), 0),
    "btrfs/btrfs-stall-report": (("--help",), 0),
    "btrfs/docker-gc": (("--help",), 2),
    "btrfs/docker-volumes-nocow": (("--help",), 2),
    "cpu/cpu-watch": (("--help",), 2),
    "cpu/vm-pin": (("--help",), 2),
    "bluetooth/bt-ldac-watch": (("--help",), 0),
    "toolchains/cst-toolchains": (("help",), 0),
    "vm/cst-vm": (("help",), 0),
}

# Repository folders and names that hold installable files: anything under
# them must be a manifest source (or checkout-only, with a reason).
_INSTALLABLE_DIRS: Final = ("/units/", "/sysctl/", "/tmpfiles/", "/snapshots/")
_INSTALLABLE_SUFFIXES: Final = (".in", ".conf", ".ini", ".rules", ".service", ".timer", ".slice", ".mount")
_NOT_SHIPPED_PREFIXES: Final = ("tests/", ".github/", "docs/", ".idea/")


def _is_production(rel: str) -> bool:
    return not rel.startswith(_NOT_SHIPPED_PREFIXES) and "/tests/" not in rel


def _stdlib(name: str) -> bool:
    top = name.split(".", 1)[0]
    return top in sys.stdlib_module_names or top == "__future__"


def zipapp_import_problems(src: str, data: bytes) -> list[str]:
    """Imports in the zipapp of `src` that neither the stdlib nor the archive provides."""
    problems: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        provided = {n.split("/", 1)[0] for n in names if "/" in n} | {
            n[: -len(".py")] for n in names if "/" not in n and n.endswith(".py")
        }
        for member in names:
            if not member.endswith(".py"):
                continue
            text = zf.read(member).decode("utf-8")
            for mod in sorted(imported_modules(text, member)):
                top = mod.split(".", 1)[0]
                if not _stdlib(mod) and top not in provided:
                    problems.append(f"{src}: {member} imports {mod}, which the zipapp does not hold")
            for line in loads_code_by_path(text, member):
                problems.append(f"{src}: {member}:{line} loads code from a file path (invisible to the bundle)")
    return problems


def mode_problems(e: InstallEntry) -> list[str]:
    """Problems with the entry's mode for its runner."""
    bits = e.mode.bits
    need = 0o5 if e.kind in ("python-entry", "shell") else 0o4  # r-x for programs, r-- for the rest
    shift = 6 if e.runner == e.owner else 0  # the owner's bits, else everyone's
    if (bits >> shift) & need != need:
        return [f"{e.src}: mode {bits:04o} does not let {e.runner} {'run' if need == 0o5 else 'read'} {e.dest}"]
    return []


def manifest_completeness_problems(files: list[str]) -> list[str]:
    """Sources missing from the repository, and installable-looking files missing from the manifest."""
    srcs = {e.src for e in INSTALL_MANIFEST}
    problems = [f"manifest source missing: {s}" for s in sorted(srcs) if not os.path.isfile(os.path.join(REPO, s))]
    for rel in files:
        if not _is_production(rel) or rel in srcs or rel in CHECKOUT_ONLY_FILES:
            continue
        if rel in SETUP_ENTRYPOINTS or rel in CHECKOUT_ONLY_EXECUTABLES:
            continue
        path = os.path.join(REPO, rel)
        looks = any(d in "/" + rel for d in _INSTALLABLE_DIRS) or rel.endswith(_INSTALLABLE_SUFFIXES)
        executable = os.access(path, os.X_OK) and not os.path.isdir(path)
        if looks or executable:
            problems.append(f"{rel}: installable-looking file in neither INSTALL_MANIFEST nor a checkout-only list")
    return problems


_SOURCE_LINE = re.compile(r"^\s*(?:source|\.)\s+\S")
_DIRECTIVE = re.compile(r"#\s*shellcheck\s+source=(\S+)")


def source_problems(rel: str, text: str) -> list[str]:
    """`source`/`.` lines without a `# shellcheck source=` naming an existing file."""
    problems: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _SOURCE_LINE.match(line):
            continue
        prev = lines[i - 1] if i else ""
        m = _DIRECTIVE.search(prev)
        if m is None:
            problems.append(f"{rel}:{i + 1}: sources a file without a '# shellcheck source=PATH' line above it")
        elif not os.path.isfile(os.path.join(REPO, m.group(1))):
            problems.append(f"{rel}:{i + 1}: sources {m.group(1)}, which is not in the checkout")
    return problems


class DistributionGateTest(unittest.TestCase):
    def test_manifest_and_repository_agree(self) -> None:
        problems = manifest_completeness_problems(repo_files())
        self.assertEqual(problems, [], "\n".join(problems))

    def test_checker_flags_an_unlisted_unit(self) -> None:
        self.assertTrue(manifest_completeness_problems(["cpu/units/new.service"]))
        self.assertEqual(manifest_completeness_problems(["cpu/README.md"]), [])

    def test_every_python_entry_zipapp_is_self_contained(self) -> None:
        problems: list[str] = []
        for e in python_entry_srcs():
            problems.extend(zipapp_import_problems(e.src, build_python_zipapp(REPO, e.src)))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_import_checker_sees_function_level_and_path_loads(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-dist-")
        self.addCleanup(shutil.rmtree, tmp)
        Path(tmp, "cst_core").mkdir()
        Path(tmp, "cst_core", "__init__.py").write_text("")
        Path(tmp, "x").mkdir()
        Path(tmp, "x", "tool").write_text(
            "def main():\n    import notbundled.sub\n    import importlib.machinery\n"
            "    importlib.machinery.SourceFileLoader('m', '/repo/m')\n"
        )
        problems = zipapp_import_problems("x/tool", build_python_zipapp(tmp, "x/tool"))
        self.assertEqual(len(problems), 2, problems)

    def test_modes_let_the_runner_use_the_file(self) -> None:
        problems = [p for e in INSTALL_MANIFEST for p in mode_problems(e)]
        self.assertEqual(problems, [], "\n".join(problems))
        bad = InstallEntry("m/x", "/usr/local/bin/x", OctalMode(0o750), "root", "shell", "user")
        self.assertTrue(mode_problems(bad))

    def test_help_modes_name_python_entries(self) -> None:
        self.assertLessEqual(set(HELP_MODES), {e.src for e in python_entry_srcs()})

    @unittest.skipUnless(os.access(SYSTEM_PYTHON, os.X_OK), "needs /usr/bin/python3")
    def test_python_entries_run_their_help_from_root_without_the_checkout(self) -> None:
        stage = tempfile.mkdtemp(prefix="cst-dist-run-")
        self.addCleanup(shutil.rmtree, stage)
        for e in python_entry_srcs():
            if e.src not in HELP_MODES:
                continue
            argv, rc = HELP_MODES[e.src]
            dest = os.path.join(stage, e.src.replace("/", "_"))
            Path(dest).write_bytes(build_python_zipapp(REPO, e.src))
            os.chmod(dest, e.mode.bits)
            with self.subTest(src=e.src):
                r = subprocess.run(
                    [SYSTEM_PYTHON, "-I", dest, *argv],
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd="/",
                    env={"PATH": "/usr/bin:/bin", "HOME": stage, "LC_ALL": "C.UTF-8"},
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
                self.assertNotRegex(r.stderr, r"ImportError|ModuleNotFoundError|Traceback", r.stderr)
                self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
                # argparse names a zipapp "python3 ARCHIVE" unless the program sets prog=.
                self.assertNotRegex(r.stdout + r.stderr, r"(?m)^usage: python", "usage names the program")

    @unittest.skipUnless(os.access(SYSTEM_PYTHON, os.X_OK), "needs /usr/bin/python3")
    def test_an_installed_entry_finds_its_siblings_at_their_installed_dests(self) -> None:
        # btrfs-bulk runs btrfs-snapshot: from a zipapp, the installed one, not a path inside the archive.
        stage = tempfile.mkdtemp(prefix="cst-dist-sibling-")
        self.addCleanup(shutil.rmtree, stage)
        dest = os.path.join(stage, "btrfs-bulk")
        Path(dest).write_bytes(build_python_zipapp(REPO, "btrfs/btrfs-bulk"))
        # What main() does with the entry's __file__, run from the archive's own copy of the package.
        main_file = os.path.join(dest, "__main__.py")
        probe = (
            f"import sys; sys.path.insert(0, {dest!r})\n"
            "from btrfs.bulk.adapters import Program, snapshot_program\n"
            f"Program.entry_file = {main_file!r}\n"
            "print(snapshot_program())"
        )
        r = subprocess.run(
            [SYSTEM_PYTHON, "-I", "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "/usr/local/sbin/btrfs-snapshot"), r.stderr)

    def test_shell_files_parse_and_source_only_checkout_files(self) -> None:
        problems: list[str] = []
        shell = [e.src for e in INSTALL_MANIFEST if e.kind == "shell"]
        for rel in sorted(set(shell) | SETUP_ENTRYPOINTS):
            path = os.path.join(REPO, rel)
            r = subprocess.run(
                ["bash", "-n", path], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
            )
            if r.returncode != 0:
                problems.append(f"{rel}: bash -n: {r.stderr.strip()}")
            problems.extend(source_problems(rel, Path(path).read_text(encoding="utf-8")))
        # An installed shell program runs without the checkout: it may source nothing of CST.
        for rel in shell:
            for i, line in enumerate(Path(REPO, rel).read_text(encoding="utf-8").splitlines()):
                if _SOURCE_LINE.match(line):
                    problems.append(f"{rel}:{i + 1}: an installed shell program sources a file")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_source_checker(self) -> None:
        self.assertTrue(source_problems("x.sh", 'source "$DIR/a.bash"\n'))
        self.assertTrue(source_problems("x.sh", '# shellcheck source=no/such.bash\n. "$DIR/a"\n'))
        self.assertEqual(source_problems("x.sh", '# shellcheck source=cst-link.bash\nsource "$D/cst-link.bash"\n'), [])

    def test_entry_sources_parse(self) -> None:
        for e in python_entry_srcs():
            with self.subTest(src=e.src):
                ast.parse(Path(REPO, e.src).read_text(encoding="utf-8"), filename=e.src)


if __name__ == "__main__":
    unittest.main()
