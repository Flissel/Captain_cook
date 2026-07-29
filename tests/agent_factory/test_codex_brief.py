from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import UUID

import pytest

from agenten.agent_factory.codex_brief import CodexBriefBuilder
from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryPhase
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
    FactorySandboxMode,
)
from agenten.agent_factory.forge_contracts import FactoryBuildAssignmentV1
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    FactorySkillInvocationV1,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_workflow_contracts import (
    artifact,
    build_assignment_payload,
    inventory_payload,
    invocation_payload,
    lease_payload,
    evaluation_payload,
)
from tests.agent_factory.test_state_machine import block


class PromptArtifactStore:
    def __init__(self) -> None:
        self._content: dict[str, str] = {}

    def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        reference = ArtifactRef(
            uri=f"artifact://factory-prompts/{job_id}/{digest}",
            sha256=digest,
            media_type="text/markdown",
        )
        self._content[reference.uri] = content.decode("utf-8")
        return reference

    def read(self, reference: ArtifactRef) -> str:
        return self._content[reference.uri]


def policy() -> FactoryExecutionPolicyV1:
    return FactoryExecutionPolicyV1(
        schema_name="captain.factory-execution-policy.v1",
        mode=FactoryExecutionMode.DEMO,
        live_execution=True,
        max_cost_usd=Decimal("2.50"),
        max_runtime_seconds=900,
        required_live_runs=1,
        allowed_models=("gpt-5.1-codex-mini",),
        live_capabilities=(FactoryLiveCapability.MODEL_INVOKE,),
        sandbox_mode=FactorySandboxMode.WORKSPACE_WRITE,
    )


def retry_authorization() -> FactoryImprovementAuthorizationV1:
    evaluation_data = evaluation_payload(
        failure_class="behavioral_failure",
        recommendation="RETRY_BUILD",
        prior_green_regression_ids=["schema_valid"],
        benchmark_disposition="failed",
        benchmark_reason_codes=["unsafe_tool_intent"],
        failed_benchmark_metric_ids=["tool_safety"],
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
            "artifact_refs": [prior_candidate.model_dump(mode="json")],
            "evidence_refs": [evaluation.artifact_ref.model_dump(mode="json")],
        }
    )
    return FactoryImprovementAuthorizationV1(
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
        prior_green_assertion_ids=("schema_valid",),
        prior_green_benchmark_metric_ids=("coverage",),
    )


def test_codex_brief_contains_goal_gates_and_only_opaque_refs() -> None:
    store = PromptArtifactStore()
    invocation = FactorySkillInvocationV1.model_validate(
        invocation_payload("brief_codex")
    )
    assignment = FactoryBuildAssignmentV1.model_validate(build_assignment_payload())
    inventory = CodebaseInventoryV1.model_validate(inventory_payload())

    brief = CodexBriefBuilder(artifact_store=store).build(
        invocation,
        assignment,
        inventory,
        policy(),
    )

    rendered = store.read(brief.prompt_ref)
    assert "Goal" in rendered
    assert "Measurable outcome" in rendered
    assert "prior green assertions" in rendered
    assert "max_cost_usd" in rendered
    assert assignment.compiled_spec_ref.uri in rendered
    assert inventory.reusable_component_ids[0] in rendered
    assert invocation.lease.capabilities[0] in rendered
    assert assignment.workspace_ref in rendered
    assert "C:\\Users" not in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert brief.build_assignment == assignment
    assert brief.required_test_command_ids == (
        "python.compileall",
        "pytest.not-live",
        "pytest.live.demo",
    )
    assert brief.required_test_command_ids != invocation.lease.capabilities


def test_codex_brief_is_deterministic_and_keeps_context_opaque() -> None:
    store = PromptArtifactStore()
    builder = CodexBriefBuilder(artifact_store=store)
    invocation = FactorySkillInvocationV1.model_validate(
        invocation_payload("brief_codex")
    )
    assignment = FactoryBuildAssignmentV1.model_validate(build_assignment_payload())
    inventory = CodebaseInventoryV1.model_validate(inventory_payload())

    first = builder.build(invocation, assignment, inventory, policy())
    second = builder.build(invocation, assignment, inventory, policy())

    assert first == second
    assert tuple(ref.uri for ref in first.context_refs) == (
        inventory.artifact_ref.uri,
        assignment.compiled_spec_ref.uri,
        assignment.dependency_graph_ref.uri,
        *(ref.uri for ref in inventory.documentation_refs),
    )


@pytest.mark.parametrize(
    ("compiled_n8n", "authorized_n8n"),
    [(True, False), (False, True)],
)
def test_codex_brief_requires_compiled_n8n_intent_and_separate_authority(
    compiled_n8n: bool,
    authorized_n8n: bool,
) -> None:
    invocation_data = invocation_payload("brief_codex")
    if authorized_n8n:
        invocation_data["lease"] = lease_payload(
            "tool_integrator",
            "n8n-builder",
            integration_intent="n8n",
            capabilities=["n8n.workflow.manage"],
        )
    assignment_data = build_assignment_payload()
    if compiled_n8n:
        assignment_data["integrations"] = [
            {
                "integration_id": "support_workflow",
                "kind": "n8n",
                "severity": "required",
                "input_contract_ref": artifact("n8n-input"),
                "output_contract_ref": artifact("n8n-output"),
            }
        ]

    with pytest.raises(ValueError, match="n8n requires both"):
        CodexBriefBuilder(artifact_store=PromptArtifactStore()).build(
            FactorySkillInvocationV1.model_validate(invocation_data),
            FactoryBuildAssignmentV1.model_validate(assignment_data),
            CodebaseInventoryV1.model_validate(inventory_payload()),
            policy(),
        )


def test_retry_brief_binds_failed_evaluation_candidate_and_prior_green() -> None:
    authorization = retry_authorization()
    invocation_data = invocation_payload(
        "brief_codex",
        attempt=2,
        lease=lease_payload(
            "tool_integrator",
            "factory-tool-integrator",
            attempt=2,
        ),
    )
    invocation = FactorySkillInvocationV1.model_validate(invocation_data)
    assignment_data = build_assignment_payload()
    assignment_data["attempt"] = 2
    assignment = FactoryBuildAssignmentV1.model_validate(assignment_data)
    inventory_data = inventory_payload()
    inventory = CodebaseInventoryV1.model_validate(inventory_data)
    store = PromptArtifactStore()

    brief = CodexBriefBuilder(artifact_store=store).build(
        invocation,
        assignment,
        inventory,
        policy(),
        improvement_authorization=authorization,
    )

    assert authorization.authorization_ref in brief.context_refs
    assert authorization.failed_evaluation.artifact_ref in brief.context_refs
    assert authorization.prior_candidate_ref in brief.context_refs
    rendered = store.read(brief.prompt_ref)
    assert '"prior green assertions": [\n      "schema_valid"' in rendered
    assert '"prior green benchmark metrics": [\n      "coverage"' in rendered
    assert '"failed benchmark metric IDs": [\n      "tool_safety"' in rendered
    assert '"benchmark reason codes": [\n      "unsafe_tool_intent"' in rendered
    assert brief.failed_benchmark_metric_ids == ("tool_safety",)
    assert brief.regression_benchmark_metric_ids == ("coverage",)
    assert authorization.prior_candidate_ref.uri in rendered
