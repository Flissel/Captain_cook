"""FastAPI sole-writer gateway over the transactional MariaDB ledger."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path as FileSystemPath
from threading import Lock
from collections.abc import Mapping
from typing import Any, Callable, Literal, Protocol
from uuid import UUID, uuid4

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementRequestV1,
    RuntimeResumeCostSettlementV1,
    RuntimeProviderCostSettlementV1,
    RuntimeUsagePricingSnapshotV1,
)
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)
from agenten.provider_proxy.contracts import (
    ProviderBudgetContextV1,
    RuntimeProviderProxyBindingV1,
)
from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryJob, FactoryLease, FactoryPhase
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
)
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionRebuildReceiptV1,
)
from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkSummaryV1
from agenten.agent_factory.skill_workflow_contracts import TeamEvaluationV1
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from gateway.auth import (
    GatewayRole,
    load_gateway_settings,
    require_actor,
    require_captain,
    require_reader,
    require_provider_receipt_authority,
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
from gateway.portal_control_auth import (
    PortalControlRole,
    require_evidence_control,
    require_provider_control,
    require_restart_control,
)
from gateway.portal_n8n_composition import build_portal_n8n_adapter_bundle
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
from agenten.agent_runtime.http_executor import AgentRuntimeHttpExecutor
from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityResumeAuthorization,
)
from gateway.capability_resume_repository import (
    GatewayCapabilityResumeAuthoritySource,
    GatewayCapabilityResumeRepository,
)
from gateway.capability_resume_contracts import (
    CapabilityResumeAuthorizationHttpRequest,
    CapabilityResumeAuthorizeHttpRequest,
)
from gateway.capability_resume_service import (
    CapabilityResumeConflict,
    CapabilityResumeDispatchFailed,
    CapabilityResumeExecutor,
    CapabilityResumeNotFound,
    CapabilityResumeService,
    CapabilityResumeUnavailable,
)
from gateway.capability_assembly_api import (
    CapabilityAssemblyCompositionFactory,
    create_production_capability_assembly_router,
    unavailable_capability_assembly_composition,
)
from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from gateway.capability_assembly_bootstrap import (
    CapabilityAssemblyFactoryRuntime,
    GatewayPrivateHoldoutStore,
    build_capability_assembly_factory_runtime_from_environment,
    build_production_capability_assembly_from_environment,
    close_production_capability_assembly,
    load_capability_assembly_bootstrap_settings,
)
from gateway.portal_live_contracts import (
    PortalLiveEvidenceQueryV1,
    PortalLiveEvidenceV1,
    PortalLiveRunDecisionV1,
    PortalLiveRunFinalizationV1,
    PortalProviderAuditQueryV1,
    PortalProviderAuditV1,
    PortalProviderProbeCompletionV1,
    PortalProviderProbeRequestV1,
    PortalRestartReceiptV1,
)


logger = logging.getLogger(__name__)


CapabilityAssemblyRuntimeBuilder = Callable[..., CapabilityAssemblyFactoryRuntime]


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
    capability_resume_service: CapabilityResumeService | None = None,
    capability_resume_executor: CapabilityResumeExecutor | None = None,
    capability_assembly_composition_factory: (
        CapabilityAssemblyCompositionFactory | None
    ) = None,
    capability_assembly_environment: Mapping[str, str] | None = None,
    capability_assembly_factory_runtime: CapabilityAssemblyFactoryRuntime | None = None,
    capability_assembly_private_catalog: PrivateHoldoutStore | None = None,
    capability_assembly_repository_root: FileSystemPath | None = None,
    runtime_usage_artifacts: ContentAddressedArtifactAdapter | None = None,
    runtime_usage_pricing_snapshots: tuple[
        RuntimeUsagePricingSnapshotV1, ...
    ] = (),
) -> FastAPI:
    mirror = mirror or MirrorQueue(mirror_captain_projection)
    store_lock = Lock()
    def environment_value(name: str) -> str:
        source = capability_assembly_environment or os.environ
        value = source.get(name, "")
        return value if isinstance(value, str) else ""

    def configured_store(
        target_storage: MariaDBStorage,
        configured: GatewaySettings | None,
    ) -> GatewayStore:
        created = GatewayStore(
            target_storage,
            claim_ttl=(
                timedelta(seconds=configured.claim_ttl_seconds)
                if configured is not None
                else timedelta(minutes=90)
            ),
            portal_credential_source=portal_credential_source,
            portal_verification_source=portal_verification_source,
            runtime_usage_artifacts=(
                runtime_usage_artifacts
                or ContentAddressedArtifactAdapter(
                    FileSystemPath.cwd() / ".captain-cook" / "artifacts" / "sha256"
                )
            ),
            runtime_usage_pricing_snapshots=runtime_usage_pricing_snapshots,
            runtime_provider_proxy_bindings=(
                (
                    configured.provider_proxy_url,
                    configured.provider_policy_sha256,
                    configured.provider_price_card_sha256,
                ),
            )
            if configured is not None and configured.provider_proxy_url is not None
            else (),
            runtime_provider_bundle_bindings=(
                (
                    configured.provider_proxy_url.removesuffix("/v1"),
                    environment_value("CAPTAIN_PROVIDER_POLICY_SHA256"),
                    environment_value("CAPTAIN_PROVIDER_PRICE_CARD_SHA256"),
                    environment_value("CAPTAIN_PROVIDER_CONTEXT_SHA256"),
                    environment_value("CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256"),
                    environment_value("CAPTAIN_PROVIDER_PROXY_CODE_SHA256"),
                    environment_value("CAPTAIN_PROVIDER_PAID_COMMAND_SHA256"),
                    os.environ.get("CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED") == "true",
                ),
            )
            if configured is not None and configured.provider_proxy_url is not None
            else (),
        )
        if (
            configured is not None
            and configured.portal_n8n_adapters_configured
            and portal_credential_source is None
            and portal_verification_source is None
        ):
            bundle = build_portal_n8n_adapter_bundle(
                reader=created,
                settings=configured,
            )
            created.configure_portal_sources(
                credential_source=bundle.credential_source,
                verification_source=bundle.verification_source,
            )
        return created

    store: GatewayStore | None = gateway_store or (
        configured_store(storage, settings) if storage else None
    )
    portal_clock = portal_clock or (lambda: datetime.now(timezone.utc))
    selected_capability_assembly_factory = capability_assembly_composition_factory
    production_composition = None
    production_requested = selected_capability_assembly_factory is None and all(
        value is not None
        for value in (
            store,
            capability_assembly_environment,
            capability_assembly_factory_runtime,
            capability_assembly_private_catalog,
            capability_assembly_repository_root,
        )
    )
    if production_requested:
        def selected_production_composition():
            if production_composition is None:
                return unavailable_capability_assembly_composition()
            return production_composition
        selected_capability_assembly_factory = selected_production_composition
    resume_service = capability_resume_service
    runtime_http_client: httpx.AsyncClient | None = None
    resume_service_lock = Lock()
    portal_control_boot_id = str(uuid4())
    sink_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal production_composition
        load_gateway_settings(app)
        if production_requested:
            assert store is not None
            assert capability_assembly_environment is not None
            assert capability_assembly_factory_runtime is not None
            assert capability_assembly_private_catalog is not None
            assert capability_assembly_repository_root is not None
            production_composition = build_production_capability_assembly_from_environment(
                repository_root=capability_assembly_repository_root,
                environment=capability_assembly_environment,
                store=store,
                runtime=capability_assembly_factory_runtime,
                private_benchmark_catalog=capability_assembly_private_catalog,
                clock=portal_clock,
            )
        start = getattr(mirror, "start", None)
        if start:
            await start()
        try:
            yield
        finally:
            if runtime_http_client is not None:
                await runtime_http_client.aclose()
            if production_composition is not None:
                await close_production_capability_assembly(production_composition)
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
    app.include_router(
        create_production_capability_assembly_router(
            selected_capability_assembly_factory
            or unavailable_capability_assembly_composition,
            resolve_lazily=production_requested,
        )
    )

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
        if request.method == "POST" and path.startswith(
            "/v1/capability-resumes/"
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid capability resume request"},
            )
        if path.startswith("/v1/capability-assemblies"):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid capability assembly request"},
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
                    store = configured_store(MariaDBStorage(dsn), configured)
        return store

    def get_capability_resume_service() -> CapabilityResumeService:
        nonlocal resume_service, runtime_http_client
        if resume_service is None:
            with resume_service_lock:
                if resume_service is None:
                    gateway = get_store()
                    configured = load_gateway_settings(app)
                    selected_executor = capability_resume_executor
                    if selected_executor is None:
                        if (
                            configured.captain_runtime_url is None
                            or configured.captain_runtime_token is None
                        ):
                            raise CapabilityResumeUnavailable(
                                "Captain Runtime resume configuration is unavailable"
                            )
                        runtime_http_client = httpx.AsyncClient(
                            verify=configured.tls_ca_bundle_path or True,
                            timeout=30.0,
                        )
                        selected_executor = AgentRuntimeHttpExecutor(
                            base_url=configured.captain_runtime_url,
                            token=configured.captain_runtime_token.get_secret_value(),
                            client=runtime_http_client,
                        )
                    authority = GatewayCapabilityResumeAuthoritySource(
                        gateway,
                    )
                    resume_service = CapabilityResumeService(
                        repository=GatewayCapabilityResumeRepository(gateway),
                        authority_source=authority,
                        runtime_reader=authority,
                        executor=selected_executor,
                        provider_proxy_binding=(
                            RuntimeProviderProxyBindingV1(
                                schema="captain.runtime-provider-proxy-binding.v1",
                                responses_url=configured.provider_proxy_url,
                                policy_sha256=configured.provider_policy_sha256,
                                price_card_sha256=configured.provider_price_card_sha256,
                            )
                            if configured.provider_proxy_url is not None
                            else None
                        ),
                    )
        return resume_service

    app.state.get_capability_resume_service = get_capability_resume_service

    def translate_capability_resume_error(error: Exception) -> HTTPException:
        if isinstance(error, CapabilityResumeNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, CapabilityResumeConflict):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, CapabilityResumeUnavailable):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, CapabilityResumeDispatchFailed):
            return HTTPException(
                status_code=502,
                detail={
                    "code": "authoritative_runtime_failure",
                    "failure_id": str(error.failure.execution.execution_result_id),
                },
            )
        return HTTPException(status_code=503, detail="capability resume unavailable")

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

    @app.get("/v1/control/provider/health")
    async def provider_control_health(
        _: PortalControlRole = Depends(require_provider_control),
    ) -> dict[str, str]:
        return {
            "status": "ok",
            "service_version": "captain.portal-provider-control.v1",
            "boot_id": portal_control_boot_id,
        }

    @app.post("/v1/control/provider/probes")
    async def run_provider_control_probe(
        request: PortalProviderProbeRequestV1,
        _: PortalControlRole = Depends(require_provider_control),
    ) -> PortalProviderProbeCompletionV1:
        return await run_in_threadpool(
            get_store().run_portal_provider_probe,
            request,
            now=portal_clock(),
        )

    @app.post("/v1/control/provider/audit")
    async def query_provider_control_audit(
        request: PortalProviderAuditQueryV1,
        _: PortalControlRole = Depends(require_provider_control),
    ) -> PortalProviderAuditV1:
        return await run_in_threadpool(
            get_store().portal_provider_audit,
            request,
            observed_at=portal_clock(),
        )

    @app.post("/v1/control/restarts/{restart_id}/receipts")
    async def record_restart_control_receipt(
        restart_id: UUID,
        receipt: PortalRestartReceiptV1,
        _: PortalControlRole = Depends(require_restart_control),
    ) -> PortalRestartReceiptV1:
        if receipt.restart_id != restart_id:
            raise HTTPException(status_code=409, detail="restart_id must match route")
        return await run_in_threadpool(
            get_store().record_portal_restart_receipt,
            receipt,
        )

    @app.get("/v1/control/restarts/health")
    async def restart_control_health(
        _: PortalControlRole = Depends(require_restart_control),
    ) -> dict[str, str]:
        return {
            "status": "ok",
            "service_version": "captain.portal-restart-control.v1",
            "boot_id": portal_control_boot_id,
        }

    @app.post("/v1/control/portal-runs/{run_id}/decisions")
    async def finalize_portal_live_run(
        run_id: str,
        request: PortalLiveRunFinalizationV1,
        _: GatewayRole = Depends(require_captain),
    ) -> PortalLiveRunDecisionV1:
        if request.run_id != run_id:
            raise HTTPException(status_code=409, detail="run_id must match route")
        return await run_in_threadpool(
            get_store().finalize_portal_live_run,
            request,
        )

    @app.get("/v1/control/evidence/health")
    async def evidence_control_health(
        _: PortalControlRole = Depends(require_evidence_control),
    ) -> dict[str, str]:
        return {
            "status": "ok",
            "service_version": "captain.portal-evidence-control.v1",
            "boot_id": portal_control_boot_id,
        }

    @app.post("/v1/control/evidence/query")
    async def query_portal_live_evidence(
        query: PortalLiveEvidenceQueryV1,
        _: PortalControlRole = Depends(require_evidence_control),
    ) -> PortalLiveEvidenceV1:
        return await run_in_threadpool(
            get_store().portal_live_evidence,
            query,
        )

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
        persisted = await run_in_threadpool(
            get_store().portal_discover,
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
        persisted = await run_in_threadpool(
            get_store().portal_verify,
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

    @app.post("/api/v1/projections/minibook/rebuild-receipts")
    async def record_minibook_projection_rebuild(
        receipt: MinibookProjectionRebuildReceiptV1,
        _: GatewayRole = Depends(require_captain),
    ) -> MinibookProjectionRebuildReceiptV1:
        return await run_in_threadpool(
            get_store().record_minibook_projection_rebuild_receipt,
            receipt,
        )

    @app.post(
        "/v1/capability-resumes/authorizations",
        status_code=status.HTTP_201_CREATED,
    )
    async def authorize_capability_resume(
        request: CapabilityResumeAuthorizeHttpRequest,
        _: GatewayRole = Depends(require_captain),
    ) -> CapabilityResumeAuthorization:
        try:
            return await run_in_threadpool(
                get_capability_resume_service().authorize,
                request.to_domain(),
            )
        except (
            CapabilityResumeConflict,
            CapabilityResumeNotFound,
            CapabilityResumeUnavailable,
            CapabilityResumeDispatchFailed,
        ) as exc:
            raise translate_capability_resume_error(exc) from None

    @app.post(
        "/v1/capability-resumes/dispatches",
        status_code=status.HTTP_201_CREATED,
    )
    async def dispatch_capability_resume(
        authorization: CapabilityResumeAuthorizationHttpRequest,
        _: GatewayRole = Depends(require_captain),
    ) -> CapabilityExecutionAuthority:
        try:
            return await get_capability_resume_service().dispatch(
                authorization.to_domain()
            )
        except (
            CapabilityResumeConflict,
            CapabilityResumeNotFound,
            CapabilityResumeUnavailable,
            CapabilityResumeDispatchFailed,
        ) as exc:
            raise translate_capability_resume_error(exc) from None

    @app.post("/v1/capability-resumes/readbacks")
    async def readback_capability_resume(
        authorization: CapabilityResumeAuthorizationHttpRequest,
        _: GatewayRole = Depends(require_captain),
    ) -> CapabilityExecutionAuthority:
        try:
            return await run_in_threadpool(
                get_capability_resume_service().readback,
                authorization.to_domain(),
            )
        except (
            CapabilityResumeConflict,
            CapabilityResumeNotFound,
            CapabilityResumeUnavailable,
            CapabilityResumeDispatchFailed,
        ) as exc:
            raise translate_capability_resume_error(exc) from None

    @app.post("/v1/capability-resumes/cost-authorities/validate")
    async def validate_capability_resume_cost_authority(
        command: AgentRuntimeCommand,
        _: GatewayRole = Depends(require_captain),
    ) -> RuntimeResumeCostAuthorityV1:
        return await run_in_threadpool(
            get_store().validate_capability_resume_cost_authority,
            command,
            now=datetime.now(timezone.utc),
        )

    @app.post(
        "/v1/capability-resumes/provider-settlements",
        status_code=status.HTTP_201_CREATED,
    )
    async def record_runtime_provider_settlement(
        settlement: RuntimeProviderCostSettlementV1,
        response: Response,
        _: GatewayRole = Depends(require_provider_receipt_authority),
    ) -> RuntimeWriteReceipt:
        receipt = await run_in_threadpool(
            get_store().record_runtime_provider_settlement,
            settlement,
        )
        if receipt.replayed:
            response.status_code = status.HTTP_200_OK
        return receipt

    @app.post("/v1/capability-resumes/provider-authorities/preflight")
    async def preflight_runtime_provider_authority(
        context: ProviderBudgetContextV1,
        _: GatewayRole = Depends(require_provider_receipt_authority),
    ) -> ProviderBudgetContextV1:
        return await run_in_threadpool(
            get_store().validate_provider_proxy_authority,
            context,
            now=datetime.now(timezone.utc),
        )

    @app.get("/v1/capability-resumes/provider-authorities/by-context/{context_sha256}")
    async def resolve_runtime_provider_authority(
        context_sha256: str,
        _: GatewayRole = Depends(require_provider_receipt_authority),
    ) -> ProviderBudgetContextV1:
        return await run_in_threadpool(
            get_store().resolve_provider_proxy_authority,
            f"gateway://provider-budget-contexts/{context_sha256}",
            now=datetime.now(timezone.utc),
        )

    @app.post("/v1/capability-resumes/cost-authorities/settle")
    async def settle_capability_resume_cost(
        request: RuntimeResumeCostSettlementRequestV1,
        _: GatewayRole = Depends(require_captain),
    ) -> RuntimeResumeCostSettlementV1:
        return await run_in_threadpool(
            get_store().settle_capability_resume_cost,
            request,
            now=datetime.now(timezone.utc),
        )

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


def create_production_app_from_environment(
    *,
    environment: Mapping[str, str] | None = None,
    repository_root: FileSystemPath | None = None,
    runtime_builder: CapabilityAssemblyRuntimeBuilder = (
        build_capability_assembly_factory_runtime_from_environment
    ),
) -> FastAPI:
    """Build the shipped production app once after complete, pure validation."""

    source = dict(os.environ if environment is None else environment)
    root = (repository_root or FileSystemPath.cwd()).resolve(strict=True)
    configured = GatewaySettings.from_env(source)
    bootstrap = load_capability_assembly_bootstrap_settings(
        environment=source,
        repository_root=root,
    )
    clock = lambda: datetime.now(timezone.utc)
    artifacts = ContentAddressedArtifactAdapter(bootstrap.artifact_root)
    gateway = GatewayStore(
        MariaDBStorage(configured.ledger_dsn.get_secret_value()),
        claim_ttl=timedelta(seconds=configured.claim_ttl_seconds),
        runtime_usage_artifacts=artifacts,
        runtime_provider_proxy_bindings=((
            configured.provider_proxy_url,
            configured.provider_policy_sha256,
            configured.provider_price_card_sha256,
        ),) if configured.provider_proxy_url is not None else (),
        runtime_provider_bundle_bindings=((
            bootstrap.provider_url,
            bootstrap.provider_policy_sha256,
            bootstrap.provider_price_card_sha256,
            bootstrap.provider_context_sha256,
            bootstrap.provider_proxy_executable_sha256,
            bootstrap.provider_proxy_code_sha256,
            bootstrap.provider_paid_command_sha256,
            bootstrap.provider_hard_ceiling_enforced,
        ),),
    )
    private_catalog = GatewayPrivateHoldoutStore(gateway_store=gateway)
    runtime = runtime_builder(
        repository_root=root,
        environment=source,
        store=gateway,
        private_benchmark_catalog=private_catalog,
        settings=bootstrap,
        clock=clock,
    )
    return create_app(
        settings=configured,
        gateway_store=gateway,
        capability_assembly_environment=source,
        capability_assembly_factory_runtime=runtime,
        capability_assembly_private_catalog=private_catalog,
        capability_assembly_repository_root=root,
        runtime_usage_artifacts=artifacts,
        portal_clock=clock,
    )


app = create_app()


def main() -> None:
    import uvicorn

    production_app = create_production_app_from_environment()
    settings = production_app.state.gateway_settings
    uvicorn.run(production_app, host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
