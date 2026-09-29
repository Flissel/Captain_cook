import pytest

from config.llm_provider import (
    NEBIUS_BASE_URL,
    NEMOTRON_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    ProviderConfigurationError,
    resolve_provider,
)


def test_default_is_nebius_with_nemotron():
    profile = resolve_provider({"NEBIUS_API_KEY": "nb-test"})
    assert profile.name == "nebius"
    assert profile.base_url == NEBIUS_BASE_URL
    assert profile.model == NEMOTRON_DEFAULT_MODEL
    assert profile.api_key == "nb-test"


def test_provider_name_is_normalized():
    assert resolve_provider({"CAPTAIN_LLM_PROVIDER": " Nebius "}).name == "nebius"


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderConfigurationError, match="nebuis"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebuis"})


def test_openai_is_opt_in_and_keeps_its_default_model():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"})
    assert profile.name == "openai"
    assert profile.base_url is None
    assert profile.model == OPENAI_DEFAULT_MODEL
    assert profile.api_key == "sk-test"


def test_openai_model_is_rejected_under_nebius():
    # A leftover Build-Week .env must not send gpt-* model names to Token Factory.
    with pytest.raises(ProviderConfigurationError, match="gpt-5.6"):
        resolve_provider({"CAPTAIN_MODEL": "gpt-5.6", "OPENAI_API_KEY": "sk-old"})


def test_nebius_does_not_fall_back_to_the_openai_key():
    profile = resolve_provider({"OPENAI_API_KEY": "sk-old"})
    assert profile.name == "nebius"
    assert profile.api_key is None


def test_base_url_alone_keeps_the_local_shim_path():
    profile = resolve_provider({"CAPTAIN_LLM_BASE_URL": "http://127.0.0.1:8114/v1"})
    assert profile.name == "custom"
    assert profile.base_url == "http://127.0.0.1:8114/v1"
    assert profile.tool_calling is False
    assert profile.structured_output is True


def test_base_url_with_nebius_is_contradictory():
    with pytest.raises(ProviderConfigurationError, match="CAPTAIN_LLM_BASE_URL"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebius", "CAPTAIN_LLM_BASE_URL": "http://x/v1"})


def test_custom_requires_a_base_url():
    with pytest.raises(ProviderConfigurationError, match="CAPTAIN_LLM_BASE_URL"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "custom"})


def test_blank_values_count_as_unset():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "", "CAPTAIN_MODEL": "  ", "NEBIUS_API_KEY": ""})
    assert profile.name == "nebius"
    assert profile.model == NEMOTRON_DEFAULT_MODEL
    assert profile.api_key is None


def test_api_key_is_not_in_repr():
    assert "nb-secret" not in repr(resolve_provider({"NEBIUS_API_KEY": "nb-secret"}))
