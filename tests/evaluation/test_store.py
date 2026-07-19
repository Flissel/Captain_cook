import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.evaluation.models import (
    AcceptanceTestPlan,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationRun,
    EvaluationSource,
    EvaluationStatus,
    EvaluationTelemetry,
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
async def test_inventory_rejects_unsafe_component_keys_before_persistence(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(_candidate().model_copy(update={"component_key": "../outside"}),),
    )

    with pytest.raises(ValueError, match="safe logical identifier"):
        await store.stage_inventory(run.run_id, inventory)

    assert not (tmp_path / "eval-001" / "component-inventory.json").exists()


@pytest.mark.asyncio
async def test_candidate_must_belong_to_declared_inventory_and_finalize_rechecks_safe_key(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-001",
        source=run.source,
        source_citations=("block-0001",),
        components=(_candidate(),),
    )
    await store.stage_inventory(run.run_id, inventory)

    with pytest.raises(EvaluationConflictError, match="declared inventory"):
        await store.stage_candidate(run.run_id, _candidate().model_copy(update={"component_key": "other-component"}))

    unsafe_inventory = inventory.model_copy(
        update={"components": (_candidate().model_copy(update={"component_key": "../outside"}),)}
    )
    (tmp_path / "eval-001" / "component-inventory.json").write_text(
        json.dumps(unsafe_inventory.model_dump(mode="json")), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="safe logical identifier"):
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
        "lifecycle/transition-0001.json",
        "lifecycle/transition-0002.json",
        "lifecycle/transition-0003.json",
        "lifecycle/transition-0004.json",
    ])
    assert not list(run_dir.rglob("*.tmp"))
    assert json.loads((run_dir / "run-manifest.json").read_text("utf-8"))["status"] == "accepted"
    assert manifest.artifact_digests


@pytest.mark.asyncio
async def test_finalize_persists_unresolved_component_without_candidate_or_review(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")
    await store.stage_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=run.source,
            source_citations=("block-0001",),
            components=(_candidate(),),
        ),
    )

    manifest = await store.finalize(
        run.run_id,
        {"delivery-api": EvaluationOutcome.UNRESOLVED},
    )

    outcome = manifest.component_outcomes[0]
    assert manifest.status is EvaluationStatus.PARTIAL
    assert outcome.outcome is EvaluationOutcome.UNRESOLVED
    assert outcome.candidate is None
    assert outcome.review is None


@pytest.mark.asyncio
async def test_store_enforces_persisted_round_limit_without_tool_service(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(
        _source(),
        run_id="eval-001",
        idempotency_key="input-v1",
        max_rounds=1,
    )
    await store.stage_inventory(
        run.run_id,
        ComponentInventoryCandidate(
            inventory_id="inventory-001",
            source=run.source,
            source_citations=("block-0001",),
            components=(_candidate(),),
        ),
    )

    with pytest.raises(EvaluationConflictError, match="round limit"):
        await store.stage_candidate(run.run_id, _candidate(revision=2))
    with pytest.raises(EvaluationConflictError, match="round limit"):
        await store.record_review(
            run.run_id,
            QaReview(
                component_key="delivery-api",
                revision=2,
                decision="approved",
                score=7,
                defect_codes=(),
                revision_requests=(),
            ),
        )
    with pytest.raises(EvaluationConflictError, match="round limit"):
        await store.consume_slice(
            run.run_id,
            slice_kind="component",
            component_key="delivery-api",
            revision=2,
        )


@pytest.mark.asyncio
async def test_store_finalizes_failed_run_without_inventory(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(_source(), run_id="eval-001", idempotency_key="input-v1")

    manifest = await store.finalize(run.run_id, EvaluationOutcome.FAILED)

    assert manifest.status is EvaluationStatus.FAILED
    assert manifest.component_outcomes == ()
    assert (tmp_path / "eval-001" / "run-manifest.json").is_file()
    assert (tmp_path / "eval-001" / "evaluation.md").is_file()


@pytest.mark.asyncio
async def test_store_persists_typed_provider_telemetry_in_terminal_manifest(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    await store.create_run(
        _source(),
        run_id="eval-telemetry",
        idempotency_key="input-v1",
        max_calls=4,
    )

    manifest = await store.finalize(
        "eval-telemetry",
        EvaluationOutcome.FAILED,
        telemetry=EvaluationTelemetry(
            model_identifier="gpt-live-test",
            prompt_version="agentfarm-evaluation-v1",
            call_count=4,
            token_total=123,
            cost_total=None,
        ),
    )

    persisted = EvaluationManifest.model_validate_json(
        (tmp_path / "eval-telemetry" / "run-manifest.json").read_bytes()
    )
    assert manifest == persisted
    assert persisted.model_identifier == "gpt-live-test"
    assert persisted.call_count == 4
    assert persisted.token_total == 123
    assert persisted.cost_total is None


@pytest.mark.asyncio
async def test_provider_call_reservations_are_restart_durable_and_run_authoritative(
    tmp_path: Path,
) -> None:
    store = JsonEvaluationStore(tmp_path)
    await store.create_run(
        _source(),
        run_id="eval-provider-budget",
        idempotency_key="input-v1",
        max_calls=4,
    )

    for expected_index in range(1, 5):
        receipt = await store.reserve_provider_call(
            "eval-provider-budget",
            model_identifier="gpt-test",
        )
        assert receipt.call_index == expected_index

    restarted = JsonEvaluationStore(tmp_path)
    with pytest.raises(EvaluationConflictError, match="provider call budget"):
        await restarted.reserve_provider_call(
            "eval-provider-budget",
            model_identifier="gpt-test",
        )

    telemetry = restarted.provider_telemetry(
        "eval-provider-budget",
        prompt_version="agentfarm-evaluation-v1",
    )
    assert telemetry.call_count == 4
    assert telemetry.token_total == 0
    assert telemetry.cost_total is None


@pytest.mark.asyncio
async def test_finalize_rejects_telemetry_above_persisted_provider_budget(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    await store.create_run(
        _source(),
        run_id="eval-telemetry-over-budget",
        idempotency_key="input-v1",
        max_calls=4,
    )

    with pytest.raises(EvaluationConflictError, match="telemetry exceeds"):
        await store.finalize(
            "eval-telemetry-over-budget",
            EvaluationOutcome.FAILED,
            telemetry=EvaluationTelemetry(
                model_identifier="gpt-test",
                prompt_version="agentfarm-evaluation-v1",
                call_count=5,
                token_total=10,
                cost_total=None,
            ),
        )


@pytest.mark.asyncio
async def test_store_enforces_persisted_component_limit_at_inventory_boundary(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    await store.create_run(
        _source(),
        run_id="eval-component-limit",
        idempotency_key="input-v1",
        max_components=1,
    )
    second = _candidate().model_copy(update={"component_key": "second-component"})
    inventory = ComponentInventoryCandidate(
        inventory_id="inventory-too-large",
        source=_source(),
        source_citations=("block-0001",),
        components=(_candidate(), second),
    )

    with pytest.raises(EvaluationConflictError, match="component limit"):
        await store.stage_inventory("eval-component-limit", inventory)

    assert not (tmp_path / "eval-component-limit" / "component-inventory.json").exists()
