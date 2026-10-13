from __future__ import annotations

import json
import httpx
import pytest
from uuid import NAMESPACE_URL, uuid5

from agenten.provider_proxy.transports import (
    GatewayProviderSettlementHTTPClient,
    OpenAIResponsesHTTPClient,
)
from .test_service import _context


@pytest.mark.asyncio
async def test_upstream_and_gateway_credentials_are_confined_to_their_transports() -> None:
    observed: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((str(request.url), request.headers.get("authorization", "")))
        if request.url.host == "api.openai.com" and request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 42})
        if request.url.host == "api.openai.com":
            return httpx.Response(200, json={"id": "resp_one", "status": "in_progress"})
        if request.url.path.endswith("/provider-authorities/preflight"):
            return httpx.Response(200, json=json.loads(request.content))
        if "/provider-authorities/by-context/" in request.url.path:
            return httpx.Response(200, json=_context().model_dump(mode="json", by_alias=True))
        return httpx.Response(201, json={"operation_id": str(request_id), "replayed": False})

    request_id = uuid5(NAMESPACE_URL, "provider-request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenAIResponsesHTTPClient(
            base_url="https://api.openai.com/v1", api_key="openai-only", client=http
        )
        gateway = GatewayProviderSettlementHTTPClient(
            base_url="http://127.0.0.1:8090", provider_token="gateway-only", client=http
        )
        assert await provider.count_input_tokens({"model": "gpt-5.6-terra", "input": "x"}) == 42
        await provider.create_response({"model": "gpt-5.6-terra", "input": "x"})
        assert await gateway.require_active_provider_authority(_context()) == _context()
        context = _context()
        assert await gateway.resolve_provider_authority(
            f"gateway://provider-budget-contexts/{context.sha256}"
        ) == context
        await gateway.record_provider_settlement(
            {"schema": "example", "receipt": {"request_id": str(request_id)}}
        )

    assert observed[0][1] == "Bearer openai-only"
    assert observed[1][1] == "Bearer openai-only"
    assert observed[2][1] == "Bearer gateway-only"
    assert observed[3][1] == "Bearer gateway-only"
    assert observed[4][1] == "Bearer gateway-only"
    assert all("gateway-only" not in auth for url, auth in observed if "openai" in url)
    assert all("openai-only" not in auth for url, auth in observed if "127.0.0.1" in url)


@pytest.mark.asyncio
async def test_benchmark_settlement_uses_distinct_provider_receipt_route() -> None:
    observed: list[str] = []
    settlement_id = str(uuid5(NAMESPACE_URL, "benchmark-settlement"))

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        return httpx.Response(201, json={
            "schema": "captain.capability-benchmark-provider-settlement-write.v1",
            "settlement_id": settlement_id,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gateway = GatewayProviderSettlementHTTPClient(
            base_url="http://127.0.0.1:8090",
            provider_token="provider-receipt-only", client=http,
        )
        await gateway.record_provider_settlement({
            "schema": "captain.capability-benchmark-provider-settlement.v1",
            "settlement_id": settlement_id,
        })

    assert observed == ["/v1/capability-benchmark/provider-settlements"]


def test_transport_rejects_noncanonical_origins() -> None:
    with pytest.raises(ValueError, match="OpenAI"):
        OpenAIResponsesHTTPClient(
            base_url="http://api.openai.com/v1",
            api_key="x",
            client=httpx.AsyncClient(),
        )
    with pytest.raises(ValueError, match="Gateway"):
        GatewayProviderSettlementHTTPClient(
            base_url="https://evil.example",
            provider_token="x",
            client=httpx.AsyncClient(),
        )
