import os

import pytest

from agenten.agent_factory.hermes_endpoint import (
    HERMES_LOCAL_ENDPOINT_PLACEHOLDER_KEY,
    hermes_endpoint_overrides,
)
from config.llm_provider import NEBIUS_BASE_URL


def test_no_base_url_means_no_overrides() -> None:
    assert dict(hermes_endpoint_overrides({"NEBIUS_API_KEY": "nb-test"})) == {}
    assert dict(hermes_endpoint_overrides({"CAPTAIN_HERMES_BASE_URL": "  "})) == {}


def test_token_factory_uses_the_nebius_key() -> None:
    overrides = hermes_endpoint_overrides(
        {
            "CAPTAIN_HERMES_BASE_URL": NEBIUS_BASE_URL + "/",
            "NEBIUS_API_KEY": " nb-test ",
            "OPENAI_API_KEY": "sk-must-not-be-used",
        }
    )

    assert dict(overrides) == {
        "OPENAI_BASE_URL": NEBIUS_BASE_URL + "/",
        "OPENAI_API_KEY": "nb-test",
    }


def test_token_factory_without_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        hermes_endpoint_overrides({"CAPTAIN_HERMES_BASE_URL": NEBIUS_BASE_URL})


def test_local_endpoint_gets_the_placeholder_not_an_ambient_key() -> None:
    overrides = hermes_endpoint_overrides(
        {
            "CAPTAIN_HERMES_BASE_URL": "http://127.0.0.1:8114/v1",
            "NEBIUS_API_KEY": "nb-must-not-leak",
            "OPENAI_API_KEY": "sk-must-not-leak",
        }
    )

    assert dict(overrides) == {
        "OPENAI_BASE_URL": "http://127.0.0.1:8114/v1",
        "OPENAI_API_KEY": HERMES_LOCAL_ENDPOINT_PLACEHOLDER_KEY,
    }


def test_custom_endpoint_accepts_an_explicit_hermes_key() -> None:
    overrides = hermes_endpoint_overrides(
        {
            "CAPTAIN_HERMES_BASE_URL": "https://llm.example.test/v1",
            "CAPTAIN_HERMES_API_KEY": "explicit-key",
        }
    )

    assert overrides["OPENAI_API_KEY"] == "explicit-key"


def test_overrides_never_touch_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    hermes_endpoint_overrides(
        {"CAPTAIN_HERMES_BASE_URL": NEBIUS_BASE_URL, "NEBIUS_API_KEY": "nb-test"}
    )

    assert "OPENAI_BASE_URL" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
