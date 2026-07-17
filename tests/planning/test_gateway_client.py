import json

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
async def test_release_posts_batch_then_hidden_holdout() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"index": 40 + len(requests)}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway/", http)
        await client.release(batch_fixture(), holdout_fixture())

    payloads = [json.loads(request.content) for request in requests]
    assert [payload["block_type"] for payload in payloads] == ["work_batch", "holdout"]
    assert payloads[1]["parent_index"] == 41


@pytest.mark.asyncio
async def test_release_accepts_idempotent_existing_gateway_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"index": 41}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        await client.release(batch_fixture(), holdout_fixture())


@pytest.mark.asyncio
async def test_release_maps_different_content_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "batch differs"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        with pytest.raises(ReleaseConflictError, match="409"):
            await client.release(batch_fixture(), holdout_fixture())


@pytest.mark.asyncio
async def test_find_match_uses_canonical_capability_query() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "artifact_ref": "artifact://wrong-target",
                    "data": {
                        "target": "autogen",
                        "capabilities": ["crm", "delivery"],
                    },
                },
                {
                    "artifact_ref": "artifact://validated/old",
                    "data": {
                        "target": "n8n",
                        "capabilities": ["delivery", "crm", "extra"],
                    },
                },
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        match = await client.find_match("n8n", ["delivery", "crm"])

    assert match == "artifact://validated/old"
    assert observed[0].url.params["need"] == "n8n crm delivery"


@pytest.mark.asyncio
async def test_find_match_rejects_missing_capability_tags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "artifact_ref": "artifact://partial",
                    "data": {"target": "n8n", "capabilities": ["delivery"]},
                }
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        match = await client.find_match("n8n", ["delivery", "crm"])

    assert match is None


@pytest.mark.asyncio
async def test_invalid_gateway_response_is_a_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        with pytest.raises(GatewayPlanningError, match="invalid response"):
            await client.find_match("n8n", ["delivery"])
