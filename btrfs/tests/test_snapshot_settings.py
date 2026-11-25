# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot reads snapshots.conf as data: its filter (conf_line in
btrfs/btrfs-snapshot) and cst_core.domain.text.parse_snapshots_conf, which
cst doctor, the health check and cst vm use, judge every line of one shared
corpus (btrfs/tests/fixtures/snapshots-conf-corpus.conf) alike.

For each line (and the same line with a carriage return at its end): blank or
comment for both; or an assignment for both, with the same key and the value
bash gives it (the filter only decides; bash's eval of the accepted line gives
the value); or a problem for both, with the same reason. The filter's three
functions and its two patterns are cut out of the program for this: the
whole-program tests of what the reader does are in test_snapshot_hourly.py
(SettingsTest).

Run with: invowk cmd test module btrfs.tests.test_snapshot_settings
"""

import os
import re
import subprocess
import tempfile
import unittest

from btrfs.snapshot_runtime.domain import GuardError, guard_number
from btrfs.tests.snapshot_fakes import SCRIPT
from cst_core.domain.text import file_lines, parse_snapshots_conf
from tests.shell_source import shell_function

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "fixtures", "snapshots-conf-corpus.conf")

# Each line: its verdict and, for an assignment, the key and the value bash gives it (in hex).
HARNESS = r"""
while IFS= read -r line || [[ -n "$line" ]]; do
    conf_line "$line"; rc=$?
    if (( rc == 0 )); then
        value="$(unset "$CONF_KEY"; eval "$CONF_ASSIGN"; printf '%s' "${!CONF_KEY}" | /usr/bin/od -An -v -tx1 | /usr/bin/tr -d ' \n')"
        printf 'ok\t%s\t%s\n' "$CONF_KEY" "$value"
    elif (( rc == 1 )); then
        printf 'problem\t%s\n' "$CONF_WHY"
    else
        printf 'none\n'
    fi
done <"$1"
"""


def filter_text() -> str:
    """The filter of btrfs-snapshot: its two patterns and three functions."""
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    patterns = [m.group(0) for m in re.finditer(r"^CONF_Q_(?:WORD|ANSI)=.*$", text, re.MULTILINE)]
    assert len(patterns) == 2, patterns
    functions = [shell_function(SCRIPT, n) for n in ("conf_comment_after", "conf_line", "conf_assignment")]
    return "\n".join([*patterns, *functions])


def expected(line: str) -> tuple[str, ...]:
    """parse_snapshots_conf's verdict on one line, in the harness's form."""
    got = parse_snapshots_conf(line + "\n")
    if got.entries:
        (a,) = got.entries
        return ("ok", a.key, a.value.encode("utf-8", "surrogateescape").hex())
    if got.problems:
        return ("problem", got.problems[0].reason)
    return ("none",)


class CorpusTest(unittest.TestCase):
    def test_the_filter_and_parse_snapshots_conf_agree_on_every_line(self) -> None:
        with open(CORPUS, encoding="utf-8") as f:
            lines = file_lines(f.read())
        cases = [*lines, *(ln + "\r" for ln in lines if ln)]
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "lines")
            with open(src, "w", encoding="utf-8", newline="") as f:
                f.write("".join(c + "\n" for c in cases))
            r = subprocess.run(
                ["bash", "-c", filter_text() + HARNESS, "_", src],
                check=False,
                capture_output=True,
                text=True,
                cwd=d,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": d},
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            # The filter decides before anything runs: no line of the corpus ran a command.
            self.assertEqual(sorted(os.listdir(d)), ["lines"])
        got = [tuple(ln.split("\t")) for ln in r.stdout.splitlines()]
        self.assertEqual(len(got), len(cases))
        for case, verdict in zip(cases, got, strict=True):
            with self.subTest(case):
                self.assertEqual(verdict, expected(case))
        # The corpus has each kind of line.
        self.assertEqual({v[0] for v in got}, {"ok", "problem", "none"})
        self.assertGreater(sum(v[0] == "ok" for v in got), 30)
        self.assertGreater(sum(v[0] == "problem" for v in got), 30)


if __name__ == "__main__":
    unittest.main()


class GuardNumberTest(unittest.TestCase):
    """guard_number (the runtime's space guard) against bash's (( free <= X * GIB )) under the
    program's set -Euo pipefail: the same answer, or bash stopped where the runtime stops."""

    BASH = 'set -Euo pipefail\nX="$1"; free="$2"; GIB=1\nif (( free <= X * GIB )); then echo le; else echo gt; fi\n'

    def bash(self, value: str, free: int) -> str:
        r = subprocess.run(
            ["/usr/bin/bash", "-c", self.BASH, "_", value, str(free)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        return r.stdout.strip() if r.returncode == 0 else "stop"

    def test_the_same_answer_as_bash(self) -> None:
        values = ["250", " 250 ", "\t250", "0250", "0", "", "-5", "+7", "0x10", "0X1f", "08", "09", "abc", "x y", "2.5"]
        values += ["250GiB", "${X}", "'250'", "007", "999999999", "1+1", "250+0", "(250)", "2*125", "500/2", "7%3"]
        values += ["1/0", "-(-250)", "250 +", "(250", "1 2", "0x", "250+abc", "- 5"]
        # Powers and BASE#DIGITS numbers, which bash reads too. The runtime used to refuse them, so
        # the emergency purge never started when EMERGENCY_AT_OR_BELOW_GIB was written in base 2.
        values += ["2**7", "2 ** 7", "-2**2", "2**3**2", "3*2**3", "2**-1", "0**0", "2#10010110", "8#377", "10#250"]
        values += ["16#fF", "36#Zz", "37#Aa", "64#@_", "1#0", "65#1", "2#2", "2#", "#1", "2#1#1", "2**63", "7**30"]
        # 64-bit wrap-around of every way to a number, and a base with a leading 0 (an error in bash).
        values += ["9223372036854775808", "0x8000000000000000", "-(2**63)", "2**63/-1", "0xffffffffffffffff"]
        values += ["18446744073709551616", "-2**63%-1", "010#9", "08#1", "02#1"]
        for value in values:
            for free in (0, 7, 8, 16, 168, 249, 250, 251, 2**40):
                with self.subTest(value=value, free=free):
                    n = guard_number(value)
                    if n is GuardError.BAD_NUMBER:
                        want = "gt"  # bash: an error line, and the comparison is false
                    elif n is GuardError.NOT_A_NUMBER:
                        want = "stop"
                    else:
                        want = "le" if free <= n else "gt"
                    self.assertEqual(self.bash(value, free), want)

    def want(self, value: str, free: int) -> str:
        """What bash prints for VALUE and FREE, by guard_number's answer."""
        n = guard_number(value)
        if n is GuardError.BAD_NUMBER:
            return "gt"
        if n is GuardError.NOT_A_NUMBER:
            return "stop"
        return "le" if free <= n else "gt"

    def test_blanks_and_the_token_after_a_name_as_bash_reads_them(self) -> None:
        # bash's blanks in arithmetic are space, tab and newline: a no-break space, U+3000, \r, \f, \v,
        # U+001C, U+0085 or U+2028 is an error (the runtime skipped them as \s). And bash reads the token
        # after a name before the name's value: "x;" or "x 08" is that token's error (the comparison is
        # false, the run goes on), where the runtime stopped as for an unset name.
        nbsp, ideo, acute = "\N{NO-BREAK SPACE}", "\N{IDEOGRAPHIC SPACE}", "\N{LATIN SMALL LETTER E WITH ACUTE}"
        values = [f"1{nbsp}+ 1", f"{nbsp}1", f"1{ideo}", "1\r", "1\f", "1\v", "1\x1c", "1\x85", "1\N{LINE SEPARATOR}"]
        values += ["1 +\n1", f"a{acute}", f"x {acute}", f"x{nbsp}", "x;", "x $", "x @", "x.", "x'", 'x"', "x\\", "x\r"]
        values += ["x 08", "x 2#2", "x 1#0", f"x+{acute}", f"(x){nbsp}", "x)", "x(", "x[0]", "x 1", "x?", "x:1", "x,1"]
        values += ["x!", "x~", f"{acute} x", f"{acute}a", f"1{acute}", "x", "x y", "x#"]
        for value in values:
            for free in (0, 251, 2**40):
                with self.subTest(value=value, free=free):
                    self.assertEqual(self.bash(value, free), self.want(value, free))

    def test_the_token_after_a_chain_of_names_as_bash_reads_it(self) -> None:
        # bash reads the token after each name of a chain before the first name's value: in "x y;" the
        # ";" is its error (the comparison is false, the run goes on), where the runtime stopped as for
        # an unset name, as it did for "x;" alone.
        nbsp = "\N{NO-BREAK SPACE}"
        values = ["x y;", "x y 08", "x y z;", f"x y{nbsp}", "x y z 08", "x y z w;", "x y 2#2", "x y.", "1+x y;"]
        values += ["(x y;)", "x y 1", "x y", "x y z", "x y+1", "x y (", "x y)", "x y 7#1", "x y#"]
        for value in values:
            for free in (0, 251, 2**40):
                with self.subTest(value=value, free=free):
                    self.assertEqual(self.bash(value, free), self.want(value, free))

    def test_long_numbers_and_bases_as_bash_reads_them(self) -> None:
        # A decimal constant wraps at 64 bits at any length: 5000 digits are a number in bash (the
        # runtime read them as BAD_NUMBER past int()'s 4300 digits). Octal too, and BASE#DIGITS.
        values = ["1" * 5000, "9" * 4301, "0" + "7" * 30, "0" * 5000 + "1", "10#" + "1" * 5000, "2#" + "1" * 100]
        values += ["1" * 5000 + "#1", "2#101", "10#08", "36#zz", "36#ZZ", "37#a", "37#A", "64#@", "64#_"]
        # Digits and letters of other scripts are arithmetic errors in bash, in every locale.
        values += ["\N{ARABIC-INDIC DIGIT THREE}", "\N{SUPERSCRIPT TWO}", "\N{FULLWIDTH DIGIT ONE}"]
        values += ["\N{LATIN SMALL LETTER E WITH ACUTE}", "\N{CYRILLIC SMALL LETTER ZHE}"]
        values += [
            "2#\N{ARABIC-INDIC DIGIT THREE}",
            "36#\N{LATIN SMALL LETTER E WITH ACUTE}",
            "10#\N{FULLWIDTH DIGIT ONE}",
        ]
        for value in values:
            for free in (0, 251, 2**40):
                with self.subTest(value=value[:24], digits=len(value), free=free):
                    self.assertEqual(self.bash(value, free), self.want(value, free))
