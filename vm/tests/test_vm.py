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

import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
VM_DIR = os.path.dirname(HERE)
TOOL = os.path.join(VM_DIR, "cst-vm")
REPO = os.path.dirname(VM_DIR)
SETUP = os.path.join(REPO, "vm-setup.sh")
UUID = "ff2515e6-3708-42be-be94-d4356f12bfc2"
NOW = 1794100000


def load():
    loader = importlib.machinery.SourceFileLoader("cst_vm", TOOL)
    spec = importlib.util.spec_from_loader("cst_vm", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


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
    def setUp(self):
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
            json.dump({"deployments": [{"booted": False, "checksum": "s" * 64},
                                       {"booted": True, "checksum": "b" * 64, "version": "45.1",
                                        "timestamp": NOW - 86400}]}, f)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": self.tmp, "LANG": "C.UTF-8", "TZ": "UTC",
            "STUB_LOG": self.log, "FIX": self.fix, "CST_VM_NONROOT_OK": "1", "CST_VM_NOW": str(NOW),
            "CST_VM_BACKUP_DIR": self.backups, "CST_VM_INDEX": self.index, "CST_VM_SWTPM_DIR": self.swtpm,
            "CST_PROG": "cst vm", "CST_VM_CPU_CONF": os.path.join(self.tmp, "cpu-setup.conf"),
        }

    def state(self, s):
        with open(os.path.join(self.fix, "state.txt"), "w") as f:
            f.write(s + "\n\n")

    def domain_xml(self, extra_disk="", cpu="<cpu mode='host-passthrough'/>"):
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

    def domain(self, xml):
        with open(os.path.join(self.fix, "domain.xml"), "w") as f:
            f.write(xml)

    def run_tool(self, *args):
        return subprocess.run(["python3", "-I", TOOL, *args], env=self.env, capture_output=True, text=True,
                              timeout=60)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def dest(self):
        return os.path.join(self.backups, "win", "20261108T010640Z")


class PartsTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_disks_nvram_tpm(self):
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

    def test_block_and_network_disks_are_problems(self):
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

    def test_parse_args(self):
        self.assertEqual(self.m.parse_args(["backup", "x", "--full"]), ("backup", "x", True))
        self.assertEqual(self.m.parse_args(["list"]), ("list", None, False))
        self.assertEqual(self.m.parse_args([]), ("help", None, False))
        for bad in (["status", "--full"], ["backup", "a", "b"], ["prune"], ["backup", "--full", "--full"]):
            with self.assertRaises(self.m.Usage):
                self.m.parse_args(bad)

    def test_version_changes(self):
        self.assertEqual(self.m.version_changes({"qemu-kvm": "a", "swtpm": "x"}, {"qemu-kvm": "b", "swtpm": "x"}),
                         ["qemu-kvm: a -> b"])

    def test_cpu_disable_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "c.conf")
            self.assertEqual(self.m.cpu_disable_list(conf)[0], "cet-ss")
            with open(conf, "w") as f:
                f.write("VM_CPU_DISABLE=''\n")
            self.assertEqual(self.m.cpu_disable_list(conf), [])


class BackupTest(Scratch):
    def test_reflink_backup(self):
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = self.dest()
        for d in (self.backups, os.path.dirname(dest), dest):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        with open(os.path.join(dest, "files", self.disk.lstrip("/")), "rb") as f:
            self.assertTrue(f.read().endswith(b"data"))
        self.assertTrue(os.path.isfile(os.path.join(dest, "files", self.nvram.lstrip("/"))))
        self.assertTrue(os.path.isfile(os.path.join(dest, "files", self.swtpm.lstrip("/"), UUID, "tpm2",
                                                    "tpm2-00.permall")))
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

    def test_index_drops_removed_backups(self):
        os.makedirs(os.path.dirname(self.index))
        kept = os.path.join(self.tmp, "kept")
        os.makedirs(kept)
        with open(self.index, "w") as f:
            json.dump({"backups": [{"vm": "win", "path": kept, "epoch": 1, "timestamp": "a"},
                                   {"vm": "win", "path": os.path.join(self.tmp, "gone"), "epoch": 2,
                                    "timestamp": "b"}]}, f)
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        paths = [e["path"] for e in json.load(open(self.index))["backups"]]
        self.assertEqual(paths, [kept, self.dest()])

    def test_full_backup_flags(self):
        r = self.run_tool("backup", "win", "--full")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cps = [c for c in self.calls() if c.startswith("cp ")]
        self.assertTrue(cps[0].startswith("cp --reflink=never --sparse=always -a -- "), cps)
        self.assertTrue(cps[1].startswith(f"cp --reflink=never -a -- {self.nvram} "), cps)
        self.assertTrue(cps[2].startswith("cp --reflink=never -a -- "), cps)
        self.assertEqual(json.load(open(os.path.join(self.dest(), "manifest.json")))["mode"], "full")

    def test_refuses_a_running_vm(self):
        self.state("running")
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("win is running: shut it down first", r.stderr)
        self.assertFalse(os.path.exists(self.backups))
        self.assertFalse([c for c in self.calls() if "dumpxml" in c])

    def test_refuses_block_disks(self):
        self.domain(self.domain_xml("<disk type='block' device='disk'><source dev='/dev/sdz'/>"
                                    "<target dev='sdc'/></disk>"))
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sdc: a block disk (/dev/sdz): only file disks can be copied", r.stderr)
        self.assertFalse(os.path.exists(self.backups))

    def test_refuses_qcow2_overlays(self):
        with open(self.disk, "wb") as f:
            f.write(b"QFI\xfb\0\0\0\3" + struct.pack(">Q", 512))
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("qcow2 overlay with a backing file", r.stderr)

    def test_reflink_failure_hints_full_and_cleans_up(self):
        self.env["FIX_CP_FAIL"] = "1"
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Invalid cross-device link", r.stderr)
        self.assertIn("cst vm backup VM --full", r.stderr)
        self.assertIn("incomplete backup removed", r.stderr)
        self.assertFalse(os.path.exists(self.dest()))
        self.assertFalse(os.path.exists(self.index))

    def test_refuses_a_managed_saved_vm(self):
        with open(os.path.join(self.fix, "managed.txt"), "w") as f:
            f.write("other\nwin\n\n")
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("win was saved (managed save), not shut down", r.stderr)
        self.assertIn("managedsave-remove win", r.stderr)
        self.assertFalse(os.path.exists(self.backups))

    def test_virsh_answers_in_english(self):
        self.env["LC_ALL"] = "pt_BR.UTF-8"
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(os.path.join(self.fix, "virsh-locale.txt")).read().strip(), "LC_ALL=C")

    def test_vm_started_during_the_copy(self):
        for state, needle in (("running", "state changed during the copy"),
                              ("shut off", "changed during the copy (the VM was started?)")):
            with self.subTest(state=state):
                self.state("shut off")
                self.env.update(FIX_CP_STARTS_VM=state, FIX_CP_TOUCH=self.disk)
                r = self.run_tool("backup", "win")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(needle, r.stderr)
                self.assertIn("incomplete backup removed", r.stderr)
                self.assertFalse(os.path.exists(self.dest()))
                self.assertFalse(os.path.exists(self.index))

    def test_symlinked_disk_is_copied_with_its_data(self):
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
        self.assertIn(f"{kept} {real}", r.stdout)       # restored to the file it points to
        man = json.load(open(os.path.join(self.dest(), "manifest.json")))
        self.assertEqual(man["files"][0]["source"], real)

    def test_missing_tpm_state_is_a_note(self):
        shutil.rmtree(os.path.join(self.swtpm, UUID))
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TPM state", r.stdout)
        self.assertIn("BitLocker", r.stdout)

    def test_needs_root(self):
        del self.env["CST_VM_NONROOT_OK"]
        if os.geteuid() == 0:
            self.skipTest("running as root")
        r = self.run_tool("backup", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("backup needs root", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_full_refuses_without_space(self):
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
    def test_default_vm_from_cpu_setup_conf(self):
        with open(self.env["CST_VM_CPU_CONF"], "w") as f:
            f.write("VM_NAME='win'\n")
        r = self.run_tool("status")
        self.assertIn("win: shut off", r.stdout)
        self.assertIn("virsh -c qemu:///system domstate win", self.calls())

    def test_list_and_status(self):
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

    def test_status_with_fixed_cpu_and_without_virsh(self):
        self.domain(self.domain_xml(cpu="<cpu mode='host-passthrough'>" + "".join(
            f"<feature policy='disable' name='{n}'/>" for n in
            ("cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet")) + "</cpu>"))
        r = self.run_tool("status", "win")
        self.assertIn("cpu mode host-passthrough: disabled cet-ss", r.stdout)
        self.assertNotIn("--security-info", "\n".join(self.calls()))
        os.unlink(os.path.join(self.bin, "virsh"))
        self.env["PATH"] = f"{self.bin}"
        r = subprocess.run([sys.executable, "-I", TOOL, "status", "win"], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("virsh not found", r.stdout)

    def test_usage(self):
        r = self.run_tool("bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        r = self.run_tool("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("reflink", r.stdout)


class SetupScriptTest(unittest.TestCase):
    def run_setup(self, *args):
        return subprocess.run(["bash", SETUP, *args], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "CST_PROG": "cst vm"})

    def test_help_and_unknown(self):
        r = self.run_setup("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("backup [VM] [--full]", r.stdout)
        self.assertIn("Usage: cst vm <command>", r.stdout)
        r = self.run_setup("nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Unknown command: nope", r.stderr)

    def test_status_in_a_container_without_flatpak_spawn(self):
        # A CI container: no way to the host, no virsh. Info lines, exit 0.
        tmp = tempfile.mkdtemp(prefix="cst-vm-test-")
        self.addCleanup(shutil.rmtree, tmp)
        for tool in ("bash", "python3", "readlink", "dirname", "sed", "env"):
            os.symlink(shutil.which(tool), os.path.join(tmp, tool))
        r = subprocess.run(["bash", SETUP, "status", "win"], capture_output=True, text=True, timeout=30,
                           env={"PATH": tmp, "CST_PROG": "cst vm", "CST_VM_CONTAINER": "1",
                                "CST_VM_INDEX": os.path.join(tmp, "none.json"),
                                "CST_VM_CPU_CONF": os.path.join(tmp, "none.conf")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cannot reach the host from this container", r.stdout)
        self.assertIn("win: virsh not found", r.stdout)
        self.assertNotIn("not found", r.stderr)

    def test_registered_in_cst(self):
        r = subprocess.run(["bash", os.path.join(REPO, "cst"), "help"], capture_output=True, text=True, timeout=30)
        self.assertIn("vm         the libvirt VM", r.stdout)
        with open(os.path.join(REPO, "cst")) as f:
            text = f.read()
        self.assertIn("[vm]=vm-setup.sh", text)
        self.assertIn("[vm]=0", text)


if __name__ == "__main__":
    unittest.main()
