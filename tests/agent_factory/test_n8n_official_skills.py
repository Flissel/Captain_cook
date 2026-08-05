from __future__ import annotations

from agenten.agent_factory.n8n_official_skills import (
    load_official_n8n_skills_lock,
)


def test_official_n8n_skill_lock_is_complete_and_pinned() -> None:
    lock = load_official_n8n_skills_lock()

    assert lock.schema_name == "captain.n8n-official-skills-lock.v1"
    assert lock.repository == "https://github.com/n8n-io/skills"
    assert lock.commit == "046c330c9308bbfc54ceab1adbe3d8fc6bebc8fa"
    assert lock.plugin == "n8n-skills@n8n-io"
    assert lock.plugin_version == "1.1.0"
    assert lock.minimum_codex_version == "0.142.0"
    assert lock.minimum_n8n_version == "2.2.0"
    assert lock.meta_skill == "using-n8n-skills-official"
    assert lock.skills == (
        "using-n8n-skills-official",
        "n8n-workflow-lifecycle-official",
        "n8n-subworkflows-official",
        "n8n-extending-mcp-official",
        "n8n-expressions-official",
        "n8n-node-configuration-official",
        "n8n-code-nodes-official",
        "n8n-loops-official",
        "n8n-agents-official",
        "n8n-error-handling-official",
        "n8n-credentials-and-security-official",
        "n8n-binary-and-data-official",
        "n8n-data-tables-official",
        "n8n-debugging-official",
    )
