# SPDX-License-Identifier: MPL-2.0
"""Linux host adapters for `cst desktop psd`: mounts, /proc busy check, install units, and recovery."""

from __future__ import annotations

import contextlib
import grp
import os
import posixpath
import pwd
import shutil
from typing import Final

from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import LocalFileSystemAdapter, ensure_dir, read_text
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import checkout_commit, checkout_root, install_payload
from cst_core.adapters.zipapp import entry_payload
from cst_core.application.install import InstallRequest, InstallService
from cst_core.domain.errors import Refused
from cst_core.domain.install import INSTALL_MANIFEST, ArtifactKind, Component, InstallEntry, select_entries
from cst_core.domain.record import ROOT_RECORD
from cst_core.domain.values import ROOT_ACCOUNT, DomainValidationError, parse_whole_number
from cst_core.ports import CommandRunnerPort, OutputMode
from desktop.adapters.overlay import (
    apply_recovery_to_lower,
    dir_size_bytes,
    merge_upper_to_lower,
    remove_path,
    snapshot_upper_to_recovery,
)
from desktop.domain import (
    DEFAULT_TMPFS_BYTES,
    DESKTOP_CONF_REL,
    parse_desktop_config,
    parse_tmpfs_size_bytes,
)
from desktop.domain.psd import (
    KNOWN_PROFILES,
    PSD_RESYNC_SERVICE_TEMPLATE,
    PSD_RESYNC_TIMER_TEMPLATE,
    PSD_SEAM_PROC_ENV,
    PSD_SEAM_ROOT_ENV,
    PSD_SERVICE_TEMPLATE,
    KnownProfile,
    PsdBusyProcess,
    PsdParsedMounts,
    PsdProfileProbe,
    PsdProfileState,
    PsdSnapshot,
    PsdTargetUser,
    clamp_tmpfs_bytes,
    parse_meminfo_total_bytes,
    parse_psd_mounts,
    resolve_profile_selection,
)

_PSD_MOUNT_TIMEOUT_S: Final = 30
_PSD_DELETED_SUFFIX: Final = " (deleted)"
_PSD_COMM_LIMIT: Final = 64
_PROC_MAPS_FIELDS: Final = 6


def _map_host_path(path: str, seam_root: str) -> str:
    if not seam_root:
        return path
    return posixpath.join(seam_root, path.lstrip("/"))


def _resolve_psd_user(requested: str | None, *, seam_root: str = "") -> PsdTargetUser:
    candidate = requested or os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    login = candidate.strip()
    if not login or login == ROOT_ACCOUNT:
        raise Refused("specify --user NAME (or run via sudo from your user account)")
    try:
        pw_rec = pwd.getpwnam(login)
    except KeyError:
        raise Refused(f"unknown account: {login}") from None
    if pw_rec.pw_uid == 0:
        raise Refused("refusing root account (browser profiles belong to a non-root user)")
    try:
        group_name = grp.getgrgid(pw_rec.pw_gid).gr_name
    except KeyError:
        group_name = str(pw_rec.pw_gid)
    logical_home = pw_rec.pw_dir.rstrip("/") or "/"
    home_path = logical_home if seam_root else os.path.realpath(logical_home)
    aliases = (logical_home,) if logical_home != home_path else ()
    return PsdTargetUser(
        name=pw_rec.pw_name,
        group=group_name,
        uid=pw_rec.pw_uid,
        gid=pw_rec.pw_gid,
        home=home_path,
        home_aliases=aliases,
    )


def _match_profile_path(raw_link: str, prefixes: tuple[str, ...]) -> str | None:
    cleaned = raw_link.removesuffix(_PSD_DELETED_SUFFIX)
    return next((cleaned for pref in prefixes if cleaned == pref or cleaned.startswith(f"{pref}/")), None)


def _open_path_under_prefixes(fd_dir: str, prefixes: tuple[str, ...]) -> str | None:
    try:
        with os.scandir(fd_dir) as fd_it:
            links = [entry.path for entry in fd_it]
    except OSError:
        return None
    for link_path in links:
        try:
            target = os.readlink(link_path)
        except OSError:
            continue
        if (hit := _match_profile_path(target, prefixes)) is not None:
            return hit
    return None


def _maps_path_under_prefixes(maps_path: str, prefixes: tuple[str, ...]) -> str | None:
    text = read_text(maps_path, "")
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) == _PROC_MAPS_FIELDS and (hit := _match_profile_path(parts[5].strip(), prefixes)) is not None:
            return hit
    return None


def scan_busy_processes(
    target_path: str,
    uid: int,
    *,
    proc_root: str = "/proc",
    extra_paths: tuple[str, ...] = (),
) -> tuple[PsdBusyProcess, ...]:
    """Inspect `/proc` for processes owned by `uid` holding `cwd`, open FDs, or mappings under `target_path`."""
    real_target = os.path.realpath(target_path)
    raw_candidates = (target_path, real_target, *extra_paths)
    prefixes = tuple(dict.fromkeys(p.rstrip("/") or "/" for p in raw_candidates if p))
    my_pid = os.getpid()
    found: list[PsdBusyProcess] = []
    try:
        proc_entries = list(os.scandir(proc_root))
    except OSError:
        return ()
    for entry in proc_entries:
        pid = parse_whole_number(entry.name)
        if pid is None or pid == my_pid:
            continue
        try:
            if os.stat(entry.path).st_uid != uid:
                continue
        except OSError:
            continue
        cwd_match: str | None = None
        with contextlib.suppress(OSError):
            cwd_match = _match_profile_path(os.readlink(f"{entry.path}/cwd"), prefixes)
        hit_path = (
            cwd_match
            or _open_path_under_prefixes(f"{entry.path}/fd", prefixes)
            or _maps_path_under_prefixes(f"{entry.path}/maps", prefixes)
        )
        if hit_path is not None:
            comm_raw = read_text(f"{entry.path}/comm", "", limit=_PSD_COMM_LIMIT) or "?"
            found.append(PsdBusyProcess(pid=pid, comm=comm_raw.strip() or "?", path=hit_path))
    found.sort(key=lambda p: p.pid)
    return tuple(found)


def _has_symlink_within_home(mapped_home: str, rel_path: str) -> bool:
    cur = mapped_home
    for seg in rel_path.split("/"):
        if not seg:
            continue
        cur = posixpath.join(cur, seg)
        if os.path.islink(cur):
            return True
    return False


def _probe_profile(
    spec_and_paths: tuple[object, ...],
    *,
    mounts: PsdParsedMounts,
    proc_root: str,
    seam_root: str,
    user: PsdTargetUser,
) -> PsdProfileProbe:
    spec, profile_path, lower_dir, upper_dir, work_dir, recovery_dir = spec_and_paths
    assert isinstance(spec, KnownProfile)
    assert isinstance(profile_path, str) and isinstance(lower_dir, str)
    assert isinstance(upper_dir, str) and isinstance(work_dir, str) and isinstance(recovery_dir, str)
    variants = tuple(posixpath.join(h, spec.rel_path) for h in user.home_variants)
    mounted_target = next((v for v in variants if v in mounts.overlay_targets), None)
    conflict_target = next((v for v in variants if v in mounts.other_mounts), None)
    effective_profile_path = mounted_target or conflict_target or profile_path

    m_home = _map_host_path(user.home, seam_root)
    m_profile = _map_host_path(effective_profile_path, seam_root)
    m_upper = _map_host_path(upper_dir, seam_root)
    m_rec = _map_host_path(recovery_dir, seam_root)

    overlay_active = mounted_target is not None
    lower_bind_active = lower_dir in mounts.lower_binds
    recovery_exists = os.path.isdir(m_rec)
    upper_used = dir_size_bytes(m_upper) if overlay_active else 0

    if _has_symlink_within_home(m_home, spec.rel_path):
        state = PsdProfileState.SYMLINK_BLOCKED
    elif conflict_target is not None:
        state = PsdProfileState.CONFLICT_MOUNT
    elif overlay_active:
        state = PsdProfileState.ACTIVE
    elif recovery_exists:
        state = PsdProfileState.RECOVERY_PENDING
    elif not os.path.isdir(m_profile):
        state = PsdProfileState.MISSING_DIR
    else:
        state = PsdProfileState.INACTIVE

    profile_exists = os.path.isdir(m_profile)
    busy = (
        scan_busy_processes(effective_profile_path, user.uid, proc_root=proc_root, extra_paths=variants)
        if profile_exists
        else ()
    )
    return PsdProfileProbe(
        spec,
        effective_profile_path,
        lower_dir,
        upper_dir,
        work_dir,
        recovery_dir,
        state=state,
        overlay_active=overlay_active,
        lower_bind_active=lower_bind_active,
        recovery_exists=recovery_exists,
        upper_used_bytes=upper_used,
        busy_procs=busy,
        profile_dir_exists=profile_exists,
    )


class LinuxPsdHost:
    """`PsdHostPort` over Linux `mount` / `umount`, overlayfs merges, `InstallService`, and `systemctl`."""

    def __init__(
        self,
        entry_file: str,
        *,
        fallback_root: str = "",
        runner: CommandRunnerPort | None = None,
        seam_root: str | None = None,
        proc_root: str | None = None,
    ) -> None:
        self._entry_file = entry_file
        self._fallback_root = fallback_root
        self._runner = SubprocessCommandRunner() if runner is None else runner
        self._fs = LocalFileSystemAdapter()
        self._seam_root = (os.environ.get(PSD_SEAM_ROOT_ENV, "") if seam_root is None else seam_root).rstrip("/")
        self._proc_root = (os.environ.get(PSD_SEAM_PROC_ENV, "/proc") if proc_root is None else proc_root).rstrip("/")
        self._systemd = SystemctlAdapter(self._runner, on_host=SystemctlHost.LOCAL)

    def _run_mount(self, args: list[str], what: str) -> None:
        res = self._runner.run(args, timeout=_PSD_MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE)
        if res.returncode != 0:
            raise Refused(res.failure(what))

    def _read_mounts(self) -> str:
        proc_mounts = os.path.join(self._proc_root, "mounts")
        return read_text(proc_mounts, "") or ""

    def _read_config_for_user(
        self, user: PsdTargetUser, profiles_override: str | None, size_override: str | None
    ) -> tuple[str, int, str]:
        conf_path = _map_host_path(posixpath.join(user.home, DESKTOP_CONF_REL), self._seam_root)
        raw_conf = self._fs.read_text(conf_path, None)
        cfg_err = ""
        profiles_spec = "auto"
        req_bytes = DEFAULT_TMPFS_BYTES
        if raw_conf is not None:
            try:
                cfg = parse_desktop_config(raw_conf, source=conf_path)
                profiles_spec = cfg.profiles_raw
                req_bytes = cfg.tmpfs_bytes
            except DomainValidationError as exc:
                cfg_err = str(exc)
        if profiles_override is not None:
            profiles_spec = profiles_override
        if size_override is not None and not cfg_err:
            try:
                req_bytes = parse_tmpfs_size_bytes(size_override)
            except DomainValidationError as exc:
                cfg_err = str(exc)
        return profiles_spec, req_bytes, cfg_err

    def _tmpfs_usage(self, user: PsdTargetUser, *, mounted: bool) -> tuple[int | None, int | None]:
        if not mounted:
            return None, None
        mapped_run = _map_host_path(user.run_dir, self._seam_root)
        try:
            st = os.statvfs(mapped_run)
        except OSError:
            return None, None
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        return max(0, total - avail), avail

    def _probe_profiles(
        self, profiles: tuple[KnownProfile, ...], user: PsdTargetUser, mounts: PsdParsedMounts
    ) -> tuple[PsdProfileProbe, ...]:
        return tuple(
            _probe_profile(
                (
                    p,
                    posixpath.join(user.home, p.rel_path),
                    posixpath.join(user.run_dir, p.profile_id, "lower"),
                    posixpath.join(user.run_dir, p.profile_id, "upper"),
                    posixpath.join(user.run_dir, p.profile_id, "work"),
                    posixpath.join(user.recovery_root, p.profile_id),
                ),
                mounts=mounts,
                proc_root=self._proc_root,
                seam_root=self._seam_root,
                user=user,
            )
            for p in profiles
        )

    def read_snapshot(
        self,
        *,
        user: str | None = None,
        profiles_override: str | None = None,
        size_override: str | None = None,
    ) -> PsdSnapshot:
        """Read config, `/proc/mounts`, memory cap, systemd state, and active/idle profiles."""
        try:
            target_user = _resolve_psd_user(user, seam_root=self._seam_root)
        except Refused as exc:
            return PsdSnapshot.unavailable(str(exc), self._seam_root)

        profiles_spec, req_bytes, cfg_err = self._read_config_for_user(target_user, profiles_override, size_override)
        meminfo = read_text(os.path.join(self._proc_root, "meminfo"), "")
        mem_total = parse_meminfo_total_bytes(meminfo)
        eff_bytes = clamp_tmpfs_bytes(req_bytes, mem_total)

        mounts = parse_psd_mounts(self._read_mounts(), run_base=target_user.run_dir)
        used_b, avail_b = self._tmpfs_usage(target_user, mounted=mounts.tmpfs_mounted)

        existing_rels = {
            p.rel_path
            for p in KNOWN_PROFILES
            if os.path.isdir(_map_host_path(posixpath.join(target_user.home, p.rel_path), self._seam_root))
            or any(posixpath.join(h, p.rel_path) in mounts.overlay_targets for h in target_user.home_variants)
            or os.path.isdir(_map_host_path(posixpath.join(target_user.recovery_root, p.profile_id), self._seam_root))
        }
        selected = ()
        if not cfg_err:
            try:
                selected = resolve_profile_selection(profiles_spec, existing_rels=existing_rels)
            except DomainValidationError as exc:
                cfg_err = str(exc)

        probes = self._probe_profiles(selected, target_user, mounts)
        selected_ids = {p.profile_id for p in selected}
        orphan_profiles = tuple(
            p
            for p in KNOWN_PROFILES
            if p.profile_id not in selected_ids
            and os.path.isdir(_map_host_path(posixpath.join(target_user.recovery_root, p.profile_id), self._seam_root))
        )
        orphan_probes = self._probe_profiles(orphan_profiles, target_user, mounts)

        unit_file = _map_host_path("/etc/systemd/system/cst-desktop-psd@.service", self._seam_root)
        systemd_installed = os.path.isfile(unit_file)
        svc_name = PSD_SERVICE_TEMPLATE.format(user=target_user.name)
        tmr_name = PSD_RESYNC_TIMER_TEMPLATE.format(user=target_user.name)
        return PsdSnapshot(
            target_user,
            "",
            profiles_spec,
            cfg_err,
            req_bytes,
            eff_bytes,
            mem_total,
            tmpfs_mounted=mounts.tmpfs_mounted,
            tmpfs_used_bytes=used_b,
            tmpfs_avail_bytes=avail_b,
            systemd_installed=systemd_installed,
            service_enabled=self._systemd.is_enabled(svc_name).returncode == 0 if systemd_installed else False,
            service_active=self._systemd.is_active(svc_name).returncode == 0 if systemd_installed else False,
            timer_enabled=self._systemd.is_enabled(tmr_name).returncode == 0 if systemd_installed else False,
            timer_active=self._systemd.is_active(tmr_name).returncode == 0 if systemd_installed else False,
            profiles=probes,
            seam_root=self._seam_root,
            orphan_recoveries=orphan_probes,
        )

    def _installer(self) -> InstallService:
        root = checkout_root(self._entry_file) or self._fallback_root

        def payload(entry: InstallEntry) -> bytes:
            if entry.kind == ArtifactKind.PYTHON_ENTRY:
                return install_payload(entry.src, self._entry_file)
            if not root:
                raise Refused(f"cannot read {entry.src} without a CST checkout")
            return entry_payload(root, entry)

        return InstallService(
            LocalInstallTarget(),
            payload,
            record=JsonInstallRecord(),
            commit=checkout_commit(root),
        )

    def _apply_manifest(self, *, remove: bool) -> list[str]:
        inst = self._installer()
        entries = select_entries(INSTALL_MANIFEST, component=Component.DESKTOP_PSD)
        req = InstallRequest(root=self._seam_root, as_root=True)
        if remove:
            places = [inst.placement(e, req) for e in entries]
            lines = list(inst.remove(places))
        else:
            lines = list(inst.install(entries, req))
            rec_path = f"{self._seam_root}{ROOT_RECORD}" if self._seam_root else ROOT_RECORD
            inst.mark(rec_path, Component.DESKTOP_PSD)
        if not self._seam_root:
            res = self._systemd.daemon_reload_result()
            if res.returncode != 0:
                raise Refused(res.failure("systemctl daemon-reload"))
        return lines

    def install_units(self) -> list[str]:
        """Install `cst-desktop` and the 3 `desktop:psd` systemd template units via `InstallService`."""
        return self._apply_manifest(remove=False)

    def remove_units(self) -> list[str]:
        """Remove `desktop:psd` installed files and record entries."""
        return self._apply_manifest(remove=True)

    def enable_systemd_units(self, username: str) -> None:
        """Enable boot/shutdown service and hourly resync timer for `username`."""
        if self._seam_root:
            return
        svc = PSD_SERVICE_TEMPLATE.format(user=username)
        tmr = PSD_RESYNC_TIMER_TEMPLATE.format(user=username)
        for unit in (svc, tmr):
            res = self._systemd.enable(unit, now=False)
            if res.returncode != 0:
                raise Refused(res.failure(f"systemctl enable {unit}"))
        for unit in (svc, tmr):
            res = self._systemd.start(unit)
            if res.returncode != 0:
                raise Refused(res.failure(f"systemctl start {unit}"))

    def disable_systemd_units(self, username: str) -> None:
        """Stop and disable boot/shutdown service and hourly resync timer for `username`."""
        if self._seam_root:
            return
        for unit in (
            PSD_RESYNC_TIMER_TEMPLATE.format(user=username),
            PSD_RESYNC_SERVICE_TEMPLATE.format(user=username),
            PSD_SERVICE_TEMPLATE.format(user=username),
        ):
            self._systemd.stop(unit)
        for unit in (
            PSD_RESYNC_TIMER_TEMPLATE.format(user=username),
            PSD_SERVICE_TEMPLATE.format(user=username),
        ):
            self._systemd.disable(unit)

    def ensure_tmpfs_mounted(self, user: PsdTargetUser, size_bytes: int) -> None:
        """Create `/run/cst-psd/<uid>` (0700 owned by user) and mount bounded tmpfs if not already mounted."""
        run_root = _map_host_path(os.path.dirname(user.run_dir), self._seam_root)
        ensure_dir(run_root, mode=0o755)
        run_dir = _map_host_path(user.run_dir, self._seam_root)
        ensure_dir(run_dir, mode=0o700)
        with contextlib.suppress(OSError):
            os.chown(run_dir, user.uid, user.gid)
        mounts = parse_psd_mounts(self._read_mounts(), run_base=user.run_dir)
        if mounts.tmpfs_mounted:
            return
        opts = f"mode=0700,uid={user.uid},gid={user.gid},size={size_bytes}"
        self._run_mount(["mount", "-t", "tmpfs", "-o", opts, "tmpfs", run_dir], f"mount tmpfs {user.run_dir}")

    def unmount_tmpfs_if_unused(self, user: PsdTargetUser) -> bool:
        """Unmount `/run/cst-psd/<uid>` when no profile lower bind or overlay remains."""
        mounts = parse_psd_mounts(self._read_mounts(), run_base=user.run_dir)
        if not mounts.tmpfs_mounted or mounts.overlay_targets or mounts.lower_binds:
            return False
        self._run_mount(["umount", _map_host_path(user.run_dir, self._seam_root)], f"umount {user.run_dir}")
        with contextlib.suppress(OSError):
            os.rmdir(_map_host_path(user.run_dir, self._seam_root))
        return True

    def mount_overlay(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        """Bind-mount original profile to `lower` (`--make-private`) and mount overlayfs over `profile_path`."""
        m_prof = _map_host_path(probe.profile_path, self._seam_root)
        m_run = _map_host_path(user.run_dir, self._seam_root).rstrip("/")
        m_base = _map_host_path(posixpath.dirname(probe.lower_dir), self._seam_root)
        m_low = _map_host_path(probe.lower_dir, self._seam_root)
        m_up = _map_host_path(probe.upper_dir, self._seam_root)
        m_wrk = _map_host_path(probe.work_dir, self._seam_root)
        for d in (m_base, m_low, m_up, m_wrk):
            created = ensure_dir(d, mode=0o700)
            for made in created:
                if made == m_run or made.startswith(f"{m_run}/"):
                    with contextlib.suppress(OSError):
                        os.chown(made, user.uid, user.gid)
            with contextlib.suppress(OSError):
                os.chown(d, user.uid, user.gid)
        self._run_mount(["mount", "--bind", m_prof, m_low], f"bind mount {probe.profile_path}")
        try:
            self._run_mount(["mount", "--make-private", m_low], f"make-private {probe.lower_dir}")
            opts = f"lowerdir={m_low},upperdir={m_up},workdir={m_wrk}"
            self._run_mount(
                ["mount", "-t", "overlay", f"cst-psd-{probe.spec.profile_id}", "-o", opts, m_prof],
                f"mount overlay {probe.profile_path}",
            )
        except BaseException:
            self._runner.run(["umount", m_low], timeout=_PSD_MOUNT_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE)
            raise

    def unmount_overlay(self, probe: PsdProfileProbe) -> None:
        """Unmount overlayfs from `profile_path` (leaving `lower` bind intact until merge finishes)."""
        self._run_mount(["umount", _map_host_path(probe.profile_path, self._seam_root)], f"umount {probe.profile_path}")

    def merge_upper(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        """Merge `upper` changes back into bound `lower` directory on RAID1, then unmount `lower` bind."""
        m_up = _map_host_path(probe.upper_dir, self._seam_root)
        m_low = _map_host_path(probe.lower_dir, self._seam_root)
        m_prof = _map_host_path(probe.profile_path, self._seam_root)
        target_lower = m_prof if self._seam_root else (m_low if os.path.isdir(m_low) else m_prof)
        merge_upper_to_lower(m_up, target_lower, uid=user.uid, gid=user.gid)
        mounts = parse_psd_mounts(self._read_mounts(), run_base=user.run_dir)
        if probe.lower_dir in mounts.lower_binds:
            self._run_mount(["umount", m_low], f"umount {probe.lower_dir}")

    def clear_upper_and_work(self, probe: PsdProfileProbe) -> None:
        """Remove `upper`, `work`, and empty `lower` dirs after verifying `lower` is no longer mounted."""
        m_prof_dir = _map_host_path(posixpath.dirname(probe.lower_dir), self._seam_root)
        m_low = _map_host_path(probe.lower_dir, self._seam_root)
        run_base = posixpath.dirname(posixpath.dirname(probe.lower_dir))
        mounts = parse_psd_mounts(self._read_mounts(), run_base=run_base)
        if probe.lower_dir in mounts.lower_binds or (not self._seam_root and os.path.ismount(m_low)):
            raise Refused(f"refusing to clean {probe.lower_dir}: lower bind mount is still active")
        for stage_dir in (probe.upper_dir, probe.work_dir):
            m_stage = _map_host_path(stage_dir, self._seam_root)
            if os.path.isdir(m_stage):
                shutil.rmtree(m_stage)
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(m_low)
        with contextlib.suppress(OSError):
            os.rmdir(m_prof_dir)

    def snapshot_recovery(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        """Copy live `upper` delta to `$HOME/.local/share/cst/psd/recovery/<id>` while browser is open."""
        snapshot_upper_to_recovery(
            _map_host_path(probe.upper_dir, self._seam_root),
            _map_host_path(probe.recovery_dir, self._seam_root),
            owner=(user.uid, user.gid),
            home_boundary=_map_host_path(user.home, self._seam_root),
        )

    def apply_recovery(self, probe: PsdProfileProbe, user: PsdTargetUser) -> None:
        """Replay saved recovery snapshot into `profile_path` and remove the recovery directory."""
        m_rec = _map_host_path(probe.recovery_dir, self._seam_root)
        apply_recovery_to_lower(m_rec, _map_host_path(probe.profile_path, self._seam_root), uid=user.uid, gid=user.gid)
        self.discard_recovery(probe)

    def discard_recovery(self, probe: PsdProfileProbe) -> None:
        """Delete unmerged recovery directory (or symlink) for a profile if present."""
        m_rec = _map_host_path(probe.recovery_dir, self._seam_root)
        if os.path.lexists(m_rec):
            remove_path(m_rec)
