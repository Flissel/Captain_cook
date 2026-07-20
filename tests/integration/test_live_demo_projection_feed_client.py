from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest

from agenten.delivery.projection_feed_client import GatewayProjectionFeedClient


CORRELATION_ID = UUID("4d53b3a5-252d-4b67-bd4d-3168df61b46a")


@pytest.mark.asyncio
async def test_feed_client_reads_all_pages_and_selects_exact_correlation() -> None:
    requests: list[httpx.Request] = []

    def event(correlation_id: UUID) -> dict[str, object]:
        return {
            "schema": "captain.minibook-projection.v2",
            "event_id": str(uuid4()),
            "correlation_id": str(correlation_id),
            "causation_id": str(uuid4()),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "producer": "captain-gateway",
            "subject_id": f"subject:{uuid4()}",
            "subject_version": 1,
            "event_type": "codex.result",
            "payload": {
                "view": "build",
                "template_id": "runtime_build_recorded",
                "status_id": "built",
            },
        }

    first = event(uuid4())
    second = event(CORRELATION_ID)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("cursor") is None:
            body = {"events": [first], "cursor": "page-1", "has_more": True}
        else:
            body = {"events": [second], "cursor": "page-2", "has_more": False}
        return httpx.Response(200, json=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        observed = await GatewayProjectionFeedClient(
            "https://gateway.test/", "gateway-secret", http, page_size=1
        ).events_for_correlation(CORRELATION_ID)

    assert [item.event_id for item in observed] == [UUID(str(second["event_id"]))]
    assert [request.url.params.get("cursor") for request in requests] == [None, "page-1"]
    assert all(
        request.headers["authorization"] == "Bearer gateway-secret"
        for request in requests
    )
