# SPDX-License-Identifier: MPL-2.0
"""Host adapter for `cst btrfs cache system` (`/var/mnt/cache/system/*`)."""

from __future__ import annotations

import os
from typing import Final

from btrfs.cache.adapters.migrate import clear_dir_entries, copy_dir_contents
from btrfs.cache.adapters.mounts import (
    CacheMountRuntime,
    dir_is_nonempty,
    ensure_owned_dir,
    from_toolbox,
    map_path,
    run_mount,
)
from btrfs.cache.domain.system_spec import SystemBindSpec
from btrfs.cache.ports import SystemBindPaths
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.domain.units import ActiveState
from cst_core.ports import OutputMode, UnitName

_CHCON_TIMEOUT_S: Final = 15
_SYSTEMCTL_TIMEOUT_S: Final = 30


class CacheSystemBindAdapter:
    """`CacheSystemBindPort` over local filesystem, `cp -a`, `mount --bind`, and `systemctl`."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def probe_paths(self, spec: SystemBindSpec) -> SystemBindPaths:
        src = map_path(spec.source, self._rt, read_only=True)
        tgt = map_path(spec.target, self._rt, read_only=True)
        tgt_link = os.path.islink(tgt)
        tgt_exists = os.path.lexists(tgt)
        src_link = os.path.islink(src)
        src_exists = os.path.lexists(src)
        return SystemBindPaths(
            target_is_link=tgt_link,
            target_exists=tgt_exists,
            target_is_dir=tgt_exists and not tgt_link and os.path.isdir(tgt),
            target_nonempty=False if tgt_link else dir_is_nonempty(tgt),
            source_is_link=src_link,
            source_exists=src_exists,
            source_is_dir=src_exists and not src_link and os.path.isdir(src),
            source_nonempty=False if src_link else dir_is_nonempty(src),
        )

    def ensure_root_dir(self, path: str, mode: int, *, reference_selinux: str = "") -> None:
        mapped = map_path(path, self._rt, read_only=False)
        ensure_owned_dir(mapped, mode, (0, 0))
        if not reference_selinux or self._rt.seam_root or not os.path.isdir("/sys/fs/selinux"):
            return
        ref_mapped = map_path(reference_selinux, self._rt, read_only=True)
        if not os.path.islink(ref_mapped) and os.path.exists(ref_mapped):
            self._rt.runner.run(
                ["chcon", "-h", "--reference", ref_mapped, mapped],
                timeout=_CHCON_TIMEOUT_S,
                locale_c=True,
                output=OutputMode.CAPTURE,
            )

    def copy_tree(self, src: str, dst: str) -> str | None:
        return copy_dir_contents(self._rt, src, dst)

    def clear_dir_contents(self, path: str) -> None:
        clear_dir_entries(self._rt, path)

    def service_is_active(self, unit: str) -> bool | None:
        on_host = SystemctlHost.SHOW_ONLY if from_toolbox(self._rt) else SystemctlHost.LOCAL
        props = SystemctlAdapter(self._rt.runner, on_host=on_host, timeout=_SYSTEMCTL_TIMEOUT_S, locale_c=True).show(
            UnitName(unit), ("ActiveState",)
        )
        if not props:
            return None
        return props.get("ActiveState") == ActiveState.ACTIVE

    def stop_service(self, unit: str) -> str | None:
        res = SystemctlAdapter(
            self._rt.runner, on_host=SystemctlHost.LOCAL, timeout=_SYSTEMCTL_TIMEOUT_S, locale_c=True
        ).stop(unit)
        return None if res.returncode == 0 else res.failure(f"systemctl stop {unit}")

    def start_service(self, unit: str) -> str | None:
        res = SystemctlAdapter(
            self._rt.runner, on_host=SystemctlHost.LOCAL, timeout=_SYSTEMCTL_TIMEOUT_S, locale_c=True
        ).start(unit)
        return None if res.returncode == 0 else res.failure(f"systemctl start {unit}")

    def bind(self, source: str, target: str) -> str | None:
        rt = self._rt
        return run_mount(
            rt,
            ["mount", "--bind", map_path(source, rt, read_only=False), map_path(target, rt, read_only=False)],
            f"mount --bind {source} {target}",
        )

    def unbind(self, target: str) -> str | None:
        rt = self._rt
        return run_mount(
            rt,
            ["umount", map_path(target, rt, read_only=False)],
            f"umount {target}",
        )
