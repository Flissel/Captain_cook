from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_runtime_entrypoints_do_not_import_legacy_autogen_package():
    sources = [
        ROOT / "agenten" / "Captain.py",
        ROOT / "agenten" / "critic.py",
        ROOT / "blockchain" / "web_scamler.py",
        ROOT / "chats" / "project_maker.py",
    ]

    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "from autogen import" not in text
        assert "import autogen\n" not in text
        assert "new_struct." not in text
        assert "register_nested_chats" not in text


def test_current_dependency_manifest_excludes_legacy_pyautogen():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    package_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "autogen-core==0.7.5" in requirements
    assert "autogen-agentchat==0.7.5" in requirements
    assert not any(line.startswith("pyautogen==") for line in package_lines)
