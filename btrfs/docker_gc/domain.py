"""docker-gc's rules, no I/O: Docker's generated names, its time stamps, which containers are ephemeral."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
import re

DAY = 86400
HEARTBEAT_SECONDS = 300
MIN_UPTIME = 600  # after boot, restart-policy containers are still coming up
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


def auto_named(name: str) -> bool:
    m = re.fullmatch(r"([a-z]+)_([a-z]+)[0-9]?", name)
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


def ephemeral(c: Mapping[str, Any]) -> bool:
    name = c.get("Name", "").lstrip("/")
    labels = (c.get("Config") or {}).get("Labels") or {}
    restart = ((c.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    return (
        (auto_named(name) or TESTCONTAINERS_LABEL in labels)
        and not any(k.startswith(KEEP_LABEL_PREFIXES) for k in labels)
        and restart == "no"
    )
