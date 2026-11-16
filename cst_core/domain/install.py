"""Declarative installation manifest and executable inventory for CST."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from cst_core.domain.values import OctalMode

type Owner = Literal["root", "user"]
type ArtifactKind = Literal["python-entry", "shell", "unit", "data"]


@dataclass(frozen=True, slots=True)
class InstallEntry:
    """One repository artifact installed onto a host or user account."""

    src: str
    dest: str
    mode: OctalMode
    owner: Owner
    kind: ArtifactKind
    runner: Owner


INSTALL_MANIFEST: Final[tuple[InstallEntry, ...]] = (
    # ---- btrfs: Python helpers (self-contained zipapps) ----
    InstallEntry(
        src="btrfs/btrfs-bulk",
        dest="/usr/local/sbin/btrfs-bulk",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/btrfs-snapshot-core",
        dest="/usr/local/sbin/btrfs-snapshot-core",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/btrfs-stall-monitord",
        dest="/usr/local/sbin/btrfs-stall-monitord",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/btrfs-stall-report",
        dest="/usr/local/bin/btrfs-stall-report",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="user",
    ),
    InstallEntry(
        src="btrfs/docker-gc",
        dest="/usr/local/sbin/docker-gc",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/docker-volumes-nocow",
        dest="/usr/local/sbin/docker-volumes-nocow",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
    # ---- btrfs: standalone shell helpers ----
    InstallEntry(
        src="btrfs/btrfs-health-check",
        dest="/usr/local/sbin/btrfs-health-check",
        mode=OctalMode(0o755),
        owner="root",
        kind="shell",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/btrfs-notify",
        dest="/usr/local/sbin/btrfs-notify",
        mode=OctalMode(0o755),
        owner="root",
        kind="shell",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/btrfs-snapshot",
        dest="/usr/local/sbin/btrfs-snapshot",
        mode=OctalMode(0o755),
        owner="root",
        kind="shell",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/cst-pending-alerts",
        dest="/usr/local/bin/cst-pending-alerts",
        mode=OctalMode(0o755),
        owner="root",
        kind="shell",
        runner="user",
    ),
    # ---- btrfs: systemd units and data ----
    InstallEntry(
        src="btrfs/units/below.service.d/10-cst.conf",
        dest="/etc/systemd/system/below.service.d/10-cst.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-bulk-boot.service",
        dest="/etc/systemd/system/btrfs-bulk-boot.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-health-check.service",
        dest="/etc/systemd/system/btrfs-health-check.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-health-check.timer",
        dest="/etc/systemd/system/btrfs-health-check.timer",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-scrub-weekly.service",
        dest="/etc/systemd/system/btrfs-scrub-weekly.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-scrub-weekly.timer",
        dest="/etc/systemd/system/btrfs-scrub-weekly.timer",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-snapshot-nas.service",
        dest="/etc/systemd/system/btrfs-snapshot-nas.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-snapshot-nas.timer",
        dest="/etc/systemd/system/btrfs-snapshot-nas.timer",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-snapshot.service",
        dest="/etc/systemd/system/btrfs-snapshot.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-snapshot.timer",
        dest="/etc/systemd/system/btrfs-snapshot.timer",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-stall-alert.service",
        dest="/etc/systemd/system/btrfs-stall-alert.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-stall-monitor.service",
        dest="/etc/systemd/system/btrfs-stall-monitor.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/btrfs-subvol-convert.service",
        dest="/etc/systemd/system/btrfs-subvol-convert.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/docker-gc-recorder.service",
        dest="/etc/systemd/system/docker-gc-recorder.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/docker-gc.service",
        dest="/etc/systemd/system/docker-gc.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/docker-gc.timer",
        dest="/etc/systemd/system/docker-gc.timer",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/docker-volumes-nocow.service",
        dest="/etc/systemd/system/docker-volumes-nocow.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/units/user/cst-pending-alerts.service",
        dest="/etc/systemd/user/cst-pending-alerts.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="user",
    ),
    InstallEntry(
        src="btrfs/sysctl/90-btrfs-writeback.conf",
        dest="/etc/sysctl.d/90-btrfs-writeback.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="data",
        runner="root",
    ),
    InstallEntry(
        src="btrfs/tmpfiles/btrfs-automatic-reclaim.conf.in",
        dest="/etc/tmpfiles.d/btrfs-automatic-reclaim.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="data",
        runner="root",
    ),
    # ---- cpu ----
    InstallEntry(
        src="cpu/vm-pin",
        dest="/usr/local/sbin/cpu-vm-pin",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
    InstallEntry(
        src="cpu/cpu-watch",
        dest="/usr/local/bin/cpu-watch",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="user",
    ),
    InstallEntry(
        src="cpu/game-profile",
        dest="/usr/local/sbin/cpu-game-profile",
        mode=OctalMode(0o755),
        owner="root",
        kind="shell",
        runner="root",
    ),
    InstallEntry(
        src="cpu/units/builds.slice",
        dest="/etc/systemd/system/builds.slice",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="cpu/units/cpu-game.service",
        dest="/etc/systemd/system/cpu-game.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="cpu/units/cpu-game-watchdog.service",
        dest="/etc/systemd/system/cpu-game-watchdog.service",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="cpu/units/cpu-game-watchdog.timer",
        dest="/etc/systemd/system/cpu-game-watchdog.timer",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="cpu/units/machine.slice.d-10-cpu-setup.conf",
        dest="/etc/systemd/system/machine.slice.d/10-cpu-setup.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="cpu/units/user@.service.d-50-cpu-setup-cpuset.conf",
        dest="/etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    InstallEntry(
        src="cpu/gamemode.ini",
        dest="~/.config/gamemode.ini",
        mode=OctalMode(0o644),
        owner="user",
        kind="data",
        runner="user",
    ),
    InstallEntry(
        src="cpu/cpu-game.rules.in",
        dest="/etc/polkit-1/rules.d/50-cpu-setup-game.rules",
        mode=OctalMode(0o644),
        owner="root",
        kind="data",
        runner="root",
    ),
    # ---- bluetooth ----
    InstallEntry(
        src="bluetooth/bt-ldac-watch",
        dest="~/.local/lib/cst/bt-ldac-watch",
        mode=OctalMode(0o755),
        owner="user",
        kind="python-entry",
        runner="user",
    ),
    # ---- swap ----
    InstallEntry(
        src="swap/sysctl/99-swap-tuning.conf",
        dest="/etc/sysctl.d/99-swap-tuning.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="data",
        runner="root",
    ),
    InstallEntry(
        src="swap/tmpfiles/zswap.conf",
        dest="/etc/tmpfiles.d/zswap.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="data",
        runner="root",
    ),
    InstallEntry(
        src="swap/units/machine.slice.d-50-memory-low.conf",
        dest="/etc/systemd/system/machine.slice.d/50-memory-low.conf",
        mode=OctalMode(0o644),
        owner="root",
        kind="unit",
        runner="root",
    ),
    # ---- toolchains ----
    InstallEntry(
        src="toolchains/cst-toolchains",
        dest="~/.local/lib/cst/cst-toolchains",
        mode=OctalMode(0o755),
        owner="user",
        kind="python-entry",
        runner="user",
    ),
    InstallEntry(
        src="toolchains/rustc-wrapper",
        dest="~/.local/libexec/cst/rustc-wrapper",
        mode=OctalMode(0o755),
        owner="user",
        kind="shell",
        runner="user",
    ),
    InstallEntry(
        src="toolchains/toolchains.conf",
        dest="~/.local/lib/cst/toolchains.conf",
        mode=OctalMode(0o644),
        owner="user",
        kind="data",
        runner="user",
    ),
    # ---- vm ----
    InstallEntry(
        src="vm/cst-vm",
        dest="/usr/local/sbin/cst-vm",
        mode=OctalMode(0o755),
        owner="root",
        kind="python-entry",
        runner="root",
    ),
)

SETUP_ENTRYPOINTS: Final[frozenset[str]] = frozenset(
    {
        "cst",
        "btrfs-setup.sh",
        "btrfs/btrfs-degraded-boot-setup.sh",
        "cpu-setup.sh",
        "bluetooth-setup.sh",
        "swap-setup.sh",
        "toolchains-setup.sh",
        "vm-setup.sh",
    }
)

CHECKOUT_ONLY_EXECUTABLES: Final[frozenset[str]] = frozenset(
    {
        ".github/scripts/pin_invowk.py",
        "bluetooth/bt-helper",
        "btrfs/btrfs-stall-watch",
        "btrfs/chrome-cache",
        "btrfs/tests/realbtrfs-snapshot-core.sh",
        "cst_core/cli.py",
        "doctor/cst-doctor",
        "fix-anaconda-webui-btrfs-compress.sh",
        "swap/cli.py",
        "tests/mutation/cst-mutation",
        "tests/uv-dev",
    }
)


def manifest_by_src() -> Mapping[str, InstallEntry]:
    """Return mapping of repo-relative source paths to manifest entries."""
    return {entry.src: entry for entry in INSTALL_MANIFEST}


def manifest_by_dest() -> Mapping[str, InstallEntry]:
    """Return mapping of installed destination paths to manifest entries."""
    return {entry.dest: entry for entry in INSTALL_MANIFEST}


def python_entry_srcs() -> tuple[InstallEntry, ...]:
    """Return all Python helper entries packaged as standalone zipapps."""
    return tuple(entry for entry in INSTALL_MANIFEST if entry.kind == "python-entry")
