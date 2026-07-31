"""Captain-owned bridge from persisted Factory blocks to Forge inputs."""

from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryPhase,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactorySkillStep,
)
from agenten.agent_runtime.contracts import ArtifactRef


_FACTORY_EVIDENCE_PREFIX = "artifact://factory-evidence/"
_ARTIFACT_TYPES = {
    "hermes.factory-codebase-inventory.v1": CodebaseInventoryV1,
    "hermes.factory-codex-build-assignment.v1": CodexBuildBriefV1,
    "hermes.factory-codex-build-evidence.v1": CodexBuildEvidenceV1,
}
_PHASE_ARTIFACT_TYPES = {
    FactoryPhase.BLUEPRINT_CREATED: (CodebaseInventoryV1,),
    FactoryPhase.TOOL_CANDIDATE_TESTED: (
        CodexBuildBriefV1,
        CodexBuildEvidenceV1,
    ),
}


class FactoryForgeEvidenceRepository(Protocol):
    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]: ...

    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


class CaptainForgeEvidenceBridge:
    """Resolve only digest-verified Forge inputs referenced by Captain blocks."""

    def __init__(
        self,
        *,
        repository: FactoryForgeEvidenceRepository,
        evidence_store: FilesystemFactoryEvidenceStore,
    ) -> None:
        self._repository = repository
        self._evidence_store = evidence_store

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]:
        artifacts: list[object] = []
        loaded: dict[tuple[str, str, str], object] = {}
        for block in self._repository.blocks(job_id):
            if block.job_id != job_id:
                raise FactoryDispatchError(
                    "Captain forge evidence block does not match requested job"
                )
            allowed_types = _PHASE_ARTIFACT_TYPES.get(block.phase)
            if block.status is not FactoryBlockStatus.SUCCEEDED or allowed_types is None:
                continue
            for reference in (*block.artifact_refs, *block.evidence_refs):
                if not reference.uri.startswith(_FACTORY_EVIDENCE_PREFIX):
                    continue
                identity = (reference.uri, reference.sha256, reference.media_type)
                artifact = loaded.get(identity)
                if artifact is None:
                    artifact = self._load(reference, job_id=job_id)
                    loaded[identity] = artifact
                if not isinstance(artifact, allowed_types):
                    raise FactoryDispatchError(
                        "Captain forge evidence schema does not match its lifecycle phase"
                    )
                if artifact not in artifacts:
                    artifacts.append(artifact)
        return tuple(artifacts)

    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill:
        return self._repository.released_for(job, step)

    def _load(self, reference: ArtifactRef, *, job_id: UUID) -> object:
        if reference.media_type != "application/json":
            raise FactoryDispatchError("Captain forge evidence must be JSON")
        try:
            content = self._evidence_store.read_verified(reference, job_id=job_id)
        except FileNotFoundError:
            raise FactoryDispatchError("Captain forge evidence file is missing") from None
        except OSError:
            raise FactoryDispatchError("Captain forge evidence file is unreadable") from None
        except ValueError as exc:
            raise FactoryDispatchError(str(exc)) from None
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("factory evidence payload must be an object")
            schema_name = payload.get("schema")
            if not isinstance(schema_name, str):
                raise ValueError("factory evidence schema must be a string")
            model = _ARTIFACT_TYPES.get(schema_name)
            if model is None:
                raise ValueError("factory evidence schema is not a Forge input")
            artifact = model.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError):
            raise FactoryDispatchError("Captain forge evidence schema is invalid") from None
        if artifact.job_id != job_id:
            raise FactoryDispatchError(
                "Captain forge evidence artifact does not match requested job"
            )
        return artifact
