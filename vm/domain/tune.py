# SPDX-License-Identifier: MPL-2.0
"""The hardware `cst vm tune` recommends (pure rules): disks on virtio-scsi with cache='none'
io='native' discard='unmap' detect_zeroes='unmap', a virtio network card, and what is held back
for a guest whose virtio drivers are not known to be installed. cst doctor's hardware check reads
the same rules.
"""

from collections.abc import Mapping, Sequence
import re
import xml.etree.ElementTree as ET

from cst_core.domain.libvirt_xml import keep_prefixes

DRIVER_WANTED = (("cache", "none"), ("io", "native"), ("discard", "unmap"), ("detect_zeroes", "unmap"))

OLD_NICS = ("e1000", "e1000e", "rtl8139")

# Interface types with an emulated card (a <model>); hostdev, vhostuser and vdpa are not.
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


def guest_kind(root: ET.Element) -> tuple[str, str]:
    """("windows" | "linux" | "unknown", why). From the libosinfo id that
    virt-manager/virt-install/Boxes write into <metadata>, else Hyper-V
    enlightenments (only Windows guests use them)."""
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
    """sda -> 0, sdz -> 25, sdaa -> 26 (libvirt's numbering); None if not sdX/hdX/vdX."""
    m = re.fullmatch(r"[shv]d([a-z]+)", name or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - ord("a") + 1)
    return n - 1


def disk_name(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("a") + r) + s
    return "sd" + s


def tune_domain(
    xml_text: str, kn: Mapping[str, bool], allow_bus: bool = True
) -> tuple[str | None, list[str], list[str], list[str]]:
    """(after_xml, changes, held, notes) for a definition: changes are what
    the new definition differs in, held the bus and network card changes
    left out because allow_bus is False (a guest without virtio drivers),
    notes things reported but not changed. Only elements that differ from
    the desired state are touched."""
    keep_prefixes(xml_text)
    root = ET.fromstring(xml_text)
    changes: list[str] = []
    held: list[str] = []
    notes: list[str] = []
    devices = root.find("devices")
    if devices is None:
        return None, changes, held, ["no <devices> in the definition"]
    disks = devices.findall("disk")
    names: set[str | None] = {(t.get("dev") if (t := d.find("target")) is not None else None) for d in disks}
    ctrl_idx: int | None = None

    def virtio_scsi_index() -> int:
        nonlocal ctrl_idx
        if ctrl_idx is not None:
            return ctrl_idx
        scsi = [c for c in devices.findall("controller") if c.get("type") == "scsi"]
        found = next((c for c in scsi if c.get("model") == "virtio-scsi"), None)
        if found is not None:
            ctrl_idx = int(found.get("index", "0"))
            return ctrl_idx
        idx = max((int(c.get("index", "0")) for c in scsi), default=-1) + 1
        el = ET.Element("controller", {"type": "scsi", "index": str(idx), "model": "virtio-scsi"})
        kids = list(devices)
        last = max((i for i, c in enumerate(kids) if c.tag == "controller"), default=None)
        if last is None:
            last = max((i for i, c in enumerate(kids) if c.tag == "disk"), default=len(kids) - 1)
        devices.insert(last + 1, el)
        changes.append(f"add a virtio-scsi controller (index {idx})")
        ctrl_idx = idx
        return idx

    def used_units(idx: int) -> set[int]:
        """Units taken on controller idx (bus 0, target 0): SCSI disks and
        CD-ROMs, and SCSI host devices (<hostdev type='scsi'>)."""
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
            continue  # cdrom, floppy, lun: left as they are
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
            continue  # hostdev, vhostuser, vdpa: not an emulated card
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
    return ET.tostring(root, encoding="unicode") + "\n", changes, held, notes


def normalized(xml_text: str) -> str:
    keep_prefixes(xml_text)
    root = ET.fromstring(xml_text)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


WINDOWS_STEPS = """The virtio drivers must be in Windows BEFORE its disk moves to virtio-scsi
(else: INACCESSIBLE_BOOT_DEVICE). With the guest running:
  1. attach the virtio-win ISO (fedorapeople.org virtio-win, check its sha256) as a CD-ROM;
  2. in an admin PowerShell (D: = the ISO; the .msi is unsigned, the .inf files are WHQL-signed):
       Get-AuthenticodeSignature D:\\vioscsi\\w11\\amd64\\vioscsi.cat
       pnputil /add-driver D:\\vioscsi\\w11\\amd64\\vioscsi.inf /install
       pnputil /add-driver D:\\NetKVM\\w11\\amd64\\netkvm.inf /install
       pnputil /add-driver D:\\Balloon\\w11\\amd64\\balloon.inf /install
  3. hot-add a virtio-scsi controller so Windows binds vioscsi as a boot driver:
       virsh -c qemu:///system attach-device {vm} ctrl.xml --live
     (ctrl.xml: <controller type='scsi' model='virtio-scsi'/>; it needs a free PCIe root port); then in Windows
       sc qc vioscsi        -> START_TYPE must be 0 (BOOT_START)
  4. shut Windows down fully (Fast Startup makes the Start menu's Shut down a hibernation, and
     resuming it on a changed boot controller can fail): in an admin prompt `shutdown /s /t 0`,
     or Shift + Shut down; then: {cst} vm tune apply {vm} --guest-has-virtio
Details: vm/README.md in CST, section "Windows guests"."""


def cpu_owner_line(root: ET.Element, names: Sequence[str], cst: str) -> str:
    """How cst cpu vm-pin's part of the definition stands (reported, never changed here);
    names: the features vm-pin keeps disabled (cpu_disable_list)."""
    cpu = root.find("cpu")
    mode = cpu.get("mode", "custom") if cpu is not None else "not set"
    off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"} if cpu is not None else set()
    missing = [n for n in names if n not in off]
    pins = {c for p in root.findall("cputune/vcpupin") if (c := p.get("cpuset")) is not None}
    pin = f"vCPUs pinned to {', '.join(sorted(pins))}" if pins else "vCPUs not pinned"
    feat = (
        f"NOT disabled: {' '.join(missing)}"
        if mode == "host-passthrough" and missing
        else "CET/MBEC disabled"
        if mode == "host-passthrough" and names
        else f"cpu mode {mode}"
    )
    return f"CPU (owned by {cst} cpu vm-pin, not by tune): {feat}; {pin}; check: {cst} cpu vm-pin plan"


def tune_line(xml_text: str, kn: Mapping[str, bool], cst: str) -> str:
    """One status line: whether the hardware matches tune (all changes, as if
    the drivers were there; Windows gets the hint to read the plan); kn: the knobs."""
    try:
        _, changes, _, _ = tune_domain(xml_text, kn, True)
        kind, _ = guest_kind(ET.fromstring(xml_text))
    except ET.ParseError:
        return "hardware: definition not readable"
    if not changes:
        return "hardware: disks and network card as tune recommends"
    extra = " (Windows: virtio drivers first, the plan shows how)" if kind != "linux" else ""
    return f"hardware: {len(changes)} difference(s) from tune{extra}: {cst} vm tune plan"


def cpu_status_line(cpu: ET.Element | None, names: Sequence[str], cst: str) -> str:
    """The status line of the definition's <cpu> element (None: none, or not readable): whether the
    features vm-pin keeps disabled (names, cpu_disable_list) are."""
    if cpu is None:
        return "cpu: no <cpu> element (or not readable)"
    mode = cpu.get("mode", "custom")
    off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"}
    missing = [n for n in names if n not in off]
    if mode != "host-passthrough":
        return f"cpu mode {mode}: the CET/MBEC disables are only for host-passthrough"
    if not names:
        return f"cpu mode {mode}: CPU features not managed (VM_CPU_DISABLE is empty)"
    if missing:
        return (
            f"cpu mode {mode}: NOT disabled: {' '.join(missing)} (Windows may bootloop under "
            f"QEMU 11; fix: {cst} cpu vm-pin plan)"
        )
    return f"cpu mode {mode}: disabled {' '.join(names)}"
