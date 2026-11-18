# SPDX-License-Identifier: MPL-2.0
"""Black-box tests of btrfs/docker-gc: the program run as a process, with a fake docker and systemctl.

The fake docker answers from a JSON file of containers, volumes and images
(the shape `docker inspect` prints), changes it when asked to remove
something, logs every call and, for `docker events`, prints the events
of the scenario. These tests check what the program prints, its exit
status, the commands it runs and the state it writes, so that they hold
whatever the code inside looks like.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest btrfs.tests.test_docker_gc_program -v
"""

from collections.abc import Mapping
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "docker-gc")
RECORDER_UNIT = os.path.join(os.path.dirname(HERE), "units", "docker-gc-recorder.service")
DAY = 86400
NEWER_STATE_EXIT = 65  # a state file of a newer docker-gc: the recorder unit does not restart on it
PROTECTED = "registry.example.com"  # an invented registry: what the settings file of these tests protects

FAKE_DOCKER = r"""#!@PYTHON@ -I
import json, os, sys, time
path = os.environ["FAKE_DOCKER_STATE"]
with open(path) as f:
    st = json.load(f)
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]


def save():
    with open(path, "w") as f:
        json.dump(st, f)


def fail(msg, rc=1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def image(ref):
    for im in st["images"]:
        if ref in (im["Id"], im["Id"][7:19]) or ref in (im.get("RepoTags") or []):
            return im
    return None


key = " ".join(a[:2])
if key in st.get("fail", {}):
    rc, msg = st["fail"][key]
    fail(msg, rc)
if a[0] == "info":
    if st.get("info_rc", 0):
        fail(st.get("info_err", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
             "Is the docker daemon running?"), st["info_rc"])
    print("FAKE-ID")
elif a[:2] == ["ps", "-aq"]:
    print("\n".join(c["Id"] for c in st["containers"]))
elif a[:2] == ["container", "inspect"]:
    print(json.dumps([c for c in st["containers"] if c["Id"] in a[2:]]))
elif a[:2] == ["volume", "ls"]:
    print("\n".join(v["Name"] for v in st["volumes"]))
elif a[:2] == ["volume", "inspect"]:
    print(json.dumps([v for v in st["volumes"] if v["Name"] in a[2:]]))
elif a[:2] == ["image", "ls"]:
    print("\n".join(im["Id"] for im in st["images"]))
elif a[:2] == ["image", "inspect"] and a[2] == "--format":
    im = image(a[4])
    if im is None:
        fail(f"Error: No such image: {a[4]}")
    print(im["Id"])
elif a[:2] == ["image", "inspect"]:
    found = [im for im in st["images"] if im["Id"] in a[2:]]
    print(json.dumps(found))
    if len(found) != len(a[2:]):
        sys.exit(1)
elif a[:2] == ["system", "df"]:
    n = len(st["images"])
    print(json.dumps({"Type": "Images", "Size": f"{n}GB"}))
    print(json.dumps({"Type": "Containers", "Size": "1MB"}))
    print("not json")
elif a[:2] == ["container", "rm"]:
    st["containers"] = [c for c in st["containers"] if c["Id"] != a[2]]
    save()
elif a[:2] == ["volume", "rm"]:
    if not any(v["Name"] == a[2] for v in st["volumes"]):
        fail(f"Error: No such volume: {a[2]}")
    st["volumes"] = [v for v in st["volumes"] if v["Name"] != a[2]]
    save()
elif a[:2] == ["image", "rm"]:
    im = image(a[2])
    if im is None:
        fail(f"Error: No such image: {a[2]}")
    if a[2] in (im.get("RepoTags") or []):
        im["RepoTags"].remove(a[2])
    if a[2] == im["Id"] or not im.get("RepoTags"):
        st["images"].remove(im)
    save()
elif a[:2] == ["builder", "prune"]:
    print("Total reclaimed space: 0B")
elif a[0] == "events":
    ev = st.get("events", {})
    for line in ev.get("lines", []):
        print(line, flush=True)
    while ev.get("hang"):
        time.sleep(0.1)
    sys.exit(ev.get("rc", 0))
else:
    fail(f"fake docker: unexpected {a}", 99)
"""

FAKE_SYSTEMCTL = r"""#!@PYTHON@ -I
import json, os, sys
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(["systemctl", *sys.argv[1:]]) + "\n")
sys.exit(int(os.environ.get("FAKE_SYSTEMCTL_RC", "0")))
"""


def iso(t: float) -> str:
    """A Docker time stamp (RFC 3339 with nanoseconds, UTC)."""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def sha(c: str) -> str:
    return "sha256:" + c * 64


OLD = 1577836800.0  # 2020-01-01: long ago for every keep window


def scenario(now: float) -> dict[str, object]:
    """Containers, volumes and images that cover each rule of a plan."""

    def container(
        cid: str,
        name: str,
        image: str,
        status: str,
        *,
        ref: str | None = None,
        labels: dict[str, str] | None = None,
        restart: str = "no",
        volumes: tuple[str, ...] = (),
    ) -> dict[str, object]:
        return {
            "Id": cid * 64,
            "Name": "/" + name,
            "Image": image,
            "Created": iso(OLD),
            "Config": {"Image": ref or "ref-of-" + name, "Labels": labels},
            "HostConfig": {"RestartPolicy": {"Name": restart}},
            "State": {
                "Status": status,
                "Running": status == "running",
                "StartedAt": iso(OLD),
                "FinishedAt": "0001-01-01T00:00:00Z" if status == "running" else iso(OLD),
            },
            "Mounts": [{"Type": "volume", "Name": v} for v in volumes],
        }

    anon = {"com.docker.volume.anonymous": ""}
    return {
        "containers": [
            container("1", "admiring_turing", sha("a"), "exited", ref="alpine:old", volumes=("v1",)),
            container("2", "redis_cache", sha("b"), "exited", volumes=("v4",)),
            container("3", "web", sha("c"), "running"),
            container("4", "db", sha("d"), "exited", labels={"org.testcontainers": "true"}),
            container("5", "eager_euler", sha("f"), "exited", labels={"com.docker.compose.project": "x"}),
            container("6", "brave_bohr", sha("f"), "exited", restart="always"),
        ],
        "volumes": [
            {"Name": "v1", "Labels": anon},
            {"Name": "v2", "Labels": anon},
            {"Name": "v3", "Labels": None},
            {"Name": "v4", "Labels": anon},
        ],
        "images": [
            {"Id": sha("a"), "RepoTags": ["alpine:old"], "Size": 7_000_000, "Metadata": {"LastTagTime": iso(OLD)}},
            {"Id": sha("b"), "RepoTags": ["redis:7"], "Size": 100, "Metadata": {}},
            {"Id": sha("c"), "RepoTags": ["web:1"], "Size": 100, "Metadata": {}},
            {
                "Id": sha("d"),
                "RepoTags": ["registry.example.com/team/db:1"],
                "Size": 3_000_000,
                "Metadata": {"LastTagTime": iso(OLD)},
            },
            {"Id": sha("e"), "RepoTags": None, "RepoDigests": [], "Size": 999, "Metadata": {}},
            {"Id": sha("9"), "RepoTags": ["fresh:1"], "Size": 5, "Metadata": {"LastTagTime": iso(now - DAY)}},
            {"Id": sha("8"), "RepoTags": ["seen:1"], "Size": 6, "Metadata": {}},
            {"Id": sha("f"), "RepoTags": ["kept:1"], "Size": 6, "Metadata": {}},
        ],
    }


class Program:
    """A temporary folder with the fake docker and systemctl, the state and the call log."""

    def __init__(self, test: unittest.TestCase, tool: str = TOOL) -> None:
        self.tool = tool
        self.tmp = tempfile.mkdtemp(prefix="cst-gc-prog-")
        test.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.tmp, "state")
        self.runtime = os.path.join(self.tmp, "run")
        os.mkdir(self.bin)
        for name, text in (("docker", FAKE_DOCKER), ("systemctl", FAKE_SYSTEMCTL)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(text.replace("@PYTHON@", sys.executable))
            os.chmod(path, 0o755)
        self.conf = os.path.join(self.tmp, "etc", "docker-gc.conf")
        os.mkdir(os.path.dirname(self.conf))
        self.installed = os.path.join(self.tmp, "no-installed-docker-gc")
        with open(self.conf, "w") as f:
            f.write(f"DOCKER_GC_PROTECT={PROTECTED}\n")
        self.docker_state = os.path.join(self.tmp, "docker.json")
        self.log = os.path.join(self.tmp, "calls.log")
        self.now = time.time()
        self.set_docker(scenario(self.now))
        self.systemctl_rc = 0

    def set_docker(self, st: Mapping[str, object]) -> None:
        with open(self.docker_state, "w") as f:
            json.dump(st, f)

    def docker(self) -> dict[str, object]:
        with open(self.docker_state) as f:
            return json.load(f)

    def recorded(self, since: float, images: dict[str, float], heartbeat: float) -> None:
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "last-used.json"), "w") as f:
            json.dump({"since": since, "images": images, "heartbeat": heartbeat}, f)

    def last_used(self) -> dict[str, object]:
        with open(os.path.join(self.state, "last-used.json")) as f:
            return json.load(f)

    def calls(self) -> list[list[str]]:
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "DOCKER_GC_STATE": self.state,
            "DOCKER_GC_RUNTIME": self.runtime,
            # The settings file (registry.example.com protected) and no installed docker-gc: never the machine's.
            "DOCKER_GC_CONF": self.conf,
            "DOCKER_GC_INSTALLED": self.installed,
            "FAKE_DOCKER_STATE": self.docker_state,
            "FAKE_DOCKER_LOG": self.log,
            "FAKE_SYSTEMCTL_RC": str(self.systemctl_rc),
        }

    def run(self, *args: str) -> tuple[int, str, str]:
        r = subprocess.run(
            [sys.executable, "-I", "-B", self.tool, *args],
            env=self.env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
            cwd="/",
        )
        return r.returncode, r.stdout, r.stderr

    def start(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-I", "-B", self.tool, *args],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd="/",
        )


def days_ago(t: float) -> int:
    return int((time.time() - t) // DAY)


PLAN_RECORDER_OK = """\
ephemeral containers stopped for 7+ days: 2
  admiring_turing                  alpine:old  (stopped {old} days ago)
  db                               ref-of-db  (stopped {old} days ago)
orphaned anonymous volumes: 2
  v1
  v2
unused images: 2, up to 7.0 MB (shared layers counted per image)
     7.0 MB  alpine:old  (last use {seen_a})
      999 B  <untagged> eeeeeeeeeeee  (last use {since})
held back: 1 image(s) (protected registry: cleanup-now only)
"""


class StatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Program(self)
        self.since = OLD + 30 * DAY
        self.p.recorded(self.since, {sha("8"): self.p.now - DAY}, self.p.now)

    def expected_plan(self) -> str:
        since = datetime.fromtimestamp(self.since).strftime("%F")
        return PLAN_RECORDER_OK.format(old=days_ago(OLD), seen_a=since, since=since)

    def test_status_with_the_recorder_running(self) -> None:
        rc, out, err = self.p.run("status")
        self.assertEqual((rc, err), (0, ""))
        since = datetime.fromtimestamp(self.since)
        removal = datetime.fromtimestamp(self.since + 14 * DAY).strftime("%F")
        self.assertEqual(
            out,
            f"protected registries: {PROTECTED} (DOCKER_GC_PROTECT in {self.p.conf})\n"
            f"image use recorded since {since.strftime('%F %T')} (1 image(s) seen); timed image removal "
            f"possible from {removal}; recorder ok\n"
            "last run: never\n"
            "a weekly run now would remove:\n" + self.expected_plan(),
        )
        calls = self.p.calls()
        self.assertEqual(
            [c for c in calls if c[0] == "systemctl"],
            [["systemctl", "-q", "is-active", "docker-gc-recorder.service"]] * 2,
        )
        self.assertEqual(
            [c[:2] for c in calls if c[0] != "systemctl"],
            [
                ["info", "--format"],
                ["ps", "-aq"],
                ["container", "inspect"],
                ["volume", "ls"],
                ["volume", "inspect"],
                ["image", "ls"],
                ["image", "inspect"],
            ],
        )
        self.assertFalse(os.path.exists(self.p.runtime), "status takes no lock")

    def test_status_with_the_recorder_stopped_holds_every_image(self) -> None:
        self.p.systemctl_rc = 3
        rc, out, _ = self.p.run("status")
        self.assertEqual(rc, 0)
        self.assertIn("; recorder NOT running (images are held back)\n", out)
        self.assertTrue(
            out.endswith(
                "unused images: 0, up to 0 B (shared layers counted per image)\n"
                "held back: 3 image(s) (the recorder is not running)\n"
            ),
            out,
        )

    def test_status_shows_the_last_run(self) -> None:
        with open(os.path.join(self.p.state, "last-run.json"), "w") as f:
            json.dump({"at": OLD, "containers": 1, "volumes": 2, "images": 3, "image_bytes": 4_500_000, "failed": 1}, f)
        _, out, _ = self.p.run("status")
        at = datetime.fromtimestamp(OLD).strftime("%F %T")
        self.assertIn(f"\nlast run {at}: 1 container(s), 2 volume(s), 3 image(s) (4.5 MB), failures 1\n", out)
        with open(os.path.join(self.p.state, "last-run.json"), "w") as f:
            f.write('{"at": 1}')
        _, out, _ = self.p.run("status")
        self.assertIn("\nlast run: never\n", out)

    def test_status_without_docker(self) -> None:
        shutil.rmtree(self.p.state)
        st = self.p.docker()
        st["info_rc"] = 1
        self.p.set_docker(st)
        rc, out, err = self.p.run("status")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            out,
            f"protected registries: {PROTECTED} (DOCKER_GC_PROTECT in {self.p.conf})\n"
            "image use not recorded yet (recorder not started)\nlast run: never\ndocker is not running; nothing to do\n",
        )
        self.assertFalse(os.path.exists(self.p.state), "status writes no state")

    def test_a_docker_that_refuses_is_an_error_not_a_stopped_docker(self) -> None:
        # Permission denied (or SELinux): the weekly run and its dry run say so and exit 1.
        st = self.p.docker()
        st["info_rc"] = 1
        st["info_err"] = "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
        self.p.set_docker(st)
        for args in (("run", "--dry-run"), ("run",), ("status",)):
            rc, out, err = self.p.run(*args)
            self.assertEqual(rc, 1, args)
            self.assertNotIn("not running", out + err)
            self.assertIn("docker info: permission denied while trying to connect", out + err)

    def test_a_dry_run_lists_and_removes_nothing(self) -> None:
        before = self.p.docker()
        rc, out, err = self.p.run("run", "--dry-run")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, self.expected_plan() + "(dry run: nothing removed)\n")
        self.assertEqual(self.p.docker(), before)
        self.assertTrue(os.path.isfile(os.path.join(self.p.runtime, "run.lock")))
        self.assertFalse(os.path.exists(os.path.join(self.p.state, "last-run.json")))

    def test_a_failing_docker_command_is_logged_and_exits_1(self) -> None:
        st = self.p.docker()
        st["fail"] = {"volume ls": [1, "Error response from daemon: boom"]}
        self.p.set_docker(st)
        rc, out, err = self.p.run("run", "--dry-run")
        self.assertEqual((rc, out, err), (1, "<3>docker-gc: docker volume ls: Error response from daemon: boom\n", ""))

    def test_a_state_of_a_newer_docker_gc_stops_every_command_and_stays(self) -> None:
        # After going back to an older CST: the older docker-gc must not read a newer last-used.json
        # as "no state" and write over it (or remove images by it).
        path = os.path.join(self.p.state, "last-used.json")
        text = json.dumps({"since": OLD, "images": {sha("8"): self.p.now}, "heartbeat": self.p.now, "v": 2})
        with open(path, "w") as f:
            f.write(text)
        before = self.p.docker()
        for args in (("status",), ("run",), ("run", "--dry-run"), ("record",)):
            with self.subTest(args=args):
                rc, out, err = self.p.run(*args)
                self.assertEqual((rc, err), (NEWER_STATE_EXIT, ""))
                self.assertIn(f"<3>docker-gc: {path} is format 2 of the docker-gc-state (this CST reads format 1", out)
                with open(path) as f:
                    self.assertEqual(f.read(), text)
        self.assertEqual(self.p.docker(), before)
        self.assertFalse(os.path.exists(os.path.join(self.p.state, "last-run.json")))

    def test_a_last_run_of_a_newer_docker_gc_stops_the_run_before_it_removes(self) -> None:
        # The run writes last-run.json at its end and never read it: a newer one was written over.
        path = os.path.join(self.p.state, "last-run.json")
        text = json.dumps(
            {"at": OLD, "containers": 0, "volumes": 0, "images": 0, "image_bytes": 0, "failed": 0, "v": 2}
        )
        with open(path, "w") as f:
            f.write(text)
        before = self.p.docker()
        rc, out, err = self.p.run("run")
        self.assertEqual((rc, err), (NEWER_STATE_EXIT, ""))
        self.assertIn(f"<3>docker-gc: {path} is format 2 of the docker-gc-last-run (this CST reads format 1", out)
        with open(path) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(self.p.docker(), before)

    def test_the_recorder_unit_does_not_restart_on_a_newer_state(self) -> None:
        # Restart=on-failure with RestartSec=10 would start the recorder again every 10 s, forever, to
        # read the same file (systemd's start limit never trips at that pace), and the unit would never
        # show as failed. Its exit status for that case stops the restarts (tier 1 test_78: real systemd).
        with open(RECORDER_UNIT) as f:
            service = f.read().split("[Service]\n", 1)[1].split("\n[", 1)[0].splitlines()
        self.assertIn("Restart=on-failure", service)
        self.assertIn(f"RestartPreventExitStatus={NEWER_STATE_EXIT}", service)

    def test_cleanup_now_needs_a_terminal(self) -> None:
        rc, out, err = self.p.run("cleanup-now")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "cleanup-now asks for confirmation: run it in a terminal.\n")
        self.assertFalse(os.path.exists(self.p.runtime), "no lock taken")


# main's (654bbda) btrfs/docker_gc/domain.py, its built-in registry replaced by PROTECTED: the one file
# of the docker-gc it installed that holds what it protects.
LEGACY_DOMAIN = os.path.join(
    os.path.dirname(os.path.dirname(HERE)), "tests", "fixtures", "legacy", "btrfs-docker_gc-domain.py@654bbda"
)


class SettingsUpgradeTest(unittest.TestCase):
    """The protected registries move from the program into /etc/cst/docker-gc.conf. While the file is
    missing, the registries the installed docker-gc protected (main's, built in) stay protected;
    `settings --write` (what `cst btrfs docker-gc enable` runs before it installs) keeps them in the
    file. The program as a process, the installed docker-gc a zipapp as main installed it."""

    def setUp(self) -> None:
        self.p = Program(self)
        self.p.recorded(OLD + 30 * DAY, {sha("8"): self.p.now - DAY}, self.p.now)
        os.remove(self.p.conf)
        self.p.installed = os.path.join(self.p.tmp, "installed-docker-gc")
        with open(LEGACY_DOMAIN, "rb") as f:
            domain = f.read()
        with zipfile.ZipFile(self.p.installed, "w") as z:
            z.writestr("__main__.py", "raise SystemExit('a stand-in for main\\'s docker-gc')\n")
            z.writestr("btrfs/__init__.py", "")
            z.writestr("btrfs/docker_gc/__init__.py", "")
            z.writestr("btrfs/docker_gc/domain.py", domain)

    def test_the_registry_of_an_older_docker_gc_stays_protected_and_moves_into_the_file(self) -> None:
        source = f"built into the installed {self.p.installed}, while {self.p.conf} is missing"
        rc, out, err = self.p.run("settings")
        self.assertEqual((rc, err), (10, ""))
        self.assertEqual(
            out.splitlines(),
            [
                f"protected registries: {PROTECTED} ({source} ('cst btrfs docker-gc enable' writes them there))",
                f"would write: {self.p.conf} (DOCKER_GC_PROTECT={PROTECTED})",
            ],
        )
        self.assertFalse(os.path.exists(self.p.conf), "the dry run wrote the file")
        # Meanwhile (this checkout's status, run --dry-run, cleanup-now) its images are held back.
        rc, out, _ = self.p.run("run", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("held back: 1 image(s) (protected registry: cleanup-now only)", out)
        rc, out, err = self.p.run("settings", "--write")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(f"written:   {self.p.conf} (DOCKER_GC_PROTECT={PROTECTED})", out)
        self.assertEqual(os.stat(self.p.conf).st_mode & 0o777, 0o644)
        with open(self.p.conf) as f:
            text = f.read()
        self.assertTrue(text.endswith(f"\nDOCKER_GC_PROTECT={PROTECTED}\n"), text)
        # From the file now, also once a docker-gc without a built-in registry is installed.
        os.remove(self.p.installed)
        rc, out, _ = self.p.run("settings", "--write")
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.splitlines(),
            [f"protected registries: {PROTECTED} (DOCKER_GC_PROTECT in {self.p.conf})", f"unchanged: {self.p.conf}"],
        )
        with open(self.p.conf) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(self.p.run("settings")[0], 0)

    def test_an_installed_docker_gc_it_cannot_read_stops_every_run(self) -> None:
        with open(self.p.installed, "w") as f:
            f.write("print('not a docker-gc of CST')\n")
        for args in (("status",), ("run", "--dry-run"), ("run",), ("settings", "--write")):
            rc, out, _ = self.p.run(*args)
            self.assertEqual(rc, 78, args)
            self.assertIn(f"{self.p.conf} is missing, and the installed {self.p.installed} does not show", out)
        self.assertFalse(os.path.exists(self.p.conf))
        self.assertEqual(self.p.calls(), [], "it asked docker before it knew what is protected")

    def test_a_settings_file_it_cannot_read_stops_every_run(self) -> None:
        with open(self.p.conf, "w") as f:
            f.write("DOCKER_GC_PROTCT=registry.example.com\n")
        rc, out, _ = self.p.run("run")
        self.assertEqual(rc, 78)
        self.assertIn(f"<3>docker-gc: {self.p.conf} line 1: DOCKER_GC_PROTCT: unknown setting", out)
        self.assertIn(f"<3>docker-gc: nothing is removed until {self.p.conf} is fixed", out)
        self.assertEqual(self.p.calls(), [], "it asked docker before it knew what is protected")


class RecordTest(unittest.TestCase):
    EVENTS = (
        json.dumps({"Type": "container", "Action": "start", "Actor": {"Attributes": {"image": "redis:7"}}}),
        json.dumps({"Type": "image", "Action": "pull", "Actor": {"ID": sha("9")}}),
        json.dumps({"Type": "image", "Action": "tag", "Actor": {"ID": "web:1"}}),
        json.dumps({"Type": "container", "Action": "exec_start", "Actor": {"Attributes": {"image": "seen:1"}}}),
        json.dumps({"Type": "container", "Action": "create", "Actor": {"Attributes": {"image": "gone:1"}}}),
        "not json",
    )

    def setUp(self) -> None:
        self.p = Program(self)
        st = self.p.docker()
        st["events"] = {"lines": list(self.EVENTS), "rc": 0}
        self.p.set_docker(st)

    def check_state(self, start: float, end: float) -> None:
        st = self.p.last_used()
        self.assertEqual(list(st), ["since", "images", "heartbeat", "v"])
        images = st["images"]
        assert isinstance(images, dict)
        # Every container counts as a use at the start (stopped ones: when they stopped), then the events.
        self.assertEqual(sorted(images), sorted([sha("a"), sha("b"), sha("c"), sha("d"), sha("f"), sha("9")]))
        self.assertEqual(images[sha("a")], OLD + 0.123456)  # the stamps carry .123456789
        for iid in (sha("b"), sha("c"), sha("9")):
            self.assertTrue(start <= images[iid] <= end, (iid, images[iid]))
        since, beat = st["since"], st["heartbeat"]
        assert isinstance(since, float) and isinstance(beat, float)
        self.assertTrue(start <= since <= beat <= end, st)

    def test_events_ending_by_themselves_exit_1_after_saving(self) -> None:
        start = time.time()
        rc, out, err = self.p.run("record")
        self.assertEqual((rc, out, err), (1, "<6>docker-gc: recording image use from docker events\n", ""))
        self.check_state(start, time.time())
        calls = self.p.calls()
        self.assertIn(
            ["events", "--format", "{{json .}}", "--filter", "type=container", "--filter", "type=image"], calls
        )
        self.assertEqual(
            [c[4] for c in calls if c[:3] == ["image", "inspect", "--format"]], ["redis:7", "web:1", "gone:1"]
        )

    def test_events_ending_with_a_status_exit_with_it(self) -> None:
        st = self.p.docker()
        st["events"] = {"lines": [], "rc": 5}
        self.p.set_docker(st)
        self.assertEqual(self.p.run("record")[0], 5)

    def test_a_stop_saves_and_exits_0(self) -> None:
        st = self.p.docker()
        st["events"] = {"lines": list(self.EVENTS), "hang": True}
        self.p.set_docker(st)
        start = time.time()
        proc = self.p.start("record")
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline(), "<6>docker-gc: recording image use from docker events\n")
        deadline = time.time() + 30
        while time.time() < deadline and sum(c[:3] == ["image", "inspect", "--format"] for c in self.p.calls()) < 3:
            time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual((proc.returncode, out, err), (0, "", ""))
        self.check_state(start, time.time())

    def test_a_recorder_that_was_running_keeps_its_clock(self) -> None:
        # A fresh heartbeat: no time lost, so "since" and what was recorded stay. (The other
        # branch, a recorder down for a while, depends on the uptime: test_docker_gc.RecordTest.)
        start = time.time()
        self.p.recorded(OLD, {sha("8"): OLD}, start)
        rc, out, _ = self.p.run("record")
        self.assertEqual((rc, out), (1, "<6>docker-gc: recording image use from docker events\n"))
        st = self.p.last_used()
        self.assertEqual(st["since"], OLD)
        images = st["images"]
        assert isinstance(images, dict)
        self.assertEqual(images[sha("8")], OLD)  # what was recorded is kept
        beat = st["heartbeat"]
        assert isinstance(beat, float)
        self.assertGreaterEqual(beat, start)


if __name__ == "__main__":
    unittest.main()
