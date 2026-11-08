"""Captain can cancel a batch; the claim holder learns it from every write."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agenten.delivery.gateway_client import (
    GatewayBatchCancelledError,
    GatewayDeliveryClient,
    GatewayDeliveryConflictError,
)
from gateway.contracts import (
    BATCH_CANCELLED_BLOCK_TYPE,
    REVIEW_MARKER_BLOCK_TYPE,
    BatchCancelEvent,
    BatchCancelRequest,
    project_batch,
)
from gateway.store import GatewayStore, require_generic_block_type

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
CLAIM_TOKEN = "claim-secret"


def work_batch(status: str = "pending") -> dict[str, Any]:
    return {
        "index": 10,
        "parent_index": None,
        "block_type": "work_batch",
        "data": {"batch_id": "batch-1"},
        "status": status,
    }


def child(index: int, block_type: str, **data: Any) -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": 10,
        "block_type": block_type,
        "data": {"batch_id": "batch-1", **data},
        "status": "recorded",
    }


def claimed(index: int = 11, *, expires: datetime | None = None) -> dict[str, Any]:
    return child(
        index,
        "batch_claimed",
        claim_id="claim-1",
        fencing_token=1,
        claim_token_sha256=hashlib.sha256(CLAIM_TOKEN.encode()).hexdigest(),
        claim_expires_at=(expires or NOW + timedelta(minutes=30)).isoformat(),
        agent_id="openclaw",
    )


def cancelled(index: int, *, claim_iteration: int, reason: str = "captain_cancelled") -> dict[str, Any]:
    return child(
        index,
        BATCH_CANCELLED_BLOCK_TYPE,
        reason=reason,
        claim_iteration=claim_iteration,
    )


# --- contract -------------------------------------------------------------


def test_cancel_request_accepts_only_known_reasons() -> None:
    assert BatchCancelRequest(reason="operator_cancelled").reason == "operator_cancelled"
    with pytest.raises(ValidationError):
        BatchCancelRequest.model_validate({"reason": "because"})
    with pytest.raises(ValidationError):
        BatchCancelRequest.model_validate({"reason": "captain_cancelled", "extra": 1})


def test_cancel_event_is_gateway_owned() -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_generic_block_type(BATCH_CANCELLED_BLOCK_TYPE)
    assert excinfo.value.status_code == 422


# --- projection -----------------------------------------------------------


def test_unclaimed_batch_can_be_cancelled() -> None:
    projection = project_batch([work_batch(), cancelled(11, claim_iteration=0)], "batch-1", now=NOW)

    assert projection.status == "cancelled"
    assert projection.claim_iteration == 0


def test_batch_awaiting_review_can_be_cancelled() -> None:
    blocks = [
        work_batch("pending_review"),
        child(11, REVIEW_MARKER_BLOCK_TYPE),
        cancelled(12, claim_iteration=0),
    ]

    assert project_batch(blocks, "batch-1", now=NOW).status == "cancelled"


def test_cancelling_a_live_claim_revokes_it() -> None:
    projection = project_batch(
        [work_batch(), claimed(), cancelled(12, claim_iteration=1)], "batch-1", now=NOW
    )

    assert projection.status == "cancelled"
    assert projection.claim_iteration == 1
    assert projection.claim_token_sha256 is None
    assert projection.claim_id is None
    assert projection.fencing_token is None
    assert projection.claim_expires_at is None
    assert projection.claim_agent_id is None


def test_expired_claim_can_be_cancelled_instead_of_recovered() -> None:
    blocks = [
        work_batch(),
        claimed(expires=NOW - timedelta(minutes=1)),
        cancelled(12, claim_iteration=1),
    ]

    assert project_batch(blocks, "batch-1", now=NOW).status == "cancelled"


def test_cancel_must_name_the_current_claim_iteration() -> None:
    with pytest.raises(ValueError, match="current claim iteration"):
        project_batch([work_batch(), claimed(), cancelled(12, claim_iteration=0)], "batch-1", now=NOW)


@pytest.mark.parametrize(
    "after",
    [
        child(12, "batch_heartbeat", claim_expires_at=(NOW + timedelta(hours=1)).isoformat()),
        cancelled(12, claim_iteration=1),
        child(12, "codex_session", iteration=1, session_id="session-1"),
    ],
)
def test_cancel_is_terminal(after: dict[str, Any]) -> None:
    blocks = [work_batch(), claimed(), cancelled(12, claim_iteration=1)]
    after = {**after, "index": 13}

    with pytest.raises(ValueError, match="terminal"):
        project_batch([*blocks, after], "batch-1", now=NOW)


def test_finished_batch_cannot_be_cancelled() -> None:
    blocks = [
        work_batch(),
        claimed(),
        child(12, "batch_done", outcome="failed"),
        cancelled(13, claim_iteration=1),
    ]

    with pytest.raises(ValueError, match="terminal"):
        project_batch(blocks, "batch-1", now=NOW)


# --- signalling the claim holder ------------------------------------------


def test_former_holder_is_told_the_batch_was_cancelled() -> None:
    projection = project_batch(
        [work_batch(), claimed(), cancelled(12, claim_iteration=1)], "batch-1", now=NOW
    )

    with pytest.raises(HTTPException) as excinfo:
        GatewayStore._assert_live_claim(projection, CLAIM_TOKEN, now=NOW, agent_id="openclaw")

    assert excinfo.value.status_code == 410
    assert excinfo.value.detail == "batch cancelled"


@pytest.mark.asyncio
async def test_client_raises_a_dedicated_cancel_error_on_410() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"detail": "batch cancelled"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "worker-secret", http)
        with pytest.raises(GatewayBatchCancelledError) as excinfo:
            await client.heartbeat("batch-1", CLAIM_TOKEN)

    # Callers that already stop on lifecycle conflicts keep working.
    assert isinstance(excinfo.value, GatewayDeliveryConflictError)


@pytest.mark.asyncio
async def test_client_cancels_with_the_captain_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        event = BatchCancelEvent(batch_id="batch-1", reason="captain_cancelled", claim_iteration=1)
        return httpx.Response(201, json=event.model_dump(mode="json"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "captain-secret", http)
        await client.cancel_batch("batch-1", reason="captain_cancelled")

    assert seen[0].method == "POST"
    assert seen[0].url.path == "/batches/batch-1/cancel"
    assert json.loads(seen[0].content) == {"reason": "captain_cancelled"}


# --- route authority ------------------------------------------------------


def test_only_captain_may_cancel_and_replays_answer_200() -> None:
    from fastapi.testclient import TestClient

    from gateway.app import create_app
    from gateway.settings import GatewaySettings

    calls: list[tuple[str, str]] = []

    class Store:
        def cancel(self, batch_id: str, reason: str) -> tuple[dict[str, Any], bool]:
            calls.append((batch_id, reason))
            block = cancelled(12, claim_iteration=1, reason=reason)
            return block, len(calls) > 1

    settings = GatewaySettings.model_validate(
        {
            "ledger_dsn": "mysql://ignored:ignored@127.0.0.1:3306/ignored",
            "captain_gateway_token": "captain-cancel-token",
            "worker_gateway_token": "worker-cancel-token",
        }
    )
    client = TestClient(create_app(settings=settings, gateway_store=Store()))
    body = {"reason": "captain_cancelled"}

    def post(token: str) -> httpx.Response:
        return client.post(
            "/batches/batch-1/cancel", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert post("worker-cancel-token").status_code == 403
    assert calls == []
    first = post("captain-cancel-token")
    assert first.status_code == 201
    assert first.json() == {"batch_id": "batch-1", "reason": "captain_cancelled", "claim_iteration": 1}
    assert post("captain-cancel-token").status_code == 200
    assert calls == [("batch-1", "captain_cancelled")] * 2
