# AgentFarm Input Evaluation Design

## Decision

Captain evaluates an immutable AgentFarm `input.md` with a bounded AutoGen
Society-of-Mind run. The run extracts implementable team components from the
whole source document and emits one complete, reviewable subtask plan per
component. Every plan includes its required tests and acceptance criteria.

The Society of Mind may reason, hand off, and submit typed candidates through
Captain-owned tools. Captain remains the sole validator, authoritative state
owner, and artifact writer. Minibook, Hermes, and an AutoGen transcript are
not sources of truth.

## Source Contract

The first evaluation uses the read-only AgentFarm source identified by this
digest:

```yaml
source_kind: agentfarm_input_markdown
source_sha256: e55e667474a3b6a3d1a1dc6f927fec9ea67a247ea30ea61141c5b994495623ac
source_bytes: 185292
media_type: text/markdown
```

The absolute local path is run configuration, never a hard-coded runtime
dependency. The configured file must hash to `source_sha256` before a run
starts. A changed file creates a new evaluation run and cannot resume an old
one.

Before any LLM call, Captain deterministically parses Markdown headings and
creates immutable `SourceBlock` artifacts. Each block has a block ID, heading
path, byte range, text digest, and redaction result. Agents receive source
content only through `read_source_block`; they never receive a mutable path or
the entire 185-KB document by default.

## Goals

- Find the components that should be built by the AgentFarm team, while
  separating executable requirements from links, examples, and reference
  material.
- Produce one complete component plan for each accepted component.
- Require an explicit test/acceptance block for every component plan.
- Keep an inspectable Markdown result now and a lossless mapping to later
  MariaDB per-run evidence.
- Use real LLM calls only in the live evaluation gate; retain deterministic
  and replay tests for normal development.

## Non-Goals

- Implementing the proposed AgentFarm components.
- Giving an LLM, a tool call, Minibook, or Hermes direct lifecycle authority.
- Persisting this first evaluation in MariaDB; that is a follow-on adapter,
  not an excuse to keep state only in a transcript.
- Treating plans as implementation success or tests as already passed.
- Letting the evaluator access external CRM, mail, n8n, browser, or Codex
  tools. This is a planning-only evaluation.

## AutoGen Topology

The implementation targets the installed AutoGen 0.7.x API. It uses the
current `SocietyOfMindAgent` as an outer, presentation-only wrapper around an
inner `RoundRobinGroupChat`.

```text
Input ingestion and source blocks (deterministic)
                  |
                  v
  SocietyOfMindAgent -- final, non-authoritative Markdown summary
                  |
                  v
  RoundRobinGroupChat (at most three review rounds)
      Source Analyst -> Component Planner -> QA Reviewer -> Planner
                  |
                  v
       Captain tool boundary and deterministic validator
                  |
                  v
        candidate artifacts -> accepted evaluation.md
```

`SocietyOfMindAgent` runs its inner team and resets that team after producing
its summary. Consequently, all resume state lives in Captain's `EvaluationRun`
and artifact store, not in AutoGen agent memory. The outer summary has no
write-capable tool.

The model client must support tool calling and must set
`parallel_tool_calls=False`. The run never relies on agent-selected concurrent
handoffs; Captain owns ordering and persistence.

## Roles

### Source Analyst

- Reads only source blocks using `read_source_block`.
- Proposes a component inventory with source citations and a confidence score.
- Marks material as `component`, `dependency`, `reference`, or `ambiguous`.
- Cannot stage plans, approve output, or invoke delivery tools.

### Component Planner

- Transforms one accepted inventory item at a time into a typed candidate plan.
- Calls `stage_component_plan` only for a complete candidate.
- Revises only the candidate identified in a QA review.
- Cannot approve its own work or release a subtask to a swarm.

### QA Reviewer

- Independently assesses each candidate against the fixed rubric below.
- Returns a typed `approved` or `revision_required` review with defect codes.
- Cannot create a component plan, relax a rubric rule, or finalize the run.

### Captain

- Creates the run, source manifest, candidate IDs, and bounded turn schedule.
- Validates every tool input before writing a non-authoritative candidate.
- Performs the final deterministic coverage and schema gate.
- Writes the final Markdown report and later maps the same record to MariaDB.

## Tool Contracts

All tools are Captain adapters with typed Pydantic input and output. A tool
call never writes a released work batch, changes a workspace, or grants MCP
capabilities.

```text
read_source_block(run_id, block_id) -> SourceBlockView
stage_component_inventory(run_id, inventory: ComponentInventoryCandidate) -> InventoryReceipt
stage_component_plan(run_id, candidate: ComponentPlanCandidate) -> CandidateReceipt
record_qa_review(run_id, review: QaReview) -> ReviewReceipt
```

`read_source_block` checks run ownership and returns a redacted immutable
view. `stage_component_inventory` verifies source citations, unique component
keys, and allowed classifications before storing the append-only inventory
artifact. `stage_component_plan` verifies source citations, candidate schema,
unique component key, and expected revision number before storing an append-only
candidate artifact. `record_qa_review` verifies that the review targets an
existing candidate and uses only registered rubric codes.

The tool outputs are receipts, not authority. Captain alone computes
`accepted`, `needs_revision`, `unresolved`, or `failed` after the loop ends.

## Candidate and Review Contracts

Each `ComponentPlanCandidate` must contain:

```yaml
component_key: lowercase-stable-id
title: human readable component name
source_block_ids: [immutable source block ID]
scope: what this component owns
non_goals: what it explicitly does not own
team_roles: [planned AgentFarm team roles]
dependencies: [component_key]
implementation_plan:
  - ordered, concrete implementation step
interfaces: [typed API, event, or artifact boundary]
acceptance_tests:
  - test_id: stable id
    type: unit | integration | contract | live
    setup: required fixture or service
    action: action under test
    expected: observable success condition
    command: executable command or explicit manual gate
definition_of_done: [objective conditions]
risks: [bounded risk]
```

A `QaReview` contains a candidate ID, revision number, decision, rubric score,
defect codes, evidence references, and actionable revision requests. It cannot
contain a replacement plan.

The QA rubric requires all of the following:

1. The component is grounded in cited source blocks.
2. It has one clear owner boundary and does not duplicate another component.
3. Dependencies form a valid DAG.
4. The implementation plan is concrete enough for a builder to execute.
5. At least one acceptance test is present; each test has setup, action, and
   observable expected outcome.
6. Test categories match risk: isolated logic needs unit tests, boundaries
   need contract/integration tests, and declared live dependencies need a live
   gate that is explicitly marked unavailable until configured.
7. No plan claims implementation or passing evidence that the evaluation did
   not execute.

## Bounded Evaluation Loop

For each inventory item, the inner team follows this order:

1. Source Analyst cites the relevant blocks and classifies the item.
2. Component Planner stages a full candidate plan.
3. QA Reviewer records an independent review.
4. Captain accepts the candidate or schedules the next Planner revision.

The maximum is three Planner/QA rounds per component, ten AgentChat turns per
reasoning slice, and a configurable total LLM-call/token/cost budget. At a
budget, timeout, malformed-output, or unresolved-review boundary Captain
persists a partial result with the explicit status `unresolved`; it never
silently invents missing plans.

An evaluation succeeds only when every accepted component has an approved QA
review and the final deterministic gate confirms full component coverage. It
may finish `partial` when some components are unresolved; this is evaluative
evidence, not a false success.

## Artifacts and Future MariaDB Mapping

Each run produces a gitignored directory:

```text
artifacts/evaluations/<run_id>/
  source-manifest.json
  component-inventory.json
  candidates/<component_key>/revision-<n>.json
  qa-reviews/<component_key>/revision-<n>.json
  evaluation.md
  run-manifest.json
```

`evaluation.md` is the human review artifact. It includes source digest,
run/model/prompt versions, timestamps, round counts, token and cost totals,
component plans, QA decisions, unresolved items, and a statement that the
listed acceptance tests are planned rather than executed. Secrets, raw API
responses, hidden holdouts, and unredacted sensitive input are excluded.

`run-manifest.json` provides the future MariaDB mapping: `evaluation_runs`,
`evaluation_source_blocks`, `evaluation_candidates`, `evaluation_reviews`, and
`evaluation_artifacts`. Every record has a run ID, correlation ID, schema
version, immutable digest, timestamp, idempotency key, and causal reference.

## Failure and Security Policy

- Source digest mismatch: fail before any LLM call.
- Unsafe or unredactable source content: fail closed and record only the
  classification, never the sensitive text.
- Unknown block, candidate, review code, duplicate revision, or stale run
  version: reject deterministically.
- Tool failure or model timeout: record a failed observation and yield a
  resumable run; do not retry beyond the declared budget.
- QA disagreement at the final round: mark the component `unresolved`.
- Candidate attempts to add tools, credentials, real-service side effects, or
  a direct release: reject it as out of scope.
- Replays with the same source digest and run idempotency key return the
  existing manifest rather than duplicating candidates.

## Verification Strategy

The implementation is not complete until it proves:

1. Markdown ingestion is deterministic: same source bytes yield the same
   source manifest and blocks.
2. The candidate schema rejects missing test plans, unsupported test types,
   duplicate component keys, bad citations, and cyclic dependencies.
3. Tool contracts prevent agents from writing accepted work or bypassing QA.
4. A replay-model integration test covers Analyst -> Planner -> QA -> Captain
   acceptance and a revision path without network calls.
5. Budget, timeout, malformed output, and third-round disagreement create
   durable partial/unresolved results without duplicate artifacts.
6. The Markdown report is reproducible from stored artifacts and redacts
   secrets and holdouts.
7. One explicit `pytest.mark.live` evaluation runs against the configured
   OpenAI model, records a real model identifier and usage, and never claims
   that the planned acceptance tests themselves have run.

## Delivery Boundary

This feature is a separate Captain work package. It consumes only the immutable
AgentFarm input and existing Captain planning/artifact contracts. It does not
modify the AgentFarm repository, Hermes submodule, Minibook, VibeMind n8n, or
the active Captain n8n builder stack.
