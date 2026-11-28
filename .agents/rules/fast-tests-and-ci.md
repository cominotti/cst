---
description: Keep unit tests fast and in-process, fixtures minimal, local iteration targeted, and CI parallel and fail-closed
---
<!-- SPDX-License-Identifier: MPL-2.0 -->
<!-- global rule: loaded for every request -->

# Fast tests and CI

Keep the feedback loop fast without weakening any check or hiding a host boundary.

## Unit tests run in-process by default

- Test domain logic, application flows, validation, error messages, exit
  codes and CLI formatting **in-process** through port fakes (`ports.py`).
- Reserve `subprocess.run`, `Popen` and shell command stubs for genuine
  process-boundary tests:
  - shell front shims (`*-setup.sh`) and `./cst` dispatch routing;
  - zipapp builds and shebang execution from `/`;
  - real OS signals (`SIGTERM`, `SIGINT`, `SIGHUP`), `fcntl.flock`
    contention, `pty` and subprocess timeouts;
  - bytecode and `stdin`-socket runner gates (`tests/run_unit.py`).
- Never spawn a subprocess and a bash stub in a `subTest` loop or across
  dozens of pure behaviour tests.
- Keep subprocess counts in unit test files within the repository's
  ratcheting budgets (`invowk cmd fix budgets --apply`; raising a budget
  requires its reason in the pull request).

## Never put heavy setup in `setUp`

- Do not copy large directory trees or re-parse large repository files inside
  `setUp` or per-iteration `subTest` helpers.
- Build shared synthetic templates once in `setUpClass` (with `addClassCleanup`)
  or construct minimal fixtures covering only the files the test mutates.
- Keep one dedicated test checking the real repository files when the gate
  targets the checkout, and assert equivalence (such as `host_closure()`)
  between any synthetic template and the real files it stands in for.
- In-process tests must clean up process-global state they touch
  (`os.environ`, signal dispositions, file descriptors) so [tests/run_unit.py](../../tests/run_unit.py)
  workers stay isolated.

## Local iteration vs. final gate

- During active editing and review fixes, run targeted checks first:
  `invowk cmd test module NAME` or `invowk cmd test affected`.
- Run the full suite (`invowk cmd check` and `invowk cmd test mbt --profile ci`)
  as the final verification before pushing, and only re-run after further
  edits.

## CI is parallel and fail-closed

- Never skip CI to merge ([AGENTS.md](../../AGENTS.md)).
- Keep independent E2E jobs free of avoidable serial bottlenecks when `changes`
  has already classified the scope, and keep `CI Status` fail-closed over
  every required job.
- Any conditional CI scope filter in [.github/workflows/ci.yml](../../.github/workflows/ci.yml)
  must be **fail-closed**:
  - on `workflow_dispatch`, `force_e2e`, unknown paths or uncertain diffs,
    default to running; on pushes to `main` and in nightly runs, keep full
    end-to-end coverage;
  - skip a tier only when all changed files fall outside its execution and
    import closure (either via a tracked allow-list such as `quint_path` or an
    audited skip-safe filter backed by a fail-closed fallback);
  - back every filter with a static unit gate (such as
    `tests/test_quint_task.py`) computing the full transitive AST import
    closure so newly imported modules cannot slip past CI.
