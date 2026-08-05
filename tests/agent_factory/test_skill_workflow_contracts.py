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
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)


NOW = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)
JOB_ID = "00000000-0000-0000-0000-000000000301"
CORRELATION_ID = "00000000-0000-0000-0000-000000000302"
EXPECTED_SKILL_IDS = {
    "discover": "captain-factory-discover",
    "brief_codex": "captain-factory-brief-codex",
    "execute_team": "captain-factory-execute-team",
    "evaluate_team": "captain-factory-evaluate-team",
    "improve_team": "captain-factory-improve-team",
    "report_captain": "captain-factory-report-captain",
}


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


def released_skill_payload(
    skill_id: str = "captain-factory-discover",
) -> dict[str, object]:
    return {
        "schema": "captain.released-hermes-skill.v1",
        "skill_id": skill_id,
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
        "released_skill": released_skill_payload(EXPECTED_SKILL_IDS[step]),
        "input_ref": artifact("input"),
        "input_sha256": "a" * 64,
        "lease": lease_payload(role, profile),
        "idempotency_key": "b" * 64,
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
    }
    payload.update(overrides)
    return payload


def benchmark_summary_artifact(digest: str = "4" * 64) -> dict[str, str]:
    return {
        "uri": f"artifact://business-benchmark-summary/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }


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
        "idempotency_key": "b" * 64,
        "released_skill": {
            "skill_id": EXPECTED_SKILL_IDS["brief_codex"],
            "version": 1,
            "content_ref": artifact("released-skill"),
            "content_sha256": "a" * 64,
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
        schema="hermes.factory-codex-build-assignment.v1",
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
    holdout_ref = {
        "schema_name": "captain.private-holdout-ref.v1",
        "holdout_id": "holdout-222222222222",
        "uri": "holdout://holdout-222222222222",
        "sha256": "2" * 64,
    }
    payload = common_payload(
        "execute_team",
        schema="hermes.factory-team-execution-evidence.v1",
        run_number=1,
        candidate_ref=artifact("candidate", "d" * 64),
        holdout_ref=holdout_ref,
        execution_outcome=execution_outcome_payload(),
        usage_receipt_refs=[artifact("usage", "e" * 64)],
        handoff_evidence_refs=[artifact("handoffs", "f" * 64)],
        tool_evidence_refs=[artifact("tools", "1" * 64)],
        workflow_evidence_refs=[],
        termination_reason="task_completed",
        status="succeeded",
    )
    invocation = payload["invocation"]
    assert isinstance(invocation, dict)
    invocation["execution_scope_ref"] = holdout_ref
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
        benchmark_summary_ref=benchmark_summary_artifact(),
        benchmark_policy_id="captain-business-value-v1",
        benchmark_disposition="passed",
        benchmark_reason_codes=[],
        failed_benchmark_metric_ids=[],
        prior_green_benchmark_metric_ids=["coverage"],
        evidence_refs=[
            artifact("evaluate_team-evidence", "c" * 64),
            benchmark_summary_artifact(),
        ],
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
        failed_benchmark_metric_ids=[],
        changed_components=["system_prompt"],
        regression_assertion_ids=["schema_valid"],
        regression_benchmark_metric_ids=["coverage"],
        codex_session_ref=artifact("codex-session", "f" * 64),
    )
    payload.update(overrides)
    return payload


def test_legacy_team_evaluation_remains_readable_without_passing_benchmark() -> None:
    payload = evaluation_payload()
    for field in (
        "benchmark_summary_ref",
        "benchmark_policy_id",
        "benchmark_disposition",
        "benchmark_reason_codes",
        "failed_benchmark_metric_ids",
        "prior_green_benchmark_metric_ids",
    ):
        payload.pop(field)

    evaluation = TeamEvaluationV1.model_validate(payload)

    assert evaluation.benchmark_summary_ref is None
    assert evaluation.benchmark_disposition is None
    assert evaluation.failed_benchmark_metric_ids == ()


def test_bound_team_evaluation_requires_benchmark_summary_evidence() -> None:
    payload = evaluation_payload(
        evidence_refs=[artifact("evaluate_team-evidence", "c" * 64)]
    )
    with pytest.raises(ValidationError, match="benchmark summary ref"):
        TeamEvaluationV1.model_validate(payload)


def test_candidate_revision_accepts_benchmark_only_failure_namespace() -> None:
    revision = CandidateRevisionV1.model_validate(
        revision_payload(
            failed_assertion_ids=[],
            failed_benchmark_metric_ids=["tool_safety"],
        )
    )

    assert revision.failed_assertion_ids == ()
    assert revision.failed_benchmark_metric_ids == ("tool_safety",)


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


def tool_gap_payload(
    *, severity: str = "required", status: str = "unresolved"
) -> dict[str, object]:
    return {
        "schema": "TODO_TOOL.v1",
        "gap_id": "missing-tool",
        "severity": severity,
        "input_contract_ref": artifact("tool-input", "d" * 64),
        "output_contract_ref": artifact("tool-output", "e" * 64),
        "least_privilege_capability": "catalog.read",
        "implementation_options": [],
        "acceptance_assertion_ids": ["schema_valid"],
        "evidence_ref": artifact("tool-gap", "f" * 64),
        "status": status,
    }


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
def test_workflow_artifact_schemas_are_exact(
    model: type[object], payload: dict[str, object]
) -> None:
    payload["schema"] = "hermes.factory-wrong.v2"

    with pytest.raises(ValidationError, match="schema"):
        model.model_validate(payload)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("step", "role", "profile"),
    [
        ("discover", "agent_architect", "factory-architect"),
        ("brief_codex", "tool_integrator", "factory-tool-integrator"),
        ("execute_team", "real_case_tester", "factory-real-case-tester"),
        ("evaluate_team", "quality_warden", "factory-quality-warden"),
        ("improve_team", "tool_integrator", "factory-tool-integrator"),
        ("report_captain", "quality_warden", "factory-quality-warden"),
    ],
)
def test_every_step_accepts_only_its_captain_role(
    step: str, role: str, profile: str
) -> None:
    invocation = FactorySkillInvocationV1.model_validate(invocation_payload(step))

    assert invocation.step is FactorySkillStep(step)
    assert invocation.lease.role.value == role
    assert invocation.lease.capability_profile.value == profile


@pytest.mark.parametrize(
    ("step", "skill_id"),
    tuple(EXPECTED_SKILL_IDS.items()),
)
def test_every_step_requires_its_exact_released_skill_id(
    step: str,
    skill_id: str,
) -> None:
    invocation = FactorySkillInvocationV1.model_validate(invocation_payload(step))

    assert invocation.released_skill.skill_id == skill_id

    payload = invocation_payload(step)
    foreign = dict(payload["released_skill"])  # type: ignore[arg-type]
    foreign["skill_id"] = "captain-factory-foreign"
    payload["released_skill"] = foreign
    with pytest.raises(ValidationError, match="skill ID"):
        FactorySkillInvocationV1.model_validate(payload)


@pytest.mark.parametrize(
    ("step", "wrong_role", "wrong_profile"),
    [
        ("discover", "tool_integrator", "factory-tool-integrator"),
        ("brief_codex", "agent_architect", "factory-architect"),
        ("execute_team", "quality_warden", "factory-quality-warden"),
        ("evaluate_team", "real_case_tester", "factory-real-case-tester"),
        ("improve_team", "agent_architect", "factory-architect"),
        ("report_captain", "tool_integrator", "factory-tool-integrator"),
    ],
)
def test_every_step_rejects_a_different_valid_role(
    step: str, wrong_role: str, wrong_profile: str
) -> None:
    payload = invocation_payload(step)
    payload["lease"] = lease_payload(wrong_role, wrong_profile)

    with pytest.raises(ValidationError, match="role"):
        FactorySkillInvocationV1.model_validate(payload)


def test_invocation_rejects_changed_input_digest_and_duplicate_captain_assertions() -> None:
    with pytest.raises(ValidationError, match="input digest"):
        FactorySkillInvocationV1.model_validate(
            invocation_payload("discover", input_sha256="f" * 64)
        )
    with pytest.raises(ValidationError, match="Captain assertion"):
        FactorySkillInvocationV1.model_validate(
            invocation_payload(
                "discover",
                acceptance_assertion_ids=["schema_valid", "schema_valid"],
            )
        )


def test_result_assertions_are_bound_to_the_captain_invocation() -> None:
    with pytest.raises(ValidationError, match="invocation.*assertion"):
        CodebaseInventoryV1.model_validate(
            inventory_payload(acceptance_assertion_ids=["schema_valid"])
        )


@pytest.mark.parametrize(
    "occurred_at",
    [NOW - timedelta(microseconds=1), NOW + timedelta(minutes=10)],
)
def test_step_result_requires_an_active_lease(occurred_at: datetime) -> None:
    with pytest.raises(ValidationError, match="active lease"):
        CodebaseInventoryV1.model_validate(inventory_payload(occurred_at=occurred_at))


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ("released_skill_id", "released skill"),
        ("released_skill_digest", "released skill"),
        ("idempotency_key", "idempotency"),
        ("workspace_ref", "workspace"),
    ],
)
def test_codex_assignment_is_bound_to_the_invocation(
    binding: str, message: str
) -> None:
    assignment = build_assignment_payload()
    if binding == "released_skill_id":
        assignment["released_skill"] = {
            "skill_id": "different-skill",
            "version": 1,
            "content_ref": artifact("released-skill"),
            "content_sha256": "a" * 64,
        }
    elif binding == "released_skill_digest":
        assignment["released_skill"] = {
            "skill_id": EXPECTED_SKILL_IDS["brief_codex"],
            "version": 1,
            "content_ref": artifact("released-skill", "9" * 64),
            "content_sha256": "9" * 64,
        }
    elif binding == "idempotency_key":
        assignment["idempotency_key"] = "9" * 64
    else:
        assignment["workspace_ref"] = "workspace://factory/different"

    with pytest.raises(ValidationError, match=message):
        CodexBuildBriefV1.model_validate(brief_payload(build_assignment=assignment))


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\User\secret.txt",
        "C:/Temp/secret.txt",
        r"\\server\share\secret.txt",
        "/root/secret.txt",
        "/srv/secret.txt",
        "/Users/tester/secret.txt",
        "/tmp/secret.txt",
        "/var/log/secret.txt",
        "/mnt/build/secret.txt",
        "/data/factory/secret.txt",
        "/usr/local/bin/secret",
    ],
)
def test_workflow_artifacts_reject_absolute_user_and_system_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="private|local.path"):
        CodebaseInventoryV1.model_validate(inventory_payload(autogen_version=path))


@pytest.mark.parametrize(
    "prose",
    [
        "failed at /mnt/private/project",
        'failed at "/data/private/project"',
        "failed at (/usr/root/private)",
    ],
)
def test_workflow_artifacts_reject_absolute_posix_paths_embedded_in_prose(
    prose: str,
) -> None:
    with pytest.raises(ValidationError, match="local.path"):
        TeamExecutionEvidenceV1.model_validate(
            execution_payload(termination_reason=prose)
        )


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://workflow/tmp/result",
        "artifact://workflow/var/result",
        "artifact://workflow/root/result",
        "artifact://workflow/Users/result",
        "artifact://workflow/mnt/result",
        "artifact://workflow/data/result",
        "artifact://workflow/usr/local/result",
        "artifact://sha256/" + "d" * 64,
    ],
)
def test_workflow_artifacts_accept_opaque_uri_path_segments(uri: str) -> None:
    source = artifact("opaque-source", "d" * 64)
    source["uri"] = uri

    inventory = CodebaseInventoryV1.model_validate(
        inventory_payload(source_refs=[source])
    )

    assert inventory.source_refs[0].uri == uri


@pytest.mark.parametrize(
    "opaque_ref",
    [
        "artifact://workflow/mnt/private/project",
        "holdout://factory/data/private/project",
        "workspace://factory/usr/root/private",
    ],
)
def test_workflow_artifacts_accept_complete_opaque_refs_in_prose(
    opaque_ref: str,
) -> None:
    evidence = TeamExecutionEvidenceV1.model_validate(
        execution_payload(termination_reason=opaque_ref)
    )

    assert evidence.termination_reason == opaque_ref


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_refs", [artifact("same"), artifact("same")]),
        ("reusable_component_ids", ["same", "same"]),
        ("evidence_refs", [artifact("same"), artifact("same")]),
    ],
)
def test_inventory_rejects_duplicate_ids_and_refs(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="unique|duplicates"):
        CodebaseInventoryV1.model_validate(inventory_payload(**{field: value}))


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
    gap = tool_gap_payload()

    with pytest.raises(ValidationError, match="PROMOTE_CANDIDATE"):
        FactoryFeedbackV1.model_validate(
            feedback_payload(tool_gaps=[gap], tool_gap_refs=[gap["evidence_ref"]])
        )


def test_feedback_cannot_promote_a_bare_tool_gap_reference() -> None:
    with pytest.raises(ValidationError, match="tool gap refs"):
        FactoryFeedbackV1.model_validate(
            feedback_payload(tool_gap_refs=[artifact("tool-gap", "f" * 64)])
        )


def test_feedback_promotion_allows_optional_unresolved_gap_when_assertions_are_green() -> None:
    unresolved_optional = tool_gap_payload(severity="optional")
    feedback = FactoryFeedbackV1.model_validate(
        feedback_payload(
            tool_gaps=[unresolved_optional],
            tool_gap_refs=[unresolved_optional["evidence_ref"]],
        )
    )

    assert feedback.recommendation.value == "PROMOTE_CANDIDATE"
    assert feedback.tool_gaps[0].status == "unresolved"

    with pytest.raises(ValidationError, match="all Captain assertions"):
        FactoryFeedbackV1.model_validate(
            feedback_payload(
                tool_gaps=[unresolved_optional],
                tool_gap_refs=[unresolved_optional["evidence_ref"]],
                assertion_ids=["schema_valid"],
            )
        )
