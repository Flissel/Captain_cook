from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkTeamSelectionV1,
    LiveBusinessBenchmarkSettings,
)
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from agenten.agent_factory.team_execution import CaptainN8nGrantAuthority
from agenten.agent_runtime.contracts import CapabilityProfile
from agenten.agent_runtime.n8n_mcp_broker import McpLeaseIssuer
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    WorkBatch,
)
from gateway.contracts import RuntimeOperationProjection, RuntimeWriteReceipt
from gateway.business_benchmark_live_composition import (
    GatewayBusinessBenchmarkLiveCompositionLoader,
    GatewayProfiledBusinessBenchmarkExecutorBuilder,
    GatewayRenewalN8nDeploymentBinding,
    GatewayRenewalN8nRuntimeAuthority,
)
from gateway.business_benchmark_composition import (
    GatewayBusinessBenchmarkCompositionAuthority,
)
from tests.scripts.test_business_benchmark_renewal_n8n_deploy import (
    _powershell_object_digest,
)


NOW = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
JOB_ID = UUID("71000000-0000-0000-0000-000000000001")
CORRELATION_ID = UUID("71000000-0000-0000-0000-000000000002")
INVOCATION_ID = UUID("71000000-0000-0000-0000-000000000003")


def _batch() -> WorkBatch:
    return WorkBatch(
        batch_id="renewal-benchmark",
        title="Renewal benchmark read tool",
        goal="Authorize one read-only renewal context operation.",
        subtask_ids=["renewal_context_read"],
        target="n8n",
        capability_tags=["n8n-builder"],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="read-only",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
    )


class RecordingRuntimeStore:
    def __init__(self, batch: WorkBatch) -> None:
        self.batch = batch
        self.command = None
        self.grant = None

    def bundle(self, batch_id: str) -> dict[str, object]:
        assert batch_id == self.batch.batch_id
        return self.batch.model_dump(mode="json")

    def runtime_operation(self, operation_id: UUID) -> RuntimeOperationProjection:
        if self.command is None or self.command.event_id != operation_id:
            raise HTTPException(status_code=404, detail="runtime operation not found")
        return RuntimeOperationProjection(
            operation_id=operation_id,
            command=self.command,
            grant=self.grant,
        )

    def accept_runtime_command(self, command) -> RuntimeWriteReceipt:
        assert self.command in {None, command}
        replayed = self.command is not None
        self.command = command
        return RuntimeWriteReceipt(operation_id=command.event_id, replayed=replayed)

    def record_capability_grant(self, grant) -> RuntimeWriteReceipt:
        assert self.grant in {None, grant}
        replayed = self.grant is not None
        self.grant = grant
        return RuntimeWriteReceipt(operation_id=grant.command_id, replayed=replayed)


def test_renewal_runtime_authority_requires_a_real_released_work_batch() -> None:
    store = SimpleNamespace(bundle=lambda _batch_id: (_ for _ in ()).throw(KeyError()))

    with pytest.raises(ValueError, match="released WorkBatch"):
        GatewayRenewalN8nRuntimeAuthority.from_gateway_store(
            store=store,
            batch_id="renewal-benchmark",
            workspace_ref="workspace://factory/renewal-benchmark",
            endpoint_identity="http://127.0.0.1:5680",
            broker_signing_secret="test-only-signing-secret",
            clock=lambda: NOW,
        )


def test_gateway_composition_exposes_store_only_to_gateway_runtime_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SimpleNamespace()
    monkeypatch.setattr(
        "gateway.business_benchmark_composition.MariaDBStorage",
        lambda _dsn: object(),
    )
    monkeypatch.setattr(
        "gateway.business_benchmark_composition.GatewayStore",
        lambda _storage: store,
    )

    authority = GatewayBusinessBenchmarkCompositionAuthority(
        "mariadb://captain:private-password@127.0.0.1:3306/captain_test"
    )

    assert authority.runtime_store is store


def test_renewal_runtime_authority_persists_one_paired_command_and_issues_broker_lease() -> None:
    store = RecordingRuntimeStore(_batch())
    signing_secret = "test-only-signing-secret"
    authority = GatewayRenewalN8nRuntimeAuthority.from_gateway_store(
        store=store,
        batch_id=store.batch.batch_id,
        workspace_ref="workspace://factory/renewal-benchmark",
        endpoint_identity="http://127.0.0.1:5680",
        broker_signing_secret=signing_secret,
        clock=lambda: NOW,
    )
    job = SimpleNamespace(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
    )
    invocation = SimpleNamespace(
        invocation_id=INVOCATION_ID,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
    )
    tool = OpaqueN8nToolReference(
        schema="captain.n8n-mcp-tool-reference.v1",
        tool_name="renewal_context_read",
        input_schema_ref="artifact://benchmark/renewal-input",
        output_schema_ref="artifact://benchmark/renewal-output",
    )

    def request(variant: str) -> SimpleNamespace:
        return SimpleNamespace(
            identity=SimpleNamespace(
                job_id=JOB_ID,
                correlation_id=CORRELATION_ID,
                subject_version=1,
                attempt=1,
                invocation_id=INVOCATION_ID,
                variant=variant,
                case_sha256="a" * 64,
            ),
            benchmark_case_sha256="a" * 64,
            maximum_latency_ms=2500,
        )

    candidate = authority.authorization_for(
        job=job,
        invocation=invocation,
        request=request("candidate"),
        tool_reference=tool,
    )
    baseline = authority.authorization_for(
        job=job,
        invocation=invocation,
        request=request("single_agent_baseline"),
        tool_reference=tool,
    )

    assert candidate == baseline
    assert store.command == candidate.runtime_command
    assert store.grant == candidate.capability_grant
    assert candidate.runtime_command.payload.batch_id == store.batch.batch_id
    assert candidate.runtime_command.payload.subtask_id == tool.tool_name
    assert candidate.capability_grant.profile is CapabilityProfile.N8N_BUILDER
    assert candidate.capability_grant.mcp_servers == ("n8n-mcp",)

    provider_request = SimpleNamespace(
        runtime_command_id=candidate.runtime_command.event_id,
        grant_id=candidate.capability_grant.grant_id,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        invocation_id=INVOCATION_ID,
        case_sha256="a" * 64,
        tool=tool,
        workspace_ref="workspace://factory/renewal-benchmark",
    )
    token = authority.broker_token_for(provider_request)
    claim = McpLeaseIssuer(signing_secret).verify(token, NOW)

    assert claim.command_id == candidate.runtime_command.event_id
    assert claim.grant_id == candidate.capability_grant.grant_id
    assert claim.endpoint_identity == "http://127.0.0.1:5680"


@pytest.mark.asyncio
async def test_gateway_grant_state_drives_the_real_captain_n8n_authority() -> None:
    store = RecordingRuntimeStore(_batch())
    authority = GatewayRenewalN8nRuntimeAuthority.from_gateway_store(
        store=store,
        batch_id=store.batch.batch_id,
        workspace_ref="workspace://factory/renewal-benchmark",
        endpoint_identity="http://127.0.0.1:5680",
        broker_signing_secret="test-only-signing-secret",
        clock=lambda: NOW,
    )
    job = SimpleNamespace(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
    )
    invocation = SimpleNamespace(
        invocation_id=INVOCATION_ID,
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=1,
        attempt=1,
        step=FactorySkillStep.EXECUTE_TEAM,
    )
    tool = OpaqueN8nToolReference(
        schema="captain.n8n-mcp-tool-reference.v1",
        tool_name="renewal_context_read",
        input_schema_ref="artifact://benchmark/renewal-input",
        output_schema_ref="artifact://benchmark/renewal-output",
    )
    request = SimpleNamespace(
        identity=SimpleNamespace(
            job_id=JOB_ID,
            correlation_id=CORRELATION_ID,
            subject_version=1,
            attempt=1,
            invocation_id=INVOCATION_ID,
            variant="candidate",
            case_sha256="b" * 64,
        ),
        benchmark_case_sha256="b" * 64,
        maximum_latency_ms=2500,
    )
    authorization = authority.authorization_for(
        job=job,
        invocation=invocation,
        request=request,
        tool_reference=tool,
    )

    grant = await CaptainN8nGrantAuthority(authority).authorize_command(
        authorization,
        now=NOW,
    )

    assert grant == authorization.capability_grant


def _live_settings(tmp_path: Path, profile: str) -> LiveBusinessBenchmarkSettings:
    selected = (
        ("claims", JOB_ID, "claims-candidate"),
        (
            "renewal",
            UUID("71000000-0000-0000-0000-000000000011"),
            "renewal-candidate",
        ),
    )
    selected = selected if profile == "all" else tuple(
        item for item in selected if item[0] == profile
    )
    selections = tuple(
        BusinessBenchmarkTeamSelectionV1(
            profile=item[0],
            job_id=item[1],
            candidate_id=item[2],
            suite_version=1,
            attempt=1,
            maximum_usd=Decimal("0.50"),
            captain_remaining_usd=Decimal("0.50"),
        )
        for item in selected
    )
    redaction_version = "benchmark-redaction-v1"
    redaction_sha256 = hashlib.sha256(
        json.dumps(
            {"redaction_policy_version": redaction_version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return LiveBusinessBenchmarkSettings(
        profile=profile,
        provider="openai",
        model="gpt-4.1-mini",
        redaction_policy_sha256=redaction_sha256,
        selections=selections,
        maximum_usd=sum(
            (item.maximum_usd for item in selections), Decimal("0")
        ),
        allowed_models=("gpt-4.1-mini",),
        evidence_root=(
            tmp_path
            / ".captain-cook"
            / "evidence"
            / "business-benchmarks"
            / "run"
        ),
        runtime_url="http://127.0.0.1:8000",
        provider_secret_name="OPENAI_API_KEY",
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "TEST_MARIADB_DSN": (
            "mariadb://captain:private-password@127.0.0.1:3306/captain_test"
        ),
        "CAPTAIN_BENCHMARK_SEED_VERSION_ID": "business-benchmark-demo-2026-07",
        "CAPTAIN_BENCHMARK_AUTHORITY_ROOT": str(
            tmp_path / ".captain-cook" / "private" / "business-benchmarks"
        ),
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-4.1-mini",
        "OPENAI_API_KEY": "test-only-openai-secret",
        "CAPTAIN_BENCHMARK_CASE_MAX_COST_USD": "0.01",
        "CAPTAIN_BENCHMARK_CASE_MAX_LATENCY_MS": "2500",
        "CAPTAIN_BENCHMARK_REDACTION_POLICY_VERSION": "benchmark-redaction-v1",
        "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD": "0.01",
        "CAPTAIN_BENCHMARK_PRICING_MINIMUM_COST_USD": "0",
        "CAPTAIN_BENCHMARK_PRICING_INPUT_COST_PER_MILLION_USD": "0.40",
        "CAPTAIN_BENCHMARK_PRICING_OUTPUT_COST_PER_MILLION_USD": "1.60",
        "CAPTAIN_BENCHMARK_PRICING_VERSION": "openai-demo-2026-07",
        "CAPTAIN_BENCHMARK_PRICING_EFFECTIVE_AT": "2026-07-01T00:00:00+00:00",
    }


def _write_renewal_deployment_receipts(
    tmp_path: Path,
) -> tuple[Path, Path, str, str]:
    workflow_id = "renewal-workflow-id"
    canonical_path = tmp_path / "renewal_context_read.json"
    canonical = {
        "name": "Captain Renewal Context Read v1",
        "active": False,
        "nodes": [{"id": "read-only", "type": "n8n-nodes-base.code"}],
        "connections": {},
        "settings": {"availableInMCP": True},
    }
    canonical_path.write_text(
        json.dumps(canonical, sort_keys=True), encoding="utf-8"
    )
    canonical_sha256 = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
    published_payload = {
        key: canonical[key] for key in ("name", "nodes", "connections", "settings")
    }
    published_sha256 = _powershell_object_digest(
        tmp_path / "published-digest",
        published_payload,
    )
    ownership_sha256 = hashlib.sha256(
        (
            "captain.business-benchmark-renewal-n8n.v1|"
            f"Captain Renewal Context Read v1|{workflow_id}"
        ).encode("utf-8")
    ).hexdigest()
    evidence_root = tmp_path / "renewal-evidence"
    deployment_root = evidence_root / "renewal-context-n8n-deployments"
    activation_root = evidence_root / "renewal-context-n8n-activations"
    deployment_root.mkdir(parents=True)
    activation_root.mkdir(parents=True)
    (deployment_root / f"{published_sha256}.json").write_text(
        json.dumps(
            {
                "schema": (
                    "captain.business-benchmark-renewal-n8n-"
                    "deployment-receipt.v1"
                ),
                "ownership_binding_sha256": ownership_sha256,
                "workflow_id": workflow_id,
                "workflow_name": canonical["name"],
                "canonical_sha256": canonical_sha256,
                "published_sha256": published_sha256,
                "published_payload": published_payload,
                "verification": "provider_read_back_matched",
            }
        ),
        encoding="utf-8",
    )
    (activation_root / f"{published_sha256}.json").write_text(
        json.dumps(
            {
                "schema": (
                    "captain.business-benchmark-renewal-n8n-"
                    "activation-receipt.v1"
                ),
                "ownership_binding_sha256": ownership_sha256,
                "workflow_id": workflow_id,
                "workflow_name": canonical["name"],
                "published_sha256": published_sha256,
                "status": "active",
            }
        ),
        encoding="utf-8",
    )
    return evidence_root, canonical_path, workflow_id, canonical_sha256


def test_renewal_deployment_binding_requires_matching_immutable_receipts(
    tmp_path: Path,
) -> None:
    evidence_root, canonical_path, workflow_id, canonical_sha256 = (
        _write_renewal_deployment_receipts(tmp_path)
    )
    deployment_root = evidence_root / "renewal-context-n8n-deployments"
    activation_root = evidence_root / "renewal-context-n8n-activations"
    current = json.loads(next(deployment_root.glob("*.json")).read_text("utf-8"))
    historical_payload = dict(current["published_payload"])
    historical_payload["settings"] = {"availableInMCP": False}
    historical_sha256 = _powershell_object_digest(
        tmp_path / "historical-digest",
        historical_payload,
    )
    (deployment_root / f"{historical_sha256}.json").write_text(
        json.dumps(
            current
            | {
                "canonical_sha256": "0" * 64,
                "published_sha256": historical_sha256,
                "published_payload": historical_payload,
            }
        ),
        encoding="utf-8",
    )
    (activation_root / f"{historical_sha256}.json").write_text(
        json.dumps(
            {
                "schema": (
                    "captain.business-benchmark-renewal-n8n-"
                    "activation-receipt.v1"
                ),
                "ownership_binding_sha256": current["ownership_binding_sha256"],
                "workflow_id": workflow_id,
                "workflow_name": current["workflow_name"],
                "published_sha256": historical_sha256,
                "status": "active",
            }
        ),
        encoding="utf-8",
    )

    binding = GatewayRenewalN8nDeploymentBinding.from_evidence_root(
        evidence_root=evidence_root,
        canonical_workflow_path=canonical_path,
    )

    assert binding.workflow_id == workflow_id
    assert binding.workflow_ref.sha256 == canonical_sha256
    assert binding.workflow_ref.uri.endswith(canonical_sha256)

    deployment_path = next(
        path
        for path in deployment_root.glob("*.json")
        if path.stem == binding.published_sha256
    )
    tampered = json.loads(deployment_path.read_text(encoding="utf-8"))
    tampered["published_payload"]["settings"]["executionOrder"] = "v1"
    deployment_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="deployment receipt"):
        GatewayRenewalN8nDeploymentBinding.from_evidence_root(
            evidence_root=evidence_root,
            canonical_workflow_path=canonical_path,
        )
    deployment_path.write_text(json.dumps(current), encoding="utf-8")

    activation = next(
        (evidence_root / "renewal-context-n8n-activations").glob("*.json")
    )
    activation.write_text(
        activation.read_text(encoding="utf-8").replace('"active"', '"inactive"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="deployment receipt"):
        GatewayRenewalN8nDeploymentBinding.from_evidence_root(
            evidence_root=evidence_root,
            canonical_workflow_path=canonical_path,
        )


def test_gateway_live_loader_composes_claims_without_n8n_or_secret_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class Authority:
        repository = SimpleNamespace()
        budget = SimpleNamespace()
        runtime_store = SimpleNamespace()

        def __init__(self, dsn: str) -> None:
            captured["dsn"] = dsn

        def compose(self, settings, **kwargs):
            captured.update(settings=settings, **kwargs)
            return "gateway-live-composition"

    monkeypatch.setattr(
        "gateway.business_benchmark_live_composition.GatewayBusinessBenchmarkCompositionAuthority",
        Authority,
    )
    environment = _environment(tmp_path)
    client = httpx.AsyncClient()
    try:
        loader = GatewayBusinessBenchmarkLiveCompositionLoader(
            environment=environment,
            n8n_client=client,
            clock=lambda: NOW,
        )

        result = loader(_live_settings(tmp_path, "claims"))
    finally:
        import asyncio

        asyncio.run(client.aclose())

    assert result == "gateway-live-composition"
    assert isinstance(
        captured["executor_builder"],
        GatewayProfiledBusinessBenchmarkExecutorBuilder,
    )
    assert captured["executor_builder"].profiles == ("claims",)
    serialized = f"{loader!r}|{captured['executor_builder']!r}"
    assert environment["OPENAI_API_KEY"] not in serialized
    assert "private-password" not in serialized


def test_gateway_live_loader_composes_renewal_only_with_gateway_work_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    network_calls: list[httpx.Request] = []
    runtime_store = RecordingRuntimeStore(_batch())

    class Authority:
        repository = SimpleNamespace()
        budget = SimpleNamespace()

        def __init__(self, dsn: str) -> None:
            captured["dsn"] = dsn
            self.runtime_store = runtime_store

        def compose(self, settings, **kwargs):
            captured.update(settings=settings, **kwargs)
            return "gateway-renewal-composition"

    monkeypatch.setattr(
        "gateway.business_benchmark_live_composition.GatewayBusinessBenchmarkCompositionAuthority",
        Authority,
    )
    environment = _environment(tmp_path) | {
        "N8N_MODE": "captain-builder",
        "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
        "CAPTAIN_N8N_API_KEY": "test-only-api-key",
        "CAPTAIN_N8N_MCP_TOKEN": "test-only-upstream-token",
        "CAPTAIN_N8N_MCP_BROKER_URL": "http://127.0.0.1:5680",
        "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET": "test-only-signing-secret",
        "CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID": runtime_store.batch.batch_id,
        "CAPTAIN_BENCHMARK_RENEWAL_WORKSPACE_REF": (
            "workspace://factory/renewal-benchmark"
        ),
    }
    evidence_root, canonical_path, _, _ = _write_renewal_deployment_receipts(tmp_path)
    environment["CAPTAIN_BENCHMARK_RENEWAL_N8N_EVIDENCE_ROOT"] = str(evidence_root)
    environment["CAPTAIN_BENCHMARK_RENEWAL_WORKFLOW_PATH"] = str(canonical_path)
    def reject_network(request: httpx.Request) -> httpx.Response:
        network_calls.append(request)
        raise AssertionError("Renewal composition preflight must be effect-free")

    client = httpx.AsyncClient(transport=httpx.MockTransport(reject_network))
    try:
        result = GatewayBusinessBenchmarkLiveCompositionLoader(
            environment=environment,
            n8n_client=client,
            clock=lambda: NOW,
        )(_live_settings(tmp_path, "all"))
    finally:
        import asyncio

        asyncio.run(client.aclose())

    assert result == "gateway-renewal-composition"
    builder = captured["executor_builder"]
    assert isinstance(builder, GatewayProfiledBusinessBenchmarkExecutorBuilder)
    assert builder.profiles == ("claims", "renewal")
    assert network_calls == []


def test_gateway_live_loader_fails_before_provider_without_renewal_work_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Authority:
        repository = SimpleNamespace()
        budget = SimpleNamespace()
        runtime_store = SimpleNamespace(
            bundle=lambda _batch_id: (_ for _ in ()).throw(
                HTTPException(status_code=404, detail="batch not found")
            )
        )

        def __init__(self, _dsn: str) -> None:
            pass

    monkeypatch.setattr(
        "gateway.business_benchmark_live_composition.GatewayBusinessBenchmarkCompositionAuthority",
        Authority,
    )
    environment = _environment(tmp_path) | {
        "N8N_MODE": "captain-builder",
        "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
        "CAPTAIN_N8N_API_KEY": "test-only-api-key",
        "CAPTAIN_N8N_MCP_TOKEN": "test-only-upstream-token",
        "CAPTAIN_N8N_MCP_BROKER_URL": "http://127.0.0.1:5680",
        "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET": "test-only-signing-secret",
        "CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID": "renewal-benchmark",
        "CAPTAIN_BENCHMARK_RENEWAL_WORKSPACE_REF": (
            "workspace://factory/renewal-benchmark"
        ),
        "CAPTAIN_BENCHMARK_RENEWAL_WORKFLOW_ID": "renewal-workflow-id",
        "CAPTAIN_BENCHMARK_RENEWAL_WORKFLOW_SHA256": "d" * 64,
    }
    client = httpx.AsyncClient()
    try:
        with pytest.raises(ValueError, match="released WorkBatch"):
            GatewayBusinessBenchmarkLiveCompositionLoader(
                environment=environment,
                n8n_client=client,
                clock=lambda: NOW,
            )(_live_settings(tmp_path, "renewal"))
    finally:
        import asyncio

        asyncio.run(client.aclose())
