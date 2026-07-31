"""Captain-owned evaluation of failed pre-benchmark technical evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.outcome_contracts import AssertionOutcome
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
)
from agenten.agent_runtime.contracts import ArtifactRef


_TECHNICAL_FAILURE_PHASES = {
    FactoryPhase.BUILD_FAILED,
    FactoryPhase.REAL_CASE_EVIDENCE,
}


class CaptainTechnicalFailureEvaluationV1(BaseModel):
    """Public-safe failed technical gate used only to authorize a rebuild."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    schema_name: Literal["captain.factory-technical-failure-evaluation.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    artifact_ref: ArtifactRef
    producer: Literal["captain"]
    status: Literal["failed"]
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    source_phase: FactoryPhase
    source_block_id: UUID
    occurred_at: datetime
    candidate_ref: ArtifactRef
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    assertion_outcomes: tuple[AssertionOutcome, ...] = Field(min_length=1)
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    prior_green_regression_ids: tuple[str, ...] = ()
    failed_benchmark_metric_ids: tuple[()] = ()
    prior_green_benchmark_metric_ids: tuple[()] = ()
    benchmark_reason_codes: tuple[()] = ()
    failure_class: Literal["behavioral_failure", "test_regression"]
    recommendation: Literal[FactoryFeedbackRecommendation.RETRY_BUILD]

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("technical failure evaluation timestamp must be UTC")
        return value

    @field_validator("acceptance_assertion_ids", "prior_green_regression_ids")
    @classmethod
    def require_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("technical failure assertion IDs must be unique and non-empty")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence(
        cls, value: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        if len(value) != len(set(value)):
            raise ValueError("technical failure evidence refs must be unique")
        return value

    @model_validator(mode="after")
    def require_exact_failed_gate(self) -> "CaptainTechnicalFailureEvaluationV1":
        if self.source_phase not in _TECHNICAL_FAILURE_PHASES:
            raise ValueError("technical failure source phase is not eligible")
        outcome_ids = tuple(item.assertion_id for item in self.assertion_outcomes)
        if outcome_ids != self.acceptance_assertion_ids:
            raise ValueError("technical failure outcomes must match Captain assertions")
        failed_ids = tuple(
            item.assertion_id
            for item in self.assertion_outcomes
            if item.status == "failed"
        )
        if not failed_ids:
            raise ValueError("technical failure evaluation requires a failed assertion")
        passed_ids = tuple(
            item.assertion_id
            for item in self.assertion_outcomes
            if item.status == "passed"
        )
        if self.prior_green_regression_ids != passed_ids:
            raise ValueError("technical prior-green assertions must be exactly the passed assertions")
        return self


def build_captain_technical_failure_evaluation(
    *,
    job_id: UUID,
    correlation_id: UUID,
    subject_version: int,
    attempt: int,
    source_phase: FactoryPhase,
    source_block_id: UUID,
    occurred_at: datetime,
    candidate_ref: ArtifactRef,
    acceptance_assertion_ids: tuple[str, ...],
    assertion_outcomes: tuple[AssertionOutcome, ...],
    evidence_refs: tuple[ArtifactRef, ...],
    failure_class: Literal["behavioral_failure", "test_regression"],
    recommendation: FactoryFeedbackRecommendation,
) -> CaptainTechnicalFailureEvaluationV1:
    """Create a semantically content-addressed Captain technical evaluation."""

    passed_ids = tuple(
        outcome.assertion_id
        for outcome in assertion_outcomes
        if outcome.status == "passed"
    )
    placeholder = ArtifactRef(
        uri=f"artifact://factory/technical-failure-evaluation/{'0' * 64}",
        sha256="0" * 64,
        media_type="application/json",
    )
    evaluation = CaptainTechnicalFailureEvaluationV1(
        schema_name="captain.factory-technical-failure-evaluation.v1",
        artifact_ref=placeholder,
        producer="captain",
        status="failed",
        job_id=job_id,
        correlation_id=correlation_id,
        subject_version=subject_version,
        attempt=attempt,
        source_phase=source_phase,
        source_block_id=source_block_id,
        occurred_at=occurred_at,
        candidate_ref=candidate_ref,
        acceptance_assertion_ids=acceptance_assertion_ids,
        assertion_outcomes=assertion_outcomes,
        evidence_refs=evidence_refs,
        prior_green_regression_ids=passed_ids,
        failure_class=failure_class,
        recommendation=recommendation,
    )
    digest = captain_technical_failure_evaluation_sha256(evaluation)
    return evaluation.model_copy(
        update={
            "artifact_ref": ArtifactRef(
                uri=f"artifact://factory/technical-failure-evaluation/{digest}",
                sha256=digest,
                media_type="application/json",
            )
        }
    )


def captain_technical_failure_evaluation_binding(
    evaluation: CaptainTechnicalFailureEvaluationV1,
) -> dict[str, object]:
    return evaluation.model_dump(
        mode="json",
        by_alias=True,
        exclude={"artifact_ref"},
    )


def captain_technical_failure_evaluation_sha256(
    evaluation: CaptainTechnicalFailureEvaluationV1,
) -> str:
    content = json.dumps(
        captain_technical_failure_evaluation_binding(evaluation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def validate_captain_technical_failure_evaluation(
    evaluation: CaptainTechnicalFailureEvaluationV1,
) -> CaptainTechnicalFailureEvaluationV1:
    digest = captain_technical_failure_evaluation_sha256(evaluation)
    if evaluation.artifact_ref != ArtifactRef(
        uri=f"artifact://factory/technical-failure-evaluation/{digest}",
        sha256=digest,
        media_type="application/json",
    ):
        raise ValueError("technical failure evaluation content binding does not match")
    return evaluation


__all__ = [
    "CaptainTechnicalFailureEvaluationV1",
    "build_captain_technical_failure_evaluation",
    "captain_technical_failure_evaluation_binding",
    "captain_technical_failure_evaluation_sha256",
    "validate_captain_technical_failure_evaluation",
]
