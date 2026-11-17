"""Reading small files (sysfs, procfs, state files) the way CST programs need it, and creating folders.

Two text readers decode on purpose in two ways; do not merge them:
- read_text here is the LENIENT reader: bytes that are not UTF-8 become
  U+FFFD (errors="replace"). For files CST only looks at (sysfs, procfs,
  settings, state): a stray byte must never stop a program with a traceback.
- LocalFileSystemAdapter.read_text (below) is the BYTE-TRANSPARENT
  reader (errors="surrogateescape", line ends kept): for shared files that
  CST edits and writes back (fstab, crypttab, sysctl files through cst swap),
  every byte, also one that is not UTF-8, is written back as it was.
"""

from collections.abc import Callable
import contextlib
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Literal, overload

from cst_core.domain.files import OwnedState, UnitFileState, owned_file_state
from cst_core.domain.install import Owner, link_read_through
from cst_core.domain.text import first_line
from cst_core.domain.units import MASK_TARGET


@overload
def read_text(path: str) -> str | None: ...


@overload
def read_text(path: str, default: str, *, limit: int = -1) -> str: ...


def read_text(path: str, default: str | None = None, *, limit: int = -1) -> str | None:
    """The file as UTF-8 text, at most limit characters; default on OSError.

    The lenient decode policy (see the module docstring): a byte that is not
    UTF-8 becomes U+FFFD. Not for a file that is written back.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return default


def read_bytes(path: str, *, limit: int = -1) -> bytes | None:
    """The file's bytes (at most limit), or None on OSError."""
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


_INT = re.compile(r"-?[0-9]+")


def read_int(path: str, default: int | None = None) -> int | None:
    """The integer the file holds (surrounding blanks ignored; one leading minus allowed), else default.

    Only ASCII digits count: "--5", "7 MiB" or a superscript digit give default, never an error.
    """
    text = read_text(path, "", limit=64).strip()
    return int(text) if _INT.fullmatch(text) else default


def ensure_dir(path: str, mode: int = 0o755) -> list[str]:
    """Create the folder path and its missing parents; the folders it creates, in order.

    Each folder it creates gets exactly mode (chmod after mkdir: the umask
    does not reduce it, as os.makedirs's mode= is); a folder that is already
    there (or that another process creates meanwhile) is left as it is.
    """
    missing: list[str] = []
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    created: list[str] = []
    for d in reversed(missing):
        try:
            os.mkdir(d, mode)
        except FileExistsError:
            if os.path.isdir(d):
                continue
            raise
        os.chmod(d, mode)
        created.append(d)
    return created


def owned_file_state_at(
    path: str, wanted: bytes | None, is_ours: Callable[[bytes], bool], *, owner: Owner
) -> OwnedState:
    """State of the file CST writes whole at `path` (an installed program, a unit) against `wanted`.

    ok: exactly wanted; change: is_ours(its bytes) but different; conflict:
    someone else's file or a file that cannot be read; missing. owner says
    whose destination it is, for the one link rule
    (cst_core.domain.install.link_read_through): at a root destination a link
    is "change" (replaced); at a user destination it is judged by its
    target (a dangling link is "missing").
    """
    try:
        is_link = os.path.islink(path)
        if is_link and not link_read_through(owner):
            return owned_file_state(None, is_link=True, wanted=wanted, ours=False, owner=owner)
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "conflict"
    return owned_file_state(data, is_link=is_link, wanted=wanted, ours=is_ours(data), owner=owner)


def is_masked_unit(path: str) -> bool:
    """True when path is a link that leads to /dev/null: `systemctl mask` put it
    there (systemd follows a chain of links too)."""
    return os.path.islink(path) and os.path.realpath(path) == MASK_TARGET


def unit_file_state_at(
    path: str, wanted: bytes | None, is_ours: Callable[[bytes], bool], *, owner: Owner
) -> UnitFileState:
    """owned_file_state_at for a systemd unit file: "masked" first when the
    unit is masked (is_masked_unit), which the link rule would otherwise replace
    (root) or call someone else's file (user). A link to anything else follows
    the link rule. Only for unit destinations: a program linked to /dev/null is
    not a mask."""
    if is_masked_unit(path):
        return "masked"
    return owned_file_state_at(path, wanted, is_ours, owner=owner)


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
        """Read the file as UTF-8 text, returning default on OSError.

        Byte-transparent, like bash's `read -r` on the shared files (fstab,
        crypttab): a byte that is not UTF-8 comes back as itself through
        write_atomic (surrogateescape), and line ends are kept as they are.
        """
        try:
            with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
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
        write_atomic(dest, content, mode=mode, owner="root")  # str: UTF-8 with surrogateescape

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
        """Ensure link_path is a symlink pointing to target.

        The link is made under a unique temporary name in the same folder and
        renamed over link_path: there is no moment with nothing at link_path.
        """
        abs_link = os.path.abspath(link_path)
        parent = os.path.dirname(abs_link)
        ensure_dir(parent, 0o755)
        for _ in range(100):
            tmp = os.path.join(parent, f".{os.path.basename(abs_link)}.{os.urandom(6).hex()}")
            try:
                os.symlink(target, tmp)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(f"{parent}: no free temporary name for a link")
        try:
            os.replace(tmp, abs_link)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


type FileOwner = Literal["caller", "root"]


def _fsync_dir(folder: str) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOTSUP):  # a filesystem that cannot sync a folder
            raise
    finally:
        os.close(fd)


def _relabel(path: str) -> None:
    """restorecon as root; a failure is reported on stderr (the file is written, its label may be wrong)."""
    restorecon = shutil.which("restorecon")
    if restorecon is None:
        return
    # "--": the path is an operand even if a caller ever passes one starting with "-".
    done = subprocess.run(
        [restorecon, "-F", "--", path],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        why = first_line(done.stderr) or f"exit status {done.returncode}"
        print(f"warning: restorecon {path}: {why} (its SELinux label may be wrong)", file=sys.stderr)


def write_atomic(path: str, data: bytes | str, *, mode: int, durable: bool = True, owner: FileOwner = "caller") -> None:
    """Replace the file at path with data, atomically: path holds the old or the new file, never a part.

    - A unique temporary file in the same folder (mkstemp), mode set exactly
      with fchmod (the umask does not reduce it), then os.replace. A link at
      path is replaced, never followed. Missing folders are created 0755
      (ensure_dir). The temporary file is removed on every error.
    - durable (the default): the file is fsynced before the rename and the
      folder after it, so the new content survives a power loss or a hard
      reset (a boot counter must not lose its increment).
    - owner: "caller" leaves the file to whoever writes it; "root" also makes
      it 0:0 when running as root (the installer's files).
    - As root, the file is relabelled (restorecon); a failure is reported on
      stderr, not raised.
    - str data is written as UTF-8 (surrogateescape: bytes read with it come
      back as they were).
    """
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    ensure_dir(parent, 0o755)
    content = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(abs_path)}.", dir=parent)
    try:
        try:
            os.fchmod(fd, mode)
            if owner == "root" and os.geteuid() == 0:
                os.fchown(fd, 0, 0)
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view) :]
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, abs_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if durable:
        _fsync_dir(parent)
    if os.geteuid() == 0:
        _relabel(abs_path)


def write_bytes_atomic(dest: str, content: bytes, mode: int = 0o644) -> None:
    """The installer's write: write_atomic, durable, owned by root as root (and relabelled)."""
    write_atomic(dest, content, mode=mode, owner="root")
