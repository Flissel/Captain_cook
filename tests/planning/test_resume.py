from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    BatchEnrichment,
    CaptainPipeline,
)
from agenten.planning.run_models import (
    CaptainRunConflictError,
    CaptainRunStatus,
    PartialReleaseError,
)
from agenten.planning.run_store import JsonCaptainRunStore
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, ExampleCase


class FailOnceOnBatch:
    def __init__(self, batch_id: str) -> None:
        self._batch_id = batch_id
        self._failed = False
        self.calls: list[str] = []

    async def release(self, batch, holdouts) -> None:
        assert batch.batch_id == holdouts.batch_id
        self.calls.append(batch.batch_id)
        if batch.batch_id == self._batch_id and not self._failed:
            self._failed = True
            raise RuntimeError("injected release failure")


def pipeline_fixture(release, store: JsonCaptainRunStore, counters: dict[str, int]) -> CaptainPipeline:
    async def decompose(project_description):
        del project_description
        counters["decompose"] += 1
        from agenten.planning.captain_pipeline import PlannedSubtask

        return [
            PlannedSubtask(subtask_id="sub-1", description="First"),
            PlannedSubtask(subtask_id="sub-2", description="Second"),
        ]

    async def align(project_description, subtasks, feedback):
        del project_description, subtasks, feedback
        return AlignmentPlan(
            batches=[
                BatchDraft(batch_id="first", title="First", subtask_ids=["sub-1"]),
                BatchDraft(
                    batch_id="second",
                    title="Second",
                    subtask_ids=["sub-2"],
                    depends_on=["first"],
                ),
            ]
        )

    async def enrich(project_description, draft, subtasks):
        del project_description, subtasks
        return BatchEnrichment(
            goal=f"Deliver {draft.title}",
            acceptance_criteria=[
                AcceptanceAssertion(
                    assertion_id=f"{draft.batch_id}-done",
                    kind=AssertionKind.STATUS_EQUALS,
                    expected="succeeded",
                )
            ],
            holdout_cases=[
                ExampleCase(case_id=f"{draft.batch_id}-hidden", input={"value": draft.batch_id})
            ],
        )

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=release,
        run_store=store,
        target="external",
    )


@pytest.mark.asyncio
async def test_run_resumes_at_first_unreleased_batch(tmp_path: Path) -> None:
    release = FailOnceOnBatch("second")
    store = JsonCaptainRunStore(tmp_path / "runs")
    counters = {"decompose": 0}
    pipeline = pipeline_fixture(release, store, counters)

    with pytest.raises(PartialReleaseError) as failure:
        await pipeline.run("project", run_id="run-1")

    assert failure.value.released_batch_ids == ("first",)
    assert failure.value.failed_batch_id == "second"
    partial = await store.load("run-1")
    assert partial is not None
    assert partial.status is CaptainRunStatus.PARTIALLY_RELEASED
    assert partial.released_batch_ids == ("first",)

    resumed = await pipeline.run("project", run_id="run-1")

    assert resumed.batches[0].batch_id == "first"
    assert release.calls == ["first", "second", "second"]
    assert counters["decompose"] == 1
    completed = await store.load("run-1")
    assert completed is not None
    assert completed.status is CaptainRunStatus.RELEASED
    assert completed.released_batch_ids == ("first", "second")


@pytest.mark.asyncio
async def test_completed_run_is_idempotent_and_project_digest_is_fenced(tmp_path: Path) -> None:
    release = FailOnceOnBatch("never")
    store = JsonCaptainRunStore(tmp_path / "runs")
    counters = {"decompose": 0}
    pipeline = pipeline_fixture(release, store, counters)

    await pipeline.run("project", run_id="run-1")
    await pipeline.run("project", run_id="run-1")

    assert release.calls == ["first", "second"]
    assert counters["decompose"] == 1
    with pytest.raises(CaptainRunConflictError, match="different project"):
        await pipeline.run("different project", run_id="run-1")


@pytest.mark.asyncio
async def test_concurrent_resume_calls_do_not_duplicate_release(tmp_path: Path) -> None:
    class SlowRelease(FailOnceOnBatch):
        async def release(self, batch, holdouts) -> None:
            await asyncio.sleep(0)
            await super().release(batch, holdouts)

    release = SlowRelease("never")
    store = JsonCaptainRunStore(tmp_path / "runs")
    counters = {"decompose": 0}
    pipeline = pipeline_fixture(release, store, counters)

    await asyncio.gather(
        pipeline.run("project", run_id="run-1"),
        pipeline.run("project", run_id="run-1"),
    )

    assert release.calls == ["first", "second"]
    assert counters["decompose"] == 1
