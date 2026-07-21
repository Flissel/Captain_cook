from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.outcome_contracts import (
    CapabilityAssertionResult,
    CapabilityPackageManifestV1,
    CapabilityReleaseEvidenceV1,
    FactoryTerminalState,
)
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.release_gate import (
    CapabilityValidationFailure,
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    FactoryPolicyFinding,
    FactoryTerminalReasonCode,
    derive_terminal_decision,
    evaluate_factory_release,
)
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_factory.state_machine import FactoryActionKind, FactoryProjection, apply_block, next_action
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_evaluation_contracts import gap_payload
from tests.agent_factory.test_state_machine import NOW, accepted_evaluation, artifact, block, job, v2_job


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


def accepted_manifest() -> CapabilityPackageManifestV1:
    payload = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "contracts" / "capability_package_manifest.v1.json").read_text(
            encoding="utf-8"
        )
    )
    factory_job = v2_job()
    assertion_refs = tuple(
        ArtifactRef.model_validate(artifact(f"assertion-{assertion_id}"))
        for assertion_id in factory_job.acceptance_assertion_ids
    )
    payload.update(
        {
            "factory_job_id": str(factory_job.job_id),
            "correlation_id": str(factory_job.correlation_id),
            "subject_version": factory_job.subject_version,
            "capability_id": factory_job.required_capability,
            "assertion_outcomes": [
                {
                    "assertion_id": assertion_id,
                    "status": "passed",
                    "integration_intent": "none",
                    "evidence_refs": [reference.model_dump(mode="json")],
                }
                for assertion_id, reference in zip(factory_job.acceptance_assertion_ids, assertion_refs)
            ],
            "private_holdout_receipts": [
                {
                    "holdout_id": factory_job.private_holdout_refs[0].holdout_id,
                    "assertion_id": factory_job.acceptance_assertion_ids[0],
                    "status": "passed",
                    "evidence_ref": artifact("holdout-passed"),
                }
            ],
            "recovery_receipt": {
                "recovery_id": "controlled-recovery-01",
                "assertion_id": factory_job.acceptance_assertion_ids[0],
                "status": "passed",
                "evidence_ref": artifact("recovery-passed"),
            },
        }
    )
    return CapabilityPackageManifestV1.model_validate(payload)


def capability_e2e(*, successes: int = 3) -> tuple[CapabilityReleaseEvidenceV1, ...]:
    factory_job = v2_job()
    manifest = accepted_manifest()
    assertion_results = tuple(
        CapabilityAssertionResult(
            assertion_id=assertion_id,
            status="passed",
            integration_intent="none",
            evidence_refs=(ArtifactRef.model_validate(artifact(f"run-assertion-{assertion_id}")),),
        )
        for assertion_id in factory_job.acceptance_assertion_ids
    )

    def run(number: int, *, kind: str, outcome: str) -> CapabilityReleaseEvidenceV1:
        return CapabilityReleaseEvidenceV1(
            schema_name="captain.capability-release-evidence.v1",
            run_id=f"release-run-{number}",
            run_number=number,
            factory_job_id=factory_job.job_id,
            creation_job_id=manifest.creation_job_id,
            correlation_id=factory_job.correlation_id,
            subject_version=factory_job.subject_version,
            attempt=1,
            capability_id=factory_job.required_capability,
            capability_version=manifest.capability_version,
            candidate_manifest_sha256="d" * 64,
            package_archive_sha256=manifest.source_ref.sha256,
            extracted_tree_sha256="e" * 64,
            kind=kind,
            outcome=outcome,
            producer="captain",
            assertion_results=assertion_results,
            recovery_id="controlled-recovery-01" if kind == "recovery" else None,
            recovery_assertion_id=factory_job.acceptance_assertion_ids[0] if kind == "recovery" else None,
        )

    return (
        run(1, kind="recovery", outcome="expected_failure_recovered"),
        *(run(number, kind="normal", outcome="succeeded") for number in range(2, 2 + successes)),
    )


@pytest.mark.parametrize(
    "reason_code",
    (
        FactoryTerminalReasonCode.STRUCTURAL_VIOLATION,
        FactoryTerminalReasonCode.SECURITY_VIOLATION,
        FactoryTerminalReasonCode.FOREIGN_LEASE,
        FactoryTerminalReasonCode.SCHEMA_VIOLATION,
        FactoryTerminalReasonCode.DIGEST_VIOLATION,
        FactoryTerminalReasonCode.AUTHORITY_VIOLATION,
    ),
)
def test_terminal_truth_table_rejects_structural_and_security_failures_first(reason_code) -> None:
    factory_job = v2_job(deadline_at=NOW + timedelta(minutes=1))
    projection = FactoryProjection.from_job(factory_job).model_copy(
        update={
            "attempt": 5,
            "policy_findings": (
                FactoryPolicyFinding(
                    reason_code=FactoryTerminalReasonCode.MISSING_PROVIDER,
                    evidence_ref=ArtifactRef.model_validate(artifact("provider-block")),
                ),
            ),
        }
    )
    validation = CapabilityValidationFailure(
        reason_code=reason_code,
        evidence_ref=ArtifactRef.model_validate(artifact(f"reject-{reason_code.value}")),
    )

    decision = derive_terminal_decision(
        factory_job,
        projection,
        validation,
        accepted_evaluation(),
        capability_e2e(),
        NOW + timedelta(minutes=2),
    )

    assert decision is not None
    assert decision.state is FactoryTerminalState.REJECTED
    assert decision.reasons == (reason_code.value,)
    assert decision.evidence_refs == (validation.evidence_ref,)


@pytest.mark.parametrize(
    "reason_code",
    (
        FactoryTerminalReasonCode.MISSING_CREDENTIAL_ALIAS,
        FactoryTerminalReasonCode.MISSING_PROVIDER,
        FactoryTerminalReasonCode.MISSING_API,
        FactoryTerminalReasonCode.USER_DECISION_REQUIRED,
    ),
)
def test_terminal_truth_table_blocks_named_external_prerequisites(reason_code) -> None:
    factory_job = v2_job()
    finding = FactoryPolicyFinding(
        reason_code=reason_code,
        evidence_ref=ArtifactRef.model_validate(artifact(f"block-{reason_code.value}")),
    )
    projection = FactoryProjection.from_job(factory_job).model_copy(update={"policy_findings": (finding,)})

    decision = derive_terminal_decision(
        factory_job,
        projection,
        accepted_manifest(),
        accepted_evaluation(),
        capability_e2e(),
        NOW,
    )

    assert decision is not None
    assert decision.state is FactoryTerminalState.BLOCKED
    assert decision.reasons == (reason_code.value,)
    assert decision.evidence_refs == (finding.evidence_ref,)


def test_terminal_truth_table_keeps_active_work_nonterminal_with_budget_remaining() -> None:
    factory_job = v2_job()
    projection = FactoryProjection.from_job(factory_job)

    assert derive_terminal_decision(factory_job, projection, None, None, (), NOW) is None
    assert next_action(projection, now=NOW).kind is FactoryActionKind.APPEND_FORGE_REQUESTED


@pytest.mark.parametrize(
    ("attempt", "now", "reason"),
    (
        (5, NOW, FactoryTerminalReasonCode.BEHAVIORAL_ITERATIONS_EXHAUSTED),
        (1, NOW + timedelta(minutes=15), FactoryTerminalReasonCode.DEADLINE_EXHAUSTED),
    ),
)
def test_terminal_truth_table_escalates_either_independent_budget(attempt, now, reason) -> None:
    factory_job = v2_job()
    projection = FactoryProjection.from_job(factory_job).model_copy(update={"attempt": attempt})
    if attempt == 5:
        for phase in (
            FactoryPhase.FORGE_REQUESTED,
            FactoryPhase.BLUEPRINT_CREATED,
            FactoryPhase.TOOL_CANDIDATE_TESTED,
            FactoryPhase.AGENT_CODE_CREATED,
            FactoryPhase.BUILD_FAILED,
        ):
            projection = apply_block(projection, block(phase, attempt=5))

    decision = derive_terminal_decision(factory_job, projection, None, None, (), now)

    assert decision is not None
    assert decision.state is FactoryTerminalState.ESCALATED
    assert decision.reasons == (reason.value,)
    if reason is FactoryTerminalReasonCode.BEHAVIORAL_ITERATIONS_EXHAUSTED:
        assert decision.evidence_refs


def test_required_tool_gap_blocks_but_optional_gap_with_fallback_does_not() -> None:
    factory_job = v2_job()
    projection = FactoryProjection.from_job(factory_job)
    optional_decision = derive_terminal_decision(
        factory_job,
        projection,
        accepted_manifest(),
        accepted_evaluation(),
        capability_e2e(),
        NOW,
    )
    stored = accepted_evaluation()
    required_gap = ToolGapMarker.model_validate(gap_payload(severity="required"))
    required = with_evidence(
        stored,
        stored.evidence.model_copy(update={"tool_gaps": (required_gap,)}),
        tool_gaps=(required_gap,),
        tool_gap_refs=((required_gap.gap_id, required_gap.evidence_ref),),
    )
    required_decision = derive_terminal_decision(
        factory_job,
        projection,
        accepted_manifest(),
        required,
        capability_e2e(),
        NOW,
    )

    assert optional_decision is not None
    assert optional_decision.state is FactoryTerminalState.READY_TO_USE
    assert required_decision is not None
    assert required_decision.state is FactoryTerminalState.BLOCKED
    assert required_decision.reasons == (FactoryTerminalReasonCode.REQUIRED_TOOL_GAP.value,)


def test_required_tool_gap_in_validated_manifest_also_blocks() -> None:
    factory_job = v2_job()
    required_gap = ToolGapMarker.model_validate(gap_payload(severity="required"))
    manifest = accepted_manifest().model_copy(update={"tool_gaps": (required_gap,)})

    decision = derive_terminal_decision(
        factory_job,
        FactoryProjection.from_job(factory_job),
        manifest,
        accepted_evaluation(),
        capability_e2e(),
        NOW,
    )

    assert decision is not None
    assert decision.state is FactoryTerminalState.BLOCKED
    assert decision.reasons == (FactoryTerminalReasonCode.REQUIRED_TOOL_GAP.value,)
    assert required_gap.evidence_ref in decision.evidence_refs


def test_assertion_failure_precedes_e2e_and_blocks_with_captain_evidence() -> None:
    factory_job = v2_job()
    manifest = accepted_manifest()
    failed = manifest.assertion_outcomes[0].model_copy(update={"status": "failed"})
    manifest = manifest.model_copy(update={"assertion_outcomes": (failed, *manifest.assertion_outcomes[1:])})

    decision = derive_terminal_decision(
        factory_job,
        FactoryProjection.from_job(factory_job),
        manifest,
        accepted_evaluation(),
        capability_e2e(successes=2),
        NOW,
    )

    assert decision is not None
    assert decision.state is FactoryTerminalState.BLOCKED
    assert decision.reasons == (FactoryTerminalReasonCode.ASSERTION_BLOCKED.value,)
    assert decision.evidence_refs == failed.evidence_refs


def test_recovery_plus_two_successes_blocks_but_exactly_three_distinct_trailing_successes_are_ready() -> None:
    factory_job = v2_job()
    projection = FactoryProjection.from_job(factory_job)
    blocked = derive_terminal_decision(
        factory_job,
        projection,
        accepted_manifest(),
        accepted_evaluation(),
        capability_e2e(successes=2),
        NOW,
    )
    ready = derive_terminal_decision(
        factory_job,
        projection,
        accepted_manifest(),
        accepted_evaluation(),
        capability_e2e(successes=3),
        NOW,
    )

    assert blocked is not None
    assert blocked.state is FactoryTerminalState.BLOCKED
    assert blocked.reasons == (FactoryTerminalReasonCode.E2E_BLOCKED.value,)
    assert ready is not None
    assert ready.state is FactoryTerminalState.READY_TO_USE
    assert ready.reasons == (FactoryTerminalReasonCode.READY_TO_USE.value,)
    assert ready.evidence_refs == (
        *accepted_manifest().release_evidence_refs,
        accepted_evaluation().evidence_ref,
    )


def test_e2e_evidence_must_bind_the_current_behavioral_attempt() -> None:
    factory_job = v2_job()
    mismatched_attempt = tuple(
        record.model_copy(update={"attempt": 2}) for record in capability_e2e()
    )

    decision = derive_terminal_decision(
        factory_job,
        FactoryProjection.from_job(factory_job),
        accepted_manifest(),
        accepted_evaluation(),
        mismatched_attempt,
        NOW,
    )

    assert decision is not None
    assert decision.state is FactoryTerminalState.BLOCKED
    assert decision.reasons == (FactoryTerminalReasonCode.E2E_BLOCKED.value,)


def test_fifth_successful_attempt_can_reach_ready_to_use() -> None:
    factory_job = v2_job()
    projection = FactoryProjection.from_job(factory_job).model_copy(update={"attempt": 5})
    runs = tuple(record.model_copy(update={"attempt": 5}) for record in capability_e2e())

    decision = derive_terminal_decision(
        factory_job,
        projection.model_copy(update={"phase": FactoryPhase.QUALITY_REVIEWED}),
        accepted_manifest(),
        accepted_evaluation(),
        runs,
        NOW,
    )

    assert decision is not None
    assert decision.state is FactoryTerminalState.READY_TO_USE


def test_job_projection_binding_is_checked_before_existing_terminal_replay() -> None:
    original_job = v2_job()
    existing = derive_terminal_decision(
        original_job,
        FactoryProjection.from_job(original_job),
        accepted_manifest(),
        accepted_evaluation(),
        capability_e2e(),
        NOW,
    )
    assert existing is not None
    projection = FactoryProjection.from_job(original_job).model_copy(
        update={"terminal_decision": existing}
    )
    foreign_job = original_job.model_copy(update={"job_id": UUID(int=998)})

    decision = derive_terminal_decision(
        foreign_job,
        projection,
        accepted_manifest(),
        accepted_evaluation(),
        capability_e2e(),
        NOW + timedelta(minutes=1),
    )

    assert decision is not None
    assert decision.job_id == foreign_job.job_id
    assert decision.state is FactoryTerminalState.REJECTED
    assert decision.reasons == (FactoryTerminalReasonCode.AUTHORITY_VIOLATION.value,)
