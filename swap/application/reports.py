# SPDX-License-Identifier: MPL-2.0
"""The read-only use cases of swap: what plan and status show (no direct OS I/O or terminal formatting)."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
import posixpath
import re

from cst_core.application.install import InstallService
from cst_core.domain.files import ManagedState, MarkedLineSpec, OwnedState, inspect_managed_file
from cst_core.domain.install import InstallEntry, manifest_by_src
from cst_core.domain.plan import PlanItem
from cst_core.domain.values import format_bytes_human as human
from cst_core.ports import FileSystemPort, SystemdPort, UnitName
from swap.domain import (
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
    SETTINGS_REL,
    SpecResolution,
    human_size,
    is_cst_dropin,
    marked_crypttab_entry,
    marked_fstab_name,
    memory_low_override,
    parse_swap_settings,
    partuuid_path,
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


# The files swap installs: where they go and with which mode comes from the
# install manifest (cst_core/domain/install.py), the one list of installed files.
_MANIFEST = manifest_by_src()
TMPFILES_ENTRY: InstallEntry = _MANIFEST["swap/tmpfiles/zswap.conf"]
SYSCTL_ENTRY: InstallEntry = _MANIFEST["swap/sysctl/99-swap-tuning.conf"]
DROPIN_ENTRY: InstallEntry = _MANIFEST["swap/units/machine.slice.d-10-swap-memory-low.conf"]
# The drop-in's name until 2026-11: it sorted after the 50-MemoryLow.conf of `systemctl set-property`
# ('M' < 'm'), so after a daemon-reload its 48G beat a runtime value. plan shows it, apply removes it.
OLD_DROPIN_ENTRY: InstallEntry = replace(
    DROPIN_ENTRY,
    src="swap/units/machine.slice.d-50-memory-low.conf",
    dest="/etc/systemd/system/machine.slice.d/50-memory-low.conf",
)


def _under(base: str, manifest_path: str, prefix: str) -> str:
    """manifest_path (which starts with prefix) moved under base."""
    return posixpath.join(base, manifest_path[len(prefix) :])


class MissingDesiredFileError(RuntimeError):
    """Raised when a desired config file inside CST's swap/ directory is missing."""


@dataclass(frozen=True, slots=True)
class SwapEnvironment:
    """Path and identity configuration injected into SwapApplicationService."""

    src_dir: str
    etc_dir: str
    proc_dir: str = "/proc"
    sys_dir: str = "/sys"
    cgroup_dir: str = "/sys/fs/cgroup"
    zswap_params_dir: str = "/sys/module/zswap/parameters"
    zswap_debug_dir: str = "/sys/kernel/debug/zswap"
    cpu_run_dir: str = "/run/cpu-setup"
    prog: str = "cst swap"
    # The install record that lists swap's three files ("": not recorded) and whether apply runs as
    # root (a root file owned by another account is then rewritten, as by the installer).
    install_record: str = ""
    as_root: bool = False
    # A test's seam that moved /proc or /sys: (variable, the kernel's folder, where it is read now).
    test_seam: tuple[tuple[str, str, str], ...] = ()

    def host_view(self, host_path: str) -> str:
        """Where a host path (one systemd names) is seen from here: under the folder etc_dir is in."""
        root = self.etc_dir.rstrip("/").removesuffix("/etc")
        return root + host_path

    @property
    def settings_path(self) -> str:
        """/etc/cst/swap.conf: the partition swap manages."""
        return posixpath.join(self.etc_dir, SETTINGS_REL)

    @property
    def crypttab(self) -> str:
        return posixpath.join(self.etc_dir, "crypttab")

    @property
    def fstab(self) -> str:
        return posixpath.join(self.etc_dir, "fstab")

    @property
    def tmpfiles_conf(self) -> str:
        return _under(self.etc_dir, TMPFILES_ENTRY.dest, "/etc/")

    @property
    def sysctl_conf(self) -> str:
        return _under(self.etc_dir, SYSCTL_ENTRY.dest, "/etc/")

    @property
    def old_sysctl_conf(self) -> str:
        return posixpath.join(self.etc_dir, "sysctl.d", "99-zram-tuning.conf")

    @property
    def slice_dropin(self) -> str:
        return _under(self.etc_dir, DROPIN_ENTRY.dest, "/etc/")

    @property
    def old_slice_dropin(self) -> str:
        return _under(self.etc_dir, OLD_DROPIN_ENTRY.dest, "/etc/")

    @property
    def zram_conf(self) -> str:
        return posixpath.join(self.etc_dir, "systemd", "zram-generator.conf")

    @property
    def src_tmpfiles(self) -> str:
        return _under(self.src_dir, TMPFILES_ENTRY.src, "swap/")

    @property
    def src_sysctl(self) -> str:
        return _under(self.src_dir, SYSCTL_ENTRY.src, "swap/")

    @property
    def src_dropin(self) -> str:
        return _under(self.src_dir, DROPIN_ENTRY.src, "swap/")


def _short_etc_path(etc_dir: str, path: str) -> str:
    prefix = etc_dir.rstrip("/") + "/"
    return path.removeprefix(prefix).removeprefix("systemd/system/")


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

    def _inspect_marked(self, path: str, spec: MarkedLineSpec) -> ManagedState:
        exists = self._fs.exists(path)
        if exists and not self._fs.is_readable(path):
            return OwnedState.UNREADABLE
        content = self._fs.read_text(path) if exists else None
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
        actual = self._fs.read_text(dest)
        return inspect_managed_file(desired, actual, exists=True, is_symlink=False, readable=actual is not None)

    def old_dropin_is_ours(self) -> bool:
        """The drop-in under its old name is there and CST (or the script before it) wrote it."""
        text = self._fs.read_text(self._env.old_slice_dropin)
        return text is not None and is_cst_dropin(text)

    def _zram_masked(self) -> bool:
        return self._fs.is_symlink(self._env.zram_conf) and self._fs.readlink(self._env.zram_conf) == "/dev/null"

    def _partition_size_sysfs(self, device_path: str) -> tuple[str, int] | None:
        dev = self._fs.readlink(device_path, canonicalize=True)
        if dev is None or not self._fs.is_block_device(dev):
            return None
        sectors = self._fs.read_first_line(
            posixpath.join(self._env.sys_dir, "class", "block", posixpath.basename(dev), "size")
        )
        if sectors is None or not sectors.isdigit():
            return None
        return dev, int(sectors) * 512

    def resolve_spec(self) -> SpecResolution:
        """The partition from /etc/cst/swap.conf; when that file is not there, the one an older CST set
        up (its marked crypttab line, the partition's size now: apply writes the file from them). A file
        that is there but cannot be read is never taken as missing."""
        env = self._env
        text = self._fs.read_text(env.settings_path)
        if text is not None:
            settings = parse_swap_settings(text)
            if settings.problems:
                return SpecResolution(None, SwapSpecState.BAD, "; ".join(settings.problems))
            return SpecResolution(settings.spec, SwapSpecState.UNSET if settings.spec is None else SwapSpecState.OK)
        if self._fs.lexists(env.settings_path):
            return self._settings_not_read()
        return self._spec_from_older_install()

    def _settings_not_read(self) -> SpecResolution:
        """/etc/cst/swap.conf is there and could not be read: never taken as missing (the migration
        would write over it). A user who may not read it is asked for --root; anything else stops apply."""
        path = self._env.settings_path
        if not self._fs.is_file(path):
            return SpecResolution(None, SwapSpecState.BAD, "not a regular file")
        if not self._fs.is_readable(path):
            return SpecResolution(None, SwapSpecState.UNREADABLE)
        return SpecResolution(None, SwapSpecState.BAD, "cannot be read")

    def _spec_from_older_install(self) -> SpecResolution:
        """No /etc/cst/swap.conf: the partition an older CST set up (its marked crypttab line, the
        partition's size now), from which apply writes the file."""
        env = self._env
        if self._fs.exists(env.crypttab) and not self._fs.is_readable(env.crypttab):
            name = marked_fstab_name(self._fs.read_text(env.fstab, "") or "")
            return (
                SpecResolution(None, SwapSpecState.UNSET)
                if name is None
                else SpecResolution(None, SwapSpecState.UNKNOWN, name)
            )
        entry = marked_crypttab_entry(self._fs.read_text(env.crypttab, "") or "")
        if entry is None:
            return SpecResolution(None, SwapSpecState.UNSET)
        name, partuuid = entry
        part = self._partition_size_sysfs(partuuid_path(partuuid))
        if part is None:
            return SpecResolution(
                None,
                SwapSpecState.UNSET,
                f"the {name} line in crypttab names {partuuid_path(partuuid)}, which is not here",
            )
        return SpecResolution(SwapHardwareSpec(partuuid, name, part[1]), SwapSpecState.WRITE, name)

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
            return PlanItem(
                "?",
                target,
                f"not written yet; an older CST set up {res.note} (fstab): apply writes it from crypttab,"
                f" which needs root to read: on the host, {env.prog} {verb} --root",
            )
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
        file_items.append(
            PlanItem(
                describe_state(c_state), f"crypttab: {spec.name} line", state_note(c_state, spec.name, env.prog, verb)
            )
        )
        file_items.append(
            PlanItem(
                describe_state(f_state),
                f"fstab: {spec.mapper_path} line",
                state_note(f_state, spec.name, env.prog, verb),
            )
        )
        tier_change = any(st in (OwnedState.MISSING, OwnedState.CHANGE) for st in (c_state, f_state))
        return file_items, tier_change

    def _partition_items(self, spec: SwapHardwareSpec, swaps_text: str, *, tier_change: bool) -> list[PlanItem]:
        """The rows of the partition and of swap0 (swaps_text: /proc/swaps)."""
        runtime_items: list[PlanItem] = []
        part = self._partition_size_sysfs(spec.device_path)
        if part is not None:
            dev, size = part
            if size == spec.expected_size_bytes:
                runtime_items.append(
                    PlanItem("ok", f"partition {spec.partuuid[:8]}...", f"{dev}, {size} bytes as expected")
                )
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

        real_swap0 = self._fs.readlink(spec.mapper_path, canonicalize=True) or spec.mapper_path
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

        for src, dest in (
            (env.src_tmpfiles, env.tmpfiles_conf),
            (env.src_sysctl, env.sysctl_conf),
            (env.src_dropin, env.slice_dropin),
        ):
            st = self._inspect_file(src, dest)
            file_items.append(
                PlanItem(describe_state(st), _short_etc_path(env.etc_dir, dest), state_note(st, name, env.prog, verb))
            )

        if self._fs.exists(env.old_sysctl_conf):
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

        if self._zram_masked():
            file_items.append(
                PlanItem(
                    "ok",
                    _short_etc_path(env.etc_dir, env.zram_conf),
                    "masked (-> /dev/null): no zram devices at boot",
                )
            )
        else:
            file_items.append(
                PlanItem(
                    "mask",
                    _short_etc_path(env.etc_dir, env.zram_conf),
                    "not masked: zram-generator would create zram0 at boot",
                )
            )

        swaps_text = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
        runtime_items = [] if spec is None else self._partition_items(spec, swaps_text, tier_change=tier_change)

        zram_used = find_swap_used_kib(swaps_text, "/dev/zram0")
        if zram_used is None:
            runtime_items.append(PlanItem("ok", "zram0", "not active as swap"))
        else:
            meminfo = parse_meminfo(self._fs.read_text(posixpath.join(env.proc_dir, "meminfo"), "") or "")
            avail_kib = meminfo.get("MemAvailable", 0) // 1024
            if can_evict_zram(zram_used, avail_kib):
                runtime_items.append(
                    PlanItem("swapoff", "zram0", "active; free RAM can take its contents: apply swaps it off")
                )
            else:
                runtime_items.append(
                    PlanItem("later", "zram0", "active; not enough free RAM to swap it off now: gone after a reboot")
                )

        return ConfigSectionReport(
            etc_visible=True,
            etc_dir=env.etc_dir,
            prog=env.prog,
            verb=verb,
            file_items=tuple(file_items),
            runtime_items=tuple(runtime_items),
        )

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
        for key, want in parse_sysctl(self._desired_text(self._env.src_sysctl)):
            live = self._read_first(posixpath.join(self._env.proc_dir, "sys", key.replace(".", "/")), "?")
            state = SettingState.OK if live == want else SettingState.DIFF
            if state == SettingState.DIFF and key == exp_key and exp_on:
                state = SettingState.EXP
            rows.append(SettingCheckRow(state, key, live, want))
        row, source = self._memory_low_row(parse_memory_low(self._desired_text(self._env.src_dropin)))
        rows.append(row)
        return SettingsReport(
            rows=tuple(rows), exp_name=exp_name, exp_key=exp_key, override_source=source, cst_dropin=DROPIN_ENTRY.dest
        )

    def _memory_low_row(self, want_low: int | None) -> tuple[SettingCheckRow, str]:
        """machine.slice's floor: "over" (and the drop-in that sets it) when systemd takes MemoryLow from a
        drop-in after CST's; else "-" when systemd says the slice is not active (a cgroup folder left
        behind is not the slice's) or the cgroup has no memory.low (an active slice's row says so: no
        memory controller for it); else the cgroup's memory.low against CST's value (DIFF when it differs)."""
        shown = self._systemd.show(UnitName("machine.slice"), ("MemoryLow", "DropInPaths", "ActiveState"))
        effective = shown.get("MemoryLow", "")
        dropins = [(p, self._fs.read_text(self._env.host_view(p))) for p in shown.get("DropInPaths", "").split()]
        source = memory_low_override(effective, dropins, DROPIN_ENTRY.dest, want_low) if effective else None
        if source is not None:
            return SettingCheckRow(
                SettingState.OVER, "machine.slice MemoryLow", human_size(effective), human(want_low)
            ), source
        active_state = shown.get("ActiveState", "")
        live_low = self._fs.read_first_line(posixpath.join(self._env.cgroup_dir, "machine.slice", "memory.low"))
        if slice_not_active(active_state) or (live_low is None and not active_state):
            return SettingCheckRow(
                SettingState.INACTIVE, "machine.slice memory.low", "slice not active (no VM running)", human(want_low)
            ), ""
        if live_low is None:
            # systemd says the slice is active, and its cgroup has no memory.low: no memory controller for it.
            return SettingCheckRow(
                SettingState.INACTIVE,
                "machine.slice memory.low",
                "slice active, no memory.low (memory controller off)",
                human(want_low),
            ), ""
        ok = live_low.isdigit() and int(live_low) == want_low
        live = human(int(live_low)) if live_low.isdigit() else live_low
        return SettingCheckRow(
            SettingState.OK if ok else SettingState.DIFF, "machine.slice memory.low", live, human(want_low)
        ), ""

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
            if name == "pool_total_size" and val.isdigit():
                val = f"{val} ({human(int(val))})"
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
        out.extend((name, posixpath.join(cg, name)) for name in ("system.slice", "machine.slice", "builds.slice"))
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
            cur = self._fs.read_first_line(posixpath.join(path, "memory.swap.current"))
            if cur is None and path == self._env.cgroup_dir and "SwapTotal" in meminfo:
                cur = str(meminfo["SwapTotal"] - meminfo.get("SwapFree", 0))
            row = cgroup_row(stat, int(cur) if cur is not None and cur.isdigit() else None)
            wb = self._read_first(posixpath.join(path, "memory.zswap.writeback"), "-")
            low = self._read_first(posixpath.join(path, "memory.low"), "-")
            low_disp = human(int(low)) if low.isdigit() and int(low) else ("0" if low == "0" else low)
            psi = psi_value(self._fs.read_text(posixpath.join(path, "memory.pressure"), "") or "") or "-"
            rows.append(CgroupSwapRow(label, row, wb, low_disp, psi))
        return tuple(rows)

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
        )
