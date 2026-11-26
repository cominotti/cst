# SPDX-License-Identifier: MPL-2.0
"""The boundaries of `cst btrfs cache`'s use cases: /etc/fstab, the live mount table, systemd, the private
folders and the scratchpad's tag, the host's read-only facts, accounts, provisioning, and migration.

Each port is one thing on the machine, not one command: application runs status, plan, attach, detach,
provision, and unprovision through them, and adapters implements them over the host's files and commands
(the test root of CST_BTRFS_CACHE_ROOT, /run/host from a toolbox). Paths go in as the user sees them
(/etc/fstab, /var/mnt/cache/USER, ~/.cache); the adapters map them.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from btrfs.cache.domain import BusyProcess, CacheMountBinding, CacheMountKind, CacheMountUser, LiveMount
from btrfs.cache.domain.provision import BlockDeviceFacts


@dataclass(frozen=True, slots=True)
class BindingPaths:
    """What the file system shows of one binding's two folders, read without following a link at the target."""

    target_is_link: bool
    target_exists: bool  # anything there, a dangling link too
    target_nonempty: bool  # a folder with an entry, or anything else that is not a folder; False for a link
    source_is_dir: bool  # a folder, through a link
    source_is_link: bool
    target_is_dir: bool  # asked only when the target exists and is not a link; False otherwise


@dataclass(frozen=True, slots=True)
class CachePathKind:
    """Non-following stat probe of a single host path."""

    exists: bool
    is_dir: bool
    is_symlink: bool
    is_regular: bool


class FstabPort(Protocol):
    """/etc/fstab, read and written byte-transparent (the lines CST does not manage keep their bytes)."""

    def read(self) -> str | None:
        """Its text; None when there is no file. OSError when a file is there and cannot be read (never
        taken for a missing one: a /etc/fstab rebuilt from "" would drop every line)."""
        ...

    def write(self, text: str) -> None:
        """The whole file replaced by text, atomically, mode 0644. OSError when it fails."""
        ...

    def remove(self) -> None:
        """No /etc/fstab (a file already gone is no error). OSError when it fails."""
        ...


class MountTablePort(Protocol):
    """The live mount table: findmnt, mount --bind and umount."""

    def read(self) -> tuple[tuple[LiveMount, ...], str | None]:
        """Every live mount; (), and why, when findmnt cannot answer (nothing is guessed from another table)."""
        ...

    def bind(self, binding: CacheMountBinding) -> str | None:
        """mount --bind of the binding's source at its target; None, or why it failed."""
        ...

    def unbind(self, binding: CacheMountBinding) -> str | None:
        """umount of the binding's target; None, or why it failed."""
        ...


class CacheSystemdPort(Protocol):
    """What systemd has loaded of /etc/fstab, and its reload."""

    def daemon_reload(self) -> str | None:
        """systemctl daemon-reload; None, or why it failed."""
        ...

    def mount_units(self) -> list[dict[str, str]] | None:
        """The mount units systemd has loaded, active or not (MOUNT_UNIT_PROPS of each); None when systemctl
        gives no answer."""
        ...


class CacheFoldersPort(Protocol):
    """The user's private folders and the scratchpad's CACHEDIR.TAG. Both calls raise OSError."""

    def ensure_private(self, path: str, user: CacheMountUser) -> None:
        """The folder at path (made when missing), then made 0700 and the user's through a descriptor opened
        without following a link: a link put there fails (ENOTDIR, as O_NOFOLLOW with O_DIRECTORY gives) instead
        of having its target changed."""
        ...

    def ensure_tag(self, source: str, user: CacheMountUser) -> None:
        """CACHEDIR.TAG in the source folder, the user's, unless something is already there."""
        ...


class CacheHostViewPort(Protocol):
    """The host's read-only facts that the probe reads besides /etc/fstab and the mounts."""

    def crypttab(self) -> str | None:
        """/etc/crypttab's text; None when it is not there."""
        ...

    def binding_paths(self, binding: CacheMountBinding) -> BindingPaths:
        """What the file system shows of the binding's source and target."""
        ...

    def home_exists(self, home: str) -> bool:
        """Whether the home is a folder."""
        ...


class CacheAccountsPort(Protocol):
    """The account a run is for."""

    def resolve(self, requested: str | None) -> tuple[CacheMountUser | None, str | None]:
        """The requested user (None: the caller's), its home as the kernel names it; or None and why not
        (no such user, a system user)."""
        ...


class CacheProvisionPort(Protocol):
    """Host storage operations for `cst btrfs cache provision` and `unprovision`."""

    def read_conf(self) -> tuple[str | None, str | None]:
        """Read `/etc/cst/btrfs-cache.conf` safely; returns `(text, error)`."""
        ...

    def write_conf(self, text: str) -> None:
        """Write `/etc/cst/btrfs-cache.conf` atomically (mode 0644). Raises OSError on failure."""
        ...

    def read_crypttab_exact(self) -> tuple[str | None, str | None]:
        """Byte-transparent read of `/etc/crypttab` (regular files only); returns `(text, error)`."""
        ...

    def write_crypttab(self, text: str) -> None:
        """Write `/etc/crypttab` atomically (mode 0600 if new, preserving existing mode). Raises OSError."""
        ...

    def remove_crypttab(self) -> None:
        """Remove `/etc/crypttab` when empty rollback target was None."""
        ...

    def inspect_device(self, device_path: str) -> tuple[BlockDeviceFacts | None, str | None]:
        """Inspect block device (`lsblk`, `blkid`, sysfs holders, `/proc/swaps`)."""
        ...

    def stat_path(self, path: str) -> CachePathKind:
        """Non-following stat probe of `path`."""
        ...

    def mapper_fs_info(self, mapper_path: str) -> tuple[str, str, str, str | None]:
        """Return `(fstype, uuid, label, luks_uuid)` for an open mapper device (`""`/None when absent)."""
        ...

    def luks_uuid(self, device_path: str) -> str | None:
        """Return LUKS2 container UUID (`cryptsetup luksUUID`), or None if not LUKS."""
        ...

    def test_keyfile(self, device_path: str, keyfile_path: str) -> bool:
        """True when `keyfile_path` opens `device_path` (`cryptsetup open --test-passphrase`)."""
        ...

    def create_keyfile(self, keyfile_path: str) -> None:
        """Generate random 64-byte keyfile atomically (`0400`, exclusive creation). Raises OSError."""
        ...

    def remove_file(self, path: str) -> None:
        """Remove `path` if present (ignoring FileNotFoundError)."""
        ...

    def luks_format(self, device_path: str, keyfile_path: str) -> str | None:
        """Format partition as LUKS2 (`--batch-mode`); returns None or error string."""
        ...

    def luks_open(self, device_path: str, mapper_name: str, keyfile_path: str) -> str | None:
        """Open LUKS2 partition under `/dev/mapper/<mapper_name>` with discard/no-workqueue flags."""
        ...

    def luks_close(self, mapper_name: str) -> str | None:
        """Close `/dev/mapper/<mapper_name>`; returns None or error string."""
        ...

    def mkfs_btrfs(self, mapper_path: str, label: str) -> str | None:
        """Run `mkfs.btrfs -f -L LABEL -m dup -d single` on mapper; returns None or error string."""
        ...

    def top_mount(self, mapper_path: str) -> AbstractContextManager[tuple[str | None, str | None]]:
        """Ephemerally mount `subvolid=5` at `/run/cst-btrfs-cache-top-XXXXXX` and always umount+rmdir."""
        ...

    def list_top_subvolumes(self, mapper_path: str) -> tuple[tuple[str, ...], str | None]:
        """Mount `subvolid=5` temporarily (or probe) to list existing top-level subvolumes."""
        ...

    def ensure_subvolumes(self, mapper_path: str, names: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
        """Ensure each subvolume in `names` exists; returns `(newly_created_names, error)`."""
        ...

    def ensure_base_dir(self, path: str) -> None:
        """Create base mountpoint directory (`0755`, SELinux relabeled) safely."""
        ...

    def remove_empty_dir(self, path: str) -> None:
        """Remove empty directory if present; ignores errors."""
        ...

    def mount_base(self, mapper_path: str, kind: CacheMountKind, target: str) -> str | None:
        """Mount `subvol=<kind>` at `target` with `BASE_MOUNT_OPTIONS`; returns None or error."""
        ...

    def umount_base(self, target: str) -> str | None:
        """Unmount base mountpoint `target`; returns None or error."""
        ...

    def confirm_format(self, prompt: str, *, assume_yes: bool) -> tuple[bool, str | None]:
        """Confirm destructive `--format`; returns `(confirmed, refusal_reason)`."""
        ...


class CacheMigrationPort(Protocol):
    """Host operations for migrating existing `~/.cache` during `attach --migrate`."""

    def backup_exists(self, backup_path: str) -> bool:
        """True when sibling backup path already exists (including symlink)."""
        ...

    def source_nonempty(self, source_path: str) -> bool:
        """True when destination directory exists and has at least one entry."""
        ...

    def is_btrfs_subvol(self, target_path: str) -> bool:
        """True when `target_path` is a btrfs subvolume root (inode 256 on btrfs)."""
        ...

    def busy_processes(self, target_path: str) -> tuple[tuple[BusyProcess, ...], str | None]:
        """List running processes with open fd or cwd under `target_path`, or error reason."""
        ...

    def copy_tree(self, src: str, dst: str) -> str | None:
        """Copy all contents from `src` into `dst` preserving metadata (`cp -a`); returns None or error."""
        ...

    def clear_dir_contents(self, path: str) -> None:
        """Best-effort cleanup of copied contents inside `dst` on migration rollback."""
        ...

    def rename(self, old: str, new: str) -> None:
        """Atomically rename directory `old` -> `new`. Raises OSError on failure."""
        ...

    def ensure_empty_dir(self, path: str, user: CacheMountUser) -> None:
        """Ensure empty `0700` user-owned mountpoint directory at `path`. Raises OSError."""
        ...

    def remove_empty_dir(self, path: str) -> None:
        """Remove empty directory `path` on rollback (ignoring FileNotFoundError)."""
        ...
