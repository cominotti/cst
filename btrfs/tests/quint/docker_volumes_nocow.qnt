// SPDX-License-Identifier: MPL-2.0
// docker-volumes-nocow's install and boot migration (btrfs/docker_volumes_nocow/application.py) as a
// state machine: DockerVolumesNocow.mark (btrfs-setup.sh writes nothing after it), and
// migrate (recover, the folder's +C and its request, the give-up check, rewrite, give_up), with a
// crash possible after any step: a kill, or a power loss that also loses a copy not yet synced.
// What it checks and why: docs/dev-tools.md, "Model checking with Quint (optional)".
//
// One action per call of the code that changes the disk or the helper's files (VolumeTreePort and
// NocowStatePort), in the code's order. The label each action leaves in "step" is the call the replay
// (btrfs/tests/test_docker_volumes_nocow_replay.py) records from the real code, so a trace of this
// spec can be replayed against it; "~" labels mark what the code does without a recorded call (a
// boot, a crash, a decision). A change of order in the module changes this spec and its traces in the
// same commit.
//
// Abstraction: a volume's folders hold contents, not files: "cow" (the original, copy-on-write), "copy"
// (a complete NOCOW copy of it), "partial" (an incomplete or wrong copy), "empty" (the twin just made),
// "absent". Not modelled: scan(fix) setting +C in place on folders and empty files, the postponed
// record, journal lines and the alert, daemon.json problems, a dockerd outside docker.service, status
// and unmark, a failing remove_tree, containers writing during an install (mark's second scan finds
// what the first did), durability of metadata, and the start an older install could leave (+C on
// the folder, copy-on-write data, no flag: requestKept rules it out; MarkUnmarkTest and tier 2
// test_05a check that an install requests the migration again from there). A power loss is modelled
// only as the loss of a copy's unsynced content: the flag (/etc), the counter (/var) and the data root
// may be on different filesystems and roll back apart; the code keeps the pairs that matter in order by
// fsync (the flag before +C, the counter's reset before the flag), which unit tests check.
module nocow {
  // The volumes, in the order the code walks them (LocalVolumeTree.volumes: sorted names).
  const VOLS: List[str]
  const GIVE_UP_AFTER: int
  // Bounds on what the environment does in one trace: crashes (kills and power losses); installs and
  // Docker recreated.
  const MAX_CRASHES: int
  const MAX_ENV: int

  pure val ABSENT = "absent"
  pure val COW = "cow"
  pure val COPY = "copy"
  pure val PARTIAL = "partial"
  pure val EMPTY = "empty"

  // _data, _data.nocow-new (the twin), the number of _data.cow-old-N folders (each the original), and
  // whether a copy in this volume is not yet synced (os.sync flushes every volume's).
  type Vol = { data: str, twin: str, olds: int, unsynced: bool }
  type Label = { name: str, vol: str, outcome: str }
  type State = {
    vols: str -> Vol,
    volsNocow: bool,   // +C on the volumes folder
    flag: bool,        // /etc/btrfs-setup/docker-nocow-pending
    attempts: int,     // the attempts counter (0: no file)
    pc: str,           // "idle" between runs, else the step that runs next
    i: int,            // the volume being worked on (index in VOLS, or in todo)
    todo: List[str],   // this boot's volumes with copy-on-write data
    failed: int,       // volumes that failed in this boot's rewrite
    recFailed: bool,   // a volume this boot's recover could not recover
    count: int,        // the count give_up read (written back when the flag cannot go)
    // ghosts: not on disk, for the properties
    gaveUp: bool,      // give_up removed the flag, and no flag was made since
    boots: int,        // boots of this request's round that chose to rewrite
    rewrites: int,     // rewrites started in this round
    carried: int,      // the count when the code decided to end the round (give up, or all rewritten)
    started: bool,     // this run started a rewrite (its first make_twin)
    clean: bool,       // this run had no fault and no crash so far
    rewrote: bool,     // this run chose to rewrite
    crashes: int,      // crashes so far (bounded by MAX_CRASHES)
    env: int,          // installs and Docker recreations so far (bounded by MAX_ENV)
    installing: bool,  // the last run started is an install
  }

  var s: State
  var step: Label

  // Faults are picked as a roll of 0..4 (0: the fault) and a crash needs a roll of 0 out of 0..7: the
  // same behaviours for the checker, a fault only now and then for a random walk.
  pure val ROLLS = 0.to(4)
  pure val CRASH_ROLLS = 0.to(7)

  pure def lbl(name: str, vol: str, outcome: str): Label = { name: name, vol: vol, outcome: outcome }
  pure val SILENT = lbl("~", "", "")

  def vol(v: str): Vol = s.vols.get(v)
  def anyCow: bool = VOLS.foldl(false, (acc, v) => acc or vol(v).data == COW)
  def cowVols: List[str] = VOLS.foldl([], (acc, v) => if (vol(v).data == COW) acc.append(v) else acc)
  def setVol(v: str, x: Vol): str -> Vol = s.vols.set(v, x)
  // A new request (a flag where there was none) starts a round; an existing flag keeps its round.
  def newRound(st: State): State = { ...st, boots: 0, rewrites: 0, carried: 0, gaveUp: false }
  def flagged(st: State): State = if (st.flag) st else { ...newRound(st), flag: true }
  // The decision to end the request (give up, or every volume rewritten) ends its round. The counter
  // still holds the round's count until the code removes it: carried keeps that count, so a stop before
  // the removal goes on with it.
  def endRound(st: State): State = { ...st, boots: 0, rewrites: 0, carried: st.attempts }
  def fault(st: State): State = { ...st, clean: false }

  // ---- the start: after an install (+C and the flag) or with Docker set up after it (no +C) -----------
  // Without a flag, the counter may hold a count an older helper left (it removed the flag first, then
  // the counter, and could stop between the two): any count up to GIVE_UP_AFTER.
  action init = {
    nondet start = VOLS.foldl(Set(Map()), (acc, v) =>
      acc.map(m => Set(m.put(v, { data: COW, twin: ABSENT, olds: 0, unsynced: false }),
                       m.put(v, { data: COPY, twin: ABSENT, olds: 0, unsynced: false }))).flatten())
      .oneOf()
    nondet nocow = Set(true, false).oneOf()
    nondet stale = 0.to(GIVE_UP_AFTER).oneOf()
    val cow = VOLS.foldl(false, (acc, v) => acc or start.get(v).data == COW)
    val flag = nocow and cow
    all {
      s' = { vols: start, volsNocow: nocow, flag: flag, attempts: if (flag) 0 else stale, pc: "idle", i: 0,
             todo: [], failed: 0, recFailed: false, count: 0, gaveUp: false, boots: 0, rewrites: 0, carried: 0,
             started: false, clean: false, rewrote: false, crashes: 0, env: 0, installing: false },
      step' = lbl("~init", "", ""),
    }
  }

  // ---- between runs ---------------------------------------------------------------------------------
  // A boot: docker-volumes-nocow.service starts; Docker may be running (then nothing is touched).
  action boot = all {
    s.pc == "idle",
    nondet roll = ROLLS.oneOf()
    if (roll == 0) all {
      s' = { ...s, clean: false, rewrote: false, started: false, installing: false },
      step' = lbl("~postpone", "", ""),
    } else all {
      s' = { ...s, pc: "recover", i: 0, recFailed: false, clean: true, rewrote: false, started: false, installing: false },
      step' = lbl("~boot", "", ""),
    },
  }

  // cst btrfs install: mark (btrfs-setup.sh writes nothing after it, whatever its exit status). mark's
  // request comes before +C: with copy-on-write data it resets an old count (renew: the request gets
  // its GIVE_UP_AFTER boots again) and writes the flag, flushed; exit 2 when either fails (no +C then:
  // the next boot asks again). Without copy-on-write data it forgets the count; +C; exit 0.
  action install = all {
    s.pc == "idle",
    s.env < MAX_ENV,
    val st = { ...s, clean: false, rewrote: false, started: false, env: s.env + 1, installing: true }
    if (not(anyCow)) all { s' = { ...st, pc: "install_forget" }, step' = lbl("~install", "", "") }
    else if (s.attempts > 0) all { s' = { ...st, pc: "install_reset" }, step' = lbl("~install", "", "") }
    else all { s' = { ...newRound(st), gaveUp: s.gaveUp, pc: "install_request" }, step' = lbl("~install", "", "") },
  }
  action installReset = all {
    s.pc == "install_reset",
    nondet roll = ROLLS.oneOf()
    if (roll != 0) all {
      s' = { ...newRound(s), gaveUp: s.gaveUp, attempts: 0, pc: "install_request" },
      step' = lbl("save_attempts", "", "ok"),
    } else all { s' = { ...s, pc: "idle" }, step' = lbl("save_attempts", "", "fail") },
  }
  action installRequest = all {
    s.pc == "install_request",
    nondet roll = ROLLS.oneOf()
    if (roll != 0) all { s' = { ...flagged(s), pc: "install_nocow" }, step' = lbl("request_migration", "", "ok") }
    else all { s' = { ...s, pc: "idle" }, step' = lbl("request_migration", "", "fail") },
  }
  // Install renews the count either way: forgotten here, reset above. Its round starts once that is done.
  action installForget = all {
    s.pc == "install_forget",
    s' = { ...newRound(s), gaveUp: s.gaveUp, attempts: 0, pc: "install_nocow" },
    step' = lbl("forget_attempts", "", ""),
  }
  action installNocow = all {
    s.pc == "install_nocow",
    s' = { ...s, volsNocow: true, pc: "idle" },
    step' = lbl("set_nocow_vols", "", ""),
  }

  // Docker made again (its data root recreated): the folder without +C, and a volume created
  // meanwhile is copy-on-write.
  action recreate = {
    nondet v = VOLS.foldl(Set(), (acc, x) => acc.union(Set(x))).oneOf()
    val x = vol(v)
    all {
      s.pc == "idle",
      s.env < MAX_ENV,
      s' = { ...s, volsNocow: false, clean: false, rewrote: false, started: false, env: s.env + 1, installing: false,
             vols: if (x.data == COPY and x.twin == ABSENT and x.olds == 0) setVol(v, { ...x, data: COW }) else s.vols },
      step' = lbl("~recreate", v, ""),
    }
  }

  // ---- a crash after any step: a kill (or a timeout), or a power loss ----------------------------------
  // A power loss also loses what a copy not yet synced holds, wherever it is by then (the twin, or
  // _data after the second rename).
  pure def lose(x: Vol): Vol =
    if (not(x.unsynced)) x
    else { data: if (x.data == COPY) PARTIAL else x.data, twin: if (x.twin == COPY) PARTIAL else x.twin,
           olds: x.olds, unsynced: false }
  action crashIf(allowed: bool): bool = {
    nondet roll = CRASH_ROLLS.oneOf()
    nondet power = Set(false, true).oneOf()
    all {
      allowed,
      s.pc != "idle",
      roll == 0,
      s.crashes < MAX_CRASHES,
      s' = { ...s, pc: "idle", clean: false, crashes: s.crashes + 1,
             vols: if (power) VOLS.foldl(s.vols, (m, v) => m.set(v, lose(m.get(v)))) else s.vols },
      step' = if (power) lbl("~power_loss", "", "") else lbl("~crash", "", ""),
    }
  }
  action crash = crashIf(true)

  // ---- recover(): recover_one for each volume; one that cannot be recovered does not stop the others,
  // and then this boot migrates nothing --------------------------------------------------------------
  action recover = all {
    s.pc == "recover",
    if (s.i >= VOLS.length()) all {
      s' = { ...s, pc: if (s.recFailed) "idle" else "check_folder" },
      step' = if (s.recFailed) lbl("~recover_failed", "", "") else SILENT,
    } else {
      val v = VOLS[s.i]
      val x = vol(v)
      if (x.data == ABSENT and x.olds > 0) {
        // Died between the two renames: the newest old copy goes back.
        nondet roll = ROLLS.oneOf()
        if (roll != 0) all { s' = { ...s, vols: setVol(v, { ...x, data: COW, olds: x.olds - 1 }) },
                             step' = lbl("rename_old_data", v, "ok") }
        else all { s' = fault({ ...s, recFailed: true, i: s.i + 1 }), step' = lbl("rename_old_data", v, "fail") }
      }
      // With _data a folder: the other old copies, then the twin.
      else if (x.data != ABSENT and x.olds > 0)
        all { s' = { ...s, vols: setVol(v, { ...x, olds: x.olds - 1 }) }, step' = lbl("remove_old", v, "") }
      else if (x.data != ABSENT and x.twin != ABSENT)
        all { s' = { ...s, vols: setVol(v, { ...x, twin: ABSENT, unsynced: false }) }, step' = lbl("remove_new", v, "") }
      else all { s' = { ...s, i: s.i + 1 }, step' = SILENT }
    }
  }

  // ---- migrate(): a folder without +C is a request, written before +C --------------------------------
  action checkFolder = all {
    s.pc == "check_folder",
    s' = { ...s, pc: if (s.volsNocow) "check_flag"
                     else if (not(anyCow)) "set_nocow_vols1"
                     else if (not(s.flag) and s.attempts > 0) "reset"
                     else "request" },
    step' = SILENT,
  }
  // request_migration: a count without its flag is not this request's: reset first, durably.
  action reset = all {
    s.pc == "reset",
    nondet roll = ROLLS.oneOf()
    if (roll != 0) all { s' = { ...s, attempts: 0, pc: "request" }, step' = lbl("save_attempts", "", "ok") }
    else all { s' = fault({ ...s, pc: "idle" }), step' = lbl("save_attempts", "", "fail") },
  }
  action request = all {
    s.pc == "request",
    nondet roll = ROLLS.oneOf()
    if (roll != 0) all { s' = { ...flagged(s), pc: "set_nocow_vols1" }, step' = lbl("request_migration", "", "ok") }
    else all { s' = fault({ ...s, pc: "idle" }), step' = lbl("request_migration", "", "fail") },
  }
  action setNocowVols1 = all {
    s.pc == "set_nocow_vols1",
    s' = { ...s, volsNocow: true, pc: "check_flag" },
    step' = lbl("set_nocow_vols", "", ""),
  }
  // The give-up check: no flag, nothing to do; GIVE_UP_AFTER boots counted: give up; else count this boot.
  action checkFlag = all {
    s.pc == "check_flag",
    if (not(s.flag)) all { s' = { ...s, pc: "idle" }, step' = SILENT }
    else if (s.attempts >= GIVE_UP_AFTER)
      all { s' = { ...endRound(s), count: s.attempts, pc: "give_up_attempts" }, step' = lbl("~give_up", "", "") }
    else all { s' = { ...s, boots: s.boots + 1, rewrote: true, pc: "save_attempts" }, step' = lbl("~rewrite", "", "") },
  }
  // give_up: the counter before the flag; when the flag cannot go, the count goes back.
  action giveUpAttempts = all {
    s.pc == "give_up_attempts",
    s' = { ...s, attempts: 0, pc: "give_up_flag" },
    step' = lbl("remove_attempts", "", ""),
  }
  action giveUpFlag = all {
    s.pc == "give_up_flag",
    nondet roll = ROLLS.oneOf()
    if (roll != 0) all {
      s' = { ...s, flag: false, gaveUp: true, carried: 0, pc: "idle" },
      step' = lbl("remove_flag", "", "ok"),
    } else all { s' = fault({ ...s, pc: "give_up_save" }), step' = lbl("remove_flag", "", "fail") },
  }
  // The count back: the next boot gives up again at once (a stop before it is a stop between the
  // counter's removal and the flag's: the next boot starts the request over, as in the property's note).
  action giveUpSave = all {
    s.pc == "give_up_save",
    s' = { ...s, attempts: s.count, pc: "idle" },
    step' = lbl("save_attempts", "", "ok"),
  }
  // Durable before the rewrite starts: a hang or a hard reset during it still counts this boot.
  action saveAttempts = all {
    s.pc == "save_attempts",
    s' = { ...s, attempts: s.attempts + 1, pc: "set_nocow_vols2" },
    step' = lbl("save_attempts", "", "ok"),
  }
  action setNocowVols2 = all {
    s.pc == "set_nocow_vols2",
    s' = { ...s, volsNocow: true, todo: cowVols, i: 0, failed: 0, pc: "vol_start" },
    step' = lbl("set_nocow_vols", "", ""),
  }

  // ---- rewrite(): migrate_one for each volume with copy-on-write data ---------------------------------
  def cur: str = s.todo[s.i]
  action failVol(st: State, x: Vol, label: Label): bool =
    all { s' = fault({ ...st, vols: st.vols.set(cur, x), failed: st.failed + 1, i: st.i + 1, pc: "vol_start" }), step' = label }

  action volStart = all {
    s.pc == "vol_start",
    if (s.i >= s.todo.length()) {
      if (s.failed == 0) all { s' = { ...endRound(s), pc: "final_attempts" }, step' = lbl("~rewritten", "", "") }
      else all { s' = { ...s, pc: "idle" }, step' = SILENT }
    } else {
      nondet roll = ROLLS.oneOf()
      if (roll != 0) all { s' = { ...s, pc: "make_twin" }, step' = SILENT }
      else failVol(s, vol(cur), lbl("~no_room", cur, ""))
    }
  }
  action makeTwin = all {
    s.pc == "make_twin",
    s' = { ...s, vols: setVol(cur, { ...vol(cur), twin: EMPTY }), pc: "reference_label",
           rewrites: if (s.started) s.rewrites else s.rewrites + 1, started: true },
    step' = lbl("make_twin", cur, ""),
  }
  action referenceLabel = all {
    s.pc == "reference_label",
    nondet roll = ROLLS.oneOf()
    // chcon failed while the volume has labels: the adapter removes the twin.
    if (roll != 0) all { s' = { ...s, pc: "set_nocow_new" }, step' = lbl("reference_label", cur, "ok") }
    else failVol(s, { ...vol(cur), twin: ABSENT }, lbl("reference_label", cur, "fail"))
  }
  action setNocowNew = all {
    s.pc == "set_nocow_new",
    s' = { ...s, pc: "copy" },
    step' = lbl("set_nocow_new", cur, ""),
  }
  action copy = all {
    s.pc == "copy",
    nondet roll = 0.to(6).oneOf()
    val outcome = if (roll == 1) "fail" else if (roll == 2) "mismatch" else if (roll == 3) "killed" else "ok"
    val x = vol(cur)
    if (outcome == "ok")
      all { s' = { ...s, vols: setVol(cur, { ...x, twin: COPY, unsynced: true }), pc: "sync" }, step' = lbl("copy", cur, "ok") }
    // cp died with the process (a kill during the copy): the twin stays as it is.
    else if (outcome == "killed") all {
      s.crashes < MAX_CRASHES,
      s' = { ...s, vols: setVol(cur, { ...x, twin: PARTIAL }), pc: "idle", clean: false, crashes: s.crashes + 1 },
      step' = lbl("copy", cur, "killed"),
    }
    // cp failed, or its copy does not match the original: the twin goes.
    else all { s' = fault({ ...s, vols: setVol(cur, { ...x, twin: PARTIAL }), pc: "remove_new" }), step' = lbl("copy", cur, outcome) }
  }
  action removeNew = all {
    s.pc == "remove_new",
    failVol(s, { ...vol(cur), twin: ABSENT }, lbl("remove_new", cur, "")),
  }
  // The copy on disk before the names are swapped (os.sync: every volume's).
  action sync = all {
    s.pc == "sync",
    s' = { ...s, vols: VOLS.foldl(s.vols, (m, v) => m.set(v, { ...m.get(v), unsynced: false })), pc: "rename_data_old" },
    step' = lbl("sync", "", ""),
  }
  action renameDataOld = all {
    s.pc == "rename_data_old",
    val x = vol(cur)
    s' = { ...s, vols: setVol(cur, { ...x, data: ABSENT, olds: x.olds + 1 }), pc: "rename_new_data" },
    step' = lbl("rename_data_old", cur, ""),
  }
  action renameNewData = all {
    s.pc == "rename_new_data",
    nondet roll = ROLLS.oneOf()
    val x = vol(cur)
    if (roll != 0) all { s' = { ...s, vols: setVol(cur, { ...x, data: x.twin, twin: ABSENT }), pc: "remove_old" },
                         step' = lbl("rename_new_data", cur, "ok") }
    else all { s' = fault({ ...s, pc: "rollback" }), step' = lbl("rename_new_data", cur, "fail") },
  }
  action rollback = all {
    s.pc == "rollback",
    val x = vol(cur)
    failVol(s, { ...x, data: COW, olds: x.olds - 1 }, lbl("rename_old_data", cur, "ok")),
  }
  action removeOld = all {
    s.pc == "remove_old",
    val x = vol(cur)
    s' = { ...s, vols: setVol(cur, { ...x, olds: x.olds - 1 }), i: s.i + 1, pc: "vol_start" },
    step' = lbl("remove_old", cur, ""),
  }
  // Every volume rewritten: the counter before the flag, as in give_up.
  action finalAttempts = all {
    s.pc == "final_attempts",
    s' = { ...s, attempts: 0, pc: "final_flag" },
    step' = lbl("remove_attempts", "", ""),
  }
  action finalFlag = all {
    s.pc == "final_flag",
    s' = { ...s, flag: false, carried: 0, pc: "idle" },
    step' = lbl("remove_flag", "", "ok"),
  }

  action step_ = any {
    boot, install, installReset, installRequest, installForget, installNocow, recreate, crash,
    recover, checkFolder, reset, request, setNocowVols1, checkFlag, giveUpAttempts, giveUpFlag, giveUpSave,
    saveAttempts, setNocowVols2, volStart, makeTwin, referenceLabel, setNocowNew, copy, removeNew, sync,
    renameDataOld, renameNewData, rollback, removeOld, finalAttempts, finalFlag,
  }

  // The steps of step_ with a crash only where the code ends a request (after its decision, before the
  // flag is gone): for the new-round witness only. A random walk of step_ spends its crashes long
  // before a rewrite ends, so it almost never reaches a stop there; each step of this one is one of step_'s.
  pure val ROUND_END_PCS = Set("final_attempts", "final_flag", "give_up_attempts", "give_up_flag", "give_up_save")
  action step_round_end = any {
    boot, install, installReset, installRequest, installForget, installNocow, recreate, crashIf(ROUND_END_PCS.contains(s.pc)),
    recover, checkFolder, reset, request, setNocowVols1, checkFlag, giveUpAttempts, giveUpFlag, giveUpSave,
    saveAttempts, setNocowVols2, volStart, makeTwin, referenceLabel, setNocowNew, copy, removeNew, sync,
    renameDataOld, renameNewData, rollback, removeOld, finalAttempts, finalFlag,
  }

  // ---- properties ------------------------------------------------------------------------------------
  // Data never lost: each volume's _data holds the original or a complete copy of it, or an old copy
  // (the original, which recover puts back) is there.
  val noDataLoss = VOLS.foldl(true, (acc, v) => acc and (vol(v).data == COW or vol(v).data == COPY or vol(v).olds > 0))
  // The request is never lost: +C on the folder with copy-on-write data left means the flag is there,
  // unless the helper gave up. (+C first and the flag after would lose it for good: no later boot
  // looks again.)
  val requestKept = (s.volsNocow and anyCow and not(s.gaveUp)) implies s.flag
  // The counter holds no boot this request's round did not have (no early give-up). Per round, not
  // per request: the counter goes before the flag, so a stop between the two leaves the flag with no
  // counter and the next boot starts a new round (docs/dev-tools.md says why).
  val attemptsMatch = s.flag implies s.attempts <= s.boots + s.carried
  // The limit: at most GIVE_UP_AFTER rewrites in one round.
  val boundedTries = s.rewrites <= GIVE_UP_AFTER
  // A run with no fault and no crash that chose to rewrite ends with every volume a NOCOW copy, no
  // leftovers, no flag and no counter.
  val cleanRunConverges = (s.pc == "idle" and s.clean and s.rewrote) implies all {
    VOLS.foldl(true, (acc, v) => acc and vol(v) == { data: COPY, twin: ABSENT, olds: 0, unsynced: false }),
    not(s.flag),
    s.attempts == 0,
  }
  // Witnesses: the parts of the protocol a trace reaches (for the checked-in traces).
  val wRecovered = step.name == "rename_old_data" and step.outcome == "ok" and s.pc == "recover"
  val wRecoverFailed = step.name == "rename_old_data" and step.outcome == "fail"
  val wOldRemoved = step.name == "remove_old" and s.pc == "recover"
  val wTwinRemoved = step.name == "remove_new" and s.pc == "recover"
  val wRewritten = step.name == "rename_new_data" and step.outcome == "ok"
  val wRolledBack = step.name == "rename_old_data" and s.pc == "vol_start"
  val wGaveUp = step.name == "remove_flag" and step.outcome == "ok" and s.gaveUp
  val wGaveUpFlagKept = step.name == "remove_flag" and step.outcome == "fail"
  val wConverged = step.name == "remove_flag" and step.outcome == "ok" and not(s.gaveUp)
  // install's +C (only install's leads back to "idle") after its request
  val wInstallRequested = step.name == "set_nocow_vols" and s.pc == "idle" and s.flag and anyCow
  val wInstallRequestFailed = step.name == "request_migration" and step.outcome == "fail" and s.installing
  val wPowerLossPartial = step.name == "~power_loss" and VOLS.foldl(false, (acc, v) => acc or vol(v).twin == PARTIAL)
  // A rewrite in a request the code had decided to end: after a stop between the counter's removal and
  // the flag's (the trade-off of the counter-first order).
  val wNewRound = s.rewrites > 0 and s.carried > 0
  // Negated, for a checker that answers with a trace that reaches the witness.
  val neverRecovered = not(wRecovered)
  val neverRecoverFailed = not(wRecoverFailed)
  val neverOldRemoved = not(wOldRemoved)
  val neverTwinRemoved = not(wTwinRemoved)
  val neverRolledBack = not(wRolledBack)
  val neverGaveUp = not(wGaveUp)
  val neverGaveUpFlagKept = not(wGaveUpFlagKept)
  val neverConverged = not(wConverged)
  val neverInstallRequested = not(wInstallRequested)
  val neverInstallRequestFailed = not(wInstallRequestFailed)
  val neverPowerLossPartial = not(wPowerLossPartial)
  val neverNewRound = not(wNewRound)
}

// Two volumes, two boots, as the code has it (domain.GIVE_UP_AFTER).
module nocow_ab {
  import nocow(VOLS = ["a", "b"], GIVE_UP_AFTER = 2, MAX_CRASHES = 3, MAX_ENV = 2).*
}

// One volume: the smaller instance, for Apalache's witnesses.
module nocow_a {
  import nocow(VOLS = ["a"], GIVE_UP_AFTER = 2, MAX_CRASHES = 3, MAX_ENV = 2).*
}

// Boots only (no install, Docker never recreated): the migration's own protocol.
module nocow_ab_boots {
  import nocow(VOLS = ["a", "b"], GIVE_UP_AFTER = 2, MAX_CRASHES = 3, MAX_ENV = 0).*
}

// Larger bounds, for the exhaustive check (TLC).
module nocow_ab_wide {
  import nocow(VOLS = ["a", "b"], GIVE_UP_AFTER = 2, MAX_CRASHES = 6, MAX_ENV = 4).*
}
module nocow_abc {
  import nocow(VOLS = ["a", "b", "c"], GIVE_UP_AFTER = 2, MAX_CRASHES = 3, MAX_ENV = 2).*
}
