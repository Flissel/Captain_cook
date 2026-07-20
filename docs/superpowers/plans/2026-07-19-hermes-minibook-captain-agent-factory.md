# Hermes–Minibook–Captain Agent Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Captain-governed lifecycle that reuses a validated AutoGen team on a capability hit and, on a capability miss, drives four Hermes factory roles plus the existing Minibook SwarmPipeline through creation, typed n8n tool integration, five bounded improvement rounds, validation blocks, and `ready_to_use` promotion.

**Architecture:** Add a focused `agenten/agent_factory/` domain package on top of the existing Agent Runtime Control Plane. Captain and the MariaDB gateway own immutable lifecycle blocks, leases, holdouts, iteration limits, and capability promotion; Hermes and Minibook Forge remain independent processes behind typed ports and versioned JSON envelopes. Minibook consumes redacted projections, and generated AutoGen teams receive only Captain-promoted typed n8n tools.

**Tech Stack:** Python 3.11, Pydantic 2, FastAPI, MariaDB 11.8, AutoGen Core/AgentChat/Ext 0.7.5, Hermes Agent, Codex CLI/app-server runtime, Context7 MCP, external VibeMind n8n MCP/API, Minibook FastAPI/SQLite projection, pytest.

## Global Constraints

- Execute from a new isolated worktree based on the latest approved branch containing `codex/agent-runtime-architecture` commit `deea2a4` or its verified descendant; do not implement in the dirty `feat/householder-runtime` checkout.
- Bring design commit `cef9222` into that worktree without merging unrelated `feat/householder-runtime` code.
- Re-run `git fetch origin --prune`, `git worktree list --porcelain`, dirty-state checks, and ancestry checks immediately before creating the implementation branch.
- Treat Captain Core, Minibook, Minibook Forge, and Hermes/Codex/n8n as independent work packages with no shared database, process memory, credentials, or internal runtime imports.
- Only `gateway/` may open a MariaDB connection. Captain domain code uses injected ports or authenticated HTTP clients.
- Minibook is a replayable projection and discussion surface, never lifecycle authority.
- Keep the existing `minibook/swarm/SwarmPipeline` algorithm behind a versioned job boundary; do not import it from Captain.
- Use four persistent Hermes roles: `agent_architect`, `tool_integrator`, `real_case_tester`, and `quality_warden`.
- All Hermes roles discover the same skills and tool catalog; Captain-issued short-lived leases enforce callable and mutating capabilities.
- Pin supported AutoGen behavior to 0.7.5. Context7 evidence must record library ID, query timestamp, returned source references, version, and content digest.
- Generated AutoGen teams receive one typed tool per promoted n8n workflow; never expose a generic workflow-ID execution tool.
- VibeMind owns n8n and its volumes. Never start, stop, migrate, adopt, or delete that deployment or its volumes.
- `input.md` is an opaque content-addressed input artifact in this plan. Its domain schema is a separate approved design before implementing an input parser.
- Five behavioral iterations are the absolute ceiling. Infrastructure failures do not consume an iteration; a behavioral failure does.
- `CapabilityPromoted` is the only transition that creates a reusable `ready_to_use` catalog record.
- Never place credentials, raw holdouts, private prompts, unrestricted paths, complete transcripts, or `.env` contents in blocks, projections, fixtures, logs, or commits.
- A mocked, skipped, unavailable, or synthesized live dependency cannot satisfy a live gate.
- Add a failing acceptance test before each behavioral implementation and preserve the deterministic offline path.

## Program routing and ownership

| Work package | Exclusive write scope | Deliverable gate |
| --- | --- | --- |
| WP-A Captain contracts/lifecycle | `agenten/agent_factory/**`, related Captain tests | pure state transitions, five-round ceiling, promotion authority |
| WP-B Gateway authority | `gateway/factory_*.py`, narrow additions to `gateway/app.py` and `gateway/store.py`, gateway tests | real MariaDB idempotency, fencing, immutable block projection |
| WP-C Minibook Forge | `minibook/swarm/contracts.py`, `job_store.py`, `service.py`, `api.py`, Minibook Forge tests | job create/status/cancel/result and resumable checkpoints |
| WP-D Hermes factory | Hermes submodule `agent_factory/**`, factory skill, templates, Hermes tests | four real role runners, Context7 evidence, Codex/n8n adapters |
| WP-E Typed n8n tools | `agenten/agent_factory/tool_gap.py`, `typed_tools.py`, `agenten/targets/n8n.py`, tests | promoted version-bound AutoGen FunctionTools with real execution evidence |
| WP-F Projection/integration | `agenten/delivery/minibook_events.py`, `projector.py`, cross-package/live tests | replayable Minibook view and one zero-skip live lifecycle |

## File structure

### Captain Agent Factory

- `agenten/agent_factory/contracts.py` — immutable cross-package factory commands, evidence blocks, projections, and capability records.
- `agenten/agent_factory/ports.py` — catalog, gateway, Hermes, Forge, validator, and clock protocols.
- `agenten/agent_factory/state_machine.py` — pure next-transition rules and five-round ceiling.
- `agenten/agent_factory/service.py` — one-transition-at-a-time orchestration and idempotent dispatch.
- `agenten/agent_factory/tool_gap.py` — deterministic reuse/native-n8n/typed-workflow/local-tool decision policy.
- `agenten/agent_factory/typed_tools.py` — promoted n8n tool schema and AutoGen FunctionTool adapter.

### Gateway authority

- `gateway/factory_projection.py` — derive current factory/job/capability state from immutable blocks.
- `gateway/factory_routes.py` — authenticated factory job, block, projection, and capability endpoints.
- `gateway/app.py` — include the factory router only.
- `gateway/store.py` — narrow transactional append/query methods using the existing storage layer.

### Minibook Forge boundary

- `minibook/swarm/contracts.py` — `CreationJobV1`, `CreationProgressV1`, and `CreationResultV1`.
- `minibook/swarm/job_store.py` — SQLite-backed job/checkpoint state owned by Minibook Forge.
- `minibook/swarm/service.py` — adapter around the existing `SwarmPipeline`.
- `minibook/swarm/api.py` — create/status/cancel/result HTTP API, started independently from collaboration-only Minibook.

### Hermes factory

- `hermes-agent/skills/autonomous-ai-agents/autogen-team-factory/SKILL.md` — shared process used by all four roles.
- `hermes-agent/skills/autonomous-ai-agents/autogen-team-factory/references/*.md` — AutoGen policy, role contract, tool-gap decision tree, evidence rules.
- `hermes-agent/skills/autonomous-ai-agents/autogen-team-factory/templates/*.md` — exact role system/user prompt templates.
- `hermes-agent/agent_factory/contracts.py` — Hermes-side validation of Captain envelopes.
- `hermes-agent/agent_factory/roles.py` — role catalog and prompt rendering.
- `hermes-agent/agent_factory/context7.py` — sanitized documentation evidence capture.
- `hermes-agent/agent_factory/worker.py` — bounded role execution using existing Hermes Codex/MCP runtime surfaces.
- `hermes-agent/agent_factory/cli.py` — JSONL command/result entry point.

---

### Task 1: Freeze Agent Factory contracts and fixtures

**Files:**
- Create: `agenten/agent_factory/__init__.py`
- Create: `agenten/agent_factory/contracts.py`
- Create: `tests/agent_factory/test_contracts.py`
- Create: `tests/fixtures/contracts/agent_factory_job.v1.json`
- Create: `tests/fixtures/contracts/agent_factory_block.v1.json`
- Modify: `tests/test_architecture_fitness.py:76-80`

**Interfaces:**
- Consumes: `agenten.agent_runtime.contracts.ArtifactRef`, UUID/RFC3339 envelope conventions.
- Produces: `AgentFactoryJob`, `FactoryEvidenceBlock`, `FactoryProjection`, `PromotedCapability`, `FactoryRole`, `FactoryPhase`, and `FactoryBlockStatus`.

- [ ] **Step 1: Write failing strict-contract tests**

```python
def test_factory_job_is_strict_and_fixed_to_five_iterations() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    job = AgentFactoryJob.model_validate(payload)
    assert job.schema_name == "captain.agent-factory-job.v1"
    assert job.max_behavioral_iterations == 5
    with pytest.raises(ValidationError):
        AgentFactoryJob.model_validate({**payload, "unknown": True})


def test_promoted_capability_requires_promotion_block() -> None:
    with pytest.raises(ValidationError, match="promotion block"):
        PromotedCapability.model_validate({
            "capability_id": "support_triage",
            "version": 1,
            "status": "ready_to_use",
            "blueprint_ref": artifact("blueprint"),
            "code_ref": artifact("code"),
            "tool_refs": [],
            "promotion_block_ref": None,
        })
```

- [ ] **Step 2: Run the contract tests and verify collection fails**

Run: `python -m pytest tests/agent_factory/test_contracts.py -q`

Expected: FAIL during import because `agenten.agent_factory.contracts` does not exist.

- [ ] **Step 3: Implement the immutable contract vocabulary**

```python
class FactoryRole(str, Enum):
    AGENT_ARCHITECT = "agent_architect"
    TOOL_INTEGRATOR = "tool_integrator"
    REAL_CASE_TESTER = "real_case_tester"
    QUALITY_WARDEN = "quality_warden"


class FactoryPhase(str, Enum):
    FORGE_REQUESTED = "forge_requested"
    BLUEPRINT_CREATED = "blueprint_created"
    TOOL_CANDIDATE_TESTED = "tool_candidate_tested"
    AGENT_CODE_CREATED = "agent_code_created"
    BUILD_PASSED = "build_passed"
    BUILD_FAILED = "build_failed"
    REAL_CASE_EVIDENCE = "real_case_evidence"
    IMPROVEMENT_REQUESTED = "improvement_requested"
    CAPABILITY_PROMOTED = "capability_promoted"
    ESCALATED = "escalated"


class AgentFactoryJob(_FrozenContract):
    schema_name: Literal["captain.agent-factory-job.v1"] = "captain.agent-factory-job.v1"
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None
    occurred_at: datetime
    producer: Literal["captain"] = "captain"
    job_id: UUID
    subject_version: int = Field(ge=1)
    input_ref: ArtifactRef
    required_capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    max_behavioral_iterations: Literal[5] = 5
```

Implement `FactoryEvidenceBlock` with unique assertion/evidence references, attempt `1..5`, optional `lease_id`, and phase/role consistency validation. Implement `PromotedCapability` so `ready_to_use` always carries a `promotion_block_ref`.

- [ ] **Step 4: Add canonical fixtures and architecture isolation**

Add complete JSON fixtures containing only opaque `artifact://sha256/...` references. Extend the architecture fitness rule so `agenten.agent_factory` cannot import `minibook`, `hermes_agent`, `hermes-agent`, or concrete gateway storage modules.

- [ ] **Step 5: Run focused validation**

Run: `python -m pytest tests/agent_factory/test_contracts.py tests/test_architecture_fitness.py -q`

Expected: all tests PASS; fixture round trips are byte-stable under canonical serialization.

- [ ] **Step 6: Commit WP-A contract freeze**

```powershell
git add agenten/agent_factory tests/agent_factory/test_contracts.py tests/fixtures/contracts/agent_factory_job.v1.json tests/fixtures/contracts/agent_factory_block.v1.json tests/test_architecture_fitness.py
git commit -m "feat(factory): freeze lifecycle contracts"
```

### Task 2: Implement the pure five-round lifecycle state machine

**Files:**
- Create: `agenten/agent_factory/state_machine.py`
- Create: `tests/agent_factory/test_state_machine.py`

**Interfaces:**
- Consumes: Task 1 contracts.
- Produces: `FactoryAction`, `next_action(projection: FactoryProjection) -> FactoryAction`, and `apply_block(projection, block) -> FactoryProjection`.

- [ ] **Step 1: Write transition-table tests**

```python
@pytest.mark.parametrize(
    ("phase", "status", "expected"),
    [
        (None, "pending", "dispatch_agent_architect"),
        ("blueprint_created", "running", "dispatch_tool_integrator"),
        ("build_passed", "running", "dispatch_real_case_tester"),
        ("real_case_evidence", "running", "dispatch_quality_warden"),
        ("capability_promoted", "ready_to_use", "complete"),
        ("escalated", "escalated", "complete"),
    ],
)
def test_next_action_is_deterministic(phase, status, expected):
    assert next_action(projection(phase=phase, status=status)).kind == expected


def test_fifth_behavioral_failure_escalates() -> None:
    state = projection(attempt=5, phase="real_case_evidence")
    failed = evidence_block(phase="improvement_requested", attempt=5, status="failed")
    result = apply_block(state, failed)
    assert result.status == "escalated"
    assert next_action(result).kind == "append_escalated"


def test_infrastructure_failure_does_not_increment_attempt() -> None:
    state = projection(attempt=2)
    result = apply_block(state, infrastructure_block(attempt=2))
    assert result.attempt == 2
```

- [ ] **Step 2: Verify the tests fail before implementation**

Run: `python -m pytest tests/agent_factory/test_state_machine.py -q`

Expected: FAIL because `FactoryAction`, `next_action`, and `apply_block` are missing.

- [ ] **Step 3: Implement a closed transition table**

```python
class FactoryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[
        "dispatch_agent_architect",
        "dispatch_tool_integrator",
        "submit_forge_job",
        "dispatch_real_case_tester",
        "dispatch_quality_warden",
        "append_escalated",
        "validate_for_promotion",
        "complete",
        "wait_infrastructure",
    ]
    attempt: int = Field(ge=1, le=5)


def next_action(projection: FactoryProjection) -> FactoryAction:
    key = (projection.phase, projection.status)
    try:
        kind = _TRANSITIONS[key]
    except KeyError as exc:
        raise FactoryTransitionError(f"illegal factory state: {key!r}") from exc
    return FactoryAction(kind=kind, attempt=projection.attempt)
```

Keep `apply_block` pure: verify matching job/correlation/subject version, reject duplicate phase data with a different digest, and increment the attempt only for a behavioral `ImprovementRequested` block.

- [ ] **Step 4: Run transition and property tests**

Run: `python -m pytest tests/agent_factory/test_state_machine.py -q`

Expected: PASS, including illegal transition, duplicate replay, stale-version, regression, and iteration-ceiling cases.

- [ ] **Step 5: Commit the lifecycle domain**

```powershell
git add agenten/agent_factory/state_machine.py tests/agent_factory/test_state_machine.py
git commit -m "feat(factory): enforce bounded lifecycle transitions"
```

### Task 3: Persist factory blocks and catalog state through the gateway

**Files:**
- Create: `gateway/factory_projection.py`
- Create: `gateway/factory_routes.py`
- Modify: `gateway/store.py:97-138`
- Modify: `gateway/app.py:97-158`
- Create: `tests/gateway/test_factory_projection.py`
- Create: `tests/gateway/test_factory_routes.py`
- Create: `tests/gateway/test_factory_mariadb.py`

**Interfaces:**
- Consumes: `AgentFactoryJob`, `FactoryEvidenceBlock`, `FactoryProjection`, and existing `MariaDBStorage` transaction behavior.
- Produces: `GatewayStore.accept_factory_job`, `append_factory_block`, `factory_projection`, `find_ready_capability`; HTTP endpoints under `/v1/factory`.

- [ ] **Step 1: Write projection and HTTP acceptance tests**

```python
def test_capability_is_invisible_before_promotion(client, job_headers) -> None:
    create_job(client, job_headers)
    append_green_blocks_except_promotion(client, job_headers)
    response = client.get("/v1/factory/capabilities/support_triage", headers=job_headers)
    assert response.status_code == 404


def test_replay_is_idempotent_but_changed_data_conflicts(client, job_headers) -> None:
    block = factory_block("blueprint_created")
    first = client.post("/v1/factory/blocks", json=block, headers=job_headers)
    replay = client.post("/v1/factory/blocks", json=block, headers=job_headers)
    changed = client.post(
        "/v1/factory/blocks",
        json={**block, "artifact_refs": [artifact_ref("different")]},
        headers=job_headers,
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert changed.status_code == 409
```

- [ ] **Step 2: Verify the new routes fail**

Run: `python -m pytest tests/gateway/test_factory_projection.py tests/gateway/test_factory_routes.py -q`

Expected: FAIL with 404 for `/v1/factory/jobs` and missing projection imports.

- [ ] **Step 3: Implement projection and narrow store methods**

```python
def project_factory(job: AgentFactoryJob, blocks: Sequence[FactoryEvidenceBlock]) -> FactoryProjection:
    state = FactoryProjection.from_job(job)
    for block in sorted(blocks, key=lambda item: (item.attempt, item.occurred_at, str(item.event_id))):
        state = apply_block(state, block)
    return state


class FactoryRouterStore(Protocol):
    def accept_factory_job(self, job: AgentFactoryJob) -> AppendResult: ...
    def append_factory_block(self, block: FactoryEvidenceBlock) -> AppendResult: ...
    def factory_projection(self, job_id: UUID) -> FactoryProjection: ...
    def find_ready_capability(self, capability_id: str) -> PromotedCapability | None: ...
```

Store factory jobs as root blocks and evidence as immutable children in one MariaDB transaction. Reuse the existing canonical idempotency and retry helpers; do not add a second connection pool.

- [ ] **Step 4: Mount authenticated routes**

Implement:

```text
POST /v1/factory/jobs
POST /v1/factory/blocks
GET  /v1/factory/jobs/{job_id}
GET  /v1/factory/capabilities/{capability_id}
```

Require Captain writer credentials for jobs/promotion, role-specific worker credentials for role blocks, and read credentials for projections. `QualityWarden` may recommend promotion but cannot write `CAPABILITY_PROMOTED`.

- [ ] **Step 5: Prove real MariaDB fencing and replay behavior**

Run: `python -m pytest tests/gateway/test_factory_projection.py tests/gateway/test_factory_routes.py tests/gateway/test_factory_mariadb.py -q`

Expected: PASS with MariaDB available. If `TEST_MARIADB_DSN` is absent, the unit suites pass and the MariaDB file is reported as a required skip; the package gate remains incomplete.

- [ ] **Step 6: Commit WP-B**

```powershell
git add gateway/factory_projection.py gateway/factory_routes.py gateway/store.py gateway/app.py tests/gateway/test_factory_projection.py tests/gateway/test_factory_routes.py tests/gateway/test_factory_mariadb.py
git commit -m "feat(gateway): persist agent factory lifecycle"
```

### Task 4: Add capability lookup and one-transition orchestration

**Files:**
- Create: `agenten/agent_factory/ports.py`
- Create: `agenten/agent_factory/service.py`
- Create: `agenten/agent_factory/gateway_client.py`
- Modify: `agenten/agent_runtime/contracts.py:48-53`
- Modify: `agenten/agent_runtime/capabilities.py:20-51`
- Create: `tests/agent_factory/test_service.py`
- Create: `tests/agent_factory/test_gateway_client.py`
- Modify: `tests/agent_runtime/test_capabilities.py`

**Interfaces:**
- Consumes: Tasks 1–3 contracts/routes; existing `ArtifactPort`, `AgentRuntimeService`, and opaque artifact references.
- Produces: `AgentFactoryService.resolve_or_create`, `advance`, `FactoryResolution`, `CreationProgressView`, `CreationResultView`, `FactoryGatewayPort`, `HermesFactoryPort`, `MinibookForgePort`, and `FactoryValidatorPort`.

- [ ] **Step 1: Write capability-hit and capability-miss tests**

```python
@pytest.mark.asyncio
async def test_capability_hit_reuses_without_starting_forge() -> None:
    catalog = FakeCatalog(promoted=promoted_capability("support_triage"))
    forge = AsyncMock()
    service = factory_service(catalog=catalog, forge=forge)
    decision = await service.resolve_or_create(factory_request("support_triage"))
    assert decision.kind == "reuse"
    assert decision.capability.status == "ready_to_use"
    forge.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_capability_miss_creates_exactly_one_job() -> None:
    gateway = FakeFactoryGateway()
    service = factory_service(catalog=FakeCatalog(promoted=None), gateway=gateway)
    first = await service.resolve_or_create(factory_request("support_triage"))
    replay = await service.resolve_or_create(factory_request("support_triage"))
    assert first.job_id == replay.job_id
    assert gateway.accepted_job_count == 1
```

- [ ] **Step 2: Verify the service tests fail**

Run: `python -m pytest tests/agent_factory/test_service.py tests/agent_factory/test_gateway_client.py -q`

Expected: FAIL because the ports, service, and client do not exist.

- [ ] **Step 3: Define the independent process ports**

```python
class HermesFactoryPort(Protocol):
    async def run_role(
        self,
        *,
        job: AgentFactoryJob,
        role: FactoryRole,
        attempt: int,
        lease: CapabilityGrant,
        prompt_ref: ArtifactRef,
    ) -> FactoryEvidenceBlock: ...


class MinibookForgePort(Protocol):
    async def create(self, job: AgentFactoryJob, blueprint_ref: ArtifactRef) -> UUID: ...
    async def status(self, creation_job_id: UUID) -> CreationProgressView: ...
    async def result(self, creation_job_id: UUID) -> CreationResultView: ...


class FactoryValidatorPort(Protocol):
    async def validate_for_promotion(self, projection: FactoryProjection) -> FactoryEvidenceBlock: ...
```

Define the transport-neutral views before the ports:

```python
class CreationProgressView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    creation_job_id: UUID
    checkpoint: str
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]


class CreationResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    creation_job_id: UUID
    artifact_refs: tuple[ArtifactRef, ...]
    evidence_refs: tuple[ArtifactRef, ...]


class FactoryResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["reuse", "created"]
    job_id: UUID | None = None
    capability: PromotedCapability | None = None
```

- [ ] **Step 4: Extend role-specific capability profiles**

Add `FACTORY_ARCHITECT`, `FACTORY_TOOL_INTEGRATOR`, `FACTORY_REAL_CASE_TESTER`, and `FACTORY_QUALITY_WARDEN` to the existing `CapabilityProfile` enum. Extend `PROFILE_CAPABILITIES` so all four roles share discovery/read capabilities while only the integrator receives Codex/repository/n8n definition mutations and only the tester receives real-case/n8n execution. Add tests proving a visible but unleased tool fails `validate_grant`.

- [ ] **Step 5: Implement one action per `advance` call**

`advance(job_id)` must load the authoritative projection, call `next_action`, execute at most one external action, and append exactly one resulting block. It must never loop across multiple external systems inside one transaction.

```python
async def advance(self, job_id: UUID) -> FactoryProjection:
    current = await self._gateway.projection(job_id)
    action = next_action(current)
    block = await self._dispatch(action, current)
    if block is not None:
        await self._gateway.append_block(block)
    return await self._gateway.projection(job_id)
```

- [ ] **Step 6: Implement authenticated gateway serialization**

Follow `agenten/agent_runtime/gateway_client.py`: strict Pydantic serialization, finite timeout, no secret-bearing exception bodies, and explicit mapping of HTTP 409 to replay/stale-version errors.

- [ ] **Step 7: Run focused tests**

Run: `python -m pytest tests/agent_factory/test_service.py tests/agent_factory/test_gateway_client.py tests/agent_runtime/test_capabilities.py -q`

Expected: PASS; external ports are called once, replays are idempotent, and a stale projection fails closed before dispatch.

- [ ] **Step 8: Commit WP-A orchestration**

```powershell
git add agenten/agent_factory/ports.py agenten/agent_factory/service.py agenten/agent_factory/gateway_client.py agenten/agent_runtime/contracts.py agenten/agent_runtime/capabilities.py tests/agent_factory/test_service.py tests/agent_factory/test_gateway_client.py tests/agent_runtime/test_capabilities.py
git commit -m "feat(factory): orchestrate capability gaps"
```

### Task 5: Put the existing Minibook SwarmPipeline behind a resumable job boundary

**Files:**
- Create: `minibook/swarm/contracts.py`
- Create: `minibook/swarm/job_store.py`
- Create: `minibook/swarm/service.py`
- Create: `minibook/swarm/api.py`
- Modify: `minibook/swarm/pipeline.py:103-130`
- Modify: `minibook/swarm/pipeline.py:2259-2333`
- Modify: `minibook/swarm/__init__.py:1-58`
- Create: `minibook/tests/forge/test_contracts.py`
- Create: `minibook/tests/forge/test_job_store.py`
- Create: `minibook/tests/forge/test_service.py`
- Create: `minibook/tests/forge/test_api.py`

**Interfaces:**
- Consumes: `CreationJobV1` carrying opaque input/blueprint refs and the existing `SwarmPipeline.run` behavior.
- Produces: HTTP-isolated `CreationProgressV1` and content-addressed `CreationResultV1`; named safe checkpoints.

- [ ] **Step 1: Write strict job-envelope tests**

```python
def test_creation_job_never_contains_captain_holdouts() -> None:
    payload = creation_job_payload()
    assert CreationJobV1.model_validate(payload).schema_name == "minibook.creation-job.v1"
    with pytest.raises(ValidationError):
        CreationJobV1.model_validate({**payload, "holdout_cases": [{"secret": True}]})


def test_result_requires_content_addressed_artifacts() -> None:
    with pytest.raises(ValidationError, match="artifact"):
        CreationResultV1.model_validate(result_payload(artifact_refs=("C:/output/team.py",)))
```

- [ ] **Step 2: Write resumability tests with an injected pipeline**

```python
@pytest.mark.asyncio
async def test_restart_resumes_last_safe_checkpoint(tmp_path: Path) -> None:
    store = SqliteForgeJobStore(tmp_path / "forge.db")
    pipeline = RecordingPipeline(fail_after="build_passed")
    service = ForgeJobService(store=store, pipeline=pipeline, artifact_store=FakeArtifacts())
    job_id = await service.create(creation_job())
    await service.run_once(job_id)
    resumed = ForgeJobService(store=store, pipeline=RecordingPipeline(), artifact_store=FakeArtifacts())
    await resumed.run_once(job_id)
    assert resumed.pipeline.started_from == "build_passed"
```

- [ ] **Step 3: Verify tests fail before boundary extraction**

Run: `python -m pytest minibook/tests/forge -q`

Expected: FAIL because job contracts/store/service/API are absent.

- [ ] **Step 4: Implement job contracts and SQLite state**

```python
class CreationJobV1(_FrozenContract):
    schema_name: Literal["minibook.creation-job.v1"] = "minibook.creation-job.v1"
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1)
    input_ref: str = Field(pattern=r"^artifact://sha256/[0-9a-f]{64}$")
    blueprint_ref: str = Field(pattern=r"^artifact://sha256/[0-9a-f]{64}$")
    attempt: int = Field(ge=1, le=5)


class CreationProgressV1(_FrozenContract):
    schema_name: Literal["minibook.creation-progress.v1"] = "minibook.creation-progress.v1"
    job_id: UUID
    checkpoint: Literal["accepted", "specified", "coded", "built", "executed", "evaluated", "exported"]
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
```

Use a Minibook-owned SQLite database with unique `(job_id, subject_version)` and append-only checkpoints. Do not write Captain state into this database.

- [ ] **Step 5: Adapt `SwarmPipeline` without redesigning its algorithm**

Add injected `ProgressSink`, `CancellationPort`, and `ArtifactSink`. Replace only checkpoint-worthy side effects in `run`; preserve the existing role order, code/build/evaluation loop, and ToolForge behavior. `ForgeJobService` converts outputs to content-addressed artifacts.

- [ ] **Step 6: Add independent Forge HTTP routes**

```text
POST /api/v1/forge/jobs
GET  /api/v1/forge/jobs/{job_id}
POST /api/v1/forge/jobs/{job_id}/cancel
GET  /api/v1/forge/jobs/{job_id}/result
```

Start these routes only in the Forge process. Core Minibook startup must not import Docker, AutoGen clients, or the full swarm runtime.

- [ ] **Step 7: Run Minibook Forge gate**

Run: `python -m pytest minibook/tests/forge minibook/tests/test_e2e.py -q`

Expected: PASS; collaboration-only Minibook imports without Forge dependencies, duplicate jobs are idempotent, and restart resumes from a named checkpoint.

- [ ] **Step 8: Commit WP-C**

```powershell
git add minibook/swarm/contracts.py minibook/swarm/job_store.py minibook/swarm/service.py minibook/swarm/api.py minibook/swarm/pipeline.py minibook/swarm/__init__.py minibook/tests/forge
git commit -m "feat(minibook): expose resumable agent forge"
```

### Task 6: Create the shared Hermes AutoGen Factory skill and role prompts

**Files (Hermes submodule worktree):**
- Create: `skills/autonomous-ai-agents/autogen-team-factory/SKILL.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/references/autogen-version-policy.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/references/role-contracts.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/references/tool-gap-decision-tree.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/references/evidence-contract.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/templates/agent-architect.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/templates/tool-integrator.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/templates/real-case-tester.md`
- Create: `skills/autonomous-ai-agents/autogen-team-factory/templates/quality-warden.md`
- Create: `tests/agent_factory/test_skill.py`
- Create: `tests/agent_factory/test_prompts.py`

**Interfaces:**
- Consumes: Task 1 role/phase vocabulary serialized into the Hermes package fixture.
- Produces: one discoverable shared skill and deterministic role/user prompt templates.

- [ ] **Step 1: Create failing skill validation tests**

```python
def test_factory_skill_has_peer_frontmatter_and_required_sections() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter, body = parse_skill(skill)
    assert frontmatter["name"] == "autogen-team-factory"
    assert len(frontmatter["description"]) <= 1024
    for heading in (
        "## Workflow",
        "## Context7 Evidence",
        "## Tool Gap Decision",
        "## Captain Block Handoff",
        "## Verification Checklist",
    ):
        assert heading in body


@pytest.mark.parametrize("role", FACTORY_ROLES)
def test_role_prompt_contains_enforced_envelope_fields(role: str) -> None:
    rendered = render_role_prompt(role, role_context())
    for field in ("job_id", "input_digest", "attempt", "allowed_tools", "forbidden_actions", "required_evidence"):
        assert field in rendered
    assert "${N8N_API_KEY}" not in rendered
```

- [ ] **Step 2: Verify missing-skill failures**

Run from the Hermes repository: `python -m pytest tests/agent_factory/test_skill.py tests/agent_factory/test_prompts.py -q`

Expected: FAIL because the factory skill/templates do not exist.

- [ ] **Step 3: Author the skill with a closed workflow**

The skill workflow must require this order:

```text
validate Captain envelope
→ load role template
→ resolve pinned AutoGen 0.7.5 docs through Context7
→ inventory promoted tools
→ apply tool-gap decision tree
→ run only lease-permitted Codex/n8n actions
→ verify role-specific completion criteria
→ emit one sanitized FactoryEvidenceBlock candidate
```

The skill must state that prompts do not grant authority and that `QualityWarden` recommendations cannot promote capabilities.

- [ ] **Step 4: Write exact role templates**

Each template begins with the stable role contract and ends with an output schema. For example, `real-case-tester.md` must include:

```text
You are the independent RealCaseTester for job {{ job_id }}, attempt {{ attempt }}.
You may read sealed artifacts, run approved tests, and execute promoted or candidate n8n tools through lease {{ lease_id }}.
You must not modify source, prompts, workflow definitions, assertions, or holdouts.
Return exactly one evidence document matching captain.agent-factory-block.v1 with phase real_case_evidence.
```

- [ ] **Step 5: Validate skill and prompt redaction**

Run: `python -m pytest tests/agent_factory/test_skill.py tests/agent_factory/test_prompts.py -q`

Expected: PASS; all four prompts render deterministically and secret-like keys/values are rejected.

- [ ] **Step 6: Commit inside the Hermes repository**

```powershell
git add skills/autonomous-ai-agents/autogen-team-factory tests/agent_factory/test_skill.py tests/agent_factory/test_prompts.py
git commit -m "feat: add autogen team factory skill"
```

Do not update the parent submodule pointer until Tasks 7–8 and the Hermes live gate have a reviewed Hermes commit.

### Task 7: Implement four Hermes role runners with Context7, Codex, and n8n adapters

**Files (Hermes submodule worktree):**
- Create: `agent_factory/__init__.py`
- Create: `agent_factory/contracts.py`
- Create: `agent_factory/roles.py`
- Create: `agent_factory/context7.py`
- Create: `agent_factory/capabilities.py`
- Create: `agent_factory/worker.py`
- Create: `agent_factory/cli.py`
- Modify: `pyproject.toml` console-script configuration
- Create: `tests/agent_factory/test_contracts.py`
- Create: `tests/agent_factory/test_context7.py`
- Create: `tests/agent_factory/test_capabilities.py`
- Create: `tests/agent_factory/test_worker.py`
- Create: `tests/agent_factory/test_cli.py`

**Interfaces:**
- Consumes: Captain `AgentFactoryJob`, `FactoryEvidenceBlock`, and `CapabilityGrant` JSON fixtures; existing Hermes Codex runtime switch/app-server, MCP configuration, and skill loader.
- Produces: `hermes agent-factory run --role ...` JSONL process contract and sanitized phase evidence.

- [ ] **Step 1: Write cross-repository fixture compatibility tests**

```python
def test_captain_factory_job_fixture_is_accepted() -> None:
    payload = json.loads(CAPTAIN_FIXTURE.read_text(encoding="utf-8"))
    command = FactoryRoleCommand.model_validate(payload)
    assert command.max_behavioral_iterations == 5


def test_unknown_schema_fails_closed() -> None:
    payload = json.loads(CAPTAIN_FIXTURE.read_text(encoding="utf-8"))
    payload["schema_name"] = "captain.agent-factory-job.v2"
    with pytest.raises(ValidationError):
        FactoryRoleCommand.model_validate(payload)
```

- [ ] **Step 2: Write Context7 evidence tests**

```python
@pytest.mark.asyncio
async def test_context7_capture_binds_version_sources_and_digest() -> None:
    client = FakeContext7Client(library_id="/microsoft/autogen/python_v0_7_4")
    evidence = await Context7EvidenceCollector(client, supported_version="0.7.5").query(
        "Swarm handoff and state persistence"
    )
    assert evidence.supported_runtime_version == "0.7.5"
    assert evidence.library_id == "/microsoft/autogen/python_v0_7_4"
    assert evidence.source_refs
    assert re.fullmatch(r"[0-9a-f]{64}", evidence.content_digest)
```

The 0.7.4 documentation corpus is advisory for the pinned 0.7.5 runtime. The role must verify imported 0.7.5 APIs in the build environment; documentation evidence alone cannot pass a code gate.

- [ ] **Step 3: Verify new Hermes tests fail**

Run: `python -m pytest tests/agent_factory/test_contracts.py tests/agent_factory/test_context7.py tests/agent_factory/test_capabilities.py tests/agent_factory/test_worker.py tests/agent_factory/test_cli.py -q`

Expected: FAIL because `agent_factory` is absent.

- [ ] **Step 4: Implement the shared catalog and role-specific lease enforcement**

```python
DISCOVERABLE_TOOLS = frozenset({
    "context7.resolve",
    "context7.query",
    "repo.read",
    "repo.write",
    "codex.run",
    "codex.resume",
    "codex.status",
    "test.run",
    "n8n.discover",
    "n8n.workflow.write",
    "n8n.workflow.validate",
    "n8n.workflow.execute",
    "evidence.read",
})

ROLE_MUTATIONS = {
    FactoryRole.AGENT_ARCHITECT: frozenset(),
    FactoryRole.TOOL_INTEGRATOR: frozenset({"repo.write", "codex.run", "codex.resume", "n8n.workflow.write"}),
    FactoryRole.REAL_CASE_TESTER: frozenset({"test.run", "n8n.workflow.execute"}),
    FactoryRole.QUALITY_WARDEN: frozenset(),
}
```

Every role can list `DISCOVERABLE_TOOLS`; `CapabilityGuard.require(tool_name)` validates lease ID, role, job, expiry, subject version, and mutation membership before a call.

- [ ] **Step 5: Implement bounded role execution using existing Hermes surfaces**

`FactoryWorker.run(command)` loads the shared skill, renders the role prompt, creates a job-scoped runtime configuration, invokes only adapters already present in Hermes, and returns one strict evidence document. Do not create a second generic Codex session engine or MCP client.

```python
async def run(self, command: FactoryRoleCommand) -> FactoryRoleResult:
    grant = self._guard.validate(command.grant, command)
    prompt = self._prompts.render(command.role, command, grant)
    raw = await self._runner.execute(prompt=prompt, grant=grant)
    return self._evidence.validate_and_sanitize(raw, command=command, grant=grant)
```

- [ ] **Step 6: Add a JSONL CLI with non-zero contract failures**

Expose `hermes agent-factory run --command <artifact-path>`. Emit progress/evidence JSONL to stdout, operational diagnostics to stderr, and never print environment variables. Invalid schema, expired lease, denied tool, and secret detection return distinct non-zero exit codes.

- [ ] **Step 7: Run the complete Hermes focused suite**

Run: `python -m pytest tests/agent_factory -q`

Expected: PASS; the tester cannot write source/workflows, the warden cannot run Codex, the integrator can use approved Codex/n8n mutations, and all roles share the catalog.

- [ ] **Step 8: Commit the Hermes worker implementation**

```powershell
git add agent_factory pyproject.toml tests/agent_factory
git commit -m "feat: run captain agent factory roles"
```

### Task 8: Detect tool gaps and publish version-bound typed n8n tools

**Files:**
- Create: `agenten/agent_factory/tool_gap.py`
- Create: `agenten/agent_factory/typed_tools.py`
- Modify: `agenten/targets/n8n.py:15-168`
- Create: `tests/agent_factory/test_tool_gap.py`
- Create: `tests/agent_factory/test_typed_tools.py`
- Modify: `tests/targets/test_n8n_target.py`

**Interfaces:**
- Consumes: promoted capability records, AutoGen `FunctionTool`, `N8nTarget`, candidate workflow evidence.
- Produces: `ToolGapDecision`, `TypedN8nToolSpec`, and `build_promoted_n8n_tool(spec, executor)`.

- [ ] **Step 1: Write decision-order tests**

```python
@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        (inventory(promoted_function=True), "reuse_promoted_tool"),
        (inventory(native_n8n=True), "use_native_n8n"),
        (inventory(n8n_composable=True), "create_typed_n8n_workflow"),
        (inventory(), "implement_local_tool"),
    ],
)
def test_tool_gap_policy_has_fixed_preference_order(inventory, expected):
    assert decide_tool_gap(requirement(), inventory).kind == expected
```

- [ ] **Step 2: Write typed workflow binding tests**

```python
@pytest.mark.asyncio
async def test_typed_tool_cannot_swap_workflow_identity() -> None:
    spec = typed_spec(workflow_id="wf-123", revision="7")
    executor = RecordingExecutor()
    tool = build_promoted_n8n_tool(spec, executor)
    await tool.run_json({"case_id": "C-17", "summary": "broken login"}, CancellationToken())
    assert executor.calls[0].workflow_id == "wf-123"
    assert executor.calls[0].revision == "7"
    assert "workflow_id" not in tool.schema["parameters"]["properties"]


def test_unpromoted_workflow_cannot_become_autogen_tool() -> None:
    with pytest.raises(TypedToolPolicyError, match="promotion"):
        build_promoted_n8n_tool(typed_spec(promotion_block_ref=None), RecordingExecutor())
```

- [ ] **Step 3: Verify focused tests fail**

Run: `python -m pytest tests/agent_factory/test_tool_gap.py tests/agent_factory/test_typed_tools.py tests/targets/test_n8n_target.py -q`

Expected: FAIL because the tool-gap policy and typed adapter are absent.

- [ ] **Step 4: Implement strict specs and workflow evidence**

```python
class TypedN8nToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str = Field(min_length=1, max_length=512)
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    workflow_id: str
    workflow_revision: str
    workflow_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_block_ref: ArtifactRef
```

Extend `N8nExecutionEvidence` with workflow revision, input/output digests, start/end timestamps, and MCP/API call identity. The canonical workflow ID comes only from the promoted spec.

- [ ] **Step 5: Build the AutoGen FunctionTool adapter**

Generate the callable signature from `input_schema`, validate before dispatch, call the bound executor, validate `output_schema`, and return a sanitized typed result. Disable parallel tool calls for teams whose n8n tools mutate the same resource.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/agent_factory/test_tool_gap.py tests/agent_factory/test_typed_tools.py tests/targets/test_n8n_target.py -q`

Expected: PASS; no caller-controlled workflow identity, unpromoted tool, schema mismatch, stale revision, or uncorrelated evidence is accepted.

- [ ] **Step 7: Commit WP-E**

```powershell
git add agenten/agent_factory/tool_gap.py agenten/agent_factory/typed_tools.py agenten/targets/n8n.py tests/agent_factory/test_tool_gap.py tests/agent_factory/test_typed_tools.py tests/targets/test_n8n_target.py
git commit -m "feat(factory): publish typed n8n agent tools"
```

### Task 9: Project the factory lifecycle into Minibook

**Files:**
- Modify: `agenten/delivery/minibook_events.py:18-144`
- Modify: `agenten/delivery/projector.py:77-479`
- Create: `tests/delivery/test_agent_factory_projection.py`
- Modify: `tests/delivery/test_minibook_rebuild.py`
- Create: `tests/fixtures/contracts/minibook_factory_projection.v1.json`

**Interfaces:**
- Consumes: authoritative factory blocks emitted by the gateway.
- Produces: redacted `captain.minibook-factory-projection.v1` events and idempotent posts for job, blueprint, tool gap, iteration, validation, promotion, and escalation views.

- [ ] **Step 1: Write redaction and replay tests**

```python
def test_factory_projection_rejects_holdouts_prompts_and_credentials() -> None:
    for forbidden in (
        {"holdout_body": "secret"},
        {"raw_prompt": "private"},
        {"n8n_api_key": "value"},
        {"workspace_path": "C:/private/worktree"},
    ):
        with pytest.raises(ValidationError):
            MinibookFactoryProjectionEvent.model_validate(factory_projection_payload(**forbidden))


def test_rebuild_is_idempotent_and_monotonic(projector) -> None:
    events = factory_projection_sequence()
    first = projector.rebuild(events)
    second = projector.rebuild(reversed(events))
    assert first.content_hashes == second.content_hashes
    assert projector.client.post_count == len(events)
```

- [ ] **Step 2: Verify tests fail against the current event catalog**

Run: `python -m pytest tests/delivery/test_agent_factory_projection.py tests/delivery/test_minibook_rebuild.py -q`

Expected: FAIL because factory event/template/status IDs are unknown.

- [ ] **Step 3: Extend the closed projection catalog**

Add explicit event, view, template, status, and actor literals for all factory phases. Map Hermes role names to display-only actor labels. Projection payloads contain digests, IDs, phase, attempt, status, evidence links, and safe summaries only.

- [ ] **Step 4: Render lifecycle and iteration posts**

Use one stable Minibook project per Captain project and stable subject keys per factory job/phase. Updates are monotonic by subject version; conflicts are quarantined. Comments remain discussion signals and never call lifecycle mutation routes directly.

- [ ] **Step 5: Run projection suites**

Run: `python -m pytest tests/delivery/test_agent_factory_projection.py tests/delivery/test_minibook_rebuild.py tests/delivery/test_minibook_projector.py -q`

Expected: PASS with deterministic content hashes and no duplicate posts.

- [ ] **Step 6: Commit WP-F projection**

```powershell
git add agenten/delivery/minibook_events.py agenten/delivery/projector.py tests/delivery/test_agent_factory_projection.py tests/delivery/test_minibook_rebuild.py tests/fixtures/contracts/minibook_factory_projection.v1.json
git commit -m "feat(minibook): project agent factory lifecycle"
```

### Task 10: Compose the real end-to-end lifecycle and promotion gate

**Files:**
- Create: `agenten/agent_factory/composition.py`
- Modify: `agenten/agent_runtime/control_plane.py:287-489`
- Create: `scripts/run_agent_factory.py`
- Create: `tests/integration/test_agent_factory_lifecycle.py`
- Create: `tests/live/test_agent_factory_lifecycle_live.py`
- Modify: `tests/test_import_boundaries.py`
- Modify: `tests/test_workstream_docs.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/WORKSTREAMS.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1–9, reviewed Hermes submodule commit, authenticated gateway, reachable Minibook Forge, Context7, Codex CLI, and externally owned n8n MCP/API.
- Produces: one command that resolves/reuses or creates/validates/promotes a team, plus complete run evidence.

- [ ] **Step 1: Write an offline composed lifecycle test**

```python
@pytest.mark.asyncio
async def test_capability_miss_runs_all_roles_and_promotes_only_after_holdouts() -> None:
    system = composed_factory_with_fakes()
    result = await system.run(input_ref=artifact("input"), required_capability="support_triage")
    assert result.status == "ready_to_use"
    assert result.role_order == (
        "agent_architect",
        "tool_integrator",
        "real_case_tester",
        "quality_warden",
    )
    assert result.promotion_block.phase == FactoryPhase.CAPABILITY_PROMOTED
    assert result.validation.all_required_assertions_passed


@pytest.mark.asyncio
async def test_second_run_reuses_without_hermes_or_forge() -> None:
    system = composed_factory_with_fakes()
    await system.run(input_ref=artifact("input"), required_capability="support_triage")
    system.hermes.reset_calls()
    system.forge.reset_calls()
    result = await system.run(input_ref=artifact("second-input"), required_capability="support_triage")
    assert result.decision == "reuse"
    system.hermes.assert_not_called()
    system.forge.assert_not_called()
```

- [ ] **Step 2: Write the live test with explicit dependency gates**

The live test must:

1. upload/hash the selected `input.md` without parsing a domain schema;
2. confirm gateway/MariaDB readiness and auth;
3. confirm Minibook and Forge readiness;
4. confirm Hermes factory CLI and skill discovery;
5. resolve AutoGen docs through Context7 and record evidence;
6. use Codex in an authorized disposable worktree;
7. discover, validate, and execute one typed n8n workflow with real execution ID;
8. run the generated AutoGen team against Captain holdouts;
9. prove restart/resume preserves correlation and artifact digests;
10. observe `CapabilityPromoted`, then prove the next run reuses the catalog entry.

- [ ] **Step 3: Verify tests fail before composition**

Run: `python -m pytest tests/integration/test_agent_factory_lifecycle.py -q`

Expected: FAIL because `agenten.agent_factory.composition` does not exist.

- [ ] **Step 4: Wire injected adapters at the composition root**

```python
def build_agent_factory(settings: AgentFactorySettings) -> AgentFactoryApplication:
    gateway = HttpFactoryGateway(settings.gateway)
    hermes = HermesFactoryProcessAdapter(settings.hermes)
    forge = MinibookForgeHttpAdapter(settings.forge)
    validator = CaptainFactoryValidator(gateway=gateway, holdouts=settings.holdouts)
    service = AgentFactoryService(
        gateway=gateway,
        catalog=gateway,
        hermes=hermes,
        forge=forge,
        validator=validator,
        clock=UtcClock(),
    )
    return AgentFactoryApplication(service=service)
```

Keep `agenten/orchestration/pipeline.py` free of new factory domain behavior. The existing Agent Runtime Control Plane may invoke the application port after a structured capability miss; it must not import Minibook or Hermes modules.

- [ ] **Step 5: Add a safe CLI**

Expose:

```powershell
python scripts/run_agent_factory.py --input-ref artifact://sha256/<digest> --capability support_triage
```

The CLI accepts an artifact reference, not a secret-bearing file dump. It prints job/correlation IDs, current phase, attempt, and evidence manifest path. It returns non-zero for `escalated`, infrastructure blocked, unresolved evidence, or validation failure.

- [ ] **Step 6: Update parent submodule pin only after Hermes gates pass**

In the parent worktree, record the reviewed Hermes commit and add only the submodule pointer. Confirm `git diff --submodule=log` contains the intended Hermes factory commits and no unrelated upstream changes.

- [ ] **Step 7: Run focused non-live gates**

```powershell
python -m pytest tests/agent_factory tests/gateway/test_factory_projection.py tests/gateway/test_factory_routes.py tests/integration/test_agent_factory_lifecycle.py tests/delivery/test_agent_factory_projection.py -q
python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
python -m compileall -q agenten gateway chats config
```

Expected: zero failures. Report exact pass/skip/deselect counts; any required MariaDB/Hermes/Minibook/Context7/Codex/n8n skip keeps the corresponding live gate open.

- [ ] **Step 8: Run the live gate**

```powershell
python -m pytest tests/gateway/test_factory_mariadb.py -v -m mariadb
python -m pytest tests/live/test_agent_factory_lifecycle_live.py -v -m live
```

Expected: both PASS with zero required skips. Record gateway block IDs, Hermes/Codex session ID, Context7 evidence digest, Minibook project/post IDs, n8n workflow/revision/execution IDs, generated code digest, holdout validation block, and promotion block in a gitignored run manifest.

- [ ] **Step 9: Run the complete repository gate**

```powershell
python -m pytest -q
python scripts/verify_submission.py
python main.py demo --output artifacts/demo-run.json
```

Do not keep a rewritten `artifacts/demo-run.json` unless the factory implementation intentionally changes deterministic demo evidence and that diff is reviewed.

- [ ] **Step 10: Update architecture and ownership documentation**

Document `input artifact → capability lookup → Hermes roles → Minibook Forge → typed n8n tool → independent test → Captain validation blocks → promotion/reuse`. State exact live prerequisites and external n8n ownership. Add new branch contracts and exclusive scopes to `docs/WORKSTREAMS.md`.

- [ ] **Step 11: Commit the composed lifecycle**

```powershell
git add agenten/agent_factory/composition.py agenten/agent_runtime/control_plane.py scripts/run_agent_factory.py tests/integration/test_agent_factory_lifecycle.py tests/live/test_agent_factory_lifecycle_live.py tests/test_import_boundaries.py tests/test_workstream_docs.py docs/ARCHITECTURE.md docs/WORKSTREAMS.md README.md hermes-agent
git commit -m "feat(factory): prove agent team lifecycle"
```

## Integration order and merge gates

1. Integrate the existing Agent Runtime Control Plane baseline and the approved lifecycle design/plan into a clean integration worktree.
2. Complete Task 1 before any package implements its own copy of the factory contracts.
3. Tasks 2 and 3 establish Captain/gateway authority.
4. Tasks 5 and 6 may proceed in independent Minibook and Hermes worktrees against Task 1 fixtures.
5. Task 7 stays in the Hermes repository until its package gate is green; then update the parent submodule pin.
6. Task 8 consumes gateway promotion records and n8n target evidence; it cannot promote itself.
7. Task 9 begins after gateway event vocabulary is frozen.
8. Task 10 is the only cross-package integration branch and is not green until MariaDB, Hermes, Minibook Forge, Context7, Codex, n8n, generated AutoGen execution, holdouts, recovery, and reuse all pass with zero required skips.

## Stop conditions

Stop and report rather than weakening the design when:

- the integration baseline lacks a reviewed control-plane commit or has overlapping uncommitted edits;
- a package requires another package's database, credentials, or internal Python import;
- a role can call a mutating tool without a valid Captain lease;
- Minibook or Hermes can write `CapabilityPromoted`;
- a generated tool accepts a caller-provided workflow ID or revision;
- a live dependency is absent and a test proposes a mock/skip as release evidence;
- a behavioral failure would exceed five attempts;
- Context7 evidence is unversioned or the built AutoGen API is not verified against installed 0.7.5;
- n8n execution evidence lacks canonical workflow, revision, execution, input/output digest, and timestamps;
- any secret, raw holdout, unrestricted path, or full transcript crosses a projection or block boundary.
