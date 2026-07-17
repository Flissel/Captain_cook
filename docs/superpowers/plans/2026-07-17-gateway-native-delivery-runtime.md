# Gateway-Native Hermes Delivery Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every behavioral change and superpowers:verification-before-completion before every gate or completion claim.

**Goal:** Deliver a restart-safe Hermes agent lane that supervises real Codex CLI sessions, builds real n8n and AutoGen artifacts, validates hidden holdouts, and releases only from MariaDB/Ledger Gateway evidence.

**Architecture:** The MariaDB-backed Gateway is the sole production authority for commands, events, claims, projections, and release evidence. Hermes workers are stateless orchestration processes that recover from Gateway state, AutoGen owns reasoning conversations, n8n owns deterministic integrations, and Minibook receives a read-only registry projection. Every implementation packet ends at an independently reproducible live gate.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy/MariaDB, pytest, AutoGen AgentChat, Codex CLI JSONL, n8n REST/webhooks, PowerShell launch scripts.

**Global Constraints:**

- Follow `docs/superpowers/specs/2026-07-17-gateway-native-delivery-runtime-design.md` as the binding design.
- Work in the dependency order `gateway prerequisite -> D02 -> D03 -> D04 -> D05`; do not start a downstream packet before its gate is green.
- Add a failing acceptance test before each behavioral change. Use no mocks, fakes, stubs, or synthetic success evidence in Gate A-E tests.
- Unit tests may inject deterministic in-memory ports only where they test pure policy or state transitions; they may never be cited as live-gate evidence.
- A behavioral failure may trigger at most three repair iterations. Infrastructure failures do not consume that budget and must remain distinguishable in Gateway evidence.
- Holdout inputs remain inaccessible to builders until the artifact is sealed.
- Preserve existing worktree changes. Do not edit `README.md`, setup/CI files, existing remediation plans, or the Hermes submodule in D02-D05.
- Use narrow Conventional Commits. Run `git diff --check` before each commit.

---

## Packet 0: Gateway prerequisite contract

### Task 1: Add canonical delivery event envelopes and projections

**Files:**

- Modify: `gateway/contracts.py`
- Modify: `tests/gateway/test_gateway_contracts.py`
- Create: `tests/gateway/test_delivery_events.py`

**Step 1: Write the failing contract tests**

Add table-driven tests that validate these event types through one discriminated envelope: `codex_task`, `codex_session`, `artifact_built`, `deploy`, `validation_run`, `repair_request`, `batch_done`, `e2e_run`, `evaluation`, `release_decision`, and `registry_mirror`.

```python
@pytest.mark.parametrize("event_type", DELIVERY_EVENT_TYPES)
def test_delivery_event_requires_trace_identity(event_type: str) -> None:
    payload = valid_delivery_payload(event_type)
    payload.pop("trace_id")
    with pytest.raises(ValidationError):
        DeliveryEventEnvelope.model_validate(payload)


def test_release_projection_requires_three_distinct_clean_e2e_runs() -> None:
    projection = project_release(candidate_events(clean_runs=2))
    assert projection.releasable is False
    assert projection.blocking_reasons == ["three_clean_e2e_runs_required"]
```

The canonical interfaces are:

```python
class TraceContext(BaseModel):
    project_id: str
    run_id: str
    trace_id: str
    batch_id: str | None = None
    worker_id: str | None = None
    claim_id: str | None = None
    fencing_token: int | None = None
    artifact_id: str | None = None
    session_id: str | None = None
    case_id: str | None = None

class DeliveryEventEnvelope(BaseModel):
    event_id: UUID
    event_type: DeliveryEventType
    occurred_at: datetime
    actor: str
    trace: TraceContext
    payload: DeliveryEventPayload

def project_release(events: Sequence[DeliveryEventEnvelope]) -> ReleaseProjection: ...
```

**Step 2: Verify the tests fail**

Run: `python -m pytest -q tests/gateway/test_delivery_events.py tests/gateway/test_gateway_contracts.py`

Expected: FAIL because the delivery envelope, payload union, and release projection do not exist.

**Step 3: Implement the minimal typed contracts**

Add frozen Pydantic event models, a discriminated payload union, cross-field checks for identifiers required by each event, and a deterministic `project_release`. Preserve the existing batch projection API.

**Step 4: Verify the focused tests pass**

Run: `python -m pytest -q tests/gateway/test_delivery_events.py tests/gateway/test_gateway_contracts.py`

Expected: PASS.

**Step 5: Commit**

```powershell
git add gateway/contracts.py tests/gateway/test_gateway_contracts.py tests/gateway/test_delivery_events.py
git commit -m "feat(gateway): define delivery event contracts"
```

### Task 2: Persist events idempotently and enforce holdout roles

**Files:**

- Modify: `gateway/store.py`
- Modify: `gateway/app.py`
- Modify: `tests/gateway/test_gateway.py`
- Create: `tests/gateway/test_holdout_access.py`

**Step 1: Write failing API and store tests**

Cover idempotent replay by `event_id`, conflict on a changed payload, optimistic fencing rejection, append-only history, role-gated holdout reads, and release projection reads.

```python
def test_builder_cannot_read_holdout_before_artifact_is_sealed(client: TestClient) -> None:
    response = client.get(
        "/v1/projects/p1/runs/r1/holdouts/case-1",
        headers=role_headers("builder"),
    )
    assert response.status_code == 403


def test_duplicate_event_id_with_changed_payload_is_conflict(client: TestClient) -> None:
    assert append_event(client, EVENT).status_code == 201
    assert append_event(client, EVENT | {"payload": changed_payload()}).status_code == 409
```

Add these store/API surfaces:

```python
class GatewayStore:
    def append_delivery_event(self, event: DeliveryEventEnvelope) -> AppendResult: ...
    def delivery_events(self, *, project_id: str, run_id: str) -> tuple[DeliveryEventEnvelope, ...]: ...
    def release_projection(self, *, project_id: str, run_id: str) -> ReleaseProjection: ...

POST /v1/delivery/events
GET  /v1/projects/{project_id}/runs/{run_id}/events
GET  /v1/projects/{project_id}/runs/{run_id}/release
GET  /v1/projects/{project_id}/runs/{run_id}/holdouts/{case_id}
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/gateway/test_delivery_events.py tests/gateway/test_holdout_access.py tests/gateway/test_gateway.py`

Expected: FAIL on missing store and routes.

**Step 3: Implement persistence and authorization**

Use the existing append-only block store and transaction boundary. Store the envelope once, return replay status for byte-equivalent duplicates, reject conflicting duplicates, and permit holdout reads only to the validator role after an `artifact_built` event has sealed the referenced artifact.

**Step 4: Verify Packet 0**

Run: `python -m pytest -q tests/gateway/test_delivery_events.py tests/gateway/test_holdout_access.py tests/gateway/test_gateway.py tests/gateway/test_gateway_auth.py`

Expected: PASS with no skipped tests in these files.

**Step 5: Commit**

```powershell
git add gateway/store.py gateway/app.py tests/gateway/test_gateway.py tests/gateway/test_holdout_access.py
git commit -m "feat(gateway): persist delivery evidence securely"
```

---

## Packet D02: Supervised Codex and Gate A

### Task 3: Enforce Codex execution policy before process launch

**Files:**

- Create: `agenten/execution/codex_policy.py`
- Create: `agenten/execution/codex_events.py`
- Modify: `agenten/execution/codex_supervisor.py`
- Create: `tests/execution/test_codex_policy.py`
- Create: `tests/execution/test_codex_events.py`
- Modify: `tests/execution/test_codex_supervisor.py`

**Step 1: Write failing policy tests**

Test allowed workspace roots, command allowlist, forbidden secret paths, dirty-worktree protection, required trace context, and JSONL event parsing. Include malformed and unknown Codex events as durable warnings rather than crashes.

```python
def test_policy_rejects_workspace_outside_claimed_project(tmp_path: Path) -> None:
    with pytest.raises(CodexPolicyViolation, match="workspace"):
        CodexExecutionPolicy(project_root=tmp_path / "project").authorize(
            CodexRunRequest(workspace=tmp_path / "elsewhere", **request_fields())
        )
```

Define:

```python
class CodexExecutionPolicy:
    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun: ...

def parse_codex_jsonl(line: str) -> CodexProcessEvent | CodexParseWarning: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/execution/test_codex_policy.py tests/execution/test_codex_events.py tests/execution/test_codex_supervisor.py`

Expected: FAIL because policy and parser modules are absent.

**Step 3: Implement policy and parser**

Make authorization mandatory in `CodexSupervisor` before calling its injected `CodexRunner`. Redact environment values by key, never serialize prompt secrets, and map Codex lifecycle output to typed `codex_session` events.

**Step 4: Verify green**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```powershell
git add agenten/execution/codex_policy.py agenten/execution/codex_events.py agenten/execution/codex_supervisor.py tests/execution
git commit -m "feat(execution): guard supervised Codex sessions"
```

### Task 4: Persist and resume real Codex sessions

**Files:**

- Create: `agenten/delivery/codex_runs.py`
- Modify: `agenten/delivery/gateway_client.py`
- Modify: `agenten/execution/codex_supervisor.py`
- Create: `scripts/codex-session.ps1`
- Create: `tests/execution/test_codex_session_recovery.py`
- Modify: `tests/delivery/test_gateway_delivery_client.py`

**Step 1: Write failing recovery tests**

Prove that every started session records its session ID before work, a lost process can be reconciled after restart, cancellation is recorded, and an infrastructure exit is not classified as a behavioral repair.

```python
class CodexRunRepository(Protocol):
    def start(self, request: CodexRunRequest) -> CodexRunRecord: ...
    def append(self, event: CodexProcessEvent) -> None: ...
    def active(self, *, worker_id: str) -> tuple[CodexRunRecord, ...]: ...
    def finish(self, session_id: str, outcome: CodexOutcome) -> None: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/execution/test_codex_session_recovery.py tests/delivery/test_gateway_delivery_client.py`

Expected: FAIL on the missing repository and Gateway methods.

**Step 3: Implement repository, reconciliation, and launcher**

Make `GatewayCodexRunRepository` append typed delivery events via `GatewayDeliveryClient`. The PowerShell script must locate `codex`, use JSONL output, preserve the workspace, return the real exit code, and never echo environment values.

**Step 4: Verify green and script syntax**

Run:

```powershell
python -m pytest -q tests/execution/test_codex_session_recovery.py tests/delivery/test_gateway_delivery_client.py
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/codex-session.ps1))"
```

Expected: PASS and exit code 0.

**Step 5: Commit**

```powershell
git add agenten/delivery/codex_runs.py agenten/delivery/gateway_client.py agenten/execution/codex_supervisor.py scripts/codex-session.ps1 tests
git commit -m "feat(delivery): recover Codex run evidence"
```

### Task 5: Build and validate one real n8n capability (Gate A)

**Files:**

- Create: `agenten/targets/__init__.py`
- Create: `agenten/targets/n8n.py`
- Create: `tests/targets/test_n8n_target.py`
- Create: `tests/live/test_gate_a_codex_n8n.py`

**Step 1: Write failing target contract tests**

Define a typed n8n client port for create/update, activate, execute, and fetch execution evidence. Unit tests cover payload normalization and rejection of success without a matching n8n execution ID.

```python
class N8nTarget:
    async def deploy(self, artifact: SealedArtifact) -> N8nDeployment: ...
    async def execute(self, deployment: N8nDeployment, case: ValidationCase) -> N8nExecutionEvidence: ...
```

**Step 2: Add the initially failing live acceptance test**

`test_gate_a_codex_n8n.py` must require explicit live credentials, start an actual supervised Codex CLI task, create one harmless namespaced workflow in the existing VibeMind-owned n8n instance, execute it, fetch its execution record, and verify the linked Gateway events. Missing infrastructure must skip with a precise reason; it must never substitute a fake client.

**Step 3: Verify red**

Run: `python -m pytest -q tests/targets/test_n8n_target.py`

Expected: FAIL because `N8nTarget` does not exist.

**Step 4: Implement the adapter and pass focused tests**

Run: `python -m pytest -q tests/targets/test_n8n_target.py tests/execution tests/delivery/test_gateway_delivery_client.py`

Expected: PASS.

**Step 5: Run Gate A against real services**

Run: `python -m pytest -q -m live tests/live/test_gate_a_codex_n8n.py -rs`

Expected: `1 passed`; any skip means Gate A is not achieved. Record workflow ID, execution ID, Codex session ID, artifact digest, and Gateway run ID in the test output/evidence record without printing secrets.

**Step 6: Commit only after Gate A passes**

```powershell
git add agenten/targets tests/targets tests/live/test_gate_a_codex_n8n.py
git commit -m "feat(targets): deliver n8n capability through Codex"
```

---

## Packet D03: Restart-safe Hermes worker and Gate B

### Task 6: Define the worker state machine, workspace, and heartbeat

**Files:**

- Create: `agenten/hermes/__init__.py`
- Create: `agenten/hermes/models.py`
- Create: `agenten/hermes/workspace.py`
- Create: `agenten/hermes/heartbeat.py`
- Create: `tests/hermes/test_workspace.py`
- Create: `tests/hermes/test_worker_models.py`

**Step 1: Write failing invariant tests**

Cover legal state transitions, one workspace per claim, path containment, fencing-token propagation, heartbeat expiry, and artifact sealing immutability.

```python
class WorkerPhase(StrEnum):
    CLAIMED = "claimed"
    PLANNING = "planning"
    BUILDING = "building"
    SEALED = "sealed"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    COMPLETED = "completed"
    BLOCKED = "blocked"

class WorkspaceManager:
    def prepare(self, claim: WorkClaim) -> ClaimedWorkspace: ...
    def seal(self, workspace: ClaimedWorkspace) -> SealedArtifact: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/hermes/test_workspace.py tests/hermes/test_worker_models.py`

Expected: FAIL on missing modules.

**Step 3: Implement pure state and filesystem policies**

Do not add orchestration here. Validate every transition and derive artifact digest from sealed content.

**Step 4: Verify green and commit**

```powershell
python -m pytest -q tests/hermes/test_workspace.py tests/hermes/test_worker_models.py
git add agenten/hermes tests/hermes
git commit -m "feat(hermes): define worker lifecycle invariants"
```

### Task 7: Implement the restart-safe worker loop and repair budget

**Files:**

- Create: `agenten/hermes/worker.py`
- Create: `agenten/hermes/recovery.py`
- Modify: `agenten/delivery/gateway_client.py`
- Create: `scripts/run_hermes_worker.py`
- Create: `scripts/resume_hermes_worker.ps1`
- Create: `tests/hermes/test_worker_loop.py`
- Create: `tests/hermes/test_recovery.py`

**Step 1: Write failing state-machine tests**

Test plan-before-build, artifact sealing before holdout access, event-before-side-effect ordering, stale fencing rejection, restart resumption, max three behavioral repairs, and unlimited non-consuming infrastructure retries with bounded backoff.

```python
class HermesWorker:
    async def run_once(self) -> WorkerOutcome: ...
    async def resume(self, claim_id: str) -> WorkerOutcome: ...

class RecoveryPlanner:
    def next_action(self, history: Sequence[DeliveryEventEnvelope]) -> RecoveryAction: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/hermes/test_worker_loop.py tests/hermes/test_recovery.py`

Expected: FAIL.

**Step 3: Implement the minimal worker composition**

Inject Gateway client, Codex supervisor, target, validator, clock, and backoff. Persist intent before external side effects and result afterward. On restart, reconcile incomplete intent/result pairs instead of repeating blindly.

**Step 4: Verify green and launch scripts**

```powershell
python -m pytest -q tests/hermes/test_worker_loop.py tests/hermes/test_recovery.py tests/execution tests/targets/test_n8n_target.py
python -m compileall -q agenten/hermes scripts/run_hermes_worker.py
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/resume_hermes_worker.ps1))"
```

Expected: PASS.

**Step 5: Commit**

```powershell
git add agenten/hermes agenten/delivery/gateway_client.py scripts/run_hermes_worker.py scripts/resume_hermes_worker.ps1 tests/hermes
git commit -m "feat(hermes): run restart-safe delivery claims"
```

### Task 8: Prove crash recovery on real infrastructure (Gate B)

**Files:**

- Create: `tests/live/test_gate_b_hermes_recovery.py`

**Step 1: Write the live failure-injection test**

The test must start a real worker subprocess, wait until Gateway shows a real Codex/n8n side effect, terminate the worker process, restart it through `resume_hermes_worker.ps1`, and prove exactly one completed artifact and deployment for the claim.

**Step 2: Run Gate B**

Run: `python -m pytest -q -m live tests/live/test_gate_b_hermes_recovery.py -rs`

Expected: `1 passed`. A skip or a manually edited artifact is not a pass.

**Step 3: Inspect evidence invariants**

Query Gateway and assert one claim, monotonic fencing tokens, one sealed artifact digest, linked pre-crash and post-restart session IDs, and no duplicate n8n workflow side effect.

**Step 4: Commit**

```powershell
git add tests/live/test_gate_b_hermes_recovery.py
git commit -m "test(hermes): prove live worker crash recovery"
```

---

## Packet D04: AutoGen composition, validation, and Gate C

### Task 9: Add sealed-artifact validation and hidden holdouts

**Files:**

- Create: `agenten/validation/static_gate.py`
- Create: `agenten/validation/contract_gate.py`
- Create: `agenten/validation/holdout_gate.py`
- Create: `agenten/validation/e2e.py`
- Create: `tests/validation/test_static_gate.py`
- Create: `tests/validation/test_contract_gate.py`
- Create: `tests/validation/test_holdout_gate.py`

**Step 1: Write failing layer tests**

Prove ordering `static -> contract -> holdout -> e2e`, sealed-digest verification, builder denial, validator-only retrieval, case-level trace IDs, and failure classification.

```python
class ValidationPipeline:
    async def validate(self, artifact: SealedArtifact, suite: ValidationSuite) -> ValidationReport: ...

class HoldoutGate:
    async def evaluate(self, artifact: SealedArtifact, case_id: str) -> CaseResult: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/validation/test_static_gate.py tests/validation/test_contract_gate.py tests/validation/test_holdout_gate.py`

Expected: FAIL.

**Step 3: Implement ordered validation**

Every layer appends `validation_run` evidence. Stop downstream layers on prerequisite failure. Never place holdout bodies in builder prompts or general logs.

**Step 4: Verify green and commit**

```powershell
python -m pytest -q tests/validation
git add agenten/validation tests/validation
git commit -m "feat(validation): enforce sealed hidden holdouts"
```

### Task 10: Deliver three real n8n capabilities

**Files:**

- Modify: `agenten/targets/n8n.py`
- Modify: `tests/targets/test_n8n_target.py`
- Create: `tests/live/test_n8n_capability_suite.py`

**Step 1: Add failing capability-suite tests**

Parameterize three distinct capability specifications and assert deterministic workflow naming, activation, webhook/execution correlation, output schema, cleanup metadata, and artifact digest linkage.

**Step 2: Verify red**

Run: `python -m pytest -q tests/targets/test_n8n_target.py`

Expected: FAIL until multi-capability deployment is supported.

**Step 3: Implement capability deployment without target-specific orchestration**

Keep n8n API mechanics in `N8nTarget`; keep iteration and repair decisions in `HermesWorker`.

**Step 4: Run unit and live tests**

```powershell
python -m pytest -q tests/targets/test_n8n_target.py
python -m pytest -q -m live tests/live/test_n8n_capability_suite.py -rs
```

Expected: all three live cases pass with real execution IDs.

**Step 5: Commit**

```powershell
git add agenten/targets/n8n.py tests/targets/test_n8n_target.py tests/live/test_n8n_capability_suite.py
git commit -m "feat(targets): deliver n8n capability suite"
```

### Task 11: Generate and run an isolated AutoGen team

**Files:**

- Create: `agenten/targets/autogen.py`
- Create: `tests/targets/test_autogen_target.py`
- Create: `tests/live/test_autogen_isolated_team.py`

**Step 1: Write failing target tests**

Cover generated role definitions, bounded conversation termination, allowed-tool assignment, model endpoint selection from configuration, transcript evidence, and isolation from holdout data.

```python
class AutoGenTarget:
    async def build(self, specification: TeamSpecification) -> SealedArtifact: ...
    async def execute(self, artifact: SealedArtifact, task: TeamTask) -> AutoGenRunEvidence: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/targets/test_autogen_target.py`

Expected: FAIL.

**Step 3: Implement using current AgentChat APIs**

Use injected model-client configuration, explicit termination conditions, typed tool adapters, and durable transcript references. Do not import legacy `pyautogen` APIs.

**Step 4: Verify unit and isolated live execution**

```powershell
python -m pytest -q tests/targets/test_autogen_target.py
python -m pytest -q -m live tests/live/test_autogen_isolated_team.py -rs
```

Expected: the live team completes one bounded task and produces linked transcript evidence.

**Step 5: Commit**

```powershell
git add agenten/targets/autogen.py tests/targets/test_autogen_target.py tests/live/test_autogen_isolated_team.py
git commit -m "feat(targets): generate bounded AutoGen teams"
```

### Task 12: Evaluate the composed system and pass Gate C

**Files:**

- Create: `agenten/evaluation/models.py`
- Create: `agenten/evaluation/runner.py`
- Create: `tests/evaluation/test_runner.py`
- Create: `tests/live/test_gate_c_composed_delivery.py`

**Step 1: Write failing evaluator tests**

Score functional correctness, contract compliance, holdout results, repair count, trace completeness, and real-evidence provenance. Reject evaluation when any required external execution ID is absent.

```python
class EvaluationRunner:
    async def evaluate(self, run_id: str, rubric: EvaluationRubric) -> EvaluationResult: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/evaluation/test_runner.py`

Expected: FAIL.

**Step 3: Implement Gateway-derived evaluation**

Read evidence through the Gateway client only. Append one `evaluation` event and never mutate source evidence.

**Step 4: Run Gate C**

Run: `python -m pytest -q -m live tests/live/test_gate_c_composed_delivery.py -rs`

Expected: a real Hermes run builds three n8n tools and one AutoGen team, passes hidden holdouts and composed E2E validation, and finishes within three behavioral repair iterations.

**Step 5: Commit**

```powershell
git add agenten/evaluation tests/evaluation tests/live/test_gate_c_composed_delivery.py
git commit -m "feat(evaluation): score composed delivery evidence"
```

---

## Packet D05: Fleet, registry, learning, and release

### Task 13: Add fenced fleet claims and provisioning

**Files:**

- Create: `agenten/hermes/fleet.py`
- Create: `scripts/provision_hermes_worker.py`
- Create: `tests/hermes/test_fleet.py`
- Create: `tests/live/test_gate_d_worker_fleet.py`

**Step 1: Write failing concurrency tests**

Prove unique claims, lease renewal, stale-worker fencing, capacity limits, worker labels, and safe reprovisioning.

```python
class HermesFleet:
    async def provision(self, specification: WorkerSpecification) -> WorkerIdentity: ...
    async def claim_next(self, worker: WorkerIdentity) -> WorkClaim | None: ...
    async def reconcile(self) -> FleetProjection: ...
```

**Step 2: Verify red, implement, and verify green**

```powershell
python -m pytest -q tests/hermes/test_fleet.py
python -m compileall -q agenten/hermes scripts/provision_hermes_worker.py
```

Expected after implementation: PASS.

**Step 3: Run Gate D**

Run: `python -m pytest -q -m live tests/live/test_gate_d_worker_fleet.py -rs`

Expected: multiple real worker processes complete independent claims without duplicate ownership or stale writes.

**Step 4: Commit**

```powershell
git add agenten/hermes/fleet.py scripts/provision_hermes_worker.py tests/hermes/test_fleet.py tests/live/test_gate_d_worker_fleet.py
git commit -m "feat(hermes): coordinate fenced worker fleet"
```

### Task 14: Project registry state and learn only from useful failures

**Files:**

- Create: `agenten/hermes/learning.py`
- Create: `agenten/delivery/projector.py`
- Create: `agenten/delivery/minibook_client.py`
- Create: `agenten/registry/models.py`
- Create: `agenten/registry/client.py`
- Create: `tests/hermes/test_learning.py`
- Create: `tests/delivery/test_registry_projection.py`

**Step 1: Write failing selection and projection tests**

Select only outputs that are erroneous, low-scoring, contradictory, policy-violating, or semantically useless. Explicitly exclude clean successes and infrastructure outages. Prove Minibook receives a projection, cannot become release authority, and repeated projection is idempotent.

```python
class LearningSelector:
    def select(self, evaluation: EvaluationResult) -> tuple[LearningExample, ...]: ...

class RegistryProjector:
    async def project(self, release: ReleaseProjection) -> RegistryMirrorResult: ...
```

**Step 2: Verify red**

Run: `python -m pytest -q tests/hermes/test_learning.py tests/delivery/test_registry_projection.py`

Expected: FAIL.

**Step 3: Implement selective learning and read-only projection**

Store references and classifications in Gateway evidence; do not copy secrets, raw holdouts, or unrestricted transcripts into learning records. Append `registry_mirror` only after Minibook acknowledges the projection.

**Step 4: Verify green and commit**

```powershell
python -m pytest -q tests/hermes/test_learning.py tests/delivery/test_registry_projection.py
git add agenten/hermes/learning.py agenten/delivery/projector.py agenten/delivery/minibook_client.py agenten/registry tests
git commit -m "feat(registry): project releases and select failures"
```

### Task 15: Prove release readiness with three clean E2E runs (Gate E)

**Files:**

- Create: `tests/live/test_gate_e_release_decision.py`

**Step 1: Write the live release acceptance test**

Run three fresh composed E2E cases with distinct `run_id` values against real Codex, n8n, AutoGen, Gateway/MariaDB, and Minibook endpoints. Assert each is clean, trace-complete, and independently reproducible.

**Step 2: Prove negative release cases first**

Assert release remains blocked after two clean runs, after a third run with incomplete evidence, and after a run whose artifact digest differs from its validation digest.

**Step 3: Run Gate E**

Run: `python -m pytest -q -m live tests/live/test_gate_e_release_decision.py -rs`

Expected: PASS with one Gateway-authored `release_decision`, three distinct clean `e2e_run` records, and an acknowledged `registry_mirror`. A skip is not a pass.

**Step 4: Run the full packet verification**

```powershell
python -m pytest -q -m "not live"
python -m pytest -q -m live tests/live/test_gate_a_codex_n8n.py tests/live/test_gate_b_hermes_recovery.py tests/live/test_gate_c_composed_delivery.py tests/live/test_gate_d_worker_fleet.py tests/live/test_gate_e_release_decision.py -rs
python scripts/verify_submission.py
python main.py demo --output artifacts/gateway-native-delivery-demo.json
python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
python -m compileall -q agenten blockchain chats config gateway
git diff --check
```

Expected:

- All non-live tests pass; report every skip and warning separately.
- Gates A-E all pass with no skips.
- Submission verification passes.
- Demo exits 0 and writes a new intentionally named artifact; do not overwrite `artifacts/demo-run.json`.
- Architecture/import tests and compileall pass.

**Step 5: Audit release evidence manually**

For the release run, verify that Gateway contains a continuous chain:

```text
codex_task -> codex_session -> artifact_built -> deploy -> validation_run
-> e2e_run -> evaluation -> batch_done -> release_decision -> registry_mirror
```

Confirm all identifiers correlate, no holdout leaked before sealing, behavioral repairs are `<= 3`, and every external success has a real provider execution/session ID.

**Step 6: Commit**

```powershell
git add tests/live/test_gate_e_release_decision.py artifacts/gateway-native-delivery-demo.json
git commit -m "test(delivery): prove gateway-native release gate"
```

---

## Integration and review order

1. Merge the Gateway prerequisite packet after its focused tests pass.
2. Rebase D02 onto that packet; merge only after Gate A passes live.
3. Rebase D03 onto D02; merge only after Gate B passes live.
4. Rebase D04 onto D03; merge only after Gate C passes live.
5. Rebase D05 onto D04; merge only after Gates D and E pass live.
6. Before each integration, simulate the merge and inspect overlap in `README.md`, `.env.example`, `requirements.txt`, and `main.py`; D02-D05 should not edit those files.
7. Request code review per packet. Resolve findings with new focused tests and narrow commits; never amend evidence to appear green.

## Completion definition

The runtime is complete only when the current code, not merely this plan, satisfies all of the following:

- MariaDB/Ledger Gateway is the sole production delivery authority.
- A real Codex session produces a real n8n capability with linked evidence.
- A killed Hermes worker resumes without duplicate side effects.
- Three n8n capabilities and one generated AutoGen team pass sealed hidden-holdout evaluation.
- Multiple workers operate under leases and fencing without duplicate claims.
- Only poor or invalid outputs become learning examples; infrastructure faults do not.
- Minibook mirrors the Gateway release registry without deciding release.
- Three consecutive clean real E2E runs produce the release decision.
- No Gate A-E result relies on mocks, fakes, stubs, synthetic provider IDs, or manual artifact edits.
