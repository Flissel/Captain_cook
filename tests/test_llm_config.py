import importlib
import os

import pytest

import config.llm_config as llm_config
from config.llm_provider import NEMOTRON_DEFAULT_MODEL, ProviderConfigurationError

_PROVIDER_VARS = (
    "CAPTAIN_LLM_PROVIDER",
    "CAPTAIN_LLM_BASE_URL",
    "CAPTAIN_MODEL",
    "NEBIUS_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _fresh_profile_cache():
    llm_config.reset_profile_cache()
    yield
    llm_config.reset_profile_cache()


def _clean(monkeypatch, tmp_path):
    # The module also reads a .env from the working directory, so defaults are
    # only pinned once both sources are known to be empty.
    monkeypatch.chdir(tmp_path)
    for name in _PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)


def test_default_model_is_nemotron_on_nebius(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)

    assert llm_config.get_profile().name == "nebius"
    assert llm_config.PROFILE.name == "nebius"
    assert llm_config.MODEL == NEMOTRON_DEFAULT_MODEL


def test_local_env_file_supplies_openai_opt_in(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "CAPTAIN_LLM_PROVIDER=openai\nOPENAI_API_KEY=test-key\nCAPTAIN_MODEL=test-model\n",
        encoding="utf-8",
    )

    assert llm_config.API_KEY == "test-key"
    assert llm_config.MODEL == "test-model"
    assert "OPENAI_API_KEY" not in os.environ
    assert "CAPTAIN_MODEL" not in os.environ


def test_local_env_file_supplies_nebius_key(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=nb-test\n", encoding="utf-8")

    assert llm_config.API_KEY == "nb-test"
    assert "NEBIUS_API_KEY" not in os.environ


def test_process_environment_overrides_the_env_file(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("CAPTAIN_MODEL=nvidia/from-file\n", encoding="utf-8")
    monkeypatch.setenv("CAPTAIN_MODEL", "nvidia/from-process")

    assert llm_config.get_profile().model == "nvidia/from-process"


def test_profile_is_cached_until_reset(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    first = llm_config.get_profile()
    monkeypatch.setenv("CAPTAIN_MODEL", "nvidia/changed")

    assert llm_config.get_profile() is first
    llm_config.reset_profile_cache()
    assert llm_config.get_profile().model == "nvidia/changed"


def test_unknown_module_attribute_still_raises():
    with pytest.raises(AttributeError):
        getattr(llm_config, "NOT_A_SETTING")


def test_stale_openai_model_in_env_file_does_not_break_imports(monkeypatch, tmp_path):
    """A Build-Week .env (CAPTAIN_MODEL=gpt-5.6) must not break the offline gate.

    The contradiction still surfaces, but only when a real client is built.
    """
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "CAPTAIN_MODEL=gpt-5.6\nOPENAI_API_KEY=sk-dummy\n", encoding="utf-8"
    )
    import agenten.llm.model_client as model_client_module

    importlib.reload(llm_config)
    reloaded = importlib.reload(model_client_module)

    with pytest.raises(ProviderConfigurationError, match="gpt-5.6"):
        reloaded.build_model_client()
    with pytest.raises(ProviderConfigurationError, match="gpt-5.6"):
        llm_config.get_profile()
