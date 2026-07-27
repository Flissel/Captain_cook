"""Fail-closed provider boundary for paired business benchmark execution.

The coordinator in :mod:`business_benchmark_execution` owns replay and claim
orchestration.  This module only binds an already claimed effect to an injected
production runtime bundle and an injected durable provider fence store.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.business_benchmark_execution import (
    BusinessBenchmarkExecutionEnvelopeV1,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkFenceReceiptV1,
    BusinessBenchmarkPreparedEffectV1,
    BusinessBenchmarkRecoveryObservationV1,
    BusinessBenchmarkRuntimePreparationV1,
)
from agenten.agent_factory.execution_budget import FactoryUsageReceiptV1
from agenten.agent_factory.team_execution import (
    FactoryHandoffEvidenceV1,
    FactoryToolExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


_SAFE_FACT_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_RELATIVE_ROOT = Path(".captain-cook/evidence/business-benchmarks")
_PROVIDER_SECRET_NAMES = {"openai": "OPENAI_API_KEY"}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProductionAdapterUnavailableError(RuntimeError):
    """The real runtime bundle required for a provider effect is unavailable."""

    code = "TODO_TOOL.v1"

    def __init__(self, detail: str = "production benchmark adapter bundle is unavailable") -> None:
        super().__init__(f"{self.code}: {detail}")


class UnsafeBenchmarkToolError(ValueError):
    """A provider run observed a tool without a trusted intent binding."""


class BaselineAssistantPolicyV1(_FrozenModel):
    """Authority-free policy used to create one fresh AssistantAgent per case."""

    schema_name: Literal["captain.business-benchmark-baseline-assistant-policy.v1"] = Field(
        default="captain.business-benchmark-baseline-assistant-policy.v1",
        alias="schema",
        serialization_alias="schema",
    )
    agent_name: str
    system_policy_version: str = "single-agent-baseline-v1"
    terminal_schema: Literal["captain.business-benchmark-terminal.v1"] = (
        "captain.business-benchmark-terminal.v1"
    )
    team_manifest_ref: None = None
    handoffs: tuple[()] = ()
    routing_authority: Literal[False] = False
    publication_authority: Literal[False] = False
    grant_authority: Literal[False] = False

    @field_validator("agent_name")
    @classmethod
    def require_safe_agent_name(cls, value: str) -> str:
        if _SAFE_AGENT_NAME.fullmatch(value) is None:
            raise ValueError("baseline agent name is invalid")
        return value


class BenchmarkTerminalOutputV1(_FrozenModel):
    schema_name: Literal["captain.business-benchmark-terminal.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    observed_decision: str
    observed_rationale_fact_ids: tuple[str, ...]

    @field_validator("observed_decision")
    @classmethod
    def require_safe_decision(cls, value: str) -> str:
        if _SAFE_FACT_ID.fullmatch(value) is None:
            raise ValueError("observed decision is not a redacted identifier")
        return value

    @field_validator("observed_rationale_fact_ids")
    @classmethod
    def require_redacted_facts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(_SAFE_FACT_ID.fullmatch(item) is None for item in value):
            raise ValueError("rationale must contain redacted fact identifiers")
        if len(value) != len(set(value)):
            raise ValueError("rationale fact identifiers must be unique")
        return value


class ProviderBenchmarkExecutionV1(_FrozenModel):
    """Typed output of the injected provider runtime bundle.

    Production wiring must execute candidate variants through the sealed
    ``HostAutoGenTeamRunner`` and current candidate package.  Baseline variants
    must create a fresh AutoGen ``AssistantAgent`` from ``baseline_policy``.
    This model deliberately carries no endpoint, token, manifest publication,
    grant, or routing authority.
    """

    request_id: UUID
    runtime_session_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    variant: Literal["candidate", "single_agent_baseline"]
    candidate_ref: ArtifactRef | None
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_cost_micro_usd: int = Field(ge=0, strict=True)
    maximum_latency_ms: int = Field(ge=0, strict=True)
    redaction_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal[
        "succeeded", "failed", "infrastructure_failed", "policy_failed", "cancelled"
    ]
    terminal_output: str | None = None
    usage_receipts: tuple[FactoryUsageReceiptV1, ...] = ()
    runtime_evidence_ref: ArtifactRef
    terminal_evidence_ref: ArtifactRef | None = None
    tool_executions: tuple[FactoryToolExecutionEvidenceV1, ...] = ()
    handoffs: tuple[FactoryHandoffEvidenceV1, ...] = ()
    completed_at: datetime

    @model_validator(mode="after")
    def require_terminal_success_evidence(self) -> "ProviderBenchmarkExecutionV1":
        if self.status == "succeeded":
            if self.terminal_output is None or self.terminal_evidence_ref is None:
                raise ValueError("successful provider execution requires terminal evidence")
            if not self.usage_receipts:
                raise ValueError("successful provider execution requires finalized usage receipts")
        elif self.terminal_output is not None or self.terminal_evidence_ref is not None:
            raise ValueError("non-successful provider execution cannot carry terminal output")
        return self


class DurableProviderFencePort(Protocol):
    """Durable production-side greatest-fence persistence."""

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1: ...

    async def assert_current(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None: ...


class ProductionBusinessBenchmarkRuntimeBundlePort(Protocol):
    """Required bridge to provider model, candidate package, and approved tools."""

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1: ...

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        *,
        baseline_policy: BaselineAssistantPolicyV1 | None,
    ) -> ProviderBenchmarkExecutionV1: ...

    async def recover(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRecoveryObservationV1: ...


class BusinessBenchmarkLiveAdapter:
    """Concrete ``BusinessBenchmarkExecutorPort`` for an injected live bundle."""

    def __init__(
        self,
        *,
        runtime_bundle: ProductionBusinessBenchmarkRuntimeBundlePort,
        fence_store: DurableProviderFencePort,
        trusted_tool_intents: Mapping[str, IntegrationIntent],
        monotonic_clock: Callable[[], float],
        clock: Callable[[], datetime],
        approved_n8n_tool_names: tuple[str, ...] = (),
    ) -> None:
        self._runtime_bundle = runtime_bundle
        self._fence_store = fence_store
        self._trusted_tool_intents = dict(trusted_tool_intents)
        self._monotonic_clock = monotonic_clock
        self._clock = clock
        self._approved_n8n_tool_names = frozenset(approved_n8n_tool_names)

    async def prepare(
        self, envelope: BusinessBenchmarkExecutionEnvelopeV1
    ) -> BusinessBenchmarkRuntimePreparationV1:
        preparation = await self._runtime_bundle.prepare(envelope)
        if preparation.runtime_session_id != envelope.runtime_session_id:
            raise ValueError("runtime preparation does not match benchmark session")
        return preparation

    async def register_fence(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        self._require_claim_binding(prepared, claim)
        receipt = await self._fence_store.register_fence(prepared, claim)
        self._require_fence_binding(prepared, claim, receipt)
        return receipt

    async def execute(
        self,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        prepared = claim.prepared_effect
        self._require_claim_binding(prepared, claim)
        self._require_fence_binding(prepared, claim, fence_receipt)
        if prepared.runtime_session_id != envelope.runtime_session_id:
            raise ValueError("claimed runtime session does not match benchmark envelope")
        await self._fence_store.assert_current(prepared, claim, fence_receipt)

        baseline_policy = (
            self._baseline_policy(envelope)
            if envelope.variant == "single_agent_baseline"
            else None
        )
        started = self._monotonic_clock()
        provider = await self._runtime_bundle.execute(
            envelope,
            claim,
            fence_receipt,
            baseline_policy=baseline_policy,
        )
        latency_ms = math.ceil((self._monotonic_clock() - started) * 1_000)
        if latency_ms < 0:
            raise ValueError("monotonic benchmark latency moved backwards")
        if provider.runtime_session_id != envelope.runtime_session_id:
            raise ValueError("provider runtime session does not match benchmark envelope")
        if provider.model_version != envelope.model_version:
            raise ValueError("provider model does not match benchmark envelope")
        provider_bindings = (
            (provider.request_id, envelope.request_id),
            (provider.variant, envelope.variant),
            (provider.candidate_ref, envelope.candidate_ref),
            (provider.case_sha256, envelope.case_sha256),
            (provider.maximum_cost_micro_usd, envelope.maximum_cost_micro_usd),
            (provider.maximum_latency_ms, envelope.maximum_latency_ms),
            (provider.redaction_policy_sha256, envelope.redaction_policy_sha256),
        )
        if any(observed != expected for observed, expected in provider_bindings):
            raise ValueError("provider execution bindings do not match benchmark envelope")
        if any(
            receipt.job_id != envelope.job_id
            or receipt.correlation_id != envelope.correlation_id
            or receipt.attempt != envelope.attempt
            or receipt.model != envelope.model_version
            for receipt in provider.usage_receipts
        ):
            raise ValueError("usage receipt does not match benchmark envelope")

        if envelope.variant == "single_agent_baseline" and provider.handoffs:
            raise ValueError("baseline has no handoff authority")
        observed_intents = self._observed_tool_intents(provider.tool_executions)
        terminal = self._parse_terminal(provider)
        cost_micro_usd = _usage_micro_usd(provider.usage_receipts)
        evidence_refs = _unique_refs(
            (
                fence_receipt.evidence_ref,
                *(item.evidence_ref for item in provider.usage_receipts),
                provider.runtime_evidence_ref,
                *((provider.terminal_evidence_ref,) if provider.terminal_evidence_ref else ()),
                *(item.evidence_ref for item in provider.tool_executions),
                *(item.evidence_ref for item in provider.handoffs),
            )
        )
        succeeded = provider.status == "succeeded"
        return BusinessBenchmarkRunReceiptV1(
            schema="captain.business-benchmark-run-receipt.v1",
            run_id=uuid5(NAMESPACE_URL, f"business-benchmark-run:{envelope.idempotency_key}"),
            request_id=envelope.request_id,
            execution_policy_sha256=envelope.execution_policy_sha256,
            runtime_session_id=envelope.runtime_session_id,
            job_id=envelope.job_id,
            correlation_id=envelope.correlation_id,
            subject_version=envelope.subject_version,
            attempt=envelope.attempt,
            suite_ref=envelope.suite_ref,
            suite_id=envelope.suite_id,
            case_id=envelope.case.case_id,
            case_sha256=envelope.case_sha256,
            variant=envelope.variant,
            candidate_ref=envelope.candidate_ref,
            model_version=envelope.model_version,
            allowed_tool_intents=envelope.allowed_tool_intents,
            maximum_cost_micro_usd=envelope.maximum_cost_micro_usd,
            maximum_latency_ms=envelope.maximum_latency_ms,
            status=provider.status,
            observed_decision=terminal.observed_decision if terminal else None,
            observed_rationale_fact_ids=(
                terminal.observed_rationale_fact_ids if terminal else ()
            ),
            observed_tool_intents=observed_intents,
            unsafe_tool_use=bool(set(observed_intents) - set(envelope.allowed_tool_intents)),
            human_handoff_completed=(
                bool(provider.handoffs)
                if succeeded and envelope.variant == "candidate"
                else False if succeeded else None
            ),
            cost_micro_usd=cost_micro_usd,
            latency_ms=latency_ms,
            evidence_refs=evidence_refs,
            completed_at=provider.completed_at,
        )

    async def recover(
        self,
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkRecoveryObservationV1:
        self._require_claim_binding(prepared, claim)
        self._require_fence_binding(prepared, claim, fence_receipt)
        await self._fence_store.assert_current(prepared, claim, fence_receipt)
        observation = await self._runtime_bundle.recover(prepared, claim, fence_receipt)
        if (
            observation.effect_id != prepared.identity.effect_id
            or observation.runtime_session_id != prepared.runtime_session_id
            or observation.claim_id != claim.claim_id
            or observation.fence != claim.fence
            or observation.fence_receipt != fence_receipt
        ):
            raise ValueError("provider recovery is not bound to the exact fence proof")
        return observation

    def _observed_tool_intents(
        self, executions: tuple[FactoryToolExecutionEvidenceV1, ...]
    ) -> tuple[IntegrationIntent, ...]:
        observed: list[IntegrationIntent] = []
        for execution in executions:
            try:
                intent = self._trusted_tool_intents[execution.tool_name]
            except KeyError as exc:
                raise UnsafeBenchmarkToolError(
                    f"unknown tool is unsafe: {execution.tool_name}"
                ) from exc
            if (
                intent is IntegrationIntent.N8N
                and execution.tool_name not in self._approved_n8n_tool_names
            ):
                raise UnsafeBenchmarkToolError(
                    "n8n intent requires an injected Captain-approved tool port"
                )
            if intent not in observed:
                observed.append(intent)
        return tuple(observed)

    @staticmethod
    def _parse_terminal(
        provider: ProviderBenchmarkExecutionV1,
    ) -> BenchmarkTerminalOutputV1 | None:
        if provider.status != "succeeded":
            return None
        assert provider.terminal_output is not None
        try:
            raw = json.loads(provider.terminal_output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("provider terminal output is not strict JSON") from exc
        return BenchmarkTerminalOutputV1.model_validate(raw)

    @staticmethod
    def _baseline_policy(
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
    ) -> BaselineAssistantPolicyV1:
        safe_case = envelope.case.case_id.replace("-", "_")
        return BaselineAssistantPolicyV1(
            schema="captain.business-benchmark-baseline-assistant-policy.v1",
            agent_name=f"baseline_{safe_case}",
        )

    @staticmethod
    def _require_claim_binding(
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> None:
        if claim.prepared_effect != prepared:
            raise ValueError("benchmark claim does not match prepared effect")

    @staticmethod
    def _require_fence_binding(
        prepared: BusinessBenchmarkPreparedEffectV1,
        claim: BusinessBenchmarkEffectClaimV1,
        receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> None:
        if (
            receipt.effect_id != prepared.identity.effect_id
            or receipt.runtime_session_id != prepared.runtime_session_id
            or receipt.claim_id != claim.claim_id
            or receipt.fence != claim.fence
        ):
            raise ValueError("provider fence receipt does not match claim")


class LiveBusinessBenchmarkSettings(_FrozenModel):
    profile: Literal["claims", "renewal", "all"]
    provider: str
    model: str
    suite_version: int = Field(ge=1, strict=True)
    candidate_id: str
    job_id: UUID
    maximum_usd: Decimal
    captain_remaining_usd: Decimal
    allowed_models: tuple[str, ...]
    evidence_root: Path
    runtime_url: str
    provider_secret_name: str

    @property
    def execution_count(self) -> int:
        return 60 if self.profile == "all" else 30

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        repository_root: Path | None = None,
    ) -> "LiveBusinessBenchmarkSettings":
        maximum = _positive_decimal(
            _required(environment, "CAPTAIN_BENCHMARK_MAX_USD"),
            "maximum benchmark cost",
        )
        profile = _required(environment, "CAPTAIN_BENCHMARK_PROFILE")
        if profile not in {"claims", "renewal", "all"}:
            raise ValueError("benchmark profile must be claims, renewal, or all")
        provider = _required(environment, "CAPTAIN_BENCHMARK_PROVIDER")
        model = _required(environment, "CAPTAIN_BENCHMARK_MODEL")
        try:
            suite_version = int(_required(environment, "CAPTAIN_BENCHMARK_SUITE_VERSION"))
        except ValueError as exc:
            raise ValueError("benchmark suite version must be a positive integer") from exc
        if suite_version <= 0:
            raise ValueError("benchmark suite version must be a positive integer")
        remaining = _positive_decimal(
            _required(environment, "CAPTAIN_JOB_REMAINING_USD"),
            "remaining Captain budget",
        )
        if maximum > remaining:
            raise ValueError("maximum benchmark cost exceeds remaining Captain budget")
        allowed_models = tuple(
            item.strip()
            for item in _required(environment, "CAPTAIN_JOB_ALLOWED_MODELS").split(",")
            if item.strip()
        )
        if not allowed_models or len(allowed_models) != len(set(allowed_models)):
            raise ValueError("allowed model list is empty or duplicated")
        if model not in allowed_models:
            raise ValueError("benchmark model is not an allowed model")
        expected_secret = _PROVIDER_SECRET_NAMES.get(provider)
        if expected_secret is None:
            raise ValueError("benchmark provider is unsupported")
        secret_name = _required(environment, "CAPTAIN_BENCHMARK_PROVIDER_SECRET")
        if secret_name != expected_secret:
            raise ValueError("provider secret name is not allowlisted")
        root = (repository_root or Path.cwd()).resolve()
        configured_root = Path(
            _required(environment, "CAPTAIN_BENCHMARK_EVIDENCE_ROOT")
        )
        evidence_root = (
            configured_root if configured_root.is_absolute() else root / configured_root
        ).resolve()
        safe_root = (root / _EVIDENCE_RELATIVE_ROOT).resolve()
        if not evidence_root.is_relative_to(safe_root):
            raise ValueError("benchmark evidence root must stay below the safe evidence root")
        return cls(
            profile=profile,
            provider=provider,
            model=model,
            suite_version=suite_version,
            candidate_id=_required(environment, "CAPTAIN_BENCHMARK_CANDIDATE_ID"),
            job_id=UUID(_required(environment, "CAPTAIN_BENCHMARK_JOB_ID")),
            maximum_usd=maximum,
            captain_remaining_usd=remaining,
            allowed_models=allowed_models,
            evidence_root=evidence_root,
            runtime_url=_required(environment, "CAPTAIN_RUNTIME_URL"),
            provider_secret_name=secret_name,
        )


class BusinessBenchmarkPreflightReceiptV1(_FrozenModel):
    profile: Literal["claims", "renewal", "all"]
    execution_count: Literal[30, 60]
    model: str
    maximum_usd: Decimal
    evidence_root: Path
    runtime_healthy: Literal[True] = True
    provider_secret_present: Literal[True] = True
    production_bundle_present: Literal[True] = True


class LiveBusinessBenchmarkPreflight:
    def __init__(
        self,
        *,
        health_check: Callable[[str], Awaitable[bool]],
        runtime_bundle: ProductionBusinessBenchmarkRuntimeBundlePort | None,
    ) -> None:
        self._health_check = health_check
        self._runtime_bundle = runtime_bundle

    async def validate_environment(
        self,
        environment: Mapping[str, str],
        *,
        repository_root: Path | None = None,
    ) -> BusinessBenchmarkPreflightReceiptV1:
        # Deterministic budget/model/path validation always runs before health or effects.
        settings = LiveBusinessBenchmarkSettings.from_environment(
            environment, repository_root=repository_root
        )
        if not environment.get(settings.provider_secret_name, "").strip():
            raise ValueError("provider secret is not present")
        if not await self._health_check(settings.runtime_url):
            raise ValueError("Captain runtime health check failed")
        if self._runtime_bundle is None:
            raise ProductionAdapterUnavailableError()
        return BusinessBenchmarkPreflightReceiptV1(
            profile=settings.profile,
            execution_count=settings.execution_count,
            model=settings.model,
            maximum_usd=settings.maximum_usd,
            evidence_root=settings.evidence_root,
        )


def _usage_micro_usd(receipts: tuple[FactoryUsageReceiptV1, ...]) -> int:
    total = sum((receipt.cost_usd for receipt in receipts), start=Decimal(0))
    micro = total * Decimal(1_000_000)
    if micro != micro.to_integral_value():
        raise ValueError("provider usage cost is not integral micro-USD")
    return int(micro)


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    result: list[ArtifactRef] = []
    seen: set[tuple[str, str]] = set()
    for reference in references:
        key = (reference.uri, reference.sha256)
        if key not in seen:
            result.append(reference)
            seen.add(key)
    return tuple(result)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required benchmark setting is missing: {name}")
    return value


def _positive_decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be an exact decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed
