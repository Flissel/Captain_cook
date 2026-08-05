"""Durable, separately fenced replay for private business benchmark effects."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
)
from agenten.agent_factory.business_benchmark_store import _reject_unsafe_evidence
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_runtime.contracts import ArtifactRef


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUNTIME_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"


class _FrozenReplayContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BusinessBenchmarkEffectIdentityV1(_FrozenReplayContract):
    """Canonical identity of one candidate or baseline external effect."""

    schema_name: Literal["captain.business-benchmark-effect-identity.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    effect_id: str = Field(pattern=_SHA256_PATTERN)
    request_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    suite_ref: PrivateHoldoutRef
    suite_sha256: str = Field(pattern=_SHA256_PATTERN)
    suite_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    variant: Literal["candidate", "single_agent_baseline"]
    execution_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    variant_policy_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def require_exact_digests(self) -> "BusinessBenchmarkEffectIdentityV1":
        if self.suite_sha256 != self.suite_ref.sha256:
            raise ValueError("suite_sha256 must match suite_ref")
        if self.effect_id != _effect_identity_digest(self):
            raise ValueError("effect_id must match the complete benchmark effect identity")
        return self

    @classmethod
    def create(
        cls,
        *,
        request_id: UUID,
        job_id: UUID,
        correlation_id: UUID,
        subject_version: int,
        attempt: int,
        suite_ref: PrivateHoldoutRef,
        suite_id: str,
        case_id: str,
        variant: Literal["candidate", "single_agent_baseline"],
        execution_policy_sha256: str,
        variant_policy_sha256: str,
    ) -> "BusinessBenchmarkEffectIdentityV1":
        payload: dict[str, object] = {
            "schema": "captain.business-benchmark-effect-identity.v1",
            "request_id": str(request_id),
            "job_id": str(job_id),
            "correlation_id": str(correlation_id),
            "subject_version": subject_version,
            "attempt": attempt,
            "suite_ref": suite_ref.model_dump(mode="json", by_alias=True),
            "suite_sha256": suite_ref.sha256,
            "suite_id": suite_id,
            "case_id": case_id,
            "variant": variant,
            "execution_policy_sha256": execution_policy_sha256,
            "variant_policy_sha256": variant_policy_sha256,
        }
        payload["effect_id"] = _digest_value(payload)
        return cls.model_validate(payload)


class BusinessBenchmarkRuntimePreparationV1(_FrozenReplayContract):
    """Opaque stable executor identity obtained before the external effect."""

    schema_name: Literal["captain.business-benchmark-runtime-preparation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    runtime_session_id: str = Field(pattern=_RUNTIME_ID_PATTERN)


class BusinessBenchmarkPreparedEffectV1(_FrozenReplayContract):
    """Persisted effect binding created before execution is allowed."""

    schema_name: Literal["captain.business-benchmark-prepared-effect.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    identity: BusinessBenchmarkEffectIdentityV1
    runtime_session_id: str = Field(pattern=_RUNTIME_ID_PATTERN)


class BusinessBenchmarkEffectClaimV1(_FrozenReplayContract):
    """Append-only pending claim for exactly one prepared benchmark effect."""

    schema_name: Literal["captain.business-benchmark-effect-claim.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    claim_id: UUID
    claim_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    fence: int = Field(ge=1, strict=True)
    acquired_at: datetime
    expires_at: datetime
    prepared_effect: BusinessBenchmarkPreparedEffectV1

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("claim times must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_positive_lease(self) -> "BusinessBenchmarkEffectClaimV1":
        if self.expires_at <= self.acquired_at:
            raise ValueError("claim expiry must be after acquisition")
        if self.claim_fingerprint != _digest_value(
            {
                "claim_id": str(self.claim_id),
                "effect_id": self.identity.effect_id,
                "fence": self.fence,
            }
        ):
            raise ValueError("claim fingerprint must bind effect, claim, and fence")
        return self

    @property
    def identity(self) -> BusinessBenchmarkEffectIdentityV1:
        return self.prepared_effect.identity


class BusinessBenchmarkFenceReceiptV1(_FrozenReplayContract):
    """Provider-side acknowledgement that one exact fence is registered."""

    schema_name: Literal["captain.business-benchmark-fence-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    effect_id: str = Field(pattern=_SHA256_PATTERN)
    runtime_session_id: str = Field(pattern=_RUNTIME_ID_PATTERN)
    claim_id: UUID
    fence: int = Field(ge=1, strict=True)
    registered_at: datetime
    evidence_ref: ArtifactRef

    @field_validator("registered_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        return _utc(value)


class BusinessBenchmarkRecoveryObservationV1(_FrozenReplayContract):
    """Typed recovery result; deliberately contains no provider prose."""

    schema_name: Literal["captain.business-benchmark-recovery-observation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    effect_id: str = Field(pattern=_SHA256_PATTERN)
    runtime_session_id: str = Field(pattern=_RUNTIME_ID_PATTERN)
    claim_id: UUID
    fence: int = Field(ge=1, strict=True)
    fence_receipt: BusinessBenchmarkFenceReceiptV1
    checked_at: datetime
    evidence_ref: ArtifactRef
    outcome: Literal["terminal", "no_effect", "uncertain"]
    receipt: BusinessBenchmarkRunReceiptV1 | None = None

    @field_validator("checked_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def require_typed_terminal_receipt(self) -> "BusinessBenchmarkRecoveryObservationV1":
        if (
            self.fence_receipt.effect_id != self.effect_id
            or self.fence_receipt.runtime_session_id != self.runtime_session_id
            or self.fence_receipt.claim_id != self.claim_id
            or self.fence_receipt.fence != self.fence
        ):
            raise ValueError("recovery observation must bind the exact fence receipt")
        if self.outcome == "terminal" and self.receipt is None:
            raise ValueError("terminal recovery requires a receipt")
        if self.outcome != "terminal" and self.receipt is not None:
            raise ValueError("non-terminal recovery cannot carry a receipt")
        return self


class BenchmarkReplayError(ValueError):
    """Base error for durable benchmark replay state."""


class BenchmarkReplayConflictError(BenchmarkReplayError):
    """A durable effect identity was replayed with changed canonical content."""


class BenchmarkClaimBusyError(BenchmarkReplayError):
    """An unexpired claimant may currently be executing the effect."""


class BenchmarkRecoveryUncertainError(BenchmarkReplayError):
    """Recovery could prove neither terminal effect nor definite no-effect."""


@dataclass(frozen=True)
class BusinessBenchmarkReplaySnapshot:
    identity: BusinessBenchmarkEffectIdentityV1
    prepared_effect: BusinessBenchmarkPreparedEffectV1 | None = None
    latest_claim: BusinessBenchmarkEffectClaimV1 | None = None
    latest_fence_receipt: BusinessBenchmarkFenceReceiptV1 | None = None
    latest_recovery_observation: BusinessBenchmarkRecoveryObservationV1 | None = None
    receipt: BusinessBenchmarkRunReceiptV1 | None = None


@dataclass(frozen=True)
class BusinessBenchmarkClaimResult:
    claim: BusinessBenchmarkEffectClaimV1 | None = None
    receipt: BusinessBenchmarkRunReceiptV1 | None = None
    acquired: bool = False
    recovery_required: bool = False


class BusinessBenchmarkReplayStore(Protocol):
    def snapshot(
        self, identity: BusinessBenchmarkEffectIdentityV1
    ) -> BusinessBenchmarkReplaySnapshot: ...

    def claim(
        self,
        prepared_effect: BusinessBenchmarkPreparedEffectV1,
        *,
        claim_id: UUID,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> BusinessBenchmarkClaimResult: ...

    def complete(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1: ...

    def record_fence(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkFenceReceiptV1: ...

    def record_recovery(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        observation: BusinessBenchmarkRecoveryObservationV1,
    ) -> BusinessBenchmarkRecoveryObservationV1: ...


@dataclass
class _InMemoryEffectState:
    identity: BusinessBenchmarkEffectIdentityV1
    prepared_effect: BusinessBenchmarkPreparedEffectV1 | None = None
    claims: list[BusinessBenchmarkEffectClaimV1] = field(default_factory=list)
    fence_receipts: dict[int, BusinessBenchmarkFenceReceiptV1] = field(
        default_factory=dict
    )
    recovery_observations: dict[int, BusinessBenchmarkRecoveryObservationV1] = field(
        default_factory=dict
    )
    receipt_content: bytes | None = None


class InMemoryBusinessBenchmarkReplayStore:
    """Deterministic process-local implementation of the replay contract."""

    def __init__(self) -> None:
        self._states: dict[str, _InMemoryEffectState] = {}
        self._lock = threading.RLock()

    def snapshot(
        self, identity: BusinessBenchmarkEffectIdentityV1
    ) -> BusinessBenchmarkReplaySnapshot:
        with self._lock:
            state = self._states.get(identity.effect_id)
            if state is None:
                return BusinessBenchmarkReplaySnapshot(identity=identity)
            _require_identity(state.identity, identity)
            return _snapshot_from_state(state)

    def claim(
        self,
        prepared_effect: BusinessBenchmarkPreparedEffectV1,
        *,
        claim_id: UUID,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> BusinessBenchmarkClaimResult:
        with self._lock:
            _canonical_model(prepared_effect)
            identity = prepared_effect.identity
            state = self._states.get(identity.effect_id)
            if state is None:
                state = _InMemoryEffectState(identity=identity)
                self._states[identity.effect_id] = state
            _require_identity(state.identity, identity)
            return _claim_state(
                state,
                prepared_effect,
                claim_id=claim_id,
                acquired_at=acquired_at,
                expires_at=expires_at,
            )

    def complete(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        with self._lock:
            state = self._states.get(claim.identity.effect_id)
            if state is None:
                raise BenchmarkReplayConflictError("benchmark effect claim is unknown")
            return _complete_state(state, claim, fence_receipt, receipt)

    def record_fence(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        with self._lock:
            state = self._states.get(claim.identity.effect_id)
            if state is None or not state.claims or state.claims[-1] != claim:
                raise BenchmarkReplayConflictError(
                    "benchmark fence registration has no current local claim"
                )
            _validate_fence_receipt_binding(fence_receipt, claim)
            _canonical_model(fence_receipt)
            existing = state.fence_receipts.get(claim.fence)
            if existing is not None and existing != fence_receipt:
                raise BenchmarkReplayConflictError(
                    "benchmark fence already has a different provider receipt"
                )
            state.fence_receipts[claim.fence] = fence_receipt
            return fence_receipt

    def record_recovery(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        observation: BusinessBenchmarkRecoveryObservationV1,
    ) -> BusinessBenchmarkRecoveryObservationV1:
        with self._lock:
            state = self._states.get(claim.identity.effect_id)
            if state is None or not state.claims or state.claims[-1] != claim:
                raise BenchmarkReplayConflictError(
                    "benchmark recovery proof has no current local claim"
                )
            if state.fence_receipts.get(claim.fence) != fence_receipt:
                raise BenchmarkReplayConflictError(
                    "benchmark recovery proof lacks current provider fence"
                )
            _validate_recovery_binding(observation, claim, fence_receipt)
            _canonical_model(observation)
            existing = state.recovery_observations.get(claim.fence)
            if existing is not None and existing != observation:
                raise BenchmarkReplayConflictError(
                    "benchmark fence already has a different recovery proof"
                )
            state.recovery_observations[claim.fence] = observation
            return observation

    def prepared(self, effect_id: str) -> BusinessBenchmarkPreparedEffectV1 | None:
        with self._lock:
            state = self._states.get(effect_id)
            return None if state is None else state.prepared_effect

    def latest_claim(self, effect_id: str) -> BusinessBenchmarkEffectClaimV1 | None:
        with self._lock:
            state = self._states.get(effect_id)
            return None if state is None or not state.claims else state.claims[-1]

    def receipt_bytes(self, effect_id: str) -> bytes | None:
        with self._lock:
            state = self._states.get(effect_id)
            return None if state is None else state.receipt_content

    def fence_receipt(
        self, effect_id: str, fence: int
    ) -> BusinessBenchmarkFenceReceiptV1 | None:
        with self._lock:
            state = self._states.get(effect_id)
            return None if state is None else state.fence_receipts.get(fence)

    def recovery_observation(
        self, effect_id: str, fence: int
    ) -> BusinessBenchmarkRecoveryObservationV1 | None:
        with self._lock:
            state = self._states.get(effect_id)
            return None if state is None else state.recovery_observations.get(fence)


class FilesystemBusinessBenchmarkReplayStore:
    """Append-only filesystem replay state reconstructed from canonical records."""

    _thread_locks: dict[str, threading.RLock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(self, root: Path) -> None:
        self._root = root

    def snapshot(
        self, identity: BusinessBenchmarkEffectIdentityV1
    ) -> BusinessBenchmarkReplaySnapshot:
        with self._effect_lock(identity.effect_id):
            return self._snapshot_locked(identity)

    def claim(
        self,
        prepared_effect: BusinessBenchmarkPreparedEffectV1,
        *,
        claim_id: UUID,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> BusinessBenchmarkClaimResult:
        identity = prepared_effect.identity
        with self._effect_lock(identity.effect_id):
            snapshot = self._snapshot_locked(identity)
            if snapshot.receipt is not None:
                return BusinessBenchmarkClaimResult(receipt=snapshot.receipt)
            if snapshot.prepared_effect is not None:
                if snapshot.prepared_effect != prepared_effect:
                    raise BenchmarkReplayConflictError(
                        "prepared benchmark effect identity has different content"
                    )
                persisted_prepared = snapshot.prepared_effect
            else:
                self._write_once(
                    self._prepared_path(identity.effect_id),
                    _canonical_model(prepared_effect),
                )
                persisted_prepared = prepared_effect
            latest = snapshot.latest_claim
            if latest is not None and latest.expires_at > _utc(acquired_at):
                raise BenchmarkClaimBusyError("benchmark effect has an active pending claim")
            fence = 1 if latest is None else latest.fence + 1
            claim = _new_claim(
                persisted_prepared,
                claim_id=claim_id,
                fence=fence,
                acquired_at=acquired_at,
                expires_at=expires_at,
            )
            self._write_once(self._claim_path(identity.effect_id, fence), _canonical_model(claim))
            return BusinessBenchmarkClaimResult(
                claim=claim,
                acquired=True,
                recovery_required=latest is not None,
            )

    def complete(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkRunReceiptV1:
        identity = claim.identity
        content = _canonical_receipt(receipt)
        with self._effect_lock(identity.effect_id):
            existing_path = self._receipt_path(identity.effect_id)
            if existing_path.exists():
                existing = existing_path.read_bytes()
                if existing != content:
                    raise BenchmarkReplayConflictError(
                        "benchmark effect already has a different receipt"
                    )
                return BusinessBenchmarkRunReceiptV1.model_validate_json(existing)
            snapshot = self._snapshot_locked(identity)
            if snapshot.latest_claim != claim:
                raise BenchmarkReplayConflictError(
                    "benchmark effect claim was superseded by a higher fence"
                )
            if snapshot.latest_fence_receipt != fence_receipt:
                raise BenchmarkReplayConflictError(
                    "benchmark completion lacks the current provider fence receipt"
                )
            _validate_receipt_binding(receipt, claim.prepared_effect)
            self._write_once(existing_path, content)
            return BusinessBenchmarkRunReceiptV1.model_validate_json(content)

    def record_fence(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
    ) -> BusinessBenchmarkFenceReceiptV1:
        identity = claim.identity
        with self._effect_lock(identity.effect_id):
            snapshot = self._snapshot_locked(identity)
            if snapshot.latest_claim != claim:
                raise BenchmarkReplayConflictError(
                    "benchmark fence registration has no current local claim"
                )
            _validate_fence_receipt_binding(fence_receipt, claim)
            self._write_once(
                self._fence_path(identity.effect_id, claim.fence),
                _canonical_model(fence_receipt),
            )
            return self.fence_receipt(identity.effect_id, claim.fence) or fence_receipt

    def record_recovery(
        self,
        claim: BusinessBenchmarkEffectClaimV1,
        fence_receipt: BusinessBenchmarkFenceReceiptV1,
        observation: BusinessBenchmarkRecoveryObservationV1,
    ) -> BusinessBenchmarkRecoveryObservationV1:
        identity = claim.identity
        with self._effect_lock(identity.effect_id):
            snapshot = self._snapshot_locked(identity)
            if snapshot.latest_claim != claim:
                raise BenchmarkReplayConflictError(
                    "benchmark recovery proof has no current local claim"
                )
            if snapshot.latest_fence_receipt != fence_receipt:
                raise BenchmarkReplayConflictError(
                    "benchmark recovery proof lacks current provider fence"
                )
            _validate_recovery_binding(observation, claim, fence_receipt)
            self._write_once(
                self._recovery_path(identity.effect_id, claim.fence),
                _canonical_model(observation),
            )
            return (
                self.recovery_observation(identity.effect_id, claim.fence)
                or observation
            )

    def prepared(self, effect_id: str) -> BusinessBenchmarkPreparedEffectV1 | None:
        path = self._prepared_path(effect_id)
        if not path.exists():
            return None
        return _read_model(path, BusinessBenchmarkPreparedEffectV1)

    def latest_claim(self, effect_id: str) -> BusinessBenchmarkEffectClaimV1 | None:
        paths = sorted(self._claims_dir(effect_id).glob("*.json"))
        if not paths:
            return None
        return _read_model(paths[-1], BusinessBenchmarkEffectClaimV1)

    def receipt_bytes(self, effect_id: str) -> bytes | None:
        path = self._receipt_path(effect_id)
        return path.read_bytes() if path.exists() else None

    def fence_receipt(
        self, effect_id: str, fence: int
    ) -> BusinessBenchmarkFenceReceiptV1 | None:
        path = self._fence_path(effect_id, fence)
        if not path.exists():
            return None
        return _read_model(path, BusinessBenchmarkFenceReceiptV1)

    def recovery_observation(
        self, effect_id: str, fence: int
    ) -> BusinessBenchmarkRecoveryObservationV1 | None:
        path = self._recovery_path(effect_id, fence)
        if not path.exists():
            return None
        return _read_model(path, BusinessBenchmarkRecoveryObservationV1)

    def _snapshot_locked(
        self, identity: BusinessBenchmarkEffectIdentityV1
    ) -> BusinessBenchmarkReplaySnapshot:
        prepared = self.prepared(identity.effect_id)
        if prepared is not None:
            _require_identity(prepared.identity, identity)
        latest = self.latest_claim(identity.effect_id)
        latest_fence_receipt = None
        latest_recovery_observation = None
        if latest is not None:
            _require_identity(latest.identity, identity)
            if prepared != latest.prepared_effect:
                raise BenchmarkReplayConflictError("claim conflicts with prepared effect")
            latest_fence_receipt = self.fence_receipt(
                identity.effect_id, latest.fence
            )
            if latest_fence_receipt is not None:
                _validate_fence_receipt_binding(latest_fence_receipt, latest)
                latest_recovery_observation = self.recovery_observation(
                    identity.effect_id, latest.fence
                )
                if latest_recovery_observation is not None:
                    _validate_recovery_binding(
                        latest_recovery_observation,
                        latest,
                        latest_fence_receipt,
                    )
        content = self.receipt_bytes(identity.effect_id)
        receipt = None
        if content is not None:
            try:
                receipt = BusinessBenchmarkRunReceiptV1.model_validate_json(content)
            except ValueError as exc:
                raise BenchmarkReplayConflictError(
                    "durable benchmark receipt is invalid"
                ) from exc
            if _canonical_receipt(receipt) != content:
                raise BenchmarkReplayConflictError(
                    "durable benchmark receipt is not canonical"
                )
            if prepared is None:
                raise BenchmarkReplayConflictError("receipt has no prepared benchmark effect")
            _validate_receipt_binding(receipt, prepared)
        return BusinessBenchmarkReplaySnapshot(
            identity=identity,
            prepared_effect=prepared,
            latest_claim=latest,
            latest_fence_receipt=latest_fence_receipt,
            latest_recovery_observation=latest_recovery_observation,
            receipt=receipt,
        )

    def _prepared_path(self, effect_id: str) -> Path:
        return self._root / "prepared" / f"{effect_id}.json"

    def _claims_dir(self, effect_id: str) -> Path:
        return self._root / "claims" / effect_id

    def _claim_path(self, effect_id: str, fence: int) -> Path:
        return self._claims_dir(effect_id) / f"{fence:020d}.json"

    def _receipt_path(self, effect_id: str) -> Path:
        return self._root / "receipts" / f"{effect_id}.json"

    def _fence_path(self, effect_id: str, fence: int) -> Path:
        return self._root / "fences" / effect_id / f"{fence:020d}.json"

    def _recovery_path(self, effect_id: str, fence: int) -> Path:
        return self._root / "recovery" / effect_id / f"{fence:020d}.json"

    @contextmanager
    def _effect_lock(self, effect_id: str) -> Iterator[None]:
        lock_path = self._root / "locks" / f"{effect_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(lock_path.resolve())
        with self._thread_locks_guard:
            thread_lock = self._thread_locks.setdefault(key, threading.RLock())
        with thread_lock:
            with lock_path.open("a+b") as handle:
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                _lock_file(handle.fileno())
                try:
                    yield
                finally:
                    _unlock_file(handle.fileno())

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
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
                    raise BenchmarkReplayConflictError(
                        "durable benchmark identity already has different content"
                    )
        finally:
            temporary.unlink(missing_ok=True)


def _claim_state(
    state: _InMemoryEffectState,
    prepared_effect: BusinessBenchmarkPreparedEffectV1,
    *,
    claim_id: UUID,
    acquired_at: datetime,
    expires_at: datetime,
) -> BusinessBenchmarkClaimResult:
    if state.receipt_content is not None:
        return BusinessBenchmarkClaimResult(
            receipt=BusinessBenchmarkRunReceiptV1.model_validate_json(state.receipt_content)
        )
    if state.prepared_effect is None:
        state.prepared_effect = prepared_effect
    elif state.prepared_effect != prepared_effect:
        raise BenchmarkReplayConflictError(
            "prepared benchmark effect identity has different content"
        )
    latest = state.claims[-1] if state.claims else None
    acquired = _utc(acquired_at)
    if latest is not None and latest.expires_at > acquired:
        raise BenchmarkClaimBusyError("benchmark effect has an active pending claim")
    claim = _new_claim(
        state.prepared_effect,
        claim_id=claim_id,
        fence=1 if latest is None else latest.fence + 1,
        acquired_at=acquired,
        expires_at=expires_at,
    )
    _canonical_model(claim)
    state.claims.append(claim)
    return BusinessBenchmarkClaimResult(
        claim=claim,
        acquired=True,
        recovery_required=latest is not None,
    )


def _complete_state(
    state: _InMemoryEffectState,
    claim: BusinessBenchmarkEffectClaimV1,
    fence_receipt: BusinessBenchmarkFenceReceiptV1,
    receipt: BusinessBenchmarkRunReceiptV1,
) -> BusinessBenchmarkRunReceiptV1:
    content = _canonical_receipt(receipt)
    if state.receipt_content is not None:
        if state.receipt_content != content:
            raise BenchmarkReplayConflictError(
                "benchmark effect already has a different receipt"
            )
        return BusinessBenchmarkRunReceiptV1.model_validate_json(state.receipt_content)
    if not state.claims or state.claims[-1] != claim:
        raise BenchmarkReplayConflictError(
            "benchmark effect claim was superseded by a higher fence"
        )
    if state.fence_receipts.get(claim.fence) != fence_receipt:
        raise BenchmarkReplayConflictError(
            "benchmark completion lacks the current provider fence receipt"
        )
    _validate_receipt_binding(receipt, claim.prepared_effect)
    state.receipt_content = content
    return BusinessBenchmarkRunReceiptV1.model_validate_json(content)


def _snapshot_from_state(state: _InMemoryEffectState) -> BusinessBenchmarkReplaySnapshot:
    receipt = (
        None
        if state.receipt_content is None
        else BusinessBenchmarkRunReceiptV1.model_validate_json(state.receipt_content)
    )
    return BusinessBenchmarkReplaySnapshot(
        identity=state.identity,
        prepared_effect=state.prepared_effect,
        latest_claim=state.claims[-1] if state.claims else None,
        latest_fence_receipt=(
            None
            if not state.claims
            else state.fence_receipts.get(state.claims[-1].fence)
        ),
        latest_recovery_observation=(
            None
            if not state.claims
            else state.recovery_observations.get(state.claims[-1].fence)
        ),
        receipt=receipt,
    )


def _new_claim(
    prepared_effect: BusinessBenchmarkPreparedEffectV1,
    *,
    claim_id: UUID,
    fence: int,
    acquired_at: datetime,
    expires_at: datetime,
) -> BusinessBenchmarkEffectClaimV1:
    claim_fingerprint = _digest_value(
        {
            "claim_id": str(claim_id),
            "effect_id": prepared_effect.identity.effect_id,
            "fence": fence,
        }
    )
    return BusinessBenchmarkEffectClaimV1(
        schema="captain.business-benchmark-effect-claim.v1",
        claim_id=claim_id,
        claim_fingerprint=claim_fingerprint,
        fence=fence,
        acquired_at=_utc(acquired_at),
        expires_at=_utc(expires_at),
        prepared_effect=prepared_effect,
    )


def _validate_receipt_binding(
    receipt: BusinessBenchmarkRunReceiptV1,
    prepared_effect: BusinessBenchmarkPreparedEffectV1,
) -> None:
    identity = prepared_effect.identity
    expected: dict[str, object] = {
        "request_id": identity.request_id,
        "job_id": identity.job_id,
        "correlation_id": identity.correlation_id,
        "subject_version": identity.subject_version,
        "attempt": identity.attempt,
        "suite_ref": identity.suite_ref,
        "suite_id": identity.suite_id,
        "case_id": identity.case_id,
        "variant": identity.variant,
        "execution_policy_sha256": identity.execution_policy_sha256,
        "runtime_session_id": prepared_effect.runtime_session_id,
    }
    for field_name, value in expected.items():
        if getattr(receipt, field_name) != value:
            raise BenchmarkReplayConflictError(
                f"benchmark receipt {field_name} conflicts with prepared effect"
            )


def _validate_fence_receipt_binding(
    fence_receipt: BusinessBenchmarkFenceReceiptV1,
    claim: BusinessBenchmarkEffectClaimV1,
) -> None:
    expected: dict[str, object] = {
        "effect_id": claim.identity.effect_id,
        "runtime_session_id": claim.prepared_effect.runtime_session_id,
        "claim_id": claim.claim_id,
        "fence": claim.fence,
    }
    for field_name, value in expected.items():
        if getattr(fence_receipt, field_name) != value:
            raise BenchmarkReplayConflictError(
                f"provider fence receipt {field_name} conflicts with claim"
            )


def _validate_recovery_binding(
    observation: BusinessBenchmarkRecoveryObservationV1,
    claim: BusinessBenchmarkEffectClaimV1,
    fence_receipt: BusinessBenchmarkFenceReceiptV1,
) -> None:
    expected: dict[str, object] = {
        "effect_id": claim.identity.effect_id,
        "runtime_session_id": claim.prepared_effect.runtime_session_id,
        "claim_id": claim.claim_id,
        "fence": claim.fence,
        "fence_receipt": fence_receipt,
    }
    for field_name, value in expected.items():
        if getattr(observation, field_name) != value:
            raise BenchmarkReplayConflictError(
                f"benchmark recovery {field_name} conflicts with claim"
            )


def _require_identity(
    stored: BusinessBenchmarkEffectIdentityV1,
    incoming: BusinessBenchmarkEffectIdentityV1,
) -> None:
    if stored != incoming:
        raise BenchmarkReplayConflictError(
            "benchmark effect digest resolves to different identity content"
        )


def _effect_identity_digest(identity: BusinessBenchmarkEffectIdentityV1) -> str:
    payload = identity.model_dump(mode="json", by_alias=True)
    payload.pop("effect_id")
    return _digest_value(payload)


def _canonical_receipt(receipt: BusinessBenchmarkRunReceiptV1) -> bytes:
    canonical = BusinessBenchmarkRunReceiptV1.model_validate(
        receipt.model_dump(mode="json", by_alias=True)
    )
    return _canonical_model(canonical)


def _canonical_model(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "benchmark replay record")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_value(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _read_model(path: Path, model: type[_FrozenReplayContract]):
    try:
        content = path.read_bytes()
        parsed = model.model_validate_json(content)
        if _canonical_model(parsed) != content:
            raise ValueError("record is not canonical")
        return parsed
    except (OSError, ValueError) as exc:
        raise BenchmarkReplayConflictError(
            "durable benchmark replay record is invalid"
        ) from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("benchmark replay clock must be timezone-aware")
    return value.astimezone(timezone.utc)


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
