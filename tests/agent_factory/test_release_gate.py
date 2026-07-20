from __future__ import annotations

from uuid import UUID

import pytest

from agenten.agent_factory.release_gate import E2EKind, E2EOutcome, E2ERunEvidence, evaluate_factory_release
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from tests.agent_factory.test_skill_evaluation_contracts import gap_payload
from tests.agent_factory.test_state_machine import accepted_evaluation, artifact, job


def evidence(number: int, kind: E2EKind, outcome: E2EOutcome) -> E2ERunEvidence:
    factory_job = job()
    return E2ERunEvidence(
        run_number=number,
        correlation_id=factory_job.correlation_id,
        kind=kind,
        outcome=outcome,
        evidence_ref=artifact(f"e2e-{number}"),
    )


def successful_e2e() -> tuple[E2ERunEvidence, ...]:
    return (
        evidence(1, E2EKind.RECOVERY, E2EOutcome.EXPECTED_FAILURE),
        evidence(2, E2EKind.NORMAL, E2EOutcome.SUCCEEDED),
        evidence(3, E2EKind.NORMAL, E2EOutcome.SUCCEEDED),
        evidence(4, E2EKind.NORMAL, E2EOutcome.SUCCEEDED),
    )


def with_evidence(
    stored: StoredSkillEvaluation,
    evaluation_evidence,
    **updates,
) -> StoredSkillEvaluation:
    return StoredSkillEvaluation(
        **{
            **stored.__dict__,
            "evidence": evaluation_evidence,
            **updates,
        }
    )


def test_release_requires_recovery_followed_by_three_successes() -> None:
    decision = evaluate_factory_release(
        job(),
        successful_e2e(),
        accepted_evaluation(),
    )

    assert decision.status == "ready"


def test_release_rejects_a_streak_without_the_required_recovery() -> None:
    decision = evaluate_factory_release(
        job(),
        tuple(evidence(number, E2EKind.NORMAL, E2EOutcome.SUCCEEDED) for number in range(1, 4)),
        accepted_evaluation(),
    )

    assert decision.status == "blocked"
    assert "recovery" in decision.reasons[0]


def test_release_fails_closed_on_missing_evaluation_before_e2e_checks() -> None:
    decision = evaluate_factory_release(job(), ())

    assert decision.status == "blocked"
    assert decision.reasons == ("missing accepted Hermes skill evaluation evidence",)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("job_id", UUID(int=990), "skill evaluation job does not match the factory job"),
        (
            "correlation_id",
            UUID(int=991),
            "skill evaluation correlation does not match the factory job",
        ),
        (
            "subject_version",
            2,
            "skill evaluation subject version does not match the factory job",
        ),
    ),
)
def test_release_requires_matching_evaluation_identity(field, value, reason) -> None:
    stored = accepted_evaluation()
    mismatched = stored.evidence.model_copy(update={field: value})

    decision = evaluate_factory_release(job(), successful_e2e(), with_evidence(stored, mismatched))

    assert decision.status == "blocked"
    assert decision.reasons == (reason,)


def test_release_rejects_a_skill_for_another_factory_capability() -> None:
    stored = accepted_evaluation()
    other_skill = stored.evidence.request.released_skill.model_copy(
        update={"capability": "other_capability"}
    )
    request = stored.evidence.request.model_copy(update={"released_skill": other_skill})
    receipt = stored.evidence.receipt.model_copy(update={"released_skill": other_skill})
    assert stored.evidence.candidate is not None
    candidate = stored.evidence.candidate.model_copy(
        update={"parent_released_skill": other_skill}
    )
    mismatched = stored.evidence.model_copy(
        update={"request": request, "receipt": receipt, "candidate": candidate}
    )

    decision = evaluate_factory_release(
        job(),
        successful_e2e(),
        with_evidence(stored, mismatched),
    )

    assert decision.status == "blocked"
    assert decision.reasons == (
        "released skill capability does not match the factory job",
    )


def test_release_requires_valid_receipt_successful_evaluator_and_all_assertions() -> None:
    stored = accepted_evaluation()
    invalid_receipt = stored.evidence.receipt.model_copy(update={"outcome": "failed"})
    receipt_decision = evaluate_factory_release(
        job(),
        successful_e2e(),
        with_evidence(
            stored,
            stored.evidence.model_copy(update={"receipt": invalid_receipt}),
        ),
    )
    failed_checks = tuple(
        check.model_copy(update={"status": "failed"})
        if check.kind == "test"
        else check
        for check in stored.evidence.checks
    )
    evaluator_decision = evaluate_factory_release(
        job(),
        successful_e2e(),
        with_evidence(
            stored,
            stored.evidence.model_copy(update={"checks": failed_checks}),
        ),
    )
    assertions_decision = evaluate_factory_release(
        job(),
        successful_e2e(),
        with_evidence(
            stored,
            stored.evidence.model_copy(update={"assertion_ids": ("schema_valid",)}),
        ),
    )

    assert receipt_decision.reasons == ("skill usage receipt is not valid for the factory job",)
    assert evaluator_decision.reasons == ("skill candidate evaluator did not succeed",)
    assert assertions_decision.reasons == (
        "skill evaluation is missing required acceptance assertions: real_case_green",
    )


def test_required_tool_gap_blocks_with_specific_reason() -> None:
    stored = accepted_evaluation()
    required_gap = ToolGapMarker.model_validate(gap_payload(severity="required"))
    blocked = with_evidence(
        stored,
        stored.evidence.model_copy(update={"tool_gaps": (required_gap,)}),
        tool_gaps=(required_gap,),
        tool_gap_refs=((required_gap.gap_id, required_gap.evidence_ref),),
    )

    decision = evaluate_factory_release(job(), successful_e2e(), blocked)

    assert decision.status == "blocked"
    assert decision.reasons == (
        "unresolved required TODO_TOOL gaps: missing-diagnostic-tool",
    )


def test_optional_tool_gap_remains_on_ready_captain_decision() -> None:
    stored = accepted_evaluation()

    decision = evaluate_factory_release(job(), successful_e2e(), stored)

    assert decision.status == "ready"
    assert decision.evaluation_id == stored.evidence.evidence_id
    assert decision.evaluation_ref == stored.evidence_ref
    assert decision.tool_gaps == stored.tool_gaps
    assert decision.tool_gaps[0].severity == "optional"
    assert decision.tool_gaps[0].status == "unresolved"
