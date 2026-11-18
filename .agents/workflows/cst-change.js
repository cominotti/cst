// SPDX-License-Identifier: MPL-2.0
export const meta = {
  name: 'cst-change',
  description: 'One CST change: design, implement, simplify, findings-only review, one independent fixer (branch ready)',
  whenToUse: 'A CST change worth a reviewed branch: a command, a fix, a refactor, a port. Integration and merge stay with the orchestrator.',
  phases: [
    { title: 'Design', detail: 'brief + advisor review' },
    { title: 'Implement', detail: 'worktree, commits of one kind, check green' },
    { title: 'Simplify', detail: 'quality pass by hand, in the worktree' },
    { title: 'Review', detail: 'findings only: code-review high <branch> + checklist lenses by path' },
    { title: 'Fix', detail: 'one independent agent applies confirmed findings, runs the gates once' },
  ],
}
//
// The CST per-change workflow (Workflow tool: name "cst-change", from .claude/workflows/).
// It ends at "branch ready" (result.ready): integration (one branch, one CI run with tier 2, fast-forward) stays
// with the orchestrator.
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
//   tier1     "run" (default) | "defer": a batch runs tier 1 once on its integration branch; the result says so
//   mode      "new" (default) | "continue" (the worktree exists) | "review" (review + fix of an existing branch)
//             | "selftest" (only the self-tests below)
//
// Facts this script is built on (checked 2026-11-18; recheck after a Claude Code upgrade):
// - A workflow agent has the Skill tool (and the advisor) but not Agent or Workflow; it loads AGENTS.md and the
//   rules, not the conversation. Its shell starts in the MAIN checkout on every call (cd does not persist), so
//   every command below names the worktree (git -C <worktree>, cd <worktree> && ... in the same call).
// - The code-review skill runs as a fork in the session's main directory: it reviews the right tree only with an
//   explicit target (code-review "high <branch>"), reads surrounding files from main, and must never get --fix.
//   The caller's Skill result carries only the fork's agentId ("Skill execution completed"): the findings are the
//   input of the fork's last StructuredOutput call, in its transcript (~/.claude/projects/*/*/subagents/
//   agent-<agentId>.jsonl). The review agent reads them there and says so (skill_findings_read); a review that
//   could not is a failed review (retried once, then the run stops), never an empty one.
//   The simplify skill follows the agent's directory (the main checkout), so this script does simplify by hand.
// - Never send a message to a running workflow agent: it revives a second copy of it. Change the brief instead.
// - Do not move or edit the main checkout while a run is going: the guard below stops the run when it changes.
// - Unverified: whether code-review "high <branch>" reviews only <base>..<branch> when the base is not main; the
//   review agent drops findings on files outside this change's paths and, with a base other than main, findings
//   that come only from the base's own commits. Named lookup through .claude/workflows was
//   not tried before the first merge (scriptPath was).
// - Every agent pastes the output of GUARD_CMD for the main checkout (its HEAD, status lines, staged and unstaged
//   contents and untracked files' contents) before and after; the script stops when an
//   agent's two readings differ or when one agent's "before" differs from the previous agent's "after".
// - The branch head is checked the same way: simplify reports the head it leaves (and a clean worktree), every
//   reviewer the head it reviewed (it must be that one), and the fixer the head it gated and the head after
//   everything (they must be equal for ready).

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

// Which checklist lenses review a change, and whether tier 1 must run, from the paths it touches
// (git diff --name-status --no-renames). Pure: tested below at every start.
function pickLenses(kind, changed) {
  if (!changed.length) throw new Error('nothing changed beyond the base')
  for (const p of changed) if (!/^[AMDT]$/.test(p.status)) throw new Error('changed path status not in A/M/D/T: ' + JSON.stringify(p))
  const code = changed.filter((p) => !MD.test(p.path))
  const machine = code.filter((p) => !REPO_ONLY.test(p.path) && (!UNIT_TEST.test(p.path) || E2E.test(p.path)))
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

// Why a review does not count ('' when it counts): the code-review agent must have read the fork's findings, and
// every reviewer must have reviewed the head the stage before reported. Pure: tested below.
function reviewProblem(lens, r, head) {
  if (lens === 'code-review' && r.skill_findings_read !== true) return 'the code-review findings were not read'
  if (String(r.reviewed_head).trim() !== head) return 'reviewed ' + String(r.reviewed_head).trim() + ', not ' + head
  return ''
}
// Whether the fixer's report makes the branch ready. final_head (read after everything) must be the gated head: a
// commit after the gates is ungated. Pure: tested below.
function isReady(fix, tier1) {
  return !!(fix.gates_ok && fix.worktree_clean && (!tier1 || fix.tier1_ran) && fix.gated_head && String(fix.final_head).trim() === String(fix.gated_head).trim())
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
  eq([reviewProblem('code-review', { skill_findings_read: true, reviewed_head: 'h\n' }, 'h'), reviewProblem('port', { reviewed_head: 'h' }, 'h')], ['', ''], 'reviews that count')
  eq(reviewProblem('port', { reviewed_head: 'g' }, 'h'), 'reviewed g, not h', 'a review of another head')
  const F = { gates_ok: true, worktree_clean: true, tier1_ran: false, gated_head: 'h', final_head: 'h' }
  eq([isReady(F, false), isReady({ ...F, final_head: 'h2' }, false), isReady({ ...F, gated_head: '', final_head: '' }, false), isReady(F, true), isReady({ ...F, worktree_clean: false }, false)], [true, false, false, false, false], 'ready')
  eq([shq('/a/b-c_d.e'), shq('/a b'), shq("/it's")], ['/a/b-c_d.e', "'/a b'", "'/it'\\''s'"], 'shell quoting')
  eq([worktreeProblem('/r/cst', '/r/cst-wt-x'), worktreeProblem('/r/cst', '/r/cst/.wt/x'), worktreeProblem('/r/cst', '/r/cst'), worktreeProblem('/r/cst', 'wt')], ['', 'must be outside the main checkout /r/cst', 'must be outside the main checkout /r/cst', 'must be an absolute path'], 'worktree path')
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
const DIFF_CMD = 'git -C ' + QWT + ' diff --name-status --no-renames ' + QBASE + '...HEAD'
const GATE_TAIL = 'Then report gated_head (git -C ' + QWT + ' rev-parse HEAD) and worktree_clean (true when git -C ' + QWT + ' status --porcelain prints nothing).'
const shared = (stage, rereadBrief) => [
  'Repository rules: AGENTS.md ("Rules", "Commands", "Test gotchas") and .agents/rules/*.md apply; the kind "' + A.kind + '" decides the proof (.agents/rules/refactor-vs-behaviour.md).',
  'Work only in the worktree ' + WT + ' (branch ' + BRANCH + '). Your shell starts in the main checkout on EVERY call: use git -C ' + QWT + ', absolute paths, or "cd ' + QWT + ' && ..." inside the same command. Never edit, commit or check out anything in ' + MAIN + '.',
  'Shared machine: TMPDIR=$HOME/.cache/cst-change-' + A.slug + '/' + stage + ' for anything you run (remove that folder when done), a timeout on every harness you write, no large files in /tmp; never sudo, never tier 2 E2E, never git stash, never push; repository text in English, with no personal or private names.',
  'Do not invoke the code-review or simplify skills unless this prompt says so.' + (rereadBrief ? ' Re-read the brief ' + A.brief + ' before your final check: it may have grown.' : ''),
  'Guard: run exactly "' + GUARD_CMD + '" as your first and as your last command, and paste each output verbatim into main_before and main_after.',
].join('\n')
const GUARDED = { main_before: { type: 'string' }, main_after: { type: 'string' } }
const GREQ = ['main_before', 'main_after']
const CHANGED = { type: 'array', items: { type: 'object', properties: { path: { type: 'string' }, status: { type: 'string' } }, required: ['path', 'status'] } }
const GATED = { gated_head: { type: 'string' }, worktree_clean: { type: 'boolean' } }

let lastAfter = null
const trail = []
// Runs one agent and checks the guard; returns null (and logs why) when the run must stop.
async function guarded(label, phase_, prompt, props, required) {
  const r = await agent(prompt, { label, phase: phase_, schema: { type: 'object', properties: { ...props, ...GUARDED }, required: [...required, ...GREQ] } })
  if (!r) { log(label + ': the agent failed or was skipped: stopping'); trail.push({ label, failed: true }); return null }
  const norm = (s) => String(s).trim()
  if (norm(r.main_before) !== norm(r.main_after)) { log(label + ': the main checkout changed during this agent: stopping'); trail.push({ label, main_changed: true }); return null }
  if (lastAfter !== null && norm(r.main_before) !== lastAfter) { log(label + ': the main checkout changed between agents: stopping'); trail.push({ label, main_changed_between: true }); return null }
  lastAfter = norm(r.main_after)
  return r
}
const stopped = (stage, extra) => ({ ready: false, stopped: stage, branch: BRANCH, worktree: WT, trail, ...extra })

let design = null
if (MODE === 'new' && A.kind !== 'docs') {
  phase('Design')
  design = await guarded('design', 'Design', shared('design', false) + '\n\nStage: design. Read the brief ' + A.brief + ' and the module READMEs it names. Write the design note ' + A.notes + '/DESIGN-' + A.slug + '.md: what changes, the commits in order with their kind and proof, the tests (unit and E2E per AGENTS.md), what the README says after, risks. Then call the advisor tool once with the note written and apply its advice to the note. If the tool is missing or fails, say so (advisor_called false) and leave the note unreviewed. Edit nothing in the repository.',
    { note: { type: 'string' }, advisor_called: { type: 'boolean' }, advisor_changes: { type: 'string' } }, ['note', 'advisor_called'])
  if (!design) return stopped('design')
  if (!design.advisor_called) log('design: the advisor was not called; the note is unreviewed')
}

let impl = null, simp = null, changed = [], HEAD = ''
if (MODE === 'new' || MODE === 'continue') {
  phase('Implement')
  const start = MODE === 'new'
    ? 'Create the worktree: git -C ' + QM + ' worktree add -b ' + QBRANCH + ' ' + QWT + ' ' + QBASE + '. If the branch or the worktree already exists, stop and report it (check_ok false).'
    : 'The worktree ' + WT + ' exists: continue from its state' + ' (read ' + A.notes + '/DESIGN-' + A.slug + '.md if it exists).'
  impl = await guarded('implement', 'Implement', shared('implement', true) + '\n\nStage: implement "' + A.title + '". ' + start + ' Follow the brief ' + A.brief + (design ? ' and the design note ' + A.notes + '/DESIGN-' + A.slug + '.md' : '') + '. Commits of one kind each, with their proof; tests with the change (a behaviour change has a test that fails on the old code, checked). While working run "cd ' + QWT + ' && invowk cmd test module <module>"; at the end run "cd ' + QWT + ' && invowk cmd check" once: check_ok is true only when it passed, and check holds its summary. ' + GATE_TAIL + ' Return changed_paths exactly as "' + DIFF_CMD + '" prints them.',
    { commits: { type: 'array', items: { type: 'string' } }, changed_paths: CHANGED, check_ok: { type: 'boolean' }, check: { type: 'string' }, notes: { type: 'string' }, ...GATED }, ['commits', 'changed_paths', 'check_ok', 'check', 'gated_head', 'worktree_clean'])
  if (!impl) return stopped('implement')
  if (!impl.check_ok || !impl.worktree_clean) return stopped('implement', { impl })

  phase('Simplify')
  simp = await guarded('simplify', 'Simplify', shared('simplify', false) + '\n\nStage: simplify, by hand (quality only: no bug hunting, no behaviour change). On "git -C ' + QWT + ' diff ' + QBASE + '...HEAD": reuse what cst_core provides (.agents/rules/reuse-primitives.md), remove needless code, keep each function at one level of abstraction. Commit each change as a refactor proven by the unchanged tests of what it touched ("cd ' + QWT + ' && invowk cmd test module <module>"). Report what you changed and what you left on purpose, and return changed_paths exactly as "' + DIFF_CMD + '" prints them after your commits. Commit every edit you keep: the review sees only commits. ' + GATE_TAIL,
    { commits: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, changed_paths: CHANGED, ...GATED }, ['commits', 'summary', 'changed_paths', 'gated_head', 'worktree_clean'])
  if (!simp) return stopped('simplify', { impl })
  if (!simp.worktree_clean) { log('simplify: the worktree has uncommitted edits: stopping'); return stopped('simplify', { impl, simp }) }
  changed = simp.changed_paths
  HEAD = String(simp.gated_head).trim()
} else {
  phase('Review')
  const survey = await guarded('survey', 'Review', shared('survey', false) + '\n\nStage: survey for a review of an existing branch (edit nothing). Report worktree_exists (the folder ' + WT + ' exists and git -C ' + QWT + ' rev-parse --abbrev-ref HEAD prints ' + BRANCH + '), head (git -C ' + QWT + ' rev-parse HEAD), and changed_paths exactly as "' + DIFF_CMD + '" prints them.',
    { worktree_exists: { type: 'boolean' }, head: { type: 'string' }, changed_paths: CHANGED }, ['worktree_exists', 'head', 'changed_paths'])
  if (!survey || !survey.worktree_exists) return stopped('survey', { survey })
  changed = survey.changed_paths
  HEAD = String(survey.head).trim()
}
changed = changed.map((p) => ({ path: p.path, status: String(p.status).trim().charAt(0).toUpperCase() }))

phase('Review')
let pick
try { pick = pickLenses(A.kind, changed) } catch (e) { log(String(e)); return stopped('lenses', { impl, simp, error: String(e) }) }
const TIER1 = pick.tier1 && A.tier1 !== 'defer'
if (pick.tier1 && !TIER1) log('tier 1 deferred to the integration branch')
log('lenses: ' + (pick.lenses.join(', ') || 'none') + '; tier 1: ' + pick.tier1)
const FIND = { findings: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, line: { type: 'number' }, summary: { type: 'string' }, scenario: { type: 'string' }, severity: { type: 'string' } }, required: ['file', 'summary', 'scenario'] } } }
const FORK_FINDINGS = "jq -c 'select(.type==\"assistant\") | .message.content[]? | select(.type==\"tool_use\" and .name==\"StructuredOutput\") | .input.findings' $HOME/.claude/projects/*/*/subagents/agent-<agentId>.jsonl | tail -n 1"
const REVIEWED = ' Report reviewed_head: what "git -C ' + QWT + ' rev-parse HEAD" prints when you start.'
const reviewPrompt = (lens) => lens === 'code-review'
  ? shared('review-code-review', false) + '\n\nStage: review, FINDINGS ONLY (edit nothing). Invoke the Skill code-review with args "high ' + BRANCH + '" (exactly this target; NEVER --fix). The skill answers through a fork: the Skill result carries its agentId and no findings. Read the findings from the fork\'s transcript, the input of its last StructuredOutput call (for example: ' + FORK_FINDINGS + '). Set skill_findings_read true only when you read them there (an empty list read there counts); false otherwise, and then return no findings of your own. The fork runs in the main checkout and reads surrounding files there: before returning a finding, check its file and line on the branch with "git -C ' + QWT + ' show HEAD:<file>" and drop it when it does not hold there, and drop findings on files outside this change: ' + JSON.stringify(changed.map((p) => p.path)) + (BASE === 'main' ? '' : ', and findings that come only from commits of the base ' + BASE + ' (the skill reviews against main; check with "' + 'git -C ' + QWT + ' diff ' + QBASE + '...HEAD")') + '. Return each finding with file, line, summary and a concrete failure scenario.' + REVIEWED
  : shared('review-' + lens, false) + '\n\nStage: review through the lens "' + lens + '" (' + LENSES[lens].why + '), FINDINGS ONLY (edit nothing). ' + (LENSES[lens].skill ? 'Load the checklist with the Skill ' + LENSES[lens].skill + ' (read it; run nothing it suggests in the main checkout) and check each item against ' : 'Check ') + '"git -C ' + QWT + ' diff ' + QBASE + '...HEAD". Report only what fails, each with file, line and a concrete failure scenario; an item that does not apply is not a finding.' + REVIEWED
const labels = ['code-review', ...pick.lenses]
const runReview = async (l) => {
  const props = { ...FIND, reviewed_head: { type: 'string' }, ...(l === 'code-review' ? { skill_findings_read: { type: 'boolean' } } : {}) }
  const r = await guarded('review: ' + l, 'Review', reviewPrompt(l), props, l === 'code-review' ? ['findings', 'reviewed_head', 'skill_findings_read'] : ['findings', 'reviewed_head'])
  const why = r ? reviewProblem(l, r, HEAD) : ''
  if (why) { log('review ' + l + ': ' + why); trail.push({ label: 'review: ' + l, problem: why }); return null }
  return r
}
let reviews = await parallel(labels.map((l) => () => runReview(l)))
for (let i = 0; i < reviews.length; i++) if (!reviews[i]) { log('review ' + labels[i] + ': retrying once'); reviews[i] = await runReview(labels[i]) }
const missing = labels.filter((l, i) => !reviews[i])
if (missing.length) return stopped('review', { impl, simp, missing })
const all = reviews.flatMap((r) => r.findings)
log(all.length + ' finding(s) from ' + reviews.length + ' reviewer(s)')

phase('Fix')
const fix = await guarded('fix', 'Fix', shared('fix', true) + '\n\nStage: fix. You wrote none of this code. Findings from the reviewers (JSON): ' + JSON.stringify(all) + '\nDedupe them; verify each against the code on the branch (reproduce where you can); apply the confirmed ones in the worktree, each in a commit of its own kind with its proof (a bug fix gets a test that fails without it); record every rejected one with the reason. Then run the gates ONCE on the final tree: "cd ' + QWT + ' && invowk cmd check"' + (TIER1 ? ' and "cd ' + QWT + ' && invowk cmd test e2e tier1" (if its container does not start, say so in gates and set tier1_ran false; never skip or loosen a test)' : '') + '. gates_ok is true only when every gate you ran passed. ' + GATE_TAIL + ' needs_review is true when your commits change code outside tests. Nothing edits after you. Just before your last guard command, run "git -C ' + QWT + ' rev-parse HEAD" and report its output as final_head.',
  { applied: { type: 'array', items: { type: 'string' } }, rejected: { type: 'array', items: { type: 'string' } }, commits: { type: 'array', items: { type: 'string' } }, gates_ok: { type: 'boolean' }, tier1_ran: { type: 'boolean' }, gates: { type: 'string' }, needs_review: { type: 'boolean' }, final_head: { type: 'string' }, ...GATED }, ['applied', 'rejected', 'commits', 'gates_ok', 'tier1_ran', 'gates', 'needs_review', 'gated_head', 'worktree_clean', 'final_head'])
if (!fix) return stopped('fix', { impl, simp, findings: all })
const ready = isReady(fix, TIER1)
if (fix.gates_ok && String(fix.final_head).trim() !== String(fix.gated_head).trim()) log('fix: HEAD moved after the gates (' + fix.gated_head + ' -> ' + fix.final_head + '): not ready')
return { ready, tier1_deferred: pick.tier1 && !TIER1, needs_review: fix.needs_review, branch: BRANCH, worktree: WT, head: fix.gated_head, lenses: pick, design, impl, simp, findings: all, fix, trail }
