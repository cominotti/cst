<!-- SPDX-License-Identifier: MPL-2.0 -->
# perf: Read-Only Workstation Responsiveness Telemetry

`cst perf` samples live kernel, memory, CPU, IRQ, storage, and GPU settings in one read-only snapshot and produces an objective health verdict (`OPTIMIZED`, `ATTENTION`, `BOTTLENECK`, or `SKIP`) with recommended actions.

Nothing is written to the host and root is not required. From inside a toolbox container, `cst perf` automatically reads the host filesystem under `/run/host`.

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
   - `vm.min_free_kbytes`: expects at least `192 MiB` headroom on hosts with `>= 32 GiB` RAM (`32 MiB` on smaller hosts); emits `fix: sudo cst swap apply` when low.
   - Direct reclaim & compaction stalls (`allocstall_*`, `compact_stall`): computes hourly rates once uptime exceeds 10 minutes (`>= 5/h` `allocstall` or `>= 10/h` `compact_stall` -> `ATTENTION`; `>= 30/h` or `>= 60/h` -> `BOTTLENECK`).
   - THP (`transparent_hugepage/defrag`): flags `always` and `madvise` (synchronous page-fault compaction pauses) in favor of `defer+madvise` (`fix: sudo cst swap apply`).
   - MGLRU (`lru_gen/enabled`, `lru_gen/min_ttl_ms`): flags disabled MGLRU or `min_ttl_ms = 0` (`fix: sudo cst swap apply`).
   - `zswap`: flags disabled `zswap` with `fix: sudo cst swap apply`.
   - Memory PSI (`avg60`): flags `some >= 1%` (`ATTENTION`), `some >= 5%` or `full >= 1%` (`BOTTLENECK`).
   - Session memory floor (`user-*.slice/user@*.service/session.slice`): computes effective `min(session.memory.min, user.slice.memory.min, user-<uid>.slice.memory.min, user@<uid>.service.memory.min)` and flags unset floors or active working sets above the protected floor (`fix: sudo cst swap apply`).

2. **cpu** (`cpuidle`, `preempt`, `sched_ext`, `/dev/ntsync`, `irqbalance`, `/proc/interrupts`, `BUILD_CPUS`):
   - `cpuidle`: flags enabled C-states on `cpu0` with exit latency `>= 200 µs`.
   - Preemption mode (`sched/preempt` or kernel cmdline `preempt=`): expects `lazy` or `full`.
   - `ntsync`: checks `/dev/ntsync`, `/sys/class/misc/ntsync`, and `/sys/module/ntsync` (`fix: sudo cst cpu install`).
   - `irqbalance` & `BUILD_CPUS`: checks systemd unit runtime marker (`/run/systemd/units/invocation:irqbalance.service`) and detects high-volume latency-sensitive IRQs (`nvme`, GPUs, NICs, `xhci`, audio, `kvm`) whose interrupts land predominantly on `BUILD_CPUS`.

3. **io** (`block/*/queue/scheduler`, `io.cost.qos`, `io.weight`, `nvme_core`, dirty writeback, btrfs `commit_stats` & `SCRUB_LIMIT`):
   - Flags non-default slice `io.weight` (other than `100`) when `io.cost.qos` is empty and NVMe queues use `none`.
   - Flags `nvme_core.default_ps_max_latency_us > 50000` (deep APST sleep states; `fix: sudo cst btrfs install`).
   - Flags both `vm.dirty_background_bytes` and `vm.dirty_bytes` set to `0` (unbounded percentage-based dirty cache).
   - Flags unset `SCRUB_LIMIT` in `/etc/btrfs-setup/snapshots.conf` when btrfs filesystems exist (`fix: sudo cst btrfs scrub-limit set 500M`).
   - Evaluates `/sys/fs/btrfs/*/commit_stats` (`avg >= 25 ms` or `max >= 500 ms` -> `ATTENTION`; `avg >= 100 ms` or `max >= 2000 ms` -> `BOTTLENECK`).

4. **gpu** (`/sys/class/drm/card*`, `device/power/control`, `power_dpm_force_performance_level`, `pp_power_profile_mode`):
   - Inspects DRM connector status dynamically and flags only GPUs with connected monitors when `power/control=auto` (runtime PM sleep risk).
   - Flags connected AMD GPUs staying on `BOOTUP_DEFAULT` with `power_dpm_force_performance_level=auto` when CST game mode is active.

## 3. Integrations across CST

- `cst doctor` runs the `responsiveness` area using the exact same rules (`BOTTLENECK` or auto-fixable issues become `WARN`; advisory items become `INFO`).
- `cst swap status` includes a `== diagnosis` section evaluating memory signals from swap's view.
- `cst cpu status` includes a `== diagnosis` section evaluating CPU and GPU signals when host sysfs/procfs telemetry is present.
- `cst btrfs status` includes a compact `--axis io` diagnosis block.
