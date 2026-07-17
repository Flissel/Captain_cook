"""Composition root for the standalone Captain planning pipeline."""

from pathlib import Path
from typing import Any, List

from autogen_core.models import ChatCompletionClient

from agenten.llm.decompose import make_llm_decompose
from agenten.llm.plan_batches import make_llm_align, make_llm_enrich
from agenten.llm.resilience import LlmSchemaError, LlmStage, run_llm_stage
from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    BatchEnrichment,
    BatchReleaseClient,
    CaptainPipeline,
    CapabilityResolver,
    PlannedSubtask,
)
from agenten.planning.policy import PlanningPolicy
from agenten.planning.release import JsonDirectoryReleaseClient
from agenten.planning.run_store import CaptainRunStore
from agenten.planning.captain_pipeline import HermesPlanResultReader


def build_captain_pipeline(
    *,
    model_client: ChatCompletionClient,
    output_dir: Path | str,
    target: str,
    known_capability_tags: List[str],
    allowed_targets: List[str] | None = None,
    max_alignment_attempts: int = 2,
    llm_timeout_seconds: float = 30.0,
    llm_max_attempts: int = 2,
    release_client: BatchReleaseClient | None = None,
    capability_resolver: CapabilityResolver | None = None,
    run_store: CaptainRunStore | None = None,
    plan_reader: HermesPlanResultReader | None = None,
) -> CaptainPipeline:
    """Wire the Captain's LLM stages to its deterministic planning core."""

    raw_decompose = make_llm_decompose(model_client, known_capability_tags)
    raw_align = make_llm_align(model_client, sorted(allowed_targets or [target]))
    raw_enrich = make_llm_enrich(model_client)
    allowed_tags = set(known_capability_tags)

    async def decompose(project_description: str) -> List[PlannedSubtask]:
        async def invoke() -> List[dict[str, Any]]:
            try:
                return await raw_decompose(project_description, 0)
            except ValueError as exc:
                # The concrete structured adapter reserves ValueError for
                # missing or invalid model content. Candidate policy checks
                # remain below, outside the provider retry boundary.
                raise LlmSchemaError(
                    LlmStage.DECOMPOSE,
                    "decomposer returned invalid structured output",
                ) from exc

        candidates = await run_llm_stage(
            LlmStage.DECOMPOSE,
            invoke,
            timeout_seconds=llm_timeout_seconds,
            max_attempts=llm_max_attempts,
        )
        planned: List[PlannedSubtask] = []
        for index, candidate in enumerate(candidates, start=1):
            tags = list(candidate.get("capability_tags", []))
            unknown = sorted(set(tags) - allowed_tags)
            if unknown:
                raise ValueError(f"decomposition returned unknown capability tags: {unknown}")
            planned.append(
                PlannedSubtask(
                    subtask_id=f"sub-{index:02d}",
                    description=str(candidate["description"]),
                    capability_tags=tags,
                )
            )
        return planned

    target_allowlist = frozenset(allowed_targets or [target])

    async def align(
        project_description: str,
        subtasks: List[PlannedSubtask],
        feedback: str,
    ) -> AlignmentPlan:
        return await run_llm_stage(
            LlmStage.ALIGN,
            lambda: raw_align(project_description, subtasks, feedback),
            timeout_seconds=llm_timeout_seconds,
            max_attempts=llm_max_attempts,
        )

    async def enrich(
        project_description: str,
        draft: BatchDraft,
        subtasks: List[PlannedSubtask],
    ) -> BatchEnrichment:
        return await run_llm_stage(
            LlmStage.ENRICH,
            lambda: raw_enrich(project_description, draft, subtasks),
            timeout_seconds=llm_timeout_seconds,
            max_attempts=llm_max_attempts,
        )

    return CaptainPipeline(
        decompose=decompose,
        align=align,
        enrich=enrich,
        release_client=release_client or JsonDirectoryReleaseClient(output_dir),
        policy=PlanningPolicy(frozenset(known_capability_tags)),
        capability_resolver=capability_resolver,
        run_store=run_store,
        plan_reader=plan_reader,
        target=target,
        allowed_targets=target_allowlist,
        max_alignment_attempts=max_alignment_attempts,
    )
