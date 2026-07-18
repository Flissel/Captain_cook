"""Captain-owned append-only JSON artifacts for deterministic evaluations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .models import (
    CandidateReceipt,
    ComponentInventoryCandidate,
    ComponentOutcome,
    ComponentPlanCandidate,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationRun,
    EvaluationStatus,
    EvaluationSource,
    InventoryReceipt,
    QaReview,
    ReviewReceipt,
)
from .redaction import redact_model, redact_text
from .report import render_evaluation_markdown


class EvaluationConflictError(RuntimeError):
    """An append-only artifact identity already has different content."""


_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_Model = TypeVar("_Model", bound=BaseModel)


class JsonEvaluationStore:
    """One-process atomic, append-only store; Captain is its sole caller."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    async def create_run(
        self,
        source: EvaluationSource,
        *,
        run_id: str,
        idempotency_key: str,
        max_rounds: int = 3,
        max_calls: int = 1,
    ) -> EvaluationRun:
        self._safe_id(run_id)
        source = redact_model(source)
        run = EvaluationRun(
            run_id=run_id,
            idempotency_key=redact_text(idempotency_key),
            source=source,
            status=EvaluationStatus.CREATED,
            max_rounds=max_rounds,
            max_calls=max_calls,
        )
        async with self._lock:
            stored = self._write_model(self._run_dir(run_id) / "source-manifest.json", run)
        return EvaluationRun.model_validate_json(stored)

    async def stage_inventory(self, run_id: str, inventory: ComponentInventoryCandidate) -> InventoryReceipt:
        async with self._lock:
            run = self._read_run(run_id)
            inventory = redact_model(inventory)
            if inventory.source != run.source:
                raise EvaluationConflictError("inventory source differs from source manifest")
            for candidate in inventory.components:
                self._safe_id(candidate.component_key)
            relative = "component-inventory.json"
            stored = self._write_model(self._run_dir(run_id) / relative, inventory)
        return InventoryReceipt(run_id=run_id, inventory_id=inventory.inventory_id, artifact_reference=relative, sha256=_digest(stored))

    async def stage_candidate(self, run_id: str, candidate: ComponentPlanCandidate) -> CandidateReceipt:
        self._safe_id(candidate.component_key)
        async with self._lock:
            self._read_run(run_id)
            inventory = self._read_model(self._run_dir(run_id) / "component-inventory.json", ComponentInventoryCandidate)
            candidate = redact_model(candidate)
            if not any(declared.component_key == candidate.component_key for declared in inventory.components):
                raise EvaluationConflictError("candidate does not belong to declared inventory")
            relative = f"candidates/{candidate.component_key}/revision-{candidate.revision}.json"
            stored = self._write_model(self._run_dir(run_id) / relative, candidate)
        return CandidateReceipt(run_id=run_id, component_key=candidate.component_key, revision=candidate.revision, artifact_reference=relative, sha256=_digest(stored))

    async def record_review(self, run_id: str, review: QaReview) -> ReviewReceipt:
        self._safe_id(run_id)
        self._safe_id(review.component_key)
        async with self._lock:
            self._read_run(run_id)
            candidate_path = self._run_dir(run_id) / "candidates" / review.component_key / f"revision-{review.revision}.json"
            self._read_model(candidate_path, ComponentPlanCandidate)
            review = redact_model(review)
            relative = f"qa-reviews/{review.component_key}/revision-{review.revision}.json"
            stored = self._write_model(self._run_dir(run_id) / relative, review)
        return ReviewReceipt(run_id=run_id, component_key=review.component_key, revision=review.revision, artifact_reference=relative, sha256=_digest(stored))

    async def finalize(
        self,
        run_id: str,
        outcome: EvaluationOutcome | Mapping[str, EvaluationOutcome],
    ) -> EvaluationManifest:
        async with self._lock:
            run = self._read_run(run_id)
            inventory = self._read_model(self._run_dir(run_id) / "component-inventory.json", ComponentInventoryCandidate)
            requested = (
                {candidate.component_key: outcome for candidate in inventory.components}
                if isinstance(outcome, EvaluationOutcome)
                else dict(outcome)
            )
            expected_keys = {candidate.component_key for candidate in inventory.components}
            if set(requested) != expected_keys:
                raise EvaluationConflictError("component outcomes must cover the staged inventory exactly")
            components = tuple(
                self._staged_component_outcome(run_id, candidate, requested[candidate.component_key])
                for candidate in inventory.components
            )
            status = _status_for_components(components)
            digests = tuple(self._artifact_digests(run_id))
            manifest = EvaluationManifest(
                run_id=run.run_id,
                idempotency_key=run.idempotency_key,
                status=status,
                source=run.source,
                component_outcomes=components,
                artifact_digests=digests,
            )
            stored = self._write_model(self._run_dir(run_id) / "run-manifest.json", manifest)
            persisted = EvaluationManifest.model_validate_json(stored)
            self._write_bytes(self._run_dir(run_id) / "evaluation.md", render_evaluation_markdown(persisted).encode("utf-8"))
        return persisted

    def _staged_component_outcome(self, run_id: str, inventory_candidate: ComponentPlanCandidate, outcome: EvaluationOutcome) -> ComponentOutcome:
        self._safe_id(inventory_candidate.component_key)
        first_path = self._run_dir(run_id) / "candidates" / inventory_candidate.component_key / "revision-1.json"
        first_candidate = self._read_model(first_path, ComponentPlanCandidate)
        if first_candidate != inventory_candidate:
            raise EvaluationConflictError("candidate artifact does not match staged inventory")
        candidate = first_candidate
        for revision in range(2, 4):
            candidate_path = self._run_dir(run_id) / "candidates" / inventory_candidate.component_key / f"revision-{revision}.json"
            if not candidate_path.is_file():
                break
            candidate = self._read_model(candidate_path, ComponentPlanCandidate)
        review_path = self._run_dir(run_id) / "qa-reviews" / candidate.component_key / f"revision-{candidate.revision}.json"
        review = self._read_model(review_path, QaReview)
        if outcome == EvaluationOutcome.ACCEPTED and review.decision != "approved":
            raise EvaluationConflictError("accepted component requires an approved persisted QA review")
        return ComponentOutcome(component_key=candidate.component_key, outcome=outcome, revision=candidate.revision, candidate=candidate, review=review)

    def _artifact_digests(self, run_id: str) -> list[str]:
        run_dir = self._run_dir(run_id)
        paths = sorted(path for path in run_dir.rglob("*.json") if path.name != "run-manifest.json")
        return [f"{path.relative_to(run_dir).as_posix()}:{_digest(path.read_bytes())}" for path in paths]

    def _read_run(self, run_id: str) -> EvaluationRun:
        self._safe_id(run_id)
        return self._read_model(self._run_dir(run_id) / "source-manifest.json", EvaluationRun)

    def _run_dir(self, run_id: str) -> Path:
        return self._root / run_id

    @staticmethod
    def _safe_id(value: str) -> None:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("artifact identity must be a safe logical identifier")

    def _read_model(self, path: Path, model_type: type[_Model]) -> _Model:
        if not path.exists():
            raise EvaluationConflictError(f"required artifact is missing: {path.name}")
        return model_type.model_validate_json(path.read_bytes())

    def _write_model(self, path: Path, model: BaseModel) -> bytes:
        return self._write_bytes(path, _canonical_json(model.model_dump(mode="json")))

    def _write_bytes(self, path: Path, content: bytes) -> bytes:
        if path.exists():
            existing = path.read_bytes()
            if existing != content:
                raise EvaluationConflictError(f"artifact {path.as_posix()} already staged differently")
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
        return content


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _status_for_components(components: tuple[ComponentOutcome, ...]) -> EvaluationStatus:
    outcomes = {component.outcome for component in components}
    if outcomes == {EvaluationOutcome.ACCEPTED}:
        return EvaluationStatus.ACCEPTED
    if EvaluationOutcome.FAILED in outcomes:
        return EvaluationStatus.FAILED
    return EvaluationStatus.PARTIAL
