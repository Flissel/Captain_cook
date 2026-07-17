from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenten.planning.run_models import CaptainRunState, CaptainRunStatus
from agenten.planning.run_store import CaptainRunStoreError, JsonCaptainRunStore
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


def run_state(run_id: str = "run-1") -> CaptainRunState:
    batch = WorkBatch(
        batch_id="batch-1",
        title="Batch",
        goal="Ship",
        subtask_ids=["sub-1"],
        target="external",
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
    )
    holdout = HoldoutSuite(
        batch_id="batch-1",
        cases=[ExampleCase(case_id="hidden", input={"value": 2})],
    )
    return CaptainRunState(
        run_id=run_id,
        project_id="project-abc",
        project_digest="a" * 64,
        status=CaptainRunStatus.RELEASING,
        batches=[batch],
        holdouts=[holdout],
    )


@pytest.mark.asyncio
async def test_json_run_store_round_trips_with_atomic_replace(tmp_path: Path) -> None:
    store = JsonCaptainRunStore(tmp_path / "runs")
    state = run_state()

    await store.save(state)

    assert await store.load("run-1") == state
    assert (tmp_path / "runs" / "run-1.json").exists()
    assert not (tmp_path / "runs" / "run-1.json.tmp").exists()


@pytest.mark.asyncio
async def test_json_run_store_rejects_unsafe_run_id_and_corrupt_state(tmp_path: Path) -> None:
    store = JsonCaptainRunStore(tmp_path / "runs")
    with pytest.raises(ValueError, match="run_id"):
        await store.load("../escape")

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "broken.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(CaptainRunStoreError, match="broken"):
        await store.load("broken")


@pytest.mark.asyncio
async def test_separate_store_instances_serialize_the_same_run_id(tmp_path: Path) -> None:
    first = JsonCaptainRunStore(tmp_path / "runs")
    second = JsonCaptainRunStore(tmp_path / "runs")
    second_acquired = asyncio.Event()

    async def acquire_second() -> None:
        async with second.lock("run-1"):
            second_acquired.set()

    async with first.lock("run-1"):
        contender = asyncio.create_task(acquire_second())
        await asyncio.sleep(0.05)
        assert not second_acquired.is_set()

    await asyncio.wait_for(contender, timeout=2)
    assert second_acquired.is_set()


def test_run_state_rejects_batch_holdout_or_checkpoint_drift() -> None:
    state = run_state()
    invalid_holdout_state = state.model_dump()
    invalid_holdout_state["holdouts"][0]["batch_id"] = "other"
    with pytest.raises(ValueError, match="identical ordering"):
        CaptainRunState.model_validate(invalid_holdout_state)

    with pytest.raises(ValueError, match="released_batch_ids"):
        CaptainRunState.model_validate(
            state.model_dump() | {"released_batch_ids": ["unknown"]}
        )
