"""FastAPI sole-writer gateway over the transactional MariaDB ledger."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
)
from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryJob, FactoryLease, FactoryPhase
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
)
from agenten.delivery.minibook_events import MinibookProjectionAcknowledgementV1
from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkSummaryV1
from agenten.agent_factory.skill_workflow_contracts import TeamEvaluationV1
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from gateway.auth import (
    GatewayRole,
    load_gateway_settings,
    require_actor,
    require_captain,
    require_reader,
    require_worker,
)
from gateway.contracts import (
    ActiveCodexSession,
    BatchProjection,
    DeliveryEventEnvelope,
    ReleaseProjection,
    RecoveryDecisionEvent,
    ReviewDecisionEvent,
    RuntimeOperationProjection,
    RuntimeWriteReceipt,
    FactoryJobProjection,
    FactoryBudgetReleaseRequest,
    FactoryBudgetReservationWriteReceipt,
    FactoryWorkflowArtifact,
    FactoryWorkflowArtifactWriteReceipt,
    BusinessBenchmarkSummaryWriteReceipt,
    FactoryUsageSubmissionV2,
    FactoryReleaseDecisionSubmission,
    FactoryWriteReceipt,
    FactorySkillEvaluationSubmission,
    FactorySkillWriteReceipt,
    PublishedHermesSkill,
)
from gateway.mirror import MirrorQueue
from gateway.portal_auth import initialize_portal_auth, require_portal_principal
from gateway.portal_contracts import (
    PortalPrincipalV1,
    PortalSetupActionRequestV1,
    PortalSetupSelectionRequestV1,
    PortalSetupTicketIssueV1,
    PortalSetupTicketUseV1,
    PortalSetupTicketV1,
    PortalTenantBindingV1,
)
from gateway.registry_feed import mirror_captain_projection
from gateway.registry_feed import (
    MinibookProjectionFeedPage,
    factory_promotion_projection,
    integration_setup_projection,
    runtime_result_projection,
)
from gateway.settings import GatewaySettings
from gateway.store import (
    AppendResult,
    GatewayStore,
    PortalCredentialMetadataSource,
    PortalCredentialVerificationSource,
)
from gateway.integration_setup_contracts import (
    IntegrationSetupMutationV1,
    IntegrationSetupSubmissionV1,
    IntegrationSetupWriteReceiptV1,
    PersistedIntegrationSetupV1,
    IntegrationSetupSurfaceV1,
    build_integration_setup_surface,
)


logger = logging.getLogger(__name__)


class Mirror(Protocol):
    def enqueue_nowait(self, block: dict[str, Any]) -> None: ...


class BlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: str = Field(min_length=1, max_length=128)
    data: dict[str, Any]
    status: str = Field(default="pending", min_length=1, max_length=64)
    parent_index: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SinkCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str = Field(min_length=1, max_length=128)
    tag: str = Field(min_length=1, max_length=128)


class LegacyDeliveryImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legacy_record_id: str = Field(min_length=1, max_length=256)
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    record_type: Literal["todo", "event"]
    data: dict[str, Any]


class ReleaseDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(min_length=1, max_length=128)


CAPTAIN_WRITE_BLOCK_TYPES = frozenset(
    {"problem", "work_batch", "holdout", "recovery_decision", "review_decision"}
)
CAPTAIN_FACTORY_PHASES = frozenset(
    {
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.IMPROVEMENT_REQUESTED,
        FactoryPhase.CAPABILITY_PROMOTED,
        FactoryPhase.ESCALATED,
    }
)
CAPTAIN_SKILL_EVENT_TYPES = frozenset(
    {
        "hermes_skill_evaluation_requested",
        "hermes_skill_published",
        "hermes_ready_to_use_validated",
        "captain_business_benchmark_validated",
    }
)
HERMES_SKILL_EVENT_TYPES = frozenset(
    {
        "hermes_skill_candidate_built",
        "hermes_skill_test_recorded",
        "hermes_tool_gap_recorded",
        "hermes_skill_evaluation_submitted",
    }
)


def require_block_writer(block_type: str, actor: GatewayRole) -> None:
    expected = (
        GatewayRole.CAPTAIN
        if block_type in CAPTAIN_WRITE_BLOCK_TYPES
        else GatewayRole.WORKER
    )
    if actor is not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient gateway role",
        )


def require_factory_block_writer(evidence: FactoryEvidenceBlock, actor: GatewayRole) -> None:
    expected = (
        GatewayRole.CAPTAIN
        if evidence.phase in CAPTAIN_FACTORY_PHASES
        else GatewayRole.WORKER
    )
    if actor is not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient gateway role",
        )


def require_skill_event_writer(event: DeliveryEventEnvelope, actor: GatewayRole) -> None:
    expected: GatewayRole | None = None
    expected_payload_actor: str | None = None
    if event.event_type in CAPTAIN_SKILL_EVENT_TYPES:
        expected = GatewayRole.CAPTAIN
        expected_payload_actor = "captain"
    elif event.event_type in HERMES_SKILL_EVENT_TYPES:
        expected = GatewayRole.WORKER
        expected_payload_actor = "hermes"
    if expected is not None and (
        actor is not expected or event.actor != expected_payload_actor
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient gateway role",
        )


def _factory_promotion_benchmark_summary(
    store: GatewayStore,
    block: dict[str, Any],
) -> BusinessBenchmarkSummaryV1:
    """Resolve the exact persisted V3 summary; never derive public metrics."""

    job_id = UUID(str(block["job_id"]))
    attempt = int(block["attempt"])
    evaluations = tuple(
        artifact
        for artifact in store.factory_workflow_artifacts(job_id)
        if isinstance(artifact, TeamEvaluationV1) and artifact.attempt == attempt
    )
    if len(evaluations) != 1 or evaluations[0].benchmark_summary_ref is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="factory promotion has no unambiguous business benchmark evaluation",
        )
    summary = store.business_benchmark_summary_by_artifact(
        evaluations[0].benchmark_summary_ref
    )
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="factory promotion business benchmark summary is unavailable",
        )
    return summary


def create_app(
    *,
    storage: MariaDBStorage | None = None,
    mirror: Mirror | None = None,
    settings: GatewaySettings | None = None,
    gateway_store: GatewayStore | None = None,
    portal_credential_source: PortalCredentialMetadataSource | None = None,
    portal_verification_source: PortalCredentialVerificationSource | None = None,
    portal_clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    mirror = mirror or MirrorQueue(mirror_captain_projection)
    store_lock = Lock()
    store: GatewayStore | None = gateway_store or (
        GatewayStore(
            storage,
            claim_ttl=timedelta(seconds=settings.claim_ttl_seconds),
            portal_credential_source=portal_credential_source,
            portal_verification_source=portal_verification_source,
        )
        if storage and settings
        else GatewayStore(storage)
        if storage
        else None
    )
    portal_clock = portal_clock or (lambda: datetime.now(timezone.utc))
    sink_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        load_gateway_settings(app)
        start = getattr(mirror, "start", None)
        if start:
            await start()
        try:
            yield
        finally:
            stop = getattr(mirror, "stop", None)
            if stop:
                await stop()

    app = FastAPI(
        title="Captain Cook Ledger Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.gateway_settings = settings
    app.state.gateway_settings_lock = Lock()
    initialize_portal_auth(app)

    @app.exception_handler(RequestValidationError)
    async def sanitized_review_validation_error(
        request: Request,
        exc: RequestValidationError,
    ):
        path = request.url.path
        if request.method == "POST" and path.startswith("/v1/portal/"):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid portal request"},
            )
        if request.method == "POST" and path.startswith("/v1/factory/"):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid factory request"},
            )
        if (
            request.method == "POST"
            and path.startswith("/batches/")
            and path.endswith("/review")
            and path.count("/") == 3
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid review decision"},
            )
        return await request_validation_exception_handler(request, exc)

    def get_store() -> GatewayStore:
        nonlocal store
        if store is None:
            with store_lock:
                if store is None:
                    configured = load_gateway_settings(app)
                    dsn = configured.ledger_dsn.get_secret_value()
                    store = GatewayStore(
                        MariaDBStorage(dsn),
                        claim_ttl=timedelta(seconds=configured.claim_ttl_seconds),
                        portal_credential_source=portal_credential_source,
                        portal_verification_source=portal_verification_source,
                    )
        return store

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        try:
            load_gateway_settings(app)
            with get_store().storage.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    if cursor.fetchone() is None:
                        raise RuntimeError("database readiness query returned no row")
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="gateway unavailable",
            ) from None
        return {"status": "ok", "database": "ready"}

    @app.get("/batches")
    async def list_batches(
        status_filter: str = Query(alias="status"),
        _: GatewayRole = Depends(require_reader),
    ) -> list[dict[str, str]]:
        return get_store().list_batches(status_filter)

    def enqueue_runtime_projection(projection: dict[str, Any]) -> None:
        try:
            mirror.enqueue_nowait(projection)
        except Exception:
            logger.exception(
                "Could not enqueue runtime event %s for Minibook mirroring",
                projection.get("event_id"),
            )

    @app.post("/v1/factory/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def accept_factory_job(
        job: FactoryJob,
        _: GatewayRole = Depends(require_captain),
    ) -> FactoryWriteReceipt:
        return get_store().record_factory_job(job)

    @app.post(
        "/v1/factory/integration-setups",
        status_code=status.HTTP_201_CREATED,
    )
    async def record_integration_setup(
        submission: IntegrationSetupSubmissionV1,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> IntegrationSetupWriteReceiptV1:
        receipt = get_store().record_integration_setup(submission)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.get("/v1/factory/jobs/{job_id}/integration-setup")
    async def get_integration_setup(
        job_id: UUID,
        _: GatewayRole = Depends(require_captain),
    ) -> PersistedIntegrationSetupV1:
        return get_store().integration_setup(job_id)

    @app.get("/v1/factory/jobs/{job_id}/integration-setup/surface")
    async def get_integration_setup_surface(
        job_id: UUID,
        _: GatewayRole = Depends(require_captain),
    ) -> IntegrationSetupSurfaceV1:
        configured = load_gateway_settings(app)
        return build_integration_setup_surface(
            get_store().integration_setup(job_id),
            n8n_ui_base_url=configured.captain_n8n_ui_url,
        )

    @app.post(
        "/v1/factory/jobs/{job_id}/integration-setup/mutations",
        status_code=status.HTTP_201_CREATED,
    )
    async def mutate_integration_setup(
        job_id: UUID,
        mutation: IntegrationSetupMutationV1,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> IntegrationSetupWriteReceiptV1:
        receipt = get_store().mutate_integration_setup(job_id, mutation)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    def portal_surface(
        job_id: UUID,
        principal: PortalPrincipalV1,
    ) -> IntegrationSetupSurfaceV1:
        configured = load_gateway_settings(app)
        return build_integration_setup_surface(
            get_store().portal_integration_setup(job_id, principal.organization_id),
            n8n_ui_base_url=configured.captain_n8n_ui_url,
        )

    @app.post(
        "/v1/portal/integration-setups/{job_id}/tickets",
        status_code=status.HTTP_201_CREATED,
    )
    async def issue_portal_setup_ticket(
        job_id: UUID,
        request: PortalSetupTicketIssueV1,
        principal: PortalPrincipalV1 = Depends(require_portal_principal),
    ) -> PortalSetupTicketV1:
        return get_store().issue_portal_ticket(
            job_id=job_id,
            principal=principal,
            credential_alias=request.credential_alias,
            action=request.action,
            now=portal_clock(),
        )

    @app.post(
        "/v1/factory/integration-setups/{job_id}/portal-tenant-binding",
        status_code=status.HTTP_201_CREATED,
    )
    async def provision_portal_tenant_binding(
        job_id: UUID,
        binding: PortalTenantBindingV1,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> PortalTenantBindingV1:
        if binding.job_id != job_id:
            raise HTTPException(status_code=409, detail="portal binding job_id must match route")
        created = get_store().provision_portal_tenant(binding)
        if not created:
            response.status_code = status.HTTP_200_OK
        return binding

    @app.get("/v1/portal/integration-setups/{job_id}")
    async def get_portal_integration_setup(
        job_id: UUID,
        principal: PortalPrincipalV1 = Depends(require_portal_principal),
    ) -> IntegrationSetupSurfaceV1:
        return portal_surface(job_id, principal)

    @app.post("/v1/portal/integration-setups/{job_id}/discover")
    async def discover_portal_credentials(
        job_id: UUID,
        request: PortalSetupTicketUseV1,
        principal: PortalPrincipalV1 = Depends(require_portal_principal),
    ) -> IntegrationSetupSurfaceV1:
        persisted = get_store().portal_discover(
            job_id=job_id,
            principal=principal,
            request=request,
            now=portal_clock(),
        )
        configured = load_gateway_settings(app)
        return build_integration_setup_surface(
            persisted,
            n8n_ui_base_url=configured.captain_n8n_ui_url,
        )

    @app.post("/v1/portal/integration-setups/{job_id}/select")
    async def select_portal_credential(
        job_id: UUID,
        request: PortalSetupSelectionRequestV1,
        principal: PortalPrincipalV1 = Depends(require_portal_principal),
    ) -> IntegrationSetupSurfaceV1:
        persisted = get_store().portal_select(
            job_id=job_id,
            principal=principal,
            request=request,
            now=portal_clock(),
        )
        configured = load_gateway_settings(app)
        return build_integration_setup_surface(
            persisted,
            n8n_ui_base_url=configured.captain_n8n_ui_url,
        )

    @app.post("/v1/portal/integration-setups/{job_id}/verify")
    async def verify_portal_credential(
        job_id: UUID,
        request: PortalSetupTicketUseV1,
        principal: PortalPrincipalV1 = Depends(require_portal_principal),
    ) -> IntegrationSetupSurfaceV1:
        persisted = get_store().portal_verify(
            job_id=job_id,
            principal=principal,
            request=request,
            now=portal_clock(),
        )
        configured = load_gateway_settings(app)
        return build_integration_setup_surface(
            persisted,
            n8n_ui_base_url=configured.captain_n8n_ui_url,
        )

    @app.post("/v1/portal/integration-setups/{job_id}/actions")
    async def mutate_portal_integration_setup(
        job_id: UUID,
        request: PortalSetupActionRequestV1,
        principal: PortalPrincipalV1 = Depends(require_portal_principal),
    ) -> IntegrationSetupSurfaceV1:
        persisted = get_store().portal_mutate(
            job_id=job_id,
            principal=principal,
            request=request,
            now=portal_clock(),
        )
        configured = load_gateway_settings(app)
        return build_integration_setup_surface(
            persisted,
            n8n_ui_base_url=configured.captain_n8n_ui_url,
        )

    @app.post("/v1/factory/budget/reservations", status_code=status.HTTP_201_CREATED)
    async def reserve_factory_budget(
        reservation: FactoryBudgetReservationV1,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> FactoryBudgetReservationWriteReceipt:
        receipt = get_store().reserve_factory_budget(reservation)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.post("/v1/factory/budget/usage", status_code=status.HTTP_201_CREATED)
    async def record_factory_usage(
        usage: FactoryUsageSubmissionV2,
        response: Response,
        _: GatewayRole = Depends(require_worker),
    ) -> FactoryBudgetWriteReceipt:
        receipt = get_store().record_factory_usage(usage)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.post("/v1/factory/budget/releases", status_code=status.HTTP_201_CREATED)
    async def release_factory_budget(
        release: FactoryBudgetReleaseRequest,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> FactoryBudgetWriteReceipt:
        receipt = get_store().release_factory_budget(release)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.get("/v1/factory/jobs/{job_id}/budget")
    async def get_factory_budget(
        job_id: UUID,
        _: GatewayRole = Depends(require_reader),
    ) -> FactoryBudgetProjection:
        return get_store().factory_budget(job_id)

    @app.post("/v1/factory/workflow-artifacts", status_code=status.HTTP_201_CREATED)
    async def record_factory_workflow_artifact(
        artifact: FactoryWorkflowArtifact,
        response: Response,
        _: GatewayRole = Depends(require_worker),
    ) -> FactoryWorkflowArtifactWriteReceipt:
        receipt = get_store().record_factory_workflow_artifact(artifact)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.get("/v1/factory/jobs/{job_id}/workflow-artifacts")
    async def get_factory_workflow_artifacts(
        job_id: UUID,
        _: GatewayRole = Depends(require_reader),
    ) -> tuple[FactoryWorkflowArtifact, ...]:
        return get_store().factory_workflow_artifacts(job_id)

    @app.post("/v1/factory/business-benchmarks", status_code=status.HTTP_201_CREATED)
    async def record_business_benchmark_summary(
        summary: BusinessBenchmarkSummaryV1,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> BusinessBenchmarkSummaryWriteReceipt:
        receipt = get_store().record_business_benchmark_summary(summary)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.get("/v1/factory/business-benchmarks/artifacts/{artifact_sha256}")
    async def get_business_benchmark_summary_by_artifact(
        artifact_sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
        _: GatewayRole = Depends(require_reader),
    ) -> BusinessBenchmarkSummaryV1:
        artifact_ref = ArtifactRef(
            uri=f"artifact://business-benchmark-summary/{artifact_sha256}",
            sha256=artifact_sha256,
            media_type="application/json",
        )
        summary = get_store().business_benchmark_summary_by_artifact(artifact_ref)
        if summary is None:
            raise HTTPException(status_code=404, detail="business benchmark summary not found")
        return summary

    @app.get("/v1/factory/business-benchmarks/{summary_id}")
    async def get_business_benchmark_summary(
        summary_id: UUID,
        _: GatewayRole = Depends(require_reader),
    ) -> BusinessBenchmarkSummaryV1:
        summary = get_store().business_benchmark_summary(summary_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="business benchmark summary not found")
        return summary

    @app.post("/v1/factory/blocks", status_code=status.HTTP_201_CREATED)
    async def record_factory_block(
        evidence: FactoryEvidenceBlock,
        response: Response,
        actor: GatewayRole = Depends(require_actor),
    ) -> FactoryWriteReceipt:
        require_factory_block_writer(evidence, actor)
        receipt = get_store().record_factory_block(evidence)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        else:
            factory = get_store().factory_job(evidence.job_id)
            enqueue_runtime_projection(
                {
                    "event_type": "factory_lifecycle",
                    "job_id": str(evidence.job_id),
                    "capability_id": factory.job.required_capability,
                    "phase": evidence.phase.value,
                    "status": evidence.status.value,
                    "attempt": evidence.attempt,
                    "subject_version": evidence.subject_version,
                }
            )
        return receipt

    @app.post("/v1/factory/skills/releases", status_code=status.HTTP_201_CREATED)
    async def record_released_factory_skill(
        skill: ReleasedHermesSkill,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> FactorySkillWriteReceipt:
        receipt = get_store().record_released_factory_skill(skill)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.post("/v1/factory/evaluations", status_code=status.HTTP_201_CREATED)
    async def record_factory_skill_evaluation(
        submission: FactorySkillEvaluationSubmission,
        response: Response,
        _: GatewayRole = Depends(require_worker),
    ) -> FactorySkillWriteReceipt:
        receipt = get_store().record_factory_skill_evaluation(submission)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.get("/v1/factory/evaluations/{job_id}")
    async def get_factory_skill_evaluation(
        job_id: UUID,
        _: GatewayRole = Depends(require_reader),
    ) -> Any:
        evaluation = get_store().factory_skill_evaluation(job_id)
        if evaluation is None:
            raise HTTPException(status_code=404, detail="factory skill evaluation not found")
        return evaluation

    @app.post("/v1/factory/skills/publications", status_code=status.HTTP_201_CREATED)
    async def publish_factory_skill(
        publication: PublishedHermesSkill,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> FactorySkillWriteReceipt:
        receipt = get_store().publish_factory_skill(publication)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.post("/v1/factory/release-decisions", status_code=status.HTTP_201_CREATED)
    async def record_factory_release_decision(
        submission: FactoryReleaseDecisionSubmission,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> FactorySkillWriteReceipt:
        receipt = get_store().record_factory_release_decision(submission)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.post("/v1/factory/leases", status_code=status.HTTP_201_CREATED)
    async def record_factory_lease(
        lease: FactoryLease,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> FactoryWriteReceipt:
        receipt = get_store().record_factory_lease(lease)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.get("/v1/factory/jobs/{job_id}")
    async def get_factory_job(
        job_id: UUID,
        _: GatewayRole = Depends(require_reader),
    ) -> FactoryJobProjection:
        return get_store().factory_job(job_id)

    @app.get("/api/v1/projections/minibook/events")
    async def minibook_projection_feed(
        cursor: str | None = Query(default=None, pattern=r"^[0-9]+$"),
        limit: int = Query(default=100, ge=1, le=100),
        _: GatewayRole = Depends(require_captain),
    ) -> MinibookProjectionFeedPage:
        after_index = int(cursor) if cursor is not None else -1
        records, has_more = get_store().minibook_projection_feed(
            after_index=after_index,
            limit=limit,
        )
        projected_events = []
        for _, block_type, data, parent in records:
            if block_type == "agent_factory_block" and parent is not None:
                benchmark_summary = (
                    _factory_promotion_benchmark_summary(get_store(), data)
                    if parent.get("schema") == "captain.agent-factory-job.v3"
                    else None
                )
                event = factory_promotion_projection(
                    data,
                    parent,
                    benchmark_summary=benchmark_summary,
                )
            elif block_type == "factory_integration_setup" and parent is not None:
                event = integration_setup_projection(
                    IntegrationSetupSubmissionV1.model_validate(data),
                    parent,
                )
            else:
                event = runtime_result_projection(data)
            if event is not None:
                projected_events.append(event)
        events = tuple(projected_events)
        next_cursor = str(records[-1][0]) if records else str(after_index)
        return MinibookProjectionFeedPage(
            events=events,
            cursor=next_cursor,
            has_more=has_more,
        )

    @app.post(
        "/api/v1/projections/minibook/acknowledgements",
        status_code=status.HTTP_201_CREATED,
    )
    async def acknowledge_minibook_projection(
        acknowledgement: MinibookProjectionAcknowledgementV1,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> AppendResult:
        result = get_store().record_minibook_projection_acknowledgement(
            acknowledgement
        )
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return result

    @app.post("/v1/runtime/commands", status_code=status.HTTP_202_ACCEPTED)
    async def accept_runtime_command(
        command: AgentRuntimeCommand,
        _: GatewayRole = Depends(require_captain),
    ) -> RuntimeWriteReceipt:
        receipt = get_store().accept_runtime_command(command)
        if not receipt.replayed:
            payload = command.payload
            enqueue_runtime_projection(
                {
                    "event_type": "runtime_command_accepted",
                    "event_id": str(command.event_id),
                    "correlation_id": str(command.correlation_id),
                    "project_id": payload.project_id,
                    "batch_id": payload.batch_id,
                    "subtask_id": payload.subtask_id,
                    "subject_version": command.subject_version,
                    "operation": payload.operation.value,
                    "status": "accepted",
                }
            )
        return receipt

    @app.post("/v1/runtime/grants", status_code=status.HTTP_201_CREATED)
    async def record_runtime_grant(
        grant: CapabilityGrant,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> RuntimeWriteReceipt:
        receipt = get_store().record_capability_grant(grant)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        else:
            enqueue_runtime_projection(
                {
                    "event_type": "runtime_capability_granted",
                    "event_id": grant.grant_id,
                    "operation_id": str(grant.command_id),
                    "batch_id": grant.batch_id,
                    "subtask_id": grant.subtask_id,
                    "subject_version": grant.batch_version,
                    "profile": grant.profile.value,
                    "status": "active",
                    "expires_at": grant.expires_at.isoformat(),
                }
            )
        return receipt

    @app.post("/v1/runtime/grant-revocations", status_code=status.HTTP_201_CREATED)
    async def revoke_runtime_grant(
        revocation: CapabilityGrantRevocation,
        response: Response,
        _: GatewayRole = Depends(require_captain),
    ) -> RuntimeWriteReceipt:
        receipt = get_store().record_capability_grant_revocation(revocation)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        else:
            enqueue_runtime_projection(
                {
                    "event_type": "runtime_capability_revoked",
                    "event_id": str(revocation.revocation_id),
                    "operation_id": str(revocation.command_id),
                    "grant_id": revocation.grant_id,
                    "status": "revoked",
                    "reason": revocation.reason,
                    "revoked_at": revocation.revoked_at.isoformat(),
                }
            )
        return receipt

    @app.post("/v1/runtime/results", status_code=status.HTTP_201_CREATED)
    async def record_runtime_result(
        result: AgentRuntimeResult,
        response: Response,
        _: GatewayRole = Depends(require_actor),
    ) -> RuntimeWriteReceipt:
        receipt = get_store().record_runtime_result(result)
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        else:
            enqueue_runtime_projection(
                {
                    "event_type": "runtime_result_recorded",
                    "event_id": str(result.event_id),
                    "operation_id": str(result.command_id),
                    "correlation_id": str(result.correlation_id),
                    "subject_id": result.subject_id,
                    "subject_version": result.subject_version,
                    "operation": result.operation.value,
                    "status": result.status.value,
                    "session_id": result.session_id,
                }
            )
        return receipt

    @app.get("/v1/runtime/operations/{operation_id}")
    async def get_runtime_operation(
        operation_id: UUID,
        _: GatewayRole = Depends(require_reader),
    ) -> RuntimeOperationProjection:
        return get_store().runtime_operation(operation_id)

    @app.post("/v1/delivery/events")
    async def append_delivery_event(
        event: DeliveryEventEnvelope,
        response: Response,
        actor: GatewayRole = Depends(require_actor),
    ) -> AppendResult:
        require_skill_event_writer(event, actor)
        result = get_store().append_delivery_event(
            event,
            require_current_claim=(
                actor is GatewayRole.WORKER
                and event.event_type not in HERMES_SKILL_EVENT_TYPES
            ),
        )
        response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        return result

    @app.get("/v1/projects/{project_id}/runs/{run_id}/events")
    async def delivery_events(
        project_id: str,
        run_id: str,
        _: GatewayRole = Depends(require_reader),
    ) -> tuple[DeliveryEventEnvelope, ...]:
        return get_store().delivery_events(project_id=project_id, run_id=run_id)

    @app.get("/v1/projects/{project_id}/runs/{run_id}/release")
    async def delivery_release(
        project_id: str,
        run_id: str,
        _: GatewayRole = Depends(require_reader),
    ) -> ReleaseProjection:
        return get_store().release_projection(project_id=project_id, run_id=run_id)

    @app.post("/v1/projects/{project_id}/runs/{run_id}/release/decision")
    async def record_release_decision(
        project_id: str,
        run_id: str,
        request: ReleaseDecisionRequest,
        _: GatewayRole = Depends(require_captain),
    ) -> DeliveryEventEnvelope:
        decision, _readiness = get_store().record_release_decision(
            project_id=project_id,
            run_id=run_id,
            policy_version=request.policy_version,
        )
        return decision

    @app.get("/v1/projects/{project_id}/runs/{run_id}/holdouts/{case_id}")
    async def delivery_holdout_case(
        project_id: str,
        run_id: str,
        case_id: str,
        _: GatewayRole = Depends(require_captain),
    ) -> dict[str, Any]:
        return get_store().delivery_holdout_case(
            project_id=project_id,
            run_id=run_id,
            case_id=case_id,
        )

    @app.post("/batches/{batch_id}/claim")
    async def claim_batch(
        batch_id: str,
        _: GatewayRole = Depends(require_worker),
    ) -> dict[str, str | int]:
        return get_store().claim(batch_id)

    @app.post("/batches/{batch_id}/claim/heartbeat")
    async def heartbeat(
        batch_id: str,
        x_claim_token: str | None = Header(default=None),
        _: GatewayRole = Depends(require_worker),
    ) -> dict[str, str]:
        return get_store().heartbeat(batch_id, x_claim_token)

    @app.post("/batches/{batch_id}/approve")
    async def approve(
        batch_id: str,
        _: GatewayRole = Depends(require_captain),
    ) -> dict[str, str]:
        if not load_gateway_settings(app).approval_enabled:
            raise HTTPException(status_code=404, detail="approval endpoint disabled")
        get_store().approve(batch_id)
        return {"status": "pending"}

    @app.get("/batches/{batch_id}")
    async def get_batch(
        batch_id: str,
        _: GatewayRole = Depends(require_reader),
    ) -> BatchProjection:
        return get_store().batch_projection(batch_id)

    @app.get("/batches/{batch_id}/active-codex-sessions")
    async def get_active_codex_sessions(
        batch_id: str,
        _: GatewayRole = Depends(require_captain),
    ) -> tuple[ActiveCodexSession, ...]:
        return get_store().active_codex_sessions(batch_id)

    @app.post(
        "/batches/{batch_id}/recovery",
        status_code=status.HTTP_201_CREATED,
    )
    async def record_recovery(
        batch_id: str,
        request: RecoveryDecisionEvent,
        _: GatewayRole = Depends(require_captain),
    ) -> RecoveryDecisionEvent:
        if request.batch_id != batch_id:
            raise HTTPException(status_code=422, detail="recovery batch_id must match route")
        block = get_store().recover(request)
        return RecoveryDecisionEvent.model_validate(block["data"])

    @app.post(
        "/batches/{batch_id}/review",
        status_code=status.HTTP_201_CREATED,
    )
    async def record_review(
        batch_id: str,
        request: ReviewDecisionEvent,
        _: GatewayRole = Depends(require_captain),
    ) -> ReviewDecisionEvent:
        if request.batch_id != batch_id:
            raise HTTPException(status_code=422, detail="review batch_id must match route")
        block = get_store().review(request)
        return ReviewDecisionEvent.model_validate(block["data"])

    @app.post("/blocks", status_code=status.HTTP_201_CREATED)
    async def add_block(
        request: BlockRequest,
        x_claim_token: str | None = Header(default=None),
        actor: GatewayRole = Depends(require_actor),
    ) -> dict[str, Any]:
        require_block_writer(request.block_type, actor)
        block = get_store().append(request, x_claim_token)
        try:
            mirror.enqueue_nowait(block)
        except Exception:
            logger.exception("Could not enqueue block %s for Minibook mirroring", block["index"])
        return block

    @app.get("/batches/{batch_id}/bundle")
    async def get_bundle(
        batch_id: str,
        _: GatewayRole = Depends(require_reader),
    ) -> dict[str, Any]:
        return get_store().bundle(batch_id)

    @app.get("/batches/{batch_id}/blocks")
    async def get_blocks(
        batch_id: str,
        _: GatewayRole = Depends(require_reader),
    ) -> list[dict[str, Any]]:
        return get_store().blocks(batch_id)

    @app.get("/batches/{batch_id}/holdout")
    async def get_holdout(
        batch_id: str,
        _: GatewayRole = Depends(require_reader),
    ) -> None:
        del batch_id
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="legacy holdout route is gone",
        )

    @app.post("/sink/crm", status_code=status.HTTP_201_CREATED)
    async def write_sink(
        call: SinkCall,
        _: GatewayRole = Depends(require_worker),
    ) -> dict[str, Any]:
        payload = call.model_dump()
        sink_calls.append(payload)
        return payload

    @app.get("/sink/crm")
    async def read_sink(
        case_id: str,
        _: GatewayRole = Depends(require_worker),
    ) -> list[dict[str, Any]]:
        return [call for call in sink_calls if call["case_id"] == case_id]

    @app.get("/capabilities")
    async def capabilities(
        need: str = Query(min_length=1),
        _: GatewayRole = Depends(require_reader),
    ) -> list[dict[str, Any]]:
        return get_store().capabilities(need)

    @app.post("/imports/legacy-delivery", status_code=status.HTTP_201_CREATED)
    async def import_legacy_delivery(
        request: LegacyDeliveryImportRequest,
        _: GatewayRole = Depends(require_captain),
    ) -> dict[str, Any]:
        block, created = get_store().import_legacy_record(request)
        return {"created": created, "block": block}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = GatewaySettings.from_env()
    app.state.gateway_settings = settings
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
