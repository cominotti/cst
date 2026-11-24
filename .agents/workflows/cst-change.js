// SPDX-License-Identifier: MPL-2.0
export const meta = {
  name: 'cst-change',
  description: 'One CST change: design, implement, simplify, findings-only review (a fast lane for a small change of docs, tests and tooling), one independent fixer, a re-review of its code (branch ready)',
  whenToUse: 'A CST change worth a reviewed branch: a command, a fix, a refactor, a port. Integration and merge stay with the orchestrator.',
  phases: [
    { title: 'Design', detail: 'brief + advisor review (the spec review)' },
    { title: 'Implement', detail: 'worktree, commits of one kind, check green' },
    { title: 'Simplify', detail: 'quality pass by hand, in the worktree' },
    { title: 'Review', detail: 'findings only: code-review high <branch> + checklist lenses by path, at most 3 agents at once' },
    { title: 'Fix', detail: 'one independent agent applies confirmed findings, runs the gates once' },
    { title: 'Fast lane', detail: 'instead of Review and Fix for a small change of docs, tests and tooling: one agent reviews by hand and fixes' },
    { title: 'Re-review', detail: 'when the fixer changed code outside tests: findings only, by hand, on its commits; a high finding stops the run' },
  ],
}
//
// The CST per-change workflow (Workflow tool: name "cst-change", from .claude/workflows/).
// It ends at "branch ready" (result.ready): integration (one branch, one CI run with tier 2, a merge commit on
// the pull request, AGENTS.md "CI is the gate") stays with the orchestrator.
//
// args (JSON):
//   main      absolute path of the main checkout (required: the script cannot run a shell)
//   slug      [a-z0-9-]; the worktree defaults to <main>-wt-<slug>, the branch to change/<slug>
//   title     one line
//   kind      "refactor" | "behaviour" | "docs" (no CST behaviour: docs, agent tooling, CI config; it may not touch
//             what CST installs or runs)   (.agents/rules/refactor-vs-behaviour.md picks the proof)
//   brief     absolute path of the brief (may live outside the repository)
//   notes     absolute path of a folder for the design note and reports (outside the repository)
//   base      git ref to start from (default "main")
//   branch, worktree   optional overrides (to review or continue an existing branch)
//   tier1     "run" (default) | "defer": a batch runs tier 1 once on its integration branch; the result says so.
//             Prefer "run": the fixer then runs the whole tier (about 3 minutes), which also catches tests that
//             depend on order; with "defer", tier 1 failures surfaced only at integration (2026-11-23).
//   mode      "new" (default) | "continue" (the worktree exists) | "review" (review + fix of an existing branch)
//             | "selftest" (only the self-tests below)
//
// The lane is the script's choice, not an argument (fullReviewReason): a change of at most FAST_LINES changed lines
// (git diff --numstat) that touches only Markdown, unit tests and repository tooling (REPO_ONLY), with no checklist
// lens beyond primitives, takes the fast lane: one agent that did not write it reviews it by hand, fixes and runs
// the gates once; no code-review fork, no lens agents. Anything else takes the full review.
// The fixer (in both lanes) applies every confirmed finding and defers one only when it suspects a redesign would be
// significantly more appropriate than a local fix, never by size or count; the result's deferred lists each with
// its reason, and a deferral without one makes the branch not ready.
// In both lanes the result's needs_review is true when the fixer says its commits change code outside tests or when
// a path it changed is neither Markdown nor a test: in the fast lane the reviewer is the fixer, so its word is not
// enough.
// When it is true and the branch is otherwise ready, the workflow reviews the fixer's commits itself (Re-review:
// one findings-only agent, by hand, on reviewed_head..head; the orchestrator no longer does): a "high" finding
// stops the run (ready false, stopped "re-review"), the others go to the result's rereview.low.
//
// Facts this script is built on (checked 2026-11-18; recheck after a Claude Code upgrade):
// - A workflow agent has the Skill tool (and the advisor) but not Agent or Workflow; it loads AGENTS.md and the
//   rules, not the conversation. Its shell starts in the MAIN checkout on every call (cd does not persist), so
//   every command below names the worktree (git -C <worktree>, cd <worktree> && ... in the same call).
//   Rechecked 2026-11-19 by an implement agent: the same tools (tools.list(): Skill, no Agent or Workflow; the
//   advisor answered), and pwd in the main checkout on each call. Also seen then: the AGENTS.md and rules in
//   the agent's context can be older than main's (it had AGENTS.md without the magic-values rule and the
//   ResourceWarning gotcha, and a reuse-primitives.md without the cst_core.domain.shapes rows, all on main),
//   so an agent reads the files in its worktree; the prompts' "AGENTS.md ... apply" does not ensure it.
// - The code-review skill runs as a fork in the session's main directory: it reviews the right tree only with an
//   explicit target (code-review "high <branch>"), reads surrounding files from main, and must never get --fix.
//   With an output schema on the caller (as here), the caller's Skill result carries only the fork's agentId
//   ("Skill execution completed"); without one, the result text carries the findings (peer data). Here the findings are the
//   input of the fork's last StructuredOutput call, in its transcript (~/.claude/projects/*/*/subagents/
//   agent-<agentId>.jsonl). The review agent reads them there and says so (skill_findings_read); a review that
//   could not is a failed review (retried once, then the run stops), never an empty one.
//   The simplify skill follows the agent's directory (the main checkout), so this script does simplify by hand.
// - An agent that backgrounds its gates and ends its turn is forced to return and its gates die (peer report): every
//   prompt says to run gates in the foreground, and the fixer commits before the gates.
// - Never send a message to a running workflow agent: it revives a second copy of it. Change the brief instead.
// - Do not move or edit the main checkout while a run is going: the guard below stops the run when it changes.
// - code-review "high <branch>" does NOT honour a base other than main (checked 2026-11-19, from the skill's text and
//   three fork transcripts, without a run on another base): the skill takes a target and nothing else, and its
//   fork computes the diff itself, always as "git diff main...<branch>". So the review agent drops findings on
//   files outside this change's paths and, with a base other than main, findings that come only from the base's
//   own commits; and a review of a range that is no branch (the fixer's commits, reviewed_head..head) is a review
//   by hand, never code-review. Named lookup through .claude/workflows did not find it
//   in a session that started before it landed on main (scriptPath works); retry by name in a new session.
// - Every agent pastes the output of GUARD_CMD for the main checkout (its HEAD, status lines, staged and unstaged
//   contents and untracked files' contents) before and after; the script stops when an
//   agent's two readings differ or when one agent's "before" differs from the previous agent's "after".
// - The branch head is checked the same way: simplify reports the head it leaves (and a clean worktree), every
//   reviewer the head it reviewed (it must be that one), and the fixer the head it gated and the head after
//   everything (they must be equal for ready).

// Restarts, the guard, the advisor and when to stop (2026-11-24, from the runs of 2026-11-22/24):
// - A stage may run again after a usage-limit pause, with the same prompt. Implement decides from what exists
//   (startAction): it creates the worktree, continues from it, or refuses; it never stops because its own earlier
//   work is there. Simplify, fix and the fast lane are told the same. Decisions live on disk (the design note,
//   commits), so a restart costs a re-read, not the work.
// - The harness relays to every agent the owner's request that triggered the run, and says it wins over the stage
//   prompt, so a request that asks for the advisor can override a stage's "forbidden" sentence; the orchestrator
//   words such requests with that in mind. (Whether a message sent mid-run or before a resume is relayed too was
//   not checked.)
// - The guard's equality check stays on GUARD_CMD's four lines. Each agent also pastes GUARD_DETAIL_CMD (status
//   lines and a hash per modified or untracked file), report only: when the guard stops a run, the log and the
//   trail name what changed. Someone else's edit in the main checkout is never reverted, stashed or committed by
//   the orchestrator: it resumes in mode "review" and integrates in a clean worktree.
// - Advisor (the owner's rule for workflow agents): allowed once, at the final checkpoint, in design (the spec
//   review: no later stage reviews the approach) and in re-review (the last independent review); forbidden in every
//   other stage, including the code-review reviewer, its fork and the fast lane (an author fixes there). Every
//   prompt carries one of the two sentences (advisorLine); guarded() refuses a prompt without the right one.
// - Stopping rule for the orchestrator: the result's followups (the re-review's low findings, the run's
//   out-of-scope notes) go to the pull request body unless one is a correctness bug in code the pull request
//   ships; only those get another run.

const LENSES = {
  parity: { skill: 'cst-refactor-parity', why: 'a refactor: the proof that nothing changed' },
  command: { skill: 'cst-new-command', why: 'a behaviour change of what CST does to the machine: plan, apply, status, undo, upgrade, E2E' },
  helper: { skill: 'cst-new-helper', why: 'an installed Python program: manifest row, pyproject lists, BOOTSTRAP, tier 1 install test' },
  port: { skill: 'cst-shell-to-python', why: 'bash replaced by Python: bash-first behaviour tests, same CLI, exit codes and output' },
  primitives: { skill: '', why: '.agents/rules/reuse-primitives.md: no copy of a cst_core primitive' },
}
const MD = /\.md$/
// Paths that are no part of what CST installs or runs: they never ask for tier 1 or the command lens.
const REPO_ONLY = /^(\.agents|\.claude|\.github|docs|cst\.invowkmod)\/|^(\.claude|pyproject\.toml|uv\.lock|LICENSE|\.gitignore|\.git-blame-ignore-revs)$/
const UNIT_TEST = /(^|\/)tests\//
const E2E = /^tests\/e2e\//
const BASH = /\.(sh|bash)$/
// A file name without a dot: an extensionless program (btrfs/btrfs-snapshot is bash, btrfs/docker-gc is Python).
// The paths do not say which, so such a file counts as both: a lens too many costs one reviewer, a lens too few
// a missed checklist.
const EXTLESS = /(^|\/)[^./]+$/
const INSTALL = /^cst_core\/(domain|application|adapters)\/install\.py$/
// A path of what CST installs or runs (an E2E test runs on the machine too): neither Markdown, a unit test nor
// repository tooling.
const machinePath = (p) => !MD.test(p.path) && !REPO_ONLY.test(p.path) && (!UNIT_TEST.test(p.path) || E2E.test(p.path))

// Which checklist lenses review a change, and whether tier 1 must run, from the paths it touches
// (git diff --name-status --no-renames). Pure: tested below at every start.
function pickLenses(kind, changed) {
  if (!changed.length) throw new Error('nothing changed beyond the base')
  for (const p of changed) if (!/^[AMDT]$/.test(p.status)) throw new Error('changed path status not in A/M/D/T: ' + JSON.stringify(p))
  const code = changed.filter((p) => !MD.test(p.path))
  const machine = code.filter(machinePath)
  const python = (p) => /\.py$/.test(p.path) || (EXTLESS.test(p.path) && !REPO_ONLY.test(p.path))
  const bashSource = (p) => p.status !== 'A' && (BASH.test(p.path) || (EXTLESS.test(p.path) && !UNIT_TEST.test(p.path) && !REPO_ONLY.test(p.path)))
  if (kind === 'docs' && (machine.length || code.some(python))) throw new Error('kind "docs" but code CST runs changed: ' + code.map((p) => p.path).join(', '))
  const lenses = new Set()
  if (kind === 'refactor') lenses.add('parity')
  if (kind === 'behaviour' && machine.some((p) => !E2E.test(p.path))) lenses.add('command')
  if (code.some((p) => p.status === 'A' && /^[a-z_]+\/[a-z0-9-]+$/.test(p.path) && !UNIT_TEST.test(p.path)) || code.some((p) => INSTALL.test(p.path))) lenses.add('helper')
  if (code.some(bashSource) && code.some((p) => p.status === 'A' && /\.py$/.test(p.path))) lenses.add('port')
  if (code.some((p) => python(p) && !UNIT_TEST.test(p.path) && !REPO_ONLY.test(p.path))) lenses.add('primitives')
  return { lenses: [...lenses].sort(), tier1: kind !== 'docs' && machine.length > 0 }
}

// The most agents one stage runs at once (the owner's limit, 2026-11-22). A code-review reviewer counts 2: its skill
// forks one more agent. Every other stage runs one agent.
const MAX_STAGE_AGENTS = 3
const agentCost = (label) => (label === 'code-review' ? 2 : 1)
// The review labels in waves, in order, each running at most cap agents at once. Pure: tested below.
function reviewWaves(labels, cap = MAX_STAGE_AGENTS) {
  const waves = []
  let cur = [], used = 0
  for (const l of labels) {
    const c = agentCost(l)
    if (c > cap) throw new Error('review ' + l + ' alone needs ' + c + ' agents, more than ' + cap)
    if (cur.length && used + c > cap) { waves.push(cur); cur = []; used = 0 }
    cur.push(l)
    used += c
  }
  if (cur.length) waves.push(cur)
  return waves
}

// Why a review does not count ('' when it counts): the code-review agent must have read the fork's findings, and
// every reviewer must have reviewed the head the stage before reported. Pure: tested below.
function reviewProblem(lens, r, head) {
  if (lens === 'code-review' && r.skill_findings_read !== true) return 'the code-review findings were not read'
  if (lens === 'code-review' && !String(r.fork_agent_id || '').trim()) return 'the code-review fork was not named'
  const got = String(r.reviewed_head).trim()
  if (got.length < 7 || !head.startsWith(got)) return 'reviewed ' + got + ', not ' + head
  return ''
}
// Whether the fixer's report makes the branch ready. final_head (read after everything) must be the gated head: a
// commit after the gates is ungated. Pure: tested below.
function isReady(fix, tier1) {
  return !!(fix.gates_ok && fix.worktree_clean && (!tier1 || fix.tier1_ran) && fix.gated_head && String(fix.final_head).trim() === String(fix.gated_head).trim())
}

// Why the fixer's deferrals do not count ('' when they do): each names the finding and the redesign it waits for.
// The reason itself is the fixer's judgement; the orchestrator reads it in the result. Pure: tested below.
function deferralProblem(deferred) {
  if (!Array.isArray(deferred)) return 'deferred is not a list'
  const bad = deferred.filter((d) => !d || !String(d.finding || '').trim() || !String(d.reason || '').trim())
  return bad.length ? bad.length + ' deferral(s) without a finding or a reason' : ''
}

// The re-review's findings split by severity: a "high" one stops the run, the others go to the result. A finding
// with no severity, or one the script does not know, counts as high. Pure: tested below.
function reReviewOutcome(findings) {
  const high = findings.filter((f) => String(f.severity || '').trim().toLowerCase() !== 'low')
  return { high, low: findings.filter((f) => !high.includes(f)) }
}

// The largest change (added plus deleted lines) the fast lane takes.
const FAST_LINES = 150
// The changed lines (added plus deleted) that git diff --numstat prints; null when a file is binary ("-") or a line
// does not parse: such a change never takes the fast lane. Any blanks separate the fields: an agent that pastes the
// output may turn its tabs into spaces. Pure: tested below.
function numstatLines(text) {
  let n = 0
  for (const line of String(text).split('\n')) {
    if (!line.trim()) continue
    const m = /^(\d+)\s+(\d+)\s+\S/.exec(line)
    if (!m) return null
    n += Number(m[1]) + Number(m[2])
  }
  return n
}
// Why a change needs the full review ('' when the fast lane is enough): too many changed lines, a path beyond
// Markdown, unit tests and repository tooling (an E2E test needs tier 1), or a checklist lens beyond primitives.
// Pure: tested below.
function fullReviewReason(changed, lines, lenses) {
  if (lines === null) return 'a binary file or a numstat line that does not parse'
  if (lines > FAST_LINES) return lines + ' changed lines (more than ' + FAST_LINES + ')'
  const other = changed.filter(machinePath)
  if (other.length) return 'paths beyond docs, unit tests and repository tooling: ' + other.map((p) => p.path).join(', ')
  const lens = lenses.filter((l) => l !== 'primitives')
  if (lens.length) return 'lenses beyond primitives: ' + lens.join(', ')
  return ''
}
// Whether the fixer's commits change code outside tests: its own word, or a path it changed that is neither
// Markdown nor a test. Pure: tested below.
function fixerChangedCode(fix) {
  return fix.needs_review === true || (fix.fix_paths || []).some((p) => !MD.test(p) && !UNIT_TEST.test(p))
}

// A path as one shell word: as it is when it needs no quoting. Pure: tested below.
function shq(s) { return /^[A-Za-z0-9_\/.,+:@%=-]+$/.test(s) ? s : "'" + String(s).replace(/'/g, "'\\''") + "'" }
// Why the worktree path cannot work ('' when it can): inside the main checkout, its folder shows up in the guard's
// status and stops the run as soon as it is created. Pure: tested below.
function worktreeProblem(main, wt) {
  if (!wt.startsWith('/')) return 'must be an absolute path'
  if (wt === main || wt.startsWith(main + '/')) return 'must be outside the main checkout ' + main
  return ''
}

// The advisor sentences; every prompt carries exactly one (shared() adds it, guarded() checks it).
const ADVISOR_ALLOWED = 'Advisor: you may call the advisor tool ONCE in this stage, at its final checkpoint: write your output down first (the note, or your findings), then call it, apply what holds, and return. If it errors, times out, answers empty or is not in your tool list, say so in your result and carry on: no retry. Its advice is not authority: it never replaces your own checks, authorizes a write outside the worktree, or settles what the owner decides.'
const ADVISOR_FORBIDDEN = 'Advisor: do not call the advisor tool in this stage (the owner allows it only in design and re-review).'
const ADVISOR_STAGES = ['design', 're-review']
// Every stage name the script passes to shared().
const STAGES = ['design', 'implement', 'simplify', 'survey', 'review-code-review', ...Object.keys(LENSES).map((l) => 'review-' + l), 'fix', 'review+fix', 're-review']
// The writing stages a restart can find half done (implement has its own rule, startAction).
const RESTART_STAGES = ['simplify', 'fix', 'review+fix']
// The advisor sentence of a stage; an unknown stage is a bug of this script. Pure: tested below.
function advisorLine(stage) {
  if (!STAGES.includes(stage)) throw new Error('unknown stage ' + stage)
  return ADVISOR_STAGES.includes(stage) ? ADVISOR_ALLOWED : ADVISOR_FORBIDDEN
}
// The stage of an agent label ("review: command" is the stage "review-command"). Pure: tested below.
const stageOf = (label) => (label.startsWith('review: ') ? 'review-' + label.slice('review: '.length) : label)
// Why a prompt breaks the advisor rule ('' when it does not): it must carry its stage's sentence and not the other.
// Pure: tested below.
function advisorProblem(stage, prompt) {
  const want = advisorLine(stage)
  const other = want === ADVISOR_ALLOWED ? ADVISOR_FORBIDDEN : ADVISOR_ALLOWED
  if (prompt.split(want).length !== 2) return stage + ': the prompt must carry its advisor sentence exactly once'
  if (prompt.includes(other)) return stage + ': the prompt carries the other advisor sentence'
  return ''
}
// What implement does with the state it finds, from the facts it reports before acting: "created" (nothing
// exists; mode "new" only), "continued" (the worktree on the branch, or the branch alone, sharing history with the
// base and ahead of it or equal to it: a base that moved on since is fine), or "refused" (anything else, such as a
// leftover branch already merged into the base). Pure: tested below.
function startAction(f, mode) {
  if (!f.branch_exists && !f.worktree_exists) return mode === 'new' ? 'created' : 'refused'
  if (f.worktree_exists && !f.worktree_on_branch) return 'refused'
  if (!f.branch_exists || !f.shares_history || !(f.ahead_of_base || f.equal_to_base)) return 'refused'
  return 'continued'
}
// The lines one guard detail reading has and the other has not: what changed in the main checkout. Pure: tested
// below.
function guardChanges(before, after) {
  const a = String(before || '').split('\n').map((s) => s.trimEnd()).filter(Boolean)
  const b = String(after || '').split('\n').map((s) => s.trimEnd()).filter(Boolean)
  return [...a.filter((l) => !b.includes(l)).map((l) => '- ' + l), ...b.filter((l) => !a.includes(l)).map((l) => '+ ' + l)]
}

function selftest() {
  const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error('selftest ' + what + ': ' + JSON.stringify(a) + ' != ' + JSON.stringify(b)) }
  const throws = (f, what) => { try { f() } catch (e) { return } throw new Error('selftest ' + what + ': did not throw') }
  const M = (path, status = 'M') => ({ path, status })
  eq(pickLenses('docs', [M('btrfs/README.md')]), { lenses: [], tier1: false }, 'docs only')
  throws(() => pickLenses('docs', [M('btrfs/units/btrfs-x.service')]), 'docs that is not docs')
  throws(() => pickLenses('refactor', [M('a.py', 'R100')]), 'a rename status')
  eq(pickLenses('refactor', [M('swap/domain.py')]), { lenses: ['parity', 'primitives'], tier1: true }, 'refactor of a domain')
  eq(pickLenses('behaviour', [M('cpu/setup/adapters.py')]), { lenses: ['command', 'primitives'], tier1: true }, 'machine logic outside cli.py')
  eq(pickLenses('behaviour', [M('cst-install.bash')]), { lenses: ['command'], tier1: true }, 'the top-level installer')
  eq(pickLenses('behaviour', [M('swap/sysctl/99-cst-swap.conf')]), { lenses: ['command'], tier1: true }, 'installed data')
  eq(pickLenses('behaviour', [M('cst_core/domain/install.py'), M('btrfs/new-helper', 'A')]), { lenses: ['command', 'helper', 'primitives'], tier1: true }, 'a new installed program')
  eq(pickLenses('refactor', [M('vm-setup.sh'), M('vm/setup/cli.py', 'A')]), { lenses: ['parity', 'port', 'primitives'], tier1: true }, 'a bash port with a shim')
  eq(pickLenses('behaviour', [M('tests/e2e/suites/suite_tier1_systemd.py')]), { lenses: [], tier1: true }, 'an e2e suite')
  eq(pickLenses('refactor', [M('vm/tests/test_vm.py')]), { lenses: ['parity'], tier1: false }, 'tests only')
  eq(pickLenses('behaviour', [M('tests/newthing', 'A')]), { lenses: [], tier1: false }, 'a new test helper is not an installed program')
  eq(pickLenses('docs', [M('.agents/workflows/cst-change.js', 'A'), M('.claude/workflows', 'A'), M('AGENTS.md')]), { lenses: [], tier1: false }, 'agent tooling')
  eq(pickLenses('behaviour', [M('.github/workflows/ci.yml'), M('pyproject.toml'), M('uv.lock'), M('tests/test_python_files.py')]), { lenses: [], tier1: false }, 'CI and gate config')
  throws(() => pickLenses('docs', [M('tests/test_x.py')]), 'docs that changes Python')
  throws(() => pickLenses('behaviour', []), 'an empty change')
  eq(pickLenses('refactor', [M('btrfs/btrfs-snapshot'), M('btrfs/snapshot/cli.py', 'A')]), { lenses: ['parity', 'port', 'primitives'], tier1: true }, 'a port of an extensionless bash script')
  eq(pickLenses('behaviour', [M('btrfs/docker-gc')]), { lenses: ['command', 'primitives'], tier1: true }, 'an extensionless Python program')
  throws(() => pickLenses('docs', [M('tests/mutation/cst-mutation')]), 'docs that changes an extensionless Python test tool')
  eq(pickLenses('docs', [M('cst.invowkmod/invowkfile.cue'), M('.git-blame-ignore-revs')]), { lenses: [], tier1: false }, 'invowk tasks and blame config')
  eq(pickLenses('behaviour', [M('cst.invowkmod/invowkfile.cue')]), { lenses: [], tier1: false }, 'an invowk task')
  eq([reviewProblem('code-review', { skill_findings_read: false, reviewed_head: 'h' }, 'h'), reviewProblem('code-review', { reviewed_head: 'h' }, 'h')], ['the code-review findings were not read', 'the code-review findings were not read'], 'code-review findings not read')
  eq([reviewProblem('code-review', { skill_findings_read: true, fork_agent_id: 'a1', reviewed_head: 'abcdef1\n' }, 'abcdef1234'), reviewProblem('port', { reviewed_head: 'abcdef1234' }, 'abcdef1234')], ['', ''], 'reviews that count (an abbreviated head counts)')
  eq(reviewProblem('code-review', { skill_findings_read: true, reviewed_head: 'abcdef1234' }, 'abcdef1234'), 'the code-review fork was not named', 'a code-review without its fork')
  eq(reviewProblem('port', { reviewed_head: 'ffffff9' }, 'abcdef1234'), 'reviewed ffffff9, not abcdef1234', 'a review of another head')
  const F = { gates_ok: true, worktree_clean: true, tier1_ran: false, gated_head: 'h', final_head: 'h' }
  eq([isReady(F, false), isReady({ ...F, final_head: 'h2' }, false), isReady({ ...F, gated_head: '', final_head: '' }, false), isReady(F, true), isReady({ ...F, worktree_clean: false }, false)], [true, false, false, false, false], 'ready')
  eq([shq('/a/b-c_d.e'), shq('/a b'), shq("/it's")], ['/a/b-c_d.e', "'/a b'", "'/it'\\''s'"], 'shell quoting')
  eq([worktreeProblem('/r/cst', '/r/cst-wt-x'), worktreeProblem('/r/cst', '/r/cst/.wt/x'), worktreeProblem('/r/cst', '/r/cst'), worktreeProblem('/r/cst', 'wt')], ['', 'must be outside the main checkout /r/cst', 'must be outside the main checkout /r/cst', 'must be an absolute path'], 'worktree path')
  eq([numstatLines('3\t4\ta.md\n10\t0\tb/c.js\n'), numstatLines(''), numstatLines('-\t-\timg.png\n1\t1\ta.md'), numstatLines('x'), numstatLines('3 4  a.md\n2\t1 b c.md')], [17, 0, null, null, 10], 'numstat lines')
  const doc = [M('.agents/workflows/cst-change.js'), M('AGENTS.md'), M('vm/tests/test_vm.py'), M('btrfs/README.md')]
  eq([fullReviewReason(doc, 150, []), fullReviewReason(doc, 0, ['primitives']), fullReviewReason(doc, 151, []), fullReviewReason(doc, null, [])], ['', '', '151 changed lines (more than 150)', 'a binary file or a numstat line that does not parse'], 'fast lane by size')
  eq([fullReviewReason([M('swap/domain.py')], 10, ['primitives']), fullReviewReason([M('tests/e2e/suites/suite_tier1_systemd.py')], 10, []), fullReviewReason([M('vm/tests/test_vm.py')], 10, ['parity'])], ['paths beyond docs, unit tests and repository tooling: swap/domain.py', 'paths beyond docs, unit tests and repository tooling: tests/e2e/suites/suite_tier1_systemd.py', 'lenses beyond primitives: parity'], 'full review by path and lens')
  eq([fixerChangedCode({ needs_review: false, fix_paths: [] }), fixerChangedCode({ needs_review: false, fix_paths: ['vm/tests/test_vm.py', 'AGENTS.md'] }), fixerChangedCode({ needs_review: false, fix_paths: ['.agents/workflows/cst-change.js'] }), fixerChangedCode({ needs_review: true, fix_paths: [] }), fixerChangedCode({ needs_review: false })], [false, false, true, true, false], 'the fixer changed code')
  eq([deferralProblem([]), deferralProblem([{ finding: 'f', reason: 'the parser wants a redesign' }]), deferralProblem([{ finding: 'f', reason: ' ' }, { reason: 'r' }]), deferralProblem(undefined)], ['', '', '2 deferral(s) without a finding or a reason', 'deferred is not a list'], 'deferrals')
  const fh = { file: 'a', summary: 's', scenario: 'c', severity: 'high' }, fl = { ...fh, severity: 'low' }, fn = { file: 'a', summary: 's', scenario: 'c' }
  eq([reReviewOutcome([]), reReviewOutcome([fh, fl]), reReviewOutcome([fn, { ...fl, severity: 'LOW ' }, { ...fl, severity: 'medium' }])], [{ high: [], low: [] }, { high: [fh], low: [fl] }, { high: [fn, { ...fl, severity: 'medium' }], low: [{ ...fl, severity: 'LOW ' }] }], 're-review outcome')
  eq([reviewWaves([]), reviewWaves(['parity']), reviewWaves(['code-review', 'command', 'primitives']), reviewWaves(['code-review', 'command', 'helper', 'primitives']), reviewWaves(['code-review', 'parity', 'port', 'primitives', 'command'])], [[], [['parity']], [['code-review', 'command'], ['primitives']], [['code-review', 'command'], ['helper', 'primitives']], [['code-review', 'parity'], ['port', 'primitives', 'command']]], 'review waves')
  throws(() => reviewWaves(['code-review'], 1), 'a cap below one reviewer')
  eq(STAGES.filter((s) => advisorLine(s) === ADVISOR_ALLOWED), ['design', 're-review'], 'advisor allowed only in design and re-review')
  eq(STAGES.filter((s) => advisorLine(s) === ADVISOR_FORBIDDEN).length, STAGES.length - 2, 'advisor forbidden everywhere else')
  throws(() => advisorLine('review-unknown'), 'an unknown stage')
  eq([stageOf('review: command'), stageOf('review: code-review'), stageOf('fix'), stageOf('review+fix')], ['review-command', 'review-code-review', 'fix', 'review+fix'], 'stage of a label')
  eq([advisorProblem('fix', 'x ' + ADVISOR_FORBIDDEN), advisorProblem('re-review', 'x ' + ADVISOR_ALLOWED), advisorProblem('fix', 'x'), advisorProblem('fix', ADVISOR_FORBIDDEN + ADVISOR_FORBIDDEN), advisorProblem('design', ADVISOR_ALLOWED + ADVISOR_FORBIDDEN)], ['', '', 'fix: the prompt must carry its advisor sentence exactly once', 'fix: the prompt must carry its advisor sentence exactly once', 'design: the prompt carries the other advisor sentence'], 'advisor problem')
  const S = (branch_exists, worktree_exists, worktree_on_branch, shares_history, ahead_of_base, equal_to_base) => ({ branch_exists, worktree_exists, worktree_on_branch, shares_history, ahead_of_base, equal_to_base })
  eq([
    startAction(S(false, false, false, false, false, false), 'new'), // nothing yet
    startAction(S(false, false, false, false, false, false), 'continue'), // continue expects work
    startAction(S(true, true, true, true, true, false), 'new'), // a restart: work on the branch
    startAction(S(true, true, true, true, true, false), 'continue'), // the same in mode continue
    startAction(S(true, true, true, true, false, true), 'new'), // restarted before the first commit
    startAction(S(true, false, false, true, true, false), 'new'), // the branch alone: add the worktree
    startAction(S(true, true, true, true, false, false), 'new'), // a leftover branch merged into the base
    startAction(S(true, true, false, true, true, false), 'new'), // the worktree on another branch
    startAction(S(true, true, true, false, true, false), 'new'), // no history in common with the base
    startAction(S(false, true, false, false, false, false), 'new'), // a worktree without the branch
  ], ['created', 'refused', 'continued', 'continued', 'continued', 'continued', 'refused', 'refused', 'refused', 'refused'], 'start action')
  eq([guardChanges('a\nb', 'a\nb'), guardChanges(' M AGENTS.md\nh1  AGENTS.md', ' M AGENTS.md\nh2  AGENTS.md\n?? x'), guardChanges('', '')], [[], ['- h1  AGENTS.md', '+ h2  AGENTS.md', '+ ?? x'], []], 'guard changes')
  return 'ok'
}

const st = selftest()
const A = args || {}
const MODES = ['new', 'continue', 'review', 'selftest']
const MODE = A.mode || 'new'
if (!MODES.includes(MODE)) throw new Error('args.mode: one of ' + MODES.join(', '))
if (MODE === 'selftest') return { selftest: st }
for (const k of ['main', 'slug', 'title', 'kind', 'brief', 'notes']) if (!A[k]) throw new Error('args.' + k + ' is required')
for (const k of ['main', 'brief', 'notes']) if (!String(A[k]).startsWith('/')) throw new Error('args.' + k + ' must be an absolute path')
if (!/^[a-z0-9][a-z0-9-]*$/.test(A.slug)) throw new Error('args.slug: [a-z0-9-]')
if (!['refactor', 'behaviour', 'docs'].includes(A.kind)) throw new Error('args.kind: refactor | behaviour | docs')
if (A.tier1 !== undefined && !['run', 'defer'].includes(A.tier1)) throw new Error('args.tier1: run | defer')
const MAIN = A.main.replace(/\/$/, '')
const WT = String(A.worktree || MAIN + '-wt-' + A.slug).replace(/\/$/, '')
const BRANCH = A.branch || 'change/' + A.slug
const BASE = A.base || 'main'
const wtProblem = worktreeProblem(MAIN, WT)
if (wtProblem) throw new Error('args.worktree: ' + wtProblem)

const QM = shq(MAIN), QWT = shq(WT), QBASE = shq(BASE), QBRANCH = shq(BRANCH)
// The main checkout's state: HEAD, the status lines, the staged and unstaged contents, the untracked files' contents.
const GUARD_CMD = 'git -C ' + QM + ' rev-parse HEAD; git -C ' + QM + ' status --porcelain --untracked-files=all | sha256sum; git -C ' + QM + ' diff HEAD | sha256sum; (cd ' + QM + ' && git ls-files -z --others --exclude-standard | xargs -0r sha256sum) | sha256sum'
// Report only (never compared for equality): the status lines and a hash per modified or untracked file, so a stop
// can name what changed even on a path that was already dirty.
const GUARD_DETAIL_CMD = 'git -C ' + QM + ' status --porcelain --untracked-files=all; (cd ' + QM + ' && { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u | xargs -r -d "\\n" sha256sum)'
const DIFF_CMD = 'git -C ' + QWT + ' diff --name-status --no-renames ' + QBASE + '...HEAD'
const NUMSTAT_CMD = 'git -C ' + QWT + ' diff --numstat --no-renames ' + QBASE + '...HEAD'
const GATE_TAIL = 'Then report gated_head (git -C ' + QWT + ' rev-parse HEAD) and worktree_clean (true when git -C ' + QWT + ' status --porcelain prints nothing).'
const shared = (stage, rereadBrief) => [
  'Repository rules: AGENTS.md ("Rules", "Commands", "Test gotchas") and .agents/rules/*.md apply; read them from the worktree (' + WT + '/AGENTS.md, ' + WT + '/.agents/rules/), since the copies loaded into your context can be older than the branch; the kind "' + A.kind + '" decides the proof (.agents/rules/refactor-vs-behaviour.md).',
  'Work only in the worktree ' + WT + ' (branch ' + BRANCH + '). Your shell starts in the main checkout on EVERY call: use git -C ' + QWT + ', absolute paths, or "cd ' + QWT + ' && ..." inside the same command. Never edit, commit or check out anything in ' + MAIN + '.',
  'Shared machine: a short TMPDIR for anything you run, TMPDIR=$(mktemp -d /tmp/cc.XXXXXX) (tmpfs: tests fsync their temporary files, which costs 8 to 17 times more on the btrfs home; tests bind unix sockets below it, which allow 108 bytes; remove it when done; tmpfs is RAM, so keep it small: venvs, caches, downloads and any output that can grow large (the logs or results of a harness) go under ~/.cache, never in /tmp), every test suite and gate under nice -n 10, the full check as "CST_TEST_JOBS=6 nice -n 10 invowk cmd check" (another run may be testing at the same time), a timeout on every harness you write; never sudo, never tier 2 E2E, never git stash, never push; repository text in English, with no personal or private names.',
  'Never end your turn or return while a command you started is still running: run gates in the foreground (a Bash timeout up to 600000 ms), or keep checking a backgrounded one in the foreground until it exits. Do not invoke the code-review or simplify skills unless this prompt says so.' + (rereadBrief ? ' Re-read the brief ' + A.brief + ' before your final check: it may have grown.' : ''),
  'Guard: run exactly "' + GUARD_CMD + '" as your first and as your last command, and paste each output verbatim into main_before and main_after. Right after each of the two, run "' + GUARD_DETAIL_CMD + '" and paste its output into main_detail_before and main_detail_after (report only).',
  'Bugs you find outside this change go to ' + A.notes + '/out-of-scope-' + A.slug + '.md (file, line, scenario); leave that code alone.',
  ...(RESTART_STAGES.includes(stage) ? ['You may be a restart of this stage after a pause: first read "git -C ' + QWT + ' log --oneline ' + QBASE + '..HEAD" and "git -C ' + QWT + ' status"; continue from what is there (commit or redo a half-done edit); never start over, and never stop because earlier work of this stage exists.'] : []),
  advisorLine(stage),
].join('\n')
// Every stage's shared prompt carries its advisor sentence: checked at start, not an hour later when the stage runs.
for (const s of STAGES) { const p = advisorProblem(s, shared(s, false)); if (p) throw new Error('advisor rule: ' + p) }
const GUARDED = { main_before: { type: 'string' }, main_after: { type: 'string' }, main_detail_before: { type: 'string' }, main_detail_after: { type: 'string' } }
const GREQ = ['main_before', 'main_after']
const CHANGED = { type: 'array', items: { type: 'object', properties: { path: { type: 'string' }, status: { type: 'string' } }, required: ['path', 'status'] } }
const GATED = { gated_head: { type: 'string' }, worktree_clean: { type: 'boolean' } }

let lastAfter = null
const trail = []
// Runs one agent and checks the guard; returns null (and logs why) when the run must stop.
let lastDetail = null
async function guarded(label, phase_, prompt, props, required) {
  const why = advisorProblem(stageOf(label), prompt)
  if (why) throw new Error('advisor rule: ' + why)
  const r = await agent(prompt, { label, phase: phase_, schema: { type: 'object', properties: { ...props, ...GUARDED }, required: [...required, ...GREQ] } })
  if (!r) { log(label + ': the agent failed or was skipped: stopping'); trail.push({ label, failed: true }); return null }
  const norm = (s) => String(s).trim()
  if (norm(r.main_before) !== norm(r.main_after)) {
    const main_changes = guardChanges(r.main_detail_before, r.main_detail_after)
    log(label + ': the main checkout changed during this agent: stopping; changed: ' + (main_changes.join(' | ') || 'not reported'))
    trail.push({ label, main_changed: true, main_changes }); return null
  }
  if (lastAfter !== null && norm(r.main_before) !== lastAfter) {
    const main_changes = guardChanges(lastDetail, r.main_detail_before)
    log(label + ': the main checkout changed between agents: stopping; changed: ' + (main_changes.join(' | ') || 'not reported'))
    trail.push({ label, main_changed_between: true, main_changes }); return null
  }
  lastAfter = norm(r.main_after)
  lastDetail = r.main_detail_after
  return r
}
const stopped = (stage, extra) => ({ ready: false, stopped: stage, branch: BRANCH, worktree: WT, trail, ...extra })
// A reviewer's report (null when the agent stopped), or null when the review does not count (reviewProblem): then
// logged under logLabel and kept in the trail under label.
const counted = (r, lens, head, label, logLabel = label) => {
  const why = r ? reviewProblem(lens, r, head) : ''
  if (why) { log(logLabel + ': ' + why); trail.push({ label, problem: why }); return null }
  return r
}

let design = null
if (MODE === 'new' && A.kind !== 'docs') {
  phase('Design')
  design = await guarded('design', 'Design', shared('design', false) + '\n\nStage: design. Read the brief ' + A.brief + ' and the module READMEs it names. Write the design note ' + A.notes + '/DESIGN-' + A.slug + '.md: what changes, the commits in order with their kind and proof, the tests (unit and E2E per AGENTS.md), what the README says after, risks. Then call the advisor tool once with the note written and apply its advice to the note. If the tool is missing or fails, say so (advisor_called false) and leave the note unreviewed. Edit nothing in the repository.',
    { note: { type: 'string' }, advisor_called: { type: 'boolean' }, advisor_changes: { type: 'string' } }, ['note', 'advisor_called'])
  if (!design) return stopped('design')
  if (!design.advisor_called) log('design: the advisor was not called; the note is unreviewed')
}

let impl = null, simp = null, changed = [], numstat = '', HEAD = ''
if (MODE === 'new' || MODE === 'continue') {
  phase('Implement')
  const START_FACTS = { type: 'object', properties: { branch_exists: { type: 'boolean' }, worktree_exists: { type: 'boolean' }, worktree_on_branch: { type: 'boolean' }, shares_history: { type: 'boolean' }, ahead_of_base: { type: 'boolean' }, equal_to_base: { type: 'boolean' } }, required: ['branch_exists', 'worktree_exists', 'worktree_on_branch', 'shares_history', 'ahead_of_base', 'equal_to_base'] }
  const start = 'Before anything else, look at what exists and report start_facts, read before you act: branch_exists ("git -C ' + QM + ' rev-parse --verify --quiet refs/heads/' + BRANCH + '" exits 0), worktree_exists (the folder ' + WT + ' exists and "git -C ' + QWT + ' rev-parse --git-dir" exits 0), worktree_on_branch ("git -C ' + QWT + ' rev-parse --abbrev-ref HEAD" prints ' + BRANCH + '), shares_history ("git -C ' + QM + ' merge-base ' + QBASE + ' ' + QBRANCH + '" exits 0), ahead_of_base ("git -C ' + QM + ' rev-list --count ' + QBASE + '..' + QBRANCH + '" prints more than 0), equal_to_base ("git -C ' + QM + ' rev-parse ' + QBASE + '" and "git -C ' + QM + ' rev-parse ' + QBRANCH + '" print the same; each of the three false when the branch does not exist), and found_head ("git -C ' + QWT + ' rev-parse HEAD", or "" without a worktree). Then act by this table and report start: ' +
    (MODE === 'new' ? 'no branch and no worktree: "created" (git -C ' + QM + ' worktree add -b ' + QBRANCH + ' ' + QWT + ' ' + QBASE + '); ' : 'no branch and no worktree: "refused" (mode "continue" expects them); ') +
    'the worktree on ' + BRANCH + ', or the branch alone, and the branch shares history with ' + BASE + ' and is ahead of it or equal to it (a base that moved on since is fine): "continued" (with the branch alone, first git -C ' + QM + ' worktree add ' + QWT + ' ' + QBRANCH + '). You may be a restart of this stage after a pause: continue from what is there (read ' + A.notes + '/DESIGN-' + A.slug + '.md if it exists, "git -C ' + QWT + ' log --oneline ' + QBASE + '..HEAD" and "git -C ' + QWT + ' status"), finish or redo a half-done edit and commit it; never start over, and never stop because your own earlier work exists. Anything else (a worktree on another branch, a branch with no history in common with ' + BASE + ', a branch already merged into it): "refused": change nothing, say what you found, check_ok false.'
  impl = await guarded('implement', 'Implement', shared('implement', true) + '\n\nStage: implement "' + A.title + '". ' + start + ' Follow the brief ' + A.brief + (design ? ' and the design note ' + A.notes + '/DESIGN-' + A.slug + '.md' : '') + '. Commits of one kind each, with their proof; tests with the change (a behaviour change has a test that fails on the old code, checked). While working run "cd ' + QWT + ' && nice -n 10 invowk cmd test module <module>"; at the end run "cd ' + QWT + ' && CST_TEST_JOBS=6 nice -n 10 invowk cmd check" once: check_ok is true only when it passed, and check holds its summary. ' + GATE_TAIL + ' Return changed_paths exactly as "' + DIFF_CMD + '" prints them.',
    { start: { type: 'string', enum: ['created', 'continued', 'refused'] }, start_facts: START_FACTS, found_head: { type: 'string' }, commits: { type: 'array', items: { type: 'string' } }, changed_paths: CHANGED, check_ok: { type: 'boolean' }, check: { type: 'string' }, notes: { type: 'string' }, ...GATED }, ['start', 'start_facts', 'found_head', 'commits', 'changed_paths', 'check_ok', 'check', 'gated_head', 'worktree_clean'])
  if (!impl) return stopped('implement')
  const wantStart = startAction(impl.start_facts, MODE)
  if (impl.start !== wantStart) { log('implement: it reported start "' + impl.start + '" where its facts give "' + wantStart + '": stopping'); return stopped('implement', { impl }) }
  if (wantStart === 'refused') { log('implement: refused to start (see impl.notes): stopping'); return stopped('implement', { impl }) }
  if (wantStart === 'continued') log('implement: continued from the existing worktree at ' + (impl.found_head || 'its branch'))
  if (!impl.check_ok || !impl.worktree_clean) return stopped('implement', { impl })

  phase('Simplify')
  simp = await guarded('simplify', 'Simplify', shared('simplify', false) + '\n\nStage: simplify, by hand (quality only: no bug hunting, no behaviour change). On "git -C ' + QWT + ' diff ' + QBASE + '...HEAD": reuse what cst_core provides (.agents/rules/reuse-primitives.md), remove needless code, keep each function at one level of abstraction. Commit each change as a refactor proven by the unchanged tests of what it touched ("cd ' + QWT + ' && nice -n 10 invowk cmd test module <module>"). Report what you changed and what you left on purpose, and return changed_paths exactly as "' + DIFF_CMD + '" prints them after your commits, and numstat, the verbatim output of "' + NUMSTAT_CMD + '". Commit every edit you keep: the review sees only commits. ' + GATE_TAIL,
    { commits: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, changed_paths: CHANGED, numstat: { type: 'string' }, ...GATED }, ['commits', 'summary', 'changed_paths', 'numstat', 'gated_head', 'worktree_clean'])
  if (!simp) return stopped('simplify', { impl })
  if (!simp.worktree_clean) { log('simplify: the worktree has uncommitted edits: stopping'); return stopped('simplify', { impl, simp }) }
  changed = simp.changed_paths
  numstat = simp.numstat
  HEAD = String(simp.gated_head).trim()
} else {
  phase('Review')
  const survey = await guarded('survey', 'Review', shared('survey', false) + '\n\nStage: survey for a review of an existing branch (edit nothing). Report worktree_exists (the folder ' + WT + ' exists and git -C ' + QWT + ' rev-parse --abbrev-ref HEAD prints ' + BRANCH + '), worktree_clean (true when git -C ' + QWT + ' status --porcelain prints nothing), head (git -C ' + QWT + ' rev-parse HEAD), changed_paths exactly as "' + DIFF_CMD + '" prints them, and numstat, the verbatim output of "' + NUMSTAT_CMD + '".',
    { worktree_exists: { type: 'boolean' }, worktree_clean: { type: 'boolean' }, head: { type: 'string' }, changed_paths: CHANGED, numstat: { type: 'string' } }, ['worktree_exists', 'worktree_clean', 'head', 'changed_paths', 'numstat'])
  if (!survey || !survey.worktree_exists || !survey.worktree_clean) return stopped('survey', { survey })
  changed = survey.changed_paths
  numstat = survey.numstat
  HEAD = String(survey.head).trim()
}
changed = changed.map((p) => ({ path: p.path, status: String(p.status).trim().charAt(0).toUpperCase() }))

phase('Review')
let pick
try { pick = pickLenses(A.kind, changed) } catch (e) { log(String(e)); return stopped('lenses', { impl, simp, error: String(e) }) }
const TIER1 = pick.tier1 && A.tier1 !== 'defer'
if (pick.tier1 && !TIER1) log('tier 1 deferred to the integration branch')
log('lenses: ' + (pick.lenses.join(', ') || 'none') + '; tier 1: ' + pick.tier1)
const lines = numstatLines(numstat)
const slow = fullReviewReason(changed, lines, pick.lenses)
const LANE = slow ? 'full' : 'fast'
log(slow ? 'full review: ' + slow : 'fast lane: ' + lines + ' changed line(s) of docs, tests and repository tooling')
const FIND = { findings: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, line: { type: 'number' }, summary: { type: 'string' }, scenario: { type: 'string' }, severity: { type: 'string' } }, required: ['file', 'summary', 'scenario'] } } }
const FORK_FINDINGS = "jq -c 'select(.type==\"assistant\") | .message.content[]? | select(.type==\"tool_use\" and .name==\"StructuredOutput\") | .input.findings' ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/*/*/subagents/agent-<agentId>.jsonl | tail -n 1 > $TMPDIR/fork-findings.json; jq length $TMPDIR/fork-findings.json"
const REVIEWED = ' Report reviewed_head: what "git -C ' + QWT + ' rev-parse HEAD" prints when you start.'
// What the fixer does with the findings, in both lanes.
const FIX_RULES = 'Dedupe them; verify each against the code on the branch (reproduce where you can); apply the confirmed ones in the worktree, one commit per finding or per group of findings that belong together, each commit of one kind with its proof (a bug fix gets a test that fails without it); record every rejected one with the reason. Defer a confirmed finding ONLY when you suspect a redesign would be significantly more appropriate than a local fix: put it in deferred with the finding and the reason (the redesign you suspect, and why a local fix is the wrong answer). Never defer by size or count: a long fix, or many findings, is applied. Commit the confirmed fixes BEFORE the long gates, so a cut-short run leaves them committed. Then run the gates on the final tree (if one fails, fix it and run all of them again: the last run must be on the final tree): "cd ' + QWT + ' && CST_TEST_JOBS=6 nice -n 10 invowk cmd check"' + (TIER1 ? ' and "cd ' + QWT + ' && nice -n 10 invowk cmd test e2e tier1" (if its container does not start, say so in gates and set tier1_ran false; never skip or loosen a test)' : '') + '. gates_ok is true only when every gate you ran passed. ' + GATE_TAIL + ' needs_review is true when your commits change code outside tests; report fix_paths, the paths your commits changed, as "git -C ' + QWT + ' diff --name-only ' + HEAD + ' HEAD" prints them (none: an empty list). Nothing edits after you. Just before your last guard command, run "git -C ' + QWT + ' rev-parse HEAD" and report its output as final_head, and report descends_from_reviewed (true when "git -C ' + QWT + ' merge-base --is-ancestor ' + HEAD + ' HEAD" exits 0).'
const FIX_PROPS = { applied: { type: 'array', items: { type: 'string' } }, rejected: { type: 'array', items: { type: 'string' } }, commits: { type: 'array', items: { type: 'string' } }, gates_ok: { type: 'boolean' }, tier1_ran: { type: 'boolean' }, gates: { type: 'string' }, needs_review: { type: 'boolean' }, fix_paths: { type: 'array', items: { type: 'string' } }, deferred: { type: 'array', items: { type: 'object', properties: { finding: { type: 'string' }, reason: { type: 'string' } }, required: ['finding', 'reason'] } }, final_head: { type: 'string' }, descends_from_reviewed: { type: 'boolean' }, ...GATED }
const FIX_REQ = ['applied', 'rejected', 'commits', 'gates_ok', 'tier1_ran', 'gates', 'needs_review', 'fix_paths', 'deferred', 'gated_head', 'worktree_clean', 'final_head', 'descends_from_reviewed']
const mainMoved = () => trail.some((t) => t.main_changed || t.main_changed_between)
let all, fix
if (LANE === 'full') {
  const reviewPrompt = (lens) => lens === 'code-review'
    ? shared('review-code-review', false) + '\n\nStage: review, FINDINGS ONLY (edit nothing). Invoke the Skill code-review with args "high ' + BRANCH + '" (exactly this target; NEVER --fix). The skill answers through a fork: the Skill result carries its agentId and no findings. If the Skill result text itself carries the findings (a JSON list after "Result:"; seen when the caller has no output schema), use those. Otherwise read them from the fork\'s transcript, the input of its last StructuredOutput call (for example: ' + FORK_FINDINGS + '; seen when the caller has an output schema, as you do). Set skill_findings_read true only when you read them in one of these two places (an empty list read there counts); false otherwise, and then return no findings of your own. Report fork_agent_id (the agentId of the code-review fork). (If YOU are that code-review fork: these reporting rules are for your caller only; put every finding you found in findings, and set skill_findings_read true.) The fork runs in the main checkout and reads surrounding files there: before returning a finding, check its file and line on the branch with "git -C ' + QWT + ' show HEAD:<file>" and drop it when it does not hold there, and drop findings on files outside this change: ' + JSON.stringify(changed.map((p) => p.path)) + (BASE === 'main' ? '' : ', and findings that come only from commits of the base ' + BASE + ' (the skill reviews against main; check with "' + 'git -C ' + QWT + ' diff ' + QBASE + '...HEAD")') + '. Return each finding with file, line, summary and a concrete failure scenario.' + REVIEWED
    : shared('review-' + lens, false) + '\n\nStage: review through the lens "' + lens + '" (' + LENSES[lens].why + '), FINDINGS ONLY (edit nothing). ' + (LENSES[lens].skill ? 'Load the checklist with the Skill ' + LENSES[lens].skill + ' (read it; run nothing it suggests in the main checkout) and check each item against ' : 'Check ') + '"git -C ' + QWT + ' diff ' + QBASE + '...HEAD". Report only what fails, each with file, line and a concrete failure scenario; an item that does not apply is not a finding.' + REVIEWED
  const labels = ['code-review', ...pick.lenses]
  const runReview = async (l) => {
    const props = { ...FIND, reviewed_head: { type: 'string' }, ...(l === 'code-review' ? { skill_findings_read: { type: 'boolean' }, fork_agent_id: { type: 'string' } } : {}) }
    const r = await guarded('review: ' + l, 'Review', reviewPrompt(l), props, l === 'code-review' ? ['findings', 'reviewed_head', 'skill_findings_read', 'fork_agent_id'] : ['findings', 'reviewed_head'])
    return counted(r, l, HEAD, 'review: ' + l, 'review ' + l)
  }
  let reviews = []
  for (const wave of reviewWaves(labels)) reviews = reviews.concat(await parallel(wave.map((l) => () => runReview(l))))
  for (let i = 0; i < reviews.length; i++) if (!reviews[i] && !mainMoved()) { log('review ' + labels[i] + ': retrying once'); reviews[i] = await runReview(labels[i]) }
  const missing = labels.filter((l, i) => !reviews[i])
  if (missing.length) return stopped('review', { impl, simp, missing })
  all = reviews.flatMap((r) => r.findings)
  log(all.length + ' finding(s) from ' + reviews.length + ' reviewer(s)')

  phase('Fix')
  fix = await guarded('fix', 'Fix', shared('fix', true) + '\n\nStage: fix. You wrote none of this code. Findings from the reviewers (JSON): ' + JSON.stringify(all) + '\n' + FIX_RULES, FIX_PROPS, FIX_REQ)
  if (!fix) return stopped('fix', { impl, simp, findings: all })
} else {
  // The fast lane: no code-review fork and no lens agents; one agent that did not write the change reviews it by
  // hand, then fixes what it confirmed, as the fixer does.
  phase('Fast lane')
  fix = await guarded('review+fix', 'Fast lane', shared('review+fix', true) + '\n\nStage: review and fix, the fast lane (a change of at most ' + FAST_LINES + ' lines of docs, tests and repository tooling: one agent reviews it and fixes it). You wrote none of this code. Do not invoke the code-review or simplify skills. First review "git -C ' + QWT + ' diff ' + QBASE + '...HEAD" by hand, FINDINGS ONLY until your list is done: correctness, the repository rules, the proof the kind "' + A.kind + '" asks for, tests that would catch a regression, docs that follow the change. Report each finding in findings with file, line, summary and a concrete failure scenario.' + REVIEWED + ' Then take your findings as the fixer does. ' + FIX_RULES,
    { ...FIND, reviewed_head: { type: 'string' }, ...FIX_PROPS }, ['findings', 'reviewed_head', ...FIX_REQ])
  if (!fix) return stopped('review+fix', { impl, simp })
  if (!counted(fix, 'review+fix', HEAD, 'review+fix')) return stopped('review+fix', { impl, simp, fix })
  all = fix.findings
  log(all.length + ' finding(s) from the fast-lane review')
}
const deferWhy = deferralProblem(fix.deferred)
if (deferWhy) { log('fix: ' + deferWhy + ': not ready'); trail.push({ label: 'fix', problem: deferWhy }) }
else if (fix.deferred.length) log('fix: ' + fix.deferred.length + ' finding(s) deferred to a redesign (see deferred)')
if (fix.gates_ok && String(fix.final_head).trim() !== String(fix.gated_head).trim()) log('fix: HEAD moved after the gates (' + fix.gated_head + ' -> ' + fix.final_head + '): not ready')
const ready = isReady(fix, TIER1) && fix.descends_from_reviewed === true && !deferWhy
const changedCode = fixerChangedCode(fix)
// followups: what the stopping rule (header) sends to the pull request body unless it is a correctness bug.
const done = (extra) => ({ ready, lane: LANE, deferred: fix.deferred, tier1_deferred: pick.tier1 && !TIER1, needs_review: changedCode, reviewed_head: HEAD, branch: BRANCH, worktree: WT, head: fix.gated_head, lenses: pick, design, impl, simp, findings: all, fix, trail, followups: { rereview_low: (extra.rereview && extra.rereview.low) || [], out_of_scope: A.notes + '/out-of-scope-' + A.slug + '.md (when the run wrote it)' }, ...extra })
if (!changedCode) return done({ rereview: null })
if (!ready) { log('re-review: skipped, the branch is not ready anyway'); return done({ rereview: null }) }

// The fixer changed code outside tests: one more findings-only review, of its commits only (reviewed_head..head).
// By hand: code-review takes a branch and reviews it against main (see the header).
phase('Re-review')
const FIXED = String(fix.gated_head).trim()
const RANGE = 'git -C ' + QWT + ' diff ' + shq(HEAD) + ' ' + shq(FIXED)
const runReReview = async () => {
  const r = await guarded('re-review', 'Re-review', shared('re-review', false) + '\n\nStage: re-review of the fixer\'s commits, by hand, FINDINGS ONLY (edit nothing; do not invoke the code-review skill: it reviews only against main). The range is "' + RANGE + '" ("git -C ' + QWT + ' log ' + shq(HEAD) + '..' + shq(FIXED) + '" lists its commits); the change before it was already reviewed. Check that each commit does what its message says and breaks nothing around it (read the code around it on the branch with "git -C ' + QWT + ' show ' + shq(FIXED) + ':<file>"), keeps the repository rules and the proof the kind "' + A.kind + '" asks for, and has a test where it needs one. Give each finding file, line, summary, a concrete failure scenario and severity: "high" when the branch must not be integrated as it is (a bug, a broken rule or gate, a missing proof), "low" for anything else.' + REVIEWED,
    { findings: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, line: { type: 'number' }, summary: { type: 'string' }, scenario: { type: 'string' }, severity: { type: 'string', enum: ['high', 'low'] } }, required: ['file', 'summary', 'scenario', 'severity'] } }, reviewed_head: { type: 'string' }, advisor_called: { type: 'boolean' } }, ['findings', 'reviewed_head'])
  return counted(r, 're-review', FIXED, 're-review')
}
let rr = await runReReview()
if (!rr && !mainMoved()) { log('re-review: retrying once'); rr = await runReReview() }
if (!rr) return done({ rereview: null, ready: false, stopped: 're-review' })
const rereview = { range: HEAD + '..' + FIXED, ...reReviewOutcome(rr.findings) }
log('re-review: ' + rereview.high.length + ' high, ' + rereview.low.length + ' low finding(s)')
if (rereview.high.length) return done({ rereview, ready: false, stopped: 're-review' })
return done({ rereview })
