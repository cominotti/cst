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

from btrfs.tests.snapshot_fakes import SCRIPT
from btrfs.tests.test_snapshot_runtime import shell_function
from cst_core.domain.text import file_lines, parse_snapshots_conf

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
    patterns = [m.group(0) for m in re.finditer(r"^CONF_Q_(?:WORD|ANSI)=.*$", text, re.M)]
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
