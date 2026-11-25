# SPDX-License-Identifier: MPL-2.0
"""Pure domain model, constants, parsers, and rules for `cst doctor`.

Standard library only (no filesystem I/O, no subprocess calls, no outer
layers).
"""

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import os.path
import re
import shlex
from typing import NewType, Self, TypedDict, TypeIs
import xml.etree.ElementTree as ET

from btrfs.docker_volumes_nocow.domain import NocowPaths
from btrfs.health_check.domain import UNALLOC_CRIT_GIB, UNALLOC_MIN_GIB
from btrfs.snapshot.domain import DEFAULT_STATE_DIR, UNFINISHED_FILE, WRITABLE_FILE
from cst_core.domain.contracts import CPU_SETTINGS, SNAPSHOTS_CONF, StateFormatKey, Urgency, state_format
from cst_core.domain.errors import Usage
from cst_core.domain.install import manifest_by_src
from cst_core.domain.text import SettingsProblem, ShellAssignments, group
from cst_core.domain.units import GLOBAL_USER_UNIT_DIR, SYSTEM_UNIT_DIR, USER_UNIT_DIR_REL
from cst_core.domain.values import HOUR, Json, parse_whole_number
from swap.domain import SETTINGS_REL

NAME = "cst-doctor"

# A path on the host machine ("/etc/cst/vm.conf"), and where this process
# reads it: the same path on the host, /run/host/... in a toolbox
# (DoctorHostPort.path maps one to the other). The host's file methods take
# a ViewPath only: a host path passed unmapped would read the toolbox's own
# file, and a ViewPath mapped again would point nowhere.
HostPath = NewType("HostPath", str)
ViewPath = NewType("ViewPath", str)


def host_join(base: HostPath, *parts: str) -> HostPath:
    """os.path.join on the host side."""
    return HostPath(os.path.join(base, *parts))


def view_join(base: ViewPath, *parts: str) -> ViewPath:
    """os.path.join on this process's side (a name inside a folder already mapped)."""
    return ViewPath(os.path.join(base, *parts))


OK, INFO, WARN, ERROR = "ok", "info", "WARN", "ERROR"
SEVERITY: dict[str, int] = {OK: 0, INFO: 1, WARN: 2, ERROR: 3}


class DoctorCommand(StrEnum):
    """What `cst doctor`'s command line asks for (parse_args)."""

    HELP = "help"
    RUN = "run"


class UnitScope(StrEnum):
    """Whose systemd manager a unit check asks: the system's or the user's (systemctl --user)."""

    SYSTEM = "system"
    USER = "user"


STAGED_OLD_DAYS = 7  # a staged deployment waiting longer gets a note
ALERT_KEEP_DAYS = 7  # as btrfs-notify and cst-pending-alerts
ALERT_MAX_FILE = 16384
ALERT_NAME = re.compile(r"^[0-9a-f]{32}\.alert$")
ALERT_PENDING = HostPath(state_format(StateFormatKey.ALERT_SPOOL).folder)  # btrfs-notify keeps them there

# The machine's own architecture: a bare name installs this one (or noarch).
ARCHES = frozenset({"x86_64", "i686", "i386", "noarch", "aarch64", "ppc64le", "s390x", "armv7hl", "riscv64", "src"})

# name-[epoch:]version-release with a distribution tag in the release
# (.fc44, .el9, .eln140), and an optional .arch: only the form gives it away
# when the deployment's package list cannot be read.
# The release is checked for the tag with a lookahead, then taken whole: [^-]* followed by the tag made
# the match backtrack over every ".fcN" of a long release.
NVR_FORM = re.compile(r"^(?P<name>.+)-(?:\d+:)?(?P<ver>\d[^-]*)-(?P<rel>\d(?=[^-]*\.(?:fc|el|eln)\d)[^-]*)$")

TRANSIENT = re.compile(r"^run-[^ ]+\.(scope|service)$")

BTRFS_SYSFS = HostPath("/sys/fs/btrfs")
RECLAIM_CONF = HostPath("/etc/tmpfiles.d/btrfs-automatic-reclaim.conf")
_BY_SRC = manifest_by_src()  # the install manifest by source, for the installed paths below
HEALTH_CHECK_HOST = HostPath(_BY_SRC["btrfs/btrfs-health-check"].dest)
# btrfs-notify, which a VM backup that fails alerts through (its manifest dest).
NOTIFY_HOST = HostPath(_BY_SRC["btrfs/btrfs-notify"].dest)
PENDING_READER_HOST = HostPath(_BY_SRC["btrfs/cst-pending-alerts"].dest)  # shows them at a login
# The installed units whose programs alert through it (the VM backup timers have their own check).
ALERTING_UNITS = tuple(
    HostPath(_BY_SRC[f"btrfs/units/{name}"].dest)
    for name in (
        "btrfs-bulk-boot.service",
        "btrfs-health-check.service",
        "btrfs-snapshot.service",
        "btrfs-snapshot-nas.service",
        "btrfs-stall-alert.service",
        "docker-volumes-nocow.service",
    )
)
# Those whose programs send through cst_core's notify adapter, which keeps an alert btrfs-notify cannot take.
# btrfs-snapshot.service is not among them: its runtime's alerts are kept, but those of its conversion step
# (the bash engine: "Subvolume conversion: manual check needed") are not.
KEEPING_UNITS = frozenset({"btrfs-bulk-boot.service", "btrfs-snapshot-nas.service", "docker-volumes-nocow.service"})
# GiB: the health check's own levels, when neither the installed copy nor the checkout's can be read.
UNALLOC_DEFAULTS = (UNALLOC_MIN_GIB, UNALLOC_CRIT_GIB)
FS_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Copies of each chunk per profile: a device holds one copy of every chunk of
# raid1 (2 devices), raid1c3 (3) or raid1c4 (4); single and dup only exist
# per device on a one-device filesystem. Other layouts: no per-device number.
MIRROR_COPIES: dict[str, int] = {"raid1": 2, "raid1c3": 3, "raid1c4": 4}
ONE_DEVICE_FACTOR: dict[str, int] = {"single": 1, "dup": 2}
# statfs rounds the size down to whole blocks, per device and per copy: a
# difference up to this much between the btrfs devices and their block
# devices is rounding, not a shrunk device.
RESIZE_SLACK = 16 * 2**20

# What btrfs-snapshot-core writes (its state folder and the two lists in it).
SNAP_STATE = HostPath(DEFAULT_STATE_DIR)
SNAP_UNFINISHED = HostPath(f"{SNAP_STATE}/{UNFINISHED_FILE}")
SNAP_WRITABLE = HostPath(f"{SNAP_STATE}/{WRITABLE_FILE}")
SNAP_CONF = HostPath(SNAPSHOTS_CONF)  # also read by the VM NAS check

NOCOW_POSTPONED = HostPath(NocowPaths().postponed)  # docker-volumes-nocow's record (StoredPostponed)

# The weekly scrub and the drop-in `cst btrfs install` writes from SCRUB_LIMIT.
SCRUB_UNIT = HostPath("/etc/systemd/system/btrfs-scrub-weekly.service")
SCRUB_DROPIN = HostPath("/etc/systemd/system/btrfs-scrub-weekly.service.d/10-cst-limit.conf")
_SCRUB_SIZE = re.compile(r"[0-9]+[KkMmGgTt]?")
_SCRUB_LIMIT_LINE = re.compile(r"[ \t]*(?:export[ \t]+)?SCRUB_LIMIT(?![A-Za-z0-9_])")
_SCRUB_DROPIN_LIMIT = re.compile(r"^ExecStartPre=/usr/sbin/btrfs scrub limit -a -l (\S+) /var$", re.MULTILINE)

# swap's zswap and memory responsiveness settings as installed (INSTALL_MANIFEST), and where the kernel has them.
ZSWAP_CONF = HostPath("/etc/tmpfiles.d/zswap.conf")
MM_RESPONSIVENESS_CONF = HostPath("/etc/tmpfiles.d/cst-mm-responsiveness.conf")
ZSWAP_PARAMS = HostPath("/sys/module/zswap/parameters")
# The partition swap manages (swap/domain.py, SETTINGS_REL), and the files an older CST marked a line in.
SWAP_CONF = HostPath("/etc/" + SETTINGS_REL)
SWAP_DROPIN = HostPath(_BY_SRC["swap/units/machine.slice.d-10-swap-memory-low.conf"].dest)
FSTAB = HostPath("/etc/fstab")
CRYPTTAB = HostPath("/etc/crypttab")
NOCOW_WARN_BOOTS = 2  # one boot can be chance; two in a row are a pattern

GAME_RUN = HostPath(state_format(StateFormatKey.GAME_STATE).folder)
GAME_STATE = HostPath(state_format(StateFormatKey.GAME_STATE).path)
GAME_INTENDED = HostPath(state_format(StateFormatKey.GAME_INTENDED).path)
GAME_MANUAL = HostPath(f"{GAME_RUN}/manual")
GAME_DROPIN_DIR = HostPath("/run/systemd/system")
GAME_DROPIN = "zz-cst-game.conf"
GAME_PROFILE_HOST = _BY_SRC["cpu/game-profile"].dest


def game_dropin_of(unit: str) -> HostPath:
    """The game profile's MemoryLow drop-in of a system unit: GAME_DROPIN_DIR/UNIT.d/GAME_DROPIN."""
    return host_join(GAME_DROPIN_DIR, f"{unit}.d", GAME_DROPIN)


# The top-level slices of the user manager that CST knows: app.slice (desktop
# apps and the game), session.slice (gnome-shell), background.slice (GNOME's
# background tasks: the file indexer), user.slice (the toolboxes) and
# sccache.slice (the host's shared sccache server, which the game profile
# demotes during games like any other top-level slice). Any other one is a
# sibling of app.slice that no rule covers outside games. (The game profile's
# own list differs: it makes background.slice and sccache.slice idle too.)
USER_KNOWN_SLICES = ("app.slice", "session.slice", "background.slice", "user.slice", "sccache.slice")
LIST_MAX = 5  # names shown per line, then "and N more"

VIRSH_URI = "qemu:///system"
CPU_CONF = HostPath(CPU_SETTINGS)
URESOURCED_CONF = HostPath(_BY_SRC["swap/units/uresourced.conf"].dest)
URESOURCED_CONF_BAK = HostPath(f"{URESOURCED_CONF}.pre-swap-config.bak")
URESOURCED_CST_MARKER = "Managed by swap-setup.sh (CST)"
# Host markers showing which CST modules are installed or configured for `doctor`'s responsiveness area.
MEMORY_RESPONSIVENESS_MARKERS: tuple[HostPath, ...] = (
    SWAP_CONF,
    ZSWAP_CONF,
    MM_RESPONSIVENESS_CONF,
    SWAP_DROPIN,
    HostPath(_BY_SRC["swap/sysctl/99-swap-tuning.conf"].dest),
    URESOURCED_CONF_BAK,
)
CPU_RESPONSIVENESS_MARKERS: tuple[HostPath, ...] = (
    CPU_CONF,
    HostPath("/etc/cpu-setup/irqbalance.env"),
    HostPath(_BY_SRC["cpu/units/cst-cpu-power.service"].dest),
    HostPath(_BY_SRC["cpu/units/cpu-game.service"].dest),
    HostPath("/etc/tmpfiles.d/cpu-setup-hwp-boost.conf"),
)
IO_RESPONSIVENESS_MARKERS: tuple[HostPath, ...] = (
    SNAP_CONF,
    RECLAIM_CONF,
    SCRUB_UNIT,
    HostPath(_BY_SRC["btrfs/sysctl/90-btrfs-writeback.conf"].dest),
    HostPath(_BY_SRC["btrfs/tmpfiles/cst-nvme-apst.conf"].dest),
    HostPath(_BY_SRC["btrfs/units/cst-btrfs-iocost.service"].dest),
)
CET_ISSUE = "https://gitlab.com/qemu-project/qemu/-/issues/3215"
VM_INDEX_FORMAT = state_format(StateFormatKey.VM_INDEX)
VM_INDEX = HostPath(VM_INDEX_FORMAT.path)
VM_TORN_FORMAT = state_format(StateFormatKey.VM_NAS_TORN)
VM_TORN = HostPath(VM_TORN_FORMAT.path)


VM_CONF = HostPath("/etc/cst/vm.conf")
VM_OVERLAY_MARK = ".cst-overlay-"  # as OVERLAY_MARK in vm/cst-vm
VM_RUN_DIR = HostPath("/run/cst-vm")  # nas-<vm>.lock: the pid of a NAS backup in progress
VM_TIMER_WANTS = HostPath("/etc/systemd/system/timers.target.wants")
# Packages that make up the guest's virtual hardware: name prefixes.
VIRT_PREFIXES = ("qemu-kvm", "qemu-system-x86", "libvirt-daemon", "edk2-ovmf", "swtpm", "libtpms")
# The ones named in the message (the others are only counted).
VIRT_MAIN = ("qemu-kvm", "qemu-kvm-core", "qemu-system-x86-core", "libvirt-daemon", "edk2-ovmf", "swtpm", "libtpms")


class Result(tuple[str, str, str]):
    """(status, message, fix): fix is the command to run, or ""."""

    __slots__ = ()

    def __new__(cls, status: str, message: str, fix: str = "") -> Self:
        return tuple.__new__(cls, (status, message, fix))

    @property
    def status(self) -> str:
        return str(self[0])

    @property
    def message(self) -> str:
        return str(self[1])

    @property
    def fix(self) -> str:
        return str(self[2])


# A deployment of `rpm-ostree status --json`: JSON with many keys of many
# types, read with .get and checked where it matters. These are the keys
# doctor reads; rpm-ostree writes others, which stay in the dict unread.
Deployment = TypedDict(
    "Deployment",
    {
        "booted": bool,
        "staged": bool,
        "pinned": bool,
        "osname": str,
        "version": str,
        "checksum": str,
        "timestamp": int,
        "requested-packages": list[str],
        "requested-local-packages": list[str],
        "requested-local-fileoverride-packages": list[str],
        "base-remote-replacements": Json,
        "base-local-replacements": list[Json],
        "requested-base-local-replacements": list[str],
        "base-removals": list[Json],
        "requested-base-removals": list[str],
    },
    total=False,
)


def is_deployment(d: object) -> TypeIs[Deployment]:
    """A deployment as cst_core.domain.ostree hands it (a JSON object, kept as it is)."""
    return isinstance(d, dict)


# (booted, pending, error) of ostree_deployments.
type OstreeAnswer = tuple[Deployment | None, Deployment | None, str]
# (name, epoch, version, release, arch) of a package, from parse_nevra.
type Nevra = tuple[str, str, str, str, str]


class PinHow(StrEnum):
    """How classify_requests sees one requested layered package (its docstring says what each means)."""

    NAME = "name"
    PINNED = "pinned"
    PINNED_FORM = "pinned-form"
    INACTIVE = "inactive"


# (request, spec, how) of one requested package, from classify_requests.
type Request = tuple[str, str, PinHow]


def parse_nevra(s: str) -> Nevra | None:
    """'name-[epoch:]version-release.arch' -> (name, epoch, version, release,
    arch), or None. The arch is the part after the last dot; version and
    release never contain a dash, so the name is everything before them."""
    if "." not in s:
        return None
    nvr, arch = s.rsplit(".", 1)
    parts = nvr.rsplit("-", 2)
    if len(parts) != 3 or not all(parts):
        return None
    name, ev, rel = parts
    epoch = ""
    if ":" in ev:
        epoch, ev = ev.split(":", 1)
        if parse_whole_number(epoch) is None:  # ASCII digits only: "²" or "٣" is no epoch
            return None
    if not ev or not rel:
        return None
    return name, epoch, ev, rel, arch


def spellings(pkg: Nevra) -> dict[str, bool]:
    """Every way a request can name this exact package with a version:
    {spelling: True if it names the architecture}."""
    name, epoch, ver, rel, arch = pkg
    out = {f"{name}-{ver}-{rel}": False, f"{name}-{ver}-{rel}.{arch}": True, f"{name}-{ver}": False}
    if epoch:
        out.update(
            {
                f"{name}-{epoch}:{ver}-{rel}": False,
                f"{name}-{epoch}:{ver}-{rel}.{arch}": True,
                f"{name}-{epoch}:{ver}": False,
            }
        )
    return out


def install_spec(name: str, arch: str, named: bool, native: str) -> str:
    """What to pass to --install= so that the same package comes back:
    name.arch when the request named the architecture, or when the package
    is of a foreign one (wine-core.i686 on x86_64); else the bare name."""
    if arch and (named or arch not in (native, "noarch")):
        return f"{name}.{arch}"
    return name


def classify_requests(requests: Iterable[str], packages: Iterable[str] | None, native: str) -> list[Request]:
    """Split requested-packages into by name and pinned to a version.
    packages: the deployment's installed NEVRAs, or None if unknown.
    Returns a list of (request, spec, how). how is:
      "name"          fine; spec is the request itself;
      "pinned"        the request is the name-version of an installed
                      package; spec is what --install= needs to bring that
                      same package back (name, or name.arch);
      "pinned-form"   the package list is unknown, but the request looks
                      like name-version-release.fcNN[.arch]; spec is read
                      off the string;
      "inactive"      looks like a pin, but no installed package matches
                      (an inactive request, for example one the base image
                      now provides): spec is "", nothing to install back."""
    names: set[str] = set()
    versioned: dict[str, list[tuple[str, str, bool]]] = {}
    for p in packages or ():
        parsed = parse_nevra(p)
        if not parsed:
            continue
        names.add(parsed[0])
        for s, named in spellings(parsed).items():
            versioned.setdefault(s, []).append((parsed[0], parsed[4], named))
    out: list[Request] = []
    for r in requests:
        if r in names:
            out.append((r, r, PinHow.NAME))
        elif r in versioned:
            out.append(_pinned_request(r, versioned[r], native))
        else:
            out.append(_unmatched_request(r, packages, native))
    return out


def _pinned_request(r: str, hits: list[tuple[str, str, bool]], native: str) -> Request:
    """A request that is the name-version of installed packages (hits: their (name, arch, whether the
    spelling names the arch))."""
    name, _, named = hits[0]
    arches = [a for _, a, _ in hits]
    # Without an arch in the request and several installed (multilib),
    # the bare name is what installed the native one.
    arch = "" if (not named and (native in arches or "noarch" in arches)) else arches[0]
    return (r, install_spec(name, arch, named, native), PinHow.PINNED)


def _unmatched_request(r: str, packages: Iterable[str] | None, native: str) -> Request:
    """A request that names no installed package: by name, unless it has the form of a pin (read off the
    string when the package list is unknown, else an inactive request)."""
    m = NVR_FORM.match(r)
    if not m:
        return (r, r, PinHow.NAME)  # a name, a provide (vim), a path...
    if packages is None:
        last = m.group("rel").rsplit(".", 1)[-1]
        arch = last if last in ARCHES else ""
        return (r, install_spec(m.group("name"), arch, bool(arch), native), PinHow.PINNED_FORM)
    return (r, "", PinHow.INACTIVE)


def unpin_command(classified: Iterable[Request]) -> str:
    """One command that swaps every pinned request for its bare name (a name
    already requested on its own is not requested twice). Inactive pins are
    only dropped: nothing installed matches them, so nothing is asked back."""
    pinned = [(r, n) for r, n, how in classified if how != PinHow.NAME]
    if not pinned:
        return ""
    by_name = {r for r, _, how in classified if how == PinHow.NAME}
    install: list[str] = []
    for _, n in pinned:
        if n and n not in by_name and n not in install:
            install.append(n)
    words = ["sudo", "rpm-ostree", "uninstall"] + [r for r, _ in pinned]
    words += [f"--install={n}" for n in install]
    return " ".join(shlex.quote(w) for w in words)


def pkg_names(obj: object) -> list[str]:
    """Package names in the JSON of replacements and removals. rpm-ostree
    writes packages there as [nevra, name, epoch, version, release, arch]
    or as plain NEVRA strings, inside lists (pairs new/old) or dicts."""
    out: list[str] = []

    def add(n: str) -> None:
        if n and n not in out:
            out.append(n)

    _walk_packages(obj, add)
    return out


def _walk_packages(o: object, add: Callable[[str], None]) -> None:
    """add each package name in o (pkg_names): a NEVRA string, a [nevra, name, ...] row, or the values
    of a list, tuple or dict, walked in order."""
    if isinstance(o, str):
        parsed = parse_nevra(o)
        add(parsed[0] if parsed else o)
        return
    if isinstance(o, dict):
        values: Iterable[object] = o.values()
    elif isinstance(o, (list, tuple)):
        name = _package_row_name(o)
        if name is not None:
            add(name)
            return
        values = o
    else:
        return
    for v in values:
        _walk_packages(v, add)


def _package_row_name(o: Sequence[object]) -> str | None:
    """The name of a [nevra, name, epoch, version, release, arch] row, None when o is no such row."""
    if (
        len(o) >= 2
        and isinstance(o[0], str)
        and isinstance(o[1], str)
        and (o[0] == o[1] or o[0].startswith(o[1] + "-"))
    ):
        return o[1]
    return None


def describe(d: Deployment) -> str:
    """'booted deployment 44.x', 'staged deployment 44.x', or 'pending
    deployment 44.x (next boot)' for the next boot that was not staged (after
    rpm-ostree rollback, or a deploy finished at once)."""
    ver = d.get("version") or (d.get("checksum") or "?")[:10]
    if d.get("booted", False):
        return f"booted deployment {ver}"
    if d.get("staged", False):
        return f"staged deployment {ver}"
    return f"pending deployment {ver} (next boot)"


def unit_results(scope: UnitScope, units: list[str]) -> Iterator[Result]:
    """Yield ok/WARN/info Results for failed systemd units in system or user scope."""
    user = scope == UnitScope.USER
    sysctl = "systemctl --user" if user else "sudo systemctl"
    journal = "journalctl --user" if user else "journalctl"
    real = [u for u in units if not TRANSIENT.match(u)]
    transient = [u for u in units if TRANSIENT.match(u)]
    if real:
        q = " ".join(shlex.quote(u) for u in real)
        logs = " ".join(f"-u {shlex.quote(u)}" for u in real)
        yield Result(
            WARN,
            f"{scope} units failed: {', '.join(real)}. Read why first: {journal} -b {logs} "
            "(the fix only clears the mark, not the cause)",
            f"{sysctl} reset-failed {q}",
        )
    if transient:
        q = " ".join(shlex.quote(u) for u in transient)
        yield Result(
            INFO,
            f"{scope}: {len(transient)} transient unit(s) failed (systemd-run, low priority): "
            f"{', '.join(transient)}; to clear: {sysctl} reset-failed {q}",
        )
    if not units:
        yield Result(OK, f"no failed {scope} units")


def parse_alert_text(data: str) -> tuple[str, str, int] | None:
    """Parse decoded alert file text into (urgency, title, last), or None; last is 0 when it is not
    ASCII digits."""
    lines = data.split("\n")
    if not lines or lines[0] not in ("cst-alert 1", "f44-alert 1"):
        return None
    head: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        k, _, v = line.partition("=")
        head[k] = v
    if head.get("urgency") not in (Urgency.CRITICAL, Urgency.NORMAL) or head.get("title", "") == "":
        return None
    return head["urgency"], head["title"][:200], parse_whole_number(head.get("last", "")) or 0


def mtime_matches_ns(recorded: str, mtime_ns: int) -> bool:
    """As cst-pending-alerts: compare recorded mtime string against nanoseconds."""
    sec, ns = divmod(mtime_ns, 1_000_000_000)
    whole, _, frac = recorded.partition(".")
    if whole != str(sec):
        return False
    return not frac or frac[:9].ljust(9, "0") == f"{ns:09d}"


def parse_unalloc_thresholds_text(text: str) -> tuple[int, int] | None:
    """Extract (UNALLOC_MIN_GIB, UNALLOC_CRIT_GIB) from btrfs-health-check's source: the bash version's
    `NAME=50` lines, or the Python version's `NAME: Final = 50` (btrfs/health_check/domain.py)."""
    w = re.search(r"^UNALLOC_MIN_GIB(?:=|: Final = )(\d+)\b", text, re.MULTILINE)
    c = re.search(r"^UNALLOC_CRIT_GIB(?:=|: Final = )(\d+)\b", text, re.MULTILINE)
    if w and c:
        return int(w.group(1)), int(c.group(1))
    return None


def pick_mount(targets: Sequence[str]) -> str:
    """The mount point to name a filesystem by: / or /var first (as
    btrfs-health-check), then /sysroot, then the shortest."""
    for p in ("/", "/var", "/sysroot"):
        if p in targets:
            return p
    return min(targets, key=len) if targets else ""


def per_device_allocated(profiles: Mapping[str, Mapping[str, int]], ndev: int) -> int | None:
    """Bytes allocated on each device, the same on all of them, when the
    layout makes that exact; else None."""
    used = {p for prof in profiles.values() for p in prof}
    if not used:
        return None
    if ndev == 1 and used <= set(ONE_DEVICE_FACTOR):
        return sum(t * ONE_DEVICE_FACTOR[p] for prof in profiles.values() for p, t in prof.items())
    if all(MIRROR_COPIES.get(p) == ndev for p in used):
        return sum(t for prof in profiles.values() for t in prof.values())
    return None


def parse_cpu_stat_seconds(text: str) -> float | None:
    """Parse cgroup cpu.stat text and return usage_usec in seconds, or None."""
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        usec = parse_whole_number(v.strip()) if k == "usage_usec" else None  # ASCII digits within int()'s limit
        if usec is not None:
            return usec / 1e6
    return None


def cpu_time(secs: float | None) -> str:
    """Human-readable CPU-seconds (and CPU-hours above 1 hour)."""
    if secs is None:
        return "CPU time unknown"
    if secs >= HOUR:
        return f"{secs:,.0f} CPU-seconds ({secs / HOUR:.1f} CPU-hours)"
    return f"{secs:,.0f} CPU-seconds"


def name_list(items: Sequence[str]) -> str:
    """Format up to LIST_MAX items followed by 'and N more' if truncated."""
    shown = ", ".join(items[:LIST_MAX])
    return shown + (f" and {len(items) - LIST_MAX} more" if len(items) > LIST_MAX else "")


def virt_versions(pkgs: Iterable[str]) -> dict[str, str]:
    """{name: version-release} of the virtualization packages in a NEVRA list."""
    out: dict[str, str] = {}
    for p in pkgs:
        parsed = parse_nevra(p)
        if parsed and parsed[0].startswith(VIRT_PREFIXES):
            name, epoch, ver, rel, _ = parsed
            out[name] = f"{epoch + ':' if epoch else ''}{ver}-{rel}"
    return out


def virt_changes(old: Mapping[str, str], new: Mapping[str, str]) -> str:
    """'qemu-kvm 10.2.2-1.fc44 -> 11.1.3-1.fc45, ...' (the main packages
    named, the rest counted), or "" when nothing changed."""
    changed = sorted(n for n in set(old) | set(new) if old.get(n) != new.get(n))
    if not changed:
        return ""
    main = [n for n in changed if n in VIRT_MAIN] or changed[:3]
    parts = [f"{n} {old.get(n, 'none')} -> {new.get(n, 'none')}" for n in main]
    rest = len(changed) - len(main)
    if rest:
        parts.append(f"and {rest} more virtualization package(s)")
    return ", ".join(parts)


def parse_offered_features_xml(xml_text: str) -> tuple[set[str | None] | None, str]:
    """Parse `virsh domcapabilities` XML for host-model offered CPU features."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return None, f"domcapabilities: unreadable ({e})"
    names: set[str | None] = set()
    for mode in root.findall("cpu/mode"):
        if mode.get("name") == "host-model":
            names |= {f.get("name") for f in mode.findall("feature") if f.get("policy") != "disable"}
    return names, ""


def summary(results: Sequence[tuple[str, Result]]) -> tuple[str, int]:
    """Format the doctor summary line and return (line, exit_code)."""
    counts = dict.fromkeys(SEVERITY, 0)
    for _, r in results:
        counts[r.status] += 1
    worst = max((SEVERITY[r.status] for _, r in results), default=0)
    verdict = "all good" if worst < SEVERITY[WARN] else "needs attention"
    line = f"doctor: {verdict}: {counts[ERROR]} ERROR, {counts[WARN]} WARN, {counts[INFO]} info, {counts[OK]} ok"
    return line, (1 if worst >= SEVERITY[WARN] else 0)


def parse_args(argv: Sequence[str]) -> tuple[DoctorCommand, bool]:
    """Parse `cst doctor` CLI flags into (help or run, brief)."""
    if argv and argv[0] in (DoctorCommand.HELP, "-h", "--help"):
        if len(argv) > 1:
            raise Usage("help takes no arguments")
        return DoctorCommand.HELP, False
    brief = False
    for a in argv:
        if a == "--brief" and not brief:
            brief = True
        else:
            raise Usage(f"unknown argument '{a}'")
    return DoctorCommand.RUN, brief


def scrub_limit_lines(settings: ShellAssignments) -> tuple[SettingsProblem, ...]:
    """The problem lines of the btrfs settings file that set SCRUB_LIMIT: each one makes
    `cst btrfs install` refuse to run."""
    return tuple(p for p in settings.problems if _SCRUB_LIMIT_LINE.match(p.text))


@dataclass(frozen=True, slots=True)
class ScrubLimitLines:
    """The SCRUB_LIMIT problem lines of snapshots.conf. bash: as `cst btrfs install` reads them. taken:
    the SCRUB_LIMIT problems CST's Python programs find (btrfs-snapshot and btrfs-health-check read the
    file with its line ends too, and also take the printf %q lines an older CST wrote) on those same
    lines; the scrub row warns about them, so the settings row skips them. python_too: their line
    numbers."""

    bash: tuple[SettingsProblem, ...]
    taken: tuple[SettingsProblem, ...]
    python_too: frozenset[int]


def scrub_limit_lines_both(bash: ShellAssignments, python: ShellAssignments | None) -> ScrubLimitLines:
    """ScrubLimitLines of snapshots.conf read by bash as bash and by CST's Python programs as python
    (None: not readable); both read the same lines (only a newline ends one), so a problem is matched
    by its line number."""
    lines = scrub_limit_lines(bash)
    numbers = {p.line for p in lines}
    taken = tuple(p for p in (scrub_limit_lines(python) if python else ()) if p.line in numbers)
    return ScrubLimitLines(lines, taken, frozenset(p.line for p in taken))


def scrub_limit_setting(settings: ShellAssignments) -> tuple[str, str]:
    """SCRUB_LIMIT of the btrfs settings file (read strictly, as bash sources it):
    (size, "") with size "" for no limit (unset, empty or 0), or ("", problem)
    when a SCRUB_LIMIT line is not a plain assignment (the first one) or the value
    is not a size (digits, then K, M, G or T). btrfs-setup.sh's scrub_limit_setting
    reads it the same way and refuses to install on a problem."""
    if lines := scrub_limit_lines(settings):
        return "", str(lines[0])
    value = settings.values.get("SCRUB_LIMIT", "")
    if value in ("", "0"):
        return "", ""
    if not _SCRUB_SIZE.fullmatch(value):
        return "", f"SCRUB_LIMIT='{value}' is not a size such as 500M or 1G"
    return value, ""


def scrub_dropin_limit(text: str) -> str:
    """The limit the scrub service's drop-in sets, or "" (no drop-in, no limit)."""
    m = _SCRUB_DROPIN_LIMIT.search(text)
    return group(m, 1) if m else ""


# The settings whose value is a command line (ExecSearchPath=, ExecPaths= hold folders, not programs).
_EXEC_COMMANDS = frozenset(
    ("ExecCondition", "ExecStartPre", "ExecStart", "ExecStartPost", "ExecReload", "ExecStop", "ExecStopPost")
)
_UNIT_SUFFIXES = (".service", ".timer", ".socket", ".path", ".mount", ".automount", ".slice", ".target")


def is_unit_file(dest: str) -> bool:
    """A unit file (a drop-in, in a NAME.d folder, is not one: it runs nothing of its own)."""
    return dest.endswith(_UNIT_SUFFIXES) and not os.path.dirname(dest).endswith(".d")


def _unit_folder_scope(folder: str, home: str) -> UnitScope | None:
    """Whose manager loads units from folder (home: the user's, "" for root's record), or None."""
    if folder == SYSTEM_UNIT_DIR:
        return UnitScope.SYSTEM
    if folder == GLOBAL_USER_UNIT_DIR or (home and folder == os.path.join(home.rstrip("/"), USER_UNIT_DIR_REL)):
        return UnitScope.USER
    return None


def recorded_unit(dest: str, home: str) -> tuple[UnitScope, str] | None:
    """(the manager, the unit name) of a recorded path that is a unit file (FOLDER/NAME) or a drop-in
    (FOLDER/NAME.d/FILE.conf) in a folder a manager loads units from; None for anything else (a
    *.wants link, a file of NAME.d that is no .conf, a drop-in of a manager's own settings such as
    journald.conf.d, /run). home: the user's, for ~/.config/systemd/user; "" for root's record."""
    folder, base = os.path.split(dest)
    scope = _unit_folder_scope(folder, home)
    if scope is not None:
        return (scope, base) if base.endswith(_UNIT_SUFFIXES) else None
    parent, sub = os.path.split(folder)
    scope = _unit_folder_scope(parent, home)
    name = sub.removesuffix(".d")
    if scope is None or name == sub or not name.endswith(_UNIT_SUFFIXES) or not base.endswith(".conf"):
        return None
    return scope, name


def unit_applies(name: str, unit_id: str) -> bool:
    """A recorded unit name applies to a loaded unit's Id: the same name, or a template's (p@.t) to each
    of its instances (p@i.t). Prefix drop-ins (a-.slice.d) and type-wide ones (service.d) are not matched:
    CST installs none."""
    stem, at, suffix = name.partition("@")
    if not (at and stem and suffix.startswith(".")):
        return unit_id == name
    head = f"{stem}@"
    return unit_id.startswith(head) and unit_id.endswith(suffix) and len(unit_id) > len(head) + len(suffix)


def unit_exec_paths(text: str, home: str) -> list[str]:
    """The CST programs a unit runs: every word of its command lines (ExecStart= and the like) under
    /usr/local/ or %h/ (%h as home), in order, without the prefixes systemd allows before the command
    (-@:+!|)."""
    out: list[str] = []
    roots = ("/usr/local/", home.rstrip("/") + "/") if home else ("/usr/local/",)
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() not in _EXEC_COMMANDS:
            continue
        for raw in value.split():
            word = raw.lstrip("-@:+!|")
            if word.startswith("%h/") and home:
                word = home.rstrip("/") + word[2:]
            if word.startswith(roots) and word not in out:
                out.append(word)
    return out
