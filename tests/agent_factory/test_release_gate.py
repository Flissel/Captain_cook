from __future__ import annotations

from agenten.agent_factory.release_gate import E2EKind, E2EOutcome, E2ERunEvidence, evaluate_factory_release
from tests.agent_factory.test_state_machine import artifact, job


def evidence(number: int, kind: E2EKind, outcome: E2EOutcome) -> E2ERunEvidence:
    factory_job = job()
    return E2ERunEvidence(
        run_number=number,
        correlation_id=factory_job.correlation_id,
        kind=kind,
        outcome=outcome,
        evidence_ref=artifact(f"e2e-{number}"),
    )


def test_release_requires_recovery_followed_by_three_successes() -> None:
    decision = evaluate_factory_release(
        job(),
        (
            evidence(1, E2EKind.RECOVERY, E2EOutcome.EXPECTED_FAILURE),
            evidence(2, E2EKind.NORMAL, E2EOutcome.SUCCEEDED),
            evidence(3, E2EKind.NORMAL, E2EOutcome.SUCCEEDED),
            evidence(4, E2EKind.NORMAL, E2EOutcome.SUCCEEDED),
        ),
    )

    assert decision.status == "ready"


def test_release_rejects_a_streak_without_the_required_recovery() -> None:
    decision = evaluate_factory_release(
        job(),
        tuple(evidence(number, E2EKind.NORMAL, E2EOutcome.SUCCEEDED) for number in range(1, 4)),
    )

    assert decision.status == "blocked"
    assert "recovery" in decision.reasons[0]
