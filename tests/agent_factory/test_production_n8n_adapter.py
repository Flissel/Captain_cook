from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from autogen_core import CancellationToken

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.production_n8n_adapter import (
    CaptainBrokerN8nToolAdapter,
    CaptainN8nToolBinding,
    CaptainN8nWorkflowObservation,
    build_captain_factory_n8n_binding,
)
from agenten.agent_runtime.n8n_mcp_broker import McpLeaseIssuer
from agenten.targets.n8n import N8nExecutionRecord, N8nWorkflowRecord


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def test_demo_binding_pins_the_operator_validated_workflow() -> None:
    binding = build_captain_factory_n8n_binding(
        {"CAPTAIN_FACTORY_N8N_WORKFLOW_ID": "uROkVuVjYGnw8Dfm"},
        tool=_binding().tool,
        batch_id="factory-n8n-test",
    )

    assert binding.workflow_id == "uROkVuVjYGnw8Dfm"
    assert binding.workflow_name == "Captain Factory Integration Evidence"
    assert binding.mcp_tool_name == "execute_workflow"
    assert binding.batch_id == "factory-n8n-test"


class Registrar:
    def __init__(self) -> None:
        self.registered: list[tuple[object, object]] = []

    def register(self, command: object, grant: object) -> None:
        self.registered.append((command, grant))


class Mcp:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    async def call(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return self.payload


class Executions:
    def __init__(
        self,
        record: N8nExecutionRecord,
        workflow: dict[str, object],
    ) -> None:
        self.record = record
        self.workflow = workflow
        self.calls: list[str] = []

    async def fetch_execution(self, execution_id: str) -> N8nExecutionRecord:
        self.calls.append(execution_id)
        return self.record

    async def fetch_workflow(
        self, workflow_id: str
    ) -> CaptainN8nWorkflowObservation:
        self.calls.append(workflow_id)
        return CaptainN8nWorkflowObservation(
            record=N8nWorkflowRecord(
                id=workflow_id,
                name="Captain Factory Integration Evidence",
            ),
            definition=self.workflow,
        )


def _binding() -> CaptainN8nToolBinding:
    return CaptainN8nToolBinding(
        tool=TypedN8nTool(
            name="support_triage",
            description="Read and triage one support case",
            input_schema_ref="artifact://schemas/support-triage-input",
            output_schema_ref="artifact://schemas/support-triage-output",
        ),
        mcp_tool_name="execute_workflow",
        workflow_id="workflow-7",
        workflow_name="Captain Factory Integration Evidence",
        batch_id="factory-n8n-test",
    )


def _adapter(
    tmp_path: Path,
    *,
    mcp_payload: dict[str, object],
    execution: N8nExecutionRecord,
) -> tuple[CaptainBrokerN8nToolAdapter, Registrar, Mcp]:
    registrar = Registrar()
    mcp = Mcp(mcp_payload)
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    workflow = dict(mcp_payload.get("workflow_artifact", {}))
    adapter = CaptainBrokerN8nToolAdapter(
        bindings=(_binding(),),
        correlation_id=UUID("10000000-0000-0000-0000-000000000001"),
        subject_version=3,
        project_id="factory-live-demo",
        batch_id="factory-live-demo-n8n",
        workspace_ref="workspace://factory-live-demo/n8n",
        broker_url="http://127.0.0.1:5680",
        signing_secret="lease-signing-secret",
        registrar=registrar,
        mcp=mcp,
        executions=Executions(execution, workflow),
        artifacts=artifacts,
        clock=lambda: NOW,
    )
    return adapter, registrar, mcp


@pytest.mark.asyncio
async def test_adapter_issues_fresh_lease_calls_broker_and_requires_rest_evidence(
    tmp_path: Path,
) -> None:
    workflow = {
        "id": "workflow-7",
        "name": "Captain Factory Integration Evidence",
        "nodes": [
            {"type": "n8n-nodes-base.webhook"},
            {"type": "n8n-nodes-base.set"},
        ],
    }
    workflow_bytes = json.dumps(
        workflow, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(workflow_bytes).hexdigest()
    correlation_id = "10000000-0000-0000-0000-000000000001"
    payload = {
        "execution_id": "execution-42",
        "workflow_id": "workflow-7",
        "artifact_digest": digest,
        "workflow_artifact": workflow,
    }
    record = N8nExecutionRecord(
        execution_id="execution-42",
        workflow_id="workflow-7",
        status="success",
        output={
            "artifact_digest": digest,
            "correlation_id": correlation_id,
            "result": {"tier": "gold"},
        },
    )
    adapter, registrar, mcp = _adapter(
        tmp_path, mcp_payload=payload, execution=record
    )

    claim = adapter.authorization("support_triage")
    command, grant = registrar.registered[0]
    assert claim.runtime_command == command
    assert claim.capability_grant == grant
    assert command.payload.integration_intent.value == "n8n"
    assert grant.profile.value == "n8n-builder"

    result = await adapter.tool("support_triage").run_json(
        {"arguments": {"input": {"ticket": "case-1"}}}, CancellationToken()
    )

    assert result == {
        "status": "success",
        "execution_id": "execution-42",
        "workflow_id": "workflow-7",
        "result": {"tier": "gold"},
    }
    assert len(mcp.calls) == 1
    assert mcp.calls[0]["arguments"] == {
        "workflowId": "workflow-7",
        "executionMode": "manual",
        "inputs": {
            "type": "webhook",
            "body": {
                "input": {"ticket": "case-1"},
                "correlation_id": correlation_id,
                "artifact_digest": digest,
                "idempotency_key": str(command.event_id),
            },
        },
    }
    issued_token = str(mcp.calls[0]["lease_token"])
    lease = McpLeaseIssuer("lease-signing-secret").verify(issued_token, NOW)
    assert lease.command_id == command.event_id
    assert lease.endpoint_identity == "http://127.0.0.1:5680"
    evidence = adapter.observed_evidence()
    assert len(evidence) == 1
    assert evidence[0].execution.execution_id == "execution-42"
    assert evidence[0].workflow_ref.sha256 == digest
    assert evidence[0].runtime_result.evidence_refs

    second = adapter.authorization("support_triage")
    assert second.runtime_command.event_id != claim.runtime_command.event_id
    assert len(registrar.registered) == 2


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_rest_execution_does_not_match_mcp(
    tmp_path: Path,
) -> None:
    workflow = {
        "id": "workflow-7",
        "name": "Captain Factory Integration Evidence",
        "nodes": [
            {"type": "n8n-nodes-base.webhook"},
            {"type": "n8n-nodes-base.set"},
        ],
    }
    digest = hashlib.sha256(
        json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    adapter, _, _ = _adapter(
        tmp_path,
        mcp_payload={
            "execution_id": "execution-42",
            "workflow_id": "workflow-7",
            "artifact_digest": digest,
            "workflow_artifact": workflow,
        },
        execution=N8nExecutionRecord(
            execution_id="execution-42",
            workflow_id="workflow-other",
            status="success",
            output={
                "artifact_digest": digest,
                "correlation_id": "10000000-0000-0000-0000-000000000001",
            },
        ),
    )
    adapter.authorization("support_triage")

    with pytest.raises(ValueError, match="REST execution evidence"):
        await adapter.tool("support_triage").run_json(
            {"arguments": {"input": {"ticket": "case-1"}}}, CancellationToken()
        )

    assert adapter.observed_evidence() == ()


@pytest.mark.asyncio
async def test_adapter_rejects_unissued_or_unknown_tool_calls(tmp_path: Path) -> None:
    adapter, registrar, _ = _adapter(
        tmp_path,
        mcp_payload={
            "workflow_artifact": {
                "id": "workflow-7",
                "name": "Captain Factory Integration Evidence",
                "nodes": [
                    {"type": "n8n-nodes-base.webhook"},
                    {"type": "n8n-nodes-base.set"},
                ],
            }
        },
        execution=N8nExecutionRecord(
            execution_id="unused",
            workflow_id="unused",
            status="success",
            output={},
        ),
    )

    with pytest.raises(ValueError, match="fresh Captain authorization"):
        await adapter.tool("support_triage").run_json(
            {"arguments": {}}, CancellationToken()
        )
    with pytest.raises(ValueError, match="not registered"):
        adapter.authorization("delete_workflow")
    assert registrar.registered == []


@pytest.mark.asyncio
async def test_model_arguments_cannot_select_a_different_workflow(tmp_path: Path) -> None:
    adapter, _, mcp = _adapter(
        tmp_path,
        mcp_payload={
            "workflow_artifact": {
                "id": "workflow-7",
                "name": "Captain Factory Integration Evidence",
                "nodes": [
                    {"type": "n8n-nodes-base.webhook"},
                    {"type": "n8n-nodes-base.set"},
                ],
            }
        },
        execution=N8nExecutionRecord(
            execution_id="unused",
            workflow_id="workflow-7",
            status="success",
            output={},
        ),
    )
    adapter.authorization("support_triage")

    with pytest.raises(ValueError, match="only body.input"):
        await adapter.tool("support_triage").run_json(
            {"arguments": {"workflowId": "workflow-attacker-selected"}},
            CancellationToken(),
        )

    assert mcp.calls == []
