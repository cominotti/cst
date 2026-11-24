# SPDX-License-Identifier: MPL-2.0
"""Behaviour tests for btrfs/cst-pending-alerts (no root and no desktop needed).

cst-pending-alerts runs at each graphical login. It shows the alerts that
btrfs-notify kept in /var/lib/cst-alerts/pending while no desktop session could
show them, and records what this user has seen in
~/.local/state/cst-alerts/delivered. These tests run the program against a
temporary folder (CST_ALERTS_DIR, CST_ALERTS_OWNER, CST_ALERTS_WAIT), with a
fake busctl (is a notification server up?) and a fake notify-send that writes
down what it was asked to show.

They pin what a user and the other readers of the same files see: the usage
line and exit codes, the --list lines, the notifications and their order, the
log lines, and the exact bytes of the record. A copy of another version (the
one installed under /usr/local, or the checkout) reads and writes the same
record, so its format is part of the contract.

Run with: invowk cmd test module btrfs.tests.test_pending_alerts
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Final, override
import unittest

from btrfs.pending_alerts.domain import PendingPaths, alert_paths

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "cst-pending-alerts")
# How the tests start the program.
RUN: Final[list[str]] = ["/usr/bin/python3", "-I", "-B", SCRIPT]

USAGE = "usage: cst-pending-alerts [--list]\n"
SUMMARY_TITLE = "{n} more storage alert(s) while nobody was logged in"
SUMMARY_BODY = (
    "Older and not critical (critical ones are always shown). Read them: cst-pending-alerts --list "
    "(or cst btrfs status)."
)
NOT_SHOWN = "  [NOT shown to you yet: next login]"
SHOWN = "  [shown to you]"
SUMMARIZED = "  [only counted in the summary at login; text not shown]"
FIRST = 1_700_000_000  # Tue 14 Nov 2023 22:13 UTC: long before this boot


def when(epoch: int) -> str:
    """The time as the program prints it (TZ=UTC, C locale; not within the first 10 minutes of this boot)."""
    return time.strftime("%a %d %b %H:%M", time.gmtime(epoch))


def find_mtime(path: str) -> str:
    """A file's mtime as GNU find's %T@ prints it, as the record keeps it: ten decimals."""
    ns = os.lstat(path).st_mtime_ns
    return f"{ns // 10**9}.{ns % 10**9:09d}0"


def boot_time() -> int:
    """btime of /proc/stat: when this boot started."""
    with open("/proc/stat", encoding="ascii") as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])
    raise AssertionError("no btime in /proc/stat")


class PendingAlertsTest(unittest.TestCase):
    tmp: str
    alerts: str
    pending: str
    home: str
    bin: str
    notify_log: str
    env: dict[str, str]
    now: int

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="pending-alerts-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.alerts = os.path.join(self.tmp, "alerts")
        self.pending = os.path.join(self.alerts, "pending")
        os.makedirs(self.pending)
        os.chmod(self.alerts, 0o755)
        os.chmod(self.pending, 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.notify_log = os.path.join(self.tmp, "notify.log")
        self.stub("busctl", 'echo "b ${BUS_ANSWER:-true}"')
        self.stub("notify-send", '{ printf "%s\\x1f" "$@"; printf "\\x1e"; } >>"$NOTIFY_LOG"\nexit "${NOTIFY_RC:-0}"')
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.home,
            "CST_ALERTS_DIR": self.alerts,
            "CST_ALERTS_OWNER": str(os.getuid()),
            "CST_ALERTS_WAIT": "0",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "NOTIFY_LOG": self.notify_log,
        }
        self.now = int(time.time())

    def stub(self, name: str, body: str) -> None:
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/bash\n{body}\n")
        os.chmod(path, 0o755)

    def alert(
        self,
        n: int,
        *,
        urgency: str = "critical",
        app: str = "btrfs-test",
        title: str = "Disk: problem",
        body: str = "the body\nline 2",
        first: int = FIRST,
        last: int | None = None,
        count: int | str = 1,
        age: int = 3600,
        text: str | None = None,
    ) -> str:
        """A pending alert as btrfs-notify keeps it (or text as it is); its name. age: seconds since its mtime."""
        name = f"{n:032x}.alert"
        path = os.path.join(self.pending, name)
        if text is None:
            text = (
                f"cst-alert 1\nurgency={urgency}\napp={app}\nfirst={first}\nlast={first if last is None else last}\n"
                f"count={count}\ntitle={title}\n\n{body}\n"
            )
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o644)
        ns = (self.now - age) * 10**9 + 123_456_789
        os.utime(path, ns=(ns, ns))
        return name

    def path(self, name: str) -> str:
        return os.path.join(self.pending, name)

    def run_prog(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*RUN, *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )

    def notifications(self) -> list[list[str]]:
        """What notify-send was asked to show, one argument list per call, in order."""
        try:
            with open(self.notify_log, encoding="utf-8") as f:
                data = f.read()
        except FileNotFoundError:
            return []
        return [call.split("\x1f")[:-1] for call in data.split("\x1e")[:-1]]

    @property
    def record(self) -> str:
        return os.path.join(self.home, ".local", "state", "cst-alerts", "delivered")

    def write_record(self, text: str, state: str | None = None) -> None:
        path = self.record if state is None else os.path.join(state, "delivered")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read_record(self) -> str:
        with open(self.record, encoding="utf-8") as f:
            return f.read()

    def shown(self, title: str, at: str, body: str = "the body\nline 2") -> list[str]:
        """notify-send's arguments for one critical alert of btrfs-test."""
        text = f"{at}\n\n{body}" if body else at
        return ["-u", "critical", "-a", "btrfs-test", "-i", "drive-harddisk", "--", title, text]

    # ---- the command line ------------------------------------------------------------------------------------

    def test_usage(self) -> None:
        for args in (["--bogus"], ["--help"], ["-l"], ["list"]):
            with self.subTest(args):
                r = self.run_prog(*args)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (2, "", USAGE))

    def test_only_the_first_argument_counts_and_an_empty_one_shows(self) -> None:
        self.alert(1)
        r = self.run_prog("--list", "extra")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn("btrfs-test: Disk: problem", r.stdout)
        r = self.run_prog("", "--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(self.notifications()), 1)

    def test_without_home_it_fails_before_anything_else(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "HOME"}
        self.alert(1)
        for args in ((), ("--list",), ("--bogus",)):
            with self.subTest(args):
                r = subprocess.run(
                    [*RUN, *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    timeout=60,
                )
                self.assertEqual((r.returncode, r.stdout), (1, ""))
                self.assertIn("HOME", r.stderr)
        self.assertEqual(self.notifications(), [])

    # ---- --list ----------------------------------------------------------------------------------------------

    def test_list_without_alerts(self) -> None:
        r = self.run_prog("--list")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "none\n", ""))
        os.rmdir(self.pending)
        r = self.run_prog("--list")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "none\n", ""))

    def test_list_lines_oldest_first(self) -> None:
        self.alert(2, urgency="normal", app="other-app", title="Second", last=FIRST + 7200, age=100)
        self.alert(1, count=3, last=FIRST + 3600, age=200)
        r = self.run_prog("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout,
            f"{when(FIRST + 3600)}  critical btrfs-test: Disk: problem (3 times){NOT_SHOWN}\n"
            f"{when(FIRST + 7200)}  normal   other-app: Second{NOT_SHOWN}\n",
        )
        self.assertEqual(r.stderr, "")
        self.assertFalse(os.path.exists(os.path.dirname(self.record)), "--list writes nothing")

    def test_list_marks_alerts_raised_at_boot(self) -> None:
        btime = boot_time()
        self.alert(1, first=btime + 30)
        self.alert(2, title="later", first=btime + 600, age=10)
        r = self.run_prog("--list")
        self.assertEqual(
            r.stdout.splitlines(),
            [
                f"at boot, {when(btime + 30)}  critical btrfs-test: Disk: problem{NOT_SHOWN}",
                f"{when(btime + 600)}  critical btrfs-test: later{NOT_SHOWN}",
            ],
        )

    def test_list_follows_the_callers_time_locale(self) -> None:
        # Times as date(1) prints them in the caller's locale (pt_BR when the machine has it, else C).
        self.alert(1)
        env = {**self.env, "LC_ALL": "pt_BR.UTF-8"}
        want = subprocess.run(
            ["date", "-d", f"@{FIRST}", "+%a %d %b %H:%M"],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=True,
        ).stdout.strip()
        r = self.run_prog("--list", LC_ALL="pt_BR.UTF-8")
        self.assertEqual(r.stdout, f"{want}  critical btrfs-test: Disk: problem{NOT_SHOWN}\n")

    def test_list_marks_what_this_user_has_seen(self) -> None:
        a = self.alert(1, title="shown", age=400)
        b = self.alert(2, title="summarized", urgency="normal", age=300)
        c = self.alert(3, title="whole seconds", age=200)
        d = self.alert(4, title="other version", age=100)
        sec_c = find_mtime(self.path(c)).split(".")[0]
        self.write_record(
            f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n"
            f"{b} {find_mtime(self.path(b))} summarized 1 {FIRST}\n"
            f"{c} {sec_c}\n"
            f"{d} {sec_c}.1234567890 shown 1 {FIRST}\n"
        )
        r = self.run_prog("--list")
        self.assertEqual(
            r.stdout.splitlines(),
            [
                f"{when(FIRST)}  critical btrfs-test: shown{SHOWN}",
                f"{when(FIRST)}  normal   btrfs-test: summarized{SUMMARIZED}",
                f"{when(FIRST)}  critical btrfs-test: whole seconds{SHOWN}",
                f"{when(FIRST)}  critical btrfs-test: other version{NOT_SHOWN}",
            ],
        )

    def test_the_record_lines_it_reads(self) -> None:
        a = self.alert(1, title="later line wins", age=300)
        b = self.alert(2, title="older later line loses", age=200)
        c = self.alert(3, title="unterminated last line", age=100)
        ma, mb, mc = (find_mtime(self.path(n)) for n in (a, b, c))
        self.write_record(
            f"{a} 1.5 shown 1 1\n"
            f"{a} {ma} summarized 2 {FIRST}\n"
            f"{b} {mb} summarized 1 {FIRST}\n"
            f"{b} 1 shown 1 1\n"
            f"BAD {mb} shown 1 1\n"
            f"{c.upper()} {mc} shown 1 1\n"
            f"{c} 12a shown\n"
            f"{c} {mc} shown 1 {FIRST}"
        )
        r = self.run_prog("--list")
        self.assertEqual(
            r.stdout.splitlines(),
            [
                f"{when(FIRST)}  critical btrfs-test: later line wins{SUMMARIZED}",
                f"{when(FIRST)}  critical btrfs-test: older later line loses{SUMMARIZED}",
                f"{when(FIRST)}  critical btrfs-test: unterminated last line{NOT_SHOWN}",
            ],
        )

    def test_list_reads_the_record_of_f44_tools_and_moves_nothing(self) -> None:
        a = self.alert(
            1, text=f"f44-alert 1\nurgency=normal\napp=f44\nfirst={FIRST}\nlast={FIRST}\ncount=1\ntitle=Old\n\nx\n"
        )
        old = os.path.join(self.home, ".local", "state", "f44-alerts")
        self.write_record(f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n", old)
        r = self.run_prog("--list")
        self.assertEqual(r.stdout, f"{when(FIRST)}  normal   f44: Old{SHOWN}\n")
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(os.path.dirname(self.record)))

    def test_xdg_state_home_holds_the_record(self) -> None:
        a = self.alert(1)
        state = os.path.join(self.tmp, "state")
        self.write_record(f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n", os.path.join(state, "cst-alerts"))
        r = self.run_prog("--list", XDG_STATE_HOME=state)
        self.assertEqual(r.stdout, f"{when(FIRST)}  critical btrfs-test: Disk: problem{SHOWN}\n")

    def test_only_trusted_well_formed_files_of_the_last_seven_days(self) -> None:
        self.alert(1, title="kept")
        self.alert(2, title="old", age=7 * 86400 + 120)
        big = self.alert(3, title="too big", body="x" * 16400)
        self.assertGreater(os.path.getsize(self.path(big)), 16384)
        writable = self.alert(4, title="group-writable")
        os.chmod(self.path(writable), 0o664)
        with open(os.path.join(self.pending, "abc.alert"), "w", encoding="utf-8") as f:
            f.write("cst-alert 1\nurgency=critical\napp=a\nfirst=1\nlast=1\ncount=1\ntitle=bad name\n\n")
        target = self.alert(5, title="link target")
        os.rename(self.path(target), os.path.join(self.tmp, "target"))
        os.symlink(os.path.join(self.tmp, "target"), self.path(target))
        r = self.run_prog("--list")
        self.assertEqual(r.stdout, f"{when(FIRST)}  critical btrfs-test: kept{NOT_SHOWN}\n")
        self.assertEqual(r.stderr, "")
        for folder in (self.pending, self.alerts):
            with self.subTest(folder):
                os.chmod(folder, 0o775)
                self.assertEqual(self.run_prog("--list").stdout, "none\n")
                os.chmod(folder, 0o755)
        r = self.run_prog("--list", CST_ALERTS_OWNER=str(os.getuid() + 1))
        self.assertEqual(r.stdout, "none\n")

    def test_the_newest_fifty(self) -> None:
        for n in range(52):
            self.alert(n + 1, title=f"t{n:02d}", age=1000 - n)
        lines = self.run_prog("--list").stdout.splitlines()
        self.assertEqual(len(lines), 50)
        self.assertIn("btrfs-test: t02", lines[0])
        self.assertIn("btrfs-test: t51", lines[-1])

    def test_fields_it_checks(self) -> None:
        good = "cst-alert 1\nurgency=critical\napp=ok\nfirst=1\nlast=1\ncount=1\ntitle=T\n\nbody\n"
        bad = [
            good.replace("cst-alert 1", "cst-alert 2"),
            good.replace("urgency=critical", "urgency=low"),
            good.replace("app=ok", "app=two words"),
            good.replace("app=ok", "app=" + "a" * 41),
            good.replace("title=T\n", ""),
            good.replace("count=1", "count=x"),
            good.replace("first=1", "first=1234567890123"),
            good.replace("urgency=critical", "urgency"),
        ]
        for n, text in enumerate(bad, 1):
            self.alert(n, text=text, age=1000 - n)
        r = self.run_prog("--list")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "none\n", ""))
        r = self.run_prog()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            r.stderr,
            "".join(
                f"<4>cst-pending-alerts: {n:032x}.alert: not a valid pending alert; skipped\n" for n in range(1, 9)
            ),
        )
        self.assertEqual(self.notifications(), [])

    def test_defaults_and_leading_zeros(self) -> None:
        self.alert(1, text="cst-alert 1\nurgency=normal\napp=a\ntitle=defaults\nother=x=y\n\n")
        self.alert(2, text="cst-alert 1\nurgency=normal\napp=a\ncount=003\nfirst=0010\nlast=0020\ntitle=zeros\n\n")
        r = self.run_prog("--list")
        self.assertEqual(
            r.stdout.splitlines(),
            [f"{when(0)}  normal   a: defaults{NOT_SHOWN}", f"{when(20)}  normal   a: zeros (3 times){NOT_SHOWN}"],
        )

    def test_how_it_reads_the_header(self) -> None:
        # A header line without "=" is its own key and value, the last line of a key wins, and a
        # file with no empty line after the header has no body.
        self.alert(1, text="cst-alert 1\nurgency=critical\napp=a\ntitle\n\nbody\n", age=30)
        self.alert(2, text="cst-alert 1\nurgency=critical\napp=a\ntitle=A\ntitle=B\n\nbody\n", age=20)
        self.alert(3, text="cst-alert 1\nurgency=critical\napp=a\ntitle=no body", age=10)
        r = self.run_prog()
        self.assertEqual(r.returncode, 0, r.stderr)
        at = f"Happened {when(0)}, while no desktop session could show it."
        self.assertEqual(
            [n[-2:] for n in self.notifications()],
            [["title", f"{at}\n\nbody"], ["B", f"{at}\n\nbody"], ["no body", at]],
        )

    def test_list_into_a_closed_pipe_stops_quietly(self) -> None:
        for n in range(5):
            self.alert(n + 1, age=100 + n)
        r, w = os.pipe()
        os.close(r)
        try:
            p = subprocess.run(
                [*RUN, "--list"],
                check=False,
                stdout=w,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=self.env,
                timeout=60,
            )
        finally:
            os.close(w)
        self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""))

    # ---- at a login ------------------------------------------------------------------------------------------

    def test_shows_an_alert_once_and_records_it(self) -> None:
        a = self.alert(1, last=FIRST + 60)
        r = self.run_prog()
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(
            r.stderr, f"<5>cst-pending-alerts: shown: btrfs-test: Disk: problem (last {when(FIRST + 60)})\n"
        )
        self.assertEqual(
            self.notifications(),
            [self.shown("Disk: problem", f"Happened {when(FIRST + 60)}, while no desktop session could show it.")],
        )
        self.assertEqual(self.read_record(), f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n")
        self.assertEqual(os.stat(self.record).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.record)).st_mode & 0o777, 0o700)
        r = self.run_prog()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(len(self.notifications()), 1)
        listed = self.run_prog("--list").stdout
        self.assertEqual(listed, f"{when(FIRST + 60)}  critical btrfs-test: Disk: problem{SHOWN}\n")

    def test_a_repeat_counts_only_what_is_new(self) -> None:
        self.alert(1)
        self.run_prog()
        self.alert(1, count=2, last=FIRST + 60, age=60)
        self.run_prog()
        self.alert(1, count=4, last=FIRST + 120, age=30)
        self.run_prog()
        self.assertEqual(
            [n[-1] for n in self.notifications()],
            [
                f"Happened {when(FIRST)}, while no desktop session could show it.\n\nthe body\nline 2",
                f"Happened again {when(FIRST + 60)}, while no desktop session could show it.\n\nthe body\nline 2",
                (
                    f"Happened 2 more times since you last saw it, while no desktop session could show it: last "
                    f"{when(FIRST + 120)}.\n\nthe body\nline 2"
                ),
            ],
        )

    def test_several_times_never_seen(self) -> None:
        self.alert(1, count=3, last=FIRST + 3600, body="")
        self.run_prog()
        at = f"Happened 3 times while no desktop session could show it: first {when(FIRST)}, last {when(FIRST + 3600)}."
        self.assertEqual(self.notifications(), [self.shown("Disk: problem", at, body="")])

    def test_a_new_first_occurrence_is_counted_from_the_start(self) -> None:
        a = self.alert(1, count=2)
        self.write_record(f"{a} 1.5 shown 1 {FIRST - 10}\n")
        self.run_prog()
        self.assertTrue(self.notifications()[0][-1].startswith("Happened 2 times while"))
        self.write_record(f"{a} 1.5 summarized 1 {FIRST}\n")
        self.run_prog()
        self.assertTrue(self.notifications()[1][-1].startswith("Happened 2 times while"))

    def test_title_and_body_are_bounded(self) -> None:
        self.alert(1, title="T" * 250, body="b" * 5000)
        self.run_prog()
        [call] = self.notifications()
        self.assertEqual(call[7], "T" * 200)
        self.assertEqual(call[8], f"Happened {when(FIRST)}, while no desktop session could show it.\n\n" + "b" * 4000)

    def test_critical_ones_one_by_one_and_old_normal_ones_summed_up(self) -> None:
        # Oldest first: n1 normal, n2 critical, n3..n10 normal, n11 critical. 8 at most: both critical ones
        # and the 6 newest normal ones; the 3 oldest normal ones (n1, n3, n4) go into one summary first.
        names = []
        for n in range(1, 12):
            urgency = "critical" if n in (2, 11) else "normal"
            names.append(self.alert(n, urgency=urgency, title=f"a{n:02d}", age=2000 - n, count=n))
        r = self.run_prog()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.notifications()
        summary = ["-u", "normal", "-a", "cst-pending-alerts", "-i", "drive-harddisk", "--"]
        self.assertEqual(calls[0], [*summary, SUMMARY_TITLE.format(n=3), SUMMARY_BODY])
        self.assertEqual([c[7] for c in calls[1:]], ["a02", "a05", "a06", "a07", "a08", "a09", "a10", "a11"])
        self.assertEqual([c[1] for c in calls[1:]], ["critical"] + ["normal"] * 6 + ["critical"])
        how = {n: ("summarized" if n in (1, 3, 4) else "shown") for n in range(1, 12)}
        self.assertEqual(
            self.read_record(),
            "".join(
                f"{names[n - 1]} {find_mtime(self.path(names[n - 1]))} {how[n]} {n} {FIRST}\n" for n in range(1, 12)
            ),
        )
        listed = self.run_prog("--list").stdout.splitlines()
        self.assertTrue(listed[0].endswith(SUMMARIZED), listed[0])
        self.assertTrue(listed[1].endswith(SHOWN), listed[1])
        self.assertEqual(self.run_prog().stderr, "")
        self.assertEqual(len(self.notifications()), 9)

    def test_nine_critical_ones_are_all_shown(self) -> None:
        for n in range(1, 10):
            self.alert(n, title=f"c{n}", age=100 - n)
        self.alert(10, urgency="normal", title="n", age=50)
        self.run_prog()
        calls = self.notifications()
        self.assertEqual(calls[0][7], SUMMARY_TITLE.format(n=1))
        self.assertEqual([c[7] for c in calls[1:]], [f"c{n}" for n in range(1, 10)])

    def test_without_a_notification_server_it_waits_for_the_next_login(self) -> None:
        self.alert(1)
        r = self.run_prog(BUS_ANSWER="false")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(
            r.stderr, "<4>cst-pending-alerts: no notification server after 0 s; trying at the next login\n"
        )
        self.assertEqual(self.notifications(), [])
        self.assertFalse(os.path.exists(os.path.dirname(self.record)))

    def test_a_notification_that_fails_is_not_recorded(self) -> None:
        self.alert(1)
        r = self.run_prog(NOTIFY_RC="1")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(len(self.notifications()), 1)
        self.assertFalse(os.path.exists(os.path.dirname(self.record)))

    def test_an_alert_that_turns_invalid_before_it_is_shown_is_skipped(self) -> None:
        # Each alert is read again right before it is shown: one rewritten while the program waited
        # for the desktop (here by busctl) is skipped, and nothing is recorded.
        a = self.alert(1)
        self.stub("busctl", f'printf "junk\\n" >"{self.path(a)}"\necho "b true"')
        r = self.run_prog()
        self.assertEqual(
            (r.returncode, r.stdout, r.stderr),
            (0, "", f"<4>cst-pending-alerts: {a}: not a valid pending alert; skipped\n"),
        )
        self.assertEqual(self.notifications(), [])
        self.assertFalse(os.path.exists(self.record))

    def test_nothing_to_show_asks_no_notification_server(self) -> None:
        self.stub("busctl", 'echo called >>"$NOTIFY_LOG.busctl"; echo "b true"')
        r = self.run_prog()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertFalse(os.path.exists(self.notify_log + ".busctl"))

    def test_at_login_the_record_of_f44_tools_moves(self) -> None:
        a = self.alert(1)
        state = os.path.join(self.home, ".local", "state")
        old = os.path.join(state, "f44-alerts")
        self.write_record(f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n", old)
        r = self.run_prog()
        self.assertEqual((r.returncode, r.stderr), (0, f"<6>cst-pending-alerts: moved {old} to {state}/cst-alerts\n"))
        self.assertEqual(self.notifications(), [])
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.read_record(), f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n")

    def test_the_record_is_rewritten_when_it_names_alerts_that_are_gone(self) -> None:
        # Lines for pending files that btrfs-notify has dropped since are left out. Before the fix, the
        # rewrite failed (and the alerts just shown came back at the next login) whenever the record's
        # last name in bash's hash order was such a line: with this many of them, almost always.
        a = self.alert(1)
        self.write_record("".join(f"{n:032x}.alert 1700000000.0000000000 shown 1 {FIRST}\n" for n in range(100, 160)))
        r = self.run_prog()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read_record(), f"{a} {find_mtime(self.path(a))} shown 1 {FIRST}\n")
        self.assertEqual(self.run_prog().stderr, "")
        self.assertEqual(len(self.notifications()), 1)

    def test_a_record_folder_it_cannot_make_fails_after_showing(self) -> None:
        self.alert(1)
        state = os.path.join(self.tmp, "a-file")
        with open(state, "w", encoding="utf-8") as f:
            f.write("x")
        r = self.run_prog(XDG_STATE_HOME=state)
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("<5>cst-pending-alerts: shown: btrfs-test: Disk: problem", r.stderr)
        self.assertEqual(len(self.notifications()), 1)

    def test_ctrl_c_while_waiting_for_the_desktop_ends_it_quietly(self) -> None:
        # Ctrl-C reaches the whole foreground group: the program ends by SIGINT, with no traceback and
        # no record written.
        self.alert(1)
        p = subprocess.Popen(
            RUN,
            env={**self.env, "CST_ALERTS_WAIT": "60", "BUS_ANSWER": "false"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(1.5)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.assertEqual((p.returncode, out, err), (-signal.SIGINT, b"", b""))
        self.assertFalse(os.path.exists(self.record))
        self.assertEqual(self.notifications(), [])

    def test_nul_bytes_are_dropped(self) -> None:
        # bash's read dropped every NUL byte of a pending file: the fields and the text are shown
        # without them (a NUL cannot be passed to notify-send at all).
        name = self.alert(
            1,
            text=f"cst-alert 1\nurgency=critical\napp=btrfs-test\nfirst={FIRST}\nlast={FIRST}\n"
            "count=1\ntitle=Disk: pro\0blem\n\nthe bo\0dy\nline 2\n",
        )
        listed = self.run_prog("--list")
        self.assertEqual(
            listed.stdout, f"{when(FIRST)}  critical btrfs-test: Disk: problem{NOT_SHOWN}\n", listed.stderr
        )
        r = self.run_prog()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        at = f"Happened {when(FIRST)}, while no desktop session could show it."
        self.assertEqual(self.notifications(), [self.shown("Disk: problem", at)])
        self.assertEqual(self.read_record(), f"{name} {find_mtime(self.path(name))} shown 1 {FIRST}\n")

    def test_a_record_it_cannot_replace_still_ends_well(self) -> None:
        # The folder is there but the new record cannot take the old one's place (here a folder holds
        # its name): as bash's "mv ... || rm -f", the run still exits 0, and the old record stays, so
        # the alert comes again at the next login.
        self.alert(1)
        os.makedirs(self.record)
        r = self.run_prog()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertIn("<5>cst-pending-alerts: shown: btrfs-test: Disk: problem", r.stderr)
        self.assertTrue(os.path.isdir(self.record))
        self.assertEqual(self.run_prog().returncode, 0)
        self.assertEqual(len(self.notifications()), 2)


class AlertPathsWaitTest(unittest.TestCase):
    def wait(self, value: str | None) -> int:
        env = {"HOME": "/home/u"} if value is None else {"HOME": "/home/u", "CST_ALERTS_WAIT": value}
        paths = alert_paths(env)
        assert isinstance(paths, PendingPaths)
        return paths.wait

    def test_the_wait_is_ascii_digits_or_zero(self) -> None:
        self.assertEqual(self.wait(None), 90)
        self.assertEqual(self.wait(""), 90)
        self.assertEqual(self.wait("0"), 0)
        self.assertEqual(self.wait("060"), 60)
        for bad in ("-1", "1.5", "x", "\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}"):
            with self.subTest(bad=bad):
                self.assertEqual(self.wait(bad), 0)

    def test_a_wait_past_int_s_digit_limit_is_zero(self) -> None:
        # isascii() and isdigit() took 5000 digits, and int() raised ValueError: a traceback at login.
        self.assertEqual(self.wait("1" * 5000), 0)


if __name__ == "__main__":
    unittest.main()
