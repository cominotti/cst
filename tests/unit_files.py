#!/usr/bin/python3 -I
"""Every systemd unit CST installs or generates, as text (for the unit gates).

Use: python3 -I -B tests/unit_files.py write STAGE   (from anywhere)

- `manifest_units()`: the INSTALL_MANIFEST rows of kind unit, the `*.in`
  templates rendered with TEMPLATE_VALUES (the values tier 1's test_11 uses);
- `generated_units()`: the units programs write at run time: cst-vm's
  backup timer and service, cst-toolchains' prune service and timer, the
  LDAC watcher's user unit, and the degraded-boot units (the texts
  btrfs/tests/test_degraded_boot.py checks the setup script writes).

tests/test_unit_contract.py checks the program paths they name;
`write STAGE` puts the generated ones under STAGE/etc/systemd/{system,user}
for tier 1's test_16, which runs systemd-analyze on them next to the
installed manifest units, and prints their names with their scope.

Standard library only.
"""

from dataclasses import dataclass
import os
import sys
from typing import Final, Literal

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bluetooth.domain import LDAC_UNIT, render_unit as bt_render_unit
from btrfs.tests.test_degraded_boot import EXPECTED_RECORD, EXPECTED_SERVICE, EXPECTED_TIMER
from cst_core.domain.install import INSTALL_MANIFEST, manifest_by_src, render_template
from toolchains import domain as tc_domain
from vm import domain as vm_domain

type Scope = Literal["system", "user"]

# The --set values of tier 1's test_11 (UUID, mount options, the user).
TEMPLATE_VALUES: Final[dict[str, str]] = {
    "UUID": "00000000-0000-0000-0000-000000000000",
    "OPTIONS": "subvolid=5",
    "USER": "tester",
}


@dataclass(frozen=True, slots=True)
class UnitText:
    """One unit file: its name (the file name), its text, and which manager runs it."""

    name: str
    text: str
    scope: Scope
    origin: str  # the manifest source, or the program that writes it


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
        out.append(UnitText(os.path.basename(e.dest), text, "user" if e.runner == "user" else "system", e.src))
    return out


def generated_units() -> list[UnitText]:
    """The units CST's programs write at run time, rendered as they write them."""
    vm_program = manifest_by_src()["vm/cst-vm"].dest
    return [
        UnitText(vm_domain.SERVICE, vm_domain.render_service(vm_program, "300min"), "system", "vm/cst-vm"),
        UnitText(vm_domain.TIMER, vm_domain.render_timer(), "system", "vm/cst-vm"),
        UnitText(tc_domain.SERVICE, tc_domain.render_service(), "user", "toolchains/cst-toolchains"),
        UnitText(tc_domain.TIMER, tc_domain.render_timer(), "user", "toolchains/cst-toolchains"),
        UnitText(LDAC_UNIT, bt_render_unit("0A:1B:2C:3D:4E:5F", 48000), "user", "bluetooth/bt-helper"),
        UnitText("btrfs-last-resort@.timer", EXPECTED_TIMER, "system", "btrfs/btrfs-degraded-boot-setup.sh"),
        UnitText("btrfs-last-resort@.service", EXPECTED_SERVICE, "system", "btrfs/btrfs-degraded-boot-setup.sh"),
        UnitText("btrfs-degraded-boot-record.service", EXPECTED_RECORD, "system", "btrfs/btrfs-degraded-boot-setup.sh"),
    ]


def write(stage: str) -> list[UnitText]:
    """The generated units under STAGE/etc/systemd/{system,user}; returns them."""
    units = generated_units()
    for u in units:
        folder = os.path.join(stage, "etc", "systemd", u.scope)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, u.name), "w", encoding="utf-8") as fh:
            fh.write(u.text)
    return units


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "write":
        print("usage: unit_files.py write STAGE", file=sys.stderr)
        return 2
    for u in write(argv[1]):
        print(f"{u.scope} {u.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
