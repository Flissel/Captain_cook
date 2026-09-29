from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agenten.provider_proxy.contracts import (
    OpenAIPriceCardV1,
    ProviderBudgetContextV1,
    RuntimeProviderPolicyV1,
)
from agenten.provider_proxy.repository import SQLiteProviderBudgetRepository
from agenten.provider_proxy.service import (
    BudgetRejected,
    ProviderBudgetProxy,
    ProviderReply,
    ProviderStreamReply,
    ProviderUsageUnavailable,
)


NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


def _card() -> OpenAIPriceCardV1:
    return OpenAIPriceCardV1.model_validate(
        {
            "schema": "captain.openai-price-card.v1",
            "snapshot_id": "terra-2026-08-09",
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "input_cost_per_million_usd": "2.00",
            "cached_input_cost_per_million_usd": "0.20",
            "output_cost_per_million_usd": "12.00",
            "cache_write_multiplier": "1.25",
            "large_input_threshold_tokens": 272000,
            "large_input_multiplier": "2",
            "large_output_multiplier": "1.5",
            "source_url": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
            "effective_at": NOW - timedelta(minutes=1),
            "expires_at": NOW + timedelta(days=1),
        }
    )


def _policy(**updates: object) -> RuntimeProviderPolicyV1:
    values: dict[str, object] = {
        "schema": "captain.runtime-provider-policy.v1",
        "policy_id": "terra-high",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "maximum_run_cost_usd": "6.00",
        "maximum_provider_calls": 2,
        "request_max_retries": 0,
        "paid_hosted_tools": (),
        "maximum_output_tokens_per_call": 128000,
    }
    values.update(updates)
    return RuntimeProviderPolicyV1.model_validate(values)


def _context(
    *, policy: RuntimeProviderPolicyV1 | None = None, card: OpenAIPriceCardV1 | None = None
) -> ProviderBudgetContextV1:
    command = uuid5(NAMESPACE_URL, "proxy-command")
    policy = policy or _policy()
    card = card or _card()
    return ProviderBudgetContextV1.model_validate(
        {
            "schema": "captain.provider-budget-context.v1",
            "command_id": command,
            "original_command_id": uuid5(NAMESPACE_URL, "original"),
            "result_id": uuid5(command, "captain.runtime-result"),
            "reservation_id": uuid5(NAMESPACE_URL, "reservation"),
            "job_id": uuid5(NAMESPACE_URL, "job"),
            "run_id": uuid5(NAMESPACE_URL, "run"),
            "input_id": "demo",
            "correlation_id": uuid5(NAMESPACE_URL, "correlation"),
            "capability_id": "demo-capability",
            "capability_version": 1,
            "session_id": "codex-session-one",
            "cost_authority_ref": "gateway://capability-resume-authorizations/one",
            "ceiling_usd": "6.00",
            "expires_at": NOW + timedelta(hours=1),
            "policy_sha256": policy.sha256,
            "price_card_sha256": card.sha256,
        }
    )


class FakeProvider:
    def __init__(self, *, input_tokens: int = 1000, usage: dict[str, object] | None = None, background: bool = False) -> None:
        self.input_tokens = input_tokens
        self.usage = usage if usage is not None else {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": min(100, input_tokens)},
            "output_tokens": 200,
        }
        self.count_payloads: list[dict[str, object]] = []
        self.create_payloads: list[dict[str, object]] = []
        self.cancel_calls = 0
        self.background = background
        self.response_count = 0

    async def count_input_tokens(self, payload: dict[str, object]) -> int:
        self.count_payloads.append(payload)
        return self.input_tokens

    async def create_response(self, payload: dict[str, object]) -> ProviderReply:
        self.create_payloads.append(payload)
        if self.background and "response_id" not in payload:
            return ProviderReply(
                status_code=200,
                headers={"content-type": "application/json"},
                body=b'{"id":"resp_one","status":"in_progress"}',
            )
        response_id = payload.get("response_id")
        if not isinstance(response_id, str):
            self.response_count += 1
            response_id = f"resp_{self.response_count}"
        return ProviderReply(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"id": response_id, "status": "completed", "usage": self.usage}
            ).encode(),
        )

    async def read_response(self, response_id: str, query: str) -> ProviderReply:
        return await self.create_response({"response_id": response_id, "query": query})

    async def cancel_response(self, response_id: str) -> ProviderReply:
        self.cancel_calls += 1
        return ProviderReply(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"id": response_id, "status": "cancelled"}).encode(),
        )


class FakeGateway:
    def __init__(self) -> None:
        self.settlements: list[dict[str, object]] = []
        self.preflights: list[ProviderBudgetContextV1] = []

    async def require_active_provider_authority(
        self, context: ProviderBudgetContextV1
    ) -> ProviderBudgetContextV1:
        self.preflights.append(context)
        return context

    async def record_provider_settlement(self, payload: dict[str, object]) -> None:
        self.settlements.append(payload)


class FailingGateway(FakeGateway):
    async def record_provider_settlement(self, payload: dict[str, object]) -> None:
        raise RuntimeError("gateway unavailable")


def _proxy(tmp_path: Path, provider: FakeProvider, gateway: FakeGateway, **policy: object) -> ProviderBudgetProxy:
    configured_policy = _policy(**policy)
    card = _card()
    return ProviderBudgetProxy(
        context=_context(policy=configured_policy, card=card),
        policy=configured_policy,
        price_card=card,
        repository=SQLiteProviderBudgetRepository(tmp_path / "budget.sqlite3"),
        cas_root=tmp_path / "cas",
        provider=provider,
        gateway=gateway,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_exact_count_precedes_atomic_reservation_and_caps_output(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(input_tokens=1000), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway)
    reply = await proxy.create(
        {"model": "gpt-5.6-terra", "input": "build", "reasoning": {"effort": "high"}}
    )
    assert reply.status_code == 200
    assert len(provider.count_payloads) == 1
    assert gateway.preflights == [proxy.context, proxy.context]
    assert provider.create_payloads[0]["max_output_tokens"] <= 128000
    assert gateway.settlements == []
    settlement = await proxy.finalize(
        command_id=proxy.context.command_id, result_id=proxy.context.result_id
    )
    assert settlement["receipt"]["actual_cost_usd"] == "0.004220"


@pytest.mark.asyncio
async def test_rejects_large_input_paid_tools_and_impossible_budget_without_create(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(input_tokens=272000), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway)
    with pytest.raises(BudgetRejected, match="272000"):
        await proxy.create({"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}})
    with pytest.raises(BudgetRejected, match="hosted tool"):
        await proxy.create({"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}, "tools": [{"type": "web_search"}]})
    with pytest.raises(BudgetRejected, match="hosted tool"):
        await proxy.create({"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}, "tools": [{"type": "future_paid_tool"}]})
    assert provider.create_payloads == []


@pytest.mark.asyncio
async def test_missing_usage_consumes_full_reservation_and_never_settles_gateway(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(usage={}), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway, maximum_run_cost_usd="0.01")
    with pytest.raises(ProviderUsageUnavailable):
        await proxy.create({"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}})
    projection = proxy.repository.projection()
    assert projection.consumed_usd == Decimal("0.010000")
    assert gateway.settlements == []


@pytest.mark.asyncio
async def test_atomic_reservation_never_exceeds_call_count_under_race(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(input_tokens=1), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway, maximum_provider_calls=1)
    payload = {"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}}
    outcomes = await asyncio.gather(proxy.create(payload), proxy.create({**payload, "input": "y"}), return_exceptions=True)
    assert sum(isinstance(item, ProviderReply) for item in outcomes) == 1
    assert sum(isinstance(item, BudgetRejected) for item in outcomes) == 1
    assert len(provider.create_payloads) == 1


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_does_not_forge_usage(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway)
    first = await proxy.cancel("resp_one")
    second = await proxy.cancel("resp_one")
    assert first.body == second.body
    assert provider.cancel_calls == 2
    assert gateway.settlements == []


@pytest.mark.asyncio
async def test_background_response_remains_reserved_until_terminal_readback(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(background=True), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway)
    created = await proxy.create(
        {"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}, "background": True}
    )
    assert b'in_progress' in created.body
    assert proxy.repository.projection().active_reserved_usd > 0
    read = await proxy.read("resp_one")
    assert b'completed' in read.body
    assert proxy.repository.projection().active_reserved_usd == 0
    assert gateway.settlements == []
    await proxy.finalize(
        command_id=proxy.context.command_id, result_id=proxy.context.result_id
    )
    assert len(gateway.settlements) == 1


@pytest.mark.asyncio
async def test_slow_token_count_crossing_authority_expiry_has_zero_provider_effect(
    tmp_path: Path,
) -> None:
    moments = iter((NOW, NOW + timedelta(hours=2)))
    provider, gateway = FakeProvider(input_tokens=1000), FakeGateway()
    proxy = ProviderBudgetProxy(
        context=_context(),
        policy=_policy(),
        price_card=_card(),
        repository=SQLiteProviderBudgetRepository(tmp_path / "budget.sqlite3"),
        cas_root=tmp_path / "cas",
        provider=provider,
        gateway=gateway,
        clock=lambda: next(moments),
    )

    with pytest.raises(BudgetRejected, match="provider authority is unavailable"):
        await proxy.create(
            {"model": "gpt-5.6-terra", "input": "build", "reasoning": {"effort": "high"}}
        )

    assert provider.count_payloads
    assert provider.create_payloads == []
    assert proxy.repository.projection().call_count == 0


@pytest.mark.asyncio
async def test_gateway_settlement_failure_consumes_reservation_and_hides_output(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(), FailingGateway()
    proxy = _proxy(tmp_path, provider, gateway, maximum_run_cost_usd="0.01")
    await proxy.create(
        {"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}}
    )
    with pytest.raises(ProviderUsageUnavailable, match="settlement"):
        await proxy.finalize(
            command_id=proxy.context.command_id, result_id=proxy.context.result_id
        )
    assert proxy.repository.projection().consumed_usd == Decimal("0.004220")


@pytest.mark.asyncio
async def test_sse_terminal_event_is_withheld_until_gateway_settlement(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(), FakeGateway()

    async def stream_response(payload: dict[str, object]) -> ProviderStreamReply:
        async def chunks():
            yield b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            terminal = {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream",
                    "status": "completed",
                    "usage": provider.usage,
                },
            }
            encoded = ("data: " + json.dumps(terminal, separators=(",", ":")) + "\n\n").encode()
            yield encoded[:17]
            yield encoded[17:]
            yield b"data: [DONE]\n\n"

        return ProviderStreamReply(200, {"content-type": "text/event-stream"}, chunks())

    provider.open_response_stream = stream_response  # type: ignore[attr-defined]
    proxy = _proxy(tmp_path, provider, gateway)
    streamed = await proxy.create_stream(
        {"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}, "stream": True}
    )
    observed = []
    async for chunk in streamed.chunks:
        if b"response.completed" in chunk:
            assert gateway.settlements == []
        observed.append(chunk)
    assert b"response.completed" in b"".join(observed)
    assert proxy.repository.projection().active_reserved_usd == 0
    await proxy.finalize(
        command_id=proxy.context.command_id, result_id=proxy.context.result_id
    )
    assert len(gateway.settlements) == 1


@pytest.mark.asyncio
async def test_two_calls_finalize_as_one_exact_aggregate_settlement(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(input_tokens=1000), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway)
    await proxy.create(
        {"model": "gpt-5.6-terra", "input": "first", "reasoning": {"effort": "high"}}
    )
    await proxy.create(
        {"model": "gpt-5.6-terra", "input": "second", "reasoning": {"effort": "high"}}
    )
    first, second = await asyncio.gather(
        proxy.finalize(command_id=proxy.context.command_id, result_id=proxy.context.result_id),
        proxy.finalize(command_id=proxy.context.command_id, result_id=proxy.context.result_id),
    )
    assert first == second
    assert len(gateway.settlements) == 1
    assert first["receipt"]["input_units"] == 2000
    assert first["receipt"]["cached_input_units"] == 200
    assert first["receipt"]["output_units"] == 400


@pytest.mark.asyncio
async def test_background_cancel_consumes_unverifiable_reservation_once(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(background=True), FakeGateway()
    proxy = _proxy(tmp_path, provider, gateway, maximum_run_cost_usd="0.01")
    await proxy.create(
        {"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}, "background": True}
    )
    await proxy.cancel("resp_one")
    await proxy.cancel("resp_one")
    assert proxy.repository.projection().consumed_usd == Decimal("0.010000")
    assert proxy.repository.projection().active_reserved_usd == 0
    assert provider.cancel_calls == 2


@pytest.mark.asyncio
async def test_upstream_failure_after_reservation_consumes_full_ceiling(tmp_path: Path) -> None:
    provider, gateway = FakeProvider(), FakeGateway()

    async def fail_create(payload: dict[str, object]) -> ProviderReply:
        raise RuntimeError("connection dropped")

    provider.create_response = fail_create  # type: ignore[method-assign]
    proxy = _proxy(tmp_path, provider, gateway, maximum_run_cost_usd="0.01")
    with pytest.raises(ProviderUsageUnavailable, match="ambiguous"):
        await proxy.create(
            {"model": "gpt-5.6-terra", "input": "x", "reasoning": {"effort": "high"}}
        )
    assert proxy.repository.projection().consumed_usd == Decimal("0.010000")
