# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapter for `cst btrfs cache provision` and `unprovision`."""

from __future__ import annotations

from collections.abc import Generator
import contextlib
import errno
import os
import stat
import sys
import tempfile
from typing import Final, TypedDict

from btrfs.cache.adapters.mounts import MOUNT_TIMEOUT_S, CacheMountRuntime, from_toolbox, map_path
from btrfs.cache.domain import OPT_COMPRESS_FORCE, CacheMountKind
from btrfs.cache.domain.provision import (
    BASE_DIR_MODE,
    BASE_MOUNT_OPTIONS,
    CACHE_CONF_PATH,
    CONF_MODE,
    CRYPTTAB_MODE,
    CRYPTTAB_PATH,
    DEFAULT_SUBVOL_TOP_PREFIX,
    KEYFILE_BYTES,
    KEYFILE_MODE,
    KEYS_DIR_MODE,
    NOT_REGULAR_FILE_NOTE,
    SUBVOL_FOR_KIND,
    BlockDeviceFacts,
)
from btrfs.cache.ports import CachePathKind
from cst_core.adapters.files import FileOwner, LocalFileSystemAdapter, ensure_dir, read_text, write_atomic
from cst_core.domain.shapes import json_object, parse_json
from cst_core.domain.text import is_yes
from cst_core.domain.values import parse_whole_number
from cst_core.ports import OutputMode

_PROBE_TIMEOUT_S: Final = 15
_CRYPTO_TIMEOUT_S: Final = 120
_MKFS_TIMEOUT_S: Final = 120
_BLKID_RC_BLANK: Final = 2
_BLKID_RC_AMBIVALENT: Final = 8
_LUKS_UUID_MIN_PARTS: Final = 3
_HEX_UUID_LEN: Final = 32
_UUID_SLICE_1: Final = 8
_UUID_SLICE_2: Final = 12
_UUID_SLICE_3: Final = 16
_UUID_SLICE_4: Final = 20
_LSBLK_COLUMNS: Final = "NAME,TYPE,PARTUUID,SIZE,MODEL,FSTYPE,PTTYPE,UUID,MOUNTPOINTS"
_BLKSWAPS_PATH: Final = "/proc/swaps"


class _LsblkRow(TypedDict, total=False):
    name: str | None
    type: str | None
    partuuid: str | None
    size: int | str | None
    model: str | None
    fstype: str | None
    pttype: str | None
    uuid: str | None
    mountpoints: list[str | None] | None


def _read_regular_guarded(path: str, label: str, *, nonblock_note: bool = True) -> tuple[str | None, str | None]:
    try:
        return LocalFileSystemAdapter().read_existing_text(path, regular_only=True), None
    except OSError as exc:
        if nonblock_note and exc.errno == errno.EINVAL:
            return None, f"{label} is {NOT_REGULAR_FILE_NOTE}"
        return None, f"cannot read {label}: {exc.strerror or exc}"


def _stat_kind(path: str) -> CachePathKind:
    try:
        st = os.lstat(path)
    except OSError:
        return CachePathKind(exists=False, is_dir=False, is_symlink=False, is_regular=False)
    if stat.S_ISLNK(st.st_mode):
        try:
            tgt = os.stat(path)
            return CachePathKind(
                exists=True,
                is_dir=stat.S_ISDIR(tgt.st_mode),
                is_symlink=True,
                is_regular=stat.S_ISREG(tgt.st_mode),
            )
        except OSError:
            return CachePathKind(exists=True, is_dir=False, is_symlink=True, is_regular=False)
    return CachePathKind(
        exists=True,
        is_dir=stat.S_ISDIR(st.st_mode),
        is_symlink=False,
        is_regular=stat.S_ISREG(st.st_mode),
    )


def _format_dm_luks_uuid(dm_uuid: str) -> str | None:
    if not dm_uuid.startswith("CRYPT-LUKS"):
        return None
    parts = dm_uuid.split("-")
    if len(parts) < _LUKS_UUID_MIN_PARTS or len(parts[2]) != _HEX_UUID_LEN:
        return None
    h = parts[2].lower()
    return f"{h[:_UUID_SLICE_1]}-{h[_UUID_SLICE_1:_UUID_SLICE_2]}-{h[_UUID_SLICE_2:_UUID_SLICE_3]}-{h[_UUID_SLICE_3:_UUID_SLICE_4]}-{h[_UUID_SLICE_4:]}"


def _lsblk_size(raw_size: object) -> int:
    if isinstance(raw_size, bool):
        return 0
    if isinstance(raw_size, int):
        return raw_size
    if isinstance(raw_size, str):
        parsed = parse_whole_number(raw_size)
        return parsed if parsed is not None else 0
    return 0


def _parse_lsblk_entry(stdout: str, device_path: str) -> tuple[_LsblkRow | None, str | None]:
    try:
        data = parse_json(stdout)
    except ValueError:
        return None, f"lsblk output for {device_path} is not valid JSON"
    if not json_object(data):
        return None, f"lsblk output for {device_path} is not valid JSON"
    devs = data.get("blockdevices")
    if not isinstance(devs, list) or not devs or not json_object(devs[0]):
        return None, f"lsblk returned no block device for {device_path}"
    raw = devs[0]
    mps_raw = raw.get("mountpoints")
    mps: list[str | None] | None = (
        [m if isinstance(m, str) else None for m in mps_raw] if isinstance(mps_raw, list) else None
    )
    size_raw = raw.get("size")
    size_val: int | str | None = size_raw if isinstance(size_raw, int | str) else None
    return (
        _LsblkRow(
            name=raw.get("name") if isinstance(raw.get("name"), str) else None,
            type=raw.get("type") if isinstance(raw.get("type"), str) else None,
            partuuid=raw.get("partuuid") if isinstance(raw.get("partuuid"), str) else None,
            size=size_val,
            model=raw.get("model") if isinstance(raw.get("model"), str) else None,
            fstype=raw.get("fstype") if isinstance(raw.get("fstype"), str) else None,
            pttype=raw.get("pttype") if isinstance(raw.get("pttype"), str) else None,
            uuid=raw.get("uuid") if isinstance(raw.get("uuid"), str) else None,
            mountpoints=mps,
        ),
        None,
    )


def _parse_blkid_export(stdout: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep and k:
            pairs[k.strip()] = v.strip()
    return pairs


def _sysfs_holders(kname: str, rt: CacheMountRuntime) -> tuple[tuple[str, ...], tuple[str, ...]]:
    holders_dir = map_path(f"/sys/class/block/{kname}/holders", rt, read_only=True)
    try:
        entries = sorted(os.listdir(holders_dir))
    except OSError:
        return (), ()
    dm_names: list[str] = []
    for h in entries:
        name = (read_text(map_path(f"/sys/class/block/{h}/dm/name", rt, read_only=True), "") or "").strip()
        if name:
            dm_names.append(name)
    return tuple(entries), tuple(dm_names)


def _is_swap_device(real_path: str, rt: CacheMountRuntime) -> bool:
    swaps_text = read_text(map_path(_BLKSWAPS_PATH, rt, read_only=True)) or ""
    for line in swaps_text.splitlines()[1:]:
        parts = line.split()
        if parts and os.path.realpath(parts[0]) == real_path:
            return True
    return False


class CacheProvisionAdapter:
    """CacheProvisionPort implementation over lsblk/blkid/cryptsetup/mkfs.btrfs/mount."""

    def __init__(self, rt: CacheMountRuntime) -> None:
        self._rt = rt

    def read_conf(self) -> tuple[str | None, str | None]:
        return _read_regular_guarded(map_path(CACHE_CONF_PATH, self._rt, read_only=True), CACHE_CONF_PATH)

    def write_conf(self, text: str) -> None:
        write_atomic(map_path(CACHE_CONF_PATH, self._rt, read_only=False), text, mode=CONF_MODE)

    def read_crypttab_exact(self) -> tuple[str | None, str | None]:
        return _read_regular_guarded(map_path(CRYPTTAB_PATH, self._rt, read_only=True), CRYPTTAB_PATH)

    def write_crypttab(self, text: str) -> None:
        mapped = map_path(CRYPTTAB_PATH, self._rt, read_only=False)
        mode = CRYPTTAB_MODE
        try:
            st = os.lstat(mapped)
            if not stat.S_ISLNK(st.st_mode):
                mode = stat.S_IMODE(st.st_mode)
        except OSError:
            pass
        write_atomic(mapped, text, mode=mode)

    def remove_crypttab(self) -> None:
        LocalFileSystemAdapter().remove_file(map_path(CRYPTTAB_PATH, self._rt, read_only=False))

    def inspect_device(self, device_path: str) -> tuple[BlockDeviceFacts | None, str | None]:
        on_host = from_toolbox(self._rt)
        res = self._rt.runner.run(
            ["lsblk", "-J", "-b", "-p", "-o", _LSBLK_COLUMNS, device_path],
            on_host=on_host,
            timeout=_PROBE_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        if res.returncode != 0:
            return None, res.failure(f"lsblk {device_path}")
        entry, parse_err = _parse_lsblk_entry(res.stdout, device_path)
        if parse_err is not None or entry is None:
            return None, parse_err
        name_val = entry.get("name")
        real_path = name_val or device_path
        if not self._rt.seam_root and os.path.exists(real_path):
            real_path = os.path.realpath(real_path)
        blkid, blkid_err = self._run_blkid(device_path, on_host=on_host)
        if blkid_err is not None:
            return None, blkid_err
        raw_mps = entry.get("mountpoints")
        mps = tuple(m for m in raw_mps if m is not None and m != "") if raw_mps is not None else ()
        kname = os.path.basename(real_path)
        holders, holder_dm = _sysfs_holders(kname, self._rt)
        dev_type = entry.get("type") or ""
        # On a partition, lsblk's PTTYPE reports the enclosing disk's table (udev ID_PART_TABLE_TYPE),
        # not a nested partition table inside the partition; only blkid -p detects an inner PTTYPE.
        pttype = blkid.get("PTTYPE", "") if dev_type == "part" else (entry.get("pttype") or blkid.get("PTTYPE", ""))
        return (
            BlockDeviceFacts(
                dev_path=device_path,
                real_path=real_path,
                dev_type=dev_type,
                partuuid=(entry.get("partuuid") or blkid.get("PARTUUID") or blkid.get("PART_ENTRY_UUID", "")).lower(),
                size_bytes=_lsblk_size(entry.get("size")),
                model=(entry.get("model") or "").strip(),
                fstype=entry.get("fstype") or blkid.get("TYPE", ""),
                pttype=pttype,
                uuid=entry.get("uuid") or blkid.get("UUID", ""),
                mountpoints=mps,
                holders=holders,
                holder_dm_names=holder_dm,
                is_swap=_is_swap_device(real_path, self._rt),
            ),
            None,
        )

    def _run_blkid(self, target: str, *, on_host: bool) -> tuple[dict[str, str], str | None]:
        res = self._rt.runner.run(
            ["blkid", "-p", "-o", "export", target],
            on_host=on_host,
            timeout=_PROBE_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        pairs: dict[str, str] = {}
        err: str | None = None
        if res.returncode == 0:
            pairs = _parse_blkid_export(res.stdout)
        elif res.returncode == _BLKID_RC_AMBIVALENT:
            err = f"{target} has ambivalent/conflicting signatures (blkid -p rc={_BLKID_RC_AMBIVALENT}); inspect with wipefs"
        elif res.returncode != _BLKID_RC_BLANK and os.geteuid() == 0 and not on_host:
            err = res.failure(f"blkid -p {target}")
        return pairs, err

    def stat_path(self, path: str) -> CachePathKind:
        return _stat_kind(map_path(path, self._rt, read_only=True))

    def mapper_fs_info(self, mapper_path: str) -> tuple[str, str, str, str | None]:
        on_host = from_toolbox(self._rt)
        if not self.stat_path(mapper_path).exists:
            return "", "", "", None
        blkid, blkid_err = self._run_blkid(mapper_path, on_host=on_host)
        luks_uuid: str | None = None
        mapper_name = os.path.basename(mapper_path)
        dm = self._rt.runner.run(
            ["dmsetup", "info", "-c", "--noheadings", "-o", "uuid", mapper_name],
            on_host=on_host,
            timeout=_PROBE_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        if dm.returncode == 0:
            luks_uuid = _format_dm_luks_uuid(dm.stdout.strip())
        fstype = "ambivalent" if blkid_err is not None else blkid.get("TYPE", "")
        return fstype, blkid.get("UUID", ""), blkid.get("LABEL", ""), luks_uuid

    def luks_uuid(self, device_path: str) -> str | None:
        res = self._rt.runner.run(
            ["cryptsetup", "luksUUID", device_path],
            on_host=from_toolbox(self._rt),
            timeout=_PROBE_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        val = res.stdout.strip()
        return val if res.returncode == 0 and val else None

    def test_keyfile(self, device_path: str, keyfile_path: str) -> bool:
        on_host = from_toolbox(self._rt)
        kf_arg = keyfile_path if on_host else map_path(keyfile_path, self._rt, read_only=True)
        res = self._rt.runner.run(
            ["cryptsetup", "open", "--test-passphrase", "--key-file", kf_arg, device_path],
            on_host=on_host,
            timeout=_CRYPTO_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return res.returncode == 0

    def create_keyfile(self, keyfile_path: str) -> None:
        mapped = map_path(keyfile_path, self._rt, read_only=False)
        ensure_dir(os.path.dirname(mapped), KEYS_DIR_MODE, relabel=True)
        if os.path.lexists(mapped):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), mapped)
        write_atomic(mapped, os.urandom(KEYFILE_BYTES), mode=KEYFILE_MODE, owner=FileOwner.ROOT)

    def remove_file(self, path: str) -> None:
        LocalFileSystemAdapter().remove_file(map_path(path, self._rt, read_only=False))

    def luks_format(self, device_path: str, keyfile_path: str) -> str | None:
        mapped_kf = map_path(keyfile_path, self._rt, read_only=False)
        res = self._rt.runner.run(
            ["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--key-file", mapped_kf, device_path],
            timeout=_CRYPTO_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure("cryptsetup luksFormat")

    def luks_open(self, device_path: str, mapper_name: str, keyfile_path: str) -> str | None:
        mapped_kf = map_path(keyfile_path, self._rt, read_only=False)
        res = self._rt.runner.run(
            [
                "cryptsetup",
                "open",
                "--allow-discards",
                "--perf-no_read_workqueue",
                "--perf-no_write_workqueue",
                "--key-file",
                mapped_kf,
                device_path,
                mapper_name,
            ],
            timeout=_CRYPTO_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"cryptsetup open {mapper_name}")

    def luks_close(self, mapper_name: str) -> str | None:
        res = self._rt.runner.run(
            ["cryptsetup", "close", mapper_name],
            timeout=_CRYPTO_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"cryptsetup close {mapper_name}")

    def mkfs_btrfs(self, mapper_path: str, label: str) -> str | None:
        res = self._rt.runner.run(
            ["mkfs.btrfs", "-f", "-L", label, "-m", "dup", "-d", "single", mapper_path],
            timeout=_MKFS_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"mkfs.btrfs {mapper_path}")

    @contextlib.contextmanager
    def top_mount(self, mapper_path: str) -> Generator[tuple[str | None, str | None]]:
        run_root = map_path("/run", self._rt, read_only=False)
        ensure_dir(run_root, BASE_DIR_MODE, relabel=False)
        mnt = tempfile.mkdtemp(prefix=DEFAULT_SUBVOL_TOP_PREFIX, dir=run_root)
        try:
            res = self._rt.runner.run(
                ["mount", "-t", "btrfs", "-o", "subvolid=5", mapper_path, mnt],
                timeout=MOUNT_TIMEOUT_S,
                locale_c=True,
                output=OutputMode.CAPTURE,
            )
            if res.returncode != 0:
                yield None, res.failure(f"mount subvolid=5 {mapper_path}")
            else:
                try:
                    yield mnt, None
                finally:
                    self._rt.runner.run(
                        ["umount", mnt], timeout=MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE
                    )
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(mnt)

    def _existing_subvol_names(self, mnt: str) -> tuple[tuple[str, ...] | None, str | None]:
        res = self._rt.runner.run(
            ["btrfs", "subvolume", "list", mnt],
            timeout=_PROBE_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        if res.returncode != 0:
            return None, res.failure(f"btrfs subvolume list {mnt}")
        names: list[str] = []
        for line in res.stdout.splitlines():
            marker = " path "
            idx = line.rfind(marker)
            if idx != -1:
                names.append(line[idx + len(marker) :].strip())
        return tuple(names), None

    def list_top_subvolumes(self, mapper_path: str) -> tuple[tuple[str, ...], str | None]:
        if from_toolbox(self._rt):
            return (), None
        with self.top_mount(mapper_path) as (mnt, merr):
            if merr is not None or mnt is None:
                return (), merr
            names, serr = self._existing_subvol_names(mnt)
            return names or (), serr

    def ensure_subvolumes(self, mapper_path: str, names: tuple[str, ...]) -> tuple[tuple[str, ...], str | None]:
        created: list[str] = []
        with self.top_mount(mapper_path) as (mnt, merr):
            if merr is not None or mnt is None:
                return (), merr
            existing, serr = self._existing_subvol_names(mnt)
            if serr is not None or existing is None:
                return (), serr
            present = set(existing)
            for name in names:
                if name in present:
                    continue
                res = self._rt.runner.run(
                    ["btrfs", "subvolume", "create", f"{mnt}/{name}"],
                    timeout=_PROBE_TIMEOUT_S,
                    locale_c=True,
                    output=OutputMode.CAPTURE,
                )
                if res.returncode != 0:
                    return tuple(created), res.failure(f"btrfs subvolume create {name}")
                created.append(name)
        return tuple(created), None

    def ensure_base_dir(self, path: str) -> None:
        ensure_dir(map_path(path, self._rt, read_only=False), BASE_DIR_MODE, relabel=True)

    def remove_empty_dir(self, path: str) -> None:
        with contextlib.suppress(OSError):
            os.rmdir(map_path(path, self._rt, read_only=False))

    def mount_base(self, mapper_path: str, kind: CacheMountKind, target: str) -> str | None:
        mapped_target = map_path(target, self._rt, read_only=False)
        opts = f"subvol={SUBVOL_FOR_KIND[kind]},{BASE_MOUNT_OPTIONS}"
        res = self._rt.runner.run(
            ["mount", "-t", "btrfs", "-o", opts, mapper_path, mapped_target],
            timeout=MOUNT_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"mount {mapper_path} {target}")

    def remount_base(self, target: str) -> str | None:
        mapped_target = map_path(target, self._rt, read_only=False)
        res = self._rt.runner.run(
            ["mount", "-o", f"remount,{OPT_COMPRESS_FORCE}", mapped_target],
            timeout=MOUNT_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"mount -o remount,{OPT_COMPRESS_FORCE} {target}")

    def umount_base(self, target: str) -> str | None:
        mapped_target = map_path(target, self._rt, read_only=False)
        res = self._rt.runner.run(
            ["umount", mapped_target],
            timeout=MOUNT_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"umount {target}")

    def scrub_base(self, target: str) -> str | None:
        mapped_target = map_path(target, self._rt, read_only=False)
        res = self._rt.runner.run(
            ["btrfs", "scrub", "start", "-B", mapped_target],
            timeout=_MKFS_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return None if res.returncode == 0 else res.failure(f"btrfs scrub start -B {target}")

    def confirm_format(self, prompt: str, *, assume_yes: bool) -> tuple[bool, str | None]:
        if assume_yes:
            return True, None
        if not sys.stdin.isatty():
            return False, "stdin is not a terminal; pass --yes with --format to confirm non-interactive formatting"
        print(prompt, end="", flush=True)
        try:
            ans = sys.stdin.readline()
        except OSError:
            return False, "cannot read confirmation from stdin"
        if is_yes(ans):
            return True, None
        return False, "aborted by user (confirmation declined)"
