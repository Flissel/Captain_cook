import importlib


def test_default_model_is_gpt_5_6(monkeypatch):
    monkeypatch.delenv("CAPTAIN_MODEL", raising=False)

    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.MODEL == "gpt-5.6"


def test_local_env_file_supplies_legacy_model_configuration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CAPTAIN_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=test-key\nCAPTAIN_MODEL=test-model\n",
        encoding="utf-8",
    )

    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.API_KEY == "test-key"
    assert reloaded.MODEL == "test-model"
