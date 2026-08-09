"""Concrete submit adapter for Minibook's existing SwarmPipeline CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Protocol
from uuid import UUID, uuid5

import httpx
from pydantic import BaseModel, ConfigDict

from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, MinibookForgePort
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.capability_resolution import CapabilityResolution
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationJobV2,
    CreationJobV3,
    CreationProgressV1,
    CreationResultV1,
    CreationResultV2,
    CreationSubmissionReceipt,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ForgeBuildSkillUsageReceiptV1,
)
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    CandidateRevisionV1,
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactorySkillStep,
)
from agenten.agent_factory.state_machine import FactoryActionKind


class FactoryInputMaterializer(Protocol):
    def materialize(self, reference: ArtifactRef) -> Path:
        """Resolve a Captain artifact into a local, read-only input file path."""


class CreationJobMapper(Protocol):
    def map(self, request: FactoryDispatch) -> CreationJobV1 | CreationJobV3: ...


class ForgeBuildSkillReceiptProvider(Protocol):
    def build_skill_receipt(
        self,
        request: FactoryDispatch,
        creation_job: CreationJobV1,
    ) -> ForgeBuildSkillUsageReceiptV1: ...


class CaptainForgeEvidencePort(Protocol):
    """Read only Captain-owned evidence required to authorize a Forge job."""

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...

    def released_for(
        self,
        job: object,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


class CaptainCreationJobV3Authority(BaseModel):
    """Captain-owned immutable inputs that must equal the compiled V3 plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    capability_resolution: CapabilityResolution
    compiled_spec_ref: ArtifactRef
    lease_ref: ArtifactRef
    blueprint_ref: ArtifactRef
    tool_plan_ref: ArtifactRef
    required_agent_ids: tuple[str, ...]
    ordered_handoffs: tuple[dict[str, str], ...]
    tool_requirements: tuple[dict[str, object], ...]
    workspace_policy_ref: ArtifactRef
    clarification_policy: dict[str, object]


class CaptainCreationJobV3Store(Protocol):
    def record_creation_job(self, job: CreationJobV3) -> CreationJobV3: ...

    def creation_job_for(self, job: object, attempt: int) -> CreationJobV3: ...

    def record_accepted_result(self, result: CreationResultV2) -> CreationResultV2: ...

    def accepted_result_for(self, job: object, attempt: int) -> CreationResultV2: ...

    def record_assignment(
        self, assignment: FactoryBuildAssignmentV2
    ) -> FactoryBuildAssignmentV2: ...

    def assignment_for(self, job: object, attempt: int) -> FactoryBuildAssignmentV2: ...

    def record_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2: ...

    def effect_for(self, job: object, attempt: int) -> FactoryBuildEffectV2: ...


class FilesystemCaptainCreationJobV3Store:
    """Captain-owned immutable V3 mapping/readback state, durable across restart."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        if ".captain-cook" not in {part.casefold() for part in self._root.parts}:
            raise ValueError("V3 authority store must use the .captain-cook namespace")
        self._root.mkdir(parents=True, exist_ok=True)

    def record_creation_job(self, job: CreationJobV3) -> CreationJobV3:
        return self._record(
            job.factory_job_id,
            job.attempt,
            "creation-job.json",
            job,
            CreationJobV3,
        )

    def creation_job_for(self, job: object, attempt: int) -> CreationJobV3:
        return self._read(
            getattr(job, "job_id", None), attempt, "creation-job.json", CreationJobV3
        )

    def record_accepted_result(self, result: CreationResultV2) -> CreationResultV2:
        return self._record(
            result.factory_job_id,
            result.attempt,
            "accepted-result.json",
            result,
            CreationResultV2,
        )

    def accepted_result_for(self, job: object, attempt: int) -> CreationResultV2:
        return self._read(
            getattr(job, "job_id", None), attempt, "accepted-result.json", CreationResultV2
        )

    def record_assignment(
        self, assignment: FactoryBuildAssignmentV2
    ) -> FactoryBuildAssignmentV2:
        return self._record(
            assignment.factory_job_id,
            assignment.attempt,
            "build-assignment.json",
            assignment,
            FactoryBuildAssignmentV2,
        )

    def assignment_for(self, job: object, attempt: int) -> FactoryBuildAssignmentV2:
        return self._read(
            getattr(job, "job_id", None),
            attempt,
            "build-assignment.json",
            FactoryBuildAssignmentV2,
        )

    def record_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2:
        return self._record(
            effect.factory_job_id,
            effect.attempt,
            "build-effect.json",
            effect,
            FactoryBuildEffectV2,
        )

    def effect_for(self, job: object, attempt: int) -> FactoryBuildEffectV2:
        return self._read(
            getattr(job, "job_id", None),
            attempt,
            "build-effect.json",
            FactoryBuildEffectV2,
        )

    def _record(self, job_id, attempt, name, value, model):
        path = self._path(job_id, attempt, name)
        content = json.dumps(
            value.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise FactoryDispatchError("Captain V3 durable authority replay conflicts")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return model.model_validate_json(content)

    def _read(self, job_id, attempt, name, model):
        path = self._path(job_id, attempt, name)
        try:
            value = model.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise FactoryDispatchError("Captain V3 durable authority is unavailable or invalid") from exc
        if value.factory_job_id != job_id or value.attempt != attempt:
            raise FactoryDispatchError("Captain V3 durable authority identity drift")
        return value

    def _path(self, job_id: object, attempt: int, name: str) -> Path:
        if not isinstance(job_id, UUID) or not isinstance(attempt, int) or not 1 <= attempt <= 5:
            raise FactoryDispatchError("Captain V3 durable authority identity is invalid")
        return self._root / str(job_id) / f"attempt-{attempt}" / name


class CaptainCreationJobV3Mapper:
    """Map only an active digest-bound Captain lease and compiled authority."""

    def __init__(
        self,
        *,
        authority: Callable[[FactoryDispatch], CaptainCreationJobV3Authority],
        released_skill: Callable[[object, FactorySkillStep], ReleasedHermesSkill],
        store: CaptainCreationJobV3Store,
    ) -> None:
        self._authority = authority
        self._released_skill = released_skill
        self._store = store

    def map(self, request: FactoryDispatch) -> CreationJobV3:
        if request.action.kind is not FactoryActionKind.SUBMIT_FORGE_JOB:
            raise FactoryDispatchError("V3 creation job requires Captain's submit-forge action")
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("V3 mapper requires a Captain V3 factory job")
        if request.role is not None or request.lease is None:
            raise FactoryDispatchError("V3 creation job requires the active factory lease")
        lease = request.lease
        if (
            lease.role is not FactoryRole.TOOL_INTEGRATOR
            or lease.job_id != request.job.job_id
            or lease.correlation_id != request.job.correlation_id
            or lease.subject_version != request.job.subject_version
            or lease.attempt != request.action.attempt
        ):
            raise FactoryDispatchError("V3 creation job lease identity is stale or unbound")
        authority = self._authority(request)
        creation_material = "\x1f".join(
            (
                request.job.required_capability,
                str(request.job.subject_version),
                request.job.input_ref.sha256,
                request.job.compiled_spec_ref.sha256,
                request.job.dependency_graph_ref.sha256,
                *request.job.acceptance_assertion_ids,
            )
        )
        expected_creation_key = "factory-create-" + hashlib.sha256(
            creation_material.encode("utf-8")
        ).hexdigest()
        if (
            authority.command_id != request.job.event_id
            or authority.capability_resolution.kind != "create"
            or authority.capability_resolution.creation_key != expected_creation_key
            or authority.compiled_spec_ref != request.job.compiled_spec_ref
        ):
            raise FactoryDispatchError(
                "V3 creation job requires the exact demonstrated Captain capability miss"
            )
        lease_bytes = json.dumps(
            lease.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        lease_sha256 = hashlib.sha256(lease_bytes).hexdigest()
        if authority.lease_ref.sha256 != lease_sha256:
            raise FactoryDispatchError("V3 creation job lease digest is not bound to the active lease")
        released = self._released_skill(request.job, FactorySkillStep.BRIEF_CODEX)
        creation_job_id = uuid5(
            request.job.job_id,
            f"minibook.creation-job.v3:{request.action.attempt}",
        )
        idempotency_key = hashlib.sha256(
            "\x1f".join(
                (
                    str(request.job.job_id),
                    str(request.job.subject_version),
                    str(request.action.attempt),
                    request.job.input_ref.sha256,
                    request.job.compiled_spec_ref.sha256,
                    authority.blueprint_ref.sha256,
                    authority.tool_plan_ref.sha256,
                    lease_sha256,
                )
            ).encode("utf-8")
        ).hexdigest()
        mapped = CreationJobV3.model_validate(
            {
                "schema": "minibook.creation-job.v3",
                "creation_job_id": creation_job_id,
                "factory_job_id": request.job.job_id,
                "command_id": authority.command_id,
                "correlation_id": request.job.correlation_id,
                "causation_id": request.job.event_id,
                "subject_version": request.job.subject_version,
                "attempt": request.action.attempt,
                "idempotency_key": idempotency_key,
                "input_ref": request.job.input_ref.model_dump(mode="json"),
                "compiled_spec_ref": request.job.compiled_spec_ref.model_dump(mode="json"),
                "dependency_graph_ref": request.job.dependency_graph_ref.model_dump(mode="json"),
                "released_skill": {
                    "skill_id": released.skill_id,
                    "version": released.version,
                    "content_ref": released.content_ref.model_dump(mode="json"),
                    "content_sha256": released.content_sha256,
                },
                "public_assertion_ids": request.job.acceptance_assertion_ids,
                "deadline_at": request.job.deadline_at,
                "lease_ref": authority.lease_ref.model_dump(mode="json"),
                "lease_sha256": lease_sha256,
                "blueprint_ref": authority.blueprint_ref.model_dump(mode="json"),
                "tool_plan_ref": authority.tool_plan_ref.model_dump(mode="json"),
                "required_agent_ids": authority.required_agent_ids,
                "ordered_handoffs": authority.ordered_handoffs,
                "tool_requirements": authority.tool_requirements,
                "workspace_policy_ref": authority.workspace_policy_ref.model_dump(mode="json"),
                "clarification_policy": authority.clarification_policy,
                "required_output_contract": "minibook.creation-result.v2",
            }
        )
        return self._store.record_creation_job(mapped)

    def creation_job_for(self, job: object, attempt: int) -> CreationJobV3:
        return self._store.creation_job_for(job, attempt)


class CaptainCreationJobMapper:
    """Map one exact Captain-approved Codex brief to Minibook's boundary."""

    def __init__(self, *, evidence: CaptainForgeEvidencePort) -> None:
        self._evidence = evidence

    def map(self, request: FactoryDispatch) -> CreationJobV1:
        if request.action.kind is not FactoryActionKind.SUBMIT_FORGE_JOB:
            raise FactoryDispatchError("creation job requires Captain's submit-forge action")
        if request.role is not None or request.lease is not None:
            raise FactoryDispatchError("creation job must not receive a Hermes role lease")

        job = request.job
        attempt = request.action.attempt
        artifacts = self._evidence.workflow_artifacts(job.job_id)
        briefs = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodexBuildBriefV1)
            and _matches_dispatch(artifact, job, attempt)
        )
        builds = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodexBuildEvidenceV1)
            and _matches_dispatch(artifact, job, attempt)
        )
        if len(briefs) != 1 or len(builds) != 1:
            raise FactoryDispatchError(
                "creation job requires exactly one Captain inventory, Codex brief, and sealed build"
            )
        brief = briefs[0]
        build = builds[0]
        inventories = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodebaseInventoryV1)
            and _matches_job_identity(artifact, job)
            and artifact.attempt <= attempt
            and artifact.artifact_ref in brief.context_refs
        )
        if len(inventories) != 1:
            raise FactoryDispatchError(
                "creation job requires exactly one Captain inventory, Codex brief, and sealed build"
            )
        inventory = inventories[0]
        revisions = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CandidateRevisionV1)
            and _matches_dispatch(artifact, job, attempt)
        )
        if attempt == 1:
            if revisions:
                raise FactoryDispatchError(
                    "initial creation job must not contain retry revision evidence"
                )
            expected_brief_input = inventory.artifact_ref
        else:
            if len(revisions) != 1:
                raise FactoryDispatchError(
                    "retry creation job requires exactly one Captain candidate revision"
                )
            expected_brief_input = revisions[0].artifact_ref

        if inventory.artifact_ref not in brief.context_refs:
            raise FactoryDispatchError("Codex brief is not bound to the Captain inventory")

        assignment = brief.build_assignment
        receipt = build.build_receipt
        released = self._evidence.released_for(job, FactorySkillStep.BRIEF_CODEX)
        invoked = brief.invocation.released_skill
        if released != invoked:
            raise FactoryDispatchError("Codex brief does not use Captain's released skill")
        if (
            not _same_artifact_ref(
                brief.invocation.input_ref,
                expected_brief_input,
            )
            or not _same_artifact_ref(assignment.compiled_spec_ref, job.compiled_spec_ref)
            or not _same_artifact_ref(
                assignment.dependency_graph_ref, job.dependency_graph_ref
            )
            or assignment.deadline_at != job.deadline_at
            or tuple(assignment.public_assertion_ids)
            != tuple(job.acceptance_assertion_ids)
        ):
            raise FactoryDispatchError("Codex assignment does not match the dispatched job")
        if (
            build.invocation.input_ref != brief.artifact_ref
            or not _same_artifact_ref(receipt.build_brief_ref, brief.artifact_ref)
            or receipt.assignment_id != assignment.assignment_id
            or receipt.creation_job_id != assignment.creation_job_id
            or receipt.idempotency_key != assignment.idempotency_key
            or receipt.workspace_ref != assignment.workspace_ref
        ):
            raise FactoryDispatchError(
                "Captain-sealed Codex build does not match the build assignment"
            )

        return CreationJobV2.model_validate(
            {
                "schema": "minibook.creation-job.v2",
                "creation_job_id": assignment.creation_job_id,
                "factory_job_id": job.job_id,
                "correlation_id": job.correlation_id,
                "causation_id": brief.invocation_id,
                "subject_version": job.subject_version,
                "attempt": attempt,
                "idempotency_key": assignment.idempotency_key,
                "input_ref": job.input_ref.model_dump(mode="json"),
                "compiled_spec_ref": assignment.compiled_spec_ref.model_dump(mode="json"),
                "dependency_graph_ref": assignment.dependency_graph_ref.model_dump(mode="json"),
                "released_skill": {
                    "skill_id": released.skill_id,
                    "version": released.version,
                    "content_ref": released.content_ref.model_dump(mode="json"),
                    "content_sha256": released.content_sha256,
                },
                "public_assertion_ids": assignment.public_assertion_ids,
                "deadline_at": assignment.deadline_at,
                "source_archive_ref": receipt.source_archive_ref.model_dump(
                    mode="json"
                ),
                "codex_build_receipt_ref": build.build_receipt_ref.model_dump(
                    mode="json"
                ),
                "codex_build_receipt": receipt.model_dump(
                    mode="json", by_alias=True
                ),
            }
        )

    def build_skill_receipt(
        self,
        request: FactoryDispatch,
        creation_job: CreationJobV1,
    ) -> ForgeBuildSkillUsageReceiptV1:
        """Derive one Forge receipt only from Captain-validated Hermes artifacts."""

        if self.map(request) != creation_job:
            raise FactoryDispatchError(
                "Forge skill receipt does not match the mapped creation job"
            )
        artifacts = self._evidence.workflow_artifacts(request.job.job_id)
        inventories = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodebaseInventoryV1)
            and _matches_dispatch(artifact, request.job, request.action.attempt)
        )
        briefs = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodexBuildBriefV1)
            and _matches_dispatch(artifact, request.job, request.action.attempt)
        )
        builds = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodexBuildEvidenceV1)
            and _matches_dispatch(artifact, request.job, request.action.attempt)
        )
        if len(inventories) != 1 or len(briefs) != 1 or len(builds) != 1:
            raise FactoryDispatchError(
                "Forge skill receipt requires exact inventory, brief, and build evidence"
            )
        inventory = inventories[0]
        brief = briefs[0]
        build = builds[0]
        evidence_refs_by_identity = {
            (reference.uri, reference.sha256, reference.media_type): reference
            for reference in (
                inventory.artifact_ref,
                brief.artifact_ref,
                build.artifact_ref,
                build.build_receipt_ref,
                *inventory.evidence_refs,
                *brief.evidence_refs,
            )
        }
        evidence_refs = tuple(
            reference.model_dump(mode="json")
            for reference in evidence_refs_by_identity.values()
        )
        return ForgeBuildSkillUsageReceiptV1(
            producer="hermes",
            outcome="fulfilled",
            creation_job_id=creation_job.creation_job_id,
            factory_job_id=creation_job.factory_job_id,
            correlation_id=creation_job.correlation_id,
            subject_version=creation_job.subject_version,
            attempt=creation_job.attempt,
            idempotency_key=creation_job.idempotency_key,
            released_skill=creation_job.released_skill,
            public_assertion_ids=creation_job.public_assertion_ids,
            evidence_refs=evidence_refs,
        )


def _same_artifact_ref(left: object, right: object) -> bool:
    left_dump = getattr(left, "model_dump", None)
    right_dump = getattr(right, "model_dump", None)
    if not callable(left_dump) or not callable(right_dump):
        return False
    return left_dump(mode="json") == right_dump(mode="json")


def _matches_dispatch(artifact: object, job: object, attempt: int) -> bool:
    return (
        _matches_job_identity(artifact, job)
        and getattr(artifact, "attempt", None) == attempt
    )


def _matches_job_identity(artifact: object, job: object) -> bool:
    return (
        getattr(artifact, "job_id", None) == getattr(job, "job_id", None)
        and getattr(artifact, "correlation_id", None)
        == getattr(job, "correlation_id", None)
        and getattr(artifact, "subject_version", None)
        == getattr(job, "subject_version", None)
    )


@dataclass(frozen=True)
class MinibookForgeHttpSettings:
    base_url: str
    api_key: str
    request_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.25
    max_polls: int = 120


class MinibookForgeHttpClient(MinibookForgePort):
    def __init__(
        self,
        *,
        mapper: CreationJobMapper,
        settings: MinibookForgeHttpSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._mapper = mapper
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.api_key}"}

    async def submit(self, request: FactoryDispatch) -> CreationSubmissionReceipt:
        job = self._mapper.map(request)
        response = await self._client.post(
            f"{self._settings.base_url}/api/v1/creation-jobs",
            json=job.model_dump(mode="json", by_alias=True),
            headers=self._headers,
        )
        if response.status_code not in {200, 202}:
            raise FactoryDispatchError(f"Minibook creation submission failed ({response.status_code})")
        return CreationSubmissionReceipt.model_validate(response.json())

    async def status(self, creation_job_id: UUID) -> CreationProgressV1:
        response = await self._client.get(
            f"{self._settings.base_url}/api/v1/creation-jobs/{creation_job_id}",
            headers=self._headers,
        )
        if response.status_code != 200:
            raise FactoryDispatchError(f"Minibook creation status failed ({response.status_code})")
        return CreationProgressV1.model_validate(response.json())

    async def result(self, creation_job_id: UUID) -> CreationResultV1 | CreationResultV2:
        response = await self._client.get(
            f"{self._settings.base_url}/api/v1/creation-jobs/{creation_job_id}/result",
            headers=self._headers,
        )
        if response.status_code != 200:
            raise FactoryDispatchError(f"Minibook creation result failed ({response.status_code})")
        payload = response.json()
        model = (
            CreationResultV2
            if payload.get("schema") == "minibook.creation-result.v2"
            else CreationResultV1
        )
        return model.model_validate(payload)

    async def wait_for_result(self, creation_job_id: UUID) -> CreationResultV1 | CreationResultV2:
        for _ in range(self._settings.max_polls):
            progress = await self.status(creation_job_id)
            if progress.status in {"succeeded", "failed", "blocked", "cancelled"}:
                return await self.result(creation_job_id)
            await asyncio.sleep(self._settings.poll_interval_seconds)
        raise FactoryDispatchError("Minibook creation polling limit exceeded")


@dataclass(frozen=True)
class MinibookForgeSettings:
    python_executable: str = "python"
    swarm_script: Path = Path("minibook/autogen_swarm.py")
    working_directory: Path = Path(".")
    max_runtime_seconds: int = 1800
    artifact_root: Path = Path(".captain-cook/minibook-creation-cas")


class MinibookSwarmForge(MinibookForgePort):
    """Start an existing Minibook pipeline without granting it Captain authority."""

    def __init__(
        self,
        *,
        materializer: FactoryInputMaterializer,
        mapper: CreationJobMapper,
        skill_receipts: ForgeBuildSkillReceiptProvider,
        settings: MinibookForgeSettings = MinibookForgeSettings(),
    ) -> None:
        self._materializer = materializer
        self._mapper = mapper
        self._skill_receipts = skill_receipts
        self._settings = settings

    async def submit(self, request: FactoryDispatch) -> CreationResultV1:
        if request.role is not None or request.lease is not None:
            raise FactoryDispatchError("Minibook Forge must not receive a Hermes role lease")
        creation_job = self._mapper.map(request)
        skill_receipt = self._skill_receipts.build_skill_receipt(
            request,
            creation_job,
        )
        materialized_ref = (
            creation_job.source_archive_ref
            if isinstance(creation_job, CreationJobV2)
            else request.job.input_ref
        )
        input_path = self._materializer.materialize(materialized_ref)
        if not input_path.is_file():
            raise FactoryDispatchError("factory input artifact did not materialize to a file")
        if self._settings.max_runtime_seconds <= 0:
            raise FactoryDispatchError("Minibook Forge runtime limit must be positive")
        working_directory = self._settings.working_directory.resolve()
        if not working_directory.is_dir():
            raise FactoryDispatchError("Minibook Forge working directory is unavailable")
        artifact_root = self._settings.artifact_root
        if not artifact_root.is_absolute():
            artifact_root = working_directory / artifact_root
        artifact_root = artifact_root.resolve()
        if ".captain-cook" not in {part.casefold() for part in artifact_root.parts}:
            raise FactoryDispatchError(
                "Minibook Forge artifact root must use the .captain-cook namespace"
            )
        with tempfile.TemporaryDirectory(
            prefix=f"captain-forge-{creation_job.creation_job_id}-a{creation_job.attempt}-",
            dir=working_directory,
        ) as temporary:
            run_directory = Path(temporary)
            creation_job_path = run_directory / "creation-job.json"
            skill_receipt_path = run_directory / "forge-skill-usage-receipt.json"
            result_path = run_directory / "creation-result.json"
            creation_job_path.write_text(
                json.dumps(
                    creation_job.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            skill_receipt_path.write_text(
                json.dumps(
                    skill_receipt.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            try:
                source_option = (
                    "--source-archive-file"
                    if isinstance(creation_job, CreationJobV2)
                    else "--input-file"
                )
                process = await asyncio.create_subprocess_exec(
                    self._settings.python_executable,
                    str(self._settings.swarm_script),
                    source_option,
                    str(input_path),
                    "--creation-job-file",
                    str(creation_job_path),
                    "--skill-usage-receipt-file",
                    str(skill_receipt_path),
                    "--non-interactive",
                    "--max-runtime-seconds",
                    str(self._settings.max_runtime_seconds),
                    "--result-file",
                    str(result_path),
                    "--artifact-root",
                    str(artifact_root),
                    cwd=str(working_directory),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(
                    process.communicate(), timeout=self._settings.max_runtime_seconds
                )
            except FileNotFoundError as exc:
                raise FactoryDispatchError(
                    "Minibook Forge executable or script is unavailable"
                ) from exc
            except asyncio.TimeoutError as exc:
                raise FactoryDispatchError(
                    "Minibook Forge exceeded its runtime limit"
                ) from exc
            if process.returncode != 0:
                raise FactoryDispatchError("Minibook Forge process failed")
            if not result_path.is_file():
                raise FactoryDispatchError("Minibook Forge did not write a creation result")
            try:
                result = CreationResultV1.model_validate_json(
                    result_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise FactoryDispatchError(
                    "Minibook Forge wrote an invalid creation result"
                ) from exc
            if (
                result.creation_job_id != creation_job.creation_job_id
                or result.correlation_id != creation_job.correlation_id
                or result.subject_version != creation_job.subject_version
                or result.attempt != creation_job.attempt
            ):
                raise FactoryDispatchError(
                    "Minibook Forge result does not match the submitted creation job"
                )
            return result

    async def status(self, creation_job_id):
        raise FactoryDispatchError("offline Minibook Forge has no status endpoint")

    async def result(self, creation_job_id):
        raise FactoryDispatchError("offline Minibook Forge returns its result from submit")
