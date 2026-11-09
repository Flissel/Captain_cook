"""Layer-1 constitution check for a whole planner-supplied plan.

The event-driven gatekeeper judges one `SubproblemProposed` at a time. A
planner such as OpenClaw hands Captain a complete, flat plan instead, so
this applies the same deterministic validators to everything the plan
would release:

- every subtask (description + capability tags),
- every batch as it is released (enrichment goal + capability tags), so
  the gate reads the same content the release writes,

each with required fields, minimality against the project description
(their parent) and duplicates within its own group, plus the ruleset's
total-subproblem budget, which is checked first and stops the check.

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
from agenten.constitution.validators import (
    ValidationFailure,
    check_minimality,
    check_required_fields,
    normalize_description,
)

PlanItem = Tuple[str, str, Sequence[str]]
"""(item_id, description, capability_tags)"""


@dataclass(frozen=True)
class ItemVerdict:
    item_id: str
    failure: Optional[ValidationFailure]

    @property
    def accepted(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class PlanConstitutionReport:
    constitution_version: str
    verdicts: tuple[ItemVerdict, ...]
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
            f"{verdict.item_id}: {verdict.failure[0]}: {verdict.failure[1]}"
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
    subtasks: Sequence[PlanItem],
    ruleset: ConstitutionRuleset,
    *,
    batches: Sequence[PlanItem] = (),
) -> PlanConstitutionReport:
    """Run the deterministic constitution layer over subtasks and released batches."""

    budget = ruleset.default_budget.max_total_subproblems
    if len(subtasks) > budget or len(batches) > budget:
        return PlanConstitutionReport(
            constitution_version=ruleset.version,
            verdicts=(),
            plan_failure=(
                "budget_exceeded",
                f"plan has {len(subtasks)} subtasks and {len(batches)} batches; the budget is {budget}",
            ),
        )
    verdicts = _check_group(project_description, subtasks, prefix="")
    verdicts += _check_group(project_description, batches, prefix="batch:")
    return PlanConstitutionReport(
        constitution_version=ruleset.version,
        verdicts=tuple(verdicts),
        plan_failure=None,
    )


def _check_group(parent: str, items: Sequence[PlanItem], *, prefix: str) -> list[ItemVerdict]:
    seen: set[str] = set()
    verdicts: list[ItemVerdict] = []
    for item_id, description, capability_tags in items:
        failure = check_required_fields(description, list(capability_tags)) or check_minimality(
            description, parent
        )
        if failure is None:
            normalized = normalize_description(description)
            if normalized in seen:
                failure = ("duplicate", "an identical item already appears in this plan")
            else:
                seen.add(normalized)
        verdicts.append(ItemVerdict(item_id=f"{prefix}{item_id}", failure=failure))
    return verdicts
