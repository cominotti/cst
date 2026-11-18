# SPDX-License-Identifier: MPL-2.0
"""The fake machine of the btrfs/btrfs-snapshot tests (test_snapshot_hourly.py,
test_snapshot_convert.py, test_snapshot_nas.py).

RuntimeTest runs the whole bash program with CST_SNAPSHOT_ROOT pointing at a
scratch root (its settings, state, lock, pin file, the btrfs-snapshot-core and
btrfs-notify it runs, and /proc are all below it) and with a closed PATH: a
folder of fake commands (btrfs, findmnt, mountpoint, mount, umount, stat, cp,
chcon, restorecon, hostname, sftp, restic) and a folder of links to an
explicit list of real tools (awk, sed, find, flock, ...). A command that is
neither is not found (127): nothing reaches the real machine or the network.

The fakes answer from a description of the machine (machine.json) and write
down every call (calls, one JSON list per line: the command and its
arguments; sftp's also with its input, restic's with its RESTIC_* variables
and the pin file as it was; calls.env: the variables each call got on top of
the test's environment). The program runs with a service's umask (0022).
btrfs-snapshot-core is an argv recorder: its command line is the
contract between the two programs, and its answers (exit status, output) come
from the description. stat answers 256 (a subvolume's root inode) for the
folders the fake btrfs created or the test marked, and passes every other call
to the real stat; cp drops --reflink=always (the scratch folder may not be
btrfs) and runs the real cp, then does what machine.json says happens during
the clone (cp_write: a write into the source, cp_open: a process that opens
it), or fails with cp_err on stderr.

Nothing in here touches the real host: the fake btrfs deletes only below the
scratch folder, and the unit tests never gain privileges (tests/run_unit.py).
"""

from collections.abc import Generator, Mapping
import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from typing import Final
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT: Final = os.path.join(os.path.dirname(HERE), "btrfs-snapshot")
GIB: Final = 1024**3
USAGE: Final = (
    "usage: btrfs-snapshot hourly|nas|convert|convert-boot|list [--json]|caches [--json]|prune [--dry-run] "
    "[--allow-mass-delete]|purge|planned-left|busy [--home DIR] PATH...|convert-one PATH\n"
)
FAKES: Final = (
    "btrfs",
    "findmnt",
    "mountpoint",
    "mount",
    "umount",
    "stat",
    "cp",
    "chcon",
    "restorecon",
    "hostname",
    "sftp",
    "restic",
)
# The umask of a systemd service (UMask=0022), for the modes of the files the program creates.
UMASK: Final = 0o022
# Runs its arguments with SIGINT and SIGHUP at their defaults (the same process: exec).
SIGNALS_AT_DEFAULT: Final = (
    "/usr/bin/python3",
    "-I",
    "-c",
    "import os, signal, sys\n"
    "for s in (signal.SIGINT, signal.SIGHUP):\n"
    "    signal.signal(s, signal.SIG_DFL)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])",
)
# The real tools the program may run; everything else is not found.
REAL_TOOLS: Final = (
    "bash",
    "awk",
    "sed",
    "find",
    "sort",
    "cat",
    "cut",
    "grep",
    "head",
    "tr",
    "wc",
    "date",
    "touch",
    "rm",
    "mv",
    "mkdir",
    "rmdir",
    "mktemp",
    "flock",
    "timeout",
    "ionice",
    "nice",
    "env",
    "chown",
    "chmod",
    "install",
    "dirname",
    "basename",
    "python3",
    "tee",
    "sleep",
)

# One program answers for every fake command (named by its link), from the
# machine described in $FAKE_MACHINE.
FAKE = r"""#!/usr/bin/python3 -I
import json, os, shutil, sys
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
mf = os.environ["FAKE_MACHINE"]
scratch = os.environ["FAKE_SCRATCH"]
with open(mf) as f:
    m = json.load(f)

def save():
    with open(mf + ".tmp", "w") as f:
        json.dump(m, f)
    os.replace(mf + ".tmp", mf)

def ino(p):
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return "%d:%d" % (st.st_dev, st.st_ino)

if name == "stat":
    if len(args) == 3 and args[:2] == ["-c", "%i"] and ino(args[2]) in m.get("subvols", []):
        print(256)
        sys.exit(0)
    os.execv("/usr/bin/stat", ["stat"] + args)

rec = [name] + args
if name == "restic":
    rec.append({k: v for k, v in sorted(os.environ.items()) if k.startswith("RESTIC_")})
    pin = os.environ["CST_SNAPSHOT_ROOT"] + "/run/btrfs-snapshot/pinned"
    rec.append(open(pin).read() if os.path.exists(pin) else None)
if name == "sftp":
    rec.append(sys.stdin.read())
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps(rec) + "\n")
# The variables the program changed or added for this call (bash's own PWD, SHLVL, OLDPWD, _ aside).
base = json.loads(os.environ["FAKE_BASE_ENV"])
added = {k: v for k, v in sorted(os.environ.items())
         if base.get(k) != v and k not in ("FAKE_BASE_ENV", "PWD", "SHLVL", "OLDPWD", "_")}
with open(os.environ["FAKE_CALLS"] + ".env", "a") as f:
    f.write(json.dumps([name, added]) + "\n")

def take(key, default=0, table=None):
    # The answer under key: a list is a queue (its first item; the last one stays).
    t = m if table is None else table
    v = t.get(key, default)
    if isinstance(v, list):
        if len(v) > 1:
            t[key] = v[1:]
            save()
        return v[0] if v else default
    return v

def inside(p):
    return os.path.realpath(p).startswith(scratch + "/")

def answer(table, key):
    # An entry {"rc": .., "out": .., "err": .., "sleep": seconds} of table[key] (a queue of them, or one).
    a = take(key, {}, table)
    # Bytes that are not UTF-8 stand as lone surrogates in the description: written as those bytes.
    sys.stdout.buffer.write(a.get("out", "").encode("utf-8", "surrogateescape"))
    sys.stderr.buffer.write(a.get("err", "").encode("utf-8", "surrogateescape"))
    sys.stdout.flush()
    if a.get("sleep"):
        import time
        time.sleep(a["sleep"])
    sys.exit(a.get("rc", 0))

if name == "btrfs":
    if args[:3] == ["filesystem", "usage", "-b"]:
        free = take("free", None)
        if free is None:
            sys.exit(1)
        if isinstance(free, str):  # a reading that is not a number
            print("Overall:\n    Free (estimated):\t\t %s" % free)
            sys.exit(0)
        print("Overall:\n    Device size:\t\t 4000797868032\n    Free (estimated):\t\t %d\t(min: %d)" % (free, free // 2))
        sys.exit(0)
    if args[:2] == ["subvolume", "create"]:
        rc = take("create_rc")
        if rc or not inside(args[2]):
            sys.exit(rc or 99)
        os.mkdir(args[2])
        m.setdefault("subvols", []).append(ino(args[2]))
        save()
        print("Create subvolume '%s'" % args[2])
        sys.exit(0)
    if args[:2] == ["subvolume", "delete"]:
        rc = take("delete_rc")
        if rc:
            sys.exit(rc)
        for p in args[2:]:
            if not inside(p):
                sys.exit(99)
            i = ino(p)
            if i in m.get("subvols", []):
                m["subvols"].remove(i)
            shutil.rmtree(p, ignore_errors=True)
        save()
        sys.exit(0)
    if args[:2] == ["subvolume", "sync"]:
        sys.exit(0)
    sys.exit("fake btrfs: not faked: %s" % args)
if name == "findmnt":
    sys.stdout.write("".join(t + "\n" for t in m.get("mounts", [])))
    sys.exit(0)
if name == "mountpoint":
    sys.exit(0 if args[-1] in m.get("mountpoints", []) else 1)
if name == "mount":
    rc = take("mount_rc")
    if rc == 0:
        m.setdefault("mountpoints", []).append(args[-1])
        m.setdefault("mount_modes", {})[args[-1]] = "%o" % (os.stat(args[-1]).st_mode & 0o7777)
        save()
    sys.exit(rc)
if name == "umount":
    p = args[-1]
    if p in m.get("stuck", []):
        sys.stderr.write("umount: %s: target is busy.\n" % p)
        sys.exit(32)
    if p not in m.get("mountpoints", []):
        sys.stderr.write("umount: %s: not mounted.\n" % p)
        sys.exit(32)
    m["mountpoints"].remove(p)
    save()
    sys.exit(0)
if name == "cp":
    if not all(inside(a) for a in args if a.startswith("/")):
        sys.exit(99)
    if m.get("cp_err"):
        sys.stderr.write(m["cp_err"])
        sys.exit(m.get("cp_rc", 1))
    import subprocess
    rc = subprocess.run(["/usr/bin/cp"] + [a for a in args if a != "--reflink=always"]).returncode
    # What happens while the clone runs: a write into the source, a process that opens it.
    if m.get("cp_write"):
        with open(m["cp_write"], "a") as f:
            f.write("late")
    if m.get("cp_open"):
        link, target = m["cp_open"]
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
    sys.exit(rc)
if name in ("chcon", "restorecon"):
    sys.exit(0)
if name == "hostname":
    print("testhost")
    sys.exit(0)
if name == "sftp":
    sys.exit(take("sftp_rc"))
if name == "btrfs-notify":
    sys.exit(take("notify_rc"))
if name == "btrfs-snapshot-core":
    core = m.get("core", {})
    if args[0] == "latest":
        n = args[args.index("--name") + 1]
        p = m.get("latest", {}).get(n)
        if p:
            print(p)
        sys.exit(0 if p else 1)
    answer(core, args[0])
if name == "restic":
    r = m.get("restic", {})
    cmd = args[0]
    if cmd == "cat" and "cat" not in r:
        print(json.dumps({"version": 2, "id": m.get("repo_id", "ab" * 32), "chunker_polynomial": "1"}, indent=2))
        sys.exit(0)
    if cmd == "features" and "features" not in r:
        print("Name                     Type   Default  Description\n"
              "device-id-for-hardlinks  alpha  false    store deviceID only for hardlinks")
        sys.exit(0)
    if cmd == "snapshots" and "snapshots" not in r:
        print("[]")
        sys.exit(0)
    answer(r, cmd)
sys.exit("fake: not faked: %s %s" % (name, args))
"""


def log(level: int, text: str) -> str:
    """A journal line of btrfs-snapshot (its <level> prefix)."""
    return f"<{level}>btrfs-snapshot: {text}"


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class RuntimeTest(unittest.TestCase):
    """A scratch machine for one btrfs-snapshot run (see the module docstring).

    The home folder is HOME (HOME_MOUNT/u), the btrfs top level TOP with the
    snapshot folder SNAPDIR in it; snapshots.conf names them."""

    tmp: str
    root: str
    bin: str
    home_mount: str
    home: str
    top: str
    snapdir: str
    state: str
    machine_file: str
    calls_file: str
    env: dict[str, str]

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="btrfs-snapshot-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        self.bin = os.path.join(self.tmp, "bin")
        real = os.path.join(self.tmp, "real")
        for d in (self.bin, real, os.path.join(self.tmp, "tmpdir")):
            os.makedirs(d)
        fake = os.path.join(self.bin, "fake")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        for name in FAKES:
            os.symlink(fake, os.path.join(self.bin, name))
        for name in REAL_TOOLS:
            found = shutil.which(name, path="/usr/bin:/bin")
            if found is None:
                raise AssertionError(f"{name} is not installed (/usr/bin, /bin)")
            os.symlink(found, os.path.join(real, name))
        for d in ("usr/local/sbin", "etc/btrfs-setup", "run", "proc/self", "var/lib"):
            os.makedirs(self.path(d))
        # The program's own mount table: the home subvolume at HOME_MOUNT (see mountinfo()).
        self.mountinfo()
        os.symlink(fake, self.path("usr/local/sbin/btrfs-snapshot-core"))
        os.symlink(fake, self.path("usr/local/sbin/btrfs-notify"))
        self.home_mount = os.path.join(self.tmp, "home")
        self.home = os.path.join(self.home_mount, "u")
        self.top = os.path.join(self.tmp, "top")
        self.snapdir = os.path.join(self.top, "snapshots")
        os.makedirs(self.home)
        os.makedirs(self.snapdir)
        self.state = self.path("var/lib/btrfs-snapshot")
        self.machine_file = os.path.join(self.tmp, "machine.json")
        self.calls_file = os.path.join(self.tmp, "calls")
        self.machine(free=400 * GIB)
        self.conf()
        self.env = {
            "PATH": f"{self.bin}:{real}",
            "CST_SNAPSHOT_ROOT": self.root,
            "FAKE_MACHINE": self.machine_file,
            "FAKE_CALLS": self.calls_file,
            "FAKE_SCRATCH": self.tmp,
            "TMPDIR": os.path.join(self.tmp, "tmpdir"),
            "HOME": self.tmp,
            "LANG": "C.UTF-8",
            "TZ": "UTC",
        }
        self.env["FAKE_BASE_ENV"] = json.dumps(self.env)

    # ---- the machine -------------------------------------------------------------------
    def path(self, rel: str) -> str:
        """A path below the scratch root (rel without its leading /)."""
        return os.path.join(self.root, rel)

    def write(self, path: str, text: str, mtime: float | None = None) -> str:
        """Writes text to path (a path below the root when relative), and sets its mtime."""
        p = path if os.path.isabs(path) else self.path(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def conf(self, extra: str = "", **settings: str) -> None:
        """snapshots.conf naming the scratch home and top level, plus settings and extra lines."""
        base = {"HOME_DIR": self.home, "HOME_MOUNT": self.home_mount, "TOP": self.top, **settings}
        self.write("etc/btrfs-setup/snapshots.conf", "".join(f"{k}='{v}'\n" for k, v in base.items()) + extra)

    def machine(self, **kw: object) -> None:
        """Sets entries of the machine description (machine.json)."""
        try:
            m = json.loads(read(self.machine_file))
        except FileNotFoundError:
            m = {}
        m.update(kw)
        with open(self.machine_file, "w", encoding="utf-8") as f:
            json.dump(m, f)

    def described(self) -> dict[str, object]:
        result: dict[str, object] = json.loads(read(self.machine_file))
        return result

    def core(self, **answers: Mapping[str, object] | list[Mapping[str, object]]) -> None:
        """btrfs-snapshot-core's answers per command: {"rc": .., "out": .., "err": ..}."""
        self.machine(core=answers)

    def mark_subvol(self, path: str) -> None:
        """path (an existing folder) is a subvolume from now on: stat says inode 256."""
        st = os.lstat(path)
        found = self.described().get("subvols")
        subvols = [str(s) for s in found] if isinstance(found, list) else []
        self.machine(subvols=[*subvols, f"{st.st_dev}:{st.st_ino}"])

    def mountinfo(self, text: str = "") -> None:
        """/proc/self/mountinfo of the program (empty: nothing is mounted at HOME_MOUNT)."""
        self.write("proc/self/mountinfo", text)

    def busy(self, path: str, pid: int = 100, link: str = "fd/3") -> None:
        """A process (pid, below the root's /proc) has path open (link: fd/N, cwd, root or exe)."""
        p = self.path(f"proc/{pid}/{link}")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        os.symlink(path, p)

    @contextlib.contextmanager
    def hold_lock(self, rel: str = "run/btrfs-snapshot.lock") -> Generator[None]:
        """Another run holds the lock file (flock) while the block runs."""
        with open(self.path(rel), "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    # ---- one run -----------------------------------------------------------------------
    def run_prog(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", SCRIPT, *args],
            check=False,
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
            cwd=self.tmp,
            umask=UMASK,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def start_prog(self, *args: str) -> subprocess.Popen[str]:
        """The program started in the background (the caller waits for it, or signals it).

        Started as systemd starts a service: SIGINT and SIGHUP at their defaults, also when
        the test runner was started with them ignored (a background job), which a child
        inherits and bash cannot trap."""
        p = subprocess.Popen(
            [*SIGNALS_AT_DEFAULT, "bash", SCRIPT, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            cwd=self.tmp,
            umask=UMASK,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self.end_prog, p)
        return p

    def end_prog(self, p: subprocess.Popen[str]) -> None:
        """Kills what is left of a started run, the fakes it started too, and reaps it."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        p.communicate()

    def forget_calls(self) -> None:
        for p in (self.calls_file, self.calls_file + ".env"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(p)

    def call_envs(self, name: str | None = None) -> list[dict[str, str]]:
        """What each call of the fakes (or of one command) got on top of the test's environment."""
        try:
            lines = read(self.calls_file + ".env").splitlines()
        except FileNotFoundError:
            return []
        found: list[dict[str, str]] = []
        for ln in lines:
            command, env = json.loads(ln)
            if name is None or command == name:
                found.append(env)
        return found

    def mode(self, path: str) -> int:
        return stat.S_IMODE(os.stat(path if os.path.isabs(path) else self.path(path)).st_mode)

    def calls(self, name: str | None = None) -> list[list[object]]:
        """Every call of the fakes (or those of one command), in order."""
        try:
            lines = read(self.calls_file).splitlines()
        except FileNotFoundError:
            return []
        found: list[list[object]] = [json.loads(ln) for ln in lines]
        return [c for c in found if name is None or c[0] == name]

    def calls_but_hostname(self) -> list[list[object]]:
        """The calls of the fakes besides hostname -s, which every start runs (NAS_RESTIC_HOST's default)."""
        return [c for c in self.calls() if c[0] != "hostname"]

    def lines(self, text: str) -> list[str]:
        return text.splitlines()

    def core_calls(self) -> list[list[object]]:
        return [c[1:] for c in self.calls("btrfs-snapshot-core")]

    def notified(self) -> list[list[object]]:
        """btrfs-notify's arguments, one list per alert sent."""
        return [c[1:] for c in self.calls("btrfs-notify")]

    def state_file(self, name: str) -> str:
        return os.path.join(self.state, name)

    def read_state(self, name: str) -> str:
        return read(self.state_file(name))

    def common(self, cmd: str) -> list[str]:
        """The arguments every snapshot/prune/finish call of the core starts with."""
        return [
            cmd,
            "--snapdir",
            self.snapdir,
            "--no-lock",
            "--pin-file",
            self.path("run/btrfs-snapshot/pinned"),
            "--state-dir",
            self.state,
            "--list-hint",
            "cst btrfs snapshots list",
            "--confirm-hint",
            "cst btrfs snapshots prune --confirm",
        ]
