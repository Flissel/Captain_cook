import hashlib
import json
from pathlib import Path

import pytest

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationOutcome,
    EvaluationSource,
    EvaluationStatus,
    QaReview,
    SourceBlock,
)
from agenten.evaluation.store import EvaluationConflictError, JsonEvaluationStore


def _source() -> EvaluationSource:
    text = "# Delivery\nOPENAI_API_KEY=[REDACTED]"
    return EvaluationSource(
        source_reference="inputs/project.md",
        sha256="a" * 64,
        byte_length=len(text.encode("utf-8")),
        blocks=(
            SourceBlock(
                block_id="block-0001",
                heading_path=("Delivery",),
                line_start=1,
                line_end=2,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                text=text,
            ),
        ),
    )


def _candidate(*, scope: str = "Own the delivery boundary.", revision: int = 1) -> ComponentPlanCandidate:
    return ComponentPlanCandidate(
        component_key="delivery-api",
        revision=revision,
        scope=(scope,),
        non_goals=("Do not deploy services.",),
        team_roles=("Delivery Builder",),
        implementation_steps=("Add the deterministic adapter.",),
        interfaces=("POST /deliveries",),
        acceptance_tests=(
            AcceptanceTestPlan(
                test_id="delivery-api-unit-001",
                test_type="unit",
                setup="Create the adapter.",
                action="Submit a delivery.",
                expected="The adapter returns a typed result.",
                command="python -m pytest -q tests/evaluation",
            ),
        ),
        definition_of_done=("Validation passes.",),
        risks=("Interface drift.",),
        dependencies=(),
        source_citations=("block-0001",),
    )


@pytest.mark.asyncio
async def test_store_replays_identical_candidate_and_rejects_changed_revision(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")

    first = await store.stage_candidate(run.run_id, _candidate())
    replay = await store.stage_candidate(run.run_id, _candidate())

    assert first == replay
    with pytest.raises(EvaluationConflictError, match="already staged differently"):
        await store.stage_candidate(run.run_id, _candidate(scope="Changed scope."))


@pytest.mark.asyncio
async def test_store_persists_ordered_atomic_evaluation_artifacts(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    candidate = _candidate()
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(candidate,),
    )

    await store.stage_inventory(run.run_id, inventory)
    await store.stage_candidate(run.run_id, candidate)
    await store.record_review(
        run.run_id,
        QaReview(
            component_key="delivery-api",
            revision=1,
            decision="approved",
            score=7,
            defect_codes=(),
            revision_requests=(),
        ),
    )
    manifest = await store.finalize(
        run.run_id,
        EvaluationOutcome.ACCEPTED,
    )

    run_dir = tmp_path / "eval-001"
    assert sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()) == sorted([
        "source-manifest.json",
        "component-inventory.json",
        "candidates/delivery-api/revision-1.json",
        "qa-reviews/delivery-api/revision-1.json",
        "run-manifest.json",
        "evaluation.md",
    ])
    assert not list(run_dir.rglob("*.tmp"))
    assert json.loads((run_dir / "run-manifest.json").read_text("utf-8"))["status"] == "accepted"
    assert manifest.artifact_digests
