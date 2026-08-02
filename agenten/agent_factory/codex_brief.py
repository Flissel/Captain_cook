"""Deterministic, sealed Codex build briefs over the existing V1 assignment."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.forge_contracts import FactoryBuildAssignmentV1
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.technical_improvement_contracts import (
    CaptainTechnicalFailureEvaluationV1,
)
from agenten.agent_factory.skill_store import reject_sensitive_data
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


class CodexPromptArtifactStore(Protocol):
    """Persist one immutable prompt without exposing its body in contracts."""

    def persist(self, job_id: UUID, content: bytes) -> ArtifactRef: ...


class FactoryBuildTestCommandPolicy:
    """Select exact command identities from the assignment and execution mode."""

    def required_for(
        self,
        assignment: FactoryBuildAssignmentV1,
        policy: FactoryExecutionPolicyV1,
    ) -> tuple[str, ...]:
        command_ids = ["python.compileall", "pytest.not-live"]
        command_ids.extend(
            f"pytest.integration.{kind}"
            for kind in sorted({item.kind for item in assignment.integrations})
        )
        if policy.live_execution:
            command_ids.append(f"pytest.live.{policy.mode.value}")
        return tuple(command_ids)


class CodexBriefBuilder:
    """Seal a bounded prompt and bind it to Task-3 workflow contracts."""

    _FORBIDDEN_EFFECT_IDS = (
        "captain.ledger.write",
        "git.push",
        "holdout.read",
        "secret.read",
    )

    def __init__(
        self,
        *,
        artifact_store: CodexPromptArtifactStore,
        test_command_policy: FactoryBuildTestCommandPolicy | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._test_command_policy = (
            test_command_policy or FactoryBuildTestCommandPolicy()
        )

    def build(
        self,
        invocation: FactorySkillInvocationV1,
        assignment: FactoryBuildAssignmentV1,
        inventory: CodebaseInventoryV1,
        policy: FactoryExecutionPolicyV1,
        *,
        improvement_authorization: FactoryImprovementAuthorizationV1 | None = None,
    ) -> CodexBuildBriefV1:
        if invocation.step is not FactorySkillStep.BRIEF_CODEX:
            raise ValueError("Codex brief requires the brief_codex skill step")
        self._require_inventory_binding(invocation, inventory)
        self._require_n8n_authority(invocation, assignment)
        self._require_improvement_binding(invocation, improvement_authorization)

        improvement_refs: tuple[ArtifactRef, ...] = ()
        if improvement_authorization is not None:
            improvement_refs = (
                improvement_authorization.authorization_ref,
                improvement_authorization.failed_evaluation.artifact_ref,
                improvement_authorization.prior_candidate_ref,
            )
        context_refs = _unique_refs(
            (
                inventory.artifact_ref,
                ArtifactRef.model_validate(
                    assignment.compiled_spec_ref.model_dump(mode="json")
                ),
                ArtifactRef.model_validate(
                    assignment.dependency_graph_ref.model_dump(mode="json")
                ),
                *inventory.documentation_refs,
                *improvement_refs,
            )
        )
        test_ids = self._test_command_policy.required_for(assignment, policy)
        prompt = self._render(
            invocation=invocation,
            assignment=assignment,
            inventory=inventory,
            policy=policy,
            context_refs=context_refs,
            test_ids=test_ids,
            improvement_authorization=improvement_authorization,
        )
        prompt_ref = self._artifact_store.persist(
            invocation.job_id,
            prompt.encode("utf-8"),
        )
        return CodexBuildBriefV1(
            schema_name="hermes.factory-codex-build-assignment.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=invocation.lease.issued_at,
            producer="hermes",
            artifact_ref=prompt_ref,
            evidence_refs=_unique_refs(
                (prompt_ref, inventory.artifact_ref, *improvement_refs[:2])
            ),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            build_assignment=assignment,
            prompt_ref=prompt_ref,
            context_refs=context_refs,
            authorized_path_roots=(assignment.workspace_ref,),
            required_test_command_ids=test_ids,
            forbidden_effect_ids=self._FORBIDDEN_EFFECT_IDS,
            failed_benchmark_metric_ids=(
                ()
                if improvement_authorization is None
                else improvement_authorization.failed_evaluation.failed_benchmark_metric_ids
            ),
            regression_benchmark_metric_ids=(
                ()
                if improvement_authorization is None
                else improvement_authorization.prior_green_benchmark_metric_ids
            ),
        )

    @staticmethod
    def _require_improvement_binding(
        invocation: FactorySkillInvocationV1,
        authorization: FactoryImprovementAuthorizationV1 | None,
    ) -> None:
        if invocation.attempt > 1 and authorization is None:
            raise ValueError("retry Codex brief requires improvement authorization")
        if invocation.attempt == 1 and authorization is not None:
            raise ValueError("initial Codex brief cannot carry improvement authorization")
        if authorization is None:
            return
        request = authorization.request_block
        if (
            authorization.authorized_attempt != invocation.attempt
            or request.job_id != invocation.job_id
            or request.correlation_id != invocation.correlation_id
            or request.subject_version != invocation.subject_version
        ):
            raise ValueError("improvement authorization does not match Codex invocation")

    @staticmethod
    def _require_inventory_binding(
        invocation: FactorySkillInvocationV1,
        inventory: CodebaseInventoryV1,
    ) -> None:
        if (
            inventory.job_id != invocation.job_id
            or inventory.correlation_id != invocation.correlation_id
            or inventory.subject_version != invocation.subject_version
            or inventory.attempt != 1
            or inventory.acceptance_assertion_ids
            != invocation.acceptance_assertion_ids
        ):
            raise ValueError("codebase inventory does not match the Codex invocation")

    @staticmethod
    def _require_n8n_authority(
        invocation: FactorySkillInvocationV1,
        assignment: FactoryBuildAssignmentV1,
    ) -> None:
        requests_n8n = any(item.kind == "n8n" for item in assignment.integrations)
        authorized_n8n = invocation.lease.integration_intent is IntegrationIntent.N8N
        if requests_n8n != authorized_n8n:
            raise ValueError(
                "n8n requires both a compiled integration requirement and separate lease authority"
            )

    @classmethod
    def _render(
        cls,
        *,
        invocation: FactorySkillInvocationV1,
        assignment: FactoryBuildAssignmentV1,
        inventory: CodebaseInventoryV1,
        policy: FactoryExecutionPolicyV1,
        context_refs: tuple[ArtifactRef, ...],
        test_ids: tuple[str, ...],
        improvement_authorization: FactoryImprovementAuthorizationV1 | None,
    ) -> str:
        prior_green_ids = (
            []
            if improvement_authorization is None
            else list(improvement_authorization.prior_green_assertion_ids)
        )
        failed_assertion_ids = (
            []
            if improvement_authorization is None
            else [
                outcome.assertion_id
                for outcome in improvement_authorization.failed_evaluation.assertion_outcomes
                if outcome.status == "failed"
            ]
        )
        prior_green_benchmark_metric_ids = (
            []
            if improvement_authorization is None
            else list(
                improvement_authorization.prior_green_benchmark_metric_ids
            )
        )
        failed_benchmark_metric_ids = (
            []
            if improvement_authorization is None
            else list(
                improvement_authorization.failed_evaluation.failed_benchmark_metric_ids
            )
        )
        benchmark_reason_codes = (
            []
            if improvement_authorization is None
            else list(
                improvement_authorization.failed_evaluation.benchmark_reason_codes
            )
        )
        failed_evaluation = (
            None
            if improvement_authorization is None
            else improvement_authorization.failed_evaluation
        )
        technical_diagnostic_codes = (
            []
            if not isinstance(
                failed_evaluation,
                CaptainTechnicalFailureEvaluationV1,
            )
            else list(failed_evaluation.technical_diagnostic_codes)
        )
        technical_retry_contract: list[str] = [
                "The real-case command receives no stdin.",
                "Read CAPTAIN_TRACE_ID from the process environment.",
                "Emit exactly one JSON object on stdout and no prose.",
                "Set trace_id to CAPTAIN_TRACE_ID.",
                "Set assertion_ids to exactly the Captain acceptance assertion IDs.",
                "Exit zero only after the deterministic real-case fixture is evaluated.",
        ]
        if "candidate_build_command_failed" in technical_diagnostic_codes:
            technical_retry_contract.append(
                "Run the candidate build command to a zero exit code, fix its tests "
                "without weakening assertions, then regenerate candidate.zip from "
                "the verified source tree."
            )
        if (
            "mandatory_handoff" in invocation.acceptance_assertion_ids
            or "mandatory_handoff_failed" in technical_diagnostic_codes
        ):
            technical_retry_contract.append(
                "Require at least one meaningful configured agent handoff before terminal completion; "
                "the initial agent must not complete the task before specialist collaboration."
            )
            technical_retry_contract.append(
                "Every agent with an outgoing AutoGen handoff must explicitly name and call "
                "the generated transfer_to_<target_agent> tool in its sealed system prompt."
            )
        if (
            "business_value" in invocation.acceptance_assertion_ids
            or "business_value_failed" in technical_diagnostic_codes
        ):
            technical_retry_contract.append(
                "Produce an evidence-grounded business decision and rationale after specialist "
                "collaboration, matching the public output contract."
            )
            technical_retry_contract.append(
                "Treat public_team_build_contract in compiled-spec.json as normative: preserve "
                "its exact agent names, handoff graph, tool allocation, system prompts, limits, "
                "terminal JSON contract, and all five public acceptance categories."
            )
        if "observed_rationale_incomplete" in technical_diagnostic_codes:
            technical_retry_contract.append(
                "Preserve every evidence-grounded rationale fact from specialist messages in "
                "the terminal rationale_fact_ids output; do not omit facts used for the decision."
            )
        if "observed_decision_mismatch" in technical_diagnostic_codes:
            technical_retry_contract.append(
                "Reconcile specialist evidence before emitting the terminal decision; the decision "
                "must follow the candidate's documented deterministic business rules."
            )
        if "terminal_missing_or_invalid" in technical_diagnostic_codes:
            technical_retry_contract.append(
                "Always emit the exact structured terminal output contract after collaboration."
            )
        document = {
            "Goal": (
                "Implement the dependency-ready node described by "
                f"{assignment.compiled_spec_ref.uri}."
            ),
            "Measurable outcome": {
                "acceptance_assertion_ids": list(invocation.acceptance_assertion_ids),
                "prior green assertions": prior_green_ids,
                "failed assertion IDs": failed_assertion_ids,
                "prior green benchmark metrics": prior_green_benchmark_metric_ids,
                "failed benchmark metric IDs": failed_benchmark_metric_ids,
                "benchmark reason codes": benchmark_reason_codes,
                "technical diagnostic codes": technical_diagnostic_codes,
                "technical retry contract": technical_retry_contract,
            },
            "selected reusable components": list(inventory.reusable_component_ids),
            "authorized workspace roots": [assignment.workspace_ref],
            "exact command and test IDs": list(test_ids),
            "leased capability IDs": list(invocation.lease.capabilities),
            "architecture rules": [
                "Keep Captain as lifecycle and validation authority.",
                "Use injected typed ports and preserve deterministic offline execution.",
                "Write only below the authorized workspace roots.",
            ],
            "tool resolution order": {
                "reuse_catalog_ids": list(inventory.tool_catalog_match_ids),
                "unresolved_gap_refs": [ref.uri for ref in inventory.gap_refs],
                "fallback": "emit TODO_TOOL.v1",
            },
            "documentation refs": [ref.uri for ref in inventory.documentation_refs],
            "documentation requirements": [
                {
                    "ecosystem": query.ecosystem,
                    "package_id": query.package_id,
                    "installed_version": query.installed_version,
                    "required": query.required,
                }
                for query in assignment.documentation_queries
            ],
            "context refs": [ref.uri for ref in context_refs],
            "prior candidate ref": (
                None
                if improvement_authorization is None
                else improvement_authorization.prior_candidate_ref.uri
            ),
            "integration IDs": [item.integration_id for item in assignment.integrations],
            "live and sandbox policy": {
                "live_execution": policy.live_execution,
                "max_cost_usd": str(policy.max_cost_usd),
                "max_runtime_seconds": policy.max_runtime_seconds,
                "allowed_models": list(policy.allowed_models),
                "live_capabilities": [item.value for item in policy.live_capabilities],
                "sandbox_mode": policy.sandbox_mode.value,
            },
            "artifact requirements": [
                "code refs",
                "test refs",
                "manifest refs",
                "content digests",
                "command evidence",
                "typed failure evidence",
            ],
            "forbidden effects": list(cls._FORBIDDEN_EFFECT_IDS),
        }
        reject_sensitive_data(document, "Codex build brief")
        return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _unique_refs(references: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    unique: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        key = (reference.uri, reference.sha256, reference.media_type)
        unique.setdefault(key, reference)
    return tuple(unique.values())
