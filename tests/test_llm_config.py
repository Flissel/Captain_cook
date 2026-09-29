import importlib
import os

from config.llm_provider import NEMOTRON_DEFAULT_MODEL

_PROVIDER_VARS = (
    "CAPTAIN_LLM_PROVIDER",
    "CAPTAIN_LLM_BASE_URL",
    "CAPTAIN_MODEL",
    "NEBIUS_API_KEY",
    "OPENAI_API_KEY",
)


def _clean(monkeypatch, tmp_path):
    # The module also reads a .env from the working directory, so defaults are
    # only pinned once both sources are known to be empty.
    monkeypatch.chdir(tmp_path)
    for name in _PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)


def test_default_model_is_nemotron_on_nebius(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.PROFILE.name == "nebius"
    assert reloaded.MODEL == NEMOTRON_DEFAULT_MODEL


def test_local_env_file_supplies_openai_opt_in(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "CAPTAIN_LLM_PROVIDER=openai\nOPENAI_API_KEY=test-key\nCAPTAIN_MODEL=test-model\n",
        encoding="utf-8",
    )
    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.API_KEY == "test-key"
    assert reloaded.MODEL == "test-model"
    assert "OPENAI_API_KEY" not in os.environ
    assert "CAPTAIN_MODEL" not in os.environ


def test_local_env_file_supplies_nebius_key(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=nb-test\n", encoding="utf-8")
    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.API_KEY == "nb-test"
    assert "NEBIUS_API_KEY" not in os.environ
