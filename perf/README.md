<!-- SPDX-License-Identifier: MPL-2.0 -->
# perf: Read-Only Workstation Responsiveness Telemetry

`cst perf` samples live kernel, memory, CPU, IRQ, storage, and GPU settings in one read-only snapshot and produces an objective health verdict (`OPTIMIZED`, `ATTENTION`, `BOTTLENECK`, or `SKIP`) with recommended actions. A line of a settings file it reads (`/etc/cpu-setup/cpu-setup.conf` for the cpu and gpu sections, `/etc/cpu-setup/irqbalance.env` and `/etc/sysconfig/irqbalance` for cpu, `/etc/btrfs-setup/snapshots.conf` for io) that is no plain `KEY=VALUE` assignment sets nothing; its section shows it as a `settings line` row, `PATH line N: TEXT: REASON (ignored)` (`(still read)` for a blank beside a value that bash still reads).

Nothing is written to the host and root is not required. From inside a toolbox container, `cst perf` reads the host filesystem under `/run/host` when `/run/host/proc` and `/run/host/etc` exist (and falls back to the container's own `/proc`/`/sys`/`/etc` otherwise).

## 1. Quick start

```sh
./cst perf                     # full 1-screen dashboard + top actions
./cst perf --compact           # concise summary and findings only
./cst perf --json              # machine-readable JSON output
./cst perf --axis memory,cpu   # restrict to selected axes
```

Exit codes:
- `0`: all sampled axes are `OPTIMIZED` (or `SKIP` when no telemetry exists)
- `1`: at least one `ATTENTION` or `BOTTLENECK` finding was detected
- `2`: invalid CLI arguments

## 2. Axes and objective thresholds

1. **memory** (`/proc/meminfo`, `/proc/vmstat`, `/proc/pressure/memory`, `/proc/sys/vm/*`, `/sys/kernel/mm/*`, `/sys/module/zswap/parameters/*`, cgroup `session.slice` / `user-*.slice`):
   - `vm.min_free_kbytes`: expects at least `192 MiB` headroom on hosts with `>= 32 GiB` RAM (`32 MiB` on smaller hosts); emits `fix: cst swap apply` when low.
   - Direct reclaim & compaction stalls (`allocstall_*`, `compact_stall`): computes hourly rates once uptime exceeds 10 minutes (`>= 5/h` `allocstall` or `>= 10/h` `compact_stall` -> `ATTENTION`; `>= 30/h` or `>= 60/h` -> `BOTTLENECK`).
   - THP (`transparent_hugepage/defrag`): flags `always`, `madvise`, and `defer+madvise` (synchronous page-fault compaction pauses on `MADV_HUGEPAGE` callers) in favor of `defer` (`fix: cst swap apply`).
   - MGLRU (`lru_gen/enabled`, `lru_gen/min_ttl_ms`): flags disabled MGLRU or `min_ttl_ms = 0` (`fix: cst swap apply`).
   - `zswap`: flags disabled `zswap` with `fix: cst swap apply`.
   - Memory PSI (`avg60`): flags `some >= 1%` (`ATTENTION`), `some >= 5%` or `full >= 1%` (`BOTTLENECK`).
   - Session memory floor (`user-*.slice/user@*.service/session.slice`): computes effective `min(session.memory.min, user.slice.memory.min, user-<uid>.slice.memory.min, user@<uid>.service.memory.min)`, ignores system/greeter UIDs (`< 1000`) and inactive human sessions once an active human session has `memory.min > 0`, and flags unset floors or active working sets above the protected floor (`fix: cst swap apply`).

2. **cpu** (`cpuidle`, `preempt`, `sched_ext`, `/dev/ntsync`, `irqbalance`, `/proc/interrupts`, `BUILD_CPUS`):
   - `cpuidle`: flags an enabled C-state on `cpu0` at or above the cap `cpu-power` applies (`fix: cst cpu install`, which runs `cpu-power apply` again through `cst-cpu-power.service`, also when that unit already ran): `CPU_CSTATE_MAX_LATENCY_US` from `/etc/cpu-setup/cpu-setup.conf` when the file sets it (`off` or `0`: no finding; a value that is not a number counts as `200 µs`, as `cpu-power` applies it), else `200 µs` with the `intel_idle` driver or an unknown one and no finding with any other (AMD's `acpi_idle`), where `cpu-power` leaves the C-states on by default.
   - Preemption mode (`sched/preempt` or kernel cmdline `preempt=`): expects `lazy` or `full`.
   - `ntsync`: checks `/dev/ntsync`, `/sys/class/misc/ntsync`, and `/sys/module/ntsync` (`fix: cst cpu install`).
   - `irqbalance` & `BUILD_CPUS`: checks systemd unit runtime marker (`/run/systemd/units/invocation:irqbalance.service`), reads `IRQBALANCE_BANNED_CPULIST` from `/etc/cpu-setup/irqbalance.env` only when `/etc/systemd/system/irqbalance.service.d/10-cst-ban.conf` is present (falling back to `/etc/sysconfig/irqbalance`), flags inactive `irqbalance` or an active `irqbalance` that does not cover the effective `BUILD_CPUS` ban-list (`fix: cst cpu irq apply`; `BUILD_CPUS` and `P_CORES` are read as `cst cpu irq` reads them: spaces and tabs around a list are dropped, a list with a no-break space is invalid), and inspects live `/proc/irq/<id>/effective_affinity_list` (falling back to `/proc/interrupts`) for movable latency-sensitive IRQs (`nvme*q0`, GPUs, NICs, `xhci`, audio, `kvm`, excluding `blk-mq` managed per-CPU `nvme*q1+` queues) on `BUILD_CPUS` (`fix: cst cpu irq apply`).

3. **io** (`block/*/queue/scheduler`, `io.cost.qos`, `io.weight`, `nvme_core`, dirty writeback, root btrfs `commit=`, `commit_stats`, `SCRUB_LIMIT`):
   - Counts active `enable=1` entries in `io.cost.qos` and flags non-default slice `io.weight` (other than `100`) when no device has `blk-iocost` enabled on `none` NVMe queues (`fix: cst btrfs iocost apply`).
   - Flags `nvme_core.default_ps_max_latency_us > 50000` (deep APST sleep states; `fix: cst btrfs install`).
   - Shows `dirty_background_bytes`, `dirty_bytes`, `dirty_writeback_centisecs` (`wb`), and `dirty_expire_centisecs` (`exp`), flagging unbounded `0` byte limits or an expire interval shorter than the root btrfs `commit=` window (`fix: cst btrfs install`).
   - Inspects root btrfs mount `commit=` + `flushoncommit` and flags `< 60 s` when `flushoncommit` is enabled (`fix: cst btrfs commit-interval apply`).
   - Flags unset `SCRUB_LIMIT` in `/etc/btrfs-setup/snapshots.conf` when btrfs filesystems exist (`fix: cst btrfs scrub-limit set 500M`).
   - Evaluates `/sys/fs/btrfs/*/commit_stats` (`avg >= 25 ms` or `max >= 500 ms` -> `ATTENTION`; `avg >= 100 ms` or `max >= 2000 ms` -> `BOTTLENECK`).

4. **gpu** (`/sys/class/drm/card*`, `device/power/control`, `power_dpm_force_performance_level`, `pp_power_profile_mode`):
   - Inspects DRM connector status dynamically and flags connected GPUs with `power/control=auto` when `GPU_PM_MODE` expects runtime PM off (`always` or unset, or `game` while game mode is active; `fix: cst cpu install`).
   - Flags connected AMD GPUs staying on `BOOTUP_DEFAULT` with `power_dpm_force_performance_level=auto` when CST game mode is active (`fix: cst cpu game off && cst cpu game on` in manual mode, else `sudo systemctl restart cpu-game.service`).

## 3. Integrations across CST

- `cst doctor` runs the `responsiveness` area using the same rules for axes whose CST module (`swap`, `cpu`, or `btrfs`) is installed or configured on the host (`BOTTLENECK` or auto-fixable issues become `WARN`; advisory items become `INFO`; `cst perf` itself always checks all four axes unconditionally).
- `cst swap status` includes a `== diagnosis` section evaluating memory signals from swap's view.
- `cst cpu status` includes a `== diagnosis` section evaluating CPU and GPU signals when host sysfs/procfs telemetry is present.
- `cst btrfs status` includes a compact `--axis io` diagnosis block.
