"""Composition root for the standalone Captain planning pipeline."""

from pathlib import Path
from typing import List

from autogen_core.models import ChatCompletionClient

from agenten.llm.decompose import make_llm_decompose
from agenten.llm.plan_batches import make_llm_align, make_llm_enrich
from agenten.planning.captain_pipeline import CaptainPipeline, PlannedSubtask
from agenten.planning.policy import PlanningPolicy
from agenten.planning.release import JsonDirectoryReleaseClient


def build_captain_pipeline(
    *,
    model_client: ChatCompletionClient,
    output_dir: Path | str,
    target: str,
    known_capability_tags: List[str],
    max_alignment_attempts: int = 2,
) -> CaptainPipeline:
    """Wire the Captain's LLM stages to its deterministic planning core."""

    raw_decompose = make_llm_decompose(model_client, known_capability_tags)
    allowed_tags = set(known_capability_tags)

    async def decompose(project_description: str) -> List[PlannedSubtask]:
        candidates = await raw_decompose(project_description, 0)
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

    return CaptainPipeline(
        decompose=decompose,
        align=make_llm_align(model_client),
        enrich=make_llm_enrich(model_client),
        release_client=JsonDirectoryReleaseClient(output_dir),
        policy=PlanningPolicy(frozenset(known_capability_tags)),
        target=target,
        max_alignment_attempts=max_alignment_attempts,
    )
