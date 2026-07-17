from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import httpx
from pydantic import ValidationError

from agenten.delivery.gateway_client import (
    GatewayBatchProjection,
    GatewayDeliveryClient,
    GatewayDeliveryError,
)
from agenten.delivery.recovery import GatewayRecoveryService, RecoveryDecision
from gateway.contracts import ReasoningSliceEvent, RecoveryDecisionEvent


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


class FakeRecoveryGateway:
    def __init__(self, projections: list[GatewayBatchProjection]) -> None:
        self.projections = projections
        self.decisions: dict[tuple[str, int], RecoveryDecision] = {}
        self.record_calls = 0

    async def list_batches(self, status: str) -> tuple[str, ...]:
        assert status == "pending"
        return tuple(item.batch_id for item in self.projections)

    async def get_batch(self, batch_id: str) -> GatewayBatchProjection:
        return next(item for item in self.projections if item.batch_id == batch_id)

    async def record_recovery(self, decision: RecoveryDecision) -> RecoveryDecision:
        self.record_calls += 1
        existing = self.decisions.setdefault((decision.batch_id, decision.iteration), decision)
        projection = next(item for item in self.projections if item.batch_id == decision.batch_id)
        self.projections[self.projections.index(projection)] = projection.model_copy(
            update={
                "claim_token_sha256": None,
                "claim_expires_at": None,
                "recovery_recorded": True,
                "recovered_iteration": decision.iteration,
            }
        )
        return existing


def expired(batch_id: str = "batch-1", *, iteration: int = 2) -> GatewayBatchProjection:
    return GatewayBatchProjection(
        batch_id=batch_id,
        parent_index=41,
        status="pending",
        claim_token_sha256="a" * 64,
        claim_expires_at=NOW - timedelta(seconds=1),
        claim_iteration=iteration,
        codex_session_recorded=False,
        validation_run_recorded=False,
    )


@pytest.mark.asyncio
async def test_expired_claim_records_one_captain_requeue_decision() -> None:
    gateway = FakeRecoveryGateway([expired()])

    result = await GatewayRecoveryService(gateway).recover_expired(NOW)

    assert result == (
        RecoveryDecision(
            batch_id="batch-1",
            iteration=2,
            reason="claim_expired",
            decision="requeue",
        ),
    )


@pytest.mark.asyncio
async def test_recovery_replay_is_idempotent_per_batch_iteration() -> None:
    gateway = FakeRecoveryGateway([expired()])
    service = GatewayRecoveryService(gateway)

    first = await service.recover_expired(NOW)
    second = await service.recover_expired(NOW)

    assert first == (
        RecoveryDecision(
            batch_id="batch-1",
            iteration=2,
            reason="claim_expired",
            decision="requeue",
        ),
    )
    assert second == ()
    assert len(gateway.decisions) == 1
    assert gateway.record_calls == 1


@pytest.mark.parametrize(
    "summary_ref",
    [
        "chain-of-thought: I considered hidden alternatives",
        r"C:\\Users\\worker\\workspace\\notes.txt",
        "/home/worker/workspace/notes.txt",
    ],
)
def test_reasoning_slice_rejects_chain_of_thought_and_workspace_paths(
    summary_ref: str,
) -> None:
    with pytest.raises(ValidationError):
        ReasoningSliceEvent(
            batch_id="batch-1",
            iteration=2,
            slice_id="slice-1",
            summary_ref=summary_ref,
            sha256="a" * 64,
        )


def test_reasoning_slice_accepts_only_opaque_hash_bound_reference() -> None:
    event = ReasoningSliceEvent(
        batch_id="batch-1",
        iteration=2,
        slice_id="slice-1",
        summary_ref="artifact://reasoning/summary-42",
        sha256="a" * 64,
    )
    assert event.sha256 == "a" * 64


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ReasoningSliceEvent,
            {
                "batch_id": "batch-1",
                "iteration": 2,
                "slice_id": "slice-1",
                "summary_ref": "artifact://reasoning/summary-42",
                "sha256": "a" * 64,
                "chain_of_thought": "private reasoning",
            },
        ),
        (
            RecoveryDecisionEvent,
            {
                "batch_id": "batch-1",
                "iteration": 2,
                "reason": "claim_expired",
                "decision": "requeue",
                "workspace_path": r"C:\\repo",
            },
        ),
    ],
)
def test_recovery_payloads_reject_raw_reasoning_and_workspace_keys(
    model: type[ReasoningSliceEvent] | type[RecoveryDecisionEvent],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.asyncio
async def test_client_writes_reasoning_with_claim_and_recovery_as_captain() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/recovery"):
            return httpx.Response(201, json=json.loads(request.content))
        return httpx.Response(201, json={"index": len(requests)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "captain-token", http)
        await client.record_reasoning_slice(
            "batch-1",
            "claim-token",
            iteration=2,
            slice_id="slice-1",
            summary_ref="artifact://reasoning/summary-42",
            sha256="a" * 64,
        )
        await client.record_recovery(
            RecoveryDecision(
                batch_id="batch-1",
                iteration=2,
                reason="claim_expired",
                decision="requeue",
            )
        )

    assert requests[0].url.path == "/blocks"
    assert requests[0].headers["x-claim-token"] == "claim-token"
    assert json.loads(requests[0].content)["block_type"] == "reasoning_slice"
    assert requests[1].url.path == "/batches/batch-1/recovery"
    assert "x-claim-token" not in requests[1].headers


@pytest.mark.asyncio
async def test_client_http_error_does_not_expose_response_or_tokens() -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="chain-of-thought secret claim-token captain-token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "captain-token", http)
        with pytest.raises(GatewayDeliveryError) as caught:
            await client.record_recovery(
                RecoveryDecision(
                    batch_id="batch-1",
                    iteration=2,
                    reason="claim_expired",
                    decision="requeue",
                )
            )

    message = str(caught.value)
    assert "secret" not in message
    assert "claim-token" not in message
    assert "captain-token" not in message
