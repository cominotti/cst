// SPDX-License-Identifier: MPL-2.0
// vm's NAS backup (vm/application/nas.py: backup_nas, nas_run and what they call) as a state machine:
// the live snapshot's overlays and their merge back (blockcommit), restic's run and the torn snapshot
// it forgets (forget_torn, the record vm-nas-torn.json and the next run's retry_torn), the retention,
// the index, the alert; with a stop (SIGTERM, Ctrl-C) or a kill between any two steps, the guest
// shutting down, pausing or being started meanwhile, and libvirt answering late.
// What it checks and why: docs/dev-tools.md, "Model checking with Quint (optional)".
//
// One action per port call of nas.py that changes the machine, the repository or the records, or
// whose answer the code branches on, in the code's order. The label each action leaves in "step" is
// the call the replay (vm/tests/test_vm_nas_replay.py) records from the real code; "~" labels mark
// what happens without a recorded call (an environment event, a stop, a kill, a run's start). A
// change of order in nas.py changes this spec and its traces in the same commit.
//
// How a stop reaches the code (vm/adapters/backup.py, StopSignals): before the read it ends the run;
// during the read (snapshot, restic, the check after it, forget_torn) it is the read's failure, and
// the overlays are merged back first; inside signals_held (the snapshot call, the torn record) it
// waits for the block's end; during the merge back the stops are ignored; after it (retention,
// index) it ends the run. A kill (SIGKILL, power loss) ends the run where it is: nothing more is
// called, no alert.
//
// Abstraction: one VM, one host, one repository. A disk's source is 0 (its image) or r (the overlay
// of run r), in the live and in the persistent definition (a shut-off VM's live listing is its
// persistent one); an overlay file is (target, run). Snapshot ids are run numbers. Assumed, not
// modelled: a virsh snapshot-create-as that answered (not a timeout) switched every disk or none
// (--atomic), and never switches later; the instance nas_partial (not in cst-quint's checks) drops
// that assumption. Not modelled: two VMs at once and the record's lock (TORN_LOCK: unit tests), several
// hosts or a changed NAS_RESTIC_HOST, the /home job's prune holding the repository's lock (a call that
// fails), the notes and the words of the messages, the manifest and the staged files, the UEFI and
// TPM files read live (notes only), the clock (each timeout is an outcome), libvirt crashing, other
// VMs' and local backups' index entries, an index or a record of a newer cst vm, a find_overlays that
// cannot list the disks, a domblklist of the snapshot's poll that fails. The retention is restic forget
// with --keep-weekly and --keep-tag, one run a week: the KEEP newest snapshots and every kept one stay
// (restic's monthly buckets are not modelled). libvirt's late switch after a snapshot that timed out
// comes before the next run starts.
//
// Known gaps, stated in the properties: a torn snapshot restic committed is in the repository,
// neither recorded nor named, after a kill before its record (SIGKILL, power loss: nothing more
// runs), after a stop that ended restic once it had committed (Restic.backup drops the summary and
// its id: H1), and after a stop between restic's answer and forget_torn's held record (H2: the
// staging folder's removal, the shut-off VM's check). tornHandled and goodKept allow these;
// tornHandledFull and goodKeptFull (not in cst-quint's checks) allow the kill only, and TLC shows
// their counterexamples (docs/dev-tools.md).
module nas {
  const TARGETS: List[str]
  const RUNS: int
  const START: str            // the VM's state before the first run: "running" or "shutoff"
  const KEEPS: Set[bool]      // a run passes --keep-tag (true) or not
  const QUIESCE: Set[bool]    // a live run tries the quiesced snapshot first (agent connected, --quiesce)
  const MAX_STOPS: int        // stops in a trace (at most one per run: on_signal ignores the rest)
  const MAX_GUEST: int        // guest events in a trace (shutdown, pause, resume, a start during a read)
  const MAX_KILLS: int
  const MAX_USER: int         // the user starting or shutting down the VM between runs
  const ATOMIC: bool          // a settled snapshot answer switched every disk or none
  const KEEP: int             // the retention's newest kept (VM_NAS_KEEP_WEEKLY, one run a week)
  const SHUTOFF_ONLY: bool    // every run starts with the VM shut off (the user shuts it down between runs)

  pure val RUNNING = "running"
  pure val PAUSED = "paused"
  pure val SHUTOFF = "shutoff"
  pure val GOOD = "good"       // a snapshot that is a backup
  pure val TORN = "torn"       // incomplete, or of a shut-off VM started during the read
  pure val MIXED = "mixed"     // a live read with a disk not on its overlay (nas_partial only)

  type Snap = { id: int, kind: str, keep: bool }
  // ctx: the read's failure at a step of the merge back (for the witnesses; the replay does not read it)
  type Label = { name: str, target: str, id: int, outcome: str, stop: bool, ctx: str }
  type State = {
    // the machine, the repository and the records (they outlive a run)
    vm: str,
    live: str -> int,
    pers: str -> int,
    files: Set[(str, int)],
    repo: Set[Snap],
    record: Set[int],          // vm-nas-torn.json
    index: Set[int],           // the VM's NAS entries of the index
    // the run (reset at its start)
    r: int,
    pc: str,
    mode: str,                 // "live" or "shutoff"
    quiesce: bool,
    keep: bool,
    attempt: int,              // the snapshot attempt: 1 quiesced, 2 crash-consistent
    overlaid: Set[str],        // run.overlaid
    failure: str,              // the read's failure: "none", "refused", "uncertain", "stop"
    snap: int,                 // the restic snapshot the code knows (0: none)
    unrecorded: bool,          // forget_torn: the record failed
    problems: bool,            // the merge back's problems
    how: str -> str,           // the merge back's recovery kind of each target ("" none)
    todo: List[str],           // the targets of the loop at pc (drop, commit, remove)
    ids: List[int],            // the recorded ids retry_torn forgets
    ci: int,
    done: bool,                // retry_torn: an entry done with (to drop from the record)
    kept: bool,                // retry_torn: an entry kept (the retention waits)
    listOk: bool,              // the index's listing was read
    stopped: bool,             // this run had its stop
    end: str,                  // "", "ok", "refused", "stopped", "killed"
    // ghosts: not on the machine, for the properties
    unmerged: Set[(str, int)], // an overlay that holds writes its image lacks
    stale: Set[(str, int)],    // an overlay older than its image (a pivot left the definition on it)
    pending: Set[(str, int)],  // an overlay file libvirt may still switch to (a snapshot that timed out)
    noted: Set[(str, int)],    // an overlay whose removal failed after the disk was back (a note)
    alerted: Set[(str, int, str)], // the alert named the overlay with this recovery kind
    told: Set[int],            // a torn snapshot a message gave the user to forget by hand
    lostKill: Set[int],        // a torn snapshot left unrecorded by a kill
    lostStop: Set[int],        // a torn snapshot left unrecorded by a stop
    gap: int,                  // this run's torn snapshot not yet recorded or named (0: none)
    killed: Set[int],          // the runs that were killed
    started: bool,             // shut-off run: the VM was started since its sources were read
    made: bool,                // this run made an overlay file or switched a disk
    readyFailed: bool,         // this run's restic_ready failed
    unreachableBad: bool,      // a run whose restic_ready failed had made an overlay
    lostOverlay: bool,         // an overlay file removed while in use or unmerged
    stacked: bool,             // a snapshot taken while a disk was on an earlier overlay
    goodBad: bool,             // a retention forgot a good snapshot a torn one replaced
    goodBadFull: bool,
    keepBad: bool,             // a retention forgot a kept snapshot
    indexBad: bool,            // an index entry dropped without a readable listing, or a torn one indexed
    mixedIndexed: bool,        // a snapshot indexed whose read was not consistent
    skipped: bool,             // the retention was skipped (a recorded torn snapshot not forgotten)
    stops: int,
    guest: int,
    kills: int,
    user: int,
  }

  var s: State
  var step: Label

  pure val TSET = TARGETS.foldl(Set(), (acc, t) => acc.union(Set(t)))
  pure val PRE = Set("preflight", "ready")
  pure val READ = Set("snapshot", "drop", "backup", "after", "record", "forget", "drop_torn")
  pure val MERGE = Set("merge", "domstate", "commit", "check", "remove", "alert")
  pure val H2_WINDOW = Set("after", "record")
  pure val POST = Set("retry", "retry_forget", "retry_drop", "retention", "index", "write")

  pure def lbl(name: str, target: str, outcome: str): Label = { name: name, target: target, id: 0, outcome: outcome, stop: false, ctx: "" }
  pure def lblId(name: str, id: int, outcome: str): Label = { ...lbl(name, "", outcome), id: id }
  pure def stopped(l: Label): Label = { ...l, stop: true }

  def running(st: State): bool = st.pc != "idle"
  def ids(st: State): Set[int] = st.repo.map(x => x.id)
  def kindOf(st: State, i: int): str = st.repo.fold("", (acc, x) => if (x.id == i) x.kind else acc)
  def onOverlay(st: State, t: str): bool = st.live.get(t) != 0 or st.pers.get(t) != 0
  def fileTargets(st: State): Set[str] = TSET.filter(t => st.files.contains((t, st.r)))
  // An overlay file goes: in use (a definition names it, libvirt may still switch to it) or holding
  // writes the image lacks, it is data lost.
  def removeFile(st: State, t: str): State = {
    val o = (t, st.r)
    val inUse = st.live.get(t) == st.r or st.pers.get(t) == st.r or st.unmerged.contains(o) or st.pending.contains(o)
    { ...st, files: st.files.exclude(Set(o)), lostOverlay: st.lostOverlay or (st.files.contains(o) and inUse) }
  }

  // The end of a run: a torn snapshot it committed and a kill kept it from recording is the kill's gap.
  // What the run kept for itself is reset, so that the states between runs differ only in what outlives a run.
  def finish(st: State, end: str): State = {
    ...st, pc: "idle", end: end, gap: 0,
    mode: "", quiesce: false, keep: false, attempt: 0, overlaid: Set(), failure: "none", snap: 0,
    unrecorded: false, problems: false, how: TSET.mapBy(_ => ""), todo: [], ids: [], ci: 0, done: false,
    kept: false, listOk: false, stopped: false, started: false, made: false, skipped: false,
    unreachableBad: st.unreachableBad or (st.readyFailed and st.made), readyFailed: false,
    lostKill: if (end == "killed" and st.gap != 0) st.lostKill.union(Set(st.gap)) else st.lostKill,
    killed: if (end == "killed") st.killed.union(Set(st.r)) else st.killed,
  }
  // After the merge back: the alert for any problem (an unclear snapshot included), else the read's
  // failure raised again, else the retention and the index.
  def settle(st: State): State =
    if (st.problems or st.failure == "uncertain") { ...st, pc: "alert" }
    else if (st.failure == "stop") finish(st, "stopped")
    else if (st.failure == "refused") finish(st, "refused")
    else { ...st, pc: "retry" }
  // The read is over (or failed): a live run merges back; a shut-off run has nothing to merge.
  def toMerge(st: State, failure: str): State = {
    val f = { ...st, failure: failure }
    if (st.mode == "live") { ...f, pc: "merge" } else settle(f)
  }

  action init = all {
    s' = {
      vm: START, live: TSET.mapBy(_ => 0), pers: TSET.mapBy(_ => 0), files: Set(), repo: Set(),
      record: Set(), index: Set(),
      r: 0, pc: "idle", mode: "", quiesce: false, keep: false, attempt: 0, overlaid: Set(),
      failure: "none", snap: 0, unrecorded: false, problems: false, how: TSET.mapBy(_ => ""), todo: [],
      ids: [], ci: 0, done: false, kept: false, listOk: false, stopped: false, end: "",
      unmerged: Set(), stale: Set(), pending: Set(), noted: Set(), alerted: Set(), told: Set(),
      lostKill: Set(), lostStop: Set(), gap: 0, killed: Set(), started: false, made: false,
      readyFailed: false, unreachableBad: false, lostOverlay: false, stacked: false, goodBad: false, goodBadFull: false,
      keepBad: false, indexBad: false, mixedIndexed: false, skipped: false,
      stops: 0, guest: 0, kills: 0, user: 0,
    },
    step' = lbl("~init", "", ""),
  }

  // ---- backup_nas: a run starts (the VM's lock taken); a paused VM is refused before anything ---------
  action startRun = all {
    s.pc == "idle",
    s.r < RUNS,
    not(SHUTOFF_ONLY) or s.vm == SHUTOFF,
    nondet keep = KEEPS.oneOf()
    nondet quiesce = QUIESCE.oneOf()
    val st = {
      ...s, r: s.r + 1, mode: if (s.vm == SHUTOFF) "shutoff" else "live", quiesce: quiesce, keep: keep,
      attempt: if (quiesce) 1 else 2, overlaid: Set(), failure: "none", snap: 0, unrecorded: false,
      problems: false, how: TSET.mapBy(_ => ""), todo: [], ids: [], ci: 0, done: false, kept: false,
      listOk: false, stopped: false, end: "", gap: 0, started: false, made: false, readyFailed: false,
      skipped: false,
      // libvirt's late switch comes before the next run (the settle window is minutes, a run a week)
      pending: Set(),
    }
    if (s.vm == PAUSED) all { s' = finish(st, "refused"), step' = lbl("~start", "", "refused") }
    else all { s' = { ...st, pc: "preflight" }, step' = lbl("~start", "", st.mode) },
  }

  // _nas_preflight: a disk the definition (or, running, the VM) has on an overlay is refused
  // (SourceWalk, _nas_live_specs), nothing changed.
  action preflight = all {
    s.pc == "preflight",
    val leftover = TSET.exists(t => s.pers.get(t) != 0 or (s.mode == "live" and s.live.get(t) != 0)) // PS7
    if (leftover) all { s' = finish(s, "refused"), step' = lbl("preflight", "", "refused") }
    else all { s' = { ...s, pc: "ready" }, step' = lbl("preflight", "", "ok") },
  }

  // restic_ready: restic cat config; a repository that does not open refuses, nothing changed.
  action ready = all {
    s.pc == "ready",
    nondet ok = Set(true, false).oneOf()
    if (ok) all { s' = { ...s, pc: if (s.mode == "live") "snapshot" else "backup" }, step' = lbl("restic_ready", "", "ok") } // PS4
    else all { s' = finish({ ...s, readyFailed: true }, "refused"), step' = lbl("restic_ready", "", "fail") },
  }

  // take_snapshot, one _snapshot_attempt: virsh snapshot-create-as with the stops held, then
  // domblklist (polled until the settle window ends when virsh timed out). The outcome: every disk
  // switched (ok; a settled error with every disk in place, fail_in_place; a timeout, timeout_all),
  // none (fail_none: a settled error), or a timeout with some not switched (timeout_some: Uncertain).
  // A file libvirt made for a disk it did not switch stays; after a timeout libvirt may still switch to it.
  pure val SNAP_OUTCOMES = Set("ok", "fail_none", "fail_in_place", "timeout_all", "timeout_some", "fail_some", "ok_some")
  action snapshot = all {
    s.pc == "snapshot",
    nondet outcome = SNAP_OUTCOMES.oneOf()
    nondet sw = TSET.powerset().oneOf()
    nondet madeOnly = TSET.powerset().oneOf()
    nondet stopIn = Set(false, true).oneOf()
    val all_ = Set("ok", "fail_in_place", "timeout_all").contains(outcome)
    val some = Set("fail_some", "ok_some").contains(outcome)
    all {
      if (all_) sw == TSET
      else if (outcome == "fail_none") sw == Set()
      else if (outcome == "timeout_some") sw != TSET
      else not(ATOMIC) and sw != Set() and sw != TSET,
      // --atomic: a settled failure leaves no file either (libvirt removes what it made)
      if (all_ or (ATOMIC and outcome != "timeout_some")) madeOnly == Set() else madeOnly.intersect(sw) == Set(),
      not(stopIn) or (not(s.stopped) and s.stops < MAX_STOPS),
      val r = s.r
      val ov = s.overlaid.union(sw)
      val settled = not(Set("timeout_all", "timeout_some").contains(outcome))
      val st = {
        ...s,
        live: TSET.mapBy(t => if (sw.contains(t)) r else s.live.get(t)),
        pers: TSET.mapBy(t => if (sw.contains(t)) r else s.pers.get(t)),
        files: s.files.union(sw.union(madeOnly).map(t => (t, r))),
        unmerged: s.unmerged.union(sw.map(t => (t, r))),
        pending: if (outcome == "timeout_some") s.pending.union(madeOnly.map(t => (t, r))) else s.pending,
        overlaid: ov,
        made: s.made or sw.union(madeOnly) != Set(),
        stacked: s.stacked or TSET.exists(t => onOverlay(s, t)),
        stopped: s.stopped or stopIn,
        stops: if (stopIn) s.stops + 1 else s.stops,
      }
      val label = { ...lbl("snapshot", if (s.attempt == 1) "quiesced" else "crash", outcome), stop: stopIn }
      all {
        // An unclear outcome is the read's failure even when a stop waited for the block's end (the
        // stop is then the backup's own failure in the refusal).
        s' = if (not(settled) and ov != TSET) toMerge(st, "uncertain") // PS6
             else if (stopIn) toMerge(st, "stop")
             else if (ov != Set()) { ...st, pc: "backup" }
             else { ...st, pc: "drop", todo: TARGETS.select(t => not(ov.contains(t))), ci: 0 },
        step' = label,
      }
    }
  }
  // _drop_stray_overlays: the overlay files of the disks that did not switch (after a settled
  // error: not in use), then the crash-consistent attempt, or the refusal after it.
  action drop = all {
    s.pc == "drop",
    val t = s.todo[s.ci]
    val st = removeFile(s, t)
    all {
      s' = if (s.ci + 1 < s.todo.length()) { ...st, ci: s.ci + 1 }
           else if (s.attempt == 1) { ...st, pc: "snapshot", attempt: 2 }
           else toMerge(st, "refused"),
      step' = lbl("drop_overlay", t, ""),
    }
  }

  // _nas_restic_backup: restic backup; an incomplete snapshot (an id, exit 3) goes through
  // forget_torn; no id refuses. A stop after restic committed its snapshot ends restic, and the id
  // is lost to the code (Restic.backup raises the stop, the summary dropped).
  action backup = all {
    s.pc == "backup",
    nondet outcome = Set("ok", "incomplete", "fail", "stopped_after_commit").oneOf()
    val r = s.r
    val read = if (s.mode == "live") { if (s.overlaid == TSET) GOOD else MIXED } else { if (s.started) TORN else GOOD }
    val kind = if (outcome == "incomplete") TORN else read
    val st = { ...s, repo: s.repo.union(Set({ id: r, kind: kind, keep: s.keep })), gap: if (kind == TORN) r else 0 }
    all {
      outcome != "stopped_after_commit" or (not(s.stopped) and s.stops < MAX_STOPS),
      s' = if (outcome == "ok") { if (s.mode == "live") toMerge({ ...st, snap: r }, "none") else { ...st, snap: r, pc: "after" } }
           else if (outcome == "incomplete") { ...st, snap: r, pc: "record" }
           else if (outcome == "fail") toMerge(s, "refused")
           // H1: the id is lost to the code; a torn one stays in the repository, neither recorded nor named
           else toMerge({ ...st, stopped: true, stops: s.stops + 1, gap: 0,
                          lostStop: if (kind == TORN) s.lostStop.union(Set(r)) else s.lostStop }, "stop"),
      step' = lbl("restic_backup", "", outcome),
    }
  }

  // _nas_check_after_read, shut-off VM: started meanwhile, or a source changed: its snapshot through forget_torn.
  action after = all {
    s.pc == "after",
    if (s.started) all { s' = { ...s, pc: "record" }, step' = lbl("check_after", "", "changed") }
    else all { s' = toMerge(s, "none"), step' = lbl("check_after", "", "same") },
  }

  // forget_torn: the record first (the stops held: a stop then waits for it), restic forget of the
  // one id, then the record's entry dropped. Each failure is said; a stop after a record that failed
  // names the snapshot.
  action record = all {
    s.pc == "record",
    nondet ok = Set(true, false).oneOf()
    nondet stopIn = Set(false, true).oneOf()
    val st = if (ok) { ...s, record: s.record.union(Set(s.snap)), gap: 0 } else { ...s, unrecorded: true }
    val st2 = if (stopIn) { ...st, stopped: true, stops: s.stops + 1 } else st
    all {
      not(stopIn) or (not(s.stopped) and s.stops < MAX_STOPS),
      s' = if (stopIn and not(ok)) toMerge({ ...st2, told: st2.told.union(Set(s.snap)), gap: 0 }, "stop") // PS3
           else if (stopIn) toMerge(st2, "stop")
           else { ...st2, pc: "forget" },
      step' = { ...lbl("record_torn", "", if (ok) "ok" else "fail"), id: s.snap, stop: stopIn },
    }
  }
  action forget = all {
    s.pc == "forget",
    nondet ok = Set(true, false).oneOf()
    if (ok) all {
      val st = { ...s, repo: s.repo.filter(x => x.id != s.snap), gap: 0 }
      s' = if (s.unrecorded) toMerge(st, "refused") else { ...st, pc: "drop_torn" },
      step' = lblId("forget", s.snap, "ok"),
    } else all {
      s' = toMerge(if (s.unrecorded) { ...s, told: s.told.union(Set(s.snap)), gap: 0 } else s, "refused"),
      step' = lblId("forget", s.snap, "fail"),
    },
  }
  action dropTorn = all {
    s.pc == "drop_torn",
    nondet ok = Set(true, false).oneOf()
    all {
      s' = toMerge(if (ok) { ...s, record: s.record.exclude(Set(s.snap)) } else s, "refused"),
      step' = lblId("drop_torn", s.snap, if (ok) "ok" else "fail"),
    }
  }

  // ---- _nas_merge_back, the stops ignored ---------------------------------------------------------
  // find_overlays: what the VM or its definition runs on, of this run's overlays.
  action merge = all {
    s.pc == "merge",
    val ov = s.overlaid.union(TSET.filter(t => s.live.get(t) == s.r or s.pers.get(t) == s.r))
    val st = { ...s, overlaid: ov }
    all {
      s' = if (ov == Set()) settle(st) else { ...st, pc: "domstate" },
      step' = lbl("find_overlays", "", ""),
    }
  }
  // commit_back: domstate; a VM not alive merges nothing (each overlay offline, or unknown when the
  // state cannot be read).
  action domstate = all {
    s.pc == "domstate",
    nondet readable = Set(true, false).oneOf()
    val state = if (readable) s.vm else "unreadable"
    if (state == RUNNING or state == PAUSED) all {
      s' = { ...s, pc: "commit", todo: TARGETS.select(t => s.overlaid.contains(t)), ci: 0 },
      step' = lbl("domstate", "", "live"),
    } else all {
      val kind = if (state == SHUTOFF) "offline" else "unknown"
      s' = { ...s, problems: true, how: TSET.mapBy(t => if (s.overlaid.contains(t)) kind else s.how.get(t)), pc: "alert" },
      step' = lbl("domstate", "", if (state == SHUTOFF) "stopped" else "unreadable"),
    },
  }
  // blockcommit --active --pivot per overlay: the disk back on its image (libvirt updates the
  // persistent definition, or leaves it on the overlay, which is stale from then on), or virsh's
  // timeout (it stays on the overlay). A VM that shut down meanwhile fails it.
  action commit = all {
    s.pc == "commit",
    nondet ok = Set(true, false).oneOf()
    nondet persUpdated = Set(true, false).oneOf()
    val t = s.todo[s.ci]
    val o = (t, s.r)
    all {
      not(ok) or s.vm != SHUTOFF,
      ok or persUpdated,
      val st = if (ok) {
        ...s, live: s.live.set(t, 0), unmerged: s.unmerged.exclude(Set(o)),
        pers: if (persUpdated) s.pers.set(t, 0) else s.pers,
        stale: if (persUpdated) s.stale else s.stale.union(Set(o)),
      } else { ...s, problems: true }
      all {
        s' = if (s.ci + 1 < s.todo.length()) { ...st, ci: s.ci + 1 } else { ...st, pc: "check" },
        step' = lbl("blockcommit", t, if (ok) "ok" else if (persUpdated) "fail" else "fail"),
      }
    }
  }
  // domblklist, live and --inactive: an overlay goes only where both listings show the image; else
  // a problem with the kind that puts the disk back (live: still on the overlay; definition: only
  // the definition names it, stale now).
  action check = all {
    s.pc == "check",
    nondet lok = Set(true, false).oneOf()
    nondet pok = Set(true, false).oneOf()
    def kindOfT(t: str): str =
      if (not(lok)) "live"
      else if (s.live.get(t) != 0) "live"
      else if (pok and s.pers.get(t) != 0) "definition" // PS1
      else if (not(pok)) "definition" // PS1b
      else ""
    val bad = TSET.filter(t => s.overlaid.contains(t) and kindOfT(t) != "")
    val st = {
      ...s, problems: s.problems or bad != Set(),
      how: TSET.mapBy(t => if (bad.contains(t)) kindOfT(t) else s.how.get(t)),
    }
    val rm = TARGETS.select(t => s.overlaid.contains(t) and not(bad.contains(t)))
    all {
      s' = if (rm.length() > 0) { ...st, pc: "remove", todo: rm, ci: 0 } else settle(st),
      step' = lbl("domblklist", "", if (lok and pok) "ok" else if (lok) "inactive_unreadable" else if (pok) "live_unreadable" else "unreadable"),
    }
  }
  action remove = all {
    s.pc == "remove",
    nondet ok = Set(true, false).oneOf()
    val t = s.todo[s.ci]
    val st = if (ok) removeFile(s, t) else { ...s, noted: s.noted.union(Set((t, s.r))) }
    all {
      s' = if (s.ci + 1 < s.todo.length()) { ...st, ci: s.ci + 1 } else settle(st),
      step' = { ...lbl("remove_overlay", t, if (ok) "ok" else "fail"), ctx: s.failure },
    }
  }
  // _nas_not_merged_back: the critical alert, then Refused with the recovery lines: each overlay the
  // run knows (with the files of an unclear snapshot, or every file when it knows none), but none of
  // the disks the merge back put back. Those are the known overlays with no recovery kind: by the
  // alert, domstate or the check gave every other one its kind. When that leaves none, all of them
  // (their look-first commands, kind "unknown").
  action alert = all {
    s.pc == "alert",
    val known = if (s.failure == "uncertain") s.overlaid.union(fileTargets(s)) else s.overlaid // PS10
    val back = s.overlaid.filter(t => s.how.get(t) == "")
    val base = if (known == Set()) fileTargets(s) else known
    val left = base.exclude(back)
    val named = if (left == Set()) base else left
    def kind(t: str): str = if (s.how.get(t) == "") "unknown" else s.how.get(t)
    all {
      s' = finish({ ...s, alerted: s.alerted.union(named.map(t => (t, s.r, kind(t)))) }, "refused"),
      step' = { ...lbl("alert", "", ""), ctx: s.failure },
    }
  }

  // ---- after a good read: retention_after_torn, then the index ----------------------------------
  // retry_torn: the recorded torn snapshots restic still lists are forgotten; one it no longer lists
  // leaves the record; any left (or a listing not read) skips the retention.
  action retry = all {
    s.pc == "retry",
    if (s.record == Set()) all { s' = { ...s, pc: "retention" }, step' = lbl("~no_torn", "", "") }
    else {
      nondet ok = Set(true, false).oneOf()
      if (not(ok)) all {
        s' = { ...s, pc: "index", skipped: true },
        step' = lbl("restic_snapshots", "torn", "fail"),
      } else {
        val listed = range(1, RUNS + 1).select(i => s.record.contains(i) and ids(s).contains(i))
        val gone = s.record.exists(i => not(ids(s).contains(i)))
        val st = { ...s, done: gone, kept: false, ids: listed, ci: 0 }
        all {
          s' = if (listed.length() > 0) { ...st, pc: "retry_forget" } else { ...st, pc: "retry_drop" },
          step' = lbl("restic_snapshots", "torn", "ok"),
        }
      }
    },
  }
  action retryForget = all {
    s.pc == "retry_forget",
    nondet ok = Set(true, false).oneOf()
    val i = s.ids[s.ci]
    val st = if (ok) { ...s, repo: s.repo.filter(x => x.id != i), done: true } else { ...s, kept: true }
    all {
      s' = if (s.ci + 1 < s.ids.length()) { ...st, ci: s.ci + 1 } else { ...st, pc: "retry_drop" },
      step' = { ...lblId("forget", i, if (ok) "ok" else "fail"), target: "retry" },
    }
  }
  action retryDrop = all {
    s.pc == "retry_drop",
    val gone = s.record.filter(i => not(ids(s).contains(i)))
    if (not(s.done)) all {
      s' = { ...s, pc: if (s.kept) "index" else "retention", skipped: s.kept },
      step' = lbl("~no_drop", "", ""),
    } else {
      nondet ok = Set(true, false).oneOf()
      val kept = s.kept or not(ok)
      all {
        s' = { ...s, record: if (ok) s.record.exclude(gone) else s.record, kept: kept,
               pc: if (kept) "index" else "retention", skipped: kept },
          step' = lbl("drop_torn", "retry", if (ok) "ok" else "fail"),
      }
    }
  }
  // retention: restic forget --keep-weekly (KEEP newest, one run a week) --keep-tag; a failure is a note.
  action retention = all {
    s.pc == "retention",
    nondet ok = Set(true, false).oneOf()
    val all_ids = ids(s)
    val newest = all_ids.filter(i => all_ids.filter(j => j > i).size() < KEEP)
    val kept = s.repo.filter(x => x.keep or newest.contains(x.id))
    val gone = s.repo.exclude(kept)
    def goodNewer(g: Snap): int = kept.filter(k => k.kind == GOOD and k.id > g.id).size()
    val replaced = gone.exists(g => g.kind == GOOD and goodNewer(g) < KEEP)
    def lostTorn(lost: Set[int]): bool = s.repo.exists(x => x.kind == TORN and lost.contains(x.id))
    all {
      s' = if (ok) {
        ...s, repo: kept, pc: "index",
        goodBad: s.goodBad or (replaced and not(lostTorn(s.told.union(s.lostKill).union(s.lostStop)))),
        goodBadFull: s.goodBadFull or (replaced and not(lostTorn(s.told.union(s.lostKill)))),
        keepBad: s.keepBad or gone.exists(g => g.keep),
      } else { ...s, pc: "index" },
      step' = lbl("retention", "", if (ok) "ok" else "fail"),
    }
  }
  // _index_nas_backup: restic snapshots (the VM's ids), then the index with the new entry and without
  // the entries restic no longer lists (none dropped when the listing was not read).
  action index = all {
    s.pc == "index",
    nondet ok = Set(true, false).oneOf()
    all { s' = { ...s, listOk: ok, pc: "write" }, step' = lbl("restic_snapshots", "index", if (ok) "ok" else "fail") },
  }
  action write = all {
    s.pc == "write",
    val alive = ids(s).union(Set(s.snap))
    val added = s.index.union(Set(s.snap))
    val idx = if (s.listOk) added.intersect(alive) else added // PS8
    all {
      s' = finish({
        ...s, index: idx,
        indexBad: s.indexBad or kindOf(s, s.snap) == TORN or (not(s.listOk) and idx != added),
        mixedIndexed: s.mixedIndexed or kindOf(s, s.snap) != GOOD,
      }, "ok"),
      step' = lblId("write_index", s.snap, "ok"),
    }
  }

  // ---- the environment ---------------------------------------------------------------------------
  // A stop (SIGTERM, SIGINT, SIGHUP) between two steps, outside a held block.
  action stop = all {
    running(s),
    not(s.stopped),
    s.stops < MAX_STOPS,
    not(MERGE.contains(s.pc)), // PS5
    val st = { ...s, stopped: true, stops: s.stops + 1 }
    // H2: a stop after restic's answer (the staging folder's removal, the shut-off VM's check) and
    // before forget_torn's held record leaves a torn snapshot neither recorded nor named
    val h2 = H2_WINDOW.contains(s.pc) and s.gap != 0
    all {
      s' = if (READ.contains(s.pc)) {
             // forget_torn's "except Stop": a snapshot it could not record is named
             val named = s.pc == "forget" and s.unrecorded
             val st2 = if (h2) { ...st, lostStop: st.lostStop.union(Set(s.gap)), gap: 0 } else st
             toMerge(if (named) { ...st2, told: st2.told.union(Set(s.snap)), gap: 0 } else st2, "stop")
           }
           else finish(st, "stopped"),
      step' = lbl("~stop", "", s.pc),
    }
  }
  action kill = all {
    running(s),
    s.kills < MAX_KILLS,
    s' = finish({ ...s, kills: s.kills + 1 }, "killed"),
    step' = lbl("~kill", "", s.pc),
  }
  // The guest during a run: a live run's VM shuts down (its live listing is the definition's then;
  // libvirt switches nothing more), pauses or resumes; a shut-off run's VM is started.
  action guest = all {
    running(s),
    s.guest < MAX_GUEST,
    nondet ev = Set("shutdown", "pause", "resume", "start").oneOf()
    val g = { ...s, guest: s.guest + 1 }
    if (ev == "shutdown") all {
      s.mode == "live", s.vm != SHUTOFF,
      s' = { ...g, vm: SHUTOFF, live: s.pers, pending: Set() }, step' = lbl("~guest", "", ev),
    } else if (ev == "pause") all {
      s.mode == "live", s.vm == RUNNING,
      s' = { ...g, vm: PAUSED }, step' = lbl("~guest", "", ev),
    } else if (ev == "resume") all {
      s.mode == "live", s.vm == PAUSED,
      s' = { ...g, vm: RUNNING }, step' = lbl("~guest", "", ev),
    } else all {
      s.mode == "shutoff", s.vm == SHUTOFF,
      s' = { ...g, vm: RUNNING, live: s.pers, started: s.started or Set("preflight", "ready", "backup", "after").contains(s.pc) },
      step' = lbl("~guest", "", ev),
    },
  }
  // libvirt switches a disk late to a file of a snapshot that timed out (during its run, or before the next).
  action lateSwitch = all {
    s.pending != Set(),
    nondet o = s.pending.oneOf()
    val t = o._1
    val r = o._2
    all {
      s' = { ...s, live: s.live.set(t, r), pers: s.pers.set(t, r), unmerged: s.unmerged.union(Set(o)),
             pending: s.pending.exclude(Set(o)), lostOverlay: s.lostOverlay or not(s.files.contains(o)) },
      step' = lbl("~late_switch", t, ""),
    }
  }
  // The user between runs: starts the VM (from its definition) or shuts it down.
  action user = all {
    s.pc == "idle",
    s.user < MAX_USER,
    nondet ev = Set("start", "shutdown").oneOf()
    if (ev == "start") all {
      s.vm == SHUTOFF, not(SHUTOFF_ONLY),
      s' = { ...s, vm: RUNNING, live: s.pers, user: s.user + 1 }, step' = lbl("~user", "", ev),
    } else all {
      s.vm != SHUTOFF,
      s' = { ...s, vm: SHUTOFF, live: s.pers, pending: Set(), user: s.user + 1 }, step' = lbl("~user", "", ev),
    },
  }

  action step_ = any {
    startRun, preflight, ready, snapshot, drop, backup, after, record, forget, dropTorn,
    merge, domstate, commit, check, remove, alert, retry, retryForget, retryDrop, retention, index, write,
    stop, kill, guest, lateSwitch, user,
  }
  // The protocol alone (no stop, kill, guest or user): the checked-in traces that need several runs
  // to reach a witness (a torn snapshot recorded, then retried) are walks of it, each step one of step_'s.
  action step_calm = any {
    startRun, preflight, ready, snapshot, drop, backup, after, record, forget, dropTorn,
    merge, domstate, commit, check, remove, alert, retry, retryForget, retryDrop, retention, index, write,
  }

  // ---- properties ----------------------------------------------------------------------------------
  // After a run, however it ends (a kill aside), each disk is back on its image in both definitions
  // with its overlay file removed (or a note says it is left), or the alert named the overlay.
  val mergedOrAlerted = (s.pc == "idle" and s.r > 0 and s.end != "killed") implies TSET.forall(t => {
    val o = (t, s.r)
    val merged = s.live.get(t) != s.r and s.pers.get(t) != s.r and (not(s.files.contains(o)) or s.noted.contains(o))
    merged or Set("live", "definition", "offline", "unknown").exists(k => s.alerted.contains((t, s.r, k)))
  })
  // An overlay file is never removed while a definition names it, libvirt may still switch to it, or it
  // holds writes the image lacks; libvirt never switches to one that was removed.
  val noOverlayLost = not(s.lostOverlay)
  // The definition never names a stale overlay unless the alert named it, and never as "offline"
  // (qemu-img commit of a stale overlay overwrites newer data), or the run that left it was killed.
  val staleNamed = s.pc == "idle" implies s.stale.forall(o => s.pers.get(o._1) != o._2 or s.killed.contains(o._2) or
    Set("live", "definition", "unknown").exists(k => s.alerted.contains((o._1, o._2, k))))
  // A run whose repository did not open made no overlay and changed no definition.
  val unreachableNoOverlay = not(s.unreachableBad or (s.readyFailed and s.made))
  // A snapshot is never taken while a disk is on an earlier run's overlay (a killed run's included).
  val noStacking = not(s.stacked)
  // Every torn snapshot in the repository is recorded (the next run forgets it), or a message named
  // it, or a kill or a stop left it (the gaps), or its run is still on it.
  def tornHandledBy(lost: Set[int]): bool = s.repo.forall(x => x.kind != TORN or s.record.contains(x.id) or
    s.told.contains(x.id) or lost.contains(x.id) or (running(s) and x.id == s.r))
  val tornHandled = tornHandledBy(s.lostKill.union(s.lostStop))
  val tornHandledFull = tornHandledBy(s.lostKill)
  // The index never names a torn snapshot, and an entry leaves only when restic's listing was read.
  val indexClean = not(s.indexBad)
  // The retention never forgets a good snapshot while fewer than KEEP good ones newer than it stay,
  // unless a torn snapshot a message named, or a gap left, is in the repository.
  val goodKept = not(s.goodBad)
  val goodKeptFull = not(s.goodBadFull)
  // A snapshot made with --keep-tag is never forgotten by the retention.
  val keepKept = not(s.keepBad)
  // A snapshot is indexed (a backup) only when its read was consistent.
  val consistentRead = not(s.mixedIndexed)
  // After an index write with a listing read, every index entry names a snapshot in the repository.
  val indexInRepo = (step.name == "write_index" and s.listOk) implies s.index.forall(i => ids(s).contains(i))

  // Witnesses: the parts of the protocol a trace reaches (for the checked-in traces).
  val wStrayDropped = step.name == "drop_overlay"
  val wTimeoutAll = step.name == "snapshot" and step.outcome == "timeout_all"
  val wUncertainAlerted = step.name == "alert" and step.ctx == "uncertain"
  val wStopInResticMerged = step.name == "remove_overlay" and step.ctx == "stop"
  val wStopHeldSnapshot = step.name == "snapshot" and step.stop
  val wOffline = step.name == "alert" and s.alerted.exists(a => a._2 == s.r and a._3 == "offline")
  val wDefinition = step.name == "alert" and s.alerted.exists(a => a._2 == s.r and a._3 == "definition")
  val wCommitFailed = step.name == "blockcommit" and step.outcome == "fail"
  val wTornForgotten = step.name == "drop_torn" and step.outcome == "ok" and step.target == ""
  val wTornRetried = step.name == "forget" and step.outcome == "ok" and step.target == "retry"
  val wRetentionSkipped = s.skipped and step.name == "restic_snapshots" and step.target == "index"
  val wKilledRefused = step.name == "preflight" and step.outcome == "refused" and s.killed != Set()
  val wStartedDuringRead = step.name == "check_after" and step.outcome == "changed"
  val wLateSwitch = step.name == "~late_switch"
  val wIndexed = step.name == "write_index"
  val wShutOffSame = step.name == "check_after" and step.outcome == "same"
  val wReadyFailed = step.name == "restic_ready" and step.outcome == "fail"
  val wListingsUnreadable = step.name == "domblklist" and step.outcome == "unreadable"
  val wLiveListingUnreadable = step.name == "domblklist" and step.outcome == "live_unreadable"
  val wRemoveFailed = step.name == "remove_overlay" and step.outcome == "fail"
  val wStopAfterCommit = step.name == "restic_backup" and step.outcome == "stopped_after_commit"
  val wRetentionFailed = step.name == "retention" and step.outcome == "fail"
  val wTornForgetFailed = step.name == "forget" and step.outcome == "fail" and step.target == ""
  val wTornDropFailed = step.name == "drop_torn" and step.outcome == "fail" and step.target == ""
  val wTornListFailed = step.name == "restic_snapshots" and step.target == "torn" and step.outcome == "fail"
  val neverStrayDropped = not(wStrayDropped)
  val neverTimeoutAll = not(wTimeoutAll)
  val neverUncertainAlerted = not(wUncertainAlerted)
  val neverStopInResticMerged = not(wStopInResticMerged)
  val neverStopHeldSnapshot = not(wStopHeldSnapshot)
  val neverOffline = not(wOffline)
  val neverDefinition = not(wDefinition)
  val neverCommitFailed = not(wCommitFailed)
  val neverTornForgotten = not(wTornForgotten)
  val neverTornRetried = not(wTornRetried)
  val neverRetentionSkipped = not(wRetentionSkipped)
  val neverKilledRefused = not(wKilledRefused)
  val neverStartedDuringRead = not(wStartedDuringRead)
  val neverLateSwitch = not(wLateSwitch)
  val neverIndexed = not(wIndexed)
  val neverShutOffSame = not(wShutOffSame)
  val neverReadyFailed = not(wReadyFailed)
  val neverListingsUnreadable = not(wListingsUnreadable)
  val neverLiveListingUnreadable = not(wLiveListingUnreadable)
  val neverRemoveFailed = not(wRemoveFailed)
  val neverStopAfterCommit = not(wStopAfterCommit)
  val neverRetentionFailed = not(wRetentionFailed)
  val neverTornForgetFailed = not(wTornForgetFailed)
  val neverTornDropFailed = not(wTornDropFailed)
  val neverTornListFailed = not(wTornListFailed)
}

// Instances. KEEP = 2: a torn snapshot can then take a good one's place among those kept.
// A running VM, one disk, three runs (the quiesced attempt first, then the crash-consistent one).
module nas_live1 {
  import nas(TARGETS = ["vda"], RUNS = 3, START = "running", KEEPS = Set(false), QUIESCE = Set(true),
             MAX_STOPS = 1, MAX_GUEST = 1, MAX_KILLS = 1, MAX_USER = 0, ATOMIC = true, KEEP = 2, SHUTOFF_ONLY = false).*
}
// Two disks (a timeout with one switched, one commit failing of two), two runs.
module nas_live2 {
  import nas(TARGETS = ["vda", "vdb"], RUNS = 2, START = "running", KEEPS = Set(false), QUIESCE = Set(false),
             MAX_STOPS = 1, MAX_GUEST = 1, MAX_KILLS = 1, MAX_USER = 0, ATOMIC = true, KEEP = 2, SHUTOFF_ONLY = false).*
}
// A shut-off VM, three runs, --keep-tag or not: torn snapshots across runs, the retry and the retention.
module nas_shutoff {
  import nas(TARGETS = ["vda"], RUNS = 3, START = "shutoff", KEEPS = Set(false, true), QUIESCE = Set(false),
             MAX_STOPS = 2, MAX_GUEST = 2, MAX_KILLS = 1, MAX_USER = 2, ATOMIC = true, KEEP = 2, SHUTOFF_ONLY = true).*
}
// The VM's state chosen between runs by the user: a killed or alerted live run, then the next one.
module nas_mixed {
  import nas(TARGETS = ["vda"], RUNS = 2, START = "running", KEEPS = Set(false), QUIESCE = Set(false),
             MAX_STOPS = 1, MAX_GUEST = 1, MAX_KILLS = 1, MAX_USER = 2, ATOMIC = true, KEEP = 2, SHUTOFF_ONLY = false).*
}
// Not in cst-quint's checks: nas_live2 without --atomic, where a settled answer can switch some disks
// only (consistentRead fails: the unswitched disk is read as the guest writes it).
module nas_partial {
  import nas(TARGETS = ["vda", "vdb"], RUNS = 2, START = "running", KEEPS = Set(false), QUIESCE = Set(false),
             MAX_STOPS = 1, MAX_GUEST = 1, MAX_KILLS = 1, MAX_USER = 0, ATOMIC = false, KEEP = 2, SHUTOFF_ONLY = false).*
}
