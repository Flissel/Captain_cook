import pytest

from config.llm_provider import (
    ANTHROPIC_DEFAULT_MODEL,
    CLAUDE_CODE_DEFAULT_MODEL,
    CLAUDE_CODE_SHIM_BASE_URL,
    NEBIUS_BASE_URL,
    NEMOTRON_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    PROVIDER_SECRET_NAMES,
    ProviderConfigurationError,
    resolve_provider,
)


def test_default_is_the_claude_code_subscription_shim():
    profile = resolve_provider({})
    assert profile.name == "claude-code"
    assert profile.base_url == CLAUDE_CODE_SHIM_BASE_URL == "http://127.0.0.1:8114/v1"
    assert profile.model == CLAUDE_CODE_DEFAULT_MODEL == "claude-code"
    assert profile.api_key is None
    assert profile.tool_calling is True
    assert profile.structured_output is True


def test_a_nebius_key_alone_no_longer_selects_nebius():
    assert resolve_provider({"NEBIUS_API_KEY": "nb-test"}).name == "claude-code"


def test_nebius_stays_available_on_request():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebius", "NEBIUS_API_KEY": "nb-test"})
    assert profile.name == "nebius"
    assert profile.base_url == NEBIUS_BASE_URL
    assert profile.model == NEMOTRON_DEFAULT_MODEL
    assert profile.api_key == "nb-test"


def test_claude_code_takes_another_shim_port():
    profile = resolve_provider(
        {"CAPTAIN_LLM_PROVIDER": "claude-code", "CAPTAIN_LLM_BASE_URL": "http://127.0.0.1:18114/v1"}
    )
    assert profile.base_url == "http://127.0.0.1:18114/v1"


@pytest.mark.parametrize("model", ["claude-code-opus", "claude-code-sonnet", "claude-code-haiku"])
def test_claude_code_accepts_the_shim_model_ids(model):
    assert resolve_provider({"CAPTAIN_MODEL": model}).model == model


@pytest.mark.parametrize("model", ["nvidia/Nemotron-3-Super-120b-a12b", "gpt-5.6", "claude-opus-5-5"])
def test_claude_code_rejects_a_model_the_shim_would_silently_ignore(model):
    # A leftover CAPTAIN_MODEL must not quietly fall back to the account default.
    with pytest.raises(ProviderConfigurationError, match="claude-code"):
        resolve_provider({"CAPTAIN_MODEL": model})


def test_claude_code_never_reads_an_api_key():
    profile = resolve_provider({"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-test"})
    assert profile.api_key is None


def test_anthropic_uses_the_api_key_and_a_claude_model():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"})
    assert profile.name == "anthropic"
    assert profile.base_url is None
    assert profile.model == ANTHROPIC_DEFAULT_MODEL
    assert profile.api_key == "sk-ant-test"
    assert profile.tool_calling is True
    assert profile.structured_output is False  # AutoGen 0.7.5 Anthropic client
    assert PROVIDER_SECRET_NAMES["anthropic"] == "ANTHROPIC_API_KEY"


def test_anthropic_rejects_non_claude_models_and_base_urls():
    with pytest.raises(ProviderConfigurationError, match="gpt-5.6"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "anthropic", "CAPTAIN_MODEL": "gpt-5.6"})
    with pytest.raises(ProviderConfigurationError, match="CAPTAIN_LLM_BASE_URL"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "anthropic", "CAPTAIN_LLM_BASE_URL": "http://x/v1"})


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
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebius", "CAPTAIN_MODEL": "gpt-5.6", "OPENAI_API_KEY": "sk-old"})


def test_nebius_does_not_fall_back_to_the_openai_key():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebius", "OPENAI_API_KEY": "sk-old"})
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
    assert profile.name == "claude-code"
    assert profile.model == CLAUDE_CODE_DEFAULT_MODEL
    assert profile.api_key is None


def test_api_key_is_not_in_repr():
    assert "nb-secret" not in repr(
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebius", "NEBIUS_API_KEY": "nb-secret"})
    )
    assert "sk-ant-secret" not in repr(
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-secret"})
    )
