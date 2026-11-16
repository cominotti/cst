"""docker-gc's commands (record, run, cleanup-now, status): the plan, what it shows and removes, and main()."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
import json
import os
import signal
import subprocess
import sys
import threading
import time
from btrfs.docker_gc.adapters import (
    FLUSH_SECONDS,
    KEEP_BUILD_CACHE_DAYS,
    KEEP_CONTAINER_DAYS,
    KEEP_IMAGE_DAYS,
    LAST_RUN,
    LAST_USED,
    PROTECTED,
    STATE_DIR,
    ask,
    container_use,
    df,
    docker,
    docker_up,
    inspect_all,
    load_state,
    locked,
    log,
    recorder_alive,
    save_state,
)
from btrfs.docker_gc.domain import DAY, HEARTBEAT_SECONDS, MIN_UPTIME, day, ephemeral, ts
from cst_core.domain.values import format_bytes_decimal


def record() -> None:
    st = load_state()
    # Down for a while with Docker running (not just a reboot, when Docker was
    # down too): `--rm` containers of that time left no trace, so the 14-day
    # clock starts over.
    with open("/proc/uptime") as f:
        booted = time.time() - float(f.read().split()[0])
    if st.get("heartbeat") and time.time() - max(st["heartbeat"], booted) > 3 * HEARTBEAT_SECONDS:
        st["since"] = time.time()
        log(5, "recorder was down for a while: image clock restarted")
    lock = threading.Lock()
    resolved = {}  # image reference -> (image id, resolved at)
    dirty = [False]

    def use(iid: str, t: float) -> None:
        if iid and t > st["images"].get(iid, 0):
            with lock:
                st["images"][iid] = t
                dirty[0] = True

    # Whatever happened while no recorder ran: every container counts as a use.
    for c in inspect_all("container", ("ps", "-aq", "--no-trunc")):
        use(c.get("Image", ""), container_use(c))
    with lock:
        st["heartbeat"] = time.time()
        save_state(st)  # also fixes "since" on the very first start
        dirty[0] = False

    def flusher() -> None:  # on a clock, not only when an event arrives
        last_beat = time.time()
        while True:
            time.sleep(FLUSH_SECONDS)
            with lock:
                if dirty[0] or time.time() - last_beat >= HEARTBEAT_SECONDS:
                    st["heartbeat"] = last_beat = time.time()
                    try:
                        save_state(st)
                        dirty[0] = False
                    except OSError as e:  # stale heartbeat = images held: safe
                        log(3, f"cannot save state: {e}")

    threading.Thread(target=flusher, daemon=True).start()

    def image_id(ref: str) -> str:
        if ref.startswith("sha256:") and len(ref) == 71:
            return ref
        hit = resolved.get(ref)
        if hit and time.time() - hit[1] < 60:
            return hit[0]
        r = docker("image", "inspect", "--format", "{{.Id}}", ref, check=False, timeout=30)
        iid = r.stdout.strip() if r.returncode == 0 else ""
        if len(resolved) > 2000:
            resolved.clear()
        resolved[ref] = (iid, time.time())
        return iid

    proc = subprocess.Popen(
        ["docker", "events", "--format", "{{json .}}", "--filter", "type=container", "--filter", "type=image"],
        stdout=subprocess.PIPE,
        text=True,
    )
    stopping = []

    def stop(*_: object) -> None:  # systemctl stop: end the loop, flush below
        stopping.append(True)
        proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    log(6, "recording image use from docker events")
    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind, action = ev.get("Type"), ev.get("Action", "")
        actor = ev.get("Actor") or {}
        if kind == "container" and action in ("create", "start", "die", "stop"):
            ref = (actor.get("Attributes") or {}).get("image", "")
        elif kind == "image" and action in ("pull", "tag", "load", "import", "create", "push"):
            ref = actor.get("ID", "")
        else:
            continue
        use(image_id(ref) if ref else "", time.time())
    with lock:
        save_state(st)
    rc = proc.wait()
    if stopping or rc < 0:
        sys.exit(0)
    # docker events ended by itself (daemon restart): systemd restarts us.
    sys.exit(rc or 1)


# ---- collection -----------------------------------------------------------------------
def is_protected(im: Mapping[str, Any]) -> bool:
    refs = (im.get("RepoTags") or []) + (im.get("RepoDigests") or [])
    return any(r.startswith(PROTECTED) for r in refs)


def plan(now: float, timed: bool = True) -> dict[str, Any]:
    """What to remove. timed=True: the weekly run (the recorder's start counts
    as a use; protected registries never; no images without the recorder).
    timed=False: cleanup-now."""
    containers = inspect_all("container", ("ps", "-aq", "--no-trunc"))
    st = load_state()
    used_by = {}
    drop_c, keep_images, kept_vols, freed_vols = [], set(), set(), set()
    for c in containers:
        iid = c.get("Image", "")
        used_by[iid] = max(used_by.get(iid, 0), container_use(c))
        state = c.get("State") or {}
        ended = ts(state.get("FinishedAt")) or ts(c.get("Created"))
        vols = {m.get("Name") for m in c.get("Mounts") or [] if m.get("Type") == "volume"}
        if (
            state.get("Status") in ("exited", "created", "dead")
            and ephemeral(c)
            and ended
            and now - ended >= KEEP_CONTAINER_DAYS * DAY
        ):
            drop_c.append(
                {
                    "id": c["Id"],
                    "name": c.get("Name", "").lstrip("/"),
                    "image": (c.get("Config") or {}).get("Image", "?"),
                    "ended": ended,
                }
            )
            freed_vols |= vols
        else:
            keep_images.add(iid)
            kept_vols |= vols

    # Anonymous volumes no remaining container uses: today's orphans plus
    # those of the containers removed above. Named volumes never.
    volumes = inspect_all("volume", ("volume", "ls", "-q"))
    drop_v = sorted(
        v["Name"]
        for v in volumes
        if "com.docker.volume.anonymous" in (v.get("Labels") or {}) and v["Name"] not in kept_vols
    )

    images = inspect_all("image", ("image", "ls", "-aq", "--no-trunc"))
    recorder_ok = recorder_alive(st)
    drop_i, held = [], 0
    for im in images:
        iid = im.get("Id", "")
        if iid in keep_images:
            continue
        # Uses: recorded events, containers (also those removed now: until
        # they stopped), and the local tag time (pull, build, tag).
        used = max(st["images"].get(iid, 0), used_by.get(iid, 0), ts((im.get("Metadata") or {}).get("LastTagTime")))
        if timed:
            used = max(used, st["since"])
        if now - used < KEEP_IMAGE_DAYS * DAY:
            continue
        prot = is_protected(im)
        if timed and (prot or not recorder_ok):
            held += 1
            continue
        drop_i.append(
            {
                "id": iid,
                "tags": im.get("RepoTags") or [],
                "size": im.get("Size", 0) or 0,
                "used": used,
                "protected": prot,
            }
        )
    return {
        "containers": drop_c,
        "volumes": drop_v,
        "images": drop_i,
        "held": held,
        "recorder_ok": recorder_ok,
        "at": now,
    }


def show(p: Mapping[str, Any], full: bool = False) -> None:
    now = p["at"]
    cs, vs, ims = p["containers"], p["volumes"], p["images"]
    lc, lv, li = (None, None, None) if full else (10, 3, 15)
    print(f"ephemeral containers stopped for {KEEP_CONTAINER_DAYS}+ days: {len(cs)}")
    for c in cs[:lc]:
        print(f"  {c['name']:32} {c['image'][:60]}  (stopped {int((now - c['ended']) // DAY)} days ago)")
    print(f"orphaned anonymous volumes: {len(vs)}")
    for v in vs[:lv]:
        print(f"  {v}")
    total = sum(i["size"] for i in ims)
    # Per-image sizes overlap (shared base layers are counted for each image).
    print(f"unused images: {len(ims)}, up to {format_bytes_decimal(total)} (shared layers counted per image)")
    for i in sorted(ims, key=lambda x: -x["size"])[:li]:
        name = i["tags"][0] if i["tags"] else "<untagged> " + i["id"][7:19]
        mark = "  [protected registry]" if i["protected"] else ""
        print(f"  {format_bytes_decimal(i['size']):>9}  {name[:80]}  (last use {day(i['used'])}){mark}")
    if lc is not None and lv is not None and li is not None and (len(cs) > lc or len(vs) > lv or len(ims) > li):
        print("  (lists truncated; 'docker-gc cleanup-now' shows everything before asking)")
    if p.get("held"):
        why = "the recorder is not running" if not p["recorder_ok"] else "protected registry: cleanup-now only"
        print(f"held back: {p['held']} image(s) ({why})")


def remove_image(img: Mapping[str, Any], plan_at: float) -> str:
    """Re-checked right before removal: still unused (fresh container list and
    recorder state), and each tag still pointing at this very image."""
    iid = img["id"]
    if iid in {c.get("Image") for c in inspect_all("container", ("ps", "-aq", "--no-trunc"))}:
        return "now used by a container"
    if load_state()["images"].get(iid, 0) > plan_at - 60:
        return "used meanwhile"
    for t in img["tags"]:
        r = docker("image", "inspect", "--format", "{{.Id}}", t, check=False)
        if r.returncode != 0 or r.stdout.strip() != iid:
            continue  # re-pulled or retagged meanwhile: that tag is not ours
        r = docker("image", "rm", t, check=False)
        if r.returncode != 0:
            return f"cannot remove {t}: {r.stderr.strip()[:100]}"
    r = docker("image", "inspect", iid, check=False)
    if r.returncode == 0:
        try:
            left = json.loads(r.stdout)[0].get("RepoTags") or []
        except ValueError, IndexError:
            left = ["?"]
        if left:
            return "got new tags meanwhile"
        r = docker("image", "rm", iid, check=False)
        if r.returncode != 0 and "no such image" not in r.stderr.lower():
            return r.stderr.strip()[:120]
    return ""


def apply(p: Mapping[str, Any]) -> int:
    failed = 0
    for c in p["containers"]:
        if docker("container", "rm", c["id"], check=False).returncode != 0:
            failed += 1
            log(4, f"could not remove container {c['name']}")
    # Exactly the planned volumes; `rm` refuses one that got used meanwhile.
    for v in p["volumes"]:
        r = docker("volume", "rm", v, check=False)
        if r.returncode != 0 and "no such volume" not in r.stderr.lower():
            failed += 1
            log(4, f"could not remove volume {v[:16]}: {r.stderr.strip()[:100]}")
    for img in p["images"]:
        why = remove_image(img, p["at"])
        if why:
            log(5, f"kept image {(img['tags'] or [img['id']])[0]}: {why}")
    # --all: unused cache of any kind; until= is measured from its last use.
    docker("builder", "prune", "-f", "--all", "--filter", f"until={KEEP_BUILD_CACHE_DAYS * 24}h", check=False)
    return failed


def run(dry: bool) -> int:
    if not docker_up():
        return 0
    with open("/proc/uptime") as f:
        if float(f.read().split()[0]) < MIN_UPTIME and not dry:
            log(5, "booted less than 10 minutes ago; skipped this time")
            return 0
    _lock = locked()
    p = plan(time.time())
    show(p)
    if dry:
        print("(dry run: nothing removed)")
        return 0
    before = df()
    failed = apply(p)
    after = df()
    summary: dict[str, Any] = {
        "at": p["at"],
        "containers": len(p["containers"]),
        "volumes": len(p["volumes"]),
        "images": len(p["images"]),
        "image_bytes": sum(i["size"] for i in p["images"]),
        "failed": failed,
        "before": before,
        "after": after,
    }
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    with open(LAST_RUN, "w") as f:
        json.dump(summary, f)
    log(
        5,
        f"removed {summary['containers']} container(s), {summary['volumes']} anonymous volume(s), "
        f"{summary['images']} image(s) planned ({format_bytes_decimal(summary['image_bytes'])}), old build cache; "
        f"failures {failed}; images {before.get('Images', '?')} -> {after.get('Images', '?')}",
    )
    return 0


def cleanup_now() -> int:
    if not docker_up():
        return 0
    if not sys.stdin.isatty():
        print("cleanup-now asks for confirmation: run it in a terminal.", file=sys.stderr)
        return 1
    _lock = locked()
    p = plan(time.time(), timed=False)
    show(p, full=True)
    if not (p["containers"] or p["volumes"] or p["images"]):
        print("Nothing to remove.")
        return 0
    prot = [i for i in p["images"] if i["protected"]]
    p["images"] = [i for i in p["images"] if not i["protected"]]
    if not ask(
        f"\nRemove the containers, volumes and {len(p['images'])} image(s) above"
        f"{' (the [protected registry] ones are asked next)' if prot else ''}? [y/N] "
    ):
        print("Nothing removed.")
        return 0
    if prot and ask(
        f"Also remove the {len(prot)} image(s) marked [protected registry]? "
        f"Re-pulling them needs the private tunnel. [y/N] "
    ):
        p["images"] += prot
    failed = apply(p)
    print(f"Done; failures: {failed}. Now: {df()}")
    return 0


def status() -> int:
    st = load_state() if os.path.exists(LAST_USED) else None
    if st:
        print(
            f"image use recorded since {datetime.fromtimestamp(st['since']).strftime('%F %T')}"
            f" ({len(st['images'])} image(s) seen); timed image removal possible from "
            f"{day(st['since'] + KEEP_IMAGE_DAYS * DAY)}; recorder "
            f"{'ok' if recorder_alive(st) else 'NOT running (images are held back)'}"
        )
    else:
        print("image use not recorded yet (recorder not started)")
    try:
        with open(LAST_RUN) as f:
            lr = json.load(f)
        print(
            f"last run {datetime.fromtimestamp(lr['at']).strftime('%F %T')}: {lr['containers']} container(s), "
            f"{lr['volumes']} volume(s), {lr['images']} image(s) ({format_bytes_decimal(lr['image_bytes'])}), "
            f"failures {lr['failed']}"
        )
    except OSError, ValueError, KeyError:
        print("last run: never")
    if not docker_up():
        return 0
    print("a weekly run now would remove:")
    show(plan(time.time()))
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "record":
            record()
        elif cmd == "run":
            return run("--dry-run" in sys.argv[2:])
        elif cmd == "cleanup-now":
            return cleanup_now()
        elif cmd == "status":
            return status()
        else:
            print("usage: docker-gc record|run [--dry-run]|cleanup-now|status", file=sys.stderr)
            return 2
    except RuntimeError as e:
        log(3, str(e))
        return 1
    return 0
