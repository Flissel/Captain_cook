"""Captain-owned bounded coordinator for persisted evaluation receipts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from autogen_core.models import ChatCompletionClient

from .models import (
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationRun,
    EvaluationSource,
    QaReview,
)
from .society import build_evaluation_society
from .store import EvaluationConflictError, JsonEvaluationStore
from .tools import EvaluationToolService
from .validation import validate_component_graph


class EvaluationSociety(Protocol):
    """Small injected seam shared by AutoGen and deterministic scripted tests."""

    async def run(self, *, task: str) -> object: ...


class AgentFarmEvaluationService:
    """Schedule bounded slices and derive authority only from stored artifacts."""

    def __init__(
        self,
        *,
        model_client: ChatCompletionClient,
        tools: EvaluationToolService,
        store: JsonEvaluationStore,
        source: EvaluationSource | None = None,
        idempotency_key: str | None = None,
        max_rounds: int = 3,
        max_calls: int = 10,
        society: EvaluationSociety | None = None,
    ) -> None:
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 3:
            raise ValueError("max_rounds must be between one and three")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("max_calls must be positive")
        if source is not None and not idempotency_key:
            raise ValueError("idempotency_key is required when creating a run")
        self._model_client = model_client
        self._tools = tools
        self._store = store
        self._source = source
        self._idempotency_key = idempotency_key
        self._max_rounds = max_rounds
        self._max_calls = max_calls
        self._society = society or build_evaluation_society(
            model_client=model_client,
            tools=tools,
            max_rounds=max_rounds,
        )

    async def run(self, run_id: str) -> EvaluationManifest:
        """Run or resume one evaluation without consulting Society transcript state."""

        self._store._safe_id(run_id)
        run = await self._load_or_create_run(run_id)
        if (self._store._run_dir(run_id) / "run-manifest.json").is_file():
            return await self._store.load_manifest(run_id)

        max_rounds = run.max_rounds
        max_calls = run.max_calls
        inventory = self._optional_inventory(run_id)

        if inventory is None:
            await self._run_slice(run, "inventory", self._inventory_task(run))
            inventory = self._required_inventory(run_id)

        outcomes: dict[str, EvaluationOutcome] = {}
        latest_candidates: list[ComponentPlanCandidate] = []
        for inventory_item in inventory.components:
            candidate, review = self._latest_round(run_id, inventory_item.component_key, max_rounds)
            while review is None or review.decision != "approved":
                if candidate is not None and review is None:
                    if self._store.consumed_slice_count(run_id) >= max_calls:
                        break
                    await self._run_slice(
                        run,
                        "qa",
                        self._qa_task(run.run_id, inventory_item.component_key, candidate.revision),
                        component_key=inventory_item.component_key,
                        revision=candidate.revision,
                    )
                    review = self._required_review(
                        run_id,
                        inventory_item.component_key,
                        candidate.revision,
                    )
                    continue
                if candidate is not None and review is not None and candidate.revision >= max_rounds:
                    break
                if self._store.consumed_slice_count(run_id) >= max_calls:
                    break
                revision = 1 if candidate is None else candidate.revision + 1
                await self._run_slice(
                    run,
                    "component",
                    self._component_task(run.run_id, inventory_item.component_key, revision),
                    component_key=inventory_item.component_key,
                    revision=revision,
                )
                candidate = self._required_candidate(run_id, inventory_item.component_key, revision)
                review = self._required_review(run_id, inventory_item.component_key, revision)

            if candidate is None or review is None:
                outcomes[inventory_item.component_key] = EvaluationOutcome.UNRESOLVED
                continue
            latest_candidates.append(candidate)
            outcomes[inventory_item.component_key] = (
                EvaluationOutcome.ACCEPTED
                if review.decision == "approved"
                else EvaluationOutcome.UNRESOLVED
            )

        if validate_component_graph(tuple(latest_candidates)):
            outcomes = {key: EvaluationOutcome.FAILED for key in outcomes}
        return await self._store.finalize(run_id, outcomes)

    async def _load_or_create_run(self, run_id: str) -> EvaluationRun:
        try:
            run = self._store._read_run(run_id)
        except EvaluationConflictError:
            if self._source is None or self._idempotency_key is None:
                raise EvaluationConflictError("evaluation run is unavailable for resume") from None
            return await self._store.create_run(
                self._source,
                run_id=run_id,
                idempotency_key=self._idempotency_key,
                max_rounds=self._max_rounds,
                max_calls=self._max_calls,
            )
        if self._source is not None and run.source != self._source:
            raise EvaluationConflictError("persisted evaluation source differs from requested source")
        if self._idempotency_key is not None and run.idempotency_key != self._idempotency_key:
            raise EvaluationConflictError("persisted evaluation idempotency key differs from requested key")
        return run

    async def _run_slice(
        self,
        run: EvaluationRun,
        slice_kind: str,
        task: str,
        *,
        component_key: str | None = None,
        revision: int | None = None,
    ) -> None:
        await self._store.consume_slice(
            run.run_id,
            slice_kind=slice_kind,
            component_key=component_key,
            revision=revision,
        )
        await self._society.run(task=task)

    def _optional_inventory(self, run_id: str) -> ComponentInventoryCandidate | None:
        return self._optional_model(
            self._store._run_dir(run_id) / "component-inventory.json",
            ComponentInventoryCandidate,
        )

    def _required_inventory(self, run_id: str) -> ComponentInventoryCandidate:
        return self._store._read_model(
            self._store._run_dir(run_id) / "component-inventory.json",
            ComponentInventoryCandidate,
        )

    def _latest_round(
        self,
        run_id: str,
        component_key: str,
        max_rounds: int,
    ) -> tuple[ComponentPlanCandidate | None, QaReview | None]:
        latest_candidate: ComponentPlanCandidate | None = None
        latest_review: QaReview | None = None
        for revision in range(1, max_rounds + 1):
            candidate = self._optional_model(
                self._candidate_path(run_id, component_key, revision),
                ComponentPlanCandidate,
            )
            review = self._optional_model(
                self._review_path(run_id, component_key, revision),
                QaReview,
            )
            if review is not None and candidate is None:
                raise EvaluationConflictError("QA review exists without its candidate")
            if candidate is None:
                break
            latest_candidate, latest_review = candidate, review
            if review is None:
                break
        return latest_candidate, latest_review

    def _required_candidate(self, run_id: str, component_key: str, revision: int) -> ComponentPlanCandidate:
        return self._store._read_model(
            self._candidate_path(run_id, component_key, revision),
            ComponentPlanCandidate,
        )

    def _required_review(self, run_id: str, component_key: str, revision: int) -> QaReview:
        return self._store._read_model(
            self._review_path(run_id, component_key, revision),
            QaReview,
        )

    @staticmethod
    def _inventory_task(run: EvaluationRun) -> str:
        block_ids = ",".join(block.block_id for block in run.source.blocks)
        return f"INVENTORY_SLICE run_id={run.run_id} source_blocks={block_ids}"

    @staticmethod
    def _component_task(run_id: str, component_key: str, revision: int) -> str:
        return (
            f"COMPONENT_SLICE run_id={run_id} component_key={component_key} "
            f"revision={revision}"
        )

    @staticmethod
    def _qa_task(run_id: str, component_key: str, revision: int) -> str:
        return f"QA_SLICE run_id={run_id} component_key={component_key} revision={revision}"

    def _candidate_path(self, run_id: str, component_key: str, revision: int) -> Path:
        return self._store._run_dir(run_id) / "candidates" / component_key / f"revision-{revision}.json"

    def _review_path(self, run_id: str, component_key: str, revision: int) -> Path:
        return self._store._run_dir(run_id) / "qa-reviews" / component_key / f"revision-{revision}.json"

    @staticmethod
    def _optional_model(path: Path, model_type: type[EvaluationManifest] | type[ComponentInventoryCandidate] | type[ComponentPlanCandidate] | type[QaReview]):
        if not path.is_file():
            return None
        return model_type.model_validate_json(path.read_bytes())
