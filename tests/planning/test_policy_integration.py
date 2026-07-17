from typing import List

import pytest

from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment, CaptainPipeline, PlannedSubtask
from agenten.planning.policy import PlanningPolicy, PlanningPolicyError
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, ExampleCase


class RecordingReleaseClient:
    def __init__(self) -> None:
        self.releases = []

    async def release(self, batch, holdouts) -> None:
        self.releases.append((batch, holdouts))


def assertion(batch_id: str) -> AcceptanceAssertion:
    return AcceptanceAssertion(
        assertion_id=f"{batch_id}-done",
        kind=AssertionKind.STATUS_EQUALS,
        expected="succeeded",
    )


@pytest.mark.asyncio
async def test_policy_rejects_unknown_capability_before_publication() -> None:
    async def decompose(_: str) -> List[PlannedSubtask]:
        return [PlannedSubtask(subtask_id="s1", description="Build")]

    async def align(*_):
        return AlignmentPlan(
            batches=[BatchDraft(batch_id="build", title="Build", subtask_ids=["s1"])]
        )

    async def enrich(*_):
        return BatchEnrichment(
            goal="Build",
            capability_tags=["invented"],
            acceptance_criteria=[assertion("build")],
            holdout_cases=[ExampleCase(case_id="hidden", input={"value": 2})],
        )

    releases = RecordingReleaseClient()
    pipeline = CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=releases,
        policy=PlanningPolicy(frozenset({"delivery"})),
        target="n8n",
    )

    with pytest.raises(PlanningPolicyError, match="unknown capability"):
        await pipeline.run("Build")

    assert releases.releases == []


@pytest.mark.asyncio
async def test_policy_rejects_cross_batch_holdout_leak_before_publication() -> None:
    subtasks = [
        PlannedSubtask(subtask_id="s1", description="One"),
        PlannedSubtask(subtask_id="s2", description="Two"),
    ]

    async def decompose(_: str):
        return subtasks

    async def align(*_):
        return AlignmentPlan(
            batches=[
                BatchDraft(batch_id="one", title="One", subtask_ids=["s1"]),
                BatchDraft(batch_id="two", title="Two", subtask_ids=["s2"]),
            ]
        )

    async def enrich(_, draft, __):
        leaked = {"lead": "same-content"}
        return BatchEnrichment(
            goal=draft.title,
            capability_tags=["delivery"],
            acceptance_criteria=[assertion(draft.batch_id)],
            golden_cases=[ExampleCase(case_id=f"{draft.batch_id}-visible", input=leaked)]
            if draft.batch_id == "one"
            else [],
            holdout_cases=[
                ExampleCase(
                    case_id=f"{draft.batch_id}-hidden",
                    input=leaked if draft.batch_id == "two" else {"lead": "different"},
                )
            ],
        )

    releases = RecordingReleaseClient()
    pipeline = CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=releases,
        policy=PlanningPolicy(frozenset({"delivery"})),
        target="n8n",
    )

    with pytest.raises(PlanningPolicyError, match="overlaps"):
        await pipeline.run("Build")

    assert releases.releases == []
