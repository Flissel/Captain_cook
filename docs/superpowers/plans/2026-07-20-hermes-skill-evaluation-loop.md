# Hermes Skill Evaluation Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Hermes-created AutoGen agent build through a released skill, produce immutable build/test/tool-gap evidence, retain successful skill candidates privately, and let Captain/Gateway alone validate and publish a ready-to-use capability.

**Architecture:** Keep `hermes-agent/` an external runtime reached only through Captain's CLI adapter. Add a typed skill-evaluation aggregate beside the existing factory job aggregate. The Gateway persists the aggregate and enforces its lease, digest, evidence and tool-gap invariants. The existing factory state machine remains the lifecycle source; its promotion decision additionally requires a successful linked skill evaluation.

**Tech Stack:** Python 3.11, Pydantic v2 frozen contracts, existing Captain Agent Factory, MariaDB Gateway, filesystem evidence store for deterministic tests, Hermes CLI, AutoGen skill documents, n8n MCP capability broker.

## Global Constraints

- Captain/MariaDB is the only lifecycle authority and the only publisher of shared skills or `ready_to_use` capabilities.
- Hermes must receive exactly one released skill in a valid short-lived Factory lease and must report a digest-matching usage receipt before it can build, repair, test, or propose a skill candidate.
- A completed task may retain a private immutable candidate skill and usage receipt. Hermes may never write the shared skill registry or a Captain block directly.
- Tool gaps use `TODO_TOOL.v1`. A missing required tool blocks promotion; an optional gap remains visible and non-blocking only after all acceptance assertions succeed.
- n8n is available only through Captain-issued `integration_intent=n8n` tool references. Do not expose API keys, MCP tokens, or raw n8n endpoints in artifacts, prompts, logs, or tests.
- Candidate code runs in the existing sealed temporary workspace. Do not use live providers, databases, browser automation, or n8n in deterministic tests.
- Preserve existing factory event ordering and isolated `captain_test`/live-gate conventions. Do not modify the `hermes-agent/` submodule in this work package.

---

## File map

| Area | Files | Responsibility |
| --- | --- | --- |
| Typed contracts | `agenten/agent_factory/contracts.py`, new `agenten/agent_factory/skill_evaluation.py` | released-skill selection, candidate, receipt, TODO_TOOL, immutable evaluation evidence |
| Candidate persistence | new `agenten/agent_factory/skill_store.py`, `agenten/agent_factory/evidence_store.py` | append-only private candidates and content-addressed evidence |
| Hermes boundary | `agenten/agent_factory/hermes_cli.py`, `agenten/agent_factory/orchestration.py` | pass one lease-bound skill and parse only typed evidence |
| Lifecycle authority | `agenten/agent_factory/state_machine.py`, `agenten/agent_factory/release_gate.py`, `agenten/agent_factory/service.py` | require a successful accepted evaluation before promotion |
| Gateway authority | `gateway/contracts.py`, `gateway/store.py`, `gateway/factory_repository.py`, `gateway/app.py` | durable validation and query surfaces for skill-evaluation records |
| n8n tool broker | `agenten/agent_factory/n8n_tools.py`, `agenten/agent_runtime/n8n_endpoint.py` | lease-scoped typed n8n MCP tool references only |
| Tests and documentation | `tests/agent_factory/`, `tests/gateway/`, `tests/live/`, `docs/ARCHITECTURE.md`, `docs/WORKSTREAMS.md` | deterministic coverage, explicit live evidence, ownership docs |

## Implementation tasks

### Task 1: Define the skill-evaluation contracts before adding behavior

**Files:**
- Modify: `agenten/agent_factory/contracts.py`
- Create: `agenten/agent_factory/skill_evaluation.py`
- Create: `tests/agent_factory/test_skill_evaluation_contracts.py`

- [x] Write failing contract tests for the following frozen, `extra="forbid"`, schema-versioned records:
  - `ReleasedHermesSkill.v1`: Captain-owned `skill_id`, integer version, capability, immutable `content_ref`, SHA-256, and lifecycle status `released`.
  - `HermesSkillEvaluationRequest.v1`: job/correlation/subject identity, active Factory lease, exactly one released skill, candidate source reference, accepted assertions, and bounded iteration budget.
  - `HermesSkillUsageReceipt.v1`: Hermes producer, request/lease identity, used skill id/version/digest, bounded command/evidence references, and outcome.
  - `HermesSkillCandidate.v1`: immutable candidate content/digest, parent released-skill reference, creation reason and private-candidate status.
  - `ToolGapMarker.v1`: the literal schema name `TODO_TOOL.v1`, `required|optional` severity, needed input/output contract, least-privilege capability, at most three implementation options, acceptance assertion ids, and evidence reference.
  - `HermesSkillEvaluationEvidence.v1`: links receipt, candidate evaluation, tool gaps, build/test checks, and assertion ids to one job and correlation.
- [x] Assert rejection of unknown schema fields, non-UTC timestamps, duplicate references/assertions, altered digests, a skill not marked `released`, a lease/job/correlation mismatch, an empty tool option, more than three tool options, and an unbounded command.
- [x] Implement the models in `skill_evaluation.py`; add only shared Factory enums or references that genuinely belong in `contracts.py`. Reuse `ArtifactRef`, `CapabilityProfile`, `IntegrationIntent`, identifier validation, and the existing `FactoryLease` rather than creating parallel authority types.
- [x] Add a small `required_tool_gaps(evidence)` helper with tests that returns precisely the unresolved `required` markers. It must not decide publication.

### Task 2: Add an append-only private candidate and receipt store

**Files:**
- Create: `agenten/agent_factory/skill_store.py`
- Modify: `agenten/agent_factory/evidence_store.py`
- Create: `tests/agent_factory/test_skill_store.py`

- [x] Start with failing tests for `SkillEvaluationStore` operations: record a receipt, record a tool gap, retain a candidate only after a successful evidence record, fetch by evaluation id, and idempotently replay identical records.
- [x] Add failure tests proving that a failed build/test cannot create a candidate, a changed replay under the same id is rejected, and callers cannot persist a released/shared status through this private store.
- [x] Implement a filesystem test adapter under `artifacts/agent-factory/skill-evaluations/`, using content-addressed references and the same write-once/hash verification approach as `FilesystemFactoryEvidenceStore`.
- [x] Define a `SkillEvaluationRepository` protocol and an `InMemorySkillEvaluationRepository` for tests. Keep the protocol free of MariaDB or Hermes imports so Gateway can implement it without violating import boundaries.
- [x] Persist only redacted structured records. Tests must assert no secret-looking fields or raw n8n endpoint values are accepted in stored metadata.

### Task 3: Make Hermes consume one released skill and emit typed evaluation evidence

**Files:**
- Modify: `agenten/agent_factory/hermes_cli.py`
- Modify: `agenten/agent_factory/orchestration.py`
- Modify: `agenten/agent_factory/candidate_evaluation.py`
- Create: `tests/agent_factory/test_hermes_skill_evaluation.py`
- Modify: `tests/agent_factory/test_hermes_cli.py`
- Modify: `tests/agent_factory/test_candidate_evaluation.py`

- [x] Add failing adapter tests proving that the Hermes prompt contains exactly the selected released skill path/reference, its digest, the Factory lease id, workspace reference, assertion ids, and no credential or raw n8n endpoint.
- [x] Extend `HermesCliSettings` with an explicit read-only released-skill root. Reject a resolved skill outside that root and reject a missing or digest-mismatched skill before spawning Hermes.
- [x] Change `_prompt_for()` so Hermes is instructed to: use the supplied skill first; write only in the leased workspace; return one typed evaluation envelope; record a `TODO_TOOL.v1` rather than inventing access; retain a skill candidate only after the task is successful; and never publish a skill or write the ledger.
- [x] Add a `HermesSkillEvaluationCoordinator` which receives an approved request, invokes the CLI adapter, passes candidate build/test work to the existing `FactoryCandidateEvaluator`, persists receipt/evidence through the private store, and returns a Captain-recordable result. Inject the CLI, evaluator, clock, and stores for deterministic tests.
- [x] Reuse the sealed archive / temporary-workspace evaluator. Add tests for: successful skill usage plus retained candidate; build failure with no candidate; test failure triggering Captain's bounded improvement path; malformed Hermes JSON; stale lease; and an unresolved required gap.
- [x] Keep `HermesCliFactory.dispatch()` compatible with current role dispatches. The skill-evaluation path must be additive, not silently change existing block parsing.

### Task 4: Enforce tool-gap and evaluation evidence in the Factory lifecycle

**Files:**
- Modify: `agenten/agent_factory/state_machine.py`
- Modify: `agenten/agent_factory/service.py`
- Modify: `agenten/agent_factory/release_gate.py`
- Modify: `tests/agent_factory/test_state_machine.py`
- Modify: `tests/agent_factory/test_service.py`
- Modify: `tests/agent_factory/test_release_gate.py`

- [ ] First add failing lifecycle tests for a normal Factory path that reaches `QUALITY_REVIEWED` but cannot produce `CAPABILITY_PROMOTED` without an accepted matching `HermesSkillEvaluationEvidence`.
- [ ] Test that matching job/correlation/subject version, a successful candidate evaluator, a valid usage receipt, and all required acceptance assertion ids are necessary; any required unresolved `TODO_TOOL` returns a specific blocked reason.
- [ ] Test that optional unresolved markers do not block a fully proven candidate, but remain attached to the Captain decision/evidence projection.
- [ ] Extend the Factory coordinator/repository port with an explicit read-only evaluation lookup. Make promotion validation call that port rather than trusting a Hermes-supplied boolean or artifact name.
- [ ] Extend `evaluate_factory_release()` with a typed evaluation input and fail closed before applying the existing recovery-plus-three-E2E rule. Preserve the existing ordered E2E checks and return auditable reason strings for each missing prerequisite.
- [ ] Preserve the five-attempt ceiling. A code/skill failure must go through the existing `IMPROVEMENT_REQUESTED` transition; Hermes cannot self-issue another lease or alter capability scope.

### Task 5: Persist and validate the aggregate at the Captain Gateway boundary

**Files:**
- Modify: `gateway/contracts.py`
- Modify: `gateway/store.py`
- Modify: `gateway/factory_repository.py`
- Modify: `gateway/app.py`
- Modify: `tests/gateway/test_agent_factory.py`
- Modify: `tests/gateway/test_factory_repository.py`
- Modify: `tests/gateway/test_delivery_events.py`

- [ ] Add failing Gateway tests for recording an evaluation under an active matching Factory lease, immutable identical replay, rejection of changed replay, cross-job/correlation rejection, expired/missing lease rejection, and unknown evidence references.
- [ ] Introduce explicit delivery event payloads for `hermes_skill_evaluation_requested`, `hermes_skill_candidate_built`, `hermes_skill_test_recorded`, `hermes_tool_gap_recorded`, `hermes_skill_evaluation_submitted`, `hermes_skill_published`, and `hermes_ready_to_use_validated`. Each payload must have the required trace context in `DeliveryEventEnvelope` and be added to its parameterized coverage.
- [ ] Extend the Gateway storage schema/migrations and transaction methods to append evaluations, candidate metadata, and tool gaps. Store the shared published-skill state separately from private candidates and expose a read method used by `GatewayFactoryRepository`.
- [ ] Make the Gateway validate content references/digests and proof of an active role-compatible lease before accepting Hermes-originated evidence. Publishing an evaluated candidate and writing `CAPABILITY_PROMOTED` remain Captain-only code paths.
- [ ] Add API/store tests for the full flow: Captain registers job and released skill; Hermes submits successful evaluation; Captain records publication; Captain promotion succeeds. Assert Hermes-originated publication and direct ready-to-use attempts are rejected.

### Task 6: Connect tool-gap resolution to the capability-scoped n8n MCP path

**Files:**
- Modify: `agenten/agent_factory/n8n_tools.py`
- Modify: `agenten/agent_runtime/n8n_endpoint.py`
- Modify: `tests/agent_factory/test_candidate_evaluation.py`
- Modify: `tests/agent_runtime/test_n8n_endpoint.py`
- Modify: `tests/live/test_n8n_mcp_broker_live.py`

- [ ] Add deterministic tests that a `TODO_TOOL.v1` option can reference an existing typed n8n tool only when its lease has `integration_intent=n8n`, the role is `TOOL_INTEGRATOR`, and the capability is explicitly approved.
- [ ] Add a resolver that converts an approved tool-gap option into an opaque, capability-scoped n8n MCP reference. It must return a fresh child environment and must not serialize the token, API key, host URL, or unrestricted tool list.
- [ ] Ensure the candidate manifest records typed input/output schemas and opaque tool references, not n8n workflow internals or credentials. Add a negative test for an unleased direct endpoint.
- [ ] Extend the opt-in live test only to execute an already-approved, least-privilege workflow through the MCP broker. It must fail/skip honestly when required local configuration is absent and must never be counted as a green deterministic gate.

### Task 7: Run end-to-end verification and document the operational contract

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/WORKSTREAMS.md`
- Optionally modify: `scripts/run-gate-e.ps1` only if a new non-secret argument is needed to select a prepared skill-evaluation fixture
- Create or modify: `tests/integration/test_hermes_skill_evaluation_gateway.py`

- [ ] Add an integration test using the Gateway repository, a sealed candidate archive, a released fixture skill, a successful Hermes receipt, and a required/optional tool-gap mix. Verify the exact Captain-only chain: request → skill usage → build/test evidence → candidate retained → Gateway validation → skill published → ready-to-use promotion.
- [ ] Add recovery tests: altered skill digest, a deliberately failing build, a missing required tool, and a stale lease must fail closed; a subsequent clean bounded retry must create fresh evidence and still require the existing recovery-plus-three-successful-E2E release decision.
- [ ] Update architecture/workstream docs with ownership and the command sequence. State explicitly that Minibook receives only the resulting projection and that the Hermes submodule is not a shared-registry writer.
- [ ] Run targeted deterministic suites while iterating:

  ```powershell
  python -m pytest -q tests/agent_factory tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py tests/gateway/test_delivery_events.py tests/agent_runtime/test_n8n_endpoint.py
  python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
  python -m compileall -q agenten gateway blockchain chats config
  python scripts/verify_submission.py
  ```

- [ ] Run the complete deterministic suite once all focused gates pass:

  ```powershell
  python -m pytest -q
  python main.py demo --output artifacts/demo-run.json
  ```

  Do not rewrite the demo artifact unless the implementation intentionally changes its accepted evidence.

- [ ] With a real Captain test database, released fixture skill, and configured local n8n MCP broker, run the provider-backed release command after database-resetting tests:

  ```powershell
  pwsh -NoProfile -File scripts/run-gate-e.ps1
  ```

  Record the exact result, test count, commit SHA, database target, skipped live checks, and whether all three successful provider-backed traces are present. Do not claim the live gate green when prerequisites are absent.

## Acceptance checklist

- [ ] Hermes cannot begin the build without one Captain-released, digest-verified skill.
- [ ] A successful task stores a usage receipt and, when created, an immutable private skill candidate; a failed task stores no candidate.
- [ ] Every missing tool is a structured `TODO_TOOL.v1` with bounded options and an acceptance test; required gaps block `ready_to_use`.
- [ ] Captain/Gateway validates all leases, sources, digests, test/receipt evidence, and publication rights; Hermes cannot publish a skill or promote a capability.
- [ ] n8n access is only a lease-scoped typed MCP tool reference; no secret or raw endpoint enters evidence.
- [ ] Existing Factory, Gateway, architecture, and full deterministic tests pass; provider-backed Gate E is separately reported with no skipped-check inflation.
