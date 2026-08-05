"""Fail-closed gateway configuration without optional settings dependencies."""

from __future__ import annotations

import os
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
        "portal_supabase_jwks_url",
    )
    @classmethod
    def _portal_setting_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("portal settings must not be blank")
        return value

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
            )
        except ValidationError:
            raise GatewayConfigurationError("invalid gateway configuration") from None
