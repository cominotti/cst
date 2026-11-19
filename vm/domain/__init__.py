# SPDX-License-Identifier: MPL-2.0
"""The vm module's rules: what a libvirt domain definition is made of, the
NAS backup's names and retention, the weekly timer unit, the command line
and the records the modules pass around. Pure: text in, text or values out.
The hardware `cst vm tune` recommends is in vm.domain.tune.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import os.path
import re
import shlex
from typing import Final, Literal, TypedDict
import xml.etree.ElementTree as ET

from cst_core.domain.text import HELP_WORDS
from cst_core.domain.errors import Refused, Usage
from cst_core.domain.files import UnitFileState
from cst_core.domain.units import render_timer as render_timer_unit, render_unit_file
from cst_core.domain.values import GIB, MINUTE, format_bytes_gib_mib

URI = "qemu:///system"

DEFAULT_VM = "win11-devbox"

# Packages whose version decides how the guest sees its virtual hardware.
# Fedora names QEMU's x86 emulator qemu-system-x86-core (qemu-kvm-core on
# some releases): every name is queried, the missing ones are recorded as such.
VIRT_PACKAGES = ("qemu-kvm", "qemu-kvm-core", "qemu-system-x86-core", "libvirt-daemon", "edk2-ovmf", "swtpm", "libtpms")

NOT_INSTALLED = "not installed"

FULL_MARGIN = (GIB, 0.05)  # free space kept on top of a --full copy: 1 GiB or 5 %, the larger

KNOBS = ("VM_NOCOW", "VM_TUNE_DISK_BUS", "VM_TUNE_DISK_DRIVER", "VM_TUNE_NIC")


USER_IMAGES = (
    (".local/share/libvirt/images", "libvirt qemu:///session: virt-manager, virsh as your user"),
    (".local/share/gnome-boxes/images", "GNOME Boxes (RPM)"),
    (".var/app/org.gnome.Boxes/data/gnome-boxes/images", "GNOME Boxes (Flatpak)"),
)

BTRFS_MAGIC = 0x9123683E

NAS_SSH_ALIAS = "btrfs-setup-nas"

OVERLAY_MARK = ".cst-overlay-"  # in the name of the snapshot overlay: <image>.cst-overlay-<UTC>

TAG = "cst-vm"  # every VM backup; plus vm:<name>

KEEP_TAG = "cst-keep"  # --keep-tag: never forgotten by the retention

RETRY_LOCK = "30m"  # the /home NAS job may hold the repository's lock

NAS_DEFAULTS = {"VM_NAS_KEEP_WEEKLY": 4, "VM_NAS_KEEP_MONTHLY": 3, "VM_NAS_TIMEOUT_HOURS": 6}

# Time limits of the NAS backup's commands (seconds).
RESTIC_CHECK_TIMEOUT: Final = 5 * MINUTE  # restic cat config and snapshots: a short answer
RESTIC_FORGET_TIMEOUT: Final = 30 * MINUTE  # restic forget, of one snapshot or by the keep policy
VIRSH_COMMIT_MARGIN: Final = 5 * MINUTE  # virsh blockcommit's wait beyond its own --timeout
STOP_MARGIN: Final = 30 * MINUTE  # after the merge: the checks and the alert (the timer's stop timeout)
SNAPSHOT_TIMEOUT_DEFAULT: Final = 10 * MINUTE  # CST_VM_SNAPSHOT_TIMEOUT and CST_VM_SNAPSHOT_SETTLE when unset

SHUT_OFF = "shut off"  # virsh domstate of a VM that is not running (in the C locale)

LIVE_STATES = ("running", "paused", "pmsuspended")  # QEMU is alive: a block job works

SERVICE = "cst-vm-backup@.service"

TIMER = "cst-vm-backup@.timer"

UNIT_MARKER = "# Managed by cst vm (CST)"

ON_CALENDAR = "Sat *-*-* 04:30:00"


class VmCommand(StrEnum):
    """The commands of cst vm."""

    HELP = "help"
    BACKUP = "backup"
    BACKUP_TIMER = "backup-timer"
    LIST = "list"
    STATUS = "status"
    TUNE = "tune"
    NOCOW = "nocow"


class VmSub(StrEnum):
    """The subcommands of the commands that take one."""

    PLAN = "plan"
    APPLY = "apply"
    ON = "on"
    OFF = "off"
    STATUS = "status"


# The words parse_args reads, in the order its messages list them.
COMMANDS: Final[dict[str, VmCommand]] = {c.value: c for c in VmCommand if c != VmCommand.HELP}

SUBS: Final[dict[VmCommand, dict[str, VmSub]]] = {
    VmCommand.TUNE: {s.value: s for s in (VmSub.PLAN, VmSub.APPLY)},
    VmCommand.NOCOW: {s.value: s for s in (VmSub.PLAN, VmSub.APPLY)},
    VmCommand.BACKUP_TIMER: {s.value: s for s in (VmSub.ON, VmSub.OFF, VmSub.STATUS)},
}


@dataclass(frozen=True, slots=True)
class VmArgs:
    """The command line of cst vm, as parse_args reads it."""

    cmd: VmCommand = VmCommand.HELP
    sub: VmSub | None = None
    vm: str | None = None
    full: bool = False
    guest_virtio: bool = False
    root: bool = False
    nas: bool = False
    keep: str | None = None
    no_quiesce: bool = False


@dataclass(frozen=True, slots=True)
class DomainParts:
    """What a backup copies, from the inactive definition (domain_parts)."""

    uuid: str
    disks: list[str]
    nvram: str
    tpm: bool
    skipped: list[str]
    problems: list[str]


@dataclass(frozen=True, slots=True)
class LiveDisk:
    """One disk of a definition (live_disks); backing: the live definition shows a backing chain."""

    target: str
    device: str
    kind: str
    source: str
    backing: bool


@dataclass(frozen=True, slots=True)
class VmPaths:
    """Where cst vm reads and writes on the machine. Each can be moved by its CST_VM_* variable
    (vm.wiring.vm_settings), for the tests only: scratch folders instead of the host's."""

    backup_dir: str  # the local copies: BACKUP_DIR/VM/STAMP
    index: str  # the backups index (cst_core.domain.contracts "vm-index")
    swtpm_dir: str  # the software TPM's state, per domain UUID
    vm_conf: str  # this module's settings (optional; every knob defaults to yes)
    cpu_conf: str  # cpu-setup.conf: VM_NAME, VM_CPU_DISABLE
    system_images: str  # libvirt's default storage pool
    nas_conf: str  # the /home NAS backup's snapshots.conf: its repository, shared
    restic_password: str  # its password file (restic reads it through RESTIC_PASSWORD_FILE)
    restic_cache: str
    nas_stage: str  # the definition and the manifest, written here for restic to read
    run_dir: str  # the per-VM lock of a NAS backup (holds the pid)
    notify: str  # btrfs-notify
    alerts: str  # where an alert btrfs-notify could not take is kept
    unit_dir: str  # the weekly timer's units
    sbin_copy: str  # the program copy the timer runs, as `install` places it
    install_record: str  # what backup-timer on writes is listed there


@dataclass(frozen=True, slots=True)
class VmSettings:
    """What the environment sets for one run of cst vm (vm.wiring.vm_settings)."""

    paths: VmPaths
    prog: str  # how the help and the hints name this program (CST_PROG, else cst-vm)
    cst: str  # how they name cst itself
    nonroot_ok: bool  # CST_VM_NONROOT_OK: the commands that need root run as the user (tests)
    under_systemd: bool  # INVOCATION_ID: started by systemd (the weekly timer)
    now: str | None  # CST_VM_NOW: the clock of the backups' names (tests)
    sudo_user: str | None  # SUDO_USER: whose home has the user's image folders under sudo
    # The merge back (every blockcommit of a run together; it copies the guest's writes of the
    # backup's duration). The timer's TimeoutStopSec leaves room for it after a stop.
    commit_timeout: int
    # virsh snapshot-create-as; after a timeout the run waits up to snapshot_settle seconds for
    # libvirt to finish (or not) on its own.
    snapshot_timeout: int
    snapshot_settle: int


class NasRepo(TypedDict):
    """The restic repository of the /home NAS backup, as a VM backup uses it (nas_settings): the
    --host of its snapshots, the repository, and restic's variables (the password stays in its file)."""

    host: str
    repo: str
    env: dict[str, str]


class TimerStepKind(StrEnum):
    """What one step of backup-timer on or off did (TimerStep)."""

    INSTALLED = "installed"
    UPDATED = "updated"
    OFF = "off"
    LEFT = "left"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class TimerStep:
    """What backup-timer on or off did, as it happens: a file installed or updated, the timer turned
    off (path: its unit), a masked file left as it is, a file removed."""

    kind: TimerStepKind
    path: str


@dataclass(frozen=True, slots=True)
class TimerStatus:
    """backup-timer status of one VM: its timer unit, whether it is on, the state of each file
    backup-timer on writes, and, when on, systemd's next run and last result ("" when unknown)."""

    unit: str
    on: bool
    files: dict[str, UnitFileState]
    next_run: str
    last_result: str


type ImageScope = Literal["system", "user"]


@dataclass(frozen=True, slots=True)
class ImageDir:
    """A known VM image folder: system (libvirt's pool, root's) or one in the user's home."""

    scope: ImageScope
    path: str
    what: str


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """An image file in a probed folder; nocow None: its flags are not readable by this user."""

    name: str
    path: str
    alloc: int
    size: int
    nocow: bool | None


@dataclass(frozen=True, slots=True)
class ImageProbe:
    """What a VM image folder looks like (probe_dir). present: a folder (a symbolic link is not:
    link holds its target, and it is never created, marked or listed); btrfs None: unknown;
    nocow None: not readable; listed False: not listable by this user; images: regular files,
    not .iso, not the *.cow-old leftovers of a conversion (leftovers)."""

    scope: ImageScope
    path: str
    what: str
    present: bool = False
    link: str | None = None
    btrfs: bool | None = None
    nocow: bool | None = None
    listed: bool = False
    images: list[ImageInfo] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)


def disk_label(dev: str | None, source: str = "", *, word: str = "") -> str:
    """A disk as a message names it: word + its target (sda). Without a
    target name (libvirt requires one, but a definition read here may lack
    it): "unnamed disk", with its source path when one is given."""
    if dev:
        return word + dev
    return f"unnamed disk on {source}" if source else "unnamed disk"


def domain_parts(xml_text: str) -> DomainParts:
    """From the inactive definition: uuid, disks [path], nvram, tpm, skipped,
    problems. problems are disks a file copy cannot back up (block, network,
    pool volumes): the backup refuses then, rather than leaving one out."""
    root = ET.fromstring(xml_text)
    disks: list[str] = []
    skipped: list[str] = []
    problems: list[str] = []
    nvram = ""
    for disk in root.findall("devices/disk"):
        device = disk.get("device", "disk")
        kind = disk.get("type", "file")
        target = disk.find("target")
        dev = target.get("dev") if target is not None else None
        src = disk.find("source")
        if device in ("cdrom", "floppy"):
            image = (src.get("file") or src.get("dev") or "") if src is not None else ""
            skipped.append(f"{disk_label(dev, image)} ({device}, not copied)")
            continue
        if kind != "file":
            where = ""
            if src is not None:
                where = src.get("dev") or src.get("name") or src.get("volume") or src.get("dir") or ""
            problems.append(
                f"{disk_label(dev)}: a {kind} disk{f' ({where})' if where else ''}: only file disks "
                "can be copied; back it up by hand"
            )
            continue
        path = src.get("file") if src is not None else None
        if not path:
            skipped.append(f"{disk_label(dev)} (no source file)")
            continue
        if path not in disks:
            disks.append(path)
    nv = root.find("os/nvram")
    if nv is not None:
        path = (nv.text or "").strip()
        s = nv.find("source")
        if not path and s is not None:
            path = s.get("file") or ""
        nvram = path
    tpm = any(b.get("type") == "emulator" for b in root.findall("devices/tpm/backend"))
    return DomainParts(
        uuid=(root.findtext("uuid") or "").strip(),
        disks=disks,
        nvram=nvram,
        tpm=tpm,
        skipped=skipped,
        problems=problems,
    )


def stored(dest_dir: str, src: str) -> str:
    """Where a source path is kept inside the backup: its absolute path
    under files/, so names never collide and restoring is one cp each."""
    return os.path.join(dest_dir, stored_name(src))


def stored_name(src: str) -> str:
    """stored()'s path relative to the backup folder (the manifest's "stored"): src is a resolved
    absolute path (realpath), so this is what os.path.relpath of stored() gives, without a cwd."""
    return os.path.join("files", src.lstrip("/"))


def restore_lines(vm: str, dest_dir: str, copied: Sequence[tuple[str, bool]], tpm_dir: str) -> list[str]:
    q = shlex.quote
    lines = [
        f"# 1. the VM must be shut off: virsh -c {URI} domstate {q(vm)}",
        "# 2. the files back (each line replaces the current file):",
    ]
    for src, _ in copied:
        if src == tpm_dir:
            lines.append(f"sudo rm -rf {q(src)} && sudo cp -a {q(stored(dest_dir, src))} {q(os.path.dirname(src))}/")
        else:
            lines.append(f"sudo cp --reflink=auto -a {q(stored(dest_dir, src))} {q(src)}")
    lines += [
        "# 3. the definition (CPU, pinning, devices) as it was:",
        f"sudo virsh -c {URI} define {q(os.path.join(dest_dir, 'domain.xml'))}",
    ]
    return lines


def live_disks(xml_text: str) -> list[LiveDisk]:
    """The disks of a definition; backing: the live definition shows a backing chain (an overlay)."""
    out: list[LiveDisk] = []
    for disk in ET.fromstring(xml_text).findall("devices/disk"):
        t, s, bs = disk.find("target"), disk.find("source"), disk.find("backingStore")
        src = ""
        if s is not None:
            src = s.get("file") or s.get("dev") or s.get("name") or s.get("volume") or ""
        out.append(
            LiveDisk(
                target=(t.get("dev") if t is not None else None) or "",
                device=disk.get("device", "disk"),
                kind=disk.get("type", "file"),
                source=src,
                backing=bs is not None and bs.find("source") is not None,
            )
        )
    return out


def agent_state(xml_text: str) -> str:
    """ "connected", "disconnected" or "none": the guest agent channel
    (org.qemu.guest_agent.0) of the live definition."""
    for ch in ET.fromstring(xml_text).findall("devices/channel"):
        t = ch.find("target")
        if t is not None and t.get("name") == "org.qemu.guest_agent.0":
            return "connected" if t.get("state") == "connected" else "disconnected"
    return "none"


class VmOverlayKind(StrEnum):
    """How a disk left on a backup overlay is put back by hand (overlay_kind)."""

    LIVE = "live"
    DEFINITION = "definition"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


def overlay_kind(state: str | None, live_src: str | None, base: str) -> VmOverlayKind:
    """How a disk left on an overlay is put back by hand:
    live: QEMU still runs on the overlay (running, paused, pmsuspended);
    definition: the running VM is back on the image, only its persistent
    definition names the overlay, which is now stale: never commit it;
    offline: shut off before any merge (this run knows none happened), so
    the overlay holds the guest's last writes;
    unknown: the state could not be read."""
    if state in LIVE_STATES:
        return VmOverlayKind.DEFINITION if live_src == base else VmOverlayKind.LIVE
    if state in (SHUT_OFF, "crashed"):
        return VmOverlayKind.OFFLINE
    return VmOverlayKind.UNKNOWN


def overlay_recovery(
    vm: str, items: Iterable[tuple[str, tuple[str, str]]], how: Mapping[str, VmOverlayKind]
) -> list[str]:
    """The commands that put a disk back on its image by hand."""
    q = shlex.quote
    v = f"sudo virsh -c {URI}"
    lines: list[str] = []
    for t, (base, overlay) in items:
        match how.get(t, VmOverlayKind.UNKNOWN):
            case VmOverlayKind.LIVE:
                lines += [
                    f"# {t}: the VM still runs on {overlay}",
                    f"{v} blockjob {q(vm)} {t} --info     # a commit still running? let it finish",
                    f"{v} blockjob {q(vm)} {t} --pivot     # only if --info shows the commit ready (100 %) "
                    "and nothing waits on it",
                    f"{v} blockcommit {q(vm)} {t} --active --pivot --wait --verbose     # only if no block job is listed",
                    f"{v} domblklist {q(vm)} --details     # {t} must show {base}",
                    f"{v} domblklist {q(vm)} --details --inactive     # here too",
                    f"sudo rm {q(overlay)}     # only once both show {base}",
                ]
            case VmOverlayKind.DEFINITION:
                lines += [
                    f"# {t}: the VM is back on {base}; only its definition still names {overlay}, which is "
                    "stale now: do NOT qemu-img commit it (that would overwrite newer data in the image)",
                    f"{v} edit {q(vm)}     # disk {t}: <source file='{base}'/>, and remove its <backingStore> element",
                    f"{v} domblklist {q(vm)} --details --inactive     # {t} must show {base}",
                    f"sudo rm {q(overlay)}     # only once it shows {base}",
                ]
            case VmOverlayKind.OFFLINE:
                lines += [
                    f"# {t}: {vm} is shut off on {overlay}, which holds the guest's last writes: merge it "
                    "offline, then point the definition back",
                    f"sudo qemu-img commit -p {q(overlay)}     # writes its changes into {base}",
                    f"{v} edit {q(vm)}     # disk {t}: <source file='{base}'/>, and remove its <backingStore> element",
                    f"{v} domblklist {q(vm)} --details --inactive     # {t} must show {base}",
                    f"sudo rm {q(overlay)}",
                ]
            case _:
                lines += [
                    f"# {t}: the VM's state or the files could not be read: look first, then follow "
                    'vm/README.md in CST, "If a backup left an overlay"',
                    f"{v} domstate {q(vm)}",
                    f"{v} domblklist {q(vm)} --details",
                    f"{v} domblklist {q(vm)} --details --inactive",
                    f"sudo ls -l --time-style=full-iso {q(base)} {q(overlay)}",
                ]
    return lines


def quiesce_skipped(agent: str, *, quiesce: bool) -> str:
    """Why the NAS backup's snapshot is not even tried with --quiesce ("" when it is): agent is
    agent_state's answer."""
    if not quiesce:
        return "--no-quiesce"
    if agent == "none":
        return "no guest agent channel (org.qemu.guest_agent.0) in the definition"
    if agent != "connected":
        return "the guest agent is not connected"
    return ""


def valid_keep(name: str | None) -> bool:
    if not name:
        return False
    return (
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name))
        and not name.startswith(("cst-", "vm:"))
        and name not in ("btrfs-setup", "home", "Workspace")
    )


def forget_args(vm: str, host: str, weekly: int, monthly: int) -> list[str]:
    """restic forget for this VM only: --host and both tags, so the /home
    backup's snapshots (tag btrfs-setup) and hand-made ones (without the
    cst-vm tag) are never considered; --group-by host: one group per VM even
    when its disks change. Never --prune (the /home NAS job prunes)."""
    return [
        "forget",
        "--host",
        host,
        "--tag",
        f"{TAG},vm:{vm}",
        "--group-by",
        "host",
        "--keep-weekly",
        str(weekly),
        "--keep-monthly",
        str(monthly),
        "--keep-tag",
        KEEP_TAG,
        "--retry-lock",
        RETRY_LOCK,
    ]


def render_timer() -> str:
    return render_timer_unit(
        [
            f"{UNIT_MARKER}: installed by `cst vm backup-timer on`; not edited by hand.",
            "# Saturday morning: the /home NAS job starts between 00:00 and 01:00 every",
            "# day, the btrfs scrub runs on Sunday at 02:00.",
        ],
        "CST: weekly backup of the VM %i to the NAS",
        ON_CALENDAR,
        "30min",
    )


def render_service(program: str, stop_timeout: str) -> str:
    """The service the timer starts: `program` (the installed cst-vm) backs up
    the VM %i to the NAS; stop_timeout is TimeoutStopSec."""
    return render_unit_file(
        [
            f"{UNIT_MARKER}: installed by `cst vm backup-timer on`; not edited by hand.",
            "# The weekly backup of the libvirt VM %i to the NAS (restic): a live",
            "# snapshot while restic reads the disks, then merged back. Why and how:",
            "# vm/README.md in CST.",
        ],
        [
            (
                "Unit",
                [
                    "Description=CST: backup of the VM %i to the NAS (cst vm backup --nas)",
                    "Wants=network-online.target",
                    "After=network-online.target virtqemud.service libvirtd.service",
                    f"ConditionPathExists={program}",
                ],
            ),
            (
                "Service",
                [
                    "Type=oneshot",
                    'Environment="CST_PROG=cst vm"',
                    f"ExecStart=/usr/bin/python3 -I {program} backup %i --nas",
                    "SyslogIdentifier=cst-vm",
                    "Nice=19",
                    "IOSchedulingClass=idle",
                    "CPUWeight=20",
                    "IOWeight=20",
                    "# On stop, SIGTERM reaches the program only: it stops restic and merges the",
                    "# snapshot back before it ends (KillMode=mixed). TimeoutStopSec covers a held",
                    "# snapshot call plus the merge's own limit (virsh blockcommit --timeout) plus",
                    "# a margin, so systemd never kills a merge half done. No start limit: every step has its own (restic:",
                    "# VM_NAS_TIMEOUT_HOURS in /etc/cst/vm.conf).",
                    "KillMode=mixed",
                    "TimeoutStartSec=infinity",
                    f"TimeoutStopSec={stop_timeout}",
                ],
            ),
        ],
    )


def timer_file_words(prog: str, vm: str) -> dict[str, str]:
    """What `backup-timer status` says about each file of the timer, by its
    owned-file state."""
    return {
        "ok": "installed, up to date",
        "missing": "not installed",
        "conflict": "not CST's file",
        "change": f"installed, OLDER than this copy of CST: {prog} backup-timer on {vm} updates it",
        "masked": f"masked (systemctl mask): unmask it first, then {prog} backup-timer on {vm} (off leaves it)",
    }


def is_cst_vm_program(data: bytes) -> bool:
    """True for an installed copy of cst-vm from any CST version: the plain
    script older versions installed, or the zipapp (stored uncompressed, so
    its __main__.py, which starts with the program's docstring, is in the bytes)."""
    return data.startswith(b"#!/usr/bin/python3 -I\n") and b'"""cst-vm:' in data


def instance(vm: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", vm) or vm.startswith("-"):
        raise Refused(f"VM name {vm!r}: the timer takes names of letters, digits and _ . : - only")
    return f"cst-vm-backup@{vm}.timer"


def version_changes(old: Mapping[str, str] | None, new: Mapping[str, str] | None) -> list[str]:
    """['pkg: old -> new'] for every virtualization package that differs."""
    out: list[str] = []
    for p in VIRT_PACKAGES:
        a, b = (old or {}).get(p), (new or {}).get(p)
        if a is not None and b is not None and a != b:
            out.append(f"{p}: {a} -> {b}")
    return out


def convert_lines(img: ImageInfo, system: bool) -> list[str]:
    """The commands that make an existing image NOCOW (never run by CST)."""
    q = shlex.quote
    s = "sudo " if system else ""
    f, old = q(img.path), q(img.path + ".cow-old")
    return [
        f"{s}mv {f} {old}",
        f"{s}install -m 0600 /dev/null {f}      # new empty file, private until cp -a sets the mode",
        f"{s}chattr +C {f}",
        f"{s}cp -a --reflink=never --sparse=auto {old} {f}",
        f"{s}cmp {old} {f} && echo same",
        f"# start the VM; after a good boot: {s}rm {old}",
    ]


def todo_note(d: ImageProbe) -> str:
    """What nocow plan says after a folder it would mark: not there yet, or its mark not readable."""
    if not d.present:
        return " (not created yet)"
    if d.nocow is None:
        return " (mark not readable here: set again, harmless)"
    return ""


def nocow_lines(d: ImageProbe, prog: str, verbose: bool = True) -> list[str]:
    """Lines describing one probed folder (nocow plan and status); prog names cst vm in the hints."""
    head = f"{d.path}  [{d.what}]"
    if d.link is not None:
        return [
            f"{head}: a symbolic link to {d.link}: not managed by CST (set the mark on the real "
            f"folder by hand if it is on btrfs: chattr +C <folder>)"
        ]
    if not d.present:
        return [f"{head}: not present"]
    if d.btrfs is False:
        return [f"{head}: not on btrfs (NOCOW does not apply)"]
    out: list[str] = [head]
    root_hint = f"{prog} nocow plan --root"
    if d.nocow is None:
        out.append(f"  NOCOW mark (+C): not readable as your user (root-only folder): {root_hint}")
    else:
        out.append(
            "  NOCOW mark (+C): "
            + ("yes" if d.nocow else f"no: {prog} nocow apply sets it (only files created later get it)")
        )
    if not d.listed:
        if d.nocow is not None or d.scope != "system":
            out.append("  images: folder not listable as your user")
        return out
    cow = [i for i in d.images if i.nocow is False]
    unknown = [i for i in d.images if i.nocow is None]
    ok = [i for i in d.images if i.nocow]
    if not d.images:
        out.append("  images: none")
    if ok:
        out.append(f"  NOCOW images: {', '.join(i.name for i in ok)}")
    if unknown:
        out.append(
            f"  images whose mark is not readable as your user: {', '.join(i.name for i in unknown)} ({root_hint})"
        )
    for i in cow:
        out.append(
            f"  still copy-on-write: {i.name}  {format_bytes_gib_mib(i.alloc, mib_decimals=1)} allocated, {format_bytes_gib_mib(i.size, mib_decimals=1)} apparent"
        )
        if verbose:
            out.append(
                f"    to convert (every VM using it shut off; needs about {format_bytes_gib_mib(i.alloc, mib_decimals=1)} free):"
            )
            out += [f"      {ln}" for ln in convert_lines(i, d.scope == "system")]
    for n in d.leftovers:
        out.append(f"  leftover of a conversion: {n} (remove it once the VM booted well from the new file)")
    return out


def parse_args(argv: Sequence[str]) -> VmArgs:
    """The command line of cst vm; Usage when it is not one."""
    if not argv or argv[0] in HELP_WORDS:
        if len(argv) > 1:
            raise Usage("help takes no arguments")
        return VmArgs()
    word, rest = argv[0], list(argv[1:])
    cmd = COMMANDS.get(word)
    if cmd is None:
        raise Usage(f"unknown command '{word}'")
    sub: VmSub | None = None
    if cmd in SUBS:
        subs = SUBS[cmd]
        sub = subs.get(rest[0]) if rest else None
        if sub is None:
            raise Usage(f"{cmd}: {' or '.join(subs)}")
        rest = rest[1:]
    flags = _FLAGS.get(cmd, ())
    if cmd == VmCommand.NOCOW and sub != VmSub.PLAN:
        flags = ()
    takes_vm = cmd != VmCommand.NOCOW
    where = " ".join(filter(None, (cmd, sub)))
    seen: set[str] = set()
    vm: str | None = None
    keep: str | None = None
    while rest:
        x = rest.pop(0)
        if cmd == VmCommand.BACKUP and (x == "--keep-tag" or x.startswith("--keep-tag=")) and keep is None:
            value = x.split("=", 1)[1] if "=" in x else (rest.pop(0) if rest else "")
            if not valid_keep(value):
                raise Usage(
                    f"--keep-tag {value!r}: a name of letters, digits and . _ - (up to 64, not "
                    "starting with cst- or vm:), e.g. pre-f46"
                )
            keep = value
        elif x in flags and x not in seen:
            seen.add(x)
        elif takes_vm and not x.startswith("-") and vm is None:
            vm = x
        else:
            raise Usage(f"{where}: unexpected argument '{x}'")
    a = VmArgs(
        cmd=cmd,
        sub=sub,
        vm=vm,
        full="--full" in seen,
        guest_virtio="--guest-has-virtio" in seen,
        root="--root" in seen,
        nas="--nas" in seen,
        keep=keep,
        no_quiesce="--no-quiesce" in seen,
    )
    if a.full and a.nas:
        raise Usage("backup: --full is for the local copy, --nas for the NAS: one of them")
    if (a.keep is not None or a.no_quiesce) and not a.nas:
        raise Usage("backup: --keep-tag and --no-quiesce go with --nas")
    return a


# The flags each command takes (nocow: --root with plan only).
_FLAGS: Final[dict[VmCommand, tuple[str, ...]]] = {
    VmCommand.BACKUP: ("--full", "--nas", "--no-quiesce"),
    VmCommand.TUNE: ("--guest-has-virtio",),
    VmCommand.NOCOW: ("--root",),
}
