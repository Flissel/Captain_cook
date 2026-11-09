"""The Captain planner role files stay in step with the real MCP surface."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from agenten.captain_mcp.plan_tools import CaptainPlanner
from agenten.captain_mcp.server import CaptainGatewayWriter, create_mcp_app
from agenten.constitution.ruleset import load_constitution
from agenten.constitution.validators import (
    MAX_CAPABILITY_TAGS,
    MAX_DESCRIPTION_LENGTH,
    MIN_DESCRIPTION_LENGTH,
)
from agenten.planning.supplied_plan import (
    PlanCompilerSettings,
    SuppliedPlan,
    compile_supplied_plan,
)
from agenten.validation.contracts import AssertionKind
from tests.captain_mcp.test_captain_mcp_server import INIT, MCP_TOKEN, reader, rpc
from tests.captain_mcp.test_openclaw_deploy import DEPLOY, compose, seed_config

WORKSPACE = DEPLOY / "workspace"
ROLE = (WORKSPACE / "AGENTS.md").read_text(encoding="utf-8")
SKILL = (WORKSPACE / "skills" / "captain-plan" / "SKILL.md").read_text(encoding="utf-8")


class _NoRelease:
    async def release(self, batch: Any, holdouts: Any) -> None:
        raise AssertionError("not called")


def _full_surface() -> dict[str, dict[str, Any]]:
    planner = CaptainPlanner(
        PlanCompilerSettings("python", frozenset({"python"}), frozenset({"python"})),
        release_client=_NoRelease(),
    )
    release_writer = CaptainGatewayWriter(
        "https://gateway.test", "captain-token", httpx.AsyncClient(), release_policy_version="v1"
    )
    app = create_mcp_app(reader([]), inbound_token=MCP_TOKEN, writer=release_writer, planner=planner)
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        rpc(client, "initialize", INIT)
        listed = rpc(client, "tools/list", {})
    return {tool["name"]: tool for tool in listed.json()["result"]["tools"]}


def _names(text: str) -> set[str]:
    return set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))


def test_the_confirmation_rule_lists_exactly_the_write_tools() -> None:
    tools = _full_surface()
    rule = ROLE.split("3. Ask the operator", 1)[1].split("\n4. ", 1)[0]
    ask_first, no_confirmation = rule.split("Read-only calls", 1)

    writes = {name for name, tool in tools.items() if not tool["annotations"]["readOnlyHint"]}
    reads = {name for name, tool in tools.items() if tool["annotations"]["readOnlyHint"]}
    assert _names(ask_first) == writes
    assert _names(no_confirmation) == reads


def test_the_role_names_no_tool_that_does_not_exist() -> None:
    tools = set(_full_surface())

    assert _names(ROLE) - {"pending_review"} == tools
    assert "approve_batch" not in _names(ROLE) | _names(SKILL) | tools


def test_skill_frontmatter_has_name_and_description() -> None:
    front = yaml.safe_load(SKILL.split("---", 2)[1])

    assert front["name"] == "captain-plan"
    assert len(front["description"]) > 40


def test_skill_numbers_match_the_enforced_rules() -> None:
    assert f"at least {MIN_DESCRIPTION_LENGTH} and at most\n  {MAX_DESCRIPTION_LENGTH} characters" in SKILL
    assert f"1 to {MAX_CAPABILITY_TAGS} non-empty" in SKILL
    assert f"(default {load_constitution().default_budget.max_total_subproblems})" in SKILL
    assert _names(SKILL) >= {kind.value for kind in AssertionKind}


@pytest.mark.asyncio
async def test_skill_example_plan_compiles() -> None:
    block = re.search(r"```json\n(.*?)\n```", SKILL, re.DOTALL)
    assert block is not None
    plan = SuppliedPlan.model_validate(json.loads(block.group(1)))
    planner = CaptainPlanner(PlanCompilerSettings("python", frozenset({"python"}), frozenset({"python"})))

    assert planner.check(plan).accepted
    compiled = await compile_supplied_plan(plan, planner.settings)
    assert [batch.batch_id for batch in compiled.batches] == ["collect-data"]


def test_seed_service_installs_the_role_files_read_only() -> None:
    seed = compose()["services"]["captain-openclaw-seed"]
    mounts = {volume["target"]: volume for volume in seed["volumes"] if isinstance(volume, dict)}
    script = seed["command"][0]

    assert mounts["/seed/workspace"]["source"] == "./workspace"
    assert mounts["/seed/workspace"]["read_only"] is True
    assert "/home/node/.openclaw/workspace/AGENTS.md" in script
    assert "/home/node/.openclaw/workspace/skills/captain-plan/SKILL.md" in script


def test_planner_config_denies_execution_and_unplanned_turns() -> None:
    config = seed_config()

    assert set(config["tools"]["deny"]) >= {"group:runtime", "group:automation"}
    assert config["plugins"]["entries"]["memory-core"]["config"]["dreaming"]["enabled"] is False
    assert config["agents"]["defaults"]["skipBootstrap"] is True


def test_role_files_are_plain_text_without_secrets() -> None:
    for text in (ROLE, SKILL):
        assert not re.search(r"(sk-ant|Bearer [A-Za-z0-9_-]{16,}|CAPTAIN_MCP_CAPTAIN_TOKEN=)", text)
    assert Path(WORKSPACE / "AGENTS.md").stat().st_size < 20_000  # bootstrapMaxChars
