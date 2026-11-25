# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests for btrfs/btrfs-health-check (no root, no btrfs, no disks needed).

The hourly health check (root, btrfs-health-check.timer) reads the state of
the btrfs filesystems, the NVMe disks and the markers of the degraded-boot
part. It alerts through btrfs-notify when something is wrong. These tests run
the whole program with CST_HEALTH_ROOT pointing at a temporary folder (every
file it reads or writes: /proc, /sys, /etc, /run, /var, /usr) and with fake
findmnt, btrfs, lsblk, nvme, journalctl, systemctl, rpm-ostree and
btrfs-notify. The fakes answer from a description of the machine (JSON) and
write down every call.

They pin what the journal, btrfs-notify and the next run see: the exit
status, the journal lines (stdout: <6> OK, <5>, <4>; stderr: one <2> line per
problem), the alert's title and body, the commands run, and the state files
(the reclaim error counts, the journal cursor, the degraded-boot markers).

Not covered here: the member disks of a filesystem. They are found only for
devices that are block devices (lsblk below them, readlink -f for the
device-mapper name), which a test folder cannot fake. MemberDisksTest covers them: the rules
themselves, and one whole run in this process with the block-device test
replaced.

Run with: invowk cmd test module btrfs.tests.test_health_check
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Final, override
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "btrfs-health-check")
# How the tests start the program.
RUN: Final[list[str]] = ["/usr/bin/python3", "-I", "-B", SCRIPT]

UUID = "11111111-2222-3333-4444-555555555555"
BOOT = "0f0f0f0f-aaaa-bbbb-cccc-000000000001"
GIB = 1024**3
TAG = "btrfs-health-check"
TITLE = "Disk health: problems detected"
FAKES = ("findmnt", "btrfs", "lsblk", "nvme", "journalctl", "systemctl", "rpm-ostree")

# One program answers for every fake command, from the machine described in $FAKE_MACHINE.
FAKE = r"""#!/usr/bin/python3 -I
import json, os, sys
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["FAKE_MACHINE"]) as f:
    m = json.load(f)
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps([name] + args) + "\n")

def out(text, rc=0):
    sys.stdout.write(text)
    sys.exit(rc)

def fs(target):
    for x in m.get("mounts", []):
        if target == x["target"] or target in x.get("also", []):
            return x
    sys.stderr.write(name + ": " + target + ": not mounted\n")
    sys.exit(1)

if name == "findmnt":
    o = args[args.index("-o") + 1]
    if "-M" in args:
        x = fs(args[args.index("-M") + 1])
        out((x.get("options", "rw,noatime") if o == "OPTIONS" else x["uuid"]) + "\n")
    if "--target" in args:
        u = m.get("sysroot_uuid")
        out(u + "\n" if u else "")
    lines = []
    for x in m.get("mounts", []):
        for t in [x["target"]] + x.get("also", []):
            lines.append(x["uuid"] + " " + t if o == "UUID,TARGET" else t + " " + x["uuid"])
    out("".join(line + "\n" for line in lines))
if name == "btrfs":
    x = fs(args[-1])
    sub = " ".join(args[:-1])
    if sub == "device stats --check":
        sys.exit(x.get("stats_check", 0))
    key = {"filesystem show": "show", "filesystem usage -b": "usage", "device usage -b": "device_usage",
           "device stats": "stats", "scrub status": "scrub"}[sub]
    out(x.get(key, ""))
if name == "lsblk":
    v = m.get("lsblk", {}).get(os.path.basename(args[-1]))
    out(v, 0) if v is not None else sys.exit(32)
if name == "nvme":
    v = m.get("nvme", {}).get(os.path.basename(args[1]))
    out(v, 0) if v is not None else sys.exit(1)
if name == "journalctl":
    if "-k" in args:
        cur = next((a.split("=", 1)[1] for a in args if a.startswith("--cursor-file=")), None)
        if "-n" in args:
            with open(cur, "w") as f:
                f.write("s=cursor-new\n")
            sys.exit(0)
        if cur is not None and m.get("cursor_rc", 0):
            sys.stderr.write("Failed to seek to cursor\n")
            sys.exit(m["cursor_rc"])
        if cur is not None:
            with open(cur, "w") as f:
                f.write("s=cursor-next\n")
        if m.get("kernel_rc", 0):
            sys.stderr.write(m.get("kernel_err", ""))
        out(m.get("kernel", ""), m.get("kernel_rc", 0))
    out(m.get("suspend", ""))
if name == "systemctl":
    sys.exit(0 if args[-1] in m.get("enabled", []) else 1)
if name == "rpm-ostree":
    if "ostree_raw" in m:
        sys.stderr.write(m.get("ostree_err", ""))
        out(m["ostree_raw"], m.get("ostree_rc", 0))
    v = m.get("ostree")
    out(json.dumps(v), 0) if v is not None else sys.exit(1)
if name == "btrfs-notify":
    sys.exit(m.get("notify_rc", 0))
sys.exit(127)
"""

HEALTHY_SMART = """Smart Log for NVME device:nvme1 namespace-id:ffffffff
critical_warning                        : 0
temperature                             : 38 C
available_spare                         : 100%
available_spare_threshold               : 10%
percentage_used                         : 3%
media_errors                            : 0
"""
FAILING_SMART = (
    HEALTHY_SMART.replace(
        "critical_warning                        : 0", "critical_warning                        : 0x1"
    )
    .replace("media_errors                            : 0", "media_errors                            : 12")
    .replace("available_spare                         : 100%", "available_spare                         : 5%")
)


def ctime(epoch: float) -> str:
    """A time as btrfs scrub status prints it (TZ=UTC)."""
    return time.strftime("%a %b %e %H:%M:%S %Y", time.gmtime(epoch))


def device_usage(*unalloc: int) -> str:
    return "".join(
        f"/dev/mapper/cst-test-{n}, ID: {n}\n   Device size:   2000398934016\n   Data,RAID1:    500000000000\n"
        f"   Unallocated:   {u}\n\n"
        for n, u in enumerate(unalloc, 1)
    )


class HealthCheckTest(unittest.TestCase):
    tmp: str
    root: str
    bin: str
    calls_log: str
    machine_file: str
    now: int
    machine: dict[str, object]
    fs: dict[str, object]

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="health-check-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "root")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        fake = os.path.join(self.bin, "fake")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        for name in FAKES:
            os.symlink(fake, os.path.join(self.bin, name))
        os.makedirs(self.path("usr/local/sbin"))
        os.symlink(fake, self.path("usr/local/sbin/btrfs-notify"))
        self.calls_log = os.path.join(self.tmp, "calls")
        self.machine_file = os.path.join(self.tmp, "machine.json")
        self.now = int(time.time())
        self.fs = {
            "uuid": UUID,
            "target": "/var",
            "also": ["/etc"],
            "options": "rw,noatime,compress=zstd:1",
            "show": (
                f"Label: none  uuid: {UUID}\n\tTotal devices 2 FS bytes used 1.00TiB\n"
                "\tdevid    1 size 1.82TiB used 1.00TiB path /dev/mapper/cst-test-1\n"
                "\tdevid    2 size 1.82TiB used 1.00TiB path /dev/mapper/cst-test-2\n\n"
            ),
            "usage": "Overall:\n    Device size:\t\t   4000797868032\n    Device missing:\t\t\t   0\n"
            "    Multiple profiles:\t\t\t  no\n",
            "device_usage": device_usage(800 * GIB, 800 * GIB),
            "stats": "[/dev/mapper/cst-test-1].write_io_errs    0\n[/dev/mapper/cst-test-1].read_io_errs     0\n",
            "scrub": self.scrub(self.now - 2 * 86400),
        }
        self.machine = {"mounts": [self.fs], "sysroot_uuid": UUID}
        self.write("proc/sys/kernel/random/boot_id", BOOT + "\n")
        self.write("proc/uptime", "100000.25 1000.00\n")
        alloc = f"sys/fs/btrfs/{UUID}/allocation"
        for rel, value in (
            ("metadata/total_bytes", 20 * GIB),
            ("metadata/bytes_used", 10 * GIB),
            ("global_rsv_size", 512 * 1024**2),
            ("data/dynamic_reclaim", 1),
            ("data/periodic_reclaim", 1),
            ("data/reclaim_errors", 0),
            ("metadata/reclaim_errors", 0),
        ):
            self.write(f"{alloc}/{rel}", f"{value}\n")
        self.write(
            "etc/tmpfiles.d/btrfs-automatic-reclaim.conf",
            f"w /sys/fs/btrfs/{UUID}/allocation/data/dynamic_reclaim - - - - 1\n"
            f"w /sys/fs/btrfs/{UUID}/allocation/data/periodic_reclaim - - - - 1\n",
        )

    # ---- the machine ------------------------------------------------------------------------------------------

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def write(self, rel: str, text: str) -> str:
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def read(self, rel: str) -> str:
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    def scrub(self, started: float | None, status: str = "finished", errors: str = "no errors found") -> str:
        head = f"UUID:             {UUID}\n"
        if started is None:
            return head + "\tno stats available\n"
        return (
            head + f"Scrub started:    {ctime(started)}\nStatus:           {status}\nDuration:         1:02:03\n"
            f"Total to scrub:   1.00TiB\nRate:             300.00MiB/s\nError summary:    {errors}\n"
        )

    def run_check(self, *args: str) -> subprocess.CompletedProcess[str]:
        with open(self.machine_file, "w", encoding="utf-8") as f:
            json.dump(self.machine, f)
        if os.path.exists(self.calls_log):
            os.unlink(self.calls_log)
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "CST_HEALTH_ROOT": self.root,
            "FAKE_MACHINE": self.machine_file,
            "FAKE_CALLS": self.calls_log,
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
        return subprocess.run(
            [*RUN, *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, timeout=120
        )

    def calls(self, name: str | None = None) -> list[list[str]]:
        """The fake commands run (each [name, args...]), in order; only those of name when given."""
        with open(self.calls_log, encoding="utf-8") as f:
            calls = [json.loads(line) for line in f]
        return [c for c in calls if name is None or c[0] == name]

    def alerts(self) -> list[list[str]]:
        """btrfs-notify's calls that send an alert (not --prune or --queue)."""
        return [c[1:] for c in self.calls("btrfs-notify") if c[1] == "-u"]

    def ok_line(self, mounts: str = "/var", disks: str = "", swap: str = "") -> str:
        return f"<6>{TAG}: OK ({mounts}; disks: {disks}{'; swap disks: ' + swap if swap else ''})\n"

    def assert_healthy(self, r: subprocess.CompletedProcess[str], stdout: str | None = None) -> None:
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        self.assertEqual(r.stdout, self.ok_line() if stdout is None else stdout)
        self.assertEqual(self.alerts(), [])

    def assert_problems(self, r: subprocess.CompletedProcess[str], *problems: str, stdout: str = "") -> None:
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr, "".join(f"<2>{TAG}: {p}\n" for p in problems))
        self.assertEqual(r.stdout, stdout)
        body = f"{len(problems)} problem(s):\n" + "\n".join(problems)
        self.assertEqual(self.alerts(), [["-u", "critical", "-a", TAG, TITLE, body]])

    # ---- a healthy machine ------------------------------------------------------------------------------------

    def test_healthy(self) -> None:
        r = self.run_check()
        self.assert_healthy(r)
        self.assertEqual(self.calls("btrfs-notify"), [["btrfs-notify", "--prune"]])
        for kind in ("data", "metadata"):
            self.assertEqual(self.read(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-{kind}"), f"{BOOT} 0\n")
        self.assertEqual(self.read("var/lib/btrfs-health-check/kernel.cursor"), "s=cursor-new\n")
        journal = self.calls("journalctl")
        self.assertEqual(journal[0][1:], ["-k", "-q", "--no-pager", "-o", "short-iso", "--since", "-65min"])
        cursor = self.path("var/lib/btrfs-health-check/kernel.cursor")
        self.assertEqual(journal[1][1:], ["-k", "-q", "-n", "1", "-o", "short-iso", f"--cursor-file={cursor}"])
        self.assertEqual(
            [c[1:] for c in self.calls("btrfs")],
            [
                ["filesystem", "show", "/var"],
                ["filesystem", "usage", "-b", "/var"],
                ["device", "usage", "-b", "/var"],
                ["device", "stats", "--check", "/var"],
                ["scrub", "status", "/var"],
            ],
        )
        # The next run reads the kernel log from the cursor on.
        self.assert_healthy(self.run_check())
        self.assertEqual(
            [c[1:] for c in self.calls("journalctl")],
            [["-k", "-q", "--no-pager", "-o", "short-iso", f"--cursor-file={cursor}"]],
        )
        self.assertEqual(self.read("var/lib/btrfs-health-check/kernel.cursor"), "s=cursor-next\n")

    def test_arguments_are_ignored(self) -> None:
        self.assert_healthy(self.run_check("--help"))
        self.assert_healthy(self.run_check("", "x"))

    def test_the_stall_queue_is_delivered_and_old_alerts_pruned(self) -> None:
        os.makedirs(self.path("run/btrfs-stall-monitor"))
        self.assert_healthy(self.run_check())
        self.assertEqual(
            self.calls("btrfs-notify"),
            [["btrfs-notify", "--prune"], ["btrfs-notify", "--queue", self.path("run/btrfs-stall-monitor")]],
        )

    def test_a_failing_prune_changes_nothing(self) -> None:
        self.machine["notify_rc"] = 1
        os.makedirs(self.path("run/btrfs-stall-monitor"))
        self.assert_healthy(self.run_check())
        self.assertEqual(len(self.calls("btrfs-notify")), 2)

    def test_no_btrfs_filesystem(self) -> None:
        self.machine["mounts"] = []
        r = self.run_check()
        self.assert_healthy(r, f"<6>{TAG}: OK (; disks: )\n")

    def test_a_read_only_sysroot_is_fine(self) -> None:
        self.fs.update(target="/sysroot", also=[], options="ro,noatime")
        self.assert_healthy(self.run_check(), self.ok_line("/sysroot"))

    def test_slash_wins_over_bind_mounts(self) -> None:
        self.fs.update(target="/etc", also=["/", "/home"])
        self.assert_healthy(self.run_check(), self.ok_line("/"))

    def test_a_running_scrub_is_fine(self) -> None:
        self.fs["scrub"] = self.scrub(self.now - 40 * 86400, status="running")
        self.assert_healthy(self.run_check())

    # ---- one problem at a time --------------------------------------------------------------------------------

    def test_filesystem_problems(self) -> None:
        crit = (
            "btrfs /var: CRITICAL: only 4096 MiB unallocated on /dev/mapper/cst-test-2: ENOSPC is close. First free "
            "space (old snapshots, caches, images), then 'btrfs balance start -dusage=0 /var', then -dusage=5, 15, 30; "
            "never a metadata or full balance. If a balance hangs the PC or the boot: boot once with ,skip_balance "
            "added to the rootflags= of the kernel command line, then 'btrfs balance cancel /var' (cst btrfs README, "
            "section 13)"
        )
        cases: list[tuple[str, dict[str, object], list[str]]] = [
            (
                "missing member",
                {"show": "Label: none\n\t*** Some devices missing\n"},
                ["btrfs /var: member device MISSING (running degraded)"],
            ),
            (
                "read-only",
                {"options": "ro,noatime"},
                ["btrfs /var: mounted READ-ONLY (forced after an error?) - check 'journalctl -k -g BTRFS'"],
            ),
            (
                "bytes on a missing device",
                {"usage": "Overall:\n    Device missing:\t\t   1073741824\n"},
                ["btrfs /var: 1073741824 bytes on missing device"],
            ),
            (
                "mixed profiles",
                {"usage": "Overall:\n    Device missing:\t\t   0\n    Multiple profiles:\t\t  yes\n"},
                [
                    (
                        "btrfs /var: mixed chunk profiles (degraded writes?) - run balance "
                        "-dconvert=raid1,soft -mconvert=raid1,soft"
                    )
                ],
            ),
            ("critical unallocated", {"device_usage": device_usage(800 * GIB, 4 * GIB)}, [crit]),
            (
                "low unallocated",
                {"device_usage": device_usage(20 * GIB + 5, 800 * GIB)},
                [
                    (
                        "btrfs /var: only 20 GiB unallocated on /dev/mapper/cst-test-1 (warning below 50 GiB; metadata "
                        "grows only from unallocated space). Free space, or compact half-empty data chunks: "
                        "'btrfs balance start -dusage=20 /var'"
                    )
                ],
            ),
            (
                "a missing device's line is skipped",
                {"device_usage": device_usage(800 * GIB).replace("/dev/mapper/cst-test-1", "missing")},
                [],
            ),
            (
                "device error counters",
                {
                    "stats_check": 64,
                    "stats": "[/dev/mapper/cst-test-1].write_io_errs    0\n[/dev/mapper/cst-test-1].read_io_errs     3\n"
                    "\n",
                },
                ["btrfs /var: non-zero device error counters: [/dev/mapper/cst-test-1].read_io_errs     3  "],
            ),
            (
                "scrub errors",
                {"scrub": self.scrub(self.now - 86400, errors="csum=3 verify=1\n  Corrected: 4")},
                ["btrfs /var: last scrub reported errors: Error summary:    csum=3 verify=1"],
            ),
            ("never scrubbed", {"scrub": self.scrub(None)}, ["btrfs /var: no scrub has ever completed"]),
            (
                "scrub aborted",
                {"scrub": self.scrub(self.now - 86400, status="aborted")},
                [f"btrfs /var: last scrub (started {ctime(self.now - 86400)}) did not finish: status aborted"],
            ),
            (
                "scrub with no status",
                {"scrub": self.scrub(self.now - 86400).replace("Status:           finished\n", "")},
                [f"btrfs /var: last scrub (started {ctime(self.now - 86400)}) did not finish: status unknown"],
            ),
            (
                "old scrub",
                {"scrub": self.scrub(self.now - 12 * 86400 - 60)},
                ["btrfs /var: last scrub is 12 days old"],
            ),
            ("scrub of 10 days", {"scrub": self.scrub(self.now - 10 * 86400 - 60)}, []),
            (
                "unparsable scrub time",
                {"scrub": self.scrub(self.now).replace(ctime(self.now), "sometime")},
                [],
            ),
        ]
        base = dict(self.fs)
        for what, change, problems in cases:
            with self.subTest(what):
                self.fs.clear()
                self.fs.update(base, **change)
                r = self.run_check()
                if problems:
                    self.assert_problems(r, *problems)
                else:
                    self.assert_healthy(r)

    def test_metadata_almost_full(self) -> None:
        alloc = f"sys/fs/btrfs/{UUID}/allocation"
        self.write(f"{alloc}/metadata/bytes_used", f"{20 * GIB - 600 * 1024**2}\n")
        self.fs["device_usage"] = device_usage(GIB, 800 * GIB)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            r.stderr.splitlines()[1],
            f"<2>{TAG}: btrfs /var: CRITICAL: metadata almost full (600 MiB free in metadata chunks, 1024 MiB "
            "unallocated): writes may fail with ENOSPC soon",
        )

    def test_reclaim(self) -> None:
        alloc = f"sys/fs/btrfs/{UUID}/allocation"
        conf = self.path("etc/tmpfiles.d/btrfs-automatic-reclaim.conf")
        self.write(f"{alloc}/data/periodic_reclaim", "0\n")
        self.assert_problems(
            self.run_check(),
            f"btrfs /var: automatic reclaim is configured but data/periodic_reclaim is off - run "
            f"'systemd-tmpfiles --create {conf}'",
        )
        self.write(f"{alloc}/data/periodic_reclaim", "1\n")
        # Errors since the last check, on this boot: only the new ones count.
        self.write(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-data", f"{BOOT} 2\n")
        self.write(f"{alloc}/data/reclaim_errors", "5\n")
        self.assert_problems(
            self.run_check(),
            "btrfs /var: automatic data reclaim failed 3 time(s) since the last check (reclaim_errors=5) - see "
            "'journalctl -k -g BTRFS'",
        )
        self.assertEqual(self.read(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-data"), f"{BOOT} 5\n")
        self.assert_healthy(self.run_check())
        # Another boot (the count restarts at mount) or a damaged file: compared against 0.
        for seen in ("another-boot 9\n", "garbage\n", ""):
            with self.subTest(seen=seen):
                self.write(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-metadata", seen)
                self.write(f"{alloc}/metadata/reclaim_errors", "1\n")
                self.assert_problems(
                    self.run_check(),
                    "btrfs /var: automatic metadata reclaim failed 1 time(s) since the last check (reclaim_errors=1) "
                    "- see 'journalctl -k -g BTRFS'",
                )
        # Not a number in sysfs: skipped, the state file left as it was.
        self.write(f"{alloc}/metadata/reclaim_errors", "n/a\n")
        self.write(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-metadata", "keep 1\n")
        self.assert_healthy(self.run_check())
        self.assertEqual(self.read(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-metadata"), "keep 1\n")

    def test_reclaim_errors_missing_or_beyond_bash_arithmetic(self) -> None:
        alloc = f"sys/fs/btrfs/{UUID}/allocation"
        state = f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-metadata"
        # No reclaim_errors file: counted as 0, and the state file says so.
        os.unlink(self.path(f"{alloc}/metadata/reclaim_errors"))
        self.assert_healthy(self.run_check())
        self.assertEqual(self.read(state), f"{BOOT} 0\n")
        # 08 is digits, but not a number to bash's arithmetic (octal): no comparison, the count kept.
        self.write(state, f"{BOOT} 1\n")
        self.write(f"{alloc}/metadata/reclaim_errors", "08\n")
        self.assert_healthy(self.run_check())
        self.assertEqual(self.read(state), f"{BOOT} 08\n")

    def test_reclaim_counts_wrap_as_bash_arithmetic(self) -> None:
        # bash's (( errs > seen )) and $(( errs - seen )) are 64-bit and wrap around: a state file at
        # 18446744073709551615 is -1, so 5 errors are 6 new ones; 5000 digits are a number (wrapped), not
        # a ValueError out of the check.
        alloc = f"sys/fs/btrfs/{UUID}/allocation"
        state = f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-data"
        self.write(state, f"{BOOT} 18446744073709551615\n")
        self.write(f"{alloc}/data/reclaim_errors", "5\n")
        self.assert_problems(
            self.run_check(),
            "btrfs /var: automatic data reclaim failed 6 time(s) since the last check (reclaim_errors=5) - see "
            "'journalctl -k -g BTRFS'",
        )
        self.assertEqual(self.read(state), f"{BOOT} 5\n")
        huge = "1" * 5000  # 8198552921648689607 in bash
        self.write(state, f"{BOOT} 3\n")
        self.write(f"{alloc}/data/reclaim_errors", huge + "\n")
        self.assert_problems(
            self.run_check(),
            f"btrfs /var: automatic data reclaim failed 8198552921648689604 time(s) since the last check "
            f"(reclaim_errors={huge}) - see 'journalctl -k -g BTRFS'",
        )
        self.assertEqual(self.read(state), f"{BOOT} {huge}\n")

    def test_reclaim_not_configured_is_not_checked(self) -> None:
        os.unlink(self.path("etc/tmpfiles.d/btrfs-automatic-reclaim.conf"))
        self.write(f"sys/fs/btrfs/{UUID}/allocation/data/dynamic_reclaim", "0\n")
        self.assert_healthy(self.run_check())

    def test_without_sysfs_no_metadata_or_reclaim_check(self) -> None:
        shutil.rmtree(self.path("sys"))
        self.assert_healthy(self.run_check())
        self.assertFalse(os.path.exists(self.path(f"var/lib/btrfs-health-check/reclaim-errors-{UUID}-data")))

    # ---- the degraded-boot part ---------------------------------------------------------------------------------

    def last_resort(self, rule_uuid: str) -> str:
        return self.write(
            "etc/udev/rules.d/65-btrfs-last-resort.rules",
            f'# managed\nENV{{ID_FS_UUID}}!="{rule_uuid}", GOTO="end"\n',
        )

    def upstream_rule(self, rel: str = "usr/lib/udev/rules.d/64-btrfs.rules") -> str:
        return self.write(rel, 'ENV{ID_BTRFS_READY}=="0", ENV{SYSTEMD_READY}="0"\n')

    @staticmethod
    def next_unknown(reason: str) -> str:
        """The journal warning of a check that could not tell the next deployment's root."""
        return f"<4>{TAG}: udev: next deployment unknown ({reason}); its 64-btrfs.rules was not checked\n"

    def test_last_resort_rule(self) -> None:
        rule = self.last_resort("aaaaaaaa-0000")
        self.upstream_rule()
        # This machine has no rpm-ostree answer: the next deployment is unknown (a journal warning).
        unknown = self.next_unknown("rpm-ostree status --json failed: exit 1")
        self.assert_problems(
            self.run_check(),
            f"btrfs-last-resort: {rule} is for aaaaaaaa-0000 but the root fs is {UUID}; re-run 'cst btrfs "
            "degraded-boot'",
            stdout=unknown,
        )
        self.last_resort("")
        self.assert_problems(
            self.run_check(),
            f"btrfs-last-resort: {rule} is for ? but the root fs is {UUID}; re-run 'cst btrfs degraded-boot'",
            stdout=unknown,
        )
        self.last_resort("aaaaaaaa-0000")
        self.machine["sysroot_uuid"] = ""
        self.assert_problems(
            self.run_check(),
            f"btrfs-last-resort: {rule} is for aaaaaaaa-0000 but the root fs is ?; re-run 'cst btrfs degraded-boot'",
            stdout=unknown,
        )

    def test_upstream_rule_of_this_and_the_next_boot(self) -> None:
        self.last_resort(UUID)
        self.upstream_rule()
        self.assert_healthy(
            self.run_check(), self.next_unknown("rpm-ostree status --json failed: exit 1") + self.ok_line()
        )
        self.machine["ostree"] = {
            "deployments": [{"osname": "fedora", "checksum": "abc123", "serial": 1}, {"osname": "fedora"}]
        }
        nxt = self.path("ostree/deploy/fedora/deploy/abc123.1/usr/lib/udev/rules.d/64-btrfs.rules")
        self.assert_problems(
            self.run_check(), f"udev: {nxt} changed semantics; review btrfs/btrfs-degraded-boot-setup.sh (CST)"
        )
        self.upstream_rule("ostree/deploy/fedora/deploy/abc123.1/usr/lib/udev/rules.d/64-btrfs.rules")
        self.assert_healthy(self.run_check())
        os.unlink(self.path("usr/lib/udev/rules.d/64-btrfs.rules"))
        here = self.path("usr/lib/udev/rules.d/64-btrfs.rules")
        self.assert_problems(
            self.run_check(), f"udev: {here} changed semantics; review btrfs/btrfs-degraded-boot-setup.sh (CST)"
        )

    def test_an_unknown_next_deployment_is_a_journal_warning_and_the_booted_rule_is_still_checked(self) -> None:
        # Before, next_root dropped the reason and nothing said the next boot's rule went unchecked.
        self.last_resort(UUID)
        self.upstream_rule()
        here = self.path("usr/lib/udev/rules.d/64-btrfs.rules")
        changed = f"udev: {here} changed semantics; review btrfs/btrfs-degraded-boot-setup.sh (CST)"
        cases: list[tuple[str, dict[str, object], str]] = [
            ("rpm-ostree fails", {"ostree_raw": "", "ostree_rc": 1, "ostree_err": "error: x\n"}, "failed: error: x"),
            ("not JSON", {"ostree_raw": "{"}, "unreadable output ("),
            ("no deployment", {"ostree": {"deployments": []}}, "rpm-ostree lists no deployment"),
            (
                "no serial",
                {"ostree": {"deployments": [{"osname": "fedora", "checksum": "abc123"}]}},
                "its first deployment has no osname, checksum or whole serial",
            ),
            (
                "serial inf",
                {"ostree_raw": '{"deployments": [{"osname": "fedora", "checksum": "abc123", "serial": 1e999}]}'},
                "its first deployment has no osname, checksum or whole serial",
            ),
        ]
        for name, answer, reason in cases:
            with self.subTest(name):
                for key in ("ostree", "ostree_raw", "ostree_rc", "ostree_err"):
                    self.machine.pop(key, None)
                self.machine.update(answer)
                r = self.run_check()
                self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
                lines = r.stdout.splitlines(keepends=True)
                self.assertEqual(len(lines), 2, r.stdout)
                self.assertTrue(lines[0].startswith(f"<4>{TAG}: udev: next deployment unknown ("), lines[0])
                self.assertIn(reason, lines[0])
                self.assertTrue(lines[0].endswith("); its 64-btrfs.rules was not checked\n"), lines[0])
                self.assertEqual(lines[1], self.ok_line())
                self.assertEqual(self.alerts(), [])
                # The booted system's rule is still checked.
                os.rename(here, here + ".kept")
                r = self.run_check()
                os.rename(here + ".kept", here)
                self.assert_problems(r, changed, stdout=lines[0])

    def test_no_warning_without_degraded_boot(self) -> None:
        # rpm-ostree is not even asked when the last-resort rule is not installed.
        self.assert_healthy(self.run_check())
        self.assertEqual(self.calls("rpm-ostree"), [])

    def test_degraded_boot_flag(self) -> None:
        self.write(f"run/btrfs-allow-degraded/{UUID}", "")
        self.assert_problems(
            self.run_check(),
            f"btrfs {UUID}: this boot is DEGRADED (btrfs-last-resort: member disk missing at boot)",
        )

    def marker_problem(self, uuid: str, since_h: str, state: str) -> str:
        return (
            f"btrfs {uuid}: booted DEGRADED on {since_h} ({state}); restore RAID1 (replace the disk if needed), then "
            "'btrfs balance start -dconvert=raid1,soft -mconvert=raid1,soft /var' and 'btrfs scrub start /var'. "
            "Clears after a clean scrub started once all devices are back."
        )

    def test_degraded_marker_of_a_filesystem_not_mounted(self) -> None:
        self.write("var/lib/btrfs-degraded-boot/degraded-9999", "since=1700000000\nsince_human=2023-11-14 22:13\n")
        self.assert_problems(self.run_check(), self.marker_problem("9999", "2023-11-14 22:13", "not mounted"))
        self.write("var/lib/btrfs-degraded-boot/degraded-9999", "")
        self.assert_problems(self.run_check(), self.marker_problem("9999", "?", "not mounted"))

    def test_degraded_marker_while_a_device_is_still_missing(self) -> None:
        marker = self.write(
            f"var/lib/btrfs-degraded-boot/degraded-{UUID}",
            "since=1700000000\nsince_human=2023-11-14 22:13\ncomplete_since=1700000100\n",
        )
        self.fs["show"] = "Label: none\n\t*** Some devices missing\n"
        self.assert_problems(
            self.run_check(),
            "btrfs /var: member device MISSING (running degraded)",
            self.marker_problem(UUID, "2023-11-14 22:13", "still missing a device"),
        )
        with open(marker, encoding="utf-8") as f:
            self.assertEqual(f.read(), "since=1700000000\nsince_human=2023-11-14 22:13\n")

    def test_degraded_marker_waits_for_a_clean_scrub_then_clears(self) -> None:
        since = self.now - 5 * 86400
        marker = self.write(
            f"var/lib/btrfs-degraded-boot/degraded-{UUID}", f"since={since}\nsince_human=2026-01-01 10:00\n"
        )
        self.fs["scrub"] = self.scrub(since - 3600)
        r = self.run_check()
        with open(marker, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[:2], [f"since={since}", "since_human=2026-01-01 10:00"])
        self.assertRegex(lines[2], r"^complete_since=[0-9]+$")
        complete = int(lines[2].split("=")[1])
        self.assertLessEqual(abs(complete - self.now), 120)
        at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(complete))
        state = f"all devices present since {at}, waiting for a clean scrub started after that"
        self.assert_problems(r, self.marker_problem(UUID, "2026-01-01 10:00", state))
        # A scrub that started after the fs was complete again, finished clean: the marker goes.
        self.fs["scrub"] = self.scrub(complete + 60)
        r = self.run_check()
        self.assertFalse(os.path.exists(marker))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(
            r.stdout,
            f"<5>{TAG}: btrfs {UUID}: RAID1 restored (clean scrub started {ctime(complete + 60)} after the "
            f"degraded boot), cleared {marker}\n" + self.ok_line(),
        )

    def test_degraded_marker_keeps_waiting_on_mixed_profiles_or_scrub_errors(self) -> None:
        since = self.now - 5 * 86400
        marker = self.write(
            f"var/lib/btrfs-degraded-boot/degraded-{UUID}",
            f"since={since}\nsince_human=x\ncomplete_since={since + 10}\n",
        )
        at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since + 10))
        state = f"all devices present since {at}, waiting for a clean scrub started after that"
        self.fs["scrub"] = self.scrub(since + 60, errors="csum=1")
        self.assert_problems(
            self.run_check(),
            "btrfs /var: last scrub reported errors: Error summary:    csum=1",
            self.marker_problem(UUID, "x", state),
        )
        self.assertTrue(os.path.exists(marker))

    def test_degraded_marker_with_a_time_date_cannot_show(self) -> None:
        # A damaged complete_since= that date(1) cannot show: the time is left out (date printed nothing
        # and said why on stderr), the marker stays as it is, and the checks after it still run.
        since = self.now - 5 * 86400
        text = f"since={since}\nsince_human=x\ncomplete_since=99999999999999999999\n"
        marker = self.write(f"var/lib/btrfs-degraded-boot/degraded-{UUID}", text)
        self.write("proc/mdstat", "md0 : active raid1 sda1[0]\n      1000 blocks [2/1] [U_]\n")
        r = self.run_check()
        state = "all devices present since , waiting for a clean scrub started after that"
        problems = [
            self.marker_problem(UUID, "x", state),
            "md RAID degraded: md0 : active raid1 sda1[0] 1000 blocks [2/1] [U_] ",
        ]
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.alerts(), [["-u", "critical", "-a", TAG, TITLE, "2 problem(s):\n" + "\n".join(problems)]])
        self.assertTrue(r.stderr.endswith("".join(f"<2>{TAG}: {p}\n" for p in problems)), r.stderr)
        self.assertIn("date: ", r.stderr)
        with open(marker, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_nul_bytes_in_a_marker_are_dropped(self) -> None:
        # bash read the marker's values through $(...), which drops NUL bytes: the problem names
        # since_human without them (a NUL cannot be passed to btrfs-notify at all).
        self.write("var/lib/btrfs-degraded-boot/degraded-9999", "since=1700000000\nsince_human=2023-11-14\0 22:13\n")
        problem = self.marker_problem("9999", "2023-11-14 22:13", "not mounted")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.alerts(), [["-u", "critical", "-a", TAG, TITLE, f"1 problem(s):\n{problem}"]])
        self.assertTrue(r.stderr.endswith(f"<2>{TAG}: {problem}\n"), r.stderr)

    # ---- the kernel log -----------------------------------------------------------------------------------------

    def test_kernel_messages(self) -> None:
        kernel = (
            "2026-11-17T10:00:00+0000 host kernel: BTRFS error (device dm-9): csum failed root 5\n"
            "2026-11-17T10:00:01+0000 host kernel: I/O error, dev sdz, sector 1 op 0x0\n"
            "2026-11-17T10:00:02+0000 host kernel: BTRFS critical: corrupt leaf\n"
            "2026-11-17T10:00:03+0000 host kernel: usb 1-1: new device\n"
            "2026-11-17T10:00:04+0000 host kernel: INFO: task x:1 blocked for more than 120 seconds.\n"
            "2026-11-17T10:00:05+0000 host kernel: btrfs: Transaction aborted (error -28)\n"
            "2026-11-17T10:00:06+0000 host kernel: BTRFS: forced readonly\n"
        )
        self.machine["kernel"] = kernel
        lines = kernel.splitlines()
        self.assert_problems(
            self.run_check(),
            f"kernel: 4 btrfs/IO error message(s) since last check, e.g.: {lines[2]} | {lines[4]} | {lines[5]} | ",
        )

    def test_a_kernel_log_that_cannot_be_read(self) -> None:
        self.machine.update(kernel_rc=1, kernel_err="No journal files were found.\n")
        self.assert_problems(self.run_check(), "kernel log scan failed: No journal files were found.")

    def test_a_bad_cursor_starts_over(self) -> None:
        self.write("var/lib/btrfs-health-check/kernel.cursor", "s=old\n")
        self.machine["cursor_rc"] = 1
        self.assert_healthy(self.run_check())
        self.assertEqual(
            [c[1:] for c in self.calls("journalctl")][1:],
            [
                ["-k", "-q", "--no-pager", "-o", "short-iso", "--since", "-65min"],
                [
                    "-k",
                    "-q",
                    "-n",
                    "1",
                    "-o",
                    "short-iso",
                    "--cursor-file=" + self.path("var/lib/btrfs-health-check/kernel.cursor"),
                ],
            ],
        )
        self.assertEqual(self.read("var/lib/btrfs-health-check/kernel.cursor"), "s=cursor-new\n")

    def unwritable(self, rel: str) -> None:
        """The folder at rel made read-only (as /var forced read-only after a btrfs error), for this test."""
        os.chmod(self.path(rel), 0o555)
        self.addCleanup(os.chmod, self.path(rel), 0o755)

    def test_a_cursor_it_cannot_remove_still_alerts(self) -> None:
        # /var forced read-only: journalctl cannot write its new cursor (exit 1), the old one cannot be
        # removed, and the run must still alert (as bash's rm -f, which only said so).
        cursor = self.write("var/lib/btrfs-health-check/kernel.cursor", "s=old\n")
        self.machine["cursor_rc"] = 1
        self.fs["options"] = "ro,noatime"
        self.unwritable("var/lib/btrfs-health-check")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual([a[:4] for a in self.alerts()], [["-u", "critical", "-a", TAG]])
        self.assertIn("mounted READ-ONLY", self.alerts()[0][-1])
        if os.geteuid() != 0:  # root removes it anyway
            self.assertIn(f"{TAG}: cannot remove {cursor}: Permission denied\n", r.stderr)

    def test_a_marker_it_cannot_remove_still_alerts_the_rest(self) -> None:
        since = self.now - 5 * 86400
        marker = self.write(
            f"var/lib/btrfs-degraded-boot/degraded-{UUID}",
            f"since={since}\nsince_human=x\ncomplete_since={since + 60}\n",
        )
        self.fs["scrub"] = self.scrub(since + 3600)
        self.fs["options"] = "ro,noatime"
        self.unwritable("var/lib/btrfs-degraded-boot")
        r = self.run_check()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(len(self.alerts()), 1, r.stderr)
        self.assertIn("mounted READ-ONLY", self.alerts()[0][-1])
        if os.geteuid() != 0:
            self.assertIn(f"{TAG}: cannot remove {marker}: Permission denied\n", r.stderr)

    # ---- snapshots and the NAS backup ---------------------------------------------------------------------------

    def snapshots(self, last_ok: int) -> None:
        self.machine["enabled"] = ["btrfs-snapshot.timer"]
        self.write("var/lib/btrfs-snapshot/last-ok", f"{last_ok}\n")

    def test_snapshots_that_stopped(self) -> None:
        stale = "snapshots: no successful hourly run for over 3 hours - check 'journalctl -u btrfs-snapshot'"
        self.snapshots(self.now - 4 * 3600)
        self.assert_problems(self.run_check(), stale)
        self.assertEqual(self.calls("systemctl")[0][1:], ["is-enabled", "-q", "btrfs-snapshot.timer"])
        # Paused, trees still being dropped, a conversion or a move waiting for the next boot: said elsewhere.
        for rel, text in (
            ("var/lib/btrfs-snapshot/paused", ""),
            ("var/lib/btrfs-snapshot/drop-pending", "x\n"),
            ("etc/btrfs-setup/convert-at-boot", ""),
            ("etc/btrfs-setup/bulk-at-boot", ""),
        ):
            with self.subTest(rel):
                self.write(rel, text)
                self.assert_healthy(self.run_check())
                os.unlink(self.path(rel))
        self.write("var/lib/btrfs-snapshot/drop-pending", "")
        self.assert_problems(self.run_check(), stale)

    def test_snapshots_get_time_after_boot_or_resume(self) -> None:
        self.snapshots(self.now - 4 * 3600)
        self.write("proc/uptime", "600.00 10.00\n")
        self.assert_healthy(self.run_check())
        self.write("proc/uptime", "100000.00 10.00\n")
        self.machine["suspend"] = f"{self.now - 300}.123456 host systemd[1]: Finished System Suspend.\n"
        self.assert_healthy(self.run_check())
        self.assertEqual(
            self.calls("journalctl")[-1][1:],
            [
                "-q",
                "-o",
                "short-unix",
                "--no-pager",
                "-u",
                "systemd-suspend.service",
                "-u",
                "systemd-hibernate.service",
                "-u",
                "systemd-suspend-then-hibernate.service",
                "-n",
                "1",
            ],
        )
        self.machine["suspend"] = f"{self.now - 3600}.5 host systemd[1]: Finished System Suspend.\n"
        self.assertEqual(self.run_check().returncode, 1)

    def test_snapshots_recent_or_unknown_are_fine(self) -> None:
        self.snapshots(self.now - 3600)
        self.assert_healthy(self.run_check())
        self.write("var/lib/btrfs-snapshot/last-ok", "never\n")
        self.assert_healthy(self.run_check())
        self.machine["enabled"] = []
        self.write("var/lib/btrfs-snapshot/last-ok", f"{self.now - 9 * 3600}\n")
        self.assert_healthy(self.run_check())

    def test_nas_backup_too_old_goes_to_the_journal_only(self) -> None:
        self.snapshots(self.now - 600)
        self.write("etc/btrfs-setup/snapshots.conf", "NAS_ENABLED=1\nNAS_PATH='/srv/x'\n")
        self.write("var/lib/btrfs-snapshot/nas-last-ok", f"{self.now - 9 * 86400}\n")
        nas = f"<4>{TAG}: no successful NAS backup (restic) for over 8 days (journalctl -u btrfs-snapshot-nas)\n"
        self.assert_healthy(self.run_check(), nas + self.ok_line())
        # Turned on later than the last success: counted from then.
        self.write("var/lib/btrfs-snapshot/nas-enabled-at", f"{self.now - 86400}\n")
        self.assert_healthy(self.run_check())
        self.write("etc/btrfs-setup/snapshots.conf", "NAS_ENABLED=0\n")
        self.write("var/lib/btrfs-snapshot/nas-enabled-at", "0\n")
        self.assert_healthy(self.run_check())

    def test_nas_backup_of_unknown_age_logs_nothing(self) -> None:
        # Turned on, but neither a success nor the time it was turned on is known: no age to judge.
        self.snapshots(self.now - 600)
        self.write("etc/btrfs-setup/snapshots.conf", "NAS_ENABLED=1\nNAS_PATH='/srv/x'\n")
        self.assert_healthy(self.run_check())

    # ---- md RAID and the NVMe health log ------------------------------------------------------------------------

    def test_md_raid_degraded(self) -> None:
        self.write(
            "proc/mdstat",
            "Personalities : [raid1]\nmd0 : active raid1 sda1[0]\n      1000 blocks super 1.2 [2/1] [U_]\n\n"
            "md1 : active raid1 sdb1[0] sdc1[1]\n      1000 blocks [2/2] [UU]\n\n"
            "md2 : active raid1 sdd1[0]\n      1000 blocks [2/1] [_U]\nunused devices: <none>\n",
        )
        self.assert_problems(
            self.run_check(),
            "md RAID degraded: md0 : active raid1 sda1[0] 1000 blocks super 1.2 [2/1] [U_] -- md2 : active raid1 "
            "sdd1[0] 1000 blocks [2/1] [_U] ",
        )

    def swaps(self, *lines: str) -> None:
        self.write("proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n" + "".join(line + "\n" for line in lines))

    def test_swap_disks_are_checked_and_named(self) -> None:
        self.machine["lsblk"] = {
            "dm-2": "dm-2 crypt\nnvme1n1p2 part\nnvme1n1 disk\n",
            "dm-3": "dm-3 crypt\nnvme1n1p3 part\nnvme1n1 disk\n",
            "zram0": "zram0 disk\n",
        }
        self.machine["nvme"] = {"nvme1": HEALTHY_SMART}
        self.swaps(
            "/dev/dm-2                               partition\t134217724\t0\t-2",
            "/dev/dm-3 partition 1 0 -3",
            "/var/swap/swapfile file 1 0 -4",
            "/dev/zram0 partition 1 0 100",
        )
        self.assert_healthy(self.run_check(), self.ok_line(swap="nvme1n1"))
        self.assertEqual([c[1:] for c in self.calls("nvme")], [["smart-log", "/dev/nvme1"]])
        self.machine["nvme"] = {"nvme1": FAILING_SMART}
        self.assert_problems(
            self.run_check(),
            "NVMe nvme1n1 (swap): critical_warning=1",
            "NVMe nvme1n1 (swap): media_errors=12",
            "NVMe nvme1n1 (swap): available_spare 5% below threshold 10%",
        )

    def test_smart_values(self) -> None:
        self.machine["lsblk"] = {"dm-2": "dm-2 crypt\nnvme1n1p2 part\nnvme1n1 disk\n"}
        self.swaps("/dev/dm-2 partition 1 0 -2")
        field = "critical_warning                        : 0"
        for raw, problem in (
            ("0x8", "critical_warning=8"),
            ("0xa", "critical_warning=10"),
            ("4", "critical_warning=4"),
            ("08", "critical_warning=8"),
            ("0", None),
            ("bad", "cannot read critical_warning (bad)"),
        ):
            with self.subTest(raw):
                self.machine["nvme"] = {"nvme1": HEALTHY_SMART.replace(field, f"critical_warning : {raw}")}
                r = self.run_check()
                if problem is None:
                    self.assert_healthy(r, self.ok_line(swap="nvme1n1"))
                else:
                    self.assert_problems(r, f"NVMe nvme1n1 (swap): {problem}")
        wear = HEALTHY_SMART.replace("percentage_used                         : 3%", "percentage_used : 80%")
        self.machine["nvme"] = {"nvme1": wear}
        self.assert_problems(self.run_check(), "NVMe nvme1n1 (swap): wear percentage_used=80%")
        self.machine["nvme"] = {}
        self.assert_healthy(self.run_check(), self.ok_line(swap="nvme1n1"))

    # ---- several problems, and btrfs-notify ----------------------------------------------------------------------

    def test_several_problems_in_one_alert(self) -> None:
        self.fs["scrub"] = self.scrub(None)
        self.write(f"run/btrfs-allow-degraded/{UUID}", "")
        self.write("proc/mdstat", "md0 : active raid1 sda1[0]\n      1 blocks [2/1] [U_]\n")
        self.assert_problems(
            self.run_check(),
            "btrfs /var: no scrub has ever completed",
            f"btrfs {UUID}: this boot is DEGRADED (btrfs-last-resort: member disk missing at boot)",
            "md RAID degraded: md0 : active raid1 sda1[0] 1 blocks [2/1] [U_] ",
        )

    def test_without_btrfs_notify_the_problems_reach_the_journal(self) -> None:
        os.unlink(self.path("usr/local/sbin/btrfs-notify"))
        self.write(f"run/btrfs-allow-degraded/{UUID}", "")
        r = self.run_check()
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn(
            f"<2>{TAG}: btrfs {UUID}: this boot is DEGRADED (btrfs-last-resort: member disk missing at boot)\n",
            r.stderr,
        )
        self.assertEqual(self.calls("btrfs-notify"), [])

    def test_a_failing_btrfs_notify_keeps_exit_1(self) -> None:
        self.machine["notify_rc"] = 3
        self.write(f"run/btrfs-allow-degraded/{UUID}", "")
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.alerts()), 1)


# Every bash run of the differential tests below: a clean environment and no stdin (AGENTS.md, "Test
# gotchas"); C.UTF-8, built into glibc, so [[ =~ ^[0-9]+$ ]] takes ASCII digits only, as the Python does.
BASH_ENV: Final = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


def run_bash(script: str, *args: str) -> list[str]:
    """The stdout lines of bash -c SCRIPT _ ARGS... in BASH_ENV."""
    r = subprocess.run(
        ["/usr/bin/bash", "-c", script, "_", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
        env=BASH_ENV,
        timeout=60,
    )
    return r.stdout.splitlines()


def nul_file(folder: str, items: list[str]) -> str:
    """A file of FOLDER holding ITEMS, each ended by a NUL byte (for bash's read -r -d '')."""
    path = os.path.join(folder, "values")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(item + "\0" for item in items))
    return path


class BashArithmeticTest(unittest.TestCase):
    """bash_int and the reclaim decision against the real bash, case by case (bash's own result)."""

    # Each value in a subshell, so an arithmetic error ends only its own case.
    BASH_INT = """while IFS= read -r -d '' v; do ( echo "$(( $v ))" ) 2>/dev/null || echo error; done < "$1"\n"""
    DIGIT_VALUES: Final = (
        "0",
        "7",
        "1234567890123456789",
        "12345678901234567890",
        "9223372036854775807",
        "9223372036854775808",
        "18446744073709551615",
        "18446744073709551616",
        "18446744073709551617",
        "99999999999999999999",
        "1" * 5000,
        "9" * 4301,
        "0" + "7" * 30,
        "0" * 5000 + "1",
        "0777",
        "00",
        "08",
        "0" + "1" * 5000 + "8",
        "\N{ARABIC-INDIC DIGIT THREE}",
        "\N{SUPERSCRIPT TWO}",
        "\N{FULLWIDTH DIGIT ONE}",
        "1\N{ARABIC-INDIC DIGIT THREE}",
    )

    def test_bash_int_reads_digits_as_bash_arithmetic(self) -> None:
        from btrfs.health_check.domain import bash_int

        with tempfile.TemporaryDirectory() as tmp:
            got = run_bash(self.BASH_INT, nul_file(tmp, list(self.DIGIT_VALUES)))
        self.assertEqual(len(got), len(self.DIGIT_VALUES))
        for value, bash in zip(self.DIGIT_VALUES, got, strict=True):
            with self.subTest(value=value[:24], digits=len(value)):
                n = bash_int(value)
                self.assertEqual("error" if n is None else str(n), bash)

    # The old btrfs-health-check's reclaim count (git show b819032f^:btrfs/btrfs-health-check, lines
    # 169-179), its state file's read replaced by the value: skip, no problem, or the problem's count.
    RECLAIM = """while IFS= read -r -d '' errs && IFS= read -r -d '' seen; do
    (
        [[ "${errs}" =~ ^[0-9]+$ ]] || { echo skip; exit 0; }
        [[ "${seen}" =~ ^[0-9]+$ ]] || seen=0
        if (( errs > seen )); then echo "problem $(( errs - seen ))"; else echo none; fi
    ) 2>/dev/null || echo error
done < "$1"
"""

    def test_reclaim_decision_matches_the_old_bash(self) -> None:
        from btrfs.health_check import adapters
        from cst_core.adapters.runner import SubprocessCommandRunner

        three = "\N{ARABIC-INDIC DIGIT THREE}"
        errs_values = ("5", "08", "18446744073709551617", "9223372036854775807", "1" * 5000, three, "0777")
        seen_values = ("0", "3", "08", "18446744073709551615", "1" * 5000, three, "99999999999999999999")
        pairs = [(e, s) for e in errs_values for s in seen_values]
        with tempfile.TemporaryDirectory() as tmp:
            bash = run_bash(self.RECLAIM, nul_file(tmp, [x for pair in pairs for x in pair]))
            root = os.path.join(tmp, "root")
            alloc = f"{root}/sys/fs/btrfs/{UUID}/allocation"
            os.makedirs(f"{alloc}/data")
            os.makedirs(f"{root}/proc/sys/kernel/random")
            os.makedirs(f"{root}/var/lib/btrfs-health-check")
            with open(f"{root}/proc/sys/kernel/random/boot_id", "w", encoding="utf-8") as f:
                f.write(BOOT + "\n")
            state = f"{root}/var/lib/btrfs-health-check/reclaim-errors-{UUID}-data"
            probe = adapters.Probe(root, SubprocessCommandRunner())
            self.assertEqual(len(bash), len(pairs))
            for (errs, seen), want in zip(pairs, bash, strict=True):
                with self.subTest(errs=errs[:24], seen=seen[:24]):
                    with open(f"{alloc}/data/reclaim_errors", "w", encoding="utf-8") as f:
                        f.write(errs + "\n")
                    with open(state, "w", encoding="utf-8") as f:
                        f.write(f"{BOOT} {seen}\n")
                    before = os.stat(state).st_ino
                    found = adapters._reclaim_error_problems(probe, "/var", UUID, alloc)
                    data = [p for p in found if " data reclaim " in p]
                    if data:
                        got = "problem " + data[0].split(" failed ", 1)[1].split(" ", 1)[0]
                    else:
                        got = "skip" if os.stat(state).st_ino == before else "none"
                    self.assertEqual(got, want)


class MemberDisksTest(unittest.TestCase):
    """The member disks' part, which a test folder cannot reach through the program (their block devices):
    the rules it applies to them, as the bash version did (its smart_check and swap_disks)."""

    def test_a_member_disk_keeps_the_label_without_a_role(self) -> None:
        from btrfs.health_check.domain import smart_problems

        self.assertEqual(
            smart_problems("nvme0n1", "", FAILING_SMART.replace("12", "1")),
            [
                "NVMe nvme0n1: critical_warning=1",
                "NVMe nvme0n1: media_errors=1",
                "NVMe nvme0n1: available_spare 5% below threshold 10%",
            ],
        )
        self.assertEqual(smart_problems("nvme0n1", "", HEALTHY_SMART), [])

    def test_the_controller_of_a_namespace(self) -> None:
        from btrfs.health_check.domain import controller

        self.assertEqual(
            [controller(d) for d in ("nvme0n1", "nvme12n3", "sda", "nvme1")],
            ["/dev/nvme0", "/dev/nvme12", "/dev/sda", "/dev/nvme1"],
        )

    def test_members_and_their_disks(self) -> None:
        from btrfs.health_check.domain import disks_of, member_paths

        show = (
            "Label: none  uuid: x\n\tTotal devices 2 FS bytes used 1.00TiB\n"
            "\tdevid    1 size 1.82TiB used 1.00TiB path /dev/mapper/luks-a\n"
            "\tdevid    2 size 1.82TiB used 1.00TiB path /dev/mapper/luks-b  \n"
        )
        self.assertEqual(member_paths(show), ["/dev/mapper/luks-a", "/dev/mapper/luks-b"])
        self.assertEqual(disks_of("dm-0 crypt\nnvme0n1p3 part\nnvme0n1 disk\nx disk extra\n"), ["nvme0n1"])

    def test_kernel_lines_of_member_devices_count(self) -> None:
        from btrfs.health_check.domain import kernel_problem

        klog = (
            "a: BTRFS error (device dm-0): csum failed\n"
            "b: I/O error, dev nvme0n1p3, sector 9\n"
            "c: BTRFS error (device dm-7): other fs\n"
            "d: I/O error, dev sdb, sector 1\n"
            "e: I/O error, dev nvme0n1, sector 2\n"
        )
        self.assertEqual(
            kernel_problem(klog, ["dm-0", "nvme0n1"]),
            "kernel: 3 btrfs/IO error message(s) since last check, e.g.: a: BTRFS error (device dm-0): csum failed | "
            "b: I/O error, dev nvme0n1p3, sector 9 | e: I/O error, dev nvme0n1, sector 2 | ",
        )
        self.assertIsNone(kernel_problem("d: I/O error, dev sdb, sector 1\n", ["dm-0"]))

    def test_a_whole_run_with_member_disks(self) -> None:
        # The run in this process, the block-device test answering yes for the fake members: the member
        # disks' health logs (no role), the OK line's disks, a swap on a member disk read once, and the
        # kernel lines of member devices.
        import contextlib
        import io
        from unittest import mock

        from btrfs.health_check import adapters, cli
        from cst_core.adapters.runner import SubprocessCommandRunner

        h = HealthCheckTest("test_healthy")
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.machine["lsblk"] = {
            "cst-test-1": "dm-0 crypt\nnvme0n1p3 part\nnvme0n1 disk\n",
            "cst-test-2": "dm-1 crypt\nnvme2n1p3 part\nnvme2n1 disk\n",
            "dm-2": "dm-2 crypt\nnvme0n1p2 part\nnvme0n1 disk\n",
        }
        h.swaps("/dev/dm-2 partition 1 0 -2")
        h.machine["nvme"] = {"nvme0": HEALTHY_SMART, "nvme2": HEALTHY_SMART}
        h.machine["kernel"] = (
            "a: BTRFS error (device cst-test-1): csum failed\n"
            "b: I/O error, dev nvme2n1p3, sector 1\n"
            "c: BTRFS error (device dm-9): other\n"
        )

        def run() -> tuple[int, str, str]:
            with open(h.machine_file, "w", encoding="utf-8") as f:
                json.dump(h.machine, f)
            if os.path.exists(h.calls_log):
                os.unlink(h.calls_log)
            env = {"PATH": f"{h.bin}:/usr/bin:/bin", "FAKE_MACHINE": h.machine_file, "FAKE_CALLS": h.calls_log}
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(adapters, "is_block", lambda p: p.startswith("/dev/mapper/cst-test-")),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                rc = cli.run_check(adapters.Probe(h.root, SubprocessCommandRunner()))
            return rc, out.getvalue(), err.getvalue()

        rc, out, err = run()
        self.assertEqual(rc, 1, err)
        self.assertEqual(
            err,
            f"<2>{TAG}: kernel: 2 btrfs/IO error message(s) since last check, e.g.: a: BTRFS error (device "
            "cst-test-1): csum failed | b: I/O error, dev nvme2n1p3, sector 1 | \n",
        )
        self.assertEqual([c[1:] for c in h.calls("nvme")], [["smart-log", "/dev/nvme0"], ["smart-log", "/dev/nvme2"]])
        h.machine["kernel"] = ""
        h.machine["nvme"] = {"nvme0": FAILING_SMART.replace("12", "1"), "nvme2": HEALTHY_SMART}
        rc, out, err = run()
        self.assertEqual(
            (rc, err),
            (
                1,
                (
                    f"<2>{TAG}: NVMe nvme0n1: critical_warning=1\n<2>{TAG}: NVMe nvme0n1: media_errors=1\n"
                    f"<2>{TAG}: NVMe nvme0n1: available_spare 5% below threshold 10%\n"
                ),
            ),
        )
        h.machine["nvme"] = {"nvme0": HEALTHY_SMART, "nvme2": HEALTHY_SMART}
        rc, out, err = run()
        self.assertEqual((rc, out, err), (0, f"<6>{TAG}: OK (/var; disks: nvme0n1 nvme2n1)\n", ""))

    def test_mounts_in_the_order_findmnt_lists_them(self) -> None:
        # bash kept them in awk's hash order (two or more filesystems); the port keeps findmnt's order.
        from btrfs.health_check.domain import pick_mounts

        self.assertEqual(pick_mounts("u1 /etc\nu1 /var\nu2 /mnt/b\nu2 /mnt/a\nu1 /home\n"), ["/var", "/mnt/b"])


class ShippedUnitTest(unittest.TestCase):
    def test_the_unit_passes_no_environment(self) -> None:
        # CST_HEALTH_ROOT is a test seam of a program run as root: the shipped unit must not hand it, or
        # any other setting, to the check (the tier 1 test adds its own Environment= to a copy).
        units = os.path.join(os.path.dirname(HERE), "units")
        for name in ("btrfs-health-check.service", "btrfs-health-check.timer"):
            with open(os.path.join(units, name), encoding="utf-8") as f:
                keys = [line.split("=", 1)[0].strip() for line in f if "=" in line and not line.startswith("#")]
            with self.subTest(name):
                self.assertFalse({"Environment", "EnvironmentFile", "PassEnvironment"} & set(keys), keys)


if __name__ == "__main__":
    unittest.main()
