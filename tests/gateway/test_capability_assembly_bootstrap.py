from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.contracts import AgentFactoryJobV3
from gateway.capability_assembly_bootstrap import (
    CapabilityAssemblyBootstrapConfigurationError,
    CaptainBenchmarkToolEvidenceAuthority,
    GatewayCapabilityN8nWorkBatchAuthority,
    GatewayPrivateHoldoutStore,
    GatewayCapabilityProjectionAuthority,
    build_production_capability_assembly_from_environment,
    load_capability_assembly_bootstrap_settings,
    plan_capability_assembly_factory_runtime_from_environment,
)
from gateway.capability_generic_benchmark import (
    BenchmarkToolEvidenceVerificationRequest,
    GenericBenchmarkCategory,
    GenericBenchmarkAggregate,
    GenericBenchmarkCaseReceipt,
    GenericBenchmarkRecordingRequest,
    GenericPrivateCaseRef,
)
from agenten.agent_runtime.captain_production_adapters import ContentAddressedArtifactAdapter
from gateway.capability_assembly_bootstrap import GatewayGenericBenchmarkRecorder
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable
from agenten.delivery.minibook_events import MinibookProjectionEvent
from gateway.store import GatewayStore
from gateway.capability_assembly_production import RepositoryFactoryInputArtifactStore


def _ref(payload: bytes) -> ArtifactRef:
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactRef(
        uri=f"artifact://sha256/{digest}",
        sha256=digest,
        media_type="application/json",
    )


class _Artifacts:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        return self.payloads[reference.sha256]


def test_bootstrap_settings_fail_before_path_or_network_use(tmp_path: Path) -> None:
    touched: list[str] = []

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="configuration is incomplete",
    ):
        load_capability_assembly_bootstrap_settings(
            environment={},
            repository_root=tmp_path / "must-not-be-resolved",
            touch_probe=lambda: touched.append("touched"),
        )

    assert touched == []


@pytest.mark.parametrize(
    "name,value",
    (
        ("CAPTAIN_PROVIDER_CONTEXT_SHA256", "x" * 64),
        ("CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256", "x" * 64),
        ("CAPTAIN_PROVIDER_PROXY_CODE_SHA256", "x" * 64),
        ("CAPTAIN_PROVIDER_PAID_COMMAND_SHA256", "x" * 64),
        ("CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED", "false"),
    ),
)
def test_bootstrap_rejects_each_d7_authority_drift_before_effects(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    inputs = {
        "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md": "1" * 64,
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md": "2" * 64,
        "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md": "3" * 64,
    }
    environment = {
        "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION": "a" * 40,
        "CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON": json.dumps(inputs),
        "CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON": json.dumps(
            {digest: [] for digest in inputs.values()}
        ),
        "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION": "1",
        "CAPTAIN_PRIVATE_BENCHMARK_ROOT": str((tmp_path / "private").resolve()),
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_PATH": "catalog.json",
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_SHA256": "e" * 64,
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-plan-token",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT": "artifacts",
        "CAPTAIN_CAPABILITY_AUTHORITY_ROOT": "authority",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8088",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-plan-token",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-plan-key",
        "MINIBOOK_PROJECTION_API_KEY": "projection-plan-key",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-plan-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-plan-token",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
    }
    environment[name] = value
    touched: list[str] = []

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="provider authority is invalid",
    ):
        load_capability_assembly_bootstrap_settings(
            environment=environment,
            repository_root=tmp_path,
            touch_probe=lambda: touched.append("effect"),
        )

    assert touched == []


def test_bootstrap_settings_reject_non_loopback_http_and_redact_secrets(
    tmp_path: Path,
) -> None:
    environment = {
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://benchmark.example/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-secret-value",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT": ".captain-cook/artifacts/sha256",
        "CAPTAIN_CAPABILITY_AUTHORITY_ROOT": ".captain-cook/capability-authority",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8088",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret-value",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-secret-value",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret-value",
        "CAPTAIN_CAPABILITY_ALLOWED_HOSTS": "",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret-value",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-secret-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
    }

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="endpoint is not allowed",
    ) as captured:
        load_capability_assembly_bootstrap_settings(
            environment=environment,
            repository_root=tmp_path,
        )

    rendered = str(captured.value)
    assert "benchmark-secret-value" not in rendered
    assert "gateway-secret-value" not in rendered


def test_tool_evidence_authority_requires_three_gateway_bound_cas_documents() -> None:
    trace_id = uuid4()
    run_id = uuid4()
    policy = "a" * 64
    tool_key = "crm.lookup"
    case_digest = "b" * 64
    call_id = "call-crm-lookup"
    call_payload = json.dumps(
        {
            "schema": "captain.capability-benchmark-tool-call.v1",
            "case_digest": case_digest,
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "model_policy_digest": policy,
            "operation": "read",
            "call_id": call_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    lease_payload = json.dumps(
        {
            "schema": "captain.capability-benchmark-tool-lease.v1",
            "case_digest": case_digest,
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "model_policy_digest": policy,
            "call_id": call_id,
            "call_sha256": hashlib.sha256(call_payload).hexdigest(),
            "lease_id": "lease-crm-lookup",
            "scope": ["read"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    receipt_payload = json.dumps(
        {
            "schema": "captain.capability-benchmark-tool-receipt.v1",
            "case_digest": case_digest,
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "model_policy_digest": policy,
            "call_id": call_id,
            "call_sha256": hashlib.sha256(call_payload).hexdigest(),
            "lease_sha256": hashlib.sha256(lease_payload).hexdigest(),
            "status": "succeeded",
            "result_ref_sha256": "c" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    refs = tuple(_ref(item) for item in (call_payload, lease_payload, receipt_payload))
    registry = {
        "tool_key": tool_key,
        "trace_id": str(trace_id),
        "run_id": str(run_id),
        "model_policy_digest": policy,
        "case_digest": case_digest,
        "call_ref": refs[0].model_dump(mode="json"),
        "lease_ref": refs[1].model_dump(mode="json"),
        "receipt_ref": refs[2].model_dump(mode="json"),
        "batch_id": "n8n-batch",
        "execution_result_id": str(uuid4()),
    }
    store = SimpleNamespace(
        capability_tool_evidence=lambda _digest: registry
    )
    authority = CaptainBenchmarkToolEvidenceAuthority(
        gateway_store=store,
        artifacts=_Artifacts(
            {
                hashlib.sha256(item).hexdigest(): item
                for item in (call_payload, lease_payload, receipt_payload)
            }
        ),
    )
    request = BenchmarkToolEvidenceVerificationRequest(
        case_ref=GenericPrivateCaseRef(
            case_id="case-a",
            category=GenericBenchmarkCategory.NORMAL,
            assertion_ids=("a",),
            case_digest=case_digest,
        ),
        tool_key=tool_key,
        call_ref=refs[0],
        lease_ref=refs[1],
        receipt_ref=refs[2],
        trace_id=trace_id,
        run_id=run_id,
        model_policy_digest=policy,
    )

    assert authority.gateway_store is store
    assert authority.verify(request) is True
    changed = request.__class__(
        **{**request.__dict__, "tool_key": "crm.delete"}
    )
    assert authority.verify(changed) is False


def test_gateway_tool_registry_persists_case_bound_entry() -> None:
    rows: dict[str, str] = {}

    class Cursor:
        result: dict[str, str] | None = None
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: str, parameters: object = None) -> None:
            normalized = " ".join(statement.split())
            values = tuple(parameters or ())
            if normalized.startswith("SELECT payload FROM capability_tool_evidence_registry"):
                payload = rows.get(str(values[0]))
                self.result = None if payload is None else {"payload": payload}
            elif normalized.startswith("INSERT INTO capability_tool_evidence_registry"):
                rows[str(values[0])] = str(values[1])
                self.result = None

        def fetchone(self):
            return self.result

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def cursor(self):
            return Cursor()

    class Storage:
        def transaction(self):
            return Connection()

    class Store(GatewayStore):
        def __init__(self) -> None:
            self.storage = Storage()

        def _batch_row(self, cursor, batch_id, **kwargs):
            del cursor, kwargs
            return {"data": {"batch_id": batch_id}}

        def _runtime_block_by_json_value(self, cursor, **kwargs):
            del cursor, kwargs
            return {"data": {"status": "succeeded"}}

    refs = tuple(_ref(label.encode()) for label in ("call", "lease", "receipt"))
    store = Store()
    store.record_capability_tool_evidence(
        case_digest="d" * 64,
        tool_key="crm.lookup",
        trace_id=uuid4(),
        run_id=uuid4(),
        model_policy_digest="e" * 64,
        call_ref=refs[0],
        lease_ref=refs[1],
        receipt_ref=refs[2],
        batch_id="n8n-benchmark-batch",
        execution_result_id=uuid4(),
    )

    assert store.capability_tool_evidence(refs[2].sha256)["case_digest"] == "d" * 64


def test_gateway_tool_authority_issues_the_exact_registered_artifact_bytes(tmp_path) -> None:
    records: dict[str, object] = {}

    class Store:
        def record_capability_tool_evidence(self, **payload: object) -> None:
            records.update(payload)

        def capability_tool_evidence(self, receipt_sha256: str):
            assert receipt_sha256 == records["receipt_ref"].sha256
            return {
                "case_digest": records["case_digest"],
                "tool_key": records["tool_key"],
                "trace_id": str(records["trace_id"]),
                "run_id": str(records["run_id"]),
                "model_policy_digest": records["model_policy_digest"],
                "call_ref": records["call_ref"].model_dump(mode="json"),
                "lease_ref": records["lease_ref"].model_dump(mode="json"),
                "receipt_ref": records["receipt_ref"].model_dump(mode="json"),
                "batch_id": records["batch_id"],
                "execution_result_id": str(records["execution_result_id"]),
            }

    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    authority = CaptainBenchmarkToolEvidenceAuthority(
        gateway_store=Store(), artifacts=artifacts,
    )
    case = GenericPrivateCaseRef(
        case_id="case-a", category=GenericBenchmarkCategory.NORMAL,
        assertion_ids=("a",), case_digest="d" * 64,
    )
    trace_id, run_id = uuid4(), uuid4()

    issued = authority.issue(
        case_ref=case, tool_key="crm.lookup", operation="read",
        call_id="call-crm-lookup", lease_id="lease-crm-lookup",
        trace_id=trace_id, run_id=run_id, model_policy_digest="e" * 64,
        result_ref_sha256="c" * 64, batch_id="n8n-benchmark-batch",
        execution_result_id=uuid4(),
    )

    request = BenchmarkToolEvidenceVerificationRequest(
        case_ref=case, tool_key=issued.tool_key, call_ref=issued.call_ref,
        lease_ref=issued.lease_ref, receipt_ref=issued.receipt_ref,
        trace_id=trace_id, run_id=run_id, model_policy_digest="e" * 64,
    )
    assert authority.verify(request) is True
    for reference, schema in (
        (issued.call_ref, "captain.capability-benchmark-tool-call.v1"),
        (issued.lease_ref, "captain.capability-benchmark-tool-lease.v1"),
        (issued.receipt_ref, "captain.capability-benchmark-tool-receipt.v1"),
    ):
        body = artifacts.read_bytes(reference)
        assert hashlib.sha256(body).hexdigest() == reference.sha256
        assert json.loads(body)["schema"] == schema


class _Store:
    def __init__(self) -> None:
        self.storage = _SqlStorage()
        self.blocks: list[object] = []
        self.projections: dict[str, object] = {}

    def append(self, request: object, claim_token: None) -> dict[str, object]:
        assert claim_token is None
        self.blocks.append(request)
        data = getattr(request, "data")
        self.projections[data["batch_id"]] = SimpleNamespace(
            batch_id=data["batch_id"], status="pending"
        )
        return {"data": data}

    def batch_projection(self, batch_id: str) -> object:
        if batch_id not in self.projections:
            raise LookupError(batch_id)
        return self.projections[batch_id]

    def record_capability_n8n_work_batch(self, *, job: object, batch: object) -> str:
        batch_id = str(getattr(batch, "batch_id"))
        self.storage.rows[str(getattr(job, "job_id"))] = batch_id
        self.projections[batch_id] = SimpleNamespace(batch_id=batch_id, status="pending")
        return batch_id

    def capability_n8n_work_batch(self, job_id: UUID) -> str | None:
        return self.storage.rows.get(str(job_id))


class _SqlCursor:
    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows
        self.result: dict[str, str] | None = None

    def __enter__(self) -> "_SqlCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> None:
        normalized = " ".join(statement.split())
        values = tuple(parameters or ())
        if normalized.startswith("SELECT batch_id"):
            batch = self.rows.get(str(values[0]))
            self.result = None if batch is None else {"batch_id": batch}
        elif normalized.startswith("INSERT INTO capability_n8n_work_batches"):
            self.rows[str(values[0])] = str(values[1])
            self.result = None

    def fetchone(self) -> dict[str, str] | None:
        return self.result


class _SqlConnection:
    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows

    def __enter__(self) -> "_SqlConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _SqlCursor:
        return _SqlCursor(self.rows)


class _SqlStorage:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def transaction(self) -> _SqlConnection:
        return _SqlConnection(self.rows)


def test_n8n_authority_persists_and_reads_only_deterministic_job_batch() -> None:
    store = _Store()
    authority = GatewayCapabilityN8nWorkBatchAuthority(gateway_store=store)  # type: ignore[arg-type]
    job_id = UUID("12345678-1234-4234-8234-123456789abc")
    batch = SimpleNamespace(
        batch_id=f"n8n-{job_id.hex[:24]}",
        constraints=[f"factory-job-id:{job_id}"],
        target="n8n",
        runtime="n8n",
        capability_tags=["n8n-builder"],
        model_dump=lambda **_: {
            "batch_id": f"n8n-{job_id.hex[:24]}",
            "title": "n8n",
            "goal": "goal",
            "subtask_ids": ["n8n-workflow"],
            "target": "n8n",
            "runtime": "n8n",
            "capability_tags": ["n8n-builder"],
            "constraints": [f"factory-job-id:{job_id}"],
            "acceptance_criteria": [],
        },
    )
    job = SimpleNamespace(job_id=job_id)

    authority.persist_work_batch(job, batch)
    authority.register_n8n_work_batch(job, batch.batch_id)

    assert authority.active_n8n_work_batch_id(job_id) == batch.batch_id
    restarted = GatewayCapabilityN8nWorkBatchAuthority(gateway_store=store)  # type: ignore[arg-type]
    assert restarted.active_n8n_work_batch_id(job_id) == batch.batch_id


def test_private_holdout_store_uses_gateway_durable_exact_readback() -> None:
    bodies: dict[str, str] = {}
    gateway = SimpleNamespace(
        put_capability_private_holdouts=lambda entries: bodies.update(dict(entries)),
        capability_private_holdout=lambda uri: bodies[uri],
    )
    catalog = GatewayPrivateHoldoutStore(gateway_store=gateway)

    catalog.put_many((("holdout://holdout-aaaaaaaaaaaa", "one"), ("holdout://holdout-bbbbbbbbbbbb", "two")))

    restarted = GatewayPrivateHoldoutStore(gateway_store=gateway)
    assert restarted.get("holdout://holdout-aaaaaaaaaaaa") == "one"
    assert restarted.get("holdout://holdout-bbbbbbbbbbbb") == "two"


def test_projection_reads_exact_gateway_issued_generic_promotion_event() -> None:
    event = MinibookProjectionEvent.model_validate({
        "schema": "captain.minibook-projection.v2",
        "event_id": str(uuid4()), "correlation_id": str(uuid4()),
        "causation_id": str(uuid4()), "occurred_at": "2026-08-09T12:00:00Z",
        "producer": "captain-gateway", "subject_id": f"subject:{uuid4()}",
        "subject_version": 1, "event_type": "capability.promoted",
        "payload": {
            "view": "validation", "template_id": "factory_capability_ready_to_use",
            "status_id": "ready_to_use", "actor_role_id": "captain_gateway",
            "benchmark_disposition": "passed", "benchmark_reason_codes": [],
            "candidate_correctness_bps": 9000, "baseline_correctness_bps": 8000,
            "candidate_completion_bps": 9000, "baseline_completion_bps": 8000,
            "cost_ratio_bps": 9000, "latency_ratio_bps": 9000,
            "unsafe_tool_uses": 0, "mandatory_handoff_misses": 0,
            "benchmark_summary_digest": "sha256:" + "a" * 64,
        },
    })
    store = SimpleNamespace(
        generic_capability_promotion=lambda event_id: (
            event.model_dump(mode="json", by_alias=True)
            if event_id == event.event_id else None
        ),
        factory_promotion_source=lambda _event_id: (_ for _ in ()).throw(
            AssertionError("legacy promotion read")
        ),
    )
    authority = GatewayCapabilityProjectionAuthority(gateway_store=store)

    assert asyncio.run(authority.projection_event(event.event_id)) == event


def test_gateway_generic_promotion_event_binds_summary_before_feed() -> None:
    job_id, event_id, correlation_id = uuid4(), uuid4(), uuid4()
    job = AgentFactoryJobV3.model_construct(
        job_id=job_id, event_id=uuid4(), correlation_id=correlation_id,
        subject_version=2,
    )

    event = GatewayStore._generic_minibook_promotion_event(
        event_id=event_id, job=job,
        occurred_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        summary_sha256="c" * 64,
        aggregate={
            "team_quality_count": 14, "baseline_quality_count": 12,
            "team_cost_usd": "0.8", "baseline_cost_usd": "1.0",
            "team_latency_ms": 80, "baseline_latency_ms": 100,
            "unsafe_team_count": 0, "mandatory_handoff_misses": 0,
        },
    )

    assert event.event_id == event_id
    assert event.correlation_id == correlation_id
    assert event.payload.benchmark_summary_digest == "sha256:" + "c" * 64
    assert event.payload.cost_ratio_bps == 8000


def test_app_composes_injected_production_bundle_once_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.app as gateway_app
    import gateway.capability_assembly_api as capability_api

    calls: list[str] = []
    composition = SimpleNamespace()
    class Controller:
        def submit(self, request: object) -> object:
            raise AssertionError("health must not submit")

        def projection(self, assembly_id: object) -> object:
            raise AssertionError("health must not read a projection")

        async def advance(self, request: object) -> object:
            raise AssertionError("health must not advance")

        def final_evidence(self, assembly_id: object) -> object:
            raise AssertionError("health must not read final evidence")

    controller = Controller()
    def require_sealed(value: object) -> object:
        calls.append("attest")
        if value is not composition:
            raise RuntimeError("not the lifespan composition")
        return controller

    monkeypatch.setattr(
        capability_api, "require_production_capability_assembly", require_sealed,
    )
    monkeypatch.setattr(
        gateway_app, "build_production_capability_assembly_from_environment",
        lambda **_kwargs: calls.append("build") or composition,
    )
    async def close(_composition: object) -> None:
        calls.append("close")
    monkeypatch.setattr(gateway_app, "close_production_capability_assembly", close)
    monkeypatch.setattr(
        gateway_app, "load_gateway_settings", lambda _app: SimpleNamespace()
    )
    app = gateway_app.create_app(
        gateway_store=SimpleNamespace(),
        capability_assembly_environment={},
        capability_assembly_factory_runtime=SimpleNamespace(),
        capability_assembly_private_catalog=SimpleNamespace(),
        capability_assembly_repository_root=tmp_path,
    )
    app.dependency_overrides[gateway_app.require_captain] = lambda: None

    assert calls == []
    with TestClient(app) as client:
        response = client.get("/v1/capability-assemblies/health")
        assert response.status_code == 200
        assert response.json() == {
            "schema": "captain.capability-assembly-api-health.v1",
            "status": "ready",
            "composition": "injected",
        }
        assert calls == ["build", "attest"]
    assert calls == ["build", "attest", "close"]


def test_shipped_gateway_rejects_partial_capability_configuration_before_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.app as gateway_app

    effects: list[str] = []
    monkeypatch.setattr(
        gateway_app, "MariaDBStorage", lambda *_args: effects.append("storage")
    )
    monkeypatch.setattr(
        gateway_app, "GatewayStore", lambda *_args, **_kwargs: effects.append("store")
    )
    environment = {
        "LEDGER_DSN": "mysql://captain:secret@127.0.0.1:3306/captain_test",
        "CAPTAIN_GATEWAY_TOKEN": "captain-only-token",
        "WORKER_GATEWAY_TOKEN": "worker-only-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-only-token",
    }

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="configuration is incomplete",
    ):
        gateway_app.create_production_app_from_environment(
            environment=environment, repository_root=tmp_path,
        )

    assert effects == []


def test_shipped_gateway_rejects_factory_plan_drift_before_all_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.app as gateway_app

    effects: list[str] = []
    bootstrap = SimpleNamespace(
        artifact_root=tmp_path / "cas",
        authority_root=tmp_path / "authority",
        assembly=SimpleNamespace(expected_repository_revision="a" * 40),
    )
    monkeypatch.setattr(
        gateway_app,
        "load_capability_assembly_bootstrap_settings",
        lambda **_kwargs: bootstrap,
    )
    monkeypatch.setattr(
        gateway_app,
        "plan_capability_assembly_factory_runtime_from_environment",
        lambda **_kwargs: (_ for _ in ()).throw(
            CapabilityAssemblyBootstrapConfigurationError(
                "capability assembly Factory runtime plan is invalid"
            )
        ),
    )
    monkeypatch.setattr(
        gateway_app, "ContentAddressedArtifactAdapter",
        lambda *_args: effects.append("cas"),
    )
    monkeypatch.setattr(
        gateway_app, "MariaDBStorage", lambda *_args: effects.append("storage")
    )
    monkeypatch.setattr(
        gateway_app, "GatewayStore", lambda *_args, **_kwargs: effects.append("store")
    )
    environment = {
        "LEDGER_DSN": "mysql://captain:secret@127.0.0.1:3306/captain_test",
        "CAPTAIN_GATEWAY_TOKEN": "captain-only-token",
        "WORKER_GATEWAY_TOKEN": "worker-only-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-only-token",
    }

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="Factory runtime plan is invalid",
    ):
        gateway_app.create_production_app_from_environment(
            environment=environment,
            repository_root=tmp_path,
        )

    assert effects == []


def test_shipped_gateway_builds_exact_runtime_and_app_once_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.app as gateway_app

    calls: list[str] = []
    bootstrap = SimpleNamespace(
        artifact_root=tmp_path / "cas",
        provider_url="http://127.0.0.1:18091",
        provider_policy_sha256="1" * 64,
        provider_price_card_sha256="2" * 64,
        provider_context_sha256="3" * 64,
        provider_proxy_executable_sha256="4" * 64,
        provider_proxy_code_sha256="5" * 64,
        provider_paid_command_sha256="6" * 64,
        provider_hard_ceiling_enforced=True,
        provider_token=SecretStr("provider-only-token"),
    )
    factory_plan = SimpleNamespace()
    configured = gateway_app.GatewaySettings.from_env({
        "LEDGER_DSN": "mysql://captain:secret@127.0.0.1:3306/captain_test",
        "CAPTAIN_GATEWAY_TOKEN": "captain-only-token",
        "WORKER_GATEWAY_TOKEN": "worker-only-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-only-token",
    })
    monkeypatch.setattr(
        gateway_app, "plan_production_app_from_environment",
        lambda **_kwargs: calls.append("preflight") or gateway_app.GatewayProductionAppPlan(
            repository_root=tmp_path,
            environment={},
            settings=configured,
            bootstrap=bootstrap,
            factory_runtime=factory_plan,  # type: ignore[arg-type]
        ),
    )
    monkeypatch.setattr(
        gateway_app, "MariaDBStorage", lambda _dsn: calls.append("storage") or object()
    )
    store = SimpleNamespace()
    monkeypatch.setattr(
        gateway_app, "GatewayStore", lambda *_args, **_kwargs: calls.append("store") or store
    )
    class Artifacts:
        def put(self, content: bytes, media_type: str) -> object:
            del content, media_type
            raise AssertionError("materialization test must not write")

        def read_bytes(self, reference: object) -> bytes:
            del reference
            raise AssertionError("materialization test must not read")

    monkeypatch.setattr(
        gateway_app, "ContentAddressedArtifactAdapter",
        lambda _root: calls.append("artifacts") or Artifacts(),
    )
    runtime = SimpleNamespace()
    def runtime_builder(plan: object, **kwargs: object) -> object:
        calls.append("runtime")
        assert plan is factory_plan
        assert kwargs["store"] is store
        return runtime
    application = SimpleNamespace(state=SimpleNamespace())
    def app_builder(**kwargs: object) -> object:
        calls.append("app")
        assert kwargs["gateway_store"] is store
        assert kwargs["capability_assembly_factory_runtime"] is runtime
        application.state.gateway_settings = kwargs["settings"]
        return application
    monkeypatch.setattr(gateway_app, "create_app", app_builder)
    monkeypatch.setattr(
        gateway_app,
        "plan_production_capability_assembly_from_environment",
        lambda **_kwargs: calls.append("post-verify") or SimpleNamespace(),
    )
    environment = {
        "LEDGER_DSN": "mysql://captain:secret@127.0.0.1:3306/captain_test",
        "CAPTAIN_GATEWAY_TOKEN": "captain-only-token",
        "WORKER_GATEWAY_TOKEN": "worker-only-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-only-token",
    }

    result = gateway_app.create_production_app_from_environment(
        environment=environment,
        repository_root=tmp_path,
        runtime_materializer=runtime_builder,  # type: ignore[arg-type]
    )

    assert result is application
    assert calls == [
        "preflight", "artifacts", "storage", "store", "runtime",
        "post-verify", "app",
    ]


def test_gateway_module_main_serves_the_fully_built_production_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn
    import gateway.app as gateway_app

    application = SimpleNamespace(
        state=SimpleNamespace(gateway_settings=SimpleNamespace(host="127.0.0.1", port=8090))
    )
    observed: list[object] = []
    monkeypatch.setattr(
        gateway_app,
        "create_production_app_from_environment",
        lambda: observed.append("build") or application,
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: observed.append((app, kwargs)),
    )

    gateway_app.main()

    assert observed == [
        "build",
        (application, {"host": "127.0.0.1", "port": 8090, "workers": 1}),
    ]


def _factory_runtime_descriptor(*, adapter_sha256: str) -> dict[str, object]:
    return {
        "schema": "captain.capability-factory-runtime-plan.v1",
        "adapter_sha256": adapter_sha256,
        "adapter_ast_sha256": "0" * 64,
        "implementation_path": "gateway/capability_factory_runtime_internal.py",
        "implementation_sha256": "1" * 64,
        "implementation_ast_sha256": "2" * 64,
        "implementation_head_blob": "3" * 40,
        "constructor_signature": "(configuration, authorities)",
        "clock_signature": "() -> datetime",
        "model_client_signature": "(job, invocation)",
        "factory_type": "gateway.agent_factory_live_composition.AgentFactoryLiveComposition",
        "repository_type": "gateway.factory_repository.GatewayFactoryRepository",
        "team_execution_type": "gateway.agent_factory_live_composition.ProductionFactoryTeamExecutionPort",
        "ports_provider_type": "gateway.agent_factory_live_composition.GatewayTechnicalTeamExecutionPortsProvider",
        "model_client_type": "agenten.agent_factory.business_benchmark_production_ports.OpenAIBusinessBenchmarkModelClientBuilder",
        "budget_type": "gateway.factory_repository.GatewayFactoryBudgetLedger",
        "pricing_authority_type": "agenten.agent_factory.business_benchmark_production_ports.BusinessBenchmarkPricingAuthority",
        "pricing_source_type": "agenten.agent_factory.business_benchmark_production_ports.ConfiguredBusinessBenchmarkPricingSource",
        "replay_store_type": "agenten.agent_factory.hermes_cli.FilesystemFactorySkillReplayStore",
        "candidate_provider_type": "agenten.agent_factory.candidate_evaluation.GatewayForgeCandidateProvider",
        "skill_root": "agenten/agent_factory/skills",
        "replay_root": ".captain-cook/private/business-benchmarks/runtime-state/factory-team-replays",
        "store_authority": "gateway-sole-writer",
        "clock_authority": "shared-production-clock",
        "provider": "openai",
        "model": "gpt-5.6-terra",
    }


@pytest.mark.parametrize(
    "field,value",
    (
        ("constructor_signature", "()"),
        ("model_client_signature", "()"),
        ("clock_signature", "(now)"),
        ("ports_provider_type", "tests.FakePortsProvider"),
        ("pricing_authority_type", "tests.FakePricingAuthority"),
    ),
)
def test_factory_runtime_plan_rejects_signature_or_nested_authority_drift_purely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    import gateway.capability_assembly_bootstrap as bootstrap

    adapter = b"def build_capability_factory(configuration, authorities): pass\n"
    digest = hashlib.sha256(adapter).hexdigest()
    descriptor = _factory_runtime_descriptor(adapter_sha256=digest)
    descriptor[field] = value
    effects: list[str] = []
    descriptor_bytes = json.dumps(descriptor).encode()
    def read_verified(relative: str, **_kwargs):
        if relative == "gateway/capability_factory_runtime_descriptor.json":
            return tmp_path / "descriptor.json", descriptor_bytes
        return tmp_path / "adapter.py", adapter
    monkeypatch.setattr(bootstrap, "read_verified_repository_bytes", read_verified)
    monkeypatch.setattr(
        bootstrap,
        "load_verified_repository_module",
        lambda *_args, **_kwargs: effects.append("module") or SimpleNamespace(),
    )
    environment = {
        "CAPTAIN_CAPABILITY_FACTORY_ADAPTER": "gateway/capability_factory_runtime_adapter.py",
        "CAPTAIN_CAPABILITY_FACTORY_ADAPTER_SHA256": digest,
        "CAPTAIN_CAPABILITY_FACTORY_DESCRIPTOR": "gateway/capability_factory_runtime_descriptor.json",
        "CAPTAIN_CAPABILITY_FACTORY_PLAN_SHA256": hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "CAPTAIN_CAPABILITY_EXECUTION_POLICY_JSON": json.dumps({
            "schema": "captain.factory-execution-policy.v1",
            "mode": "release", "live_execution": True,
            "max_cost_usd": "1.00", "max_runtime_seconds": 900,
            "required_live_runs": 3, "allowed_models": ["gpt-5.6-terra"],
            "live_capabilities": ["model.invoke"],
        }),
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-5.6-terra",
    }

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="Factory runtime plan is invalid",
    ):
        plan_capability_assembly_factory_runtime_from_environment(
            repository_root=tmp_path,
            environment=environment,
            artifact_root=tmp_path / "artifacts",
            authority_root=tmp_path / "authority",
            repository_revision="a" * 40,
        )

    assert effects == []


@pytest.mark.parametrize(
    "source",
    (
        b"dangerous()\ndef build_capability_factory(configuration, authorities):\n return None\n",
        b"def build_capability_factory():\n return None\n",
        b"from gateway.capability_factory_runtime_internal import build_capability_factory_runtime as _build_capability_factory_runtime\n\ndef build_capability_factory(configuration, authorities):\n print('effect')\n return _build_capability_factory_runtime(configuration, authorities)\n",
    ),
)
def test_factory_adapter_ast_rejects_top_level_calls_or_signature_drift(
    source: bytes,
) -> None:
    import gateway.capability_assembly_bootstrap as bootstrap

    with pytest.raises(ValueError):
        bootstrap._attest_factory_adapter_ast(source)


def test_repository_factory_adapter_matches_committed_descriptor() -> None:
    import gateway.capability_assembly_bootstrap as bootstrap

    root = Path(__file__).resolve().parents[2]
    source = (root / "gateway/capability_factory_runtime_adapter.py").read_bytes()
    descriptor = bootstrap.CapabilityAssemblyFactoryRuntimeDescriptorV1.model_validate_json(
        (root / "gateway/capability_factory_runtime_descriptor.json").read_bytes()
    )

    assert hashlib.sha256(source).hexdigest() == descriptor.adapter_sha256
    assert bootstrap._attest_factory_adapter_ast(source) == descriptor.adapter_ast_sha256
    implementation = (root / descriptor.implementation_path).read_bytes()
    assert hashlib.sha256(implementation).hexdigest() == descriptor.implementation_sha256
    assert hashlib.sha256(
        ast.dump(
            ast.parse(implementation), annotate_fields=True, include_attributes=False
        ).encode("utf-8")
    ).hexdigest() == descriptor.implementation_ast_sha256
    assert b"capability_factory_runtime_internal" not in source


def test_factory_constructor_receives_only_allowlisted_config_and_redacts_any_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.capability_assembly_bootstrap as bootstrap

    adapter_sha256 = hashlib.sha256(b"adapter").hexdigest()
    descriptor = bootstrap.CapabilityAssemblyFactoryRuntimeDescriptorV1.model_validate(
        _factory_runtime_descriptor(adapter_sha256=adapter_sha256)
    )
    policy = bootstrap.FactoryExecutionPolicyV1.model_validate({
        "schema": "captain.factory-execution-policy.v1",
        "mode": "release", "live_execution": True,
        "max_cost_usd": "1.00", "max_runtime_seconds": 900,
        "required_live_runs": 3, "allowed_models": ["gpt-5.6-terra"],
        "live_capabilities": ["model.invoke"],
    })
    configuration = bootstrap.CapabilityAssemblyFactoryConfiguration(
        repository_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        authority_root=tmp_path / "authority",
        provider="openai",
        model="gpt-5.6-terra",
        execution_policy=policy,
    )
    plan = bootstrap.CapabilityAssemblyFactoryRuntimePlan(
        repository_root=tmp_path,
        adapter_path=tmp_path / "adapter.py",
        adapter_sha256=adapter_sha256,
        adapter_bytes=(
            b'"""verified"""\n\ndef build_capability_factory(configuration, authorities):\n'
            b'    return _verified_internal_builder(configuration, authorities)\n'
        ),
        implementation_path=tmp_path / "internal.py",
        implementation_bytes=(
            b"def build_capability_factory_runtime(configuration, authorities):\n"
            b"    raise RuntimeError('must-never-escape-constructor')\n"
        ),
        descriptor_sha256="d" * 64,
        descriptor=descriptor,
        configuration=configuration,
    )
    secret_marker = "must-never-escape-constructor"

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="Factory constructor is invalid",
    ) as captured:
        bootstrap.materialize_capability_assembly_factory_runtime(
            plan,
            store=SimpleNamespace(),  # type: ignore[arg-type]
            private_benchmark_catalog=SimpleNamespace(),  # type: ignore[arg-type]
            clock=lambda: datetime.now(timezone.utc),
        )

    assert not hasattr(configuration, "environment")
    assert not hasattr(configuration, "gateway_token")
    assert captured.value.__cause__ is None
    assert secret_marker not in str(captured.value)


def test_production_plan_is_pure_and_reuses_the_provisioner_source_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.capability_assembly_bootstrap as bootstrap

    artifact_root = tmp_path / "artifacts"
    raw_artifacts = ContentAddressedArtifactAdapter(artifact_root)
    source_artifacts = RepositoryFactoryInputArtifactStore(artifacts=raw_artifacts)
    store = SimpleNamespace(
        _runtime_provider_bundle_bindings=frozenset({
            (
                "http://127.0.0.1:18091", "c" * 64, "d" * 64,
                "4" * 64, "5" * 64, "6" * 64, "7" * 64, True,
            ),
        }),
        storage=SimpleNamespace(
            transaction=lambda: (_ for _ in ()).throw(
                AssertionError("plan touched a schema")
            ),
        ),
    )
    private_catalog = GatewayPrivateHoldoutStore(gateway_store=store)  # type: ignore[arg-type]
    runtime = SimpleNamespace(
        provisioning=SimpleNamespace(_source_artifacts=source_artifacts),
    )
    inputs = {
        "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md": "1" * 64,
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md": "2" * 64,
        "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md": "3" * 64,
    }
    environment = {
        "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION": "a" * 40,
        "CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON": json.dumps(inputs),
        "CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON": json.dumps(
            {digest: [] for digest in inputs.values()}
        ),
        "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION": "1",
        "CAPTAIN_PRIVATE_BENCHMARK_ROOT": str((tmp_path / "private").resolve()),
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_PATH": "catalog.json",
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_SHA256": "e" * 64,
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-plan-token",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT": "artifacts",
        "CAPTAIN_CAPABILITY_AUTHORITY_ROOT": "authority",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8088",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-plan-token",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-plan-key",
        "MINIBOOK_PROJECTION_API_KEY": "projection-plan-key",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-plan-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-plan-token",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
    }
    monkeypatch.setattr(bootstrap, "_native_runtime_is_bound", lambda **_: True)
    monkeypatch.setattr(
        bootstrap.httpx, "AsyncClient",
        lambda **_: (_ for _ in ()).throw(AssertionError("plan built an async client")),
    )
    monkeypatch.setattr(
        bootstrap.httpx, "Client",
        lambda **_: (_ for _ in ()).throw(AssertionError("plan built a sync client")),
    )
    monkeypatch.setattr(
        Path, "mkdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plan created a directory")
        ),
    )

    plan = bootstrap.plan_production_capability_assembly_from_environment(
        repository_root=tmp_path,
        environment=environment,
        store=store,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        private_benchmark_catalog=private_catalog,
    )

    assert plan.source_artifacts is source_artifacts
    assert not (tmp_path / "authority").exists()


def test_production_plan_rejects_deep_fake_team_ports_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.capability_assembly_bootstrap as bootstrap

    def bare(cls: type[object], **values: object) -> object:
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance

    store = SimpleNamespace(
        _runtime_provider_bundle_bindings=frozenset({
            (
                "http://127.0.0.1:18091", "c" * 64, "d" * 64,
                "4" * 64, "5" * 64, "6" * 64, "7" * 64, True,
            ),
        }),
        storage=SimpleNamespace(transaction=lambda: (_ for _ in ()).throw(
            AssertionError("plan touched schema")
        )),
    )
    repository = bare(bootstrap.GatewayFactoryRepository, _store=store)
    n8n = bootstrap.GatewayCapabilityN8nWorkBatchAuthority(
        gateway_store=store  # type: ignore[arg-type]
    )
    coordinator = bare(bootstrap.FactoryCoordinator, _repository=repository)
    lease = bare(
        bootstrap.GatewayNextActionLeaseIssuer,
        _store=store,
        _n8n_work_batch_source=n8n,
    )
    hermes = bare(bootstrap.HermesCliFactory, _workflow_artifacts=repository)
    clock = lambda: datetime(2026, 8, 17, tzinfo=timezone.utc)
    ports_for = bare(
        bootstrap.GatewayTechnicalTeamExecutionPortsProvider,
        _candidate_bindings=object(),
        _technical_holdout_root=tmp_path / "holdouts",
        _model_client_for=lambda _job, _invocation: object(),
        _budget=object(),
        _pricing_authority=object(),
        _replay_store=object(),
        _released_skill_catalog=repository,
        _skill_root=tmp_path / "agenten" / "agent_factory" / "skills",
        _provider="openai",
        _model="gpt-5.6-terra",
        _max_cost_per_call=Decimal("0.10"),
        _clock=clock,
    )
    team = bare(
        bootstrap.ProductionFactoryTeamExecutionPort,
        _ports_for=ports_for,
    )
    candidate = bare(
        bootstrap.CandidateEvaluationFactory,
        _workflow_artifacts=repository,
    )
    benchmark = bare(bootstrap.BusinessBenchmarkDispatchService)
    dispatcher = bare(
        bootstrap.FactoryDispatcher,
        _coordinator=coordinator,
        _business_benchmark=benchmark,
    )
    runner = bare(
        bootstrap.ProductionFactoryDispatchRunner,
        _coordinator=coordinator,
        _dispatcher=dispatcher,
    )
    factory = bootstrap.AgentFactoryLiveComposition(
        repository=repository,  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        lease_issuer=lease,  # type: ignore[arg-type]
        hermes=hermes,  # type: ignore[arg-type]
        team_execution=team,  # type: ignore[arg-type]
        candidate_validator=candidate,  # type: ignore[arg-type]
        business_benchmark=benchmark,  # type: ignore[arg-type]
        dispatcher=dispatcher,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        improvement_authority=object(),  # type: ignore[arg-type]
        runtime_retry_authority=None,
    )
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "artifacts")
    source = RepositoryFactoryInputArtifactStore(artifacts=artifacts)
    private = bootstrap.GatewayPrivateHoldoutStore(
        gateway_store=store  # type: ignore[arg-type]
    )
    provisioner = bare(
        bootstrap.CurrentAuthorityCapabilityFactoryProvisioner,
        _repository=repository,
        _source_artifacts=source,
        _work_batches=n8n,
        _n8n_registry=n8n,
        _compiler=SimpleNamespace(_holdout_store=private),
    )
    runtime = bootstrap.CapabilityAssemblyFactoryRuntime(
        factory=factory,
        provisioning=provisioner,  # type: ignore[arg-type]
        n8n=n8n,
    )
    inputs = {
        "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md": "1" * 64,
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md": "2" * 64,
        "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md": "3" * 64,
    }
    environment = {
        "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION": "a" * 40,
        "CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON": json.dumps(inputs),
        "CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON": json.dumps(
            {digest: [] for digest in inputs.values()}
        ),
        "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION": "1",
        "CAPTAIN_PRIVATE_BENCHMARK_ROOT": str((tmp_path / "private").resolve()),
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_PATH": "catalog.json",
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_SHA256": "e" * 64,
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-plan-token",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT": "artifacts",
        "CAPTAIN_CAPABILITY_AUTHORITY_ROOT": "authority",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8088",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-plan-token",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-plan-key",
        "MINIBOOK_PROJECTION_API_KEY": "projection-plan-key",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-plan-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-plan-token",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
    }
    monkeypatch.setattr(Path, "mkdir", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("plan created directory")
    ))
    monkeypatch.setattr(bootstrap.httpx, "Client", lambda **_k: (_ for _ in ()).throw(
        AssertionError("plan created client")
    ))

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="not Gateway-native",
    ):
        bootstrap.plan_production_capability_assembly_from_environment(
            repository_root=tmp_path,
            environment=environment,
            store=store,  # type: ignore[arg-type]
            runtime=runtime,
            private_benchmark_catalog=private,
            clock=clock,
        )


def test_bootstrap_rejects_raw_factory_runtime_before_schema_or_network() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = (
        "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
        "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
    )
    digests = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in paths
    }
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = {
        "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION": revision,
        "CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON": json.dumps(digests),
        "CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON": json.dumps(
            {digest: [] for digest in digests.values()}
        ),
        "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION": "1",
        "CAPTAIN_PRIVATE_BENCHMARK_ROOT": "C:/captain-private/private-benchmarks",
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_PATH": "catalog.json",
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_SHA256": "e" * 64,
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-secret-value",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT": ".captain-cook/artifacts/sha256",
        "CAPTAIN_CAPABILITY_AUTHORITY_ROOT": ".captain-cook/capability-authority",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8088",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret-value",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-secret-value",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret-value",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret-value",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-secret-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
    }
    touched_store = SimpleNamespace(storage=SimpleNamespace(transaction=lambda: (_ for _ in ()).throw(AssertionError("schema touched"))))

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="not Gateway-native",
    ):
        build_production_capability_assembly_from_environment(
            repository_root=root,
            environment=environment,
            store=touched_store,  # type: ignore[arg-type]
            runtime=SimpleNamespace(factory=object(), provisioning=object(), n8n=object()),  # type: ignore[arg-type]
            private_benchmark_catalog=object(),  # type: ignore[arg-type]
        )


class _BenchmarkCursor:
    def __init__(self, rows: dict[tuple[str, str], dict[str, str]]) -> None:
        self.rows = rows
        self.result: dict[str, str] | None = None

    def __enter__(self) -> "_BenchmarkCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> None:
        normalized = " ".join(statement.split())
        values = tuple(parameters or ())
        if normalized.startswith("SELECT payload, summary_sha256"):
            self.result = self.rows.get((str(values[0]), str(values[1])))
        elif normalized.startswith("INSERT INTO capability_generic_benchmarks"):
            self.rows[(str(values[0]), str(values[1]))] = {
                "payload": str(values[4]),
                "summary_sha256": str(values[3]),
            }

    def fetchone(self) -> dict[str, str] | None:
        return self.result


class _BenchmarkConnection:
    def __init__(self, rows: dict[tuple[str, str], dict[str, str]]) -> None:
        self.rows = rows

    def __enter__(self) -> "_BenchmarkConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _BenchmarkCursor:
        return _BenchmarkCursor(self.rows)


class _BenchmarkStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, str]] = {}

    def transaction(self) -> _BenchmarkConnection:
        return _BenchmarkConnection(self.rows)


def test_generic_benchmark_recorder_is_append_only_and_replay_stable(
    tmp_path: Path,
) -> None:
    evaluation_ref = _ref(b"evaluation")
    categories = tuple(GenericBenchmarkCategory)
    cases = tuple(
        GenericBenchmarkCaseReceipt(
            case_id=f"case-{index}",
            category=categories[index // 3],
            case_digest=f"{index + 1:064x}",
            team_trace_id=uuid4(), team_run_id=uuid4(),
            baseline_trace_id=uuid4(), baseline_run_id=uuid4(),
            team_evaluation_ref=evaluation_ref,
            baseline_evaluation_ref=evaluation_ref,
        )
        for index in range(15)
    )
    job_id, correlation_id = uuid4(), uuid4()
    input_ref = ArtifactRef(
        uri="artifact://sha256/" + "f" * 64,
        sha256="f" * 64,
        media_type="text/markdown",
    )
    job = AgentFactoryJobV3.model_construct(
        job_id=job_id, correlation_id=correlation_id, input_ref=input_ref
    )
    store = SimpleNamespace(
        storage=_BenchmarkStorage(),
        factory_job=lambda _: SimpleNamespace(job=job),
    )
    recorder = GatewayGenericBenchmarkRecorder(
        gateway_store=store,  # type: ignore[arg-type]
        artifacts=ContentAddressedArtifactAdapter(tmp_path / "cas"),
    )
    request = GenericBenchmarkRecordingRequest(
        job_id=job_id,
        correlation_id=correlation_id,
        suite_ref=_ref(b"suite"),
        suite_digest=hashlib.sha256(b"suite").hexdigest(),
        aggregate=GenericBenchmarkAggregate(
            case_count=15,
            team_quality_count=15,
            baseline_quality_count=14,
            unsafe_team_count=0,
            mandatory_handoff_misses=0,
            team_cost_usd=Decimal("0.10"),
            baseline_cost_usd=Decimal("0.05"),
            team_latency_ms=10,
            baseline_latency_ms=12,
        ),
        case_receipts=cases,
        passed=True,
    )

    first = recorder.record_generic_benchmark(request)
    replay = recorder.record_generic_benchmark(request)

    assert replay == first
    changed = GenericBenchmarkRecordingRequest(
        **{
            **request.__dict__,
            "aggregate": GenericBenchmarkAggregate(
                **{**request.aggregate.__dict__, "team_latency_ms": 9}
            ),
        }
    )
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="conflicts"):
        recorder.record_generic_benchmark(changed)


def test_generic_benchmark_recorder_rejects_incomplete_suite_before_cas_write(
    tmp_path: Path,
) -> None:
    store = SimpleNamespace(storage=_BenchmarkStorage())
    recorder = GatewayGenericBenchmarkRecorder(
        gateway_store=store,  # type: ignore[arg-type]
        artifacts=ContentAddressedArtifactAdapter(tmp_path / "cas"),
    )
    evaluation_ref = _ref(b"evaluation")
    request = GenericBenchmarkRecordingRequest(
        job_id=uuid4(),
        correlation_id=uuid4(),
        suite_ref=_ref(b"suite"),
        suite_digest=hashlib.sha256(b"suite").hexdigest(),
        aggregate=GenericBenchmarkAggregate(
            case_count=1, team_quality_count=1, baseline_quality_count=0,
            unsafe_team_count=0, mandatory_handoff_misses=0,
            team_cost_usd=Decimal("0.10"), baseline_cost_usd=Decimal("0.05"),
            team_latency_ms=10, baseline_latency_ms=12,
        ),
        case_receipts=(GenericBenchmarkCaseReceipt(
            case_id="case-a", category=GenericBenchmarkCategory.NORMAL,
            case_digest="a" * 64, team_trace_id=uuid4(), team_run_id=uuid4(),
            baseline_trace_id=uuid4(), baseline_run_id=uuid4(),
            team_evaluation_ref=evaluation_ref,
            baseline_evaluation_ref=evaluation_ref,
        ),),
        passed=True,
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="binding is invalid"):
        recorder.record_generic_benchmark(request)

    assert tuple((tmp_path / "cas").rglob("*")) == ()


def test_recorder_constructor_has_no_schema_effect(tmp_path: Path) -> None:
    calls: list[str] = []
    store = SimpleNamespace(
        storage=SimpleNamespace(
            transaction=lambda: calls.append("schema")
        )
    )

    GatewayGenericBenchmarkRecorder(
        gateway_store=store,  # type: ignore[arg-type]
        artifacts=ContentAddressedArtifactAdapter(tmp_path / "cas"),
    )

    assert calls == []


def test_bootstrap_rejects_reused_credentials_before_runtime_or_schema(
    tmp_path: Path,
) -> None:
    environment = {
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "same-secret-value",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT": ".captain-cook/artifacts/sha256",
        "CAPTAIN_CAPABILITY_AUTHORITY_ROOT": ".captain-cook/capability-authority",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8088",
        "CAPTAIN_GATEWAY_TOKEN": "same-secret-value",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-secret-value",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret-value",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret-value",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-secret-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
    }

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="credentials must be distinct",
    ):
        load_capability_assembly_bootstrap_settings(
            environment=environment,
            repository_root=tmp_path,
        )
