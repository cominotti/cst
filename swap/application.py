"""Pure use-case orchestration for the swap module (no direct OS I/O or terminal formatting)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import posixpath
import re

from cst_core.domain.files import ManagedState, MarkedLineSpec, inspect_managed_file
from cst_core.domain.install import InstallEntry, manifest_by_src
from cst_core.domain.plan import PlanItem
from cst_core.ports import CommandResult, FileSystemPort, SystemdPort
from swap.domain import (
    WSF_EXPERIMENT,
    ApplyEvent,
    CgroupSwapRow,
    ConfigSectionReport,
    DebugCounterRow,
    DebugSectionReport,
    SettingCheckRow,
    SettingsReport,
    SwapApplyResult,
    SwapDeviceRow,
    SwapHardwareSpec,
    SwapPlanReport,
    SwapStatusReport,
    can_evict_zram,
    cgroup_row,
    crypto_has_algorithm,
    describe_state,
    find_swap_used_kib,
    human,
    is_safe_partition_type,
    killed_text,
    parse_kv,
    parse_meminfo,
    parse_memory_low,
    parse_proc_swaps,
    parse_sysctl,
    parse_tmpfiles_params,
    psi_value,
    same_param,
    state_note,
    zswap_summary,
)
from swap.ports import SwapSystemPort


# The files swap installs: where they go and with which mode comes from the
# install manifest (cst_core/domain/install.py), the one list of installed files.
_MANIFEST = manifest_by_src()
TMPFILES_ENTRY: InstallEntry = _MANIFEST["swap/tmpfiles/zswap.conf"]
SYSCTL_ENTRY: InstallEntry = _MANIFEST["swap/sysctl/99-swap-tuning.conf"]
DROPIN_ENTRY: InstallEntry = _MANIFEST["swap/units/machine.slice.d-50-memory-low.conf"]


def _under(base: str, manifest_path: str, prefix: str) -> str:
    """manifest_path (which starts with prefix) moved under base."""
    return posixpath.join(base, manifest_path[len(prefix) :])


class _EventLog(list[ApplyEvent]):
    """apply's events in order; each also goes to emit as it happens, so the CLI prints every
    step before the next one runs (as swap-setup.sh's echo lines did)."""

    def __init__(self, emit: Callable[[ApplyEvent], None] | None) -> None:
        super().__init__()
        self._emit = emit

    def append(self, event: ApplyEvent, /) -> None:
        super().append(event)
        if self._emit is not None:
            self._emit(event)


def _show_output(res: CommandResult, events: list[ApplyEvent], prog: str) -> None:
    """What a command printed, passed on as it is (bash let it reach the terminal),
    then, for a command a signal killed, the line that says so (bash's "Terminated")."""
    if res.stdout:
        events.append(ApplyEvent("out", "raw", res.stdout))
    if res.stderr:
        events.append(ApplyEvent("err", "raw", res.stderr))
    if res.returncode < 0:
        events.append(ApplyEvent("err", "error", killed_text(prog, res.argv, res.returncode)))


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
    spec: SwapHardwareSpec = SwapHardwareSpec()

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
    rel = path[len(prefix) :] if path.startswith(prefix) else path
    if rel.startswith("systemd/system/"):
        rel = rel[len("systemd/system/") :]
    return rel


class SwapApplicationService:
    """Coordinates plan, status, and apply use cases for swap, zswap, and MemoryLow."""

    def __init__(
        self,
        env: SwapEnvironment,
        fs: FileSystemPort,
        systemd: SystemdPort,
        system: SwapSystemPort,
    ) -> None:
        self._env = env
        self._fs = fs
        self._systemd = systemd
        self._system = system

    @property
    def prog(self) -> str:
        """The command name in messages (`cst swap`)."""
        return self._env.prog

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
            return "unreadable"
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

    def _zram_masked(self) -> bool:
        return self._fs.is_symlink(self._env.zram_conf) and self._fs.readlink(self._env.zram_conf) == "/dev/null"

    def _partition_size_sysfs(self) -> tuple[str, int] | None:
        dev = self._fs.readlink(self._env.spec.device_path, canonicalize=True)
        if dev is None or not self._fs.is_block_device(dev):
            return None
        sectors = self._fs.read_first_line(
            posixpath.join(self._env.sys_dir, "class", "block", posixpath.basename(dev), "size")
        )
        if sectors is None or not sectors.isdigit():
            return None
        return dev, int(sectors) * 512

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

    def build_config_report(self, verb: str) -> ConfigSectionReport:
        """Inspect managed config files and runtime swap/zram states."""
        env = self._env
        spec = env.spec
        if not self._fs.is_dir(env.etc_dir):
            return ConfigSectionReport(
                etc_visible=False,
                etc_dir=env.etc_dir,
                prog=env.prog,
                verb=verb,
                file_items=(),
                runtime_items=(),
            )

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
        tier_change = c_state in ("missing", "change") or f_state in ("missing", "change")

        for src, dest in (
            (env.src_tmpfiles, env.tmpfiles_conf),
            (env.src_sysctl, env.sysctl_conf),
            (env.src_dropin, env.slice_dropin),
        ):
            st = self._inspect_file(src, dest)
            file_items.append(
                PlanItem(
                    describe_state(st), _short_etc_path(env.etc_dir, dest), state_note(st, spec.name, env.prog, verb)
                )
            )

        if self._fs.exists(env.old_sysctl_conf):
            file_items.append(
                PlanItem("remove", _short_etc_path(env.etc_dir, env.old_sysctl_conf), "legacy file of the zram setup")
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

        runtime_items: list[PlanItem] = []
        part = self._partition_size_sysfs()
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

        swaps_text = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
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
            rows.append(SettingCheckRow("BAD", "zswap", "not in this kernel (no /sys/module/zswap)", ""))
        for name, want in parse_tmpfiles_params(self._desired_text(self._env.src_tmpfiles)):
            live = params.get(name)
            if live is not None:
                rows.append(SettingCheckRow("ok" if same_param(live, want) else "DIFF", f"zswap {name}", live, want))
            elif params:
                # tmpfiles' w skips a file that is not there, without a word (renamed or removed by a kernel).
                rows.append(SettingCheckRow("MISSING", f"zswap {name}", "this kernel has no such parameter", want))
        exp_name, exp_key = WSF_EXPERIMENT
        exp_on = self._fs.exists(posixpath.join(self._env.cpu_run_dir, "exp-" + exp_name))
        for key, want in parse_sysctl(self._desired_text(self._env.src_sysctl)):
            live = self._read_first(posixpath.join(self._env.proc_dir, "sys", key.replace(".", "/")), "?")
            state = "ok" if live == want else "DIFF"
            if state == "DIFF" and key == exp_key and exp_on:
                state = "exp"
            rows.append(SettingCheckRow(state, key, live, want))
        want_low = parse_memory_low(self._desired_text(self._env.src_dropin))
        live_low = self._fs.read_first_line(posixpath.join(self._env.cgroup_dir, "machine.slice", "memory.low"))
        if live_low is None:
            rows.append(
                SettingCheckRow("-", "machine.slice memory.low", "slice not active (no VM running)", human(want_low))
            )
        else:
            ok = live_low.isdigit() and int(live_low) == want_low
            rows.append(
                SettingCheckRow(
                    "ok" if ok else "DIFF",
                    "machine.slice memory.low",
                    human(int(live_low)) if live_low.isdigit() else live_low,
                    human(want_low),
                )
            )
        return SettingsReport(rows=tuple(rows), exp_name=exp_name, exp_key=exp_key)

    def _build_debug_report(self, hint: str) -> DebugSectionReport:
        try:
            names = self._fs.list_dir(self._env.zswap_debug_dir)
        except PermissionError:
            return DebugSectionReport(self._env.zswap_debug_dir, "permission_denied", hint)
        except OSError:
            return DebugSectionReport(self._env.zswap_debug_dir, "unavailable", hint)
        from swap.domain import DEBUG_HELP

        rows: list[DebugCounterRow] = []
        for name in names:
            val = self._read_first(posixpath.join(self._env.zswap_debug_dir, name), "?")
            if name == "pool_total_size" and val.isdigit():
                val = f"{val} ({human(int(val))})"
            rows.append(DebugCounterRow(name, val, DEBUG_HELP.get(name, "")))
        return DebugSectionReport(self._env.zswap_debug_dir, "ok", hint, tuple(rows))

    def _build_devices(self) -> tuple[SwapDeviceRow, ...]:
        swaps_text = self._fs.read_text(posixpath.join(self._env.proc_dir, "swaps"), "") or ""
        out: list[SwapDeviceRow] = []
        for entry in parse_proc_swaps(swaps_text):
            name = self._fs.read_first_line(
                posixpath.join(self._env.sys_dir, "class", "block", posixpath.basename(entry.device), "dm", "name")
            )
            label = f"{entry.device} ({name})" if name else entry.device
            out.append(
                SwapDeviceRow(label, entry.swap_type, entry.used_kib * 1024, entry.size_kib * 1024, entry.priority)
            )
        return tuple(out)

    def _cgroups(self) -> list[tuple[str, str]]:
        cg = self._env.cgroup_dir
        out = [("(whole system)", cg)]
        for name in ("system.slice", "machine.slice", "builds.slice"):
            out.append((name, posixpath.join(cg, name)))
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
            row = cgroup_row(stat, int(cur) if cur and cur.isdigit() else None)
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

    def _check_preconditions(self, events: list[ApplyEvent]) -> str | None:
        env = self._env
        spec = env.spec

        def fail(msg: str) -> None:
            events.append(ApplyEvent("err", "error", f"{env.prog}: ERROR: {msg}"))

        if not self._fs.is_dir(env.zswap_params_dir):
            fail(f"zswap is not built into this kernel (no {env.zswap_params_dir}).")
            return None
        tmpfiles_text = self._desired_text(env.src_tmpfiles)
        comp = next((val for name, val in parse_tmpfiles_params(tmpfiles_text) if name == "compressor"), "")
        if not comp:
            fail(f"no compressor line in {env.src_tmpfiles}.")
            return None
        self._system.modprobe(comp)
        crypto_text = self._fs.read_text(posixpath.join(env.proc_dir, "crypto"), "") or ""
        if not crypto_has_algorithm(crypto_text, comp):
            fail(f"Compressor '{comp}' is not available in /proc/crypto.")
            return None
        if not self._fs.is_block_device(spec.device_path):
            fail(f"Swap partition {spec.device_path} not found.")
            return None
        actual = self._system.blockdev_size(spec.device_path)
        if actual != str(spec.expected_size_bytes):
            fail(
                f"{spec.device_path} is {actual} bytes, expected {spec.expected_size_bytes}: "
                "the PARTUUID points at an unexpected device; refusing."
            )
            return None
        if not self._fs.is_block_device(spec.mapper_path):
            part_type = self._system.blkid_type(spec.device_path)
            if not is_safe_partition_type(part_type):
                fail(f"{spec.device_path} has a '{part_type}' signature: refusing to overwrite it.")
                return None
        return tmpfiles_text

    def _apply_marked_line(
        self,
        path: str,
        line_spec: MarkedLineSpec,
        mode: int,
        events: list[ApplyEvent],
    ) -> tuple[bool, bool, bool]:
        """Apply one marked line. Returns (ok, line_changed, line_written)."""
        name = self._env.spec.name
        prog = self._env.prog
        st = self._inspect_marked(path, line_spec)
        if st == "ok":
            events.append(ApplyEvent("out", "line", f"unchanged: {path} ({name} entry)"))
            return True, False, False
        if st == "conflict":
            events.append(
                ApplyEvent(
                    "err",
                    "error",
                    f"{prog}: ERROR: {path} has an unmarked entry for {line_spec.key}: "
                    "remove it by hand, then run apply again.",
                )
            )
            return False, False, False
        if st not in ("adopt", "duplicate", "missing", "change"):
            events.append(ApplyEvent("err", "error", f"{prog}: ERROR: {path}: {st}"))
            return False, False, False

        if not self._fs.exists(path):
            self._fs.write_atomic(path, "", mode)
        self._fs.copy_if_absent(path, f"{path}.pre-swap-config.bak")
        existing = self._fs.read_text(path, "") or ""
        self._fs.write_atomic(path, line_spec.render_updated(existing), mode)

        if st == "adopt":
            events.append(
                ApplyEvent("out", "line", f"adopted:   {path} ({name} entry: marker renewed, entry unchanged)")
            )
            return True, False, True
        if st == "duplicate":
            events.append(
                ApplyEvent(
                    "out", "line", f"deduped:   {path} ({name} entry was there twice: one kept, entry unchanged)"
                )
            )
            return True, False, True
        events.append(ApplyEvent("out", "line", f"written:   {path} ({name} entry)"))
        return True, True, True

    def _apply_managed_file(self, src: str, dest: str, mode: int, events: list[ApplyEvent]) -> bool:
        """Ensure dest matches src atomically. Returns True if dest was written."""
        st = self._inspect_file(src, dest)
        if st == "ok":
            events.append(ApplyEvent("out", "line", f"unchanged: {dest}"))
            return False
        desired = self._desired_text(src)
        self._fs.write_atomic(dest, desired, mode)
        if st == "adopt":
            events.append(ApplyEvent("out", "line", f"adopted:   {dest} (comments renewed, settings unchanged)"))
        else:
            events.append(ApplyEvent("out", "line", f"written:   {dest}"))
        return True

    def _stopped(self, returncode: int, events: list[ApplyEvent]) -> SwapApplyResult:
        """The result of an apply that stopped before the verification."""
        return SwapApplyResult(returncode, tuple(events), None, (), "", "", self._env.prog)

    def _run_units(
        self, steps: tuple[tuple[Callable[[str], CommandResult], str], ...], events: list[ApplyEvent]
    ) -> int:
        """`systemctl VERB UNIT` for each (systemd call, unit) step, showing its output; the first failing status, else 0.

        Under `set -e` swap-setup.sh stopped at a failing systemctl with its
        exit status, and its message was the error the user saw.
        """
        for call, unit in steps:
            res = call(unit)
            _show_output(res, events, self._env.prog)
            if res.returncode != 0:
                return res.returncode
        return 0

    def apply_changes(self, emit: Callable[[ApplyEvent], None] | None = None) -> SwapApplyResult:
        """Idempotently apply swap, zswap, zram-off, sysctl, and MemoryLow config.

        emit receives each event as it happens (the CLI prints it at once);
        the result holds them all too.
        """
        env = self._env
        spec = env.spec
        events: list[ApplyEvent] = _EventLog(emit)
        tmpfiles_text = self._check_preconditions(events)
        if tmpfiles_text is None:
            return self._stopped(1, events)

        events.append(ApplyEvent("out", "section", f"== {spec.name}: crypttab and fstab"))
        c_spec = spec.crypttab_spec()
        f_spec = spec.fstab_spec()
        if self._inspect_marked(env.crypttab, c_spec) == "conflict":
            events.append(
                ApplyEvent(
                    "err",
                    "error",
                    f"{env.prog}: ERROR: {env.crypttab} has an unmarked entry for {spec.name}: "
                    "remove it by hand, then run apply again.",
                )
            )
            return self._stopped(1, events)
        if self._inspect_marked(env.fstab, f_spec) == "conflict":
            events.append(
                ApplyEvent(
                    "err",
                    "error",
                    f"{env.prog}: ERROR: {env.fstab} has an unmarked entry for {spec.mapper_path}: "
                    "remove it by hand, then run apply again.",
                )
            )
            return self._stopped(1, events)

        ok_c, changed_c, written_c = self._apply_marked_line(env.crypttab, c_spec, 0o600, events)
        if not ok_c:
            return self._stopped(1, events)
        ok_f, changed_f, written_f = self._apply_marked_line(env.fstab, f_spec, 0o644, events)
        if not ok_f:
            return self._stopped(1, events)
        line_changed = changed_c or changed_f
        line_written = written_c or written_f
        if line_written:
            self._systemd.daemon_reload()

        swap_unit = self._systemd.escape_path(spec.mapper_path, suffix="swap")
        swaps_text = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
        real_swap0 = self._fs.readlink(spec.mapper_path, canonicalize=True) or spec.mapper_path
        swap0_used = find_swap_used_kib(swaps_text, real_swap0)
        crypt_unit = f"systemd-cryptsetup@{spec.name}.service"
        if swap0_used is not None:
            if line_changed:
                if swap0_used == 0:
                    events.append(
                        ApplyEvent(
                            "out", "line", f"{spec.mapper_path}: entry changed and the device is empty: re-activating"
                        )
                    )
                    sd = self._systemd
                    rc = self._run_units(
                        ((sd.stop, swap_unit), (sd.restart, crypt_unit), (sd.start, swap_unit)), events
                    )
                    if rc != 0:
                        return self._stopped(rc, events)
                else:
                    events.append(
                        ApplyEvent(
                            "err",
                            "warn",
                            f"WARNING: {spec.mapper_path}: entry changed but {swap0_used // 1024} MiB are in use; "
                            "the live device keeps the old options until reboot.",
                        )
                    )
            else:
                events.append(ApplyEvent("out", "line", f"{spec.mapper_path} is active as swap."))
        else:
            events.append(
                ApplyEvent("out", "line", f"{spec.mapper_path}: activating (dm-crypt open + mkswap + swapon)")
            )
            rc = self._run_units(((self._systemd.start, crypt_unit), (self._systemd.start, swap_unit)), events)
            if rc != 0:
                return self._stopped(rc, events)

        events.append(ApplyEvent("out", "section", "== zswap"))
        self._apply_managed_file(env.src_tmpfiles, env.tmpfiles_conf, TMPFILES_ENTRY.mode.bits, events)
        for name, val in parse_tmpfiles_params(tmpfiles_text):
            param_path = posixpath.join(env.zswap_params_dir, name)
            live = self._read_first(param_path, "")
            if not same_param(live, val):
                self._fs.write_text_direct(param_path, f"{val}\n")
                events.append(ApplyEvent("out", "line", f"set:       zswap {name} = {val} (was {live})"))

        events.append(ApplyEvent("out", "section", "== zram"))
        if self._zram_masked():
            events.append(ApplyEvent("out", "line", f"unchanged: {env.zram_conf} (masked)"))
        else:
            self._fs.force_symlink("/dev/null", env.zram_conf)
            events.append(
                ApplyEvent("out", "line", f"masked:    {env.zram_conf} -> /dev/null (no zram devices at boot)")
            )
            self._systemd.daemon_reload()

        swaps_after_zswap = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
        zram_used = find_swap_used_kib(swaps_after_zswap, "/dev/zram0")
        if zram_used is not None:
            meminfo = parse_meminfo(self._fs.read_text(posixpath.join(env.proc_dir, "meminfo"), "") or "")
            avail_kib = meminfo.get("MemAvailable", 0) // 1024
            if can_evict_zram(zram_used, avail_kib):
                events.append(ApplyEvent("out", "line", "zram0: swapping off"))
                swapoff = self._systemd.stop("dev-zram0.swap")
                _show_output(swapoff, events, self._env.prog)
                if swapoff.returncode == 0:
                    # Silent, as `systemctl stop ... 2>/dev/null || true` was: the reset below is the fallback.
                    self._systemd.stop("systemd-zram-setup@zram0.service")
                    self._system.reset_zram0()
                    events.append(ApplyEvent("out", "line", "zram0: removed"))
                else:
                    events.append(
                        ApplyEvent(
                            "err",
                            "warn",
                            "WARNING: swapoff of zram0 failed; it is disabled in the config and gone after a reboot.",
                        )
                    )
            else:
                events.append(
                    ApplyEvent(
                        "err",
                        "warn",
                        "WARNING: not enough free RAM to swap off zram0 now; it is gone after a reboot "
                        "(or run apply again under lower pressure).",
                    )
                )

        events.append(ApplyEvent("out", "section", "== sysctl"))
        if self._fs.exists(env.old_sysctl_conf):
            self._fs.remove_file(env.old_sysctl_conf)
            events.append(ApplyEvent("out", "line", f"removed:   {env.old_sysctl_conf} (legacy)"))
        self._apply_managed_file(env.src_sysctl, env.sysctl_conf, SYSCTL_ENTRY.mode.bits, events)
        exp_name, exp_key = WSF_EXPERIMENT
        if self._fs.exists(posixpath.join(env.cpu_run_dir, "exp-" + exp_name)):
            live_wsf = self._read_first(posixpath.join(env.proc_dir, "sys", "vm", "watermark_scale_factor"), "?")
            events.append(
                ApplyEvent("out", "line", f"kept:      {exp_key} = {live_wsf} (the cpu experiment {exp_name} is on;")
            )
            events.append(
                ApplyEvent(
                    "out", "line", "           after 'cst cpu experiment off wsf-300', run apply again, or reboot)"
                )
            )
            sysctl = self._system.apply_sysctl_file(env.sysctl_conf, exclude_key=exp_key)
        else:
            sysctl = self._system.apply_sysctl_file(env.sysctl_conf)
        _show_output(sysctl, events, self._env.prog)
        if sysctl.returncode != 0:
            return self._stopped(sysctl.returncode, events)

        events.append(ApplyEvent("out", "section", "== machine.slice"))
        if self._apply_managed_file(env.src_dropin, env.slice_dropin, DROPIN_ENTRY.mode.bits, events):
            self._systemd.daemon_reload()

        settings = self.build_settings_report(self.zswap_params())
        fail = settings.has_diff
        swaps_final = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
        real_swap0_final = self._fs.readlink(spec.mapper_path, canonicalize=True) or spec.mapper_path
        active_swap0 = find_swap_used_kib(swaps_final, real_swap0_final) is not None
        active_zram0 = find_swap_used_kib(swaps_final, "/dev/zram0") is not None
        if not active_swap0:
            fail = True
        swapon = self._system.swapon_show()
        verify_notes: list[str] = []
        if not active_swap0:
            verify_notes.append(f"  BAD   {spec.name} is not active as swap")
        if active_zram0:
            verify_notes.append(
                "  later zram0 still active as swap (gone after a reboot, or apply again under lower pressure)"
            )
        verify_error = f"{env.prog}: ERROR: one or more settings did not apply as expected (see above)." if fail else ""
        return SwapApplyResult(
            1 if fail else 0,
            tuple(events),
            settings,
            tuple(verify_notes),
            swapon.stdout,
            verify_error,
            env.prog,
            swapon.stderr,
        )
