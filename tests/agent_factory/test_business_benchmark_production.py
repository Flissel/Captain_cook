from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessCaseCategory,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
)
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkFinalizedReceiptV1,
    BusinessBenchmarkTeamSelectionV1,
    LiveBusinessBenchmarkSettings,
)
from agenten.agent_factory.business_benchmark_factory import (
    BusinessBenchmarkFactoryComposition,
)
from agenten.agent_factory.business_benchmark_production import (
    BusinessBenchmarkProductionScopeError,
    CaptainBusinessBenchmarkPolicyBindingV1,
    ProductionBusinessBenchmarkComposition,
    ProductionBusinessBenchmarkScopeResolver,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryLiveCapability,
)
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from agenten.agent_factory.business_benchmark import BusinessBenchmarkEvaluator
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from tests.agent_factory.test_business_benchmark_production_ports import (
    live_job,
    stored_candidate,
)
from tests.agent_factory.test_business_benchmark_contracts import (
    summary as benchmark_summary,
)
from tests.agent_factory.test_skill_workflow_contracts import execution_outcome_payload


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
PROFILE_ID = "insurance_claims_resolution_swarm"


def ref(label: str, *, media_type: str = "application/json") -> ArtifactRef:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://business-benchmark-production-test/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def suite() -> BusinessBenchmarkSuiteV1:
    cases: list[BusinessBenchmarkCaseV1] = []
    for category in BusinessCaseCategory:
        for ordinal in range(1, 4):
            mandatory = category is BusinessCaseCategory.MANDATORY_ESCALATION
            cases.append(
                BusinessBenchmarkCaseV1(
                    schema="captain.business-benchmark-case.v1",
                    case_id=f"claims-{category.value}-{ordinal}",
                    profile_id=PROFILE_ID,
                    category=category,
                    redacted_input={"synthetic_class": category.value, "ordinal": ordinal},
                    expected_decision=(
                        "escalate_coverage"
                        if mandatory
                        else "request_information"
                        if category is BusinessCaseCategory.INCOMPLETE
                        else "route_standard_review"
                    ),
                    required_rationale_fact_ids=(
                        f"fact-{category.value}-{ordinal}",
                    ),
                    allowed_tool_intents=(),
                    human_handoff_required=mandatory,
                    severity="critical" if mandatory else "normal",
                )
            )
    return BusinessBenchmarkSuiteV1(
        schema="captain.business-benchmark-suite.v1",
        suite_id="claims-canonical-v2",
        profile_id=PROFILE_ID,
        suite_version=2,
        cases=tuple(cases),
        created_at=NOW,
    )


def invocation(
    job: AgentFactoryJobV3,
    step: FactorySkillStep,
    *,
    input_ref: ArtifactRef | None = None,
) -> FactorySkillInvocationV1:
    role = {
        FactorySkillStep.EXECUTE_TEAM: FactoryRole.REAL_CASE_TESTER,
        FactorySkillStep.EVALUATE_TEAM: FactoryRole.QUALITY_WARDEN,
        FactorySkillStep.REPORT_CAPTAIN: FactoryRole.QUALITY_WARDEN,
    }[step]
    skill_id = {
        FactorySkillStep.EXECUTE_TEAM: "captain-factory-execute-team",
        FactorySkillStep.EVALUATE_TEAM: "captain-factory-evaluate-team",
        FactorySkillStep.REPORT_CAPTAIN: "captain-factory-report-captain",
    }[step]
    bound_input = input_ref or job.input_ref
    return FactorySkillInvocationV1(
        schema="captain.factory-skill-invocation.v1",
        invocation_id=uuid5(NAMESPACE_URL, f"invocation:{step.value}:{bound_input.sha256}"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        step=step,
        released_skill=ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id=skill_id,
            version=1,
            capability="factory_workflow",
            content_ref=ref(f"skill-{step.value}"),
            content_sha256=ref(f"skill-{step.value}").sha256,
            status="released",
            released_at=NOW,
            producer="captain",
        ),
        input_ref=bound_input,
        input_sha256=bound_input.sha256,
        lease=issue_factory_lease(
            job=job,
            role=role,
            attempt=1,
            workspace_ref=f"workspace://business-benchmark/{step.value}",
            now=NOW,
        ),
        idempotency_key=hashlib.sha256(
            f"{job.job_id}:{step.value}:{bound_input.sha256}".encode("utf-8")
        ).hexdigest(),
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        execution_scope_ref=(
            job.private_holdout_refs[0]
            if step is FactorySkillStep.EXECUTE_TEAM
            else None
        ),
    )


def execution(
    job: AgentFactoryJobV3,
    candidate_ref: ArtifactRef,
    runtime_invocation: FactorySkillInvocationV1,
    run_number: int = 1,
) -> TeamExecutionEvidenceV1:
    outcome_payload = execution_outcome_payload()
    outcome_payload["correlation_id"] = str(job.correlation_id)
    assertion = outcome_payload["assertion_outcomes"][0]
    assert isinstance(assertion, dict)
    assertion["assertion_id"] = job.acceptance_assertion_ids[0]
    outcome_payload["assertion_outcomes"] = [assertion]
    return TeamExecutionEvidenceV1.model_validate(
        {
            "schema": "hermes.factory-team-execution-evidence.v1",
            "invocation": runtime_invocation.model_dump(mode="json", by_alias=True),
            "invocation_id": str(runtime_invocation.invocation_id),
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": 1,
            "occurred_at": NOW,
            "producer": "hermes",
            "artifact_ref": ref(f"technical-execution-{run_number}"),
            "evidence_refs": [ref(f"technical-evidence-{run_number}")],
            "acceptance_assertion_ids": list(job.acceptance_assertion_ids),
            "run_number": run_number,
            "candidate_ref": candidate_ref,
            "holdout_ref": job.private_holdout_refs[0].model_dump(mode="json"),
            "execution_outcome": outcome_payload,
            "usage_receipt_refs": [ref("technical-usage")],
            "handoff_evidence_refs": [ref("technical-handoff")],
            "tool_evidence_refs": (),
            "workflow_evidence_refs": (),
            "termination_reason": "task_completed",
            "status": "succeeded",
        }
    )


class Gateway:
    def __init__(
        self,
        job: AgentFactoryJobV3,
        evidence: TeamExecutionEvidenceV1,
        candidate_ref: ArtifactRef,
    ) -> None:
        self.current_job: object | None = job
        self.executions: tuple[TeamExecutionEvidenceV1, ...] = (evidence,)
        self.authoritative_candidate_ref: ArtifactRef | None = candidate_ref
        self.budget = FactoryBudgetProjection(
            job_id=job.job_id,
            limit_usd="5.00",
            consumed_usd="0",
            reserved_usd="0",
            remaining_usd="5.00",
        )

    def factory_job(self, job_id: UUID) -> object | None:
        return self.current_job if getattr(self.current_job, "job_id", None) == job_id else None

    def team_execution_evidence(
        self, job_id: UUID, attempt: int
    ) -> tuple[TeamExecutionEvidenceV1, ...]:
        return self.executions

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection | None:
        return self.budget

    def candidate_ref(
        self, job_id: UUID, attempt: int, candidate_id: str
    ) -> ArtifactRef | None:
        return self.authoritative_candidate_ref


class SuiteAuthority:
    def __init__(self, job: AgentFactoryJobV3, private_suite: BusinessBenchmarkSuiteV1) -> None:
        self.reference = job.private_holdout_refs[0]
        self.private_suite = private_suite

    def canonical_suite(self, *, profile_id: str, suite_version: int):
        return self.reference, self.private_suite


class CandidateAuthority:
    def __init__(self, resolved: ResolvedFactoryCandidate) -> None:
        self.resolved = resolved
        self.calls: list[tuple[UUID, str]] = []

    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        expected_candidate_id: str,
        expected_candidate_ref: ArtifactRef,
    ):
        self.calls.append((job.job_id, expected_candidate_id))
        if self.resolved.candidate.source_archive_ref != expected_candidate_ref:
            raise ValueError("candidate authority reference changed")
        return self.resolved


class InvocationAuthority:
    def __init__(self, job: AgentFactoryJobV3) -> None:
        self.job = job
        self.report_preflights = 0

    def runtime_invocation(self, *, job: AgentFactoryJobV3, attempt: int):
        return invocation(job, FactorySkillStep.EXECUTE_TEAM)

    def evaluation_invocation(self, *, job: AgentFactoryJobV3, attempt: int):
        return invocation(job, FactorySkillStep.EVALUATE_TEAM)

    def require_active_report(self, *, job: AgentFactoryJobV3, attempt: int) -> None:
        self.report_preflights += 1

    def report_invocation(self, *, job: AgentFactoryJobV3, attempt: int, evaluation):
        return invocation(
            job,
            FactorySkillStep.REPORT_CAPTAIN,
            input_ref=evaluation.artifact_ref,
        )


def settings(job: AgentFactoryJobV3, candidate_id: str) -> LiveBusinessBenchmarkSettings:
    selection = BusinessBenchmarkTeamSelectionV1(
        profile="claims",
        job_id=job.job_id,
        candidate_id=candidate_id,
        suite_version=2,
        attempt=1,
        maximum_usd="1.00",
        captain_remaining_usd="5.00",
    )
    return LiveBusinessBenchmarkSettings(
        profile="claims",
        provider="openai",
        model="approved-model-id",
        redaction_policy_sha256=redaction_digest("redaction-v1"),
        selections=(selection,),
        maximum_usd="1.00",
        allowed_models=("approved-model-id",),
        evidence_root=Path(".captain-cook/evidence/business-benchmarks/test"),
        runtime_url="http://127.0.0.1:8000",
        provider_secret_name="OPENAI_API_KEY",
    )


def authorities(tmp_path: Path):
    job = live_job()
    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "benchmark-cas"
    )
    manifest, _ = stored_candidate(store)
    resolved = ResolvedFactoryCandidate(
        candidate=manifest,
        source_archive=store.local_path(manifest.source_archive_ref),
    )
    runtime = invocation(job, FactorySkillStep.EXECUTE_TEAM)
    gateway = Gateway(
        job,
        execution(job, manifest.source_archive_ref, runtime),
        manifest.source_archive_ref,
    )
    invocations = InvocationAuthority(job)
    resolver = ProductionBusinessBenchmarkScopeResolver(
        gateway=gateway,
        suites=SuiteAuthority(job, suite()),
        candidates=CandidateAuthority(resolved),
        invocations=invocations,
    )
    return job, manifest, gateway, invocations, resolver


def redaction_digest(version: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"redaction_policy_version": version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def policy_binding(job: AgentFactoryJobV3) -> CaptainBusinessBenchmarkPolicyBindingV1:
    policy = BusinessBenchmarkPolicyV1(
        schema="captain.business-benchmark-policy.v1"
    )
    return CaptainBusinessBenchmarkPolicyBindingV1.create(
        job=job,
        attempt=1,
        policy=policy,
    )


class PolicyAuthority:
    def __init__(self, job: AgentFactoryJobV3) -> None:
        self.binding = policy_binding(job)

    def policy_for(self, scope):
        return self.binding


def test_scope_resolver_builds_digest_only_scope_from_exact_captain_authorities(
    tmp_path: Path,
) -> None:
    job, manifest, gateway, invocations, resolver = authorities(tmp_path)

    scope = resolver.resolve(settings(job, manifest.candidate_id))

    assert scope.job == job
    assert scope.candidate_ref == manifest.source_archive_ref
    assert scope.runtime_invocation.step is FactorySkillStep.EXECUTE_TEAM
    assert scope.evaluation_invocation.step is FactorySkillStep.EVALUATE_TEAM
    assert scope.technical_executions == gateway.executions
    assert scope.budget_projection == gateway.budget
    assert scope.expected_scope.job_id == job.job_id
    assert len(scope.expected_scope.suites[0].cases) == 15
    expected = {
        case.case_id: hashlib.sha256(
            canonical_business_benchmark_model_bytes(case)
        ).hexdigest()
        for case in suite().cases
    }
    assert {
        case.case_id: case.case_sha256
        for case in scope.expected_scope.suites[0].cases
    } == expected
    assert "redacted_input" not in scope.expected_scope.model_dump_json()
    assert invocations.report_preflights == 1


def test_release_job_requires_exactly_three_ordered_gateway_run_numbers(
    tmp_path: Path,
) -> None:
    job, manifest, gateway, _, _ = authorities(tmp_path)
    release_job = job.model_copy(
        update={
            "execution_policy": job.execution_policy.model_copy(
                update={
                    "mode": FactoryExecutionMode.RELEASE,
                    "required_live_runs": 3,
                }
            )
        }
    )
    runtime = invocation(release_job, FactorySkillStep.EXECUTE_TEAM)
    gateway.current_job = release_job
    gateway.executions = tuple(
        execution(
            release_job,
            manifest.source_archive_ref,
            runtime,
            run_number=run_number,
        )
        for run_number in (1, 2, 3)
    )
    invocations = InvocationAuthority(release_job)
    resolver = ProductionBusinessBenchmarkScopeResolver(
        gateway=gateway,
        suites=SuiteAuthority(release_job, suite()),
        candidates=CandidateAuthority(
            ResolvedFactoryCandidate(
                candidate=manifest,
                source_archive=Path(tmp_path) / "candidate.zip",
            )
        ),
        invocations=invocations,
    )

    scope = resolver.resolve(settings(release_job, manifest.candidate_id))

    assert tuple(item.run_number for item in scope.technical_executions) == (1, 2, 3)


@pytest.mark.parametrize(
    "failure",
    (
        "missing_job",
        "offline",
        "suite",
        "candidate",
        "candidate_ref",
        "execution",
        "run_count",
        "invocation",
        "budget",
        "report",
    ),
)
def test_scope_resolver_fails_closed_for_missing_stale_or_mixed_authority(
    tmp_path: Path,
    failure: str,
) -> None:
    job, manifest, gateway, invocations, resolver = authorities(tmp_path)
    if failure == "missing_job":
        gateway.current_job = None
    elif failure == "offline":
        gateway.current_job = job.model_copy(
            update={
                "execution_policy": job.execution_policy.model_copy(
                    update={
                        "live_execution": False,
                        "max_cost_usd": Decimal("0"),
                        "required_live_runs": 0,
                        "allowed_models": (),
                        "live_capabilities": (),
                    }
                )
            }
        )
    elif failure == "suite":
        resolver._suites.reference = job.private_holdout_refs[0].model_copy(
            update={"sha256": "f" * 64}
        )
    elif failure == "candidate":
        resolver._candidates.resolved = resolver._candidates.resolved.model_copy(
            update={
                "candidate": resolver._candidates.resolved.candidate.model_copy(
                    update={"candidate_id": "different_candidate"}
                )
            }
        )
    elif failure == "candidate_ref":
        gateway.authoritative_candidate_ref = None
    elif failure == "execution":
        gateway.executions = (
            gateway.executions[0].model_copy(update={"attempt": 2}),
        )
    elif failure == "run_count":
        gateway.executions = (
            gateway.executions[0],
            execution(
                job,
                manifest.source_archive_ref,
                gateway.executions[0].invocation,
                run_number=2,
            ),
        )
    elif failure == "invocation":
        gateway.executions = (
            gateway.executions[0].model_copy(
                update={
                    "invocation": gateway.executions[0].invocation.model_copy(
                        update={"idempotency_key": "7" * 64}
                    )
                }
            ),
        )
    elif failure == "budget":
        gateway.budget = gateway.budget.model_copy(update={"remaining_usd": Decimal("4.00")})
    else:
        invocations.require_active_report = lambda **_: (_ for _ in ()).throw(
            ValueError("report authority unavailable")
        )

    with pytest.raises(BusinessBenchmarkProductionScopeError):
        resolver.resolve(settings(job, manifest.candidate_id))


def run_receipt(
    *,
    job: AgentFactoryJobV3,
    candidate_ref: ArtifactRef,
    benchmark_case: BusinessBenchmarkCaseV1,
    variant: str,
) -> BusinessBenchmarkRunReceiptV1:
    case_sha256 = hashlib.sha256(
        canonical_business_benchmark_model_bytes(benchmark_case)
    ).hexdigest()
    request_id = uuid5(NAMESPACE_URL, f"request:{benchmark_case.case_id}:{variant}")
    policy = valid_execution_policy()
    return BusinessBenchmarkRunReceiptV1(
        schema="captain.business-benchmark-run-receipt.v1",
        run_id=uuid5(NAMESPACE_URL, f"run:{benchmark_case.case_id}:{variant}"),
        request_id=request_id,
        execution_policy_sha256=hashlib.sha256(
            canonical_business_benchmark_model_bytes(policy)
        ).hexdigest(),
        runtime_session_id=f"session-{benchmark_case.case_id}-{variant}",
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        suite_ref=job.private_holdout_refs[0],
        suite_id=suite().suite_id,
        case_id=benchmark_case.case_id,
        case_sha256=case_sha256,
        variant=variant,
        candidate_ref=candidate_ref if variant == "candidate" else None,
        model_version="approved-model-id",
        allowed_tool_intents=(),
        maximum_cost_micro_usd=policy.maximum_cost_micro_usd,
        maximum_latency_ms=policy.maximum_latency_ms,
        status="succeeded",
        observed_decision=benchmark_case.expected_decision,
        observed_rationale_fact_ids=benchmark_case.required_rationale_fact_ids,
        observed_tool_intents=(),
        unsafe_tool_use=False,
        human_handoff_completed=benchmark_case.human_handoff_required,
        cost_micro_usd=1,
        latency_ms=1,
        evidence_refs=(ref(f"provider-{benchmark_case.case_id}-{variant}"),),
        completed_at=NOW,
    )


def valid_execution_policy() -> BenchmarkExecutionPolicyV1:
    return BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        allowed_tool_intents=(),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=1000,
        redaction_policy_version="redaction-v1",
        baseline_system_policy_version="baseline-v1",
    )


class FactoryComposition:
    def __init__(self, job: AgentFactoryJobV3, candidate_ref: ArtifactRef) -> None:
        cases = suite().cases
        self.summary = benchmark_summary(
            job_id=str(job.job_id),
            correlation_id=str(job.correlation_id),
            subject_version=job.subject_version,
            attempt=1,
            candidate_ref=candidate_ref.model_dump(mode="json"),
            suite_ref=job.private_holdout_refs[0].model_dump(mode="json"),
            suite_id=suite().suite_id,
        )
        self.candidate_receipts = tuple(
            run_receipt(
                job=job,
                candidate_ref=candidate_ref,
                benchmark_case=case,
                variant="candidate",
            )
            for case in cases
        )
        self.baseline_receipts = tuple(
            run_receipt(
                job=job,
                candidate_ref=candidate_ref,
                benchmark_case=case,
                variant="single_agent_baseline",
            )
            for case in cases
        )
        self.calls: list[dict[str, object]] = []
        self.result_mutator = lambda result: result
        self.last_result = None

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        evaluator = BusinessBenchmarkEvaluator(clock=lambda: NOW)
        cases = suite().cases
        case_receipts = tuple(
            evaluator.evaluate_case(case, candidate, baseline)
            for case, candidate, baseline in zip(
                cases,
                self.candidate_receipts,
                self.baseline_receipts,
                strict=True,
            )
        )
        evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
            kwargs["evaluation_invocation"],
            kwargs["candidate_ref"],
            kwargs["technical_executions"],
            benchmark_summary=self.summary,
            budget_projection=kwargs["budget_projection"],
        )
        report_invocation = kwargs["report_invocation_factory"](evaluation)
        feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
            invocation=report_invocation,
            candidate_ref=kwargs["candidate_ref"],
            evaluation=evaluation,
            budget_projection=kwargs["budget_projection"],
        )
        result = SimpleNamespace(
            summary=self.summary,
            evaluation=evaluation,
            feedback=feedback,
            candidate_receipts=self.candidate_receipts,
            baseline_receipts=self.baseline_receipts,
            case_receipts=case_receipts,
        )
        self.last_result = self.result_mutator(result)
        return self.last_result


class Finalizer:
    def finalize(self, *, profile: str, receipt: BusinessBenchmarkRunReceiptV1):
        return BusinessBenchmarkFinalizedReceiptV1(
            profile=profile,
            receipt=receipt,
            receipt_ref=ref(f"receipt-{receipt.run_id}"),
        )


@pytest.mark.asyncio
async def test_production_composition_runs_one_isolated_team_and_returns_exact_evidence(
    tmp_path: Path,
) -> None:
    job, manifest, _, invocations, resolver = authorities(tmp_path)
    factory = FactoryComposition(job, manifest.source_archive_ref)
    executor = object()
    replay = object()
    execution_policy = BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        baseline_system_policy_version="baseline-v1",
        maximum_cost_micro_usd=100,
        maximum_latency_ms=1000,
        redaction_policy_version="redaction-v1",
    )
    composition = ProductionBusinessBenchmarkComposition(
        resolver=resolver,
        factory_composition=factory,
        invocation_authority=invocations,
        executor_factory=lambda scope: executor,
        replay_store_factory=lambda scope: replay,
        execution_policy_factory=lambda scope: (lambda case: execution_policy),
        benchmark_policy_authority=PolicyAuthority(job),
        receipt_finalizer=Finalizer(),
        clock=lambda: NOW,
    )

    prepared_scopes = composition.prepare_scopes(
        settings(job, manifest.candidate_id)
    )
    assert prepared_scopes == composition.expected_scopes
    assert len(prepared_scopes) == 1
    assert factory.calls == []

    result = await composition.run(settings(job, manifest.candidate_id))

    assert result.profile == "claims"
    assert len(result.receipts) == 30
    assert len([item for item in result.receipts if item.receipt.variant == "candidate"]) == 15
    assert len(
        [
            item
            for item in result.receipts
            if item.receipt.variant == "single_agent_baseline"
        ]
    ) == 15
    assert result.summary_refs == (factory.summary.artifact_ref,)
    assert factory.calls[0]["job"] == job
    assert factory.calls[0]["executor"] is executor
    assert factory.calls[0]["replay_store"] is replay
    assert factory.calls[0]["technical_executions"]
    assert factory.calls[0]["expected_suite_ref"] == job.private_holdout_refs[0]
    assert factory.calls[0]["expected_suite"] == suite()
    report = factory.calls[0]["report_invocation_factory"](
        factory.last_result.evaluation
    )
    assert report.step is FactorySkillStep.REPORT_CAPTAIN
    assert report.input_ref == factory.last_result.evaluation.artifact_ref
    assert set(result.summary_refs).issubset(result.evidence_refs)
    assert all(item.receipt_ref in result.evidence_refs for item in result.receipts)


@pytest.mark.asyncio
async def test_production_preflight_materializes_and_reuses_real_scope_adapters(
    tmp_path: Path,
) -> None:
    job, manifest, _, invocations, resolver = authorities(tmp_path)
    factory = FactoryComposition(job, manifest.source_archive_ref)
    executors: list[object] = []
    replays: list[object] = []
    execution_policy = BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        baseline_system_policy_version="baseline-v1",
        maximum_cost_micro_usd=100,
        maximum_latency_ms=1000,
        redaction_policy_version="redaction-v1",
    )
    composition = ProductionBusinessBenchmarkComposition(
        resolver=resolver,
        factory_composition=factory,
        invocation_authority=invocations,
        executor_factory=lambda scope: executors.append(object()) or executors[-1],
        replay_store_factory=lambda scope: replays.append(object()) or replays[-1],
        execution_policy_factory=lambda scope: (lambda case: execution_policy),
        benchmark_policy_authority=PolicyAuthority(job),
        receipt_finalizer=Finalizer(),
        clock=lambda: NOW,
    )
    selected = settings(job, manifest.candidate_id)

    prepared = await composition.preflight(
        selected,
        {"OPENAI_API_KEY": "present-for-deterministic-test-only"},
        repository_root=tmp_path,
    )
    repeated = await composition.preflight(
        selected,
        {"OPENAI_API_KEY": "present-for-deterministic-test-only"},
        repository_root=tmp_path,
    )

    assert prepared == repeated == composition.expected_scopes
    assert len(executors) == len(replays) == 1
    assert factory.calls == []

    await composition.run(selected)

    assert len(executors) == len(replays) == 1
    assert factory.calls[0]["executor"] is executors[0]
    assert factory.calls[0]["replay_store"] is replays[0]


@pytest.mark.asyncio
async def test_production_composition_rejects_aggregate_settings_before_effects(
    tmp_path: Path,
) -> None:
    job, manifest, _, invocations, resolver = authorities(tmp_path)
    factory = FactoryComposition(job, manifest.source_archive_ref)
    composition = ProductionBusinessBenchmarkComposition(
        resolver=resolver,
        factory_composition=factory,
        invocation_authority=invocations,
        executor_factory=lambda scope: object(),
        replay_store_factory=lambda scope: object(),
        execution_policy_factory=lambda scope: (lambda case: None),
        benchmark_policy_authority=PolicyAuthority(job),
        receipt_finalizer=Finalizer(),
        clock=lambda: NOW,
    )
    single = settings(job, manifest.candidate_id)
    aggregate = single.model_copy(
        update={
            "profile": "all",
            "selections": (
                single.selections[0],
                single.selections[0].model_copy(
                    update={
                        "profile": "renewal",
                        "job_id": UUID(int=job.job_id.int + 1),
                        "candidate_id": "renewal-candidate",
                    }
                ),
            ),
            "maximum_usd": Decimal("2.00"),
        }
    )

    with pytest.raises(BusinessBenchmarkProductionScopeError, match="single team"):
        await composition.run(aggregate)
    assert factory.calls == []


@pytest.mark.asyncio
async def test_factory_composition_rejects_suite_changed_after_production_preflight(
    tmp_path: Path,
) -> None:
    job, manifest, gateway, invocations, _ = authorities(tmp_path)
    preflighted = suite()
    changed = preflighted.model_copy(update={"suite_id": "claims-changed-v2"})

    class ChangingRepository:
        def suite_ref(self, profile_id: str, suite_version: int):
            return job.private_holdout_refs[0]

        def private_suite(self, reference):
            return changed

    composition = BusinessBenchmarkFactoryComposition(
        private_repository=ChangingRepository(),
        gateway_repository=object(),
        evaluator=object(),
        team_evaluator=object(),
        feedback_builder=object(),
    )

    with pytest.raises(ValueError, match="changed after production preflight"):
        await composition.run(
            job=job,
            profile_id=PROFILE_ID,
            suite_version=2,
            expected_suite_ref=job.private_holdout_refs[0],
            expected_suite=preflighted,
            attempt=1,
            candidate_ref=manifest.source_archive_ref,
            executor=object(),
            replay_store=object(),
            execution_policy_factory=lambda case: valid_execution_policy(),
            benchmark_policy=BusinessBenchmarkPolicyV1(
                schema="captain.business-benchmark-policy.v1"
            ),
            evaluation_invocation=invocations.evaluation_invocation(
                job=job, attempt=1
            ),
            report_invocation_factory=lambda evaluation: None,
            technical_executions=gateway.executions,
            budget_projection=gateway.budget,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_policy",
    ("model", "redaction", "tools", "cost", "latency", "captain_policy_digest"),
)
async def test_all_case_and_captain_policies_are_materialized_before_effect_factories(
    tmp_path: Path,
    invalid_policy: str,
) -> None:
    job, manifest, _, invocations, resolver = authorities(tmp_path)
    factory = FactoryComposition(job, manifest.source_archive_ref)
    created: list[str] = []
    updates: dict[str, object] = {}
    if invalid_policy == "model":
        updates["model_version"] = "unapproved-model"
    elif invalid_policy == "redaction":
        updates["redaction_policy_version"] = "different-redaction"
    elif invalid_policy == "tools":
        updates["allowed_tool_intents"] = (IntegrationIntent.N8N,)
    elif invalid_policy == "cost":
        updates["maximum_cost_micro_usd"] = 100_000
    elif invalid_policy == "latency":
        updates["maximum_latency_ms"] = 100_000
    policy = BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        allowed_tool_intents=(),
        maximum_cost_micro_usd=100,
        maximum_latency_ms=1000,
        redaction_policy_version="redaction-v1",
        baseline_system_policy_version="baseline-v1",
    ).model_copy(update=updates)
    policy_authority = PolicyAuthority(job)
    if invalid_policy == "captain_policy_digest":
        policy_authority.binding = policy_authority.binding.model_copy(
            update={"policy_sha256": "0" * 64}
        )
    composition = ProductionBusinessBenchmarkComposition(
        resolver=resolver,
        factory_composition=factory,
        invocation_authority=invocations,
        executor_factory=lambda scope: created.append("executor") or object(),
        replay_store_factory=lambda scope: created.append("replay") or object(),
        execution_policy_factory=lambda scope: (lambda case: policy),
        benchmark_policy_authority=policy_authority,
        receipt_finalizer=Finalizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(BusinessBenchmarkProductionScopeError, match="policy"):
        await composition.run(settings(job, manifest.candidate_id))

    assert created == []
    assert factory.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("evaluation", "feedback", "case_receipt"))
async def test_factory_result_requires_exact_typed_evaluation_feedback_and_case_receipts(
    tmp_path: Path,
    changed: str,
) -> None:
    job, manifest, _, invocations, resolver = authorities(tmp_path)
    factory = FactoryComposition(job, manifest.source_archive_ref)

    def mutate(result):
        if changed == "evaluation":
            result.evaluation = result.evaluation.model_copy(update={"attempt": 2})
        elif changed == "feedback":
            result.feedback = result.feedback.model_copy(update={"attempt": 2})
        else:
            first = result.case_receipts[0]
            result.case_receipts = (
                first.model_copy(
                    update={
                        "candidate": first.candidate.model_copy(
                            update={"case_sha256": "0" * 64}
                        )
                    }
                ),
                *result.case_receipts[1:],
            )
        return result

    factory.result_mutator = mutate
    execution_policy = BenchmarkExecutionPolicyV1(
        schema="captain.business-benchmark-execution-policy.v1",
        model_version="approved-model-id",
        maximum_cost_micro_usd=100,
        maximum_latency_ms=1000,
        redaction_policy_version="redaction-v1",
        baseline_system_policy_version="baseline-v1",
    )
    composition = ProductionBusinessBenchmarkComposition(
        resolver=resolver,
        factory_composition=factory,
        invocation_authority=invocations,
        executor_factory=lambda scope: object(),
        replay_store_factory=lambda scope: object(),
        execution_policy_factory=lambda scope: (lambda case: execution_policy),
        benchmark_policy_authority=PolicyAuthority(job),
        receipt_finalizer=Finalizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(BusinessBenchmarkProductionScopeError, match=changed.replace("_", " ")):
        await composition.run(settings(job, manifest.candidate_id))
