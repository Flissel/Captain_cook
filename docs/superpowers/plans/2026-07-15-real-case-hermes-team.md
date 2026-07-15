# Real-Case Hermes Team Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a crash-resumable Captain delivery loop that assigns durable TODOs to three Hermes identities, invokes real Codex CLI with Second Brain MCP, runs independent real-case tests, and retries at most five times.

**Architecture:** Add an append-only SQLite delivery ledger beside the existing supply-chain ledger so the new `planned → assigned → in_progress → testing → reviewing → passed|redo|escalated` contract does not destabilize the offline demo. A FastAPI control plane owns all state transitions; Hermes and Minibook receive projections. Codex, Second Brain, Minibook, and target systems are verified through real subprocess, MCP, and HTTP calls—never mocks or synthesized evidence.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite, AutoGen Core, Hermes Agent, Codex CLI, Minibook REST API, pytest

## Global Constraints

- No mock, fake, stub, simulated tool, synthetic success, fallback payload, or `"mock": true` evidence may satisfy a live acceptance gate.
- The existing deterministic Householder runtime remains an explicitly offline fallback and is not modified to claim live behavior.
- The delivery ledger is the source of truth; Minibook and Hermes TODOs are projections.
- Captain alone creates assignments and commits terminal transitions.
- Builder cannot approve its own work; Tester cannot approve its own evidence.
- A rejected fifth review transitions to `escalated`; no sixth build starts.
- State is committed before an event is emitted, and duplicate `event_id` values are idempotent.
- Secrets remain in existing gitignored `.env` or Hermes profile files and never enter prompts, posts, evidence, logs, fixtures, or commits.
- Every task uses a failing focused test first, then the smallest implementation, then focused and full regression verification.
- Live acceptance commands must fail closed when a required dependency is unavailable.

---

### Task 1: Durable Delivery TODO Contract

**Files:**
- Create: `agenten/delivery/__init__.py`
- Create: `agenten/delivery/models.py`
- Create: `agenten/delivery/state_machine.py`
- Create: `agenten/delivery/ledger.py`
- Create: `tests/delivery/test_delivery_ledger.py`

**Interfaces:**
- Produces: `DeliveryTodo`, `DeliveryEvent`, `DeliveryStatus`, `DeliveryRole`, `DeliveryEvidence`, `SqliteDeliveryLedger`
- Produces: `create_todo()`, `get_todo()`, `list_todos()`, `transition()`, `append_evidence()`, and `events_after()`
- Consumes: filesystem path to a real SQLite database

- [x] **Step 1: Write contract tests for durable creation and reload**

```python
def test_sqlite_ledger_persists_todo_across_instances(tmp_path):
    path = tmp_path / "delivery.db"
    first = SqliteDeliveryLedger(path)
    todo = first.create_todo(
        project_id="captain-cook",
        title="Build real webhook",
        description="Deploy and execute the webhook",
        acceptance_criteria=("HTTP 200", "correlated Mailpit message"),
    )
    second = SqliteDeliveryLedger(path)
    loaded = second.get_todo(todo.todo_id)
    assert loaded == todo
    assert loaded.status is DeliveryStatus.PLANNED
    assert loaded.iteration == 1
    assert loaded.max_iterations == 5
```

- [x] **Step 2: Run the test and verify the contract is absent**

Run: `python -m pytest tests/delivery/test_delivery_ledger.py::test_sqlite_ledger_persists_todo_across_instances -v`

Expected: FAIL because `agenten.delivery` does not exist.

- [x] **Step 3: Implement frozen Pydantic models and SQLite schema**

Create `DeliveryRole` values `architect_builder`, `real_case_tester`, and `quality_warden`. Create `DeliveryStatus` values from the approved design. Store one append-only `delivery_events` row per accepted command and one transactionally updated `delivery_todos` projection row. Use `sqlite3`, WAL mode, foreign keys, explicit transactions, JSON arrays, UTC timestamps, and a unique index on `event_id`.

- [x] **Step 4: Add legal-transition and ownership tests**

```python
def test_only_expected_role_can_advance_working_state(real_ledger, planned_todo):
    assigned = real_ledger.transition(
        planned_todo.todo_id,
        event_id="assign-1",
        expected_version=planned_todo.version,
        actor="captain",
        target=DeliveryStatus.ASSIGNED,
        assignee=DeliveryRole.ARCHITECT_BUILDER,
    )
    with pytest.raises(DeliveryTransitionError, match="assigned role"):
        real_ledger.transition(
            assigned.todo_id,
            event_id="start-1",
            expected_version=assigned.version,
            actor=DeliveryRole.REAL_CASE_TESTER.value,
            target=DeliveryStatus.IN_PROGRESS,
        )

def test_duplicate_event_is_idempotent(real_ledger, assigned_todo):
    first = real_ledger.transition(
        assigned_todo.todo_id,
        event_id="same-event",
        expected_version=assigned_todo.version,
        actor=DeliveryRole.ARCHITECT_BUILDER.value,
        target=DeliveryStatus.IN_PROGRESS,
    )
    second = real_ledger.transition(
        assigned_todo.todo_id,
        event_id="same-event",
        expected_version=assigned_todo.version,
        actor=DeliveryRole.ARCHITECT_BUILDER.value,
        target=DeliveryStatus.IN_PROGRESS,
    )
    assert second == first
```

- [x] **Step 5: Implement optimistic transitions and five-iteration enforcement**

Legal flow is `planned → assigned → in_progress → testing → reviewing → passed`, with `reviewing → redo → in_progress`. `redo` increments once. A rejected review at iteration five becomes `escalated`. Reject stale versions, unauthorized actors, changed acceptance criteria, builder-set `passed`, and any transition out of terminal state.

- [x] **Step 6: Run focused and regression tests**

Run: `python -m pytest tests/delivery/test_delivery_ledger.py -v`

Run: `python -m pytest tests/ledger_bridge tests/test_householder_runtime.py -q`

Expected: PASS.

- [x] **Step 7: Commit the contract**

```powershell
git add agenten/delivery tests/delivery/test_delivery_ledger.py
git commit -m "feat: add durable delivery todo ledger"
```

---

### Task 2: Captain Delivery Control Plane

**Files:**
- Create: `agenten/delivery/api.py`
- Create: `agenten/delivery/service.py`
- Create: `agenten/delivery/events.py`
- Create: `tests/delivery/test_delivery_api.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `SqliteDeliveryLedger`
- Produces: HTTP endpoints for TODO commands, evidence, heartbeats, and event polling
- Produces: in-process `DeliveryEventPublisher` called only after commit

- [x] **Step 1: Write API lifecycle tests against a real temporary SQLite database**

Use FastAPI `TestClient` only as an HTTP transport around the real service and real SQLite file; do not replace the ledger or publisher with mocks. Prove create, assign, start, heartbeat, testing, reviewing, redo, and passed responses plus stale-version `409`.

- [x] **Step 2: Run the focused test and verify failure**

Run: `python -m pytest tests/delivery/test_delivery_api.py -v`

Expected: FAIL because `agenten.delivery.api` does not exist.

- [x] **Step 3: Implement the control-plane endpoints**

```text
POST /delivery/todos
GET  /delivery/todos/{todo_id}
GET  /delivery/todos?assignee=&status=
POST /delivery/todos/{todo_id}/assign
POST /delivery/todos/{todo_id}/transition
POST /delivery/todos/{todo_id}/heartbeat
POST /delivery/todos/{todo_id}/evidence
GET  /delivery/events?after=
```

Commands require `event_id`, `expected_version`, and `actor`. Heartbeats renew a ten-minute lease only for the current assignee. Return committed state before publishing a notification. Log identifiers, never prompt bodies or credentials.

- [x] **Step 4: Prove commit-before-publish and idempotency without a mock publisher**

Use a real `InMemoryEventBus` subscriber plus the real SQLite ledger. Subscriber reloads the TODO during delivery and asserts the committed version is already visible. Publish the same command twice and assert one event row.

- [x] **Step 5: Run focused and full tests**

Run: `python -m pytest tests/delivery/test_delivery_api.py tests/delivery/test_delivery_ledger.py -v`

Run: `python -m pytest -q`

Expected: delivery tests PASS; if a pre-existing unrelated failure remains, record it before proceeding and do not call the branch green.

- [ ] **Step 6: Commit the control plane**

```powershell
git add agenten/delivery/api.py agenten/delivery/service.py agenten/delivery/events.py tests/delivery/test_delivery_api.py requirements.txt
git commit -m "feat: expose captain delivery control plane"
```

---

### Task 3: Minibook Plan and Assignment Projection

**Files:**
- Create: `agenten/delivery/minibook_client.py`
- Create: `agenten/delivery/projector.py`
- Create: `scripts/post_delivery_plan.py`
- Create: `tests/live/test_minibook_projection_live.py`

**Interfaces:**
- Consumes: `MINIBOOK_URL`, Hermes API key from the Hermes profile, committed delivery events, and a plan Markdown path
- Produces: one Captain Cook Minibook project, one pinned plan post, assignment posts/comments, iteration updates, and terminal status updates

- [ ] **Step 1: Write a live Minibook preflight test**

The test calls `/health`, registers or resolves the three named Hermes identities, creates or resolves project `Captain Cook`, posts a uniquely correlated test plan, reads it back, comments with an assignment, and deletes or closes only artifacts bearing the test run ID. It skips nothing: unreachable Minibook is FAIL.

- [ ] **Step 2: Run preflight against the running local Minibook**

Run: `python -m pytest tests/live/test_minibook_projection_live.py -v -m live`

Expected before implementation: FAIL because the client/projector is absent.

- [ ] **Step 3: Implement idempotent HTTP client and projector**

Use `httpx` with explicit timeouts. Search before creating posts. Never place API keys, hidden holdout cases, raw model context, or complete failure logs in Minibook. Store Minibook IDs in delivery-event metadata so replay updates existing content rather than duplicating posts.

- [ ] **Step 4: Implement the plan-posting command**

```powershell
python scripts/post_delivery_plan.py `
  --plan docs/superpowers/plans/2026-07-15-real-case-hermes-team.md `
  --project "Captain Cook"
```

The command prints project and post IDs, not credentials. It exits nonzero if read-back differs from the file hash.

- [ ] **Step 5: Reconcile the already-posted implementation plan with the projector**

Run the command above against `http://127.0.0.1:3457`. It must find and update the plan post created before Task 1 rather than creating a duplicate. Read the post back through the API and record its URL/ID in the execution handoff.

- [ ] **Step 6: Run live projection and regression tests**

Run: `python -m pytest tests/live/test_minibook_projection_live.py -v -m live`

Run: `python -m pytest minibook/tests -q`

Expected: live projection PASS. Any existing Minibook role-join failure must be fixed or explicitly isolated before claiming the Minibook suite green.

- [ ] **Step 7: Commit Minibook integration**

```powershell
git add agenten/delivery/minibook_client.py agenten/delivery/projector.py scripts/post_delivery_plan.py tests/live/test_minibook_projection_live.py
git commit -m "feat: project delivery plans into minibook"
```

---

### Task 4: Real Codex CLI and Second Brain Tool

**Files:**
- Create: `agenten/tools/codex_cli.py`
- Create: `agenten/delivery/codex_runs.py`
- Create: `scripts/codex-session.ps1`
- Create: `tests/tools/test_codex_cli_contract.py`
- Create: `tests/live/test_codex_secondbrain_live.py`
- Modify: `docs/codex-sessions.md`

**Interfaces:**
- Consumes: TODO ID, workspace, plan path, immutable acceptance criteria, optional failure report, installed `codex` executable
- Produces: `CodexRun` and `CodexRunStatus`
- Executes: real `codex exec --json` and `codex exec resume`

- [ ] **Step 1: Write parser/guard tests from a captured real Codex JSONL sample**

The fixture must be captured from an actual benign `codex exec --json` invocation during this task and sanitized only for secrets and absolute username paths. Test session-ID extraction, exit status, changed-path boundaries, event-log location, and rejection of workspaces outside the authorized root.

- [ ] **Step 2: Run contract tests and verify failure**

Run: `python -m pytest tests/tools/test_codex_cli_contract.py -v`

Expected: FAIL because the adapter is absent.

- [ ] **Step 3: Implement supervised Codex process operations**

`start()`, `resume()`, `status()`, and `cancel()` execute Codex through argument arrays, never shell-concatenated prompts. Persist process ID, session ID, timestamps, branch, before/after commit, JSONL path, and exit code into the delivery ledger. Stream heartbeats while the process lives. Reject success when the JSONL stream lacks a real session ID.

- [ ] **Step 4: Implement the PowerShell evidence wrapper**

The wrapper launches `codex exec --json`, writes UTF-8 JSONL under a gitignored run directory, extracts the real thread/session ID, and updates `docs/codex-sessions.md` only when explicitly passed `-RecordSession`. It never echoes environment variables.

- [ ] **Step 5: Run a real Second Brain preflight**

The live test runs `codex mcp list`, requires enabled `secondbrain`, then runs a minimal real Codex task in a temporary authorized Git worktree instructing Codex to list the Second Brain MCP capability without mutating project files. The test requires a real Codex session ID and real MCP discovery event; absence is FAIL, not skip.

- [ ] **Step 6: Verify focused, live, and full tests**

Run: `python -m pytest tests/tools/test_codex_cli_contract.py -v`

Run: `python -m pytest tests/live/test_codex_secondbrain_live.py -v -m live`

Run: `python -m pytest -q`

Expected: PASS subject to separately documented pre-existing failures.

- [ ] **Step 7: Commit Codex integration**

```powershell
git add agenten/tools/codex_cli.py agenten/delivery/codex_runs.py scripts/codex-session.ps1 tests/tools/test_codex_cli_contract.py tests/live/test_codex_secondbrain_live.py docs/codex-sessions.md
git commit -m "feat: add supervised codex cli delivery tool"
```

---

### Task 5: Durable Reasoning Runs and Society-of-Mind Slices

**Files:**
- Create: `agenten/reasoning/__init__.py`
- Create: `agenten/reasoning/models.py`
- Create: `agenten/reasoning/store.py`
- Create: `agenten/reasoning/society.py`
- Create: `agenten/reasoning/api.py`
- Create: `tests/reasoning/test_reasoning_runs.py`
- Create: `tests/live/test_autogen_reasoning_live.py`

**Interfaces:**
- Produces: `/reasoning-runs` create/get/resume/heartbeat/cancel endpoints
- Persists: transcript summary, completed-turn cursor, next speaker, child process/session IDs, lease, and terminal reason
- Consumes: real AutoGen agents backed by configured OpenAI `gpt-5.6`

- [ ] **Step 1: Write persistence and safe-boundary tests**

Use a real SQLite store and real deterministic message objects, not mocked agents. Prove a completed turn checkpoint survives store reconstruction, a running tool call cannot be checkpointed as completed, stale versions fail, and a ten-turn/five-minute slice yields a resumable state.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/reasoning/test_reasoning_runs.py -v`

Expected: FAIL because the reasoning package is absent.

- [ ] **Step 3: Implement reasoning store and endpoint**

Persist every completed turn and tool boundary transactionally. The HTTP request schedules or resumes a run and returns its ID; it does not hold one unbounded request open. Apply defaults: five minutes, ten turns, fifteen-second heartbeat, ten-minute lease.

- [ ] **Step 4: Implement the three-agent AutoGen pattern**

Architect/Builder proposes actions, Tester challenges observability and real-case coverage, Quality Warden checks evidence policy. Termination occurs only on a persisted yield, passed decision, escalation, cancellation, turn budget, or time budget. Tool calls route through the delivery service and Codex adapter; agents cannot directly mutate authoritative state.

- [ ] **Step 5: Run a real short Society-of-Mind conversation**

The live test uses the configured OpenAI key and `gpt-5.6`, creates a read-only planning TODO, completes at least one turn per role, checkpoints, reconstructs the runtime, resumes, and reaches a persisted yield. It records token usage and run ID but no prompt secrets.

- [ ] **Step 6: Verify reasoning and regressions**

Run: `python -m pytest tests/reasoning/test_reasoning_runs.py -v`

Run: `python -m pytest tests/live/test_autogen_reasoning_live.py -v -m live`

Run: `python -m pytest -q`

- [ ] **Step 7: Commit reasoning runs**

```powershell
git add agenten/reasoning tests/reasoning tests/live/test_autogen_reasoning_live.py
git commit -m "feat: add durable society of mind reasoning runs"
```

---

### Task 6: Wake-Up, Lease Reaping, and Restart Recovery

**Files:**
- Create: `agenten/delivery/recovery.py`
- Create: `agenten/delivery/reaper.py`
- Create: `scripts/run_captain_delivery.py`
- Create: `scripts/install-captain-delivery-task.ps1`
- Create: `tests/delivery/test_delivery_recovery.py`
- Create: `tests/live/test_delivery_restart_live.py`

**Interfaces:**
- Consumes: delivery ledger and reasoning store
- Produces: expired-lease decisions, resumable-work commands, Windows scheduled-task launcher

- [ ] **Step 1: Write restart recovery tests for every non-terminal state**

Create real SQLite records for `planned`, `assigned`, `in_progress`, `testing`, `reviewing`, and `redo`. Reopen the stores in a new process. Assert unexpired leases are untouched, expired work receives one idempotent recovery event, testing/reviewing resume at their exact gate, and terminal work never moves.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/delivery/test_delivery_recovery.py -v`

- [ ] **Step 3: Implement recovery and reaper**

Recovery derives commands from durable state, never from buffered events. Reaper scans every fifteen seconds. Ambiguous child process or Codex state becomes operator-visible `escalated` unless a real status query resolves it.

- [ ] **Step 4: Implement Windows wake entry points**

`run_captain_delivery.py` starts API, event polling, reasoning scheduler, and reaper with clean shutdown. The installer creates an opt-in Windows scheduled task triggered at logon and configured to restart after failure. It prints the exact task definition and requests confirmation before installation.

- [ ] **Step 5: Execute a real crash/restart test**

Start Captain in a child process, create an active TODO and reasoning checkpoint, terminate the process, start a fresh process, and verify exactly one resumed action with the original TODO and Codex session IDs. No in-memory object from the first process may be reused.

- [ ] **Step 6: Verify recovery and regressions**

Run: `python -m pytest tests/delivery/test_delivery_recovery.py -v`

Run: `python -m pytest tests/live/test_delivery_restart_live.py -v -m live`

Run: `python -m pytest -q`

- [ ] **Step 7: Commit recovery**

```powershell
git add agenten/delivery/recovery.py agenten/delivery/reaper.py scripts/run_captain_delivery.py scripts/install-captain-delivery-task.ps1 tests/delivery/test_delivery_recovery.py tests/live/test_delivery_restart_live.py
git commit -m "feat: recover durable delivery work after restart"
```

---

### Task 7: Real-Case Evidence Gate and Five-Iteration Controller

**Files:**
- Create: `agenten/validation/__init__.py`
- Create: `agenten/validation/evidence.py`
- Create: `agenten/validation/real_case.py`
- Create: `agenten/delivery/controller.py`
- Create: `tests/validation/test_real_evidence_policy.py`
- Create: `tests/live/test_real_case_iteration_live.py`

**Interfaces:**
- Consumes: immutable acceptance criteria and typed live observations
- Produces: `RealCaseReport`, `QualityDecision`, structured failure report, `passed|redo|escalated` command

- [ ] **Step 1: Write fail-closed evidence policy tests**

```python
@pytest.mark.parametrize("provenance", ["mock", "fake", "stub", "simulated", "synthetic", "fallback"])
def test_non_real_provenance_is_rejected(provenance):
    evidence = DeliveryEvidence(
        evidence_id="ev-1",
        channel="http",
        provenance=provenance,
        observed_at=utc_now(),
        payload={"status": 200},
    )
    with pytest.raises(EvidencePolicyError):
        validate_real_evidence(evidence)

def test_mock_true_is_rejected_even_with_green_unit_tests():
    evidence = DeliveryEvidence(
        evidence_id="ev-2",
        channel="http",
        provenance="live",
        observed_at=utc_now(),
        payload={"status": 200, "mock": True},
    )
    with pytest.raises(EvidencePolicyError):
        validate_real_evidence(evidence)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/validation/test_real_evidence_policy.py -v`

- [ ] **Step 3: Implement typed evidence and Quality Warden gate**

Support HTTP, n8n execution, Mailpit message, Codex session, Second Brain MCP, process, and persistence channels. Each criterion declares required channel and correlation fields. Evidence is immutable and content-hashed. Missing dependency is an infrastructure failure, never a pass.

- [ ] **Step 4: Implement the iteration controller**

Controller moves assigned work through builder, focused/full tests, real-case tester, and Quality Warden. Red review writes a structured failure report and resumes the existing Codex session. The fifth red review writes `escalated` and refuses subsequent build commands.

- [ ] **Step 5: Execute a real red-to-green live case**

Use the running Minibook service as the first controlled real system: Codex modifies a small isolated integration target with a deliberately incorrect health expectation, real HTTP validation fails, the same Codex session receives the failure report, corrects it, real HTTP validation passes, and Quality Warden approves correlated evidence. Record both iterations.

- [ ] **Step 6: Execute a real five-red escalation case**

Use an intentionally impossible immutable criterion against an isolated local service. Run five complete live attempts, assert `escalated`, and prove no sixth Codex invocation appears in the process/event log.

- [ ] **Step 7: Verify all evidence/controller tests**

Run: `python -m pytest tests/validation/test_real_evidence_policy.py -v`

Run: `python -m pytest tests/live/test_real_case_iteration_live.py -v -m live`

Run: `python -m pytest -q`

- [ ] **Step 8: Commit real-case loop**

```powershell
git add agenten/validation agenten/delivery/controller.py tests/validation/test_real_evidence_policy.py tests/live/test_real_case_iteration_live.py
git commit -m "feat: enforce real-case evidence and bounded redo"
```

---

### Task 8: Selective Learning and End-to-End Team Proof

**Files:**
- Create: `agenten/learning/__init__.py`
- Create: `agenten/learning/candidates.py`
- Create: `agenten/learning/promotion.py`
- Create: `tests/learning/test_selective_learning.py`
- Create: `tests/live/test_hermes_team_e2e.py`
- Modify: `README.md`
- Modify: `docs/WORKSTREAMS.md`

**Interfaces:**
- Consumes: rejected outputs, real failure evidence, correction, retest, and Quality Warden decision
- Produces: project-memory candidate or skill-promotion candidate; never writes a skill without two validated applications

- [ ] **Step 1: Write selective-learning tests**

Prove raw successful transcripts are not promoted, rejected output produces a candidate, a red retest blocks promotion, one green application remains project-only, two relevant green applications plus Quality Warden approval allow a skill candidate, and secrets are rejected before storage.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/learning/test_selective_learning.py -v`

- [ ] **Step 3: Implement candidate and promotion policy**

Store category, bad output digest, evidence references, diagnosis, correction, and retest result. Store only a compact validated pattern after success. Redact and reject credentials before persistence. Promotion emits a reviewable candidate; it does not silently overwrite Hermes skills.

- [ ] **Step 4: Run the complete real team scenario**

The E2E test creates a Minibook plan, materializes assigned TODOs, runs the three Hermes roles, invokes real Codex with Second Brain discovery, observes one intentional real-case failure, resumes the same session, reaches green, persists a learning candidate, restarts Captain during the scenario, and finishes with a Quality Warden-approved `passed` state. Every external ID and evidence hash is written to a gitignored run manifest.

- [ ] **Step 5: Audit the no-mock invariant**

Scan the live run manifest and evidence rows for `mock`, `fake`, `stub`, `simulated`, `synthetic`, `fallback`, and `mock: true`. Fail if any appears as accepted provenance. Confirm every required external system has a real observation and correlation ID.

- [ ] **Step 6: Run full verification**

Run: `python -m pytest tests/learning/test_selective_learning.py -v`

Run: `python -m pytest tests/live/test_hermes_team_e2e.py -v -m live`

Run: `python -m pytest -q`

Run: `python scripts/verify_submission.py`

Expected: all new focused and live tests PASS; full suite and submission verifier PASS with no unclassified failures.

- [ ] **Step 7: Document only proven behavior**

Update README and workstreams with exact commands, required live services, costs, restart posture, five-iteration behavior, and known limits. Do not claim any optional target system that was not exercised in the final E2E manifest.

- [ ] **Step 8: Commit learning and proof**

```powershell
git add agenten/learning tests/learning tests/live/test_hermes_team_e2e.py README.md docs/WORKSTREAMS.md
git commit -m "feat: prove learning hermes team end to end"
```

---

## Execution Gates

1. Do not start Task 1 until this plan is visible and hash-verified in Minibook.
2. Do not scale beyond one active TODO until one real Codex/Second-Brain path passes.
3. Do not run the five-iteration controller until one red-to-green case passes manually.
4. Do not enable the Windows scheduled task until restart recovery passes from a fresh process.
5. Do not promote learning into a skill during the first application.
6. Stop immediately on credential exposure, ambiguous repository ownership, or an unexpected branch/worktree switch.

## Final Handoff Evidence

- Minibook project ID, plan post ID, and three Hermes identity IDs.
- Delivery database path and schema version.
- Real Codex session IDs and sanitized JSONL paths.
- Second Brain MCP discovery evidence.
- Reasoning run IDs and restart checkpoint IDs.
- Real-case reports for red-to-green and five-red escalation scenarios.
- Full test output and accepted evidence hashes.
- Git commits for each task and an explicit list of pre-existing unrelated workspace changes.
