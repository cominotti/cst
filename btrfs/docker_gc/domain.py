# SPDX-License-Identifier: MPL-2.0
"""docker-gc's rules, no I/O: Docker's generated names, its time stamps, which containers are
ephemeral, what a run removes (the plan) and the lines that show it."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import os
import re
from typing import Final

from cst_core.domain.contracts import StateFormat, StateFormatKey, state_format, version_of
from cst_core.domain.text import describe_problems, parse_shell_assignments
from cst_core.domain.values import DAY, MINUTE, format_bytes_decimal

# The recorder's state and the last run's summary (the files of GcPaths).
STATE_FORMAT: StateFormat = state_format(StateFormatKey.DOCKER_GC_STATE)
LAST_RUN_FORMAT: StateFormat = state_format(StateFormatKey.DOCKER_GC_LAST_RUN)


class GcCommand(StrEnum):
    """docker-gc's commands (its first argument), in its usage's order."""

    RECORD = "record"
    RUN = "run"
    CLEANUP_NOW = "cleanup-now"
    STATUS = "status"
    SETTINGS = "settings"


class NewerState(RuntimeError):
    """A state file of a newer docker-gc: nothing is recorded or removed with it, and it is left as it is."""


# The exit status of a command that met a NewerState. docker-gc-recorder.service names it in
# RestartPreventExitStatus=: the recorder then stays failed (cst doctor lists it) instead of being
# started again every 10 s to read the same file. Not 1 (any other error), not 2 (usage); record
# passes on the status `docker events` ends with (the docker CLI uses 1 for its errors).
# 65: EX_DATAERR of sysexits.h.
NEWER_STATE_EXIT = 65


def stored_version(fmt: StateFormat, path: str, data: object) -> int | None:
    """The format of a state file as read ("v"; none: 0, an older docker-gc wrote it), None when it is not one
    docker-gc wrote. Raises NewerState for a newer format than fmt's."""
    if (found := fmt.newer(data, "v")) is not None:
        raise NewerState(
            f"{fmt.newer_text(path, found)}: nothing is recorded or removed with it, and it is left as it is; "
            "install the docker-gc of that CST (cst btrfs docker-gc enable in that checkout)"
        )
    return version_of(data, "v")


HEARTBEAT_SECONDS: Final = 5 * MINUTE
MIN_UPTIME: Final = 10 * MINUTE  # after boot, restart-policy containers are still coming up
DOCKER_TIMEOUT: Final = 10 * MINUTE  # seconds a docker command may take (every call but the event's inspect)
RECENT_USE: Final = MINUTE  # an image used this long before the plan was made counts as used meanwhile
# What a failed `docker info` prints when nothing answers on the daemon's socket:
# the socket is not there or nobody listens (the errno text of the dial, in every
# version), or the question older CLIs print instead. Anything else (permission
# denied, an SELinux denial, a broken daemon) is an error, never "not running".
DAEMON_DOWN_SIGNS = (
    "connect: no such file or directory",
    "connect: connection refused",
    "Is the docker daemon running?",
)
# Containers that belong to something the user manages as a unit.
KEEP_LABEL_PREFIXES = (
    "com.docker.compose.",
    "k3d.",
    "io.x-k8s.kind.",
    "dev.containers.",
    "devcontainer.",
    "com.docker.desktop.",
)
TESTCONTAINERS_LABEL = "org.testcontainers"
# Docker's generated names (moby pkg/namesgenerator, v28.5.2): left_right, plus
# one digit after a collision. A user's own "redis_cache" matches neither list.
NAMES_LEFT = frozenset(
    """
admiring adoring affectionate agitated amazing angry awesome beautiful blissful bold
boring brave busy charming clever compassionate competent condescending confident cool
cranky crazy dazzling determined distracted dreamy eager ecstatic elastic elated elegant
eloquent epic exciting fervent festive flamboyant focused friendly frosty funny gallant
gifted goofy gracious great happy hardcore heuristic hopeful hungry infallible inspiring
intelligent interesting jolly jovial keen kind laughing loving lucid magical modest
musing mystifying naughty nervous nice nifty nostalgic objective optimistic peaceful
pedantic pensive practical priceless quirky quizzical recursing relaxed reverent
romantic sad serene sharp silly sleepy stoic strange stupefied suspicious sweet tender
thirsty trusting unruffled upbeat vibrant vigilant vigorous wizardly wonderful
xenodochial youthful zealous zen
""".split()
)
NAMES_RIGHT = frozenset(
    """
agnesi albattani allen almeida antonelli archimedes ardinghelli aryabhata austin babbage
banach banzai bardeen bartik bassi beaver bell benz bhabha bhaskara black blackburn
blackwell bohr booth borg bose bouman boyd brahmagupta brattain brown buck burnell
cannon carson cartwright carver cerf chandrasekhar chaplygin chatelet chatterjee chaum
chebyshev clarke cohen colden cori cray curie curran darwin davinci dewdney dhawan
diffie dijkstra dirac driscoll dubinsky easley edison einstein elbakyan elgamal elion
ellis engelbart euclid euler faraday feistel fermat fermi feynman franklin gagarin
galileo galois ganguly gates gauss germain goldberg goldstine goldwasser golick goodall
gould greider grothendieck haibt hamilton haslett hawking heisenberg hellman hermann
herschel hertz heyrovsky hodgkin hofstadter hoover hopper hugle hypatia ishizaka jackson
jang jemison jennings jepsen johnson joliot jones kalam kapitsa kare keldysh keller
kepler khayyam khorana kilby kirch knuth kowalevski lalande lamarr lamport leakey
leavitt lederberg lehmann lewin lichterman liskov lovelace lumiere mahavira margulis
matsumoto maxwell mayer mccarthy mcclintock mclaren mclean mcnulty meitner mendel
mendeleev meninsky merkle mestorf mirzakhani montalcini moore morse moser murdock napier
nash neumann newton nightingale nobel noether northcutt noyce panini pare pascal pasteur
payne perlman pike poincare poitras proskuriakova ptolemy raman ramanujan rhodes ride
ritchie robinson roentgen rosalind rubin saha sammet sanderson satoshi shamir shannon
shaw shirley shockley shtern sinoussi snyder solomon spence stonebraker sutherland
swanson swartz swirles taussig tesla tharp thompson torvalds tu turing varahamihira
vaughan villani visvesvaraya volhard wescoff wilbur wiles williams williamson wilson
wing wozniak wright wu yalow yonath zhukovsky
""".split()
)


@dataclass(frozen=True, slots=True)
class GcSettings:
    """What a run keeps, from docker-gc's environment (the defaults are the unit's)."""

    keep_container_days: int = 7
    keep_image_days: int = 14
    keep_build_cache_days: int = 14
    # Registry prefixes ("host/") whose images only cleanup-now removes: DOCKER_GC_PROTECT in the
    # settings file (resolve_protection); none by default.
    protected: tuple[str, ...] = ()
    # How often the recorder writes what it saw (seconds).
    flush_seconds: int = 30


@dataclass(frozen=True, slots=True)
class GcPaths:
    """Where docker-gc keeps its state (StateDirectory) and its run lock (RuntimeDirectory)."""

    state_dir: str = STATE_FORMAT.folder
    # In docker-gc.service's RuntimeDirectory= (kept after the run): with the state folder, the
    # places the sandboxed unit may write. Shared with runs outside it (cleanup-now,
    # run --dry-run), which make the folder; a tmpfs, so they leave no file in the state folder.
    run_dir: str = "/run/docker-gc"
    # The settings file (SETTINGS_KEYS), and the installed program (the manifest's dest of
    # btrfs/docker-gc): while the settings file is missing, what it protects counts.
    settings_file: str = "/etc/cst/docker-gc.conf"
    installed: str = "/usr/local/sbin/docker-gc"

    @property
    def last_used(self) -> str:
        return os.path.join(self.state_dir, STATE_FORMAT.file_name)

    @property
    def last_run(self) -> str:
        return os.path.join(self.state_dir, LAST_RUN_FORMAT.file_name)

    @property
    def lock(self) -> str:
        return os.path.join(self.run_dir, "run.lock")


def protected_registries(value: str) -> tuple[str, ...]:
    """DOCKER_GC_PROTECT (comma-separated registries) as the prefixes a reference starts with."""
    return tuple(r.strip() + "/" for r in value.split(",") if r.strip())


# ---- inside a toolbox or container ------------------------------------------------------
def container_refusal(
    command: GcCommand, *, dry: bool, write: bool, settings_file: str, named_files: bool
) -> str | None:
    """Why docker-gc does not run this command inside a toolbox or container, or None.

    There /etc/cst/docker-gc.conf and the installed program are the container's (usually
    missing), while docker reaches the host's engine through the shared socket: the host's
    protected images would count as unprotected. So every command that talks to docker
    refuses, the read-only ones too (status and a dry run would list those images as
    removable). settings, which talks to no docker, runs only when the caller names both of
    its files (DOCKER_GC_CONF and DOCKER_GC_INSTALLED: the installer's staged step does).
    """
    if command == GcCommand.SETTINGS and named_files:
        return None
    words = " ".join([command, *(["--dry-run"] if dry else []), *(["--write"] if write else [])])
    on_host = (
        f"sudo {GcPaths().installed} {words}"
        if command in (GcCommand.RECORD, GcCommand.SETTINGS)
        else f"cst btrfs docker-gc {words}"
    )
    return (
        f"docker-gc {words}: refused inside a toolbox or container: {settings_file} there is the "
        "container's, not the host's, while docker reaches the host's engine, so the host's "
        f"protected images would count as unprotected. Run it on the host: {on_host}"
    )


# ---- the settings file: which registries are protected ------------------------------
# The exit status of a command that cannot tell which registries are protected (a settings file
# it cannot read, or no file and an installed docker-gc it does not know): it removes nothing.
# 78: EX_CONFIG of sysexits.h.
BAD_SETTINGS_EXIT = 78
SETTINGS_KEYS = ("DOCKER_GC_PROTECT",)
# A registry as an image reference starts with it: a host (a name or an address, a port), and
# optionally the first parts of a repository path ("ghcr.io/team").
# The path parts follow the reference grammar of the distribution spec: lower-case letters and digits,
# separated by ".", "_", "__" or a run of "-".
REGISTRY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
)


class UnknownProtection(RuntimeError):
    """docker-gc cannot tell which registries are protected: it removes nothing, and says why."""


@dataclass(frozen=True, slots=True)
class Protection:
    """The protected registries ("host" or "host/path", without the trailing "/"), where they come
    from, and what the settings file gets when it is missing (None: it is there)."""

    registries: tuple[str, ...]
    source: str
    to_write: tuple[str, ...] | None = None

    @property
    def prefixes(self) -> tuple[str, ...]:
        """As GcSettings.protected holds them."""
        return protected_registries(",".join(self.registries))


def registries_of(value: str) -> tuple[str, ...]:
    """DOCKER_GC_PROTECT's registries, blanks and empty items dropped, in order."""
    return tuple(r.strip().removesuffix("/") for r in value.split(",") if r.strip())


def parse_settings_file(path: str, text: str) -> tuple[str, ...]:
    """The protected registries of the settings file at path (its text). Strict: a line that is not
    an assignment, an unknown key or a value that is not a list of registries raises UnknownProtection
    with every such line; nothing is guessed. No DOCKER_GC_PROTECT line: none protected."""
    parsed = parse_shell_assignments(text, bash_sourced=False)
    problems = describe_problems(path, parsed.problems).splitlines()
    for a in parsed.entries:
        if a.key not in SETTINGS_KEYS:
            problems.append(f"{path} line {a.line}: {a.key}: unknown setting (known: {', '.join(SETTINGS_KEYS)})")
        elif bad := [r for r in registries_of(a.value) if not REGISTRY_RE.fullmatch(r)]:
            problems.append(f"{path} line {a.line}: {a.key}: not a registry: {', '.join(bad)}")
    if problems:
        raise UnknownProtection("\n".join(problems) + f"\nnothing is removed until {path} is fixed")
    return registries_of(parsed.values.get("DOCKER_GC_PROTECT", ""))


def render_settings_file(registries: Sequence[str]) -> str:
    """The settings file as docker-gc writes it when it is missing (`docker-gc settings --write`)."""
    return (
        "# docker-gc settings (CST): KEY=VALUE lines, '#' starts a comment. Read by docker-gc\n"
        "# (/usr/local/sbin/docker-gc and `cst btrfs docker-gc`); a line it cannot read stops its runs\n"
        '# and cleanups (they remove nothing) until it is fixed. btrfs/README.md in CST: "docker-gc".\n'
        "#\n"
        "# DOCKER_GC_PROTECT: registries, comma-separated (registry.example.com,ghcr.io/team), whose\n"
        "# images a weekly run never removes; cleanup-now removes them only after a second question.\n"
        "# Empty: no registry is protected (images in use or used lately are kept anyway).\n"
        f"DOCKER_GC_PROTECT={','.join(registries)}\n"
    )


def _literal(node: ast.expr | None) -> object:
    try:
        return None if node is None else ast.literal_eval(node)
    except ValueError, TypeError:  # TypeError: an unhashable item of a set or dict literal
        return None


def builtin_protected(sources: Sequence[str]) -> tuple[str, ...] | None:
    """The registries a docker-gc protects with no DOCKER_GC_PROTECT anywhere, read from its Python
    sources without running them: the default of GcSettings.protected (CST from 2026-11-16), else
    the default of a `.get("DOCKER_GC_PROTECT", "...")` (the older ones). None: neither is there
    (not a docker-gc CST wrote, or a source that does not parse or evaluate)."""
    env_default: tuple[str, ...] | None = None
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError, ValueError, MemoryError:  # MemoryError: the parser's nesting limit
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "GcSettings":
                for item in node.body:
                    if (
                        isinstance(item, ast.AnnAssign)
                        and isinstance(item.target, ast.Name)
                        and item.target.id == "protected"
                        and isinstance(value := _literal(item.value), tuple)
                        and all(isinstance(v, str) for v in value)
                    ):
                        return registries_of(",".join(value))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) == 2
                and _literal(node.args[0]) == "DOCKER_GC_PROTECT"
                and isinstance(default := _literal(node.args[1]), str)
            ):
                env_default = registries_of(default)
    return env_default


def resolve_protection(
    paths: GcPaths, *, env: str | None, file_text: str | None, installed: Sequence[str] | None
) -> Protection:
    """Which registries are protected. DOCKER_GC_PROTECT in the environment (tests, a drop-in of the
    unit) wins; else the settings file (file_text; None: missing); while it is missing, what the
    installed docker-gc protects (installed: its sources; None: no program installed): an older one
    had a registry built in, and the file keeps it once `docker-gc settings --write` writes it.
    Raises UnknownProtection for a file it cannot read, and for a missing file with an installed
    program that has no list of its own (this docker-gc, which reads the file, or one it does not
    know): never "none" by mistake."""
    path, prog = paths.settings_file, paths.installed
    if file_text is not None:
        base = Protection(parse_settings_file(path, file_text), f"DOCKER_GC_PROTECT in {path}")
    elif installed is None:
        base = Protection((), f"no {path}, and no docker-gc installed", to_write=())
    elif (built_in := builtin_protected(installed)) is None or len(built_in) == 0:
        # Fail closed. Only an older docker-gc has a list of its own, never an empty one; this one
        # (an empty default) reads it from the file, so the file being gone is no "none".
        what = "reads them from that file" if built_in == () else "does not show which registries it protects"
        raise UnknownProtection(
            f"{path} is missing, and the installed {prog} {what}: write {path} with "
            f"DOCKER_GC_PROTECT=<registries, comma-separated> (DOCKER_GC_PROTECT= for none); "
            "nothing is removed until then"
        )
    else:
        base = Protection(
            built_in,
            f"built into the installed {prog}, while {path} is missing ('cst btrfs docker-gc enable' writes them there)",
            to_write=built_in,
        )
    if env is None:
        return base
    return Protection(registries_of(env), "DOCKER_GC_PROTECT in the environment", to_write=base.to_write)


def protection_line(p: Protection) -> str:
    """The first line of status and `docker-gc settings`."""
    return f"protected registries: {', '.join(p.registries) or 'none'} ({p.source})"


@dataclass(frozen=True, slots=True)
class Container:
    """What a plan needs of `docker container inspect`."""

    id: str
    name: str  # without Docker's leading "/"
    image_id: str
    image_ref: str = "?"  # Config.Image, as the container was created
    status: str = ""
    running: bool = False
    started: float = 0  # epoch seconds, 0 if unset
    finished: float = 0
    created: float = 0
    label_keys: frozenset[str] = frozenset()
    restart: str = "no"
    volumes: frozenset[str] = frozenset()  # names of the volumes it mounts

    def last_use(self, now: float) -> float:
        """Last moment the container was using its image (now while it runs)."""
        if self.running:
            return now
        return max(self.started, self.finished, self.created)


@dataclass(frozen=True, slots=True)
class Volume:
    name: str
    anonymous: bool = False  # Docker made it for one container (it has no name of its own)


@dataclass(frozen=True, slots=True)
class Image:
    id: str
    tags: tuple[str, ...] = ()
    digests: tuple[str, ...] = ()
    size: int = 0
    last_tag_time: float = 0  # the local pull, build or tag


@dataclass(frozen=True, slots=True)
class DockerEvent:
    """One line of `docker events`: its type, action, the image attribute and the actor's ID."""

    kind: str
    action: str
    image_attr: str = ""
    actor_id: str = ""


@dataclass(frozen=True, slots=True)
class EventsEnd:
    """How `docker events` ended: stopped on request (SIGTERM), and its exit status."""

    stopped: bool
    returncode: int


@dataclass(frozen=True, slots=True)
class GcState:
    """The recorder's state (last-used.json): since when uses are recorded, the last use of each image, its heartbeat."""

    since: float
    images: Mapping[str, float] = field(default_factory=dict)
    heartbeat: float = 0


@dataclass(frozen=True, slots=True)
class ContainerDrop:
    """An ephemeral container a run removes: its ID, name, image reference and when it stopped."""

    id: str
    name: str
    image: str = "?"
    ended: float = 0


@dataclass(frozen=True, slots=True)
class ImageDrop:
    """An unused image a run removes: when it was last used, and whether its registry is protected."""

    id: str
    protected: bool  # no default: a protected image must never pass as an ordinary one
    tags: tuple[str, ...] = ()
    size: int = 0
    used: float = 0


@dataclass(frozen=True, slots=True)
class GcPlan:
    """What a run removes, planned at `at`; held: images kept back (protected, or no recorder)."""

    containers: tuple[ContainerDrop, ...]
    volumes: tuple[str, ...]
    images: tuple[ImageDrop, ...]
    recorder_ok: bool  # no default: images are held back unless the recorder is known to run
    at: float
    held: int = 0

    def empty(self) -> bool:
        return not (self.containers or self.volumes or self.images)


@dataclass(frozen=True, slots=True)
class RunSummary:
    """last-run.json: what the last weekly run removed (status shows it)."""

    at: float
    containers: int = 0
    volumes: int = 0
    images: int = 0
    image_bytes: int = 0
    failed: int = 0
    before: Mapping[str, str] = field(default_factory=dict)
    after: Mapping[str, str] = field(default_factory=dict)


def auto_named(name: str) -> bool:
    m = re.fullmatch(r"([a-z]+)_([a-z]+)\d?", name, re.ASCII)
    # The generator never returns boring_wozniak: that one was chosen by hand.
    return bool(m and m.group(1) in NAMES_LEFT and m.group(2) in NAMES_RIGHT and not name.startswith("boring_wozniak"))


def ts(value: str | None) -> float:
    """RFC 3339 from Docker (nanoseconds, Z) -> epoch seconds; 0 if unset."""
    if value is None or value == "" or not isinstance(value, str) or value.startswith("0001-"):
        return 0
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", value)
    if not m:
        return 0
    base, frac, tz = m.groups()
    tz = "+00:00" if tz == "Z" else tz
    return datetime.fromisoformat(f"{base}.{(frac or '0')[:6].ljust(6, '0')}{tz}").timestamp()


def day(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%F") if t else "never"


def ephemeral(c: Container) -> bool:
    return (
        (auto_named(c.name) or TESTCONTAINERS_LABEL in c.label_keys)
        and not any(k.startswith(KEEP_LABEL_PREFIXES) for k in c.label_keys)
        and c.restart == "no"
    )


def is_protected(image: Image, protected: Sequence[str]) -> bool:
    """An image of a protected registry, by tag or digest."""
    prefixes = tuple(protected)
    return any(r.startswith(prefixes) for r in (*image.tags, *image.digests))


def event_image(event: DockerEvent) -> str:
    """The image reference a use event names ("" for any other event)."""
    if event.kind == "container" and event.action in ("create", "start", "die", "stop"):
        return event.image_attr
    if event.kind == "image" and event.action in ("pull", "tag", "load", "import", "create", "push"):
        return event.actor_id
    return ""


def recorder_down_too_long(state: GcState, now: float, booted: float) -> bool:
    """The recorder missed uses: down a while with Docker running (not only a reboot, when Docker was down too)."""
    return bool(state.heartbeat) and now - max(state.heartbeat, booted) > 3 * HEARTBEAT_SECONDS


def heartbeat_fresh(state: GcState, now: float) -> bool:
    return now - state.heartbeat < 3 * HEARTBEAT_SECONDS


@dataclass(frozen=True, slots=True)
class _ContainerSplit:
    drops: tuple[ContainerDrop, ...]
    keep_images: frozenset[str]
    kept_volumes: frozenset[str]
    used_by: Mapping[str, float]


def _split_containers(containers: Sequence[Container], now: float, settings: GcSettings) -> _ContainerSplit:
    """Ephemeral containers stopped long enough go; every other one keeps its image and volumes."""
    used_by: dict[str, float] = {}
    drops: list[ContainerDrop] = []
    keep_images: set[str] = set()
    kept_vols: set[str] = set()
    for c in containers:
        used_by[c.image_id] = max(used_by.get(c.image_id, 0), c.last_use(now))
        ended = c.finished or c.created
        stopped = c.status in ("exited", "created", "dead")
        if stopped and ephemeral(c) and ended and now - ended >= settings.keep_container_days * DAY:
            drops.append(ContainerDrop(id=c.id, name=c.name, image=c.image_ref, ended=ended))
        else:
            keep_images.add(c.image_id)
            kept_vols |= c.volumes
    return _ContainerSplit(tuple(drops), frozenset(keep_images), frozenset(kept_vols), used_by)


@dataclass(frozen=True, slots=True)
class PlanInputs:
    """What docker and the recorder say when a plan is made."""

    containers: Sequence[Container]
    volumes: Sequence[Volume]
    images: Sequence[Image]
    state: GcState
    recorder_ok: bool


def plan_gc(inputs: PlanInputs, settings: GcSettings, *, now: float, timed: bool) -> GcPlan:
    """What to remove. timed=True: the weekly run (the recorder's start counts as a use;
    protected registries never; no images without the recorder). timed=False: cleanup-now."""
    split = _split_containers(inputs.containers, now, settings)
    # Anonymous volumes no remaining container uses: today's orphans plus
    # those of the containers removed. Named volumes never.
    drop_v = tuple(sorted(v.name for v in inputs.volumes if v.anonymous and v.name not in split.kept_volumes))
    drop_i: list[ImageDrop] = []
    held = 0
    for im in inputs.images:
        if im.id in split.keep_images:
            continue
        # Uses: recorded events, containers (also those removed now: until
        # they stopped), and the local tag time (pull, build, tag).
        used = max(inputs.state.images.get(im.id, 0), split.used_by.get(im.id, 0), im.last_tag_time)
        if timed:
            used = max(used, inputs.state.since)
        if now - used < settings.keep_image_days * DAY:
            continue
        prot = is_protected(im, settings.protected)
        if timed and (prot or not inputs.recorder_ok):
            held += 1
            continue
        drop_i.append(ImageDrop(id=im.id, tags=im.tags, size=im.size, used=used, protected=prot))
    return GcPlan(split.drops, drop_v, tuple(drop_i), recorder_ok=inputs.recorder_ok, at=now, held=held)


# How many containers, volumes and images a plan lists when it is not shown in full.
SHOWN_CONTAINERS, SHOWN_VOLUMES, SHOWN_IMAGES = 10, 3, 15


def plan_lines(p: GcPlan, settings: GcSettings, *, full: bool = False) -> list[str]:
    """The plan as docker-gc shows it (status, a run, cleanup-now); full: no list cut short."""
    now = p.at
    lc, lv, li = (None, None, None) if full else (SHOWN_CONTAINERS, SHOWN_VOLUMES, SHOWN_IMAGES)
    out = [f"ephemeral containers stopped for {settings.keep_container_days}+ days: {len(p.containers)}"]
    out += [
        f"  {c.name:32} {c.image[:60]}  (stopped {int((now - c.ended) // DAY)} days ago)" for c in p.containers[:lc]
    ]
    out.append(f"orphaned anonymous volumes: {len(p.volumes)}")
    out += [f"  {v}" for v in p.volumes[:lv]]
    total = sum(i.size for i in p.images)
    # Per-image sizes overlap (shared base layers are counted for each image).
    out.append(f"unused images: {len(p.images)}, up to {format_bytes_decimal(total)} (shared layers counted per image)")
    for i in sorted(p.images, key=lambda x: -x.size)[:li]:
        name = i.tags[0] if i.tags else "<untagged> " + i.id[7:19]
        mark = "  [protected registry]" if i.protected else ""
        out.append(f"  {format_bytes_decimal(i.size):>9}  {name[:80]}  (last use {day(i.used)}){mark}")
    if not full and (
        len(p.containers) > SHOWN_CONTAINERS or len(p.volumes) > SHOWN_VOLUMES or len(p.images) > SHOWN_IMAGES
    ):
        out.append("  (lists truncated; 'docker-gc cleanup-now' shows everything before asking)")
    if p.held:
        why = "the recorder is not running" if not p.recorder_ok else "protected registry: cleanup-now only"
        out.append(f"held back: {p.held} image(s) ({why})")
    return out


NOT_RECORDED = "image use not recorded yet (recorder not started)"


def recorded_line(state: GcState, settings: GcSettings, *, recorder_ok: bool) -> str:
    return (
        f"image use recorded since {datetime.fromtimestamp(state.since).strftime('%F %T')}"
        f" ({len(state.images)} image(s) seen); timed image removal possible from "
        f"{day(state.since + settings.keep_image_days * DAY)}; recorder "
        f"{'ok' if recorder_ok else 'NOT running (images are held back)'}"
    )


def last_run_line(summary: RunSummary | None) -> str:
    if summary is None:
        return "last run: never"
    return (
        f"last run {datetime.fromtimestamp(summary.at).strftime('%F %T')}: {summary.containers} container(s), "
        f"{summary.volumes} volume(s), {summary.images} image(s) ({format_bytes_decimal(summary.image_bytes)}), "
        f"failures {summary.failed}"
    )


def run_log_line(summary: RunSummary) -> str:
    """The journal line of a weekly run."""
    return (
        f"removed {summary.containers} container(s), {summary.volumes} anonymous volume(s), "
        f"{summary.images} image(s) planned ({format_bytes_decimal(summary.image_bytes)}), old build cache; "
        f"failures {summary.failed}; images {summary.before.get('Images', '?')} -> {summary.after.get('Images', '?')}"
    )


def daemon_down(stderr: str) -> bool:
    """A failed `docker info`'s stderr says the daemon is not running (DAEMON_DOWN_SIGNS)."""
    return any(sign in stderr for sign in DAEMON_DOWN_SIGNS)
