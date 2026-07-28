"""Concrete submit adapter for Minibook's existing SwarmPipeline CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from typing import Protocol
from uuid import UUID

import httpx

from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, MinibookForgePort
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationProgressV1,
    CreationResultV1,
    CreationSubmissionReceipt,
)
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    CodebaseInventoryV1,
    CodexBuildBriefV1,
    FactorySkillStep,
)
from agenten.agent_factory.state_machine import FactoryActionKind


class FactoryInputMaterializer(Protocol):
    def materialize(self, reference: ArtifactRef) -> Path:
        """Resolve a Captain artifact into a local, read-only input file path."""


class CreationJobMapper(Protocol):
    def map(self, request: FactoryDispatch) -> CreationJobV1: ...


class CaptainForgeEvidencePort(Protocol):
    """Read only Captain-owned evidence required to authorize a Forge job."""

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...

    def released_for(
        self,
        job: object,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


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
        inventories = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodebaseInventoryV1)
            and _matches_dispatch(artifact, job, attempt)
        )
        briefs = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, CodexBuildBriefV1)
            and _matches_dispatch(artifact, job, attempt)
        )
        if len(inventories) != 1 or len(briefs) != 1:
            raise FactoryDispatchError(
                "creation job requires exactly one Captain inventory and Codex brief"
            )
        inventory = inventories[0]
        brief = briefs[0]

        if inventory.artifact_ref not in brief.context_refs:
            raise FactoryDispatchError("Codex brief is not bound to the Captain inventory")

        assignment = brief.build_assignment
        released = self._evidence.released_for(job, FactorySkillStep.BRIEF_CODEX)
        invoked = brief.invocation.released_skill
        if released != invoked:
            raise FactoryDispatchError("Codex brief does not use Captain's released skill")
        if (
            not _same_artifact_ref(brief.invocation.input_ref, job.input_ref)
            or not _same_artifact_ref(assignment.compiled_spec_ref, job.compiled_spec_ref)
            or not _same_artifact_ref(
                assignment.dependency_graph_ref, job.dependency_graph_ref
            )
            or assignment.deadline_at != job.deadline_at
            or tuple(assignment.public_assertion_ids)
            != tuple(job.acceptance_assertion_ids)
        ):
            raise FactoryDispatchError("Codex assignment does not match the dispatched job")

        return CreationJobV1.model_validate(
            {
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
            }
        )


def _same_artifact_ref(left: object, right: object) -> bool:
    left_dump = getattr(left, "model_dump", None)
    right_dump = getattr(right, "model_dump", None)
    if not callable(left_dump) or not callable(right_dump):
        return False
    return left_dump(mode="json") == right_dump(mode="json")


def _matches_dispatch(artifact: object, job: object, attempt: int) -> bool:
    return (
        getattr(artifact, "job_id", None) == getattr(job, "job_id", None)
        and getattr(artifact, "correlation_id", None)
        == getattr(job, "correlation_id", None)
        and getattr(artifact, "subject_version", None)
        == getattr(job, "subject_version", None)
        and getattr(artifact, "attempt", None) == attempt
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

    async def result(self, creation_job_id: UUID) -> CreationResultV1:
        response = await self._client.get(
            f"{self._settings.base_url}/api/v1/creation-jobs/{creation_job_id}/result",
            headers=self._headers,
        )
        if response.status_code != 200:
            raise FactoryDispatchError(f"Minibook creation result failed ({response.status_code})")
        return CreationResultV1.model_validate(response.json())

    async def wait_for_result(self, creation_job_id: UUID) -> CreationResultV1:
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


class MinibookSwarmForge(MinibookForgePort):
    """Start an existing Minibook pipeline without granting it Captain authority."""

    def __init__(
        self,
        *,
        materializer: FactoryInputMaterializer,
        mapper: CreationJobMapper,
        settings: MinibookForgeSettings = MinibookForgeSettings(),
    ) -> None:
        self._materializer = materializer
        self._mapper = mapper
        self._settings = settings

    async def submit(self, request: FactoryDispatch) -> CreationResultV1:
        if request.role is not None or request.lease is not None:
            raise FactoryDispatchError("Minibook Forge must not receive a Hermes role lease")
        creation_job = self._mapper.map(request)
        input_path = self._materializer.materialize(request.job.input_ref)
        if not input_path.is_file():
            raise FactoryDispatchError("factory input artifact did not materialize to a file")
        if self._settings.max_runtime_seconds <= 0:
            raise FactoryDispatchError("Minibook Forge runtime limit must be positive")
        working_directory = self._settings.working_directory.resolve()
        if not working_directory.is_dir():
            raise FactoryDispatchError("Minibook Forge working directory is unavailable")
        with tempfile.TemporaryDirectory(
            prefix=f"captain-forge-{creation_job.creation_job_id}-a{creation_job.attempt}-",
            dir=working_directory,
        ) as temporary:
            run_directory = Path(temporary)
            creation_job_path = run_directory / "creation-job.json"
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
            try:
                process = await asyncio.create_subprocess_exec(
                    self._settings.python_executable,
                    str(self._settings.swarm_script),
                    "--input-file",
                    str(input_path),
                    "--creation-job-file",
                    str(creation_job_path),
                    "--non-interactive",
                    "--max-runtime-seconds",
                    str(self._settings.max_runtime_seconds),
                    "--result-file",
                    str(result_path),
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
