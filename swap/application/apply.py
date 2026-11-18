# SPDX-License-Identifier: MPL-2.0
"""The apply use case of swap: what changes the machine (no direct OS I/O or terminal formatting)."""

from collections.abc import Callable
import posixpath

from cst_core.application.install import Placement
from cst_core.domain.files import MarkedLineSpec
from cst_core.domain.install import InstallEntry
from cst_core.ports import CommandResult, UnitName
from swap.application.reports import (
    DROPIN_ENTRY,
    OLD_DROPIN_DEST,
    OLD_DROPIN_SRC,
    SYSCTL_ENTRY,
    TMPFILES_ENTRY,
    SwapReports,
)
from swap.domain import (
    WSF_EXPERIMENT,
    ApplyEvent,
    SwapApplyResult,
    can_evict_zram,
    crypto_has_algorithm,
    find_swap_used_kib,
    is_safe_partition_type,
    killed_text,
    parse_meminfo,
    parse_tmpfiles_params,
    same_param,
)


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


class SwapApplicationService(SwapReports):
    """Coordinates plan, status, and apply use cases for swap, zswap, and MemoryLow."""

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

    def _apply_managed_file(self, entry: InstallEntry, src: str, dest: str, events: list[ApplyEvent]) -> bool:
        """Ensure dest matches src, through the installer (atomic; listed in the install record, also when
        it was already current). Returns True if dest was written."""
        st = self._inspect_file(src, dest)
        place = Placement(
            path=dest,
            dest=entry.dest,
            record=self._env.install_record,
            component=entry.component,
            src=entry.src,
            kind=entry.kind,
            owner=entry.owner,
            mode=entry.mode.bits,
        )
        if not self._installer.put(place, self._desired_text(src).encode("utf-8"), as_root=self._env.as_root):
            events.append(ApplyEvent("out", "line", f"unchanged: {dest}"))
            return False
        if st == "adopt":
            events.append(ApplyEvent("out", "line", f"adopted:   {dest} (comments renewed, settings unchanged)"))
        else:
            events.append(ApplyEvent("out", "line", f"written:   {dest}"))
        return True

    def _remove_old_dropin(self, events: list[ApplyEvent]) -> bool:
        """Remove the drop-in under its old name (and its line in the install record) when CST wrote it.
        True when it was removed."""
        if not self.old_dropin_is_ours():
            return False
        old = Placement(
            path=self._env.old_slice_dropin,
            dest=OLD_DROPIN_DEST,
            record=self._env.install_record,
            component=DROPIN_ENTRY.component,
            src=OLD_DROPIN_SRC,
            kind=DROPIN_ENTRY.kind,
            owner=DROPIN_ENTRY.owner,
            mode=DROPIN_ENTRY.mode.bits,
        )
        for line in self._installer.remove([old]):
            events.append(
                ApplyEvent("out", "line", f"{line} (the old name of {posixpath.basename(DROPIN_ENTRY.dest)})")
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
        crypt_unit = UnitName(f"systemd-cryptsetup@{spec.name}.service")
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
        self._apply_managed_file(TMPFILES_ENTRY, env.src_tmpfiles, env.tmpfiles_conf, events)
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
                swapoff = self._systemd.stop(UnitName("dev-zram0.swap"))
                _show_output(swapoff, events, self._env.prog)
                if swapoff.returncode == 0:
                    # Silent, as `systemctl stop ... 2>/dev/null || true` was: the reset below is the fallback.
                    self._systemd.stop(UnitName("systemd-zram-setup@zram0.service"))
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
        self._apply_managed_file(SYSCTL_ENTRY, env.src_sysctl, env.sysctl_conf, events)
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
        written = self._apply_managed_file(DROPIN_ENTRY, env.src_dropin, env.slice_dropin, events)
        if self._remove_old_dropin(events) or written:
            self._systemd.daemon_reload()
        # swap:core's files are all in place: the install record says the component is complete.
        self._installer.mark(env.install_record, DROPIN_ENTRY.component)

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
