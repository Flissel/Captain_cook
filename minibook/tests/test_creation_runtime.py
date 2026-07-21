from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from minibook.swarm.contracts import CreationJobV1, CreationResultV1
from minibook.swarm.job_store import CreationJobStore
from minibook.swarm.runner import StepOutcome


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"


def creation_job(**updates: object) -> CreationJobV1:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(updates)
    return CreationJobV1.model_validate(payload)


class OneStepPipeline:
    steps = ("build",)
    effectful_steps = frozenset()

    def __init__(self, calls: list[str], release: asyncio.Event | None = None) -> None:
        self.calls = calls
        self.release = release

    async def run_step(
        self,
        job: CreationJobV1,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        del job, effect_key, accepted_effect
        self.calls.append(step)
        if self.release is not None:
            await self.release.wait()
        return StepOutcome(snapshot=prior_snapshot | {"built": True})

    def assemble_result(
        self, job: CreationJobV1, snapshot: dict[str, Any]
    ) -> CreationResultV1:
        assert snapshot == {"built": True}
        ref = {
            "uri": "artifact://sha256/result",
            "sha256": "f" * 64,
            "media_type": "application/json",
        }
        return CreationResultV1(
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status="succeeded",
            package_manifest_ref=ref,
            skill_usage_receipt_ref=ref,
        )


class PipelineFactory:
    def __init__(self, calls: list[str], release: asyncio.Event | None = None) -> None:
        self.calls = calls
        self.release = release
        self.opened = 0
        self.closed = 0

    @asynccontextmanager
    async def open(self, job: CreationJobV1):
        del job
        self.opened += 1
        try:
            yield OneStepPipeline(self.calls, self.release)
        finally:
            self.closed += 1


async def _wait_for_result(store: CreationJobStore, job: CreationJobV1) -> CreationResultV1:
    for _ in range(100):
        result = store.result(job.creation_job_id)
        if result is not None:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("creation result was not persisted")


@pytest.mark.asyncio
async def test_background_runtime_resumes_queued_jobs_on_start(tmp_path: Path) -> None:
    from minibook.swarm.creation_runtime import BackgroundCreationRuntime

    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    calls: list[str] = []
    factory = PipelineFactory(calls)
    runtime = BackgroundCreationRuntime(store, factory)

    await runtime.start()
    result = await _wait_for_result(store, job)
    await runtime.stop()

    assert result.status == "succeeded"
    assert calls == ["build"]
    assert (factory.opened, factory.closed) == (1, 1)


@pytest.mark.asyncio
async def test_schedule_is_safe_from_fastapi_worker_thread(tmp_path: Path) -> None:
    from minibook.swarm.creation_runtime import BackgroundCreationRuntime

    store = CreationJobStore(tmp_path / "creation.sqlite3")
    calls: list[str] = []
    factory = PipelineFactory(calls)
    runtime = BackgroundCreationRuntime(store, factory)
    await runtime.start()
    job = creation_job()
    store.submit(job)

    thread = threading.Thread(target=runtime.schedule, args=(job.creation_job_id,))
    thread.start()
    thread.join()
    result = await _wait_for_result(store, job)
    await runtime.stop()

    assert result.status == "succeeded"
    assert calls == ["build"]


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_work_and_restart_resumes(tmp_path: Path) -> None:
    from minibook.swarm.creation_runtime import BackgroundCreationRuntime

    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    release = asyncio.Event()
    first = BackgroundCreationRuntime(store, PipelineFactory([], release))
    await first.start()
    for _ in range(100):
        if first.active_job_ids:
            break
        await asyncio.sleep(0.01)
    await first.stop()
    assert store.result(job.creation_job_id) is None

    calls: list[str] = []
    resumed = BackgroundCreationRuntime(store, PipelineFactory(calls))
    await resumed.start()
    result = await _wait_for_result(store, job)
    await resumed.stop()

    assert result.status == "succeeded"
    assert calls == ["build"]


def test_creation_runtime_is_not_constructed_without_opt_in() -> None:
    from minibook.swarm.creation_runtime import configured_creation_runtime

    assert configured_creation_runtime({}) is None


@pytest.mark.asyncio
async def test_production_factory_injects_real_session_agents_project_and_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minibook.swarm.creation_runtime import ProductionSwarmPipelineFactory
    from minibook.swarm.pipeline_adapter import ContentAddressedCreationArtifacts
    from minibook.swarm import constants
    from minibook.swarm.cost_budget import reserve_openai_chat_completion

    monkeypatch.setenv("CAPTAIN_FACTORY_MAX_COST_USD", "1.00")
    monkeypatch.setattr(constants, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(constants, "DEFAULT_MODEL", "gpt-4o-mini")

    calls: list[object] = []

    class Session:
        async def __aenter__(self):
            calls.append("session-open")
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback
            calls.append("session-close")

    async def setup_agents(session: Session) -> dict[str, object]:
        calls.append(("agents", session))
        calls.append(("budget", reserve_openai_chat_completion(
            payload={"messages": [{"role": "user", "content": "probe"}]},
            max_output_tokens=10,
        )))
        return {"SwarmManager": {"api_key": "not-persisted"}}

    async def setup_project(
        session: Session, agents: dict[str, object], name: str
    ) -> str:
        calls.append(("project", session, agents, name))
        return "project-1"

    def resolve_input(reference) -> str:
        calls.append(("input", reference.sha256))
        return "Build the requested AutoGen team"

    class Pipeline:
        def __init__(
            self,
            agents: dict[str, object],
            project_id: str,
            task: str,
            *,
            interactive: bool,
        ) -> None:
            calls.append(("pipeline", agents, project_id, task, interactive))

    factory = ProductionSwarmPipelineFactory(
        ContentAddressedCreationArtifacts(tmp_path / "artifacts"),
        session_factory=Session,
        setup_agents=setup_agents,
        setup_project=setup_project,
        pipeline_type=Pipeline,
        input_resolver=resolve_input,
    )

    async with factory.open(creation_job()) as adapter:
        assert adapter.steps

    assert calls[0] == "session-open"
    assert calls[-1] == "session-close"
    assert ("input", creation_job().input_ref.sha256) in calls
    assert any(isinstance(call, tuple) and call[0] == "budget" and call[1] > 0 for call in calls)
    assert any(
        isinstance(call, tuple)
        and call[0] == "pipeline"
        and call[3] == "Build the requested AutoGen team"
        and call[4] is False
        for call in calls
    )
