"""Pure selection of Captain-authorized Hermes factory skill steps."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkMetricId
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactorySkillStep,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


class FactoryImprovementAuthorizationV1(BaseModel):
    """Captain evidence authorizing one retry against one failed candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.factory-improvement-authorization.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    authorization_ref: ArtifactRef
    authorized_attempt: int = Field(ge=2, le=5, strict=True)
    request_block: FactoryEvidenceBlock
    failed_evaluation: TeamEvaluationV1
    prior_candidate_ref: ArtifactRef
    prior_green_assertion_ids: tuple[str, ...]
    prior_green_benchmark_metric_ids: tuple[BusinessBenchmarkMetricId, ...] = ()

    @field_validator(
        "prior_green_assertion_ids", "prior_green_benchmark_metric_ids"
    )
    @classmethod
    def require_unique_prior_green_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("prior-green assertion IDs must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_exact_failed_attempt_binding(self) -> "FactoryImprovementAuthorizationV1":
        request = self.request_block
        evaluation = self.failed_evaluation
        if (
            request.phase is not FactoryPhase.IMPROVEMENT_REQUESTED
            or request.producer != "captain"
            or request.status is not FactoryBlockStatus.SUCCEEDED
        ):
            raise ValueError("improvement authorization requires a successful Captain request")
        if request.attempt + 1 != self.authorized_attempt:
            raise ValueError("improvement authorization attempt is stale")
        if (
            evaluation.job_id != request.job_id
            or evaluation.correlation_id != request.correlation_id
            or evaluation.subject_version != request.subject_version
            or evaluation.attempt != request.attempt
            or evaluation.occurred_at > request.occurred_at
        ):
            raise ValueError("failed evaluation does not match the Captain request")
        failed_ids = tuple(
            outcome.assertion_id
            for outcome in evaluation.assertion_outcomes
            if outcome.status == "failed"
        )
        failed_benchmark_metric_ids = evaluation.failed_benchmark_metric_ids
        if (
            (not failed_ids and not failed_benchmark_metric_ids)
            or evaluation.failure_class
            not in {"behavioral_failure", "test_regression"}
            or evaluation.recommendation is not FactoryFeedbackRecommendation.RETRY_BUILD
        ):
            raise ValueError("improvement authorization requires a failed evaluation")
        if evaluation.artifact_ref not in request.evidence_refs:
            raise ValueError("Captain request does not bind the failed evaluation")
        if self.prior_candidate_ref not in request.artifact_refs:
            raise ValueError("Captain request does not bind the prior candidate")
        if self.prior_green_assertion_ids != evaluation.prior_green_regression_ids:
            raise ValueError("prior-green assertions do not match the failed evaluation")
        if (
            self.prior_green_benchmark_metric_ids
            != evaluation.prior_green_benchmark_metric_ids
        ):
            raise ValueError(
                "prior-green benchmark metrics do not match the failed evaluation"
            )
        if set(failed_benchmark_metric_ids) & set(
            self.prior_green_benchmark_metric_ids
        ):
            raise ValueError("failed benchmark metrics cannot be prior-green guards")
        return self


class SkillSequencePolicy:
    """Map one leased factory role and attempt to its exact released steps."""

    def steps_for(
        self,
        *,
        role: FactoryRole,
        attempt: int,
    ) -> tuple[FactorySkillStep, ...]:
        if isinstance(attempt, bool) or not 1 <= attempt <= 5:
            raise ValueError("factory skill attempt must be between 1 and 5")
        if role is FactoryRole.AGENT_ARCHITECT:
            return (FactorySkillStep.DISCOVER,)
        if role is FactoryRole.TOOL_INTEGRATOR:
            if attempt > 1:
                return (
                    FactorySkillStep.IMPROVE_TEAM,
                    FactorySkillStep.BRIEF_CODEX,
                )
            return (FactorySkillStep.BRIEF_CODEX,)
        if role is FactoryRole.REAL_CASE_TESTER:
            return (FactorySkillStep.EXECUTE_TEAM,)
        if role is FactoryRole.QUALITY_WARDEN:
            return (
                FactorySkillStep.EVALUATE_TEAM,
                FactorySkillStep.REPORT_CAPTAIN,
            )
        raise ValueError("unsupported factory role")
