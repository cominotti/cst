# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-runtime's rules, as btrfs/btrfs-snapshot (bash) had them: the settings and their
defaults, btrfs-snapshot-core's command lines, and the texts of the journal lines and alerts. No I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

TAG: Final = "btrfs-snapshot"
GIB: Final = 1024**3
USAGE: Final = (
    "usage: btrfs-snapshot hourly|nas|convert|convert-boot|list [--json]|caches [--json]|prune [--dry-run] "
    "[--allow-mass-delete]|purge|planned-left|busy [--home DIR] PATH...|convert-one PATH"
)
NOT_SET_UP: Final = "not set up; run 'cst btrfs snapshots setup'"
EXIT_USAGE: Final = 2
# stat -c %i of a btrfs subvolume's root folder.
SUBVOL_ROOT_INODE: Final = "256"


class Stderr(StrEnum):
    """What a captured command's stderr does: shown on ours, or dropped (2>/dev/null)."""

    SHOW = "show"
    DROP = "drop"


# The settings snapshots.conf may set, with their defaults (strings, as bash holds them). The
# three space-guard numbers are also assignments at the top of btrfs/btrfs-snapshot, which
# btrfs-bulk reads from that file's text (a test checks the two agree).
DEFAULTS: Final[Mapping[str, str]] = {
    "HOME_DIR": "",
    "HOME_MOUNT": "/var/home",
    "HOME_SUBVOL": "home",
    "TOP": "/var/mnt/btrfs-top",
    "SNAP_DIR_NAME": "snapshots",
    "HOME_PRESERVE": "72h 7d 4w 3m",
    "WS_PRESERVE": "48h 7d",
    "EMERGENCY_PRESERVE": "24h 3d",
    "SNAPSHOT_EXCLUDE_CACHES": "yes",
    "SNAPSHOT_CACHES_SKIP": ".local/share/containers",
    "SKIP_AT_OR_BELOW_GIB": "250",
    "RESUME_ABOVE_GIB": "270",
    "EMERGENCY_AT_OR_BELOW_GIB": "150",
    "NAS_ENABLED": "0",
    "NAS_HOST": "",
    "NAS_PORT": "2204",
    "NAS_USER": "",
    "NAS_PATH": "",
    "NAS_RESTIC_HOST": "",
    "NAS_STALE_DAYS": "7",
    "NAS_TIMEOUT": "6h",
    "NAS_KEEP_DAILY": "14",
    "NAS_KEEP_WEEKLY": "8",
    "NAS_KEEP_MONTHLY": "12",
    "NAS_PRUNE_EVERY_DAYS": "7",
    "NAS_CHECK_EVERY_DAYS": "30",
    "NAS_CHECK_SUBSET": "5%",
}
GUARD_KEYS: Final = ("SKIP_AT_OR_BELOW_GIB", "RESUME_ABOVE_GIB", "EMERGENCY_AT_OR_BELOW_GIB")

# What the core names in its messages to review or confirm deletions: the commands of cst.
CORE_HINTS: Final = (
    "--list-hint",
    "cst btrfs snapshots list",
    "--confirm-hint",
    "cst btrfs snapshots prune --confirm",
)


def settings_problem_line(conf: str, number: int, text: str, reason: str) -> str:
    """The warning of a line of snapshots.conf that is not a setting (as the bash front writes it)."""
    return f"<4>{TAG}: {conf} line {number}: {text.strip()}: {reason}; ignored"


@dataclass(frozen=True, slots=True)
class Sources:
    """The snapshot sources: names (home, and Workspace once it is a subvolume), their paths below
    the top level, and HOME_DIR inside the home subvolume ("" when it is the subvolume's root)."""

    names: tuple[str, ...]
    paths: tuple[str, ...]
    home_rel: str


def sources(settings: Mapping[str, str], *, workspace_is_subvol: bool) -> Sources:
    """core_sources of the bash: ~/Workspace has snapshots of its own only once it is a subvolume."""
    rel = settings["HOME_DIR"].removeprefix(settings["HOME_MOUNT"]).removeprefix("/")
    home = f"{settings['TOP']}/{settings['HOME_SUBVOL']}"
    if not workspace_is_subvol:
        return Sources(("home",), (home,), rel)
    return Sources(("home", "Workspace"), (home, f"{home}{'/' + rel if rel else ''}/Workspace"), rel)


def source_args(src: Sources, home_policy: str, ws_policy: str) -> list[str]:
    """The core's --source arguments, NAME=PATH:POLICY."""
    out: list[str] = []
    for name, path in zip(src.names, src.paths, strict=True):
        out += ["--source", f"{name}={path}:{ws_policy if name == 'Workspace' else home_policy}"]
    return out


@dataclass(frozen=True, slots=True)
class CacheArgs:
    """The core's cache options: args (--exclude-caches when on, then the skips), skips (the
    --caches-skip pairs alone, for the listing) and the warnings on the way."""

    args: tuple[str, ...]
    skips: tuple[str, ...]
    warnings: tuple[str, ...]


def cache_args(src: Sources, exclude: str, skip_text: str) -> CacheArgs:
    """core_cache_args of the bash: SNAPSHOT_EXCLUDE_CACHES (yes, no, anything else: a warning and
    yes) and the SNAPSHOT_CACHES_SKIP paths, each given to the source that holds it."""
    args: list[str] = []
    warnings: list[str] = []
    if exclude == "yes":
        args.append("--exclude-caches")
    elif exclude != "no":
        warnings.append(f"SNAPSHOT_EXCLUDE_CACHES='{exclude}' is not yes or no; using yes")
        args.append("--exclude-caches")
    skips: list[str] = []
    # read -r -a: the first line only, words at blanks (space, tab).
    first = skip_text.split("\n", 1)[0]
    for word in [w for w in re.split(r"[ \t]+", first) if w]:
        rel = word.removeprefix("./").removesuffix("/")
        if not rel or rel.startswith("/") or "/../" in f"/{rel}/" or "/./" in f"/{rel}/":
            warnings.append(f"SNAPSHOT_CACHES_SKIP: '{rel}' is not a path inside your home; ignored")
            continue
        if "Workspace" in src.names and (rel == "Workspace" or rel.startswith("Workspace/")):
            rel = "Workspace/." if rel == "Workspace" else rel
            skips += ["--caches-skip", f"Workspace={rel.removeprefix('Workspace/')}"]
        else:
            skips += ["--caches-skip", f"home={src.home_rel + '/' if src.home_rel else ''}{rel}"]
    return CacheArgs((*args, *skips), tuple(skips), tuple(warnings))


def core_reason(lines: Sequence[str]) -> str:
    """CORE_REASON: the core's line that names a block if there is one, else its first warning or
    error (its <3>/<4> lines, without the prefix, summary lines left out)."""
    found: list[str] = []
    for line in lines:
        for level in ("<3>", "<4>"):
            prefix = f"{level}{TAG}: "
            if line.startswith(prefix):
                found.append(line[len(prefix) :])
                break
    found = [ln for ln in found if " summary: " not in ln]
    block = next(
        (ln for ln in found if re.search(r"blocked|not creating a snapshot|not deleting|circuit breaker", ln)), None
    )
    if block is not None:
        return block
    return found[0] if found else ""


def blocked_alert(reason: str, conf: str) -> tuple[str, str]:
    """(title, body) of the alert for the core's exit 4 (a safety check blocked something)."""
    title = "Snapshots: safety check blocked deletions"
    if "not creating a snapshot" in reason:
        title = "Snapshots: safety check blocked a new snapshot"
    hint = "review: cst btrfs snapshots list"
    # Only the circuit breaker can be overridden; a clock problem must be fixed.
    if "circuit breaker" in reason:
        hint += (
            f"; if those deletions are right (e.g. a shorter retention in {conf}): cst btrfs snapshots prune --confirm"
        )
    cut = reason.split("; check with ", 1)[0]
    return title, f"{cut[:300]} - {hint}; details: journalctl -u btrfs-snapshot."
