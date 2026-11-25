# SPDX-License-Identifier: MPL-2.0
"""The game profile's state files, game.state and game.intended, written and read back (Hypothesis).

Their fields are written as bash's `printf %q` writes them (cst_core.domain.text.printf_q, through
cpu.game.domain.record_line) and read back as bash's `eval "rec=(${line})"` reads them
(printf_q_words, through records): an installed bash game-profile of an older CST reads the same
files, and greps them for its own quoted form. A field is generated as bytes without NUL (what a
bash variable can hold: ASCII, high bytes alone, and the UTF-8 of any character, mixed), decoded
with surrogateescape, as the profile gets a cgroup folder's name from glob. Each example writes
its files in a scratch folder of its own (cpu/tests/fakes.py game_scratch).

Checked:
- Q1, our writer, our reader: records appended with GameProfile.append_state and read with
  records(state_text()), what 'off' reads, give back each field, in order, and no line is
  unreadable. A field that is valid UTF-8 comes back exactly. A known limit of the reader: a field
  with bytes that are not UTF-8 is written with its bytes but read back as
  b.decode("utf-8", "replace") (read_text decodes so); Q1 asserts exactly that. No writer is left
  for such a field: the profile leaves a cgroup folder whose name is no unit name alone (O5,
  escaped_unit), and the names of Docker and Steam scopes come from systemd. A reader that reads
  game.state byte for byte would flip this assertion.
- Q2, an older bash copy reads ours: bash's eval of each line of that game.state gives the
  original bytes of every field, exactly (bytes that are not UTF-8 included).
- Q3, the line is bash's own: record_line of the fields equals bash's `printf %q` of the same
  bytes, blank-separated, with its newline. printf_q's known limit (its docstring) is a code point
  one of the two Unicode versions (Python's, glibc's) has not assigned: an example that differs
  and holds such a code point (unicodedata category Cn) is assumed away; any other difference
  fails.
- Q4, we read what bash wrote: records() of bash's `printf %q` line (the files of an older copy
  after an upgrade) gives the fields as decode("utf-8", "replace").
- Q5, a record cut short: whole records plus a prefix of one more line (no newline, cut at any
  byte): records() leaves the prefix out, unreadable_lines names its line and only it, and the next
  append_state removes it first, so the file is the whole records plus the new one, byte for byte.
- Q6, game.intended: its format line and the drift records GameProfile.setp writes for a fixed
  unit, with systemd showing a generated value right after the set (valid Unicode: systemctl's
  output reaches the profile decoded with U+FFFD), read with intended_records: the six fields
  exactly, the last record of each setting (setp writes the predicted one before the set, and a
  second when systemd shows another form; an empty answer, a show that failed, adds none, so the
  predicted one stays last), the format line no record, every line readable,
  newer_intended None.
"""

from collections.abc import Generator, Sequence
import contextlib
import io
import shutil
import subprocess
import tempfile
from typing import Final, override
import unicodedata
import unittest

from hypothesis import assume, given
from hypothesis import strategies as st

from cpu.game.adapters.profile import GameProfile
from cpu.game.domain import (
    GameRecordKind,
    Manager,
    intended_header,
    intended_records,
    newer_intended,
    record_line,
    records,
    unreadable_lines,
)
from cpu.game.domain.boxes import shown_form
from cpu.tests.fakes import UID, FakeSystemd, game_scratch
from tests.mbt import profiles

profiles.load()

# bash's environment: a UTF-8 locale (printf %q quotes by it), nothing from the developer's.
BASH_ENV: Final = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
BASH_TIMEOUT_S: Final = 30
# Each line of the file on stdin, as an older copy reads it: its field count, then its fields, each
# ending in a NUL (a field holds none).
BASH_EVAL: Final = 'while IFS= read -r line; do eval "rec=(${line})"; printf "%s\\0" "${#rec[@]}" "${rec[@]}"; done'
# The arguments as one line of printf %q fields, blank-separated, with its newline.
BASH_PRINTF_Q: Final = 'printf "%q" "$1"; shift; for a; do printf " %q" "$a"; done; printf "\\n"'
# Fixed units the profile sets, of either manager (setp writes game.intended only for them).
FIXED_UNITS: Final = ("machine.slice", "user.slice", "app.slice", "builds.slice", "flatpak-session-helper.service")

# The characters printf %q treats on their own (a backslash before them, or $'...'), drawn more often
# than any byte would be.
SHELL_CHARS: Final = " \t\n!\"$&'()*,;<>?[\\]^`{|}#~=:-"
# One chunk of a field: one of SHELL_CHARS, one byte (any but NUL, a high byte alone too) or the UTF-8
# of a character.
_CHUNK: Final = st.one_of(
    st.sampled_from(SHELL_CHARS).map(str.encode),
    st.integers(1, 255).map(lambda b: bytes([b])),
    st.characters(exclude_categories=("Cs",), exclude_characters="\0").map(lambda c: c.encode()),
)
FIELD_BYTES: Final = st.lists(_CHUNK, max_size=6).map(b"".join)


def as_field(data: bytes) -> str:
    """The field the profile holds for these bytes (glob's and the file's surrogateescape)."""
    return data.decode("utf-8", "surrogateescape")


def as_read(data: bytes) -> str:
    """What the profile's reader gives for these bytes (read_text decodes with replace)."""
    return data.decode("utf-8", "replace")


def line_bytes(fields: Sequence[bytes]) -> bytes:
    """The bytes of the record_line the profile writes for these fields."""
    return record_line(*(as_field(f) for f in fields)).encode("utf-8", "surrogateescape")


@st.composite
def state_record(draw: st.DrawFn) -> tuple[bytes, ...]:
    """One record of game.state that 'off' can read (state_undo): a prop record of six fields, or a
    cgw, epp or sysfs record of three; every field after the kind generated."""
    # user and reload: game.leftovers' only (by their words: the code before reload entries has no RELOAD)
    kind = draw(st.sampled_from([k for k in GameRecordKind if k.value not in {"user", "reload"}]))
    count = 5 if kind == GameRecordKind.PROP else 2
    return (kind.value.encode(), *draw(st.lists(FIELD_BYTES, min_size=count, max_size=count)))


STATE_RECORDS: Final = st.lists(state_record(), min_size=1, max_size=5)


def run_bash(script: str, args: Sequence[bytes], *, stdin_path: str | None = None) -> bytes:
    """bash -c script with args, its stdout (stdin: the file, or nothing); fails on a non-zero exit."""
    argv = ["bash", "-c", script, "bash", *args]
    with open(stdin_path, "rb") if stdin_path is not None else contextlib.nullcontext(subprocess.DEVNULL) as stdin:
        res = subprocess.run(argv, capture_output=True, stdin=stdin, env=BASH_ENV, timeout=BASH_TIMEOUT_S, check=False)
    assert res.returncode == 0, f"bash exit {res.returncode}: {res.stderr!r}"
    return res.stdout


def bash_records(out: bytes) -> list[tuple[bytes, ...]]:
    """BASH_EVAL's output as records of bytes."""
    parts = out.split(b"\0")
    assert parts[-1] == b"", f"bash's output does not end in a NUL: {out!r}"
    parts.pop()
    recs: list[tuple[bytes, ...]] = []
    while parts:
        n = int(parts.pop(0))
        recs.append(tuple(parts[:n]))
        del parts[:n]
    return recs


class ShownSystemd(FakeSystemd):
    """The unit tests' fake systemctl, whose `show` gives one value for every property."""

    def __init__(self, root: str, shown_value: str) -> None:
        super().__init__(root)
        self.shown_value = shown_value

    @override
    def now(self, mgr: str, unit: str, prop: str) -> str:
        return self.shown_value


class GameStateFilesTest(profiles.MbtTestCase):
    @contextlib.contextmanager
    def _profile(self, shown_value: str | None = None) -> Generator[GameProfile]:
        """A profile over a scratch folder of this example, removed after it; with shown_value, systemd
        shows it for every property."""
        tmp = tempfile.mkdtemp(prefix="cst-mbt-game-")
        try:
            scratch = game_scratch(tmp)
            runner = scratch.systemd if shown_value is None else ShownSystemd(tmp, shown_value)
            out = io.StringIO()
            yield GameProfile(scratch.paths, scratch.settings, runner, uid_of=lambda _u: UID, out=out)
        finally:
            shutil.rmtree(tmp)

    def _append_all(self, profile: GameProfile, recs: Sequence[tuple[bytes, ...]]) -> None:
        for rec in recs:
            self.assertTrue(profile.append_state(*(as_field(f) for f in rec)))

    @given(recs=STATE_RECORDS)
    def test_q1_our_reader_reads_what_our_writer_wrote(self, recs: list[tuple[bytes, ...]]) -> None:
        with self._profile() as profile:
            self._append_all(profile, recs)
            text = profile.state_text()
            # Valid UTF-8 exactly; other bytes as "replace" decodes them (the reader's limit, the docstring).
            self.assertEqual(records(text), [tuple(as_read(f) for f in rec) for rec in recs])
            self.assertEqual(unreadable_lines(text, intended=False), [])

    @given(recs=STATE_RECORDS)
    def test_q2_an_older_bash_copy_reads_our_bytes(self, recs: list[tuple[bytes, ...]]) -> None:
        with self._profile() as profile:
            self._append_all(profile, recs)
            self.assertEqual(bash_records(run_bash(BASH_EVAL, [], stdin_path=profile.paths.state)), recs)

    @given(fields=st.lists(FIELD_BYTES, min_size=1, max_size=6))
    def test_q3_our_line_is_bashs_printf_q(self, fields: list[bytes]) -> None:
        ours = line_bytes(fields)
        theirs = run_bash(BASH_PRINTF_Q, fields)
        if ours != theirs:
            # printf_q's known limit: a code point Python's or glibc's Unicode has not assigned.
            assume(not any(unicodedata.category(ch) == "Cn" for f in fields for ch in as_read(f)))
        self.assertEqual(ours, theirs)

    @given(fields=st.lists(FIELD_BYTES, min_size=1, max_size=6))
    def test_q4_we_read_the_line_bash_wrote(self, fields: list[bytes]) -> None:
        line = run_bash(BASH_PRINTF_Q, fields)
        self.assertEqual(records(as_read(line)), [tuple(as_read(f) for f in fields)])

    @given(whole=st.lists(state_record(), max_size=4), cut=state_record(), new=state_record(), data=st.data())
    def test_q5_a_record_cut_short_is_left_out_and_removed_before_the_next(
        self,
        whole: list[tuple[bytes, ...]],
        cut: tuple[bytes, ...],
        new: tuple[bytes, ...],
        data: st.DataObject,
    ) -> None:
        with self._profile() as profile:
            encode = [line_bytes(rec) for rec in whole]
            cut_line = line_bytes(cut)
            prefix = cut_line[: data.draw(st.integers(1, len(cut_line) - 1), label="cut at")]
            with open(profile.paths.state, "wb") as fh:
                fh.write(b"".join(encode) + prefix)
            text = profile.state_text()
            self.assertEqual(records(text), [tuple(as_read(f) for f in rec) for rec in whole])
            self.assertEqual([n for n, _ in unreadable_lines(text, intended=False)], [len(whole) + 1])
            self.assertTrue(profile.append_state(*(as_field(f) for f in new)))
            with open(profile.paths.state, "rb") as fh:
                after = fh.read()
            self.assertEqual(after, b"".join(encode) + line_bytes(new))

    @given(
        settings=st.lists(
            st.tuples(
                st.sampled_from(("sys", "usr")),
                st.sampled_from(FIXED_UNITS),
                st.sampled_from(("CPUWeight", "AllowedCPUs", "MemoryLow", "IOWeight")),
                st.text(st.characters(exclude_categories=("Cs",), exclude_characters="\0"), max_size=8),
            ),
            min_size=1,
            max_size=4,
        ),
        shown_value=st.text(st.characters(exclude_categories=("Cs",), exclude_characters="\0\n"), max_size=8),
    )
    def test_q6_game_intended_reads_back_the_six_fields(
        self, settings: list[tuple[Manager, str, str, str]], shown_value: str
    ) -> None:
        # What `systemctl show -P` prints, as the profile reads it (shown(): one line, [not set] none).
        assume(shown_value == shown_value.strip() and shown_value != "[not set]")
        with self._profile(shown_value) as profile:
            with open(profile.paths.intended, "w", encoding="utf-8") as fh:
                fh.write(intended_header())
            for mgr, unit, prop, value in settings:
                profile.setp(mgr, unit, f"{prop}={value}")
            with open(profile.paths.intended, encoding="utf-8") as fh:
                text = fh.read()
            last: dict[tuple[str, str, str], tuple[str, ...]] = {}
            for m, u, p, v in settings:  # the last record of a setting wins, at its own place
                last.pop((m, u, p), None)
                last[m, u, p] = (GameRecordKind.PROP.value, m, u, p, v, shown_value or shown_form(p, v))
            self.assertEqual(intended_records(text), list(last.values()))
            self.assertEqual(unreadable_lines(text, intended=True), [])
            self.assertIsNone(newer_intended(profile.paths.intended, text))


if __name__ == "__main__":
    unittest.main()
