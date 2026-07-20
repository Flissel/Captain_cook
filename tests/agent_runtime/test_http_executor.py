from __future__ import annotations

import httpx
import pytest

from agenten.agent_runtime.http_executor import AgentRuntimeHttpExecutor
from tests.agent_runtime.test_gateway_client import command, result


@pytest.mark.asyncio
async def test_http_executor_posts_typed_command_and_returns_result() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=result().model_dump(mode="json", by_alias=True),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        observed = await AgentRuntimeHttpExecutor(
            "https://runtime.test/", "runtime-secret", http
        ).execute(command())

    assert observed == result()
    assert requests[0].url.path == "/v1/runtime/execute"
    assert requests[0].headers["authorization"] == "Bearer runtime-secret"
    assert b"schema_name" not in requests[0].content
