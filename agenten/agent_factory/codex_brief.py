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
from agenten.agent_factory.skill_store import reject_sensitive_data
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


class CodexPromptArtifactStore(Protocol):
    """Persist one immutable prompt without exposing its body in contracts."""

    def persist(self, job_id: UUID, content: bytes) -> ArtifactRef: ...


class CodexBriefBuilder:
    """Seal a bounded prompt and bind it to Task-3 workflow contracts."""

    _FORBIDDEN_EFFECT_IDS = (
        "captain.ledger.write",
        "git.push",
        "holdout.read",
        "secret.read",
    )

    def __init__(self, *, artifact_store: CodexPromptArtifactStore) -> None:
        self._artifact_store = artifact_store

    def build(
        self,
        invocation: FactorySkillInvocationV1,
        assignment: FactoryBuildAssignmentV1,
        inventory: CodebaseInventoryV1,
        policy: FactoryExecutionPolicyV1,
    ) -> CodexBuildBriefV1:
        if invocation.step is not FactorySkillStep.BRIEF_CODEX:
            raise ValueError("Codex brief requires the brief_codex skill step")
        self._require_inventory_binding(invocation, inventory)
        self._require_n8n_authority(invocation, assignment)

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
            )
        )
        test_ids = tuple(sorted(invocation.lease.capabilities))
        prompt = self._render(
            invocation=invocation,
            assignment=assignment,
            inventory=inventory,
            policy=policy,
            context_refs=context_refs,
            test_ids=test_ids,
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
            evidence_refs=(prompt_ref, inventory.artifact_ref),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            build_assignment=assignment,
            prompt_ref=prompt_ref,
            context_refs=context_refs,
            authorized_path_roots=(assignment.workspace_ref,),
            required_test_command_ids=test_ids,
            forbidden_effect_ids=self._FORBIDDEN_EFFECT_IDS,
        )

    @staticmethod
    def _require_inventory_binding(
        invocation: FactorySkillInvocationV1,
        inventory: CodebaseInventoryV1,
    ) -> None:
        if (
            inventory.job_id != invocation.job_id
            or inventory.correlation_id != invocation.correlation_id
            or inventory.subject_version != invocation.subject_version
            or inventory.attempt != invocation.attempt
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
    ) -> str:
        document = {
            "Goal": (
                "Implement the dependency-ready node described by "
                f"{assignment.compiled_spec_ref.uri}."
            ),
            "Measurable outcome": {
                "acceptance_assertion_ids": list(invocation.acceptance_assertion_ids),
                "prior green assertions": [],
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
