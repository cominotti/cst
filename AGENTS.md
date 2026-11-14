# Working on CST (for agents and people)

CST changes the workstation it runs on: boot, btrfs, systemd units, cgroups,
Bluetooth, toolchain caches. These rules keep changes safe and the repository
consistent.

## Rules

- **Repository text is English and plain.** Code, comments, READMEs, commit
  messages. Write for someone who is not a kernel or btrfs expert.
- **Docs follow the code in the same change.** A behaviour change updates the
  module's README (see the index below) in the same commit.
- **plan before apply.** Every command that changes the machine has a dry run
  (`plan`, `status`, or a list mode) that changes nothing. Root commands call
  sudo by themselves and refuse to run inside a toolbox; they print the host
  command instead.
- **Tests never touch the real host.** No sudo, no real systemd, no real
  libvirt, no writes outside temporary folders. Use the fakes and stubs the
  existing tests use.
- **Never walk `/`** (`find /`, `fd . /`, ...). Inside a toolbox `/run/host`
  is the whole host, including network shares.
- **CI is the gate.** Work on a branch and open a pull request; `main` accepts
  squash merges with a green `CI Status` (fast gates, both E2E tiers) and a
  green SonarCloud check. Never weaken a check to make it pass.

## Index: where each part is documented

This file holds the rules for the whole repository. Each module's README
holds what is specific to it: what it changes on the machine, its knobs, how
to undo it, and the reasons behind its defaults. Read the module's README
before changing that module.

| Area | Document |
|---|---|
| The `cst` command, recommended order, undo, adapting to another PC | [README.md](README.md) |
| Storage: disk health, stalls, writeback, Docker volumes, degraded boot | [btrfs/README.md](btrfs/README.md) |
| Snapshots, `~/Bulk`, NAS backup of `/home` | [btrfs/SNAPSHOTS.md](btrfs/SNAPSHOTS.md) |
| CPU: game profile, VM/toolbox/Docker priority, E-cores | [cpu/README.md](cpu/README.md) |
| Swap and memory | [swap/README.md](swap/README.md) |
| Bluetooth audio | [bluetooth/README.md](bluetooth/README.md) |
| Toolchain caches, cargo/uv settings, sccache wrapper | [toolchains/README.md](toolchains/README.md) |
| The libvirt VM: backups, virtual hardware, NOCOW folders | [vm/README.md](vm/README.md) |
| `cst doctor` checks | [doctor/README.md](doctor/README.md) |
| Docker E2E tests and the tier 2 opt-in | [tests/e2e/README.md](tests/e2e/README.md) |

## Commands

Run from the repository root.

```
python3 -I -B tests/run_unit.py -v            # all unit tests, socket as stdin (what CI runs)
python3 -B -m unittest vm.tests.test_vm -v    # one module; or vm.tests.test_vm.PartsTest.test_disks_nvram_tpm
invowk cmd test lint                          # bash -n + shellcheck -S warning (falls back to the devbox toolbox)
invowk validate cst.invowkmod && invowk audit cst.invowkmod
invowk cmd test e2e tier1                     # Docker, Fedora 45 + systemd; needs no opt-in
```

- Before a pull request, run the first, third and fourth lines.
- A single-module run doesn't go through `tests/run_unit.py`. It skips the socket-stdin gate (see "Test gotchas"), so the full suite still has to pass.
- A new shell entry point has to be added to the `test lint` script list in `cst.invowkmod/invowkfile.cue`. Until then it isn't linted.
- Don't run E2E tier 2 on a workstation. It starts a `--privileged` container that can see the machine's disks (`CST_E2E_ALLOW_PRIVILEGED=1`). CI runs it.
- CI runs the unit tests as a non-root user named `ci`. Tests must not depend on the developer's home or groups.

## How the code fits together

- **`cst` only dispatches.** `cst MODULE ...` runs `<module>-setup.sh`.
  - The module list is in the `MODULES` and `SCRIPT` arrays at the top of `cst`.
  - It sets `CST_PROG` so that help text reads `cst MODULE`.
  - `cst doctor` runs `doctor/cst-doctor`. `cst status` loops over every module.
- **Each module has two parts.**
  - The bash entry point `<module>-setup.sh` handles plan/apply/install/uninstall/status.
  - The parts it installs live in the `<module>/` folder: helper programs, systemd `units/`, `sysctl/`, `tmpfiles/` and udev rules. `install` copies them to `/usr/local/{bin,sbin}`, `/etc/systemd/system` and other system folders.
  - The copy that runs on the machine can therefore be older than the repository. A change to a helper reaches the machine only when the module's `install` runs again.
- **Most helpers are Python scripts with no `.py` extension.** Examples: `btrfs/btrfs-bulk`, `vm/cst-vm`, `doctor/cst-doctor`, `toolchains/cst-toolchains`.
  - Most tests load them with `importlib.machinery.SourceFileLoader` (see `<module>/tests/test_*.py`).
  - Tests call the functions directly, with fakes for `virsh`, `systemctl`, sudo, restic and similar tools.
- **Toolbox vs host.** The scripts detect a container through `/run/.containerenv` or `/.dockerenv`.
  - Inside one, read-only commands still work, sometimes by moving to the host with `flatpak-spawn --host`.
  - Root commands refuse to run there. Keep that split when adding commands.
- **`doctor/cst-doctor` is a read-only check across all modules.** It prints ok/info/WARN/ERROR lines.
  - New module behaviour that can go wrong usually also needs a doctor check, documented in `doctor/README.md`.
- **CI** (`.github/workflows/ci.yml`) runs the same `invowk cmd ...` tasks.
  - `invowk` is pinned by SHA256 and verified with cosign in `.github/actions/setup-invowk`.
  - Change the version with `invowk cmd ci pin-invowk <tag>`, not by editing the hashes.
- **Machine-specific state lives outside the repository.**
  - Knobs: `/etc/cst/vm.conf` (root) and `~/.config/cst/toolchains.conf` (user). The defaults are in the scripts and in `toolchains/toolchains.conf`.
  - The VM backup index is `/var/lib/cst/vm-backups.json`.
  - User units go in `~/.config/systemd/user`.
  - "Adapting CST to another PC" in README.md lists what is specific to this machine.

## Test gotchas

### bash reads ~/.bashrc when stdin is a socket

bash treats "SHLVL unset and stdin is a socket" as a start by sshd/rshd and
reads `~/.bashrc`, even for `bash -c`. Tests that run bash with a clean
environment (`env={"PATH": ...}`) have no SHLVL, and they inherit stdin from
whoever started the run. Agents and task runners sometimes give that run a
socket. The developer's `~/.bashrc` then prints into the test's stderr, and
the test fails only in those environments.

**Rule:** a subprocess call in a test that runs bash or sh passes `stdin=`
or `input=`. Use `stdin=subprocess.DEVNULL` when the command reads nothing:

```python
subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
               stdin=subprocess.DEVNULL)
```

Two gates enforce it:

- `tests/test_subprocess_hygiene.py` (static): fails on a call to
  `subprocess.run/Popen/call/check_call/check_output` with an argument list
  that starts with bash/sh, or with `shell=True`, and no `stdin=`/`input=`.
- `tests/run_unit.py` (at run time): runs the whole suite with a socket as
  stdin, so the failure shows up on every run. `invowk cmd test unit` and CI
  use it.
