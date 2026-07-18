"""Captain-owned, planning-only adapters for evaluation artifacts."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    CandidateReceipt,
    ComponentInventoryCandidate,
    ComponentPlanCandidate,
    EvaluationRun,
    InventoryReceipt,
    QaReview,
    ReviewReceipt,
)
from .redaction import CREDENTIAL_ASSIGNMENT, redact_text
from .store import EvaluationConflictError, JsonEvaluationStore
from .validation import validate_candidate, validate_inventory


_BLOCK_ID = re.compile(r"^block-[0-9]{4}$")


class EvaluationToolError(ValueError):
    """A tool input cannot be safely staged as non-authoritative evidence."""


class SourceBlockView(BaseModel):
    """A redacted, path-free view of one immutable evaluation source block."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    block_id: str = Field(pattern=r"^block-[0-9]{4}$")
    heading_path: tuple[str, ...] = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    text: str = Field(min_length=1)


class EvaluationToolService:
    """Expose only validation-gated reads and append-only staging receipts.

    This intentionally has no finalization, capability, filesystem, or external
    service surface. Captain retains authority over those operations.
    """

    def __init__(self, store: JsonEvaluationStore) -> None:
        self._store = store

    async def read_source_block(self, run_id: str, block_id: str) -> SourceBlockView:
        run = self._run(run_id)
        if not isinstance(block_id, str) or not _BLOCK_ID.fullmatch(block_id):
            raise EvaluationToolError("source block identifier is invalid")
        block = next((item for item in run.source.blocks if item.block_id == block_id), None)
        if block is None:
            raise EvaluationToolError("source block is not part of this run")
        return SourceBlockView(
            block_id=block.block_id,
            heading_path=block.heading_path,
            line_start=block.line_start,
            line_end=block.line_end,
            text=_redacted_view_text(block.text),
        )

    async def stage_component_inventory(
        self,
        run_id: str,
        inventory: ComponentInventoryCandidate,
    ) -> InventoryReceipt:
        run = self._run(run_id)
        if inventory.source != run.source:
            raise EvaluationToolError("inventory source does not belong to this run")
        for candidate in inventory.components:
            self._safe_component_key(candidate.component_key)
        self._raise_validation_issues(validate_inventory(inventory))
        try:
            return await self._store.stage_inventory(run_id, inventory)
        except (EvaluationConflictError, ValueError) as error:
            raise EvaluationToolError(str(error)) from error

    async def stage_component_plan(
        self,
        run_id: str,
        candidate: ComponentPlanCandidate,
    ) -> CandidateReceipt:
        run = self._run(run_id)
        self._safe_component_key(candidate.component_key)
        self._validate_round(candidate.revision)
        inventory = self._inventory(run_id)
        if not any(
            item.component_key == candidate.component_key and item.revision == candidate.revision
            for item in inventory.components
        ):
            raise EvaluationToolError("candidate does not belong to the staged inventory")
        expected_revision = self._expected_revision(run_id, candidate.component_key)
        if candidate.revision != expected_revision:
            raise EvaluationToolError(f"expected revision {expected_revision} for component candidate")
        self._raise_validation_issues(validate_candidate(candidate, run.source))
        try:
            return await self._store.stage_candidate(run_id, candidate)
        except (EvaluationConflictError, ValueError) as error:
            raise EvaluationToolError(str(error)) from error

    async def record_qa_review(self, run_id: str, review: QaReview) -> ReviewReceipt:
        run = self._run(run_id)
        self._safe_component_key(review.component_key)
        self._validate_round(review.revision)
        candidate = self._candidate(run_id, review.component_key, review.revision)
        checked_candidate = candidate.model_copy(update={"qa_reviews": (review,)})
        self._raise_validation_issues(validate_candidate(checked_candidate, run.source))
        try:
            return await self._store.record_review(run_id, review)
        except (EvaluationConflictError, ValueError) as error:
            raise EvaluationToolError(str(error)) from error

    def _run(self, run_id: str) -> EvaluationRun:
        self._safe_run_id(run_id)
        try:
            return self._store._read_run(run_id)
        except (EvaluationConflictError, ValueError) as error:
            raise EvaluationToolError("evaluation run is unavailable") from error

    def _inventory(self, run_id: str) -> ComponentInventoryCandidate:
        try:
            return self._store._read_model(
                self._store._run_dir(run_id) / "component-inventory.json",
                ComponentInventoryCandidate,
            )
        except EvaluationConflictError as error:
            raise EvaluationToolError("component inventory has not been staged") from error

    def _candidate(self, run_id: str, component_key: str, revision: int) -> ComponentPlanCandidate:
        try:
            return self._store._read_model(
                self._store._run_dir(run_id) / "candidates" / component_key / f"revision-{revision}.json",
                ComponentPlanCandidate,
            )
        except EvaluationConflictError as error:
            raise EvaluationToolError("component candidate has not been staged") from error

    def _expected_revision(self, run_id: str, component_key: str) -> int:
        for revision in range(1, 4):
            try:
                self._candidate(run_id, component_key, revision)
            except EvaluationToolError:
                return revision
        raise EvaluationToolError("three-round ceiling prevents another component candidate")

    @staticmethod
    def _safe_run_id(run_id: str) -> None:
        if not isinstance(run_id, str):
            raise EvaluationToolError("artifact identity must be a safe logical identifier")
        try:
            JsonEvaluationStore._safe_id(run_id)
        except ValueError as error:
            raise EvaluationToolError(str(error)) from error

    @staticmethod
    def _safe_component_key(component_key: str) -> None:
        if not isinstance(component_key, str):
            raise EvaluationToolError("artifact identity must be a safe logical identifier")
        try:
            JsonEvaluationStore._safe_id(component_key)
        except ValueError as error:
            raise EvaluationToolError(str(error)) from error

    @staticmethod
    def _validate_round(revision: int) -> None:
        if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision <= 3:
            raise EvaluationToolError("three-round ceiling prevents this revision")

    @staticmethod
    def _raise_validation_issues(issues: tuple[object, ...]) -> None:
        if issues:
            codes = ", ".join(getattr(issue, "code", "invalid_tool_input") for issue in issues)
            raise EvaluationToolError(f"evaluation tool input is invalid: {codes}")


def _redacted_view_text(text: str) -> str:
    """Remove credential assignment identifiers as well as values from tool views."""

    return CREDENTIAL_ASSIGNMENT.sub("[REDACTED]", redact_text(text))
