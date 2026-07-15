from pathlib import Path

import pytest

from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate
from agenten.llm.model_client import build_replay_model_client
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment
from agenten.planning.factory import build_captain_pipeline
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
)


@pytest.mark.asyncio
async def test_factory_runs_captain_from_description_to_released_contracts(tmp_path: Path) -> None:
    decomposition = DecomposeResponse(
        subproblems=[
            SubproblemCandidate(
                description="Produce the deliverable",
                capability_tags=["delivery"],
                atomic=True,
            )
        ]
    )
    alignment = AlignmentPlan(
        batches=[BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["sub-01"])]
    )
    enrichment = BatchEnrichment(
        goal="Produce a verified deliverable",
        capability_tags=["delivery"],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
        golden_cases=[ExampleCase(case_id="visible", input={"mode": "known"})],
        holdout_cases=[ExampleCase(case_id="hidden", input={"mode": "novel"})],
    )
    client = build_replay_model_client(
        [
            decomposition.model_dump_json(),
            alignment.model_dump_json(),
            enrichment.model_dump_json(),
        ]
    )
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path,
        target="external",
        known_capability_tags=["delivery"],
    )

    result = await pipeline.run("Build something useful")

    assert [batch.batch_id for batch in result.batches] == ["delivery"]
    assert (tmp_path / "batches" / "delivery.json").exists()
    assert (tmp_path / "holdouts" / "delivery.json").exists()
