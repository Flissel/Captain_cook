import pytest

from agenten.llm.plan_batches import make_llm_align, make_llm_enrich
from agenten.llm.model_client import build_replay_model_client
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment, PlannedSubtask
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
)


@pytest.mark.asyncio
async def test_align_uses_structured_output_and_forwards_validation_feedback() -> None:
    response = AlignmentPlan(
        batches=[BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["s1"])]
    )
    client = build_replay_model_client([response.model_dump_json()])
    align = make_llm_align(client)

    result = await align(
        "Build the project",
        [PlannedSubtask(subtask_id="s1", description="Deliver it")],
        "previous plan missed s1",
    )

    assert result == response


@pytest.mark.asyncio
async def test_enrich_returns_separated_golden_and_holdout_cases() -> None:
    response = BatchEnrichment(
        goal="Deliver a verified result",
        constraints=["No external assumptions"],
        capability_tags=["delivery"],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
        golden_cases=[ExampleCase(case_id="visible", input={"value": 1})],
        holdout_cases=[ExampleCase(case_id="hidden", input={"value": 2})],
    )
    client = build_replay_model_client([response.model_dump_json()])
    enrich = make_llm_enrich(client)

    result = await enrich(
        "Build the project",
        BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["s1"]),
        [PlannedSubtask(subtask_id="s1", description="Deliver it")],
    )

    assert result == response


@pytest.mark.asyncio
async def test_align_rejects_non_structured_model_output() -> None:
    client = build_replay_model_client(["not-json"])
    align = make_llm_align(client)

    with pytest.raises(Exception):
        await align(
            "Build the project",
            [PlannedSubtask(subtask_id="s1", description="Deliver it")],
            "",
        )
