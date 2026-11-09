import json
from typing import Any

import pytest

from agenten.evaluation import cli as evaluation_cli
from agenten.llm import model_client as model_client_module
from config.llm_provider import (
    CUSTOM_ENDPOINT_PLACEHOLDER_KEY,
    NEBIUS_BASE_URL,
    ProviderConfigurationError,
    ProviderProfile,
)

_NEBIUS = ProviderProfile("nebius", "nvidia/Nemotron-3-Super-120b-a12b", NEBIUS_BASE_URL, "nb-test", True, True)
_OPENAI = ProviderProfile("openai", "gpt-5.6", None, "sk-test", True, True)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def build_stub(**kw: Any) -> object:
        captured.update(kw)
        return object()

    # OpenAI is built in the CLI; every other provider through the shared builder.
    monkeypatch.setattr(evaluation_cli, "OpenAIChatCompletionClient", build_stub)
    monkeypatch.setattr(model_client_module, "OpenAIChatCompletionClient", build_stub)
    return captured


def test_claude_code_evaluation_client_uses_the_shim_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture(monkeypatch)
    profile = ProviderProfile("claude-code", "claude-code", "http://127.0.0.1:8114/v1", None, True, True)

    evaluation_cli.build_evaluation_model_client(profile=profile)

    assert captured["base_url"] == "http://127.0.0.1:8114/v1"
    assert captured["api_key"] == CUSTOM_ENDPOINT_PLACEHOLDER_KEY
    assert captured["parallel_tool_calls"] is False
    assert captured["model_info"]["function_calling"] is True


def test_nebius_evaluation_client_uses_token_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_NEBIUS)

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["api_key"] == "nb-test"
    assert captured["model"] == "nvidia/Nemotron-3-Super-120b-a12b"
    assert captured["model_info"]["function_calling"] is True
    assert "reasoning_effort" not in captured  # OpenAI-only parameter


def test_openai_evaluation_client_keeps_gpt5_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_OPENAI)

    assert "base_url" not in captured
    assert captured["reasoning_effort"] == "none"
    assert captured["model_info"]["family"] == "gpt-5"


def test_model_argument_overrides_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_NEBIUS, model="nvidia/other")

    assert captured["model"] == "nvidia/other"


def test_evaluation_requires_a_tool_capable_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    shim = ProviderProfile("custom", "claude-code", "http://127.0.0.1:8114/v1", None, False, True)

    with pytest.raises(ProviderConfigurationError, match="tool calling"):
        evaluation_cli.build_evaluation_model_client(profile=shim)


def test_nebius_evaluation_without_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    keyless = ProviderProfile("nebius", "nvidia/x", NEBIUS_BASE_URL, None, True, True)

    with pytest.raises(ProviderConfigurationError, match="NEBIUS_API_KEY"):
        evaluation_cli.build_evaluation_model_client(profile=keyless)


def test_evaluation_model_info_follows_the_profile_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture(monkeypatch)
    profile = ProviderProfile("nebius", "nvidia/x", NEBIUS_BASE_URL, "nb-test", True, False)

    evaluation_cli.build_evaluation_model_client(profile=profile)

    assert captured["model_info"]["function_calling"] is profile.tool_calling
    assert captured["model_info"]["structured_output"] is False


@pytest.mark.asyncio
async def test_missing_nebius_key_is_reported_as_invalid_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("CAPTAIN_LLM_BASE_URL", "CAPTAIN_MODEL", "NEBIUS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CAPTAIN_LLM_PROVIDER", "nebius")
    constructed: list[object] = []
    for module in (evaluation_cli, model_client_module):
        monkeypatch.setattr(
            module, "OpenAIChatCompletionClient", lambda **kw: constructed.append(kw) or object()
        )
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")

    exit_code = await evaluation_cli.async_main(
        [
            str(input_path),
            "--source-reference",
            "agentfarm/input.md",
            "--output",
            str(tmp_path / "artifacts"),
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "invalid_provider",
        "status": "failed",
    }
    assert constructed == []
