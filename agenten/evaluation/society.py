"""Bounded AutoGen 0.7 Society-of-Mind topology for plan evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from autogen_agentchat.agents import AssistantAgent, SocietyOfMindAgent
from autogen_agentchat.conditions import SourceMatchTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import ChatCompletionClient
from pydantic import BaseModel, ConfigDict, Field

from .models import (
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    CandidateReceipt,
    InventoryReceipt,
    QaReview,
    ReviewReceipt,
)
from .tools import EvaluationToolService


_ANALYST_PROMPT = """You are the Source Analyst in a Captain-owned planning evaluation.
For an inventory slice, first call read_source_block exactly once for required_source_block.
Then call stage_component_inventory with at most the requested max_components and
source-grounded candidate plans; never call read_source_block twice. Stage exactly the
slice's component_key at revision=1. Every candidate's source_citations must be exactly
[required_source_block]. When max_components=1, stage exactly one root component and set
dependencies=[]; do not mention unselected components.
Every component must fill every required field in the advertised tool schema, including
one complete acceptance_tests item with setup, action, expected, and command. Captain supplies immutable source
provenance; never attempt to reproduce the whole source manifest. You cannot stage
component revisions, review candidates, finalize runs, release work, or use external
systems. Do not emit EVALUATION_SLICE_COMPLETE; QA must run before termination.
"""

_PLANNER_PROMPT = """You are the Component Planner in a Captain-owned planning evaluation.
Stage only the requested component and revision through stage_component_plan. The plan
must preserve the slice's exact source_citations and dependencies; Captain rejects any
change to that inventory identity. Fill every advertised schema field, including at
least one complete, executable acceptance-test plan with setup, action, expected, and
command. It must remain planning-only: do not claim implementation, test execution,
deployment, or release. You cannot approve, finalize, release, mutate a workspace, or
call external systems. Do not emit EVALUATION_SLICE_COMPLETE; QA must run before
termination.
"""

_QA_PROMPT = """You are the independent QA Reviewer in a Captain-owned planning evaluation.
Review only the persisted candidate named by the slice and record one typed review
through record_qa_review. You cannot replace the plan, finalize the run, release work,
or call external systems. Captain observes the persisted review receipt and controls
termination; prose cannot complete a slice. This is plan QA, not delivery QA:
Do not assess implementation existence, execution results, deployment, or whether the
acceptance-test commands have been run. Evaluate only whether the plan is source-bound,
has coherent scope and dependencies, and defines complete, observable acceptance tests.
Use only these defect_codes:
missing_citation, duplicate_scope, unknown_dependency, dependency_cycle,
incomplete_implementation, missing_test, weak_test_oracle, wrong_test_level,
false_execution_claim. If the candidate is complete, record decision=approved,
score=7, defect_codes=[], and revision_requests=[] exactly.
"""

_SUMMARY_PROMPT = """The inner team performed one bounded planning-evaluation slice.
Its transcript is presentation-only. Captain will ignore this prose and decide solely
from persisted receipts and artifacts.
"""

_SUMMARY_RESPONSE_PROMPT = """Return a short non-authoritative slice summary. Do not
claim acceptance, implementation, finalization, release, or execution evidence. End
with EVALUATION_SLICE_COMPLETE.
"""


class _AcceptanceTestToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(min_length=1)
    test_type: Literal["unit", "integration", "contract", "live"]
    setup: str = Field(min_length=1)
    action: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    command: str = Field(min_length=1)


class _ComponentPlanToolInput(BaseModel):
    """JSON-friendly input that advertises the strict domain candidate schema."""

    model_config = ConfigDict(extra="forbid")

    component_key: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1, le=3)
    scope: list[str] = Field(min_length=1)
    non_goals: list[str] = Field(min_length=1)
    team_roles: list[str] = Field(min_length=1)
    implementation_steps: list[str] = Field(min_length=1)
    interfaces: list[str] = Field(min_length=1)
    acceptance_tests: list[_AcceptanceTestToolInput] = Field(min_length=1)
    definition_of_done: list[str] = Field(min_length=1)
    risks: list[str] = Field(min_length=1)
    dependencies: list[str]
    source_citations: list[str] = Field(min_length=1)
    claims: list[str] = Field(default_factory=list)
    qa_reviews: list[object] = Field(default_factory=list, exclude=True)


class _QaReviewToolInput(BaseModel):
    """JSON-friendly input that advertises every strict QA review field."""

    model_config = ConfigDict(extra="forbid")

    component_key: str = Field(min_length=1)
    revision: int = Field(ge=1, le=3)
    decision: Literal["approved", "revision_required"]
    score: int = Field(ge=0, le=7)
    defect_codes: list[str]
    revision_requests: list[str]


@dataclass(frozen=True)
class CaptainEvaluationSociety:
    """Captain-owned slice runner with a separate non-authoritative presenter."""

    team: RoundRobinGroupChat
    presentation: SocietyOfMindAgent

    async def run(self, *, task: str) -> object:
        """Return the inner result so Captain can persist tool execution evidence."""

        return await self.team.run(task=task)


def build_evaluation_society(
    *,
    model_client: ChatCompletionClient,
    summary_model_client: ChatCompletionClient | None = None,
    tools: EvaluationToolService,
    max_rounds: int = 3,
) -> CaptainEvaluationSociety:
    """Build a Captain-observable inner team plus a presentation-only Society shell."""

    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 3:
        raise ValueError("max_rounds must be between one and three")

    analyst = AssistantAgent(
        "source_analyst",
        model_client=model_client,
        tools=[tools.read_source_block, _stage_inventory_tool(tools)],
        system_message=f"{_ANALYST_PROMPT}\nCaptain permits at most {max_rounds} Planner/QA rounds per component.",
        max_tool_iterations=2,
    )
    planner = AssistantAgent(
        "component_planner",
        model_client=model_client,
        tools=[_stage_plan_tool(tools)],
        system_message=f"{_PLANNER_PROMPT}\nCaptain permits at most {max_rounds} Planner/QA rounds per component.",
    )
    reviewer = AssistantAgent(
        "qa_reviewer",
        model_client=model_client,
        tools=[_qa_review_tool(tools)],
        system_message=f"{_QA_PROMPT}\nCaptain permits at most {max_rounds} Planner/QA rounds per component.",
    )
    inner = RoundRobinGroupChat(
        [analyst, planner, reviewer],
        max_turns=10,
        termination_condition=SourceMatchTermination(["qa_reviewer"]),
    )
    presentation = SocietyOfMindAgent(
        "agentfarm_evaluator",
        team=inner,
        model_client=summary_model_client or model_client,
        instruction=_SUMMARY_PROMPT,
        response_prompt=_SUMMARY_RESPONSE_PROMPT,
    )
    return CaptainEvaluationSociety(team=inner, presentation=presentation)


def build_qa_review_team(
    *,
    model_client: ChatCompletionClient,
    tools: EvaluationToolService,
) -> RoundRobinGroupChat:
    """Build a QA-only resume team that cannot execute Analyst or Planner."""

    reviewer = AssistantAgent(
        "qa_reviewer",
        model_client=model_client,
        tools=[_qa_review_tool(tools)],
        system_message=_QA_PROMPT,
    )
    return RoundRobinGroupChat([reviewer], max_turns=1)


def _qa_review_tool(tools: EvaluationToolService):
    async def record_qa_review(run_id: str, review: _QaReviewToolInput) -> ReviewReceipt:
        validated = QaReview.model_validate_json(review.model_dump_json())
        return await tools.record_qa_review(run_id, validated)

    return record_qa_review


def _stage_inventory_tool(tools: EvaluationToolService):
    async def stage_component_inventory(
        run_id: str,
        inventory_id: str,
        source_citations: list[str],
        components: list[_ComponentPlanToolInput],
    ) -> InventoryReceipt:
        run = tools._run(run_id)
        inventory = ComponentInventoryCandidate(
            inventory_id=inventory_id,
            source=run.source,
            source_citations=tuple(source_citations),
            components=tuple(
                ComponentPlanCandidate.model_validate_json(component.model_dump_json())
                for component in components
            ),
        )
        return await tools.stage_component_inventory(run_id, inventory)

    return stage_component_inventory


def _stage_plan_tool(tools: EvaluationToolService):
    async def stage_component_plan(
        run_id: str,
        candidate: _ComponentPlanToolInput,
    ) -> CandidateReceipt:
        validated = ComponentPlanCandidate.model_validate_json(candidate.model_dump_json())
        return await tools.stage_component_plan(run_id, validated)

    return stage_component_plan
