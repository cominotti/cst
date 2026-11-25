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

from collections.abc import Callable, Generator, Mapping, Sequence
import contextlib
import dataclasses
import datetime
import fcntl
import io
import json
import os
import pty
import re
import resource
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Any, override
import unittest
from unittest import mock

# Run directly (python3 -I -B vm/tests/test_vm_nas.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.errors import ExitStatus, Refused, Usage
from cst_core.domain.shapes import items_of
from btrfs.tests.snapshot_fakes import RuntimeTest
from cst_core.ports import ClockPort
from vm import domain as vm_domain
from vm.adapters import backup as vm_adapters_backup, restic as vm_adapters_restic
from vm.application import nas as vm_app_nas, nas_torn as vm_app_nas_torn
from vm.domain import backup as vm_domain_backup
from vm.ports import ProgressPort
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
        f"<disk type='{d.get('type', 'file')}' device='{d['device']}'><source file='{sources[d['target']]}'/>"
        + ("<backingStore type='file'><source file='x'/></backingStore>" if vm.get("chain") else "<backingStore/>")
        + f"<target dev='{d['target']}'/></disk>" for d in vm["disks"])
    agent = vm.get("agent")
    chan = (f"<channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0' state='{agent}'/></channel>"
            if agent else "")
    pw = "<graphics type='spice' passwd='s3cret'/>" if secret else "<graphics type='spice'/>"
    tpm = "<tpm><backend type='emulator'/></tpm>" if vm.get("tpm", True) else ""
    return (f"<domain type='kvm'><name>win</name><uuid>{vm['uuid']}</uuid><os><nvram>{vm['nvram']}</nvram></os>"
            f"<devices>{disks}{chan}{pw}{tpm}</devices></domain>\n")
if cmd == "domstate":
    log = os.path.join(fix, "restic.log")
    sent = os.path.join(fix, "term-sent")
    if ("term-during-check" in fail and not os.path.exists(sent) and os.path.exists(log)
            and any(json.loads(ln)["args"][:1] == ["backup"] for ln in open(log))):
        # The stop comes while the check after restic's read waits for virsh.
        import signal
        open(sent, "w").close()
        os.kill(os.getppid(), signal.SIGTERM)
    print(vm["state"]); print()
elif cmd == "list":
    print("\n".join(vm.get("managed", [])))
elif cmd == "dumpxml":
    if ("dumpxml-inactive" if "--inactive" in args else "dumpxml-live") in fail:
        sys.exit("error: failed to get domain 'win'")
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
    if "snapshot-hang-none" in fail:
        # virsh hangs; libvirt made no overlay file and never switched the disk.
        import time; time.sleep(30)
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
    if "snapshot-error-in-place" in fail:
        sys.exit("error: internal error: unexpected answer")
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
    if "remove-nvram" in vm.get("fail", []):
        os.unlink(vm["nvram"])
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
    if "term-after-summary" in vm.get("fail", []):
        # The snapshot is saved and named; the stop comes before restic exits.
        import signal, time
        sys.stdout.flush()
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(30)
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
if [ -e "$FIX/notify-term" ]; then
  # A stop (the timer's unit stopped) while the alert is sent: SIGTERM to cst-vm, which runs it.
  kill -TERM "$PPID"; sleep 1
fi
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
            "left as it is and cst vm does not list this backup; the snapshot stays in the repository: the next NAS "
            "backups may name it as a snapshot no record names, and their retention waits for it until it is kept as "
            f"a backup (restic tag --add cst-adopted {SNAP_ID}) or forgotten (restic forget {SNAP_ID}) (restore it by "
            'its ID: vm/README.md, section "Restore from the NAS"). Then: use the cst vm of that CST',
            r.stderr,
        )
        self.assertNotIn("nothing is backed up", r.stderr)
        # The index cannot be read, so the snapshots no record names cannot be checked: the retention waits.
        self.assertIn("  retention skipped: the snapshots no record names were not checked (", r.stdout)
        self.assertEqual([c["args"][0] for c in self.restic_calls()], ["cat", "backup"])
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

    def test_a_good_read_whose_merge_back_fails_names_its_snapshot(self) -> None:
        # No stop came: the read was good, only the merge back failed. The snapshot is not indexed, so the
        # refusal names it with the commands that keep or forget it (else the next run names it as a
        # snapshot no record names, with no word of where it came from).
        self.vm(fail=["commit"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DISK NOT MERGED BACK", r.stderr)
        self.assertIn(
            f"(the backup itself: the restic snapshot {SNAP_ID[:8]} of win is a backup, its read checked, but it is "
            "not in the index: the snapshot stays in the repository",
            r.stderr,
        )
        self.assertIn(f"restic tag --add cst-adopted {SNAP_ID}", r.stderr)
        self.assertIn(f"restic forget {SNAP_ID}", r.stderr)
        self.assertFalse(os.path.exists(self.index))

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

    def test_an_entry_whose_snapshot_was_retagged_stays_in_the_index_under_its_new_id(self) -> None:
        # restic tag saves the snapshot again under a new id, with the first id as its original: the
        # entry left the index, although restic still lists the backup.
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        repo = "sftp:btrfs-setup-nas:/volume1/restic"
        old = [
            {"vm": "win", "mode": "nas", "snapshot": "first1", "repository": repo, "epoch": 1, "timestamp": "a"},
            {"vm": "win", "mode": "nas", "snapshot": "gone22", "repository": repo, "epoch": 2, "timestamp": "b"},
        ]
        with open(self.index, "w") as f:
            json.dump({"backups": old}, f)
        listed = [{"id": "second", "original": "first1"}, {"id": "other3", "original": ""}, {"id": SNAP_ID}]
        self.restic_cfg(snapshots_json=json.dumps(listed))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Under the id restic lists it by now: restic restores no snapshot by its original.
        self.assertEqual([e.get("snapshot") for e in self.index_entries()], ["second", SNAP_ID])
        self.assertEqual(
            self.index_entries()[0],
            {**old[0], "snapshot": "second", "original": "first1"},
            "the rest of the entry kept, and its first id",
        )

    def test_an_entry_whose_snapshot_was_retagged_twice_stays_in_the_index_and_is_no_stray(self) -> None:
        # restic keeps the first id as the original of every later tag change: after a second restic tag
        # the entry moved to the first copy's id named nothing restic lists, left the index and its
        # snapshot (newer than the oldest indexed backup) was named as one no record names.
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        repo = "sftp:btrfs-setup-nas:/volume1/restic"
        old = [
            {"vm": "win", "mode": "nas", "snapshot": STRAY_A, "repository": repo, "epoch": 1, "timestamp": "a"},
            {"vm": "win", "mode": "nas", "snapshot": STRAY_B, "repository": repo, "epoch": 2, "timestamp": "b"},
        ]
        with open(self.index, "w") as f:
            json.dump({"backups": old}, f)
        own = _listed(SNAP_ID, STRAY_T2)
        for copy in (STRAY_C, STRAY_D):
            with self.subTest(copy=copy):
                listed = [
                    _listed(STRAY_A, STRAY_T0),
                    _listed(copy, STRAY_T1, vm_domain.KEEP_TAG, original=STRAY_B),
                    own,
                ]
                self.restic_cfg(snapshots_json=json.dumps(listed))
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("retention skipped", r.stdout)
                self.assertEqual(
                    [(e.get("snapshot"), e.get("original")) for e in self.index_entries()],
                    [(STRAY_A, None), (copy, STRAY_B), (SNAP_ID, None)],
                )

    def test_a_snapshot_no_record_names_is_named_and_the_run_still_succeeds(self) -> None:
        # The retention counted it as a backup. The run's own snapshot is not one.
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        repo = "sftp:btrfs-setup-nas:/volume1/restic"
        first = {"vm": "win", "mode": "nas", "snapshot": STRAY_A, "repository": repo, "epoch": 1, "timestamp": "a"}
        with open(self.index, "w") as f:
            json.dump({"backups": [first]}, f)
        own = _listed(SNAP_ID, "2026-11-08T01:06:40Z")
        for listed, named in (
            ([_listed(STRAY_A, STRAY_T1), own], False),
            ([_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T2), own], True),
        ):
            with self.subTest(named=named):
                self.restic_cfg(snapshots_json=json.dumps(listed))
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                retention = [c["args"] for c in self.restic_calls() if "--keep-weekly" in c["args"]]
                alerts = [c for c in self.calls() if c.startswith("notify ") and "no record names" in c]
                if named:
                    self.assertIn("retention skipped: restic lists 1 snapshot of win that neither", r.stdout)
                    self.assertIn(f"restic tag --add cst-adopted {STRAY_B}", r.stdout)
                    self.assertEqual(len(retention), 1, "only the first run's")
                    self.assertEqual(len(alerts), 1, self.calls())
                else:
                    self.assertNotIn("retention skipped", r.stdout)
                    self.assertEqual((len(retention), alerts), (1, []))

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

    def test_a_stop_while_the_alert_is_sent_still_alerts_and_refuses(self) -> None:
        # No stop came before (on_signal is still caught): a SIGTERM during btrfs-notify must not cut
        # the alert or the refusal with the recovery lines short.
        self.vm(agent="connected", fail=["commit"])
        open(os.path.join(self.fix, "notify-term"), "w").close()
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DISK NOT MERGED BACK:\n  sda: virsh blockcommit: error: block commit failed", r.stderr)
        self.assertIn("domblklist win --details --inactive", r.stderr)
        self.assertNotIn("stopped by signal", r.stderr)
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


class NasPreflightAndSourcesTest(Nas):
    """What the NAS backup reads and refuses before restic starts, the notes of its sources, and the
    snapshot attempts whose answer and outcome differ."""

    def refused(self, words: str) -> None:
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"cst vm: {words}\n", r.stderr)
        self.assertEqual(self.virsh_calls("snapshot-create-as"), [])
        self.assertEqual(self.restic_calls(), [])

    def test_the_inactive_definition_refusals(self) -> None:
        missing = os.path.join(self.images, "gone.qcow2")
        for kw, words in (
            ({"fail": ["dumpxml-inactive"]}, "virsh dumpxml win: error: failed to get domain 'win'"),
            (
                {"disks": [{"target": "sda", "device": "disk", "type": "block"}]},
                "win: nothing backed up:\n  sda: a block disk: only file disks can be copied; back it up by hand",
            ),
            (
                {"disks": [{"target": "sdb", "device": "cdrom"}]},
                "win: no file disk in its definition: nothing to back up",
            ),
            ({"persistent": {"sda": missing, "sdb": "/iso/virtio.iso"}}, f"win: disk {missing} not found"),
            (
                {"persistent": {"sda": missing + ".cst-overlay-20261101T000000Z", "sdb": "/iso/virtio.iso"}},
                f"win: disk {missing}.cst-overlay-20261101T000000Z not found",
            ),
        ):
            with self.subTest(kw=kw):
                self.vm(**kw)
                self.refused(words)

    def test_an_overlay_left_behind_and_a_backing_file_are_refused(self) -> None:
        left = f"{self.disk}.cst-overlay-20261101T000000Z"
        with open(left, "w") as f:
            f.write("x")
        self.vm(persistent={"sda": left, "sdb": "/iso/virtio.iso"})
        self.refused(
            f"win: disk {left} is an overlay a NAS backup left behind (its blockcommit did not finish). Put the disk "
            "back first; `cst doctor` shows the commands"
        )
        self.vm()
        with open(self.disk, "wb") as f:
            f.write(b"QFI\xfb\0\0\0\3" + struct.pack(">Q", 512))
        self.refused(
            f"win: {self.disk} is a qcow2 overlay with a backing file: back up the chain by hand "
            "(qemu-img info --backing-chain)"
        )

    def test_the_running_definition_refusals(self) -> None:
        other = os.path.join(self.images, "other.qcow2")
        for kw, words in (
            ({"fail": ["dumpxml-live"]}, "virsh dumpxml win: error: failed to get domain 'win'"),
            (
                {"chain": True},
                (
                    f"win: disk sda runs on {self.disk} with a backing chain (an overlay left by an earlier backup?). "
                    "Put it back first; `cst doctor` shows the commands"
                ),
            ),
            (
                {"live": {"sda": other, "sdb": "/iso/virtio.iso"}},
                (
                    "win: the running VM's disks differ from its definition (changed since it started): restart it, "
                    "then back up"
                ),
            ),
        ):
            with self.subTest(kw=kw):
                self.vm(**kw)
                self.refused(words)
        self.vm()
        with open(self.overlay, "w") as f:
            f.write("x")
        self.refused(f"{self.overlay} already exists: wait a second and run it again")

    def test_the_notes_of_a_linked_disk_missing_uefi_variables_and_no_tpm(self) -> None:
        link = os.path.join(self.images, "link.qcow2")
        os.symlink(self.disk, link)
        missing = os.path.join(self.images, "gone_VARS.qcow2")
        self.vm(
            live={"sda": link, "sdb": "/iso/virtio.iso"},
            persistent={"sda": link, "sdb": "/iso/virtio.iso"},
            nvram=missing,
            tpm=False,
            agent="connected",
        )
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        notes = [ln for ln in r.stdout.splitlines() if ln.startswith("  note: ")]
        self.assertEqual(
            notes,
            [
                "  note: sdb (cdrom, not copied)",
                f"  note: disk {link} is a symbolic link: read (and restored) through it: {self.disk}",
                f"  note: UEFI variables {missing} not there yet (the VM never started?): not backed up",
            ],
        )
        backup = next(c for c in self.restic_calls() if c["args"][0] == "backup")
        self.assertEqual(backup["args"][-2:], [self.disk, os.path.join(self.stage, "win")])

    def test_a_disk_the_definition_names_twice_is_read_once(self) -> None:
        # Two targets on one file (or a link to it): restic reads it once and the manifest lists it once,
        # as a local copy copies it once; before, the NAS backup listed it twice.
        link = os.path.join(self.images, "again.qcow2")
        os.symlink(self.disk, link)
        both = [{"target": "sda", "device": "disk"}, {"target": "sdb", "device": "disk"}]
        self.vm(state="shut off", disks=both, live={}, persistent={"sda": self.disk, "sdb": link})
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = next(c for c in self.restic_calls() if c["args"][0] == "backup")
        self.assertEqual(backup["args"].count(self.disk), 1, backup["args"])
        self.assertEqual(r.stdout.count(f"  reading {self.disk} (disk,"), 1, r.stdout)

    def test_a_running_vm_with_one_file_on_two_disks_is_refused(self) -> None:
        # Two targets of the running VM on one file would get one overlay path: refused before anything
        # changes (before, the deduplicated sources and the set of live files matched, and the snapshot ran).
        both = [{"target": "sda", "device": "disk"}, {"target": "sdb", "device": "disk"}]
        self.vm(disks=both, live={"sda": self.disk, "sdb": self.disk}, persistent={"sda": self.disk, "sdb": self.disk})
        self.refused(
            f"win: two disks of the running VM are one file ({self.disk}): a live snapshot cannot give each "
            "its own overlay; shut the VM down and back it up then"
        )

    def test_uefi_variables_through_a_link_are_noted_as_a_local_copy_notes_them(self) -> None:
        # restic reads (and a restore writes) the file the link points to; before, only a linked disk had a note.
        link = os.path.join(self.images, "link_VARS.fd")
        os.symlink(self.nvram, link)
        self.vm(nvram=link, tpm=False)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            f"  note: UEFI variables {link} is a symbolic link: read (and restored) through it: {self.nvram}\n",
            r.stdout,
        )
        backup = next(c for c in self.restic_calls() if c["args"][0] == "backup")
        self.assertIn(self.nvram, backup["args"])
        self.assertNotIn(link, backup["args"])

    def test_uefi_variables_removed_while_restic_reads_them(self) -> None:
        self.vm(fail=["remove-nvram"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            f"  note: {self.nvram} changed while restic read it (the VM wrote it): that copy may be torn; the next "
            "backup reads it again\n",
            r.stdout,
        )
        self.assertEqual(self.index_entries()[0]["snapshot"], SNAP_ID)
        self.restic_cfg()
        open(self.log, "w").close()
        os.unlink(os.path.join(self.fix, "restic.log"))
        with open(self.nvram, "wb") as f:
            f.write(b"vars")
        self.vm(state="shut off", fail=["remove-nvram"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            "cst vm: changed during the backup (the VM was started?), the copy is not consistent (restic snapshot "
            f"{SNAP_ID[:8]} forgotten): {self.nvram}\n",
            r.stderr,
        )
        forgets = [c["args"] for c in self.restic_calls() if c["args"][0] == "forget"]
        self.assertEqual(forgets, [["forget", "--retry-lock", "30m", SNAP_ID]])

    def test_a_restic_line_that_is_not_json_is_shown(self) -> None:
        self.restic_cfg(backup_lines=["not json at all"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("  restic: not json at all\n", r.stdout)
        self.assertEqual(self.index_entries()[0]["snapshot"], SNAP_ID)

    def test_a_quiesced_snapshot_virsh_calls_failed_but_in_place(self) -> None:
        self.vm(agent="connected", fail=["snapshot-error-in-place"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        snaps = self.virsh_calls("snapshot-create-as")
        self.assertEqual(len(snaps), 1)
        self.assertIn("--quiesce", snaps[0])
        self.assertEqual(len(self.virsh_calls("domfsthaw")), 1)
        self.assertNotIn("quiesced, QEMU guest agent", r.stdout)
        self.assertNotIn("crash-consistent snapshot (", r.stdout)
        self.assertIn(
            "  note: virsh reported an error but the snapshot is in place: error: internal error: unexpected answer\n",
            r.stdout,
        )
        self.assertEqual(self.index_entries()[0]["consistency"], "crash-consistent")
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1)

    def test_a_crash_consistent_snapshot_virsh_calls_failed_but_in_place(self) -> None:
        self.vm(agent=None, fail=["snapshot-error-in-place"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.virsh_calls("snapshot-create-as")), 1)
        self.assertEqual(self.virsh_calls("domfsthaw"), [])
        notes = [ln for ln in r.stdout.splitlines() if ln.startswith("  note: ")]
        self.assertEqual(
            notes,
            [
                "  note: sdb (cdrom, not copied)",
                "  note: virsh reported an error but the snapshot is in place: error: internal error: unexpected answer",
                "  note: crash-consistent: no guest agent channel (org.qemu.guest_agent.0) in the definition",
            ],
        )
        self.assertEqual(self.index_entries()[0]["consistency"], "crash-consistent")

    def test_a_failed_merge_back_names_the_backups_own_failure(self) -> None:
        self.restic_cfg(backup_rc=1)
        self.vm(fail=["commit"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DISK NOT MERGED BACK:\n  ", r.stderr)
        self.assertIn("\n(the backup itself: restic backup failed: restic exit 1; no backup recorded)\n", r.stderr)
        self.assertTrue([c for c in self.calls() if c.startswith("notify -u critical -a cst-vm")])
        self.assertTrue(os.path.exists(self.overlay))

    def test_an_unclear_snapshot_without_any_overlay_file_alerts_and_names_no_file(self) -> None:
        self.env.update(CST_VM_SNAPSHOT_TIMEOUT="2", CST_VM_SNAPSHOT_SETTLE="3")
        self.vm(agent=None, fail=["snapshot-hang-none"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            "cst vm: DISK NOT MERGED BACK:\n  virsh snapshot-create-as win did not answer within 2 s and, 3 s later, "
            "the VM's disks still do not all show the overlay: libvirt may still switch to it. Nothing was removed\n",
            r.stderr,
        )
        self.assertNotIn("(kept:", r.stderr)
        self.assertNotIn("(the backup itself", r.stderr)
        self.assertTrue([c for c in self.calls() if c.startswith("notify -u critical -a cst-vm")])
        self.assertFalse(os.path.exists(self.overlay))
        self.assertEqual(self.virsh_calls("blockcommit"), [])
        self.assertNotIn("backup", [c["args"][0] for c in self.restic_calls()])

    def test_a_snapshot_timeout_without_quiesce_thaws_nothing(self) -> None:
        self.env.update(CST_VM_SNAPSHOT_TIMEOUT="2", CST_VM_SNAPSHOT_SETTLE="3")
        self.vm(agent=None, fail=["snapshot-hang-nothing"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("may still switch to it", r.stderr)
        self.assertIn(f"(kept: {self.overlay})", r.stderr)
        self.assertEqual(self.virsh_calls("domfsthaw"), [])
        self.assertTrue(os.path.exists(self.overlay))
        self.assertNotIn("backup", [c["args"][0] for c in self.restic_calls()])


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

    def test_a_stop_that_ends_restic_after_its_snapshot_leaves_the_snapshot_recorded(self) -> None:
        # restic printed its summary (the snapshot is saved) and is ended by the stop before it exits:
        # the snapshot counts as torn, is recorded for the next run to forget, and nothing is forgotten now.
        self.vm(fail=["term-after-summary"], agent="connected")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup: recorded in {self.torn}, "
            "the next NAS backup of win forgets it before its retention",
            r.stderr,
        )
        self.assertEqual(self.torn_list(), [self.torn_entry(SNAP_ID, timestamp=STAMP, why="stopped")])
        self.assertEqual(self.forgets(), [], "the forget is the next run's")
        self.assertEqual(len(self.virsh_calls("blockcommit")), 1, "merged back as after any stop")
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)
        self.assertFalse(os.path.exists(self.index))

    def test_a_stop_during_the_check_after_the_read_waits_for_the_torn_record(self) -> None:
        # A shut-off VM started during restic's read: the check after it finds the snapshot torn, and a
        # stop that came while the check waited for virsh is raised only once the snapshot is recorded.
        self.vm(state="shut off", fail=["start-during-backup", "term-during-check"])
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.fix, "term-sent")), "the stub sent its stop")
        self.assertIn(
            f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup: recorded in {self.torn}, "
            "the next NAS backup of win forgets it before its retention",
            r.stderr,
        )
        self.assertEqual(self.torn_list(), [self.torn_entry(SNAP_ID, timestamp=STAMP, why="state changed")])
        self.assertEqual(self.forgets(), [], "the forget is the next run's")
        self.assertFalse(os.path.exists(self.index))

    def test_a_stop_that_ends_restic_after_its_snapshot_names_it_when_the_record_fails(self) -> None:
        self.vm(fail=["term-after-summary"], agent="connected")
        self.restic_cfg(break_torn=self.torn)
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup and could NOT be recorded (",
            r.stderr,
        )
        self.assertIn(f"restic forget {SNAP_ID}, with the repository's variables of vm/README.md", r.stderr)
        self.assertEqual(self.forgets(), [])
        self.assertEqual(self.vm_state()["live"]["sda"], self.disk)

    def test_the_next_run_forgets_a_recorded_snapshot_restic_still_lists_with_both_tags(self) -> None:
        for recorded in (TORN_ID, TORN_ID[:8]):  # the full id, or a short one restic named
            with self.subTest(recorded=recorded):
                self.write_torn([self.torn_entry(recorded)])
                self.restic_cfg(snapshots_json=self.listed((SNAP_ID, BOTH_TAGS), (TORN_ID, BOTH_TAGS)))
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.fix, "restic.log"))
                # The second run has the first one's backup in the index: the snapshots no record names are listed.
                strays = ["snapshots"] if os.path.exists(self.index) else []
                r = self.run_tool("backup", "win", "--nas")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                calls = [c["args"] for c in self.restic_calls()]
                self.assertEqual(
                    [a[0] for a in calls], ["cat", "backup", "snapshots", "forget", *strays, "forget", "snapshots"]
                )
                self.assertEqual(
                    calls[2], ["snapshots", "--json", "--no-lock", "--host", "sophia", "--tag", "cst-vm,vm:win"]
                )
                self.assertEqual(calls[3], ["forget", "--retry-lock", "30m", TORN_ID], "by the listed full id")
                self.assertIn("--keep-weekly", calls[4 + len(strays)])
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

    def test_a_recorded_snapshot_whose_tags_were_changed_is_forgotten_by_its_new_id(self) -> None:
        # restic tag saved it again under a new id, with the recorded one as its original: the retry took it
        # for "forgotten already" and dropped it from the record, and the next check named it as a stray.
        retagged = "f00dcafe" + "2" * 56
        self.write_torn([self.torn_entry()])
        listing = [
            {"id": SNAP_ID, "tags": BOTH_TAGS},
            {"id": retagged, "original": TORN_ID, "tags": [*BOTH_TAGS, "cst-keep"]},
        ]
        self.restic_cfg(snapshots_json=json.dumps(listing))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forgets()[0], ["forget", "--retry-lock", "30m", retagged], "by its listed id")
        self.assertIn("--keep-weekly", self.forgets()[1], "then the retention")
        self.assertIn(f"torn restic snapshot {retagged[:8]} of an earlier run forgotten", r.stdout)
        self.assertNotIn("forgotten already", r.stdout)
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
        # Only entries of other VMs or repositories: no restic call for them (the one listing before the
        # retention is the check of the snapshots no record names: the first run indexed its backup).
        self.write_torn(odd[:2])
        os.unlink(os.path.join(self.fix, "restic.log"))
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            [c["args"][0] for c in self.restic_calls()], ["cat", "backup", "snapshots", "forget", "snapshots"]
        )
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


class SnapshotNamesTest(unittest.TestCase):
    """The ids a listed restic snapshot answers to (snapshot_names, nas_snapshot_ids): its own, and its
    original when restic tag saved it again under a new id."""

    def test_a_retagged_snapshot_answers_to_its_original_too(self) -> None:
        self.assertEqual(vm_domain_backup.snapshot_names({"id": "new", "original": "old"}), ["new", "old"])

    def test_an_original_that_is_no_id_is_left_out(self) -> None:
        for original in ("", 5, None, ["old"]):
            with self.subTest(original=original):
                self.assertEqual(vm_domain_backup.snapshot_names({"id": "new", "original": original}), ["new"])
        self.assertEqual(vm_domain_backup.snapshot_names({"id": "new"}), ["new"])

    def test_the_index_prune_gets_the_listed_ids_and_their_originals(self) -> None:
        class Listing(FakeNasRestic):
            @override
            def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
                del env, args, timeout
                return 0, json.dumps([{"id": "new", "original": "old"}, {"id": SNAP_ID}]), ""

        got = vm_app_nas_torn.nas_snapshot_ids(Listing(), "win", _NAS_REPO)
        self.assertEqual(got, ({"new": "new", "old": "new", SNAP_ID: SNAP_ID}, frozenset()))

    def test_an_indexed_entry_named_by_an_original_takes_the_listed_id(self) -> None:
        def nas(snap: str, vm: str = "win") -> vm_domain_backup.IndexEntry:
            return {"vm": vm, "mode": "nas", "snapshot": snap, "repository": "/repo", "epoch": 1}

        entries = [nas("first1"), nas("kept22"), nas("short"), nas("gone33"), nas("first1", vm="other")]
        alive = {"second": "second", "first1": "second", "kept22": "kept22", "shortened": "shortened"}
        got = vm_domain_backup.nas_index(entries, nas(SNAP_ID), alive)
        # A short id restic lists by itself stays as it is; another VM's entry is not this listing's.
        self.assertEqual(
            sorted((e["vm"], e.get("snapshot")) for e in got),
            [("other", "first1"), ("win", SNAP_ID), ("win", "kept22"), ("win", "second"), ("win", "short")],
        )

    def test_a_listed_id_that_is_another_snapshots_original_names_itself_in_either_order(self) -> None:
        # restic lists both a snapshot and a copy whose original it is (restic rewrite without --forget):
        # the later one in the listing won, and an entry of the first moved to the copy.
        first, copy = {"id": "first1"}, {"id": "copy22", "original": "first1"}
        entry: vm_domain_backup.IndexEntry = {"vm": "win", "mode": "nas", "snapshot": "first1", "repository": "/repo"}

        class Listing(FakeNasRestic):
            answer = ""

            @override
            def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
                del env, args, timeout
                return 0, self.answer, ""

        for listed in ([first, copy], [copy, first]):
            with self.subTest(listed=listed):
                restic = Listing()
                restic.answer = json.dumps(listed)
                listing = vm_app_nas_torn.nas_snapshot_ids(restic, "win", _NAS_REPO)
                assert listing is not None
                alive = listing.alive
                self.assertEqual(alive, {"first1": "first1", "copy22": "copy22"})
                own: vm_domain_backup.IndexEntry = {**entry, "snapshot": SNAP_ID}
                got = vm_domain_backup.nas_index([entry], own, alive)
                self.assertEqual([e.get("snapshot") for e in got], ["first1", SNAP_ID])

    def test_an_entry_retagged_twice_keeps_its_first_id_and_follows_the_second_copy(self) -> None:
        def nas(snap: str, **more: str) -> vm_domain_backup.IndexEntry:
            return {"vm": "win", "mode": "nas", "snapshot": snap, "repository": "/repo", "epoch": 1, **more}

        own = {SNAP_ID: SNAP_ID}
        once = vm_domain_backup.nas_index(
            [nas("first1")], nas(SNAP_ID), {"second": "second", "first1": "second", **own}
        )
        self.assertEqual(once, [nas("second", original="first1"), nas(SNAP_ID)])
        # restic lists the second copy with the first id as its original, not the first copy's.
        twice = vm_domain_backup.nas_index(once, nas(SNAP_ID), {"third": "third", "first1": "third", **own})
        self.assertEqual(twice, [nas("third", original="first1"), nas(SNAP_ID)])
        # An entry restic lists by its own id stays as it is, its original with it.
        self.assertEqual(vm_domain_backup.nas_index(twice, nas(SNAP_ID), {"third": "third", **own}), twice)


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

    def test_status_says_when_the_nas_backup_is_not_set_up(self) -> None:
        with open(self.conf, "w") as f:
            f.write("NAS_ENABLED=0\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.replace(self.tmp, "TMP").splitlines(),
            [
                "weekly NAS backup of win: off (cst-vm-backup@win.timer; Sat *-*-* 04:30:00, up to 30 min later)",
                "  TMP/sbin/cst-vm: not installed",
                "  TMP/units/cst-vm-backup@.service: not installed",
                "  TMP/units/cst-vm-backup@.timer: not installed",
                "  last NAS backup: none",
                "  the NAS backup is not set up: cst btrfs snapshots nas setup",
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

    def test_snapshots_conf_keeps_line_ends_while_vm_conf_strips_crlf(self) -> None:
        # snapshots.conf keeps line ends as btrfs-snapshot and cst doctor read it: a CR or CRLF stays
        # part of its line instead of silently changing values or shifting line numbers.
        with open(self.conf, "w", newline="") as f:
            f.write("NAS_ENABLED=1\r\nNAS_PATH=/volume1/restic\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{self.conf} line 1: NAS_ENABLED=1: a carriage return (Windows line end)", r.stderr)
        open(self.log, "w").close()
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"{self.conf} line 1: NAS_ENABLED=1: a carriage return (Windows line end)", r.stderr)
        self.assertIn("nothing was changed", r.stderr)
        self.assertEqual(self.calls(), [], "the backup ran commands")

        # Lone CR stays part of line 2 (not split into two lines); vm.conf still accepts CRLF cleanly.
        with open(self.conf, "w", newline="") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/volume1/restic\rNAS_RESTIC_HOST=sophia\nNAS_BAD=$X\n")
        with open(self.env["CST_VM_CONF"], "w", newline="") as f:
            f.write("VM_NOCOW = no\r\n")
        r = self.run_tool("backup-timer", "status", "win")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{self.conf} line 2:", r.stderr)
        self.assertIn("a carriage return (Windows line end)", r.stderr)
        self.assertIn(f"{self.conf} line 3: NAS_BAD=$X", r.stderr)
        self.assertNotIn("vm.conf", r.stderr)

    def test_a_keep_number_with_a_no_break_space_refuses_the_backup(self) -> None:
        # It used to be read as no number, so the retention kept the default 4 weekly backups without a
        # word. Now it is a problem line: the backup refuses before any command, so no restic forget runs.
        vm_conf = self.env["CST_VM_CONF"]
        with open(vm_conf, "w", encoding="utf-8") as f:
            f.write("VM_NAS_KEEP_WEEKLY=\N{NO-BREAK SPACE}8\n")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(
            f"{vm_conf} line 1: VM_NAS_KEEP_WEEKLY=\N{NO-BREAK SPACE}8: a no-break space (U+00A0) at the start of "
            "the value",
            r.stderr,
        )
        self.assertIn("Fix these lines first; nothing was changed.", r.stderr)
        self.assertEqual(self.restic_calls(), [], "restic ran: no forget may run on a default")
        self.assertEqual(self.calls(), [], "the backup ran commands")
        # Fixed: the same backup runs, and its forget keeps the 8 weekly backups the line asks for.
        with open(vm_conf, "w", encoding="utf-8") as f:
            f.write("VM_NAS_KEEP_WEEKLY=8\n")
        r = self.run_tool("backup", "win", "--nas")
        self.assertEqual(r.returncode, 0, r.stderr)
        forget = [c["args"] for c in self.restic_calls() if c["args"][:1] == ["forget"]]
        self.assertTrue(forget, self.restic_calls())
        self.assertIn("--keep-weekly", forget[0])
        self.assertEqual(forget[0][forget[0].index("--keep-weekly") + 1], "8")

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


class FakeNasCommands:
    def __init__(self, handler: Callable[[Sequence[str]], tuple[int, str, str]], signals: FakeNasSignals) -> None:
        self.handler = handler
        self.signals = signals
        self.in_detached = False
        self.virsh_log: list[tuple[tuple[str, ...], bool, bool]] = []

    def virsh(self, args: Sequence[str], timeout: int, *, detach: bool | None) -> tuple[int, str, str]:
        del timeout, detach
        self.virsh_log.append((tuple(args), self.in_detached, self.signals.ignored_active))
        return self.handler(args)

    def define(self, text: str) -> tuple[int, str, str]:
        del text
        return 0, "", ""

    def package_versions(self) -> dict[str, str]:
        return {"qemu-kvm": "9.0"}

    @contextlib.contextmanager
    def detached(self) -> Generator[None]:
        was = self.in_detached
        self.in_detached = True
        try:
            yield
        finally:
            self.in_detached = was

    def booted_deployment(self) -> vm_domain_backup.BootedDeployment | None:
        return None


class FakeNasStore:
    def __init__(self, disks: Sequence[str]) -> None:
        self.disk_paths = set(disks)
        self.overlays: set[str] = set()
        self.dropped: list[str] = []
        self.removed: list[str] = []
        self.torn: list[vm_domain_backup.TornEntry] = []
        self.record_fails = False
        self.written: list[list[vm_domain_backup.IndexEntry]] = []  # each index written, in order

    def is_file(self, path: str) -> bool:
        return path in self.disk_paths

    def is_dir(self, path: str) -> bool:
        del path
        return False

    def exists(self, path: str) -> bool:
        return path in self.disk_paths or path in self.overlays

    def lexists(self, path: str) -> bool:
        return self.exists(path)

    def realpath(self, path: str) -> str:
        return path

    def has_backing_file(self, path: str) -> bool:
        del path
        return False

    def sizes(self, path: str) -> tuple[int, int]:
        del path
        return (1024, 1024)

    def fingerprint(self, path: str) -> vm_domain_backup.Fingerprint:
        return (path, 1)

    def free_space(self, path: str) -> tuple[str, int]:
        return (path, 10**12)

    def make_backup_folder(self, backup_dir: str, vm_dir: str, dest_dir: str) -> None:
        del backup_dir, vm_dir, dest_dir

    def copy(self, src: str, dest: str, mode: str, *, big: bool) -> None:
        del src, dest, mode, big

    def write_private(self, path: str, text: str) -> None:
        del path, text

    def remove_tree(self, path: str) -> None:
        del path

    def stage(self, vm: str, xml_text: str, manifest: str) -> str:
        del xml_text, manifest
        return f"/stage/{vm}"

    def unstage(self, folder: str) -> None:
        del folder

    def remove_overlay(self, path: str) -> str | None:
        self.overlays.discard(path)
        self.removed.append(path)
        return None

    def drop(self, path: str) -> None:
        self.overlays.discard(path)
        self.dropped.append(path)

    def describe(self, entry: vm_domain_backup.IndexEntry) -> str:
        del entry
        return ""

    def read_index(self) -> list[vm_domain_backup.IndexEntry]:
        return []

    def write_index(self, entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
        """A helper of update_index (no longer a port call): each index written, recorded."""
        self.written.append(list(entries))

    def update_index(
        self, change: Callable[[list[vm_domain_backup.IndexEntry]], list[vm_domain_backup.IndexEntry]]
    ) -> None:
        self.write_index(change(self.read_index()))

    def read_torn(self) -> list[vm_domain_backup.TornEntry]:
        return []

    def record_torn(self, entry: vm_domain_backup.TornEntry) -> None:
        if self.record_fails:
            raise OSError(28, "No space left on device")
        self.torn.append(entry)

    def drop_torn(self, entries: Sequence[vm_domain_backup.TornEntry]) -> None:
        del entries


class FakeNasSettings:
    def number(self, key: str) -> int | None:
        del key
        return 0

    def nas_repo(self) -> vm_domain.NasRepo:
        return {"host": "sophia", "repo": "/repo", "env": {}}

    def nas_set_up(self) -> bool:
        return True

    def knobs(self) -> dict[str, bool]:
        return {}

    def cpu_disable(self) -> list[str]:
        return []

    def default_vm(self) -> str:
        return "win"


class FakeNasRestic:
    def __init__(self, backup_fn: Callable[[], tuple[int, vm_domain_backup.ResticSummary]] | None = None) -> None:
        self.backup_fn = backup_fn
        self.calls: list[list[str]] = []

    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        del env, timeout
        self.calls.append(list(args))
        if args and args[0] == "snapshots":
            return 0, json.dumps([{"id": SNAP_ID}]), ""
        return 0, "", ""

    def backup(
        self,
        env: Mapping[str, str],
        args: Sequence[str],
        timeout: int,
        out: ProgressPort,
        *,
        answered: Callable[[], None] | None = None,
    ) -> tuple[int, vm_domain_backup.ResticSummary]:
        del env, args, timeout, out
        good: vm_domain_backup.ResticSummary = {
            "snapshot_id": SNAP_ID,
            "total_bytes_processed": 1024,
            "data_added": 512,
        }
        rc, summary = (0, good) if self.backup_fn is None else self.backup_fn()
        if answered is not None and "snapshot_id" in summary:  # as Restic.backup: once restic exited
            answered()
        return rc, summary


class FakeNasSignals:
    def __init__(self) -> None:
        self.ignored_active = False

    def catch_stops(self) -> None:
        pass

    def held(self) -> contextlib.AbstractContextManager[object]:
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def ignored(self) -> Generator[None]:
        was = self.ignored_active
        self.ignored_active = True
        try:
            yield
        finally:
            self.ignored_active = was


class FakeNasOut:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, text: str, *, flush: bool = False) -> None:
        del flush
        self.lines.append(text)

    def warn(self, text: str) -> None:
        self.lines.append(text)


class FakeNasClock:
    def now(self) -> float:
        return float(NOW)

    def monotonic(self) -> float:
        return 100.0

    def sleep(self, seconds: float) -> None:
        del seconds


def fake_nas_xml(disks: Sequence[tuple[str, str]]) -> str:
    nodes = "".join(f"<disk type='file' device='disk'><source file='{p}'/><target dev='{t}'/></disk>" for t, p in disks)
    return f"<domain type='kvm'><name>win</name><uuid>{UUID}</uuid><os/><devices>{nodes}</devices></domain>"


def fake_blklist(disks: Sequence[tuple[str, str]]) -> str:
    rows = "\n".join(f" file   disk   {t}   {p}" for t, p in disks)
    return f" Type   Device   Target   Source\n{'-' * 40}\n{rows}\n"


def default_virsh(
    args: Sequence[str], disks: Sequence[tuple[str, str]], live: dict[str, str], store: FakeNasStore
) -> tuple[int, str, str]:
    cmd = args[0]
    if cmd == "dumpxml":
        return 0, fake_nas_xml([(t, live[t]) for t, _ in disks]), ""
    if cmd == "domstate":
        return 0, "running", ""
    if cmd == "domblklist":
        return 0, fake_blklist([(t, live[t]) for t, _ in disks]), ""
    if cmd == "snapshot-create-as":
        for i, a in enumerate(args):
            if a == "--diskspec" and "snapshot=external" in args[i + 1]:
                t, spec = args[i + 1].split(",", 1)
                ov = dict(kv.split("=", 1) for kv in spec.split(","))["file"]
                store.overlays.add(ov)
                live[t] = ov
    elif cmd == "blockcommit":
        live[args[2]] = dict(disks)[args[2]]
    return 0, "", ""


class NasMergeBackAndStrayTest(unittest.TestCase):
    """Untested merge-back failure and partial-switch overlay cleanup paths in vm.application.nas."""

    def _cfg(self) -> vm_domain.VmSettings:
        return vm_wiring.vm_settings({"CST_VM_NOW": str(NOW), "CST_PROG": "cst vm", "CST_VM_NONROOT_OK": "1"})

    def _make_backup(
        self,
        disks: Sequence[tuple[str, str]],
        *,
        on_virsh: Callable[
            [Sequence[str], FakeNasCommands, FakeNasSignals, dict[str, str], FakeNasStore], tuple[int, str, str] | None
        ],
        backup_fn: Callable[[], tuple[int, vm_domain_backup.ResticSummary]] | None = None,
        signals: FakeNasSignals | None = None,
        clock: ClockPort | None = None,
    ) -> tuple[vm_app_nas.NasBackup, FakeNasCommands, FakeNasStore, FakeNasSignals, list[tuple[str, str]]]:
        live: dict[str, str] = dict(disks)
        store = FakeNasStore([p for _, p in disks])
        signals = FakeNasSignals() if signals is None else signals
        alerts: list[tuple[str, str]] = []

        def handle(args: Sequence[str]) -> tuple[int, str, str]:
            custom = on_virsh(args, commands, signals, live, store)
            return default_virsh(args, disks, live, store) if custom is None else custom

        commands = FakeNasCommands(handle, signals)
        clock = FakeNasClock() if clock is None else clock
        port = vm_app_nas.NasBackup(
            commands=commands,
            store=store,
            settings=FakeNasSettings(),
            restic=FakeNasRestic(backup_fn),
            signals=signals,
            lock=lambda _vm: contextlib.nullcontext(),
            alert=lambda title, body: alerts.append((title, body)),
            clock=clock,
            out=FakeNasOut(),
            root=True,
        )
        return port, commands, store, signals, alerts

    def test_find_overlays_raises_with_known_overlay_still_commits_and_reports_backup_failure(self) -> None:
        disk_a = "/img/a.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"

        def hook(
            args: Sequence[str],
            cmds: FakeNasCommands,
            sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            # Only fail the initial find_overlays lookup (the commit_back verification after blockcommit runs normally).
            if (
                cmds.in_detached
                and sigs.ignored_active
                and tuple(args) == ("domblklist", "win", "--details")
                and cmds.virsh_log.count((tuple(args), True, True)) == 1
            ):
                raise RuntimeError("list boom")
            return None

        port, commands, store, _signals, alerts = self._make_backup(
            [("vda", disk_a)],
            on_virsh=hook,
            backup_fn=lambda: (1, {}),
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        cfg = self._cfg()
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(cfg, port, "win", None, nas, quiesce=False, live=True)
        msg = str(ctx.exception)
        self.assertIn("DISK NOT MERGED BACK:\n  the disks could not be listed: RuntimeError('list boom')", msg)
        self.assertIn("(the backup itself: restic backup failed: restic exit 1; no backup recorded)", msg)
        self.assertEqual(len(alerts), 1)
        self.assertIn("the disks could not be listed: RuntimeError('list boom')", alerts[0][1])
        self.assertEqual(store.removed, [overlay_a])
        commits = [c for c in commands.virsh_log if c[0][0] == "blockcommit"]
        self.assertEqual(len(commits), 1)
        self.assertTrue(commits[0][1], commits)
        self.assertTrue(commits[0][2], commits)

    def test_find_overlays_raises_with_empty_overlaid_finds_overlay_on_disk(self) -> None:
        disk_a = "/img/a.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"

        def hook(
            args: Sequence[str],
            cmds: FakeNasCommands,
            sigs: FakeNasSignals,
            live: dict[str, str],
            store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "snapshot-create-as":
                store.overlays.add(overlay_a)
                live["vda"] = overlay_a
                raise RuntimeError("virsh aborted")
            if cmds.in_detached and sigs.ignored_active and args[0] == "domblklist":
                raise RuntimeError("blklist unavailable")
            return None

        port, _commands, _store, _signals, alerts = self._make_backup([("vda", disk_a)], on_virsh=hook)
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        cfg = self._cfg()
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(cfg, port, "win", None, nas, quiesce=False, live=True)
        msg = str(ctx.exception)
        self.assertIn(
            "DISK NOT MERGED BACK:\n  the disks could not be listed: RuntimeError('blklist unavailable')", msg
        )
        self.assertIn(overlay_a, msg)
        self.assertIn("(the backup itself: virsh aborted)", msg)
        self.assertEqual(len(alerts), 1)

    def test_commit_back_raises_unexpectedly_alerts_and_reports_recovery(self) -> None:
        disk_a = "/img/a.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"

        def hook(
            args: Sequence[str],
            cmds: FakeNasCommands,
            sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if cmds.in_detached and sigs.ignored_active and args[0] == "domstate":
                raise OSError("libvirt vanished")
            return None

        def fail_backup() -> tuple[int, vm_domain_backup.ResticSummary]:
            raise OSError("nas disconnected")

        port, _commands, _store, _signals, alerts = self._make_backup(
            [("vda", disk_a)], on_virsh=hook, backup_fn=fail_backup
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        cfg = self._cfg()
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(cfg, port, "win", None, nas, quiesce=False, live=True)
        msg = str(ctx.exception)
        self.assertIn("DISK NOT MERGED BACK:\n  the merge back failed: OSError('libvirt vanished')", msg)
        self.assertIn(overlay_a, msg)
        self.assertIn("(the backup itself: nas disconnected)", msg)
        self.assertEqual(len(alerts), 1)
        self.assertIn("the merge back failed: OSError('libvirt vanished')", alerts[0][1])

    def test_find_overlays_and_commit_back_both_raise_reports_both(self) -> None:
        disk_a = "/img/a.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"

        def hook(
            args: Sequence[str],
            cmds: FakeNasCommands,
            sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if cmds.in_detached and sigs.ignored_active and args[0] == "domblklist":
                raise RuntimeError("list failed")
            if cmds.in_detached and sigs.ignored_active and args[0] == "domstate":
                raise OSError("commit crashed")
            return None

        port, _commands, _store, _signals, alerts = self._make_backup([("vda", disk_a)], on_virsh=hook)
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        cfg = self._cfg()
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(cfg, port, "win", None, nas, quiesce=False, live=True)
        msg = str(ctx.exception)
        self.assertIn(
            "DISK NOT MERGED BACK:\n  the disks could not be listed: RuntimeError('list failed')\n"
            "  the merge back failed: OSError('commit crashed')",
            msg,
        )
        self.assertIn(overlay_a, msg)
        # The read was good (no stop, no failure of its own): its snapshot is named, not indexed.
        self.assertIn(
            f"(the backup itself: the restic snapshot {SNAP_ID[:8]} of win is a backup, its read checked", msg
        )
        self.assertEqual(len(alerts), 1)
        self.assertIn("list failed", alerts[0][1])
        self.assertIn("commit crashed", alerts[0][1])

    def _partial(self, rc: int) -> tuple[str, FakeNasCommands, FakeNasStore, list[bool]]:
        """nas_run of a VM with two disks whose snapshot answers rc (settled) with only vda switched;
        (the refusal, the commands, the store, restic's backups)."""
        disk_a, disk_b = "/img/a.qcow2", "/img/b.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
        overlay_b = f"{disk_b}{vm_domain.OVERLAY_MARK}{STAMP}"
        backups: list[bool] = []

        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            live: dict[str, str],
            store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] != "snapshot-create-as":
                return None
            store.overlays.update((overlay_a, overlay_b))  # libvirt made both files, switched vda only
            live["vda"] = overlay_a
            return (0, "", "") if rc == 0 else (rc, "", "error: internal error: snapshot failed")

        def backup() -> tuple[int, vm_domain_backup.ResticSummary]:
            backups.append(True)
            return 0, {"snapshot_id": SNAP_ID}

        port, commands, store, _signals, _alerts = self._make_backup(
            [("vda", disk_a), ("vdb", disk_b)], on_virsh=hook, backup_fn=backup
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=False, live=True)
        return str(ctx.exception), commands, store, backups

    def test_a_snapshot_that_switched_some_disks_only_is_no_backup(self) -> None:
        overlay_a = f"/img/a.qcow2{vm_domain.OVERLAY_MARK}{STAMP}"
        overlay_b = f"/img/b.qcow2{vm_domain.OVERLAY_MARK}{STAMP}"
        for rc in (0, 1):  # virsh answered ok, or with an error
            with self.subTest(rc=rc):
                msg, commands, store, backups = self._partial(rc)
                self.assertIn(
                    "virsh snapshot-create-as win switched only some disks (switched: vda; not: vdb): no backup, "
                    "the switched disks are merged back",
                    msg,
                )
                self.assertEqual(backups, [], "restic never read it")
                self.assertEqual(store.dropped, [overlay_b], "the file of the disk not switched")
                self.assertEqual(store.removed, [overlay_a], "the switched disk merged back, its file removed")
                cmds = [c[0][0] for c in commands.virsh_log]
                self.assertEqual(cmds.count("snapshot-create-as"), 1, "no second attempt")
                self.assertEqual([c[0][2] for c in commands.virsh_log if c[0][0] == "blockcommit"], ["vda"])

    def test_drop_stray_overlays_keeps_switched_disk_and_drops_unswitched(self) -> None:
        disk_a, disk_b = "/img/a.qcow2", "/img/b.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
        overlay_b = f"{disk_b}{vm_domain.OVERLAY_MARK}{STAMP}"
        port, _commands, store, _signals, _alerts = self._make_backup(
            [("vda", disk_a), ("vdb", disk_b)],
            on_virsh=lambda *_a: None,
        )
        store.overlays.update((overlay_a, overlay_b))
        run = vm_domain_backup.NasRun(
            vm="win",
            live=True,
            quiesce=False,
            xml_text="",
            parts=vm_domain.DomainParts(
                uuid=UUID, disks=[disk_a, disk_b], nvram="", tpm=False, skipped=[], problems=[]
            ),
            notes=[],
            sources=[(disk_a, "disk"), (disk_b, "disk")],
            epoch=float(NOW),
            stamp=STAMP,
            specs={"vda": (disk_a, overlay_a), "vdb": (disk_b, overlay_b)},
            others=[],
            agent="none",
            versions={},
            deployment=None,
            overlaid={"vda": (disk_a, overlay_a)},
        )
        vm_app_nas._drop_stray_overlays(port, run)
        self.assertEqual(store.dropped, [overlay_b])
        self.assertTrue(store.exists(overlay_a))


class _HeldStop(contextlib.AbstractContextManager[object]):
    """A held block that a stop came during: raised at its end, as signals_held delivers it."""

    @override
    def __exit__(self, *_: object) -> None:
        raise vm_domain_backup.Stop("stopped by signal SIGTERM")


class _StopAtHeldEnd(FakeNasSignals):
    """SignalsPort whose first held block ends with a stop that came during it."""

    def __init__(self) -> None:
        super().__init__()
        self.helds = 0

    @override
    def held(self) -> contextlib.AbstractContextManager[object]:
        self.helds += 1
        return _HeldStop() if self.helds == 1 else contextlib.nullcontext()


class NasHeldStopDropTest(unittest.TestCase):
    """A stop held through a settled snapshot call that left some disks unswitched is raised once the files
    of those disks are dropped (in the held block): none stays behind for a disk libvirt never used. An
    unsettled answer drops nothing (libvirt may still switch), nor does a poll that failed."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup
    disk_a, disk_b = "/img/a.qcow2", "/img/b.qcow2"
    overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
    overlay_b = f"{disk_b}{vm_domain.OVERLAY_MARK}{STAMP}"

    def _run(
        self, rc: int, switched: Sequence[str], *, disks: int = 2, poll_fails: bool = False
    ) -> tuple[BaseException, FakeNasCommands, FakeNasStore]:
        """nas_run of a running VM whose crash-consistent snapshot call answers rc, having made every
        disk's file and switched the disks named, with a stop held through it; (what nas_run raised, the
        commands, the store)."""
        pairs = [("vda", self.disk_a), ("vdb", self.disk_b)][:disks]
        overlays = {"vda": self.overlay_a, "vdb": self.overlay_b}
        snapped: list[bool] = []

        def hook(
            args: Sequence[str],
            cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            live: dict[str, str],
            store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "snapshot-create-as":
                snapped.append(True)
                store.overlays.update(overlays[t] for t, _ in pairs)
                for t in switched:
                    live[t] = overlays[t]
                return rc, "", ("" if rc == ExitStatus.TIMED_OUT else "error: internal error: snapshot failed")
            if args[0] == "domblklist" and poll_fails and snapped and not cmds.in_detached:
                raise OSError("libvirt vanished")
            return None

        port, commands, store, _signals, _alerts = self._make_backup(
            pairs, on_virsh=hook, signals=_StopAtHeldEnd(), clock=FakeNasMovingClock()
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(BaseException) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=False, live=True)
        return ctx.exception, commands, store

    def test_a_failed_call_that_switched_nothing_drops_its_files_before_the_stop(self) -> None:
        raised, commands, store = self._run(1, [], disks=1)
        self.assertIsInstance(raised, vm_domain_backup.Stop)
        self.assertEqual(store.dropped, [self.overlay_a])
        self.assertNotIn("blockcommit", [c[0][0] for c in commands.virsh_log])

    def test_a_call_that_switched_some_disks_drops_the_others_files_before_the_stop(self) -> None:
        for rc in (0, 1):
            with self.subTest(rc=rc):
                raised, commands, store = self._run(rc, ["vda"])
                self.assertIsInstance(raised, vm_domain_backup.Stop)
                self.assertEqual(store.dropped, [self.overlay_b])
                self.assertEqual(store.removed, [self.overlay_a], "vda merged back")
                self.assertEqual([c[0][2] for c in commands.virsh_log if c[0][0] == "blockcommit"], ["vda"])

    def test_an_unsettled_answer_drops_nothing(self) -> None:
        raised, _commands, store = self._run(int(ExitStatus.TIMED_OUT), ["vda"])
        self.assertIsInstance(raised, Refused)
        self.assertIn("may still switch to it", str(raised))
        self.assertEqual(store.dropped, [])

    def test_a_poll_that_fails_drops_nothing(self) -> None:
        # Passes on the code before the fix too: it guards the drop's condition (the poll answered).
        raised, _commands, store = self._run(1, [], poll_fails=True)
        self.assertIsInstance(raised, vm_domain_backup.Stop)  # the fake's held block ends with its stop
        self.assertEqual(store.dropped, [])


class FakeNasMovingClock(FakeNasClock):
    """A clock whose sleep moves monotonic on, so a poll until a deadline ends."""

    def __init__(self) -> None:
        self.t = 100.0

    @override
    def monotonic(self) -> float:
        return self.t

    @override
    def sleep(self, seconds: float) -> None:
        self.t += seconds


class NasUnclearSnapshotTest(unittest.TestCase):
    """A snapshot whose outcome stays unclear (virsh timed out, a disk never showed its overlay) alerts and
    names the overlay files however the read ends: libvirt may still switch a disk to one."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup

    def _timeout_hook(
        self, switched: Mapping[str, str], made: Sequence[str]
    ) -> Callable[
        [Sequence[str], FakeNasCommands, FakeNasSignals, dict[str, str], FakeNasStore], tuple[int, str, str] | None
    ]:
        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            live: dict[str, str],
            store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "snapshot-create-as":
                store.overlays.update(made)
                live.update(switched)
                return 124, "", ""  # virsh's timeout: no answer
            return None

        return hook

    def test_a_stop_held_through_an_unclear_snapshot_still_alerts_and_names_the_overlay(self) -> None:
        disk_a = "/img/a.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
        port, commands, store, _signals, alerts = self._make_backup(
            [("vda", disk_a)],
            on_virsh=self._timeout_hook({}, [overlay_a]),
            signals=_StopAtHeldEnd(),
            clock=FakeNasMovingClock(),
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=False, live=True)
        msg = str(ctx.exception)
        self.assertIn("DISK NOT MERGED BACK:\n  virsh snapshot-create-as win did not answer within", msg)
        self.assertIn(f"(kept: {overlay_a})", msg)
        self.assertIn(overlay_a, msg.split("by hand (read each line first):", 1)[1])
        self.assertIn("(the backup itself: stopped by signal SIGTERM)", msg)
        self.assertEqual(len(alerts), 1)
        self.assertIn("may still switch to it", alerts[0][1])
        self.assertEqual((store.dropped, store.removed), ([], []))
        self.assertTrue(store.exists(overlay_a), "never removed while libvirt may still switch to it")
        self.assertEqual([c for c in commands.virsh_log if c[0][0] == "blockcommit"], [])

    def test_a_stop_while_the_alert_of_an_unclear_snapshot_is_sent_still_refuses(self) -> None:
        disk_a = "/img/a.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
        port, _commands, store, signals, alerts = self._make_backup(
            [("vda", disk_a)], on_virsh=self._timeout_hook({}, [overlay_a]), clock=FakeNasMovingClock()
        )

        def alert(title: str, body: str) -> None:
            alerts.append((title, body))
            if not signals.ignored_active:  # on_signal still caught: a SIGTERM now raises Stop
                raise vm_domain_backup.Stop("stopped by signal SIGTERM")

        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(
                self._cfg(), dataclasses.replace(port, alert=alert), "win", None, nas, quiesce=False, live=True
            )
        msg = str(ctx.exception)
        self.assertIn("DISK NOT MERGED BACK:\n  virsh snapshot-create-as win did not answer within", msg)
        self.assertIn(overlay_a, msg.split("by hand (read each line first):", 1)[1])
        self.assertEqual(len(alerts), 1)
        self.assertTrue(store.exists(overlay_a))

    def test_an_unclear_snapshot_is_named_when_the_merge_back_of_another_disk_fails(self) -> None:
        disk_a, disk_b = "/img/a.qcow2", "/img/b.qcow2"
        overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
        overlay_b = f"{disk_b}{vm_domain.OVERLAY_MARK}{STAMP}"
        timeout = self._timeout_hook({"vda": overlay_a}, [overlay_a, overlay_b])

        def hook(
            args: Sequence[str], cmds: FakeNasCommands, sigs: FakeNasSignals, live: dict[str, str], store: FakeNasStore
        ) -> tuple[int, str, str] | None:
            if args[0] == "blockcommit":
                return 1, "", "error: block commit timed out"
            return timeout(args, cmds, sigs, live, store)

        port, _commands, store, _signals, alerts = self._make_backup(
            [("vda", disk_a), ("vdb", disk_b)], on_virsh=hook, clock=FakeNasMovingClock()
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=False, live=True)
        msg = str(ctx.exception)
        self.assertIn("vda: virsh blockcommit: error: block commit timed out", msg)
        self.assertIn(f"may still switch to it. Nothing was removed (kept: {overlay_b})", msg)
        self.assertIn(overlay_b, msg.split("by hand (read each line first):", 1)[1])
        self.assertEqual(len(alerts), 1)
        self.assertIn("may still switch to it", alerts[0][1])
        self.assertTrue(store.exists(overlay_b))


class NasRecoveryLinesTest(unittest.TestCase):
    """DISK NOT MERGED BACK gives the commands only for the disks still on an overlay: one the merge
    back put back (both listings show its image, its overlay removed) is not named again."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup
    _timeout_hook = NasUnclearSnapshotTest._timeout_hook
    disk_a, disk_b = "/img/a.qcow2", "/img/b.qcow2"
    overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"
    overlay_b = f"{disk_b}{vm_domain.OVERLAY_MARK}{STAMP}"

    def _refusal(self, port: vm_app_nas.NasBackup) -> str:
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(Refused) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=False, live=True)
        return str(ctx.exception)

    def test_a_disk_merged_back_is_not_named_when_another_disks_commit_fails(self) -> None:
        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "blockcommit" and args[2] == "vdb":
                return 1, "", "error: block commit failed"
            return None

        port, _commands, store, _signals, alerts = self._make_backup(
            [("vda", self.disk_a), ("vdb", self.disk_b)], on_virsh=hook
        )
        msg = self._refusal(port)
        self.assertIn("vdb: virsh blockcommit: error: block commit failed", msg)
        recovery = msg.split("by hand (read each line first):", 1)[1]
        self.assertIn(f"# vdb: the VM still runs on {self.overlay_b}", recovery)
        self.assertNotIn("vda", recovery)
        self.assertNotIn(self.overlay_a, recovery)
        self.assertEqual(store.removed, [self.overlay_a])
        self.assertEqual(len(alerts), 1)

    def test_a_disk_merged_back_is_not_named_after_an_unclear_snapshot(self) -> None:
        port, _commands, store, _signals, alerts = self._make_backup(
            [("vda", self.disk_a), ("vdb", self.disk_b)],
            on_virsh=self._timeout_hook({"vda": self.overlay_a}, [self.overlay_a, self.overlay_b]),
            clock=FakeNasMovingClock(),
        )
        msg = self._refusal(port)
        self.assertIn(f"may still switch to it. Nothing was removed (kept: {self.overlay_b})", msg)
        recovery = msg.split("by hand (read each line first):", 1)[1]
        self.assertIn(self.overlay_b, recovery)
        self.assertNotIn(self.overlay_a, recovery)
        self.assertNotIn("vda", recovery)
        self.assertEqual(store.removed, [self.overlay_a])
        self.assertTrue(store.exists(self.overlay_b))
        self.assertEqual(len(alerts), 1)

    def test_the_look_first_commands_stay_when_no_disk_is_left_to_name(self) -> None:
        # vda shows its overlay and is merged back; vdb never shows its overlay, and its file is not there.
        port, _commands, store, _signals, alerts = self._make_backup(
            [("vda", self.disk_a), ("vdb", self.disk_b)],
            on_virsh=self._timeout_hook({"vda": self.overlay_a}, [self.overlay_a]),
            clock=FakeNasMovingClock(),
        )
        msg = self._refusal(port)
        self.assertIn("may still switch to it", msg)
        recovery = msg.split("by hand (read each line first):", 1)[1]
        v = f"sudo virsh -c {vm_domain.URI}"
        self.assertIn("# vda: the VM's state or the files could not be read: look first", recovery)
        for line in (
            f"{v} domstate win",
            f"{v} domblklist win --details",
            f"{v} domblklist win --details --inactive",
            f"sudo ls -l --time-style=full-iso {self.disk_a} {self.overlay_a}",
        ):
            self.assertIn(line, recovery)
        self.assertEqual(store.removed, [self.overlay_a])
        self.assertEqual(len(alerts), 1)


class NasHeldStopThawTest(unittest.TestCase):
    """A stop held through a quiesced snapshot call is raised when the held block ends: the best-effort
    domfsthaw still runs before it whenever the call did not settle the guest's freeze."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup
    disk_a = "/img/a.qcow2"
    overlay_a = f"{disk_a}{vm_domain.OVERLAY_MARK}{STAMP}"

    def _run(
        self, rc: int, *, switched: bool
    ) -> tuple[Refused | vm_domain_backup.Stop, list[str], list[tuple[str, str]]]:
        """nas_run of a running VM with a connected guest agent whose quiesced snapshot call answers rc
        (switching the disk or only making its file) while a stop is held; (what it raised, the virsh
        commands, the alerts)."""
        disks = [("vda", self.disk_a)]

        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            live: dict[str, str],
            store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "dumpxml":
                chan = "<channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0' state='connected'/></channel>"
                return 0, fake_nas_xml([(t, live[t]) for t, _ in disks]).replace("</devices>", f"{chan}</devices>"), ""
            if args[0] == "snapshot-create-as":
                self.assertIn("--quiesce", args)
                store.overlays.add(self.overlay_a)
                if switched:
                    live["vda"] = self.overlay_a
                return rc, "", ("" if rc == ExitStatus.TIMED_OUT else "error: internal error: unexpected answer")
            return None

        port, commands, _store, _signals, alerts = self._make_backup(
            disks, on_virsh=hook, signals=_StopAtHeldEnd(), clock=FakeNasMovingClock()
        )
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises((Refused, vm_domain_backup.Stop)) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=True, live=True)
        return ctx.exception, [c[0][0] for c in commands.virsh_log], alerts

    def test_an_unclear_quiesced_snapshot_is_thawed_before_the_held_stop(self) -> None:
        raised, cmds, alerts = self._run(int(ExitStatus.TIMED_OUT), switched=False)
        self.assertIsInstance(raised, Refused)
        self.assertIn("may still switch to it", str(raised))
        self.assertEqual(cmds.count("domfsthaw"), 1)
        self.assertLess(cmds.index("snapshot-create-as"), cmds.index("domfsthaw"))
        self.assertEqual(len(alerts), 1)

    def test_a_quiesced_snapshot_that_timed_out_in_place_is_thawed_before_the_held_stop(self) -> None:
        raised, cmds, alerts = self._run(int(ExitStatus.TIMED_OUT), switched=True)
        self.assertIsInstance(raised, vm_domain_backup.Stop)
        self.assertEqual(cmds.count("domfsthaw"), 1)
        self.assertLess(cmds.index("domfsthaw"), cmds.index("blockcommit"))
        self.assertEqual(alerts, [])

    def test_a_quiesced_snapshot_that_failed_in_place_is_thawed_before_the_held_stop(self) -> None:
        raised, cmds, alerts = self._run(1, switched=True)
        self.assertIsInstance(raised, vm_domain_backup.Stop)
        self.assertEqual(cmds.count("domfsthaw"), 1)
        self.assertLess(cmds.index("domfsthaw"), cmds.index("blockcommit"))
        self.assertEqual(alerts, [])


# The stop of a run whose read was a backup: the snapshot is indexed before the stop ends the run.
STOPPED_INDEXED = (
    f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} of win is a backup: indexed (the retention runs "
    "at the next backup)"
)


def _written(store: FakeNasStore) -> list[list[tuple[str, object]]]:
    """Each index the run wrote: (snapshot, consistency) of each of its entries."""
    return [[(e.get("snapshot", ""), e.get("consistency")) for e in index] for index in store.written]


def _contexts(e: BaseException) -> list[type[BaseException]]:
    """The types along e's chain of __context__."""
    found: list[type[BaseException]] = []
    cur = e.__context__
    while cur is not None:
        found.append(type(cur))
        cur = cur.__context__
    return found


class NasStopAfterSnapshotTest(unittest.TestCase):
    """A stop that ended restic once restic had written its snapshot (StopAfterSnapshot, with restic's exit
    status and summary): a torn snapshot is recorded before the stop is raised, with no forget (the next
    run's), or named when the record fails; a good answer of a shut-off VM is checked first; the overlays
    are merged back."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup
    disk_a = "/img/a.qcow2"

    def _run(
        self,
        rc: int,
        *,
        live: bool = True,
        started: bool = False,
        record_fails: bool = False,
        check_raises: bool = False,
    ) -> tuple[str, FakeNasStore, FakeNasRestic, list[str]]:
        """nas_run whose restic is stopped after its snapshot with exit status rc (started: a shut-off VM
        runs from then on; check_raises: its check after the read meets an error of the virsh port); (the
        stop's message, the store, restic, the virsh commands). The stop itself is in self.stop."""
        disks = [("vda", self.disk_a)]
        answered: list[bool] = []

        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "domstate" and not live:
                if check_raises and answered:
                    raise RuntimeError("the virsh port failed")
                return 0, ("running" if started and answered else vm_domain.SHUT_OFF) + "\n", ""
            return None

        def backup() -> tuple[int, vm_domain_backup.ResticSummary]:
            answered.append(True)
            raise vm_domain_backup.StopAfterSnapshot("stopped by signal SIGTERM", rc, {"snapshot_id": SNAP_ID})

        port, commands, store, _signals, _alerts = self._make_backup(disks, on_virsh=hook, backup_fn=backup)
        store.record_fails = record_fails
        restic = port.restic
        assert isinstance(restic, FakeNasRestic)
        nas: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
        with self.assertRaises(vm_domain_backup.Stop) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, nas, quiesce=False, live=live)
        self.stop = ctx.exception
        return str(ctx.exception), store, restic, [c[0][0] for c in commands.virsh_log]

    def test_a_torn_snapshot_is_recorded_and_nothing_forgotten_before_the_stop(self) -> None:
        msg, store, restic, cmds = self._run(-signal.SIGTERM)
        self.assertEqual([(e["snapshot"], e["why"]) for e in store.torn], [(SNAP_ID, "stopped")])
        self.assertIn(f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup: recorded in ", msg)
        self.assertNotIn("forget", [c[0] for c in restic.calls])
        self.assertEqual(cmds.count("blockcommit"), 1)

    def test_a_torn_snapshot_whose_record_fails_is_named_by_the_stop(self) -> None:
        msg, store, restic, cmds = self._run(-signal.SIGTERM, record_fails=True)
        self.assertEqual(store.torn, [])
        self.assertIn(f"restic snapshot {SNAP_ID[:8]} is no backup and could NOT be recorded (", msg)
        self.assertIn(f"restic forget {SNAP_ID}", msg)
        self.assertNotIn("forget", [c[0] for c in restic.calls])
        self.assertEqual(cmds.count("blockcommit"), 1)

    def test_a_good_answer_of_a_running_vm_is_indexed_before_the_stop(self) -> None:
        # A snapshot restic ended on its own (exit 0) is not counted as torn: it is a backup, indexed.
        msg, store, restic, cmds = self._run(0)
        self.assertEqual((msg, store.torn), (STOPPED_INDEXED, []))
        self.assertEqual(_written(store), [[(SNAP_ID, "crash-consistent")]])
        self.assertEqual([c[0] for c in restic.calls], ["cat"], "restic_ready only: no listing, forget or retention")
        self.assertEqual(cmds.count("blockcommit"), 1)

    def test_a_good_answer_of_a_shut_off_vm_is_checked_then_indexed_before_the_stop(self) -> None:
        msg, store, restic, cmds = self._run(0, live=False)
        self.assertEqual((msg, store.torn), (STOPPED_INDEXED, []))
        self.assertEqual(_written(store), [[(SNAP_ID, "shut-off")]])
        self.assertEqual([c[0] for c in restic.calls], ["cat"], "restic_ready only")
        self.assertEqual(cmds.count("domstate"), 1, "the check after the read ran")

    def test_a_good_answer_whose_check_fails_is_recorded_as_torn_before_the_stop(self) -> None:
        msg, store, restic, _cmds = self._run(0, live=False, check_raises=True)
        self.assertEqual([(e["snapshot"], e["why"]) for e in store.torn], [(SNAP_ID, "check failed")])
        self.assertIn(f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup: recorded in ", msg)
        self.assertEqual(store.written, [])
        self.assertNotIn("forget", [c[0] for c in restic.calls], "the forget is the next run's")
        self.assertIn(RuntimeError, _contexts(self.stop), "the check's error stays in the stop's context")

    def test_a_good_answer_the_index_refuses_is_named_by_the_stop(self) -> None:
        refused = vm_domain_backup.IndexRefused("/i is format 2 of the vm-index", "use that cst vm")
        with mock.patch.object(FakeNasStore, "write_index", autospec=True, side_effect=refused):
            msg, store, _restic, _cmds = self._run(0)
        self.assertEqual(store.written, [])
        self.assertTrue(
            msg.startswith(
                f"stopped by signal SIGTERM; the restic snapshot {SNAP_ID[:8]} of win is complete but not in the "
                "index: /i is format 2 of the vm-index, "
            ),
            msg,
        )
        self.assertIn("the snapshot stays in the repository", msg)
        # It is no backup the retention counts: the next runs name it, and their retention waits for it.
        self.assertIn(
            f"their retention waits for it until it is kept as a backup (restic tag --add cst-adopted {SNAP_ID})", msg
        )
        self.assertNotIn("any VM backup of its tags", msg)

    def test_a_good_answer_whose_index_cannot_be_written_is_named_by_the_stop(self) -> None:
        # The OSError replaced the stop, and nothing named the good snapshot.
        with mock.patch.object(
            FakeNasStore, "write_index", autospec=True, side_effect=OSError(28, "No space left on device")
        ):
            msg, store, _restic, _cmds = self._run(0)
        self.assertEqual((store.written, store.torn), ([], []))
        self.assertEqual(
            msg,
            f"stopped by signal SIGTERM; the restic snapshot {SNAP_ID[:8]} of win is a backup, but writing the index "
            "failed ([Errno 28] No space left on device): the index may not name it; the snapshot stays in the "
            "repository: the next NAS backups may name it as a snapshot no record names, and their retention waits "
            "for it until it is kept "
            f"as a backup (restic tag --add cst-adopted {SNAP_ID}) or forgotten (restic forget {SNAP_ID}) (restore "
            'it by its ID: vm/README.md, section "Restore from the NAS")',
        )

    def test_a_shut_off_vm_started_during_the_read_has_its_snapshot_recorded(self) -> None:
        msg, store, restic, _cmds = self._run(0, live=False, started=True)
        self.assertEqual([(e["snapshot"], e["why"]) for e in store.torn], [(SNAP_ID, "state changed")])
        self.assertIn("is no backup: recorded in ", msg)
        self.assertNotIn("forget", [c[0] for c in restic.calls])


_NAS_REPO: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}


STRAY_A = "aaaa1111" + "a" * 56  # an indexed backup
STRAY_B = "bbbb2222" + "b" * 56  # a snapshot no record names
STRAY_C = "cccc3333" + "c" * 56
STRAY_D = "dddd4444" + "d" * 56
STRAY_T0 = "2026-10-25T04:30:00.000000001+02:00"  # before the change to winter time: an hour more offset
STRAY_T1 = "2026-11-01T04:30:00.123456789+01:00"
STRAY_T2 = "2026-11-08T04:30:00.5+01:00"


class _StrayRestic(FakeNasRestic):
    """restic with a fixed listing (None: restic snapshots fails), forget failing for the ids in fail."""

    def __init__(self, listed: Sequence[Mapping[str, object]] | None, fail: Sequence[str] = ()) -> None:
        super().__init__()
        self.listed = listed
        self.fail = fail

    @override
    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        del env, timeout
        self.calls.append(list(args))
        if args[0] == "snapshots":
            if self.listed is None:
                return 1, "", "Fatal: unable to open the repository"
            return 0, json.dumps(self.listed), ""
        if args[0] == "forget" and args[-1] in self.fail:
            return 1, "", "Fatal: repository is already locked"
        return 0, "", ""


class _StrayStore(FakeNasStore):
    """The index and the torn record as given (refuse: "index" or "torn" cannot be read)."""

    def __init__(
        self,
        index: Sequence[vm_domain_backup.IndexEntry],
        torn: Sequence[vm_domain_backup.TornEntry] = (),
        refuse: str = "",
    ) -> None:
        super().__init__([])
        self.index = list(index)
        self.torn_entries = list(torn)
        self.refuse = refuse

    @override
    def read_index(self) -> list[vm_domain_backup.IndexEntry]:
        if self.refuse == "index":
            raise vm_domain_backup.IndexRefused("/i is format 2 of the vm-index", "use the cst vm of that CST")
        return list(self.index)

    @override
    def read_torn(self) -> list[vm_domain_backup.TornEntry]:
        if self.refuse == "torn":
            raise vm_domain_backup.TornRefused("/t is not JSON")
        return list(self.torn_entries)


class _KeepSettings(FakeNasSettings):
    @override
    def number(self, key: str) -> int:
        return {"VM_NAS_KEEP_WEEKLY": 4, "VM_NAS_KEEP_MONTHLY": 3}.get(key, 0)


def _stray_entry(snap: str, *, vm: str = "win", repository: str = "/repo") -> vm_domain_backup.IndexEntry:
    return {"vm": vm, "mode": "nas", "snapshot": snap, "repository": repository, "host": "sophia", "epoch": 1}


def _listed(snap: str, time_: object, *tags: str, original: str | None = None) -> dict[str, object]:
    return {
        "id": snap,
        "time": time_,
        "tags": ["cst-vm", "vm:win", *tags],
        **({} if original is None else {"original": original}),
    }


class NasStraySnapshotTest(unittest.TestCase):
    """A NAS run compares the VM's snapshots restic lists with the index and the record of torn snapshots
    (retention_after_torn, unaccounted): one that neither names, newer than the oldest indexed one, is named
    in the output and in an alert, and the retention waits (it would count it as a backup); nothing is
    forgotten. With no NAS entry of the VM in the index, nothing is compared."""

    def _run(
        self,
        index: Sequence[vm_domain_backup.IndexEntry],
        listed: Sequence[Mapping[str, object]] | None,
        *,
        torn_kept: Sequence[vm_domain_backup.TornEntry] = (),
        refuse: str = "",
        made: str = "",
    ) -> tuple[list[str], list[tuple[str, str]], list[list[str]]]:
        """retention_after_torn of win (torn_kept: recorded torn snapshots whose forget fails); (the output,
        the alerts, restic's calls)."""
        signals = FakeNasSignals()
        restic = _StrayRestic(listed, [e["snapshot"] for e in torn_kept])
        alerts: list[tuple[str, str]] = []
        out = FakeNasOut()
        port = vm_app_nas.NasBackup(
            commands=FakeNasCommands(lambda _args: (0, "", ""), signals),
            store=_StrayStore(index, torn_kept, refuse),
            settings=_KeepSettings(),
            restic=restic,
            signals=signals,
            lock=lambda _vm: contextlib.nullcontext(),
            alert=lambda title, body: alerts.append((title, body)),
            clock=FakeNasClock(),
            out=out,
            root=True,
        )
        cfg = vm_wiring.vm_settings({"CST_PROG": "cst vm", "CST_VM_NONROOT_OK": "1"})
        vm_app_nas_torn.retention_after_torn(cfg, port, "win", _NAS_REPO, vm_domain_backup.NasNumbers(), made=made)
        return out.lines, alerts, restic.calls

    def assert_retention_ran(self, lines: Sequence[str], calls: Sequence[Sequence[str]]) -> None:
        self.assertTrue(any("--keep-weekly" in c for c in calls), calls)
        self.assertFalse([ln for ln in lines if "retention skipped" in ln], lines)

    def test_a_newer_snapshot_no_record_names_is_named_and_the_retention_waits(self) -> None:
        index, listed = [_stray_entry(STRAY_A)], [_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T2)]
        lines, alerts, calls = self._run(index, listed)
        line = (
            "  retention skipped: restic lists 1 snapshot of win that neither the index nor the record of torn "
            "snapshots names (each may be torn): bbbb2222 (2026-11-08T04:30:00+01:00); look at each (restic ls "
            f"{STRAY_B}), then forget it (restic forget {STRAY_B}) or keep it as a backup (restic tag --add "
            f"cst-adopted {STRAY_B}), with the repository's variables of vm/README.md, section \"Restore from the "
            'NAS"; the repository keeps more until each is forgotten or kept'
        )
        self.assertEqual(lines, [line])
        body = (
            "win: the NAS repository has 1 snapshot(s) of win that no record names (bbbb2222); its retention "
            "waits until each is forgotten (restic forget ID) or kept (restic tag --add cst-adopted ID): see "
            "the backup's output"
        )
        self.assertEqual(alerts, [("VM backup: a NAS snapshot no record names", body)])
        self.assertEqual(calls, [["snapshots", "--json", "--no-lock", "--host", "sophia", "--tag", "cst-vm,vm:win"]])

    def test_several_strays_are_named_together(self) -> None:
        listed = [_listed(STRAY_A, STRAY_T0), _listed(STRAY_B, STRAY_T1), _listed(STRAY_C, STRAY_T2)]
        lines, alerts, _calls = self._run([_stray_entry(STRAY_A)], listed)
        self.assertIn("restic lists 2 snapshots of win that neither", lines[0])
        self.assertIn(
            "bbbb2222 (2026-11-01T04:30:00+01:00), cccc3333 (2026-11-08T04:30:00+01:00); look at each (restic ls ID), "
            "then forget it (restic forget ID) or keep it as a backup (restic tag --add cst-adopted ID) (ID: each of "
            f"{STRAY_B} {STRAY_C}), with",
            lines[0],
        )
        self.assertEqual(len(alerts), 1)
        self.assertIn("2 snapshot(s) of win that no record names (bbbb2222, cccc3333)", alerts[0][1])

    def test_a_snapshot_older_than_every_indexed_one_is_left_alone(self) -> None:
        # An older CST's backup, or one from before the index was moved aside: not named.
        lines, alerts, calls = self._run(
            [_stray_entry(STRAY_A)], [_listed(STRAY_B, STRAY_T0), _listed(STRAY_A, STRAY_T1)]
        )
        self.assert_retention_ran(lines, calls)
        self.assertEqual(alerts, [])

    def test_times_are_compared_as_instants(self) -> None:
        # 03:30+01:00 is 02:30 UTC, after 03:00+02:00 (01:00 UTC): newer, although its text sorts first.
        index = [_stray_entry(STRAY_A)]
        listed = [_listed(STRAY_A, "2026-10-25T03:00:00+02:00"), _listed(STRAY_B, "2026-10-25T02:30:00+01:00")]
        lines, alerts, _calls = self._run(index, listed)
        self.assertIn("bbbb2222 (2026-10-25T02:30:00+01:00)", lines[0])
        self.assertEqual(len(alerts), 1)

    def test_no_nas_entry_of_the_vm_in_this_repository_compares_nothing(self) -> None:
        local: vm_domain_backup.IndexEntry = {"vm": "win", "mode": "reflink", "path": "/b/win", "epoch": 1}
        for index in (
            [],
            [local],
            [_stray_entry(STRAY_A, vm="other")],
            [_stray_entry(STRAY_A, repository="sftp:elsewhere:/r")],
        ):
            with self.subTest(index=index):
                lines, alerts, calls = self._run(index, [_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T2)])
                self.assertFalse([c for c in calls if c[0] == "snapshots"], "no listing")
                self.assert_retention_ran(lines, calls)
                self.assertEqual(alerts, [])

    def test_the_runs_own_snapshot_and_an_adopted_one_are_no_strays(self) -> None:
        index = [_stray_entry(STRAY_A)]
        for listed, made in (
            ([_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T2)], STRAY_B),
            ([_listed(STRAY_A, STRAY_T1), _listed(STRAY_C, STRAY_T2, "cst-adopted")], ""),
            ([_listed(STRAY_A, STRAY_T1)], ""),  # forgotten by hand: no longer listed
        ):
            with self.subTest(listed=listed, made=made):
                lines, alerts, calls = self._run(index, listed, made=made)
                self.assert_retention_ran(lines, calls)
                self.assertEqual(alerts, [])

    def test_a_snapshot_in_the_torn_record_is_left_to_its_retry(self) -> None:
        torn: vm_domain_backup.TornEntry = {
            "snapshot": STRAY_B,
            "vm": "win",
            "repository": "/repo",
            "host": "sophia",
            "timestamp": "20261108T033000Z",
            "why": "incomplete",
        }
        listed = [_listed(STRAY_A, STRAY_T0), _listed(STRAY_B, STRAY_T1)]
        lines, alerts, _calls = self._run([_stray_entry(STRAY_A)], listed, torn_kept=[torn])
        self.assertEqual(len(lines), 1)
        self.assertIn("retention skipped: restic forget bbbb2222 (torn, of an earlier run): Fatal: ", lines[0])
        self.assertNotIn("neither the index", lines[0])
        self.assertTrue(lines[0].endswith("; the repository keeps more until the next backup"), lines[0])
        self.assertEqual(alerts, [])
        # A stray beside it: both reasons, the stray named.
        listed.append(_listed(STRAY_C, STRAY_T2))
        lines, alerts, _calls = self._run([_stray_entry(STRAY_A)], listed, torn_kept=[torn])
        self.assertRegex(
            lines[0], r"retention skipped: restic forget bbbb2222 \(torn.*; restic lists 1 snapshot of win"
        )
        self.assertIn("cccc3333", lines[0])
        self.assertEqual(len(alerts), 1)

    def test_a_retagged_backup_is_accounted_for_by_its_original(self) -> None:
        # restic tag saved A again as C (original A): C is the indexed backup, not a stray.
        index = [_stray_entry(STRAY_B), _stray_entry(STRAY_A)]
        listed = [_listed(STRAY_B, STRAY_T0), _listed(STRAY_C, STRAY_T1, "cst-keep", original=STRAY_A)]
        lines, alerts, calls = self._run(index, listed)
        self.assert_retention_ran(lines, calls)
        self.assertEqual(alerts, [])

    def test_an_upgrade_names_a_backup_an_older_cst_left_out_of_the_index_until_it_is_adopted(self) -> None:
        # The state an older cst vm left: it dropped the entry of A once restic tag saved A again as C (it
        # read only the listed ids), and left a good read a stop ended (B) out of the index. Both are newer
        # than the oldest indexed backup (D), so the first run after the update names them; adopted, they
        # are counted and the next run is quiet.
        older = "dddd4444" + "d" * 56
        index = [_stray_entry(older)]
        listed = [
            _listed(older, STRAY_T0),
            _listed(STRAY_C, STRAY_T1, original=STRAY_A),
            _listed(STRAY_B, STRAY_T2),
        ]
        lines, alerts, calls = self._run(index, listed)
        self.assertIn("restic lists 2 snapshots of win that neither", lines[0])
        self.assertIn("cccc3333 (2026-11-01T04:30:00+01:00), bbbb2222 (2026-11-08T04:30:00+01:00)", lines[0])
        self.assertEqual(len(alerts), 1)
        self.assertEqual([c[0] for c in calls], ["snapshots"], "the retention waits")
        adopted = [
            _listed(older, STRAY_T0),
            _listed(STRAY_C, STRAY_T1, "cst-adopted", original=STRAY_A),
            _listed(STRAY_B, STRAY_T2, "cst-adopted"),
        ]
        lines, alerts, calls = self._run(index, adopted)
        self.assert_retention_ran(lines, calls)
        self.assertEqual(alerts, [])

    def test_a_short_id_in_the_index_names_the_listed_snapshot(self) -> None:
        lines, alerts, calls = self._run(
            [_stray_entry(STRAY_A[:8])], [_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T0)]
        )
        self.assert_retention_ran(lines, calls)
        self.assertEqual(alerts, [])

    def test_an_entry_of_another_vm_or_repository_accounts_for_nothing(self) -> None:
        index = [_stray_entry(STRAY_A), _stray_entry(STRAY_B, vm="other"), _stray_entry(STRAY_B, repository="/r2")]
        lines, alerts, _calls = self._run(index, [_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T2)])
        self.assertIn("bbbb2222", lines[0])
        self.assertEqual(len(alerts), 1)

    def test_a_time_that_cannot_be_read(self) -> None:
        # A stray's: named (the retention keeps more). The only indexed one's: no baseline, nothing named.
        lines, alerts, _calls = self._run([_stray_entry(STRAY_A)], [_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, "x")])
        self.assertIn("bbbb2222 (time unknown)", lines[0])
        self.assertEqual(len(alerts), 1)
        for when in ("x", None, "2026-11-01T04:30:00"):
            with self.subTest(when=when):
                lines, alerts, calls = self._run(
                    [_stray_entry(STRAY_A)], [_listed(STRAY_A, when), _listed(STRAY_B, STRAY_T2)]
                )
                self.assert_retention_ran(lines, calls)
                self.assertEqual(alerts, [])

    def test_what_cannot_be_read_makes_the_retention_wait_unnamed(self) -> None:
        listed = [_listed(STRAY_A, STRAY_T1), _listed(STRAY_B, STRAY_T2)]
        cases: tuple[tuple[Sequence[Mapping[str, object]] | None, str, str], ...] = (
            (
                None,
                "",
                "restic snapshots did not list the snapshots of win, so the snapshots no record names were not checked",
            ),
            (listed, "index", "the snapshots no record names were not checked (/i is format 2 of the vm-index: "),
            (listed, "torn", "the snapshots no record names were not checked (/t is not JSON)"),
        )
        for listed_now, refuse, said in cases:
            with self.subTest(listed=listed_now is not None, refuse=refuse):
                lines, alerts, calls = self._run([_stray_entry(STRAY_A)], listed_now, refuse=refuse)
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(said, lines[0])
                self.assertTrue(lines[0].endswith("the repository keeps more until the next backup"), lines[0])
                self.assertFalse([c for c in calls if "--keep-weekly" in c])
                self.assertEqual(alerts, [])


class ResticInstantTest(unittest.TestCase):
    """restic_instant: restic's RFC 3339 times as instants; anything else None."""

    def test_restics_forms(self) -> None:
        utc = datetime.UTC
        self.assertEqual(
            vm_domain_backup.restic_instant("2026-11-01T04:30:00.123456789+01:00"),
            datetime.datetime(2026, 11, 1, 3, 30, 0, 123456, tzinfo=utc),
        )
        self.assertEqual(
            vm_domain_backup.restic_instant("2026-11-01T03:30:00Z"), datetime.datetime(2026, 11, 1, 3, 30, tzinfo=utc)
        )

    def test_offsets_across_a_change_of_time_order_as_instants(self) -> None:
        before = vm_domain_backup.restic_instant("2026-10-25T02:59:00+02:00")
        after = vm_domain_backup.restic_instant("2026-10-25T02:01:00+01:00")
        assert before is not None and after is not None
        self.assertLess(before, after)

    def test_anything_else_is_none(self) -> None:
        for value in ("", "yesterday", "2026-11-01T04:30:00", 1794100000, None, ["2026-11-01T03:30:00Z"]):
            with self.subTest(value=value):
                self.assertIsNone(vm_domain_backup.restic_instant(value))


class _PendingStops(FakeNasSignals):
    """SignalsPort whose stops wait in held blocks, as signals_held: a stop (stop_now) that came in one is
    raised at the end of the outermost, else at once."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.pending = False

    def stop_now(self) -> None:
        if self.depth:
            self.pending = True
            return
        raise vm_domain_backup.Stop("stopped by signal SIGTERM")

    @override
    @contextlib.contextmanager
    def held(self) -> Generator[None]:
        self.depth += 1
        try:
            yield
        except BaseException:
            # As signals_held: the stop that waited is raised at the unmask also when the block raised; it
            # replaces the failure, which stays its __context__.
            self.depth -= 1
            self._raise_pending()
            raise
        self.depth -= 1
        self._raise_pending()

    def _raise_pending(self) -> None:
        if self.depth == 0 and self.pending:
            self.pending = False
            raise vm_domain_backup.Stop("stopped by signal SIGTERM")


# A child that blocks a stop in the real signals_held (StopSignals), sends itself SIGTERM in the block and
# then raises: what ends the block, and the exception that stays its __context__.
SIGNALS_HELD_CHILD = r"""
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from vm.adapters.backup import StopSignals
signals = StopSignals()
signals.catch_stops()
try:
    with signals.held():
        with signals.held():
            os.kill(os.getpid(), signal.SIGTERM)
            raise RuntimeError("the block failed")
except BaseException as e:
    print(type(e).__name__, type(e.__context__).__name__)
"""


class PendingStopsMatchSignalsHeldTest(unittest.TestCase):
    """_PendingStops, the fake of the stops held, does what the real signals_held does when its block
    raises with a stop pending: the stop ends the outermost block, the failure its __context__."""

    def test_the_real_signals_held_raises_the_stop_over_the_blocks_failure(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", "-B", "-c", SIGNALS_HELD_CHILD, _CHECKOUT],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        self.assertEqual((r.returncode, r.stdout), (0, "Stop RuntimeError\n"), r.stderr)

    def test_the_fake_does_the_same(self) -> None:
        signals = _PendingStops()
        with self.assertRaises(vm_domain_backup.Stop) as ctx, signals.held(), signals.held():
            signals.stop_now()
            raise RuntimeError("the block failed")
        self.assertIsInstance(ctx.exception.__context__, RuntimeError)
        self.assertEqual((signals.depth, signals.pending), (0, False))

    def test_a_failure_with_no_stop_pending_goes_on(self) -> None:
        signals = _PendingStops()
        with self.assertRaisesRegex(RuntimeError, "the block failed"), signals.held():
            raise RuntimeError("the block failed")
        self.assertEqual(signals.depth, 0)


class NasStopHeldFromAnswerTest(unittest.TestCase):
    """From restic's answer with a snapshot on, a stop waits (the staged files' removal, the shut-off VM's
    check) until a torn snapshot is recorded or the check is done: it never leaves a torn snapshot neither
    recorded nor named."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup
    disk_a = "/img/a.qcow2"

    def _shut_off(self, *, started: bool) -> tuple[str, FakeNasStore, FakeNasRestic, int, int]:
        """nas_run of a shut-off VM whose check after the read gets a stop (started: the VM runs by then);
        (the stop's message, the store, restic, the fingerprints taken by the check's start, in all)."""
        signals = _PendingStops()
        answered: list[bool] = []
        at_check: list[int] = []

        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] != "domstate":
                return None
            if answered:
                at_check.append(fingerprint.call_count)
                signals.stop_now()
            return 0, ("running" if started and answered else vm_domain.SHUT_OFF) + "\n", ""

        def backup() -> tuple[int, vm_domain_backup.ResticSummary]:
            answered.append(True)
            return 0, {"snapshot_id": SNAP_ID}

        port, _commands, store, _signals, _alerts = self._make_backup(
            [("vda", self.disk_a)], on_virsh=hook, backup_fn=backup, signals=signals
        )
        restic = port.restic
        assert isinstance(restic, FakeNasRestic)
        with (
            mock.patch.object(store, "fingerprint", wraps=store.fingerprint) as fingerprint,
            self.assertRaises(vm_domain_backup.Stop) as ctx,
        ):
            vm_app_nas.nas_run(self._cfg(), port, "win", None, _NAS_REPO, quiesce=False, live=False)
        return str(ctx.exception), store, restic, at_check[0], fingerprint.call_count

    def test_a_stop_at_the_staged_files_removal_waits_for_the_torn_record(self) -> None:
        signals = _PendingStops()
        port, commands, store, _signals, _alerts = self._make_backup(
            [("vda", self.disk_a)],
            on_virsh=lambda *_: None,
            backup_fn=lambda: (3, {"snapshot_id": SNAP_ID}),
            signals=signals,
        )
        restic = port.restic
        assert isinstance(restic, FakeNasRestic)
        with (
            mock.patch.object(store, "unstage", side_effect=lambda _folder: signals.stop_now()),
            self.assertRaises(vm_domain_backup.Stop) as ctx,
        ):
            vm_app_nas.nas_run(self._cfg(), port, "win", None, _NAS_REPO, quiesce=False, live=True)
        self.assertEqual([(e["snapshot"], e["why"]) for e in store.torn], [(SNAP_ID, "incomplete")])
        self.assertIn(
            f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup: recorded in ", str(ctx.exception)
        )
        self.assertNotIn("forget", [c[0] for c in restic.calls], "the forget is the next run's")
        self.assertEqual([c[0][0] for c in commands.virsh_log].count("blockcommit"), 1, "merged back")

    def test_a_stop_during_the_check_of_a_started_vm_waits_for_the_torn_record(self) -> None:
        msg, store, restic, _at_check, _total = self._shut_off(started=True)
        self.assertEqual([(e["snapshot"], e["why"]) for e in store.torn], [(SNAP_ID, "state changed")])
        self.assertIn(f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} is no backup: recorded in ", msg)
        self.assertNotIn("forget", [c[0] for c in restic.calls])

    def test_a_stop_during_the_check_of_a_vm_still_shut_off_waits_for_the_checks_end(self) -> None:
        msg, store, restic, at_check, total = self._shut_off(started=False)
        self.assertEqual((msg, store.torn), (STOPPED_INDEXED, []))
        self.assertEqual(_written(store), [[(SNAP_ID, "shut-off")]], "the checked read is indexed")
        self.assertEqual([c[0] for c in restic.calls], ["cat"], "restic_ready only")
        self.assertGreater(total, at_check, "the sources' fingerprints were taken after the stop came")

    def _live_held(self, *, commit_fails: bool) -> tuple[BaseException, FakeNasStore, list[int]]:
        """nas_run of a running VM whose restic answers exit 0 with a snapshot and gets a stop at the staged
        files' removal (held); (what ended the run, the store, the blockcommits done when the index was
        written)."""
        signals = _PendingStops()

        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] == "blockcommit" and commit_fails:
                return 1, "", "error: block commit timed out"
            return None

        port, commands, store, _signals, _alerts = self._make_backup(
            [("vda", self.disk_a)], on_virsh=hook, backup_fn=lambda: (0, {"snapshot_id": SNAP_ID}), signals=signals
        )
        commits: list[int] = []
        write = store.write_index

        def counted(entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
            commits.append([c[0][0] for c in commands.virsh_log].count("blockcommit"))
            write(entries)

        with (
            mock.patch.object(store, "unstage", side_effect=lambda _folder: signals.stop_now()),
            mock.patch.object(store, "write_index", side_effect=counted),
            self.assertRaises(Exception) as ctx,
        ):
            vm_app_nas.nas_run(self._cfg(), port, "win", None, _NAS_REPO, quiesce=False, live=True)
        return ctx.exception, store, commits

    def test_a_stop_at_the_staged_files_removal_of_a_good_read_indexes_it_after_the_merge_back(self) -> None:
        stop, store, commits = self._live_held(commit_fails=False)
        self.assertIsInstance(stop, vm_domain_backup.Stop)
        self.assertEqual((str(stop), store.torn), (STOPPED_INDEXED, []))
        self.assertEqual(_written(store), [[(SNAP_ID, "crash-consistent")]])
        self.assertEqual(commits, [1], "the index is written after the merge back")

    def test_a_held_stop_after_a_good_read_prints_the_runs_notes_first(self) -> None:
        # They were dropped: a TPM copy that may be torn went unsaid for a backup the stop calls indexed.
        note = "tpm changed while restic read it (/var/lib/swtpm): that copy may be torn"
        lines: list[str] = []
        preflight = vm_app_nas._nas_preflight

        def noted(
            cfg: vm_domain.VmSettings, p: vm_app_nas.NasBackup, vm: str, *, live: bool, quiesce: bool
        ) -> vm_domain_backup.NasRun:
            run = preflight(cfg, p, vm, live=live, quiesce=quiesce)
            run.notes.append(note)
            return run

        def say(_out: FakeNasOut, text: str, *, flush: bool = False) -> None:
            del flush
            lines.append(text)

        with (
            mock.patch.object(vm_app_nas, "_nas_preflight", side_effect=noted),
            mock.patch.object(FakeNasOut, "say", autospec=True, side_effect=say),
        ):
            stop, _store, _commits = self._live_held(commit_fails=False)
        self.assertEqual(str(stop), STOPPED_INDEXED)
        self.assertIn(f"  note: {note}", lines)

    def test_a_stop_after_a_good_read_whose_merge_back_fails_indexes_nothing(self) -> None:
        refused, store, _commits = self._live_held(commit_fails=True)
        self.assertIsInstance(refused, Refused)
        self.assertNotIsInstance(refused, vm_domain_backup.Stop)
        self.assertEqual((store.written, store.torn), ([], []))
        # The refusal names the good snapshot, which the next run names as one no record names.
        self.assertIn(
            f"\n(the backup itself: stopped by signal SIGTERM; the restic snapshot {SNAP_ID[:8]} of win is a backup, "
            "its read checked, but it is not in the index: the snapshot stays in the repository: the next NAS "
            "backups may name it as a snapshot no record names, and their retention waits for it until it is kept "
            f"as a backup (restic tag --add cst-adopted {SNAP_ID}) or forgotten (restic forget {SNAP_ID}) (restore "
            'it by its ID: vm/README.md, section "Restore from the NAS"))',
            str(refused),
        )

    def _check_raises(self, *, stop: bool) -> tuple[BaseException, FakeNasStore, FakeNasRestic]:
        """nas_run of a shut-off VM whose restic answers exit 0 with a snapshot, no stop carried, and whose
        check after the read meets an error of the virsh port (stop: a stop held came before it)."""
        signals = _PendingStops()
        answered: list[bool] = []

        def hook(
            args: Sequence[str],
            _cmds: FakeNasCommands,
            _sigs: FakeNasSignals,
            _live: dict[str, str],
            _store: FakeNasStore,
        ) -> tuple[int, str, str] | None:
            if args[0] != "domstate":
                return None
            if answered:
                if stop:
                    signals.stop_now()
                raise RuntimeError("the virsh port failed")
            return 0, vm_domain.SHUT_OFF + "\n", ""

        def backup() -> tuple[int, vm_domain_backup.ResticSummary]:
            answered.append(True)
            return 0, {"snapshot_id": SNAP_ID}

        port, _commands, store, _signals, _alerts = self._make_backup(
            [("vda", self.disk_a)], on_virsh=hook, backup_fn=backup, signals=signals
        )
        restic = port.restic
        assert isinstance(restic, FakeNasRestic)
        with self.assertRaises(Exception) as ctx:
            vm_app_nas.nas_run(self._cfg(), port, "win", None, _NAS_REPO, quiesce=False, live=False)
        return ctx.exception, store, restic

    def test_a_check_that_fails_with_no_stop_carried_records_nothing(self) -> None:
        # Unchanged: only a stop that ended restic makes such a snapshot torn; else the error (or the stop
        # that waited, which replaces it) goes on, and the next run's check names the snapshot.
        for stop in (False, True):
            with self.subTest(stop=stop):
                e, store, restic = self._check_raises(stop=stop)
                if stop:
                    self.assertEqual(str(e), "stopped by signal SIGTERM")
                    self.assertIsInstance(e.__context__, RuntimeError)
                else:
                    self.assertIsInstance(e, RuntimeError)
                self.assertEqual((store.torn, store.written), ([], []))
                self.assertEqual([c[0] for c in restic.calls], ["cat"], "restic_ready only")


class NasStopAfterTheMergeBackTest(unittest.TestCase):
    """A stop after a good read's merge back (in the retention, the index's listing or its write) ends the
    run only once the snapshot is indexed, with no listing: else the next run names that backup as a
    snapshot no record names."""

    _cfg = NasMergeBackAndStrayTest._cfg
    _make_backup = NasMergeBackAndStrayTest._make_backup
    disk_a = "/img/a.qcow2"

    def _run(self, where: str) -> tuple[BaseException, FakeNasStore, list[list[str]], FakeNasOut]:
        """nas_run of a running VM whose good read is merged back, a stop coming where: "retention" (its
        restic forget), "listing" (the index's restic snapshots) or "write" (the index's first write);
        (what ended the run, the store, restic's calls, the output)."""
        signals = _PendingStops()
        port, _commands, store, _signals, _alerts = self._make_backup(
            [("vda", self.disk_a)], on_virsh=lambda *_: None, signals=signals
        )
        port = dataclasses.replace(port, settings=_KeepSettings())
        restic = port.restic
        assert isinstance(restic, FakeNasRestic)
        out = port.out
        assert isinstance(out, FakeNasOut)
        run, write = restic.run, store.write_index

        def restic_run(env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
            answer = run(env, args, timeout)
            if (where == "retention" and "--keep-weekly" in args) or (where == "listing" and args[0] == "snapshots"):
                signals.stop_now()
            return answer

        def write_index(entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
            if where == "write" and not store.written:
                store.written.append([])  # the first write started, cut short by the stop
                signals.stop_now()
            write(entries)

        with (
            mock.patch.object(restic, "run", side_effect=restic_run),
            mock.patch.object(store, "write_index", side_effect=write_index),
            self.assertRaises(BaseException) as ctx,
        ):
            vm_app_nas.nas_run(self._cfg(), port, "win", None, _NAS_REPO, quiesce=False, live=True)
        return ctx.exception, store, restic.calls, out

    def test_the_backup_is_indexed_before_the_stop_ends_the_run(self) -> None:
        for where in ("retention", "listing", "write"):
            with self.subTest(where=where):
                stop, store, calls, _out = self._run(where)
                self.assertIsInstance(stop, vm_domain_backup.Stop)
                self.assertEqual(str(stop), STOPPED_INDEXED)
                self.assertEqual(_written(store)[-1], [(SNAP_ID, "crash-consistent")])
                self.assertEqual(store.torn, [])
                retention = [c for c in calls if "--keep-weekly" in c]
                self.assertEqual(len(retention), 1, "the retention ran once, before the stop")
                self.assertEqual([c for c in calls if c[0] == "forget" and c not in retention], [], "nothing forgotten")

    def test_the_runs_notes_are_printed_before_the_stop(self) -> None:
        # They were dropped: a TPM copy that may be torn went unsaid for a backup the stop calls indexed.
        note = "tpm changed while restic read it (/var/lib/swtpm): that copy may be torn"
        preflight = vm_app_nas._nas_preflight

        def noted(
            cfg: vm_domain.VmSettings, p: vm_app_nas.NasBackup, vm: str, *, live: bool, quiesce: bool
        ) -> vm_domain_backup.NasRun:
            run = preflight(cfg, p, vm, live=live, quiesce=quiesce)
            run.notes.append(note)
            return run

        for where in ("retention", "listing", "write"):
            with self.subTest(where=where), mock.patch.object(vm_app_nas, "_nas_preflight", side_effect=noted):
                stop, _store, _calls, out = self._run(where)
                self.assertEqual(str(stop), STOPPED_INDEXED)
                self.assertIn(f"  note: {note}", out.lines)

    def test_a_stop_in_the_retention_lists_nothing_more(self) -> None:
        _stop, _store, calls, out = self._run("retention")
        self.assertEqual([c[0] for c in calls].count("snapshots"), 0, "no listing after the stop")
        self.assertNotIn("Done:", " ".join(out.lines))


class _LingeringTimer(threading.Timer):
    """A Timer whose thread goes on a while after cancel(), as a thread can under load."""

    @override
    def run(self) -> None:
        super().run()
        time.sleep(0.5)


class ResticBackupThreadTest(unittest.TestCase):
    """Restic.backup's timeout timer is a thread: none may outlive the backup, since a stop signal can go
    to any thread that does not block it, and signals_held (the record of a torn snapshot) blocks it
    only in the main thread."""

    def test_the_timers_thread_blocks_the_stops(self) -> None:
        # A stop that went to the timer's thread would run its handler in the main thread while it holds
        # the stops (a read of restic's output, the torn record).
        masks: list[set[int]] = []

        class _Recording(threading.Timer):
            @override
            def run(self) -> None:
                masks.append({int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])})

        class _Out:
            def say(self, text: str, *, flush: bool = False) -> None:
                pass

            def warn(self, text: str) -> None:
                pass

        class _Child:
            stdout = io.BytesIO(b"")

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        with (
            mock.patch.object(vm_adapters_restic.subprocess, "Popen", autospec=True, return_value=_Child()),
            mock.patch.object(vm_adapters_restic.threading, "Timer", _Recording),
        ):
            vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, _Out())
        self.assertEqual(len(masks), 1)
        self.assertLessEqual({int(signal.SIGTERM), int(signal.SIGHUP), int(signal.SIGINT)}, masks[0])
        self.assertNotIn(
            int(signal.SIGTERM), signal.pthread_sigmask(signal.SIG_BLOCK, []), "the main thread's mask is back"
        )

    def test_no_thread_of_the_backup_outlives_it(self) -> None:
        made: list[threading.Timer] = []

        def lingering(interval: float, function: Callable[[], object]) -> threading.Timer:
            made.append(_LingeringTimer(interval, function))
            return made[-1]

        class _Out:
            def say(self, text: str, *, flush: bool = False) -> None:
                pass

            def warn(self, text: str) -> None:
                pass

        class _Child:  # restic backup --json that printed its summary and exited 0
            stdout = io.BytesIO(b'{"message_type": "summary", "snapshot_id": "abc"}\n')

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        with (
            mock.patch.object(vm_adapters_restic.subprocess, "Popen", autospec=True, return_value=_Child()),
            mock.patch.object(vm_adapters_restic.threading, "Timer", lingering),
        ):
            rc, summary = vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, _Out())
        self.assertEqual((rc, summary.get("snapshot_id")), (0, "abc"))
        self.assertEqual(len(made), 1)
        self.assertFalse(made[0].is_alive(), "the timer's thread is still running")


class ResticBackupAnsweredTest(unittest.TestCase):
    """Restic.backup's answered: called once restic has exited, only when its summary names its snapshot;
    a stop it raises comes as one after the snapshot (StopAfterSnapshot, with restic's exit status)."""

    class _Out:
        def say(self, text: str, *, flush: bool = False) -> None:
            pass

        def warn(self, text: str) -> None:
            pass

    class _Child:  # restic backup --json that exited 0
        def __init__(self, text: bytes) -> None:
            self.stdout = io.BytesIO(text)
            self.waited = False

        def poll(self) -> int | None:
            return 0 if self.waited else None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.waited = True
            return 0

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    SUMMARY = b'{"message_type": "summary", "snapshot_id": "abc"}\n'

    def _backup(self, child: _Child, answered: Callable[[], None]) -> tuple[int, vm_domain_backup.ResticSummary]:
        with mock.patch.object(vm_adapters_restic.subprocess, "Popen", autospec=True, return_value=child):
            return vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup(
                {}, ["backup"], 60, self._Out(), answered=answered
            )

    def test_it_is_called_once_after_restic_exited_with_a_snapshot(self) -> None:
        child = self._Child(self.SUMMARY)
        seen: list[bool] = []
        rc, summary = self._backup(child, lambda: seen.append(child.waited))
        self.assertEqual((rc, summary.get("snapshot_id"), seen), (0, "abc", [True]))

    def test_it_is_not_called_without_a_snapshot(self) -> None:
        seen: list[bool] = []
        rc, summary = self._backup(self._Child(b""), lambda: seen.append(True))
        self.assertEqual((rc, summary, seen), (0, {}, []))

    def test_a_stop_it_raises_carries_the_snapshot(self) -> None:
        def stop() -> None:
            raise vm_domain_backup.Stop("stopped by signal SIGTERM")

        with self.assertRaises(vm_domain_backup.StopAfterSnapshot) as ctx:
            self._backup(self._Child(self.SUMMARY), stop)
        self.assertEqual((str(ctx.exception), ctx.exception.rc), ("stopped by signal SIGTERM", 0))
        self.assertEqual(ctx.exception.summary.get("snapshot_id"), "abc")


class ResticBackupOutputTest(unittest.TestCase):
    """Restic.backup closes the pipe it reads restic's output from, whether the backup ends or fails."""

    class _Child:  # restic backup --json, its output a pipe
        def __init__(self, text: str) -> None:
            self.stdout = io.BytesIO(text.encode())
            self.terminated = False

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            pass

    class _Out:
        def say(self, text: str, *, flush: bool = False) -> None:
            if "not json" in text:
                raise RuntimeError("the terminal went away")

        def warn(self, text: str) -> None:
            pass

    def _backup(self, child: _Child) -> tuple[int, object]:
        with mock.patch.object(vm_adapters_restic.subprocess, "Popen", return_value=child):
            return vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, self._Out())

    def test_the_output_is_closed_after_a_backup(self) -> None:
        child = self._Child('{"message_type": "summary", "snapshot_id": "abc"}\n')
        self.assertEqual(self._backup(child)[0], 0)
        self.assertTrue(child.stdout.closed)

    def test_the_output_is_closed_when_showing_it_fails(self) -> None:
        child = self._Child("not json\n")
        with self.assertRaisesRegex(RuntimeError, "the terminal went away"):
            self._backup(child)
        self.assertTrue(child.terminated)
        self.assertTrue(child.stdout.closed)


# select's FD_SETSIZE (glibc's): select refuses a descriptor at or above it.
SELECT_FD_LIMIT = 1024


class ResticOutputDescriptorTest(unittest.TestCase):
    """restic's output is read whatever the number of its pipe's descriptor, also one at or above select's
    FD_SETSIZE: a process with many descriptors open gets such a pipe."""

    def _high_pipe(self) -> tuple[io.FileIO, int]:
        """A pipe whose read end has a descriptor at or above SELECT_FD_LIMIT: (read end, write end)."""
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft <= SELECT_FD_LIMIT:
            # Up to the hard limit only: an unprivileged process cannot raise it (a hard limit at or below
            # SELECT_FD_LIMIT leaves no such descriptor, and the test fails).
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, SELECT_FD_LIMIT * 2), hard))
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        high = fcntl.fcntl(r, fcntl.F_DUPFD_CLOEXEC, SELECT_FD_LIMIT)
        os.close(r)
        stream = io.FileIO(high, "rb")
        self.addCleanup(stream.close)
        self.assertGreaterEqual(stream.fileno(), SELECT_FD_LIMIT)
        return stream, w

    def test_a_line_from_a_high_descriptor_is_read(self) -> None:
        stream, w = self._high_pipe()
        os.write(w, b'{"message_type": "status"}\n')
        output = vm_adapters_restic._Output(stream)
        self.assertTrue(output.fill(5.0))
        self.assertEqual(output.line(), '{"message_type": "status"}\n')

    def test_a_high_descriptor_with_nothing_to_read_times_out(self) -> None:
        stream, _ = self._high_pipe()
        self.assertFalse(vm_adapters_restic._Output(stream).fill(0.01))


# A child that lowers its own limit of open files to a hard limit between SELECT_FD_LIMIT and twice it (as
# some containers and CI runners set it), then runs a test of ResticOutputDescriptorTest.
LOW_HARD_LIMIT_CHILD = r"""
import resource, sys, unittest
sys.path.insert(0, sys.argv[1])
hard = min(resource.getrlimit(resource.RLIMIT_NOFILE)[1], 1536)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 1024), hard))
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[2])
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=0).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun == 1 else 1)
"""


class ResticOutputDescriptorLimitTest(unittest.TestCase):
    """ResticOutputDescriptorTest raises the soft limit of open files only up to the hard limit."""

    def test_it_runs_under_a_hard_limit_below_twice_select_fd_limit(self) -> None:
        name = f"{__name__}.ResticOutputDescriptorTest.test_a_line_from_a_high_descriptor_is_read"
        r = subprocess.run(
            [sys.executable, "-I", "-B", "-c", LOW_HARD_LIMIT_CHILD, _CHECKOUT, name],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class ResticBackupStartStopTest(unittest.TestCase):
    """A stop that waited while the timeout timer started is raised when the stops are let in again: restic
    is ended and the timer cancelled and joined, as for a stop while restic is read."""

    class _Child:  # restic backup --json that keeps running until it is ended
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")
            self.terminated = False

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return -signal.SIGTERM

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            pass

    class _Out:
        def say(self, text: str, *, flush: bool = False) -> None:
            pass

        def warn(self, text: str) -> None:
            pass

    def test_a_stop_let_in_after_the_timer_started_ends_restic_and_the_timer(self) -> None:
        made: list[threading.Timer] = []
        real_timer = threading.Timer

        def timer(interval: float, function: Callable[[], object]) -> threading.Timer:
            made.append(real_timer(interval, function))
            self.addCleanup(made[-1].cancel)
            return made[-1]

        unmasks: list[int] = []

        class _StopAtTheTimersUnmask(contextlib.AbstractContextManager[object]):
            # signals_held: a stop that came while the stops were held is raised at the unmask (once: the
            # stops are ignored after it, on_signal). The first hold is the one around Popen; the second,
            # the timer's start.
            old: frozenset[int] = frozenset()

            @override
            def __exit__(self, *_: object) -> None:
                unmasks.append(1)
                if len(unmasks) == 2:
                    raise vm_domain_backup.Stop("stopped by signal SIGTERM")

        child = self._Child()
        with (
            mock.patch.object(vm_adapters_restic.subprocess, "Popen", return_value=child),
            mock.patch.object(vm_adapters_restic.threading, "Timer", timer),
            mock.patch.object(vm_adapters_restic, "signals_held", _StopAtTheTimersUnmask),
            self.assertRaises(vm_domain_backup.Stop) as ctx,
        ):
            vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, self._Out())
        self.assertIs(type(ctx.exception), vm_domain_backup.Stop)
        self.assertTrue(child.terminated, "restic is still running")
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].finished.is_set(), "the timer was not cancelled")
        self.assertFalse(made[0].is_alive(), "the timer's thread is still running")
        self.assertTrue(child.stdout.closed)


SUMMARY_LINE = (
    json.dumps({"message_type": "summary", "snapshot_id": SNAP_ID, "total_bytes_processed": 2048, "data_added": 1024})
    + "\n"
)
# An error line: shown on stderr (out.warn), where the tests' stop comes.
ERROR_LINE = json.dumps({"message_type": "error", "error": {"message": "permission denied"}, "item": "/x"}) + "\n"


class ResticBackupStopTest(unittest.TestCase):
    """A stop while restic backup runs: restic is ended, and when restic had written its snapshot (its
    summary, kept or still in the pipe), StopAfterSnapshot carries restic's exit status and the summary."""

    class _Child:  # restic backup --json, its output a pipe; exited: its exit status when it is gone already
        def __init__(self, text: str, *, exited: int | None = None) -> None:
            self.stdout = io.BytesIO(text.encode())
            self.exited = exited
            self.terminated = False

        def poll(self) -> int | None:
            return self.exited

        def wait(self, timeout: float | None = None) -> int:
            return -signal.SIGTERM if self.terminated else (self.exited or 0)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            pass

    class _Out:  # the stop comes while an error line is shown
        def say(self, text: str, *, flush: bool = False) -> None:
            pass

        def warn(self, text: str) -> None:
            raise vm_domain_backup.Stop("stopped by signal SIGTERM")

    def _backup(self, child: _Child) -> tuple[int, object]:
        with mock.patch.object(vm_adapters_restic.subprocess, "Popen", return_value=child):
            return vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, self._Out())

    def _carried(self, child: _Child) -> vm_domain_backup.StopAfterSnapshot:
        with self.assertRaises(vm_domain_backup.StopAfterSnapshot) as ctx:
            self._backup(child)
        self.assertEqual(str(ctx.exception), "stopped by signal SIGTERM")
        self.assertTrue(child.stdout.closed)
        return ctx.exception

    def test_a_stop_with_the_summary_still_in_the_pipe_carries_the_snapshot(self) -> None:
        child = self._Child(ERROR_LINE + '{"message_type": "status"}\n' + SUMMARY_LINE)
        e = self._carried(child)
        self.assertEqual((e.rc, e.summary.get("snapshot_id")), (-signal.SIGTERM, SNAP_ID))
        self.assertEqual(e.summary.get("total_bytes_processed"), 2048)
        self.assertTrue(child.terminated)

    def test_a_stop_after_the_summary_keeps_it_and_the_exit_status_of_a_restic_that_had_exited(self) -> None:
        child = self._Child(SUMMARY_LINE + ERROR_LINE, exited=3)
        e = self._carried(child)
        self.assertEqual((e.rc, e.summary.get("snapshot_id")), (3, SNAP_ID))
        self.assertFalse(child.terminated)

    def test_the_summary_line_being_handled_when_the_stop_came_is_not_lost(self) -> None:
        def stop_on_the_summary(line: str, summary: object, shown: float, out: object) -> object:
            del summary, shown, out
            if "summary" in line:
                raise vm_domain_backup.Stop("stopped by signal SIGTERM")
            return {}, 0.0

        child = self._Child('{"message_type": "status"}\n' + SUMMARY_LINE)
        with mock.patch.object(vm_adapters_restic, "_backup_json_line", stop_on_the_summary):
            e = self._carried(child)
        self.assertEqual((e.rc, e.summary.get("snapshot_id")), (-signal.SIGTERM, SNAP_ID))

    def test_a_stop_before_any_summary_is_the_stop_as_it_came(self) -> None:
        for text in (ERROR_LINE, ERROR_LINE + '{"message_type": "summary"}\n', ERROR_LINE + "not json\n"):
            with self.subTest(text=text):
                child = self._Child(text)
                with self.assertRaises(vm_domain_backup.Stop) as ctx:
                    self._backup(child)
                self.assertIs(type(ctx.exception), vm_domain_backup.Stop)
                self.assertTrue(child.terminated)
                self.assertTrue(child.stdout.closed)


if __name__ == "__main__":
    unittest.main()
