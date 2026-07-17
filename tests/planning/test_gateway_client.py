from __future__ import annotations

import httpx
import pytest

from agenten.planning.gateway_client import GatewayPlanningClient, GatewayPlanningError
from agenten.planning.release import ReleaseConflictError
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


def batch_fixture() -> WorkBatch:
    return WorkBatch(
        batch_id="batch-1",
        title="Build tool",
        goal="Build a verified tool",
        subtask_ids=["sub-1"],
        target="n8n",
        runtime="n8n",
        runtime_version="v1",
        interface_schema="captain-n8n-artifact/v1",
        capability_tags=["crm", "delivery"],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
    )


def holdout_fixture() -> HoldoutSuite:
    return HoldoutSuite(
        batch_id="batch-1",
        cases=[ExampleCase(case_id="hidden", input={"lead": "novel"})],
    )


@pytest.mark.asyncio
async def test_release_posts_authenticated_batch_then_hidden_holdout() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        index = 41 if len(requests) == 1 else 42
        return httpx.Response(201, json={"index": index}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway/", "captain-secret", http)
        await client.release(batch_fixture(), holdout_fixture())

    assert [request.headers["authorization"] for request in requests] == [
        "Bearer captain-secret",
        "Bearer captain-secret",
    ]
    assert [request.url.path for request in requests] == ["/blocks", "/blocks"]
    assert requests[0].content
    first = __import__("json").loads(requests[0].content)
    second = __import__("json").loads(requests[1].content)
    assert first["block_type"] == "work_batch"
    assert second["block_type"] == "holdout"
    assert second["parent_index"] == 41


@pytest.mark.asyncio
async def test_release_maps_conflict_without_leaking_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "batch differs"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", "captain-secret", http)
        with pytest.raises(ReleaseConflictError) as failure:
            await client.release(batch_fixture(), holdout_fixture())

    assert "captain-secret" not in str(failure.value)
    assert "409" in str(failure.value)


@pytest.mark.asyncio
async def test_find_match_uses_authenticated_capability_query() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "batch_id": "old",
                    "artifact_ref": "artifact://wrong-target",
                    "data": {
                        "target": "autogen",
                        "runtime": "autogen",
                        "runtime_version": "v1",
                        "interface_schema": "captain-autogen-artifact/v1",
                        "capabilities": ["crm", "delivery"],
                    },
                },
                {
                    "batch_id": "compatible",
                    "artifact_ref": "artifact://validated/old",
                    "data": {
                        "target": "n8n",
                        "runtime": "n8n",
                        "runtime_version": "v1",
                        "interface_schema": "captain-n8n-artifact/v1",
                        "capabilities": ["delivery", "crm"],
                    },
                },
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", "captain-secret", http)
        match = await client.find_match("n8n", ["delivery", "crm"])

    assert match == "artifact://validated/old"
    assert observed[0].headers["authorization"] == "Bearer captain-secret"
    assert observed[0].url.params["need"] == "n8n crm delivery"


@pytest.mark.asyncio
async def test_invalid_gateway_response_is_a_sanitized_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", "captain-secret", http)
        with pytest.raises(GatewayPlanningError) as failure:
            await client.find_match("n8n", ["delivery"])

    assert "captain-secret" not in str(failure.value)
