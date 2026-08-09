from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.capability_assembly_bootstrap import (
    CapabilityAssemblyBootstrapConfigurationError,
    CaptainBenchmarkToolEvidenceAuthority,
    GatewayCapabilityN8nWorkBatchAuthority,
    build_production_capability_assembly_from_environment,
    load_capability_assembly_bootstrap_settings,
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
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-secret-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
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
    call_payload = json.dumps(
        {
            "schema": "captain.capability-tool-call.v1",
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "model_policy_digest": policy,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    lease_payload = json.dumps(
        {
            "schema": "captain.capability-tool-lease.v1",
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "call_sha256": hashlib.sha256(call_payload).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    receipt_payload = json.dumps(
        {
            "schema": "captain.capability-tool-receipt.v1",
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "call_sha256": hashlib.sha256(call_payload).hexdigest(),
            "lease_sha256": hashlib.sha256(lease_payload).hexdigest(),
            "status": "succeeded",
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
            case_digest="b" * 64,
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
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-secret-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
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
    store = SimpleNamespace(storage=_BenchmarkStorage())
    recorder = GatewayGenericBenchmarkRecorder(
        gateway_store=store,  # type: ignore[arg-type]
        artifacts=ContentAddressedArtifactAdapter(tmp_path / "cas"),
    )
    evaluation_ref = _ref(b"evaluation")
    case = GenericBenchmarkCaseReceipt(
        case_id="case-a",
        category=GenericBenchmarkCategory.NORMAL,
        case_digest="a" * 64,
        team_trace_id=uuid4(),
        team_run_id=uuid4(),
        baseline_trace_id=uuid4(),
        baseline_run_id=uuid4(),
        team_evaluation_ref=evaluation_ref,
        baseline_evaluation_ref=evaluation_ref,
    )
    request = GenericBenchmarkRecordingRequest(
        job_id=uuid4(),
        correlation_id=uuid4(),
        suite_ref=_ref(b"suite"),
        suite_digest=hashlib.sha256(b"suite").hexdigest(),
        aggregate=GenericBenchmarkAggregate(
            case_count=1,
            team_quality_count=1,
            baseline_quality_count=0,
            unsafe_team_count=0,
            mandatory_handoff_misses=0,
            team_cost_usd=Decimal("0.10"),
            baseline_cost_usd=Decimal("0.05"),
            team_latency_ms=10,
            baseline_latency_ms=12,
        ),
        case_receipts=(case,),
        passed=True,
    )

    first = recorder.record_generic_benchmark(request)
    replay = recorder.record_generic_benchmark(request)

    assert replay == first
    changed = GenericBenchmarkRecordingRequest(
        **{**request.__dict__, "passed": False}
    )
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="conflicts"):
        recorder.record_generic_benchmark(changed)


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
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-secret-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "d" * 64,
    }

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="credentials must be distinct",
    ):
        load_capability_assembly_bootstrap_settings(
            environment=environment,
            repository_root=tmp_path,
        )
