# SPDX-License-Identifier: MPL-2.0
"""The use cases of `cst btrfs scrub-limit`: status, plan, and set|off with its roll back.

set|off writes snapshots.conf, then the drop-in through the installer, then reloads systemd when this run
changed the drop-in, or when systemd has not loaded it as it is on disk (exit 0 means systemd loaded the
limit). When the installer or daemon-reload fails, snapshots.conf and the drop-in go back to what they
were. Every read and write goes through the ports (ports.py).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from btrfs.scrub_limit.domain import (
    SCRUB_SERVICE,
    ScrubStatusSnapshot,
    render_scrub_dropin,
    scrub_limit_plan_lines,
    scrub_limit_plan_reload_lines,
    scrub_limit_status_lines,
    update_snapshots_conf_scrub_limit,
)
from btrfs.scrub_limit.ports import ScrubDropinPort, ScrubSettingsPort, ScrubSysfsPort, ScrubSystemdPort
from cst_core.domain.install import InstallReportWord
from cst_core.domain.values import MINUTE

# The longest a daemon-reload this run's own write did not ask for may take (systemd was behind, or
# could not say): a PID 1 that does not answer must not hang a run with nothing to change.
_CATCH_UP_RELOAD_TIMEOUT: Final = MINUTE


def _restore_conf(settings: ScrubSettingsPort, old_text: str | None, *, made_dir: bool) -> str | None:
    """snapshots.conf as it was before this run (removed when there was none); what failed, or None."""
    try:
        if old_text is not None:
            settings.write(old_text)
            return None
        settings.remove()
        if made_dir:
            settings.remove_folder_if_empty()
    except OSError as exc:
        return f"{settings.path}: {exc.strerror or exc}"
    return None


def _write_dropin(dropin: ScrubDropinPort, text: str | None) -> tuple[int, list[str], str]:
    """The drop-in through the installer: written with text, or removed (None)."""
    if text is None:
        return dropin.remove()
    return dropin.write(text)


def _restore_dropin(dropin: ScrubDropinPort, old_text: str | None) -> str | None:
    rc, _lines, err = _write_dropin(dropin, old_text)
    if rc != 0:
        return f"{dropin.path}: {err or f'installer exit {rc}'}"
    if old_text is None:
        dropin.remove_folder_if_empty()
    return None


def _rolled_back(cause: str, failures: list[str | None], *, reload_hint: bool) -> str:
    left = [f for f in failures if f is not None]
    if left:
        return f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(left)})"
    hint = " (run systemctl daemon-reload once it works)" if reload_hint else ""
    return f"ERROR: {cause}; previous configuration restored.{hint}"


def _read_conf_and_dropin(
    settings: ScrubSettingsPort, dropin: ScrubDropinPort
) -> tuple[str | None, str | None, str | None]:
    """snapshots.conf's and the drop-in's text as they are now, or the ERROR line of the first that
    cannot be read: (conf text, drop-in text, error)."""
    conf_text, conf_unreadable = settings.read()
    if conf_unreadable:
        return None, None, f"ERROR: cannot read {settings.path}"
    dropin_before, dropin_unreadable = dropin.read()
    if dropin_unreadable:
        return None, None, f"ERROR: cannot read {dropin.path}"
    return conf_text, dropin_before, None


def _conf_write_failed(settings: ScrubSettingsPort, conf_text: str | None, exc: OSError, *, made_dir: bool) -> str:
    """The ERROR line of a write of snapshots.conf that failed. A failure before its rename (a full disk)
    left the file as it was; one after it (the folder's sync) left the new text there, which goes back
    then. The folder this run made is removed again either way."""
    cause = f"cannot write {settings.path}: {exc.strerror or exc}"
    now, unreadable = settings.read()
    if not unreadable and now == conf_text:
        if made_dir:  # the write removed its temporary file: the folder is empty again
            settings.remove_folder_if_empty()
        return f"ERROR: {cause}"
    return _rolled_back(cause, [_restore_conf(settings, conf_text, made_dir=made_dir)], reload_hint=False)


def _apply_conf(
    settings: ScrubSettingsPort, conf_text: str | None, target_limit: str | None
) -> tuple[bool, bool, str, str | None]:
    """Write SCRUB_LIMIT into snapshots.conf when it changes: (changed, its folder was absent, the line,
    the ERROR line of a write that failed, with snapshots.conf as it was before)."""
    new_conf = update_snapshots_conf_scrub_limit(conf_text, target_limit)
    conf_changed = new_conf is not None and new_conf != conf_text
    made_dir = not settings.folder_exists()
    if new_conf is not None and conf_changed:
        try:
            settings.write(new_conf)
        except OSError as exc:
            return conf_changed, made_dir, "", _conf_write_failed(settings, conf_text, exc, made_dir=made_dir)
        verb = "written:" if conf_text is None else "updated:"
        note = f"SCRUB_LIMIT='{target_limit}'" if target_limit is not None else "removed SCRUB_LIMIT"
        return conf_changed, made_dir, f"{verb:<10} {settings.path} ({note})", None
    suffix = " (absent)" if conf_text is None else ""
    return conf_changed, made_dir, f"unchanged: {settings.path}{suffix}", None


def _reload_or_roll_back(
    systemd: ScrubSystemdPort,
    restore_files: Callable[[], list[str | None]],
    *,
    timeout: float | None,
) -> str | None:
    """systemctl daemon-reload (at most timeout seconds; None: no limit); when it fails, snapshots.conf and
    the drop-in go back (restore_files: what failed of each, the drop-in last) and the ERROR line comes
    back."""
    dr = systemd.daemon_reload(timeout=timeout)
    if dr.returncode == 0:
        return None
    return _rolled_back(dr.failure("systemctl daemon-reload"), restore_files(), reload_hint=True)


def _reload_what_is_on_disk(systemd: ScrubSystemdPort, *, behind: bool | None) -> str | None:
    """daemon-reload when only systemd's view was behind, or systemctl could not say (behind None; this
    run changed no file: nothing to roll back), at most _CATCH_UP_RELOAD_TIMEOUT; the ERROR line when it
    fails."""
    dr = systemd.daemon_reload(timeout=_CATCH_UP_RELOAD_TIMEOUT)
    if dr.returncode == 0:
        return None
    loaded = "systemd has not loaded them" if behind is True else "systemctl cannot say whether systemd has loaded them"
    return (
        f"ERROR: {dr.failure('systemctl daemon-reload')}; snapshots.conf and the drop-in are as asked, "
        f"{loaded} (run systemctl daemon-reload once it works)"
    )


def _load_in_systemd(
    systemd: ScrubSystemdPort,
    inst_lines: Sequence[str],
    reload_or_roll_back: Callable[[float | None], str | None],
    *,
    conf_changed: bool,
) -> tuple[str | None, list[str]]:
    """daemon-reload when this run changed the drop-in, or when systemd has not loaded it as it is on
    disk (an earlier run stopped before its daemon-reload), or systemctl cannot say (exit 0 must mean
    systemd loaded the limit): (the ERROR line or None, the lines to add)."""
    if any(line.startswith((InstallReportWord.WRITTEN, InstallReportWord.REMOVED)) for line in inst_lines):
        return reload_or_roll_back(None), []
    behind = systemd.need_daemon_reload()
    if behind is False:
        return None, []
    # This run changed no file but snapshots.conf, or none at all: then there is nothing to roll back.
    if conf_changed:
        error = reload_or_roll_back(_CATCH_UP_RELOAD_TIMEOUT)
    else:
        error = _reload_what_is_on_disk(systemd, behind=behind)
    why = (
        f"systemd had not loaded {SCRUB_SERVICE} as it is on disk (an earlier run stopped)"
        if behind is True
        else f"systemctl could not say whether systemd had loaded {SCRUB_SERVICE} as it is on disk"
    )
    return error, [f"reloaded:  {why}"]


@dataclass(frozen=True, slots=True)
class ScrubLimit:
    """status, plan and set|off of the scrub speed limit, over the machine's ports."""

    settings: ScrubSettingsPort
    dropin: ScrubDropinPort
    systemd: ScrubSystemdPort
    sysfs: ScrubSysfsPort

    def status(self) -> tuple[int, list[str]]:
        """Read-only status of SCRUB_LIMIT, the drop-in, sysfs, and whether systemd loaded the drop-in."""
        conf_text, conf_unreadable = self.settings.read()
        dropin_text, _ = self.dropin.read()
        lines = scrub_limit_status_lines(
            ScrubStatusSnapshot(
                conf_path=self.settings.path,
                conf_text=conf_text,
                conf_unreadable=conf_unreadable,
                dropin_path=self.dropin.path,
                dropin_text=dropin_text,
                live_sysfs=self.sysfs.live_limits(),
                need_reload=self.systemd.need_daemon_reload(),
            )
        )
        return (1 if conf_unreadable else 0), lines

    def plan(self, target_limit: str | None) -> tuple[int, list[str]]:
        """Read-only dry run of set|off, the daemon-reload included that apply runs when systemd has not
        loaded the drop-in as it is on disk."""
        conf_text, conf_unreadable = self.settings.read()
        if conf_unreadable:
            return 1, [f"ERROR: cannot read {self.settings.path}"]
        dropin_text, dropin_unreadable = self.dropin.read()
        if dropin_unreadable:
            return 1, [f"ERROR: cannot read {self.dropin.path}"]
        lines = scrub_limit_plan_lines(
            conf_path=self.settings.path,
            conf_text=conf_text,
            dropin_path=self.dropin.path,
            dropin_text=dropin_text,
            target_limit=target_limit,
        )
        need_reload = self.systemd.need_daemon_reload()
        return 0, [
            *lines,
            *scrub_limit_plan_reload_lines(dropin_text=dropin_text, target_limit=target_limit, need_reload=need_reload),
        ]

    def apply(self, target_limit: str | None) -> tuple[int, list[str]]:
        """Update SCRUB_LIMIT in snapshots.conf, update or remove the drop-in, and reload systemd if needed.

        When the installer or daemon-reload fails, snapshots.conf and the drop-in go back to what they were."""
        settings, dropin, systemd = self.settings, self.dropin, self.systemd
        conf_text, dropin_before, read_error = _read_conf_and_dropin(settings, dropin)
        if read_error is not None:
            return 1, [read_error]
        conf_changed, made_dir, conf_line, write_error = _apply_conf(settings, conf_text, target_limit)
        if write_error is not None:  # snapshots.conf as it was (or ROLLBACK INCOMPLETE), the drop-in not touched
            return 1, [write_error]
        out: list[str] = [conf_line]

        def restore_conf() -> str | None:
            return _restore_conf(settings, conf_text, made_dir=made_dir) if conf_changed else None

        rc_i, inst_lines, err_i = _write_dropin(
            dropin, render_scrub_dropin(target_limit) if target_limit is not None else None
        )
        out.extend(inst_lines)
        if rc_i != 0:
            cause = f"the installer failed: {err_i or f'exit {rc_i}'}"
            return rc_i, [*out, _rolled_back(cause, [restore_conf()], reload_hint=False)]

        if target_limit is None:
            dropin.remove_folder_if_empty()

        def restore_files() -> list[str | None]:
            return [restore_conf(), _restore_dropin(dropin, dropin_before)]

        def reload_or_roll_back(timeout: float | None) -> str | None:
            return _reload_or_roll_back(systemd, restore_files, timeout=timeout)

        error, note = _load_in_systemd(systemd, inst_lines, reload_or_roll_back, conf_changed=conf_changed)
        if error is not None:
            return 1, [*out, error]
        out.extend(note)
        return 0, out
