import json
from pathlib import Path

import pytest

from agenten.planning.release import JsonDirectoryReleaseClient, ReleaseConflictError
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


def make_release(goal: str = "Deliver safely") -> tuple[WorkBatch, HoldoutSuite]:
    batch = WorkBatch(
        batch_id="delivery",
        title="Delivery",
        goal=goal,
        subtask_ids=["s1"],
        target="external",
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
    )
    holdouts = HoldoutSuite(
        batch_id="delivery",
        cases=[ExampleCase(case_id="hidden", input={"secret_case": 42})],
    )
    return batch, holdouts


@pytest.mark.asyncio
async def test_json_release_keeps_build_visible_batch_and_holdouts_separate(tmp_path: Path) -> None:
    client = JsonDirectoryReleaseClient(tmp_path)
    batch, holdouts = make_release()

    await client.release(batch, holdouts)

    batch_payload = json.loads((tmp_path / "batches" / "delivery.json").read_text("utf-8"))
    holdout_payload = json.loads((tmp_path / "holdouts" / "delivery.json").read_text("utf-8"))
    assert "holdout" not in json.dumps(batch_payload)
    assert holdout_payload["cases"][0]["input"] == {"secret_case": 42}


@pytest.mark.asyncio
async def test_identical_release_is_idempotent(tmp_path: Path) -> None:
    client = JsonDirectoryReleaseClient(tmp_path)
    batch, holdouts = make_release()

    await client.release(batch, holdouts)
    first_content = (tmp_path / "batches" / "delivery.json").read_bytes()
    await client.release(batch, holdouts)

    assert (tmp_path / "batches" / "delivery.json").read_bytes() == first_content


@pytest.mark.asyncio
async def test_conflicting_release_never_overwrites_existing_batch(tmp_path: Path) -> None:
    client = JsonDirectoryReleaseClient(tmp_path)
    original, holdouts = make_release()
    changed, _ = make_release(goal="A conflicting goal")
    await client.release(original, holdouts)

    with pytest.raises(ReleaseConflictError, match="delivery"):
        await client.release(changed, holdouts)

    stored = json.loads((tmp_path / "batches" / "delivery.json").read_text("utf-8"))
    assert stored["goal"] == "Deliver safely"
