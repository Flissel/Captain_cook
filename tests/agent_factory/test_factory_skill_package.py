from __future__ import annotations

import hashlib
from pathlib import Path
import re

import pytest
import yaml


SKILL_DIR = Path("agenten/agent_factory/skills/autogen-agent-factory")

SKILLS = {
    "captain-factory-discover": ("CodebaseInventoryV1", "do not change code"),
    "captain-factory-brief-codex": ("CodexBuildBriefV1", "codex.run"),
    "captain-factory-execute-team": ("TeamExecutionEvidenceV1", "max_cost_usd"),
    "captain-factory-evaluate-team": ("TeamEvaluationV1", "do not repair"),
    "captain-factory-improve-team": ("CandidateRevisionV1", "prior green"),
    "captain-factory-report-captain": ("FactoryFeedbackV1", "Captain decides"),
}

SKILL_RESOURCES = {
    "captain-factory-discover": ("references/output-schema.md",),
    "captain-factory-brief-codex": ("templates/codex-assignment.md",),
    "captain-factory-execute-team": ("references/evidence-contract.md",),
    "captain-factory-evaluate-team": ("references/rubric.md",),
    "captain-factory-improve-team": ("templates/repair-assignment.md",),
    "captain-factory-report-captain": ("references/recommendations.md",),
}


@pytest.mark.parametrize("skill_name,required", SKILLS.items())
def test_factory_workflow_skill_is_valid_safe_and_digestible(
    skill_name: str, required: tuple[str, str]
) -> None:
    root = Path("agenten/agent_factory/skills") / skill_name
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert frontmatter["name"] == skill_name
    assert all(phrase.lower() in text.lower() for phrase in required)
    assert len(text.split()) <= 400
    assert "--yolo" not in text
    assert "api_key=" not in text.lower()
    assert "bearer " not in text.lower()
    for resource in SKILL_RESOURCES[skill_name]:
        assert (root / resource).is_file()
        assert resource in text
    for reference in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        assert (root / reference).is_file()


def test_factory_workflow_bundle_is_non_authoritative_operator_aid() -> None:
    bundle = Path("agenten/agent_factory/skills/captain-agent-factory-loop/bundle.yaml")
    manifest = yaml.safe_load(bundle.read_text(encoding="utf-8"))

    assert manifest == {
        "name": "captain-agent-factory-loop",
        "description": "Captain-controlled six-skill AutoGen factory workflow",
        "skills": list(SKILLS),
        "instruction": "Use only the step released by the current Captain invocation.",
    }


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
