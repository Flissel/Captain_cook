"""Immutable contracts for the localhost Responses budget proxy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be UTC")
    return value


def _money(value: object, *, positive: bool = False) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or (positive and amount <= 0):
        raise ValueError("cost must be finite and positive" if positive else "cost must be finite and non-negative")
    return amount


def canonical_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def benchmark_provider_settlement_id(
    command_id: UUID | str, provider_call_id: str,
) -> UUID:
    """Canonical identity for one independently settled benchmark provider call."""

    if not provider_call_id or len(provider_call_id) > 128:
        raise ValueError("benchmark provider call identity is invalid")
    return uuid5(UUID(str(command_id)), provider_call_id)


class OpenAIPriceCardV1(_FrozenContract):
    schema_name: Literal["captain.openai-price-card.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    snapshot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    provider: Literal["openai"]
    model: Literal["gpt-5.6-terra"]
    input_cost_per_million_usd: Decimal
    cached_input_cost_per_million_usd: Decimal
    output_cost_per_million_usd: Decimal
    cache_write_multiplier: Decimal
    large_input_threshold_tokens: int = Field(ge=1, strict=True)
    large_input_multiplier: Decimal
    large_output_multiplier: Decimal
    source_url: Literal["https://developers.openai.com/api/docs/models/gpt-5.6-terra"]
    effective_at: datetime
    expires_at: datetime

    @field_validator(
        "input_cost_per_million_usd",
        "cached_input_cost_per_million_usd",
        "output_cost_per_million_usd",
        mode="before",
    )
    @classmethod
    def validate_cost(cls, value: object) -> Decimal:
        return _money(value)

    @field_validator(
        "cache_write_multiplier", "large_input_multiplier", "large_output_multiplier", mode="before"
    )
    @classmethod
    def validate_multiplier(cls, value: object) -> Decimal:
        multiplier = Decimal(str(value))
        if not multiplier.is_finite() or multiplier < 1:
            raise ValueError("price multiplier must be finite and at least one")
        return multiplier

    @field_validator("effective_at", "expires_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> "OpenAIPriceCardV1":
        if self.expires_at <= self.effective_at:
            raise ValueError("price-card window is invalid")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self)).hexdigest()

    def require_active(self, now: datetime) -> None:
        if not self.effective_at <= _utc(now) < self.expires_at:
            raise ValueError("price card is expired or not yet effective")


class RuntimeProviderPolicyV1(_FrozenContract):
    schema_name: Literal["captain.runtime-provider-policy.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    policy_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["high"]
    maximum_run_cost_usd: Decimal
    maximum_provider_calls: int = Field(ge=1, le=16, strict=True)
    request_max_retries: Literal[0]
    paid_hosted_tools: tuple[()] = ()
    maximum_output_tokens_per_call: int = Field(ge=1, le=128_000, strict=True)

    @field_validator("maximum_run_cost_usd", mode="before")
    @classmethod
    def validate_ceiling(cls, value: object) -> Decimal:
        return _money(value, positive=True)

    def require_safe(self) -> None:
        if (
            self.model != "gpt-5.6-terra"
            or self.reasoning_effort != "high"
            or self.request_max_retries != 0
            or self.paid_hosted_tools
        ):
            raise ValueError("runtime provider policy is unsafe")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self)).hexdigest()


class RuntimeProviderProxyBindingV1(_FrozenContract):
    schema_name: Literal["captain.runtime-provider-proxy-binding.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    responses_url: str = Field(pattern=r"^http://127\.0\.0\.1:[0-9]{1,5}/v1$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_card_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paid_command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hard_ceiling_enforced: Literal[True] = True


class ProviderBudgetContextV1(_FrozenContract):
    schema_name: Literal["captain.provider-budget-context.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    command_id: UUID
    original_command_id: UUID
    result_id: UUID
    reservation_id: UUID
    job_id: UUID
    run_id: UUID
    input_id: str = Field(min_length=1)
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    cost_authority_ref: str = Field(
        pattern=r"^gateway://capability-(?:resume|benchmark)-authorizations/"
    )
    ceiling_usd: Decimal
    expires_at: datetime
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_card_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("ceiling_usd", mode="before")
    @classmethod
    def validate_ceiling(cls, value: object) -> Decimal:
        return _money(value, positive=True)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        return _utc(value)

    def require_active(self, now: datetime) -> None:
        if _utc(now) >= self.expires_at:
            raise ValueError("provider budget context is expired")
        if self.result_id != uuid5(self.command_id, "captain.runtime-result"):
            raise ValueError("provider budget result binding is invalid")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self)).hexdigest()
