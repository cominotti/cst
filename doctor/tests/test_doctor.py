"""Unit tests for doctor/cst-doctor (`cst doctor`): telling layered packages
requested by name from ones pinned to a version (tricky names included),
the one fix command, local packages, overrides, the staged deployment,
failed units, pending alerts, btrfs chunk allocation (a fixture sysfs
tree), the game profile's drift and leftover drop-ins, the user manager's
boxes outside CST's rules (a fixture cgroup tree), the exit codes, the toolbox path (commands
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
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
DOCTOR_DIR = os.path.dirname(HERE)
TOOL = os.path.join(DOCTOR_DIR, "cst-doctor")
REPO = os.path.dirname(DOCTOR_DIR)
CST = os.path.join(REPO, "cst")
NOW = 1793900000  # 2026-11-05, after every fixture's timestamps
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
# systemctl show: prints $FIX_SHOW/<sys|usr>/<unit> (K=V lines), or fails
# when that file is missing; everything else is the failed-units list.
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >>"$STUB_LOG"
case " $* " in
  *" show "*) for a in "$@"; do last="$a"; done
              mgr=sys; case " $* " in *" --user "*) mgr=usr ;; esac
              [ -f "$FIX_SHOW/$mgr/$last" ] && exec cat "$FIX_SHOW/$mgr/$last"
              echo "Failed to connect to bus" >&2; exit 1 ;;
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
# virsh: no libvirt unless a test sets FIX_VIRSH (a folder: list.txt,
# caps.xml, NAME.xml for dumpxml); every other subcommand fails.
VIRSH = """#!/bin/sh
echo "virsh $*" >>"$STUB_LOG"
[ -n "$FIX_VIRSH" ] || { echo "error: failed to connect to the hypervisor" >&2; exit 1; }
for a in "$@"; do last="$a"; done
case " $* " in
  *" list --all --name "*) exec cat "$FIX_VIRSH/list.txt" ;;
  *" domcapabilities "*) [ -f "$FIX_VIRSH/caps.xml" ] && exec cat "$FIX_VIRSH/caps.xml" ;;
  *" dumpxml --inactive "*) [ -f "$FIX_VIRSH/$last.xml" ] && exec cat "$FIX_VIRSH/$last.xml" ;;
  *" domstate "*) [ -f "$FIX_VIRSH/$last.state" ] && exec cat "$FIX_VIRSH/$last.state"
                  echo running; exit 0 ;;
  *" domblklist "*) for a in "$@"; do case "$a" in -*|domblklist|qemu:///system) ;; *) vm="$a" ;; esac; done
                    [ -f "$FIX_VIRSH/$vm.blkfail" ] && { echo "error: failed" >&2; exit 1; }
                    [ -f "$FIX_VIRSH/$vm.blk" ] && exec cat "$FIX_VIRSH/$vm.blk"
                    printf ' Type   Device   Target   Source\n---\n' ; exit 0 ;;
esac
echo "error: unsupported in the stub" >&2
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
        for name, body in (
            ("rpm-ostree", RPM_OSTREE),
            ("systemctl", SYSTEMCTL),
            ("findmnt", FINDMNT),
            ("stat", STAT),
            ("flatpak-spawn", FLATPAK_SPAWN),
            ("virsh", VIRSH),
        ):
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
            "FIX_VIRSH": "",
            "FIX_SHOW": os.path.join(self.tmp, "show"),
        }

    def status(self, name):
        self.env["FIX_STATUS"] = os.path.join(FIX, name)

    def run_tool(self, *args, cmd=None):
        cmd = cmd or ["python3", "-I", TOOL]
        return subprocess.run(cmd + list(args), check=False, env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def assert_read_only(self):
        allowed = (
            "rpm-ostree status --json",
            "rpm-ostree db list ",
            "systemctl list-units --state=failed",
            "systemctl --user list-units --state=failed",
            "flatpak-spawn --host ",
            "findmnt -rn -t btrfs -o SOURCE,TARGET",
            "stat -f -c %S %b ",
            "virsh -r -c qemu:///system list --all --name",
            "virsh -r -c qemu:///system dumpxml --inactive ",
            "virsh -c qemu:///system domcapabilities",
            "virsh -r -c qemu:///system domblklist ",
            "virsh -r -c qemu:///system domstate ",
            "systemctl show -p LoadState ",
            "systemctl --user show -p LoadState ",
            "systemctl --user -M tester@ show -p LoadState ",
        )
        for c in self.calls():
            self.assertTrue(c.startswith(allowed), f"unexpected call: {c}")
            self.assertNotIn("sudo", c)
            self.assertNotIn("--security-info", c)


# ---- names and versions ------------------------------------------------------------------------
class ClassifyTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "db-booted.txt")) as f:
            self.pkgs = [ln.strip() for ln in f if ln.startswith(" ")]

    def test_parse_nevra(self):
        self.assertEqual(D.parse_nevra("samba-2:4.23.2-1.fc44.x86_64"), ("samba", "2", "4.23.2", "1.fc44", "x86_64"))
        self.assertEqual(D.parse_nevra("java-21-openjdk-21.0.9.0.10-1.fc44.x86_64")[0], "java-21-openjdk")
        self.assertIsNone(D.parse_nevra("vim"))
        self.assertIsNone(D.parse_nevra("foo-x:1-2.noarch"))

    def test_real_pinned_requests(self):
        reqs = [
            "below",
            "containerd-2.3.4-1.fc44",
            "docker-compose-switch-1.0.5-10.fc44",
            "tini-static-0.19.0-12.fc44",
            "pigz-2.8-8.fc44",
        ]
        got = D.classify_requests(reqs, self.pkgs)
        self.assertEqual(
            got,
            [
                ("below", "below", "name"),
                ("containerd-2.3.4-1.fc44", "containerd", "pinned"),
                ("docker-compose-switch-1.0.5-10.fc44", "docker-compose-switch", "pinned"),
                ("tini-static-0.19.0-12.fc44", "tini-static", "pinned"),
                ("pigz-2.8-8.fc44", "pigz", "pinned"),
            ],
        )

    def test_names_with_digits_and_dashes_are_not_pins(self):
        reqs = ["python3.13", "gtk4", "java-21-openjdk", "vim", "pkgconfig(gtk4)", "/usr/bin/zsh"]
        for pkgs in (self.pkgs, None):  # with and without the package list
            for r, n, how in D.classify_requests(reqs, pkgs):
                self.assertEqual((n, how), (r, "name"), r)

    def test_other_spellings_of_a_version(self):
        reqs = [
            "samba-2:4.23.2-1.fc44",
            "samba-4.23.2-1.fc44.x86_64",
            "pigz-2.8",
            "NetworkManager-1:1.56.1-2.fc44.x86_64",
        ]
        got = D.classify_requests(reqs, self.pkgs)
        self.assertEqual(
            [(n, how) for _, n, how in got],
            [("samba", "pinned"), ("samba.x86_64", "pinned"), ("pigz", "pinned"), ("NetworkManager.x86_64", "pinned")],
        )

    def test_arch_is_kept(self):
        """A request for a foreign arch (multilib) must come back as name.arch,
        or the fix would swap wine-core.i686 for wine-core.x86_64."""
        both = ["wine-core-9.0-1.fc44.x86_64", "wine-core-9.0-1.fc44.i686", "fonts-1-1.fc44.noarch"]
        got = D.classify_requests(["wine-core-9.0-1.fc44.i686"], both, native="x86_64")
        self.assertEqual(got, [("wine-core-9.0-1.fc44.i686", "wine-core.i686", "pinned")])
        self.assertEqual(
            D.unpin_command(got), "sudo rpm-ostree uninstall wine-core-9.0-1.fc44.i686 --install=wine-core.i686"
        )
        # No arch in the request: the native one is installed, so the bare name.
        got = D.classify_requests(["wine-core-9.0-1.fc44", "fonts-1-1.fc44"], both, native="x86_64")
        self.assertEqual([n for _, n, _ in got], ["wine-core", "fonts"])
        # No arch in the request, but only the foreign one installed.
        got = D.classify_requests(["wine-core-9.0-1.fc44"], both[1:], native="x86_64")
        self.assertEqual(got[0][1], "wine-core.i686")
        # The form fallback (no package list) reads the arch off the string too.
        got = D.classify_requests(["wine-core-9.0-1.fc44.i686", "pigz-2.8-8.fc44"], None, native="x86_64")
        self.assertEqual(
            got,
            [
                ("wine-core-9.0-1.fc44.i686", "wine-core.i686", "pinned-form"),
                ("pigz-2.8-8.fc44", "pigz", "pinned-form"),
            ],
        )

    def test_pin_matching_nothing_installed_is_inactive(self):
        """With the package list read, the form alone does not make a pin to
        install back: pigz may now come from the base image (another build),
        and --install=pigz would make rpm-ostree refuse the whole fix."""
        pkgs = [p for p in self.pkgs if not p.startswith("pigz-")] + ["pigz-2.8-9.fc44.x86_64"]
        got = D.classify_requests(["pigz-2.8-8.fc44", "containerd-2.3.4-1.fc44"], pkgs)
        self.assertEqual(
            got, [("pigz-2.8-8.fc44", "", "inactive"), ("containerd-2.3.4-1.fc44", "containerd", "pinned")]
        )
        self.assertEqual(
            D.unpin_command(got),
            "sudo rpm-ostree uninstall pigz-2.8-8.fc44 containerd-2.3.4-1.fc44 --install=containerd",
        )
        self.assertEqual(D.classify_requests(["foo-1.0-1.fc44"], self.pkgs)[0][2], "inactive")

    def test_form_only_when_package_list_unknown(self):
        got = D.classify_requests(["moby-engine-rootless-extras-29.7.2-1.fc44", "foo-1.0-1"], None)
        self.assertEqual(
            got[0], ("moby-engine-rootless-extras-29.7.2-1.fc44", "moby-engine-rootless-extras", "pinned-form")
        )
        self.assertEqual(got[1][2], "name")  # no distribution tag: not guessed

    def test_unpin_command(self):
        cls = D.classify_requests(
            ["below", "containerd-2.3.4-1.fc44", "samba-2:4.23.2-1.fc44", "pigz-2.8-8.fc44", "pigz"], self.pkgs
        )
        self.assertEqual(
            D.unpin_command(cls),
            "sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 samba-2:4.23.2-1.fc44 "
            "pigz-2.8-8.fc44 --install=containerd --install=samba",
        )
        self.assertEqual(D.unpin_command(D.classify_requests(["below"], self.pkgs)), "")

    def test_pkg_names(self):
        with open(os.path.join(FIX, "overrides.json")) as f:
            d = json.load(f)["deployments"][0]
        self.assertEqual(D.pkg_names(d["base-local-replacements"]), ["mesa-dri-drivers"])
        self.assertEqual(
            D.pkg_names([d["base-removals"], d["requested-base-removals"]]), ["firefox", "firefox-langpacks"]
        )


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
        self.assertIn(
            "fix: sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 docker-cli-29.7.2-1.fc44 "
            "docker-compose-switch-1.0.5-10.fc44 moby-engine-29.7.2-1.fc44 pigz-2.8-8.fc44 "
            "tini-static-0.19.0-12.fc44 --install=containerd --install=docker-cli "
            "--install=docker-compose-switch --install=moby-engine --install=pigz "
            "--install=tini-static",
            r.stdout,
        )
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
        self.assertIn("the staged deployment 44.20261104.0 has no version-pinned requests: reboot to use it", r.stdout)
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
        self.assertIn(
            "WARN   pending deployment 44.20261009.0 (next boot): 1 of 3 layered packages are pinned", r.stdout
        )
        self.assertIn("fix: sudo rpm-ostree uninstall pigz-2.8-8.fc44 --install=pigz\n", r.stdout)
        self.assertEqual(r.stdout.count("fix: sudo rpm-ostree uninstall"), 1)
        self.assertNotIn("WARN   booted", r.stdout)
        self.assertIn("pending deployment 44.20261009.0 (next boot) is not the booted one", r.stdout)
        self.assertNotIn("no pending deployment", r.stdout)

    def test_no_pending_when_booted_is_first(self):
        r = self.run_tool()
        self.assertIn("ok     no pending deployment: the next boot is the booted one", r.stdout)
        self.status("pinned.json")  # booted first, an older rollback after it
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
        self.assertIn(
            "info   booted deployment 44.20261028.0: base packages removed on purpose: firefox, firefox-langpacks",
            r.stdout,
        )

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
        self.assertIn(
            "info   user: 1 transient unit(s) failed (systemd-run, low priority): "
            "run-p1964432-i22912428.scope; to clear: systemctl --user reset-failed",
            r.stdout,
        )
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
            f.write(
                f"cst-alert 1\nurgency={urgency}\napp=btrfs-health\ntitle={title}\n"
                f"first={NOW - age}\nlast={NOW - age}\ncount=1\n\nbody\n"
            )
        os.utime(p, (NOW - age, NOW - age))
        return p

    def test_unseen_alerts(self):
        self.alert("a" * 32 + ".alert", "critical", "btrfs: device missing")
        self.alert("b" * 32 + ".alert")
        self.alert("c" * 32 + ".alert", age=8 * 86400)  # too old
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
        sec, ns = divmod(st.st_mtime_ns, 10**9)
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
GIB = 1024**3
DEV_SECTORS = 1536 * GIB // 512  # 1.5 TiB block devices


class BtrfsTest(Scratch):
    """check_btrfs_allocation on a fixture /sys/fs/btrfs tree (RAID1 over
    two dm-crypt devices unless a test changes it)."""

    def fs(
        self,
        data=(930, 730),
        meta=(20, 14),
        system=(0.04, 0.0002),
        profile="raid1",
        ndev=2,
        sectors=DEV_SECTORS,
        dyn=1,
        per=1,
        missing=0,
        rsv=0.5,
        fs_sectors=None,
    ):
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
        self.assertIn(
            "ok     btrfs /var (RAID1): data chunks 730.0 GiB used of 930.0 GiB allocated (78%), "
            "metadata chunks 14.0 GiB used of 20.0 GiB allocated (70%), metadata is 1.9% of data",
            out,
        )
        self.assertIn("ok     btrfs /var: automatic data reclaim on (dynamic, periodic): 3 chunk(s) reclaimed", out)
        # 1536 - 930 - 20 - 0.04 GiB on each device: one copy of every RAID1 chunk.
        self.assertIn(
            "unallocated per device /dev/mapper/luks-0 586.0 GiB, /dev/mapper/luks-1 586.0 GiB "
            "(warning below 50 GiB, critical below 5 GiB;",
            out,
        )
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
        self.assertIn(
            "info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var "
            "(the hourly btrfs-health-check watches it); not estimated: the btrfs devices use "
            "600.0 GiB less than their block devices (shrunk with btrfs filesystem resize?)",
            r.stdout,
        )
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
        self.assertIn(
            "WARN   btrfs /var: little unallocated space per device: at most /dev/mapper/luks-0 26.0 GiB", r.stdout
        )

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
        self.assertIn(
            "info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var "
            "(the hourly btrfs-health-check watches it); not estimated: a member device is missing",
            r.stdout,
        )

    def test_striped_layout_gives_the_sum_only(self):
        self.fs(profile="raid10", ndev=4, sectors=600 * GIB // 512)
        r = self.run_tool()
        # 4 x 600 - 2 x (930 + 20 + 0.04) GiB over all devices; the average is above 50 GiB.
        self.assertIn(
            "info   btrfs /var: 499.9 GiB unallocated over 4 device(s) (layout RAID10); "
            "per-device unallocated needs root",
            r.stdout,
        )
        self.fs(profile="raid10", ndev=4, sectors=520 * GIB // 512)
        r = self.run_tool()
        self.assertIn(
            "WARN   btrfs /var: 179.9 GiB unallocated over 4 device(s), so at least one device has less than 50 GiB",
            r.stdout,
        )

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


class SnapshotsWritableTest(Scratch):
    """check_snapshots_writable: the lists btrfs-snapshot-core keeps in
    /var/lib/btrfs-snapshot (the snapshot folder itself is root-only)."""

    def snap_state(self, unfinished=None, writable=None):
        d = os.path.join(self.root, "var", "lib", "btrfs-snapshot")
        os.makedirs(d, exist_ok=True)
        for name, lines in (("core-unfinished", unfinished), ("core-writable", writable)):
            if lines is not None:
                with open(os.path.join(d, name), "w") as f:
                    f.write("".join(f"{x}\n" for x in lines))
        return d

    def lines(self, out):
        return [ln for ln in out.splitlines() if "snapshots:" in ln]

    def test_not_set_up_is_silent(self):
        r = self.run_tool()
        self.assertEqual(self.lines(r.stdout), [])

    def test_nothing_left_is_ok(self):
        self.snap_state()
        r = self.run_tool()
        self.assertIn("ok     snapshots: none left writable", r.stdout)
        self.assert_read_only()

    def test_unfinished_warns_with_the_run_as_fix(self):
        self.snap_state(unfinished=["Workspace.20261115T0400"], writable=["Workspace.20261115T0400"])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        got = self.lines(r.stdout)
        self.assertEqual(len(got), 1, r.stdout)  # listed in both: one warning, not two
        self.assertIn("WARN   snapshots: Workspace.20261115T0400 left writable by an interrupted run", got[0])
        self.assertIn("fix: sudo systemctl start btrfs-snapshot.service", r.stdout)
        self.assert_read_only()

    def test_foreign_writable_warns_with_a_read_only_look(self):
        self.snap_state(writable=["home.20260101T0000", "home.20260102T0000"])
        os.makedirs(os.path.join(self.root, "etc", "btrfs-setup"))
        with open(os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf"), "w") as f:
            f.write("TOP=/mnt/top\nSNAP_DIR_NAME='snaps'\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn(
            "WARN   snapshots: home.20260101T0000, home.20260102T0000 in /mnt/top/snaps: writable, "
            "named like CST's snapshots, but not left by a CST run",
            r.stdout,
        )
        self.assertIn("fix: sudo btrfs subvolume show /mnt/top/snaps/home.20260101T0000", r.stdout)

    def test_default_snapshot_folder(self):
        self.snap_state(writable=["home.20260101T0000"])
        r = self.run_tool()
        self.assertIn("fix: sudo btrfs subvolume show /var/mnt/btrfs-top/snapshots/home.20260101T0000", r.stdout)

    def test_unreadable_list_is_info(self):
        d = self.snap_state()
        os.mkdir(os.path.join(d, "core-unfinished"))
        r = self.run_tool()
        self.assertIn("info   snapshots: cannot read /var/lib/btrfs-snapshot/core-unfinished", r.stdout)


# ---- vm ----------------------------------------------------------------------------------------
ROLLBACK = "r" * 64
CPU_FEATURES = ("cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet")
CAPS_QEMU11 = (
    "<domainCapabilities><cpu><mode name='host-passthrough' supported='yes'/>"
    "<mode name='host-model' supported='yes'><model>Arrow</model>"
    + "".join(f"<feature policy='require' name='{n}'/>" for n in CPU_FEATURES + ("invtsc",))
    + "</mode></cpu></domainCapabilities>\n"
)
CAPS_QEMU10 = (
    "<domainCapabilities><cpu><mode name='host-model' supported='yes'>"
    "<feature policy='require' name='invtsc'/></mode></cpu></domainCapabilities>\n"
)


def domain(cpu):
    return f"<domain type='kvm'><name>win</name><vcpu>12</vcpu>{cpu}</domain>\n"


PASSTHROUGH_BARE = domain("<cpu mode='host-passthrough' check='none'/>")
PASSTHROUGH_FIXED = domain(
    "<cpu mode='host-passthrough'>"
    + "".join(f"<feature policy='disable' name='{n}'/>" for n in CPU_FEATURES)
    + "</cpu>"
)


class GameProfileTest(Scratch):
    """The cpu check: drift of the game profile's settings (cpu/game-profile
    records them in /run/cpu-setup/game.intended)."""

    def setUp(self):
        super().setUp()
        self.run_dir = os.path.join(self.root, "run", "cpu-setup")
        os.makedirs(self.run_dir)
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='nobody-here'\n")
        self.cg = os.path.join(self.root, "sys", "fs", "cgroup", "inner", "cpuset.cpus")
        os.makedirs(os.path.dirname(self.cg))
        with open(self.cg, "w") as f:
            f.write("12-23\n")
        # As written by game-profile: printf %q, systemctl show values.
        self.intended = (
            "prop sys machine.slice AllowedCPUs 12-23 12-23\n"
            "prop sys machine.slice CPUWeight 10 10\n"
            "prop sys machine.slice IOWeight 10 10\n"
            "prop sys user.slice MemoryLow 16G 17179869184\n"
            "prop sys user-1000.slice MemoryLow 16G 17179869184\n"
            "prop sys user@1000.service MemoryLow 16G 17179869184\n"
            "prop usr user.slice CPUWeight idle idle\n"
            "prop usr app.slice MemoryZSwapWriteback no ''\n"
            "cgw /sys/fs/cgroup/inner/cpuset.cpus 12-23 12-23\n"
        )
        self.show("sys", "machine.slice", AllowedCPUs="12-23", CPUWeight="10", IOWeight="10")
        for u in ("user.slice", "user-1000.slice", "user@1000.service"):
            self.show("sys", u, MemoryLow="17179869184")
        self.show("usr", "user.slice", CPUWeight="idle")
        self.show("usr", "app.slice", MemoryZSwapWriteback="[not set]")

    def show(self, mgr, unit, load="loaded", **props):
        d = os.path.join(self.tmp, "show", mgr)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, unit), "w") as f:
            f.write(f"LoadState={load}\n" + "".join(f"{k}={v}\n" for k, v in props.items()))

    def profile_on(self, intended=True):
        open(os.path.join(self.run_dir, "game.state"), "w").close()
        if intended:
            with open(os.path.join(self.run_dir, "game.intended"), "w") as f:
                f.write(self.intended)

    def results(self):
        """The check called directly, as root (the user manager is asked with
        -M TARGET_USER@), with the stubs first in PATH."""
        host = D.Host(self.env)
        host.uid = 0
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='tester'\n")
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(D.check_game_profile_drift(host))
        self.assert_read_only()
        return res

    def test_read_intended(self):
        recs = D.read_intended(self.intended + "bogus line\nprop sys 'unclosed\n")
        self.assertEqual(len(recs), 9)
        self.assertEqual(recs[3], ("prop", "sys", "user.slice", "MemoryLow", "16G", "17179869184"))
        self.assertEqual(recs[7][5], "")
        self.assertEqual(recs[8], ("cgw", "/sys/fs/cgroup/inner/cpuset.cpus", "12-23", "12-23"))

    def test_off_is_ok_and_absent_module_is_silent(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(D.check_game_profile_drift(D.Host(self.env)))
            self.assertEqual([(r.status, r.message) for r in res], [(D.OK, "game profile: off")])
            os.remove(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"))
            self.assertEqual(list(D.check_game_profile_drift(D.Host(self.env))), [])
        self.assertEqual(self.calls(), [])

    def test_all_settings_hold(self):
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [D.OK], res)
        self.assertIn("the 8 settings it made still hold", res[0].message)  # empty SHOWN skipped
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER=''\n")  # no target: the doctor's own user manager
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok     game profile: ON, the 8 settings it made still hold", r.stdout)
        self.assert_read_only()

    def test_drift_warns_with_the_restart_fix(self):
        self.profile_on()
        self.show("sys", "machine.slice", AllowedCPUs="12-23", CPUWeight="50", IOWeight="50")
        self.show("sys", "user.slice", MemoryLow="0")
        self.show("usr", "user.slice", CPUWeight="30")
        with open(self.cg, "w") as f:
            f.write("8-23\n")
        res = self.results()
        self.assertEqual(res[0].status, D.WARN)
        m = res[0].message
        self.assertIn("5 of its settings no longer hold", m)
        self.assertIn("machine.slice CPUWeight is 50 (the profile set 10)", m)
        self.assertIn("user.slice MemoryLow is 0 (the profile set 17179869184)", m)
        self.assertIn("user.slice CPUWeight is 30 (the profile set idle)", m)
        self.assertIn("/sys/fs/cgroup/inner/cpuset.cpus is 8-23 (the profile wrote 12-23)", m)
        self.assertEqual(res[0].fix, "sudo systemctl restart cpu-game.service")
        self.assertEqual(len(res), 1)
        # Switched on by hand: a restart would drop the by-hand mark, and the
        # watchdog would then switch the profile off with no game running.
        open(os.path.join(self.run_dir, "manual"), "w").close()
        self.assertEqual(self.results()[0].fix, "cst cpu game off && cst cpu game on")

    def test_whole_run_as_user_asks_the_own_manager(self):
        self.profile_on()
        self.show("usr", "user.slice", CPUWeight="30")
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER=''\n")  # no target: the doctor's own user manager
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            "WARN   game profile: ON, but 1 of its settings no longer hold: "
            "user.slice CPUWeight is 30 (the profile set idle)",
            r.stdout,
        )
        self.assertIn("fix: sudo systemctl restart cpu-game.service", r.stdout)
        self.assertIn("systemctl --user show -p LoadState -p CPUWeight user.slice", self.calls())
        self.assert_read_only()

    def test_unreachable_manager_and_unloaded_unit_are_info(self):
        self.profile_on()
        shutil.rmtree(os.path.join(self.tmp, "show", "usr"))
        self.show("sys", "user@1000.service", load="not-found", MemoryLow="0")
        res = self.results()
        self.assertEqual([r.status for r in res], [D.OK, D.INFO], res)
        self.assertIn("the 6 settings it made still hold", res[0].message)
        self.assertIn("user@1000.service", res[1].message)
        self.assertIn("user.slice", res[1].message)

    def test_other_user_skips_the_user_manager(self):
        self.profile_on()
        host = D.Host(self.env)
        mgr, why = D.user_manager(host)  # TARGET_USER='nobody-here' is not this user
        self.assertIsNone(mgr)
        self.assertIn("the profile is for nobody-here", why)

    def test_leftover_memorylow_dropins_while_off_warn(self):
        rt = os.path.join(self.root, "run", "systemd", "system")
        for u in ("user.slice", "user@1000.service"):
            os.makedirs(os.path.join(rt, f"{u}.d"))
            open(os.path.join(rt, f"{u}.d", "zz-cst-game.conf"), "w").close()
        os.makedirs(os.path.join(rt, "user-1000.slice.d"))
        open(os.path.join(rt, "user-1000.slice.d", "10-other.conf"), "w").close()
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(D.check_game_profile_drift(D.Host(self.env)))
        self.assertEqual([r.status for r in res], [D.WARN], res)
        self.assertIn(
            "/run/systemd/system/user.slice.d/zz-cst-game.conf, "
            "/run/systemd/system/user@1000.service.d/zz-cst-game.conf",
            res[0].message,
        )
        self.assertNotIn("user-1000.slice", res[0].message)
        self.assertEqual(res[0].fix, "sudo /usr/local/sbin/cpu-game-profile off")
        # While the profile is on they belong there: no word about them.
        self.profile_on()
        self.assertNotIn("drop-ins", " ".join(r.message for r in self.results()))

    def test_old_format_state_is_info(self):
        self.profile_on(intended=False)
        res = self.results()
        self.assertEqual([r.status for r in res], [D.INFO])
        self.assertIn("older cpu-game-profile", res[0].message)
        self.assertIn("sudo systemctl restart cpu-game.service", res[0].message)
        self.assertEqual(self.calls(), [])  # nothing asked


class UserManagerBoxesTest(Scratch):
    """The cpu check of the user manager's cgroup tree: top-level slices CST
    does not know (WARN), systemd-run jobs straight in app.slice (info). Reads
    a fixture /sys/fs/cgroup only."""

    def setUp(self):
        super().setUp()
        self.ucg = os.path.join(self.root, "sys", "fs", "cgroup", "user.slice", "user-1000.slice", "user@1000.service")
        for d in (
            "app.slice/app-gnome-firefox-1.scope",
            "app.slice/flatpak-session-helper.service",
            "session.slice",
            "background.slice",
            "user.slice",
            "init.scope",
            "uresourced.service",
        ):
            os.makedirs(os.path.join(self.ucg, d))
        open(os.path.join(self.ucg, "cgroup.procs"), "w").close()

    def cg(self, rel, usage_usec=None):
        d = os.path.join(self.ucg, rel)
        os.makedirs(d, exist_ok=True)
        if usage_usec is not None:
            with open(os.path.join(d, "cpu.stat"), "w") as f:
                f.write(f"usage_usec {usage_usec}\nuser_usec 1\nsystem_usec 1\n")

    def results(self, uid=1000):
        host = D.Host(self.env)
        host.uid = uid
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(D.check_user_manager_boxes(host))
        self.assertEqual(self.calls(), [])  # files only, no command
        return res

    def test_only_known_boxes_is_ok(self):
        res = self.results()
        self.assertEqual(
            [(r.status, r.message) for r in res],
            [(D.OK, "user manager: no slices or systemd-run jobs outside the boxes CST manages")],
        )

    def test_unknown_top_level_slice_warns_with_cpu_time_and_the_name_to_use(self):
        self.cg("lushtext.slice", usage_usec=69_000_123_456)
        self.cg("_cpu.slice")  # cgroupfs name of cpu.slice
        res = self.results()
        self.assertEqual([r.status for r in res], [D.WARN, D.WARN], res)
        m = {r.message.split()[2]: r for r in res}
        lush = m["lushtext.slice"]
        self.assertIn("69,000 CPU-seconds (19.2 CPU-hours) since it was created", lush.message)
        self.assertIn(
            "Name it user-lushtext.slice so it nests under the toolboxes' user.slice, "
            "or start it with --slice=user-lushtext.slice",
            lush.message,
        )
        self.assertEqual(lush.fix, "")  # advice, not a command to paste
        self.assertIn("CPU time unknown", m["cpu.slice"].message)
        self.assertIn("user-cpu.slice", m["cpu.slice"].message)

    def test_systemd_run_jobs_in_app_slice_are_one_info_line(self):
        self.cg("app.slice/run-u7.service", usage_usec=2_500_000)
        self.cg("app.slice/run-r1a2.scope", usage_usec=90_000_000)
        self.cg("app.slice/app-run-not-transient.scope")
        res = self.results()
        self.assertEqual([r.status for r in res], [D.INFO], res)
        self.assertIn("2 systemd-run job(s) run straight in app.slice", res[0].message)
        # Busiest first.
        self.assertIn("run-r1a2.scope (90 CPU-seconds), run-u7.service (2 CPU-seconds)", res[0].message)
        self.assertNotIn("app-run-not-transient", res[0].message)

    def test_jobs_are_sorted_by_cpu_time_not_by_name(self):
        # Name order is the reverse of the CPU order, and one has no cpu.stat.
        self.cg("app.slice/run-a.service", usage_usec=1_000_000)
        self.cg("app.slice/run-b.service")
        self.cg("app.slice/run-c.service", usage_usec=7_000_000)
        res = self.results()
        self.assertIn(
            "run-c.service (7 CPU-seconds), run-a.service (1 CPU-seconds), run-b.service (CPU time unknown)",
            res[0].message,
        )

    def test_many_jobs_are_cut_short(self):
        for i in range(12):
            self.cg(f"app.slice/run-u{i}.service", usage_usec=i * 1_000_000)
        res = self.results()
        self.assertIn("12 systemd-run job(s)", res[0].message)
        self.assertIn("and 7 more", res[0].message)

    def test_exactly_the_list_size_is_not_cut(self):
        for i in range(D.LIST_MAX):
            self.cg(f"app.slice/run-u{i}.service", usage_usec=1_000_000)
        res = self.results()
        self.assertNotIn("more", res[0].message)
        self.assertIn(f"{D.LIST_MAX} systemd-run job(s)", res[0].message)

    def test_a_short_list_has_no_more_suffix(self):
        self.cg("app.slice/run-u1.service", usage_usec=1_000_000)
        res = self.results()
        self.assertNotIn("more", res[0].message)

    def test_cpu_time_keeps_the_fraction_and_shows_hours_from_one_hour(self):
        self.cg("app.slice/run-u1.service", usage_usec=2_600_000)  # 2.6 s, not 2
        self.assertIn("(3 CPU-seconds)", self.results()[0].message)
        self.assertEqual(D.cpu_time(3600.0), "3,600 CPU-seconds (1.0 CPU-hours)")
        self.assertEqual(D.cpu_time(3599.5), "3,600 CPU-seconds")  # rounds, but under one hour

    def test_no_app_slice_folder_is_not_an_error(self):
        shutil.rmtree(os.path.join(self.ucg, "app.slice"))
        res = self.results()
        self.assertEqual([r.status for r in res], [D.OK], res)

    def test_no_user_manager_is_silent(self):
        self.assertEqual(self.results(uid=4242), [])
        # As root: the user from cpu-setup.conf; none recorded, or unknown: nothing.
        self.assertEqual(self.results(uid=0), [])
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='no-such-user-here'\n")
        self.assertEqual(self.results(uid=0), [])

    def test_as_root_it_looks_at_the_target_user(self):
        import pwd

        me = pwd.getpwuid(os.getuid())
        ucg = os.path.join(
            self.root, "sys", "fs", "cgroup", "user.slice", f"user-{me.pw_uid}.slice", f"user@{me.pw_uid}.service"
        )
        os.makedirs(os.path.join(ucg, "builds.slice"))
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write(f"TARGET_USER='{me.pw_name}'\n")
        res = self.results(uid=0)
        self.assertEqual([r.status for r in res], [D.WARN], res)
        self.assertIn("builds.slice", res[0].message)

    def test_whole_run_in_a_toolbox_reads_the_host_tree(self):
        self.cg("lushtext.slice", usage_usec=1_000_000)
        ucg_real = os.path.join(
            self.root, "sys", "fs", "cgroup", "user.slice", f"user-{os.getuid()}.slice", f"user@{os.getuid()}.service"
        )
        if ucg_real != self.ucg:
            shutil.move(self.ucg, ucg_real)
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WARN   user manager: lushtext.slice is a slice of its own", r.stdout)
        self.assert_read_only()


class VmTest(Scratch):
    def setUp(self):
        super().setUp()
        self.virsh = os.path.join(self.tmp, "virsh")
        os.makedirs(self.virsh)
        self.env["FIX_VIRSH"] = self.virsh
        self.libvirt(["win"], {"win": PASSTHROUGH_FIXED}, CAPS_QEMU11)

    def libvirt(self, names, defs, caps):
        with open(os.path.join(self.virsh, "list.txt"), "w") as f:
            f.write("".join(n + "\n" for n in names) + "\n")
        for n, xml in defs.items():
            with open(os.path.join(self.virsh, n + ".xml"), "w") as f:
                f.write(xml)
        caps_file = os.path.join(self.virsh, "caps.xml")
        if caps is None:
            if os.path.exists(caps_file):
                os.unlink(caps_file)
        else:
            with open(caps_file, "w") as f:
                f.write(caps)

    def vm_lines(self, r):
        return [
            ln
            for ln in r.stdout.split("== vm", 1)[-1].splitlines()
            if ln.strip().startswith(("ok", "info", "WARN", "ERROR", "fix"))
        ]

    # -- deployments with and without a virt stack change
    def deployments(self, rollback_pinned=False, booted_qemu="11.1.3-1.fc45", rollback_qemu="10.2.2-1.fc44"):
        base = json.load(open(os.path.join(FIX, "unpinned.json")))
        booted = base["deployments"][0]
        booted["version"] = "45.20261107.0"
        booted["timestamp"] = NOW - 7 * 86400
        rb = dict(
            booted,
            checksum=ROLLBACK,
            booted=False,
            pinned=rollback_pinned,
            version="44.20261028.0",
            timestamp=NOW - 20 * 86400,
        )
        base["deployments"] = [booted, rb]
        path = os.path.join(self.tmp, "virt-status.json")
        with open(path, "w") as f:
            json.dump(base, f)
        self.env["FIX_STATUS"] = path
        for csum, qemu, fc in ((BOOTED, booted_qemu, "fc45"), (ROLLBACK, rollback_qemu, "fc44")):
            with open(os.path.join(self.db, csum), "w") as f:
                f.write(
                    f"ostree commit: {csum[:8]} ({csum})\n vim-9.1-1.{fc}.x86_64\n"
                    f" qemu-kvm-{qemu}.x86_64\n qemu-system-x86-core-{qemu}.x86_64\n"
                    f" libvirt-daemon-12.6.0-2.{fc}.x86_64\n"
                )

    def backup_index(self, epoch):
        d = os.path.join(self.root, "var", "lib", "cst")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "vm-backups.json"), "w") as f:
            json.dump({"version": 1, "backups": [{"vm": "win", "epoch": epoch, "path": "/x", "timestamp": "t"}]}, f)

    def test_fixed_vm_is_ok(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok     VM win: host-passthrough with cet-ss, cet-ibt", r.stdout)
        # domcapabilities needs a read-write connection: asked only when a disable is missing.
        self.assertNotIn("virsh -c qemu:///system domcapabilities", self.calls())
        self.assert_read_only()

    def test_missing_disables_warn_when_qemu_offers_them(self):
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(
            "WARN   VM win: host-passthrough passes cet-ss, cet-ibt, vmx-mbec, vmx-exit-save-cet, "
            "vmx-entry-load-cet to the guest",
            r.stdout,
        )
        self.assertIn("gitlab.com/qemu-project/qemu/-/issues/3215", r.stdout)
        self.assertIn("  fix: cst cpu vm-pin plan", r.stdout)
        self.assertEqual(self.calls().count("virsh -c qemu:///system domcapabilities"), 1)
        self.assert_read_only()

    def test_only_offered_features_count(self):
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, CAPS_QEMU10)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("this QEMU does not offer", r.stdout)

    def test_unreadable_caps_is_info(self):
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, None)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("info   VM win: host-passthrough without disabling", r.stdout)

    def test_other_cpu_modes_ok(self):
        self.libvirt(["win", "lin"], {"win": domain("<cpu mode='host-model'/>"), "lin": domain("")}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("VM win: cpu mode host-model", r.stdout)
        self.assertIn("VM lin: cpu mode not set", r.stdout)

    def test_empty_knob_skips(self):
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("VM_NAME='win'\nVM_CPU_DISABLE=''\n")
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("VM_CPU_DISABLE is empty", r.stdout)

    def test_last_knob_line_wins(self):
        # As in vm-pin's conf() and the shell: a later assignment overrides an earlier one.
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("VM_CPU_DISABLE='cet-ss cet-ibt'\n# VM_CPU_DISABLE='vmx-mbec'\nVM_CPU_DISABLE=''\n")
        self.assertEqual(D.cpu_disable_names(D.Host(self.env)), [])
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("VM_CPU_DISABLE=''\nVM_CPU_DISABLE=\"cet-ss\"\n")
        self.assertEqual(D.cpu_disable_names(D.Host(self.env)), ["cet-ss"])

    def test_knob_from_vm_pin(self):
        host = D.Host(self.env)
        self.assertEqual(D.cpu_disable_names(host), list(CPU_FEATURES))

    def test_no_libvirt_is_info_never_warn(self):
        for env in ({"FIX_VIRSH": ""}, {"PATH": f"{self.bin}:/usr/bin:/bin"}):
            self.env.update(env)
            if "PATH" in env:
                # No virsh: a stub that cannot start (the real one in /usr/bin must not answer).
                with open(os.path.join(self.bin, "virsh"), "w") as f:
                    f.write("#!/bin/sh\necho 'virsh: not found' >&2\nexit 127\n")
            self.deployments()
            r = self.run_tool()
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("  WARN ", r.stdout)
            self.assertIn("libvirt VMs: not checked", r.stdout)
        self.assertIn("no virsh: libvirt is not installed", r.stdout)

    def test_no_domains_is_ok(self):
        self.libvirt([], {}, CAPS_QEMU11)
        self.deployments()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok     libvirt: no VM defined", r.stdout)
        self.assertIn("no libvirt VM defined", r.stdout)

    def test_virt_change_unpinned_no_backup_warns_twice(self):
        self.deployments()
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(
            "virtualization packages changed from the rollback deployment 44.20261028.0 to the booted "
            "one: libvirt-daemon 12.6.0-2.fc44 -> 12.6.0-2.fc45, qemu-kvm 10.2.2-1.fc44 -> "
            "11.1.3-1.fc45, qemu-system-x86-core 10.2.2-1.fc44 -> 11.1.3-1.fc45",
            r.stdout,
        )
        self.assertIn("fix: sudo ostree admin pin 1", r.stdout)
        self.assertIn("  fix: cst vm backup", r.stdout)
        self.assert_read_only()

    def test_virt_change_pinned_and_backed_up_is_info(self):
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 86400)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("rollback pinned and VM backup present", r.stdout)

    def test_backup_older_than_booted_still_warns(self):
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 10 * 86400)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("ostree admin pin", r.stdout)
        self.assertIn("No VM backup since the booted deployment was built", r.stdout)

    def test_pin_index_follows_the_list(self):
        """A staged deployment first: booted is 1, rollback 2."""
        self.deployments()
        data = json.load(open(self.env["FIX_STATUS"]))
        staged = dict(data["deployments"][0], checksum=STAGED, booted=False, staged=True, version="45.2")
        data["deployments"].insert(0, staged)
        with open(self.env["FIX_STATUS"], "w") as f:
            json.dump(data, f)
        r = self.run_tool()
        self.assertIn("fix: sudo ostree admin pin 2", r.stdout)

    def test_unchanged_virt_stack_is_ok(self):
        self.deployments(rollback_qemu="11.1.3-1.fc45")
        with open(os.path.join(self.db, ROLLBACK)) as f:
            text = f.read().replace("libvirt-daemon-12.6.0-2.fc44", "libvirt-daemon-12.6.0-2.fc45")
        with open(os.path.join(self.db, ROLLBACK), "w") as f:
            f.write(text)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok     virtualization packages: the same", r.stdout)

    def test_not_ostree_is_info(self):
        os.unlink(os.path.join(self.root, "run", "ostree-booted"))
        r = self.run_tool()
        self.assertIn("info   virtualization packages: not compared (not an rpm-ostree system)", r.stdout)

    def test_virt_changes_text(self):
        self.assertEqual(D.virt_changes({"a": "1"}, {"a": "1"}), "")
        many = {f"libvirt-daemon-driver-{i}": "1" for i in range(5)}
        self.assertEqual(
            D.virt_changes(many, {}),
            "libvirt-daemon-driver-0 1 -> none, libvirt-daemon-driver-1 "
            "1 -> none, libvirt-daemon-driver-2 1 -> none, and 2 more virtualization package(s)",
        )
        self.assertEqual(
            D.virt_versions(["qemu-kvm-2:11.1.3-1.fc45.x86_64", "vim-9-1.fc45.x86_64"]), {"qemu-kvm": "2:11.1.3-1.fc45"}
        )


class VmNasBackupTest(VmTest):
    """`cst vm backup --nas`: an overlay left behind, the timer, NAS backups in the freshness check."""

    OVERLAY = "/var/lib/libvirt/images/win.qcow2.cst-overlay-20261114T033000Z"

    def on_overlay(self, live=True, persistent=False):
        src = self.OVERLAY if persistent else "/var/lib/libvirt/images/win.qcow2"
        xml = (
            f"<domain type='kvm'><name>win</name><devices><disk type='file' device='disk'>"
            f"<source file='{src}'/><target dev='sda' bus='scsi'/></disk></devices></domain>\n"
        )
        self.libvirt(["win"], {"win": xml}, CAPS_QEMU11)
        with open(os.path.join(self.virsh, "win.blk"), "w") as f:
            f.write(
                " Type   Device   Target   Source\n------\n"
                f" file   disk     sda      {self.OVERLAY if live else '/var/lib/libvirt/images/win.qcow2'}\n"
            )

    BASE = "/var/lib/libvirt/images/win.qcow2"

    def test_overlay_left_behind_warns(self):
        # Running: QEMU on the overlay -> merge it; only the definition -> point it back, never commit.
        for live, persistent in ((True, False), (True, True)):
            self.on_overlay(live, persistent)
            r = self.run_tool()
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(r.stdout.count("an overlay left by `cst vm backup --nas`"), 1, r.stdout)
            self.assertIn(f"WARN   VM win: disk sda is on {self.OVERLAY}", r.stdout)
            self.assertIn(
                "fix: sudo virsh -c qemu:///system blockcommit win sda --active --pivot --wait --verbose", r.stdout
            )
            self.assertIn("virsh blockjob win sda --pivot", r.stdout)
            self.assertIn(f"shows {self.BASE}", r.stdout)
            self.assert_read_only()
        self.on_overlay(False, True)
        r = self.run_tool()
        self.assertEqual(r.stdout.count("an overlay left by `cst vm backup --nas`"), 1, r.stdout)
        self.assertIn(f"WARN   VM win: disk sda: the definition names {self.OVERLAY}", r.stdout)
        self.assertIn(f"the VM runs on {self.BASE} again: the overlay is stale. Do NOT qemu-img commit it", r.stdout)
        self.assertIn("fix: sudo virsh -c qemu:///system edit win", r.stdout)
        self.assertNotIn("blockcommit", r.stdout)
        self.assert_read_only()
        # The live listing is not readable: never "back on the image", only commands that look.
        open(os.path.join(self.virsh, "win.blkfail"), "w").close()
        r = self.run_tool()
        self.assertIn(f"WARN   VM win: disk sda is on {self.OVERLAY}", r.stdout)
        self.assertIn("Do not remove or commit anything yet", r.stdout)
        self.assertIn("fix: sudo virsh -c qemu:///system domblklist win --details", r.stdout)
        self.assertNotIn("fix: sudo virsh -c qemu:///system edit", r.stdout)
        self.assert_read_only()

    def test_overlay_disk_without_target_dev(self):
        # libvirt requires <target dev=...>, but a definition without one must not fail the whole check
        # (a None key made sorted() raise a TypeError): it is named "?" beside the normal disk. The VM
        # runs, and "?" is not in the live listing, so its fix only looks.
        data = "/var/lib/libvirt/images/data.qcow2.cst-overlay-20261114T033000Z"
        for target in ("<target bus='scsi'/>", ""):
            xml = (
                f"<domain type='kvm'><name>win</name><devices>"
                f"<disk type='file' device='disk'><source file='{data}'/>{target}</disk>"
                f"<disk type='file' device='disk'><source file='{self.OVERLAY}'/><target dev='sda' bus='scsi'/></disk>"
                f"</devices></domain>\n"
            )
            self.libvirt(["win"], {"win": xml}, CAPS_QEMU11)
            with open(os.path.join(self.virsh, "win.blk"), "w") as f:
                f.write(f" Type   Device   Target   Source\n------\n file   disk     sda      {self.OVERLAY}\n")
            r = self.run_tool()
            self.assertNotIn("the check itself failed", r.stdout)
            self.assertEqual(r.stdout.count("an overlay left by `cst vm backup --nas`"), 2, r.stdout)
            self.assertIn(f"WARN   VM win: disk sda is on {self.OVERLAY}", r.stdout)
            self.assertIn(f"WARN   VM win: disk ? is on {data}", r.stdout)
            self.assertNotIn("disk None is on", r.stdout)
            self.assertIn("fix: sudo virsh -c qemu:///system domblklist win --details", r.stdout)
            self.assertNotIn("win ?", r.stdout)  # no blockjob or blockcommit command for disk "?"
            self.assert_read_only()

    def test_overlay_of_a_shut_off_vm(self):
        self.on_overlay(True, True)
        with open(os.path.join(self.virsh, "win.state"), "w") as f:
            f.write("shut off\n")
        r = self.run_tool()
        # The files' times are unreadable: only commands that look.
        self.assertIn("cannot tell whether that overlay still holds the guest's last writes", r.stdout)
        self.assertIn(f"fix: sudo ls -l --time-style=full-iso {self.BASE} {self.OVERLAY}", r.stdout)
        images = os.path.join(self.root, "var", "lib", "libvirt", "images")
        os.makedirs(images)
        base, overlay = os.path.join(images, "win.qcow2"), self.root + self.OVERLAY
        for p in (base, overlay):
            open(p, "w").close()
        # Shut off on the overlay, which is newer: merge it offline.
        os.utime(base, ns=(10**18, 10**18))
        os.utime(overlay, ns=(2 * 10**18, 2 * 10**18))
        r = self.run_tool()
        self.assertIn(f"fix: sudo qemu-img commit -p {self.OVERLAY}", r.stdout)
        self.assertIn("it holds the guest's last writes", r.stdout)
        # The image is newer: a merge cut short or a stale overlay; only commands that look.
        os.utime(base, ns=(3 * 10**18, 3 * 10**18))
        r = self.run_tool()
        self.assertIn("cannot tell whether that overlay still holds the guest's last writes", r.stdout)
        self.assertIn(f"fix: sudo ls -l --time-style=full-iso {self.BASE} {self.OVERLAY}", r.stdout)
        self.assertNotIn("fix: sudo qemu-img", r.stdout)
        self.assertNotIn("fix: sudo virsh -c qemu:///system edit", r.stdout)
        self.assert_read_only()

    def test_overlay_during_a_backup_is_info(self):
        self.on_overlay()
        run = os.path.join(self.root, "run", "cst-vm")
        os.makedirs(run)
        os.makedirs(os.path.join(self.root, "proc", "4242"))
        with open(os.path.join(self.root, "proc", "4242", "cmdline"), "wb") as f:
            f.write(b"/usr/bin/python3\0-I\0/usr/local/sbin/cst-vm\0backup\0win\0--nas\0")
        with open(os.path.join(run, "nas-win.lock"), "w") as f:
            f.write("4242\n")
        r = self.run_tool()
        self.assertIn(
            "info   VM win: disk sda on " + self.OVERLAY + ": a NAS backup is in progress (pid 4242)", r.stdout
        )
        self.assertNotIn("WARN   VM win: disk", r.stdout)
        # The pid was reused by another program (a killed run left its lock file): left behind.
        with open(os.path.join(self.root, "proc", "4242", "cmdline"), "wb") as f:
            f.write(b"/usr/bin/bash\0")
        r = self.run_tool()
        self.assertIn("WARN   VM win: disk sda is on", r.stdout)
        # A pid that is gone: left behind again.
        shutil.rmtree(os.path.join(self.root, "proc", "4242"))
        r = self.run_tool()
        self.assertIn("WARN   VM win: disk sda is on", r.stdout)

    def test_no_overlay_is_silent(self):
        r = self.run_tool()
        self.assertNotIn("overlay", r.stdout)

    def test_timer_state_is_info(self):
        r = self.run_tool()
        self.assertIn("info   VM win: weekly NAS backup timer: off (cst vm backup-timer on win turns it on)", r.stdout)
        wants = os.path.join(self.root, "etc", "systemd", "system", "timers.target.wants")
        os.makedirs(wants)
        open(os.path.join(wants, "cst-vm-backup@win.timer"), "w").close()
        r = self.run_tool()
        self.assertIn("info   VM win: weekly NAS backup timer: on", r.stdout)

    def test_nas_backup_counts_and_fix_names_it(self):
        self.deployments(rollback_pinned=True)
        os.makedirs(os.path.join(self.root, "etc", "btrfs-setup"))
        with open(os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf"), "w") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/volume1/restic\n")
        r = self.run_tool()
        self.assertIn("The NAS backup works with the VM running", r.stdout)
        self.assertIn("  fix: cst vm backup --nas", r.stdout)
        d = os.path.join(self.root, "var", "lib", "cst")
        os.makedirs(d)
        with open(os.path.join(d, "vm-backups.json"), "w") as f:
            json.dump(
                {
                    "version": 1,
                    "backups": [
                        {"vm": "win", "epoch": NOW - 86400, "mode": "nas", "snapshot": "abc", "timestamp": "t"}
                    ],
                },
                f,
            )
        r = self.run_tool()
        self.assertIn("rollback pinned and VM backup present", r.stdout)
        self.assertNotIn("No VM backup since", r.stdout)


class VmHardwareTest(VmTest):
    """`cst vm tune` differences and the NOCOW of the VM image folders: info, never WARN."""

    OLD_HW = (
        "<domain type='kvm'><name>win</name><vcpu>12</vcpu><features><hyperv mode='custom'/></features>"
        "<devices><disk type='file' device='disk'><driver name='qemu' type='qcow2'/>"
        "<source file='/v/win.qcow2'/><target dev='sda' bus='sata'/></disk>"
        "<interface type='network'><mac address='52:54:00:00:00:01'/><model type='e1000e'/></interface>"
        "<memballoon model='virtio'/></devices></domain>\n"
    )
    NEW_HW = (
        "<domain type='kvm'><name>lin</name><vcpu>2</vcpu><devices>"
        "<disk type='file' device='disk'><driver name='qemu' type='qcow2' cache='none' io='native' "
        "discard='unmap' detect_zeroes='unmap'/><source file='/v/l.qcow2'/><target dev='vda' bus='virtio'/>"
        "</disk><interface type='network'><mac address='52:54:00:00:00:02'/><model type='virtio'/>"
        "</interface><memballoon model='virtio'/></devices></domain>\n"
    )

    def test_hardware_differences_are_info(self):
        self.libvirt(["win", "lin"], {"win": self.OLD_HW, "lin": self.NEW_HW}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertNotIn("WARN   VM", r.stdout)
        self.assertIn(
            "info   VM win: hardware differs from what cst vm tune recommends: disk sda: cache "
            "(default) -> none; disk sda: io (default) -> native; disk sda: discard (default) -> unmap; "
            "and 4 more. Windows: the virtio drivers go in first (the plan shows how). "
            "See: cst vm tune plan win",
            r.stdout,
        )
        self.assertIn("ok     VM lin: disks and network card as cst vm tune recommends", r.stdout)
        # One dumpxml per VM for both vm checks.
        self.assertEqual(self.calls().count("virsh -r -c qemu:///system dumpxml --inactive win"), 1)
        self.assert_read_only()

    def test_knobs_come_from_the_host(self):
        os.makedirs(os.path.join(self.root, "etc", "cst"))
        with open(os.path.join(self.root, "etc", "cst", "vm.conf"), "w") as f:
            f.write("VM_TUNE_DISK_BUS=no\nVM_TUNE_DISK_DRIVER=no\nVM_TUNE_NIC=no\n")
        self.libvirt(["win"], {"win": self.OLD_HW}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertIn("ok     VM win: disks and network card as cst vm tune recommends", r.stdout)

    def folders(self, flags, btrfs=True, scandir_denied=()):
        """The image folder check in-process, with fake inode flags."""
        mod = D.vm_module()
        real_scandir = os.scandir

        def scandir(p):
            if p in scandir_denied:
                raise PermissionError(13, "Permission denied")
            return real_scandir(p)

        with (
            mock.patch.dict(os.environ, {"HOME": self.tmp}),
            mock.patch.object(mod, "is_btrfs", lambda p: btrfs),
            mock.patch.object(mod, "inode_flags", lambda p: flags.get(p, 0)),
            mock.patch.object(mod.os, "scandir", scandir),
        ):
            return list(D.check_vm_image_dirs(D.Host(self.env)))

    def test_image_folders(self):
        mod = D.vm_module()
        system = os.path.join(self.root, "var", "lib", "libvirt", "images")
        boxes = os.path.join(self.tmp, ".local", "share", "gnome-boxes", "images")
        for d in (system, boxes):
            os.makedirs(d)
        with open(os.path.join(boxes, "fedora"), "wb") as f:
            f.write(b"x" * 8192)
        # Root's folder, not readable: one info line that says so, no finding.
        res = self.folders({system: None}, scandir_denied=(system,))
        self.assertEqual(res[0].status, D.INFO)
        self.assertIn("/var/lib/libvirt/images: NOCOW mark not readable as your user", res[0].message)
        self.assertIn("cst vm nocow plan --root", res[0].message)
        self.assertEqual(
            [r.message.split(":")[0] for r in res[1:]], [f"VM image folder {boxes}", f"VM image folder {boxes}"]
        )
        self.assertIn("no NOCOW mark (+C)", res[1].message)
        self.assertIn("still copy-on-write: fedora", res[2].message)
        self.assertTrue(all(r.status in (D.OK, D.INFO) for r in res))
        # All marked: ok lines only.
        nocow = mod.FS_NOCOW_FL
        res = self.folders({system: nocow, boxes: nocow, os.path.join(boxes, "fedora"): nocow})
        self.assertEqual([r.status for r in res], [D.OK, D.OK])
        # Not btrfs, or no folders at all (CI): silent.
        self.assertEqual(self.folders({}, btrfs=False), [])
        shutil.rmtree(os.path.join(self.root, "var"))
        shutil.rmtree(os.path.join(self.tmp, ".local"))
        self.assertEqual(self.folders({}), [])

    def test_image_folders_knob_off(self):
        os.makedirs(os.path.join(self.root, "etc", "cst"))
        with open(os.path.join(self.root, "etc", "cst", "vm.conf"), "w") as f:
            f.write("VM_NOCOW=no\n")
        res = self.folders({})
        self.assertEqual(
            [(r.status, r.message) for r in res],
            [(D.INFO, "VM image folders: NOCOW not managed (VM_NOCOW=no in /etc/cst/vm.conf)")],
        )

    def test_unloadable_vm_tool_is_info(self):
        with (
            mock.patch.object(D, "vm_module", lambda: None),
            mock.patch.object(D, "libvirt_domains", lambda host: (["win"], "")),
        ):
            res = list(D.check_vm_hardware(D.Host(self.env)))
            self.assertEqual(res[0].status, D.INFO)
            self.assertEqual(list(D.check_vm_image_dirs(D.Host(self.env))), [])


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
            r2 = subprocess.run(["bash", "-n", "-c", fx], check=False, capture_output=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r2.returncode, 0, fx)

    def test_registry_order(self):
        areas = []
        for a, _ in D.CHECKS:
            if a not in areas:
                areas.append(a)
        self.assertEqual(areas, ["rpm-ostree", "services", "alerts", "btrfs", "cpu", "vm"])


class ToolboxTest(Scratch):
    def test_commands_go_through_flatpak_spawn(self):
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        self.status("pinned.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        calls = self.calls()
        spawned = [c for c in calls if c.startswith("flatpak-spawn")]
        direct = [c for c in calls if not c.startswith("flatpak-spawn")]
        self.assertEqual(len(spawned), len(direct))  # each stub call came through flatpak-spawn
        self.assertIn("flatpak-spawn --host rpm-ostree status --json", calls)
        self.assertIn(
            "flatpak-spawn --host systemctl --user list-units --state=failed --no-legend --plain --full --no-pager",
            calls,
        )
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
