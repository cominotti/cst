# Working on CST (for agents and people)

CST changes the workstation it runs on: boot, btrfs, systemd units, cgroups,
Bluetooth, toolchain caches. These rules keep changes safe and the repository
consistent.

## Rules

- **Repository text is English and plain.** Code, comments, READMEs, commit
  messages. Write for someone who is not a kernel or btrfs expert.
- **Docs follow the code in the same change.** A behaviour change updates the
  module's README (README.md, btrfs/README.md, btrfs/SNAPSHOTS.md,
  cpu/README.md, swap/README.md, bluetooth/README.md, toolchains/README.md,
  doctor/README.md, vm/README.md) in the same commit.
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
  use it. Run it locally with `python3 -I -B tests/run_unit.py -v`.

## Checks before a pull request

```
python3 -I -B tests/run_unit.py          # unit tests, socket as stdin
invowk cmd test lint                     # bash -n + shellcheck
invowk validate && invowk audit
```
