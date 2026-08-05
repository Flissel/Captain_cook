from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from minibook.swarm.contracts import CreationJobV1, CreationResultV1
from minibook.swarm.job_store import CreationJobStore
from minibook.swarm.runner import CreationRunner, StepOutcome
from minibook.swarm.scheduler import CreationScheduler


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"


def creation_job(*, creation_job_id: UUID | None = None) -> CreationJobV1:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if creation_job_id is not None:
        payload["creation_job_id"] = str(creation_job_id)
    return CreationJobV1.model_validate(payload)


def _successful_result(job: CreationJobV1) -> CreationResultV1:
    reference = {
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
        package_manifest_ref=reference,
        skill_usage_receipt_ref=reference,
    )


class SnapshotPipeline:
    steps = ("inspect",)
    effectful_steps = frozenset()

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []

    async def run_step(
        self,
        job: CreationJobV1,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        del step, effect_key, accepted_effect
        self.snapshots.append(prior_snapshot)
        assert prior_snapshot["creation_job_id"] == str(job.creation_job_id)
        return StepOutcome(snapshot=prior_snapshot | {"inspected": True})

    def assemble_result(
        self, job: CreationJobV1, snapshot: dict[str, Any]
    ) -> CreationResultV1:
        assert snapshot["inspected"] is True
        return _successful_result(job)


class ControlledPipeline(SnapshotPipeline):
    def __init__(
        self,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        failure: Exception | None = None,
        concurrency: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self.entered = entered
        self.release = release
        self.failure = failure
        self.concurrency = concurrency
        self.calls = 0

    async def run_step(
        self,
        job: CreationJobV1,
        step: str,
        prior_snapshot: dict[str, Any],
        effect_key: str,
        accepted_effect: dict[str, Any] | None,
    ) -> StepOutcome:
        self.calls += 1
        if self.concurrency is not None:
            self.concurrency["active"] += 1
            self.concurrency["maximum"] = max(
                self.concurrency["maximum"], self.concurrency["active"]
            )
        try:
            if self.entered is not None:
                self.entered.set()
            if self.release is not None:
                await self.release.wait()
            if self.failure is not None:
                raise self.failure
            return await super().run_step(
                job, step, prior_snapshot, effect_key, accepted_effect
            )
        finally:
            if self.concurrency is not None:
                self.concurrency["active"] -= 1


@pytest.mark.asyncio
async def test_runner_receives_an_initial_snapshot_bound_to_the_job(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    pipeline = SnapshotPipeline()

    result = await CreationRunner(store, pipeline).run_slice(job.creation_job_id)

    assert result.status == "succeeded"
    assert pipeline.snapshots == [{"creation_job_id": str(job.creation_job_id)}]


def test_store_lists_only_resumable_jobs_in_submission_order(tmp_path: Path) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    queued = creation_job(creation_job_id=uuid4())
    running = creation_job(creation_job_id=uuid4())
    terminal = creation_job(creation_job_id=uuid4())
    for job in (queued, running, terminal):
        store.submit(job)
    store.complete_step(
        running.creation_job_id,
        "inspect",
        "effect",
        {"creation_job_id": str(running.creation_job_id)},
    )
    store.finish(_successful_result(terminal))

    assert store.resumable_job_ids() == (
        queued.creation_job_id,
        running.creation_job_id,
    )


@pytest.mark.asyncio
async def test_scheduler_starts_resumable_jobs_and_runs_only_one_at_a_time(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    first = creation_job(creation_job_id=uuid4())
    second = creation_job(creation_job_id=uuid4())
    store.submit(first)
    store.submit(second)
    release = asyncio.Event()
    concurrency = {"active": 0, "maximum": 0}
    pipelines: dict[UUID, ControlledPipeline] = {}

    def runner_for(job_id: UUID) -> CreationRunner:
        pipeline = ControlledPipeline(release=release, concurrency=concurrency)
        pipelines[job_id] = pipeline
        return CreationRunner(store, pipeline)

    scheduler = CreationScheduler(store=store, runner_for=runner_for)
    await scheduler.start()
    await asyncio.sleep(0)
    release.set()
    await scheduler.join()
    await scheduler.stop()

    assert set(pipelines) == {first.creation_job_id, second.creation_job_id}
    assert concurrency["maximum"] == 1
    assert store.result(first.creation_job_id).status == "succeeded"
    assert store.result(second.creation_job_id).status == "succeeded"


@pytest.mark.asyncio
async def test_sync_schedule_is_thread_safe_and_deduplicates_pending_job(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    entered = asyncio.Event()
    release = asyncio.Event()
    pipeline = ControlledPipeline(entered=entered, release=release)
    scheduler = CreationScheduler(
        store=store,
        runner_for=lambda job_id: CreationRunner(store, pipeline),
    )
    await scheduler.start(resume=False)

    threads = [
        threading.Thread(target=scheduler.schedule, args=(job.creation_job_id,))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    await entered.wait()
    release.set()
    await scheduler.join()
    await scheduler.stop()

    assert pipeline.calls == 1


@pytest.mark.asyncio
async def test_scheduler_persists_a_redacted_typed_failure_from_pipeline_exception(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    pipeline = ControlledPipeline(
        failure=RuntimeError("authorization=must-not-be-persisted")
    )
    scheduler = CreationScheduler(
        store=store,
        runner_for=lambda job_id: CreationRunner(store, pipeline),
    )
    await scheduler.start()
    await scheduler.join()
    await scheduler.stop()

    result = store.result(job.creation_job_id)
    assert result is not None
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == "internal_error"
    assert result.failure.summary == "creation step failed"
    assert result.failure.exception_type == "RuntimeError"
    assert "authorization" not in result.model_dump_json().lower()


@pytest.mark.asyncio
async def test_scheduler_shutdown_cancels_the_worker_without_terminalizing_the_job(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    job = creation_job()
    store.submit(job)
    entered = asyncio.Event()
    pipeline = ControlledPipeline(entered=entered, release=asyncio.Event())
    scheduler = CreationScheduler(
        store=store,
        runner_for=lambda job_id: CreationRunner(store, pipeline),
    )
    await scheduler.start()
    await entered.wait()

    await scheduler.stop()

    assert store.result(job.creation_job_id) is None
    assert store.resumable_job_ids() == (job.creation_job_id,)


def _reload_minibook_main() -> object:
    sys.modules.pop("minibook.src.main", None)
    return importlib.import_module("minibook.src.main")


def test_main_does_not_construct_creation_runtime_without_explicit_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIBOOK_CREATION_DB", raising=False)

    module = _reload_minibook_main()

    assert module._creation_store is None
    assert module._creation_scheduler is None


def test_main_constructs_scheduler_only_for_explicit_creation_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_db = tmp_path / "creation.sqlite3"
    monkeypatch.setenv("MINIBOOK_CREATION_DB", str(creation_db))

    module = _reload_minibook_main()

    assert isinstance(module._creation_store, CreationJobStore)
    assert isinstance(module._creation_scheduler, CreationScheduler)
    assert module._creation_store.path == creation_db
