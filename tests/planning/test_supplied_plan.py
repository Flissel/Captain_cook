"""Planner-supplied stage results run through Captain's deterministic core."""

from __future__ import annotations

from typing import Any

import pytest

from agenten.planning.captain_pipeline import CaptainPlanningError
from agenten.planning.policy import PlanningPolicyError
from agenten.planning.supplied_plan import (
    PlanCompilerSettings,
    SuppliedPlan,
    compile_supplied_plan,
    publish_supplied_plan,
)
from agenten.validation.contracts import HoldoutSuite, WorkBatch

SETTINGS = PlanCompilerSettings(
    target="python",
    allowed_targets=frozenset({"python", "n8n"}),
    capability_tags=frozenset({"python", "email"}),
)


def enrichment(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "goal": "Send the weekly report",
        "capability_tags": ["email"],
        "acceptance_criteria": [
            {"assertion_id": "status-ok", "kind": "status_equals", "expected": "succeeded"}
        ],
        "holdout_cases": [{"case_id": "holdout-1", "input": {"week": 1}, "expected_observations": {"sent": True}}],
    }
    values.update(overrides)
    return values


def plan(**overrides: Any) -> SuppliedPlan:
    values: dict[str, Any] = {
        "project_description": "Weekly report mailer",
        "subtasks": [
            {"subtask_id": "s1", "description": "Collect data", "capability_tags": ["python"]},
            {"subtask_id": "s2", "description": "Send mail", "capability_tags": ["email"]},
        ],
        "alignment": {
            "batches": [
                {"batch_id": "collect", "title": "Collect", "subtask_ids": ["s1"]},
                {
                    "batch_id": "mail",
                    "title": "Mail",
                    "subtask_ids": ["s2"],
                    "depends_on": ["collect"],
                    "target": "n8n",
                },
            ]
        },
        "enrichments": {
            "collect": enrichment(capability_tags=["python"]),
            "mail": enrichment(holdout_cases=[{"case_id": "holdout-2", "input": {"week": 2}, "expected_observations": {"sent": True}}]),
        },
    }
    values.update(overrides)
    return SuppliedPlan.model_validate(values)


class RecordingRelease:
    def __init__(self) -> None:
        self.released: list[tuple[WorkBatch, HoldoutSuite]] = []

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        self.released.append((batch, holdouts))


@pytest.mark.asyncio
async def test_supplied_plan_compiles_deterministically() -> None:
    compiled = await compile_supplied_plan(plan(), SETTINGS)

    assert [batch.batch_id for batch in compiled.batches] == ["collect", "mail"]
    assert [batch.target for batch in compiled.batches] == ["python", "n8n"]
    assert compiled.batches[1].depends_on == ["collect"]
    assert [suite.batch_id for suite in compiled.holdouts] == ["collect", "mail"]
    assert compiled == await compile_supplied_plan(plan(), SETTINGS)


@pytest.mark.asyncio
async def test_alignment_errors_are_not_retried() -> None:
    broken = plan(
        alignment={"batches": [{"batch_id": "collect", "title": "C", "subtask_ids": ["s1"]}]},
        enrichments={"collect": enrichment(capability_tags=["python"])},
    )

    with pytest.raises(CaptainPlanningError, match="missing subtask ids"):
        await compile_supplied_plan(broken, SETTINGS)


@pytest.mark.asyncio
async def test_unknown_capability_tags_violate_policy() -> None:
    rogue = plan(enrichments={"collect": enrichment(capability_tags=["root"]), "mail": enrichment()})

    with pytest.raises(PlanningPolicyError):
        await compile_supplied_plan(rogue, SETTINGS)


@pytest.mark.asyncio
async def test_disallowed_target_is_rejected() -> None:
    settings = PlanCompilerSettings(
        target="python", allowed_targets=frozenset({"python"}), capability_tags=SETTINGS.capability_tags
    )

    with pytest.raises(CaptainPlanningError, match="disallowed target"):
        await compile_supplied_plan(plan(), settings)


def test_enrichments_must_match_the_aligned_batches_exactly() -> None:
    with pytest.raises(ValueError, match="enrichments"):
        plan(enrichments={"collect": enrichment()})
    with pytest.raises(ValueError, match="enrichments"):
        plan(enrichments={"collect": enrichment(), "mail": enrichment(), "extra": enrichment()})


@pytest.mark.asyncio
async def test_publish_recompiles_and_releases_every_batch() -> None:
    release = RecordingRelease()

    compiled = await publish_supplied_plan(plan(), SETTINGS, release_client=release)

    assert [batch.batch_id for batch, _ in release.released] == ["collect", "mail"]
    assert [batch for batch, _ in release.released] == list(compiled.batches)
