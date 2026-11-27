# SPDX-License-Identifier: MPL-2.0
"""Linux/toolbox host adapter for `cst btrfs compress-force`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
import os
from typing import Final, Protocol

from btrfs.cache.adapters.mounts import loaded_mount_units, reload_failure
from btrfs.compress.application import (
    LayerRun,
    LayerStep,
    RootOptionChangeOps,
    reload_pending_line,
    run_root_option_change,
)
from btrfs.compress.domain import (
    KARGS_SETTLE_POLL_S,
    KARGS_SETTLE_TIMEOUT_S,
    KARGS_STILL_RUNNING,
    PROC_CMDLINE_PATH,
    PROC_MOUNTS_PATH,
    ROOT_ANCHOR_TARGETS,
    TOP_MOUNT_UNIT_NAME,
    CompressSnapshot,
    TopMountInfo,
    parse_top_mount_unit,
    transaction_running,
)
from btrfs.compress.domain.root_option import RootOptionAction, RootOptionPlanMixin
from btrfs.compress.ports import TOP_MOUNT_ERRORS, CompressHostPort
from btrfs.compress.presenter import (
    RootOptionPlanView,
    RootOptionStatusSpec,
    format_seam_notice,
    render_root_option_plan_lines,
    render_root_option_status,
)
from cst_core.adapters import SystemClockAdapter, SystemctlAdapter
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget, SystemdUnitVerifier
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, checkout_commit, checkout_root
from cst_core.adapters.terminal import confirm as terminal_confirm
from cst_core.adapters.zipapp import entry_payload
from cst_core.application.install import InstallRequest, InstallService
from cst_core.domain.install import INSTALL_MANIFEST, Component
from cst_core.domain.record import ROOT_RECORD
from cst_core.domain.values import MINUTE, ExitAndLines
from cst_core.ports import (
    ClockPort,
    CommandFailed,
    CommandRunnerPort,
    ExecutionContextPort,
    FileSystemPort,
    OutputMode,
    SystemctlHost,
    WriteFailed,
)

_DEFAULT_PROC_DIR: Final[str] = "/proc"
_FSTAB_CATCH_UP_TIMEOUT_S: Final = MINUTE
"""How long the catch-up daemon-reload (CompressHostPort.daemon_reload) waits: a PID 1 that does not answer
must not hold a run that has nothing else to do forever (scrub-limit's catch-up reload waits as long)."""


@dataclass(frozen=True, slots=True)
class CompressAdapterConfig:
    """Entry path and optional test seams for `LinuxCompressHostAdapter`."""

    entry_file: str
    fallback_root: str = ""
    proc_dir: str = _DEFAULT_PROC_DIR
    host_root: str = ""


def _findmnt_btrfs_to_proc_lines(findmnt_out: str) -> str:
    lines: list[str] = []
    for raw in findmnt_out.splitlines():
        match raw.strip().split(None, 2):
            case [source, target, opts]:
                lines.append(f"{source} {target} btrfs {opts} 0 0\n")
    return "".join(lines)


class LinuxCompressHostAdapter:
    """Implements `CompressHostPort` using `cst_core` primitives."""

    def __init__(
        self,
        config: CompressAdapterConfig,
        *,
        ctx: ExecutionContextPort | None = None,
        runner: CommandRunnerPort | None = None,
        fs: FileSystemPort | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._entry_file = config.entry_file
        self._fallback_root = config.fallback_root
        self._proc_dir = config.proc_dir.rstrip("/") or _DEFAULT_PROC_DIR
        self._host_root = config.host_root.rstrip("/")
        self._ctx = HostExecutionContext() if ctx is None else ctx
        self._runner = SubprocessCommandRunner() if runner is None else runner
        self._fs = LocalFileSystemAdapter() if fs is None else fs
        self._clock = SystemClockAdapter() if clock is None else clock
        self._systemctl = SystemctlAdapter(self._runner, on_host=SystemctlHost.LOCAL)

    def _etc_file(self, *parts: str) -> str:
        base = os.path.join(self._host_root, "etc") if self._host_root else self._ctx.etc_dir()
        return os.path.join(base, *parts)

    def _ostree_booted_path(self) -> str:
        if self._host_root:
            return os.path.join(self._host_root, "run", "ostree-booted")
        return self._ctx.host_run_dir("ostree-booted")

    def _proc_file(self, name: str, default_path: str) -> str:
        if self._proc_dir != _DEFAULT_PROC_DIR:
            return os.path.join(self._proc_dir, name)
        if self._host_root:
            return os.path.join(self._host_root, "proc", name)
        return default_path

    def is_ostree(self) -> bool:
        """Return True on an rpm-ostree booted host."""
        return self._fs.exists(self._ostree_booted_path())

    def root_uuid(self) -> str:
        """Return the root btrfs filesystem UUID, or "" when unknown: the btrfs of /sysroot, else of /, else of
        /var (`ROOT_ANCHOR_TARGETS`, the order of `pick_root_mounts`, cst doctor and btrfs-setup); else the top
        mount unit's."""
        for target in ROOT_ANCHOR_TARGETS:
            res = self._runner.run(
                ["findmnt", "-rn", "-t", "btrfs", "-o", "UUID", "--target", target],
                on_host=not self._host_root,
                timeout=15,
                locale_c=True,
                output=OutputMode.CAPTURE,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.splitlines()[0].strip()
        top = self.read_top_mount()
        return top.uuid if top is not None else ""

    def read_fstab(self) -> str | None:
        """Read `/etc/fstab` byte-transparently, or return None when absent."""
        return self._fs.read_text(self._etc_file("fstab"), None)

    def read_proc_mounts(self) -> str | None:
        """Read live root btrfs mounts from test `/proc/mounts` or host `findmnt`."""
        if self._proc_dir != _DEFAULT_PROC_DIR or self._host_root:
            return self._fs.read_text(self._proc_file("mounts", PROC_MOUNTS_PATH), None)
        res = self._runner.run(
            ["findmnt", "-rn", "-t", "btrfs", "-o", "SOURCE,TARGET,OPTIONS"],
            on_host=True,
            timeout=15,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        if res.returncode == 0 and res.stdout.strip():
            return _findmnt_btrfs_to_proc_lines(res.stdout)
        return self._fs.read_text(self._proc_file("mounts", PROC_MOUNTS_PATH), None)

    def read_proc_cmdline(self) -> str | None:
        """Read `/proc/cmdline`."""
        return self._fs.read_text(self._proc_file("cmdline", PROC_CMDLINE_PATH), None)

    def read_kargs(self) -> tuple[bool, str, str]:
        """Run `rpm-ostree kargs` on the host and return `(ok, stdout, err)`."""
        res = self._runner.run(
            ["rpm-ostree", "kargs"],
            on_host=True,
            timeout=30,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return (res.returncode == 0, res.stdout, res.stderr.strip() or res.stdout.strip())

    def read_settled_kargs(self) -> tuple[bool, str, str]:
        """`read_kargs` once `rpm-ostree status --json` names no running transaction, asking every
        KARGS_SETTLE_POLL_S for at most KARGS_SETTLE_TIMEOUT_S: `(False, "", why)` when one still runs then or
        rpm-ostree cannot say."""
        deadline = self._clock.monotonic() + KARGS_SETTLE_TIMEOUT_S
        while True:
            res = self._runner.run(
                ["rpm-ostree", "status", "--json"],
                on_host=True,
                timeout=30,
                locale_c=True,
                output=OutputMode.CAPTURE,
            )
            if res.returncode != 0:
                return False, "", res.failure("rpm-ostree status")
            running = transaction_running(res.stdout)
            if running is None:
                return False, "", "rpm-ostree status --json named no transaction state"
            if not running:
                return self.read_kargs()
            if self._clock.monotonic() >= deadline:
                return False, "", KARGS_STILL_RUNNING
            self._clock.sleep(KARGS_SETTLE_POLL_S)

    def read_top_mount(self) -> TopMountInfo | None:
        """Read installed `var-mnt-btrfs\\x2dtop.mount`, or return None when not installed."""
        text = self._fs.read_text(self._etc_file("systemd", "system", TOP_MOUNT_UNIT_NAME), None)
        if text is None:
            return None
        return parse_top_mount_unit(text)

    def write_fstab(self, text: str, *, put_back: bool = True) -> None:
        """Atomically write `/etc/fstab` and reload systemd units (restoring previous text on reload error, unless
        put_back is False: an undo, whose previous text is the one it undoes)."""
        path = self._etc_file("fstab")
        previous = self._fs.read_text(path, None) if put_back else None
        self._fs.write_atomic(path, text, mode=0o644)
        try:
            self._systemctl.daemon_reload()
        except CommandFailed:
            if previous is not None:
                self._fs.write_atomic(path, previous, mode=0o644)
            raise

    def mount_units(self) -> list[dict[str, str]] | None:
        """The loaded mount units, on the host from a toolbox (cache's loaded_mount_units); none with a test root
        (CST_BTRFS_*_ROOT): no systemd loaded that root's /etc/fstab, so none of its units is behind."""
        if self._host_root:
            return []
        return loaded_mount_units(self._runner, from_toolbox=self._ctx.is_container())

    def daemon_reload(self) -> str | None:
        return reload_failure(self._runner, timeout=_FSTAB_CATCH_UP_TIMEOUT_S)

    def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
        """Update `rootflags=` in a single `rpm-ostree kargs` invocation."""
        if not edits:
            return True, ""
        argv = ["rpm-ostree", "kargs"]
        for old_tok, new_tok in edits:
            argv.append(f"--delete={old_tok}")
            argv.append(f"--append={new_tok}")
        res = self._runner.run(argv, timeout=60, locale_c=True, output=OutputMode.CAPTURE)
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip())

    def remount(self, target: str, option: str) -> tuple[bool, str]:
        """Remount `target` with `option`."""
        res = self._runner.run(
            ["mount", "-o", f"remount,{option}", target],
            timeout=30,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip())

    def update_top_mount(self, uuid: str, options: str, *, put_back: bool = True) -> None:
        """Re-render and verify `btrfs:top-mount` through `InstallService`, reload systemd, and mark done (the
        previous unit put back on an error, unless put_back is False: an undo)."""
        root = checkout_root(self._entry_file) or self._fallback_root
        prev_top = self.read_top_mount() if put_back else None
        installer = InstallService(
            LocalInstallTarget(),
            lambda e: entry_payload(root, e),
            record=JsonInstallRecord(),
            commit=checkout_commit(root),
            verifier=SystemdUnitVerifier(self._runner),
        )
        entries = [e for e in INSTALL_MANIFEST if e.component == Component.BTRFS_TOP_MOUNT]
        req = InstallRequest(values={"UUID": uuid, "OPTIONS": options}, as_root=True, root=self._host_root)
        rec_path = f"{self._host_root}{ROOT_RECORD}" if self._host_root else ROOT_RECORD
        try:
            list(installer.install(entries, req))
            installer.mark(rec_path, Component.BTRFS_TOP_MOUNT)
            self._systemctl.daemon_reload()
        except TOP_MOUNT_ERRORS:
            # Put the previous unit back only when something was written (a refusal writes nothing).
            if prev_top is not None and self.read_top_mount() != prev_top:
                prev_req = InstallRequest(
                    values={"UUID": prev_top.uuid or uuid, "OPTIONS": prev_top.options},
                    as_root=True,
                    root=self._host_root,
                )
                list(installer.install(entries, prev_req))
                with suppress(OSError, WriteFailed):
                    installer.mark(rec_path, Component.BTRFS_TOP_MOUNT)
            raise

    def confirm(self, prompt: str) -> bool:
        """Ask interactive confirmation on the terminal."""
        return terminal_confirm(prompt)


def make_root_option_host(
    env: ManagedCliEnv, *, proc_seam_var: str, root_seam_var: str
) -> tuple[CompressHostPort, str, dict[str, str]]:
    """Build `LinuxCompressHostAdapter` from `ManagedCliEnv` and test seam variable names."""
    env_map = dict(os.environ if env.env is None else env.env)
    seam_proc = env_map.get(proc_seam_var, "")
    host_root = env_map.get(root_seam_var, "")
    cfg = CompressAdapterConfig(
        entry_file=env.entry_file,
        fallback_root=env.fallback_root,
        proc_dir=seam_proc or "/proc",
        host_root=host_root,
    )
    return LinuxCompressHostAdapter(cfg, ctx=env.ctx, runner=env.runner), seam_proc, env_map


def read_root_option_snapshot(host: CompressHostPort) -> tuple[CompressSnapshot, bool, str]:
    """Read current root btrfs state across fstab, rpm-ostree kargs, live mounts, and top-mount."""
    is_ostree = host.is_ostree()
    kargs_ok, kargs_text, kargs_err = host.read_kargs() if is_ostree else (True, "", "")
    snap = CompressSnapshot(
        is_ostree=is_ostree,
        root_uuid=host.root_uuid(),
        fstab_text=host.read_fstab(),
        kargs_text=kargs_text if (is_ostree and kargs_ok) else None,
        proc_mounts_text=host.read_proc_mounts(),
        top_mount=host.read_top_mount(),
    )
    return snap, (True if not is_ostree else kargs_ok), kargs_err


class _RootOptionParsed(Protocol):
    @property
    def action(self) -> RootOptionAction: ...
    @property
    def error(self) -> str | None: ...
    @property
    def code(self) -> int: ...
    @property
    def yes(self) -> bool: ...


class RootOptionDriver[ParsedT: _RootOptionParsed, PlanT: RootOptionPlanMixin](Protocol):
    """Behavior hooks implemented per root btrfs mount option tool."""

    def build_plan(self, snap: CompressSnapshot, parsed: ParsedT) -> PlanT: ...
    def confirm_prompt(self, plan: PlanT) -> str: ...
    def build_steps(self, run: LayerRun, plan: PlanT) -> tuple[LayerStep, ...]: ...
    def status_spec(
        self, snap: CompressSnapshot, proc_cmdline: str | None, *, kargs_ok: bool
    ) -> RootOptionStatusSpec: ...
    def plan_view(self, plan: PlanT, prog: str, *, dry_run: bool, reload_pending: str) -> RootOptionPlanView: ...


@dataclass(frozen=True, slots=True)
class RootOptionTool[ParsedT: _RootOptionParsed, PlanT: RootOptionPlanMixin]:
    """Shared orchestrator for `status`, `plan`, `apply`, and `revert` of a root btrfs option."""

    default_prog: str
    seam_proc_var: str
    seam_root_var: str
    driver: RootOptionDriver[ParsedT, PlanT]

    def status_lines(
        self,
        snap: CompressSnapshot,
        proc_cmdline: str | None,
        *,
        kargs: tuple[bool, str],
        seam_proc: str = "",
    ) -> list[str]:
        """Render `status` lines for this option tool."""
        kargs_ok, kargs_error = kargs
        spec = self.driver.status_spec(snap, proc_cmdline, kargs_ok=kargs_ok)
        return render_root_option_status(snap, spec, kargs_ok=kargs_ok, kargs_error=kargs_error, seam_proc=seam_proc)

    def plan_lines(self, plan: PlanT, hint_prog: str, *, seam_proc: str = "", reload_pending: str = "") -> list[str]:
        """Render `plan` / action header lines (`hint_prog` non-empty enables the dry-run hint)."""
        view = self.driver.plan_view(plan, hint_prog, dry_run=bool(hint_prog), reload_pending=reload_pending)
        return render_root_option_plan_lines(replace(view, seam_line=format_seam_notice(self.seam_proc_var, seam_proc)))

    def execute(self, host: CompressHostPort, parsed: ParsedT, *, prog: str = "", seam_proc: str = "") -> ExitAndLines:
        """Run transactional `apply` or `revert` against `host`."""
        del prog
        ops = RootOptionChangeOps(
            build_plan=lambda snap: self.driver.build_plan(snap, parsed),
            has_changes=lambda p: p.has_changes,
            new_fstab_text=lambda p: p.new_fstab_text,
            confirm_prompt=self.driver.confirm_prompt,
            steps=self.driver.build_steps,
            format_plan=lambda p, said: self.plan_lines(p, "", seam_proc=seam_proc, reload_pending=said),
        )
        return run_root_option_change(host, read_root_option_snapshot(host), ops, yes=parsed.yes)

    def dispatch(
        self,
        parsed: ParsedT,
        env: ManagedCliEnv,
        *,
        host_for_env: Callable[[ManagedCliEnv], tuple[CompressHostPort, str, dict[str, str]]] | None = None,
    ) -> ExitAndLines:
        """Resolve host adapter and run the requested subcommand."""
        resolver = host_for_env or (
            lambda e: make_root_option_host(e, proc_seam_var=self.seam_proc_var, root_seam_var=self.seam_root_var)
        )
        host, seam_proc, env_map = resolver(env)
        prog = _resolve_root_prog(self.default_prog, env_map.get("CST_PROG") or "")
        if parsed.action == RootOptionAction.STATUS:
            snap, kargs_ok, kargs_err = read_root_option_snapshot(host)
            lines = self.status_lines(snap, host.read_proc_cmdline(), kargs=(kargs_ok, kargs_err), seam_proc=seam_proc)
            if reload := reload_pending_line(host):
                lines.insert(-1, f"  {reload}")
            return 0, lines
        if parsed.action == RootOptionAction.PLAN:
            snap, kargs_ok, kargs_err = read_root_option_snapshot(host)
            if not kargs_ok:
                return 1, [f"ERROR: cannot inspect rpm-ostree kargs: {kargs_err}"]
            plan = self.driver.build_plan(snap, parsed)
            pending = reload_pending_line(host) if plan.new_fstab_text is None else ""
            return 0, self.plan_lines(plan, prog, seam_proc=seam_proc, reload_pending=pending)
        return self.execute(host, parsed, prog=prog, seam_proc=seam_proc)

    def cli_spec(
        self,
        parse: Callable[[Sequence[str]], ParsedT],
        format_usage: Callable[[str], str],
        elevation_reason: str,
    ) -> ManagedCliSpec[ParsedT]:
        """Build `ManagedCliSpec` wired to `self.dispatch`."""
        return make_btrfs_option_cli_spec(
            self.default_prog,
            parse=parse,
            format_usage=format_usage,
            elevation_reason=elevation_reason,
            execute=self.dispatch,
        )


def _resolve_root_prog(default_prog: str, raw: str) -> str:
    return default_prog if raw == "cst btrfs" else (raw or default_prog)


def make_btrfs_option_cli_spec[ParsedT: _RootOptionParsed](
    default_prog: str,
    *,
    parse: Callable[[Sequence[str]], ParsedT],
    format_usage: Callable[[str], str],
    elevation_reason: str,
    execute: Callable[[ParsedT, ManagedCliEnv], ExitAndLines],
) -> ManagedCliSpec[ParsedT]:
    """Build `ManagedCliSpec` for a `cst btrfs <option>` tool with standard subcommands."""

    def _run(parsed: ParsedT, env: ManagedCliEnv, *, read_only: bool) -> ExitAndLines:
        del read_only
        return execute(parsed, env)

    return ManagedCliSpec(
        default_prog=default_prog,
        parse=parse,
        format_usage=format_usage,
        is_help=lambda p: p.action == RootOptionAction.HELP,
        parse_error=lambda p: None if p.error is None else (p.code, p.error),
        write_action=lambda p: (
            p.action.value if p.action in (RootOptionAction.APPLY, RootOptionAction.REVERT) else None
        ),
        elevation_reason=elevation_reason,
        execute=_run,
        resolve_prog=lambda raw: _resolve_root_prog(default_prog, raw),
    )
