"""Opt-in, restart-safe scheduling for Minibook creation jobs."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from .contracts import CreationJobV1, CreationResultV1
from .job_store import CreationJobStore
from .pipeline_adapter import (
    ContentAddressedCreationArtifacts,
    ExportArtifactSnapshotter,
    SwarmPipelineAdapter,
    translate_creation_failure,
)
from .runner import CreationRunner, PipelineStepPort


class CreationPipelineFactory(Protocol):
    def open(
        self, job: CreationJobV1
    ) -> AbstractAsyncContextManager[PipelineStepPort]: ...


class BackgroundCreationRuntime:
    """Own background tasks while persistence remains the restart authority."""

    def __init__(
        self, store: CreationJobStore, pipeline_factory: CreationPipelineFactory
    ) -> None:
        self.store = store
        self.pipeline_factory = pipeline_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._stopping = False

    @property
    def active_job_ids(self) -> tuple[UUID, ...]:
        return tuple(self._tasks)

    async def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        for job_id in self.store.resumable_job_ids():
            self._spawn(job_id)

    def schedule(self, job_id: UUID) -> None:
        loop = self._loop
        if loop is None or self._stopping:
            raise RuntimeError("creation runtime is not accepting work")
        loop.call_soon_threadsafe(self._spawn, job_id)

    def _spawn(self, job_id: UUID) -> None:
        if self._stopping or job_id in self._tasks:
            return
        if self.store.result(job_id) is not None:
            return
        task = asyncio.create_task(self._run(job_id), name=f"minibook-creation-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda completed, identity=job_id: self._done(identity, completed))

    def _done(self, job_id: UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        if not task.cancelled():
            task.exception()

    async def _run(self, job_id: UUID) -> None:
        job = self.store.job(job_id)
        try:
            async with self.pipeline_factory.open(job) as pipeline:
                await CreationRunner(self.store, pipeline).run_slice(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.store.result(job_id) is not None:
                return
            failure = translate_creation_failure(exc)
            status = (
                "blocked"
                if failure.code in {"documentation_unavailable", "tool_unresolved"}
                else "failed"
            )
            self.store.finish(
                CreationResultV1(
                    creation_job_id=job.creation_job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=job.attempt,
                    status=status,
                    failure=failure,
                )
            )

    async def stop(self) -> None:
        self._stopping = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._loop = None


class ProductionSwarmPipelineFactory:
    """Build the existing SwarmPipeline with live Minibook agents and one session."""

    def __init__(
        self,
        artifacts: ContentAddressedCreationArtifacts,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        setup_agents: Callable[[Any], Any] | None = None,
        setup_project: Callable[[Any, dict[str, Any], str], Any] | None = None,
        pipeline_type: Callable[..., Any] | None = None,
        input_resolver: Callable[[Any], str] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self._session_factory = session_factory
        self._setup_agents = setup_agents
        self._setup_project = setup_project
        self._pipeline_type = pipeline_type
        self._input_resolver = input_resolver

    def _dependencies(self):
        if all(
            dependency is not None
            for dependency in (
                self._session_factory,
                self._setup_agents,
                self._setup_project,
                self._pipeline_type,
            )
        ):
            return (
                self._session_factory,
                self._setup_agents,
                self._setup_project,
                self._pipeline_type,
            )
        import aiohttp
        from minibook.autogen_swarm import setup_agents, setup_project
        from minibook.swarm.pipeline import SwarmPipeline

        return (
            self._session_factory or aiohttp.ClientSession,
            self._setup_agents or setup_agents,
            self._setup_project or setup_project,
            self._pipeline_type or SwarmPipeline,
        )

    @asynccontextmanager
    async def open(self, job: CreationJobV1) -> AsyncIterator[PipelineStepPort]:
        session_factory, setup_agents, setup_project, pipeline_type = self._dependencies()
        async with session_factory() as session:
            agents = await setup_agents(session)
            project_name = f"Factory: {job.creation_job_id}"
            project_id = await setup_project(session, agents, project_name)
            if self._input_resolver is None:
                task = self.artifacts.read(job.input_ref).decode("utf-8")
            else:
                task = self._input_resolver(job.input_ref)
            if not task.strip():
                raise ValueError("creation input artifact is empty")
            pipeline = pipeline_type(
                agents,
                project_id,
                task,
                interactive=False,
            )
            snapshotter = ExportArtifactSnapshotter(self.artifacts)

            def restore(snapshot):
                return snapshotter.restore(pipeline, snapshot)

            yield SwarmPipelineAdapter(
                restore,
                session=session,
                snapshotter=snapshotter,
            )


@dataclass(frozen=True)
class ConfiguredCreationRuntime:
    store: CreationJobStore
    runtime: BackgroundCreationRuntime


def configured_creation_runtime(
    environment: Mapping[str, str] | None = None,
    *,
    pipeline_factory: CreationPipelineFactory | None = None,
) -> ConfiguredCreationRuntime | None:
    environment = os.environ if environment is None else environment
    database = environment.get("MINIBOOK_CREATION_DB")
    if not database:
        return None
    database_path = Path(database)
    artifact_root = Path(
        environment.get(
            "MINIBOOK_CREATION_ARTIFACTS",
            str(database_path.with_suffix(database_path.suffix + ".artifacts")),
        )
    )
    store = CreationJobStore(database_path)
    factory = pipeline_factory or ProductionSwarmPipelineFactory(
        ContentAddressedCreationArtifacts(artifact_root)
    )
    return ConfiguredCreationRuntime(
        store=store,
        runtime=BackgroundCreationRuntime(store, factory),
    )
