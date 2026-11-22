# SPDX-License-Identifier: MPL-2.0
"""Composition root for `perf`."""

from collections.abc import Mapping
import os
from typing import Final

from cst_core.adapters.runner import HostExecutionContext
from cst_core.adapters.runtime import restore_default_signals
from cst_core.ports import ExecutionContextPort
from perf.adapters import LinuxPerfProbe, PerfPaths, resolve_host_perf_paths
from perf.ports import PerfProbePort

_ENV_PERF_ROOT: Final = "CST_PERF_ROOT"


def start_perf(env: Mapping[str, str] | None = None) -> str:
    """Restore default SIGPIPE handling and return the CLI program name (`CST_PROG` or `'cst perf'`)."""
    restore_default_signals()
    source = os.environ if env is None else env
    return source.get("CST_PROG") or "cst perf"


def perf_paths_from_env(ctx: ExecutionContextPort | None = None) -> PerfPaths:
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
    return resolve_host_perf_paths(HostExecutionContext() if ctx is None else ctx)


def start_perf_probe() -> PerfProbePort:
    """Build the default `PerfProbePort` for CLI execution."""
    return LinuxPerfProbe(perf_paths_from_env())
