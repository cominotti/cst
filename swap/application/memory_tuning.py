# SPDX-License-Identifier: MPL-2.0
"""Memory reclaim responsiveness and uresourced session floor helpers for `swap`."""

from collections.abc import Callable
from dataclasses import dataclass
import posixpath
import re
from typing import Final

from cst_core.domain.install import InstallEntry, manifest_by_src
from cst_core.domain.text import group
from cst_core.domain.values import format_bytes_human as human
from cst_core.ports import CommandResult, FileSystemPort, SystemdPort, UnitName
from perf.domain.models import HUMAN_UID_MIN, KIB, PerfPathKey, PerfRootKind, perf_path_spec
from perf.domain.parsers import parse_bracket_choice, parse_proc_kib_table, parse_sysfs_tmpfiles_writes
from swap.domain import (
    ActiveState,
    ApplyEvent,
    ApplyEventKind,
    SettingCheckRow,
    SettingState,
    parse_size,
)

_MANIFEST: Final = manifest_by_src()
MM_TMPFILES_ENTRY: Final[InstallEntry] = _MANIFEST["swap/tmpfiles/cst-mm-responsiveness.conf"]
URESOURCED_ENTRY: Final[InstallEntry] = _MANIFEST["swap/units/uresourced.conf"]
THP_EXPERIMENT: Final[tuple[str, str]] = ("thp-defer", "/sys/kernel/mm/transparent_hugepage/defrag")
URESOURCED_UNIT: Final = UnitName("uresourced.service")
USER_SLICE: Final = "user.slice"
URESOURCED_BAK_SUFFIX: Final = ".pre-swap-config.bak"
_URESOURCED_WAIT_STEPS: Final[int] = 20
_URESOURCED_WAIT_STEP_S: Final[float] = 0.1

_SYS_PREFIX: Final = "/sys/"
_GLOBAL_SECTION: Final = "[Global]"
_ACTIVE_USER_SECTION: Final = "[ActiveUser]"
_USER_SLICE_ROW: Final = "user.slice memory.min"
_MEMORY_MIN_LINE_RE: Final = re.compile(r"^\s*MemoryMin\s*=\s*(\S+)")
_MAX_MEMORY_MIN_LINE_RE: Final = re.compile(r"^\s*MaxMemoryMin\s*=\s*(\S+)")
_PERCENT_RE: Final = re.compile(r"^([0-9]+)%$")
_PERCENT: Final = 100
# The kernel keeps memory.min in pages: a value systemd writes reads back rounded down to one.
_PAGE_BYTES: Final = 4096
# logind's seat files (/run/systemd/seats/SEAT): ACTIVE= names the seat's foreground session, if any,
# and ACTIVE_UID= its user.
_SEAT_ACTIVE_KEY: Final = "ACTIVE"
_SEAT_ACTIVE_UID_KEY: Final = "ACTIVE_UID"
_MM_LABELS: Final[dict[str, str]] = {
    "/sys/kernel/mm/transparent_hugepage/defrag": "THP defrag",
    "/sys/kernel/mm/lru_gen/min_ttl_ms": "MGLRU min_ttl_ms",
}


class SwapPerfReader:
    """Adapt swap's `FileSystemPort` and paths to `PerfFsReader`."""

    def __init__(self, fs: FileSystemPort, roots: dict[PerfRootKind, str]) -> None:
        self._fs = fs
        self._roots = roots

    def _path(self, root: PerfRootKind, rel: str) -> str | None:
        base = self._roots.get(root)
        return posixpath.join(base, rel) if base is not None else None

    def read_key(self, key: PerfPathKey) -> str | None:
        """Read `PerfPathKey` under swap's configured root dirs."""
        return self.read_rel(*perf_path_spec(key))

    def read_preempt(self) -> tuple[str | None, bool]:
        """Swap reports do not inspect sched debugfs."""
        return (None, False)

    def read_rel(self, root: PerfRootKind, rel: str) -> str | None:
        """Read relative path under `root`, or `None` if absent."""
        target = self._path(root, rel)
        return self._fs.read_text(target) if target is not None else None

    def list_rel(self, root: PerfRootKind, rel: str) -> tuple[str, ...]:
        """List directory entries under `root/rel`."""
        target = self._path(root, rel)
        if target is None or not self._fs.is_dir(target):
            return ()
        try:
            return tuple(sorted(self._fs.list_dir(target)))
        except OSError:
            return ()

    def exists_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` exists."""
        target = self._path(root, rel)
        return target is not None and self._fs.exists(target)

    def is_dir_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` is a directory."""
        target = self._path(root, rel)
        return target is not None and self._fs.is_dir(target)


def parse_uresourced_memory_min(text: str) -> int | None:
    """Extract `[ActiveUser] MemoryMin=` in bytes from an `uresourced.conf` file."""
    in_active_user = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_active_user = stripped == _ACTIVE_USER_SECTION
            continue
        if in_active_user:
            match = _MEMORY_MIN_LINE_RE.match(stripped)
            if match is not None:
                return parse_size(group(match, 1))
    return None


def mm_knob_label(sysfs_path: str) -> str:
    """Human-readable label for a sysfs memory knob."""
    return _MM_LABELS.get(sysfs_path, posixpath.basename(sysfs_path))


def sysfs_target_path(sys_dir: str, sysfs_path: str) -> str:
    """Map an absolute `/sys/...` path into `sys_dir`."""
    return posixpath.join(sys_dir, sysfs_path.removeprefix(_SYS_PREFIX))


def _live_sysfs_value(fs: FileSystemPort, target: str) -> str | None:
    raw = fs.read_first_line(target)
    if raw is None:
        return None
    choice = parse_bracket_choice(raw)
    return choice if choice is not None else raw.strip()


def mm_settings_rows(
    fs: FileSystemPort,
    *,
    sys_dir: str,
    cpu_run_dir: str,
    mm_text: str,
) -> tuple[list[SettingCheckRow], bool]:
    """Build status rows comparing live sysfs mm knobs against `cst-mm-responsiveness.conf`."""
    exp_name, exp_path = THP_EXPERIMENT
    exp_on = fs.exists(posixpath.join(cpu_run_dir, f"exp-{exp_name}"))
    rows: list[SettingCheckRow] = []
    thp_overridden = False
    for sysfs_path, want in parse_sysfs_tmpfiles_writes(mm_text):
        label = mm_knob_label(sysfs_path)
        live = _live_sysfs_value(fs, sysfs_target_path(sys_dir, sysfs_path))
        if live is None:
            rows.append(SettingCheckRow(SettingState.MISSING, label, "this kernel has no such knob", want))
            continue
        state = SettingState.OK if live == want else SettingState.DIFF
        if state == SettingState.DIFF and sysfs_path == exp_path and exp_on:
            state = SettingState.EXP
            thp_overridden = True
        rows.append(SettingCheckRow(state, label, live, want))
    return rows, thp_overridden


def parse_uresourced_max_memory_min(text: str) -> tuple[int | None, int | None]:
    """`[Global] MaxMemoryMin=` of a uresourced.conf as `(percent, bytes)` (None for the form it is not)."""
    section = ""
    for line in (raw.strip() for raw in text.splitlines()):
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == _GLOBAL_SECTION and (found := _MAX_MEMORY_MIN_LINE_RE.match(line)) is not None:
            pct = _PERCENT_RE.match(group(found, 1))
            return (int(group(pct, 1)), None) if pct is not None else (None, parse_size(group(found, 1)))
    return None, None


def effective_uresourced_memory_min(
    uresourced_text: str, mem_total_bytes: int | None, *, active_users: int = 1
) -> tuple[int | None, bool]:
    """`(bytes, capped)`: the floor uresourced gives user.slice, `[ActiveUser] MemoryMin` times the active
    users (at least one), after `[Global] MaxMemoryMin` (a share of MemTotal, rounded down to a page, or a
    size), which caps the total. capped: a share of MemTotal decided it, so the live value may differ by a
    page (MemTotal is read here, not by it)."""
    per_user = parse_uresourced_memory_min(uresourced_text)
    if per_user is None:
        return None, False
    want = per_user * max(active_users, 1)
    max_pct, max_bytes = parse_uresourced_max_memory_min(uresourced_text)
    capped = False
    if max_pct is not None and mem_total_bytes is not None:
        cap = mem_total_bytes * max_pct // _PERCENT // _PAGE_BYTES * _PAGE_BYTES
        if cap < want:
            want, capped = cap, True
    if max_bytes is not None and max_bytes < want:
        want, capped = max_bytes, False
    return want, capped


def _mem_total_bytes(fs: FileSystemPort, proc_dir: str) -> int | None:
    mem = parse_proc_kib_table(fs.read_text(posixpath.join(proc_dir, "meminfo")))
    return mem["MemTotal"] * KIB if "MemTotal" in mem else None


def _floor_matches(raw: str | None, want: int, *, capped: bool) -> bool:
    """Whether memory.min's text is the floor wanted (within a page when a share of MemTotal decided it)."""
    if raw is None or not raw.isdigit():
        return False
    live = int(raw)
    return abs(live - want) <= _PAGE_BYTES if capped else live == want


def active_seat_users(fs: FileSystemPort, seats_dir: str) -> int:
    """How many users have a session in a seat's foreground (logind's seat files: ACTIVE= and
    ACTIVE_UID=): uresourced gives its memory floor to each of them. A greeter's (a uid below
    HUMAN_UID_MIN, as perf's floor check) does not count; a user in front of two seats counts once.
    0 when the folder cannot be read (no logind)."""
    if not fs.is_dir(seats_dir):
        return 0
    try:
        seats = fs.list_dir(seats_dir)
    except OSError:
        return 0
    users: set[str] = set()
    for seat in seats:
        fields: dict[str, str] = {}
        for line in (fs.read_text(posixpath.join(seats_dir, seat)) or "").splitlines():
            key, _, value = line.partition("=")
            fields[key] = value
        if not fields.get(_SEAT_ACTIVE_KEY, "").strip():
            continue
        uid = fields.get(_SEAT_ACTIVE_UID_KEY, "").strip()
        if uid.isdigit() and int(uid) < HUMAN_UID_MIN:
            continue
        users.add(uid or f"seat {seat}")  # no uid given: that seat's user, whoever it is
    return len(users)


@dataclass(frozen=True, slots=True)
class UresourcedView:
    """Where uresourced's floor is read: the cgroup tree, /proc (MemTotal) and logind's seat files."""

    cgroup_dir: str
    proc_dir: str
    seats_dir: str


def _expected_floor(fs: FileSystemPort, where: UresourcedView, uresourced_text: str) -> tuple[int | None, bool, int]:
    """`(bytes, capped, active users)` of the floor uresourced should give user.slice now."""
    users = active_seat_users(fs, where.seats_dir)
    want, capped = effective_uresourced_memory_min(
        uresourced_text, _mem_total_bytes(fs, where.proc_dir), active_users=users
    )
    return want, capped, users


def uresourced_settings_row(
    fs: FileSystemPort,
    systemd: SystemdPort,
    *,
    where: UresourcedView,
    uresourced_text: str,
) -> SettingCheckRow | None:
    """Compare live `user.slice/memory.min` against `uresourced.conf` (after its MaxMemoryMin) when
    `uresourced.service` is active; a live 0 with no active seat session is inactive, not a difference."""
    want, capped, users = _expected_floor(fs, where, uresourced_text)
    if want is None:
        return None
    want_txt = human(want)
    active = systemd.show(URESOURCED_UNIT, ("ActiveState",)).get("ActiveState", "")
    if active != ActiveState.ACTIVE:
        return SettingCheckRow(SettingState.INACTIVE, _USER_SLICE_ROW, "uresourced not active", want_txt)
    live_min = fs.read_first_line(posixpath.join(where.cgroup_dir, USER_SLICE, "memory.min"))
    if live_min is None:
        return SettingCheckRow(SettingState.INACTIVE, _USER_SLICE_ROW, "no memory.min in cgroup", want_txt)
    if live_min == "0" and users == 0:
        return SettingCheckRow(SettingState.INACTIVE, _USER_SLICE_ROW, "no active seat session", want_txt)
    ok = _floor_matches(live_min, want, capped=capped)
    live_txt = human(int(live_min)) if live_min.isdigit() else live_min
    return SettingCheckRow(SettingState.OK if ok else SettingState.DIFF, _USER_SLICE_ROW, live_txt, want_txt)


def apply_mm_knobs(
    fs: FileSystemPort,
    *,
    sys_dir: str,
    cpu_run_dir: str,
    mm_text: str,
    events: list[ApplyEvent],
) -> None:
    """Write live sysfs memory responsiveness knobs, skipping absent nodes and honoring `exp-thp-defer`."""
    exp_name, exp_path = THP_EXPERIMENT
    exp_on = fs.exists(posixpath.join(cpu_run_dir, f"exp-{exp_name}"))
    for sysfs_path, want in parse_sysfs_tmpfiles_writes(mm_text):
        label = mm_knob_label(sysfs_path)
        target = sysfs_target_path(sys_dir, sysfs_path)
        live = _live_sysfs_value(fs, target)
        if live is None:
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"skipped:   {label} (this kernel has no such knob)"))
            continue
        if sysfs_path == exp_path and exp_on:
            events.append(
                ApplyEvent(
                    "out",
                    ApplyEventKind.LINE,
                    f"kept:      {label} = {live} (the cpu experiment {exp_name} is on;",
                )
            )
            events.append(
                ApplyEvent(
                    "out",
                    ApplyEventKind.LINE,
                    f"           after 'cst cpu experiment off {exp_name}', run apply again, or reboot)",
                )
            )
            continue
        if live != want:
            fs.write_text_direct(target, f"{want}\n")
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"set:       {label} = {want} (was {live})"))


def maybe_restart_uresourced(
    systemd: SystemdPort,
    *,
    written: bool,
    events: list[ApplyEvent],
    show_result: Callable[[CommandResult], None],
) -> tuple[int, bool]:
    """Restart `uresourced.service` when it is active and `/etc/uresourced.conf` was rewritten or the live
    floor differs from it (written: either)."""
    if not written:
        return (0, False)
    if systemd.show(URESOURCED_UNIT, ("ActiveState",)).get("ActiveState", "") != ActiveState.ACTIVE:
        return (0, False)
    res = systemd.restart(URESOURCED_UNIT)
    show_result(res)
    if res.returncode != 0:
        return (res.returncode, False)
    events.append(
        ApplyEvent(
            "out",
            ApplyEventKind.LINE,
            "restarted: uresourced.service (run 'systemctl --user daemon-reload' or log in again for live session.slice)",
        )
    )
    return (0, True)


def wait_for_uresourced_floor(
    fs: FileSystemPort,
    *,
    where: UresourcedView,
    uresourced_text: str,
    sleep: Callable[[float], None],
) -> bool:
    """Wait briefly after restarting `uresourced` for `user.slice/memory.min` to reflect `uresourced.conf`."""
    want, capped, _ = _expected_floor(fs, where, uresourced_text)
    if want is None:
        return True
    path = posixpath.join(where.cgroup_dir, USER_SLICE, "memory.min")

    def _matches() -> bool:
        return _floor_matches(fs.read_first_line(path), want, capped=capped)

    if _matches():
        return True
    for _ in range(_URESOURCED_WAIT_STEPS):
        sleep(_URESOURCED_WAIT_STEP_S)
        if _matches():
            return True
    return False
