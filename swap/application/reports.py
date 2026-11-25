# SPDX-License-Identifier: MPL-2.0
"""The read-only use cases of swap: what plan and status show (no direct OS I/O or terminal formatting)."""

from collections.abc import Callable, Mapping
import posixpath
import re
from typing import Final

from cst_core.application.install import InstallRefused, InstallService
from cst_core.domain.files import ManagedState, MarkedLineSpec, OwnedState, inspect_managed_file
from cst_core.domain.plan import PlanItem
from cst_core.domain.values import format_bytes_human as human, parse_whole_number
from cst_core.ports import FileSystemPort, SystemdPort, UnitName
from perf.application import collect_memory_from_reader
from perf.domain.evaluate import evaluate_perf_memory
from perf.domain.models import PerfRootKind, PerfVerdict
from swap.application.environment import DROPIN_ENTRY, MissingDesiredFileError, SwapEnvironment
from swap.application.in_the_way import folder_in_place, non_folder_above
from swap.application.memory_tuning import (
    MM_TMPFILES_ENTRY,
    THP_EXPERIMENT,
    URESOURCED_BAK_SUFFIX,
    URESOURCED_ENTRY,
    SwapPerfReader,
    mm_settings_rows,
    uresourced_settings_row,
)
from swap.domain import (
    NOT_REGULAR_NOTE,
    crypto_has_algorithm,
    is_safe_partition_type,
    MACHINE_SLICE,
    WSF_EXPERIMENT,
    CgroupSwapRow,
    ConfigSectionReport,
    DebugCounterRow,
    DebugSectionReport,
    SettingCheckRow,
    SettingState,
    SettingsReport,
    SwapDeviceRow,
    SwapHardwareSpec,
    SwapPlanReport,
    SwapSpecState,
    SwapStatusReport,
    ZswapDebugStatus,
    can_evict_zram,
    cgroup_row,
    describe_state,
    find_swap_used_kib,
    DEFAULT_SWAP_NAME,
    SpecResolution,
    human_size,
    is_cst_dropin,
    memory_low_override,
    resolve_settings,
    sysfs_partition,
    parse_kv,
    parse_meminfo,
    parse_memory_low,
    parse_proc_swaps,
    parse_sysctl,
    parse_tmpfiles_params,
    psi_value,
    same_param,
    slice_not_active,
    state_note,
    zswap_summary,
)
from swap.ports import SwapSystemPort


# The row of machine.slice's live floor in status.
_MEMORY_LOW_ROW: Final = f"{MACHINE_SLICE} memory.low"


# A crypttab or fstab, or a file apply writes whole (the tmpfiles, sysctl and uresourced files, the slice
# drop-in), that root cannot read (a folder in its place, a security policy, a root-squashed mount): --root cannot
# help, and apply refuses it before the first write.
_ROOT_CANNOT_READ: Final = "cannot be read, even as root: apply refuses before it writes anything"
# A path in apply's way (swap.application.in_the_way), which apply refuses before its first write: the note of its
# row, whoever runs plan (after the name, for a name above a file apply writes whole that is not a folder).
_FOLDER_IN_PLACE_NOTE: Final = "a folder is in its place: apply refuses before it writes anything"
_NOT_A_FOLDER_NOTE: Final = "is not a folder: apply refuses before it writes anything"
# The drop-in folders of machine.slice that systemd reads at a daemon-reload (its own and every slice's).
_SLICE_DROPIN_FOLDERS: Final = ("machine.slice.d", "slice.d")
# What plan and apply say of a drop-in there that is not a regular file.
SLICE_DROPIN_NOT_REGULAR: Final = "not a regular file: systemd's daemon-reload would wait on it"
# The install record's row (check_record's refusal), and what that refusal says apply does.
_RECORD_ROW: Final = "install record"
_RECORD_REFUSAL_OUTCOME: Final = "apply stops before it writes anything"


def tmpfiles_compressor(text: str) -> str:
    """The compressor zswap.conf sets (its first compressor line); "" when it sets none."""
    return next((val for name, val in parse_tmpfiles_params(text) if name == "compressor"), "")


def _not_regular(fs: FileSystemPort, path: str) -> bool:
    """Whether path is there (a link that leads nowhere too) and is not a regular file after links."""
    return fs.lexists(path) and not fs.is_file(path)


def _root_note(env: SwapEnvironment, state: ManagedState, swap_name: str, verb: str, *, cannot_read: str) -> str:
    """The note of a row of plan and status: state_note's, except a file that cannot be read when the
    reader already runs as the host's root (env.host_root; root in a toolbox is not): cannot_read, with
    no --root advice."""
    if env.host_root and state == OwnedState.UNREADABLE:
        return cannot_read
    return state_note(state, swap_name, env.prog, verb)


def _short_etc_path(etc_dir: str, path: str) -> str:
    prefix = etc_dir.rstrip("/") + "/"
    return path.removeprefix(prefix).removeprefix("systemd/system/")


def _low_text(low: str) -> str:
    """A cgroup's memory.low as status shows it: a size, or the text itself ("0", "max", "-")."""
    number = parse_whole_number(low)  # ASCII digits only
    if number is None or number == 0:
        return low
    return human(number)


class _FileSettingsFacts:
    """swap.ports' SwapSettingsFacts over swap's file system (the paths of env; partition: the
    device a path leads to and its size, from sysfs)."""

    def __init__(
        self, fs: FileSystemPort, env: SwapEnvironment, partition: Callable[[str], tuple[str, int] | None]
    ) -> None:
        self._fs = fs
        self._env = env
        self._partition = partition

    # /etc's readers take regular_only: a FIFO or a device there is a file it cannot read, at once, never a wait.
    def settings_text(self) -> str | None:
        return self._fs.read_text(self._env.settings_path, regular_only=True)

    def settings_seen(self) -> bool:
        return self._fs.lexists(self._env.settings_path)

    def settings_is_file(self) -> bool:
        return self._fs.is_file(self._env.settings_path)

    def settings_readable(self) -> bool:
        return self._fs.is_readable(self._env.settings_path)

    def settings_folder_closed(self) -> bool:
        # lexists cannot see the file under a folder this user may not search, so it may be there.
        folder = posixpath.dirname(self._env.settings_path)
        while not self._fs.is_dir(folder) and folder not in ("", "/"):
            folder = posixpath.dirname(folder)
        return not self._fs.is_searchable(folder)

    def crypttab_closed(self) -> bool:
        return self._fs.exists(self._env.crypttab) and not self._fs.is_readable(self._env.crypttab)

    def crypttab_not_regular(self) -> bool:
        return _not_regular(self._fs, self._env.crypttab)

    def crypttab_text(self) -> str:
        return self._fs.read_text(self._env.crypttab, "", regular_only=True) or ""

    def fstab_text(self) -> str:
        return self._fs.read_text(self._env.fstab, "", regular_only=True) or ""

    def partition_size(self, device_path: str) -> int | None:
        part = self._partition(device_path)
        return None if part is None else part[1]


class SwapReports:
    """The plan and status use cases, and the inspection apply builds on (SwapApplicationService)."""

    def __init__(
        self,
        env: SwapEnvironment,
        fs: FileSystemPort,
        systemd: SystemdPort,
        system: SwapSystemPort,
        *,
        installer: InstallService,
    ) -> None:
        self._env = env
        self._fs = fs
        self._systemd = systemd
        self._system = system
        self._installer = installer

    @property
    def prog(self) -> str:
        """The command name in messages (`cst swap`)."""
        return self._env.prog

    @property
    def test_seam_note(self) -> str:
        """The line plan, status and apply print first when a test seam moved /proc or /sys ("": none), so
        a stale export in a root shell cannot pass for the kernel's state. The seam moves only those
        folders: /etc stays the real one, and apply's commands (sysctl, modprobe, zramctl) are the ones
        on PATH, so the line says that too."""
        seam = self._env.test_seam
        if not seam:
            return ""
        names = ", ".join(name for name, _kernel, _path in seam)
        moved = ", ".join(f"{kernel} in {path}" for _name, kernel, path in seam)
        return f"test seam ({names}): {moved}, not the kernel's; /etc is the real one, and sysctl, modprobe and zramctl run as PATH finds them"

    def _desired_text(self, path: str) -> str:
        text = self._fs.read_text(path)
        if text is None:
            raise MissingDesiredFileError(f"swap: {path} is missing (incomplete copy of CST?)")
        return text

    def _read_first(self, path: str, default: str) -> str:
        text = self._fs.read_first_line(path)
        return default if text is None else text

    def _in_proc_crypto(self, algorithm: str) -> bool:
        """Whether /proc/crypto lists algorithm now (none listed when it cannot be read)."""
        return crypto_has_algorithm(
            self._fs.read_text(posixpath.join(self._env.proc_dir, "crypto"), "") or "", algorithm
        )

    def _inspect_marked(self, path: str, spec: MarkedLineSpec) -> ManagedState:
        exists = self._fs.exists(path)
        if exists and not self._fs.is_readable(path):
            return OwnedState.UNREADABLE
        content = self._fs.read_text(path, regular_only=True) if exists else None  # a FIFO, a device: None
        readable = (not exists) or (content is not None)
        return spec.inspect(content, exists=exists, readable=readable)

    def _inspect_file(self, src: str, dest: str) -> ManagedState:
        desired = self._desired_text(src)
        is_symlink = self._fs.is_symlink(dest)
        exists = self._fs.exists(dest)
        if is_symlink:
            return inspect_managed_file(desired, None, exists=exists, is_symlink=True, readable=True)
        if not exists:
            return inspect_managed_file(desired, None, exists=False, is_symlink=False, readable=True)
        if not self._fs.is_readable(dest):
            return inspect_managed_file(desired, None, exists=True, is_symlink=False, readable=False)
        actual = self._fs.read_text(dest, regular_only=True)  # a FIFO, a device: None, as a file it cannot read
        return inspect_managed_file(desired, actual, exists=True, is_symlink=False, readable=actual is not None)

    def _uresourced_copy_due(self, dest: str, st: ManagedState) -> bool:
        """apply copies /etc/uresourced.conf (dest, whose _inspect_file state is st) aside before it writes it:
        when it is there (through a link) and not CST's (neither ok nor adopt)."""
        return self._fs.exists(dest) and st not in (OwnedState.OK, OwnedState.ADOPT)

    def _managed_state(self, src: str, dest: str) -> ManagedState:
        """The state of a file apply writes whole (src: its source in swap/), as plan shows it and apply checks it
        before its first write: _inspect_file's, except unreadable for /etc/uresourced.conf when apply would copy it
        aside and cannot read it (a link to a folder, to a file it may not read, or to anything but a file or a
        character device, such as a FIFO, whose open would block: the copy follows the link). A character device it
        may read (/dev/null: a masked vendor file) counts as an empty file. Nothing is read through the link. The
        installer replaces a link at any of the others itself, and reads nothing through it."""
        st = self._inspect_file(src, dest)
        if st == OwnedState.UNREADABLE or dest != self._env.etc_path(URESOURCED_ENTRY):
            return st
        copy_fails = (
            self._uresourced_copy_due(dest, st)
            and not self._fs.lexists(dest + URESOURCED_BAK_SUFFIX)
            and not ((self._fs.is_file(dest) or self._fs.is_char_device(dest)) and self._fs.is_readable(dest))
        )
        return OwnedState.UNREADABLE if copy_fails else st

    def old_dropin_is_ours(self) -> bool:
        """The drop-in under its old name is a regular file there (not opened in a way that waits) CST wrote."""
        return is_cst_dropin(self._fs.read_text(self._env.old_slice_dropin, "", regular_only=True) or "")

    def _zram_masked(self) -> bool:
        return self._fs.is_symlink(self._env.zram_conf) and self._fs.readlink(self._env.zram_conf) == "/dev/null"

    def _swap0_device(self, spec: SwapHardwareSpec) -> str:
        """The device /proc/swaps names for swap0: where its mapper path's link leads, or the path
        itself when it leads nowhere."""
        return self._fs.readlink(spec.mapper_path, canonicalize=True) or spec.mapper_path

    def _zram_evictable(self, zram_used: int) -> bool:
        """Whether free RAM (MemAvailable in /proc/meminfo) can take the zram0 swap in use now
        (zram_used, in KiB), so that swapping it off is safe."""
        meminfo = parse_meminfo(self._fs.read_text(posixpath.join(self._env.proc_dir, "meminfo"), "") or "")
        return can_evict_zram(zram_used, meminfo.get("MemAvailable", 0) // 1024)

    def _partition_size_sysfs(self, device_path: str) -> tuple[str, int] | None:
        return sysfs_partition(
            device_path,
            resolve=lambda path: self._fs.readlink(path, canonicalize=True),
            is_block=self._fs.is_block_device,
            first_line=self._fs.read_first_line,
            sys_dir=self._env.sys_dir,
        )

    def resolve_spec(self) -> SpecResolution:
        """The partition from /etc/cst/swap.conf, or the one an older CST set up (swap.domain's
        resolve_settings, over this file system)."""
        facts = _FileSettingsFacts(self._fs, self._env, self._partition_size_sysfs)
        return resolve_settings(facts, as_root=self._env.host_root)

    def _settings_item(self, res: SpecResolution, verb: str) -> PlanItem:
        """The row of /etc/cst/swap.conf in plan and status."""
        env = self._env
        target = _short_etc_path(env.etc_dir, env.settings_path)
        spec = res.spec
        if res.state == SwapSpecState.OK and spec is not None:
            return PlanItem("ok", target, f"partition {spec.partuuid}, {spec.expected_size_bytes} bytes")
        if res.state == SwapSpecState.WRITE and spec is not None:
            return PlanItem(
                "write",
                target,
                f"from the {spec.name} line an older CST wrote in crypttab: partition {spec.partuuid},"
                f" {spec.expected_size_bytes} bytes (its size now)",
            )
        if res.state == SwapSpecState.UNKNOWN:
            older = f"not written yet; an older CST set up {res.note} (fstab)"
            note = (
                f"{older}, and crypttab {_ROOT_CANNOT_READ}"
                if env.host_root
                else f"{older}: apply writes it from crypttab, which needs root to read: on the host, {env.prog} {verb} --root"
            )
            return PlanItem("?", target, note)
        if res.state == SwapSpecState.UNREADABLE:
            return PlanItem("?", target, f"cannot be read as this user: on the host, {env.prog} {verb} --root")
        if res.state == SwapSpecState.BAD:
            return PlanItem("REFUSE", target, f"{res.note}: apply stops")
        why = f" ({res.note})" if res.note else ""
        return PlanItem(
            "REFUSE",
            target,
            f"no swap partition configured{why}: set SWAP_PARTUUID and SWAP_SIZE_BYTES (swap/README.md, 2.3);"
            " apply stops",
        )

    def zswap_params(self) -> dict[str, str]:
        """Read current /sys/module/zswap/parameters entries."""
        out: dict[str, str] = {}
        try:
            names = self._fs.list_dir(self._env.zswap_params_dir)
        except OSError:
            return out
        for name in names:
            out[name] = self._read_first(posixpath.join(self._env.zswap_params_dir, name), "?")
        return out

    def _marked_line_items(self, spec: SwapHardwareSpec, verb: str) -> tuple[list[PlanItem], bool]:
        """The crypttab and fstab rows, and whether either line changes (so swap0 must be re-activated)."""
        env = self._env
        file_items: list[PlanItem] = []
        c_state = self._inspect_marked(env.crypttab, spec.crypttab_spec())
        f_state = self._inspect_marked(env.fstab, spec.fstab_spec())
        rows = (
            (c_state, env.crypttab, f"crypttab: {spec.name} line"),
            (f_state, env.fstab, f"fstab: {spec.mapper_path} line"),
        )
        for state, path, row in rows:
            if state == OwnedState.UNREADABLE and _not_regular(self._fs, path):
                # Root cannot help: no --root advice.
                note = f"{NOT_REGULAR_NOTE}: apply refuses; put a regular file there"
            else:
                note = _root_note(env, state, spec.name, verb, cannot_read=_ROOT_CANNOT_READ)
            file_items.append(PlanItem(describe_state(state), row, note))
        tier_change = any(st in (OwnedState.MISSING, OwnedState.CHANGE) for st in (c_state, f_state))
        return file_items, tier_change

    def _signature_item(self, spec: SwapHardwareSpec, verb: str) -> PlanItem | None:
        """apply's signature check, where apply makes it (the partition found with its size, swap0 not open):
        as the host's root, blkid with no cache file (plan writes nothing; /run/blkid is blkid's cache), and
        REFUSE for a signature apply would not overwrite; anyone else gets "?" (blkid needs root)."""
        if self._fs.is_block_device(spec.mapper_path):
            return None
        target = f"partition {spec.partuuid[:8]}..."
        if not self._env.host_root:
            return PlanItem(
                "?", target, f"its signature needs root (blkid): on the host, {self._env.prog} {verb} --root"
            )
        part_type = self._system.blkid_type(spec.device_path, cache=False)
        if is_safe_partition_type(part_type):
            return None
        return PlanItem("REFUSE", target, f"has a '{part_type}' signature: apply refuses to overwrite it")

    def _compressor_item(self) -> PlanItem | None:
        """apply's compressor check without its modprobe (plan loads nothing): "?" when the compressor of
        swap/tmpfiles is not in /proc/crypto now."""
        comp = tmpfiles_compressor(self._desired_text(self._env.src_tmpfiles))
        if not comp or self._in_proc_crypto(comp):
            return None
        return PlanItem(
            "?",
            f"compressor {comp}",
            "not in /proc/crypto now: apply loads its module (modprobe) and stops if it is still missing",
        )

    def _partition_items(
        self, spec: SwapHardwareSpec, swaps_text: str, *, tier_change: bool, verb: str
    ) -> list[PlanItem]:
        """The rows of the partition and of swap0 (swaps_text: /proc/swaps)."""
        runtime_items: list[PlanItem] = []
        part = self._partition_size_sysfs(spec.device_path)
        if part is not None:
            dev, size = part
            if size == spec.expected_size_bytes:
                runtime_items.append(
                    PlanItem("ok", f"partition {spec.partuuid[:8]}...", f"{dev}, {size} bytes as expected")
                )
                signature = self._signature_item(spec, verb)
                if signature is not None:
                    runtime_items.append(signature)
            else:
                runtime_items.append(
                    PlanItem(
                        "REFUSE",
                        f"partition {spec.partuuid[:8]}...",
                        f"{size} bytes, expected {spec.expected_size_bytes}: apply stops",
                    )
                )
        else:
            runtime_items.append(
                PlanItem("REFUSE", f"partition {spec.partuuid[:8]}...", f"{spec.device_path} not found: apply stops")
            )

        real_swap0 = self._swap0_device(spec)
        swap0_used = find_swap_used_kib(swaps_text, real_swap0)
        if swap0_used is not None:
            if tier_change:
                if swap0_used == 0:
                    runtime_items.append(
                        PlanItem("restart", spec.name, "active and empty: apply re-activates it with the new options")
                    )
                else:
                    runtime_items.append(
                        PlanItem(
                            "later",
                            spec.name,
                            f"active with {swap0_used // 1024} MiB in use: new options only after a reboot",
                        )
                    )
            else:
                runtime_items.append(PlanItem("ok", spec.name, f"active as swap ({real_swap0})"))
        else:
            runtime_items.append(
                PlanItem("start", spec.name, "not active: apply opens it (dm-crypt + mkswap) and swaps on")
            )
        return runtime_items

    def build_config_report(self, verb: str) -> ConfigSectionReport:
        """Inspect managed config files and runtime swap/zram states."""
        env = self._env
        if not self._fs.is_dir(env.etc_dir):
            return ConfigSectionReport(
                etc_visible=False,
                etc_dir=env.etc_dir,
                prog=env.prog,
                verb=verb,
                file_items=(),
                runtime_items=(),
            )

        res = self.resolve_spec()
        spec = res.spec
        file_items, tier_change = self._marked_line_items(spec, verb) if spec is not None else ([], False)
        file_items.insert(0, self._settings_item(res, verb))
        name = DEFAULT_SWAP_NAME if spec is None else spec.name

        for src, dest in env.managed_files:
            above = non_folder_above(self._fs, dest)
            if above is not None:
                file_items.append(PlanItem("?", _short_etc_path(env.etc_dir, dest), f"{above} {_NOT_A_FOLDER_NOTE}"))
                continue
            st = self._managed_state(src, dest)
            note = _root_note(env, st, name, verb, cannot_read=_ROOT_CANNOT_READ)
            file_items.append(PlanItem(describe_state(st), _short_etc_path(env.etc_dir, dest), note))

        if folder_in_place(self._fs, env.old_sysctl_conf):
            file_items.append(PlanItem("?", _short_etc_path(env.etc_dir, env.old_sysctl_conf), _FOLDER_IN_PLACE_NOTE))
        elif self._fs.exists(env.old_sysctl_conf):
            file_items.append(
                PlanItem("remove", _short_etc_path(env.etc_dir, env.old_sysctl_conf), "legacy file of the zram setup")
            )
        if self.old_dropin_is_ours():
            file_items.append(
                PlanItem(
                    "remove",
                    _short_etc_path(env.etc_dir, env.old_slice_dropin),
                    "the old name: a runtime MemoryLow lost to it after a daemon-reload",
                )
            )

        file_items.append(self._zram_conf_item())
        record = self._record_item(verb)
        if record is not None:
            file_items.append(record)
        dropin = self.slice_dropin_in_the_way()
        if dropin is not None:
            file_items.append(
                PlanItem("REFUSE", _short_etc_path(env.etc_dir, dropin), f"{SLICE_DROPIN_NOT_REGULAR}; apply stops")
            )

        swaps_text = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
        runtime_items = (
            [] if spec is None else self._partition_items(spec, swaps_text, tier_change=tier_change, verb=verb)
        )
        compressor = self._compressor_item()
        if compressor is not None:
            runtime_items.append(compressor)
        runtime_items.append(self._zram_runtime_item(swaps_text))

        return ConfigSectionReport(
            etc_visible=True,
            etc_dir=env.etc_dir,
            prog=env.prog,
            verb=verb,
            file_items=tuple(file_items),
            runtime_items=tuple(runtime_items),
        )

    def slice_dropin_in_the_way(self) -> str | None:
        """The first NAME.conf in machine.slice.d/ or slice.d/ of the system manager's unit folders that is
        there and is not a regular file after links (a FIFO, a device, a folder): every daemon-reload of apply
        makes systemd open it, and a FIFO's open waits for ever (PID 1 stops answering). A link that leads
        nowhere is no obstacle (systemd skips it). Only stat, never an open. None: none."""
        for base in self._env.slice_dropin_dirs:
            for sub in _SLICE_DROPIN_FOLDERS:
                folder = posixpath.join(base, sub)
                try:
                    names = sorted(self._fs.list_dir(folder)) if self._fs.is_dir(folder) else []
                except OSError:  # a folder this user may not list: plan cannot tell, apply (root) can
                    names = []
                for name in names:
                    path = posixpath.join(folder, name)
                    if path == self._env.slice_dropin:
                        continue  # CST's own: refused as a file apply writes whole ("cannot be read")
                    if name.endswith(".conf") and self._fs.exists(path) and not self._fs.is_file(path):
                        return path
        return None

    def _record_item(self, verb: str) -> PlanItem | None:
        """The install record's row when apply would refuse it before its first write (check_record: one of a
        newer CST, or one that is there and cannot be read); None when apply would take it. A reader that may
        not read it and is not the host's root gets "?": the host's root may."""
        try:
            self._installer.check_record(self._env.install_record, _RECORD_REFUSAL_OUTCOME)
        except InstallRefused as e:
            if isinstance(e.__cause__, PermissionError) and not self._env.host_root:
                return PlanItem(
                    "?", _RECORD_ROW, f"cannot be read as this user: on the host, {self._env.prog} {verb} --root"
                )
            return PlanItem("REFUSE", _RECORD_ROW, str(e))
        return None

    def _zram_conf_item(self) -> PlanItem:
        """The row of zram-generator.conf: masked, apply masks it, or a folder in its place that apply refuses."""
        path = _short_etc_path(self._env.etc_dir, self._env.zram_conf)
        if folder_in_place(self._fs, self._env.zram_conf):
            return PlanItem("?", path, _FOLDER_IN_PLACE_NOTE)
        if self._zram_masked():
            return PlanItem("ok", path, "masked (-> /dev/null): no zram devices at boot")
        return PlanItem("mask", path, "not masked: zram-generator would create zram0 at boot")

    def _zram_runtime_item(self, swaps_text: str) -> PlanItem:
        """The row of zram0 (swaps_text: /proc/swaps): not active, apply swaps it off, or only a reboot
        removes it (meminfo is read only when it is active)."""
        zram_used = find_swap_used_kib(swaps_text, "/dev/zram0")
        if zram_used is None:
            return PlanItem("ok", "zram0", "not active as swap")
        if self._zram_evictable(zram_used):
            return PlanItem("swapoff", "zram0", "active; free RAM can take its contents: apply swaps it off")
        return PlanItem("later", "zram0", "active; not enough free RAM to swap it off now: gone after a reboot")

    def build_settings_report(self, params: Mapping[str, str]) -> SettingsReport:
        """Compare live zswap, sysctl, and MemoryLow settings against swap/ desired values."""
        rows: list[SettingCheckRow] = []
        if not params:
            rows.append(SettingCheckRow(SettingState.BAD, "zswap", "not in this kernel (no /sys/module/zswap)", ""))
        for name, want in parse_tmpfiles_params(self._desired_text(self._env.src_tmpfiles)):
            live = params.get(name)
            if live is not None:
                rows.append(
                    SettingCheckRow(
                        SettingState.OK if same_param(live, want) else SettingState.DIFF, f"zswap {name}", live, want
                    )
                )
            elif params:
                # tmpfiles' w skips a file that is not there, without a word (renamed or removed by a kernel).
                rows.append(
                    SettingCheckRow(SettingState.MISSING, f"zswap {name}", "this kernel has no such parameter", want)
                )
        exp_name, exp_key = WSF_EXPERIMENT
        exp_on = self._fs.exists(posixpath.join(self._env.cpu_run_dir, "exp-" + exp_name))
        wsf_overridden = False
        for key, want in parse_sysctl(self._desired_text(self._env.src_sysctl)):
            live = self._read_first(posixpath.join(self._env.proc_dir, "sys", key.replace(".", "/")), "?")
            state = SettingState.OK if live == want else SettingState.DIFF
            if state == SettingState.DIFF and key == exp_key and exp_on:
                state = SettingState.EXP
                wsf_overridden = True
            rows.append(SettingCheckRow(state, key, live, want))
        mm_rows, thp_overridden = mm_settings_rows(
            self._fs,
            sys_dir=self._env.sys_dir,
            cpu_run_dir=self._env.cpu_run_dir,
            mm_text=self._desired_text(self._env.src_path(MM_TMPFILES_ENTRY)),
        )
        rows.extend(mm_rows)
        ures_row = uresourced_settings_row(
            self._fs,
            self._systemd,
            where=self._env.uresourced_view,
            uresourced_text=self._desired_text(self._env.src_path(URESOURCED_ENTRY)),
        )
        if ures_row is not None:
            rows.append(ures_row)
        row, source = self._memory_low_row(parse_memory_low(self._desired_text(self._env.src_dropin)))
        rows.append(row)
        return SettingsReport(
            rows=tuple(rows),
            experiments=tuple(
                exp for exp, on in ((WSF_EXPERIMENT, wsf_overridden), (THP_EXPERIMENT, thp_overridden)) if on
            ),
            override_source=source,
            cst_dropin=DROPIN_ENTRY.dest,
        )

    def _memory_low_row(self, want_low: int | None) -> tuple[SettingCheckRow, str]:
        """machine.slice's floor: "over" (and the drop-in that sets it) when systemd takes MemoryLow from a
        drop-in after CST's; else "-" when systemd says the slice is not active (a cgroup folder left
        behind is not the slice's) or the cgroup has no memory.low (an active slice's row says so: no
        memory controller for it); else the cgroup's memory.low against CST's value (DIFF when it differs)."""
        shown = self._systemd.show(UnitName(MACHINE_SLICE), ("MemoryLow", "DropInPaths", "ActiveState"))
        effective = shown.get("MemoryLow", "")
        listed = shown.get("DropInPaths", "").split()  # one not a regular file: None, as one it cannot read
        dropins = [(p, self._fs.read_text(self._env.host_view(p), regular_only=True)) for p in listed]
        source = memory_low_override(effective, dropins, DROPIN_ENTRY.dest, want_low) if effective else None
        if source is not None:
            return SettingCheckRow(
                SettingState.OVER, f"{MACHINE_SLICE} MemoryLow", human_size(effective), human(want_low)
            ), source
        active_state = shown.get("ActiveState", "")
        live_low = self._fs.read_first_line(posixpath.join(self._env.cgroup_dir, MACHINE_SLICE, "memory.low"))
        if slice_not_active(active_state) or (live_low is None and not active_state):
            return SettingCheckRow(
                SettingState.INACTIVE, _MEMORY_LOW_ROW, "slice not active (no VM running)", human(want_low)
            ), ""
        if live_low is None:
            # systemd says the slice is active, and its cgroup has no memory.low: no memory controller for it.
            return SettingCheckRow(
                SettingState.INACTIVE,
                _MEMORY_LOW_ROW,
                "slice active, no memory.low (memory controller off)",
                human(want_low),
            ), ""
        live_bytes = parse_whole_number(live_low)  # ASCII digits only
        ok = live_bytes == want_low
        live = human(live_bytes) if live_bytes is not None else live_low
        return SettingCheckRow(SettingState.OK if ok else SettingState.DIFF, _MEMORY_LOW_ROW, live, human(want_low)), ""

    def _build_debug_report(self, hint: str) -> DebugSectionReport:
        try:
            names = self._fs.list_dir(self._env.zswap_debug_dir)
        except PermissionError:
            return DebugSectionReport(self._env.zswap_debug_dir, ZswapDebugStatus.PERMISSION_DENIED, hint)
        except OSError:
            return DebugSectionReport(self._env.zswap_debug_dir, ZswapDebugStatus.UNAVAILABLE, hint)
        from swap.domain import DEBUG_HELP

        rows: list[DebugCounterRow] = []
        for name in names:
            val = self._read_first(posixpath.join(self._env.zswap_debug_dir, name), "?")
            if name == "pool_total_size" and (size := parse_whole_number(val)) is not None:
                val = f"{val} ({human(size)})"
            rows.append(DebugCounterRow(name, val, DEBUG_HELP.get(name, "")))
        return DebugSectionReport(self._env.zswap_debug_dir, ZswapDebugStatus.OK, hint, tuple(rows))

    def _build_devices(self) -> tuple[SwapDeviceRow, ...]:
        swaps_text = self._fs.read_text(posixpath.join(self._env.proc_dir, "swaps"), "") or ""
        out: list[SwapDeviceRow] = []
        for entry in parse_proc_swaps(swaps_text):
            name = self._fs.read_first_line(
                posixpath.join(self._env.sys_dir, "class", "block", posixpath.basename(entry.device), "dm", "name")
            )
            label = f"{entry.device} ({name})" if name is not None and name != "" else entry.device
            out.append(
                SwapDeviceRow(label, entry.swap_type, entry.used_kib * 1024, entry.size_kib * 1024, entry.priority)
            )
        return tuple(out)

    def _cgroups(self) -> list[tuple[str, str]]:
        cg = self._env.cgroup_dir
        out = [("(whole system)", cg)]
        out.extend((name, posixpath.join(cg, name)) for name in ("system.slice", MACHINE_SLICE, "builds.slice"))
        base = posixpath.join(cg, "user.slice")
        try:
            users = [d for d in self._fs.list_dir(base) if re.fullmatch(r"user-\d+\.slice", d)]
        except OSError:
            users = []
        for u in users:
            uid = u[5:-6]
            mgr = posixpath.join(base, u, f"user@{uid}.service")
            out.append((f"apps (uid {uid}, app.slice)", posixpath.join(mgr, "app.slice")))
            out.append((f"toolboxes (uid {uid}, user.slice)", posixpath.join(mgr, "user.slice")))
        return [(label, p) for label, p in out if self._fs.is_dir(p)]

    def _build_cgroup_rows(self) -> tuple[CgroupSwapRow, ...]:
        meminfo = parse_meminfo(self._fs.read_text(posixpath.join(self._env.proc_dir, "meminfo"), "") or "")
        rows: list[CgroupSwapRow] = []
        for label, path in self._cgroups():
            stat = parse_kv(self._fs.read_text(posixpath.join(path, "memory.stat"), "") or "")
            row = cgroup_row(stat, self._swap_current(path, meminfo))
            wb = self._read_first(posixpath.join(path, "memory.zswap.writeback"), "-")
            low = _low_text(self._read_first(posixpath.join(path, "memory.low"), "-"))
            psi = psi_value(self._fs.read_text(posixpath.join(path, "memory.pressure"), "") or "") or "-"
            rows.append(CgroupSwapRow(label, row, wb, low, psi))
        return tuple(rows)

    def _swap_current(self, path: str, meminfo: Mapping[str, int]) -> int | None:
        """The cgroup's memory.swap.current in bytes; the root cgroup has none, so the whole system's
        swap in use comes from meminfo there. None when it is not a number."""
        cur = self._fs.read_first_line(posixpath.join(path, "memory.swap.current"))
        if cur is None and path == self._env.cgroup_dir and "SwapTotal" in meminfo:
            cur = str(meminfo["SwapTotal"] - meminfo.get("SwapFree", 0))
        return None if cur is None else parse_whole_number(cur)  # ASCII digits only

    def plan_report(self) -> SwapPlanReport:
        """Compute dry-run plan without modifying files or system state."""
        config = self.build_config_report("plan")
        settings = self.build_settings_report(self.zswap_params())
        return SwapPlanReport(config=config, settings=settings, prog=self._env.prog)

    def status_report(
        self,
        *,
        hint: str = "",
        settings_only: bool = False,
        include_config: bool = True,
    ) -> SwapStatusReport:
        """Collect read-only status across config files and live kernel state."""
        config = self.build_config_report("status") if (include_config and not settings_only) else None
        params = self.zswap_params()
        settings = self.build_settings_report(params)
        if settings_only:
            return SwapStatusReport(
                config=config,
                settings=settings,
                settings_only=True,
                params=params,
                summary=None,
                debug=None,
                devices=(),
                cgroups=(),
                psi_lines=(),
            )
        meminfo = parse_meminfo(self._fs.read_text(posixpath.join(self._env.proc_dir, "meminfo"), "") or "")
        vmstat = parse_kv(self._fs.read_text(posixpath.join(self._env.proc_dir, "vmstat"), "") or "")
        summary = zswap_summary(meminfo, vmstat, params)
        debug = self._build_debug_report(hint)
        devices = self._build_devices()
        cgroups = self._build_cgroup_rows()
        psi_text = self._fs.read_text(posixpath.join(self._env.proc_dir, "pressure", "memory")) or "  not available\n"
        perf_roots = {
            PerfRootKind.PROC: self._env.proc_dir,
            PerfRootKind.SYS: self._env.sys_dir,
            PerfRootKind.CGROUP: self._env.cgroup_dir,
            PerfRootKind.ETC: self._env.etc_dir,
            PerfRootKind.ZSWAP_PARAMS: self._env.zswap_params_dir,
        }
        mem_diag = evaluate_perf_memory(collect_memory_from_reader(SwapPerfReader(self._fs, perf_roots)))
        diagnosis = mem_diag.findings if mem_diag.verdict is not PerfVerdict.SKIP else None
        return SwapStatusReport(
            config=config,
            settings=settings,
            settings_only=False,
            params=params,
            summary=summary,
            debug=debug,
            devices=devices,
            cgroups=cgroups,
            psi_lines=tuple(psi_text.splitlines()),
            diagnosis=diagnosis,
        )
