"""Bounded AutoGen 0.7 Society-of-Mind topology for plan evaluation."""

from __future__ import annotations

from autogen_agentchat.agents import AssistantAgent, SocietyOfMindAgent
from autogen_agentchat.conditions import TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import ChatCompletionClient

from .tools import EvaluationToolService


_SLICE_COMPLETE = "EVALUATION_SLICE_COMPLETE"

_ANALYST_PROMPT = """You are the Source Analyst in a Captain-owned planning evaluation.
Read only the requested redacted source blocks. For an inventory slice, stage exactly
one source-grounded inventory through stage_component_inventory. You cannot stage
component revisions, review candidates, finalize runs, release work, or use external
systems. Do not emit EVALUATION_SLICE_COMPLETE; QA must run before termination.
"""

_PLANNER_PROMPT = """You are the Component Planner in a Captain-owned planning evaluation.
Stage only the requested component and revision through stage_component_plan. The plan
must remain planning-only and include executable acceptance-test plans. You cannot
approve, finalize, release, mutate a workspace, or call external systems. Do not emit
EVALUATION_SLICE_COMPLETE; QA must run before termination.
"""

_QA_PROMPT = """You are the independent QA Reviewer in a Captain-owned planning evaluation.
Review only the persisted candidate named by the slice and record one typed review
through record_qa_review. You cannot replace the plan, finalize the run, release work,
or call external systems. End the slice with EVALUATION_SLICE_COMPLETE.
"""

_SUMMARY_PROMPT = """The inner team performed one bounded planning-evaluation slice.
Its transcript is presentation-only. Captain will ignore this prose and decide solely
from persisted receipts and artifacts.
"""

_SUMMARY_RESPONSE_PROMPT = """Return a short non-authoritative slice summary. Do not
claim acceptance, implementation, finalization, release, or execution evidence. End
with EVALUATION_SLICE_COMPLETE.
"""


def build_evaluation_society(
    *,
    model_client: ChatCompletionClient,
    tools: EvaluationToolService,
    max_rounds: int = 3,
) -> SocietyOfMindAgent:
    """Build the presentation wrapper and its ten-turn, receipt-only inner team."""

    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 3:
        raise ValueError("max_rounds must be between one and three")

    analyst = AssistantAgent(
        "source_analyst",
        model_client=model_client,
        tools=[tools.read_source_block, tools.stage_component_inventory],
        system_message=f"{_ANALYST_PROMPT}\nCaptain permits at most {max_rounds} Planner/QA rounds per component.",
    )
    planner = AssistantAgent(
        "component_planner",
        model_client=model_client,
        tools=[tools.stage_component_plan],
        system_message=f"{_PLANNER_PROMPT}\nCaptain permits at most {max_rounds} Planner/QA rounds per component.",
    )
    reviewer = AssistantAgent(
        "qa_reviewer",
        model_client=model_client,
        tools=[tools.record_qa_review],
        system_message=f"{_QA_PROMPT}\nCaptain permits at most {max_rounds} Planner/QA rounds per component.",
    )
    inner = RoundRobinGroupChat(
        [analyst, planner, reviewer],
        max_turns=10,
        termination_condition=TextMentionTermination(
            _SLICE_COMPLETE,
            sources=("qa_reviewer",),
        ),
    )
    return SocietyOfMindAgent(
        "agentfarm_evaluator",
        team=inner,
        model_client=model_client,
        instruction=_SUMMARY_PROMPT,
        response_prompt=_SUMMARY_RESPONSE_PROMPT,
    )
