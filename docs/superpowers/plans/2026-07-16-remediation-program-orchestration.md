# Captain Cook Remediation Program Orchestration Plan

> **Orchestrator-owned:** Worker sessions may read this file but must not edit
> it. The orchestrator alone dispatches packets, integrates commits, updates
> checkboxes, and records evidence.

**Goal:** Convert the four overlapping 2026-07-16 remediation plans into one
conflict-safe implementation program with explicit architecture authority,
dependencies, file ownership, review gates, and handoffs.

**Integration baseline:** local `main@78f3cf1` plus the hardened system plan at
`docs/system-gap-remediation-plan@d445ce3`, integrated on
`feat/system-remediation-orchestration`. `origin/main` is not a dispatch base;
it is 39 local commits behind. The dirty `feat/householder-runtime` root
worktree is not an implementation workspace.

## Binding architecture decisions

1. The FastAPI gateway backed by MariaDB is the only production writer and
   source of delivery lifecycle truth. JSON remains an explicit offline/demo
   adapter. SQLite is legacy import input only.
2. The gateway runs as a Captain-managed Python process. Compose owns Captain's
   MariaDB and Mailpit services; VibeMind owns the external n8n deployment and
   all of its volumes.
3. `EventBus` is publish-only. Only `SubscribableEventBus` exposes local
   callbacks, exactly as defined in
   `docs/superpowers/specs/2026-07-16-event-bus-capability-segregation-design.md`.
4. The program produces one canonical `.github/workflows/ci.yml`; no competing
   `integration.yml` is introduced.
5. `/healthz` is the gateway's only unauthenticated, non-sensitive readiness
   route. Every other production read or write requires a role-scoped token.
6. Shared documentation, CI, plan checkboxes, and this ledger have one owner:
   the orchestrator.
7. P02 and P08 intentionally supersede the earlier system-design statement
   that production authentication was out of scope. Minibook admin routes fail
   closed, and gateway routes other than `/healthz` require role-scoped tokens.
8. Ordinary pytest runs exclude the `live` marker. A packet may select
   `-m live` only when its allowlist names the target service and its cleanup
   contract; P20 owns the consolidated explicit live gates.

## Plan authority and supersession

| Area | Canonical source | Absorbed or blocked work |
|---|---|---|
| Reproducible environment, Windows lifecycle, health, runtime modularity, final acceptance | `2026-07-16-system-gap-remediation.md` | Owns shared integration, event-bus, architecture, and final-doc gates |
| Gateway authority, append-only lifecycle, HTTP clients, auth, SQLite retirement | `2026-07-16-mariadb-gateway-source-of-truth.md` | Replaces Captain Task 3 and the production-storage assumptions in broad Tasks 2-4 |
| Captain policy, resilience, resume, root planning CLI | `2026-07-16-captain-gap-remediation.md` Tasks 2, 4, 5, and CLI-only part of Task 8 | Captain Tasks 1, 3, 6, 7, and shared-doc portions are absorbed by program packets |
| Minibook authentication | `2026-07-16-gap-remediation.md` Task 1 | Dispatchable after the environment baseline |
| Codex, recovery, evidence control, Hermes learning | D01-D05 gateway-native delivery lane | Broad Tasks 5-8 are blocked until D01 replaces their storage assumptions; broad Tasks 2-3 must never be implemented as written |

The more recent gateway source-of-truth decision wins wherever an older plan
describes SQLite as an authoritative production command log or control plane.

## Dependency DAG

```text
P00 Baseline -> P01 Dev environment
                    |-> P02 Minibook auth
                    |-> P03 DB harness -> P03A Scalable gate
                                           -> P07A Projection -> P07B Event store
                                           -> P07C Replay -> P08 Gateway auth
                    |-> P04 Checkpoint/repair -> P05 Bootstrap -> P06 Preflight
                    |-> P09 Captain policy -> P10 LLM resilience
                    `-> P15 Event bus -+-> P16 URL adapter --+
                                       `-> P17 Recorder ------+-> P18 Pipeline

P08 + P10 -> P11 Gateway clients -+-> P12 Captain resume -> P19 Root CLI
                                   `-> P13 SQLite retirement
P06 + P08 -> P14 System health

P13 -> D01 Delivery design -> D02 Codex -> D03 Recovery -> D04 Evidence -> D05 Hermes

P02 + P12 + P13 + P14 + P18 + P19 + D05 -> P20 Live gate/CI -> P21 Docs/clean clone
```

P01 is the first implementation packet and must merge before any other packet
is dispatched. After P01, at most three independent external worker sessions
may run concurrently. This orchestrator uses one implementer at a time and a
fresh reviewer after every packet.

## Agent Factory v2 completion board

These checks are the terminal product definition. Packet completion is only
enabling evidence; no item below may be checked from mocks, skipped live tests,
uncommitted local files, or an offline-only demo.

- [ ] **SC01 — Versioned source input:** the concrete lead-processing use case
  and exact interfaces are committed and reproducibly hashed.
- [x] **SC02 — Canonical immutable DAG:** every batch has exactly one derived
  status and deterministic dependency order in integrated code.
- [ ] **SC03 — Two real n8n tools:** Hermes/Codex builds, imports, publishes,
  and invokes at least two target workflows against the authorized instance.
- [ ] **SC04 — Mixed AutoGen team:** at least one real AutoGen team uses both
  n8n tools through typed adapters.
- [ ] **SC05 — Process separation:** planning, n8n integration, AutoGen
  reasoning, execution, and independent review remain separate authorities.
- [ ] **SC06 — End-to-end trace chain:** `run_id`, `trace_id`, `batch_id`,
  `worker_id`, real Codex session/thread ID, and artifact version survive every
  gateway, worker, adapter, review, and Minibook boundary.
- [ ] **SC07 — Golden/holdout isolation:** holdouts never enter worker prompts,
  argv, environment, Codex logs, or MCP context before artifact sealing.
- [ ] **SC08 — Artifact contracts:** each produced artifact is independently
  validated before dependent batches are released.
- [ ] **SC09 — Failure recovery:** happy path plus at least three distinct
  failure paths prove repair, infrastructure retry, and terminal escalation.
- [ ] **SC10 — Three clean E2E runs:** three consecutive isolated executions
  pass without manual state repair or stale artifacts.
- [ ] **SC11 — Release projection:** only release-gate-passed artifacts appear
  as validated in Minibook; Minibook never becomes command authority.
- [ ] **SC12 — Clean reproduction:** a clean Windows/Python 3.11 environment
  can install, configure, run, and verify the v2 path from committed sources.
- [ ] **SC13 — Judge demo:** the verified live path is demonstrable in under
  three minutes without substituting offline evidence for required services.

Live gates remain independently blocking:

- [ ] **Gate A — n8n:** two real tools, primary MCP plus bounded REST fallback,
  published once each and invoked twice with Mailpit/sink evidence.
- [ ] **Gate B — Hermes/Codex:** claim, heartbeat, isolated workspace, real
  session capture, static/secret gates, same-session repair, and completion.
- [ ] **Gate C — AutoGen:** real team construction and typed use of both n8n
  tools with complete correlation metadata.
- [ ] **Gate D — Fleet/release:** unattended multi-worker execution, fencing,
  recovery, release gate, and read-only Minibook projection.
- [ ] **Gate E — acceptance:** three E2E runs, failure suite, evidence archive,
  clean-clone reproduction, and judge sandbox.

Current strict audit: `1/13` success criteria and `0/5` live gates are complete.
SC02 is integrated with deterministic contract evidence. SC07 has integrated
planning-policy evidence but still lacks the required Hermes/Codex sandbox
proof. This counter must be recomputed after every integrated packet and may
advance only from fresh evidence.

## Exclusive file locks

| Lock | Owned paths | Required order |
|---|---|---|
| `LOCK_PROGRAM` | This master plan, source-plan checkboxes, `docs/superpowers/IMPLEMENTATION_ACK.md`, `docs/superpowers/prompts/**`, `.superpowers/sdd/` reports | Orchestrator only |
| `LOCK_ENV_CI` | `requirements*.txt`, `pytest.ini`, `.github/workflows/**`, CI contract tests | P01 -> P20 -> P21 |
| `LOCK_LIFECYCLE` | `scripts/setup/**`, `setup.ps1`, `repair.ps1`, `status.ps1`, setup acceptance tests | P04 -> P05 -> P06 -> P14 |
| `LOCK_GATEWAY` | `gateway/**`, gateway/storage tests, isolated DB harness | P03 -> P03A -> P07A -> P07B -> P07C -> P08 -> P14 -> P20 |
| `LOCK_PLANNING` | `agenten/planning/**`, `agenten/llm/**`, planning/LLM tests, `main.py` | P09 -> P10 -> P11 -> P12 -> P19 |
| `LOCK_RUNTIME_CORE` | Runtime buses, pipeline, shared runtime/E2E tests | P15 -> P18 |
| `LOCK_ADAPTER_FITNESS` | URL adapter, `blockchain/web_scamler.py`, architecture/import tests | P15 -> P16 -> P18 |
| `LOCK_RECORDER` | Recorder facade/modules and ledger-bridge tests | P15 -> P17 -> P18 |
| `LOCK_DELIVERY` | `agenten/delivery/**`, delivery migrations and tests | P11 -> P13 -> D02 -> D03 -> D04 -> D05 |
| `LOCK_DELIVERY_DESIGN` | Only `docs/superpowers/specs/2026-07-16-gateway-native-delivery-runtime-design.md` and `docs/superpowers/plans/2026-07-16-gateway-native-delivery-runtime.md` | D01 only |
| `LOCK_MINIBOOK` | `minibook/src/**`, Minibook configuration and tests | P02 -> D05 if projections require Minibook changes |
| `LOCK_SHARED_DOCS` | `README.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/WORKSTREAMS.md`, `docs/DEMO.md`, `docs/DEVPOST_CHECKLIST.md`, submission verifier | P00/P01 narrowly, then P21 only |

Two active packets may not hold the same lock. A narrower exception is allowed
only when the orchestrator records exact non-overlapping paths and dispatch
SHAs before either worker begins.

## Executable session packets

### Foundation and independent Wave 1

- [x] **P00 — Integrate and freeze the program baseline**
  - Branch: `feat/system-remediation-orchestration`
  - Source: all four plans, both approved specs, and Gateway Task 1.
  - Output: this authority table, DAG, lock table, dispatch protocol, and a
    single baseline SHA. Declare the MariaDB gateway truth in
    `docs/WORKSTREAMS.md` and enforce exclusive `MariaDBStorage` references
    with the shared AST architecture helper.
  - Owns: the four source plans, this master plan, `docs/WORKSTREAMS.md`, the
    architecture TODO, `tests/architecture_fitness.py`,
    `tests/test_architecture_fitness.py`, and `tests/test_workstream_docs.py`.
  - Gate: `git diff --check`, plan presence, architecture/workstream tests, and
    a clean worktree.

- [x] **P01 — Reproducible development environment**
  - Branch: `build/reproducible-test-env`
  - Source: System Task 0.
  - Owns: `requirements-dev.txt`, `requirements.txt`, `pytest.ini`,
    `tests/test_import_boundaries.py`, and the development commands in
    `AGENTS.md`.
  - Absorbs Captain Task 8's duplicate-runtime-dependency cleanup. Warning
    compatibility checks are deferred to P20; architecture prose is P21-owned.
  - Gate: disposable Python 3.11 venv, `pip check`, full pytest, explicit skip
    report, and coverage floor 70%.

- [ ] **P02 — Fail-closed Minibook admin authentication**
  - Branch: `fix/minibook-admin-auth`; source: Broad Task 1; requires P01.
  - Owns only `minibook/src/main.py`, `minibook/config.example.yaml`, and
    `minibook/tests/test_admin_auth.py`.
  - Gate: Minibook authentication suite and root regression suite.

- [x] **P03 — Isolated MariaDB/gateway test harness**
  - Branch: `test/isolated-mariadb-gateway`; source: System Task 6 steps 1-3;
    requires P01.
  - Owns exactly: `docker-compose.test.yml`, `scripts/test_gateway.ps1`,
    `tests/support/__init__.py`, `tests/support/mariadb.py`,
    `tests/test_mariadb_test_guard.py`, `tests/gateway/test_gateway.py`, and
    `tests/blockchain/test_mariadb_storage.py`.
  - Explicitly excludes production Compose, `.env*`, `pytest.ini`, workflows,
    gateway production code, README, shared docs, and plans.
  - Output: disposable `captain_test`, temporary credentials, no production
    volume, and zero database-test skips.
  - Gate: `pwsh -NoProfile -File scripts/test_gateway.ps1`. Its focused 22-test
    database/gateway run uses `--no-cov` and permits zero skips. It then runs
    the full configured non-live coverage suite (`-m "not live"`), rejects
    every database/gateway skip, and permits only an explicit allowlist of
    known non-database compatibility or degradation skips; every new or unknown
    skip fails the gate.

- [x] **P03A — Make the MariaDB/gateway gate extensible**
  - Branch: `fix/scalable-gateway-test-count`; requires P03.
  - Owns exactly: `scripts/test_gateway.ps1` and
    `tests/test_mariadb_test_guard.py`.
  - Output: parse the selected pytest summary as an integer, require at least
    the established 22-test baseline, permit later gateway-test growth, and
    continue to reject every selected skip or missing/malformed summary. The
    full-suite skip allowlist and all Compose safety behavior remain unchanged.
  - Gate: focused parser regression tests followed by
    `pwsh -NoProfile -File scripts/test_gateway.ps1`.

- [x] **P04 — Checkpoint revalidation and targeted repair**
  - Branch: `fix/setup-checkpoint-repair`; source: System Tasks 1-2; requires
    P01.
  - Output: downstream-only invalidation and `Repair-CaptainSystem`.
  - Gate: full Pester setup suite.

- [x] **P09 — Deterministic Captain planning policy**
  - Branch: `feat/captain-planning-policy`; source: Captain Task 2; requires
    P01.
  - Output: canonical capabilities and isolated content fingerprints.
  - Gate: planning policy, Captain pipeline, and factory E2E tests.

- [x] **P15 — Event-bus capability segregation**
  - Branch: `refactor/event-bus-capabilities`; source: System Task 7; requires
    P01; replaces Captain Task 6 and Broad Task 9.
  - Output: publish-only `EventBus`, `SubscribableEventBus`, explicit recorder
    subscription, and fail-fast composition.
  - Gate: runtime, AutoGen, recorder, pipeline, E2E, architecture tests, and
    `compileall`.

### Sequential setup, gateway, planning, and runtime lanes

- [x] **P05 — Safe repository and external-n8n bootstrap**
  - Branch: `feat/setup-external-bootstrap`; source: System Task 3; requires
  P04; gate: Pester and both Compose configurations.
  - Submodule acceptance uses an explicit absent-then-present probe for the
    single allowed Git call and a separate already-present fast path that
    proves zero Git calls.
  - Injected repository probes, command results, and Hermes stage results are
    strict scalar contracts. Null, string, collection, or multi-output shapes
    fail closed before Git or installer actions.

- [ ] **P06 — Aggregate Windows preflight**
  - Branch: `fix/setup-preflight-contract`; source: System Task 4; requires
    P05; gate: Pester with version, port, and restart-required cases.

- [x] **P07A — Pure append-only lifecycle projection contracts**
  - Branch: `feat/gateway-event-contracts`; source: Gateway Task 2 Step 3;
    requires P03A.
  - Owns exactly: `gateway/contracts.py` and the gateway projection test module.
    It initially landed as `tests/gateway/test_contracts.py`; P07B performs the
    canonical no-content rename to `tests/gateway/test_gateway_contracts.py`
    after the full gate exposed a top-level pytest basename collision.
  - Output: deterministic event projection with an injectable clock,
    `pending_review` approval, lazy expired-claim recovery, current-iteration
    fencing/evidence state, all existing and delivery-native terminal states,
    and ordering/batch-id validation. No database or FastAPI dependency.
  - Gate: `python -m pytest -q --no-cov tests/gateway/test_contracts.py`.

- [x] **P07B — Persist gateway lifecycle as append-only child events**
  - Branch: `refactor/gateway-append-only-store`; source: Gateway Task 2
    Steps 1, 2, 4, and 5; requires P07A.
  - Owns exactly: `gateway/store.py`, `gateway/app.py`,
    `tests/gateway/test_gateway.py`, and
    `tests/blockchain/test_mariadb_storage.py`, plus the exact test-only rename
    `tests/gateway/test_contracts.py` ->
    `tests/gateway/test_gateway_contracts.py`.
  - Output: extract `GatewayStore` into the MariaDB adapter module so
    `gateway/app.py` remains routing/composition only. There is no post-insert
    mutation of work-batch `status`, `metadata`, or `children`; every
    lifecycle read and fence uses the P07A projection.
    `batch_done:succeeded` requires a preceding `validation_run` event; D04
    remains responsible for proving the evidence payload means all holdouts
    are green.
  - Gate: `pwsh -NoProfile -File scripts/test_gateway.ps1` with zero selected
    skips and no exact-count ceiling.
  - Quality hardening required before integration:
    - [x] concurrent `work_batch` creation yields exactly one immutable root;
    - [x] parallel writes preserve immediate `previous_hash` adjacency;
    - [x] affected writers share one lock order and bounded transaction retry;
    - [x] generic `/blocks` rejects gateway-owned claim, heartbeat, and
      approval event types;
    - [x] invalid initial work-batch status is rejected before persistence;
    - [x] a second holdout cannot replace the effective immutable holdout.

- [x] **P07C — Make Captain batch and holdout release idempotent**
  - Branch: `feat/gateway-idempotent-release`; source: Captain Task 3 Step 4;
    requires P07B.
  - Owns exactly: `gateway/store.py` and `tests/gateway/test_gateway.py`.
  - Output: identical canonical `work_batch` and its one `holdout` replay
    return the existing immutable block; different content, parent mismatch,
    or a second distinct holdout remains `409`.
  - Gate: `pwsh -NoProfile -File scripts/test_gateway.ps1` with zero selected
    skips.

- [x] **P08 — Gateway authentication and settings**
  - Branch: `feat/gateway-auth-settings`; source: Gateway Task 5 steps 1-3;
    requires P07C.
  - Gate: auth, settings, gateway, and database-backed health tests.

- [x] **P10 — Typed LLM resilience**
  - Branch: `feat/captain-llm-resilience`; source: Captain Task 5; requires
    P09; gate: LLM and factory E2E suites.

- [ ] **P11 — Authenticated gateway HTTP clients**
  - Branch: `feat/gateway-http-clients`; source: Gateway Task 3; requires P08
    and P10; replaces Captain Task 3.
  - Output: planning and delivery clients, JSON default for offline mode, and
    no direct production database access.

- [ ] **P12 — Atomic Captain run resume**
  - Branch: `feat/captain-run-resume`; source: Captain Task 4; requires P11.
  - Gate: run-store, crash/resume, and pipeline suites.

- [ ] **P13 — SQLite legacy import and production retirement**
  - Branch: `refactor/sqlite-legacy-import`; source: Gateway Task 4; requires
    P11.
  - Gate: dry-run, idempotent replay, legacy-ledger, and boundary tests.

- [ ] **P14 — Unified system health contract**
  - Branch: `feat/system-health-contract`; source: System Task 5 plus the
    gateway service portion of Gateway Task 5; requires P06 and P08.
  - Output: ten-component health with gateway as a Captain-managed process.
    Replace P04's transitional private Minibook/service validation adapters
    with calls into `Health.psm1`; no duplicate path, HTTP, TCP, or aggregate
    service-health implementation may remain in `StageValidation.psm1`.
  - [ ] Move gateway schema initialization to explicit process startup so
    `/healthz` remains a read-only `SELECT 1` probe and never performs lazy
    DDL or migration work on the first readiness request.

- [ ] **P16 — URL relevance adapter boundary**
  - Branch: `refactor/url-relevance-adapter`; source: System Task 8; requires
    P15; gate: adapter, architecture, and import tests.

- [ ] **P17 — Recorder modularization behind the public facade**
  - Branch: `refactor/ledger-recorder-modules`; source: System Task 9; requires
    P15; gate: full ledger-bridge, AutoGen, import, and compile suites.

- [ ] **P18 — Pipeline composition split**
  - Branch: `refactor/pipeline-composition`; source: System Task 10; requires
    P16 and P17; preserves `build_pipeline`.

- [ ] **P19 — Root planning CLI delegation**
  - Branch: `feat/root-plan-cli`; source: CLI-only portion of Captain Task 8;
    requires P12; owns `main.py` and its delegation test only.

### Program gates

- [ ] **P20 — Unified live evidence and CI gate**
  - Branch: `ci/remediation-gates`; absorbs System Task 6 steps 4-6, Captain
    Task 7, Gateway Task 6, and Broad Task 4.
  - Requires P02, P12, P13, P14, P18, P19, and D05.
  - Output: one `.github/workflows/ci.yml`, mandatory unit/architecture/
    MariaDB/Pester/submission jobs, a real Captain-gateway verifier, and tracked
    checks for the `httpx`/Starlette and `requests` dependency warnings without
    globally silencing them.
  - Every external-service test is selected explicitly with `-m live` in its
    owning job; the ordinary unit/coverage job remains non-live.
  - [ ] Pin the supported OpenAI SDK version and add a reviewed rate-limit
    policy for bounded backoff/`Retry-After`; document that the current
    in-process `asyncio.wait_for` timeout requires cooperative cancellation
    and is a soft deadline rather than a process-level kill boundary.
  - Gate: isolated DB script, live verifier, Pester, full pytest, `compileall`,
    Compose validation, and submission verification.

- [ ] **P21 — Truthful docs and clean-clone acceptance**
  - Branch: `docs/verified-remediation-handoff`; absorbs System Task 11 and all
    remaining shared-document tasks; requires every prior packet.
  - This is the only final shared-doc session.
  - Gate: all static/live gates and a clean-clone setup/start/status/repair/
    smoke run with a consolidated evidence table.

## Blocked design lane

- [ ] **D01 — Design a gateway-native delivery runtime**
  - Branch: `docs/gateway-native-delivery-runtime`; requires P13; holds only
    `LOCK_DELIVERY_DESIGN`.
  - Owns exactly
    `docs/superpowers/specs/2026-07-16-gateway-native-delivery-runtime-design.md`
    and
    `docs/superpowers/plans/2026-07-16-gateway-native-delivery-runtime.md`.
  - Replace Broad Tasks 2-8 with a reviewed spec and implementation plan for a durable
    gateway outbox or cursor, Codex process events, reasoning-slice events,
    leases, recovery, evidence events, and Minibook/Hermes projections.
  - The design must not reintroduce an authoritative SQLite production path.
  - Gate: architecture/spec review, exact file maps for D02-D05, and executable
    red/green acceptance commands. D02-D05 remain non-dispatchable until this
    review passes.

- [ ] **D02 — Supervised Codex and Second Brain adapter**
  - Branch: assigned by D01; revised source: Broad Task 5; requires D01 and P11.
  - Preserve argument-array process supervision, workspace guards, resume, and
    cancellation while persisting process metadata through gateway events.
  - Gate and exact allowlist: defined by approved D01.

- [ ] **D03 — Gateway-native reasoning slices and crash recovery**
  - Branch: assigned by D01; revised source: Broad Task 6; requires D02.
  - Replace SQLite state transitions with append-only gateway events, leases,
    reaping, and Windows resume behavior.
  - Gate and exact allowlist: defined by approved D01.

- [ ] **D04 — Real-evidence iteration controller**
  - Branch: assigned by D01; revised source: Broad Task 7; requires D03.
  - Persist typed evidence and independent review decisions through the gateway;
    retain the five-red escalation rule and fail-closed live evidence.
  - Gate and exact allowlist: defined by approved D01.

- [ ] **D05 — Hermes workers and selective learning projections**
  - Branch: assigned by D01; revised source: Broad Task 8; requires D04 and P02.
  - Provision workers idempotently and project gateway events to Hermes/
    Minibook without creating a second source of truth.
  - Gate and exact allowlist: defined by approved D01.

D01-D05 are mandatory program work, not optional backlog. P20 and P21 remain
blocked until every D-lane checkbox is closed with reviewed evidence.

## Dispatch and integration protocol

For every packet, the orchestrator must:

1. Record the integration SHA, packet ID, branch, worktree, exact allowed
   paths, acquired locks, prerequisites, and verification commands in
   `.superpowers/sdd/progress.md` before dispatch.
2. Create the worker branch and worktree from that exact integration SHA.
   Workers must not merge, rebase, push, edit shared plans/docs, or update this
   master file. D01 is the sole exception and may create only its two exact
   `LOCK_DELIVERY_DESIGN` files; it still may not edit existing plans or docs.
3. Require RED -> GREEN -> REFACTOR, a narrow Conventional Commit, and a worker
   report containing commit SHA, changed paths, commands, pass/fail/skip counts,
   and open risks.
4. Reject any candidate with paths outside its allowlist, secrets, an altered
   `hermes-agent` gitlink, unexpected merge commits, non-conventional commit
   subjects, or `git diff --check` failures.
5. Compare `dispatch_sha..candidate` with `dispatch_sha..integration`. If any
   path overlaps, stop and redispatch from the new integration tip. Otherwise
   preview with `git merge-tree` before integration.
6. Run a fresh specification review and then a fresh code-quality review. The
   implementer fixes findings; the same reviewer confirms each fix.
7. Integrate only after both reviews and all packet gates pass on the
   orchestrator branch. Then immediately update this checkbox, the source-plan
   checkbox(es), Session Insights, and the local progress ledger.

## Program stop gates

- Never dispatch a packet whose prerequisite checkbox is open.
- Never run two sessions that hold the same lock.
- Never treat a skipped required database/gateway test as passing evidence.
- Never start, stop, adopt, migrate, or delete VibeMind n8n resources.
- Never use the dirty root worktree or stale `origin/main` as a dispatch base.
- Stop and redesign if code requires direct production SQLite writes, direct
  MariaDB access outside `gateway/`, a second CI workflow, or an unauthenticated
  sensitive gateway route.

## Session insights

- The four original plans are individually coherent but not jointly
  dispatchable; exact path ownership is stricter than task-title similarity.
- `Lifecycle.psm1`, `Setup.Tests.ps1`, `gateway/app.py`, `factory.py`,
  `pipeline.py`, `pytest.ini`, and shared docs are the dominant merge hot spots.
- Plan branches and `main` were conflict-free at integration time because their
  post-base paths were disjoint; semantic conflicts still required this
  supersession layer.
- P00 used RED/GREEN architecture tests to enforce the gateway sole-writer
  boundary. The symbol scan covers direct, qualified, and aliased references
  while excluding foreign worktrees, virtual environments, the Hermes
  submodule, and the adjacent Minibook product.
- P00 baseline evidence: 219 tests passed and 23 service-dependent tests
  skipped. P03 owns the zero-skip MariaDB/gateway proof; P20 owns the tracked
  Starlette/`httpx` and `requests` compatibility warnings.
- P01 clean-environment evidence: Python 3.11.0 installed both manifests,
  `pip check` reported no broken requirements, 217 tests passed, 24 were
  explicitly reported as service/compatibility skips, one Starlette warning
  remained visible, and measured coverage was 74.76% against the 70% floor.
  Specification and code-quality reviews both passed before integration.
- A post-P01 audit found that the prior bare full-suite command also collected
  a Minibook `live` test and wrote test records to a reachable local service.
  No deletion was attempted. Default pytest now excludes `live`; P20 must
  select each authorized live system explicitly and prove cleanup/isolation.
- P03 integrated candidate `45ea024` after disjoint-path and conflict checks,
  specification PASS, and code-quality PASS. The gate on integration commit
  `d400d9e` executed all 22 MariaDB/gateway tests with zero skips; the complete
  non-live suite passed 261 tests with two explicitly allowed degradation
  skips, one live deselection, one pre-existing Starlette warning, and 80.54%
  coverage. The `captain-cook-test` resources were removed, while Captain's
  MariaDB/Mailpit and external VibeMind n8n retained their start times.
- The P07 pre-dispatch audit found that P03's exact `22 passed` equality would
  reject every legitimate new gateway test. It also found that the source
  projection omitted the existing `pending_review` approval state and the
  delivery design's validation ordering and terminal outcomes. P03A removes
  the brittle ceiling; P07A-P07C split pure projection, event persistence, and
  idempotent release behind one serial gateway lock. P07B extracts
  `GatewayStore` into `gateway/store.py`, preventing lifecycle persistence from
  remaining coupled to FastAPI routing.
- P03A integrated candidate `854c7e1` as merge commit `ed7c977` after a
  disjoint-path audit, conflict-free `merge-tree` preview, specification PASS,
  and code-quality PASS. The integrated gate accepted the established 22-test
  minimum with zero selected skips, then passed 262 non-live tests with two
  explicitly allowed degradation skips, one live deselection, one pre-existing
  Starlette warning, and 80.54% coverage. Its disposable Compose resources
  were removed; Captain MariaDB/Mailpit, the observed Captain n8n container,
  and external VibeMind n8n remained healthy with unchanged start times. P07A
  is therefore unlocked on the serial gateway lane.
- P07A integrated fixed candidate `46dfc80` as merge commit `e951549` after a
  disjoint-path audit, specification PASS, and quality PASS. The first quality
  review correctly rejected coercible boolean/string evidence iterations and
  two loose parent-reference cases; five RED regressions then proved the fixes,
  and the same reviewer confirmed them. The integrated projection/architecture
  gate passed 59 tests and `compileall` succeeded without database, Docker, or
  network access. P07B is now unlocked on `LOCK_GATEWAY`.
- P04 integrated fixed candidate `1f09a30` as merge commit `4652fb8` after
  specification PASS and quality PASS. Quality review first exposed a hidden
  `StageValidation -> Lifecycle` reverse dependency and an unstable null-runner
  path; four RED/green increments replaced the dependency with an injected,
  fail-closed status provider and normalized failed repair results. The
  integrated gate passed 50 Pester tests with zero skips plus 14 architecture/
  import/workstream tests. P05 is unlocked; P14 retains the explicit TODO to
  replace P04's private compatibility probes with the unified health contract.
- P15 integrated candidate `e688177` as merge commit `61b1858` after exact
  allowlist and merge-tree audits, specification PASS, and quality PASS. Its
  integrated gate passed 54 tests with zero skips and `compileall` passed.
  `EventBus` is now publish-only, local callbacks require
  `SubscribableEventBus`, recorder wiring is explicit, and publish-only buses
  fail before partial pipeline construction. P16 and P17 are unlocked in their
  respective serial lock lanes.
- P09 integrated candidate `51cfb69` together with AF01 as merge commit
  `df79012` after explicit four-path conflict resolution. Four review cycles
  closed partial publication, resolver-list mutation, and release-boundary
  aliasing. The combined gate passed 123 tests and `compileall`; the diagnostic
  full suite passed 349 tests with 23 explicit skips, one live deselection, one
  known warning, and 78.12% coverage. P10 is unlocked.
- P05 pre-dispatch review found a contradictory source example that supplied an
  always-true Hermes probe while expecting a Git call. The source now proves
  the absent-to-present initialization path separately from the already-present
  zero-mutation path, matching the packet brief and repository-safety contract.
- P07B's real selected gate passed 36 MariaDB/gateway tests, but its first full
  gate exposed that `tests/gateway/test_contracts.py` and
  `tests/validation/test_contracts.py` import as the same top-level module.
  P07B owns only a no-content rename of the newer gateway file to
  `test_gateway_contracts.py`; the gate and global collection semantics remain
  unchanged, so collection cannot be hidden by a command-line workaround.
- P07B integrated candidate `6acee13` as merge commit `848c83f` after a
  disjoint-path audit, conflict-free merge-tree preview, specification PASS,
  and quality PASS. The integrated disposable-MariaDB gate passed 46 selected
  tests with zero skips; its full gate passed 391 tests with two explicit
  skips, one live deselection, one known warning, and 81.79% coverage. The
  no-content test rename restored default pytest collection, all disposable
  test containers were removed, and P07C is now unlocked on `LOCK_GATEWAY`.
- P07C integrated candidate `2f66ae3` as merge commit `c277c3b` after
  specification PASS and quality PASS with no Critical/Important findings.
  RED evidence covered canonical batch and holdout replays plus concurrent
  duplicate release. The integrated disposable-MariaDB gate passed 49 selected
  tests with zero skips; its full gate passed 394 tests with two explicit
  skips, one live deselection, one known warning, and 83% coverage. Replays
  return the immutable stored block without consuming an index or breaking
  hash adjacency. P08 is now unlocked on `LOCK_GATEWAY`.
- P08 integrated candidate chain `e5449d1` + `1e6defb` as merge commit
  `cfa4123` after the first specification review rejected a remotely
  overridable bind host and unauthenticated slash redirects. RED repairs then
  fixed the host to `127.0.0.1` and disabled redirect slashes; fresh
  specification and quality reviews passed. The integrated disposable-MariaDB
  gate passed 49 selected tests with zero skips; its full gate passed 413 tests
  with two explicit skips, one live deselection, one known warning, and 84%
  coverage. P11 now waits only for P10; P14 also requires P06.
- P10 integrated candidate chain `dfd8983` + `4575d18` as merge commit
  `e8754ea`. Pre-dispatch review expanded the exact packet by
  `agenten/llm/model_client.py` and its test so P10 is the sole retry owner and
  the OpenAI SDK uses `max_retries=0`. The first spec/quality reviews rejected
  inaccurate mixed-failure attempt metadata and non-finite timeouts; RED
  repairs closed both and fresh reviews passed. The integrated LLM/planning
  gate passed 94 tests plus compileall; the default non-live suite passed 386
  with 59 explicit service/environment skips, one live deselection, one known
  warning, and 76.87% coverage. P11 is now unlocked.
