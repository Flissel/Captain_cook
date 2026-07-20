from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from agenten.agent_factory.contracts import FactoryLease
from agenten.agent_factory.skill_evaluation import (
    HermesSkillCandidate,
    HermesSkillEvaluationEvidence,
    HermesSkillEvaluationRequest,
    HermesSkillUsageReceipt,
    ReleasedHermesSkill,
    ToolGapMarker,
    required_tool_gaps,
)


NOW = datetime(2026, 7, 20, 10, tzinfo=timezone.utc)
JOB_ID = "00000000-0000-0000-0000-000000000101"
CORRELATION_ID = "00000000-0000-0000-0000-000000000102"
REQUEST_ID = "00000000-0000-0000-0000-000000000103"


def artifact(name: str, digest: str = "a" * 64) -> dict[str, str]:
    return {
        "uri": f"artifact://skill-evaluation/{name}",
        "sha256": digest,
        "media_type": "application/json",
    }


def released_skill_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.released-hermes-skill.v1",
        "skill_id": "factory_skill_evaluator",
        "version": 1,
        "capability": "factory_skill_evaluation",
        "content_ref": artifact("released-skill"),
        "content_sha256": "a" * 64,
        "status": "released",
        "released_at": NOW,
        "producer": "captain",
    }
    payload.update(overrides)
    return payload


def lease_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.factory-lease.v1",
        "lease_id": "factory-lease-skill-evaluation",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "role": "tool_integrator",
        "capability_profile": "factory-tool-integrator",
        "capabilities": ["codex.run", "python.compileall"],
        "workspace_ref": "workspace://factory/skill-evaluation",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    payload.update(overrides)
    return payload


def request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.hermes-skill-evaluation-request.v1",
        "request_id": REQUEST_ID,
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_id": "support_triage",
        "subject_version": 1,
        "occurred_at": NOW,
        "producer": "captain",
        "lease": lease_payload(),
        "released_skill": released_skill_payload(),
        "candidate_source_ref": artifact("candidate-source"),
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
        "max_iterations": 3,
    }
    payload.update(overrides)
    return payload


def receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "hermes.skill-usage-receipt.v1",
        "receipt_id": "00000000-0000-0000-0000-000000000104",
        "request_id": REQUEST_ID,
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "lease_id": "factory-lease-skill-evaluation",
        "occurred_at": NOW + timedelta(minutes=1),
        "producer": "hermes",
        "released_skill": released_skill_payload(),
        "used_skill_id": "factory_skill_evaluator",
        "used_skill_version": 1,
        "used_skill_sha256": "a" * 64,
        "commands": [
            {"command_id": "python.compileall", "max_seconds": 60},
            {"command_id": "captain.real_case", "max_seconds": 120},
        ],
        "evidence_refs": [artifact("build-evidence"), artifact("test-evidence")],
        "assertion_ids": ["schema_valid", "real_case_green"],
        "outcome": "passed",
    }
    payload.update(overrides)
    return payload


def candidate_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "hermes.skill-candidate.v1",
        "candidate_id": "factory_skill_evaluator_candidate",
        "request_id": REQUEST_ID,
        "created_at": NOW + timedelta(minutes=2),
        "producer": "hermes",
        "content_ref": artifact("candidate-skill", "b" * 64),
        "content_sha256": "b" * 64,
        "parent_released_skill": released_skill_payload(),
        "creation_reason": "Repair bounded evaluation diagnostics.",
        "status": "private_candidate",
    }
    payload.update(overrides)
    return payload


def gap_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "TODO_TOOL.v1",
        "gap_id": "missing-diagnostic-tool",
        "severity": "required",
        "input_contract_ref": artifact("tool-input"),
        "output_contract_ref": artifact("tool-output"),
        "least_privilege_capability": "diagnostics.read",
        "implementation_options": [
            {
                "option_id": "captain-diagnostic-adapter",
                "description": "Expose a read-only diagnostic adapter.",
                "acceptance_assertion_id": "diagnostic_available",
            }
        ],
        "acceptance_assertion_ids": ["diagnostic_available"],
        "evidence_ref": artifact("tool-gap-evidence"),
        "status": "unresolved",
    }
    payload.update(overrides)
    return payload


def evidence_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "hermes.skill-evaluation-evidence.v1",
        "evidence_id": "00000000-0000-0000-0000-000000000105",
        "request_id": REQUEST_ID,
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_id": "support_triage",
        "subject_version": 1,
        "occurred_at": NOW + timedelta(minutes=4),
        "producer": "hermes",
        "request": request_payload(),
        "receipt": receipt_payload(),
        "candidate": candidate_payload(),
        "tool_gaps": [gap_payload(severity="optional")],
        "checks": [
            {
                "check_id": "build",
                "kind": "build",
                "command": {"command_id": "python.compileall", "max_seconds": 60},
                "status": "passed",
                "occurred_at": NOW + timedelta(minutes=2),
                "evidence_ref": artifact("build-check"),
                "assertion_ids": ["schema_valid"],
            },
            {
                "check_id": "real-case",
                "kind": "test",
                "command": {"command_id": "captain.real_case", "max_seconds": 120},
                "status": "passed",
                "occurred_at": NOW + timedelta(minutes=3),
                "evidence_ref": artifact("test-check"),
                "assertion_ids": ["real_case_green"],
            },
        ],
        "assertion_ids": ["schema_valid", "real_case_green"],
        "outcome": "passed",
    }
    payload.update(overrides)
    return payload


def test_released_skill_and_evaluation_request_are_frozen_and_strict() -> None:
    skill = ReleasedHermesSkill.model_validate(released_skill_payload())
    request = HermesSkillEvaluationRequest.model_validate(request_payload())

    assert skill.status == "released"
    assert request.lease == FactoryLease.model_validate(lease_payload())
    assert request.released_skill == skill
    with pytest.raises(ValidationError):
        ReleasedHermesSkill.model_validate(released_skill_payload(unexpected=True))
    with pytest.raises(ValidationError):
        request.max_iterations = 1  # type: ignore[misc]


def test_request_rejects_non_utc_duplicate_assertions_unreleased_skills_and_mismatched_lease() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        HermesSkillEvaluationRequest.model_validate(request_payload(occurred_at=datetime(2026, 7, 20, 10)))
    with pytest.raises(ValidationError, match="duplicates"):
        HermesSkillEvaluationRequest.model_validate(
            request_payload(acceptance_assertion_ids=["schema_valid", "schema_valid"])
        )
    with pytest.raises(ValidationError):
        HermesSkillEvaluationRequest.model_validate(
            request_payload(released_skill=released_skill_payload(status="private_candidate"))
        )
    with pytest.raises(ValidationError, match="job"):
        HermesSkillEvaluationRequest.model_validate(
            request_payload(lease=lease_payload(job_id="00000000-0000-0000-0000-000000000199"))
        )
    with pytest.raises(ValidationError, match="correlation"):
        HermesSkillEvaluationRequest.model_validate(
            request_payload(lease=lease_payload(correlation_id="00000000-0000-0000-0000-000000000199"))
        )


def test_request_must_be_issued_within_its_factory_lease() -> None:
    with pytest.raises(ValidationError, match="active"):
        HermesSkillEvaluationRequest.model_validate(
            request_payload(occurred_at=NOW - timedelta(seconds=1))
        )
    with pytest.raises(ValidationError, match="active"):
        HermesSkillEvaluationRequest.model_validate(
            request_payload(occurred_at=NOW + timedelta(minutes=10))
        )


def test_released_and_candidate_skill_content_digests_are_immutable() -> None:
    with pytest.raises(ValidationError, match="digest"):
        ReleasedHermesSkill.model_validate(released_skill_payload(content_sha256="b" * 64))
    with pytest.raises(ValidationError, match="digest"):
        HermesSkillCandidate.model_validate(candidate_payload(content_sha256="c" * 64))


def test_usage_receipt_requires_bounded_unique_commands_and_evidence() -> None:
    receipt = HermesSkillUsageReceipt.model_validate(receipt_payload())

    assert receipt.used_skill_sha256 == receipt.released_skill.content_sha256
    with pytest.raises(ValidationError, match="max_seconds"):
        HermesSkillUsageReceipt.model_validate(
            receipt_payload(commands=[{"command_id": "python.compileall"}])
        )
    with pytest.raises(ValidationError, match="duplicates"):
        HermesSkillUsageReceipt.model_validate(
            receipt_payload(evidence_refs=[artifact("same"), artifact("same")])
        )
    with pytest.raises(ValidationError, match="digest"):
        HermesSkillUsageReceipt.model_validate(receipt_payload(used_skill_sha256="c" * 64))
    with pytest.raises(ValidationError):
        HermesSkillUsageReceipt.model_validate(
            receipt_payload(
                commands=[
                    {"command_id": f"command-{index}", "max_seconds": 60}
                    for index in range(6)
                ]
            )
        )
    with pytest.raises(ValidationError):
        HermesSkillUsageReceipt.model_validate(
            receipt_payload(evidence_refs=[artifact(f"evidence-{index}") for index in range(11)])
        )


def test_tool_gap_is_typed_bounded_and_rejects_empty_options() -> None:
    marker = ToolGapMarker.model_validate(gap_payload())

    assert marker.schema_name == "TODO_TOOL.v1"
    with pytest.raises(ValidationError, match="description"):
        ToolGapMarker.model_validate(
            gap_payload(
                implementation_options=[
                    {
                        "option_id": "empty-option",
                        "description": "",
                        "acceptance_assertion_id": "diagnostic_available",
                    }
                ]
            )
        )
    with pytest.raises(ValidationError):
        ToolGapMarker.model_validate(
            gap_payload(
                implementation_options=[
                    {
                        "option_id": f"option-{index}",
                        "description": "A valid bounded option.",
                        "acceptance_assertion_id": "diagnostic_available",
                    }
                    for index in range(4)
                ]
            )
        )
    with pytest.raises(ValidationError, match="duplicates"):
        ToolGapMarker.model_validate(
            gap_payload(acceptance_assertion_ids=["diagnostic_available", "diagnostic_available"])
        )


def test_evidence_links_one_request_receipt_candidate_and_checks() -> None:
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())

    assert evidence.job_id == UUID(JOB_ID)
    assert evidence.receipt.request_id == evidence.request_id
    with pytest.raises(ValidationError, match="request"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(receipt=receipt_payload(request_id="00000000-0000-0000-0000-000000000199"))
        )
    with pytest.raises(ValidationError, match="assertion"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(assertion_ids=["schema_valid"])
        )


def test_evidence_rejects_a_receipt_not_authorized_by_the_captain_request() -> None:
    with pytest.raises(ValidationError, match="lease"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(receipt=receipt_payload(lease_id="unrelated-factory-lease"))
        )

    other_skill = released_skill_payload(
        skill_id="other_factory_skill",
        version=2,
        content_ref=artifact("other-released-skill", "d" * 64),
        content_sha256="d" * 64,
    )
    with pytest.raises(ValidationError, match="released skill"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(
                receipt=receipt_payload(
                    released_skill=other_skill,
                    used_skill_id="other_factory_skill",
                    used_skill_version=2,
                    used_skill_sha256="d" * 64,
                )
            )
        )
    with pytest.raises(ValidationError, match="active"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(receipt=receipt_payload(occurred_at=NOW + timedelta(minutes=10)))
        )


def test_evidence_requires_receipt_before_candidate_creation_and_checks() -> None:
    with pytest.raises(ValidationError, match="candidate"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(candidate=candidate_payload(created_at=NOW))
        )
    with pytest.raises(ValidationError, match="check"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(
                checks=[
                    {
                        "check_id": "build",
                        "kind": "build",
                        "command": {"command_id": "python.compileall", "max_seconds": 60},
                        "status": "passed",
                        "occurred_at": NOW,
                        "evidence_ref": artifact("build-check"),
                        "assertion_ids": ["schema_valid"],
                    }
                ]
            )
        )


def test_evidence_rejects_unknown_or_incomplete_captain_assertions() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(receipt=receipt_payload(assertion_ids=["unknown_assertion"]))
        )
    with pytest.raises(ValidationError, match="unknown"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(assertion_ids=["schema_valid", "real_case_green", "unknown_assertion"])
        )
    with pytest.raises(ValidationError, match="unknown"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(
                assertion_ids=["schema_valid", "real_case_green", "unknown_assertion"],
                checks=[
                    {
                        "check_id": "unknown-check",
                        "kind": "test",
                        "command": {"command_id": "captain.real_case", "max_seconds": 120},
                        "status": "passed",
                        "occurred_at": NOW + timedelta(minutes=2),
                        "evidence_ref": artifact("unknown-check"),
                        "assertion_ids": ["unknown_assertion"],
                    }
                ],
            )
        )
    with pytest.raises(ValidationError, match="missing"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(
                receipt=receipt_payload(assertion_ids=["schema_valid"]),
                checks=[
                    {
                        "check_id": "build",
                        "kind": "build",
                        "command": {"command_id": "python.compileall", "max_seconds": 60},
                        "status": "passed",
                        "occurred_at": NOW + timedelta(minutes=2),
                        "evidence_ref": artifact("build-check"),
                        "assertion_ids": ["schema_valid"],
                    }
                ],
                assertion_ids=["schema_valid"],
            )
        )
    with pytest.raises(ValidationError, match="successful"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(
                checks=[
                    {
                        "check_id": "build",
                        "kind": "build",
                        "command": {"command_id": "python.compileall", "max_seconds": 60},
                        "status": "passed",
                        "occurred_at": NOW + timedelta(minutes=2),
                        "evidence_ref": artifact("build-check"),
                        "assertion_ids": ["schema_valid"],
                    },
                    {
                        "check_id": "real-case",
                        "kind": "test",
                        "command": {"command_id": "captain.real_case", "max_seconds": 120},
                        "status": "failed",
                        "occurred_at": NOW + timedelta(minutes=3),
                        "evidence_ref": artifact("test-check"),
                        "assertion_ids": ["real_case_green"],
                    },
                ]
            )
        )


def test_evidence_rejects_a_receipt_that_precedes_the_captain_request() -> None:
    with pytest.raises(ValidationError, match="request"):
        HermesSkillEvaluationEvidence.model_validate(
            evidence_payload(request=request_payload(occurred_at=NOW + timedelta(minutes=2)))
        )


def test_required_tool_gaps_returns_only_unresolved_required_markers() -> None:
    evidence = HermesSkillEvaluationEvidence.model_validate(
        evidence_payload(
            tool_gaps=[
                gap_payload(gap_id="required-open", severity="required", status="unresolved"),
                gap_payload(gap_id="required-resolved", severity="required", status="resolved"),
                gap_payload(gap_id="optional-open", severity="optional", status="unresolved"),
            ]
        )
    )

    assert tuple(marker.gap_id for marker in required_tool_gaps(evidence)) == ("required-open",)
