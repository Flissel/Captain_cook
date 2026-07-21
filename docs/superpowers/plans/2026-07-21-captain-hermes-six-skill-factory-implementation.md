# Captain-Controlled Hermes Six-Skill Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver six versioned Hermes skills that Captain advances through discovery, Codex build, paid AutoGen execution, independent evaluation, bounded improvement, and authoritative feedback while enforcing job-wide USD cost and capability limits.

**Architecture:** Extend the existing Agent Factory additively with a V3 job execution policy, durable cost reservations, six content-addressed workflow artifacts, and a phase-to-skill sequence policy. Reuse the integrated `FactoryBuildAssignmentV1`, Hermes `FactoryWorker`, Candidate Evaluator, runtime Swarm, typed n8n broker, Gateway skill evaluation, and Captain release gate. Hermes runs only the skill sequence released for the current Factory phase; Captain/Gateway remains the sole lifecycle and promotion authority.

**Tech Stack:** Python 3.11, Pydantic v2, `Decimal`, AutoGen 0.7.5, Hermes CLI skills/external directories, Codex CLI/app-server, Context7, n8n MCP/REST, FastAPI, MariaDB, PowerShell, pytest.

**Design:** `docs/superpowers/specs/2026-07-21-captain-hermes-six-skill-factory-design.md`

## Global Constraints

- Source of truth for all six skills is `agenten/agent_factory/skills/`; do not copy or edit Hermes builtin skills.
- `hermes-agent/` remains an independent submodule. Reuse its existing `FactoryWorker`; modify and repin it only if a parent-side adapter cannot satisfy a tested contract.
- Captain/Gateway owns Factory jobs, leases, budget reservations, assertions, private holdouts, publication, and `ready_to_use`.
- `AgentFactoryJob` V1 and V2 remain parseable. Paid execution is introduced only through `captain.agent-factory-job.v3`.
- `max_cost_usd` is cumulative per Factory job and represented with `Decimal`; binary floats are rejected.
- `mode=demo` requires exactly one successful live run and can produce only `demo_ready`. `mode=release` requires three distinct successful live runs plus the existing recovery policy.
- A behavioral failure consumes one of at most five job iterations. An infrastructure recovery resumes the same attempt.
- Every skill result binds job, correlation, subject version, attempt, released skill digest, input digest, lease, and idempotency key.
- n8n is available only for compiled `integration_intent=n8n` work under the short-lived `n8n-builder`/`n8n-mcp` lease. Never expose a generic workflow-ID executor.
- Context7 is used for installed-version AutoGen documentation. Query n8n documentation only for a declared integration.
- Codex is the only component allowed to mutate the assigned build worktree. No prompt may grant a permanent approval bypass.
- `isolated_danger_full_access` requires a disposable worktree/container and explicit Captain policy; the default is `workspace_write`.
- Missing required tools, credentials, infrastructure, cost receipts, or provider evidence block promotion. Skips are never success.
- Preserve the user's modified `input.md`, untracked `TO_BE_BUILT.md`, unrelated worktrees, and VibeMind-owned n8n resources.
- Add a failing acceptance test before every behavioral implementation change. Keep commits narrow and Conventional Commit formatted.

## Existing Interfaces to Reuse

- `agenten.agent_factory.forge_contracts.FactoryBuildAssignmentV1`
- `hermes-agent/hermes_cli/factory_worker.py::FactoryWorker.execute`
- `agenten.agent_factory.skill_evaluation.TODO_TOOL.v1`
- `agenten.agent_factory.candidate_evaluation.FactoryCandidateEvaluator`
- `agenten.agent_factory.orchestration.HermesSkillEvaluationCoordinator`
- `agenten.agent_runtime.swarm.SwarmOrchestrator`
- `agenten.agent_runtime.tools` operations `codex.run` and `codex.resume`
- `agenten.agent_factory.n8n_tools.TypedN8nTool`
- `agenten.agent_runtime.n8n_mcp_broker`
- `gateway.store.GatewayStore` Factory repository and release-decision methods
- `agenten.agent_factory.outcome_contracts.CapabilityPackageManifestV1` and `ExecutionOutcomeV1`

## File Structure

### New focused modules

- `agenten/agent_factory/execution_policy.py`: V3 live/cost/sandbox policy.
- `agenten/agent_factory/execution_budget.py`: usage receipts, reservations, projections, and budget port.
- `agenten/agent_factory/skill_workflow_contracts.py`: six typed workflow artifacts and step identity.
- `agenten/agent_factory/skill_sequence.py`: pure Factory phase/attempt to skill sequence policy.
- `agenten/agent_factory/codebase_discovery.py`: semantic repository inventory behind injected readers/runners.
- `agenten/agent_factory/codex_brief.py`: bounded Codex prompt/context construction over `FactoryBuildAssignmentV1`.
- `agenten/agent_factory/team_execution.py`: deterministic preflight, budget reservation, real run, and execution evidence.
- `agenten/agent_factory/team_evaluation.py`: independent assertion/holdout evaluation and regression set.
- `agenten/agent_factory/improvement.py`: evidence-bound repair request generation.
- `agenten/agent_factory/factory_feedback.py`: final Hermes recommendation builder.
- `scripts/configure-hermes-factory-skills.ps1`: idempotent external-directory and bundle setup.
- `scripts/run-hermes-factory-live-gate.ps1`: explicit paid demo/release entrypoint.

### Existing files changed narrowly

- `agenten/agent_factory/contracts.py`: add V3 job and union parsing.
- `agenten/agent_factory/job_builder.py`: add V3 builder without changing V2 behavior.
- `agenten/agent_factory/leases.py`: derive exact V3 live capabilities per role and job.
- `agenten/agent_factory/hermes_cli.py`: resolve and invoke the phase-specific released skill sequence.
- `agenten/agent_factory/orchestration.py`: compose the step services behind ports; no new domain policy here.
- `agenten/agent_factory/state_machine.py`: accept `FactoryJob` union and attach feedback without adding six lifecycle phases.
- `gateway/contracts.py`, `gateway/store.py`, `gateway/app.py`, `gateway/factory_repository.py`: durable budget and workflow-evidence writes.
- `docs/AGENT_FACTORY_RUNBOOK.md`, `docs/ARCHITECTURE.md`: operator and ownership documentation.

## Parallel Execution Map

Use three isolated worktrees after Task 1 freezes Job V3. Merge in this order:

```text
Task 1 Job V3 contract
  |
  +-- Session A: Task 2 budget -> Task 9 Gateway persistence
  +-- Session B: Task 3 artifacts -> Task 4 skill packages -> Task 10 Hermes setup
  +-- Session C: Task 5 discovery -> Task 6 Codex/sequence
                                      |
                                      +-- Task 7 real execution
                                      +-- Task 8 evaluation/improvement/report
  all reviewed branches -> Task 11 deterministic and paid live integration
```

Each session starts with `git fetch origin --prune`, `git worktree list --porcelain`, and a clean dedicated feature worktree. Do not use the main working tree's `input.md` or `TO_BE_BUILT.md` as scratch files.

---

### Task 1: Add Factory Job V3 and Explicit Execution Policy

**Files:**
- Create: `agenten/agent_factory/execution_policy.py`
- Modify: `agenten/agent_factory/contracts.py:61-150`
- Modify: `agenten/agent_factory/job_builder.py:1-62`
- Modify: `agenten/agent_factory/leases.py:1-92`
- Modify: `agenten/agent_factory/__init__.py`
- Test: `tests/agent_factory/test_execution_policy.py`
- Test: `tests/agent_factory/test_contracts.py`
- Test: `tests/agent_factory/test_job_builder.py`
- Test: `tests/agent_factory/test_leases.py`

**Interfaces:**
- Consumes: `AgentFactoryJobV2`, `CompiledFactorySpecification`, `ArtifactRef`.
- Produces: `FactoryExecutionMode`, `FactorySandboxMode`, `FactoryLiveCapability`, `FactoryExecutionPolicyV1`, `AgentFactoryJobV3`, `build_factory_job_v3(...) -> AgentFactoryJobV3`, exact V3 Factory leases, and an expanded `FactoryJob` union.

- [ ] **Step 1: Write failing strict-policy tests**

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1


def test_release_policy_requires_three_runs_and_decimal_budget() -> None:
    policy = FactoryExecutionPolicyV1(
        schema="captain.factory-execution-policy.v1",
        mode="release",
        live_execution=True,
        max_cost_usd="5.00",
        max_runtime_seconds=900,
        required_live_runs=3,
        allowed_models=("approved-model-id",),
        live_capabilities=("model.invoke", "docker.run"),
        sandbox_mode="workspace_write",
    )
    assert policy.max_cost_usd == Decimal("5.00")


@pytest.mark.parametrize("value", [5.0, "NaN", "Infinity", "-1.00"])
def test_execution_policy_rejects_float_or_invalid_cost(value: object) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        FactoryExecutionPolicyV1.model_validate(
            {
                "schema": "captain.factory-execution-policy.v1",
                "mode": "release",
                "live_execution": True,
                "max_cost_usd": value,
                "max_runtime_seconds": 900,
                "required_live_runs": 3,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            }
        )


def test_demo_and_offline_policies_fail_closed() -> None:
    with pytest.raises(ValidationError, match="demo.*one"):
        FactoryExecutionPolicyV1.model_validate(
            release_payload() | {"mode": "demo", "required_live_runs": 3}
        )
    with pytest.raises(ValidationError, match="offline"):
        FactoryExecutionPolicyV1.model_validate(
            release_payload()
            | {
                "live_execution": False,
                "max_cost_usd": "5.00",
                "required_live_runs": 3,
            }
        )
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_execution_policy.py
```

Expected: collection fails with `ModuleNotFoundError: agenten.agent_factory.execution_policy`.

- [ ] **Step 3: Implement the frozen policy**

```python
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FactoryExecutionMode(str, Enum):
    DEMO = "demo"
    RELEASE = "release"


class FactorySandboxMode(str, Enum):
    WORKSPACE_WRITE = "workspace_write"
    ISOLATED_DANGER_FULL_ACCESS = "isolated_danger_full_access"


class FactoryLiveCapability(str, Enum):
    MODEL_INVOKE = "model.invoke"
    DOCKER_RUN = "docker.run"
    CAPTAIN_TEST_DATABASE = "database.captain_test"
    BROWSER_USE = "browser.use"
    COMPUTER_USE = "computer.use"


class FactoryExecutionPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.factory-execution-policy.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    mode: FactoryExecutionMode
    live_execution: bool
    max_cost_usd: Decimal
    max_runtime_seconds: int = Field(ge=1, le=86400, strict=True)
    required_live_runs: int = Field(ge=0, le=3, strict=True)
    allowed_models: tuple[str, ...] = ()
    live_capabilities: tuple[FactoryLiveCapability, ...] = ()
    sandbox_mode: FactorySandboxMode = FactorySandboxMode.WORKSPACE_WRITE

    @field_validator("max_cost_usd", mode="before")
    @classmethod
    def require_decimal_string(cls, value: object) -> Decimal:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise TypeError("max_cost_usd must be a decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("max_cost_usd must be finite") from exc
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise ValueError("max_cost_usd must be finite, non-negative, and use cents")
        return amount

    @model_validator(mode="after")
    def require_mode_consistency(self) -> "FactoryExecutionPolicyV1":
        if not self.live_execution:
            if (
                self.max_cost_usd != 0
                or self.required_live_runs != 0
                or self.allowed_models
                or self.live_capabilities
            ):
                raise ValueError("offline execution requires zero live budget and no models")
            return self
        if (
            self.max_cost_usd <= 0
            or not self.allowed_models
            or FactoryLiveCapability.MODEL_INVOKE not in self.live_capabilities
        ):
            raise ValueError("live execution requires a positive budget and allowed models")
        required = 1 if self.mode is FactoryExecutionMode.DEMO else 3
        if self.required_live_runs != required:
            raise ValueError(f"{self.mode.value} execution requires exactly {required} live run(s)")
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must not contain duplicates")
        if len(self.live_capabilities) != len(set(self.live_capabilities)):
            raise ValueError("live_capabilities must not contain duplicates")
        return self
```

- [ ] **Step 4: Add V3 without changing V1/V2 parsing**

Add `AgentFactoryJobV3(AgentFactoryJobV2)` with schema
`captain.agent-factory-job.v3` and `execution_policy: FactoryExecutionPolicyV1`.
Expand:

```python
FactoryJob = AgentFactoryJob | AgentFactoryJobV2 | AgentFactoryJobV3


def parse_factory_job(value: object) -> FactoryJob:
    if not isinstance(value, dict):
        raise ValueError("factory job payload must be an object")
    schema = value.get("schema", value.get("schema_name"))
    models = {
        "captain.agent-factory-job.v1": AgentFactoryJob,
        "captain.agent-factory-job.v2": AgentFactoryJobV2,
        "captain.agent-factory-job.v3": AgentFactoryJobV3,
    }
    try:
        return models[schema].model_validate(value)
    except KeyError as exc:
        raise ValueError("unsupported factory job schema") from exc
```

Keep the existing `max_behavioral_iterations` field on the job; do not duplicate it in the execution policy.

- [ ] **Step 5: Add a separate V3 builder and preserve the V2 builder**

```python
def build_factory_job_v3(
    compiled: CompiledFactorySpecification,
    *,
    correlation_id: UUID,
    now: datetime,
    execution_policy: FactoryExecutionPolicyV1,
) -> AgentFactoryJobV3:
    v2 = build_factory_job(
        compiled,
        correlation_id=correlation_id,
        now=now,
        wall_clock_budget_seconds=execution_policy.max_runtime_seconds,
    )
    policy_json = _canonical_json(
        execution_policy.model_dump(mode="json", by_alias=True)
    )
    policy_digest = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
    identity = f"factory-job-v3|{v2.job_id}|{policy_digest}"
    payload = v2.model_dump(mode="json", by_alias=True)
    payload["schema"] = "captain.agent-factory-job.v3"
    payload["job_id"] = str(uuid5(_JOB_NAMESPACE, identity))
    payload["event_id"] = str(uuid5(correlation_id, identity))
    payload["execution_policy"] = execution_policy.model_dump(mode="json", by_alias=True)
    return AgentFactoryJobV3.model_validate(payload)
```

Add tests proving byte-stable V3 identity, different policy/different job identity, deadline binding, V1/V2 compatibility, unknown-field rejection, and that a float cost cannot enter through `model_validate`.

- [ ] **Step 6: Derive exact V3 live capabilities into the Real Case Tester lease**

For V1/V2 jobs, keep the current fixed profile capabilities. For V3 jobs, only the Real Case Tester receives the base `FACTORY_REAL_CASE_TESTER` capabilities plus `job.execution_policy.live_capabilities`. Architect, Tool Integrator, and Quality Warden remain unchanged. `validate_factory_lease` must recompute the same exact set from the job; extra capabilities fail closed.

```python
def expected_factory_capabilities(
    job: FactoryJob,
    role: FactoryRole,
    profile: CapabilityProfile,
) -> frozenset[str]:
    expected = set(PROFILE_CAPABILITIES[profile])
    if isinstance(job, AgentFactoryJobV3) and role is FactoryRole.REAL_CASE_TESTER:
        expected.update(item.value for item in job.execution_policy.live_capabilities)
    return frozenset(expected)
```

Add tests proving a release job can explicitly lease model/Docker/browser/computer/`captain_test`, an offline job cannot, a Tool Integrator cannot inherit them, and one injected extra capability makes lease validation fail.

- [ ] **Step 7: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_execution_policy.py tests/agent_factory/test_contracts.py tests/agent_factory/test_job_builder.py tests/agent_factory/test_leases.py
```

Expected: all selected tests pass with no live calls.

- [ ] **Step 8: Commit the contract**

```powershell
git add agenten/agent_factory/execution_policy.py agenten/agent_factory/contracts.py agenten/agent_factory/job_builder.py agenten/agent_factory/leases.py agenten/agent_factory/__init__.py tests/agent_factory/test_execution_policy.py tests/agent_factory/test_contracts.py tests/agent_factory/test_job_builder.py tests/agent_factory/test_leases.py
git commit -m "feat: define factory live execution policy"
```

### Task 2: Implement Job-Wide USD Reservation and Usage Receipts

**Files:**
- Create: `agenten/agent_factory/execution_budget.py`
- Test: `tests/agent_factory/test_execution_budget.py`

**Interfaces:**
- Consumes: `AgentFactoryJobV3`, UTC timestamps, provider usage data.
- Produces: `FactoryUsageReceiptV1`, `FactoryBudgetReservationV1`, `FactoryBudgetProjection`, `FactoryBudgetPort`, `InMemoryFactoryBudgetLedger`, `reserve(...)`, `record_usage(...)`, and `release(...)`.

- [ ] **Step 1: Write atomic reservation and exhaustion tests**

```python
from decimal import Decimal

import pytest

from agenten.agent_factory.execution_budget import (
    BudgetExhausted,
    FactoryUsageReceiptV1,
    InMemoryFactoryBudgetLedger,
)


def test_reservation_prevents_concurrent_overspend(job_v3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    first = ledger.reserve(job_v3, attempt=1, requested_usd=Decimal("3.00"), now=NOW)
    with pytest.raises(BudgetExhausted):
        ledger.reserve(job_v3, attempt=1, requested_usd=Decimal("3.00"), now=NOW)
    assert ledger.projection(job_v3.job_id).reserved_usd == Decimal("3.00")
    assert first.job_id == job_v3.job_id


def test_known_usage_consumes_reservation_and_replay_is_idempotent(job_v3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, cost_usd="1.25")
    )
    assert ledger.record_usage(job_v3, reservation, receipt).replayed is False
    assert ledger.record_usage(job_v3, reservation, receipt).replayed is True
    projection = ledger.projection(job_v3.job_id)
    assert projection.consumed_usd == Decimal("1.25")
    assert projection.reserved_usd == Decimal("0.00")


def test_unknown_or_changed_usage_never_counts_as_success(job_v3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    with pytest.raises(ValueError, match="known USD cost"):
        FactoryUsageReceiptV1.model_validate(usage_payload(reservation, cost_usd=None))
```

- [ ] **Step 2: Run the failing test**

Run: `.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_execution_budget.py`

Expected: missing-module failure.

- [ ] **Step 3: Implement frozen receipts and the port**

Use UUID identities, `Decimal` parsing identical to Task 1, UTC validation, and strict model fields:

```python
class FactoryUsageReceiptV1(_FrozenContract):
    schema_name: Literal["captain.factory-usage-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    receipt_id: UUID
    reservation_id: UUID
    job_id: UUID
    correlation_id: UUID
    attempt: int = Field(ge=1, le=5, strict=True)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_units: int = Field(ge=0, strict=True)
    output_units: int = Field(ge=0, strict=True)
    cost_usd: Decimal
    started_at: datetime
    ended_at: datetime
    evidence_ref: ArtifactRef


class FactoryBudgetReservationV1(_FrozenContract):
    schema_name: Literal["captain.factory-budget-reservation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    reservation_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    requested_usd: Decimal
    reserved_at: datetime
    expires_at: datetime
    status: Literal["active"] = "active"


class FactoryBudgetWriteReceipt(_FrozenContract):
    event_id: UUID
    job_id: UUID
    replayed: bool


class FactoryBudgetProjection(_FrozenContract):
    job_id: UUID
    limit_usd: Decimal
    consumed_usd: Decimal
    reserved_usd: Decimal
    remaining_usd: Decimal
    active_reservation_ids: tuple[UUID, ...] = ()


class FactoryBudgetPort(Protocol):
    def reserve(
        self,
        job: AgentFactoryJobV3,
        *,
        attempt: int,
        requested_usd: Decimal,
        now: datetime,
    ) -> FactoryBudgetReservationV1: ...

    def record_usage(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        receipt: FactoryUsageReceiptV1,
    ) -> FactoryBudgetWriteReceipt: ...

    def release(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        *,
        now: datetime,
        reason: Literal["provider_failed", "cancelled", "unused"],
    ) -> FactoryBudgetWriteReceipt: ...

    def projection(self, job_id: UUID) -> FactoryBudgetProjection: ...
```

`FactoryUsageReceiptV1` contains `receipt_id`, `reservation_id`, job/correlation/attempt, provider, allowed model, input/output token or unit counts, exact `cost_usd`, started/ended UTC times, and a redacted evidence ref. Reject blank providers/models, negative units, non-allowed models, receipts outside the reservation window, duplicate IDs with changed content, and totals above the reservation or job budget.

- [ ] **Step 4: Implement the deterministic ledger**

Keep an append-only event tuple and derive `FactoryBudgetProjection` on every read. The in-memory implementation is for unit tests only; its docstring must state that production uses the Gateway implementation from Task 9. Use a lock around reserve/record/release to make the overspend test deterministic.

- [ ] **Step 5: Run focused tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_execution_budget.py
git add agenten/agent_factory/execution_budget.py tests/agent_factory/test_execution_budget.py
git commit -m "feat: enforce factory USD execution budgets"
```

### Task 3: Define the Six Workflow Artifact Contracts

**Files:**
- Create: `agenten/agent_factory/skill_workflow_contracts.py`
- Modify: `agenten/agent_factory/__init__.py`
- Test: `tests/agent_factory/test_skill_workflow_contracts.py`

**Interfaces:**
- Consumes: `ReleasedHermesSkill`, `FactoryLease`, `FactoryBuildAssignmentV1`, `ExecutionOutcomeV1`, `AssertionOutcome`, `ToolGapMarker`, `ArtifactRef`, and `FactoryUsageReceiptV1` refs.
- Produces: `FactorySkillStep`, `FactorySkillInvocationV1`, `CodebaseInventoryV1`, `CodexBuildBriefV1`, `TeamExecutionEvidenceV1`, `TeamEvaluationV1`, `CandidateRevisionV1`, and `FactoryFeedbackV1`.

- [ ] **Step 1: Write schema, binding, and redaction tests**

```python
@pytest.mark.parametrize(
    "model,payload",
    [
        (CodebaseInventoryV1, inventory_payload()),
        (CodexBuildBriefV1, brief_payload()),
        (TeamExecutionEvidenceV1, execution_payload()),
        (TeamEvaluationV1, evaluation_payload()),
        (CandidateRevisionV1, revision_payload()),
        (FactoryFeedbackV1, feedback_payload()),
    ],
)
def test_workflow_artifacts_are_frozen_strict_and_round_trip(model, payload) -> None:
    parsed = model.model_validate(payload)
    assert model.model_validate(parsed.model_dump(mode="json", by_alias=True)) == parsed
    with pytest.raises(ValidationError):
        model.model_validate(payload | {"unknown": True})


@pytest.mark.parametrize("field", ["api_key", "authorization", "raw_prompt", "transcript"])
def test_workflow_artifacts_reject_private_fields(field: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        CodebaseInventoryV1.model_validate(inventory_payload() | {field: "secret"})


def test_step_result_must_match_invocation_identity() -> None:
    invocation = invocation_payload(step="execute_team")
    with pytest.raises(ValidationError, match="invocation"):
        TeamExecutionEvidenceV1.model_validate(
            execution_payload() | {"invocation_id": str(uuid4())}
        )
```

- [ ] **Step 2: Define a common invocation envelope**

```python
class FactorySkillStep(str, Enum):
    DISCOVER = "discover"
    BRIEF_CODEX = "brief_codex"
    EXECUTE_TEAM = "execute_team"
    EVALUATE_TEAM = "evaluate_team"
    IMPROVE_TEAM = "improve_team"
    REPORT_CAPTAIN = "report_captain"


class FactorySkillInvocationV1(_FrozenContract):
    schema_name: Literal["captain.factory-skill-invocation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    invocation_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    step: FactorySkillStep
    released_skill: ReleasedHermesSkill
    input_ref: ArtifactRef
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    lease: FactoryLease
    idempotency_key: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_exact_bindings(self) -> "FactorySkillInvocationV1":
        if self.input_ref.sha256 != self.input_sha256:
            raise ValueError("skill invocation input digest mismatch")
        if self.lease.job_id != self.job_id or self.lease.correlation_id != self.correlation_id:
            raise ValueError("skill invocation lease mismatch")
        if self.lease.subject_version != self.subject_version or self.lease.attempt != self.attempt:
            raise ValueError("skill invocation version or attempt mismatch")
        return self
```

- [ ] **Step 3: Define the six result models**

Use a shared base carrying `schema`, invocation/job/correlation/version/attempt, `occurred_at`, `producer="hermes"`, `artifact_ref`, and `evidence_refs`. Exact payload responsibilities:

- `CodebaseInventoryV1`: inspected revision, source refs, reusable component IDs, entrypoint/test/schema refs, AutoGen version, documentation refs, tool-catalog matches, and gap refs.
- `CodexBuildBriefV1`: existing `FactoryBuildAssignmentV1`, prompt ref, context refs, authorized path roots, required test command IDs, and forbidden effect IDs.
- `TeamExecutionEvidenceV1`: run number, candidate ref, `ExecutionOutcomeV1`, usage-receipt refs, handoff/tool/workflow evidence refs, termination reason, and `succeeded|failed|unresolved`.
- `TeamEvaluationV1`: assertion outcomes, holdout receipt refs, deterministic check refs, optional judge ref, prior-green regression IDs, cost/latency summary refs, failure class, and recommendation.
- `CandidateRevisionV1`: parent/new candidate refs, failed assertion IDs, changed component enum values, regression assertion IDs, and Codex session ref.
- `FactoryFeedbackV1`: exact recommendation enum, assertion/tool-gap/evidence refs, and redacted reason codes.

Model validators enforce unique IDs/refs, exact invocation identity, only Captain assertion IDs, no raw prompt/transcript/holdout bodies, successful execution only with passed runtime outcome, and `PROMOTE_CANDIDATE` only when no required unresolved `TODO_TOOL.v1` remains.

- [ ] **Step 4: Run contract tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_skill_workflow_contracts.py tests/agent_factory/test_outcome_contracts.py tests/agent_factory/test_skill_evaluation_contracts.py
git add agenten/agent_factory/skill_workflow_contracts.py agenten/agent_factory/__init__.py tests/agent_factory/test_skill_workflow_contracts.py
git commit -m "feat: define six-skill factory artifacts"
```

### Task 4: Author and Validate the Six Hermes Skill Packages

**Files:**
- Create: `agenten/agent_factory/skills/captain-factory-discover/SKILL.md`
- Create: `agenten/agent_factory/skills/captain-factory-discover/references/output-schema.md`
- Create: `agenten/agent_factory/skills/captain-factory-brief-codex/SKILL.md`
- Create: `agenten/agent_factory/skills/captain-factory-brief-codex/templates/codex-assignment.md`
- Create: `agenten/agent_factory/skills/captain-factory-execute-team/SKILL.md`
- Create: `agenten/agent_factory/skills/captain-factory-execute-team/references/evidence-contract.md`
- Create: `agenten/agent_factory/skills/captain-factory-evaluate-team/SKILL.md`
- Create: `agenten/agent_factory/skills/captain-factory-evaluate-team/references/rubric.md`
- Create: `agenten/agent_factory/skills/captain-factory-improve-team/SKILL.md`
- Create: `agenten/agent_factory/skills/captain-factory-improve-team/templates/repair-assignment.md`
- Create: `agenten/agent_factory/skills/captain-factory-report-captain/SKILL.md`
- Create: `agenten/agent_factory/skills/captain-factory-report-captain/references/recommendations.md`
- Create: `agenten/agent_factory/skills/captain-agent-factory-loop/bundle.yaml`
- Modify: `tests/agent_factory/test_factory_skill_package.py`

**Interfaces:**
- Consumes: schemas from Task 3 and existing Hermes builtin skills `codex`, `plan`, `test-driven-development`, `systematic-debugging`, `requesting-code-review`, `github-pr-workflow`, `github-repo-management`, and `architecture-diagram`.
- Produces: six agentskills.io-compatible, digestible skill directories and a non-authoritative bundle manifest.

- [ ] **Step 1: Use the skill-authoring sub-skill and write failing package tests**

Implementation workers must read the system `skill-creator` instructions before editing these files. Parameterize the existing package test:

```python
SKILLS = {
    "captain-factory-discover": ("CodebaseInventoryV1", "do not change code"),
    "captain-factory-brief-codex": ("CodexBuildBriefV1", "codex.run"),
    "captain-factory-execute-team": ("TeamExecutionEvidenceV1", "max_cost_usd"),
    "captain-factory-evaluate-team": ("TeamEvaluationV1", "do not repair"),
    "captain-factory-improve-team": ("CandidateRevisionV1", "prior green"),
    "captain-factory-report-captain": ("FactoryFeedbackV1", "Captain decides"),
}


@pytest.mark.parametrize("skill_name,required", SKILLS.items())
def test_factory_workflow_skill_is_valid_and_safe(skill_name, required) -> None:
    root = Path("agenten/agent_factory/skills") / skill_name
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == skill_name
    assert all(phrase.lower() in text.lower() for phrase in required)
    assert "--yolo" not in text
    assert "api_key=" not in text.lower()
    assert "bearer " not in text.lower()
```

- [ ] **Step 2: Write concise skill instructions with exact stop conditions**

Every `SKILL.md` must contain frontmatter `name` and a trigger-focused `description`, then these shared rules:

```markdown
Use only the supplied captain.factory-skill-invocation.v1. Verify the job,
released skill digest, input digest, active role lease, attempt, and
idempotency key before any effect. Return exactly the declared typed artifact.
Stop on stale authority, digest mismatch, terminal state, missing required
evidence, or an effect outside the lease. Never publish a skill, write the
ledger, weaken Captain assertions, expose secrets, or claim ready_to_use.
```

Add step-specific instructions exactly matching the approved design:

- Discover: semantic `rg`/imports/entrypoints/tests/schema/tool search; builtin `codebase-inspection` is metrics only; Context7 AutoGen docs; n8n docs only for declared integrations; no writes.
- Brief Codex: goal/outcome, worktree, paths, architecture constraints, test IDs, tool policy, model/budget limits, evidence requirements; use `codex.run` and digest-bound `codex.resume` only.
- Execute Team: fresh workspace, deterministic preflight before paid calls, Captain budget reservation, real team run, cost receipt, typed n8n evidence, no repairs.
- Evaluate Team: deterministic assertions first, private holdouts, n8n/tool evidence, conversation/handoff/memory/termination/cost checks, optional judge after gates, no code changes.
- Improve Team: only after `IMPROVEMENT_REQUESTED`, change evidence-implicated components, rerun every prior-green assertion, produce child candidate, never promote.
- Report Captain: bind all evidence, choose exactly one approved recommendation, and state that Captain recomputes the lifecycle decision.

- [ ] **Step 3: Add the bundle manifest**

```yaml
name: captain-agent-factory-loop
description: Captain-controlled six-skill AutoGen factory workflow
skills:
  - captain-factory-discover
  - captain-factory-brief-codex
  - captain-factory-execute-team
  - captain-factory-evaluate-team
  - captain-factory-improve-team
  - captain-factory-report-captain
instruction: Use only the step released by the current Captain invocation.
```

The runtime uses individual released skills; the bundle is only for operator inspection and interactive rehearsal.

- [ ] **Step 4: Validate structure, references, and token size**

Run the system skill validator against each directory and extend tests to assert that every referenced file exists, no skill embeds executable secrets, and each `SKILL.md` remains concise enough for progressive disclosure.

```powershell
$validator = 'C:\Users\User\.codex\skills\.system\skill-creator\scripts\quick_validate.py'
Get-ChildItem agenten\agent_factory\skills -Directory |
  Where-Object Name -Like 'captain-factory-*' |
  ForEach-Object { py -3.11 $validator $_.FullName }
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_factory_skill_package.py
```

Expected: six validators pass and all package tests pass.

- [ ] **Step 5: Commit the skill source**

```powershell
git add agenten/agent_factory/skills tests/agent_factory/test_factory_skill_package.py
git commit -m "feat: add captain factory workflow skills"
```

### Task 5: Implement Semantic Codebase Discovery

**Files:**
- Create: `agenten/agent_factory/codebase_discovery.py`
- Test: `tests/agent_factory/test_codebase_discovery.py`

**Interfaces:**
- Consumes: `FactorySkillInvocationV1(step=discover)`, repository/worktree reader, allowlisted command runner, `CompiledFactorySpecification`, documentation/tool catalog ports.
- Produces: `CodebaseDiscoveryService.discover(...) -> CodebaseInventoryV1`.

- [ ] **Step 1: Write fixture-repository acceptance tests**

Build a temporary repository containing an AutoGen entrypoint, model client, prompt file, tests, typed tool, optional n8n workflow, and an unrelated secret-like `.env`. Prove:

```python
def test_discovery_finds_semantic_reuse_without_reading_secrets(tmp_path: Path) -> None:
    repo = factory_repo_fixture(tmp_path, integration_intent="none")
    inventory = service(repo).discover(invocation(repo), compiled_spec(repo))
    assert "agenten.workflows.existing_team" in inventory.reusable_component_ids
    assert "tests/test_existing_team.py" in inventory.safe_relative_test_paths
    assert inventory.n8n_documentation_refs == ()
    assert all(".env" not in ref.uri for ref in inventory.evidence_refs)


def test_n8n_docs_are_queried_only_for_declared_integration(tmp_path: Path) -> None:
    repo = factory_repo_fixture(tmp_path, integration_intent="n8n")
    docs = RecordingDocumentationPort()
    service(repo, docs=docs).discover(invocation(repo), compiled_spec(repo))
    assert [query.ecosystem for query in docs.queries] == ["autogen", "n8n"]
```

- [ ] **Step 2: Define injected read-only ports**

```python
class WorktreeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    relative_name: str = Field(min_length=1)
    branch: str | None = None
    dirty: bool


class SourceMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str = Field(min_length=1)
    line: int = Field(ge=1, strict=True)
    symbol: str | None = None
    content_sha256: str = Field(pattern=SHA256_PATTERN)


class RepositoryInspectionPort(Protocol):
    def revision(self) -> str: ...
    def worktrees(self) -> tuple[WorktreeObservation, ...]: ...
    def search(self, pattern: str, globs: tuple[str, ...]) -> tuple[SourceMatch, ...]: ...
    def read_text(self, relative_path: PurePosixPath) -> str: ...


class CodebaseDiscoveryService:
    def __init__(self, repository, documentation, tool_catalog, evidence_store) -> None:
        self._repository = repository
        self._documentation = documentation
        self._tool_catalog = tool_catalog
        self._evidence_store = evidence_store
```

The concrete repository adapter may call `rg` through an argv-based runner. It must exclude `.git`, `.env*`, `.venv`, `node_modules`, build outputs, caches, artifacts containing secrets, and paths outside the assigned worktree. Never construct a shell string from task input.

- [ ] **Step 3: Implement deterministic discovery categories**

Search for entrypoints, AutoGen imports/classes, model clients, system/user prompts, memory, termination conditions, handoffs, typed tools, n8n intent/contracts, tests, schemas, architecture docs, and unresolved `TODO_TOOL.v1`. Sort and deduplicate observations before sealing them. Store only safe relative paths, symbols, version IDs, and digests.

Add a required-gap fixture whose requested API is absent. Require the inventory to bind the blocked assertion and emit one to three concrete implementation options in existing `TODO_TOOL.v1` form: reuse a released tool, implement a typed local adapter with schema/auth/health/idempotency tests, or provide a typed n8n integration when external integration intent exists. An optional gap must remain separately classified.

- [ ] **Step 4: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_codebase_discovery.py
git add agenten/agent_factory/codebase_discovery.py tests/agent_factory/test_codebase_discovery.py
git commit -m "feat: inspect reusable factory code semantically"
```

### Task 6: Build Codex Briefs and Select the Phase-Specific Skill Sequence

**Files:**
- Create: `agenten/agent_factory/codex_brief.py`
- Create: `agenten/agent_factory/skill_sequence.py`
- Modify: `agenten/agent_factory/hermes_cli.py:32-85,214-261`
- Modify: `agenten/agent_factory/orchestration.py:482-540`
- Test: `tests/agent_factory/test_codex_brief.py`
- Test: `tests/agent_factory/test_skill_sequence.py`
- Modify: `tests/agent_factory/test_hermes_cli.py`

**Interfaces:**
- Consumes: `CodebaseInventoryV1`, existing `FactoryBuildAssignmentV1`, Factory projection/action, released six-skill catalog, and role lease.
- Produces: `CodexBriefBuilder.build(...) -> CodexBuildBriefV1`, `SkillSequencePolicy.steps_for(...)`, and a Hermes adapter that runs exactly the released sequence.

- [ ] **Step 1: Write prompt-contract and sequence tests**

```python
def test_codex_brief_contains_goal_gates_and_only_opaque_refs() -> None:
    brief = builder().build(invocation(), assignment(), inventory(), policy())
    rendered = artifact_store.read(brief.prompt_ref)
    assert "Goal" in rendered
    assert "Measurable outcome" in rendered
    assert "prior green assertions" in rendered
    assert "max_cost_usd" in rendered
    assert "C:\\Users" not in rendered
    assert "OPENAI_API_KEY" not in rendered


@pytest.mark.parametrize(
    "role,attempt,expected",
    [
        (FactoryRole.AGENT_ARCHITECT, 1, (FactorySkillStep.DISCOVER,)),
        (FactoryRole.TOOL_INTEGRATOR, 1, (FactorySkillStep.BRIEF_CODEX,)),
        (
            FactoryRole.TOOL_INTEGRATOR,
            2,
            (FactorySkillStep.IMPROVE_TEAM, FactorySkillStep.BRIEF_CODEX),
        ),
        (FactoryRole.REAL_CASE_TESTER, 1, (FactorySkillStep.EXECUTE_TEAM,)),
        (
            FactoryRole.QUALITY_WARDEN,
            1,
            (FactorySkillStep.EVALUATE_TEAM, FactorySkillStep.REPORT_CAPTAIN),
        ),
    ],
)
def test_role_attempt_maps_to_exact_skill_sequence(role, attempt, expected) -> None:
    assert SkillSequencePolicy().steps_for(role=role, attempt=attempt) == expected
```

- [ ] **Step 2: Implement the brief builder over V1 assignment**

Do not create a duplicate build-assignment schema. `CodexBuildBriefV1` wraps the existing `FactoryBuildAssignmentV1` and adds sealed prompt/context refs. Render a deterministic template containing goal, outcome, selected reusable components, authorized workspace roots, exact command/test IDs, architecture rules, tool-resolution order, AutoGen/n8n docs refs, live/sandbox policy, artifact requirements, and forbidden effects.

- [ ] **Step 3: Implement the pure skill sequence policy**

The policy above preserves current Factory phases. Skill 5 runs only on Tool Integrator attempts greater than one, after Captain emitted `IMPROVEMENT_REQUESTED`. Skill 6 runs after Skill 4 under the same still-active Quality Warden lease; Captain records only the resulting `QUALITY_REVIEWED` block and then recomputes the next action.

- [ ] **Step 4: Replace the single `skill_path` setting with a released catalog**

```python
@dataclass(frozen=True)
class HermesCliSettings:
    executable: str = "hermes"
    skill_root: Path = Path("agenten/agent_factory/skills")
    timeout_seconds: int = 900
    evidence_root: Path = Path("artifacts/agent-factory/evidence")
    released_skill_root: Path = Path("agenten/agent_factory/released-skills")


class ReleasedFactorySkillCatalog(Protocol):
    def released_for(
        self, job: FactoryJob, step: FactorySkillStep
    ) -> ReleasedHermesSkill: ...
```

For every step, resolve the skill directory beneath `skill_root`, hash the complete deterministic directory manifest, compare it with the Captain release, construct `FactorySkillInvocationV1`, and invoke Hermes. Accumulate only typed step artifacts. Stop before the next skill if the prior result is failed, blocked, unresolved, stale, or digest-mismatched.

- [ ] **Step 5: Keep prompt safety and process-tree cancellation**

Reuse `_validate_serialized_prompt_value`, async timeout, and process-tree cancellation. Pass the invocation as canonical JSON plus the slash skill name. Do not embed API keys, endpoints, absolute paths, raw prompts, or holdouts. Add tests for changed skill bytes, wrong step, expired lease, timeout, partial sequence, and replay.

- [ ] **Step 6: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_codex_brief.py tests/agent_factory/test_skill_sequence.py tests/agent_factory/test_hermes_cli.py tests/agent_factory/test_orchestration.py
git add agenten/agent_factory/codex_brief.py agenten/agent_factory/skill_sequence.py agenten/agent_factory/hermes_cli.py agenten/agent_factory/orchestration.py tests/agent_factory/test_codex_brief.py tests/agent_factory/test_skill_sequence.py tests/agent_factory/test_hermes_cli.py
git commit -m "feat: dispatch phase scoped factory skills"
```

### Task 7: Execute Real AutoGen Teams Under the Cost Lease

**Files:**
- Create: `agenten/agent_factory/team_execution.py`
- Modify: `agenten/agent_factory/candidate_evaluation.py:118-260`
- Test: `tests/agent_factory/test_team_execution.py`
- Modify: `tests/agent_factory/test_candidate_evaluation.py`

**Interfaces:**
- Consumes: `FactorySkillInvocationV1(step=execute_team)`, sealed candidate, Real Case Tester lease, private holdout resolver, execution policy, `FactoryBudgetPort`, and injected real/deterministic team runner.
- Produces: `TeamExecutionService.execute(...) -> TeamExecutionEvidenceV1`.

- [ ] **Step 1: Write preflight-before-cost and real-run tests**

```python
@pytest.mark.asyncio
async def test_failed_preflight_never_reserves_or_calls_provider() -> None:
    service, budget, runner = execution_service(preflight_status="failed")
    evidence = await service.execute(invocation(), candidate(), case_ref())
    assert evidence.status == "failed"
    assert budget.reservations == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_live_run_records_cost_handoffs_and_termination() -> None:
    service, budget, runner = execution_service(
        run=successful_swarm_run(cost_usd="0.42")
    )
    evidence = await service.execute(invocation(), candidate(), case_ref())
    assert evidence.status == "succeeded"
    assert len(evidence.usage_receipt_refs) == 1
    assert evidence.termination_reason == "task_completed"
    assert runner.last_manifest.conversation_pattern == "swarm"


@pytest.mark.asyncio
async def test_n8n_execution_requires_scoped_id_and_matching_digest() -> None:
    with pytest.raises(ValueError, match="n8n execution evidence"):
        await execution_service(run=n8n_run_without_execution_id()).execute(
            n8n_invocation(), n8n_candidate(), case_ref()
        )
```

- [ ] **Step 2: Define the runner and preflight ports**

```python
class FactoryTeamRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["succeeded", "failed", "unresolved"]
    runtime_result: AgentRuntimeResult
    execution_outcome: ExecutionOutcomeV1
    usage_receipts: tuple[FactoryUsageReceiptV1, ...] = ()
    handoff_evidence_refs: tuple[ArtifactRef, ...] = ()
    tool_evidence_refs: tuple[ArtifactRef, ...] = ()
    workflow_evidence_refs: tuple[ArtifactRef, ...] = ()
    termination_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_receipt_for_success(self) -> "FactoryTeamRunResult":
        if self.status == "succeeded" and not self.usage_receipts:
            raise ValueError("successful live run requires a usage receipt")
        return self


class FactoryTeamRunner(Protocol):
    async def run(
        self,
        *,
        candidate: ResolvedFactoryCandidate,
        case_ref: PrivateHoldoutRef,
        lease: FactoryLease,
        allowed_models: tuple[str, ...],
        max_seconds: int,
    ) -> FactoryTeamRunResult: ...


class CandidatePreflightPort(Protocol):
    def validate(
        self, candidate: ResolvedFactoryCandidate, max_seconds: float
    ) -> FactoryCandidateEvaluationResult: ...
```

Adapt the existing Candidate Evaluator for compile/import/build preflight. The real runner starts the candidate's sealed entrypoint in the evaluator workspace and requires typed output. It does not import generated code into Captain's process.

- [ ] **Step 3: Implement reserve-run-record-release sequencing**

1. Revalidate candidate and skill digests.
2. Run compile/import/deterministic smoke tests.
3. Refuse live execution when policy is offline.
4. Reserve the runner's declared maximum cost through `FactoryBudgetPort`.
5. Execute with remaining time and allowed models.
6. Persist provider receipts and release unused reservation.
7. Validate runtime IDs, assertion IDs, handoffs, termination, tool evidence, and optional n8n execution evidence.
8. Seal `TeamExecutionEvidenceV1`.

Provider failure records/releases the reservation according to actual known usage. Missing cost remains `unresolved`; never invent zero cost.

The Real Case Tester Factory lease authorizes the case and the job's explicit live capabilities. It does not gain n8n MCP. When the generated team needs n8n, the tool call carries a separate Captain runtime `CapabilityGrant` for the released `n8n-builder` work node; the tester observes its typed result and execution evidence. Reject a run that reports n8n activity under the tester lease alone.

- [ ] **Step 4: Validate AutoGen manifest choices**

Extend candidate validation to require a declared conversation pattern (`swarm`, `selector_group_chat`, `round_robin_group_chat`, or `single_agent`), agents, per-agent tools, system prompt refs, memory policy, handoff allowlist, message/handoff ceilings, and termination conditions. `swarm` is the default for more than one specialist with handoffs. Reject unknown handoffs and tools before the real run.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_team_execution.py tests/agent_factory/test_candidate_evaluation.py tests/agent_factory/test_n8n_tools.py
git add agenten/agent_factory/team_execution.py agenten/agent_factory/candidate_evaluation.py tests/agent_factory/test_team_execution.py tests/agent_factory/test_candidate_evaluation.py
git commit -m "feat: execute factory teams within cost leases"
```

### Task 8: Evaluate, Improve, and Report Without Weakening Assertions

**Files:**
- Create: `agenten/agent_factory/team_evaluation.py`
- Create: `agenten/agent_factory/improvement.py`
- Create: `agenten/agent_factory/factory_feedback.py`
- Modify: `agenten/agent_factory/release_gate.py`
- Modify: `agenten/agent_factory/state_machine.py:30-180`
- Modify: `agenten/agent_factory/service.py`
- Test: `tests/agent_factory/test_team_evaluation.py`
- Test: `tests/agent_factory/test_improvement.py`
- Test: `tests/agent_factory/test_factory_feedback.py`
- Modify: `tests/agent_factory/test_release_gate.py`
- Modify: `tests/agent_factory/test_state_machine.py`
- Modify: `tests/agent_factory/test_service.py`

**Interfaces:**
- Consumes: immutable candidate/run evidence, Captain assertions/holdouts, prior evaluation, current Factory projection, and cost projection.
- Produces: `TeamEvaluationService.evaluate(...)`, `ImprovementBuilder.build(...)`, `FactoryFeedbackBuilder.build(...)`, and release checks that distinguish `demo_ready` from `ready_to_use`.

- [ ] **Step 1: Write independent-evaluator tests**

```python
def test_deterministic_assertions_run_before_optional_model_judge() -> None:
    judge = RecordingJudge()
    evaluation = evaluator(judge=judge).evaluate(
        invocation(), candidate(), failed_deterministic_execution()
    )
    assert evaluation.recommendation == "RETRY_BUILD"
    assert judge.calls == []


def test_evaluator_cannot_accept_unknown_or_missing_assertions() -> None:
    with pytest.raises(ValueError, match="Captain assertions"):
        evaluator().evaluate(
            invocation(), candidate(), execution_with_assertions(("invented",))
        )


def test_release_and_demo_readiness_are_distinct(job_v3, passing_evaluation) -> None:
    demo = evaluate_factory_workflow_release(
        demo_job(job_v3), one_live_run(), passing_evaluation
    )
    assert demo.status == "demo_ready"
    release = evaluate_factory_workflow_release(
        release_job(job_v3), three_live_runs(), passing_evaluation
    )
    assert release.status == "ready"
```

- [ ] **Step 2: Implement deterministic-first evaluation**

Validate schema/digest/lease/redaction, build/test/integration evidence, every released assertion, holdout receipts, n8n execution, handoff/conversation/memory/termination behavior, cost, latency, and repeated-run stability. Call an injected judge only when deterministic gates pass and a qualitative rubric remains. Store the judge output as one evidence ref, never as sole acceptance evidence.

- [ ] **Step 3: Write improvement tests and builder**

```python
def test_improvement_targets_failed_components_and_preserves_green_assertions() -> None:
    revision = builder().build(
        invocation=improvement_invocation(attempt=2),
        prior_candidate=candidate_ref(),
        evaluation=evaluation_with_prompt_failure(),
    )
    assert revision.changed_components == ("system_prompt",)
    assert revision.regression_assertion_ids == ("schema_valid", "tool_contract")
    assert revision.failed_assertion_ids == ("answer_quality",)
```

`ImprovementBuilder` maps evidence classifications to the smallest allowed component enum: agent code, system prompt, user prompt, context, tool contract, model client, memory, AutoGen conversation pattern, handoffs, termination, n8n workflow/nodes, tests, or documentation. It returns a child candidate assignment for Skill 2 and never edits files itself.

- [ ] **Step 4: Implement exact feedback recommendations**

Map current evidence to exactly one of:

```python
class FactoryFeedbackRecommendation(str, Enum):
    PROMOTE_CANDIDATE = "PROMOTE_CANDIDATE"
    RETRY_BUILD = "RETRY_BUILD"
    BLOCKED_TOOL_REQUIRED = "BLOCKED_TOOL_REQUIRED"
    BLOCKED_CREDENTIAL_REQUIRED = "BLOCKED_CREDENTIAL_REQUIRED"
    BLOCKED_INFRASTRUCTURE = "BLOCKED_INFRASTRUCTURE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MANUAL_DECISION_REQUIRED = "MANUAL_DECISION_REQUIRED"
```

Required unresolved `TODO_TOOL.v1` wins over optional gaps. Missing secret references produce credential block; reachable-code/service failures produce infrastructure block; exhausted cost/time wins before retry. `PROMOTE_CANDIDATE` is only a recommendation.

- [ ] **Step 5: Integrate feedback with existing state transitions**

Do not add six new phases. After `QUALITY_REVIEWED`, Captain translates validated feedback into the existing `IMPROVEMENT_REQUESTED`, `CAPABILITY_PROMOTED`, or `ESCALATED` path. Job V3 release validation additionally checks mode, required live-run count, budget receipts, and `demo_ready` separation. V1/V2 release behavior remains unchanged.

Change `FactoryProjection.job`, `FactoryProjection.from_job`, `FactoryRepository`, `FactoryLeasePort`, and related coordinator signatures from concrete `AgentFactoryJob` to the `FactoryJob` union. Keep phase ordering identical. Add V1, V2, and V3 projection/replay tests so the typing expansion cannot change older lifecycle behavior.

- [ ] **Step 6: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_team_evaluation.py tests/agent_factory/test_improvement.py tests/agent_factory/test_factory_feedback.py tests/agent_factory/test_release_gate.py tests/agent_factory/test_state_machine.py tests/agent_factory/test_service.py
git add agenten/agent_factory/team_evaluation.py agenten/agent_factory/improvement.py agenten/agent_factory/factory_feedback.py agenten/agent_factory/release_gate.py agenten/agent_factory/state_machine.py agenten/agent_factory/service.py tests/agent_factory/test_team_evaluation.py tests/agent_factory/test_improvement.py tests/agent_factory/test_factory_feedback.py tests/agent_factory/test_release_gate.py tests/agent_factory/test_state_machine.py tests/agent_factory/test_service.py
git commit -m "feat: evaluate and improve factory teams"
```

### Task 9: Persist Budget and Workflow Evidence Through the Gateway

**Files:**
- Modify: `gateway/contracts.py:78-160`
- Modify: `gateway/store.py:160-270,727-1290`
- Modify: `gateway/factory_repository.py`
- Modify: `gateway/app.py:270-390`
- Test: `tests/gateway/test_factory_budget.py`
- Modify: `tests/gateway/test_agent_factory.py`
- Modify: `tests/gateway/test_factory_repository.py`

**Interfaces:**
- Consumes: Task 1 execution policy, Task 2 budget contracts, Task 3 workflow artifacts.
- Produces: `GatewayFactoryBudgetLedger(FactoryBudgetPort)`, idempotent reservation/usage/release endpoints, workflow evidence submission, and recovered projections.

- [ ] **Step 1: Write Gateway atomicity and restart tests**

Cover identical replay, changed replay conflict, concurrent overspend, expired reservation, unknown model, usage above reservation, missing job, V1/V2 job refusing paid reservation, restart reconstruction, and terminal job refusing new reservations.

```python
def test_gateway_budget_reservation_is_atomic(store, release_job_v3) -> None:
    store.record_factory_job(release_job_v3)
    first = store.reserve_factory_budget(
        reservation_request(release_job_v3, amount="3.00")
    )
    with pytest.raises(HTTPException, match="budget exhausted"):
        store.reserve_factory_budget(
            reservation_request(release_job_v3, amount="3.00", nonce="second")
        )
    assert first.replayed is False


def test_gateway_rehydrates_consumed_cost_after_restart(storage, release_job_v3) -> None:
    first = GatewayStore(storage)
    first.record_factory_job(release_job_v3)
    reservation = first.reserve_factory_budget(reservation_request(release_job_v3))
    first.record_factory_usage(usage_submission(reservation, "0.80"))
    recovered = GatewayStore(storage).factory_budget(release_job_v3.job_id)
    assert recovered.consumed_usd == Decimal("0.80")
```

- [ ] **Step 2: Add append-only storage**

Create indexed tables for immutable Factory budget events and workflow artifacts. Store canonical JSON and content digests, not credentials or raw provider responses. Reservation must lock the job/budget head with `SELECT ... FOR UPDATE`, compute consumed plus active reservations, and append the event/head update atomically. Usage closes exactly one active reservation. Identical IDs/content replay; changed content returns HTTP 409.

- [ ] **Step 3: Parse Factory jobs through the union everywhere**

Replace direct `AgentFactoryJob.model_validate(...)` calls in Gateway Factory paths with `parse_factory_job(...)`. Type repository methods as `FactoryJob`. Preserve serialized schema metadata from the actual version rather than hardcoding V1.

- [ ] **Step 4: Expose role-protected endpoints**

Add Captain/runtime-authenticated routes for reserve, usage, release, projection, and skill-artifact submission. Hermes never receives direct database credentials. Enforce actor role, active lease, job/version/attempt/model bindings, terminal-state fence, and redaction before calling the store.

- [ ] **Step 5: Run deterministic and isolated MariaDB tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/gateway/test_factory_budget.py tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py tests/gateway/test_gateway_auth.py
if (-not $env:TEST_MARIADB_DSN -or $env:TEST_MARIADB_DSN -notmatch '/captain_test(?:\?|$)') { throw 'TEST_MARIADB_DSN must already be set to the isolated captain_test database' }
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/gateway/test_factory_budget.py tests/integration/test_hermes_skill_evaluation_gateway.py
Remove-Item Env:TEST_MARIADB_DSN
```

Expected: deterministic tests pass; MariaDB tests run only against exact `captain_test` and report no skips when explicitly configured.

- [ ] **Step 6: Commit Gateway authority**

```powershell
git add gateway/contracts.py gateway/store.py gateway/factory_repository.py gateway/app.py tests/gateway/test_factory_budget.py tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py
git commit -m "feat: persist factory execution budgets"
```

### Task 10: Configure Hermes External Skills and Bundle Idempotently

**Files:**
- Create: `scripts/configure-hermes-factory-skills.ps1`
- Create: `tests/test_hermes_factory_skills_setup.py`
- Modify: `docs/AGENT_FACTORY_RUNBOOK.md`

**Interfaces:**
- Consumes: repository skill root, Hermes CLI `config set`, `skills list`, and `bundles create`.
- Produces: an idempotent operator command that preserves unrelated Hermes config and verifies six enabled external skills plus `/captain-agent-factory-loop`.

- [ ] **Step 1: Write a temporary-HERMES_HOME script test**

```python
def test_configure_script_adds_external_dir_and_bundle_idempotently(tmp_path: Path) -> None:
    env = os.environ | {"HERMES_HOME": str(tmp_path / "hermes")}
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        "scripts/configure-hermes-factory-skills.ps1",
        "-RepositoryRoot",
        str(ROOT),
    ]
    first = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    second = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    config = yaml.safe_load((tmp_path / "hermes" / "config.yaml").read_text())
    assert config["skills"]["external_dirs"].count(
        str(ROOT / "agenten" / "agent_factory" / "skills")
    ) == 1
    assert "configured" in first.stdout.lower()
    assert "already configured" in second.stdout.lower()
```

- [ ] **Step 2: Implement safe config merge through Hermes CLI**

The script resolves exact absolute paths, reads existing external directories via `hermes config get skills.external_dirs`, appends only the missing repository skill root, serializes the array as JSON, and calls:

```powershell
hermes config set skills.external_dirs $externalDirsJson
hermes bundles create captain-agent-factory-loop `
  --skill captain-factory-discover `
  --skill captain-factory-brief-codex `
  --skill captain-factory-execute-team `
  --skill captain-factory-evaluate-team `
  --skill captain-factory-improve-team `
  --skill captain-factory-report-captain `
  --description 'Captain-controlled six-skill AutoGen factory workflow' `
  --instruction 'Use only the step released by the current Captain invocation.' `
  --force
```

Do not open or print `.env`. Fail when a skill is missing, disabled, shadowed by another path, or its on-disk digest differs from the repository release manifest.

- [ ] **Step 3: Verify with Hermes commands**

```powershell
pwsh -NoProfile -File scripts/configure-hermes-factory-skills.ps1
hermes skills list
hermes bundles show captain-agent-factory-loop
```

Expected: all six skills show source `external`/enabled and the bundle lists each exactly once.

- [ ] **Step 4: Document setup and rollback**

Add exact setup, verify, and removal commands. Rollback removes only this repository path and this bundle; it must not reset builtin skills or delete other external directories.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_hermes_factory_skills_setup.py tests/agent_factory/test_factory_skill_package.py
git add scripts/configure-hermes-factory-skills.ps1 tests/test_hermes_factory_skills_setup.py docs/AGENT_FACTORY_RUNBOOK.md
git commit -m "feat: configure hermes factory skills"
```

### Task 11: Prove the Complete Six-Skill Chain Deterministically and Live

**Files:**
- Create: `tests/integration/test_hermes_six_skill_factory.py`
- Create: `tests/live/test_hermes_six_skill_factory_live.py`
- Create: `scripts/run-hermes-factory-live-gate.ps1`
- Create: `docs/diagrams/hermes-six-skill-factory.html`
- Modify: `docs/AGENT_FACTORY_RUNBOOK.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `scripts/verify_submission.py`

**Interfaces:**
- Consumes: all prior tasks, existing Captain Gateway, Hermes CLI, Codex runtime, optional Context7 and n8n MCP, isolated `captain_test`.
- Produces: deterministic restart/retry evidence, one explicit paid `demo_ready` case, and a release mode requiring recovery plus three distinct successful live cases.

- [ ] **Step 1: Write deterministic full-chain cases**

Use real domain contracts and fakes only at provider/process boundaries. Cover:

1. first-pass success through Skills 1, 2, 3, 4, and 6;
2. behavioral failure through Skill 5 then Skills 2, 3, 4, and 6;
3. required tool block;
4. optional tool gap with all assertions passing;
5. credential block distinct from infrastructure block;
6. budget exhaustion before a second paid run;
7. infrastructure recovery in the same attempt;
8. changed skill digest rejection;
9. restart after reservation and after execution evidence;
10. idempotent replay with no duplicate Codex/n8n/provider effect;
11. `demo_ready` cannot become `ready_to_use`; and
12. only Captain promotion reaches the terminal ready state.

```python
@pytest.mark.asyncio
async def test_behavioral_retry_uses_improve_then_rebuild_and_promotes() -> None:
    harness = six_skill_harness(first_run="behavioral_failure", second_run="passed")
    result = await harness.run()
    assert result.skill_steps == (
        "discover",
        "brief_codex",
        "execute_team",
        "evaluate_team",
        "report_captain",
        "discover",
        "improve_team",
        "brief_codex",
        "execute_team",
        "evaluate_team",
        "report_captain",
    )
    assert result.attempts == 2
    assert result.gateway_projection.status == "ready_to_use"
```

- [ ] **Step 2: Run the deterministic integration gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/integration/test_hermes_six_skill_factory.py tests/integration/test_hermes_skill_evaluation_gateway.py tests/integration/test_agent_runtime_control_plane.py
```

Expected: all selected deterministic cases pass without external service claims.

- [ ] **Step 3: Add an explicit paid live test**

The live test is marked `live` and requires Job V3, `live_execution=true`, an allowed model, `max_cost_usd`, real Codex session ID, provider cost receipt, and isolated Gateway. The integration fixture additionally requires Context7 provenance and real n8n MCP execution ID/workflow digest. It fails rather than skips after the wrapper has confirmed prerequisites.

- [ ] **Step 4: Implement the safe live wrapper**

`scripts/run-hermes-factory-live-gate.ps1` accepts:

```powershell
param(
  [ValidateSet('demo','release')][string]$Mode = 'demo',
  [Parameter(Mandatory)][decimal]$MaxCostUsd,
  [string]$Model,
  [switch]$WithN8n
)
```

It validates Docker/services/config without printing secrets, verifies `captain_test`, confirms the Hermes six-skill digests and Codex authentication, constructs the execution policy, and runs only `tests/live/test_hermes_six_skill_factory_live.py -m live`. Demo mode requests one case and emits `demo_ready`; release mode executes the controlled recovery plus three normal cases. It writes a redacted, content-addressed report outside tracked source artifacts.

- [ ] **Step 5: Run the live demo gate with an explicit budget**

```powershell
if (-not $env:CAPTAIN_FACTORY_MODEL) { throw 'CAPTAIN_FACTORY_MODEL is required' }
pwsh -NoProfile -File scripts/run-hermes-factory-live-gate.ps1 -Mode demo -MaxCostUsd 5.00 -Model $env:CAPTAIN_FACTORY_MODEL
```

Expected: one real Captain -> Hermes skills -> Codex -> AutoGen -> evaluation -> Captain trace, exact cost at or below USD 5.00, terminal `demo_ready`, and no `ready_to_use` claim.

For a declared n8n integration:

```powershell
pwsh -NoProfile -File scripts/run-hermes-factory-live-gate.ps1 -Mode demo -MaxCostUsd 5.00 -Model $env:CAPTAIN_FACTORY_MODEL -WithN8n
```

Expected additionally: scoped Captain n8n MCP lease, matching workflow digest, real execution ID, and successful bounded evidence polling.

- [ ] **Step 6: Generate the accepted architecture diagram**

After the deterministic chain is green, use the builtin `architecture-diagram` skill to create `docs/diagrams/hermes-six-skill-factory.html`. It must show Captain/Gateway authority, the six numbered Hermes skills, Codex, AutoGen Swarm, separate Context7 and scoped n8n MCP paths, USD budget reservation, Minibook read-only projection, retry/recovery, and the distinction between `demo_ready` and `ready_to_use`. The diagram is documentation only and must not be referenced as validation evidence.

- [ ] **Step 7: Run release and repository gates**

Only after demo evidence is reviewed:

```powershell
pwsh -NoProfile -File scripts/run-hermes-factory-live-gate.ps1 -Mode release -MaxCostUsd 15.00 -Model $env:CAPTAIN_FACTORY_MODEL -WithN8n
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
.\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
.\.venv\Scripts\python.exe scripts/verify_submission.py
git diff --check
git status --short --branch
```

Report exact passed/skipped/deselected counts, live run IDs, total USD cost, candidate/package digests, commit SHA, remote parity, and any remaining service skip. Do not call release green if any required live evidence skipped.

- [ ] **Step 8: Commit integration and operations**

```powershell
git add tests/integration/test_hermes_six_skill_factory.py tests/live/test_hermes_six_skill_factory_live.py scripts/run-hermes-factory-live-gate.ps1 docs/diagrams/hermes-six-skill-factory.html docs/AGENT_FACTORY_RUNBOOK.md docs/ARCHITECTURE.md scripts/verify_submission.py
git commit -m "feat: prove hermes six-skill factory chain"
```

## Integration and Review Gates

Before merging the three work portions:

1. Fetch and inspect all worktrees, dirty state, ancestry, and remote parity.
2. Simulate merges in the order Task 1/3 contracts -> Task 2/9 authority -> Tasks 4/5/6/10 skills -> Tasks 7/8 runtime -> Task 11 integration.
3. Inspect overlap in `agenten/agent_factory/contracts.py`, `orchestration.py`, `hermes_cli.py`, `gateway/store.py`, `.env.example`, `requirements.txt`, and docs.
4. Confirm `hermes-agent/` is unchanged or its reviewed commit exists on its remote before committing a new parent pin.
5. Run focused tests after each merge and the full deterministic gate only after all portions integrate.
6. Run paid live evidence last, after every DB-resetting test; perform only non-mutating checks afterward.

## Definition of Done

- Six external Hermes skills are installed/enabled from the Captain repository and individually digest-released.
- Captain dispatches exactly the correct skill sequence for role and attempt.
- Semantic discovery reuses existing code and distinguishes metrics from architecture inspection.
- Codex receives a bounded, reproducible goal prompt with tests and evidence requirements.
- Real AutoGen execution is impossible without explicit live policy, lease, allowed model, remaining time, and remaining USD budget.
- Evaluation is independent, deterministic-first, holdout-aware, and unable to repair its own candidate.
- Improvement can change only evidence-implicated components and reruns all prior-green assertions.
- n8n is present only for declared integrations and always uses typed, scoped evidence.
- Missing tool, credential, infrastructure, and budget conditions remain distinct and actionable.
- Demo evidence reports only `demo_ready`; production promotion requires the configured recovery and three clean live runs.
- Gateway persists and reconstructs budget/workflow evidence and remains the sole promotion writer.
- Deterministic, architecture, compile, submission, isolated MariaDB, and explicit paid live gates are reported with exact evidence.
