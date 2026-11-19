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
  own code: btrfs-setup.sh's scrub limit (its shell function run in bash),
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

from dataclasses import dataclass
import os
import re
import subprocess
import sys
from typing import Final, Literal

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bluetooth.domain import LDAC_UNIT, render_unit as bt_render_unit
from btrfs.tests.test_degraded_boot import EXPECTED_RECORD, EXPECTED_SERVICE, EXPECTED_TIMER
from btrfs.tests.test_snapshot_runtime import shell_function
from cpu.game import domain as game_domain
from cpu.setup import domain as cpu_domain
from cst_core.domain.install import INSTALL_MANIFEST, manifest_by_src, render_template
from cst_core.domain.text import group
from toolchains.domain import auto as tcd_auto
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


def _shell_value(path: str, name: str) -> str:
    """The value a shell script assigns to NAME on a line of its own (NAME=value, unquoted)."""
    with open(path, encoding="utf-8") as fh:
        found = re.search(rf"^{re.escape(name)}=(\S+)$", fh.read(), re.MULTILINE)
    if found is None:
        raise AssertionError(f"{name}= not found in {path}")
    return group(found, 1)


def scrub_limit_dropin(limit: str) -> str:
    """btrfs-setup.sh's scrub limit drop-in, as its function scrub_limit_dropin prints it."""
    script = (
        f"BTRFS_CONF={_shell_value(BTRFS_SETUP, 'BTRFS_CONF')}\n"
        f"{shell_function(BTRFS_SETUP, 'scrub_limit_dropin')}"
        f'scrub_limit_dropin "$1"\n'
    )
    done = subprocess.run(
        ["bash", "-c", script, "scrub_limit_dropin", limit],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    return done.stdout


def generated_dropins() -> list[UnitText]:
    """The drop-ins CST's programs write, rendered by their own code."""
    chain = (f"user-{GAME_UID}.slice", f"user@{GAME_UID}.service")  # a slice and a service of its memory chain
    return [
        UnitText(
            "10-cst-limit.conf",
            scrub_limit_dropin(SCRUB_LIMIT),
            "system",
            "btrfs-setup.sh",
            _shell_value(BTRFS_SETUP, "SCRUB_SERVICE"),
        ),
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
        folder = os.path.join(stage, "etc", "systemd", u.scope, *([] if u.dropin_of is None else [f"{u.dropin_of}.d"]))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, u.name), "w", encoding="utf-8") as fh:
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
