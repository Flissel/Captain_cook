from __future__ import annotations

import pytest

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.skill_sequence import (
    FactoryImprovementAuthorizationV1,
    SkillSequencePolicy,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillStep,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_workflow_contracts import evaluation_payload
from tests.agent_factory.test_state_machine import block


@pytest.mark.parametrize(
    ("role", "attempt", "expected"),
    [
        (FactoryRole.AGENT_ARCHITECT, 1, (FactorySkillStep.DISCOVER,)),
        (FactoryRole.AGENT_ARCHITECT, 2, (FactorySkillStep.DISCOVER,)),
        (FactoryRole.TOOL_INTEGRATOR, 1, (FactorySkillStep.BRIEF_CODEX,)),
        (
            FactoryRole.TOOL_INTEGRATOR,
            2,
            (FactorySkillStep.IMPROVE_TEAM, FactorySkillStep.BRIEF_CODEX),
        ),
        (FactoryRole.REAL_CASE_TESTER, 1, (FactorySkillStep.EXECUTE_TEAM,)),
        (
            FactoryRole.QUALITY_WARDEN,
            1,
            (FactorySkillStep.EVALUATE_TEAM, FactorySkillStep.REPORT_CAPTAIN),
        ),
    ],
)
def test_role_attempt_maps_to_exact_skill_sequence(
    role: FactoryRole,
    attempt: int,
    expected: tuple[FactorySkillStep, ...],
) -> None:
    assert SkillSequencePolicy().steps_for(role=role, attempt=attempt) == expected


@pytest.mark.parametrize("attempt", [0, 6])
def test_sequence_rejects_attempt_outside_captain_limit(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        SkillSequencePolicy().steps_for(
            role=FactoryRole.TOOL_INTEGRATOR,
            attempt=attempt,
        )


def test_improvement_authorization_binds_captain_failure_and_prior_candidate() -> None:
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
        prior_green_regression_ids=["real_case_green"],
    )
    outcomes = evaluation_data["assertion_outcomes"]
    assert isinstance(outcomes, list)
    failed = outcomes[1]
    assert isinstance(failed, dict)
    failed["status"] = "failed"
    evaluation = TeamEvaluationV1.model_validate(evaluation_data)
    prior_candidate = ArtifactRef(
        uri="artifact://workflow/prior-candidate",
        sha256="9" * 64,
        media_type="application/zip",
    )
    request_data = block(FactoryPhase.IMPROVEMENT_REQUESTED).model_dump(
        mode="json",
        by_alias=True,
    )
    request_data.update(
        {
            "job_id": str(evaluation.job_id),
            "correlation_id": str(evaluation.correlation_id),
            "subject_version": evaluation.subject_version,
            "attempt": evaluation.attempt,
            "occurred_at": evaluation.occurred_at.isoformat(),
        }
    )
    request_data["artifact_refs"] = [prior_candidate.model_dump(mode="json")]
    request_data["evidence_refs"] = [
        evaluation.artifact_ref.model_dump(mode="json")
    ]
    request_block = FactoryEvidenceBlock.model_validate(request_data)

    authorization = FactoryImprovementAuthorizationV1(
        schema_name="captain.factory-improvement-authorization.v1",
        authorization_ref=ArtifactRef(
            uri="artifact://factory/improvement-request",
            sha256="8" * 64,
            media_type="application/json",
        ),
        authorized_attempt=2,
        request_block=request_block,
        failed_evaluation=evaluation,
        prior_candidate_ref=prior_candidate,
        prior_green_assertion_ids=("real_case_green",),
        prior_green_benchmark_metric_ids=("coverage",),
    )

    assert authorization.request_block.phase is FactoryPhase.IMPROVEMENT_REQUESTED
    assert authorization.failed_evaluation.failure_class == "behavioral_failure"


def test_improvement_authorization_rejects_unbound_prior_candidate() -> None:
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
        prior_green_regression_ids=["real_case_green"],
    )
    outcomes = evaluation_data["assertion_outcomes"]
    assert isinstance(outcomes, list)
    failed = outcomes[1]
    assert isinstance(failed, dict)
    failed["status"] = "failed"
    evaluation = TeamEvaluationV1.model_validate(evaluation_data)
    request_data = block(FactoryPhase.IMPROVEMENT_REQUESTED).model_dump(
        mode="json",
        by_alias=True,
    )
    request_data.update(
        {
            "job_id": str(evaluation.job_id),
            "correlation_id": str(evaluation.correlation_id),
            "subject_version": evaluation.subject_version,
            "attempt": evaluation.attempt,
            "occurred_at": evaluation.occurred_at.isoformat(),
        }
    )
    request_data["evidence_refs"] = [
        evaluation.artifact_ref.model_dump(mode="json")
    ]

    with pytest.raises(ValueError, match="prior candidate"):
        FactoryImprovementAuthorizationV1(
            schema_name="captain.factory-improvement-authorization.v1",
            authorization_ref=ArtifactRef(
                uri="artifact://factory/improvement-request",
                sha256="8" * 64,
                media_type="application/json",
            ),
            authorized_attempt=2,
            request_block=FactoryEvidenceBlock.model_validate(request_data),
            failed_evaluation=evaluation,
            prior_candidate_ref=ArtifactRef(
                uri="artifact://workflow/prior-candidate",
                sha256="9" * 64,
                media_type="application/zip",
            ),
            prior_green_assertion_ids=("real_case_green",),
            prior_green_benchmark_metric_ids=("coverage",),
        )


def test_improvement_authorization_accepts_benchmark_only_failure() -> None:
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
        benchmark_disposition="failed",
        benchmark_reason_codes=["unsafe_tool_intent"],
        failed_benchmark_metric_ids=["tool_safety"],
        prior_green_benchmark_metric_ids=["coverage"],
    )
    outcomes = evaluation_data["assertion_outcomes"]
    assert isinstance(outcomes, list)
    evaluation = TeamEvaluationV1.model_validate(evaluation_data)
    prior_candidate = ArtifactRef(
        uri="artifact://workflow/prior-candidate",
        sha256="9" * 64,
        media_type="application/zip",
    )
    request_data = block(FactoryPhase.IMPROVEMENT_REQUESTED).model_dump(
        mode="json", by_alias=True
    )
    request_data.update(
        {
            "job_id": str(evaluation.job_id),
            "correlation_id": str(evaluation.correlation_id),
            "subject_version": evaluation.subject_version,
            "attempt": evaluation.attempt,
            "occurred_at": evaluation.occurred_at.isoformat(),
            "artifact_refs": [prior_candidate.model_dump(mode="json")],
            "evidence_refs": [evaluation.artifact_ref.model_dump(mode="json")],
        }
    )

    authorization = FactoryImprovementAuthorizationV1(
        schema_name="captain.factory-improvement-authorization.v1",
        authorization_ref=ArtifactRef(
            uri="artifact://factory/improvement-request",
            sha256="8" * 64,
            media_type="application/json",
        ),
        authorized_attempt=2,
        request_block=FactoryEvidenceBlock.model_validate(request_data),
        failed_evaluation=evaluation,
        prior_candidate_ref=prior_candidate,
        prior_green_assertion_ids=evaluation.prior_green_regression_ids,
        prior_green_benchmark_metric_ids=("coverage",),
    )

    assert authorization.failed_evaluation.failed_benchmark_metric_ids == (
        "tool_safety",
    )
    assert authorization.prior_green_benchmark_metric_ids == ("coverage",)
