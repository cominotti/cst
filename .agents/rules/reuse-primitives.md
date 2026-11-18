---
description: Before writing a helper for a command, a file, a lock, a journal line, the host or toolbox split, a size, a duration, a CPU list, a settings file or a unit, use the cst_core primitive; add a missing option there, never a copy
---
<!-- SPDX-License-Identifier: MPL-2.0 -->
<!-- global rule: loaded for every request -->

# Reuse before you write

A concept more than one module needs lives once, in `cst_core`. Before you write
code that runs a command, reads or writes a file, creates a folder, takes a lock,
logs to the journal, tells the host from a toolbox, parses a size, a duration, a
CPU list or a KEY=VALUE settings file, or renders a systemd unit, find the
primitive below and import it from the module named.

**If it lacks an option you need, add the option to the primitive, with its
tests (`tests/test_cst_core.py`, `tests/test_cst_core_shared.py`), in the same
change. Do not copy it into your module.** A copy drifts: the next fix lands in
one place only.

## Need -> primitive

| Need | Use | Notes |
|---|---|---|
| Run a command | `CommandRunnerPort` (`cst_core.ports`), `SubprocessCommandRunner` (`cst_core.adapters.runner`) | `CommandResult`, `CommandFailed` in `cst_core.ports`. Tests inject a fake through the port. Every option is off by default and a call names what it needs: `timeout`, `input_text`, `env`/`unset`/`base_env`, `cwd`, `locale_c` (a command whose answers are parsed: not an `LC_ALL` literal), `new_session`, `on_host` (`flatpak-spawn --host` inside a container), `stdin`, `output`, `decode_errors` (by default bytes that are not UTF-8 become U+FFFD; `"strict"` raises instead: docker-volumes-nocow asks for it, for docker's and systemctl's answers), `nice`. A command that is missing, cannot start or times out comes back with `fault` and `cause` set: a module keeps its own message from them (a 2-line binding), never its own `subprocess.run`. Outside it on purpose: streaming readers (`subprocess.Popen`: docker events, restic, the cpu watch) and the dispatcher's foreground run. Not moved yet, counted in `PRIMITIVE_BUDGETS` (`tests/test_architecture.py`), never above their entry: bulk, docker-volumes-nocow, stall-report, cpu's `pgrep` and the restorecon call in `cst_core.adapters.files`. With `on_host` inside a container, `env` values go on `flatpak-spawn`'s command line: never a secret there. |
| Host or toolbox, run on the host | `HostExecutionContext` (`cst_core.adapters.runner`), `ExecutionContextPort`, `ContainerProbePort` | The one detection in Python: the dispatcher, doctor's `Host` and toolchains ask it (a module's test override, such as `CST_DOCTOR_CONTAINER`, stays in front of it). Root commands refuse inside a toolbox ([docs/architecture.md](../../docs/architecture.md#toolbox-vs-host)). |
| rpm-ostree's deployments | `cst_core.domain.ostree.query_status` (`OstreeStatus`: every deployment, the booted one, the next boot's) | Takes a run function (`argv -> (rc, stdout, stderr)`); doctor passes its `Host.run`. vm's `booted_deployment` passes its `run` (and records None for any failure). The bash copies in the btrfs scripts stay until they are Python. |
| systemd | `SystemdPort` (`cst_core.ports`), `SystemctlAdapter` (`cst_core.adapters`) | |
| libvirt | `VirshPort` (`cst_core.ports`), `SubprocessVirshAdapter` (`cst_core.adapters.virsh`) | virsh in the C locale through a `CommandRunnerPort`: `on_host`, `read_only` (`-r`); output that is not UTF-8 becomes U+FFFD. cpu and vm use it directly, doctor over its host's runner (`Host.virsh`). |
| Write a file | `cst_core.adapters.files.write_atomic` | Unique temp file, exact mode (or `mode="umask"`: what `open(path, "w")` gives a new file), fsync of the file and its folder (`durable="file"`: the file only; `durable=False`: none), owner `"caller"` or `"root"`, restorecon as root unless `relabel=False`. A writer moved onto it keeps its old mode, durability and labelling through these options. `write_bytes_atomic` is the installer's durable root-owned write. `locale_bytes`: a text encoded as `open(path, "w")` would write it, for a writer that kept the locale's encoding. |
| A desktop alert | `cst_core.adapters.notify.Notifier` (`send(urgency, app, title, body)`) | Runs btrfs-notify (the manifest's dest) with a 60 s timeout; when it is missing or fails, a journal line and the alert kept in the pending folder for cst doctor (in the format, file name and lock of btrfs-notify). Tests point `program` and `pending` into a temporary folder. |
| A state file another CST program reads | `cst_core.domain.contracts.state_format(key)` (a `StateFormat`: path, version, writers, readers) | Register the format there and import its path and version in the writer and every reader. A file with no version reads as version 0. |
| Value types | `cst_core.domain.values.Json` (a value JSON carries as it is), `cst_core.ports.UnitName` (a systemd unit name), `cst_core.ports.VmName` (a libvirt domain name) | NewType and Literal cost nothing at run time; use them where a value crosses a port. doctor has its own `HostPath`/`ViewPath`, snapshot its `SubvolPath`. |
| Create folders | `cst_core.adapters.files.ensure_dir` | The folders it creates get exactly their mode (the umask does not reduce it). |
| Read a file | `cst_core.adapters.files`: `read_text`, `read_int`, `read_bytes` | `read_text` decodes leniently (bad bytes replaced): not for a file written back. For that, `LocalFileSystemAdapter.read_text` (byte-transparent). The two policies are on purpose. `FileSystemPort` is the port. |
| A count kept across boots | `cst_core.adapters.files.CounterFile` | `count()` is 0 when the file is absent or holds no number; `save()` writes it atomically and durably (the file and its folder), so a reset right after still counts; `remove()` (FileNotFoundError when there is none) and `forget()` (no error when there is none). bulk's boot attempts and docker-volumes-nocow's use it. |
| Install a file of CST | a row of `INSTALL_MANIFEST` (`cst_core.domain.install`); from a setup script `cst-install.bash`: `cst_install`, `cst_install_as`, `cst_install_text` (`cst_install_text_plan` to only judge), `cst_installed_dests` | Never copy a file by hand ([docs/architecture.md](../../docs/architecture.md#each-module-has-two-parts)). |
| A link at a destination | `cst_core.domain.install.link_read_through`, `cst_core.adapters.files.owned_file_state_at` | Replaced at a root destination, judged by its target in a home. |
| A unit file CST writes (masked or not) | `cst_core.adapters.files.unit_file_state_at`, `is_masked_unit`; `cst_core.domain.units.masked_refusal` | A link to `/dev/null` is `masked` (`systemctl mask`): refuse, never unmask. |
| Managed lines and files, safe paths | `cst_core.domain.files`: `MarkedLineSpec`, `owned_file_state`, `inspect_managed_file`, `effective_lines`, `checked_path` | |
| One run at a time | `cst_core.adapters.locks.take_lock` | A lock file it creates gets exactly its mode; refuses a link; `timeout` (with `wait=True`) gives up after that many seconds. |
| A journal line | `cst_core.adapters.journal.log` | `<prio>tag: msg`, flushed. |
| Ask yes or no on the terminal | `cst_core.adapters.terminal.confirm` | Prints the question and ` [y/N] `; a yes is `is_yes`. `Refused` ("apply asks for confirmation: run it in a terminal") when stdin is not a terminal. vm's apply and nocow and cpu's vm-pin apply ask with it. docker-gc's `TerminalConsole.ask` is still a copy of its own (an EOF there is a no; here it raises). |
| A KEY=VALUE settings file | `cst_core.domain.text.parse_shell_assignments`, `describe_problems` | The one reader: bash's rules for files bash also sources, `bash_sourced=False` for files only Python reads. Problem lines set nothing and carry their line number. `parse_snapshots_conf`: the same for `snapshots.conf` only, which also reads the `printf %q` lines an older CST wrote there. |
| Lines, first line, yes/no | `cst_core.domain.text`: `file_lines` (as bash's `read -r` sees them), `first_line`, `is_yes` | |
| Fields a bash program writes with `printf %q` and reads back with `eval` | `cst_core.domain.text`: `printf_q` (bash's bytes, checked against bash), `printf_q_words` | The game profile's `game.state` and `game.intended`, which an installed bash copy of an older CST also reads. |
| Sizes, durations, CPU lists, modes | `cst_core.domain.values`: `ByteSize`, `Duration`, `CpuSet`, `OctalMode`, `MIB`, `GIB`, `format_bytes_human`, `format_bytes_gib_mib`, `format_bytes_decimal` | toolchains' settings parse sizes and durations with `ByteSize.parse_iec` and `Duration.parse_float` (its own `ConfigError`, same words); cpu reads its core types' CPU lists with `CpuSet`. |
| A systemd unit or timer text | `cst_core.domain.units`: `render_unit_file`, `render_timer` | |
| Usage errors, refusals, exit status | `cst_core.domain.errors`: `Usage`, `Refused`, `shell_exit_status` | `shell_exit_status`: 128+N for a child killed by signal N. |
| libvirt XML | `cst_core.domain.libvirt_xml.keep_prefixes` | |
| NOCOW | `cst_core.adapters.nocow`: `set_nocow`, `inode_flags` | |
| The program's own path, the checkout, a sibling program, the installed bytes | `cst_core.adapters.runtime`: `checkout_root`, `program_path`, `program_argv`, `sibling_program`, `install_payload` | Never compute paths from `__file__` by hand. |
| Signals and locale at start | `cst_core.adapters.runtime`: `restore_default_signals`, `undo_python_start` | `restore_default_signals` (SIGPIPE back to its default) only in `cst`, the installer, `cst swap` and commands that only read: `cst doctor`, `btrfs-stall-report`, `cst vm` status/list/plans, `cst toolchains` status/plans, `cst cpu vm-pin` status/plan, `cpu-game-profile state`, `cpu-watch report` and its help, `docker-gc status` and `run --dry-run`, `chrome-cache status` and `plan`. Never in a command with cleanup (a backup, a prune, bulk, snapshots, the daemons): a closed pipe must not cut the cleanup short. `undo_python_start` (in `cst`, `cst swap` and the installer) also gives the commands they run the caller's LC_CTYPE back. |
| How `cst` runs a module | `cst_core.adapters.dispatch` (`LocalCstDispatchAdapter`) | `exec`, or in the foreground with bash's signal rules. |
| Zipapps, import analysis | `cst_core.adapters.zipapp`, `cst_core.domain.imports` | |
| The installer's use case | `cst_core.application.install` (`InstallService`) | |

## The gates that catch a copy

- `tests/test_duplication.py`: clones, and the same name defined in several
  files (Python top-level names and shell functions).
- `tests/test_architecture.py`: no I/O in `domain` or `application`; and
  `PRIMITIVE_OWNERS` / `PRIMITIVE_LITERALS`, which name the owner of each
  primitive (running a command, replacing or syncing a file, temporary files,
  locks, the NOCOW ioctl) and of the literals that betray a copy of one
  (`/run/.containerenv`, `/.dockerenv`, `flatpak-spawn`, `qemu:///system`,
  `LC_ALL`, `restorecon`, `systemd-escape`). A call or a literal anywhere
  else counts against its package's budget (`PRIMITIVE_BUDGETS`,
  `LITERAL_BUDGETS`), which it may not go above: a new copy fails. Every public
  name of cst_core's shared layers must have a user outside its file
  (`CORE_NAMES_WITHOUT_USER` lists today's exceptions).

See [docs/architecture.md, "Static gates beyond lint and types"](../../docs/architecture.md#static-gates-beyond-lint-and-types).
