# SPDX-License-Identifier: MPL-2.0
"""Declarative installation manifest and executable inventory for CST."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.values import DomainValidationError, OctalMode


class Owner(StrEnum):
    """Whose destination a file is (the manifest's owner and runner columns)."""

    ROOT = "root"
    USER = "user"


# The width the installer pads its report word to: that of `unchanged:`, so the paths line up.
_REPORT_WORD_WIDTH: Final = 10


class InstallReportWord(StrEnum):
    """The first word of the line the installer prints for each file: an install's `written:` or
    `unchanged:`, a removal's `removed:`, a plan's `would write:`, `would record:` or `unchanged:`.
    The programs that read the installer's output (bluetooth, cpu irq, scrub-limit, the settings steps)
    match these words."""

    UNCHANGED = "unchanged:"
    WRITTEN = "written:"
    REMOVED = "removed:"
    WOULD_WRITE = "would write:"
    WOULD_RECORD = "would record:"

    def line(self, path: str) -> str:
        """The installer's line for path: this word padded to the width of `unchanged:`, a space, path."""
        return f"{self.value:<{_REPORT_WORD_WIDTH}} {path}"


class ArtifactKind(StrEnum):
    """What an installed file is (the manifest's kind column)."""

    PYTHON_ENTRY = "python-entry"
    SHELL = "shell"
    UNIT = "unit"
    DATA = "data"


class GeneratedKind(StrEnum):
    """The kind an install record lists for a file CST generates (no manifest source): what
    Placement.kind holds besides an ArtifactKind. Not a manifest kind."""

    TEXT = "text"


class Component(StrEnum):
    """The parts of the modules that one command installs as a whole (COMPONENTS: that command), as the
    manifest's component column, the install record and the setup scripts name them (MODULE:PART)."""

    BTRFS_CORE = "btrfs:core"
    BTRFS_ALERTS = "btrfs:alerts"
    BTRFS_BELOW = "btrfs:below"
    BTRFS_SNAPSHOTS = "btrfs:snapshots"
    BTRFS_TOP_MOUNT = "btrfs:top-mount"
    BTRFS_DOCKER_GC = "btrfs:docker-gc"
    BTRFS_DEGRADED_BOOT = "btrfs:degraded-boot"
    BTRFS_IOCOST = "btrfs:iocost"
    BTRFS_REPORT_LAUNCHER = "btrfs:report-launcher"
    CPU_CORE = "cpu:core"
    CPU_GAMEMODE = "cpu:gamemode"
    CPU_DOCKER_SLICE = "cpu:docker-slice"
    CPU_IRQ = "cpu:irq"
    CPU_TOOLBOX = "cpu:toolbox"
    BLUETOOTH_LDAC = "bluetooth:ldac"
    SWAP_CORE = "swap:core"
    TOOLCHAINS_AUTO = "toolchains:auto"
    TOOLCHAINS_SCCACHE = "toolchains:sccache"
    VM_BACKUP_TIMER = "vm:backup-timer"


@dataclass(frozen=True, slots=True)
class InstallEntry:
    """One repository artifact installed onto a host or user account."""

    src: str
    dest: str
    mode: OctalMode
    owner: Owner
    kind: ArtifactKind
    runner: Owner
    # The part of its module this file belongs to (COMPONENTS: the command that installs it).
    component: str

    @property
    def module(self) -> str:
        """The CST module (top-level folder) this file belongs to."""
        return self.src.split("/", 1)[0]

    @property
    def is_template(self) -> bool:
        """True for `*.in` sources: installed with every @KEY@ replaced."""
        return self.src.endswith(".in")


_OWNERS: Final[Mapping[str, Owner]] = {o.value: o for o in Owner}
_KINDS: Final[Mapping[str, ArtifactKind]] = {k.value: k for k in ArtifactKind}


def parse_manifest(table: str) -> tuple[InstallEntry, ...]:
    """The entries of a manifest table, in order.

    One row per file: `src dest mode owner kind runner component`, separated
    by blanks; `mode` is octal; `#` starts a comment. A malformed row raises
    ValueError.
    """
    entries: list[InstallEntry] = []
    for number, raw in enumerate(table.splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        try:
            src, dest, mode, owner, kind, runner, component = fields
            entries.append(
                InstallEntry(
                    src, dest, OctalMode(int(mode, 8)), _OWNERS[owner], _KINDS[kind], _OWNERS[runner], component
                )
            )
        except (ValueError, KeyError) as exc:
            raise ValueError(f"manifest row {number}: {raw.strip()!r}: {exc}") from None
    return tuple(entries)


# What `install` writes: every installable file of the repository, once.
# runner: the account that runs or reads the installed file; component: the
# part of the module it belongs to, installed and removed as a whole by one
# command (COMPONENTS).
INSTALL_MANIFEST: Final[tuple[InstallEntry, ...]] = parse_manifest(
    r"""
# src                                               dest                                                          mode owner kind          runner  component
# btrfs: Python helpers (self-contained zipapps)
btrfs/btrfs-bulk                                    /usr/local/sbin/btrfs-bulk                                    755  root  python-entry  root   btrfs:snapshots
btrfs/btrfs-health-check                            /usr/local/sbin/btrfs-health-check                            755  root  python-entry  root   btrfs:core
btrfs/btrfs-snapshot-core                           /usr/local/sbin/btrfs-snapshot-core                           755  root  python-entry  root   btrfs:snapshots
btrfs/btrfs-snapshot-runtime                        /usr/local/sbin/btrfs-snapshot-runtime                        755  root  python-entry  root   btrfs:snapshots
btrfs/btrfs-stall-monitord                          /usr/local/sbin/btrfs-stall-monitord                          755  root  python-entry  root   btrfs:core
btrfs/btrfs-stall-report                            /usr/local/bin/btrfs-stall-report                             755  root  python-entry  user   btrfs:core
btrfs/cst-pending-alerts                            /usr/local/bin/cst-pending-alerts                             755  root  python-entry  user   btrfs:alerts
btrfs/docker-gc                                     /usr/local/sbin/docker-gc                                     755  root  python-entry  root   btrfs:docker-gc
btrfs/docker-volumes-nocow                          /usr/local/sbin/docker-volumes-nocow                          755  root  python-entry  root   btrfs:core
# btrfs: standalone shell helpers
btrfs/btrfs-notify                                  /usr/local/sbin/btrfs-notify                                  755  root  shell         root   btrfs:alerts
btrfs/btrfs-snapshot                                /usr/local/sbin/btrfs-snapshot                                755  root  shell         root   btrfs:snapshots
# btrfs: systemd units and data
btrfs/units/below.service.d/10-cst.conf             /etc/systemd/system/below.service.d/10-cst.conf               644  root  unit          root   btrfs:below
btrfs/units/journald.conf.d/10-cst-write-budget.conf /etc/systemd/journald.conf.d/10-cst-write-budget.conf        644  root  data          root   btrfs:core
btrfs/units/btrfs-bulk-boot.service                 /etc/systemd/system/btrfs-bulk-boot.service                   644  root  unit          root   btrfs:snapshots
btrfs/units/btrfs-health-check.service              /etc/systemd/system/btrfs-health-check.service                644  root  unit          root   btrfs:core
btrfs/units/btrfs-health-check.timer                /etc/systemd/system/btrfs-health-check.timer                  644  root  unit          root   btrfs:core
btrfs/units/btrfs-scrub-weekly.service              /etc/systemd/system/btrfs-scrub-weekly.service                644  root  unit          root   btrfs:core
btrfs/units/btrfs-scrub-weekly.timer                /etc/systemd/system/btrfs-scrub-weekly.timer                  644  root  unit          root   btrfs:core
btrfs/units/btrfs-snapshot-nas.service              /etc/systemd/system/btrfs-snapshot-nas.service                644  root  unit          root   btrfs:snapshots
btrfs/units/btrfs-snapshot-nas.timer                /etc/systemd/system/btrfs-snapshot-nas.timer                  644  root  unit          root   btrfs:snapshots
btrfs/units/btrfs-snapshot.service                  /etc/systemd/system/btrfs-snapshot.service                    644  root  unit          root   btrfs:snapshots
btrfs/units/btrfs-snapshot.timer                    /etc/systemd/system/btrfs-snapshot.timer                      644  root  unit          root   btrfs:snapshots
btrfs/units/btrfs-stall-alert.service               /etc/systemd/system/btrfs-stall-alert.service                 644  root  unit          root   btrfs:core
btrfs/units/btrfs-stall-monitor.service             /etc/systemd/system/btrfs-stall-monitor.service               644  root  unit          root   btrfs:core
btrfs/units/btrfs-subvol-convert.service            /etc/systemd/system/btrfs-subvol-convert.service              644  root  unit          root   btrfs:snapshots
btrfs/units/docker-gc-recorder.service              /etc/systemd/system/docker-gc-recorder.service                644  root  unit          root   btrfs:docker-gc
btrfs/units/docker-gc.service                       /etc/systemd/system/docker-gc.service                         644  root  unit          root   btrfs:docker-gc
btrfs/units/docker-gc.timer                         /etc/systemd/system/docker-gc.timer                           644  root  unit          root   btrfs:docker-gc
btrfs/units/docker-volumes-nocow.service            /etc/systemd/system/docker-volumes-nocow.service              644  root  unit          root   btrfs:core
btrfs/units/user/cst-pending-alerts.service         /etc/systemd/user/cst-pending-alerts.service                  644  root  unit          user   btrfs:alerts
btrfs/snapshots/top.mount.in                        /etc/systemd/system/var-mnt-btrfs\x2dtop.mount                644  root  unit          root   btrfs:top-mount
btrfs/snapshots/top.automount.in                    /etc/systemd/system/var-mnt-btrfs\x2dtop.automount            644  root  unit          root   btrfs:top-mount
btrfs/sysctl/90-btrfs-writeback.conf                /etc/sysctl.d/90-btrfs-writeback.conf                         644  root  data          root   btrfs:core
btrfs/tmpfiles/btrfs-automatic-reclaim.conf.in      /etc/tmpfiles.d/btrfs-automatic-reclaim.conf                  644  root  data          root   btrfs:core
btrfs/tmpfiles/cst-nvme-apst.conf                   /etc/tmpfiles.d/cst-nvme-apst.conf                            644  root  data          root   btrfs:core
btrfs/udev/65-cst-nvme-apst.rules                   /etc/udev/rules.d/65-cst-nvme-apst.rules                      644  root  data          root   btrfs:core
btrfs/btrfs-iocost                                  /usr/local/sbin/btrfs-iocost                                  755  root  python-entry  root   btrfs:iocost
btrfs/units/cst-btrfs-iocost.service                /etc/systemd/system/cst-btrfs-iocost.service                  644  root  unit          root   btrfs:iocost
btrfs/udev/91-cst-iocost.rules                      /etc/udev/rules.d/91-cst-iocost.rules                         644  root  data          root   btrfs:iocost
# cpu
cpu/vm-pin                                          /usr/local/sbin/cpu-vm-pin                                    755  root  python-entry  root   cpu:core
cpu/cpu-irq                                         /usr/local/sbin/cpu-irq                                       755  root  python-entry  root   cpu:core
cpu/cpu-power                                       /usr/local/sbin/cpu-power                                     755  root  python-entry  root   cpu:core
cpu/cpu-watch                                       /usr/local/bin/cpu-watch                                      755  root  python-entry  user   cpu:core
cpu/game-profile                                    /usr/local/sbin/cpu-game-profile                              755  root  python-entry  root   cpu:core
cpu/units/builds.slice                              /etc/systemd/system/builds.slice                              644  root  unit          root   cpu:docker-slice
cpu/units/cpu-game.service                          /etc/systemd/system/cpu-game.service                          644  root  unit          root   cpu:core
cpu/units/cpu-game-watchdog.service                 /etc/systemd/system/cpu-game-watchdog.service                 644  root  unit          root   cpu:core
cpu/units/cpu-game-watchdog.timer                   /etc/systemd/system/cpu-game-watchdog.timer                   644  root  unit          root   cpu:core
cpu/units/cst-cpu-power.service                     /etc/systemd/system/cst-cpu-power.service                     644  root  unit          root   cpu:core
cpu/units/machine.slice.d-10-cpu-setup.conf         /etc/systemd/system/machine.slice.d/10-cpu-setup.conf         644  root  unit          root   cpu:core
cpu/units/user@.service.d-50-cpu-setup-cpuset.conf  /etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf  644  root  unit          root   cpu:core
cpu/gamemode.ini                                    ~/.config/gamemode.ini                                        644  user  data          user   cpu:gamemode
cpu/cpu-game.rules.in                               /etc/polkit-1/rules.d/50-cpu-setup-game.rules                 644  root  data          root   cpu:core
cpu/udev/65-cst-gpu-pm.rules                        /etc/udev/rules.d/65-cst-gpu-pm.rules                         644  root  data          root   cpu:core
# bluetooth
bluetooth/bt-ldac-watch                             ~/.local/lib/cst/bt-ldac-watch                                755  user  python-entry  user   bluetooth:ldac
# swap
swap/sysctl/99-swap-tuning.conf                     /etc/sysctl.d/99-swap-tuning.conf                             644  root  data          root   swap:core
swap/tmpfiles/cst-mm-responsiveness.conf           /etc/tmpfiles.d/cst-mm-responsiveness.conf                   644  root  data          root   swap:core
swap/tmpfiles/zswap.conf                            /etc/tmpfiles.d/zswap.conf                                    644  root  data          root   swap:core
swap/units/machine.slice.d-10-swap-memory-low.conf  /etc/systemd/system/machine.slice.d/10-swap-memory-low.conf   644  root  unit          root   swap:core
swap/units/uresourced.conf                          /etc/uresourced.conf                                          644  root  data          root   swap:core
# toolchains
toolchains/cst-toolchains                           ~/.local/lib/cst/cst-toolchains                               755  user  python-entry  user   toolchains:auto
toolchains/rustc-wrapper                            ~/.local/libexec/cst/rustc-wrapper                            755  user  shell         user   toolchains:sccache
toolchains/toolchains.conf                          ~/.local/lib/cst/toolchains.conf                              644  user  data          user   toolchains:auto
# vm
vm/cst-vm                                           /usr/local/sbin/cst-vm                                        755  root  python-entry  root   vm:backup-timer
"""
)


# The components of the manifest (Component): each is a part of one module whose files one
# command installs (and refreshes) together, in one installer call; the value is that
# command, as a user types it. Written by word, read by member (COMPONENTS).
_COMMANDS: Final = {
    "btrfs:core": "cst btrfs install",
    # btrfs-notify and the login-time reader of the alerts it keeps: `cst btrfs install` always installs
    # them; `snapshots setup` (which alerts through them too) installs them as well.
    "btrfs:alerts": "cst btrfs install",
    "btrfs:below": "cst btrfs install",
    "btrfs:snapshots": "cst btrfs snapshots setup",
    "btrfs:top-mount": "cst btrfs snapshots setup",
    "btrfs:docker-gc": "cst btrfs docker-gc enable",
    "btrfs:degraded-boot": "cst btrfs degraded-boot install",
    "btrfs:iocost": "cst btrfs iocost apply",
    "btrfs:report-launcher": "cst btrfs install",
    "cpu:core": "cst cpu install",
    "cpu:gamemode": "cst cpu install",
    "cpu:docker-slice": "cst cpu docker-slice install",
    "cpu:irq": "cst cpu irq apply",
    "cpu:toolbox": "cst cpu install",
    "bluetooth:ldac": "cst bluetooth apply",
    "swap:core": "cst swap apply",
    "toolchains:auto": "cst toolchains auto on",
    "toolchains:sccache": "cst toolchains config apply",
    "vm:backup-timer": "cst vm backup-timer on",
}
# One row of _COMMANDS per member (a member without one is a KeyError at import; tests/test_install.py
# checks that no row names a word Component does not have).
COMPONENTS: Final[Mapping[Component, str]] = {c: _COMMANDS[c] for c in Component}


# The components whose files are all generated text (cst_install_text DEST MODE OWNER COMPONENT), so
# no manifest entry names their owner: each with its owner (whose install record lists them).
GENERATED_COMPONENTS: Final[Mapping[Component, Owner]] = {
    Component.BTRFS_DEGRADED_BOOT: Owner.ROOT,
    Component.BTRFS_REPORT_LAUNCHER: Owner.USER,
    Component.CPU_IRQ: Owner.ROOT,
    Component.CPU_TOOLBOX: Owner.USER,
}


SETUP_ENTRYPOINTS: Final[frozenset[str]] = frozenset(
    [
        "bluetooth-setup.sh",
        "btrfs-setup.sh",
        "btrfs/btrfs-degraded-boot-setup.sh",
        "btrfs/setup-bulk.bash",
        "btrfs/setup-snapshots.bash",
        "cpu-setup.sh",
        "cst",
        "cst-install.bash",
        "cst-link.bash",
        "swap-setup.sh",
        "toolchains-setup.sh",
        "vm-setup.sh",
    ]
)

CHECKOUT_ONLY_EXECUTABLES: Final[frozenset[str]] = frozenset(
    [
        ".github/scripts/audit_summary.py",
        ".github/scripts/pin_invowk.py",
        "bluetooth/bt-helper",
        "bluetooth/bt-setup",
        "btrfs/btrfs-cache",
        "btrfs/btrfs-commit-interval",
        "btrfs/btrfs-compress",
        "btrfs/btrfs-scrub-limit",
        "btrfs/btrfs-stall-watch",
        "btrfs/chrome-cache",
        "btrfs/tests/realbtrfs-snapshot-core.sh",
        "cst_core/cli.py",
        "cst_core/installer.py",
        "cst_core/route.py",
        "cpu/cpu-setup",
        "doctor/cst-doctor",
        "fix-anaconda-webui-btrfs-compress.sh",
        "perf/cst-perf",
        "swap/cli.py",
        "tests/mutation/cst-mutation",
        "tests/uv-dev",
    ]
)


# Repository files that look installable (a config or data file next to a
# module) but are read from the checkout only, with the reason.
CHECKOUT_ONLY_FILES: Final[Mapping[str, str]] = {
    "bluetooth/bluetooth.conf": "defaults read by bluetooth/bt-setup (cst bluetooth) from the checkout; never copied",
}


def manifest_by_src() -> Mapping[str, InstallEntry]:
    """Return mapping of repo-relative source paths to manifest entries."""
    return {entry.src: entry for entry in INSTALL_MANIFEST}


def manifest_by_dest() -> Mapping[str, InstallEntry]:
    """Return mapping of installed destination paths to manifest entries."""
    return {entry.dest: entry for entry in INSTALL_MANIFEST}


def python_entry_srcs() -> tuple[InstallEntry, ...]:
    """Return all Python helper entries packaged as standalone zipapps."""
    return tuple(entry for entry in INSTALL_MANIFEST if entry.kind == ArtifactKind.PYTHON_ENTRY)


def expand_dest(dest: str, home: str | None) -> str:
    """The destination with a leading `~/` replaced by `home` (a user-owned entry)."""
    if dest == "~" or dest.startswith("~/"):
        if home is None or home == "":
            raise ValueError(f"{dest}: a home folder is needed for this destination")
        return home.rstrip("/") + dest[1:]
    return dest


_PLACEHOLDER: Final = re.compile(r"@([A-Z_][A-Z0-9_]*)@")


def template_keys(text: str) -> frozenset[str]:
    """The KEYs of the @KEY@ placeholders in a template's text."""
    return frozenset(_PLACEHOLDER.findall(text))


def render_template(text: str, values: Mapping[str, str]) -> str:
    """`text` with every @KEY@ replaced literally by its value, ending in one newline.

    Same result as the shell render it replaces (`$(<file)` drops the trailing
    newlines, printf adds one back). One pass: a value that itself holds
    `@KEY@` is not replaced again. A placeholder without a value is an error
    (DomainValidationError): a unit with `What=/dev/disk/by-uuid/@UUID@` must
    never be written. Values no placeholder uses are checked by the caller,
    which sees every template of one install (InstallService).
    """
    missing = sorted(template_keys(text) - values.keys())
    if missing:
        raise DomainValidationError(
            "no value for " + ", ".join(f"@{k}@" for k in missing) + " (give it with --set KEY=VALUE)"
        )
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text).rstrip("\n") + "\n"


@dataclass(frozen=True, slots=True)
class OnDisk:
    """What is at an install destination now.

    data, mode, uid, gid: of the regular file found there, read through a
    symbolic link (data None: no regular file can be read there); is_link:
    the destination itself is a symbolic link.
    """

    data: bytes | None
    mode: int
    uid: int
    gid: int
    is_link: bool


def link_read_through(owner: Owner) -> bool:
    """THE rule for a symbolic link at a destination CST writes whole (the
    installer, vm backup-timer, toolchains' copies and units).

    At a root destination (owner root) a link is never current: it is
    replaced by an atomic write, never followed (only root can plant it, and
    root programs must not depend on where it points). At a user destination
    (owner user, `~/...`) a link is read through: when its target already
    holds what CST would write, it is left alone as a link (a dotfiles
    manager's link); otherwise the caller decides as for a file with those
    bytes, and a write replaces the link itself, never what it points to.
    """
    return owner == Owner.USER


def is_current(found: OnDisk | None, data: bytes, mode: int, owner: Owner, *, as_root: bool) -> bool:
    """True when the destination already holds exactly data with mode.

    A link counts only where link_read_through allows it; as root, a root
    entry must also be owned by 0:0 (a file left by a hand copy, owned by a
    user, is rewritten).
    """
    if found is None or found.data is None:
        return False
    if found.is_link and not link_read_through(owner):
        return False
    if (found.data, found.mode) != (data, mode):
        return False
    return not (as_root and owner == Owner.ROOT and (found.uid, found.gid) != (0, 0))


def select_entries(
    entries: Sequence[InstallEntry],
    *,
    module: str | None = None,
    kind: ArtifactKind | None = None,
    component: str | None = None,
) -> tuple[InstallEntry, ...]:
    """The entries of one module, one kind and/or one component, in manifest order."""
    return tuple(
        entry
        for entry in entries
        if (module is None or entry.module == module)
        and (kind is None or entry.kind == kind)
        and (component is None or entry.component == component)
    )
