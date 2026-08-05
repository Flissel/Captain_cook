"""Fail-closed gateway configuration without optional settings dependencies."""

from __future__ import annotations

import os
import json
import secrets
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agenten.agent_factory.gitea_template_contracts import (
    GiteaTemplateReleaseV1,
    validate_safe_https_url,
)


class GatewayConfigurationError(ValueError):
    """Raised when production gateway configuration is absent or ambiguous."""


class GatewaySettings(BaseModel):
    """Strict immutable settings loaded explicitly from the process environment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ledger_dsn: SecretStr
    captain_gateway_token: SecretStr
    worker_gateway_token: SecretStr
    approval_enabled: bool = False
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)
    claim_ttl_seconds: int = Field(default=5_400, ge=1, le=86_400)
    captain_n8n_ui_url: str = "http://localhost:5679"
    portal_supabase_issuer: str | None = None
    portal_supabase_audience: str | None = None
    portal_supabase_jwks_url: str | None = None
    portal_organization_claim: str = "organization_id"
    portal_provider_control_token: SecretStr | None = None
    portal_evidence_token: SecretStr | None = None
    portal_restart_control_token: SecretStr | None = None
    portal_n8n_adapters_enabled: bool = False
    portal_n8n_api_key: SecretStr | None = None
    portal_n8n_mcp_token: SecretStr | None = None
    portal_gitea_origin: str | None = None
    portal_verification_releases: tuple[GiteaTemplateReleaseV1, ...] = ()

    @field_validator(
        "ledger_dsn",
        "captain_gateway_token",
        "worker_gateway_token",
    )
    @classmethod
    def _secret_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("secret settings must not be blank")
        return value

    @field_validator("captain_n8n_ui_url")
    @classmethod
    def _n8n_url_must_be_safe(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Captain n8n URL must be a safe HTTP URL")
        return value.rstrip("/")

    @field_validator(
        "portal_supabase_issuer",
        "portal_supabase_audience",
    )
    @classmethod
    def _portal_setting_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("portal settings must not be blank")
        return value

    @field_validator("portal_supabase_jwks_url")
    @classmethod
    def _portal_jwks_url_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("portal JWKS URL must be a safe HTTPS URL")
        return value.rstrip("/")

    @field_validator("portal_organization_claim")
    @classmethod
    def _portal_organization_claim_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("portal organization claim must not be blank")
        return value

    @model_validator(mode="after")
    def _role_tokens_must_be_distinct(self) -> "GatewaySettings":
        if secrets.compare_digest(
            self.captain_gateway_token.get_secret_value(),
            self.worker_gateway_token.get_secret_value(),
        ):
            raise ValueError("gateway role tokens must be distinct")
        portal_settings = (
            self.portal_supabase_issuer,
            self.portal_supabase_audience,
            self.portal_supabase_jwks_url,
        )
        if any(value is not None for value in portal_settings) and not all(
            value is not None for value in portal_settings
        ):
            raise ValueError("portal identity settings must be configured together")
        control_tokens = (
            self.portal_provider_control_token,
            self.portal_evidence_token,
            self.portal_restart_control_token,
        )
        if any(value is not None for value in control_tokens) and not all(
            value is not None for value in control_tokens
        ):
            raise ValueError("portal control tokens must be configured together")
        if all(value is not None for value in control_tokens):
            values = (
                self.captain_gateway_token.get_secret_value(),
                self.worker_gateway_token.get_secret_value(),
                *(value.get_secret_value() for value in control_tokens if value is not None),
            )
            if any(not value.strip() for value in values[2:]):
                raise ValueError("portal control tokens must not be blank")
            if len(values) != len(set(values)):
                raise ValueError("portal control tokens must be distinct")
        if self.portal_n8n_adapters_enabled:
            adapter_values = (
                self.portal_n8n_api_key,
                self.portal_n8n_mcp_token,
                self.portal_gitea_origin,
                self.portal_verification_releases,
            )
            if any(value is None or value == () for value in adapter_values):
                raise ValueError("portal n8n adapter configuration is incomplete")
            assert self.portal_n8n_api_key is not None
            assert self.portal_n8n_mcp_token is not None
            assert self.portal_gitea_origin is not None
            if (
                not self.portal_n8n_api_key.get_secret_value().strip()
                or not self.portal_n8n_mcp_token.get_secret_value().strip()
            ):
                raise ValueError("portal n8n adapter secrets must not be blank")
            validate_safe_https_url(
                self.portal_gitea_origin,
                label="portal Gitea origin",
            )
            if urlsplit(self.portal_gitea_origin).path not in {"", "/"}:
                raise ValueError("portal Gitea origin must not contain a path")
            digests = tuple(
                release.sha256 for release in self.portal_verification_releases
            )
            if len(digests) != len(set(digests)):
                raise ValueError("portal verification release digests must be unique")
            origin = self.portal_gitea_origin.rstrip("/") + "/"
            if any(
                not release.contents_url.startswith(origin)
                for release in self.portal_verification_releases
            ):
                raise ValueError("portal verification releases must use the Gitea origin")
        return self

    @property
    def portal_identity_configured(self) -> bool:
        return all(
            value is not None
            for value in (
                self.portal_supabase_issuer,
                self.portal_supabase_audience,
                self.portal_supabase_jwks_url,
            )
        )

    @property
    def portal_control_configured(self) -> bool:
        return all(
            value is not None
            for value in (
                self.portal_provider_control_token,
                self.portal_evidence_token,
                self.portal_restart_control_token,
            )
        )

    @property
    def portal_n8n_adapters_configured(self) -> bool:
        return self.portal_n8n_adapters_enabled

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "GatewaySettings":
        source = os.environ if environ is None else environ
        required_names = (
            "LEDGER_DSN",
            "CAPTAIN_GATEWAY_TOKEN",
            "WORKER_GATEWAY_TOKEN",
        )
        missing = [
            name
            for name in required_names
            if not isinstance(source.get(name), str) or not source[name].strip()
        ]
        if missing:
            raise GatewayConfigurationError(
                f"missing required gateway settings: {', '.join(missing)}"
            )

        captain_token = source["CAPTAIN_GATEWAY_TOKEN"]
        worker_token = source["WORKER_GATEWAY_TOKEN"]
        if secrets.compare_digest(captain_token, worker_token):
            raise GatewayConfigurationError("gateway role tokens must be distinct")

        approval_raw = source.get("GATEWAY_APPROVAL_ENABLED", "false")
        if approval_raw.lower() not in {"true", "false"}:
            raise GatewayConfigurationError("invalid gateway configuration")
        approval_enabled = approval_raw.lower() == "true"

        portal_n8n_raw = source.get(
            "CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED",
            "false",
        )
        if portal_n8n_raw.lower() not in {"true", "false"}:
            raise GatewayConfigurationError("invalid gateway configuration")
        portal_n8n_enabled = portal_n8n_raw.lower() == "true"
        releases: tuple[GiteaTemplateReleaseV1, ...] = ()
        if portal_n8n_enabled:
            releases_raw = source.get(
                "CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON",
                "",
            )
            try:
                decoded_releases = json.loads(releases_raw)
                if not isinstance(decoded_releases, list):
                    raise ValueError
                releases = tuple(
                    GiteaTemplateReleaseV1.model_validate(item)
                    for item in decoded_releases
                )
            except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
                raise GatewayConfigurationError("invalid gateway configuration") from None

        port_raw = source.get("GATEWAY_PORT", "8090")
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise GatewayConfigurationError("invalid gateway configuration") from None

        claim_ttl_raw = source.get("GATEWAY_CLAIM_TTL_SECONDS", "5400")
        try:
            claim_ttl_seconds = int(claim_ttl_raw)
        except (TypeError, ValueError):
            raise GatewayConfigurationError("invalid gateway configuration") from None

        try:
            return cls(
                ledger_dsn=SecretStr(source["LEDGER_DSN"]),
                captain_gateway_token=SecretStr(captain_token),
                worker_gateway_token=SecretStr(worker_token),
                approval_enabled=approval_enabled,
                port=port,
                claim_ttl_seconds=claim_ttl_seconds,
                captain_n8n_ui_url=source.get(
                    "CAPTAIN_N8N_URL",
                    "http://localhost:5679",
                ),
                portal_supabase_issuer=source.get("PORTAL_SUPABASE_ISSUER"),
                portal_supabase_audience=source.get("PORTAL_SUPABASE_AUDIENCE"),
                portal_supabase_jwks_url=source.get("PORTAL_SUPABASE_JWKS_URL"),
                portal_organization_claim=source.get(
                    "PORTAL_ORGANIZATION_CLAIM",
                    "organization_id",
                ),
                portal_provider_control_token=(
                    SecretStr(source["PORTAL_PROVIDER_CONTROL_TOKEN"])
                    if source.get("PORTAL_PROVIDER_CONTROL_TOKEN") is not None
                    else None
                ),
                portal_evidence_token=(
                    SecretStr(source["PORTAL_EVIDENCE_TOKEN"])
                    if source.get("PORTAL_EVIDENCE_TOKEN") is not None
                    else None
                ),
                portal_restart_control_token=(
                    SecretStr(source["PORTAL_RESTART_CONTROL_TOKEN"])
                    if source.get("PORTAL_RESTART_CONTROL_TOKEN") is not None
                    else None
                ),
                portal_n8n_adapters_enabled=portal_n8n_enabled,
                portal_n8n_api_key=(
                    SecretStr(source["CAPTAIN_N8N_API_KEY"])
                    if portal_n8n_enabled and source.get("CAPTAIN_N8N_API_KEY") is not None
                    else None
                ),
                portal_n8n_mcp_token=(
                    SecretStr(source["CAPTAIN_N8N_MCP_TOKEN"])
                    if portal_n8n_enabled and source.get("CAPTAIN_N8N_MCP_TOKEN") is not None
                    else None
                ),
                portal_gitea_origin=(
                    source.get("CAPTAIN_PORTAL_GITEA_ORIGIN")
                    if portal_n8n_enabled
                    else None
                ),
                portal_verification_releases=releases,
            )
        except ValidationError:
            raise GatewayConfigurationError("invalid gateway configuration") from None
