from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.business_benchmark_execution import (
    BusinessBenchmarkExecutionEnvelopeV1,
)
from agenten.agent_factory.business_benchmark_live import (
    BaselineAssistantPolicyV1,
    BenchmarkEvidenceBindingV1,
    BoundBenchmarkHandoffEvidenceV1,
    BoundBenchmarkToolEvidenceV1,
    BoundBenchmarkUsageEvidenceV1,
    BusinessBenchmarkLiveAdapter,
    LiveBusinessBenchmarkPreflight,
    LiveBusinessBenchmarkSettings,
    ProductionAdapterUnavailableError,
    ProviderBenchmarkExecutionV1,
    UnsafeBenchmarkToolError,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkEffectClaimV1,
    BusinessBenchmarkEffectIdentityV1,
    BusinessBenchmarkFenceReceiptV1,
    BusinessBenchmarkPreparedEffectV1,
    BusinessBenchmarkRecoveryObservationV1,
    BusinessBenchmarkRuntimePreparationV1,
)
from agenten.agent_factory.execution_budget import FactoryUsageReceiptV1
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.team_execution import (
    FactoryHandoffEvidenceV1,
    FactoryToolExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


NOW = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000701")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000702")
CLAIM_ID = UUID("00000000-0000-0000-0000-000000000703")


def artifact(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-live/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def envelope(variant: str = "candidate") -> BusinessBenchmarkExecutionEnvelopeV1:
    case = BusinessBenchmarkCaseV1(
        schema="captain.business-benchmark-case.v1",
        case_id="claims-ordinary-01",
        profile_id="insurance_claims_resolution_swarm",
        category="ordinary",
        redacted_input={"test_organization_id": "test-org"},
        expected_decision="route_standard_review",
        required_rationale_fact_ids=("fact-policy-state",),
        allowed_tool_intents=(IntegrationIntent.NONE,),
        human_handoff_required=False,
        severity="normal",
    )
    case_sha256 = hashlib.sha256(
        json.dumps(
            case.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    suite_digest = hashlib.sha256(b"private-suite").hexdigest()
    return BusinessBenchmarkExecutionEnvelopeV1(
        schema="captain.business-benchmark-execution-envelope.v1",
        request_id=uuid5(NAMESPACE_URL, f"request:{variant}"),
        idempotency_key=hashlib.sha256(variant.encode()).hexdigest(),
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=3,
        attempt=2,
        suite_ref=PrivateHoldoutRef(
            holdout_id=f"holdout-{suite_digest[:12]}",
            uri=f"holdout://holdout-{suite_digest[:12]}",
            sha256=suite_digest,
        ),
        suite_id="claims-suite-v2",
        case=case,
        case_sha256=case_sha256,
        variant=variant,
        candidate_ref=artifact("sealed-candidate") if variant == "candidate" else None,
        model_version="gpt-5-business-v1",
        allowed_tool_intents=(IntegrationIntent.NONE,),
        maximum_cost_micro_usd=500_000,
        maximum_latency_ms=2_000,
        redaction_policy_sha256="a" * 64,
        execution_policy_sha256="b" * 64,
        variant_policy_sha256="c" * 64,
        runtime_session_id=f"benchmark-session-{variant}-{'d' * 64}",
        evaluation_only=variant == "single_agent_baseline",
    )


def prepared(env: BusinessBenchmarkExecutionEnvelopeV1) -> BusinessBenchmarkPreparedEffectV1:
    identity = BusinessBenchmarkEffectIdentityV1.create(
        request_id=env.request_id,
        job_id=env.job_id,
        correlation_id=env.correlation_id,
        subject_version=env.subject_version,
        attempt=env.attempt,
        suite_ref=env.suite_ref,
        suite_id=env.suite_id,
        case_id=env.case.case_id,
        variant=env.variant,
        execution_policy_sha256=env.execution_policy_sha256,
        variant_policy_sha256=env.variant_policy_sha256,
    )
    return BusinessBenchmarkPreparedEffectV1(
        schema="captain.business-benchmark-prepared-effect.v1",
        identity=identity,
        runtime_session_id=env.runtime_session_id,
    )


def claim(env: BusinessBenchmarkExecutionEnvelopeV1) -> BusinessBenchmarkEffectClaimV1:
    item = prepared(env)
    fingerprint = hashlib.sha256(
        json.dumps(
            {"claim_id": str(CLAIM_ID), "effect_id": item.identity.effect_id, "fence": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return BusinessBenchmarkEffectClaimV1(
        schema="captain.business-benchmark-effect-claim.v1",
        claim_id=CLAIM_ID,
        claim_fingerprint=fingerprint,
        fence=1,
        acquired_at=NOW,
        expires_at=datetime(2026, 7, 27, 8, 5, tzinfo=timezone.utc),
        prepared_effect=item,
    )


def usage(env: BusinessBenchmarkExecutionEnvelopeV1, cost: str, label: str) -> FactoryUsageReceiptV1:
    return FactoryUsageReceiptV1(
        schema="captain.factory-usage-receipt.v1",
        receipt_id=uuid5(NAMESPACE_URL, f"receipt:{label}"),
        reservation_id=uuid5(NAMESPACE_URL, f"reservation:{label}"),
        job_id=env.job_id,
        correlation_id=env.correlation_id,
        attempt=env.attempt,
        provider="openai",
        model=env.model_version,
        input_units=10,
        output_units=5,
        cost_usd=cost,
        started_at=NOW,
        ended_at=NOW,
        evidence_ref=artifact(f"usage-{label}"),
    )


class DurableFenceStore:
    def __init__(self) -> None:
        self.greatest: dict[str, int] = {}
        self.calls: list[str] = []
        self.finalized_receipt: BusinessBenchmarkRunReceiptV1 | None = None

    async def register_fence(self, item, effect_claim):
        self.calls.append("register_fence")
        self.greatest[item.identity.effect_id] = max(
            effect_claim.fence, self.greatest.get(item.identity.effect_id, 0)
        )
        return BusinessBenchmarkFenceReceiptV1(
            schema="captain.business-benchmark-fence-receipt.v1",
            effect_id=item.identity.effect_id,
            runtime_session_id=item.runtime_session_id,
            claim_id=effect_claim.claim_id,
            fence=effect_claim.fence,
            registered_at=NOW,
            evidence_ref=artifact("provider-fence"),
        )

    async def assert_current(self, item, effect_claim, receipt) -> None:
        self.calls.append("assert_current")
        assert receipt.fence == self.greatest[item.identity.effect_id] == effect_claim.fence

    async def begin_dispatch(self, item, effect_claim, receipt) -> None:
        self.calls.append("begin_dispatch")
        await self.assert_current(item, effect_claim, receipt)

    async def record_provider_terminal(
        self, item, effect_claim, fence_receipt, run_receipt
    ) -> None:
        self.calls.append("record_provider_terminal")
        await self.assert_current(item, effect_claim, fence_receipt)
        self.finalized_receipt = run_receipt

    async def finalize(self, item, effect_claim, fence_receipt, run_receipt):
        self.calls.append("finalize")
        await self.assert_current(item, effect_claim, fence_receipt)
        assert self.finalized_receipt == run_receipt
        return run_receipt


class RuntimeBundle:
    def __init__(self, result: ProviderBenchmarkExecutionV1 | None = None) -> None:
        self.result = result
        self.policies: list[BaselineAssistantPolicyV1 | None] = []

    async def prepare(self, env):
        return BusinessBenchmarkRuntimePreparationV1(
            schema="captain.business-benchmark-runtime-preparation.v1",
            runtime_session_id=env.runtime_session_id,
        )

    async def execute(self, env, effect_claim, fence_receipt, *, baseline_policy):
        self.policies.append(baseline_policy)
        assert effect_claim.prepared_effect.runtime_session_id == env.runtime_session_id
        assert self.result is not None
        return self.result

    async def recover(self, item, effect_claim, fence_receipt):
        return BusinessBenchmarkRecoveryObservationV1(
            schema="captain.business-benchmark-recovery-observation.v1",
            effect_id=item.identity.effect_id,
            runtime_session_id=item.runtime_session_id,
            claim_id=effect_claim.claim_id,
            fence=effect_claim.fence,
            fence_receipt=fence_receipt,
            checked_at=NOW,
            evidence_ref=artifact("provider-recovery"),
            outcome="no_effect",
        )


def result(env: BusinessBenchmarkExecutionEnvelopeV1, **overrides: object) -> ProviderBenchmarkExecutionV1:
    binding = BenchmarkEvidenceBindingV1.from_execution(env, claim(env))
    payload: dict[str, object] = {
        "request_id": env.request_id,
        "runtime_session_id": env.runtime_session_id,
        "model_version": env.model_version,
        "variant": env.variant,
        "candidate_ref": env.candidate_ref,
        "case_sha256": env.case_sha256,
        "maximum_cost_micro_usd": env.maximum_cost_micro_usd,
        "maximum_latency_ms": env.maximum_latency_ms,
        "redaction_policy_sha256": env.redaction_policy_sha256,
        "status": "succeeded",
        "terminal_output": json.dumps(
            {
                "schema": "captain.business-benchmark-terminal.v1",
                "observed_decision": "route_standard_review",
                "observed_rationale_fact_ids": ["fact-policy-state"],
            }
        ),
        "usage_receipts": (
            BoundBenchmarkUsageEvidenceV1(
                binding=binding, receipt=usage(env, "0.000001", "one")
            ),
            BoundBenchmarkUsageEvidenceV1(
                binding=binding, receipt=usage(env, "0.000002", "two")
            ),
        ),
        "runtime_evidence_ref": artifact("runtime"),
        "terminal_evidence_ref": artifact("terminal"),
        "tool_executions": (),
        "handoffs": (),
        "completed_at": NOW,
    }
    payload.update(overrides)
    payload["usage_receipts"] = tuple(
        item
        if isinstance(item, BoundBenchmarkUsageEvidenceV1)
        else BoundBenchmarkUsageEvidenceV1(binding=binding, receipt=item)
        for item in payload["usage_receipts"]  # type: ignore[union-attr]
    )
    payload["tool_executions"] = tuple(
        item
        if isinstance(item, BoundBenchmarkToolEvidenceV1)
        else BoundBenchmarkToolEvidenceV1(binding=binding, execution=item)
        for item in payload["tool_executions"]  # type: ignore[union-attr]
    )
    payload["handoffs"] = tuple(
        item
        if isinstance(item, BoundBenchmarkHandoffEvidenceV1)
        else BoundBenchmarkHandoffEvidenceV1(
            binding=binding,
            handoff=item,
            authority=None,
            status="observed",
        )
        for item in payload["handoffs"]  # type: ignore[union-attr]
    )
    return ProviderBenchmarkExecutionV1.model_validate(payload)


@pytest.mark.parametrize("profile,count", [("claims", 30), ("renewal", 30)])
def test_live_settings_validate_scope_budget_model_and_safe_root(profile: str, count: int) -> None:
    settings = LiveBusinessBenchmarkSettings.from_environment(
        {
            "CAPTAIN_BENCHMARK_PROFILE": profile,
            "CAPTAIN_BENCHMARK_PROVIDER": "openai",
            "CAPTAIN_BENCHMARK_MODEL": "gpt-5-business-v1",
            "CAPTAIN_BENCHMARK_SUITE_VERSION": "2",
            "CAPTAIN_BENCHMARK_CANDIDATE_ID": "candidate-v3",
            "CAPTAIN_BENCHMARK_JOB_ID": str(JOB_ID),
            "CAPTAIN_BENCHMARK_MAX_USD": "3.25",
            "CAPTAIN_JOB_REMAINING_USD": "4.00",
            "CAPTAIN_JOB_ALLOWED_MODELS": "gpt-5-business-v1,gpt-5-business-v2",
            "CAPTAIN_BENCHMARK_EVIDENCE_ROOT": ".captain-cook/evidence/business-benchmarks/run-1",
            "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8000",
            "CAPTAIN_BENCHMARK_PROVIDER_SECRET": "OPENAI_API_KEY",
        },
        repository_root=Path.cwd(),
    )
    assert settings.maximum_usd == Decimal("3.25")
    assert settings.execution_count == count


def test_live_settings_require_positive_maximum_before_other_live_inputs() -> None:
    with pytest.raises(ValueError, match="maximum benchmark cost"):
        LiveBusinessBenchmarkSettings.from_environment(
            {"CAPTAIN_BENCHMARK_MAX_USD": "0"}
        )


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"CAPTAIN_BENCHMARK_MAX_USD": "0"}, "maximum benchmark cost"),
        ({"CAPTAIN_BENCHMARK_MAX_USD": "4.01"}, "remaining Captain budget"),
        ({"CAPTAIN_BENCHMARK_MODEL": "unreleased"}, "allowed model"),
        ({"CAPTAIN_BENCHMARK_PROFILE": "unknown"}, "benchmark profile"),
        ({"CAPTAIN_BENCHMARK_EVIDENCE_ROOT": "artifacts/out"}, "evidence root"),
    ],
)
def test_live_settings_fail_closed(changes: dict[str, str], message: str) -> None:
    environment = {
        "CAPTAIN_BENCHMARK_PROFILE": "claims",
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-5-business-v1",
        "CAPTAIN_BENCHMARK_SUITE_VERSION": "2",
        "CAPTAIN_BENCHMARK_CANDIDATE_ID": "candidate-v3",
        "CAPTAIN_BENCHMARK_JOB_ID": str(JOB_ID),
        "CAPTAIN_BENCHMARK_MAX_USD": "3.25",
        "CAPTAIN_JOB_REMAINING_USD": "4.00",
        "CAPTAIN_JOB_ALLOWED_MODELS": "gpt-5-business-v1",
        "CAPTAIN_BENCHMARK_EVIDENCE_ROOT": ".captain-cook/evidence/business-benchmarks/run-1",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8000",
        "CAPTAIN_BENCHMARK_PROVIDER_SECRET": "OPENAI_API_KEY",
    }
    environment.update(changes)
    with pytest.raises(ValueError, match=message):
        LiveBusinessBenchmarkSettings.from_environment(environment, repository_root=Path.cwd())


@pytest.mark.asyncio
async def test_adapter_maps_exact_usage_latency_terminal_and_deduplicated_evidence() -> None:
    env = envelope()
    provider = result(
        env,
        tool_executions=(
            FactoryToolExecutionEvidenceV1(
                agent_name="claims_agent",
                tool_name="read_policy",
                status="succeeded",
                evidence_ref=artifact("tool"),
            ),
        ),
    )
    bundle = RuntimeBundle(provider)
    ticks = iter((10.000, 10.125))
    fence_store = DurableFenceStore()
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=bundle,
        fence_store=fence_store,
        trusted_tool_intents={"read_policy": IntegrationIntent.NONE},
        monotonic_clock=lambda: next(ticks),
        clock=lambda: NOW,
    )
    effect_claim = claim(env)
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    receipt = await adapter.execute(env, effect_claim, fence)
    assert receipt.cost_micro_usd == 3
    assert receipt.latency_ms == 125
    assert receipt.observed_decision == "route_standard_review"
    assert receipt.observed_rationale_fact_ids == ("fact-policy-state",)
    assert receipt.observed_tool_intents == (IntegrationIntent.NONE,)
    assert receipt.human_handoff_completed is False
    assert len(receipt.evidence_refs) == len(set(receipt.evidence_refs)) == 6
    assert bundle.policies == [None]
    assert fence_store.finalized_receipt == receipt
    assert fence_store.calls == [
        "register_fence",
        "assert_current",
        "begin_dispatch",
        "assert_current",
        "record_provider_terminal",
        "assert_current",
        "finalize",
        "assert_current",
    ]


@pytest.mark.asyncio
async def test_provider_result_must_bind_exact_case_budget_redaction_and_candidate() -> None:
    env = envelope()
    mismatched = result(env, redaction_policy_sha256="f" * 64)
    fence_store = DurableFenceStore()
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(mismatched),
        fence_store=fence_store,
        trusted_tool_intents={},
        monotonic_clock=iter((0.0, 0.1)).__next__,
        clock=lambda: NOW,
    )
    effect_claim = claim(env)
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    with pytest.raises(ValueError, match="provider execution bindings"):
        await adapter.execute(env, effect_claim, fence)
    assert fence_store.finalized_receipt is None
    assert "begin_dispatch" in fence_store.calls
    assert "record_provider_terminal" not in fence_store.calls


@pytest.mark.asyncio
async def test_baseline_has_fresh_single_agent_policy_and_no_candidate_or_handoff_authority() -> None:
    env = envelope("single_agent_baseline")
    bundle = RuntimeBundle(result(env))
    ticks = iter((0.0, 0.001))
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=bundle,
        fence_store=DurableFenceStore(),
        trusted_tool_intents={},
        monotonic_clock=lambda: next(ticks),
        clock=lambda: NOW,
    )
    effect_claim = claim(env)
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    receipt = await adapter.execute(env, effect_claim, fence)
    policy = bundle.policies[0]
    assert receipt.variant == "single_agent_baseline"
    assert receipt.candidate_ref is None
    assert policy is not None
    assert policy.agent_name.endswith(env.case.case_id.replace("-", "_"))
    assert policy.system_policy_version == "single-agent-baseline-v1"
    assert policy.team_manifest_ref is None
    assert policy.handoffs == ()
    assert policy.routing_authority is False
    assert policy.publication_authority is False
    assert policy.grant_authority is False


@pytest.mark.asyncio
async def test_unknown_tool_is_unsafe_and_baseline_rejects_handoff_evidence() -> None:
    env = envelope()
    unknown = result(
        env,
        tool_executions=(
            FactoryToolExecutionEvidenceV1(
                agent_name="claims_agent",
                tool_name="untrusted_tool",
                status="succeeded",
                evidence_ref=artifact("unknown-tool"),
            ),
        ),
    )
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(unknown),
        fence_store=DurableFenceStore(),
        trusted_tool_intents={},
        monotonic_clock=iter((0.0, 0.1)).__next__,
        clock=lambda: NOW,
    )
    effect_claim = claim(env)
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    with pytest.raises(UnsafeBenchmarkToolError, match="unknown tool"):
        await adapter.execute(env, effect_claim, fence)

    baseline = envelope("single_agent_baseline")
    handoff = FactoryHandoffEvidenceV1(
        from_agent="baseline_agent",
        to_agent="human_review",
        evidence_ref=artifact("handoff"),
    )
    baseline_adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(result(baseline, handoffs=(handoff,))),
        fence_store=DurableFenceStore(),
        trusted_tool_intents={},
        monotonic_clock=iter((0.0, 0.1)).__next__,
        clock=lambda: NOW,
    )
    baseline_claim = claim(baseline)
    baseline_fence = await baseline_adapter.register_fence(
        baseline_claim.prepared_effect, baseline_claim
    )
    with pytest.raises(ValueError, match="baseline has no handoff authority"):
        await baseline_adapter.execute(baseline, baseline_claim, baseline_fence)


@pytest.mark.asyncio
async def test_prepare_fence_and_recover_stay_proof_bound() -> None:
    env = envelope()
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(result(env)),
        fence_store=DurableFenceStore(),
        trusted_tool_intents={},
        monotonic_clock=lambda: 0.0,
        clock=lambda: NOW,
    )
    preparation = await adapter.prepare(env)
    assert preparation.runtime_session_id == env.runtime_session_id
    effect_claim = claim(env)
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    recovery = await adapter.recover(effect_claim.prepared_effect, effect_claim, fence)
    assert recovery.outcome == "no_effect"
    assert recovery.fence_receipt == fence


@pytest.mark.asyncio
async def test_preflight_checks_deterministic_budget_before_health_and_bundle() -> None:
    calls: list[str] = []

    async def health(url: str) -> bool:
        calls.append(url)
        return True

    with pytest.raises(ValueError, match="remaining Captain budget"):
        await LiveBusinessBenchmarkPreflight(
            health_check=health, runtime_bundle=None
        ).validate_environment(
            {
                "CAPTAIN_BENCHMARK_PROFILE": "all",
                "CAPTAIN_BENCHMARK_PROVIDER": "openai",
                "CAPTAIN_BENCHMARK_MODEL": "gpt-5-business-v1",
                "CAPTAIN_BENCHMARK_MAX_USD": "5.00",
                "CAPTAIN_BENCHMARK_CLAIMS_SUITE_VERSION": "2",
                "CAPTAIN_BENCHMARK_CLAIMS_CANDIDATE_ID": "claims-candidate-v3",
                "CAPTAIN_BENCHMARK_CLAIMS_JOB_ID": str(JOB_ID),
                "CAPTAIN_BENCHMARK_CLAIMS_ATTEMPT": "2",
                "CAPTAIN_BENCHMARK_CLAIMS_MAX_USD": "3.00",
                "CAPTAIN_BENCHMARK_CLAIMS_REMAINING_USD": "4.00",
                "CAPTAIN_BENCHMARK_RENEWAL_SUITE_VERSION": "2",
                "CAPTAIN_BENCHMARK_RENEWAL_CANDIDATE_ID": "renewal-candidate-v3",
                "CAPTAIN_BENCHMARK_RENEWAL_JOB_ID": (
                    "00000000-0000-0000-0000-000000000799"
                ),
                "CAPTAIN_BENCHMARK_RENEWAL_ATTEMPT": "2",
                "CAPTAIN_BENCHMARK_RENEWAL_MAX_USD": "2.00",
                "CAPTAIN_BENCHMARK_RENEWAL_REMAINING_USD": "1.50",
                "CAPTAIN_JOB_ALLOWED_MODELS": "gpt-5-business-v1",
                "CAPTAIN_BENCHMARK_EVIDENCE_ROOT": ".captain-cook/evidence/business-benchmarks/run-1",
                "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8000",
                "CAPTAIN_BENCHMARK_PROVIDER_SECRET": "OPENAI_API_KEY",
                "OPENAI_API_KEY": "exists-but-must-not-be-printed",
            },
            repository_root=Path.cwd(),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_preflight_fails_typed_when_production_bundle_is_missing() -> None:
    async def health(url: str) -> bool:
        return True

    with pytest.raises(ProductionAdapterUnavailableError, match="TODO_TOOL.v1"):
        await LiveBusinessBenchmarkPreflight(
            health_check=health, runtime_bundle=None
        ).validate_environment(
            {
                "CAPTAIN_BENCHMARK_PROFILE": "claims",
                "CAPTAIN_BENCHMARK_PROVIDER": "openai",
                "CAPTAIN_BENCHMARK_MODEL": "gpt-5-business-v1",
                "CAPTAIN_BENCHMARK_SUITE_VERSION": "2",
                "CAPTAIN_BENCHMARK_CANDIDATE_ID": "candidate-v3",
                "CAPTAIN_BENCHMARK_JOB_ID": str(JOB_ID),
                "CAPTAIN_BENCHMARK_MAX_USD": "3.00",
                "CAPTAIN_JOB_REMAINING_USD": "4.00",
                "CAPTAIN_JOB_ALLOWED_MODELS": "gpt-5-business-v1",
                "CAPTAIN_BENCHMARK_EVIDENCE_ROOT": ".captain-cook/evidence/business-benchmarks/run-1",
                "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8000",
                "CAPTAIN_BENCHMARK_PROVIDER_SECRET": "OPENAI_API_KEY",
                "OPENAI_API_KEY": "exists-but-must-not-be-printed",
            },
            repository_root=Path.cwd(),
        )


def test_powershell_runner_contract_is_fail_closed_and_redacted() -> None:
    script = Path("scripts/run-business-benchmark-live.ps1").read_text(encoding="utf-8")
    assert script.startswith("#requires -Version 7")
    for required in (
        "[CmdletBinding()]",
        "[ValidateSet('claims', 'renewal', 'all')]",
        "Set-StrictMode -Version Latest",
        "CAPTAIN_BENCHMARK_MAX_USD",
        "-m live",
        "--no-cov",
        ".captain-cook/evidence/business-benchmarks",
        "finally",
    ):
        assert required in script
    assert "Get-ChildItem Env:" not in script
    assert "Write-Host $env:" not in script
