"""Frozen, transport-neutral Agent Factory outcome contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_runtime.contracts import (
    ArtifactRef,
    IDENTIFIER_PATTERN,
    IntegrationIntent,
)


_VERSIONED_COMPONENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}@[1-9][0-9]*$"
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|authorization|credentials?|password|"
    r"private[_-]?key|secrets?|tokens?)(?:$|[_-])"
)
_PRIVATE_BODY_KEY_PATTERN = re.compile(r"(?i)(?:holdout.*body|transcripts?)")
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:\bsk-(?:proj-)?[a-z0-9_-]{8,}|\bbearer\s+\S+|"
    r"\b(?:api[_ -]?key|authorization|credential|password|secret|token)\b\s*[:=])"
)
_ABSOLUTE_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[a-z]:[\\/]|\\\\|/(?:home|users|mnt|tmp)/)"
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FactoryTerminalState(str, Enum):
    READY_TO_USE = "ready_to_use"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    REJECTED = "rejected"


class PackageArtifact(_FrozenContract):
    """One content-addressed file in the sealed logical capability package."""

    path: str = Field(min_length=1)
    kind: Literal[
        "team_manifest",
        "autogen_source",
        "n8n_workflow",
        "local_adapter",
        "skill",
        "test",
        "evidence",
        "runbook",
    ]
    reference: ArtifactRef

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        if "\\" in value or re.match(r"(?i)^[a-z]:", value):
            raise ValueError("package artifact path must be a safe relative POSIX path")
        path = PurePosixPath(value)
        if path.is_absolute() or "." in path.parts or ".." in path.parts or value.endswith("/"):
            raise ValueError("package artifact path must be a safe relative POSIX path")
        return path.as_posix()


class AssertionOutcome(_FrozenContract):
    """Public assertion result; private case bodies remain outside the contract."""

    assertion_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["passed", "failed"]
    integration_intent: IntegrationIntent = IntegrationIntent.NONE
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        return _unique_artifact_refs(value, "assertion evidence refs")


class PrivateHoldoutReceipt(_FrozenContract):
    """Opaque result identity for a Captain-owned private holdout."""

    holdout_id: str = Field(pattern=IDENTIFIER_PATTERN)
    assertion_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["passed", "failed"]
    evidence_ref: ArtifactRef


class ControlledRecoveryReceipt(_FrozenContract):
    """Accepted controlled recovery proof without the private scenario body."""

    recovery_id: str = Field(pattern=IDENTIFIER_PATTERN)
    assertion_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal["passed"]
    evidence_ref: ArtifactRef


class CapabilityPackageManifestV1(_FrozenContract):
    schema_name: Literal["captain.capability-package.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    capability_id: str = Field(pattern=IDENTIFIER_PATTERN)
    capability_version: int = Field(ge=1, strict=True)
    factory_job_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    source_ref: ArtifactRef
    team_manifest_ref: ArtifactRef
    artifacts: tuple[PackageArtifact, ...] = Field(min_length=1)
    assertion_outcomes: tuple[AssertionOutcome, ...] = Field(min_length=1)
    private_holdout_receipts: tuple[PrivateHoldoutReceipt, ...] = Field(min_length=1)
    recovery_receipt: ControlledRecoveryReceipt
    release_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=4)
    skill_usage_receipt_ref: ArtifactRef
    tool_gaps: tuple[ToolGapMarker, ...] = ()
    runbook_ref: ArtifactRef

    @model_validator(mode="before")
    @classmethod
    def reject_private_content(cls, value: object) -> object:
        _reject_private_content(value, "capability package")
        return value

    @model_validator(mode="after")
    def require_closed_logical_package(self) -> "CapabilityPackageManifestV1":
        paths = tuple(item.path for item in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("package artifact paths must be unique")
        digests = tuple(item.reference.sha256 for item in self.artifacts)
        if len(digests) != len(set(digests)):
            raise ValueError("package artifact digests must be unique")

        required_roots = (
            "team-manifest.json",
            "autogen/",
            "skills/",
            "tests/",
            "evidence/",
            "RUNBOOK.md",
        )
        missing = tuple(root for root in required_roots if not _contains_root(paths, root))
        if missing:
            raise ValueError("missing logical package root: " + ", ".join(missing))

        if any(
            outcome.integration_intent is IntegrationIntent.N8N
            for outcome in self.assertion_outcomes
        ) and not _contains_root(paths, "n8n/"):
            raise ValueError("declared n8n assertions require the n8n/ package root")
        if any(item.kind == "local_adapter" for item in self.artifacts) and not any(
            item.path.startswith("adapters/") for item in self.artifacts
        ):
            raise ValueError("declared local adapters require the adapters/ package root")

        artifacts_by_path = {item.path: item.reference for item in self.artifacts}
        if artifacts_by_path["team-manifest.json"] != self.team_manifest_ref:
            raise ValueError("team_manifest_ref must match team-manifest.json")
        if artifacts_by_path["RUNBOOK.md"] != self.runbook_ref:
            raise ValueError("runbook_ref must match RUNBOOK.md")

        assertion_ids = tuple(item.assertion_id for item in self.assertion_outcomes)
        _require_unique_nonblank(assertion_ids, "assertion outcome IDs")
        holdout_ids = tuple(item.holdout_id for item in self.private_holdout_receipts)
        _require_unique_nonblank(holdout_ids, "private holdout receipt IDs")
        _unique_artifact_refs(self.release_evidence_refs, "release evidence refs")
        return self


class ExecutionOutcomeV1(_FrozenContract):
    schema_name: Literal["captain.execution-outcome.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    capability_id: str = Field(pattern=IDENTIFIER_PATTERN)
    capability_version: int = Field(ge=1, strict=True)
    team_version: int = Field(ge=1, strict=True)
    correlation_id: UUID
    command_id: UUID
    result_id: UUID
    business_output: JsonValue | None = None
    output_ref: ArtifactRef | None = None
    assertion_outcomes: tuple[AssertionOutcome, ...] = Field(min_length=1)
    tool_versions: tuple[str, ...] = ()
    workflow_versions: tuple[str, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    status: Literal["succeeded", "failed", "escalated"]
    escalation_ref: ArtifactRef | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_private_content(cls, value: object) -> object:
        _reject_private_content(value, "execution outcome")
        return value

    @field_validator("tool_versions", "workflow_versions")
    @classmethod
    def require_versioned_components(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("component versions must not contain duplicates")
        if any(re.fullmatch(_VERSIONED_COMPONENT_PATTERN, item) is None for item in value):
            raise ValueError("component versions must use the name@positive-version form")
        return value

    @model_validator(mode="after")
    def require_consistent_execution_outcome(self) -> "ExecutionOutcomeV1":
        if (self.business_output is None) == (self.output_ref is None):
            raise ValueError("execution outcome requires exactly one business output form")
        if self.status == "escalated" and self.escalation_ref is None:
            raise ValueError("escalated execution outcomes require escalation_ref")
        if self.status != "escalated" and self.escalation_ref is not None:
            raise ValueError("only escalated execution outcomes may include escalation_ref")
        assertion_ids = tuple(item.assertion_id for item in self.assertion_outcomes)
        _require_unique_nonblank(assertion_ids, "execution assertion outcome IDs")
        _unique_artifact_refs(self.evidence_refs, "execution evidence refs")
        return self


class FactoryTerminalDecision(_FrozenContract):
    schema_name: Literal["captain.factory-terminal-decision.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    decision_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    state: FactoryTerminalState
    reasons: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[ArtifactRef, ...] = ()
    decided_at: datetime

    @field_validator("reasons")
    @classmethod
    def require_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_nonblank(value, "terminal decision reasons")

    @field_validator("evidence_refs")
    @classmethod
    def require_unique_evidence(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        return _unique_artifact_refs(value, "terminal decision evidence refs")

    @field_validator("decided_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must include a UTC offset")
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("decided_at must be UTC")
        return value.astimezone(timezone.utc)


def _contains_root(paths: tuple[str, ...], root: str) -> bool:
    if root.endswith("/"):
        return any(path.startswith(root) for path in paths)
    return root in paths


def _require_unique_nonblank(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must not contain duplicates")
    if any(not item.strip() for item in value):
        raise ValueError(f"{field_name} must not contain blanks")
    return value


def _unique_artifact_refs(
    value: tuple[ArtifactRef, ...], field_name: str
) -> tuple[ArtifactRef, ...]:
    identities = tuple((item.uri, item.sha256, item.media_type) for item in value)
    if len(identities) != len(set(identities)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return value


def _reject_private_content(value: object, location: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            normalized_key = key_text.replace("-", "_")
            if _SECRET_KEY_PATTERN.search(normalized_key) or _PRIVATE_BODY_KEY_PATTERN.search(
                normalized_key
            ):
                raise ValueError(f"{location} contains a private field")
            _reject_private_content(nested, f"{location}.{key_text}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _reject_private_content(nested, f"{location}[{index}]")
        return
    if isinstance(value, str):
        if _SECRET_VALUE_PATTERN.search(value):
            raise ValueError(f"{location} contains a private value")
        if _ABSOLUTE_LOCAL_PATH_PATTERN.search(value):
            raise ValueError(f"{location} contains an unrestricted local path")
