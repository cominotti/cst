# SPDX-License-Identifier: MPL-2.0
"""Composition root for `perf`."""

import os
from typing import Final

from cst_core.adapters.runner import HostExecutionContext
from perf.adapters import LinuxPerfProbe, PerfPaths
from perf.ports import PerfProbePort

_ENV_PERF_ROOT: Final = "CST_PERF_ROOT"


def perf_paths_from_env() -> PerfPaths:
    """Resolve procfs/sysfs/cgroup/etc roots from `CST_PERF_ROOT` or `HostExecutionContext`."""
    override_root = os.environ.get(_ENV_PERF_ROOT, "").strip()
    if override_root:
        sys_root = os.path.join(override_root, "sys")
        return PerfPaths(
            proc_dir=os.path.join(override_root, "proc"),
            sys_dir=sys_root,
            cgroup_dir=os.path.join(sys_root, "fs", "cgroup"),
            etc_dir=os.path.join(override_root, "etc"),
            run_dir=os.path.join(override_root, "run"),
            dev_dir=os.path.join(override_root, "dev"),
            zswap_params_dir=os.path.join(sys_root, "module", "zswap", "parameters"),
        )
    ctx = HostExecutionContext()
    return PerfPaths(
        proc_dir=ctx.path("/proc"),
        sys_dir=ctx.path("/sys"),
        cgroup_dir=ctx.path("/sys/fs/cgroup"),
        etc_dir=ctx.etc_dir(),
        run_dir=ctx.path("/run"),
        dev_dir=ctx.path("/dev"),
        zswap_params_dir=ctx.path("/sys/module/zswap/parameters"),
    )


def start_perf_probe() -> PerfProbePort:
    """Build the default `PerfProbePort` for CLI execution."""
    return LinuxPerfProbe(perf_paths_from_env())
