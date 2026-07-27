"""Deterministic-first evaluation of immutable Factory team evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Protocol

from agenten.agent_factory.business_benchmark_contracts import (
    BUSINESS_BENCHMARK_METRIC_IDS,
    BenchmarkDisposition,
    BusinessBenchmarkSummaryV1,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.outcome_contracts import AssertionOutcome
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


class TeamQualitativeJudge(Protocol):
    """Optional qualitative judge invoked only after deterministic gates pass."""

    def evaluate(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        candidate_ref: ArtifactRef,
        executions: tuple[TeamExecutionEvidenceV1, ...],
    ) -> ArtifactRef: ...


class TeamEvaluationService:
    """Build one redacted evaluation without mutating the candidate or assertions."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        judge: TeamQualitativeJudge | None = None,
    ) -> None:
        self._clock = clock
        self._judge = judge

    def evaluate(
        self,
        invocation: FactorySkillInvocationV1,
        candidate_ref: ArtifactRef,
        execution: TeamExecutionEvidenceV1 | tuple[TeamExecutionEvidenceV1, ...],
        *,
        benchmark_summary: BusinessBenchmarkSummaryV1,
        budget_projection: FactoryBudgetProjection | None = None,
        prior_evaluation: TeamEvaluationV1 | None = None,
    ) -> TeamEvaluationV1:
        """Evaluate released assertions before considering an optional judge."""

        executions = tuple(
            sorted(
                execution if isinstance(execution, tuple) else (execution,),
                key=lambda item: item.run_number,
            )
        )
        benchmark_summary = _validated_business_summary(benchmark_summary)
        now = self._validate_invocation(invocation)
        self._validate_bindings(
            invocation,
            candidate_ref,
            executions,
            benchmark_summary=benchmark_summary,
            budget_projection=budget_projection,
            prior_evaluation=prior_evaluation,
        )

        deterministic_passed, failure_class = self._deterministic_result(executions)
        failure_class = _combined_failure_class(
            failure_class,
            benchmark_summary,
        )
        if (
            failure_class in {"behavioral_failure", "test_regression"}
            and budget_projection is not None
            and budget_projection.remaining_usd == 0
        ):
            failure_class = "budget_exhausted"

        recommendation = self._recommendation_for(failure_class)
        judge_ref: ArtifactRef | None = None
        if (
            deterministic_passed
            and benchmark_summary.disposition is BenchmarkDisposition.PASSED
            and self._judge is not None
        ):
            judge_ref = self._judge.evaluate(
                invocation=invocation,
                candidate_ref=candidate_ref,
                executions=executions,
            )
            if not isinstance(judge_ref, ArtifactRef):
                raise ValueError("qualitative judge must return one redacted evidence ref")

        assertion_outcomes = _aggregate_assertion_outcomes(
            invocation.acceptance_assertion_ids,
            executions,
        )
        prior_green = set(
            ()
            if prior_evaluation is None
            else prior_evaluation.prior_green_regression_ids
        )
        for item in assertion_outcomes:
            if item.status == "passed":
                prior_green.add(item.assertion_id)
            else:
                prior_green.discard(item.assertion_id)
        regression_ids = tuple(
            assertion_id
            for assertion_id in invocation.acceptance_assertion_ids
            if assertion_id in prior_green
        )
        prior_green_benchmark = set(
            ()
            if prior_evaluation is None
            else prior_evaluation.prior_green_benchmark_metric_ids
        )
        prior_green_benchmark.update(benchmark_summary.passed_metric_ids)
        prior_green_benchmark.difference_update(benchmark_summary.failed_metric_ids)
        regression_benchmark_metric_ids = tuple(
            metric_id
            for metric_id in BUSINESS_BENCHMARK_METRIC_IDS
            if metric_id in prior_green_benchmark
        )

        holdout_refs = _unique_refs(
            reference
            for run in executions
            for outcome in run.execution_outcome.assertion_outcomes
            for reference in outcome.evidence_refs
        )
        deterministic_refs = _unique_refs(
            reference
            for run in executions
            for reference in (run.artifact_ref, *run.execution_outcome.evidence_refs)
        )
        cost_summary_ref = _content_ref(
            "cost-summary",
            (
                {
                    "job_id": str(budget_projection.job_id),
                    "limit_usd": str(budget_projection.limit_usd),
                    "consumed_usd": str(budget_projection.consumed_usd),
                    "reserved_usd": str(budget_projection.reserved_usd),
                    "remaining_usd": str(budget_projection.remaining_usd),
                }
                if budget_projection is not None
                else {
                    "usage_receipt_sha256": [
                        reference.sha256
                        for run in executions
                        for reference in run.usage_receipt_refs
                    ]
                }
            ),
        )
        latency_summary_ref = _content_ref(
            "latency-summary",
            {
                "run_numbers": [run.run_number for run in executions],
                "observed_at": [run.occurred_at.isoformat() for run in executions],
                "statuses": [run.status for run in executions],
            },
        )
        evidence_refs = _unique_refs(
            (
                candidate_ref,
                *(reference for run in executions for reference in run.evidence_refs),
                *holdout_refs,
                *deterministic_refs,
                cost_summary_ref,
                latency_summary_ref,
                benchmark_summary.artifact_ref,
                *((judge_ref,) if judge_ref is not None else ()),
            )
        )
        artifact_ref = _content_ref(
            "team-evaluation",
            {
                "invocation_id": str(invocation.invocation_id),
                "job_id": str(invocation.job_id),
                "correlation_id": str(invocation.correlation_id),
                "subject_version": invocation.subject_version,
                "attempt": invocation.attempt,
                "released_skill_sha256": invocation.released_skill.content_sha256,
                "candidate_sha256": candidate_ref.sha256,
                "assertion_ids": list(invocation.acceptance_assertion_ids),
                "execution_sha256": [run.artifact_ref.sha256 for run in executions],
                "benchmark_summary_sha256": benchmark_summary.artifact_ref.sha256,
                "benchmark_policy_id": benchmark_summary.policy.policy_id,
                "benchmark_disposition": benchmark_summary.disposition.value,
                "benchmark_reason_codes": list(benchmark_summary.reason_codes),
                "failed_benchmark_metric_ids": list(
                    benchmark_summary.failed_metric_ids
                ),
                "prior_green_benchmark_metric_ids": list(
                    regression_benchmark_metric_ids
                ),
                "recommendation": recommendation.value,
                "failure_class": failure_class,
                "evidence_sha256": [reference.sha256 for reference in evidence_refs],
            },
        )
        return TeamEvaluationV1(
            schema_name="hermes.factory-team-evaluation.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=now,
            producer="hermes",
            artifact_ref=artifact_ref,
            evidence_refs=evidence_refs,
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            assertion_outcomes=assertion_outcomes,
            holdout_receipt_refs=holdout_refs,
            deterministic_check_refs=deterministic_refs,
            judge_ref=judge_ref,
            prior_green_regression_ids=regression_ids,
            benchmark_summary_ref=benchmark_summary.artifact_ref,
            benchmark_policy_id=benchmark_summary.policy.policy_id,
            benchmark_disposition=benchmark_summary.disposition.value,
            benchmark_reason_codes=benchmark_summary.reason_codes,
            failed_benchmark_metric_ids=benchmark_summary.failed_metric_ids,
            prior_green_benchmark_metric_ids=regression_benchmark_metric_ids,
            cost_summary_ref=cost_summary_ref,
            latency_summary_ref=latency_summary_ref,
            failure_class=failure_class,
            recommendation=recommendation,
        )

    def _validate_invocation(self, invocation: FactorySkillInvocationV1) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("team evaluation clock must be UTC")
        if invocation.step is not FactorySkillStep.EVALUATE_TEAM:
            raise ValueError("team evaluation requires the evaluate_team invocation")
        if not invocation.lease.issued_at <= now < invocation.lease.expires_at:
            raise ValueError("team evaluation requires an active Quality Warden lease")
        return now

    @staticmethod
    def _validate_bindings(
        invocation: FactorySkillInvocationV1,
        candidate_ref: ArtifactRef,
        executions: tuple[TeamExecutionEvidenceV1, ...],
        *,
        benchmark_summary: BusinessBenchmarkSummaryV1,
        budget_projection: FactoryBudgetProjection | None,
        prior_evaluation: TeamEvaluationV1 | None,
    ) -> None:
        if not executions:
            raise ValueError("team evaluation requires immutable execution evidence")
        if (
            benchmark_summary.job_id != invocation.job_id
            or benchmark_summary.correlation_id != invocation.correlation_id
            or benchmark_summary.subject_version != invocation.subject_version
            or benchmark_summary.attempt != invocation.attempt
            or benchmark_summary.candidate_ref != candidate_ref
        ):
            raise ValueError("business benchmark binding does not match evaluation")
        if len({run.run_number for run in executions}) != len(executions):
            raise ValueError("team execution run-number binding is not unique")
        expected_assertions = invocation.acceptance_assertion_ids
        for run in executions:
            outcome_ids = tuple(
                outcome.assertion_id
                for outcome in run.execution_outcome.assertion_outcomes
            )
            if (
                run.job_id != invocation.job_id
                or run.correlation_id != invocation.correlation_id
                or run.subject_version != invocation.subject_version
                or run.attempt != invocation.attempt
                or run.invocation.job_id != invocation.job_id
                or run.invocation.correlation_id != invocation.correlation_id
                or run.invocation.subject_version != invocation.subject_version
                or run.invocation.attempt != invocation.attempt
                or run.candidate_ref != candidate_ref
            ):
                raise ValueError("team execution binding does not match evaluation")
            if (
                run.acceptance_assertion_ids != expected_assertions
                or outcome_ids != expected_assertions
            ):
                raise ValueError("execution evidence must contain exactly the Captain assertions")
            if run.execution_outcome.correlation_id != invocation.correlation_id:
                raise ValueError("execution outcome binding does not match evaluation")
        if budget_projection is not None:
            if budget_projection.job_id != invocation.job_id:
                raise ValueError("cost projection binding does not match evaluation")
            expected_remaining = (
                budget_projection.limit_usd
                - budget_projection.consumed_usd
                - budget_projection.reserved_usd
            )
            if expected_remaining != budget_projection.remaining_usd:
                raise ValueError("cost projection binding is inconsistent")
        if prior_evaluation is not None:
            if (
                prior_evaluation.job_id != invocation.job_id
                or prior_evaluation.correlation_id != invocation.correlation_id
                or prior_evaluation.subject_version != invocation.subject_version
                or prior_evaluation.attempt >= invocation.attempt
                or prior_evaluation.acceptance_assertion_ids != expected_assertions
            ):
                raise ValueError("prior evaluation binding does not match evaluation")

    @staticmethod
    def _deterministic_result(
        executions: tuple[TeamExecutionEvidenceV1, ...],
    ) -> tuple[bool, str | None]:
        termination_codes = {run.termination_reason for run in executions}
        if "credential_required" in termination_codes:
            return False, "credential_required"
        if "infrastructure_failure" in termination_codes:
            return False, "infrastructure_failure"
        if any(run.status == "unresolved" for run in executions):
            return False, "unresolved"
        if any(
            run.status == "succeeded" and not run.usage_receipt_refs
            for run in executions
        ):
            return False, "infrastructure_failure"
        if any(
            outcome.integration_intent is IntegrationIntent.N8N
            and not run.workflow_evidence_refs
            for run in executions
            for outcome in run.execution_outcome.assertion_outcomes
        ):
            return False, "infrastructure_failure"
        if any(
            run.execution_outcome.tool_versions and not run.tool_evidence_refs
            for run in executions
        ):
            return False, "infrastructure_failure"
        failed = tuple(
            outcome
            for run in executions
            for outcome in run.execution_outcome.assertion_outcomes
            if outcome.status == "failed"
        )
        if failed or any(
            run.status != "succeeded" or run.execution_outcome.status != "succeeded"
            for run in executions
        ):
            if any(run.termination_reason == "preflight_failed" for run in executions):
                return False, "test_regression"
            return False, "behavioral_failure"
        return True, None

    @staticmethod
    def _recommendation_for(
        failure_class: str | None,
    ) -> FactoryFeedbackRecommendation:
        return {
            None: FactoryFeedbackRecommendation.PROMOTE_CANDIDATE,
            "behavioral_failure": FactoryFeedbackRecommendation.RETRY_BUILD,
            "test_regression": FactoryFeedbackRecommendation.RETRY_BUILD,
            "tool_required": FactoryFeedbackRecommendation.BLOCKED_TOOL_REQUIRED,
            "credential_required": FactoryFeedbackRecommendation.BLOCKED_CREDENTIAL_REQUIRED,
            "infrastructure_failure": FactoryFeedbackRecommendation.BLOCKED_INFRASTRUCTURE,
            "budget_exhausted": FactoryFeedbackRecommendation.BUDGET_EXHAUSTED,
            "unresolved": FactoryFeedbackRecommendation.MANUAL_DECISION_REQUIRED,
        }[failure_class]


def _validated_business_summary(
    summary: BusinessBenchmarkSummaryV1,
) -> BusinessBenchmarkSummaryV1:
    if not isinstance(summary, BusinessBenchmarkSummaryV1):
        raise ValueError("authoritative business benchmark summary is required")
    try:
        return BusinessBenchmarkSummaryV1.model_validate(
            summary.model_dump(mode="json", by_alias=True)
        )
    except ValueError as exc:
        raise ValueError("business benchmark summary is not canonical") from exc


def _combined_failure_class(
    execution_failure: str | None,
    benchmark_summary: BusinessBenchmarkSummaryV1,
) -> str | None:
    if benchmark_summary.disposition is BenchmarkDisposition.PASSED:
        return execution_failure
    if "missing_receipt" in benchmark_summary.reason_codes:
        return "infrastructure_failure"
    if execution_failure is None:
        return "behavioral_failure"
    return execution_failure


def _content_ref(kind: str, payload: object) -> ArtifactRef:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return ArtifactRef(
        uri=f"artifact://factory/{kind}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _unique_refs(references: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    unique: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        unique.setdefault(
            (reference.uri, reference.sha256, reference.media_type),
            reference,
        )
    return tuple(unique.values())


def _aggregate_assertion_outcomes(
    assertion_ids: tuple[str, ...],
    executions: tuple[TeamExecutionEvidenceV1, ...],
) -> tuple[AssertionOutcome, ...]:
    """Retain the worst outcome for every assertion across all required runs."""

    aggregated: list[AssertionOutcome] = []
    for assertion_id in assertion_ids:
        observed = tuple(
            outcome
            for run in executions
            for outcome in run.execution_outcome.assertion_outcomes
            if outcome.assertion_id == assertion_id
        )
        intents = {outcome.integration_intent for outcome in observed}
        if len(intents) != 1:
            raise ValueError(
                "execution evidence changed an assertion integration intent"
            )
        status = (
            "failed"
            if any(outcome.status == "failed" for outcome in observed)
            or any(run.status == "unresolved" for run in executions)
            else "passed"
        )
        aggregated.append(
            AssertionOutcome(
                assertion_id=assertion_id,
                status=status,
                integration_intent=observed[0].integration_intent,
                evidence_refs=_unique_refs(
                    reference
                    for outcome in observed
                    for reference in outcome.evidence_refs
                ),
            )
        )
    return tuple(aggregated)
