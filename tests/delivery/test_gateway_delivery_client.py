from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

from agenten.delivery.gateway_client import (
    GatewayDeliveryClient,
    GatewayDeliveryConflictError,
    GatewayDeliveryError,
)


def projection(*, iteration: int = 1) -> dict[str, object]:
    return {
        "batch_id": "batch-1",
        "parent_index": 41,
        "status": "claimed",
        "claim_token_sha256": "a" * 64,
        "claim_expires_at": "2026-07-16T12:00:00Z",
        "claim_iteration": iteration,
        "codex_session_recorded": False,
        "validation_run_recorded": False,
    }


@pytest.mark.asyncio
async def test_claim_returns_typed_token_and_current_iteration() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"claim_token": "claim-secret", "claim_expires_at": "2026-07-16T12:00:00Z"},
                request=request,
            )
        return httpx.Response(200, json=projection(iteration=3), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("http://gateway/", "worker-secret", http)
        claim = await client.claim("batch-1")

    assert claim.token == "claim-secret"
    assert claim.iteration == 3
    assert claim.expires_at == datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    assert [request.url.path for request in requests] == [
        "/batches/batch-1/claim",
        "/batches/batch-1",
    ]
    assert all(request.headers["authorization"] == "Bearer worker-secret" for request in requests)
    assert all("x-claim-token" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_fenced_writes_send_claim_token_and_typed_payloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("heartbeat"):
            return httpx.Response(
                200,
                json={"claim_expires_at": "2026-07-16T12:30:00Z"},
                request=request,
            )
        return httpx.Response(201, json={"index": 42 + len(requests)}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("http://gateway", "worker-secret", http)
        expiry = await client.heartbeat("batch-1", "claim-secret")
        await client.record_codex_session(
            "batch-1", "claim-secret", iteration=2, session_id="thread-123"
        )
        await client.record_validation(
            "batch-1", "claim-secret", iteration=2, artifact_ref="artifact://run/1"
        )
        await client.complete("batch-1", "claim-secret", outcome="succeeded")

    assert expiry == datetime(2026, 7, 16, 12, 30, tzinfo=timezone.utc)
    assert all(request.headers["x-claim-token"] == "claim-secret" for request in requests)
    payloads = [json.loads(request.content) for request in requests[1:]]
    assert [payload["block_type"] for payload in payloads] == [
        "codex_session",
        "validation_run",
        "batch_done",
    ]
    assert payloads[0]["data"] == {
        "batch_id": "batch-1",
        "iteration": 2,
        "session_id": "thread-123",
    }
    assert payloads[1]["data"]["artifact_ref"] == "artifact://run/1"
    assert payloads[2]["status"] == "succeeded"
    assert payloads[2]["data"] == {"batch_id": "batch-1", "outcome": "succeeded"}


@pytest.mark.asyncio
async def test_delivery_conflict_and_transport_errors_are_sanitized() -> None:
    def conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "stale claim"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(conflict)) as http:
        client = GatewayDeliveryClient("http://gateway", "worker-secret", http)
        with pytest.raises(GatewayDeliveryConflictError) as failure:
            await client.heartbeat("batch-1", "claim-secret")

    message = str(failure.value)
    assert "409" in message
    assert "worker-secret" not in message
    assert "claim-secret" not in message

    def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"claim_expires_at": "not-a-date"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid)) as http:
        client = GatewayDeliveryClient("http://gateway", "worker-secret", http)
        with pytest.raises(GatewayDeliveryError):
            await client.heartbeat("batch-1", "claim-secret")
