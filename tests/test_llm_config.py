import importlib


def test_default_model_is_gpt_5_6(monkeypatch):
    monkeypatch.delenv("CAPTAIN_MODEL", raising=False)

    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.MODEL == "gpt-5.6"
