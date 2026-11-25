# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the cst_core Shared Kernel and base adapters."""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import errno
import fcntl
import io
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Final, Literal, override
import unittest
from unittest import mock

from cst_core.adapters import SystemClockAdapter, SystemctlAdapter
from cst_core.adapters.runner import HostExecutionContext, can_spawn_on_host, on_host_argv, SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.dispatch import LocalCstDispatchAdapter
from cst_core.adapters import files as core_files
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.runtime import beside_program, lc_ctype_handover, restore_lc_ctype, undo_python_start
from cst_core import wiring as core_wiring
from cst_core.domain.contracts import StateFormatKey, first_line_version, state_format, version_of
from cst_core.application.user_units import (
    kept_unit_copies,
    manager_unit_path,
    older_unit_copies,
)
from cst_core.domain.install import INSTALL_MANIFEST, Component, Owner
from cst_core.domain.record import InstallRecord, RecordedFile, RecordFormatError
from cst_core.domain.ostree import OstreeStatus, query_status
from cst_core.application.dispatch import CstDispatcher, ModuleRouter, RouteSettings
from cst_core.domain.dispatch import (
    MODULES,
    ROUTING,
    Privilege,
    RouteAction,
    Routed,
    format_cst_usage,
    route_command,
    script_help,
)
from cst_core.domain.files import MarkedLineSpec, effective_lines, inspect_managed_file
from cst_core.domain.plan import PlanItem
from cst_core.domain.text import split_ascii_blanks, strip_ascii_blanks
from cst_core.domain.values import (
    ASCII_BLANKS,
    ByteSize,
    CpuSet,
    DomainValidationError,
    Duration,
    OctalMode,
    format_bytes_decimal,
    format_bytes_gib_mib,
    format_bytes_human,
    format_cpulist,
    parse_integer,
    parse_whole_number,
)
from cst_core.domain.units import (
    ActiveState,
    SYSTEMD_WHITESPACE,
    UNIT_PATH_PROP,
    named_user_unit_dir,
    show_manager_value_argv,
    systemd_lines,
    user_manager_head,
    user_unit_dir,
)
from cst_core.ports import (
    CommandFailed,
    CommandFault,
    CommandResult,
    OutputMode,
    ReplacedNotSynced,
    SystemctlHost,
    UnitName,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_PYTHON = "/usr/bin/python3"
_CLEAN_ENV: Mapping[str, str] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


class FakeRunner:
    """In-memory CommandRunnerPort recording invocations for SystemctlAdapter tests."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.options: list[dict[str, object]] = []
        self.responses = responses or {}

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        **_options: object,
    ) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        self.options.append({"timeout": timeout, **_options})
        res = self.responses.get(tuple(cmd), CommandResult(0, "", ""))
        if check and res.returncode != 0:
            raise RuntimeError(f"command failed: {cmd}")
        return res


class AsciiBlanksTest(unittest.TestCase):
    def test_only_ascii_blanks_are_trimmed(self) -> None:
        # Space, tab, newline, carriage return, form feed and vertical tab; not the Unicode blanks
        # str.strip() also removes (no-break space, ideographic space, NEL) nor the separators
        # U+001C-U+001F, which bash and systemd keep in a word.
        self.assertEqual(ASCII_BLANKS, " \t\n\r\f\v")
        self.assertEqual(strip_ascii_blanks("  off\t\r\n"), "off")
        self.assertEqual(strip_ascii_blanks("\f\voff \v"), "off")
        self.assertEqual(strip_ascii_blanks(""), "")
        self.assertEqual(strip_ascii_blanks(" \t "), "")
        for kept in (
            "\N{NO-BREAK SPACE}off",
            "off\N{IDEOGRAPHIC SPACE}",
            "\x1foff",
            "off\x1c",
            "off\x85",
            "\N{LINE SEPARATOR}off",
        ):
            with self.subTest(kept=kept):
                self.assertEqual(strip_ascii_blanks(kept), kept)
                self.assertNotEqual(kept.strip(), kept)  # what str.strip() would have trimmed

    def test_words_split_at_ascii_blanks_only(self) -> None:
        # (text, blanks or None for the default, the words)
        nbsp, ideo = "\N{NO-BREAK SPACE}", "\N{IDEOGRAPHIC SPACE}"
        cases: tuple[tuple[str, str | None, list[str]], ...] = (
            ("", None, []),
            (" \t\n\r\f\v", None, []),
            ("  a \tb\n\nc  ", None, ["a", "b", "c"]),
            ("a\fb\vc\rd", None, ["a", "b", "c", "d"]),
            (f"a{nbsp}b c", None, [f"a{nbsp}b", "c"]),
            (f"a{ideo}b", None, [f"a{ideo}b"]),
            ("a\x1cb\x85c", None, ["a\x1cb\x85c"]),
            ("a b\tc\fd", " \t", ["a", "b", "c\fd"]),
            ("a]b^c", "]^", ["a", "b", "c"]),
        )
        for text, blanks, words in cases:
            with self.subTest(text=text, blanks=blanks):
                got = split_ascii_blanks(text) if blanks is None else split_ascii_blanks(text, blanks)
                self.assertEqual(got, words)
        self.assertEqual(strip_ascii_blanks(" \N{NO-BREAK SPACE}off\t"), "\N{NO-BREAK SPACE}off")


class WriteAtomicReplacedNotSyncedTest(unittest.TestCase):
    """write_atomic's ReplacedNotSynced: raised only when the rename was done and the folder's sync failed."""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-wa-rns-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dest = os.path.join(self.tmp, "fstab")
        with open(self.dest, "wb") as fh:
            fh.write(b"old\n")

    def _failing_fsync(self, kind: Literal["folder", "file"], code: int) -> contextlib.AbstractContextManager[object]:
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode) == (kind == "folder"):
                raise OSError(code, os.strerror(code))
            real_fsync(fd)

        return mock.patch.object(core_files.os, "fsync", fsync)

    def _content(self) -> bytes:
        with open(self.dest, "rb") as fh:
            return fh.read()

    def test_the_signal_is_raised_only_after_the_rename_when_the_folder_sync_fails(self) -> None:
        cases: tuple[
            tuple[str, Literal["folder", "file"], int, bool | Literal["file"], type[OSError] | None, bytes], ...
        ] = (
            ("folder fsync EIO", "folder", errno.EIO, True, ReplacedNotSynced, b"new\n"),
            ("file fsync EIO, before the rename", "file", errno.EIO, True, OSError, b"old\n"),
            ("folder fsync EINVAL, ignored", "folder", errno.EINVAL, True, None, b"new\n"),
            ("folder fsync ENOTSUP, ignored", "folder", errno.ENOTSUP, True, None, b"new\n"),
            ("durable file: no folder sync", "folder", errno.EIO, "file", None, b"new\n"),
            ("durable False: no sync", "folder", errno.EIO, False, None, b"new\n"),
        )
        for name, kind, code, durable, raised, content in cases:
            with self.subTest(name):
                with open(self.dest, "wb") as fh:
                    fh.write(b"old\n")
                with self._failing_fsync(kind, code):
                    if raised is None:
                        core_files.write_atomic(self.dest, "new\n", mode=0o644, durable=durable, relabel=False)
                    else:
                        with self.assertRaises(raised) as caught:
                            core_files.write_atomic(self.dest, "new\n", mode=0o644, durable=durable, relabel=False)
                        err = caught.exception
                        self.assertEqual(err.errno, code)
                        self.assertEqual(isinstance(err, ReplacedNotSynced), raised is ReplacedNotSynced)
                        if isinstance(err, ReplacedNotSynced):
                            self.assertEqual(err.dest, self.dest)
                            self.assertEqual(str(err), str(OSError(code, os.strerror(code))))
                self.assertEqual(self._content(), content)
                self.assertEqual(os.listdir(self.tmp), ["fstab"])

    def test_a_folder_that_cannot_be_opened_after_the_rename_is_the_signal_too(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root opens a folder whatever its mode")
        os.chmod(self.tmp, 0o300)  # a file can still be made and renamed in it, the folder not opened to read
        try:
            with self.assertRaises(ReplacedNotSynced) as caught:
                core_files.write_atomic(self.dest, "new\n", mode=0o644, relabel=False)
        finally:
            os.chmod(self.tmp, 0o755)
        err = caught.exception
        self.assertEqual((err.errno, err.filename), (errno.EACCES, self.tmp))
        self.assertEqual(self._content(), b"new\n")


class ValueObjectsTest(unittest.TestCase):
    def test_byte_size_parse_and_human(self) -> None:
        self.assertEqual(ByteSize.parse("48G"), ByteSize(48 * 2**30))
        self.assertEqual(ByteSize.parse(" 512M "), ByteSize(512 * 2**20))
        self.assertEqual(ByteSize.parse("4K"), ByteSize(4096))
        self.assertEqual(ByteSize.parse("2T"), ByteSize(2 * 2**40))
        self.assertEqual(ByteSize.parse("1024"), ByteSize(1024))
        self.assertIsNone(ByteSize.parse("infinity"))
        self.assertIsNone(ByteSize.parse(None))
        self.assertIsNone(ByteSize.parse(""))

        self.assertEqual(ByteSize(2 * 2**30).human(), "2.0 GiB")
        self.assertEqual(ByteSize.parse_iec("150G"), ByteSize(150 * 2**30))
        self.assertEqual(ByteSize.parse_iec("off"), ByteSize(0))
        self.assertEqual(format_bytes_human(None), "-")
        self.assertEqual(format_bytes_human(512), "512 B")
        self.assertEqual(format_bytes_human(-2048), "-2.0 KiB")
        self.assertEqual(format_bytes_gib_mib(2**30), "1.0 GiB")
        self.assertEqual(format_bytes_gib_mib(512 * 2**20), "512 MiB")
        self.assertEqual(format_bytes_decimal(1500), "1.5 KB")

        with self.assertRaises(DomainValidationError):
            ByteSize(-1)
        with self.assertRaises(DomainValidationError):
            ByteSize.parse_iec("150")

    def test_sizes_and_durations_are_ascii(self) -> None:
        # \d matched every Unicode decimal digit, \s every Unicode blank, and IGNORECASE folded the long s
        # (U+017F) to "s": systemd and bash read ASCII only.
        three = "\N{ARABIC-INDIC DIGIT THREE}"
        for spec in (f"{three}G", "48G\N{NO-BREAK SPACE}", f"1{three}M"):
            with self.subTest(spec=spec):
                self.assertIsNone(ByteSize.parse(spec))
        for spec in (f"{three}G", "1\N{KELVIN SIGN}", "\N{NO-BREAK SPACE}150G", f"0{three}"):
            with self.subTest(spec=spec), self.assertRaisesRegex(DomainValidationError, "is not a size"):
                ByteSize.parse_iec(spec)
        for spec in (f"{three}m", "3\N{LATIN SMALL LETTER LONG S}", "3m\N{NO-BREAK SPACE}"):
            with self.subTest(spec=spec), self.assertRaises(DomainValidationError):
                Duration.parse(spec)
        for spec in (f"{three}m", "3\N{LATIN SMALL LETTER LONG S}", f"1.{three}h"):
            with self.subTest(spec=spec), self.assertRaises(DomainValidationError):
                Duration.parse_float(spec)
            with self.subTest(spec=spec, allow_unitless=True), self.assertRaises(DomainValidationError):
                Duration.parse_float(spec, allow_unitless=True)
        # The ASCII forms, any case and ASCII blanks, are unchanged.
        self.assertEqual(ByteSize.parse(" 48G\t"), ByteSize(48 * 2**30))
        self.assertEqual(ByteSize.parse_iec(" 1.5 GiB "), ByteSize(int(1.5 * 2**30)))
        self.assertEqual(ByteSize.parse_iec("OFF"), ByteSize(0))
        self.assertEqual(Duration.parse_float("3S"), 3.0)
        self.assertEqual(Duration.parse(" 2h "), Duration(7200))

    def test_a_size_is_trimmed_of_ascii_blanks_only(self) -> None:
        # str.strip() also dropped the separators U+001C-U+001F, which are no blanks to bash or systemd
        # (strip_ascii_blanks): "\x1foff" was 0, no cap. ASCII blanks around a size are still dropped.
        for spec in ("\x1foff", "\x1f150G", "150G\x1c", "\x1d0", "\x1enone"):
            with self.subTest(spec=spec), self.assertRaisesRegex(DomainValidationError, "is not a size"):
                ByteSize.parse_iec(spec)
        self.assertEqual(ByteSize.parse_iec("\t\r\f\v150G \n"), ByteSize(150 * 2**30))
        self.assertEqual(ByteSize.parse_iec(" off\v"), ByteSize(0))

    def test_a_whole_number_is_ascii_digits_only(self) -> None:
        for text, want in (("0", 0), ("060", 60), ("300", 300)):
            with self.subTest(text=text):
                self.assertEqual(parse_whole_number(text), want)
        for bad in (
            "",
            "\N{SUPERSCRIPT TWO}",
            "\N{ARABIC-INDIC DIGIT THREE}\N{ARABIC-INDIC DIGIT ZERO}\N{ARABIC-INDIC DIGIT ZERO}",
            " 5",
            "5 ",
            "+5",
            "-1",
            "1_0",
            "5\n",
            "0" * 5000 + "5",
        ):
            with self.subTest(bad=bad[:12]):
                self.assertIsNone(parse_whole_number(bad))

    def test_an_integer_is_one_leading_minus_and_ascii_digits(self) -> None:
        for text, want in (("0", 0), ("-0", 0), ("060", 60), ("-7", -7), ("-060", -60), ("9" * 4300, int("9" * 4300))):
            with self.subTest(text=text[:12]):
                self.assertEqual(parse_integer(text), want)
        for digits in (
            "",
            "\N{SUPERSCRIPT TWO}",
            "\N{ARABIC-INDIC DIGIT THREE}",
            " 5",
            "5 ",
            "+5",
            "--5",
            "1_0",
            "5\n",
            "9" * 5000,
        ):
            for bad in (digits, "-" + digits):
                with self.subTest(bad=bad[:12]):
                    self.assertIsNone(parse_integer(bad))

    def test_an_integer_with_plus_and_bounds_is_read_as_the_kernel_reads_it(self) -> None:
        # (text, plus, bounds, the integer or None)
        cases = (
            ("+5", True, None, 5),
            ("+0", True, None, 0),
            ("-5", True, None, -5),
            ("+-5", True, None, None),
            ("++5", True, None, None),
            ("-+5", True, None, None),
            ("+", True, None, None),
            ("+ 5", True, None, None),
            ("+5", False, None, None),
            ("7", False, (0, 7), 7),
            ("8", False, (0, 7), None),
            ("-1", False, (0, 7), None),
            ("-8", True, (-8, 7), -8),
            ("-9", True, (-8, 7), None),
            ("+7", True, (-8, 7), 7),
            ("+8", True, (-8, 7), None),
            ("x", True, (-8, 7), None),
        )
        for text, plus, bounds, want in cases:
            with self.subTest(text=text, plus=plus, bounds=bounds):
                self.assertEqual(parse_integer(text, plus=plus, bounds=bounds), want)

    def test_duration_parse(self) -> None:
        self.assertEqual(Duration.parse("30s"), Duration(30))
        self.assertEqual(Duration.parse("15m"), Duration(900))
        self.assertEqual(Duration.parse("48h"), Duration(48 * 3600))
        self.assertEqual(Duration.parse("7d"), Duration(7 * 86400))
        self.assertEqual(Duration.parse("2w"), Duration(2 * 604800))
        self.assertEqual(Duration.parse_float("120", allow_unitless=True, positive_only=True), 120.0)
        self.assertEqual(Duration.parse_float("1.5h"), 5400.0)
        for bad in ("150", "", "5x", "-1s"):
            with self.subTest(bad=bad), self.assertRaises(DomainValidationError):
                Duration.parse(bad)
        with self.assertRaises(DomainValidationError):
            Duration(-5)

    def test_sizes_too_large_to_hold_are_the_domain_error(self) -> None:
        # float('9' * 400) is inf and '9' * 308 is finite until multiplied by T: int() of either
        # raised OverflowError; int() of more than 4300 digits raised a plain ValueError.
        for spec in ("9" * 400 + "G", "9" * 308 + "T", "9" * 5000 + "K"):
            with self.subTest(spec=spec[-4:]), self.assertRaisesRegex(DomainValidationError, "too large for a size"):
                ByteSize.parse_iec(spec)
        self.assertIsNone(ByteSize.parse("9" * 5000))
        self.assertIsNone(ByteSize.parse("9" * 5000 + "G"))
        # Huge but finite values keep their value.
        self.assertEqual(ByteSize.parse_iec("8" + "0" * 290 + "T"), ByteSize(int(8e290 * 2**40)))

    def test_a_systemd_size_above_64_bits_is_invalid(self) -> None:
        # systemd's sizes are 64-bit unsigned: one above 2**64 - 1 is invalid (None), before or
        # after its unit. A value that big once came back whole, and the float formats raised
        # OverflowError on it (format_bytes_human of 10**400 - 1).
        self.assertEqual(ByteSize.parse(str(2**64 - 1)), ByteSize(2**64 - 1))
        self.assertEqual(ByteSize.parse("16777215T"), ByteSize(16777215 * 2**40))
        for spec in (str(2**64), "16777216T", "18014398509481984K", "9" * 400, "9" * 400 + "K"):
            with self.subTest(spec=spec[-6:]):
                self.assertIsNone(ByteSize.parse(spec))

    def test_durations_too_large_to_hold_are_the_domain_error(self) -> None:
        with self.assertRaisesRegex(DomainValidationError, "invalid duration"):
            Duration.parse("9" * 5000 + "s")
        for spec in ("9" * 400 + "s", "9" * 5000 + "w", "9" * 307 + "w"):
            with self.subTest(spec=spec[-4:]), self.assertRaisesRegex(DomainValidationError, "duration too large"):
                Duration.parse_float(spec)
        with self.assertRaisesRegex(DomainValidationError, "duration too large"):
            Duration.parse_float("9" * 400, allow_unitless=True, positive_only=True)
        # Huge but finite values keep their value.
        self.assertEqual(Duration.parse("9" * 400 + "s"), Duration(int("9" * 400)))
        self.assertEqual(Duration.parse_float("1" + "0" * 300 + "s"), 1e300)

    def test_cpu_set_parse(self) -> None:
        cs = CpuSet.parse("0-3,6,12-13")
        self.assertEqual(cs.cpus, frozenset({0, 1, 2, 3, 6, 12, 13}))
        self.assertEqual(cs.sorted_cpus(), (0, 1, 2, 3, 6, 12, 13))
        # Digits other than ASCII 0-9 ("²" passes str.isdigit, int() refuses it): an error of the
        # parser, not a ValueError from int().
        for bad in ("", " , ", "3-1", "a-b", "0-1-2", "cpu0", "²", "0-²", "٣"):
            with self.subTest(bad=bad), self.assertRaises(DomainValidationError):
                CpuSet.parse(bad)
        # ASCII blanks around the list and an element are dropped; a no-break space or an ideographic
        # space is an invalid element, as systemd's AllowedCPUs= reads it (str.strip() dropped them).
        self.assertEqual(CpuSet.parse(" 0-3, 6\n").cpus, frozenset({0, 1, 2, 3, 6}))
        for bad in ("12-23\N{NO-BREAK SPACE}", "0-3,\N{IDEOGRAPHIC SPACE}6", "\N{NO-BREAK SPACE}1", "1\x1c"):
            with self.subTest(blank=bad), self.assertRaises(DomainValidationError):
                CpuSet.parse(bad)
        # An index past int()'s digit limit (4300 digits) is an invalid index or range too, not the
        # plain ValueError int() raises: callers that catch only DomainValidationError (cpu irq's
        # BUILD_CPUS, cpu power's topology) would see a traceback.
        for huge in ("1" * 5000, "0-" + "1" * 5000, "1" * 5000 + "-1"):
            with self.subTest(huge=huge[:12]), self.assertRaises(DomainValidationError) as cm:
                CpuSet.parse(huge)
            self.assertIs(type(cm.exception), DomainValidationError)
        with self.assertRaises(DomainValidationError):
            CpuSet(frozenset())
        with self.assertRaises(DomainValidationError):
            CpuSet(frozenset({-1}))

    def test_cpu_set_refuses_an_index_past_the_cpu_limit(self) -> None:
        # systemd's CPU_SET_MAX_NCPU: 8191 is the last index a CPU list may hold.
        self.assertEqual(CpuSet.parse("8191").cpus, frozenset({8191}))
        self.assertEqual(len(CpuSet.parse("0-8191").cpus), 8192)
        for bad, why in (
            ("8192", "invalid CPU index '8192'"),
            ("0-8192", "beyond the CPU limit 8191"),
            ("8192-8193", "beyond the CPU limit 8191"),
            ("1,9999", "invalid CPU index '9999'"),
        ):
            with self.subTest(bad), self.assertRaises(DomainValidationError) as cm:
                CpuSet.parse(bad)
            self.assertIn(why, str(cm.exception))
        # Refused before the range is built (main allocated a set of 10**11 CPUs until memory ran out).
        started = time.monotonic()
        with self.assertRaises(DomainValidationError):
            CpuSet.parse("0-99999999999")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_format_cpulist(self) -> None:
        self.assertEqual(format_cpulist([0, 1, 2, 5, 8, 9, 10]), "0-2,5,8-10")
        # Any order, a CPU named twice, none at all (cpu setup's split can be empty).
        self.assertEqual(format_cpulist([13, 12, 6, 6, 0]), "0,6,12-13")
        self.assertEqual(format_cpulist([]), "")
        # What CpuSet.parse reads back as the same set.
        cs = CpuSet.parse("0-3,6,12-13")
        self.assertEqual(CpuSet.parse(format_cpulist(cs.cpus)), cs)

    def test_octal_mode_parse(self) -> None:
        self.assertEqual(OctalMode.parse("0600"), OctalMode(0o600))
        self.assertEqual(OctalMode.parse("644"), OctalMode(0o644))
        self.assertEqual(OctalMode.parse(0o755), OctalMode(0o755))
        for bad in ("999", "064444", "abc", -1, 0o10000, True):
            with self.subTest(bad=bad), self.assertRaises(DomainValidationError):
                OctalMode.parse(bad)


class ActiveStateTest(unittest.TestCase):
    """ActiveState holds systemd's ActiveState words, which systemctl's text is compared with."""

    def test_it_holds_systemds_eight_words(self) -> None:
        # systemd 262, unit_active_state_to_string.
        words = {"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance", "refreshing"}
        self.assertEqual({state.value for state in ActiveState}, words)

    def test_plain_text_is_found_in_a_set_of_members(self) -> None:
        # The modules keep systemctl's answer a str and look it up in a frozenset of members.
        running = frozenset({ActiveState.ACTIVE, ActiveState.RELOADING})
        self.assertIn("active", running)
        self.assertIn("reloading", running)
        self.assertNotIn("inactive", running)
        self.assertNotIn("", running)
        self.assertNotIn("a-word-a-newer-systemd-adds", running)
        self.assertEqual(str(ActiveState.MAINTENANCE), "maintenance")


class SystemdLinesTest(unittest.TestCase):
    """systemd_lines and SYSTEMD_WHITESPACE: a unit file's lines and blanks as systemd's read_line and
    strstrip see them (src/basic/fileio.c, src/fundamental/string-util.h)."""

    def test_lines_end_where_read_line_ends_them(self) -> None:
        cases = (
            ("", [""]),
            ("a\nb\n", ["a", "b", ""]),
            ("a\rb", ["a", "b"]),
            ("a\0b", ["a", "b"]),
            ("a\r\nb", ["a", "b"]),
            ("a\n\rb", ["a", "b"]),
            ("a\r\n\0b", ["a", "b"]),
            ("a\n\n\rb", ["a", "", "b"]),
            ("a\vb\fc\x1cd\x85e\u2028f\u2029g", ["a\vb\fc\x1cd\x85e\u2028f\u2029g"]),
        )
        for text, lines in cases:
            with self.subTest(text=text):
                self.assertEqual(systemd_lines(text), lines)

    def test_the_whitespace_is_space_tab_newline_and_carriage_return(self) -> None:
        self.assertEqual(SYSTEMD_WHITESPACE, " \t\n\r")
        self.assertEqual(" \t\fx\v\xa0\r\n".strip(SYSTEMD_WHITESPACE), "\fx\v\xa0")


class UserUnitDirTest(unittest.TestCase):
    """user_unit_dir: the folder a user manager reads the user's own units from, by its UnitPath."""

    # systemd 262's answer for a user manager without XDG_CONFIG_HOME (a workstation's, the home renamed).
    SYSTEMD_262: Final = (
        "/var/home/u/.config/systemd/user.control /run/user/1000/systemd/user.control "
        "/run/user/1000/systemd/transient /run/user/1000/systemd/generator.early /var/home/u/.config/systemd/user "
        "/etc/xdg/systemd/user /etc/systemd/user /run/user/1000/systemd/user /run/systemd/user "
        "/run/user/1000/systemd/generator /var/home/u/.local/share/systemd/user "
        "/var/home/u/.local/share/flatpak/exports/share/systemd/user /var/lib/flatpak/exports/share/systemd/user "
        "/usr/local/share/systemd/user /usr/share/systemd/user /usr/local/lib/systemd/user /usr/lib/systemd/user "
        "/run/user/1000/systemd/generator.late\n"
    )

    def test_the_first_folder_of_its_own(self) -> None:
        self.assertEqual(user_unit_dir(self.SYSTEMD_262, "/var/home/u"), "/var/home/u/.config/systemd/user")
        # The manager's own XDG_CONFIG_HOME comes first (systemd's persistent config folder).
        xdg = self.SYSTEMD_262.replace("/var/home/u/.config/systemd/user ", "/var/home/u/xdg/systemd/user ")
        self.assertEqual(user_unit_dir(xdg, "/var/home/u"), "/var/home/u/xdg/systemd/user")
        self.assertEqual(
            user_unit_dir("/srv/u/cfg//systemd/user /etc/systemd/user", "/home/u"), "/srv/u/cfg/systemd/user"
        )

    def test_systemds_default_without_a_safe_answer(self) -> None:
        default = "/home/u/.config/systemd/user"
        for answer in (None, "", "\n", "/etc/systemd/user /usr/lib/systemd/user", "/run/user/1/systemd/user"):
            with self.subTest(answer=answer):
                self.assertEqual(user_unit_dir(answer, "/home/u"), default)
        # A folder with a space: systemd does not quote it, the split gives a relative piece.
        spaced = "/home/u/my cfg/systemd/user /etc/systemd/user"
        self.assertEqual(user_unit_dir(spaced, "/home/u"), default)
        # A piece of a split that is still absolute lands on a system folder: not the user's.
        self.assertEqual(user_unit_dir("/home/u/x /etc/systemd/user", "/home/u"), default)

    def test_the_folder_named_or_none(self) -> None:
        # named_user_unit_dir: the folder only when the answer names it; None where user_unit_dir guesses
        # systemd's default (also for an answer that names ~/.config/systemd/user, it is named).
        self.assertEqual(named_user_unit_dir(self.SYSTEMD_262), "/var/home/u/.config/systemd/user")
        self.assertEqual(named_user_unit_dir("/srv/u/cfg//systemd/user /etc/systemd/user"), "/srv/u/cfg/systemd/user")
        for answer in (
            None,
            "",
            "\n",
            "/etc/systemd/user /usr/lib/systemd/user",
            "/run/user/1/systemd/user",
            "/home/u/my cfg/systemd/user /etc/systemd/user",
            "/home/u/x /etc/systemd/user",
        ):
            with self.subTest(answer=answer):
                self.assertIsNone(named_user_unit_dir(answer))


class _Records:
    """An InstallRecordPort that serves one record for the user's record path (or raises)."""

    def __init__(self, record: InstallRecord | None, error: Exception | None = None) -> None:
        self.record = record
        self.error = error
        self.paths: list[str] = []

    def read(self, path: str) -> InstallRecord | None:
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return self.record

    def update(self, path: str, change: Callable[[InstallRecord], InstallRecord]) -> bool:
        raise AssertionError(f"no update expected: {path} {change}")

    def sha256(self, data: bytes) -> str:
        raise AssertionError(f"no sha256 expected: {len(data)} bytes")


class UserUnitCopiesTest(unittest.TestCase):
    """cst_core.application.user_units: the user manager's UnitPath and the recorded copies elsewhere."""

    HOME: Final = "/home/u"
    NAME: Final = "x.service"
    CURRENT: Final = "/home/u/xdg/systemd/user/x.service"
    OLD: Final = "/home/u/.config/systemd/user/x.service"
    NAMED: Final = "/home/u/xdg/systemd/user /etc/systemd/user /usr/lib/systemd/user"

    def _record(self, *dests: tuple[str, Component]) -> InstallRecord:
        def recorded(component: Component) -> RecordedFile:
            return RecordedFile(None, component, "text", "0" * 64, 0o644, Owner.USER)

        return InstallRecord({dest: recorded(component) for dest, component in dests}, {})

    def test_manager_unit_path_is_the_answer_or_none(self) -> None:
        class Answer:
            def __init__(self, result: CommandResult) -> None:
                self.result = result

            def run(self, argv: Sequence[str], *, check: bool = False, **options: object) -> CommandResult:
                return self.result

        named = SystemctlAdapter(Answer(CommandResult(0, self.NAMED, "")))
        self.assertEqual(manager_unit_path(named), self.NAMED)
        failing = SystemctlAdapter(Answer(CommandResult(1, "", "Failed to connect to bus")))
        self.assertIsNone(manager_unit_path(failing))

    def test_older_copies_are_the_recorded_ones_elsewhere_when_the_manager_names_its_folder(self) -> None:
        rec = self._record(
            (self.OLD, Component.CPU_TOOLBOX),
            (self.CURRENT, Component.CPU_TOOLBOX),
            ("/home/u/.config/systemd/user/y.service", Component.CPU_TOOLBOX),  # another name
            ("/home/u/old/x.service", Component.TOOLCHAINS_AUTO),  # another component
        )
        records = _Records(rec)
        found = older_unit_copies(records, self.HOME, Component.CPU_TOOLBOX, self.NAME, self.CURRENT, self.NAMED)
        self.assertEqual(found, (self.OLD,))
        self.assertEqual(records.paths, ["/home/u/.local/state/cst/installed.json"])
        # No answer, or an answer that names no folder of the user's own: every recorded copy is kept.
        for answer in (None, "/etc/systemd/user /usr/lib/systemd/user"):
            with self.subTest(answer=answer):
                self.assertEqual(
                    older_unit_copies(records, self.HOME, Component.CPU_TOOLBOX, self.NAME, self.CURRENT, answer), ()
                )

    def test_a_record_that_cannot_be_read_lists_no_copy(self) -> None:
        for records in (
            _Records(None),
            _Records(None, OSError(5, "Input/output error")),
            _Records(None, RecordFormatError("not a record")),
            _Records(None, ValueError("bad")),
        ):
            with self.subTest(error=records.error):
                self.assertEqual(
                    older_unit_copies(records, self.HOME, Component.CPU_TOOLBOX, self.NAME, self.CURRENT, self.NAMED),
                    (),
                )

    def test_kept_copies_are_those_on_disk_and_not_current_when_the_manager_names_no_folder(self) -> None:
        other = "/home/u/other/x.service"
        link = "/home/u/link/x.service"
        rec = self._record(
            (self.OLD, Component.CPU_TOOLBOX), (other, Component.CPU_TOOLBOX), (link, Component.CPU_TOOLBOX)
        )
        on_disk = {self.OLD, link}
        real = {link: self.CURRENT}

        def kept(answer: str | None) -> tuple[str, ...]:
            return kept_unit_copies(
                on_disk.__contains__,
                lambda p: real.get(p, p),
                _Records(rec),
                self.HOME,
                Component.CPU_TOOLBOX,
                self.NAME,
                self.CURRENT,
                answer,
            )

        self.assertEqual(kept(None), (self.OLD,))
        self.assertEqual(kept(self.NAMED), ())


class ManagedFilesTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.spec = MarkedLineSpec(
            desired_line="/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: swap-setup.sh (CST)",
            key="/dev/mapper/swap0",
            current_marker="# managed-by: swap-setup.sh (CST)",
            legacy_markers=("# managed-by: apply-swap-config.sh", "# managed-by: apply-zram-config.sh"),
        )

    def test_marked_line_states(self) -> None:
        old = "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: apply-swap-config.sh"
        older = "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: apply-zram-config.sh"
        changed = "/dev/mapper/swap0 none swap defaults,nofail,pri=5 0 0 # managed-by: apply-swap-config.sh"

        self.assertEqual(self.spec.inspect(None, exists=False, readable=True), "missing")
        self.assertEqual(self.spec.inspect(None, exists=True, readable=False), "unreadable")
        self.assertEqual(self.spec.inspect("UUID=x / btrfs defaults 0 0\n", exists=True, readable=True), "missing")
        self.assertEqual(self.spec.inspect(self.spec.desired_line + "\n", exists=True, readable=True), "ok")
        self.assertEqual(self.spec.inspect(old + "\n", exists=True, readable=True), "adopt")
        self.assertEqual(self.spec.inspect(older + "\n", exists=True, readable=True), "adopt")
        self.assertEqual(
            self.spec.inspect(f"{self.spec.desired_line}\n{old}\n", exists=True, readable=True), "duplicate"
        )
        self.assertEqual(self.spec.inspect(f"{old}\n{changed}\n", exists=True, readable=True), "change")
        self.assertEqual(self.spec.inspect(changed + "\n", exists=True, readable=True), "change")
        self.assertEqual(
            self.spec.inspect("/dev/mapper/swap0 none swap defaults 0 0\n", exists=True, readable=True), "conflict"
        )

    def test_render_updated_replaces_in_place_and_dedupes(self) -> None:
        old = "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: apply-swap-config.sh"
        existing = f"# head\n{old}\nUUID=x / btrfs defaults 0 0\n{old}\n# tail\n"
        updated = self.spec.render_updated(existing)
        self.assertEqual(
            updated,
            f"# head\n{self.spec.desired_line}\nUUID=x / btrfs defaults 0 0\n# tail\n",
        )
        appended = self.spec.render_updated("UUID=x / btrfs defaults 0 0\n")
        self.assertEqual(appended, f"UUID=x / btrfs defaults 0 0\n{self.spec.desired_line}\n")

    def test_inspect_managed_file(self) -> None:
        desired = "# new comment\nvm.swappiness = 100\n\nvm.page-cluster = 0\n"
        adopted = "# old comment\n\nvm.swappiness = 100\nvm.page-cluster = 0\n"
        changed = "# old comment\nvm.swappiness = 60\nvm.page-cluster = 0\n"

        self.assertEqual(effective_lines(desired), ["vm.swappiness = 100", "vm.page-cluster = 0"])
        self.assertEqual(
            inspect_managed_file(desired, desired, exists=True, is_symlink=False, readable=True),
            "ok",
        )
        self.assertEqual(
            inspect_managed_file(desired, adopted, exists=True, is_symlink=False, readable=True),
            "adopt",
        )
        self.assertEqual(
            inspect_managed_file(desired, changed, exists=True, is_symlink=False, readable=True),
            "change",
        )
        self.assertEqual(
            inspect_managed_file(desired, None, exists=False, is_symlink=False, readable=True),
            "missing",
        )
        self.assertEqual(
            inspect_managed_file(desired, None, exists=True, is_symlink=False, readable=False),
            "unreadable",
        )
        self.assertEqual(
            inspect_managed_file(desired, desired, exists=True, is_symlink=True, readable=True),
            "change",
        )


class PlanItemTest(unittest.TestCase):
    def test_plan_item(self) -> None:
        row = PlanItem("ok", "crypttab: swap0 line", "up to date")
        self.assertEqual(row.format_line(), "  ok        crypttab: swap0 line                   up to date")
        self.assertEqual(PlanItem("ok", "zram0").format_line(), "  ok        zram0")


class AdaptersTest(unittest.TestCase):
    def test_virsh_in_a_new_session_replaces_bytes_that_are_not_utf8(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-core-virsh-")
        self.addCleanup(shutil.rmtree, tmp)
        stub = os.path.join(tmp, "virsh")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nprintf "\\377abc LC_ALL=%s" "$LC_ALL"\n')
        os.chmod(stub, 0o755)
        virsh = SubprocessVirshAdapter(ctx=HostExecutionContext(exists=lambda _path: False))
        with mock.patch.dict(os.environ, {"PATH": f"{tmp}:/usr/bin:/bin"}):
            res = virsh.call("domstate", "vm1", start_new_session=True)
        self.assertEqual((res.returncode, res.stdout), (0, "\ufffdabc LC_ALL=C"))
        self.assertEqual(res.argv, ("virsh", "-c", "qemu:///system", "domstate", "vm1"))

    def test_is_searchable_tells_a_folder_this_user_may_enter(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-core-fs-")
        self.addCleanup(shutil.rmtree, tmp)
        fs = LocalFileSystemAdapter()
        open_dir, closed_dir = os.path.join(tmp, "open"), os.path.join(tmp, "closed")
        os.mkdir(open_dir, 0o300)  # searchable, not readable
        os.mkdir(closed_dir, 0o600)  # readable, not searchable
        for d in (open_dir, closed_dir):
            self.addCleanup(os.chmod, d, 0o700)
        self.assertTrue(fs.is_searchable(open_dir))
        # Root may search any folder; any other user may not search one without its x bit.
        self.assertEqual(fs.is_searchable(closed_dir), os.geteuid() == 0)
        self.assertFalse(fs.is_searchable(os.path.join(tmp, "missing")))

    def test_is_char_device_tells_a_character_device_through_a_link(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-core-fs-")
        self.addCleanup(shutil.rmtree, tmp)
        fs = LocalFileSystemAdapter()
        regular, folder, fifo = (os.path.join(tmp, name) for name in ("file", "folder", "fifo"))
        with open(regular, "w", encoding="utf-8") as f:
            f.write("x\n")
        os.mkdir(folder)
        os.mkfifo(fifo)
        to_null, dangling = os.path.join(tmp, "to-null"), os.path.join(tmp, "dangling")
        os.symlink("/dev/null", to_null)
        os.symlink(os.path.join(tmp, "missing"), dangling)
        self.assertTrue(fs.is_char_device("/dev/null"))
        self.assertTrue(fs.is_char_device(to_null))
        for path in (regular, folder, fifo, dangling, os.path.join(tmp, "missing")):
            with self.subTest(path=os.path.basename(path)):
                self.assertFalse(fs.is_char_device(path))

    def test_local_filesystem_adapter(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-core-fs-")
        self.addCleanup(shutil.rmtree, tmp)
        fs = LocalFileSystemAdapter()

        target = os.path.join(tmp, "sub", "file.conf")
        fs.write_atomic(target, "line1\nline2\n", mode=0o600)
        self.assertTrue(fs.exists(target))
        self.assertTrue(fs.is_file(target))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(fs.read_text(target), "line1\nline2\n")
        self.assertEqual(fs.read_first_line(target), "line1\nline2")
        # Byte-transparent, as bash's read -r was on fstab/crypttab: no decode error, no newline translation.
        raw = os.path.join(tmp, "raw")
        with open(raw, "wb") as fh:
            fh.write(b"caf\xe9\r\nx\n")
        text = fs.read_text(raw)
        assert text is not None
        self.assertTrue(text.endswith("\r\nx\n"))
        fs.write_atomic(raw, text, mode=0o644)
        with open(raw, "rb") as fh:
            self.assertEqual(fh.read(), b"caf\xe9\r\nx\n")

        bak = target + ".bak"
        fs.copy_if_absent(target, bak)
        fs.write_atomic(target, "changed\n", mode=0o644)
        fs.copy_if_absent(target, bak)  # keeps original backup
        self.assertEqual(fs.read_text(bak), "line1\nline2\n")

        link = os.path.join(tmp, "link")
        fs.force_symlink("/dev/null", link)
        self.assertTrue(fs.is_symlink(link))
        self.assertEqual(fs.readlink(link), "/dev/null")
        fs.write_atomic(link, "replaced\n", mode=0o644)
        self.assertFalse(fs.is_symlink(link))
        self.assertEqual(fs.read_text(link), "replaced\n")

        self.assertTrue(fs.remove_file(link))  # something was there
        self.assertFalse(fs.remove_file(link))  # idempotent: nothing left to remove
        self.assertFalse(fs.lexists(link))

    def test_subprocess_runner_and_systemctl_adapter(self) -> None:
        runner = SubprocessCommandRunner()
        res = runner.run([sys.executable, "-c", "import sys; print('hi:' + sys.stdin.read())"], check=True)
        self.assertEqual(res.stdout.strip(), "hi:")
        res_in = runner.run(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            check=True,
            input_text="abc",
        )
        self.assertEqual(res_in.stdout.strip(), "abc")
        with self.assertRaises(CommandFailed) as ctx:
            runner.run([sys.executable, "-c", "import sys; sys.exit(3)"], check=True)
        self.assertEqual(ctx.exception.returncode, 3)
        missing = runner.run(["/nonexistent/cst-binary-check"], check=False)
        self.assertEqual(missing.returncode, 127)

        fake = FakeRunner(
            {
                ("systemd-escape", "-p", "--suffix=swap", "/dev/mapper/swap0"): CommandResult(
                    0, "dev-mapper-swap0.swap\n", ""
                ),
                ("systemctl", "stop", "dev-zram0.swap"): CommandResult(1, "", "failed"),
            }
        )
        sysd = SystemctlAdapter(fake)
        sysd.daemon_reload()
        self.assertEqual(sysd.start("systemd-cryptsetup@swap0.service").returncode, 0)
        self.assertEqual(sysd.restart("systemd-cryptsetup@swap0.service").returncode, 0)
        # systemctl's message comes back with its exit status: swap apply shows both.
        self.assertEqual(sysd.stop("dev-zram0.swap"), CommandResult(1, "", "failed"))
        self.assertEqual(sysd.escape_path("/dev/mapper/swap0", suffix="swap"), "dev-mapper-swap0.swap")
        # By default no limit and the caller's locale (what swap has always run).
        self.assertEqual({(o["timeout"], o.get("locale_c")) for o in fake.options}, {(None, False), (None, None)})

    def test_systemctl_adapter_enable_disable_clean(self) -> None:
        unit = UnitName("cst-vm-backup@win.timer")
        fake = FakeRunner(
            {
                ("systemctl", "disable", "--now", unit): CommandResult(1, "", "Access denied"),
            }
        )
        sysd = SystemctlAdapter(fake, timeout=120, locale_c=True)
        self.assertEqual(sysd.enable(unit, now=True).returncode, 0)
        self.assertEqual(sysd.enable(unit).returncode, 0)
        self.assertEqual(sysd.disable(unit, now=True), CommandResult(1, "", "Access denied"))
        self.assertEqual(sysd.disable(unit).returncode, 0)
        self.assertEqual(sysd.clean(unit, what="state").returncode, 0)
        sysd.daemon_reload()
        self.assertEqual(
            fake.calls,
            [
                ["systemctl", "enable", "--now", unit],
                ["systemctl", "enable", unit],
                ["systemctl", "disable", "--now", unit],
                ["systemctl", "disable", unit],
                ["systemctl", "clean", "--what=state", unit],
                ["systemctl", "daemon-reload"],
            ],
        )
        # The options of the adapter reach every systemctl call.
        self.assertEqual({(o["timeout"], o["locale_c"]) for o in fake.options}, {(120, True)})

    def test_systemctl_adapter_over_the_real_runner(self) -> None:
        # A stub systemctl first in PATH (nothing reaches the real one): daemon-reload raises
        # CommandFailed with systemctl's words; a systemctl that is not there reaches the runner's words.
        tmp = tempfile.mkdtemp(prefix="cst-systemctl-")
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "systemctl"), "w") as f:
            f.write("#!/bin/sh\necho \"LC_ALL=$LC_ALL $*\"\necho 'Access denied' >&2\nexit 1\n")
        os.chmod(os.path.join(tmp, "systemctl"), 0o755)
        runner = SubprocessCommandRunner(base_env={"PATH": f"{tmp}:/usr/bin:/bin"})
        sysd = SystemctlAdapter(runner, timeout=30, locale_c=True)
        with self.assertRaises(CommandFailed) as cm:
            sysd.daemon_reload()
        self.assertEqual((cm.exception.returncode, cm.exception.stderr), (1, "Access denied\n"))
        self.assertEqual(sysd.enable(UnitName("a.timer")).stdout, "LC_ALL=C enable a.timer\n")

        def words(argv: Sequence[str], r: CommandResult, timeout: float | None) -> CommandResult:
            return CommandResult(127, "", f"{argv[0]}: not found ({timeout} s)", fault=r.fault)

        empty = os.path.join(tmp, "empty")
        os.mkdir(empty)
        gone = SystemctlAdapter(SubprocessCommandRunner(base_env={"PATH": empty}, words=words), timeout=30)
        self.assertEqual(gone.enable(UnitName("a.timer"), now=True).stderr, "systemctl: not found (30 s)")
        with self.assertRaises(CommandFailed) as cm:
            gone.daemon_reload()
        self.assertEqual(cm.exception.stderr, "systemctl: not found (30 s)")

    def test_systemctl_show_reads_properties_on_the_host(self) -> None:
        seen: list[tuple[list[str], dict[str, object]]] = []

        class Recording:
            def __init__(self, result: CommandResult) -> None:
                self.result = result

            def run(self, argv: Sequence[str], *, check: bool = False, **options: object) -> CommandResult:
                seen.append((list(argv), {"check": check, **options}))
                return self.result

        out = "MemoryLow=51539607552\nDropInPaths=/etc/a.conf /run/b=c.conf\n"
        sysd = SystemctlAdapter(Recording(CommandResult(0, out, "")))
        self.assertEqual(
            sysd.show(UnitName("machine.slice"), ("MemoryLow", "DropInPaths")),
            {"MemoryLow": "51539607552", "DropInPaths": "/etc/a.conf /run/b=c.conf"},
        )
        argv, options = seen[0]
        self.assertEqual(argv, ["systemctl", "show", "-p", "MemoryLow", "-p", "DropInPaths", "machine.slice"])
        self.assertEqual(options, {"check": False, "timeout": 20, "locale_c": True, "on_host": True})
        failing = SystemctlAdapter(Recording(CommandResult(1, "", "Failed to connect to bus")))
        self.assertEqual(failing.show(UnitName("machine.slice"), ("MemoryLow",)), {})
        # A caller that reads the systemd it runs under, with a timeout of its own (vm's timer status).
        seen.clear()
        local = SystemctlAdapter(
            Recording(CommandResult(0, "Result=success\n", "")), timeout=120, on_host=SystemctlHost.LOCAL
        )
        self.assertEqual(local.show(UnitName("a.service"), ("Result",)), {"Result": "success"})
        self.assertEqual(seen[0][1], {"check": False, "timeout": 120, "locale_c": True, "on_host": False})
        # A unit of the user's own manager (bluetooth's wireplumber.service): --user before show.
        seen.clear()
        mine = SystemctlAdapter(Recording(CommandResult(0, "ActiveState=active\nInvocationID=\n", "")))
        self.assertEqual(
            mine.show(UnitName("wireplumber.service"), ("ActiveState", "InvocationID"), user=True),
            {"ActiveState": "active", "InvocationID": ""},
        )
        argv, options = seen[0]
        self.assertEqual(
            argv, ["systemctl", "--user", "show", "-p", "ActiveState", "-p", "InvocationID", "wireplumber.service"]
        )
        self.assertEqual(options, {"check": False, "timeout": 20, "locale_c": True, "on_host": True})

    def test_systemctl_show_units_lists_the_loaded_units_a_glob_names(self) -> None:
        seen: list[tuple[list[str], dict[str, object]]] = []

        class Recording:
            def __init__(self, result: CommandResult) -> None:
                self.result = result

            def run(self, argv: Sequence[str], *, check: bool = False, **options: object) -> CommandResult:
                seen.append((list(argv), {"check": check, **options}))
                return self.result

        out = (
            "Id=var-mnt-cache.mount\nSourcePath=/etc/fstab\nNeedDaemonReload=yes\n\n"
            "Id=home-u-.cache.mount\nSourcePath=/proc/self/mountinfo\nNeedDaemonReload=no\n"
        )
        sysd = SystemctlAdapter(Recording(CommandResult(0, out, "")))
        props = ("Id", "SourcePath", "NeedDaemonReload")
        self.assertEqual(
            sysd.show_units("*.mount", props),
            [
                {"Id": "var-mnt-cache.mount", "SourcePath": "/etc/fstab", "NeedDaemonReload": "yes"},
                {"Id": "home-u-.cache.mount", "SourcePath": "/proc/self/mountinfo", "NeedDaemonReload": "no"},
            ],
        )
        argv, options = seen[0]
        # --state=loaded: every loaded unit the glob names, active or not (a glob alone hides the
        # inactive ones that have no job queued).
        self.assertEqual(
            argv,
            [
                "systemctl",
                "show",
                "--state=loaded",
                "-p",
                "Id",
                "-p",
                "SourcePath",
                "-p",
                "NeedDaemonReload",
                "*.mount",
            ],
        )
        # As show: on the host from a toolbox (SHOW_ONLY, the default), the C locale, 20 s.
        self.assertEqual(options, {"check": False, "timeout": 20, "locale_c": True, "on_host": True})
        # The user manager's adapter asks it with --user first; show of one unit names no --state.
        seen.clear()
        user = SystemctlAdapter(Recording(CommandResult(0, "Id=a.mount\n", "")), user=True)
        self.assertEqual(user.show_units("*.mount", ("Id",)), [{"Id": "a.mount"}])
        user.show(UnitName("a.mount"), ("Id",))
        self.assertEqual(
            [a for a, _ in seen],
            [
                ["systemctl", "--user", "show", "--state=loaded", "-p", "Id", "*.mount"],
                ["systemctl", "--user", "show", "-p", "Id", "a.mount"],
            ],
        )
        # An answer that lists no unit is [] (blank lines too); a systemctl that fails is no answer: None.
        self.assertEqual(SystemctlAdapter(Recording(CommandResult(0, "", ""))).show_units("*.mount", props), [])
        self.assertEqual(SystemctlAdapter(Recording(CommandResult(0, "\n\n", ""))).show_units("*.mount", props), [])
        failing = SystemctlAdapter(Recording(CommandResult(1, "", "Failed to connect to bus")))
        self.assertIsNone(failing.show_units("*.mount", props))
        # LOCAL and a timeout of the adapter's own.
        seen.clear()
        local = SystemctlAdapter(
            Recording(CommandResult(0, "Id=a.mount\n", "")), timeout=5, on_host=SystemctlHost.LOCAL
        )
        self.assertEqual(local.show_units("*.mount", ("Id",)), [{"Id": "a.mount"}])
        self.assertEqual(seen[0][1], {"check": False, "timeout": 5, "locale_c": True, "on_host": False})

    def test_systemctl_show_value_of_the_manager_itself(self) -> None:
        seen: list[tuple[list[str], dict[str, object]]] = []

        class Recording:
            def run(self, argv: Sequence[str], *, check: bool = False, **options: object) -> CommandResult:
                seen.append((list(argv), {"check": check, **options}))
                return CommandResult(0, "/h/.config/systemd/user /etc/systemd/user\n", "")

        # No unit: a property of the manager, asked as show asks (on the host from a toolbox unless
        # LOCAL, the C locale, 20 s or the adapter's timeout); a unit: as before, the adapter's own.
        user = SystemctlAdapter(Recording(), user=True, on_host=SystemctlHost.EVERY)
        res = user.show_value(None, UNIT_PATH_PROP)
        self.assertEqual(res.stdout, "/h/.config/systemd/user /etc/systemd/user\n")
        user.show_value(UnitName("wireplumber"), "MainPID")
        self.assertEqual(
            seen,
            [
                (
                    ["systemctl", "--user", "show", "-p", "UnitPath", "--value"],
                    {"check": False, "timeout": 20, "locale_c": True, "on_host": True},
                ),
                (
                    ["systemctl", "--user", "show", "-p", "MainPID", "--value", "wireplumber"],
                    {"check": False, "timeout": None, "locale_c": False, "on_host": True},
                ),
            ],
        )
        seen.clear()
        local = SystemctlAdapter(Recording(), timeout=5, on_host=SystemctlHost.LOCAL)
        local.show_value(None, UNIT_PATH_PROP, output=OutputMode.STDOUT)
        self.assertEqual(
            seen,
            [
                (
                    ["systemctl", "show", "-p", "UnitPath", "--value"],
                    {"check": False, "timeout": 5, "locale_c": True, "on_host": False, "output": OutputMode.STDOUT},
                )
            ],
        )
        self.assertEqual(
            show_manager_value_argv(["systemctl", "--user"], "UnitPath"),
            ["systemctl", "--user", "show", "-p", "UnitPath", "--value"],
        )

    def test_user_manager_head(self) -> None:
        self.assertEqual(user_manager_head("tester"), ["systemctl", "--user", "-M", "tester@"])
        self.assertEqual(user_manager_head(""), ["systemctl", "--user", "-M", "@"])  # as given: no name checked
        self.assertEqual(user_manager_head("a b"), ["systemctl", "--user", "-M", "a b@"])  # one argument
        self.assertEqual(
            show_manager_value_argv(user_manager_head("tester"), "UnitPath"),
            ["systemctl", "--user", "-M", "tester@", "show", "-p", "UnitPath", "--value"],
        )
        first, second = user_manager_head("tester"), user_manager_head("tester")
        first.append("daemon-reload")
        self.assertEqual(second, ["systemctl", "--user", "-M", "tester@"])  # a new list each time

    def test_systemctl_adapter_user_scope(self) -> None:
        unit = UnitName("cst-toolchains-prune.timer")

        def every_call(sysd: SystemctlAdapter) -> None:
            sysd.start(unit)
            sysd.stop(unit)
            sysd.restart(unit)
            sysd.enable(unit, now=True)
            sysd.disable(unit, now=True)
            sysd.clean(unit, what="state")
            sysd.daemon_reload()
            sysd.daemon_reload_result()
            sysd.is_active(unit)
            sysd.is_enabled(unit)
            sysd.show_environment()
            sysd.show_value(unit, "NextElapseUSecRealtime")
            sysd.show(unit, ("Result",))

        def words(head: list[str]) -> list[list[str]]:
            return [
                [*head, *args]
                for args in (
                    ["start", unit],
                    ["stop", unit],
                    ["restart", unit],
                    ["enable", "--now", unit],
                    ["disable", "--now", unit],
                    ["clean", "--what=state", unit],
                    ["daemon-reload"],
                    ["daemon-reload"],
                    ["is-active", unit],
                    ["is-enabled", unit],
                    ["show-environment"],
                    ["show", "-p", "NextElapseUSecRealtime", "--value", unit],
                    ["show", "-p", "Result", unit],
                )
            ]

        # user: --user right after the program, on every call; nothing else changes.
        user = FakeRunner()
        every_call(SystemctlAdapter(user, user=True))
        self.assertEqual(user.calls, words(["systemctl", "--user"]))
        self.assertEqual([set(o) for o in user.options[:-1]], [{"timeout", "locale_c"}] * 12)
        self.assertEqual(user.options[-1], {"timeout": 20, "locale_c": True, "on_host": True})
        # on_host EVERY: every call through the runner's on_host; LOCAL: show too stays where it runs.
        host = FakeRunner()
        every_call(SystemctlAdapter(host, user=True, on_host=SystemctlHost.EVERY))
        SystemctlAdapter(host, user=True, on_host=SystemctlHost.LOCAL).show(unit, ("Result",))
        self.assertEqual([o.get("on_host") for o in host.options], [True] * 13 + [False])
        # LOCAL names on_host to the runner only for show; SHOW_ONLY is what an adapter built without it does.
        local = FakeRunner()
        every_call(SystemctlAdapter(local, user=True, on_host=SystemctlHost.LOCAL))
        self.assertEqual(local.options, [*user.options[:-1], {**user.options[-1], "on_host": False}])
        show_only = FakeRunner()
        every_call(SystemctlAdapter(show_only, user=True, on_host=SystemctlHost.SHOW_ONLY))
        self.assertEqual(show_only.options, user.options)
        # program: a stub in place of systemctl, never on the host (show neither); base_env and stdin
        # reach every systemctl call, with the adapter's timeout and locale.
        stub = FakeRunner()
        env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": "/run/user/1000"}
        stubbed = SystemctlAdapter(
            stub,
            timeout=60,
            user=True,
            program="/stub/systemctl",
            on_host=SystemctlHost.EVERY,
            base_env=env,
            stdin="inherit",
        )
        every_call(stubbed)
        self.assertEqual(stub.calls, words(["/stub/systemctl", "--user"]))
        scope = {"timeout": 60, "locale_c": False, "base_env": env, "stdin": "inherit"}
        self.assertEqual(stub.options, [scope] * 12 + [{**scope, "locale_c": True, "on_host": False}])
        # Without the scope: the runner gets exactly the options it always got.
        plain = FakeRunner()
        every_call(SystemctlAdapter(plain))
        self.assertEqual(plain.calls, words(["systemctl"]))
        self.assertEqual(plain.options, [{"timeout": None, "locale_c": False}] * 12 + [user.options[-1]])
        # show(user=True) (bluetooth's per-call scope) on an adapter whose scope is already user: one --user.
        both = FakeRunner()
        SystemctlAdapter(both, user=True).show(unit, ("Result",), user=True)
        SystemctlAdapter(both, program="/stub/systemctl").show(unit, ("Result",), user=True)
        self.assertEqual(
            both.calls,
            [
                ["systemctl", "--user", "show", "-p", "Result", unit],
                ["/stub/systemctl", "--user", "show", "-p", "Result", unit],
            ],
        )

    def test_systemctl_adapter_user_scope_over_the_real_runner(self) -> None:
        # is-active exits 3 for a unit that is not active: its state and status come back, nothing raised.
        tmp = tempfile.mkdtemp(prefix="cst-systemctl-")
        self.addCleanup(shutil.rmtree, tmp)
        stub = os.path.join(tmp, "systemctl")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$*"\nexit 3\n')
        os.chmod(stub, 0o755)
        sysd = SystemctlAdapter(SubprocessCommandRunner(), user=True, program=stub, base_env={"PATH": "/usr/bin:/bin"})
        res = sysd.is_active(UnitName("a.timer"))
        self.assertEqual((res.returncode, res.stdout), (3, "--user is-active a.timer\n"))
        # A program that is not there: daemon_reload_result gives the fault and its cause, no CommandFailed.
        gone = SystemctlAdapter(SubprocessCommandRunner(), user=True, program=os.path.join(tmp, "missing"))
        res = gone.daemon_reload_result()
        self.assertEqual((res.returncode, res.fault), (127, CommandFault.MISSING))
        self.assertIsInstance(res.cause, FileNotFoundError)

    def test_systemctl_adapter_per_call_output_mode(self) -> None:
        unit = UnitName("cst-bt-ldac48.service")

        def run_all(sysd: SystemctlAdapter, **kw: OutputMode) -> None:
            sysd.daemon_reload_result(**kw)
            sysd.is_active(unit, **kw)
            sysd.is_enabled(unit, **kw)
            sysd.show_environment(**kw)
            sysd.show_value(unit, "MainPID", **kw)
            sysd.start(unit, **kw)
            sysd.stop(unit, **kw)
            sysd.restart(unit, **kw)
            sysd.enable(unit, now=True, **kw)
            sysd.disable(unit, now=True, **kw)
            sysd.clean(unit, what="state", **kw)

        default_runner = FakeRunner()
        run_all(SystemctlAdapter(default_runner))
        run_all(SystemctlAdapter(default_runner), output=OutputMode.CAPTURE)
        self.assertTrue(all("output" not in opts for opts in default_runner.options))

        for mode in (OutputMode.INHERIT, OutputMode.STDOUT):
            fake = FakeRunner()
            run_all(SystemctlAdapter(fake, user=True), output=mode)
            self.assertEqual([o.get("output") for o in fake.options], [mode] * 11, mode)

    def test_systemctl_adapter_per_call_output_over_real_runner(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-systemctl-out-")
        self.addCleanup(shutil.rmtree, tmp)
        stub = os.path.join(tmp, "systemctl")
        with open(stub, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "OUT $*"\necho "ERR $*" >&2\n')
        os.chmod(stub, 0o755)
        sysd = SystemctlAdapter(SubprocessCommandRunner(), user=True, program=stub, base_env={"PATH": "/usr/bin:/bin"})
        unit = UnitName("a.service")
        cap = sysd.restart(unit)
        self.assertEqual(
            (cap.returncode, cap.stdout, cap.stderr),
            (0, "OUT --user restart a.service\n", "ERR --user restart a.service\n"),
        )
        stdout_only = sysd.restart(unit, output=OutputMode.STDOUT)
        self.assertEqual(
            (stdout_only.returncode, stdout_only.stdout, stdout_only.stderr), (0, "OUT --user restart a.service\n", "")
        )

        recorded = os.path.join(tmp, "inherit.json")
        probe = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from cst_core.adapters import SystemctlAdapter\n"
            "from cst_core.adapters.runner import SubprocessCommandRunner\n"
            "from cst_core.ports import OutputMode, UnitName\n"
            "sysd = SystemctlAdapter(SubprocessCommandRunner(), user=True, program=sys.argv[2])\n"
            "res = sysd.restart(UnitName('a.service'), output=OutputMode.INHERIT)\n"
            "with open(sys.argv[3], 'w', encoding='utf-8') as fh:\n"
            "    json.dump([res.returncode, res.stdout, res.stderr], fh)\n"
        )
        child = subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe, REPO_ROOT, stub, recorded],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, "OUT --user restart a.service\n")
        self.assertEqual(child.stderr, "ERR --user restart a.service\n")
        with open(recorded, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), [0, "", ""])

    def test_host_execution_context_and_clock(self) -> None:
        host = HostExecutionContext(exists=lambda _p: False, euid=lambda: 1000)
        self.assertFalse(host.is_container())
        self.assertFalse(host.is_root())
        self.assertEqual(host.etc_dir(), "/etc")
        self.assertEqual(host.host_run_dir("cpu-setup"), "/run/cpu-setup")
        self.assertEqual(host.path("/proc/meminfo"), "/proc/meminfo")

        box = HostExecutionContext(exists=lambda p: p == "/.dockerenv", euid=lambda: 0)
        self.assertTrue(box.is_container())
        self.assertTrue(box.is_root())
        self.assertEqual(box.etc_dir(), "/run/host/etc")
        self.assertEqual(box.host_run_dir("cpu-setup"), "/run/host/run/cpu-setup")
        self.assertEqual(box.path("/proc/meminfo"), "/run/host/proc/meminfo")

        clock = SystemClockAdapter()
        self.assertGreater(clock.now(), 0.0)
        self.assertGreater(clock.monotonic(), 0.0)


class _FakeDispatchPort:
    """In-memory CstDispatchPort double recording executed subprocesses."""

    def __init__(
        self,
        *,
        files: set[str] | None = None,
        container: bool = False,
        uid: int = 1000,
        env: Mapping[str, str] | None = None,
        passwd: Mapping[str, tuple[int, str]] | None = None,
        handler: Callable[[list[str], Mapping[str, str] | None], int] | None = None,
        on_path: Mapping[str, str] | None = None,
        links: Mapping[str, str] | None = None,
    ) -> None:
        self.on_path = {} if on_path is None else dict(on_path)
        self.links = {} if links is None else dict(links)
        self.files = set() if files is None else files
        self.container = container
        self.uid = uid
        self.env = {} if env is None else dict(env)
        self.passwd = {} if passwd is None else dict(passwd)
        self.handler = handler
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self.execs: list[list[str]] = []

    def is_file(self, path: str) -> bool:
        return path in self.files

    def is_container(self) -> bool:
        return self.container

    def euid(self) -> int:
        return self.uid

    def environ(self) -> Mapping[str, str]:
        return dict(self.env)

    def user_passwd(self, user: str) -> tuple[int, str]:
        return self.passwd[user]

    def which(self, name: str, path: str | None) -> str | None:
        return self.on_path.get(name) if path is not None and path != "" else None

    def realpath(self, path: str) -> str:
        return self.links.get(path, path)

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        cmd = list(argv)
        copied_env = None if env is None else dict(env)
        self.calls.append((cmd, copied_env))
        if self.handler is not None:
            return self.handler(cmd, copied_env)
        return 0

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        self.execs.append(list(argv))
        return self.run_passthrough(argv, env=env)


class CstDispatcherTest(unittest.TestCase):
    def _dispatcher(
        self, port: _FakeDispatchPort, *, repo_dir: str = "/repo", arg0: str = "./cst"
    ) -> tuple[CstDispatcher, io.StringIO, io.StringIO]:
        out = io.StringIO()
        err = io.StringIO()
        dispatcher = CstDispatcher(
            port=port,
            repo_dir=repo_dir,
            self_path=f"{repo_dir}/cst",
            arg0=arg0,
            stdout=out,
            stderr=err,
        )
        return dispatcher, out, err

    def test_format_cst_usage_marks_missing_modules(self) -> None:
        text = format_cst_usage({"btrfs": True, "cpu": False})
        self.assertIn("btrfs      storage:", text)
        self.assertNotIn("btrfs      storage:.*not in this copy", text)
        self.assertIn("cpu-setup.sh", text)
        self.assertIn("(not in this copy)", text)

    def test_help_and_unknown_command(self) -> None:
        files = {f"/repo/{m.script}" for m in MODULES if m.name != "vm"}
        port = _FakeDispatchPort(files=files, env={"CST_NAME": "cst"})
        dispatcher, out, err = self._dispatcher(port)

        self.assertEqual(dispatcher.dispatch([]), 0)
        self.assertIn("Modules here:", out.getvalue())
        self.assertIn("  vm         ", out.getvalue())
        self.assertIn("(not in this copy)", out.getvalue())

        # bash's `cmd="${1:-help}"`: one empty word is help too.
        out.truncate(0)
        self.assertEqual(dispatcher.dispatch([""]), 0)
        self.assertIn("Modules here:", out.getvalue())
        self.assertEqual(err.getvalue(), "")

        self.assertEqual(dispatcher.dispatch(["nope"]), 2)
        self.assertIn("cst: unknown command or module 'nope'", err.getvalue())
        self.assertIn("Modules here:", err.getvalue())

    def test_the_name_in_messages_comes_from_the_port(self) -> None:
        # "cst" when the cst on PATH is this copy (through a link), else how it was called.
        missing = ["nope"]
        for on_path, links, want in (
            ({"cst": "/home/u/.local/bin/cst"}, {"/home/u/.local/bin/cst": "/repo/cst"}, "cst"),
            ({"cst": "/usr/bin/cst"}, {}, "./cst"),
            ({}, {}, "./cst"),
        ):
            port = _FakeDispatchPort(env={"PATH": "/usr/bin"}, on_path=on_path, links=links)
            dispatcher, _, err = self._dispatcher(port)
            with self.subTest(on_path=on_path):
                self.assertEqual(dispatcher.dispatch(missing), 2)
                self.assertTrue(
                    err.getvalue().startswith(f"{want}: unknown command or module 'nope'\n"), err.getvalue()
                )

    def test_module_dispatch_and_missing_module(self) -> None:
        port = _FakeDispatchPort(files={"/repo/swap-setup.sh"}, env={"CST_NAME": "cst"})
        dispatcher, _, err = self._dispatcher(port)

        self.assertEqual(dispatcher.dispatch(["swap", "plan", "--root"]), 0)
        cmd, env = port.calls[0]
        self.assertEqual(cmd, ["bash", "/repo/swap-setup.sh", "plan", "--root"])
        assert env is not None
        self.assertEqual(env["CST_PROG"], "cst swap")
        self.assertEqual(port.execs, [cmd], "the module replaces cst (bash's exec)")

        self.assertEqual(dispatcher.dispatch(["vm", "status"]), 1)
        self.assertIn("cst: the 'vm' module is not in this copy (/repo/vm-setup.sh is missing)", err.getvalue())

    def test_doctor_as_root_with_a_sudo_user_that_is_gone_says_so(self) -> None:
        # A SUDO_USER no longer in passwd (deleted, or a directory user while the directory is down):
        # one error line, as the module routing gives, not a KeyError traceback.
        port = _FakeDispatchPort(files={"/repo/doctor/cst-doctor"}, uid=0, env={"CST_NAME": "cst", "SUDO_USER": "gone"})
        dispatcher, _, err = self._dispatcher(port)
        self.assertEqual(dispatcher.dispatch(["doctor"]), 1)
        self.assertEqual(err.getvalue(), "cst: ERROR: SUDO_USER=gone: no such user\n")
        self.assertEqual(port.calls, [])

    def test_doctor_as_user_and_via_sudo_user(self) -> None:
        doc = "/repo/doctor/cst-doctor"
        user_port = _FakeDispatchPort(files={doc}, uid=1000, env={"CST_NAME": "cst"})
        user_disp, _, _ = self._dispatcher(user_port)
        self.assertEqual(user_disp.dispatch(["doctor", "--brief"]), 0)
        self.assertEqual(user_port.calls[0][0], ["python3", "-I", "-B", doc, "--brief"])
        self.assertEqual(user_port.execs, [], "doctor runs in the foreground, not exec'd")
        assert user_port.calls[0][1] is not None
        self.assertEqual(user_port.calls[0][1]["CST_PROG"], "cst doctor")

        root_port = _FakeDispatchPort(
            files={doc},
            uid=0,
            env={"CST_NAME": "cst", "SUDO_USER": "alice"},
            passwd={"alice": (1001, "/home/alice")},
        )
        root_disp, _, _ = self._dispatcher(root_port)
        self.assertEqual(root_disp.dispatch(["doctor"]), 0)
        self.assertEqual(
            root_port.calls[0][0],
            [
                "runuser",
                "-u",
                "alice",
                "--",
                "env",
                "-u",
                "XDG_STATE_HOME",
                "-u",
                "SUDO_USER",
                "HOME=/home/alice",
                "USER=alice",
                "LOGNAME=alice",
                "XDG_RUNTIME_DIR=/run/user/1001",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
                "CST_PROG=cst doctor",
                "python3",
                "-I",
                "-B",
                doc,
            ],
        )

    def test_status_elevation_and_container_skip(self) -> None:
        all_files = {f"/repo/{m.script}" for m in MODULES} | {"/repo/doctor/cst-doctor"}
        host_nonroot = _FakeDispatchPort(files=all_files, container=False, uid=1000, env={"CST_NAME": "cst"})
        disp_elevate, _, err = self._dispatcher(host_nonroot)
        self.assertEqual(disp_elevate.dispatch(["status", "extra"]), 2)
        self.assertIn("cst status: no arguments", err.getvalue())

        self.assertEqual(disp_elevate.dispatch(["status"]), 0)
        self.assertIn("asking sudo once", err.getvalue())
        self.assertEqual(host_nonroot.calls[0][0], ["sudo", "--", "env", "CST_NAME=cst", "/repo/cst", "status"])
        self.assertEqual(host_nonroot.execs, [host_nonroot.calls[0][0]], "sudo replaces cst (bash's exec)")

        box_port = _FakeDispatchPort(files=all_files, container=True, uid=1000, env={"CST_NAME": "cst"})
        disp_box, out, _ = self._dispatcher(box_port)
        self.assertEqual(disp_box.dispatch(["status"]), 0)
        self.assertIn("skipped: its status needs root on the host", out.getvalue())
        self.assertEqual(
            [cmd for cmd, _ in box_port.calls],
            [
                ["bash", "/repo/swap-setup.sh", "status"],
                ["bash", "/repo/bluetooth-setup.sh", "status"],
                ["bash", "/repo/toolchains-setup.sh", "status"],
                ["bash", "/repo/vm-setup.sh", "status"],
                ["python3", "-I", "-B", "/repo/doctor/cst-doctor", "--brief"],
            ],
        )
        self.assertEqual(box_port.execs, [], "the status of each module runs in the foreground")


class LocalCstDispatchAdapterTest(unittest.TestCase):
    """The real adapter: exit statuses as bash shows them."""

    def test_a_foreground_command_killed_by_a_signal_gives_128_plus_n(self) -> None:
        port = LocalCstDispatchAdapter()
        # run_passthrough hands its own stdin on (a terminal, as bash does); SHLVL keeps a socket
        # stdin from making bash read ~/.bashrc (AGENTS.md, "Test gotchas").
        env = {**_CLEAN_ENV, "SHLVL": "1"}
        self.assertEqual(port.run_passthrough(["bash", "-c", "kill -TERM $$"], env=env), 143)
        self.assertEqual(port.run_passthrough(["bash", "-c", "exit 3"], env=env), 3)

    def test_restore_lc_ctype(self) -> None:
        for handed_over, before, after in (
            (None, {"LC_CTYPE": "C.UTF-8"}, "C.UTF-8"),  # cli.py run without the wrapper: left alone
            ("", {"LC_CTYPE": "C.UTF-8"}, None),
            ("=C", {"LC_CTYPE": "C.UTF-8"}, "C"),
            ("=", {}, ""),
        ):
            env = before if handed_over is None else {**before, "CST_LC_CTYPE": handed_over}
            with (
                self.subTest(handed_over=handed_over),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("cst_core.adapters.runtime.restore_default_signals"),
            ):
                undo_python_start()
                self.assertEqual(os.environ.get("LC_CTYPE"), after)
                self.assertNotIn("CST_LC_CTYPE", os.environ)

    def test_beside_program_is_in_the_folder_it_was_run_from_unresolved(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-beside-")
        self.addCleanup(shutil.rmtree, tmp)
        real = os.path.join(tmp, "real")
        os.mkdir(real)
        link = os.path.join(tmp, "link")
        os.symlink(real, link)
        self.assertEqual(beside_program("front", f"{link}/prog"), f"{link}/front")
        self.assertEqual(beside_program("front", "/usr/local/sbin/prog"), "/usr/local/sbin/front")
        self.assertEqual(beside_program("front", "prog"), os.path.join(os.getcwd(), "front"))
        self.assertEqual(beside_program("front", ""), "")

    def test_restore_lc_ctype_leaves_the_signals_alone(self) -> None:
        # The LC_CTYPE part only: a program with cleanup keeps SIGPIPE ignored.
        before = signal.getsignal(signal.SIGPIPE)
        self.addCleanup(signal.signal, signal.SIGPIPE, before)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        with mock.patch.dict(os.environ, {"LC_CTYPE": "C.UTF-8", "CST_LC_CTYPE": "=C"}, clear=True):
            restore_lc_ctype()
            self.assertEqual(os.environ.get("LC_CTYPE"), "C")
            self.assertNotIn("CST_LC_CTYPE", os.environ)
        self.assertEqual(signal.getsignal(signal.SIGPIPE), signal.SIG_IGN)
        with mock.patch.dict(os.environ, {"LC_CTYPE": "C.UTF-8", "CST_LC_CTYPE": ""}, clear=True):
            restore_lc_ctype()
            self.assertNotIn("LC_CTYPE", os.environ)

    def test_undo_python_start_gives_a_child_the_caller_s_lc_ctype(self) -> None:
        # A real Python under a C locale (no LC_ALL, LANG or LC_CTYPE: PEP 538 sets LC_CTYPE=C.UTF-8), and
        # the command it then runs.
        probe = (
            f"import subprocess, sys; sys.path.insert(0, {REPO_ROOT!r})\n"
            "from cst_core.adapters.runtime import undo_python_start\n"
            "undo_python_start()\n"
            "subprocess.run(['sh', '-c', 'echo \"${LC_CTYPE-unset} ${CST_LC_CTYPE-gone}\"'])\n"
        )
        for caller, seen in (
            ({}, "C.UTF-8 gone"),  # no wrapper: Python's own change stays (the case this undoes)
            ({"CST_LC_CTYPE": ""}, "unset gone"),
            ({"LC_CTYPE": "C", "CST_LC_CTYPE": "=C"}, "C gone"),
        ):
            with self.subTest(caller=caller):
                proc = subprocess.run(
                    [SYSTEM_PYTHON, "-I", "-B", "-c", probe],
                    env={"PATH": "/usr/bin:/bin", **caller},
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(proc.stdout, seen + "\n", proc.stderr)

    def test_lc_ctype_handover(self) -> None:
        # What `cst swap` puts in sudo's `env` when it starts itself again as root.
        for env, setting in (
            ({}, "CST_LC_CTYPE="),
            ({"LC_CTYPE": "C"}, "CST_LC_CTYPE==C"),
            ({"LC_CTYPE": ""}, "CST_LC_CTYPE=="),
        ):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(lc_ctype_handover(), setting)

    def test_a_command_that_cannot_start(self) -> None:
        port = LocalCstDispatchAdapter()
        missing = os.path.join(tempfile.gettempdir(), "cst-no-such-program")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(port.run_passthrough([missing]), 127)
            self.assertEqual(port.exec_replace([missing]), 127)
        self.assertEqual(err.getvalue(), f"{missing}: No such file or directory\n" * 2)


def _cst_copy(tmp: str, swap_setup: str) -> str:
    """A copy of the cst command (the wrapper and cst_core) in tmp, with swap-setup.sh as the only module."""
    shutil.copy2(os.path.join(REPO_ROOT, "cst"), os.path.join(tmp, "cst"))
    shutil.copytree(
        os.path.join(REPO_ROOT, "cst_core"),
        os.path.join(tmp, "cst_core"),
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    with open(os.path.join(tmp, "swap-setup.sh"), "w", encoding="utf-8") as fh:
        fh.write(swap_setup)
    return os.path.join(tmp, "cst")


class CstCommandTest(unittest.TestCase):
    """The real cst command, run from a scratch copy: exec, signals, pipes and the environment of a module."""

    tmp: str

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-cmd-")

    @override
    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _run(
        self, swap_setup: str, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        cst = _cst_copy(self.tmp, swap_setup)
        return subprocess.run(
            [cst, *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=dict(_CLEAN_ENV if env is None else env),
            timeout=30,
        )

    def test_a_module_killed_by_a_signal_kills_cst_with_it(self) -> None:
        # main ran `exec bash MODULE`: the module was the cst process (bash showed 143).
        proc = self._run("kill -TERM $$\n", "swap")
        self.assertEqual(proc.returncode, -signal.SIGTERM, proc.stderr)

    def test_the_module_replaces_cst(self) -> None:
        proc = self._run('echo "pid=$$ ppid=$PPID"\n', "swap")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The module runs as the process cst started as: its parent is this test.
        self.assertIn(f"ppid={os.getpid()}", proc.stdout)

    def test_ctrl_c_lets_the_module_finish_its_trap(self) -> None:
        cst = _cst_copy(
            self.tmp,
            # The sleep runs in the background (where Ctrl-C does not reach it) so the
            # trap runs at once wherever the signal lands; the trap ends it.
            "trap 'echo cleanup start; kill $s; sleep 0.3; echo cleanup done; exit 130' INT\n"
            "sleep 10 & s=$!\necho started\nwait $s\n",
        )
        proc = subprocess.Popen(
            [cst, "swap"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline(), "started\n")
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn("cleanup done", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(proc.returncode, 130)

    def test_status_waits_for_a_module_that_handles_ctrl_c(self) -> None:
        # status runs each module in the foreground, as bash did: Ctrl-C reaches the module, which may
        # handle it; cst neither prints a traceback nor kills the module.
        cst = _cst_copy(
            self.tmp,
            "trap 'echo cleanup start; kill $s; sleep 0.3; echo cleanup done' INT\n"
            "sleep 10 & s=$!\necho started\nwait $s\necho after\n",
        )
        proc = subprocess.Popen(
            [cst, "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn("cleanup done", out)
        self.assertIn("==== doctor", out)
        self.assertNotIn("Traceback", err)

    def _status_signalled(self, module: str, sig: int) -> tuple[str, str, int]:
        """cst status, with sig sent to its whole process group (as the terminal sends Ctrl-C) once the module started."""
        proc = subprocess.Popen(
            [_cst_copy(self.tmp, module), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, sig)
        out, err = proc.communicate(timeout=30)
        return out, err, int(proc.returncode)  # set by communicate

    def test_status_stops_when_a_module_dies_of_ctrl_c(self) -> None:
        out, err, rc = self._status_signalled("echo started\nsleep 10\n", signal.SIGINT)
        self.assertNotIn("==== doctor", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, -signal.SIGINT)

    def test_status_goes_on_after_ctrl_backslash(self) -> None:
        # bash ignores SIGQUIT itself: a module killed by Ctrl-\ failed, and main's status went on.
        # (bash ignores it too until the exec: the module says "started" from the program it became.)
        module = "exec /usr/bin/python3 -I -c 'import time; print(\"started\", flush=True); time.sleep(10)'\n"
        out, err, rc = self._status_signalled(module, signal.SIGQUIT)
        self.assertIn("==== doctor", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, 1)

    def test_status_ignores_ctrl_backslash_between_two_modules(self) -> None:
        # bash ignored SIGQUIT the whole time, also between two modules. A small stdout pipe that the
        # test fills while the module runs holds cst in the next header, after the module ended.
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)
        proc = subprocess.Popen(
            # No core file when it dies of the signal; SIGQUIT at its default, whatever this test got.
            [
                "bash",
                "-c",
                'ulimit -c 0; exec env --default-signal=QUIT "$0" status',
                _cst_copy(self.tmp, 'echo "module $$"\nsleep 0.5\n'),
            ],
            stdout=w,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stderr is not None
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.wait, 30)
        self.addCleanup(proc.kill)  # cleanups run last first: kill, wait, close
        out = b""
        while not (started := re.search(rb"^module (\d+)$", out, re.MULTILINE)):
            self.assertTrue(select.select([r], [], [], 10)[0], f"no module line: {out!r}")
            out += os.read(r, 4096)
        # The module sleeps, cst waits for it: neither writes while the pipe is non-blocking.
        os.set_blocking(w, False)
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(w, b"x")
        os.set_blocking(w, True)
        os.close(w)
        module_pid = int(started.group(1))
        deadline = time.monotonic() + 10
        while os.path.exists(f"/proc/{module_pid}") and time.monotonic() < deadline:  # until cst reaped it
            time.sleep(0.01)
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGQUIT)
        time.sleep(0.2)
        while chunk := os.read(r, 65536):
            out += chunk
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 1, err)  # 1: this copy has no doctor
        self.assertIn(b"==== doctor", out)
        self.assertNotIn(b"Traceback", err)

    def test_programs_cst_starts_get_sigquit_at_its_default(self) -> None:
        # cst ignores SIGQUIT with a handler, not SIG_IGN, which a module would inherit across exec.
        module = "grep '^SigIgn:' /proc/self/status\n"
        for args in (["status"], ["swap"]):
            with self.subTest(args=args):
                proc = subprocess.run(
                    ["env", "--default-signal=INT,QUIT", _cst_copy(self.tmp, module), *args],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    env=dict(_CLEAN_ENV),
                    check=False,
                    timeout=30,
                )
                lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("SigIgn:")]
                self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
                ignored = int(lines[0].split()[1], 16)
                for sig in (signal.SIGQUIT, signal.SIGINT):
                    self.assertFalse(ignored & (1 << (sig - 1)), f"{sig.name} ignored in a module: {lines[0]}")

    def test_status_goes_on_when_only_the_module_got_ctrl_c(self) -> None:
        # bash dies with a foreground command's SIGINT only when it got the signal too (Ctrl-C reaches
        # both); a SIGINT sent to the module alone is that module's failure, and main's status went on.
        proc = self._run("echo started\nkill -INT $$\necho not reached\n", "status")
        self.assertIn("started", proc.stdout)
        self.assertNotIn("not reached", proc.stdout)
        self.assertIn("==== doctor", proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_ctrl_c_ignored_by_cst_s_caller_stays_ignored(self) -> None:
        # bash keeps a signal ignored at its start ignored for the commands it runs (`nohup`-style callers).
        cst = _cst_copy(self.tmp, "echo started\nsleep 0.5\necho after\n")
        proc = subprocess.Popen(
            ["bash", "-c", "trap '' INT; exec \"$0\" status", cst],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn("after", out)
        self.assertIn("==== doctor", out)
        self.assertNotIn("Traceback", err)

    def test_help_into_a_closed_pipe_stops_quietly(self) -> None:
        cst = _cst_copy(self.tmp, "")
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                [cst, "help"],
                stdout=write_end,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                check=False,
                env=dict(_CLEAN_ENV),
                timeout=30,
            )
        finally:
            os.close(write_end)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.returncode, -signal.SIGPIPE)

    def test_a_module_starts_with_sigpipe_at_its_default(self) -> None:
        # Python ignores SIGPIPE; an exec'd module must not inherit that (bash cannot undo an ignored signal).
        proc = self._run('yes | head -n 1\necho "pipe=${PIPESTATUS[*]}"\n', "swap")
        self.assertIn("pipe=141 0", proc.stdout)

    def test_a_module_sees_only_cst_prog_from_cst(self) -> None:
        script = (
            'echo "PROG=$CST_PROG NAME=${CST_NAME-unset} SELF=${CST_SELF-unset} DIR=${CST_DIR-unset}'
            ' ARG0=${CST_ARG0-unset} LC=${CST_LC_CTYPE-unset}"\n'
        )
        proc = self._run(script, "swap")
        cst = os.path.join(self.tmp, "cst")
        unset = "SELF=unset DIR=unset ARG0=unset LC=unset"
        self.assertEqual(proc.stdout, f"PROG={cst} swap NAME=unset {unset}\n", proc.stderr)
        # A name the caller set (sudo's `env CST_NAME=...`) is kept, as before.
        proc = self._run(script, "swap", env={**_CLEAN_ENV, "CST_NAME": "cst"})
        self.assertEqual(proc.stdout, f"PROG=cst swap NAME=cst {unset}\n", proc.stderr)

    def test_a_module_gets_the_caller_s_lc_ctype(self) -> None:
        # Under a C locale Python sets LC_CTYPE=C.UTF-8 in its own environment (PEP 538); the bash cst
        # handed the caller's LC_CTYPE on as it was, unset included.
        script = 'echo "LC_CTYPE=${LC_CTYPE-unset}"\n'
        path = {"PATH": "/usr/bin:/bin"}
        for caller, seen in (
            ({}, "unset"),
            ({"LANG": "C"}, "unset"),
            ({"LC_CTYPE": "C"}, "C"),
            ({"LC_CTYPE": ""}, ""),
            ({"LC_CTYPE": "C.UTF-8"}, "C.UTF-8"),
        ):
            with self.subTest(caller=caller):
                proc = self._run(script, "swap", env={**path, **caller})
                self.assertEqual(proc.stdout, f"LC_CTYPE={seen}\n", proc.stderr)


class OstreeQueryTest(unittest.TestCase):
    """cst_core.domain.ostree: the one `rpm-ostree status --json` query, in the words doctor always used."""

    @staticmethod
    def _answer(rc: int, out: str, err: str = "") -> tuple[list[list[str]], OstreeStatus | str]:
        calls: list[list[str]] = []

        def run(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            return rc, out, err

        return calls, query_status(run)

    def test_the_query_and_its_failures(self) -> None:
        calls, got = self._answer(1, "", "error: no daemon\nmore\n")
        self.assertEqual(calls, [["rpm-ostree", "status", "--json"]])
        self.assertEqual(got, "rpm-ostree status --json failed: error: no daemon")
        self.assertEqual(self._answer(3, "")[1], "rpm-ostree status --json failed: exit 3")
        prefix = "rpm-ostree status --json: unreadable output "
        for out, why in (
            ("{", "(Expecting property name enclosed in double quotes: line 1 column 2 (char 1))"),
            ("{}", "('deployments')"),
            # Another JSON value than an object: one reason, not the text Python gave each type.
            ("[]", "(not a JSON object)"),
            ('"x"', "(not a JSON object)"),
            ("5", "(not a JSON object)"),
            ("null", "(not a JSON object)"),
            ('{"deployments": null}', "('NoneType' object is not iterable)"),
            ('{"deployments": 5}', "('int' object is not iterable)"),
        ):
            with self.subTest(out=out):
                self.assertEqual(self._answer(0, out)[1], prefix + why)

    def test_deep_nesting_is_unreadable_output_not_a_crash(self) -> None:
        # json.loads accepts a few hundred levels, which the check of each deployment's values
        # then walks past the recursion limit; deeper, json.loads itself gives up. Both: RecursionError.
        prefix = "rpm-ostree status --json: unreadable output ("
        for depth in (700, 100_000):
            with self.subTest(depth=depth):
                out = '{"deployments": [{"booted": true, "x": ' + "[" * depth + "]" * depth + "}]}"
                got = self._answer(0, out)[1]
                self.assertIsInstance(got, str)
                self.assertTrue(str(got).startswith(prefix), got)

    def test_booted_and_pending(self) -> None:
        booted = {"booted": True, "osname": "fedora", "checksum": "b"}
        staged = {"osname": "fedora", "checksum": "s", "staged": True}
        other = {"osname": "other", "checksum": "o"}
        rollback = {"osname": "fedora", "checksum": "r"}
        cases = (
            ([staged, booted], staged),  # staged: listed first
            ([rollback, booted], rollback),  # after rpm-ostree rollback: first, not staged
            ([other, booted, rollback], None),  # another osname first: the booted one is the next boot
            ([booted, rollback], None),
        )
        for deps, pending in cases:
            with self.subTest(deps=deps):
                got = self._answer(0, json.dumps({"deployments": ["junk", *deps, 3]}))[1]
                assert isinstance(got, OstreeStatus), got
                self.assertEqual(got.deployments, deps, "only the objects, in order")
                self.assertEqual(got.booted, booted)
                self.assertEqual(got.pending, pending)
        got = self._answer(0, json.dumps({"deployments": [staged, rollback]}))[1]
        assert isinstance(got, OstreeStatus), got
        self.assertEqual((got.booted, got.pending), (None, staged), "no booted one: the first of any osname")
        got = self._answer(0, '{"deployments": []}')[1]
        self.assertEqual(got, OstreeStatus([], None, None))


class HostDetectionTest(unittest.TestCase):
    """One host-or-toolbox detection (HostExecutionContext), injected where a module asks."""

    class _Probe:
        def __init__(self, *, container: bool) -> None:
            self.container = container

        def is_container(self) -> bool:
            return self.container

    def test_the_dispatcher_asks_the_probe(self) -> None:
        for container in (True, False):
            self.assertIs(LocalCstDispatchAdapter(self._Probe(container=container)).is_container(), container)
        markers = {"/run/.containerenv"}
        ctx = HostExecutionContext(exists=lambda p: p in markers)
        self.assertTrue(LocalCstDispatchAdapter(ctx).is_container())
        markers.clear()
        self.assertFalse(LocalCstDispatchAdapter(ctx).is_container())


class _FakeRoutePort:
    """ModuleRoutePort over plain values: what the router says and execs is recorded."""

    def __init__(
        self,
        *,
        euid: int = 1000,
        container: bool = False,
        spawn: bool = True,
        env: Mapping[str, str] | None = None,
        script_text: str = "#!/bin/bash\n# SPDX-License-Identifier: MPL-2.0\n#\n# x-setup.sh  help\n\nset -e\n",
    ) -> None:
        self._euid, self._container, self._spawn, self._text = euid, container, spawn, script_text
        self._env = {"PATH": "/usr/bin"} if env is None else dict(env)
        self.said: list[tuple[str, str]] = []
        self.execs: list[tuple[list[str], dict[str, str] | None]] = []

    def environ(self) -> Mapping[str, str]:
        return self._env

    def euid(self) -> int:
        return self._euid

    def is_container(self) -> bool:
        return self._container

    def user_passwd(self, user: str) -> tuple[int, str]:
        if user != "alice":
            raise KeyError(user)
        return 1234, "/home/alice"

    def exec_on_host(self, argv: Sequence[str], *, settings: Mapping[str, str]) -> int | None:
        if not self._spawn:
            return None
        self.execs.append((["flatpak-spawn", *argv], dict(settings)))
        return 0

    def read_text(self, path: str) -> str:
        return self._text

    def say(self, stream: Literal["out", "err"], text: str) -> None:
        self.said.append((stream, text))

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        self.execs.append((list(argv), None if env is None else dict(env)))
        return 0


class ModuleRouterTest(unittest.TestCase):
    """cst_core's routing table and ModuleRouter (what vm-setup.sh and toolchains-setup.sh run)."""

    @staticmethod
    def _router(
        module: str, port: _FakeRoutePort, *, cst_prog: str = "cst x", container: str = "", sudo_user: str = ""
    ) -> ModuleRouter:
        settings = RouteSettings(
            script="/c/x-setup.sh", arg0="./x-setup.sh", cst_prog=cst_prog, container=container, sudo_user=sudo_user
        )
        return ModuleRouter(port=port, routing=ROUTING[module], settings=settings)

    def test_route_command(self) -> None:
        vm = ROUTING["vm"]
        self.assertEqual(route_command(vm, "help", [], "p").action, "help")
        self.assertEqual(
            route_command(vm, "nope", [], "p"), Routed(RouteAction.UNKNOWN, message="Unknown command: nope")
        )
        self.assertEqual(
            route_command(vm, "tune", ["x"], "cst vm"),
            Routed(RouteAction.USAGE, message="Usage: cst vm tune plan|apply [VM] [--guest-has-virtio]"),
        )
        for args, privilege in (
            (["list"], Privilege.READ),
            (["tune", "plan"], Privilege.READ),
            (["tune", "apply"], Privilege.ROOT),
            (["nocow", "plan"], Privilege.READ),
            (["nocow", "plan", "w", "--root"], Privilege.ROOT),
            (["nocow", "apply", "--root"], Privilege.USER),
            (["backup-timer", "status"], Privilege.READ),
            (["backup-timer", "off"], Privilege.ROOT),
            (["backup"], Privilege.ROOT),
        ):
            with self.subTest(args=args):
                self.assertEqual(route_command(vm, args[0], args[1:], "p"), Routed(RouteAction.RUN, privilege))
        for cmd in ("status", "prune", "auto", "config"):
            self.assertEqual(
                route_command(ROUTING["toolchains"], cmd, ["x"], "p"), Routed(RouteAction.RUN, Privilege.AS_USER)
            )
        self.assertEqual(route_command(ROUTING["toolchains"], "list", [], "p").action, "unknown")

    def test_route_words_are_plain_strings(self) -> None:
        # Routed's action and privilege as the strings they are: a member whose value changes fails here.
        vm = ROUTING["vm"]
        self.assertEqual(route_command(vm, "tune", ["x"], "p").action, "usage")
        for args, privilege in (
            (["list"], "read"),
            (["tune", "apply"], "root"),
            (["nocow", "apply", "--root"], "user"),
        ):
            with self.subTest(args=args):
                routed = route_command(vm, args[0], args[1:], "p")
                self.assertEqual((routed.action, routed.privilege), ("run", privilege))
        self.assertEqual(route_command(ROUTING["toolchains"], "status", ["x"], "p").privilege, "as-user")
        self.assertEqual(
            {p.name: p.value for p in Privilege}, {"READ": "read", "USER": "user", "ROOT": "root", "AS_USER": "as-user"}
        )
        self.assertEqual(
            {a.name: a.value for a in RouteAction},
            {"HELP": "help", "USAGE": "usage", "UNKNOWN": "unknown", "RUN": "run"},
        )

    def test_script_help_is_what_the_header_sed_printed(self) -> None:
        text = "#!/x\n# SPDX-License-Identifier: MPL-2.0\n#\n# a  b\n#c\n#   d\nnot a comment\n\nlater\n"
        self.assertEqual(script_help(text, "p"), "\na  b\nc\n  d\nnot a comment\n\nUsage: p <command> [args]\n")
        self.assertEqual(
            script_help("#!/x\n# one", "p"), "one\nUsage: p <command> [args]\n", "no empty line: to the end"
        )
        # The header runs to the end of a file that ends with a newline: no blank line before Usage (as sed).
        self.assertEqual(script_help("#!/x\n# one\n# two\n", "p"), "one\ntwo\nUsage: p <command> [args]\n")
        self.assertEqual(script_help("#!/x\n# one\n\n", "p"), "one\n\nUsage: p <command> [args]\n")
        # The words after the command, as the script's usage line named them (bluetooth-setup.sh: [options]).
        self.assertEqual(
            script_help("#!/x\n# one\n\n", "p", words="[options]"), "one\n\nUsage: p <command> [options]\n"
        )
        for module in ROUTING:
            script = os.path.join(REPO_ROOT, f"{module}-setup.sh")
            sed = subprocess.run(
                [
                    "bash",
                    "-c",
                    "sed -n '2,/^$/{/SPDX-License-Identifier:/!p;}' \"$1\" | sed 's/^# \\{0,1\\}//'",
                    "_",
                    script,
                ],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            with open(script, encoding="utf-8") as fh:
                self.assertEqual(script_help(fh.read(), "q"), sed.stdout + "Usage: q <command> [args]\n")

    def test_help_unknown_and_usage(self) -> None:
        port = _FakeRoutePort()
        self.assertEqual(self._router("vm", port).route([]), 0)
        self.assertEqual(port.said, [("out", "\nx-setup.sh  help\n\nUsage: cst x <command> [args]\n")])
        port = _FakeRoutePort()
        self.assertEqual(self._router("vm", port, cst_prog="").route(["nope"]), 2)
        self.assertEqual(port.said[0][1].split("\n", 1)[0], "Unknown command: nope")
        self.assertTrue(port.said[0][1].endswith("Usage: ./x-setup.sh <command> [args]\n"))
        port = _FakeRoutePort(container=True)
        self.assertEqual(self._router("vm", port).route(["backup-timer", ""]), 2)
        self.assertEqual(port.said, [("err", "Usage: cst x backup-timer on|off|status [VM]\n")])
        self.assertEqual(port.execs, [])

    def test_read_runs_the_program_or_spawns_it_on_the_host(self) -> None:
        program = ["python3", "-I", "-B", "/c/vm/cst-vm", "status", "w"]
        port = _FakeRoutePort(env={"PATH": "/p", "A": "1"})
        self._router("vm", port).route(["status", "w"])
        self.assertEqual(port.execs, [(program, {"PATH": "/p", "A": "1", "CST_PROG": "cst x"})])
        for container, detected in (("1", False), ("", True)):
            port = _FakeRoutePort(container=detected)
            self._router("vm", port, container=container).route(["status", "w"])
            self.assertEqual(port.execs, [(["flatpak-spawn", *program], {"CST_PROG": "cst x"})])
        port = _FakeRoutePort(container=True, spawn=False)
        self._router("vm", port).route(["status", "w"])
        self.assertEqual(
            port.said, [("out", "(cannot reach the host from this container: showing what is visible here)\n")]
        )
        self.assertEqual(port.execs, [(program, {"PATH": "/usr/bin", "CST_PROG": "cst x"})])
        port = _FakeRoutePort(container=True)
        self._router("vm", port, container="0").route(["status", "w"])
        self.assertEqual(port.execs[0][0], program, "the override says host")

    def test_changing_commands(self) -> None:
        port = _FakeRoutePort(container=True)
        self.assertEqual(self._router("vm", port).route(["nocow", "apply"]), 1)
        self.assertEqual(
            port.said,
            [
                (
                    "err",
                    (
                        "'cst x nocow apply' must run on the host, not inside a toolbox/container.\n"
                        "In a host terminal: cst x nocow apply\n"
                    ),
                )
            ],
        )
        port = _FakeRoutePort()
        self._router("vm", port).route(["backup", "w"])
        self.assertEqual(port.execs, [(["sudo", "--", "env", "CST_PROG=cst x", "/c/x-setup.sh", "backup", "w"], None)])
        port = _FakeRoutePort()
        self._router("vm", port, cst_prog="").route(["backup"])
        self.assertEqual(port.execs, [(["sudo", "--", "/c/x-setup.sh", "backup"], None)])
        port = _FakeRoutePort(euid=0)
        self._router("vm", port, cst_prog="").route(["backup"])
        self.assertEqual(
            port.execs,
            [(["python3", "-I", "-B", "/c/vm/cst-vm", "backup"], {"PATH": "/usr/bin", "CST_PROG": "./x-setup.sh"})],
        )
        port = _FakeRoutePort()
        self._router("vm", port).route(["nocow", "apply"])
        self.assertEqual(port.execs[0][0][:4], ["python3", "-I", "-B", "/c/vm/cst-vm"], "user: no sudo")

    def test_as_user(self) -> None:
        port = _FakeRoutePort()
        self._router("toolchains", port).route(["status"])
        self.assertEqual(
            port.execs,
            [
                (
                    ["python3", "-I", "-B", "/c/toolchains/cst-toolchains", "status"],
                    {"PATH": "/usr/bin", "CST_PROG": "cst x"},
                )
            ],
        )
        port = _FakeRoutePort(euid=0)
        self._router("toolchains", port, sudo_user="alice").route(["prune", "plan"])
        self.assertEqual(
            port.execs,
            [
                (
                    [
                        "runuser",
                        "-u",
                        "alice",
                        "--",
                        "env",
                        "-u",
                        "XDG_CONFIG_HOME",
                        "-u",
                        "SUDO_USER",
                        "HOME=/home/alice",
                        "USER=alice",
                        "LOGNAME=alice",
                        "XDG_RUNTIME_DIR=/run/user/1234",
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1234/bus",
                        "CST_PROG=cst x",
                        "/c/x-setup.sh",
                        "prune",
                        "plan",
                    ],
                    None,
                )
            ],
        )
        for user in ("", "root"):
            port = _FakeRoutePort(euid=0)
            self.assertEqual(self._router("toolchains", port, sudo_user=user).route(["status"]), 1)
            self.assertEqual(
                port.said,
                [
                    (
                        "err",
                        (
                            "cst x: ERROR: run it from your user account (needs SUDO_USER): "
                            "toolchain caches and settings belong to your user\n"
                        ),
                    )
                ],
            )
        port = _FakeRoutePort(euid=0)
        self.assertEqual(self._router("toolchains", port, sudo_user="bob").route(["status"]), 1)
        self.assertEqual(port.said, [("err", "cst x: ERROR: SUDO_USER=bob: no such user\n")])
        self.assertEqual(port.execs, [])

    def test_the_router_starts_with_ctrl_c_as_bash_had_it(self) -> None:
        for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        for before, after in ((signal.default_int_handler, signal.SIG_DFL), (signal.SIG_IGN, signal.SIG_IGN)):
            signal.signal(signal.SIGINT, before)
            with mock.patch.dict(os.environ, {"CST_LC_CTYPE": ""}):
                core_wiring.start_router()
            self.assertIs(signal.getsignal(signal.SIGINT), after)
        self.assertIs(signal.getsignal(signal.SIGPIPE), signal.SIG_DFL)

    def test_the_adapter_keeps_every_byte_and_spawns_only_with_flatpak_spawn(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-route-adapter-")
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "s")
        with open(path, "wb") as fh:
            fh.write(b"caf\xe9 \xc3\xa9\n")
        port = LocalCstDispatchAdapter()
        text = port.read_text(path)
        self.assertEqual(os.fsencode(text), b"caf\xe9 \xc3\xa9\n")
        self.assertEqual(on_host_argv(["a", "b"], {"K": "v w"}), ["flatpak-spawn", "--host", "--env=K=v w", "a", "b"])
        self.assertFalse(can_spawn_on_host(tmp))
        os.symlink("/bin/true", os.path.join(tmp, "flatpak-spawn"))
        self.assertTrue(can_spawn_on_host(tmp))
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIsNone(port.exec_on_host(["x"], settings={}))


class CoreWiringTest(unittest.TestCase):
    """cst_core/wiring.py: what cst and the installer take from the process."""

    def test_the_router_needs_the_shim_it_routes_for(self) -> None:
        # Without CST_SELF (route.py run by hand, not by vm-setup.sh or toolchains-setup.sh) there is
        # no script to print the help of or to run again: a usage error, not a traceback or ./vm/cst-vm.
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True):
            self.assertIsNone(core_wiring.module_router("vm"))
        env = {"PATH": "/usr/bin:/bin"}
        for args in (["vm", "help"], ["toolchains", "status"], ["nope"], []):
            with self.subTest(args=args):
                p = subprocess.run(
                    [sys.executable, "-I", "-B", os.path.join(REPO_ROOT, "cst_core", "route.py"), *args],
                    env=env,
                    cwd=tempfile.gettempdir(),
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                )
                self.assertEqual((p.returncode, p.stdout), (2, ""), p.stderr)
                self.assertTrue(p.stderr.startswith("usage: route.py MODULE [ARGS...]"), p.stderr)

    def test_the_installer_builds_payloads_from_this_checkout(self) -> None:
        self.assertIsNotNone(core_wiring.install_service())

    def test_the_dispatcher_takes_and_removes_what_the_wrapper_hands_over(self) -> None:
        handed = {"CST_DIR": "/nonexistent-dir", "CST_SELF": "/nonexistent-dir/cst", "CST_ARG0": "./cst"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**handed, "PATH": "/nonexistent"}, clear=True):
            dispatcher = core_wiring.cst_dispatcher(
                fallback_root="/fallback",
                stdout=io.StringIO(),
                stderr=err,
            )
            self.assertEqual(dict(os.environ), {"PATH": "/nonexistent"}, "the modules do not inherit them")
        self.assertEqual(dispatcher.dispatch(["nope"]), 2)
        self.assertTrue(err.getvalue().startswith("./cst: unknown command or module 'nope'\n"), err.getvalue())
        self.assertIn("vm         the libvirt VM", err.getvalue())
        self.assertIn("(not in this copy)", err.getvalue(), "CST_DIR names the folder of the modules")

    def test_without_the_wrapper_the_checkout_of_cli_py_and_argv0(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True),
            mock.patch.object(sys, "argv", ["/some/cst-link"]),
        ):
            dispatcher = core_wiring.cst_dispatcher(
                fallback_root="/fallback",
                stdout=out,
                stderr=io.StringIO(),
            )
        self.assertEqual(dispatcher.dispatch(["help"]), 0)
        self.assertNotIn("(not in this copy)", out.getvalue(), "this checkout holds every module")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True), mock.patch.object(sys, "argv", []):
            core_wiring.cst_dispatcher(fallback_root="/fallback", stdout=io.StringIO(), stderr=err).dispatch(["nope"])
        self.assertTrue(err.getvalue().startswith("cst: unknown command or module 'nope'\n"), err.getvalue())


class StateContractsTest(unittest.TestCase):
    """cst_core/domain/contracts.py: every shared state format, and the programs that use its paths."""

    KEYS: Final = (
        StateFormatKey.ALERT_SPOOL,
        StateFormatKey.STALL_RECORDS,
        StateFormatKey.STALL_QUEUE,
        StateFormatKey.BULK_JOURNAL,
        StateFormatKey.DOCKER_GC_STATE,
        StateFormatKey.DOCKER_GC_LAST_RUN,
        StateFormatKey.VM_INDEX,
        StateFormatKey.VM_NAS_TORN,
        StateFormatKey.INSTALL_RECORD,
        StateFormatKey.GAME_STATE,
        StateFormatKey.GAME_INTENDED,
        StateFormatKey.GAME_LEFTOVERS,
        StateFormatKey.DOCKER_NOCOW_OUTCOME,
        StateFormatKey.DOCKER_NOCOW_POSTPONED,
    )

    def test_every_format_names_real_writers_and_readers(self) -> None:
        for key in self.KEYS:
            f = state_format(key)
            with self.subTest(key):
                self.assertEqual(f.key, key)
                self.assertTrue(f.path.startswith("/") and f.folder and f.file_name, f.path)
                self.assertGreaterEqual(f.version, 0)
                self.assertTrue(f.writers and f.readers)
                for rel in f.writers + f.readers:
                    self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, rel)), rel)

    def test_first_line_version(self) -> None:
        self.assertEqual(first_line_version("cst-game-intended 1\nprop a\n", "cst-game-intended"), 1)
        self.assertEqual(first_line_version("cst-game-intended 12", "cst-game-intended"), 12)
        for text in ("", "prop sys a b c d\n", "cst-alert 1\n", "cst-game-intendedx 1\n"):
            with self.subTest(text=text):
                self.assertEqual(first_line_version(text, "cst-game-intended"), 0)
        for text in (
            "cst-game-intended\n",
            "cst-game-intended x\n",
            "cst-game-intended -1\n",
            "cst-game-intended 1 2\n",
            "cst-game-intended \u00b2\n",  # a digit for str.isdigit, not for int()
            "cst-game-intended \u0661\n",  # an Arabic-Indic one: int() reads it, the format does not
            "cst-game-intended " + "9" * 5000 + "\n",  # more digits than int() converts: ValueError
        ):
            with self.subTest(text=text):
                self.assertIsNone(first_line_version(text, "cst-game-intended"))
        self.assertEqual(state_format(StateFormatKey.GAME_INTENDED).version, 1)
        self.assertEqual(state_format(StateFormatKey.GAME_LEFTOVERS).version, 2)

    def test_a_version_is_a_whole_number_and_none_is_version_0(self) -> None:
        self.assertEqual(
            [version_of({}, "v"), version_of({"v": 3}, "v"), version_of({"version": 1}, "version")], [0, 3, 1]
        )
        for bad in ({"v": True}, {"v": "1"}, {"v": -1}, {"v": 1.0}, [], "text", None):
            with self.subTest(bad=bad):
                self.assertIsNone(version_of(bad, "v"))
        self.assertEqual(
            state_format(StateFormatKey.VM_INDEX).newer_text("/x.json", 2),
            "/x.json is format 2 of the vm-index (this CST reads format 1 and older): written by a newer CST",
        )

    def test_the_programs_use_the_registered_paths(self) -> None:
        from btrfs.bulk.domain import BulkPaths
        from btrfs.docker_gc.domain import GcPaths
        from btrfs.docker_volumes_nocow.domain import NocowPaths
        from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
        from btrfs.stall_monitord.domain import VERSION
        from btrfs.stall_report.adapters import LOG_DIR
        from cpu.domain import GAME_STATE
        from cpu.game.domain import GamePaths
        from cst_core.domain.record import ROOT_RECORD
        from doctor.domain import ALERT_PENDING, GAME_INTENDED, NOCOW_POSTPONED, VM_INDEX, VM_TORN

        self.assertEqual(GcPaths().last_used, state_format(StateFormatKey.DOCKER_GC_STATE).path)
        self.assertEqual(GcPaths().last_run, state_format(StateFormatKey.DOCKER_GC_LAST_RUN).path)
        self.assertEqual(BulkPaths().state, state_format(StateFormatKey.BULK_JOURNAL).folder)
        journal = state_format(StateFormatKey.BULK_JOURNAL)
        self.assertEqual(SnapshotRun({}, SubprocessCommandRunner()).bulk_journals, journal.path)
        self.assertEqual(
            (VERSION, LOG_DIR),
            (state_format(StateFormatKey.STALL_RECORDS).version, state_format(StateFormatKey.STALL_RECORDS).folder),
        )
        self.assertEqual(GAME_STATE, state_format(StateFormatKey.GAME_STATE).path)
        self.assertEqual(GAME_INTENDED, state_format(StateFormatKey.GAME_INTENDED).path)
        self.assertEqual(GamePaths().leftovers, state_format(StateFormatKey.GAME_LEFTOVERS).path)
        self.assertEqual(ALERT_PENDING, state_format(StateFormatKey.ALERT_SPOOL).folder)
        self.assertEqual(VM_INDEX, state_format(StateFormatKey.VM_INDEX).path)
        self.assertEqual(VM_TORN, state_format(StateFormatKey.VM_NAS_TORN).path)
        self.assertEqual(ROOT_RECORD, state_format(StateFormatKey.INSTALL_RECORD).path)
        self.assertEqual(NocowPaths().outcome, state_format(StateFormatKey.DOCKER_NOCOW_OUTCOME).path)
        postponed = state_format(StateFormatKey.DOCKER_NOCOW_POSTPONED)
        self.assertEqual((NocowPaths().postponed, NOCOW_POSTPONED), (postponed.path, postponed.path))
        self.assertEqual(postponed.version, 1)

    def test_paths_of_installed_programs_come_from_the_manifest(self) -> None:
        from btrfs.bulk.domain import BulkPaths
        from btrfs.docker_volumes_nocow.domain import NocowPaths
        from doctor.domain import GAME_PROFILE_HOST, HEALTH_CHECK_HOST, NOTIFY_HOST
        from toolchains.domain.common import DEFAULTS_REL, PROG_REL
        from toolchains.domain.config import WRAPPER_REL

        dest = {e.src: e.dest for e in INSTALL_MANIFEST}
        notify = dest["btrfs/btrfs-notify"]
        self.assertEqual(notify, "/usr/local/sbin/btrfs-notify")
        self.assertEqual((BulkPaths().notify, NocowPaths().notify, NOTIFY_HOST), (notify, notify, notify))
        self.assertEqual(HEALTH_CHECK_HOST, dest["btrfs/btrfs-health-check"])
        self.assertEqual(GAME_PROFILE_HOST, dest["cpu/game-profile"])
        self.assertEqual(
            ("~/" + PROG_REL, "~/" + DEFAULTS_REL, "~/" + WRAPPER_REL),
            (dest["toolchains/cst-toolchains"], dest["toolchains/toolchains.conf"], dest["toolchains/rustc-wrapper"]),
        )


class CstPerfDispatchTest(unittest.TestCase):
    """`cst perf` dispatches cleanly like `cst doctor` and steps down under sudo."""

    def _run(self, port: _FakeDispatchPort, args: Sequence[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        dispatcher = CstDispatcher(
            port=port, repo_dir="/repo", self_path="/repo/cst", arg0="./cst", stdout=out, stderr=err
        )
        return dispatcher.dispatch(args), out.getvalue(), err.getvalue()

    def test_perf_usage_mentions_cst_perf(self) -> None:
        rc, stdout, _ = self._run(_FakeDispatchPort(), ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("cst perf", stdout)

    def test_perf_missing_entry_returns_one(self) -> None:
        rc, _, stderr = self._run(_FakeDispatchPort(), ["perf"])
        self.assertEqual(rc, 1)
        self.assertIn("perf is not in this copy", stderr)

    def test_perf_as_user_and_sudo_step_down(self) -> None:
        port = _FakeDispatchPort(files={"/repo/perf/cst-perf"}, uid=1000, env={"CST_NAME": "cst", "KEEP": "1"})
        rc, _, _ = self._run(port, ["perf", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(port.calls[0][0], ["python3", "-I", "-B", "/repo/perf/cst-perf", "--json"])
        self.assertEqual((port.calls[0][1] or {}).get("CST_PROG"), "cst perf")

        sudo_port = _FakeDispatchPort(
            files={"/repo/perf/cst-perf"},
            uid=0,
            env={"CST_NAME": "cst", "SUDO_USER": "alice"},
            passwd={"alice": (1000, "/home/alice")},
        )
        rc_sudo, _, _ = self._run(sudo_port, ["perf", "--compact"])
        self.assertEqual(rc_sudo, 0)
        argv = sudo_port.calls[0][0]
        self.assertIn("/repo/perf/cst-perf", argv)
        self.assertIn("--compact", argv)
        self.assertIn("CST_PROG=cst perf", argv)


class SubcommandHeadTest(unittest.TestCase):
    def test_parse_subcommand_head_handles_default_help_member_and_unknown(self) -> None:
        from enum import StrEnum
        from cst_core.domain.text import parse_subcommand_head

        class Act(StrEnum):
            STATUS = "status"
            APPLY = "apply"
            HELP = "help"

        self.assertEqual(
            parse_subcommand_head([], Act, default=Act.STATUS, help_action=Act.HELP), (Act.STATUS, (), None)
        )
        self.assertEqual(
            parse_subcommand_head(["--help", "x"], Act, default=Act.STATUS, help_action=Act.HELP),
            (Act.HELP, ["x"], None),
        )
        self.assertEqual(
            parse_subcommand_head(["apply", "--yes"], Act, default=Act.STATUS, help_action=Act.HELP),
            (Act.APPLY, ["--yes"], None),
        )
        act, rest, err = parse_subcommand_head(["bogus"], Act, default=Act.STATUS, help_action=Act.HELP)
        self.assertEqual((act, rest), (Act.STATUS, ()))
        self.assertIn("unknown subcommand", err or "")


if __name__ == "__main__":
    unittest.main()
