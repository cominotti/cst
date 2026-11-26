# SPDX-License-Identifier: MPL-2.0
"""Host inspection and execution adapters for `cst sync`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import os
import pwd

from cst_core.adapters.files import open_regular_bytes
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import (
    SYSTEM_PYTHON,
    checkout_root,
    host_view_path,
    lc_ctype_handover,
    program_path,
)
from cst_core.domain.dispatch import step_down_argv
from cst_core.domain.install import Owner
from cst_core.domain.record import ROOT_RECORD, USER_RECORD_REL, InstallRecord, RecordFormatError, RecordedFile
from cst_core.domain.values import ROOT_ACCOUNT
from cst_core.ports import CommandRunnerPort, OutputMode
from doctor.adapters import InstalledFiles
from sync.domain import SYNC_PROG, SyncArgs


class HostSyncInspectAdapter:
    """`SyncInspectPort` backed by local files, install records, and the checkout payloads."""

    def __init__(self, entry_file: str, *, env: Mapping[str, str] | None = None) -> None:
        self._entry_file = entry_file
        self._env = os.environ if env is None else env
        self._repo = checkout_root(entry_file) or ""
        self._ctx = HostExecutionContext()
        self._records = JsonInstallRecord()
        self._desired = InstalledFiles(self._repo)

    def in_container(self) -> bool:
        return self._ctx.is_container()

    def euid(self) -> int:
        return os.geteuid()

    def target_user(self) -> tuple[str, int, str]:
        sudo_user = self._env.get("SUDO_USER", "")
        if self.euid() == 0:
            if sudo_user and sudo_user != ROOT_ACCOUNT:
                try:
                    entry = pwd.getpwnam(sudo_user)
                    return entry.pw_name, entry.pw_uid, entry.pw_dir
                except KeyError:
                    return "", 0, ""
            return "", 0, ""
        uid = os.getuid()
        try:
            entry = pwd.getpwuid(uid)
            home = self._env.get("HOME") or entry.pw_dir
            return entry.pw_name, uid, home
        except KeyError:
            home = self._env.get("HOME", "")
            return self._env.get("USER", ""), uid, home

    def read_record(self, owner: Owner, home: str) -> tuple[InstallRecord | None, str | None]:
        raw_path = ROOT_RECORD if owner == Owner.ROOT else os.path.join(home.rstrip("/"), USER_RECORD_REL)
        view_path = host_view_path(self._ctx, raw_path)
        try:
            rec = self._records.read(view_path)
        except RecordFormatError as exc:
            why = (
                "written by a newer CST: update this checkout (git pull)"
                if exc.newer
                else "it is not a valid install record"
            )
            return None, f"{raw_path}: {exc} ({why})"
        except PermissionError as exc:
            return None, f"{raw_path}: not readable as this user ({exc.strerror or exc})"
        except OSError as exc:
            return None, f"{raw_path}: cannot be read ({exc.strerror or exc})"
        return rec, None

    def stat_and_hash(self, dest: str) -> tuple[bool, str | None]:
        view_path = host_view_path(self._ctx, dest)
        try:
            os.lstat(view_path)
        except FileNotFoundError, NotADirectoryError:
            return False, None
        except OSError:
            return True, None
        try:
            with open_regular_bytes(view_path) as fh:
                return True, hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return True, None

    def desired_manifest_sha256(self, src: str, values: dict[str, str] | None) -> str | None:
        return self._desired.desired_sha256(src, values)

    def desired_generated_sha256(self, dest: str, f: RecordedFile) -> str | None:
        return self._desired.desired_generated_sha256(dest, f)


class HostSyncExecAdapter:
    """`SyncExecPort` invoking `./cst <module> ...` cleanly and elevating once via `sudo` if needed."""

    def __init__(
        self,
        entry_file: str,
        *,
        prog: str = SYNC_PROG,
        env: Mapping[str, str] | None = None,
        runner: CommandRunnerPort | None = None,
    ) -> None:
        self._entry_file = entry_file
        self._prog = prog
        self._env = os.environ if env is None else env
        self._repo = checkout_root(entry_file) or ""
        self._runner: CommandRunnerPort = SubprocessCommandRunner() if runner is None else runner

    def _cst_name(self) -> str:
        return self._env.get("CST_NAME") or "cst"

    def run_cst_subcommand(self, argv: Sequence[str], *, as_user: str | None = None) -> int:
        cst_bin = os.path.join(self._repo, "cst") if self._repo else "cst"
        cmd: list[str] = [cst_bin, *argv]
        env_extra = {"CST_NAME": self._cst_name()}
        if as_user is not None and as_user != "":
            entry = pwd.getpwnam(as_user)
            full_cmd = step_down_argv(
                as_user,
                entry.pw_uid,
                entry.pw_dir,
                unset=("XDG_CONFIG_HOME", "XDG_STATE_HOME"),
                then=[f"CST_NAME={self._cst_name()}", *cmd],
            )
            res = self._runner.run(full_cmd, output=OutputMode.INHERIT, stdin="inherit", check=False)
            return res.returncode
        res = self._runner.run(cmd, env=env_extra, output=OutputMode.INHERIT, stdin="inherit", check=False)
        return res.returncode

    def run_root_subpass(self, args: SyncArgs) -> int:
        flags: list[str] = ["--root-only"]
        if args.include_risky:
            flags.append("--include-risky")
        if args.overwrite_modified:
            flags.append("--overwrite-modified")
        cmd = [
            "sudo",
            "--",
            "env",
            f"CST_PROG={self._prog}",
            f"CST_NAME={self._cst_name()}",
            lc_ctype_handover(),
            SYSTEM_PYTHON,
            "-I",
            "-B",
            program_path(self._entry_file),
            "apply",
            *flags,
        ]
        res = self._runner.run(cmd, output=OutputMode.INHERIT, stdin="inherit", check=False)
        return res.returncode
