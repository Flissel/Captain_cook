from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
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
ProjectionTemplateId = Literal[
    "runtime_plan_requested",
    "runtime_plan_published",
    "runtime_blueprint_published",
    "runtime_build_running",
    "runtime_build_recorded",
    "automation_evidence_recorded",
    "runtime_validation_recorded",
    "runtime_replanning_requested",
]
ProjectionStatusId = Literal[
    "requested",
    "planned",
    "ready",
    "running",
    "built",
    "observed",
    "validated",
    "replanning",
]
ActorRoleId = Literal["captain_planner", "codex_worker"]
SubjectReference = Annotated[
    str,
    StringConstraints(
        pattern=r"^subject:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
BatchReference = Annotated[
    str,
    StringConstraints(
        pattern=r"^batch:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
ArtifactDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

_EVENT_CATALOG: dict[
    ProjectionEventType,
    tuple[ProjectionView, ProjectionTemplateId, ProjectionStatusId],
] = {
    "plan.requested": ("project", "runtime_plan_requested", "requested"),
    "plan.published": ("plan", "runtime_plan_published", "planned"),
    "blueprint.published": ("blueprint", "runtime_blueprint_published", "ready"),
    "codex.running": ("build", "runtime_build_running", "running"),
    "codex.result": ("build", "runtime_build_recorded", "built"),
    "n8n.evidence": ("build", "automation_evidence_recorded", "observed"),
    "validation.recorded": (
        "validation",
        "runtime_validation_recorded",
        "validated",
    ),
    "replanning.requested": (
        "plan",
        "runtime_replanning_requested",
        "replanning",
    ),
}


class MinibookProjectionPayload(BaseModel):
    """The complete allow-list that may cross into Minibook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view: ProjectionView
    template_id: ProjectionTemplateId
    status_id: ProjectionStatusId
    batch_id: BatchReference | None = None
    batch_version: int | None = Field(default=None, ge=1)
    actor_role_id: ActorRoleId | None = None
    artifact_digest: ArtifactDigest | None = None


def redact_projection_payload(payload: dict[str, object]) -> MinibookProjectionPayload:
    """Validate the structured public contract, failing closed on free text."""

    return MinibookProjectionPayload.model_validate(payload)


class MinibookProjectionEvent(BaseModel):
    """Versioned, redacted event emitted only after authoritative commit."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.minibook-projection.v2"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None
    occurred_at: AwareDatetime
    producer: Literal["captain-gateway"]
    subject_id: SubjectReference
    subject_version: int = Field(ge=1)
    event_type: ProjectionEventType
    payload: MinibookProjectionPayload

    @field_validator("payload", mode="before")
    @classmethod
    def validate_redacted_payload(cls, value: Any) -> MinibookProjectionPayload:
        if isinstance(value, MinibookProjectionPayload):
            return value
        if not isinstance(value, dict):
            raise ValueError("projection payload must be an object")
        return redact_projection_payload(value)

    @model_validator(mode="after")
    def validate_catalog_entry(self) -> "MinibookProjectionEvent":
        expected = _EVENT_CATALOG[self.event_type]
        actual = (
            self.payload.view,
            self.payload.template_id,
            self.payload.status_id,
        )
        if actual != expected:
            raise ValueError("projection template/status does not match event type")
        return self
