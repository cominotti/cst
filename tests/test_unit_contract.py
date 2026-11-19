# SPDX-License-Identifier: MPL-2.0
"""Unit contract: every program a unit starts or checks for is an installed CST file that its runner can run.

For every unit CST installs or writes (tests/unit_files.py: the manifest's
units and drop-ins, templates rendered, the units cst-vm, cst-toolchains,
the LDAC watcher and the degraded-boot setup generate, and the drop-ins
btrfs-setup.sh, cpu-setup and the game profile write), each absolute path under
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
import os
import tempfile
import unittest

from cst_core.domain.install import ArtifactKind, InstallEntry, Owner, manifest_by_dest
from cst_core.domain.values import OctalMode
from tests.unit_files import (
    BTRFS_SETUP,
    Scope,
    UnitText,
    generated_dropins,
    generated_units,
    manifest_units,
    scrub_dropin,
)

ROOTS = ("/usr/local/", "%h/.local/")


def unit_paths(text: str) -> list[tuple[str, str]]:
    """(key, path) for every path under ROOTS in an Exec*, Condition* or Assert* setting."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not key.startswith(("Exec", "Condition", "Assert")):
            continue
        for raw in value.split():
            word = raw.lstrip("-@:+!|")
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
        where = f"{unit.origin}: {unit.label}: {key}={path}"
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


# Fedora's file contexts give /usr/(local/)s?bin/docker* the type
# container_runtime_exec_t, whose domain alone may connect to the Docker
# socket. NoNewPrivileges forbids that domain change at exec.
DOCKER_DOMAIN_PREFIX = "/usr/local/sbin/docker"


def nnp_problems(unit: UnitText) -> list[str]:
    """A system unit that starts a docker* program must not set NoNewPrivileges=yes."""
    if unit.scope != "system":
        return []
    starts = [path for key, path in unit_paths(unit.text) if key.startswith("Exec")]
    if not any(path.startswith(DOCKER_DOMAIN_PREFIX) for path in starts):
        return []
    lines = [line.strip().replace(" ", "") for line in unit.text.splitlines()]
    if any(
        line.lower() in ("nonewprivileges=yes", "nonewprivileges=true", "nonewprivileges=1", "nonewprivileges=on")
        for line in lines
    ):
        return [f"{unit.origin}: {unit.label}: NoNewPrivileges=yes blocks the SELinux move into the Docker domain"]
    return []


class UnitContractTest(unittest.TestCase):
    def test_docker_programs_keep_their_selinux_domain(self) -> None:
        problems = [p for unit in manifest_units() for p in nnp_problems(unit)]
        self.assertEqual(problems, [])

    def test_nnp_rule_catches_the_setting_only_where_it_matters(self) -> None:
        def unit(text: str, scope: Scope = "system") -> UnitText:
            return UnitText(origin="t", name="x.service", scope=scope, text=text)

        bad = "[Service]\nExecStart=/usr/local/sbin/docker-gc record\nNoNewPrivileges=yes\n"
        self.assertEqual(len(nnp_problems(unit(bad))), 1)
        self.assertEqual(nnp_problems(unit(bad.replace("yes", "no"))), [])
        self.assertEqual(nnp_problems(unit(bad.replace("docker-gc", "btrfs-notify"))), [])
        self.assertEqual(nnp_problems(unit(bad, scope="user")), [])

    def test_every_unit_names_installed_programs(self) -> None:
        by_dest = manifest_by_dest()
        units = manifest_units() + generated_units() + generated_dropins()
        problems = [p for u in units for p in contract_problems(u, by_dest)]
        self.assertEqual(problems, [], "\n".join(problems))
        # The gate sees the units and drop-ins it is meant for.
        names = {u.label for u in units}
        for name in (
            "btrfs-snapshot.service",
            "cst-vm-backup@.service",
            "cst-bt-ldac48.service",
            "btrfs-scrub-weekly.service.d/10-cst-limit.conf",
            "below.service.d/10-cst.conf",
            "machine.slice.d/10-cpu-setup.conf",
            "user@.service.d/50-cpu-setup-cpuset.conf",
            "machine.slice.d/10-swap-memory-low.conf",
            "user.slice.d/10-cpu-setup.conf",
            "user@1000.service.d/zz-cst-game.conf",
        ):
            self.assertIn(name, names)
        self.assertTrue(any(unit_paths(u.text) for u in units))
        self.assertIn("ExecStartPre=/usr/sbin/btrfs scrub limit -a -l 500M /var\n", generated_dropins()[0].text)

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
                "m/x.conf",
                "~/.local/lib/cst/x.conf",
                OctalMode(0o644),
                Owner.USER,
                ArtifactKind.DATA,
                Owner.USER,
                "m:x",
            ),
            "~/.local/lib/cst/tool": InstallEntry(
                "m/tool",
                "~/.local/lib/cst/tool",
                OctalMode(0o700),
                Owner.USER,
                ArtifactKind.PYTHON_ENTRY,
                Owner.USER,
                "m:x",
            ),
            "/usr/local/sbin/data": InstallEntry(
                "m/data", "/usr/local/sbin/data", OctalMode(0o644), Owner.ROOT, ArtifactKind.DATA, Owner.ROOT, "m:x"
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
        # A drop-in is named with its UNIT.d folder.
        dropin = UnitText("10-x.conf", "[Service]\nExecStartPre=/usr/local/sbin/gone\n", "system", "m", "a.service")
        self.assertEqual(
            contract_problems(dropin, by_dest),
            ["m: a.service.d/10-x.conf: ExecStartPre=/usr/local/sbin/gone: not a file INSTALL_MANIFEST installs"],
        )
        # The same program started by the system manager: root is not its owner (user), and others may not run it.
        system = UnitText("s.service", "[Service]\nExecStart=%h/.local/lib/cst/tool\n", "system", "m")
        self.assertEqual(
            contract_problems(system, by_dest),
            ["m: s.service: ExecStart=%h/.local/lib/cst/tool: mode 0700 does not let root run it"],
        )


class ScrubDropinTest(unittest.TestCase):
    """unit_files reads btrfs-setup.sh's names of the scrub drop-in as bash reads them."""

    def planted(self, *edits: tuple[str, str]) -> str:
        """btrfs-setup.sh with each (old, new) line replaced once, in a temporary folder."""
        with open(BTRFS_SETUP, encoding="utf-8") as f:
            text = f.read()
        for old, new in edits:
            self.assertEqual(text.count(old), 1, old)
            text = text.replace(old, new)
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-scrub-dropin-"))
        path = os.path.join(folder, "btrfs-setup.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_the_drop_in_name_comes_from_scrub_dropin(self) -> None:
        dropin = scrub_dropin(self.planted(('.d/10-cst-limit.conf"\n', '.d/20-cst-limit.conf"\n')))
        self.assertEqual(dropin.label, "btrfs-scrub-weekly.service.d/20-cst-limit.conf")
        self.assertIn("ExecStartPre=/usr/sbin/btrfs scrub limit -a -l 500M /var\n", dropin.text)

    def test_quoted_values_are_read_as_bash_reads_them(self) -> None:
        path = self.planted(
            ("\nSCRUB_SERVICE=btrfs-scrub-weekly.service\n", '\nSCRUB_SERVICE="other-scrub.service"\n'),
            ("\nBTRFS_CONF=/etc/btrfs-setup/snapshots.conf\n", "\nBTRFS_CONF='/etc/other/snapshots.conf'\n"),
        )
        dropin = scrub_dropin(path)
        self.assertEqual((dropin.name, dropin.dropin_of), ("10-cst-limit.conf", "other-scrub.service"))
        self.assertIn("from SCRUB_LIMIT in /etc/other/snapshots.conf.\n", dropin.text)

    def test_a_trailing_comment_is_no_part_of_the_value(self) -> None:
        path = self.planted(
            ("\nSCRUB_SERVICE=btrfs-scrub-weekly.service\n", "\nSCRUB_SERVICE=btrfs-scrub-weekly.service  # weekly\n")
        )
        self.assertEqual(scrub_dropin(path).label, "btrfs-scrub-weekly.service.d/10-cst-limit.conf")

    def test_the_checkouts_script_gives_the_weekly_scrubs_drop_in(self) -> None:
        dropin = scrub_dropin()
        self.assertEqual(dropin.label, "btrfs-scrub-weekly.service.d/10-cst-limit.conf")
        self.assertIn("from SCRUB_LIMIT in /etc/btrfs-setup/snapshots.conf.\n", dropin.text)

    def test_a_name_the_script_no_longer_assigns_is_a_loud_error(self) -> None:
        path = self.planted(("\nSCRUB_SERVICE=btrfs-scrub-weekly.service\n", "\n"))
        with self.assertRaisesRegex(AssertionError, "SCRUB_SERVICE"):
            scrub_dropin(path)


if __name__ == "__main__":
    unittest.main()
