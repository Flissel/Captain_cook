# Captain–Hermes–Minibook Agent Factory V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Captain dispatch an authorized V3 creation job that Hermes actually executes through Codex, while Minibook durably owns checkpoints, validation, packaging, CAS readback, and async user clarification; prove the exact four-agent `sales_pipeline_brief` team end to end.

**Architecture:** Add an opt-in V3 path beside unchanged V1 legacy generation and V2 sealed import. Captain remains lifecycle/success authority, Hermes is the factory worker, Codex is the only isolated build-workspace writer, and Minibook owns durable assignments, effects, validation, and CAS packaging. Missing user decisions pause through the existing authenticated Captain/Gateway portal and resume by version-fenced metadata; secrets remain behind the existing credential boundary and only opaque references flow downstream.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI, SQLite, MariaDB-backed Gateway stores, filesystem CAS, pytest/pytest-asyncio, React 19, TypeScript strict, Vite/Vitest, Supabase portal authentication.

---

## Global implementation rules

- Keep `CreationJobV1`, `CreationJobV2`, and `CreationResultV1` semantics unchanged.
- Never fall back from V3 to the generic Minibook parser, browser parser, V1, or V2.
- Do not add raw credential values to factory contracts, logs, snapshots, artifacts, or portal responses.
- Only the Codex execution adapter may write beneath the authorized build workspace.
- Run root and Minibook pytest suites in separate processes because both contain a top-level `tests` package.
- Do not claim success from HTTP 2xx, a receipt, or a process exit alone; require typed content and independent CAS readback.
- Do not register with OpenFang, promote, deploy, or mark a team `ready_to_use` in this plan.

## Task 1: Freeze the V3 wire contracts and cross-repository fixtures

**Files:**

- Modify: `agenten/agent_factory/forge_contracts.py`
- Modify: `minibook/swarm/contracts.py`
- Create: `tests/fixtures/contracts/minibook_creation_job.v3.json`
- Create: `tests/fixtures/contracts/hermes_factory_assignment.v2.json`
- Create: `tests/fixtures/contracts/hermes_factory_build_effect.v2.json`
- Create: `tests/fixtures/contracts/minibook_creation_result.v2.json`
- Create: `tests/fixtures/contracts/factory_action_request.v1.json`
- Create: `tests/fixtures/contracts/factory_action_response.v1.json`
- Modify: `tests/agent_factory/test_forge_contract_compatibility.py`
- Modify: `minibook/tests/test_creation_contracts.py`

- [ ] **Step 1: Write failing compatibility and rejection tests.**

Add parameterized fixture round-trip tests in both repositories and assert byte-compatible canonical JSON. Add negative cases for source archives in V3 jobs, absolute paths, raw credential fields, mismatched lease digests, duplicate agents, invalid handoffs, missing required output contracts, bad media types, identity drift, and a `CreationResultV2(status="succeeded")` missing any required reference.

```python
@pytest.mark.parametrize(
    ("fixture_name", "contract"),
    (
        ("minibook_creation_job.v3.json", CreationJobV3),
        ("hermes_factory_assignment.v2.json", FactoryBuildAssignmentV2),
        ("hermes_factory_build_effect.v2.json", FactoryBuildEffectV2),
        ("minibook_creation_result.v2.json", CreationResultV2),
        ("factory_action_request.v1.json", FactoryActionRequestV1),
        ("factory_action_response.v1.json", FactoryActionResponseV1),
    ),
)
def test_v3_contract_fixture_is_canonical(fixture_name: str, contract: type[BaseModel]) -> None:
    payload = json.loads((_FIXTURES / fixture_name).read_text(encoding="utf-8"))
    parsed = contract.model_validate(payload)
    assert parsed.model_dump(mode="json", by_alias=True) == payload


def test_creation_result_v2_cannot_succeed_from_receipt_only(
    creation_result_v2_payload: dict[str, object],
) -> None:
    payload = {**creation_result_v2_payload, "team_manifest_ref": None}
    with pytest.raises(ValidationError, match="successful V2 result requires"):
        CreationResultV2.model_validate(payload)
```

- [ ] **Step 2: Run the RED tests.**

Run:

```powershell
python -m pytest tests/agent_factory/test_forge_contract_compatibility.py -q
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_contracts.py -q
```

Expected: collection fails because the six V3 contract classes do not exist.

- [ ] **Step 3: Implement strict frozen models in both mirrors.**

Use shared field shapes and validators in both files. The core shapes must include these non-optional bindings:

```python
class CreationJobV3(CreationJobV1):
    schema_name: Literal["minibook.creation-job.v3"] = Field(
        default="minibook.creation-job.v3", alias="schema", serialization_alias="schema"
    )
    command_id: UUID
    lease_ref: ArtifactRef
    lease_sha256: str = Field(pattern=SHA256_PATTERN)
    blueprint_ref: ArtifactRef
    tool_plan_ref: ArtifactRef
    required_agent_ids: tuple[str, ...] = Field(min_length=1)
    ordered_handoffs: tuple[AgentHandoffV1, ...]
    tool_requirements: tuple[FactoryToolRequirementV1, ...] = Field(min_length=1)
    workspace_policy_ref: ArtifactRef
    clarification_policy: ClarificationPolicyV1
    required_output_contract: Literal["minibook.creation-result.v2"]


class FactoryBuildAssignmentV2(_FrozenContract):
    schema_name: Literal["hermes.factory-build-assignment.v2"] = Field(
        default="hermes.factory-build-assignment.v2", alias="schema", serialization_alias="schema"
    )
    assignment_id: UUID
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    checkpoint_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    effect_key: str = Field(pattern=SHA256_PATTERN)
    workspace_ref: str = Field(pattern=r"^workspace://[A-Za-z0-9._:/-]+$")
    workspace_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    required_agent_ids: tuple[str, ...] = Field(min_length=1)
    ordered_handoffs: tuple[AgentHandoffV1, ...]
    tool_requirements: tuple[FactoryToolRequirementV1, ...] = Field(min_length=1)
    allowed_output_roots: tuple[str, ...] = Field(min_length=1)
    allowed_commands: tuple[tuple[str, ...], ...] = Field(min_length=1)
    limits: FactoryBuildLimitsV1
    lease_ref: ArtifactRef
    deadline_at: datetime


class FactoryBuildEffectV2(_FrozenContract):
    schema_name: Literal["hermes.factory-build-effect.v2"] = Field(
        default="hermes.factory-build-effect.v2", alias="schema", serialization_alias="schema"
    )
    assignment_id: UUID
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    checkpoint_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    effect_key: str = Field(pattern=SHA256_PATTERN)
    hermes_usage_receipt_ref: ArtifactRef
    codex_build_receipt_ref: ArtifactRef
    codex_session_ref: ArtifactRef
    workspace_snapshot_ref: ArtifactRef
    source_archive_ref: ArtifactRef
    preliminary_team_manifest_ref: ArtifactRef
    build_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    test_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()


class CreationResultV2(_FrozenContract):
    schema_name: Literal["minibook.creation-result.v2"] = Field(
        default="minibook.creation-result.v2", alias="schema", serialization_alias="schema"
    )
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    status: Literal["succeeded", "failed", "blocked", "cancelled"]
    team_manifest_ref: ArtifactRef | None = None
    candidate_manifest_ref: ArtifactRef | None = None
    source_archive_ref: ArtifactRef | None = None
    package_manifest_ref: ArtifactRef | None = None
    hermes_usage_receipt_ref: ArtifactRef | None = None
    codex_build_receipt_ref: ArtifactRef | None = None
    build_evidence_refs: tuple[ArtifactRef, ...] = ()
    real_case_evidence_refs: tuple[ArtifactRef, ...] = ()
    cas_readback_receipt_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()
    failure: CreationFailure | None = None
```

Model `FactoryActionRequestV1` and `FactoryActionResponseV1` exactly as the approved design, including `credential://` validation and a recursive secret-like key/value rejection validator. Keep models frozen and forbid extra fields.

- [ ] **Step 4: Run both contract suites GREEN and compare canonical fixture bytes.**

- [ ] **Step 5: Commit.**

```powershell
git add agenten/agent_factory/forge_contracts.py minibook/swarm/contracts.py tests/fixtures/contracts tests/agent_factory/test_forge_contract_compatibility.py minibook/tests/test_creation_contracts.py
git commit -m "feat(factory): add strict v3 creation contracts"
```

## Task 2: Add durable Minibook V3 state, assignment claims, effects, and pause/resume

**Files:**

- Modify: `minibook/swarm/job_store.py`
- Modify: `minibook/swarm/runner.py`
- Modify: `minibook/swarm/scheduler.py`
- Create: `minibook/tests/test_creation_v3_state.py`
- Modify: `minibook/tests/test_creation_resume.py`
- Modify: `minibook/tests/test_creation_scheduler.py`

- [ ] **Step 1: Write state-machine tests before schema changes.**

Cover `queued -> planning -> awaiting_build -> building -> validating -> packaging -> readback -> succeeded`, pause to `awaiting_user_input`, version-fenced resume to the prior safe checkpoint, expiry to `blocked`, cancellation, claim replay, conflicting second worker, effect replay, mismatched effect key, and restart recovery from a new `CreationJobStore` instance.

```python
def test_assignment_claim_is_exactly_once_across_restart(tmp_path: Path, job_v3: CreationJobV3) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    store.submit(job_v3)
    assignment = store.publish_build_assignment(build_assignment(job_v3))
    first = store.claim_build_assignment(assignment.assignment_id, worker_id="hermes-a")

    restarted = CreationJobStore(tmp_path / "creation.sqlite3")
    replay = restarted.claim_build_assignment(assignment.assignment_id, worker_id="hermes-a")
    assert replay == first
    with pytest.raises(CreationConflictError, match="claimed by another worker"):
        restarted.claim_build_assignment(assignment.assignment_id, worker_id="hermes-b")
```

- [ ] **Step 2: Run RED.**

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_v3_state.py minibook/tests/test_creation_resume.py minibook/tests/test_creation_scheduler.py -q
```

- [ ] **Step 3: Extend SQLite append-only state.**

Add `creation_build_assignments`, `creation_assignment_claims`, `creation_build_effects`, and `creation_action_projections` tables. Store canonical JSON plus identity columns and unique constraints. Extend progress to V2 or add a V3-only progress model with statuses `planning`, `awaiting_build`, `building`, `validating`, `packaging`, `readback`, and `awaiting_user_input`. Parse stored jobs/results by their `schema` discriminator rather than forcing V1.

Implement these public methods with immutable replay semantics. Keep their SQL in
small private transaction helpers so the public boundary stays typed:

```python
def publish_build_assignment(
    self, assignment: FactoryBuildAssignmentV2
) -> FactoryBuildAssignmentV2:
    canonical = _json(assignment)
    with self._immediate_transaction() as db:
        existing = self._assignment_row(db, assignment.assignment_id)
        if existing is not None:
            if existing["payload"] != canonical:
                raise CreationConflictError("assignment already has different content")
            return FactoryBuildAssignmentV2.model_validate_json(existing["payload"])
        self._require_assignment_matches_head(db, assignment)
        db.execute(
            "INSERT INTO creation_build_assignments VALUES (?, ?, ?, NULL)",
            (str(assignment.assignment_id), str(assignment.creation_job_id), canonical),
        )
    return assignment


def accept_build_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2:
    canonical = _json(effect)
    with self._immediate_transaction() as db:
        assignment = self._claimed_assignment_row(db, effect.assignment_id)
        self._require_effect_matches_assignment(effect, assignment)
        existing = self._effect_row(db, effect.assignment_id)
        if existing is not None:
            if existing["payload"] != canonical:
                raise CreationConflictError("assignment effect already differs")
            return FactoryBuildEffectV2.model_validate_json(existing["payload"])
        db.execute(
            "INSERT INTO creation_build_effects VALUES (?, ?, ?)",
            (str(effect.assignment_id), effect.effect_key, canonical),
        )
    return effect
```

Use one `BEGIN IMMEDIATE` transaction per claim/effect/transition. Same identity plus same canonical body returns the existing record; changed content raises `CreationConflictError`.

- [ ] **Step 4: Make the runner yield at external boundaries.**

Introduce an explicit nonterminal slice result instead of manufacturing a generic receipt for missing effects:

```python
@dataclass(frozen=True)
class StepOutcome:
    snapshot: dict[str, Any]
    effect_receipt: dict[str, Any] | None = None
    yield_status: Literal["awaiting_build", "awaiting_user_input"] | None = None


if outcome.yield_status is not None:
    self.store.transition(
        job.creation_job_id,
        expected_version=progress.version,
        status=outcome.yield_status,
        checkpoint=step,
        snapshot=outcome.snapshot,
    )
    return None
```

Update scheduler typing so a yielded V3 slice remains resumable but is not busy-polled while awaiting an assignment effect or user response.

- [ ] **Step 5: Run GREEN plus existing Minibook regression tests.**

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_v3_state.py minibook/tests/test_creation_resume.py minibook/tests/test_creation_scheduler.py minibook/tests/test_pipeline_adapter.py -q
```

- [ ] **Step 6: Commit.**

```powershell
git add minibook/swarm/job_store.py minibook/swarm/runner.py minibook/swarm/scheduler.py minibook/tests/test_creation_v3_state.py minibook/tests/test_creation_resume.py minibook/tests/test_creation_scheduler.py
git commit -m "feat(minibook): persist v3 factory checkpoints"
```

## Task 3: Expose authenticated Minibook V3 worker operations and production composition

**Files:**

- Modify: `minibook/swarm/api.py`
- Modify: `minibook/swarm/api_client.py`
- Modify: `minibook/src/main.py`
- Create: `minibook/swarm/v3_pipeline.py`
- Modify: `minibook/tests/test_creation_api.py`
- Modify: `minibook/tests/test_swarm_api_client.py`
- Modify: `minibook/tests/test_creation_cli.py`

- [ ] **Step 1: Add failing API tests.**

Prove worker auth, bound worker identity, assignment claim replay, conflicting claims, effect-key binding, typed 409/422 responses, scheduling resume after effect, and that V3 startup fails closed when CAS/pipeline dependencies are absent. Retain all current V1/V2 route tests.

```python
def test_worker_submits_effect_and_resumes_scheduler(client: TestClient, assignment: FactoryBuildAssignmentV2) -> None:
    claimed = client.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/claim",
        headers={"Authorization": "Bearer worker-secret"},
        json={"worker_id": "hermes-factory"},
    )
    assert claimed.status_code == 200
    accepted = client.post(
        f"/v3/creation/assignments/{assignment.assignment_id}/effect",
        headers={"Authorization": "Bearer worker-secret"},
        json=build_effect(assignment).model_dump(mode="json", by_alias=True),
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "validating"
```

- [ ] **Step 2: Run RED, then implement routes and typed client methods.**

Add submit/status/result/cancel handling that dispatches by contract version, plus:

- `GET /v3/creation/assignments/next`
- `POST /v3/creation/assignments/{assignment_id}/claim`
- `POST /v3/creation/assignments/{assignment_id}/effect`
- `GET /v3/creation/artifacts/{sha256}/metadata`

Use the same constant-time bearer-auth pattern as existing creation routes. Do not return filesystem paths.

- [ ] **Step 3: Replace the unconfigured production-runner exception only for explicit V3 configuration.**

`minibook/src/main.py::_creation_runner_for` must construct `V3CreationPipeline` only when all required settings exist. If V3 is requested without store, CAS, validator, and worker auth, raise a configuration error at startup. Disabled creation must not import Forge, Docker, or LLM modules.

- [ ] **Step 4: Run GREEN and regressions.**

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_api.py minibook/tests/test_swarm_api_client.py minibook/tests/test_creation_cli.py -q
```

- [ ] **Step 5: Commit.**

```powershell
git add minibook/swarm/api.py minibook/swarm/api_client.py minibook/swarm/v3_pipeline.py minibook/src/main.py minibook/tests/test_creation_api.py minibook/tests/test_swarm_api_client.py minibook/tests/test_creation_cli.py
git commit -m "feat(minibook): expose authenticated v3 build handoff"
```

## Task 4: Implement strict team validation, deterministic packaging, and Minibook CAS readback

**Files:**

- Create: `minibook/swarm/team_package.py`
- Modify: `minibook/swarm/creation_export.py`
- Modify: `minibook/swarm/artifact_store.py`
- Modify: `minibook/swarm/v3_pipeline.py`
- Create: `minibook/tests/test_team_package.py`
- Modify: `minibook/tests/test_creation_export.py`
- Modify: `minibook/tests/test_creation_artifact_store.py`

- [ ] **Step 1: Write failing package/gate tests.**

Use a canonical `sales_pipeline_brief` fixture requiring exactly:

```python
EXPECTED_AGENTS = (
    "account_researcher",
    "opportunity_qualifier",
    "deal_strategist",
    "executive_brief_writer",
)
EXPECTED_HANDOFFS = tuple(zip(EXPECTED_AGENTS, EXPECTED_AGENTS[1:], strict=True))
REQUIRED_TOOLS = ("account_snapshot_reader", "opportunity_scoring_rules")
```

Test rejection of one agent, extra agent, reordered/missing handoff, missing required tool, untyped tool schema, empty source, symlink/path escape, source that fails `compileall`, failed declared tests, model-invented scoring weights, undeclared tool gaps, receipt-only output, null output, archive/manifest drift, and CAS tampering.

- [ ] **Step 2: Run RED.**

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_team_package.py minibook/tests/test_creation_export.py minibook/tests/test_creation_artifact_store.py -q
```

- [ ] **Step 3: Implement validation from the compiled contract, never from free text.**

`validate_team_package(...)` must read referenced bytes, validate the exact agent set and ordered edges, inspect declared tool schemas and implementation files, require passing compile/test/real-case receipts, and compare optional gaps exactly against policy. Treat forbidden implementation markers in required tool files as failures.

```python
def require_exact_topology(
    manifest: TeamManifestV2,
    required_agents: tuple[str, ...],
    required_handoffs: tuple[AgentHandoffV1, ...],
) -> None:
    actual_agents = tuple(agent.agent_id for agent in manifest.agents)
    actual_handoffs = tuple(manifest.handoffs)
    if actual_agents != required_agents:
        raise TeamPackageError("team agents do not match compiled specification")
    if actual_handoffs != required_handoffs:
        raise TeamPackageError("team handoffs do not match compiled specification")
```

- [ ] **Step 4: Build deterministic package and read back every digest.**

Require `team_manifest.json`, `factory-candidate.json`, Hermes usage receipt, Codex build receipt, source, tests, and evidence in the safe file set. Publish each artifact and the deterministic ZIP to `FilesystemCreationArtifactStore`; read them through the same public CAS reader and emit a canonical `minibook.cas-readback-receipt.v1` only if every digest matches.

- [ ] **Step 5: Run GREEN and commit.**

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_team_package.py minibook/tests/test_creation_export.py minibook/tests/test_creation_artifact_store.py -q
git add minibook/swarm/team_package.py minibook/swarm/creation_export.py minibook/swarm/artifact_store.py minibook/swarm/v3_pipeline.py minibook/tests/test_team_package.py minibook/tests/test_creation_export.py minibook/tests/test_creation_artifact_store.py
git commit -m "feat(minibook): validate and seal v3 team packages"
```

## Task 5: Make Hermes claim assignments and invoke the existing authorized Codex builder

**Files:**

- Create: `agenten/agent_factory/hermes_factory_worker.py`
- Modify: `agenten/agent_factory/hermes_cli.py`
- Modify: `agenten/agent_factory/codex_build_execution.py`
- Modify: `agenten/agent_factory/codex_build_provenance.py`
- Modify: `gateway/agent_factory_live_composition.py`
- Modify: `gateway/agent_factory_live_operator.py`
- Create: `tests/agent_factory/test_hermes_factory_worker.py`
- Modify: `tests/agent_factory/test_codex_build_execution.py`
- Modify: `tests/gateway/test_agent_factory_live_composition.py`

- [ ] **Step 1: Write behavioral RED tests with counters and raise-on-second-call guards.**

Prove Hermes validates the released skill digest, claims once, maps V2 assignment to the existing detached workspace/Codex execution path, submits exactly one effect, never writes itself, never accepts null/timeout/process-exit-only outcomes, and does not retry without lease/retry authority.

```python
@pytest.mark.asyncio
async def test_worker_replay_does_not_start_second_codex_session(assignment: FactoryBuildAssignmentV2) -> None:
    codex = CountingCodexBuilder(valid_effect(assignment), fail_after=1)
    worker = HermesFactoryWorker(minibook=ReplayMinibook(assignment), codex=codex)
    first = await worker.run_once()
    replay = await worker.run_once()
    assert first == replay
    assert codex.calls == 1
```

- [ ] **Step 2: Run RED, then implement the coordinator.**

```python
class HermesFactoryWorker:
    async def run_once(self) -> FactoryBuildEffectV2 | None:
        assignment = await self._minibook.next_assignment()
        if assignment is None:
            return None
        claim = await self._minibook.claim(assignment.assignment_id, self._worker_id)
        existing = await self._minibook.accepted_effect(assignment.assignment_id)
        if existing is not None:
            return existing
        receipt = await self._codex.build(self._validated_request(assignment, claim))
        effect = self._effect_from_receipt(assignment, receipt)
        return await self._minibook.submit_effect(effect)
```

Adapt, do not duplicate, `GitDetachedFactoryWorkspacePreparer`, `CaptainCodexBuildSealer`, checkpoint store, and `CodexBuildArtifactCas`. The assignment's allowed paths/commands/limits must narrow existing defaults. A real `CodexBuildReceiptV1` remains required and is referenced by `FactoryBuildEffectV2`.

- [ ] **Step 3: Wire production composition behind an explicit V3 enable flag.**

Reject partial configuration. Record Hermes worker identity, assignment/effect identity, and sanitized exit metadata; never store prompts, provider responses, local paths, or credentials.

- [ ] **Step 4: Run GREEN and commit.**

```powershell
python -m pytest tests/agent_factory/test_hermes_factory_worker.py tests/agent_factory/test_codex_build_execution.py tests/agent_factory/test_codex_build_provenance.py tests/gateway/test_agent_factory_live_composition.py -q
git add agenten/agent_factory/hermes_factory_worker.py agenten/agent_factory/hermes_cli.py agenten/agent_factory/codex_build_execution.py agenten/agent_factory/codex_build_provenance.py gateway/agent_factory_live_composition.py gateway/agent_factory_live_operator.py tests/agent_factory/test_hermes_factory_worker.py tests/agent_factory/test_codex_build_execution.py tests/gateway/test_agent_factory_live_composition.py
git commit -m "feat(factory): execute v3 assignments through codex"
```

## Task 6: Dispatch V3 from Captain and independently validate Minibook CAS results

**Files:**

- Modify: `agenten/agent_factory/minibook_forge.py`
- Modify: `agenten/agent_factory/orchestration.py`
- Modify: `agenten/agent_factory/candidate_evaluation.py`
- Modify: `gateway/minibook_creation_artifacts.py`
- Modify: `gateway/agent_factory_live_composition.py`
- Modify: `tests/agent_factory/test_minibook_forge.py`
- Modify: `tests/agent_factory/test_orchestration.py`
- Modify: `tests/agent_factory/test_forge_candidate_binding.py`
- Modify: `tests/gateway/test_minibook_creation_artifacts.py`

- [ ] **Step 1: Write failing Captain authority tests.**

Require a demonstrated capability miss and active digest-bound lease before one V3 submission. Assert Captain rejects V1/V2 results, stale subject/attempt/correlation, one-agent packages, missing tools, receipt-only success, missing readback receipt, invalid build/test evidence, and CAS bytes changed after Minibook validation.

```python
@pytest.mark.asyncio
async def test_captain_rejects_minibook_success_when_independent_readback_is_tampered(
    dispatcher: FactoryDispatcher,
    cas: TamperingCas,
) -> None:
    with pytest.raises(FactoryDispatchError, match="Captain CAS readback digest mismatch"):
        await dispatcher.dispatch_next()
    assert dispatcher.repository.successful_lifecycle_events == ()
```

- [ ] **Step 2: Implement the V3 mapper/client without changing V1/V2.**

Add `CaptainCreationJobV3Mapper` and typed V3 client methods. Map exact compiled topology/tool requirements, the active lease ref/digest, blueprint/tool-plan refs, clarification policy, and `required_output_contract="minibook.creation-result.v2"`. Never add a source ZIP.

- [ ] **Step 3: Add independent Captain readback.**

Extend `GatewayMinibookCreationArtifactStore` with metadata lookup if needed, but retain read-only authority. Add a V3 acceptance branch in `GatewayForgeCandidateProvider` that reads every result/package reference, recomputes SHA-256, revalidates identities, topology, tools, candidate bindings, ZIP contents, build/test status, and Minibook readback receipt before emitting agent-code evidence.

- [ ] **Step 4: Run GREEN and commit.**

```powershell
python -m pytest tests/agent_factory/test_minibook_forge.py tests/agent_factory/test_orchestration.py tests/agent_factory/test_forge_candidate_binding.py tests/gateway/test_minibook_creation_artifacts.py -q
git add agenten/agent_factory/minibook_forge.py agenten/agent_factory/orchestration.py agenten/agent_factory/candidate_evaluation.py gateway/minibook_creation_artifacts.py gateway/agent_factory_live_composition.py tests/agent_factory/test_minibook_forge.py tests/agent_factory/test_orchestration.py tests/agent_factory/test_forge_candidate_binding.py tests/gateway/test_minibook_creation_artifacts.py
git commit -m "feat(captain): validate v3 factory results independently"
```

## Task 7: Add the Captain/Gateway asynchronous Action Inbox

**Files:**

- Create: `gateway/factory_action_store.py`
- Modify: `gateway/portal_contracts.py`
- Modify: `gateway/portal_store.py`
- Modify: `gateway/store.py`
- Modify: `gateway/app.py`
- Create: `agenten/agent_factory/action_resume.py`
- Create: `tests/gateway/test_factory_action_store.py`
- Modify: `tests/gateway/test_portal_api.py`
- Create: `tests/agent_factory/test_action_resume.py`

- [ ] **Step 1: Write RED tests for the full async lifecycle.**

Cover request creation, authenticated tenant-scoped list/read, version-fenced answer, duplicate replay, changed duplicate conflict, stale version, foreign tenant/job, expiry, over-scoped decision, secret-bearing metadata, credential reference format, consume-once, restart persistence, redacted Minibook projection, and resumption from the exact safe checkpoint.

```python
def test_action_response_contains_reference_but_never_secret(
    store: FactoryActionStore, pending_request: FactoryActionRequestV1
) -> None:
    response = action_response(
        pending_request,
        credential_ref="credential://tenant-a/n8n-salesforce",
        answer_metadata={"credential_alias": "salesforce"},
    )
    stored = store.answer(response, tenant_id="tenant-a")
    serialized = stored.model_dump_json()
    assert "credential://tenant-a/n8n-salesforce" in serialized
    assert "api_key" not in serialized.lower()
```

- [ ] **Step 2: Implement a generic durable store and portal routes.**

Use MariaDB through the existing Gateway database composition. Store canonical request/response payloads, tenant binding, status, expected version, checkpoint digest, deadline, and consume fence. Reuse existing portal principal/authentication and ticket semantics.

Add:

- `GET /v1/portal/factory-actions?status=pending`
- `GET /v1/portal/factory-actions/{request_id}`
- `POST /v1/portal/factory-actions/{request_id}/responses`
- `POST /v1/portal/factory-actions/{request_id}/credentials` using the dedicated credential-store adapter

The credential route returns only an opaque reference. It must not persist secret material in `GatewayStore` or `FactoryActionStore`.

- [ ] **Step 3: Implement nonblocking pause/resume coordination.**

Captain records last safe checkpoint ref/digest and returns control immediately after creating the action request. `FactoryActionResumeCoordinator` consumes an answered request, verifies subject version/scope/deadline/checkpoint, records approved metadata, projects a redacted status to Minibook, and schedules continuation. It must not wait in an HTTP request or worker process.

- [ ] **Step 4: Run GREEN and commit.**

```powershell
python -m pytest tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py -q
git add gateway/factory_action_store.py gateway/portal_contracts.py gateway/portal_store.py gateway/store.py gateway/app.py agenten/agent_factory/action_resume.py tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py
git commit -m "feat(gateway): add asynchronous factory action inbox"
```

## Task 8: Extend the existing React portal with the Action Inbox

**Files:**

- Modify: `portal/src/types.ts`
- Modify: `portal/src/api.ts`
- Modify: `portal/src/App.tsx`
- Create: `portal/src/components/FactoryActionInbox.tsx`
- Create: `portal/src/components/FactoryActionCard.tsx`
- Modify: `portal/src/styles.css`
- Modify: `portal/src/test/fixtures.ts`
- Modify: `portal/src/api.test.ts`
- Modify: `portal/src/App.test.tsx`
- Create: `portal/src/components/FactoryActionInbox.test.tsx`

- [ ] **Step 1: Write failing UI/API tests.**

Test authenticated loading, pending/answered/expired states, display of last safe checkpoint, allowed decisions only, clarification answer, credential setup through the dedicated route, n8n approval scope, stale-version conflict refresh, no secret echo, and accessible form labels/status announcements.

```tsx
it("submits only the opaque credential reference with the action response", async () => {
  const user = userEvent.setup();
  render(<FactoryActionInbox api={api} actions={[credentialAction]} />);
  await user.click(screen.getByRole("button", { name: /credential verbinden/i }));
  await user.click(screen.getByRole("button", { name: /fortsetzen/i }));
  expect(api.respondToFactoryAction).toHaveBeenCalledWith(
    credentialAction.request_id,
    expect.objectContaining({ credential_ref: "credential://tenant-a/n8n-salesforce" }),
  );
  expect(JSON.stringify(api.respondToFactoryAction.mock.calls)).not.toContain("secret-value");
});
```

- [ ] **Step 2: Implement strict TypeScript types and API parsing.**

Use discriminated unions for request type/status and `unknown` plus narrowing for network payloads; never use `any`. Reuse the existing bearer token and safe URL logic.

- [ ] **Step 3: Add the inbox to the existing portal shell.**

Keep Integration Setup intact. Render safe message, scope, deadline, checkpoint, and action-specific controls. On 409, refetch rather than overwriting newer state. Never render or log raw credential values after submission.

- [ ] **Step 4: Run frontend verification and commit.**

```powershell
npm --prefix portal test -- --run
npm --prefix portal run lint
npm --prefix portal run build
git add portal/src
git commit -m "feat(portal): render factory action inbox"
```

## Task 9: Prove the hermetic four-agent flow and all fail-closed variants

**Files:**

- Create: `tests/e2e/test_factory_v3_sales_pipeline.py`
- Create: `tests/fixtures/agent_factory/sales_pipeline_brief.v3.json`
- Create: `tests/support/factory_v3_harness.py`
- Modify: `tests/agent_factory/test_input_compiler.py`
- Modify: `agenten/agent_factory/input_compiler.py`

- [ ] **Step 1: Freeze the authoritative compiled sales-pipeline requirements.**

Add a compiler test proving fixed ordered agents/handoffs, required tools `account_snapshot_reader` and `opportunity_scoring_rules`, optional `industry_signal_reader`, and public assertions. Model-generated changes are not accepted.

- [ ] **Step 2: Build a hermetic harness using real boundaries.**

Use the real Captain dispatcher/coordinator, FastAPI HTTP clients, Minibook SQLite scheduler/store, filesystem CAS/package builder, Action Inbox store, and Captain readback validator. The deterministic Hermes/Codex adapter must create actual nonempty Python source/tests in an isolated workspace and invoke the same authorized build port; it may replace the external model process only.

- [ ] **Step 3: Write the happy-path RED test.**

```python
@pytest.mark.asyncio
async def test_sales_pipeline_brief_builds_exact_team_and_resumes_credential_action(
    factory_v3_harness: FactoryV3Harness,
) -> None:
    run = await factory_v3_harness.submit_sales_pipeline_brief()
    action = await run.wait_for_action("credential_required")
    await run.answer(action, credential_ref="credential://tenant-a/crm-readonly")
    result = await run.wait_for_result()

    assert result.status == "succeeded"
    assert run.creation_submissions == 1
    assert run.codex_sessions == 1
    assert run.build_effects == 1
    assert run.team_agent_ids == (
        "account_researcher",
        "opportunity_qualifier",
        "deal_strategist",
        "executive_brief_writer",
    )
    assert run.required_tools_with_passing_tests == {
        "account_snapshot_reader",
        "opportunity_scoring_rules",
    }
    assert run.minibook_readback_verified
    assert run.captain_readback_verified
```

- [ ] **Step 4: Add parameterized negative cases.**

```python
@pytest.mark.parametrize(
    "variant",
    ("one_agent", "null_output", "timeout", "receipt_only", "tampered_cas"),
)
@pytest.mark.asyncio
async def test_factory_v3_never_marks_invalid_build_successful(
    factory_v3_harness: FactoryV3Harness, variant: str
) -> None:
    result = await factory_v3_harness.run_variant(variant)
    assert result.status in {"failed", "blocked"}
    assert factory_v3_harness.captain_success_events == ()
```

- [ ] **Step 5: Run hermetic E2E and focused regressions.**

```powershell
python -m pytest tests/e2e/test_factory_v3_sales_pipeline.py -q
python -m pytest tests/agent_factory/test_forge_contract_compatibility.py tests/agent_factory/test_orchestration.py tests/agent_factory/test_minibook_forge.py tests/agent_factory/test_hermes_factory_worker.py tests/gateway/test_portal_api.py -q
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_contracts.py minibook/tests/test_creation_v3_state.py minibook/tests/test_creation_api.py minibook/tests/test_team_package.py -q
npm --prefix portal test -- --run
```

- [ ] **Step 6: Commit.**

```powershell
git add agenten/agent_factory/input_compiler.py tests/agent_factory/test_input_compiler.py tests/e2e/test_factory_v3_sales_pipeline.py tests/fixtures/agent_factory/sales_pipeline_brief.v3.json tests/support/factory_v3_harness.py
git commit -m "test(factory): prove hermetic v3 team creation"
```

## Task 10: Add and execute the opt-in live Hermes/Codex proof

**Files:**

- Create: `tests/live/test_factory_v3_sales_pipeline_live.py`
- Create: `scripts/run-factory-v3-live.ps1`
- Create: `docs/runbooks/factory-v3-live.md`
- Modify: `gateway/agent_factory_live_operator.py`

- [ ] **Step 1: Write a fail-closed live preflight test.**

Require explicit enablement, reachable Captain/Gateway and Minibook endpoints, active Hermes released-skill digest, authorized Codex session script, isolated workspace root, CAS roots, worker token, bounded budget/deadline, and portal/credential prerequisites. Missing items produce an unmet-preflight report and start no build.

- [ ] **Step 2: Implement the run script with correlation-scoped evidence.**

The script must create a fresh isolated run directory, submit the exact `sales_pipeline_brief`, poll bounded status, surface an Action Inbox URL if user input is required, resume from metadata, and write a sanitized evidence summary containing IDs/digests/statuses only. It must never open the generic browser parser and never substitute a deterministic adapter.

- [ ] **Step 3: Execute the real live chain.**

```powershell
$env:CAPTAIN_FACTORY_V3_LIVE='1'
pwsh -File scripts/run-factory-v3-live.ps1 -Input tests/fixtures/agent_factory/sales_pipeline_brief.v3.json
python -m pytest tests/live/test_factory_v3_sales_pipeline_live.py -q -m live
```

Expected evidence under one correlation ID:

- real Hermes released-skill invocation;
- one real authorized Codex session and isolated workspace snapshot;
- four exact agents and ordered handoffs;
- both required tools implemented and tested;
- Minibook build/test/package/readback receipts;
- Captain independent readback acceptance;
- no OpenFang registration, promotion, deployment, or `ready_to_use` event.

If credentials or n8n approval are missing, answer through the Action Inbox and resume; do not edit configuration around the gate. The goal remains incomplete until this step passes or the user explicitly narrows the required evidence.

- [ ] **Step 4: Run final regression gates.**

```powershell
python -m pytest tests/agent_factory tests/gateway -q
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests -q
npm --prefix portal test -- --run
npm --prefix portal run lint
npm --prefix portal run build
git status --short
```

- [ ] **Step 5: Document verified facts and commit.**

Record exact commands, timestamps, correlation ID, artifact digests, test counts, and explicit non-claims in `docs/runbooks/factory-v3-live.md`. Do not include secrets, local absolute workspace paths, prompts, provider responses, or holdouts.

```powershell
git add tests/live/test_factory_v3_sales_pipeline_live.py scripts/run-factory-v3-live.ps1 docs/runbooks/factory-v3-live.md gateway/agent_factory_live_operator.py
git commit -m "test(factory): verify live v3 hermes codex chain"
```

## Completion checklist

- [ ] V1/V2 compatibility and historical reads remain green.
- [ ] V3 has no direct parser/browser/ZIP-import fallback.
- [ ] Captain records one active lease and one creation submission.
- [ ] Hermes claims one assignment and starts one Codex session.
- [ ] Codex is the only isolated workspace writer.
- [ ] Minibook survives restart at every nonterminal checkpoint.
- [ ] Action Inbox pauses and resumes without a waiting worker.
- [ ] Only opaque credential references cross the credential boundary.
- [ ] Exact four-agent topology, required tools, tests, manifests, and evidence pass.
- [ ] One-agent, null, timeout, receipt-only, and tampered-CAS variants fail closed.
- [ ] Minibook and Captain independently read back CAS bytes.
- [ ] Hermetic E2E and relevant regressions pass in separate root/Minibook processes.
- [ ] Opt-in live Hermes/Codex E2E passes with current-state evidence.
- [ ] OpenFang, promotion, deployment, and `ready_to_use` remain out of scope.
