# SPDX-License-Identifier: MPL-2.0
"""Responsiveness bottleneck checks for `cst doctor`, backed by `perf` rules."""

from collections.abc import Iterable
from typing import Final

from doctor.application.shared import check
from doctor.domain import HostPath, INFO, OK, ViewPath, WARN, Result, host_join
from doctor.ports import CheckFn, DoctorHostPort
from perf.application import collect_snapshot_from_reader
from perf.domain.evaluate import evaluate_perf_snapshot
from perf.domain.models import ALL_PERF_AXES, PerfPathKey, PerfRootKind, PerfVerdict, perf_path_spec

CHECKS: list[tuple[str, CheckFn]] = []

_ROOT_HOSTS: Final[dict[PerfRootKind, HostPath]] = {
    PerfRootKind.PROC: HostPath("/proc"),
    PerfRootKind.SYS: HostPath("/sys"),
    PerfRootKind.CGROUP: HostPath("/sys/fs/cgroup"),
    PerfRootKind.ETC: HostPath("/etc"),
    PerfRootKind.RUN: HostPath("/run"),
    PerfRootKind.DEV: HostPath("/dev"),
    PerfRootKind.ZSWAP_PARAMS: HostPath("/sys/module/zswap/parameters"),
}


class _DoctorPerfReader:
    """Adapt `DoctorHostPort` to `PerfFsReader` without direct host I/O."""

    def __init__(self, host: DoctorHostPort) -> None:
        self._host = host

    def _vp(self, root: PerfRootKind, rel: str) -> ViewPath:
        return self._host.path(host_join(_ROOT_HOSTS[root], rel))

    def _read_vp(self, vp: ViewPath) -> tuple[str | None, bool]:
        try:
            with self._host.open_text(vp) as fh:
                return (fh.read(), False)
        except PermissionError:
            return (None, True)
        except OSError:
            return (None, False)

    def read_key(self, key: PerfPathKey) -> str | None:
        """Read one `PerfPathKey` via `DoctorHostPort`."""
        root, rel = perf_path_spec(key)
        text, _ = self._read_vp(self._vp(root, rel))
        return text

    def read_preempt(self) -> tuple[str | None, bool]:
        """Read `sched/preempt` via `DoctorHostPort`, preserving permission status."""
        root, rel = perf_path_spec(PerfPathKey.SCHED_PREEMPT)
        return self._read_vp(self._vp(root, rel))

    def read_rel(self, root: PerfRootKind, rel: str) -> str | None:
        """Read relative path under `root`."""
        text, _ = self._read_vp(self._vp(root, rel))
        return text

    def list_rel(self, root: PerfRootKind, rel: str) -> tuple[str, ...]:
        """List directory entries under `root/rel`."""
        try:
            return tuple(sorted(self._host.listdir(self._vp(root, rel))))
        except OSError:
            return ()

    def exists_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` exists."""
        return self._host.exists(self._vp(root, rel))

    def is_dir_rel(self, root: PerfRootKind, rel: str) -> bool:
        """Return True when `root/rel` is a directory."""
        return self._host.isdir(self._vp(root, rel))


@check(CHECKS, "responsiveness")
def check_responsiveness(host: DoctorHostPort) -> Iterable[Result]:
    """Summarize responsiveness diagnosis from `perf` rules across memory/cpu/io/gpu."""
    snapshot = collect_snapshot_from_reader(_DoctorPerfReader(host), ALL_PERF_AXES)
    report = evaluate_perf_snapshot(snapshot, ALL_PERF_AXES)
    sampled = [ax for ax in report.axes if ax.verdict is not PerfVerdict.SKIP]
    if not sampled:
        return ()
    findings = [f for ax in sampled for f in ax.findings]
    if not findings:
        return [Result(OK, f"responsiveness: {len(sampled)} axes checked, no bottlenecks (cst perf)")]
    results: list[Result] = []
    for f in findings:
        msg = f"{f.axis.value}: {f.summary}"
        if f.verdict is PerfVerdict.BOTTLENECK or f.fix_command:
            results.append(Result(WARN, msg, f.fix_command))
        else:
            detail = f"{msg} — {f.recommendation}" if f.recommendation else msg
            results.append(Result(INFO, detail))
    return results
