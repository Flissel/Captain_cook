from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveReservation,
    CapabilityLiveStage,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
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
)
from gateway.capability_assembly_repository import GatewayCapabilityAssemblyRepository
from gateway.capability_live_current_adapters import CapabilityProjectionReadback
from gateway.capability_live_factory_adapter import CurrentCapabilityFactoryAdapter
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable
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

    async def projection_event(self, event_id: UUID) -> MinibookProjectionEvent | None:
        self.calls.append("feed")
        return self.event if event_id == self.event.event_id else None

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
    def provision(self, request: object) -> object | None:
        del request
        return None

    def binding_for(self, request: object) -> object | None:
        del request
        return None


class _ResumeGateway:
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


def _factory_composition() -> AgentFactoryLiveComposition:
    return AgentFactoryLiveComposition(
        repository=_FactoryRepository(),  # type: ignore[arg-type]
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
        gateway=_ProjectionGateway(event),
        minibook=_MinibookProjection(_acknowledgement(event)),
    )
    source_artifacts = RepositoryFactoryInputArtifactStore(
        artifacts=_Sha256Artifacts()
    )
    return CapabilityAssemblyProductionAuthorities(
        store=store,  # type: ignore[arg-type]
        factory=_factory_composition(),
        provisioning=_Provisioning(),
        resume_gateway=resume_gateway or _ResumeGateway(),  # type: ignore[arg-type]
        reconciliation=_Reconciliation(),
        minibook_effects=projection,
        source_artifacts=source_artifacts,
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
    adapter = CapabilityAssemblyCurrentFactoryAdapter(
        composition=_factory_composition(),
        provisioning=_Provisioning(),
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    request = SimpleNamespace(
        input=SimpleNamespace(required_tool_gaps=(object(),)),
    )

    with pytest.raises(CapabilityLiveAuthorityUnavailable, match="TODO_TOOL"):
        asyncio.run(adapter.promote(request))  # type: ignore[arg-type]


class _ProvisioningProbe(Exception):
    def __init__(self, request: object) -> None:
        super().__init__("captured current provisioning request")
        self.request = request


class _CapturingProvisioning(_Provisioning):
    def provision(self, request: object) -> object | None:
        raise _ProvisioningProbe(request)


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
    adapter = CapabilityAssemblyCurrentFactoryAdapter(
        composition=_factory_composition(),
        provisioning=_CapturingProvisioning(),
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    with pytest.raises(_ProvisioningProbe) as captured:
        asyncio.run(adapter.scaffold(request))

    assert captured.value.request.input.integration_intent == expected_intent


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
            clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        )
