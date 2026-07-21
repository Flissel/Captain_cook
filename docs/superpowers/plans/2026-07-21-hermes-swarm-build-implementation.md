# Hermes Skill, SwarmPipeline, and Codex Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Minibook's existing `SwarmPipeline` execute one idempotent, resumable creation job in which Hermes uses the released AutoGen factory skill, resolves documentation and tools, assigns scoped implementation work to Codex, and returns a content-addressed capability-package candidate.

**Architecture:** Captain submits a versioned creation envelope over a process/HTTP boundary. Minibook persists the job and advances the existing pipeline through named checkpoints. Pipeline build nodes call a Hermes factory worker, which proves the released skill digest, captures AutoGen/n8n documentation provenance, and delegates authorized filesystem changes to the existing Codex worker. Required integrations use scoped n8n MCP only when native/documented; otherwise Codex creates a typed local API/tool or emits `TODO_TOOL.v1`. Minibook returns artifacts and evidence but never marks them ready.

**Tech Stack:** Python 3.11, Pydantic v2, aiohttp/FastAPI-compatible Minibook routes, SQLite, AutoGen 0.7.5, Hermes CLI, Codex CLI/app-server, n8n MCP/REST, pytest.

## Global Constraints

- This package owns `minibook/swarm/*`, the minimal `minibook/autogen_swarm.py` extraction, `agenten/agent_factory/forge_*`, `minibook_forge.py`, and scoped Hermes submodule files. Do not edit Captain state/release policy or Gateway persistence.
- Parent Captain code and Minibook code exchange JSON fixtures; neither imports the other's implementation modules.
- Preserve the current eleven-step SwarmPipeline behavior while extracting checkpoints. Do not redesign agent prompts and pipeline logic in the same commit as persistence.
- Every creation effect is keyed by `creation_job_id`, `subject_version`, `attempt`, and idempotency key. A replay cannot start a second Codex session or duplicate an n8n workflow effect.
- Hermes must use the exact Captain-released skill digest. The skill is an instruction boundary, not release authority.
- Context7/official AutoGen documentation and current n8n documentation/tool discovery are evidence sources. Unavailable required documentation is a block, never guessed knowledge.
- Codex alone mutates the authorized creation workspace. Hermes and SwarmPipeline may create control-plane state only in their own stores.
- n8n is created only for declared external integrations. Prefer a documented native node, then approved MCP, then typed HTTP to a tested local adapter.
- Reasoning stays in AutoGen. Do not hide agent decisions inside n8n Code nodes.
- Successful work may create an immutable private Hermes skill candidate. Only Captain can validate, publish, and promote it.
- The Hermes submodule must be clean before work. Commit there first, then pin exactly that reviewed SHA in a separate parent commit.

---

## Task 1: Freeze Cross-Process Creation and Assignment Contracts

**Files:**
- Create: `agenten/agent_factory/forge_contracts.py`
- Create: `minibook/swarm/contracts.py`
- Create: `tests/fixtures/contracts/minibook_creation_job.v1.json`
- Create: `tests/fixtures/contracts/minibook_creation_result.v1.json`
- Create: `tests/fixtures/contracts/hermes_factory_assignment.v1.json`
- Create: `tests/contracts/test_forge_contract_compatibility.py`
- Create: `minibook/tests/test_creation_contracts.py`

**Interfaces:** Produces byte-compatible producer/consumer forms of `CreationJobV1`, `CreationProgressV1`, `CreationResultV1`, `FactoryBuildAssignmentV1`, `DocumentationEvidence`, `CreationArtifact`, and `CreationFailure`.

- [ ] **Step 1: Write failing fixture round-trip and forbidden-field tests**

```python
def test_parent_and_minibook_validate_the_same_creation_job_fixture() -> None:
    payload = json.loads(JOB_FIXTURE.read_text(encoding="utf-8"))
    captain = CaptainCreationJob.model_validate(payload)
    minibook = CreationJobV1.model_validate(payload)
    assert captain.model_dump(mode="json", by_alias=True) == minibook.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize("field", ["holdout_bodies", "credentials", "absolute_workspace"])
def test_creation_job_rejects_private_or_unrestricted_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        CreationJobV1.model_validate(job_payload() | {field: "forbidden"})
```

- [ ] **Step 2: Define the strict contracts on both sides**

```python
class CreationJobV1(_FrozenContract):
    schema_name: Literal["minibook.creation-job.v1"] = Field(alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    factory_job_id: UUID
    correlation_id: UUID
    causation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    idempotency_key: str = Field(pattern=SHA256_PATTERN)
    input_ref: ArtifactRef
    compiled_spec_ref: ArtifactRef
    dependency_graph_ref: ArtifactRef
    released_skill: ReleasedSkillRefV1
    public_assertion_ids: tuple[str, ...] = Field(min_length=1)
    deadline_at: datetime


class CreationResultV1(_FrozenContract):
    schema_name: Literal["minibook.creation-result.v1"] = Field(alias="schema", serialization_alias="schema")
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int
    attempt: int
    status: Literal["succeeded", "failed", "blocked", "cancelled"]
    package_manifest_ref: ArtifactRef | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()
    skill_usage_receipt_ref: ArtifactRef | None = None
    private_skill_candidate_ref: ArtifactRef | None = None
```

Model validators require a package manifest and skill receipt only for `succeeded`; failures require a sanitized `CreationFailure`; all artifact URIs are opaque/content-addressed and never absolute paths.

- [ ] **Step 3: Run contract tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/contracts/test_forge_contract_compatibility.py minibook/tests/test_creation_contracts.py
```

Expected: PASS with byte-identical JSON fixture round trips.

- [ ] **Step 4: Commit the boundary**

```powershell
git add agenten/agent_factory/forge_contracts.py minibook/swarm/contracts.py tests/fixtures/contracts tests/contracts/test_forge_contract_compatibility.py minibook/tests/test_creation_contracts.py
git commit -m "feat: define forge creation contracts"
```

## Task 2: Add Durable Creation State and Named Checkpoints

**Files:**
- Create: `minibook/swarm/job_store.py`
- Create: `minibook/swarm/runner.py`
- Create: `minibook/tests/test_creation_resume.py`

**Interfaces:** Produces `CreationJobStore`, `CreationRunner`, `CreationCheckpoint`, `CreationStatus`, `PipelineStepPort`, `run_slice(job_id)`, `cancel(job_id, expected_version)`, and `result(job_id)`.

- [ ] **Step 1: Write restart and exactly-once tests against temporary SQLite**

Cover queued, running between steps, expired deadline, cancelled, failed, blocked, and completed states. Simulate a crash after an external effect is recorded but before the next step starts. Restart with a new `CreationRunner` and prove the accepted effect is reused.

```python
@pytest.mark.asyncio
async def test_restart_resumes_after_last_committed_step_without_duplicate_effect(tmp_path: Path) -> None:
    first = scripted_runner(tmp_path, fail_after="tool_resolution")
    await first.run_slice(JOB_ID)
    resumed = scripted_runner(tmp_path)
    result = await resumed.run_slice(JOB_ID)
    assert result.status == "succeeded"
    assert resumed.calls["tool_resolution"] == 0
    assert resumed.calls["codex_build"] == 1
```

- [ ] **Step 2: Implement append-only SQLite records**

Use tables for immutable jobs, monotonic job heads, step receipts, external-effect receipts, and final results. Identical replay returns the existing record; changed content under the same ID raises `CreationConflictError`. Update the head and append the step receipt in one transaction.

- [ ] **Step 3: Enforce deadline and cancellation before effects**

`run_slice` checks `deadline_at`, current version, and cancellation before dispatching a step. A started-but-uncommitted pure step may rerun; an effectful step must first obtain its deterministic effect key and reuse an existing receipt.

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q --no-cov minibook/tests/test_creation_resume.py`

- [ ] **Step 5: Commit persistence**

```powershell
git add minibook/swarm/job_store.py minibook/swarm/runner.py minibook/tests/test_creation_resume.py
git commit -m "feat: persist resumable swarm creation jobs"
```

## Task 3: Extract the Existing SwarmPipeline Behind a Step Port

**Files:**
- Modify: `minibook/swarm/pipeline.py`
- Modify: `minibook/autogen_swarm.py`
- Create: `minibook/swarm/pipeline_adapter.py`
- Create: `minibook/tests/test_pipeline_adapter.py`

**Interfaces:** Produces `PIPELINE_STEP_ORDER`, `SwarmStep`, `SwarmSnapshot`, `SwarmPipelineAdapter.run_step(job, step, prior_snapshot)`, and `assemble_result(snapshot)`.

- [ ] **Step 1: Characterize current call order before refactoring**

Write a test around the existing `run()` path that records the current named steps: manager, catalog, architect, coder, reviewer, tester, validator, builder, executor, output evaluation, TODO implementation/toolforge where needed, feedback loop, evaluation report, export. Freeze the observed conditional ordering.

- [ ] **Step 2: Extract one-step dispatch without changing step bodies**

```python
class SwarmPipelineAdapter:
    async def run_step(
        self,
        job: CreationJobV1,
        step: SwarmStep,
        prior: SwarmSnapshot,
    ) -> SwarmSnapshot:
        pipeline = self._pipeline_factory(prior)
        output = await self._dispatch[step](pipeline)
        return self._snapshotter.capture(job, step, pipeline, output)
```

Snapshots contain safe relative artifact bindings, digests, decisions, and opaque external receipt IDs, not API keys, raw `.env`, full transcripts, or private holdouts. Keep `run_input_file_pipeline` as a compatibility wrapper that submits/runs a local creation job.

- [ ] **Step 3: Replace broad state-boundary exception suppression**

Translate known documentation, tool, Codex, n8n, build, and validation failures into typed `CreationFailure` codes. Redact unknown exceptions and preserve their type name only. Do not change non-boundary user-facing compatibility behavior in this task.

- [ ] **Step 4: Run pipeline characterization and existing Minibook tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov minibook/tests/test_pipeline_adapter.py minibook/tests/test_runtime_options.py minibook/tests/test_pipeline_interaction.py
```

- [ ] **Step 5: Commit the extraction**

```powershell
git add minibook/swarm/pipeline.py minibook/autogen_swarm.py minibook/swarm/pipeline_adapter.py minibook/tests/test_pipeline_adapter.py
git commit -m "refactor: checkpoint the existing swarm pipeline"
```

## Task 4: Expose Optional Creation API and Replace Fire-and-Forget Submission

**Files:**
- Create: `minibook/swarm/api.py`
- Modify: `minibook/src/main.py`
- Create: `minibook/tests/test_creation_api.py`
- Modify: `agenten/agent_factory/minibook_forge.py`
- Modify: `agenten/agent_factory/orchestration.py`
- Modify: `tests/agent_factory/test_minibook_forge.py`

**Interfaces:** Minibook exposes `POST /api/v1/creation-jobs`, `GET /api/v1/creation-jobs/{id}`, `POST /api/v1/creation-jobs/{id}/cancel`, and `GET /api/v1/creation-jobs/{id}/result`. Captain's `MinibookForgePort.submit()` returns a typed submission receipt and adds `status()`/`result()` reads.

- [ ] **Step 1: Write API idempotency and optional-startup tests**

Test identical POST replay (`200` after initial `202`), changed replay (`409`), version-fenced cancel, result-before-completion (`409`), unknown job (`404`), and core Minibook startup when Forge dependencies are absent. Forge-disabled mode returns a capability document and never imports Docker/LLM modules.

- [ ] **Step 2: Implement lazy route wiring**

The route layer validates JSON into `CreationJobV1`, persists before scheduling, and returns only typed progress/result payloads. The runner is injected; route handlers do not instantiate model clients or shell commands.

- [ ] **Step 3: Replace the parent subprocess-only adapter**

Introduce `MinibookForgeHttpClient` with bounded polling and auth from injected configuration. Keep `MinibookSwarmForge` as an offline compatibility adapter, but make it await process exit and parse one `CreationResultV1` result file rather than discarding stdout/stderr and returning immediately.

```python
class MinibookForgePort(Protocol):
    async def submit(self, request: FactoryDispatch) -> CreationSubmissionReceipt: ...
    async def status(self, creation_job_id: UUID) -> CreationProgressV1: ...
    async def result(self, creation_job_id: UUID) -> CreationResultV1: ...
```

- [ ] **Step 4: Run API and adapter tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov minibook/tests/test_creation_api.py tests/agent_factory/test_minibook_forge.py
```

- [ ] **Step 5: Commit the service boundary**

```powershell
git add minibook/swarm/api.py minibook/src/main.py minibook/tests/test_creation_api.py agenten/agent_factory/minibook_forge.py agenten/agent_factory/orchestration.py tests/agent_factory/test_minibook_forge.py
git commit -m "feat: expose minibook creation job service"
```

## Task 5: Bind Hermes Skill, Documentation, Tool Resolution, and Codex Work

**Files (parent):**
- Modify: `agenten/agent_factory/skills/autogen-agent-factory/SKILL.md`
- Modify: `agenten/agent_factory/skills/autogen-agent-factory/agents/openai.yaml`
- Create: `agenten/agent_factory/forge_documentation.py`
- Create: `tests/agent_factory/test_forge_documentation.py`
- Create: `tests/agent_factory/test_factory_skill_package.py`

**Files (Hermes submodule):**
- Create: `hermes_cli/factory_worker_contracts.py`
- Create: `hermes_cli/factory_documentation.py`
- Create: `hermes_cli/factory_worker.py`
- Create: `tests/hermes_cli/test_factory_documentation.py`
- Create: `tests/hermes_cli/test_factory_worker.py`
- Modify only as required: `hermes_cli/captain_worker.py`
- Modify only as required: `hermes_cli/n8n_worker_mcp.py`

**Interfaces:** Produces `DocumentationQuery`, `DocumentationEvidence`, `FactoryWorkerResult`, `FactoryDocumentationPort`, `Context7DocumentationAdapter`, `FactoryWorker.execute(assignment)`, and a prompt/skill package whose digest is verified by existing `ReleasedHermesSkill` logic.

- [ ] **Step 1: Write documentation provenance tests**

Require installed AutoGen version, official package/library identifier, query text digest, retrieved version/time, source references, and content digest. A mismatched AutoGen major/minor, missing required snapshot, or unversioned answer fails before Codex. n8n documentation is queried only when the compiled spec contains an external integration.

- [ ] **Step 2: Write tool-resolution decision tests**

For each integration prove this exact order: released typed tool → documented native n8n node → approved n8n MCP operation → typed HTTP workflow → self-built local API/tool/MCP adapter → `TODO_TOOL.v1`. Required/optional severity comes only from the compiled input. A self-built adapter resolves a required gap only after schema, auth boundary, health/idempotency, failure, contract, and isolated execution evidence all pass.

- [ ] **Step 3: Strengthen the released skill instructions and package test**

The skill must tell Hermes to:

1. verify the released skill and job digests;
2. retrieve AutoGen docs before architecture/code decisions;
3. retrieve n8n docs/catalog only for declared integrations;
4. assign each dependency-ready work node to Codex using an authorized workspace and bounded command;
5. require code, tests, manifests, and real receipts rather than prose;
6. preserve prior green assertions on retries;
7. retain a private skill candidate only after the assigned capability succeeds;
8. never publish the skill or claim `ready_to_use`.

`test_factory_skill_package.py` reads `SKILL.md`, hashes it, validates frontmatter, checks referenced scripts/files exist, and rejects secret-like content.

- [ ] **Step 4: Implement the Hermes factory worker using existing Codex/n8n primitives**

```python
class FactoryWorker:
    async def execute(self, assignment: FactoryBuildAssignmentV1) -> FactoryWorkerResult:
        docs = await self._docs.resolve(assignment.documentation_queries)
        decisions = await self._tools.resolve(assignment.integrations, docs)
        work_result = await self._codex.execute(self._work_package(assignment, decisions))
        return self._seal_result(assignment, docs, decisions, work_result)
```

Reuse `CaptainWorker` session/restart/path confinement and `N8nWorkerMcp` allow-list/idempotency evidence. Do not create a second Codex runner or generic workflow-ID executor. The worker writes only to the authorized workspace and its own state root.

- [ ] **Step 5: Prove private skill-candidate retention semantics**

On success, emit a digest-bound `HermesSkillUsageReceipt` and optionally a `HermesSkillCandidate(status="private_candidate")`. On failed/blocked/cancelled work, store evaluation evidence but no candidate. Parent/Gateway publication remains outside this package.

- [ ] **Step 6: Run parent and Hermes focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_forge_documentation.py tests/agent_factory/test_factory_skill_package.py tests/agent_factory/test_hermes_cli.py
git -C hermes-agent status --short --branch
git -C hermes-agent diff --check
Push-Location hermes-agent
python -m pytest -q tests/hermes_cli/test_factory_documentation.py tests/hermes_cli/test_factory_worker.py tests/hermes_cli/test_captain_worker.py tests/hermes_cli/test_n8n_worker_mcp.py
Pop-Location
```

- [ ] **Step 7: Commit the submodule first, then the parent pin**

In Hermes:

```powershell
git add hermes_cli/factory_worker_contracts.py hermes_cli/factory_documentation.py hermes_cli/factory_worker.py tests/hermes_cli/test_factory_documentation.py tests/hermes_cli/test_factory_worker.py hermes_cli/captain_worker.py hermes_cli/n8n_worker_mcp.py
git commit -m "feat: execute skill guided factory builds"
```

In parent after review:

```powershell
git add hermes-agent agenten/agent_factory/skills/autogen-agent-factory agenten/agent_factory/forge_documentation.py tests/agent_factory/test_forge_documentation.py tests/agent_factory/test_factory_skill_package.py
git commit -m "feat: bind hermes factory worker skill"
```

## Task 6: Assemble and Prove the Candidate Package

**Files:**
- Create: `minibook/swarm/package_assembler.py`
- Create: `minibook/tests/test_package_assembler.py`
- Create: `minibook/tests/live/test_creation_job_live.py`
- Create: `tests/live/test_hermes_factory_build_live.py`

**Interfaces:** Produces a sealed archive with `team-manifest.json`, `autogen/`, conditional `n8n/`, conditional `adapters/`, `skills/`, `tests/`, `evidence/`, and `RUNBOOK.md`, plus `CreationResultV1`.

- [ ] **Step 1: Write package completeness and traversal tests**

Require every manifest path to be safe/relative and digest-matching. An integration-free fixture must not require an `n8n/` workflow. An integration fixture must bind each workflow to typed schemas, idempotency, timeout, retry, duplicate, failure, and compensation behavior. Reject symlinks, traversal, absolute paths, unknown executable commands, and missing evidence.

- [ ] **Step 2: Implement deterministic assembly**

Sort archive entries, normalize timestamps/permissions, exclude `.env`, VCS metadata, caches, logs, and raw transcripts, and produce the same archive digest for the same inputs. Validate Python imports and the team startup command in a fresh isolated workspace before returning success.

- [ ] **Step 3: Add opt-in live creation tests**

The Minibook live test submits through the public creation API, restarts after a named checkpoint, resumes, verifies every artifact digest, imports/starts the generated AutoGen team, and proves one external integration only when the fixture declares it. The Hermes live test requires a real Codex session ID and, for the integration fixture, a real n8n MCP call/workflow execution ID.

- [ ] **Step 4: Run the complete Package B deterministic gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov minibook/tests/test_creation_contracts.py minibook/tests/test_creation_resume.py minibook/tests/test_pipeline_adapter.py minibook/tests/test_creation_api.py minibook/tests/test_package_assembler.py tests/contracts/test_forge_contract_compatibility.py tests/agent_factory/test_minibook_forge.py tests/agent_factory/test_factory_skill_package.py
.\.venv\Scripts\python.exe -m compileall -q minibook agenten\agent_factory
git diff --check
git diff --submodule=log
```

- [ ] **Step 5: Commit assembly and live gates**

```powershell
git add minibook/swarm/package_assembler.py minibook/tests/test_package_assembler.py minibook/tests/live/test_creation_job_live.py tests/live/test_hermes_factory_build_live.py
git commit -m "feat: assemble factory capability candidates"
```

## Package B Handoff

Publish the reviewed `minibook.creation-result.v1` fixture, package-manifest digest, Hermes submodule SHA, exact deterministic test count, and live prerequisites. Do not call the package `ready_to_use`; Package C must independently validate and release it.
