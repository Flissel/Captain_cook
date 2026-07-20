"""Concrete submit adapter for Minibook's existing SwarmPipeline CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError, MinibookForgePort
from agenten.agent_runtime.contracts import ArtifactRef


class FactoryInputMaterializer(Protocol):
    def materialize(self, reference: ArtifactRef) -> Path:
        """Resolve a Captain artifact into a local, read-only input file path."""


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

    async def submit(self, request: FactoryDispatch) -> None:
        if request.role is not None or request.lease is not None:
            raise FactoryDispatchError("Minibook Forge must not receive a Hermes role lease")
        input_path = self._materializer.materialize(request.job.input_ref)
        if not input_path.is_file():
            raise FactoryDispatchError("factory input artifact did not materialize to a file")
        if self._settings.max_runtime_seconds <= 0:
            raise FactoryDispatchError("Minibook Forge runtime limit must be positive")
        try:
            await asyncio.create_subprocess_exec(
                self._settings.python_executable,
                str(self._settings.swarm_script),
                "--input-file",
                str(input_path),
                "--non-interactive",
                "--max-runtime-seconds",
                str(self._settings.max_runtime_seconds),
                cwd=str(self._settings.working_directory),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise FactoryDispatchError("Minibook Forge executable or script is unavailable") from exc
