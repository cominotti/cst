"""Pure domain model, value objects, and transformations for the `vm` bounded context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import io
import re
import xml.etree.ElementTree as ET

URI = "qemu:///system"
DEFAULT_VM = "win11-devbox"
VIRT_PACKAGES = (
    "qemu-kvm",
    "qemu-kvm-core",
    "qemu-system-x86-core",
    "libvirt-daemon",
    "edk2-ovmf",
    "swtpm",
    "libtpms",
)
NOT_INSTALLED = "not installed"
KNOBS = ("VM_NOCOW", "VM_TUNE_DISK_BUS", "VM_TUNE_DISK_DRIVER", "VM_TUNE_NIC")
NAS_DEFAULTS = {"VM_NAS_KEEP_WEEKLY": 4, "VM_NAS_KEEP_MONTHLY": 3, "VM_NAS_TIMEOUT_HOURS": 6}
LIVE_STATES = ("running", "paused", "pmsuspended")
OVERLAY_MARK = ".cst-overlay-"
TAG = "cst-vm"
KEEP_TAG = "cst-keep"
RETRY_LOCK = "30m"
ON_CALENDAR = "Sat *-*-* 04:30:00"
UNIT_MARKER = "# Managed by cst vm (CST)"
DRIVER_WANTED = (("cache", "none"), ("io", "native"), ("discard", "unmap"), ("detect_zeroes", "unmap"))
OLD_NICS = ("e1000", "e1000e", "rtl8139")
NIC_TYPES = ("network", "bridge", "direct", "user", "ethernet", "mcast", "server", "client", "udp", "null", "vds")
LINUX_OS = (
    "fedoraproject.org",
    "redhat.com",
    "centos.org",
    "almalinux.org",
    "rockylinux.org",
    "debian.org",
    "ubuntu.com",
    "archlinux.org",
    "opensuse.org",
    "suse.com",
    "alpinelinux.org",
    "gentoo.org",
    "nixos.org",
    "linuxmint.com",
    "manjaro.org",
    "kali.org",
    "endlessos.com",
    "silverblue",
    "popos",
    "elementary.io",
)
CPU_DISABLE_FALLBACK = "cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet"


class Usage(Exception):
    """Invalid CLI usage for a `cst vm` command."""


class Refused(Exception):
    """Precondition refused a `cst vm` operation safely."""


@dataclass(frozen=True, slots=True)
class VmKnobs:
    """Typed `/etc/cst/vm.conf` settings with repository defaults."""

    vm_nocow: bool = True
    vm_tune_disk_bus: bool = True
    vm_tune_disk_driver: bool = True
    vm_tune_nic: bool = True
    keep_weekly: int = 4
    keep_monthly: int = 3
    timeout_hours: int = 6

    @classmethod
    def from_text(cls, text: str) -> VmKnobs:
        """Parse key=value configuration text into immutable `VmKnobs`."""
        conf = parse_conf_text(text)

        def _bool(key: str) -> bool:
            return conf.get(key, "yes").strip().lower() not in ("no", "0", "false", "off")

        def _num(key: str) -> int:
            raw = conf.get(key, "")
            return int(raw) if raw.isascii() and raw.isdigit() else NAS_DEFAULTS[key]

        return cls(
            vm_nocow=_bool("VM_NOCOW"),
            vm_tune_disk_bus=_bool("VM_TUNE_DISK_BUS"),
            vm_tune_disk_driver=_bool("VM_TUNE_DISK_DRIVER"),
            vm_tune_nic=_bool("VM_TUNE_NIC"),
            keep_weekly=_num("VM_NAS_KEEP_WEEKLY"),
            keep_monthly=_num("VM_NAS_KEEP_MONTHLY"),
            timeout_hours=_num("VM_NAS_TIMEOUT_HOURS"),
        )

    def as_dict(self) -> dict[str, bool]:
        """Return boolean tuning knobs keyed by config name."""
        return {
            "VM_NOCOW": self.vm_nocow,
            "VM_TUNE_DISK_BUS": self.vm_tune_disk_bus,
            "VM_TUNE_DISK_DRIVER": self.vm_tune_disk_driver,
            "VM_TUNE_NIC": self.vm_tune_nic,
        }


@dataclass(frozen=True, slots=True)
class DomainParts:
    """Extracted storage, firmware, and validation items from inactive domain XML."""

    uuid: str
    disks: tuple[str, ...]
    nvram: str
    tpm: bool
    skipped: tuple[str, ...]
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return legacy dict view for callers expecting list fields."""
        return {
            "uuid": self.uuid,
            "disks": list(self.disks),
            "nvram": self.nvram,
            "tpm": self.tpm,
            "skipped": list(self.skipped),
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class TuneResult:
    """Outcome of planning/applying safe virtio-scsi and NIC tuning on domain XML."""

    after_xml: str | None
    changes: tuple[str, ...]
    held: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VmCliCommand:
    """Parsed `cst vm` command invocation."""

    cmd: str
    sub: str | None = None
    vm: str | None = None
    full: bool = False
    guest_virtio: bool = False
    root: bool = False
    nas: bool = False
    keep: str | None = None
    no_quiesce: bool = False

    def as_dict(self) -> dict[str, object]:
        """Return legacy dict representation for compatibility with CLI handlers."""
        return {
            "cmd": self.cmd,
            "sub": self.sub,
            "vm": self.vm,
            "full": self.full,
            "guest_virtio": self.guest_virtio,
            "root": self.root,
            "nas": self.nas,
            "keep": self.keep,
            "no_quiesce": self.no_quiesce,
        }


def parse_conf_text(text: str) -> dict[str, str]:
    """Parse `KEY=VALUE` lines from a CST config string."""
    vals: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            vals[k.strip()] = v.strip().strip("'\"")
    return vals


def extract_domain_parts(xml_text: str) -> DomainParts:
    """Extract UUID, file-backed disks, NVRAM, emulated TPM, and non-file disk issues."""
    root = ET.fromstring(xml_text)
    uuid = (root.findtext("uuid") or "").strip()
    disks: list[str] = []
    skipped: list[str] = []
    problems: list[str] = []
    nvram = ""

    for disk in root.findall("devices/disk"):
        device = disk.get("device", "disk")
        kind = disk.get("type", "file")
        target = disk.find("target")
        dev = target.get("dev") if target is not None else "?"
        src = disk.find("source")
        if device in ("cdrom", "floppy"):
            skipped.append(f"{dev} ({device}, not copied)")
            continue
        if kind != "file":
            where = ""
            if src is not None:
                where = src.get("dev") or src.get("name") or src.get("volume") or src.get("dir") or ""
            problems.append(
                f"{dev}: a {kind} disk{f' ({where})' if where else ''}: only file disks "
                "can be copied; back it up by hand"
            )
            continue
        path = src.get("file") if src is not None else None
        if not path:
            skipped.append(f"{dev} (no source file)")
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
        uuid=uuid,
        disks=tuple(disks),
        nvram=nvram,
        tpm=tpm,
        skipped=tuple(skipped),
        problems=tuple(problems),
    )


def keep_prefixes(xml_text: str) -> None:
    """Register non-default XML namespace prefixes before re-serializing domain XML."""
    for _, (prefix, uri) in ET.iterparse(io.StringIO(xml_text), events=("start-ns",)):
        if prefix and not re.fullmatch(r"ns\d+", prefix):
            ET.register_namespace(prefix, uri)


def normalized_xml(xml_text: str) -> str:
    """Normalize libvirt domain XML formatting while preserving namespace prefixes."""
    keep_prefixes(xml_text)
    root = ET.fromstring(xml_text)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def guest_kind(root: ET.Element) -> tuple[str, str]:
    """Classify guest OS as ('windows' | 'linux' | 'unknown', reason)."""
    for el in root.iter():
        if el.tag.endswith("}os") and el.get("id"):
            osid = el.get("id") or ""
            if "microsoft.com/win" in osid:
                return "windows", f"libosinfo {osid}"
            if any(k in osid for k in LINUX_OS):
                return "linux", f"libosinfo {osid}"
            return "unknown", f"libosinfo {osid}"
    if root.find("features/hyperv") is not None:
        return "windows", "Hyper-V enlightenments in <features>"
    return "unknown", "no libosinfo id in the definition"


def disk_index(name: str | None) -> int | None:
    """Convert target device name (`sda`, `sdaa`, `vdb`) to zero-based index."""
    m = re.fullmatch(r"[shv]d([a-z]+)", name or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - ord("a") + 1)
    return n - 1


def disk_name(idx: int) -> str:
    """Convert zero-based SCSI disk index to target device name (`sda`, `sdab`)."""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("a") + r) + s
    return "sd" + s


def tune_domain_xml(
    xml_text: str,
    knobs: Mapping[str, bool] | None = None,
    *,
    allow_bus: bool = True,
) -> TuneResult:
    """Compute tuned domain XML plus recorded changes, held bus/NIC items, and notes."""
    kn: Mapping[str, bool] = VmKnobs().as_dict() if knobs is None else knobs
    keep_prefixes(xml_text)
    root = ET.fromstring(xml_text)
    changes: list[str] = []
    held: list[str] = []
    notes: list[str] = []
    devices = root.find("devices")
    if devices is None:
        return TuneResult(after_xml=None, changes=(), held=(), notes=("no <devices> in the definition",))
    disks = devices.findall("disk")
    names: set[str | None] = {(t.get("dev") if (t := d.find("target")) is not None else None) for d in disks}
    ctrl: int | None = None

    def virtio_scsi_index() -> int:
        nonlocal ctrl
        if ctrl is not None:
            return ctrl
        scsi = [c for c in devices.findall("controller") if c.get("type") == "scsi"]
        found = next((c for c in scsi if c.get("model") == "virtio-scsi"), None)
        if found is not None:
            ctrl = int(found.get("index", "0"))
            return ctrl
        idx = max((int(c.get("index", "0")) for c in scsi), default=-1) + 1
        el = ET.Element("controller", {"type": "scsi", "index": str(idx), "model": "virtio-scsi"})
        kids = list(devices)
        last = max((i for i, c in enumerate(kids) if c.tag == "controller"), default=None)
        if last is None:
            last = max((i for i, c in enumerate(kids) if c.tag == "disk"), default=len(kids) - 1)
        devices.insert(last + 1, el)
        changes.append(f"add a virtio-scsi controller (index {idx})")
        ctrl = idx
        return idx

    def used_units(idx: int) -> set[int]:
        out: set[int] = set()
        on_scsi = [d for d in disks if (t := d.find("target")) is not None and t.get("bus") == "scsi"]
        on_scsi += [h for h in devices.findall("hostdev") if h.get("type") == "scsi"]
        for d in on_scsi:
            a = d.find("address")
            if (
                a is not None
                and a.get("type") == "drive"
                and a.get("controller", "0") == str(idx)
                and a.get("bus", "0") == "0"
                and a.get("target", "0") == "0"
            ):
                out.add(int(a.get("unit", "0")))
        return out

    for disk in disks:
        if disk.get("device", "disk") != "disk":
            continue
        target = disk.find("target")
        dev = (target.get("dev") or "?") if target is not None else "?"
        kind = disk.get("type", "file")
        if kind not in ("file", "block"):
            notes.append(f"disk {dev}: a {kind} disk: left as it is (only file and block disks are tuned)")
            continue
        if kn["VM_TUNE_DISK_DRIVER"]:
            drv = disk.find("driver")
            if drv is None:
                drv = ET.Element("driver", {"name": "qemu"})
                disk.insert(0, drv)
            if drv.get("name", "qemu") != "qemu":
                notes.append(f"disk {dev}: driver {drv.get('name')}: left as it is")
            else:
                for k, v in DRIVER_WANTED:
                    if drv.get(k) != v:
                        changes.append(f"disk {dev}: {k} {drv.get(k) or '(default)'} -> {v}")
                        drv.set(k, v)
        bus = target.get("bus") if target is not None else None
        if not kn["VM_TUNE_DISK_BUS"] or bus not in ("sata", "ide"):
            continue
        if not allow_bus:
            held.append(f"disk {dev}: bus {bus} -> scsi (virtio-scsi)")
            continue
        assert target is not None
        idx = virtio_scsi_index()
        new = dev
        if not dev.startswith("sd"):
            n = 0
            while disk_name(n) in names:
                n += 1
            new = disk_name(n)
            names.add(new)
            target.set("dev", new)
        target.set("bus", "scsi")
        addr = disk.find("address")
        if addr is not None and addr.get("type") == "drive":
            disk.remove(addr)
        taken = used_units(idx)
        want = disk_index(new)
        unit = want if want is not None and want not in taken else min(set(range(len(taken) + 1)) - taken)
        ET.SubElement(
            disk, "address", {"type": "drive", "controller": str(idx), "bus": "0", "target": "0", "unit": str(unit)}
        )
        changes.append(
            f"disk {dev}: bus {bus} -> scsi (virtio-scsi controller {idx}, unit {unit})"
            + (f", renamed {dev} -> {new}" if new != dev else "")
        )

    for nic in devices.findall("interface"):
        mac = nic.find("mac")
        label = f"network card {mac.get('address') if mac is not None else '(no MAC)'}"
        if nic.get("type") not in NIC_TYPES:
            continue
        model = nic.find("model")
        m = model.get("type") if model is not None else None
        if m is None:
            notes.append(f"{label}: no <model> (QEMU's default card): add <model type='virtio'/> by hand if wanted")
            continue
        if not kn["VM_TUNE_NIC"] or m not in OLD_NICS:
            continue
        if not allow_bus:
            held.append(f"{label}: {m} -> virtio")
            continue
        assert model is not None
        model.set("type", "virtio")
        changes.append(f"{label}: {m} -> virtio (same MAC)")

    bal = devices.find("memballoon")
    if bal is None:
        notes.append("no <memballoon>: not added (libvirt adds one by default; it was removed on purpose?)")
    elif bal.get("model") == "none":
        notes.append("memballoon model='none': left as it is")

    ET.indent(root, space="  ")
    return TuneResult(
        after_xml=ET.tostring(root, encoding="unicode") + "\n",
        changes=tuple(changes),
        held=tuple(held),
        notes=tuple(notes),
    )


def version_changes(old: Mapping[str, str] | None, new: Mapping[str, str] | None) -> list[str]:
    """Return `['pkg: old -> new']` for every virtualization package that changed."""
    out: list[str] = []
    for p in VIRT_PACKAGES:
        a, b = (old or {}).get(p), (new or {}).get(p)
        if a is not None and b is not None and a != b:
            out.append(f"{p}: {a} -> {b}")
    return out


def valid_keep(name: str | None) -> bool:
    """Validate a `--keep-tag` value so it cannot collide with managed/restic tags."""
    if not name:
        return False
    return (
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name))
        and not name.startswith(("cst-", "vm:"))
        and name not in ("btrfs-setup", "home", "Workspace")
    )


def overlay_kind(state: str | None, live_src: str | None, base: str) -> str:
    """Classify how an interrupted snapshot overlay relates to domain state."""
    if state in LIVE_STATES:
        return "definition" if live_src == base else "live"
    if state in ("shut off", "crashed"):
        return "offline"
    return "unknown"


def forget_args(vm: str, host: str, weekly: int, monthly: int) -> list[str]:
    """Build `restic forget` arguments scoped strictly to one VM and host."""
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


def parse_vm_args(argv: Sequence[str]) -> VmCliCommand:
    """Parse `cst vm` command-line arguments into an immutable `VmCliCommand`."""
    if not argv or argv[0] in ("help", "-h", "--help"):
        if len(argv) > 1:
            raise Usage("help takes no arguments")
        return VmCliCommand(cmd="help")

    cmd, rest = argv[0], list(argv[1:])
    if cmd not in ("backup", "backup-timer", "list", "status", "tune", "nocow"):
        raise Usage(f"unknown command '{cmd}'")

    subs: dict[str, tuple[str, ...]] = {
        "tune": ("plan", "apply"),
        "nocow": ("plan", "apply"),
        "backup-timer": ("on", "off", "status"),
    }
    sub: str | None = None
    if cmd in subs:
        if not rest or rest[0] not in subs[cmd]:
            raise Usage(f"{cmd}: {' or '.join(subs[cmd])}")
        sub, rest = rest[0], rest[1:]

    flags: dict[str, str] = {
        "backup": {"--full": "full", "--nas": "nas", "--no-quiesce": "no_quiesce"},
        "tune": {"--guest-has-virtio": "guest_virtio"},
        "nocow": {"--root": "root"} if sub == "plan" else {},
    }.get(cmd, {})
    takes_vm = cmd != "nocow"
    where = " ".join(filter(None, (cmd, sub)))

    vm: str | None = None
    full = False
    guest_virtio = False
    root = False
    nas = False
    keep: str | None = None
    no_quiesce = False
    seen_flags: set[str] = set()

    while rest:
        x = rest.pop(0)
        if cmd == "backup" and (x == "--keep-tag" or x.startswith("--keep-tag=")) and keep is None:
            value = x.split("=", 1)[1] if "=" in x else (rest.pop(0) if rest else "")
            if not valid_keep(value):
                raise Usage(
                    f"--keep-tag {value!r}: a name of letters, digits and . _ - (up to 64, not "
                    "starting with cst- or vm:), e.g. pre-f46"
                )
            keep = value
        elif x in flags and flags[x] not in seen_flags:
            field = flags[x]
            seen_flags.add(field)
            if field == "full":
                full = True
            elif field == "nas":
                nas = True
            elif field == "no_quiesce":
                no_quiesce = True
            elif field == "guest_virtio":
                guest_virtio = True
            elif field == "root":
                root = True
        elif takes_vm and not x.startswith("-") and vm is None:
            vm = x
        else:
            raise Usage(f"{where}: unexpected argument '{x}'")

    if full and nas:
        raise Usage("backup: --full is for the local copy, --nas for the NAS: one of them")
    if (keep is not None or no_quiesce) and not nas:
        raise Usage("backup: --keep-tag and --no-quiesce go with --nas")

    return VmCliCommand(
        cmd=cmd,
        sub=sub,
        vm=vm,
        full=full,
        guest_virtio=guest_virtio,
        root=root,
        nas=nas,
        keep=keep,
        no_quiesce=no_quiesce,
    )
