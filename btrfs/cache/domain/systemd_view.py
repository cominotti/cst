# SPDX-License-Identifier: MPL-2.0
"""What systemd has loaded of /etc/fstab, from the mount units it lists: whether a cache attach or detach
left it behind (an earlier run stopped between its /etc/fstab write and its daemon-reload), for attach,
plan, status and cst doctor. Pure: the caller asks `systemctl show --state=loaded -p MOUNT_UNIT_PROPS
'*.mount'` (every mount unit systemd has loaded, active or not)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Final

from btrfs.cache.domain import BindingProbe
from btrfs.compress.domain import FSTAB_PATH
from cst_core.domain.text import YES_WORDS
from cst_core.domain.units import LoadState

MOUNT_UNIT_PROPS: Final = ("Id", "LoadState", "SourcePath", "NeedDaemonReload", "Where")
"""What systemd_behind_fstab reads of each loaded mount unit, active or not (systemctl show --state=loaded
-p ... '*.mount')."""


def _loaded_units(units: Sequence[Mapping[str, str]]) -> list[Mapping[str, str]]:
    return [u for u in units if u.get("LoadState") == LoadState.LOADED]


def _reload_flagged(loaded: Sequence[Mapping[str, str]]) -> bool:
    """A unit systemd loaded from /etc/fstab says NeedDaemonReload=yes (any write of /etc/fstab since the
    last reload)."""
    return any(
        u.get("SourcePath") == FSTAB_PATH and u.get("NeedDaemonReload", "").strip().lower() in YES_WORDS for u in loaded
    )


def fstab_lines_unread(units: Sequence[Mapping[str, str]] | None, in_fstab: Collection[str]) -> bool | None:
    """status's and cst doctor's reading of systemd_behind_fstab, over every managed line (in_fstab: their
    targets): True when a unit systemd loaded from /etc/fstab says NeedDaemonReload=yes, or when a loaded
    unit at the target of a managed line comes from elsewhere than /etc/fstab (/proc/self/mountinfo: a
    mount systemd did not start, so the bind is mounted, while its line is in /etc/fstab); None when
    systemctl gave no answer; False otherwise."""
    if units is None:
        return None
    loaded = _loaded_units(units)
    return _reload_flagged(loaded) or any(
        u.get("Where", "") in in_fstab and u.get("SourcePath", "") != FSTAB_PATH for u in loaded
    )


def systemd_behind_fstab(
    units: Sequence[Mapping[str, str]] | None, bindings: Sequence[BindingProbe], mounting: Collection[str] = ()
) -> bool | None:
    """Whether systemd has not loaded /etc/fstab as it is on disk, from the mount units it has loaded
    (MOUNT_UNIT_PROPS, a glob's answer: a unit loaded by the question itself would say a false no).
    True when either holds:
    - a unit systemd loaded from /etc/fstab says NeedDaemonReload=yes (any write of /etc/fstab since the
      last reload);
    - a bind with its managed line in /etc/fstab, mounted from its source or one of mounting (the
      targets attach mounts in this run), has a unit (by Where) from elsewhere (/proc/self/mountinfo), or,
      for one of mounting, no unit at all: had systemd read that line it would have its unit from
      /etc/fstab (CST's lines are nofail, so local-fs.target wants them), also before systemd has seen
      the new mount. Either way systemd has not read /etc/fstab since that line was written.
    None when systemctl gave no answer; False otherwise. A bind mounted before this run with no unit at
    all proves nothing (test_160's test root, whose mounts the real systemd does not see). detach has
    only the first rule (it takes the lines out): with no unit of /etc/fstab loaded at all, a detach
    killed before its reload reads as not behind."""
    if units is None:
        return None
    loaded = _loaded_units(units)
    if _reload_flagged(loaded):
        return True
    source_of = {u.get("Where", ""): u.get("SourcePath", "") for u in loaded}

    def unread(p: BindingProbe) -> bool:
        target = p.binding.target
        if not p.in_fstab or not (p.mounted_from_source or target in mounting):
            return False
        source = source_of.get(target)
        return source != FSTAB_PATH if source is not None else target in mounting

    return any(unread(p) for p in bindings)
