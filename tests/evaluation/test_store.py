import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationOutcome,
    EvaluationRun,
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
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(_candidate(),),
    )
    await store.stage_inventory(run.run_id, inventory)

    first = await store.stage_candidate(run.run_id, _candidate())
    replay = await store.stage_candidate(run.run_id, _candidate())

    assert first == replay
    with pytest.raises(EvaluationConflictError, match="already staged differently"):
        await store.stage_candidate(run.run_id, _candidate(scope="Changed scope."))


@pytest.mark.asyncio
async def test_store_redacts_case_insensitive_credentials_in_every_persisted_text_field(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    source = EvaluationSource(
        source_reference="PASSWORD = source-secret",
        sha256="a" * 64,
        byte_length=1,
        blocks=_source().blocks,
    )
    run = await store.create_run(source, run_id="eval-001", idempotency_key="CLIENT_TOKEN = raw-token")
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(_candidate(scope="gateway_api_key = raw-api-key\nPASSWORD = raw-password"),),
    )

    await store.stage_inventory(run.run_id, inventory)
    await store.stage_candidate(run.run_id, _candidate(scope="gateway_api_key = raw-api-key\nPASSWORD = raw-password"))

    persisted = "\n".join(path.read_text("utf-8") for path in (tmp_path / "eval-001").rglob("*.json"))
    assert "raw-token" not in persisted
    assert "raw-api-key" not in persisted
    assert "raw-password" not in persisted
    assert "source-secret" not in persisted
    assert "[REDACTED]" in persisted


def test_persisted_source_and_run_contracts_are_strict() -> None:
    with pytest.raises(ValidationError):
        SourceBlock(
            block_id="block-0001",
            heading_path=("Delivery",),
            line_start="1",
            line_end=1,
            sha256=hashlib.sha256(b"text").hexdigest(),
            text="text",
        )
    with pytest.raises(ValidationError):
        EvaluationSource(
            source_reference="inputs/project.md",
            sha256="a" * 64,
            byte_length="1",
            blocks=_source().blocks,
        )
    with pytest.raises(ValidationError):
        EvaluationRun(
            run_id="eval-001",
            idempotency_key="input-v1",
            source=_source(),
            status=EvaluationStatus.CREATED,
            max_rounds=3,
            max_calls="1",
        )


@pytest.mark.asyncio
async def test_store_enforces_stage_order_and_safe_run_ids_before_review_paths(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    review = QaReview(component_key="delivery-api", revision=1, decision="approved", score=7, defect_codes=(), revision_requests=())

    with pytest.raises(EvaluationConflictError, match="component-inventory"):
        await store.stage_candidate(run.run_id, _candidate())
    with pytest.raises(ValueError, match="safe logical identifier"):
        await store.record_review("../outside", review)
    with pytest.raises(EvaluationConflictError, match="component-inventory"):
        await store.finalize(run.run_id, EvaluationOutcome.ACCEPTED)


@pytest.mark.asyncio
async def test_finalize_rejects_candidate_that_disagrees_with_staged_inventory(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(_candidate(scope="Inventory scope."),),
    )
    await store.stage_inventory(run.run_id, inventory)
    await store.stage_candidate(run.run_id, _candidate(scope="Different staged scope."))
    await store.record_review(
        run.run_id,
        QaReview(component_key="delivery-api", revision=1, decision="approved", score=7, defect_codes=(), revision_requests=()),
    )

    with pytest.raises(EvaluationConflictError, match="does not match staged inventory"):
        await store.finalize(run.run_id, EvaluationOutcome.ACCEPTED)


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
