# SPDX-License-Identifier: MPL-2.0
"""Application layer for `cst btrfs cache provision {plan,init,apply}` and `unprovision`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from btrfs.cache.domain import (
    BASE_FOR_KIND,
    CACHE_BASE,
    FSTYPE_BTRFS,
    OPT_COMPRESS_FORCE,
    SCRATCHPAD_BASE,
    CacheMountKind,
    CacheMountParsedArgs,
    LiveMount,
    ProvisionSubcommand,
    mounts_by_target,
    parse_all_mounts,
    parse_managed_fstab_entries,
)
from btrfs.cache.domain.format import cache_seam_lines
from btrfs.cache.domain.system import parse_system_fstab_entries
from btrfs.cache.domain.provision import (
    BTRFS_CACHE_LABEL,
    CACHE_CONF_PATH,
    CRYPTTAB_PATH,
    DEFAULT_MAPPER_NAME,
    SUBVOL_FOR_KIND,
    BlockDeviceFacts,
    CacheHardwareSpec,
    LuksMapperState,
    ProvisionBaseState,
    ProvisionProbe,
    format_provision_device_plan,
    format_provision_plan,
    needs_base_remount,
    parse_cache_conf,
    plan_provision_crypttab,
    plan_provision_fstab,
    plan_unprovision_crypttab,
    plan_unprovision_fstab,
    render_cache_conf,
    render_crypttab_line,
    unmanaged_base_fstab_conflicts,
    validate_device_for_init,
    validate_provision_apply,
    validate_unprovision,
)
from btrfs.cache.domain.systemd_view import fstab_lines_unread
from btrfs.cache.ports import (
    CacheProvisionPort,
    CacheSystemdPort,
    FstabPort,
    MountTablePort,
)
from btrfs.compress.domain import FSTAB_PATH
from cst_core.domain.values import ExitAndLines
from cst_core.ports import ReplacedNotSynced


@dataclass(frozen=True, slots=True)
class ProvisionEnv:
    """Ports and seam root needed by `provision` and `unprovision`."""

    prov: CacheProvisionPort | None
    fstab: FstabPort
    mounts: MountTablePort
    systemd: CacheSystemdPort
    seam_root: str = ""


@dataclass(slots=True)
class _ProvisionTxn:
    """Undo ledger for every resource created during this `provision apply` run."""

    keyfile_created: str | None = None
    luks_created: bool = False
    opened_mapper: str | None = None
    mounted_bases: list[str] = field(default_factory=list)
    created_base_dirs: list[str] = field(default_factory=list)
    btrfs_created: bool = False
    prev_crypttab: str | None = None
    crypttab_written: bool = False
    prev_fstab: str | None = None
    fstab_written: bool = False


@dataclass(slots=True)
class _UnprovisionState:
    probe: ProvisionProbe
    spec: CacheHardwareSpec | None
    env: ProvisionEnv
    fstab_changed: bool = False
    crypttab_changed: bool = False
    unmounted_kinds: list[CacheMountKind] = field(default_factory=list)

    def rollback(self) -> list[str]:
        errs: list[str] = []
        assert self.env.prov is not None
        for kind in reversed(self.unmounted_kinds):
            src = (
                self.spec.mapper_path
                if self.spec is not None
                else self.probe.bases[kind].mounted_source.split("[", 1)[0]
            )
            if not src:
                continue
            merr = self.env.prov.mount_base(src, kind, BASE_FOR_KIND[kind])
            if merr is not None:
                errs.append(merr)
        if self.crypttab_changed:
            errs.extend(_restore_crypttab(self.env.prov, self.probe.crypttab_text))
        if self.fstab_changed:
            errs.extend(_restore_provision_fstab(self.env.fstab, self.probe.fstab_text, self.env.systemd))
        return errs


def _read_existing_conf(
    prov: CacheProvisionPort,
) -> tuple[str | None, str | None, CacheHardwareSpec | None, tuple[str, ...]]:
    text, err = prov.read_conf()
    if err is not None or text is None:
        return None, err, None, ()
    parsed = parse_cache_conf(text)
    return text, None, parsed.spec, parsed.problems


def _probe_bases(
    live: dict[str, LiveMount],
    fstab_text: str | None,
    prov: CacheProvisionPort,
) -> dict[CacheMountKind, ProvisionBaseState]:
    unmanaged = set(unmanaged_base_fstab_conflicts(fstab_text or ""))
    fstab_map = {e.target: e for e in parse_all_mounts(fstab_text or "")}
    result: dict[CacheMountKind, ProvisionBaseState] = {}
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        path = BASE_FOR_KIND[kind]
        pk = prov.stat_path(path)
        mnt = live.get(path)
        fst = fstab_map.get(path)
        result[kind] = ProvisionBaseState(
            kind=kind,
            dir_exists=pk.exists,
            dir_is_symlink=pk.is_symlink,
            dir_not_dir=pk.exists and not pk.is_symlink and not pk.is_dir,
            mounted_source=mnt.source if mnt is not None else "",
            mounted_fstype=mnt.fstype if mnt is not None else "",
            mounted_uuid=mnt.uuid if mnt is not None else "",
            mounted_fsroot=mnt.fsroot if mnt is not None else "",
            mounted_options=mnt.options if mnt is not None else "",
            fstab_source=fst.source if fst is not None else "",
            fstab_managed=fst is not None and path not in unmanaged,
            fstab_unmanaged=path in unmanaged,
        )
    return result


def _is_base_subvol_bind(m: LiveMount, bases: dict[CacheMountKind, ProvisionBaseState]) -> bool:
    if m.target in (CACHE_BASE, SCRATCHPAD_BASE) or not m.fsroot:
        return False
    for kind, base in bases.items():
        if not base.mounted_uuid or m.uuid != base.mounted_uuid:
            continue
        root_pref = base.mounted_fsroot.rstrip("/") or f"/{SUBVOL_FOR_KIND[kind]}"
        if m.fsroot.startswith(f"{root_pref}/"):
            return True
    return False


def _user_mounts_under_bases(
    mounts: tuple[LiveMount, ...], bases: dict[CacheMountKind, ProvisionBaseState]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                m.target
                for m in mounts
                if m.target.startswith(f"{CACHE_BASE}/")
                or m.target.startswith(f"{SCRATCHPAD_BASE}/")
                or _is_base_subvol_bind(m, bases)
            }
        )
    )


def _probe_mapper_and_luks(
    spec: CacheHardwareSpec,
    dev: BlockDeviceFacts | None,
    bases: dict[CacheMountKind, ProvisionBaseState],
    prov: CacheProvisionPort,
) -> LuksMapperState:
    kf = prov.stat_path(spec.keyfile_path)
    kf_bad = kf.exists and (kf.is_symlink or not kf.is_regular)
    mapper = prov.stat_path(spec.mapper_path)

    luks_uuid: str | None = None
    unlocks: bool | None = None
    if dev is not None and dev.fstype == "crypto_LUKS":
        luks_uuid = prov.luks_uuid(spec.device_path)
        if kf.exists and not kf_bad:
            unlocks = prov.test_keyfile(spec.device_path, spec.keyfile_path)

    mp_fstype, mp_uuid, mp_label, mp_luks_uuid = (
        prov.mapper_fs_info(spec.mapper_path) if mapper.exists else ("", "", "", None)
    )
    subvols: list[str] = []
    if mapper.exists and mp_fstype == FSTYPE_BTRFS:
        listed, _ = prov.list_top_subvolumes(spec.mapper_path)
        subvols.extend(listed)
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        b = bases[kind]
        subvol_name = SUBVOL_FOR_KIND[kind]
        if (
            subvol_name not in subvols
            and b.mounted_source
            and b.mounted_fstype == FSTYPE_BTRFS
            and b.mounted_fsroot in ("", f"/{subvol_name}")
        ):
            subvols.append(subvol_name)
    return LuksMapperState(
        kf.exists, kf_bad, mapper.exists, luks_uuid, mp_luks_uuid, unlocks, mp_fstype, mp_uuid, mp_label, tuple(subvols)
    )


def _probe_provision(prov: CacheProvisionPort, env: ProvisionEnv) -> ProvisionProbe:
    conf_text, conf_error, spec, conf_problems = _read_existing_conf(prov)
    crypttab_text, crypttab_error = prov.read_crypttab_exact()
    fstab_text: str | None = None
    fstab_error: str | None = None
    try:
        fstab_text = env.fstab.read()
    except OSError as exc:
        fstab_error = f"cannot read {FSTAB_PATH}: {exc.strerror or exc}"

    live_mounts, mounts_error = env.mounts.read()
    if mounts_error is not None and fstab_error is None:
        fstab_error = f"cannot read mounts: {mounts_error}"
    bases = _probe_bases(mounts_by_target(live_mounts), fstab_text, prov)
    dev, dev_error = prov.inspect_device(spec.device_path) if spec is not None else (None, None)

    return ProvisionProbe(
        conf_text=conf_text,
        conf_error=conf_error,
        spec=spec,
        conf_problems=conf_problems,
        device=dev,
        device_error=dev_error,
        luks=_probe_mapper_and_luks(spec, dev, bases, prov) if spec is not None else LuksMapperState(),
        crypttab_text=crypttab_text,
        crypttab_error=crypttab_error,
        fstab_text=fstab_text,
        fstab_error=fstab_error,
        bases=bases,
        user_managed_entries=parse_managed_fstab_entries(fstab_text or ""),
        user_mounts_active=_user_mounts_under_bases(live_mounts, bases),
        fstab_behind=fstab_lines_unread(env.systemd.mount_units(), {CACHE_BASE, SCRATCHPAD_BASE}),
        system_managed_entries=parse_system_fstab_entries(fstab_text or ""),
    )


def _restore_provision_fstab(fstab: FstabPort, prev: str | None, systemd: CacheSystemdPort) -> list[str]:
    errs: list[str] = []
    try:
        if prev is None:
            fstab.remove()
        else:
            fstab.write(prev)
    except ReplacedNotSynced as exc:
        errs.append(f"{FSTAB_PATH} put back, folder not synced: {exc.strerror or exc}")
    except OSError as exc:
        errs.append(f"cannot restore {FSTAB_PATH}: {exc.strerror or exc}")
    else:
        why = systemd.daemon_reload()
        if why is not None:
            errs.append(why)
    return errs


def _restore_crypttab(prov: CacheProvisionPort, prev: str | None) -> list[str]:
    try:
        if prev is None:
            prov.remove_crypttab()
        else:
            prov.write_crypttab(prev)
    except ReplacedNotSynced as exc:
        return [f"{CRYPTTAB_PATH} put back, folder not synced: {exc.strerror or exc}"]
    except OSError as exc:
        return [f"cannot restore {CRYPTTAB_PATH}: {exc.strerror or exc}"]
    return []


def _rollback_provision(txn: _ProvisionTxn, prov: CacheProvisionPort, env: ProvisionEnv) -> list[str]:
    errs: list[str] = []
    if txn.fstab_written:
        errs.extend(_restore_provision_fstab(env.fstab, txn.prev_fstab, env.systemd))
    if txn.crypttab_written:
        errs.extend(_restore_crypttab(prov, txn.prev_crypttab))
    for target in reversed(txn.mounted_bases):
        uerr = prov.umount_base(target)
        if uerr is not None:
            errs.append(uerr)
    for d in reversed(txn.created_base_dirs):
        prov.remove_empty_dir(d)
    if txn.opened_mapper is not None:
        cerr = prov.luks_close(txn.opened_mapper)
        if cerr is not None:
            errs.append(cerr)
    if txn.keyfile_created is not None and not txn.luks_created:
        prov.remove_file(txn.keyfile_created)
    return errs


def _fail_txn(
    cause: str, txn: _ProvisionTxn, prov: CacheProvisionPort, env: ProvisionEnv, seam: list[str]
) -> ExitAndLines:
    undo_errs = _rollback_provision(txn, prov, env)
    if undo_errs:
        return 1, [*seam, f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(undo_errs)})"]
    return 1, [*seam, f"ERROR: {cause}; changes rolled back cleanly."]


def _provision_plan(
    parsed: CacheMountParsedArgs, prog: str, prov: CacheProvisionPort, env: ProvisionEnv, seam: list[str]
) -> ExitAndLines:
    if parsed.device is not None:
        _, conf_err, existing_spec, conf_problems = _read_existing_conf(prov)
        if conf_err is not None:
            return 0, [*seam, f"BLOCKED: {conf_err}"]
        if conf_problems:
            return 0, [*seam, *(f"BLOCKED: {CACHE_CONF_PATH}: {p}" for p in conf_problems)]
        dev_probe = prov.inspect_device(parsed.device)
        mapper = parsed.mapper or (existing_spec.mapper_name if existing_spec is not None else DEFAULT_MAPPER_NAME)
        return 0, format_provision_device_plan(
            dev_probe,
            existing_conf=existing_spec,
            mapper_name=mapper,
            prog=prog,
            seam_lines=seam,
        )
    probe = _probe_provision(prov, env)
    return 0, format_provision_plan(probe, prog, do_format=parsed.do_format, seam_lines=seam)


def _prepare_init_spec(
    parsed: CacheMountParsedArgs, prov: CacheProvisionPort
) -> tuple[CacheHardwareSpec | None, str | None, str | None]:
    if parsed.device is None:
        return None, None, "`provision init` requires --device PATH"
    conf_text, conf_err, existing_spec, conf_problems = _read_existing_conf(prov)
    if conf_err is not None:
        return None, None, conf_err
    if conf_problems:
        return None, None, "; ".join(f"{CACHE_CONF_PATH}: {p}" for p in conf_problems)
    dev, dev_err = prov.inspect_device(parsed.device)
    if dev_err is not None or dev is None:
        return None, None, dev_err or "cannot inspect device"
    mapper = parsed.mapper or (existing_spec.mapper_name if existing_spec is not None else DEFAULT_MAPPER_NAME)
    blockers = validate_device_for_init(dev, existing_conf=existing_spec, mapper_name=mapper)
    if blockers:
        return None, None, "; ".join(blockers)
    return CacheHardwareSpec(partuuid=dev.partuuid, size_bytes=dev.size_bytes, mapper_name=mapper), conf_text, None


def _provision_init(parsed: CacheMountParsedArgs, prog: str, prov: CacheProvisionPort, seam: list[str]) -> ExitAndLines:
    spec, conf_text, err = _prepare_init_spec(parsed, prov)
    if err is not None or spec is None:
        return 1, [*seam, f"ERROR: {err or 'invalid device'}"]
    rendered = render_cache_conf(spec, origin="init")
    if conf_text == rendered:
        return 0, [*seam, f"{CACHE_CONF_PATH} is already up to date (PARTUUID={spec.partuuid}); nothing to do."]
    try:
        prov.write_conf(rendered)
    except OSError as exc:
        return 1, [*seam, f"ERROR: cannot write {CACHE_CONF_PATH}: {exc.strerror or exc}"]
    return 0, [
        *seam,
        f"Wrote {CACHE_CONF_PATH} (PARTUUID={spec.partuuid}, size={spec.size_bytes} bytes, mapper={spec.mapper_name}).",
        f"Partition untouched. Next: `{prog} provision plan` (or `{prog} provision apply --format` if partition is blank).",
    ]


@dataclass(frozen=True, slots=True)
class _ApplyContext:
    probe: ProvisionProbe
    spec: CacheHardwareSpec
    prov: CacheProvisionPort
    env: ProvisionEnv
    txn: _ProvisionTxn
    lines: list[str]


def _confirm_format_if_needed(
    prov: CacheProvisionPort,
    spec: CacheHardwareSpec,
    *,
    needs_luks: bool,
    assume_yes: bool,
) -> tuple[bool, str | None]:
    action = "LUKS2 format + Btrfs mkfs" if needs_luks else "Btrfs mkfs"
    prompt = (
        f"About to run {action} on {spec.device_path} (size={spec.size_bytes} bytes, mapper={spec.mapper_name}). "
        "Continue? [y/N]: "
    )
    return prov.confirm_format(prompt, assume_yes=assume_yes)


def _ensure_keyfile(ctx: _ApplyContext) -> str | None:
    probe, spec, prov, txn, lines = ctx.probe, ctx.spec, ctx.prov, ctx.txn, ctx.lines
    if probe.luks.keyfile_exists:
        return None
    try:
        prov.create_keyfile(spec.keyfile_path)
    except ReplacedNotSynced as exc:
        txn.keyfile_created = spec.keyfile_path
        return f"cannot sync {spec.keyfile_path}: {exc.strerror or exc}"
    except OSError as exc:
        return f"cannot create {spec.keyfile_path}: {exc.strerror or exc}"
    txn.keyfile_created = spec.keyfile_path
    lines.append(f"Created keyfile {spec.keyfile_path}.")
    return None


def _ensure_luks_and_open_mapper(
    ctx: _ApplyContext, *, needs_luks: bool, assume_yes: bool
) -> tuple[str | None, str | None]:
    probe, spec, prov, txn, lines = ctx.probe, ctx.spec, ctx.prov, ctx.txn, ctx.lines
    if needs_luks:
        ok, why = _confirm_format_if_needed(prov, spec, needs_luks=True, assume_yes=assume_yes)
        if not ok:
            return None, why or "formatting cancelled"
    if (kf_err := _ensure_keyfile(ctx)) is not None:
        return None, kf_err
    if needs_luks:
        err = prov.luks_format(spec.device_path, spec.keyfile_path)
        if err is not None:
            return None, err
        txn.luks_created = True
        lines.append(f"Formatted LUKS2 on {spec.device_path}.")
    luks_uuid = prov.luks_uuid(spec.device_path)
    if luks_uuid is None or luks_uuid == "":
        return None, f"cannot determine LUKS UUID of {spec.device_path}"
    if not probe.luks.mapper_exists:
        err = prov.luks_open(spec.device_path, spec.mapper_name, spec.keyfile_path)
        if err is not None:
            return None, err
        txn.opened_mapper = spec.mapper_name
        lines.append(f"Opened {spec.mapper_path}.")
    return luks_uuid, None


def _validate_opened_mapper(
    ctx: _ApplyContext, luks_uuid: str, *, do_format: bool, assume_yes: bool, needs_luks: bool
) -> tuple[str | None, str | None]:
    spec = ctx.spec
    fstype, _, label, mapper_luks_uuid = ctx.prov.mapper_fs_info(spec.mapper_path)
    if mapper_luks_uuid != luks_uuid:
        return None, f"{spec.mapper_path} does not belong to LUKS UUID {luks_uuid} (found {mapper_luks_uuid!r})"
    if fstype and fstype != FSTYPE_BTRFS:
        return None, f"{spec.mapper_path} holds filesystem {fstype!r}, not {FSTYPE_BTRFS}"
    if fstype == FSTYPE_BTRFS and label not in ("", BTRFS_CACHE_LABEL, spec.mapper_name):
        return None, f"{spec.mapper_path} has btrfs label {label!r} (expected {BTRFS_CACHE_LABEL!r})"
    if not fstype and not do_format:
        return None, f"{spec.mapper_path} has no filesystem yet; pass --format to create Btrfs subvolumes"
    if not fstype and not needs_luks:
        ok, why = _confirm_format_if_needed(ctx.prov, spec, needs_luks=False, assume_yes=assume_yes)
        if not ok:
            return None, why or "formatting cancelled"
    return fstype, None


def _step_luks_and_open(
    ctx: _ApplyContext, *, do_format: bool, assume_yes: bool
) -> tuple[str | None, str | None, str | None]:
    """Ensure keyfile, LUKS2 header, open mapper, and return `(luks_uuid, mapper_fstype, error)`."""
    dev = ctx.probe.device
    assert dev is not None
    needs_luks = dev.fstype == "" and dev.pttype == ""
    luks_uuid, open_err = _ensure_luks_and_open_mapper(ctx, needs_luks=needs_luks, assume_yes=assume_yes)
    if open_err is not None or luks_uuid is None:
        return None, None, open_err or "cannot open LUKS mapper"
    fstype, err = _validate_opened_mapper(
        ctx, luks_uuid, do_format=do_format, assume_yes=assume_yes, needs_luks=needs_luks
    )
    if err is not None:
        return None, None, err
    return luks_uuid, fstype, None


def _step_btrfs_and_mounts(ctx: _ApplyContext, mapper_fstype: str) -> str | None:
    probe, spec, prov, txn, lines = ctx.probe, ctx.spec, ctx.prov, ctx.txn, ctx.lines
    if mapper_fstype == "":
        err = prov.mkfs_btrfs(spec.mapper_path, BTRFS_CACHE_LABEL)
        if err is not None:
            return err
        txn.btrfs_created = True
        lines.append(f"Formatted Btrfs (-L {BTRFS_CACHE_LABEL} -m dup -d single) on {spec.mapper_path}.")
    wanted_subvols = tuple(SUBVOL_FOR_KIND[k] for k in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD))
    created_subvols, sub_err = prov.ensure_subvolumes(spec.mapper_path, wanted_subvols)
    if sub_err is not None:
        return sub_err
    if created_subvols:
        lines.append(f"Created subvolume(s): {', '.join(created_subvols)}.")
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        path = BASE_FOR_KIND[kind]
        base_state = probe.bases[kind]
        if not base_state.dir_exists:
            try:
                prov.ensure_base_dir(path)
            except OSError as exc:
                return f"cannot create base folder {path}: {exc.strerror or exc}"
            txn.created_base_dirs.append(path)
        if not base_state.mounted_source:
            merr = prov.mount_base(spec.mapper_path, kind, path)
            if merr is not None:
                return merr
            txn.mounted_bases.append(path)
            lines.append(f"Mounted {SUBVOL_FOR_KIND[kind]} -> {path}.")
    return None


def _write_config_guarded(
    path: str,
    new_text: str,
    cur_text: str,
    writer: Callable[[str], None],
    *,
    on_written: Callable[[], None],
) -> tuple[bool, str | None]:
    if new_text == cur_text:
        return False, None
    try:
        writer(new_text)
    except ReplacedNotSynced as exc:
        on_written()
        return True, f"cannot write {path}: {exc.strerror or exc}"
    except OSError as exc:
        return False, f"cannot write {path}: {exc.strerror or exc}"
    on_written()
    return True, None


def _step_crypttab_and_fstab(ctx: _ApplyContext, luks_uuid: str) -> str | None:
    probe, spec, prov, env, txn, lines = ctx.probe, ctx.spec, ctx.prov, ctx.env, ctx.txn, ctx.lines

    def _mark_crypt() -> None:
        txn.prev_crypttab = probe.crypttab_text
        txn.crypttab_written = True

    wrote_crypt, cerr = _write_config_guarded(
        CRYPTTAB_PATH,
        plan_provision_crypttab(probe.crypttab_text or "", spec, luks_uuid),
        probe.crypttab_text or "",
        prov.write_crypttab,
        on_written=_mark_crypt,
    )
    if cerr is not None:
        return cerr
    if wrote_crypt:
        lines.append(f"Updated {CRYPTTAB_PATH} ({render_crypttab_line(spec, luks_uuid)}).")

    def _mark_fstab() -> None:
        txn.prev_fstab = probe.fstab_text
        txn.fstab_written = True

    wrote_fstab, ferr = _write_config_guarded(
        FSTAB_PATH,
        plan_provision_fstab(probe.fstab_text or "", spec),
        probe.fstab_text or "",
        env.fstab.write,
        on_written=_mark_fstab,
    )
    if ferr is not None:
        return ferr
    if wrote_fstab or wrote_crypt or probe.fstab_behind is True:
        rerr = env.systemd.daemon_reload()
        if rerr is not None:
            return rerr
        if wrote_fstab:
            lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
        else:
            lines.append("Reloaded systemd.")
    return None


def _step_remount_bases(ctx: _ApplyContext) -> str | None:
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        if not needs_base_remount(ctx.probe.bases[kind]):
            continue
        path = BASE_FOR_KIND[kind]
        rerr = ctx.prov.remount_base(path)
        if rerr is not None:
            return rerr
        ctx.lines.append(f"Remounted {path} with {OPT_COMPRESS_FORCE}.")
    return None


def _provision_apply(
    parsed: CacheMountParsedArgs, prov: CacheProvisionPort, env: ProvisionEnv, seam: list[str]
) -> ExitAndLines:
    probe = _probe_provision(prov, env)
    blockers = validate_provision_apply(probe, do_format=parsed.do_format)
    if blockers or probe.spec is None:
        return 1, [*seam, *(f"ERROR: {b}" for b in (blockers or ["configuration missing"]))]
    spec = probe.spec
    txn = _ProvisionTxn()
    lines = [*seam]
    ctx = _ApplyContext(probe=probe, spec=spec, prov=prov, env=env, txn=txn, lines=lines)

    luks_uuid, mapper_fstype, err = _step_luks_and_open(ctx, do_format=parsed.do_format, assume_yes=parsed.assume_yes)
    if err is not None or luks_uuid is None or mapper_fstype is None:
        return _fail_txn(err or "LUKS step failed", txn, prov, env, seam)

    berr = _step_btrfs_and_mounts(ctx, mapper_fstype)
    if berr is not None:
        return _fail_txn(berr, txn, prov, env, seam)

    cerr = _step_crypttab_and_fstab(ctx, luks_uuid)
    if cerr is not None:
        return _fail_txn(cerr, txn, prov, env, seam)

    rerr = _step_remount_bases(ctx)
    if rerr is not None:
        return _fail_txn(rerr, txn, prov, env, seam)

    if txn.btrfs_created:
        serr = prov.scrub_base(CACHE_BASE)
        if serr is None:
            lines.append(f"Completed initial scrub on {CACHE_BASE}.")
        else:
            lines.append(
                f"WARNING: initial scrub on {CACHE_BASE} failed ({serr}); "
                f"run 'sudo btrfs scrub start -B {CACHE_BASE}' manually."
            )

    if len(lines) == len(seam):
        lines.append(f"Already provisioned ({CACHE_BASE}, {SCRATCHPAD_BASE}); nothing to do.")
    return 0, lines


def run_provision(parsed: CacheMountParsedArgs, env: ProvisionEnv, prog: str) -> ExitAndLines:
    """Dispatch `cst btrfs cache provision {plan,init,apply}`."""
    seam = cache_seam_lines(env.seam_root)
    if env.prov is None:
        return 1, [*seam, "ERROR: provisioning adapter is unavailable"]
    match parsed.provision_action:
        case ProvisionSubcommand.PLAN:
            return _provision_plan(parsed, prog, env.prov, env, seam)
        case ProvisionSubcommand.INIT:
            return _provision_init(parsed, prog, env.prov, seam)
        case ProvisionSubcommand.APPLY:
            return _provision_apply(parsed, env.prov, env, seam)
        case _:
            return _provision_plan(parsed, prog, env.prov, env, seam)


def _format_unprov_rollback(cause: str, errs: list[str], fallback: str) -> str:
    suffix = f"ROLLBACK INCOMPLETE ({'; '.join(errs)})" if errs else fallback
    return f"ERROR: {cause}; {suffix}"


def _write_unprovision_configs(
    st: _UnprovisionState, mapper_name: str, lines: list[str], seam: list[str]
) -> tuple[bool, list[str] | None]:
    prov = st.env.prov
    assert prov is not None
    wrote_fstab, ferr = _write_config_guarded(
        FSTAB_PATH,
        plan_unprovision_fstab(st.probe.fstab_text or ""),
        st.probe.fstab_text or "",
        st.env.fstab.write,
        on_written=lambda: setattr(st, "fstab_changed", True),
    )
    if ferr is not None:
        return False, [*seam, _format_unprov_rollback(ferr, st.rollback(), "restored.")]
    if wrote_fstab:
        lines.append(f"Removed base mount line(s) from {FSTAB_PATH}.")

    wrote_crypt, cerr = _write_config_guarded(
        CRYPTTAB_PATH,
        plan_unprovision_crypttab(st.probe.crypttab_text or "", mapper_name),
        st.probe.crypttab_text or "",
        prov.write_crypttab,
        on_written=lambda: setattr(st, "crypttab_changed", True),
    )
    if cerr is not None:
        return False, [*seam, _format_unprov_rollback(cerr, st.rollback(), f"{FSTAB_PATH} restored.")]
    if wrote_crypt:
        lines.append(f"Removed {mapper_name} entry from {CRYPTTAB_PATH}.")

    if st.fstab_changed or st.crypttab_changed or st.probe.fstab_behind is True:
        rerr = st.env.systemd.daemon_reload()
        if rerr is not None:
            return False, [
                *seam,
                _format_unprov_rollback(rerr, st.rollback(), f"{FSTAB_PATH} and {CRYPTTAB_PATH} restored."),
            ]
    return True, None


def run_unprovision(env: ProvisionEnv) -> ExitAndLines:
    """Undo provisioning: remove base fstab + crypttab lines, reload systemd, unmount bases, close mapper."""
    seam = cache_seam_lines(env.seam_root)
    if env.prov is None:
        return 1, [*seam, "ERROR: provisioning adapter is unavailable"]
    prov = env.prov
    probe = _probe_provision(prov, env)
    blockers = validate_unprovision(probe)
    if blockers:
        return 1, [*seam, *(f"ERROR: {b}" for b in blockers)]

    mapper_name = probe.spec.mapper_name if probe.spec is not None else DEFAULT_MAPPER_NAME
    st = _UnprovisionState(probe=probe, spec=probe.spec, env=env)
    lines = [*seam]

    ok, err_lines = _write_unprovision_configs(st, mapper_name, lines, seam)
    if not ok and err_lines is not None:
        return 1, err_lines

    for kind in (CacheMountKind.SCRATCHPAD, CacheMountKind.CACHE):
        if not probe.bases[kind].mounted_source:
            continue
        uerr = prov.umount_base(BASE_FOR_KIND[kind])
        if uerr is not None:
            return 1, [*seam, _format_unprov_rollback(uerr, st.rollback(), "mounts and configs restored.")]
        st.unmounted_kinds.append(kind)
        lines.append(f"Unmounted {BASE_FOR_KIND[kind]}.")

    if prov.stat_path(f"/dev/mapper/{mapper_name}").exists:
        cerr = prov.luks_close(mapper_name)
        if cerr is not None:
            return 1, [*seam, _format_unprov_rollback(cerr, st.rollback(), "mounts and configs restored.")]
        lines.append(f"Closed /dev/mapper/{mapper_name}.")

    for kind in st.unmounted_kinds:
        prov.remove_empty_dir(BASE_FOR_KIND[kind])

    if len(lines) == len(seam):
        lines.append("Cache drive already unprovisioned; nothing to do.")
    else:
        lines.append(f"Kept {CACHE_CONF_PATH}, keyfile, and data on the partition untouched.")
    return 0, lines
