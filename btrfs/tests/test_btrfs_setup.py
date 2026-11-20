# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the timer removal paths of btrfs-setup.sh and
btrfs/setup-snapshots.bash (bash, so the functions are taken from the
scripts and run with systemctl and rm as shell functions that log; rm
removes only files under the test's folder). systemd keeps the last run of
a Persistent= timer (a stamp in /var/lib/systemd/timers) after the timer is
gone; `systemctl clean --what=state` removes it, and needs the unit loaded
and stopped: after `disable --now`, before the unit file goes.

Nothing reaches the real host: no sudo, no systemctl, writes only in a
temporary folder.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_btrfs_setup -v
"""

import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import TypedDict, override
import unittest
import zipfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BTRFS = os.path.dirname(HERE)
REPO = os.path.dirname(BTRFS)
SETUP = os.path.join(REPO, "btrfs-setup.sh")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from btrfs.docker_gc import wiring as gc_wiring
from cst_core.adapters.files import read_text
from cst_core.domain.install import INSTALL_MANIFEST
from cst_core.domain.record import USER_RECORD_REL
from cst_core.domain.shapes import json_object, object_of, parse_json
from cst_core.domain.text import parse_shell_assignments, parse_snapshots_conf
from doctor.domain import scrub_dropin_limit, scrub_limit_setting
from tests.shell_source import shell_function

# The log says, for each clean, whether the unit file was still there.
STUBS = """
set -Eeuo pipefail
systemctl() {
    echo "systemctl $*" >>"$LOG"
    if [[ "$1" == clean && -e "${UNIT_DIR}/${!#}" ]]; then echo "  (${!#} file present)" >>"$LOG"; fi
    if [[ " $* " == *" ${FAIL:-none} "* ]]; then echo "Failed to $1 unit: $*" >&2; return 1; fi
}
rm() {
    echo "rm $*" >>"$LOG"
    local a r=-f
    for a in "$@"; do
        if [[ "$a" == -*r* ]]; then r=-rf; fi
    done
    for a in "$@"; do
        if [[ "$a" == "$T"/* ]]; then command rm "$r" -- "$a"; fi
    done
}
"""


def assignment(name: str) -> str:
    """The line (or lines, for an array) that sets NAME at the top of btrfs-setup.sh."""
    with open(SETUP) as f:
        text = f.read()
    m = re.search(rf"^{name}=(\(.*?\)|[^\n]*)$", text, re.MULTILINE | re.DOTALL)
    if m is None:
        raise AssertionError(f"{name}= not found in {SETUP}")
    return m.group(0)


class TimerRemovalTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-btrfs-setup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.units = os.path.join(self.tmp, "units")
        self.stamps = os.path.join(self.tmp, "timers")
        os.makedirs(self.units)
        os.makedirs(self.stamps)
        self.log = os.path.join(self.tmp, "log")

    def touch(self, *paths: str) -> None:
        for p in paths:
            open(p, "w").close()

    def run_bash(self, body: str, fail: str = "none") -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                STUBS,
                shell_function(SETUP, "forget_timer_state"),
                shell_function(SETUP, "disable_timers"),
                f'UNIT_DIR="$T/units"; TIMER_STAMP_DIR="$T/timers"; SRC={BTRFS}; PROG=cst-btrfs-test',
                body,
            ]
        )
        r = subprocess.run(
            ["bash", "-c", script, "_"],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LOG": self.log,
                "T": self.tmp,
                "FAIL": fail,
                "TMPDIR": self.tmp,
            },
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def calls(self) -> list[str]:
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_old_units_are_stopped_forgotten_then_removed(self) -> None:
        service, timer = (os.path.join(self.units, f"btrfs-scrub-monthly.{k}") for k in ("service", "timer"))
        stamp = os.path.join(self.stamps, "stamp-btrfs-scrub-monthly.timer")
        self.touch(service, timer, stamp)
        self.run_bash(
            "\n".join([assignment("OLD_UNITS"), shell_function(SETUP, "remove_old_units"), "remove_old_units"])
        )
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now btrfs-scrub-monthly.service",
                f"rm -fv {service}",
                "systemctl disable --now btrfs-scrub-monthly.timer",
                "systemctl clean --what=state btrfs-scrub-monthly.timer",
                "  (btrfs-scrub-monthly.timer file present)",
                f"rm -fv {timer}",
                f"rm -fv {stamp}",
            ],
        )
        for p in (service, timer, stamp):
            self.assertFalse(os.path.exists(p), p)

    def test_a_stamp_whose_unit_is_already_gone_is_removed(self) -> None:
        # What an older install left on this machine: the unit files removed, the stamp not.
        stamp = os.path.join(self.stamps, "stamp-btrfs-scrub-monthly.timer")
        self.touch(stamp)
        body = "\n".join([assignment("OLD_UNITS"), shell_function(SETUP, "remove_old_units"), "remove_old_units"])
        self.run_bash(body)
        self.assertEqual(self.calls(), [f"rm -fv {stamp}"])
        self.assertFalse(os.path.exists(stamp))
        # A second run finds nothing left to change.
        os.unlink(self.log)
        self.run_bash(body)
        self.assertNotIn("systemctl", " ".join(self.calls()))

    def test_forget_timer_state_cleans_only_timers_and_ignores_a_failure(self) -> None:
        # On a service, --what=state would delete its StateDirectory.
        r = self.run_bash("forget_timer_state a.service b.timer docker-gc-recorder.service\necho went-on", fail="clean")
        self.assertEqual(self.calls(), ["systemctl clean --what=state b.timer"])
        self.assertIn("went-on", r.stdout)

    def test_disable_timers_disables_each_unit_then_forgets_only_the_timers(self) -> None:
        # Every disable fails: only x.timer has a unit file, so only its failure is news.
        self.touch(os.path.join(self.units, "x.timer"))
        r = self.run_bash("disable_timers x.timer x.service gone.timer\necho went-on", fail="disable")
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now x.timer",
                "systemctl disable --now x.service",
                "systemctl disable --now gone.timer",
                "systemctl clean --what=state x.timer",
                "  (x.timer file present)",
                "systemctl clean --what=state gone.timer",
            ],
        )
        self.assertIn("went-on", r.stdout)
        self.assertEqual(
            r.stderr, "WARNING: could not disable x.timer: Failed to disable unit: disable --now x.timer\n"
        )

    def test_disable_timers_a_missing_unit_does_not_keep_the_others_enabled(self) -> None:
        # One systemctl call for all three failed as a whole on gone.timer (no unit file):
        # the timer and the service there stayed enabled and running.
        self.touch(*(os.path.join(self.units, u) for u in ("x.timer", "x.service")))
        r = self.run_bash("disable_timers x.timer gone.timer x.service\necho went-on", fail="gone.timer")
        self.assertEqual(
            [c for c in self.calls() if "disable" in c],
            [
                "systemctl disable --now x.timer",
                "systemctl disable --now gone.timer",
                "systemctl disable --now x.service",
            ],
        )
        self.assertIn("went-on", r.stdout)
        self.assertEqual(r.stderr, "")  # a unit that is not there is not a problem

    def test_disable_timers_names_systemctls_reason_not_its_hint(self) -> None:
        # A stop that fails: notices first, the reason, then a "See ..." hint as the last line.
        self.touch(os.path.join(self.units, "x.service"))
        body = """
systemctl() {
    [[ "$1" == disable ]] || return 0
    printf '%s\\n' 'The unit files have no installation config (WantedBy=, RequiredBy=, UpheldBy=,' \\
        "Removed '/etc/systemd/system/multi-user.target.wants/x.service'." \\
        'Job for x.service failed because the control process exited with error code.' \\
        'See "systemctl status x.service" and "journalctl -xeu x.service" for details.' >&2
    return 1
}
disable_timers x.service
"""
        r = self.run_bash(body)
        self.assertEqual(
            r.stderr,
            "WARNING: could not disable x.service: "
            "Job for x.service failed because the control process exited with error code.\n",
        )

    def test_uninstall_stops_every_timer_before_it_forgets_them(self) -> None:
        timers = re.findall(r"[\w.-]+\.timer", assignment("TIMERS"))
        self.assertGreaterEqual(len(timers), 2)
        self.touch(*(os.path.join(self.units, t) for t in timers))
        self.run_bash("\n".join([assignment("TIMERS"), shell_function(SETUP, "stop_monitoring"), "stop_monitoring"]))
        want = [f"systemctl disable --now {u}" for u in [*timers, "btrfs-stall-monitor.service"]]
        for t in timers:
            want += [f"systemctl clean --what=state {t}", f"  ({t} file present)"]
        want.append("systemctl stop btrfs-stall-alert.service")
        self.assertEqual(self.calls(), want)

    def test_docker_gc_disable_forgets_its_timer(self) -> None:
        self.touch(os.path.join(self.units, "docker-gc.timer"))
        r = self.run_bash("\n".join([*self.docker_gc_body(), "docker_gc disable"]))
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now docker-gc-recorder.service",
                "systemctl disable --now docker-gc.service",
                "systemctl disable --now docker-gc.timer",
                "systemctl clean --what=state docker-gc.timer",
                "  (docker-gc.timer file present)",
            ],
        )
        self.assertIn("docker-gc disabled", r.stdout)
        self.assertIn("docker-gc disable --purge", r.stdout)

    def docker_gc_body(self) -> list[str]:
        """docker_gc as btrfs-setup.sh defines it, its recorded state in the test's folder and the
        real installer, every file it writes or removes staged under $T/stage."""
        return [
            STAGED_INSTALLER,
            DOCKER_GC_STAGED,
            shell_function(SETUP, "docker_gc_files"),
            shell_function(SETUP, "docker_gc_keep_settings"),
            shell_function(SETUP, "docker_gc"),
        ]

    def test_docker_gc_disable_purge_removes_program_units_and_state(self) -> None:
        self.touch(os.path.join(self.units, "docker-gc.timer"))
        state = os.path.join(self.tmp, "docker-gc-state")
        os.makedirs(state)
        self.touch(os.path.join(state, "last-used.json"))
        stage = os.path.join(self.tmp, "stage")
        # Installed first (staged), so the record lists the component.
        self.run_bash(f"{STAGED_INSTALLER}\ncst_install --component btrfs:docker-gc >/dev/null")
        r = self.run_bash("\n".join([*self.docker_gc_body(), "docker_gc disable --purge"]))
        units = ["docker-gc-recorder.service", "docker-gc.service", "docker-gc.timer"]
        self.assertEqual(
            self.calls(),
            [
                *(f"systemctl disable --now {u}" for u in units),
                "systemctl clean --what=state docker-gc.timer",
                "  (docker-gc.timer file present)",
                f"rm -rf {state}",
                "systemctl daemon-reload",
                "systemctl reset-failed " + " ".join(units),
            ],
        )
        for dest in ["/usr/local/sbin/docker-gc", *(f"/etc/systemd/system/{u}" for u in units)]:
            self.assertIn(f"removed:   {stage}{dest}\n", r.stdout)
            self.assertFalse(os.path.lexists(stage + dest), dest)
        # The install record no longer lists the component (cst doctor would call its files missing).
        with open(os.path.join(stage, "var/lib/cst/installed.json")) as f:
            record = json.load(f)
        self.assertEqual((record["files"], record["components"]), ({}, {}))
        self.assertFalse(os.path.exists(state))
        self.assertIn("docker-gc removed", r.stdout)
        # A second purge: the state is gone, so it is not removed again.
        os.unlink(self.log)
        self.run_bash("\n".join([*self.docker_gc_body(), "docker_gc disable --purge"]))
        self.assertNotIn(f"rm -rf {state}", self.calls())

    def test_docker_gc_disable_refuses_an_unknown_argument(self) -> None:
        for args in ("--all", "--purge --dry-run"):
            with self.subTest(args=args):
                body = "\n".join([*self.docker_gc_body(), f"(docker_gc disable {args}) && exit 9 || echo rc=$?"])
                r = self.run_bash(body)
                self.assertIn("rc=2", r.stdout)
                self.assertIn("only --purge", r.stderr)
                self.assertEqual(self.calls(), [])

    def docker_gc_status(self, enabled: str, rc: int) -> str:
        # systemctl as systemd 262 answers: is-enabled prints its word and exits 1 (disabled) or 4
        # (not-found); the Python status part is a stub.
        body = [
            (
                f'systemctl() {{ case "$1" in is-enabled) printf "%s\\n" {enabled!r}; return {rc} ;; '
                "is-active) echo inactive; return 3 ;; esac; }"
            ),
            'python3() { echo "python3 $*"; }',
            *self.docker_gc_body(),
            # The installed files, in the test's folder (the real ones may exist where the test runs).
            'docker_gc_files() { printf "%s\\n" "$T/units/docker-gc" "$T/units/docker-gc.timer"; }',
            "docker_gc status",
        ]
        return self.run_bash("\n".join(body)).stdout

    def test_docker_gc_status_prints_one_timer_state(self) -> None:
        # With "|| echo off", a disabled timer printed "disabled" and then "off" on a line of its own.
        self.assertEqual(self.docker_gc_status("disabled", 1).splitlines()[0], "timer disabled, recorder inactive")
        self.assertEqual(self.docker_gc_status("enabled", 0).splitlines()[0], "timer enabled, recorder inactive")
        self.assertEqual(
            self.docker_gc_status("not-found", 4).splitlines()[0], "timer not installed, recorder inactive"
        )
        self.assertEqual(self.docker_gc_status("", 1).splitlines()[0], "timer off, recorder inactive")
        self.assertEqual(len(self.docker_gc_status("disabled", 1).splitlines()), 2)

    def test_docker_gc_status_lists_what_purge_would_remove(self) -> None:
        self.touch(os.path.join(self.units, "docker-gc.timer"))
        os.makedirs(os.path.join(self.tmp, "docker-gc-state"))
        self.assertEqual(
            self.docker_gc_status("disabled", 1).splitlines(),
            [
                "timer disabled, recorder inactive",
                f"still installed: {self.units}/docker-gc.timer",
                f"still installed: {self.tmp}/docker-gc-state",
                "('cst-btrfs-test docker-gc disable --purge' removes them)",
                "python3 -I -B " + BTRFS + "/docker-gc status",
            ],
        )
        # Enabled: nothing to list.
        self.assertEqual(len(self.docker_gc_status("enabled", 0).splitlines()), 2)

    def snapshots(self, call: str) -> list[str]:
        # Sourced the way the frontend does; the paths it writes point into the test's folder.
        self.touch(*(os.path.join(self.units, t) for t in ("btrfs-snapshot.timer", "btrfs-snapshot-nas.timer")))
        self.run_bash(
            f'. "{REPO}/cst-install.bash"\n'
            '. "$SRC/setup-snapshots.bash"\n'
            'SNAP_BOOT_FLAG="$T/convert-at-boot"; BULK_FLAG="$T/bulk-at-boot"; SNAP_CONF="$T/snapshots.conf"\n'
            "target_user=u\n"
            f"{call}"
        )
        return [c for c in self.calls() if not c.startswith("rm ")]

    def test_snapshots_disable_forgets_both_timers(self) -> None:
        self.assertEqual(
            self.snapshots("snap_disable"),
            [
                "systemctl disable --now btrfs-snapshot.timer",
                "systemctl disable --now btrfs-snapshot-nas.timer",
                "systemctl disable --now btrfs-subvol-convert.service",
                "systemctl clean --what=state btrfs-snapshot.timer",
                "  (btrfs-snapshot.timer file present)",
                "systemctl clean --what=state btrfs-snapshot-nas.timer",
                "  (btrfs-snapshot-nas.timer file present)",
            ],
        )

    def test_nas_disable_forgets_its_timer(self) -> None:
        self.assertEqual(
            self.snapshots("snap_nas disable"),
            [
                "systemctl disable --now btrfs-snapshot-nas.timer",
                "systemctl clean --what=state btrfs-snapshot-nas.timer",
                "  (btrfs-snapshot-nas.timer file present)",
            ],
        )
        with open(os.path.join(self.tmp, "snapshots.conf")) as f:
            self.assertIn("NAS_ENABLED='0'\n", f.read())


BASH = shutil.which("bash") or "/usr/bin/bash"

# snap_conf_set as CST wrote it before values went in quotes (btrfs/setup-snapshots.bash at
# 2488fea, verbatim): printf %q, so snapshots.conf files on machines hold a\ b and $'...' values.
OLD_SNAP_CONF_SET = r"""
snap_conf_set() {
    local kv key val tmp
    install -d -m 0755 "$(dirname "${SNAP_CONF}")"
    if [[ ! -e "${SNAP_CONF}" ]]; then
        cat >"${SNAP_CONF}" <<'DEFAULTS'
# Managed by btrfs-setup.sh (CST); read by /usr/local/sbin/btrfs-snapshot.
# Manual edits of values are kept when btrfs-setup.sh updates other keys.
HOME_DIR=''
HOME_SUBVOL=home
SKIP_AT_OR_BELOW_GIB=250
RESUME_ABOVE_GIB=270
EMERGENCY_AT_OR_BELOW_GIB=150
NAS_ENABLED=0
NAS_HOST=''
NAS_PORT=2204
NAS_USER=''
NAS_PATH=''
NAS_RESTIC_HOST=''
NAS_STALE_DAYS=7
NAS_TIMEOUT=6h
NAS_KEEP_DAILY=14
NAS_KEEP_WEEKLY=8
NAS_KEEP_MONTHLY=12
DEFAULTS
        chmod 0644 "${SNAP_CONF}"
    fi
    for kv in "$@"; do
        key="${kv%%=*}"; val="${kv#*=}"
        printf -v val '%q' "${val}"
        tmp="$(mktemp)"
        K="${key}" L="${key}=${val}" awk 'BEGIN {k = ENVIRON["K"]; line = ENVIRON["L"]; done = 0}
            index($0, k "=") == 1 {print line; done = 1; next} {print}
            END {if (!done) print line}' "${SNAP_CONF}" >"${tmp}"
        install -m 0644 "${tmp}" "${SNAP_CONF}"
        rm -f "${tmp}"
    done
}
"""

# Values the writer must keep exactly: blanks, ~ where bash would expand it, #, both quote
# kinds (one at a time), $, backtick, backslash, globs, braces, command syntax, a tab,
# control characters, UTF-8.
WRITABLE = (
    "a b",
    "  lead and trail  ",
    "~",
    "~x",
    "a:~b",
    "x#y",
    "#x",
    "x # y",
    'say "hi"',
    "it's",
    "'",
    '"',
    "$HOME",
    "${HOME}",
    "`id`",
    "$(id)",
    "back\\slash",
    "\\",
    "tab\tx",
    "\x01ctl\x7f",
    "é ü",
    "",
    "*?[a]",
    "{a,b}",
    "a;b&c|d<e>f(g)",
    "!x",
    "%s %q",
    "a=b",
    "--opt",
)
# No form both bash and the strict reader read holds these: a line break, a carriage
# return, a ' together with one of " $ ` \.
UNWRITABLE = ("a\nb", "a\rb", "line\n", "it's $HOME", 'it\'s "x"', "it's `id`", "it's \\")


def writable(value: str) -> bool:
    if "\n" in value or "\r" in value:
        return False
    return "'" not in value or not any(c in value for c in '"$`\\')


# cst-install.bash with the real installer, every write staged under $T/stage
# (dests and srcs list the manifest's real paths; --root changes only where files go).
STAGED_INSTALLER = f"""
. "{REPO}/cst-install.bash"
_cst_installer_as() {{
    shift
    local command="$1"
    shift
    /usr/bin/python3 -I -B "$CST_INSTALLER" "$command" --root "$T/stage" "$@"
}}
"""


# cst-install.bash with the real installer as this user, every install record in $T/home (a run as a
# user records under the home it is given): never the developer's own record. Paths are not staged.
HOME_INSTALLER = f"""
. "{REPO}/cst-install.bash"
_cst_installer_as() {{
    shift
    local command="$1"
    shift
    /usr/bin/python3 -I -B "$CST_INSTALLER" "$command" --home "$T/home" "$@"
}}
_cst_user_home() {{ printf '%s\\n' "$T/home"; }}
"""


def home_record(tmp: str) -> dict[str, dict[str, object]]:
    """The files and the components of the install record HOME_INSTALLER keeps in tmp/home."""
    path = os.path.join(tmp, "home", USER_RECORD_REL)
    if not os.path.exists(path):
        return {"files": {}, "components": {}}
    with open(path) as f:
        return {k: v for k, v in object_of(parse_json(f.read())).items() if json_object(v)}


# docker-gc's settings file and installed program, staged (never the machine's /etc/cst, /usr/local/sbin).
DOCKER_GC_STAGED = (
    'DOCKER_GC_STATE="$T/docker-gc-state"; DOCKER_GC_CONF="$T/stage/etc/cst/docker-gc.conf"; '
    'DOCKER_GC_BIN="$T/stage/usr/local/sbin/docker-gc"'
)
# main's (654bbda) btrfs/docker_gc/domain.py, its built-in registry replaced by an invented one.
LEGACY_GC_DOMAIN = os.path.join(REPO, "tests", "fixtures", "legacy", "btrfs-docker_gc-domain.py@654bbda")


def manifest_dests(*components: str) -> list[str]:
    """The manifest's destinations of these components (root ones: no ~/ entries among them)."""
    return sorted(e.dest for e in INSTALL_MANIFEST if e.component in components)


class ComponentInstallTest(unittest.TestCase):
    """Each install path of btrfs-setup.sh and setup-snapshots.bash writes
    exactly the files of its components: the functions taken from the
    scripts, the real installer staged under the test's folder, systemctl
    and the other host commands as logging shell functions."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-btrfs-component-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stage = os.path.join(self.tmp, "stage")
        os.makedirs(self.stage)
        self.log = os.path.join(self.tmp, "log")

    def run_bash(self, body: str, *, want_rc: int = 0) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                STUBS,
                STAGED_INSTALLER,
                'install() { echo "install $*" >>"$LOG"; }',
                "restorecon() { :; }",
                f'SRC={BTRFS}; UNIT_DIR=/etc/systemd/system; PROG=cst-btrfs-test; target_user=""',
                'ALERTS_DIR="$T/alerts"; OLD_ALERTS_DIR="$T/old-alerts"; USER_UNIT_DIR=/etc/systemd/user',
                "PENDING_UNIT=cst-pending-alerts.service; OLD_PENDING_UNIT=f44-pending-alerts.service",
                'OLD_PENDING_READER="$T/f44-pending-alerts"',
                DOCKER_GC_STAGED,
                *(
                    shell_function(SETUP, f)
                    for f in (
                        "check_syntax",
                        "check_component_syntax",
                        "remove_old_reader",
                        "migrate_old_alerts",
                        "install_pending_alerts",
                        "forget_timer_state",
                        "disable_timers",
                        "docker_gc_files",
                        "docker_gc_keep_settings",
                        "docker_gc",
                        "remove_tuning_files",
                    )
                ),
                body,
            ]
        )
        r = subprocess.run(
            ["bash", "-c", script, "_"],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LOG": self.log, "T": self.tmp},
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        self.assertEqual(r.returncode, want_rc, r.stdout + r.stderr)
        return r

    def staged(self) -> list[str]:
        """The files written under the stage, the install record and its lock aside."""
        out = []
        for dirpath, _dirs, names in os.walk(self.stage):
            out += ["/" + os.path.relpath(os.path.join(dirpath, n), self.stage) for n in names]
        return sorted(p for p in out if not p.startswith("/var/lib/cst/installed.json"))

    def record(self) -> dict[str, dict[str, object]]:
        with open(os.path.join(self.stage, "var/lib/cst/installed.json")) as f:
            return {k: v for k, v in object_of(parse_json(f.read())).items() if json_object(v)}  # its tables

    def test_alerts_install_writes_btrfs_alerts(self) -> None:
        self.run_bash("install_pending_alerts")
        self.assertEqual(self.staged(), manifest_dests("btrfs:alerts"))
        # Listed in the install record, and marked done once the step finished.
        rec = self.record()
        self.assertEqual(sorted(rec["files"]), manifest_dests("btrfs:alerts"))
        self.assertEqual(rec["components"], {"btrfs:alerts": "done"})

    def test_docker_gc_enable_writes_btrfs_docker_gc(self) -> None:
        r = self.run_bash("docker_gc enable")
        # And its settings, first: no docker-gc was installed, so no registry is protected.
        self.assertEqual(self.staged(), sorted([*manifest_dests("btrfs:docker-gc"), "/etc/cst/docker-gc.conf"]))
        self.assertIn("docker-gc enabled", r.stdout)
        self.assertEqual(self.record()["components"], {"btrfs:docker-gc": "done"})
        conf = os.path.join(self.stage, "etc/cst/docker-gc.conf")
        self.assertIn(f"written:   {conf} (DOCKER_GC_PROTECT=)", r.stdout)
        with open(conf) as f:
            self.assertTrue(f.read().endswith("\nDOCKER_GC_PROTECT=\n"))
        r = self.run_bash("docker_gc enable")
        self.assertIn(f"unchanged: {conf}", r.stdout)

    def stage_legacy_docker_gc(self) -> str:
        """main's docker-gc (a zipapp with a registry built in), installed, and no settings file."""
        prog = os.path.join(self.stage, "usr/local/sbin/docker-gc")
        os.makedirs(os.path.dirname(prog))
        with zipfile.ZipFile(prog, "w") as z, open(LEGACY_GC_DOMAIN, "rb") as f:
            z.writestr("__main__.py", "")
            z.writestr("btrfs/docker_gc/domain.py", f.read())
        return prog

    def test_docker_gc_enable_keeps_the_registry_of_the_installed_docker_gc(self) -> None:
        # The upgrade: enable writes the installed program's registry into the settings file
        # before the new program replaces it.
        prog = self.stage_legacy_docker_gc()
        r = self.run_bash("docker_gc enable")
        conf = os.path.join(self.stage, "etc/cst/docker-gc.conf")
        self.assertIn(f"written:   {conf} (DOCKER_GC_PROTECT=registry.example.com)", r.stdout)
        self.assertLess(r.stdout.index(conf), r.stdout.index(f"written:   {prog}"), "the program was replaced first")
        with open(conf) as f:
            self.assertTrue(f.read().endswith("\nDOCKER_GC_PROTECT=registry.example.com\n"))
        # A purge keeps the settings.
        r = self.run_bash("docker_gc disable --purge")
        self.assertIn(f"kept:      {conf} (the settings: which registries are protected)", r.stdout)
        self.assertTrue(os.path.exists(conf))
        self.assertFalse(os.path.exists(prog))

    def test_docker_gc_purge_before_enable_keeps_the_registry_of_the_installed_docker_gc(self) -> None:
        # The upgrade, then `disable --purge` before any enable: the purge takes the installed
        # program away, so it writes its registry into the settings file first.
        prog = self.stage_legacy_docker_gc()
        conf = os.path.join(self.stage, "etc/cst/docker-gc.conf")
        r = self.run_bash("docker_gc disable --purge")
        self.assertIn(f"written:   {conf} (DOCKER_GC_PROTECT=registry.example.com)", r.stdout)
        self.assertIn(f"kept:      {conf} (the settings: which registries are protected)", r.stdout)
        with open(conf) as f:
            self.assertTrue(f.read().endswith("\nDOCKER_GC_PROTECT=registry.example.com\n"))
        self.assertFalse(os.path.exists(prog))
        r = self.run_bash("docker_gc enable")
        self.assertIn(f"unchanged: {conf}", r.stdout)
        with open(conf) as f:
            self.assertTrue(f.read().endswith("\nDOCKER_GC_PROTECT=registry.example.com\n"))

    def test_docker_gc_stops_at_an_installed_program_it_does_not_know(self) -> None:
        # No settings file and a /usr/local/sbin/docker-gc that is not CST's: enable and the purge
        # stop with 78 at `settings --write` (set -e), before they change anything.
        prog = os.path.join(self.stage, "usr/local/sbin/docker-gc")
        os.makedirs(os.path.dirname(prog))
        other = "#!/bin/sh\necho another docker-gc\n"
        with open(prog, "w") as f:
            f.write(other)
        for action in ("enable", "disable --purge"):
            r = self.run_bash(f"docker_gc {action}", want_rc=78)
            self.assertIn("does not show which registries it protects", r.stdout + r.stderr, action)
            self.assertEqual(self.staged(), ["/usr/local/sbin/docker-gc"], action)
            with open(prog) as f:
                self.assertEqual(f.read(), other, action)
            if os.path.exists(self.log):
                with open(self.log) as f:
                    self.assertNotIn("systemctl", f.read(), action)

    def test_docker_gc_purge_with_nothing_installed_writes_no_settings(self) -> None:
        r = self.run_bash("docker_gc disable --purge")
        self.assertNotIn("docker-gc.conf", r.stdout)
        self.assertEqual(self.staged(), [])

    def test_a_step_that_fails_after_the_install_leaves_the_component_pending(self) -> None:
        # systemctl enable fails (set -e stops docker_gc there): the files are written and listed,
        # but the record says pending, which cst doctor reports as a partial install.
        r = subprocess.run(
            ["bash", "-c", f"{STAGED_INSTALLER}\ncst_install --component btrfs:docker-gc >/dev/null"],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "T": self.tmp},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.record()["components"], {"btrfs:docker-gc": "pending"})
        self.assertEqual(sorted(self.record()["files"]), manifest_dests("btrfs:docker-gc"))

    def test_snapshot_runtime_writes_alerts_then_snapshots(self) -> None:
        r = self.run_bash('. "$SRC/setup-snapshots.bash"\nsnap_install_runtime')
        self.assertEqual(self.staged(), manifest_dests("btrfs:alerts", "btrfs:snapshots"))
        self.assertEqual(self.record()["components"], {"btrfs:alerts": "done", "btrfs:snapshots": "done"})
        # btrfs-notify, which the snapshot units' runs alert through, is written before them.
        out = r.stdout
        self.assertLess(out.index("/usr/local/sbin/btrfs-notify"), out.index("/btrfs-snapshot.service"))

    def test_disable_stops_when_the_installer_cannot_list_the_units(self) -> None:
        # A broken installer must not end in "docker-gc disabled" with the timer still enabled.
        script = f"{STAGED_INSTALLER}\n{DOCKER_GC_STAGED}\ncst_component_units() {{ return 1; }}\ndocker_gc disable"
        r = subprocess.run(
            [
                "bash",
                "-c",
                "\n".join([STUBS, *(shell_function(SETUP, f) for f in ("docker_gc_files", "docker_gc")), script]),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LOG": self.log, "T": self.tmp},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cannot list its units", r.stderr)
        self.assertNotIn("docker-gc disabled", r.stdout)

    def test_uninstall_forgets_a_tuning_file_already_gone(self) -> None:
        # btrfs:core's sysctl and tmpfiles files, installed (staged) and recorded; the sysctl file was
        # deleted by hand. remove_tuning_files removes the other and drops both record lines.
        sysctl = "/etc/sysctl.d/90-btrfs-writeback.conf"
        reclaim = "/etc/tmpfiles.d/btrfs-automatic-reclaim.conf"
        self.run_bash(
            "cst_install --set UUID=00000000-0000-0000-0000-000000000000 "
            "btrfs/sysctl/90-btrfs-writeback.conf btrfs/tmpfiles/btrfs-automatic-reclaim.conf.in >/dev/null"
        )
        self.assertEqual(sorted(self.record()["files"]), [sysctl, reclaim])
        os.unlink(self.stage + sysctl)
        self.run_bash(
            f'SYSCTL_FILE="$T/stage{sysctl}"; RECLAIM_FILE="$T/stage{reclaim}"\n'
            f"{assignment('SYSCTL_SRC')}\n{assignment('RECLAIM_SRC')}\nremove_tuning_files"
        )
        self.assertEqual(self.staged(), [])
        self.assertEqual((self.record()["files"], self.record()["components"]), ({}, {}))

    def test_uninstall_keeps_a_reclaim_file_it_did_not_create_and_drops_its_line(self) -> None:
        # The reclaim file, installed (staged) and recorded, then rewritten by hand without CST's
        # marker: remove_tuning_files keeps it as it is and drops its record line, so cst doctor
        # stops calling it changed. The sysctl file and its line go as before.
        sysctl = "/etc/sysctl.d/90-btrfs-writeback.conf"
        reclaim = "/etc/tmpfiles.d/btrfs-automatic-reclaim.conf"
        self.run_bash(
            "cst_install --set UUID=00000000-0000-0000-0000-000000000000 "
            "btrfs/sysctl/90-btrfs-writeback.conf btrfs/tmpfiles/btrfs-automatic-reclaim.conf.in >/dev/null"
        )
        mine = b"w /sys/fs/btrfs/x/allocation/data/dynamic_reclaim - - - - 0\n"
        Path(self.stage + reclaim).write_bytes(mine)
        r = self.run_bash(
            f'SYSCTL_FILE="$T/stage{sysctl}"; RECLAIM_FILE="$T/stage{reclaim}"\n'
            f"{assignment('SYSCTL_SRC')}\n{assignment('RECLAIM_SRC')}\nremove_tuning_files"
        )
        self.assertIn(f"Kept {self.stage}{reclaim}: not created by this script.", r.stdout)
        self.assertEqual(self.staged(), [reclaim])
        self.assertEqual(Path(self.stage + reclaim).read_bytes(), mine)
        self.assertEqual((self.record()["files"], self.record()["components"]), ({}, {}))

    def test_a_program_that_does_not_parse_installs_nothing(self) -> None:
        r = subprocess.run(
            [
                "bash",
                "-c",
                "\n".join(
                    [
                        STAGED_INSTALLER,
                        shell_function(SETUP, "check_syntax"),
                        shell_function(SETUP, "check_component_syntax"),
                        f"SRC={self.tmp}/cst/btrfs",
                        "check_component_syntax btrfs:docker-gc",
                    ]
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "T": self.tmp},
            stdin=subprocess.DEVNULL,
        )
        # The source is missing under that SRC: refused before anything is written.
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("docker-gc", r.stderr)
        self.assertEqual(self.staged(), [])


class SnapshotsConfTest(unittest.TestCase):
    """snap_conf_set (btrfs/setup-snapshots.bash) writes snapshots.conf in quotes that bash and
    cst_core's strict reader read alike, and rewrites the printf %q lines an older CST wrote."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-snap-conf-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.conf = os.path.join(self.tmp, "snapshots.conf")
        self.log = os.path.join(self.tmp, "log")
        self.empty = os.path.join(self.tmp, "no-commands")
        os.mkdir(self.empty)

    def run_bash(
        self, body: str, values: dict[str, str] | None = None, *, locale: str = "C.UTF-8", rc: int | None = 0
    ) -> subprocess.CompletedProcess[str]:
        """body after cst-install.bash and setup-snapshots.bash are sourced as the frontend sources
        them (the real installer writes snapshots.conf); SNAP_CONF and the other paths it writes
        point into the test's folder. values: environment variables (surrogate escapes stand for
        bytes that are not UTF-8)."""
        script = "\n".join(
            [
                STUBS,
                f"SRC={BTRFS}; PROG=cst-btrfs-test",
                f'. "{REPO}/cst-install.bash"',
                '. "$SRC/setup-snapshots.bash"',
                'SNAP_CONF="$T/snapshots.conf"; SNAP_PLAN="$T/convert-plan"; SNAP_BOOT_FLAG="$T/convert-at-boot"',
                body,
            ]
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LOG": self.log,
            "T": self.tmp,
            "TMPDIR": self.tmp,
            "HOME": self.tmp,
            "LC_ALL": locale,
            **(values or {}),
        }
        r = subprocess.run(
            ["bash", "-c", script, "_"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        if rc is not None:
            self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        return r

    def set_values(self, values: dict[str, str], *, writer: str = "", locale: str = "C.UTF-8", rc: int = 0) -> str:
        """snap_conf_set KEY=VALUE... (writer: bash that redefines snap_conf_set first); its stderr."""
        env = {f"V_{k}": v for k, v in values.items()}
        args = " ".join(f'"{k}=$V_{k}"' for k in values)
        return self.run_bash(f"{writer}\nsnap_conf_set {args}", env, locale=locale, rc=rc).stderr

    def bash_values(self, keys: list[str]) -> dict[str, str]:
        """What bash sets when it sources snapshots.conf (as btrfs-snapshot does), read back
        NUL-separated; bytes that are not UTF-8 become U+FFFD, as CST's Python readers read them."""
        script = '. "$1"; shift; for k in "$@"; do if [[ -v $k ]]; then printf "%s=%s\\0" "$k" "${!k}"; fi; done'
        r = subprocess.run(
            [BASH, "--norc", "--noprofile", "-c", script, "_", self.conf, *keys],
            check=True,
            capture_output=True,
            env={"PATH": self.empty, "HOME": self.tmp},
            stdin=subprocess.DEVNULL,
        )
        out: dict[str, str] = {}
        for item in r.stdout.split(b"\0")[:-1]:
            k, _, v = item.partition(b"=")
            out[k.decode()] = v.decode("utf-8", "replace")
        return out

    def text(self) -> str:
        got = read_text(self.conf)
        assert got is not None
        return got

    def raw(self) -> bytes:
        with open(self.conf, "rb") as f:
            return f.read()

    def test_nas_status_reads_the_file_as_btrfs_snapshot_does_and_runs_nothing_in_it(self) -> None:
        # snapshots status and nas sourced snapshots.conf as root: a command line in it ran, and a
        # value with $(...) was expanded, though btrfs-snapshot (the units) ignores both lines. Now
        # they read it through btrfs-snapshot settings-lines: the same lines, nothing run.
        ran, ran2 = os.path.join(self.tmp, "ran"), os.path.join(self.tmp, "ran2")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(f"NAS_ENABLED=1\ntouch '{ran}'\nNAS_PATH=\"$(touch '{ran2}'; echo /volume1/x)\"\n")
        r = self.run_bash("target_user=u; snap_nas status")
        self.assertEqual(r.stdout, "NAS backup not set up: cst-btrfs-test snapshots nas setup --path ...\n")
        self.assertFalse(os.path.exists(ran))
        self.assertFalse(os.path.exists(ran2))
        self.assertIn(f"<4>btrfs-snapshot: {self.conf} line 2: touch '{ran}': not a KEY=VALUE line; ignored", r.stderr)
        self.assertIn(f"<4>btrfs-snapshot: {self.conf} line 3: NAS_PATH=", r.stderr)

    def test_snap_conf_read_sets_only_the_keys_asked_from_plain_lines(self) -> None:
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write("NAS_ENABLED=1 # on\nexport NAS_PATH='/volume1/b c'\nNAS_USER=old\\ name\nHOME_DIR='/x'\nPATH=/x\n")
        body = (
            'f() { local NAS_ENABLED=0 NAS_PATH="" NAS_USER="" HOME_DIR=keep; '
            "snap_conf_read NAS_ENABLED NAS_PATH NAS_USER; "
            'printf "[%s]" "$NAS_ENABLED" "$NAS_PATH" "$NAS_USER" "$HOME_DIR" "$PATH"; }; f'
        )
        r = self.run_bash(body, {"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.stdout, "[1][/volume1/b c][old name][keep][/usr/bin:/bin]")
        self.assertEqual(r.stderr, "")

    def test_written_values_read_the_same_in_bash_and_in_the_strict_reader(self) -> None:
        values = {f"K{i}": v for i, v in enumerate(WRITABLE)}
        self.set_values(values)
        got = parse_shell_assignments(self.text())
        self.assertEqual(got.problems, ())
        self.assertEqual({k: got.values[k] for k in values}, values)
        self.assertEqual(self.bash_values(list(values)), values)
        lines = self.text().splitlines()
        self.assertIn(f"K{WRITABLE.index('a b')}='a b'", lines)
        self.assertIn(f'K{WRITABLE.index(chr(39))}="\'"', lines)  # a ' alone: double quotes
        # The defaults come first, once, as before.
        self.assertEqual(lines[:4][2:], ["HOME_DIR=''", "HOME_SUBVOL=home"])

    def test_a_value_no_quoted_form_holds_is_refused_and_nothing_is_written(self) -> None:
        self.set_values({"NAS_PATH": "/x"})
        before = self.raw()
        for value in UNWRITABLE:
            with self.subTest(value=value):
                err = self.set_values({"NAS_HOST": "nas", "NAS_RESTIC_HOST": value}, rc=1)
                self.assertIn("the value of NAS_RESTIC_HOST has a line break", err)
                self.assertIn("nothing was written", err)
                self.assertEqual(self.raw(), before, "a key was written")
        os.remove(self.conf)
        self.set_values({"NAS_RESTIC_HOST": "a\nb"}, rc=1)
        self.assertFalse(os.path.exists(self.conf))

    def test_the_file_is_written_only_when_it_changes_and_manual_lines_stay(self) -> None:
        self.set_values({"NAS_ENABLED": "1"})
        with open(self.conf, "a") as f:
            f.write("# my note\nNAS_TIMEOUT=12h  # longer\nexport X=1\nNAS_ENABLED=1\n")
        self.set_values({"NAS_ENABLED": "1"})
        st = os.stat(self.conf)
        before = self.raw()
        self.set_values({"NAS_ENABLED": "1"})
        after = os.stat(self.conf)
        self.assertEqual((after.st_ino, after.st_mtime_ns, self.raw()), (st.st_ino, st.st_mtime_ns, before))
        lines = self.text().splitlines()
        # Every NAS_ENABLED= line takes the value; other lines (manual edits too) stay as they are.
        self.assertEqual([ln for ln in lines if ln.startswith("NAS_ENABLED=")], ["NAS_ENABLED='1'"] * 2)
        self.assertIn("NAS_TIMEOUT=12h  # longer", lines)
        self.assertIn("# my note", lines)
        self.assertIn("export X=1", lines)
        self.set_values({"NAS_ENABLED": "0"})
        self.assertNotEqual(self.raw(), before)
        self.assertEqual(oct(os.stat(self.conf).st_mode & 0o777), "0o644")

    def test_an_unchanged_file_gets_its_mode_back(self) -> None:
        """Same content, mode changed by hand: the installer writes the file again with mode 0644
        (as the old writer's install -m 0644 did on every run) and says so. Before, a chmod put the
        mode back and the installer then printed unchanged: for the file it had just changed."""
        self.set_values({"NAS_ENABLED": "1"})
        content = self.raw()
        os.chmod(self.conf, 0o600)
        before = os.stat(self.conf)
        r = self.run_bash("snap_conf_set NAS_ENABLED=1")
        self.assertEqual((r.returncode, r.stdout), (0, f"written:   {self.conf}\n"))
        after = os.stat(self.conf)
        self.assertEqual(oct(after.st_mode & 0o777), "0o644")
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertEqual(self.raw(), content)
        self.assertEqual(self.run_bash("snap_conf_set NAS_ENABLED=1").stdout, f"unchanged: {self.conf}\n")

    def test_the_installer_writes_the_file_and_names_it(self) -> None:
        # Through cst_install_text: written: on a change, unchanged: otherwise; a change is a new
        # file renamed into place (a new inode), never a write into the file the hourly run reads.
        self.assertEqual(self.run_bash("snap_conf_set NAS_ENABLED=1").stdout, f"written:   {self.conf}\n")
        before = os.stat(self.conf)
        self.assertEqual(self.run_bash("snap_conf_set NAS_ENABLED=1").stdout, f"unchanged: {self.conf}\n")
        self.assertEqual(self.run_bash("snap_conf_set NAS_ENABLED=0").stdout, f"written:   {self.conf}\n")
        after = os.stat(self.conf)
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertEqual(oct(after.st_mode & 0o777), "0o644")
        self.assertIn("NAS_ENABLED='0'", self.text().splitlines())

    def test_a_link_in_its_place_is_replaced_and_its_target_is_left(self) -> None:
        # The installer replaces the link itself with a regular file and never writes into what
        # it points to. (As root it also replaces a link whose target holds the same content,
        # which the old writer kept, changing the target's mode: tier 1 test_17.)
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write("NAS_ENABLED='1'\n")
        os.symlink(target, self.conf)
        r = self.run_bash("snap_conf_set NAS_ENABLED=0")
        self.assertEqual(r.stdout, f"written:   {self.conf}\n")
        self.assertFalse(os.path.islink(self.conf))
        self.assertIn("NAS_ENABLED='0'", self.text().splitlines())
        with open(target) as f:
            self.assertEqual(f.read(), "NAS_ENABLED='1'\n")

    def test_its_folder_is_created(self) -> None:
        conf = os.path.join(self.tmp, "etc", "btrfs-setup", "snapshots.conf")
        r = self.run_bash(f'SNAP_CONF="{conf}"; snap_conf_set NAS_ENABLED=1')
        self.assertEqual(r.stdout, f"written:   {conf}\n")
        self.assertEqual(oct(os.stat(conf).st_mode & 0o777), "0o644")

    def test_an_old_writer_file_reads_as_bash_reads_it_and_is_rewritten_in_quotes(self) -> None:
        # The upgrade: the fixture comes from the old writer and real bash printf %q, every byte
        # 1..255 and the tricky values, written in either locale and rewritten in either.
        tricky = WRITABLE + UNWRITABLE + ("/volume1/My Backups",)
        values = {f"Q{i}": v for i, v in enumerate(tricky)}
        values.update({f"B{b}": os.fsdecode(bytes([b])) + "x" for b in range(1, 256)})
        keys = sorted(values)
        for old_locale in ("C", "C.UTF-8"):
            for new_locale in ("C", "C.UTF-8"):
                with self.subTest(old=old_locale, new=new_locale):
                    if os.path.exists(self.conf):
                        os.remove(self.conf)
                    self.set_values(values, writer=OLD_SNAP_CONF_SET, locale=old_locale)
                    old_text = self.text()
                    want = self.bash_values(keys)
                    self.assertEqual(len(want), len(keys))
                    # The snapshots.conf reader (doctor, cst vm) reads every old line as bash does;
                    # the generic reader stays strict on them.
                    old = parse_snapshots_conf(old_text)
                    self.assertEqual(old.problems, ())
                    self.assertEqual({k: old.values[k] for k in keys}, want)
                    self.assertTrue(any("\\" in p.text for p in parse_shell_assignments(old_text).problems))
                    err = self.set_values({"HOME_SUBVOL": "home"}, locale=new_locale)
                    self.assertEqual(self.bash_values(keys), want, "the rewrite changed a value")
                    new = parse_snapshots_conf(self.text())
                    self.assertEqual(new.problems, ())
                    self.assertEqual({k: new.values[k] for k in keys}, want)
                    # Only lines no quoted form holds keep their old form (and say so); all the
                    # others now read with zero problems in the strict reader.
                    kept = {p.text.partition("=")[0] for p in parse_shell_assignments(self.text()).problems}
                    self.assertEqual(kept, {k for k in keys if not writable(want[k])})
                    self.assertEqual(err.count("is kept as an older CST wrote it"), len(kept))
                    # A line the old writer escaped is now in quotes; a plain one (x#y) stays as it was.
                    escaped = {ln.partition("=")[0] for ln in old_text.splitlines() if "\\" in ln}
                    for ln in self.text().splitlines():
                        key, _, value = ln.partition("=")
                        if key in escaped - kept:
                            self.assertIn(value[:1], ("'", '"'), ln)
                    self.assertGreater(len(escaped - kept), 200)

    # snap_setup with everything but snapshots.conf stubbed (no disks, no units, no user files).
    SETUP_STUBS = r"""
snap_home() { echo "$T/home"; }
snap_subvol() { echo home; }
root_fs_uuid() { echo 0000-uuid; }
snap_plan_summary() { echo "plan summary"; }
bulk_run() { echo "bulk $*"; }
bulk_apply() { return 0; }
bulk_warn() { :; }
snap_clean_targets_plan() { :; }
snap_clean_targets_apply() { :; }
snap_toolchains_config_preview() { :; }
snap_chrome() { echo "chrome $*"; }
snap_install_runtime() { echo "runtime installed"; }
snap_user_configs() { :; }
snap_activate() { echo "activated"; }
snap_status() { echo "status"; }
function /usr/local/sbin/btrfs-snapshot { echo "btrfs-snapshot $* (seam: ${CST_SNAPSHOT_ROOT-unset})"; }
"""

    def test_setup_cycle_on_an_old_file_plan_apply_and_a_second_run(self) -> None:
        old = {
            "HOME_DIR": os.path.join(self.tmp, "home"),
            "HOME_SUBVOL": "home",
            "NAS_PATH": "/volume1/My Backups",
            "NAS_RESTIC_HOST": "host #1",
            "SNAPSHOT_CACHES_SKIP": ".local/share/containers ~/x",
        }
        self.set_values(old, writer=OLD_SNAP_CONF_SET)
        before = self.raw()
        want = self.bash_values(sorted(old))
        self.assertEqual(want, old)
        self.assertNotEqual(parse_shell_assignments(self.text()).problems, ())
        # plan: the preview names the rewrite; no terminal to confirm, so nothing changes.
        r = self.run_bash(self.SETUP_STUBS + "snap_setup", rc=1)
        self.assertIn("3 line(s) that an older CST wrote with backslashes", r.stdout)
        self.assertIn("No terminal to confirm", r.stderr)
        self.assertEqual(self.raw(), before)
        # apply: the file is in quotes, every value as bash read the old file. The live conversion
        # never gets btrfs-snapshot's test seam, also when the caller has it.
        r = self.run_bash(self.SETUP_STUBS + "export CST_SNAPSHOT_ROOT=/elsewhere\nsnap_setup --yes")
        self.assertIn("activated", r.stdout)
        self.assertIn("btrfs-snapshot convert (seam: unset)\n", r.stdout)
        self.assertEqual(self.bash_values(sorted(old)), want)
        got = parse_shell_assignments(self.text())
        self.assertEqual(got.problems, ())
        self.assertEqual({k: got.values[k] for k in old}, old)
        self.assertIn("NAS_PATH='/volume1/My Backups'", self.text().splitlines())
        # A second run changes nothing: same bytes, same file, no rewrite in the preview.
        st = os.stat(self.conf)
        after = self.raw()
        r = self.run_bash(self.SETUP_STUBS + "snap_setup --yes")
        again = os.stat(self.conf)
        self.assertEqual((again.st_ino, again.st_mtime_ns, self.raw()), (st.st_ino, st.st_mtime_ns, after))
        self.assertNotIn("older CST", r.stdout)


def service_lines(unit: str) -> list[str]:
    """The [Service] lines of btrfs/units/UNIT as shipped, without blanks and comments."""
    with open(os.path.join(BTRFS, "units", unit)) as f:
        text = f.read()
    m = re.search(r"^\[Service\]\n(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert m is not None, text
    return [ln for ln in m.group(1).splitlines() if ln and not ln.startswith("#")]


class StallMonitorUnitTest(unittest.TestCase):
    """btrfs/units/btrfs-stall-monitor.service as shipped."""

    def service_lines(self) -> list[str]:
        return service_lines("btrfs-stall-monitor.service")

    def test_journal_lines_carry_the_daemon_name(self) -> None:
        # Not python3, the program systemd starts.
        self.assertIn("SyslogIdentifier=btrfs-stall-monitord", self.service_lines())

    def test_queued_alerts_survive_a_restart(self) -> None:
        # alert-*.txt waits in the RuntimeDirectory for btrfs-stall-alert.service; with the default
        # (no) a restart after a crash removed it.
        lines = self.service_lines()
        self.assertIn("RuntimeDirectory=btrfs-stall-monitor", lines)
        self.assertIn("Restart=on-failure", lines)
        self.assertIn("RuntimeDirectoryPreserve=restart", lines)


class DockerGcUnitsTest(unittest.TestCase):
    """btrfs/units/docker-gc-recorder.service and docker-gc.service as shipped: sandboxed, with
    /var/lib/docker-gc (docker-gc's state) as the place they may write, and /run/docker-gc (the
    run lock) for the weekly run."""

    UNITS = ("docker-gc-recorder.service", "docker-gc.service")

    def service(self, name: str) -> dict[str, list[str]]:
        keys: dict[str, list[str]] = {}
        for ln in service_lines(name):
            k, _, v = ln.partition("=")
            keys.setdefault(k, []).append(v)
        return keys

    def test_both_have_the_same_sandbox(self) -> None:
        # The block is written out in each unit (systemd has no include): a setting added to
        # one only shows here. What differs is the job, and the weekly run's lock folder.
        own = {"Type", "ExecCondition", "ExecStart", "Restart", "RestartSec", "RestartPreventExitStatus", "Nice"}
        own |= {"IOSchedulingClass"}
        own |= {"TimeoutStartSec", "Environment", "RuntimeDirectory", "RuntimeDirectoryPreserve"}
        recorder, weekly = ({k: v for k, v in self.service(name).items() if k not in own} for name in self.UNITS)
        self.assertEqual(recorder, weekly)

    def test_both_run_sandboxed_with_no_capability(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                s = self.service(name)
                self.assertEqual(s.get("CapabilityBoundingSet"), [""])
                # Not NoNewPrivileges: it blocks the SELinux move into the Docker domain.
                self.assertNotIn("NoNewPrivileges", s)
                for key, value in (
                    ("ProtectSystem", "strict"),
                    ("ProtectHome", "read-only"),
                    ("PrivateTmp", "yes"),
                    ("RestrictAddressFamilies", "AF_UNIX"),
                    ("SystemCallFilter", "@system-service"),
                    ("MemoryDenyWriteExecute", "yes"),
                ):
                    self.assertEqual(s.get(key), [value], key)

    def test_buildx_keeps_its_state_in_the_state_directory(self) -> None:
        # docker builder prune runs buildx, which fails to mkdir ~/.docker under ProtectHome=read-only.
        self.assertEqual(
            self.service("docker-gc.service").get("Environment"), ["BUILDX_CONFIG=/var/lib/docker-gc/buildx"]
        )

    def test_the_paths_docker_gc_writes_are_the_units_state_and_runtime_directories(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                s = self.service(name)
                self.assertEqual(s.get("StateDirectory"), ["docker-gc"])
                self.assertNotIn("ReadWritePaths", s)
        paths = gc_wiring.paths_from({})  # the program's paths without the tests' overrides
        self.assertEqual(paths.state_dir, "/var/lib/docker-gc")
        for path in (paths.last_used, paths.last_run):
            self.assertEqual(os.path.dirname(path), paths.state_dir, path)
        # The run lock: only the weekly run takes it (the recorder does not). Kept after the run
        # (Preserve=), so a cleanup-now that holds it then does not lose its file.
        self.assertEqual(paths.lock, "/run/docker-gc/run.lock")
        gc = self.service("docker-gc.service")
        self.assertEqual((gc.get("RuntimeDirectory"), gc.get("RuntimeDirectoryPreserve")), (["docker-gc"], ["yes"]))
        self.assertNotIn("RuntimeDirectory", self.service("docker-gc-recorder.service"))


SCRUB_CASES: list[tuple[str, str | None]] = [
    # (snapshots.conf text, the limit both readers give; None: both refuse)
    ("", ""),
    ("SCRUB_LIMIT=1G\n", "1G"),
    ("SCRUB_LIMIT='500M'\n", "500M"),
    ('SCRUB_LIMIT="2g"\n', "2g"),
    ("SCRUB_LIMIT=1G  # per device\n", "1G"),
    ("SCRUB_LIMIT='1G'\t# quoted, then a comment\n", "1G"),
    ("export SCRUB_LIMIT=1G\n", "1G"),
    ("  SCRUB_LIMIT=750K\n", "750K"),
    ("SCRUB_LIMIT=1G\nSCRUB_LIMIT=2T\n", "2T"),
    ("SCRUB_LIMIT=0\n", ""),
    ("SCRUB_LIMIT=\n", ""),
    ("SCRUB_LIMIT=''\n", ""),
    ("SCRUB_LIMIT= # none\n", ""),
    ("#SCRUB_LIMIT=1G\n", ""),
    ("SCRUB_LIMITS=fast\n", ""),
    ("HOME_SUBVOL=home\nNAS_HOST=''\nSCRUB_LIMIT=1G # c\n", "1G"),
    ("SCRUB_LIMIT=$X\n", None),
    ('SCRUB_LIMIT="1$G"\n', None),
    ("SCRUB_LIMIT=1\\G\n", None),
    ("SCRUB_LIMIT = 1G\n", None),
    ("SCRUB_LIMIT= 1G\n", None),
    ("SCRUB_LIMIT=1G; touch x\n", None),
    ("SCRUB_LIMIT=1G 2G\n", None),
    ("SCRUB_LIMIT='1G'x\n", None),
    ("SCRUB_LIMIT=1G\r\n", None),
    ("SCRUB_LIMIT\n", None),
    ("SCRUB_LIMIT=fast\n", None),
    ("SCRUB_LIMIT=1.5G\n", None),
    ("SCRUB_LIMIT=-1\n", None),
    ("SCRUB_LIMIT=1G\nSCRUB_LIMIT=$(date)\n", None),
]


class ScrubLimitTest(unittest.TestCase):
    """SCRUB_LIMIT (the weekly scrub's speed limit) in snapshots.conf: btrfs-setup.sh's
    reader, the drop-in install writes from it, and what status shows."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-scrub-limit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.conf = os.path.join(self.tmp, "snapshots.conf")
        self.dropin = os.path.join(self.tmp, "units", "btrfs-scrub-weekly.service.d", "10-cst-limit.conf")

    def bash(self, *lines: str) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                HOME_INSTALLER,
                assignment("SCRUB_SIZE_RE"),
                assignment("SCRUB_LINE_RE"),
                f'BTRFS_CONF="{self.conf}"; SCRUB_DROPIN="{self.dropin}"; BTRFS_SYSFS="{self.tmp}/sys"',
                *(shell_function(SETUP, f) for f in ("scrub_limit_setting", "scrub_limit_dropin")),
                *(shell_function(SETUP, f) for f in ("install_scrub_limit", "status_scrub_limit")),
                *lines,
            ]
        )
        return subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": self.tmp, "LC_ALL": "C", "T": self.tmp},
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_the_drop_in_is_in_the_install_record_until_it_is_removed(self) -> None:
        r = self.bash("install_scrub_limit 500M")
        self.assertEqual(r.returncode, 0, r.stderr)
        f = object_of(home_record(self.tmp)["files"][self.dropin])
        self.assertEqual((f["component"], f["kind"], f["mode"]), ("btrfs:core", "text", "0644"))
        r = self.bash('install_scrub_limit ""')
        self.assertEqual((r.returncode, r.stdout), (0, f"removed:   {self.dropin}\n"), r.stderr)
        self.assertEqual(home_record(self.tmp)["files"], {})
        # Deleted by hand: no limit drops the line left over.
        self.bash("install_scrub_limit 1G")
        os.unlink(self.dropin)
        r = self.bash('install_scrub_limit ""')
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual(home_record(self.tmp)["files"], {})

    def test_a_drop_in_from_before_the_record_is_adopted(self) -> None:
        # What an older CST wrote: the same drop-in, no record line.
        r = self.bash('scrub_limit_dropin 1G | cst_install_text "$SCRUB_DROPIN" 0644')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(home_record(self.tmp)["files"], {})
        r = self.bash("install_scrub_limit 1G")
        self.assertEqual(r.stdout, f"unchanged: {self.dropin}\n", r.stderr)
        self.assertIn(self.dropin, home_record(self.tmp)["files"])

    def write_conf(self, text: str) -> None:
        with open(self.conf, "w", newline="") as f:
            f.write(text)

    def test_bash_and_doctor_read_scrub_limit_alike(self) -> None:
        for text, want in SCRUB_CASES:
            with self.subTest(conf=text):
                if text:
                    self.write_conf(text)
                elif os.path.exists(self.conf):
                    os.unlink(self.conf)
                r = self.bash("scrub_limit_setting")
                bash = r.stdout if r.returncode == 0 else None
                setting, problem = scrub_limit_setting(parse_shell_assignments(text))
                doctor = None if problem else setting
                self.assertEqual((bash, doctor), (want, want), r.stderr)
                if want is None:
                    self.assertIn("SCRUB_LIMIT", r.stderr)
                elif want:
                    # The value bash itself gives when it sources the file (btrfs-snapshot does).
                    sourced = subprocess.run(
                        ["bash", "-c", '. "$1"; printf %s "$SCRUB_LIMIT"', "_", self.conf],
                        check=True,
                        capture_output=True,
                        text=True,
                        env={"PATH": "/usr/bin:/bin"},
                        stdin=subprocess.DEVNULL,
                    ).stdout
                    self.assertEqual(sourced, want)

    def test_install_writes_the_drop_in_for_a_limit_and_removes_it_for_none(self) -> None:
        self.write_conf("SCRUB_LIMIT=1G\n")
        r = self.bash('install_scrub_limit "$(scrub_limit_setting)"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"written:   {self.dropin}", r.stdout)
        with open(self.dropin) as f:
            lines = [ln for ln in f.read().splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(
            lines,
            [
                "[Service]",
                "ExecStartPre=/usr/sbin/btrfs scrub limit -a -l 1G /var",
                # "-": a reset that fails does not turn a good scrub into a failed unit.
                "ExecStopPost=-/usr/sbin/btrfs scrub limit -a -l 0 /var",
            ],
        )
        self.assertEqual(os.stat(self.dropin).st_mode & 0o777, 0o644)
        # A second install changes nothing.
        r = self.bash('install_scrub_limit "$(scrub_limit_setting)"')
        self.assertIn(f"unchanged: {self.dropin}", r.stdout)
        # No limit any more: the drop-in and its folder go; a second run finds nothing to do.
        self.write_conf("SCRUB_LIMIT=0\n")
        r = self.bash('install_scrub_limit "$(scrub_limit_setting)"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.dirname(self.dropin)))
        r = self.bash('install_scrub_limit ""')
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_an_unreadable_settings_file_is_refused_not_read_as_no_limit(self) -> None:
        # Read as "no limit", install would remove a drop-in that the setting asks for.
        self.write_conf("SCRUB_LIMIT=1G\n")
        os.chmod(self.conf, 0)
        r = self.bash('scrub_limit_setting && echo "rc=0"')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("rc=0", r.stdout)
        self.assertIn(f"{self.conf}: cannot read it", r.stderr)

    def test_doctor_reads_the_limit_back_from_the_drop_in_bash_writes(self) -> None:
        for limit in ("1G", "500m", "750K"):
            with self.subTest(limit=limit):
                r = self.bash(f"scrub_limit_dropin {limit}")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(scrub_dropin_limit(r.stdout), limit)

    def test_no_setting_writes_no_drop_in(self) -> None:
        r = self.bash('install_scrub_limit "$(scrub_limit_setting)"')
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(os.path.exists(os.path.dirname(self.dropin)))

    def status(self) -> list[str]:
        r = self.bash("status_scrub_limit 00000000-0000-0000-0000-000000000000")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_status_compares_the_setting_with_the_service(self) -> None:
        conf = self.conf
        self.assertEqual(
            self.status(),
            [f"  setting: none (SCRUB_LIMIT in {conf}; none = full speed)", "  service: no limit, as set"],
        )
        self.write_conf("SCRUB_LIMIT='1G'\n")
        self.assertEqual(self.status()[1], "  service: no limit, DIFFERS from the setting (run install)")
        self.bash('install_scrub_limit "$(scrub_limit_setting)"')
        self.assertEqual(
            self.status(),
            [f"  setting: 1G (SCRUB_LIMIT in {conf}; none = full speed)", "  service: 1G, as set"],
        )
        self.write_conf("SCRUB_LIMIT=$FAST\n")
        out = self.status()
        self.assertTrue(out[0].startswith(f"  setting: INVALID: {conf} line 1: "), out)
        self.assertEqual(out[1], "  service: 1G (fix the setting, then run install)")

    def test_status_shows_the_limit_sysfs_holds_now(self) -> None:
        for devid, limit in (("1", 8388608), ("2", 0)):
            d = os.path.join(self.tmp, "sys", "00000000-0000-0000-0000-000000000000", "devinfo", devid)
            os.makedirs(d)
            with open(os.path.join(d, "scrub_speed_max"), "w") as f:
                f.write(f"{limit}\n")
        self.assertEqual(self.status()[2], "  now in sysfs (bytes/s, 0 = no limit): device 1 8388608, device 2 0")

    def test_the_weekly_timer_spaces_out_its_catch_up(self) -> None:
        with open(os.path.join(BTRFS, "units", "btrfs-scrub-weekly.timer")) as f:
            timer = f.read().splitlines()
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=15min", timer)


# A toolchains or chrome-cache program as setup runs it: prints what reached it (the session
# variables, CST_PROG, the arguments) as JSON; exits with FAKE_RC.
FAKE_USER_TOOL = """import json, os, sys
keys = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "CST_PROG", "SUDO_USER", "HOME", "USER", "LOGNAME")
with open(os.environ["TOOL_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], **{k: os.environ.get(k) for k in keys}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


class ReportLauncherTest(unittest.TestCase):
    """btrfs-setup.sh's install_report_launcher and remove_report_launcher: the stall-report launcher in
    the target user's home, listed in that user's install record (HOME_INSTALLER keeps it in the test's
    folder) under btrfs:report-launcher, and removed or kept with its line dropped."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-launcher-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.shim = os.path.join(self.tmp, "home", ".local", "bin", "btrfs-stall-report")

    def bash(self, *lines: str) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                HOME_INSTALLER,
                *(
                    shell_function(SETUP, f)
                    for f in (
                        "shim_content",
                        "install_report_launcher",
                        "mark_report_launcher",
                        "remove_report_launcher",
                    )
                ),
                f'target_user="$(id -un)"; shim="{self.shim}"',
                *lines,
            ]
        )
        r = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "T": self.tmp, "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def not_recorded(self) -> str:
        """install_report_launcher's warning when the installer fails (the target user: this account)."""
        user = pwd.getpwuid(os.getuid()).pw_name
        return (
            f"WARNING: {self.shim} may not have been installed or updated, and the install record of {user} "
            "was not updated for it (see above)\n"
        )

    def test_install_records_the_launcher_and_marks_it(self) -> None:
        self.bash("install_report_launcher", "mark_report_launcher")
        self.assertEqual(os.stat(self.shim).st_mode & 0o777, 0o755)
        rec = home_record(self.tmp)
        f = object_of(rec["files"][self.shim])
        self.assertEqual((f["component"], f["kind"], f["mode"]), ("btrfs:report-launcher", "text", "0755"))
        self.assertEqual(rec["components"], {"btrfs:report-launcher": "done"})
        r = self.bash("install_report_launcher")
        self.assertEqual(r.stdout, f"unchanged: {self.shim}\n")

    def test_remove_takes_our_launcher_and_its_line(self) -> None:
        self.bash("install_report_launcher")
        r = self.bash("remove_report_launcher")
        self.assertEqual(r.stdout, f"removed:   {self.shim}\n")
        self.assertFalse(os.path.lexists(self.shim))
        self.assertEqual(home_record(self.tmp), {"files": {}, "components": {}})

    def test_remove_keeps_a_file_without_the_marker_and_drops_its_line(self) -> None:
        self.bash("install_report_launcher")
        Path(self.shim).write_text("#!/bin/sh\necho mine\n")
        r = self.bash("remove_report_launcher")
        self.assertEqual(r.stdout, "")
        self.assertEqual(Path(self.shim).read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(home_record(self.tmp)["files"], {})

    def test_an_installer_that_fails_does_not_stop_the_removal(self) -> None:
        # The user's install record was written by a newer CST: the installer refuses it. Uninstall
        # goes on: our launcher goes as before the record, a file of the user stays, the record is
        # left as it is.
        newer = '{"format": 999, "files": {}, "components": {}}\n'
        record = os.path.join(self.tmp, "home", USER_RECORD_REL)
        for mine in (False, True):
            with self.subTest(users_own_file=mine):
                self.bash("install_report_launcher")
                if mine:
                    Path(self.shim).write_text("#!/bin/sh\necho mine\n")
                Path(record).write_text(newer)
                r = self.bash("remove_report_launcher", "echo went-on")
                self.assertTrue(r.stdout.endswith("went-on\n"), r.stdout)
                self.assertIn("written by a newer CST", r.stderr)
                self.assertIn(f"was not changed (see above); {self.shim} goes only if this script wrote it\n", r.stderr)
                self.assertEqual(os.path.exists(self.shim), mine)
                self.assertEqual(Path(record).read_text(), newer)
            os.unlink(record)
            if mine:
                os.unlink(self.shim)

    def test_an_installer_that_fails_does_not_stop_the_install(self) -> None:
        # The user's install record was written by a newer CST: the installer refuses it. Install goes
        # on (the services after the launcher still get enabled): the launcher is left as it is, the
        # record is left as it is, and the launcher is not marked done.
        newer = '{"format": 999, "files": {}, "components": {}}\n'
        record = os.path.join(self.tmp, "home", USER_RECORD_REL)
        os.makedirs(os.path.dirname(record), exist_ok=True)
        Path(record).write_text(newer)
        r = self.bash("install_report_launcher", "mark_report_launcher", "echo went-on")
        self.assertEqual(r.stdout, "went-on\n")
        self.assertIn("written by a newer CST", r.stderr)
        self.assertIn(self.not_recorded(), r.stderr)
        self.assertFalse(os.path.lexists(self.shim))
        self.assertEqual(Path(record).read_text(), newer)

    def test_a_record_that_cannot_be_written_after_the_launcher_is_warned_about_and_not_marked(self) -> None:
        # The installer writes the launcher, then fails to save the record (here its folder is a file; a
        # full disk does the same). The launcher is there, without its record line: the warning says it
        # may have been written, install goes on, and the launcher is not marked done.
        state = os.path.dirname(os.path.join(self.tmp, "home", USER_RECORD_REL))
        os.makedirs(os.path.dirname(state))
        Path(state).write_text("not a folder\n")
        r = self.bash("install_report_launcher", "mark_report_launcher", "echo went-on")
        self.assertTrue(r.stdout.endswith("went-on\n"), r.stdout)
        self.assertIn("cst installer: cannot write", r.stderr)
        self.assertIn(self.not_recorded(), r.stderr)
        expected = self.bash("shim_content").stdout
        self.assertEqual(Path(self.shim).read_text(), expected)
        self.assertEqual(Path(state).read_text(), "not a folder\n")

    def test_a_user_without_a_home_folder_does_not_stop_the_removal(self) -> None:
        # The installer cannot run as the user (no home folder to keep the record in): our launcher
        # goes as before the record, and uninstall goes on.
        self.bash("install_report_launcher")
        no_home = """_cst_user_home() { echo "cst-install.bash: no home folder for user '$1'" >&2; return 1; }"""
        r = self.bash(no_home, "remove_report_launcher", "echo went-on")
        self.assertEqual(r.stdout, f"removed '{self.shim}'\nwent-on\n")
        self.assertIn("no home folder for user", r.stderr)
        self.assertFalse(os.path.lexists(self.shim))

    def test_remove_keeps_a_link_of_the_user_that_leads_nowhere(self) -> None:
        # The user's own launcher, a link into a folder that is not mounted now: it is not "no file",
        # so it is kept (as before the record), and its line dropped.
        self.bash("install_report_launcher")
        os.unlink(self.shim)
        os.symlink(os.path.join(self.tmp, "not-mounted", "stall-report"), self.shim)
        r = self.bash("remove_report_launcher")
        self.assertEqual(r.stdout, "")
        self.assertTrue(os.path.islink(self.shim))
        self.assertEqual(home_record(self.tmp)["files"], {})

    def test_remove_drops_the_line_of_a_launcher_already_gone(self) -> None:
        self.bash("install_report_launcher")
        os.unlink(self.shim)
        r = self.bash("remove_report_launcher")
        self.assertEqual(r.stdout, "")
        self.assertEqual(home_record(self.tmp)["files"], {})

    def test_no_target_user_writes_and_removes_nothing(self) -> None:
        r = self.bash('shim=""; target_user=""', "install_report_launcher", "remove_report_launcher")
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "home")))

    def test_a_launcher_from_before_the_record_is_adopted(self) -> None:
        # What an older CST wrote: the same launcher, no record line.
        self.bash('shim_content | cst_install_text "$shim" 0755 "$target_user"')
        self.assertEqual(home_record(self.tmp)["files"], {})
        r = self.bash("install_report_launcher")
        self.assertEqual(r.stdout, f"unchanged: {self.shim}\n")
        self.assertIn(self.shim, home_record(self.tmp)["files"])


class SeenCall(TypedDict):
    """One call of FAKE_USER_TOOL."""

    argv: list[str]
    XDG_RUNTIME_DIR: str | None
    DBUS_SESSION_BUS_ADDRESS: str | None
    CST_PROG: str | None
    SUDO_USER: str | None
    HOME: str | None
    USER: str | None
    LOGNAME: str | None


class UserStepsTest(unittest.TestCase):
    """setup's steps as the user (snap_toolchains, snap_chrome, snap_user_configs) run through
    cst_as_user: runuser with that user's session variables, whatever root's environment holds.
    runuser is a function that logs and runs the command as the current user; the programs are a
    fake that records what reached it; the user's runtime folder of the systemd/private check is
    in the test's folder (SNAP_USER_RUN)."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-snap-user-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = os.path.join(self.tmp, "run", str(os.getuid()))
        self.tool = os.path.join(self.tmp, "fake-tool")
        with open(self.tool, "w") as f:
            f.write(FAKE_USER_TOOL)
        self.tool_log = os.path.join(self.tmp, "tool.log")
        me = pwd.getpwuid(os.getuid())
        # What a login of the user sets (step_down_argv's session); never root's.
        self.session = {
            "XDG_RUNTIME_DIR": f"/run/user/{me.pw_uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{me.pw_uid}/bus",
            "SUDO_USER": None,
            "HOME": me.pw_dir,
            "USER": me.pw_name,
            "LOGNAME": me.pw_name,
        }

    def socket_at(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(path)

    def run_bash(self, body: str, fake_rc: int = 0) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                f"SRC={BTRFS}; PROG='cst btrfs'",
                f'. "{REPO}/cst-install.bash"',
                '. "$SRC/setup-snapshots.bash"',
                'SNAP_USER_RUN="$T/run"; target_user="$(id -un)"; TOOLCHAINS_TOOL="$TOOL"; CHROME_TOOL="$TOOL"',
                'snap_home() { echo "$T/home"; }',
                'runuser() { echo "runuser $*" >>"$T/runuser.log"; shift 3; "$@"; }',
                body,
            ]
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "T": self.tmp,
            "TOOL": self.tool,
            "TOOL_LOG": self.tool_log,
            "FAKE_RC": str(fake_rc),
            "HOME": self.tmp,
            # What sudo hands root: its own runtime folder and bus, and SUDO_USER.
            "XDG_RUNTIME_DIR": "/run/user/0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
            "SUDO_USER": "someone",
            "USER": "root",
            "LOGNAME": "root",
        }
        return subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL
        )

    def seen(self) -> list[SeenCall]:
        with open(self.tool_log) as f:
            return [json.loads(line) for line in f]

    def test_the_users_session_variables_replace_roots(self) -> None:
        # The variables a login of the user sets, SUDO_USER removed, whether /run/user/UID exists
        # or not: cst_as_user reads the real /run/user, so the second case makes id -u answer a
        # uid that has no folder there. Before, XDG_RUNTIME_DIR and the bus were set only when
        # they existed, and SUDO_USER reached the programs.
        home = os.path.join(self.tmp, "home")
        missing = next(n for n in range(4_000_000_000, 4_000_000_100) if not os.path.lexists(f"/run/user/{n}"))
        for case, uid in (("the user's uid", None), ("no /run/user/UID", missing)):
            with self.subTest(case):
                if os.path.exists(self.tool_log):
                    os.remove(self.tool_log)
                fake_id = (
                    ""
                    if uid is None
                    else f'id() {{ if [[ "$1" == -u && $# == 2 ]]; then echo {uid}; else command id "$@"; fi; }}\n'
                )
                r = self.run_bash(fake_id + "snap_toolchains auto on --unless-off\nsnap_chrome apply")
                self.assertEqual(r.returncode, 0, r.stderr)
                session = dict(self.session)
                if uid is not None:
                    session["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
                    session["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
                self.assertEqual(
                    self.seen(),
                    [
                        {
                            "argv": ["auto", "on", "--unless-off", "--home", home],
                            "CST_PROG": "cst toolchains",
                            **session,
                        },
                        {"argv": ["apply", "--home", home], "CST_PROG": None, **session},
                    ],
                )

    def user_configs(self, fake_rc: int) -> subprocess.CompletedProcess[str]:
        r = self.run_bash("snap_user_configs", fake_rc=fake_rc)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            [s["argv"][:2] for s in self.seen()], [["config", "apply"], ["auto", "on"], ["apply", "--home"]]
        )
        return r

    def test_auto_on_without_a_running_user_manager_says_why(self) -> None:
        os.makedirs(self.run_dir)
        err = self.user_configs(1).stderr
        self.assertIn("NOTE: cst toolchains auto on --unless-off reported a problem (see above): ", err)
        self.assertIn(f"user manager is not running (no {self.run_dir}/systemd/private: no login session", err)
        self.assertIn("so toolchains:auto stays pending (cst doctor says so); run it again once", err)
        self.assertNotIn("was not turned on", err)

    def test_auto_on_failing_with_a_running_manager_keeps_the_general_note(self) -> None:
        self.socket_at(os.path.join(self.run_dir, "systemd", "private"))
        err = self.user_configs(1).stderr
        self.assertIn("cst toolchains auto on --unless-off reported a problem (see above); once it is fixed", err)
        self.assertNotIn("user manager is not running", err)

    def test_auto_on_that_works_prints_no_note(self) -> None:
        self.socket_at(os.path.join(self.run_dir, "systemd", "private"))
        self.assertEqual(self.user_configs(0).stderr, "")


class PendingAlertsStatusTest(unittest.TestCase):
    """status_pending_alerts (btrfs-setup.sh) runs the reader as the user who ran sudo through
    cst_as_user, with that user's session variables; as root itself without runuser. runuser
    is a function that logs and runs the command as the current user; the reader is a fake that
    prints what reached it."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-pending-status-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        with open(os.path.join(self.tmp, "cst-pending-alerts"), "w") as f:
            f.write(
                "import os, sys\nprint(sys.argv[1:], *(os.environ.get(k) for k in "
                '("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SUDO_USER", "HOME")))\n'
            )
        self.runuser_log = os.path.join(self.tmp, "runuser.log")

    def status(self, target_user: str) -> str:
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                f'. "{REPO}/cst-install.bash"',
                shell_function(SETUP, "status_pending_alerts"),
                'SRC="$T"; PROG=cst-btrfs-test; USER_UNIT_DIR="$T"; PENDING_UNIT=unit; OLD_ALERTS_DIR="$T/old"',
                'touch "$T/unit"',
                'runuser() { echo "runuser $*" >>"$T/runuser.log"; shift 3; "$@"; }',
                f"target_user={target_user}",
                "status_pending_alerts",
            ]
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "T": self.tmp,
            "HOME": self.tmp,
            # What sudo hands root.
            "XDG_RUNTIME_DIR": "/run/user/0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
            "SUDO_USER": "someone",
        }
        r = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        return r.stdout

    def test_the_reader_runs_as_the_user_with_the_users_session(self) -> None:
        # Before, plain runuser: root's runtime folder, bus and SUDO_USER reached the reader.
        me = pwd.getpwuid(os.getuid())
        out = self.status(me.pw_name)
        run = f"/run/user/{me.pw_uid}"
        self.assertEqual(out, f"  ['--list'] {run} unix:path={run}/bus None {me.pw_dir}\n")
        with open(self.runuser_log) as f:
            self.assertEqual(
                f.read(),
                f"runuser -u {me.pw_name} -- env -u SUDO_USER HOME={me.pw_dir} USER={me.pw_name} "
                f"LOGNAME={me.pw_name} XDG_RUNTIME_DIR={run} DBUS_SESSION_BUS_ADDRESS=unix:path={run}/bus "
                f"python3 -I -B {self.tmp}/cst-pending-alerts --list\n",
            )

    def test_without_a_user_it_runs_as_root_without_runuser(self) -> None:
        out = self.status('""')
        self.assertEqual(out, f"  ['--list'] /run/user/0 unix:path=/run/user/0/bus someone {self.tmp}\n")
        self.assertFalse(os.path.exists(self.runuser_log))


if __name__ == "__main__":
    unittest.main()
