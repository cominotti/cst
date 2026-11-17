"""The vm module's rules: what a libvirt domain definition is made of, the
hardware `cst vm tune` recommends, the NAS backup's names and retention,
the weekly timer unit, the command line. Pure: text in, text or values out.
"""

from collections.abc import Iterable, Mapping, Sequence
import os.path
import re
import shlex
from typing import Any
import xml.etree.ElementTree as ET

from cst_core.domain.errors import Refused, Usage
from cst_core.domain.libvirt_xml import keep_prefixes
from cst_core.domain.units import render_timer as render_timer_unit, render_unit_file
from cst_core.domain.values import GIB

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

LIVE_STATES = ("running", "paused", "pmsuspended")  # QEMU is alive: a block job works

SERVICE = "cst-vm-backup@.service"

TIMER = "cst-vm-backup@.timer"

UNIT_MARKER = "# Managed by cst vm (CST)"

ON_CALENDAR = "Sat *-*-* 04:30:00"


def disk_label(dev: str | None, source: str = "", *, word: str = "") -> str:
    """A disk as a message names it: word + its target (sda). Without a
    target name (libvirt requires one, but a definition read here may lack
    it): "unnamed disk", with its source path when one is given."""
    if dev:
        return word + dev
    return f"unnamed disk on {source}" if source else "unnamed disk"


def domain_parts(xml_text: str) -> dict[str, Any]:
    """From the inactive definition: {uuid, disks: [path], nvram, tpm, skipped,
    problems}. problems are disks a file copy cannot back up (block, network,
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
    return {
        "uuid": (root.findtext("uuid") or "").strip(),
        "disks": disks,
        "nvram": nvram,
        "tpm": tpm,
        "skipped": skipped,
        "problems": problems,
    }


def index_entry(manifest: Mapping[str, Any], path: str) -> dict[str, Any]:
    return {
        "vm": manifest["vm"],
        "timestamp": manifest["timestamp"],
        "epoch": manifest["epoch"],
        "path": path,
        "mode": manifest["mode"],
        "versions": manifest["versions"],
        "deployment": manifest["deployment"],
    }


def stored(dest_dir: str, src: str) -> str:
    """Where a source path is kept inside the backup: its absolute path
    under files/, so names never collide and restoring is one cp each."""
    return os.path.join(dest_dir, "files", src.lstrip("/"))


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


def live_disks(xml_text: str) -> list[dict[str, Any]]:
    """[{target, device, type, source, backing}] of a definition's disks;
    backing: the live definition shows a backing chain (an overlay)."""
    out: list[dict[str, Any]] = []
    for disk in ET.fromstring(xml_text).findall("devices/disk"):
        t, s, bs = disk.find("target"), disk.find("source"), disk.find("backingStore")
        src = ""
        if s is not None:
            src = s.get("file") or s.get("dev") or s.get("name") or s.get("volume") or ""
        out.append(
            {
                "target": (t.get("dev") if t is not None else None) or "",
                "device": disk.get("device", "disk"),
                "type": disk.get("type", "file"),
                "source": src,
                "backing": bs is not None and bs.find("source") is not None,
            }
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


def overlay_kind(state: str | None, live_src: str | None, base: str) -> str:
    """How a disk left on an overlay is put back by hand:
    live: QEMU still runs on the overlay (running, paused, pmsuspended);
    definition: the running VM is back on the image, only its persistent
    definition names the overlay, which is now stale: never commit it;
    offline: shut off before any merge (this run knows none happened), so
    the overlay holds the guest's last writes;
    unknown: the state could not be read."""
    if state in LIVE_STATES:
        return "definition" if live_src == base else "live"
    if state in ("shut off", "crashed"):
        return "offline"
    return "unknown"


def overlay_recovery(vm: str, items: Iterable[tuple[str, tuple[str, str]]], how: Mapping[str, str]) -> list[str]:
    """The commands that put a disk back on its image by hand."""
    q = shlex.quote
    v = f"sudo virsh -c {URI}"
    lines: list[str] = []
    for t, (base, overlay) in items:
        kind = how.get(t, "unknown")
        if kind == "live":
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
        elif kind == "definition":
            lines += [
                f"# {t}: the VM is back on {base}; only its definition still names {overlay}, which is "
                "stale now: do NOT qemu-img commit it (that would overwrite newer data in the image)",
                f"{v} edit {q(vm)}     # disk {t}: <source file='{base}'/>, and remove its <backingStore> element",
                f"{v} domblklist {q(vm)} --details --inactive     # {t} must show {base}",
                f"sudo rm {q(overlay)}     # only once it shows {base}",
            ]
        elif kind == "offline":
            lines += [
                f"# {t}: {vm} is shut off on {overlay}, which holds the guest's last writes: merge it "
                "offline, then point the definition back",
                f"sudo qemu-img commit -p {q(overlay)}     # writes its changes into {base}",
                f"{v} edit {q(vm)}     # disk {t}: <source file='{base}'/>, and remove its <backingStore> element",
                f"{v} domblklist {q(vm)} --details --inactive     # {t} must show {base}",
                f"sudo rm {q(overlay)}",
            ]
        else:
            lines += [
                f"# {t}: the VM's state or the files could not be read: look first, then follow "
                'vm/README.md in CST, "If a backup left an overlay"',
                f"{v} domstate {q(vm)}",
                f"{v} domblklist {q(vm)} --details",
                f"{v} domblklist {q(vm)} --details --inactive",
                f"sudo ls -l --time-style=full-iso {q(base)} {q(overlay)}",
            ]
    return lines


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


def convert_lines(img: Mapping[str, Any], system: bool) -> list[str]:
    """The commands that make an existing image NOCOW (never run by CST)."""
    q = shlex.quote
    s = "sudo " if system else ""
    f, old = q(img["path"]), q(img["path"] + ".cow-old")
    return [
        f"{s}mv {f} {old}",
        f"{s}install -m 0600 /dev/null {f}      # new empty file, private until cp -a sets the mode",
        f"{s}chattr +C {f}",
        f"{s}cp -a --reflink=never --sparse=auto {old} {f}",
        f"{s}cmp {old} {f} && echo same",
        f"# start the VM; after a good boot: {s}rm {old}",
    ]


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


def parse_args(argv: Sequence[str]) -> dict[str, Any]:
    """{cmd, sub, vm, full, guest_virtio, root, nas, keep, no_quiesce}."""
    a: dict[str, Any] = {
        "cmd": "help",
        "sub": None,
        "vm": None,
        "full": False,
        "guest_virtio": False,
        "root": False,
        "nas": False,
        "keep": None,
        "no_quiesce": False,
    }
    if not argv or argv[0] in ("help", "-h", "--help"):
        if len(argv) > 1:
            raise Usage("help takes no arguments")
        return a
    a["cmd"], rest = argv[0], list(argv[1:])
    if a["cmd"] not in ("backup", "backup-timer", "list", "status", "tune", "nocow"):
        raise Usage(f"unknown command '{a['cmd']}'")
    subs: dict[str, tuple[str, ...]] = {
        "tune": ("plan", "apply"),
        "nocow": ("plan", "apply"),
        "backup-timer": ("on", "off", "status"),
    }
    if a["cmd"] in subs:
        if not rest or rest[0] not in subs[a["cmd"]]:
            raise Usage(f"{a['cmd']}: {' or '.join(subs[a['cmd']])}")
        a["sub"], rest = rest[0], rest[1:]
    flags = {
        "backup": {"--full": "full", "--nas": "nas", "--no-quiesce": "no_quiesce"},
        "tune": {"--guest-has-virtio": "guest_virtio"},
        "nocow": {"--root": "root"} if a["sub"] == "plan" else {},
    }.get(a["cmd"], {})
    takes_vm = a["cmd"] != "nocow"
    where = " ".join(filter(None, (a["cmd"], a["sub"])))
    while rest:
        x = rest.pop(0)
        if a["cmd"] == "backup" and (x == "--keep-tag" or x.startswith("--keep-tag=")) and a["keep"] is None:
            value = x.split("=", 1)[1] if "=" in x else (rest.pop(0) if rest else "")
            if not valid_keep(value):
                raise Usage(
                    f"--keep-tag {value!r}: a name of letters, digits and . _ - (up to 64, not "
                    "starting with cst- or vm:), e.g. pre-f46"
                )
            a["keep"] = value
        elif x in flags and not a[flags[x]]:
            a[flags[x]] = True
        elif takes_vm and not x.startswith("-") and a["vm"] is None:
            a["vm"] = x
        else:
            raise Usage(f"{where}: unexpected argument '{x}'")
    if a["full"] and a["nas"]:
        raise Usage("backup: --full is for the local copy, --nas for the NAS: one of them")
    if (a["keep"] is not None or a["no_quiesce"]) and not a["nas"]:
        raise Usage("backup: --keep-tag and --no-quiesce go with --nas")
    return a
