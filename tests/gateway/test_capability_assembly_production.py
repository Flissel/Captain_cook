from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest
from pydantic import SecretStr
import gateway.capability_assembly_production as production_composition_module

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveReservation,
    CapabilityLiveStage,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_contracts import (
    BenchmarkExpectedPredicate,
    PrivateBenchmarkCaseRequirement,
)
from agenten.agent_factory.input_document import FactoryInputError, parse_factory_input_bytes
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionEvent,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projector import MinibookProjector, ProjectionResult
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_assembly_admission import (
    CapabilityAssemblyAdmissionError,
    RepositoryCapabilityAssemblyInputAdmission,
)
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyInputRequest,
    CapabilityAssemblyStageRequest,
)
from gateway.capability_assembly_controller import CapabilityAssemblyController
from gateway.capability_assembly_production import (
    AsyncMinibookProjectorAdapter,
    CapabilityAssemblyProductionAuthorities,
    CapabilityAssemblyCurrentFactoryAdapter,
    CapabilityAssemblyProductionConfigurationError,
    CapabilityAssemblyProductionSettings,
    GatewayMinibookProjectionEffectsAdapter,
    ProductionCapabilityAssemblyComposition,
    RepositoryFactoryInputArtifactStore,
    _compose_nonproduction_capability_assembly_controller,
    build_capability_assembly_controller,
    require_production_capability_assembly,
    _release_projection_digest,
)
from gateway.capability_generic_benchmark import (
    BenchmarkToolEvidenceVerificationRequest,
    GenericBenchmarkCategory,
    GenericBenchmarkExecutionEvidence,
    GenericEvaluationCaseView,
    GenericPrivateCaseDefinition,
    GenericBenchmarkReceipt,
    GenericCapabilityBenchmarkAdapter,
    ContentAddressedBenchmarkEvidenceResolver,
    IndependentBenchmarkEvaluationRequest,
    GenericPrivateBenchmarkRepository,
    GenericToolCallEvidence,
    RuleBasedCapabilityBenchmarkEvaluator,
    build_generic_benchmark_suite,
)
from gateway.capability_assembly_repository import GatewayCapabilityAssemblyRepository
from gateway.capability_benchmark_process_executor import (
    GatewayHttpBenchmarkProcessTransport,
    GatewayIsolatedBenchmarkExecutorClient,
)
from gateway.capability_live_current_adapters import CapabilityProjectionReadback
from gateway.capability_live_factory_adapter import CurrentCapabilityFactoryAdapter
from gateway.capability_live_pipeline import (
    CapabilityFactoryAuthority,
    CapabilityLiveAuthorityUnavailable,
)
from gateway.capability_live_runtime_resume import GatewayCapabilityResumeExecutionAdapter
from gateway.store import GatewayStore


_NS = UUID("53ec72aa-2496-5fa9-ac4d-a4ccfb51b49b")


def _id(label: str) -> UUID:
    return uuid5(_NS, label)


def _ref(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://capability-assembly/{digest}",
        sha256=digest,
        media_type="application/json",
    )


class _ToolEvidenceAuthority:
    authority_id = _id("captain-tool-evidence-authority")

    def __init__(self, gateway_store: object) -> None:
        self.gateway_store = gateway_store
        self.requests: list[BenchmarkToolEvidenceVerificationRequest] = []
        self._issued: set[tuple[str, str, str]] = set()

    def issue(
        self, call_ref: ArtifactRef, lease_ref: ArtifactRef, receipt_ref: ArtifactRef,
    ) -> None:
        self._issued.add((call_ref.sha256, lease_ref.sha256, receipt_ref.sha256))

    def verify(self, request: BenchmarkToolEvidenceVerificationRequest) -> bool:
        self.requests.append(request)
        return (
            request.call_ref.sha256,
            request.lease_ref.sha256,
            request.receipt_ref.sha256,
        ) in self._issued


def _private_case_repository(
    holdouts: InMemoryPrivateHoldoutStore,
    *, executor_authority_id: UUID | None = None,
) -> GenericPrivateBenchmarkRepository:
    return GenericPrivateBenchmarkRepository(
        holdouts=holdouts,
        executor_authority_id=(
            executor_authority_id or _id("test-private-case-executor")
        ),
        evaluator_authority_id=RuleBasedCapabilityBenchmarkEvaluator.AUTHORITY_ID,
    )


def _event() -> MinibookProjectionEvent:
    return MinibookProjectionEvent.model_validate(
        {
            "schema": "captain.minibook-projection.v2",
            "event_id": str(_id("projection-event")),
            "correlation_id": str(_id("correlation")),
            "causation_id": str(_id("causation")),
            "occurred_at": "2026-08-09T12:00:00Z",
            "producer": "captain-gateway",
            "subject_id": "subject:12345678-1234-4234-8234-123456789abc",
            "subject_version": 1,
            "event_type": "capability.promoted",
            "payload": {
                "view": "validation",
                "template_id": "factory_capability_ready_to_use",
                "status_id": "ready_to_use",
                "actor_role_id": "captain_gateway",
            },
        }
    )


def _acknowledgement(event: MinibookProjectionEvent) -> MinibookProjectionAcknowledgementV1:
    rendered = MinibookProjector.render(event)
    post_id = "captain-projection-" + hashlib.sha256(
        str(event.event_id).encode()
    ).hexdigest()[:32]
    return MinibookProjectionAcknowledgementV1(
        acknowledgement_id=minibook_projection_acknowledgement_id(
            event.event_id,
            post_id=post_id,
            content_sha256=rendered.content_hash,
        ),
        projection_event_id=event.event_id,
        correlation_id=event.correlation_id,
        subject_id=event.subject_id,
        subject_version=event.subject_version,
        project_id=MinibookProjector.PROJECTION_PROJECT_ID,
        post_id=post_id,
        content_sha256=rendered.content_hash,
        acknowledged_at=datetime(2026, 8, 9, 12, 1, tzinfo=timezone.utc),
        outcome="mirrored",
    )


class _ProjectionGateway:
    def __init__(self, event: MinibookProjectionEvent) -> None:
        self.event = event
        self.acknowledgement: MinibookProjectionAcknowledgementV1 | None = None
        self.calls: list[str] = []
        self.gateway_store: object | None = None

    async def projection_event(self, event_id: UUID) -> MinibookProjectionEvent | None:
        self.calls.append("feed")
        return self.event if event_id == self.event.event_id else None

    async def capability_promotion_event(self, release: object) -> MinibookProjectionEvent:
        del release
        return self.event

    async def acknowledge_projection(
        self, acknowledgement: MinibookProjectionAcknowledgementV1
    ) -> None:
        self.calls.append("ack")
        self.acknowledgement = acknowledgement

    async def projection_readback(
        self, event_id: UUID
    ) -> CapabilityProjectionReadback | None:
        self.calls.append("readback")
        if event_id != self.event.event_id or self.acknowledgement is None:
            return None
        return CapabilityProjectionReadback(
            event=self.event,
            acknowledgement=self.acknowledgement,
            evidence_ref=_ref("gateway-registry-mirror"),
        )


class _MinibookProjection:
    def __init__(self, acknowledgement: MinibookProjectionAcknowledgementV1) -> None:
        self._acknowledgement = acknowledgement
        self.calls: list[str] = []

    async def project(self, event: MinibookProjectionEvent) -> ProjectionResult:
        self.calls.append("project")
        return ProjectionResult(
            event_id=str(event.event_id),
            outcome="projected",
            post_id=self._acknowledgement.post_id,
        )

    async def acknowledgement(
        self, event: MinibookProjectionEvent
    ) -> MinibookProjectionAcknowledgementV1:
        self.calls.append("minibook-readback")
        assert event.event_id == self._acknowledgement.projection_event_id
        return self._acknowledgement


def test_projection_effects_require_feed_project_ack_and_gateway_readback() -> None:
    event = _event()
    acknowledgement = _acknowledgement(event)
    gateway = _ProjectionGateway(event)
    minibook = _MinibookProjection(acknowledgement)
    effects = GatewayMinibookProjectionEffectsAdapter(
        gateway=gateway,
        minibook=minibook,
    )

    result = asyncio.run(effects.project_and_ack(event))

    assert result == CapabilityProjectionReadback(
        event=event,
        acknowledgement=acknowledgement,
        evidence_ref=_ref("gateway-registry-mirror"),
    )
    assert gateway.calls == ["feed", "ack", "readback"]
    assert minibook.calls == ["project", "minibook-readback"]


def test_projection_effects_fail_closed_when_gateway_feed_differs() -> None:
    event = _event()
    changed = event.model_copy(update={"subject_version": 2})
    gateway = _ProjectionGateway(changed)
    minibook = _MinibookProjection(_acknowledgement(event))
    effects = GatewayMinibookProjectionEffectsAdapter(
        gateway=gateway,
        minibook=minibook,
    )

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="authoritative Gateway feed",
    ):
        asyncio.run(effects.project_and_ack(event))

    assert gateway.calls == ["feed"]
    assert minibook.calls == []


class _ForgedReadbackGateway(_ProjectionGateway):
    async def projection_readback(
        self, event_id: UUID
    ) -> CapabilityProjectionReadback | None:
        self.calls.append("readback")
        assert event_id == self.event.event_id
        assert self.acknowledgement is not None
        return CapabilityProjectionReadback(
            event=self.event,
            acknowledgement=self.acknowledgement,
            evidence_ref=object(),  # type: ignore[arg-type]
        )


def test_projection_effects_reject_forged_gateway_readback_evidence() -> None:
    event = _event()
    acknowledgement = _acknowledgement(event)
    gateway = _ForgedReadbackGateway(event)
    effects = GatewayMinibookProjectionEffectsAdapter(
        gateway=gateway,
        minibook=_MinibookProjection(acknowledgement),
    )

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="read-back",
    ):
        asyncio.run(effects.project_and_ack(event))


class _OfficialProjector:
    def __init__(self, event: MinibookProjectionEvent) -> None:
        self.event = event
        self.thread_calls: list[str] = []

    def project(self, event: MinibookProjectionEvent) -> ProjectionResult:
        self.thread_calls.append("project")
        return ProjectionResult(
            event_id=str(event.event_id),
            outcome="duplicate",
            post_id=None,
        )

    def acknowledgement(
        self, event: MinibookProjectionEvent
    ) -> MinibookProjectionAcknowledgementV1:
        self.thread_calls.append("acknowledgement")
        return _acknowledgement(event)


def test_async_minibook_adapter_uses_official_projector_contract() -> None:
    event = _event()
    projector = _OfficialProjector(event)
    adapter = AsyncMinibookProjectorAdapter(projector=projector)

    projection = asyncio.run(adapter.project(event))
    acknowledgement = asyncio.run(adapter.acknowledgement(event))

    assert projection.outcome == "duplicate"
    assert acknowledgement.projection_event_id == event.event_id
    assert projector.thread_calls == ["project", "acknowledgement"]


def _settings_environment(root: Path) -> dict[str, str]:
    paths = (
        "inputs/a/TO_BE_BUILT.md",
        "inputs/b/TO_BE_BUILT.md",
        "inputs/c/TO_BE_BUILT.md",
    )
    digests = {path: hashlib.sha256(path.encode()).hexdigest() for path in paths}
    return {
        "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION": "a" * 40,
        "CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON": json.dumps(digests),
        "CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON": json.dumps(
            {digest: [] for digest in digests.values()}
        ),
        "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION": "1",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret-must-not-render",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret-must-not-render",
        "UNRELATED_ROOT": str(root),
    }


def test_production_settings_parse_exact_three_digest_bound_inputs_and_redact_repr(
    tmp_path: Path,
) -> None:
    settings = CapabilityAssemblyProductionSettings.from_environment(
        _settings_environment(tmp_path)
    )

    assert tuple(settings.expected_sha256_by_path) == (
        "inputs/a/TO_BE_BUILT.md",
        "inputs/b/TO_BE_BUILT.md",
        "inputs/c/TO_BE_BUILT.md",
    )
    assert settings.subject_version == 1
    rendered = repr(settings)
    assert "gateway-secret-must-not-render" not in rendered
    assert "projection-secret-must-not-render" not in rendered


class _CanonicalInputSink:
    def __init__(self) -> None:
        self.sources: list[bytes] = []

    def put_factory_input(self, source: bytes) -> ArtifactRef:
        self.sources.append(source)
        digest = hashlib.sha256(source).hexdigest()
        return ArtifactRef(
            uri=f"artifact://factory-input/{digest}",
            sha256=digest,
            media_type="text/markdown",
        )


def test_repository_admission_mirrors_the_same_verified_bytes_for_provisioning() -> None:
    root = Path(__file__).resolve().parents[2]
    path = "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md"
    source = (root / path).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    sink = _CanonicalInputSink()
    admission = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root,
        expected_repository_revision=revision,
        expected_sha256_by_path={path: digest},
        compiled_tool_gaps_by_source_sha256={digest: ()},
        holdout_store=InMemoryPrivateHoldoutStore(),
        subject_version=1,
        source_artifacts=sink,
    )
    assembly_id = _id("source-mirror-assembly")

    admitted = admission.admit(
        CapabilityAssemblyInputRequest(
            input_id="source-mirror",
            repository_path=path,
            correlation_id=_id("source-mirror-correlation"),
            reservation=CapabilityLiveReservation(
                reservation_id=_id("source-mirror-reservation"),
                run_id=assembly_id,
                input_id="source-mirror",
                maximum_cost_usd="1.00",
            ),
        )
    )

    assert sink.sources == [source]
    assert admitted.input.source_ref.sha256 == digest


class _Sha256Artifacts:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}

    def put(self, source: bytes, media_type: str) -> ArtifactRef:
        assert media_type == "text/markdown"
        digest = hashlib.sha256(source).hexdigest()
        self.content[digest] = source
        return ArtifactRef(
            uri=f"artifact://sha256/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        assert reference.uri == f"artifact://sha256/{reference.sha256}"
        return self.content[reference.sha256]


class _BenchmarkArtifacts:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}

    def put(self, source: bytes, media_type: str) -> ArtifactRef:
        assert media_type == "application/json"
        digest = hashlib.sha256(source).hexdigest()
        self.content[digest] = source
        return ArtifactRef(
            uri=f"artifact://sha256/{digest}", sha256=digest, media_type=media_type
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        return self.content[reference.sha256]


def test_factory_input_artifact_store_preserves_canonical_ref_over_sha256_cas() -> None:
    source = b"canonical factory input"
    underlying = _Sha256Artifacts()
    store = RepositoryFactoryInputArtifactStore(artifacts=underlying)

    reference = store.put_factory_input(source)

    assert reference.uri == f"artifact://factory-input/{reference.sha256}"
    assert store.read_bytes(reference) == source


class _Cursor:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> None:
        del parameters
        self._statements.append(" ".join(statement.split()))


class _Connection:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self._statements)


class _Storage:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def transaction(self) -> _Connection:
        return _Connection(self.statements)


class _GatewayStore(GatewayStore):
    def __init__(self) -> None:
        self.storage = _Storage()


class _Provisioning:
    def __init__(self, store: object | None = None) -> None:
        self._repository = SimpleNamespace(_store=store)
    def provision(self, request: object) -> object | None:
        del request
        return None

    def binding_for(self, request: object) -> object | None:
        del request
        return None


class _ResumeGateway:
    def __init__(self, store: object | None = None) -> None:
        self.gateway_store = store
    async def authorize_resume(self, request: object) -> object | None:
        del request
        return None

    async def dispatch_resume(self, authorization: object) -> object | None:
        del authorization
        return None

    async def readback_resume(self, authorization: object) -> object | None:
        del authorization
        return None


class _Reconciliation:
    def __init__(self, store: object | None = None) -> None:
        self.gateway_store = store
    async def reconcile(self, request: object) -> object | None:
        del request
        return None


class _InvalidResumeGateway(_ResumeGateway):
    def authorize_resume(self, request: object) -> object | None:
        raise AssertionError("invalid port must not run")


class _WideResumeGateway(_ResumeGateway):
    async def authorize_resume(
        self,
        request: object,
        optional_second_authority: object | None = None,
    ) -> object | None:
        del request, optional_second_authority
        return None


class _FactoryRunner:
    async def run(self, job_id: object, **kwargs: object) -> object:
        del job_id, kwargs
        return object()


class _FactoryCoordinator:
    def projection(self, job_id: object) -> object:
        del job_id
        return object()


class _FactoryRepository:
    def __init__(self, store: object | None = None) -> None:
        self._store = store
    def snapshot(self, job_id: object) -> object:
        del job_id
        return object()

    def workflow_artifacts(self, job_id: object) -> tuple[object, ...]:
        del job_id
        return ()

    def workflow_budget_projection(self, job_id: object) -> object:
        del job_id
        return object()

    def business_benchmark_summary_by_artifact(self, reference: object) -> object | None:
        del reference
        return None


def _factory_composition(store: object | None = None) -> AgentFactoryLiveComposition:
    return AgentFactoryLiveComposition(
        repository=_FactoryRepository(store),  # type: ignore[arg-type]
        coordinator=_FactoryCoordinator(),  # type: ignore[arg-type]
        lease_issuer=object(),  # type: ignore[arg-type]
        hermes=object(),  # type: ignore[arg-type]
        team_execution=object(),  # type: ignore[arg-type]
        candidate_validator=object(),  # type: ignore[arg-type]
        business_benchmark=object(),  # type: ignore[arg-type]
        dispatcher=object(),  # type: ignore[arg-type]
        runner=_FactoryRunner(),  # type: ignore[arg-type]
        improvement_authority=object(),  # type: ignore[arg-type]
        runtime_retry_authority=None,
    )


def _authorities(
    *,
    store: object,
    resume_gateway: object | None = None,
) -> CapabilityAssemblyProductionAuthorities:
    event = _event()
    projection = GatewayMinibookProjectionEffectsAdapter(
        gateway=(projection_gateway := _ProjectionGateway(event)),
        minibook=_MinibookProjection(_acknowledgement(event)),
    )
    source_artifacts = RepositoryFactoryInputArtifactStore(
        artifacts=_Sha256Artifacts()
    )
    projection_gateway.gateway_store = store
    class BenchmarkRecorder:
        gateway_store = store
        def record_generic_benchmark(self, request: object) -> object:
            raise AssertionError("not called by builder")
    class Promotion:
        gateway_store = store
        async def promote_generic_capability(self, request: object, binding: object) -> object:
            raise AssertionError("not called by builder")
    class N8nBatches:
        gateway_store = store
        def active_n8n_work_batch_id(self, job_id: object) -> str | None:
            return None
    selected_resume = resume_gateway or _ResumeGateway(store)
    setattr(selected_resume, "gateway_store", store)
    return CapabilityAssemblyProductionAuthorities(
        store=store,  # type: ignore[arg-type]
        factory=_factory_composition(store),
        provisioning=_Provisioning(store),
        resume_gateway=selected_resume,  # type: ignore[arg-type]
        reconciliation=_Reconciliation(store),
        minibook_effects=projection,
        source_artifacts=source_artifacts,
        benchmark_executor=GatewayIsolatedBenchmarkExecutorClient(
            gateway_store=store,
            authority_id=_id("builder-benchmark-executor"),
            endpoint="http://127.0.0.1:8765/v1/execute",
            transport=GatewayHttpBenchmarkProcessTransport(
                bearer_token=SecretStr("unit-test-runtime-token")
            ),
        ),
        benchmark_evidence=ContentAddressedBenchmarkEvidenceResolver(
            gateway_store=store, artifacts=_BenchmarkArtifacts()
        ),
        benchmark_tool_authority=_ToolEvidenceAuthority(store),
        benchmark_recorder=BenchmarkRecorder(),  # type: ignore[arg-type]
        promotion=Promotion(),  # type: ignore[arg-type]
        n8n_work_batches=N8nBatches(),  # type: ignore[arg-type]
    )


def test_production_builder_preflights_all_ports_before_repository_schema_effect(
    tmp_path: Path,
) -> None:
    store = _GatewayStore()
    settings = CapabilityAssemblyProductionSettings.from_environment(
        _settings_environment(tmp_path)
    )

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="resume_gateway.authorize_resume",
    ):
        build_capability_assembly_controller(
            repository_root=tmp_path,
            settings=settings,
            authorities=_authorities(
                store=store,
                resume_gateway=_InvalidResumeGateway(),
            ),
            holdout_store=InMemoryPrivateHoldoutStore(),
        )

    assert store.storage.statements == []


def test_production_builder_rejects_in_process_benchmark_executor_before_schema(
    tmp_path: Path,
) -> None:
    store = _GatewayStore()
    class InProcessExecutor:
        gateway_store = store
        authority_id = _id("unsafe-in-process-executor")
        async def execute(self, request: object) -> object:
            return request
    authorities = replace(
        _authorities(store=store),
        benchmark_executor=InProcessExecutor(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="immutable Gateway HTTP process client",
    ):
        build_capability_assembly_controller(
            repository_root=tmp_path,
            settings=CapabilityAssemblyProductionSettings.from_environment(
                _settings_environment(tmp_path)
            ),
            authorities=authorities,
            holdout_store=InMemoryPrivateHoldoutStore(),
        )
    assert store.storage.statements == []


def test_production_builder_rejects_fake_transport_hidden_in_exact_client(
    tmp_path: Path,
) -> None:
    store = _GatewayStore()
    class FakeTransport:
        async def post(self, endpoint: str, body: bytes) -> bytes:
            raise AssertionError((endpoint, body))
    wrapped_fake = GatewayIsolatedBenchmarkExecutorClient(
        gateway_store=store, authority_id=_id("wrapped-fake-executor"),
        endpoint="https://benchmark-runtime.invalid/v1/execute",
        transport=FakeTransport(),
        allowed_hosts=("benchmark-runtime.invalid",),
    )
    authorities = replace(
        _authorities(store=store), benchmark_executor=wrapped_fake,
    )

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="immutable Gateway HTTP process client",
    ):
        build_capability_assembly_controller(
            repository_root=tmp_path,
            settings=CapabilityAssemblyProductionSettings.from_environment(
                _settings_environment(tmp_path)
            ),
            authorities=authorities,
            holdout_store=InMemoryPrivateHoldoutStore(),
        )
    assert store.storage.statements == []


def test_production_builder_rejects_non_exact_port_signature_before_schema_effect(
    tmp_path: Path,
) -> None:
    store = _GatewayStore()
    settings = CapabilityAssemblyProductionSettings.from_environment(
        _settings_environment(tmp_path)
    )

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="resume_gateway.authorize_resume.*signature",
    ):
        build_capability_assembly_controller(
            repository_root=tmp_path,
            settings=settings,
            authorities=_authorities(
                store=store,
                resume_gateway=_WideResumeGateway(),
            ),
            holdout_store=InMemoryPrivateHoldoutStore(),
        )

    assert store.storage.statements == []


def test_current_factory_adapter_blocks_required_todo_before_promotion() -> None:
    class N8n:
        def active_n8n_work_batch_id(self, job_id: object) -> str | None:
            return None
    class Promotion:
        async def promote_generic_capability(self, request: object, binding: object) -> object:
            return object()
    adapter = CapabilityAssemblyCurrentFactoryAdapter(
        composition=_factory_composition(),
        provisioning=_Provisioning(),
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        private_cases=_private_case_repository(InMemoryPrivateHoldoutStore()),
        n8n_work_batches=N8n(),  # type: ignore[arg-type]
        promotion=Promotion(),  # type: ignore[arg-type]
    )
    request = SimpleNamespace(
        input=SimpleNamespace(required_tool_gaps=(object(),)),
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="TODO_TOOL"):
        asyncio.run(adapter.promote(request))  # type: ignore[arg-type]


class _CapturingProvisioning(_Provisioning):
    request: object | None = None

    def provision(self, request: object) -> object | None:
        self.request = request
        return None


@pytest.mark.parametrize(
    ("relative_path", "expected_intent"),
    (
        (
            "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
            "none",
        ),
        (
            "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
            "n8n",
        ),
    ),
)
def test_current_factory_bridge_derives_n8n_only_from_canonical_agent_requirements(
    relative_path: str,
    expected_intent: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / relative_path).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    input_id = f"intent-{expected_intent}"
    assembly_id = _id(f"assembly-{expected_intent}")
    reservation = CapabilityLiveReservation(
        reservation_id=_id(f"reservation-{expected_intent}"),
        run_id=assembly_id,
        input_id=input_id,
        maximum_cost_usd="1.00",
    )
    admitted = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root,
        expected_repository_revision=revision,
        expected_sha256_by_path={relative_path: digest},
        compiled_tool_gaps_by_source_sha256={digest: ()},
        holdout_store=InMemoryPrivateHoldoutStore(),
        subject_version=1,
    ).admit(
        CapabilityAssemblyInputRequest(
            input_id=input_id,
            repository_path=relative_path,
            correlation_id=_id(f"correlation-{expected_intent}"),
            reservation=reservation,
        )
    )
    request = CapabilityAssemblyStageRequest(
        run_id=assembly_id,
        input=admitted.input,
        correlation_id=_id(f"correlation-{expected_intent}"),
        reservation=reservation,
        stage=CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
        controlled_recovery_required=True,
    )
    class N8n:
        def active_n8n_work_batch_id(self, job_id: object) -> str | None:
            return None
    class Promotion:
        async def promote_generic_capability(self, request: object, binding: object) -> object:
            return object()
    provisioning = _CapturingProvisioning()
    adapter = CapabilityAssemblyCurrentFactoryAdapter(
        composition=_factory_composition(),
        provisioning=provisioning,
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        private_cases=_private_case_repository(InMemoryPrivateHoldoutStore()),
        n8n_work_batches=N8n(),  # type: ignore[arg-type]
        promotion=Promotion(),  # type: ignore[arg-type]
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable):
        asyncio.run(adapter.scaffold(request))

    assert provisioning.request.input.integration_intent == expected_intent


def test_production_builder_uses_gateway_repository_and_current_authority_adapters(
    tmp_path: Path,
) -> None:
    store = _GatewayStore()
    settings = CapabilityAssemblyProductionSettings.from_environment(
        _settings_environment(tmp_path)
    )
    authorities = _authorities(store=store)

    composition = build_capability_assembly_controller(
        repository_root=tmp_path,
        settings=settings,
        authorities=authorities,
        holdout_store=InMemoryPrivateHoldoutStore(),
    )

    assert type(composition) is ProductionCapabilityAssemblyComposition
    controller = require_production_capability_assembly(composition)
    assert type(controller) is CapabilityAssemblyController
    assert isinstance(controller._repository, GatewayCapabilityAssemblyRepository)
    assert isinstance(controller._factory, CurrentCapabilityFactoryAdapter)
    assert isinstance(controller._resume, GatewayCapabilityResumeExecutionAdapter)
    assert len(store.storage.statements) == 4
    with pytest.raises(AttributeError, match="immutable"):
        authorities.benchmark_executor._transport = object()  # type: ignore[attr-defined]
    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError, match="attestation"
    ):
        require_production_capability_assembly(controller)
    with pytest.raises(TypeError, match="sealed"):
        ProductionCapabilityAssemblyComposition(
            controller=controller, attestation_digest="a" * 64,
            executor_client=authorities.benchmark_executor,
            executor_configuration_digest=(
                authorities.benchmark_executor.sealed_configuration_digest()
            ),
            _seal=object(),
        )
    forged = ProductionCapabilityAssemblyComposition(
        controller=controller, attestation_digest="f" * 64,
        executor_client=authorities.benchmark_executor,
        executor_configuration_digest=(
            authorities.benchmark_executor.sealed_configuration_digest()
        ),
        _seal=production_composition_module._PRODUCTION_COMPOSITION_SEAL,
    )
    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError, match="attestation"
    ):
        require_production_capability_assembly(forged)
    object.__setattr__(
        authorities.benchmark_executor,
        "_endpoint", "http://127.0.0.1:8765/v1/substituted",
    )
    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError, match="attestation"
    ):
        require_production_capability_assembly(composition)


def test_low_level_composition_rejects_missing_authority_before_using_repository() -> None:
    class ExplodingRepository:
        def history(self, assembly_id: object) -> tuple[object, ...]:
            raise AssertionError("repository must not be read during composition")

        def find_by_idempotency_key(self, idempotency_key: object) -> None:
            del idempotency_key

        def append(self, checkpoint: object, *, expected_revision: int) -> object:
            del expected_revision
            return checkpoint

        def consume_retry_authorization_and_append(
            self,
            checkpoint: object,
            authorization: object,
            *,
            expected_revision: int,
        ) -> object:
            del authorization, expected_revision
            return checkpoint

    class Admission:
        def admit(self, request: object) -> object:
            return request

    class BenchmarkExecutor:
        authority_id = _id("low-level-executor")
        async def execute(self, request: object) -> object:
            return request

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="reconciliation",
    ):
        _compose_nonproduction_capability_assembly_controller(
            repository=ExplodingRepository(),  # type: ignore[arg-type]
            admission=Admission(),  # type: ignore[arg-type]
            factory_composition=_factory_composition(),
            provisioning=_Provisioning(),
            resume_gateway=_ResumeGateway(),
            reconciliation=None,  # type: ignore[arg-type]
            minibook_effects=GatewayMinibookProjectionEffectsAdapter(
                gateway=_ProjectionGateway(_event()),
                minibook=_MinibookProjection(_acknowledgement(_event())),
            ),
            benchmark_executor=BenchmarkExecutor(),  # type: ignore[arg-type]
            benchmark_evidence=ContentAddressedBenchmarkEvidenceResolver(
                gateway_store=object(), artifacts=_BenchmarkArtifacts()
            ),
            benchmark_tool_authority=_ToolEvidenceAuthority(object()),
            benchmark_recorder=None,  # type: ignore[arg-type]
            promotion=None,  # type: ignore[arg-type]
            n8n_work_batches=None,  # type: ignore[arg-type]
            holdout_store=InMemoryPrivateHoldoutStore(),  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        )


def _admitted_demo(relative_path: str, holdouts: InMemoryPrivateHoldoutStore):
    root = Path(__file__).resolve().parents[2]
    source = (root / relative_path).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    input_id = "review-input"
    run_id = _id("review-run")
    reservation = CapabilityLiveReservation(
        reservation_id=_id("review-reservation"), run_id=run_id,
        input_id=input_id, maximum_cost_usd="1.00",
    )
    admitted = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root,
        expected_repository_revision=revision,
        expected_sha256_by_path={relative_path: digest},
        compiled_tool_gaps_by_source_sha256={digest: ()},
        holdout_store=holdouts,
        subject_version=1,
    ).admit(CapabilityAssemblyInputRequest(
        input_id=input_id, repository_path=relative_path,
        correlation_id=_id("review-correlation"), reservation=reservation,
    ))
    return admitted, reservation


def _private_benchmark_section(*, duplicate_semantic_case: bool = False) -> str:
    expectations = {
        "normal": ("observation_equals", "status", "ok"),
        "boundary": ("observation_contains", "detail", "bounded"),
        "missing": ("observation_absent", "required_value", True),
        "conflicting": ("observation_equals", "resolution", "signed_source"),
        "mandatory_escalation": ("handoff_event", "reason_code", "mandatory_stop"),
    }
    sources = ("app_regression", "database_pressure", "auth_uncertainty")
    lines = ["", "## Private benchmark cases", ""]
    for category, (kind, observed_key, expected) in expectations.items():
        for index, source in enumerate(sources):
            key = f"explicit_{category}_{index}"
            semantic_index = (
                0 if duplicate_semantic_case and category == "normal" and index == 1
                else index
            )
            semantic_source = (
                sources[0]
                if duplicate_semantic_case and category == "normal" and index == 1
                else source
            )
            lines.extend((
                f"### Benchmark case: {key}",
                "#### Category", category,
                "#### Setup", f"Source-grounded {category} setup {semantic_index}",
                "#### Action", f"Exercise {category} behavior {semantic_index}",
                "#### Source cases", f"- {semantic_source}",
                "#### Expected predicates",
                f"- {key}_primary | {kind} | {observed_key} | {json.dumps(expected)}",
                *(() if kind.startswith("observation_") else (
                    f"- {key}_business | observation_equals | status | \"ok\"",
                )),
                f"- {key}_tool | tool_receipt | captain_tool | true",
                f"- {key}_rationale | rationale_contains | rationale | \"verified\"",
                f"- {key}_safety | forbidden_tool_absent | forbidden_tool | true",
                "",
            ))
    return "\n".join(lines)


def _admitted_explicit_benchmark(tmp_path: Path, holdouts: InMemoryPrivateHoldoutStore):
    root = tmp_path / "benchmark-repository"
    relative_path = "inputs/incident/TO_BE_BUILT.md"
    target = root / relative_path
    target.parent.mkdir(parents=True)
    base = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md"
    ).read_text(encoding="utf-8")
    source = (base.rstrip() + _private_benchmark_section() + "\n").encode()
    target.write_bytes(source)
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "Captain Test"),
        ("git", "config", "user.email", "captain@example.invalid"),
        ("git", "add", relative_path),
        ("git", "commit", "-q", "-m", "test: add canonical input"),
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    digest = hashlib.sha256(source).hexdigest()
    reservation = CapabilityLiveReservation(
        reservation_id=_id("parsed-review-reservation"),
        run_id=_id("parsed-review-run"), input_id="parsed-review-input",
        maximum_cost_usd="1.00",
    )
    admitted = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root,
        expected_repository_revision=revision,
        expected_sha256_by_path={relative_path: digest},
        compiled_tool_gaps_by_source_sha256={digest: ()},
        holdout_store=holdouts, subject_version=1,
        require_private_benchmark_cases=True,
    ).admit(CapabilityAssemblyInputRequest(
        input_id=reservation.input_id, repository_path=relative_path,
        correlation_id=_id("parsed-review-correlation"), reservation=reservation,
    ))
    assert target.read_bytes() == source
    return admitted, reservation, source


def test_repository_admission_parses_exact_private_benchmark_section_without_mutation(
    tmp_path: Path,
) -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _, source = _admitted_explicit_benchmark(tmp_path, holdouts)

    cases = admitted.input.document.private_benchmark_cases
    assert len(cases) == 15
    assert {category: sum(case.category == category for case in cases) for category in (
        "normal", "boundary", "missing", "conflicting", "mandatory_escalation"
    )} == {
        "normal": 3, "boundary": 3, "missing": 3,
        "conflicting": 3, "mandatory_escalation": 3,
    }
    assert len(admitted.input.compiled_specification.private_holdout_refs) == 15
    assert admitted.input.source_ref.sha256 == hashlib.sha256(source).hexdigest()


def test_production_admission_rejects_missing_private_benchmark_section() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    root = Path(__file__).resolve().parents[2]
    relative_path = "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md"
    source = (root / relative_path).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    reservation = CapabilityLiveReservation(
        reservation_id=_id("missing-private-reservation"),
        run_id=_id("missing-private-run"), input_id="missing-private-input",
        maximum_cost_usd="1.00",
    )
    admission = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root, expected_repository_revision=revision,
        expected_sha256_by_path={relative_path: digest},
        compiled_tool_gaps_by_source_sha256={digest: ()},
        holdout_store=holdouts, subject_version=1,
        require_private_benchmark_cases=True,
    )

    with pytest.raises(CapabilityAssemblyAdmissionError, match="explicit Private benchmark"):
        admission.admit(CapabilityAssemblyInputRequest(
            input_id=reservation.input_id, repository_path=relative_path,
            correlation_id=_id("missing-private-correlation"), reservation=reservation,
        ))


def test_private_benchmark_markdown_rejects_malformed_case_count() -> None:
    base = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md"
    ).read_text(encoding="utf-8")
    malformed = (base.rstrip() + _private_benchmark_section()).replace(
        "### Benchmark case: explicit_normal_0",
        "### Invalid private case: explicit_normal_0",
    )

    with pytest.raises(FactoryInputError, match="three cases per category"):
        parse_factory_input_bytes(malformed.encode(), logical_name="TO_BE_BUILT.md")


def test_duplicate_semantic_private_cases_leave_no_resolvable_registration() -> None:
    repository = _private_case_repository(InMemoryPrivateHoldoutStore())
    predicate = BenchmarkExpectedPredicate(
        predicate_key="status", kind="observation_equals",
        observed_key="status", expected_value="ok",
    )
    first = GenericPrivateCaseDefinition(
        category=GenericBenchmarkCategory.NORMAL,
        assertion_ids=("assertion-a",), private_holdout_uri="holdout://first",
        setup="same setup", action="same action", expected_predicates=(predicate,),
    )
    duplicate = GenericPrivateCaseDefinition(
        category=GenericBenchmarkCategory.NORMAL,
        assertion_ids=("assertion-a",), private_holdout_uri="holdout://second",
        setup="same setup", action="same action", expected_predicates=(
            predicate.model_copy(update={"predicate_key": "renamed_status"}),
        ),
    )
    reference = repository._reference(first)

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="duplicated"):
        repository.register_many((first, duplicate))
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="stale"):
        repository.resolve(
            reference, purpose="execution",
            authority_id=_id("test-private-case-executor"),
        )


def test_duplicate_semantic_markdown_is_rejected_before_holdout_registration() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    base = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md"
    ).read_text(encoding="utf-8")
    source = base.rstrip() + _private_benchmark_section(duplicate_semantic_case=True)

    with pytest.raises(FactoryInputError, match="semantic definitions must be unique"):
        FactoryInputCompiler(holdout_store=holdouts).compile(
            parse_factory_input_bytes(source.encode(), logical_name="TO_BE_BUILT.md"),
            subject_version=1,
        )
    assert len(holdouts) == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("tool_only", "business outcome"),
        ("missing_business", "business outcome"),
        ("missing_rationale", "rationale"),
        ("missing_safety", "safety"),
        ("missing_tool", "required tool"),
        ("escalation_missing_handoff", "handoff"),
    ),
)
def test_incomplete_benchmark_dimensions_fail_before_private_mutation(
    mutation: str, expected: str,
) -> None:
    base = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md"
    ).read_text(encoding="utf-8")
    section = _private_benchmark_section()
    removable = {
        "missing_business": (
            '- explicit_normal_0_primary | observation_equals | status | "ok"',
        ),
        "missing_rationale": (
            '- explicit_normal_0_rationale | rationale_contains | rationale | "verified"',
        ),
        "missing_safety": (
            "- explicit_normal_0_safety | forbidden_tool_absent | forbidden_tool | true",
        ),
        "missing_tool": (
            "- explicit_normal_0_tool | tool_receipt | captain_tool | true",
        ),
        "escalation_missing_handoff": (
            "- explicit_mandatory_escalation_0_primary | handoff_event | "
            'reason_code | "mandatory_stop"',
        ),
        "tool_only": (
            '- explicit_normal_0_primary | observation_equals | status | "ok"',
            '- explicit_normal_0_rationale | rationale_contains | rationale | "verified"',
            "- explicit_normal_0_safety | forbidden_tool_absent | forbidden_tool | true",
        ),
    }
    for line in removable[mutation]:
        assert line in section
        section = section.replace(line + "\n", "", 1)
    source = (base.rstrip() + section).encode()
    holdouts = InMemoryPrivateHoldoutStore()
    private_cases = _private_case_repository(holdouts)

    with pytest.raises(FactoryInputError, match=expected):
        FactoryInputCompiler(holdout_store=holdouts).compile(
            parse_factory_input_bytes(source, logical_name="TO_BE_BUILT.md"),
            subject_version=1,
        )
    assert len(holdouts) == 0
    assert private_cases._definitions == {}


def test_generic_suite_uses_compiled_private_authority_and_all_five_categories() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable,
        match="fifteen explicit compiler holdouts",
    ):
        build_generic_benchmark_suite(
            admitted.input, _private_case_repository(holdouts)
        )


def test_internal_execution_issuance_is_revoked_by_suite_retirement() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )
    canonical = _with_grounded_benchmark_sources(admitted.input, holdouts)
    repository = _private_case_repository(holdouts)
    suite = build_generic_benchmark_suite(canonical, repository)
    nonce = _id("retired-execution-request")
    repository.issue_execution(
        suite.cases[0], authority_id=_id("test-private-case-executor"),
        request_nonce=nonce, request_digest="a" * 64,
    )

    repository.retire(suite)

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="stale"):
        repository.consume_execution(
            suite.cases[0], request_nonce=nonce, request_digest="a" * 64,
        )


def test_internal_execution_issuance_is_single_use() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )
    canonical = _with_grounded_benchmark_sources(admitted.input, holdouts)
    repository = _private_case_repository(holdouts)
    suite = build_generic_benchmark_suite(canonical, repository)
    nonce = _id("single-use-execution-request")
    repository.issue_execution(
        suite.cases[0], authority_id=_id("test-private-case-executor"),
        request_nonce=nonce, request_digest="e" * 64,
    )
    repository.consume_execution(
        suite.cases[0], request_nonce=nonce, request_digest="e" * 64,
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="stale"):
        repository.consume_execution(
            suite.cases[0], request_nonce=nonce, request_digest="e" * 64,
        )


def test_raw_execution_contract_contains_no_executor_authored_verdicts() -> None:
    fields = set(GenericBenchmarkExecutionEvidence.__dataclass_fields__)
    assert fields.isdisjoint({
        "assertion_results", "safe", "mandatory_handoff_performed",
        "rationale_present", "tool_valid", "outcome_satisfied",
    })


def _with_grounded_benchmark_sources(canonical, holdouts):
    document = canonical.document
    cases = []
    expectations = {
        "normal": ("observation_equals", "status", "ok"),
        "boundary": ("observation_contains", "detail", "bounded"),
        "missing": ("observation_absent", "required_value", True),
        "conflicting": ("observation_equals", "resolution", "signed_source"),
        "mandatory_escalation": ("handoff_event", "reason_code", "mandatory_stop"),
    }
    for category, (kind, observed_key, expected) in expectations.items():
        for index in range(3):
            predicates = [
                BenchmarkExpectedPredicate(
                    predicate_key=f"{category}_{index}_primary",
                    kind=kind,
                    observed_key=observed_key,
                    expected_value=expected,
                ),
                BenchmarkExpectedPredicate(
                    predicate_key=f"{category}_{index}_tool",
                    kind="tool_receipt",
                    observed_key="captain_tool",
                    expected_value=True,
                ),
                BenchmarkExpectedPredicate(
                    predicate_key=f"{category}_{index}_rationale",
                    kind="rationale_contains",
                    observed_key="rationale",
                    expected_value="verified",
                ),
                BenchmarkExpectedPredicate(
                    predicate_key=f"{category}_{index}_safety",
                    kind="forbidden_tool_absent",
                    observed_key="forbidden_tool",
                    expected_value=True,
                ),
            ]
            if not kind.startswith("observation_"):
                predicates.append(BenchmarkExpectedPredicate(
                    predicate_key=f"{category}_{index}_business",
                    kind="observation_equals", observed_key="status",
                    expected_value="ok",
                ))
            cases.append(PrivateBenchmarkCaseRequirement(
                case_key=f"explicit_{category}_{index}",
                category=category,
                observable_setup=f"Private source scenario {category} {index}",
                observable_action=f"Execute source-bound action {category} {index}",
                source_case_keys=(document.real_cases[index].case_key,),
                expected_predicates=tuple(predicates),
            ))
    grounded_document = document.model_copy(update={
        "private_benchmark_cases": tuple(cases),
    })
    compiled = FactoryInputCompiler(holdout_store=holdouts).compile(
        grounded_document, subject_version=canonical.compiled_specification.subject_version
    )
    return canonical.model_copy(update={
        "document": grounded_document,
        "compiled_specification": compiled,
    })


@pytest.mark.parametrize("mutation", ("missing_category", "duplicate_case"))
def test_explicit_private_categories_fail_closed_when_incomplete_or_ambiguous(
    mutation: str,
) -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )
    grounded = _with_grounded_benchmark_sources(admitted.input, holdouts)
    cases = list(grounded.document.private_benchmark_cases)
    if mutation == "missing_category":
        cases = [
            item.model_copy(update={"category": "normal"})
            if item.category == "mandatory_escalation" else item
            for item in cases
        ]
        expected = "exactly three explicit cases"
    else:
        cases[-1] = cases[-1].model_copy(update={"case_key": cases[0].case_key})
        expected = "case keys are ambiguous"
    document = grounded.document.model_copy(update={
        "private_benchmark_cases": tuple(cases)
    })
    compiled = FactoryInputCompiler(holdout_store=holdouts).compile(document, 1)
    canonical = grounded.model_copy(update={
        "document": document, "compiled_specification": compiled
    })

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match=expected):
        build_generic_benchmark_suite(
            canonical, _private_case_repository(holdouts)
        )


def test_builder_rejects_mixed_gateway_store_before_schema_effect(tmp_path: Path) -> None:
    store = _GatewayStore()
    authorities = _authorities(store=store)
    object.__setattr__(authorities.benchmark_recorder, "gateway_store", _GatewayStore())

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="benchmark recorder.*production Gateway store",
    ):
        build_capability_assembly_controller(
            repository_root=tmp_path,
            settings=CapabilityAssemblyProductionSettings.from_environment(
                _settings_environment(tmp_path)
            ),
            authorities=authorities,
            holdout_store=InMemoryPrivateHoldoutStore(),
        )

    assert store.storage.statements == []


def test_projection_external_error_is_redacted_without_cause_or_secret() -> None:
    secret = "sensitive-provider-token /private/provider/body.json"
    event = _event()

    class ExplodingGateway(_ProjectionGateway):
        async def projection_event(self, event_id: UUID):
            raise RuntimeError(secret)

    effects = GatewayMinibookProjectionEffectsAdapter(
        gateway=ExplodingGateway(event),
        minibook=_MinibookProjection(_acknowledgement(event)),
    )

    with pytest.raises(CapabilityAssemblyProductionConfigurationError) as captured:
        asyncio.run(effects.project_and_ack(event))

    assert captured.value.__cause__ is None
    assert secret not in "".join(__import__("traceback").format_exception(captured.value))


def test_promotion_event_rejects_wrong_release_id_before_minibook() -> None:
    event = _event()
    release = CapabilityFactoryAuthority(
        stage=CapabilityLiveStage.PROMOTE,
        input_id="projection-input",
        source_ref=_ref("source"),
        job_id=UUID("12345678-1234-4234-8234-123456789abc"),
        correlation_id=event.correlation_id,
        subject_version=1,
        capability_id="generic_capability",
        capability_version=1,
        suite_ref=_ref("suite"),
        hermes_result_ref=_ref("hermes"),
        evidence_refs=(_ref("release-evidence"),),
        booked_cost_usd="1.00",
        normal_trace_ids=(_id("trace-1"), _id("trace-2"), _id("trace-3")),
        benchmark_authority_id=_id("benchmark-authority"),
        benchmark_summary_ref=_ref("benchmark-summary"),
        release_authority_id=_id("different-release"),
    )
    payload = event.payload.model_copy(update={
        "artifact_digest": _release_projection_digest(release)
    })
    forged = event.model_copy(update={"payload": payload})
    gateway = _ProjectionGateway(forged)
    minibook = _MinibookProjection(_acknowledgement(forged))
    effects = GatewayMinibookProjectionEffectsAdapter(gateway=gateway, minibook=minibook)

    with pytest.raises(
        CapabilityAssemblyProductionConfigurationError,
        match="promotion projection authority is invalid",
    ):
        asyncio.run(effects.promotion_event(release))

    assert minibook.calls == []


@pytest.mark.parametrize(
    ("relative_path", "bound_batch", "recorded_batch", "message"),
    (
        (
            "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
            "n8n-required-batch",
            None,
            "required n8n WorkBatch lease",
        ),
        (
            "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
            "n8n-unexpected-batch",
            "n8n-unexpected-batch",
            "unexpected n8n WorkBatch lease",
        ),
    ),
)
def test_n8n_gateway_lease_is_cross_checked_in_both_directions(
    relative_path: str,
    bound_batch: str,
    recorded_batch: str | None,
    message: str,
) -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, reservation = _admitted_demo(relative_path, holdouts)
    request = CapabilityAssemblyStageRequest(
        run_id=reservation.run_id,
        input=admitted.input,
        correlation_id=_id("review-correlation"),
        reservation=reservation,
        stage=CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS,
        controlled_recovery_required=True,
    )
    binding = SimpleNamespace(
        job=SimpleNamespace(job_id=_id("review-job")),
        n8n_work_batch_id=bound_batch,
    )

    class Provisioning(_Provisioning):
        def binding_for(self, request: object) -> object:
            return binding

    class N8n:
        def active_n8n_work_batch_id(self, job_id: UUID) -> str | None:
            assert job_id == binding.job.job_id
            return recorded_batch

    class Promotion:
        async def promote_generic_capability(self, request: object, binding: object):
            return object()

    adapter = CapabilityAssemblyCurrentFactoryAdapter(
        composition=_factory_composition(),
        provisioning=Provisioning(),
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        private_cases=_private_case_repository(holdouts),
        n8n_work_batches=N8n(),  # type: ignore[arg-type]
        promotion=Promotion(),  # type: ignore[arg-type]
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match=message):
        adapter._require_n8n_authority(request)


def test_generic_benchmark_keeps_private_case_bodies_out_of_execution_and_recording(
    tmp_path: Path,
) -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, reservation, _ = _admitted_explicit_benchmark(tmp_path, holdouts)
    grounded_input = admitted.input
    request = CapabilityAssemblyStageRequest(
        run_id=reservation.run_id,
        input=grounded_input,
        correlation_id=_id("review-correlation"),
        reservation=reservation,
        stage=CapabilityLiveStage.EVALUATE,
        controlled_recovery_required=True,
        suite_ref=_ref("prior-suite"),
        hermes_result_ref=_ref("hermes-result"),
    )
    job = SimpleNamespace(
        job_id=_id("generic-benchmark-job"),
        required_capability="incident_command",
        subject_version=1,
        private_holdout_refs=grounded_input.compiled_specification.private_holdout_refs,
        execution_policy=SimpleNamespace(
            allowed_models=("fixed-model",),
            model_dump=lambda **kwargs: {"model": "fixed-model", "policy": "fixed"}
        ),
    )

    class Bindings:
        def binding_for(self, request: object) -> object:
            return SimpleNamespace(job=job)

    class Evidence:
        gateway_store = object()
        bodies: dict[ArtifactRef, bytes] = {}

        def put(self, value: dict[str, object]) -> ArtifactRef:
            body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(body).hexdigest()
            ref = ArtifactRef(
                uri=f"artifact://sha256/{digest}", sha256=digest,
                media_type="application/json",
            )
            self.bodies[ref] = body
            return ref

        def resolve(self, reference: ArtifactRef) -> bytes:
            return self.bodies[reference]

        def record_evaluation(self, payload: bytes) -> ArtifactRef:
            value = json.loads(payload)
            assert isinstance(value, dict)
            return self.put(value)

    class Executor:
        gateway_store = object()
        authority_id = _id("raw-executor-authority")
        requests: list[object] = []

        async def execute(self, execution_request):
            self.requests.append(execution_request)
            assert not hasattr(execution_request.case_ref, "setup")
            private_case = execution_request.execution_case
            frontier = list(gc.get_referents(execution_request))
            seen: set[int] = set()
            while frontier:
                referent = frontier.pop()
                if id(referent) in seen or isinstance(referent, type):
                    continue
                seen.add(id(referent))
                assert not isinstance(referent, (
                    GenericPrivateBenchmarkRepository,
                    GenericEvaluationCaseView,
                    BenchmarkExpectedPredicate,
                ))
                frontier.extend(gc.get_referents(referent))
            with pytest.raises(
                CapabilityLiveAuthorityUnavailable, match="purpose authority"
            ):
                private_cases.resolve(
                    execution_request.case_ref, purpose="evaluation",
                    authority_id=self.authority_id,
                )
            with pytest.raises(
                CapabilityLiveAuthorityUnavailable, match="purpose authority"
            ):
                private_cases.resolve(
                    execution_request.case_ref, purpose="execution",
                    authority_id=RuleBasedCapabilityBenchmarkEvaluator.AUTHORITY_ID,
                )
            assert private_case.setup and private_case.action
            assert not hasattr(private_case, "expected_predicates")
            kind = execution_request.team_kind
            trace_id = _id(f"{execution_request.case_ref.case_id}-{kind}-trace")
            run_id = _id(f"{execution_request.case_ref.case_id}-{kind}-run")
            common = {
                "case_digest": execution_request.case_ref.case_digest,
                "trace_id": str(trace_id),
                "run_id": str(run_id),
                "model_policy_digest": execution_request.model_policy_digest,
            }
            outcome_ref = evidence.put({
                "schema": "captain.capability-benchmark-outcome.v1",
                **common,
                "observations": {
                    "status": "ok",
                    "detail": "bounded result",
                    "resolution": "signed_source",
                },
            })
            rationale_ref = evidence.put({
                "schema": "captain.capability-benchmark-rationale.v1",
                **common,
                "text": "verified against private predicates",
            })
            message_ref = evidence.put({
                "schema": "captain.capability-benchmark-message.v1",
                **common,
                "message_key": "result",
                "content": "observable result",
            })
            handoff_ref = evidence.put({
                "schema": "captain.capability-benchmark-handoff.v1",
                **common,
                "from_agent": "worker",
                "to_agent": "captain",
                "reason_code": "mandatory_stop",
            })
            call_ref = evidence.put({
                "schema": "captain.capability-benchmark-tool-call.v1",
                **common,
                "tool_key": "captain_tool",
                "operation": "read",
                "call_id": f"call-{execution_request.case_ref.case_id}-{kind}",
            })
            lease_ref = evidence.put({
                "schema": "captain.capability-benchmark-tool-lease.v1",
                **common,
                "tool_key": "captain_tool",
                "lease_id": f"lease-{execution_request.case_ref.case_id}-{kind}",
                "scope": ["read"],
            })
            receipt_ref = evidence.put({
                "schema": "captain.capability-benchmark-tool-receipt.v1",
                **common,
                "tool_key": "captain_tool",
                "call_id": f"call-{execution_request.case_ref.case_id}-{kind}",
                "status": "succeeded",
                "result_ref_sha256": outcome_ref.sha256,
            })
            tool_authority.issue(call_ref, lease_ref, receipt_ref)
            return GenericBenchmarkExecutionEvidence(
                case_ref=execution_request.case_ref,
                team_kind=kind,
                agent_count=2 if kind == "team" else 1,
                model_id=execution_request.model_id,
                model_policy_digest=execution_request.model_policy_digest,
                input_ref=execution_request.input_ref,
                capability_id=execution_request.capability_id,
                outcome_ref=outcome_ref,
                message_event_refs=(message_ref,),
                handoff_event_refs=(handoff_ref,),
                tool_calls=(GenericToolCallEvidence(
                    tool_key="captain_tool", call_ref=call_ref,
                    lease_ref=lease_ref, receipt_ref=receipt_ref,
                ),),
                rationale_ref=rationale_ref,
                cost_usd=Decimal("0.10") if kind == "team" else Decimal("0.20"),
                latency_ms=10 if kind == "team" else 20,
                trace_id=trace_id,
                run_id=run_id,
                request_nonce=execution_request.request_nonce,
                request_digest=execution_request.request_digest,
            )

    class Recorder:
        gateway_store = object()
        request: object | None = None
        def record_generic_benchmark(self, recording_request):
            self.request = recording_request
            return GenericBenchmarkReceipt(
                authority_id=_id("generic-benchmark-authority"),
                summary_ref=_ref("generic-benchmark-summary"),
                evidence_refs=(_ref("generic-benchmark-recording"),),
            )

    evidence, executor, recorder = Evidence(), Executor(), Recorder()
    tool_authority = _ToolEvidenceAuthority(evidence.gateway_store)
    private_cases = _private_case_repository(
        holdouts, executor_authority_id=executor.authority_id,
    )
    authority = asyncio.run(GenericCapabilityBenchmarkAdapter(
        bindings=Bindings(), executor=executor, evidence=evidence,
        tool_authority=tool_authority,
        recorder=recorder,
        private_cases=private_cases,
    ).evaluate(request))

    serialized = repr(recorder.request)
    for ref in grounded_input.compiled_specification.private_holdout_refs:
        assert holdouts.get(ref.uri) not in serialized
        assert ref.uri not in serialized
    assert "setup=" not in serialized and "expected=" not in serialized
    assert len(executor.requests) == 30
    assert authority.benchmark_summary_ref == _ref("generic-benchmark-summary")
    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="stale"):
        private_cases.resolve(
            executor.requests[0].case_ref,
            purpose="execution",
            authority_id=executor.authority_id,
        )


def test_benchmark_cas_resolver_rejects_forged_artifact_bytes() -> None:
    artifacts = _BenchmarkArtifacts()
    reference = artifacts.put(b'{"schema":"expected"}', "application/json")
    artifacts.content[reference.sha256] = b'{"schema":"forged"}'
    resolver = ContentAddressedBenchmarkEvidenceResolver(
        gateway_store=object(), artifacts=artifacts
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="forged"):
        resolver.resolve(reference)


@pytest.mark.parametrize(
    "mode", (
        "stale_trace", "malicious_observation", "forged_tool_receipts",
        "incomplete_dimensions",
    )
)
def test_rule_evaluator_rejects_stale_or_malicious_observable_evidence(
    mode: str,
) -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )
    canonical = _with_grounded_benchmark_sources(admitted.input, holdouts)
    private_cases = _private_case_repository(
        holdouts, executor_authority_id=_id("stale-fixture-executor"),
    )
    case_ref = build_generic_benchmark_suite(canonical, private_cases).cases[0]
    if mode == "incomplete_dimensions":
        original = private_cases._definitions[case_ref.case_id]
        case_ref = private_cases.register(GenericPrivateCaseDefinition(
            category=original.category,
            assertion_ids=original.assertion_ids,
            private_holdout_uri=original.private_holdout_uri,
            setup=original.setup,
            action=original.action,
            expected_predicates=(BenchmarkExpectedPredicate(
                predicate_key="business_only", kind="observation_equals",
                observed_key="status", expected_value="ok",
            ),),
        ))
    private_cases.resolve(
        case_ref, purpose="execution", authority_id=_id("stale-fixture-executor")
    )
    artifacts = _BenchmarkArtifacts()
    resolver = ContentAddressedBenchmarkEvidenceResolver(
        gateway_store=object(), artifacts=artifacts
    )
    trace_id, stale_trace_id = _id("fresh-trace"), _id("stale-trace")
    run_id = _id("fresh-run")
    common = {
        "case_digest": case_ref.case_digest,
        "trace_id": str(stale_trace_id if mode == "stale_trace" else trace_id),
        "run_id": str(run_id),
        "model_policy_digest": "a" * 64,
    }

    def put(value: dict[str, object]) -> ArtifactRef:
        return artifacts.put(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            "application/json",
        )

    outcome = put({
        "schema": "captain.capability-benchmark-outcome.v1", **common,
        "observations": {
            "status": "malicious_executor_claim" if mode == "malicious_observation" else "ok"
        },
    })
    rationale = put({
        "schema": "captain.capability-benchmark-rationale.v1", **common,
        "text": "verified",
    })
    message = put({
        "schema": "captain.capability-benchmark-message.v1", **common,
        "message_key": "result", "content": "stale",
    })
    handoff = put({
        "schema": "captain.capability-benchmark-handoff.v1", **common,
        "from_agent": "worker", "to_agent": "captain", "reason_code": "mandatory_stop",
    })
    call = put({
        "schema": "captain.capability-benchmark-tool-call.v1", **common,
        "tool_key": "captain_tool", "operation": "read", "call_id": "stale-call",
    })
    lease = put({
        "schema": "captain.capability-benchmark-tool-lease.v1", **common,
        "tool_key": "captain_tool", "lease_id": "stale-lease", "scope": ["read"],
    })
    receipt = put({
        "schema": "captain.capability-benchmark-tool-receipt.v1", **common,
        "tool_key": "captain_tool", "call_id": "stale-call", "status": "succeeded",
        "result_ref_sha256": outcome.sha256,
    })
    execution = GenericBenchmarkExecutionEvidence(
        case_ref=case_ref, team_kind="team", agent_count=2,
        model_id="fixed-model", model_policy_digest="a" * 64,
        input_ref=canonical.source_ref, capability_id="incident_command",
        outcome_ref=outcome, message_event_refs=(message,),
        handoff_event_refs=(handoff,),
        tool_calls=(GenericToolCallEvidence(
            tool_key="captain_tool", call_ref=call, lease_ref=lease,
            receipt_ref=receipt),),
        rationale_ref=rationale, cost_usd=Decimal("0.10"), latency_ms=10,
        trace_id=trace_id, run_id=run_id,
        request_nonce=_id("direct-evaluator-request"),
        request_digest="b" * 64,
    )

    tool_authority = _ToolEvidenceAuthority(resolver.gateway_store)
    if mode != "forged_tool_receipts":
        tool_authority.issue(call, lease, receipt)
    evaluator = RuleBasedCapabilityBenchmarkEvaluator(
        private_cases=private_cases, evidence=resolver,
        tool_authority=tool_authority,
    )
    evaluation_request = IndependentBenchmarkEvaluationRequest(
        job_id=_id("stale-job"), case_ref=case_ref, execution=execution
    )
    if mode == "stale_trace":
        with pytest.raises(
            CapabilityLiveAuthorityUnavailable,
            match="changed execution binding",
        ):
            evaluator.evaluate(evaluation_request)
    elif mode == "malicious_observation":
        result = evaluator.evaluate(evaluation_request)
        assert result.outcome_satisfied is False
    elif mode == "forged_tool_receipts":
        result = evaluator.evaluate(evaluation_request)
        assert result.tool_decisions_valid is False
        assert result.outcome_satisfied is False
    else:
        with pytest.raises(
            CapabilityLiveAuthorityUnavailable,
            match="predicate dimensions are incomplete",
        ):
            evaluator.evaluate(evaluation_request)


def test_benchmark_rejects_same_executor_and_evaluator_authority() -> None:
    class SameAuthorityExecutor:
        gateway_store = object()
        authority_id = RuleBasedCapabilityBenchmarkEvaluator.AUTHORITY_ID
        async def execute(self, request: object) -> object:
            raise AssertionError("must fail during composition")

    class Recorder:
        def record_generic_benchmark(self, request: object) -> object:
            raise AssertionError("must fail during composition")

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable,
        match="authorities must be independent",
    ):
        GenericCapabilityBenchmarkAdapter(
            bindings=object(),
            executor=SameAuthorityExecutor(),  # type: ignore[arg-type]
            evidence=ContentAddressedBenchmarkEvidenceResolver(
                gateway_store=object(), artifacts=_BenchmarkArtifacts()
            ),
            tool_authority=_ToolEvidenceAuthority(object()),
            recorder=Recorder(),  # type: ignore[arg-type]
            private_cases=_private_case_repository(InMemoryPrivateHoldoutStore()),
        )
