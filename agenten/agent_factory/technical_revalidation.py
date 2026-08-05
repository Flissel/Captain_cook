"""Captain authority for replaying one failed technical run without rebuilding."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef


TECHNICAL_REVALIDATION_RUNTIME_PATHS = (
    "agenten/agent_factory/team_execution.py",
    "agenten/agent_factory/candidate_evaluation.py",
    "agenten/agent_factory/state_machine.py",
    "gateway/agent_factory_live_composition.py",
    "gateway/store.py",
)
TECHNICAL_REVALIDATION_EVALUATOR_PATHS = (
    "agenten/agent_factory/business_benchmark_technical_holdout.py",
)


class FactoryTechnicalRevalidationAuthorizationV1(BaseModel):
    """One-shot, content-addressed Captain permission for the same candidate."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    schema_name: Literal[
        "captain.factory-technical-revalidation-authorization.v1"
    ] = Field(alias="schema", serialization_alias="schema")
    artifact_ref: ArtifactRef
    producer: Literal["captain"]
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    source_block_id: UUID
    source_evidence_ref: ArtifactRef
    candidate_ref: ArtifactRef
    holdout_ref: PrivateHoldoutRef
    reason: Literal["host_runtime_corrected"]
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_additional_cost_usd: Decimal
    budget_remaining_usd: Decimal
    issued_at: datetime
    expires_at: datetime

    @field_validator(
        "maximum_additional_cost_usd",
        "budget_remaining_usd",
        mode="before",
    )
    @classmethod
    def require_decimal(cls, value: object) -> Decimal:
        if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
            raise ValueError("technical revalidation costs must be decimal strings")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("technical revalidation cost is invalid") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("technical revalidation costs must be finite and non-negative")
        return parsed

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("technical revalidation timestamps must be UTC")
        return value

    @model_validator(mode="after")
    def require_bounded_window(self) -> "FactoryTechnicalRevalidationAuthorizationV1":
        if (
            self.maximum_additional_cost_usd <= 0
            or self.maximum_additional_cost_usd > self.budget_remaining_usd
            or self.expires_at <= self.issued_at
        ):
            raise ValueError("technical revalidation authority is unbounded")
        return self


def technical_revalidation_binding(
    authorization: FactoryTechnicalRevalidationAuthorizationV1,
) -> dict[str, object]:
    return authorization.model_dump(
        mode="json",
        by_alias=True,
        exclude={"artifact_ref"},
    )


def technical_revalidation_sha256(
    authorization: FactoryTechnicalRevalidationAuthorizationV1,
) -> str:
    encoded = json.dumps(
        technical_revalidation_binding(authorization),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_technical_revalidation_authorization(
    authorization: FactoryTechnicalRevalidationAuthorizationV1,
) -> FactoryTechnicalRevalidationAuthorizationV1:
    digest = technical_revalidation_sha256(authorization)
    expected = ArtifactRef(
        uri=f"artifact://factory/technical-revalidation/{digest}",
        sha256=digest,
        media_type="application/json",
    )
    if authorization.artifact_ref != expected:
        raise ValueError("technical revalidation authorization digest mismatch")
    return authorization


def build_technical_revalidation_authorization(
    **values: object,
) -> FactoryTechnicalRevalidationAuthorizationV1:
    placeholder = ArtifactRef(
        uri=f"artifact://factory/technical-revalidation/{'0' * 64}",
        sha256="0" * 64,
        media_type="application/json",
    )
    authorization = FactoryTechnicalRevalidationAuthorizationV1(
        schema_name="captain.factory-technical-revalidation-authorization.v1",
        artifact_ref=placeholder,
        producer="captain",
        **values,
    )
    digest = technical_revalidation_sha256(authorization)
    return authorization.model_copy(
        update={
            "artifact_ref": ArtifactRef(
                uri=f"artifact://factory/technical-revalidation/{digest}",
                sha256=digest,
                media_type="application/json",
            )
        }
    )


def technical_revalidation_runtime_sha256(
    repository_root: Path,
    relative_paths: Iterable[str],
) -> str:
    root = repository_root.resolve()
    entries: list[dict[str, object]] = []
    for relative in sorted(set(relative_paths)):
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("technical revalidation runtime path escapes repository") from exc
        if not path.is_file():
            raise ValueError("technical revalidation runtime file is missing")
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class FilesystemFactoryTechnicalRevalidationAuthority:
    """Validate the exact Captain artifact again immediately before dispatch."""

    def __init__(
        self,
        root: Path,
        *,
        repository_root: Path,
        runtime_paths: tuple[str, ...],
        evaluator_paths: tuple[str, ...],
    ) -> None:
        self._root = root.resolve()
        self._repository_root = repository_root.resolve()
        self._runtime_paths = runtime_paths
        self._evaluator_paths = evaluator_paths

    def persist(
        self,
        authorization: FactoryTechnicalRevalidationAuthorizationV1,
    ) -> None:
        validate_technical_revalidation_authorization(authorization)
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / f"{authorization.artifact_ref.sha256}.json"
        encoded = authorization.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode("utf-8")
        if target.exists():
            if target.read_bytes() != encoded:
                raise ValueError("technical revalidation artifact already differs")
            return
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def active(
        self,
        *,
        job: AgentFactoryJobV3,
        action: FactoryAction,
        budget: FactoryBudgetProjection,
        now: datetime,
        code_revision: str,
        candidate_ref: ArtifactRef,
        holdout_ref: PrivateHoldoutRef,
    ) -> FactoryTechnicalRevalidationAuthorizationV1:
        if (
            action.kind is not FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION
            or action.authorization_ref is None
            or action.supersedes_ref is None
        ):
            raise ValueError("technical revalidation action is not fully bound")
        target = self._root / f"{action.authorization_ref.sha256}.json"
        if not target.is_file():
            raise ValueError("technical revalidation authorization is unavailable")
        authorization = validate_technical_revalidation_authorization(
            FactoryTechnicalRevalidationAuthorizationV1.model_validate_json(
                target.read_bytes()
            )
        )
        if (
            authorization.artifact_ref != action.authorization_ref
            or authorization.source_evidence_ref != action.supersedes_ref
            or authorization.job_id != job.job_id
            or authorization.correlation_id != job.correlation_id
            or authorization.subject_version != job.subject_version
            or authorization.attempt != action.attempt
            or authorization.candidate_ref != candidate_ref
            or authorization.holdout_ref != holdout_ref
            or authorization.holdout_ref not in job.private_holdout_refs
            or authorization.code_revision != code_revision
            or not authorization.issued_at <= now < authorization.expires_at
            or budget.job_id != job.job_id
            or budget.reserved_usd != 0
            or budget.remaining_usd < authorization.maximum_additional_cost_usd
            or authorization.runtime_sha256
            != technical_revalidation_runtime_sha256(
                self._repository_root,
                self._runtime_paths,
            )
            or authorization.evaluator_sha256
            != technical_revalidation_runtime_sha256(
                self._repository_root,
                self._evaluator_paths,
            )
        ):
            raise ValueError("technical revalidation authorization is stale or mixed")
        return authorization


__all__ = [
    "FactoryTechnicalRevalidationAuthorizationV1",
    "FilesystemFactoryTechnicalRevalidationAuthority",
    "TECHNICAL_REVALIDATION_EVALUATOR_PATHS",
    "TECHNICAL_REVALIDATION_RUNTIME_PATHS",
    "build_technical_revalidation_authorization",
    "technical_revalidation_runtime_sha256",
    "technical_revalidation_sha256",
    "validate_technical_revalidation_authorization",
]
