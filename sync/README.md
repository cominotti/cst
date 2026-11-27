<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst sync`: keep installed CST files up to date

`cst sync` compares the files recorded on the machine (`/var/lib/cst/installed.json`
for root and `~/.local/state/cst/installed.json` for your user) against what
this checkout builds now, and runs only the module commands needed to refresh
installed components that are outdated, missing files, or `pending`.

It never installs modules you never set up on this machine.

## Commands

- **`cst sync plan [--include-risky] [--overwrite-modified]`** (also `cst sync`):
  read-only dry run (works as your user and inside a toolbox). Lists:
  - components ready to refresh under **would sync**;
  - precautionary components held back under **skipped by precaution**, with the
    reason and `--include-risky` hint;
  - components with manual edits on disk (`sha256` differs from the install
    record) under **skipped**, with `--overwrite-modified` hint;
  - count of already up-to-date components.
- **`cst sync apply [--include-risky] [--overwrite-modified]`** (host only):
  runs user-level refreshes directly and elevates once via `sudo` only if any
  root-owned component needs refreshing. (When `--include-risky` runs
  `bluetooth:ldac`, `cst bluetooth apply --yes` may itself invoke `sudo` if
  `/etc/bluetooth/main.conf` or `bluetooth.service` needs changes.) A second run
  when up to date runs zero module commands and never invokes `sudo`.

## Safe vs precautionary (`--include-risky`)

| Safety | Components | Command run | Why precautionary (if risky) |
|---|---|---|---|
| Safe (default) | `btrfs:core`, `btrfs:alerts`, `btrfs:below`, `btrfs:report-launcher` | `cst btrfs install` | — |
| Safe (default) | `btrfs:snapshots` | `cst btrfs snapshots refresh` | Refreshes snapshot/alert helpers and units without touching subvolumes, `~/Bulk`, or config |
| Safe (default) | `btrfs:docker-gc` | `cst btrfs docker-gc enable` | — |
| Safe (default) | `btrfs:iocost` | `cst btrfs iocost apply` | — |
| Safe (default) | `cpu:core`, `cpu:gamemode`, `cpu:toolbox` | `cst cpu install` | — |
| Safe (default) | `cpu:docker-slice` | `cst cpu docker-slice install` | — |
| Safe (default) | `cpu:irq` | `cst cpu irq apply` | — |
| Safe (default) | `desktop:psd` | `cst desktop psd apply --user $USER` | — |
| Safe (default) | `toolchains:auto` | `cst toolchains auto on` | — |
| Safe (default) | `toolchains:sccache` | `cst toolchains config apply` | — |
| Safe (default) | `toolchains:tmpfs` | `cst toolchains tmpfs apply` | — |
| Safe (default) | `vm:backup-timer` | `cst vm backup-timer on` | — |
| Precautionary (`--include-risky`) | `btrfs:top-mount` | `cst btrfs snapshots setup --yes` | Full snapshots setup (subvolume conversion check, top-mount units, `rpm-ostree`) |
| Precautionary (`--include-risky`) | `btrfs:degraded-boot` | `cst btrfs degraded-boot install` | Modifies boot `initramfs` overlay (`rpm-ostree initramfs-etc`) |
| Precautionary (`--include-risky`) | `swap:core` | `cst swap apply` | Edits `/etc/crypttab` & `/etc/fstab`, restarts `uresourced`, may reactivate `swap0` |
| Precautionary (`--include-risky`) | `bluetooth:ldac` | `cst bluetooth apply --yes` | Restarts `bluetooth.service` and `wireplumber` (briefly drops Bluetooth devices and audio) |

## Generated files, hand edits, and system defaults

- **Generated files and `toolchains/rustc-wrapper`**: CST checks deterministic
  generated unit/config files (`toolchains:auto`, `cpu:core` `ntsync`/HWP
  drop-ins, `cpu:toolbox`, `cpu:irq` drop-in, `vm:backup-timer` timer) and
  parameterized files whose install values were recorded (`toolchains/rustc-wrapper`,
  `toolchains:tmpfs`, `vm:backup-timer` service, `cpu:irq` env). Legacy
  records without recorded parameters are left alone instead of causing false
  positives.
- **Hand-edited files**: if an installed file differs on disk from its install
  record, `cst sync` skips that component unless `--overwrite-modified` is
  passed so manual adjustments are never silently overwritten.
- **Parameterised units (`toolchains:tmpfs`)**: recorded `size_bytes` prevents
  false drift when custom `--size` mounts exist; if the unit template itself is
  updated later, `cst sync apply` invokes `cst toolchains tmpfs apply` under the
  current `toolchains:tmpfs` policy.
- **Scope**: `cst sync` covers file-backed installed components tracked in
  `installed.json`. Host-level kernel/boot policies such as
  `cst btrfs commit-interval apply`, `cst vm tune apply`, or `cst cpu vm-pin`
  are applied explicitly and surfaced by [cst doctor](../doctor/README.md) when
  out of sync.

See also [README.md](../README.md), [btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md),
and [AGENTS.md](../AGENTS.md).
