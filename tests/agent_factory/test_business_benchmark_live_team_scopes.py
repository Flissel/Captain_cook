from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkExpectedCaseV1,
    BusinessBenchmarkExpectedScopeV1,
    BusinessBenchmarkExpectedSuiteV1,
    BusinessBenchmarkFinalizedReceiptV1,
    BusinessBenchmarkLiveRunResultV1,
    BusinessBenchmarkTeamSelectionV1,
    LiveBusinessBenchmarkSettings,
    run_provider_business_benchmarks,
)
from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)


def all_environment() -> dict[str, str]:
    return {
        "CAPTAIN_BENCHMARK_PROFILE": "all",
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-5-business-v1",
        "CAPTAIN_BENCHMARK_REDACTION_POLICY_SHA256": "d" * 64,
        "CAPTAIN_BENCHMARK_MAX_USD": "5.00",
        "CAPTAIN_JOB_ALLOWED_MODELS": "gpt-5-business-v1",
        "CAPTAIN_BENCHMARK_EVIDENCE_ROOT": (
            ".captain-cook/evidence/business-benchmarks/team-scope-test"
        ),
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8000",
        "CAPTAIN_BENCHMARK_PROVIDER_SECRET": "OPENAI_API_KEY",
        "CAPTAIN_BENCHMARK_CLAIMS_SUITE_VERSION": "2",
        "CAPTAIN_BENCHMARK_CLAIMS_CANDIDATE_ID": "claims-candidate-v3",
        "CAPTAIN_BENCHMARK_CLAIMS_JOB_ID": (
            "00000000-0000-0000-0000-000000000711"
        ),
        "CAPTAIN_BENCHMARK_CLAIMS_ATTEMPT": "2",
        "CAPTAIN_BENCHMARK_CLAIMS_MAX_USD": "2.00",
        "CAPTAIN_BENCHMARK_CLAIMS_REMAINING_USD": "2.50",
        "CAPTAIN_BENCHMARK_RENEWAL_SUITE_VERSION": "3",
        "CAPTAIN_BENCHMARK_RENEWAL_CANDIDATE_ID": "renewal-candidate-v4",
        "CAPTAIN_BENCHMARK_RENEWAL_JOB_ID": (
            "00000000-0000-0000-0000-000000000712"
        ),
        "CAPTAIN_BENCHMARK_RENEWAL_ATTEMPT": "3",
        "CAPTAIN_BENCHMARK_RENEWAL_MAX_USD": "3.00",
        "CAPTAIN_BENCHMARK_RENEWAL_REMAINING_USD": "3.50",
    }


def test_all_profile_builds_two_distinct_captain_team_selections() -> None:
    settings = LiveBusinessBenchmarkSettings.from_environment(
        all_environment(), repository_root=Path.cwd()
    )

    assert settings.execution_count == 60
    assert settings.maximum_usd == Decimal("5.00")
    assert settings.redaction_policy_sha256 == "d" * 64
    assert settings.selections == (
        BusinessBenchmarkTeamSelectionV1(
            profile="claims",
            job_id="00000000-0000-0000-0000-000000000711",
            candidate_id="claims-candidate-v3",
            suite_version=2,
            attempt=2,
            maximum_usd=Decimal("2.00"),
            captain_remaining_usd=Decimal("2.50"),
        ),
        BusinessBenchmarkTeamSelectionV1(
            profile="renewal",
            job_id="00000000-0000-0000-0000-000000000712",
            candidate_id="renewal-candidate-v4",
            suite_version=3,
            attempt=3,
            maximum_usd=Decimal("3.00"),
            captain_remaining_usd=Decimal("3.50"),
        ),
    )


def test_all_profile_never_falls_back_to_generic_team_identity() -> None:
    environment = all_environment()
    del environment["CAPTAIN_BENCHMARK_CLAIMS_CANDIDATE_ID"]
    environment["CAPTAIN_BENCHMARK_CANDIDATE_ID"] = "shared-candidate"

    with pytest.raises(
        ValueError, match="CAPTAIN_BENCHMARK_CLAIMS_CANDIDATE_ID"
    ):
        LiveBusinessBenchmarkSettings.from_environment(
            environment, repository_root=Path.cwd()
        )


@pytest.mark.parametrize(
    "field,message",
    [
        ("CAPTAIN_BENCHMARK_RENEWAL_JOB_ID", "distinct job"),
        ("CAPTAIN_BENCHMARK_RENEWAL_CANDIDATE_ID", "distinct candidate"),
    ],
)
def test_all_profile_rejects_reused_job_or_candidate_identity(
    field: str, message: str
) -> None:
    environment = all_environment()
    suffix = field.removeprefix("CAPTAIN_BENCHMARK_RENEWAL_")
    environment[field] = environment[f"CAPTAIN_BENCHMARK_CLAIMS_{suffix}"]

    with pytest.raises(ValueError, match=message):
        LiveBusinessBenchmarkSettings.from_environment(
            environment, repository_root=Path.cwd()
        )


def test_all_profile_requires_aggregate_budget_to_equal_team_budgets() -> None:
    environment = all_environment()
    environment["CAPTAIN_BENCHMARK_MAX_USD"] = "5.01"

    with pytest.raises(ValueError, match="aggregate benchmark budget"):
        LiveBusinessBenchmarkSettings.from_environment(
            environment, repository_root=Path.cwd()
        )


def artifact(label: str) -> ArtifactRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-team-scope/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def holdout(profile: str) -> PrivateHoldoutRef:
    digest = hashlib.sha256(f"{profile}-private-suite".encode()).hexdigest()
    return PrivateHoldoutRef(
        holdout_id=f"holdout-{digest[:12]}",
        uri=f"holdout://holdout-{digest[:12]}",
        sha256=digest,
    )


def expected_scope(
    selection: BusinessBenchmarkTeamSelectionV1,
    *,
    candidate_ref: ArtifactRef | None = None,
) -> BusinessBenchmarkExpectedScopeV1:
    profile = selection.profile
    return BusinessBenchmarkExpectedScopeV1(
        job_id=selection.job_id,
        correlation_id=uuid5(NAMESPACE_URL, f"correlation:{profile}"),
        subject_version=3,
        attempt=selection.attempt,
        model_version="gpt-5-business-v1",
        candidate_id=selection.candidate_id,
        candidate_ref=candidate_ref or artifact(f"{profile}-candidate"),
        suites=(
            BusinessBenchmarkExpectedSuiteV1(
                profile=profile,
                suite_id=f"{profile}-suite-v{selection.suite_version}",
                suite_version=selection.suite_version,
                suite_ref=holdout(profile),
                cases=tuple(
                    BusinessBenchmarkExpectedCaseV1(
                        case_id=f"{profile}-case-{index:02d}",
                        case_sha256=hashlib.sha256(
                            f"{profile}-case-{index:02d}".encode()
                        ).hexdigest(),
                    )
                    for index in range(15)
                ),
            ),
        ),
    )


def finalized_receipt(
    scope: BusinessBenchmarkExpectedScopeV1,
    *,
    variant: str,
    index: int,
    cost_micro_usd: int,
) -> BusinessBenchmarkFinalizedReceiptV1:
    suite = scope.suites[0]
    expected_case = suite.cases[index]
    evidence_ref = artifact(f"{suite.profile}-{variant}-{index}-evidence")
    return BusinessBenchmarkFinalizedReceiptV1(
        profile=suite.profile,
        receipt=BusinessBenchmarkRunReceiptV1(
            schema="captain.business-benchmark-run-receipt.v1",
            run_id=uuid5(NAMESPACE_URL, f"run:{suite.profile}:{variant}:{index}"),
            request_id=uuid5(
                NAMESPACE_URL, f"request:{suite.profile}:{variant}:{index}"
            ),
            execution_policy_sha256="a" * 64,
            runtime_session_id=f"benchmark-{suite.profile}-{variant}-{index}",
            job_id=scope.job_id,
            correlation_id=scope.correlation_id,
            subject_version=scope.subject_version,
            attempt=scope.attempt,
            suite_ref=suite.suite_ref,
            suite_id=suite.suite_id,
            case_id=expected_case.case_id,
            case_sha256=expected_case.case_sha256,
            variant=variant,
            candidate_ref=scope.candidate_ref if variant == "candidate" else None,
            model_version=scope.model_version,
            allowed_tool_intents=(),
            maximum_cost_micro_usd=100_000,
            maximum_latency_ms=2_000,
            status="succeeded",
            observed_decision="route_standard_review",
            observed_rationale_fact_ids=("fact-policy-state",),
            observed_tool_intents=(),
            unsafe_tool_use=False,
            human_handoff_completed=False,
            cost_micro_usd=cost_micro_usd,
            latency_ms=1,
            evidence_refs=(evidence_ref,),
            completed_at=NOW,
        ),
        receipt_ref=artifact(f"{suite.profile}-{variant}-{index}-receipt"),
    )


def live_result(
    settings: LiveBusinessBenchmarkSettings,
    scopes: tuple[BusinessBenchmarkExpectedScopeV1, ...],
    *,
    claims_cost_micro_usd: int = 60_000,
    renewal_cost_micro_usd: int = 90_000,
) -> BusinessBenchmarkLiveRunResultV1:
    receipts = tuple(
        finalized_receipt(
            scope,
            variant=variant,
            index=index,
            cost_micro_usd=(
                claims_cost_micro_usd
                if scope.suites[0].profile == "claims"
                else renewal_cost_micro_usd
            ),
        )
        for scope in scopes
        for variant in ("candidate", "single_agent_baseline")
        for index in range(15)
    )
    summaries = tuple(
        artifact(f"{scope.suites[0].profile}-summary") for scope in scopes
    )
    return BusinessBenchmarkLiveRunResultV1(
        profile=settings.profile,
        selections=settings.selections,
        receipts=receipts,
        summary_refs=summaries,
        evidence_refs=tuple(
            [
                *(item.receipt_ref for item in receipts),
                *(ref for item in receipts for ref in item.receipt.evidence_refs),
                *summaries,
            ]
        ),
        completed_at=NOW,
    )


class RuntimePort:
    async def prepare(self, envelope): ...
    async def execute(self, envelope, claim, fence_receipt, *, baseline_policy): ...
    async def recover(self, prepared, claim, fence_receipt): ...


class FencePort:
    async def register_fence(self, prepared, claim): ...
    async def assert_current(self, prepared, claim, receipt): ...
    async def begin_dispatch(self, prepared, claim, receipt): ...
    async def record_provider_terminal(
        self, prepared, claim, fence_receipt, run_receipt
    ): ...
    async def finalize(self, prepared, claim, fence_receipt, run_receipt): ...


@pytest.mark.asyncio
async def test_all_validates_both_exact_scopes_before_health_or_provider_effect() -> None:
    environment = all_environment()
    environment["OPENAI_API_KEY"] = "present-for-test"
    settings = LiveBusinessBenchmarkSettings.from_environment(
        environment, repository_root=Path.cwd()
    )
    claims, renewal = tuple(expected_scope(item) for item in settings.selections)
    wrong_renewal = renewal.model_copy(update={"job_id": claims.job_id})
    calls: list[str] = []

    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()
        expected_scopes = (claims, wrong_renewal)

        async def health_check(self, url: str) -> bool:
            calls.append("health")
            return True

        async def run(self, live_settings: LiveBusinessBenchmarkSettings):
            calls.append("provider")
            return live_result(live_settings, (claims, renewal))

    with pytest.raises(ValueError, match="configured team selection"):
        await run_provider_business_benchmarks(
            environment,
            repository_root=Path.cwd(),
            composition_loader=lambda _: Composition(),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_all_rejects_reused_candidate_artifact_before_health() -> None:
    environment = all_environment()
    environment["OPENAI_API_KEY"] = "present-for-test"
    settings = LiveBusinessBenchmarkSettings.from_environment(
        environment, repository_root=Path.cwd()
    )
    shared_candidate = artifact("shared-candidate")
    scopes = tuple(
        expected_scope(selection, candidate_ref=shared_candidate)
        for selection in settings.selections
    )
    calls: list[str] = []

    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()
        expected_scopes = scopes

        async def health_check(self, url: str) -> bool:
            calls.append("health")
            return True

        async def run(self, live_settings: LiveBusinessBenchmarkSettings):
            calls.append(live_settings.profile)
            scope = next(
                item
                for item in scopes
                if item.suites[0].profile == live_settings.profile
            )
            return live_result(live_settings, (scope,))

    with pytest.raises(ValueError, match="distinct candidate references"):
        await run_provider_business_benchmarks(
            environment,
            repository_root=Path.cwd(),
            composition_loader=lambda _: Composition(),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_all_binds_results_and_cost_to_each_team_selection() -> None:
    environment = all_environment()
    environment["OPENAI_API_KEY"] = "present-for-test"
    settings = LiveBusinessBenchmarkSettings.from_environment(
        environment, repository_root=Path.cwd()
    )
    scopes = tuple(expected_scope(item) for item in settings.selections)
    calls: list[str] = []

    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()
        expected_scopes = scopes

        async def health_check(self, url: str) -> bool:
            calls.append("health")
            return True

        async def run(self, live_settings: LiveBusinessBenchmarkSettings):
            calls.append(live_settings.profile)
            scope = next(
                item
                for item in scopes
                if item.suites[0].profile == live_settings.profile
            )
            return live_result(live_settings, (scope,))

    outcome = await run_provider_business_benchmarks(
        environment,
        repository_root=Path.cwd(),
        composition_loader=lambda _: Composition(),
    )
    assert outcome.selections == settings.selections
    assert len(outcome.receipts) == 60
    assert calls == ["health", "claims", "renewal"]


@pytest.mark.asyncio
async def test_all_rejects_team_cost_overrun_even_below_aggregate_budget() -> None:
    environment = all_environment()
    environment["OPENAI_API_KEY"] = "present-for-test"
    settings = LiveBusinessBenchmarkSettings.from_environment(
        environment, repository_root=Path.cwd()
    )
    scopes = tuple(expected_scope(item) for item in settings.selections)

    class Composition:
        runtime_bundle = RuntimePort()
        fence_store = FencePort()
        expected_scopes = scopes

        async def health_check(self, url: str) -> bool:
            return True

        async def run(self, live_settings: LiveBusinessBenchmarkSettings):
            scope = next(
                item
                for item in scopes
                if item.suites[0].profile == live_settings.profile
            )
            return live_result(
                live_settings,
                (scope,),
                claims_cost_micro_usd=70_000,
                renewal_cost_micro_usd=80_000,
            )

    with pytest.raises(ValueError, match="claims team cost"):
        await run_provider_business_benchmarks(
            environment,
            repository_root=Path.cwd(),
            composition_loader=lambda _: Composition(),
        )


def test_environment_template_exposes_global_and_isolated_team_scope_inputs() -> None:
    template = Path(".env.example").read_text(encoding="utf-8")
    for variable in (
        "CAPTAIN_BENCHMARK_REDACTION_POLICY_SHA256=",
        "CAPTAIN_BENCHMARK_ATTEMPT=",
        "CAPTAIN_BENCHMARK_CLAIMS_JOB_ID=",
        "CAPTAIN_BENCHMARK_CLAIMS_CANDIDATE_ID=",
        "CAPTAIN_BENCHMARK_CLAIMS_SUITE_VERSION=",
        "CAPTAIN_BENCHMARK_CLAIMS_ATTEMPT=",
        "CAPTAIN_BENCHMARK_CLAIMS_MAX_USD=",
        "CAPTAIN_BENCHMARK_CLAIMS_REMAINING_USD=",
        "CAPTAIN_BENCHMARK_RENEWAL_JOB_ID=",
        "CAPTAIN_BENCHMARK_RENEWAL_CANDIDATE_ID=",
        "CAPTAIN_BENCHMARK_RENEWAL_SUITE_VERSION=",
        "CAPTAIN_BENCHMARK_RENEWAL_ATTEMPT=",
        "CAPTAIN_BENCHMARK_RENEWAL_MAX_USD=",
        "CAPTAIN_BENCHMARK_RENEWAL_REMAINING_USD=",
    ):
        assert variable in template


def test_powershell_runner_requires_isolated_inputs_for_all_only() -> None:
    script = Path("scripts/run-business-benchmark-live.ps1").read_text(
        encoding="utf-8"
    )
    assert "$Profile -eq 'all'" in script
    for variable in (
        "CAPTAIN_BENCHMARK_REDACTION_POLICY_SHA256",
        "CAPTAIN_BENCHMARK_CLAIMS_JOB_ID",
        "CAPTAIN_BENCHMARK_CLAIMS_CANDIDATE_ID",
        "CAPTAIN_BENCHMARK_CLAIMS_SUITE_VERSION",
        "CAPTAIN_BENCHMARK_CLAIMS_ATTEMPT",
        "CAPTAIN_BENCHMARK_CLAIMS_MAX_USD",
        "CAPTAIN_BENCHMARK_CLAIMS_REMAINING_USD",
        "CAPTAIN_BENCHMARK_RENEWAL_JOB_ID",
        "CAPTAIN_BENCHMARK_RENEWAL_CANDIDATE_ID",
        "CAPTAIN_BENCHMARK_RENEWAL_SUITE_VERSION",
        "CAPTAIN_BENCHMARK_RENEWAL_ATTEMPT",
        "CAPTAIN_BENCHMARK_RENEWAL_MAX_USD",
        "CAPTAIN_BENCHMARK_RENEWAL_REMAINING_USD",
    ):
        assert variable in script
