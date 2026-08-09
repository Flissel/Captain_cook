"""Hermes coordinator for one durable Minibook V3 build assignment."""

from __future__ import annotations

import re
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ToolGapMarkerV1,
    codex_build_receipt_sha256,
    validate_build_effect_binding,
)


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HermesFactoryMinibookPort(Protocol):
    async def next_assignment(self) -> FactoryBuildAssignmentV2 | None: ...

    async def claim(
        self,
        assignment_id: UUID,
        worker_id: str,
    ) -> FactoryBuildAssignmentV2: ...

    async def accepted_effect(
        self,
        assignment_id: UUID,
    ) -> FactoryBuildEffectV2 | None: ...

    async def submit_effect(
        self,
        effect: FactoryBuildEffectV2,
    ) -> FactoryBuildEffectV2: ...


class HermesFactorySkillVerifierPort(Protocol):
    def validate_runtime_skill(
        self,
        *,
        skill_name: str,
        released_skill_sha256: str,
    ) -> None: ...


class HermesFactoryCodexBuild(BaseModel):
    """Typed, Captain-sealed Codex output accepted by the Hermes worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hermes_usage_receipt_ref: ArtifactRef
    codex_build_receipt_ref: ArtifactRef
    codex_build_receipt: CodexBuildReceiptV1
    build_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()

    def validate_for(self, assignment: FactoryBuildAssignmentV2) -> None:
        receipt = self.codex_build_receipt
        if receipt.assignment_id != assignment.assignment_id:
            raise ValueError("Codex build receipt assignment does not match")
        if (
            receipt.creation_job_id != assignment.creation_job_id
            or receipt.factory_job_id != assignment.factory_job_id
            or receipt.correlation_id != assignment.correlation_id
            or receipt.subject_version != assignment.subject_version
            or receipt.attempt != assignment.attempt
            or receipt.workspace_ref != assignment.workspace_ref
            or receipt.idempotency_key != assignment.effect_key
            or receipt.seal_idempotency_key != assignment.effect_key
        ):
            raise ValueError("Codex build receipt identity does not match assignment")
        expected_digest = codex_build_receipt_sha256(receipt)
        if (
            self.codex_build_receipt_ref.sha256 != expected_digest
            or self.codex_build_receipt_ref.media_type != "application/json"
            or self.codex_build_receipt_ref.uri
            != f"artifact://captain-codex-build/build-receipt/{expected_digest}"
        ):
            raise ValueError("Codex build receipt reference does not match receipt")
        if self.hermes_usage_receipt_ref.media_type != "application/json":
            raise ValueError("Hermes usage receipt must be application/json")


class HermesFactoryCodexPort(Protocol):
    async def build(
        self,
        assignment: FactoryBuildAssignmentV2,
    ) -> HermesFactoryCodexBuild: ...


class HermesFactoryWorker:
    """Claim once, invoke Codex once, and submit one immutable build effect."""

    def __init__(
        self,
        *,
        minibook: HermesFactoryMinibookPort,
        codex: HermesFactoryCodexPort,
        skill_verifier: HermesFactorySkillVerifierPort,
        worker_id: str,
        released_skill_name: str,
        released_skill_sha256: str,
    ) -> None:
        if _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("Hermes Factory worker identity is invalid")
        if _SKILL_NAME.fullmatch(released_skill_name) is None:
            raise ValueError("Hermes Factory released skill name is invalid")
        if _SHA256.fullmatch(released_skill_sha256) is None:
            raise ValueError("Hermes Factory released skill digest is invalid")
        self._minibook = minibook
        self._codex = codex
        self._skill_verifier = skill_verifier
        self._worker_id = worker_id
        self._released_skill_name = released_skill_name
        self._released_skill_sha256 = released_skill_sha256

    async def run_once(self) -> FactoryBuildEffectV2 | None:
        assignment = await self._minibook.next_assignment()
        if assignment is None:
            return None
        self._skill_verifier.validate_runtime_skill(
            skill_name=self._released_skill_name,
            released_skill_sha256=self._released_skill_sha256,
        )
        claimed = await self._minibook.claim(
            assignment.assignment_id,
            self._worker_id,
        )
        if claimed != assignment:
            raise ValueError("Minibook assignment claim changed immutable content")
        replay = await self._minibook.accepted_effect(assignment.assignment_id)
        if replay is not None:
            validate_build_effect_binding(assignment, replay)
            return replay

        build = await self._codex.build(assignment)
        if not isinstance(build, HermesFactoryCodexBuild):
            raise ValueError("Hermes Factory requires a typed Codex build result")
        build.validate_for(assignment)
        receipt = build.codex_build_receipt
        effect = FactoryBuildEffectV2(
            assignment_id=assignment.assignment_id,
            creation_job_id=assignment.creation_job_id,
            factory_job_id=assignment.factory_job_id,
            correlation_id=assignment.correlation_id,
            subject_version=assignment.subject_version,
            checkpoint_version=assignment.checkpoint_version,
            attempt=assignment.attempt,
            effect_key=assignment.effect_key,
            hermes_usage_receipt_ref=build.hermes_usage_receipt_ref,
            codex_build_receipt_ref=build.codex_build_receipt_ref,
            codex_session_ref=receipt.codex_session_ref,
            workspace_snapshot_ref=receipt.workspace_snapshot_ref,
            source_archive_ref=receipt.source_archive_ref,
            preliminary_team_manifest_ref=receipt.candidate_manifest_ref,
            build_evidence_refs=build.build_evidence_refs,
            test_evidence_refs=receipt.test_evidence_refs,
            tool_gaps=build.tool_gaps,
        )
        accepted = await self._minibook.submit_effect(effect)
        if accepted != effect:
            raise ValueError("Minibook accepted a different build effect")
        return accepted


__all__ = [
    "HermesFactoryCodexBuild",
    "HermesFactoryCodexPort",
    "HermesFactoryMinibookPort",
    "HermesFactorySkillVerifierPort",
    "HermesFactoryWorker",
]
