# SPDX-License-Identifier: MPL-2.0
"""The swap area of `cst doctor`: the zswap parameters swap's zswap.conf sets, and the partition settings."""

from collections.abc import Iterator
import stat
from typing import Final

from cst_core.domain.values import format_bytes_human as human
from doctor.application.cpu import unit_props
from doctor.application.shared import check, read_text
from doctor.domain import (
    CRYPTTAB,
    FSTAB,
    MM_RESPONSIVENESS_CONF,
    HostPath,
    INFO,
    OK,
    SWAP_CONF,
    SWAP_DROPIN,
    WARN,
    ZSWAP_CONF,
    ZSWAP_PARAMS,
    Result,
    ViewPath,
)
from doctor.ports import CheckFn, DoctorHostPort
from perf.domain.parsers import parse_sysfs_tmpfiles_writes
from swap.domain import (
    SwapSpecState,
    human_size,
    marked_fstab_name,
    memory_low_override,
    parse_memory_low,
    parse_size,
    parse_tmpfiles_params,
    resolve_settings,
    sysfs_partition,
)

# How much of swap.conf, /etc/crypttab and /etc/fstab doctor reads, as of every settings file it reads:
# a file that never ends (a link to /dev/zero) must not fill the memory.
SETTINGS_READ_LIMIT: Final = 65536

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


@check(CHECKS, "swap")
def check_zswap_params(host: DoctorHostPort) -> Iterator[Result]:
    """WARN for each zswap parameter that swap's zswap.conf sets and this
    kernel does not have: systemd-tmpfiles skips it at boot without a word
    (a new kernel can rename or drop one). Nothing when that file is not
    installed."""
    try:
        with host.open_regular_text(host.path(ZSWAP_CONF)) as f:
            wanted = [name for name, _ in parse_tmpfiles_params(f.read(65536))]
    except FileNotFoundError:
        return
    except OSError:
        yield Result(INFO, f"zswap: cannot read {ZSWAP_CONF}")
        return
    if not wanted:
        return
    try:
        have = set(host.listdir(host.path(ZSWAP_PARAMS)))
    except OSError:
        yield Result(WARN, f"zswap: not in this kernel (no {ZSWAP_PARAMS}): {ZSWAP_CONF} sets nothing")
        return
    missing = [name for name in wanted if name not in have]
    for name in missing:
        yield Result(
            WARN,
            f"zswap {name}: this kernel has no such parameter; {ZSWAP_CONF} sets it, and systemd-tmpfiles "
            "skips it at boot without a word (cst swap status shows the zswap settings)",
        )
    if not missing:
        yield Result(OK, f"zswap: every parameter {ZSWAP_CONF} sets is in this kernel")


@check(CHECKS, "swap")
def check_mm_responsiveness_params(host: DoctorHostPort) -> Iterator[Result]:
    """WARN for each sysfs memory knob cst-mm-responsiveness.conf sets that this kernel lacks."""
    try:
        with host.open_text(host.path(MM_RESPONSIVENESS_CONF)) as f:
            wanted = [path for path, _ in parse_sysfs_tmpfiles_writes(f.read(SETTINGS_READ_LIMIT))]
    except FileNotFoundError:
        return
    except OSError:
        yield Result(INFO, f"mm responsiveness: cannot read {MM_RESPONSIVENESS_CONF}")
        return
    if not wanted:
        return
    missing = [sysfs_path for sysfs_path in wanted if not host.exists(host.path(HostPath(sysfs_path)))]
    for sysfs_path in missing:
        yield Result(
            WARN,
            f"mm responsiveness {sysfs_path}: this kernel lacks this knob; {MM_RESPONSIVENESS_CONF} "
            "skips it at boot without a word",
        )
    if not missing:
        yield Result(OK, f"mm responsiveness: every sysfs knob {MM_RESPONSIVENESS_CONF} sets is in this kernel")


class _HostSettingsFacts:
    """swap.ports' SwapSettingsFacts over doctor's view of the host, so that doctor reads
    /etc/cst/swap.conf as cst swap reads it. Each file is read, and swap.conf's lstat asked, once,
    when an answer first needs it: no answer depends on which were asked before it."""

    def __init__(self, host: DoctorHostPort) -> None:
        self._host = host
        self._view = host.path(SWAP_CONF)
        # What reading each file gave: its text, or why it could not be read (no key: not read yet).
        self._reads: dict[HostPath, str | OSError] = {}
        # Why swap.conf's lstat failed, boxed (None: not asked yet; (None,): it did not fail).
        self._lstat: tuple[OSError | None] | None = None

    def _read(self, path: HostPath) -> str | OSError:
        """The file's first SETTINGS_READ_LIMIT characters (undecodable bytes replaced), or the OSError of
        its read; read once. Anything but a regular file (a FIFO, a device, or a link to one) is one it
        cannot read, opened without waiting, as cst swap's readers take it (regular_only)."""
        known = self._reads.get(path)
        if known is None:
            try:
                with self._host.open_regular_text(self._host.path(path)) as f:
                    known = f.read(SETTINGS_READ_LIMIT)
            except OSError as e:
                known = e
            self._reads[path] = known
        return known

    def _text(self, path: HostPath) -> str:
        """The file's text as _read gives it; "" when it cannot be read."""
        text = self._read(path)
        return text if isinstance(text, str) else ""

    def _lstat_error(self) -> OSError | None:
        """Why swap.conf's lstat failed; None when it did not."""
        if self._lstat is None:
            try:
                self._host.lstat(self._view)
                self._lstat = (None,)
            except OSError as e:
                self._lstat = (e,)
        return self._lstat[0]

    def settings_text(self) -> str | None:
        text = self._read(SWAP_CONF)
        return text if isinstance(text, str) else None

    def settings_seen(self) -> bool:
        return self._lstat_error() is None

    def settings_is_file(self) -> bool:
        return self._host.isfile(self._view)

    def settings_readable(self) -> bool:
        # doctor has no access(2): the read is the answer, and only a refused one (EACCES, EPERM)
        # says this user may not read it; any other failure (EIO, ...) is one --root would not help.
        return not isinstance(self._read(SWAP_CONF), PermissionError)

    def settings_folder_closed(self) -> bool:
        # lstat cannot see into a folder this user may not search: EACCES, where a missing file is ENOENT.
        return isinstance(self._lstat_error(), PermissionError)

    def crypttab_closed(self) -> bool:
        return isinstance(self._read(CRYPTTAB), PermissionError)

    def crypttab_not_regular(self) -> bool:
        view = self._host.path(CRYPTTAB)
        try:
            self._host.lstat(view)
        except OSError:
            return False
        return not self._host.isfile(view)

    def crypttab_text(self) -> str:
        return self._text(CRYPTTAB)

    def fstab_text(self) -> str:
        return self._text(FSTAB)

    def partition_size(self, device_path: str) -> int | None:
        part = sysfs_partition(
            device_path, resolve=self._resolve, is_block=self._is_block, first_line=self._first_line, sys_dir="/sys"
        )
        return None if part is None else part[1]

    def _resolve(self, path: str) -> str | None:
        """A host path with every link followed, in this process's view; None when it cannot be."""
        try:
            return self._host.realpath(self._host.path(HostPath(path)))
        except OSError:
            return None

    def _is_block(self, view: str) -> bool:
        try:
            return stat.S_ISBLK(self._host.stat(ViewPath(view)).st_mode)
        except OSError:
            return False

    def _first_line(self, path: str) -> str:
        """A host file's first line, stripped ("" when it cannot be read)."""
        return read_text(self._host, self._host.path(HostPath(path)))


@check(CHECKS, "swap")
def check_swap_settings(host: DoctorHostPort) -> Iterator[Result]:
    """The swap partition in /etc/cst/swap.conf, resolved as cst swap resolves it (swap.domain's
    resolve_settings): ok when it names a partition; WARN for each problem, or for a file that is not
    a regular file (cst swap apply refuses); info when this user may not read it (cst swap status --root
    can); WARN when it is missing where an older CST set swap up and apply can write it from
    /etc/crypttab; WARN "not configured" where swap is set up without one. Nothing where swap is not
    set up."""
    facts = _HostSettingsFacts(host)
    # Root in a toolbox is root of its user namespace only: a host file it may not read is one
    # cst swap status --root reads on the host (cst swap's root commands refuse in a toolbox).
    res = resolve_settings(facts, as_root=host.uid == 0 and not host.container)
    match res.state:
        case SwapSpecState.OK if res.spec is not None:
            spec = res.spec
            yield Result(OK, f"swap partition: {spec.partuuid} ({spec.expected_size_bytes} bytes, {SWAP_CONF})")
        case SwapSpecState.BAD:
            for problem in res.problems or (res.note,):
                yield Result(WARN, f"swap partition: {SWAP_CONF} {problem}: cst swap apply refuses until it is fixed")
        case SwapSpecState.UNREADABLE:
            yield Result(
                INFO,
                f"swap partition: cannot read {SWAP_CONF} as this user: on the host, cst swap status --root reads it",
            )
        case SwapSpecState.UNKNOWN | SwapSpecState.WRITE:
            yield Result(
                WARN,
                f"swap partition: {SWAP_CONF} not written yet; an older CST set up {res.note}, and"
                " `cst swap apply` writes it from /etc/crypttab",
                f"{host.cst} swap apply",
            )
        case _:
            # Not configured: say so where the file was read, or where swap is set up (installed, or an
            # older CST's marked fstab line whose partition apply cannot find).
            set_up = host.exists(host.path(ZSWAP_CONF)) or marked_fstab_name(facts.fstab_text()) is not None
            if facts.settings_text() is not None or set_up:
                why = f"; {res.note}" if res.note else ""
                yield Result(
                    WARN,
                    f"swap partition: not configured (no SWAP_PARTUUID in {SWAP_CONF}{why}): cst swap apply refuses"
                    " (swap/README.md, section 2.3)",
                )


@check(CHECKS, "swap")
def check_machine_slice_memory_low(host: DoctorHostPort) -> Iterator[Result]:
    """machine.slice's MemoryLow as systemd applies it, read as `cst swap status` reads it: info when a
    drop-in after CST's sets it (status shows it as "over", apply leaves it), ok when it is CST's.
    Nothing when CST's drop-in is not installed or systemd cannot be asked."""
    want = parse_memory_low(read_text(host, host.path(SWAP_DROPIN), 65536))
    if want is None:
        return
    vals = unit_props(host, ["systemctl"], "machine.slice", ["MemoryLow", "DropInPaths"])
    if vals is None or not vals.get("MemoryLow", ""):
        return
    effective = vals["MemoryLow"]
    paths = vals.get("DropInPaths", "").split()
    dropins = [(p, read_text(host, host.path(HostPath(p)), 65536) or None) for p in paths]
    source = memory_low_override(effective, dropins, SWAP_DROPIN, want)
    size = parse_size(effective)
    if source is not None:
        yield Result(
            INFO,
            f"machine.slice MemoryLow {human_size(effective)}: set by {source}, which systemd applies after CST's"
            f" {SWAP_DROPIN} ({human(want)}); cst swap leaves it",
        )
    elif SWAP_DROPIN in paths and size == want:
        yield Result(OK, f"machine.slice MemoryLow {human(want)}, from {SWAP_DROPIN}")
