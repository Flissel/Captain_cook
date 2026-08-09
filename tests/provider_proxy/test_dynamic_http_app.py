from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4, uuid5

from fastapi.testclient import TestClient

from agenten.provider_proxy.app import create_dynamic_provider_proxy_app
from agenten.provider_proxy.contracts import ProviderBudgetContextV1
from agenten.provider_proxy.service import ProviderReply
from agenten.provider_proxy.supervisor import DynamicProviderBudgetProxySupervisor


def _context(label: str) -> ProviderBudgetContextV1:
    command_id = uuid4()
    return ProviderBudgetContextV1(
        schema="captain.provider-budget-context.v1",
        command_id=command_id,
        original_command_id=command_id,
        result_id=uuid5(command_id, "captain.runtime-result"),
        reservation_id=uuid4(),
        job_id=uuid4(),
        run_id=uuid4(),
        input_id=f"input-{label}",
        correlation_id=uuid4(),
        capability_id="capability",
        capability_version=1,
        session_id=f"session-{label}",
        cost_authority_ref=f"gateway://capability-resume-authorizations/{uuid4()}",
        ceiling_usd=Decimal("1.00"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        policy_sha256="a" * 64,
        price_card_sha256="b" * 64,
    )


class _Proxy:
    def __init__(self, context: ProviderBudgetContextV1) -> None:
        self.context = context
        self.calls = 0

    async def create(self, payload: dict[str, object]) -> ProviderReply:
        self.calls += 1
        return ProviderReply(200, {"content-type": "application/json"}, b'{"ok":true}')


class _Resolver:
    def __init__(self, contexts: dict[str, ProviderBudgetContextV1]) -> None:
        self.contexts = contexts
        self.calls: list[str] = []

    async def resolve_provider_authority(self, reference: str) -> ProviderBudgetContextV1:
        self.calls.append(reference)
        return self.contexts[reference]


def _reference(context: ProviderBudgetContextV1) -> str:
    return f"gateway://provider-budget-contexts/{context.sha256}"


def test_dynamic_proxy_preflights_and_isolates_three_command_contexts() -> None:
    contexts = tuple(_context(str(index)) for index in range(3))
    resolver = _Resolver({_reference(item): item for item in contexts})
    proxies: list[_Proxy] = []
    supervisor = DynamicProviderBudgetProxySupervisor(
        resolver=resolver,
        proxy_factory=lambda context: proxies.append(_Proxy(context)) or proxies[-1],
    )
    app = create_dynamic_provider_proxy_app(
        supervisor=supervisor,
        client_token="provider-client-token",
        runtime_finalize_token="runtime-finalize-token",
    )

    with TestClient(app) as client:
        for context in contexts:
            response = client.post(
                "/v1/responses",
                headers={
                    "Authorization": "Bearer provider-client-token",
                    "X-Captain-Provider-Context-Ref": _reference(context),
                },
                json={"model": "gpt-5.6-terra", "input": context.input_id},
            )
            assert response.status_code == 200

        health = client.get(
            "/v1/healthz",
            headers={"Authorization": "Bearer provider-client-token"},
        )

    assert len(proxies) == 3
    assert {item.context.command_id for item in proxies} == {item.command_id for item in contexts}
    assert all(item.calls == 1 for item in proxies)
    assert resolver.calls == [_reference(item) for item in contexts]
    assert health.json() == {
        "schema": "captain.provider-proxy-health.v1",
        "status": "ready",
        "supervisor_mode": "dynamic_gateway_context_v1",
    }


def test_dynamic_proxy_rejects_missing_auth_and_changed_reference_binding_before_effect() -> None:
    original = _context("original")
    changed = _context("changed")
    reference = _reference(original)
    resolver = _Resolver({reference: original})
    proxies: list[_Proxy] = []
    supervisor = DynamicProviderBudgetProxySupervisor(
        resolver=resolver,
        proxy_factory=lambda context: proxies.append(_Proxy(context)) or proxies[-1],
    )
    app = create_dynamic_provider_proxy_app(
        supervisor=supervisor,
        client_token="provider-client-token",
        runtime_finalize_token="runtime-finalize-token",
    )

    with TestClient(app) as client:
        assert client.post("/v1/responses", json={}).status_code == 401
        first = client.post(
            "/v1/responses",
            headers={
                "Authorization": "Bearer provider-client-token",
                "X-Captain-Provider-Context-Ref": reference,
            },
            json={"model": "gpt-5.6-terra", "input": "first"},
        )
        resolver.contexts[reference] = changed
        second = client.post(
            "/v1/responses",
            headers={
                "Authorization": "Bearer provider-client-token",
                "X-Captain-Provider-Context-Ref": reference,
            },
            json={"model": "gpt-5.6-terra", "input": "second"},
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json() == {"detail": "provider authority is unavailable"}
    assert len(proxies) == 1
    assert proxies[0].calls == 1

