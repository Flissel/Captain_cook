from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluationResult,
    FactoryEvaluationCheck,
)
from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.skill_sequence import (
    build_factory_improvement_authorization,
)
from agenten.agent_factory.service import FactoryCoordinator, InMemoryFactoryRepository
from agenten.agent_factory.skill_workflow_contracts import TeamExecutionEvidenceV1
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_factory.team_execution import (
    FactoryHoldoutAssertionDecisionV1,
    FactoryHoldoutEvaluationReceiptV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_improvement_authority import (
    CaptainFactoryImprovementAuthorizationStore,
    CaptainTechnicalImprovementIssuer,
    CaptainTechnicalFailureEvaluator,
    FilesystemFactoryImprovementAuthority,
)
from tests.agent_factory.test_skill_workflow_contracts import execution_payload
from tests.agent_factory.test_state_machine import job_v3


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)


def _job():
    return job_v3(mode="demo").model_copy(
        update={"deadline_at": NOW + timedelta(hours=2)}
    )


def _ref(namespace: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://factory/{namespace}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _source_block(phase: FactoryPhase, evidence_ref: ArtifactRef) -> FactoryEvidenceBlock:
    job = _job()
    role = "tool_integrator" if phase is FactoryPhase.BUILD_FAILED else "real_case_tester"
    return FactoryEvidenceBlock.model_validate(
        {
            "schema": "captain.agent-factory-block.v1",
            "event_id": "00000000-0000-0000-0000-000000000451",
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "causation_id": str(job.event_id),
            "occurred_at": NOW - timedelta(minutes=2),
            "producer": "hermes",
            "subject_version": job.subject_version,
            "attempt": 1,
            "phase": phase.value,
            "role": role,
            "status": (
                FactoryBlockStatus.FAILED.value
                if phase is FactoryPhase.BUILD_FAILED
                else FactoryBlockStatus.SUCCEEDED.value
            ),
            "artifact_refs": [],
            "evidence_refs": [evidence_ref.model_dump(mode="json")],
            "assertion_ids": [],
            "lease_id": "factory-technical-lease",
        }
    )


def _request(evaluation) -> FactoryEvidenceBlock:
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id="00000000-0000-0000-0000-000000000452",
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


def test_captain_converts_failed_build_preflight_to_fail_closed_assertions() -> None:
    job = _job()
    evidence_ref = _ref("candidate-evaluation", "1" * 64)
    source = _source_block(FactoryPhase.BUILD_FAILED, evidence_ref)
    candidate_ref = _ref("candidate", "2" * 64)
    result = FactoryCandidateEvaluationResult(
        status="failed",
        trace_id="trace-build-failed",
        assertion_ids=(),
        tool_names=(),
        checks=(
            FactoryEvaluationCheck(
                name="build", status="passed", detail="command exited 0"
            ),
            FactoryEvaluationCheck(
                name="real_case",
                status="failed",
                detail=(
                    "candidate command failed: rejected: Expecting value: "
                    "line 1 column 1 (char 0)"
                ),
            ),
        ),
    )

    evaluation = CaptainTechnicalFailureEvaluator().from_build_failure(
        job=job,
        source_block=source,
        candidate_ref=candidate_ref,
        result=result,
        evidence_ref=evidence_ref,
        occurred_at=NOW,
    )

    assert tuple(item.status for item in evaluation.assertion_outcomes) == (
        "failed",
        "failed",
    )
    assert evaluation.failure_class == "test_regression"
    assert evaluation.technical_diagnostic_codes == (
        "real_case_command_failed",
    )


def test_captain_converts_candidate_pytest_failure_to_retry_evidence() -> None:
    job = _job()
    evidence_ref = _ref("candidate-evaluation", "9" * 64)
    source = _source_block(FactoryPhase.BUILD_FAILED, evidence_ref)
    candidate_ref = _ref("candidate", "a" * 64)
    result = FactoryCandidateEvaluationResult(
        status="failed",
        trace_id="trace-build-failed",
        assertion_ids=(),
        tool_names=(),
        checks=(
            FactoryEvaluationCheck(
                name="source_archive", status="passed", detail="sha256 verified"
            ),
            FactoryEvaluationCheck(
                name="build",
                status="failed",
                detail="candidate command failed: pytest exited 1",
            ),
        ),
    )

    evaluation = CaptainTechnicalFailureEvaluator().from_build_failure(
        job=job,
        source_block=source,
        candidate_ref=candidate_ref,
        result=result,
        evidence_ref=evidence_ref,
        occurred_at=NOW,
    )

    assert evaluation.failure_class == "test_regression"
    assert evaluation.technical_diagnostic_codes == (
        "candidate_build_command_failed",
    )
    assert tuple(item.status for item in evaluation.assertion_outcomes) == (
        "failed",
        "failed",
    )


def test_captain_classifies_missing_trace_without_copying_raw_failure_text() -> None:
    job = _job()
    evidence_ref = _ref("candidate-evaluation", "7" * 64)
    source = _source_block(FactoryPhase.BUILD_FAILED, evidence_ref)
    candidate_ref = _ref("candidate", "8" * 64)
    result = FactoryCandidateEvaluationResult(
        status="failed",
        trace_id="trace-build-failed",
        assertion_ids=(),
        tool_names=(),
        checks=(
            FactoryEvaluationCheck(
                name="real_case",
                status="failed",
                detail="real-case result does not carry the Captain trace ID",
            ),
        ),
    )

    evaluation = CaptainTechnicalFailureEvaluator().from_build_failure(
        job=job,
        source_block=source,
        candidate_ref=candidate_ref,
        result=result,
        evidence_ref=evidence_ref,
        occurred_at=NOW,
    )

    assert evaluation.technical_diagnostic_codes == (
        "real_case_trace_id_mismatch",
    )
    assert "detail" not in evaluation.model_dump(mode="json")


@pytest.mark.parametrize(
    "source_phase",
    (FactoryPhase.REAL_CASE_EVIDENCE, FactoryPhase.REAL_CASE_REVALIDATED),
)
@pytest.mark.parametrize("execution_status", ("failed", "unresolved"))
def test_captain_retains_only_passed_technical_assertions_as_regression_guards(
    source_phase: FactoryPhase,
    execution_status: str,
) -> None:
    job = _job()
    payload = execution_payload(status=execution_status)
    outcomes = payload["execution_outcome"]
    assert isinstance(outcomes, dict)
    assertion_outcomes = outcomes["assertion_outcomes"]
    assert isinstance(assertion_outcomes, list)
    assert isinstance(assertion_outcomes[1], dict)
    assertion_outcomes[1]["status"] = "failed"
    execution = TeamExecutionEvidenceV1.model_validate(payload)
    source = _source_block(
        source_phase,
        execution.artifact_ref,
    ).model_copy(update={"artifact_refs": (execution.artifact_ref,)})

    evaluation = CaptainTechnicalFailureEvaluator().from_team_execution(
        job=job,
        source_block=source,
        candidate_ref=execution.candidate_ref,
        execution=execution,
        occurred_at=NOW,
    )

    assert evaluation.prior_green_regression_ids == ("schema_valid",)
    assert tuple(
        outcome.assertion_id
        for outcome in evaluation.assertion_outcomes
        if outcome.status == "failed"
    ) == ("real_case_green",)
    assert evaluation.source_phase is source_phase


def test_captain_classifies_public_business_and_handoff_failures() -> None:
    job = _job().model_copy(
        update={
            "acceptance_assertion_ids": (
                "business_value",
                "safe_tool_use",
                "mandatory_handoff",
            )
        }
    )
    payload = execution_payload(status="unresolved")
    invocation = payload["invocation"]
    assert isinstance(invocation, dict)
    invocation["acceptance_assertion_ids"] = list(job.acceptance_assertion_ids)
    payload["acceptance_assertion_ids"] = list(job.acceptance_assertion_ids)
    execution_outcome = payload["execution_outcome"]
    assert isinstance(execution_outcome, dict)
    execution_outcome["assertion_outcomes"] = [
        {
            "assertion_id": "business_value",
            "status": "failed",
            "evidence_refs": [_ref("business", "1" * 64).model_dump(mode="json")],
        },
        {
            "assertion_id": "safe_tool_use",
            "status": "passed",
            "evidence_refs": [_ref("safe", "2" * 64).model_dump(mode="json")],
        },
        {
            "assertion_id": "mandatory_handoff",
            "status": "failed",
            "evidence_refs": [_ref("handoff", "3" * 64).model_dump(mode="json")],
        },
    ]
    execution_outcome["status"] = "failed"
    execution = TeamExecutionEvidenceV1.model_validate(payload)
    holdout_receipt = FactoryHoldoutEvaluationReceiptV1(
        schema="captain.factory-holdout-evaluation-receipt.v1",
        holdout_ref=execution.holdout_ref,
        candidate_ref=execution.candidate_ref,
        assertion_ids=job.acceptance_assertion_ids,
        decisions=(
            FactoryHoldoutAssertionDecisionV1(
                assertion_id="business_value",
                passed=False,
                provenance_code="observed_rationale_incomplete",
            ),
            FactoryHoldoutAssertionDecisionV1(
                assertion_id="safe_tool_use",
                passed=True,
                provenance_code="captain_private_rule_pass",
            ),
            FactoryHoldoutAssertionDecisionV1(
                assertion_id="mandatory_handoff",
                passed=False,
                provenance_code="terminal_missing_or_invalid",
            ),
        ),
        evaluator_id="captain_technical_business_holdout",
        evaluator_version="1",
        evaluated_at=NOW,
    )
    source = _source_block(
        FactoryPhase.REAL_CASE_EVIDENCE,
        execution.artifact_ref,
    ).model_copy(update={"artifact_refs": (execution.artifact_ref,)})

    evaluation = CaptainTechnicalFailureEvaluator().from_team_execution(
        job=job,
        source_block=source,
        candidate_ref=execution.candidate_ref,
        execution=execution,
        holdout_receipt=holdout_receipt,
        occurred_at=NOW,
    )

    assert evaluation.technical_diagnostic_codes == (
        "business_value_failed",
        "observed_rationale_incomplete",
        "terminal_missing_or_invalid",
        "mandatory_handoff_failed",
    )


def test_captain_classifies_preflight_failure_as_candidate_contract_failure() -> None:
    job = _job()
    payload = execution_payload(status="failed")
    payload["termination_reason"] = "preflight_failed"
    outcome = payload["execution_outcome"]
    assert isinstance(outcome, dict)
    assertions = outcome["assertion_outcomes"]
    assert isinstance(assertions, list)
    for assertion in assertions:
        assert isinstance(assertion, dict)
        assertion["status"] = "failed"
    outcome["status"] = "failed"
    execution = TeamExecutionEvidenceV1.model_validate(payload)
    source = _source_block(
        FactoryPhase.REAL_CASE_EVIDENCE,
        execution.artifact_ref,
    ).model_copy(update={"artifact_refs": (execution.artifact_ref,)})

    evaluation = CaptainTechnicalFailureEvaluator().from_team_execution(
        job=job,
        source_block=source,
        candidate_ref=execution.candidate_ref,
        execution=execution,
        occurred_at=NOW,
    )

    assert evaluation.technical_diagnostic_codes == (
        "real_case_contract_failed",
    )


def test_issuer_recovers_team_execution_from_block_referenced_evidence(
    tmp_path: Path,
) -> None:
    job = _job()
    payload = execution_payload(status="unresolved")
    outcomes = payload["execution_outcome"]
    assert isinstance(outcomes, dict)
    assertion_outcomes = outcomes["assertion_outcomes"]
    assert isinstance(assertion_outcomes, list)
    assert isinstance(assertion_outcomes[1], dict)
    assertion_outcomes[1]["status"] = "failed"
    execution = TeamExecutionEvidenceV1.model_validate(payload)
    serialized_ref = ArtifactRef(
        uri=f"artifact://factory-evidence/{job.job_id}/{'6' * 64}",
        sha256="6" * 64,
        media_type="application/json",
    )
    source = _source_block(
        FactoryPhase.REAL_CASE_EVIDENCE,
        serialized_ref,
    ).model_copy(
        update={
            "artifact_refs": (execution.artifact_ref,),
            "evidence_refs": (serialized_ref, execution.artifact_ref),
        }
    )

    class Repository:
        def workflow_artifacts(self, _job_id):
            return ()

    class Evidence:
        def read_verified(self, reference, *, job_id=None):
            assert job_id == job.job_id
            if reference == serialized_ref:
                return execution.model_dump_json(by_alias=True).encode("utf-8")
            return b"{}"

    issuer = CaptainTechnicalImprovementIssuer(
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        candidates=object(),  # type: ignore[arg-type]
        evidence=Evidence(),
        authorizations=CaptainFactoryImprovementAuthorizationStore(
            tmp_path / ".captain-cook" / "improvements"
        ),
        clock=lambda: NOW,
    )

    evaluation = issuer._evaluate_execution(
        job=job,
        source_block=source,
        candidate_ref=execution.candidate_ref,
        occurred_at=NOW,
    )

    assert evaluation.prior_green_regression_ids == ("schema_valid",)


def test_persisted_authority_is_returned_only_for_exact_attempt_and_request_block(
    tmp_path: Path,
) -> None:
    job = _job()
    evidence_ref = _ref("candidate-evaluation", "3" * 64)
    source = _source_block(FactoryPhase.BUILD_FAILED, evidence_ref)
    evaluation = CaptainTechnicalFailureEvaluator().from_build_failure(
        job=job,
        source_block=source,
        candidate_ref=_ref("candidate", "4" * 64),
        result=FactoryCandidateEvaluationResult(
            status="failed",
            trace_id="trace-build-failed",
            assertion_ids=(),
            tool_names=(),
            checks=(
                FactoryEvaluationCheck(
                    name="real_case", status="failed", detail="runtime missing"
                ),
            ),
        ),
        evidence_ref=evidence_ref,
        occurred_at=NOW,
    )
    request = _request(evaluation)
    authorization = build_factory_improvement_authorization(
        request_block=request,
        failed_evaluation=evaluation,
        prior_candidate_ref=evaluation.candidate_ref,
    )
    root = tmp_path / ".captain-cook" / "improvements"
    CaptainFactoryImprovementAuthorizationStore(root).persist(authorization)
    authority = FilesystemFactoryImprovementAuthority(root)
    projection = type(
        "Projection",
        (),
        {
            "job": job,
            "attempt": 2,
            "phase": FactoryPhase.BLUEPRINT_CREATED,
            "block_ids": (source.event_id, request.event_id),
        },
    )()

    assert authority.active(
        job,
        FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=2,
        ),
        projection,
        NOW + timedelta(minutes=1),
    ) == authorization

    projection_without_request = type(
        "Projection",
        (),
        {
            "job": job,
            "attempt": 2,
            "phase": FactoryPhase.BLUEPRINT_CREATED,
            "block_ids": (source.event_id,),
        },
    )()
    with pytest.raises(ValueError, match="request block"):
        authority.active(
            job,
            FactoryAction(
                kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
                attempt=2,
            ),
            projection_without_request,
            NOW + timedelta(minutes=1),
        )


def test_issuer_appends_attempt_two_request_from_current_build_failure(
    tmp_path: Path,
) -> None:
    job = _job()
    candidate_ref = _ref("candidate", "7" * 64)
    evidence_ref = ArtifactRef(
        uri=f"artifact://factory-evidence/{job.job_id}/{'8' * 64}",
        sha256="8" * 64,
        media_type="application/json",
    )
    failed = FactoryCandidateEvaluationResult(
        status="failed",
        trace_id="trace-build-failed",
        assertion_ids=(),
        tool_names=(),
        checks=(
            FactoryEvaluationCheck(
                name="real_case", status="failed", detail="runtime missing"
            ),
        ),
    )

    class Evidence:
        def read_verified(self, reference, *, job_id=None):
            assert reference == evidence_ref
            assert job_id == job.job_id
            return failed.model_dump_json().encode("utf-8")

    class Candidates:
        def current_candidate_ref(self, selected_job, attempt):
            assert selected_job == job
            assert attempt == 1
            return candidate_ref

    repository = InMemoryFactoryRepository()
    coordinator = FactoryCoordinator(repository)
    coordinator.register(job)

    def lifecycle_block(
        phase: FactoryPhase,
        *,
        status: FactoryBlockStatus = FactoryBlockStatus.SUCCEEDED,
        event_suffix: int,
        evidence_refs: tuple[ArtifactRef, ...] = (),
    ) -> FactoryEvidenceBlock:
        roles = {
            FactoryPhase.BLUEPRINT_CREATED: "agent_architect",
            FactoryPhase.TOOL_CANDIDATE_TESTED: "tool_integrator",
            FactoryPhase.AGENT_CODE_CREATED: "tool_integrator",
            FactoryPhase.BUILD_FAILED: "tool_integrator",
        }
        role = roles.get(phase)
        return FactoryEvidenceBlock.model_validate(
            {
                "schema": "captain.agent-factory-block.v1",
                "event_id": f"00000000-0000-0000-0000-{event_suffix:012d}",
                "job_id": str(job.job_id),
                "correlation_id": str(job.correlation_id),
                "causation_id": str(job.event_id),
                "occurred_at": NOW - timedelta(minutes=10 - event_suffix),
                "producer": "hermes" if role else "captain",
                "subject_version": job.subject_version,
                "attempt": 1,
                "phase": phase.value,
                "role": role,
                "status": status.value,
                "artifact_refs": [],
                "evidence_refs": [
                    item.model_dump(mode="json") for item in evidence_refs
                ]
                or (
                    []
                    if role is None
                    else [_ref(f"phase-{event_suffix}", f"{event_suffix}" * 64).model_dump(mode="json")]
                ),
                "assertion_ids": [],
                "lease_id": f"lease-{event_suffix}" if role else None,
            }
        )

    for item in (
        lifecycle_block(FactoryPhase.FORGE_REQUESTED, event_suffix=1),
        lifecycle_block(FactoryPhase.BLUEPRINT_CREATED, event_suffix=2),
        lifecycle_block(FactoryPhase.TOOL_CANDIDATE_TESTED, event_suffix=3),
        lifecycle_block(FactoryPhase.AGENT_CODE_CREATED, event_suffix=4),
        lifecycle_block(
            FactoryPhase.BUILD_FAILED,
            status=FactoryBlockStatus.FAILED,
            event_suffix=5,
            evidence_refs=(evidence_ref,),
        ),
    ):
        coordinator.record(item)

    issuer = CaptainTechnicalImprovementIssuer(
        repository=repository,
        coordinator=coordinator,
        candidates=Candidates(),
        evidence=Evidence(),
        authorizations=CaptainFactoryImprovementAuthorizationStore(
            tmp_path / ".captain-cook" / "improvements"
        ),
        clock=lambda: NOW,
    )
    authorization = issuer.issue(job.job_id)
    replay = issuer.issue(job.job_id)

    projection = coordinator.projection(job.job_id)
    assert projection.phase is FactoryPhase.IMPROVEMENT_REQUESTED
    assert projection.attempt == 2
    assert authorization.request_block.event_id in projection.block_ids
    assert replay == authorization
    assert len(
        tuple(
            block
            for block in repository.blocks(job.job_id)
            if block.phase is FactoryPhase.IMPROVEMENT_REQUESTED
        )
    ) == 1
