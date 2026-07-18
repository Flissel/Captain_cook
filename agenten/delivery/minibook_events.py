from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from typing_extensions import Annotated


ProjectionEventType = Literal[
    "plan.requested",
    "plan.published",
    "blueprint.published",
    "codex.running",
    "codex.result",
    "n8n.evidence",
    "validation.recorded",
    "replanning.requested",
]
ProjectionView = Literal["project", "plan", "blueprint", "build", "validation"]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ArtifactDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

_FORBIDDEN_KEY_PARTS = ("token", "password", "secret", "holdout", "prompt", "transcript")
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


class MinibookProjectionPayload(BaseModel):
    """The complete allow-list that may cross into Minibook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view: ProjectionView
    batch_id: NonEmptyText | None = None
    batch_version: int | None = Field(default=None, ge=1)
    public_title: NonEmptyText
    status: NonEmptyText
    assignee_display_name: NonEmptyText | None = None
    artifact_digest: ArtifactDigest | None = None
    evidence_summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ] | None = None


def _reject_forbidden_projection_data(value: object, *, location: str = "payload") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).casefold()
            if any(part in normalized_key for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError(f"forbidden projection key at {location}.{key}")
            _reject_forbidden_projection_data(nested, location=f"{location}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_forbidden_projection_data(nested, location=f"{location}[{index}]")
        return
    if isinstance(value, str) and _ABSOLUTE_PATH.match(value.strip()):
        raise ValueError(f"absolute paths are forbidden at {location}")


def redact_projection_payload(payload: dict[str, object]) -> MinibookProjectionPayload:
    """Validate the redacted allow-list, failing closed on unsafe input."""

    _reject_forbidden_projection_data(payload)
    return MinibookProjectionPayload.model_validate(payload)


class MinibookProjectionEvent(BaseModel):
    """Versioned, redacted event emitted only after authoritative commit."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.minibook-projection.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None
    occurred_at: AwareDatetime
    producer: Literal["captain-gateway"]
    subject_id: NonEmptyText
    subject_version: int = Field(ge=1)
    event_type: ProjectionEventType
    payload: MinibookProjectionPayload

    @field_validator("subject_id")
    @classmethod
    def validate_public_subject_id(cls, value: str) -> str:
        _reject_forbidden_projection_data(value, location="subject_id")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def validate_redacted_payload(cls, value: Any) -> MinibookProjectionPayload:
        if isinstance(value, MinibookProjectionPayload):
            return value
        if not isinstance(value, dict):
            raise ValueError("projection payload must be an object")
        return redact_projection_payload(value)
