# SPDX-License-Identifier: MPL-2.0
"""Memory reclaim responsiveness and uresourced session floor helpers for `swap`."""

from collections.abc import Callable
import posixpath
import re
from typing import Final

from cst_core.domain.install import InstallEntry, manifest_by_src
from cst_core.domain.text import group
from cst_core.domain.values import format_bytes_human as human
from cst_core.ports import CommandResult, FileSystemPort, SystemdPort, UnitName
from perf.domain.models import PerfPathKey, PerfRootKind, perf_path_spec
from perf.domain.parsers import parse_bracket_choice, parse_sysfs_tmpfiles_writes
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
_ACTIVE_USER_SECTION: Final = "[ActiveUser]"
_USER_SLICE_ROW: Final = "user.slice memory.min"
_MEMORY_MIN_LINE_RE: Final = re.compile(r"^\s*MemoryMin\s*=\s*(\S+)")
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


def uresourced_settings_row(
    fs: FileSystemPort,
    systemd: SystemdPort,
    *,
    cgroup_dir: str,
    uresourced_text: str,
) -> SettingCheckRow | None:
    """Compare live `user.slice/memory.min` against `uresourced.conf` when `uresourced.service` is active."""
    want = parse_uresourced_memory_min(uresourced_text)
    if want is None:
        return None
    want_txt = human(want)
    active = systemd.show(URESOURCED_UNIT, ("ActiveState",)).get("ActiveState", "")
    if active != ActiveState.ACTIVE:
        return SettingCheckRow(SettingState.INACTIVE, _USER_SLICE_ROW, "uresourced not active", want_txt)
    live_min = fs.read_first_line(posixpath.join(cgroup_dir, USER_SLICE, "memory.min"))
    if live_min is None:
        return SettingCheckRow(SettingState.INACTIVE, _USER_SLICE_ROW, "no memory.min in cgroup", want_txt)
    ok = live_min.isdigit() and int(live_min) == want
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
    """Restart `uresourced.service` after rewriting `/etc/uresourced.conf` when it is active."""
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
    cgroup_dir: str,
    uresourced_text: str,
    sleep: Callable[[float], None],
) -> bool:
    """Wait briefly after restarting `uresourced` for `user.slice/memory.min` to reflect `uresourced.conf`."""
    want = parse_uresourced_memory_min(uresourced_text)
    if want is None:
        return True
    path = posixpath.join(cgroup_dir, USER_SLICE, "memory.min")

    def _matches() -> bool:
        raw = fs.read_first_line(path)
        return raw is not None and raw.isdigit() and int(raw) == want

    if _matches():
        return True
    for _ in range(_URESOURCED_WAIT_STEPS):
        sleep(_URESOURCED_WAIT_STEP_S)
        if _matches():
            return True
    return False
