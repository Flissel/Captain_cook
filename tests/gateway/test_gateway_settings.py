from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import SecretStr, ValidationError

from gateway.settings import GatewayConfigurationError, GatewaySettings


def valid_environment(**overrides: str) -> Mapping[str, str]:
    values = {
        "LEDGER_DSN": "mariadb://captain:database-secret@127.0.0.1/captain",
        "CAPTAIN_GATEWAY_TOKEN": "captain-test-token",
        "WORKER_GATEWAY_TOKEN": "worker-test-token",
        "GATEWAY_APPROVAL_ENABLED": "true",
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_PORT": "18090",
        "UNRELATED_PROCESS_VALUE": "ignored",
    }
    values.update(overrides)
    return values


def test_settings_load_explicit_environment_without_exposing_secrets() -> None:
    settings = GatewaySettings.from_env(valid_environment())

    assert isinstance(settings.ledger_dsn, SecretStr)
    assert settings.ledger_dsn.get_secret_value().startswith("mariadb://")
    assert settings.captain_gateway_token.get_secret_value() == "captain-test-token"
    assert settings.worker_gateway_token.get_secret_value() == "worker-test-token"
    assert settings.approval_enabled is True
    assert settings.host == "127.0.0.1"
    assert settings.port == 18090
    rendered = repr(settings)
    assert "database-secret" not in rendered
    assert "captain-test-token" not in rendered
    assert "worker-test-token" not in rendered


@pytest.mark.parametrize(
    "missing_name",
    ("LEDGER_DSN", "CAPTAIN_GATEWAY_TOKEN", "WORKER_GATEWAY_TOKEN"),
)
def test_settings_fail_closed_when_required_environment_is_missing(
    missing_name: str,
) -> None:
    environment = dict(valid_environment())
    del environment[missing_name]

    with pytest.raises(GatewayConfigurationError, match=missing_name):
        GatewaySettings.from_env(environment)


def test_settings_reject_ambiguous_role_tokens_without_echoing_them() -> None:
    environment = valid_environment(WORKER_GATEWAY_TOKEN="captain-test-token")

    with pytest.raises(GatewayConfigurationError) as error:
        GatewaySettings.from_env(environment)

    assert "distinct" in str(error.value)
    assert "captain-test-token" not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("GATEWAY_APPROVAL_ENABLED", "yes"),
        ("GATEWAY_PORT", "not-a-port"),
        ("GATEWAY_PORT", "70000"),
        ("GATEWAY_HOST", "   "),
    ),
)
def test_settings_fail_closed_on_invalid_explicit_values(name: str, value: str) -> None:
    with pytest.raises(GatewayConfigurationError):
        GatewaySettings.from_env(valid_environment(**{name: value}))


def test_settings_model_is_frozen_and_strict() -> None:
    settings = GatewaySettings.from_env(valid_environment())

    with pytest.raises(ValidationError):
        settings.port = 8090

    with pytest.raises(ValidationError):
        GatewaySettings(
            ledger_dsn=SecretStr("mariadb://captain:test@127.0.0.1/captain"),
            captain_gateway_token=SecretStr("captain-test-token"),
            worker_gateway_token=SecretStr("worker-test-token"),
            approval_enabled="false",  # type: ignore[arg-type]
            host="127.0.0.1",
            port="8090",  # type: ignore[arg-type]
        )
