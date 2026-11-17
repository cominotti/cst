# SPDX-License-Identifier: MPL-2.0
"""Local files for the install use case: atomic writes, modes, root ownership, SELinux labels."""

from collections.abc import Callable, Mapping
import hashlib
import os
import shutil
import stat
import tempfile

from cst_core.adapters import SubprocessCommandRunner
from cst_core.adapters.files import ensure_dir, write_bytes_atomic
from cst_core.adapters.locks import take_lock
from cst_core.domain.files import checked_path
from cst_core.domain.install import OnDisk
from cst_core.domain.record import EMPTY_RECORD, InstallRecord, RecordFormatError, parse_record, render_record
from cst_core.ports import CommandRunnerPort, UnitVerdict, WriteFailed


class LocalInstallTarget:
    """InstallTargetPort on this machine's files.

    Run as root, written files are owned by root and relabelled
    (restorecon). Files in a user's home are written by a process running
    as that user (cst_install_as drops privileges first), never by root.
    """

    def current(self, path: str) -> OnDisk | None:
        """What is at path: the regular file read through a link (data None when there is none to read).

        None when nothing is there at all. Whether a link counts as current
        is domain.install.link_read_through's rule; write() replaces it.
        """
        path = checked_path(path)
        try:
            is_link = os.path.islink(path)
            st = os.stat(path)
        except OSError:
            return OnDisk(None, 0, -1, -1, True) if os.path.lexists(path) else None
        data: bytes | None = None
        if stat.S_ISREG(st.st_mode):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None
        return OnDisk(data, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, is_link)

    def write(self, path: str, data: bytes, mode: int) -> None:
        """Atomic replace (a link at path is replaced, not followed); WriteFailed naming path on an error."""
        path = checked_path(path)
        try:
            write_bytes_atomic(path, data, mode=mode)
        except OSError as e:
            raise WriteFailed(path, e.strerror or str(e)) from None

    def remove(self, path: str) -> bool:
        """unlink(2): a link is removed itself; False when nothing is there; WriteFailed on an error."""
        path = checked_path(path)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError as e:
            raise WriteFailed(path, e.strerror or str(e)) from None
        return True


class JsonInstallRecord:
    """InstallRecordPort on JSON files: written atomically (0644, folders 0755) under a lock file
    next to the record (RECORD.lock), so two installs never lose each other's lines."""

    def read(self, path: str) -> InstallRecord | None:
        """parse_record of the file; None when there is no file."""
        try:
            with open(checked_path(path), encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as e:
            raise RecordFormatError(f"not UTF-8 ({e.reason})") from None
        return parse_record(text)

    def update(self, path: str, change: Callable[[InstallRecord], InstallRecord]) -> bool:
        """Read, change, write back if different, under RECORD.lock."""
        path = checked_path(path)
        folder = os.path.dirname(path)
        try:
            # 0755 whatever the umask (sudo's 077 would give 0700; cst doctor reads the record as a user).
            ensure_dir(folder, 0o755)
            fd = take_lock(path + ".lock", wait=True, mode=0o644)
        except OSError as e:
            raise WriteFailed(path, e.strerror or str(e)) from None
        assert fd is not None  # wait=True never returns None
        try:
            try:
                old = self.read(path)
            except RecordFormatError as e:
                if e.newer:
                    raise
                old = None  # not a record (cut short, edited by hand): started again, the next install fills it
            before = old or EMPTY_RECORD
            after = change(before)
            text = render_record(after)
            if text == render_record(before) and (old is not None or after == EMPTY_RECORD):
                return False  # no change; and no empty record where there was none (a mark with nothing listed)
            write_bytes_atomic(path, text.encode("utf-8"), mode=0o644)
            return True
        except OSError as e:
            raise WriteFailed(path, e.strerror or str(e)) from None
        finally:
            os.close(fd)

    def sha256(self, data: bytes) -> str:
        """hashlib.sha256 in hex."""
        return hashlib.sha256(data).hexdigest()


class SystemdUnitVerifier:
    """UnitVerifierPort: `systemd-analyze verify --man=no --recursive-errors=no` on copies of the units,
    under their own names in a temporary folder, all in one call.

    --recursive-errors=no: without it, verify only warns about a problem in
    the named units (an unknown key) and exits 0; with it, any problem in
    them fails, and the units they only refer to (docker.service, the
    targets) do not. The exit status decides; the lines shown are the ones
    about these units (their paths or names). Run only where systemd runs
    the machine (/run/systemd/system): in a toolbox or a plain container
    the answer would be about the wrong system.
    """

    def __init__(
        self,
        runner: CommandRunnerPort | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
        booted: Callable[[], bool] = lambda: os.path.isdir("/run/systemd/system"),
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._which = which
        self._booted = booted

    def verify(self, units: Mapping[str, bytes]) -> UnitVerdict:
        """The verdict on the units (ok with a note when they cannot be verified here)."""
        if not units or not self._booted():
            return UnitVerdict(ok=True)
        if self._which("systemd-analyze") is None:
            return UnitVerdict(ok=True, note="systemd-analyze not found: the units are written without verification")
        with tempfile.TemporaryDirectory(prefix="cst-verify-") as tmp:
            paths = []
            for name, data in units.items():
                p = os.path.join(tmp, name)
                with open(p, "wb") as fh:
                    fh.write(data)
                paths.append(p)
            r = self._runner.run(
                ["systemd-analyze", "verify", "--man=no", "--recursive-errors=no", *paths], timeout=120, output="merge"
            )
            if r.returncode == 0:
                return UnitVerdict(ok=True)
            mine = tuple(
                line.replace(tmp + "/", "")
                for line in r.stdout.splitlines()
                if tmp in line or any(line.startswith(n + ":") or f" {n} " in line for n in units)
            )
            return UnitVerdict(ok=False, lines=mine or tuple(r.stdout.splitlines()[-5:]))
