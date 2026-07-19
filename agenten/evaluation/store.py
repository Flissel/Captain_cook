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
    EvaluationLifecycleEvent,
    EvaluationManifest,
    EvaluationOutcome,
    EvaluationRun,
    EvaluationSliceReceipt,
    EvaluationStatus,
    EvaluationSource,
    EvaluationTelemetry,
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
        max_components: int = 100,
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
            max_components=max_components,
            max_rounds=max_rounds,
            max_calls=max_calls,
        )
        async with self._lock:
            stored = self._write_model(self._run_dir(run_id) / "source-manifest.json", run)
            if not self._lifecycle_paths(run_id):
                self._append_lifecycle(run_id, EvaluationStatus.CREATED, "active")
        return EvaluationRun.model_validate_json(stored)

    async def stage_inventory(self, run_id: str, inventory: ComponentInventoryCandidate) -> InventoryReceipt:
        async with self._lock:
            run = self._read_run(run_id)
            inventory = redact_model(inventory)
            if inventory.source != run.source:
                raise EvaluationConflictError("inventory source differs from source manifest")
            if len(inventory.components) > run.max_components:
                raise EvaluationConflictError("inventory exceeds the persisted component limit")
            for candidate in inventory.components:
                self._safe_id(candidate.component_key)
            self._transition_lifecycle(run_id, EvaluationStatus.INVENTORYING, "active")
            relative = "component-inventory.json"
            stored = self._write_model(self._run_dir(run_id) / relative, inventory)
        return InventoryReceipt(run_id=run_id, inventory_id=inventory.inventory_id, artifact_reference=relative, sha256=_digest(stored))

    async def stage_candidate(self, run_id: str, candidate: ComponentPlanCandidate) -> CandidateReceipt:
        self._safe_id(candidate.component_key)
        async with self._lock:
            run = self._read_run(run_id)
            if candidate.revision > run.max_rounds:
                raise EvaluationConflictError("candidate revision exceeds the persisted round limit")
            inventory = self._read_model(self._run_dir(run_id) / "component-inventory.json", ComponentInventoryCandidate)
            candidate = redact_model(candidate)
            if not any(declared.component_key == candidate.component_key for declared in inventory.components):
                raise EvaluationConflictError("candidate does not belong to declared inventory")
            if candidate.revision > 1:
                previous = self._run_dir(run_id) / "candidates" / candidate.component_key / f"revision-{candidate.revision - 1}.json"
                self._read_model(previous, ComponentPlanCandidate)
            self._transition_lifecycle(run_id, EvaluationStatus.PLANNING, "active")
            relative = f"candidates/{candidate.component_key}/revision-{candidate.revision}.json"
            stored = self._write_model(self._run_dir(run_id) / relative, candidate)
        return CandidateReceipt(run_id=run_id, component_key=candidate.component_key, revision=candidate.revision, artifact_reference=relative, sha256=_digest(stored))

    async def record_review(self, run_id: str, review: QaReview) -> ReviewReceipt:
        self._safe_id(run_id)
        self._safe_id(review.component_key)
        async with self._lock:
            run = self._read_run(run_id)
            if review.revision > run.max_rounds:
                raise EvaluationConflictError("QA revision exceeds the persisted round limit")
            candidate_path = self._run_dir(run_id) / "candidates" / review.component_key / f"revision-{review.revision}.json"
            self._read_model(candidate_path, ComponentPlanCandidate)
            review = redact_model(review)
            relative = f"qa-reviews/{review.component_key}/revision-{review.revision}.json"
            stored = self._write_model(self._run_dir(run_id) / relative, review)
        return ReviewReceipt(run_id=run_id, component_key=review.component_key, revision=review.revision, artifact_reference=relative, sha256=_digest(stored))

    async def consume_slice(
        self,
        run_id: str,
        *,
        slice_kind: str,
        component_key: str | None = None,
        revision: int | None = None,
    ) -> EvaluationSliceReceipt:
        """Atomically consume one run-budget slot before starting Society work."""

        async with self._lock:
            run = self._read_run(run_id)
            if component_key is not None:
                self._safe_id(component_key)
            if revision is not None and revision > run.max_rounds:
                raise EvaluationConflictError("slice revision exceeds the persisted round limit")
            index = self._consumed_slice_count(run_id) + 1
            if index > run.max_calls:
                raise EvaluationConflictError("evaluation call budget is exhausted")
            receipt = EvaluationSliceReceipt(
                run_id=run_id,
                slice_index=index,
                slice_kind=slice_kind,
                component_key=component_key,
                revision=revision,
            )
            relative = f"slices/slice-{index:04d}.json"
            stored = self._write_model(self._run_dir(run_id) / relative, receipt)
        return EvaluationSliceReceipt.model_validate_json(stored)

    def consumed_slice_count(self, run_id: str) -> int:
        self._safe_id(run_id)
        return self._consumed_slice_count(run_id)

    async def transition_run(
        self,
        run_id: str,
        status: EvaluationStatus,
        recovery_state: str = "active",
    ) -> EvaluationLifecycleEvent:
        """Append one atomic lifecycle transition after validating its predecessor."""

        async with self._lock:
            self._read_run(run_id)
            return self._transition_lifecycle(run_id, status, recovery_state)

    def lifecycle_events(self, run_id: str) -> tuple[EvaluationLifecycleEvent, ...]:
        self._safe_id(run_id)
        return tuple(
            self._read_model(path, EvaluationLifecycleEvent)
            for path in self._lifecycle_paths(run_id)
        )

    async def finalize(
        self,
        run_id: str,
        outcome: EvaluationOutcome | Mapping[str, EvaluationOutcome],
        *,
        telemetry: EvaluationTelemetry | None = None,
    ) -> EvaluationManifest:
        async with self._lock:
            run = self._read_run(run_id)
            inventory = self._optional_inventory(run_id)
            if inventory is None:
                if outcome != EvaluationOutcome.FAILED:
                    raise EvaluationConflictError("component-inventory is missing and can only finalize as failed")
                components: tuple[ComponentOutcome, ...] = ()
                status = EvaluationStatus.FAILED
            else:
                current = self._latest_lifecycle(run_id)
                if current.recovery_state != "terminal":
                    self._transition_lifecycle(run_id, EvaluationStatus.PLANNING, "active")
                requested = (
                    {candidate.component_key: outcome for candidate in inventory.components}
                    if isinstance(outcome, EvaluationOutcome)
                    else dict(outcome)
                )
                expected_keys = {candidate.component_key for candidate in inventory.components}
                if set(requested) != expected_keys:
                    raise EvaluationConflictError("component outcomes must cover the staged inventory exactly")
                components = tuple(
                    self._staged_component_outcome(run, candidate, requested[candidate.component_key])
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
                model_identifier=(
                    telemetry.model_identifier if telemetry is not None else "not-configured"
                ),
                prompt_version=(
                    telemetry.prompt_version if telemetry is not None else "not-configured"
                ),
                call_count=telemetry.call_count if telemetry is not None else 0,
                token_total=telemetry.token_total if telemetry is not None else 0,
                cost_total=telemetry.cost_total if telemetry is not None else 0.0,
                artifact_digests=digests,
            )
            report = render_evaluation_markdown(manifest).encode("utf-8")
            self._write_bytes(self._run_dir(run_id) / "evaluation.md", report)
            stored = self._write_model(self._run_dir(run_id) / "run-manifest.json", manifest)
            persisted = EvaluationManifest.model_validate_json(stored)
            self._transition_lifecycle(run_id, status, "terminal")
        return persisted

    async def load_manifest(self, run_id: str) -> EvaluationManifest:
        """Validate a finalized projection and recover its deterministic report."""

        async with self._lock:
            run = self._read_run(run_id)
            inventory = self._optional_inventory(run_id)
            manifest = self._read_model(self._run_dir(run_id) / "run-manifest.json", EvaluationManifest)
            if manifest.run_id != run.run_id or manifest.idempotency_key != run.idempotency_key or manifest.source != run.source:
                raise EvaluationConflictError("run manifest identity does not match source manifest")
            outcomes_by_key = {component.component_key: component for component in manifest.component_outcomes}
            expected_keys = {candidate.component_key for candidate in inventory.components} if inventory is not None else set()
            if set(outcomes_by_key) != expected_keys:
                raise EvaluationConflictError("run manifest does not cover the staged inventory")
            expected_components = (
                tuple(
                    self._staged_component_outcome(
                        run,
                        candidate,
                        outcomes_by_key[candidate.component_key].outcome,
                    )
                    for candidate in inventory.components
                )
                if inventory is not None
                else ()
            )
            if manifest.component_outcomes != expected_components:
                raise EvaluationConflictError("run manifest component evidence is inconsistent")
            expected_status = _status_for_components(expected_components) if inventory is not None else EvaluationStatus.FAILED
            if manifest.status != expected_status:
                raise EvaluationConflictError("run manifest status is inconsistent")
            if manifest.artifact_digests != tuple(self._artifact_digests(run_id)):
                raise EvaluationConflictError("run manifest artifact digests are inconsistent")
            self._write_bytes(
                self._run_dir(run_id) / "evaluation.md",
                render_evaluation_markdown(manifest).encode("utf-8"),
            )
            current = self._latest_lifecycle(run_id)
            if current.status != manifest.status or current.recovery_state != "terminal":
                self._transition_lifecycle(run_id, manifest.status, "terminal")
        return manifest

    def _staged_component_outcome(
        self,
        run: EvaluationRun,
        inventory_candidate: ComponentPlanCandidate,
        outcome: EvaluationOutcome,
    ) -> ComponentOutcome:
        self._safe_id(inventory_candidate.component_key)
        candidate: ComponentPlanCandidate | None = None
        for revision in range(1, run.max_rounds + 1):
            candidate_path = self._run_dir(run.run_id) / "candidates" / inventory_candidate.component_key / f"revision-{revision}.json"
            if not candidate_path.is_file():
                break
            candidate = self._read_model(candidate_path, ComponentPlanCandidate)
            if revision == 1 and candidate != inventory_candidate:
                raise EvaluationConflictError("candidate artifact does not match staged inventory")
        review: QaReview | None = None
        if candidate is not None:
            review_path = self._run_dir(run.run_id) / "qa-reviews" / candidate.component_key / f"revision-{candidate.revision}.json"
            if review_path.is_file():
                review = self._read_model(review_path, QaReview)
        if outcome == EvaluationOutcome.ACCEPTED and (review is None or review.decision != "approved"):
            raise EvaluationConflictError("accepted component requires an approved persisted QA review")
        return ComponentOutcome(
            component_key=inventory_candidate.component_key,
            outcome=outcome,
            revision=candidate.revision if candidate is not None else 1,
            candidate=candidate,
            review=review,
        )

    def _artifact_digests(self, run_id: str) -> list[str]:
        run_dir = self._run_dir(run_id)
        paths = sorted(
            path
            for path in run_dir.rglob("*.json")
            if path.name != "run-manifest.json" and "lifecycle" not in path.relative_to(run_dir).parts
        )
        return [f"{path.relative_to(run_dir).as_posix()}:{_digest(path.read_bytes())}" for path in paths]

    def _optional_inventory(self, run_id: str) -> ComponentInventoryCandidate | None:
        path = self._run_dir(run_id) / "component-inventory.json"
        if not path.is_file():
            return None
        return self._read_model(path, ComponentInventoryCandidate)

    def _lifecycle_paths(self, run_id: str) -> tuple[Path, ...]:
        return tuple(sorted((self._run_dir(run_id) / "lifecycle").glob("transition-*.json")))

    def _latest_lifecycle(self, run_id: str) -> EvaluationLifecycleEvent:
        paths = self._lifecycle_paths(run_id)
        if not paths:
            raise EvaluationConflictError("evaluation lifecycle is missing")
        return self._read_model(paths[-1], EvaluationLifecycleEvent)

    def _append_lifecycle(
        self,
        run_id: str,
        status: EvaluationStatus,
        recovery_state: str,
    ) -> EvaluationLifecycleEvent:
        sequence = len(self._lifecycle_paths(run_id)) + 1
        event = EvaluationLifecycleEvent(
            run_id=run_id,
            sequence=sequence,
            status=status,
            recovery_state=recovery_state,
        )
        path = self._run_dir(run_id) / "lifecycle" / f"transition-{sequence:04d}.json"
        stored = self._write_model(path, event)
        return EvaluationLifecycleEvent.model_validate_json(stored)

    def _transition_lifecycle(
        self,
        run_id: str,
        status: EvaluationStatus,
        recovery_state: str,
    ) -> EvaluationLifecycleEvent:
        current = self._latest_lifecycle(run_id)
        if current.status in {EvaluationStatus.ACCEPTED, EvaluationStatus.PARTIAL, EvaluationStatus.FAILED}:
            if current.status != status or recovery_state != "terminal":
                raise EvaluationConflictError("terminal evaluation lifecycle cannot transition")
            return current
        if not _allowed_lifecycle_transition(current.status, status):
            raise EvaluationConflictError("evaluation lifecycle transition is invalid")
        if current.status == status and current.recovery_state == recovery_state:
            return current
        return self._append_lifecycle(run_id, status, recovery_state)

    def _consumed_slice_count(self, run_id: str) -> int:
        return len(tuple((self._run_dir(run_id) / "slices").glob("slice-*.json")))

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


def _allowed_lifecycle_transition(current: EvaluationStatus, requested: EvaluationStatus) -> bool:
    if current == requested:
        return True
    return requested in {
        EvaluationStatus.CREATED: {EvaluationStatus.INVENTORYING, EvaluationStatus.PLANNING, EvaluationStatus.FAILED},
        EvaluationStatus.INVENTORYING: {EvaluationStatus.PLANNING, EvaluationStatus.FAILED},
        EvaluationStatus.PLANNING: {
            EvaluationStatus.ACCEPTED,
            EvaluationStatus.PARTIAL,
            EvaluationStatus.FAILED,
        },
    }.get(current, set())
