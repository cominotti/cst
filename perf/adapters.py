# SPDX-License-Identifier: MPL-2.0
"""Linux procfs/sysfs/cgroupfs adapter implementing `PerfFsReader` and `PerfProbePort`."""

from collections.abc import Sequence
from dataclasses import dataclass
import os
from typing import Final

from cst_core.adapters.files import read_text
from cst_core.ports import ExecutionContextPort
from perf.application import collect_snapshot_from_reader
from perf.domain.models import (
    ALL_PERF_AXES,
    PerfAxis,
    PerfPathKey,
    PerfProbeSnapshot,
    PerfRootKind,
    perf_path_spec,
)

# The procfs root a probe reads, unless a container maps the host's.
_PROC_DIR: Final = "/proc"


@dataclass(frozen=True, slots=True, kw_only=True)
class PerfPaths:
    """Resolved root and override paths readable from the current host/container view."""

    proc_dir: str = _PROC_DIR
    sys_dir: str = "/sys"
    cgroup_dir: str = "/sys/fs/cgroup"
    etc_dir: str = "/etc"
    run_dir: str = "/run"
    dev_dir: str = "/dev"
    zswap_params_dir: str = "/sys/module/zswap/parameters"
    cpu_conf_path: str | None = None
    btrfs_conf_path: str | None = None
    preempt_path: str | None = None
    sched_ext_path: str | None = None


def resolve_host_perf_paths(exec_ctx: ExecutionContextPort) -> PerfPaths:
    """Map host procfs/sysfs roots through `exec_ctx` only when the container exposes them."""
    if exec_ctx.is_container() and os.path.isdir(exec_ctx.path(_PROC_DIR)) and os.path.isdir(exec_ctx.etc_dir()):
        return PerfPaths(
            proc_dir=exec_ctx.path(_PROC_DIR),
            sys_dir=exec_ctx.path("/sys"),
            cgroup_dir=exec_ctx.path("/sys/fs/cgroup"),
            etc_dir=exec_ctx.etc_dir(),
            run_dir=exec_ctx.path("/run"),
            dev_dir=exec_ctx.path("/dev"),
            zswap_params_dir=exec_ctx.path("/sys/module/zswap/parameters"),
        )
    return PerfPaths()


def _safe_read_optional(path: str) -> tuple[str | None, bool]:
    text = read_text(path)
    if text is None and os.path.lexists(path) and not os.access(path, os.R_OK):
        return (None, True)
    return (text, False)


class LinuxPerfFsReader:
    """`PerfFsReader` backed by local/container-mapped procfs, sysfs, cgroupfs, and etc paths."""

    def __init__(self, paths: PerfPaths) -> None:
        self._paths = paths
        self._roots: dict[PerfRootKind, str] = {
            PerfRootKind.PROC: paths.proc_dir,
            PerfRootKind.SYS: paths.sys_dir,
            PerfRootKind.CGROUP: paths.cgroup_dir,
            PerfRootKind.ETC: paths.etc_dir,
            PerfRootKind.RUN: paths.run_dir,
            PerfRootKind.DEV: paths.dev_dir,
            PerfRootKind.ZSWAP_PARAMS: paths.zswap_params_dir,
        }
        self._key_overrides: dict[PerfPathKey, str] = {}
        if paths.cpu_conf_path is not None:
            self._key_overrides[PerfPathKey.CPU_CONF] = paths.cpu_conf_path
        if paths.btrfs_conf_path is not None:
            self._key_overrides[PerfPathKey.BTRFS_CONF] = paths.btrfs_conf_path
        if paths.preempt_path is not None:
            self._key_overrides[PerfPathKey.SCHED_PREEMPT] = paths.preempt_path
        if paths.sched_ext_path is not None:
            self._key_overrides[PerfPathKey.SCHED_EXT_STATE] = paths.sched_ext_path

    def _resolve_key(self, key: PerfPathKey) -> str:
        override = self._key_overrides.get(key)
        if override is not None:
            return override
        root, rel = perf_path_spec(key)
        return os.path.join(self._roots[root], rel)

    def read_key(self, key: PerfPathKey) -> str | None:
        """Read `key` returning `None` if absent or unreadable."""
        text, _ = _safe_read_optional(self._resolve_key(key))
        return text

    def read_preempt(self) -> tuple[str | None, bool]:
        """Read `sched/preempt`, tracking permission denial explicitly."""
        return _safe_read_optional(self._resolve_key(PerfPathKey.SCHED_PREEMPT))

    def read_rel(self, root: PerfRootKind, rel: str) -> str | None:
        """Read relative path under `root`, or `None` if absent or unreadable."""
        text, _ = _safe_read_optional(os.path.join(self._roots[root], rel))
        return text

    def list_rel(self, root: PerfRootKind, rel: str) -> tuple[str, ...]:
        """Return sorted entry names in `root/rel`, or `()` when missing/unreadable."""
        target = os.path.join(self._roots[root], rel)
        try:
            entries = os.listdir(target)
        except OSError:
            return ()
        return tuple(sorted(entries))

    def exists_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` exists."""
        return os.path.lexists(os.path.join(self._roots[root], rel))

    def is_dir_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` is a directory."""
        return os.path.isdir(os.path.join(self._roots[root], rel))


class LinuxPerfProbe:
    """Concrete `PerfProbePort` reading live or test-root host telemetry."""

    def __init__(self, paths: PerfPaths) -> None:
        self._reader = LinuxPerfFsReader(paths)

    def collect_snapshot(self, axes: Sequence[PerfAxis] = ALL_PERF_AXES) -> PerfProbeSnapshot:
        """Sample requested responsiveness axes without mutating host state."""
        return collect_snapshot_from_reader(self._reader, axes)
