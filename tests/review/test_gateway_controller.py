from __future__ import annotations

import inspect
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from agenten.delivery.gateway_client import GatewayDeliveryClient, GatewayDeliveryError
from agenten.review.gateway_controller import GatewayReviewController, GatewayReviewDecision


class FakeReviewGateway:
    def __init__(self) -> None:
        self.decisions: list[GatewayReviewDecision] = []

    async def record_review(
        self, decision: GatewayReviewDecision
    ) -> GatewayReviewDecision:
        self.decisions.append(decision)
        return decision


@pytest.mark.asyncio
async def test_controller_records_one_immutable_review_decision() -> None:
    gateway = FakeReviewGateway()
    controller = GatewayReviewController(gateway)

    result = await controller.record(
        batch_id="batch-1",
        iteration=2,
        review_id="review-2",
        decision="passed",
        evidence_refs=("artifact://reviews/review-2",),
    )

    assert result == GatewayReviewDecision(
        batch_id="batch-1",
        iteration=2,
        review_id="review-2",
        decision="passed",
        evidence_refs=("artifact://reviews/review-2",),
    )
    assert gateway.decisions == [result]
    with pytest.raises(ValidationError):
        result.iteration = 3


def test_controller_has_no_mutable_retry_counter_or_database_access() -> None:
    source = inspect.getsource(GatewayReviewController).lower()

    assert "counter" not in source
    assert "failed_reviews" not in source
    assert "pymysql" not in source
    assert "mariadb" not in source
    assert "storage" not in source


@pytest.mark.asyncio
async def test_captain_client_uses_dedicated_review_route_without_claim_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=json.loads(request.content), request=request)

    decision = GatewayReviewDecision(
        batch_id="batch-1",
        iteration=2,
        review_id="review-2",
        decision="failed",
        evidence_refs=("artifact://reviews/review-2",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "captain-secret", http)
        result = await client.record_review(decision)

    assert result == decision
    assert requests[0].url.path == "/batches/batch-1/review"
    assert requests[0].headers["authorization"] == "Bearer captain-secret"
    assert "x-claim-token" not in requests[0].headers


@pytest.mark.asyncio
async def test_review_client_errors_are_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            text="captain-secret private-review workspace path",
            request=request,
        )

    decision = GatewayReviewDecision(
        batch_id="batch-1",
        iteration=2,
        review_id="review-2",
        decision="passed",
        evidence_refs=("artifact://reviews/review-2",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayDeliveryClient("https://gateway.test", "captain-secret", http)
        with pytest.raises(GatewayDeliveryError) as caught:
            await client.record_review(decision)

    message = str(caught.value)
    assert "captain-secret" not in message
    assert "private-review" not in message
    assert "workspace" not in message


def test_gateway_review_controller_boundary_has_no_database_dependency() -> None:
    source = Path("agenten/review/gateway_controller.py").read_text(encoding="utf-8").lower()
    assert "gateway.store" not in source
    assert "pymysql" not in source
    assert "mariadb" not in source
