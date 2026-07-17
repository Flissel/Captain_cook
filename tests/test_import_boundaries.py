import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _normalized_requirement_name(requirement: str) -> str:
    package_name = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", package_name).lower()


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
    package_lines = _requirement_lines(ROOT / "requirements.txt")

    assert "autogen-core==0.7.5" in requirements
    assert "autogen-agentchat==0.7.5" in requirements
    assert not any(line.startswith("pyautogen==") for line in package_lines)


def test_runtime_dependency_manifest_has_unique_package_names():
    package_names = [
        _normalized_requirement_name(requirement)
        for requirement in _requirement_lines(ROOT / "requirements.txt")
    ]
    duplicates = sorted(
        name for name, count in Counter(package_names).items() if count > 1
    )

    assert duplicates == []


def test_development_dependency_manifest_includes_test_tooling():
    manifest = ROOT / "requirements-dev.txt"
    assert manifest.is_file(), "requirements-dev.txt must exist"

    requirement_lines = _requirement_lines(manifest)
    assert "-r requirements.txt" in requirement_lines
    assert "pytest==9.0.2" in requirement_lines
    assert "pytest-asyncio==1.4.0" in requirement_lines
    assert "pytest-cov==4.1.0" in requirement_lines


def test_default_pytest_gate_excludes_explicit_live_and_legacy_tests():
    pytest_config = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert '-m "not live and not legacy"' in pytest_config
    assert "live:" in pytest_config
    assert "legacy:" in pytest_config
