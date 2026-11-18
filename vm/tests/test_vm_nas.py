# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst vm backup --nas` (vm/cst-vm): the live external
snapshot, the quiesce fallback, restic's arguments, the blockcommit that
always follows, its verification, the refusals, the retention (forget), the
index entries, and the weekly timer's units.

Nothing reaches the real host: virsh and restic are stateful fakes (Python
scripts in a scratch bin folder first in PATH) that keep the VM's disk
sources in a JSON file, so domblklist shows what the snapshot and the pivot
did; rpm, rpm-ostree, systemctl and the alert program are stubs; every path
of the tool is a scratch path (CST_VM_*), and it runs as the user
(CST_VM_NONROOT_OK).

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s vm/tests -t . -p test_vm_nas.py -v
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import time
from typing import Any
import unittest

# Run directly (python3 -I -B vm/tests/test_vm_nas.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.errors import Usage
from btrfs.tests.test_snapshot_runtime import shell_function
from vm import adapters as vm_adapters, domain as vm_domain

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
VM_DIR = os.path.dirname(HERE)
TOOL = os.path.join(VM_DIR, "cst-vm")
REPO = os.path.dirname(VM_DIR)
SNAPSHOT_RUNTIME = os.path.join(REPO, "btrfs", "btrfs-snapshot")
UUID = "ff2515e6-3708-42be-be94-d4356f12bfc2"
NOW = 1794100000
STAMP = "20261108T010640Z"
SNAP_ID = "a1b2c3d4" + "e" * 56


# The VM, as JSON in $FIX/vm.json: state, agent, live and persistent disk
# sources, and the failures a test asks for (fail: a list of words).
VIRSH = r"""#!/usr/bin/python3 -I
import json, os, sys
fix = os.environ["FIX"]
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("virsh " + " ".join(args) + "\n")
path = os.path.join(fix, "vm.json")
vm = json.load(open(path))
def save():
    json.dump(vm, open(path, "w"))
if args[:2] == ["-c", "qemu:///system"]:
    args = args[2:]
cmd = args[0]
fail = vm.get("fail", [])
def xml(sources, secret):
    disks = "".join(
        f"<disk type='file' device='{d['device']}'><source file='{sources[d['target']]}'/>"
        + ("<backingStore type='file'><source file='x'/></backingStore>" if vm.get("chain") else "<backingStore/>")
        + f"<target dev='{d['target']}'/></disk>" for d in vm["disks"])
    agent = vm.get("agent")
    chan = (f"<channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0' state='{agent}'/></channel>"
            if agent else "")
    pw = "<graphics type='spice' passwd='s3cret'/>" if secret else "<graphics type='spice'/>"
    return (f"<domain type='kvm'><name>win</name><uuid>{vm['uuid']}</uuid><os><nvram>{vm['nvram']}</nvram></os>"
            f"<devices>{disks}{chan}{pw}<tpm><backend type='emulator'/></tpm></devices></domain>\n")
if cmd == "domstate":
    print(vm["state"]); print()
elif cmd == "list":
    print("\n".join(vm.get("managed", [])))
elif cmd == "dumpxml":
    print(xml(vm["persistent"] if "--inactive" in args else vm["live"], "--security-info" in args))
elif cmd == "domblklist":
    if "blklist" in fail or "--inactive" in args and "blklist-inactive" in fail and vm["live"]["sda"].endswith(".qcow2"):
        sys.exit("error: failed")
    src = vm["persistent"] if "--inactive" in args else vm["live"]
    print(" Type   Device   Target   Source\n" + "-" * 40)
    for d in vm["disks"]:
        print(f" file   {d['device']}   {d['target']}   {src[d['target']]}")
elif cmd == "snapshot-create-as":
    if "--quiesce" in args and "quiesce" in fail:
        sys.exit("error: Guest agent is not responding: QEMU guest agent is not connected")
    if "snapshot" in fail:
        sys.exit("error: internal error: snapshot failed")
    if "snapshot-hang-nothing" in fail:
        # virsh hangs; libvirt made the file but never switched the disk (yet).
        for i, a in enumerate(args):
            if a == "--diskspec" and "snapshot=external" in args[i + 1]:
                open(args[i + 1].split("file=", 1)[1], "w").write("overlay")
        import time; time.sleep(30)
    for i, a in enumerate(args):
        if a == "--diskspec":
            spec = dict(kv.split("=", 1) for kv in args[i + 1].split(",")[1:])
            t = args[i + 1].split(",")[0]
            if spec.get("snapshot") == "external":
                open(spec["file"], "w").write("overlay")
                vm["live"][t] = spec["file"]
                vm["persistent"][t] = spec["file"]
    save()
    if "term-after-snapshot" in fail:
        import signal
        os.kill(os.getppid(), signal.SIGTERM)
    if "snapshot-hang" in fail:
        # libvirt finished, but virsh does not return in time.
        import time; time.sleep(30)
elif cmd == "blockcommit":
    t = args[2]
    if "commit-sigint" in fail:
        # As the real virsh: SIGINT cancels a `blockcommit --wait` (man virsh).
        import signal, time
        signal.signal(signal.SIGINT, lambda *a: sys.exit("error: block commit aborted (SIGINT)"))
        open(os.path.join(fix, "commit-started"), "w").close()
        time.sleep(3)
    if "commit" in fail:
        save(); sys.exit("error: block commit failed")
    base = vm["live"][t].split(".cst-overlay-")[0]
    vm["live"][t] = base
    if "persistent-stays" not in fail:
        vm["persistent"][t] = base
    save()
elif cmd == "domfsthaw":
    pass
else:
    sys.exit(f"error: unsupported in the stub: {cmd}")
"""

# restic: logs each call (arguments, the restic variables, and what the VM's
# disks pointed to at that moment) to $FIX/restic.log, one JSON per line;
# $FIX/restic.json sets exit codes and the snapshots `restic snapshots` lists.
RESTIC = r"""#!/usr/bin/python3 -I
import json, os, sys
fix = os.environ["FIX"]
args = sys.argv[1:]
cfg = json.load(open(os.path.join(fix, "restic.json")))
vm = json.load(open(os.path.join(fix, "vm.json")))
env = {k: v for k, v in os.environ.items() if k.startswith("RESTIC_")}
stage = [a for a in args if a.endswith("/vm-nas/win")]
staged = sorted(os.listdir(stage[0])) if stage and os.path.isdir(stage[0]) else []
with open(os.path.join(fix, "restic.log"), "a") as f:
    f.write(json.dumps({"args": args, "env": env, "live": vm["live"], "staged": staged}) + "\n")
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("restic " + " ".join(args) + "\n")
cmd = args[0]
if cmd == "cat":
    sys.exit(cfg.get("cat_rc", 0))
if cmd == "backup":
    if staged:
        xml = open(os.path.join(stage[0], "domain.xml")).read()
        open(os.path.join(fix, "staged-domain.xml"), "w").write(xml)
        open(os.path.join(fix, "staged-manifest.json"), "w").write(open(os.path.join(stage[0], "manifest.json")).read())
    for word, state in (("stop-during-backup", "shut off"), ("start-during-backup", "running"),
                        ("pause-during-backup", "paused")):
        if word in vm.get("fail", []):
            vm["state"] = state
            json.dump(vm, open(os.path.join(fix, "vm.json"), "w"))
    if "touch-nvram" in vm.get("fail", []):
        open(vm["nvram"], "a").write("more")
    if "slow-backup" in vm.get("fail", []):
        open(os.path.join(fix, "backup-started"), "w").close()
        import time; time.sleep(3)
    print(json.dumps({"message_type": "status", "percent_done": 0.5, "total_bytes": 10, "bytes_done": 5}))
    if "term-during-backup" in vm.get("fail", []):
        import signal, time
        sys.stdout.flush()
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(30)
    rc = cfg.get("backup_rc", 0)
    if rc in (0, 3):
        print(json.dumps({"message_type": "summary", "snapshot_id": cfg.get("id", "a1b2c3d4" + "e" * 56),
                          "total_bytes_processed": 2048, "data_added": 1024}))
    sys.exit(rc)
if cmd == "forget":
    print("remove 1 snapshots:" if cfg.get("forgot") else "keep 2 snapshots:")
    sys.exit(cfg.get("forget_rc", 0))
if cmd == "snapshots":
    print(json.dumps([{"id": i} for i in cfg.get("snapshots", [])]))
    sys.exit(0)
sys.exit(1)
"""
RPM = """#!/bin/sh
echo "rpm $*" >>"$STUB_LOG"
exit 1
"""
RPM_OSTREE = """#!/bin/sh
echo '{"deployments": [{"booted": true, "checksum": "bbbb", "version": "45.1", "timestamp": 1794000000}]}'
"""
NOTIFY = """#!/bin/sh
echo "notify $*" >>"$STUB_LOG"
"""
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >>"$STUB_LOG"
case "$1" in
  enable) mkdir -p "$UNIT_DIR/timers.target.wants" && touch "$UNIT_DIR/timers.target.wants/$3" ;;
  disable) rm -f "$UNIT_DIR/timers.target.wants/$3" ;;
  show) echo "Sat 2026-11-21 04:41:12 UTC" ;;
  # clean needs the unit loaded: the log says whether the timer's file was still there.
  clean) [ -e "$UNIT_DIR/cst-vm-backup@.timer" ] && echo "  (timer file present)" >>"$STUB_LOG" ;;
esac
exit 0
"""


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


class Nas(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-vm-nas-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin, self.fix = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "fix")
        self.images = os.path.join(self.tmp, "images")
        self.swtpm = os.path.join(self.tmp, "swtpm")
        self.units = os.path.join(self.tmp, "units")
        for d in (self.bin, self.fix, self.images, os.path.join(self.swtpm, UUID), self.units):
            os.makedirs(d)
        for name, body in (
            ("virsh", VIRSH),
            ("restic", RESTIC),
            ("rpm", RPM),
            ("rpm-ostree", RPM_OSTREE),
            ("notify", NOTIFY),
            ("systemctl", SYSTEMCTL),
        ):
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
        with open(os.path.join(self.swtpm, UUID, "tpm2-00.permall"), "wb") as f:
            f.write(b"tpm")
        self.overlay = f"{self.disk}.cst-overlay-{STAMP}"
        self.vm(state="running", agent="connected")
        self.restic_cfg()
        self.conf = os.path.join(self.tmp, "snapshots.conf")
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/volume1/restic\nNAS_RESTIC_HOST=sophia\n")
        self.password = os.path.join(self.tmp, "restic-password")
        with open(self.password, "w") as f:
            f.write("pw\n")
        self.index = os.path.join(self.tmp, "lib-cst", "vm-backups.json")
        self.stage = os.path.join(self.tmp, "lib-cst", "vm-nas")
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "STUB_LOG": self.log,
            "FIX": self.fix,
            "CST_VM_NONROOT_OK": "1",
            "CST_VM_NOW": str(NOW),
            "CST_VM_INDEX": self.index,
            "CST_VM_SWTPM_DIR": self.swtpm,
            "CST_PROG": "cst vm",
            "CST_VM_CPU_CONF": os.path.join(self.tmp, "cpu-setup.conf"),
            "CST_VM_NAS_CONF": self.conf,
            "CST_VM_RESTIC_PASSWORD": self.password,
            "CST_VM_RESTIC_CACHE": os.path.join(self.tmp, "cache"),
            "CST_VM_NAS_STAGE": self.stage,
            "CST_VM_RUN_DIR": os.path.join(self.tmp, "run"),
            "CST_VM_NOTIFY": os.path.join(self.bin, "notify"),
            "CST_VM_ALERTS": os.path.join(self.tmp, "alerts", "pending"),
            "CST_VM_CONF": os.path.join(self.tmp, "vm.conf"),
            "CST_VM_UNIT_DIR": self.units,
            "UNIT_DIR": self.units,
            "CST_VM_SBIN_COPY": os.path.join(self.tmp, "sbin", "cst-vm"),
            "CST_VM_INSTALL_RECORD": os.path.join(self.tmp, "var-lib-cst", "installed.json"),
        }

    def vm(self, **kw: object) -> None:
        state: dict[str, Any] = {
            "state": "running",
            "uuid": UUID,
            "nvram": self.nvram,
            "fail": [],
            "disks": [{"target": "sda", "device": "disk"}, {"target": "sdb", "device": "cdrom"}],
            "live": {"sda": self.disk, "sdb": "/iso/virtio.iso"},
            "persistent": {"sda": self.disk, "sdb": "/iso/virtio.iso"},
        }
        state.update(kw)
        with open(os.path.join(self.fix, "vm.json"), "w") as f:
            json.dump(state, f)

    def vm_state(self) -> dict[str, Any]:
        with open(os.path.join(self.fix, "vm.json")) as f:
            return json.load(f)

    def restic_cfg(self, **kw: object) -> None:
        cfg: dict[str, Any] = {"snapshots": [SNAP_ID]}
        cfg.update(kw)
        with open(os.path.join(self.fix, "restic.json"), "w") as f:
            json.dump(cfg, f)

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", "-I", TOOL, *args],
            check=False,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def calls(self) -> list[str]:
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def restic_calls(self) -> list[dict[str, Any]]:
        try:
            with open(os.path.join(self.fix, "restic.log")) as f:
                return [json.loads(ln) for ln in f]
        except FileNotFoundError:
            return []

    def virsh_calls(self, sub: str) -> list[str]:
        return [c for c in self.calls() if c.startswith(f"virsh -c qemu:///system {sub}")]

    def index_entries(self) -> list[dict[str, Any]]:
        with open(self.index) as f:
            return json.load(f)["backups"]


class LiveBackupTest(Nas):
    def test_running_vm_quiesced(self) -> None:
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snaps = self.virsh_calls("snapshot-create-as")
        self.assertEqual(
            snaps,
            [
                f"virsh -c qemu:///system snapshot-create-as win --name cst-nas-{STAMP} --disk-only --atomic "
                f"--no-metadata --quiesce --diskspec sda,snapshot=external,file={self.overlay} "
                "--diskspec sdb,snapshot=no"
            ],
        )
        rc = self.restic_calls()
        self.assertEqual([c["args"][0] for c in rc], ["cat", "backup", "forget", "snapshots"])
        self.assertEqual(rc[0]["args"], ["cat", "config", "--no-lock"])
        backup = rc[1]
        stage = os.path.join(self.stage, "win")
        self.assertEqual(
            backup["args"],
            [
                "backup",
                "--json",
                "--host",
                "sophia",
                "--tag",
                "cst-vm,vm:win",
                "--retry-lock",
                "30m",
                self.disk,
                self.nvram,
                os.path.join(self.swtpm, UUID),
                stage,
            ],
        )
        # restic read the base image while the VM wrote into the overlay.
        self.assertEqual(backup["live"]["sda"], self.overlay)
        self.assertEqual(backup["staged"], ["domain.xml", "manifest.json"])
        self.assertEqual(
            backup["env"],
            {
                "RESTIC_REPOSITORY": "sftp:btrfs-setup-nas:/volume1/restic",
                "RESTIC_PASSWORD_FILE": self.password,
                "RESTIC_CACHE_DIR": os.path.join(self.tmp, "cache"),
            },
        )
        # The definition restic stored has the secrets (the repository is encrypted); the staged files are gone.
        self.assertIn("s3cret", read(os.path.join(self.fix, "staged-domain.xml")))
        self.assertEqual(os.listdir(stage), [])
        self.assertEqual(stat.S_IMODE(os.stat(stage).st_mode), 0o700)
        man = json.loads(read(os.path.join(self.fix, "staged-manifest.json")))
        self.assertEqual((man["mode"], man["consistency"], man["epoch"]), ("nas", "quiesced", NOW))
        # Merged back, verified in the live and the persistent definition, overlay removed.
        commits = self.virsh_calls("blockcommit")
        self.assertEqual(len(commits), 1)
        self.assertRegex(
            commits[0],
            r"^virsh -c qemu:///system blockcommit win sda --active --pivot --wait "
            r"--verbose --timeout \d+$",
        )
        self.assertLessEqual(int(commits[0].split()[-1]), 4 * 3600)
        self.assertIn("virsh -c qemu:///system domblklist win --details --inactive", self.calls())
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertFalse(os.path.exists(self.overlay))
        # Order: snapshot, restic backup, blockcommit, forget.
        order = [
            c.split()[3] if c.startswith("virsh") else c.split()[1]
            for c in self.calls()
            if c.startswith(
                (
                    "virsh -c qemu:///system snapshot",
                    "virsh -c qemu:///system blockcommit",
                    "restic backup",
                    "restic forget",
                )
            )
        ]
        self.assertEqual(order, ["snapshot-create-as", "backup", "blockcommit", "forget"])
        e = self.index_entries()
        self.assertEqual(len(e), 1)
        self.assertEqual(
            {k: e[0][k] for k in ("vm", "mode", "snapshot", "consistency", "keep", "epoch", "host")},
            {
                "vm": "win",
                "mode": "nas",
                "snapshot": SNAP_ID,
                "consistency": "quiesced",
                "keep": None,
                "epoch": NOW,
                "host": "sophia",
            },
        )
        self.assertEqual(e[0]["deployment"]["checksum"], "bbbb")
        self.assertNotIn("s3cret", read(self.index) + r.stdout + r.stderr)
        self.assertIn("quiesced", r.stdout)
        self.assertNotIn("notify", "\n".join(self.calls()))

    def test_quiesce_fallback(self) -> None:
        self.vm(fail=["quiesce"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snaps = self.virsh_calls("snapshot-create-as")
        self.assertEqual(len(snaps), 2)
        self.assertIn("--quiesce", snaps[0])
        self.assertNotIn("--quiesce", snaps[1])
        self.assertIn("crash-consistent snapshot (quiesce failed: error: Guest agent is not responding", r.stdout)
        self.assertEqual(self.index_entries()[0]["consistency"], "crash-consistent")

    def test_no_agent_and_no_quiesce_skip_the_attempt(self) -> None:
        for agent, args, why in (
            (None, (), "no guest agent channel"),
            ("disconnected", (), "the guest agent is not connected"),
            ("connected", ("--no-quiesce",), "--no-quiesce"),
        ):
            self.vm(agent=agent)
            open(self.log, "w").close()
            r = self.run_tool("backup", "win", "--nas", *args)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            snaps = self.virsh_calls("snapshot-create-as")
            self.assertEqual(len(snaps), 1)
            self.assertNotIn("--quiesce", snaps[0])
            self.assertIn(f"crash-consistent snapshot ({why}", r.stdout)

    def test_commit_runs_when_restic_fails(self) -> None:
        self.restic_cfg(backup_rc=1)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("restic backup failed", r.stderr)
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertFalse(os.path.exists(self.overlay))
        self.assertFalse(os.path.exists(self.index))
        self.assertNotIn("forget", [c["args"][0] for c in self.restic_calls()])

    def test_sigterm_during_restic_still_merges(self) -> None:
        self.vm(fail=["term-during-backup"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("stopped by signal SIGTERM", r.stderr)
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertFalse(os.path.exists(self.overlay))
        self.assertFalse(os.path.exists(self.index))

    def test_sigterm_during_the_snapshot_call_still_merges(self) -> None:
        # libvirt finished the snapshot, but the stop arrived before virsh returned.
        self.vm(fail=["term-after-snapshot"], agent=None)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("stopped by signal SIGTERM", r.stderr)
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertFalse(os.path.exists(self.overlay))
        self.assertNotIn("backup", [c["args"][0] for c in self.restic_calls()])

    def test_unreadable_persistent_definition_keeps_the_overlay(self) -> None:
        # After the pivot the --inactive listing fails: the file stays (fail closed).
        self.vm(fail=["blklist-inactive"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("the persistent definition was not readable", r.stderr)
        self.assertTrue(os.path.exists(self.overlay))
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)

    def test_incomplete_restic_snapshot_is_forgotten(self) -> None:
        self.restic_cfg(backup_rc=3)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("some files could not be read", r.stderr)
        forgets = [c["args"] for c in self.restic_calls() if c["args"][0] == "forget"]
        self.assertEqual(forgets, [["forget", "--retry-lock", "30m", SNAP_ID]])
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)

    def test_commit_failure_is_loud_and_leaves_everything(self) -> None:
        self.vm(fail=["commit"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DISK NOT MERGED BACK", r.stderr)
        self.assertIn("sudo virsh -c qemu:///system blockcommit win sda --active --pivot --wait --verbose", r.stderr)
        self.assertIn(
            "sudo virsh -c qemu:///system blockjob win sda --pivot     # only if --info shows the commit ready",
            r.stderr,
        )
        self.assertIn(f"sudo rm {self.overlay}", r.stderr)
        self.assertTrue(os.path.exists(self.overlay))
        self.assertEqual(self.vm_state()["live"]["sda"], self.overlay)
        self.assertTrue([c for c in self.calls() if c.startswith("notify -u critical -a cst-vm")])
        self.assertFalse(os.path.exists(self.index))
        self.assertNotIn("forget", [c["args"][0] for c in self.restic_calls()])

    def test_persistent_definition_still_on_the_overlay(self) -> None:
        self.vm(fail=["persistent-stays"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the definition still names", r.stderr)
        self.assertTrue(os.path.exists(self.overlay), "never removed while the definition names it")
        # The running VM is back on the image: the fix is the definition, never a commit of the stale overlay.
        self.assertIn("sudo virsh -c qemu:///system edit win", r.stderr)
        self.assertIn("do NOT qemu-img commit it", r.stderr)
        self.assertNotIn("sudo qemu-img commit", r.stderr)
        self.assertNotIn("blockcommit win sda --active --pivot --wait --verbose     #", r.stderr)

    def test_vm_stopped_during_the_backup(self) -> None:
        self.vm(fail=["stop-during-backup"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stopped during the backup", r.stderr)
        self.assertIn(f"sudo qemu-img commit -p {self.overlay}", r.stderr)
        self.assertEqual(self.virsh_calls("blockcommit"), [])
        self.assertTrue(os.path.exists(self.overlay))

    def test_snapshot_failure_changes_nothing(self) -> None:
        self.vm(fail=["snapshot"], agent=None)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("snapshot failed (nothing changed)", r.stderr)
        self.assertEqual(self.virsh_calls("blockcommit"), [])
        self.assertNotIn("backup", [c["args"][0] for c in self.restic_calls()])

    def test_keep_tag_and_retention(self) -> None:
        with open(self.env["CST_VM_CONF"], "w") as f:
            f.write("VM_NAS_KEEP_WEEKLY=6\nVM_NAS_KEEP_MONTHLY=2\n")
        r = self.run_tool("backup", "win", "--nas", "--keep-tag", "pre-f46")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rc = self.restic_calls()
        self.assertIn("cst-vm,vm:win,cst-keep,pre-f46", rc[1]["args"])
        self.assertEqual(
            rc[2]["args"],
            [
                "forget",
                "--host",
                "sophia",
                "--tag",
                "cst-vm,vm:win",
                "--group-by",
                "host",
                "--keep-weekly",
                "6",
                "--keep-monthly",
                "2",
                "--keep-tag",
                "cst-keep",
                "--retry-lock",
                "30m",
            ],
        )
        self.assertNotIn("--prune", rc[2]["args"])
        self.assertEqual(self.index_entries()[0]["keep"], "pre-f46")
        self.assertIn("kept until removed by hand", r.stdout)

    def test_forgotten_snapshots_leave_the_index(self) -> None:
        os.makedirs(os.path.dirname(self.index))
        local = os.path.join(self.tmp, "local-backup")
        os.makedirs(local)
        old = [
            {
                "vm": "win",
                "mode": "nas",
                "snapshot": "old1",
                "repository": "sftp:btrfs-setup-nas:/volume1/restic",
                "epoch": 1,
                "timestamp": "a",
            },
            {
                "vm": "win",
                "mode": "nas",
                "snapshot": "kept",
                "repository": "sftp:btrfs-setup-nas:/volume1/restic",
                "epoch": 2,
                "timestamp": "b",
            },
            {"vm": "win", "mode": "reflink", "path": local, "epoch": 3, "timestamp": "c"},
        ]
        with open(self.index, "w") as f:
            json.dump({"backups": old}, f)
        self.restic_cfg(snapshots=["kept", SNAP_ID])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e.get("snapshot") or e.get("path") for e in self.index_entries()], ["kept", local, SNAP_ID])

    def test_an_index_of_a_newer_cst_vm_stops_the_nas_backup_before_restic(self) -> None:
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        with open(self.index, "w") as f:
            json.dump({"version": 2, "backups": []}, f)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{self.index} is format 2 of the vm-index (this CST reads format 1 and older)", r.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("restic ") or "snapshot-create" in c])

    def test_list_shows_nas_entries(self) -> None:
        self.assertEqual(self.run_tool("backup", "win", "--nas").returncode, 0)
        r = self.run_tool("list")
        self.assertIn(f"win              {STAMP}  nas      deployment 45.1", r.stdout)
        self.assertIn("restic a1b2c3d4 (quiesced)", r.stdout)

    def test_a_local_backup_keeps_the_nas_entries(self) -> None:
        self.assertEqual(self.run_tool("backup", "win", "--nas").returncode, 0)
        entries = [e for e in vm_adapters.read_index(self.index)]
        self.assertEqual(entries[0]["mode"], "nas")


class RobustnessTest(Nas):
    """The merge back survives what a manual run meets: a closed terminal, a
    reader that went away, Ctrl-C pressed twice, a paused VM, a virsh that
    does not answer."""

    def popen_tool(
        self,
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        text: bool = False,
        start_new_session: bool = False,
    ) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
        if text:
            return subprocess.Popen(
                ["python3", "-I", TOOL, "backup", "win", "--nas"],
                env=self.env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=start_new_session,
            )
        return subprocess.Popen(
            ["python3", "-I", TOOL, "backup", "win", "--nas"],
            env=self.env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )

    def wait_for(self, name: str) -> None:
        path = os.path.join(self.fix, name)
        for _ in range(400):
            if os.path.exists(path):
                return
            time.sleep(0.05)
        self.fail(f"{name} never appeared")

    def assert_merged(self) -> None:
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertFalse(os.path.exists(self.overlay))

    def test_closed_stdout_still_merges(self) -> None:
        r, w = os.pipe()
        os.close(r)  # `| head` that is gone: EPIPE on the first write
        p = self.popen_tool(stdin=subprocess.DEVNULL, stdout=w, stderr=subprocess.DEVNULL)
        os.close(w)
        self.assertEqual(p.wait(60), 0)
        self.assert_merged()
        self.assertEqual(len(self.index_entries()), 1)

    def test_closed_terminal_during_restic_still_merges(self) -> None:
        self.vm(agent="connected", fail=["slow-backup"])
        master, slave = pty.openpty()

        def controlling_tty() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        p = subprocess.Popen(
            ["python3", "-I", TOOL, "backup", "win", "--nas"],
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=controlling_tty,
            pass_fds=(slave,),
        )
        os.close(slave)
        self.wait_for("backup-started")
        os.read(master, 65536)
        os.close(master)  # the terminal window closes: SIGHUP, then EIO on every write
        self.assertEqual(p.wait(60), 1)
        self.assert_merged()
        self.assertFalse(os.path.exists(self.index))

    def test_commit_failure_with_closed_stderr_still_alerts(self) -> None:
        self.vm(agent="connected", fail=["commit"])
        r, w = os.pipe()
        os.close(r)
        p = self.popen_tool(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=w)
        os.close(w)
        self.assertEqual(p.wait(60), 1)
        self.assertTrue([c for c in self.calls() if c.startswith("notify -u critical -a cst-vm")])
        self.assertTrue(os.path.exists(self.overlay))

    def test_ctrl_c_twice_does_not_cancel_the_merge(self) -> None:
        self.vm(agent="connected", fail=["slow-backup", "commit-sigint"])
        p = self.popen_tool(
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
        )
        self.wait_for("backup-started")
        os.killpg(p.pid, signal.SIGINT)  # Ctrl-C: the whole foreground group
        self.wait_for("commit-started")
        os.killpg(p.pid, signal.SIGINT)  # again, while virsh merges
        out, err = p.communicate(timeout=60)
        assert isinstance(out, str) and isinstance(err, str)
        self.assertEqual(p.returncode, 1, out + err)
        self.assertIn("stopped by signal SIGINT", err)
        self.assertNotIn("DISK NOT MERGED BACK", err)
        self.assert_merged()

    def test_paused_vm_is_merged_back(self) -> None:
        self.vm(agent="connected", fail=["pause-during-backup"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("win is paused: merging back anyway", r.stdout)
        self.assert_merged()
        self.assertNotIn("qemu-img", r.stdout + r.stderr)

    def short_snapshot_limits(self) -> None:
        self.env.update(CST_VM_SNAPSHOT_TIMEOUT="2", CST_VM_SNAPSHOT_SETTLE="3")

    def test_snapshot_timeout_then_in_place(self) -> None:
        self.short_snapshot_limits()
        self.vm(agent="connected", fail=["snapshot-hang"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.virsh_calls("snapshot-create-as")), 1, "no second attempt after a timeout")
        self.assertTrue(self.virsh_calls("domfsthaw"))
        self.assertEqual(self.index_entries()[0]["consistency"], "crash-consistent")
        self.assert_merged()

    def test_snapshot_timeout_unclear_keeps_the_files_and_alerts(self) -> None:
        self.short_snapshot_limits()
        self.vm(agent="connected", fail=["snapshot-hang-nothing"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("may still switch to it", r.stderr)
        self.assertEqual(len(self.virsh_calls("snapshot-create-as")), 1)
        self.assertTrue(os.path.exists(self.overlay), "never removed while libvirt may still use it")
        self.assertEqual(self.virsh_calls("blockcommit"), [])
        self.assertNotIn("backup", [c["args"][0] for c in self.restic_calls()])
        self.assertTrue([c for c in self.calls() if c.startswith("notify -u critical -a cst-vm")])
        self.assertIn("domblklist win --details --inactive", r.stderr)

    def test_nvram_written_during_the_read_is_noted(self) -> None:
        self.vm(agent="connected", fail=["touch-nvram"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"note: {self.nvram} changed while restic read it", r.stdout)

    def test_recovery_kinds(self) -> None:
        # State unreadable: only commands that look.
        lines = vm_domain.overlay_recovery("win", [("sda", (self.disk, self.overlay))], {})
        self.assertFalse(
            [
                ln
                for ln in lines
                if "qemu-img" in ln or "blockcommit" in ln or " edit " in ln or ln.startswith("sudo rm")
            ]
        )
        self.assertEqual(vm_domain.overlay_kind(None, None, self.disk), "unknown")
        self.assertEqual(vm_domain.overlay_kind("pmsuspended", self.overlay, self.disk), "live")
        self.assertEqual(vm_domain.overlay_kind("paused", None, self.disk), "live")
        self.assertEqual(vm_domain.overlay_kind("running", self.disk, self.disk), "definition")
        # Shut off in the run: no merge happened, the overlay holds the last writes.
        self.assertEqual(vm_domain.overlay_kind("shut off", self.overlay, self.disk), "offline")


class ShutOffAndRefusalTest(Nas):
    def test_shut_off_vm_is_read_directly(self) -> None:
        self.vm(state="shut off")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.virsh_calls("snapshot-create-as"), [])
        self.assertEqual(self.virsh_calls("blockcommit"), [])
        self.assertIn("virsh -c qemu:///system list --all --with-managed-save --name", self.calls())
        self.assertEqual(self.index_entries()[0]["consistency"], "shut-off")
        calls = self.calls()
        last_state = max(i for i, c in enumerate(calls) if c.startswith("virsh -c qemu:///system domstate"))
        self.assertGreater(
            last_state,
            calls.index(next(c for c in calls if c.startswith("restic backup"))),
            "checked again after restic",
        )

    def test_shut_off_vm_started_during_the_read(self) -> None:
        self.vm(state="shut off", fail=["start-during-backup"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not consistent", r.stderr)
        forgets = [c["args"] for c in self.restic_calls() if c["args"][0] == "forget"]
        self.assertEqual(forgets, [["forget", "--retry-lock", "30m", SNAP_ID]])
        self.assertFalse(os.path.exists(self.index))

    def test_refused_states(self) -> None:
        for state in ("paused", "pmsuspended", "in shutdown"):
            self.vm(state=state)
            r = self.run_tool("backup", "win", "--nas")
            self.assertEqual(r.returncode, 1, state)
            self.assertIn(f"win is {state}: the NAS backup reads a running VM", r.stderr)
        self.vm(state="shut off", managed=["win"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("managed save", r.stderr)
        self.assertEqual(self.restic_calls(), [])

    def test_leftover_overlay_is_refused(self) -> None:
        left = f"{self.disk}.cst-overlay-20261101T000000Z"
        self.vm(
            live={"sda": left, "sdb": "/iso/virtio.iso"}, persistent={"sda": left, "sdb": "/iso/virtio.iso"}, chain=True
        )
        with open(left, "w") as f:
            f.write("x")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("an overlay a NAS backup left behind", r.stderr)
        self.assertEqual(self.virsh_calls("snapshot-create-as"), [])

    def test_unusable_repository_before_any_snapshot(self) -> None:
        for rc, text in (
            (12, "does not open the repository"),
            (10, "no restic repository"),
            (124, "did not answer"),
            (11, "the repository is locked"),
        ):
            self.restic_cfg(cat_rc=rc)
            r = self.run_tool("backup", "win", "--nas")
            self.assertEqual(r.returncode, 1)
            self.assertIn(text, r.stderr)
            self.assertIn("no snapshot taken", r.stderr)
        self.assertEqual(self.virsh_calls("snapshot-create-as"), [])

    def test_nas_not_set_up(self) -> None:
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=0\n")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cst btrfs snapshots nas setup", r.stderr)
        self.assertEqual(self.calls(), [])
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/x\n")
        os.unlink(self.password)
        r = self.run_tool("backup", "win", "--nas")
        self.assertIn("missing or empty", r.stderr)

    def test_needs_root_and_arguments(self) -> None:
        env = dict(self.env)
        del env["CST_VM_NONROOT_OK"]
        r = subprocess.run(
            ["python3", "-I", TOOL, "backup", "win", "--nas"],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if os.geteuid() != 0:
            self.assertEqual(r.returncode, 1)
            self.assertIn("needs root", r.stderr)
        a = vm_domain.parse_args(["backup", "--keep-tag", "pre-f46", "--nas", "w"])
        self.assertEqual((a["vm"], a["keep"], a["nas"]), ("w", "pre-f46", True))
        self.assertEqual(vm_domain.parse_args(["backup", "--nas", "--keep-tag=x1"])["keep"], "x1")
        for bad in (
            ["backup", "--nas", "--full"],
            ["backup", "--keep-tag", "x"],
            ["backup", "--no-quiesce"],
            ["backup", "--nas", "--keep-tag", "a,b"],
            ["backup", "--nas", "--keep-tag", "cst-keep"],
            ["backup", "--nas", "--keep-tag"],
            ["backup-timer"],
            ["backup-timer", "go"],
        ):
            with self.assertRaises(Usage, msg=bad):
                vm_domain.parse_args(bad)

    def test_scheduled_failure_alerts(self) -> None:
        self.restic_cfg(cat_rc=12)
        self.env["INVOCATION_ID"] = "x"
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertTrue([c for c in self.calls() if c.startswith("notify -u normal -a cst-vm Weekly VM backup")])


class SharedRepositoryTest(Nas):
    """cst-vm's NAS backup and the /home NAS backup of btrfs/btrfs-snapshot share one restic repository."""

    def test_both_backups_give_restic_the_same_repository_password_and_cache(self) -> None:
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        vm_calls = self.restic_calls()
        vm_env = vm_calls[1]["env"]
        os.remove(os.path.join(self.fix, "restic.log"))
        # btrfs-snapshot's nas and nas_maintain, run for real with the same
        # settings and the same fake restic; the steps around them are stubs
        # (nas_pin failing ends the backup part after `restic unlock`).
        script = "\n".join(
            [
                "set -Euo pipefail",
                'log() { echo "<$1>btrfs-snapshot: $2"; }',
                'nas_stale_alert() { echo "stale: $*"; }',
                "alert() { :; }; clear_alert() { :; }; nas_summary() { :; }",
                "nas_unmount() { return 0; }; nas_reachable() { return 0; }; nas_pin() { return 1; }",
                "due() { return 1; }",
                "time_left() { echo $(( NAS_DEADLINE - $(date +%s) )); }",
                *(
                    shell_function(SNAPSHOT_RUNTIME, name)
                    for name in (
                        "duration_s",
                        "restic_run",
                        "nas_repo_ready",
                        "nas_repo_records",
                        "nas",
                        "nas_maintain",
                    )
                ),
                'RESTIC_RUN_ENV=(); NAS_REPO_ID=""',
                'NAS_ENABLED=1 NAS_PATH="$1" RESTIC_PASSWORD="$2" RESTIC_CACHE="$3" NAS_SSH_ALIAS="$4"',
                'STATE="$5" NAS_LOCK="$5/nas.lock" NAS_TIMEOUT=6h NAS_RESTIC_HOST=sophia',
                "NAS_KEEP_DAILY=14 NAS_KEEP_WEEKLY=8 NAS_KEEP_MONTHLY=12 NAS_USER=u NAS_HOST=h NAS_PORT=22",
                "NAS_PRUNE_EVERY_DAYS=7 NAS_CHECK_EVERY_DAYS=30 NAS_CHECK_SUBSET=5%",
                "nas",
                "NAS_DEADLINE=$(( $(date +%s) + 3600 ))",
                "nas_maintain",
            ]
        )
        state = os.path.join(self.tmp, "snapshot-state")
        os.makedirs(state)
        b = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "_",
                "/volume1/restic",
                self.password,
                os.path.join(self.tmp, "cache"),
                vm_domain.NAS_SSH_ALIAS,
                state,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        home_calls = self.restic_calls()
        self.assertEqual([c["args"][0] for c in home_calls], ["cat", "unlock", "forget"], b.stdout + b.stderr)
        for call in home_calls:
            self.assertEqual(call["env"], vm_env, call["args"])
        # Each forgets only its own tag: the VM backups are never in the /home job's groups.
        forget = home_calls[2]["args"]
        self.assertEqual(forget[forget.index("--tag") + 1], "btrfs-setup")
        vm_forget = next(c["args"] for c in vm_calls if c["args"][0] == "forget")
        self.assertEqual(vm_forget[vm_forget.index("--tag") + 1], "cst-vm,vm:win")


class ConstantsTest(unittest.TestCase):
    def test_nas_default_paths_match_btrfs_snapshot(self) -> None:
        # The defaults themselves: btrfs-snapshot cannot run unprivileged (its
        # settings, password and lock are root paths), so this one check still
        # reads its assignments. It goes when btrfs-snapshot is converted to
        # Python and imports these values.
        with open(SNAPSHOT_RUNTIME) as f:
            text = f.read()

        def shell(name: str) -> str:
            m_re = re.search(rf"^{name}=(\S+)", text, re.M)
            assert m_re is not None
            return m_re.group(1)

        self.assertEqual(shell("NAS_SSH_ALIAS"), vm_domain.NAS_SSH_ALIAS)
        self.assertEqual(shell("RESTIC_PASSWORD"), "/etc/btrfs-setup/restic-password")
        self.assertEqual(shell("RESTIC_CACHE"), "/var/cache/btrfs-snapshot-restic")
        self.assertEqual(shell("CONF"), "/etc/btrfs-setup/snapshots.conf")

    def test_forget_args_are_scoped(self) -> None:
        a = vm_domain.forget_args("win", "sophia", 4, 3)
        self.assertEqual(a[a.index("--host") + 1], "sophia")
        self.assertEqual(a[a.index("--tag") + 1], "cst-vm,vm:win")
        self.assertNotIn("--prune", a)


class TimerTest(Nas):
    def test_on_with_a_record_of_a_newer_cst_is_refused_with_a_message(self) -> None:
        record = self.env["CST_VM_INSTALL_RECORD"]
        os.makedirs(os.path.dirname(record))
        with open(record, "w") as f:
            f.write('{"format": 99}')
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("written by a newer CST", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(self.env["CST_VM_SBIN_COPY"]))

    def test_on_status_off(self) -> None:
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("weekly NAS backup of win: off", r.stdout)
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("systemctl daemon-reload", self.calls())
        self.assertIn("systemctl enable --now cst-vm-backup@win.timer", self.calls())
        service = read(os.path.join(self.units, "cst-vm-backup@.service"))
        timer = read(os.path.join(self.units, "cst-vm-backup@.timer"))
        copy = self.env["CST_VM_SBIN_COPY"]
        for line in (
            f"ExecStart=/usr/bin/python3 -I {copy} backup %i --nas",
            "Nice=19",
            "IOSchedulingClass=idle",
            "CPUWeight=20",
            "IOWeight=20",
            "KillMode=mixed",
            "Type=oneshot",
            "TimeoutStartSec=infinity",
        ):
            self.assertIn(line + "\n", service)
        # systemd never kills a merge half done: the stop budget covers the merge's own limit.
        stop = re.search(r"^TimeoutStopSec=(\d+)min$", service, re.M)
        self.assertTrue(stop, service)
        assert stop is not None
        self.assertGreaterEqual(
            int(stop.group(1)) * 60,
            vm_adapters.SNAPSHOT_TIMEOUT + vm_adapters.SNAPSHOT_SETTLE + vm_adapters.COMMIT_TIMEOUT + 300 + 15 * 60,
        )
        for line in (
            "OnCalendar=Sat *-*-* 04:30:00",
            "RandomizedDelaySec=30min",
            "Persistent=true",
            "WantedBy=timers.target",
        ):
            self.assertIn(line + "\n", timer)
        # The program as `install` writes it: the zipapp built from this checkout.
        with open(copy, "rb") as f:
            self.assertEqual(f.read(), build_python_zipapp(REPO, "vm/cst-vm"))
        self.assertEqual(stat.S_IMODE(os.stat(copy).st_mode), 0o755)
        # The three files are in the install record, the component marked done; a second on changes neither.
        record_path = self.env["CST_VM_INSTALL_RECORD"]
        rec = json.loads(read(record_path))
        service_path = os.path.join(self.units, "cst-vm-backup@.service")
        timer_path = os.path.join(self.units, "cst-vm-backup@.timer")
        self.assertEqual(sorted(rec["files"]), sorted([copy, service_path, timer_path]))
        self.assertEqual(rec["files"][copy]["src"], "vm/cst-vm")
        self.assertEqual(rec["files"][timer_path]["kind"], "text")
        self.assertEqual(rec["components"], {"vm:backup-timer": "done"})
        before = read(record_path)
        r = self.run_tool("backup-timer", "on", "win")
        self.assertNotIn("installed:", r.stdout)
        self.assertEqual(read(record_path), before)
        r = self.run_tool("backup-timer", "status", "win")
        self.assertIn("weekly NAS backup of win: on", r.stdout)
        self.assertIn("installed, up to date", r.stdout)
        self.assertIn("next run: Sat 2026-11-21", r.stdout)
        # The installed copy runs on its own, from /, and sees itself as up to date.
        r = subprocess.run(
            ["/usr/bin/python3", "-I", copy, "backup-timer", "status", "win"],
            check=False,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/",
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"  {copy}: installed, up to date", r.stdout)
        with open(copy, "a") as f:
            f.write("# old\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertIn("OLDER than this copy of CST", r.stdout)
        r = self.run_tool("status", "win")
        self.assertIn("weekly NAS backup timer: on", r.stdout)
        # Neither on nor status forgets the timer's last run.
        self.assertNotIn("clean", " ".join(self.calls()))
        r = self.run_tool("backup-timer", "off", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        # disable --now, then clean --what=state (the Persistent= stamp) while the timer file is still
        # there (clean needs the unit loaded), then the files go.
        calls = self.calls()
        at = calls.index("systemctl disable --now cst-vm-backup@win.timer")
        self.assertEqual(
            calls[at : at + 3],
            [
                "systemctl disable --now cst-vm-backup@win.timer",
                "systemctl clean --what=state cst-vm-backup@win.timer",
                "  (timer file present)",
            ],
        )
        self.assertFalse(os.path.exists(copy))
        self.assertFalse(os.path.exists(os.path.join(self.units, "cst-vm-backup@.service")))
        # Off removed them from the install record too (cst doctor would call them missing).
        self.assertEqual(json.loads(read(record_path)), {"format": 1, "files": {}, "components": {}})

    def test_off_goes_on_when_clean_fails(self) -> None:
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.bin, "systemctl"), "w") as f:
            f.write(SYSTEMCTL.replace('case "$1" in', 'case "$1" in\n  clean) exit 1 ;;', 1))
        r = self.run_tool("backup-timer", "off", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("systemctl clean --what=state cst-vm-backup@win.timer", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.units, "cst-vm-backup@.timer")))

    def test_status_calls_a_service_without_its_journal_name_older(self) -> None:
        # The service as an older CST wrote it, before SyslogIdentifier=cst-vm (its journal lines read
        # "python3"): status says it is older, and on rewrites it.
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        service = os.path.join(self.units, "cst-vm-backup@.service")
        new = read(service)
        self.assertIn("\nSyslogIdentifier=cst-vm\n", new)
        with open(service, "w") as f:
            f.write(new.replace("SyslogIdentifier=cst-vm\n", ""))
        r = self.run_tool("backup-timer", "status", "win")
        self.assertIn(f"  {service}: installed, OLDER than this copy of CST", r.stdout)
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"  updated: {service}", r.stdout)
        self.assertEqual(read(service), new)

    def test_on_upgrades_a_plain_script_copy_and_refuses_a_foreign_program(self) -> None:
        copy = self.env["CST_VM_SBIN_COPY"]
        os.makedirs(os.path.dirname(copy))
        # What CST installed before the zipapps: the script itself, as text.
        with open(copy, "w") as f:
            f.write('#!/usr/bin/python3 -I\n"""cst-vm: copies of a libvirt VM (an older CST)."""\n')
        r = self.run_tool("backup-timer", "status", "win")
        self.assertIn(f"  {copy}: installed, OLDER than this copy of CST", r.stdout)
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"  updated: {copy}", r.stdout)
        with open(copy, "rb") as f:
            self.assertEqual(f.read(), build_python_zipapp(REPO, "vm/cst-vm"))
        r = self.run_tool("backup-timer", "on", "win")
        self.assertNotIn("installed:", r.stdout)
        self.assertNotIn("updated:", r.stdout)
        with open(copy, "w") as f:
            f.write("#!/usr/bin/python3 -I\nprint('someone else')\n")
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"not CST's files, left as they are: {copy}", r.stderr)

    def test_a_link_at_a_root_destination_is_replaced_not_followed(self) -> None:
        # The installer's one link rule (cst_core.domain.install.link_read_through): at a root
        # destination a link is replaced, whatever it points to; it was "not CST's file" before.
        elsewhere = os.path.join(self.tmp, "elsewhere.timer")
        with open(elsewhere, "w") as f:
            f.write("[Timer]\n")
        timer = os.path.join(self.units, "cst-vm-backup@.timer")
        os.symlink(elsewhere, timer)
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"  updated: {timer}", r.stdout)
        self.assertFalse(os.path.islink(timer))
        self.assertIn("Persistent=true\n", read(timer))
        self.assertEqual(read(elsewhere), "[Timer]\n", "the link was written through")

    def test_a_masked_unit_refuses_on_and_is_left_by_off(self) -> None:
        # `systemctl mask` leaves a link to /dev/null where the unit file goes. The installer's link
        # rule replaces a link at a root destination, so `on` unmasked the unit without a word.
        timer = os.path.join(self.units, "cst-vm-backup@.timer")
        os.symlink("/dev/null", timer)
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"  {timer}: masked (systemctl mask): unmask it first", r.stdout)
        open(self.log, "w").close()
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(
            "cst-vm-backup@.timer is masked (systemctl mask); unmask it first: "
            "sudo systemctl unmask cst-vm-backup@.timer; nothing was changed",
            r.stderr,
        )
        self.assertEqual(os.readlink(timer), "/dev/null")
        self.assertFalse(os.path.lexists(os.path.join(self.units, "cst-vm-backup@.service")))
        self.assertFalse(os.path.lexists(self.env["CST_VM_SBIN_COPY"]))
        self.assertEqual(self.calls(), [], "systemctl was called")
        # off turns the VM's timer off and removes CST's files, never the user's mask.
        os.makedirs(os.path.dirname(self.env["CST_VM_SBIN_COPY"]))
        with open(self.env["CST_VM_SBIN_COPY"], "wb") as f:
            f.write(build_python_zipapp(REPO, "vm/cst-vm"))
        r = self.run_tool("backup-timer", "off", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"  left as it is: {timer} (masked with systemctl mask)", r.stdout)
        self.assertIn(f"  removed: {self.env['CST_VM_SBIN_COPY']}", r.stdout)
        self.assertEqual(os.readlink(timer), "/dev/null")

    def test_a_masked_timer_of_the_vm_refuses_on(self) -> None:
        # `systemctl mask cst-vm-backup@win.timer` masks that VM only: a link CST never writes.
        # `on` wrote the template files and only then failed at `enable`.
        mask = os.path.join(self.units, "cst-vm-backup@win.timer")
        os.symlink("/dev/null", mask)
        r = self.run_tool("backup-timer", "status", "win")
        self.assertIn(f"  {mask}: masked (systemctl mask)", r.stdout)
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cst-vm-backup@win.timer is masked (systemctl mask); unmask it first", r.stderr)
        self.assertEqual(sorted(os.listdir(self.units)), ["cst-vm-backup@win.timer"], "files were written")
        # A link to /dev/null through another link is a mask too (systemd follows the chain).
        os.remove(mask)
        hop = os.path.join(self.tmp, "hop")
        os.symlink("/dev/null", hop)
        os.symlink(hop, os.path.join(self.units, "cst-vm-backup@.service"))
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cst-vm-backup@.service is masked (systemctl mask)", r.stderr)

    def test_a_problem_line_fails_the_weekly_run_and_alerts(self) -> None:
        # The service runs `cst-vm backup VM --nas`: a settings line that is not a plain KEY=VALUE
        # must end it non-zero (no SuccessExitStatus=: the unit is failed, which cst doctor lists)
        # and send the alert, naming the file and the line.
        self.assertNotIn("SuccessExitStatus", vm_domain.render_service("/usr/local/sbin/cst-vm", "40min"))
        with open(self.conf, "a") as f:
            f.write("NAS_PATH=`hostname`\n")
        self.env["INVOCATION_ID"] = "x"
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"{self.conf} line 4: NAS_PATH=`hostname`", r.stderr)
        alerts = [c for c in self.calls() if c.startswith("notify ")]
        self.assertEqual(len(alerts), 1, self.calls())
        self.assertIn("Weekly VM backup of win to the NAS failed", alerts[0])
        self.assertIn(f"{self.conf} line 4: NAS_PATH=`hostname`", alerts[0])
        self.assertIn("journalctl -u cst-vm-backup@win.service", alerts[0])

    def test_settings_are_read_as_bash_reads_them_and_a_problem_line_refuses_the_backup(self) -> None:
        # Snapshots.conf is sourced by btrfs-snapshot (bash); "NAS_ENABLED=1 # on" is 1 there,
        # but cst-vm read "1 # on" and called the NAS backup "not set up".
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=1 # on\nNAS_PATH='/volume1/restic'\nNAS_RESTIC_HOST=sophia\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        self.assertNotIn("not set up", r.stdout)
        # A line bash reads differently (it runs NAS_PATH as a command): status says so and goes on,
        # a backup refuses before it touches the VM (never a default or a guessed value).
        with open(self.conf, "a") as f:
            f.write("NAS_PATH = /volume2/other\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{self.conf} line 4: NAS_PATH = /volume2/other: blanks around =", r.stderr)
        open(self.log, "w").close()
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"{self.conf} line 4: NAS_PATH = /volume2/other", r.stderr)
        self.assertIn("nothing was changed", r.stderr)
        self.assertEqual(self.calls(), [], "the backup ran commands")
        cpu = self.env["CST_VM_CPU_CONF"]
        with open(cpu, "w") as f:
            f.write("VM_NAME=$HOSTNAME\n")  # bash would expand it
        r = self.run_tool("backup")  # no VM named: VM_NAME would choose it
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"{cpu} line 1: VM_NAME=$HOSTNAME: $", r.stderr)
        self.assertEqual(self.calls(), [])
        # A backup of a named VM reads nothing from cpu-setup.conf: a line there does not stop it
        # (the weekly timer names the VM).
        r = self.run_tool("backup", "win")
        self.assertNotIn(cpu, r.stderr)
        self.assertNotEqual(self.calls(), [], "the backup of a named VM did not start")
        # The way back is never blocked: off reads no setting; it prints the lines and turns the timer off.
        r = self.run_tool("backup-timer", "off", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{self.conf} line 4: NAS_PATH = /volume2/other", r.stderr)
        self.assertIn("Off: cst-vm-backup@win.timer", r.stdout)

    def test_lines_an_older_cst_wrote_with_printf_q_are_read_as_bash_reads_them(self) -> None:
        # snap_conf_set wrote values with printf %q until values went in quotes: a\ b is "a b" for
        # btrfs-snapshot (bash), so the backup reads it the same way instead of refusing.
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/volume1/My\\ restic\nNAS_RESTIC_HOST=$'sophia\\x2dpc'\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.conf, r.stderr)
        backup = self.restic_calls()[1]
        self.assertEqual(backup["env"]["RESTIC_REPOSITORY"], "sftp:btrfs-setup-nas:/volume1/My restic")
        self.assertEqual(backup["args"][2:4], ["--host", "sophia-pc"])
        # Only snapshots.conf: the same line in vm.conf is still a problem there.
        with open(self.env["CST_VM_CONF"], "w") as f:
            f.write("VM_TUNE_NIC=a\\ b\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertIn("vm.conf line 1: VM_TUNE_NIC=a\\ b", r.stderr)

    def test_on_refuses_without_nas_and_foreign_files(self) -> None:
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=0\n")
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not set up", r.stderr)
        with open(os.path.join(self.units, "cst-vm-backup@.timer"), "w") as f:
            f.write("[Timer]\n")
        r = self.run_tool("backup-timer", "on", "win")
        self.assertIn("not CST's files", r.stderr)
        self.assertNotIn("systemctl enable", "\n".join(self.calls()))


class SetupRoutingTest(unittest.TestCase):
    def test_backup_timer_routing(self) -> None:
        setup = os.path.join(REPO, "vm-setup.sh")
        env = {"PATH": "/usr/bin:/bin", "CST_PROG": "cst vm", "CST_VM_CONTAINER": "1"}
        r = subprocess.run(
            ["bash", setup, "backup-timer", "go"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("backup-timer on|off|status", r.stderr)
        for args in (("backup-timer", "on", "w"), ("backup", "w", "--nas")):
            r = subprocess.run(
                ["bash", setup, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("must run on the host", r.stderr)
        r = subprocess.run(
            ["bash", setup, "help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        self.assertIn("backup [VM] --nas [--keep-tag NAME] [--no-quiesce]", r.stdout)


if __name__ == "__main__":
    unittest.main()
