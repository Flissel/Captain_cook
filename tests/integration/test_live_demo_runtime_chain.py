from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.live_demo_runtime_chain import (
    LiveDemoRuntimeChain,
    LiveDemoRuntimeChainError,
    LiveDemoRuntimeRelease,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    IntegrationIntent,
    RuntimeStatus,
)


NOW = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
CORRELATION_ID = UUID("4d53b3a5-252d-4b67-bd4d-3168df61b46a")


def _ref(name: str) -> ArtifactRef:
    return ArtifactRef(uri=f"artifact://demo/{name}", sha256="a" * 64, media_type="application/json")


def _release() -> LiveDemoRuntimeRelease:
    job = AgentFactoryJob(
        schema_name="captain.agent-factory-job.v1",
        event_id=uuid4(), correlation_id=CORRELATION_ID, occurred_at=NOW,
        producer="captain", job_id=uuid4(), subject_version=1,
        input_ref=_ref("input"), required_capability="n8n-builder",
        acceptance_assertion_ids=("demo-chain",),
    )
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://demo/live",
        now=NOW,
        integration_intent=IntegrationIntent.N8N,
    )
    action = FactoryAction(kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, attempt=1)
    command = AgentRuntimeCommand.model_validate({
        "schema": "captain.agent-runtime-command.v1", "event_id": str(uuid4()),
        "correlation_id": str(CORRELATION_ID), "causation_id": str(job.event_id),
        "occurred_at": NOW, "producer": "captain", "subject_id": "demo-task",
        "subject_version": 1, "payload": {"operation": "codex.run",
        "project_id": "demo-project", "batch_id": "demo-batch", "subtask_id": "demo-task",
        "workspace_ref": "workspace://demo/live", "prompt_ref": _ref("prompt").model_dump(mode="json"),
        "integration_intent": "n8n", "capability_profile": "n8n-builder",
        "limits": {"wall_seconds": 60, "max_iterations": 1}},
    })
    return LiveDemoRuntimeRelease(dispatch=FactoryDispatch(job, action, FactoryRole.TOOL_INTEGRATOR, lease), command=command)


class HermesWorker:
    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        assert request.lease is not None
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1", event_id=uuid4(),
            job_id=request.job.job_id, correlation_id=request.job.correlation_id,
            causation_id=request.job.event_id, occurred_at=NOW, producer="hermes",
            subject_version=1, attempt=1, phase=FactoryPhase.TOOL_CANDIDATE_TESTED,
            role=FactoryRole.TOOL_INTEGRATOR, status=FactoryBlockStatus.SUCCEEDED,
            evidence_refs=(_ref("hermes"),), lease_id=request.lease.lease_id,
        )


class GatewayBackedRuntime:
    def __init__(self) -> None:
        self.commands: list[AgentRuntimeCommand] = []

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        self.commands.append(command)
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1", event_id=uuid4(),
            command_id=command.event_id, correlation_id=command.correlation_id,
            occurred_at=NOW, producer="agent-runtime", subject_id=command.subject_id,
            subject_version=command.subject_version, grant_id="gateway-demo-grant",
            operation=command.payload.operation, status=RuntimeStatus.SUCCEEDED,
            session_id="codex-demo-session", evidence_refs=(_ref("codex-n8n"),),
        )


@pytest.mark.asyncio
async def test_one_shot_chain_routes_hermes_evidence_through_real_autogen_runtime() -> None:
    runtime = GatewayBackedRuntime()
    result = await LiveDemoRuntimeChain(
        hermes=HermesWorker(), runtime_service=runtime, clock=lambda: NOW
    ).run(_release())

    assert result.correlation_id == CORRELATION_ID
    assert result.hermes_evidence.correlation_id == CORRELATION_ID
    assert result.runtime_result.correlation_id == CORRELATION_ID
    assert result.runtime_result.status is RuntimeStatus.SUCCEEDED
    assert runtime.commands == [result.command]
    assert result.autogen_delivery_count == 1


class FailedHermes(HermesWorker):
    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        evidence = await super().dispatch(request)
        return evidence.model_copy(update={"status": FactoryBlockStatus.FAILED})


@pytest.mark.asyncio
async def test_failed_hermes_evidence_never_reaches_autogen_or_codex() -> None:
    runtime = GatewayBackedRuntime()
    with pytest.raises(LiveDemoRuntimeChainError, match="successful Hermes evidence"):
        await LiveDemoRuntimeChain(
            hermes=FailedHermes(), runtime_service=runtime, clock=lambda: NOW
        ).run(_release())
    assert runtime.commands == []
