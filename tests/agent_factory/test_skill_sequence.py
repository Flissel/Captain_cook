from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import (
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.skill_sequence import (
    FactoryImprovementAuthorizationV1,
    FactoryRuntimeRetryAuthorizationV1,
    SkillSequencePolicy,
    validate_factory_runtime_retry_authorization,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillStep,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_workflow_contracts import evaluation_payload
from tests.agent_factory.test_state_machine import block


RUNTIME_RETRY_NOW = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)


def _runtime_retry_payload() -> dict[str, object]:
    return {
        "schema": "captain.factory-runtime-retry-authorization.v1",
        "authorization_ref": {
            "uri": f"artifact://factory/runtime-retry/{'a' * 64}",
            "sha256": "a" * 64,
            "media_type": "application/json",
        },
        "producer": "captain",
        "status": "succeeded",
        "job_id": UUID("00000000-0000-0000-0000-000000000301"),
        "correlation_id": UUID("00000000-0000-0000-0000-000000000302"),
        "subject_version": 3,
        "attempt": 1,
        "invocation_id": UUID("00000000-0000-0000-0000-000000000303"),
        "idempotency_key": "b" * 64,
        "lease_id": "factory-lease-1",
        "checkpoint_ref": {
            "uri": f"artifact://factory/codex-checkpoint/{'c' * 64}",
            "sha256": "c" * 64,
            "media_type": "application/json",
        },
        "terminal_receipt_ref": {
            "uri": f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
            "sha256": "d" * 64,
            "media_type": "application/json",
        },
        "workspace_ref": "workspace://factory/job-301/attempt-1",
        "base_revision": "e" * 40,
        "scaffold_manifest_sha256": "f" * 64,
        "brief_sha256": "1" * 64,
        "resume_ordinal": 1,
        "maximum_runtime_seconds": 300,
        "issued_at": RUNTIME_RETRY_NOW,
        "expires_at": RUNTIME_RETRY_NOW + timedelta(minutes=5),
    }


def _validate_runtime_retry(
    authorization: FactoryRuntimeRetryAuthorizationV1,
    **updates: object,
) -> FactoryRuntimeRetryAuthorizationV1:
    values: dict[str, object] = {
        "job_id": UUID("00000000-0000-0000-0000-000000000301"),
        "correlation_id": UUID("00000000-0000-0000-0000-000000000302"),
        "subject_version": 3,
        "attempt": 1,
        "invocation_id": UUID("00000000-0000-0000-0000-000000000303"),
        "idempotency_key": "b" * 64,
        "lease_id": "factory-lease-1",
        "checkpoint_ref": ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
            sha256="c" * 64,
            media_type="application/json",
        ),
        "terminal_receipt_ref": ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
            sha256="d" * 64,
            media_type="application/json",
        ),
        "workspace_ref": "workspace://factory/job-301/attempt-1",
        "base_revision": "e" * 40,
        "scaffold_manifest_sha256": "f" * 64,
        "brief_sha256": "1" * 64,
        "current_resume_ordinal": 0,
        "remaining_runtime_seconds": 300,
        "now": RUNTIME_RETRY_NOW,
    }
    values.update(updates)
    return validate_factory_runtime_retry_authorization(authorization, **values)


@pytest.mark.parametrize(
    ("role", "attempt", "expected"),
    [
        (FactoryRole.AGENT_ARCHITECT, 1, (FactorySkillStep.DISCOVER,)),
        (FactoryRole.AGENT_ARCHITECT, 2, (FactorySkillStep.DISCOVER,)),
        (
            FactoryRole.TOOL_INTEGRATOR,
            1,
            (FactorySkillStep.BRIEF_CODEX, FactorySkillStep.SEAL_CODEX_BUILD),
        ),
        (
            FactoryRole.TOOL_INTEGRATOR,
            2,
            (
                FactorySkillStep.IMPROVE_TEAM,
                FactorySkillStep.BRIEF_CODEX,
                FactorySkillStep.SEAL_CODEX_BUILD,
            ),
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


def test_legacy_v1_sequence_can_omit_additive_codex_seal() -> None:
    assert SkillSequencePolicy().steps_for(
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        require_codex_seal=False,
    ) == (FactorySkillStep.BRIEF_CODEX,)


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


def test_prior_green_benchmark_duplicate_has_metric_specific_diagnostic() -> None:
    with pytest.raises(ValueError, match="benchmark metric IDs"):
        FactoryImprovementAuthorizationV1.require_unique_prior_green_benchmark_metric_ids(
            ("coverage", "coverage")
        )


def test_runtime_retry_authorization_accepts_exact_captain_binding() -> None:
    authorization = FactoryRuntimeRetryAuthorizationV1.model_validate(
        _runtime_retry_payload()
    )

    assert _validate_runtime_retry(authorization) is authorization


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("producer", "hermes", "Captain"),
        ("status", "failed", "successful"),
        (
            "job_id",
            UUID("00000000-0000-0000-0000-000000000399"),
            "binding",
        ),
        (
            "correlation_id",
            UUID("00000000-0000-0000-0000-000000000398"),
            "binding",
        ),
        ("attempt", 2, "binding"),
        (
            "invocation_id",
            UUID("00000000-0000-0000-0000-000000000397"),
            "binding",
        ),
        ("idempotency_key", "9" * 64, "binding"),
        ("lease_id", "factory-lease-2", "binding"),
        (
            "checkpoint_ref",
            {
                "uri": f"artifact://factory/codex-checkpoint/{'8' * 64}",
                "sha256": "8" * 64,
                "media_type": "application/json",
            },
            "checkpoint",
        ),
        (
            "terminal_receipt_ref",
            {
                "uri": f"artifact://factory/codex-terminal-receipt/{'7' * 64}",
                "sha256": "7" * 64,
                "media_type": "application/json",
            },
            "receipt",
        ),
        ("resume_ordinal", 2, "ordinal"),
    ],
)
def test_runtime_retry_authorization_rejects_mismatched_binding(
    field: str,
    value: object,
    diagnostic: str,
) -> None:
    payload = _runtime_retry_payload()
    payload[field] = value
    if field in {"producer", "status"}:
        with pytest.raises(ValueError, match=diagnostic):
            FactoryRuntimeRetryAuthorizationV1.model_validate(payload)
        return
    authorization = FactoryRuntimeRetryAuthorizationV1.model_validate(payload)

    with pytest.raises(ValueError, match=diagnostic):
        _validate_runtime_retry(authorization)


@pytest.mark.parametrize(
    ("updates", "diagnostic"),
    [
        ({"now": RUNTIME_RETRY_NOW - timedelta(seconds=1)}, "not active"),
        ({"now": RUNTIME_RETRY_NOW + timedelta(minutes=5)}, "expired"),
        ({"remaining_runtime_seconds": 299}, "runtime"),
        ({"current_resume_ordinal": 1}, "ordinal"),
    ],
)
def test_runtime_retry_authorization_rejects_stale_overbound_or_reused_authority(
    updates: dict[str, object],
    diagnostic: str,
) -> None:
    authorization = FactoryRuntimeRetryAuthorizationV1.model_validate(
        _runtime_retry_payload()
    )

    with pytest.raises(ValueError, match=diagnostic):
        _validate_runtime_retry(authorization, **updates)


def test_runtime_retry_authorization_runtime_cannot_outlive_its_expiry() -> None:
    payload = _runtime_retry_payload()
    payload["maximum_runtime_seconds"] = 60
    payload["expires_at"] = RUNTIME_RETRY_NOW + timedelta(seconds=30)
    authorization = FactoryRuntimeRetryAuthorizationV1.model_validate(payload)

    with pytest.raises(ValueError, match="authorization window"):
        _validate_runtime_retry(authorization)


@pytest.mark.parametrize("resume_ordinal", [0, 3])
def test_runtime_retry_authorization_limits_resume_ordinal(
    resume_ordinal: int,
) -> None:
    payload = _runtime_retry_payload()
    payload["resume_ordinal"] = resume_ordinal

    with pytest.raises(ValueError, match="resume_ordinal"):
        FactoryRuntimeRetryAuthorizationV1.model_validate(payload)
