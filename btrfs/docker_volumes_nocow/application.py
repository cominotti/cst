# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's use cases: status, mark, unmark, migrate (the boot's run) and one volume's NOCOW copy."""

from __future__ import annotations

from btrfs.docker_volumes_nocow.domain import (
    GAVE_UP,
    GIVE_UP_AFTER,
    MARGIN,
    NEW_SUFFIX,
    OLD_MARK,
    NocowCommand,
    NocowPaths,
    PendingVolume,
    copy_check_failed,
    config_reason,
    copy_matches,
    label_mismatch,
    next_postponed,
    postponed_line,
)
from btrfs.docker_volumes_nocow.ports import DockerDaemonPort, NocowOutputPort, NocowStatePort, VolumeTreePort
from cst_core.ports import ClockPort


class DockerVolumesNocow:
    """docker-volumes-nocow's commands over its ports."""

    def __init__(
        self,
        paths: NocowPaths,
        tree: VolumeTreePort,
        state: NocowStatePort,
        daemon: DockerDaemonPort,
        out: NocowOutputPort,
        clock: ClockPort,
    ) -> None:
        self.paths, self.tree, self.state, self.daemon, self.out, self.clock = paths, tree, state, daemon, out, clock

    def execute(self, command: NocowCommand) -> int:
        """Run one command; its exit status. An OSError (e.g. a data-root that is not on
        btrfs) is one journal line; exit 0 for the boot's migrate and for unmark, else 2."""
        try:
            match command:
                case NocowCommand.MARK if self.paths.config_problem:
                    # btrfs-setup then leaves the request to the boot, which waits until it is fixed.
                    self.out.log(3, f"{self.paths.config_problem}: nothing marked (fix the file, then run it again)")
                    return 2
                case NocowCommand.MARK:
                    return 1 if self.mark() else 0
                case NocowCommand.MIGRATE:
                    self.migrate()
                case NocowCommand.UNMARK:
                    self.unmark()
                case _:
                    self.status()
        except OSError as e:
            self.out.log(3, f"{self.paths.volumes}: {e}")
            return 0 if command in (NocowCommand.MIGRATE, NocowCommand.UNMARK) else 2
        return 0

    # ---- what the volumes hold -------------------------------------------
    def pending(self, *, fix: bool = False) -> list[PendingVolume]:
        """Volumes that still have copy-on-write data. fix: first set +C in place on folders and empty files."""
        out = []
        for name, data in self.tree.volumes():
            scan = self.tree.scan(data, fix=fix)
            if scan.cow_files:
                out.append(PendingVolume(name, data, scan.cow_files, scan.cow_bytes))
        return out

    def not_fully_nocow(self) -> list[str]:
        """Volumes with copy-on-write data or a copy-on-write folder (status)."""
        return [
            name for name, data in self.tree.volumes() if self.tree.scan(data).cow_files or self.tree.dirs_cow(data)
        ]

    def recover(self) -> None:
        """Leftovers of an interrupted migration (kill, timeout, power loss)."""
        for name in self.tree.restore_interrupted():
            self.out.log(4, f"{name}: restored the original after an interrupted migration")

    # ---- one volume ------------------------------------------------------
    def migrate_one(self, data: str) -> None:
        """Copy _data into a NOCOW twin, check it, swap the names, drop the old copy."""
        new, old = data + NEW_SUFFIX, data + f"{OLD_MARK}{int(self.clock.now())}"
        self.tree.make_twin(data, new)
        self.tree.reference_label(data, new)
        self.tree.set_nocow(new, on=True)
        # Real copy (NOCOW and CoW extents cannot share data); every file and
        # folder created below `new` inherits +C before any data is written.
        r = self.tree.copy(data, new)
        if r.returncode != 0:
            self.tree.remove_tree(new, ignore_errors=True)
            raise RuntimeError(f"copy failed: {r.stderr.strip()[:200]}")
        new_scan = self.tree.scan(new)
        old_scan = self.tree.scan(data)
        if not copy_matches(old_scan, new_scan) or self.tree.dirs_cow(new):
            self.tree.remove_tree(new, ignore_errors=True)
            raise RuntimeError(copy_check_failed(old_scan, new_scan))
        # cp -a keeps SELinux labels when it can and says nothing when it cannot;
        # a container cannot open a file whose label changed.
        why = label_mismatch(self.tree.labels(data), self.tree.labels(new))
        if why:
            self.tree.remove_tree(new, ignore_errors=True)
            raise RuntimeError(why)
        self.tree.sync()
        self.tree.rename(data, old)
        try:
            self.tree.rename(new, data)
        except OSError:
            self.tree.rename(old, data)
            raise
        self.tree.remove_tree(old)

    # ---- the commands ----------------------------------------------------
    def request_migration(self) -> bool:
        """Write the flag (and its folder), durable before +C is set: the data root may be on
        another filesystem than /etc, and after a power loss +C must not stay while the flag
        is lost. False, with a log line, when that fails."""
        try:
            if not self.state.flag_exists() and self.state.attempts():
                # A count of boots without its flag is not this request's (an older helper removed the
                # flag, then the count, and could stop between the two): the request gets its two boots.
                self.state.forget_attempts()
            self.state.request_migration()
        except OSError as e:
            self.out.log(
                3,
                f"cannot schedule the migration ({e}); {self.paths.volumes} keeps copy-on-write, the next boot tries again",
            )
            return False
        return True

    def note_postponed(self, why: str) -> None:
        """Count this boot as postponed (once per boot) and keep the latest reason."""
        boot = self.state.boot_id()
        record = next_postponed(self.state.read_postponed(), why=why, boot=boot, now=int(self.clock.now()))
        self.state.write_postponed(record)

    def postpone(self, busy: str, *, always: bool = False) -> None:
        """A Docker daemon may use the volumes: nothing is touched this boot."""
        self.out.log(4, f"{busy}: migration left for the next boot")
        # Not an attempt and no alert, but recorded when there was work to
        # do (a scheduled migration, or the folder still without +C), so status
        # and `cst doctor` can show a migration held back boot after boot.
        # ALWAYS: recorded whatever the folder shows (it may not be the one Docker uses).
        vols = self.paths.volumes
        if always or self.state.flag_exists() or (self.tree.is_dir(vols) and not self.tree.is_nocow(vols)):
            self.note_postponed(busy)
        else:
            self.state.clear_postponed()

    def give_up(self) -> None:
        # The counter before the flag: a stop between the two leaves the flag (the next boot tries
        # again), never a counter without its flag (a later request would give up without a try).
        self.state.remove_attempts()  # a later 'install' starts afresh
        self.state.remove_flag()
        self.out.log(3, GAVE_UP)
        # At boot nobody is logged in yet: btrfs-notify keeps it and it is
        # shown at the next graphical login.
        self.out.notify("Docker volumes: NOCOW migration gave up", GAVE_UP)

    def rewrite(self, todo: list[PendingVolume]) -> int:
        """Each volume's NOCOW copy; the number that failed (one must not stop the others)."""
        failed = 0
        for v in todo:
            if self.tree.free_bytes() < v.size + MARGIN:
                self.out.log(4, f"{v.name}: not enough free space for a {v.size >> 20} MiB copy; skipped")
                failed += 1
                continue
            t0 = self.clock.now()
            try:
                self.migrate_one(v.data)
                self.out.log(6, f"{v.name}: {v.files} file(s), {v.size >> 20} MiB in {self.clock.now() - t0:.0f}s")
            except Exception as e:  # one volume must not stop the others
                self.out.log(3, f"{v.name}: {e}; left as it was")
                failed += 1
        return failed

    def migrate(self) -> None:
        """The boot's run (docker-volumes-nocow.service, before docker.service)."""
        vols = self.paths.volumes
        if self.paths.config_problem:  # the folder Docker uses is unknown: it may not be vols
            # Recorded even with no flag and vols absent or +C: the volumes Docker uses may be elsewhere,
            # and only a fixed file tells; status and cst doctor then say so.
            self.postpone(config_reason(self.paths.config_problem), always=True)
            return
        if not self.tree.is_dir(vols):
            self.state.clear_postponed()  # no volumes (Docker removed, data-root moved): nothing waits
            return
        busy = self.daemon.service_busy()
        if busy is None:
            busy = self.daemon.dockerd_busy()
        if busy is not None:
            self.postpone(busy)
            return
        self.state.clear_postponed()
        self.recover()
        # Every boot: Docker may have been installed after btrfs-setup, creating
        # the volumes folder without +C; the volumes made in between are migrated too.
        # The folder without +C is the only sign of those volumes, so the flag is
        # written first: if that fails, the folder keeps no +C and the next boot sees
        # the same sign again (setting +C first would lose the request for good).
        if not self.tree.is_nocow(vols):
            if self.pending() and not self.request_migration():
                return
            self.tree.set_nocow(vols, on=True)
            self.pending(fix=True)
        if not self.state.flag_exists():
            return
        attempts = self.state.attempts()
        if attempts >= GIVE_UP_AFTER:
            self.give_up()
            return
        # Durable before the long rewrite starts: a hang or hard reset must still count this boot,
        # or "gave up after two boots" never trips.
        self.state.save_attempts(attempts + 1)
        self.tree.set_nocow(vols, on=True)
        todo = self.pending(fix=True)
        self.out.log(5, f"rewriting {len(todo)} volume(s) without copy-on-write before Docker starts")
        if not self.rewrite(todo):
            self.state.remove_attempts()  # before the flag, as in give_up
            self.state.remove_flag()
            self.out.log(5, "all Docker volumes are NOCOW now")

    def mark(self) -> bool:
        """+C on the volumes folder (new volumes inherit it); True when existing volumes still need migrating."""
        vols = self.paths.volumes
        if not self.tree.is_dir(vols):
            self.out.say(f"{vols}: absent (Docker not set up yet): nothing to mark")
            return False
        self.tree.set_nocow(vols, on=True)
        todo = self.pending(fix=True)  # folders and empty files: in place, now
        self.state.forget_attempts()  # a new request gets its two boots again
        if not todo:
            return False
        self.out.say(
            f"{len(todo)} existing volume(s) hold copy-on-write data "
            f"({sum(t.size for t in todo) >> 20} MiB): rewritten at the next boot, before Docker starts"
        )
        return True

    def unmark(self) -> None:
        """Clear +C on the volumes folder (uninstall); the volumes keep theirs."""
        self.state.clear_postponed()
        if self.tree.is_dir(self.paths.volumes):
            self.tree.set_nocow(self.paths.volumes, on=False)

    def status(self) -> None:
        vols = self.paths.volumes
        if self.paths.config_problem:
            self.out.say(f"{self.paths.config_problem}: the boot's migration waits until it is fixed")
        if not self.tree.is_dir(vols):
            self.out.say(f"{vols}: absent")
            return
        on = self.tree.is_nocow(vols)
        todo = self.pending()
        count = len(self.tree.volumes())
        self.out.say(
            f"{vols}: new volumes {'NOCOW' if on else 'copy-on-write'}; "
            f"{count - len(self.not_fully_nocow())} of {count} volume(s) fully NOCOW"
        )
        if todo:
            when = "at the next boot" if self.state.flag_exists() else "not scheduled"
            self.out.say(
                f"  {len(todo)} with copy-on-write data ({sum(t.size for t in todo) >> 20} MiB), migration {when}"
            )
        line = postponed_line(self.state.read_postponed())
        if line:
            self.out.say(line)
