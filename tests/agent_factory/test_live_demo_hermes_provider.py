import os

import pytest

from agenten.agent_factory import live_demo_entrypoint


def _build(monkeypatch, tmp_path, environ):
    captured = {}

    class _Factory:
        def __init__(self, *, settings, **_):
            captured["settings"] = settings

        def validate_dispatch_configuration(self, dispatch):
            return None

    monkeypatch.setattr(live_demo_entrypoint, "HermesCliFactory", _Factory)
    monkeypatch.setattr(
        live_demo_entrypoint, "FilesystemReleasedFactorySkillCatalog", lambda root: object()
    )
    monkeypatch.setattr(
        live_demo_entrypoint, "FilesystemFactorySkillReplayStore", lambda root: object()
    )

    live_demo_entrypoint.build_live_demo_hermes(
        dispatch=object(),
        hermes_executable="hermes",
        skill_root=tmp_path,
        released_skill_root=tmp_path,
        evidence_root=tmp_path,
        clock=lambda: None,
        environ=environ,
    )
    return captured["settings"]


def test_live_demo_hermes_settings_read_provider_and_model(monkeypatch, tmp_path):
    settings = _build(
        monkeypatch,
        tmp_path,
        {"CAPTAIN_HERMES_PROVIDER": "custom", "CAPTAIN_HERMES_MODEL": "nvidia/x"},
    )

    assert settings.provider == "custom"
    assert settings.model == "nvidia/x"


def test_live_demo_hermes_without_provider_keeps_defaults(monkeypatch, tmp_path):
    settings = _build(monkeypatch, tmp_path, {})

    assert settings.provider is None
    assert settings.model is None


def test_live_demo_hermes_on_token_factory_carries_the_nebius_endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = _build(
        monkeypatch,
        tmp_path,
        {
            "CAPTAIN_HERMES_PROVIDER": "custom",
            "CAPTAIN_HERMES_MODEL": "nvidia/Nemotron-3-Super-120b-a12b",
            "CAPTAIN_HERMES_BASE_URL": "https://api.tokenfactory.nebius.com/v1",
            "NEBIUS_API_KEY": "nb-test",
        },
    )

    assert dict(settings.environment_overrides) == {
        "OPENAI_BASE_URL": "https://api.tokenfactory.nebius.com/v1",
        "OPENAI_API_KEY": "nb-test",
    }
    assert "OPENAI_API_KEY" not in os.environ


def test_live_demo_hermes_on_token_factory_without_key_fails_closed(monkeypatch, tmp_path):
    with pytest.raises(live_demo_entrypoint.LiveDemoConfigurationError, match="NEBIUS_API_KEY"):
        _build(
            monkeypatch,
            tmp_path,
            {
                "CAPTAIN_HERMES_PROVIDER": "custom",
                "CAPTAIN_HERMES_MODEL": "nvidia/x",
                "CAPTAIN_HERMES_BASE_URL": "https://api.tokenfactory.nebius.com/v1",
            },
        )


def test_live_demo_hermes_without_base_url_has_no_overrides(monkeypatch, tmp_path):
    settings = _build(monkeypatch, tmp_path, {})

    assert dict(settings.environment_overrides) == {}
