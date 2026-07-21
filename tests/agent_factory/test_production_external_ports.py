from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import parse_factory_input_bytes
from agenten.agent_factory.job_builder import build_factory_job_v3
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.production_external_ports import (
    ProductionExternalPortsConfigurationError,
    build_production_v3_external_ports,
)
from agenten.agent_factory.production_holdout_policy import (
    CanonicalInputHoldoutPolicy,
)
from agenten.agent_factory.production_model_pricing import (
    OpenAIFactoryModelClientBuilder,
)
from agenten.agent_factory.production_n8n_adapter import (
    CaptainBrokerN8nToolAdapter,
    CaptainN8nToolBinding,
)
from agenten.agent_factory.team_execution import CaptainN8nGrantAuthority
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "TO_BE_BUILT.valid.md"


def _environment() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "fixture-provider-secret",
        "CAPTAIN_FACTORY_PROVIDER": "openai",
        "CAPTAIN_FACTORY_MODEL": "gpt-5.2",
        "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD": "1.25",
        "CAPTAIN_FACTORY_PRICING_VERSION": "2026-07-21",
        "CAPTAIN_FACTORY_PRICING_EFFECTIVE_AT": "2026-07-21T00:00:00Z",
        "CAPTAIN_FACTORY_PRICING_INPUT_COST_PER_MILLION_USD": "1.75",
        "CAPTAIN_FACTORY_PRICING_OUTPUT_COST_PER_MILLION_USD": "14.00",
        "CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD": "0.01",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "fixture-runtime-token",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_GATEWAY_TOKEN": "fixture-gateway-token",
        "N8N_MODE": "captain-builder",
        "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
        "CAPTAIN_N8N_API_KEY": "fixture-n8n-key",
        "CAPTAIN_N8N_MCP_BROKER_URL": "http://127.0.0.1:5680",
        "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET": "fixture-signing-secret",
        "CAPTAIN_N8N_PROJECT_ID": "factory-live-demo",
        "CAPTAIN_N8N_BATCH_ID": "factory-live-demo-n8n",
        "CAPTAIN_N8N_WORKSPACE_REF": "workspace://factory-live-demo/n8n",
        "CAPTAIN_FACTORY_N8N_WORKFLOW_ID": "uROkVuVjYGnw8Dfm",
    }


def _policy() -> FactoryExecutionPolicyV1:
    return FactoryExecutionPolicyV1(
        schema_name="captain.factory-execution-policy.v1",
        mode=FactoryExecutionMode.RELEASE,
        live_execution=True,
        max_cost_usd="12.00",
        max_runtime_seconds=900,
        required_live_runs=3,
        allowed_models=("gpt-5.2",),
        live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
    )


def _job(
    source: bytes,
    correlation_id: UUID,
    artifacts: ContentAddressedArtifactStore,
) -> object:
    document = parse_factory_input_bytes(source, "TO_BE_BUILT.md")
    compiled = FactoryInputCompiler(
        holdout_store=InMemoryPrivateHoldoutStore()
    ).compile(document, 3)
    stored = artifacts.put(source, "text/markdown", namespace="factory-input")
    assert stored.sha256 == document.input_ref.sha256
    return build_factory_job_v3(
        compiled,
        correlation_id=correlation_id,
        now=NOW,
        execution_policy=_policy(),
    )


def _candidate_ref(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://candidate/{digest}",
        sha256=digest,
        media_type="application/zip",
    )


def _typed_tool() -> TypedN8nTool:
    return TypedN8nTool(
        name="support_triage",
        description="Triage support",
        input_schema_ref="artifact://schemas/support-in",
        output_schema_ref="artifact://schemas/support-out",
    )


def _binding() -> CaptainN8nToolBinding:
    return CaptainN8nToolBinding(
        tool=_typed_tool(),
        mcp_tool_name="execute_workflow",
        workflow_id="uROkVuVjYGnw8Dfm",
        workflow_name="Captain Factory Integration Evidence",
    )


class CandidateProvider:
    def __init__(self, refs: dict[UUID, ArtifactRef]) -> None:
        self._refs = refs

    def candidate_for(self, job: object, candidate: object) -> object:
        reference = self._refs[job.job_id]
        assert candidate.source_ref == reference
        return SimpleNamespace(
            candidate=SimpleNamespace(
                source_archive_ref=reference,
                n8n_tools=(_typed_tool(),),
            ),
            source_archive=Path("sealed.zip"),
        )


def _request_candidate(reference: ArtifactRef) -> object:
    return SimpleNamespace(source_ref=reference)


def test_singleton_builder_scopes_two_inputs_candidates_and_correlations_per_job(
    tmp_path: Path,
) -> None:
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    source_a = FIXTURE.read_bytes()
    source_b = source_a.replace(
        b"# Customer Support Triage",
        b"# Customer Support Routing",
        1,
    )
    job_a = _job(
        source_a,
        UUID("10000000-0000-0000-0000-000000000001"),
        artifacts,
    )
    job_b = _job(
        source_b,
        UUID("20000000-0000-0000-0000-000000000002"),
        artifacts,
    )
    ref_a = _candidate_ref("candidate-a")
    ref_b = _candidate_ref("candidate-b")
    effects: list[str] = []

    def sync_handler(request: httpx.Request) -> httpx.Response:
        effects.append(request.url.path)
        status = 202 if request.url.path.endswith("/commands") else 201
        return httpx.Response(status, json={"operation_id": str(job_a.job_id), "replayed": False})

    async def async_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("builder must not dispatch async effects")

    with httpx.Client(transport=httpx.MockTransport(sync_handler)) as sync_http:
        gateway_http = httpx.AsyncClient(transport=httpx.MockTransport(async_handler))
        n8n_http = httpx.AsyncClient(transport=httpx.MockTransport(async_handler))
        try:
            ports = build_production_v3_external_ports(
                _environment(),
                candidate_provider=CandidateProvider(
                    {job_a.job_id: ref_a, job_b.job_id: ref_b}
                ),
                candidate_attestation=Mock(),
                artifacts=artifacts,
                n8n_bindings_for=lambda *_: (_binding(),),
                gateway_sync_http=sync_http,
                gateway_async_http=gateway_http,
                n8n_async_http=n8n_http,
            )
            ports.candidate_provider.candidate_for(job_a, _request_candidate(ref_a))
            ports.candidate_provider.candidate_for(job_b, _request_candidate(ref_b))
            holdout_a = ports.holdout_ports_for(job_a)
            holdout_b = ports.holdout_ports_for(job_b)
            n8n_a = ports.n8n_ports_for(job_a)
            n8n_b = ports.n8n_ports_for(job_b)

            assert isinstance(ports.model_client_for, OpenAIFactoryModelClientBuilder)
            assert isinstance(holdout_a.holdout_source, CanonicalInputHoldoutPolicy)
            assert isinstance(holdout_b.holdout_source, CanonicalInputHoldoutPolicy)
            assert holdout_a is ports.holdout_ports_for(job_a)
            assert holdout_b is ports.holdout_ports_for(job_b)
            assert holdout_a is not holdout_b
            assert holdout_a.holdout_source.policy_ref != holdout_b.holdout_source.policy_ref
            assert isinstance(n8n_a.n8n_adapter, CaptainBrokerN8nToolAdapter)
            assert isinstance(n8n_b.n8n_adapter, CaptainBrokerN8nToolAdapter)
            assert isinstance(n8n_a.n8n_authority, CaptainN8nGrantAuthority)
            assert n8n_a is not n8n_b
            claim_a = n8n_a.n8n_adapter.authorization("support_triage")
            claim_b = n8n_b.n8n_adapter.authorization("support_triage")
            assert claim_a.runtime_command.correlation_id == job_a.correlation_id
            assert claim_b.runtime_command.correlation_id == job_b.correlation_id
        finally:
            asyncio.run(gateway_http.aclose())
            asyncio.run(n8n_http.aclose())

    assert effects == [
        "/v1/runtime/commands",
        "/v1/runtime/grants",
        "/v1/runtime/commands",
        "/v1/runtime/grants",
    ]


@pytest.mark.parametrize("name", ("CAPTAIN_GATEWAY_URL", "CAPTAIN_GATEWAY_TOKEN"))
def test_runtime_credentials_cannot_substitute_for_gateway_authority(
    tmp_path: Path,
    name: str,
) -> None:
    environ = _environment()
    environ[name] = ""
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    job = _job(
        FIXTURE.read_bytes(),
        UUID("10000000-0000-0000-0000-000000000001"),
        artifacts,
    )
    reference = _candidate_ref("candidate")
    sync_http = httpx.Client()
    gateway_http = httpx.AsyncClient()
    n8n_http = httpx.AsyncClient()
    try:
        ports = build_production_v3_external_ports(
            environ,
            candidate_provider=CandidateProvider({job.job_id: reference}),
            candidate_attestation=Mock(),
            artifacts=artifacts,
            n8n_bindings_for=lambda *_: (_binding(),),
            gateway_sync_http=sync_http,
            gateway_async_http=gateway_http,
            n8n_async_http=n8n_http,
        )
        ports.candidate_provider.candidate_for(job, _request_candidate(reference))
        with pytest.raises(
            ProductionExternalPortsConfigurationError,
            match=name,
        ):
            ports.n8n_ports_for(job)
    finally:
        sync_http.close()
        asyncio.run(gateway_http.aclose())
        asyncio.run(n8n_http.aclose())
