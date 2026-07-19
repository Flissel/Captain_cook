"""FastAPI sole-writer gateway over the transactional MariaDB ledger."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from threading import Lock
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    CapabilityGrantRevocation,
)
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
)
from gateway.mirror import MirrorQueue
from gateway.registry_feed import mirror_validated_batch
from gateway.settings import GatewaySettings
from gateway.store import AppendResult, GatewayStore


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


def create_app(
    *,
    storage: MariaDBStorage | None = None,
    mirror: Mirror | None = None,
    settings: GatewaySettings | None = None,
) -> FastAPI:
    mirror = mirror or MirrorQueue(mirror_validated_batch)
    store_lock = Lock()
    store: GatewayStore | None = (
        GatewayStore(
            storage,
            claim_ttl=timedelta(seconds=settings.claim_ttl_seconds),
        )
        if storage and settings
        else GatewayStore(storage)
        if storage
        else None
    )
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

    @app.exception_handler(RequestValidationError)
    async def sanitized_review_validation_error(
        request: Request,
        exc: RequestValidationError,
    ):
        path = request.url.path
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
        result = get_store().append_delivery_event(
            event,
            require_current_claim=actor is GatewayRole.WORKER,
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
