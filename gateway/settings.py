"""Fail-closed gateway configuration without optional settings dependencies."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping

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
    host: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)

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

    @field_validator("host")
    @classmethod
    def _host_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("host must not be blank")
        return value

    @model_validator(mode="after")
    def _role_tokens_must_be_distinct(self) -> "GatewaySettings":
        if secrets.compare_digest(
            self.captain_gateway_token.get_secret_value(),
            self.worker_gateway_token.get_secret_value(),
        ):
            raise ValueError("gateway role tokens must be distinct")
        return self

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

        try:
            return cls(
                ledger_dsn=SecretStr(source["LEDGER_DSN"]),
                captain_gateway_token=SecretStr(captain_token),
                worker_gateway_token=SecretStr(worker_token),
                approval_enabled=approval_enabled,
                host=source.get("GATEWAY_HOST", "127.0.0.1"),
                port=port,
            )
        except ValidationError:
            raise GatewayConfigurationError("invalid gateway configuration") from None
