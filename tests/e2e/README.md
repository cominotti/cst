# Docker E2E tests

These tests start Fedora 45 containers with systemd as PID 1 and run `cst` in
them. They need Docker. Run them through invowk from the repository root, or
call `tests/e2e/run.py` directly.

| Tier | Command | What it does | Privileged? |
|------|---------|--------------|-------------|
| 1 | `invowk cmd test e2e tier1` | systemd, toolchains, bluetooth/WirePlumber files, restic over SFTP to a NAS container, alerts, `cst doctor` | No (`--cap-add SYS_ADMIN` only) |
| 2 | `invowk cmd test e2e tier2 --allow_privileged` | loopback btrfs RAID1, Docker-in-Docker, cgroups v2, Polkit | **Yes** (`--privileged`) |

Both commands take `--keep_logs` (keep the journald/systemd logs in
`tests/e2e/.artifacts/` even on success) and `--rebuild` (rebuild the images).
`invowk cmd test all` runs the unit tests, the shell lint and both tiers.

## Tier 2 needs an explicit opt-in

Tier 2 runs a `--privileged` Docker container. Such a container sees all of
the host's block devices (`/dev/sd*`, `/dev/nvme*`, `/dev/loop*`), creates loop
devices and btrfs filesystems, and could change the host's real disks if a test
or `cst` itself goes wrong. CI runs it on throwaway GitHub runners.

So `run.py` refuses tier 2 (exit code 2, with an explanation) unless you opt in
with one of:

- the environment variable `CST_E2E_ALLOW_PRIVILEGED=1` (only the value `1` counts);
- `run.py --allow-privileged`;
- `invowk cmd test e2e tier2 --allow_privileged` or `invowk cmd test all --allow_privileged`.

`invowk cmd test all` checks the opt-in first, before the long unit and lint
runs. The CI and nightly workflows set `CST_E2E_ALLOW_PRIVILEGED=1` on their
tier 2 steps. Do not opt in on a workstation whose disks you care about: use
a disposable VM instead. Tier 1 needs no opt-in.

The unit tests for this guard are in `tests/e2e/test_run_guard.py`; they never
start Docker.
