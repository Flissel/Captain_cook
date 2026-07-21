from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
)
from agenten.agent_factory.production_model_pricing import (
    ConfiguredFactoryPricingSource,
    OpenAIFactoryModelClientBuilder,
    ProductionModelPricingConfigurationError,
    build_production_model_pricing,
)


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def _policy() -> FactoryExecutionPolicyV1:
    return FactoryExecutionPolicyV1(
        schema_name="captain.factory-execution-policy.v1",
        mode=FactoryExecutionMode.RELEASE,
        live_execution=True,
        max_cost_usd="12.00",
        max_runtime_seconds=900,
        required_live_runs=3,
        allowed_models=("gpt-5.2",),
        live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
    )


def _environment() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "fixture-provider-secret",
        "CAPTAIN_FACTORY_PROVIDER": "openai",
        "CAPTAIN_FACTORY_MODEL": "gpt-5.2",
        "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD": "1.25",
        "CAPTAIN_FACTORY_PRICING_VERSION": "2026-07-21",
        "CAPTAIN_FACTORY_PRICING_EFFECTIVE_AT": "2026-07-21T00:00:00Z",
        "CAPTAIN_FACTORY_PRICING_INPUT_COST_PER_MILLION_USD": "1.75",
        "CAPTAIN_FACTORY_PRICING_OUTPUT_COST_PER_MILLION_USD": "14.00",
        "CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD": "0.01",
    }


def test_model_builder_is_job_bound_and_does_not_call_provider_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = Mock(name="constructed_client")
    factory = Mock(return_value=constructed)
    monkeypatch.setattr(
        "agenten.agent_factory.production_model_pricing.build_model_client",
        factory,
    )
    builder = OpenAIFactoryModelClientBuilder.from_environment(_environment())
    assert factory.call_count == 0
    job_id = UUID("10000000-0000-0000-0000-000000000001")
    correlation_id = UUID("10000000-0000-0000-0000-000000000002")
    job = SimpleNamespace(
        job_id=job_id,
        correlation_id=correlation_id,
        subject_version=3,
        execution_policy=_policy(),
    )
    invocation = SimpleNamespace(
        job_id=job_id,
        correlation_id=correlation_id,
        subject_version=3,
    )

    assert builder(job, invocation) is constructed
    factory.assert_called_once_with(
        api_key="fixture-provider-secret", model="gpt-5.2"
    )
    assert "fixture-provider-secret" not in repr(builder)


def test_pricing_source_persists_exact_job_policy_quote(tmp_path: Path) -> None:
    artifacts = ContentAddressedArtifactStore(tmp_path / "cas")
    bundle = build_production_model_pricing(
        _environment(), artifacts=artifacts
    )
    assert isinstance(bundle.pricing_source, ConfiguredFactoryPricingSource)
    job = SimpleNamespace(
        job_id=UUID("10000000-0000-0000-0000-000000000001"),
        subject_version=3,
        execution_policy=_policy(),
    )
    quote = bundle.pricing_source.resolve_quote(
        job=job,
        invocation=SimpleNamespace(),
        provider="openai",
        model="gpt-5.2",
        now=NOW,
    )

    assert quote is not None
    assert quote.max_cost_per_call == Decimal("1.25")
    assert quote.input_cost_per_million == Decimal("1.75")
    assert quote.output_cost_per_million == Decimal("14.00")
    assert artifacts.read_bytes(quote.evidence_ref)
    assert bundle.pricing_source.resolve_quote(
        job=job,
        invocation=SimpleNamespace(),
        provider="openai",
        model="gpt-other",
        now=NOW,
    ) is None


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("OPENAI_API_KEY", ""),
        ("CAPTAIN_FACTORY_PROVIDER", "anthropic"),
        ("CAPTAIN_FACTORY_PRICING_EFFECTIVE_AT", "not-a-time"),
        ("CAPTAIN_FACTORY_PRICING_OUTPUT_COST_PER_MILLION_USD", "unknown"),
        ("CAPTAIN_FACTORY_PRICING_MINIMUM_COST_USD", "2.00"),
    ),
)
def test_model_pricing_fails_closed_with_todo_tool(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    environ = _environment()
    environ[name] = value

    with pytest.raises(
        ProductionModelPricingConfigurationError,
        match="TODO_TOOL.v1",
    ):
        build_production_model_pricing(
            environ,
            artifacts=ContentAddressedArtifactStore(tmp_path / "cas"),
        )
