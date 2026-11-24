# SPDX-License-Identifier: MPL-2.0
"""Linux/toolbox host adapter for `cst btrfs compress-force`."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
import os
from typing import Final

from btrfs.compress.domain import (
    PROC_CMDLINE_PATH,
    PROC_MOUNTS_PATH,
    TOP_MOUNT_UNIT_NAME,
    TopMountInfo,
    parse_top_mount_unit,
)
from cst_core.adapters import SystemctlAdapter
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget, SystemdUnitVerifier
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import checkout_commit, checkout_root
from cst_core.adapters.terminal import confirm as terminal_confirm
from cst_core.adapters.zipapp import entry_payload
from cst_core.application.install import InstallRequest, InstallService
from cst_core.domain.errors import Refused
from cst_core.domain.install import INSTALL_MANIFEST, Component
from cst_core.domain.record import ROOT_RECORD
from cst_core.ports import (
    CommandFailed,
    CommandRunnerPort,
    ExecutionContextPort,
    FileSystemPort,
    OutputMode,
    SystemctlHost,
    WriteFailed,
)

_DEFAULT_PROC_DIR: Final[str] = "/proc"

# What installing the top-mount unit, marking it and reloading systemd can raise.
_TOP_MOUNT_ERRORS: Final = (CommandFailed, OSError, WriteFailed, Refused, ValueError)


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
    ) -> None:
        self._entry_file = config.entry_file
        self._fallback_root = config.fallback_root
        self._proc_dir = config.proc_dir.rstrip("/") or _DEFAULT_PROC_DIR
        self._host_root = config.host_root.rstrip("/")
        self._ctx = HostExecutionContext() if ctx is None else ctx
        self._runner = SubprocessCommandRunner() if runner is None else runner
        self._fs = LocalFileSystemAdapter() if fs is None else fs
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
        /var (the order of `pick_root_mounts`, cst doctor and btrfs-setup); else the top mount unit's."""
        for target in ("/sysroot", "/", "/var"):
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

    def read_top_mount(self) -> TopMountInfo | None:
        """Read installed `var-mnt-btrfs\\x2dtop.mount`, or return None when not installed."""
        text = self._fs.read_text(self._etc_file("systemd", "system", TOP_MOUNT_UNIT_NAME), None)
        if text is None:
            return None
        return parse_top_mount_unit(text)

    def write_fstab(self, text: str) -> None:
        """Atomically write `/etc/fstab` and reload systemd units (restoring previous text on reload error)."""
        path = self._etc_file("fstab")
        previous = self._fs.read_text(path, None)
        self._fs.write_atomic(path, text, mode=0o644)
        try:
            self._systemctl.daemon_reload()
        except CommandFailed:
            if previous is not None:
                self._fs.write_atomic(path, previous, mode=0o644)
            raise

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

    def update_top_mount(self, uuid: str, options: str) -> None:
        """Re-render and verify `btrfs:top-mount` through `InstallService`, reload systemd, and mark done."""
        root = checkout_root(self._entry_file) or self._fallback_root
        prev_top = self.read_top_mount()
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
        except _TOP_MOUNT_ERRORS:
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
