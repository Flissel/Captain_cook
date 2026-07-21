from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.release_gate import (
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    evaluate_factory_release,
    evaluate_factory_workflow_release,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from tests.agent_factory.test_skill_evaluation_contracts import gap_payload
from tests.agent_factory.test_state_machine import accepted_evaluation, artifact, job
from tests.agent_factory.test_skill_workflow_contracts import (
    CORRELATION_ID,
    JOB_ID,
    artifact as workflow_artifact,
    execution_payload,
    invocation_payload,
)


WORKFLOW_NOW = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)


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


def workflow_job(*, mode: str) -> AgentFactoryJobV3:
    required_runs = 1 if mode == "demo" else 3
    return AgentFactoryJobV3.model_validate(
        {
            "schema": "captain.agent-factory-job.v3",
            "event_id": "00000000-0000-0000-0000-000000000311",
            "correlation_id": CORRELATION_ID,
            "occurred_at": WORKFLOW_NOW,
            "producer": "captain",
            "job_id": JOB_ID,
            "subject_version": 1,
            "input_ref": {
                "uri": f"artifact://sha256/{'a' * 64}",
                "sha256": "a" * 64,
                "media_type": "text/markdown",
            },
            "compiled_spec_ref": {
                "uri": f"artifact://sha256/{'b' * 64}",
                "sha256": "b" * 64,
                "media_type": "application/json",
            },
            "dependency_graph_ref": {
                "uri": f"artifact://sha256/{'c' * 64}",
                "sha256": "c" * 64,
                "media_type": "application/json",
            },
            "required_capability": "factory_workflow",
            "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
            "private_holdout_refs": [
                {
                    "schema_name": "captain.private-holdout-ref.v1",
                    "holdout_id": "holdout-222222222222",
                    "uri": "holdout://holdout-222222222222",
                    "sha256": "2" * 64,
                }
            ],
            "max_behavioral_iterations": 5,
            "deadline_at": WORKFLOW_NOW + timedelta(minutes=15),
            "execution_policy": {
                "schema": "captain.factory-execution-policy.v1",
                "mode": mode,
                "live_execution": True,
                "max_cost_usd": "5.00",
                "max_runtime_seconds": 900,
                "required_live_runs": required_runs,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            },
        }
    )


def workflow_run(
    number: int,
    *,
    attempt: int = 1,
) -> TeamExecutionEvidenceV1:
    payload = execution_payload(
        run_number=number,
        artifact_ref=workflow_artifact(
            f"run-{attempt}-{number}",
            hashlib.sha256(f"run-{attempt}-{number}".encode()).hexdigest(),
        ),
        evidence_refs=[
            workflow_artifact(
                f"run-evidence-{attempt}-{number}",
                hashlib.sha256(
                    f"run-evidence-{attempt}-{number}".encode()
                ).hexdigest(),
            )
        ],
        usage_receipt_refs=[
            workflow_artifact(
                f"usage-{attempt}-{number}",
                hashlib.sha256(f"usage-{attempt}-{number}".encode()).hexdigest(),
            )
        ],
    )
    payload["attempt"] = attempt
    invocation = payload["invocation"]
    assert isinstance(invocation, dict)
    invocation["attempt"] = attempt
    lease = invocation["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = attempt
    invocation_id = (
        f"00000000-0000-0000-0000-{320 + (attempt - 1) * 10 + number:012d}"
    )
    invocation["invocation_id"] = invocation_id
    invocation["idempotency_key"] = hashlib.sha256(
        f"invocation-{attempt}-{number}".encode()
    ).hexdigest()
    payload["invocation_id"] = invocation_id
    outcome = payload["execution_outcome"]
    assert isinstance(outcome, dict)
    outcome["command_id"] = f"00000000-0000-0000-0000-{330 + number:012d}"
    outcome["result_id"] = f"00000000-0000-0000-0000-{340 + number:012d}"
    return TeamExecutionEvidenceV1.model_validate(payload)


def workflow_evaluation(
    runs: tuple[TeamExecutionEvidenceV1, ...],
    *,
    budget: FactoryBudgetProjection | None = None,
):
    payload = invocation_payload("evaluate_team")
    payload["attempt"] = runs[0].attempt
    lease = payload["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = runs[0].attempt
    invocation = FactorySkillInvocationV1.model_validate(payload)
    projection = budget or workflow_budget()
    return TeamEvaluationService(
        clock=lambda: WORKFLOW_NOW + timedelta(minutes=2)
    ).evaluate(
        invocation,
        runs[0].candidate_ref,
        runs,
        budget_projection=projection,
    )


def workflow_budget(
    consumed_usd: str = "0.75",
) -> FactoryBudgetProjection:
    consumed = Decimal(consumed_usd)
    return FactoryBudgetProjection(
        job_id=UUID(JOB_ID),
        limit_usd="5.00",
        consumed_usd=consumed,
        reserved_usd="0",
        remaining_usd=Decimal("5.00") - consumed,
    )


def workflow_receipts(
    runs: tuple[TeamExecutionEvidenceV1, ...],
) -> tuple[FactoryUsageReceiptV1, ...]:
    cost = "0.75" if len(runs) == 1 else "0.25"
    return tuple(
        FactoryUsageReceiptV1(
            schema_name="captain.factory-usage-receipt.v1",
            receipt_id=UUID(
                f"00000000-0000-0000-0000-"
                f"{410 + (run.attempt - 1) * 10 + run.run_number:012d}"
            ),
            reservation_id=UUID(
                f"00000000-0000-0000-0000-"
                f"{510 + (run.attempt - 1) * 10 + run.run_number:012d}"
            ),
            job_id=run.job_id,
            correlation_id=run.correlation_id,
            attempt=run.attempt,
            provider="approved-provider",
            model="approved-model-id",
            input_units=100,
            output_units=20,
            cost_usd=cost,
            started_at=WORKFLOW_NOW,
            ended_at=WORKFLOW_NOW + timedelta(seconds=1),
            evidence_ref=run.usage_receipt_refs[0],
        )
        for run in runs
    )


def test_workflow_release_keeps_demo_ready_distinct_from_ready() -> None:
    demo_runs = (workflow_run(1),)
    release_runs = tuple(workflow_run(number) for number in range(1, 4))

    demo = evaluate_factory_workflow_release(
        workflow_job(mode="demo"),
        demo_runs,
        workflow_evaluation(demo_runs),
        budget_projection=workflow_budget(),
        usage_receipts=workflow_receipts(demo_runs),
    )
    release = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        release_runs,
        workflow_evaluation(release_runs),
        budget_projection=workflow_budget(),
        usage_receipts=workflow_receipts(release_runs),
    )

    assert demo.status == "demo_ready"
    assert release.status == "ready"


def test_workflow_release_requires_exact_live_run_count_and_usage_receipts() -> None:
    runs = (workflow_run(1), workflow_run(2))
    decision = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        runs,
        workflow_evaluation(runs),
        budget_projection=workflow_budget(),
        usage_receipts=workflow_receipts(runs),
    )

    assert decision.status == "blocked"
    assert "three" in decision.reasons[0]


def test_workflow_release_requires_a_gateway_budget_projection() -> None:
    runs = (workflow_run(1),)

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="demo"),
        runs,
        workflow_evaluation(runs),
    )

    assert decision.status == "blocked"
    assert decision.reasons == (
        "missing Gateway workflow budget projection",
    )


def test_workflow_release_requires_receipts_to_cover_the_budget_projection() -> None:
    runs = (workflow_run(1),)

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="demo"),
        runs,
        workflow_evaluation(runs),
        budget_projection=workflow_budget(),
        usage_receipts=(),
    )

    assert decision.status == "blocked"
    assert decision.reasons == (
        "workflow usage receipts do not cover the Gateway budget projection",
    )


def test_workflow_release_rejects_receipt_reference_reused_between_runs() -> None:
    runs = tuple(workflow_run(number) for number in range(1, 4))
    shared_ref = runs[0].usage_receipt_refs[0]
    reused = tuple(
        run.model_copy(update={"usage_receipt_refs": (shared_ref,)})
        for run in runs
    )

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        reused,
        workflow_evaluation(reused),
        budget_projection=workflow_budget(),
        usage_receipts=workflow_receipts((runs[0],)),
    )

    assert decision.status == "blocked"
    assert decision.reasons == (
        "workflow usage receipts must exactly and uniquely cover every run",
    )


def test_workflow_release_rejects_extra_gateway_receipt_not_bound_to_a_run() -> None:
    runs = tuple(workflow_run(number) for number in range(1, 4))
    receipts = workflow_receipts(runs)
    extra = receipts[0].model_copy(
        update={
            "receipt_id": UUID("00000000-0000-0000-0000-000000000499"),
            "reservation_id": UUID("00000000-0000-0000-0000-000000000498"),
            "cost_usd": Decimal("0.00"),
            "evidence_ref": ArtifactRef.model_validate(
                workflow_artifact("extra-usage", "f" * 64)
            ),
        }
    )

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        runs,
        workflow_evaluation(runs),
        budget_projection=workflow_budget(),
        usage_receipts=(*receipts, extra),
    )

    assert decision.status == "blocked"
    assert decision.reasons == (
        "workflow usage receipts must exactly and uniquely cover every run",
    )


def test_workflow_release_accepts_disjoint_exact_receipt_union() -> None:
    runs = tuple(workflow_run(number) for number in range(1, 4))

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        runs,
        workflow_evaluation(runs),
        budget_projection=workflow_budget(),
        usage_receipts=workflow_receipts(runs),
    )

    assert decision.status == "ready"


def test_workflow_release_scopes_exact_receipt_coverage_to_current_attempt() -> None:
    historical_run = workflow_run(1, attempt=1)
    current_runs = tuple(
        workflow_run(number, attempt=2) for number in range(1, 4)
    )
    historical_receipts = workflow_receipts((historical_run,))
    current_receipts = workflow_receipts(current_runs)
    total_budget = workflow_budget("1.50")

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        current_runs,
        workflow_evaluation(current_runs, budget=total_budget),
        budget_projection=total_budget,
        usage_receipts=(*historical_receipts, *current_receipts),
    )

    assert decision.status == "ready"
    assert {
        receipt.evidence_ref for receipt in historical_receipts
    }.isdisjoint(
        reference
        for run in current_runs
        for reference in run.usage_receipt_refs
    )


def test_workflow_release_rejects_changed_candidate_binding() -> None:
    runs = tuple(workflow_run(number) for number in range(1, 4))
    changed = runs[2].model_copy(
        update={
            "candidate_ref": ArtifactRef.model_validate(
                workflow_artifact("changed-candidate", "f" * 64)
            )
        }
    )

    decision = evaluate_factory_workflow_release(
        workflow_job(mode="release"),
        (*runs[:2], changed),
        workflow_evaluation(runs),
        budget_projection=workflow_budget(),
        usage_receipts=workflow_receipts(runs),
    )

    assert decision.status == "blocked"
    assert "candidate" in decision.reasons[0]
