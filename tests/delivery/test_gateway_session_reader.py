from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from agenten.delivery.gateway_client import GatewayDeliveryClient


@pytest.mark.asyncio
async def test_captain_reads_only_active_codex_session_trace_for_batch() -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer captain-token"
        assert request.url.path == "/batches/batch-1/active-codex-sessions"
        return httpx.Response(
            200,
            json=[
                {
                    "project_id": "project-1",
                    "run_id": "run-1",
                    "trace_id": "trace-1",
                    "batch_id": "batch-1",
                    "worker_id": "worker-1",
                    "claim_id": "claim-1",
                    "fencing_token": 1,
                    "session_id": "session-1",
                    "iteration": 1,
                    "process_ref": "artifact://processes/abc",
                    "started_at": now.isoformat(),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "captain-token", http)
        sessions = await client.active_codex_sessions("batch-1")

    assert sessions[0].session_id == "session-1"
    assert sessions[0].worker_id == "worker-1"
