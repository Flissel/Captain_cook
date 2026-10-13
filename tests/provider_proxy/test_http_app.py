from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agenten.provider_proxy.app import create_provider_proxy_app
from agenten.provider_proxy.codex_config import write_isolated_codex_config

from .test_service import FakeGateway, FakeProvider, _proxy


def test_proxy_requires_its_own_bearer_and_exposes_only_responses_routes(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path, FakeProvider(), FakeGateway())
    app = create_provider_proxy_app(
        proxy=proxy,
        client_token="proxy-client-secret",
        runtime_finalize_token="runtime-finalize-secret",
    )
    with TestClient(app) as client:
        payload = {
            "model": "gpt-5.6-terra",
            "input": "build",
            "reasoning": {"effort": "high"},
        }
        assert client.post("/v1/responses", json=payload).status_code == 401
        assert client.post(
            "/v1/responses",
            json=payload,
            headers={"Authorization": "Bearer runtime-token"},
        ).status_code == 401
        response = client.post(
            "/v1/responses",
            json=payload,
            headers={"Authorization": "Bearer proxy-client-secret"},
        )
        assert response.status_code == 200
        assert response.json()["id"] == "resp_1"
        assert proxy.gateway.settlements == []
        finalized = client.post(
            "/v1/captain/provider-settlements/finalize",
            json={
                "command_id": str(proxy.context.command_id),
                "result_id": str(proxy.context.result_id),
            },
            headers={"Authorization": "Bearer runtime-finalize-secret"},
        )
        assert finalized.status_code == 200
        assert len(proxy.gateway.settlements) == 1
        assert client.get("/health").status_code == 404


def test_generated_codex_home_is_isolated_and_contains_no_upstream_credentials(tmp_path: Path) -> None:
    config = write_isolated_codex_config(
        root=tmp_path / "codex-home",
        proxy_base_url="http://127.0.0.1:18091/v1",
    )
    text = config.read_text(encoding="utf-8")
    assert 'model = "gpt-5.6-terra"' in text
    assert 'model_reasoning_effort = "high"' in text
    assert 'base_url = "http://127.0.0.1:18091/v1"' in text
    assert 'env_key = "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"' in text
    assert (
        'env_http_headers = { "X-Captain-Provider-Context-Ref" = '
        '"CAPTAIN_PROVIDER_CONTEXT_REF" }'
    ) in text
    assert "request_max_retries = 0" in text
    assert 'wire_api = "responses"' in text
    assert "OPENAI_API_KEY" not in text
    assert "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN" not in text
