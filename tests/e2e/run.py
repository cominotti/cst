#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Docker E2E orchestrator for cst (Layer 1 unprivileged systemd + Layer 2 privileged storage/cgroups)."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import datetime
import functools
import os
from pathlib import Path
from types import ModuleType
import shutil
import subprocess
import sys
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.selection import (
    E2ETag,
    SuiteSelection,
    build_suite,
    format_case_line,
    iter_cases,
    parse_patterns,
    parse_tags,
    select_cases,
)

E2E_DIR = Path(__file__).resolve().parent
SUITES_DIR = E2E_DIR / "suites"
ARTIFACTS_BASE = E2E_DIR / ".artifacts"

IMG_SYSTEMD = "cst-e2e-systemd:local"
IMG_NAS = "cst-e2e-nas:local"
IMG_PRIV = "cst-e2e-priv:local"
# The label each image carries: the digest of its sources (image_digest.py's LABEL).
SOURCES_LABEL = "org.cst.e2e.sources"

# Tier 2 starts a --privileged container. Such a container sees the host's
# block devices and loop devices and can load kernel modules, so a mistake in
# a test or in cst could touch the real disks of the machine that runs it.
# It is meant for disposable CI runners: on any other machine it only runs
# after an explicit opt-in.
PRIVILEGED_OPT_IN_ENV = "CST_E2E_ALLOW_PRIVILEGED"

PRIVILEGED_REFUSAL = f"""\
[e2e] Refusing to run tier 2 without an explicit opt-in.

Tier 2 runs a --privileged Docker container. It sees this machine's block
devices (/dev/sd*, /dev/nvme*, /dev/loop*), creates loop devices and btrfs
filesystems, and could change the host's real disks if something goes wrong.
CI runs it on throwaway runners. Do not run it on a workstation you care about.

To run it anyway (for example in a disposable VM), opt in with either:
  {PRIVILEGED_OPT_IN_ENV}=1 python3 -I -B tests/e2e/run.py --tier 2
  python3 -I -B tests/e2e/run.py --tier 2 --allow-privileged
  invowk cmd test e2e tier2 --allow_privileged
Tier 1 (unprivileged) needs no opt-in.
"""


def privileged_allowed(flag: bool, environ: Mapping[str, str] | None = None) -> bool:
    """True when tier 2 may run: --allow-privileged, or CST_E2E_ALLOW_PRIVILEGED=1."""
    environ = os.environ if environ is None else environ
    return bool(flag) or environ.get(PRIVILEGED_OPT_IN_ENV, "").strip() == "1"


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(REPO_ROOT),
        env=env,
        check=check,
        capture_output=capture,
        text=True,
    )


# The checkout is bind-mounted into the containers (/opt/cst, read-only, no
# :z relabel) and its scripts run from there. Under SELinux in enforcing
# mode, files labelled for a home folder (user_home_t, a fresh clone or a git
# worktree) cannot be read or run by the container ("Permission denied" on
# /opt/cst/cst in test_01) unless it starts with label=disable, which
# host_lsm_security_opts adds whenever it sees the host's SELinux: at
# /sys/fs/selinux, or under /run/host inside a toolbox (whose docker is the
# host's daemon). checkout_preflight is the fallback for any other case: it
# stops the run and prints the label containers may use.
SELINUX_ENFORCE = Path("/sys/fs/selinux/enforce")
# Inside a toolbox the host's selinuxfs is not mounted at /sys/fs/selinux; it is under /run/host.
SELINUX_ENFORCE_FROM_TOOLBOX = Path("/run/host/sys/fs/selinux/enforce")
# "Y" when the kernel has AppArmor on: host_lsm_security_opts then adds apparmor=unconfined.
APPARMOR_ENABLED = Path("/sys/module/apparmor/parameters/enabled")
# podman and toolbox create this file in every container they start.
CONTAINER_MARKER = Path("/run/.containerenv")
CONTAINER_FILE_TYPE = "container_file_t"
# The types a container may read and run besides container_file_t.
CONTAINER_READABLE_TYPES = frozenset({CONTAINER_FILE_TYPE, "container_ro_file_t"})


def selinux_type(path: Path) -> str | None:
    """The SELinux type of a file (container_file_t), or None when it has no label."""
    try:
        raw = os.getxattr(path, "security.selinux")
    except OSError:
        return None
    parts = raw.rstrip(b"\0").decode("ascii", "replace").split(":")
    return parts[2] if len(parts) >= 3 else None


def checkout_label_problem(
    *, enforcing: bool, types: Mapping[str, str | None], repo: Path, label_disabled: bool = False
) -> str | None:
    """Why the containers could not use the checkout (types: path -> SELinux type), with the fix; None if fine.

    label_disabled: the containers start with --security-opt label=disable
    (host_lsm_security_opts), so labels do not matter.
    """
    if not enforcing or label_disabled:
        return None
    wrong = sorted(path for path, kind in types.items() if kind is not None and kind not in CONTAINER_READABLE_TYPES)
    if not wrong:
        return None
    found = ", ".join(f"{p} ({types[p]})" for p in wrong)
    return (
        f"[e2e] SELinux is enforcing and the checkout is not labelled {CONTAINER_FILE_TYPE}: {found}.\n"
        "The containers bind-mount it and run its scripts; with this label they get 'Permission denied'.\n"
        f"Fix (once per checkout or worktree): chcon -R -t {CONTAINER_FILE_TYPE} {repo}\n"
    )


def checkout_preflight(repo: Path = REPO_ROOT) -> str | None:
    """checkout_label_problem for this machine and checkout."""
    enforcing = False
    for flag in (SELINUX_ENFORCE, SELINUX_ENFORCE_FROM_TOOLBOX):
        try:
            enforcing = flag.read_text().strip() == "1"
            break
        except OSError:
            continue
    return checkout_label_problem(
        enforcing=enforcing,
        types={str(p): selinux_type(p) for p in (repo, repo / "cst")},
        repo=repo,
        label_disabled="label=disable" in host_lsm_security_opts(),
    )


def host_selinux(
    enforce: Path = SELINUX_ENFORCE, marker: Path = CONTAINER_MARKER, host_enforce: Path = SELINUX_ENFORCE_FROM_TOOLBOX
) -> bool:
    """SELinux is on in the kernel the containers run on: here, or on the host of this toolbox."""
    return enforce.exists() or (marker.exists() and host_enforce.exists())


def host_lsm_security_opts(apparmor: Path = APPARMOR_ENABLED) -> list[str]:
    opts: list[str] = []
    if host_selinux():
        opts.extend(["--security-opt", "label=disable"])
    try:
        if apparmor.exists() and apparmor.read_text().strip() == "Y":
            opts.extend(["--security-opt", "apparmor=unconfined"])
    except OSError:
        pass
    return opts


@functools.cache
def _image_digest_module() -> ModuleType:
    """tests/e2e/image_digest.py, loaded by its path (run.py runs with -I: its folder is not on sys.path)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("cst_e2e_image_digest", E2E_DIR / "image_digest.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sources_digest(name: str) -> str:
    """The digest of image NAME's sources in this checkout (image_digest.py)."""
    digest = vars(_image_digest_module())["image_digest"](name, REPO_ROOT)
    if not isinstance(digest, str):
        raise TypeError(f"image_digest({name!r}) gave {digest!r}")
    return digest


def image_label(tag: str) -> str | None:
    """The org.cst.e2e.sources label of the local image TAG ("" without one), None when there is no image."""
    fmt = '{{ index .Config.Labels "' + SOURCES_LABEL + '" }}'
    found = run(["docker", "image", "inspect", "-f", fmt, tag], check=False, capture=True)
    if found.returncode != 0:
        return None
    label = found.stdout.strip()
    return "" if label == "<no value>" else label


def docker_build(tag: str, dockerfile: Path, *, digest: str, force: bool = False) -> None:
    """Build TAG from DOCKERFILE with the label of its sources' DIGEST, unless (without force) a local
    image of that tag has that label: an image without it, or with another, is rebuilt, never used."""
    if not force:
        label = image_label(tag)
        if label == digest:
            print(f"[e2e] using cached image {tag} (its sources are this checkout's)", flush=True)
            return
        if label is None:
            why = "no image"
        elif label == "":
            why = "no sources label"
        else:
            why = "other sources"
        print(f"[e2e] {tag}: {why}: rebuilding", flush=True)
    print(f"[e2e] building {tag} from {dockerfile}...", flush=True)
    cmd = ["docker", "build", "--label", f"{SOURCES_LABEL}={digest}"]
    cache_from = os.environ.get("CST_E2E_BUILD_CACHE_FROM", "").strip()
    if cache_from:
        for ref in (r.strip() for r in cache_from.split(",") if r.strip()):
            cmd.extend(["--cache-from", ref])
    cmd.extend(["-t", tag, "-f", str(dockerfile), "."])
    run(cmd)


def wait_systemd_ready(container: str, *, user: str = "tester", timeout_s: float = 45) -> None:
    deadline = time.monotonic() + timeout_s
    last_sys = ""
    while time.monotonic() < deadline:
        p = run(["docker", "exec", container, "systemctl", "is-system-running"], check=False, capture=True)
        last_sys = p.stdout.strip() or p.stderr.strip()
        if last_sys in ("running", "degraded"):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f"{container}: systemd did not become ready within {timeout_s}s (last={last_sys!r})")

    # Ensure tester@ user manager is up and dbus session bus is listening
    run(["docker", "exec", container, "systemctl", "start", "user@1000.service"], check=False, capture=True)
    while time.monotonic() < deadline:
        up = run(
            [
                "docker",
                "exec",
                container,
                "runuser",
                "-u",
                user,
                "--",
                "env",
                "XDG_RUNTIME_DIR=/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                "systemctl",
                "--user",
                "is-system-running",
            ],
            check=False,
            capture=True,
        )
        if (up.stdout.strip() or "") == "running":
            return
        time.sleep(0.3)
    raise RuntimeError(f"{container}: user@{user} manager failed to reach running state")


def dump_logs(containers: Sequence[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in containers:
        for label, cmd in (
            ("system_failed", ["docker", "exec", c, "systemctl", "--failed", "--no-pager"]),
            ("user_failed", ["docker", "exec", c, "systemctl", "--user", "-M", "tester@", "--failed", "--no-pager"]),
            ("journal_boot", ["docker", "exec", c, "journalctl", "-b", "--no-pager"]),
            ("docker_logs", ["docker", "logs", c]),
        ):
            p = run(cmd, check=False, capture=True)
            (out_dir / f"{c}_{label}.log").write_text(
                f"$ {' '.join(cmd)}\nrc={p.returncode}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}\n"
            )


SUITE_FILES: Mapping[int, str] = {
    1: "suite_tier1_systemd.py",
    2: "suite_tier2_storage_cpu.py",
}


def load_suite(filename: str) -> unittest.TestSuite:
    import importlib.util

    path = SUITES_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return unittest.defaultTestLoader.loadTestsFromModule(mod)


def _selected_suite(tier: int, selection: SuiteSelection) -> unittest.TestSuite | None:
    suite = load_suite(SUITE_FILES[tier])
    scoped = selection.for_tier(tier)
    if not scoped.active:
        return suite
    selected = select_cases(iter_cases(suite), patterns=scoped.patterns, tags=scoped.tags)
    if not selected:
        return None
    return build_suite(selected)


def run_tier1(
    run_id: str,
    artifact_dir: Path,
    *,
    rebuild: bool = False,
    keep_logs: bool = False,
    selection: SuiteSelection | None = None,
) -> bool:
    suite = _selected_suite(1, selection or SuiteSelection())
    if suite is None:
        print("[e2e] tier 1: no tests matched selection", file=sys.stderr)
        return False

    docker_build(
        IMG_SYSTEMD, E2E_DIR / "images" / "systemd.Dockerfile", digest=sources_digest("systemd"), force=rebuild
    )
    docker_build(IMG_NAS, E2E_DIR / "images" / "nas.Dockerfile", digest=sources_digest("nas"), force=rebuild)

    net = f"cst-e2e-net-{run_id}"
    nas = f"cst-e2e-nas-{run_id}"
    sysd = f"cst-e2e-sysd-{run_id}"
    ok = False
    try:
        run(["docker", "network", "create", net])
        run(["docker", "run", "-d", "--name", nas, "--network", net, IMG_NAS])
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                sysd,
                "--network",
                net,
                "--cap-add",
                "SYS_ADMIN",
                *host_lsm_security_opts(),
                "--cgroupns=private",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                "--tmpfs",
                "/tmp",
                "-v",
                f"{REPO_ROOT}:/opt/cst:ro",
                IMG_SYSTEMD,
            ]
        )
        wait_systemd_ready(sysd)

        os.environ["CST_E2E_SYSTEMD_CONTAINER"] = sysd
        os.environ["CST_E2E_NAS_CONTAINER"] = nas
        os.environ["CST_E2E_SYSTEMD_IMAGE"] = IMG_SYSTEMD
        os.environ["CST_E2E_REPO_ROOT"] = str(REPO_ROOT)
        # test_01 starts containers of its own on the checkout: the same options.
        os.environ["CST_E2E_SECURITY_OPTS"] = " ".join(host_lsm_security_opts())

        result = unittest.TextTestRunner(verbosity=2).run(suite)
        ok = result.wasSuccessful()
        return ok
    finally:
        if not ok or keep_logs:
            dump_logs([sysd, nas], artifact_dir / "tier1")
        for c in (sysd, nas):
            run(["docker", "rm", "-f", c], check=False, capture=True)
        run(["docker", "network", "rm", net], check=False, capture=True)


def run_tier2(
    run_id: str,
    artifact_dir: Path,
    *,
    rebuild: bool = False,
    keep_logs: bool = False,
    selection: SuiteSelection | None = None,
) -> bool:
    suite = _selected_suite(2, selection or SuiteSelection())
    if suite is None:
        print("[e2e] tier 2: no tests matched selection", file=sys.stderr)
        return False

    docker_build(
        IMG_SYSTEMD, E2E_DIR / "images" / "systemd.Dockerfile", digest=sources_digest("systemd"), force=rebuild
    )
    docker_build(IMG_NAS, E2E_DIR / "images" / "nas.Dockerfile", digest=sources_digest("nas"), force=rebuild)
    docker_build(IMG_PRIV, E2E_DIR / "images" / "priv.Dockerfile", digest=sources_digest("priv"), force=rebuild)

    net = f"cst-e2e-net2-{run_id}"
    nas = f"cst-e2e-nas2-{run_id}"
    priv = f"cst-e2e-priv-{run_id}"
    ok = False
    try:
        run(["docker", "network", "create", net])
        run(["docker", "run", "-d", "--name", nas, "--network", net, IMG_NAS])
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                priv,
                "--network",
                net,
                "--privileged",
                "--cgroupns=private",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                "--tmpfs",
                "/tmp",
                "-e",
                "E2E_LOOP_BTRFS=1",
                "-v",
                f"{REPO_ROOT}:/opt/cst:ro",
                IMG_PRIV,
            ]
        )
        wait_systemd_ready(priv, timeout_s=60)

        os.environ["CST_E2E_PRIV_CONTAINER"] = priv
        os.environ["CST_E2E_NAS_CONTAINER"] = nas
        os.environ["CST_E2E_REPO_ROOT"] = str(REPO_ROOT)

        result = unittest.TextTestRunner(verbosity=2).run(suite)
        ok = result.wasSuccessful()
        return ok
    finally:
        if not ok or keep_logs:
            dump_logs([priv, nas], artifact_dir / "tier2")
        # Stop services holding btrfs mounts, detach loop devices inside priv,
        # remove containers, and assert no /e2e-disks loop device leaks on host.
        run(
            [
                "docker",
                "exec",
                priv,
                "bash",
                "-c",
                (
                    "systemctl stop e2e-dind virtqemud virtqemud.socket virtqemud-ro.socket "
                    "virtqemud-admin.socket docker-gc-recorder btrfs-stall-monitor "
                    "var-mnt-btrfs\\\\x2dtop.automount var-mnt-btrfs\\\\x2dtop.mount 2>/dev/null || true; "
                    "umount -l /var/mnt/btrfs-top /var/home /sysroot /var 2>/dev/null || true; "
                    "if [ -f /run/e2e-loop-devs ]; then "
                    "xargs -r losetup -d </run/e2e-loop-devs 2>/dev/null || true; fi"
                ),
            ],
            check=False,
            capture=True,
        )
        for c in (priv, nas):
            run(["docker", "rm", "-f", c], check=False, capture=True)
        run(["docker", "network", "rm", net], check=False, capture=True)
        leaked: list[str] = []
        for _ in range(5):
            leaked = []
            for bf in Path("/sys/block").glob("loop*/loop/backing_file"):
                try:
                    val = bf.read_text().strip()
                except OSError:
                    continue
                if "/e2e-disks/" in val:
                    leaked.append(f"{bf.parent.parent.name}:{val}")
            if not leaked:
                break
            time.sleep(0.2)
        if leaked:
            raise RuntimeError(f"Tier 2 loop device(s) leaked on host: {leaked}")


def _validate_filters(
    tiers: Sequence[int], raw_match: Sequence[str] | None, raw_tag: Sequence[str] | None
) -> SuiteSelection:
    if raw_tag is not None and 2 in tiers:
        raise ValueError("--tag is only supported for tier 1")
    patterns = parse_patterns(raw_match) if raw_match is not None else ()
    tags = parse_tags(raw_tag) if raw_tag is not None else frozenset()
    return SuiteSelection(patterns=patterns, tags=tags)


def _list_tier_tests(tier: int, selection: SuiteSelection) -> int:
    suite = _selected_suite(tier, selection)
    if suite is None:
        print(f"[e2e] tier {tier}: no tests matched selection", file=sys.stderr)
        return 2
    cases = iter_cases(suite)
    print(f"Tier {tier} ({len(cases)} tests):")
    for case in cases:
        print(f"  {format_case_line(case)}")
    return 0


def _run_selected_tiers(tiers: Sequence[int], args: argparse.Namespace, selection: SuiteSelection) -> int:
    if selection.active:
        for tier in tiers:
            if _selected_suite(tier, selection) is None:
                print(f"[e2e] tier {tier}: no tests matched selection", file=sys.stderr)
                return 2
    problem = checkout_preflight()
    if problem is not None:
        print(problem, file=sys.stderr, end="")
        return 2
    if args.check_only:
        return 0

    run_id = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    artifact_dir = ARTIFACTS_BASE / run_id
    all_ok = True
    try:
        for tier in tiers:
            print(f"\n=== CST E2E LAYER {tier} (run_id={run_id}) ===", flush=True)
            if tier == 1:
                ok = run_tier1(
                    run_id, artifact_dir, rebuild=args.rebuild, keep_logs=args.keep_logs, selection=selection
                )
            else:
                ok = run_tier2(
                    run_id, artifact_dir, rebuild=args.rebuild, keep_logs=args.keep_logs, selection=selection
                )
            all_ok = all_ok and ok
    finally:
        if all_ok and not args.keep_logs and artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
        elif artifact_dir.exists():
            print(f"[e2e] logs preserved at {artifact_dir}", file=sys.stderr)

    return 0 if all_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CST Docker E2E test runner")
    parser.add_argument("--tier", action="append", type=int, choices=(1, 2), required=True)
    parser.add_argument("--keep-logs", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument(
        "-k",
        "--match",
        action="append",
        help="filter tests by substring or glob pattern (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        help=f"filter tier 1 tests by domain tag ({', '.join(t.value for t in E2ETag)}; repeatable or comma-separated)",
    )
    parser.add_argument(
        "--list-tests",
        action="store_true",
        help="list selected tests and exit without starting Docker",
    )
    parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help=f"allow tier 2 (a --privileged container) to run; same as {PRIVILEGED_OPT_IN_ENV}=1",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only check that the requested tiers may run (opt-in for tier 2), then exit",
    )
    args = parser.parse_args(argv)
    tiers = sorted(set(args.tier))

    if 2 in tiers and not privileged_allowed(args.allow_privileged):
        print(PRIVILEGED_REFUSAL, file=sys.stderr, end="")
        return 2
    try:
        selection = _validate_filters(tiers, args.match, args.tag)
    except ValueError as exc:
        print(f"[e2e] {exc}", file=sys.stderr)
        return 2

    if args.list_tests:
        for tier in tiers:
            rc = _list_tier_tests(tier, selection)
            if rc != 0:
                return rc
        return 0

    return _run_selected_tiers(tiers, args, selection)


if __name__ == "__main__":
    sys.exit(main())
