"""Unit tests for the vm module: vm/cst-vm (what a domain definition is
made of, backup with its refusals, the manifest and the world-readable
index, list, status) and the shell frontend vm-setup.sh.

Nothing reaches the real host: virsh, cp, rpm and rpm-ostree are stubs in a
scratch bin folder first in PATH (cp hands the copy to the real cp without
--reflink, since scratch folders are rarely btrfs), the backup folder, the
index and the swtpm folder are scratch paths (CST_VM_*), and backup runs as
the user (CST_VM_NONROOT_OK).

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s vm/tests -t . -v
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from types import ModuleType
from typing import Protocol, TypedDict, cast
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from cst_core.domain.dispatch import MODULE_BY_NAME
from vm.adapters import CstVmRuntimeAdapter, SubprocessVirshAdapter
from tests.script_loader import load_script
from vm.application import VmService
from vm.domain import (
    Usage,
    VmKnobs,
    extract_domain_parts,
    forget_args,
    overlay_kind,
    parse_vm_args,
    tune_domain_xml,
    valid_keep,
    version_changes,
)

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
VM_DIR = os.path.dirname(HERE)
TOOL = os.path.join(VM_DIR, "cst-vm")
REPO = os.path.dirname(VM_DIR)
SETUP = os.path.join(REPO, "vm-setup.sh")
UUID = "ff2515e6-3708-42be-be94-d4356f12bfc2"
NOW = 1794100000


class _DomainParts(TypedDict):
    uuid: str
    disks: list[str]
    nvram: str
    tpm: bool
    problems: list[str]
    skipped: list[str]


class _ParsedArgs(TypedDict):
    cmd: str
    sub: str | None
    vm: str | None
    full: bool
    guest_virtio: bool
    root: bool
    nas: bool
    keep: str | None
    no_quiesce: bool


class _DirSpec(TypedDict):
    scope: str
    path: str
    what: str


class _ImageEntry(TypedDict):
    path: str
    name: str


class _ProbeResult(TypedDict):
    scope: str
    path: str
    what: str
    present: bool
    link: str | None
    nocow: bool | None
    listed: bool
    images: list[_ImageEntry]
    leftovers: list[str]


class _VmModule(Protocol):
    os: ModuleType
    subprocess: ModuleType
    Usage: type[Exception]
    Refused: type[Exception]
    BACKUP_DIR: str
    INDEX: str
    SWTPM_DIR: str
    SYSTEM_IMAGES: str
    CPU_CONF: str
    VM_PIN: str
    FS_NOCOW_FL: int
    FS_IOC_SETFLAGS: int

    def domain_parts(self, xml: str) -> _DomainParts: ...
    def parse_args(self, argv: Sequence[str]) -> _ParsedArgs: ...
    def version_changes(self, old: Mapping[str, str] | None, new: Mapping[str, str] | None) -> list[str]: ...
    def cpu_disable_list(self, path: str = ...) -> list[str]: ...
    def cmd_backup(self, vm: str, full: bool = False) -> int: ...
    def tune_domain(
        self, xml: str, kn: Mapping[str, bool] | None = None, allow_bus: bool = True
    ) -> tuple[str | None, list[str], list[str], list[str]]: ...
    def normalized(self, xml: str) -> str: ...
    def guest_kind(self, root: ET.Element) -> tuple[str, str]: ...
    def disk_name(self, i: int) -> str: ...
    def disk_index(self, name: str) -> int | None: ...
    def virsh(self, *args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]: ...
    def knobs(self, path: str | None = None) -> dict[str, bool]: ...
    def confirm(self, prompt: str) -> bool: ...
    def cmd_tune(self, vm: str, do_apply: bool = False, guest_virtio: bool = False) -> int: ...
    def is_btrfs(self, path: str) -> bool: ...
    def inode_flags(self, path: str) -> int | None: ...
    def user_home(self) -> str: ...
    def probe_dir(self, d: Mapping[str, object]) -> _ProbeResult: ...
    def image_dirs(self) -> list[_DirSpec]: ...
    def cmd_nocow_plan(self, as_root: bool = False) -> int: ...
    def cmd_nocow_apply(self) -> int: ...
    def convert_lines(self, img: Mapping[str, object], as_root: bool) -> list[str]: ...
    def needs_mark(self, probe: Mapping[str, object]) -> bool: ...
    def set_nocow(self, path: str) -> None: ...
    def run(self, cmd: Sequence[str], timeout: int = 120) -> tuple[int, str, str]: ...
    def mark_system_dir(self, probe: Mapping[str, object]) -> str: ...
    def read_index(self, path: str | None = None) -> list[dict[str, object]]: ...
    def cmd_status(self, vm: str | None = None) -> int: ...
    def nocow(self, path: str) -> bool: ...
    def copy(self, src: str, dst: str, mode: str, nocow_dst: bool = False) -> None: ...


def load() -> _VmModule:
    mod = load_script(TOOL, "cst_vm")
    return cast("_VmModule", cast("object", mod))


VIRSH = """#!/bin/sh
echo "virsh $*" >>"$STUB_LOG"
echo "LC_ALL=$LC_ALL" >"$FIX/virsh-locale.txt"
for a in "$@"; do last="$a"; done
case " $* " in
  *" domstate "*) cat "$FIX/state.txt"; exit 0 ;;
  *" list --all --with-managed-save --name "*) cat "$FIX/managed.txt" 2>/dev/null; exit 0 ;;
  *" dumpxml "*) exec cat "$FIX/domain.xml" ;;
esac
exit 1
"""
CP = """#!/bin/sh
echo "cp $*" >>"$STUB_LOG"
if [ -n "$FIX_CP_FAIL" ]; then
  case " $* " in *" --reflink=always "*) echo "cp: failed to clone: Invalid cross-device link" >&2; exit 1 ;; esac
fi
if [ -n "$FIX_CP_STARTS_VM" ]; then
  echo "$FIX_CP_STARTS_VM" >"$FIX/state.txt"; touch -d '+1 min' "$FIX_CP_TOUCH"
fi
args=""
for a in "$@"; do
  case "$a" in --reflink=*|--sparse=*) ;; *) set -- "$@" "$a" ;; esac
  shift
done
exec /usr/bin/cp "$@"
"""
RPM = """#!/bin/sh
echo "rpm $*" >>"$STUB_LOG"
for a in "$@"; do last="$a"; done
v="$(sed -n "s/^$last=//p" "$FIX/rpm.txt")"
[ -n "$v" ] || { echo "package $last is not installed"; exit 1; }
echo "$v"
"""
RPM_OSTREE = """#!/bin/sh
echo "rpm-ostree $*" >>"$STUB_LOG"
[ -f "$FIX/status.json" ] && exec cat "$FIX/status.json"
exit 1
"""


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-vm-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.fix = os.path.join(self.tmp, "fix")
        self.images = os.path.join(self.tmp, "images")
        self.swtpm = os.path.join(self.tmp, "swtpm")
        self.backups = os.path.join(self.tmp, "cst-backups")
        self.index = os.path.join(self.tmp, "lib-cst", "vm-backups.json")
        for d in (self.bin, self.fix, self.images, os.path.join(self.swtpm, UUID, "tpm2")):
            os.makedirs(d)
        for name, body in (("virsh", VIRSH), ("cp", CP), ("rpm", RPM), ("rpm-ostree", RPM_OSTREE)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.disk = os.path.join(self.images, "win.qcow2")
        with open(self.disk, "wb") as f:
            f.write(b"QFI\xfb" + b"\0" * 12 + b"data")
        self.nvram = os.path.join(self.images, "win_VARS.qcow2")
        with open(self.nvram, "wb") as f:
            f.write(b"vars")
        with open(os.path.join(self.swtpm, UUID, "tpm2", "tpm2-00.permall"), "wb") as f:
            f.write(b"tpm")
        self.state("shut off")
        self.domain(self.domain_xml())
        with open(os.path.join(self.fix, "rpm.txt"), "w") as f:
            f.write("qemu-kvm=qemu-kvm-11.1.3-1.fc45.x86_64\nlibvirt-daemon=libvirt-daemon-12.6.0-2.fc45.x86_64\n")
        with open(os.path.join(self.fix, "status.json"), "w") as f:
            json.dump(
                {
                    "deployments": [
                        {"booted": False, "checksum": "s" * 64},
                        {"booted": True, "checksum": "b" * 64, "version": "45.1", "timestamp": NOW - 86400},
                    ]
                },
                f,
            )
        self.log = os.path.join(self.tmp, "stub.log")
        self.env: dict[str, str] = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "STUB_LOG": self.log,
            "FIX": self.fix,
            "CST_VM_NONROOT_OK": "1",
            "CST_VM_NOW": str(NOW),
            "CST_VM_BACKUP_DIR": self.backups,
            "CST_VM_INDEX": self.index,
            "CST_VM_SWTPM_DIR": self.swtpm,
            "CST_PROG": "cst vm",
            "CST_VM_CPU_CONF": os.path.join(self.tmp, "cpu-setup.conf"),
        }

    def state(self, s: str) -> None:
        with open(os.path.join(self.fix, "state.txt"), "w") as f:
            f.write(s + "\n\n")

    def domain_xml(self, extra_disk: str = "", cpu: str = "<cpu mode='host-passthrough'/>") -> str:
        return f"""<domain type='kvm'>
  <name>win</name><uuid>{UUID}</uuid>
  <os firmware='efi'><nvram template='/usr/share/x.qcow2' format='qcow2'>{self.nvram}</nvram></os>
  {cpu}
  <devices>
    <disk type='file' device='disk'><source file='{self.disk}'/><target dev='sda'/></disk>
    <disk type='file' device='cdrom'><source file='/iso/virtio.iso'/><target dev='sdb'/></disk>
    {extra_disk}
    <graphics type='spice' passwd='s3cret'/>
    <tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>
  </devices>
</domain>
"""

    def domain(self, xml: str) -> None:
        with open(os.path.join(self.fix, "domain.xml"), "w") as f:
            f.write(xml)

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", "-I", TOOL, *args], check=False, env=self.env, capture_output=True, text=True, timeout=60
        )

    def calls(self) -> list[str]:
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def dest(self) -> str:
        return os.path.join(self.backups, "win", "20261108T010640Z")


class PartsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m: _VmModule = load()

    def test_disks_nvram_tpm(self) -> None:
        xml = f"""<domain><uuid>{UUID}</uuid><os><nvram><source file='/n/VARS.fd'/></nvram></os><devices>
          <disk type='file' device='disk'><source file='/a.qcow2'/><target dev='vda'/></disk>
          <disk type='file'><source file='/b.raw'/><target dev='vdb'/></disk>
          <disk type='file' device='cdrom'><target dev='sdc'/></disk>
          <tpm><backend type='emulator'/></tpm></devices></domain>"""
        p = self.m.domain_parts(xml)
        self.assertEqual(p["uuid"], UUID)
        self.assertEqual(p["disks"], ["/a.qcow2", "/b.raw"])
        self.assertEqual(p["nvram"], "/n/VARS.fd")
        self.assertTrue(p["tpm"])
        self.assertEqual(p["problems"], [])
        self.assertEqual(p["skipped"], ["sdc (cdrom, not copied)"])

    def test_block_and_network_disks_are_problems(self) -> None:
        xml = """<domain><devices>
          <disk type='block' device='disk'><source dev='/dev/vg/win'/><target dev='vda'/></disk>
          <disk type='network' device='disk'><source protocol='rbd' name='pool/img'/><target dev='vdb'/></disk>
          <tpm><backend type='passthrough'/></tpm></devices></domain>"""
        p = self.m.domain_parts(xml)
        self.assertEqual(len(p["problems"]), 2)
        self.assertIn("vda: a block disk (/dev/vg/win)", p["problems"][0])
        self.assertIn("vdb: a network disk (pool/img)", p["problems"][1])
        self.assertFalse(p["tpm"])
        self.assertEqual(p["nvram"], "")

    def test_parse_args(self) -> None:
        def short(argv: Sequence[str]) -> tuple[str, str | None, str | None, bool, bool, bool]:
            a = self.m.parse_args(argv)
            return a["cmd"], a["sub"], a["vm"], a["full"], a["guest_virtio"], a["root"]

        self.assertEqual(short(["backup", "x", "--full"]), ("backup", None, "x", True, False, False))
        self.assertEqual(short(["list"]), ("list", None, None, False, False, False))
        self.assertEqual(short([]), ("help", None, None, False, False, False))
        self.assertEqual(short(["tune", "plan", "w", "--guest-has-virtio"]), ("tune", "plan", "w", False, True, False))
        self.assertEqual(short(["nocow", "plan", "--root"]), ("nocow", "plan", None, False, False, True))
        self.assertEqual(short(["nocow", "apply"]), ("nocow", "apply", None, False, False, False))
        for bad in (
            ["status", "--full"],
            ["backup", "a", "b"],
            ["prune"],
            ["backup", "--full", "--full"],
            ["tune"],
            ["tune", "go"],
            ["tune", "plan", "--full"],
            ["nocow", "plan", "vm"],
            ["nocow", "apply", "--root"],
            ["backup", "--guest-has-virtio"],
        ):
            with self.assertRaises(self.m.Usage):
                self.m.parse_args(bad)

    def test_version_changes(self) -> None:
        self.assertEqual(
            self.m.version_changes({"qemu-kvm": "a", "swtpm": "x"}, {"qemu-kvm": "b", "swtpm": "x"}),
            ["qemu-kvm: a -> b"],
        )

    def test_cpu_disable_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "c.conf")
            self.assertEqual(self.m.cpu_disable_list(conf)[0], "cet-ss")
            with open(conf, "w") as f:
                f.write("VM_CPU_DISABLE=''\n")
            self.assertEqual(self.m.cpu_disable_list(conf), [])


class BackupTest(Scratch):
    def test_reflink_backup(self) -> None:
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = self.dest()
        for d in (self.backups, os.path.dirname(dest), dest):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        with open(os.path.join(dest, "files", self.disk.lstrip("/")), "rb") as f:
            self.assertTrue(f.read().endswith(b"data"))
        self.assertTrue(os.path.isfile(os.path.join(dest, "files", self.nvram.lstrip("/"))))
        self.assertTrue(
            os.path.isfile(os.path.join(dest, "files", self.swtpm.lstrip("/"), UUID, "tpm2", "tpm2-00.permall"))
        )
        xml = os.path.join(dest, "domain.xml")
        self.assertEqual(stat.S_IMODE(os.stat(xml).st_mode), 0o600)
        self.assertIn("s3cret", open(xml).read())
        man = json.load(open(os.path.join(dest, "manifest.json")))
        self.assertEqual((man["vm"], man["uuid"], man["mode"], man["epoch"]), ("win", UUID, "reflink", NOW))
        self.assertEqual([f["kind"] for f in man["files"]], ["disk", "nvram", "tpm"])
        self.assertEqual(man["versions"]["qemu-kvm"], "qemu-kvm-11.1.3-1.fc45.x86_64")
        self.assertEqual(man["versions"]["swtpm"], "not installed")
        self.assertEqual(man["deployment"], {"version": "45.1", "checksum": "b" * 64, "timestamp": NOW - 86400})
        self.assertIn("sdb (cdrom, not copied)", man["notes"])
        # The index: world-readable, without secrets.
        self.assertEqual(stat.S_IMODE(os.stat(self.index).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.index)).st_mode), 0o755)
        text = open(self.index).read()
        self.assertNotIn("s3cret", text)
        idx = json.load(open(self.index))["backups"]
        self.assertEqual(len(idx), 1)
        self.assertEqual((idx[0]["path"], idx[0]["epoch"], idx[0]["mode"]), (dest, NOW, "reflink"))
        cps = [c for c in self.calls() if c.startswith("cp ")]
        self.assertTrue(cps[0].startswith(f"cp --reflink=always -a -- {self.disk} "), cps)
        self.assertTrue(cps[1].startswith(f"cp -a -- {self.nvram} "), cps)
        self.assertIn("virsh -c qemu:///system dumpxml --inactive --security-info win", self.calls())
        self.assertIn(f"sudo virsh -c qemu:///system define {dest}/domain.xml", r.stdout)
        self.assertIn(f"sudo cp --reflink=auto -a {dest}/files{self.disk} {self.disk}", r.stdout)
        self.assertIn("not against losing the disks", r.stdout)
        self.assertNotIn("s3cret", r.stdout + r.stderr)

    def test_index_drops_removed_backups(self) -> None:
        os.makedirs(os.path.dirname(self.index))
        kept = os.path.join(self.tmp, "kept")
        os.makedirs(kept)
        with open(self.index, "w") as f:
            json.dump(
                {
                    "backups": [
                        {"vm": "win", "path": kept, "epoch": 1, "timestamp": "a"},
                        {"vm": "win", "path": os.path.join(self.tmp, "gone"), "epoch": 2, "timestamp": "b"},
                    ]
                },
                f,
            )
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        paths = [e["path"] for e in json.load(open(self.index))["backups"]]
        self.assertEqual(paths, [kept, self.dest()])

    def test_full_backup_flags(self) -> None:
        r = self.run_tool("backup", "win", "--full")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cps = [c for c in self.calls() if c.startswith("cp ")]
        self.assertTrue(cps[0].startswith("cp --reflink=never --sparse=auto -a -- "), cps)
        self.assertTrue(cps[1].startswith(f"cp --reflink=never -a -- {self.nvram} "), cps)
        self.assertTrue(cps[2].startswith("cp --reflink=never -a -- "), cps)
        self.assertEqual(json.load(open(os.path.join(self.dest(), "manifest.json")))["mode"], "full")

    def test_refuses_a_running_vm(self) -> None:
        self.state("running")
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("win is running: shut it down first", r.stderr)
        self.assertFalse(os.path.exists(self.backups))
        self.assertFalse([c for c in self.calls() if "dumpxml" in c])

    def test_refuses_block_disks(self) -> None:
        self.domain(
            self.domain_xml("<disk type='block' device='disk'><source dev='/dev/sdz'/><target dev='sdc'/></disk>")
        )
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sdc: a block disk (/dev/sdz): only file disks can be copied", r.stderr)
        self.assertFalse(os.path.exists(self.backups))

    def test_refuses_qcow2_overlays(self) -> None:
        with open(self.disk, "wb") as f:
            f.write(b"QFI\xfb\0\0\0\3" + struct.pack(">Q", 512))
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("qcow2 overlay with a backing file", r.stderr)

    def test_reflink_failure_hints_full_and_cleans_up(self) -> None:
        self.env["FIX_CP_FAIL"] = "1"
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Invalid cross-device link", r.stderr)
        self.assertIn("cst vm backup VM --full", r.stderr)
        self.assertIn("incomplete backup removed", r.stderr)
        self.assertFalse(os.path.exists(self.dest()))
        self.assertFalse(os.path.exists(self.index))

    def test_refuses_a_managed_saved_vm(self) -> None:
        with open(os.path.join(self.fix, "managed.txt"), "w") as f:
            f.write("other\nwin\n\n")
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("win was saved (managed save), not shut down", r.stderr)
        self.assertIn("managedsave-remove win", r.stderr)
        self.assertFalse(os.path.exists(self.backups))

    def test_virsh_answers_in_english(self) -> None:
        self.env["LC_ALL"] = "pt_BR.UTF-8"
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(os.path.join(self.fix, "virsh-locale.txt")).read().strip(), "LC_ALL=C")

    def test_vm_started_during_the_copy(self) -> None:
        for state, needle in (
            ("running", "state changed during the copy"),
            ("shut off", "changed during the copy (the VM was started?)"),
        ):
            with self.subTest(state=state):
                self.state("shut off")
                self.env.update(FIX_CP_STARTS_VM=state, FIX_CP_TOUCH=self.disk)
                r = self.run_tool("backup", "win")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(needle, r.stderr)
                self.assertIn("incomplete backup removed", r.stderr)
                self.assertFalse(os.path.exists(self.dest()))
                self.assertFalse(os.path.exists(self.index))

    def test_symlinked_disk_is_copied_with_its_data(self) -> None:
        real = os.path.join(self.tmp, "bulk", "win.qcow2")
        os.makedirs(os.path.dirname(real))
        os.replace(self.disk, real)
        os.symlink(real, self.disk)
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kept = os.path.join(self.dest(), "files", real.lstrip("/"))
        self.assertFalse(os.path.islink(kept))
        with open(kept, "rb") as f:
            self.assertTrue(f.read().endswith(b"data"))
        self.assertFalse(os.path.lexists(os.path.join(self.dest(), "files", self.disk.lstrip("/"))))
        self.assertIn(f"disk {self.disk} is a symbolic link", r.stdout)
        self.assertIn(f"{kept} {real}", r.stdout)  # restored to the file it points to
        man = json.load(open(os.path.join(self.dest(), "manifest.json")))
        self.assertEqual(man["files"][0]["source"], real)

    def test_missing_tpm_state_is_a_note(self) -> None:
        shutil.rmtree(os.path.join(self.swtpm, UUID))
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TPM state", r.stdout)
        self.assertIn("BitLocker", r.stdout)

    def test_needs_root(self) -> None:
        del self.env["CST_VM_NONROOT_OK"]
        if os.geteuid() == 0:
            self.skipTest("running as root")
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("backup needs root", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_full_refuses_without_space(self) -> None:
        m = load()
        m.BACKUP_DIR, m.INDEX, m.SWTPM_DIR = self.backups, self.index, self.swtpm
        fake = os.statvfs_result((4096, 4096, 100, 10, 10, 0, 0, 0, 0, 255))
        with mock.patch.dict(os.environ, self.env), mock.patch.object(m.os, "statvfs", return_value=fake):
            with self.assertRaises(m.Refused) as cm:
                m.cmd_backup("win", True)
        self.assertIn("--full needs", str(cm.exception))
        self.assertIn("allocated size", str(cm.exception))
        self.assertFalse(os.path.exists(self.backups))


class ListStatusTest(Scratch):
    def test_default_vm_from_cpu_setup_conf(self) -> None:
        with open(self.env["CST_VM_CPU_CONF"], "w") as f:
            f.write("VM_NAME='win'\n")
        r = self.run_tool("status")
        self.assertIn("win: shut off", r.stdout)
        self.assertIn("virsh -c qemu:///system domstate win", self.calls())

    def test_list_and_status(self) -> None:
        r = self.run_tool("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no VM backups", r.stdout)
        r = self.run_tool("status", "win")
        self.assertEqual(r.returncode, 0)
        self.assertIn("win: shut off", r.stdout)
        self.assertIn("NOT disabled: cet-ss cet-ibt", r.stdout)
        self.assertIn("last backup: none", r.stdout)
        self.assertEqual(self.run_tool("backup", "win").returncode, 0)
        r = self.run_tool("list")
        self.assertIn("win              20261108T010640Z  reflink  deployment 45.1  qemu-kvm-11.1.3", r.stdout)
        with open(os.path.join(self.fix, "rpm.txt"), "w") as f:
            f.write("qemu-kvm=qemu-kvm-11.2.0-1.fc45.x86_64\nlibvirt-daemon=libvirt-daemon-12.6.0-2.fc45.x86_64\n")
        r = self.run_tool("status", "win")
        self.assertIn("qemu-kvm: qemu-kvm-11.1.3-1.fc45.x86_64 -> qemu-kvm-11.2.0-1.fc45.x86_64", r.stdout)
        self.assertNotIn("libvirt-daemon:", r.stdout)

    def test_status_with_fixed_cpu_and_without_virsh(self) -> None:
        self.domain(
            self.domain_xml(
                cpu="<cpu mode='host-passthrough'>"
                + "".join(
                    f"<feature policy='disable' name='{n}'/>"
                    for n in ("cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet")
                )
                + "</cpu>"
            )
        )
        r = self.run_tool("status", "win")
        self.assertIn("cpu mode host-passthrough: disabled cet-ss", r.stdout)
        self.assertNotIn("--security-info", "\n".join(self.calls()))
        os.unlink(os.path.join(self.bin, "virsh"))
        self.env["PATH"] = f"{self.bin}"
        r = subprocess.run(
            [sys.executable, "-I", TOOL, "status", "win"],
            check=False,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("virsh not found", r.stdout)

    def test_usage(self) -> None:
        r = self.run_tool("bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        r = self.run_tool("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("reflink", r.stdout)


# ---- tune: the recommended virtual hardware ------------------------------------------------------
FIXTURES = os.path.join(HERE, "fixtures")
ALL_ON = {"VM_NOCOW": True, "VM_TUNE_DISK_BUS": True, "VM_TUNE_DISK_DRIVER": True, "VM_TUNE_NIC": True}


def real_domain() -> str:
    """win11-devbox as migrated by hand (2026-11-14): virtio-scsi, cache='none'..., virtio NIC."""
    with open(os.path.join(FIXTURES, "win11-devbox.xml")) as f:
        return f.read()


def old_domain(text: str | None = None) -> str:
    """The same VM before the migration: disk on SATA, plain driver, e1000e,
    no virtio-scsi controller."""
    x = text or real_domain()
    x = x.replace(
        "<driver name='qemu' type='qcow2' cache='none' io='native' discard='unmap' detect_zeroes='unmap'/>",
        "<driver name='qemu' type='qcow2'/>",
    )
    x = x.replace("<target dev='sda' bus='scsi'/>", "<target dev='sda' bus='sata'/>")
    start = x.index("<controller type='scsi'")
    end = x.index("</controller>", start) + len("</controller>")
    x = x[:start] + x[end:]
    x = x.replace(
        "<model type='virtio'/>\n      <address type='pci' domain='0x0000' bus='0x01'",
        "<model type='e1000e'/>\n      <address type='pci' domain='0x0000' bus='0x01'",
    )
    assert "bus='sata'/>\n      <address type='drive' controller='0' bus='0' target='0' unit='0'/>" in x
    return x


def with_devices(devices: str, extra: str = "") -> str:
    return (
        f"<domain type='kvm'><name>t</name>{extra}<devices>{devices}<memballoon model='virtio'/></devices></domain>\n"
    )


def _child(el: ET.Element, tag: str) -> ET.Element:
    child = el.find(tag)
    assert child is not None
    return child


class TuneTransformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m: _VmModule = load()

    def tune(
        self, xml: str, allow: bool = True, kn: Mapping[str, bool] | None = None
    ) -> tuple[str | None, list[str], list[str], list[str]]:
        return self.m.tune_domain(xml, kn or ALL_ON, allow)

    def disks(self, xml: str) -> list[ET.Element]:
        return ET.fromstring(xml).findall("devices/disk")

    def test_hand_migrated_vm_has_no_diff(self) -> None:
        after: str | None = None
        for allow in (True, False):
            after, changes, held, notes = self.tune(real_domain(), allow)
            self.assertEqual((changes, held, notes), ([], [], []))
            self.assertEqual(after, self.m.normalized(real_domain()))
        assert after is not None
        self.assertIn("libosinfo:os id=", after)  # prefixes kept, no ns0
        self.assertNotIn("ns0", after)

    def test_sata_to_virtio_scsi_without_a_controller(self) -> None:
        after, changes, held, _ = self.tune(old_domain())
        assert after is not None
        self.assertEqual(held, [])
        self.assertIn("add a virtio-scsi controller (index 0)", changes)
        self.assertIn("disk sda: bus sata -> scsi (virtio-scsi controller 0, unit 0)", changes)
        self.assertIn("disk sda: cache (default) -> none", changes)
        self.assertIn("network card 52:54:00:48:14:b1: e1000e -> virtio (same MAC)", changes)
        disk, cdrom = self.disks(after)
        self.assertEqual(_child(disk, "target").attrib, {"dev": "sda", "bus": "scsi"})
        self.assertEqual(
            _child(disk, "address").attrib,
            {"type": "drive", "controller": "0", "bus": "0", "target": "0", "unit": "0"},
        )
        self.assertEqual(
            _child(disk, "driver").attrib,
            {
                "name": "qemu",
                "type": "qcow2",
                "cache": "none",
                "io": "native",
                "discard": "unmap",
                "detect_zeroes": "unmap",
            },
        )
        # The CD-ROM stays on SATA, address and all.
        self.assertEqual(_child(cdrom, "target").attrib, {"dev": "sdb", "bus": "sata"})
        self.assertEqual(_child(cdrom, "address").get("unit"), "1")
        self.assertEqual(_child(cdrom, "driver").attrib, {"name": "qemu", "type": "raw"})
        self.assertIn("<mac address='52:54:00:48:14:b1' />", after.replace('"', "'"))
        self.assertEqual(after.count('model="virtio-scsi"'), 1)
        # Idempotent: a second pass changes nothing.
        again, changes2, _, _ = self.tune(after)
        self.assertEqual(changes2, [])
        self.assertEqual(again, after)

    def test_windows_without_drivers_gets_only_the_driver_attributes(self) -> None:
        after, changes, held, _ = self.tune(old_domain(), allow=False)
        assert after is not None
        self.assertEqual(
            held, ["disk sda: bus sata -> scsi (virtio-scsi)", "network card 52:54:00:48:14:b1: e1000e -> virtio"]
        )
        self.assertTrue(all(c.startswith("disk sda: ") and " bus " not in c for c in changes), changes)
        self.assertNotIn("virtio-scsi", after)
        self.assertIn("e1000e", after)
        self.assertEqual(_child(self.disks(after)[0], "target").get("bus"), "sata")
        self.assertEqual(_child(self.disks(after)[0], "driver").get("cache"), "none")

    def test_existing_lsilogic_controller_gets_a_new_virtio_scsi_one(self) -> None:
        xml = with_devices(
            "<disk type='file' device='disk'><source file='/a'/><target dev='sda' bus='sata'/>"
            "<address type='drive' controller='0' bus='0' target='0' unit='0'/></disk>"
            "<disk type='file' device='disk'><source file='/b'/><target dev='sdb' bus='scsi'/>"
            "<address type='drive' controller='0' bus='0' target='0' unit='0'/></disk>"
            "<controller type='scsi' index='0' model='lsilogic'/>"
        )
        after, changes, _, _ = self.tune(xml)
        assert after is not None
        self.assertIn("add a virtio-scsi controller (index 1)", changes)
        a, b = self.disks(after)
        self.assertEqual(_child(a, "address").get("controller"), "1")
        self.assertEqual(_child(a, "address").get("unit"), "0")
        self.assertEqual(_child(b, "address").get("controller"), "0")  # the lsilogic disk is not moved

    def test_multiple_disks_units_and_ide_rename(self) -> None:
        xml = with_devices(
            "<disk type='file' device='disk'><source file='/a'/><target dev='sda' bus='sata'/></disk>"
            "<disk type='block' device='disk'><source dev='/dev/x'/><target dev='sdb' bus='sata'/></disk>"
            "<disk type='file' device='disk'><source file='/c'/><target dev='hda' bus='ide'/></disk>"
            "<disk type='network' device='disk'><source protocol='nbd' name='n'/>"
            "<target dev='sdd' bus='sata'/></disk>"
            "<disk type='file' device='cdrom'><target dev='sde' bus='sata'/></disk>"
            "<controller type='scsi' index='0' model='virtio-scsi'/>"
        )
        after, changes, _, notes = self.tune(xml)
        assert after is not None
        units = [
            (
                _child(d, "target").get("dev"),
                _child(d, "target").get("bus"),
                addr.get("unit") if (addr := d.find("address")) is not None else None,
            )
            for d in self.disks(after)
        ]
        self.assertEqual(
            units,
            [
                ("sda", "scsi", "0"),
                ("sdb", "scsi", "1"),
                ("sdc", "scsi", "2"),
                ("sdd", "sata", None),
                ("sde", "sata", None),
            ],
        )
        self.assertIn("disk hda: bus ide -> scsi (virtio-scsi controller 0, unit 2), renamed hda -> sdc", changes)
        self.assertNotIn("add a virtio-scsi controller (index 0)", changes)
        self.assertEqual(notes[0], "disk sdd: a network disk: left as it is (only file and block disks are tuned)")
        self.assertIn("disk sdb: io (default) -> native", changes)  # block disks: tuned too

    def test_driver_attributes_merge_and_missing_driver(self) -> None:
        xml = with_devices(
            "<disk type='file' device='disk'><driver name='qemu' type='raw' cache='writeback' "
            "error_policy='stop' discard='unmap'/><source file='/a'/><target dev='vda' bus='virtio'/>"
            "</disk><disk type='file' device='disk'><source file='/b'/>"
            "<target dev='vdb' bus='virtio'/></disk>"
        )
        after, changes, _, _ = self.tune(xml)
        assert after is not None
        a, b = self.disks(after)
        self.assertEqual(
            _child(a, "driver").attrib,
            {
                "name": "qemu",
                "type": "raw",
                "cache": "none",
                "error_policy": "stop",
                "discard": "unmap",
                "io": "native",
                "detect_zeroes": "unmap",
            },
        )
        self.assertEqual(
            _child(b, "driver").attrib,
            {"name": "qemu", "cache": "none", "io": "native", "discard": "unmap", "detect_zeroes": "unmap"},
        )
        self.assertEqual(list(b)[0].tag, "driver")
        self.assertIn("disk vda: cache writeback -> none", changes)
        self.assertNotIn("disk vda: discard", " ".join(changes))
        self.assertFalse(any(" bus " in c for c in changes))  # virtio-blk stays

    def test_knobs_skip_items(self) -> None:
        kn = dict(ALL_ON, VM_TUNE_DISK_BUS=False, VM_TUNE_NIC=False)
        _, changes, held, _ = self.tune(old_domain(), kn=kn)
        self.assertEqual(held, [])
        self.assertFalse(any(" bus " in c or "network" in c for c in changes))
        _, changes, _, _ = self.tune(old_domain(), kn=dict(ALL_ON, VM_TUNE_DISK_DRIVER=False))
        self.assertFalse(any("cache" in c for c in changes))
        self.assertTrue(any(" bus " in c for c in changes))

    def test_nic_notes_and_memballoon(self) -> None:
        xml = (
            "<domain><devices><interface type='network'><mac address='52:54:00:00:00:01'/>"
            "<model type='rtl8139'/></interface><interface type='bridge'><mac address='52:54:00:00:00:02'/>"
            "</interface><interface type='hostdev'><mac address='52:54:00:00:00:03'/>"
            "<model type='e1000e'/></interface></devices></domain>"
        )
        after, changes, _, notes = self.tune(xml)
        assert after is not None
        self.assertEqual(changes, ["network card 52:54:00:00:00:01: rtl8139 -> virtio (same MAC)"])
        self.assertIn("network card 52:54:00:00:00:02: no <model>", notes[0])
        self.assertIn("no <memballoon>: not added", notes[1])
        for mac in ("01", "02", "03"):
            self.assertIn(f"52:54:00:00:00:{mac}", after)
        self.assertIn("e1000e", after)  # hostdev: not an emulated card

    def test_guest_kind_and_disk_names(self) -> None:
        osinfo = (
            "<metadata><libosinfo:libosinfo xmlns:libosinfo='http://libosinfo.org/xmlns/libvirt/domain/1.0'>"
            "<libosinfo:os id='{}'/></libosinfo:libosinfo></metadata>"
        )

        def kind(extra: str) -> str:
            return self.m.guest_kind(ET.fromstring(f"<domain>{extra}</domain>"))[0]

        self.assertEqual(kind(osinfo.format("http://microsoft.com/win/11")), "windows")
        self.assertEqual(kind(osinfo.format("http://fedoraproject.org/fedora/43")), "linux")
        self.assertEqual(kind(osinfo.format("http://freebsd.org/freebsd/14.0")), "unknown")
        self.assertEqual(kind("<features><hyperv mode='custom'/></features>"), "windows")
        self.assertEqual(kind(""), "unknown")
        for i, n in ((0, "sda"), (25, "sdz"), (26, "sdaa")):
            self.assertEqual(self.m.disk_name(i), n)
            self.assertEqual(self.m.disk_index(n), i)
        self.assertEqual(self.m.disk_index("hdb"), 1)
        self.assertIsNone(self.m.disk_index("xvda"))


class TuneEdgeCaseTest(unittest.TestCase):
    """Cases found in review: default namespaces, SCSI host devices, socket NICs."""

    def setUp(self) -> None:
        self.m: _VmModule = load()

    def tune(self, xml: str, allow: bool = True) -> tuple[str | None, list[str], list[str], list[str]]:
        return self.m.tune_domain(xml, ALL_ON, allow)

    def test_default_namespace_in_metadata_stays_on_its_element(self) -> None:
        x = old_domain()
        x = x.replace("</metadata>", "  <app xmlns='http://example.com/app/1'><v>1</v></app>\n  </metadata>", 1)
        self.assertIn("example.com/app/1", x)
        after, changes, _, _ = self.tune(x)
        assert after is not None
        self.assertTrue(changes)
        root = ET.fromstring(after)
        self.assertEqual(root.tag, "domain")  # libvirt elements keep no namespace
        self.assertIsNotNone(root.find("devices/disk"))
        self.assertIsNotNone(root.find("metadata/{http://example.com/app/1}app/{http://example.com/app/1}v"))
        self.assertIn("libosinfo:os id=", after)
        self.assertEqual(ET.fromstring(self.m.normalized(x)).tag, "domain")
        again, changes2, _, _ = self.tune(after)
        self.assertEqual((changes2, again), ([], after))

    def test_a_scsi_hostdev_unit_is_not_reused(self) -> None:
        devs = (
            "<disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='/x.qcow2'/>"
            "<target dev='sda' bus='sata'/><address type='drive' controller='0' bus='0' target='0' unit='0'/>"
            "</disk><controller type='scsi' index='0' model='virtio-scsi'/>"
            "<hostdev mode='subsystem' type='scsi'><source><adapter name='scsi_host0'/>"
            "<address bus='0' target='0' unit='0'/></source>"
            "<address type='drive' controller='0' bus='0' target='0' unit='0'/></hostdev>"
        )
        after, changes, _, _ = self.tune(with_devices(devs))
        assert after is not None
        self.assertIn("disk sda: bus sata -> scsi (virtio-scsi controller 0, unit 1)", changes)
        disk = ET.fromstring(after).find("devices/disk")
        assert disk is not None
        self.assertEqual(_child(disk, "address").get("unit"), "1")

    def test_socket_network_cards_are_tuned(self) -> None:
        for kind in ("mcast", "server", "client", "udp", "null"):
            devs = (
                f"<interface type='{kind}'><mac address='52:54:00:00:00:01'/><source address='230.0.0.1' "
                f"port='5558'/><model type='rtl8139'/></interface>"
            )
            after, changes, held, _ = self.tune(with_devices(devs))
            assert after is not None
            self.assertEqual(changes, ["network card 52:54:00:00:00:01: rtl8139 -> virtio (same MAC)"], kind)
            self.assertIn("52:54:00:00:00:01", after)
            _, _, held, _ = self.tune(with_devices(devs), allow=False)
            self.assertEqual(held, ["network card 52:54:00:00:00:01: rtl8139 -> virtio"], kind)
        devs = "<interface type='vhostuser'><mac address='52:54:00:00:00:02'/><model type='virtio'/></interface>"
        self.assertEqual(self.tune(with_devices(devs))[1], [])


class TuneCommandTest(unittest.TestCase):
    """cmd_tune with a fake virsh (in-process: apply asks on a terminal)."""

    def setUp(self) -> None:
        self.m: _VmModule = load()
        self.tmp = tempfile.mkdtemp(prefix="cst-vm-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = "shut off"
        self.calls: list[tuple[str, ...]] = []
        self.defined: list[str] = []
        self.managed = ""
        self.xml = old_domain()
        secret = self.xml.replace("<graphics type='spice'>", "<graphics type='spice' passwd='s3cret'>")
        self.secret = secret

        def virsh(*args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
            self.calls.append(args)
            if args[0] == "domstate":
                return 0, self.state + "\n", ""
            if args[0] == "dumpxml":
                return 0, (self.secret if "--security-info" in args else self.xml), ""
            if args[:3] == ("list", "--all", "--with-managed-save"):
                return 0, self.managed, ""
            if args[0] == "define":
                with open(args[1]) as f:
                    self.defined.append(f.read())
                self.xml = self.secret = self.defined[-1]
                return 0, "", ""
            return 1, "", "unsupported"

        def _knobs(path: str | None = None) -> dict[str, bool]:
            return dict(ALL_ON)

        patches = [
            mock.patch.object(self.m, "virsh", virsh),
            mock.patch.object(self.m, "BACKUP_DIR", self.tmp),
            mock.patch.object(self.m, "knobs", _knobs),
            mock.patch.object(self.m, "CPU_CONF", os.path.join(self.tmp, "none.conf")),
            mock.patch.object(self.m, "VM_PIN", os.path.join(self.tmp, "no-vm-pin")),
            mock.patch.dict(os.environ, {"CST_VM_NONROOT_OK": "1"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, apply: bool, flag: bool, answer: bool = True) -> tuple[int, str]:
        out = io.StringIO()

        def _confirm(q: str) -> bool:
            return answer

        with mock.patch.object(self.m, "confirm", _confirm), mock.patch("sys.stdout", out):
            rc = self.m.cmd_tune("win11-devbox", apply, flag)
        return rc, out.getvalue()

    def test_plan_windows_without_flag(self) -> None:
        rc, out = self.run_cmd(False, False)
        self.assertEqual(rc, 0)
        self.assertIn("guest: Windows (libosinfo http://microsoft.com/win/11)", out)
        self.assertIn("held back: disk sda: bus sata -> scsi", out)
        self.assertIn("pnputil /add-driver D:\\vioscsi\\w11\\amd64\\vioscsi.inf /install", out)
        self.assertIn("sc qc vioscsi", out)
        self.assertIn("cst vm tune apply win11-devbox --guest-has-virtio", out)
        self.assertIn('+      <driver name="qemu" type="qcow2" cache="none"', out)
        self.assertIn("CPU (owned by cst cpu vm-pin, not by tune): CET/MBEC disabled; vCPUs pinned to 12-23", out)
        self.assertEqual(self.defined, [])
        self.assertFalse(any("--security-info" in c for c in self.calls))
        self.assertNotIn("s3cret", out)

    def test_apply_with_flag_refuses_a_running_windows(self) -> None:
        self.state = "running"
        with self.assertRaises(self.m.Refused) as e:
            self.run_cmd(True, True)
        self.assertIn("only with the VM shut off", str(e.exception))
        self.assertEqual(self.defined, [])
        # Driver attributes alone may be defined while it runs (effective at the next start).
        rc, out = self.run_cmd(True, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.defined), 1)
        self.assertIn("Effective at the next start", out)

    def test_apply_with_flag_refuses_a_managed_saved_windows(self) -> None:
        self.managed = "win11-devbox\n"
        with self.assertRaises(self.m.Refused) as e:
            self.run_cmd(True, True)
        self.assertIn("was saved (managed save), not shut down", str(e.exception))
        self.assertIn("managedsave-remove win11-devbox", str(e.exception))
        self.assertEqual(self.defined, [])
        rc, out = self.run_cmd(False, True)  # the plan says so instead of failing
        self.assertEqual(rc, 0)
        self.assertIn("was saved (managed save)", out)
        # Without the flag only the driver attributes change: no reason to refuse.
        rc, out = self.run_cmd(True, False)
        self.assertEqual((rc, len(self.defined)), (0, 1))

    def test_windows_steps_ask_for_a_full_shutdown(self) -> None:
        rc, out = self.run_cmd(False, False)
        self.assertIn("shutdown /s /t 0", out)
        self.assertIn("Fast Startup", out)

    def test_apply_with_flag_defines_and_keeps_the_old_definition(self) -> None:
        rc, out = self.run_cmd(True, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.defined), 1)
        new = self.defined[0]
        self.assertIn('passwd="s3cret"', new)  # defined from the --security-info dump
        self.assertIn('model="virtio-scsi"', new)
        self.assertIn("52:54:00:48:14:b1", new)
        self.assertNotIn("s3cret", out)
        keep = os.path.join(self.tmp, "win11-devbox", "definitions")
        saved = [os.path.join(keep, n) for n in os.listdir(keep)]
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].endswith(".before-tune.xml"))
        self.assertEqual(stat.S_IMODE(os.stat(saved[0]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(keep).st_mode), 0o700)
        with open(saved[0]) as f:
            self.assertIn("bus='sata'", f.read())
        self.assertNotIn("still different", out)
        rc, out = self.run_cmd(False, True)
        self.assertIn("already as cst vm tune recommends", out)

    def test_no_answer_changes_nothing(self) -> None:
        rc, out = self.run_cmd(True, True, answer=False)
        self.assertEqual(rc, 0)
        self.assertIn("Nothing changed.", out)
        self.assertEqual(self.defined, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "win11-devbox")))

    def test_hand_migrated_plan_is_clean(self) -> None:
        self.xml = self.secret = real_domain()
        rc, out = self.run_cmd(False, False)
        self.assertEqual(rc, 0)
        self.assertIn("win11-devbox: disks and network card already as cst vm tune recommends.", out)
        self.assertNotIn("held back", out)

    def test_apply_needs_root(self) -> None:
        def _uid() -> int:
            return 1000

        with (
            mock.patch.dict(os.environ, {"CST_VM_NONROOT_OK": ""}),
            mock.patch.object(self.m.os, "geteuid", _uid),
        ):
            with self.assertRaises(self.m.Refused):
                self.run_cmd(True, True)


# ---- NOCOW of the VM image folders ----------------------------------------------------------------
class NocowTest(unittest.TestCase):
    """The probe with fake inode flags (scratch folders are rarely btrfs)."""

    def setUp(self) -> None:
        self.m: _VmModule = load()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-vm-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.system = os.path.join(self.tmp, "var", "lib", "libvirt", "images")
        self.boxes = os.path.join(self.home, ".local", "share", "gnome-boxes", "images")
        for d in (self.system, self.boxes):
            os.makedirs(d)
        self.flags: dict[str, int | None] = {}  # path -> flags (None: not readable); missing: 0 (copy-on-write)

        def _is_btrfs(p: str) -> bool:
            return True

        def _inode_flags(p: str) -> int | None:
            return self.flags.get(p, 0)

        def _user_home() -> str:
            return self.home

        def _knobs(path: str | None = None) -> dict[str, bool]:
            return dict(ALL_ON)

        patches = [
            mock.patch.object(self.m, "SYSTEM_IMAGES", self.system),
            mock.patch.object(self.m, "is_btrfs", _is_btrfs),
            mock.patch.object(self.m, "inode_flags", _inode_flags),
            mock.patch.object(self.m, "user_home", _user_home),
            mock.patch.object(self.m, "knobs", _knobs),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def file(self, d: str, name: str, size: int = 4096) -> str:
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def dirs(self) -> dict[str, _ProbeResult]:
        return {d["path"]: self.m.probe_dir(d) for d in self.m.image_dirs()}

    def out(self, fn: Callable[..., int], *args: object) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = fn(*args)
        return rc, buf.getvalue()

    def test_probe_and_plan(self) -> None:
        win = self.file(self.system, "win.qcow2")
        self.file(self.system, "virtio-win.iso")
        self.file(self.system, "old.qcow2.cow-old")
        self.flags[self.system] = self.m.FS_NOCOW_FL
        self.flags[self.boxes] = 0
        good = self.file(self.boxes, "fedora")
        self.flags[good] = self.m.FS_NOCOW_FL
        d = self.dirs()
        s = d[self.system]
        self.assertEqual((s["present"], s["nocow"], s["listed"]), (True, True, True))
        self.assertEqual([i["name"] for i in s["images"]], ["win.qcow2"])
        self.assertEqual(s["leftovers"], ["old.qcow2.cow-old"])
        self.assertFalse(d[os.path.join(self.home, ".local/share/libvirt/images")]["present"])
        rc, out = self.out(self.m.cmd_nocow_plan)
        self.assertEqual(rc, 0)
        self.assertIn("still copy-on-write: win.qcow2", out)
        self.assertIn(f"sudo mv {win} {win}.cow-old", out)
        self.assertIn(f"sudo cp -a --reflink=never --sparse=auto {win}.cow-old {win}", out)
        self.assertIn("leftover of a conversion: old.qcow2.cow-old", out)
        self.assertIn(f"{self.boxes}  [GNOME Boxes (RPM)]\n  NOCOW mark (+C): no", out)
        self.assertIn("NOCOW images: fedora", out)
        self.assertIn(f"Set the mark with: cst-vm nocow apply   (folders: {self.boxes})", out)
        self.assertIn("not present", out)
        self.assertNotIn("virtio-win.iso", out)

    def test_convert_lines_order(self) -> None:
        img = {"path": "/i/w b.qcow2"}
        lines = self.m.convert_lines(img, False)
        self.assertEqual([ln.split()[0] for ln in lines], ["mv", "install", "chattr", "cp", "cmp", "#"])
        self.assertIn("install -m 0600 /dev/null '/i/w b.qcow2'", lines[1])
        self.assertTrue(lines[-1].startswith("# start the VM; after a good boot: rm "))
        self.assertTrue(all(ln.startswith(("sudo ", "# ")) for ln in self.m.convert_lines(img, True)))

    def test_unreadable_is_never_copy_on_write(self) -> None:
        self.file(self.system, "win.qcow2")
        self.flags[self.system] = None
        self.flags[os.path.join(self.system, "win.qcow2")] = None
        self.flags[self.boxes] = self.m.FS_NOCOW_FL
        real_scandir = os.scandir

        def scandir(p: str) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
            if p == self.system:
                raise PermissionError(13, "Permission denied")
            return real_scandir(p)

        with mock.patch.object(self.m.os, "scandir", scandir):
            rc, out = self.out(self.m.cmd_nocow_plan)
        self.assertIn("NOCOW mark (+C): not readable as your user (root-only folder): cst-vm nocow plan --root", out)
        self.assertNotIn("still copy-on-write", out)
        self.assertNotIn(": no:", out)
        # Readable listing but unreadable image flags (qemu's 0600 files): reported as unknown.
        rc, out = self.out(self.m.cmd_nocow_plan)
        self.assertIn("images whose mark is not readable as your user: win.qcow2", out)
        self.assertNotIn("still copy-on-write", out)

    def test_not_btrfs_and_missing_system_folder(self) -> None:
        def _not_btrfs(p: str) -> bool:
            return False

        with mock.patch.object(self.m, "is_btrfs", _not_btrfs):
            rc, out = self.out(self.m.cmd_nocow_plan)
        self.assertIn("not on btrfs (NOCOW does not apply)", out)
        self.assertIn("No folder to mark.", out)
        os.rmdir(self.system)
        d = self.dirs()[self.system]
        self.assertTrue(self.m.needs_mark(d))  # /var/lib/libvirt is there: create it
        os.rmdir(os.path.dirname(self.system))
        self.assertFalse(self.m.needs_mark(self.dirs()[self.system]))  # libvirt not installed

    def test_knob_off(self) -> None:
        def _knobs_off(path: str | None = None) -> dict[str, bool]:
            return dict(ALL_ON, VM_NOCOW=False)

        with mock.patch.object(self.m, "knobs", _knobs_off):
            rc, out = self.out(self.m.cmd_nocow_plan)
            self.assertIn("VM_NOCOW=no", out)
            rc, out = self.out(self.m.cmd_nocow_apply)
            self.assertIn("nothing to do", out)

    def test_apply_marks_user_folders_and_uses_sudo_for_the_system_one(self) -> None:
        self.flags[self.system] = None
        self.flags[self.boxes] = 0
        marked: list[str] = []
        sudo: list[list[str]] = []

        def set_nocow(p: str) -> None:
            marked.append(p)
            self.flags[p] = self.m.FS_NOCOW_FL

        def fake_sub_run(
            cmd: Sequence[str], check: bool = False, env: Mapping[str, str] | None = None
        ) -> subprocess.CompletedProcess[bytes]:
            sudo.append(list(cmd))
            return subprocess.CompletedProcess(list(cmd), 0)

        def fake_run(cmd: Sequence[str], timeout: int = 120) -> tuple[int, str, str]:
            sudo.append(list(cmd))
            return 0, "-----C---- x", ""

        def _uid() -> int:
            return 1000

        def _confirm(q: str) -> bool:
            return True

        with (
            mock.patch.object(self.m, "set_nocow", set_nocow),
            mock.patch.object(self.m.os, "geteuid", _uid),
            mock.patch.object(self.m.subprocess, "run", fake_sub_run),
            mock.patch.object(self.m, "run", fake_run),
            mock.patch.object(self.m, "confirm", _confirm),
        ):
            rc, out = self.out(self.m.cmd_nocow_apply)
        self.assertEqual(rc, 0, out)
        self.assertEqual(marked, [self.boxes])
        self.assertEqual(
            sudo, [["sudo", "--", "chattr", "+C", self.system], ["sudo", "--", "lsattr", "-d", self.system]]
        )
        self.assertIn(f"done: {self.system}", out)
        self.assertIn("existing images stay as they are", out)

    def test_apply_creates_a_missing_system_folder_like_libvirt(self) -> None:
        os.rmdir(self.system)
        self.flags[self.boxes] = self.m.FS_NOCOW_FL
        sudo: list[list[str]] = []

        def fake_sub_run(
            cmd: Sequence[str], check: bool = False, env: Mapping[str, str] | None = None
        ) -> subprocess.CompletedProcess[bytes]:
            sudo.append(list(cmd))
            return subprocess.CompletedProcess(list(cmd), 0)

        def fake_run(cmd: Sequence[str], timeout: int = 120) -> tuple[int, str, str]:
            return 0, "-----C---- x", ""

        def _uid() -> int:
            return 1000

        def _confirm(q: str) -> bool:
            return True

        with (
            mock.patch.object(self.m.os, "geteuid", _uid),
            mock.patch.object(self.m.subprocess, "run", fake_sub_run),
            mock.patch.object(self.m, "run", fake_run),
            mock.patch.object(self.m, "confirm", _confirm),
        ):
            rc, out = self.out(self.m.cmd_nocow_apply)
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            sudo,
            [
                ["sudo", "--", "install", "-d", "-o", "root", "-g", "root", "-m", "0711", self.system],
                ["sudo", "--", "restorecon", self.system],
                ["sudo", "--", "chattr", "+C", self.system],
            ],
        )

    def test_a_symlinked_folder_is_never_created_or_marked(self) -> None:
        real = os.path.join(self.tmp, "elsewhere")
        os.makedirs(real)
        os.chmod(real, 0o755)
        os.rmdir(self.system)
        os.symlink(real, self.system)
        libvirt_user = os.path.join(self.home, ".local", "share", "libvirt", "images")
        os.makedirs(os.path.dirname(libvirt_user))
        os.symlink(real, libvirt_user)
        self.flags[self.boxes] = self.m.FS_NOCOW_FL
        d = self.dirs()
        for p in (self.system, libvirt_user):
            self.assertEqual((d[p]["present"], d[p]["link"]), (False, real))
            self.assertFalse(self.m.needs_mark(d[p]))
        rc, out = self.out(self.m.cmd_nocow_plan)
        self.assertIn(
            f"{self.system}  [libvirt qemu:///system: virt-manager, virsh]: a symbolic link to {real}: "
            "not managed by CST",
            out,
        )
        self.assertIn("No folder to mark.", out)
        self.assertNotIn("not created yet", out)
        sudo: list[list[str]] = []

        def fake_sub_run(
            cmd: Sequence[str], check: bool = False, env: Mapping[str, str] | None = None
        ) -> subprocess.CompletedProcess[bytes]:
            sudo.append(list(cmd))
            return subprocess.CompletedProcess(list(cmd), 0)

        def _confirm(q: str) -> bool:
            return True

        with (
            mock.patch.object(self.m.subprocess, "run", fake_sub_run),
            mock.patch.object(self.m, "confirm", _confirm),
        ):
            rc, out = self.out(self.m.cmd_nocow_apply)
        self.assertEqual((rc, sudo), (0, []))
        self.assertIn("already has the NOCOW mark (+C) (symbolic links are not managed).", out)
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o755)  # target untouched
        # Even if the folder turns into a link between the probe and the action.
        probe: dict[str, object] = {
            "scope": "system",
            "path": self.system,
            "what": "x",
            "present": False,
            "link": None,
            "nocow": None,
        }
        self.assertIn("symbolic link: not touched", self.m.mark_system_dir(probe))

    def test_apply_with_nothing_to_do_and_without_a_terminal(self) -> None:
        self.flags[self.system] = self.flags[self.boxes] = self.m.FS_NOCOW_FL
        rc, out = self.out(self.m.cmd_nocow_apply)
        self.assertIn("already has the NOCOW mark", out)
        self.flags[self.boxes] = 0
        with mock.patch("sys.stdin", io.StringIO("")):
            with self.assertRaises(self.m.Refused):
                self.out(self.m.cmd_nocow_apply)

    def test_status_lists_the_folders(self) -> None:
        self.flags[self.system] = self.flags[self.boxes] = self.m.FS_NOCOW_FL

        def _virsh(*a: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
            return (0, "shut off\n", "") if a[0] == "domstate" else (0, real_domain(), "")

        def _read_index(path: str | None = None) -> list[dict[str, object]]:
            return []

        with (
            mock.patch.object(self.m, "virsh", _virsh),
            mock.patch.object(self.m, "read_index", _read_index),
        ):
            rc, out = self.out(self.m.cmd_status, "win11-devbox")
        self.assertIn("hardware: disks and network card as tune recommends", out)
        self.assertIn("VM image folders (NOCOW):", out)
        self.assertIn(f"  {self.system}  [libvirt qemu:///system: virt-manager, virsh]", out)
        self.assertNotIn("to convert", out)


class BtrfsReflinkTest(unittest.TestCase):
    """On a real btrfs (a scratch folder under ~/.cache, skipped elsewhere):
    a NOCOW image still gets a reflink backup, because the backup pre-creates
    a NOCOW destination; without it, the clone fails (the negative control)."""

    def setUp(self) -> None:
        self.m: _VmModule = load()
        cache = os.path.join(os.path.expanduser("~"), ".cache")
        if not os.path.isdir(cache) or not self.m.is_btrfs(cache):
            self.skipTest("~/.cache is not on btrfs")
        self.tmp = tempfile.mkdtemp(prefix="cst-vm-reflink-", dir=cache)
        self.addCleanup(shutil.rmtree, self.tmp)
        # The scratch folder itself copy-on-write, whatever ~/.cache has.
        flags = self.m.inode_flags(self.tmp)
        assert flags is not None
        self.setflags(self.tmp, flags & ~self.m.FS_NOCOW_FL, directory=True)

    def setflags(self, path: str, flags: int, directory: bool = False) -> None:
        import fcntl

        fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if directory else 0))
        try:
            fcntl.ioctl(fd, self.m.FS_IOC_SETFLAGS, struct.pack("l", flags))
        finally:
            os.close(fd)

    def test_reflink_of_a_nocow_image(self) -> None:
        src = os.path.join(self.tmp, "img.qcow2")
        open(src, "wb").close()
        src_flags = self.m.inode_flags(src)
        assert src_flags is not None
        self.setflags(src, src_flags | self.m.FS_NOCOW_FL)  # only while empty
        with open(src, "ab") as f:
            f.write(os.urandom(1 << 20))
        if not self.m.nocow(src):
            self.skipTest("NOCOW not settable here")
        plain = os.path.join(self.tmp, "plain.qcow2")
        r = subprocess.run(["cp", "--reflink=always", src, plain], check=False, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "btrfs cloned NOCOW -> COW: the negative control failed")
        dest = os.path.join(self.tmp, "backup", "img.qcow2")
        self.m.copy(src, dest, "reflink", True)
        self.assertTrue(self.m.nocow(dest))
        with open(src, "rb") as a, open(dest, "rb") as b:
            self.assertEqual(a.read(), b.read())
        # Restoring a copy-on-write backup onto the NOCOW image (the restore
        # commands use --reflink=auto): a real copy, and the image stays NOCOW.
        cow = os.path.join(self.tmp, "cow-backup.qcow2")
        with open(cow, "wb") as f:
            f.write(os.urandom(1 << 20))
        self.assertFalse(self.m.nocow(cow))
        r = subprocess.run(["cp", "--reflink=auto", "-a", cow, src], check=False, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.m.nocow(src))
        with open(src, "rb") as a, open(cow, "rb") as b:
            self.assertEqual(a.read(), b.read())


class SetupScriptTest(unittest.TestCase):
    def run_setup(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", SETUP, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "CST_PROG": "cst vm"},
            stdin=subprocess.DEVNULL,
        )

    def test_tune_and_nocow_routing(self) -> None:
        r = self.run_setup("tune")
        self.assertEqual(r.returncode, 2)
        self.assertIn("tune plan|apply", r.stderr)
        r = self.run_setup("nocow", "bogus")
        self.assertEqual(r.returncode, 2)
        # Changing commands refuse in a container, before any sudo.
        for args in (("tune", "apply", "w"), ("nocow", "apply"), ("nocow", "plan", "--root")):
            r = subprocess.run(
                ["bash", SETUP, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": "/usr/bin:/bin", "CST_PROG": "cst vm", "CST_VM_CONTAINER": "1"},
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("must run on the host", r.stderr)

    def test_help_and_unknown(self) -> None:
        r = self.run_setup("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("backup [VM] [--full]", r.stdout)
        self.assertIn("tune plan [VM] [--guest-has-virtio]", r.stdout)
        self.assertIn("nocow plan [--root]", r.stdout)
        self.assertIn("Usage: cst vm <command>", r.stdout)
        r = self.run_setup("nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Unknown command: nope", r.stderr)

    def test_status_in_a_container_without_flatpak_spawn(self) -> None:
        # A CI container: no way to the host, no virsh. Info lines, exit 0.
        tmp = tempfile.mkdtemp(prefix="cst-vm-test-")
        self.addCleanup(shutil.rmtree, tmp)
        for tool in ("bash", "python3", "readlink", "dirname", "sed", "env"):
            resolved = shutil.which(tool)
            assert resolved is not None
            os.symlink(resolved, os.path.join(tmp, tool))
        r = subprocess.run(
            ["bash", SETUP, "status", "win"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": tmp,
                "CST_PROG": "cst vm",
                "CST_VM_CONTAINER": "1",
                "CST_VM_INDEX": os.path.join(tmp, "none.json"),
                "CST_VM_CPU_CONF": os.path.join(tmp, "none.conf"),
            },
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cannot reach the host from this container", r.stdout)
        self.assertIn("win: virsh not found", r.stdout)
        self.assertNotIn("not found", r.stderr)

    def test_registered_in_cst(self) -> None:
        r = subprocess.run(
            ["bash", os.path.join(REPO, "cst"), "help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        self.assertIn("vm         the libvirt VM", r.stdout)
        self.assertEqual(MODULE_BY_NAME["vm"].script, "vm-setup.sh")
        self.assertFalse(MODULE_BY_NAME["vm"].status_needs_root)


class HexagonalArchitectureTest(unittest.TestCase):
    """Tests for `vm.domain`, `vm.adapters`, and `vm.application`."""

    def test_knobs_parse_and_defaults(self) -> None:
        defaults = VmKnobs()
        self.assertEqual(
            defaults.as_dict(),
            {"VM_NOCOW": True, "VM_TUNE_DISK_BUS": True, "VM_TUNE_DISK_DRIVER": True, "VM_TUNE_NIC": True},
        )
        self.assertEqual((defaults.keep_weekly, defaults.keep_monthly, defaults.timeout_hours), (4, 3, 6))
        parsed = VmKnobs.from_text(
            "# comment\n"
            "VM_NOCOW='no'\n"
            "VM_TUNE_DISK_BUS=yes\n"
            "VM_TUNE_DISK_DRIVER=0\n"
            "VM_TUNE_NIC=off\n"
            'VM_NAS_KEEP_WEEKLY="8"\n'
            "VM_NAS_KEEP_MONTHLY=bad\n"
            "VM_NAS_TIMEOUT_HOURS=12\n"
        )
        self.assertFalse(parsed.vm_nocow)
        self.assertTrue(parsed.vm_tune_disk_bus)
        self.assertFalse(parsed.vm_tune_disk_driver)
        self.assertFalse(parsed.vm_tune_nic)
        self.assertEqual((parsed.keep_weekly, parsed.keep_monthly, parsed.timeout_hours), (8, 3, 12))

    def test_domain_parts_and_tune_domain_xml(self) -> None:
        parts = extract_domain_parts(old_domain())
        self.assertEqual(parts.uuid, UUID)
        self.assertIn("/var/lib/libvirt/images/win11-devbox.qcow2", parts.disks)
        self.assertTrue(parts.as_dict()["tpm"])
        held_result = tune_domain_xml(old_domain(), VmKnobs().as_dict(), allow_bus=False)
        self.assertIsNotNone(held_result.after_xml)
        self.assertTrue(held_result.held)
        clean_result = tune_domain_xml(real_domain(), allow_bus=True)
        self.assertEqual((clean_result.changes, clean_result.held, clean_result.notes), ((), (), ()))

    def test_cli_parsing_and_backup_helpers(self) -> None:
        cmd = parse_vm_args(["backup", "win", "--nas", "--keep-tag=pre-f46", "--no-quiesce"])
        self.assertEqual((cmd.cmd, cmd.vm, cmd.nas, cmd.keep, cmd.no_quiesce), ("backup", "win", True, "pre-f46", True))
        self.assertEqual(cmd.as_dict()["keep"], "pre-f46")
        for bad in (
            ["backup", "--full", "--nas"],
            ["backup", "--keep-tag", "pre-f46"],
            ["backup", "--keep-tag=cst-bad"],
        ):
            with self.assertRaises(Usage):
                parse_vm_args(bad)
        self.assertTrue(valid_keep("pre-f46"))
        for bad_tag in (None, "", "cst-auto", "vm:win", "home", "bad/tag"):
            self.assertFalse(valid_keep(bad_tag))
        self.assertEqual(overlay_kind("running", "/live.qcow2", "/base.qcow2"), "live")
        self.assertEqual(overlay_kind("running", "/base.qcow2", "/base.qcow2"), "definition")
        self.assertEqual(overlay_kind("shut off", "/live.qcow2", "/base.qcow2"), "offline")
        self.assertEqual(overlay_kind("weird", "/live.qcow2", "/base.qcow2"), "unknown")
        args = forget_args("win", "workstation", 4, 3)
        self.assertIn("cst-vm,vm:win", args)
        self.assertIn("cst-keep", args)
        self.assertEqual(version_changes({"qemu-kvm": "1"}, {"qemu-kvm": "2", "edk2-ovmf": "3"}), ["qemu-kvm: 1 -> 2"])

    def test_service_inspect_plan_and_delegation(self) -> None:
        calls: list[list[str]] = []

        class FakeRuntime:
            def main(self, argv: Sequence[str]) -> int:
                calls.append(list(argv))
                return 42

        svc = VmService(FakeRuntime())
        self.assertEqual(svc.parse_args(["status", "win"]).cmd, "status")
        self.assertTrue(svc.inspect_domain(old_domain()).disks)
        result, diff, kind, why = svc.plan_tune(old_domain(), guest_has_virtio=False)
        self.assertEqual(kind, "windows")
        self.assertIn("http://microsoft.com/win/11", why)
        self.assertTrue(result.held)
        self.assertTrue(diff)
        self.assertEqual(svc.run(["list"]), 42)
        self.assertEqual(calls, [["list"]])

    def test_adapters_subprocess_virsh_and_runtime(self) -> None:
        recorded: list[tuple[list[str], bool, str]] = []

        def fake_run(
            cmd: Sequence[str],
            *,
            check: bool = False,
            capture_output: bool = False,
            text: bool = False,
            timeout: int = 60,
            stdin: int | None = None,
            env: Mapping[str, str] | None = None,
            start_new_session: bool = False,
        ) -> subprocess.CompletedProcess[str]:
            recorded.append((list(cmd), start_new_session, (env or {}).get("LC_ALL", "")))
            return subprocess.CompletedProcess(list(cmd), 0, "shut off\n", "")

        adapter = SubprocessVirshAdapter()
        with mock.patch("subprocess.run", fake_run):
            rc, out, err = adapter.run("domstate", "win", timeout=15, detach=True)
        self.assertEqual((rc, out, err), (0, "shut off\n", ""))
        self.assertEqual(recorded, [(["virsh", "-c", "qemu:///system", "domstate", "win"], True, "C")])

        def fake_missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("virsh")

        with mock.patch("subprocess.run", fake_missing):
            self.assertEqual(adapter.run("domstate", "win"), (127, "", "virsh: command not found"))

        def fake_timeout(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(list(cmd), 5)

        with mock.patch("subprocess.run", fake_timeout):
            self.assertEqual(adapter.run("domstate", "win", timeout=5), (124, "", "timed out after 5s"))

        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.txt")
            script = os.path.join(tmp, "fake_vm_tool")
            with open(script, "w") as f:
                f.write(
                    f"def main(argv):\n"
                    f"    with open({log!r}, 'a') as fh:\n"
                    f"        fh.write(' '.join(argv) + '\\n')\n"
                    f"    return 7\n"
                )
            runtime = CstVmRuntimeAdapter(tool_path=script)
            self.assertEqual(runtime.main(["status", "win"]), 7)
            self.assertEqual(runtime.main(["list"]), 7)
            with open(log) as f:
                self.assertEqual(f.read().splitlines(), ["status win", "list"])


if __name__ == "__main__":
    unittest.main()
