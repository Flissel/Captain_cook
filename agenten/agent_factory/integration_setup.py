"""Captain-owned integration setup without credential value access."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.input_contracts import (
    CredentialAlias,
    RequestedIntegration,
)
from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1


_CREDENTIAL_ID_PATTERN = r"^\S{1,256}$"
_CREDENTIAL_TYPE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,127}$"


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class IntegrationSetupStatus(str, Enum):
    MISSING = "missing"
    SELECTION_REQUIRED = "selection_required"
    VERIFICATION_REQUIRED = "verification_required"
    VERIFICATION_FAILED = "verification_failed"
    READY = "ready"
    REVOKED = "revoked"
    EXPIRED = "expired"


class IntegrationCredentialRequirementV1(_FrozenContract):
    """Exact n8n credential metadata released by Captain's Tool Integrator."""

    schema_name: Literal["captain.integration-credential-requirement.v1"] = Field(
        default="captain.integration-credential-requirement.v1",
        alias="schema",
        serialization_alias="schema",
    )
    integration_key: str = Field(pattern=IDENTIFIER_PATTERN)
    credential_alias: str
    credential_type: str = Field(pattern=_CREDENTIAL_TYPE_PATTERN)
    required: bool
    setup_method: Literal["n8n_ui"]
    setup_label: str = Field(min_length=1, max_length=128)
    project_id: str | None = Field(default=None, pattern=_CREDENTIAL_ID_PATTERN)
    verification_workflow_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("credential_alias")
    @classmethod
    def require_safe_alias(cls, value: str) -> str:
        CredentialAlias(alias=value)
        return value


class N8nCredentialMetadataV1(_FrozenContract):
    """Sanitized metadata returned by n8n; it can never contain secret data."""

    schema_name: Literal["captain.n8n-credential-metadata.v1"] = Field(
        default="captain.n8n-credential-metadata.v1",
        alias="schema",
        serialization_alias="schema",
    )
    credential_id: str = Field(pattern=_CREDENTIAL_ID_PATTERN)
    credential_name: str = Field(min_length=1, max_length=256)
    credential_type: str = Field(pattern=_CREDENTIAL_TYPE_PATTERN)
    project_id: str | None = Field(default=None, pattern=_CREDENTIAL_ID_PATTERN)
    project_name: str | None = Field(default=None, min_length=1, max_length=256)


class CredentialVerificationReceiptV1(_FrozenContract):
    """Immutable references proving one harmless provider-backed credential probe."""

    schema_name: Literal["captain.integration-credential-verification.v1"] = Field(
        default="captain.integration-credential-verification.v1",
        alias="schema",
        serialization_alias="schema",
    )
    integration_key: str = Field(pattern=IDENTIFIER_PATTERN)
    credential_alias: str
    credential_id: str = Field(pattern=_CREDENTIAL_ID_PATTERN)
    credential_type: str = Field(pattern=_CREDENTIAL_TYPE_PATTERN)
    project_id: str | None = Field(default=None, pattern=_CREDENTIAL_ID_PATTERN)
    status: Literal["passed", "failed"]
    occurred_at: datetime
    template_ref: ArtifactRef | None = None
    verification_release: GiteaTemplateReleaseV1 | None = None
    template_content_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    workflow_ref: ArtifactRef
    workflow_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_ref: ArtifactRef
    provider_trace_id: UUID | None = None
    provider_proof_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_kind: Literal["bearer", "oauth2"] | None = None
    provider_probe_id: str | None = Field(default=None, min_length=1, max_length=128)
    oauth_consent_ref: ArtifactRef | None = None
    oauth_callback_ref: ArtifactRef | None = None
    valid_until: datetime | None = None

    @field_validator("credential_alias")
    @classmethod
    def require_safe_alias(cls, value: str) -> str:
        CredentialAlias(alias=value)
        return value

    @field_validator("occurred_at", "valid_until")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("verification receipt timestamp must be UTC")
        return value

    @model_validator(mode="after")
    def require_digest_and_expiry_binding(self) -> "CredentialVerificationReceiptV1":
        if self.workflow_content_sha256 != self.workflow_ref.sha256:
            raise ValueError("verification receipt workflow digest mismatch")
        if (self.template_ref is None) != (self.template_content_sha256 is None):
            raise ValueError("verification receipt template reference is incomplete")
        if (
            self.template_ref is not None
            and self.template_content_sha256 != self.template_ref.sha256
        ):
            raise ValueError("verification receipt template digest mismatch")
        if self.verification_release is not None:
            release_digest = (
                self.template_ref.sha256
                if self.template_ref is not None
                else self.workflow_ref.sha256
            )
            if self.verification_release.sha256 != release_digest:
                raise ValueError("verification receipt Gitea release digest mismatch")
        if (self.oauth_consent_ref is None) != (self.oauth_callback_ref is None):
            raise ValueError("OAuth verification evidence is incomplete")
        provider_evidence = (
            self.provider_trace_id,
            self.provider_proof_sha256,
            self.provider_kind,
            self.provider_probe_id,
        )
        if any(value is not None for value in provider_evidence) and not all(
            value is not None for value in provider_evidence
        ):
            raise ValueError("provider verification evidence is incomplete")
        if self.valid_until is not None and self.valid_until <= self.occurred_at:
            raise ValueError("verification receipt validity must end after verification")
        return self


class IntegrationConnectionV1(_FrozenContract):
    schema_name: Literal["captain.integration-connection.v1"] = Field(
        default="captain.integration-connection.v1",
        alias="schema",
        serialization_alias="schema",
    )
    requirement: IntegrationCredentialRequirementV1
    status: IntegrationSetupStatus
    candidate_credentials: tuple[N8nCredentialMetadataV1, ...]
    selected_credential: N8nCredentialMetadataV1 | None = None
    verification_receipt: CredentialVerificationReceiptV1 | None = None

    @model_validator(mode="after")
    def require_consistent_state(self) -> "IntegrationConnectionV1":
        selected = self.selected_credential
        receipt = self.verification_receipt
        if selected is not None and selected not in self.candidate_credentials:
            raise ValueError("selected credential must be one of the candidates")
        if self.status in {
            IntegrationSetupStatus.VERIFICATION_REQUIRED,
            IntegrationSetupStatus.VERIFICATION_FAILED,
            IntegrationSetupStatus.READY,
            IntegrationSetupStatus.EXPIRED,
        } and selected is None:
            raise ValueError("connection state requires a selected credential")
        if self.status is IntegrationSetupStatus.READY:
            if receipt is None or receipt.status != "passed":
                raise ValueError("ready connection requires a passed verification receipt")
        if self.status is IntegrationSetupStatus.EXPIRED:
            if (
                receipt is None
                or receipt.status != "passed"
                or receipt.valid_until is None
            ):
                raise ValueError("expired connection requires an expiring passed receipt")
        if receipt is not None and selected is not None:
            expected_workflow = self.requirement.verification_workflow_sha256
            released_template_sha256 = (
                receipt.template_content_sha256
                if receipt.template_content_sha256 is not None
                else receipt.workflow_content_sha256
            )
            if (
                expected_workflow is None
                or released_template_sha256 != expected_workflow
            ):
                raise ValueError(
                    "verification receipt does not match Captain verification workflow"
                )
            if (
                receipt.integration_key != self.requirement.integration_key
                or receipt.credential_alias != self.requirement.credential_alias
                or receipt.credential_id != selected.credential_id
                or receipt.credential_type != selected.credential_type
                or receipt.project_id != selected.project_id
            ):
                raise ValueError("verification receipt does not match selected credential")
        return self


class IntegrationSetupPlanV1(_FrozenContract):
    schema_name: Literal["captain.integration-setup-plan.v1"] = Field(
        default="captain.integration-setup-plan.v1",
        alias="schema",
        serialization_alias="schema",
    )
    connections: tuple[IntegrationConnectionV1, ...]


class IntegrationSetupPlanner:
    """Resolve released requirements against secret-free n8n credential metadata."""

    def plan(
        self,
        *,
        integrations: tuple[RequestedIntegration, ...],
        requirements: tuple[IntegrationCredentialRequirementV1, ...],
        credentials: tuple[N8nCredentialMetadataV1, ...],
        selected_credential_ids: Mapping[str, str] | None = None,
        verification_receipts: tuple[CredentialVerificationReceiptV1, ...] = (),
        now: datetime | None = None,
    ) -> IntegrationSetupPlanV1:
        expected = {
            (integration.integration_key, alias): integration.required
            for integration in integrations
            for alias in integration.credential_aliases
        }
        if len(expected) != sum(len(item.credential_aliases) for item in integrations):
            raise ValueError("integration credential aliases must be unique")

        actual = {
            (requirement.integration_key, requirement.credential_alias): requirement.required
            for requirement in requirements
        }
        if len(actual) != len(requirements) or actual != expected:
            raise ValueError(
                "credential requirements must exactly match released integration aliases"
            )

        credential_ids = tuple(item.credential_id for item in credentials)
        if len(credential_ids) != len(set(credential_ids)):
            raise ValueError("n8n credential metadata IDs must be unique")

        selections = dict(selected_credential_ids or {})
        known_aliases = {requirement.credential_alias for requirement in requirements}
        unknown_selections = set(selections) - known_aliases
        if unknown_selections:
            raise ValueError("credential selection names an unknown alias")

        receipt_keys = tuple(
            (item.credential_alias, item.credential_id) for item in verification_receipts
        )
        if len(receipt_keys) != len(set(receipt_keys)):
            raise ValueError("verification receipts must be unique")
        receipts_by_alias: dict[str, tuple[CredentialVerificationReceiptV1, ...]] = {}
        for alias in known_aliases:
            receipts_by_alias[alias] = tuple(
                item for item in verification_receipts if item.credential_alias == alias
            )
        unknown_receipts = {
            item.credential_alias for item in verification_receipts
        } - known_aliases
        if unknown_receipts:
            raise ValueError("verification receipt names an unknown alias")
        if now is not None:
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("integration setup evaluation time must be timezone-aware")
            now = now.astimezone(timezone.utc)

        connections = tuple(
            self._resolve_connection(
                requirement=requirement,
                credentials=credentials,
                selected_credential_id=selections.get(requirement.credential_alias),
                receipts=receipts_by_alias[requirement.credential_alias],
                now=now,
            )
            for requirement in requirements
        )
        return IntegrationSetupPlanV1(connections=connections)

    @staticmethod
    def _resolve_connection(
        *,
        requirement: IntegrationCredentialRequirementV1,
        credentials: tuple[N8nCredentialMetadataV1, ...],
        selected_credential_id: str | None,
        receipts: tuple[CredentialVerificationReceiptV1, ...],
        now: datetime | None,
    ) -> IntegrationConnectionV1:
        candidates = tuple(
            sorted(
                (
                    item
                    for item in credentials
                    if item.credential_type == requirement.credential_type
                    and (
                        requirement.project_id is None
                        or item.project_id == requirement.project_id
                    )
                ),
                key=lambda item: item.credential_id,
            )
        )
        if not candidates:
            if receipts:
                raise ValueError("verification receipt has no selected credential")
            return IntegrationConnectionV1(
                requirement=requirement,
                status=IntegrationSetupStatus.MISSING,
                candidate_credentials=(),
            )

        selected: N8nCredentialMetadataV1 | None = None
        if selected_credential_id is not None:
            selected = next(
                (
                    item
                    for item in candidates
                    if item.credential_id == selected_credential_id
                ),
                None,
            )
            if selected is None:
                raise ValueError("selected credential does not match the requirement")
        elif len(candidates) == 1:
            selected = candidates[0]

        if selected is None:
            if receipts:
                raise ValueError("verification receipt has no selected credential")
            return IntegrationConnectionV1(
                requirement=requirement,
                status=IntegrationSetupStatus.SELECTION_REQUIRED,
                candidate_credentials=candidates,
            )

        selected_receipts = tuple(
            item for item in receipts if item.credential_id == selected.credential_id
        )
        if len(selected_receipts) != len(receipts):
            raise ValueError("verification receipt does not match selected credential")
        if not selected_receipts:
            return IntegrationConnectionV1(
                requirement=requirement,
                status=IntegrationSetupStatus.VERIFICATION_REQUIRED,
                candidate_credentials=candidates,
                selected_credential=selected,
            )

        receipt = selected_receipts[0]
        if receipt.project_id != selected.project_id:
            raise ValueError("verification receipt project does not match selected credential")
        if receipt.valid_until is not None and now is None:
            raise ValueError("expiring verification receipt requires an evaluation time")
        status = (
            IntegrationSetupStatus.VERIFICATION_FAILED
            if receipt.status == "failed"
            else IntegrationSetupStatus.EXPIRED
            if receipt.valid_until is not None and now is not None and receipt.valid_until <= now
            else IntegrationSetupStatus.READY
        )
        return IntegrationConnectionV1(
            requirement=requirement,
            status=status,
            candidate_credentials=candidates,
            selected_credential=selected,
            verification_receipt=receipt,
        )


def require_required_integrations_ready(
    plan: IntegrationSetupPlanV1,
) -> IntegrationSetupPlanV1:
    blockers = tuple(
        connection
        for connection in plan.connections
        if connection.requirement.required
        and connection.status is not IntegrationSetupStatus.READY
    )
    if blockers:
        details = ", ".join(
            f"{item.requirement.credential_alias}:{item.status.value}"
            for item in blockers
        )
        raise PermissionError(f"required integrations are not ready: {details}")
    return plan
