# SPDX-License-Identifier: MPL-2.0
"""What a run reads before it surveys: the settings, and the headsets of CST's LDAC rules."""

from collections.abc import Callable
from dataclasses import replace
from posixpath import basename, join

from bluetooth.domain import MARKER, set_keys, settings_report
from bluetooth.ports import BluetoothHostPort
from bluetooth.setup.domain import (
    Config,
    Exit,
    OLD_WP_MARKER,
    Paths,
    read_settings_output,
    rule_device,
)
from bluetooth.setup.ports import SetupFilesPort


def read_bluetooth_settings(
    host: BluetoothHostPort, paths: Paths
) -> tuple[dict[str, str], tuple[str, ...], str] | Exit:
    """(the merged settings, the keys the user's file sets, what to print on stderr). As
    bluetooth-setup.sh's two bt-helper calls (settings, then settings-keys), each names a file
    it cannot read: the first leaves the settings empty, the second (the user's file) ends the
    run with exit 1."""
    files: list[tuple[str, str]] = []
    user: str | None = None
    try:
        for path in (paths.default_settings, paths.user_settings):
            text = host.read_or_none(path)
            if text is not None:
                files.append((path, text))
        user = next((t for p, t in files if p == paths.user_settings), "")
        shown, said = settings_report(files)
    except (OSError, ValueError) as e:
        shown, said = "", f"bt-helper: {e}\n"
    if user is None:  # not read yet: settings-keys read it again
        try:
            user = host.read_or_none(paths.user_settings) or ""
        except (OSError, ValueError) as e:
            return Exit(1, err=f"{said}bt-helper: {e}\n")
    return read_settings_output(shown), tuple(set_keys(user)), said


def is_cst_rule(files: SetupFilesPort, path: str) -> bool:
    """The LDAC rule at path carries the marker of CST or of apply-le-audio.sh (its writers)."""
    return files.contains(path, MARKER, quiet=True) or files.contains(path, OLD_WP_MARKER, quiet=True)


def rule_headsets(files: SetupFilesPort, wp_dir: str) -> list[str]:
    """The headsets of the LDAC rules CST (or apply-le-audio.sh) wrote in wp_dir: its
    50-bluez-<ID>.conf files with one of their markers. Another program's file of that name, a .bak
    copy and a name that is not an address do not count."""
    try:
        names = files.list_dir(wp_dir)
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        device, path = rule_device(name), f"{wp_dir}/{name}"
        if device is None or not files.is_file(path):
            continue
        if is_cst_rule(files, path):
            found.append(device)
    return found


def shell_wp_stale(files: SetupFilesPort, paths: Paths, wp_conf: str, *, has_headset: bool) -> list[tuple[str, bool]]:
    """[(path, is_cst)] of WirePlumber files CST used to write under the shell's XDG_CONFIG_HOME
    when that differs from the user manager's config folder (none when identical)."""
    sdir = paths.shell_wp_dir
    if sdir is None:
        return []
    candidates: list[tuple[str, str, bool]] = []
    if has_headset:
        candidates.append((join(sdir, basename(wp_conf)), wp_conf, True))
        candidates.append((join(sdir, basename(paths.legacy_wp_conf)), paths.legacy_wp_conf, True))
    candidates.append((join(sdir, basename(paths.le_conf)), paths.le_conf, False))
    found: list[tuple[str, bool]] = []
    for path, cur, rule in candidates:
        if not files.is_file(path) or files.realpath(path) == files.realpath(cur):
            continue
        ours = is_cst_rule(files, path) if rule else files.contains(path, MARKER, quiet=True)
        found.append((path, ours))
    return found


def resolve_headset(
    files: SetupFilesPort, paths: Paths, cfg: Config, tilde: Callable[[str], str]
) -> tuple[Config, bool, tuple[str, ...]]:
    """Use CST's single LDAC rule when neither --device nor DEVICE in settings sets a headset."""
    if cfg.device or "DEVICE" in cfg.user_keys:
        return cfg, False, ()
    rules = rule_headsets(files, paths.wp_dir)
    if len(rules) == 1:
        updated = replace(
            cfg,
            device=rules[0],
            device_from=f"from CST's LDAC rule; apply saves it as DEVICE in {tilde(paths.user_settings)}",
        )
        return updated, True, ()
    return cfg, False, tuple(rules) if len(rules) > 1 else ()
