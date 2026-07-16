"""FastAPI sole-writer gateway over the transactional MariaDB ledger."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from blockchain.mariadb_storage import MariaDBStorage
from gateway.contracts import BatchProjection
from gateway.mirror import MirrorQueue
from gateway.registry_feed import mirror_validated_batch
from gateway.store import GatewayStore


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


def create_app(
    *,
    storage: MariaDBStorage | None = None,
    mirror: Mirror | None = None,
    approval_enabled: bool | None = None,
) -> FastAPI:
    mirror = mirror or MirrorQueue(mirror_validated_batch)
    approval_enabled = (
        os.getenv("GATEWAY_APPROVAL_ENABLED", "false").lower() == "true"
        if approval_enabled is None
        else approval_enabled
    )
    store_lock = Lock()
    store: GatewayStore | None = GatewayStore(storage) if storage else None
    sink_calls: list[dict[str, Any]] = []

    def get_store() -> GatewayStore:
        nonlocal store
        if store is None:
            with store_lock:
                if store is None:
                    dsn = os.getenv("LEDGER_DSN")
                    if not dsn:
                        raise HTTPException(status_code=503, detail="LEDGER_DSN is not configured")
                    store = GatewayStore(MariaDBStorage(dsn))
        return store

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        start = getattr(mirror, "start", None)
        if start:
            await start()
        try:
            yield
        finally:
            stop = getattr(mirror, "stop", None)
            if stop:
                await stop()

    app = FastAPI(title="Captain Cook Ledger Gateway", lifespan=lifespan)

    @app.get("/batches")
    async def list_batches(status_filter: str = Query(alias="status")) -> list[dict[str, str]]:
        return get_store().list_batches(status_filter)

    @app.post("/batches/{batch_id}/claim")
    async def claim_batch(batch_id: str) -> dict[str, str]:
        return get_store().claim(batch_id)

    @app.post("/batches/{batch_id}/claim/heartbeat")
    async def heartbeat(batch_id: str, x_claim_token: str | None = Header(default=None)) -> dict[str, str]:
        return get_store().heartbeat(batch_id, x_claim_token)

    @app.post("/batches/{batch_id}/approve")
    async def approve(batch_id: str) -> dict[str, str]:
        if not approval_enabled:
            raise HTTPException(status_code=404, detail="approval endpoint disabled")
        get_store().approve(batch_id)
        return {"status": "pending"}

    @app.get("/batches/{batch_id}")
    async def get_batch(batch_id: str) -> BatchProjection:
        return get_store().batch_projection(batch_id)

    @app.post("/blocks", status_code=status.HTTP_201_CREATED)
    async def add_block(
        request: BlockRequest,
        x_claim_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        block = get_store().append(request, x_claim_token)
        try:
            mirror.enqueue_nowait(block)
        except Exception:
            logger.exception("Could not enqueue block %s for Minibook mirroring", block["index"])
        return block

    @app.get("/batches/{batch_id}/bundle")
    async def get_bundle(batch_id: str) -> dict[str, Any]:
        return get_store().bundle(batch_id)

    @app.get("/batches/{batch_id}/blocks")
    async def get_blocks(batch_id: str) -> list[dict[str, Any]]:
        return get_store().blocks(batch_id)

    @app.get("/batches/{batch_id}/holdout")
    async def get_holdout(
        batch_id: str,
        x_claim_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return get_store().holdout(batch_id, x_claim_token)

    @app.post("/sink/crm", status_code=status.HTTP_201_CREATED)
    async def write_sink(call: SinkCall) -> dict[str, Any]:
        payload = call.model_dump()
        sink_calls.append(payload)
        return payload

    @app.get("/sink/crm")
    async def read_sink(case_id: str) -> list[dict[str, Any]]:
        return [call for call in sink_calls if call["case_id"] == case_id]

    @app.get("/capabilities")
    async def capabilities(need: str = Query(min_length=1)) -> list[dict[str, Any]]:
        return get_store().capabilities(need)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("gateway.app:app", host="0.0.0.0", port=8090, workers=1)


if __name__ == "__main__":
    main()
