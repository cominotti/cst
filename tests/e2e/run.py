#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Docker E2E orchestrator for cst (Layer 1 unprivileged systemd + Layer 2 privileged storage/cgroups)."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import concurrent.futures
import datetime
import functools
import os
from pathlib import Path
from types import ModuleType
import shutil
import subprocess
import sys
import time
from typing import NamedTuple
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.selection import (
    E2ETag,
    HostScope,
    ShardSpec,
    SuiteSelection,
    assign_shards,
    build_suite,
    default_tier1_jobs,
    filter_host_scope,
    format_case_line,
    iter_cases,
    parse_patterns,
    parse_shard,
    parse_tags,
    select_cases,
    select_shard,
    split_host_exclusive,
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


class _Tier1Exec(NamedTuple):
    run_id: str
    artifact_dir: Path
    rebuild: bool = False
    keep_logs: bool = False
    build_images: bool = True
    selection: SuiteSelection = SuiteSelection()


def _resolve_cases(tier: int, selection: SuiteSelection) -> tuple[list[unittest.TestCase], bool]:
    suite = load_suite(SUITE_FILES[tier])
    scoped = selection.for_tier(tier)
    filtered_by_query = scoped.active
    if filtered_by_query or scoped.shard is not None or scoped.host_scope != HostScope.ALL:
        cases = select_cases(iter_cases(suite), patterns=scoped.patterns, tags=scoped.tags)
        if filtered_by_query and not cases:
            return [], True
        cases = filter_host_scope(cases, scoped.host_scope)
        if scoped.shard is not None:
            cases = select_shard(cases, scoped.shard)
        return cases, True
    return iter_cases(suite), False


def _ensure_tier1_images(*, rebuild: bool) -> None:
    docker_build(
        IMG_SYSTEMD, E2E_DIR / "images" / "systemd.Dockerfile", digest=sources_digest("systemd"), force=rebuild
    )
    docker_build(IMG_NAS, E2E_DIR / "images" / "nas.Dockerfile", digest=sources_digest("nas"), force=rebuild)


def _tier1_log_dir(cfg: _Tier1Exec) -> Path:
    if cfg.selection.shard is not None:
        return cfg.artifact_dir / f"tier1-{cfg.selection.shard.dir_suffix()}"
    if cfg.selection.host_scope == HostScope.EXCLUSIVE:
        return cfg.artifact_dir / "tier1-exclusive"
    return cfg.artifact_dir / "tier1"


def _run_tier1_container(cfg: _Tier1Exec) -> bool:
    cases, is_sliced = _resolve_cases(1, cfg.selection)
    if is_sliced and not cases:
        if cfg.selection.patterns or cfg.selection.tags:
            matched = select_cases(
                iter_cases(load_suite(SUITE_FILES[1])),
                patterns=cfg.selection.patterns,
                tags=cfg.selection.tags,
            )
            if not matched:
                print("[e2e] tier 1: no tests matched selection", file=sys.stderr)
                return False
        shard_msg = f" shard {cfg.selection.shard.label()}" if cfg.selection.shard is not None else ""
        print(f"[e2e] tier 1{shard_msg}: 0 tests selected", flush=True)
        return True

    suite = build_suite(cases) if is_sliced else load_suite(SUITE_FILES[1])
    if cfg.build_images:
        _ensure_tier1_images(rebuild=cfg.rebuild)

    net = f"cst-e2e-net-{cfg.run_id}"
    nas = f"cst-e2e-nas-{cfg.run_id}"
    sysd = f"cst-e2e-sysd-{cfg.run_id}"
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
        if not ok or cfg.keep_logs:
            dump_logs([sysd, nas], _tier1_log_dir(cfg))
        for c in (sysd, nas):
            run(["docker", "rm", "-f", c], check=False, capture=True)
        run(["docker", "network", "rm", net], check=False, capture=True)


def run_tier1(
    run_id: str,
    artifact_dir: Path,
    *,
    rebuild: bool = False,
    keep_logs: bool = False,
    selection: SuiteSelection | None = None,
) -> bool:
    return _run_tier1_container(
        _Tier1Exec(
            run_id=run_id,
            artifact_dir=artifact_dir,
            rebuild=rebuild,
            keep_logs=keep_logs,
            selection=selection or SuiteSelection(),
        )
    )


def _shard_child_cmd(cfg: _Tier1Exec, shard: ShardSpec) -> list[str]:
    cmd = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--tier",
        "1",
        "--no-build",
        "--host-scope",
        HostScope.PARALLEL,
        "--shard",
        shard.label(),
        "--run-id",
        f"{cfg.run_id}-s{shard.index}",
        "--artifact-dir",
        str(cfg.artifact_dir),
    ]
    if cfg.keep_logs:
        cmd.append("--keep-logs")
    if cfg.selection.patterns:
        cmd.extend(["--match", ",".join(cfg.selection.patterns)])
    if cfg.selection.tags:
        cmd.extend(["--tag", ",".join(sorted(t.value for t in cfg.selection.tags))])
    return cmd


def _exec_shard_child(shard: ShardSpec, cmd: Sequence[str]) -> tuple[ShardSpec, bool, float, str]:
    start = time.monotonic()
    proc = subprocess.run(
        list(cmd),
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - start
    combined = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
    return shard, proc.returncode == 0, elapsed, combined


def _run_tier1_parallel(cfg: _Tier1Exec, jobs: int) -> bool:
    all_cases = select_cases(
        iter_cases(load_suite(SUITE_FILES[1])),
        patterns=cfg.selection.patterns,
        tags=cfg.selection.tags,
    )
    if not all_cases:
        print("[e2e] tier 1: no tests matched selection", file=sys.stderr)
        return False
    resolved_jobs = default_tier1_jobs(len(all_cases)) if jobs == 0 else jobs
    parallel_cases, exclusive_cases = split_host_exclusive(all_cases)
    if resolved_jobs <= 1 or (len(parallel_cases) <= 1 and not exclusive_cases):
        return _run_tier1_container(cfg)

    if cfg.build_images:
        _ensure_tier1_images(rebuild=cfg.rebuild)

    started = time.monotonic()
    ok = True
    worker_count = min(resolved_jobs, len(parallel_cases))
    if worker_count > 0:
        shards = [ShardSpec(index=i, total=worker_count) for i in range(1, worker_count + 1)]
        print(
            f"[e2e] tier 1: running {len(parallel_cases)} tests across {worker_count} parallel containers"
            + (f" (+{len(exclusive_cases)} host-exclusive solo afterwards)" if exclusive_cases else ""),
            flush=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_exec_shard_child, s, _shard_child_cmd(cfg, s)) for s in shards]
            for fut in concurrent.futures.as_completed(futures):
                shard, shard_ok, dur_s, output = fut.result()
                status = "OK" if shard_ok else "FAILED"
                print(f"\n--- Tier 1 shard {shard.label()} ({status}, {dur_s:.1f}s) ---", flush=True)
                if output:
                    print(output, end="" if output.endswith("\n") else "\n", flush=True)
                ok = ok and shard_ok

    if exclusive_cases:
        print(
            f"\n--- Tier 1 host-exclusive solo phase ({len(exclusive_cases)} tests) ---",
            flush=True,
        )
        solo_cfg = cfg._replace(
            run_id=f"{cfg.run_id}-solo",
            build_images=False,
            selection=SuiteSelection(
                patterns=cfg.selection.patterns,
                tags=cfg.selection.tags,
                host_scope=HostScope.EXCLUSIVE,
            ),
        )
        solo_ok = _run_tier1_container(solo_cfg)
        ok = ok and solo_ok

    total_s = time.monotonic() - started
    print(f"[e2e] tier 1 parallel summary: {len(all_cases)} tests in {total_s:.1f}s ({'OK' if ok else 'FAILED'})")
    return ok


def run_tier2(
    run_id: str,
    artifact_dir: Path,
    *,
    rebuild: bool = False,
    keep_logs: bool = False,
    selection: SuiteSelection | None = None,
) -> bool:
    cases, is_sliced = _resolve_cases(2, selection or SuiteSelection())
    if is_sliced and not cases:
        print("[e2e] tier 2: no tests matched selection", file=sys.stderr)
        return False
    suite = build_suite(cases) if is_sliced else load_suite(SUITE_FILES[2])

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


def _validate_options(tiers: Sequence[int], args: argparse.Namespace) -> tuple[SuiteSelection, int]:
    if args.tag is not None and 2 in tiers:
        raise ValueError("--tag is only supported for tier 1")
    patterns = parse_patterns(args.match) if args.match is not None else ()
    tags = parse_tags(args.tag) if args.tag is not None else frozenset()
    shard = parse_shard(args.shard) if args.shard is not None else None
    if shard is not None and 2 in tiers:
        raise ValueError("--shard is only supported for tier 1")
    raw_jobs = args.jobs
    if raw_jobs is None:
        jobs = 1
    else:
        assert isinstance(raw_jobs, int) and not isinstance(raw_jobs, bool)
        if raw_jobs < 0:
            raise ValueError(f"--jobs must be >= 0, got {raw_jobs}")
        if shard is not None and raw_jobs != 1:
            raise ValueError("cannot combine --shard with --jobs")
        if 2 in tiers and raw_jobs != 1:
            raise ValueError("--jobs is only supported for tier 1")
        jobs = raw_jobs
    host_scope = HostScope(args.host_scope)
    return SuiteSelection(patterns=patterns, tags=tags, shard=shard, host_scope=host_scope), jobs


def _print_parallel_listing(cases: Sequence[unittest.TestCase], resolved_jobs: int) -> None:
    parallel_cases, exclusive_cases = split_host_exclusive(cases)
    worker_count = min(resolved_jobs, len(parallel_cases))
    print(f"Tier 1 ({len(cases)} tests across {worker_count} parallel shards):")
    if worker_count > 0:
        for idx, bucket in enumerate(assign_shards(parallel_cases, worker_count), 1):
            print(f"  Shard {idx}/{worker_count} ({len(bucket)} tests):")
            for case in bucket:
                print(f"    {format_case_line(case)}")
    if exclusive_cases:
        print(f"  Host-exclusive solo phase ({len(exclusive_cases)} tests):")
        for case in exclusive_cases:
            print(f"    {format_case_line(case)}")


def _list_tier_tests(tier: int, selection: SuiteSelection, jobs: int) -> int:
    cases, is_sliced = _resolve_cases(tier, selection)
    if is_sliced and selection.active and not cases and selection.shard is None:
        print(f"[e2e] tier {tier}: no tests matched selection", file=sys.stderr)
        return 2
    if tier == 1 and jobs != 1 and selection.shard is None:
        resolved = default_tier1_jobs(len(cases)) if jobs == 0 else jobs
        if resolved > 1:
            _print_parallel_listing(cases, resolved)
            return 0
    shard_note = f", shard {selection.shard.label()}" if tier == 1 and selection.shard is not None else ""
    print(f"Tier {tier} ({len(cases)} tests{shard_note}):")
    for case in cases:
        print(f"  {format_case_line(case)}")
    return 0


def _dispatch_tier(tier: int, cfg: _Tier1Exec, jobs: int, *, direct: bool) -> bool:
    if tier == 2:
        return run_tier2(
            cfg.run_id, cfg.artifact_dir, rebuild=cfg.rebuild, keep_logs=cfg.keep_logs, selection=cfg.selection
        )
    if jobs != 1:
        return _run_tier1_parallel(cfg, jobs)
    if direct:
        return run_tier1(
            cfg.run_id, cfg.artifact_dir, rebuild=cfg.rebuild, keep_logs=cfg.keep_logs, selection=cfg.selection
        )
    return _run_tier1_container(cfg)


def _run_selected_tiers(tiers: Sequence[int], args: argparse.Namespace, selection: SuiteSelection, jobs: int) -> int:
    if selection.active:
        for tier in tiers:
            cases = select_cases(
                iter_cases(load_suite(SUITE_FILES[tier])),
                patterns=selection.patterns,
                tags=selection.tags if tier == 1 else frozenset(),
            )
            if not cases:
                print(f"[e2e] tier {tier}: no tests matched selection", file=sys.stderr)
                return 2
    problem = checkout_preflight()
    if problem is not None:
        print(problem, file=sys.stderr, end="")
        return 2
    if args.check_only:
        return 0

    run_id = args.run_id or (datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir is not None else (ARTIFACTS_BASE / run_id)
    cleanup_dir = args.artifact_dir is None
    direct = not args.no_build and args.run_id is None and cleanup_dir
    all_ok = True
    try:
        for tier in tiers:
            print(f"\n=== CST E2E LAYER {tier} (run_id={run_id}) ===", flush=True)
            cfg = _Tier1Exec(
                run_id=run_id,
                artifact_dir=artifact_dir,
                rebuild=args.rebuild,
                keep_logs=args.keep_logs,
                build_images=not args.no_build,
                selection=selection,
            )
            all_ok = _dispatch_tier(tier, cfg, jobs, direct=direct) and all_ok
    finally:
        if cleanup_dir:
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
    parser.add_argument("--shard", help="run only shard I/N of tier 1 (e.g. 1/3)")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="parallel tier 1 containers (0 = auto, 1 = single container)",
    )
    parser.add_argument("--no-build", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--artifact-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--host-scope",
        choices=tuple(s.value for s in HostScope),
        default=HostScope.ALL.value,
        help=argparse.SUPPRESS,
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
        selection, jobs = _validate_options(tiers, args)
    except ValueError as exc:
        print(f"[e2e] {exc}", file=sys.stderr)
        return 2

    if args.list_tests:
        for tier in tiers:
            rc = _list_tier_tests(tier, selection, jobs)
            if rc != 0:
                return rc
        return 0

    return _run_selected_tiers(tiers, args, selection, jobs)


if __name__ == "__main__":
    sys.exit(main())
