# SPDX-License-Identifier: MPL-2.0
"""The toolchains area of `cst doctor`: host binary readiness for active CST toolchain settings."""

from collections.abc import Iterator
import os.path
import tomllib

from cst_core.domain.contracts import (
    CARGO_BUILD_DIR_REL,
    CARGO_BUILD_TMPFS_MARKER,
    CARGO_BUILD_TMPFS_REL,
    CARGO_BUILD_TMPFS_TMPFILES_PREFIX,
    CARGO_CONFIG_REL,
    SCCACHE_WRAPPER_REL,
)
from cst_core.domain.shapes import json_object, parse_toml
from cst_core.domain.text import parse_shell_assignments, strip_ascii_blanks, unescape_mount_field
from cst_core.domain.units import SYSTEMD_WHITESPACE, SYSTEM_UNIT_DIR, systemd_lines
from doctor.application.findmnt import findmnt_all_mounts
from doctor.application.shared import check, runs
from doctor.domain import INFO, OK, WARN, HostPath, Result, host_join
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them.
CHECKS: list[tuple[str, CheckFn]] = []

SCCACHE_SEARCH_PATHS: tuple[HostPath, ...] = (
    HostPath("/usr/local/bin/sccache"),
    HostPath("/usr/bin/sccache"),
)
_MOUNTS_MIN_COLUMNS = 4


def _find_host_sccache(host: DoctorHostPort) -> HostPath | None:
    """First executable sccache candidate in standard user/host locations."""
    candidates = (
        host_join(host.home, ".cargo", "bin", "sccache"),
        host_join(host.home, ".local", "bin", "sccache"),
        *SCCACHE_SEARCH_PATHS,
    )
    return next((cand for cand in candidates if runs(host, cand)), None)


def _read_cargo_build(host: DoctorHostPort) -> tuple[str | None, str | None]:
    """(rustc-wrapper, build-dir) from ~/.cargo/config.toml [build], or (None, None)."""
    cfg_path = host_join(host.home, CARGO_CONFIG_REL)
    try:
        with host.open_regular_text(host.path(cfg_path)) as fh:
            data = parse_toml(fh.read())
    except OSError, tomllib.TOMLDecodeError:
        return None, None
    build = data.get("build") if json_object(data) else None
    if not json_object(build):
        return None, None
    w = build.get("rustc-wrapper")
    bd = build.get("build-dir")
    return (w if isinstance(w, str) else None), (bd if isinstance(bd, str) else None)


@check(CHECKS, "toolchains")
def check_sccache_binary(host: DoctorHostPort) -> Iterator[Result]:
    """Warn if CST's rustc-wrapper is active in ~/.cargo/config.toml without an executable sccache binary."""
    wrapper, _ = _read_cargo_build(host)
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


def _host_real_home(host: DoctorHostPort) -> HostPath:
    vp = host.realpath(host.path(host.home))
    root = host.root.rstrip("/")
    if root and vp.startswith(root + "/"):
        return HostPath(vp[len(root) :])
    return HostPath(vp)


def _mount_unit_info(text: str) -> tuple[str, str]:
    where = ""
    size = ""
    for raw in systemd_lines(text):
        line = raw.strip(SYSTEMD_WHITESPACE)
        if not line or line.startswith(("#", ";", "[")):
            continue
        k, sep, v = line.partition("=")
        if not sep:
            continue
        key, val = k.strip(SYSTEMD_WHITESPACE), v.strip(SYSTEMD_WHITESPACE)
        if key == "Where":
            where = val
        elif key == "Options":
            for opt in val.split(","):
                if opt.startswith("size="):
                    size = opt.split("=", 1)[1]
    return where, size


def _find_cst_tmpfs_unit(host: DoctorHostPort, targets: set[HostPath]) -> tuple[str, str, str] | None:
    unit_dir = host.path(HostPath(SYSTEM_UNIT_DIR))
    try:
        names = sorted(host.listdir(unit_dir))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".mount"):
            continue
        try:
            with host.open_regular_text(host.path(host_join(HostPath(SYSTEM_UNIT_DIR), name))) as fh:
                text = fh.read()
        except OSError:
            continue
        if CARGO_BUILD_TMPFS_MARKER not in text:
            continue
        where, size = _mount_unit_info(text)
        if HostPath(where) in targets:
            return name, where, size
    return None


def _tmpfs_size_in_mounts_text(text: str, norm_targets: set[str]) -> str | None:
    size_seen: str | None = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < _MOUNTS_MIN_COLUMNS:
            continue
        if os.path.normpath(unescape_mount_field(fields[1])) not in norm_targets:
            continue
        if fields[2] == "tmpfs":
            size_seen = next((o.split("=", 1)[1] for o in fields[3].split(",") if o.startswith("size=")), "mounted")
    return size_seen


def _mounted_tmpfs_size(host: DoctorHostPort, targets: set[HostPath], *, allow_findmnt: bool) -> str | None:
    norm_targets = {os.path.normpath(t) for t in targets}
    try:
        with host.open_text(host.path(HostPath("/proc/mounts"))) as fh:
            found = _tmpfs_size_in_mounts_text(fh.read(), norm_targets)
            if found is not None:
                return found
    except OSError:
        pass
    if not allow_findmnt:
        return None
    live_findmnt = findmnt_all_mounts(host)
    return _tmpfs_size_in_mounts_text(live_findmnt, norm_targets) if live_findmnt is not None else None


def _rust_build_tmpfs_is_no(host: DoctorHostPort) -> bool:
    conf = host_join(host.home, ".config/cst/toolchains.conf")
    try:
        with host.open_regular_text(host.path(conf)) as fh:
            parsed = parse_shell_assignments(fh.read(), bash_sourced=False)
    except OSError:
        return False
    return strip_ascii_blanks(parsed.values.get("RUST_BUILD_TMPFS", "")).lower() == "no"


@check(CHECKS, "toolchains")
def check_cargo_build_tmpfs(host: DoctorHostPort) -> Iterator[Result]:
    """Check readiness and config coherence for CST's dedicated cargo build-dir tmpfs mount."""
    norm_home = HostPath(os.path.normpath(host.home))
    real_home = _host_real_home(host)
    targets = {
        host_join(norm_home, CARGO_BUILD_TMPFS_REL),
        host_join(real_home, CARGO_BUILD_TMPFS_REL),
    }
    _, build_dir = _read_cargo_build(host)
    tmpfs_tpls = {f"{h}/{CARGO_BUILD_TMPFS_REL}/{{workspace-path-hash}}" for h in (norm_home, real_home)}
    nvme_tpls = {f"{h}/{CARGO_BUILD_DIR_REL}/{{workspace-path-hash}}" for h in (norm_home, real_home)}
    unit = _find_cst_tmpfs_unit(host, targets)
    points_tmpfs = isinstance(build_dir, str) and build_dir in tmpfs_tpls
    mounted_size = _mounted_tmpfs_size(host, targets, allow_findmnt=(unit is not None or points_tmpfs))
    if unit is None and not points_tmpfs and mounted_size is None:
        return
    if points_tmpfs and mounted_size is None:
        yield Result(
            WARN,
            f"cargo build-dir tmpfs: ~/{CARGO_CONFIG_REL} points to ~/{CARGO_BUILD_TMPFS_REL}, which is not mounted",
            f"{host.cst} toolchains tmpfs apply (or {host.cst} toolchains config apply to fall back to NVMe)",
        )
    if unit is not None:
        unit_name, where, _ = unit
        if mounted_size is None:
            yield Result(
                WARN,
                f"cargo build-dir tmpfs: unit {unit_name} is installed, but {where} is not mounted",
                f"{host.cst} toolchains tmpfs apply",
            )
            return
        user = host.user_name(host.uid)
        tf_path = HostPath(f"{CARGO_BUILD_TMPFS_TMPFILES_PREFIX}{user}.conf") if user else None
        if tf_path is not None and not host.isfile(host.path(tf_path)):
            yield Result(
                WARN,
                f"cargo build-dir tmpfs: missing {tf_path}",
                f"{host.cst} toolchains tmpfs apply",
            )
        else:
            yield Result(OK, f"cargo build-dir tmpfs: mounted at {where} (cap {mounted_size})")
    if (
        mounted_size is not None
        and isinstance(build_dir, str)
        and build_dir in nvme_tpls
        and not _rust_build_tmpfs_is_no(host)
    ):
        yield Result(
            INFO,
            f"cargo build-dir tmpfs: mounted, while ~/{CARGO_CONFIG_REL} still points to ~/{CARGO_BUILD_DIR_REL}",
            f"{host.cst} toolchains config apply",
        )
