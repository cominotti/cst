<!-- SPDX-License-Identifier: MPL-2.0 -->
# Formal verification, model checking, and model-based testing (MBT)

Which formal verification, model checking, and model-based testing (MBT) techniques fit CST well, where they pay off, and how they fit the repository's rules and gates.

## Why CST is a strong fit

Most system-administration tools mix business rules directly with `subprocess`, `os`, and `systemd` calls, making model-based testing and protocol verification expensive. CST already avoids that trap:

- **Hexagonal boundaries ([architecture.md](../architecture.md)):** Pure rules live in `domain.py` / `domain/` with `frozen=True, slots=True` dataclasses and no I/O; multi-step mutations live in `application.py` / `application/` typed against narrow `Protocol` ports (`ports.py`) that already have in-memory fakes in unit tests.
- **Explicit lifecycle invariants ([AGENTS.md](../../AGENTS.md)):** Machine-changing commands share strong contracts across modules (`plan` changes nothing, `apply` converges to what `status` reports, a second run is a no-op, interrupted runs recover or roll back cleanly, and `undo`/`uninstall` restores prior state).
- **High-stakes stateful protocols:** Several modules coordinate multi-step mutations across crashes, reboots, locks, and partial failures where unit tests built around hand-picked examples can easily miss interleavings or edge cases.

## Applicable techniques and libraries

Only techniques with clear affinity and positive return on effort for CST are included below.

### 1. Stateful MBT and property-based testing: Hypothesis

- **Tools:** `hypothesis` (`hypothesis.stateful.RuleBasedStateMachine`, `@rule`, `@invariant`, `@precondition`, and `@given`).
- **Affinity:** Very high (best overall return on effort).
- **Rationale:**
  - **Stateful MBT (`RuleBasedStateMachine`):** Models a minimal abstract state (for example, expected files, component states, or volume flags) and drives the real `application/` use cases over their existing in-memory port fakes through arbitrary sequences of `plan`, `apply`, `status`, `remove`, `recover`, and injected failures (I/O errors, partial writes, restarts). Hypothesis checks `@invariant`s after every step and shrinks any failing sequence to the minimal reproduction.
  - **Property-based testing (`@given`):** Exercises pure `domain/` functions (parsers, retention schedules, arithmetic limits, state serialization) across wide ranges of generated inputs, catching boundary, ordering, and round-trip bugs that example-based tests miss.
- **Fit with CST:**
  - Pure Python, so it works smoothly with `requires-python = ">=3.15"` without native extension churn.
  - Integrates directly with `unittest.TestCase`.
  - Can live in a dedicated `[dependency-groups]` entry in [pyproject.toml](../../pyproject.toml) (such as `mbt`, alongside `mutation` and `coverage`) and a separate test location/task so `invowk cmd test unit` remains 100% standard-library Python.
- **Caveat:** CST enforces `[tool.ty.rules] all = "error"` and `ruff --select ANN` across every Python file, including tests ([dev-tools.md](../dev-tools.md)). Dynamic decorators such as `@rule`, `@invariant`, and `@st.composite` can cause typing friction with strict checkers, so ergonomics under `ty` should be validated on a small pilot before expanding.

### 2. Exhaustive fault-injection and finite-state exploration in Python stdlib

- **Tools:** Plain Python (`unittest` + instrumented port fakes; zero external dependencies).
- **Affinity:** High for short, critical multi-step mutation protocols.
- **Rationale:**
  - CST's critical `migrate` / `apply` / `backup` steps typically perform a small, bounded number of mutating port calls ($N \approx 5\text{–}15$: writing flags, setting `+C`, copying, syncing, renaming, deleting).
  - Instead of sampling failure points randomly, an instrumented port fake can abort (simulating `SIGKILL`, `ENOSPC`, or power loss) after every $k$-th mutating step for all $k \in [1, N]$ — and optionally inject a second failure during `recover()` — then run recovery and a follow-up `apply` to assert safety and convergence invariants exhaustively against the **real production code** (no model-vs-code drift).
- **Fit with CST:** Zero new dependencies, runs directly inside `invowk cmd test unit` on `/usr/bin/python3 -I`, and introduces no `uv.lock` or `ty` compatibility issues.

### 3. Protocol model checking: Quint (preferred) or TLA+ / TLC

- **Tools:** [Quint](https://quint-lang.org/) (typed specification language with built-in simulator and Apalache symbolic checker) or TLA+ / TLC.
- **Affinity:** High for 3–4 critical crash-recovery and concurrency protocols; low everywhere else.
- **Rationale:**
  - When a protocol involves interleaved failures across boundaries (crash during copy, reboot loop limits, lock contention between units, VM stopping mid-backup, SIGHUP handling, torn-state markers), a model checker systematically explores interleavings that example tests and random walks can miss.
  - **Why Quint over raw TLA+:** Quint has a modern typed syntax with a shorter learning curve and outputs traces in standard ITF JSON, enabling **model-based trace replay** directly against CST's Python port fakes so the specification stays tied to the implementation rather than drifting into stale documentation. TLA+ / TLC remains the more mature explicit-state alternative, at the cost of a JVM dependency and greater syntactic distance from Python.
- **Scope discipline:** Use only for protocols where partial failure risks data loss or leaving the workstation unrecoverable. Do not write formal specs for CLI parsing, config rendering, or read-only reporting.

### 4. Targeted symbolic execution and SMT: CrossHair (or Z3)

- **Tools:** `crosshair` (`crosshair check`, `crosshair diffbehavior`) or `z3-solver` for finite decision-table checks.
- **Affinity:** Narrow, specialized niche.
- **Rationale:**
  - Pure, strictly annotated `domain/` functions match CrossHair's sweet spot. In particular, `crosshair diffbehavior old_fn new_fn` directly complements CST's differential parity workflow ([cst-refactor-parity](../../.agents/skills/cst-refactor-parity/SKILL.md)) by proving input equivalence on pure helpers rather than relying only on sampled inputs.
- **Caveats:**
  - CrossHair hooks into CPython internals and symbolic proxies, which often lag behind brand-new Python releases; compatibility with `requires-python = ">=3.15"` must be verified before adding it.
  - Symbolic execution degrades quickly on `re`, `datetime`/`zoneinfo`, and filesystem strings, confining practical use to numeric, interval, and small structured logic (for example, memory share calculations or discrete state transitions).

## Target map with rationale

| Target | Critical invariants and risks | Best-fit techniques | Rationale |
|---|---|---|---|
| [btrfs/docker_volumes_nocow/application.py](../../btrfs/docker_volumes_nocow/application.py) | Order `flag` before `+C`, twin creation/copy/verification/`sync`/`rename(data, old)`/`rename(new, data)`/`remove_tree(old)`, interrupted migration `recover()`, and `GIVE_UP_AFTER` boot-attempt tracking. | Exhaustive stdlib fault-injection, Quint/TLA+, Hypothesis (`RuleBasedStateMachine`) | Short, high-risk crash-recovery protocol where losing `data` or losing the migration flag after `+C` is catastrophic. Small step count makes exhaustive cut-point injection practical immediately; Quint covers multi-boot + daemon-busy interleavings. |
| [btrfs/bulk/application/items.py](../../btrfs/bulk/application/items.py) | `classify` state machine (`kept`, `ok`, `mkdir`, `link`, `move`, `move-subvol`, `nocow`, `wait`, `unexpected`), `after` dependency ordering, migration journal, and `recover()`. | Hypothesis (`RuleBasedStateMachine`), exhaustive stdlib fault-injection | Complex filesystem state matrix with dependency constraints and interrupted-move recovery; MBT excels at uncovering missing cases in `classify` + `recover` combinations. |
| [vm/application/nas.py](../../vm/application/nas.py) | Live snapshot, `restic`, `blockcommit --active --pivot`, live vs. persistent `domblklist` check before `remove_overlay`, and `vm-nas-torn.json` tracking across VM stops, timeouts, and `SIGHUP`. | Quint/TLA+ (with trace replay), Hypothesis (`RuleBasedStateMachine`) | External state transitions (VM shutting down mid-backup, commit timeout, stale persistent XML) create subtle interleavings where deleting an overlay too early would corrupt VM boot state. |
| [cst_core/application/install.py](../../cst_core/application/install.py) and [cst_core/domain/record.py](../../cst_core/domain/record.py) | `plan`/`install`/`check`/`mark`/`remove`/`forget` lifecycle, adoption of pre-existing current files, `pending` vs. `done` states, refusal of newer record formats, and `parse_record(render_record(r)) == r` with no orphaned component states. | Hypothesis (`RuleBasedStateMachine` for `InstallService`; `@given` for `record.py`) | Core state machine shared by all modules; MBT verifies that arbitrary sequences of installs, partial failures, adoptions, removals, and forgets never leave `/var/lib/cst/installed.json` inconsistent with disk state. |
| [btrfs/snapshot/domain.py](../../btrfs/snapshot/domain.py) | `btrbk`-compatible `plan()`, `KEEP_NEWEST`, `MIN_AGE`, `MASS_DELETE` circuit breaker, backwards-clock / NTP grace / future-dated snapshots, DST fold boundaries, and policy/name round-trips. | Hypothesis (`@given`) | Pure functions with rich calendar, boundary, and bucket logic; property tests catch edge timestamps, policy combinations, and circuit-breaker thresholds far better than fixed fixtures. |
| [cpu/game/domain.py](../../cpu/game/domain.py) | Write-ahead `game.state` so `off` reverts a partially completed `on`, `game.intended` drift repair, `printf_q` / `printf_q_words` round-trip, and `run_lock` coordination with `cpu-power`. | Hypothesis (`@given` + stateful `on`/`tick`/`off`), Quint/TLA+ if lock contention grows | Write-ahead undo and drift repair have clear stateful invariants; `printf_q` quoting round-trips are natural `@given` targets. |
| [swap/application/apply.py](../../swap/application/apply.py) and [swap/domain.py](../../swap/domain.py) | Precondition refusals before any write, idempotent marked lines, and pure memory/share/size calculations. | Hypothesis (`RuleBasedStateMachine` + `@given`), CrossHair (`diffbehavior` on pure math) | Pure numerical helpers fit property and differential checks; `apply` benefits from stateful idempotency and refusal-safety checks. |
| [toolchains/application/prune.py](../../toolchains/application/prune.py) and [bluetooth/setup/application/change.py](../../bluetooth/setup/application/change.py) | Cleanup of leftovers after interrupted folder renames (`prune`); resuming pending unit restarts after a partial `apply` (`bluetooth/setup`). | Exhaustive stdlib fault-injection, Hypothesis (`RuleBasedStateMachine`) | Small, multi-step recovery workflows that can be verified thoroughly against port fakes with minimal modeling overhead. |

## Fitting CST's rules and gates

Any adoption must respect CST's architectural boundaries ([dev-tools.md](../dev-tools.md)):

- **Keep runtime and unit tests 100% stdlib:** Production helpers always run on `/usr/bin/python3 -I` without external packages. Third-party verification tools (such as `hypothesis`) belong in an optional `[dependency-groups]` entry in [pyproject.toml](../../pyproject.toml), pinned in `uv.lock`, audited by `invowk cmd ci uv-audit`, and invoked by a separate `invowk` command rather than `invowk cmd test unit`.
- **Keep `ty` and `ruff --select ANN` strict:** All Python files under the repo's included folders are checked with `[tool.ty.rules] all = "error"` and `ANN` rules. Validate any MBT test pattern against `invowk cmd test types` early.
- **Know what fakes and models cannot prove:** Unit-level MBT and protocol model checking prove properties relative to the port abstractions and assumptions. How real `btrfs`, `systemd`, `udev`, `cgroups`, `polkit`, and `libvirt` behave remains the responsibility of tier 1 and tier 2 E2E suites ([tests/e2e/README.md](../../tests/e2e/README.md)).

## Recommended adoption order

1. **Pilot stdlib exhaustive fault-injection** on [btrfs/docker_volumes_nocow/application.py](../../btrfs/docker_volumes_nocow/application.py): zero new dependencies, runs inside `invowk cmd test unit`, and immediately tests every crash point and recovery path.
2. **Pilot Hypothesis** in an isolated `uv` group on [btrfs/snapshot/domain.py](../../btrfs/snapshot/domain.py) (`@given`) and [cst_core/application/install.py](../../cst_core/application/install.py) (`RuleBasedStateMachine`), verifying `ty` and `ANN` compatibility on Python 3.15+.
3. **Pilot Quint with trace replay** on [btrfs/docker_volumes_nocow/application.py](../../btrfs/docker_volumes_nocow/application.py) or [vm/application/nas.py](../../vm/application/nas.py) if multi-actor or multi-boot failure interleavings justify maintaining an explicit protocol spec.

## Status (2026-11-24): what was adopted

This plan has been carried out. The gates, profiles, worker counts and measured costs are in [dev-tools.md](../dev-tools.md); this section only maps the plan to what landed.

- **Exhaustive fault injection** (technique 2): `tests/fault_injection.py` drives every crash and failure point of docker-volumes-nocow, iocost, scrub-limit, the cache, toolchains' prune, bluetooth's setup and bulk's items, inside the unit suite. Each bug it found was fixed with a test that fails on the old code, or was written down for a design choice.
- **Hypothesis** (technique 1), in uv's dev group only: `tests/mbt` holds the stateful machines and properties (install record, snapshot domain, the iocost, scrub-limit and cache adapters, bulk's items, the game profile, swap's apply). `invowk cmd test mbt` runs them. CI uses a fixed profile and the nightly a random seed it prints.
- **Quint with TLC and trace replay** (technique 3): docker-volumes-nocow's boot protocol and vm's NAS backup have specs checked over their whole state space by `invowk cmd test quint`. Their traces are replayed against the real code in the unit suite.
- **Not adopted:**
  - CrossHair (technique 4): the pilot found no proof and gave false counterexamples. The three real gaps it pointed at became unit tests.
  - A Hypothesis machine for docker-volumes-nocow: exhaustive fault injection and TLC already cover its whole state space.
  - Quint for the game profile: not until its lock contention grows.
