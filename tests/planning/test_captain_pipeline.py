from typing import List

import pytest

from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    BatchEnrichment,
    CaptainPipeline,
    CaptainPlanningError,
    PlannedSubtask,
)
from agenten.planning.policy import PlanningPolicy, PlanningPolicyError
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
    HoldoutSuite,
    WorkBatch,
)


class RecordingReleaseClient:
    def __init__(self) -> None:
        self.releases: List[tuple[WorkBatch, HoldoutSuite]] = []

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        self.releases.append((batch, holdouts))


class MatchingCapabilityResolver:
    async def find_match(self, target: str, capability_tags: List[str]) -> str | None:
        assert target == "external"
        assert capability_tags == ["delivery"]
        return "validated-capability:delivery-v2"


def enrichment_for(draft: BatchDraft) -> BatchEnrichment:
    return BatchEnrichment(
        goal=f"Deliver {draft.title}",
        constraints=["Remain target-neutral"],
        capability_tags=["delivery"],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id=f"{draft.batch_id}-status",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
        golden_cases=[ExampleCase(case_id=f"{draft.batch_id}-golden", input={"visible": True})],
        holdout_cases=[ExampleCase(case_id=f"{draft.batch_id}-hidden", input={"hidden": True})],
    )


@pytest.mark.asyncio
async def test_pipeline_retries_invalid_alignment_and_releases_dependency_ordered_batches() -> None:
    subtasks = [
        PlannedSubtask(subtask_id="s1", description="Foundation"),
        PlannedSubtask(subtask_id="s2", description="Delivery"),
    ]
    proposals = [
        AlignmentPlan(
            batches=[BatchDraft(batch_id="incomplete", title="Incomplete", subtask_ids=["s1"])]
        ),
        AlignmentPlan(
            batches=[
                BatchDraft(
                    batch_id="delivery",
                    title="Delivery",
                    subtask_ids=["s2"],
                    depends_on=["foundation"],
                ),
                BatchDraft(batch_id="foundation", title="Foundation", subtask_ids=["s1"]),
            ]
        ),
    ]
    align_calls: List[str] = []

    async def decompose(_: str) -> List[PlannedSubtask]:
        return subtasks

    async def align(_: str, __: List[PlannedSubtask], feedback: str) -> AlignmentPlan:
        align_calls.append(feedback)
        return proposals[len(align_calls) - 1]

    async def enrich(
        _: str, draft: BatchDraft, __: List[PlannedSubtask]
    ) -> BatchEnrichment:
        return enrichment_for(draft)

    releases = RecordingReleaseClient()
    pipeline = CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=releases,
        policy=PlanningPolicy(frozenset({"delivery"})),
        target="external",
        max_alignment_attempts=2,
    )

    result = await pipeline.run("Build a delivery system")

    assert "missing subtask ids" in align_calls[1]
    assert [batch.batch_id for batch in result.batches] == ["foundation", "delivery"]
    assert [batch.batch_id for batch, _ in releases.releases] == ["foundation", "delivery"]
    assert all("holdout" not in batch.model_dump() for batch, _ in releases.releases)
    assert releases.releases[0][1].cases[0].input == {"hidden": True}


@pytest.mark.asyncio
async def test_pipeline_stops_before_enrichment_when_alignment_never_covers_all_subtasks() -> None:
    enrich_calls: List[str] = []
    releases = RecordingReleaseClient()

    async def decompose(_: str) -> List[PlannedSubtask]:
        return [
            PlannedSubtask(subtask_id="s1", description="One"),
            PlannedSubtask(subtask_id="s2", description="Two"),
        ]

    async def align(_: str, __: List[PlannedSubtask], ___: str) -> AlignmentPlan:
        return AlignmentPlan(
            batches=[BatchDraft(batch_id="incomplete", title="Incomplete", subtask_ids=["s1"])]
        )

    async def enrich(
        _: str, draft: BatchDraft, __: List[PlannedSubtask]
    ) -> BatchEnrichment:
        enrich_calls.append(draft.batch_id)
        return enrichment_for(draft)

    pipeline = CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=releases,
        policy=PlanningPolicy(frozenset({"delivery"})),
        target="external",
        max_alignment_attempts=2,
    )

    with pytest.raises(CaptainPlanningError, match="alignment failed after 2 attempts"):
        await pipeline.run("Impossible plan")

    assert enrich_calls == []
    assert releases.releases == []


@pytest.mark.asyncio
async def test_pipeline_derives_capability_reuse_from_resolver_not_the_llm() -> None:
    async def decompose(_: str) -> List[PlannedSubtask]:
        return [PlannedSubtask(subtask_id="s1", description="Delivery")]

    async def align(_: str, __: List[PlannedSubtask], ___: str) -> AlignmentPlan:
        return AlignmentPlan(
            batches=[BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["s1"])]
        )

    async def enrich(
        _: str, draft: BatchDraft, __: List[PlannedSubtask]
    ) -> BatchEnrichment:
        return enrichment_for(draft)

    releases = RecordingReleaseClient()
    pipeline = CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=releases,
        policy=PlanningPolicy(frozenset({"delivery"})),
        capability_resolver=MatchingCapabilityResolver(),
        target="external",
    )

    result = await pipeline.run("Reuse a validated delivery capability")

    assert result.batches[0].satisfied_by == "validated-capability:delivery-v2"


@pytest.mark.asyncio
async def test_pipeline_validates_enrichment_before_capability_lookup_and_release() -> None:
    class UnexpectedResolver:
        async def find_match(self, target: str, capability_tags: List[str]) -> str | None:
            raise AssertionError("capability lookup must not run for invalid enrichment")

    async def decompose(_: str) -> List[PlannedSubtask]:
        return [PlannedSubtask(subtask_id="s1", description="Delivery")]

    async def align(_: str, __: List[PlannedSubtask], ___: str) -> AlignmentPlan:
        return AlignmentPlan(
            batches=[BatchDraft(batch_id="delivery", title="Delivery", subtask_ids=["s1"])]
        )

    async def enrich(
        _: str, draft: BatchDraft, __: List[PlannedSubtask]
    ) -> BatchEnrichment:
        return enrichment_for(draft).model_copy(
            update={"capability_tags": ["invented"]}
        )

    releases = RecordingReleaseClient()
    pipeline = CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=releases,
        capability_resolver=UnexpectedResolver(),
        policy=PlanningPolicy(frozenset({"delivery"})),
        target="external",
    )

    with pytest.raises(PlanningPolicyError, match="unknown capability tags"):
        await pipeline.run("Reject invented capabilities")

    assert releases.releases == []
