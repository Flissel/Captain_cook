# Real-Case Hermes Team Design

## Decision

Captain Cook will coordinate three persistent, learning Hermes identities:
`architect_builder`, `real_case_tester`, and `quality_warden`. A deterministic
ledger service remains responsible for durable state, leases, idempotency,
iteration limits, and restart recovery. The live delivery path uses Codex CLI,
the already configured Second Brain MCP server, and real external systems; it
must never treat a mock, fake, stub, simulated response, or `"mock": true`
payload as passing evidence.

## Goals

- Turn an approved plan into assigned, visible TODOs that every participating
  Hermes agent can discover and track.
- Let the builder invoke Codex CLI with Second Brain context through an
  explicit, auditable tool contract.
- Run independent real-case tests after implementation.
- Repeat build, test, and review at most five times.
- Persist enough state that a crashed or sleeping process can resume without
  relying on an in-memory event queue or one long AutoGen conversation.
- Learn selectively from poor output, errors, and meaningless work without
  polluting durable skills with unverified conclusions.

## Non-Goals

- Replacing the existing offline demonstration or its deterministic executor.
- Allowing unit tests to open the real-case quality gate.
- Letting agents invent acceptance criteria after implementation begins.
- Automatically continuing after the fifth failed iteration.
- Treating Minibook, a Hermes-local TODO store, or an AutoGen transcript as the
  authoritative execution ledger.

## Runtime Topology

```text
external wake mechanism
        |
        v
Captain runtime ---------------------------+
  |                                        |
  | durable TODO commands                  | short reasoning slices
  v                                        v
ledger/state gateway                AutoGen Society of Mind
  |                                        |
  | assigned TODO view                     | tool request
  v                                        v
Hermes Architect/Builder ------------> Codex CLI
  |                                  codex exec / resume
  |                                        |
  |                                        +--> Second Brain MCP
  |                                        +--> permitted real tools
  v
Hermes Real-Case Tester
  |
  | real evidence only
  v
Hermes Quality Warden
  |
  v
Captain terminal transition: passed / redo / escalated
```

Captain owns orchestration and assignment. Hermes agents own work on assigned
TODOs and maintain local TODO projections for situational awareness. The
ledger is the only source of truth. Minibook mirrors plans, assignments,
progress, failure reports, and final decisions for human and agent visibility.

## Roles

### Captain

- Creates TODOs from the approved plan.
- Assigns exactly one responsible role per TODO.
- Persists state before publishing the corresponding event.
- Starts or resumes reasoning slices.
- Enforces leases and the five-iteration ceiling.
- Applies terminal state changes recommended by the Quality Warden.

### Hermes Architect/Builder

- Reads the posted plan and assigned TODOs.
- Uses the Codex CLI tool to implement within the authorized workspace.
- Cannot mark its own work as passed.
- Responds to a structured failure report by resuming the existing Codex
  session whenever the session is still valid.

### Hermes Real-Case Tester

- Receives an immutable copy of the acceptance criteria.
- Uses live dependencies and real tools to execute integration, end-to-end,
  and user-realistic cases.
- Records commands, correlation identifiers, responses, side effects, and
  timestamps.
- Does not repair builder code while acting as tester.

### Hermes Quality Warden

- Confirms that every required observation came from the declared real system.
- Rejects missing, contradictory, mocked, stale, or uncorrelated evidence.
- Recommends `passed` or emits a structured failure report for `redo`.
- Reviews proposed durable learning or skill changes.

### Deterministic Ledger Service

- Is not an LLM agent.
- Stores TODO state, assignment, lease, attempts, events, evidence references,
  and terminal decisions.
- Provides idempotent compare-and-set transitions and restart recovery.

## TODO Contract

Each durable TODO contains:

```yaml
todo_id: string
project_id: string
title: string
description: string
acceptance_criteria: [string]
assignee: architect_builder | real_case_tester | quality_warden
status: planned | assigned | in_progress | testing | reviewing | passed | redo | escalated
iteration: 1
max_iterations: 5
lease_expires_at: timestamp | null
codex_session_id: string | null
dependencies: [todo_id]
evidence: [evidence_id]
failure_report_id: string | null
version: integer
```

The Hermes-local TODO projection may contain `id`, `content`, and `status`, but
it cannot own assignment or iteration state. Assignment remains in the ledger
because the existing Hermes TODO tool has no assignee field.

## State Machine

```text
planned -> assigned -> in_progress -> testing -> reviewing -> passed
                         ^                         |
                         +---------- redo <-------+

reviewing -- fifth failed iteration --> escalated
```

Invariants:

1. Only Captain creates and assigns durable TODOs.
2. Only the assigned role may advance its non-terminal working state.
3. Builder cannot set `passed`.
4. Tester cannot approve its own test evidence.
5. Quality Warden recommends a decision; Captain commits the transition.
6. `redo` increments `iteration` exactly once with optimistic version checks.
7. Iteration cannot exceed five; a fifth rejected review becomes `escalated`.
8. State is committed before its event is published.
9. Event handlers deduplicate by `event_id`.
10. `passed` requires all declared real-case evidence predicates to hold.

## Codex CLI Tool Contract

Captain exposes Codex CLI to the builder through a constrained adapter:

```python
start(task_id, workspace, plan_path, acceptance_criteria) -> CodexRun
resume(task_id, session_id, failure_report_path) -> CodexRun
status(task_id, session_id) -> CodexRunStatus
cancel(task_id, session_id, reason) -> CodexRunStatus
```

`CodexRun` records the actual session ID, workspace, branch, starting and
resulting commit, command timestamps, exit status, changed paths, and captured
event-log location. The adapter invokes real `codex exec` / `codex exec
resume`; it never returns synthesized success. Codex inherits the existing
`secondbrain` MCP registration and may use only tools allowed by the assigned
role and workspace policy.

## Reasoning Endpoint and Slices

One unbounded AutoGen request is prohibited. Captain exposes durable reasoning
runs:

```http
POST /reasoning-runs
GET  /reasoning-runs/{run_id}
POST /reasoning-runs/{run_id}/resume
POST /reasoning-runs/{run_id}/heartbeat
POST /reasoning-runs/{run_id}/cancel
```

Defaults:

- Reasoning slice: at most five minutes or ten AutoGen turns.
- Heartbeat: every fifteen seconds during active reasoning or a child tool.
- Worker lease: ten minutes, renewed by valid heartbeats.
- Checkpoint: after every completed agent turn and every completed tool call.
- Codex child-process budget: fifteen minutes unless the TODO explicitly
  authorizes a longer real operation.
- Full build/test/review iteration budget: thirty minutes by default.

A slice ends only at a safe boundary. Captain persists the conversation
summary, decisions, open TODOs, next speaker, child process/session IDs, and
next event before yielding. A live child process may outlast a reasoning slice
as long as its supervised process record emits heartbeats.

## Wake-Up and Recovery

The event bus transports notifications but is not durability. An external
process manager or scheduler must restart Captain after host/process failure.
At startup Captain scans the ledger and:

- republishes work that is durably queued but not terminal;
- leaves work with an unexpired lease untouched;
- converts expired assigned or in-progress work into a retry decision;
- resumes persisted reasoning runs from their last safe checkpoint;
- polls or resumes recorded Codex sessions instead of starting duplicates;
- explicitly recovers `testing` and `reviewing`, which the current generic
  recovery path does not yet understand;
- flags ambiguous state for human review instead of fabricating progress.

## Real-Case Evidence Gate

Every acceptance criterion maps to a typed observation channel. Examples:

- HTTP behavior: real request, response, correlation ID, and expected payload.
- n8n behavior: deployed workflow ID plus actual execution record.
- Email behavior: actual Mailpit message with correlated case identifier.
- Codex behavior: actual Codex session ID and captured command/event stream.
- Second Brain behavior: actual MCP call result tied to the Codex run.
- Persistence behavior: state read back from the real configured store.

The gate fails closed when a dependency is unreachable. Unit tests may run
before real-case tests and remain useful developer feedback, but they cannot
substitute for a required live observation. Any evidence marked mock, fake,
stub, simulated, synthetic, fallback, or `mock: true` is rejected.

## Five-Iteration Loop

```text
1. Architect/Builder writes the plan and Captain mirrors it to Minibook.
2. Captain materializes and assigns TODOs.
3. Builder invokes or resumes Codex CLI.
4. Focused and full automated tests run.
5. Real-Case Tester runs live acceptance cases.
6. Quality Warden validates evidence.
7a. Green: Captain marks passed and stores a compact validated pattern.
7b. Red and iteration < 5: store failure, mark redo, resume Codex.
7c. Red at iteration 5: mark escalated and require human direction.
```

No agent may silently weaken acceptance criteria between iterations.

## Selective Learning

Raw successful conversations are not promoted. A learning candidate is
created for tool/API errors, red tests, timeouts, lost leases, irrelevant or
meaningless output, fabricated tool use, unverified success claims,
contradictions, out-of-scope mutations, secret exposure, and differences
between lower-level tests and real-case behavior.

Each candidate stores the bad output, observed evidence, diagnosis,
correction, and retest result. After a green real-case run, Captain stores only
a compact validated pattern. A Quality Warden review is required before a
candidate changes project memory or a reusable skill. A reusable skill change
requires the corrected pattern to pass in at least two relevant applications.

## Minibook Projection

Minibook is the collaboration and visibility surface, not the state authority.
Captain posts:

- the approved plan;
- assigned TODO summaries and responsible Hermes identity;
- iteration changes and structured failure reports;
- evidence links without credentials or hidden holdout details;
- final `passed` or `escalated` decisions.

Agents check Minibook for discussion and mentions, but every mutation command
is validated against the current ledger version before it can affect work.

## Failure Handling

- Duplicate event: deduplicate and return the existing transition result.
- Stale TODO version: reject and force the agent to reload current state.
- Missing heartbeat: expire the lease and schedule retry or escalation.
- Codex process timeout: capture logs, cancel the process, and emit a failure
  report; do not claim implementation success.
- Reasoning slice timeout: checkpoint at the last completed boundary and
  schedule a new slice.
- Real dependency unavailable: classify as infrastructure failure, retain
  evidence, and do not count it as a behavioral pass.
- Invalid/mocked evidence: reject review and record an evidence-policy failure.
- Fifth rejected iteration: stop all automatic retries and escalate.

## Acceptance Criteria

The implementation is complete only when all of the following pass using real
local services:

1. An approved plan is posted to Minibook and produces assigned durable TODOs.
2. Each Hermes identity sees its assigned TODO projection.
3. Builder starts a real Codex CLI session and the run records a real session
   ID while Codex can discover the configured Second Brain MCP server.
4. A forced process interruption resumes from the ledger checkpoint without
   duplicate TODO execution or a second Codex session.
5. A deliberately failing real case produces a structured failure report and
   resumes the same Codex session.
6. A corrected implementation becomes `passed` only after live evidence is
   independently reviewed.
7. Five consecutive rejected iterations become `escalated` and trigger no
   sixth build attempt.
8. Evidence containing `mock: true` or declared mock/fake/stub provenance is
   rejected even when unit tests are green.
9. Restart recovery covers `testing` and `reviewing` states.
10. No API key, token, credential, hidden holdout case, or raw sensitive model
    context appears in Minibook, logs, commits, or learning records.

## Delivery Boundaries

This design should be implemented in independently reviewable increments:

1. Durable TODO/assignment and transition contract.
2. Codex CLI adapter with session capture and Second Brain preflight.
3. Durable reasoning-run endpoint and wake/recovery behavior.
4. Three Hermes identity projections and Minibook mirroring.
5. Real-case evidence runner, Quality Warden gate, and five-iteration loop.
6. Selective learning promotion after the live loop is proven.

The existing deterministic Householder runtime remains an offline fallback and
must not be presented as evidence that this live design is complete.
