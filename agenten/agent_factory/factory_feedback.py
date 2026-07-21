"""Redacted Hermes recommendation that leaves every decision with Captain."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactoryFeedbackV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef

if TYPE_CHECKING:
    from agenten.agent_factory.state_machine import FactoryProjection


class FactoryFeedbackBuilder:
    """Recompute one bounded recommendation from typed evidence references."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def build(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        candidate_ref: ArtifactRef,
        evaluation: TeamEvaluationV1,
        tool_gaps: tuple[ToolGapMarker, ...] = (),
        budget_projection: FactoryBudgetProjection | None = None,
        current_projection: FactoryProjection | None = None,
    ) -> FactoryFeedbackV1:
        """Return evidence-bound advice; this method cannot release or promote."""

        now = self._validate_bindings(
            invocation,
            candidate_ref,
            evaluation,
            tool_gaps=tool_gaps,
            budget_projection=budget_projection,
            current_projection=current_projection,
        )
        recommendation, reason_code = _recommendation_for(
            evaluation,
            tool_gaps=tool_gaps,
            budget_projection=budget_projection,
        )
        tool_gap_refs = tuple(gap.evidence_ref for gap in tool_gaps)
        evidence_refs = _unique_refs(
            (
                evaluation.artifact_ref,
                candidate_ref,
                evaluation.cost_summary_ref,
                evaluation.latency_summary_ref,
                *tool_gap_refs,
                *evaluation.evidence_refs,
            )
        )
        artifact_ref = _content_ref(
            "factory-feedback",
            {
                "invocation_id": str(invocation.invocation_id),
                "job_id": str(invocation.job_id),
                "correlation_id": str(invocation.correlation_id),
                "subject_version": invocation.subject_version,
                "attempt": invocation.attempt,
                "released_skill_sha256": invocation.released_skill.content_sha256,
                "candidate_sha256": candidate_ref.sha256,
                "evaluation_sha256": evaluation.artifact_ref.sha256,
                "assertion_ids": list(invocation.acceptance_assertion_ids),
                "tool_gap_ids": [gap.gap_id for gap in tool_gaps],
                "recommendation": recommendation.value,
                "reason_code": reason_code,
                "evidence_sha256": [reference.sha256 for reference in evidence_refs],
            },
        )
        return FactoryFeedbackV1(
            schema_name="hermes.factory-feedback.v1",
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
            recommendation=recommendation,
            assertion_ids=invocation.acceptance_assertion_ids,
            tool_gaps=tool_gaps,
            tool_gap_refs=tool_gap_refs,
            reason_codes=(reason_code,),
        )

    def _validate_bindings(
        self,
        invocation: FactorySkillInvocationV1,
        candidate_ref: ArtifactRef,
        evaluation: TeamEvaluationV1,
        *,
        tool_gaps: tuple[ToolGapMarker, ...],
        budget_projection: FactoryBudgetProjection | None,
        current_projection: FactoryProjection | None,
    ) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("factory feedback clock must be UTC")
        if invocation.step is not FactorySkillStep.REPORT_CAPTAIN:
            raise ValueError("factory feedback requires the report_captain invocation")
        if not invocation.lease.issued_at <= now < invocation.lease.expires_at:
            raise ValueError("factory feedback requires an active Quality Warden lease")
        if (
            invocation.job_id != evaluation.job_id
            or invocation.correlation_id != evaluation.correlation_id
            or invocation.subject_version != evaluation.subject_version
            or invocation.attempt != evaluation.attempt
            or invocation.acceptance_assertion_ids
            != evaluation.acceptance_assertion_ids
            or invocation.input_ref != evaluation.artifact_ref
        ):
            raise ValueError("factory feedback evaluation binding does not match invocation")
        if candidate_ref not in evaluation.evidence_refs:
            raise ValueError("factory feedback candidate binding is missing from evaluation")
        gap_ids = tuple(gap.gap_id for gap in tool_gaps)
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("factory feedback tool-gap binding contains duplicates")
        accepted = set(invocation.acceptance_assertion_ids)
        if any(set(gap.acceptance_assertion_ids) - accepted for gap in tool_gaps):
            raise ValueError("factory feedback tool gap contains non-Captain assertions")
        if budget_projection is not None and budget_projection.job_id != invocation.job_id:
            raise ValueError("factory feedback cost binding does not match job")
        if current_projection is not None:
            job = current_projection.job
            if (
                job.job_id != invocation.job_id
                or job.correlation_id != invocation.correlation_id
                or job.subject_version != invocation.subject_version
                or current_projection.attempt != invocation.attempt
                or current_projection.phase is None
                or current_projection.phase.value
                not in {"real_case_evidence", "quality_reviewed"}
            ):
                raise ValueError("factory feedback projection binding is stale")
        return now


def _recommendation_for(
    evaluation: TeamEvaluationV1,
    *,
    tool_gaps: tuple[ToolGapMarker, ...],
    budget_projection: FactoryBudgetProjection | None,
) -> tuple[FactoryFeedbackRecommendation, str]:
    if any(
        gap.severity == "required" and gap.status == "unresolved"
        for gap in tool_gaps
    ):
        return (
            FactoryFeedbackRecommendation.BLOCKED_TOOL_REQUIRED,
            "required_tool_unresolved",
        )
    if evaluation.failure_class == "credential_required":
        return (
            FactoryFeedbackRecommendation.BLOCKED_CREDENTIAL_REQUIRED,
            "credential_reference_missing",
        )
    if evaluation.failure_class == "infrastructure_failure":
        return (
            FactoryFeedbackRecommendation.BLOCKED_INFRASTRUCTURE,
            "leased_infrastructure_unavailable",
        )
    exhausted = (
        evaluation.failure_class == "budget_exhausted"
        or (
            evaluation.failure_class is not None
            and budget_projection is not None
            and budget_projection.remaining_usd == 0
        )
    )
    if exhausted:
        return FactoryFeedbackRecommendation.BUDGET_EXHAUSTED, "budget_exhausted"
    if evaluation.failure_class in {"behavioral_failure", "test_regression"}:
        return FactoryFeedbackRecommendation.RETRY_BUILD, "candidate_retry_required"
    if evaluation.failure_class is not None:
        return (
            FactoryFeedbackRecommendation.MANUAL_DECISION_REQUIRED,
            "evaluation_unresolved",
        )
    if (
        evaluation.recommendation is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
        and all(outcome.status == "passed" for outcome in evaluation.assertion_outcomes)
    ):
        return (
            FactoryFeedbackRecommendation.PROMOTE_CANDIDATE,
            "all_assertions_passed",
        )
    return (
        FactoryFeedbackRecommendation.MANUAL_DECISION_REQUIRED,
        "evaluation_unresolved",
    )


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
