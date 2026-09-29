from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest

from agenten.provider_proxy.contracts import (
    OpenAIPriceCardV1,
    ProviderBudgetContextV1,
    RuntimeProviderPolicyV1,
)


NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


def price_card(**updates: object) -> OpenAIPriceCardV1:
    values: dict[str, object] = {
        "schema": "captain.openai-price-card.v1",
        "snapshot_id": "openai-gpt-5.6-terra-2026-08-09",
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "input_cost_per_million_usd": "2.00",
        "cached_input_cost_per_million_usd": "0.20",
        "output_cost_per_million_usd": "12.00",
        "cache_write_multiplier": "1.25",
        "large_input_threshold_tokens": 272_000,
        "large_input_multiplier": "2",
        "large_output_multiplier": "1.5",
        "source_url": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        "effective_at": NOW,
        "expires_at": NOW + timedelta(days=7),
    }
    values.update(updates)
    return OpenAIPriceCardV1.model_validate(values)


def test_price_card_is_digest_and_expiry_bound() -> None:
    card = price_card()
    assert len(card.sha256) == 64
    card.require_active(NOW + timedelta(days=1))
    with pytest.raises(ValueError, match="expired"):
        card.require_active(card.expires_at)


def test_policy_is_exact_terra_high_and_has_no_paid_tools_or_retries() -> None:
    policy = RuntimeProviderPolicyV1(
        schema="captain.runtime-provider-policy.v1",
        policy_id="terra-high-no-tools",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        maximum_run_cost_usd="6.00",
        maximum_provider_calls=2,
        request_max_retries=0,
        paid_hosted_tools=(),
        maximum_output_tokens_per_call=128_000,
    )
    assert policy.model == "gpt-5.6-terra"
    with pytest.raises(ValueError):
        policy.model_copy(update={"request_max_retries": 1}).require_safe()


def test_budget_context_requires_complete_d1_binding() -> None:
    command_id = uuid5(NAMESPACE_URL, "command")
    context = ProviderBudgetContextV1(
        schema="captain.provider-budget-context.v1",
        command_id=command_id,
        original_command_id=uuid5(NAMESPACE_URL, "original"),
        result_id=uuid5(command_id, "captain.runtime-result"),
        reservation_id=uuid5(NAMESPACE_URL, "reservation"),
        job_id=uuid5(NAMESPACE_URL, "job"),
        run_id=uuid5(NAMESPACE_URL, "run"),
        input_id="demo-input",
        correlation_id=uuid5(NAMESPACE_URL, "correlation"),
        capability_id="sales-team",
        capability_version=1,
        session_id="codex-session-one",
        cost_authority_ref="gateway://capability-resume-authorizations/one",
        ceiling_usd=Decimal("6"),
        expires_at=NOW + timedelta(hours=1),
        policy_sha256="1" * 64,
        price_card_sha256="2" * 64,
    )
    context.require_active(NOW)
    with pytest.raises(ValueError, match="result"):
        context.model_copy(
            update={"result_id": uuid5(NAMESPACE_URL, "wrong")}
        ).require_active(NOW)
