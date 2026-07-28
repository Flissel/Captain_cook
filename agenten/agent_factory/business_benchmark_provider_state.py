"""Captain-owned durable provider state for business benchmark effects.

The store records only typed, redacted benchmark bindings and terminal receipt
contracts. Provider prompts, case bodies, and transcripts are deliberately not
part of this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkRunReceiptV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_store import _reject_unsafe_evidence
from agenten.agent_runtime.contracts import IDENTIFIER_PATTERN


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUNTIME_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
_STAGE_FILENAMES = (
    "00-fenced.json",
    "10-dispatching.json",
    "20-provider-terminal.json",
    "30-receipt-finalized.json",
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BusinessBenchmarkProviderBindingV1(_FrozenContract):
    """Exact provider-side identity for one fenced benchmark execution."""

    schema_name: Literal["captain.business-benchmark-provider-binding.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    effect_id: str = Field(pattern=_SHA256_PATTERN)
    runtime_session_id: str = Field(pattern=_RUNTIME_ID_PATTERN)
    claim_id: UUID
    fence: int = Field(ge=1, strict=True)
    job_id: UUID
    correlation_id: UUID
    attempt: int = Field(ge=1, le=5, strict=True)
    request_id: UUID
    case_sha256: str = Field(pattern=_SHA256_PATTERN)
    variant: Literal["candidate", "single_agent_baseline"]
    model_version: str = Field(pattern=IDENTIFIER_PATTERN)


ProviderStateStage = Literal[
    "fenced", "dispatching", "provider_terminal", "receipt_finalized"
]


class BusinessBenchmarkProviderStateV1(_FrozenContract):
    """One immutable transition in the provider-side state chain."""

    schema_name: Literal["captain.business-benchmark-provider-state.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    binding: BusinessBenchmarkProviderBindingV1
    stage: ProviderStateStage
    recorded_at: datetime
    previous_state_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    provider_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    state_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("recorded_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider state timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_chain_shape_and_digest(self) -> "BusinessBenchmarkProviderStateV1":
        if self.stage == "fenced":
            if self.previous_state_sha256 is not None:
                raise ValueError("fenced provider state cannot have a predecessor")
            if self.provider_receipt_sha256 is not None:
                raise ValueError("fenced provider state cannot carry a receipt digest")
        elif self.previous_state_sha256 is None:
            raise ValueError("provider state transition requires its predecessor digest")
        if self.stage in {"provider_terminal", "receipt_finalized"}:
            if self.provider_receipt_sha256 is None:
                raise ValueError("terminal provider state requires a receipt digest")
        elif self.provider_receipt_sha256 is not None:
            raise ValueError("pre-terminal provider state cannot carry a receipt digest")
        if self.state_sha256 != _state_digest(self):
            raise ValueError("provider state digest does not match canonical content")
        return self

    @classmethod
    def create(
        cls,
        *,
        binding: BusinessBenchmarkProviderBindingV1,
        stage: ProviderStateStage,
        recorded_at: datetime,
        previous_state_sha256: str | None = None,
        provider_receipt_sha256: str | None = None,
    ) -> "BusinessBenchmarkProviderStateV1":
        payload: dict[str, object] = {
            "schema": "captain.business-benchmark-provider-state.v1",
            "binding": binding.model_dump(mode="json", by_alias=True),
            "stage": stage,
            "recorded_at": _utc(recorded_at).isoformat().replace("+00:00", "Z"),
            "previous_state_sha256": previous_state_sha256,
            "provider_receipt_sha256": provider_receipt_sha256,
        }
        payload["state_sha256"] = _digest_value(payload)
        return cls.model_validate(payload)


class BusinessBenchmarkProviderRecoveryV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-provider-recovery.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    binding: BusinessBenchmarkProviderBindingV1
    state_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: Literal["terminal", "no_effect", "uncertain"]
    receipt: BusinessBenchmarkRunReceiptV1 | None = None

    @model_validator(mode="after")
    def require_terminal_receipt(self) -> "BusinessBenchmarkProviderRecoveryV1":
        if self.outcome == "terminal" and self.receipt is None:
            raise ValueError("terminal provider recovery requires a receipt")
        if self.outcome != "terminal" and self.receipt is not None:
            raise ValueError("non-terminal provider recovery cannot carry a receipt")
        return self


class BusinessBenchmarkProviderStateError(ValueError):
    """Base error for durable provider state."""


class BusinessBenchmarkProviderStateConflictError(BusinessBenchmarkProviderStateError):
    """Persisted immutable provider bytes conflict with the requested operation."""


class BusinessBenchmarkStaleProviderFenceError(BusinessBenchmarkProviderStateError):
    """A caller attempted to act under a fence lower than the durable maximum."""


class BusinessBenchmarkProviderStateUncertainError(BusinessBenchmarkProviderStateError):
    """An effect may already have reached the provider and cannot be repeated safely."""


def default_business_benchmark_provider_state_root(workspace_root: Path) -> Path:
    """Return the Captain-private, gitignored provider-state location."""

    return (
        Path(workspace_root)
        / ".captain-cook"
        / "private"
        / "business-benchmark-provider-state"
    )


class BusinessBenchmarkProviderStateStore:
    """Process-safe append-only filesystem adapter for provider effect recovery."""

    _thread_locks: dict[str, threading.RLock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(self, root: Path) -> None:
        resolved = Path(root).resolve()
        if ".captain-cook" not in {part.lower() for part in resolved.parts}:
            raise ValueError(
                "provider state root must be inside the gitignored .captain-cook namespace"
            )
        self._root = resolved

    def register_fence(
        self,
        binding: BusinessBenchmarkProviderBindingV1,
        *,
        registered_at: datetime,
    ) -> BusinessBenchmarkProviderStateV1:
        with self._effect_lock(binding.effect_id):
            latest = self._latest_state(binding.effect_id)
            if latest is not None:
                if binding.fence < latest.binding.fence:
                    raise BusinessBenchmarkStaleProviderFenceError(
                        "provider fence is stale"
                    )
                if binding.fence == latest.binding.fence:
                    self._require_binding(latest.binding, binding)
                    proposed = BusinessBenchmarkProviderStateV1.create(
                        binding=binding,
                        stage="fenced",
                        recorded_at=registered_at,
                    )
                    fenced = self._read_stage(binding, "fenced")
                    if fenced != proposed:
                        raise BusinessBenchmarkProviderStateConflictError(
                            "provider fence already has different content"
                        )
                    return latest
                self._require_same_effect_scope(latest.binding, binding)
                if latest.stage == "receipt_finalized":
                    raise BusinessBenchmarkProviderStateConflictError(
                        "finalized provider effect cannot be refenced"
                    )
            state = BusinessBenchmarkProviderStateV1.create(
                binding=binding,
                stage="fenced",
                recorded_at=registered_at,
            )
            self._write_once(self._stage_path(binding, "fenced"), _canonical_model(state))
            return state

    def assert_current(
        self, binding: BusinessBenchmarkProviderBindingV1
    ) -> BusinessBenchmarkProviderStateV1:
        with self._effect_lock(binding.effect_id):
            return self._assert_current_locked(binding)

    def begin_dispatch(
        self,
        binding: BusinessBenchmarkProviderBindingV1,
        *,
        started_at: datetime,
    ) -> BusinessBenchmarkProviderStateV1:
        with self._effect_lock(binding.effect_id):
            latest = self._assert_current_locked(binding)
            if self._has_unresolved_prior_dispatch(binding):
                raise BusinessBenchmarkProviderStateUncertainError(
                    "provider state is uncertain after a prior fenced dispatch"
                )
            if latest.stage != "fenced":
                if latest.stage in {"dispatching", "provider_terminal"}:
                    raise BusinessBenchmarkProviderStateUncertainError(
                        "provider state is uncertain; automatic reexecution is forbidden"
                    )
                raise BusinessBenchmarkProviderStateConflictError(
                    "finalized provider effect cannot begin dispatch"
                )
            state = BusinessBenchmarkProviderStateV1.create(
                binding=binding,
                stage="dispatching",
                recorded_at=started_at,
                previous_state_sha256=latest.state_sha256,
            )
            self._write_once(
                self._stage_path(binding, "dispatching"), _canonical_model(state)
            )
            return state

    def record_provider_terminal(
        self,
        binding: BusinessBenchmarkProviderBindingV1,
        receipt: BusinessBenchmarkRunReceiptV1,
        *,
        recorded_at: datetime,
    ) -> BusinessBenchmarkProviderStateV1:
        receipt_bytes = _canonical_receipt(receipt)
        self._validate_receipt_binding(binding, receipt)
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        with self._effect_lock(binding.effect_id):
            latest = self._assert_current_locked(binding)
            if latest.stage == "provider_terminal":
                if latest.provider_receipt_sha256 != receipt_sha256:
                    raise BusinessBenchmarkProviderStateConflictError(
                        "provider terminal state has a different receipt"
                    )
                return latest
            if latest.stage == "receipt_finalized":
                if latest.provider_receipt_sha256 != receipt_sha256:
                    raise BusinessBenchmarkProviderStateConflictError(
                        "finalized provider state has a different receipt"
                    )
                return latest
            if latest.stage != "dispatching":
                raise BusinessBenchmarkProviderStateConflictError(
                    "provider terminal state requires dispatching"
                )
            state = BusinessBenchmarkProviderStateV1.create(
                binding=binding,
                stage="provider_terminal",
                recorded_at=recorded_at,
                previous_state_sha256=latest.state_sha256,
                provider_receipt_sha256=receipt_sha256,
            )
            self._write_once(
                self._stage_path(binding, "provider_terminal"),
                _canonical_model(state),
            )
            return state

    def finalize(
        self,
        binding: BusinessBenchmarkProviderBindingV1,
        receipt: BusinessBenchmarkRunReceiptV1,
        *,
        finalized_at: datetime,
    ) -> BusinessBenchmarkRunReceiptV1:
        receipt_bytes = _canonical_receipt(receipt)
        self._validate_receipt_binding(binding, receipt)
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        with self._effect_lock(binding.effect_id):
            latest = self._assert_current_locked(binding)
            receipt_path = self._receipt_path(binding)
            if latest.stage == "receipt_finalized":
                persisted = self._read_receipt(receipt_path, binding)
                if _canonical_receipt(persisted) != receipt_bytes:
                    raise BusinessBenchmarkProviderStateConflictError(
                        "finalized provider state has a different receipt"
                    )
                return persisted
            if latest.stage != "provider_terminal":
                raise BusinessBenchmarkProviderStateConflictError(
                    "provider receipt finalization requires provider_terminal"
                )
            if latest.provider_receipt_sha256 != receipt_sha256:
                raise BusinessBenchmarkProviderStateConflictError(
                    "provider terminal state has a different receipt"
                )
            self._write_once(receipt_path, receipt_bytes)
            state = BusinessBenchmarkProviderStateV1.create(
                binding=binding,
                stage="receipt_finalized",
                recorded_at=finalized_at,
                previous_state_sha256=latest.state_sha256,
                provider_receipt_sha256=receipt_sha256,
            )
            self._write_once(
                self._stage_path(binding, "receipt_finalized"),
                _canonical_model(state),
            )
            return self._read_receipt(receipt_path, binding)

    def recover(
        self, binding: BusinessBenchmarkProviderBindingV1
    ) -> BusinessBenchmarkProviderRecoveryV1:
        with self._effect_lock(binding.effect_id):
            latest = self._assert_current_locked(binding)
            if latest.stage == "receipt_finalized":
                receipt = self._read_receipt(self._receipt_path(binding), binding)
                return BusinessBenchmarkProviderRecoveryV1(
                    schema="captain.business-benchmark-provider-recovery.v1",
                    binding=binding,
                    state_sha256=latest.state_sha256,
                    outcome="terminal",
                    receipt=receipt,
                )
            if latest.stage == "fenced" and not self._has_unresolved_prior_dispatch(binding):
                outcome: Literal["no_effect", "uncertain"] = "no_effect"
            else:
                outcome = "uncertain"
            return BusinessBenchmarkProviderRecoveryV1(
                schema="captain.business-benchmark-provider-recovery.v1",
                binding=binding,
                state_sha256=latest.state_sha256,
                outcome=outcome,
            )

    def _assert_current_locked(
        self, binding: BusinessBenchmarkProviderBindingV1
    ) -> BusinessBenchmarkProviderStateV1:
        latest = self._latest_state(binding.effect_id)
        if latest is None:
            raise BusinessBenchmarkProviderStateConflictError(
                "provider effect has no registered fence"
            )
        if binding.fence < latest.binding.fence:
            raise BusinessBenchmarkStaleProviderFenceError("provider fence is stale")
        if binding.fence > latest.binding.fence:
            raise BusinessBenchmarkProviderStateConflictError(
                "provider fence is not registered"
            )
        self._require_binding(latest.binding, binding)
        return latest

    def _latest_state(self, effect_id: str) -> BusinessBenchmarkProviderStateV1 | None:
        effect_root = self._effect_root(effect_id)
        fence_dirs = sorted(
            (path for path in effect_root.glob("[0-9]*") if path.is_dir()),
            key=lambda path: int(path.name),
        )
        if not fence_dirs:
            return None
        fence_root = fence_dirs[-1]
        expected_stages: tuple[ProviderStateStage, ...] = (
            "fenced",
            "dispatching",
            "provider_terminal",
            "receipt_finalized",
        )
        states: list[BusinessBenchmarkProviderStateV1] = []
        missing_predecessor = False
        for filename, expected_stage in zip(_STAGE_FILENAMES, expected_stages, strict=True):
            path = fence_root / filename
            if not path.exists():
                missing_predecessor = True
                continue
            if missing_predecessor:
                raise BusinessBenchmarkProviderStateConflictError(
                    "provider state chain has a missing predecessor"
                )
            state = self._read_state(path)
            if state.stage != expected_stage:
                raise BusinessBenchmarkProviderStateConflictError(
                    "provider state chain has an invalid stage"
                )
            if states:
                predecessor = states[-1]
                if state.binding != predecessor.binding:
                    raise BusinessBenchmarkProviderStateConflictError(
                        "provider state chain changed its fence binding"
                    )
                if state.previous_state_sha256 != predecessor.state_sha256:
                    raise BusinessBenchmarkProviderStateConflictError(
                        "provider state chain digest does not match its predecessor"
                    )
                if (
                    predecessor.provider_receipt_sha256 is not None
                    and state.provider_receipt_sha256
                    != predecessor.provider_receipt_sha256
                ):
                    raise BusinessBenchmarkProviderStateConflictError(
                        "provider state chain changed its receipt digest"
                    )
            states.append(state)
        if not states:
            raise BusinessBenchmarkProviderStateConflictError(
                "provider fence directory contains no valid state"
            )
        return states[-1]

    def _read_stage(
        self, binding: BusinessBenchmarkProviderBindingV1, stage: ProviderStateStage
    ) -> BusinessBenchmarkProviderStateV1:
        return self._read_state(self._stage_path(binding, stage))

    def _read_state(self, path: Path) -> BusinessBenchmarkProviderStateV1:
        try:
            content = path.read_bytes()
            state = BusinessBenchmarkProviderStateV1.model_validate_json(content)
            if _canonical_model(state) != content:
                raise ValueError("provider state is not canonical")
            return state
        except (OSError, ValueError) as exc:
            raise BusinessBenchmarkProviderStateConflictError(
                "durable provider state is invalid"
            ) from exc

    def _read_receipt(
        self, path: Path, binding: BusinessBenchmarkProviderBindingV1
    ) -> BusinessBenchmarkRunReceiptV1:
        try:
            content = path.read_bytes()
            receipt = BusinessBenchmarkRunReceiptV1.model_validate_json(content)
            if _canonical_receipt(receipt) != content:
                raise ValueError("provider receipt is not canonical")
            self._validate_receipt_binding(binding, receipt)
            return receipt
        except (OSError, ValueError) as exc:
            raise BusinessBenchmarkProviderStateConflictError(
                "durable provider receipt is invalid"
            ) from exc

    def _has_unresolved_prior_dispatch(
        self, binding: BusinessBenchmarkProviderBindingV1
    ) -> bool:
        for fence_dir in self._effect_root(binding.effect_id).glob("[0-9]*"):
            if not fence_dir.is_dir() or int(fence_dir.name) >= binding.fence:
                continue
            if (fence_dir / "30-receipt-finalized.json").exists():
                continue
            if (fence_dir / "10-dispatching.json").exists():
                return True
        return False

    @staticmethod
    def _validate_receipt_binding(
        binding: BusinessBenchmarkProviderBindingV1,
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> None:
        expected: dict[str, object] = {
            "runtime_session_id": binding.runtime_session_id,
            "job_id": binding.job_id,
            "correlation_id": binding.correlation_id,
            "attempt": binding.attempt,
            "request_id": binding.request_id,
            "case_sha256": binding.case_sha256,
            "variant": binding.variant,
            "model_version": binding.model_version,
        }
        for field_name, expected_value in expected.items():
            if getattr(receipt, field_name) != expected_value:
                raise BusinessBenchmarkProviderStateConflictError(
                    f"provider receipt does not match {field_name} binding"
                )

    @staticmethod
    def _require_binding(
        existing: BusinessBenchmarkProviderBindingV1,
        requested: BusinessBenchmarkProviderBindingV1,
    ) -> None:
        if existing != requested:
            raise BusinessBenchmarkProviderStateConflictError(
                "provider fence already has different content"
            )

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
            raise BusinessBenchmarkProviderStateConflictError(
                "provider effect has different content across fences"
            )

    def _effect_root(self, effect_id: str) -> Path:
        return self._root / "effects" / effect_id

    def _fence_root(self, binding: BusinessBenchmarkProviderBindingV1) -> Path:
        return self._effect_root(binding.effect_id) / f"{binding.fence:020d}"

    def _stage_path(
        self, binding: BusinessBenchmarkProviderBindingV1, stage: ProviderStateStage
    ) -> Path:
        index = {
            "fenced": 0,
            "dispatching": 1,
            "provider_terminal": 2,
            "receipt_finalized": 3,
        }[stage]
        return self._fence_root(binding) / _STAGE_FILENAMES[index]

    def _receipt_path(self, binding: BusinessBenchmarkProviderBindingV1) -> Path:
        return self._fence_root(binding) / "receipt.json"

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
                    raise BusinessBenchmarkProviderStateConflictError(
                        "durable provider state already has different content"
                    )
        finally:
            temporary.unlink(missing_ok=True)


def _canonical_model(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "provider state")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_receipt(receipt: BusinessBenchmarkRunReceiptV1) -> bytes:
    payload = receipt.model_dump(mode="json", by_alias=True)
    _reject_unsafe_evidence(payload, "provider receipt")
    return canonical_business_benchmark_model_bytes(receipt)


def _state_digest(state: BusinessBenchmarkProviderStateV1) -> str:
    payload = state.model_dump(mode="json", by_alias=True, exclude={"state_sha256"})
    return _digest_value(payload)


def _digest_value(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provider state timestamp must be timezone-aware")
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
