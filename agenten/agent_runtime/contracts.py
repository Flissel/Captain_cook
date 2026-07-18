"""Strict contracts shared by Captain, Hermes, Codex, and Minibook adapters."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
BLUEPRINT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)(?:$|_)"
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RuntimeOperation(str, Enum):
    HERMES_PLAN = "hermes.plan"
    HERMES_DESIGN_AGENT = "hermes.design_agent"
    CODEX_RUN = "codex.run"
    CODEX_RESUME = "codex.resume"
    CODEX_STATUS = "codex.status"
    CODEX_CANCEL = "codex.cancel"
    CODEX_HEARTBEAT = "codex.heartbeat"


class IntegrationIntent(str, Enum):
    NONE = "none"
    N8N = "n8n"


class CapabilityProfile(str, Enum):
    PLANNER = "planner"
    AGENT_DESIGNER = "agent-designer"
    CODE_BUILDER = "code-builder"
    N8N_BUILDER = "n8n-builder"


class RuntimeStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    POLICY_FAILED = "policy_failed"
    CANCELLED = "cancelled"


class ArtifactRef(_FrozenContract):
    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")

    @field_validator("uri")
    @classmethod
    def require_opaque_artifact_uri(cls, value: str) -> str:
        if not value.startswith("artifact://"):
            raise ValueError("artifact refs must use artifact:// URIs")
        return value


class RuntimeLimits(_FrozenContract):
    wall_seconds: int = Field(ge=1, le=3600, strict=True)
    max_iterations: int = Field(ge=1, le=10, strict=True)


class AgentRuntimeCommandPayload(_FrozenContract):
    operation: RuntimeOperation
    project_id: str = Field(pattern=IDENTIFIER_PATTERN)
    batch_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    subtask_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    workspace_ref: str | None = Field(default=None, min_length=1)
    prompt_ref: ArtifactRef
    integration_intent: IntegrationIntent = IntegrationIntent.NONE
    capability_profile: CapabilityProfile
    limits: RuntimeLimits

    @field_validator("workspace_ref")
    @classmethod
    def require_opaque_workspace_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("workspace://"):
            raise ValueError("workspace_ref must use workspace://")
        return value

    @model_validator(mode="after")
    def require_operation_contract(self) -> "AgentRuntimeCommandPayload":
        codex_operations = {
            RuntimeOperation.CODEX_RUN,
            RuntimeOperation.CODEX_RESUME,
            RuntimeOperation.CODEX_STATUS,
            RuntimeOperation.CODEX_CANCEL,
            RuntimeOperation.CODEX_HEARTBEAT,
        }
        if self.operation in codex_operations and not all(
            (self.batch_id, self.subtask_id, self.workspace_ref)
        ):
            raise ValueError("Codex operations require batch, subtask, and workspace refs")
        if self.operation is RuntimeOperation.HERMES_PLAN and self.capability_profile is not CapabilityProfile.PLANNER:
            raise ValueError("hermes.plan requires the planner profile")
        if (
            self.operation is RuntimeOperation.HERMES_DESIGN_AGENT
            and self.capability_profile is not CapabilityProfile.AGENT_DESIGNER
        ):
            raise ValueError("hermes.design_agent requires the agent-designer profile")
        if self.capability_profile is CapabilityProfile.N8N_BUILDER:
            if self.integration_intent is not IntegrationIntent.N8N:
                raise ValueError("n8n-builder requires integration_intent=n8n")
        elif self.integration_intent is IntegrationIntent.N8N:
            raise ValueError("integration_intent=n8n requires n8n-builder")
        if self.operation in codex_operations and self.capability_profile not in {
            CapabilityProfile.CODE_BUILDER,
            CapabilityProfile.N8N_BUILDER,
        }:
            raise ValueError("Codex operations require a builder profile")
        return self


class AgentRuntimeCommand(_FrozenContract):
    schema_name: Literal["captain.agent-runtime-command.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    occurred_at: datetime
    producer: Literal["captain-swarm", "captain"]
    subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_version: int = Field(ge=1, strict=True)
    payload: AgentRuntimeCommandPayload

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def subject_matches_subtask(self) -> "AgentRuntimeCommand":
        if self.payload.subtask_id is not None and self.subject_id != self.payload.subtask_id:
            raise ValueError("subject_id must match payload.subtask_id")
        return self


class CapabilityGrant(_FrozenContract):
    schema_name: Literal["captain.capability-grant.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    grant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    command_id: UUID
    batch_id: str = Field(pattern=IDENTIFIER_PATTERN)
    batch_version: int = Field(ge=1, strict=True)
    subtask_id: str = Field(pattern=IDENTIFIER_PATTERN)
    workspace_ref: str = Field(min_length=1)
    profile: CapabilityProfile
    capabilities: tuple[str, ...] = Field(min_length=1)
    mcp_servers: tuple[str, ...] = ()
    issued_at: datetime
    expires_at: datetime

    @field_validator("workspace_ref")
    @classmethod
    def require_workspace_uri(cls, value: str) -> str:
        if not value.startswith("workspace://"):
            raise ValueError("workspace_ref must use workspace://")
        return value

    @model_validator(mode="after")
    def validate_lifetime_and_capabilities(self) -> "CapabilityGrant":
        issued_at = _require_utc(self.issued_at)
        expires_at = _require_utc(self.expires_at)
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must not contain duplicates")
        if len(self.mcp_servers) != len(set(self.mcp_servers)):
            raise ValueError("mcp_servers must not contain duplicates")
        if self.profile is CapabilityProfile.N8N_BUILDER:
            if "mcp.n8n" not in self.capabilities or self.mcp_servers != ("n8n-mcp",):
                raise ValueError("n8n-builder grants require only the n8n-mcp server")
        elif self.mcp_servers:
            raise ValueError("non-n8n grants cannot include MCP servers")
        return self


class AgentRuntimeResult(_FrozenContract):
    schema_name: Literal["captain.agent-runtime-result.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    event_id: UUID
    command_id: UUID
    correlation_id: UUID
    occurred_at: datetime
    producer: Literal["agent-runtime", "hermes-runtime"]
    subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_version: int = Field(ge=1, strict=True)
    grant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    operation: RuntimeOperation
    status: RuntimeStatus
    session_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    error: str | None = Field(default=None, min_length=1)

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def status_has_consistent_error(self) -> "AgentRuntimeResult":
        failures = {
            RuntimeStatus.FAILED,
            RuntimeStatus.INFRASTRUCTURE_FAILED,
            RuntimeStatus.POLICY_FAILED,
        }
        if self.status in failures and self.error is None:
            raise ValueError("failed runtime results require an error")
        if self.status not in failures and self.error is not None:
            raise ValueError("non-failed runtime results cannot contain an error")
        return self


class MinibookReference(_FrozenContract):
    project_id: str = Field(pattern=IDENTIFIER_PATTERN)
    post_id: str = Field(pattern=IDENTIFIER_PATTERN)


class HermesPlanResult(_FrozenContract):
    schema_name: Literal["captain.hermes-plan-result.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    project_id: str = Field(pattern=IDENTIFIER_PATTERN)
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    plan_ref: ArtifactRef
    decision_log_ref: ArtifactRef
    blueprint_refs: tuple[ArtifactRef, ...] = ()
    assumptions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    integration_intents: tuple[IntegrationIntent, ...] = ()
    minibook: MinibookReference
    planner_id: str = Field(pattern=IDENTIFIER_PATTERN)
    runtime_provenance: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def validate_timestamps_and_intents(self) -> "HermesPlanResult":
        if _require_utc(self.ended_at) < _require_utc(self.started_at):
            raise ValueError("ended_at cannot precede started_at")
        if len(self.integration_intents) != len(set(self.integration_intents)):
            raise ValueError("integration_intents must not contain duplicates")
        return self


class AgentLimits(_FrozenContract):
    max_turns: int = Field(ge=1, le=50, strict=True)
    wall_seconds: int = Field(ge=1, le=3600, strict=True)


class AgentEvaluationCase(_FrozenContract):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    assertion: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class AgentBlueprint(_FrozenContract):
    schema_name: Literal["captain.agent-blueprint.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    name: str = Field(pattern=BLUEPRINT_NAME_PATTERN)
    purpose: str = Field(min_length=1)
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    system_prompt_ref: ArtifactRef
    tools: tuple[str, ...] = ()
    integration_intent: IntegrationIntent = IntegrationIntent.NONE
    n8n_tool_families: tuple[str, ...] = ()
    handoffs: tuple[str, ...] = ()
    limits: AgentLimits
    evaluation_cases: tuple[AgentEvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_blueprint_boundaries(self) -> "AgentBlueprint":
        for field_name, value in (("inputs", self.inputs), ("outputs", self.outputs)):
            secret_key = _find_secret_key(value)
            if secret_key is not None:
                raise ValueError(f"{field_name} contains secret-bearing field: {secret_key}")
        for name, values in (
            ("tools", self.tools),
            ("n8n_tool_families", self.n8n_tool_families),
            ("handoffs", self.handoffs),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
        if self.integration_intent is IntegrationIntent.N8N and not self.n8n_tool_families:
            raise ValueError("n8n intent requires tool families")
        if self.integration_intent is IntegrationIntent.NONE and self.n8n_tool_families:
            raise ValueError("n8n tool families require n8n intent")
        case_ids = [case.case_id for case in self.evaluation_cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must not contain duplicates")
        return self


def canonical_json_bytes(model: BaseModel) -> bytes:
    """Return deterministic UTF-8 JSON for cross-repository fixture checks."""

    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    normalized = value.astimezone(timezone.utc)
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamps must be UTC")
    return normalized


def _find_secret_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _SECRET_KEY_PATTERN.search(str(key)):
                return str(key)
            found = _find_secret_key(nested)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_secret_key(nested)
            if found is not None:
                return found
    return None
