# SPDX-License-Identifier: MPL-2.0
"""The swap area of `cst doctor`: the zswap parameters swap's zswap.conf sets, and the partition settings."""

from collections.abc import Iterator

from doctor.application.shared import check, read_text
from doctor.domain import FSTAB, HostPath, INFO, OK, SWAP_CONF, WARN, ZSWAP_CONF, ZSWAP_PARAMS, Result
from doctor.ports import CheckFn, DoctorHostPort
from swap.domain import marked_fstab_name, parse_swap_settings, parse_tmpfiles_params

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


def _settings_text(host: DoctorHostPort, path: HostPath) -> str | None:
    """The file's text ("" when it cannot be read), or None when it is not there."""
    view = host.path(path)
    return read_text(host, view, 65536) if host.exists(view) else None


@check(CHECKS, "swap")
def check_swap_settings(host: DoctorHostPort) -> Iterator[Result]:
    """The swap partition in /etc/cst/swap.conf: WARN for each problem (cst swap apply refuses), WARN
    when it is missing where swap is set up (an older CST's marked fstab line: apply writes it; else
    apply refuses), ok when it names a partition. Nothing where swap is not set up."""
    text = _settings_text(host, SWAP_CONF)
    if text is not None:
        settings = parse_swap_settings(text)
        for problem in settings.problems:
            yield Result(WARN, f"swap partition: {SWAP_CONF} {problem}: cst swap apply refuses until it is fixed")
        if settings.spec is not None:
            spec = settings.spec
            yield Result(OK, f"swap partition: {spec.partuuid} ({spec.expected_size_bytes} bytes, {SWAP_CONF})")
            return
        if settings.problems:
            return
    name = marked_fstab_name(_settings_text(host, FSTAB) or "")
    if text is None and name is not None:
        yield Result(
            WARN,
            f"swap partition: {SWAP_CONF} not written yet; an older CST set up {name}, and"
            " `cst swap apply` writes it from /etc/crypttab",
            f"{host.cst} swap apply",
        )
    elif text is not None or name is not None or host.exists(host.path(ZSWAP_CONF)):
        yield Result(
            WARN,
            f"swap partition: not configured (no SWAP_PARTUUID in {SWAP_CONF}): cst swap apply refuses"
            " (swap/README.md, section 2.3)",
        )
