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

from collections.abc import Sequence
import contextlib
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
from typing import Any, override
import unittest

# Run directly (python3 -I -B vm/tests/test_vm_nas.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.errors import Refused, Usage
from cst_core.domain.shapes import items_of
from btrfs.tests.snapshot_fakes import RuntimeTest
from vm import domain as vm_domain
from vm.adapters import backup as vm_adapters_backup
from vm.domain import backup as vm_domain_backup
from vm import wiring as vm_wiring

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
VM_DIR = os.path.dirname(HERE)
TOOL = os.path.join(VM_DIR, "cst-vm")
REPO = os.path.dirname(VM_DIR)
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
# Another NAS backup holding the torn record's lock (argv: the lock, cst-vm's pid, a file made once it
# holds it): it sends cst-vm a SIGTERM once cst-vm has the lock file open (waiting for it), then lets go.
HOLDER = '''
import fcntl, os, signal, sys, time
lock, pid, ready = sys.argv[1], int(sys.argv[2]), sys.argv[3]
fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
open(ready, "w").close()
def waiting():
    try:
        return any(os.readlink(f"/proc/{pid}/fd/{n}") == lock for n in os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return False
deadline = time.monotonic() + 20
while not waiting() and time.monotonic() < deadline:
    time.sleep(0.02)
os.kill(pid, signal.SIGTERM)
time.sleep(0.5)
'''
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
    if "write_index" in cfg:  # [path, text]: a newer cst vm rewrote the index meanwhile
        open(cfg["write_index"][0], "w").write(cfg["write_index"][1])
    if "hold_torn_lock" in cfg:  # the torn record's lock, held by another backup when the record comes
        import subprocess, time
        os.makedirs(os.path.dirname(cfg["hold_torn_lock"]), exist_ok=True)
        ready = os.path.join(fix, "torn-lock-held")
        subprocess.Popen([sys.executable, "-I", "-c", HOLDER, cfg["hold_torn_lock"], str(os.getppid()), ready],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        deadline = time.monotonic() + 20
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.02)
    if "break_torn" in cfg:  # the torn record's path becomes a folder: it can no longer be written
        if os.path.isfile(cfg["break_torn"]):
            os.unlink(cfg["break_torn"])
        os.makedirs(cfg["break_torn"])
    for line in cfg.get("backup_lines", []):
        print(line)
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
    one = "--keep-weekly" not in args  # one snapshot by its id, not the retention
    if one and "term-during-forget" in vm.get("fail", []):
        import signal, time
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(30)
    if one and "freeze_torn_on_forget" in cfg:  # the torn record's folder can no longer be written
        os.chmod(cfg["freeze_torn_on_forget"], 0o555)
    print("remove 1 snapshots:" if cfg.get("forgot") else "keep 2 snapshots:")
    if "forget_err" in cfg:
        print(cfg["forget_err"], file=sys.stderr)
    sys.exit(cfg.get("forget_rc_by_id", {}).get(args[-1], cfg.get("forget_rc", 0)))
if cmd == "snapshots":
    host = args[args.index("--host") + 1] if "--host" in args else None
    if "snapshots_by_host" in cfg:  # {host: answer}: restic lists only the snapshots made on --host
        print(cfg["snapshots_by_host"].get(host, "[]"))
    else:
        print(cfg["snapshots_json"] if "snapshots_json" in cfg else json.dumps([{"id": i} for i in cfg.get("snapshots", [])]))
    sys.exit(cfg.get("snapshots_rc", 0))
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
  # Without --value, systemctl shows PROP=VALUE.
  show) shift; v="Sat 2026-11-21 04:41:12 UTC"; case " $* " in *" -p Result "*) v=success ;; esac
        case " $* " in *" --value "*) echo "$v" ;; *) echo "$2=$v" ;; esac ;;
  # clean needs the unit loaded: the log says whether the timer's file was still there.
  clean) [ -e "$UNIT_DIR/cst-vm-backup@.timer" ] && echo "  (timer file present)" >>"$STUB_LOG" ;;
esac
exit 0
"""


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


class Nas(unittest.TestCase):
    @override
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
        self.torn = os.path.join(self.tmp, "lib-torn", "vm-nas-torn.json")
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
            "CST_VM_TORN": self.torn,
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
                (
                    f"virsh -c qemu:///system snapshot-create-as win --name cst-nas-{STAMP} --disk-only --atomic "
                    f"--no-metadata --quiesce --diskspec sda,snapshot=external,file={self.overlay} "
                    "--diskspec sdb,snapshot=no"
                )
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

    def test_an_index_changed_during_the_backup_keeps_the_snapshot_and_says_it_is_not_indexed(self) -> None:
        # A newer cst vm rewrote the index while restic ran: the message said "nothing is backed up".
        text = json.dumps({"version": 2, "backups": []})
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        self.restic_cfg(write_index=[self.index, text])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            f"cst vm: the restic snapshot {SNAP_ID[:8]} of win is complete but not in the index: {self.index} is "
            "format 2 of the vm-index (this CST reads format 1 and older): written by a newer CST, so the index was "
            "left as it is and cst vm does not list this backup; the snapshot stays in the repository, where the "
            'retention sees it as any VM backup of its tags (restore it by its ID: vm/README.md, section "Restore '
            'from the NAS"). Then: use the cst vm of that CST',
            r.stderr,
        )
        self.assertNotIn("nothing is backed up", r.stderr)
        self.assertEqual([c["args"][0] for c in self.restic_calls()], ["cat", "backup", "forget"])
        self.assertFalse(os.path.exists(self.overlay))
        with open(self.index) as f:
            self.assertEqual(f.read(), text)

    def test_a_snapshot_not_indexed_still_prints_its_notes_and_how_to_restore_it(self) -> None:
        # The refusal came before the notes: a missing TPM state went unsaid, and restic keeps no
        # manifest with them.
        shutil.rmtree(os.path.join(self.swtpm, UUID))
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        self.restic_cfg(write_index=[self.index, json.dumps({"version": 2, "backups": []})])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("is complete but not in the index", r.stderr)
        self.assertIn("  note: TPM state", r.stdout)
        self.assertIn("BitLocker", r.stdout)
        self.assertIn(f"restic restore {SNAP_ID[:8]} --sparse", r.stdout)

    def test_an_incomplete_snapshot_restic_does_not_forget_is_named(self) -> None:
        # The forget's answer was dropped: a torn snapshot stayed in the repository unsaid. These runs
        # stop before the retention, whose forget forget_rc would fail too.
        self.restic_cfg(backup_rc=3, forget_rc=1)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            f"restic backup failed: some files could not be read; no backup recorded; restic snapshot {SNAP_ID[:8]} "
            f"could NOT be forgotten (exit 1): it is no backup; forget it by hand, restic forget {SNAP_ID}, with "
            'the repository\'s variables of vm/README.md, section "Restore from the NAS"',
            r.stderr,
        )
        self.assertFalse(os.path.exists(self.index))
        self.restic_cfg(backup_rc=3)
        r = self.run_tool("backup", "win", "--nas")
        self.assertIn("some files could not be read; no backup recorded\n", r.stderr)
        self.assertNotIn("forgotten", r.stderr)

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

    def test_restic_lines_of_another_shape_do_not_stop_the_backup(self) -> None:
        # A string "error" or a status whose numbers are text raised inside the loop: restic was
        # stopped and the backup failed.
        self.restic_cfg(
            backup_lines=[
                json.dumps({"message_type": "status", "percent_done": "half", "bytes_done": None, "total_bytes": [1]}),
                json.dumps({"message_type": "error", "error": "disk gone", "item": "/x"}),
                json.dumps({"message_type": "error", "error": {"message": "unreadable"}, "item": "/y"}),
                "[1, 2]",
            ]
        )
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("  restic: ? % (0.0 MiB of ? read)", r.stdout)
        self.assertIn("  restic error: /x disk gone", r.stderr)
        self.assertIn("  restic error: /y unreadable", r.stderr)
        self.assertEqual(self.index_entries()[-1]["snapshot"], SNAP_ID)

    def test_a_snapshot_id_that_is_not_text_is_no_snapshot(self) -> None:
        # str(123) became a snapshot to record, or to forget after an incomplete backup.
        for rc, why in ((0, "restic exit 0"), (3, "some files could not be read")):
            with self.subTest(rc=rc):
                self.restic_cfg(id=123, backup_rc=rc)
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"restic backup failed: {why}; no backup recorded", r.stderr)
                self.assertFalse([c for c in self.restic_calls() if c["args"][0] == "forget" and "123" in c["args"]])
                self.assertFalse(os.path.exists(self.index) and self.index_entries())

    def test_a_snapshot_list_of_another_shape_drops_no_index_entry(self) -> None:
        # Read as no snapshots, {} dropped every older NAS entry of the VM; an id that is a number
        # stopped the run after the backup, with the index not written.
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        old = {
            "vm": "win",
            "mode": "nas",
            "snapshot": "old1",
            "repository": "sftp:btrfs-setup-nas:/volume1/restic",
            "epoch": 1,
            "timestamp": "a",
        }
        for answer in ("{}", '[{"id": 5}]', '[{"id": "' + SNAP_ID + '"}, {"x": 1}]', '[{"id": ""}]', '"x"'):
            with self.subTest(answer=answer):
                with open(self.index, "w") as f:
                    json.dump({"backups": [old]}, f)
                self.restic_cfg(snapshots_json=answer)
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual([e.get("snapshot") for e in self.index_entries()], ["old1", SNAP_ID])

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

    def test_a_nas_entry_without_its_snapshot_is_kept_and_does_not_stop_the_prune(self) -> None:
        # It passed the reader (it has a path) and the prune read its snapshot: a KeyError after the backup.
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        odd = {"vm": "win", "mode": "nas", "path": "/p", "repository": "sftp:btrfs-setup-nas:/volume1/restic"}
        with open(self.index, "w") as f:
            json.dump({"backups": [odd]}, f)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.index_entries()[-1], odd)
        self.assertEqual(self.index_entries()[0]["snapshot"], SNAP_ID)

    def test_an_index_of_a_newer_cst_vm_stops_the_nas_backup_before_restic(self) -> None:
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        with open(self.index, "w") as f:
            json.dump({"version": 2, "backups": []}, f)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{self.index} is format 2 of the vm-index (this CST reads format 1 and older)", r.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("restic ") or "snapshot-create" in c])

    def test_a_nas_entry_has_the_fields_of_the_checked_in_index(self) -> None:
        # The fixture main's readers were shown to read (tests/fixtures/state-formats/vm-index-v1.json).
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(repo, "tests", "fixtures", "state-formats", "vm-index-v1.json")) as f:
            nas = next(e for e in json.load(f)["backups"] if e["mode"] == "nas")
        self.assertEqual(self.run_tool("backup", "win", "--nas").returncode, 0)
        [real] = [
            e
            for e in vm_adapters_backup.read_index(vm_wiring.vm_settings({"CST_VM_INDEX": self.index}))
            if e.get("mode") == "nas"
        ]
        self.assertEqual(set(real), set(nas))

    def test_list_shows_nas_entries(self) -> None:
        self.assertEqual(self.run_tool("backup", "win", "--nas").returncode, 0)
        r = self.run_tool("list")
        self.assertIn(f"win              {STAMP}  nas      deployment 45.1", r.stdout)
        self.assertIn("restic a1b2c3d4 (quiesced)", r.stdout)

    def test_a_local_backup_keeps_the_nas_entries(self) -> None:
        self.assertEqual(self.run_tool("backup", "win", "--nas").returncode, 0)
        entries = list(vm_adapters_backup.read_index(vm_wiring.vm_settings({"CST_VM_INDEX": self.index})))
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
        assert isinstance(out, str)
        assert isinstance(err, str)
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

    def test_a_torn_snapshot_restic_does_not_forget_is_named(self) -> None:
        # "(restic snapshot X forgotten)" was said whatever forget answered. These runs stop before the
        # retention, whose forget forget_rc would fail too.
        lock = "Fatal: unable to create lock in backend: repository is already locked"
        for fail, what in (
            ("start-during-backup", "the VM's state changed during the backup, the copy is not consistent ("),
            ("touch-nvram", "changed during the backup (the VM was started?), the copy is not consistent ("),
        ):
            for cfg, said in (
                ({"forget_rc": 1, "forget_err": lock}, f"could NOT be forgotten ({lock}): it is no backup; "),
                ({"forget_rc": 1}, "could NOT be forgotten (exit 1): it is no backup; "),
                ({}, "forgotten): "),
            ):
                with self.subTest(fail=fail, cfg=cfg):
                    self.vm(state="shut off", fail=[fail])
                    self.restic_cfg(**cfg)
                    r = self.run_tool("backup", "win", "--nas")
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(f"{what}restic snapshot {SNAP_ID[:8]} {said}", r.stderr)
                    if cfg:
                        self.assertIn(f"forget it by hand, restic forget {SNAP_ID}, with the repository's", r.stderr)
                        self.assertNotIn("forgotten)", r.stderr)
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
        self.assertEqual((a.vm, a.keep, a.nas), ("w", "pre-f46", True))
        self.assertEqual(vm_domain.parse_args(["backup", "--nas", "--keep-tag=x1"]).keep, "x1")
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
        # btrfs-snapshot's fake machine (btrfs/tests/snapshot_fakes.py): the whole bash program
        # runs there. cst-vm reads the same settings, password and restic cache below its root.
        home = RuntimeTest()
        self.addCleanup(home.doCleanups)  # also when setUp stops halfway
        home.setUp()
        home.conf(NAS_ENABLED="1", NAS_PATH="/volume1/restic", NAS_RESTIC_HOST="sophia")
        self.env.update(
            CST_VM_NAS_CONF=home.path("etc/btrfs-setup/snapshots.conf"),
            CST_VM_RESTIC_PASSWORD=home.write("etc/btrfs-setup/restic-password", "pw\n"),
            CST_VM_RESTIC_CACHE=home.path("var/cache/btrfs-snapshot-restic"),
        )
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        vm_calls = self.restic_calls()
        vm_env = vm_calls[1]["env"]
        home.machine(latest={"home": os.path.join(home.snapdir, "home.20261118T0905")})
        b = home.run_prog("nas")
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        home_calls = [(c[1:-2], c[-2]) for c in home.calls("restic")]
        self.assertEqual(
            [args[0] for args, _ in home_calls],
            ["cat", "unlock", "features", "backup", "forget", "check", "snapshots"],
            b.stdout + b.stderr,
        )
        for args, env in home_calls:
            # Only its own feature flag (backup) and output copy (check) come on top.
            assert isinstance(env, dict)
            own = {k: v for k, v in env.items() if k not in ("RESTIC_FEATURES", "RESTIC_CAPTURE")}
            self.assertEqual(own, vm_env, args)
        # Each forgets only its own tag: the VM backups are never in the /home job's groups.
        forget = home_calls[4][0]
        self.assertEqual(forget[forget.index("--tag") + 1], "btrfs-setup")
        vm_forget = next(c["args"] for c in vm_calls if c["args"][0] == "forget")
        self.assertEqual(vm_forget[vm_forget.index("--tag") + 1], "cst-vm,vm:win")


REPO_URL = "sftp:btrfs-setup-nas:/volume1/restic"
TORN_ID = "f00dbabe" + "1" * 56  # a torn snapshot of an earlier run (the good one of a run is SNAP_ID)
BOTH_TAGS = ["cst-vm", "vm:win"]


class TornRecordTest(Nas):
    """The restic snapshots a NAS backup could not forget (torn: incomplete, or not consistent) are
    recorded in CST_VM_TORN, and the next NAS backup of that VM forgets them before its retention, or
    skips the retention."""

    def torn_entry(
        self,
        snapshot: str = TORN_ID,
        *,
        vm: str = "win",
        repository: str = REPO_URL,
        timestamp: str = "20261101T010000Z",
        why: str = "incomplete",
    ) -> vm_domain_backup.TornEntry:
        return {
            "snapshot": snapshot,
            "vm": vm,
            "repository": repository,
            "host": "sophia",
            "timestamp": timestamp,
            "why": why,
        }

    def write_torn(self, entries: Sequence[object], version: int = 1) -> None:
        os.makedirs(os.path.dirname(self.torn), exist_ok=True)
        with open(self.torn, "w") as f:
            json.dump({"version": version, "torn": entries}, f)

    def torn_doc(self) -> object:
        with open(self.torn) as f:
            return json.load(f)

    def torn_list(self) -> list[object]:
        doc = self.torn_doc()
        torn = doc.get("torn") if isinstance(doc, dict) else None
        if not isinstance(torn, list):
            self.fail(f"no torn list in {doc!r}")
        return items_of(torn)

    def listed(self, *snaps: tuple[str, list[str] | None]) -> str:
        return json.dumps([{"id": i, **({} if tags is None else {"tags": tags})} for i, tags in snaps])

    def forgets(self) -> list[list[str]]:
        return [c["args"] for c in self.restic_calls() if c["args"][0] == "forget"]

    def test_a_torn_snapshot_restic_does_not_forget_is_recorded(self) -> None:
        for vm_kw, why in (
            ({"state": "running", "agent": "connected"}, "incomplete"),
            ({"state": "shut off", "fail": ["start-during-backup"]}, "state changed"),
            ({"state": "shut off", "fail": ["touch-nvram"]}, "changed during the read"),
        ):
            with self.subTest(why=why):
                shutil.rmtree(os.path.dirname(self.torn), ignore_errors=True)
                self.vm(**vm_kw)
                self.restic_cfg(backup_rc=3 if why == "incomplete" else 0, forget_rc=1)
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(
                    f"could NOT be forgotten (exit 1): it is no backup; forget it by hand, restic forget {SNAP_ID}, "
                    'with the repository\'s variables of vm/README.md, section "Restore from the NAS"; recorded in '
                    f"{self.torn}: the next NAS backup of win forgets it before its retention",
                    r.stderr,
                )
                doc = self.torn_doc()
                self.assertEqual(doc, {"version": 1, "torn": [self.torn_entry(SNAP_ID, timestamp=STAMP, why=why)]})
                self.assertEqual(stat.S_IMODE(os.stat(self.torn).st_mode), 0o644)
                self.assertEqual(os.listdir(os.path.dirname(self.torn)), ["vm-nas-torn.json"], "no temporary file")
                # The same snapshot torn again is recorded once.
                self.vm(**vm_kw)
                self.run_tool("backup", "win", "--nas")
                self.assertEqual(len(self.torn_list()), 1)

    def test_a_torn_snapshot_restic_forgets_leaves_nothing_in_the_record(self) -> None:
        self.restic_cfg(backup_rc=3)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("some files could not be read; no backup recorded\n", r.stderr)
        self.assertEqual(self.torn_doc(), {"version": 1, "torn": []})

    def test_a_torn_snapshot_that_cannot_be_recorded_says_so(self) -> None:
        # The record was fine at the start of the run, and cannot be written when the forget comes.
        self.restic_cfg(backup_rc=3, forget_rc=1, break_torn=self.torn)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"forget it by hand, restic forget {SNAP_ID}, with the repository's variables", r.stderr)
        self.assertIn(
            f"; it could NOT be recorded ({self.torn} cannot be read as cst vm's record of torn NAS snapshots (",
            r.stderr,
        )
        self.assertIn("forget it by hand before the next backup of win, whose retention would count it", r.stderr)
        self.assertEqual(self.forgets(), [["forget", "--retry-lock", "30m", SNAP_ID]], "the forget still ran")
        # restic forgets it: nothing more is said.
        os.rmdir(self.torn)
        self.restic_cfg(backup_rc=3, break_torn=self.torn)
        r = self.run_tool("backup", "win", "--nas")
        self.assertIn("some files could not be read; no backup recorded\n", r.stderr)

    def test_a_record_not_updated_after_a_good_forget_is_said_and_cleared_by_the_next_run(self) -> None:
        folder = os.path.dirname(self.torn)
        self.addCleanup(lambda: os.path.isdir(folder) and os.chmod(folder, 0o755))
        self.restic_cfg(backup_rc=3, freeze_torn_on_forget=folder)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            f"note: restic snapshot {SNAP_ID[:8]} is forgotten but still in {self.torn} (", r.stdout + r.stderr
        )
        self.assertIn("the next NAS backup of win drops it", r.stdout + r.stderr)
        os.chmod(folder, 0o755)
        self.assertEqual(self.torn_list(), [self.torn_entry(SNAP_ID, timestamp=STAMP)])
        # The next run: restic no longer lists it (it was forgotten), so it is dropped, never forgotten again.
        good = "c0ffee00" + "2" * 56
        self.restic_cfg(id=good, snapshots_json=self.listed((good, BOTH_TAGS)))
        os.unlink(os.path.join(self.fix, "restic.log"))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.torn_list(), [])
        self.assertEqual(len(self.forgets()), 1, "only the retention's")
        self.assertIn("--keep-weekly", self.forgets()[0])

    def test_a_stop_while_forget_waits_leaves_the_snapshot_recorded(self) -> None:
        self.vm(fail=["term-during-forget"], agent="connected")
        self.restic_cfg(backup_rc=3)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("stopped by signal SIGTERM", r.stderr)
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1, "merged back as after any stop")
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertEqual(self.torn_list(), [self.torn_entry(SNAP_ID, timestamp=STAMP)])

    def test_a_stop_while_the_record_waits_for_its_lock_leaves_the_snapshot_recorded(self) -> None:
        # Another backup holds the record's lock: the stop waits until the record is written, and
        # comes before the forget.
        self.vm(agent="connected")
        self.restic_cfg(backup_rc=3, hold_torn_lock=os.path.join(self.tmp, "run", "torn-record.lock"))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("stopped by signal SIGTERM", r.stderr)
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk, "merged back as after any stop")
        self.assertEqual(self.torn_list(), [self.torn_entry(SNAP_ID, timestamp=STAMP)])
        self.assertEqual(self.forgets(), [], "stopped before the forget")

    def test_a_stop_after_a_record_that_failed_names_the_snapshot_left(self) -> None:
        self.vm(fail=["term-during-forget"], agent="connected")
        self.restic_cfg(backup_rc=3, break_torn=self.torn)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup and could NOT be recorded ("
            f"{self.torn} cannot be read as cst vm's record of torn NAS snapshots (",
            r.stderr,
        )
        self.assertIn(
            f") or forgotten: forget it by hand before the next backup of win, whose retention would count it: "
            f"restic forget {SNAP_ID}, with the repository's variables of vm/README.md",
            r.stderr,
        )

    def test_the_next_run_forgets_a_recorded_snapshot_restic_still_lists_with_both_tags(self) -> None:
        for recorded in (TORN_ID, TORN_ID[:8]):  # the full id, or a short one restic named
            with self.subTest(recorded=recorded):
                self.write_torn([self.torn_entry(recorded)])
                self.restic_cfg(snapshots_json=self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, BOTH_TAGS)))
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.fix, "restic.log"))
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                calls = [c["args"] for c in self.restic_calls()]
                self.assertEqual([a[0] for a in calls], ["cat", "backup", "snapshots", "forget", "forget", "snapshots"])
                self.assertEqual(
                    calls[2], ["snapshots", "--json", "--no-lock", "--host", "sophia", "--tag", "cst-vm,vm:win"]
                )
                self.assertEqual(calls[3], ["forget", "--retry-lock", "30m", TORN_ID], "by the listed full id")
                self.assertIn("--keep-weekly", calls[4])
                self.assertIn(f"torn restic snapshot {TORN_ID[:8]} of an earlier run forgotten", r.stdout)
                self.assertEqual(self.torn_doc(), {"version": 1, "torn": []})

    def old_host_entry(self) -> vm_domain_backup.TornEntry:
        e = self.torn_entry()
        e["host"] = "oldhost"
        return e

    def test_a_recorded_snapshot_of_another_host_is_listed_and_forgotten_under_that_host(self) -> None:
        # NAS_RESTIC_HOST (or the hostname) changed since the torn run: restic lists that snapshot only
        # under the host it was made on.
        self.write_torn([self.old_host_entry()])
        self.restic_cfg(
            snapshots_by_host={
                "sophia": self.listed((SNAP_ID, BOTH_TAGS)),
                "oldhost": self.listed((TORN_ID, BOTH_TAGS)),
            }
        )
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = [c["args"] for c in self.restic_calls()]
        self.assertEqual(calls[2], ["snapshots", "--json", "--no-lock", "--host", "oldhost", "--tag", "cst-vm,vm:win"])
        self.assertEqual(self.forgets()[0], ["forget", "--retry-lock", "30m", TORN_ID])
        self.assertIn("--keep-weekly", self.forgets()[1], "then the retention")
        self.assertEqual(self.torn_doc(), {"version": 1, "torn": []})
        # Its listing fails: the entry stays and the retention waits.
        self.write_torn([self.old_host_entry()])
        self.restic_cfg(snapshots_rc=1)
        os.unlink(os.path.join(self.fix, "restic.log"))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forgets(), [])
        self.assertIn(
            "retention skipped: restic snapshots did not list the snapshots of win made on host oldhost, so the "
            "torn ones were not forgotten",
            r.stdout,
        )
        self.assertEqual(self.torn_list(), [self.old_host_entry()])

    def test_a_retry_that_fails_skips_the_retention(self) -> None:
        self.write_torn([self.torn_entry()])
        self.restic_cfg(
            snapshots_json=self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, BOTH_TAGS)), forget_rc_by_id={TORN_ID: 1}
        )
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forgets(), [["forget", "--retry-lock", "30m", TORN_ID]], "no retention forget")
        self.assertIn(
            f"retention skipped: restic forget {TORN_ID[:8]} (torn, of an earlier run): exit 1; the repository "
            "keeps more until the next backup",
            r.stdout,
        )
        self.assertEqual(self.torn_list(), [self.torn_entry()])
        self.assertEqual(self.index_entries()[0]["snapshot"], SNAP_ID, "the backup itself is recorded")

    def test_a_recorded_snapshot_restic_no_longer_lists_is_dropped_from_the_record(self) -> None:
        # Forgotten already: by hand, or by the run that recorded it when its record was not updated.
        self.write_torn([self.torn_entry()])
        self.restic_cfg(snapshots_json=self.listed((SNAP_ID, BOTH_TAGS)))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse([a for a in self.forgets() if TORN_ID in a], "never forgotten by its recorded id")
        self.assertEqual(len(self.forgets()), 1)
        self.assertIn("--keep-weekly", self.forgets()[0], "the retention runs")
        self.assertIn(
            f"torn restic snapshot {TORN_ID[:8]}: restic no longer lists it among the cst-vm snapshots of win "
            "(forgotten already): dropped from the record",
            r.stdout,
        )
        self.assertEqual(self.torn_list(), [])

    def test_a_recorded_snapshot_listed_without_both_tags_is_kept_and_the_retention_waits(self) -> None:
        # restic lists by both tags, so a listed snapshot without them is an answer of another shape:
        # never forgotten, never dropped, and the retention (which could count it) waits.
        for listing in (
            self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, ["cst-vm"])),
            self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, ["cst-vm", "vm:other"])),
            self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, None)),  # restic gave no tags
            json.dumps([{"id": SNAP_ID, "tags": BOTH_TAGS}, {"id": TORN_ID, "tags": "cst-vm,vm:win"}]),
        ):
            with self.subTest(listing=listing):
                self.write_torn([self.torn_entry()])
                self.restic_cfg(snapshots_json=listing)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.fix, "restic.log"))
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.forgets(), [], "nothing forgotten, no retention")
                self.assertIn(
                    f"retention skipped: restic lists the torn snapshot {TORN_ID[:8]} without both tags cst-vm and "
                    "vm:win, so it was not forgotten; the repository keeps more until the next backup",
                    r.stdout,
                )
                self.assertEqual(self.torn_list(), [self.torn_entry()])

    def test_an_ambiguous_or_unlisted_record_keeps_the_entry_and_skips_the_retention(self) -> None:
        other = "f00dbabe" + "3" * 56
        for recorded, cfg, said in (
            (
                "f00dbabe",
                {"snapshots_json": self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, BOTH_TAGS), (other, BOTH_TAGS))},
                "the torn snapshot f00dbabe names 2 listed snapshots",
            ),
            (TORN_ID, {"snapshots_rc": 1}, "restic snapshots did not list the snapshots of win"),
            (TORN_ID, {"snapshots_json": '{"id": "x"}'}, "restic snapshots did not list the snapshots of win"),
        ):
            with self.subTest(cfg=cfg):
                self.write_torn([self.torn_entry(recorded)])
                self.restic_cfg(**cfg)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.fix, "restic.log"))
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.forgets(), [], "nothing forgotten, no retention")
                self.assertIn(f"retention skipped: {said}", r.stdout)
                self.assertEqual(self.torn_list(), [self.torn_entry(recorded)])

    def test_entries_of_other_vms_and_repositories_and_unknown_ones_are_kept_as_they_are(self) -> None:
        odd: list[object] = [
            self.torn_entry(vm="other"),
            self.torn_entry(repository="sftp:elsewhere:/r"),
            {"snapshot": TORN_ID, "vm": "win", "note": "an entry of a newer cst vm"},
        ]
        self.write_torn([*odd, self.torn_entry()])
        self.restic_cfg(snapshots_json=self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, BOTH_TAGS)))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forgets()[0], ["forget", "--retry-lock", "30m", TORN_ID])
        self.assertEqual(self.torn_doc(), {"version": 1, "torn": odd})
        # Only entries of other VMs or repositories: no restic call for them.
        self.write_torn(odd[:2])
        os.unlink(os.path.join(self.fix, "restic.log"))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c["args"][0] for c in self.restic_calls()], ["cat", "backup", "forget", "snapshots"])
        self.assertEqual(self.torn_list(), odd[:2])

    def test_a_good_run_without_a_record_writes_none(self) -> None:
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.lexists(self.torn))

    def test_a_record_cst_vm_cannot_use_refuses_before_anything_is_done(self) -> None:
        for content, said in (
            (
                json.dumps({"version": 2, "torn": []}).encode(),
                (
                    f"{self.torn} is format 2 of the vm-nas-torn (this CST reads format 1 and older): written by "
                    "a newer CST: left as it is, and no NAS backup runs with it; use the cst vm of that CST"
                ),
            ),
            (b"{not json", f"{self.torn} cannot be read as cst vm's record of torn NAS snapshots (not JSON)"),
            (b'{"torn": "\xff\xfe"}', "(not JSON)"),
            (b"\xff\xfe", "(not JSON)"),
            (json.dumps({"version": 1}).encode(), '(no "torn" list)'),
            (None, f"{self.torn} cannot be read as cst vm's record of torn NAS snapshots ("),
        ):
            with self.subTest(content=content):
                shutil.rmtree(os.path.dirname(self.torn), ignore_errors=True)
                os.makedirs(self.torn if content is None else os.path.dirname(self.torn))  # None: a folder there
                if content is not None:
                    with open(self.torn, "wb") as f:
                        f.write(content)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.log)
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(said, r.stderr)
                if content is not None and b"version" not in content[:12]:
                    self.assertIn(f"then move it aside (mv {self.torn} {self.torn}.bad)", r.stderr)
                self.assertFalse([c for c in self.calls() if c.startswith("restic ") or "snapshot-create" in c])
                if content is not None:
                    with open(self.torn, "rb") as f:
                        self.assertEqual(f.read(), content, "left as it is")

    def test_the_record_waits_for_its_lock_and_gives_up_after_the_timeout(self) -> None:
        cfg = vm_wiring.vm_settings({"CST_VM_TORN": self.torn, "CST_VM_RUN_DIR": os.path.join(self.tmp, "run")})
        vm_adapters_backup.record_torn(cfg, self.torn_entry())
        os.makedirs(os.path.join(self.tmp, "run"), exist_ok=True)
        lock = os.path.join(self.tmp, "run", "torn-record.lock")
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaisesRegex(Refused, f"{re.escape(lock)} stayed taken for 0.2 s"):
            vm_adapters_backup.drop_torn(cfg, [self.torn_entry()], lock_timeout=0.2)
        self.assertEqual(len(self.torn_list()), 1, "nothing changed without the lock")
        fcntl.flock(fd, fcntl.LOCK_UN)
        # The NAS backup of a VM named "torn" holds its own lock, never the record's.
        with vm_adapters_backup.VmLock("torn", os.path.join(self.tmp, "run")):
            vm_adapters_backup.drop_torn(cfg, [self.torn_entry()], lock_timeout=0.2)
        self.assertEqual(self.torn_list(), [])


class ConstantsTest(unittest.TestCase):
    def test_nas_default_paths_match_btrfs_snapshot(self) -> None:
        # btrfs-snapshot's NAS backup is its Python runtime's (btrfs/snapshot_runtime): cst vm's
        # paths are its paths outside the test seam (no CST_SNAPSHOT_ROOT). The ssh alias is
        # compared by SharedRepositoryTest: both give restic the same RESTIC_REPOSITORY.
        from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
        from cst_core.adapters.runner import SubprocessCommandRunner

        run = SnapshotRun({}, SubprocessCommandRunner())
        vm = vm_wiring.vm_paths({})
        self.assertEqual(
            (vm.nas_conf, vm.restic_password, vm.restic_cache), (run.conf, run.restic_password, run.restic_cache)
        )
        # The machine's paths, which setup-snapshots.bash and the READMEs also name.
        self.assertEqual(
            (vm.nas_conf, vm.restic_password, vm.restic_cache),
            (
                "/etc/btrfs-setup/snapshots.conf",
                "/etc/btrfs-setup/restic-password",
                "/var/cache/btrfs-snapshot-restic",
            ),
        )

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

    def test_status_reads_the_timer_properties_where_it_runs(self) -> None:
        # The two properties through cst_core's show (PROP=VALUE, from the systemd cst-vm runs
        # under), not `systemctl show --value`.
        r = self.run_tool("backup-timer", "on", "win")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        open(self.log, "w").close()
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("  next run: Sat 2026-11-21 04:41:12 UTC\n", r.stdout)
        self.assertIn("  last run of the service: success\n", r.stdout)
        self.assertEqual(
            [c for c in self.calls() if c.startswith("systemctl ")],
            [
                "systemctl show -p NextElapseUSecRealtime cst-vm-backup@win.timer",
                "systemctl show -p Result cst-vm-backup@win.service",
            ],
        )

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
        stop = re.search(r"^TimeoutStopSec=(\d+)min$", service, re.MULTILINE)
        self.assertTrue(stop, service)
        assert stop is not None
        cfg = vm_wiring.vm_settings(self.env)
        self.assertGreaterEqual(
            int(stop.group(1)) * 60, cfg.snapshot_timeout + cfg.snapshot_settle + cfg.commit_timeout + 300 + 15 * 60
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
