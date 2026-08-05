from __future__ import annotations

from datetime import timezone
from typing import Any, Literal
from uuid import UUID, uuid5

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
    "capability.promoted",
    "integration.setup",
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
    "factory_capability_ready_to_use",
    "integration_setup_status",
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
    "ready_to_use",
]
ActorRoleId = Literal["captain_planner", "codex_worker", "captain_gateway"]
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
BenchmarkReasonCode = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")
]
_PROJECTION_ACKNOWLEDGEMENT_NAMESPACE = UUID(
    "cbd7e2c8-71b1-4b83-8d0c-1311590f4c50"
)


def minibook_projection_acknowledgement_id(
    projection_event_id: UUID,
    *,
    post_id: str,
    content_sha256: str,
) -> UUID:
    """Derive one replay-stable acknowledgement identity from canonical output."""

    return uuid5(
        _PROJECTION_ACKNOWLEDGEMENT_NAMESPACE,
        f"{projection_event_id}|{post_id}|{content_sha256}",
    )


class MinibookProjectionAcknowledgementV1(BaseModel):
    """Captain-owned proof that one canonical projection is durable in Minibook."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal[
        "captain.minibook-projection-acknowledgement.v1"
    ] = Field(
        default="captain.minibook-projection-acknowledgement.v1",
        alias="schema",
        serialization_alias="schema",
    )
    acknowledgement_id: UUID
    projection_event_id: UUID
    correlation_id: UUID
    subject_id: SubjectReference
    subject_version: int = Field(ge=1, strict=True)
    project_id: Literal["captain-runtime-projection-v2"]
    post_id: str = Field(
        pattern=r"^captain-projection-[0-9a-f]{32}$",
    )
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledged_at: AwareDatetime
    outcome: Literal["mirrored"]

    @field_validator("acknowledged_at")
    @classmethod
    def require_utc_acknowledgement(cls, value: AwareDatetime) -> AwareDatetime:
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("acknowledged_at must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_deterministic_acknowledgement_id(
        self,
    ) -> "MinibookProjectionAcknowledgementV1":
        expected = minibook_projection_acknowledgement_id(
            self.projection_event_id,
            post_id=self.post_id,
            content_sha256=self.content_sha256,
        )
        if self.acknowledgement_id != expected:
            raise ValueError("acknowledgement_id does not match canonical projection")
        return self


class MinibookProjectionRebuildReceiptV1(BaseModel):
    """Captain-owned proof that a full Minibook replay converged."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.minibook-projection-rebuild-receipt.v1"] = Field(
        default="captain.minibook-projection-rebuild-receipt.v1",
        alias="schema",
        serialization_alias="schema",
    )
    rebuild_id: UUID
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    job_id: UUID
    correlation_id: UUID
    projection_event_id: UUID
    acknowledgement_id: UUID
    setup_revision: int = Field(ge=1, strict=True)
    setup_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_project_id: Literal["captain-runtime-projection-v2"]
    outcome: Literal["converged"]
    occurred_at: AwareDatetime

    @field_validator("occurred_at")
    @classmethod
    def require_rebuild_at_utc(cls, value: AwareDatetime) -> AwareDatetime:
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("rebuild receipt timestamp must be UTC")
        return value.astimezone(timezone.utc)

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
    "capability.promoted": (
        "validation",
        "factory_capability_ready_to_use",
        "ready_to_use",
    ),
    "integration.setup": (
        "validation",
        "integration_setup_status",
        "observed",
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
    benchmark_disposition: Literal["passed", "failed"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    benchmark_reason_codes: tuple[BenchmarkReasonCode, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    candidate_correctness_bps: int | None = Field(
        default=None, ge=0, le=10000, exclude_if=lambda value: value is None
    )
    baseline_correctness_bps: int | None = Field(
        default=None, ge=0, le=10000, exclude_if=lambda value: value is None
    )
    candidate_completion_bps: int | None = Field(
        default=None, ge=0, le=10000, exclude_if=lambda value: value is None
    )
    baseline_completion_bps: int | None = Field(
        default=None, ge=0, le=10000, exclude_if=lambda value: value is None
    )
    cost_ratio_bps: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    latency_ratio_bps: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    unsafe_tool_uses: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    mandatory_handoff_misses: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    benchmark_summary_digest: ArtifactDigest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    integration_status: Literal[
        "missing",
        "selection_required",
        "verification_required",
        "verification_failed",
        "ready",
        "revoked",
        "expired",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    required_integration_count: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    ready_integration_count: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )


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
        _validate_benchmark_projection(self.event_type, self.payload)
        _validate_integration_setup_projection(self.event_type, self.payload)
        return self


def _validate_benchmark_projection(
    event_type: ProjectionEventType,
    payload: MinibookProjectionPayload,
) -> None:
    aggregate_values = (
        payload.benchmark_disposition,
        payload.candidate_correctness_bps,
        payload.baseline_correctness_bps,
        payload.candidate_completion_bps,
        payload.baseline_completion_bps,
        payload.cost_ratio_bps,
        payload.latency_ratio_bps,
        payload.unsafe_tool_uses,
        payload.mandatory_handoff_misses,
        payload.benchmark_summary_digest,
    )
    present = tuple(value is not None for value in aggregate_values)
    if any(present) and not all(present):
        raise ValueError("benchmark projection aggregates must be complete")
    if (any(present) or payload.benchmark_reason_codes) and event_type != "capability.promoted":
        raise ValueError("benchmark aggregates are allowed only on capability promotion")
    if payload.benchmark_disposition == "passed" and payload.benchmark_reason_codes:
        raise ValueError("passed benchmark projection cannot include failure reasons")


def _validate_integration_setup_projection(
    event_type: ProjectionEventType,
    payload: MinibookProjectionPayload,
) -> None:
    values = (
        payload.integration_status,
        payload.required_integration_count,
        payload.ready_integration_count,
    )
    present = tuple(value is not None for value in values)
    if any(present) and not all(present):
        raise ValueError("integration setup projection aggregates must be complete")
    if any(present) != (event_type == "integration.setup"):
        raise ValueError("integration setup aggregates require an integration setup event")
    if (
        payload.ready_integration_count is not None
        and payload.required_integration_count is not None
        and payload.ready_integration_count > payload.required_integration_count
    ):
        raise ValueError("ready integration count exceeds required integration count")
