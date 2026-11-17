# Docker E2E tests

These tests start Fedora 45 containers with systemd as PID 1 and run `cst` in
them. They need Docker. Run them through invowk from the repository root, or
call `tests/e2e/run.py` directly.

| Tier | Command | Privileged? |
|------|---------|-------------|
| 1 | `invowk cmd test e2e tier1` | No (`--cap-add SYS_ADMIN` only) |
| 2 | `invowk cmd test e2e tier2 --allow_privileged` | **Yes** (`--privileged`) |

Tier 1 (`suites/suite_tier1_systemd.py`):

- `test_01`: install and apply commands refuse inside a container.
- `test_02`: systemd PID 1, the system D-Bus and the user manager run.
- `test_03`: the `cst` dispatch, the `~/.local/bin/cst` link, `cst status` and `cst doctor`.
- `test_04`: toolchains: the timer, its settings and the prune. `auto off` refuses, with nothing removed and the timer still running, when `systemctl --user` cannot reach the user manager. A normal `auto off` stops the timer. A second `auto off` stops a timer an earlier one left running with its files gone (which `status` shows), and removes its dangling `timers.target.wants` link.
- `test_05`: bluetooth `plan`, `apply --no-restart`, the WirePlumber roles checked by the real libwireplumber, the LDAC unit, and `revert`.
- `test_06`: the `btrfs-notify` queue delivered by `cst-pending-alerts` and seen by `cst doctor`, and real restic over SFTP to a NAS container.
- `test_07`: the drop-in order the cpu game profile relies on (`set-property --runtime` vs CST's `10-` baseline, a uresourced-style `system.control` file and the profile's `zz-cst-game.conf`), across `daemon-reload`.
- `test_08`: the real game profile `on`/`tick`/`off` against the real user manager: a slice of its own and a `systemd-run` job made idle, kept across the slice coming back, nothing added by a second `tick`, all reverted by `off` even for a slice already gone, a `zz-cst-game.conf` left behind removed by a `tick`. `cst doctor` flags the escaped slice, the job and the leftover drop-in, and not a `user-<x>.slice`.
- `test_09`: the sccache wrapper under real cargo: which crates `RUST_SCCACHE_WORKSPACE=no` and `CST_SCCACHE_WORKSPACE=0` keep out of sccache, for a workspace member, a path, a git and a vendored dependency.
- `test_10`: `cst cpu quiet` against the real `/proc` files: a plain run exits 0 or 1 with one line per test; busy loops on every CPU fail the CPU test with its value and limit; the game profile's `game.state` fails its test; a usage error exits 2.
- `test_11`: the real installer writes the whole install manifest (staged, as root and as the user), a second run changes nothing, and every Python program runs its help from `/` with no import error.
- `test_12`: `cst vm backup-timer on` installs the zipapp, the service and the timer; a second `on` writes nothing; `off` removes everything.
- `test_13`: `cst btrfs degraded-boot` with fakes for rpm-ostree, findmnt, btrfs, lsblk and the udevd part of udevadm: install, `udevadm verify` on the rule and `systemd-analyze verify` on the units, a second install that writes nothing and changes no deployment, a `status` that changes nothing, and an uninstall that removes everything.
- `test_16`: every unit CST installs (the manifest, staged by the real installer) or generates (`tests/unit_files.py`: cst-vm, cst-toolchains, the LDAC watcher, the degraded-boot setup) passes `systemd-analyze verify` with no output under `--root` of the stage, and `systemd-analyze security --offline` of each service stays at its entry in `SECURITY_CEILINGS` (only goes down).

Tier 2 (`suites/suite_tier2_storage_cpu.py`):

- `test_01`: a loopback btrfs RAID1 for `/sysroot`, `/var` and `/var/home`, with the host's kernel files shadowed.
- `test_02`: `cst btrfs install`: the units, the stall monitor, the scrub and the automatic reclaim.
- `test_03`, `test_03a`: snapshots: convert, hourly, the top-level automount, restic to the NAS, purge; cache folders left out of each snapshot.
- `test_04`: `~/Bulk`: the subvolume, the links, NOCOW, keep and unkeep.
- `test_05`, `test_05a`: Docker-in-Docker, `builds.slice` and docker-gc; the Docker volumes NOCOW migration with a stub `docker.service`: postponed while it is active, with nothing touched; a copy-on-write volume rewritten with the same data, `+C` everywhere and no leftovers; still run when its unit and `docker.service` start in one transaction, as at boot, with the `ActiveState` the helper saw printed in the log.
- `test_06`: cgroups v2 (the game profile's weights also after a `daemon-reload`, its `zz-cst-game.conf` drop-ins, and `uninstall` removing one left behind), Polkit, and `cpu-vm-pin`.

Both commands take `--keep_logs` (keep the journald/systemd logs in
`tests/e2e/.artifacts/` even on success) and `--rebuild` (rebuild the images).
`invowk cmd test all` runs the fast gates (`invowk cmd check`: validate, audit,
`test lint`, `test pylint`, `test types`, `test unit`), then both tiers.

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
