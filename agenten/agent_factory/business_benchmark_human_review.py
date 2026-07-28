"""Captain-owned durable human-review queue for business benchmarks.

The runtime-facing port can only append an ``accepted`` receipt.  A
``completed`` receipt requires a separate explicit Captain call, so an agent
handoff can never turn itself into evidence that a human review happened.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel

from agenten.agent_factory.business_benchmark_contracts import (
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_handoff import (
    CaptainHumanReviewReceiptV1,
    CaptainHumanReviewRequestV1,
    validate_captain_human_review_receipt,
)
from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderBindingV1,
)
from agenten.agent_factory.business_benchmark_store import _reject_unsafe_evidence
from agenten.agent_runtime.contracts import ArtifactRef


class CaptainHumanReviewError(ValueError):
    """Base error for Captain's durable human-review boundary."""


class CaptainHumanReviewConflictError(CaptainHumanReviewError):
    """Persisted immutable review evidence conflicts with the requested action."""


class CaptainHumanReviewStaleFenceError(CaptainHumanReviewError):
    """A review action attempted to use a fence below Captain's durable maximum."""


def default_captain_human_review_root(workspace_root: Path) -> Path:
    """Return the Captain-private, gitignored review queue location."""

    return (
        Path(workspace_root)
        / ".captain-cook"
        / "private"
        / "business-benchmark-human-review"
    )


class CaptainHumanReviewStore:
    """Process-safe append-only implementation of ``CaptainHumanReviewPort``."""

    _thread_locks: dict[str, threading.RLock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(self, root: Path) -> None:
        resolved = Path(root).resolve()
        if ".captain-cook" not in {part.lower() for part in resolved.parts}:
            raise ValueError(
                "human review root must be inside the gitignored .captain-cook namespace"
            )
        self._root = resolved

    async def request_review(
        self, request: CaptainHumanReviewRequestV1
    ) -> CaptainHumanReviewReceiptV1:
        """Durably accept a review request without ever completing it."""

        canonical_request = CaptainHumanReviewRequestV1.model_validate(
            request.model_dump(mode="json", by_alias=True)
        )
        binding = canonical_request.binding
        with self._effect_lock(binding.effect_id):
            latest = self._latest_request(binding.effect_id)
            if latest is not None:
                if binding.fence < latest.binding.fence:
                    raise CaptainHumanReviewStaleFenceError(
                        "human review fence is stale"
                    )
                if binding.fence == latest.binding.fence:
                    if latest != canonical_request:
                        raise CaptainHumanReviewConflictError(
                            "human review request already has different content"
                        )
                    return self._terminal_receipt(canonical_request)
                self._require_same_effect_scope(latest.binding, binding)
                if canonical_request.requested_at < latest.requested_at:
                    raise CaptainHumanReviewConflictError(
                        "human review request time cannot move backwards"
                    )

            request_bytes = _canonical(canonical_request)
            request_path = self._request_path(binding)
            self._write_once(request_path, request_bytes)
            evidence_ref = _request_artifact_ref(request_bytes)
            accepted = CaptainHumanReviewReceiptV1(
                schema="captain.business-benchmark-human-review-receipt.v1",
                review_request_id=canonical_request.review_request_id,
                binding=binding,
                authority="captain_human_review",
                status="accepted",
                evidence_ref=evidence_ref,
                recorded_at=canonical_request.requested_at,
            )
            validate_captain_human_review_receipt(canonical_request, accepted)
            self._write_once(self._accepted_path(binding), _canonical(accepted))
            return self._terminal_receipt(canonical_request)

    def complete_review(
        self,
        request: CaptainHumanReviewRequestV1,
        *,
        evidence_ref: ArtifactRef,
        completed_at: datetime,
    ) -> CaptainHumanReviewReceiptV1:
        """Explicitly record Captain's completed review decision.

        This operation deliberately is not part of ``CaptainHumanReviewPort``.
        The provider runtime therefore cannot invoke it while handling an agent
        handoff.
        """

        canonical_request = CaptainHumanReviewRequestV1.model_validate(
            request.model_dump(mode="json", by_alias=True)
        )
        canonical_evidence = ArtifactRef.model_validate(
            evidence_ref.model_dump(mode="json")
        )
        binding = canonical_request.binding
        with self._effect_lock(binding.effect_id):
            latest = self._latest_request(binding.effect_id)
            if latest is None:
                raise CaptainHumanReviewConflictError(
                    "human review must be accepted before it can be completed"
                )
            if binding.fence < latest.binding.fence:
                raise CaptainHumanReviewStaleFenceError("human review fence is stale")
            if latest != canonical_request:
                raise CaptainHumanReviewConflictError(
                    "human review completion does not match the accepted request"
                )
            self._read_receipt(
                self._accepted_path(binding), canonical_request, expected_status="accepted"
            )
            completed = CaptainHumanReviewReceiptV1(
                schema="captain.business-benchmark-human-review-receipt.v1",
                review_request_id=canonical_request.review_request_id,
                binding=binding,
                authority="captain_human_review",
                status="completed",
                evidence_ref=canonical_evidence,
                recorded_at=completed_at,
            )
            validate_captain_human_review_receipt(canonical_request, completed)
            self._write_once(self._completed_path(binding), _canonical(completed))
            return self._read_receipt(
                self._completed_path(binding),
                canonical_request,
                expected_status="completed",
            )

    def _terminal_receipt(
        self, request: CaptainHumanReviewRequestV1
    ) -> CaptainHumanReviewReceiptV1:
        accepted = self._read_receipt(
            self._accepted_path(request.binding), request, expected_status="accepted"
        )
        completed_path = self._completed_path(request.binding)
        if completed_path.exists():
            return self._read_receipt(
                completed_path, request, expected_status="completed"
            )
        return accepted

    def _latest_request(self, effect_id: str) -> CaptainHumanReviewRequestV1 | None:
        fence_roots = sorted(
            (path for path in self._effect_root(effect_id).glob("[0-9]*") if path.is_dir()),
            key=lambda path: int(path.name),
        )
        if not fence_roots:
            return None
        request_path = fence_roots[-1] / "request.json"
        if not request_path.exists():
            raise CaptainHumanReviewConflictError(
                "human review fence is missing its request"
            )
        return self._read_request(request_path)

    @staticmethod
    def _require_same_effect_scope(
        existing: BusinessBenchmarkProviderBindingV1,
        requested: BusinessBenchmarkProviderBindingV1,
    ) -> None:
        fields = (
            "effect_id",
            "runtime_session_id",
            "job_id",
            "correlation_id",
            "attempt",
            "request_id",
            "case_sha256",
            "variant",
            "model_version",
        )
        if any(getattr(existing, field) != getattr(requested, field) for field in fields):
            raise CaptainHumanReviewConflictError(
                "human review effect has different content across fences"
            )

    def _read_request(self, path: Path) -> CaptainHumanReviewRequestV1:
        try:
            content = path.read_bytes()
            request = CaptainHumanReviewRequestV1.model_validate_json(content)
            if _canonical(request) != content:
                raise ValueError("human review request is not canonical")
            return request
        except (OSError, ValueError) as exc:
            raise CaptainHumanReviewConflictError(
                "durable human review request is invalid"
            ) from exc

    def _read_receipt(
        self,
        path: Path,
        request: CaptainHumanReviewRequestV1,
        *,
        expected_status: str,
    ) -> CaptainHumanReviewReceiptV1:
        try:
            content = path.read_bytes()
            receipt = CaptainHumanReviewReceiptV1.model_validate_json(content)
            if _canonical(receipt) != content:
                raise ValueError("human review receipt is not canonical")
            validate_captain_human_review_receipt(request, receipt)
            if receipt.status != expected_status:
                raise ValueError("human review receipt has an invalid stage")
            if expected_status == "accepted":
                request_bytes = _canonical(request)
                if receipt.evidence_ref != _request_artifact_ref(request_bytes):
                    raise ValueError("accepted receipt does not reference its request")
                if receipt.recorded_at != request.requested_at:
                    raise ValueError("accepted receipt timestamp is not deterministic")
            return receipt
        except (OSError, ValueError) as exc:
            raise CaptainHumanReviewConflictError(
                "durable human review receipt is invalid"
            ) from exc

    def _effect_root(self, effect_id: str) -> Path:
        return self._root / "effects" / effect_id

    def _fence_root(self, binding: BusinessBenchmarkProviderBindingV1) -> Path:
        return self._effect_root(binding.effect_id) / f"{binding.fence:020d}"

    def _request_path(self, binding: BusinessBenchmarkProviderBindingV1) -> Path:
        return self._fence_root(binding) / "request.json"

    def _accepted_path(self, binding: BusinessBenchmarkProviderBindingV1) -> Path:
        return self._fence_root(binding) / "accepted.json"

    def _completed_path(self, binding: BusinessBenchmarkProviderBindingV1) -> Path:
        return self._fence_root(binding) / "completed.json"

    @contextmanager
    def _effect_lock(self, effect_id: str) -> Iterator[None]:
        lock_path = self._root / "locks" / f"{effect_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(lock_path.resolve())
        with self._thread_locks_guard:
            thread_lock = self._thread_locks.setdefault(key, threading.RLock())
        with thread_lock:
            with lock_path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                _lock_file(handle.fileno())
                try:
                    yield
                finally:
                    handle.seek(0)
                    _unlock_file(handle.fileno())

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise CaptainHumanReviewConflictError(
                        "durable human review already has different content"
                    )
        finally:
            temporary.unlink(missing_ok=True)


def _canonical(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "human review")
    return canonical_business_benchmark_model_bytes(model)


def _request_artifact_ref(request_bytes: bytes) -> ArtifactRef:
    digest = hashlib.sha256(request_bytes).hexdigest()
    return ArtifactRef(
        uri=f"artifact://captain-human-review/request/{digest}",
        sha256=digest,
        media_type="application/json",
    )


if os.name == "nt":
    import msvcrt

    def _lock_file(descriptor: int) -> None:
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

    def _unlock_file(descriptor: int) -> None:
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def _unlock_file(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


__all__ = [
    "CaptainHumanReviewConflictError",
    "CaptainHumanReviewError",
    "CaptainHumanReviewStaleFenceError",
    "CaptainHumanReviewStore",
    "default_captain_human_review_root",
]
