from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.input_contracts import RequestedIntegration
from agenten.agent_factory.integration_setup import (
    CredentialVerificationReceiptV1,
    IntegrationCredentialRequirementV1,
    IntegrationSetupPlanner,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import N8nDeploymentToolAdapter, TypedN8nCall, TypedN8nCatalog, TypedN8nTool
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from agenten.targets.n8n import N8nDeployment, N8nExecutionEvidence
from tests.agent_factory.test_state_machine import job


NOW = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


class Mcp:
    async def call_typed_tool(self, tool, payload):
        return {"tool": tool.name, "payload": payload}


class Target:
    async def execute(self, deployment, case):
        return N8nExecutionEvidence(
            execution_id="execution-1",
            workflow_id=deployment.workflow_id,
            artifact_digest=deployment.artifact_digest,
            correlation_id=case.correlation_id,
            status="success",
        )


@pytest.mark.asyncio
async def test_n8n_call_requires_registered_tool_and_captain_n8n_lease() -> None:
    lease = issue_factory_lease(
        job=job(), role=FactoryRole.TOOL_INTEGRATOR, attempt=1,
        workspace_ref="workspace://factory/support-triage", now=NOW,
        integration_intent=IntegrationIntent.N8N,
    )
    catalog = TypedN8nCatalog((TypedN8nTool(
        name="crm_lookup", description="Look up an approved CRM record",
        input_schema_ref="artifact://schemas/crm-lookup-input",
        output_schema_ref="artifact://schemas/crm-lookup-output",
    ),))

    result = await catalog.invoke(
        lease=lease, call=TypedN8nCall(tool_name="crm_lookup", case_id="lookup-1", correlation_id="00000000-0000-0000-0000-000000000010", payload={"email": "a@example.test"}), mcp=Mcp()
    )

    assert result["tool"] == "crm_lookup"
    with pytest.raises(PermissionError, match="not registered"):
        await catalog.invoke(
            lease=lease, call=TypedN8nCall(tool_name="arbitrary_workflow", case_id="lookup-2", correlation_id="00000000-0000-0000-0000-000000000010", payload={}), mcp=Mcp()
        )


@pytest.mark.asyncio
async def test_typed_tool_deployment_adapter_returns_execution_evidence() -> None:
    adapter = N8nDeploymentToolAdapter(
        target=Target(),
        deployments={
            "crm_lookup": N8nDeployment(
                workflow_id="hidden-workflow-id",
                workflow_name="captain::factory::crm_lookup",
                webhook_path="captain-factory-crm-lookup",
                artifact_digest="a" * 64,
            )
        },
    )

    result = await adapter.call_with_context(
        TypedN8nCall(
            tool_name="crm_lookup",
            case_id="lookup-1",
            correlation_id="00000000-0000-0000-0000-000000000010",
            payload={"email": "a@example.test"},
        )
    )

    assert result["execution_id"] == "execution-1"
    assert result["workflow_id"] == "hidden-workflow-id"


@pytest.mark.asyncio
async def test_integration_bound_tool_requires_verified_connection() -> None:
    lease = issue_factory_lease(
        job=job(), role=FactoryRole.TOOL_INTEGRATOR, attempt=1,
        workspace_ref="workspace://factory/support-triage", now=NOW,
        integration_intent=IntegrationIntent.N8N,
    )
    catalog = TypedN8nCatalog((TypedN8nTool(
        name="crm_lookup", description="Look up an approved CRM record",
        input_schema_ref="artifact://schemas/crm-lookup-input",
        output_schema_ref="artifact://schemas/crm-lookup-output",
        integration_key="crm",
    ),))
    requested = RequestedIntegration(
        integration_key="crm",
        purpose="Read customer records",
        trigger="A released agent requests customer context",
        operation="Read one customer record",
        required=True,
        credential_aliases=("CRM_API_KEY",),
        success_behavior="Return the typed customer record",
        failure_behavior="Escalate without inventing customer data",
    )
    requirement = IntegrationCredentialRequirementV1(
        integration_key="crm",
        credential_alias="CRM_API_KEY",
        credential_type="httpBearerAuth",
        required=True,
        setup_method="n8n_ui",
        setup_label="Bearer Auth",
        verification_workflow_sha256="a" * 64,
    )
    metadata = N8nCredentialMetadataV1(
        credential_id="cred-prod",
        credential_name="CRM production",
        credential_type="httpBearerAuth",
        project_id="captain-production",
        project_name="Captain production",
    )
    planner = IntegrationSetupPlanner()
    unverified = planner.plan(
        integrations=(requested,), requirements=(requirement,), credentials=(metadata,)
    )
    verified = planner.plan(
        integrations=(requested,),
        requirements=(requirement,),
        credentials=(metadata,),
        verification_receipts=(CredentialVerificationReceiptV1(
            integration_key="crm",
            credential_alias="CRM_API_KEY",
            credential_id="cred-prod",
            credential_type="httpBearerAuth",
            project_id="captain-production",
            status="passed",
            occurred_at=NOW,
            workflow_ref=ArtifactRef(
                uri="artifact://n8n-workflow/" + "a" * 64,
                sha256="a" * 64,
                media_type="application/json",
            ),
            workflow_content_sha256="a" * 64,
            execution_ref=ArtifactRef(
                uri="artifact://n8n-execution/" + "b" * 64,
                sha256="b" * 64,
                media_type="application/json",
            ),
            valid_until=NOW + timedelta(minutes=30),
        ),),
        now=NOW,
    )
    call = TypedN8nCall(
        tool_name="crm_lookup",
        case_id="lookup-1",
        correlation_id="00000000-0000-0000-0000-000000000010",
        payload={"email": "a@example.test"},
    )

    with pytest.raises(PermissionError, match="integration connection is not ready"):
        await catalog.invoke(
            lease=lease,
            call=call,
            mcp=Mcp(),
            integration_setup_plan=unverified,
        )
    with pytest.raises(PermissionError, match="current evaluation time"):
        await catalog.invoke(
            lease=lease,
            call=call,
            mcp=Mcp(),
            integration_setup_plan=verified,
        )
    with pytest.raises(PermissionError, match="expired"):
        await catalog.invoke(
            lease=lease,
            call=call,
            mcp=Mcp(),
            integration_setup_plan=verified,
            now=NOW + timedelta(hours=1),
        )
    result = await catalog.invoke(
        lease=lease,
        call=call,
        mcp=Mcp(),
        integration_setup_plan=verified,
        now=NOW,
    )

    assert result["tool"] == "crm_lookup"
