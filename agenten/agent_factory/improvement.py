"""Captain-authorized, evidence-bounded candidate revision planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone

from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    CandidateChangedComponent,
    CandidateRevisionV1,
    FactoryFeedbackRecommendation,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


class ImprovementBuilder:
    """Describe the smallest authorized repair; never mutate candidate files."""

    def __init__(
        self,
        *,
        candidate_ref: ArtifactRef,
        codex_session_ref: ArtifactRef,
        clock: Callable[[], datetime],
        authorization: FactoryImprovementAuthorizationV1 | None = None,
        component_by_assertion: Mapping[
            str, CandidateChangedComponent
        ] | None = None,
    ) -> None:
        self._authorization = authorization
        self._candidate_ref = candidate_ref
        self._codex_session_ref = codex_session_ref
        self._component_by_assertion = dict(component_by_assertion or {})
        self._clock = clock

    def build(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        prior_candidate: ArtifactRef,
        evaluation: TeamEvaluationV1,
        authorization: FactoryImprovementAuthorizationV1 | None = None,
    ) -> CandidateRevisionV1:
        """Return a child-candidate assignment bound to Captain's request block."""

        authority = authorization or self._authorization
        if authority is None:
            raise ValueError(
                "Captain IMPROVEMENT_REQUESTED authorization is required"
            )
        now = self._validate_bindings(
            invocation,
            prior_candidate,
            evaluation,
            authority,
        )
        failed_ids = tuple(
            outcome.assertion_id
            for outcome in evaluation.assertion_outcomes
            if outcome.status == "failed"
        )
        failed_benchmark_metric_ids = evaluation.failed_benchmark_metric_ids
        changed_components = _unique_components(
            (
                self._component_by_assertion.get(
                    assertion_id,
                    _component_for_assertion(assertion_id),
                )
                for assertion_id in failed_ids
            )
        )
        changed_components = _unique_components(
            (
                *changed_components,
                *(
                    component
                    for reason_code in evaluation.benchmark_reason_codes
                    for component in _components_for_benchmark_reason(reason_code)
                ),
                *(
                    component
                    for metric_id in failed_benchmark_metric_ids
                    for component in _components_for_benchmark_metric(metric_id)
                ),
            )
        )
        if not changed_components:
            raise ValueError("improvement requires an evidence-implicated component")
        evidence_refs = _unique_refs(
            (
                authority.authorization_ref,
                authority.failed_evaluation.artifact_ref,
                prior_candidate,
                self._candidate_ref,
                self._codex_session_ref,
                *evaluation.evidence_refs,
            )
        )
        artifact_ref = _content_ref(
            "candidate-revision",
            {
                "invocation_id": str(invocation.invocation_id),
                "job_id": str(invocation.job_id),
                "correlation_id": str(invocation.correlation_id),
                "subject_version": invocation.subject_version,
                "attempt": invocation.attempt,
                "released_skill_sha256": invocation.released_skill.content_sha256,
                "authorization_sha256": authority.authorization_ref.sha256,
                "parent_candidate_sha256": prior_candidate.sha256,
                "candidate_sha256": self._candidate_ref.sha256,
                "failed_assertion_ids": list(failed_ids),
                "failed_benchmark_metric_ids": list(failed_benchmark_metric_ids),
                "regression_assertion_ids": list(
                    authority.prior_green_assertion_ids
                ),
                "regression_benchmark_metric_ids": list(
                    authority.prior_green_benchmark_metric_ids
                ),
                "changed_components": [item.value for item in changed_components],
                "evidence_sha256": [reference.sha256 for reference in evidence_refs],
            },
        )
        return CandidateRevisionV1(
            schema_name="hermes.factory-candidate-revision.v1",
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
            parent_candidate_ref=prior_candidate,
            candidate_ref=self._candidate_ref,
            failed_assertion_ids=failed_ids,
            failed_benchmark_metric_ids=failed_benchmark_metric_ids,
            changed_components=changed_components,
            regression_assertion_ids=authority.prior_green_assertion_ids,
            regression_benchmark_metric_ids=(
                authority.prior_green_benchmark_metric_ids
            ),
            codex_session_ref=self._codex_session_ref,
        )

    def _validate_bindings(
        self,
        invocation: FactorySkillInvocationV1,
        prior_candidate: ArtifactRef,
        evaluation: TeamEvaluationV1,
        authority: FactoryImprovementAuthorizationV1,
    ) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("improvement clock must be UTC")
        if invocation.step is not FactorySkillStep.IMPROVE_TEAM:
            raise ValueError("improvement requires the improve_team invocation")
        if not invocation.lease.issued_at <= now < invocation.lease.expires_at:
            raise ValueError("improvement requires an active Tool Integrator lease")
        if (
            authority.failed_evaluation != evaluation
            or authority.prior_candidate_ref != prior_candidate
            or authority.authorized_attempt != invocation.attempt
            or invocation.input_ref != authority.authorization_ref
            or invocation.job_id != evaluation.job_id
            or invocation.correlation_id != evaluation.correlation_id
            or invocation.subject_version != evaluation.subject_version
            or invocation.acceptance_assertion_ids
            != evaluation.acceptance_assertion_ids
            or authority.request_block.job_id != invocation.job_id
            or authority.request_block.correlation_id != invocation.correlation_id
            or authority.request_block.subject_version != invocation.subject_version
        ):
            raise ValueError("improvement authorization binding does not match invocation")
        if evaluation.recommendation is not FactoryFeedbackRecommendation.RETRY_BUILD:
            raise ValueError("Captain improvement authorization is not a retry decision")
        if evaluation.failure_class not in {
            "behavioral_failure",
            "test_regression",
        }:
            raise ValueError("Captain improvement authorization is not behavioral")
        failed_ids = tuple(
            outcome.assertion_id
            for outcome in evaluation.assertion_outcomes
            if outcome.status == "failed"
        )
        failed_benchmark_metric_ids = evaluation.failed_benchmark_metric_ids
        if not failed_ids and not failed_benchmark_metric_ids:
            raise ValueError(
                "improvement authorization contains no failed assertion or benchmark metric"
            )
        if set(failed_ids) & set(authority.prior_green_assertion_ids):
            raise ValueError("improvement cannot weaken a prior-green assertion")
        if set(failed_benchmark_metric_ids) & set(
            authority.prior_green_benchmark_metric_ids
        ):
            raise ValueError("improvement cannot weaken a prior-green benchmark metric")
        if self._candidate_ref == prior_candidate:
            raise ValueError("improvement must produce a child candidate")
        return now


def _component_for_assertion(assertion_id: str) -> CandidateChangedComponent:
    normalized = assertion_id.lower()
    ordered = (
        (("system_prompt", "answer_quality", "relevance"), CandidateChangedComponent.SYSTEM_PROMPT),
        (("user_prompt",), CandidateChangedComponent.USER_PROMPT),
        (("context",), CandidateChangedComponent.CONTEXT),
        (("tool",), CandidateChangedComponent.TOOL_CONTRACT),
        (("model",), CandidateChangedComponent.MODEL_CLIENT),
        (("memory",), CandidateChangedComponent.MEMORY),
        (("conversation", "swarm", "selector", "round_robin"), CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN),
        (("handoff",), CandidateChangedComponent.HANDOFFS),
        (("termination", "stop_reason"), CandidateChangedComponent.TERMINATION),
        (("n8n", "workflow"), CandidateChangedComponent.N8N_WORKFLOW),
        (("test", "regression"), CandidateChangedComponent.TESTS),
        (("documentation", "runbook"), CandidateChangedComponent.DOCUMENTATION),
    )
    for markers, component in ordered:
        if any(marker in normalized for marker in markers):
            return component
    return CandidateChangedComponent.AGENT_CODE


def _components_for_benchmark_reason(
    reason_code: str,
) -> tuple[CandidateChangedComponent, ...]:
    decision_components = (
        CandidateChangedComponent.SYSTEM_PROMPT,
        CandidateChangedComponent.CONTEXT,
        CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
    )
    conversation_components = (
        CandidateChangedComponent.MODEL_CLIENT,
        CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
    )
    return {
        "wrong_decision": decision_components,
        "missing_rationale": decision_components,
        "below_minimum_correctness": decision_components,
        "below_baseline_correctness": decision_components,
        "unsafe_tool_intent": (CandidateChangedComponent.TOOL_CONTRACT,),
        "mandatory_handoff_missed": (CandidateChangedComponent.HANDOFFS,),
        "below_baseline_completion": (
            CandidateChangedComponent.TERMINATION,
            CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
        ),
        "cost_ratio_exceeded": conversation_components,
        "zero_baseline_cost_with_candidate_spend": conversation_components,
        "latency_ratio_exceeded": conversation_components,
        "zero_baseline_latency_with_candidate_time": conversation_components,
    }.get(reason_code, ())


def _components_for_benchmark_metric(
    metric_id: str,
) -> tuple[CandidateChangedComponent, ...]:
    decision_components = (
        CandidateChangedComponent.SYSTEM_PROMPT,
        CandidateChangedComponent.CONTEXT,
        CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
    )
    model_components = (
        CandidateChangedComponent.MODEL_CLIENT,
        CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
    )
    return {
        "decision_correctness": decision_components,
        "rationale_completeness": decision_components,
        "baseline_correctness": decision_components,
        "tool_safety": (CandidateChangedComponent.TOOL_CONTRACT,),
        "mandatory_handoff": (CandidateChangedComponent.HANDOFFS,),
        "terminal_completion": (
            CandidateChangedComponent.TERMINATION,
            CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
        ),
        "baseline_completion": (
            CandidateChangedComponent.TERMINATION,
            CandidateChangedComponent.AUTOGEN_CONVERSATION_PATTERN,
        ),
        "cost_efficiency": model_components,
        "latency_efficiency": model_components,
    }.get(metric_id, ())


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


def _unique_components(
    components: Iterable[CandidateChangedComponent],
) -> tuple[CandidateChangedComponent, ...]:
    return tuple(dict.fromkeys(components))
