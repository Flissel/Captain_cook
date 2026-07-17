# Agent Runtime Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the typed control plane through which AutoGen reasoning swarms request Hermes planning, feed accepted plans through Captain decomposition, and run bounded Codex implementation sessions with per-task n8n MCP capability leases.

**Architecture:** Captain owns the state machine and MariaDB authority while a new `agenten/agent_runtime/` package owns cross-runtime contracts, capability policy, and injected ports. AutoGen swarm tools call that service; concrete Hermes, Minibook, Codex, and n8n behavior stays behind cross-product adapters and the existing independent work-package plans.

**Tech Stack:** Python 3.11, Pydantic v2, AutoGen Core/AgentChat 0.7.5, FastAPI gateway, MariaDB, Hermes Agent, Codex CLI/app server, MCP, n8n, Minibook, pytest.

## Global Constraints

- `main` is the source of truth; each task is implemented on a dedicated branch/worktree and merged only after its owned gate passes.
- MariaDB through the gateway is the sole production lifecycle authority.
- Minibook is a rebuildable planning/collaboration projection, not lifecycle authority.
- Captain must not import Hermes or Minibook internals; compatibility uses versioned JSON fixtures.
- Hermes owns Codex process/session supervision; Captain must not duplicate it.
- n8n MCP is granted only for Captain-approved `integration_intent=n8n` subtasks.
- VibeMind owns n8n containers and volumes; tests may call the instance but never manage it.
- Secrets and private holdouts never enter prompts, Minibook, fixtures, logs, evidence payloads, or commits.
- The reasoning model selects among valid actions; deterministic code enforces transitions, budgets, and capabilities.
- Live evidence may not be replaced by mocks, skips, or synthesized identifiers.

---

## File structure

### Captain control plane

- Create `agenten/agent_runtime/contracts.py`: strict cross-runtime commands, results, planning results, agent blueprints, and capability grants.
- Create `agenten/agent_runtime/ports.py`: injected planner, artifact, state, and clock protocols plus an adapter protocol for the existing execution process.
- Create `agenten/agent_runtime/capabilities.py`: deterministic profile derivation and lease validation.
- Create `agenten/agent_runtime/prompt_policy.py`: Codex overlays, including the n8n integration-first policy.
- Create `agenten/agent_runtime/service.py`: command-before-effect orchestration and idempotent result recording.
- Create `agenten/agent_runtime/tools.py`: AutoGen-facing `hermes.*` and `codex.*` tool wrappers.
- Create `agenten/agent_runtime/swarm.py`: ready-task next-action selection guarded by deterministic state.
- Create `agenten/agent_runtime/gateway_client.py`: HTTP-only authoritative state adapter.

### Existing Captain integration points

- Modify `agenten/planning/captain_pipeline.py`: accept a validated Hermes plan artifact as versioned planning input.
- Modify `agenten/planning/factory.py`: inject the plan-result reader and runtime control-plane ports.
- Modify `agenten/execution/codex_supervisor.py`: accept a validated runtime grant and delegate through its injected runner without widening its secret environment allow-list.
- Modify `agenten/execution/process.py`: consume runtime results through the existing reviewed-plan and validation gates.
- Modify `agenten/runtime/bootstrap.py`: register the new routed swarm adapter without adding domain logic to the bootstrap.
- Modify `gateway/contracts.py`, `gateway/store.py`, and `gateway/app.py`: add version-fenced runtime-command/result endpoints and capability-grant persistence through the existing store.

### Cross-product work

- Hermes changes follow `docs/superpowers/plans/2026-07-17-hermes-codex-n8n-worker.md` in a dedicated submodule branch.
- Minibook Forge changes follow `docs/superpowers/plans/2026-07-17-minibook-creation-pipeline.md`.
- Minibook projections follow `docs/superpowers/plans/2026-07-17-minibook-projection-boundary.md`.
- Captain validation/promotion follows `docs/superpowers/plans/2026-07-17-captain-authority-chain.md`.

---

### Task 1: Freeze Agent Runtime contracts

**Files:**
- Create: `agenten/agent_runtime/__init__.py`
- Create: `agenten/agent_runtime/contracts.py`
- Create: `tests/agent_runtime/test_contracts.py`
- Create: `tests/fixtures/contracts/agent_runtime_command.v1.json`
- Create: `tests/fixtures/contracts/agent_runtime_result.v1.json`
- Create: `tests/fixtures/contracts/hermes_plan_result.v1.json`
- Create: `tests/fixtures/contracts/agent_blueprint.v1.yaml`

**Interfaces:**
- Produces: `RuntimeOperation`, `IntegrationIntent`, `CapabilityProfile`, `ArtifactRef`, `AgentRuntimeCommand`, `AgentRuntimeResult`, `HermesPlanResult`, and `AgentBlueprint`.
- Consumes: the shared envelope fields defined in `docs/superpowers/specs/2026-07-17-independent-work-packages-design.md`.

- [ ] **Step 1: Write failing strict-contract tests**

```python
def test_n8n_profile_requires_approved_intent() -> None:
    with pytest.raises(ValidationError):
        AgentRuntimeCommand.model_validate({
            **COMMAND_FIXTURE,
            "payload": {
                **COMMAND_FIXTURE["payload"],
                "operation": "codex.run",
                "integration_intent": "none",
                "capability_profile": "n8n-builder",
            },
        })


def test_agent_blueprint_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError):
        AgentBlueprint.model_validate({
            **BLUEPRINT_FIXTURE,
            "environment": {"N8N_MCP_TOKEN": "secret"},
        })
```

- [ ] **Step 2: Run the contract tests and prove the module is missing**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_contracts.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'agenten.agent_runtime'`.

- [ ] **Step 3: Implement the strict enums and models**

```python
class RuntimeOperation(str, Enum):
    HERMES_PLAN = "hermes.plan"
    HERMES_DESIGN_AGENT = "hermes.design_agent"
    CODEX_RUN = "codex.run"
    CODEX_RESUME = "codex.resume"
    CODEX_STATUS = "codex.status"


class IntegrationIntent(str, Enum):
    NONE = "none"
    N8N = "n8n"


class CapabilityProfile(str, Enum):
    PLANNER = "planner"
    AGENT_DESIGNER = "agent-designer"
    CODE_BUILDER = "code-builder"
    N8N_BUILDER = "n8n-builder"
```

Use `ConfigDict(extra="forbid", frozen=True)` on every cross-process model,
validate lowercase SHA-256 digests, and reject `n8n-builder` unless the command
contains `integration_intent=n8n`.

- [ ] **Step 4: Add byte-stable JSON/YAML fixtures and round-trip tests**

Require the command fixture to reference an authorized workspace and prompt
artifact; require results to echo command ID, subject version, grant ID, and
correlation ID. Fixtures contain no absolute user paths or credentials.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_contracts.py`
Expected: PASS.
Commit: `feat: define agent runtime contracts`

---

### Task 2: Implement deterministic capability policy

**Files:**
- Create: `agenten/agent_runtime/capabilities.py`
- Create: `tests/agent_runtime/test_capabilities.py`

**Interfaces:**
- Consumes: `AgentRuntimeCommand`, released `WorkBatch` capability tags, and an injected UTC clock.
- Produces: `CapabilityGrant derive_grant(command, released_batch, now)` and `validate_grant(grant, command, now)`.

- [ ] **Step 1: Write failing policy tests**

```python
def test_swarm_cannot_self_grant_n8n() -> None:
    command = command_for(profile="n8n-builder", intent="n8n")
    batch = released_batch(capability_tags=["code-builder"])
    with pytest.raises(CapabilityDenied, match="n8n-builder was not released"):
        derive_grant(command, batch, NOW)


def test_expired_grant_cannot_resume_codex() -> None:
    grant = grant_for(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(CapabilityDenied, match="expired"):
        validate_grant(grant, resume_command(), NOW)
```

- [ ] **Step 2: Run tests and confirm the missing implementation**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_capabilities.py`
Expected: FAIL on import.

- [ ] **Step 3: Implement fail-closed profile derivation**

```python
PROFILE_CAPABILITIES: dict[CapabilityProfile, frozenset[str]] = {
    CapabilityProfile.PLANNER: frozenset({"hermes.plan", "minibook.plan"}),
    CapabilityProfile.AGENT_DESIGNER: frozenset({"hermes.plan", "hermes.design_agent", "minibook.plan"}),
    CapabilityProfile.CODE_BUILDER: frozenset({"codex.run", "codex.resume", "codex.status", "workspace.write", "tests.run"}),
    CapabilityProfile.N8N_BUILDER: frozenset({"codex.run", "codex.resume", "codex.status", "workspace.write", "tests.run", "mcp.n8n"}),
}
```

Bind every grant to command ID, batch ID/version, subtask ID, workspace ref,
profile, issued/expiry timestamps, and permitted MCP server names. Do not store
tokens or arbitrary environment values.

- [ ] **Step 4: Test replay, profile escalation, expiry, and workspace mismatch**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_capabilities.py`
Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `feat: enforce agent runtime capability leases`

---

### Task 3: Generate bounded runtime prompts and n8n overlay

**Files:**
- Create: `agenten/agent_runtime/prompt_policy.py`
- Create: `agenten/agent_runtime/prompts/codex_base.md`
- Create: `agenten/agent_runtime/prompts/codex_n8n_overlay.md`
- Create: `tests/agent_runtime/test_prompt_policy.py`

**Interfaces:**
- Produces: `render_codex_prompt(command, batch, grant) -> RenderedPrompt` with content digest and redaction report.
- Consumes: build-visible cases only; never consumes `HoldoutSuite` bodies.

- [ ] **Step 1: Write failing prompt-policy tests**

```python
def test_plain_builder_has_no_n8n_instructions() -> None:
    rendered = render_codex_prompt(plain_command(), batch(), plain_grant())
    assert "n8n" not in rendered.text.lower()


def test_n8n_builder_requires_discover_validate_test_evidence_order() -> None:
    rendered = render_codex_prompt(n8n_command(), batch(), n8n_grant())
    positions = [rendered.text.index(term) for term in (
        "discover MCP tools", "prefer native n8n nodes", "validate", "test", "evidence"
    )]
    assert positions == sorted(positions)
```

- [ ] **Step 2: Run tests and prove the renderer is absent**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_prompt_policy.py`
Expected: FAIL on import.

- [ ] **Step 3: Implement base and n8n overlays**

The base prompt fixes workspace, task, acceptance assertions, allowed tools,
iteration budget, and required result schema. The n8n overlay requires MCP
discovery, native-node preference, validation, isolated execution, and real
workflow/call evidence while prohibiting Docker/volume management.

- [ ] **Step 4: Add redaction and holdout-isolation assertions**

Reject known secret field names, token-like values, raw holdout objects, and
absolute paths outside the authorized workspace before creating the artifact.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_prompt_policy.py`
Expected: PASS.
Commit: `feat: render bounded codex runtime prompts`

---

### Task 4: Build the runtime control-plane service with injected ports

**Files:**
- Create: `agenten/agent_runtime/ports.py`
- Create: `agenten/agent_runtime/service.py`
- Create: `tests/agent_runtime/test_service.py`
- Modify: `agenten/execution/codex_supervisor.py`
- Modify: `agenten/execution/process.py`
- Modify: `tests/execution/test_codex_supervisor.py`

**Interfaces:**
- Produces: `AgentRuntimeService.execute(command) -> AgentRuntimeResult`.
- Consumes: `RuntimeStatePort`, `HermesPlannerPort`, `CodexExecutionPort`, `ArtifactPort`, `CapabilityPolicyPort`, and `Clock`; the Codex port adapts the existing `ExecutionProcess`/`CodexSupervisor` pair.

- [ ] **Step 1: Define the ports and failing orchestration tests**

```python
class HermesPlannerPort(Protocol):
    async def plan(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> HermesPlanResult: ...


class CodexExecutionPort(Protocol):
    async def start(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult: ...
    async def resume(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult: ...
    async def status(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult: ...
```

Test that state persists `command_accepted` before the fake external adapter is
called, result persistence occurs before acknowledgement, and replay returns
the stored result without a second adapter call.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_service.py`
Expected: FAIL on import.

- [ ] **Step 3: Implement operation dispatch without product imports**

Use an explicit `match command.payload.operation` and injected ports. Adapt
Codex operations to the existing reviewed `ExecutionProcess` and
gateway-fenced `CodexSupervisor`; do not create a parallel execution state
machine. Reject transitions not valid for the current gateway state. Do not
import `hermes-agent`, `minibook.swarm`, subprocess, or Docker modules.

- [ ] **Step 4: Prove idempotency and restart recovery**

Create a new service instance over the same fake durable state port and prove
the same command resumes/returns the original external job or session rather
than starting another effect.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_service.py`
Expected: PASS.
Commit: `feat: orchestrate agent runtime commands`

---

### Task 5: Persist runtime commands and grants through the gateway

**Files:**
- Create: `agenten/agent_runtime/gateway_client.py`
- Modify: `gateway/contracts.py`
- Modify: `gateway/store.py`
- Modify: `gateway/app.py`
- Create: `tests/gateway/test_agent_runtime.py`
- Create: `tests/agent_runtime/test_gateway_client.py`

**Interfaces:**
- Produces gateway operations `accept_runtime_command`, `record_capability_grant`, `record_runtime_result`, and `get_runtime_operation`.
- Consumes exact Task 1 models and existing authenticated/version-fenced gateway behavior.

- [ ] **Step 1: Write route and client contract tests**

```python
def test_runtime_command_is_idempotent_and_version_fenced(client) -> None:
    first = client.post("/v1/runtime/commands", json=COMMAND_FIXTURE)
    replay = client.post("/v1/runtime/commands", json=COMMAND_FIXTURE)
    assert first.status_code == replay.status_code == 202
    assert first.json()["operation_id"] == replay.json()["operation_id"]

    stale = copy.deepcopy(COMMAND_FIXTURE)
    stale["subject_version"] -= 1
    assert client.post("/v1/runtime/commands", json=stale).status_code == 409
```

- [ ] **Step 2: Run focused tests and observe missing routes**

Run: `python -m pytest -q --no-cov tests/gateway/test_agent_runtime.py tests/agent_runtime/test_gateway_client.py`
Expected: FAIL with 404/import errors.

- [ ] **Step 3: Add append-only gateway persistence**

Extend the existing gateway contracts and store to persist commands, grants,
heartbeats, and terminal results transactionally.
Unique command/event IDs implement idempotency; batch version and claim fence
prevent stale writers. The Minibook mirror receives only redacted events after
commit.

- [ ] **Step 4: Add MariaDB integration coverage**

Prove concurrent duplicate command submission, restart retrieval, stale grant,
and result-before-command rejection using `docker-compose.test.yml` or the
repository MariaDB test fixture. This gate must not silently skip.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest -q --no-cov tests/gateway/test_agent_runtime.py tests/agent_runtime/test_gateway_client.py`
Expected: PASS.
Commit: `feat: persist agent runtime operations`

---

### Task 6: Expose guarded tools to the AutoGen reasoning swarm

**Files:**
- Create: `agenten/agent_runtime/tools.py`
- Create: `agenten/agent_runtime/swarm.py`
- Modify: `agenten/runtime/bootstrap.py`
- Create: `tests/agent_runtime/test_swarm_tools.py`
- Create: `tests/agent_runtime/test_swarm_orchestration.py`

**Interfaces:**
- Produces tools `hermes.plan`, `hermes.design_agent`, `codex.run`, `codex.resume`, and `codex.status` registered through the existing tool/runtime seams.
- Consumes only gateway-projected ready tasks and `AgentRuntimeService`.

- [ ] **Step 1: Write failing tool-availability tests**

```python
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("project_received", {"hermes.plan"}),
        ("agent_design_requested", {"hermes.design_agent"}),
        ("subtask_ready", {"codex.run", "codex.status"}),
        ("redo", {"codex.resume", "codex.status"}),
        ("passed", set()),
    ],
)
def test_tools_are_derived_from_authoritative_state(state: str, expected: set[str]) -> None:
    assert available_tools(projected_state(state)) == expected
```

- [ ] **Step 2: Run tests and confirm missing tools**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_swarm_tools.py tests/agent_runtime/test_swarm_orchestration.py`
Expected: FAIL on import.

- [ ] **Step 3: Implement thin typed wrappers and deterministic guards**

Each wrapper validates its Pydantic input, submits one command to the service,
and returns a structured result. The reasoning model may select only from
`available_tools(state)` and cannot pass a raw capability list.

- [ ] **Step 4: Register the routed swarm adapter**

Keep `agenten/runtime/bootstrap.py` limited to registration and subscription.
Place next-action and retry-domain behavior in `agenten/agent_runtime/swarm.py`,
not in `agenten/orchestration/pipeline.py`.

- [ ] **Step 5: Test dependency order, crash recovery, and no direct delegation**

Use a fake reasoning selector and durable state port to prove one ready task at
a time per dependency chain, bounded overlap across independent planning,
Captain, and Codex lanes, no child-before-parent execution, no shared-worktree
writers, no worker-to-worker call, plan-version fencing, and stable recovery
after recreating the swarm object.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest -q --no-cov tests/agent_runtime/test_swarm_tools.py tests/agent_runtime/test_swarm_orchestration.py tests/test_autogen_bus_integration.py`
Expected: PASS.
Commit: `feat: expose runtime tools to autogen swarm`

---

### Task 7: Feed Hermes planning results into Captain decomposition

**Files:**
- Create: `agenten/planning/hermes_plan.py`
- Modify: `agenten/planning/captain_pipeline.py`
- Modify: `agenten/planning/factory.py`
- Create: `tests/planning/test_hermes_plan.py`
- Modify: `tests/planning/test_captain_pipeline.py`

**Interfaces:**
- Produces: `HermesPlanReader.read(plan_ref) -> ValidatedPlanningInput` and `CaptainPipeline.compile_from_plan(plan_input) -> PlanCompilationResult`; the result wraps an unchanged `CaptainCompiledPlan` with source plan digest/version metadata.
- Consumes: immutable `HermesPlanResult` and `AgentBlueprint` artifacts from Task 1.

- [ ] **Step 1: Write failing plan acceptance tests**

```python
async def test_captain_decomposes_validated_hermes_plan() -> None:
    source = validated_plan()
    result = await pipeline.compile_from_plan(source)
    assert result.source_plan_version == source.subject_version
    assert result.source_plan_digest == source.plan_ref.sha256
    assert result.compiled.batches


async def test_n8n_intent_becomes_released_capability_not_prompt_text() -> None:
    result = await pipeline.compile_from_plan(plan_with_n8n_blueprint())
    batch = next(batch for batch in result.compiled.batches if batch.target == "n8n")
    assert "n8n-builder" in batch.capability_tags
    assert "N8N_MCP_TOKEN" not in batch.model_dump_json()
```

- [ ] **Step 2: Run tests and confirm missing reader/entrypoint**

Run: `python -m pytest -q --no-cov tests/planning/test_hermes_plan.py tests/planning/test_captain_pipeline.py`
Expected: FAIL on import/attribute.

- [ ] **Step 3: Validate artifacts before decomposition**

Verify digest, schema version, correlation/project IDs, planner provenance,
blueprint references, and allowed integration intents. Convert the accepted
plan into Captain planning input without treating Minibook text as authority.

- [ ] **Step 4: Preserve existing direct-project-description compatibility**

Keep `CaptainPipeline.compile(project_description)` and
`run(project_description)` for deterministic/offline compatibility. Add
`compile_from_plan` without crossing the existing compile/review/publication
boundary or changing current callers until the new control plane is wired.

- [ ] **Step 5: Run and commit**

Run: `python -m pytest -q --no-cov tests/planning/test_hermes_plan.py tests/planning/test_captain_pipeline.py tests/planning/test_policy_integration.py`
Expected: PASS.
Commit: `feat: decompose validated hermes plans`

---

### Task 8: Implement Hermes planning and Codex adapters in the submodule

**Files (Hermes repository):**
- Create: `hermes_cli/captain_planner.py`
- Create: `hermes_cli/captain_worker.py`
- Create: `hermes_cli/captain_worker_contracts.py`
- Create: `optional-skills/captain-planner/SKILL.md`
- Create: `tests/hermes_cli/test_captain_planner.py`
- Create: `tests/hermes_cli/test_captain_worker.py`
- Modify: `hermes_cli/mcp_config.py`

**Interfaces:**
- Produces the Task 1 fixture contracts and uses existing Hermes Codex runtime/MCP configuration.
- Consumes plan/design commands and Codex work commands; Minibook interaction occurs through public API/tooling.

- [ ] **Step 1: Start from the dedicated Hermes worker plan**

Execute Tasks 1-4 of
`docs/superpowers/plans/2026-07-17-hermes-codex-n8n-worker.md` on a Hermes
submodule branch. Extend the contract with `hermes.plan` and
`hermes.design_agent`; do not copy Hermes modules into Captain.

- [ ] **Step 2: Write planner-skill acceptance tests**

Require the planner to post a correlated Minibook planning thread and return a
content-addressed `HermesPlanResult`. Require `hermes.design_agent` to emit
strict `AgentBlueprint` artifacts and reject credentials or holdouts.

- [ ] **Step 3: Bind `codex.run` to existing Hermes Codex runtime**

Start/resume/status must use Hermes' existing Codex session APIs. Add only the
small injected port needed for Captain envelopes; preserve normal Hermes use.

- [ ] **Step 4: Generate isolated n8n MCP configuration from the grant**

For `n8n-builder`, create per-run Codex MCP configuration referencing
`N8N_MCP_TOKEN` by environment-variable name. For `code-builder`, prove the
n8n server is absent. Never print or persist the token.

- [ ] **Step 5: Run Hermes gates and pin the reviewed commit**

Run focused Hermes tests, its full non-live gate, then the explicit live gate.
After review, update only the parent submodule pin and matching fixtures.
Commits: `feat: add captain planning runtime` and `feat: supervise captain codex work`.

---

### Task 9: Project planning and progress into Minibook

**Files:**
- Modify only files owned by `docs/superpowers/plans/2026-07-17-minibook-projection-boundary.md`
- Create: `tests/contracts/test_minibook_runtime_projection.py`
- Add: redacted fixtures under `tests/fixtures/contracts/`

**Interfaces:**
- Consumes redacted runtime/planning projection events after gateway commit.
- Produces rebuildable Minibook project, plan, blueprint, build, and validation views.

- [ ] **Step 1: Extend projection fixtures**

Add plan requested/published, blueprint published, Codex running/result, n8n
evidence reference, validation, and replanning events. Exclude prompt bodies,
raw transcripts, tokens, holdout bodies, and unrestricted paths.

- [ ] **Step 2: Prove ordered idempotent projection**

Replay the same event stream twice and assert one row/post per event ID and
monotonic subject versions. Deliver an out-of-order event and require
quarantine/retry without overwriting newer state.

- [ ] **Step 3: Preserve independent Minibook startup**

Run Minibook startup tests with Hermes, Codex, Docker, Forge, and Captain
unavailable. The core health endpoint must remain green.

- [ ] **Step 4: Run and commit**

Run: `python -m pytest -q --no-cov tests/contracts/test_minibook_runtime_projection.py minibook/tests`
Expected: PASS.
Commit: `feat: project agent runtime planning to minibook`

---

### Task 10: Prove the end-to-end orchestration and n8n lease boundary

**Files:**
- Create: `tests/integration/test_agent_runtime_control_plane.py`
- Create: `tests/live/test_agent_runtime_n8n_live.py`
- Create: `scripts/run_agent_runtime_demo.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DEMO.md`
- Modify: `docs/WORKSTREAMS.md`

**Interfaces:**
- Exercises Hermes plan -> Minibook -> Captain decomposition -> swarm -> Codex -> optional n8n MCP -> result -> validation.
- Produces one correlation-ID-indexed evidence manifest.

- [ ] **Step 1: Add deterministic cross-product orchestration test**

Use strict fake ports, real Captain planning/state code, and fixture envelopes.
Prove task dependency order, n8n grant derivation, redo/resume, replanning, and
restart idempotency without claiming external execution.

- [ ] **Step 2: Add live case A without n8n**

Hermes publishes a plan and agent blueprint, Captain decomposes it, and a real
Codex session changes only a disposable authorized worktree. Assert that the
Codex MCP tool list/config contains no n8n server.

- [ ] **Step 3: Add live case B with n8n**

Use an isolated workflow name/correlation ID. Require n8n MCP discovery,
workflow validation, publication/test, workflow ID, real call/execution ID,
and content digests. Do not start/stop n8n or mutate its volumes.

- [ ] **Step 4: Add failure and recovery beats**

Stop the control-plane process between command persistence and result
collection, restart it, and prove the same Hermes job/Codex session resumes.
Simulate n8n unavailability and prove `infrastructure_failed` does not consume
a Codex behavioral iteration. Cause one behavioral validation failure and
prove the same Codex session resumes with the validation artifact.

- [ ] **Step 5: Run all gates**

```powershell
python -m pytest -q -m "not live" -rs
python -m pytest -q -m live tests/live/test_agent_runtime_n8n_live.py -rs
python scripts/verify_submission.py
python -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
python -m compileall -q agenten blockchain chats config gateway
```

Expected: no failures; the dedicated live test has zero required skips. Report
all unrelated skips and dependency warnings separately.

- [ ] **Step 6: Document only verified behavior and commit**

Update the architecture and demo with actual commands, correlation IDs, and
known boundaries. Do not claim a live path from deterministic fixtures.
Commit: `docs: document agent runtime control plane`

---

## Integration order and merge gates

1. Merge Task 1 contracts and fixtures.
2. Merge Tasks 2-4 capability/prompt/service behavior.
3. Merge Task 5 gateway persistence after the MariaDB gate has zero skips.
4. Merge Tasks 6-7 swarm and Captain planning integration.
5. Merge reviewed Hermes changes and parent submodule pin from Task 8.
6. Merge Minibook projection from Task 9 independently.
7. Merge Task 10 only after both real Codex cases and the n8n live case pass.
8. Re-run the full gate on the latest `main`, then push `main` as source of truth.

Before every integration, fetch `origin`, inspect all worktrees, simulate the
merge, and check overlap in `README.md`, `.env.example`, `requirements.txt`,
`main.py`, `docs/ARCHITECTURE.md`, and `docs/WORKSTREAMS.md`. Never resolve
unrelated conflicts by choosing an entire side.
