from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

from agenten.delivery.gateway_client import (
    GatewayDeliveryClient,
    GatewayDeliveryConflictError,
    GatewayDeliveryError,
    GatewayEvidence,
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
        await client.record_codex_process(
            "batch-1", "claim-secret", iteration=2, process_id="process-1",
            state="started", command_digest="a" * 64,
        )
        await client.record_validation(
            "batch-1", "claim-secret", iteration=2, artifact_ref="artifact://run/1"
        )
        await client.complete(
            "batch-1",
            "claim-secret",
            outcome="succeeded",
            capabilities=["crm", "delivery"],
            artifact_ref="artifact://validated/1",
            target="n8n",
            runtime="n8n",
            runtime_version="v1",
            interface_schema="captain-n8n-artifact/v1",
        )

    assert expiry == datetime(2026, 7, 16, 12, 30, tzinfo=timezone.utc)
    assert all(request.headers["x-claim-token"] == "claim-secret" for request in requests)
    payloads = [json.loads(request.content) for request in requests[1:]]
    assert [payload["block_type"] for payload in payloads] == [
        "codex_session",
        "codex_process",
        "validation_run",
        "batch_done",
    ]
    assert payloads[0]["data"] == {
        "batch_id": "batch-1",
        "iteration": 2,
        "session_id": "thread-123",
    }
    assert payloads[1]["data"] == {
        "batch_id": "batch-1", "iteration": 2, "process_id": "process-1",
        "state": "started", "command_digest": "a" * 64,
    }
    assert payloads[2]["data"]["artifact_ref"] == "artifact://run/1"
    assert payloads[3]["status"] == "succeeded"
    assert payloads[3]["data"] == {
        "batch_id": "batch-1",
        "outcome": "succeeded",
        "capabilities": ["crm", "delivery"],
        "artifact_ref": "artifact://validated/1",
        "target": "n8n",
        "runtime": "n8n",
        "runtime_version": "v1",
        "interface_schema": "captain-n8n-artifact/v1",
    }


@pytest.mark.asyncio
async def test_append_evidence_accepts_only_typed_lifecycle_evidence() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(201, json={"index": 43}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("http://gateway", "worker-secret", http)
        await client.append_evidence(
            "batch-1",
            "claim-secret",
            GatewayEvidence(
                kind="codex_session",
                iteration=2,
                session_id="thread-123",
            ),
        )

    assert captured == [
        {
            "block_type": "codex_session",
            "data": {
                "batch_id": "batch-1",
                "iteration": 2,
                "session_id": "thread-123",
            },
            "status": "recorded",
        }
    ]

    with pytest.raises(ValueError, match="session_id"):
        GatewayEvidence(kind="codex_session", iteration=2)
    with pytest.raises(ValueError, match="artifact_ref"):
        GatewayEvidence(kind="validation_run", iteration=2)


@pytest.mark.asyncio
async def test_validated_artifact_requires_complete_compatibility_metadata() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as http:
        client = GatewayDeliveryClient("http://gateway", "worker-secret", http)
        with pytest.raises(ValueError, match="compatibility metadata"):
            await client.complete(
                "batch-1",
                "claim-secret",
                outcome="succeeded",
                capabilities=["delivery"],
                artifact_ref="artifact://validated/1",
            )


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
