from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agenten.agent_runtime.capabilities import derive_grant, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    HermesPlanResult,
    IntegrationIntent,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.control_plane import (
    AgentRuntimeControlPlane,
    ControlPlaneEvidenceManifest,
    ControlPlaneRunRequest,
    InMemoryControlPlaneRunStore,
    JsonControlPlaneRunStore,
    ValidationDisposition,
    ValidationRecord,
)
from agenten.agent_runtime.service import AgentRuntimeService
from agenten.agent_runtime.swarm import SwarmOrchestrator
from agenten.agent_runtime.tools import RuntimeToolset
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    BatchEnrichment,
    CaptainPipeline,
    PlannedSubtask,
)
from agenten.planning.hermes_plan import HermesPlanReader
from agenten.planning.policy import PlanningPolicy
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


NOW = datetime(2026, 7, 18, 10, tzinfo=timezone.utc)
CORRELATION_ID = UUID("10000000-0000-4000-8000-000000000010")


def _ref(name: str, content: bytes, media_type: str = "text/markdown") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://control-plane/{name}",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type=media_type,
    )


class MemoryArtifacts:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.required: list[str] = []

    def put(self, name: str, content: bytes, media_type: str = "text/markdown") -> ArtifactRef:
        reference = _ref(name, content, media_type)
        self.values[reference.uri] = content
        return reference

    async def read(self, reference: ArtifactRef) -> bytes:
        return self.values[reference.uri]

    async def require(self, reference: ArtifactRef) -> None:
        content = self.values[reference.uri]
        assert hashlib.sha256(content).hexdigest() == reference.sha256
        self.required.append(reference.uri)


def _hermes_plan(artifacts: MemoryArtifacts) -> HermesPlanResult:
    blueprint = json.dumps(
        {
            "schema": "captain.agent-blueprint.v1",
            "name": "runtime_builder",
            "purpose": "Design the bounded runtime implementation agent.",
            "inputs": {"project_context": "object"},
            "outputs": {"implementation_result": "object"},
            "system_prompt_ref": {
                "uri": "artifact://control-plane/system-prompt",
                "sha256": "f" * 64,
                "media_type": "text/markdown",
            },
            "tools": ["knowledge.search"],
            "integration_intent": "n8n",
            "n8n_tool_families": ["workflow"],
            "handoffs": ["captain.decompose"],
            "limits": {"max_turns": 8, "wall_seconds": 300},
            "evaluation_cases": [
                {"case_id": "bounded-tools", "assertion": "tool_allowlist_enforced"}
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    blueprint_ref = artifacts.put("blueprint", blueprint, "application/json")
    plan = json.dumps(
        {
            "schema": "captain.hermes-planning-document.v1",
            "project_id": "runtime-project",
            "correlation_id": str(CORRELATION_ID),
            "subject_version": 1,
            "objective": "Build code, then publish its isolated n8n integration.",
            "planner_id": "hermes-planner-runtime",
            "blueprint_digests": [blueprint_ref.sha256],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    plan_ref = artifacts.put("plan", plan, "application/json")
    decision_ref = artifacts.put("decision-log", b"Approved bounded runtime plan.")
    return HermesPlanResult(
        schema_name="captain.hermes-plan-result.v1",
        project_id="runtime-project",
        correlation_id=CORRELATION_ID,
        subject_version=1,
        plan_ref=plan_ref,
        decision_log_ref=decision_ref,
        blueprint_refs=(blueprint_ref,),
        integration_intents=(IntegrationIntent.N8N,),
        minibook={"project_id": "runtime-project", "post_id": "minibook-plan-1"},
        planner_id="hermes-planner-runtime",
        runtime_provenance="hermes-agent/captain-planner-v1",
        started_at=NOW,
        ended_at=NOW,
    )


class RuntimeState:
    def __init__(self) -> None:
        self.batches: dict[str, WorkBatch] = {}
        self.commands: dict[UUID, AgentRuntimeCommand] = {}
        self.grants: dict[UUID, CapabilityGrant] = {}
        self.revocations: dict[UUID, CapabilityGrantRevocation] = {}
        self.results: dict[UUID, AgentRuntimeResult] = {}
        self.release_order: list[str] = []

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        assert batch.batch_id == holdouts.batch_id
        existing = self.batches.get(batch.batch_id)
        if existing is not None:
            assert existing == batch
            return
        assert all(dependency in self.batches for dependency in batch.depends_on)
        self.batches[batch.batch_id] = batch
        self.release_order.append(batch.batch_id)

    async def accept_command(self, command: AgentRuntimeCommand) -> None:
        existing = self.commands.get(command.event_id)
        if existing is not None and existing != command:
            raise RuntimeError("conflicting command replay")
        self.commands[command.event_id] = command

    async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch:
        assert command.payload.batch_id is not None
        return self.batches[command.payload.batch_id]

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        return self.grants.get(command_id)

    async def get_grant_revocation(
        self, command_id: UUID
    ) -> CapabilityGrantRevocation | None:
        return self.revocations.get(command_id)

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        self.grants[grant.command_id] = grant
        return grant

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None:
        return self.results.get(command_id)

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult:
        self.results[result.command_id] = result
        return result


class CapabilityPolicy:
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch,
        now: datetime,
    ) -> CapabilityGrant:
        return derive_grant(command, batch, now)

    def validate(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        now: datetime,
        revocation: CapabilityGrantRevocation | None = None,
    ) -> CapabilityGrant:
        return validate_grant(grant, command, now, revocation)


class Clock:
    def now(self) -> datetime:
        return NOW


class UnusedHermes:
    async def plan(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> Any:
        raise AssertionError("Hermes planning is complete before Captain compilation")

    async def design_agent(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> Any:
        raise AssertionError("Hermes planning is complete before Captain compilation")


class ScriptedCodex:
    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[RuntimeOperation, str, str]] = []
        self.sessions: dict[str, str] = {}

    async def start(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def resume(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def status(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def cancel(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def heartbeat(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    def _result(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        behavior = self.script.pop(0) if self.script else "success"
        self.calls.append(
            (
                command.payload.operation,
                command.payload.prompt_ref.sha256,
                grant.profile.value,
            )
        )
        if behavior == "infrastructure":
            raise OSError("adapter unavailable with sensitive diagnostics")
        session_id = self.sessions.setdefault(command.subject_id, f"session-{command.subject_id}")
        artifact = ArtifactRef(
            uri=f"artifact://control-plane/output/{command.subject_id}",
            sha256=hashlib.sha256(command.subject_id.encode()).hexdigest(),
            media_type="application/json",
        )
        evidence = ArtifactRef(
            uri=f"artifact://control-plane/evidence/{command.subject_id}",
            sha256=hashlib.sha256(f"evidence:{command.subject_id}".encode()).hexdigest(),
            media_type="application/json",
        )
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid4(),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id=session_id,
            artifact_refs=(artifact,),
            evidence_refs=(evidence,),
        )


class FirstSelector:
    async def select(self, task_id: str, tools: tuple[str, ...]) -> str:
        del task_id
        return "codex.resume" if "codex.resume" in tools else "codex.run"


class ScriptedValidator:
    def __init__(
        self,
        artifacts: MemoryArtifacts,
        scripts: dict[str, list[ValidationDisposition]] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.scripts = {key: list(value) for key, value in (scripts or {}).items()}
        self.calls: list[str] = []

    async def validate(
        self,
        batch: WorkBatch,
        result: AgentRuntimeResult,
    ) -> ValidationRecord:
        del batch
        self.calls.append(result.subject_id)
        choices = self.scripts.get(result.subject_id, [])
        disposition = choices.pop(0) if choices else ValidationDisposition.PASSED
        content = f"{result.subject_id}:{disposition.value}".encode()
        return ValidationRecord(
            task_id=result.subject_id,
            disposition=disposition,
            artifact_ref=self.artifacts.put(
                f"validation/{result.subject_id}/{len(self.calls)}",
                content,
                "application/json",
            ),
            assertion_ids=("runtime-result-valid",),
            occurred_at=NOW,
        )


def _captain(state: RuntimeState, artifacts: MemoryArtifacts) -> CaptainPipeline:
    async def decompose(description: str) -> list[PlannedSubtask]:
        assert description == "Build code, then publish its isolated n8n integration."
        return [
            PlannedSubtask(subtask_id="code-task", description="Build the code"),
            PlannedSubtask(subtask_id="n8n-task", description="Build the workflow"),
        ]

    async def align(
        description: str,
        subtasks: list[PlannedSubtask],
        feedback: str,
    ) -> AlignmentPlan:
        del description, subtasks, feedback
        return AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="code-batch",
                    title="Code build",
                    subtask_ids=["code-task"],
                    target="python",
                ),
                BatchDraft(
                    batch_id="n8n-batch",
                    title="n8n integration",
                    subtask_ids=["n8n-task"],
                    depends_on=["code-batch"],
                    target="n8n",
                ),
            ]
        )

    async def enrich(
        description: str,
        draft: BatchDraft,
        subtasks: list[PlannedSubtask],
    ) -> BatchEnrichment:
        del description, subtasks
        capability = "code-builder" if draft.batch_id == "code-batch" else "delivery"
        return BatchEnrichment(
            goal=f"Complete {draft.title} under the released contract.",
            capability_tags=[capability],
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id="runtime-result-valid",
                    kind=AssertionKind.STATUS_EQUALS,
                    path="status",
                    expected="succeeded",
                )
            ],
            golden_cases=[ExampleCase(case_id="visible", input={"case": "public"})],
            holdout_cases=[ExampleCase(case_id="private", input={"case": "sealed"})],
        )

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=state,
        policy=PlanningPolicy(frozenset({"code-builder", "delivery", "n8n-builder"})),
        target="python",
        allowed_targets=frozenset({"python", "n8n"}),
        plan_reader=HermesPlanReader(artifacts),
    )


def _request(artifacts: MemoryArtifacts, plan: HermesPlanResult) -> ControlPlaneRunRequest:
    code_prompt = artifacts.put("prompt/code", b"Build the bounded code artifact.")
    n8n_prompt = artifacts.put("prompt/n8n", b"Build the isolated workflow artifact.")
    return ControlPlaneRunRequest(
        hermes_result=plan,
        workspace_refs={
            "code-task": "workspace://authorized/runtime/code",
            "n8n-task": "workspace://authorized/runtime/n8n",
        },
        prompt_refs={"code-task": code_prompt, "n8n-task": n8n_prompt},
        wall_seconds=300,
        max_iterations=3,
    )


def _control_plane(
    *,
    state: RuntimeState,
    artifacts: MemoryArtifacts,
    codex: ScriptedCodex,
    validator: ScriptedValidator,
    store: InMemoryControlPlaneRunStore | JsonControlPlaneRunStore | None = None,
) -> AgentRuntimeControlPlane:
    service = AgentRuntimeService(
        state=state,
        hermes=UnusedHermes(),
        codex=codex,
        artifacts=artifacts,
        capabilities=CapabilityPolicy(),
        clock=Clock(),
    )
    tools = RuntimeToolset(service=service, clock=Clock())
    swarm = SwarmOrchestrator(tools=tools, selector=FirstSelector())
    return AgentRuntimeControlPlane(
        captain=_captain(state, artifacts),
        swarm=swarm,
        validator=validator,
        store=store or InMemoryControlPlaneRunStore(),
        clock=Clock(),
    )


@pytest.mark.asyncio
async def test_complete_chain_preserves_dependency_order_and_n8n_lease_boundary() -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()
    codex = ScriptedCodex()
    result = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=ScriptedValidator(artifacts),
    ).execute(_request(artifacts, plan))

    assert result.status == "succeeded"
    assert state.release_order == ["code-batch", "n8n-batch"]
    assert [call[0] for call in codex.calls] == [
        RuntimeOperation.CODEX_RUN,
        RuntimeOperation.CODEX_RUN,
    ]
    assert [call[2] for call in codex.calls] == ["code-builder", "n8n-builder"]
    grants = list(state.grants.values())
    assert grants[0].mcp_servers == ()
    assert grants[1].mcp_servers == ("n8n-mcp",)
    assert result.manifest.correlation_id == CORRELATION_ID
    assert result.manifest.minibook_post_id == "minibook-plan-1"
    assert result.manifest.batch_order == ("code-batch", "n8n-batch")
    assert result.manifest.completed_tasks == ("code-task", "n8n-task")
    serialized = result.manifest.model_dump_json()
    assert "N8N_MCP_TOKEN" not in serialized
    assert "private" not in serialized
    assert "Build the bounded" not in serialized


@pytest.mark.asyncio
async def test_infrastructure_retry_does_not_consume_behavioral_iteration_and_redo_uses_new_prompt() -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()
    codex = ScriptedCodex(["infrastructure", "success", "success", "success"])
    validator = ScriptedValidator(
        artifacts,
        {"code-task": [ValidationDisposition.REDO, ValidationDisposition.PASSED]}
    )
    store = InMemoryControlPlaneRunStore()
    result = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=validator,
        store=store,
    ).execute(_request(artifacts, plan))

    checkpoint = await store.load(CORRELATION_ID)
    assert checkpoint is not None
    assert checkpoint.behavioral_iterations["code-task"] == 1
    code_calls = [call for call in codex.calls if call[2] == "code-builder"]
    assert [call[0] for call in code_calls] == [
        RuntimeOperation.CODEX_RUN,
        RuntimeOperation.CODEX_RESUME,
        RuntimeOperation.CODEX_RESUME,
    ]
    assert code_calls[1][1] != code_calls[2][1]
    assert len({result.session_id for result in checkpoint.result_history if result.subject_id == "code-task" and result.session_id}) == 1
    assert result.manifest.infrastructure_failures == 1
    assert result.manifest.behavioral_redos == 1


@pytest.mark.asyncio
async def test_infrastructure_retry_budget_fails_closed_without_behavioral_charge() -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()
    codex = ScriptedCodex(["infrastructure", "infrastructure", "success"])
    request = ControlPlaneRunRequest.model_validate(
        {
            **_request(artifacts, plan).model_dump(mode="json"),
            "max_infrastructure_failures": 2,
        }
    )
    result = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=ScriptedValidator(artifacts),
    ).execute(request)

    assert result.status == "failed"
    assert result.manifest.infrastructure_failures == 2
    assert result.manifest.behavioral_redos == 0
    assert len(codex.calls) == 2


class WrongAssertionValidator(ScriptedValidator):
    async def validate(
        self,
        batch: WorkBatch,
        result: AgentRuntimeResult,
    ) -> ValidationRecord:
        record = await super().validate(batch, result)
        return record.model_copy(update={"assertion_ids": ("not-released-by-captain",)})


@pytest.mark.asyncio
async def test_validation_is_bound_to_captain_acceptance_assertions() -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()

    with pytest.raises(RuntimeError, match="assertion IDs"):
        await _control_plane(
            state=state,
            artifacts=artifacts,
            codex=ScriptedCodex(),
            validator=WrongAssertionValidator(artifacts),
        ).execute(_request(artifacts, plan))


class SimulatedProcessStop(BaseException):
    pass


class CrashAfterSessionCodex(ScriptedCodex):
    def __init__(self) -> None:
        super().__init__()
        self.stopped = False

    def _result(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        self.sessions.setdefault(command.subject_id, f"session-{command.subject_id}")
        if not self.stopped:
            self.stopped = True
            raise SimulatedProcessStop()
        return super()._result(command, grant)


@pytest.mark.asyncio
async def test_restart_after_command_persistence_reuses_command_grant_and_session() -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()
    store = InMemoryControlPlaneRunStore()
    codex = CrashAfterSessionCodex()
    request = _request(artifacts, plan)

    with pytest.raises(SimulatedProcessStop):
        await _control_plane(
            state=state,
            artifacts=artifacts,
            codex=codex,
            validator=ScriptedValidator(artifacts),
            store=store,
        ).execute(request)

    accepted_ids = set(state.commands)
    grant_ids = {grant.grant_id for grant in state.grants.values()}
    restarted = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=ScriptedValidator(artifacts),
        store=store,
    ).execute(request)

    assert restarted.status == "succeeded"
    assert accepted_ids.issubset(state.commands)
    assert grant_ids.issubset({grant.grant_id for grant in state.grants.values()})
    assert codex.sessions["code-task"] == "session-code-task"
    replay = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=ScriptedValidator(artifacts),
        store=store,
    ).execute(request)
    assert replay.manifest == restarted.manifest


@pytest.mark.asyncio
async def test_behavioral_replan_stops_dependent_work_and_is_restart_idempotent() -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()
    codex = ScriptedCodex()
    store = InMemoryControlPlaneRunStore()
    validator = ScriptedValidator(
        artifacts,
        {"code-task": [ValidationDisposition.REPLAN]},
    )
    request = _request(artifacts, plan)
    plane = _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=validator,
        store=store,
    )

    first = await plane.execute(request)
    replay = await plane.execute(request)

    assert first.status == replay.status == "replanning"
    assert first.manifest == replay.manifest
    assert [call[2] for call in codex.calls] == ["code-builder"]
    assert "n8n-task" not in first.manifest.completed_tasks


@pytest.mark.asyncio
async def test_json_checkpoint_restart_is_stable_and_contains_references_only(
    tmp_path: Path,
) -> None:
    artifacts = MemoryArtifacts()
    plan = _hermes_plan(artifacts)
    state = RuntimeState()
    codex = ScriptedCodex()
    store = JsonControlPlaneRunStore(tmp_path / "control-plane")
    request = _request(artifacts, plan)

    first = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=ScriptedValidator(artifacts),
        store=store,
    ).execute(request)
    replay = await _control_plane(
        state=state,
        artifacts=artifacts,
        codex=codex,
        validator=ScriptedValidator(artifacts),
        store=store,
    ).execute(request)

    checkpoint_text = (tmp_path / "control-plane" / f"{CORRELATION_ID}.json").read_text(
        encoding="utf-8"
    )
    assert replay.manifest == first.manifest
    assert "Build the bounded code artifact" not in checkpoint_text
    assert "N8N_MCP_TOKEN" not in checkpoint_text
    assert "compiled_batch_digest" in checkpoint_text


def test_evidence_manifest_rejects_secret_like_or_absolute_values() -> None:
    base = {
        "schema": "captain.control-plane-evidence.v1",
        "correlation_id": str(CORRELATION_ID),
        "project_id": "runtime-project",
        "plan_version": 1,
        "plan_digest": "a" * 64,
        "generated_at": NOW,
        "status": "succeeded",
        "minibook_project_id": "runtime-project",
        "minibook_post_id": "post-1",
        "batch_order": ["code-batch"],
        "completed_tasks": ["code-task"],
        "behavioral_redos": 0,
        "infrastructure_failures": 0,
        "observations": [],
    }
    for private_value in (
        "token=must-not-leak",
        "sk-proj-unlabelled-canary-1234567890",
        "Bearer unlabelled-sensitive-value",
        r"C:\\Users\\Private\\result",
    ):
        value = dict(base)
        value["project_id"] = private_value
        with pytest.raises(ValueError, match="private|identifier"):
            ControlPlaneEvidenceManifest.model_validate(value)
