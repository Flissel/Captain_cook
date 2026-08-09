from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveReservation,
    CapabilityLiveStage,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_contracts import RealCaseRequirement
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionEvent,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projector import MinibookProjector, ProjectionResult
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_assembly_admission import RepositoryCapabilityAssemblyInputAdmission
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
    RepositoryFactoryInputArtifactStore,
    build_capability_assembly_controller,
    compose_capability_assembly_controller,
    _release_projection_digest,
)
from gateway.capability_generic_benchmark import (
    GenericBenchmarkCategory,
    GenericBenchmarkExecutionEvidence,
    GenericBenchmarkReceipt,
    GenericCapabilityBenchmarkAdapter,
    ContentAddressedBenchmarkEvidenceResolver,
    IndependentBenchmarkEvaluationRequest,
    GenericPrivateBenchmarkRepository,
    GenericToolCallDecision,
    IndependentBenchmarkOutcome,
    RuleBasedCapabilityBenchmarkEvaluator,
    build_generic_benchmark_suite,
)
from gateway.capability_assembly_repository import GatewayCapabilityAssemblyRepository
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
    class BenchmarkExecutor:
        gateway_store = store
        authority_id = _id("builder-benchmark-executor")
        async def execute(self, request: object) -> object:
            raise AssertionError("not called by builder")
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
        benchmark_executor=BenchmarkExecutor(),  # type: ignore[arg-type]
        benchmark_evidence=ContentAddressedBenchmarkEvidenceResolver(
            gateway_store=store, artifacts=_BenchmarkArtifacts()
        ),
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
        private_cases=GenericPrivateBenchmarkRepository(
            holdouts=InMemoryPrivateHoldoutStore()
        ),
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
        private_cases=GenericPrivateBenchmarkRepository(
            holdouts=InMemoryPrivateHoldoutStore()
        ),
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

    controller = build_capability_assembly_controller(
        repository_root=tmp_path,
        settings=settings,
        authorities=_authorities(store=store),
        holdout_store=InMemoryPrivateHoldoutStore(),
    )

    assert isinstance(controller, CapabilityAssemblyController)
    assert isinstance(controller._repository, GatewayCapabilityAssemblyRepository)
    assert isinstance(controller._factory, CurrentCapabilityFactoryAdapter)
    assert isinstance(controller._resume, GatewayCapabilityResumeExecutionAdapter)
    assert len(store.storage.statements) == 3


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
        compose_capability_assembly_controller(
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


def test_generic_suite_uses_compiled_private_authority_and_all_five_categories() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable,
        match="three source-grounded cases",
    ):
        build_generic_benchmark_suite(
            admitted.input, GenericPrivateBenchmarkRepository(holdouts=holdouts)
        )


def _with_grounded_benchmark_sources(canonical, holdouts):
    document = canonical.document
    third_integration = document.integrations[0].model_copy(
        update={"integration_key": "third_grounded_integration"}
    )
    conflicts = tuple(
        RealCaseRequirement(
            case_key=f"grounded_conflict_{index}",
            observable_setup=f"Source declares conflict {index} between signed facts",
            observable_action=f"Resolve documented conflict {index}",
            observable_expected=f"Use the signed source-of-truth rule {index}",
        )
        for index in range(1, 4)
    )
    grounded_document = document.model_copy(update={
        "integrations": (*document.integrations, third_integration),
        "real_cases": (*document.real_cases, *conflicts),
    })
    compiled = FactoryInputCompiler(holdout_store=holdouts).compile(
        grounded_document, subject_version=canonical.compiled_specification.subject_version
    )
    return canonical.model_copy(update={
        "document": grounded_document,
        "compiled_specification": compiled,
    })


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
        private_cases=GenericPrivateBenchmarkRepository(holdouts=holdouts),
        n8n_work_batches=N8n(),  # type: ignore[arg-type]
        promotion=Promotion(),  # type: ignore[arg-type]
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match=message):
        adapter._require_n8n_authority(request)


def test_generic_benchmark_keeps_private_case_bodies_out_of_execution_and_recording() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, reservation = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )
    grounded_input = _with_grounded_benchmark_sources(admitted.input, holdouts)
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
            private_case = execution_request.private_case_access.resolve()
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
                "expected_digest": hashlib.sha256(private_case.expected.encode()).hexdigest(),
                "assertion_results": {item: True for item in private_case.assertion_ids},
                "safe": True,
                "mandatory_handoff_performed": True,
            })
            rationale_ref = evidence.put({
                "schema": "captain.capability-benchmark-rationale.v1",
                **common,
                "rationale_present": True,
            })
            tool_ref = evidence.put({
                "schema": "captain.capability-benchmark-tool-decision.v1",
                **common,
                "tool_key": "captain_tool",
                "decision": "used",
                "valid": True,
            })
            return GenericBenchmarkExecutionEvidence(
                case_ref=execution_request.case_ref,
                team_kind=kind,
                agent_count=2 if kind == "team" else 1,
                model_id=execution_request.model_id,
                model_policy_digest=execution_request.model_policy_digest,
                input_ref=execution_request.input_ref,
                capability_id=execution_request.capability_id,
                outcome_ref=outcome_ref,
                artifact_refs=(outcome_ref,),
                tool_call_decisions=(GenericToolCallDecision(
                    tool_key="captain_tool", decision="used",
                    evidence_ref=tool_ref,
                ),),
                rationale_ref=rationale_ref,
                cost_usd=Decimal("0.10") if kind == "team" else Decimal("0.20"),
                latency_ms=10 if kind == "team" else 20,
                trace_id=trace_id,
                run_id=run_id,
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
    private_cases = GenericPrivateBenchmarkRepository(holdouts=holdouts)
    authority = asyncio.run(GenericCapabilityBenchmarkAdapter(
        bindings=Bindings(), executor=executor, evidence=evidence,
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


def test_rule_evaluator_rejects_stale_trace_bound_evidence() -> None:
    holdouts = InMemoryPrivateHoldoutStore()
    admitted, _ = _admitted_demo(
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md", holdouts
    )
    canonical = _with_grounded_benchmark_sources(admitted.input, holdouts)
    private_cases = GenericPrivateBenchmarkRepository(holdouts=holdouts)
    case_ref = build_generic_benchmark_suite(canonical, private_cases).cases[0]
    private_case = private_cases.resolve(
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
        "trace_id": str(stale_trace_id),
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
        "expected_digest": hashlib.sha256(private_case.expected.encode()).hexdigest(),
        "assertion_results": {item: True for item in private_case.assertion_ids},
        "safe": True, "mandatory_handoff_performed": True,
    })
    rationale = put({
        "schema": "captain.capability-benchmark-rationale.v1", **common,
        "rationale_present": True,
    })
    tool = put({
        "schema": "captain.capability-benchmark-tool-decision.v1", **common,
        "tool_key": "captain_tool", "decision": "used",
        "valid": True,
    })
    execution = GenericBenchmarkExecutionEvidence(
        case_ref=case_ref, team_kind="team", agent_count=2,
        model_id="fixed-model", model_policy_digest="a" * 64,
        input_ref=canonical.source_ref, capability_id="incident_command",
        outcome_ref=outcome, artifact_refs=(outcome,),
        tool_call_decisions=(GenericToolCallDecision(
            tool_key="captain_tool", decision="used", evidence_ref=tool),),
        rationale_ref=rationale, cost_usd=Decimal("0.10"), latency_ms=10,
        trace_id=trace_id, run_id=run_id,
    )

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable,
        match="changed execution binding",
    ):
        RuleBasedCapabilityBenchmarkEvaluator(
            private_cases=private_cases, evidence=resolver
        ).evaluate(IndependentBenchmarkEvaluationRequest(
            job_id=_id("stale-job"), case_ref=case_ref, execution=execution
        ))


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
            recorder=Recorder(),  # type: ignore[arg-type]
            private_cases=GenericPrivateBenchmarkRepository(
                holdouts=InMemoryPrivateHoldoutStore()
            ),
        )
