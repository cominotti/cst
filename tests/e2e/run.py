#!/usr/bin/env python3
"""Docker E2E orchestrator for cst (Layer 1 unprivileged systemd + Layer 2 privileged storage/cgroups)."""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = Path(__file__).resolve().parent
SUITES_DIR = E2E_DIR / "suites"
ARTIFACTS_BASE = E2E_DIR / ".artifacts"

IMG_SYSTEMD = "cst-e2e-systemd:local"
IMG_NAS = "cst-e2e-nas:local"
IMG_PRIV = "cst-e2e-priv:local"


def run(cmd, *, check=True, env=None, capture=False):
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        check=check,
        capture_output=capture,
        text=True,
    )


def host_lsm_security_opts():
    opts = []
    if Path("/sys/fs/selinux/enforce").exists():
        opts.extend(["--security-opt", "label=disable"])
    apparmor = Path("/sys/module/apparmor/parameters/enabled")
    try:
        if apparmor.exists() and apparmor.read_text().strip() == "Y":
            opts.extend(["--security-opt", "apparmor=unconfined"])
    except OSError:
        pass
    return opts


def docker_build(tag, dockerfile, *, force=False):
    if not force:
        check = run(["docker", "image", "inspect", tag], check=False, capture=True)
        if check.returncode == 0:
            print(f"[e2e] using cached image {tag}", flush=True)
            return
    print(f"[e2e] building {tag} from {dockerfile}...", flush=True)
    cmd = ["docker", "build"]
    cache_from = os.environ.get("CST_E2E_BUILD_CACHE_FROM", "").strip()
    if cache_from:
        for ref in (r.strip() for r in cache_from.split(",") if r.strip()):
            cmd.extend(["--cache-from", ref])
    cmd.extend(["-t", tag, "-f", str(dockerfile), "."])
    run(cmd)


def wait_systemd_ready(container, *, user="tester", timeout_s=45):
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
                "docker", "exec", container,
                "runuser", "-u", user, "--", "env",
                "XDG_RUNTIME_DIR=/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                "systemctl", "--user", "is-system-running",
            ],
            check=False,
            capture=True,
        )
        if (up.stdout.strip() or "") == "running":
            return
        time.sleep(0.3)
    raise RuntimeError(f"{container}: user@{user} manager failed to reach running state")


def dump_logs(containers, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in containers:
        for label, cmd in (
            ("system_failed", ["docker", "exec", c, "systemctl", "--failed", "--no-pager"]),
            ("user_failed", ["docker", "exec", c, "systemctl", "--user", "-M", "tester@", "--failed", "--no-pager"]),
            ("journal_tail", ["docker", "exec", c, "journalctl", "-b", "--no-pager", "-n", "200"]),
            ("docker_logs", ["docker", "logs", c]),
        ):
            p = run(cmd, check=False, capture=True)
            (out_dir / f"{c}_{label}.log").write_text(
                f"$ {' '.join(cmd)}\nrc={p.returncode}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}\n"
            )


def load_suite(filename):
    import importlib.util

    path = SUITES_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return unittest.defaultTestLoader.loadTestsFromModule(mod)


def run_tier1(run_id, artifact_dir, *, rebuild=False, keep_logs=False):
    docker_build(IMG_SYSTEMD, E2E_DIR / "images" / "systemd.Dockerfile", force=rebuild)
    docker_build(IMG_NAS, E2E_DIR / "images" / "nas.Dockerfile", force=rebuild)

    net = f"cst-e2e-net-{run_id}"
    nas = f"cst-e2e-nas-{run_id}"
    sysd = f"cst-e2e-sysd-{run_id}"
    ok = False
    try:
        run(["docker", "network", "create", net])
        run(["docker", "run", "-d", "--name", nas, "--network", net, IMG_NAS])
        run([
            "docker", "run", "-d", "--name", sysd, "--network", net,
            "--cap-add", "SYS_ADMIN",
            *host_lsm_security_opts(),
            "--cgroupns=private",
            "--tmpfs", "/run",
            "--tmpfs", "/run/lock",
            "--tmpfs", "/tmp",
            "-v", f"{REPO_ROOT}:/opt/cst:ro",
            IMG_SYSTEMD,
        ])
        wait_systemd_ready(sysd)

        os.environ["CST_E2E_SYSTEMD_CONTAINER"] = sysd
        os.environ["CST_E2E_NAS_CONTAINER"] = nas
        os.environ["CST_E2E_SYSTEMD_IMAGE"] = IMG_SYSTEMD
        os.environ["CST_E2E_REPO_ROOT"] = str(REPO_ROOT)

        suite = load_suite("suite_tier1_systemd.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        ok = result.wasSuccessful()
        return ok
    finally:
        if not ok or keep_logs:
            dump_logs([sysd, nas], artifact_dir / "tier1")
        for c in (sysd, nas):
            run(["docker", "rm", "-f", c], check=False, capture=True)
        run(["docker", "network", "rm", net], check=False, capture=True)


def run_tier2(run_id, artifact_dir, *, rebuild=False, keep_logs=False):
    docker_build(IMG_SYSTEMD, E2E_DIR / "images" / "systemd.Dockerfile", force=rebuild)
    docker_build(IMG_NAS, E2E_DIR / "images" / "nas.Dockerfile", force=rebuild)
    docker_build(IMG_PRIV, E2E_DIR / "images" / "priv.Dockerfile", force=rebuild)

    net = f"cst-e2e-net2-{run_id}"
    nas = f"cst-e2e-nas2-{run_id}"
    priv = f"cst-e2e-priv-{run_id}"
    ok = False
    try:
        run(["docker", "network", "create", net])
        run(["docker", "run", "-d", "--name", nas, "--network", net, IMG_NAS])
        run([
            "docker", "run", "-d", "--name", priv, "--network", net,
            "--privileged",
            "--cgroupns=private",
            "--tmpfs", "/run",
            "--tmpfs", "/run/lock",
            "--tmpfs", "/tmp",
            "-e", "E2E_LOOP_BTRFS=1",
            "-v", f"{REPO_ROOT}:/opt/cst:ro",
            IMG_PRIV,
        ])
        wait_systemd_ready(priv, timeout_s=60)

        os.environ["CST_E2E_PRIV_CONTAINER"] = priv
        os.environ["CST_E2E_NAS_CONTAINER"] = nas
        os.environ["CST_E2E_REPO_ROOT"] = str(REPO_ROOT)

        suite = load_suite("suite_tier2_storage_cpu.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        ok = result.wasSuccessful()
        return ok
    finally:
        if not ok or keep_logs:
            dump_logs([priv, nas], artifact_dir / "tier2")
        # Stop services holding btrfs mounts, detach loop devices inside priv,
        # remove containers, and assert no /e2e-disks loop device leaks on host.
        run(
            ["docker", "exec", priv, "bash", "-c",
             "systemctl stop e2e-dind virtqemud virtqemud.socket virtqemud-ro.socket "
             "virtqemud-admin.socket docker-gc-recorder btrfs-stall-monitor "
             "var-mnt-btrfs\\\\x2dtop.automount var-mnt-btrfs\\\\x2dtop.mount 2>/dev/null || true; "
             "umount -l /var/mnt/btrfs-top /var/home /sysroot /var 2>/dev/null || true; "
             "if [ -f /run/e2e-loop-devs ]; then "
             "xargs -r losetup -d </run/e2e-loop-devs 2>/dev/null || true; fi"],
            check=False,
            capture=True,
        )
        for c in (priv, nas):
            run(["docker", "rm", "-f", c], check=False, capture=True)
        run(["docker", "network", "rm", net], check=False, capture=True)
        leaked = []
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


def main():
    parser = argparse.ArgumentParser(description="CST Docker E2E test runner")
    parser.add_argument("--tier", action="append", type=int, choices=(1, 2), required=True)
    parser.add_argument("--keep-logs", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    artifact_dir = ARTIFACTS_BASE / run_id
    all_ok = True
    try:
        for tier in sorted(set(args.tier)):
            print(f"\n=== CST E2E LAYER {tier} (run_id={run_id}) ===", flush=True)
            if tier == 1:
                ok = run_tier1(run_id, artifact_dir, rebuild=args.rebuild, keep_logs=args.keep_logs)
            else:
                ok = run_tier2(run_id, artifact_dir, rebuild=args.rebuild, keep_logs=args.keep_logs)
            all_ok = all_ok and ok
    finally:
        if all_ok and not args.keep_logs and artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
        elif artifact_dir.exists():
            print(f"[e2e] logs preserved at {artifact_dir}", file=sys.stderr)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
