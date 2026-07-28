from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from autogen_core import CancellationToken

from agenten.agent_factory.business_benchmark_n8n import (
    CaptainRenewalContextN8nAdapter,
    RenewalCommercialSnapshotV1,
    RenewalContextN8nProviderResponseV1,
    RenewalContextReadInputV1,
    RenewalContextReadOutputV1,
    RenewalContextTransientError,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    factory_execution_policy_sha256,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    CaptainAuthorizedN8nTool,
    FactoryN8nToolAuthorizationV1,
    HostAutoGenSessionIdentityV1,
)
from agenten.agent_runtime.capabilities import PROFILE_CAPABILITIES
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeLimits,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from agenten.targets.n8n import N8nExecutionEvidence


NOW = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
TOOL_NAME = "renewal_context_read"


def artifact(label: str, digest: str, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://benchmark-n8n-tests/{label}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def job() -> AgentFactoryJobV3:
    return AgentFactoryJobV3.model_validate(
        {
            "schema": "captain.agent-factory-job.v3",
            "event_id": "91000000-0000-0000-0000-000000000001",
            "correlation_id": "91000000-0000-0000-0000-000000000002",
            "occurred_at": NOW,
            "producer": "captain",
            "job_id": "91000000-0000-0000-0000-000000000003",
            "subject_version": 2,
            "input_ref": artifact("input", "a" * 64, "text/markdown"),
            "compiled_spec_ref": artifact("spec", "b" * 64),
            "dependency_graph_ref": artifact("graph", "c" * 64),
            "required_capability": "customer_renewal_orchestration_team",
            "acceptance_assertion_ids": ["business_value"],
            "private_holdout_refs": [
                {
                    "holdout_id": "holdout-dddddddddddd",
                    "uri": "holdout://holdout-dddddddddddd",
                    "sha256": "6" * 64,
                }
            ],
            "deadline_at": NOW + timedelta(minutes=15),
            "execution_policy": {
                "schema": "captain.factory-execution-policy.v1",
                "mode": "demo",
                "live_execution": True,
                "max_cost_usd": "5.00",
                "max_runtime_seconds": 900,
                "required_live_runs": 1,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            },
        }
    )


def invocation(current_job: AgentFactoryJobV3) -> FactorySkillInvocationV1:
    lease = issue_factory_lease(
        job=current_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://business-benchmark/renewal",
        now=NOW,
    )
    return FactorySkillInvocationV1(
        schema="captain.factory-skill-invocation.v1",
        invocation_id=UUID("91000000-0000-0000-0000-000000000004"),
        job_id=current_job.job_id,
        correlation_id=current_job.correlation_id,
        subject_version=current_job.subject_version,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
        released_skill=ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id="captain-factory-execute-team",
            version=1,
            capability="factory_workflow",
            content_ref=artifact("skill", "e" * 64),
            content_sha256="e" * 64,
            status="released",
            released_at=NOW,
            producer="captain",
        ),
        input_ref=current_job.input_ref,
        input_sha256=current_job.input_ref.sha256,
        lease=lease,
        idempotency_key="f" * 64,
        acceptance_assertion_ids=current_job.acceptance_assertion_ids,
        execution_scope_ref=current_job.private_holdout_refs[0],
    )


def tool_ref():
    return TypedN8nTool(
        name=TOOL_NAME,
        description="Read a Captain-scoped synthetic renewal context without mutation.",
        input_schema_ref="artifact://factory-seed/renewal/input/" + "1" * 64,
        output_schema_ref="artifact://factory-seed/renewal/output/" + "2" * 64,
    ).opaque_reference()


def command_ref() -> ArtifactRef:
    encoded = json.dumps(
        tool_ref().model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return artifact("n8n-command", hashlib.sha256(encoded).hexdigest())


def authorization(current_job: AgentFactoryJobV3) -> FactoryN8nToolAuthorizationV1:
    command = AgentRuntimeCommand(
        schema="captain.agent-runtime-command.v1",
        event_id=UUID("91000000-0000-0000-0000-000000000005"),
        correlation_id=current_job.correlation_id,
        occurred_at=NOW,
        producer="captain",
        subject_id=TOOL_NAME,
        subject_version=current_job.subject_version,
        payload=AgentRuntimeCommandPayload(
            operation=RuntimeOperation.CODEX_RUN,
            project_id="factory-renewal",
            batch_id="benchmark-renewal",
            subtask_id=TOOL_NAME,
            workspace_ref="workspace://business-benchmark/renewal/n8n",
            prompt_ref=command_ref(),
            integration_intent=IntegrationIntent.N8N,
            capability_profile=CapabilityProfile.N8N_BUILDER,
            limits=RuntimeLimits(wall_seconds=30, max_iterations=2),
        ),
    )
    grant = CapabilityGrant(
        schema="captain.capability-grant.v1",
        grant_id="grant-renewal-context",
        command_id=command.event_id,
        batch_id=command.payload.batch_id,
        batch_version=current_job.subject_version,
        subtask_id=TOOL_NAME,
        workspace_ref=command.payload.workspace_ref,
        profile=CapabilityProfile.N8N_BUILDER,
        capabilities=tuple(sorted(PROFILE_CAPABILITIES[CapabilityProfile.N8N_BUILDER])),
        mcp_servers=("n8n-mcp",),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    return FactoryN8nToolAuthorizationV1(
        tool_name=TOOL_NAME,
        approved_tool_ref=tool_ref(),
        runtime_command=command,
        capability_grant=grant,
    )


def identity(
    current_job: AgentFactoryJobV3,
    current_invocation: FactorySkillInvocationV1,
) -> HostAutoGenSessionIdentityV1:
    return HostAutoGenSessionIdentityV1(
        job_id=current_job.job_id,
        correlation_id=current_job.correlation_id,
        subject_id="customer_renewal_orchestration_team_v1",
        subject_version=current_job.subject_version,
        attempt=current_invocation.attempt,
        invocation_id=current_invocation.invocation_id,
        request_id=UUID("91000000-0000-0000-0000-000000000006"),
        runtime_session_id="benchmark-session-candidate-renewal",
        case_id="holdout-dddddddddddd",
        case_sha256="d" * 64,
        variant="candidate",
        effect_id="3" * 64,
        claim_id=UUID("91000000-0000-0000-0000-000000000007"),
        fence=4,
        model="approved-model-id",
        execution_policy_sha256=factory_execution_policy_sha256(current_job),
    )


def endpoint(*, port: int = 5679) -> N8nEndpoint:
    return N8nEndpoint(
        mode="captain-builder",
        api_base_url=f"http://localhost:{port}",
        webhook_base_url=f"http://localhost:{port}",
        api_key="captain-demo-secret-never-persist",
        mcp_token="captain-mcp-secret-never-persist",
        mcp_broker_url="http://localhost:5680",
    )


def input_value() -> RenewalContextReadInputV1:
    return RenewalContextReadInputV1(
        operation="read_renewal_context",
        idempotency_key="agent-proposed-key-0001",
        evidence_partition="ordinary",
        synthetic_subject_id="subject-demo1",
        commercial_snapshot=RenewalCommercialSnapshotV1(
            renewal_window="30_days",
            engagement_band="medium",
            commercial_evidence_state="complete",
            consent_state="granted",
        ),
    )


class Authority:
    def __init__(self, claim: FactoryN8nToolAuthorizationV1) -> None:
        self.claim = claim
        self.calls = 0

    async def authorize_command(self, claim, *, now):
        self.calls += 1
        assert claim == self.claim
        assert now == NOW + timedelta(seconds=1)
        return claim.capability_grant


class Transport:
    def __init__(self) -> None:
        self.requests = []
        self.failures = 0
        self.mutate = None

    async def execute(self, *, endpoint, request, timeout_seconds):
        self.requests.append(request)
        assert endpoint.api_base_url == "http://localhost:5679"
        assert timeout_seconds == 0.05
        if self.failures:
            self.failures -= 1
            raise RenewalContextTransientError("temporary provider outage with secret")
        output = RenewalContextReadOutputV1(
            operation="read_renewal_context",
            idempotency_key=request.input_payload.idempotency_key,
            status="read",
            facts=(
                "renewal_window.30_days",
                "engagement_band.medium",
                "commercial_evidence_state.complete",
                "consent_state.granted",
            ),
        )
        response = RenewalContextN8nProviderResponseV1(
            request=request,
            effect="read_only",
            mcp_call_id="mcp-renewal-call-1",
            workflow_ref=artifact("renewal-workflow", "4" * 64),
            execution=N8nExecutionEvidence(
                execution_id="renewal-execution-1",
                workflow_id="renewal-workflow-1",
                artifact_digest="4" * 64,
                correlation_id=str(request.correlation_id),
                status="success",
            ),
            runtime_result=AgentRuntimeResult(
                schema="captain.agent-runtime-result.v1",
                event_id=UUID("91000000-0000-0000-0000-000000000008"),
                command_id=request.runtime_command_id,
                correlation_id=request.correlation_id,
                occurred_at=NOW + timedelta(seconds=1),
                producer="agent-runtime",
                subject_id=TOOL_NAME,
                subject_version=request.subject_version,
                grant_id=request.grant_id,
                operation=RuntimeOperation.CODEX_RUN,
                status=RuntimeStatus.SUCCEEDED,
            ),
            output=output,
        )
        return self.mutate(response) if self.mutate else response


def build_adapter(tmp_path: Path, transport: Transport):
    current_job = job()
    current_invocation = invocation(current_job)
    claim = authorization(current_job)
    store = FilesystemFactoryEvidenceStore(tmp_path / "evidence")
    adapter = CaptainRenewalContextN8nAdapter(
        job=current_job,
        invocation=current_invocation,
        identity=identity(current_job, current_invocation),
        authorization=claim,
        endpoint=endpoint(),
        allowed_endpoint_urls=frozenset({"http://localhost:5679"}),
        workflow_ref=artifact("renewal-workflow", "4" * 64),
        transport=transport,
        evidence_store=store,
        clock=lambda: NOW + timedelta(seconds=1),
        timeout_seconds=0.05,
        max_attempts=2,
    )
    return adapter, claim, store


@pytest.mark.asyncio
async def test_grant_authorized_renewal_read_records_exact_paired_evidence(
    tmp_path: Path,
) -> None:
    transport = Transport()
    adapter, claim, store = build_adapter(tmp_path, transport)
    authority = Authority(claim)
    tool = CaptainAuthorizedN8nTool(
        name=TOOL_NAME,
        approved_tool_ref=tool_ref(),
        adapter=adapter,
        authority=authority,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    result = await tool.run(input_value(), CancellationToken())

    assert result["status"] == "read"
    assert len(result["idempotency_key"]) == 64
    assert result["idempotency_key"] != input_value().idempotency_key
    assert authority.calls == 1
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.job_id == job().job_id
    assert request.invocation_id == invocation(job()).invocation_id
    assert request.workspace_ref == claim.runtime_command.payload.workspace_ref
    assert request.tool == tool_ref()
    assert request.input_payload.idempotency_key == result["idempotency_key"]
    assert request.effect_id == "3" * 64
    assert request.claim_id == UUID("91000000-0000-0000-0000-000000000007")
    assert request.fence == 4
    evidence = adapter.observed_evidence()
    assert len(evidence) == 1
    assert evidence[0].tool_name == TOOL_NAME
    assert evidence[0].runtime_command == claim.runtime_command
    assert evidence[0].capability_grant == claim.capability_grant
    persisted = await store.read(evidence[0].evidence_ref)
    assert b"captain.renewal-context-n8n-receipt.v1" in persisted
    assert b"captain-demo-secret-never-persist" not in persisted
    assert b"captain-mcp-secret-never-persist" not in persisted
    assert b"30_days" not in persisted
    assert b"subject-demo1" not in persisted


@pytest.mark.asyncio
async def test_exact_replay_is_cached_and_does_not_repeat_provider_effect(
    tmp_path: Path,
) -> None:
    transport = Transport()
    adapter, _, _ = build_adapter(tmp_path, transport)
    delegate = adapter.tool(TOOL_NAME)

    first = await delegate.run(input_value(), CancellationToken())
    second = await delegate.run(input_value(), CancellationToken())

    assert second == first
    assert len(transport.requests) == 1
    assert len(adapter.observed_evidence()) == 1


def test_adapter_rejects_vibemind_or_non_allowlisted_endpoint_before_effect(
    tmp_path: Path,
) -> None:
    current_job = job()
    current_invocation = invocation(current_job)
    for selected in (endpoint(port=15678), endpoint(port=5678)):
        with pytest.raises(ValueError, match="Captain-owned|allowlist|VibeMind"):
            CaptainRenewalContextN8nAdapter(
                job=current_job,
                invocation=current_invocation,
                identity=identity(current_job, current_invocation),
                authorization=authorization(current_job),
                endpoint=selected,
                allowed_endpoint_urls=frozenset({"http://localhost:5679"}),
                workflow_ref=artifact("renewal-workflow", "4" * 64),
                transport=Transport(),
                evidence_store=FilesystemFactoryEvidenceStore(tmp_path),
                clock=lambda: NOW,
            )


def test_adapter_rejects_stale_factory_invocation_before_effect(tmp_path: Path) -> None:
    current_job = job()
    current_invocation = invocation(current_job)
    stale_identity = identity(current_job, current_invocation).model_copy(
        update={"invocation_id": UUID("92000000-0000-0000-0000-000000000002")}
    )

    with pytest.raises(ValueError, match="job, invocation"):
        CaptainRenewalContextN8nAdapter(
            job=current_job,
            invocation=current_invocation,
            identity=stale_identity,
            authorization=authorization(current_job),
            endpoint=endpoint(),
            allowed_endpoint_urls=frozenset({"http://localhost:5679"}),
            workflow_ref=artifact("renewal-workflow", "4" * 64),
            transport=Transport(),
            evidence_store=FilesystemFactoryEvidenceStore(tmp_path),
            clock=lambda: NOW,
        )


def test_input_model_matches_the_seed_contract_without_hidden_restrictions() -> None:
    seed_schema = json.loads(
        Path(
            "examples/business_benchmark_candidates/"
            "customer_renewal_orchestration_team/schemas/"
            "renewal_context_read.input.json"
        ).read_text(encoding="utf-8")
    )
    model_schema = RenewalContextReadInputV1.model_json_schema()
    snapshot_schema = model_schema["$defs"]["RenewalCommercialSnapshotV1"]

    assert model_schema["additionalProperties"] == seed_schema["additionalProperties"]
    assert set(model_schema["required"]) == set(seed_schema["required"])
    for name in ("operation", "idempotency_key", "evidence_partition", "synthetic_subject_id"):
        expected = seed_schema["properties"][name]
        actual = model_schema["properties"][name]
        normalized = {key: value for key, value in actual.items() if key != "title"}
        if "const" in normalized or "enum" in normalized:
            normalized.pop("type", None)
        assert normalized == expected
    expected_snapshot = seed_schema["properties"]["commercial_snapshot"]
    assert snapshot_schema["additionalProperties"] == expected_snapshot["additionalProperties"]
    assert set(snapshot_schema["required"]) == set(expected_snapshot["required"])
    for name, expected in expected_snapshot["properties"].items():
        actual = snapshot_schema["properties"][name]
        assert {key: value for key, value in actual.items() if key != "title"} == expected


def test_output_model_matches_the_seed_contract() -> None:
    seed_schema = json.loads(
        Path(
            "examples/business_benchmark_candidates/"
            "customer_renewal_orchestration_team/schemas/"
            "renewal_context_read.output.json"
        ).read_text(encoding="utf-8")
    )
    model_schema = RenewalContextReadOutputV1.model_json_schema()

    assert model_schema["additionalProperties"] == seed_schema["additionalProperties"]
    assert set(model_schema["required"]) == set(seed_schema["required"])
    for name, expected in seed_schema["properties"].items():
        actual = {
            key: value
            for key, value in model_schema["properties"][name].items()
            if key != "title"
        }
        if "const" in actual:
            actual.pop("type", None)
        assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["fence", "invocation_id", "workspace_ref", "tool"])
async def test_provider_binding_tamper_fails_closed_without_evidence(
    tmp_path: Path,
    field: str,
) -> None:
    transport = Transport()
    adapter, _, _ = build_adapter(tmp_path, transport)

    def mutate(response):
        request = response.request
        if field == "tool":
            request = request.model_copy(
                update={"tool": request.tool.model_copy(update={"output_schema_ref": "artifact://forged"})}
            )
        else:
            replacements = {
                "fence": request.fence + 1,
                "invocation_id": UUID("92000000-0000-0000-0000-000000000001"),
                "workspace_ref": "workspace://forged",
            }
            request = request.model_copy(update={field: replacements[field]})
        return response.model_copy(update={"request": request})

    transport.mutate = mutate
    with pytest.raises(ValueError, match="binding|request"):
        await adapter.tool(TOOL_NAME).run(input_value(), CancellationToken())
    assert adapter.observed_evidence() == ()


@pytest.mark.asyncio
async def test_transient_retry_reuses_idempotency_then_fails_sanitized(
    tmp_path: Path,
) -> None:
    transport = Transport()
    transport.failures = 2
    adapter, _, _ = build_adapter(tmp_path, transport)

    with pytest.raises(RuntimeError, match="Captain n8n renewal read failed") as error:
        await adapter.tool(TOOL_NAME).run(input_value(), CancellationToken())

    assert "secret" not in str(error.value)
    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert adapter.observed_evidence() == ()


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_does_not_publish_evidence(tmp_path: Path) -> None:
    class SlowTransport(Transport):
        async def execute(self, **kwargs):
            self.requests.append(kwargs["request"])
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

    transport = SlowTransport()
    adapter, _, _ = build_adapter(tmp_path, transport)

    with pytest.raises(RuntimeError, match="Captain n8n renewal read failed"):
        await adapter.tool(TOOL_NAME).run(input_value(), CancellationToken())

    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert adapter.observed_evidence() == ()
