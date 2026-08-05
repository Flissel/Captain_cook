from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.technical_improvement_contracts import (
    CaptainTechnicalFailureEvaluationV1,
    build_captain_technical_failure_evaluation,
    captain_technical_failure_evaluation_binding,
    validate_captain_technical_failure_evaluation,
)
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.outcome_contracts import AssertionOutcome
from agenten.agent_factory.skill_sequence import (
    build_factory_improvement_authorization,
    validate_factory_improvement_authorization,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
JOB_ID = UUID("00000000-0000-0000-0000-000000000401")
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000402")


def _ref(name: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://factory/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _evaluation() -> CaptainTechnicalFailureEvaluationV1:
    evidence = _ref("evidence", "1" * 64)
    return build_captain_technical_failure_evaluation(
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        subject_version=3,
        attempt=1,
        source_phase=FactoryPhase.REAL_CASE_EVIDENCE,
        source_block_id=UUID("00000000-0000-0000-0000-000000000403"),
        occurred_at=NOW,
        candidate_ref=_ref("candidate", "2" * 64),
        acceptance_assertion_ids=(
            "business_value",
            "safe_tool_use",
            "mandatory_handoff",
        ),
        assertion_outcomes=(
            AssertionOutcome(
                assertion_id="business_value",
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(evidence,),
            ),
            AssertionOutcome(
                assertion_id="safe_tool_use",
                status="passed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(evidence,),
            ),
            AssertionOutcome(
                assertion_id="mandatory_handoff",
                status="failed",
                integration_intent=IntegrationIntent.NONE,
                evidence_refs=(evidence,),
            ),
        ),
        evidence_refs=(evidence,),
        failure_class="behavioral_failure",
        recommendation=FactoryFeedbackRecommendation.RETRY_BUILD,
    )


def test_technical_failure_evaluation_is_content_bound_and_retains_prior_green() -> None:
    evaluation = _evaluation()

    assert evaluation.artifact_ref.uri.endswith(evaluation.artifact_ref.sha256)
    assert evaluation.prior_green_regression_ids == ("safe_tool_use",)
    assert validate_captain_technical_failure_evaluation(evaluation) is evaluation


def test_v1_binding_preserves_legacy_identity_when_diagnostics_are_absent() -> None:
    evaluation = _evaluation()

    assert evaluation.technical_diagnostic_codes == ()
    assert (
        "technical_diagnostic_codes"
        not in captain_technical_failure_evaluation_binding(evaluation)
    )
    assert validate_captain_technical_failure_evaluation(evaluation) is evaluation


def test_technical_failure_evaluation_rejects_tampered_binding() -> None:
    evaluation = _evaluation().model_copy(
        update={"artifact_ref": _ref("technical-failure", "f" * 64)}
    )

    with pytest.raises(ValueError, match="content binding"):
        validate_captain_technical_failure_evaluation(evaluation)


def test_technical_failure_evaluation_requires_a_failed_assertion() -> None:
    evaluation = _evaluation()
    passed = tuple(
        outcome.model_copy(update={"status": "passed"})
        for outcome in evaluation.assertion_outcomes
    )

    with pytest.raises(ValueError, match="failed assertion"):
        build_captain_technical_failure_evaluation(
            job_id=evaluation.job_id,
            correlation_id=evaluation.correlation_id,
            subject_version=evaluation.subject_version,
            attempt=evaluation.attempt,
            source_phase=evaluation.source_phase,
            source_block_id=evaluation.source_block_id,
            occurred_at=evaluation.occurred_at,
            candidate_ref=evaluation.candidate_ref,
            acceptance_assertion_ids=evaluation.acceptance_assertion_ids,
            assertion_outcomes=passed,
            evidence_refs=evaluation.evidence_refs,
            failure_class="behavioral_failure",
            recommendation=FactoryFeedbackRecommendation.RETRY_BUILD,
        )


def test_improvement_authorization_accepts_exact_technical_failure() -> None:
    evaluation = _evaluation()
    request = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000404"),
        job_id=evaluation.job_id,
        correlation_id=evaluation.correlation_id,
        causation_id=evaluation.source_block_id,
        occurred_at=evaluation.occurred_at,
        producer="captain",
        subject_version=evaluation.subject_version,
        attempt=evaluation.attempt,
        phase=FactoryPhase.IMPROVEMENT_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(evaluation.candidate_ref,),
        evidence_refs=(evaluation.artifact_ref,),
        assertion_ids=evaluation.prior_green_regression_ids,
    )

    authorization = build_factory_improvement_authorization(
        request_block=request,
        failed_evaluation=evaluation,
        prior_candidate_ref=evaluation.candidate_ref,
    )

    assert authorization.authorized_attempt == 2
    assert authorization.failed_evaluation is evaluation
    assert authorization.authorization_ref.uri.endswith(
        authorization.authorization_ref.sha256
    )
    assert "authorization" not in authorization.authorization_ref.uri
    assert validate_factory_improvement_authorization(authorization) is authorization


def test_improvement_authorization_rejects_tampered_technical_failure() -> None:
    evaluation = _evaluation()
    request = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000405"),
        job_id=evaluation.job_id,
        correlation_id=evaluation.correlation_id,
        causation_id=evaluation.source_block_id,
        occurred_at=evaluation.occurred_at,
        producer="captain",
        subject_version=evaluation.subject_version,
        attempt=evaluation.attempt,
        phase=FactoryPhase.IMPROVEMENT_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(evaluation.candidate_ref,),
        evidence_refs=(evaluation.artifact_ref,),
        assertion_ids=evaluation.prior_green_regression_ids,
    )
    authorization = build_factory_improvement_authorization(
        request_block=request,
        failed_evaluation=evaluation,
        prior_candidate_ref=evaluation.candidate_ref,
    )
    tampered = authorization.model_copy(
        update={
            "authorization_ref": _ref("improvement-authorization", "e" * 64)
        }
    )

    with pytest.raises(ValueError, match="content binding"):
        validate_factory_improvement_authorization(tampered)
