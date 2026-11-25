# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst swap apply`: its refusals and its idempotence, over drawn machines (Hypothesis).

It runs the real SwapApplicationService (swap/application/apply.py) through the command line's
run_command on the unit tests' scratch (swap/tests/test_swap.py swap_scratch: /etc, /proc, /sys and
the cgroup tree in a folder of its own) with their fakes: SwapTempFileSystem (the real file system
adapter, with the partition's and the mapper's block devices), SwapFakeSystemd (start and stop of
swap0 and zram0 change /proc/swaps), FakeSwapSystem (blockdev, blkid, modprobe, sysctl, zramctl,
swapon) and the real installer (swap_installer) with an install record in the scratch. os.fsync is
stubbed (the TestCase's setUp).

The machine at the start, and each part a rule changes between two applies (a hand edit, an older
CST's file, a reboot):
- /etc/cst/swap.conf: the partition; a bad value; a folder; absent, with an older CST's marked
  crypttab line (apply writes it: resolve_spec's "write"); absent with no swap0 line at all.
- /etc/crypttab and /etc/fstab, each: absent; CST's line; an older CST's marker; the line twice;
  other options; a swap0 line written by hand (unmarked); a folder; with unrelated lines.
- each of the five files apply writes whole (MANAGED: zswap.conf, the sysctl file, the mm file,
  uresourced.conf, the machine.slice drop-in): absent, CST's, someone else's, a folder in its place.
- zswap built in or not, zstd in /proc/crypto or not, the partition's size right or not, its
  signature (none, swap, ext4), the mapper there or not, zram-generator.conf absent, masked or a file,
  zram0 in use or not, the cpu experiment wsf-300 on or off, the install record absent or of a newer
  CST.

The expected outcome of an apply comes from those parts (an oracle over the files, not the code):
1. A refusal (no partition, a bad or unreadable settings file, no zswap, no zstd, the wrong size,
   a filesystem signature on a partition with no mapper, an unmarked swap0 line, a crypttab or
   fstab apply cannot read, a folder in place of a file apply writes whole, an install record of a
   newer CST): exit 1, an ERROR line, and the machine as it was: every file of the scratch tree
   byte for byte, no systemctl call, no daemon-reload, no sysctl or zram call.
   modprobe is left out on purpose: the checks load the compressor module before they can tell
   whether zstd exists (bash parity).
2. Otherwise exit 0 with no ERROR line and no zswap parameter skipped (a kernel with zswap has all
   four the scratch has; the kernel rule puts them back when zswap comes back), and an apply right
   after changes no file, starts, stops or restarts no unit, reloads nothing and exits 0.
3. After an apply that exits 0, the install record lists swap's five files with the sha256 of what is
   in place, and swap:core is done.
An install record of a newer CST was gap O1 (apply wrote crypttab and fstab and started swap0
before its first put refused); apply now checks the record before its first write, so it is a
refusal of point 1. A folder in place of a file apply writes whole was a gap too (apply wrote
crypttab and fstab, started swap0 and ran the steps before that file's, then failed there); apply
now checks those files before its first write, so it is a refusal of point 1 as well.

Not modelled: a non-root run (plan's "?" rows), a file apply writes whole that it may not read
(mode 0: the machine may run as root, and file_tree reads every file), a systemctl call that
fails, a sysctl that fails, uresourced's restart, the old drop-in name (the unit tests cover them).
"""

import dataclasses
from enum import StrEnum
import hashlib
import io
import json
import os
import shutil
import tempfile
from typing import Final, override
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from swap.application.apply import SwapApplicationService
from swap.application.reports import DROPIN_ENTRY, SYSCTL_ENTRY, TMPFILES_ENTRY
from swap.application.memory_tuning import MM_TMPFILES_ENTRY, URESOURCED_ENTRY
from swap.domain import SwapCommand
from swap.presenter import run_command
from swap.tests.test_swap import (
    NEW,
    OLD,
    OLD_CRYPTTAB,
    OLD_FSTAB,
    TEST_SETTINGS,
    TEST_SPEC,
    FakeSwapSystem,
    SwapFakeSystemd,
    SwapTempFileSystem,
    file_tree,
    swap_installer,
    swap_scratch,
    write_zswap_params,
)
from tests.fault_injection import stub_fsync
from tests.mbt import profiles

profiles.load()

MANAGED: Final = (TMPFILES_ENTRY, SYSCTL_ENTRY, MM_TMPFILES_ENTRY, URESOURCED_ENTRY, DROPIN_ENTRY)
NEWER_RECORD: Final = '{"format": 99, "files": {}}'
BAD_SETTINGS: Final = "SWAP_PARTUUID=not-a-partuuid\nSWAP_SIZE_BYTES=1\n"
CRYPT_KEY: Final = "swap0"
FSTAB_KEY: Final = "/dev/mapper/swap0"
SPEC = TEST_SPEC
CST_CRYPTTAB: Final = SPEC.crypttab_spec().render_updated("").strip("\n")
CST_FSTAB: Final = SPEC.fstab_spec().render_updated("").strip("\n")
SWAPS_HEADER: Final = "Filename\tType\tSize\tUsed\tPriority"


class LineKind(StrEnum):
    """What one of /etc/crypttab and /etc/fstab holds for swap0."""

    ABSENT = "absent"  # no file
    NONE = "none"  # only unrelated lines
    CST = "cst"
    OLDER = "older"  # an older CST's marker (adopt)
    TWICE = "twice"  # dedupe
    OPTIONS = "options"  # other options (change)
    HAND = "hand"  # an unmarked swap0 line (refused)
    FOLDER = "folder"  # unreadable (refused)


class ManagedKind(StrEnum):
    """What is at one of the files apply writes whole."""

    ABSENT = "absent"
    CST = "cst"  # the source's text
    FOREIGN = "foreign"  # someone else's (apply copies uresourced.conf aside, then writes CST's)
    FOLDER = "folder"  # unreadable (refused)


class SettingsKind(StrEnum):
    """/etc/cst/swap.conf."""

    OK = "ok"
    BAD = "bad"
    FOLDER = "folder"
    WRITE = "write"  # absent, an older CST's crypttab line
    NOTHING = "nothing"  # absent, no swap0 line anywhere


# A part that makes apply refuse is drawn less often than the others, so that most applies get past the
# checks.
MOSTLY_TRUE: Final = st.sampled_from((True, True, True, False))
SETTINGS_KINDS: Final = st.sampled_from((SettingsKind.OK, SettingsKind.OK, SettingsKind.OK, *SettingsKind))
LINE_KINDS: Final = st.sampled_from((*LineKind, LineKind.ABSENT, LineKind.CST, LineKind.CST, LineKind.OLDER))
MANAGED_KINDS: Final = st.sampled_from((*ManagedKind, ManagedKind.CST, ManagedKind.CST, ManagedKind.FOREIGN))


def line_text(path_key: str, kind: LineKind) -> str | None:
    """The text of crypttab (path_key swap0) or fstab for kind; None for no file (or a folder)."""
    crypt = path_key == CRYPT_KEY
    cst, older = (CST_CRYPTTAB, OLD_CRYPTTAB) if crypt else (CST_FSTAB, OLD_FSTAB)
    other = "other /dev/sdz none" if crypt else "UUID=1 / btrfs defaults 0 0"
    hand = "swap0 /dev/nvme0n1p5 none luks" if crypt else "/dev/mapper/swap0 none swap defaults 0 0"
    options = cst.replace(",discard", "") if crypt else cst.replace("defaults,nofail,discard", "defaults")
    assert options != cst, "the changed options must differ from CST's"
    match kind:
        case LineKind.ABSENT | LineKind.FOLDER:
            return None
        case LineKind.NONE:
            lines = [other]
        case LineKind.CST:
            lines = [other, cst]
        case LineKind.OLDER:
            lines = [other, older]
        case LineKind.TWICE:
            lines = [cst, other, cst]
        case LineKind.OPTIONS:
            lines = [other, options]
        case LineKind.HAND:
            lines = [other, hand]
    return "".join(f"{x}\n" for x in lines)


def managed_text(src: str, kind: ManagedKind) -> str | None:
    """The text of a file apply writes whole for kind (src: its source in swap/); None for no file (or a
    folder)."""
    match kind:
        case ManagedKind.ABSENT | ManagedKind.FOLDER:
            return None
        case ManagedKind.CST:
            with open(src, encoding="utf-8") as fh:
                return fh.read()
        case ManagedKind.FOREIGN:
            return "# mine\n"


def is_key_line(line: str, key: str) -> bool:
    """The line of crypttab or fstab is one for key (its first field)."""
    return line.split()[:1] == [key]


def sha256_of(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class StartDraw:
    """The machine at the start (the initialize rule's one draw)."""

    settings: SettingsKind
    crypttab: LineKind
    fstab: LineKind
    zram_conf: str
    mapper: bool
    zram_in_use: bool
    wsf: bool
    foreign: bool
    folder: int | None  # the index in MANAGED of a file that is a folder
    newer: bool


class SwapApplyMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-mbt-swap-"))
        record = os.path.join(self.tmp, "var", "lib", "cst", "installed.json")
        self.env = dataclasses.replace(swap_scratch(self.tmp, SPEC), install_record=record)
        self.size_ok = True
        self.signature = ""
        self.crypto = True
        self.zswap = True
        self.fs = self._new_fs(mapper=False)
        self.systemd = SwapFakeSystemd(self.swaps, "/dev/dm-0", fs=self.fs)

    @override
    def teardown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------------------------
    @property
    def swaps(self) -> str:
        return os.path.join(self.env.proc_dir, "swaps")

    def _new_fs(self, *, mapper: bool) -> SwapTempFileSystem:
        links = {SPEC.device_path: "/dev/nvme0n1p5"}
        if mapper:
            links[SPEC.mapper_path] = "/dev/dm-0"
        return SwapTempFileSystem(links)

    def _put(self, path: str, text: str | None, *, folder: bool = False) -> None:
        """path holds text (None: no file; folder: a folder in its place)."""
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.unlink(path)
        if folder:
            os.makedirs(path)
        elif text is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)

    def _boot(self, *, mapper: bool, zram: bool) -> None:
        """A boot: swap0's mapper there or not (and open in /proc/swaps), zram0 in use or not."""
        self.fs = self._new_fs(mapper=mapper)
        self.systemd = SwapFakeSystemd(self.swaps, "/dev/dm-0", fs=self.fs)
        rows = [SWAPS_HEADER]
        if mapper:
            rows.append("/dev/dm-0 partition 134217724 0 -2")
        if zram:
            rows.append("/dev/zram0 partition 8388604 102400 100")
        self._put(self.swaps, "".join(f"{r}\n" for r in rows))

    def _put_line(self, path: str, key: str, kind: LineKind) -> None:
        self._put(path, line_text(key, kind), folder=kind == LineKind.FOLDER)

    def _set_settings(self, kind: SettingsKind) -> None:
        env = self.env
        match kind:
            case SettingsKind.OK:
                self._put(env.settings_path, TEST_SETTINGS)
            case SettingsKind.BAD:
                self._put(env.settings_path, BAD_SETTINGS)
            case SettingsKind.FOLDER:
                self._put(env.settings_path, None, folder=True)
            case SettingsKind.WRITE:
                self._put(env.settings_path, None)
                self._put_line(env.crypttab, CRYPT_KEY, LineKind.OLDER)
            case SettingsKind.NOTHING:
                self._put(env.settings_path, None)
                for path, key in ((env.crypttab, CRYPT_KEY), (env.fstab, FSTAB_KEY)):
                    if self._has_swap0_line(path, key):
                        self._put_line(path, key, LineKind.NONE)

    @staticmethod
    def _lines(path: str) -> list[str] | None:
        """path's lines; None for a folder; [] for no file."""
        if os.path.isdir(path):
            return None
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def _has_swap0_line(self, path: str, key: str) -> bool:
        lines = self._lines(path)
        return lines is not None and any(is_key_line(line, key) for line in lines)

    def refusal(self) -> str | None:
        """Why apply must refuse this machine (the oracle), None when it must not."""
        newer = "an install record of a newer CST" if self.newer_record() else None
        return (
            self._settings_refusal()
            or self._probe_refusal()
            or self._lines_refusal()
            or self._managed_refusal()
            or newer
        )

    def _settings_refusal(self) -> str | None:
        env = self.env
        if os.path.isdir(env.settings_path):
            return "settings folder"
        if not os.path.exists(env.settings_path):
            crypt = self._lines(env.crypttab) or []
            if not any(is_key_line(line, CRYPT_KEY) and (OLD in line or NEW in line) for line in crypt):
                return "no partition configured"
            return None
        with open(env.settings_path, encoding="utf-8") as fh:
            return "bad settings" if fh.read() == BAD_SETTINGS else None

    def _probe_refusal(self) -> str | None:
        if not self.zswap:
            return "no zswap"
        if not self.crypto:
            return "no zstd"
        if not self.size_ok:
            return "wrong size"
        if self.signature == "ext4" and not self.fs.is_block_device(SPEC.mapper_path):
            return "a filesystem signature"
        return None

    def _lines_refusal(self) -> str | None:
        for path, key in ((self.env.crypttab, CRYPT_KEY), (self.env.fstab, FSTAB_KEY)):
            lines = self._lines(path)
            if lines is None:
                return f"{path} unreadable"
            if any(is_key_line(line, key) and "managed-by:" not in line for line in lines):
                return f"{path}: an unmarked swap0 line"
        return None

    def _managed_refusal(self) -> str | None:
        for entry in MANAGED:
            path = self.env.etc_path(entry)
            if os.path.isdir(path) and not os.path.islink(path):
                return f"{path} a folder"
        return None

    def newer_record(self) -> bool:
        if not os.path.exists(self.env.install_record):
            return False
        with open(self.env.install_record, encoding="utf-8") as fh:
            return fh.read() == NEWER_RECORD

    def apply(self) -> tuple[int, str, str, FakeSwapSystem]:
        self.systemd.calls, self.systemd.daemon_reloads = [], 0
        size = SPEC.expected_size_bytes if self.size_ok else SPEC.expected_size_bytes + 4096
        system = FakeSwapSystem(self.env.proc_dir, size_bytes=size, part_type=self.signature)
        service = SwapApplicationService(self.env, self.fs, self.systemd, system, installer=swap_installer())
        out, err = io.StringIO(), io.StringIO()
        rc = run_command(SwapCommand.APPLY, service, hint="", out=out, err=err)
        return rc, out.getvalue(), err.getvalue(), system

    def assert_record(self) -> None:
        """Point 3 of the docstring."""
        with open(self.env.install_record, encoding="utf-8") as fh:
            rec = json.load(fh)
        want = {e.dest: sha256_of(self.env.etc_path(e)) for e in MANAGED}
        got = {d: f["sha256"] for d, f in rec["files"].items()}
        assert got == want, f"the install record lists {got}, not {want}"
        assert rec["components"] == {"swap:core": "done"}, rec["components"]

    # ---- rules -------------------------------------------------------------------------------
    @initialize(
        start=st.builds(
            StartDraw,
            settings=SETTINGS_KINDS,
            crypttab=LINE_KINDS,
            fstab=LINE_KINDS,
            mapper=st.booleans(),
            zram_in_use=st.booleans(),
            zram_conf=st.sampled_from(("absent", "masked", "file")),
            wsf=st.booleans(),
            foreign=st.booleans(),
            folder=st.sampled_from((*(None,) * 10, *range(len(MANAGED)))),
            newer=st.booleans(),
        )
    )
    def start(self, start: StartDraw) -> None:
        env = self.env
        self._put_line(env.crypttab, CRYPT_KEY, start.crypttab)
        self._put_line(env.fstab, FSTAB_KEY, start.fstab)
        self._set_settings(start.settings)
        self._boot(mapper=start.mapper, zram=start.zram_in_use)
        match start.zram_conf:
            case "masked":
                os.makedirs(os.path.dirname(env.zram_conf), exist_ok=True)
                os.symlink("/dev/null", env.zram_conf)
            case "file":
                self._put(env.zram_conf, "[zram0]\nzram-size = ram / 2\n")
            case _:
                pass
        if start.wsf:
            self._put(os.path.join(env.cpu_run_dir, "exp-wsf-300"), "")
        if start.foreign:  # someone else's copy of swap's files
            for entry in MANAGED:
                self._put(env.etc_path(entry), "# mine\n")
        if start.folder is not None:
            self._put(env.etc_path(MANAGED[start.folder]), None, folder=True)
        if start.newer:
            self._put(env.install_record, NEWER_RECORD)

    @rule(crypttab=st.booleans(), kind=LINE_KINDS)
    def edit_line(self, kind: LineKind, *, crypttab: bool) -> None:
        """A hand edit (or an older CST's script) of crypttab or fstab."""
        env = self.env
        self._put_line(env.crypttab if crypttab else env.fstab, CRYPT_KEY if crypttab else FSTAB_KEY, kind)

    @rule(index=st.integers(0, len(MANAGED) - 1), kind=MANAGED_KINDS)
    def managed_file(self, index: int, kind: ManagedKind) -> None:
        """A hand edit of one of the files apply writes whole: removed, CST's, someone else's, a folder."""
        entry = MANAGED[index]
        text = managed_text(self.env.src_path(entry), kind)
        self._put(self.env.etc_path(entry), text, folder=kind == ManagedKind.FOLDER)

    @rule(kind=SETTINGS_KINDS)
    def settings(self, kind: SettingsKind) -> None:
        self._set_settings(kind)

    @rule(
        zswap=MOSTLY_TRUE,
        crypto=MOSTLY_TRUE,
        size_ok=MOSTLY_TRUE,
        signature=st.sampled_from(("", "", "swap", "ext4")),
    )
    def kernel(self, signature: str, *, zswap: bool, crypto: bool, size_ok: bool) -> None:
        """Another kernel, another disk: zswap, zstd, the partition's size and signature."""
        params = self.env.zswap_params_dir
        if zswap and not os.path.isdir(params):
            write_zswap_params(params)  # its four parameters, as at a boot
        elif not zswap and os.path.isdir(params):
            shutil.rmtree(params)
        self._put(
            os.path.join(self.env.proc_dir, "crypto"),
            "name         : zstd\ndriver       : zstd-generic\n" if crypto else "name         : lz4\n",
        )
        self.zswap, self.crypto, self.size_ok, self.signature = zswap, crypto, size_ok, signature

    @rule(zram=st.booleans())
    def reboot(self, *, zram: bool) -> None:
        """swap0 closed, the mapper gone, zram0 back when zram-generator.conf is not masked."""
        self._boot(mapper=False, zram=zram and not os.path.islink(self.env.zram_conf))

    @rule(newer=st.booleans())
    def record(self, *, newer: bool) -> None:
        """The install record of a newer CST (a downgrade), or none."""
        self._put(self.env.install_record, NEWER_RECORD if newer else None)

    @rule()
    def apply_once(self) -> None:
        why = self.refusal()
        before = file_tree(self.tmp)
        rc, out, err, system = self.apply()
        if why is not None:
            assert rc == 1, f"refusal ({why}) exited {rc}: {out}{err}"
            assert "ERROR:" in err, f"refusal ({why}) with no ERROR line: {err}"
            assert file_tree(self.tmp) == before, f"refusal ({why}) changed files: {out}{err}"
            calls = (self.systemd.calls, self.systemd.daemon_reloads, system.sysctl_calls, system.zram_resets)
            assert calls == ([], 0, [], 0), f"refusal ({why}) ran {calls}"
            return
        assert (rc, "ERROR:" in err) == (0, False), f"apply exited {rc}: {out}{err}"
        assert "skipped:   zswap" not in out, f"a kernel with zswap lacks a parameter: {out}"
        self.assert_record()
        after = file_tree(self.tmp)
        rc, out, err, system = self.apply()
        assert rc == 0, f"apply again exited {rc}: {out}{err}"
        assert file_tree(self.tmp) == after, f"apply again changed files: {out}"
        verbs = [verb for verb, _unit in self.systemd.calls]
        reloads = self.systemd.daemon_reloads
        assert (verbs, reloads) == ([], 0), f"apply again ran {self.systemd.calls} and {reloads} daemon-reloads"


class SwapApplyMachineTest(profiles.MbtTestCase):
    # Not `SwapApplyMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where
    # run_unit's workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_refusals_change_nothing_and_a_second_apply_nothing_more(self) -> None:
        profiles.run_machine(SwapApplyMachine)


if __name__ == "__main__":
    unittest.main()
