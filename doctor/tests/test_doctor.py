"""Unit tests for doctor/cst-doctor (`cst doctor`): telling layered packages
requested by name from ones pinned to a version (tricky names included),
the one fix command, local packages, overrides, the staged deployment,
failed units, pending alerts, btrfs chunk allocation (a fixture sysfs
tree), the exit codes, the toolbox path (commands
through flatpak-spawn --host, files under the host's root) and the doctor
section of `cst status`.

Nothing reaches the real host: rpm-ostree, systemctl, findmnt, stat and
flatpak-spawn are stubs in a scratch bin folder first in PATH, the host's / is a scratch
folder (CST_DOCTOR_ROOT), and the stubs log every call, so the tests also
check that only read-only queries were made.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s doctor/tests -t . -v
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
DOCTOR_DIR = os.path.dirname(HERE)
TOOL = os.path.join(DOCTOR_DIR, "cst-doctor")
REPO = os.path.dirname(DOCTOR_DIR)
CST = os.path.join(REPO, "cst")
NOW = 1793900000          # 2026-11-05, after every fixture's timestamps
BOOTED, STAGED = "b" * 64, "5" * 64


def load():
    loader = importlib.machinery.SourceFileLoader("cst_doctor", TOOL)
    spec = importlib.util.spec_from_loader("cst_doctor", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


D = load()

# Each stub appends "<name> <args>" to $STUB_LOG.
RPM_OSTREE = """#!/bin/sh
echo "rpm-ostree $*" >>"$STUB_LOG"
case "$1" in
  status) [ "$2" = --json ] && exec cat "$FIX_STATUS" ;;
  db) [ "$2" = list ] && [ -f "$FIX_DB/$3" ] && exec cat "$FIX_DB/$3" ;;
esac
exit 1
"""
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >>"$STUB_LOG"
case " $* " in
  *" --user "*) cat "$FIX_USER_FAILED" ;;
  *) cat "$FIX_SYSTEM_FAILED" ;;
esac
"""
FINDMNT = """#!/bin/sh
echo "findmnt $*" >>"$STUB_LOG"
[ -f "$FIX_FINDMNT" ] && exec cat "$FIX_FINDMNT"
exit 1
"""
STAT = """#!/bin/sh
echo "stat $*" >>"$STUB_LOG"
[ -f "$FIX_STATFS" ] && exec cat "$FIX_STATFS"
exit 1
"""
FLATPAK_SPAWN = """#!/bin/sh
echo "flatpak-spawn $*" >>"$STUB_LOG"
[ "$1" = --host ] || exit 1
shift
exec "$@"
"""


class Scratch(unittest.TestCase):
    """A scratch host root, stub commands, and helpers to run the doctor."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-doctor-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "host")
        os.makedirs(os.path.join(self.root, "run"))
        open(os.path.join(self.root, "run", "ostree-booted"), "w").close()
        self.pending = os.path.join(self.root, "var", "lib", "cst-alerts", "pending")
        os.makedirs(self.pending)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("rpm-ostree", RPM_OSTREE), ("systemctl", SYSTEMCTL),
                           ("findmnt", FINDMNT), ("stat", STAT), ("flatpak-spawn", FLATPAK_SPAWN)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.db = os.path.join(self.tmp, "db")
        os.makedirs(self.db)
        for csum in (BOOTED, STAGED):
            shutil.copy(os.path.join(FIX, "db-booted.txt"), os.path.join(self.db, csum))
        self.sys_failed = os.path.join(self.tmp, "sys-failed")
        self.user_failed = os.path.join(self.tmp, "user-failed")
        open(self.sys_failed, "w").close()
        open(self.user_failed, "w").close()
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "XDG_STATE_HOME": self.state,
            "CST_DOCTOR_CONTAINER": "0",
            "CST_DOCTOR_ROOT": self.root,
            "CST_DOCTOR_NOW": str(NOW),
            "STUB_LOG": self.log,
            "FIX_STATUS": os.path.join(FIX, "unpinned.json"),
            "FIX_DB": self.db,
            "FIX_SYSTEM_FAILED": self.sys_failed,
            "FIX_USER_FAILED": self.user_failed,
            "FIX_FINDMNT": os.path.join(self.tmp, "findmnt.out"),
            "FIX_STATFS": os.path.join(self.tmp, "statfs.out"),
        }

    def status(self, name):
        self.env["FIX_STATUS"] = os.path.join(FIX, name)

    def run_tool(self, *args, cmd=None):
        cmd = cmd or ["python3", "-I", TOOL]
        return subprocess.run(cmd + list(args), env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def assert_read_only(self):
        allowed = ("rpm-ostree status --json", "rpm-ostree db list ", "systemctl list-units --state=failed",
                   "systemctl --user list-units --state=failed", "flatpak-spawn --host ",
                   "findmnt -rn -t btrfs -o SOURCE,TARGET", "stat -f -c %S %b ")
        for c in self.calls():
            self.assertTrue(c.startswith(allowed), f"unexpected call: {c}")
            self.assertNotIn("sudo", c)


# ---- names and versions ------------------------------------------------------------------------
class ClassifyTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "db-booted.txt")) as f:
            self.pkgs = [ln.strip() for ln in f if ln.startswith(" ")]

    def test_parse_nevra(self):
        self.assertEqual(D.parse_nevra("samba-2:4.23.2-1.fc44.x86_64"),
                         ("samba", "2", "4.23.2", "1.fc44", "x86_64"))
        self.assertEqual(D.parse_nevra("java-21-openjdk-21.0.9.0.10-1.fc44.x86_64")[0], "java-21-openjdk")
        self.assertIsNone(D.parse_nevra("vim"))
        self.assertIsNone(D.parse_nevra("foo-x:1-2.noarch"))

    def test_real_pinned_requests(self):
        reqs = ["below", "containerd-2.3.4-1.fc44", "docker-compose-switch-1.0.5-10.fc44",
                "tini-static-0.19.0-12.fc44", "pigz-2.8-8.fc44"]
        got = D.classify_requests(reqs, self.pkgs)
        self.assertEqual(got, [("below", "below", "name"),
                               ("containerd-2.3.4-1.fc44", "containerd", "pinned"),
                               ("docker-compose-switch-1.0.5-10.fc44", "docker-compose-switch", "pinned"),
                               ("tini-static-0.19.0-12.fc44", "tini-static", "pinned"),
                               ("pigz-2.8-8.fc44", "pigz", "pinned")])

    def test_names_with_digits_and_dashes_are_not_pins(self):
        reqs = ["python3.13", "gtk4", "java-21-openjdk", "vim", "pkgconfig(gtk4)", "/usr/bin/zsh"]
        for pkgs in (self.pkgs, None):          # with and without the package list
            for r, n, how in D.classify_requests(reqs, pkgs):
                self.assertEqual((n, how), (r, "name"), r)

    def test_other_spellings_of_a_version(self):
        reqs = ["samba-2:4.23.2-1.fc44", "samba-4.23.2-1.fc44.x86_64", "pigz-2.8",
                "NetworkManager-1:1.56.1-2.fc44.x86_64"]
        got = D.classify_requests(reqs, self.pkgs)
        self.assertEqual([(n, how) for _, n, how in got],
                         [("samba", "pinned"), ("samba.x86_64", "pinned"), ("pigz", "pinned"),
                          ("NetworkManager.x86_64", "pinned")])

    def test_arch_is_kept(self):
        """A request for a foreign arch (multilib) must come back as name.arch,
        or the fix would swap wine-core.i686 for wine-core.x86_64."""
        both = ["wine-core-9.0-1.fc44.x86_64", "wine-core-9.0-1.fc44.i686", "fonts-1-1.fc44.noarch"]
        got = D.classify_requests(["wine-core-9.0-1.fc44.i686"], both, native="x86_64")
        self.assertEqual(got, [("wine-core-9.0-1.fc44.i686", "wine-core.i686", "pinned")])
        self.assertEqual(D.unpin_command(got),
                         "sudo rpm-ostree uninstall wine-core-9.0-1.fc44.i686 --install=wine-core.i686")
        # No arch in the request: the native one is installed, so the bare name.
        got = D.classify_requests(["wine-core-9.0-1.fc44", "fonts-1-1.fc44"], both, native="x86_64")
        self.assertEqual([n for _, n, _ in got], ["wine-core", "fonts"])
        # No arch in the request, but only the foreign one installed.
        got = D.classify_requests(["wine-core-9.0-1.fc44"], both[1:], native="x86_64")
        self.assertEqual(got[0][1], "wine-core.i686")
        # The form fallback (no package list) reads the arch off the string too.
        got = D.classify_requests(["wine-core-9.0-1.fc44.i686", "pigz-2.8-8.fc44"], None, native="x86_64")
        self.assertEqual(got, [("wine-core-9.0-1.fc44.i686", "wine-core.i686", "pinned-form"),
                               ("pigz-2.8-8.fc44", "pigz", "pinned-form")])

    def test_pin_matching_nothing_installed_is_inactive(self):
        """With the package list read, the form alone does not make a pin to
        install back: pigz may now come from the base image (another build),
        and --install=pigz would make rpm-ostree refuse the whole fix."""
        pkgs = [p for p in self.pkgs if not p.startswith("pigz-")] + ["pigz-2.8-9.fc44.x86_64"]
        got = D.classify_requests(["pigz-2.8-8.fc44", "containerd-2.3.4-1.fc44"], pkgs)
        self.assertEqual(got, [("pigz-2.8-8.fc44", "", "inactive"),
                               ("containerd-2.3.4-1.fc44", "containerd", "pinned")])
        self.assertEqual(D.unpin_command(got),
                         "sudo rpm-ostree uninstall pigz-2.8-8.fc44 containerd-2.3.4-1.fc44 --install=containerd")
        self.assertEqual(D.classify_requests(["foo-1.0-1.fc44"], self.pkgs)[0][2], "inactive")

    def test_form_only_when_package_list_unknown(self):
        got = D.classify_requests(["moby-engine-rootless-extras-29.7.2-1.fc44", "foo-1.0-1"], None)
        self.assertEqual(got[0], ("moby-engine-rootless-extras-29.7.2-1.fc44", "moby-engine-rootless-extras",
                                  "pinned-form"))
        self.assertEqual(got[1][2], "name")      # no distribution tag: not guessed

    def test_unpin_command(self):
        cls = D.classify_requests(["below", "containerd-2.3.4-1.fc44", "samba-2:4.23.2-1.fc44",
                                   "pigz-2.8-8.fc44", "pigz"], self.pkgs)
        self.assertEqual(D.unpin_command(cls),
                         "sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 samba-2:4.23.2-1.fc44 "
                         "pigz-2.8-8.fc44 --install=containerd --install=samba")
        self.assertEqual(D.unpin_command(D.classify_requests(["below"], self.pkgs)), "")

    def test_pkg_names(self):
        with open(os.path.join(FIX, "overrides.json")) as f:
            d = json.load(f)["deployments"][0]
        self.assertEqual(D.pkg_names(d["base-local-replacements"]), ["mesa-dri-drivers"])
        self.assertEqual(D.pkg_names([d["base-removals"], d["requested-base-removals"]]),
                         ["firefox", "firefox-langpacks"])


# ---- whole runs ------------------------------------------------------------------------------------
class RpmOstreeTest(Scratch):
    def test_unpinned_is_all_ok(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("8 layered packages, all requested by name", r.stdout)
        self.assertIn("doctor: all good", r.stdout)
        self.assert_read_only()

    def test_pinned_booted_warns_with_one_fix(self):
        self.status("pinned.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("booted deployment 44.20261028.0: 6 of 10 layered packages are pinned", r.stdout)
        self.assertIn("fix: sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 docker-cli-29.7.2-1.fc44 "
                      "docker-compose-switch-1.0.5-10.fc44 moby-engine-29.7.2-1.fc44 pigz-2.8-8.fc44 "
                      "tini-static-0.19.0-12.fc44 --install=containerd --install=docker-cli "
                      "--install=docker-compose-switch --install=moby-engine --install=pigz "
                      "--install=tini-static", r.stdout)
        self.assertEqual(r.stdout.count("fix: sudo rpm-ostree uninstall"), 1)
        self.assertNotIn("recognised by their form", r.stdout)
        self.assert_read_only()

    def test_pinned_without_package_list_uses_the_form(self):
        self.status("pinned.json")
        os.unlink(os.path.join(self.db, BOOTED))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("recognised by their form only", r.stdout)
        self.assertIn("--install=docker-compose-switch", r.stdout)

    def test_staged_clean_only_notes_the_booted(self):
        self.status("staged-clean.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("info   booted deployment 44.20261028.0: 6 of 10", r.stdout)
        self.assertIn("the staged deployment 44.20261104.0 has no version-pinned requests: reboot to use it",
                      r.stdout)
        self.assertNotIn("by name only", r.stdout)
        self.assertIn("staged deployment 44.20261104.0: 8 layered packages, all requested by name", r.stdout)
        self.assertIn("waits for a reboot", r.stdout)
        self.assertNotIn("fix:", r.stdout)

    def test_staged_pinned_warns_for_the_staged(self):
        self.status("staged-pinned.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARN   staged deployment 44.20261104.0: 6 of 10", r.stdout)
        self.assertNotIn("booted deployment 44.20261028.0: 6", r.stdout)
        self.assertEqual(r.stdout.count("fix: sudo rpm-ostree uninstall"), 1)
        self.assertIn(f"rpm-ostree db list {STAGED}", "\n".join(self.calls()))

    def test_inactive_pin_in_a_whole_run(self):
        self.status("pinned.json")
        with open(os.path.join(self.db, BOOTED)) as f:
            db = f.read().replace("pigz-2.8-8.fc44", "pigz-2.8-9.fc44")
        with open(os.path.join(self.db, BOOTED), "w") as f:
            f.write(db)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("matching no installed package (probably inactive requests", r.stdout)
        self.assertIn("): pigz-2.8-8.fc44", r.stdout)
        self.assertNotIn("--install=pigz", r.stdout)
        self.assertNotIn("form only", r.stdout)
        self.assertIn(" pigz-2.8-8.fc44 tini-static-0.19.0-12.fc44 --install=containerd", r.stdout)

    def test_rollback_pending_is_the_next_boot(self):
        """After rpm-ostree rollback the first deployment boots next without
        being staged: the fix must come from its requests."""
        self.status("rollback.json")
        shutil.copy(os.path.join(FIX, "db-booted.txt"), os.path.join(self.db, "0" * 64))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WARN   pending deployment 44.20261009.0 (next boot): 1 of 3 layered packages are pinned",
                      r.stdout)
        self.assertIn("fix: sudo rpm-ostree uninstall pigz-2.8-8.fc44 --install=pigz\n", r.stdout)
        self.assertEqual(r.stdout.count("fix: sudo rpm-ostree uninstall"), 1)
        self.assertNotIn("WARN   booted", r.stdout)
        self.assertIn("pending deployment 44.20261009.0 (next boot) is not the booted one", r.stdout)
        self.assertNotIn("no pending deployment", r.stdout)

    def test_no_pending_when_booted_is_first(self):
        r = self.run_tool()
        self.assertIn("ok     no pending deployment: the next boot is the booted one", r.stdout)
        self.status("pinned.json")                      # booted first, an older rollback after it
        r = self.run_tool()
        self.assertIn("no pending deployment", r.stdout)
        self.assertNotIn("44.20261009.0", r.stdout)

    def test_staged_old(self):
        self.status("staged-old.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"staged deployment 44.20261020.0 waits for a reboot, built .*, 2[0-9] days ago")

    def test_local_packages(self):
        self.status("local.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 local RPM file(s) layered: mytool-1.2-3.fc44.x86_64", r.stdout)
        self.assertIn("fix: sudo rpm-ostree uninstall mytool-1.2-3.fc44.x86_64 --install=mytool", r.stdout)

    def test_overrides(self):
        self.status("overrides.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("base packages replaced by another version: mesa-dri-drivers", r.stdout)
        self.assertIn("fix: sudo rpm-ostree override reset mesa-dri-drivers", r.stdout)
        self.assertIn("info   booted deployment 44.20261028.0: base packages removed on purpose: "
                      "firefox, firefox-langpacks", r.stdout)

    def test_not_ostree(self):
        os.unlink(os.path.join(self.root, "run", "ostree-booted"))
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertIn("not an rpm-ostree system", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("rpm-ostree")])

    def test_rpm_ostree_failure_warns(self):
        self.env["FIX_STATUS"] = os.path.join(self.tmp, "missing.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("layered packages: not checked (rpm-ostree status --json failed", r.stdout)


class ServicesTest(Scratch):
    def test_failed_units(self):
        with open(self.sys_failed, "w") as f:
            f.write("btrfs-health-check.service loaded failed failed btrfs health\n")
        with open(self.user_failed, "w") as f:
            f.write("run-p1964432-i22912428.scope loaded failed failed [systemd-run] /usr/bin/true\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARN   system units failed: btrfs-health-check.service", r.stdout)
        self.assertIn("Read why first: journalctl -b -u btrfs-health-check.service", r.stdout)
        self.assertIn("fix: sudo systemctl reset-failed btrfs-health-check.service\n", r.stdout)
        self.assertIn("info   user: 1 transient unit(s) failed (systemd-run, low priority): "
                      "run-p1964432-i22912428.scope; to clear: systemctl --user reset-failed", r.stdout)
        self.assert_read_only()

    def test_only_transient_is_exit_0(self):
        with open(self.user_failed, "w") as f:
            f.write("run-r1.service loaded failed failed x\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_user_units_skipped_as_root(self):
        host = D.Host(self.env)
        host.uid = 0
        res = list(D.check_user_units(host))
        self.assertEqual(res[0].status, D.INFO)
        self.assertIn("running as root", res[0].message)


class AlertsTest(Scratch):
    def alert(self, name, urgency="normal", title="Disk sda: SMART warning", age=3600):
        p = os.path.join(self.pending, name)
        with open(p, "w") as f:
            f.write(f"cst-alert 1\nurgency={urgency}\napp=btrfs-health\ntitle={title}\n"
                    f"first={NOW - age}\nlast={NOW - age}\ncount=1\n\nbody\n")
        os.utime(p, (NOW - age, NOW - age))
        return p

    def test_unseen_alerts(self):
        self.alert("a" * 32 + ".alert", "critical", "btrfs: device missing")
        self.alert("b" * 32 + ".alert")
        self.alert("c" * 32 + ".alert", age=8 * 86400)            # too old
        self.alert("not-an-alert.txt")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR  alert you have not seen yet (critical", r.stdout)
        self.assertIn("btrfs: device missing", r.stdout)
        self.assertIn("WARN   alert you have not seen yet (normal", r.stdout)
        self.assertEqual(r.stdout.count("alert you have not seen yet"), 2)
        self.assertIn("fix: cst-pending-alerts --list\n", r.stdout)

    def test_seen_alert_is_info(self):
        p = self.alert("d" * 32 + ".alert")
        st = os.stat(p)
        sec, ns = divmod(st.st_mtime_ns, 10 ** 9)
        os.makedirs(os.path.join(self.state, "cst-alerts"))
        with open(os.path.join(self.state, "cst-alerts", "delivered"), "w") as f:
            f.write(f"{'d' * 32}.alert {sec}.{ns:09d}0 shown 1 {NOW}\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 alert(s) of the last 7 days already shown to you", r.stdout)

    def test_legacy_link(self):
        os.symlink("cst-alerts", os.path.join(self.root, "var", "lib", "f44-alerts"))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("/var/lib/f44-alerts: link left by the move", r.stdout)
        self.assertIn("fix: cst btrfs install\n", r.stdout)

    def test_no_alert_folder(self):
        shutil.rmtree(os.path.dirname(self.pending))
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertIn("no alert folder", r.stdout)


UUID = "afed93e2-f4ac-43de-8a48-751b552435c7"
GIB = 1024 ** 3
DEV_SECTORS = 1536 * GIB // 512        # 1.5 TiB block devices


class BtrfsTest(Scratch):
    """check_btrfs_allocation on a fixture /sys/fs/btrfs tree (RAID1 over
    two dm-crypt devices unless a test changes it)."""

    def fs(self, data=(930, 730), meta=(20, 14), system=(0.04, 0.0002), profile="raid1",
           ndev=2, sectors=DEV_SECTORS, dyn=1, per=1, missing=0, rsv=0.5, fs_sectors=None):
        """fs_sectors: the btrfs size of each device (default: its whole
        block device); statfs reports their sum over the data copies."""
        base = os.path.join(self.root, "sys", "fs", "btrfs", UUID)
        os.makedirs(os.path.join(self.root, "sys", "fs", "btrfs", "features"), exist_ok=True)
        copies = {"raid1": 2, "single": 1, "dup": 2, "raid10": 2}[profile]

        def put(rel, value):
            p = os.path.join(base, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(f"{value}\n")

        for kind, (total, used) in (("data", data), ("metadata", meta), ("system", system)):
            t, u = int(total * GIB), int(used * GIB)
            put(f"allocation/{kind}/total_bytes", t)
            put(f"allocation/{kind}/bytes_used", u)
            put(f"allocation/{kind}/disk_total", t * copies)
            put(f"allocation/{kind}/{profile}/total_bytes", t)
            put(f"allocation/{kind}/{profile}/used_bytes", u)
            if dyn is not None:
                put(f"allocation/{kind}/dynamic_reclaim", dyn if kind == "data" else 0)
                put(f"allocation/{kind}/periodic_reclaim", per if kind == "data" else 0)
            put(f"allocation/{kind}/reclaim_count", 3)
            put(f"allocation/{kind}/reclaim_errors", 0)
        put("allocation/global_rsv_size", int(rsv * GIB))
        for i in range(ndev):
            put(f"devices/dm-{i}/size", sectors)
            put(f"devices/dm-{i}/dm/name", f"luks-{i}")
            put(f"devinfo/{i + 1}/missing", missing if i == ndev - 1 else 0)
        with open(self.env["FIX_FINDMNT"], "w") as f:
            f.write("/dev/dm-1[/root] /sysroot\n/dev/dm-1[/var] /var\n/dev/dm-1[/home] /var/home\n")
        fs_bytes = ndev * (sectors if fs_sectors is None else fs_sectors) * 512
        with open(self.env["FIX_STATFS"], "w") as f:
            f.write(f"4096 {fs_bytes // copies // 4096}\n")
        return base

    def test_no_btrfs_is_info(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("info   allocation: no mounted btrfs filesystem", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("findmnt")])

    def test_healthy_raid1(self):
        self.fs()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn("ok     btrfs /var (RAID1): data chunks 730.0 GiB used of 930.0 GiB allocated (78%), "
                      "metadata chunks 14.0 GiB used of 20.0 GiB allocated (70%), metadata is 1.9% of data", out)
        self.assertIn("ok     btrfs /var: automatic data reclaim on (dynamic, periodic): 3 chunk(s) reclaimed", out)
        # 1536 - 930 - 20 - 0.04 GiB on each device: one copy of every RAID1 chunk.
        self.assertIn("unallocated per device /dev/mapper/luks-0 586.0 GiB, /dev/mapper/luks-1 586.0 GiB "
                      "(warning below 50 GiB, critical below 5 GiB;", out)
        self.assertIn("findmnt -rn -t btrfs -o SOURCE,TARGET", self.calls())
        self.assert_read_only()

    def test_low_unallocated_warns(self):
        self.fs(data=(1490, 1400))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARN   btrfs /var: little unallocated space per device: /dev/mapper/luks-0 26.0 GiB", r.stdout)
        self.assertIn("'No space left' but df shows free space", r.stdout)

    def test_critical_unallocated_is_error_without_a_fix(self):
        # The Fedora report: metadata 95% full, data 78%, 1 MiB unallocated.
        self.fs(data=(1515.959, 1182), meta=(20, 19.5), rsv=0.5)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        out = r.stdout
        self.assertIn("ERROR  btrfs /var: CRITICAL: unallocated per device /dev/mapper/luks-0 1 MiB", out)
        self.assertIn("never a metadata or full balance", out)
        self.assertIn("ERROR  btrfs /var: metadata almost full (512 MiB free in metadata chunks", out)
        btrfs = out.split("== btrfs", 1)[1].split("\ndoctor:", 1)[0]
        self.assertNotIn("fix:", btrfs)
        self.assertNotIn("balance start", out)

    def test_shrunk_device_is_not_ok(self):
        # One btrfs device shrunk by 600 GiB: the block sizes overstate unallocated.
        self.fs(fs_sectors=DEV_SECTORS - 300 * GIB // 512)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var "
                      "(the hourly btrfs-health-check watches it); not estimated: the btrfs devices use "
                      "600.0 GiB less than their block devices (shrunk with btrfs filesystem resize?)", r.stdout)
        self.assertNotIn("ok     btrfs /var: unallocated", r.stdout)
        self.assertIn("stat -f -c %S %b /var", self.calls())
        self.assert_read_only()

    def test_shrunk_device_still_warns_with_an_upper_bound(self):
        # The block sizes give an upper bound: below the levels is below them for sure.
        self.fs(data=(1515.959, 1182), meta=(20, 19.5), fs_sectors=DEV_SECTORS - 100 * GIB // 512)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR  btrfs /var: CRITICAL: unallocated per device at most /dev/mapper/luks-0 1 MiB", r.stdout)

    def test_unreadable_statfs_is_not_ok(self):
        self.fs()
        os.unlink(self.env["FIX_STATFS"])
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("not estimated: the filesystem size could not be read (stat -f /var)", r.stdout)
        self.fs(data=(1490, 1400))
        os.unlink(self.env["FIX_STATFS"])
        r = self.run_tool()
        self.assertIn("WARN   btrfs /var: little unallocated space per device: at most /dev/mapper/luks-0 26.0 GiB",
                      r.stdout)

    def test_statfs_rounding_is_not_a_shrink(self):
        self.fs()
        with open(self.env["FIX_STATFS"], "w") as f:
            f.write(f"4096 {2 * DEV_SECTORS * 512 // 2 // 4096 - 3}\n")
        r = self.run_tool()
        self.assertIn("ok     btrfs /var: unallocated per device /dev/mapper/luks-0 586.0 GiB", r.stdout)

    def test_thresholds_come_from_the_health_check(self):
        self.assertEqual(D.unalloc_thresholds(D.Host(self.env)), (50, 5))
        orig = D.HEALTH_CHECK_REPO
        D.HEALTH_CHECK_REPO = os.path.join(self.tmp, "nope")
        try:
            hc = os.path.join(self.root, "usr", "local", "sbin")
            os.makedirs(hc)
            with open(os.path.join(hc, "btrfs-health-check"), "w") as f:
                f.write("UNALLOC_MIN_GIB=600       # x\nUNALLOC_CRIT_GIB=7\n")
            self.assertEqual(D.unalloc_thresholds(D.Host(self.env)), (600, 7))
            self.fs()
            res = [r for a, r in D.run_checks(D.Host(self.env)) if a == "btrfs"]
            self.assertEqual(res[-1].status, D.WARN, res)
            self.assertIn("warning below 600 GiB, critical below 7 GiB", res[-1].message)
            os.unlink(os.path.join(hc, "btrfs-health-check"))
            self.assertEqual(D.unalloc_thresholds(D.Host(self.env)), D.UNALLOC_DEFAULTS)
        finally:
            D.HEALTH_CHECK_REPO = orig

    def test_reclaim_off_on_the_root_warns(self):
        self.fs(dyn=0, per=1)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARN   btrfs /var: automatic data reclaim is not set up (dynamic_reclaim = 0)", r.stdout)
        self.assertIn("         fix: cst btrfs install", r.stdout)
        conf = os.path.join(self.root, "etc", "tmpfiles.d")
        os.makedirs(conf)
        with open(os.path.join(conf, "btrfs-automatic-reclaim.conf"), "w") as f:
            f.write(f"w- /sys/fs/btrfs/{UUID}/allocation/data/dynamic_reclaim - - - - 1\n")
        r = self.run_tool()
        self.assertIn("automatic data reclaim is configured by CST but switched off", r.stdout)

    def test_reclaim_off_elsewhere_is_info(self):
        self.fs(dyn=0, per=0)
        with open(self.env["FIX_FINDMNT"], "w") as f:
            f.write("/dev/dm-0 /mnt/usb\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("info   btrfs /mnt/usb: automatic data reclaim off (dynamic_reclaim, periodic_reclaim)", r.stdout)

    def test_old_kernel_without_reclaim_switches(self):
        self.fs(dyn=None)
        r = self.run_tool()
        self.assertIn("no automatic reclaim switches (Linux 6.11 or later)", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_missing_device_needs_root(self):
        self.fs(missing=1)
        r = self.run_tool()
        self.assertIn("info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var "
                      "(the hourly btrfs-health-check watches it); not estimated: a member device is missing",
                      r.stdout)

    def test_striped_layout_gives_the_sum_only(self):
        self.fs(profile="raid10", ndev=4, sectors=600 * GIB // 512)
        r = self.run_tool()
        # 4 x 600 - 2 x (930 + 20 + 0.04) GiB over all devices; the average is above 50 GiB.
        self.assertIn("info   btrfs /var: 499.9 GiB unallocated over 4 device(s) (layout RAID10); "
                      "per-device unallocated needs root", r.stdout)
        self.fs(profile="raid10", ndev=4, sectors=520 * GIB // 512)
        r = self.run_tool()
        self.assertIn("WARN   btrfs /var: 179.9 GiB unallocated over 4 device(s), so at least one device "
                      "has less than 50 GiB", r.stdout)

    def test_single_device_dup_metadata(self):
        base = self.fs(profile="single", ndev=1, sectors=1000 * GIB // 512)
        # Metadata as DUP: two copies on the one device.
        shutil.rmtree(os.path.join(base, "allocation", "metadata", "single"))
        os.makedirs(os.path.join(base, "allocation", "metadata", "dup"))
        with open(os.path.join(base, "allocation", "metadata", "dup", "total_bytes"), "w") as f:
            f.write(str(20 * GIB))
        with open(self.env["FIX_FINDMNT"], "w") as f:
            f.write("/dev/dm-0 /\n")
        r = self.run_tool()
        # 1000 - 930 - 2 x 20 - 0.04 GiB.
        self.assertIn("ok     btrfs / (DUP/SINGLE): data chunks", r.stdout)
        self.assertIn("little unallocated space per device: /dev/mapper/luks-0 30.0 GiB", r.stdout)

    def test_toolbox_reads_sysfs_under_the_host_root(self):
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        self.fs()
        r = self.run_tool()
        self.assertIn("flatpak-spawn --host findmnt -rn -t btrfs -o SOURCE,TARGET", self.calls())
        self.assertIn("flatpak-spawn --host stat -f -c %S %b /var", self.calls())
        self.assertIn("btrfs /var: unallocated per device", r.stdout)
        self.assert_read_only()


class CommandLineTest(Scratch):
    def test_usage_and_help(self):
        r = self.run_tool("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        self.assertEqual(self.run_tool("--brief", "--brief").returncode, 2)
        r = self.run_tool("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Exit status", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_brief_shows_only_problems(self):
        self.status("pinned.json")
        with open(self.user_failed, "w") as f:
            f.write("run-r1.scope loaded failed failed x\n")
        r = self.run_tool("--brief")
        self.assertEqual(r.returncode, 1)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith("doctor: needs attention: 0 ERROR, 1 WARN"), lines[0])
        self.assertIn("[rpm-ostree]", r.stdout)
        self.assertNotIn(" ok ", r.stdout)
        self.assertNotIn("info", r.stdout.split("\n", 1)[1])

    def test_crashing_check_is_an_error_line(self):
        def boom(host):
            raise RuntimeError("bad")
            yield  # pragma: no cover
        res = D.run_checks(D.Host(self.env), [("x", boom)])
        self.assertEqual(res[0][1].status, D.ERROR)
        out = io.StringIO()
        self.assertEqual(D.render(res, False, out), 1)
        self.assertIn("boom: the check itself failed (RuntimeError: bad)", out.getvalue())

    def test_fix_lines_are_only_a_command(self):
        """Every fix: line can be pasted as it is: no remarks after it."""
        self.status("local.json")
        with open(self.sys_failed, "w") as f:
            f.write("a.service loaded failed failed a\n")
        os.makedirs(os.path.join(self.root, "var", "lib", "f44-alerts"))
        self.alert_file = os.path.join(self.pending, "e" * 32 + ".alert")
        with open(self.alert_file, "w") as f:
            f.write(f"cst-alert 1\nurgency=normal\ntitle=t\nlast={NOW}\n\nbody\n")
        os.utime(self.alert_file, (NOW, NOW))
        r = self.run_tool()
        fixes = [ln.split("fix: ", 1)[1] for ln in r.stdout.splitlines() if "fix: " in ln]
        self.assertEqual(len(fixes), 4, r.stdout)
        for fx in fixes:
            self.assertNotIn("(", fx)
            self.assertNotIn("   ", fx)
            r2 = subprocess.run(["bash", "-n", "-c", fx], capture_output=True)
            self.assertEqual(r2.returncode, 0, fx)

    def test_registry_order(self):
        areas = []
        for a, _ in D.CHECKS:
            if a not in areas:
                areas.append(a)
        self.assertEqual(areas, ["rpm-ostree", "services", "alerts", "btrfs"])


class ToolboxTest(Scratch):
    def test_commands_go_through_flatpak_spawn(self):
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        self.status("pinned.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        calls = self.calls()
        spawned = [c for c in calls if c.startswith("flatpak-spawn")]
        direct = [c for c in calls if not c.startswith("flatpak-spawn")]
        self.assertEqual(len(spawned), len(direct))   # each stub call came through flatpak-spawn
        self.assertIn("flatpak-spawn --host rpm-ostree status --json", calls)
        self.assertIn("flatpak-spawn --host systemctl --user list-units --state=failed --no-legend "
                      "--plain --full --no-pager", calls)
        self.assert_read_only()

    def test_fixes_say_on_the_host_in_a_toolbox(self):
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        self.status("pinned.json")
        r = self.run_tool()
        self.assertIn("Inside a toolbox: run every command shown here on the host", r.stdout)
        self.assertIn("fix (on the host): sudo rpm-ostree uninstall", r.stdout)
        self.assertNotIn("  fix: ", r.stdout)
        r = self.run_tool("--brief")
        self.assertIn("fix (on the host): sudo rpm-ostree uninstall", r.stdout)
        self.env["CST_DOCTOR_CONTAINER"] = "0"
        r = self.run_tool()
        self.assertNotIn("on the host", r.stdout)
        self.assertIn("  fix: sudo rpm-ostree uninstall", r.stdout)

    def test_default_root_in_a_toolbox(self):
        env = dict(self.env)
        del env["CST_DOCTOR_ROOT"]
        env["CST_DOCTOR_CONTAINER"] = "1"
        self.assertEqual(D.Host(env).path("/var/lib/cst-alerts"), "/run/host/var/lib/cst-alerts")
        env["CST_DOCTOR_CONTAINER"] = "0"
        self.assertEqual(D.Host(env).path("/var/lib/cst-alerts"), "/var/lib/cst-alerts")

    def test_missing_flatpak_spawn(self):
        host = D.Host(dict(self.env, CST_DOCTOR_CONTAINER="1"))
        os.unlink(os.path.join(self.bin, "flatpak-spawn"))
        old = os.environ["PATH"]
        os.environ["PATH"] = self.bin
        try:
            rc, _, err = host.run(["systemctl", "--failed"])
        finally:
            os.environ["PATH"] = old
        self.assertEqual(rc, 127)
        self.assertIn("flatpak-spawn", err)


class CstIntegrationTest(Scratch):
    """`cst doctor` and the doctor section of `cst status`, on a copy of cst
    with no module scripts (so status runs no module and never asks sudo)."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "doctor"))
        shutil.copy(CST, self.repo)
        shutil.copy(TOOL, os.path.join(self.repo, "doctor"))
        self.cst = os.path.join(self.repo, "cst")
        self.env["CST_NAME"] = "cst"

    def test_cst_doctor(self):
        self.status("pinned.json")
        r = self.run_tool(cmd=["bash", self.cst, "doctor"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cst doctor: read-only check-up", r.stdout)
        self.assertEqual(self.run_tool("--nope", cmd=["bash", self.cst, "doctor"]).returncode, 2)
        self.assertEqual(self.run_tool(cmd=["bash", self.cst, "doctor", "--brief"]).returncode, 1)

    def test_cst_status_ends_with_doctor(self):
        r = self.run_tool(cmd=["bash", self.cst, "status"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("==== doctor (cst doctor: the full list)\ndoctor: all good", r.stdout)
        self.assertIn("skipped: not in this copy", r.stdout)
        self.status("pinned.json")
        r = self.run_tool(cmd=["bash", self.cst, "status"])
        self.assertEqual(r.returncode, 1)
        tail = r.stdout.split("==== doctor", 1)[1]
        self.assertIn("WARN   [rpm-ostree] booted deployment", tail)
        self.assertNotIn(" ok ", tail)

    def test_cst_help_lists_doctor(self):
        r = self.run_tool(cmd=["bash", self.cst, "help"])
        self.assertIn("cst doctor [--brief]", r.stdout)


if __name__ == "__main__":
    unittest.main()
