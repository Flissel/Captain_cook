"""Fail-closed helpers for the explicitly opted-in portal live gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from typing import Literal, Mapping, Self, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


OPT_IN_NAME = "CAPTAIN_PORTAL_LIVE_E2E"
LOOPBACK_NAME = "CAPTAIN_PORTAL_LIVE_ALLOW_LOOPBACK"
MAX_RESPONSE_BYTES = 128 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
EVIDENCE_REFERENCE_PATTERN = (
    r"^(?:artifact|evidence|execution|decision|minibook|trace|gitea)://\S{1,500}$"
)

REQUIRED_ENVIRONMENT = (
    "CAPTAIN_PORTAL_LIVE_BASE_URL",
    "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL",
    "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN",
    "CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN",
    "CAPTAIN_PORTAL_LIVE_ORG_A_ID",
    "CAPTAIN_PORTAL_LIVE_ORG_B_ID",
    "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_TOKEN",
    "CAPTAIN_PORTAL_LIVE_JOB_ID",
    "CAPTAIN_PORTAL_LIVE_CORRELATION_ID",
    "CAPTAIN_PORTAL_LIVE_BEARER_ALIAS",
    "CAPTAIN_PORTAL_LIVE_BEARER_CREDENTIAL_ID",
    "CAPTAIN_PORTAL_LIVE_OAUTH_ALIAS",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CREDENTIAL_ID",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CLIENT_ID",
    "CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL",
    "CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_TOKEN",
    "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL",
    "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_TOKEN",
    "CAPTAIN_PORTAL_LIVE_EVIDENCE_URL",
    "CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_EVIDENCE_TOKEN",
    "CAPTAIN_PORTAL_LIVE_SECRET_CANARY",
)

_URL_ENVIRONMENT = (
    "CAPTAIN_PORTAL_LIVE_BASE_URL",
    "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL",
    "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL",
    "CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL",
    "CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL",
    "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL",
    "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL",
    "CAPTAIN_PORTAL_LIVE_EVIDENCE_URL",
    "CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL",
)


class PortalLiveConfigurationError(ValueError):
    """Raised before networking when the disposable live group is unsafe."""


class PortalLiveResponseError(RuntimeError):
    """Fixed-message response error which never includes a provider body."""


@dataclass(frozen=True)
class PortalLiveConfig:
    base_url: str
    captain_control_base_url: str
    captain_control_health_url: str
    org_a_access_token: str
    org_b_access_token: str
    org_a_id: str
    org_b_id: str
    captain_control_token: str
    job_id: UUID
    correlation_id: UUID
    bearer_alias: str
    bearer_credential_id: str
    oauth_alias: str
    oauth_credential_id: str
    oauth_client_id: str
    oauth_auth_url: str
    oauth_callback_url: str
    n8n_health_url: str
    gitea_health_url: str
    supabase_health_url: str
    minibook_health_url: str
    provider_audit_url: str
    provider_control_url: str
    provider_control_health_url: str
    provider_control_token: str
    restart_control_url: str
    restart_control_health_url: str
    restart_control_token: str
    evidence_url: str
    evidence_health_url: str
    evidence_token: str
    secret_canary: str

    @classmethod
    def from_environment(cls) -> "PortalLiveConfig":
        if os.environ.get(OPT_IN_NAME) != "1":
            raise PortalLiveConfigurationError(
                f"set {OPT_IN_NAME}=1 to authorize the disposable portal live gate"
            )
        missing = tuple(name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name))
        if missing:
            raise PortalLiveConfigurationError(
                "missing complete portal live group: " + ", ".join(missing)
            )
        values = {name: os.environ[name] for name in REQUIRED_ENVIRONMENT}
        allow_loopback = os.environ.get(LOOPBACK_NAME) == "1"
        for name in _URL_ENVIRONMENT:
            _validate_safe_url(values[name], name=name, allow_loopback=allow_loopback)
        portal_origin = _origin(values["CAPTAIN_PORTAL_LIVE_BASE_URL"])
        protected_groups = (
            (
                _origin(values["CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL"]),
                ("CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL",),
            ),
            (
                _origin(values["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL"]),
                (
                    "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL",
                    "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL",
                ),
            ),
            (
                _origin(values["CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL"]),
                ("CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL",),
            ),
            (
                _origin(values["CAPTAIN_PORTAL_LIVE_EVIDENCE_URL"]),
                ("CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL",),
            ),
        )
        for capability_origin, member_names in protected_groups:
            if any(_origin(values[name]) != capability_origin for name in member_names):
                raise PortalLiveConfigurationError(
                    "protected URL must share its capability origin"
                )
            if capability_origin == portal_origin:
                raise PortalLiveConfigurationError(
                    "protected origin must be distinct from the browser portal origin"
                )
        if portal_origin == _origin(
            values["CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL"]
        ):
            raise PortalLiveConfigurationError(
                "portal and Captain control origins must be distinct"
            )
        if values["CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN"] == values[
            "CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN"
        ]:
            raise PortalLiveConfigurationError("portal live user tokens must be distinct")
        if values["CAPTAIN_PORTAL_LIVE_ORG_A_ID"] == values[
            "CAPTAIN_PORTAL_LIVE_ORG_B_ID"
        ]:
            raise PortalLiveConfigurationError("portal live organizations must be distinct")
        if len(values["CAPTAIN_PORTAL_LIVE_SECRET_CANARY"].encode("utf-8")) < 12:
            raise PortalLiveConfigurationError("portal live secret canary is too short")
        try:
            job_id = UUID(values["CAPTAIN_PORTAL_LIVE_JOB_ID"])
            correlation_id = UUID(values["CAPTAIN_PORTAL_LIVE_CORRELATION_ID"])
        except ValueError as error:
            raise PortalLiveConfigurationError(
                "portal live job and correlation identifiers must be UUIDs"
            ) from error
        return cls(
            base_url=values["CAPTAIN_PORTAL_LIVE_BASE_URL"].rstrip("/"),
            captain_control_base_url=values[
                "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL"
            ].rstrip("/"),
            captain_control_health_url=values[
                "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL"
            ],
            org_a_access_token=values["CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN"],
            org_b_access_token=values["CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN"],
            org_a_id=values["CAPTAIN_PORTAL_LIVE_ORG_A_ID"],
            org_b_id=values["CAPTAIN_PORTAL_LIVE_ORG_B_ID"],
            captain_control_token=values["CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_TOKEN"],
            job_id=job_id,
            correlation_id=correlation_id,
            bearer_alias=values["CAPTAIN_PORTAL_LIVE_BEARER_ALIAS"],
            bearer_credential_id=values["CAPTAIN_PORTAL_LIVE_BEARER_CREDENTIAL_ID"],
            oauth_alias=values["CAPTAIN_PORTAL_LIVE_OAUTH_ALIAS"],
            oauth_credential_id=values["CAPTAIN_PORTAL_LIVE_OAUTH_CREDENTIAL_ID"],
            oauth_client_id=values["CAPTAIN_PORTAL_LIVE_OAUTH_CLIENT_ID"],
            oauth_auth_url=values["CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL"],
            oauth_callback_url=values["CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL"],
            n8n_health_url=values["CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL"],
            gitea_health_url=values["CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL"],
            supabase_health_url=values["CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL"],
            minibook_health_url=values["CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL"],
            provider_audit_url=values["CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL"],
            provider_control_url=values["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL"],
            provider_control_health_url=values[
                "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL"
            ],
            provider_control_token=values["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_TOKEN"],
            restart_control_url=values["CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL"],
            restart_control_health_url=values[
                "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL"
            ],
            restart_control_token=values["CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_TOKEN"],
            evidence_url=values["CAPTAIN_PORTAL_LIVE_EVIDENCE_URL"],
            evidence_health_url=values["CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL"],
            evidence_token=values["CAPTAIN_PORTAL_LIVE_EVIDENCE_TOKEN"],
            secret_canary=values["CAPTAIN_PORTAL_LIVE_SECRET_CANARY"],
        )


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class ResponseStatus(_WireModel):
    status_code: int = Field(ge=100, le=599)


class PortalTenantBindingEvidence(_WireModel):
    job_id: UUID
    organization_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EphemeralPortalTicket(_WireModel):
    ticket_id: UUID
    ticket: str = Field(min_length=1, max_length=256, repr=False)
    job_id: UUID
    credential_alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    expires_at: datetime


class CredentialMetadataEvidence(_WireModel):
    schema_name: Literal["captain.n8n-credential-metadata.v1"] = Field(alias="schema")
    credential_id: str = Field(pattern=r"^\S{1,256}$")
    credential_name: str = Field(min_length=1, max_length=256)
    credential_type: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
    project_id: str | None = Field(default=None, pattern=r"^\S{1,256}$")
    project_name: str | None = Field(default=None, min_length=1, max_length=256)


class PortalActionWire(_WireModel):
    integration_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    credential_alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    credential_type: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
    setup_label: str = Field(min_length=1, max_length=128)
    required: bool
    status: Literal[
        "missing",
        "selection_required",
        "verification_required",
        "verification_failed",
        "ready",
        "revoked",
        "expired",
    ]
    candidate_credentials: tuple[CredentialMetadataEvidence, ...]
    selected_credential: CredentialMetadataEvidence | None = None


class PortalSurfaceWire(_WireModel):
    job_id: UUID
    revision: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overall_status: Literal[
        "missing",
        "selection_required",
        "verification_required",
        "verification_failed",
        "ready",
        "revoked",
        "expired",
    ]
    n8n_credentials_url: str
    actions: tuple[PortalActionWire, ...]

    @field_validator("n8n_credentials_url")
    @classmethod
    def require_safe_n8n_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unsafe n8n credential URL")
        return value


class PortalSurfaceEvidence(_WireModel):
    job_id: UUID
    revision: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overall_status: str
    actions: tuple[PortalActionWire, ...]


class ProviderAuditEvidence(_WireModel):
    correlation_id: UUID
    invocation_count: int = Field(ge=0)
    observed_at: datetime


class ProviderTraceEvidence(_WireModel):
    trace_id: UUID
    correlation_id: UUID
    credential_alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    credential_id: str = Field(pattern=r"^\S{1,256}$")
    integration_kind: Literal["bearer", "oauth"]
    status: Literal["passed"]
    occurred_at: datetime
    execution_ref: str = Field(pattern=EVIDENCE_REFERENCE_PATTERN)
    consent_ref: str | None = Field(default=None, pattern=EVIDENCE_REFERENCE_PATTERN)
    callback_ref: str | None = Field(default=None, pattern=EVIDENCE_REFERENCE_PATTERN)


class RestartEvidence(_WireModel):
    restart_id: UUID
    correlation_id: UUID
    status: Literal["resumed"]
    occurred_at: datetime


class ReleaseEvidence(_WireModel):
    correlation_id: UUID
    revision: int = Field(ge=1)
    status: Literal["accepted"]
    provider_traces: tuple[ProviderTraceEvidence, ...] = Field(
        min_length=3,
        max_length=3,
    )
    gitea_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gateway_decision_ref: str = Field(pattern=EVIDENCE_REFERENCE_PATTERN)
    gateway_execution_ref: str = Field(pattern=EVIDENCE_REFERENCE_PATTERN)
    minibook_projection_ref: str = Field(pattern=EVIDENCE_REFERENCE_PATTERN)
    minibook_rebuild_ref: str = Field(pattern=EVIDENCE_REFERENCE_PATTERN)

    @model_validator(mode="after")
    def require_unique_trace_ids(self) -> Self:
        trace_ids = tuple(trace.trace_id for trace in self.provider_traces)
        if len(set(trace_ids)) != 3:
            raise ValueError("release evidence trace IDs must be unique")
        return self


WireModel = TypeVar("WireModel", bound=_WireModel)


class PortalLiveClient:
    """Typed bounded clients for mutually isolated live control surfaces."""

    def __init__(
        self,
        config: PortalLiveConfig,
        *,
        portal_transport: httpx.BaseTransport | None = None,
        captain_transport: httpx.BaseTransport | None = None,
        auxiliary_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        common = {"follow_redirects": False, "timeout": REQUEST_TIMEOUT_SECONDS}
        self._portal = httpx.Client(
            base_url=config.base_url,
            transport=portal_transport,
            **common,
        )
        self._captain = httpx.Client(
            base_url=config.captain_control_base_url,
            transport=captain_transport,
            **common,
        )
        self._auxiliary = httpx.Client(transport=auxiliary_transport, **common)

    def close(self) -> None:
        self._portal.close()
        self._captain.close()
        self._auxiliary.close()

    def health(self, url: str, *, token: str | None = None) -> ResponseStatus:
        return self._status(self._auxiliary, "GET", url, token=token)

    def preflight_statuses(self) -> tuple[ResponseStatus, ...]:
        """Read-only reachability/auth checks for every seam before mutation."""

        public_urls = (
            self.config.n8n_health_url,
            self.config.gitea_health_url,
            self.config.supabase_health_url,
            self.config.minibook_health_url,
        )
        protected = (
            (
                self.config.captain_control_health_url,
                self.config.captain_control_token,
            ),
            (
                self.config.provider_control_health_url,
                self.config.provider_control_token,
            ),
            (
                self.config.restart_control_health_url,
                self.config.restart_control_token,
            ),
            (self.config.evidence_health_url, self.config.evidence_token),
        )
        return tuple(self.health(url) for url in public_urls) + tuple(
            self.health(url, token=token) for url, token in protected
        )

    def portal_status(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> ResponseStatus:
        if not path.startswith("/v1/portal/"):
            raise PortalLiveConfigurationError("portal client path is outside portal scope")
        return self._status(
            self._portal,
            method,
            path,
            token=access_token,
            payload=payload,
        )

    def provision_tenant(self, organization_id: str) -> PortalTenantBindingEvidence:
        path = (
            f"/v1/factory/integration-setups/{self.config.job_id}/"
            "portal-tenant-binding"
        )
        return self._typed(
            self._captain,
            "POST",
            path,
            PortalTenantBindingEvidence,
            expected=(200, 201),
            token=self.config.captain_control_token,
            payload={
                "job_id": str(self.config.job_id),
                "organization_id": organization_id,
            },
        )

    def get_surface(self, *, access_token: str) -> PortalSurfaceEvidence:
        wire = self._typed(
            self._portal,
            "GET",
            self._portal_path(),
            PortalSurfaceWire,
            expected=(200,),
            token=access_token,
        )
        return PortalSurfaceEvidence(
            job_id=wire.job_id,
            revision=wire.revision,
            content_sha256=wire.content_sha256,
            overall_status=wire.overall_status,
            actions=wire.actions,
        )

    def issue_ticket(self, *, alias: str, action: str) -> EphemeralPortalTicket:
        return self._typed(
            self._portal,
            "POST",
            self._portal_path("/tickets"),
            EphemeralPortalTicket,
            expected=(201,),
            token=self.config.org_a_access_token,
            payload={"credential_alias": alias, "action": action},
        )

    def discover(
        self,
        ticket: EphemeralPortalTicket,
        *,
        access_token: str,
    ) -> PortalSurfaceEvidence:
        return self._surface_mutation(
            "/discover",
            ticket,
            access_token=access_token,
        )

    def select(self, ticket: EphemeralPortalTicket, credential_id: str) -> PortalSurfaceEvidence:
        return self._surface_mutation(
            "/select",
            ticket,
            access_token=self.config.org_a_access_token,
            extra={"credential_id": credential_id},
        )

    def action(self, ticket: EphemeralPortalTicket, action: str) -> PortalSurfaceEvidence:
        return self._surface_mutation(
            "/actions",
            ticket,
            access_token=self.config.org_a_access_token,
            extra={"action": action},
        )

    def provider_audit(self) -> ProviderAuditEvidence:
        return self._typed(
            self._auxiliary,
            "POST",
            self.config.provider_audit_url,
            ProviderAuditEvidence,
            expected=(200,),
            token=self.config.provider_control_token,
            payload={"correlation_id": str(self.config.correlation_id)},
        )

    def provider_probe(self, kind: Literal["bearer", "oauth"]) -> ProviderTraceEvidence:
        alias = self.config.bearer_alias if kind == "bearer" else self.config.oauth_alias
        credential_id = (
            self.config.bearer_credential_id
            if kind == "bearer"
            else self.config.oauth_credential_id
        )
        payload: dict[str, object] = {
            "correlation_id": str(self.config.correlation_id),
            "job_id": str(self.config.job_id),
            "integration_kind": kind,
            "credential_alias": alias,
            "credential_id": credential_id,
        }
        if kind == "oauth":
            payload.update(
                {
                    "oauth_client_id": self.config.oauth_client_id,
                    "oauth_auth_url": self.config.oauth_auth_url,
                    "oauth_callback_url": self.config.oauth_callback_url,
                }
            )
        return self._typed(
            self._auxiliary,
            "POST",
            self.config.provider_control_url,
            ProviderTraceEvidence,
            expected=(200, 201),
            token=self.config.provider_control_token,
            payload=payload,
        )

    def restart_and_resume(self) -> RestartEvidence:
        return self._typed(
            self._auxiliary,
            "POST",
            self.config.restart_control_url,
            RestartEvidence,
            expected=(200, 202),
            token=self.config.restart_control_token,
            payload={
                "correlation_id": str(self.config.correlation_id),
                "job_id": str(self.config.job_id),
                "services": ["gateway", "portal"],
            },
        )

    def release_evidence(self) -> ReleaseEvidence:
        return self._typed(
            self._auxiliary,
            "POST",
            self.config.evidence_url,
            ReleaseEvidence,
            expected=(200,),
            token=self.config.evidence_token,
            payload={"correlation_id": str(self.config.correlation_id)},
        )

    def _surface_mutation(
        self,
        suffix: str,
        ticket: EphemeralPortalTicket,
        *,
        access_token: str,
        extra: Mapping[str, object] | None = None,
    ) -> PortalSurfaceEvidence:
        payload: dict[str, object] = {
            "ticket_id": str(ticket.ticket_id),
            "ticket": ticket.ticket,
            "credential_alias": ticket.credential_alias,
        }
        payload.update(extra or {})
        wire = self._typed(
            self._portal,
            "POST",
            self._portal_path(suffix),
            PortalSurfaceWire,
            expected=(200,),
            token=access_token,
            payload=payload,
        )
        return PortalSurfaceEvidence(
            job_id=wire.job_id,
            revision=wire.revision,
            content_sha256=wire.content_sha256,
            overall_status=wire.overall_status,
            actions=wire.actions,
        )

    def _portal_path(self, suffix: str = "") -> str:
        return f"/v1/portal/integration-setups/{self.config.job_id}{suffix}"

    def _status(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> ResponseStatus:
        status_code, _ = self._bounded_request(
            client,
            method,
            url,
            token=token,
            payload=payload,
        )
        return ResponseStatus(status_code=status_code)

    def _typed(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        model: type[WireModel],
        *,
        expected: tuple[int, ...],
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> WireModel:
        status_code, body = self._bounded_request(
            client,
            method,
            url,
            token=token,
            payload=payload,
        )
        if status_code not in expected:
            raise PortalLiveResponseError("portal live response had an unexpected status")
        try:
            decoded = json.loads(body.decode("utf-8"))
            return model.model_validate(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            raise PortalLiveResponseError("portal live response violated its schema") from None

    def _bounded_request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        token: str | None,
        payload: Mapping[str, object] | None,
    ) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with client.stream(method, url, headers=headers, json=payload) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PortalLiveResponseError(
                            "portal live response exceeded the size limit"
                        )
        except httpx.HTTPError:
            raise PortalLiveResponseError("portal live request failed") from None
        if self.config.secret_canary.encode("utf-8") in body:
            raise PortalLiveResponseError("portal live response exposed the secret canary")
        return response.status_code, bytes(body)


def _origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    port = parsed.port
    if port is None:
        port = {"https": 443, "http": 80}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname, port


def _validate_safe_url(value: str, *, name: str, allow_loopback: bool) -> None:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise PortalLiveConfigurationError(f"{name} must be a safe HTTPS URL") from None
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    allowed_scheme = parsed.scheme == "https" or (
        allow_loopback and loopback and parsed.scheme == "http"
    )
    if (
        not allowed_scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PortalLiveConfigurationError(f"{name} must be a safe HTTPS URL")
