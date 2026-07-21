"""Concrete submit adapter for Minibook's existing SwarmPipeline CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
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


class FactoryInputMaterializer(Protocol):
    def materialize(self, reference: ArtifactRef) -> Path:
        """Resolve a Captain artifact into a local, read-only input file path."""


class CreationJobMapper(Protocol):
    def map(self, request: FactoryDispatch) -> CreationJobV1: ...


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
        settings: MinibookForgeSettings = MinibookForgeSettings(),
    ) -> None:
        self._materializer = materializer
        self._settings = settings

    async def submit(self, request: FactoryDispatch) -> CreationResultV1:
        if request.role is not None or request.lease is not None:
            raise FactoryDispatchError("Minibook Forge must not receive a Hermes role lease")
        input_path = self._materializer.materialize(request.job.input_ref)
        if not input_path.is_file():
            raise FactoryDispatchError("factory input artifact did not materialize to a file")
        if self._settings.max_runtime_seconds <= 0:
            raise FactoryDispatchError("Minibook Forge runtime limit must be positive")
        try:
            result_path = self._settings.working_directory / "creation-result.json"
            process = await asyncio.create_subprocess_exec(
                self._settings.python_executable,
                str(self._settings.swarm_script),
                "--input-file",
                str(input_path),
                "--non-interactive",
                "--max-runtime-seconds",
                str(self._settings.max_runtime_seconds),
                "--result-file",
                str(result_path),
                cwd=str(self._settings.working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(
                process.communicate(), timeout=self._settings.max_runtime_seconds
            )
        except FileNotFoundError as exc:
            raise FactoryDispatchError("Minibook Forge executable or script is unavailable") from exc
        except asyncio.TimeoutError as exc:
            raise FactoryDispatchError("Minibook Forge exceeded its runtime limit") from exc
        if process.returncode != 0:
            raise FactoryDispatchError("Minibook Forge process failed")
        if not result_path.is_file():
            raise FactoryDispatchError("Minibook Forge did not write a creation result")
        try:
            return CreationResultV1.model_validate_json(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("Minibook Forge wrote an invalid creation result") from exc

    async def status(self, creation_job_id):
        raise FactoryDispatchError("offline Minibook Forge has no status endpoint")

    async def result(self, creation_job_id):
        raise FactoryDispatchError("offline Minibook Forge returns its result from submit")
