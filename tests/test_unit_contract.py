"""Unit contract: every program a unit starts or checks for is an installed CST file that its runner can run.

For every unit CST installs or writes (tests/unit_files.py: the manifest's
units, templates rendered, and the units cst-vm, cst-toolchains, the LDAC
watcher and the degraded-boot setup generate), each absolute path under
/usr/local/ or %h/.local/ in an Exec*= line (ExecStart, ExecStartPost,
ExecCondition, ...) must be a manifest destination of kind python-entry or
shell whose mode lets the unit's runner (root for a system unit, the user
for a user unit) read and run it. A path in a Condition*= or Assert*= line
must be a manifest destination too. A renamed or dropped helper otherwise
leaves a unit that fails at its next start, which systemd reports only then.

tier 1's test_16 runs `systemd-analyze verify` and `security` on the same
units for what a parser cannot see.
"""

from collections.abc import Mapping
import unittest

from cst_core.domain.install import InstallEntry, manifest_by_dest
from cst_core.domain.values import OctalMode
from tests.unit_files import UnitText, generated_units, manifest_units

ROOTS = ("/usr/local/", "%h/.local/")


def unit_paths(text: str) -> list[tuple[str, str]]:
    """(key, path) for every path under ROOTS in an Exec*, Condition* or Assert* setting."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not key.startswith(("Exec", "Condition", "Assert")):
            continue
        for word in value.split():
            word = word.lstrip("-@:+!|")
            if word.startswith(ROOTS):
                out.append((key, word))
    return out


def contract_problems(unit: UnitText, by_dest: Mapping[str, InstallEntry]) -> list[str]:
    """What is wrong with the paths one unit names, as text lines (empty: nothing)."""
    runner = "root" if unit.scope == "system" else "user"
    problems: list[str] = []
    for key, path in unit_paths(unit.text):
        dest = "~/" + path[len("%h/") :] if path.startswith("%h/") else path
        entry = by_dest.get(dest)
        where = f"{unit.origin}: {unit.name}: {key}={path}"
        if entry is None:
            problems.append(f"{where}: not a file INSTALL_MANIFEST installs")
            continue
        if not key.startswith("Exec"):
            continue
        if entry.kind not in ("python-entry", "shell"):
            problems.append(f"{where}: {entry.src} is of kind {entry.kind}, not a program")
            continue
        shift = 6 if entry.owner == runner else 0
        if (entry.mode.bits >> shift) & 0o5 != 0o5:
            problems.append(f"{where}: mode {entry.mode.bits:04o} does not let {runner} run it")
    return problems


class UnitContractTest(unittest.TestCase):
    def test_every_unit_names_installed_programs(self) -> None:
        by_dest = manifest_by_dest()
        units = manifest_units() + generated_units()
        problems = [p for u in units for p in contract_problems(u, by_dest)]
        self.assertEqual(problems, [], "\n".join(problems))
        # The gate sees the units it is meant for.
        names = {u.name for u in units}
        for name in ("btrfs-snapshot.service", "cst-vm-backup@.service", "cst-bt-ldac48.service"):
            self.assertIn(name, names)
        self.assertTrue(any(unit_paths(u.text) for u in units))

    def test_paths_are_found_and_checked(self) -> None:
        text = (
            "[Unit]\nConditionPathExists=|%h/.local/lib/cst/x.conf\nAssertPathExists=/etc/y\n"
            "[Service]\nExecStart=/usr/bin/python3 -I %h/.local/lib/cst/tool run\n"
            "ExecStartPost=-/usr/local/sbin/gone\nExecCondition=/usr/local/sbin/data --x\n"
            "Environment=A=/usr/local/sbin/not-a-command\n"
        )
        self.assertEqual(
            unit_paths(text),
            [
                ("ConditionPathExists", "%h/.local/lib/cst/x.conf"),
                ("ExecStart", "%h/.local/lib/cst/tool"),
                ("ExecStartPost", "/usr/local/sbin/gone"),
                ("ExecCondition", "/usr/local/sbin/data"),
            ],
        )
        by_dest = {
            "~/.local/lib/cst/x.conf": InstallEntry(
                "m/x.conf", "~/.local/lib/cst/x.conf", OctalMode(0o644), "user", "data", "user"
            ),
            "~/.local/lib/cst/tool": InstallEntry(
                "m/tool", "~/.local/lib/cst/tool", OctalMode(0o700), "user", "python-entry", "user"
            ),
            "/usr/local/sbin/data": InstallEntry(
                "m/data", "/usr/local/sbin/data", OctalMode(0o644), "root", "data", "root"
            ),
        }
        user = UnitText("u.service", text, "user", "m")
        self.assertEqual(
            contract_problems(user, by_dest),
            [
                "m: u.service: ExecStartPost=/usr/local/sbin/gone: not a file INSTALL_MANIFEST installs",
                "m: u.service: ExecCondition=/usr/local/sbin/data: m/data is of kind data, not a program",
            ],
        )
        # The same program started by the system manager: root is not its owner (user), and others may not run it.
        system = UnitText("s.service", "[Service]\nExecStart=%h/.local/lib/cst/tool\n", "system", "m")
        self.assertEqual(
            contract_problems(system, by_dest),
            ["m: s.service: ExecStart=%h/.local/lib/cst/tool: mode 0700 does not let root run it"],
        )


if __name__ == "__main__":
    unittest.main()
