"""Structured LLM adapters for Captain batch alignment and enrichment."""

import json
from typing import List

from autogen_agentchat.agents import AssistantAgent
from autogen_core.models import ChatCompletionClient

from agenten.planning.alignment import AlignmentPlan, BatchDraft
from agenten.planning.captain_pipeline import (
    Align,
    BatchEnrichment,
    Enrich,
    PlannedSubtask,
)


_ALIGN_SYSTEM_MESSAGE = """You align already-decomposed project subtasks into executable work batches.
Return structured output only. Every supplied subtask_id must appear exactly once. Never invent ids.
Batch ids are lowercase slugs of at most 32 characters. Dependencies may reference only other batch ids.
Prefer independently deliverable batches and express their dependency edges explicitly.
If validation feedback is present, repair that exact defect in the new proposal."""


_ENRICH_SYSTEM_MESSAGE = """You enrich one work batch for an external execution system.
Return structured output only. Define a concrete goal, constraints, required capabilities, and observable
acceptance assertions. Produce useful build-visible golden examples and separate hidden holdout examples.
Golden and holdout case ids and values must not overlap. Do not mention or assume a particular executor."""


def make_llm_align(model_client: ChatCompletionClient) -> Align:
    async def align(
        project_description: str,
        subtasks: List[PlannedSubtask],
        feedback: str,
    ) -> AlignmentPlan:
        agent = AssistantAgent(
            name="batch_aligner",
            model_client=model_client,
            system_message=_ALIGN_SYSTEM_MESSAGE,
            output_content_type=AlignmentPlan,
        )
        payload = {
            "project_description": project_description,
            "subtasks": [subtask.model_dump() for subtask in subtasks],
            "validation_feedback": feedback,
        }
        result = await agent.run(task=json.dumps(payload, ensure_ascii=False))
        if not result.messages:
            raise ValueError("batch aligner returned no messages")
        content = getattr(result.messages[-1], "content", None)
        if not isinstance(content, AlignmentPlan):
            raise ValueError(f"batch aligner returned {type(content)!r}, expected AlignmentPlan")
        return content

    return align


def make_llm_enrich(model_client: ChatCompletionClient) -> Enrich:
    async def enrich(
        project_description: str,
        draft: BatchDraft,
        subtasks: List[PlannedSubtask],
    ) -> BatchEnrichment:
        agent = AssistantAgent(
            name="batch_enricher",
            model_client=model_client,
            system_message=_ENRICH_SYSTEM_MESSAGE,
            output_content_type=BatchEnrichment,
        )
        payload = {
            "project_description": project_description,
            "batch": draft.model_dump(),
            "subtasks": [subtask.model_dump() for subtask in subtasks],
        }
        result = await agent.run(task=json.dumps(payload, ensure_ascii=False))
        if not result.messages:
            raise ValueError("batch enricher returned no messages")
        content = getattr(result.messages[-1], "content", None)
        if not isinstance(content, BatchEnrichment):
            raise ValueError(f"batch enricher returned {type(content)!r}, expected BatchEnrichment")
        return content

    return enrich
