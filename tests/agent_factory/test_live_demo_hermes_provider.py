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
