"""Layer-1 constitution check for a whole planner-supplied plan.

The event-driven gatekeeper judges one `SubproblemProposed` at a time. A
planner such as OpenClaw hands Captain a complete, flat plan instead, so
this applies the same deterministic validators to every subtask in it:

- required fields (description and capability tags),
- minimality against the project description (the subtasks' parent),
- duplicates within the plan (the plan is one root problem),
- the ruleset's total-subproblem budget.

Layer 2, the semantic scope/quality judge, is deliberately *not* run here:
the planner must not judge its own plan, and a report that silently skipped
it would be indistinguishable from a full admission. The report therefore
states `semantic_review = "not_performed"`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Optional, Tuple

from agenten.constitution.ruleset import ConstitutionRuleset
from agenten.constitution.validators import ValidationFailure, run_deterministic_checks

_PLAN_ROOT = "supplied-plan"

PlanSubtask = Tuple[str, str, Sequence[str]]
"""(subtask_id, description, capability_tags)"""


@dataclass(frozen=True)
class SubtaskVerdict:
    subtask_id: str
    failure: Optional[ValidationFailure]

    @property
    def accepted(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class PlanConstitutionReport:
    constitution_version: str
    verdicts: tuple[SubtaskVerdict, ...]
    plan_failure: Optional[ValidationFailure]
    semantic_review: Literal["not_performed"] = "not_performed"

    @property
    def accepted(self) -> bool:
        return self.plan_failure is None and all(verdict.accepted for verdict in self.verdicts)

    def rejections(self) -> list[str]:
        found: list[str] = []
        if self.plan_failure is not None:
            found.append(f"plan: {self.plan_failure[0]}: {self.plan_failure[1]}")
        found += [
            f"{verdict.subtask_id}: {verdict.failure[0]}: {verdict.failure[1]}"
            for verdict in self.verdicts
            if verdict.failure is not None
        ]
        return found

    def to_json(self) -> dict[str, Any]:
        return {
            "constitution_version": self.constitution_version,
            "accepted": self.accepted,
            "layer": "deterministic",
            "semantic_review": self.semantic_review,
            "rejections": self.rejections(),
        }


def check_supplied_plan(
    project_description: str,
    subtasks: Sequence[PlanSubtask],
    ruleset: ConstitutionRuleset,
) -> PlanConstitutionReport:
    """Run the deterministic constitution layer over every subtask."""

    budget = ruleset.default_budget.max_total_subproblems
    plan_failure: Optional[ValidationFailure] = None
    if len(subtasks) > budget:
        plan_failure = ("budget_exceeded", f"plan has {len(subtasks)} subtasks; the budget is {budget}")

    seen: list[Tuple[str, str]] = []
    verdicts: list[SubtaskVerdict] = []
    for subtask_id, description, capability_tags in subtasks:
        failure = run_deterministic_checks(
            description=description,
            capability_tags=list(capability_tags),
            root_problem_id=_PLAN_ROOT,
            parent_description=project_description,
            pending_descriptions=seen,
        )
        verdicts.append(SubtaskVerdict(subtask_id=subtask_id, failure=failure))
        if failure is None:
            seen.append((_PLAN_ROOT, description))
    return PlanConstitutionReport(
        constitution_version=ruleset.version,
        verdicts=tuple(verdicts),
        plan_failure=plan_failure,
    )
