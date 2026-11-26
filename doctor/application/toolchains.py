# SPDX-License-Identifier: MPL-2.0
"""The toolchains area of `cst doctor`: host binary readiness for active CST toolchain settings."""

from collections.abc import Iterator
import os.path
import tomllib

from cst_core.domain.contracts import CARGO_CONFIG_REL, SCCACHE_WRAPPER_REL
from cst_core.domain.shapes import json_object, parse_toml
from doctor.application.shared import check, runs
from doctor.domain import OK, WARN, HostPath, Result, host_join
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them.
CHECKS: list[tuple[str, CheckFn]] = []

SCCACHE_SEARCH_PATHS: tuple[HostPath, ...] = (
    HostPath("/usr/local/bin/sccache"),
    HostPath("/usr/bin/sccache"),
)


def _find_host_sccache(host: DoctorHostPort) -> HostPath | None:
    """First executable sccache candidate in standard user/host locations."""
    candidates = (
        host_join(host.home, ".cargo", "bin", "sccache"),
        host_join(host.home, ".local", "bin", "sccache"),
        *SCCACHE_SEARCH_PATHS,
    )
    return next((cand for cand in candidates if runs(host, cand)), None)


@check(CHECKS, "toolchains")
def check_sccache_binary(host: DoctorHostPort) -> Iterator[Result]:
    """Warn if CST's rustc-wrapper is active in ~/.cargo/config.toml without an executable sccache binary."""
    cfg_path = host_join(host.home, CARGO_CONFIG_REL)
    try:
        with host.open_regular_text(host.path(cfg_path)) as fh:
            data = parse_toml(fh.read())
    except OSError, tomllib.TOMLDecodeError:
        return
    build = data.get("build") if json_object(data) else None
    wrapper = build.get("rustc-wrapper") if json_object(build) else None
    if wrapper != os.path.join(host.home, SCCACHE_WRAPPER_REL):
        return
    found = _find_host_sccache(host)
    if found is not None:
        yield Result(OK, f"sccache binary: {found} (for CST's rustc-wrapper)")
        return
    yield Result(
        WARN,
        "sccache binary: missing (CST's rustc-wrapper is active in ~/.cargo/config.toml, "
        "so host builds fall back to rustc directly)",
        f"{host.cst} toolchains config apply",
    )
