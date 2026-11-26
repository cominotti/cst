# SPDX-License-Identifier: MPL-2.0
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

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
import errno
import hashlib
import importlib
import io
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Protocol, Self, TextIO, override
import types
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
DOCTOR_DIR = os.path.dirname(HERE)
TOOL = os.path.join(DOCTOR_DIR, "cst-doctor")
REPO = os.path.dirname(DOCTOR_DIR)
CST = os.path.join(REPO, "cst")
NOW = 1793900000  # 2026-11-05, after every fixture's timestamps
BOOTED, STAGED = "b" * 64, "5" * 64

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from doctor import (
    adapters as doc_adapters,
    cli as doc_cli,
    domain as doc_domain,
    presenter as doc_presenter,
    wiring as doc_wiring,
)
from doctor.application import (
    alerts as doc_alerts,
    btrfs as doc_btrfs,
    cpu as doc_cpu,
    docker_nocow as doc_nocow,
    install as doc_install,
    registry as doc_registry,
    services as doc_services,
    shared as doc_shared,
    swap as doc_swap,
    vm as doc_vm,
)
from btrfs.docker_volumes_nocow import adapters as nocow_adapters, domain as nocow_domain
from btrfs.health_check import domain as hc_domain
from btrfs.snapshot_runtime import domain as snap_domain
from cst_core.adapters import nocow as inode_attrs
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.adapters.zipapp import entry_payload
from cst_core.application.install import InstallRequest, InstallService, Placement
from cst_core.domain.install import INSTALL_MANIFEST, GeneratedKind, Owner
from cst_core.domain.record import ComponentState, InstallRecord
from cst_core.domain.errors import Usage
from cst_core.ports import CommandResult
from doctor.domain import Result
from doctor.ports import DoctorHostPort
from tests.probes import bytes_program, time_bound
from vm.adapters import images as vm_images
from vm.wiring import vm_settings


class _ScandirIter(Protocol):
    def __iter__(self) -> Iterator[os.DirEntry[str]]: ...
    def __next__(self) -> os.DirEntry[str]: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object, /) -> None: ...
    def close(self) -> None: ...


# Each stub appends "<name> <args>" to $STUB_LOG.
RPM_OSTREE = """#!/bin/sh
echo "rpm-ostree $*" >>"$STUB_LOG"
case "$1" in
  status) [ "$2" = --json ] && exec cat "$FIX_STATUS" ;;
  db) [ "$2" = list ] && [ -f "$FIX_DB/$3" ] && exec cat "$FIX_DB/$3" ;;
  kargs) [ -n "$FIX_KARGS" ] && [ -f "$FIX_KARGS" ] && exec cat "$FIX_KARGS" ;;
esac
exit 1
"""
# systemctl show: prints $FIX_SHOW/<sys|usr>/<unit> (K=V lines), or fails
# when that file is missing; a show with no --state= prints
# $FIX_SHOW/<sys|usr>/<unit>.active instead when that file is there (what a
# glob lists without --state=loaded: not the inactive units); everything else
# is the failed-units list.
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >>"$STUB_LOG"
case " $* " in
  *" show -p UnitPath --value "*) mgr=sys; case " $* " in *" --user "*) mgr=usr ;; esac
              [ -f "$FIX_SHOW/$mgr/UnitPath" ] && exec cat "$FIX_SHOW/$mgr/UnitPath"
              echo "Failed to connect to bus" >&2; exit 1 ;;
  *" show "*) for a in "$@"; do last="$a"; done
              mgr=sys; case " $* " in *" --user "*) mgr=usr ;; esac
              case " $* " in *" --state="*) ;; *) [ -f "$FIX_SHOW/$mgr/$last.active" ] && exec cat "$FIX_SHOW/$mgr/$last.active" ;; esac
              [ -f "$FIX_SHOW/$mgr/$last" ] && exec cat "$FIX_SHOW/$mgr/$last"
              echo "Failed to connect to bus" >&2; exit 1 ;;
  *" --user "*) cat "$FIX_USER_FAILED" ;;
  *) cat "$FIX_SYSTEM_FAILED" ;;
esac
"""
FINDMNT = """#!/bin/sh
echo "findmnt $*" >>"$STUB_LOG"
case " $* " in
  *"FSTYPE,OPTIONS"*) [ -n "$FIX_FINDMNT_ALL" ] && [ -f "$FIX_FINDMNT_ALL" ] && exec cat "$FIX_FINDMNT_ALL" ;;
  *"OPTIONS"*) [ -n "$FIX_FINDMNT_OPTS" ] && [ -f "$FIX_FINDMNT_OPTS" ] && exec cat "$FIX_FINDMNT_OPTS" ;;
esac
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
while [ "${1#--env=}" != "$1" ]; do export "${1#--env=}"; shift; done
exec "$@"
"""


class Scratch(unittest.TestCase):
    """A scratch host root, stub commands, and helpers to run the doctor."""

    @override
    def setUp(self) -> None:
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
        self.env: dict[str, str] = {
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
            "FIX_FINDMNT_OPTS": os.path.join(self.tmp, "findmnt-opts.out"),
            "FIX_FINDMNT_ALL": os.path.join(self.tmp, "findmnt-all.out"),
            "FIX_KARGS": os.path.join(self.tmp, "kargs.out"),
            "FIX_STATFS": os.path.join(self.tmp, "statfs.out"),
            "FIX_VIRSH": "",
            "FIX_SHOW": os.path.join(self.tmp, "show"),
        }

    def status(self, name: str) -> None:
        self.env["FIX_STATUS"] = os.path.join(FIX, name)

    def run_tool(self, *args: str, cmd: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        cmd = cmd or ["python3", "-I", TOOL]
        return subprocess.run(
            cmd + list(args),
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

    def assert_read_only(self) -> None:
        allowed = (
            "rpm-ostree status --json",
            "rpm-ostree db list ",
            "rpm-ostree kargs",
            "systemctl list-units --state=failed",
            "systemctl --user list-units --state=failed",
            "flatpak-spawn --host ",
            "findmnt -rn -t btrfs -o SOURCE,TARGET",
            "findmnt -rn -o SOURCE,TARGET,FSTYPE,OPTIONS",
            "stat -f -c %S %b ",
            "virsh -r -c qemu:///system list --all --name",
            "virsh -r -c qemu:///system dumpxml --inactive ",
            "virsh -c qemu:///system domcapabilities",
            "virsh -r -c qemu:///system domblklist ",
            "virsh -r -c qemu:///system domstate ",
            "systemctl show -p LoadState ",
            "systemctl show --state=loaded -p Id -p LoadState -p SourcePath ",
            "systemctl --user show -p LoadState ",
            "systemctl --user -M tester@ show -p LoadState ",
            "systemctl show --state=loaded -p Id -p LoadState -p NeedDaemonReload ",
            "systemctl --user show --state=loaded -p Id -p LoadState -p NeedDaemonReload ",
            "systemctl --user show -p UnitPath --value",
        )
        for c in self.calls():
            self.assertTrue(c.startswith(allowed), f"unexpected call: {c}")
            self.assertNotIn("sudo", c)
            self.assertNotIn("--security-info", c)


def area_lines(stdout: str, area: str) -> list[str]:
    """The lines of one area of the report ("== AREA" up to the blank line that ends it)."""
    return stdout.split(f"== {area}\n", 1)[1].split("\n\n", 1)[0].splitlines()


# ---- names and versions ------------------------------------------------------------------------
class ClassifyTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        with open(os.path.join(FIX, "db-booted.txt")) as f:
            self.pkgs = [ln.strip() for ln in f if ln.startswith(" ")]

    def test_parse_nevra(self) -> None:
        self.assertEqual(
            doc_domain.parse_nevra("samba-2:4.23.2-1.fc44.x86_64"), ("samba", "2", "4.23.2", "1.fc44", "x86_64")
        )
        parsed = doc_domain.parse_nevra("java-21-openjdk-21.0.9.0.10-1.fc44.x86_64")
        assert parsed is not None
        self.assertEqual(parsed[0], "java-21-openjdk")
        self.assertIsNone(doc_domain.parse_nevra("vim"))
        self.assertIsNone(doc_domain.parse_nevra("foo-x:1-2.noarch"))

    def test_real_pinned_requests(self) -> None:
        reqs = [
            "below",
            "containerd-2.3.4-1.fc44",
            "docker-compose-switch-1.0.5-10.fc44",
            "tini-static-0.19.0-12.fc44",
            "pigz-2.8-8.fc44",
        ]
        got = doc_domain.classify_requests(reqs, self.pkgs, native="x86_64")
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

    def test_names_with_digits_and_dashes_are_not_pins(self) -> None:
        reqs = ["python3.13", "gtk4", "java-21-openjdk", "vim", "pkgconfig(gtk4)", "/usr/bin/zsh"]
        for pkgs in (self.pkgs, None):  # with and without the package list
            for r, n, how in doc_domain.classify_requests(reqs, pkgs, native="x86_64"):
                self.assertEqual((n, how), (r, "name"), r)

    def test_other_spellings_of_a_version(self) -> None:
        reqs = [
            "samba-2:4.23.2-1.fc44",
            "samba-4.23.2-1.fc44.x86_64",
            "pigz-2.8",
            "NetworkManager-1:1.56.1-2.fc44.x86_64",
        ]
        got = doc_domain.classify_requests(reqs, self.pkgs, native="x86_64")
        self.assertEqual(
            [(n, how) for _, n, how in got],
            [("samba", "pinned"), ("samba.x86_64", "pinned"), ("pigz", "pinned"), ("NetworkManager.x86_64", "pinned")],
        )

    def test_arch_is_kept(self) -> None:
        """A request for a foreign arch (multilib) must come back as name.arch,
        or the fix would swap wine-core.i686 for wine-core.x86_64."""
        both = ["wine-core-9.0-1.fc44.x86_64", "wine-core-9.0-1.fc44.i686", "fonts-1-1.fc44.noarch"]
        got = doc_domain.classify_requests(["wine-core-9.0-1.fc44.i686"], both, native="x86_64")
        self.assertEqual(got, [("wine-core-9.0-1.fc44.i686", "wine-core.i686", "pinned")])
        self.assertEqual(
            doc_domain.unpin_command(got),
            "sudo rpm-ostree uninstall wine-core-9.0-1.fc44.i686 --install=wine-core.i686",
        )
        # No arch in the request: the native one is installed, so the bare name.
        got = doc_domain.classify_requests(["wine-core-9.0-1.fc44", "fonts-1-1.fc44"], both, native="x86_64")
        self.assertEqual([n for _, n, _ in got], ["wine-core", "fonts"])
        # No arch in the request, but only the foreign one installed.
        got = doc_domain.classify_requests(["wine-core-9.0-1.fc44"], both[1:], native="x86_64")
        self.assertEqual(got[0][1], "wine-core.i686")
        # The form fallback (no package list) reads the arch off the string too.
        got = doc_domain.classify_requests(["wine-core-9.0-1.fc44.i686", "pigz-2.8-8.fc44"], None, native="x86_64")
        self.assertEqual(
            got,
            [
                ("wine-core-9.0-1.fc44.i686", "wine-core.i686", "pinned-form"),
                ("pigz-2.8-8.fc44", "pigz", "pinned-form"),
            ],
        )

    def test_pin_matching_nothing_installed_is_inactive(self) -> None:
        """With the package list read, the form alone does not make a pin to
        install back: pigz may now come from the base image (another build),
        and --install=pigz would make rpm-ostree refuse the whole fix."""
        pkgs = [p for p in self.pkgs if not p.startswith("pigz-")] + ["pigz-2.8-9.fc44.x86_64"]
        got = doc_domain.classify_requests(["pigz-2.8-8.fc44", "containerd-2.3.4-1.fc44"], pkgs, native="x86_64")
        self.assertEqual(
            got, [("pigz-2.8-8.fc44", "", "inactive"), ("containerd-2.3.4-1.fc44", "containerd", "pinned")]
        )
        self.assertEqual(
            doc_domain.unpin_command(got),
            "sudo rpm-ostree uninstall pigz-2.8-8.fc44 containerd-2.3.4-1.fc44 --install=containerd",
        )
        self.assertEqual(doc_domain.classify_requests(["foo-1.0-1.fc44"], self.pkgs, native="x86_64")[0][2], "inactive")

    def test_form_only_when_package_list_unknown(self) -> None:
        got = doc_domain.classify_requests(
            ["moby-engine-rootless-extras-29.7.2-1.fc44", "foo-1.0-1"], None, native="x86_64"
        )
        self.assertEqual(
            got[0], ("moby-engine-rootless-extras-29.7.2-1.fc44", "moby-engine-rootless-extras", "pinned-form")
        )
        self.assertEqual(got[1][2], "name")  # no distribution tag: not guessed

    def test_unpin_command(self) -> None:
        cls = doc_domain.classify_requests(
            ["below", "containerd-2.3.4-1.fc44", "samba-2:4.23.2-1.fc44", "pigz-2.8-8.fc44", "pigz"],
            self.pkgs,
            native="x86_64",
        )
        self.assertEqual(
            doc_domain.unpin_command(cls),
            "sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 samba-2:4.23.2-1.fc44 "
            "pigz-2.8-8.fc44 --install=containerd --install=samba",
        )
        self.assertEqual(
            doc_domain.unpin_command(doc_domain.classify_requests(["below"], self.pkgs, native="x86_64")), ""
        )

    def test_pkg_names(self) -> None:
        with open(os.path.join(FIX, "overrides.json")) as f:
            d = json.load(f)["deployments"][0]
        self.assertEqual(doc_domain.pkg_names(d["base-local-replacements"]), ["mesa-dri-drivers"])
        self.assertEqual(
            doc_domain.pkg_names([d["base-removals"], d["requested-base-removals"]]), ["firefox", "firefox-langpacks"]
        )

    def test_pkg_names_walks_dicts_and_skips_values_that_are_no_package(self) -> None:
        # A dict's values are walked; a number or None inside the lists names no package.
        self.assertEqual(doc_domain.pkg_names({"a": ["vim-9.1-1.fc45.x86_64", 7, None], "b": {"c": 3.5}}), ["vim"])
        self.assertEqual(doc_domain.pkg_names(7), [])


# ---- whole runs ------------------------------------------------------------------------------------
class RpmOstreeTest(Scratch):
    def test_unpinned_is_all_ok(self) -> None:
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("8 layered packages, all requested by name", r.stdout)
        self.assertIn("doctor: all good", r.stdout)
        self.assert_read_only()

    def test_pinned_booted_warns_with_one_fix(self) -> None:
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

    def test_pinned_without_package_list_uses_the_form(self) -> None:
        self.status("pinned.json")
        os.unlink(os.path.join(self.db, BOOTED))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("recognised by their form only", r.stdout)
        self.assertIn("--install=docker-compose-switch", r.stdout)

    def test_staged_clean_only_notes_the_booted(self) -> None:
        self.status("staged-clean.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("info   booted deployment 44.20261028.0: 6 of 10", r.stdout)
        self.assertIn("the staged deployment 44.20261104.0 has no version-pinned requests: reboot to use it", r.stdout)
        self.assertNotIn("by name only", r.stdout)
        self.assertIn("staged deployment 44.20261104.0: 8 layered packages, all requested by name", r.stdout)
        self.assertIn("waits for a reboot", r.stdout)
        self.assertNotIn("fix:", r.stdout)

    def test_staged_pinned_warns_for_the_staged(self) -> None:
        self.status("staged-pinned.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARN   staged deployment 44.20261104.0: 6 of 10", r.stdout)
        self.assertNotIn("booted deployment 44.20261028.0: 6", r.stdout)
        self.assertEqual(r.stdout.count("fix: sudo rpm-ostree uninstall"), 1)
        self.assertIn(f"rpm-ostree db list {STAGED}", "\n".join(self.calls()))

    def test_inactive_pin_in_a_whole_run(self) -> None:
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

    def test_rollback_pending_is_the_next_boot(self) -> None:
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

    def test_no_pending_when_booted_is_first(self) -> None:
        r = self.run_tool()
        self.assertIn("ok     no pending deployment: the next boot is the booted one", r.stdout)
        self.status("pinned.json")  # booted first, an older rollback after it
        r = self.run_tool()
        self.assertIn("no pending deployment", r.stdout)
        self.assertNotIn("44.20261009.0", r.stdout)

    def test_staged_old(self) -> None:
        self.status("staged-old.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"staged deployment 44.20261020.0 waits for a reboot, built .*, 2[0-9] days ago")

    def test_local_packages(self) -> None:
        self.status("local.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 local RPM file(s) layered: mytool-1.2-3.fc44.x86_64", r.stdout)
        self.assertIn("fix: sudo rpm-ostree uninstall mytool-1.2-3.fc44.x86_64 --install=mytool", r.stdout)

    def test_overrides(self) -> None:
        self.status("overrides.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("base packages replaced by another version: mesa-dri-drivers", r.stdout)
        self.assertIn("fix: sudo rpm-ostree override reset mesa-dri-drivers", r.stdout)
        self.assertIn(
            "info   booted deployment 44.20261028.0: base packages removed on purpose: firefox, firefox-langpacks",
            r.stdout,
        )

    def test_not_ostree(self) -> None:
        os.unlink(os.path.join(self.root, "run", "ostree-booted"))
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertIn("not an rpm-ostree system", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("rpm-ostree")])

    def test_rpm_ostree_failure_warns(self) -> None:
        self.env["FIX_STATUS"] = os.path.join(self.tmp, "missing.json")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("layered packages: not checked (rpm-ostree status --json failed", r.stdout)

    def test_a_status_without_a_deployment_warns(self) -> None:
        path = os.path.join(self.tmp, "no-deployments.json")
        with open(path, "w") as f:
            json.dump({"deployments": []}, f)
        self.env["FIX_STATUS"] = path
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "rpm-ostree"),
            [
                "  WARN   layered packages: no deployment in rpm-ostree status",
                "  ok     no pending deployment: the next boot is the booted one",
            ],
        )


class ServicesTest(Scratch):
    def test_failed_units(self) -> None:
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

    def test_only_transient_is_exit_0(self) -> None:
        with open(self.user_failed, "w") as f:
            f.write("run-r1.service loaded failed failed x\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_user_units_skipped_as_root(self) -> None:
        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = 0
        res = list(doc_services.check_user_units(host))
        self.assertEqual(res[0].status, doc_domain.INFO)
        self.assertIn("running as root", res[0].message)


class AlertsTest(Scratch):
    def alert(self, name: str, urgency: str = "normal", title: str = "Disk sda: SMART warning", age: int = 3600) -> str:
        p = os.path.join(self.pending, name)
        with open(p, "w") as f:
            f.write(
                f"cst-alert 1\nurgency={urgency}\napp=btrfs-health\ntitle={title}\n"
                f"first={NOW - age}\nlast={NOW - age}\ncount=1\n\nbody\n"
            )
        os.utime(p, (NOW - age, NOW - age))
        return p

    def reader(self) -> None:
        """cst-pending-alerts installed, as btrfs:alerts installs it."""
        p = os.path.join(self.root, "usr", "local", "bin", "cst-pending-alerts")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)

    def test_unseen_alerts(self) -> None:
        self.reader()
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

    def test_an_unseen_alert_without_its_reader_names_the_install(self) -> None:
        # Kept by cst_core when btrfs-notify is missing: then cst-pending-alerts (same component) is
        # missing too, and "shown at your next login, fix: cst-pending-alerts --list" was false.
        self.alert("a" * 32 + ".alert", "critical", "~/Bulk: nothing moved at boot")
        r = self.run_tool()
        self.assertIn(
            "ERROR  alert you have not seen yet (critical, Thu 05 Nov 16:33): ~/Bulk: nothing moved at boot (no login "
            "shows it while /usr/local/bin/cst-pending-alerts is missing: the fix installs it)\n"
            "         fix: cst btrfs install\n",
            r.stdout,
        )

    def test_seen_alert_is_info(self) -> None:
        p = self.alert("d" * 32 + ".alert")
        st = os.stat(p)
        sec, ns = divmod(st.st_mtime_ns, 10**9)
        os.makedirs(os.path.join(self.state, "cst-alerts"))
        with open(os.path.join(self.state, "cst-alerts", "delivered"), "w") as f:
            f.write(f"{'d' * 32}.alert {sec}.{ns:09d}0 shown 1 {NOW}\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 alert(s) of the last 7 days already shown to you", r.stdout)

    def test_legacy_link(self) -> None:
        os.symlink("cst-alerts", os.path.join(self.root, "var", "lib", "f44-alerts"))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("/var/lib/f44-alerts: link left by the move", r.stdout)
        self.assertIn("fix: cst btrfs install\n", r.stdout)

    def test_no_alert_folder(self) -> None:
        shutil.rmtree(os.path.dirname(self.pending))
        r = self.run_tool()
        self.assertEqual(r.returncode, 0)
        self.assertIn("no alert folder", r.stdout)

    def test_no_pending_alerts_is_ok(self) -> None:
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "alerts"),
            ["  ok     no pending alerts", "  ok     no leftovers of f44-tools in /var/lib"],
        )

    def test_files_that_are_no_pending_alert_are_not_counted(self) -> None:
        # A folder, a dangling link, a file that does not parse and one older than ALERT_KEEP_DAYS, all
        # named like alerts: none is unseen, none is seen.
        os.makedirs(os.path.join(self.pending, "e" * 32 + ".alert"))
        os.symlink(os.path.join(self.pending, "gone.alert"), os.path.join(self.pending, "1" * 32 + ".alert"))
        with open(os.path.join(self.pending, "2" * 32 + ".alert"), "w") as f:
            f.write("garbage\n")
        self.alert("3" * 32 + ".alert", age=8 * 86400)
        r = self.run_tool()
        self.assertIn("  ok     no pending alerts\n", r.stdout)
        self.assertNotIn("alert(s) of the last", r.stdout)

    def test_a_live_link_named_like_an_alert_is_not_counted(self) -> None:
        target = self.alert("f" * 32 + ".alert")
        os.rename(target, os.path.join(self.tmp, "outside.alert"))
        os.symlink(os.path.join(self.tmp, "outside.alert"), target)
        r = self.run_tool()
        self.assertIn("  ok     no pending alerts\n", r.stdout)

    def test_an_alert_whose_lstat_fails_is_skipped(self) -> None:
        self.alert("a" * 32 + ".alert")
        host = doc_adapters.Host(self.env, repo=REPO)
        with mock.patch.object(host, "lstat", side_effect=PermissionError(13, "Permission denied")):
            res = list(doc_alerts.check_pending_alerts(host))
        self.assertEqual(res, [Result(doc_domain.OK, "no pending alerts")])

    def test_a_pending_folder_that_cannot_be_listed_warns(self) -> None:
        self.alert("a" * 32 + ".alert")
        host = doc_adapters.Host(self.env, repo=REPO)
        with mock.patch.object(host, "listdir", side_effect=PermissionError(13, "Permission denied")):
            res = list(doc_alerts.check_pending_alerts(host))
        self.assertEqual(res, [Result(doc_domain.WARN, "pending alerts: not readable (Permission denied)")])

    def test_unseen_and_seen_alerts_together(self) -> None:
        # Both kinds in one folder: the unseen ones oldest first, then the count of the seen ones.
        self.reader()
        self.alert("a" * 32 + ".alert", "critical", "newer", age=600)
        self.alert("b" * 32 + ".alert", "normal", "older", age=7200)
        p = self.alert("d" * 32 + ".alert")
        sec, ns = divmod(os.stat(p).st_mtime_ns, 10**9)
        os.makedirs(os.path.join(self.state, "cst-alerts"))
        with open(os.path.join(self.state, "cst-alerts", "delivered"), "w") as f:
            f.write(f"{'d' * 32}.alert {sec}.{ns:09d}0 shown 1 {NOW}\n")
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "alerts"),
            [
                (
                    "  WARN   alert you have not seen yet (normal, Thu 05 Nov 15:33): older (shown at your next login; "
                    "the fix shows it now, as does cst btrfs status)"
                ),
                "         fix: cst-pending-alerts --list",
                (
                    "  ERROR  alert you have not seen yet (critical, Thu 05 Nov 17:23): newer (shown at your next login; "
                    "the fix shows it now, as does cst btrfs status)"
                ),
                "         fix: cst-pending-alerts --list",
                "  info   1 alert(s) of the last 7 days already shown to you (cst-pending-alerts --list)",
                "  ok     no leftovers of f44-tools in /var/lib",
            ],
        )


UUID = "afed93e2-f4ac-43de-8a48-751b552435c7"
GIB = 1024**3
DEV_SECTORS = 1536 * GIB // 512  # 1.5 TiB block devices


def zipapp(members: dict[str, bytes], method: int = zipfile.ZIP_STORED) -> bytes:
    """A zip archive behind a shebang line, as an installed zipapp: these members, compressed with method."""
    buf = io.BytesIO(b"#!/usr/bin/python3 -I\n")
    buf.seek(0, io.SEEK_END)
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


class BtrfsTest(Scratch):
    """check_btrfs_allocation on a fixture /sys/fs/btrfs tree (RAID1 over
    two dm-crypt devices unless a test changes it)."""

    def fs(
        self,
        data: tuple[float, float] = (930, 730),
        meta: tuple[float, float] = (20, 14),
        system: tuple[float, float] = (0.04, 0.0002),
        profile: str = "raid1",
        ndev: int = 2,
        sectors: int = DEV_SECTORS,
        dyn: int | None = 1,
        per: int = 1,
        missing: int = 0,
        rsv: float = 0.5,
        fs_sectors: int | None = None,
    ) -> str:
        """fs_sectors: the btrfs size of each device (default: its whole
        block device); statfs reports their sum over the data copies."""
        base = os.path.join(self.root, "sys", "fs", "btrfs", UUID)
        os.makedirs(os.path.join(self.root, "sys", "fs", "btrfs", "features"), exist_ok=True)
        copies = {"raid1": 2, "single": 1, "dup": 2, "raid10": 2}[profile]

        def put(rel: str, value: int | str) -> None:
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

    def scrub(
        self, conf: str | None = None, dropin: str | None = None, live: int = 0, need_reload: str | None = None
    ) -> str:
        """The scrub unit installed, snapshots.conf, the drop-in, and devinfo/1's limit now; need_reload:
        what systemctl show says of the unit's NeedDaemonReload (None: systemctl cannot be asked)."""
        self.fs()
        if need_reload is not None:
            os.makedirs(os.path.join(self.env["FIX_SHOW"], "sys"), exist_ok=True)
            with open(os.path.join(self.env["FIX_SHOW"], "sys", "btrfs-scrub-weekly.service"), "w") as f:
                f.write(f"LoadState=loaded\nNeedDaemonReload={need_reload}\n")
        units = os.path.join(self.root, "etc", "systemd", "system")
        os.makedirs(os.path.join(units, "btrfs-scrub-weekly.service.d"))
        open(os.path.join(units, "btrfs-scrub-weekly.service"), "w").close()
        if dropin is not None:
            with open(os.path.join(units, "btrfs-scrub-weekly.service.d", "10-cst-limit.conf"), "w") as f:
                f.write(f"[Service]\nExecStartPre=/usr/sbin/btrfs scrub limit -a -l {dropin} /var\n")
        if conf is not None:
            os.makedirs(os.path.join(self.root, "etc", "btrfs-setup"), exist_ok=True)
            with open(os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf"), "w") as f:
                f.write(conf)
        for devid in ("1", "2"):
            with open(
                os.path.join(self.root, "sys", "fs", "btrfs", UUID, "devinfo", devid, "scrub_speed_max"), "w"
            ) as f:
                f.write(f"{live if devid == '1' else 0}\n")
        r = self.run_tool()
        self.assertEqual(r.stderr, "")
        return r.stdout

    def test_scrub_without_a_limit_is_info(self) -> None:
        out = self.scrub()
        self.assertIn(
            "info   weekly scrub: speed limit none (full speed; SCRUB_LIMIT, btrfs/README.md 4.3); "
            "now in sysfs: no limit on any device",
            out,
        )
        self.assertNotIn("SCRUB_LIMIT of", out)

    def test_scrub_limit_set_and_installed_shows_it_and_the_live_value(self) -> None:
        out = self.scrub(conf="SCRUB_LIMIT='1G'\n", dropin="1G", live=1 << 30)
        self.assertIn(
            f"info   weekly scrub: speed limit 1G per device; now in sysfs: {UUID[:8]} device 1 1.0 GiB/s", out
        )
        self.assertNotIn("WARN   weekly scrub", out)

    def test_a_scrub_unit_systemd_has_not_loaded_as_it_is_on_disk_warns(self) -> None:
        # A scrub-limit set killed after its installer and before its daemon-reload: the drop-in says 1G,
        # systemd still runs the limit it loaded before.
        warn = (
            "WARN   weekly scrub: systemd has not loaded btrfs-scrub-weekly.service as it is on disk (a change "
            "stopped before its daemon-reload), so the scrub runs with the speed limit systemd loaded before"
        )
        for answer, warned in (("yes", True), ("no", False), (None, False)):
            with self.subTest(need_reload=answer):
                self.setUp()
                out = self.scrub(conf="SCRUB_LIMIT=1G\n", dropin="1G", need_reload=answer)
                self.assertEqual(warn in out, warned, out)
                self.assertEqual("fix: sudo systemctl daemon-reload\n" in out, warned, out)
                self.assertIn(
                    "systemctl show -p LoadState -p NeedDaemonReload btrfs-scrub-weekly.service", self.calls()
                )
                self.assert_read_only()

    def test_scrub_limit_not_installed_yet_warns(self) -> None:
        out = self.scrub(conf="SCRUB_LIMIT=500M\n")
        self.assertIn(
            "WARN   weekly scrub: speed limit none in the service but 500M in SCRUB_LIMIT of "
            "/etc/btrfs-setup/snapshots.conf: run `cst btrfs install`",
            out,
        )

    def test_a_scrub_limit_line_ending_in_cr_warns_as_install_refuses_it(self) -> None:
        # Line ends kept, as bash reads them: the CR is part of the value, which install refuses.
        out = self.scrub(conf="SCRUB_LIMIT=1G\r\n", dropin="1G")
        self.assertIn("WARN   /etc/btrfs-setup/snapshots.conf line 1: SCRUB_LIMIT=1G", out)
        self.assertIn("`cst btrfs install` refuses to run until it is fixed", out)

    def test_a_bad_scrub_limit_line_has_the_files_own_line_number(self) -> None:
        out = self.scrub(conf="\n\nSCRUB_LIMIT=$X\n")
        scrub = [ln for ln in out.splitlines() if "`cst btrfs install` refuses to run until it is fixed" in ln]
        self.assertEqual(len(scrub), 1, out)
        self.assertIn("snapshots.conf line 3: SCRUB_LIMIT=$X", scrub[0])

    def scrub_warnings(self, out: str, line: str) -> list[str]:
        """The WARN lines that name this line of snapshots.conf."""
        return [ln for ln in out.splitlines() if ln.lstrip().startswith("WARN") and f"snapshots.conf {line}" in ln]

    def test_a_bad_scrub_limit_line_gets_one_warning_the_scrub_rows(self) -> None:
        # The scrub row and the settings row both warned about the same line.
        out = self.scrub(conf="\n\nSCRUB_LIMIT=$X\nFOO=$Y\n", dropin="1G")
        named = self.scrub_warnings(out, "line 3: SCRUB_LIMIT=$X")
        self.assertEqual(len(named), 1, out)
        self.assertIn("`cst btrfs install` refuses to run until it is fixed", named[0])
        # Another bad line beside it is still the settings row's.
        foo = self.scrub_warnings(out, "line 4: FOO=$Y")
        self.assertEqual(len(foo), 1, out)
        self.assertIn("CST's Python programs take nothing from the line (btrfs-snapshot ignores it too", foo[0])

    def test_every_bad_scrub_limit_line_gets_one_warning(self) -> None:
        # The scrub row named only the first, so with the settings row leaving them to it none may be lost.
        out = self.scrub(conf="SCRUB_LIMIT=$X\nSCRUB_LIMIT=`y`\n", dropin="1G")
        for line in ("line 1: SCRUB_LIMIT=$X", "line 2: SCRUB_LIMIT=`y`"):
            named = self.scrub_warnings(out, line)
            self.assertEqual(len(named), 1, out)
            self.assertIn("`cst btrfs install` refuses to run until it is fixed", named[0])

    def test_a_scrub_limit_line_ending_in_cr_gets_one_warning(self) -> None:
        # The settings row reads the file as text (the CR goes with the line end) and finds no problem there.
        out = self.scrub(conf="SCRUB_LIMIT=1G\r\n", dropin="1G")
        self.assertEqual(len(self.scrub_warnings(out, "line 1: SCRUB_LIMIT=1G")), 1, out)

    def test_the_scrub_rows_warning_says_what_the_settings_row_said(self) -> None:
        # The settings row left the line to the scrub row, whose warning named only the install: that
        # cst vm's NAS backup (and the other commands that read the file) refuses too was lost.
        out = self.scrub(conf="\n\nSCRUB_LIMIT=$X\n", dropin="1G")
        named = self.scrub_warnings(out, "line 3: SCRUB_LIMIT=$X")
        self.assertEqual(len(named), 1, out)
        for words in (
            "`cst btrfs install` refuses to run until it is fixed (btrfs/README.md, 4.3)",
            (
                "CST's Python programs take nothing from the line (btrfs-snapshot ignores it too, with a "
                "warning in its journal), and the cst commands that change the machine with these settings "
                "refuse to run until it is fixed"
            ),
        ):
            self.assertIn(words, named[0])

    def test_a_scrub_limit_line_ending_in_a_cr_is_ignored_by_the_python_programs_too(self) -> None:
        # btrfs-snapshot and btrfs-health-check read the file with its line ends, as bash does: the CR is
        # part of the line, which sets nothing for them either (before, doctor read it in text mode).
        out = self.scrub(conf="SCRUB_LIMIT=1G\r\n", dropin="1G")
        named = self.scrub_warnings(out, "line 1: SCRUB_LIMIT=1G")
        self.assertEqual(len(named), 1, out)
        self.assertIn("CST's Python programs take nothing from the line", named[0])

    def assert_one_scrub_limit_warning(self, conf: str, line: str) -> None:
        """One WARN names the bad SCRUB_LIMIT line of conf, at bash's line number, with both effects."""
        out = self.scrub(conf=conf, dropin="1G")
        named = [ln for ln in out.splitlines() if ln.lstrip().startswith("WARN") and "SCRUB_LIMIT=$X" in ln]
        self.assertEqual(len(named), 1, out)
        self.assertIn(f"snapshots.conf {line}: SCRUB_LIMIT=$X", named[0])
        self.assertIn("`cst btrfs install` refuses to run", named[0])
        self.assertIn("CST's Python programs take nothing from the line", named[0])

    # The two rows read the line ends differently (bash keeps a CR in the line, text mode ends the line
    # there), so the same line had two warnings: with other reasons, or other line numbers.
    def test_a_bad_scrub_limit_line_ending_in_crlf_gets_one_warning(self) -> None:
        self.assert_one_scrub_limit_warning("SCRUB_LIMIT=$X\r\n", "line 1")

    def test_a_bad_scrub_limit_line_after_a_crlf_line_gets_one_warning(self) -> None:
        self.assert_one_scrub_limit_warning("A=1\r\nSCRUB_LIMIT=$X\r\n", "line 2")

    def test_a_bad_scrub_limit_line_after_a_lone_cr_gets_one_warning(self) -> None:
        self.assert_one_scrub_limit_warning("A=1\rB=2\nSCRUB_LIMIT=$X\n", "line 2")

    def test_a_line_behind_a_cr_of_a_scrub_limit_line_is_part_of_it(self) -> None:
        # A lone CR ends no line for bash nor for btrfs-snapshot: A=$Y is part of the SCRUB_LIMIT line,
        # which gets one warning (both effects), and no line 2 is named.
        out = self.scrub(conf="SCRUB_LIMIT=$X\rA=$Y\n", dropin="1G")
        scrub = self.scrub_warnings(out, "line 1: SCRUB_LIMIT=$X")
        self.assertEqual(len(scrub), 1, out)
        self.assertIn("CST's Python programs take nothing from the line", out)  # after the CR on that line
        self.assertEqual(self.scrub_warnings(out, "line 2:"), [], out)

    def test_a_setting_ending_in_a_cr_is_named_as_btrfs_snapshot_ignores_it(self) -> None:
        # NAS_ENABLED=1 with a Windows line end: btrfs-snapshot (which reads the file with its line ends)
        # takes nothing from the line; doctor names it (before, it read the value in text mode and said
        # nothing).
        out = self.scrub(conf="NAS_ENABLED=1\r\n", dropin="1G")
        named = self.scrub_warnings(out, "line 1: NAS_ENABLED=1")
        self.assertEqual(len(named), 1, out)
        self.assertIn("carriage return", named[0])

    def test_a_bad_scrub_limit_line_warns_once(self) -> None:
        out = self.scrub(conf="SCRUB_LIMIT=fast\n", dropin="1G")
        self.assertIn(
            "WARN   /etc/btrfs-setup/snapshots.conf SCRUB_LIMIT='fast' is not a size such as 500M or 1G: "
            "`cst btrfs install` refuses to run until it is fixed",
            out,
        )
        self.assertNotIn("in the service but", out)

    def test_no_scrub_unit_no_scrub_line(self) -> None:
        self.fs()
        r = self.run_tool()
        self.assertNotIn("weekly scrub", r.stdout)

    def test_no_btrfs_is_info(self) -> None:
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("info   allocation: no mounted btrfs filesystem", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("findmnt")])

    def test_healthy_raid1(self) -> None:
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

    def test_low_unallocated_warns(self) -> None:
        self.fs(data=(1490, 1400))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARN   btrfs /var: little unallocated space per device: /dev/mapper/luks-0 26.0 GiB", r.stdout)
        self.assertIn("'No space left' but df shows free space", r.stdout)

    def test_critical_unallocated_is_error_without_a_fix(self) -> None:
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

    def test_shrunk_device_is_not_ok(self) -> None:
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

    def test_shrunk_device_still_warns_with_an_upper_bound(self) -> None:
        # The block sizes give an upper bound: below the levels is below them for sure.
        self.fs(data=(1515.959, 1182), meta=(20, 19.5), fs_sectors=DEV_SECTORS - 100 * GIB // 512)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR  btrfs /var: CRITICAL: unallocated per device at most /dev/mapper/luks-0 1 MiB", r.stdout)

    def test_unreadable_statfs_is_not_ok(self) -> None:
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

    def test_a_statfs_number_of_other_than_ascii_digits_is_not_read(self) -> None:
        # isdigit() takes these: int() refused "²" (a traceback) and read "٣" as 3.
        for statfs in ("4096 \N{SUPERSCRIPT TWO}\n", "\N{ARABIC-INDIC DIGIT THREE} 10\n", f"4096 {'1' * 5000}\n"):
            with self.subTest(statfs=statfs[:12]):
                self.fs()
                with open(self.env["FIX_STATFS"], "w", encoding="utf-8") as f:
                    f.write(statfs)
                r = self.run_tool()
                self.assertNotIn("Traceback", r.stderr)
                self.assertIn("not estimated: the filesystem size could not be read (stat -f /var)", r.stdout)

    def test_read_int_takes_ascii_digits_only(self) -> None:
        host = doc_adapters.Host(self.env, repo=REPO)
        path = os.path.join(self.tmp, "value")
        for text, want in (
            ("42\n", 42),
            ("\N{SUPERSCRIPT TWO}\n", None),
            ("\N{ARABIC-INDIC DIGIT THREE}\n", None),
            ("-1\n", None),
            ("", None),
        ):
            with self.subTest(text=text):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self.assertEqual(doc_btrfs.read_int(host, doc_domain.ViewPath(path)), want)

    def test_statfs_rounding_is_not_a_shrink(self) -> None:
        self.fs()
        with open(self.env["FIX_STATFS"], "w") as f:
            f.write(f"4096 {2 * DEV_SECTORS * 512 // 2 // 4096 - 3}\n")
        r = self.run_tool()
        self.assertIn("ok     btrfs /var: unallocated per device /dev/mapper/luks-0 586.0 GiB", r.stdout)

    def test_the_installed_health_check_s_thresholds_win_over_the_checkout_s(self) -> None:
        # The installed copy is the one that alerts: with both there, its values count.
        hc = os.path.join(self.root, "usr", "local", "sbin")
        os.makedirs(hc)
        with open(os.path.join(hc, "btrfs-health-check"), "w") as f:
            f.write("UNALLOC_MIN_GIB=70\nUNALLOC_CRIT_GIB=9\n")
        self.assertEqual(doc_btrfs.unalloc_thresholds(doc_adapters.Host(self.env, repo=REPO)), (70, 9))

    def test_thresholds_come_from_the_health_check(self) -> None:
        self.assertEqual(doc_btrfs.unalloc_thresholds(doc_adapters.Host(self.env, repo=REPO)), (50, 5))
        # No checkout copy: the installed one decides.
        no_repo = doc_adapters.Host(self.env, repo=os.path.join(self.tmp, "nope"))
        hc = os.path.join(self.root, "usr", "local", "sbin")
        os.makedirs(hc)
        with open(os.path.join(hc, "btrfs-health-check"), "w") as f:
            f.write("UNALLOC_MIN_GIB=600       # x\nUNALLOC_CRIT_GIB=7\n")
        self.assertEqual(doc_btrfs.unalloc_thresholds(no_repo), (600, 7))
        self.fs()
        res = [r for a, r in doc_registry.run_checks(no_repo) if a == "btrfs"]
        self.assertEqual(res[-1].status, doc_domain.WARN, res)
        self.assertIn("warning below 600 GiB, critical below 7 GiB", res[-1].message)
        os.unlink(os.path.join(hc, "btrfs-health-check"))
        self.assertEqual(doc_btrfs.unalloc_thresholds(no_repo), doc_domain.UNALLOC_DEFAULTS)

    def test_an_installed_zipapp_s_thresholds_win_over_the_checkout_s(self) -> None:
        # Once the Python version is installed, the installed copy is a zipapp: the levels module
        # inside it is the one that alerts, so its values count.
        hc = os.path.join(self.root, "usr", "local", "sbin")
        os.makedirs(hc)
        data = zipapp(
            {
                "__main__.py": b"",
                "btrfs/health_check/domain.py": b"UNALLOC_MIN_GIB: Final = 70  # early warning\nUNALLOC_CRIT_GIB: Final = 9\n",
            }
        )
        with open(os.path.join(hc, "btrfs-health-check"), "wb") as f:
            f.write(data)
        self.assertEqual(doc_btrfs.unalloc_thresholds(doc_adapters.Host(self.env, repo=REPO)), (70, 9))
        # What install writes holds the checkout's levels where doctor looks for them (the defaults are
        # set apart, so only a read can give them).
        entry = next(e for e in INSTALL_MANIFEST if e.src == "btrfs/btrfs-health-check")
        with open(os.path.join(hc, "btrfs-health-check"), "wb") as f:
            f.write(entry_payload(REPO, entry))
        no_repo = doc_adapters.Host(self.env, repo=os.path.join(self.tmp, "nope"))
        with mock.patch.object(doc_btrfs, "UNALLOC_DEFAULTS", (1, 1)):
            self.assertEqual(
                doc_btrfs.unalloc_thresholds(no_repo), (hc_domain.UNALLOC_MIN_GIB, hc_domain.UNALLOC_CRIT_GIB)
            )

    def checkout_with_levels(self, warn: int, crit: int) -> str:
        """A checkout whose btrfs/health_check/domain.py holds these levels."""
        repo = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(repo, "btrfs", "health_check"))
        with open(os.path.join(repo, "btrfs", "health_check", "domain.py"), "w", encoding="utf-8") as f:
            f.write(f"UNALLOC_MIN_GIB: Final = {warn}\nUNALLOC_CRIT_GIB: Final = {crit}\n")
        return repo

    def test_without_an_installed_copy_the_checkout_s_levels_count(self) -> None:
        repo = self.checkout_with_levels(61, 6)
        self.assertEqual(doc_btrfs.unalloc_thresholds(doc_adapters.Host(self.env, repo=repo)), (61, 6))

    def test_a_damaged_or_foreign_installed_zipapp_falls_back_to_the_checkout_s_levels(self) -> None:
        # A zip archive at the installed path that zipfile cannot read (damaged, compressed, encrypted,
        # odd flags) is read as unreadable: the checkout's levels count, and the allocation check runs.
        hc = os.path.join(self.root, "usr", "local", "sbin")
        os.makedirs(hc)
        repo = self.checkout_with_levels(61, 6)
        member = "btrfs/health_check/domain.py"

        def archive(content: bytes, method: int) -> bytearray:
            return bytearray(zipapp({member: content}, method))

        def patched(local: dict[int, int], central: dict[int, int], method: int = zipfile.ZIP_STORED) -> bytes:
            # Sets bits (offset: mask) in the member's local header and in its central directory entry.
            data = archive(b"x = 1\n", method)
            lh, cd = data.find(b"PK\x03\x04"), data.rfind(b"PK\x01\x02")
            for at, mask in local.items():
                data[lh + at] |= mask
            for at, mask in central.items():
                data[cd + at] |= mask
            return bytes(data)

        deflated = archive(b"UNALLOC_MIN_GIB: Final = 70\nUNALLOC_CRIT_GIB: Final = 9\n" * 50, zipfile.ZIP_DEFLATED)
        start = deflated.find(b"PK\x03\x04") + 30 + len(member)
        deflated[start : start + 8] = b"\xff" * 8
        name_at = 46  # where the name starts in a central directory entry
        cases = {
            # Readable, but not stored as CST stores its files: refused before any decompressor runs (lzma,
            # bzip2 and zstd raise their own error types on damaged data).
            "deflated, intact": zipapp(
                {member: b"UNALLOC_MIN_GIB: Final = 70\nUNALLOC_CRIT_GIB: Final = 9\n" * 50}, zipfile.ZIP_DEFLATED
            ),
            "deflate data damaged": bytes(deflated),
            "compression method 99": patched({8: 99}, {10: 99}),
            "encrypted": patched({6: 0x01}, {8: 0x01}),
            "patched-data flag": patched({6: 0x20}, {8: 0x20}),
            "UTF-8 flag on a name that is not": patched({}, {9: 0x08, name_at: 0xFF}),
        }
        host = doc_adapters.Host(self.env, repo=repo)
        self.fs()
        for case, data in cases.items():
            with self.subTest(case), mock.patch.object(doc_btrfs, "UNALLOC_DEFAULTS", (1, 1)):
                with open(os.path.join(hc, "btrfs-health-check"), "wb") as f:
                    f.write(data)
                self.assertEqual(doc_btrfs.unalloc_thresholds(host), (61, 6))
                res = [r for a, r in doc_registry.run_checks(host) if a == "btrfs"]
                self.assertNotIn(doc_domain.ERROR, [r.status for r in res], res)
                self.assertIn("warning below 61 GiB, critical below 6 GiB", res[-1].message)

    def test_reclaim_off_on_the_root_warns(self) -> None:
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

    def test_reclaim_off_elsewhere_is_info(self) -> None:
        self.fs(dyn=0, per=0)
        with open(self.env["FIX_FINDMNT"], "w") as f:
            f.write("/dev/dm-0 /mnt/usb\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("info   btrfs /mnt/usb: automatic data reclaim off (dynamic_reclaim, periodic_reclaim)", r.stdout)

    def test_old_kernel_without_reclaim_switches(self) -> None:
        self.fs(dyn=None)
        r = self.run_tool()
        self.assertIn("no automatic reclaim switches (Linux 6.11 or later)", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_missing_device_needs_root(self) -> None:
        self.fs(missing=1)
        r = self.run_tool()
        self.assertIn(
            "info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var "
            "(the hourly btrfs-health-check watches it); not estimated: a member device is missing",
            r.stdout,
        )

    def test_striped_layout_gives_the_sum_only(self) -> None:
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

    def test_single_device_dup_metadata(self) -> None:
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

    def test_toolbox_reads_sysfs_under_the_host_root(self) -> None:
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        self.fs()
        r = self.run_tool()
        self.assertIn("flatpak-spawn --host findmnt -rn -t btrfs -o SOURCE,TARGET", self.calls())
        self.assertIn("flatpak-spawn --host stat -f -c %S %b /var", self.calls())
        self.assertIn("btrfs /var: unallocated per device", r.stdout)
        self.assert_read_only()

    # The allocation lines of one filesystem, whole, for the arms the tests above assert only in part.
    OK_FILL = (
        "  ok     btrfs /var (RAID1): data chunks 730.0 GiB used of 930.0 GiB allocated (78%), metadata chunks "
        "14.0 GiB used of 20.0 GiB allocated (70%), metadata is 1.9% of data"
    )
    OK_RECLAIM = "  ok     btrfs /var: automatic data reclaim on (dynamic, periodic): 3 chunk(s) reclaimed since mount"
    OK_UNALLOC = (
        "  ok     btrfs /var: unallocated per device /dev/mapper/luks-0 586.0 GiB, /dev/mapper/luks-1 586.0 GiB "
        "(warning below 50 GiB, critical below 5 GiB; from sysfs, each btrfs device fills its block device)"
    )

    def test_a_healthy_filesystem_whole(self) -> None:
        self.fs()
        r = self.run_tool()
        self.assertEqual(area_lines(r.stdout, "btrfs"), [self.OK_FILL, self.OK_RECLAIM, self.OK_UNALLOC])

    def test_chunk_usage_not_readable_and_reclaim_errors(self) -> None:
        base = self.fs()
        for kind in ("data", "metadata"):
            os.unlink(os.path.join(base, "allocation", kind, "total_bytes"))
        with open(os.path.join(base, "allocation", "data", "reclaim_errors"), "w") as f:
            f.write("2\n")
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "btrfs"),
            [
                "  info   btrfs /var: chunk usage not readable in sysfs",
                (
                    "  info   btrfs /var: automatic data reclaim on (dynamic, periodic): 3 chunk(s) reclaimed since "
                    "mount, 2 failed (journalctl -k -g BTRFS)"
                ),
                self.OK_UNALLOC,
            ],
        )

    def test_a_statfs_larger_than_the_devices_is_not_estimated(self) -> None:
        self.fs(fs_sectors=DEV_SECTORS + 300 * GIB // 512)
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "btrfs"),
            [
                self.OK_FILL,
                self.OK_RECLAIM,
                (
                    "  info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var (the "
                    "hourly btrfs-health-check watches it); not estimated: the filesystem size from statfs does not "
                    "match its block devices"
                ),
            ],
        )

    def test_reclaim_off_elsewhere_whole(self) -> None:
        self.fs(dyn=0, per=0)
        with open(self.env["FIX_FINDMNT"], "w") as f:
            f.write("/dev/dm-0 /mnt/usb\n")
        r = self.run_tool()
        self.assertIn(
            "  info   btrfs /mnt/usb: automatic data reclaim off (dynamic_reclaim, periodic_reclaim); CST sets it up "
            "only for the root filesystem\n",
            r.stdout,
        )

    def test_a_striped_layout_without_disk_totals_names_only_root(self) -> None:
        base = self.fs(profile="raid10", ndev=4, sectors=600 * GIB // 512)
        for kind in ("data", "metadata", "system"):
            os.unlink(os.path.join(base, "allocation", kind, "disk_total"))
        r = self.run_tool()
        self.assertIn(
            "  info   btrfs /var: per-device unallocated needs root: sudo btrfs filesystem usage -T /var (the "
            "hourly btrfs-health-check watches it)\n",
            r.stdout,
        )

    def test_a_sysfs_that_cannot_be_listed_is_no_filesystem(self) -> None:
        os.makedirs(os.path.join(self.root, "sys", "fs"))
        with open(os.path.join(self.root, "sys", "fs", "btrfs"), "w") as f:
            f.write("not a folder\n")
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "btrfs"),
            ["  info   allocation: no mounted btrfs filesystem (/sys/fs/btrfs has none)"],
        )

    def test_a_filesystem_without_a_device_list(self) -> None:
        # No devices/ folder: no mount point found (named by its uuid), and unallocated not estimated.
        base = self.fs()
        shutil.rmtree(os.path.join(base, "devices"))
        r = self.run_tool()
        self.assertEqual(
            area_lines(r.stdout, "btrfs"),
            [
                self.OK_FILL.replace("btrfs /var", f"btrfs {UUID[:8]}"),
                self.OK_RECLAIM.replace("btrfs /var", f"btrfs {UUID[:8]}"),
                (
                    f"  info   btrfs {UUID[:8]}: per-device unallocated needs root: sudo btrfs filesystem usage -T "
                    "MOUNTPOINT (the hourly btrfs-health-check watches it); not estimated: no device list in sysfs"
                ),
            ],
        )


class BtrfsCompressionDoctorTest(Scratch):
    """check_btrfs_compression checks coherence across fstab, kargs, live mounts, and top-mount."""

    def _setup_compression(
        self,
        *,
        fstab_opt: str | None = "compress=zstd:1",
        kargs_opt: str | None = "compress=zstd:1",
        live_opt: str | None = "compress=zstd:1",
        top_opt: str | None = "compress=zstd:1",
    ) -> None:
        if fstab_opt is not None:
            etc = os.path.join(self.root, "etc")
            os.makedirs(etc, exist_ok=True)
            with open(os.path.join(etc, "fstab"), "w") as f:
                opt = f",{fstab_opt}" if fstab_opt else ""
                f.write(f"UUID={UUID} /var btrfs subvol=var,degraded{opt},noatime 0 0\n")
        if kargs_opt is not None:
            opt = f",{kargs_opt}" if kargs_opt else ""
            with open(self.env["FIX_KARGS"], "w") as f:
                f.write(f"ostree=/abc root=UUID={UUID} rw rootflags=subvol=root,degraded{opt},noatime\n")
        if live_opt is not None:
            opt = f",{live_opt}" if live_opt else ""
            with open(self.env["FIX_FINDMNT_OPTS"], "w") as f:
                f.write(f"/dev/dm-0[/var] /var rw,seclabel{opt},ssd,noatime\n")
            proc = os.path.join(self.root, "proc")
            os.makedirs(proc, exist_ok=True)
            with open(os.path.join(proc, "mounts"), "w") as f:
                f.write(f"/dev/dm-0 /var btrfs rw,seclabel{opt},ssd,noatime 0 0\n")
        if top_opt is not None:
            units = os.path.join(self.root, "etc", "systemd", "system")
            os.makedirs(units, exist_ok=True)
            opt = f",{top_opt}" if top_opt else ""
            with open(os.path.join(units, r"var-mnt-btrfs\x2dtop.mount"), "w") as f:
                f.write(
                    f"[Mount]\nWhat=/dev/disk/by-uuid/{UUID}\nWhere=/var/mnt/btrfs-top\nOptions=subvolid=5{opt},noatime\n"
                )

    def test_coherent_compress_and_compress_force_are_ok(self) -> None:
        self._setup_compression(
            fstab_opt="compress=zstd:1",
            kargs_opt="compress=zstd:1",
            live_opt="compress=zstd:1",
            top_opt="compress=zstd:1",
        )
        r = self.run_tool()
        self.assertIn("  ok     root btrfs compression: coherent (compress=zstd:1)", area_lines(r.stdout, "btrfs"))
        self.assert_read_only()

        self._setup_compression(
            fstab_opt="compress-force=zstd:1",
            kargs_opt="compress-force=zstd:1",
            live_opt="compress-force=zstd:1",
            top_opt="compress-force=zstd:1",
        )
        r = self.run_tool()
        self.assertIn(
            "  ok     root btrfs compression: coherent (compress-force=zstd:1)", area_lines(r.stdout, "btrfs")
        )
        self.assert_read_only()

    def test_drift_warns_with_fix_and_works_in_toolbox(self) -> None:
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        self._setup_compression(
            fstab_opt="compress-force=zstd:1",
            kargs_opt="compress=zstd:1",
            live_opt="compress-force=zstd:1",
            top_opt="compress=zstd:1",
        )
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        btrfs_text = "\n".join(area_lines(r.stdout, "btrfs"))
        self.assertIn("WARN   root btrfs compression: drift across layers:", btrfs_text)
        self.assertIn("fix (on the host): cst btrfs compress-force apply", btrfs_text)
        self.assertIn("flatpak-spawn --host rpm-ostree kargs", self.calls())
        self.assert_read_only()

    def test_toolbox_without_proc_mounts_and_uuid_fstab_entry(self) -> None:
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        etc = os.path.join(self.root, "etc")
        os.makedirs(etc, exist_ok=True)
        with open(os.path.join(etc, "fstab"), "w") as f:
            f.write(f"UUID={UUID} /mnt/subvol-root btrfs subvol=extra,compress=zstd:1 0 0\n")
        with open(self.env["FIX_KARGS"], "w") as f:
            f.write(f"ostree=/abc root=UUID={UUID} rw rootflags=subvol=root,compress-force=zstd:1\n")
        with open(self.env["FIX_FINDMNT_OPTS"], "w") as f:
            f.write(f"/dev/dm-0[/var] /var rw,seclabel,compress-force=zstd:1,ssd {UUID}\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        btrfs_text = "\n".join(area_lines(r.stdout, "btrfs"))
        self.assertIn("WARN   root btrfs compression: drift across layers:", btrfs_text)
        self.assertIn("fstab /mnt/subvol-root=compress=zstd:1", btrfs_text)
        self.assertIn("flatpak-spawn --host findmnt -rn -t btrfs -o SOURCE,TARGET,OPTIONS,UUID", self.calls())
        self.assert_read_only()

    def test_the_root_uuid_is_the_root_filesystems_not_a_separate_home(self) -> None:
        # The UUID was that of the first root-like target findmnt listed: a separate btrfs /var/home
        # listed first gave its UUID, and /var/home's fstab line (no compression) was read as the root's.
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        home_uuid = "0b7c3a52-9d1e-4f60-8a2b-5c4d3e2f1a09"
        etc = os.path.join(self.root, "etc")
        os.makedirs(etc, exist_ok=True)
        with open(os.path.join(etc, "fstab"), "w") as f:
            f.write(
                f"UUID={home_uuid} /var/home btrfs subvol=home,noatime 0 0\n"
                f"UUID={UUID} /var btrfs subvol=var,compress=zstd:1,noatime 0 0\n"
            )
        with open(self.env["FIX_KARGS"], "w") as f:
            f.write(f"ostree=/abc root=UUID={UUID} rw rootflags=subvol=root,compress=zstd:1,noatime\n")
        home = f"/dev/dm-1[/home] /var/home rw,seclabel,ssd,noatime {home_uuid}\n"
        root = (
            f"/dev/dm-0[/root] /sysroot rw,seclabel,compress=zstd:1,ssd,noatime {UUID}\n"
            f"/dev/dm-0[/var] /var rw,seclabel,compress=zstd:1,ssd,noatime {UUID}\n"
        )
        for listed in (home + root, root + home):
            with self.subTest(home_first=listed.startswith(home)):
                with open(self.env["FIX_FINDMNT_OPTS"], "w") as f:
                    f.write(listed)
                r = self.run_tool()
                self.assertIn(
                    "  ok     root btrfs compression: coherent (compress=zstd:1)", area_lines(r.stdout, "btrfs")
                )
                self.assert_read_only()

    def test_no_compression_anywhere_is_info(self) -> None:
        self._setup_compression(fstab_opt=None, kargs_opt=None, live_opt=None, top_opt="")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(
            "  info   root btrfs compression: no compression option set across active layers",
            area_lines(r.stdout, "btrfs"),
        )

    def test_commit_interval_coherence_and_drift(self) -> None:
        self._setup_compression(
            fstab_opt="compress-force=zstd:1,commit=60",
            kargs_opt="compress-force=zstd:1,commit=60",
            live_opt="compress-force=zstd:1,commit=60",
            top_opt="compress-force=zstd:1,commit=60",
        )
        ok_r = self.run_tool()
        self.assertIn("  ok     root btrfs commit interval: coherent (commit=60)", area_lines(ok_r.stdout, "btrfs"))

        self._setup_compression(
            fstab_opt="compress-force=zstd:1,commit=60",
            kargs_opt="compress-force=zstd:1",
            live_opt="compress-force=zstd:1",
            top_opt="compress-force=zstd:1",
        )
        drift_r = self.run_tool()
        self.assertEqual(drift_r.returncode, 1)
        btrfs_text = "\n".join(area_lines(drift_r.stdout, "btrfs"))
        self.assertIn("WARN   root btrfs commit interval: drift across layers:", btrfs_text)
        self.assertIn("fix: cst btrfs commit-interval apply", btrfs_text)

        # A commit= in /etc/fstab in digits the kernel does not read (U+00B2, which int() refuses; U+0663...,
        # which int() reads as 300) is no number: fstab reads as the 30 s default, a drift from the others.
        etc_fstab = os.path.join(self.root, "etc", "fstab")
        arabic_300 = "\N{ARABIC-INDIC DIGIT THREE}\N{ARABIC-INDIC DIGIT ZERO}\N{ARABIC-INDIC DIGIT ZERO}"
        for value in ("\N{SUPERSCRIPT TWO}", arabic_300):
            with self.subTest(value=value):
                self._setup_compression(
                    fstab_opt="compress-force=zstd:1,commit=60",
                    kargs_opt="compress-force=zstd:1,commit=60",
                    live_opt="compress-force=zstd:1,commit=60",
                    top_opt="compress-force=zstd:1,commit=60",
                )
                with open(etc_fstab, "w", encoding="utf-8") as f:
                    f.write(
                        f"UUID={UUID} /var btrfs subvol=var,degraded,compress-force=zstd:1,commit={value},noatime 0 0\n"
                    )
                bad_r = self.run_tool()
                self.assertEqual(bad_r.returncode, 1, bad_r.stdout + bad_r.stderr)
                bad_text = "\n".join(area_lines(bad_r.stdout, "btrfs"))
                self.assertIn("WARN   root btrfs commit interval: drift across layers:", bad_text)
                self.assertIn("fstab /var=commit=30 (default)", bad_text)

        # The kernel reads a commit= with a leading 0 as octal: commit=060 is 48 s, a drift from the others' 60.
        self._setup_compression(
            fstab_opt="compress-force=zstd:1,commit=60",
            kargs_opt="compress-force=zstd:1,commit=60",
            live_opt="compress-force=zstd:1,commit=60",
            top_opt="compress-force=zstd:1,commit=60",
        )
        with open(etc_fstab, "w", encoding="utf-8") as f:
            f.write(f"UUID={UUID} /var btrfs subvol=var,degraded,compress-force=zstd:1,commit=060,noatime 0 0\n")
        octal_r = self.run_tool()
        self.assertEqual(octal_r.returncode, 1, octal_r.stdout + octal_r.stderr)
        octal_text = "\n".join(area_lines(octal_r.stdout, "btrfs"))
        self.assertIn("WARN   root btrfs commit interval: drift across layers:", octal_text)
        self.assertIn("fstab /var=commit=48", octal_text)

        self._setup_compression(fstab_opt="", kargs_opt="", live_opt="", top_opt="")
        r2 = self.run_tool()
        self.assertEqual(r2.returncode, 0, r2.stdout)
        self.assertIn(
            "  info   root btrfs compression: no compression option set across active layers",
            area_lines(r2.stdout, "btrfs"),
        )


class BtrfsCacheDoctorTest(Scratch):
    """check_btrfs_cache: silent when unconfigured; warns on flushoncommit, missing crypttab nofail, or unmounted bind."""

    def _put(self, *parts: str, text: str) -> None:
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _set_findmnt(self, text: str) -> None:
        with open(self.env["FIX_FINDMNT_OPTS"], "w", encoding="utf-8") as fh:
            fh.write(text)

    def _set_findmnt_all(self, text: str) -> None:
        with open(self.env["FIX_FINDMNT_ALL"], "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_cache_mount_health_states(self) -> None:
        self._put("etc", "fstab", text=f"UUID={UUID} / btrfs subvol=root,compress-force=zstd:1,flushoncommit 0 0\n")
        self._put("proc", "mounts", text="/dev/dm-0 /var btrfs rw,compress-force=zstd:1,flushoncommit 0 0\n")
        self._set_findmnt(f"/dev/dm-0 /var compress-force=zstd:1,flushoncommit {UUID}\n")
        silent = self.run_tool()
        self.assertNotIn("btrfs cache:", "\n".join(area_lines(silent.stdout, "btrfs")))

        self._put(
            "etc",
            "fstab",
            text=(
                f"UUID={UUID} / btrfs subvol=root,compress-force=zstd:1,flushoncommit 0 0\n"
                "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,noatime,compress=zstd:1,nofail 0 0\n"
                "/dev/mapper/cache /var/mnt/scratchpad btrfs subvol=scratchpad,noatime,compress=zstd:1,nofail 0 0\n"
                "/var/mnt/cache/danilo /var/home/danilo/.cache none bind,nofail 0 0 "
                "# managed-by: btrfs-cache (CST) user=danilo kind=cache\n"
            ),
        )
        self._put("etc", "crypttab", text="cache UUID=abc /etc/cryptsetup-keys.d/cache.key nofail,discard\n")
        self._set_findmnt(
            f"/dev/dm-0 /var compress-force=zstd:1,flushoncommit {UUID}\n"
            "/dev/mapper/cache /var/mnt/cache noatime,compress=zstd:1,subvol=/cache cache-u\n"
            "/dev/mapper/cache /var/mnt/scratchpad noatime,compress=zstd:1,subvol=/scratchpad cache-u\n"
            "/dev/mapper/cache /var/home/danilo/.cache noatime,compress=zstd:1,subvol=/cache/danilo cache-u\n"
        )
        self._set_findmnt_all(
            "/dev/dm-0 /var btrfs compress-force=zstd:1,flushoncommit\n"
            "/dev/mapper/cache /var/mnt/cache btrfs noatime,compress=zstd:1,subvol=/cache\n"
            "/dev/mapper/cache /var/mnt/scratchpad btrfs noatime,compress=zstd:1,subvol=/scratchpad\n"
            "/dev/mapper/cache /var/home/danilo/.cache btrfs noatime,compress=zstd:1,subvol=/cache/danilo\n"
        )
        ok_r = self.run_tool()
        self.assertIn(
            "  ok     btrfs cache: /var/mnt/cache, /var/mnt/scratchpad mounted",
            "\n".join(area_lines(ok_r.stdout, "btrfs")),
        )

        self._put("etc", "crypttab", text="cache UUID=abc /etc/cryptsetup-keys.d/cache.key discard\n")
        self._set_findmnt(
            f"/dev/dm-0 /var compress-force=zstd:1,flushoncommit {UUID}\n"
            "/dev/mapper/cache /var/mnt/cache noatime,flushoncommit cache-u\n"
            "/dev/mapper/cache /var/mnt/scratchpad noatime cache-u\n"
        )
        self._set_findmnt_all(
            "/dev/dm-0 /var btrfs compress-force=zstd:1,flushoncommit\n"
            "/dev/mapper/cache /var/mnt/cache btrfs noatime,flushoncommit\n"
            "/dev/mapper/cache /var/mnt/scratchpad btrfs noatime\n"
        )
        warn_r = self.run_tool()
        self.assertEqual(warn_r.returncode, 1)
        btrfs_text = "\n".join(area_lines(warn_r.stdout, "btrfs"))
        self.assertIn("WARN   btrfs cache: /var/mnt/cache has 'flushoncommit'", btrfs_text)
        self.assertIn("WARN   btrfs cache: /etc/crypttab entry 'cache' is missing 'nofail'", btrfs_text)

        self._put(
            "etc",
            "fstab",
            text=(
                f"UUID={UUID} / btrfs subvol=root,compress-force=zstd:1,flushoncommit 0 0\n"
                "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,noatime,compress=zstd:1,nofail 0 0 "
                "# managed-by: btrfs-cache (CST) provision=base kind=cache\n"
                "/dev/mapper/cache /var/mnt/scratchpad btrfs subvol=scratchpad,noatime,compress-force=zstd:1,nofail 0 0 "
                "# managed-by: btrfs-cache (CST) provision=base kind=scratchpad\n"
            ),
        )
        self._put("etc", "crypttab", text="cache UUID=abc /etc/cryptsetup-keys.d/cache.key nofail,discard\n")
        self._set_findmnt(
            f"/dev/dm-0 /var compress-force=zstd:1,flushoncommit {UUID}\n"
            "/dev/mapper/cache /var/mnt/cache noatime,compress=zstd:1,subvol=/cache cache-u\n"
            "/dev/mapper/cache /var/mnt/scratchpad noatime,compress-force=zstd:1,subvol=/scratchpad cache-u\n"
        )
        self._set_findmnt_all(
            "/dev/dm-0 /var btrfs compress-force=zstd:1,flushoncommit\n"
            "/dev/mapper/cache /var/mnt/cache btrfs noatime,compress=zstd:1,subvol=/cache\n"
            "/dev/mapper/cache /var/mnt/scratchpad btrfs noatime,compress-force=zstd:1,subvol=/scratchpad\n"
        )
        cf_warn = self.run_tool()
        self.assertEqual(cf_warn.returncode, 1)
        cf_text = "\n".join(area_lines(cf_warn.stdout, "btrfs"))
        self.assertIn("WARN   btrfs cache: /var/mnt/cache is not using 'compress-force=zstd:1'", cf_text)
        self.assertIn("fix: cst btrfs cache provision apply", cf_text)
        self.assertIn("fix: cst btrfs cache attach --user danilo", btrfs_text)

    def test_systemd_behind_on_fstab_warns_with_the_reload(self) -> None:
        # An attach or detach killed between its /etc/fstab write and its daemon-reload (or an edit): a unit
        # systemd loaded from /etc/fstab says NeedDaemonReload=yes, or a bind with its line in /etc/fstab
        # has a unit systemd took from /proc/self/mountinfo.
        line = "/var/mnt/cache/danilo /var/home/danilo/.cache none bind,nofail 0 0 # managed-by: btrfs-cache (CST) user=danilo kind=cache\n"
        self._put(
            "etc", "fstab", text="/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,noatime,nofail 0 0\n" + line
        )
        self._put("etc", "crypttab", text="cache UUID=abc /etc/cryptsetup-keys.d/cache.key nofail,discard\n")
        warn = (
            "WARN   btrfs cache: systemd has not loaded /etc/fstab as it is on disk (an edit, or a "
            "`cst btrfs cache attach` or `detach` that stopped before its daemon-reload), so it keeps the mounts "
            "of the /etc/fstab it loaded before"
        )
        base = "Id=var-mnt-cache.mount\nLoadState=loaded\nSourcePath=/etc/fstab\nNeedDaemonReload={}\nWhere=/var/mnt/cache\n"
        bind = "Id=var-home-danilo-.cache.mount\nLoadState=loaded\nSourcePath={}\nNeedDaemonReload=no\nWhere=/var/home/danilo/.cache\n"
        # active: what a glob without --state=loaded lists (not the inactive units); None: the same as the
        # whole answer. The inactive cases are the base not mounted: a question without --state=loaded
        # does not see its unit.
        for name, answer, active, warned in (
            ("flagged", base.format("yes"), None, True),
            ("mountinfo", base.format("no") + "\n" + bind.format("/proc/self/mountinfo"), None, True),
            ("current", base.format("no") + "\n" + bind.format("/etc/fstab"), None, False),
            ("inactive flagged", base.format("yes"), "", True),
            ("inactive current", base.format("no"), "", False),
            ("none", "", None, False),
            ("no answer", None, None, False),
        ):
            with self.subTest(name):
                show = os.path.join(self.env["FIX_SHOW"], "sys", "*.mount")
                os.makedirs(os.path.dirname(show), exist_ok=True)
                for path, text in ((show, answer), (f"{show}.active", active)):
                    if text is None:
                        if os.path.exists(path):
                            os.unlink(path)
                    else:
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(text)
                out = self.run_tool().stdout
                btrfs_text = "\n".join(area_lines(out, "btrfs"))
                self.assertEqual(warn in btrfs_text, warned, btrfs_text)
                self.assertEqual("fix: sudo systemctl daemon-reload" in btrfs_text, warned, btrfs_text)
                self.assertIn(
                    "systemctl show --state=loaded -p Id -p LoadState -p SourcePath -p NeedDaemonReload -p Where *.mount",
                    self.calls(),
                )

    def test_systemd_behind_on_fstab_is_no_warning_without_the_cache(self) -> None:
        # A btrfs machine without the cache drive (no line, mount or name of the cache's) whose /etc/fstab
        # was edited by hand (a NAS line) and not reloaded: no warning of the cache's, and systemd is not
        # asked. With the cache drive's base line, the same answer warns.
        os.makedirs(os.path.join(self.root, "sys", "fs", "btrfs", UUID, "allocation"), exist_ok=True)
        root_line = f"UUID={UUID} / btrfs subvol=root 0 0\n"
        self._put("etc", "fstab", text=root_line + "nas:/export /var/mnt/nas nfs defaults,nofail 0 0\n")
        show = os.path.join(self.env["FIX_SHOW"], "sys", "*.mount")
        os.makedirs(os.path.dirname(show), exist_ok=True)
        with open(show, "w", encoding="utf-8") as fh:
            fh.write(
                "Id=var-mnt-nas.mount\nLoadState=loaded\nSourcePath=/etc/fstab\nNeedDaemonReload=yes\nWhere=/var/mnt/nas\n"
            )
        btrfs_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
        self.assertNotIn("systemd has not loaded /etc/fstab", btrfs_text)
        self.assertNotIn("fix: sudo systemctl daemon-reload", btrfs_text)
        self.assertNotIn("*.mount", self.calls())
        self._put(
            "etc",
            "fstab",
            text=root_line
            + "nas:/export /var/mnt/nas nfs defaults,nofail 0 0\n"
            + "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,noatime,nofail 0 0\n",
        )
        btrfs_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
        self.assertIn("WARN   btrfs cache: systemd has not loaded /etc/fstab as it is on disk", btrfs_text)
        self.assertIn("fix: sudo systemctl daemon-reload", btrfs_text)

    def test_the_cache_drives_crypttab_line_is_found_through_its_dm_device(self) -> None:
        self._put("etc", "fstab", text="UUID=feed /var/mnt/cache btrfs subvol=cache,noatime,nofail 0 0\n")
        self._put("etc", "crypttab", text="luks-1234 UUID=abc none discard\nluks-abcd UUID=def none discard\n")
        self._put("sys", "class", "block", "dm-3", "dm", "name", text="luks-1234\n")
        for proc, findmnt_all in (
            ("/dev/dm-3 /var/mnt/cache btrfs rw,noatime 0 0\n", ""),
            ("", "/dev/mapper/luks-1234[/cache] /var/mnt/cache btrfs rw,noatime\n"),
        ):
            with self.subTest(proc=proc, findmnt=findmnt_all):
                self._put("proc", "mounts", text=proc)
                self._set_findmnt_all(findmnt_all)
                btrfs_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
                self.assertIn("WARN   btrfs cache: /etc/crypttab entry 'luks-1234' is missing 'nofail'", btrfs_text)
                self.assertNotIn("'luks-abcd'", btrfs_text)

    def test_cache_mount_non_btrfs_via_findmnt_and_unreadable_crypttab(self) -> None:
        self._put(
            "etc",
            "fstab",
            text="/dev/nvme2n1p1 /var/mnt/cache btrfs subvol=cache,noatime,nofail 0 0\n",
        )
        self._set_findmnt_all("/dev/nvme2n1p1 /var/mnt/cache ext4 rw,noatime\n")
        crypttab = os.path.join(self.root, "etc", "crypttab")
        self._put("etc", "crypttab", text="cache UUID=abc /etc/cryptsetup-keys.d/cache.key discard\n")
        if os.geteuid() != 0:
            os.chmod(crypttab, 0o000)
            self.addCleanup(os.chmod, crypttab, 0o600)
        res = self.run_tool()
        self.assertEqual(res.returncode, 1)
        btrfs_text = "\n".join(area_lines(res.stdout, "btrfs"))
        self.assertIn("WARN   btrfs cache: /var/mnt/cache filesystem is ext4, expected btrfs", btrfs_text)
        if os.geteuid() != 0:
            self.assertIn("info   btrfs cache: /etc/crypttab is not readable as this user", btrfs_text)

    def test_cache_conf_ok_corrupt_missing_partuuid_and_fifo(self) -> None:
        partuuid = "12345678-1234-1234-1234-123456789abc"
        by_part = os.path.join(self.root, "dev", "disk", "by-partuuid", partuuid)
        os.makedirs(os.path.dirname(by_part), exist_ok=True)
        with open(by_part, "w", encoding="utf-8") as fh:
            fh.write("")
        self._put(
            "etc",
            "cst",
            "btrfs-cache.conf",
            text=f"CACHE_MAPPER_NAME=cache\nCACHE_PARTUUID={partuuid}\nCACHE_SIZE_BYTES=500107862016\n",
        )
        ok_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
        self.assertIn(
            f"ok     btrfs cache: /etc/cst/btrfs-cache.conf (/dev/disk/by-partuuid/{partuuid}, mapper=cache)", ok_text
        )

        os.remove(by_part)
        miss_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
        self.assertIn(
            f"WARN   btrfs cache: /etc/cst/btrfs-cache.conf names /dev/disk/by-partuuid/{partuuid} which is missing",
            miss_text,
        )

        self._put("etc", "cst", "btrfs-cache.conf", text="CACHE_PARTUUID=BAD\nCACHE_SIZE_BYTES=0\n")
        bad_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
        self.assertIn("WARN   btrfs cache: /etc/cst/btrfs-cache.conf:", bad_text)
        self.assertIn("CACHE_PARTUUID='BAD'", bad_text)
        self.assertNotIn("fix:", bad_text)

        conf_path = os.path.join(self.root, "etc", "cst", "btrfs-cache.conf")
        os.remove(conf_path)
        os.mkfifo(conf_path)
        with time_bound(5):
            fifo_text = "\n".join(area_lines(self.run_tool().stdout, "btrfs"))
        self.assertIn("WARN   btrfs cache: /etc/cst/btrfs-cache.conf is unreadable or not a regular file", fifo_text)


class SnapshotsWritableTest(Scratch):
    """check_snapshots_writable: the lists btrfs-snapshot-core keeps in
    /var/lib/btrfs-snapshot (the snapshot folder itself is root-only)."""

    def snap_state(self, unfinished: list[str] | None = None, writable: list[str] | None = None) -> str:
        d = os.path.join(self.root, "var", "lib", "btrfs-snapshot")
        os.makedirs(d, exist_ok=True)
        for name, lines in (("core-unfinished", unfinished), ("core-writable", writable)):
            if lines is not None:
                with open(os.path.join(d, name), "w") as f:
                    f.write("".join(f"{x}\n" for x in lines))
        return d

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "snapshots:" in ln]

    def test_not_set_up_is_silent(self) -> None:
        r = self.run_tool()
        self.assertEqual(self.lines(r.stdout), [])

    def test_nothing_left_is_ok(self) -> None:
        self.snap_state()
        r = self.run_tool()
        self.assertIn("ok     snapshots: none left writable", r.stdout)
        self.assert_read_only()

    def test_unfinished_warns_with_the_run_as_fix(self) -> None:
        self.snap_state(unfinished=["Workspace.20261115T0400"], writable=["Workspace.20261115T0400"])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        got = self.lines(r.stdout)
        self.assertEqual(len(got), 1, r.stdout)  # listed in both: one warning, not two
        self.assertIn("WARN   snapshots: Workspace.20261115T0400 left writable by an interrupted run", got[0])
        self.assertIn("fix: sudo systemctl start btrfs-snapshot.service", r.stdout)
        self.assert_read_only()

    def test_foreign_writable_warns_with_a_read_only_look(self) -> None:
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

    def test_default_snapshot_folder(self) -> None:
        self.snap_state(writable=["home.20260101T0000"])
        r = self.run_tool()
        self.assertIn("fix: sudo btrfs subvolume show /var/mnt/btrfs-top/snapshots/home.20260101T0000", r.stdout)

    def test_unreadable_list_is_info(self) -> None:
        d = self.snap_state()
        os.mkdir(os.path.join(d, "core-unfinished"))
        r = self.run_tool()
        self.assertIn("info   snapshots: cannot read /var/lib/btrfs-snapshot/core-unfinished", r.stdout)


class ZswapParamsTest(Scratch):
    """check_zswap_params: a zswap parameter that swap's zswap.conf (as
    installed) sets and this kernel does not have. systemd-tmpfiles skips
    it at boot without a word."""

    def install_conf(self) -> None:
        d = os.path.join(self.root, "etc", "tmpfiles.d")
        os.makedirs(d, exist_ok=True)
        shutil.copy(os.path.join(REPO, "swap", "tmpfiles", "zswap.conf"), os.path.join(d, "zswap.conf"))

    def params(self, *names: str) -> None:
        d = os.path.join(self.root, "sys", "module", "zswap", "parameters")
        os.makedirs(d, exist_ok=True)
        for n in names:
            with open(os.path.join(d, n), "w") as f:
                f.write("Y\n")

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "zswap" in ln]

    def test_nothing_without_the_installed_file(self) -> None:
        self.params("enabled")
        r = self.run_tool()
        self.assertEqual(self.lines(r.stdout), [])

    def test_ok_when_every_parameter_is_there(self) -> None:
        self.install_conf()
        self.params("compressor", "max_pool_percent", "shrinker_enabled", "enabled", "accept_threshold_percent")
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            ["  ok     zswap: every parameter /etc/tmpfiles.d/zswap.conf sets is in this kernel"],
        )
        self.assert_read_only()

    def test_a_parameter_this_kernel_lacks_warns(self) -> None:
        self.install_conf()
        self.params("compressor", "max_pool_percent", "enabled")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   zswap shrinker_enabled: this kernel has no such parameter; /etc/tmpfiles.d/zswap.conf "
                    "sets it, and systemd-tmpfiles skips it at boot without a word (cst swap status shows the zswap "
                    "settings)"
                )
            ],
        )
        self.assert_read_only()

    def test_no_zswap_in_the_kernel_warns_once(self) -> None:
        self.install_conf()
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   zswap: not in this kernel (no /sys/module/zswap/parameters): "
                    "/etc/tmpfiles.d/zswap.conf sets nothing"
                )
            ],
        )


class MmResponsivenessDoctorTest(Scratch):
    """check_mm_responsiveness_params: sysfs knobs cst-mm-responsiveness.conf sets."""

    def install_conf(self) -> None:
        d = os.path.join(self.root, "etc", "tmpfiles.d")
        os.makedirs(d, exist_ok=True)
        shutil.copy(
            os.path.join(REPO, "swap", "tmpfiles", "cst-mm-responsiveness.conf"),
            os.path.join(d, "cst-mm-responsiveness.conf"),
        )

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "mm responsiveness" in ln]

    def test_nothing_without_file_ok_when_present_warn_when_missing(self) -> None:
        self.assertEqual(self.lines(self.run_tool().stdout), [])
        self.install_conf()
        defrag = os.path.join(self.root, "sys", "kernel", "mm", "transparent_hugepage", "defrag")
        os.makedirs(os.path.dirname(defrag), exist_ok=True)
        with open(defrag, "w", encoding="utf-8") as f:
            f.write("defer\n")
        r_missing = self.run_tool()
        self.assertEqual(r_missing.returncode, 1, r_missing.stdout)
        self.assertEqual(
            self.lines(r_missing.stdout),
            [
                (
                    "  WARN   mm responsiveness /sys/kernel/mm/lru_gen/min_ttl_ms: this kernel lacks this knob; "
                    "/etc/tmpfiles.d/cst-mm-responsiveness.conf skips it at boot without a word"
                )
            ],
        )
        min_ttl = os.path.join(self.root, "sys", "kernel", "mm", "lru_gen", "min_ttl_ms")
        os.makedirs(os.path.dirname(min_ttl), exist_ok=True)
        with open(min_ttl, "w", encoding="utf-8") as f:
            f.write("1000\n")
        r_ok = self.run_tool()
        self.assertEqual(r_ok.returncode, 0, r_ok.stdout)
        self.assertEqual(
            self.lines(r_ok.stdout),
            [
                "  ok     mm responsiveness: every sysfs knob /etc/tmpfiles.d/cst-mm-responsiveness.conf sets is in this kernel"
            ],
        )


# What an older CST's apply left in /etc/fstab and /etc/crypttab (swap's marker), and doctor's rows.
SWAP_PU = "612551b1-ca02-4514-86ad-7d8902e612dc"
SWAP_FSTAB = (
    "UUID=x / btrfs defaults 0 0\n"
    "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: swap-setup.sh (CST)\n"
)
SWAP_CRYPTTAB = (
    f"swap0 /dev/disk/by-partuuid/{SWAP_PU} /dev/urandom"
    " swap,cipher=aes-xts-plain64,size=512,sector-size=4096,discard # managed-by: swap-setup.sh (CST)\n"
)
NOT_CONFIGURED = (
    "  WARN   swap partition: not configured (no SWAP_PARTUUID in /etc/cst/swap.conf): cst swap apply"
    " refuses (swap/README.md, section 2.3)"
)
UNREADABLE = (
    "  info   swap partition: cannot read /etc/cst/swap.conf as this user: on the host, cst swap status --root reads it"
)


class SwapSettingsDoctorTest(Scratch):
    """check_swap_settings: the partition /etc/cst/swap.conf names (cst swap apply refuses without
    one), or that an older CST set swap up and apply writes the file."""

    def write(self, rel: str, text: str) -> None:
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "swap partition" in ln]

    def close(self, rel: str) -> None:
        """Mode 000 on a file or folder of the scratch root (put back at the end, so it can be removed)."""
        p = os.path.join(self.root, rel)
        mode = stat.S_IMODE(os.lstat(p).st_mode)
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, mode)

    def not_as_root(self) -> None:
        # Root opens a file of mode 000 and enters a folder of mode 000: the user's view needs a user.
        if os.geteuid() == 0:
            self.skipTest("not as root")

    def test_nothing_where_swap_is_not_set_up(self) -> None:
        self.assertEqual(self.lines(self.run_tool().stdout), [])

    def test_ok_with_a_partition(self) -> None:
        self.write("etc/cst/swap.conf", "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4096\n")
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout), ["  ok     swap partition: 0a1b2c3d-02 (4096 bytes, /etc/cst/swap.conf)"]
        )
        self.assert_read_only()

    def test_a_problem_warns(self) -> None:
        self.write("etc/cst/swap.conf", "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4 GiB\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   swap partition: /etc/cst/swap.conf SWAP_SIZE_BYTES=4 GiB: the partition's exact size in bytes"
                    " (blockdev --getsize64 /dev/disk/by-partuuid/...): cst swap apply refuses until it is fixed"
                )
            ],
        )

    def test_an_older_cst_set_it_up(self) -> None:
        # As the user: /etc/crypttab (root's, 0600) cannot be read, and the marked fstab line tells.
        self.not_as_root()
        self.write("etc/fstab", SWAP_FSTAB)
        self.write("etc/crypttab", SWAP_CRYPTTAB)
        self.close("etc/crypttab")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   swap partition: /etc/cst/swap.conf not written yet; an older CST set up swap0, and"
                    " `cst swap apply` writes it from /etc/crypttab"
                )
            ],
        )
        self.assertIn("swap apply", r.stdout.split("swap partition:", 1)[1])
        self.assert_read_only()

    def test_a_marked_fstab_line_without_a_crypttab_is_not_configured(self) -> None:
        # cst swap apply refuses there (nothing to write the file from): no "apply writes it" row.
        self.write("etc/fstab", SWAP_FSTAB)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.lines(r.stdout), [NOT_CONFIGURED])

    def test_a_readable_crypttab_without_its_line_is_not_configured(self) -> None:
        self.write("etc/fstab", SWAP_FSTAB)
        self.write("etc/crypttab", "luks-x UUID=x none luks\n")
        self.assertEqual(self.lines(self.run_tool().stdout), [NOT_CONFIGURED])

    def test_a_crypttab_line_whose_partition_is_not_here_is_not_configured(self) -> None:
        # The by-partuuid link leads to a regular file, not a block device.
        self.write("etc/fstab", SWAP_FSTAB)
        self.write("etc/crypttab", SWAP_CRYPTTAB)
        self.write("dev/nvme0n1p5", "")
        os.makedirs(os.path.join(self.root, "dev/disk/by-partuuid"))
        os.symlink("../../nvme0n1p5", os.path.join(self.root, "dev/disk/by-partuuid", SWAP_PU))
        self.assertEqual(
            self.lines(self.run_tool().stdout),
            [
                NOT_CONFIGURED.replace(
                    "swap.conf)",
                    f"swap.conf; the swap0 line in crypttab names /dev/disk/by-partuuid/{SWAP_PU}, which is not here)",
                )
            ],
        )

    def partition_here(self) -> doc_adapters.Host:
        """An older CST's marked lines, and the by-partuuid link of its crypttab line leading to a block
        device of 4 GiB (sysfs: 8388608 sectors): a Host whose stat says so (a test cannot make a block
        device)."""
        self.write("etc/fstab", SWAP_FSTAB)
        self.write("etc/crypttab", SWAP_CRYPTTAB)
        self.write("dev/nvme0n1p5", "")
        self.write("sys/class/block/nvme0n1p5/size", "8388608\n")
        os.makedirs(os.path.join(self.root, "dev/disk/by-partuuid"))
        os.symlink("../../nvme0n1p5", os.path.join(self.root, "dev/disk/by-partuuid", SWAP_PU))
        host = doc_adapters.Host(dict(self.env))
        device = os.path.join(self.root, "dev/nvme0n1p5")

        def stat_of(path: str) -> os.stat_result:
            st = os.stat(path)
            return os.stat_result((stat.S_IFBLK | 0o660, *tuple(st)[1:])) if path == device else st

        patcher = mock.patch.object(host, "stat", stat_of)
        patcher.start()
        self.addCleanup(patcher.stop)
        return host

    def test_a_crypttab_line_whose_partition_is_here_is_written_by_apply(self) -> None:
        # cst swap plan shows "write" there: apply writes swap.conf from the line and the size.
        host = self.partition_here()
        self.assertEqual(
            list(doc_swap.check_swap_settings(host)),
            [
                Result(
                    "WARN",
                    "swap partition: /etc/cst/swap.conf not written yet; an older CST set up swap0, and"
                    " `cst swap apply` writes it from /etc/crypttab",
                    "cst swap apply",
                )
            ],
        )

    def test_a_crypttab_line_whose_partition_sysfs_does_not_size_is_not_configured(self) -> None:
        host = self.partition_here()
        self.write("sys/class/block/nvme0n1p5/size", "\n")
        self.assertEqual(
            list(doc_swap.check_swap_settings(host)),
            [
                Result(
                    "WARN",
                    "swap partition: not configured (no SWAP_PARTUUID in /etc/cst/swap.conf; the swap0 line in"
                    f" crypttab names /dev/disk/by-partuuid/{SWAP_PU}, which is not here): cst swap apply refuses"
                    " (swap/README.md, section 2.3)",
                )
            ],
        )

    def test_a_swap_conf_this_user_cannot_read(self) -> None:
        # cst swap plan shows "?" and asks for --root: not "not configured".
        self.not_as_root()
        self.write("etc/cst/swap.conf", "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4096\n")
        self.write("etc/tmpfiles.d/zswap.conf", "")
        self.close("etc/cst/swap.conf")
        self.assertEqual(self.lines(self.run_tool().stdout), [UNREADABLE])

    def test_only_the_first_64_kib_of_swap_conf_are_read(self) -> None:
        # As every settings file doctor reads: a swap.conf that never ends (a link to /dev/zero) must not
        # fill the memory. Here the partition's lines come after 64 KiB of comments, so doctor sees none.
        self.write("etc/cst/swap.conf", "# padding\n" * 7000 + "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4096\n")
        self.assertEqual(self.lines(self.run_tool().stdout), [NOT_CONFIGURED])

    def test_root_in_a_toolbox_who_cannot_read_swap_conf_is_asked_to_read_it_on_the_host(self) -> None:
        # sudo cst doctor in a toolbox: root of the user namespace may not read a host root's 0600 file.
        # cst swap status --root on the host reads it: the info row, not "cannot be read" (apply refuses).
        self.not_as_root()
        self.write("etc/cst/swap.conf", "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4096\n")
        self.close("etc/cst/swap.conf")
        host = doc_adapters.Host(dict(self.env, CST_DOCTOR_CONTAINER="1"))
        host.uid = 0
        self.assertEqual(
            list(doc_swap.check_swap_settings(host)),
            [Result("info", UNREADABLE.removeprefix("  info   "))],
        )
        # Root on the host itself is never sent to --root: a file it cannot read stops apply.
        host = doc_adapters.Host(dict(self.env))
        host.uid = 0
        self.assertEqual(
            list(doc_swap.check_swap_settings(host)),
            [
                Result(
                    "WARN",
                    "swap partition: /etc/cst/swap.conf cannot be read: cst swap apply refuses until it is fixed",
                )
            ],
        )

    def test_a_swap_conf_whose_read_fails_otherwise_stops_apply(self) -> None:
        # A file this user may read whose read fails (EIO): cst swap plan says "cannot be read" and
        # apply refuses; --root would not help, so no "cst swap status --root reads it" row.
        self.write("etc/cst/swap.conf", "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4096\n")
        host = doc_adapters.Host(dict(self.env))
        conf = os.path.join(self.root, "etc/cst/swap.conf")
        opened = host.open_regular_text

        def open_regular_text(path: str) -> TextIO:
            if path == conf:
                raise OSError(errno.EIO, "Input/output error", path)
            return opened(doc_domain.ViewPath(path))

        with mock.patch.object(host, "open_regular_text", open_regular_text):
            rows = list(doc_swap.check_swap_settings(host))
        self.assertEqual(
            rows,
            [
                Result(
                    "WARN",
                    "swap partition: /etc/cst/swap.conf cannot be read: cst swap apply refuses until it is fixed",
                )
            ],
        )

    def test_a_swap_conf_in_a_folder_this_user_may_not_enter(self) -> None:
        # Not taken as missing: no "an older CST set up swap0" row from the marked fstab line.
        self.not_as_root()
        self.write("etc/cst/swap.conf", "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=4096\n")
        self.write("etc/fstab", SWAP_FSTAB)
        self.close("etc/cst")
        self.assertEqual(self.lines(self.run_tool().stdout), [UNREADABLE])

    def test_a_folder_or_a_dangling_link_at_swap_conf_is_not_a_regular_file(self) -> None:
        folder = os.path.join(self.root, "etc/cst/swap.conf")
        os.makedirs(folder)
        not_a_file = (
            "  WARN   swap partition: /etc/cst/swap.conf not a regular file: cst swap apply refuses until it is fixed"
        )
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.lines(r.stdout), [not_a_file])
        os.rmdir(folder)
        os.symlink("nowhere", folder)
        self.write("etc/fstab", SWAP_FSTAB)
        self.assertEqual(self.lines(self.run_tool().stdout), [not_a_file])

    def test_a_fifo_or_a_link_to_a_device_at_swap_conf_is_not_a_regular_file(self) -> None:
        # As cst swap takes it (REFUSE "not a regular file"). Before, a link to /dev/null read as an empty
        # file ("not configured": add SWAP_PARTUUID to it), and a FIFO's open waited for a writer for ever
        # (bounded here: CallBlocked).
        conf = os.path.join(self.root, "etc/cst/swap.conf")
        os.makedirs(os.path.dirname(conf))
        not_a_file = Result(
            "WARN", "swap partition: /etc/cst/swap.conf not a regular file: cst swap apply refuses until it is fixed"
        )
        for kind, make in (("fifo", os.mkfifo), ("link to /dev/null", lambda p: os.symlink(os.devnull, p))):
            with self.subTest(kind=kind), time_bound(5):
                make(conf)
                try:
                    rows = list(doc_swap.check_swap_settings(doc_adapters.Host(dict(self.env))))
                finally:
                    os.remove(conf)
                self.assertEqual(rows, [not_a_file])

    def test_a_fifo_at_crypttab_or_fstab_is_one_it_cannot_read_and_nothing_blocks(self) -> None:
        # No swap.conf: the migration path reads crypttab and fstab, as cst swap reads them (regular_only:
        # an empty text). Before, the FIFO's open waited for a writer for ever (bounded here: CallBlocked).
        # crypttab: resolve_settings reads it, and zswap.conf says swap is set up, so "not configured".
        # fstab: without zswap.conf doctor reads it for an older CST's marked line; an empty text has none.
        not_configured = Result(
            "WARN",
            "swap partition: not configured (no SWAP_PARTUUID in /etc/cst/swap.conf): cst swap apply"
            " refuses (swap/README.md, section 2.3)",
        )
        zswap = os.path.join(self.root, "etc", "tmpfiles.d", "zswap.conf")
        # crypttab is not a regular file: its lines cannot be read, so not "not configured" (since 2026-11).
        not_regular = Result(
            "WARN",
            "swap partition: /etc/cst/swap.conf not written yet, and /etc/crypttab is not a regular file (a FIFO,"
            " a device, a folder, or a link to one): cst swap apply refuses until it is fixed",
        )
        del not_configured
        for name, set_up, want in (("crypttab", True, [not_regular]), ("fstab", False, [])):
            path = os.path.join(self.root, "etc", name)
            with self.subTest(name=name), time_bound(5):
                if set_up:
                    self.write("etc/tmpfiles.d/zswap.conf", "")
                os.mkfifo(path)
                try:
                    rows = list(doc_swap.check_swap_settings(doc_adapters.Host(dict(self.env))))
                finally:
                    os.remove(path)
                    if set_up:
                        os.remove(zswap)
                self.assertEqual(rows, want)

    def test_installed_but_not_configured(self) -> None:
        self.write("etc/tmpfiles.d/zswap.conf", "")
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   swap partition: not configured (no SWAP_PARTUUID in /etc/cst/swap.conf): cst swap apply"
                    " refuses (swap/README.md, section 2.3)"
                )
            ],
        )


class MachineSliceMemoryLowDoctorTest(Scratch):
    """check_machine_slice_memory_low: the same answer as cst swap status (an "over" row there)."""

    CST = "/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf"
    RUN = "/run/systemd/system.control/machine.slice.d/50-MemoryLow.conf"

    def write(self, host_path: str, text: str) -> None:
        p = os.path.join(self.root, host_path.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def show(self, memory_low: str, *dropins: str) -> None:
        d = os.path.join(self.tmp, "show", "sys")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "machine.slice"), "w") as f:
            f.write(f"LoadState=loaded\nMemoryLow={memory_low}\nDropInPaths={' '.join(dropins)}\n")

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "machine.slice MemoryLow" in ln]

    def install(self) -> None:
        with open(os.path.join(REPO, "swap", "units", "machine.slice.d-10-swap-memory-low.conf")) as f:
            self.write(self.CST, f.read())

    def test_a_drop_in_after_cst_s_is_named(self) -> None:
        self.install()
        self.write(self.RUN, "[Slice]\nMemoryLow=1G\n")
        self.show(str(2**30), self.CST, self.RUN)
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    f"  info   machine.slice MemoryLow 1.0 GiB: set by {self.RUN}, which systemd applies after CST's"
                    f" {self.CST} (48.0 GiB); cst swap leaves it"
                )
            ],
        )
        self.assertNotIn("revert", r.stdout)
        self.assert_read_only()

    def test_ok_with_cst_s_value(self) -> None:
        self.install()
        self.show(str(48 * 2**30), self.CST)
        r = self.run_tool()
        self.assertEqual(self.lines(r.stdout), [f"  ok     machine.slice MemoryLow 48.0 GiB, from {self.CST}"])

    def test_nothing_without_cst_s_drop_in_or_an_answer(self) -> None:
        self.show(str(2**30), self.RUN)
        self.assertEqual(self.lines(self.run_tool().stdout), [])
        self.install()
        os.remove(os.path.join(self.tmp, "show", "sys", "machine.slice"))
        self.assertEqual(self.lines(self.run_tool().stdout), [])


class ReadsNeverWaitTest(Scratch):
    """doctor's shared reads (fstab, the machine.slice drop-ins, zswap.conf): a FIFO is a file they cannot
    read, at once. Before, the open waited for a writer for ever (bounded here: CallBlocked)."""

    CST = "/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf"
    OTHER = "/run/systemd/system.control/machine.slice.d/50-MemoryLow.conf"

    def _fifo(self, host_path: str) -> str:
        p = os.path.join(self.root, host_path.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        os.mkfifo(p)
        return p

    def _show(self, *dropins: str) -> None:
        d = os.path.join(self.tmp, "show", "sys")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "machine.slice"), "w") as f:
            f.write(f"LoadState=loaded\nMemoryLow={48 * 2**30}\nDropInPaths={' '.join(dropins)}\n")

    def _install_cst_dropin(self) -> None:
        p = os.path.join(self.root, self.CST.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copy(os.path.join(REPO, "swap", "units", "machine.slice.d-10-swap-memory-low.conf"), p)

    def test_the_shared_read_of_a_fifo_gives_nothing_and_a_regular_empty_file_still_reads(self) -> None:
        host = doc_adapters.Host(dict(self.env))
        for name in ("/etc/fstab", self.OTHER, self.CST):
            with self.subTest(path=name), time_bound(5):
                fifo = self._fifo(name)
                try:
                    got = doc_shared.read_text(host, host.path(doc_domain.HostPath(name)), 65536)
                finally:
                    os.remove(fifo)
                self.assertEqual(got, "")
        empty = os.path.join(self.root, "proc", "cmdline")
        os.makedirs(os.path.dirname(empty))
        with open(empty, "w") as f:
            f.write("  root=UUID=x rw  \n")
        self.assertEqual(doc_shared.read_text(host, doc_domain.ViewPath(empty)), "root=UUID=x rw")

    def test_a_fifo_at_zswap_conf_is_one_it_cannot_read(self) -> None:
        self.addCleanup(os.remove, self._fifo("/etc/tmpfiles.d/zswap.conf"))
        with time_bound(5):
            rows = list(doc_swap.check_zswap_params(doc_adapters.Host(dict(self.env))))
        self.assertEqual(rows, [Result("info", "zswap: cannot read /etc/tmpfiles.d/zswap.conf")])

    def test_a_fifo_among_machine_slice_s_drop_ins_is_skipped(self) -> None:
        # CST's drop-in is a FIFO: nothing (as for one not installed). Another drop-in is a FIFO: it sets
        # nothing doctor can read, so CST's value is the one systemd shows.
        cases = (("cst", self.CST, []), ("other", self.OTHER, [f"machine.slice MemoryLow 48.0 GiB, from {self.CST}"]))
        for kind, fifo, want in cases:
            with self.subTest(kind=kind), time_bound(5):
                if kind == "other":
                    self._install_cst_dropin()
                path = self._fifo(fifo)
                self._show(self.CST, self.OTHER)
                try:
                    rows = list(doc_swap.check_machine_slice_memory_low(doc_adapters.Host(dict(self.env))))
                finally:
                    os.remove(path)
                self.assertEqual([r.message for r in rows], want)

    def test_installed_files_sha256_and_check_install_do_not_wait_on_fifo(self) -> None:
        fifo = self._fifo("/etc/tmpfiles.d/zswap.conf")
        self.addCleanup(os.remove, fifo)
        installed = doc_adapters.InstalledFiles(REPO)
        with time_bound(5):
            with self.assertRaises(OSError) as cm:
                installed.file_sha256(doc_domain.ViewPath(fifo))
            self.assertEqual(cm.exception.errno, errno.EINVAL)
            list(doc_install.check_install(doc_adapters.Host(dict(self.env)), installed=installed))

    def test_btrfs_cache_check_does_not_wait_on_fifo_crypttab(self) -> None:
        os.makedirs(
            os.path.join(self.root, "sys", "fs", "btrfs", "11111111-2222-3333-4444-555555555555", "allocation"),
            exist_ok=True,
        )
        fifo = self._fifo("/etc/crypttab")
        self.addCleanup(os.remove, fifo)
        with time_bound(5):
            rows = list(doc_btrfs.check_btrfs_cache(doc_adapters.Host(dict(self.env))))
        self.assertEqual(rows, [])


class SnapshotNumbersDoctorTest(Scratch):
    """check_snapshot_numbers: a number of snapshots.conf the hourly run cannot use, judged by the runtime's
    own parsers (guard_number of each GUARD_KEYS value, nas_unreadable), on the settings merged as the
    runtime merges them. Before, doctor said nothing: only the run's journal named them."""

    def _rows(self, text: str | None) -> list[str]:
        conf = os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf")
        if text is not None:
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            with open(conf, "w") as f:
                f.write(text)
        try:
            return [r.message for r in doc_btrfs.check_snapshot_numbers(doc_adapters.Host(dict(self.env)))]
        finally:
            if os.path.exists(conf):
                os.remove(conf)

    def test_each_value_the_guard_cannot_hold_at_is_named(self) -> None:
        guard = (
            "snapshots: SKIP_AT_OR_BELOW_GIB={!r} in /etc/btrfs-setup/snapshots.conf: not a number of GiB the space"
            " guard can hold at: the hourly run takes no snapshot until it is fixed"
        )
        cases: tuple[tuple[str, bool], ...] = (
            ("250GiB", True),
            ("abc", True),
            ("1 x", True),
            ("1\N{NO-BREAK SPACE}", True),
            ("08", True),
            (str(2**63), True),
            ("200", False),
            ("", False),
            ("100 + 20", False),
        )
        for value, warned in cases:
            with self.subTest(value=value):
                # The oracle: the runtime's own reading of the value.
                self.assertEqual(not isinstance(snap_domain.guard_number(value, exact=True), int), warned)
                want = [guard.format(value)] if warned else []
                self.assertEqual(self._rows(f"SKIP_AT_OR_BELOW_GIB='{value}'\n"), want)

    def test_a_nas_number_is_named_only_with_the_nas_backup_on(self) -> None:
        nas = (
            "snapshots: NAS_STALE_DAYS='abc' in /etc/btrfs-setup/snapshots.conf: the NAS run cannot compute it: the"
            " step it governs is skipped and the run fails"
        )
        cases: tuple[tuple[str, str, list[str]], ...] = (
            ("on", "NAS_ENABLED=1\nNAS_STALE_DAYS=abc\n", [nas]),
            ("off", "NAS_ENABLED=0\nNAS_STALE_DAYS=abc\n", []),
            ("on, a good number", "NAS_ENABLED=1\nNAS_STALE_DAYS=3\nNAS_PRUNE_EVERY_DAYS=7\n", []),
        )
        for label, text, want in cases:
            with self.subTest(label):
                self.assertEqual(self._rows(text), want)

    def test_nothing_for_defaults_a_problem_line_an_unknown_key_or_no_file(self) -> None:
        cases: tuple[tuple[str, str | None], ...] = (
            ("defaults only", "TOP=/var/mnt/btrfs-top\n"),
            ("a problem line sets nothing", "SKIP_AT_OR_BELOW_GIB = abc\n"),
            ("a key the runtime does not read", "OTHER_GIB=abc\n"),
            ("no file", None),
        )
        for label, text in cases:
            with self.subTest(label):
                self.assertEqual(self._rows(text), [])


class DockerNocowPostponedTest(Scratch):
    """check_docker_nocow_postponed: the record btrfs/docker-volumes-nocow
    keeps of the boots at which its migration had to wait."""

    def record(self, text: str) -> str:
        d = os.path.join(self.root, "var", "lib", "docker-volumes-nocow")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "postponed")
        with open(path, "w") as f:
            f.write(text)
        return path

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "Docker volumes NOCOW" in ln]

    def test_no_record_is_silent(self) -> None:
        r = self.run_tool()
        self.assertEqual(self.lines(r.stdout), [])

    def test_one_boot_is_info(self) -> None:
        self.record(json.dumps({"reason": "Docker is running", "boots": 1, "first": NOW, "last": NOW, "boot_id": "b"}))
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  info   Docker volumes NOCOW: the boot-time migration was postponed at 1 boot(s) in a row, last "
                    "2026-11-05 17:33: Docker is running; tried again at the next boot"
                )
            ],
        )
        self.assert_read_only()

    def test_two_boots_warn(self) -> None:
        reason = "dockerd (pid 812) uses /var/lib/docker/volumes outside docker.service"
        self.record(json.dumps({"reason": reason, "boots": 2, "first": NOW - 86400, "last": NOW, "boot_id": "c"}))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        got = self.lines(r.stdout)
        self.assertEqual(len(got), 1, r.stdout)
        self.assertIn(
            "WARN   Docker volumes NOCOW: the boot-time migration was postponed at 2 boot(s) in a row, last ", got[0]
        )
        self.assertIn(f": {reason}; the volumes keep copy-on-write data", got[0])
        self.assertIn("fix: journalctl -b -u docker-volumes-nocow", r.stdout)
        self.assert_read_only()

    def test_two_boots_held_back_by_daemon_json_say_to_fix_it(self) -> None:
        # Not "until a boot where nothing uses them": no quiet boot helps, only a fixed file.
        reason = "/etc/docker/daemon.json: not JSON, so the volumes folder Docker uses is unknown"
        self.record(json.dumps({"reason": reason, "boots": 2, "first": NOW - 86400, "last": NOW, "boot_id": "c"}))
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        got = self.lines(r.stdout)
        self.assertEqual(len(got), 1, r.stdout)
        self.assertTrue(
            got[0].endswith(
                f": {reason}; fix /etc/docker/daemon.json (the fix line checks it as dockerd reads it), "
                "and the next boot runs it"
            ),
            got[0],
        )
        self.assertIn("fix: sudo dockerd --validate --config-file /etc/docker/daemon.json", r.stdout)
        self.assertNotIn("until a boot where nothing uses them", r.stdout)
        self.assert_read_only()

    def test_odd_fields(self) -> None:
        self.record(json.dumps({"boots": "7", "last": "yesterday"}))
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  info   Docker volumes NOCOW: the boot-time migration was postponed at 1 boot(s) in a row, last "
                    "at an unknown time: no reason recorded; tried again at the next boot"
                )
            ],
        )

    def test_unreadable_record_is_info(self) -> None:
        for text in ("not json", "[1, 2]"):
            with self.subTest(text=text):
                self.record(text)
                r = self.run_tool()
                self.assertEqual(
                    self.lines(r.stdout),
                    ["  info   Docker volumes NOCOW: cannot read /var/lib/docker-volumes-nocow/postponed"],
                )
        os.remove(self.record(""))
        os.mkdir(os.path.join(self.root, "var", "lib", "docker-volumes-nocow", "postponed"))
        r = self.run_tool()
        self.assertIn("info   Docker volumes NOCOW: cannot read", r.stdout)

    def test_a_file_in_place_of_its_folder_is_no_record(self) -> None:
        # As the helper reads it: no record can be there (before, "cannot read" at every run).
        d = os.path.join(self.root, "var", "lib", "docker-volumes-nocow")
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(d, "w") as f:
            f.write("in the way\n")
        r = self.run_tool()
        self.assertEqual(self.lines(r.stdout), [])

    def test_a_record_of_a_newer_format_is_named(self) -> None:
        self.record(json.dumps({"v": 99, "reason": "r", "boots": 3, "first": NOW, "last": NOW, "boot_id": "b"}))
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  info   Docker volumes NOCOW: /var/lib/docker-volumes-nocow/postponed is format 99 of the "
                    "docker-nocow-postponed (this CST reads format 1 and older): written by a newer CST"
                )
            ],
        )

    def test_a_version_that_is_no_whole_number_cannot_be_read(self) -> None:
        for v in ("x", True, -1, 1.0):
            with self.subTest(v=v):
                self.record(json.dumps({"v": v, "reason": "r", "boots": 1, "first": NOW, "last": NOW, "boot_id": "b"}))
                r = self.run_tool()
                self.assertEqual(
                    self.lines(r.stdout),
                    ["  info   Docker volumes NOCOW: cannot read /var/lib/docker-volumes-nocow/postponed"],
                )

    def test_format_1_and_format_0_read_alike(self) -> None:
        for extra in ({"v": 1}, {}):
            with self.subTest(extra=extra):
                rec = {**extra, "reason": "Docker is running", "boots": 1, "first": NOW, "last": NOW, "boot_id": "b"}
                self.record(json.dumps(rec))
                r = self.run_tool()
                self.assertEqual(
                    self.lines(r.stdout),
                    [
                        (
                            "  info   Docker volumes NOCOW: the boot-time migration was postponed at 1 boot(s) in a "
                            "row, last 2026-11-05 17:33: Docker is running; tried again at the next boot"
                        )
                    ],
                )

    def test_a_time_no_clock_can_show_is_unknown(self) -> None:
        # localtime's OverflowError, OSError EOVERFLOW, and strftime's failure on a year past a C int:
        # the check failed as a whole.
        for last in (100000000000000000000, 1000000000000000000, 67768036191676799):
            with self.subTest(last=last):
                self.record(json.dumps({"v": 1, "reason": "r", "boots": 1, "first": NOW, "last": last, "boot_id": "b"}))
                r = self.run_tool()
                self.assertNotIn("the check itself failed", r.stdout)
                self.assertEqual(
                    self.lines(r.stdout),
                    [
                        (
                            "  info   Docker volumes NOCOW: the boot-time migration was postponed at 1 boot(s) in a "
                            "row, last at an unknown time: r; tried again at the next boot"
                        )
                    ],
                )


class DockerNocowOutcomeTest(Scratch):
    """check_docker_nocow_outcome: a Docker volumes migration no boot will ask for, read from what
    docker-volumes-nocow leaves (its outcome record, its count of boots, its flag); doctor never opens
    Docker's own folders."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.state_dir = os.path.join(self.root, "var", "lib", "docker-volumes-nocow")
        os.makedirs(self.state_dir)
        self.flag = os.path.join(self.root, "etc", "btrfs-setup", "docker-nocow-pending")
        os.makedirs(os.path.dirname(self.flag))
        self.helper = os.path.join(self.root, "usr", "local", "sbin", "docker-volumes-nocow")
        os.makedirs(os.path.dirname(self.helper))
        with open(self.helper, "w") as f:
            f.write("#!/bin/sh\n")

    def record(self, outcome: str, *, version: int = 1, reason: str = "cannot schedule the migration: boom") -> None:
        with open(os.path.join(self.state_dir, "outcome"), "w") as f:
            json.dump(
                {"v": version, "outcome": outcome, "at": NOW, "volumes": "/var/lib/docker/volumes", "reason": reason},
                f,
            )

    def count(self, text: str) -> None:
        with open(os.path.join(self.state_dir, "attempts"), "w") as f:
            f.write(text)

    def lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if "Docker volumes NOCOW" in ln]

    def test_an_install_that_could_not_schedule_warns(self) -> None:
        # The line names no cause doctor cannot know: +C without a flag also follows a give-up (the
        # flag removed, +C kept), not only an older install stopped half-way or a container's write.
        self.record("not-scheduled")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   Docker volumes NOCOW: /var/lib/docker/volumes keeps copy-on-write data and no boot will "
                    "migrate it (the folder has +C already, and a boot asks only for one without it), and the "
                    "install at 2026-11-05 17:33 failed: cannot schedule the migration: boom; fix that, then run the "
                    "install"
                )
            ],
        )
        self.assertIn("fix: cst btrfs install", r.stdout)
        self.assert_read_only()

    def _root_rows(
        self, *, uid: int = 0, container: bool = False, answer: tuple[str, str] | None
    ) -> tuple[list[Result], int]:
        host = doc_adapters.Host(dict(self.env))
        host.uid = uid
        host.container = container
        with mock.patch.object(doc_adapters.Host, "nocow_unscheduled", return_value=answer) as asked:
            rows = list(doc_nocow.check_docker_nocow_outcome(host))
        return rows, asked.call_count

    def test_as_root_on_the_host_a_folder_with_c_and_no_flag_or_record_warns(self) -> None:
        # As cst btrfs status's "no boot asks for it". Before, nothing: doctor never looked at the folder.
        rows, asked = self._root_rows(answer=("/var/lib/docker/volumes", "db"))
        self.assertEqual(asked, 1)
        self.assertEqual(
            rows,
            [
                Result(
                    "WARN",
                    "Docker volumes NOCOW: /var/lib/docker/volumes has +C and its volumes keep copy-on-write data (db "
                    "first), but no migration is scheduled and no outcome is recorded (an older install stopped "
                    "between its +C and its flag): no boot asks for it",
                    "cst btrfs install",
                )
            ],
        )

    def test_the_host_reads_the_folder_with_the_helper_s_tree_adapter(self) -> None:
        volumes = os.path.join(self.tmp, "docker", "volumes")
        for name in ("a", "b"):
            os.makedirs(os.path.join(volumes, name, "_data"))
        cow = {os.path.join(volumes, "a", "_data"): 0, os.path.join(volumes, "b", "_data"): 2}

        def scan(_tree: object, top: str, *, fix: bool = False) -> nocow_domain.TreeScan:
            del fix
            return nocow_domain.TreeScan(cow[top], cow[top] * 4096, 1, 4096)

        cases: tuple[tuple[str, str, bool | OSError, tuple[str, str] | None], ...] = (
            ("+C and a volume with copy-on-write data: the first one", volumes, True, (volumes, "b")),
            ("no +C", volumes, False, None),
            ("absent", os.path.join(self.tmp, "none"), True, None),
            ("+C cannot be read", volumes, OSError(errno.EIO, "I/O error"), None),
        )
        for label, folder, nocow, want in cases:
            with (
                self.subTest(label),
                mock.patch.object(doc_adapters, "nocow_docker_root", return_value=(folder, "")),
                mock.patch.object(nocow_adapters.LocalVolumeTree, "is_nocow", side_effect=[nocow]),
                mock.patch.object(nocow_adapters.LocalVolumeTree, "scan", scan),
            ):
                self.assertEqual(doc_adapters.Host(dict(self.env)).nocow_unscheduled(), want)

    def test_the_folder_is_looked_at_only_when_nothing_else_answers(self) -> None:
        cases: tuple[tuple[str, int, bool, str, bool], ...] = (
            # (label, uid, container, what is on disk, whether the folder is asked)
            ("as a user", 1000, False, "", False),
            ("as root in a toolbox", 0, True, "", False),
            ("a flag", 0, False, "flag", False),
            ("a give-up's record", 0, False, "gave-up", False),
            ("a count left", 0, False, "count", False),
            ("no helper installed", 0, False, "no helper", False),
            ("nothing else: the folder is asked, and answers none", 0, False, "", True),
        )
        for label, uid, container, on_disk, want_asked in cases:
            with self.subTest(label):
                match on_disk:
                    case "flag":
                        open(self.flag, "w").close()
                    case "gave-up":
                        self.record("gave-up")
                    case "count":
                        self.count("1")
                    case "no helper":
                        os.remove(self.helper)
                    case _:
                        pass
                try:
                    rows, asked = self._root_rows(uid=uid, container=container, answer=None)
                finally:
                    for p in (
                        self.flag,
                        os.path.join(self.state_dir, "outcome"),
                        os.path.join(self.state_dir, "attempts"),
                    ):
                        if os.path.exists(p):
                            os.remove(p)
                    if not os.path.exists(self.helper):
                        with open(self.helper, "w") as f:
                            f.write("#!/bin/sh\n")
                self.assertEqual(asked, int(want_asked))
                self.assertFalse(any("has +C" in r.message for r in rows), rows)

    def test_nothing_beside_the_flag(self) -> None:
        self.record("not-scheduled")
        self.count("2")
        open(self.flag, "w").close()
        self.assertEqual(self.lines(self.run_tool().stdout), [])

    def test_a_count_left_without_its_flag_warns(self) -> None:
        self.count("2\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            self.lines(r.stdout),
            [
                (
                    "  WARN   Docker volumes NOCOW: a count of 2 boot(s) is left with no migration scheduled: an install "
                    "or a boot could not reset it, so a migration may not be asked for (journalctl -u "
                    "docker-volumes-nocow says why); fix that, then run the install"
                )
            ],
        )
        self.assertIn("fix: cst btrfs install", r.stdout)
        # A give-up's record does not hide it (the give-up removes the count first).
        self.record("gave-up", reason="the migration did not finish after 2 boot(s)")
        self.assertEqual(len(self.lines(self.run_tool().stdout)), 1)

    def test_a_count_of_0_none_or_no_number_is_silent(self) -> None:
        self.assertEqual(self.lines(self.run_tool().stdout), [])
        for text in ("0", "", "two"):
            with self.subTest(text=text):
                self.count(text)
                self.assertEqual(self.lines(self.run_tool().stdout), [])

    def test_a_give_up_alone_is_silent(self) -> None:
        # Its alert reached the next login, and status shows it.
        self.record("gave-up", reason="the migration did not finish after 2 boot(s)")
        self.assertEqual(self.lines(self.run_tool().stdout), [])

    def test_nothing_without_the_helper(self) -> None:
        os.remove(self.helper)
        self.record("not-scheduled")
        self.count("2")
        self.assertEqual(self.lines(self.run_tool().stdout), [])

    def test_a_flag_it_cannot_look_for_is_info_and_no_warning(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root searches any folder")
        self.record("not-scheduled")
        folder = os.path.dirname(self.flag)
        os.chmod(folder, 0)
        self.addCleanup(os.chmod, folder, 0o755)
        r = self.run_tool()
        self.assertEqual(
            self.lines(r.stdout),
            ["  info   Docker volumes NOCOW: cannot tell whether a migration is scheduled (Permission denied)"],
        )

    def test_a_record_whose_folder_is_a_file_is_none(self) -> None:
        # No record can be there, as the helper's status reads it (doctor said it could not read it).
        shutil.rmtree(self.state_dir)
        with open(self.state_dir, "w") as f:
            f.write("in the way\n")
        lines = self.lines(self.run_tool().stdout)
        self.assertEqual([ln for ln in lines if "outcome" in ln or "scheduled" in ln], [], lines)

    def test_a_record_it_cannot_read_or_of_a_newer_cst_is_info(self) -> None:
        path = "/var/lib/docker-volumes-nocow/outcome"
        for text in (
            "not json",
            '{"outcome": "lost", "at": 1, "volumes": "v", "reason": "r"}',
            # A time past what the C library can show: the check itself failed before.
            '{"v": 1, "outcome": "not-scheduled", "at": 100000000000000000000, "volumes": "v", "reason": "r"}',
            '{"v": 1, "outcome": "not-scheduled", "at": 1000000000000000000, "volumes": "v", "reason": "r"}',
            # A time localtime accepts but strftime cannot show (a year past a C int, in UTC and west).
            '{"v": 1, "outcome": "not-scheduled", "at": 67768036191676799, "volumes": "v", "reason": "r"}',
        ):
            with self.subTest(text=text):
                with open(os.path.join(self.state_dir, "outcome"), "w") as f:
                    f.write(text)
                self.assertEqual(
                    self.lines(self.run_tool().stdout), [f"  info   Docker volumes NOCOW: cannot read {path}"]
                )
        self.record("not-scheduled", version=99)
        self.assertEqual(
            self.lines(self.run_tool().stdout),
            [
                (
                    f"  info   Docker volumes NOCOW: {path} is format 99 of the docker-nocow-outcome (this CST reads "
                    "format 1 and older): written by a newer CST"
                )
            ],
        )


# ---- vm ----------------------------------------------------------------------------------------
ROLLBACK = "r" * 64
CPU_FEATURES = ("cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet")
CAPS_QEMU11 = (
    "<domainCapabilities><cpu><mode name='host-passthrough' supported='yes'/>"
    "<mode name='host-model' supported='yes'><model>Arrow</model>"
    + "".join(f"<feature policy='require' name='{n}'/>" for n in (*CPU_FEATURES, "invtsc"))
    + "</mode></cpu></domainCapabilities>\n"
)
CAPS_QEMU10 = (
    "<domainCapabilities><cpu><mode name='host-model' supported='yes'>"
    "<feature policy='require' name='invtsc'/></mode></cpu></domainCapabilities>\n"
)


def domain(cpu: str) -> str:
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

    @override
    def setUp(self) -> None:
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

    def show(self, mgr: str, unit: str, load: str = "loaded", **props: str) -> None:
        d = os.path.join(self.tmp, "show", mgr)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, unit), "w") as f:
            f.write(f"LoadState={load}\n" + "".join(f"{k}={v}\n" for k, v in props.items()))

    def profile_on(self, intended: bool = True) -> None:
        open(os.path.join(self.run_dir, "game.state"), "w").close()
        if intended:
            with open(os.path.join(self.run_dir, "game.intended"), "w") as f:
                f.write(self.intended)

    def results(self) -> list[Result]:
        """The check called directly, as root (the user manager is asked with
        -M TARGET_USER@), with the stubs first in PATH."""
        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = 0
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='tester'\n")
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(doc_cpu.check_game_profile_drift(host))
        self.assert_read_only()
        return res

    def test_a_last_line_cut_short_is_not_compared(self) -> None:
        # 'on' died while it wrote it: the profile never reads it, and neither does the doctor.
        self.intended += "prop sys machine.slice CPUWeight 10 1"
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)

    def test_a_file_name_printf_q_quotes_is_read_as_bash_reads_it(self) -> None:
        # A tab in the cgroup's name: printf %q writes $'...', which shlex read as a "$..." word.
        cg = os.path.join(self.root, "sys", "fs", "cgroup", "in\tner", "cpuset.cpus")
        os.makedirs(os.path.dirname(cg))
        with open(cg, "w") as f:
            f.write("0-3\n")
        self.intended += "cgw $'/sys/fs/cgroup/in\\tner/cpuset.cpus' 12-23 12-23\n"
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
        self.assertIn("/sys/fs/cgroup/in\tner/cpuset.cpus is 0-3 (the profile wrote 12-23)", res[0].message)

    def test_the_last_cgw_record_of_a_file_wins(self) -> None:
        # 'on' writes the kernel's form of the list before the write, and a second record when the read-back
        # differs: the last one is compared (here the file holds 12-23, the prediction said 12-15).
        one = "cgw /sys/fs/cgroup/inner/cpuset.cpus 12-23 12-23\n"
        predicted = "cgw /sys/fs/cgroup/inner/cpuset.cpus 12-23 12-15\n"
        base = self.intended
        self.intended = base.replace(one, predicted + one)
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)
        self.assertIn("the 8 settings it made still hold", res[0].message)
        # The other order: the last record says 12-15, which the file does not hold.
        self.intended = base.replace(one, one + predicted)
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
        self.assertIn("/sys/fs/cgroup/inner/cpuset.cpus is 12-23 (the profile wrote 12-15)", res[0].message)

    def test_off_is_ok_and_absent_module_is_silent(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(doc_cpu.check_game_profile_drift(doc_adapters.Host(self.env, repo=REPO)))
            self.assertEqual([(r.status, r.message) for r in res], [(doc_domain.OK, "game profile: off")])
            os.remove(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"))
            self.assertEqual(list(doc_cpu.check_game_profile_drift(doc_adapters.Host(self.env, repo=REPO))), [])
        self.assertEqual(self.calls(), [])

    def test_the_format_line_of_game_intended(self) -> None:
        # This version's file starts with its format line: the records are read as before.
        self.intended = "cst-game-intended 1\n" + self.intended
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)
        self.assertIn("the 8 settings it made still hold", res[0].message)
        # A newer format (a downgraded CST while the profile is on): not compared, and said so.
        self.intended = self.intended.replace("cst-game-intended 1", "cst-game-intended 2", 1)
        self.profile_on()
        asked = len(self.calls())
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.INFO], res)
        self.assertIn("drift not checked (", res[0].message)
        self.assertIn(
            "is format 2 of the game-intended (this CST reads format 1 and older): written by a newer CST",
            res[0].message,
        )
        self.assertEqual(self.calls()[asked:], [])  # systemd is not asked

    def test_all_settings_hold(self) -> None:
        self.profile_on()
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)
        self.assertIn("the 8 settings it made still hold", res[0].message)  # empty SHOWN skipped
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER=''\n")  # no target: the doctor's own user manager
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok     game profile: ON, the 8 settings it made still hold", r.stdout)
        self.assert_read_only()

    def test_drift_warns_with_the_restart_fix(self) -> None:
        self.profile_on()
        self.show("sys", "machine.slice", AllowedCPUs="12-23", CPUWeight="50", IOWeight="50")
        self.show("sys", "user.slice", MemoryLow="0")
        self.show("usr", "user.slice", CPUWeight="30")
        with open(self.cg, "w") as f:
            f.write("8-23\n")
        res = self.results()
        self.assertEqual(res[0].status, doc_domain.WARN)
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

    def test_whole_run_as_user_asks_the_own_manager(self) -> None:
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

    def test_unreachable_manager_and_unloaded_unit_are_info(self) -> None:
        self.profile_on()
        shutil.rmtree(os.path.join(self.tmp, "show", "usr"))
        self.show("sys", "user@1000.service", load="not-found", MemoryLow="0")
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK, doc_domain.INFO], res)
        self.assertIn("the 6 settings it made still hold", res[0].message)
        self.assertIn("user@1000.service", res[1].message)
        self.assertIn("user.slice", res[1].message)

    def test_the_user_game_state_names_is_asked_not_a_changed_target_user(self) -> None:
        # 'on' acted for the user whose user@UID.service it recorded first; TARGET_USER names another
        # user now (results() sets tester). The profile's tick and 'state' ask the user game.state
        # names, and so does the doctor.
        me = pwd.getpwuid(os.getuid()).pw_name
        state = os.path.join(self.run_dir, "game.state")
        for uid, asked in ((os.getuid(), me),):
            with self.subTest(uid=uid):
                self.profile_on()
                with open(state, "w") as f:
                    f.write(f"prop sys user@{uid}.service MemoryLow new -\n")
                before = len(self.calls())
                host = doc_adapters.Host(self.env, repo=REPO)
                host.uid = 0
                with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
                    f.write("TARGET_USER='tester'\n")
                with mock.patch.dict(os.environ, self.env, clear=True):
                    list(doc_cpu.check_game_profile_drift(host))
                calls = self.calls()[before:]
                self.assertIn(f"systemctl --user -M {asked}@ show -p LoadState -p CPUWeight user.slice", calls)
                self.assertEqual([c for c in calls if " -M " in c and f"-M {asked}@" not in c], [])

    def test_the_user_manager_of_a_recorded_uid_with_no_account_is_not_compared(self) -> None:
        # No account has the uid game.state records, and TARGET_USER names another user (tester): the tick leaves
        # that uid's user manager alone and 'state' compares no drift of it, so the doctor compares none either,
        # and says why. The old doctor compared game.intended's records with tester's manager, and warned of a
        # drift the tick never repairs.
        self.profile_on()
        with open(os.path.join(self.run_dir, "game.state"), "w") as f:
            f.write("prop sys user@999999.service MemoryLow new -\n")
        self.show("usr", "user.slice", CPUWeight="30")  # tester's: not the user 'on' acted for
        before = len(self.calls())
        res = self.results()
        self.assertEqual([c for c in self.calls()[before:] if "--user" in c], [])
        self.assertEqual([r.status for r in res], [doc_domain.OK, doc_domain.INFO], res)
        self.assertIn("the 7 settings it made still hold", res[0].message)
        self.assertEqual(
            res[1].message,
            "game profile: drift not checked for user.slice (no account has uid 999999, the user 'on' acted for)",
        )

    def test_as_root_the_user_manager_is_reached_with_m_target_user_at(self) -> None:
        # The argv as one list, and unit_props runs it with a 20 s timeout and the caller's locale.
        self.profile_on()
        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = 0
        conf = os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf")
        with open(conf, "w") as f:
            f.write("TARGET_USER='tester'\n")
        with mock.patch.dict(os.environ, self.env, clear=True):
            mgr = doc_cpu.user_manager(host)
            self.assertEqual(mgr, (["systemctl", "--user", "-M", "tester@"], ""))
            base = mgr[0]
            assert base is not None
            with mock.patch.object(host, "run", wraps=host.run) as run:
                doc_cpu.unit_props(host, base, "user.slice", ["CPUWeight"])
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        "systemctl",
                        "--user",
                        "-M",
                        "tester@",
                        "show",
                        "-p",
                        "LoadState",
                        "-p",
                        "CPUWeight",
                        "user.slice",
                    ],
                    timeout=20,
                )
            ],
        )
        # No TARGET_USER: no -M @ is ever built, and the reason says why.
        with open(conf, "w") as f:
            f.write("")
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(doc_cpu.user_manager(host), (None, f"no TARGET_USER in {doc_cpu.CPU_CONF}"))

    def test_other_user_skips_the_user_manager(self) -> None:
        self.profile_on()
        host = doc_adapters.Host(self.env, repo=REPO)
        mgr, why = doc_cpu.user_manager(host)  # TARGET_USER='nobody-here' is not this user
        self.assertIsNone(mgr)
        self.assertIn("the profile is for nobody-here", why)

    def test_leftover_memorylow_dropins_while_off_warn(self) -> None:
        rt = os.path.join(self.root, "run", "systemd", "system")
        for u in ("user.slice", "user@1000.service"):
            os.makedirs(os.path.join(rt, f"{u}.d"))
            open(os.path.join(rt, f"{u}.d", "zz-cst-game.conf"), "w").close()
        os.makedirs(os.path.join(rt, "user-1000.slice.d"))
        open(os.path.join(rt, "user-1000.slice.d", "10-other.conf"), "w").close()
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(doc_cpu.check_game_profile_drift(doc_adapters.Host(self.env, repo=REPO)))
        self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
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

    def leftovers_results(self) -> list[Result]:
        """The check of the settings 'off' could not undo, called directly, as root."""
        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = 0
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(doc_cpu.check_game_leftovers(host))
        self.assert_read_only()
        return res

    def test_settings_off_could_not_undo_warn(self) -> None:
        left = os.path.join(self.run_dir, "game.leftovers")
        with open(left, "w") as f:
            f.write(
                "cst-game-leftovers 1\nprop sys machine.slice CPUWeight existed 70\n"
                "user 1000\nprop usr user.slice CPUWeight existed 30\ncgw /sys/fs/cgroup/x/cpuset.cpus 0-23\n"
            )
        message = (
            "game profile: 3 setting(s) 'off' could not undo keep the game's value: machine.slice CPUWeight, "
            "user.slice CPUWeight, /sys/fs/cgroup/x/cpuset.cpus (kept in /run/cpu-setup/game.leftovers). The next "
            "'off' and the watchdog's tick while the profile is off (cpu-game-watchdog.timer, every 30 s) undo them "
            "again; the journal of 'off' names why each one failed"
        )
        self.assertEqual(
            self.leftovers_results(), [Result(doc_domain.WARN, message, "sudo /usr/local/sbin/cpu-game-profile off")]
        )
        self.assertEqual(self.calls(), [])  # nothing asked
        # While the profile is on, 'off' would end the game's profile: no fix line.
        self.profile_on()
        self.assertEqual(
            self.leftovers_results(),
            [Result(doc_domain.WARN, message + ". The profile is on: the next 'off' retries them")],
        )

    def test_a_format_2_file_names_its_reloads_due(self) -> None:
        # game.leftovers format 2 (G2-G5): a user line before every entry, and the daemon-reloads due.
        left = os.path.join(self.run_dir, "game.leftovers")
        with open(left, "w") as f:
            f.write(
                "cst-game-leftovers 2\nuser -\nprop sys machine.slice CPUWeight existed 70\nreload sys\n"
                "user 1000\nreload usr\n"
            )
        res = self.leftovers_results()
        self.assertEqual([r[0] for r in res], [doc_domain.WARN])
        self.assertIn(
            "game profile: 1 setting(s) and 2 daemon-reload(s) 'off' could not finish keep the game's value:"
            " machine.slice CPUWeight, daemon-reload of the system manager, daemon-reload of uid 1000's user manager"
            " (kept in",
            res[0].message,
        )

    def test_no_settings_off_could_not_undo_say_nothing(self) -> None:
        self.assertEqual(self.leftovers_results(), [])  # no file
        left = os.path.join(self.run_dir, "game.leftovers")
        with open(left, "w") as f:
            f.write("cst-game-leftovers 1\n")
        self.assertEqual(self.leftovers_results(), [])

    def test_a_leftovers_file_it_cannot_read_is_info(self) -> None:
        left = os.path.join(self.run_dir, "game.leftovers")
        with open(left, "w") as f:
            f.write("cst-game-leftovers 9\nprop sys machine.slice CPUWeight existed 70\n")
        res = self.leftovers_results()
        self.assertEqual([r.status for r in res], [doc_domain.INFO], res)
        self.assertIn("is format 9 of the game-leftovers", res[0].message)
        os.unlink(left)
        os.makedirs(os.path.join(left, "inside"))
        self.assertEqual(
            self.leftovers_results(),
            [
                Result(
                    doc_domain.INFO,
                    "game profile: settings 'off' could not undo not checked "
                    "(/run/cpu-setup/game.leftovers: Is a directory)",
                )
            ],
        )

    def test_old_format_state_is_info(self) -> None:
        self.profile_on(intended=False)
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.INFO])
        self.assertIn("older cpu-game-profile", res[0].message)
        self.assertIn("sudo systemctl restart cpu-game.service", res[0].message)
        self.assertEqual(self.calls(), [])  # nothing asked

    def test_profile_off_with_drop_ins_left_warns_and_without_them_is_ok(self) -> None:
        dropins = os.path.join(self.root, "run", "systemd", "system", "user.slice.d")
        os.makedirs(dropins)
        open(os.path.join(dropins, "zz-cst-game.conf"), "w").close()
        self.assertEqual(
            self.results(),
            [
                Result(
                    doc_domain.WARN,
                    "game profile: off, but its MemoryLow drop-ins are still there: "
                    "/run/systemd/system/user.slice.d/zz-cst-game.conf. At every daemon-reload systemd applies "
                    "them, so the game's memory protection comes back without a game (that memory is then harder "
                    "to get for the VM and the builds) until the next reboot. The watchdog "
                    "(cpu-game-watchdog.timer) removes them within 30 s; if this stays, 'off' removes them and "
                    "reloads systemd",
                    "sudo /usr/local/sbin/cpu-game-profile off",
                )
            ],
        )
        shutil.rmtree(dropins)
        self.assertEqual(self.results(), [Result(doc_domain.OK, "game profile: off")])

    def test_a_profile_without_its_record_is_not_checked(self) -> None:
        self.profile_on(intended=False)
        self.assertEqual(
            self.results(),
            [
                Result(
                    doc_domain.INFO,
                    "game profile: ON, but switched on by an older cpu-game-profile that kept no record of the "
                    "values it set, so drift is not checked. Switching it off and on again records them: "
                    "sudo systemctl restart cpu-game.service",
                )
            ],
        )

    def test_a_unit_the_manager_does_not_show_is_not_checked(self) -> None:
        self.profile_on()
        self.assertEqual(self.results(), [Result(doc_domain.OK, "game profile: ON, the 8 settings it made still hold")])
        os.unlink(os.path.join(self.tmp, "show", "sys", "machine.slice"))
        self.assertEqual(
            self.results(),
            [
                Result(doc_domain.OK, "game profile: ON, the 5 settings it made still hold"),
                Result(
                    doc_domain.INFO,
                    "game profile: drift not checked for machine.slice (not loaded, or systemd did not answer)",
                ),
            ],
        )

    def test_a_cgroup_that_is_gone_is_not_compared(self) -> None:
        # Its file is gone: not counted, not drifted.
        self.intended += "cgw /sys/fs/cgroup/gone/cpuset.cpus 1-2 1-2\n"
        self.profile_on()
        self.assertEqual(self.results(), [Result(doc_domain.OK, "game profile: ON, the 8 settings it made still hold")])
        self.intended = "cgw /sys/fs/cgroup/gone/cpuset.cpus 1-2 1-2\n"
        self.profile_on()
        self.assertEqual(self.results(), [Result(doc_domain.INFO, "game profile: ON, no recorded setting to compare")])

    def test_a_cgroup_record_without_a_shown_value_is_not_compared(self) -> None:
        # The file is there and holds another value, but the record names nothing to compare with. It is the
        # file's last record, which wins (G1): the earlier record of the same file is not counted either.
        self.intended += "cgw /sys/fs/cgroup/inner/cpuset.cpus 1-2 ''\n"
        self.profile_on()
        self.assertEqual(self.results(), [Result(doc_domain.OK, "game profile: ON, the 7 settings it made still hold")])

    def test_a_recorded_uid_with_no_account_falls_back_to_target_user(self) -> None:
        # game.state names a uid no account has (999999) and TARGET_USER names tester: the profile acts for
        # tester, and the user manager boxes looked at are tester's (a slice of tester's own warns).
        tester = types.SimpleNamespace(pw_name="tester", pw_uid=4243)
        mine = os.path.join(self.root, "sys", "fs", "cgroup", "user.slice", "user-4243.slice", "user@4243.service")
        os.makedirs(os.path.join(mine, "x.slice"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='tester'\n")
        with open(os.path.join(self.run_dir, "game.state"), "w") as f:
            f.write("prop sys user@999999.service MemoryLow new -\n")

        def by_uid(uid: int) -> types.SimpleNamespace:
            if uid != tester.pw_uid:
                raise KeyError(uid)
            return tester

        def by_name(name: str) -> types.SimpleNamespace:
            if name != tester.pw_name:
                raise KeyError(name)
            return tester

        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = 0
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(doc_adapters.pwd, "getpwuid", by_uid),
            mock.patch.object(doc_adapters.pwd, "getpwnam", by_name),
        ):
            user = doc_cpu.profile_user(host)
            res = list(doc_cpu.check_user_manager_boxes(host))
        self.assertEqual(user, "tester")
        self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
        self.assertIn("user manager: x.slice is a slice of its own", res[0].message)


class UserManagerBoxesTest(Scratch):
    """The cpu check of the user manager's cgroup tree: top-level slices CST
    does not know (WARN), systemd-run jobs straight in app.slice (info). Reads
    a fixture /sys/fs/cgroup only."""

    @override
    def setUp(self) -> None:
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

    def cg(self, rel: str, usage_usec: int | None = None) -> None:
        d = os.path.join(self.ucg, rel)
        os.makedirs(d, exist_ok=True)
        if usage_usec is not None:
            with open(os.path.join(d, "cpu.stat"), "w") as f:
                f.write(f"usage_usec {usage_usec}\nuser_usec 1\nsystem_usec 1\n")

    def results(self, uid: int = 1000) -> list[Result]:
        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = uid
        with mock.patch.dict(os.environ, self.env, clear=True):
            res = list(doc_cpu.check_user_manager_boxes(host))
        self.assertEqual(self.calls(), [])  # files only, no command
        return res

    def test_as_root_the_user_game_state_names_is_looked_at(self) -> None:
        # The profile is on for the user game.state names (the tick demotes that user's boxes); TARGET_USER
        # names another, here no account: the boxes looked at are game.state's user's, as the drift check's.
        uid = os.getuid()
        mine = os.path.join(self.root, "sys", "fs", "cgroup", "user.slice", f"user-{uid}.slice", f"user@{uid}.service")
        os.makedirs(os.path.join(mine, "x.slice"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"), exist_ok=True)
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='no-such-user-here'\n")
        os.makedirs(os.path.join(self.root, "run", "cpu-setup"), exist_ok=True)
        with open(os.path.join(self.root, "run", "cpu-setup", "game.state"), "w") as f:
            f.write(f"prop sys user@{uid}.service MemoryLow new -\n")
        res = self.results(uid=0)
        self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
        self.assertIn("x.slice is a slice of its own", res[0].message)

    def as_root_with_game_state_uid(self, uid: str) -> None:
        """As root: TARGET_USER is this user's account (its boxes are looked at) and game.state records UID."""
        me = pwd.getpwuid(os.getuid())
        mine = os.path.join(self.root, "sys", "fs", "cgroup", "user.slice", f"user-{me.pw_uid}.slice")
        if not os.path.isdir(mine):
            shutil.move(self.ucg, os.path.join(mine, f"user@{me.pw_uid}.service"))
            self.ucg = os.path.join(mine, f"user@{me.pw_uid}.service")
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"), exist_ok=True)
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write(f"TARGET_USER='{me.pw_name}'\n")
        os.makedirs(os.path.join(self.root, "run", "cpu-setup"), exist_ok=True)
        with open(os.path.join(self.root, "run", "cpu-setup", "game.state"), "w") as f:
            f.write(f"prop sys user@{uid}.service MemoryLow new -\n")

    def test_a_uid_no_account_has_is_left_alone_by_the_profile(self) -> None:
        # game.state records a uid no account has: the profile's tick and 'off' call no user manager, so the
        # boxes of TARGET_USER's manager are not made idle while a game runs; the lines say so.
        self.as_root_with_game_state_uid("999999")
        self.cg("x.slice", usage_usec=1_000_000)
        self.cg("app.slice/run-u1.service", usage_usec=1_000_000)
        res = self.results(uid=0)
        self.assertEqual([r.status for r in res], [doc_domain.WARN, doc_domain.INFO], res)
        alone = "game profile, on for uid 999999, which no account has now, leaves this user manager alone until 'off'"
        warn, info = res[0].message, res[1].message
        self.assertIn(f"without the toolboxes' limits; the {alone}. Name it user-x.slice", warn)
        self.assertNotIn("idle", warn)
        self.assertIn(f"The {alone}; for builds", info)
        self.assertNotIn("idle", info)

    def test_the_lines_of_one_check_agree_when_off_removes_game_state_midway(self) -> None:
        # game.state records a uid no account has, and an 'off' removes it while the check runs: right after
        # the check's first read of it past the one that picks the user manager. The check reads the uid once
        # for all its lines, so the jobs line says what the slice's line says; before, it read game.state again
        # for each line and the jobs line, finding none, got the idle words.
        self.as_root_with_game_state_uid("999999")
        self.cg("x.slice", usage_usec=1_000_000)
        self.cg("app.slice/run-u1.service", usage_usec=1_000_000)
        host = doc_adapters.Host(self.env, repo=REPO)
        host.uid = 0
        state = os.path.join(self.root, "run", "cpu-setup", "game.state")
        opened = host.open_text
        reads: list[str] = []

        def open_text(path: str) -> TextIO:
            f = opened(doc_domain.ViewPath(path))
            if path == state:
                reads.append(path)
                if len(reads) == 2:
                    os.remove(state)  # 'off' ends the profile after this read
            return f

        with mock.patch.object(host, "open_text", open_text), mock.patch.dict(os.environ, self.env, clear=True):
            res = list(doc_cpu.check_user_manager_boxes(host))
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(reads), 2)  # one read to pick the manager, one for every line
        self.assertEqual([r.status for r in res], [doc_domain.WARN, doc_domain.INFO], res)
        alone = "game profile, on for uid 999999, which no account has now, leaves this user manager alone until 'off'"
        warn, info = res[0].message, res[1].message
        self.assertIn(f"without the toolboxes' limits; the {alone}. Name it user-x.slice", warn)
        self.assertIn(f"The {alone}; for builds", info)
        self.assertNotIn("idle", info)

    def test_a_uid_an_account_has_keeps_the_idle_words(self) -> None:
        self.as_root_with_game_state_uid(str(os.getuid()))
        self.cg("x.slice", usage_usec=1_000_000)
        self.cg("app.slice/run-u1.service", usage_usec=1_000_000)
        res = self.results(uid=0)
        self.assertEqual([r.status for r in res], [doc_domain.WARN, doc_domain.INFO], res)
        self.assertIn(
            "without the toolboxes' limits; the game profile only makes it idle while a game runs. Name it user-x.slice",
            res[0].message,
        )
        self.assertIn(
            "The game profile makes them idle while a game runs; for builds, start them with --slice=user-<name>.slice",
            res[1].message,
        )

    def test_only_known_boxes_is_ok(self) -> None:
        res = self.results()
        self.assertEqual(
            [(r.status, r.message) for r in res],
            [(doc_domain.OK, "user manager: no slices or systemd-run jobs outside the boxes CST manages")],
        )

    def test_the_slice_kept_for_the_host_sccache_server_is_known(self) -> None:
        # sccache.slice (dashless: systemd nests a dashed name under its prefix's slice) is where the
        # host's sccache server may move from app.slice: no warning for it.
        self.cg("sccache.slice/cst-sccache-host.scope", usage_usec=1_000_000)
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)

    def test_unknown_top_level_slice_warns_with_cpu_time_and_the_name_to_use(self) -> None:
        self.cg("lushtext.slice", usage_usec=69_000_123_456)
        self.cg("_cpu.slice")  # cgroupfs name of cpu.slice
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.WARN, doc_domain.WARN], res)
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

    def test_systemd_run_jobs_in_app_slice_are_one_info_line(self) -> None:
        self.cg("app.slice/run-u7.service", usage_usec=2_500_000)
        self.cg("app.slice/run-r1a2.scope", usage_usec=90_000_000)
        self.cg("app.slice/app-run-not-transient.scope")
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.INFO], res)
        self.assertIn("2 systemd-run job(s) run straight in app.slice", res[0].message)
        # Busiest first.
        self.assertIn("run-r1a2.scope (90 CPU-seconds), run-u7.service (2 CPU-seconds)", res[0].message)
        self.assertNotIn("app-run-not-transient", res[0].message)

    def test_jobs_are_sorted_by_cpu_time_not_by_name(self) -> None:
        # Name order is the reverse of the CPU order, and one has no cpu.stat.
        self.cg("app.slice/run-a.service", usage_usec=1_000_000)
        self.cg("app.slice/run-b.service")
        self.cg("app.slice/run-c.service", usage_usec=7_000_000)
        res = self.results()
        self.assertIn(
            "run-c.service (7 CPU-seconds), run-a.service (1 CPU-seconds), run-b.service (CPU time unknown)",
            res[0].message,
        )

    def test_many_jobs_are_cut_short(self) -> None:
        for i in range(12):
            self.cg(f"app.slice/run-u{i}.service", usage_usec=i * 1_000_000)
        res = self.results()
        self.assertIn("12 systemd-run job(s)", res[0].message)
        self.assertIn("and 7 more", res[0].message)

    def test_exactly_the_list_size_is_not_cut(self) -> None:
        for i in range(doc_domain.LIST_MAX):
            self.cg(f"app.slice/run-u{i}.service", usage_usec=1_000_000)
        res = self.results()
        self.assertNotIn("more", res[0].message)
        self.assertIn(f"{doc_domain.LIST_MAX} systemd-run job(s)", res[0].message)

    def test_a_short_list_has_no_more_suffix(self) -> None:
        self.cg("app.slice/run-u1.service", usage_usec=1_000_000)
        res = self.results()
        self.assertNotIn("more", res[0].message)

    def test_cpu_time_keeps_the_fraction_and_shows_hours_from_one_hour(self) -> None:
        self.cg("app.slice/run-u1.service", usage_usec=2_600_000)  # 2.6 s, not 2
        self.assertIn("(3 CPU-seconds)", self.results()[0].message)
        self.assertEqual(doc_domain.cpu_time(3600.0), "3,600 CPU-seconds (1.0 CPU-hours)")
        self.assertEqual(doc_domain.cpu_time(3599.5), "3,600 CPU-seconds")  # rounds, but under one hour

    def test_no_app_slice_folder_is_not_an_error(self) -> None:
        shutil.rmtree(os.path.join(self.ucg, "app.slice"))
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)

    def test_no_user_manager_is_silent(self) -> None:
        self.assertEqual(self.results(uid=4242), [])
        # As root: the user from cpu-setup.conf; none recorded, or unknown: nothing.
        self.assertEqual(self.results(uid=0), [])
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='no-such-user-here'\n")
        self.assertEqual(self.results(uid=0), [])

    def test_as_root_it_looks_at_the_target_user(self) -> None:
        me = pwd.getpwuid(os.getuid())
        ucg = os.path.join(
            self.root, "sys", "fs", "cgroup", "user.slice", f"user-{me.pw_uid}.slice", f"user@{me.pw_uid}.service"
        )
        os.makedirs(os.path.join(ucg, "builds.slice"))
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write(f"TARGET_USER='{me.pw_name}'\n")
        res = self.results(uid=0)
        self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
        self.assertIn("builds.slice", res[0].message)

    def test_whole_run_in_a_toolbox_reads_the_host_tree(self) -> None:
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
    @override
    def setUp(self) -> None:
        super().setUp()
        self.virsh = os.path.join(self.tmp, "virsh")
        os.makedirs(self.virsh)
        self.env["FIX_VIRSH"] = self.virsh
        self.libvirt(["win"], {"win": PASSTHROUGH_FIXED}, CAPS_QEMU11)

    def libvirt(self, names: Sequence[str], defs: Mapping[str, str], caps: str | None) -> None:
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

    def vm_lines(self, r: subprocess.CompletedProcess[str]) -> list[str]:
        return [
            ln
            for ln in r.stdout.split("== vm", 1)[-1].splitlines()
            if ln.strip().startswith(("ok", "info", "WARN", "ERROR", "fix"))
        ]

    # -- deployments with and without a virt stack change
    def deployments(
        self,
        rollback_pinned: bool = False,
        booted_qemu: str = "11.1.3-1.fc45",
        rollback_qemu: str = "10.2.2-1.fc44",
    ) -> None:
        with open(os.path.join(FIX, "unpinned.json"), encoding="utf-8") as f:
            base = json.load(f)
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

    def backup_index(self, epoch: float) -> None:
        d = os.path.join(self.root, "var", "lib", "cst")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "vm-backups.json"), "w") as f:
            json.dump({"version": 1, "backups": [{"vm": "win", "epoch": epoch, "path": "/x", "timestamp": "t"}]}, f)

    def test_fixed_vm_is_ok(self) -> None:
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok     VM win: host-passthrough with cet-ss, cet-ibt", r.stdout)
        # domcapabilities needs a read-write connection: asked only when a disable is missing.
        self.assertNotIn("virsh -c qemu:///system domcapabilities", self.calls())
        self.assert_read_only()

    def test_missing_disables_warn_when_qemu_offers_them(self) -> None:
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

    def test_only_offered_features_count(self) -> None:
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, CAPS_QEMU10)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("this QEMU does not offer", r.stdout)

    def test_unreadable_caps_is_info(self) -> None:
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, None)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("info   VM win: host-passthrough without disabling", r.stdout)

    def test_other_cpu_modes_ok(self) -> None:
        self.libvirt(["win", "lin"], {"win": domain("<cpu mode='host-model'/>"), "lin": domain("")}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("VM win: cpu mode host-model", r.stdout)
        self.assertIn("VM lin: cpu mode not set", r.stdout)

    def test_empty_knob_skips(self) -> None:
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("VM_NAME='win'\nVM_CPU_DISABLE=''\n")
        self.libvirt(["win"], {"win": PASSTHROUGH_BARE}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("VM_CPU_DISABLE is empty", r.stdout)

    def test_last_knob_line_wins(self) -> None:
        # As in vm-pin's conf() and the shell: a later assignment overrides an earlier one.
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("VM_CPU_DISABLE='cet-ss cet-ibt'\n# VM_CPU_DISABLE='vmx-mbec'\nVM_CPU_DISABLE=''\n")
        self.assertEqual(doc_vm.cpu_disable_names(doc_adapters.Host(self.env, repo=REPO)), [])
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("VM_CPU_DISABLE=''\nVM_CPU_DISABLE=\"cet-ss\"\n")
        self.assertEqual(doc_vm.cpu_disable_names(doc_adapters.Host(self.env, repo=REPO)), ["cet-ss"])

    def test_knob_from_vm_pin(self) -> None:
        host = doc_adapters.Host(self.env, repo=REPO)
        self.assertEqual(doc_vm.cpu_disable_names(host), list(CPU_FEATURES))

    def test_no_libvirt_is_info_never_warn(self) -> None:
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

    def test_no_domains_is_ok(self) -> None:
        self.libvirt([], {}, CAPS_QEMU11)
        self.deployments()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok     libvirt: no VM defined", r.stdout)
        self.assertIn("no libvirt VM defined", r.stdout)

    def test_virt_change_unpinned_no_backup_warns_twice(self) -> None:
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

    def test_virt_change_pinned_and_backed_up_is_info(self) -> None:
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 86400)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("rollback pinned and VM backup present", r.stdout)

    def test_backup_older_than_booted_still_warns(self) -> None:
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 10 * 86400)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("ostree admin pin", r.stdout)
        self.assertIn("No VM backup since the booted deployment was built", r.stdout)

    def test_entries_cst_vm_does_not_list_are_no_backup(self) -> None:
        # cst vm keeps them in the index without listing them (is_index_entry): an empty vm, a NAS
        # entry without its snapshot, an epoch that is a bool. Their epoch must not pass for a backup.
        self.deployments(rollback_pinned=True)
        d = os.path.join(self.root, "var", "lib", "cst")
        os.makedirs(d, exist_ok=True)
        foreign = [
            {"vm": "", "epoch": NOW, "path": "/x"},
            {"vm": "win", "mode": "nas", "epoch": NOW},
            {"vm": "win", "epoch": True, "path": "/x"},
            {"vm": "win", "epoch": NOW, "path": "/x", "versions": {"qemu-kvm": 1}},
        ]
        with open(os.path.join(d, "vm-backups.json"), "w") as f:
            json.dump({"version": 1, "backups": foreign}, f)
        r = self.run_tool()
        self.assertIn("No VM backup since the booted deployment was built", r.stdout)
        self.assertIn("last backup: never", r.stdout)
        self.assert_read_only()

    def test_a_damaged_index_warns(self) -> None:
        # cst vm refuses to back up or list with it: doctor says so, and changes nothing.
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 86400)
        path = os.path.join(self.root, "var", "lib", "cst", "vm-backups.json")
        # A byte that is not UTF-8, even inside a string, is "not JSON" for cst vm, so for doctor too.
        for text, why in ((b"{", "not JSON"), (b'{"backups": ["\xff"]}', "not JSON"), (b"[]", 'no "backups" list')):
            with self.subTest(text=text):
                with open(path, "wb") as f:
                    f.write(text)
                r = self.run_tool()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(
                    f"WARN   /var/lib/cst/vm-backups.json cannot be read as cst vm's backup index ({why}): "
                    "cst vm refuses to back up or list with it; move it aside to start a new index",
                    r.stdout,
                )
                self.assert_read_only()

    def test_an_index_cst_vm_cannot_read_counts_no_backup(self) -> None:
        # Doctor read the index two ways: the backup count decoded a byte that is not UTF-8 as U+FFFD
        # and counted a backup cst vm refuses to list, which cleared the warning.
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 86400)
        path = os.path.join(self.root, "var", "lib", "cst", "vm-backups.json")
        with open(path) as f:
            text = f.read()
        for raw in (text.encode().replace(b'"t"', b'"t\xff"'), b"[" * 100_000):
            with self.subTest(raw=raw[:20]):
                with open(path, "wb") as f:
                    f.write(raw)
                r = self.run_tool()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("cannot be read as cst vm's backup index (not JSON)", r.stdout)
                self.assertNotIn("rollback pinned and VM backup present", r.stdout)
                self.assertIn("No VM backup since the booted deployment was built", r.stdout)
                self.assertIn("last backup: never", r.stdout)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                self.assert_read_only()

    def write_torn(self, raw: bytes) -> str:
        path = os.path.join(self.root, "var", "lib", "cst", "vm-nas-torn.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return path

    def torn_lines(self, r: subprocess.CompletedProcess[str]) -> list[str]:
        lines = self.vm_lines(r)
        after = [ln.strip().startswith("fix") and i > 0 and "vm-nas-torn" in lines[i - 1] for i, ln in enumerate(lines)]
        return [ln.strip() for ln, fix in zip(lines, after, strict=True) if "vm-nas-torn" in ln or fix]

    def test_no_row_without_torn_snapshots(self) -> None:
        r = self.run_tool()
        self.assertEqual(self.torn_lines(r), [])
        self.write_torn(json.dumps({"version": 1, "torn": []}).encode())
        r = self.run_tool()
        self.assertEqual(self.torn_lines(r), [], r.stdout)
        self.assert_read_only()

    def test_torn_snapshots_a_nas_backup_could_not_forget_warn(self) -> None:
        def entry(vm: str, snapshot: str, stamp: str) -> dict[str, str]:
            return {
                "snapshot": snapshot,
                "vm": vm,
                "repository": "sftp:nas:/r",
                "host": "h",
                "timestamp": stamp,
                "why": "x",
            }

        torn: list[object] = [
            entry("win", "a1b2c3d4" + "e" * 56, "20261108T010640Z"),
            entry("lab", "f00dbabe" + "1" * 56, "20261101T010000Z"),
            {"snapshot": "c0ffee", "note": "an entry of a newer cst vm: not counted"},
        ]
        self.write_torn(json.dumps({"version": 1, "torn": torn}).encode())
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(
            self.torn_lines(r),
            [
                (
                    "WARN   2 restic snapshot(s) recorded as torn (no backup) by a failed NAS backup, in "
                    "/var/lib/cst/vm-nas-torn.json: win a1b2c3d4 (20261108T010640Z), lab f00dbabe (20261101T010000Z); "
                    "the next `cst vm backup VM --nas` to the same repository forgets them (or drops those already "
                    "forgotten) before its retention, and skips the retention until then"
                ),
                (
                    "fix: or by hand: restic forget ID, with the repository's variables of vm/README.md, section "
                    "10.9; for a VM or repository with no more NAS backups, and for an older cst-vm the weekly "
                    "timer runs (`cst vm backup-timer on VM`), see section 10.6"
                ),
            ],
        )
        self.assertFalse([c for c in self.calls() if c.startswith("restic")], "no restic call")
        self.assert_read_only()

    def test_a_torn_record_cst_vm_cannot_use_warns(self) -> None:
        for raw, said in (
            (
                json.dumps({"version": 2, "torn": []}).encode(),
                (
                    "/var/lib/cst/vm-nas-torn.json is format 2 of the vm-nas-torn (this CST reads format 1 and older): "
                    "written by a newer CST: cst vm backup --nas refuses to run with it; use the cst vm of that CST"
                ),
            ),
            (b"{", "(not JSON)"),
            (b'{"torn": ["\xff"]}', "(not JSON)"),
            (b'{"version": 1}', '(no "torn" list)'),
        ):
            with self.subTest(raw=raw):
                self.write_torn(raw)
                r = self.run_tool()
                self.assertEqual(r.returncode, 1, r.stdout)
                lines = self.torn_lines(r)
                self.assertEqual(len(lines), 1, r.stdout)
                self.assertIn(said, lines[0])
                if b"version" not in raw[:12]:
                    self.assertEqual(
                        lines[0],
                        f"WARN   /var/lib/cst/vm-nas-torn.json cannot be read as cst vm's record of torn NAS snapshots "
                        f"{said}: cst vm backup --nas refuses to run with it; forget the snapshots it names by hand, "
                        "then move it aside",
                    )
                self.assert_read_only()

    def test_an_index_of_a_newer_cst_vm_warns_and_is_not_read(self) -> None:
        # After going back to an older CST: a newer index is neither taken for "never backed up"
        # nor read as if this checkout knew its format.
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 86400)
        path = os.path.join(self.root, "var", "lib", "cst", "vm-backups.json")
        with open(path) as f:
            data = json.load(f)
        data["version"] = 2
        with open(path, "w") as f:
            json.dump(data, f)
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(
            "WARN   /var/lib/cst/vm-backups.json is format 2 of the vm-index (this CST reads format 1 and older): "
            "written by a newer CST: cst vm refuses to back up or list with it",
            r.stdout,
        )
        self.assertIn("last backup: unknown (see the index line)", r.stdout)
        self.assertNotIn("rollback pinned and VM backup present", r.stdout)
        self.assert_read_only()

    def test_pin_index_follows_the_list(self) -> None:
        """A staged deployment first: booted is 1, rollback 2."""
        self.deployments()
        with open(self.env["FIX_STATUS"], encoding="utf-8") as f:
            data = json.load(f)
        staged = dict(data["deployments"][0], checksum=STAGED, booted=False, staged=True, version="45.2")
        data["deployments"].insert(0, staged)
        with open(self.env["FIX_STATUS"], "w") as f:
            json.dump(data, f)
        r = self.run_tool()
        self.assertIn("fix: sudo ostree admin pin 2", r.stdout)

    def test_unchanged_virt_stack_is_ok(self) -> None:
        self.deployments(rollback_qemu="11.1.3-1.fc45")
        with open(os.path.join(self.db, ROLLBACK)) as f:
            text = f.read().replace("libvirt-daemon-12.6.0-2.fc44", "libvirt-daemon-12.6.0-2.fc45")
        with open(os.path.join(self.db, ROLLBACK), "w") as f:
            f.write(text)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok     virtualization packages: the same", r.stdout)

    def test_not_ostree_is_info(self) -> None:
        os.unlink(os.path.join(self.root, "run", "ostree-booted"))
        r = self.run_tool()
        self.assertIn("info   virtualization packages: not compared (not an rpm-ostree system)", r.stdout)

    def test_virt_changes_text(self) -> None:
        self.assertEqual(doc_domain.virt_changes({"a": "1"}, {"a": "1"}), "")
        many = {f"libvirt-daemon-driver-{i}": "1" for i in range(5)}
        self.assertEqual(
            doc_domain.virt_changes(many, {}),
            "libvirt-daemon-driver-0 1 -> none, libvirt-daemon-driver-1 "
            "1 -> none, libvirt-daemon-driver-2 1 -> none, and 2 more virtualization package(s)",
        )
        self.assertEqual(
            doc_domain.virt_versions(["qemu-kvm-2:11.1.3-1.fc45.x86_64", "vim-9-1.fc45.x86_64"]),
            {"qemu-kvm": "2:11.1.3-1.fc45"},
        )

    WHAT = (
        "virtualization packages changed from the rollback deployment 44.20261028.0 to the booted one: "
        "libvirt-daemon 12.6.0-2.fc44 -> 12.6.0-2.fc45, qemu-kvm 10.2.2-1.fc44 -> 11.1.3-1.fc45, "
        "qemu-system-x86-core 10.2.2-1.fc44 -> 11.1.3-1.fc45"
    )

    def test_the_unpinned_and_no_backup_warnings_whole(self) -> None:
        self.deployments()
        r = self.run_tool()
        self.assertIn(
            f"  WARN   {self.WHAT}. The rollback deployment (index 1) is not pinned: the next upgrade can remove "
            "it, and with it the way back if a VM stops booting. Keep it pinned until the VMs (win) work on the "
            "new one\n"
            "         fix: sudo ostree admin pin 1\n"
            f"  WARN   {self.WHAT}. No VM backup since the booted deployment was built (2026-10-29; last backup: "
            "never). Shut the VM down first\n"
            "         fix: cst vm backup\n",
            r.stdout,
        )

    def test_a_backup_older_than_the_build_names_its_date(self) -> None:
        self.deployments(rollback_pinned=True)
        self.backup_index(NOW - 10 * 86400)
        r = self.run_tool()
        self.assertIn(
            f"  WARN   {self.WHAT}. No VM backup since the booted deployment was built (2026-10-29; last backup: "
            "2026-10-26). Shut the VM down first\n"
            "         fix: cst vm backup\n",
            r.stdout,
        )
        os.makedirs(os.path.join(self.root, "etc", "btrfs-setup"))
        with open(os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf"), "w") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/volume1/restic\n")
        r = self.run_tool()
        self.assertIn(
            f"  WARN   {self.WHAT}. No VM backup since the booted deployment was built (2026-10-29; last backup: "
            "2026-10-26). The NAS backup works with the VM running\n"
            "         fix: cst vm backup --nas\n",
            r.stdout,
        )

    def test_without_a_rollback_or_its_package_list_nothing_is_compared(self) -> None:
        r = self.run_tool()
        self.assertIn("  ok     virtualization packages: no rollback deployment to compare with\n", r.stdout)
        self.deployments()
        os.unlink(os.path.join(self.db, ROLLBACK))
        r = self.run_tool()
        self.assertIn("  info   virtualization packages: not compared (rpm-ostree db list did not answer)\n", r.stdout)

    def test_a_change_with_no_vm_defined_is_info(self) -> None:
        self.deployments()
        self.libvirt([], {}, None)
        r = self.run_tool()
        self.assertIn(f"  info   {self.WHAT}; no libvirt VM defined\n", r.stdout)

    def test_definitions_that_cannot_be_read_and_caps_that_cannot(self) -> None:
        self.libvirt(["win", "bad", "gone"], {"win": PASSTHROUGH_BARE, "bad": "<domain"}, None)
        r = self.run_tool()
        self.assertEqual(
            self.vm_lines(r)[:3],
            [
                (
                    "  info   VM win: host-passthrough without disabling cet-ss, cet-ibt, vmx-mbec, vmx-exit-save-cet, "
                    "vmx-entry-load-cet; whether this QEMU passes them was not checked (error: unsupported in the "
                    "stub). If Windows loops in Automatic Repair: cst cpu vm-pin plan "
                    "(https://gitlab.com/qemu-project/qemu/-/issues/3215)"
                ),
                "  info   VM bad: definition not readable (definition unreadable)",
                "  info   VM gone: definition not readable (error: unsupported in the stub)",
            ],
        )
        self.assert_read_only()


class VmNasBackupTest(VmTest):
    """`cst vm backup --nas`: an overlay left behind, the timer, NAS backups in the freshness check."""

    OVERLAY = "/var/lib/libvirt/images/win.qcow2.cst-overlay-20261114T033000Z"

    def on_overlay(self, live: bool = True, persistent: bool = False) -> None:
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

    def test_overlay_left_behind_warns(self) -> None:
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

    def test_overlay_disk_without_target_dev(self) -> None:
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

    def test_overlay_of_a_shut_off_vm(self) -> None:
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

    def test_overlay_during_a_backup_is_info(self) -> None:
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

    def test_no_overlay_is_silent(self) -> None:
        r = self.run_tool()
        self.assertNotIn("overlay", r.stdout)

    def test_timer_state_is_info(self) -> None:
        r = self.run_tool()
        self.assertIn("info   VM win: weekly NAS backup timer: off (cst vm backup-timer on win turns it on)", r.stdout)
        wants = os.path.join(self.root, "etc", "systemd", "system", "timers.target.wants")
        os.makedirs(wants)
        open(os.path.join(wants, "cst-vm-backup@win.timer"), "w").close()
        r = self.run_tool()
        self.assertIn("info   VM win: weekly NAS backup timer: on", r.stdout)

    def test_settings_are_read_as_bash_reads_them_and_problem_lines_warn(self) -> None:
        # "NAS_ENABLED=1 # On" is 1 for btrfs-snapshot (bash); doctor's own copy of the
        # reader saw "1 # on" and called the NAS backup not set up.
        self.deployments(rollback_pinned=True)
        os.makedirs(os.path.join(self.root, "etc", "btrfs-setup"))
        snap = os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf")
        with open(snap, "w") as f:
            f.write("NAS_ENABLED=1 # on\nNAS_PATH='/volume1/restic'\n")
        r = self.run_tool()
        self.assertIn("The NAS backup works with the VM running", r.stdout)
        self.assertNotIn("snapshots.conf line", r.stdout)
        with open(snap, "a") as f:
            f.write("NAS_HOST = nas\n")
        os.makedirs(os.path.join(self.root, "etc", "cst"), exist_ok=True)
        with open(os.path.join(self.root, "etc", "cst", "vm.conf"), "w") as f:
            f.write("VM_NOCOW = no\nVM_TUNE_NIC=$X\n")  # blanks are fine in vm.conf; $ is not
        r = self.run_tool()
        self.assertIn("WARN", r.stdout)
        self.assertIn("/etc/btrfs-setup/snapshots.conf line 3: NAS_HOST = nas: blanks around =", r.stdout)
        # btrfs-snapshot reads snapshots.conf as data too: the warning says it ignores the line (it
        # said that bash, which sourced the file, read the line its own way).
        self.assertIn(
            "NAS_HOST = nas: blanks around =: bash runs it as a command and sets nothing. CST's Python "
            "programs take nothing from the line (btrfs-snapshot ignores it too, with a warning in its journal)",
            r.stdout,
        )
        self.assertIn("/etc/cst/vm.conf line 2: VM_TUNE_NIC=$X: $", r.stdout)
        self.assertNotIn("vm.conf line 1", r.stdout)

    def test_a_cpu_setup_conf_line_warns_that_the_game_profile_does_not_switch_on(self) -> None:
        # No bash program of this CST sources cpu-setup.conf any more: the game profile is Python, and
        # refuses to switch on over such a line.
        self.deployments(rollback_pinned=True)
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        with open(os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf"), "w") as f:
            f.write("TARGET_USER='tester'\nBUILD_CPUS=$(nproc)\n")
        r = self.run_tool()
        line = next((x for x in r.stdout.split("\n") if "cpu-setup.conf line 2" in x), "")
        self.assertIn("WARN", line)
        self.assertIn(
            "CST's Python programs take nothing from the line (the game profile does not switch on), and the cst"
            " commands that change the machine with these settings refuse to run until it is fixed",
            line,
        )
        self.assertNotIn("bash, which also sources", line)

    def test_a_target_user_with_no_account_warns_that_the_game_profile_does_not_switch_on(self) -> None:
        # 'on' refuses such a name and the watchdog does not start the profile; the doctor names it. Asked of
        # the host's accounts (getent passwd, on the host also from a toolbox).
        me = pwd.getpwuid(os.getuid()).pw_name
        os.makedirs(os.path.join(self.root, "etc", "cpu-setup"))
        conf = os.path.join(self.root, "etc", "cpu-setup", "cpu-setup.conf")
        warning = "/etc/cpu-setup/cpu-setup.conf: TARGET_USER names no account: no-such-user-here. The game profile"
        for container in ("0", "1"):
            self.env["CST_DOCTOR_CONTAINER"] = container
            with open(conf, "w") as f:
                f.write("TARGET_USER='no-such-user-here'\n")
            host = doc_adapters.Host(self.env, repo=REPO)
            with mock.patch.dict(os.environ, self.env, clear=True):
                res = list(doc_cpu.check_cpu_settings(host))
            self.assertEqual([r.status for r in res], [doc_domain.WARN], res)
            self.assertTrue(res[0].message.startswith(warning), res[0].message)
            self.assertIn("Set TARGET_USER to your user (id -un)", res[0].message)
            for text in (f"TARGET_USER='{me}'\n", "TARGET_USER=''\n", "BUILD_CPUS='12-23'\n"):
                with open(conf, "w") as f:
                    f.write(text)
                with mock.patch.dict(os.environ, self.env, clear=True):
                    self.assertEqual(list(doc_cpu.check_cpu_settings(host)), [], text)
        self.assertIn("flatpak-spawn --host getent -- passwd no-such-user-here", self.calls())
        self.assert_read_only()
        # The whole run: the warning in the cpu area.
        self.deployments(rollback_pinned=True)
        self.env["CST_DOCTOR_CONTAINER"] = "0"
        with open(conf, "w") as f:
            f.write("TARGET_USER='no-such-user-here'\n")
        r = self.run_tool()
        line = next((x for x in r.stdout.split("\n") if "TARGET_USER names no account" in x), "")
        self.assertIn("WARN", line)

    def test_getent_that_cannot_answer_is_no_warning(self) -> None:
        host = doc_adapters.Host(self.env, repo=REPO)
        with mock.patch.object(host, "run", return_value=(127, "", "getent: not found")):
            self.assertFalse(doc_cpu.names_no_account(host, "no-such-user-here"))
        with mock.patch.object(host, "run", return_value=(0, "x:x:1:1::/:/bin/sh\n", "")):
            self.assertFalse(doc_cpu.names_no_account(host, "x"))

    def test_snapshots_conf_lines_an_older_cst_wrote_with_printf_q_do_not_warn(self) -> None:
        # The old snap_conf_set wrote values with printf %q (a\ b); bash reads "a b", and so does doctor.
        self.deployments(rollback_pinned=True)
        os.makedirs(os.path.join(self.root, "etc", "btrfs-setup"))
        with open(os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf"), "w") as f:
            f.write("NAS_ENABLED=1\nNAS_PATH=/volume1/My\\ restic\nNAS_HOST=$'nas\\x2d1'\n")
        r = self.run_tool()
        self.assertIn("The NAS backup works with the VM running", r.stdout)
        self.assertNotIn("snapshots.conf line", r.stdout)
        # Anything else with a backslash still warns, there too.
        with open(os.path.join(self.root, "etc", "btrfs-setup", "snapshots.conf"), "a") as f:
            f.write("NAS_USER=a\\ $USER\n")
        r = self.run_tool()
        self.assertIn("/etc/btrfs-setup/snapshots.conf line 4: NAS_USER=a\\ $USER: $", r.stdout)

    def test_nas_backup_counts_and_fix_names_it(self) -> None:
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

    LEFT = "an overlay left by `cst vm backup --nas` (it could not merge it back)"
    GUIDE = '(vm/README.md, "If a backup left an overlay")'

    def test_an_overlay_the_vm_writes_into_whole(self) -> None:
        self.on_overlay(live=True, persistent=False)
        expected = (
            f"  WARN   VM win: disk sda is on {self.OVERLAY}, {self.LEFT}. The VM writes into it, and it grows. If "
            "`virsh blockjob win sda --info` shows a commit ready (100 %), finish it with `virsh blockjob win sda "
            "--pivot`; with no block job, merge it with the command below. Then check that `virsh domblklist win "
            f"--details` (also with --inactive) shows {self.BASE}, and remove the overlay file {self.GUIDE}\n"
            "         fix: sudo virsh -c qemu:///system blockcommit win sda --active --pivot --wait --verbose\n"
        )
        r = self.run_tool()
        self.assertIn(expected, r.stdout)
        # A definition that does not parse names no overlay: the live listing alone finds it.
        with open(os.path.join(self.virsh, "win.xml"), "w") as f:
            f.write("<domain")
        r = self.run_tool()
        self.assertIn(expected, r.stdout)
        self.assert_read_only()

    def test_an_overlay_whose_state_or_listing_does_not_tell_whole(self) -> None:
        # Running, the definition on the overlay, the live listing not readable; then the state unreadable.
        def expected(state: str) -> str:
            return (
                f"  WARN   VM win: disk sda is on {self.OVERLAY}, {self.LEFT}. The VM's state ({state}) or its disk "
                "listing does not tell how to put it back. Do not remove or commit anything yet: look with "
                f"`virsh domstate win` and `virsh domblklist win --details` (and --inactive) {self.GUIDE}\n"
                "         fix: sudo virsh -c qemu:///system domblklist win --details\n"
            )

        self.on_overlay(live=False, persistent=True)
        open(os.path.join(self.virsh, "win.blkfail"), "w").close()
        r = self.run_tool()
        self.assertIn(expected("running"), r.stdout)
        os.unlink(os.path.join(self.virsh, "win.blkfail"))
        self.on_overlay(live=True, persistent=True)
        open(os.path.join(self.virsh, "win.state"), "w").close()
        r = self.run_tool()
        self.assertIn(expected("unreadable"), r.stdout)
        self.assert_read_only()


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

    def test_hardware_differences_are_info(self) -> None:
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

    def test_knobs_come_from_the_host(self) -> None:
        os.makedirs(os.path.join(self.root, "etc", "cst"))
        with open(os.path.join(self.root, "etc", "cst", "vm.conf"), "w") as f:
            f.write("VM_TUNE_DISK_BUS=no\nVM_TUNE_DISK_DRIVER=no\nVM_TUNE_NIC=no\n")
        self.libvirt(["win"], {"win": self.OLD_HW}, CAPS_QEMU11)
        r = self.run_tool()
        self.assertIn("ok     VM win: disks and network card as cst vm tune recommends", r.stdout)

    def folders(
        self,
        flags: Mapping[str, int | None],
        btrfs: bool = True,
        scandir_denied: tuple[str, ...] = (),
    ) -> list[Result]:
        """The image folder check in-process, with fake inode flags."""
        real_scandir = os.scandir

        def scandir(p: str) -> _ScandirIter:
            if p in scandir_denied:
                raise PermissionError(13, "Permission denied")
            return real_scandir(p)

        # The probe lives in vm.adapters.images (VmTools calls it there).
        with (
            mock.patch.dict(os.environ, {"HOME": self.tmp}),
            mock.patch.object(vm_images, "is_btrfs", return_value=btrfs),
            mock.patch.object(vm_images, "readable_inode_flags", lambda p: flags.get(p, 0)),
            mock.patch.object(os, "scandir", scandir),
        ):
            return list(
                doc_vm.check_vm_image_dirs(
                    doc_adapters.Host(self.env, repo=REPO), vm_tools=doc_adapters.VmTools(vm_settings())
                )
            )

    def test_image_folders(self) -> None:
        system = os.path.join(self.root, "var", "lib", "libvirt", "images")
        boxes = os.path.join(self.tmp, ".local", "share", "gnome-boxes", "images")
        for d in (system, boxes):
            os.makedirs(d)
        with open(os.path.join(boxes, "fedora"), "wb") as f:
            f.write(b"x" * 8192)
        # Root's folder, not readable: one info line that says so, no finding.
        res = self.folders({system: None}, scandir_denied=(system,))
        self.assertEqual(res[0].status, doc_domain.INFO)
        self.assertIn("/var/lib/libvirt/images: NOCOW mark not readable as your user", res[0].message)
        self.assertIn("cst vm nocow plan --root", res[0].message)
        self.assertEqual(
            [r.message.split(":")[0] for r in res[1:]], [f"VM image folder {boxes}", f"VM image folder {boxes}"]
        )
        self.assertIn("no NOCOW mark (+C)", res[1].message)
        self.assertIn("still copy-on-write: fedora", res[2].message)
        self.assertTrue(all(r.status in (doc_domain.OK, doc_domain.INFO) for r in res))
        # All marked: ok lines only.
        nocow: int = inode_attrs.FS_NOCOW_FL
        res = self.folders({system: nocow, boxes: nocow, os.path.join(boxes, "fedora"): nocow})
        self.assertEqual([r.status for r in res], [doc_domain.OK, doc_domain.OK])
        # Not btrfs, or no folders at all (CI): silent.
        self.assertEqual(self.folders({}, btrfs=False), [])
        shutil.rmtree(os.path.join(self.root, "var"))
        shutil.rmtree(os.path.join(self.tmp, ".local"))
        self.assertEqual(self.folders({}), [])

    def test_image_folders_knob_off(self) -> None:
        os.makedirs(os.path.join(self.root, "etc", "cst"))
        with open(os.path.join(self.root, "etc", "cst", "vm.conf"), "w") as f:
            f.write("VM_NOCOW=no\n")
        res = self.folders({})
        self.assertEqual(
            [(r.status, r.message) for r in res],
            [(doc_domain.INFO, "VM image folders: NOCOW not managed (VM_NOCOW=no in /etc/cst/vm.conf)")],
        )

    def test_unwired_vm_tools_is_info(self) -> None:
        host = doc_adapters.Host(self.env, repo=REPO)
        res = list(doc_vm.check_vm_hardware(host, vm_tools=None, domains_fn=lambda host: (["win"], "")))
        self.assertEqual(res[0].status, doc_domain.INFO)
        self.assertIn("VM hardware: not checked (the vm package is not wired in)", res[0].message)
        self.assertEqual(list(doc_vm.check_vm_image_dirs(host, vm_tools=None)), [])

    def test_a_marked_folder_with_marked_images_is_ok_whole(self) -> None:
        boxes = os.path.join(self.tmp, ".local", "share", "gnome-boxes", "images")
        os.makedirs(boxes)
        with open(os.path.join(boxes, "fedora"), "wb") as f:
            f.write(b"x" * 8192)
        nocow: int = inode_attrs.FS_NOCOW_FL
        res = self.folders({boxes: nocow, os.path.join(boxes, "fedora"): nocow})
        self.assertEqual(res, [Result(doc_domain.OK, f"VM image folder {boxes}: NOCOW, no copy-on-write image")])


class CommandLineTest(Scratch):
    def test_report_into_a_closed_pipe_stops_quietly(self) -> None:
        # `cst doctor | head -1` ended in "Exception ignored ... BrokenPipeError"
        # (exit 120). The doctor only reads: it now ends on SIGPIPE, silently (its help too).
        for args in ([], ["help"]):
            with self.subTest(args=args):
                r, w = os.pipe()
                os.close(r)
                try:
                    p = subprocess.run(
                        ["python3", "-I", TOOL, *args],
                        check=False,
                        env=self.env,
                        stdout=w,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        timeout=60,
                    )
                finally:
                    os.close(w)
                self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""))

    def test_usage_and_help(self) -> None:
        r = self.run_tool("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        self.assertEqual(self.run_tool("--brief", "--brief").returncode, 2)
        r = self.run_tool("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Exit status", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_brief_shows_only_problems(self) -> None:
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

    def test_crashing_check_is_an_error_line(self) -> None:
        def boom(host: DoctorHostPort) -> Iterator[Result]:
            raise RuntimeError("bad")
            yield  # pragma: no cover

        res = doc_registry.run_checks(doc_adapters.Host(self.env, repo=REPO), [("x", boom)])
        self.assertEqual(res[0][1].status, doc_domain.ERROR)
        out = io.StringIO()
        self.assertEqual(doc_presenter.render(res, False, out), 1)
        self.assertIn("boom: the check itself failed (RuntimeError: bad)", out.getvalue())

    def test_fix_lines_are_only_a_command(self) -> None:
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

    def test_wired_checks_hand_the_vm_checks_the_vm_tools(self) -> None:
        calls: list[str] = []

        class Tools(doc_adapters.VmTools):
            @override
            def knobs(self, path: str) -> dict[str, bool]:
                calls.append(path)
                return {"VM_NOCOW": False}

        wired = doc_registry.wired_checks(Tools(vm_settings()))
        self.assertEqual([(a, fn.__name__) for a, fn in wired], [(a, fn.__name__) for a, fn in doc_registry.CHECKS])
        by_name = {fn.__name__: fn for _, fn in wired}
        host = doc_adapters.Host(self.env, repo=REPO)
        res = list(by_name["check_vm_image_dirs"](host))
        self.assertIn("NOCOW not managed", res[0].message)
        self.assertEqual(calls, [host.path("/etc/cst/vm.conf")])
        unwired = {fn.__name__: fn for _, fn in doc_registry.CHECKS}
        self.assertIs(by_name["check_staged"], unwired["check_staged"])

    def test_registry_order(self) -> None:
        areas: list[str] = []
        for a, _ in doc_registry.CHECKS:
            if a not in areas:
                areas.append(a)
        self.assertEqual(
            areas, ["rpm-ostree", "services", "alerts", "btrfs", "cpu", "swap", "vm", "responsiveness", "install"]
        )

    def test_every_module_that_registers_checks_is_an_area(self) -> None:
        # registry.CHECKS holds only the checks of the modules in AREAS: a module of doctor/application/
        # that registers checks (@check) and is missing there would drop them from the report unseen.
        app_dir = os.path.join(DOCTOR_DIR, "application")
        registering: list[str] = []
        for fn in sorted(os.listdir(app_dir)):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            module = importlib.import_module(f"doctor.application.{fn.removesuffix('.py')}")
            if module is not doc_registry and getattr(module, "CHECKS", None):
                registering.append(module.__name__)
        self.assertIn("doctor.application.vm", registering)  # the scan finds the area modules
        areas = [m.__name__ for m in doc_registry.AREAS]
        self.assertEqual(sorted(registering), sorted(areas))
        self.assertEqual(len(set(areas)), len(areas), "an area listed twice would print its checks twice")
        # One area name per module, and no two modules share one: the report starts a new "== area"
        # header each time the area changes, so a check registered under another module's name would
        # split that area in two.
        names = [{area for area, _ in m.CHECKS} for m in doc_registry.AREAS]
        self.assertEqual([len(n) for n in names], [1] * len(names), names)
        self.assertEqual(len(set().union(*names)), len(names), names)


class InstallAreaTest(Scratch):
    """The install area: the install records under the fixture root (written by the real installer's
    use case, staged there), the files as they are, and this checkout."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.host = doc_adapters.Host(self.env, repo=REPO)
        self.files = doc_adapters.InstalledFiles(REPO)
        self.svc = InstallService(
            LocalInstallTarget(), lambda e: entry_payload(REPO, e), record=JsonInstallRecord(), commit="c"
        )
        self.record = os.path.join(self.root, "var/lib/cst/installed.json")

    def install(self, component: str, **values: str) -> None:
        entries = [e for e in INSTALL_MANIFEST if e.component == component]
        list(self.svc.install(entries, InstallRequest(root=self.root, values=values or None)))

    def mark(self, component: str) -> None:
        self.svc.mark(self.record, component)

    def results(self) -> list[Result]:
        return list(doc_install.check_install(self.host, installed=self.files))

    def warns(self) -> list[Result]:
        return [r for r in self.results() if r.status == doc_domain.WARN]

    def test_a_complete_current_install_is_ok(self) -> None:
        self.install("btrfs:docker-gc")
        self.mark("btrfs:docker-gc")
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)
        self.assertIn("4 file(s) as CST installed them", res[0].message)

    def test_pending_changed_and_missing_are_warnings_with_the_refresh_command(self) -> None:
        self.install("btrfs:docker-gc")
        warns = self.warns()
        self.assertEqual(len(warns), 1, warns)
        # The cause it names holds for every way a component stays pending: an interrupted setup command, a
        # file of it written by another command before the setup command ever ran (cst btrfs scrub-limit), or
        # a file adopted, recorded without being written (test_a_pending_adopted_file_names_the_same_cause). A
        # component whose removal marks it pending first names that removal too
        # (test_a_component_a_removal_left_pending_names_the_removal_too); docker-gc's does not.
        self.assertEqual(
            warns[0].message,
            "btrfs:docker-gc: installed only in part (a file of it was recorded and "
            "`cst btrfs docker-gc enable` has not finished since)",
        )
        self.assertEqual(warns[0].fix, "cst btrfs docker-gc enable")
        self.mark("btrfs:docker-gc")
        with open(os.path.join(self.root, "etc/systemd/system/docker-gc.timer"), "a") as f:
            f.write("# edited\n")
        os.unlink(os.path.join(self.root, "etc/systemd/system/docker-gc.service"))
        msgs = sorted(r.message for r in self.warns())
        self.assertEqual(len(msgs), 2, msgs)
        self.assertIn("/etc/systemd/system/docker-gc.service: missing (CST installed it, btrfs:docker-gc)", msgs[0])
        self.assertIn("/etc/systemd/system/docker-gc.timer: differs from its install record", msgs[1])
        self.assertTrue(all(r.fix == "cst btrfs docker-gc enable" for r in self.warns()))

    def test_a_pending_adopted_file_names_the_same_cause(self) -> None:
        # An install from before the record: the files on disk as this checkout installs them, no record.
        # The install writes none of them (each is unchanged) and adopts them, so the component is pending
        # although no file of it was written.
        self.install("btrfs:docker-gc")
        os.unlink(self.record)
        self.install("btrfs:docker-gc")
        warns = self.warns()
        self.assertEqual(len(warns), 1, warns)
        self.assertEqual(
            warns[0].message,
            "btrfs:docker-gc: installed only in part (a file of it was recorded and "
            "`cst btrfs docker-gc enable` has not finished since)",
        )

    def test_a_component_a_removal_left_pending_names_the_removal_too(self) -> None:
        # cst btrfs iocost revert marks btrfs:iocost pending before it changes anything and may stop at a
        # removal that fails: its files were recorded by an apply that did finish. The warning names both
        # ways out (the fix line stays the one install command), so a user who was removing it is not sent
        # to turn it back on without a word.
        self.install("btrfs:iocost")
        self.mark("btrfs:iocost")
        self.assertEqual(self.warns(), [])
        self.svc.mark(self.record, "btrfs:iocost", ComponentState.PENDING)
        warns = self.warns()
        self.assertEqual(len(warns), 1, warns)
        self.assertEqual(
            warns[0].message,
            "btrfs:iocost: installed only in part (a file of it was recorded and `cst btrfs iocost apply` has not "
            "finished since, or `cst btrfs iocost revert` began removing it and has not finished since: run "
            "`cst btrfs iocost revert` again to finish the removal)",
        )
        self.assertEqual(warns[0].fix, "cst btrfs iocost apply")

    def test_an_install_older_than_this_checkout_is_info_only(self) -> None:
        # What an older CST installed: other bytes, recorded as such.
        self.install("btrfs:docker-gc")
        self.mark("btrfs:docker-gc")
        old = os.path.join(self.root, "etc/systemd/system/docker-gc.timer")
        with open(old, "a") as f:
            f.write("# older\n")
        with open(old, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        JsonInstallRecord().update(
            self.record,
            lambda rec: InstallRecord(
                {
                    **rec.files,
                    "/etc/systemd/system/docker-gc.timer": replace(
                        rec.files["/etc/systemd/system/docker-gc.timer"], sha256=sha
                    ),
                },
                rec.components,
            ),
        )
        res = self.results()
        self.assertEqual(self.warns(), [], res)
        older = [r for r in res if r.status == doc_domain.INFO]
        self.assertEqual(len(older), 1, res)
        self.assertIn(
            "btrfs:docker-gc: 1 file(s) older than this checkout (/etc/systemd/system/docker-gc.timer)",
            older[0].message,
        )
        self.assertIn("cst btrfs docker-gc enable installs them again", older[0].message)

    def test_a_template_is_compared_with_its_recorded_values(self) -> None:
        self.install("btrfs:top-mount", UUID="1111-2222", OPTIONS="subvolid=5")
        self.mark("btrfs:top-mount")
        self.assertEqual([r.status for r in self.results()], [doc_domain.OK])
        with open(os.path.join(self.root, "etc/systemd/system/var-mnt-btrfs\\x2dtop.mount"), "a") as f:
            f.write("# edited\n")
        self.assertEqual(len(self.warns()), 1)

    def test_a_changed_mode_is_info(self) -> None:
        self.install("btrfs:docker-gc")
        self.mark("btrfs:docker-gc")
        os.chmod(os.path.join(self.root, "etc/systemd/system/docker-gc.timer"), 0o600)
        res = self.results()
        self.assertIn(
            doc_domain.Result(doc_domain.INFO, "/etc/systemd/system/docker-gc.timer: mode 0600, CST installed it 0644"),
            res,
        )
        self.assertEqual(self.warns(), [])

    def test_a_unit_whose_program_is_gone_is_a_warning(self) -> None:
        self.install("btrfs:docker-gc")
        self.mark("btrfs:docker-gc")
        os.unlink(os.path.join(self.root, "usr/local/sbin/docker-gc"))
        msgs = [r.message for r in self.warns()]
        self.assertIn("/usr/local/sbin/docker-gc: missing (CST installed it, btrfs:docker-gc)", msgs)
        self.assertIn(
            "/etc/systemd/system/docker-gc.service runs /usr/local/sbin/docker-gc, which is missing or not executable",
            msgs,
        )

    def test_a_record_of_a_newer_cst_or_no_record_at_all(self) -> None:
        os.makedirs(os.path.dirname(self.record))
        with open(self.record, "w") as f:
            f.write('{"format": 3}')
        warns = self.warns()
        self.assertEqual(len(warns), 1)
        self.assertIn("written by a newer CST", warns[0].message)
        with open(self.record, "w") as f:
            f.write("{")
        self.assertIn("no install record", self.warns()[0].message)
        # No record: an install from before it, compared with this checkout, as INFO per component.
        os.unlink(self.record)
        self.assertEqual(self.results(), [], "nothing installed, nothing to say")
        gc = os.path.join(self.root, "usr/local/sbin/docker-gc")
        os.makedirs(os.path.dirname(gc))
        with open(gc, "w") as f:
            f.write("#!/usr/bin/python3\n# a plain-script copy of an older CST\n")
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.INFO], res)
        self.assertIn("btrfs:docker-gc: installed before the install record; 1 of 1 file(s) differ", res[0].message)

    def unreadable(self, path: str, why: str) -> Result:
        return Result(
            doc_domain.WARN,
            f"{path}: the install record cannot be read ({why}): every CST install and removal stops at it, "
            "after it has changed its files; move it aside",
        )

    def test_a_folder_in_the_place_of_a_record_is_a_warning(self) -> None:
        # The installer leaves such a record out and stops at its update, after its files were written.
        os.makedirs(self.record)
        self.assertEqual(self.warns(), [self.unreadable("/var/lib/cst/installed.json", "Is a directory")])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("/var/lib/cst/installed.json: the install record cannot be read (Is a directory)", r.stdout)
        os.rmdir(self.record)
        user = self.root + self.env["HOME"] + "/.local/state/cst/installed.json"
        os.makedirs(user)
        self.assertEqual(
            self.warns(), [self.unreadable(self.env["HOME"] + "/.local/state/cst/installed.json", "Is a directory")]
        )

    def test_a_file_in_the_place_of_the_records_folder_is_a_warning(self) -> None:
        os.makedirs(os.path.dirname(os.path.dirname(self.record)), exist_ok=True)
        open(os.path.dirname(self.record), "w").close()  # /var/lib/cst a file
        self.assertEqual(self.warns(), [self.unreadable("/var/lib/cst/installed.json", "Not a directory")])

    def test_a_read_error_is_a_warning_and_a_refusal_stays_info(self) -> None:
        home_record = self.env["HOME"] + "/.local/state/cst/installed.json"
        with mock.patch.object(self.files, "record", side_effect=OSError(errno.EIO, "Input/output error")):
            res = self.results()
        self.assertEqual(
            res,
            [
                self.unreadable("/var/lib/cst/installed.json", "Input/output error"),
                self.unreadable(home_record, "Input/output error"),
            ],
        )
        # A record this user may not read (its folder's mode) is no fault: info, as before this check
        # warned on a record that cannot be read (this assertion passes on the old code too).
        with mock.patch.object(self.files, "record", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            res = self.results()
        self.assertEqual(
            res,
            [
                Result(doc_domain.INFO, "/var/lib/cst/installed.json: not readable as your user (Permission denied)"),
                Result(doc_domain.INFO, f"{home_record}: not readable as your user (Permission denied)"),
            ],
        )

    def test_an_absent_record_is_no_unreadable_record(self) -> None:
        self.assertNotIn("cannot be read", "\n".join(r.message for r in self.results()))
        self.assertNotIn("cannot be read", self.run_tool().stdout)

    def test_without_a_checkout_to_compare_with_nothing_is_said_to_differ(self) -> None:
        # desired_sha256 cannot say (no checkout): a plain-script copy is not reported as differing.
        gc = os.path.join(self.root, "usr/local/sbin/docker-gc")
        os.makedirs(os.path.dirname(gc))
        with open(gc, "w") as f:
            f.write("#!/usr/bin/python3\n# a plain-script copy of an older CST\n")
        res = list(doc_install.check_install(self.host, installed=doc_adapters.InstalledFiles("")))
        self.assertEqual(res, [])

    def test_a_component_the_record_does_not_list_is_compared_as_before_the_record(self) -> None:
        # The record lists docker-gc; swap was installed by an older CST and is not in it.
        self.install("btrfs:docker-gc")
        self.mark("btrfs:docker-gc")
        old = os.path.join(self.root, "etc/sysctl.d/99-swap-tuning.conf")
        os.makedirs(os.path.dirname(old))
        with open(old, "w") as f:
            f.write("vm.swappiness = 60\n")
        infos = [r.message for r in self.results() if r.status == doc_domain.INFO]
        self.assertEqual(len(infos), 1, infos)
        self.assertIn("swap:core: installed before the install record; 1 of 1 file(s) differ", infos[0])

    def test_the_users_record_in_the_home(self) -> None:
        home = self.env["HOME"]
        entries = [e for e in INSTALL_MANIFEST if e.component == "bluetooth:ldac"]
        list(self.svc.install(entries, InstallRequest(root=self.root, home=home)))
        self.svc.mark(self.root + home + "/.local/state/cst/installed.json", "bluetooth:ldac")
        self.assertEqual([r.status for r in self.results()], [doc_domain.OK])

    def launcher(self) -> tuple[str, str]:
        """The stall-report launcher's destination in the fixture home, and the user's record (both staged)."""
        home = self.env["HOME"]
        return home + "/.local/bin/btrfs-stall-report", self.root + home + "/.local/state/cst/installed.json"

    def test_a_file_that_differs_from_its_record_is_not_called_a_hand_edit(self) -> None:
        # An install wrote the launcher again (new text) and could not update its record line (its folder
        # not writable, a full disk): the line still has the older sha. Two causes fit; the WARN names both.
        dest, record = self.launcher()
        place = Placement(
            path=self.root + dest,
            dest=dest,
            record=record,
            component="btrfs:report-launcher",
            src=None,
            kind=GeneratedKind.TEXT,
            owner=Owner.USER,
            mode=0o755,
        )
        self.svc.put(place, b"#!/bin/sh\n# older\n", as_root=False)
        self.svc.mark(record, "btrfs:report-launcher")
        with open(self.root + dest, "wb") as f:
            f.write(b"#!/bin/sh\n# newer\n")
        want = (
            f"{dest}: differs from its install record (btrfs:report-launcher): changed by hand, or written "
            "by an install that could not update the record"
        )
        self.assertEqual([(r.message, r.fix) for r in self.warns()], [(want, "cst btrfs install")])

    def test_a_launcher_without_a_record_line_is_neither_warned_about_nor_counted_ok(self) -> None:
        # The first install wrote the launcher and could not record it: no line, so doctor makes no claim.
        home = self.env["HOME"]
        dest, record = self.launcher()
        entries = [e for e in INSTALL_MANIFEST if e.component == "bluetooth:ldac"]
        list(self.svc.install(entries, InstallRequest(root=self.root, home=home)))
        self.svc.mark(record, "bluetooth:ldac")
        os.makedirs(os.path.dirname(self.root + dest))
        with open(self.root + dest, "wb") as f:
            f.write(b"#!/bin/sh\n")
        res = self.results()
        self.assertEqual([r.status for r in res], [doc_domain.OK], res)
        self.assertNotIn(dest, "\n".join(r.message for r in res))
        self.assertIn(f"{len(entries)} file(s) as CST installed them", res[0].message)

    def test_a_vm_backup_timer_without_btrfs_notify_warns(self) -> None:
        wants = os.path.join(self.root, "etc/systemd/system/timers.target.wants")
        os.makedirs(wants)
        res = list(doc_vm.check_vm_backup_alerts(self.host))
        self.assertEqual(res, [])
        open(os.path.join(wants, "cst-vm-backup@win.timer"), "w").close()
        res = list(doc_vm.check_vm_backup_alerts(self.host))
        self.assertEqual(len(res), 1)
        self.assertEqual((res[0].status, res[0].fix), (doc_domain.WARN, "cst btrfs install"))
        # cst vm sends through cst_core's notify adapter, which keeps the alert for the alerts area.
        self.assertIn(
            "a failed NAS backup of the VM reaches no desktop, only the journal and the pending alerts above",
            res[0].message,
        )
        self.install("btrfs:alerts")
        self.assertEqual(list(doc_vm.check_vm_backup_alerts(self.host)), [])

    def test_units_that_alert_without_btrfs_notify_warn(self) -> None:
        # A refusal of btrfs-bulk-boot (a bad bulk.conf line) alerts through btrfs-notify; without it the
        # alert reached nobody. Now it is kept for this doctor, which also says why no desktop shows it.
        self.assertEqual(list(doc_alerts.check_alert_program(self.host)), [])
        self.install("btrfs:snapshots")
        res = list(doc_alerts.check_alert_program(self.host))
        self.assertEqual(len(res), 1, res)
        self.assertEqual((res[0].status, res[0].fix), (doc_domain.WARN, "cst btrfs install"))
        self.assertTrue(
            res[0].message.startswith(
                "btrfs-bulk-boot.service, btrfs-snapshot.service, btrfs-snapshot-nas.service alert through "
                "/usr/local/sbin/btrfs-notify, which is missing"
            ),
            res[0].message,
        )
        # btrfs-snapshot-nas's alerts go through cst_core's Notifier too (the runtime's NAS run);
        # btrfs-snapshot.service's conversion step is bash, whose alerts are not kept.
        self.assertTrue(
            res[0].message.endswith(
                "; btrfs-bulk-boot.service, btrfs-snapshot-nas.service: also the pending alerts above"
            ),
            res[0].message,
        )
        self.install("btrfs:alerts")
        self.assertEqual(list(doc_alerts.check_alert_program(self.host)), [])

    def test_units_whose_alerts_are_not_kept_are_not_said_to_be(self) -> None:
        # The health check and the stall alert are bash: without btrfs-notify their alerts reach only the
        # journal, and the line must not send the reader to the pending alerts for them.
        units = os.path.join(self.root, "etc/systemd/system")
        os.makedirs(units, exist_ok=True)
        for name in ("btrfs-health-check.service", "btrfs-stall-alert.service"):  # btrfs:core's
            open(os.path.join(units, name), "w").close()
        [res] = list(doc_alerts.check_alert_program(self.host))
        self.assertIn("btrfs-health-check.service", res.message)
        self.assertNotIn("btrfs-bulk-boot", res.message)
        self.assertNotIn("pending alerts", res.message)
        self.assertTrue(res.message.endswith("their alerts reach no desktop, only the journal"), res.message)

    def test_unit_exec_paths(self) -> None:
        text = (
            "[Service]\nExecStartPre=-/usr/sbin/btrfs x\nExecStart=/usr/bin/python3 -I /usr/local/sbin/a run\n"
            "ExecStopPost=-%h/.local/lib/cst/b\nEnvironment=X=/usr/local/c\n"
            # Folders, not programs: never "missing or not executable".
            "ExecSearchPath=/usr/local/sbin:/usr/bin\nExecPaths=/usr/local/sbin\n"
        )
        self.assertEqual(doc_domain.unit_exec_paths(text, "/home/u"), ["/usr/local/sbin/a", "/home/u/.local/lib/cst/b"])
        self.assertEqual(doc_domain.unit_exec_paths(text, ""), ["/usr/local/sbin/a"])
        self.assertTrue(doc_domain.is_unit_file("/etc/systemd/system/x.service"))
        self.assertFalse(doc_domain.is_unit_file("/etc/systemd/system/x.service.d/10.conf"))
        self.assertFalse(doc_domain.is_unit_file("/etc/sysctl.d/90.conf"))

    def test_the_cli_wires_the_install_area(self) -> None:
        self.install("btrfs:docker-gc")
        r = self.run_tool()
        self.assertIn("btrfs:docker-gc: installed only in part", r.stdout)

    def put(self, dest: str, content: bytes) -> None:
        """A file at an installed path under the fixture root (~ is the fixture home)."""
        p = self.root + dest.replace("~", self.env["HOME"], 1)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)

    def test_without_a_record_templates_the_rustc_wrapper_and_recorded_components_are_not_compared(self) -> None:
        # A template needs the install's values; the rustc wrapper is written by toolchains itself.
        self.put("/etc/polkit-1/rules.d/50-cpu-setup-game.rules", b"changed\n")
        self.put("~/.local/libexec/cst/rustc-wrapper", b"changed\n")
        self.assertEqual(self.results(), [])
        # Not only because this checkout cannot judge them: with an answer for every source, still nothing.
        with mock.patch.object(self.files, "desired_sha256", return_value="0" * 64):
            self.assertEqual(self.results(), [])
        self.put("/etc/sysctl.d/99-swap-tuning.conf", b"vm.swappiness = 60\n")
        self.assertEqual(
            list(doc_install._without_record(self.host, self.files, Owner.ROOT, frozenset({"swap:core"}))), []
        )
        self.assertEqual(list(doc_install._without_record(self.host, self.files, Owner.USER)), [])
        self.assertEqual(len(list(doc_install._without_record(self.host, self.files, Owner.ROOT))), 1)

    def test_without_a_record_only_the_files_this_checkout_can_judge_count(self) -> None:
        # One file as this checkout builds it, one that differs, and one this checkout cannot say about:
        # 1 of 2 differs.
        entries = {e.src: e for e in INSTALL_MANIFEST if e.component == "swap:core"}
        same = entries["swap/tmpfiles/zswap.conf"]
        self.put(same.dest, entry_payload(REPO, same))
        self.put("/etc/sysctl.d/99-swap-tuning.conf", b"vm.swappiness = 60\n")
        unknown = entries["swap/units/machine.slice.d-10-swap-memory-low.conf"]
        self.put(unknown.dest, b"changed\n")
        real = self.files.desired_sha256
        with mock.patch.object(
            self.files,
            "desired_sha256",
            side_effect=lambda src, values: None if src == unknown.src else real(src, values),
        ):
            res = self.results()
        self.assertEqual(
            res,
            [
                Result(
                    doc_domain.INFO,
                    "swap:core: installed before the install record; 1 of 2 file(s) differ from this checkout "
                    "(/etc/sysctl.d/99-swap-tuning.conf): cst swap apply installs them again and records them",
                )
            ],
        )


class UnitsLoadedTest(Scratch):
    """check_units_loaded: the units of the unit files and drop-ins CST recorded that systemd has loaded and
    not loaded again since (NeedDaemonReload yes in one `systemctl show --state=loaded '*'` per manager)."""

    QUERY = "systemctl show --state=loaded -p Id -p LoadState -p NeedDaemonReload *"
    USER_QUERY = "systemctl --user show --state=loaded -p Id -p LoadState -p NeedDaemonReload *"

    @override
    def setUp(self) -> None:
        super().setUp()
        self.host = doc_adapters.Host(self.env, repo=REPO)
        self.files = doc_adapters.InstalledFiles(REPO)
        self.svc = InstallService(
            LocalInstallTarget(), lambda e: entry_payload(REPO, e), record=JsonInstallRecord(), commit="c"
        )
        self.home = self.env["HOME"]
        self.root_record = os.path.join(self.root, "var/lib/cst/installed.json")
        self.user_record = self.root + self.home + "/.local/state/cst/installed.json"

    def record(self, *dests: str, user: bool = False) -> None:
        """Each dest written under the scratch root by the installer's use case and listed in root's record
        (user: the user's), its component done."""
        rec = self.user_record if user else self.root_record
        for dest in dests:
            place = Placement(
                path=self.root + dest,
                dest=dest,
                record=rec,
                component="e2e:units",
                src=None,
                kind=GeneratedKind.TEXT,
                owner=Owner.USER if user else Owner.ROOT,
                mode=0o644,
            )
            self.svc.put(place, b"[Service]\n", as_root=False)
        self.svc.mark(rec, "e2e:units")

    def answer(self, *units: tuple[str, str], user: bool = False, load: str = "loaded") -> None:
        """What `systemctl [--user] show --state=loaded ... '*'` prints: one block per (Id, NeedDaemonReload)."""
        folder = os.path.join(self.env["FIX_SHOW"], "usr" if user else "sys")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "*"), "w") as f:
            f.write(
                "\n".join(
                    f"Id={i}\nLoadState={load}\nActiveState=inactive\nNeedDaemonReload={need}\n" for i, need in units
                )
            )

    def results(self) -> list[Result]:
        return list(doc_install.check_units_loaded(self.host, installed=self.files))

    def warning(self, unit_id: str, *dests: str, fix: str = "sudo systemctl daemon-reload") -> Result:
        return Result(
            doc_domain.WARN,
            f"{unit_id}: systemd has not loaded its files as they are on disk (CST installed {', '.join(dests)}): "
            "this manager has not been reloaded since they changed (a command stopped before its daemon-reload "
            "or reloaded only another user's manager, or a hand edit); systemd runs it as it loaded it before",
            fix,
        )

    def test_recorded_unit(self) -> None:
        sys_, usr = doc_domain.UnitScope.SYSTEM, doc_domain.UnitScope.USER
        home = "/home/u"
        cases: list[tuple[str, str, tuple[doc_domain.UnitScope, str] | None]] = [
            ("/etc/systemd/system/docker-gc.timer", "", (sys_, "docker-gc.timer")),
            ("/etc/systemd/system/x.service.d/10-cst.conf", "", (sys_, "x.service")),
            ("/etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf", "", (sys_, "user@.service")),
            ("/etc/systemd/system/var-mnt-btrfs\\x2dtop.mount", "", (sys_, "var-mnt-btrfs\\x2dtop.mount")),
            ("/etc/systemd/user/cst-pending-alerts.service", "", (usr, "cst-pending-alerts.service")),
            ("/etc/systemd/user/cst-pending-alerts.service", home, (usr, "cst-pending-alerts.service")),
            ("/home/u/.config/systemd/user/cst-toolchains-prune.timer", home, (usr, "cst-toolchains-prune.timer")),
            ("/home/u/.config/systemd/user/user.slice.d/10-cpu-setup.conf", home, (usr, "user.slice")),
            ("/home/u/.config/systemd/user/x.service", "", None),  # root's record has no home of a user
            ("/home/v/.config/systemd/user/x.service", home, None),  # another home
            ("/etc/systemd/journald.conf.d/10-cst-write-budget.conf", "", None),
            ("/etc/systemd/system/timers.target.wants/x.timer", "", None),
            ("/etc/systemd/system/x.service.d/README", "", None),
            ("/etc/systemd/system/service.d/10.conf", "", None),
            ("/etc/systemd/system/x.service.d/sub/10.conf", "", None),
            ("/run/systemd/system/x.service", "", None),
            ("/etc/systemd/system/10-cst.conf", "", None),
            ("/usr/local/sbin/docker-gc", "", None),
        ]
        for dest, h, want in cases:
            with self.subTest(dest=dest, home=h):
                self.assertEqual(doc_domain.recorded_unit(dest, h), want)
        # The folder the user's manager names (its UnitPath): cst bluetooth writes the LDAC watcher's unit there.
        cfg = "/home/u/.cfg/systemd/user"
        ldac = cfg + "/cst-bt-ldac48.service"
        for dest, h, user_dir, want in (
            (ldac, home, cfg, (usr, "cst-bt-ldac48.service")),
            (ldac, home, cfg + "/", (usr, "cst-bt-ldac48.service")),
            (cfg + "/x.service.d/10.conf", home, cfg, (usr, "x.service")),
            (ldac, home, "", None),
            (ldac, home, "/home/u/.cfg", None),  # the config folder itself loads no unit
            (ldac, "", cfg, None),  # root's record: no folder of a user
            ("/home/u/.config/systemd/user/x.service", home, cfg, (usr, "x.service")),
        ):
            with self.subTest(dest=dest, home=h, user_dir=user_dir):
                self.assertEqual(doc_domain.recorded_unit(dest, h, user_dir), want)

    def test_needs_user_unit_dir(self) -> None:
        home = "/home/u"
        for dest, h, want in (
            ("/home/u/.cfg/systemd/user/cst-bt-ldac48.service", home, True),
            ("/home/u/.cfg/systemd/user/x.service.d/10.conf", home, True),
            ("/home/u/.config/systemd/user/x.service", home, False),  # placed without asking
            ("/etc/systemd/user/x.service", home, False),
            ("/home/u/.cfg/systemd/user/x.service", "", False),  # root's record
            ("/home/u/.cfg/systemd/user/README", home, False),  # no unit file
            ("/home/u/.cfg/systemd/user/x.service.d/README", home, False),  # no drop-in
            ("/home/u/.cfg/systemd/user/x.service.d/sub/10.conf", home, False),
            ("/home/u/.cfg/systemd/user.control/x.service", home, False),
            ("/home/u/.local/bin/x.service", home, False),
        ):
            with self.subTest(dest=dest, home=h):
                self.assertEqual(doc_domain.needs_user_unit_dir(dest, h), want)

    def test_unit_applies(self) -> None:
        for name, unit_id, want in (
            ("x.service", "x.service", True),
            ("x.service", "y.service", False),
            ("x.service", "x.service.d", False),
            ("user@.service", "user@1000.service", True),
            ("user@.service", "user@.service", False),
            ("user@.service", "user@1000.socket", False),
            ("user@.service", "xuser@1.service", False),
            ("cst-vm-backup@win.timer", "cst-vm-backup@win.timer", True),
            ("cst-vm-backup@win.timer", "cst-vm-backup@other.timer", False),
            ("@.service", "@x.service", False),
        ):
            with self.subTest(name=name, unit_id=unit_id):
                self.assertEqual(doc_domain.unit_applies(name, unit_id), want)

    def test_a_drop_in_systemd_has_not_loaded_is_a_warning_with_the_reload(self) -> None:
        dropin = "/etc/systemd/system/x.service.d/10-cst.conf"
        self.record(dropin)
        self.answer(("x.service", "yes"), ("other.service", "yes"), ("y.service", "no"))
        self.assertEqual(self.results(), [self.warning("x.service", dropin)])
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        inst = area_lines(r.stdout, "install")
        line = [n for n, s in enumerate(inst) if "systemd has not loaded" in s]
        self.assertEqual(len(line), 1, inst)
        self.assertTrue(inst[line[0]].lstrip().startswith("WARN"), inst)
        self.assertIn(
            f"x.service: systemd has not loaded its files as they are on disk (CST installed {dropin})", inst[line[0]]
        )
        self.assertEqual(inst[line[0] + 1].strip(), "fix: sudo systemctl daemon-reload")
        self.assertNotIn("other.service", r.stdout)  # never a unit the record does not name
        self.assertIn(self.QUERY, self.calls())
        self.assertNotIn(self.USER_QUERY, self.calls())  # no unit of the user's manager recorded
        self.assert_read_only()

    def test_a_unit_systemd_has_loaded_as_it_is_says_nothing(self) -> None:
        self.record("/etc/systemd/system/x.service", "/etc/systemd/system/x.service.d/10-cst.conf")
        self.answer(("x.service", "no"))
        self.assertEqual(self.results(), [])
        self.assertIn(self.QUERY, self.calls())

    def test_a_unit_file_and_its_drop_in_are_one_warning_naming_both(self) -> None:
        unit, dropin = "/etc/systemd/system/x.timer", "/etc/systemd/system/x.timer.d/10-cst.conf"
        self.record(unit, dropin)
        self.answer(("x.timer", "yes"))
        self.assertEqual(self.results(), [self.warning("x.timer", unit, dropin)])

    def test_more_than_three_files_are_cut_short(self) -> None:
        dests = [f"/etc/systemd/system/x.service.d/{n}-cst.conf" for n in (10, 20, 30, 40)]
        self.record(*dests)
        self.answer(("x.service", "yes"))
        [res] = self.results()
        self.assertIn(f"(CST installed {', '.join(dests[:3])}, ...)", res.message)

    def test_a_template_drop_in_names_each_instance(self) -> None:
        dropin = "/etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf"
        self.record(dropin)
        self.answer(("user@1000.service", "yes"), ("user@1001.service", "no"), ("user-1000.slice", "yes"))
        self.assertEqual(self.results(), [self.warning("user@1000.service", dropin)])

    def test_a_unit_with_a_drop_in_of_the_game_profile_is_left_out(self) -> None:
        # The profile writes /run/systemd/system/NAME.d/zz-cst-game.conf with no reload on purpose: the flag
        # is its own, the cpu rows cover it.
        dropin = "/etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf"
        self.record(dropin)
        self.answer(("user@1000.service", "yes"))
        self.assertEqual(self.results(), [self.warning("user@1000.service", dropin)])
        game = os.path.join(self.root, "run/systemd/system/user@1000.service.d")
        os.makedirs(game)
        with open(os.path.join(game, "zz-cst-game.conf"), "w") as f:
            f.write("[Service]\nMemoryLow=1G\n")
        self.assertEqual(self.results(), [])

    def test_a_user_unit_names_the_user_managers_reload(self) -> None:
        unit = self.home + "/.config/systemd/user/cst-toolchains-prune.timer"
        self.record(unit, user=True)
        self.answer(("cst-toolchains-prune.timer", "yes"), user=True)
        self.answer(("cst-toolchains-prune.timer", "yes"))  # the system manager's answer is never asked
        self.assertEqual(
            self.results(), [self.warning("cst-toolchains-prune.timer", unit, fix="systemctl --user daemon-reload")]
        )
        self.assertIn(self.USER_QUERY, self.calls())
        self.assertNotIn(self.QUERY, self.calls())
        r = self.run_tool()
        self.assertIn("fix: systemctl --user daemon-reload", r.stdout)
        self.assert_read_only()

    UNIT_PATH_QUERY = "systemctl --user show -p UnitPath --value"

    def unit_path(self, *folders: str) -> None:
        """What the user manager answers for its UnitPath: its control folder, then folders, then the
        global one (no file: it does not answer)."""
        folder = os.path.join(self.env["FIX_SHOW"], "usr")
        os.makedirs(folder, exist_ok=True)
        entries = [self.home + "/.config/systemd/user.control", *folders, "/etc/systemd/user", "/usr/lib/systemd/user"]
        with open(os.path.join(folder, "UnitPath"), "w") as f:
            f.write(" ".join(entries) + "\n")

    def test_a_user_unit_in_the_folder_the_user_manager_names_is_checked(self) -> None:
        # The manager reads HOME/xdg/systemd/user (its own XDG_CONFIG_HOME); this doctor's environment has no
        # XDG_CONFIG_HOME at all, as under `sudo cst status`'s step down.
        self.env.pop("XDG_CONFIG_HOME", None)
        self.host = doc_adapters.Host(self.env, repo=REPO)
        folder = self.home + "/xdg/systemd/user"
        unit, dropin = folder + "/cst-bt-ldac48.service", folder + "/x.service.d/10-cst.conf"
        self.record(unit, dropin, user=True)
        self.unit_path(folder, self.home + "/.config/systemd/user")
        self.answer(("cst-bt-ldac48.service", "yes"), ("x.service", "yes"), user=True)
        self.assertEqual(
            self.results(),
            [
                self.warning("cst-bt-ldac48.service", unit, fix="systemctl --user daemon-reload"),
                self.warning("x.service", dropin, fix="systemctl --user daemon-reload"),
            ],
        )
        self.assertEqual(self.calls().count(self.UNIT_PATH_QUERY), 1)  # asked once per check-up
        r = self.run_tool()
        self.assertIn("cst-bt-ldac48.service: systemd has not loaded", r.stdout)
        self.assert_read_only()

    def test_a_folder_the_user_manager_does_not_name_is_not_checked(self) -> None:
        # The shell's XDG_CONFIG_HOME names the folder, the manager does not read it: no unit there is loaded.
        self.env["XDG_CONFIG_HOME"] = self.home + "/xdg"
        self.host = doc_adapters.Host(self.env, repo=REPO)
        unit = self.home + "/xdg/systemd/user/cst-bt-ldac48.service"
        self.record(unit, user=True)
        self.unit_path(self.home + "/.config/systemd/user")
        self.answer(("cst-bt-ldac48.service", "yes"), user=True)
        self.assertEqual(self.results(), [])
        self.assertIn(self.UNIT_PATH_QUERY, self.calls())
        self.assertNotIn(self.USER_QUERY, self.calls())  # no unit of the user's manager recorded

    def test_a_user_manager_that_does_not_answer_leaves_its_two_folders(self) -> None:
        unit, ours = (
            self.home + "/xdg/systemd/user/cst-bt-ldac48.service",
            "/etc/systemd/user/cst-pending-alerts.service",
        )
        self.record(unit, user=True)
        self.record(ours)
        self.answer(("cst-bt-ldac48.service", "yes"), ("cst-pending-alerts.service", "yes"), user=True)
        self.assertEqual(
            self.results(), [self.warning("cst-pending-alerts.service", ours, fix="systemctl --user daemon-reload")]
        )
        self.assertIn(self.UNIT_PATH_QUERY, self.calls())

    def test_the_user_manager_s_folder_is_asked_only_for_a_unit_no_other_folder_places(self) -> None:
        self.record(
            self.home + "/.config/systemd/user/cst-toolchains-prune.timer", self.home + "/.local/bin/x", user=True
        )
        self.record("/etc/systemd/user/cst-pending-alerts.service")
        self.unit_path(self.home + "/.config/systemd/user")
        self.answer(("cst-toolchains-prune.timer", "no"), user=True)
        self.assertEqual(self.results(), [])
        self.assertNotIn(self.UNIT_PATH_QUERY, self.calls())

    def test_as_root_the_user_manager_s_folder_is_not_asked(self) -> None:
        unit = self.home + "/xdg/systemd/user/cst-bt-ldac48.service"
        self.record(unit, user=True)
        self.unit_path(self.home + "/xdg/systemd/user")
        self.answer(("cst-bt-ldac48.service", "yes"), user=True)
        self.host.uid = 0
        self.assertEqual(self.results(), [])
        self.assertNotIn(self.UNIT_PATH_QUERY, self.calls())

    def test_a_unit_of_root_s_record_in_etc_systemd_user_asks_the_user_manager(self) -> None:
        unit = "/etc/systemd/user/cst-pending-alerts.service"
        self.record(unit)
        self.answer(("cst-pending-alerts.service", "yes"), user=True)
        self.assertEqual(
            self.results(), [self.warning("cst-pending-alerts.service", unit, fix="systemctl --user daemon-reload")]
        )

    def test_as_root_no_user_manager_is_asked(self) -> None:
        # Root's record names a unit of the user managers and one of the system's; root's own user manager
        # (a login of root starts it) would answer, but root's doctor asks the system manager only.
        unit, dropin = "/etc/systemd/user/cst-pending-alerts.service", "/etc/systemd/system/x.service.d/10-cst.conf"
        self.record(unit, dropin)
        self.answer(("cst-pending-alerts.service", "yes"), user=True)
        self.answer(("x.service", "yes"))
        self.host.uid = 0
        self.assertEqual(self.results(), [self.warning("x.service", dropin)])
        calls = self.calls()
        self.assertIn(self.QUERY, calls)
        self.assertNotIn(self.USER_QUERY, calls)

    def test_no_unit_file_recorded_asks_no_manager(self) -> None:
        # A record with no unit file and no drop-in (PR #29's lesson: no question where nothing is set up).
        self.record("/usr/local/sbin/e2e-units", "/etc/systemd/journald.conf.d/10-cst-write-budget.conf")
        self.answer(("x.service", "yes"))
        self.answer(("x.service", "yes"), user=True)
        self.assertEqual(self.results(), [])
        r = self.run_tool()
        self.assertNotIn("systemd has not loaded", r.stdout)
        calls = self.calls()
        self.assertFalse([c for c in calls if "--state=loaded" in c], calls)

    def test_no_record_asks_no_manager(self) -> None:
        self.answer(("x.service", "yes"))
        self.assertEqual(self.results(), [])
        self.run_tool()
        calls = self.calls()
        self.assertFalse([c for c in calls if "--state=loaded" in c], calls)

    def test_both_checks_judge_each_record_as_it_was_read_once(self) -> None:
        # An install that rewrites a record between the install check and this one cannot make the two
        # disagree: each record is read once per check-up.
        dropin = "/etc/systemd/system/x.service.d/10-cst.conf"
        self.record(dropin)
        self.answer(("x.service", "yes"))
        reads: list[str] = []

        class Counting(doc_adapters.InstalledFiles):
            @override
            def record(self, path: doc_domain.ViewPath) -> InstallRecord | None:
                reads.append(path)
                return super().record(path)

        files = Counting(REPO)
        install = list(doc_install.check_install(self.host, installed=files))
        self.assertIn(doc_domain.OK, [r.status for r in install])
        os.unlink(self.root_record)  # the record changes after the install check read it
        units = list(doc_install.check_units_loaded(self.host, installed=files))
        self.assertEqual(units, [self.warning("x.service", dropin)])
        self.assertEqual(sorted(reads), sorted([self.root_record, self.user_record]))

    def test_a_record_that_cannot_be_read_gives_no_line_here(self) -> None:
        os.makedirs(self.root_record)
        self.answer(("x.service", "yes"))
        self.assertEqual(self.results(), [])
        self.assertEqual(self.calls(), [])

    def test_a_manager_that_cannot_be_asked_says_nothing(self) -> None:
        self.record("/etc/systemd/system/x.service.d/10-cst.conf")
        self.assertEqual(self.results(), [])  # no fixture: the stub exits 1
        self.assertIn(self.QUERY, self.calls())

    def test_a_unit_that_is_not_loaded_is_not_trusted_to_the_filter(self) -> None:
        self.record("/etc/systemd/system/x.service.d/10-cst.conf")
        self.answer(("x.service", "yes"), load="not-found")
        self.assertEqual(self.results(), [])

    def test_the_scrub_unit_is_left_to_the_scrub_row(self) -> None:
        unit, dropin = (
            "/etc/systemd/system/btrfs-scrub-weekly.service",
            ("/etc/systemd/system/btrfs-scrub-weekly.service.d/10-cst-limit.conf"),
        )
        self.record(unit, dropin)
        self.answer(("btrfs-scrub-weekly.service", "yes"))
        self.assertEqual(self.results(), [])
        # Without its unit file the scrub row asks nothing, so this check does.
        os.unlink(self.root + unit)
        self.assertEqual(self.results(), [self.warning("btrfs-scrub-weekly.service", unit, dropin)])

    def test_in_a_toolbox_the_query_runs_on_the_host(self) -> None:
        self.env["CST_DOCTOR_CONTAINER"] = "1"
        dropin = "/etc/systemd/system/x.service.d/10-cst.conf"
        self.record(dropin)
        self.answer(("x.service", "yes"))
        r = self.run_tool()
        self.assertIn(f"flatpak-spawn --host --env=LC_ALL=C {self.QUERY}", self.calls())
        inst = area_lines(r.stdout, "install")
        n = next(n for n, s in enumerate(inst) if "x.service: systemd has not loaded" in s)
        self.assertEqual(inst[n + 1].strip(), "fix (on the host): sudo systemctl daemon-reload")
        self.assert_read_only()


class ToolboxTest(Scratch):
    def test_commands_go_through_flatpak_spawn(self) -> None:
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

    def test_fixes_say_on_the_host_in_a_toolbox(self) -> None:
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

    def test_default_root_in_a_toolbox(self) -> None:
        env = dict(self.env)
        del env["CST_DOCTOR_ROOT"]
        env["CST_DOCTOR_CONTAINER"] = "1"
        self.assertEqual(doc_adapters.Host(env).path("/var/lib/cst-alerts"), "/run/host/var/lib/cst-alerts")
        env["CST_DOCTOR_CONTAINER"] = "0"
        self.assertEqual(doc_adapters.Host(env).path("/var/lib/cst-alerts"), "/var/lib/cst-alerts")

    def test_virsh_runs_in_the_c_locale(self) -> None:
        # The checks compare virsh's answers ("running", "shut off"), which it translates ("desligado" in pt_BR).
        with open(os.path.join(self.bin, "virsh"), "w") as f:
            f.write('#!/bin/sh\necho "LC_ALL=${LC_ALL-unset} $*"\n')
        env = dict(self.env, LANG="pt_BR.UTF-8", LC_ALL="pt_BR.UTF-8")
        want = (0, "LC_ALL=C -r -c qemu:///system domstate vm1\n", "")
        self.assertEqual(doc_vm.virsh(doc_adapters.Host(env), "domstate", "vm1"), want)
        # In a toolbox the host command does not get flatpak-spawn's environment: the setting goes in --env.
        self.assertEqual(doc_vm.virsh(doc_adapters.Host(dict(env, CST_DOCTOR_CONTAINER="1")), "domstate", "vm1"), want)
        self.assertEqual(self.calls(), ["flatpak-spawn --host --env=LC_ALL=C virsh -r -c qemu:///system domstate vm1"])

    def test_missing_flatpak_spawn(self) -> None:
        # The Host's own PATH finds the commands (only the stub folder here: no real flatpak-spawn).
        host = doc_adapters.Host(dict(self.env, CST_DOCTOR_CONTAINER="1", PATH=self.bin))
        os.unlink(os.path.join(self.bin, "flatpak-spawn"))
        rc, _, err = host.run(["systemctl", "--failed"])
        self.assertEqual(rc, 127)
        self.assertIn("flatpak-spawn", err)

    def test_the_texts_of_a_command_that_did_not_run(self) -> None:
        # What the checks show (first_line of err) when a query is missing, cannot start or hangs.
        host = doc_adapters.Host(dict(self.env, CST_DOCTOR_CONTAINER="0"))
        self.assertEqual(
            host.run(["cst-no-such-command", "a"]),
            (127, "", "cst-no-such-command: not found (No such file or directory)"),
        )
        with open(os.path.join(self.bin, "plain"), "w") as f:
            f.write("not a program\n")
        self.assertEqual(host.run(["plain"]), (126, "", "plain: Permission denied"))
        with open(os.path.join(self.bin, "slow"), "w") as f:
            f.write("#!/bin/sh\nexec sleep 5\n")
        os.chmod(os.path.join(self.bin, "slow"), 0o755)
        self.assertEqual(host.run(["slow", "x"], timeout=0.3), (124, "", "slow x: no answer within 0.3 s"))
        self.assertEqual(doc_vm.virsh_problem(*host.run(["slow"], timeout=0.3)[::2]), "slow: no answer within 0.3 s")


class HermeticTest(Scratch):
    """No command the doctor starts may reach the real host: each one, and
    the command flatpak-spawn --host would run, resolves in the stub folder
    through the PATH the command really gets (the Host's env)."""

    def run_recorded(self, container: str) -> list[tuple[str, str | None]]:
        """Every check in-process; [(command, where it resolves)] of what it
        started. A check that crashed (also on a command the recorder could
        not take) fails the test: it would hide its commands."""
        real_run = subprocess.run
        seen: list[tuple[str, str | None]] = []

        def recording_run(
            args: Sequence[str],
            *,
            check: bool,
            capture_output: bool,
            text: bool,
            timeout: float,
            stdin: int,
            env: Mapping[str, str] | None = None,
            errors: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            path = (os.environ if env is None else env).get("PATH")
            args = list(args)
            spawned = (
                [a for a in args[2:] if not a.startswith("--env=")] if args[:2] == ["flatpak-spawn", "--host"] else []
            )
            names = [args[0], *spawned[:1]]
            found = [(n, shutil.which(n, path=path)) for n in names]
            seen.extend(found)
            if any(w is None or os.path.dirname(w) != self.bin for _, w in found):
                return subprocess.CompletedProcess(args, 127, "", "not run: outside the stub folder")
            if not text:
                raise AssertionError(f"{args}: the doctor runs its commands in text mode")
            return real_run(
                args,
                check=check,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
                stdin=stdin,
                env=env,
                errors=errors,
            )

        os.makedirs(self.virsh_dir, exist_ok=True)
        with open(os.path.join(self.virsh_dir, "list.txt"), "w") as f:
            f.write("vm1\n")
        self.env.update(CST_DOCTOR_CONTAINER=container, FIX_VIRSH=self.virsh_dir)
        host = doc_adapters.Host(self.env, repo=REPO)
        with mock.patch.object(subprocess, "run", recording_run):
            results = doc_registry.run_checks(host, doc_registry.wired_checks(doc_adapters.VmTools(vm_settings())))
        crashed = [r.message for _, r in results if "the check itself failed" in r.message]
        self.assertEqual(crashed, [])
        return seen

    @override
    def setUp(self) -> None:
        super().setUp()
        self.virsh_dir = os.path.join(self.tmp, "virsh")

    def test_every_command_is_a_stub(self) -> None:
        for container in ("0", "1"):
            with self.subTest(container=container):
                seen = self.run_recorded(container)
                self.assertTrue(seen, "the doctor ran no command at all")
                outside = [(n, w) for n, w in seen if w is None or os.path.dirname(w) != self.bin]
                self.assertEqual(outside, [], "commands that would reach the real host")
                self.assertIn("rpm-ostree", {n for n, _ in seen})
                self.assertIn("virsh", {n for n, _ in seen})


class CstIntegrationTest(Scratch):
    """`cst doctor` and the doctor section of `cst status`, on a copy of cst
    with no module scripts (so status runs no module and never asks sudo)."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "doctor"))
        for pkg in ("btrfs", "cst_core", "cpu", "perf", "swap", "vm"):  # doctor imports these packages
            shutil.copytree(
                os.path.join(REPO, pkg), os.path.join(self.repo, pkg), ignore=shutil.ignore_patterns("tests")
            )
        shutil.copy(CST, self.repo)
        shutil.copy(TOOL, os.path.join(self.repo, "doctor"))
        for fn in os.listdir(DOCTOR_DIR):
            if fn.endswith(".py"):
                shutil.copy(os.path.join(DOCTOR_DIR, fn), os.path.join(self.repo, "doctor"))
        shutil.copytree(
            os.path.join(DOCTOR_DIR, "application"),
            os.path.join(self.repo, "doctor", "application"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.cst = os.path.join(self.repo, "cst")
        self.env["CST_NAME"] = "cst"

    def test_cst_doctor(self) -> None:
        self.status("pinned.json")
        r = self.run_tool(cmd=["bash", self.cst, "doctor"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cst doctor: read-only check-up", r.stdout)
        self.assertEqual(self.run_tool("--nope", cmd=["bash", self.cst, "doctor"]).returncode, 2)
        self.assertEqual(self.run_tool(cmd=["bash", self.cst, "doctor", "--brief"]).returncode, 1)

    def test_cst_status_ends_with_doctor(self) -> None:
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

    def test_cst_help_lists_doctor(self) -> None:
        r = self.run_tool(cmd=["bash", self.cst, "help"])
        self.assertIn("cst doctor [--brief]", r.stdout)


class RenderTest(unittest.TestCase):
    """The report's text, whole: the full report and --brief, on the host and in a toolbox."""

    RESULTS = (
        ("rpm-ostree", Result(doc_domain.OK, "a")),
        ("rpm-ostree", Result(doc_domain.WARN, "b", "fix b")),
        ("vm", Result(doc_domain.ERROR, "c")),
        ("vm", Result(doc_domain.INFO, "d")),
    )
    SUMMARY = "doctor: needs attention: 1 ERROR, 1 WARN, 1 info, 1 ok\n"
    FOOTER = "Each fix is a suggestion: read it before you run it. Details: doctor/README.md in CST.\n"
    TOOLBOX = (
        "Inside a toolbox: run every command shown here on the host (a host terminal, or put flatpak-spawn "
        "--host in front of a command without sudo).\n"
    )

    def render(self, results: Sequence[tuple[str, Result]], *, brief: bool, container: bool) -> tuple[int, str]:
        out = io.StringIO()
        rc = doc_presenter.render(results, brief, out, container, prog="cst doctor")
        return rc, out.getvalue()

    def test_the_full_report(self) -> None:
        for container, label, toolbox in ((False, "fix:", ""), (True, "fix (on the host):", self.TOOLBOX)):
            with self.subTest(container=container):
                self.assertEqual(
                    self.render(self.RESULTS, brief=False, container=container),
                    (
                        1,
                        (
                            "cst doctor: read-only check-up (nothing is changed)\n"
                            f"{toolbox}\n== rpm-ostree\n  ok     a\n  WARN   b\n         {label} fix b\n"
                            f"\n== vm\n  ERROR  c\n  info   d\n\n{self.SUMMARY}{self.FOOTER}"
                        ),
                    ),
                )

    def test_the_brief_report(self) -> None:
        for container, label in ((False, "fix:"), (True, "fix (on the host):")):
            with self.subTest(container=container):
                self.assertEqual(
                    self.render(self.RESULTS, brief=True, container=container),
                    (1, f"{self.SUMMARY}  WARN   [rpm-ostree] b\n         {label} fix b\n  ERROR  [vm] c\n"),
                )

    def test_all_good_has_no_footer(self) -> None:
        out = io.StringIO()
        rc = doc_presenter.render([("vm", Result(doc_domain.OK, "a"))], brief=False, out=out)
        self.assertEqual(
            (rc, out.getvalue()),
            (
                0,
                (
                    "cst-doctor: read-only check-up (nothing is changed)\n\n== vm\n  ok     a\n\n"
                    "doctor: all good: 0 ERROR, 0 WARN, 0 info, 1 ok\n"
                ),
            ),
        )


class SettingsWarningsTest(Scratch):
    """settings_file_warnings' note after "take nothing from the line": its default for a file bash also
    sources, none for a file only Python reads, or the caller's."""

    def warnings(self, *, bash_sourced: bool, note: str | None = None) -> list[str]:
        d = os.path.join(self.root, "etc", "cst")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "vm.conf"), "w") as f:
            f.write("FOO=$X\n")
        host = doc_adapters.Host(self.env, repo=REPO)
        path = doc_domain.HostPath("/etc/cst/vm.conf")
        return [r.message for r in doc_shared.settings_file_warnings(host, path, bash_sourced=bash_sourced, note=note)]

    def test_the_note(self) -> None:
        rest = ", and the cst commands that change the machine with these settings refuse to run until it is fixed"
        line = "/etc/cst/vm.conf line 1: FOO=$X"
        for bash_sourced, note, said in (
            (True, None, " (bash, which also sources this file, reads it its own way)"),
            (False, None, ""),
            (True, " (mine)", " (mine)"),
            (False, "", ""),
        ):
            with self.subTest(bash_sourced=bash_sourced, note=note):
                ws = self.warnings(bash_sourced=bash_sourced, note=note)
                self.assertEqual(len(ws), 1, ws)
                self.assertTrue(ws[0].startswith(line), ws)
                self.assertTrue(ws[0].endswith(f". CST's Python programs take nothing from the line{said}{rest}"), ws)

    def test_a_blank_beside_a_value_has_the_kept_wording_where_bash_sources_the_file(self) -> None:
        # The line keeps bash's value in a file bash sources, so "take nothing from the line" would be false
        # there; in vm.conf, which only Python reads, the line sets nothing and keeps the old wording.
        d = os.path.join(self.root, "etc", "cst")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "vm.conf"), "w", encoding="utf-8") as f:
            f.write("FOO='8\N{NO-BREAK SPACE}'\n")
        host = doc_adapters.Host(self.env, repo=REPO)
        path = doc_domain.HostPath("/etc/cst/vm.conf")
        why = "a no-break space (U+00A0) at the end of the value: CST does not trim it (delete it)"
        kept, plain = (
            [r.message for r in doc_shared.settings_file_warnings(host, path, bash_sourced=b)] for b in (True, False)
        )
        self.assertEqual(
            kept,
            [
                (
                    f"/etc/cst/vm.conf line 1: FOO='8\N{NO-BREAK SPACE}': {why}. CST's Python programs read the "
                    "value with the blank, as bash does, and the cst commands that change the machine with these "
                    "settings refuse to run until it is fixed"
                )
            ],
        )
        self.assertEqual(len(plain), 1, plain)
        self.assertIn(f"{why}. CST's Python programs take nothing from the line, and", plain[0])


class DoctorDomainTest(unittest.TestCase):
    """Pure domain functions exposed by `doctor.domain`."""

    def test_parse_alert_and_mtime_ns(self) -> None:
        self.assertEqual(
            doc_domain.parse_alert_text("cst-alert 1\nurgency=critical\ntitle=Disk error\nlast=123\n\nbody"),
            ("critical", "Disk error", 123),
        )
        self.assertIsNone(doc_domain.parse_alert_text("unknown 1\nurgency=critical\ntitle=x\n"))
        # A last= in digits CST does not write (a hand-edited or damaged file) reads as 0: int() refuses the
        # first and past its digit limit, and reads the Arabic-Indic three as 3.
        for last in ("\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}", "1" * 5000):
            with self.subTest(last=last[:12]):
                self.assertEqual(
                    doc_domain.parse_alert_text(
                        f"cst-alert 1\nurgency=critical\ntitle=Disk error\nlast={last}\n\nbody"
                    ),
                    ("critical", "Disk error", 0),
                )
        self.assertTrue(doc_domain.mtime_matches_ns("1000.000000042", 1000 * 1_000_000_000 + 42))
        self.assertFalse(doc_domain.mtime_matches_ns("1001.0", 1000 * 1_000_000_000))

    def test_parse_unalloc_thresholds_and_conf_value(self) -> None:
        self.assertEqual(
            doc_domain.parse_unalloc_thresholds_text("UNALLOC_MIN_GIB=42\nUNALLOC_CRIT_GIB=3\n"),
            (42, 3),
        )
        self.assertIsNone(doc_domain.parse_unalloc_thresholds_text("UNALLOC_MIN_GIB=42\n"))

    def test_parse_cpu_stat_and_offered_features(self) -> None:
        self.assertEqual(doc_domain.parse_cpu_stat_seconds("nr_periods 5\nusage_usec 2500000\n"), 2.5)
        self.assertIsNone(doc_domain.parse_cpu_stat_seconds("nr_periods 5\n"))
        # usage_usec is ASCII digits within int()'s limit: "²" (int() refuses it), "٣" (int() reads 3) and
        # 5000 digits are no number, and a later line that is one still counts.
        for bad in ("\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}", "9" * 5000):
            with self.subTest(value=bad[:8]):
                self.assertIsNone(doc_domain.parse_cpu_stat_seconds(f"usage_usec {bad}\n"))
                self.assertEqual(doc_domain.parse_cpu_stat_seconds(f"usage_usec {bad}\nusage_usec 1000000\n"), 1.0)
        # Within int()'s limit but too large for a float (about 308 digits): no number either, not the
        # OverflowError of the division out of cst doctor's cpu check.
        self.assertIsNone(doc_domain.parse_cpu_stat_seconds("usage_usec " + "9" * 400 + "\n"))
        self.assertEqual(doc_domain.parse_cpu_stat_seconds("usage_usec " + "9" * 400 + "\nusage_usec 1000000\n"), 1.0)
        feats, err = doc_domain.parse_offered_features_xml(CAPS_QEMU11)
        self.assertEqual(err, "")
        self.assertIsNotNone(feats)
        assert feats is not None
        self.assertIn("cet-ss", feats)
        bad_feats, bad_err = doc_domain.parse_offered_features_xml("<broken")
        self.assertIsNone(bad_feats)
        self.assertIn("unreadable", bad_err)

    def test_summary_and_parse_args(self) -> None:
        line, rc = doc_domain.summary([("btrfs", Result(doc_domain.OK, "fine"))])
        self.assertEqual(rc, 0)
        self.assertIn("all good", line)
        self.assertEqual(doc_domain.parse_args(["help"]), ("help", False))
        self.assertEqual(doc_domain.parse_args(["--brief"]), ("run", True))
        with self.assertRaises(Usage):
            doc_domain.parse_args(["--bogus"])


class HostAccountsTest(unittest.TestCase):
    """The Host adapter answers the account and machine questions the checks
    used to ask pwd and os.uname directly (fakes: pwd stubs, no real accounts)."""

    def test_user_name_and_id(self) -> None:
        host = doc_adapters.Host({"CST_DOCTOR_ROOT": "/nonexistent"}, repo=REPO)
        entry = types.SimpleNamespace(pw_name="alice", pw_uid=4242)
        by_uid = {4242: entry}
        by_name = {"alice": entry}
        with (
            mock.patch.object(doc_adapters.pwd, "getpwuid", lambda u: by_uid[u]),
            mock.patch.object(doc_adapters.pwd, "getpwnam", lambda n: by_name[n]),
        ):
            self.assertEqual(host.user_name(4242), "alice")
            self.assertEqual(host.user_name(1), "")
            self.assertEqual(host.user_id("alice"), 4242)
            self.assertIsNone(host.user_id("nobody-here"))
        self.assertEqual(host.arch, os.uname().machine)


class PackageRulesEdgeTest(unittest.TestCase):
    """parse_nevra, classify_requests and virt_changes on the cases a mutation run left unchecked."""

    def test_parse_nevra_refuses_malformed(self) -> None:
        p = doc_domain.parse_nevra
        self.assertIsNone(p("foo-1.x86_64"))  # no release
        self.assertIsNone(p("-1-1.fc45.x86_64"))  # no name
        self.assertIsNone(p("a-1:-4.x86_64"))  # an epoch and no version
        self.assertEqual(p("a-1:2:3-4.x86_64"), ("a", "1", "2:3", "4", "x86_64"))  # only the first colon is the epoch
        # An epoch is ASCII digits: "²" and "٣" pass isdigit() but are no epoch rpm writes.
        self.assertIsNone(p("a-\N{SUPERSCRIPT TWO}:2-4.x86_64"))
        self.assertIsNone(p("a-\N{ARABIC-INDIC DIGIT THREE}:2-4.x86_64"))

    def test_classify_multilib_and_unreadable_entries(self) -> None:
        c = doc_domain.classify_requests
        pkgs = ["garbage", "wine-core-9-1.fc45.i686", "wine-core-9-1.fc45.x86_64"]
        # An unreadable entry is skipped, not the end of the list.
        self.assertEqual(c(["wine-core-9-1.fc45"], pkgs, "x86_64"), [("wine-core-9-1.fc45", "wine-core", "pinned")])
        # Neither installed arch is the native one: the first installed arch is named.
        self.assertEqual(
            c(["wine-core-9-1.fc45"], pkgs, "aarch64"), [("wine-core-9-1.fc45", "wine-core.i686", "pinned")]
        )

    def test_virt_changes_counts_the_rest(self) -> None:
        old = {"qemu-kvm": "1", "a": "1", "b": "1", "c": "1"}
        new = {"qemu-kvm": "2", "a": "2", "b": "2", "c": "2"}
        self.assertEqual(doc_domain.virt_changes(old, new), "qemu-kvm 1 -> 2, and 3 more virtualization package(s)")


class DoctorFactsTest(unittest.TestCase):
    """What several checks ask (the domains, each definition, rpm-ostree) is
    asked once per Host and kept by the application, not on the host port."""

    @override
    def setUp(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def host(self) -> doc_adapters.Host:
        tmp = tempfile.mkdtemp(prefix="cst-doctor-facts-")
        self.addCleanup(shutil.rmtree, tmp)
        host = doc_adapters.Host({"PATH": tmp, "CST_DOCTOR_CONTAINER": "0", "CST_DOCTOR_ROOT": tmp})
        calls = self.calls

        class Virsh:
            def call(self, *args: str, timeout: float | None = 60) -> CommandResult:
                calls.append(args)
                return CommandResult(0, "win\n" if args[0] == "list" else "<domain/>", "")

        def virsh(*, read_only: bool) -> Virsh:
            return Virsh()

        mock.patch.object(host, "virsh", virsh).start()
        self.addCleanup(mock.patch.stopall)
        return host

    def test_each_host_is_asked_once(self) -> None:
        a, b = self.host(), self.host()
        for _ in range(2):
            self.assertEqual(doc_vm.libvirt_domains(a), (["win"], ""))
            self.assertEqual(doc_vm.domain_xml(a, "win"), (0, "<domain/>", ""))
        self.assertEqual(self.calls, [("list", "--all", "--name"), ("dumpxml", "--inactive", "win")])
        doc_vm.libvirt_domains(b)
        self.assertEqual(len(self.calls), 3, "a new Host starts with no answers")

    def test_the_answers_are_not_kept_on_the_host(self) -> None:
        host = self.host()
        doc_vm.libvirt_domains(host)
        doc_vm.domain_xml(host, "win")
        doc_shared.ostree_facts(host)
        self.assertEqual([n for n in vars(host) if n in ("_domains", "_dumpxml", "_ostree", "_ostree_list")], [])


class NotUtf8OutputTest(Scratch):
    """Host.run and Host.virsh: bytes that are not UTF-8 become U+FFFD (they raised UnicodeDecodeError)."""

    def test_run_and_virsh(self) -> None:
        bytes_program(self.bin, "virsh", b"win\xff\n")
        host = doc_adapters.Host(dict(self.env, CST_DOCTOR_CONTAINER="0"))
        self.assertEqual(host.run(["printf", "\\377abc"]), (0, "\ufffdabc", ""))
        self.assertEqual(host.virsh(read_only=True).domains(), ["win\ufffd"])


class HostProbeTest(unittest.TestCase):
    """Host: CST_DOCTOR_CONTAINER wins; without it, cst_core's detection (a probe) tells."""

    class _Probe:
        def __init__(self, *, container: bool) -> None:
            self.container = container

        def is_container(self) -> bool:
            return self.container

    def test_the_probe_decides_without_the_override(self) -> None:
        for container, root in ((True, "/run/host"), (False, "/")):
            host = doc_adapters.Host({"PATH": "/usr/bin:/bin"}, probe=self._Probe(container=container))
            self.assertEqual((host.container, host.root, host.is_container()), (container, root, container))
        for forced, container in (("1", True), ("0", False)):
            host = doc_adapters.Host({"CST_DOCTOR_CONTAINER": forced}, probe=self._Probe(container=not container))
            self.assertEqual(host.container, container)


class DoctorWiringTest(unittest.TestCase):
    """doctor/wiring.py: what cst doctor takes from the process (the composition root)."""

    def test_the_host_knows_the_checkout_the_doctor_runs_from(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "CST_DOCTOR_CONTAINER": "0"}
        host = doc_wiring.host(env, os.path.join(DOCTOR_DIR, "cst-doctor"))
        self.assertEqual(host.repo_path("cst"), os.path.join(os.path.realpath(REPO), "cst"))
        nowhere = doc_wiring.host(env, "/nonexistent/doctor/cst-doctor")
        self.assertEqual(nowhere.repo_path("cst"), "/nonexistent-cst-checkout/cst")

    def test_main_reads_the_environment_when_asked(self) -> None:
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CST_PROG": "my doctor"}, clear=True):
            rc = doc_cli.main(
                ["help"], doc="usage: doctor [--brief]", entry_file=os.path.join(DOCTOR_DIR, "cst-doctor"), out=out
            )
        self.assertEqual((rc, out.getvalue()), (0, "usage: my doctor [--brief]\n"))


class ResponsivenessAreaTest(Scratch):
    """Doctor's `responsiveness` area delegates cleanly to `perf` diagnosis rules."""

    def _write_host(self, rel: str, content: str) -> None:
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_empty_root_emits_no_responsiveness_findings(self) -> None:
        res = self.run_tool()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("responsiveness:", res.stdout)

    def test_unconfigured_host_skips_responsiveness_even_with_kernel_telemetry(self) -> None:
        self._write_host("proc/meminfo", "MemTotal: 67108864 kB\nMemAvailable: 50000000 kB\n")
        self._write_host("proc/sys/vm/min_free_kbytes", "65536\n")
        self._write_host("sys/kernel/mm/lru_gen/enabled", "0x0007\n")
        self._write_host("sys/kernel/mm/lru_gen/min_ttl_ms", "0\n")
        self._write_host("sys/module/zswap/parameters/enabled", "N\n")
        self._write_host("sys/module/nvme_core/parameters/default_ps_max_latency_us", "100000\n")
        self._write_host("sys/class/drm/card0/device/uevent", "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:84:00.0\n")
        self._write_host("sys/class/drm/card0/card0-DP-1/status", "connected\n")
        self._write_host("sys/class/drm/card0/device/power/control", "auto\n")
        self._write_host("etc/uresourced.conf", "[Global]\nMaxMemoryMin=10%\n")
        res = self.run_tool()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("0 ERROR, 0 WARN", res.stdout)
        self.assertNotIn("responsiveness", res.stdout)

        # CST-managed /etc/uresourced.conf activates the memory responsiveness axis.
        self._write_host(
            "etc/uresourced.conf", "# Managed by swap-setup.sh (CST): uresourced\n[ActiveUser]\nMemoryMin=1G\n"
        )
        cst_res = self.run_tool()
        self.assertEqual(cst_res.returncode, 1, cst_res.stdout + cst_res.stderr)
        self.assertIn("WARN   memory: zswap is disabled", cst_res.stdout)
        self.assertIn("fix: cst swap apply", cst_res.stdout)

    def test_healthy_and_flagged_host_states(self) -> None:
        self._write_host("etc/sysctl.d/99-swap-tuning.conf", "vm.swappiness = 60\n")
        self._write_host("proc/meminfo", "MemTotal: 67108864 kB\nMemAvailable: 50000000 kB\n")
        self._write_host("proc/sys/vm/min_free_kbytes", "262144\n")
        self._write_host("sys/kernel/mm/transparent_hugepage/defrag", "always [defer] defer+madvise madvise never\n")
        self._write_host("sys/kernel/mm/lru_gen/enabled", "0x0007\n")
        self._write_host("sys/kernel/mm/lru_gen/min_ttl_ms", "1000\n")
        self._write_host("sys/module/zswap/parameters/enabled", "Y\n")

        ok_res = self.run_tool()
        self.assertEqual(ok_res.returncode, 0, ok_res.stdout + ok_res.stderr)
        self.assertIn("responsiveness: 1 axes checked, no bottlenecks", ok_res.stdout)

        # Advisory cpu item -> info (no fix command); dangling invocation symlink counts as active!
        self._write_host(
            "etc/tmpfiles.d/cpu-setup-hwp-boost.conf",
            "w /sys/devices/system/cpu/cpufreq/boost - - - - 1\n",
        )
        self._write_host("dev/ntsync", "")
        os.makedirs(os.path.join(self.root, "run/systemd/units"), exist_ok=True)
        os.symlink(
            "deadbeef00112233445566778899aabb",
            os.path.join(self.root, "run/systemd/units/invocation:irqbalance.service"),
        )
        self._write_host("proc/cmdline", "quiet preempt=voluntary\n")
        info_res = self.run_tool()
        self.assertEqual(info_res.returncode, 0, info_res.stdout + info_res.stderr)
        self.assertIn("info   cpu: Kernel preemption is 'voluntary'", info_res.stdout)
        self.assertNotIn("irqbalance service is inactive", info_res.stdout)

        self._write_host("etc/sysctl.d/90-btrfs-writeback.conf", "vm.dirty_bytes = 268435456\n")
        self._write_host("sys/module/nvme_core/parameters/default_ps_max_latency_us", "100000\n")
        nvme_res = self.run_tool()
        self.assertEqual(nvme_res.returncode, 1, nvme_res.stdout + nvme_res.stderr)
        self.assertIn("WARN   io: nvme_core default_ps_max_latency_us=100000", nvme_res.stdout)
        self.assertIn("fix: cst btrfs install", nvme_res.stdout)

        self._write_host("sys/class/drm/card0/device/uevent", "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:84:00.0\n")
        self._write_host("sys/class/drm/card0/card0-DP-1/status", "connected\n")
        self._write_host("sys/class/drm/card0/device/power/control", "auto\n")
        gpu_res = self.run_tool()
        self.assertEqual(gpu_res.returncode, 1, gpu_res.stdout + gpu_res.stderr)
        self.assertIn("WARN   gpu: Connected display GPU card0 (amdgpu, DP-1) has power/control=auto", gpu_res.stdout)
        self.assertIn("fix: cst cpu install", gpu_res.stdout)

        self._write_host("sys/kernel/mm/lru_gen/min_ttl_ms", "0\n")
        mglru_res = self.run_tool()
        self.assertEqual(mglru_res.returncode, 1, mglru_res.stdout + mglru_res.stderr)
        self.assertIn("WARN   memory: MGLRU min_ttl_ms is 0", mglru_res.stdout)
        self.assertIn("fix: cst swap apply", mglru_res.stdout)

        self._write_host("sys/kernel/mm/lru_gen/min_ttl_ms", "1000\n")
        self._write_host("sys/kernel/mm/transparent_hugepage/defrag", "always defer [defer+madvise] madvise never\n")
        thp_res = self.run_tool()
        self.assertEqual(thp_res.returncode, 1, thp_res.stdout + thp_res.stderr)
        self.assertIn("WARN   memory: THP defrag is 'defer+madvise'", thp_res.stdout)
        self.assertIn("fix: cst swap apply", thp_res.stdout)

        self._write_host("sys/module/zswap/parameters/enabled", "N\n")
        warn_res = self.run_tool()
        self.assertEqual(warn_res.returncode, 1, warn_res.stdout + warn_res.stderr)
        self.assertIn("WARN   memory: zswap is disabled", warn_res.stdout)
        self.assertIn("fix: cst swap apply", warn_res.stdout)

    def test_phase4_and_cst_prog_fix_rewrite(self) -> None:
        self._write_host("etc/cpu-setup/cpu-setup.conf", "BUILD_CPUS='12-23'\n")
        self._write_host("etc/sysctl.d/90-btrfs-writeback.conf", "vm.dirty_bytes = 268435456\n")
        os.makedirs(os.path.join(self.root, "run/systemd/units"), exist_ok=True)
        os.symlink(
            "deadbeef00112233445566778899aabb",
            os.path.join(self.root, "run/systemd/units/invocation:irqbalance.service"),
        )
        self._write_host("sys/block/nvme0n1/queue/scheduler", "[none]\n")
        self._write_host("sys/block/nvme0n1/queue/rotational", "0\n")
        self._write_host("sys/fs/cgroup/io.cost.qos", "")
        self._write_host("sys/fs/cgroup/builds.slice/io.weight", "default 20\n")
        self.env["CST_PROG"] = "./cst doctor"
        p4_res = self.run_tool()
        self.assertEqual(p4_res.returncode, 1, p4_res.stdout + p4_res.stderr)
        self.assertNotIn("irqbalance service is inactive", p4_res.stdout)
        self.assertIn("fix: ./cst cpu irq apply", p4_res.stdout)
        self.assertIn("fix: ./cst btrfs iocost apply", p4_res.stdout)
        self.assertNotIn("fix: sudo cst ", p4_res.stdout)


if __name__ == "__main__":
    unittest.main()
