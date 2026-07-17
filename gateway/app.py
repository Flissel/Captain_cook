"""FastAPI sole-writer gateway over the transactional MariaDB ledger."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agenten.validation.contracts import HoldoutSuite, WorkBatch
from blockchain.mariadb_storage import MariaDBStorage
from gateway.mirror import MirrorQueue
from gateway.registry_feed import mirror_validated_batch


logger = logging.getLogger(__name__)
CAPTAIN_BLOCK_TYPES = {"problem", "work_batch", "holdout"}
TERMINAL_STATUSES = {"succeeded", "failed", "rejected", "cancelled"}


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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def canonical_captain_data_matches(
    existing: dict[str, Any],
    replay: dict[str, Any],
) -> bool:
    """Compare validated Captain payloads by canonical JSON content."""

    return json.dumps(existing, sort_keys=True, separators=(",", ":")) == json.dumps(
        replay,
        sort_keys=True,
        separators=(",", ":"),
    )


class GatewayStore:
    def __init__(self, storage: MariaDBStorage):
        self.storage = storage
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ledger_state (
                        id TINYINT NOT NULL PRIMARY KEY,
                        next_block_index BIGINT NOT NULL
                    ) ENGINE=InnoDB
                    """
                )
                cursor.execute("INSERT IGNORE INTO ledger_state (id, next_block_index) VALUES (1, 0)")
                cursor.execute("SELECT COALESCE(MAX(`index`) + 1, 0) AS next_index FROM blocks")
                next_index = cursor.fetchone()["next_index"]
                cursor.execute(
                    "UPDATE ledger_state SET next_block_index = GREATEST(next_block_index, %s) WHERE id = 1",
                    (next_index,),
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS validated_capabilities (
                        batch_id VARCHAR(32) NOT NULL PRIMARY KEY,
                        descriptor TEXT NOT NULL,
                        artifact_ref TEXT NULL,
                        block_index BIGINT NOT NULL,
                        payload JSON NOT NULL,
                        created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                        FULLTEXT INDEX idx_capability_descriptor (descriptor),
                        CONSTRAINT fk_capability_block
                            FOREIGN KEY (block_index) REFERENCES blocks (`index`) ON DELETE CASCADE
                    ) ENGINE=InnoDB
                    """
                )

    @staticmethod
    def _batch_row(cursor: Any, batch_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, data, status, metadata
            FROM blocks
            WHERE block_type = 'work_batch'
              AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
            ORDER BY `index` DESC LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (batch_id,))
        row = cursor.fetchone()
        if row:
            row["data"] = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        return row

    @staticmethod
    def _captain_block_row(
        cursor: Any,
        block_type: str,
        batch_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
            WHERE block_type = %s
              AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
            ORDER BY `index` DESC LIMIT 1
        """
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (block_type, batch_id))
        row = cursor.fetchone()
        if row:
            for field in ("data", "children", "metadata"):
                if isinstance(row[field], str):
                    row[field] = json.loads(row[field])
        return row

    @staticmethod
    def _next_index(cursor: Any) -> int:
        cursor.execute("SELECT next_block_index FROM ledger_state WHERE id = 1 FOR UPDATE")
        index = int(cursor.fetchone()["next_block_index"])
        cursor.execute("UPDATE ledger_state SET next_block_index = next_block_index + 1 WHERE id = 1")
        return index

    @staticmethod
    def _insert(cursor: Any, block: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO blocks
                (`index`, parent_index, block_type, data, status, children,
                 metadata, hash, previous_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                block["index"], block["parent_index"], block["block_type"],
                json.dumps(block["data"], sort_keys=True), block["status"], "[]",
                json.dumps(block["metadata"], sort_keys=True), block["hash"], block["previous_hash"],
            ),
        )

    def append(self, request: BlockRequest, claim_token: str | None) -> dict[str, Any]:
        try:
            if request.block_type == "work_batch":
                request = request.model_copy(
                    update={"data": WorkBatch.model_validate(request.data).model_dump(mode="json")}
                )
            elif request.block_type == "holdout":
                request = request.model_copy(
                    update={"data": HoldoutSuite.model_validate(request.data).model_dump(mode="json")}
                )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        batch_id = request.data.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise HTTPException(status_code=422, detail="data.batch_id is required")
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                parent_index = request.parent_index
                if request.block_type not in CAPTAIN_BLOCK_TYPES:
                    batch = self._batch_row(cursor, batch_id, for_update=True)
                    self._assert_live_claim(batch, claim_token)
                    if parent_index is None:
                        parent_index = batch["index"]
                if request.block_type == "work_batch":
                    existing_batch = self._captain_block_row(
                        cursor,
                        "work_batch",
                        batch_id,
                        for_update=True,
                    )
                    if existing_batch is not None and canonical_captain_data_matches(
                        existing_batch["data"],
                        request.data,
                    ):
                        return existing_batch
                    if existing_batch is not None:
                        raise HTTPException(status_code=409, detail="batch_id already exists")
                if request.block_type == "holdout" and parent_index is None:
                    raise HTTPException(status_code=422, detail="holdout requires its work_batch parent")
                if parent_index is not None and request.block_type != "work_batch":
                    cursor.execute("SELECT data FROM blocks WHERE `index`=%s", (parent_index,))
                    parent = cursor.fetchone()
                    if parent is None:
                        raise HTTPException(status_code=404, detail="parent block not found")
                    parent_data = json.loads(parent["data"]) if isinstance(parent["data"], str) else parent["data"]
                    if parent_data.get("batch_id") != batch_id:
                        raise HTTPException(status_code=409, detail="parent belongs to another batch")
                if request.block_type == "holdout":
                    existing_holdout = self._captain_block_row(
                        cursor,
                        "holdout",
                        batch_id,
                        for_update=True,
                    )
                    if (
                        existing_holdout is not None
                        and existing_holdout["parent_index"] == parent_index
                        and canonical_captain_data_matches(
                            existing_holdout["data"],
                            request.data,
                        )
                    ):
                        return existing_holdout
                    if existing_holdout is not None:
                        raise HTTPException(
                            status_code=409,
                            detail="holdout suite already exists",
                        )
                index = self._next_index(cursor)
                cursor.execute("SELECT hash FROM blocks ORDER BY `index` DESC LIMIT 1")
                previous = cursor.fetchone()
                from blockchain.Blockchain_modell import Block

                model = Block(
                    index=index,
                    block_type=request.block_type,
                    data=request.data,
                    status=request.status,
                    previous_hash=previous["hash"] if previous else "0",
                    parent_index=parent_index,
                    metadata=request.metadata,
                )
                block = model.to_dict()
                self._insert(cursor, block)
                if parent_index is not None:
                    cursor.execute(
                        "UPDATE blocks SET children = JSON_ARRAY_APPEND(children, '$', %s) WHERE `index` = %s",
                        (index, parent_index),
                    )
                if request.block_type == "batch_done":
                    outcome = str(request.data.get("outcome", request.status))
                    if outcome not in TERMINAL_STATUSES:
                        raise HTTPException(status_code=422, detail="batch_done requires a terminal outcome")
                    if request.status != outcome:
                        raise HTTPException(status_code=422, detail="batch_done status must match outcome")
                    cursor.execute("UPDATE blocks SET status = %s WHERE `index` = %s", (outcome, parent_index))
                    if outcome == "succeeded":
                        self._upsert_capability(cursor, block)
        return block

    @staticmethod
    def _assert_live_claim(batch: dict[str, Any] | None, token: str | None) -> None:
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        if batch["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="batch is terminal")
        metadata = batch["metadata"]
        expires = _parse_time(metadata.get("claim_expires_at"))
        presented_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
        if (
            batch["status"] != "claimed"
            or not token
            or not secrets.compare_digest(str(metadata.get("claim_token_hash", "")), presented_hash)
            or expires is None
            or expires <= _utcnow()
        ):
            raise HTTPException(status_code=409, detail="invalid or expired claim token")

    @staticmethod
    def _upsert_capability(cursor: Any, block: dict[str, Any]) -> None:
        data = block["data"]
        capabilities = data.get("capabilities", [])
        batch = GatewayStore._batch_row(cursor, data["batch_id"])
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        payload = dict(data)
        payload["target"] = batch["data"]["target"]
        descriptor = " ".join(
            [payload["target"], *(str(value) for value in capabilities)]
        )
        cursor.execute(
            """
            INSERT INTO validated_capabilities
                (batch_id, descriptor, artifact_ref, block_index, payload)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE descriptor=VALUES(descriptor),
                artifact_ref=VALUES(artifact_ref), block_index=VALUES(block_index), payload=VALUES(payload)
            """,
            (
                data["batch_id"], descriptor, data.get("artifact_ref"), block["index"],
                json.dumps(payload, sort_keys=True),
            ),
        )

    def claim(self, batch_id: str) -> dict[str, str]:
        now = _utcnow()
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id, for_update=True)
                if batch is None:
                    raise HTTPException(status_code=404, detail="batch not found")
                old_expiry = _parse_time(batch["metadata"].get("claim_expires_at"))
                reclaimable = batch["status"] == "claimed" and old_expiry is not None and old_expiry <= now
                if batch["status"] != "pending" and not reclaimable:
                    raise HTTPException(status_code=409, detail="batch is not claimable")
                token = secrets.token_urlsafe(32)
                expiry = now + timedelta(minutes=90)
                metadata = dict(batch["metadata"])
                metadata.update(
                    claim_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    claim_expires_at=expiry.isoformat(),
                    claim_iteration=int(metadata.get("claim_iteration", 0)) + 1,
                )
                cursor.execute(
                    "UPDATE blocks SET status='claimed', metadata=%s WHERE `index`=%s",
                    (json.dumps(metadata, sort_keys=True), batch["index"]),
                )
        return {"claim_token": token, "claim_expires_at": expiry.isoformat()}

    def heartbeat(self, batch_id: str, token: str | None) -> dict[str, str]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id, for_update=True)
                self._assert_live_claim(batch, token)
                expiry = _utcnow() + timedelta(minutes=30)
                metadata = dict(batch["metadata"])
                metadata["claim_expires_at"] = expiry.isoformat()
                cursor.execute(
                    "UPDATE blocks SET metadata=%s WHERE `index`=%s",
                    (json.dumps(metadata, sort_keys=True), batch["index"]),
                )
        return {"claim_expires_at": expiry.isoformat()}

    def approve(self, batch_id: str) -> None:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id, for_update=True)
                if batch is None:
                    raise HTTPException(status_code=404, detail="batch not found")
                if batch["status"] != "pending_review":
                    raise HTTPException(status_code=409, detail="batch is not pending review")
                cursor.execute("UPDATE blocks SET status='pending' WHERE `index`=%s", (batch["index"],))

    def list_batches(self, requested_status: str) -> list[dict[str, str]]:
        rows = self.storage.load_by_status(requested_status)
        return [
            {"batch_id": row["data"]["batch_id"], "title": str(row["data"].get("title", ""))}
            for row in rows if row["block_type"] == "work_batch"
        ]

    def bundle(self, batch_id: str) -> dict[str, Any]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return {key: value for key, value in batch["data"].items() if "holdout" not in key.lower()}

    def blocks(self, batch_id: str, *, include_holdout: bool = False) -> list[dict[str, Any]]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT `index`, parent_index, block_type, data, status, children,
                           metadata, hash, previous_hash
                    FROM blocks
                    WHERE JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id')) = %s
                    ORDER BY `index`
                    """,
                    (batch_id,),
                )
                rows = cursor.fetchall()
        decoded = [self.storage._decode_row(row) for row in rows]
        for row in decoded:
            row["metadata"] = {
                key: value for key, value in row["metadata"].items()
                if not key.startswith("claim_")
            }
        return decoded if include_holdout else [row for row in decoded if row["block_type"] != "holdout"]

    def holdout(self, batch_id: str, token: str | None) -> dict[str, Any]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                batch = self._batch_row(cursor, batch_id)
                self._assert_live_claim(batch, token)
                iteration = int(batch["metadata"].get("claim_iteration", 0))
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count FROM blocks
                    WHERE block_type='codex_session'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id'))=%s
                      AND CAST(JSON_UNQUOTE(JSON_EXTRACT(data, '$.iteration')) AS UNSIGNED)=%s
                    """,
                    (batch_id, iteration),
                )
                if cursor.fetchone()["count"] == 0:
                    raise HTTPException(status_code=404, detail="holdout not released")
                cursor.execute(
                    """
                    SELECT data FROM blocks WHERE block_type='holdout'
                      AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.batch_id'))=%s
                    ORDER BY `index` DESC LIMIT 1
                    """,
                    (batch_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="holdout not found")
        return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]

    def capabilities(self, need: str) -> list[dict[str, Any]]:
        with self.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT batch_id, artifact_ref, payload FROM validated_capabilities
                    WHERE MATCH(descriptor) AGAINST (%s IN NATURAL LANGUAGE MODE)
                    ORDER BY MATCH(descriptor) AGAINST (%s IN NATURAL LANGUAGE MODE) DESC
                    """,
                    (need, need),
                )
                rows = cursor.fetchall()
        return [
            {
                "batch_id": row["batch_id"],
                "artifact_ref": row["artifact_ref"],
                "data": json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
            }
            for row in rows
        ]


def create_app(
    *,
    storage: MariaDBStorage | None = None,
    mirror: Mirror | None = None,
    approval_enabled: bool | None = None,
) -> FastAPI:
    mirror = mirror or MirrorQueue(mirror_validated_batch)
    approval_enabled = (
        os.getenv("GATEWAY_APPROVAL_ENABLED", "false").lower() == "true"
        if approval_enabled is None else approval_enabled
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

    @app.post("/blocks", status_code=status.HTTP_201_CREATED)
    async def add_block(request: BlockRequest, x_claim_token: str | None = Header(default=None)) -> dict[str, Any]:
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
    async def get_holdout(batch_id: str, x_claim_token: str | None = Header(default=None)) -> dict[str, Any]:
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
