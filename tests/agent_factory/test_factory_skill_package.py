from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


SKILL_DIR = Path("agenten/agent_factory/skills/autogen-agent-factory")


def test_factory_skill_is_digestible_and_contains_release_boundaries() -> None:
    content = (SKILL_DIR / "SKILL.md").read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    text = content.decode("utf-8")
    assert len(digest) == 64
    for phrase in (
        "verify the released skill and job digests",
        "retrieve AutoGen documentation",
        "dependency-ready work node",
        "preserve prior green assertions",
        "private candidate",
        "never publish",
        "ready_to_use",
    ):
        assert phrase.lower() in text.lower()
    assert "api_key=" not in text.lower()
    assert "bearer " not in text.lower()


def test_factory_skill_frontmatter_and_agent_reference_are_valid() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == "autogen-agent-factory"
    agent = yaml.safe_load((SKILL_DIR / "agents/openai.yaml").read_text(encoding="utf-8"))
    assert agent["interface"]["default_prompt"]
