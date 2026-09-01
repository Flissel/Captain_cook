import importlib


def test_default_model_is_gpt_5_6(monkeypatch, tmp_path):
    # The module also reads a .env from the working directory, so the
    # default is only pinned once both sources are known to be empty --
    # otherwise this asserts whatever the developer happens to have set.
    monkeypatch.chdir(tmp_path)
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
    assert "OPENAI_API_KEY" not in __import__("os").environ
    assert "CAPTAIN_MODEL" not in __import__("os").environ
