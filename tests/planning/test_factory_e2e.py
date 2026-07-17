from pathlib import Path

import httpx
import openai
import pytest

from agenten.llm.decompose import DecomposeResponse, SubproblemCandidate
from agenten.llm.model_client import build_replay_model_client
from agenten.llm.resilience import LlmSchemaError, LlmStage, LlmStageError
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import BatchEnrichment
from agenten.planning.captain_pipeline import CapabilityResolver
from agenten.planning.factory import build_captain_pipeline
from agenten.planning.policy import PlanningPolicyError
from agenten.validation.contracts import (
    AcceptanceAssertion,
    AssertionKind,
    ExampleCase,
)


class RecordingReleaseClient:
    def __init__(self) -> None:
        self.batch_ids: list[str] = []

    async def release(self, batch, holdouts) -> None:
        assert batch.batch_id == holdouts.batch_id
        self.batch_ids.append(batch.batch_id)


class NoMatchResolver(CapabilityResolver):
    async def find_match(self, target: str, capability_tags) -> None:
        del target, capability_tags
        return None


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


@pytest.mark.asyncio
async def test_factory_uses_injected_release_and_capability_ports(tmp_path: Path) -> None:
    release = RecordingReleaseClient()
    client = build_replay_model_client(
        [
            valid_decomposition().model_dump_json(),
            valid_alignment().model_dump_json(),
            valid_enrichment().model_dump_json(),
        ]
    )
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path / "must-not-be-used",
        target="external",
        known_capability_tags=["delivery"],
        release_client=release,
        capability_resolver=NoMatchResolver(),
    )

    await pipeline.run("Build something useful")

    assert release.batch_ids == ["delivery"]
    assert not (tmp_path / "must-not-be-used").exists()


def valid_decomposition(*, capability_tag: str = "delivery") -> DecomposeResponse:
    return DecomposeResponse(
        subproblems=[
            SubproblemCandidate(
                description="Produce the deliverable",
                capability_tags=[capability_tag],
                atomic=True,
            )
        ]
    )


def valid_alignment() -> AlignmentPlan:
    return AlignmentPlan(
        batches=[
            BatchDraft(
                batch_id="delivery",
                title="Delivery",
                subtask_ids=["sub-01"],
            )
        ]
    )


def valid_enrichment(*, capability_tag: str = "delivery") -> BatchEnrichment:
    return BatchEnrichment(
        goal="Produce a verified deliverable",
        capability_tags=[capability_tag],
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


@pytest.mark.parametrize(
    ("stage", "expected_provider_calls"),
    (
        (LlmStage.DECOMPOSE, 1),
        (LlmStage.ALIGN, 2),
        (LlmStage.ENRICH, 3),
    ),
)
@pytest.mark.asyncio
async def test_factory_types_each_structured_stage_failure(
    tmp_path: Path,
    stage: LlmStage,
    expected_provider_calls: int,
) -> None:
    responses = [
        valid_decomposition().model_dump_json(),
        valid_alignment().model_dump_json(),
        valid_enrichment().model_dump_json(),
    ][:expected_provider_calls]
    responses[-1] = "not-json"
    client = build_replay_model_client(responses)
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path,
        target="external",
        known_capability_tags=["delivery"],
    )

    with pytest.raises(LlmSchemaError) as failure:
        await pipeline.compile("Build something useful")

    assert failure.value.stage is stage
    assert failure.value.attempts == 1
    assert isinstance(failure.value.__cause__, ValueError)
    assert len(client.create_calls) == expected_provider_calls


@pytest.mark.asyncio
async def test_factory_keeps_unknown_decomposition_tags_deterministic(
    tmp_path: Path,
) -> None:
    client = build_replay_model_client(
        [valid_decomposition(capability_tag="invented").model_dump_json()]
    )
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path,
        target="external",
        known_capability_tags=["delivery"],
    )

    with pytest.raises(ValueError, match="unknown capability tags") as failure:
        await pipeline.compile("Build something useful")

    assert not isinstance(failure.value, LlmStageError)
    assert len(client.create_calls) == 1


@pytest.mark.asyncio
async def test_alignment_policy_feedback_is_separate_from_provider_retry(
    tmp_path: Path,
) -> None:
    invalid_alignment = AlignmentPlan(
        batches=[
            BatchDraft(
                batch_id="delivery",
                title="Delivery",
                subtask_ids=["unknown-subtask"],
            )
        ]
    )
    client = build_replay_model_client(
        [
            valid_decomposition().model_dump_json(),
            invalid_alignment.model_dump_json(),
            valid_alignment().model_dump_json(),
            valid_enrichment().model_dump_json(),
        ]
    )
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path,
        target="external",
        known_capability_tags=["delivery"],
        max_alignment_attempts=2,
    )

    compiled = await pipeline.compile("Build something useful")

    assert [batch.batch_id for batch in compiled.batches] == ["delivery"]
    assert len(client.create_calls) == 4


@pytest.mark.asyncio
async def test_two_alignment_policy_rounds_bound_provider_calls_to_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_alignment = AlignmentPlan(
        batches=[
            BatchDraft(
                batch_id="delivery",
                title="Delivery",
                subtask_ids=["unknown-subtask"],
            )
        ]
    )
    align_outcomes: list[AlignmentPlan | Exception] = [
        openai.APIConnectionError(
            request=httpx.Request("POST", "https://provider.invalid/align")
        ),
        invalid_alignment,
        openai.APIConnectionError(
            request=httpx.Request("POST", "https://provider.invalid/align")
        ),
        valid_alignment(),
    ]
    align_calls = 0

    async def scripted_align(*args, **kwargs) -> AlignmentPlan:
        nonlocal align_calls
        del args, kwargs
        outcome = align_outcomes[align_calls]
        align_calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        "agenten.planning.factory.make_llm_align",
        lambda *args, **kwargs: scripted_align,
    )
    client = build_replay_model_client(
        [
            valid_decomposition().model_dump_json(),
            valid_enrichment().model_dump_json(),
        ]
    )
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path,
        target="external",
        known_capability_tags=["delivery"],
        max_alignment_attempts=2,
    )

    compiled = await pipeline.compile("Build something useful")

    assert [batch.batch_id for batch in compiled.batches] == ["delivery"]
    assert align_calls == 4
    assert len(client.create_calls) == 2


@pytest.mark.asyncio
async def test_planning_policy_error_is_not_a_provider_retry(
    tmp_path: Path,
) -> None:
    client = build_replay_model_client(
        [
            valid_decomposition().model_dump_json(),
            valid_alignment().model_dump_json(),
            valid_enrichment(capability_tag="invented").model_dump_json(),
        ]
    )
    pipeline = build_captain_pipeline(
        model_client=client,
        output_dir=tmp_path,
        target="external",
        known_capability_tags=["delivery"],
    )

    with pytest.raises(PlanningPolicyError):
        await pipeline.compile("Build something useful")

    assert len(client.create_calls) == 3
