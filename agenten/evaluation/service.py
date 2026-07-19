"""Captain-owned bounded coordinator for persisted evaluation receipts."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable
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
    EvaluationStatus,
    EvaluationTelemetry,
    QaReview,
)
from .society import build_evaluation_society, build_qa_review_team
from .store import EvaluationConflictError, JsonEvaluationStore
from .tools import EvaluationToolService
from .validation import validate_component_graph


_SAFE_TOOL_REASON_CODES = frozenset(
    {
        "missing_scope",
        "missing_non_goals",
        "missing_team_roles",
        "missing_implementation_steps",
        "missing_interfaces",
        "missing_acceptance_tests",
        "missing_definition_of_done",
        "missing_risks",
        "missing_source_citations",
        "missing_setup",
        "missing_action",
        "missing_expected",
        "missing_command",
        "missing_source_citation",
        "duplicate_component_key",
        "unknown_dependency",
        "dependency_cycle",
        "qa_component_mismatch",
        "invalid_qa_approval",
        "missing_revision_request",
        "unknown_rubric_code",
        "false_execution_claim",
    }
)


class EvaluationSociety(Protocol):
    """Small injected seam shared by AutoGen and deterministic scripted tests."""

    async def run(self, *, task: str) -> object: ...


class EvaluationSliceFailure(RuntimeError):
    """A bounded provider-backed slice failed after Captain persisted its budget."""


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
        max_components: int = 1,
        max_rounds: int = 3,
        max_calls: int = 10,
        society: EvaluationSociety | None = None,
        qa_society: EvaluationSociety | None = None,
        summary_model_client: ChatCompletionClient | None = None,
        telemetry: Callable[[], EvaluationTelemetry] | None = None,
    ) -> None:
        if (
            isinstance(max_components, bool)
            or not isinstance(max_components, int)
            or max_components < 1
        ):
            raise ValueError("max_components must be positive")
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
        self._max_components = max_components
        self._max_rounds = max_rounds
        self._max_calls = max_calls
        self._telemetry = telemetry
        self._society = society or build_evaluation_society(
            model_client=model_client,
            summary_model_client=summary_model_client,
            tools=tools,
            max_rounds=max_rounds,
        )
        self._qa_society = qa_society or build_qa_review_team(
            model_client=model_client,
            tools=tools,
        )

    async def run(self, run_id: str) -> EvaluationManifest:
        """Run or resume one evaluation without consulting Society transcript state."""

        try:
            return await self._run_authoritative(run_id)
        except EvaluationSliceFailure:
            return await self._finalize_failure(run_id)

    async def _run_authoritative(self, run_id: str) -> EvaluationManifest:
        """Execute the receipt-driven lifecycle after validating run identity."""

        self._store._safe_id(run_id)
        run = await self._load_or_create_run(run_id)
        if (self._store._run_dir(run_id) / "run-manifest.json").is_file():
            return await self._store.load_manifest(run_id)

        lifecycle = self._store.lifecycle_events(run_id)[-1]
        terminal_recovery = lifecycle.recovery_state == "terminal"
        if lifecycle.recovery_state == "cancelled":
            await self._store.transition_run(run_id, lifecycle.status, "resuming")

        max_rounds = run.max_rounds
        max_calls = run.max_calls
        inventory = self._optional_inventory(run_id)

        if inventory is None:
            if self._store.consumed_slice_count(run_id) >= max_calls:
                return await self._finalize(run_id, EvaluationOutcome.FAILED)
            await self._store.transition_run(run_id, EvaluationStatus.INVENTORYING)
            await self._run_slice(
                run,
                "inventory",
                self._inventory_task(run),
                runner=self._society,
            )
            inventory = self._optional_inventory(run_id)
            if inventory is None:
                return await self._finalize(run_id, EvaluationOutcome.FAILED)

        if not terminal_recovery:
            lifecycle = self._store.lifecycle_events(run_id)[-1]
            if lifecycle.status == EvaluationStatus.CREATED:
                await self._store.transition_run(run_id, EvaluationStatus.INVENTORYING)
            await self._store.transition_run(run_id, EvaluationStatus.PLANNING)

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
                        runner=self._qa_society,
                        component_key=inventory_item.component_key,
                        revision=candidate.revision,
                    )
                    review = self._optional_model(
                        self._review_path(run_id, inventory_item.component_key, candidate.revision),
                        QaReview,
                    )
                    if review is None:
                        break
                    continue
                if candidate is not None and review is not None and candidate.revision >= max_rounds:
                    break
                if self._store.consumed_slice_count(run_id) >= max_calls:
                    break
                revision = 1 if candidate is None else candidate.revision + 1
                await self._run_slice(
                    run,
                    "component",
                    self._component_task(run.run_id, inventory_item, revision),
                    runner=self._society,
                    component_key=inventory_item.component_key,
                    revision=revision,
                )
                candidate = self._optional_model(
                    self._candidate_path(run_id, inventory_item.component_key, revision),
                    ComponentPlanCandidate,
                )
                review = self._optional_model(
                    self._review_path(run_id, inventory_item.component_key, revision),
                    QaReview,
                )
                if candidate is None or review is None:
                    break

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
        return await self._finalize(run_id, outcomes)

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
                max_components=self._max_components,
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
        runner: EvaluationSociety,
        component_key: str | None = None,
        revision: int | None = None,
    ) -> None:
        await self._store.consume_slice(
            run.run_id,
            slice_kind=slice_kind,
            component_key=component_key,
            revision=revision,
        )
        try:
            result = await runner.run(task=task)
            await self._record_tool_execution_observations(run.run_id, result)
        except asyncio.CancelledError:
            phase = self._store.lifecycle_events(run.run_id)[-1].status
            await asyncio.shield(self._store.transition_run(run.run_id, phase, "cancelled"))
            raise
        except Exception as exc:
            raise EvaluationSliceFailure("evaluation slice failed") from exc

    async def _finalize_failure(self, run_id: str) -> EvaluationManifest:
        inventory = self._optional_inventory(run_id)
        outcome: EvaluationOutcome | dict[str, EvaluationOutcome]
        if inventory is None:
            outcome = EvaluationOutcome.FAILED
        else:
            outcome = {
                component.component_key: EvaluationOutcome.FAILED
                for component in inventory.components
            }
        return await self._finalize(run_id, outcome)

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
        block = max(
            run.source.blocks,
            key=lambda item: (
                _inventory_block_priority(item.heading_path),
                -item.line_start,
            ),
        )
        return (
            f"INVENTORY_SLICE run_id={run.run_id} "
            f"required_source_block={block.block_id} "
            f"source_citations=[{block.block_id}] "
            f"max_components={run.max_components} "
            f"component_key={_component_key_for_block(block.heading_path, block.block_id)} "
            "revision=1 dependencies=[]"
        )

    async def _finalize(
        self,
        run_id: str,
        outcome: EvaluationOutcome | dict[str, EvaluationOutcome],
    ) -> EvaluationManifest:
        telemetry = self._telemetry() if self._telemetry is not None else None
        return await self._store.finalize(run_id, outcome, telemetry=telemetry)

    @staticmethod
    def _component_task(
        run_id: str,
        inventory_item: ComponentPlanCandidate,
        revision: int,
    ) -> str:
        return (
            f"COMPONENT_SLICE run_id={run_id} component_key={inventory_item.component_key} "
            f"revision={revision} source_citations=[{','.join(inventory_item.source_citations)}] "
            f"dependencies=[{','.join(inventory_item.dependencies)}]"
        )

    @staticmethod
    def _qa_task(run_id: str, component_key: str, revision: int) -> str:
        return f"QA_SLICE run_id={run_id} component_key={component_key} revision={revision}"

    def _candidate_path(self, run_id: str, component_key: str, revision: int) -> Path:
        return self._store._run_dir(run_id) / "candidates" / component_key / f"revision-{revision}.json"

    def _review_path(self, run_id: str, component_key: str, revision: int) -> Path:
        return self._store._run_dir(run_id) / "qa-reviews" / component_key / f"revision-{revision}.json"

    async def _record_tool_execution_observations(self, run_id: str, result: object) -> None:
        """Persist only structural tool outcomes from an AutoGen task result."""

        messages = getattr(result, "messages", ())
        for message in messages if isinstance(messages, (tuple, list)) else ():
            content = getattr(message, "content", ())
            for item in content if isinstance(content, (tuple, list)) else ():
                tool_name = getattr(item, "name", None)
                if tool_name not in {
                    "read_source_block",
                    "stage_component_inventory",
                    "stage_component_plan",
                    "record_qa_review",
                }:
                    continue
                if getattr(item, "is_error", False):
                    error = str(getattr(item, "content", "")).lower()
                    reason_codes: tuple[str, ...] = ()
                    if "validation error" in error:
                        outcome = "schema_rejected"
                    elif "evaluation tool input is invalid" in error:
                        outcome = "validation_rejected"
                        reason_codes = tuple(
                            sorted(
                                {
                                    code
                                    for code in re.findall(r"[a-z_]+", error)
                                    if code in _SAFE_TOOL_REASON_CODES
                                }
                            )
                        )
                    elif "unable to stage" in error or "has not been staged" in error:
                        outcome = "staging_rejected"
                    else:
                        outcome = "unexpected_error"
                else:
                    outcome = "succeeded"
                    reason_codes = ()
                await self._store.record_tool_execution(
                    run_id,
                    tool_name=tool_name,
                    outcome=outcome,
                    reason_codes=reason_codes,
                )

    @staticmethod
    def _optional_model(path: Path, model_type: type[EvaluationManifest] | type[ComponentInventoryCandidate] | type[ComponentPlanCandidate] | type[QaReview]):
        if not path.is_file():
            return None
        return model_type.model_validate_json(path.read_bytes())


def _inventory_block_priority(heading_path: tuple[str, ...]) -> int:
    """Choose a deterministic implementation-oriented block for a bounded smoke slice."""

    heading = " ".join(heading_path).lower()
    return sum(
        weight
        for token, weight in (
            ("phase 1", 8),
            ("foundation", 6),
            ("implementation guide", 4),
            ("getting started", 2),
        )
        if token in heading
    )


def _component_key_for_block(heading_path: tuple[str, ...], block_id: str) -> str:
    """Derive a stable safe component identity before the LLM starts planning."""

    normalized = unicodedata.normalize("NFKD", " ".join(heading_path)).encode(
        "ascii", "ignore"
    ).decode("ascii")
    key = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return key or f"component-{block_id.removeprefix('block-')}"
