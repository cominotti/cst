"""Concrete infrastructure adapters implementing cst_core ports and zipapp packaging."""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import zipfile

if __name__ == "__main__":
    _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.isdir(_repo_root) and _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

from cst_core.domain.install import INSTALL_MANIFEST
from cst_core.ports import CommandFailed, CommandResult, CommandRunnerPort, ExecutionContextPort

_ZIP_FIXED_TIME = (2026, 1, 1, 0, 0, 0)
_ZIP_PREAMBLE = b"#!/usr/bin/python3 -I\n"


def _decode_stream(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


class SubprocessCommandRunner:
    """CommandRunnerPort backed by subprocess.run with explicit stdin handling."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run argv with capture_output=True and DEVNULL stdin unless input_text is given."""
        cmd = list(argv)
        merged_env = None if env is None else {**os.environ, **env}
        try:
            if input_text is None:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    env=merged_env,
                )
            else:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    input=input_text,
                    env=merged_env,
                )
        except FileNotFoundError as exc:
            if check:
                raise CommandFailed(cmd, 127, "", str(exc)) from None
            return CommandResult(returncode=127, stdout="", stderr=str(exc))
        except subprocess.TimeoutExpired as exc:
            msg = f"timed out after {timeout}s" if timeout is not None else "command timed out"
            out = _decode_stream(exc.stdout)
            err = _decode_stream(exc.stderr) or msg
            if check:
                raise CommandFailed(cmd, 124, out, err) from None
            return CommandResult(returncode=124, stdout=out, stderr=err)
        except OSError as exc:
            if check:
                raise CommandFailed(cmd, 126, "", str(exc)) from None
            return CommandResult(returncode=126, stdout="", stderr=str(exc))
        if check and proc.returncode != 0:
            raise CommandFailed(cmd, proc.returncode, proc.stdout, proc.stderr)
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class LocalFileSystemAdapter:
    """FileSystemPort operating on local files with atomic rename and SELinux restorecon."""

    def exists(self, path: str) -> bool:
        """Return True if path exists (following symlinks)."""
        return os.path.exists(path)

    def lexists(self, path: str) -> bool:
        """Return True if path exists (including broken symlinks)."""
        return os.path.lexists(path)

    def is_dir(self, path: str) -> bool:
        """Return True if path is an existing directory."""
        return os.path.isdir(path)

    def is_file(self, path: str) -> bool:
        """Return True if path is an existing regular file."""
        return os.path.isfile(path)

    def is_symlink(self, path: str) -> bool:
        """Return True if path is a symbolic link."""
        return os.path.islink(path)

    def is_block_device(self, path: str) -> bool:
        """Return True if path resolves to a block special device."""
        try:
            return stat.S_ISBLK(os.stat(path).st_mode)
        except OSError:
            return False

    def is_readable(self, path: str) -> bool:
        """Return True if the current process has read access to path."""
        return os.access(path, os.R_OK)

    def read_text(self, path: str, default: str | None = None) -> str | None:
        """Read UTF-8 text from path, returning default on OSError."""
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return default

    def read_first_line(self, path: str, default: str | None = None) -> str | None:
        """Read and strip file text from path, returning default on OSError."""
        text = self.read_text(path)
        return default if text is None else text.strip()

    def readlink(self, path: str, *, canonicalize: bool = False) -> str | None:
        """Read symlink target, or canonicalize path via realpath if canonicalize=True."""
        if canonicalize:
            if not os.path.lexists(path):
                return None
            try:
                return os.path.realpath(path)
            except OSError:
                return None
        try:
            return os.readlink(path)
        except OSError:
            return None

    def list_dir(self, path: str) -> list[str]:
        """Return sorted directory entry names (may raise OSError or PermissionError)."""
        return sorted(os.listdir(path))

    def write_atomic(self, dest: str, content: str, mode: int = 0o644) -> None:
        """Write content to dest atomically via a temporary file in the target directory."""
        write_bytes_atomic(dest, content.encode("utf-8"), mode=mode)

    def write_text_direct(self, path: str, content: str) -> None:
        """Write content directly in place (for sysfs/procfs pseudo-files)."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def copy_if_absent(self, src: str, dest: str) -> None:
        """Copy src to dest preserving metadata only when dest does not yet exist."""
        if not os.path.lexists(dest) and os.path.exists(src):
            shutil.copy2(src, dest)

    def remove_file(self, path: str) -> None:
        """Remove path if present (no error if already absent)."""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    def force_symlink(self, target: str, link_path: str) -> None:
        """Ensure link_path is a symlink pointing to target."""
        parent = os.path.dirname(os.path.abspath(link_path))
        os.makedirs(parent, exist_ok=True)
        if os.path.lexists(link_path):
            os.unlink(link_path)
        os.symlink(target, link_path)


def write_bytes_atomic(dest: str, content: bytes, mode: int = 0o644) -> None:
    """Write bytes atomically with explicit permissions (ignoring umask) and SELinux restorecon."""
    abs_dest = os.path.abspath(dest)
    parent = os.path.dirname(abs_dest)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    if os.path.islink(abs_dest):
        os.unlink(abs_dest)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(abs_dest)}.", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, abs_dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if os.geteuid() == 0:
        restorecon = shutil.which("restorecon")
        if restorecon is not None:
            subprocess.run(
                [restorecon, "-F", abs_dest],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )


class HostExecutionContext:
    """ExecutionContextPort and HostPathPort detecting host vs toolbox/container environments."""

    def __init__(
        self,
        *,
        exists: Callable[[str], bool] | None = None,
        euid: Callable[[], int] | None = None,
        host_root: str = "/run/host",
    ) -> None:
        self._exists = os.path.exists if exists is None else exists
        self._euid = os.geteuid if euid is None else euid
        self._host_root = host_root.rstrip("/") or "/run/host"

    def is_container(self) -> bool:
        """Return True when running inside a Podman toolbox or Docker container."""
        return self._exists("/run/.containerenv") or self._exists("/.dockerenv")

    def is_root(self) -> bool:
        """Return True when effective UID is 0."""
        return self._euid() == 0

    def path(self, host_path: str) -> str:
        """Map an absolute host path into /run/host when inside a container."""
        if self.is_container() and host_path.startswith("/"):
            return f"{self._host_root}/{host_path.lstrip('/')}"
        return host_path

    def etc_dir(self) -> str:
        """Return the host's /etc path as readable from the current context."""
        return f"{self._host_root}/etc" if self.is_container() else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        """Return a host /run subdirectory as readable from the current context."""
        cleaned = subpath.strip("/")
        base = f"{self._host_root}/run" if self.is_container() else "/run"
        return f"{base}/{cleaned}" if cleaned else base


class SystemClockAdapter:
    """ClockPort delegating to the Python standard library `time` module."""

    def now(self) -> float:
        """Return wall-clock seconds since the Unix epoch."""
        return time.time()

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Sleep for `seconds`."""
        time.sleep(seconds)


class SystemctlAdapter:
    """SystemdPort implementation delegating to systemctl and systemd-escape."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self._runner = runner

    def daemon_reload(self) -> None:
        """Run `systemctl daemon-reload`."""
        self._runner.run(["systemctl", "daemon-reload"], check=True)

    def start(self, unit: str) -> bool:
        """Start unit and return True on zero exit status."""
        return self._runner.run(["systemctl", "start", unit], check=False).returncode == 0

    def stop(self, unit: str) -> bool:
        """Stop unit and return True on zero exit status."""
        return self._runner.run(["systemctl", "stop", unit], check=False).returncode == 0

    def restart(self, unit: str) -> bool:
        """Restart unit and return True on zero exit status."""
        return self._runner.run(["systemctl", "restart", unit], check=False).returncode == 0

    def escape_path(self, path: str, *, suffix: str) -> str:
        """Escape a filesystem path into a systemd unit name via systemd-escape."""
        res = self._runner.run(["systemd-escape", "-p", f"--suffix={suffix}", path], check=True)
        return res.stdout.strip()


class SubprocessVirshAdapter:
    """VirshPort adapter running `virsh` on the host (via `flatpak-spawn --host` in containers)."""

    def __init__(
        self,
        uri: str = "qemu:///system",
        runner: CommandRunnerPort | None = None,
        ctx: ExecutionContextPort | None = None,
    ) -> None:
        self._uri = uri
        self._runner = SubprocessCommandRunner() if runner is None else runner
        self._ctx = HostExecutionContext() if ctx is None else ctx

    def call(
        self,
        *args: str,
        check: bool = False,
        timeout: float | None = 60,
        start_new_session: bool = False,
    ) -> CommandResult:
        """Execute `virsh -c <uri> *args` (using direct subprocess when start_new_session=True)."""
        base = ["virsh", "-c", self._uri, *args]
        cmd = ["flatpak-spawn", "--host", *base] if self._ctx.is_container() else base
        if start_new_session:
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "LC_ALL": "C"},
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                if check:
                    raise CommandFailed(cmd, 127, "", str(exc)) from None
                return CommandResult(127, "", str(exc))
            except subprocess.TimeoutExpired:
                if check:
                    raise CommandFailed(cmd, 124, "", f"timed out after {timeout}s") from None
                return CommandResult(124, "", f"timed out after {timeout}s")
            if check and proc.returncode != 0:
                raise CommandFailed(cmd, proc.returncode, proc.stdout, proc.stderr)
            return CommandResult(proc.returncode, proc.stdout, proc.stderr)
        return self._runner.run(cmd, check=check, timeout=timeout, env={"LC_ALL": "C"})

    def domains(self, *, all_domains: bool = True) -> list[str]:
        """Return configured domain names, or empty list on failure."""
        args = ["list", "--all", "--name"] if all_domains else ["list", "--name"]
        res = self.call(*args, check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def dumpxml(self, domain: str, *, inactive: bool = False) -> str | None:
        """Return XML definition of `domain`, or None on failure."""
        args = ["dumpxml", "--inactive", domain] if inactive else ["dumpxml", domain]
        res = self.call(*args, check=False)
        return res.stdout if res.returncode == 0 else None


def load_script(path: str | Path, module_name: str | None = None) -> types.ModuleType:
    """Load a Python script (including extensionless helpers) with sys.modules registration.

    Python 3.15 `@dataclass` and `typing` inspection require the module to be
    registered in `sys.modules` before executing its body.
    """
    abs_path = os.path.abspath(os.fspath(path))
    repo_root = os.path.dirname(os.path.dirname(abs_path))
    if os.path.isdir(os.path.join(repo_root, "cst_core")) and repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    if module_name is None:
        slug = os.path.basename(abs_path).replace("-", "_").replace(".", "_")
        module_name = f"cst_script_{slug}_{abs(hash(abs_path))}"
    loader = importlib.machinery.SourceFileLoader(module_name, abs_path)
    spec = importlib.util.spec_from_loader(module_name, loader, origin=abs_path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return mod


def script_path(dunder_file: str) -> str:
    """Return the path of the running script or zipapp archive."""
    abs_file = os.path.abspath(dunder_file)
    if os.path.basename(abs_file) == "__main__.py":
        archive = os.path.dirname(abs_file)
        if os.path.isfile(archive):
            return archive
    return abs_file


def _collect_package_files(pkg_dir: Path, pkg_name: str) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    if not pkg_dir.is_dir():
        return files
    for path in sorted(pkg_dir.rglob("*.py")):
        rel_parts = path.relative_to(pkg_dir).parts
        if "tests" in rel_parts or "__pycache__" in rel_parts:
            continue
        arc_name = f"{pkg_name}/{'/'.join(rel_parts)}"
        files.append((arc_name, path.read_bytes()))
    return files


def build_python_zipapp(repo_root: str | Path, src_rel: str) -> bytes:
    """Build a deterministic, uncompressed self-contained zipapp for a CST helper.

    Bundles `__main__.py` (`src_rel`), `cst_core/`, and the helper's owning
    bounded-context package (`<pkg>/`) using ZIP_STORED and fixed timestamps so
    repeated builds are byte-for-byte reproducible and grep-friendly.
    """
    root = Path(repo_root).resolve()
    norm_rel = src_rel.strip("/")
    entry_path = root / norm_rel
    if not entry_path.is_file():
        raise FileNotFoundError(f"zipapp source not found: {entry_path}")

    pkg_name = norm_rel.split("/", 1)[0]
    raw_entry = entry_path.read_bytes()
    preamble_lines: list[bytes] = [_ZIP_PREAMBLE]
    for line in raw_entry.splitlines(keepends=True)[1:4]:
        if line.startswith(b"# managed-by:"):
            preamble_lines.append(line if line.endswith(b"\n") else line + b"\n")
            break
    preamble = b"".join(preamble_lines)

    entries: dict[str, bytes] = {"__main__.py": raw_entry}
    for arc_name, data in _collect_package_files(root / "cst_core", "cst_core"):
        entries[arc_name] = data
    if "/" in norm_rel and (root / pkg_name / "__init__.py").is_file():
        for arc_name, data in _collect_package_files(root / pkg_name, pkg_name):
            entries[arc_name] = data

    buf = io.BytesIO()
    buf.write(preamble)
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for arc_name in sorted(entries):
            info = zipfile.ZipInfo(filename=arc_name, date_time=_ZIP_FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            zf.writestr(info, entries[arc_name])
    return buf.getvalue()


def write_python_zipapp(
    repo_root: str | Path,
    src_rel: str,
    dest_path: str | Path,
    mode: int = 0o755,
) -> bool:
    """Write a deterministic zipapp atomically; return True if dest bytes changed."""
    payload = build_python_zipapp(repo_root, src_rel)
    abs_dest = os.path.abspath(os.fspath(dest_path))
    try:
        if (
            not os.path.islink(abs_dest)
            and os.path.isfile(abs_dest)
            and stat.S_IMODE(os.stat(abs_dest).st_mode) == mode
        ):
            with open(abs_dest, "rb") as fh:
                if fh.read() == payload:
                    return False
    except OSError:
        pass
    write_bytes_atomic(abs_dest, payload, mode=mode)
    return True


def _cli_main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["build-zipapp"] and len(args) in (4, 5):
        repo_root, src_rel, dest_path = args[1], args[2], args[3]
        mode = int(args[4], 8) if len(args) == 5 else 0o755
        data = build_python_zipapp(repo_root, src_rel)
        parent = os.path.dirname(os.path.abspath(dest_path))
        if parent:
            os.makedirs(parent, mode=0o755, exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(data)
        os.chmod(dest_path, mode)
        return 0
    if args[:1] == ["manifest-tsv"]:
        kind_filter = args[1] if len(args) > 1 else ""
        for entry in INSTALL_MANIFEST:
            if not kind_filter or entry.kind == kind_filter:
                print(
                    f"{entry.src}\t{entry.dest}\t{entry.mode.bits:04o}\t{entry.owner}\t{entry.kind}\t{entry.runner}"
                )
        return 0
    print(
        "usage: cst_core.adapters [build-zipapp <repo> <src_rel> <dest> [mode] | manifest-tsv [kind]]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())


__all__ = [
    "HostExecutionContext",
    "LocalFileSystemAdapter",
    "SubprocessCommandRunner",
    "SubprocessVirshAdapter",
    "SystemClockAdapter",
    "SystemctlAdapter",
    "build_python_zipapp",
    "load_script",
    "script_path",
    "write_bytes_atomic",
    "write_python_zipapp",
]
