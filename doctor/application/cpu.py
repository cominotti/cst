# SPDX-License-Identifier: MPL-2.0
"""The cpu area of `cst doctor`: cpu.conf, the game profile's drift and leftover drop-ins, the user manager's
boxes outside CST's rules."""

from collections.abc import Iterator
from typing import Final

from cpu.game.domain import GameRecordKind, Record, intended_records, newer_intended
from doctor.application.shared import check, conf_value, read_text, settings_file_warnings
from doctor.domain import (
    CPU_CONF,
    GAME_DROPIN,
    GAME_DROPIN_DIR,
    GAME_INTENDED,
    GAME_MANUAL,
    GAME_PROFILE_HOST,
    GAME_STATE,
    INFO,
    OK,
    TRANSIENT,
    USER_KNOWN_SLICES,
    WARN,
    HostPath,
    Result,
    cpu_time,
    name_list,
    parse_cpu_stat_seconds,
    view_join,
    ViewPath,
    host_join,
)
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []

# getent's exit status for a key its database does not hold (getent(1)).
GETENT_NO_KEY: Final = 2


def names_no_account(host: DoctorHostPort, name: str) -> bool:
    """No account of the host is called name: getent passwd, run on the host (from a toolbox too, whose own
    passwd lacks the host's other accounts). False when getent cannot tell (missing, a timeout)."""
    rc, _, _ = host.run(["getent", "--", "passwd", name], timeout=20)
    return rc == GETENT_NO_KEY


def game_dropins_left(host: DoctorHostPort) -> list[str]:
    """Host paths of the game profile's MemoryLow drop-ins that exist now."""
    try:
        names = sorted(host.listdir(host.path(GAME_DROPIN_DIR)))
    except OSError:
        return []
    return [
        f"{GAME_DROPIN_DIR}/{n}/{GAME_DROPIN}"
        for n in names
        if n.endswith(".d") and host.isfile(host.path(host_join(GAME_DROPIN_DIR, n, GAME_DROPIN)))
    ]


def game_again(host: DoctorHostPort) -> str:
    """The command that switches the profile off and on again in its mode:
    'off' drops the by-hand mark, so after a restart of the unit alone the
    watchdog would switch a by-hand profile off when no game runs."""
    if host.exists(host.path(GAME_MANUAL)):
        return "cst cpu game off && cst cpu game on"
    return "sudo systemctl restart cpu-game.service"


@check(CHECKS, "cpu")
def check_cpu_settings(host: DoctorHostPort) -> Iterator[Result]:
    # Read with bash's rules (an older CST's bash copies sourced it), but no bash program of this CST does.
    yield from settings_file_warnings(host, CPU_CONF, bash_sourced=True, note=" (the game profile does not switch on)")
    target = conf_value(host, CPU_CONF, "TARGET_USER")
    if target is not None and target != "" and names_no_account(host, target):
        yield Result(
            WARN,
            f"{CPU_CONF}: TARGET_USER names no account: {target}. The game profile does not switch on: "
            "cpu-game.service fails when a game starts it, and the watchdog does not start it (its journal says "
            "so once per boot). Set TARGET_USER to your user (id -un)",
        )


def user_manager(host: DoctorHostPort) -> tuple[list[str] | None, str]:
    """(systemctl args for the target user's manager, or None, why not)."""
    target = conf_value(host, CPU_CONF, "TARGET_USER") or ""
    if host.uid == 0:
        if not target:
            return None, f"no TARGET_USER in {CPU_CONF}"
        return ["systemctl", "--user", "-M", f"{target}@"], ""
    me = host.user_name(host.uid)
    if target and me and target != me:
        return None, f"the profile is for {target}, this is {me}"
    return ["systemctl", "--user"], ""


def unit_props(host: DoctorHostPort, base: list[str], unit: str, props: list[str]) -> dict[str, str] | None:
    """{prop: value} of a loaded unit ("[not set]" as ""), or None when the
    manager cannot be asked or the unit is not loaded."""
    cmd = base + ["show", "-p", "LoadState"] + [a for p in props for a in ("-p", p)] + [unit]
    rc, out, _ = host.run(cmd, timeout=20)
    if rc != 0:
        return None
    vals: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k] = "" if v == "[not set]" else v
    if vals.get("LoadState") != "loaded":
        return None
    return vals


def _off_results(host: DoctorHostPort) -> Iterator[Result]:
    """The profile is off: its MemoryLow drop-ins left behind, or ok (when cpu.conf is there)."""
    left = game_dropins_left(host)
    if left:
        yield Result(
            WARN,
            f"game profile: off, but its MemoryLow drop-ins are still there: {', '.join(left)}. "
            "At every daemon-reload systemd applies them, so the game's memory protection comes "
            "back without a game (that memory is then harder to get for the VM and the builds) "
            "until the next reboot. The watchdog (cpu-game-watchdog.timer) removes them within "
            "30 s; if this stays, 'off' removes them and reloads systemd",
            f"sudo {GAME_PROFILE_HOST} off",
        )
    elif host.exists(host.path(CPU_CONF)):
        yield Result(OK, "game profile: off")


def _current_props(host: DoctorHostPort, records: list[Record]) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """({(manager, unit, property): value now} of the properties the records set, [the units that
    could not be asked, with why]): one systemctl show per unit, in the order of the records."""
    managers: dict[str, tuple[list[str] | None, str]] = {"sys": (["systemctl"], "")}
    managers["usr"] = user_manager(host)
    wanted: dict[tuple[str, str], list[str]] = {}
    for r in records:
        if r[0] == GameRecordKind.PROP and r[5]:
            wanted.setdefault((r[1], r[2]), []).append(r[3])
    current: dict[tuple[str, str, str], str] = {}
    unknown: list[str] = []
    for (mgr, unit), props in wanted.items():
        base, why = managers[mgr]
        vals = unit_props(host, base, unit, props) if base is not None and len(base) > 0 else None
        if vals is None:
            unknown.append(f"{unit} ({why or 'not loaded, or systemd did not answer'})")
            continue
        for p in props:
            current[mgr, unit, p] = vals.get(p, "")
    return current, unknown


def _record_drift(host: DoctorHostPort, r: Record, current: dict[tuple[str, str, str], str]) -> tuple[bool, str]:
    """(whether the record could be compared, what no longer holds: "" when it holds). A property is
    compared when systemd showed it; a cgroup file when it can still be read."""
    if r[0] == GameRecordKind.PROP:
        key, want = (r[1], r[2], r[3]), r[5]
        if not want or key not in current:
            return False, ""
        now = current[key]
        return True, "" if now == want else f"{r[2]} {r[3]} is {now or 'not set'} (the profile set {want})"
    if not r[3]:
        return False, ""
    try:
        with host.open_text(host.path(HostPath(r[1]))) as f:
            now = f.read(256).strip()
    except OSError:
        return False, ""  # the cgroup is gone: nothing to compare
    return True, "" if now == r[3] else f"{r[1]} is {now or 'empty'} (the profile wrote {r[3]})"


def _drifted(
    host: DoctorHostPort, records: list[Record], current: dict[tuple[str, str, str], str]
) -> tuple[list[str], int]:
    """([what no longer holds], how many records were compared), in the order of the records."""
    drifted: list[str] = []
    checked = 0
    for r in records:
        compared, drift = _record_drift(host, r, current)
        checked += compared
        if drift:
            drifted.append(drift)
    return drifted, checked


@check(CHECKS, "cpu")
def check_game_profile_drift(host: DoctorHostPort) -> Iterator[Result]:
    """WARN while the game profile is on and a setting it made no longer
    holds; ok when they all hold; info when it cannot tell."""
    if not host.exists(host.path(GAME_STATE)):
        yield from _off_results(host)
        return
    try:
        with host.open_text(host.path(GAME_INTENDED)) as f:
            text = f.read(65536)
    except FileNotFoundError:
        yield Result(
            INFO,
            "game profile: ON, but switched on by an older cpu-game-profile that kept no "
            "record of the values it set, so drift is not checked. Switching it off and on again "
            f"records them: {game_again(host)}",
        )
        return
    except OSError as e:
        yield Result(INFO, f"game profile: ON; drift not checked ({GAME_INTENDED}: {e.strerror})")
        return
    newer = newer_intended(GAME_INTENDED, text)
    if newer is not None:
        yield Result(INFO, f"game profile: ON; drift not checked ({newer})")
        return
    records = intended_records(text)
    current, unknown = _current_props(host, records)
    drifted, checked = _drifted(host, records, current)
    if drifted:
        yield Result(
            WARN,
            f"game profile: ON, but {len(drifted)} of its settings no longer hold: "
            f"{'; '.join(drifted)}. A daemon-reload or uresourced undid them. The watchdog "
            "(cpu-game-watchdog.timer) sets them again within 30 s; if this stays, switch the "
            "profile off and on again",
            game_again(host),
        )
    elif checked:
        yield Result(OK, f"game profile: ON, the {checked} settings it made still hold")
    if unknown:
        yield Result(INFO, f"game profile: drift not checked for {', '.join(unknown)}")
    if not drifted and not checked and not unknown:
        yield Result(INFO, "game profile: ON, no recorded setting to compare")


def user_manager_cgroup(host: DoctorHostPort) -> HostPath | None:
    """The cgroup folder of the user manager to look at (the doctor's own
    user, or TARGET_USER when run as root), or None."""
    uid = host.uid
    if uid == 0:
        target = conf_value(host, CPU_CONF, "TARGET_USER")
        if target is None or target == "":
            return None
        found = host.user_id(target)
        if found is None:
            return None
        uid = found
    return HostPath(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")


def cpu_seconds(host: DoctorHostPort, path: ViewPath) -> float | None:
    """CPU time a cgroup used since it was created (cpu.stat usage_usec),
    in seconds, or None."""
    return parse_cpu_stat_seconds(read_text(host, view_join(path, "cpu.stat"), 4096))


def cgroup_units(host: DoctorHostPort, path: ViewPath) -> list[tuple[str, ViewPath]]:
    """[(unit, cgroup folder)] of the child cgroups of PATH; systemd puts a
    '_' before a name that could clash with a controller file (cpu.slice ->
    _cpu.slice)."""
    try:
        names = sorted(host.listdir(path))
    except OSError:
        return []
    return [(n.removeprefix("_"), view_join(path, n)) for n in names if host.isdir(view_join(path, n))]


@check(CHECKS, "cpu")
def check_user_manager_boxes(host: DoctorHostPort) -> Iterator[Result]:
    """WARN for each top-level slice of the user manager that CST does not
    know (it competes with the game's app.slice at the default weight, on
    every CPU); info for systemd-run jobs straight in app.slice. Reads
    /sys/fs/cgroup only; nothing when there is no user manager to look at."""
    rel = user_manager_cgroup(host)
    if rel is None or rel == "" or not host.isdir(host.path(rel)):
        return
    base = host.path(rel)
    loose = [(u, d) for u, d in cgroup_units(host, base) if u.endswith(".slice") and u not in USER_KNOWN_SLICES]
    for unit, d in loose:
        name = unit[: -len(".slice")]
        yield Result(
            WARN,
            f"user manager: {unit} is a slice of its own next to app.slice, where the game runs "
            f"({cpu_time(cpu_seconds(host, d))} since it was created). Outside games it runs at the default "
            "weight on every CPU, without the toolboxes' limits; the game profile only makes it idle "
            f"while a game runs. Name it user-{name}.slice so it nests under the toolboxes' user.slice, or "
            f"start it with --slice=user-{name}.slice",
        )
    jobs = [
        (u, cpu_seconds(host, d)) for u, d in cgroup_units(host, view_join(base, "app.slice")) if TRANSIENT.match(u)
    ]
    if jobs:
        jobs.sort(key=lambda j: -(j[1] or 0))
        yield Result(
            INFO,
            f"user manager: {len(jobs)} systemd-run job(s) run straight in app.slice, next to the "
            f"game, at the default weight on every CPU: "
            f"{name_list([f'{u} ({cpu_time(s)})' for u, s in jobs])}. The game profile makes them idle "
            "while a game runs; for builds, start them with --slice=user-<name>.slice to get the "
            "toolboxes' limits always",
        )
    if not loose and not jobs:
        yield Result(OK, "user manager: no slices or systemd-run jobs outside the boxes CST manages")
