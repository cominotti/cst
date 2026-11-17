# SPDX-License-Identifier: MPL-2.0
"""docker-gc's rules, no I/O: Docker's generated names, its time stamps, which containers are
ephemeral, what a run removes (the plan) and the lines that show it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import os
import re

from cst_core.domain.contracts import StateFormat, state_format
from cst_core.domain.values import format_bytes_decimal

# The recorder's state and the last run's summary (the files of GcPaths).
STATE_FORMAT: StateFormat = state_format("docker-gc-state")
LAST_RUN_FORMAT: StateFormat = state_format("docker-gc-last-run")
DAY = 86400
HEARTBEAT_SECONDS = 300
MIN_UPTIME = 600  # after boot, restart-policy containers are still coming up
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
    # Registry prefixes ("host/") whose images only cleanup-now removes.
    protected: tuple[str, ...] = ("registry.example.com/",)
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
    if not value or not isinstance(value, str) or value.startswith("0001-"):
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
