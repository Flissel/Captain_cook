from __future__ import annotations

from datetime import timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.business_benchmark_live import (
    BenchmarkEvidenceBindingV1,
    BoundBenchmarkHandoffEvidenceV1,
    BoundBenchmarkToolEvidenceV1,
    BoundBenchmarkUsageEvidenceV1,
    BusinessBenchmarkFinalizedReceiptV1,
    BusinessBenchmarkLiveAdapter,
    BusinessBenchmarkLiveRunResultV1,
    LiveBusinessBenchmarkSettings,
    ProductionAdapterUnavailableError,
    UnsafeBenchmarkToolError,
    run_provider_business_benchmarks,
)
from agenten.agent_factory.team_execution import (
    FactoryHandoffEvidenceV1,
    FactoryToolExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import IntegrationIntent
from tests.agent_factory.test_business_benchmark_live import (
    DurableFenceStore,
    NOW,
    RuntimeBundle,
    artifact,
    claim,
    envelope,
    result,
)


def live_environment(profile: str = "claims") -> dict[str, str]:
    return {
        "CAPTAIN_BENCHMARK_PROFILE": profile,
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-5-business-v1",
        "CAPTAIN_BENCHMARK_SUITE_VERSION": "2",
        "CAPTAIN_BENCHMARK_CANDIDATE_ID": "candidate-v3",
        "CAPTAIN_BENCHMARK_JOB_ID": "00000000-0000-0000-0000-000000000701",
        "CAPTAIN_BENCHMARK_MAX_USD": "3.00",
        "CAPTAIN_JOB_REMAINING_USD": "4.00",
        "CAPTAIN_JOB_ALLOWED_MODELS": "gpt-5-business-v1",
        "CAPTAIN_BENCHMARK_EVIDENCE_ROOT": ".captain-cook/evidence/business-benchmarks/run-review",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8000",
        "CAPTAIN_BENCHMARK_PROVIDER_SECRET": "OPENAI_API_KEY",
        "OPENAI_API_KEY": "present-for-deterministic-test-only",
    }


class RuntimePort:
    async def prepare(self, envelope): ...
    async def execute(self, envelope, claim, fence_receipt, *, baseline_policy): ...
    async def recover(self, prepared, claim, fence_receipt): ...


class FencePort:
    async def register_fence(self, prepared, claim): ...
    async def assert_current(self, prepared, claim, receipt): ...


def evidence_binding(variant: str = "candidate") -> BenchmarkEvidenceBindingV1:
    env = envelope(variant)
    effect_claim = claim(env)
    return BenchmarkEvidenceBindingV1(
        request_id=env.request_id,
        runtime_session_id=env.runtime_session_id,
        case_sha256=env.case_sha256,
        variant=env.variant,
        effect_id=effect_claim.prepared_effect.identity.effect_id,
        fence=effect_claim.fence,
    )


def finalized_receipt(
    index: int, variant: str, profile: str
) -> BusinessBenchmarkFinalizedReceiptV1:
    env = envelope(variant)
    evidence = artifact(f"final-{variant}-{index}")
    receipt = BusinessBenchmarkRunReceiptV1(
        schema="captain.business-benchmark-run-receipt.v1",
        run_id=uuid5(NAMESPACE_URL, f"final-run:{profile}:{variant}:{index}"),
        request_id=uuid5(NAMESPACE_URL, f"final-request:{profile}:{variant}:{index}"),
        execution_policy_sha256=env.execution_policy_sha256,
        runtime_session_id=f"{env.runtime_session_id}-{index}",
        job_id=env.job_id,
        correlation_id=env.correlation_id,
        subject_version=env.subject_version,
        attempt=env.attempt,
        suite_ref=env.suite_ref,
        suite_id=f"{profile}-suite-v2",
        case_id=f"{profile}-case-{index:02d}",
        case_sha256=f"{index + 1:064x}",
        variant=variant,
        candidate_ref=env.candidate_ref,
        model_version=env.model_version,
        allowed_tool_intents=env.allowed_tool_intents,
        maximum_cost_micro_usd=env.maximum_cost_micro_usd,
        maximum_latency_ms=env.maximum_latency_ms,
        status="succeeded",
        observed_decision="route_standard_review",
        observed_rationale_fact_ids=("fact-policy-state",),
        observed_tool_intents=(),
        unsafe_tool_use=False,
        human_handoff_completed=False,
        cost_micro_usd=1,
        latency_ms=1,
        evidence_refs=(evidence,),
        completed_at=NOW,
    )
    return BusinessBenchmarkFinalizedReceiptV1(
        profile=profile,
        receipt=receipt,
        receipt_ref=artifact(f"receipt-{profile}-{variant}-{index}"),
    )


def live_result(profile: str = "claims") -> BusinessBenchmarkLiveRunResultV1:
    selected_profiles = ("claims", "renewal") if profile == "all" else (profile,)
    receipts = tuple(
        finalized_receipt(index, variant, selected_profile)
        for selected_profile in selected_profiles
        for variant in ("candidate", "single_agent_baseline")
        for index in range(15)
    )
    summaries = (
        (artifact("claims-summary"), artifact("renewal-summary"))
        if profile == "all"
        else (artifact(f"{profile}-summary"),)
    )
    return BusinessBenchmarkLiveRunResultV1(
        profile=profile,
        receipts=receipts,
        summary_refs=summaries,
        evidence_refs=tuple(
            dict.fromkeys(
                [
                    *(receipt.receipt_ref for receipt in receipts),
                    *(
                        ref
                        for receipt in receipts
                        for ref in receipt.receipt.evidence_refs
                    ),
                    *summaries,
                ]
            )
        ),
        completed_at=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected", [("claims", 30), ("renewal", 30), ("all", 60)])
async def test_live_entrypoint_loads_composition_and_requires_finalized_receipts(
    profile: str, expected: int
) -> None:
    calls: list[str] = []

    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()

        async def health_check(self, url: str) -> bool:
            calls.append("health")
            return True

        async def run(self, settings: LiveBusinessBenchmarkSettings):
            calls.append(settings.profile)
            return live_result(settings.profile)

    outcome = await run_provider_business_benchmarks(
        live_environment(profile),
        repository_root=Path.cwd(),
        composition_loader=lambda settings: Composition(),
    )
    assert len(outcome.receipts) == expected
    assert calls == ["health", profile]


@pytest.mark.asyncio
async def test_live_entrypoint_rejects_incomplete_receipt_scope() -> None:
    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()

        async def health_check(self, url: str) -> bool:
            return True

        async def run(self, settings: LiveBusinessBenchmarkSettings):
            return live_result(settings.profile).model_copy(
                update={"receipts": live_result(settings.profile).receipts[:-1]}
            )

    with pytest.raises(ValueError, match="finalized candidate and baseline receipts"):
        await run_provider_business_benchmarks(
            live_environment(),
            repository_root=Path.cwd(),
            composition_loader=lambda settings: Composition(),
        )


@pytest.mark.asyncio
async def test_all_profile_requires_claims_and_renewal_receipt_coverage() -> None:
    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()

        async def health_check(self, url: str) -> bool:
            return True

        async def run(self, settings: LiveBusinessBenchmarkSettings):
            complete = live_result("all")
            claims_only = tuple(
                item.model_copy(update={"profile": "claims"})
                for item in complete.receipts
            )
            return complete.model_copy(update={"receipts": claims_only})

    with pytest.raises(ValueError, match="selected profile and variant"):
        await run_provider_business_benchmarks(
            live_environment("all"),
            repository_root=Path.cwd(),
            composition_loader=lambda settings: Composition(),
        )


@pytest.mark.asyncio
async def test_live_entrypoint_requires_durable_fence_wiring() -> None:
    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = object()

        async def health_check(self, url: str) -> bool:
            return True

        async def run(self, settings: LiveBusinessBenchmarkSettings):
            return live_result(settings.profile)

    with pytest.raises(ProductionAdapterUnavailableError, match="durable provider fence"):
        await run_provider_business_benchmarks(
            live_environment(),
            repository_root=Path.cwd(),
            composition_loader=lambda settings: Composition(),
        )


@pytest.mark.asyncio
async def test_handoff_completion_requires_bound_human_review_authority() -> None:
    env = envelope()
    effect_claim = claim(env)
    binding = evidence_binding()
    completed_handoff = BoundBenchmarkHandoffEvidenceV1(
        binding=binding,
        handoff=FactoryHandoffEvidenceV1(
            from_agent="claims_agent",
            to_agent="human_review",
            evidence_ref=artifact("human-review-handoff"),
        ),
        authority="captain_human_review",
        status="completed",
    )
    provider = result(env, handoffs=(completed_handoff,))
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(provider),
        fence_store=DurableFenceStore(),
        trusted_tool_intents={},
        monotonic_clock=iter((0.0, 0.1)).__next__,
        clock=lambda: NOW,
    )
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    receipt = await adapter.execute(env, effect_claim, fence)
    assert receipt.human_handoff_completed is True

    wrong = completed_handoff.model_copy(update={"authority": "untrusted_review"})
    with pytest.raises(ValueError, match="human-review authority"):
        result(env, handoffs=(wrong,))


@pytest.mark.asyncio
async def test_nested_evidence_cannot_be_reused_across_variants() -> None:
    env = envelope()
    effect_claim = claim(env)
    wrong_binding = evidence_binding("single_agent_baseline")
    raw_usage = result(env).usage_receipts[0].receipt
    provider = result(
        env,
        usage_receipts=(
            BoundBenchmarkUsageEvidenceV1(binding=wrong_binding, receipt=raw_usage),
        ),
    )
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(provider),
        fence_store=DurableFenceStore(),
        trusted_tool_intents={},
        monotonic_clock=iter((0.0, 0.1)).__next__,
        clock=lambda: NOW,
    )
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    with pytest.raises(ValueError, match="nested provider evidence binding"):
        await adapter.execute(env, effect_claim, fence)


@pytest.mark.asyncio
async def test_n8n_intent_requires_typed_grant_command_result_chain() -> None:
    env = envelope()
    effect_claim = claim(env)
    tool = BoundBenchmarkToolEvidenceV1(
        binding=evidence_binding(),
        execution=FactoryToolExecutionEvidenceV1(
            agent_name="claims_agent",
            tool_name="approved_n8n_tool",
            status="succeeded",
            evidence_ref=artifact("n8n-tool-call"),
        ),
        n8n_execution=None,
    )
    provider = result(env, tool_executions=(tool,))
    adapter = BusinessBenchmarkLiveAdapter(
        runtime_bundle=RuntimeBundle(provider),
        fence_store=DurableFenceStore(),
        trusted_tool_intents={"approved_n8n_tool": IntegrationIntent.N8N},
        monotonic_clock=iter((0.0, 0.1)).__next__,
        clock=lambda: NOW,
    )
    fence = await adapter.register_fence(effect_claim.prepared_effect, effect_claim)
    with pytest.raises(UnsafeBenchmarkToolError, match="typed n8n grant"):
        await adapter.execute(env, effect_claim, fence)


def test_powershell_runner_resolves_a_validated_python_interpreter() -> None:
    script = Path("scripts/run-business-benchmark-live.ps1").read_text(encoding="utf-8")
    for required in (
        "$PythonPath",
        "Test-Path -LiteralPath $resolvedPython -PathType Leaf",
        "TODO_TOOL.v1: validated Python",
        "sys.version_info[:2] == (3, 11)",
    ):
        assert required in script
