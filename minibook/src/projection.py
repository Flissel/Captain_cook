"""Fail-closed Captain projection contract and canonical rendering.

Minibook owns this copy of the public v2 contract so projection HTTP writes do
not trust caller-supplied display text, tags, or fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
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
ProjectionRetireReason = Literal["duplicate", "orphaned", "v1-cutover"]
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


class ProjectionPayloadV2(BaseModel):
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


class ProjectionEventV2(BaseModel):
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
    payload: ProjectionPayloadV2

    @model_validator(mode="after")
    def validate_catalog_entry(self) -> "ProjectionEventV2":
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
    payload: ProjectionPayloadV2,
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
    payload: ProjectionPayloadV2,
) -> None:
    values = (
        payload.integration_status,
        payload.required_integration_count,
        payload.ready_integration_count,
    )
    present = tuple(value is not None for value in values)
    if any(present) and not all(present):
        raise ValueError("integration setup aggregates must be complete")
    if any(present) != (event_type == "integration.setup"):
        raise ValueError("integration aggregates are allowed only on integration setup")
    if (
        payload.ready_integration_count is not None
        and payload.required_integration_count is not None
        and payload.ready_integration_count > payload.required_integration_count
    ):
        raise ValueError("ready integration count exceeds required integration count")


class ProjectionRetireRequest(BaseModel):
    """Structured retirement command with no caller-controlled display text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: ProjectionRetireReason


@dataclass(frozen=True)
class CanonicalProjectionPost:
    title: str
    content: str
    tags: tuple[str, ...]
    content_hash: str


def projection_event_fingerprint(event: ProjectionEventV2) -> str:
    canonical = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_projection_event(event: ProjectionEventV2) -> CanonicalProjectionPost:
    payload = event.payload
    fields: list[tuple[str, str]] = [
        ("Status", _status_label(payload.status_id)),
        ("View", _view_label(payload.view)),
        ("Correlation", str(event.correlation_id)),
        ("Subject", event.subject_id),
        ("Subject version", str(event.subject_version)),
    ]
    if payload.batch_id is not None:
        fields.append(("Batch", payload.batch_id))
    if payload.batch_version is not None:
        fields.append(("Batch version", str(payload.batch_version)))
    if payload.actor_role_id is not None:
        fields.append(("Actor", _actor_label(payload.actor_role_id)))
    if payload.artifact_digest is not None:
        fields.append(("Artifact", payload.artifact_digest))
    if payload.benchmark_disposition is not None:
        fields.extend(
            (
                ("Benchmark", payload.benchmark_disposition),
                ("Candidate correctness", str(payload.candidate_correctness_bps)),
                ("Baseline correctness", str(payload.baseline_correctness_bps)),
                ("Candidate completion", str(payload.candidate_completion_bps)),
                ("Baseline completion", str(payload.baseline_completion_bps)),
                ("Cost ratio", str(payload.cost_ratio_bps)),
                ("Latency ratio", str(payload.latency_ratio_bps)),
                ("Unsafe tool uses", str(payload.unsafe_tool_uses)),
                ("Mandatory handoff misses", str(payload.mandatory_handoff_misses)),
                ("Benchmark summary", str(payload.benchmark_summary_digest)),
            )
        )
        if payload.benchmark_reason_codes:
            fields.append(("Benchmark reasons", ", ".join(payload.benchmark_reason_codes)))
    if payload.integration_status is not None:
        fields.extend(
            (
                ("Integration status", payload.integration_status),
                ("Required integrations", str(payload.required_integration_count)),
                ("Ready integrations", str(payload.ready_integration_count)),
            )
        )
    content = "\n".join(f"- **{label}:** {value}" for label, value in fields)
    title = f"[{event.event_type}] {_template_title(payload.template_id)}"
    identity_tags = (
        "captain-projection:v2",
        f"captain-event:{event.event_id}",
        f"captain-correlation:{event.correlation_id}",
        f"captain-subject:{event.subject_id}",
        f"captain-version:{event.subject_version}",
        f"captain-view:{payload.view}",
    )
    content_hash = _content_hash(title, content, identity_tags)
    return CanonicalProjectionPost(
        title=title,
        content=content,
        tags=(*identity_tags, f"captain-hash:{content_hash}"),
        content_hash=content_hash,
    )


def render_retired_projection(
    reason: ProjectionRetireReason,
) -> CanonicalProjectionPost:
    labels: dict[ProjectionRetireReason, str] = {
        "duplicate": "Duplicate projection",
        "orphaned": "Orphaned projection",
        "v1-cutover": "Legacy v1 cutover",
    }
    title = "Retired Captain projection"
    content = f"- **State:** Retired\n- **Reason:** {labels[reason]}"
    tags = (
        "captain-projection-retired:v2",
        f"captain-retired:{reason}",
    )
    return CanonicalProjectionPost(
        title=title,
        content=content,
        tags=tags,
        content_hash=_content_hash(title, content, tags),
    )


def _template_title(template_id: ProjectionTemplateId) -> str:
    return {
        "runtime_plan_requested": "Runtime planning requested",
        "runtime_plan_published": "Runtime delivery plan published",
        "runtime_blueprint_published": "Runtime blueprint published",
        "runtime_build_running": "Runtime build running",
        "runtime_build_recorded": "Runtime build result recorded",
        "automation_evidence_recorded": "Automation evidence recorded",
        "runtime_validation_recorded": "Runtime validation recorded",
        "runtime_replanning_requested": "Runtime replanning requested",
        "factory_capability_ready_to_use": "Factory capability ready to use",
        "integration_setup_status": "Integration setup status",
    }[template_id]


def _status_label(status_id: ProjectionStatusId) -> str:
    return {
        "requested": "Requested",
        "planned": "Planned",
        "ready": "Ready",
        "running": "Running",
        "built": "Built",
        "observed": "Observed",
        "validated": "Validated",
        "replanning": "Replanning",
        "ready_to_use": "Ready to use",
    }[status_id]


def _view_label(view: ProjectionView) -> str:
    return {
        "project": "Project",
        "plan": "Plan",
        "blueprint": "Blueprint",
        "build": "Build",
        "validation": "Validation",
    }[view]


def _actor_label(actor_role_id: ActorRoleId) -> str:
    return {
        "captain_planner": "Captain Planner",
        "codex_worker": "Codex Worker",
        "captain_gateway": "Captain Gateway",
    }[actor_role_id]


def _content_hash(title: str, content: str, tags: tuple[str, ...]) -> str:
    canonical = json.dumps(
        {"title": title, "content": content, "tags": tags},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
