# SPDX-License-Identifier: MPL-2.0
"""The swap area of `cst doctor`: the zswap parameters swap's zswap.conf sets."""

from collections.abc import Iterator

from doctor.application.shared import check
from doctor.domain import INFO, OK, WARN, ZSWAP_CONF, ZSWAP_PARAMS, Result
from doctor.ports import CheckFn, DoctorHostPort
from swap.domain import parse_tmpfiles_params

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


@check(CHECKS, "swap")
def check_zswap_params(host: DoctorHostPort) -> Iterator[Result]:
    """WARN for each zswap parameter that swap's zswap.conf sets and this
    kernel does not have: systemd-tmpfiles skips it at boot without a word
    (a new kernel can rename or drop one). Nothing when that file is not
    installed."""
    try:
        with host.open_text(host.path(ZSWAP_CONF)) as f:
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
