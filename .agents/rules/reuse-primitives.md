---
description: Before writing a helper for a command, a file, a lock, a journal line, the host or toolbox split, a size, a duration, a CPU list, a settings file or a unit, use the cst_core primitive; add a missing option there, never a copy
---
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
| Run a command | `CommandRunnerPort` (`cst_core.ports`), `SubprocessCommandRunner` (`cst_core.adapters`) | `CommandResult`, `CommandFailed` in `cst_core.ports`. Tests inject a fake through the port. |
| Host or toolbox, run on the host | `HostExecutionContext` (`cst_core.adapters`), `ExecutionContextPort` | Root commands refuse inside a toolbox ([docs/architecture.md](../../docs/architecture.md#toolbox-vs-host)). |
| systemd | `SystemdPort` (`cst_core.ports`), `SystemctlAdapter` (`cst_core.adapters`) | |
| libvirt | `VirshPort` (`cst_core.ports`), `SubprocessVirshAdapter` (`cst_core.adapters`) | |
| Write a file | `cst_core.adapters.files.write_atomic` | Unique temp file, exact mode, fsync of the file and its folder unless `durable=False`, owner `"caller"` or `"root"`. `write_bytes_atomic` is the installer's durable root-owned write. |
| Create folders | `cst_core.adapters.files.ensure_dir` | The folders it creates get exactly their mode (the umask does not reduce it). |
| Read a file | `cst_core.adapters.files`: `read_text`, `read_int`, `read_bytes` | `read_text` decodes leniently (bad bytes replaced): not for a file written back. For that, `LocalFileSystemAdapter.read_text` (byte-transparent). The two policies are on purpose. `FileSystemPort` is the port. |
| Install a file of CST | a row of `INSTALL_MANIFEST` (`cst_core.domain.install`); from a setup script `cst-install.bash`: `cst_install`, `cst_install_as`, `cst_install_text`, `cst_installed_dests` | Never copy a file by hand ([docs/architecture.md](../../docs/architecture.md#each-module-has-two-parts)). |
| A link at a destination | `cst_core.domain.install.link_read_through`, `cst_core.adapters.files.owned_file_state_at` | Replaced at a root destination, judged by its target in a home. |
| Managed lines and files, safe paths | `cst_core.domain.files`: `MarkedLineSpec`, `owned_file_state`, `inspect_managed_file`, `effective_lines`, `checked_path` | |
| One run at a time | `cst_core.adapters.locks.take_lock` | A lock file it creates gets exactly its mode; refuses a link. |
| A journal line | `cst_core.adapters.journal.log` | `<prio>tag: msg`, flushed. |
| A KEY=VALUE settings file | `cst_core.domain.text.parse_shell_assignments`, `describe_problems` | The one reader: bash's rules for files bash also sources, `bash_sourced=False` for files only Python reads. Problem lines set nothing and carry their line number. |
| Lines, first line, yes/no | `cst_core.domain.text`: `file_lines` (as bash's `read -r` sees them), `first_line`, `is_yes` | |
| Sizes, durations, CPU lists, modes | `cst_core.domain.values`: `ByteSize`, `Duration`, `CpuSet`, `OctalMode`, `KIB`, `MIB`, `GIB`, `format_bytes_human`, `format_bytes_gib_mib`, `format_bytes_decimal` | |
| A systemd unit or timer text | `cst_core.domain.units`: `render_unit_file`, `render_timer` | |
| Usage errors, refusals, exit status | `cst_core.domain.errors`: `Usage`, `Refused`, `shell_exit_status` | `shell_exit_status`: 128+N for a child killed by signal N. |
| libvirt XML | `cst_core.domain.libvirt_xml.keep_prefixes` | |
| NOCOW | `cst_core.adapters.nocow`: `set_nocow`, `inode_flags` | |
| The program's own path, the checkout, a sibling program, the installed bytes | `cst_core.adapters.runtime`: `checkout_root`, `program_path`, `program_argv`, `sibling_program`, `install_payload` | Never compute paths from `__file__` by hand. |
| Signals and locale at start | `cst_core.adapters.runtime`: `restore_default_signals`, `restore_lc_ctype` | `restore_default_signals` (SIGPIPE back to its default) only in `cst`, the installer, `cst swap` and commands that only read: `cst doctor`, `btrfs-stall-report`, `cst vm` status/list/plans, `cst toolchains` status/plans, `cst cpu vm-pin` status/plan, `cpu-watch report`. Never in a command with cleanup (a backup, a prune, bulk, snapshots, the daemons): a closed pipe must not cut the cleanup short. `restore_lc_ctype` gives the modules `cst` runs the caller's LC_CTYPE back. |
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
  `LITERAL_BUDGETS`), which only goes down: a new copy fails. Every public
  name of cst_core's shared layers must have a user outside its file
  (`CORE_NAMES_WITHOUT_USER` lists today's exceptions).

See [docs/architecture.md, "Static gates beyond lint and types"](../../docs/architecture.md#static-gates-beyond-lint-and-types).
