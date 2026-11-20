# SPDX-License-Identifier: MPL-2.0
"""The apply use case of swap: what changes the machine (no direct OS I/O or terminal formatting)."""

from collections.abc import Callable
import posixpath
from typing import override

from cst_core.application.install import Placement
from cst_core.domain.files import MarkedLineSpec, OwnedState
from cst_core.domain.install import InstallEntry
from cst_core.ports import CommandResult, UnitName
from swap.application.reports import (
    DROPIN_ENTRY,
    OLD_DROPIN_ENTRY,
    SYSCTL_ENTRY,
    TMPFILES_ENTRY,
    SwapReports,
)
from swap.domain import (
    SETTING_STATE_WIDTH,
    WSF_EXPERIMENT,
    ApplyEvent,
    ApplyEventKind,
    SettingState,
    SwapApplyResult,
    SwapHardwareSpec,
    SwapSpecState,
    crypto_has_algorithm,
    find_swap_used_kib,
    is_safe_partition_type,
    killed_text,
    parse_tmpfiles_params,
    render_swap_settings,
    same_param,
)


# swap0's marked lines, as apply checks and writes them: (file, line, the mode a new file gets).
type _SwapLines = tuple[tuple[str, MarkedLineSpec, int], ...]


class _EventLog(list[ApplyEvent]):
    """apply's events in order; each also goes to emit as it happens, so the CLI prints every
    step before the next one runs (as swap-setup.sh's echo lines did)."""

    def __init__(self, emit: Callable[[ApplyEvent], None] | None) -> None:
        super().__init__()
        self._emit = emit

    @override
    def append(self, event: ApplyEvent, /) -> None:
        super().append(event)
        if self._emit is not None:
            self._emit(event)


def _show_output(res: CommandResult, events: list[ApplyEvent], prog: str) -> None:
    """What a command printed, passed on as it is (bash let it reach the terminal),
    then, for a command a signal killed, the line that says so (bash's "Terminated")."""
    if res.stdout:
        events.append(ApplyEvent("out", ApplyEventKind.RAW, res.stdout))
    if res.stderr:
        events.append(ApplyEvent("err", ApplyEventKind.RAW, res.stderr))
    if res.returncode < 0:
        events.append(ApplyEvent("err", ApplyEventKind.ERROR, killed_text(prog, res.argv, res.returncode)))


class SwapApplicationService(SwapReports):
    """Coordinates plan, status, and apply use cases for swap, zswap, and MemoryLow."""

    def _check_preconditions(self, spec: SwapHardwareSpec, events: list[ApplyEvent]) -> str | None:
        env = self._env

        def fail(msg: str) -> None:
            events.append(ApplyEvent("err", ApplyEventKind.ERROR, f"{env.prog}: ERROR: {msg}"))

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
        *,
        name: str,
    ) -> tuple[bool, bool, bool]:
        """Apply one marked line (name: the swap device's). Returns (ok, line_changed, line_written)."""
        prog = self._env.prog
        st = self._inspect_marked(path, line_spec)
        if st == OwnedState.OK:
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"unchanged: {path} ({name} entry)"))
            return True, False, False
        if st == OwnedState.CONFLICT:
            events.append(
                ApplyEvent(
                    "err",
                    ApplyEventKind.ERROR,
                    f"{prog}: ERROR: {path} has an unmarked entry for {line_spec.key}: "
                    "remove it by hand, then run apply again.",
                )
            )
            return False, False, False
        if st not in (OwnedState.ADOPT, OwnedState.DUPLICATE, OwnedState.MISSING, OwnedState.CHANGE):
            events.append(ApplyEvent("err", ApplyEventKind.ERROR, f"{prog}: ERROR: {path}: {st}"))
            return False, False, False

        if not self._fs.exists(path):
            self._fs.write_atomic(path, "", mode)
        self._fs.copy_if_absent(path, f"{path}.pre-swap-config.bak")
        existing = self._fs.read_text(path, "") or ""
        self._fs.write_atomic(path, line_spec.render_updated(existing), mode)

        if st == OwnedState.ADOPT:
            events.append(
                ApplyEvent(
                    "out", ApplyEventKind.LINE, f"adopted:   {path} ({name} entry: marker renewed, entry unchanged)"
                )
            )
            return True, False, True
        if st == OwnedState.DUPLICATE:
            events.append(
                ApplyEvent(
                    "out",
                    ApplyEventKind.LINE,
                    f"deduped:   {path} ({name} entry was there twice: one kept, entry unchanged)",
                )
            )
            return True, False, True
        events.append(ApplyEvent("out", ApplyEventKind.LINE, f"written:   {path} ({name} entry)"))
        return True, True, True

    def _placement(self, entry: InstallEntry, path: str) -> Placement:
        """entry's file at path (under this run's /etc), listed in this run's install record."""
        return Placement.of(entry, path=path, dest=entry.dest, record=self._env.install_record)

    def _apply_managed_file(self, entry: InstallEntry, src: str, dest: str, events: list[ApplyEvent]) -> bool:
        """Ensure dest matches src, through the installer (atomic; listed in the install record, also when
        it was already current). Returns True if dest was written."""
        st = self._inspect_file(src, dest)
        place = self._placement(entry, dest)
        if not self._installer.put(place, self._desired_text(src).encode("utf-8"), as_root=self._env.as_root):
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"unchanged: {dest}"))
            return False
        if st == OwnedState.ADOPT:
            events.append(
                ApplyEvent("out", ApplyEventKind.LINE, f"adopted:   {dest} (comments renewed, settings unchanged)")
            )
        else:
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"written:   {dest}"))
        return True

    def _write_settings(self, spec: SwapHardwareSpec, events: list[ApplyEvent]) -> None:
        """/etc/cst/swap.conf from what an older CST set up (resolve_spec's "write")."""
        origin = f"from the {spec.name} line an older CST wrote in /etc/crypttab and the partition's size then"
        self._fs.write_atomic(self._env.settings_path, render_swap_settings(spec, origin), 0o644)
        events.append(ApplyEvent("out", ApplyEventKind.SECTION, "== settings"))
        events.append(
            ApplyEvent(
                "out",
                ApplyEventKind.LINE,
                f"written:   {self._env.settings_path} (SWAP_PARTUUID={spec.partuuid},"
                f" SWAP_SIZE_BYTES={spec.expected_size_bytes}: {origin})",
            )
        )

    def _remove_old_dropin(self, events: list[ApplyEvent]) -> bool:
        """Remove the drop-in under its old name (and its line in the install record) when CST wrote it.
        True when it was removed."""
        if not self.old_dropin_is_ours():
            # Gone, or someone else's now: CST no longer installs that name, so its record line goes
            # (doctor would ask for an apply that never puts it back); the file is left alone.
            self._installer.forget(self._env.install_record, [OLD_DROPIN_ENTRY.dest])
            return False
        for line in self._installer.remove([self._placement(OLD_DROPIN_ENTRY, self._env.old_slice_dropin)]):
            events.append(
                ApplyEvent(
                    "out", ApplyEventKind.LINE, f"{line} (the old name of {posixpath.basename(DROPIN_ENTRY.dest)})"
                )
            )
        return True

    def _stopped(self, returncode: int, events: list[ApplyEvent]) -> SwapApplyResult:
        """The result of an apply that stopped before the verification."""
        return SwapApplyResult(returncode, tuple(events), None, (), "", "", self._env.prog)

    def _run_units(
        self, steps: tuple[tuple[Callable[[UnitName], CommandResult], UnitName], ...], events: list[ApplyEvent]
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
        events: list[ApplyEvent] = _EventLog(emit)
        res = self.resolve_spec()
        spec = res.spec
        if spec is None:
            note = self._settings_item(res, "plan").note
            events.append(ApplyEvent("err", ApplyEventKind.ERROR, f"{env.prog}: ERROR: {env.settings_path}: {note}."))
            return self._stopped(1, events)
        lines = self._swap_lines(spec)
        tmpfiles_text = self._check_before_writing(spec, lines, events)
        if tmpfiles_text is None:
            return self._stopped(1, events)
        if res.state == SwapSpecState.WRITE:
            self._write_settings(spec, events)
        rc = self._apply_swap0(spec, lines, events)
        if rc != 0:
            return self._stopped(rc, events)
        self._apply_zswap(tmpfiles_text, events)
        self._mask_zram(events)
        self._swapoff_zram(events)
        rc = self._apply_sysctl(events)
        if rc != 0:
            return self._stopped(rc, events)
        self._apply_slice(events)
        return self._verified(spec, events)

    def _swap_lines(self, spec: SwapHardwareSpec) -> _SwapLines:
        """swap0's marked lines, in the order apply checks and writes them: (file, line, the mode a
        new file gets). Built once, so the refusals before the first write check the lines written."""
        env = self._env
        return ((env.crypttab, spec.crypttab_spec(), 0o600), (env.fstab, spec.fstab_spec(), 0o644))

    def _check_before_writing(self, spec: SwapHardwareSpec, lines: _SwapLines, events: list[ApplyEvent]) -> str | None:
        """Every refusal, before the first write (the settings file too): the preconditions, and an
        unmarked swap line among lines. The desired tmpfiles text, or None when apply stops."""
        env = self._env
        tmpfiles_text = self._check_preconditions(spec, events)
        if tmpfiles_text is None:
            return None
        for path, line_spec, _mode in lines:
            if self._inspect_marked(path, line_spec) == OwnedState.CONFLICT:
                events.append(
                    ApplyEvent(
                        "err",
                        ApplyEventKind.ERROR,
                        f"{env.prog}: ERROR: {path} has an unmarked entry for {line_spec.key}: "
                        "remove it by hand, then run apply again.",
                    )
                )
                return None
        return tmpfiles_text

    def _apply_swap0(self, spec: SwapHardwareSpec, lines: _SwapLines, events: list[ApplyEvent]) -> int:
        """The "crypttab and fstab" section: each marked line of lines (a daemon-reload when one was
        written), then swap0's activation. The status apply stops with: 1 when a line was refused,
        else _activate_swap's (0: go on)."""
        events.append(ApplyEvent("out", ApplyEventKind.SECTION, f"== {spec.name}: crypttab and fstab"))
        changed = written = False
        for path, line_spec, mode in lines:
            ok, line_changed, line_written = self._apply_marked_line(path, line_spec, mode, events, name=spec.name)
            if not ok:
                return 1
            changed = changed or line_changed
            written = written or line_written
        if written:
            self._systemd.daemon_reload()
        return self._activate_swap(spec, events, line_changed=changed)

    def _activate_swap(self, spec: SwapHardwareSpec, events: list[ApplyEvent], *, line_changed: bool) -> int:
        """swap0 on: started when it is not active; started again when its line changed and it is
        empty; left as it is otherwise (a line change then waits for a reboot). The status of the
        first systemctl call that failed, else 0."""
        sd = self._systemd
        swap_unit = sd.escape_path(spec.mapper_path, suffix="swap")
        swaps_text = self._fs.read_text(posixpath.join(self._env.proc_dir, "swaps"), "") or ""
        real_swap0 = self._fs.readlink(spec.mapper_path, canonicalize=True) or spec.mapper_path
        swap0_used = find_swap_used_kib(swaps_text, real_swap0)
        crypt_unit = UnitName(f"systemd-cryptsetup@{spec.name}.service")
        if swap0_used is None:
            events.append(
                ApplyEvent(
                    "out", ApplyEventKind.LINE, f"{spec.mapper_path}: activating (dm-crypt open + mkswap + swapon)"
                )
            )
            return self._run_units(((sd.start, crypt_unit), (sd.start, swap_unit)), events)
        if not line_changed:
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"{spec.mapper_path} is active as swap."))
            return 0
        if swap0_used != 0:
            events.append(
                ApplyEvent(
                    "err",
                    ApplyEventKind.WARN,
                    f"WARNING: {spec.mapper_path}: entry changed but {swap0_used // 1024} MiB are in use; "
                    "the live device keeps the old options until reboot.",
                )
            )
            return 0
        events.append(
            ApplyEvent(
                "out",
                ApplyEventKind.LINE,
                f"{spec.mapper_path}: entry changed and the device is empty: re-activating",
            )
        )
        return self._run_units(((sd.stop, swap_unit), (sd.restart, crypt_unit), (sd.start, swap_unit)), events)

    def _apply_zswap(self, tmpfiles_text: str, events: list[ApplyEvent]) -> None:
        """The "zswap" section: tmpfiles.d/zswap.conf for the next boot, and each parameter set live."""
        env = self._env
        events.append(ApplyEvent("out", ApplyEventKind.SECTION, "== zswap"))
        self._apply_managed_file(TMPFILES_ENTRY, env.src_tmpfiles, env.tmpfiles_conf, events)
        for name, val in parse_tmpfiles_params(tmpfiles_text):
            param_path = posixpath.join(env.zswap_params_dir, name)
            if not self._fs.exists(param_path):
                # MISSING in plan: tmpfiles skips it at boot, and sysfs cannot create it.
                events.append(
                    ApplyEvent(
                        "out", ApplyEventKind.LINE, f"skipped:   zswap {name} (this kernel has no such parameter)"
                    )
                )
                continue
            live = self._read_first(param_path, "")
            if not same_param(live, val):
                self._fs.write_text_direct(param_path, f"{val}\n")
                events.append(ApplyEvent("out", ApplyEventKind.LINE, f"set:       zswap {name} = {val} (was {live})"))

    def _mask_zram(self, events: list[ApplyEvent]) -> None:
        """The "zram" section: zram-generator.conf masked, so no zram device comes up at boot."""
        env = self._env
        events.append(ApplyEvent("out", ApplyEventKind.SECTION, "== zram"))
        if self._zram_masked():
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"unchanged: {env.zram_conf} (masked)"))
            return
        self._fs.force_symlink("/dev/null", env.zram_conf)
        events.append(
            ApplyEvent("out", ApplyEventKind.LINE, f"masked:    {env.zram_conf} -> /dev/null (no zram devices at boot)")
        )
        self._systemd.daemon_reload()

    def _swapoff_zram(self, events: list[ApplyEvent]) -> None:
        """zram0 swapped off now when it is active and free RAM can take its contents; a warning
        otherwise (it is gone after a reboot)."""
        env = self._env
        swaps_after_zswap = self._fs.read_text(posixpath.join(env.proc_dir, "swaps"), "") or ""
        zram_used = find_swap_used_kib(swaps_after_zswap, "/dev/zram0")
        if zram_used is None:
            return
        if not self._zram_evictable(zram_used):
            events.append(
                ApplyEvent(
                    "err",
                    ApplyEventKind.WARN,
                    "WARNING: not enough free RAM to swap off zram0 now; it is gone after a reboot "
                    "(or run apply again under lower pressure).",
                )
            )
            return
        events.append(ApplyEvent("out", ApplyEventKind.LINE, "zram0: swapping off"))
        swapoff = self._systemd.stop(UnitName("dev-zram0.swap"))
        _show_output(swapoff, events, self._env.prog)
        if swapoff.returncode == 0:
            # Silent, as `systemctl stop ... 2>/dev/null || true` was: the reset below is the fallback.
            self._systemd.stop(UnitName("systemd-zram-setup@zram0.service"))
            self._system.reset_zram0()
            events.append(ApplyEvent("out", ApplyEventKind.LINE, "zram0: removed"))
        else:
            events.append(
                ApplyEvent(
                    "err",
                    ApplyEventKind.WARN,
                    "WARNING: swapoff of zram0 failed; it is disabled in the config and gone after a reboot.",
                )
            )

    def _apply_sysctl(self, events: list[ApplyEvent]) -> int:
        """The "sysctl" section: the legacy zram file removed, 99-swap-tuning.conf written and applied
        live (without the key a cpu experiment holds). sysctl's exit status."""
        env = self._env
        events.append(ApplyEvent("out", ApplyEventKind.SECTION, "== sysctl"))
        if self._fs.exists(env.old_sysctl_conf):
            self._fs.remove_file(env.old_sysctl_conf)
            events.append(ApplyEvent("out", ApplyEventKind.LINE, f"removed:   {env.old_sysctl_conf} (legacy)"))
        self._apply_managed_file(SYSCTL_ENTRY, env.src_sysctl, env.sysctl_conf, events)
        exp_name, exp_key = WSF_EXPERIMENT
        if self._fs.exists(posixpath.join(env.cpu_run_dir, "exp-" + exp_name)):
            live_wsf = self._read_first(posixpath.join(env.proc_dir, "sys", "vm", "watermark_scale_factor"), "?")
            events.append(
                ApplyEvent(
                    "out",
                    ApplyEventKind.LINE,
                    f"kept:      {exp_key} = {live_wsf} (the cpu experiment {exp_name} is on;",
                )
            )
            events.append(
                ApplyEvent(
                    "out",
                    ApplyEventKind.LINE,
                    "           after 'cst cpu experiment off wsf-300', run apply again, or reboot)",
                )
            )
            sysctl = self._system.apply_sysctl_file(env.sysctl_conf, exclude_key=exp_key)
        else:
            sysctl = self._system.apply_sysctl_file(env.sysctl_conf)
        _show_output(sysctl, events, self._env.prog)
        return sysctl.returncode

    def _apply_slice(self, events: list[ApplyEvent]) -> None:
        """The "machine.slice" section: the MemoryLow drop-in (the old name removed), then the install
        record marks swap:core complete."""
        env = self._env
        events.append(ApplyEvent("out", ApplyEventKind.SECTION, "== machine.slice"))
        written = self._apply_managed_file(DROPIN_ENTRY, env.src_dropin, env.slice_dropin, events)
        if self._remove_old_dropin(events) or written:
            self._systemd.daemon_reload()
        # swap:core's files are all in place: the install record says the component is complete.
        self._installer.mark(env.install_record, DROPIN_ENTRY.component)

    def _verified(self, spec: SwapHardwareSpec, events: list[ApplyEvent]) -> SwapApplyResult:
        """The result of a whole apply: the settings as they are now, swap0 active, zram0 gone."""
        env = self._env
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
            verify_notes.append(f"  {SettingState.BAD:<{SETTING_STATE_WIDTH}} {spec.name} is not active as swap")
        if active_zram0:
            verify_notes.append(
                f"  {SettingState.LATER:<{SETTING_STATE_WIDTH}} zram0 still active as swap"
                " (gone after a reboot, or apply again under lower pressure)"
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
