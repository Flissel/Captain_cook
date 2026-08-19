"""Strict contracts shared by Captain, Hermes, Codex, and Minibook adapters."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
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
    FACTORY_ARCHITECT = "factory-architect"
    FACTORY_TOOL_INTEGRATOR = "factory-tool-integrator"
    FACTORY_REAL_CASE_TESTER = "factory-real-case-tester"
    FACTORY_QUALITY_WARDEN = "factory-quality-warden"


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
    maximum_cost_usd: Decimal | None = None
    budget_reservation_id: UUID | None = None
    cost_authority_ref: str | None = Field(default=None, min_length=1)
    cost_job_id: UUID | None = None
    cost_run_id: UUID | None = None
    cost_input_id: str | None = Field(default=None, min_length=1)
    cost_capability_id: str | None = Field(default=None, min_length=1)
    cost_capability_version: int | None = Field(default=None, ge=1, strict=True)
    provider_proxy_url: str | None = Field(default=None, pattern=r"^http://127\.0\.0\.1:[0-9]{1,5}/v1$")
    provider_policy_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_price_card_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_context_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_session_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    provider_result_id: UUID | None = None

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
        planner_profiles = {
            CapabilityProfile.PLANNER,
            CapabilityProfile.FACTORY_ARCHITECT,
        }
        if (
            self.operation is RuntimeOperation.HERMES_PLAN
            and self.capability_profile not in planner_profiles
        ):
            raise ValueError("hermes.plan requires the planner profile")
        designer_profiles = {
            CapabilityProfile.AGENT_DESIGNER,
            CapabilityProfile.FACTORY_ARCHITECT,
        }
        if (
            self.operation is RuntimeOperation.HERMES_DESIGN_AGENT
            and self.capability_profile not in designer_profiles
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
            CapabilityProfile.FACTORY_TOOL_INTEGRATOR,
        }:
            raise ValueError("Codex operations require a builder profile")
        cost_fields = (
            self.maximum_cost_usd,
            self.budget_reservation_id,
            self.cost_authority_ref,
            self.cost_job_id,
            self.cost_run_id,
            self.cost_input_id,
            self.cost_capability_id,
            self.cost_capability_version,
        )
        if any(value is not None for value in cost_fields) and not all(
            value is not None for value in cost_fields
        ):
            raise ValueError("runtime cost authority must be complete")
        if self.maximum_cost_usd is not None and (
            not self.maximum_cost_usd.is_finite() or self.maximum_cost_usd <= 0
        ):
            raise ValueError("runtime maximum cost must be finite and positive")
        provider_fields = (
            self.provider_proxy_url,
            self.provider_policy_sha256,
            self.provider_price_card_sha256,
            self.provider_context_sha256,
            self.provider_session_id,
            self.provider_result_id,
        )
        if any(value is not None for value in provider_fields) and not all(
            value is not None for value in provider_fields
        ):
            raise ValueError("runtime provider proxy binding must be complete")
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


class CapabilityGrantRevocation(_FrozenContract):
    """Captain's append-only invalidation of an otherwise valid grant."""

    schema_name: Literal["captain.capability-grant-revocation.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    revocation_id: UUID
    grant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    command_id: UUID
    revoked_at: datetime
    reason: Literal["captain_cancelled", "policy_violation", "operator_cancelled"]

    @field_validator("revoked_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)


class RuntimeResumeCostAuthorityV1(_FrozenContract):
    """Authenticated Gateway authority for one bounded resume effect."""

    schema_name: Literal["captain.runtime-resume-cost-authority.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    authorization_receipt_id: UUID
    cost_authority_ref: str = Field(
        pattern=r"^gateway://capability-resume-authorizations/"
    )
    reservation_id: UUID
    job_id: UUID
    run_id: UUID
    input_id: str = Field(min_length=1)
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    command_id: UUID
    ceiling_usd: Decimal
    expires_at: datetime
    hard_ceiling_enforced: bool = Field(strict=True)
    metering_mode: Literal["provider_usage_receipt", "unavailable"]
    provider_proxy_url: str | None = Field(default=None, pattern=r"^http://127\.0\.0\.1:[0-9]{1,5}/v1$")
    provider_policy_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_price_card_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_context_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_session_id: str | None = Field(default=None, min_length=1)
    provider_result_id: UUID | None = None

    @field_validator("ceiling_usd", mode="before")
    @classmethod
    def require_positive_ceiling(cls, value: object) -> Decimal:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("resume cost ceiling must be finite and positive")
        return amount

    @field_validator("expires_at")
    @classmethod
    def require_utc_expiry(cls, value: datetime) -> datetime:
        return _require_utc(value)


class RuntimeUsagePricingSnapshotV1(_FrozenContract):
    """Immutable model allowlist and token pricing used for independent verification."""

    schema_name: Literal["captain.runtime-usage-pricing-snapshot.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    snapshot_id: str = Field(pattern=IDENTIFIER_PATTERN)
    provider: Literal["openai"]
    model: str = Field(pattern=IDENTIFIER_PATTERN)
    input_cost_per_million_usd: Decimal
    cached_input_cost_per_million_usd: Decimal
    output_cost_per_million_usd: Decimal
    effective_at: datetime
    expires_at: datetime

    @field_validator(
        "input_cost_per_million_usd",
        "cached_input_cost_per_million_usd",
        "output_cost_per_million_usd",
        mode="before",
    )
    @classmethod
    def require_nonnegative_price(cls, value: object) -> Decimal:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("runtime usage price must be finite and non-negative")
        return amount

    @field_validator("effective_at", "expires_at")
    @classmethod
    def require_utc_price_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_active_price_window(self) -> "RuntimeUsagePricingSnapshotV1":
        if self.expires_at <= self.effective_at:
            raise ValueError("runtime usage pricing window is invalid")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()

    def cost(
        self,
        *,
        input_units: int,
        cached_input_units: int,
        output_units: int,
    ) -> Decimal:
        if (
            min(input_units, cached_input_units, output_units) < 0
            or cached_input_units > input_units
        ):
            raise ValueError("runtime usage token counts are invalid")
        cost = (
            Decimal(input_units - cached_input_units)
            * self.input_cost_per_million_usd
            + Decimal(cached_input_units)
            * self.cached_input_cost_per_million_usd
            + Decimal(output_units) * self.output_cost_per_million_usd
        ) / Decimal(1_000_000)
        return cost.quantize(Decimal("0.000001"))


class RuntimeProviderUsageReceiptV1(_FrozenContract):
    """Raw-free provider usage persisted in CAS and independently repriced."""

    schema_name: Literal["captain.runtime-provider-usage-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    receipt_id: UUID
    request_id: UUID
    command_id: UUID
    result_id: UUID
    reservation_id: UUID
    job_id: UUID
    run_id: UUID
    input_id: str = Field(min_length=1)
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    session_id: str = Field(pattern=IDENTIFIER_PATTERN)
    provider: Literal["openai"]
    model: str = Field(min_length=1)
    input_units: int = Field(ge=0, strict=True)
    cached_input_units: int = Field(ge=0, strict=True)
    output_units: int = Field(ge=0, strict=True)
    actual_cost_usd: Decimal
    pricing_snapshot_id: str = Field(pattern=IDENTIFIER_PATTERN)
    pricing_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    started_at: datetime
    ended_at: datetime

    @field_validator("actual_cost_usd", mode="before")
    @classmethod
    def require_known_receipt_cost(cls, value: object) -> Decimal:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("runtime provider cost must be finite and non-negative")
        return amount

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_utc_receipt_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_valid_receipt(self) -> "RuntimeProviderUsageReceiptV1":
        if self.cached_input_units > self.input_units or self.ended_at < self.started_at:
            raise ValueError("runtime provider usage receipt is invalid")
        return self


class RuntimeProviderCostSettlementV1(_FrozenContract):
    """Provider-authoritative immutable usage settlement for one exact call."""

    schema_name: Literal["captain.runtime-provider-cost-settlement.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    settlement_id: UUID
    provider_call_id: str = Field(pattern=IDENTIFIER_PATTERN)
    receipt: RuntimeProviderUsageReceiptV1
    receipt_sha256: str = Field(pattern=SHA256_PATTERN)
    settled_at: datetime

    @field_validator("settled_at")
    @classmethod
    def require_utc_settlement_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_digest_bound_settlement(self) -> "RuntimeProviderCostSettlementV1":
        digest = hashlib.sha256(canonical_json_bytes(self.receipt)).hexdigest()
        if self.receipt_sha256 != digest or self.settled_at < self.receipt.ended_at:
            raise ValueError("runtime provider settlement is not digest bound")
        return self


class RuntimeCostEvidenceV1(_FrozenContract):
    """Provider-originated actual usage for one runtime resume command."""

    schema_name: Literal["captain.runtime-cost-evidence.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    receipt_id: UUID
    command_id: UUID
    result_id: UUID
    original_command_id: UUID
    reservation_id: UUID
    job_id: UUID
    run_id: UUID
    input_id: str = Field(min_length=1)
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_units: int = Field(ge=0, strict=True)
    cached_input_units: int = Field(ge=0, strict=True)
    output_units: int = Field(ge=0, strict=True)
    actual_cost_usd: Decimal
    pricing_snapshot_id: str = Field(pattern=IDENTIFIER_PATTERN)
    pricing_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    started_at: datetime
    ended_at: datetime
    evidence_ref: ArtifactRef

    @field_validator("actual_cost_usd", mode="before")
    @classmethod
    def require_known_actual_cost(cls, value: object) -> Decimal:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("actual runtime cost must be finite and non-negative")
        return amount

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_utc_usage_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_ordered_usage_time(self) -> "RuntimeCostEvidenceV1":
        if (
            self.ended_at < self.started_at
            or self.cached_input_units > self.input_units
        ):
            raise ValueError("runtime cost evidence usage is invalid")
        return self


class RuntimeResumeCostSettlementV1(_FrozenContract):
    """Atomic Gateway accounting result for a terminal resume attempt."""

    schema_name: Literal["captain.runtime-resume-cost-settlement.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    settlement_id: UUID
    command_id: UUID
    reservation_id: UUID
    disposition: Literal["accounted", "overrun", "unmetered"]
    actual_cost_usd: Decimal | None = None
    accounted_cost_usd: Decimal
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)

    @field_validator("actual_cost_usd", "accounted_cost_usd", mode="before")
    @classmethod
    def require_nonnegative_settlement_cost(
        cls, value: object
    ) -> Decimal | None:
        if value is None:
            return None
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("settlement cost must be finite and non-negative")
        return amount


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
    cost_evidence: RuntimeCostEvidenceV1 | None = None
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


class RuntimeInfrastructureFailureEvidenceV1(_FrozenContract):
    """Redacted, content-addressed identity for an adapter infrastructure failure."""

    schema_name: Literal["captain.runtime-infrastructure-failure-evidence.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    failure_id: UUID
    command_id: UUID
    correlation_id: UUID
    operation: RuntimeOperation
    status: Literal["infrastructure_failed"] = "infrastructure_failed"
    reason_code: Literal["adapter_failed"] = "adapter_failed"
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)


class RuntimeResumeCostSettlementRequestV1(_FrozenContract):
    """Terminal Runtime truth submitted for atomic Gateway cost accounting."""

    schema_name: Literal["captain.runtime-resume-cost-settlement-request.v1"] = Field(
        default="captain.runtime-resume-cost-settlement-request.v1",
        alias="schema",
        serialization_alias="schema",
    )
    command: AgentRuntimeCommand
    result: AgentRuntimeResult
    authority: RuntimeResumeCostAuthorityV1


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
