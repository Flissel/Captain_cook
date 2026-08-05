"""Pinned official n8n skill metadata used by Captain-authored Codex briefs."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_SHA256_PATTERN = r"^[0-9a-f]{40}$"
_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"
_DEFAULT_LOCK_PATH = Path(__file__).with_name("n8n_official_skills.lock.json")

N8N_CODEX_BUILD_SKILLS = (
    "using-n8n-skills-official",
    "n8n-workflow-lifecycle-official",
    "n8n-node-configuration-official",
    "n8n-agents-official",
    "n8n-error-handling-official",
    "n8n-credentials-and-security-official",
)

N8N_MCP_BUILD_SEQUENCE = (
    "get_sdk_reference",
    "get_node_types",
    "validate_workflow",
    "create_workflow_from_code or update_workflow",
    "get_workflow_details",
)


class OfficialN8nSkillsLockV1(BaseModel):
    """Strict, reviewable binding to the upstream n8n skills plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.n8n-official-skills-lock.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    repository: Literal["https://github.com/n8n-io/skills"]
    source: Literal["n8n-io/skills"]
    commit: str = Field(pattern=_SHA256_PATTERN)
    marketplace: Literal["n8n-io"]
    plugin: Literal["n8n-skills@n8n-io"]
    plugin_version: str = Field(pattern=_VERSION_PATTERN)
    minimum_codex_version: str = Field(pattern=_VERSION_PATTERN)
    minimum_n8n_version: str = Field(pattern=_VERSION_PATTERN)
    mcp_server_name: Literal["n8n"]
    mcp_url: Literal["http://localhost:5679/mcp-server/http"]
    mcp_bearer_token_env_var: Literal["N8N_MCP_TOKEN"]
    meta_skill: Literal["using-n8n-skills-official"]
    skills: tuple[str, ...]

    @model_validator(mode="after")
    def require_complete_unique_catalog(self) -> "OfficialN8nSkillsLockV1":
        if not self.skills or self.skills[0] != self.meta_skill:
            raise ValueError("official n8n meta skill must be first")
        if len(self.skills) != len(set(self.skills)):
            raise ValueError("official n8n skill catalog contains duplicates")
        missing = set(N8N_CODEX_BUILD_SKILLS).difference(self.skills)
        if missing:
            raise ValueError("official n8n build skill catalog is incomplete")
        return self


@lru_cache(maxsize=1)
def load_official_n8n_skills_lock(
    path: Path = _DEFAULT_LOCK_PATH,
) -> OfficialN8nSkillsLockV1:
    return OfficialN8nSkillsLockV1.model_validate_json(path.read_text(encoding="utf-8"))


def official_n8n_build_protocol() -> dict[str, object]:
    """Return the bounded upstream protocol embedded in n8n Codex assignments."""

    lock = load_official_n8n_skills_lock()
    return {
        "source": lock.source,
        "commit": lock.commit,
        "plugin": lock.plugin,
        "minimum_n8n_version": lock.minimum_n8n_version,
        "mcp_server_name": lock.mcp_server_name,
        "meta_skill": lock.meta_skill,
        "required_skills": list(N8N_CODEX_BUILD_SKILLS),
        "mcp_sequence": list(N8N_MCP_BUILD_SEQUENCE),
        "report_skills_used": True,
        "rules": [
            "Invoke the official meta skill before every n8n action.",
            "Use live MCP SDK and node metadata instead of remembered parameters.",
            "Validate before create, update, or publish and fetch the workflow back afterward.",
            "Use the n8n credential system; never place tokens or secrets in workflow fields.",
            "Prefer native nodes and official credentials before HTTP Request; use Code only as a last resort.",
            "Do not publish or execute side effects without the separate Captain lease and evidence contract.",
        ],
    }
