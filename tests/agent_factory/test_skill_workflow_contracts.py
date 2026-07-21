from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agenten.agent_factory.skill_workflow_contracts import (
    CandidateRevisionV1,
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactoryFeedbackV1,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)


NOW = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)
JOB_ID = "00000000-0000-0000-0000-000000000301"
CORRELATION_ID = "00000000-0000-0000-0000-000000000302"


def artifact(name: str, digest: str = "a" * 64) -> dict[str, str]:
    return {
        "uri": f"artifact://workflow/{name}",
        "sha256": digest,
        "media_type": "application/json",
    }


def lease_payload(role: str, profile: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.factory-lease.v1",
        "lease_id": f"lease-{role}",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "role": role,
        "capability_profile": profile,
        "capabilities": ["python.compileall"],
        "workspace_ref": "workspace://factory/workflow",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    payload.update(overrides)
    return payload


def released_skill_payload() -> dict[str, object]:
    return {
        "schema": "captain.released-hermes-skill.v1",
        "skill_id": "captain_factory_skill",
        "version": 1,
        "capability": "factory_workflow",
        "content_ref": artifact("released-skill"),
        "content_sha256": "a" * 64,
        "status": "released",
        "released_at": NOW,
        "producer": "captain",
    }


def invocation_payload(step: str, **overrides: object) -> dict[str, object]:
    role_profiles = {
        "discover": ("agent_architect", "factory-architect"),
        "brief_codex": ("tool_integrator", "factory-tool-integrator"),
        "execute_team": ("real_case_tester", "factory-real-case-tester"),
        "evaluate_team": ("quality_warden", "factory-quality-warden"),
        "improve_team": ("tool_integrator", "factory-tool-integrator"),
        "report_captain": ("quality_warden", "factory-quality-warden"),
    }
    role, profile = role_profiles[step]
    payload: dict[str, object] = {
        "schema": "captain.factory-skill-invocation.v1",
        "invocation_id": "00000000-0000-0000-0000-000000000303",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "step": step,
        "released_skill": released_skill_payload(),
        "input_ref": artifact("input"),
        "input_sha256": "a" * 64,
        "lease": lease_payload(role, profile),
        "idempotency_key": "b" * 64,
    }
    payload.update(overrides)
    return payload


def common_payload(step: str, **overrides: object) -> dict[str, object]:
    invocation = invocation_payload(step)
    payload: dict[str, object] = {
        "invocation": invocation,
        "invocation_id": invocation["invocation_id"],
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "occurred_at": NOW + timedelta(minutes=1),
        "producer": "hermes",
        "artifact_ref": artifact(f"{step}-artifact", "b" * 64),
        "evidence_refs": [artifact(f"{step}-evidence", "c" * 64)],
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
    }
    payload.update(overrides)
    return payload


def build_assignment_payload() -> dict[str, object]:
    return {
        "schema": "hermes.factory-build-assignment.v1",
        "assignment_id": "00000000-0000-0000-0000-000000000304",
        "creation_job_id": "00000000-0000-0000-0000-000000000305",
        "correlation_id": CORRELATION_ID,
        "subject_version": 1,
        "attempt": 1,
        "idempotency_key": "d" * 64,
        "released_skill": {
            "skill_id": "factory_build",
            "version": 1,
            "content_ref": artifact("build-skill", "d" * 64),
            "content_sha256": "d" * 64,
        },
        "compiled_spec_ref": artifact("compiled-spec", "e" * 64),
        "dependency_graph_ref": artifact("dependency-graph", "f" * 64),
        "workspace_ref": "workspace://factory/workflow",
        "documentation_queries": [
            {
                "ecosystem": "autogen",
                "package_id": "autogen-agentchat",
                "installed_version": "0.7.5",
                "query": "Swarm handoff validation",
                "required": True,
            }
        ],
        "public_assertion_ids": ["schema_valid", "real_case_green"],
        "deadline_at": NOW + timedelta(minutes=30),
    }


def inventory_payload(**overrides: object) -> dict[str, object]:
    payload = common_payload(
        "discover",
        schema="hermes.factory-codebase-inventory.v1",
        inspected_revision="3e16ac6",
        source_refs=[artifact("source", "d" * 64)],
        reusable_component_ids=["existing_swarm"],
        entrypoint_refs=[artifact("entrypoint", "e" * 64)],
        test_refs=[artifact("tests", "f" * 64)],
        schema_refs=[artifact("schemas", "1" * 64)],
        autogen_version="0.7.5",
        documentation_refs=[artifact("autogen-docs", "2" * 64)],
        tool_catalog_match_ids=["typed_n8n_tool"],
        gap_refs=[artifact("gaps", "3" * 64)],
    )
    payload.update(overrides)
    return payload


def brief_payload(**overrides: object) -> dict[str, object]:
    payload = common_payload(
        "brief_codex",
        schema="hermes.factory-codex-build-brief.v1",
        build_assignment=build_assignment_payload(),
        prompt_ref=artifact("sealed-prompt", "d" * 64),
        context_refs=[artifact("inventory-context", "e" * 64)],
        authorized_path_roots=["workspace://factory/workflow/src"],
        required_test_command_ids=["python.compileall"],
        forbidden_effect_ids=["git.push"],
    )
    payload.update(overrides)
    return payload


def execution_outcome_payload(status: str = "succeeded") -> dict[str, object]:
    return {
        "schema": "captain.execution-outcome.v1",
        "capability_id": "factory_workflow",
        "capability_version": 1,
        "team_version": 1,
        "correlation_id": CORRELATION_ID,
        "command_id": "00000000-0000-0000-0000-000000000306",
        "result_id": "00000000-0000-0000-0000-000000000307",
        "business_output": {"result": "accepted"},
        "assertion_outcomes": [
            {
                "assertion_id": "schema_valid",
                "status": "passed",
                "evidence_refs": [artifact("assertion", "d" * 64)],
            },
            {
                "assertion_id": "real_case_green",
                "status": "passed",
                "evidence_refs": [artifact("real-case", "e" * 64)],
            },
        ],
        "evidence_refs": [artifact("runtime", "f" * 64)],
        "status": status,
    }


def execution_payload(**overrides: object) -> dict[str, object]:
    payload = common_payload(
        "execute_team",
        schema="hermes.factory-team-execution-evidence.v1",
        run_number=1,
        candidate_ref=artifact("candidate", "d" * 64),
        execution_outcome=execution_outcome_payload(),
        usage_receipt_refs=[artifact("usage", "e" * 64)],
        handoff_evidence_refs=[artifact("handoffs", "f" * 64)],
        tool_evidence_refs=[artifact("tools", "1" * 64)],
        workflow_evidence_refs=[],
        termination_reason="task_completed",
        status="succeeded",
    )
    payload.update(overrides)
    return payload


def evaluation_payload(**overrides: object) -> dict[str, object]:
    payload = common_payload(
        "evaluate_team",
        schema="hermes.factory-team-evaluation.v1",
        assertion_outcomes=execution_outcome_payload()["assertion_outcomes"],
        holdout_receipt_refs=[artifact("holdout-receipt", "d" * 64)],
        deterministic_check_refs=[artifact("deterministic-check", "e" * 64)],
        judge_ref=None,
        prior_green_regression_ids=["schema_valid"],
        cost_summary_ref=artifact("cost-summary", "f" * 64),
        latency_summary_ref=artifact("latency-summary", "1" * 64),
        failure_class=None,
        recommendation="PROMOTE_CANDIDATE",
    )
    payload.update(overrides)
    return payload


def revision_payload(**overrides: object) -> dict[str, object]:
    payload = common_payload(
        "improve_team",
        schema="hermes.factory-candidate-revision.v1",
        parent_candidate_ref=artifact("parent-candidate", "d" * 64),
        candidate_ref=artifact("new-candidate", "e" * 64),
        failed_assertion_ids=["real_case_green"],
        changed_components=["system_prompt"],
        regression_assertion_ids=["schema_valid"],
        codex_session_ref=artifact("codex-session", "f" * 64),
    )
    payload.update(overrides)
    return payload


def feedback_payload(**overrides: object) -> dict[str, object]:
    payload = common_payload(
        "report_captain",
        schema="hermes.factory-feedback.v1",
        recommendation="PROMOTE_CANDIDATE",
        assertion_ids=["schema_valid", "real_case_green"],
        tool_gaps=[],
        tool_gap_refs=[],
        reason_codes=["all_assertions_passed"],
    )
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CodebaseInventoryV1, inventory_payload()),
        (CodexBuildBriefV1, brief_payload()),
        (TeamExecutionEvidenceV1, execution_payload()),
        (TeamEvaluationV1, evaluation_payload()),
        (CandidateRevisionV1, revision_payload()),
        (FactoryFeedbackV1, feedback_payload()),
    ],
)
def test_workflow_artifacts_are_frozen_strict_and_round_trip(
    model: type[object], payload: dict[str, object]
) -> None:
    parsed = model.model_validate(payload)  # type: ignore[attr-defined]

    assert model.model_validate(parsed.model_dump(mode="json", by_alias=True)) == parsed  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        model.model_validate(payload | {"unknown": True})  # type: ignore[attr-defined]
    with pytest.raises(ValidationError, match="frozen"):
        parsed.producer = "captain"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["api_key", "authorization", "raw_prompt", "transcript"])
def test_workflow_artifacts_reject_private_fields(field: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        CodebaseInventoryV1.model_validate(inventory_payload() | {field: "secret"})


def test_step_result_must_match_invocation_identity() -> None:
    with pytest.raises(ValidationError, match="invocation"):
        TeamExecutionEvidenceV1.model_validate(
            execution_payload(invocation_id=str(uuid4()))
        )


def test_execution_success_requires_a_passing_runtime_outcome() -> None:
    failed_outcome = execution_outcome_payload(status="failed")
    with pytest.raises(ValidationError, match="successful execution"):
        TeamExecutionEvidenceV1.model_validate(
            execution_payload(execution_outcome=failed_outcome)
        )


def test_feedback_cannot_promote_with_a_required_unresolved_tool_gap() -> None:
    gap = {
        "schema": "TODO_TOOL.v1",
        "gap_id": "missing-tool",
        "severity": "required",
        "input_contract_ref": artifact("tool-input", "d" * 64),
        "output_contract_ref": artifact("tool-output", "e" * 64),
        "least_privilege_capability": "catalog.read",
        "implementation_options": [],
        "acceptance_assertion_ids": ["schema_valid"],
        "evidence_ref": artifact("tool-gap", "f" * 64),
        "status": "unresolved",
    }

    with pytest.raises(ValidationError, match="PROMOTE_CANDIDATE"):
        FactoryFeedbackV1.model_validate(feedback_payload(tool_gaps=[gap]))
