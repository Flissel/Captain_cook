# Capability Outcome, Gateway Release, and Live Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Independently validate a Forge candidate, derive one authoritative terminal state, persist a reusable capability package and later execution outcomes in the Gateway, and prove the complete Captain → Hermes → SwarmPipeline → Codex/AutoGen/n8n → Gateway → Minibook chain with controlled recovery and three clean E2E runs.

**Architecture:** Captain consumes only versioned creation-result artifacts and recomputes package/evidence validity. A terminal-decision policy combines schema/security checks, tool gaps, assertions, private holdouts, iteration count, and deadline. The MariaDB Gateway persists immutable decisions, catalog entries, executions, and an ordered projection feed. A separately hosted authenticated runtime endpoint supplies the existing missing `/v1/runtime/execute` boundary. Live release runs only after deterministic and DB-mutating tests complete.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, MariaDB, httpx, AutoGen runtime adapters, Minibook projection HTTP, pytest, PowerShell.

## Global Constraints

- This package owns `agenten/agent_factory/outcome_*`, `state_machine.py`, `release_gate.py`, `service.py`, Gateway persistence/routes/projection, runtime HTTP server, and final live gates. It does not edit input parsing, SwarmPipeline, or Hermes implementation.
- Consume Package A and B through committed fixtures and artifact digests. Never import `minibook.*` or `hermes-agent/*` from Captain production code.
- Captain derives `ready_to_use`, `blocked`, `escalated`, or `rejected`; worker-supplied terminal labels are untrusted evidence only.
- `blocked` is resumable only for a named missing external prerequisite or required user decision. `escalated` is budget exhaustion. `rejected` is an inadmissible security/authority/schema/integrity/non-recoverable quality violation.
- Five iterations and the job `deadline_at` are independent limits; the first exhausted limit stops mutation.
- Preserve the current recovery-then-three-distinct-successful-E2E rule. Do not count the recovery record among the three successes.
- Capability publication and Minibook projection happen only after the Gateway transaction that records the accepted release decision.
- Runtime and projection endpoints are authenticated, typed, idempotent, and secret-redacting.
- Final live evidence targets only the isolated `captain_test` database and Captain-owned services. Never reset or adopt VibeMind n8n data/volumes.

---

## Task 1: Define and Independently Validate Capability Package Outcomes

**Files:**
- Create: `agenten/agent_factory/outcome_contracts.py`
- Create: `agenten/agent_factory/outcome_validation.py`
- Create: `tests/agent_factory/test_outcome_contracts.py`
- Create: `tests/agent_factory/test_outcome_validation.py`
- Create: `tests/fixtures/contracts/capability_package_manifest.v1.json`
- Create: `tests/fixtures/contracts/execution_outcome.v1.json`

**Interfaces:** Produces `CapabilityPackageManifestV1`, `PackageArtifact`, `AssertionOutcome`, `PrivateHoldoutReceipt`, `ControlledRecoveryReceipt`, `ExecutionOutcomeV1`, `FactoryTerminalState`, `FactoryTerminalDecision`, and `CapabilityPackageValidator.validate(job, creation_result, package)`.

- [ ] **Step 1: Write strict package and execution-outcome tests**

Require the logical package roots `team-manifest.json`, `autogen/`, `skills/`, `tests/`, `evidence/`, and `RUNBOOK.md`; require `n8n/` only when declared integration assertions demand it and `adapters/` only when the manifest declares local adapters. Reject duplicate paths/digests, unsafe paths, unknown assertion IDs, raw holdout bodies, credentials, unrestricted local paths, and execution outcomes whose capability/correlation/command/result IDs do not bind.

- [ ] **Step 2: Implement frozen outcome contracts**

```python
class FactoryTerminalState(str, Enum):
    READY_TO_USE = "ready_to_use"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    REJECTED = "rejected"


class CapabilityPackageManifestV1(_FrozenContract):
    schema_name: Literal["captain.capability-package.v1"] = Field(alias="schema", serialization_alias="schema")
    capability_id: str = Field(pattern=IDENTIFIER_PATTERN)
    capability_version: int = Field(ge=1, strict=True)
    factory_job_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    source_ref: ArtifactRef
    team_manifest_ref: ArtifactRef
    artifacts: tuple[PackageArtifact, ...] = Field(min_length=1)
    assertion_outcomes: tuple[AssertionOutcome, ...] = Field(min_length=1)
    private_holdout_receipts: tuple[PrivateHoldoutReceipt, ...] = Field(min_length=1)
    recovery_receipt: ControlledRecoveryReceipt
    release_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=4)
    skill_usage_receipt_ref: ArtifactRef
    tool_gaps: tuple[ToolGapMarker, ...] = ()
    runbook_ref: ArtifactRef
```

`ExecutionOutcomeV1` contains capability/team version, correlation/command/result IDs, typed business output or content-addressed output, assertion results, tool/workflow versions, redacted evidence refs, status, and optional escalation ref. It forbids transcripts, credentials, holdout bodies, and workspace paths by schema and recursive content scanning. Forge produces a candidate manifest; Captain creates `CapabilityPackageManifestV1` only after independent validation, controlled recovery, and the three accepted E2E evidence refs exist.

- [ ] **Step 3: Implement independent package validation**

The validator resolves every artifact through an injected read-only content store, re-hashes bytes, opens the sealed archive in a fresh temporary directory, rejects traversal/symlinks, validates manifest/schema relationships, compiles/imports AutoGen code, executes allowlisted package tests, and compares assertion/holdout/recovery receipts with Captain-owned job references. It never trusts Forge `status="succeeded"` by itself.

- [ ] **Step 4: Run focused contract and validation tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_outcome_contracts.py tests/agent_factory/test_outcome_validation.py tests/agent_factory/test_candidate_evaluation.py
```

- [ ] **Step 5: Commit outcome validation**

```powershell
git add agenten/agent_factory/outcome_contracts.py agenten/agent_factory/outcome_validation.py tests/agent_factory/test_outcome_contracts.py tests/agent_factory/test_outcome_validation.py tests/fixtures/contracts/capability_package_manifest.v1.json tests/fixtures/contracts/execution_outcome.v1.json
git commit -m "feat: validate capability package outcomes"
```

## Task 2: Derive Terminal State and Enforce Both Budgets

**Files:**
- Modify: `agenten/agent_factory/state_machine.py`
- Modify: `agenten/agent_factory/service.py`
- Modify: `agenten/agent_factory/release_gate.py`
- Modify: `tests/agent_factory/test_state_machine.py`
- Modify: `tests/agent_factory/test_service.py`
- Modify: `tests/agent_factory/test_release_gate.py`

**Interfaces:** Extends `FactoryProjection` with deadline-aware state and produces `derive_terminal_decision(job, projection, validation, evaluation, e2e, now)`.

- [ ] **Step 1: Add a terminal-state truth-table test**

Cover:

- active work with budget remaining → next action;
- missing required credential alias/provider/API/user choice → `blocked`;
- attempt five failed → `escalated`;
- deadline reached before attempt five → `escalated`;
- secret/path traversal/foreign lease/schema/digest/authority violation → `rejected`;
- optional tool gap with proven fallback → not blocking;
- required tool gap → `blocked`;
- recovery plus only two successes → `blocked`;
- recovery plus exactly three distinct trailing successes and all assertions → `ready_to_use`.

- [ ] **Step 2: Make the state machine deadline-aware**

```python
def next_action(
    projection: FactoryProjection,
    *,
    now: datetime,
) -> FactoryAction:
    if projection.terminal_decision is not None:
        return FactoryAction(kind=FactoryActionKind.NO_ACTION, attempt=projection.attempt)
    if now >= projection.job.deadline_at:
        return FactoryAction(kind=FactoryActionKind.RECORD_ESCALATION, attempt=projection.attempt)
    return _next_nonterminal_action(projection)
```

Keep historical v1 jobs on their existing iteration-only behavior; v2 jobs use `deadline_at`. An identical terminal decision replays; a different second terminal decision conflicts.

- [ ] **Step 3: Centralize terminal derivation in Captain policy**

`derive_terminal_decision` evaluates in fail-closed priority order: structural/security rejection → explicit prerequisite block → budget exhaustion → package/evaluation/assertion block → E2E release decision → ready. Workers may propose reasons, but the decision cites Captain evidence refs and stable reason codes.

- [ ] **Step 4: Run lifecycle and existing integration tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_state_machine.py tests/agent_factory/test_service.py tests/agent_factory/test_release_gate.py tests/integration/test_hermes_skill_evaluation_gateway.py
```

- [ ] **Step 5: Commit terminal policy**

```powershell
git add agenten/agent_factory/state_machine.py agenten/agent_factory/service.py agenten/agent_factory/release_gate.py tests/agent_factory/test_state_machine.py tests/agent_factory/test_service.py tests/agent_factory/test_release_gate.py
git commit -m "feat: derive bounded factory terminal states"
```

## Task 3: Persist Decisions, Capability Catalog, and Execution Outcomes in Gateway

**Files:**
- Modify: `gateway/contracts.py`
- Modify: `gateway/store.py`
- Modify: `gateway/factory_repository.py`
- Modify: `gateway/app.py`
- Create: `gateway/capability_catalog.py`
- Modify: `tests/gateway/test_agent_factory.py`
- Modify: `tests/gateway/test_factory_repository.py`
- Create: `tests/gateway/test_capability_catalog.py`
- Modify: `tests/gateway/test_delivery_events.py`

**Interfaces:** Adds `POST/GET /v1/factory/terminal-decisions`, `POST/GET /v1/capabilities`, `GET /v1/capabilities/compatible`, and `POST/GET /v1/capability-executions` with Captain-only writes and reader-scoped reads.

- [ ] **Step 1: Write MariaDB/store and API authority tests**

Test identical replay, changed replay conflict, foreign job/correlation/version, unknown artifact digest, pre-promotion catalog write, Hermes-originated terminal/catalog write, execution against unpublished/stale capability, and a successful lookup requiring assertion compatibility plus no unresolved required gaps.

- [ ] **Step 2: Add append-only Gateway payloads**

Add strict delivery payloads for `factory_terminal_decided`, `capability_package_published`, and `capability_execution_recorded`. Link terminal decision and package blocks to the Factory job; link execution blocks to the published capability and runtime command/result. Preserve event/correlation/causation IDs, producer, subject version, idempotency, and monotonic version checks.

- [ ] **Step 3: Implement the catalog adapter consumed by Package A**

```python
class GatewayCapabilityCatalog(CapabilityCatalogPort):
    def compatible_capability(self, job: AgentFactoryJobV2) -> PromotedCapability | None:
        record = self._repository.find_ready_capability(job.required_capability)
        if record is None or not record.satisfies(job.acceptance_assertion_ids):
            return None
        return record.promoted_capability
```

Compatibility is exact on schema major, capability key, accepted assertions, required integration/tool contracts, and non-revoked status. Do not return private evidence or package bytes.

- [ ] **Step 4: Persist atomically after accepted release**

Within one MariaDB transaction: verify recomputed terminal decision is `ready_to_use`, append the terminal decision, append the capability package record, and publish catalog head. A crash before commit exposes none; identical retry exposes one.

- [ ] **Step 5: Run Gateway deterministic and isolated MariaDB tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py tests/gateway/test_capability_catalog.py tests/gateway/test_delivery_events.py
if (-not $env:TEST_MARIADB_DSN.EndsWith('/captain_test')) { throw 'TEST_MARIADB_DSN must target captain_test' }
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/gateway/test_agent_factory.py tests/gateway/test_capability_catalog.py
```

The DSN comes only from local environment configuration and must pass the existing `captain_test` guard.

- [ ] **Step 6: Commit Gateway persistence**

```powershell
git add gateway/contracts.py gateway/store.py gateway/factory_repository.py gateway/app.py gateway/capability_catalog.py tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py tests/gateway/test_capability_catalog.py tests/gateway/test_delivery_events.py
git commit -m "feat: persist capability releases and executions"
```

## Task 4: Host the Missing Authenticated Runtime Execute Endpoint

**Files:**
- Create: `agenten/agent_runtime/http_server.py`
- Create: `agenten/agent_runtime/runtime_entrypoint.py`
- Modify: `agenten/agent_runtime/__init__.py`
- Create: `tests/agent_runtime/test_http_server.py`
- Modify: `scripts/live-demo-services.ps1`
- Modify: `.env.example`

**Interfaces:** Exposes `POST /v1/runtime/execute`, `GET /health`, `create_runtime_app(executor, token)`, and a process entrypoint that composes the existing `AgentRuntimeService` with real Hermes/Codex adapters and Gateway-backed state.

- [ ] **Step 1: Write server contract and auth tests**

Test missing/malformed/wrong bearer token (`401`), strict command schema (`422`), idempotent identical command (`200` with same result), adapter infrastructure failure as typed result, and no token/command body in logs/errors. Use `httpx.ASGITransport` with an injected fake executor.

- [ ] **Step 2: Implement the thin HTTP boundary**

```python
def create_runtime_app(*, executor: RuntimeCommandExecutor, token: str) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/runtime/execute", response_model=AgentRuntimeResult)
    async def execute(command: AgentRuntimeCommand, _: None = Depends(require_runtime_token)) -> AgentRuntimeResult:
        return await executor.execute(command)

    return app
```

Use constant-time token comparison. The route must not add authority; `AgentRuntimeService` still accepts command, derives/validates grants, requires artifacts, runs the adapter, and persists result through Gateway-backed ports.

- [ ] **Step 3: Add a safe service start/stop/status path**

Extend `scripts/live-demo-services.ps1` to validate configuration, start the runtime hidden, record PID/start-time/executable identity in the existing service state area, poll `/health`, and stop only the verified process tree. Never print tokens or reuse an unrelated process on the configured port.

- [ ] **Step 4: Run server and existing HTTP client tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_runtime/test_http_server.py tests/agent_runtime/test_http_executor.py tests/integration/test_live_demo_runtime_chain.py tests/integration/test_live_demo_one_shot.py
```

- [ ] **Step 5: Commit the runtime service**

```powershell
git add agenten/agent_runtime/http_server.py agenten/agent_runtime/runtime_entrypoint.py agenten/agent_runtime/__init__.py tests/agent_runtime/test_http_server.py scripts/live-demo-services.ps1 .env.example
git commit -m "feat: host agent runtime execute service"
```

## Task 5: Project Both Promotions and Runtime Results Through One Ordered Feed

**Files:**
- Modify: `gateway/registry_feed.py`
- Modify: `gateway/store.py`
- Modify: `gateway/app.py`
- Modify: `agenten/delivery/minibook_events.py`
- Modify: `agenten/delivery/projection_feed_client.py`
- Modify: `agenten/delivery/projector.py`
- Modify: `tests/gateway/test_registry_feed.py`
- Modify: `tests/delivery/test_gateway_delivery_client.py`
- Modify: `tests/delivery/test_minibook_projector.py`

**Interfaces:** Replaces promotion-only paging with `minibook_projection_feed(after_index, limit)` over admitted Factory promotion and runtime-result ledger records, projected as `factory_capability_ready_to_use` and `codex.result` events.

- [ ] **Step 1: Write mixed-order pagination and correlation tests**

Insert promotion, unrelated block, runtime result, and second promotion at increasing ledger indices. Page with limit one and prove cursor monotonicity, no duplicates, no missing admitted records, redaction, stable event IDs, runtime result causation ID, and correlation filtering in `GatewayProjectionFeedClient.events_for_correlation()`.

- [ ] **Step 2: Implement one ledger-ordered query**

Read both admitted block types after the cursor, ordered by global ledger index, and fetch `limit + 1`. Do not concatenate two independently paginated feeds. Map runtime result fields through a new `runtime_result_projection()` function; never expose session transcripts, prompts, credentials, errors containing provider text, or workspace paths.

- [ ] **Step 3: Keep Minibook projection idempotent and rebuildable**

The projector recognizes `codex.result`, derives canonical display text from typed enums, and stores one event-to-post identity plus monotonic subject head. Replaying the same mixed feed after restart produces no duplicate post; rebuilding from zero recreates the same redacted state.

- [ ] **Step 4: Run projection tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/gateway/test_registry_feed.py tests/delivery/test_gateway_delivery_client.py tests/delivery/test_minibook_projector.py tests/live/test_minibook_projection_replay_live.py
```

The live replay test remains explicit; if its marker excludes it from this command, report it separately rather than counting it green.

- [ ] **Step 5: Commit the ordered feed**

```powershell
git add gateway/registry_feed.py gateway/store.py gateway/app.py agenten/delivery/minibook_events.py agenten/delivery/projection_feed_client.py agenten/delivery/projector.py tests/gateway/test_registry_feed.py tests/delivery/test_gateway_delivery_client.py tests/delivery/test_minibook_projector.py
git commit -m "feat: project correlated runtime outcomes"
```

## Task 6: Run the Complete Factory Release and Recovery Chain

**Files:**
- Create: `agenten/agent_factory/capability_factory_entrypoint.py`
- Create: `scripts/run-capability-factory-live.ps1`
- Create: `tests/integration/test_to_be_built_capability_factory.py`
- Create: `tests/live/test_to_be_built_capability_factory_live.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/WORKSTREAMS.md`

**Interfaces:** Produces one restart-safe command that accepts `-InputPath`, preflights/compiles/resolves, reuses or creates, independently validates, runs recovery plus three E2E cases, publishes, executes once, and verifies the Minibook projection.

- [ ] **Step 1: Write a deterministic full-chain integration test**

Use real parsers, compilers, state machine, package validator, Gateway repository, and projector with scripted external ports. Assert:

```text
TO_BE_BUILT bytes
 -> AgentFactoryJob.v2
 -> catalog miss
 -> one CreationJob.v1
 -> one CreationResult.v1
 -> package validation
 -> controlled recovery
 -> E2E success 1
 -> E2E success 2
 -> E2E success 3
 -> terminal ready_to_use
 -> catalog publication
 -> ExecutionOutcome.v1
 -> correlated Minibook projection
```

Restart after creation submission and after E2E success 2; prove the same IDs and no duplicate external effects. Add negative cases for input mutation, required tool gap, failed holdout, expired deadline, rejected artifact, and only two successes.

- [ ] **Step 2: Implement the composition entrypoint**

The entrypoint takes service URLs and safe paths as arguments and reads tokens only from environment. It writes a redacted content-addressed evidence manifest under gitignored `artifacts/capability-factory/`. It resumes by correlation/job ID and never silently creates a replacement job for changed bytes.

- [ ] **Step 3: Implement the PowerShell live gate**

The script runs in this order:

1. validate Compose/config without printing secrets;
2. require exact isolated database `captain_test`;
3. start/health-check Captain services, runtime, and Minibook;
4. strict-preflight the selected `TO_BE_BUILT.md`;
5. run one intentional expected-failure recovery case;
6. run three distinct normal E2E batches with no test-side repair;
7. query Gateway terminal decision, package, execution, and ordered projection feed;
8. rebuild/read Minibook projection;
9. run non-mutating final health checks;
10. print IDs, counts, timings, artifact path, and commit SHA but no secrets.

- [ ] **Step 4: Run all deterministic gates before the live gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
.\.venv\Scripts\python.exe -m compileall -q agenten gateway blockchain chats config
.\.venv\Scripts\python.exe scripts/verify_submission.py
```

- [ ] **Step 5: Run the provider-backed gate only after DB-resetting tests**

```powershell
pwsh -NoProfile -File scripts/run-capability-factory-live.ps1 -InputPath .\TO_BE_BUILT.md
```

Expected: one controlled recovery plus three distinct clean E2E traces, a `ready_to_use` terminal decision, one published package, one typed execution outcome, and at least one same-correlation Minibook runtime-result event. Any missing required credential/API/tool/documentation/provider, skip, or unavailable service leaves the gate blocked.

- [ ] **Step 6: Commit operational wiring and documentation**

```powershell
git add agenten/agent_factory/capability_factory_entrypoint.py scripts/run-capability-factory-live.ps1 tests/integration/test_to_be_built_capability_factory.py tests/live/test_to_be_built_capability_factory_live.py docs/ARCHITECTURE.md docs/WORKSTREAMS.md
git commit -m "feat: run capability factory release chain"
```

## Package C Handoff and Release Report

The final report must state:

- exact deterministic and live test counts, skips, and warnings;
- controlled recovery ID and the three normal E2E batch IDs;
- Factory job, creation job, capability version, execution command/result, and projection event IDs;
- Gateway/runtime/Minibook/n8n target identities without credentials;
- isolated database name;
- capability/package/evidence digests;
- final commit SHA and `origin` parity;
- any remaining required/optional `TODO_TOOL.v1` markers.

Do not say `ready_to_use` unless the Gateway decision and every required live item above are present.
