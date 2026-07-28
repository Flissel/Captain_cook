"""Fail-closed provider boundary for paired business benchmark execution.

The coordinator in :mod:`business_benchmark_execution` owns replay and claim
orchestration.  This module only binds an already claimed effect to an injected
production runtime bundle and an injected durable provider fence store.
"""

from __future__ import annotations

import hashlib
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
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.team_execution import (
    FactoryHandoffEvidenceV1,
    FactoryN8nExecutionEvidenceV1,
    FactoryToolExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


_SAFE_FACT_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_AGENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_RELATIVE_ROOT = Path(".captain-cook/evidence/business-benchmarks")
_PROVIDER_SECRET_NAMES = {"openai": "OPENAI_API_KEY"}
_DEFAULT_REDACTION_POLICY_SHA256 = hashlib.sha256(
    b"captain-business-benchmark-redaction-policy-v1"
).hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProductionAdapterUnavailableError(RuntimeError):
    """The real runtime bundle required for a provider effect is unavailable."""

    code = "TODO_TOOL.v1"

    def __init__(self, detail: str = "production benchmark adapter bundle is unavailable") -> None:
        super().__init__(f"{self.code}: {detail}")


class UnsafeBenchmarkToolError(ValueError):
    """A provider run observed a tool without a trusted intent binding."""


class BenchmarkEvidenceBindingV1(_FrozenModel):
    """Exact effect/fence identity carried by every nested provider receipt."""

    request_id: UUID
    runtime_session_id: str = Field(min_length=1)
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant: Literal["candidate", "single_agent_baseline"]
    effect_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fence: int = Field(ge=1, strict=True)

    @classmethod
    def from_execution(
        cls,
        envelope: BusinessBenchmarkExecutionEnvelopeV1,
        claim: BusinessBenchmarkEffectClaimV1,
    ) -> "BenchmarkEvidenceBindingV1":
        return cls(
            request_id=envelope.request_id,
            runtime_session_id=envelope.runtime_session_id,
            case_sha256=envelope.case_sha256,
            variant=envelope.variant,
            effect_id=claim.prepared_effect.identity.effect_id,
            fence=claim.fence,
        )


class BoundBenchmarkUsageEvidenceV1(_FrozenModel):
    binding: BenchmarkEvidenceBindingV1
    receipt: FactoryUsageReceiptV1


class BoundBenchmarkToolEvidenceV1(_FrozenModel):
    binding: BenchmarkEvidenceBindingV1
    execution: FactoryToolExecutionEvidenceV1
    n8n_execution: FactoryN8nExecutionEvidenceV1 | None = None

    @model_validator(mode="after")
    def bind_n8n_tool_name(self) -> "BoundBenchmarkToolEvidenceV1":
        if (
            self.n8n_execution is not None
            and self.n8n_execution.tool_name != self.execution.tool_name
        ):
            raise ValueError("typed n8n evidence belongs to a different tool")
        return self


class BoundBenchmarkHandoffEvidenceV1(_FrozenModel):
    binding: BenchmarkEvidenceBindingV1
    handoff: FactoryHandoffEvidenceV1
    authority: Literal["captain_human_review"] | None = None
    status: Literal["observed", "completed"]

    @model_validator(mode="after")
    def bind_completed_human_review(self) -> "BoundBenchmarkHandoffEvidenceV1":
        if self.status == "completed" and (
            self.authority != "captain_human_review"
            or self.handoff.to_agent != "human_review"
        ):
            raise ValueError(
                "completed handoff requires the Captain human-review authority"
            )
        if self.status == "observed" and self.authority is not None:
            raise ValueError("observed internal handoff cannot claim review authority")
        return self


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
    usage_receipts: tuple[BoundBenchmarkUsageEvidenceV1, ...] = ()
    runtime_evidence_ref: ArtifactRef
    terminal_evidence_ref: ArtifactRef | None = None
    tool_executions: tuple[BoundBenchmarkToolEvidenceV1, ...] = ()
    handoffs: tuple[BoundBenchmarkHandoffEvidenceV1, ...] = ()
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
    ) -> None:
        self._runtime_bundle = runtime_bundle
        self._fence_store = fence_store
        self._trusted_tool_intents = dict(trusted_tool_intents)
        self._monotonic_clock = monotonic_clock
        self._clock = clock

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
            item.receipt.job_id != envelope.job_id
            or item.receipt.correlation_id != envelope.correlation_id
            or item.receipt.attempt != envelope.attempt
            or item.receipt.model != envelope.model_version
            for item in provider.usage_receipts
        ):
            raise ValueError("usage receipt does not match benchmark envelope")

        expected_binding = BenchmarkEvidenceBindingV1.from_execution(envelope, claim)
        nested_bindings = (
            *(item.binding for item in provider.usage_receipts),
            *(item.binding for item in provider.tool_executions),
            *(item.binding for item in provider.handoffs),
        )
        if any(binding != expected_binding for binding in nested_bindings):
            raise ValueError("nested provider evidence binding does not match effect and fence")

        if envelope.variant == "single_agent_baseline" and provider.handoffs:
            raise ValueError("baseline has no handoff authority")
        observed_intents = self._observed_tool_intents(provider.tool_executions)
        human_handoff_completed = self._human_handoff_completed(provider.handoffs)
        terminal = self._parse_terminal(provider)
        cost_micro_usd = _usage_micro_usd(
            tuple(item.receipt for item in provider.usage_receipts)
        )
        evidence_refs = _unique_refs(
            (
                fence_receipt.evidence_ref,
                *(item.receipt.evidence_ref for item in provider.usage_receipts),
                provider.runtime_evidence_ref,
                *((provider.terminal_evidence_ref,) if provider.terminal_evidence_ref else ()),
                *(item.execution.evidence_ref for item in provider.tool_executions),
                *(item.handoff.evidence_ref for item in provider.handoffs),
                *(
                    item.n8n_execution.evidence_ref
                    for item in provider.tool_executions
                    if item.n8n_execution is not None
                ),
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
                human_handoff_completed
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
        self, executions: tuple[BoundBenchmarkToolEvidenceV1, ...]
    ) -> tuple[IntegrationIntent, ...]:
        observed: list[IntegrationIntent] = []
        for item in executions:
            execution = item.execution
            try:
                intent = self._trusted_tool_intents[execution.tool_name]
            except KeyError as exc:
                raise UnsafeBenchmarkToolError(
                    f"unknown tool is unsafe: {execution.tool_name}"
                ) from exc
            if intent is IntegrationIntent.N8N and item.n8n_execution is None:
                raise UnsafeBenchmarkToolError(
                    "n8n intent requires a typed n8n grant command result chain"
                )
            if intent is not IntegrationIntent.N8N and item.n8n_execution is not None:
                raise UnsafeBenchmarkToolError(
                    "typed n8n evidence cannot authorize a non-n8n tool intent"
                )
            if intent not in observed:
                observed.append(intent)
        return tuple(observed)

    @staticmethod
    def _human_handoff_completed(
        handoffs: tuple[BoundBenchmarkHandoffEvidenceV1, ...]
    ) -> bool:
        for item in handoffs:
            if item.status == "completed" and (
                item.authority != "captain_human_review"
                or item.handoff.to_agent != "human_review"
            ):
                raise ValueError(
                    "completed handoff is not bound to the human-review authority"
                )
        return any(item.status == "completed" for item in handoffs)

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


class BusinessBenchmarkTeamSelectionV1(_FrozenModel):
    """One Captain-authorized business team and its isolated job budget."""

    profile: Literal["claims", "renewal"]
    job_id: UUID
    candidate_id: str = Field(min_length=1)
    suite_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    maximum_usd: Decimal = Field(gt=0)
    captain_remaining_usd: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def require_available_job_budget(self) -> "BusinessBenchmarkTeamSelectionV1":
        if self.maximum_usd > self.captain_remaining_usd:
            raise ValueError("maximum benchmark cost exceeds remaining Captain budget")
        return self


class LiveBusinessBenchmarkSettings(_FrozenModel):
    profile: Literal["claims", "renewal", "all"]
    provider: str
    model: str
    redaction_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selections: tuple[BusinessBenchmarkTeamSelectionV1, ...] = Field(
        min_length=1, max_length=2
    )
    maximum_usd: Decimal
    allowed_models: tuple[str, ...]
    evidence_root: Path
    runtime_url: str
    provider_secret_name: str

    @model_validator(mode="after")
    def require_exact_profile_and_budget_scope(self) -> "LiveBusinessBenchmarkSettings":
        expected_profiles = (
            ("claims", "renewal") if self.profile == "all" else (self.profile,)
        )
        if tuple(selection.profile for selection in self.selections) != expected_profiles:
            raise ValueError("benchmark selections do not match selected profiles")
        if len({selection.job_id for selection in self.selections}) != len(
            self.selections
        ):
            raise ValueError("business benchmark teams require distinct job identities")
        if len({selection.candidate_id for selection in self.selections}) != len(
            self.selections
        ):
            raise ValueError("business benchmark teams require distinct candidate identities")
        if sum(
            (selection.maximum_usd for selection in self.selections), Decimal(0)
        ) != self.maximum_usd:
            raise ValueError(
                "aggregate benchmark budget must equal the selected team budgets"
            )
        return self

    @property
    def execution_count(self) -> int:
        return len(self.selections) * 30

    def selection_for(
        self, profile: Literal["claims", "renewal"]
    ) -> BusinessBenchmarkTeamSelectionV1:
        for selection in self.selections:
            if selection.profile == profile:
                return selection
        raise ValueError(f"business benchmark profile is not selected: {profile}")

    def for_selection(
        self, selection: BusinessBenchmarkTeamSelectionV1
    ) -> "LiveBusinessBenchmarkSettings":
        if selection not in self.selections:
            raise ValueError("team selection is not authorized by aggregate settings")
        return self.model_copy(
            update={
                "profile": selection.profile,
                "selections": (selection,),
                "maximum_usd": selection.maximum_usd,
            }
        )

    @property
    def suite_version(self) -> int:
        return self._single_selection().suite_version

    @property
    def candidate_id(self) -> str:
        return self._single_selection().candidate_id

    @property
    def job_id(self) -> UUID:
        return self._single_selection().job_id

    @property
    def captain_remaining_usd(self) -> Decimal:
        return self._single_selection().captain_remaining_usd

    def _single_selection(self) -> BusinessBenchmarkTeamSelectionV1:
        if len(self.selections) != 1:
            raise ValueError("aggregate benchmark settings have no single team identity")
        return self.selections[0]

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
        selections = (
            tuple(
                _team_selection_from_environment(environment, selected_profile)
                for selected_profile in ("claims", "renewal")
            )
            if profile == "all"
            else (
                _team_selection_from_environment(
                    environment,
                    profile,
                    allow_generic=True,
                    aggregate_maximum=maximum,
                ),
            )
        )
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
            redaction_policy_sha256=environment.get(
                "CAPTAIN_BENCHMARK_REDACTION_POLICY_SHA256",
                _DEFAULT_REDACTION_POLICY_SHA256,
            ).strip()
            or _DEFAULT_REDACTION_POLICY_SHA256,
            selections=selections,
            maximum_usd=maximum,
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


class BusinessBenchmarkFinalizedReceiptV1(_FrozenModel):
    """One finalized receipt with explicit selected business-profile binding."""

    profile: Literal["claims", "renewal"]
    receipt: BusinessBenchmarkRunReceiptV1
    receipt_ref: ArtifactRef

    @model_validator(mode="after")
    def require_successful_final_receipt(self) -> "BusinessBenchmarkFinalizedReceiptV1":
        if self.receipt.status != "succeeded":
            raise ValueError("provider-live receipt must be finalized successfully")
        return self


class BusinessBenchmarkExpectedCaseV1(_FrozenModel):
    """Canonical private-case identity without the private case body."""

    case_id: str = Field(min_length=1)
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BusinessBenchmarkExpectedSuiteV1(_FrozenModel):
    """Authoritative suite identity supplied by production composition."""

    profile: Literal["claims", "renewal"]
    suite_id: str = Field(min_length=1)
    suite_version: int = Field(ge=1, strict=True)
    suite_ref: PrivateHoldoutRef
    cases: tuple[BusinessBenchmarkExpectedCaseV1, ...] = Field(
        min_length=15, max_length=15
    )

    @model_validator(mode="after")
    def require_unique_case_identities(self) -> "BusinessBenchmarkExpectedSuiteV1":
        identities = tuple((item.case_id, item.case_sha256) for item in self.cases)
        if len(set(identities)) != 15 or len({item.case_id for item in self.cases}) != 15:
            raise ValueError("authoritative suite requires 15 unique case identities")
        return self


class BusinessBenchmarkExpectedScopeV1(_FrozenModel):
    """Captain-authoritative job, candidate, and digest-only suite scope."""

    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    model_version: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_ref: ArtifactRef
    suites: tuple[BusinessBenchmarkExpectedSuiteV1, ...] = Field(
        min_length=1, max_length=1
    )

    @model_validator(mode="after")
    def require_unique_profiles(self) -> "BusinessBenchmarkExpectedScopeV1":
        profiles = tuple(item.profile for item in self.suites)
        if len(profiles) != len(set(profiles)):
            raise ValueError("authoritative scope contains duplicate suite profiles")
        return self


class BusinessBenchmarkLiveRunResultV1(_FrozenModel):
    """Final provider-live receipts plus Captain summary/evidence references."""

    profile: Literal["claims", "renewal", "all"]
    selections: tuple[BusinessBenchmarkTeamSelectionV1, ...] = Field(
        min_length=1, max_length=2
    )
    receipts: tuple[BusinessBenchmarkFinalizedReceiptV1, ...]
    summary_refs: tuple[ArtifactRef, ...]
    evidence_refs: tuple[ArtifactRef, ...]
    completed_at: datetime

    @model_validator(mode="after")
    def require_complete_finalized_scope(self) -> "BusinessBenchmarkLiveRunResultV1":
        expected_profiles = (
            ("claims", "renewal") if self.profile == "all" else (self.profile,)
        )
        if tuple(selection.profile for selection in self.selections) != expected_profiles:
            raise ValueError("live result selections do not match selected profiles")
        per_variant = 30 if self.profile == "all" else 15
        expected_total = per_variant * 2
        candidate = tuple(
            item for item in self.receipts if item.receipt.variant == "candidate"
        )
        baseline = tuple(
            item
            for item in self.receipts
            if item.receipt.variant == "single_agent_baseline"
        )
        if (
            len(self.receipts) != expected_total
            or len(candidate) != per_variant
            or len(baseline) != per_variant
            or any(item.receipt.status != "succeeded" for item in self.receipts)
        ):
            raise ValueError(
                "live run requires exact finalized candidate and baseline receipts"
            )
        if len({item.receipt.run_id for item in self.receipts}) != expected_total:
            raise ValueError("finalized live receipt IDs must be unique")
        if len(
            {
                (item.profile, item.receipt.case_id, item.receipt.variant)
                for item in self.receipts
            }
        ) != expected_total:
            raise ValueError("live receipts must cover each case and variant exactly once")
        for profile in expected_profiles:
            for variant in ("candidate", "single_agent_baseline"):
                if (
                    sum(
                        item.profile == profile and item.receipt.variant == variant
                        for item in self.receipts
                    )
                    != 15
                ):
                    raise ValueError(
                        "live receipts must cover 15 cases per selected profile and variant"
                    )
        if any(item.profile not in expected_profiles for item in self.receipts):
            raise ValueError("live receipt names an unselected business profile")
        expected_summaries = 2 if self.profile == "all" else 1
        if len(self.summary_refs) != expected_summaries:
            raise ValueError("live run requires one Captain summary per selected profile")
        if len(set(self.summary_refs)) != expected_summaries:
            raise ValueError("live run requires distinct Captain summaries per profile")
        required_evidence = {
            *(item.receipt_ref for item in self.receipts),
            *(
                reference
                for item in self.receipts
                for reference in item.receipt.evidence_refs
            ),
            *self.summary_refs,
        }
        if not required_evidence.issubset(set(self.evidence_refs)):
            raise ValueError("live run evidence must include every receipt and summary reference")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("live run evidence references must be deduplicated")
        return self


class ProductionBusinessBenchmarkCompositionPort(Protocol):
    """Production wiring for runtime bundle, durable fence store, and full run."""

    runtime_bundle: ProductionBusinessBenchmarkRuntimeBundlePort
    fence_store: DurableProviderFencePort
    expected_scopes: tuple[BusinessBenchmarkExpectedScopeV1, ...]

    async def health_check(self, url: str) -> bool: ...

    async def run(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> BusinessBenchmarkLiveRunResultV1: ...


BusinessBenchmarkCompositionLoader = Callable[
    [LiveBusinessBenchmarkSettings], ProductionBusinessBenchmarkCompositionPort
]


def load_production_business_benchmark_composition(
    settings: LiveBusinessBenchmarkSettings,
) -> ProductionBusinessBenchmarkCompositionPort:
    """Fail closed until the real adapter/capability bundle is integrated."""

    del settings
    raise ProductionAdapterUnavailableError(
        "production_adapter_bundle and capability live bridges are not integrated"
    )


async def run_provider_business_benchmarks(
    environment: Mapping[str, str],
    *,
    repository_root: Path | None = None,
    composition_loader: BusinessBenchmarkCompositionLoader = (
        load_production_business_benchmark_composition
    ),
) -> BusinessBenchmarkLiveRunResultV1:
    """Load production wiring, preflight it, execute, and validate full scope."""

    settings = LiveBusinessBenchmarkSettings.from_environment(
        environment, repository_root=repository_root
    )
    if not environment.get(settings.provider_secret_name, "").strip():
        raise ValueError("provider secret is not present")
    composition = composition_loader(settings)
    runtime_bundle = getattr(composition, "runtime_bundle", None)
    if runtime_bundle is None or any(
        not callable(getattr(runtime_bundle, method, None))
        for method in ("prepare", "execute", "recover")
    ):
        raise ProductionAdapterUnavailableError("runtime bundle is unavailable")
    fence_store = getattr(composition, "fence_store", None)
    if fence_store is None or any(
        not callable(getattr(fence_store, method, None))
        for method in ("register_fence", "assert_current")
    ):
        raise ProductionAdapterUnavailableError("durable provider fence store is unavailable")
    expected_scopes = _load_expected_scopes(composition, settings)
    _validate_expected_scope_settings(expected_scopes, settings)
    await LiveBusinessBenchmarkPreflight(
        health_check=composition.health_check,
        runtime_bundle=composition.runtime_bundle,
    ).validate_environment(environment, repository_root=repository_root)
    scopes_by_profile = {scope.suites[0].profile: scope for scope in expected_scopes}
    team_results: list[BusinessBenchmarkLiveRunResultV1] = []
    for selection in settings.selections:
        team_settings = settings.for_selection(selection)
        raw = await composition.run(team_settings)
        result = BusinessBenchmarkLiveRunResultV1.model_validate(
            raw.model_dump(mode="python") if isinstance(raw, BaseModel) else raw
        )
        if result.profile != selection.profile:
            raise ValueError("live result profile does not match selected settings")
        if result.selections != (selection,):
            raise ValueError("live result selections do not match configured team selections")
        _validate_result_against_expected_scope(
            result, (scopes_by_profile[selection.profile],), team_settings
        )
        team_results.append(result)
    aggregate = _aggregate_team_results(settings, tuple(team_results))
    _validate_result_against_expected_scope(aggregate, expected_scopes, settings)
    return aggregate


def _aggregate_team_results(
    settings: LiveBusinessBenchmarkSettings,
    results: tuple[BusinessBenchmarkLiveRunResultV1, ...],
) -> BusinessBenchmarkLiveRunResultV1:
    if len(results) == 1:
        return results[0]
    receipts = tuple(item for result in results for item in result.receipts)
    summaries = tuple(item for result in results for item in result.summary_refs)
    evidence = _unique_refs(
        tuple(item for result in results for item in result.evidence_refs)
    )
    return BusinessBenchmarkLiveRunResultV1(
        profile=settings.profile,
        selections=settings.selections,
        receipts=receipts,
        summary_refs=summaries,
        evidence_refs=evidence,
        completed_at=max(result.completed_at for result in results),
    )


def _load_expected_scopes(
    composition: ProductionBusinessBenchmarkCompositionPort,
    settings: LiveBusinessBenchmarkSettings,
) -> tuple[BusinessBenchmarkExpectedScopeV1, ...]:
    raw_scopes = getattr(composition, "expected_scopes", None)
    if raw_scopes is None and settings.profile != "all":
        legacy_scope = getattr(composition, "expected_scope", None)
        raw_scopes = (legacy_scope,) if legacy_scope is not None else None
    if raw_scopes is None:
        raise ProductionAdapterUnavailableError(
            "authoritative expected team scopes are unavailable"
        )
    try:
        return tuple(
            BusinessBenchmarkExpectedScopeV1.model_validate(
                item.model_dump(mode="python") if isinstance(item, BaseModel) else item
            )
            for item in raw_scopes
        )
    except TypeError as exc:
        raise ProductionAdapterUnavailableError(
            "authoritative expected team scopes are unavailable"
        ) from exc


def _validate_expected_scope_settings(
    scopes: tuple[BusinessBenchmarkExpectedScopeV1, ...],
    settings: LiveBusinessBenchmarkSettings,
) -> None:
    if len(scopes) != len(settings.selections):
        raise ValueError(
            "authoritative benchmark scopes do not match configured team selections"
        )
    by_profile: dict[str, BusinessBenchmarkExpectedScopeV1] = {}
    for scope in scopes:
        profile = scope.suites[0].profile
        if profile in by_profile:
            raise ValueError("authoritative benchmark scopes contain duplicate profiles")
        by_profile[profile] = scope
    if set(by_profile) != {selection.profile for selection in settings.selections}:
        raise ValueError(
            "authoritative benchmark scopes do not match configured team selections"
        )
    if len({scope.candidate_ref for scope in scopes}) != len(scopes):
        raise ValueError("business benchmark teams require distinct candidate references")
    if len({scope.correlation_id for scope in scopes}) != len(scopes):
        raise ValueError("business benchmark teams require distinct correlation identities")
    for selection in settings.selections:
        scope = by_profile[selection.profile]
        suite = scope.suites[0]
        if (
            scope.job_id != selection.job_id
            or scope.attempt != selection.attempt
            or scope.model_version != settings.model
            or scope.candidate_id != selection.candidate_id
            or suite.suite_version != selection.suite_version
        ):
            raise ValueError(
                "authoritative benchmark scope does not match configured team selection"
            )


def _validate_result_against_expected_scope(
    result: BusinessBenchmarkLiveRunResultV1,
    scopes: tuple[BusinessBenchmarkExpectedScopeV1, ...],
    settings: LiveBusinessBenchmarkSettings,
) -> None:
    scopes_by_profile = {scope.suites[0].profile: scope for scope in scopes}
    observed: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for item in result.receipts:
        receipt = item.receipt
        scope = scopes_by_profile[item.profile]
        suite = scope.suites[0]
        expected_candidate_ref = (
            scope.candidate_ref if receipt.variant == "candidate" else None
        )
        if (
            receipt.job_id != scope.job_id
            or receipt.correlation_id != scope.correlation_id
            or receipt.subject_version != scope.subject_version
            or receipt.attempt != scope.attempt
            or receipt.model_version != scope.model_version
            or receipt.suite_id != suite.suite_id
            or receipt.suite_ref != suite.suite_ref
            or receipt.candidate_ref != expected_candidate_ref
        ):
            raise ValueError("live receipt does not match authoritative benchmark scope")
        case_identity = (receipt.case_id, receipt.case_sha256)
        if case_identity not in {
            (expected.case_id, expected.case_sha256) for expected in suite.cases
        }:
            raise ValueError("live receipt does not match authoritative benchmark scope")
        observed.setdefault((item.profile, receipt.variant), set()).add(case_identity)

    for scope in scopes:
        suite = scope.suites[0]
        expected_cases = {
            (expected.case_id, expected.case_sha256) for expected in suite.cases
        }
        for variant in ("candidate", "single_agent_baseline"):
            if observed.get((suite.profile, variant), set()) != expected_cases:
                raise ValueError("live receipt does not match authoritative benchmark scope")

    for selection in settings.selections:
        team_maximum_micro_usd = _maximum_micro_usd(
            selection.maximum_usd, f"{selection.profile} team"
        )
        team_cost = sum(
            item.receipt.cost_micro_usd
            for item in result.receipts
            if item.profile == selection.profile
        )
        if team_cost > team_maximum_micro_usd:
            raise ValueError(
                f"{selection.profile} team cost exceeds configured benchmark maximum"
            )
    maximum_micro_usd = _maximum_micro_usd(settings.maximum_usd, "aggregate")
    if sum(item.receipt.cost_micro_usd for item in result.receipts) > maximum_micro_usd:
        raise ValueError("live receipt cost exceeds configured benchmark maximum")


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


def _maximum_micro_usd(value: Decimal, label: str) -> int:
    micro = value * Decimal(1_000_000)
    if micro != micro.to_integral_value():
        raise ValueError(f"{label} benchmark budget is not integral micro-USD")
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


def _team_selection_from_environment(
    environment: Mapping[str, str],
    profile: Literal["claims", "renewal"],
    *,
    allow_generic: bool = False,
    aggregate_maximum: Decimal | None = None,
) -> BusinessBenchmarkTeamSelectionV1:
    prefix = f"CAPTAIN_BENCHMARK_{profile.upper()}_"

    def selected(name: str, *, default: str | None = None) -> str:
        prefixed_name = prefix + name
        if prefixed_name in environment and environment[prefixed_name].strip():
            return environment[prefixed_name].strip()
        if allow_generic:
            generic_name = (
                "CAPTAIN_JOB_REMAINING_USD"
                if name == "REMAINING_USD"
                else f"CAPTAIN_BENCHMARK_{name}"
            )
            if generic_name in environment and environment[generic_name].strip():
                return environment[generic_name].strip()
            if default is not None:
                return default
            raise ValueError(f"required benchmark setting is missing: {generic_name}")
        raise ValueError(f"required benchmark setting is missing: {prefixed_name}")

    maximum = (
        aggregate_maximum
        if allow_generic and aggregate_maximum is not None
        else _positive_decimal(selected("MAX_USD"), f"{profile} maximum benchmark cost")
    )
    remaining = _positive_decimal(
        selected("REMAINING_USD"), f"{profile} remaining Captain budget"
    )
    try:
        suite_version = int(selected("SUITE_VERSION"))
    except ValueError as exc:
        raise ValueError(
            f"{profile} benchmark suite version must be a positive integer"
        ) from exc
    try:
        attempt = int(selected("ATTEMPT", default="1"))
    except ValueError as exc:
        raise ValueError(f"{profile} benchmark attempt must be an integer") from exc
    try:
        job_id = UUID(selected("JOB_ID"))
    except ValueError as exc:
        raise ValueError(f"{profile} benchmark job ID must be a UUID") from exc
    return BusinessBenchmarkTeamSelectionV1(
        profile=profile,
        job_id=job_id,
        candidate_id=selected("CANDIDATE_ID"),
        suite_version=suite_version,
        attempt=attempt,
        maximum_usd=maximum,
        captain_remaining_usd=remaining,
    )


def _positive_decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be an exact decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed
