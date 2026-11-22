#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Every systemd unit CST installs or generates, as text (for the unit gates).

Use: python3 -I -B tests/unit_files.py write STAGE   (from anywhere)

- `manifest_units()`: the INSTALL_MANIFEST rows of kind unit, the `*.in`
  templates rendered with TEMPLATE_VALUES (the values tier 1's test_11 uses);
- `generated_units()`: the units programs write at run time: cst-vm's
  backup timer and service, cst-toolchains' prune service and timer, the
  LDAC watcher's user unit, and the degraded-boot units (the texts
  btrfs/tests/test_degraded_boot.py checks the setup script writes);
- `generated_dropins()`: the drop-ins programs write, rendered by their
  own code: btrfs-setup.sh's scrub limit (its shell function run in bash,
  at the path its SCRUB_DROPIN names; its values read as bash reads them),
  cpu-setup's toolbox and session-helper drop-ins, and the game profile's
  MemoryLow drop-ins. A drop-in's UnitText names the unit it extends
  (dropin_of); the manifest's drop-in rows get it too.

tests/test_unit_contract.py checks the program paths they name;
`write STAGE` puts the generated ones under STAGE/etc/systemd/{system,user}
(a drop-in in its UNIT.d folder) for tier 1's test_16, which runs
systemd-analyze on them next to the installed manifest units, and prints
their names with their scope.

Standard library only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import os
import subprocess
import sys
from typing import Final, Literal

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bluetooth.domain import LDAC_UNIT, render_unit as bt_render_unit
from btrfs.tests.test_degraded_boot import EXPECTED_RECORD, EXPECTED_SERVICE, EXPECTED_TIMER
from cpu.game import domain as game_domain
from cpu.irq import domain as irq_domain
from cpu.setup import domain as cpu_domain
from cst_core.domain.install import INSTALL_MANIFEST, manifest_by_src, render_template
from cst_core.domain.text import file_lines, parse_shell_assignments
from toolchains.domain import auto as tcd_auto
from tests.shell_source import shell_function
from toolchains.domain import common as tcd_common
from vm import domain as vm_domain

type Scope = Literal["system", "user"]

# The --set values of tier 1's test_11 (UUID, mount options, the user).
TEMPLATE_VALUES: Final[dict[str, str]] = {
    "UUID": "00000000-0000-0000-0000-000000000000",
    "OPTIONS": "subvolid=5",
    "USER": "tester",
    "ALLOWED": "AllowedCPUs=0",
}

# What the generated drop-ins are rendered with: cpu-setup's E-cores, a SCRUB_LIMIT, the game
# profile's GAME_MEMORY_LOW (cpu-setup.conf's default) and a user's uid for its memory chain.
E_CORES: Final = "8-23"
SCRUB_LIMIT: Final = "500M"
GAME_MEMORY_LOW: Final = "16G"
GAME_UID: Final = 1000
BTRFS_SETUP: Final = os.path.join(REPO, "btrfs-setup.sh")


@dataclass(frozen=True, slots=True)
class UnitText:
    """One unit file: its name (the file name), its text, and which manager runs it."""

    name: str
    text: str
    scope: Scope
    origin: str  # the manifest source, or the program that writes it
    dropin_of: str | None = None  # for a drop-in: the unit it extends (the UNIT of its UNIT.d folder)

    @property
    def label(self) -> str:
        """The name, with its UNIT.d folder for a drop-in."""
        return self.name if self.dropin_of is None else f"{self.dropin_of}.d/{self.name}"


def manifest_units() -> list[UnitText]:
    """The unit rows of INSTALL_MANIFEST, templates rendered."""
    out: list[UnitText] = []
    for e in INSTALL_MANIFEST:
        if e.kind != "unit":
            continue
        with open(os.path.join(REPO, e.src), encoding="utf-8") as fh:
            text = fh.read()
        if e.src.endswith(".in"):
            text = render_template(text, TEMPLATE_VALUES)
        folder = os.path.basename(os.path.dirname(e.dest))
        dropin_of = folder.removesuffix(".d") if folder.endswith(".d") else None
        scope: Scope = "user" if e.runner == "user" else "system"
        out.append(UnitText(os.path.basename(e.dest), text, scope, e.src, dropin_of))
    return out


def generated_units() -> list[UnitText]:
    """The units CST's programs write at run time, rendered as they write them."""
    vm_program = manifest_by_src()["vm/cst-vm"].dest
    return [
        UnitText(vm_domain.SERVICE, vm_domain.render_service(vm_program, "300min"), "system", "vm/cst-vm"),
        UnitText(vm_domain.TIMER, vm_domain.render_timer(), "system", "vm/cst-vm"),
        UnitText(tcd_common.SERVICE, tcd_auto.render_service(), "user", "toolchains/cst-toolchains"),
        UnitText(tcd_common.TIMER, tcd_auto.render_timer(), "user", "toolchains/cst-toolchains"),
        UnitText(LDAC_UNIT, bt_render_unit("0A:1B:2C:3D:4E:5F", 48000), "user", "bluetooth/bt-helper"),
        UnitText("btrfs-last-resort@.timer", EXPECTED_TIMER, "system", "btrfs/btrfs-degraded-boot-setup.sh"),
        UnitText("btrfs-last-resort@.service", EXPECTED_SERVICE, "system", "btrfs/btrfs-degraded-boot-setup.sh"),
        UnitText("btrfs-degraded-boot-record.service", EXPECTED_RECORD, "system", "btrfs/btrfs-degraded-boot-setup.sh"),
    ]


def _setup_value(text: str, path: str, name: str) -> str:
    """The value the setup script at PATH (its TEXT) assigns to NAME, read as bash reads it
    (parse_shell_assignments: quotes, a trailing # comment); it must be assigned there exactly once."""
    found = [a.value for a in parse_shell_assignments(text).entries if a.key == name]
    if len(found) != 1:
        raise AssertionError(f"{path}: {name}= is assigned {len(found)} times as a plain value, not once")
    return found[0]


def _dropin_path(text: str, path: str, values: Mapping[str, str]) -> str:
    """The value of the script's own SCRUB_DROPIN= line, which names other variables: bash expands it
    with VALUES in its environment."""
    lines = [line for line in file_lines(text) if line.startswith("SCRUB_DROPIN=")]
    if len(lines) != 1:
        raise AssertionError(f"{path}: SCRUB_DROPIN= is assigned on {len(lines)} lines, not one")
    done = subprocess.run(
        ["bash", "-c", f'{lines[0]}\nprintf "%s" "$SCRUB_DROPIN"\n'],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", **values},
        timeout=60,
    )
    return done.stdout


def scrub_limit_dropin(limit: str, setup: str = BTRFS_SETUP) -> str:
    """btrfs-setup.sh's scrub limit drop-in, as its function scrub_limit_dropin prints it."""
    with open(setup, encoding="utf-8") as fh:
        text = fh.read()
    script = f'{shell_function(setup, "scrub_limit_dropin")}scrub_limit_dropin "$1"\n'
    done = subprocess.run(
        ["bash", "-c", script, "scrub_limit_dropin", limit],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "BTRFS_CONF": _setup_value(text, setup, "BTRFS_CONF")},
        timeout=60,
    )
    return done.stdout


def scrub_dropin(setup: str = BTRFS_SETUP) -> UnitText:
    """btrfs-setup.sh's scrub limit drop-in, for SCRUB_LIMIT, at the path its SCRUB_DROPIN names
    (the file name, and the unit of its UNIT.d folder under UNIT_DIR)."""
    with open(setup, encoding="utf-8") as fh:
        text = fh.read()
    values = {name: _setup_value(text, setup, name) for name in ("UNIT_DIR", "SCRUB_SERVICE")}
    dropin = _dropin_path(text, setup, values)
    prefix = values["UNIT_DIR"] + "/"
    folder, sep, name = dropin.removeprefix(prefix).partition("/")
    if not dropin.startswith(prefix) or not sep or "/" in name or not folder.endswith(".d"):
        raise AssertionError(f"{setup}: SCRUB_DROPIN={dropin} is not UNIT_DIR/UNIT.d/NAME")
    return UnitText(name, scrub_limit_dropin(SCRUB_LIMIT, setup), "system", "btrfs-setup.sh", folder.removesuffix(".d"))


def generated_dropins() -> list[UnitText]:
    """The drop-ins CST's programs write, rendered by their own code."""
    chain = (f"user-{GAME_UID}.slice", f"user@{GAME_UID}.service")  # a slice and a service of its memory chain
    return [
        scrub_dropin(),
        UnitText(
            cpu_domain.DROPIN_NAME, cpu_domain.toolbox_dropin_text(E_CORES), "user", "cpu/cpu-setup", "user.slice"
        ),
        UnitText(
            cpu_domain.DROPIN_NAME,
            cpu_domain.helper_dropin_text(E_CORES),
            "user",
            "cpu/cpu-setup",
            "flatpak-session-helper.service",
        ),
        UnitText(
            "10-cst-ban.conf",
            irq_domain.render_irq_dropin(),
            "system",
            "cpu/cpu-irq",
            irq_domain.IRQBALANCE_SERVICE,
        ),
        *(
            UnitText(
                game_domain.GAME_DROPIN,
                game_domain.dropin_text(unit, GAME_MEMORY_LOW),
                "system",
                "cpu/game-profile",
                unit,
            )
            for unit in chain
        ),
    ]


def write(stage: str) -> list[UnitText]:
    """The generated units and drop-ins under STAGE/etc/systemd/{system,user}; returns them."""
    units = generated_units() + generated_dropins()
    for u in units:
        path = os.path.join(stage, "etc", "systemd", u.scope, u.label)  # a drop-in in its UNIT.d folder
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(u.text)
    return units


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "write":
        print("usage: unit_files.py write STAGE", file=sys.stderr)
        return 2
    for u in write(argv[1]):
        print(f"{u.scope} {u.label}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
