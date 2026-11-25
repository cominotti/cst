# SPDX-License-Identifier: MPL-2.0
"""The health check's and the NAS run's numbers against the old bash's arithmetic (Hypothesis).

btrfs-health-check and btrfs-snapshot were bash; their Python ports keep bash's 64-bit arithmetic,
which wraps around past 2**63 - 1, and bash's reads of a number (a leading 0 octal, 10#, hex). Each
property draws values around the bounds (0, +-2**63, +-2**64) and anywhere in [-2**65, 2**65],
calls one domain function, and has the real bash evaluate the old line's expression with the same
values put in as text (the text bash read, for a read). One bash -c per example, every expression in
a subshell of its own, so an arithmetic error ends only its line ("error"), with PATH only and
LC_ALL=C, stdin /dev/null and a timeout.

The old programs, the oracle: git show b819032f^:btrfs/btrfs-health-check and
git show 9f1c0569^:btrfs/btrfs-snapshot; each property names its line.

A value a reader gives is a 64-bit one (bash_int wraps), and is drawn as one (WORD) where the old
line compares it; a value only subtracted can be any (WIDE): bash wraps the text it reads, and a
wrapped difference is the same as the difference of the wrapped values.
"""

import subprocess
from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from btrfs.health_check.domain import (
    bash_decimal,
    bash_int,
    boot_since,
    critical_warning_value,
    nas_overdue,
    scrub_age_days,
    snapshots_overdue,
)
from btrfs.snapshot_runtime.domain import days_between, latest_epoch, next_check_failures
from tests.mbt import profiles

profiles.load()

BASH_ENV: Final = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
BASH_TIMEOUT_S: Final = 30
INT_MIN: Final = -(2**63)
INT_MAX: Final = 2**63 - 1
NEAR: Final = 10**6

# A value as a reader of bash's arithmetic gives it: 64-bit, its bounds drawn often.
WORD: Final = st.one_of(
    st.sampled_from([0, 1, -1, INT_MAX, INT_MIN, INT_MAX - 1, INT_MIN + 1, 1_700_000_000]),
    st.integers(INT_MAX - NEAR, INT_MAX),
    st.integers(INT_MIN, INT_MIN + NEAR),
    st.integers(-NEAR, NEAR),
    st.integers(INT_MIN, INT_MAX),
)
# Any integer, the 64-bit bounds and the next ones (2**64) among them.
WIDE: Final = st.one_of(
    WORD,
    st.sampled_from([2**63, -(2**63) - 1, 2**64, -(2**64), 2**64 - 1, 2**64 + 1, 2**65, -(2**65)]),
    st.integers(2**64 - NEAR, 2**64 + NEAR),
    st.integers(-(2**64) - NEAR, -(2**64) + NEAR),
    st.integers(-(2**65), 2**65),
)
# ASCII digits as a file or a tool prints them: leading zeros, 1 to 5000 digits (past int()'s limit).
DIGITS: Final = st.one_of(
    st.text("0123456789", min_size=1, max_size=40),
    st.builds(
        lambda zeros, rest: "0" * zeros + rest, st.integers(0, 3), st.text("0123456789", min_size=1, max_size=25)
    ),
    st.text("0123456789", min_size=4290, max_size=5000),
    st.integers(0, 2**66).map(str),
)
HEX: Final = st.builds(
    lambda x, digits: f"0{x}{digits}", st.sampled_from("xX"), st.text("0123456789abcdefABCDEF", min_size=1, max_size=40)
)
# What nvme-cli's critical_warning field may hold, without blanks (smart_raw drops them).
CRITICAL: Final = st.one_of(DIGITS, HEX, st.sampled_from(["", "0x", "abc", "0x1g", "-1", "1.5"]))


def bash(script: str, *values: object) -> list[str]:
    """The stdout lines of bash -c SCRIPT with VALUES as its arguments, as text."""
    r = subprocess.run(
        ["bash", "-c", script, "bash", *(str(v) for v in values)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=BASH_ENV,
        timeout=BASH_TIMEOUT_S,
        check=False,
    )
    assert r.returncode == 0, f"bash exit {r.returncode}: {r.stderr!r}"
    return r.stdout.splitlines()


def read_as_bash(n: int | None) -> str:
    """A read's result as the oracle prints it."""
    return "error" if n is None else str(n)


class HealthCheckArithmeticTest(profiles.MbtTestCase):
    # health check line 201: age_days=$(( ( $(date +%s) - started_epoch ) / 86400 )).
    @given(now=WORD, started=WIDE)
    def test_scrub_age_days(self, now: int, started: int) -> None:
        got = bash('started=$2; ( echo "$(( ( $1 - started ) / 86400 ))" ) 2>/dev/null || echo error', now, started)
        self.assertEqual(got, [str(scrub_age_days(now, started))])

    # health check line 314: boot_since=$(( $(date +%s) - $(awk '{print int($1)}' /proc/uptime) )).
    @given(now=WORD, uptime=WIDE)
    def test_boot_since(self, now: int, uptime: int) -> None:
        got = bash('( echo "$(( $1 - $2 ))" ) 2>/dev/null || echo error', now, uptime)
        self.assertEqual(got, [str(boot_since(now, uptime))])

    # health check lines 318-319: (( $(date +%s) - last > 3 * 3600 )) && (( $(date +%s) - awake_since > 20 * 60 )).
    @given(now=WORD, last=WORD, awake=WIDE)
    def test_snapshots_overdue(self, now: int, last: int, awake: int) -> None:
        script = (
            "last=$2 awake_since=$3; ( if (( $1 - last > 3 * 3600 )) && (( $1 - awake_since > 20 * 60 )); "
            "then echo 1; else echo 0; fi ) 2>/dev/null || echo error"
        )
        self.assertEqual(bash(script, now, last, awake), [str(int(snapshots_overdue(now, last, awake)))])

    # health check line 333: (( nas_last > 0 && $(date +%s) - nas_last > 8 * 86400 )).
    @given(now=WORD, nas_last=WORD)
    def test_nas_overdue(self, now: int, nas_last: int) -> None:
        script = (
            "nas_last=$2; ( if (( nas_last > 0 && $1 - nas_last > 8 * 86400 )); then echo 1; else echo 0; fi )"
            " 2>/dev/null || echo error"
        )
        self.assertEqual(bash(script, now, nas_last), [str(int(nas_overdue(now, nas_last)))])


class NasArithmeticTest(profiles.MbtTestCase):
    # btrfs-snapshot line 1092 (and 1369): age_d=$(( ($(date +%s) - last) / 86400 )).
    @given(now=WORD, last=WORD)
    def test_days_between(self, now: int, last: int) -> None:
        got = bash('last=$2; ( echo "$(( ($1 - last) / 86400 ))" ) 2>/dev/null || echo error', now, last)
        self.assertEqual(got, [str(days_between(now, last))])

    # btrfs-snapshot line 1348: fails=$(( $(cat nas-check-failures || echo 0) + 1 )), the count as text.
    @given(before=WORD)
    def test_next_check_failures(self, before: int) -> None:
        got = bash('( echo "$(( $1 + 1 ))" ) 2>/dev/null || echo error', before)
        self.assertEqual(got, [str(next_check_failures(before))])

    # btrfs-snapshot lines 1078-1081: nas_since's loop over nas-last-ok and nas-enabled-at (None: no file,
    # an empty $(cat)); best printed as bash arithmetic reads it ("010" is 8).
    @given(last_ok=st.one_of(st.none(), DIGITS, WORD.map(str)), enabled_at=st.one_of(st.none(), DIGITS, WORD.map(str)))
    def test_latest_epoch(self, last_ok: str | None, enabled_at: str | None) -> None:
        script = """best=0
for v in "$1" "$2"; do
    [[ "${v}" =~ ^[0-9]+$ ]] && (( v > best )) && best="${v}"
done 2>/dev/null
echo "$(( best ))"
"""
        got = bash(script, last_ok or "", enabled_at or "")
        self.assertEqual(got, [str(latest_epoch([last_ok, enabled_at]))])


class BashReadsTest(profiles.MbtTestCase):
    # bash arithmetic's read of digits in a variable: $(( v )) (a leading 0 octal, 08 an error).
    @given(text=DIGITS)
    def test_bash_int(self, text: str) -> None:
        got = bash('v=$1; ( echo "$(( v ))" ) 2>/dev/null || echo error', text)
        self.assertEqual(got, [read_as_bash(bash_int(text))])

    # bash's 10#: $(( 10#v )), decimal with a leading 0.
    @given(text=DIGITS)
    def test_bash_decimal(self, text: str) -> None:
        got = bash('( echo "$(( 10#$1 ))" ) 2>/dev/null || echo error', text)
        self.assertEqual(got, [read_as_bash(bash_decimal(text))])

    # health check lines 41-48 and 53: crit=$(( crit )) for hex, $(( 10#crit )) for decimal, else no number.
    @given(raw=CRITICAL)
    def test_critical_warning_value(self, raw: str) -> None:
        script = """crit=$1
(
    if [[ "${crit}" =~ ^0[xX][0-9a-fA-F]+$ ]]; then crit=$(( crit ))
    elif [[ "${crit}" =~ ^[0-9]+$ ]]; then crit=$(( 10#${crit} ))
    elif [[ -n "${crit}" ]]; then echo unreadable; exit 0
    fi
    echo "$(( ${crit:-0} ))"
) 2>/dev/null || echo error
"""
        value = critical_warning_value(raw)
        self.assertEqual(bash(script, raw), ["unreadable" if value is None else str(value)])
