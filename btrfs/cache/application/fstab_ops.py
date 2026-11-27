# SPDX-License-Identifier: MPL-2.0
"""Shared base-mount probing and atomic `/etc/fstab` + `daemon-reload` rollback helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from btrfs.cache.domain import BaseMountInfo, LiveMount
from btrfs.cache.ports import CacheSystemdPort, FstabPort
from btrfs.compress.domain import FSTAB_PATH, MountEntry
from cst_core.ports import ReplacedNotSynced

_NOT_MOUNTED: Final = LiveMount(source="", target="", fstype="", options="", uuid="", fsroot="")


def probe_base_mount(path: str, live: dict[str, LiveMount], fstab: dict[str, MountEntry]) -> BaseMountInfo:
    """Assemble live and `/etc/fstab` state for a base mountpoint (`CACHE_BASE` or `SCRATCHPAD_BASE`)."""
    live_m = live.get(path)
    fst = fstab.get(path)
    seen = live_m if live_m is not None else _NOT_MOUNTED
    return BaseMountInfo(
        path=path,
        mounted=live_m is not None,
        fstype=seen.fstype,
        uuid=seen.uuid,
        options=seen.options,
        in_fstab=fst is not None,
        fstab_options=fst.options if fst is not None else "",
        source=seen.source,
        fsroot=seen.fsroot,
    )


@dataclass(frozen=True, slots=True)
class FstabFailure:
    """Why writing /etc/fstab and reloading systemd failed, and what of /etc/fstab could not be undone."""

    cause: str
    undo_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PutBack:
    """What putting /etc/fstab back did: whether the old text (or no file) is in place, and what failed."""

    in_place: bool
    error: str | None = None

    @property
    def errors(self) -> tuple[str, ...]:
        """The error, if any, as the undo errors it adds."""
        return () if self.error is None else (self.error,)


def _put_fstab_back(previous: str | None, fstab: FstabPort) -> _PutBack:
    """/etc/fstab as it was (the previous text, or no file when there was none)."""
    try:
        if previous is None:
            fstab.remove()
        else:
            fstab.write(previous)
    except ReplacedNotSynced as exc:
        return _PutBack(in_place=True, error=f"{FSTAB_PATH} put back, its folder not synced: {exc.strerror or exc}")
    except OSError as exc:
        return _PutBack(in_place=False, error=f"cannot put {FSTAB_PATH} back: {exc.strerror or exc}")
    return _PutBack(in_place=True)


def _undo_replaced(previous: str | None, new_text: str, fstab: FstabPort) -> tuple[str, ...]:
    """After a write of new_text that raised once its rename was done: /etc/fstab read back, and put back."""
    try:
        now = fstab.read()
    except OSError as exc:
        return (f"cannot read {FSTAB_PATH} back: {exc.strerror or exc}",)
    return _put_fstab_back(previous, fstab).errors if now == new_text else ()


def commit_fstab(
    previous: str | None, new_text: str, fstab: FstabPort, systemd: CacheSystemdPort
) -> FstabFailure | None:
    """Write /etc/fstab and reload systemd; None, or why it failed, after /etc/fstab rollback attempt."""
    try:
        fstab.write(new_text)
    except ReplacedNotSynced as exc:
        return FstabFailure(
            f"cannot write {FSTAB_PATH}: {exc.strerror or exc}", _undo_replaced(previous, new_text, fstab)
        )
    except OSError as exc:
        return FstabFailure(f"cannot write {FSTAB_PATH}: {exc.strerror or exc}")
    why = systemd.daemon_reload()
    if why is None:
        return None
    return FstabFailure(why, _put_fstab_back(previous, fstab).errors)


def restore_fstab(previous: str | None, fstab: FstabPort, systemd: CacheSystemdPort) -> list[str]:
    """Put /etc/fstab back as it was and reload systemd when the old text is in place."""
    put_back = _put_fstab_back(previous, fstab)
    why = systemd.daemon_reload() if put_back.in_place else None
    return [*put_back.errors] if why is None else [*put_back.errors, why]


def format_undo_error(cause: str, undo_errors: Sequence[str]) -> str:
    """Format `ERROR:` message summarizing rollback completion or incomplete undo errors."""
    if undo_errors:
        return f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(undo_errors)})"
    return f"ERROR: {cause}; mounts and {FSTAB_PATH} restored."
