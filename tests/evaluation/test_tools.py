import hashlib
from pathlib import Path

import pytest

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationSource,
    QaReview,
    SourceBlock,
)
from agenten.evaluation.store import JsonEvaluationStore
from agenten.evaluation.tools import EvaluationToolError, EvaluationToolService


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


def _candidate(*, component_key: str = "delivery-api", revision: int = 1, **changes: object) -> ComponentPlanCandidate:
    values: dict[str, object] = {
        "component_key": component_key,
        "revision": revision,
        "scope": ("Own the delivery boundary.",),
        "non_goals": ("Do not deploy services.",),
        "team_roles": ("Delivery Builder",),
        "implementation_steps": ("Add the deterministic adapter.",),
        "interfaces": ("POST /deliveries",),
        "acceptance_tests": (
            AcceptanceTestPlan(
                test_id=f"{component_key}-unit-{revision:03d}",
                test_type="unit",
                setup="Create the adapter.",
                action="Submit a delivery.",
                expected="The adapter returns a typed result.",
                command="python -m pytest -q tests/evaluation",
            ),
        ),
        "definition_of_done": ("Validation passes.",),
        "risks": ("Interface drift.",),
        "dependencies": (),
        "source_citations": ("block-0001",),
    }
    values.update(changes)
    return ComponentPlanCandidate(**values)


def _inventory(*candidates: ComponentPlanCandidate) -> ComponentInventoryCandidate:
    return ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=_source(),
        source_citations=("block-0001",),
        components=candidates,
    )


async def _service(tmp_path: Path) -> EvaluationToolService:
    store = JsonEvaluationStore(tmp_path)
    await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    return EvaluationToolService(store)


@pytest.mark.asyncio
async def test_tool_service_returns_only_redacted_source_views(tmp_path: Path) -> None:
    service = await _service(tmp_path)

    view = await service.read_source_block("eval-001", "block-0001")

    assert view.block_id == "block-0001"
    assert "OPENAI_API_KEY" not in view.text
    assert not hasattr(service, "finalize")


@pytest.mark.asyncio
async def test_tool_service_rejects_plan_before_inventory_and_review_of_missing_candidate(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    review = QaReview(component_key="delivery-api", revision=1, decision="approved", score=7, defect_codes=(), revision_requests=())

    with pytest.raises(EvaluationToolError, match="inventory"):
        await service.stage_component_plan("eval-001", _candidate())
    with pytest.raises(EvaluationToolError, match="candidate"):
        await service.record_qa_review("eval-001", review)


@pytest.mark.asyncio
async def test_tool_service_rejects_unsafe_run_ids_and_component_keys_before_store_writes(tmp_path: Path) -> None:
    service = await _service(tmp_path)

    with pytest.raises(EvaluationToolError, match="safe logical identifier"):
        await service.read_source_block("../outside", "block-0001")
    with pytest.raises(EvaluationToolError, match="safe logical identifier"):
        await service.stage_component_inventory("eval-001", _inventory(_candidate(component_key="../outside")))

    assert not (tmp_path / "eval-001" / "component-inventory.json").exists()


@pytest.mark.asyncio
async def test_tool_service_requires_monotonic_revisions_and_enforces_three_round_ceiling(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    second_revision = _candidate(revision=2)
    await service.stage_component_inventory("eval-001", _inventory(second_revision))

    with pytest.raises(EvaluationToolError, match="expected revision 1"):
        await service.stage_component_plan("eval-001", second_revision)

    fourth_values = _candidate().model_dump()
    fourth_values["revision"] = 4
    fourth = ComponentPlanCandidate.model_construct(**fourth_values)
    with pytest.raises(EvaluationToolError, match="three-round"):
        await service.stage_component_plan("eval-001", fourth)


@pytest.mark.asyncio
async def test_tool_service_validates_citations_and_qa_rubric_before_store_calls(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    invalid_inventory = _inventory(_candidate(source_citations=("block-9999",)))

    with pytest.raises(EvaluationToolError, match="missing_source_citation"):
        await service.stage_component_inventory("eval-001", invalid_inventory)

    await service.stage_component_inventory("eval-001", _inventory(_candidate()))
    await service.stage_component_plan("eval-001", _candidate())
    invalid_review = QaReview(
        component_key="delivery-api",
        revision=1,
        decision="revision_required",
        score=3,
        defect_codes=("invented_code",),
        revision_requests=("Use a registered code.",),
    )

    with pytest.raises(EvaluationToolError, match="unknown_rubric_code"):
        await service.record_qa_review("eval-001", invalid_review)
