"""Captain-owned durable human-review queue for business benchmarks.

The runtime-facing port can only append an ``accepted`` receipt.  A
``completed`` receipt requires a separate explicit Captain call, so an agent
handoff can never turn itself into evidence that a human review happened.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Callable, Iterator, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
from agenten.agent_runtime.contracts import IDENTIFIER_PATTERN


_MAX_COMPLETION_TIMEOUT_SECONDS = 300.0


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class CaptainHumanReviewQueueItemV1(_FrozenContract):
    """Redacted operator projection; it deliberately contains no case body."""

    schema_name: Literal["captain.business-benchmark-human-review-queue-item.v1"] = (
        Field(alias="schema", serialization_alias="schema")
    )
    review_request_id: UUID
    effect_id: str
    fence: int
    job_id: UUID
    correlation_id: UUID
    attempt: int
    request_id: UUID
    case_sha256: str
    variant: Literal["candidate"]
    model_version: str
    reason_code: str
    requested_at: datetime
    status: Literal["accepted", "completed"]


class CaptainHumanReviewEvidenceV1(_FrozenContract):
    """Minimal evidence written only by the explicit Captain operator path."""

    schema_name: Literal["captain.business-benchmark-human-review-evidence.v1"] = (
        Field(alias="schema", serialization_alias="schema")
    )
    review_request_id: UUID
    effect_id: str
    fence: int
    authority: Literal["captain_human_review"]
    operator_id: str = Field(pattern=IDENTIFIER_PATTERN)
    decision_code: str = Field(pattern=IDENTIFIER_PATTERN)
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("human review completion timestamp must be timezone-aware")
        return value


class CaptainHumanReviewCompletionAdapterResultV1(_FrozenContract):
    """Bounded redacted outcome of one explicit delegated-operator process."""

    schema_name: Literal[
        "captain.business-benchmark-human-review-completion-adapter.v1"
    ] = Field(alias="schema", serialization_alias="schema")
    status: Literal["completed", "timed_out"]
    job_ids: tuple[UUID, ...]
    operator_id: str = Field(pattern=IDENTIFIER_PATTERN)
    decision_code: str = Field(pattern=IDENTIFIER_PATTERN)
    expected_completions: int = Field(ge=1, le=100, strict=True)
    completed_count: int = Field(ge=0, le=100, strict=True)
    completed_review_request_ids: tuple[UUID, ...]

    @field_validator("job_ids")
    @classmethod
    def require_unique_jobs(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("completion adapter job IDs must be non-empty and unique")
        return value


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

    def __init__(
        self,
        root: Path,
        *,
        completion_timeout_seconds: float = 0.0,
        completion_poll_interval_seconds: float = 0.1,
    ) -> None:
        resolved = Path(root).resolve()
        if ".captain-cook" not in {part.lower() for part in resolved.parts}:
            raise ValueError(
                "human review root must be inside the gitignored .captain-cook namespace"
            )
        if (
            not math.isfinite(completion_timeout_seconds)
            or completion_timeout_seconds < 0
            or completion_timeout_seconds > _MAX_COMPLETION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "completion timeout must be finite and between 0 and 300 seconds"
            )
        if (
            not math.isfinite(completion_poll_interval_seconds)
            or completion_poll_interval_seconds <= 0
            or completion_poll_interval_seconds > _MAX_COMPLETION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "completion poll interval must be finite and between 0 and 300 seconds"
            )
        self._root = resolved
        self._completion_timeout_seconds = completion_timeout_seconds
        self._completion_poll_interval_seconds = completion_poll_interval_seconds

    async def request_review(
        self, request: CaptainHumanReviewRequestV1
    ) -> CaptainHumanReviewReceiptV1:
        """Durably accept a review request without ever completing it."""

        canonical_request = CaptainHumanReviewRequestV1.model_validate(
            request.model_dump(mode="json", by_alias=True)
        )
        binding = canonical_request.binding
        if binding.variant != "candidate":
            raise CaptainHumanReviewConflictError(
                "only a candidate may request Captain human review"
            )
        with self._effect_lock(binding.effect_id):
            receipt: CaptainHumanReviewReceiptV1 | None = None
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
                    receipt = self._terminal_receipt(canonical_request)
                else:
                    self._require_same_effect_scope(latest.binding, binding)
                    if canonical_request.requested_at < latest.requested_at:
                        raise CaptainHumanReviewConflictError(
                            "human review request time cannot move backwards"
                        )

            if receipt is None:
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
                receipt = self._terminal_receipt(canonical_request)
        return await self._wait_for_completion(canonical_request, receipt)

    async def _wait_for_completion(
        self,
        request: CaptainHumanReviewRequestV1,
        initial: CaptainHumanReviewReceiptV1,
    ) -> CaptainHumanReviewReceiptV1:
        if initial.status == "completed" or self._completion_timeout_seconds == 0:
            return initial
        deadline = monotonic() + self._completion_timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return initial
            await asyncio.sleep(min(self._completion_poll_interval_seconds, remaining))
            with self._effect_lock(request.binding.effect_id):
                terminal = self._terminal_receipt(request)
            if terminal.status == "completed":
                return terminal

    def list_reviews(
        self,
        *,
        status: Literal["pending", "completed", "all"] = "pending",
    ) -> tuple[CaptainHumanReviewQueueItemV1, ...]:
        """List only latest-fence redacted metadata for Captain operators."""

        if status not in {"pending", "completed", "all"}:
            raise ValueError("human review list status is invalid")
        items: list[CaptainHumanReviewQueueItemV1] = []
        effects_root = self._root / "effects"
        if not effects_root.exists():
            return ()
        for effect_root in sorted(path for path in effects_root.iterdir() if path.is_dir()):
            with self._effect_lock(effect_root.name):
                request = self._latest_request(effect_root.name)
                if request is None:
                    continue
                receipt = self._terminal_receipt(request)
                if status == "pending" and receipt.status != "accepted":
                    continue
                if status == "completed" and receipt.status != "completed":
                    continue
                binding = request.binding
                items.append(
                    CaptainHumanReviewQueueItemV1(
                        schema="captain.business-benchmark-human-review-queue-item.v1",
                        review_request_id=request.review_request_id,
                        effect_id=binding.effect_id,
                        fence=binding.fence,
                        job_id=binding.job_id,
                        correlation_id=binding.correlation_id,
                        attempt=binding.attempt,
                        request_id=binding.request_id,
                        case_sha256=binding.case_sha256,
                        variant=binding.variant,
                        model_version=binding.model_version,
                        reason_code=request.reason_code,
                        requested_at=request.requested_at,
                        status=receipt.status,
                    )
                )
        return tuple(items)

    def find_request(self, review_request_id: UUID) -> CaptainHumanReviewRequestV1:
        """Resolve one immutable request identity for an explicit operator action."""

        matches: list[CaptainHumanReviewRequestV1] = []
        for path in (self._root / "effects").glob("*/[0-9]*/request.json"):
            candidate = self._read_request(path)
            if candidate.review_request_id == review_request_id:
                matches.append(candidate)
        if len(matches) != 1:
            raise CaptainHumanReviewConflictError(
                "human review request identity is missing or ambiguous"
            )
        request = matches[0]
        latest = self._latest_request(request.binding.effect_id)
        if latest != request:
            raise CaptainHumanReviewStaleFenceError("human review fence is stale")
        return request

    def find_receipt(self, review_request_id: UUID) -> CaptainHumanReviewReceiptV1:
        request = self.find_request(review_request_id)
        with self._effect_lock(request.binding.effect_id):
            return self._terminal_receipt(request)

    def complete_review_as_operator(
        self,
        review_request_id: UUID,
        *,
        operator_id: str,
        decision_code: str,
        completed_at: datetime,
    ) -> CaptainHumanReviewReceiptV1:
        """Create redacted evidence and explicitly complete one accepted request."""

        request = self.find_request(review_request_id)
        binding = request.binding
        evidence = CaptainHumanReviewEvidenceV1(
            schema="captain.business-benchmark-human-review-evidence.v1",
            review_request_id=request.review_request_id,
            effect_id=binding.effect_id,
            fence=binding.fence,
            authority="captain_human_review",
            operator_id=operator_id,
            decision_code=decision_code,
            completed_at=completed_at,
        )
        evidence_bytes = _canonical(evidence)
        digest = hashlib.sha256(evidence_bytes).hexdigest()
        evidence_ref = ArtifactRef(
            uri=f"artifact://captain-human-review/evidence/{digest}",
            sha256=digest,
            media_type="application/json",
        )
        with self._effect_lock(binding.effect_id):
            completed = self._prepare_completion(
                request,
                evidence_ref=evidence_ref,
                completed_at=completed_at,
            )
            evidence_path = self._fence_root(binding) / "evidence.json"
            completed_path = self._completed_path(binding)
            if completed_path.exists() and not evidence_path.exists():
                raise CaptainHumanReviewConflictError(
                    "durable human review completion is missing operator evidence"
                )
            self._write_once(evidence_path, evidence_bytes)
            self._write_once(completed_path, _canonical(completed))
            return self._read_receipt(
                completed_path,
                request,
                expected_status="completed",
            )

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
            completed = self._prepare_completion(
                canonical_request,
                evidence_ref=canonical_evidence,
                completed_at=completed_at,
            )
            self._write_once(self._completed_path(binding), _canonical(completed))
            return self._read_receipt(
                self._completed_path(binding),
                canonical_request,
                expected_status="completed",
            )

    def _prepare_completion(
        self,
        request: CaptainHumanReviewRequestV1,
        *,
        evidence_ref: ArtifactRef,
        completed_at: datetime,
    ) -> CaptainHumanReviewReceiptV1:
        """Validate every immutable predecessor before any completion write."""

        binding = request.binding
        latest = self._latest_request(binding.effect_id)
        if latest is None:
            raise CaptainHumanReviewConflictError(
                "human review must be accepted before it can be completed"
            )
        if binding.fence < latest.binding.fence:
            raise CaptainHumanReviewStaleFenceError("human review fence is stale")
        if latest != request:
            raise CaptainHumanReviewConflictError(
                "human review completion does not match the accepted request"
            )
        self._read_receipt(
            self._accepted_path(binding), request, expected_status="accepted"
        )
        completed = CaptainHumanReviewReceiptV1(
            schema="captain.business-benchmark-human-review-receipt.v1",
            review_request_id=request.review_request_id,
            binding=binding,
            authority="captain_human_review",
            status="completed",
            evidence_ref=evidence_ref,
            recorded_at=completed_at,
        )
        validate_captain_human_review_receipt(request, completed)
        completed_path = self._completed_path(binding)
        if completed_path.exists():
            persisted = self._read_receipt(
                completed_path,
                request,
                expected_status="completed",
            )
            if persisted != completed:
                raise CaptainHumanReviewConflictError(
                    "durable human review already has different content"
                )
        return completed

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


def run_captain_human_review_completion_adapter(
    root: Path,
    *,
    job_attempts: tuple[tuple[UUID, int], ...],
    operator_id: str,
    decision_code: str,
    expected_completions: int,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.1,
    completed_at: Callable[[], datetime] | None = None,
) -> CaptainHumanReviewCompletionAdapterResultV1:
    """Complete only explicitly scoped pending reviews in a separate process.

    Calling this function is the operator action.  The provider runtime cannot
    import it through ``CaptainHumanReviewPort`` and cannot widen its job scope.
    The decision code acknowledges the synthetic benchmark escalation only; it
    is not an approval of the underlying insurance or commercial decision.
    """

    if not math.isfinite(timeout_seconds) or timeout_seconds < 0 or timeout_seconds > 5400:
        raise ValueError("completion adapter timeout must be between 0 and 5400 seconds")
    if (
        not math.isfinite(poll_interval_seconds)
        or poll_interval_seconds <= 0
        or poll_interval_seconds > 10
    ):
        raise ValueError("completion adapter poll interval must be between 0 and 10 seconds")
    if (
        not job_attempts
        or len({job_id for job_id, _attempt in job_attempts}) != len(job_attempts)
        or any(
            isinstance(attempt, bool) or not 1 <= attempt <= 5
            for _job_id, attempt in job_attempts
        )
    ):
        raise ValueError("completion adapter job attempts are invalid")
    allowed_attempts = dict(job_attempts)
    scope = CaptainHumanReviewCompletionAdapterResultV1(
        schema="captain.business-benchmark-human-review-completion-adapter.v1",
        status="timed_out",
        job_ids=tuple(allowed_attempts),
        operator_id=operator_id,
        decision_code=decision_code,
        expected_completions=expected_completions,
        completed_count=0,
        completed_review_request_ids=(),
    )
    clock = completed_at or (lambda: datetime.now(timezone.utc))
    store = CaptainHumanReviewStore(Path(root))
    deadline = monotonic() + timeout_seconds

    while True:
        completed = tuple(
            item
            for item in store.list_reviews(status="completed")
            if allowed_attempts.get(item.job_id) == item.attempt
            and item.reason_code == "mandatory_human_review"
        )
        if len(completed) > expected_completions:
            raise CaptainHumanReviewConflictError(
                "completion adapter scope contains excess completed reviews"
            )
        remaining = expected_completions - len(completed)
        pending = tuple(
            item
            for item in store.list_reviews(status="pending")
            if allowed_attempts.get(item.job_id) == item.attempt
            and item.reason_code == "mandatory_human_review"
        )
        for item in pending[:remaining]:
            store.complete_review_as_operator(
                item.review_request_id,
                operator_id=scope.operator_id,
                decision_code=scope.decision_code,
                completed_at=clock(),
            )

        completed_ids = tuple(
            sorted(
                (
                    item.review_request_id
                    for item in store.list_reviews(status="completed")
                    if allowed_attempts.get(item.job_id) == item.attempt
                    and item.reason_code == "mandatory_human_review"
                ),
                key=str,
            )
        )
        if len(completed_ids) == expected_completions:
            status: Literal["completed", "timed_out"] = "completed"
        elif monotonic() >= deadline:
            status = "timed_out"
        else:
            sleep(poll_interval_seconds)
            continue
        return scope.model_copy(
            update={
                "status": status,
                "completed_count": len(completed_ids),
                "completed_review_request_ids": completed_ids,
            }
        )


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
    "CaptainHumanReviewCompletionAdapterResultV1",
    "CaptainHumanReviewConflictError",
    "CaptainHumanReviewEvidenceV1",
    "CaptainHumanReviewError",
    "CaptainHumanReviewQueueItemV1",
    "CaptainHumanReviewStaleFenceError",
    "CaptainHumanReviewStore",
    "default_captain_human_review_root",
    "run_captain_human_review_completion_adapter",
]
