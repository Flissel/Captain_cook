"""Explicit OpenAI model and immutable pricing ports for Factory V3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from autogen_core.models import ChatCompletionClient

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_policy import FactoryLiveCapability
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1
from agenten.agent_factory.team_execution import FactoryPricingQuoteV1
from agenten.llm.model_client import build_model_client


class ProductionModelPricingConfigurationError(RuntimeError):
    """A required provider or price authority is missing."""


def _todo(name: str) -> ProductionModelPricingConfigurationError:
    return ProductionModelPricingConfigurationError(
        f"TODO_TOOL.v1 required capability=model_pricing; name={name}"
    )


@dataclass(frozen=True)
class OpenAIFactoryModelClientBuilder:
    """Build a real, no-SDK-retry model client only for an exact V3 job."""

    model: str
    _api_key: str = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> "OpenAIFactoryModelClientBuilder":
        provider = _required(environ, "CAPTAIN_FACTORY_PROVIDER")
        if provider != "openai":
            raise _todo("CAPTAIN_FACTORY_PROVIDER:openai")
        return cls(
            model=_required(environ, "CAPTAIN_FACTORY_MODEL"),
            _api_key=_required(environ, "OPENAI_API_KEY"),
        )

    def __call__(
        self,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
    ) -> ChatCompletionClient:
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
        ):
            raise ValueError("model invocation is not bound to the Captain job")
        policy = job.execution_policy
        if (
            not policy.live_execution
            or self.model not in policy.allowed_models
            or FactoryLiveCapability.MODEL_INVOKE not in policy.live_capabilities
        ):
            raise ValueError("OpenAI model is not authorized by the Captain policy")
        return build_model_client(api_key=self._api_key, model=self.model)


class ConfiguredFactoryPricingSource:
    """Serve a versioned operator-configured price card with CAS evidence."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        version: str,
        effective_at: datetime,
        max_cost_per_call: Decimal,
        input_cost_per_million: Decimal,
        output_cost_per_million: Decimal,
        minimum_cost_usd: Decimal,
        artifacts: ContentAddressedArtifactStore,
    ) -> None:
        self._provider = provider
        self._model = model
        self._version = version
        self._effective_at = effective_at
        self._max_cost_per_call = max_cost_per_call
        self._input_cost_per_million = input_cost_per_million
        self._output_cost_per_million = output_cost_per_million
        self._minimum_cost_usd = minimum_cost_usd
        self._artifacts = artifacts

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        artifacts: ContentAddressedArtifactStore,
    ) -> "ConfiguredFactoryPricingSource":
        provider = _required(environ, "CAPTAIN_FACTORY_PROVIDER")
        if provider != "openai":
            raise _todo("CAPTAIN_FACTORY_PROVIDER:openai")
        effective_at = _utc_timestamp(
            environ,
            "CAPTAIN_FACTORY_PRICING_EFFECTIVE_AT",
        )
        max_cost = _money(
            environ,
            "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD",
            positive=True,
        )
        minimum = _money(
            environ,
            "CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD",
            positive=False,
        )
        if minimum > max_cost:
            raise _todo("CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD")
        return cls(
            provider=provider,
            model=_required(environ, "CAPTAIN_FACTORY_MODEL"),
            version=_required(environ, "CAPTAIN_FACTORY_PRICING_VERSION"),
            effective_at=effective_at,
            max_cost_per_call=max_cost,
            input_cost_per_million=_money(
                environ,
                "CAPTAIN_FACTORY_PRICING_INPUT_COST_PER_MILLION_USD",
                positive=False,
            ),
            output_cost_per_million=_money(
                environ,
                "CAPTAIN_FACTORY_PRICING_OUTPUT_COST_PER_MILLION_USD",
                positive=False,
            ),
            minimum_cost_usd=minimum,
            artifacts=artifacts,
        )

    def resolve_quote(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        provider: str,
        model: str,
        now: datetime,
    ) -> FactoryPricingQuoteV1 | None:
        del invocation
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("pricing quote clock must be UTC")
        if (
            provider != self._provider
            or model != self._model
            or now < self._effective_at
        ):
            return None
        policy_digest = _execution_policy_digest(job)
        payload = {
            "schema": "captain.factory-price-card-evidence.v1",
            "job_id": str(job.job_id),
            "subject_version": job.subject_version,
            "execution_policy_sha256": policy_digest,
            "provider": provider,
            "model": model,
            "version": self._version,
            "effective_at": self._effective_at.isoformat(),
            "max_cost_per_call": str(self._max_cost_per_call),
            "input_cost_per_million": str(self._input_cost_per_million),
            "output_cost_per_million": str(self._output_cost_per_million),
            "minimum_cost_usd": str(self._minimum_cost_usd),
        }
        encoded = _canonical_json(payload)
        evidence_ref = self._artifacts.put(
            encoded,
            "application/json",
            namespace="pricing-quote",
        )
        return FactoryPricingQuoteV1(
            quote_id=f"price-{evidence_ref.sha256[:24]}",
            job_id=job.job_id,
            subject_version=job.subject_version,
            execution_policy_sha256=policy_digest,
            provider=provider,
            model=model,
            version=self._version,
            effective_at=self._effective_at,
            max_cost_per_call=self._max_cost_per_call,
            input_cost_per_million=self._input_cost_per_million,
            output_cost_per_million=self._output_cost_per_million,
            minimum_cost_usd=self._minimum_cost_usd,
            evidence_ref=evidence_ref,
        )


@dataclass(frozen=True)
class ProductionModelPricingPorts:
    model_client_for: OpenAIFactoryModelClientBuilder
    pricing_source: ConfiguredFactoryPricingSource


def build_production_model_pricing(
    environ: Mapping[str, str],
    *,
    artifacts: ContentAddressedArtifactStore,
) -> ProductionModelPricingPorts:
    """Validate both live provider ports without starting a paid effect."""

    model = OpenAIFactoryModelClientBuilder.from_environment(environ)
    pricing = ConfiguredFactoryPricingSource.from_environment(
        environ,
        artifacts=artifacts,
    )
    return ProductionModelPricingPorts(
        model_client_for=model,
        pricing_source=pricing,
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise _todo(name)
    return value


def _money(
    environ: Mapping[str, str],
    name: str,
    *,
    positive: bool,
) -> Decimal:
    raw = _required(environ, name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise _todo(name) from exc
    if (
        not value.is_finite()
        or value < 0
        or (positive and value <= 0)
        or value.as_tuple().exponent < -8
    ):
        raise _todo(name)
    return value


def _utc_timestamp(environ: Mapping[str, str], name: str) -> datetime:
    raw = _required(environ, name)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _todo(name) from exc
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise _todo(name)
    return value


def _execution_policy_digest(job: AgentFactoryJobV3) -> str:
    return hashlib.sha256(
        _canonical_json(job.execution_policy.model_dump(mode="json", by_alias=True))
    ).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
