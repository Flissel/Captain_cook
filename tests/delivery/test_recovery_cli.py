from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agenten.delivery.recovery_cli import (
    GatewayRecoveryConfigurationError,
    async_main,
)


NOW = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)


def _projection(
    batch_id: str,
    *,
    expired: bool = True,
    iteration: int = 1,
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "parent_index": 1,
        "status": "pending",
        "claim_token_sha256": "a" * 64,
        "claim_id": "claim-1",
        "fencing_token": 1,
        "claim_expires_at": (NOW - timedelta(seconds=1) if expired else NOW + timedelta(seconds=1)).isoformat(),
        "claim_iteration": iteration,
        "codex_session_recorded": False,
        "validation_run_recorded": False,
        "recovery_recorded": False,
        "recovered_iteration": None,
    }


@pytest.mark.asyncio
async def test_recovery_cli_runs_captain_pass_and_reports_requeue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/batches":
            return httpx.Response(200, json=[{"batch_id": "batch-1", "title": "work"}])
        if request.url.path == "/batches/batch-1":
            return httpx.Response(200, json=_projection("batch-1"))
        if request.url.path == "/batches/batch-1/recovery":
            return httpx.Response(201, json=json.loads(request.content))
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "captain-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        exit_code = await async_main(
            ["--gateway-url", "https://gateway.test"],
            http_client=http,
            now=lambda: NOW,
        )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "deferred_batch_ids": [],
        "recovered_batch_ids": ["batch-1"],
    }
    assert requests[-1].headers["authorization"] == "Bearer captain-token"


@pytest.mark.asyncio
async def test_recovery_cli_defers_conflicted_session_instead_of_failing_the_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/batches":
            return httpx.Response(200, json=[{"batch_id": "batch-active", "title": "work"}])
        if request.url.path == "/batches/batch-active":
            return httpx.Response(200, json=_projection("batch-active"))
        if request.url.path == "/batches/batch-active/recovery":
            return httpx.Response(409, json={"detail": "active Codex session requires terminal evidence"})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "captain-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        exit_code = await async_main(
            ["--gateway-url", "https://gateway.test"],
            http_client=http,
            now=lambda: NOW,
        )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "deferred_batch_ids": ["batch-active"],
        "recovered_batch_ids": [],
    }


@pytest.mark.asyncio
async def test_recovery_cli_requires_captain_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAPTAIN_GATEWAY_TOKEN", raising=False)

    with pytest.raises(GatewayRecoveryConfigurationError, match="CAPTAIN_GATEWAY_TOKEN"):
        await async_main(["--gateway-url", "https://gateway.test"])
