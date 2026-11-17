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

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BTRFS = os.path.dirname(HERE)
REPO = os.path.dirname(BTRFS)
SETUP = os.path.join(REPO, "btrfs-setup.sh")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from btrfs.tests.test_snapshot_runtime import shell_function
from cst_core.adapters.files import read_text
from cst_core.domain.text import parse_shell_assignments, parse_snapshots_conf

# The log says, for each clean, whether the unit file was still there.
STUBS = """
set -Eeuo pipefail
systemctl() {
    echo "systemctl $*" >>"$LOG"
    if [[ "$1" == clean && -e "${UNIT_DIR}/${!#}" ]]; then echo "  (${!#} file present)" >>"$LOG"; fi
    [[ " $* " != *" ${FAIL:-none} "* ]]
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
    m = re.search(rf"^{name}=(\(.*?\)|.*)$", text, re.M | re.S)
    if m is None:
        raise AssertionError(f"{name}= not found in {SETUP}")
    return m.group(0)


class TimerRemovalTest(unittest.TestCase):
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

    def test_disable_timers_disables_in_one_call_then_forgets_only_the_timers(self) -> None:
        self.touch(os.path.join(self.units, "x.timer"))
        r = self.run_bash("disable_timers x.timer x.service gone.timer\necho went-on", fail="disable")
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now x.timer x.service gone.timer",
                "systemctl clean --what=state x.timer",
                "  (x.timer file present)",
                "systemctl clean --what=state gone.timer",
            ],
        )
        self.assertIn("went-on", r.stdout)  # a failed disable (a unit that is not there) is ignored

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
                "systemctl disable --now docker-gc-recorder.service docker-gc.service docker-gc.timer",
                "systemctl clean --what=state docker-gc.timer",
                "  (docker-gc.timer file present)",
            ],
        )
        self.assertIn("docker-gc disabled", r.stdout)
        self.assertIn("docker-gc disable --purge", r.stdout)

    def docker_gc_body(self) -> list[str]:
        """docker_gc as btrfs-setup.sh defines it, its recorded state in the test's folder and the
        real cst_installed_dests (read-only: the manifest's destinations)."""
        return [
            f'. "{REPO}/cst-install.bash"',
            assignment("DOCKER_GC_UNITS"),
            'DOCKER_GC_STATE="$T/docker-gc-state"',
            shell_function(SETUP, "docker_gc_files"),
            shell_function(SETUP, "docker_gc"),
        ]

    def test_docker_gc_disable_purge_removes_program_units_and_state(self) -> None:
        self.touch(os.path.join(self.units, "docker-gc.timer"))
        state = os.path.join(self.tmp, "docker-gc-state")
        os.makedirs(state)
        self.touch(os.path.join(state, "last-used.json"))
        r = self.run_bash("\n".join([*self.docker_gc_body(), "docker_gc disable --purge"]))
        units = ["docker-gc-recorder.service", "docker-gc.service", "docker-gc.timer"]
        self.assertEqual(
            self.calls(),
            [
                "systemctl disable --now " + " ".join(units),
                "systemctl clean --what=state docker-gc.timer",
                "  (docker-gc.timer file present)",
                "rm -fv /usr/local/sbin/docker-gc",
                *(f"rm -fv /etc/systemd/system/{u}" for u in units),
                f"rm -rf {state}",
                "systemctl daemon-reload",
                "systemctl reset-failed " + " ".join(units),
            ],
        )
        self.assertFalse(os.path.exists(state))
        self.assertIn("docker-gc removed", r.stdout)
        # A second purge: the state is gone, so it is not removed again.
        os.unlink(self.log)
        self.run_bash("\n".join([*self.docker_gc_body(), "docker_gc disable --purge"]))
        self.assertNotIn(f"rm -rf {state}", self.calls())

    def test_docker_gc_disable_refuses_an_unknown_argument(self) -> None:
        body = "\n".join([*self.docker_gc_body(), "(docker_gc disable --all) && exit 9 || echo rc=$?"])
        r = self.run_bash(body)
        self.assertIn("rc=2", r.stdout)
        self.assertIn("only --purge", r.stderr)
        self.assertEqual(self.calls(), [])

    def docker_gc_status(self, enabled: str, rc: int) -> str:
        # systemctl as systemd 262 answers: is-enabled prints its word and exits 1 (disabled) or 4
        # (not-found); the Python status part is a stub.
        body = [
            f'systemctl() {{ case "$1" in is-enabled) printf "%s\\n" {enabled!r}; return {rc} ;; '
            "is-active) echo inactive; return 3 ;; esac; }",
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
            "\n".join(
                [
                    '. "$SRC/setup-snapshots.bash"',
                    'SNAP_BOOT_FLAG="$T/convert-at-boot"; BULK_FLAG="$T/bulk-at-boot"; SNAP_CONF="$T/snapshots.conf"',
                    "target_user=u",
                    call,
                ]
            )
        )
        return [c for c in self.calls() if not c.startswith("rm ")]

    def test_snapshots_disable_forgets_both_timers(self) -> None:
        self.assertEqual(
            self.snapshots("snap_disable"),
            [
                "systemctl disable --now btrfs-snapshot.timer btrfs-snapshot-nas.timer btrfs-subvol-convert.service",
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


class SnapshotsConfTest(unittest.TestCase):
    """snap_conf_set (btrfs/setup-snapshots.bash) writes snapshots.conf in quotes that bash and
    cst_core's strict reader read alike, and rewrites the printf %q lines an older CST wrote."""

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
        """body after setup-snapshots.bash is sourced as the frontend sources it; SNAP_CONF and
        the other paths it writes point into the test's folder. values: environment variables
        (surrogate escapes stand for bytes that are not UTF-8)."""
        script = "\n".join(
            [
                STUBS,
                f"SRC={BTRFS}; PROG=cst-btrfs-test",
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
        """Same content, mode changed by hand: the mode goes back to 0644 (as the old writer's
        install -m 0644 did on every run); the file itself is not rewritten."""
        self.set_values({"NAS_ENABLED": "1"})
        os.chmod(self.conf, 0o600)
        before = os.stat(self.conf)
        self.set_values({"NAS_ENABLED": "1"})
        after = os.stat(self.conf)
        self.assertEqual(oct(after.st_mode & 0o777), "0o644")
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

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
cst_install_text() { cat >"$1"; }
snap_install_runtime() { echo "runtime installed"; }
snap_user_configs() { :; }
snap_activate() { echo "activated"; }
snap_status() { echo "status"; }
function /usr/local/sbin/btrfs-snapshot { echo "btrfs-snapshot $*"; }
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
        # apply: the file is in quotes, every value as bash read the old file.
        r = self.run_bash(self.SETUP_STUBS + "snap_setup --yes")
        self.assertIn("activated", r.stdout)
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


class StallMonitorUnitTest(unittest.TestCase):
    """btrfs/units/btrfs-stall-monitor.service as shipped."""

    def service_lines(self) -> list[str]:
        with open(os.path.join(BTRFS, "units", "btrfs-stall-monitor.service")) as f:
            text = f.read()
        m = re.search(r"^\[Service\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
        assert m is not None, text
        return [ln for ln in m.group(1).splitlines() if ln and not ln.startswith("#")]

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


if __name__ == "__main__":
    unittest.main()
