"""compile_plan / publish_plan: Captain compiles, the planner only supplies."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from agenten.captain_mcp.plan_tools import (
    CaptainPlanner,
    PlanToolsConfigurationError,
    plan_compiler_settings_from_env,
)
from agenten.captain_mcp.server import McpSettings, create_mcp_app
from agenten.planning.gateway_client import GatewayPlanningClient
from tests.captain_mcp.test_captain_mcp_server import INIT, MCP_TOKEN, environment, reader, rpc
from tests.planning.test_supplied_plan import SETTINGS, plan

CAPTAIN_TOKEN = "captain-mcp-captain-token"


def _call(app: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        rpc(client, "initialize", INIT)
        response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    result: dict[str, Any] = response.json()["result"]
    return result


def _tools(app: Any) -> dict[str, dict[str, Any]]:
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        rpc(client, "initialize", INIT)
        listed = rpc(client, "tools/list", {})
    return {tool["name"]: tool for tool in listed.json()["result"]["tools"]}


def _plan_json() -> dict[str, Any]:
    return plan().model_dump(mode="json")


# --- settings -------------------------------------------------------------


def test_planning_tools_stay_off_without_a_target() -> None:
    assert plan_compiler_settings_from_env({}) is None
    assert McpSettings.from_env(environment()).plan is None


@pytest.mark.parametrize(
    "values",
    [
        {"CAPTAIN_PLAN_CAPABILITY_TAGS": "python"},
        {"CAPTAIN_PLAN_TARGET": "python"},
        {"CAPTAIN_PLAN_TARGET": "python", "CAPTAIN_PLAN_CAPABILITY_TAGS": " , "},
        {
            "CAPTAIN_PLAN_TARGET": "python",
            "CAPTAIN_PLAN_CAPABILITY_TAGS": "python",
            "CAPTAIN_PLAN_ALLOWED_TARGETS": "n8n",
        },
    ],
)
def test_incomplete_planning_settings_fail_closed(values: dict[str, str]) -> None:
    with pytest.raises(PlanToolsConfigurationError):
        plan_compiler_settings_from_env(values)


def test_planning_settings_parse_lists_and_default_the_allow_list() -> None:
    settings = McpSettings.from_env(
        environment(CAPTAIN_PLAN_TARGET="python", CAPTAIN_PLAN_CAPABILITY_TAGS="python, email,")
    ).plan

    assert settings is not None
    assert settings.capability_tags == frozenset({"python", "email"})
    assert settings.allowed_targets == frozenset({"python"})


# --- tool surface ---------------------------------------------------------


def test_compile_only_planner_offers_a_read_only_compile_tool() -> None:
    tools = _tools(create_mcp_app(reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS)))

    assert "compile_plan" in tools and "publish_plan" not in tools
    assert tools["compile_plan"]["annotations"]["readOnlyHint"] is True


def test_publish_tool_needs_a_release_client_and_is_not_idempotent() -> None:
    planner = CaptainPlanner(SETTINGS, release_client=_RecordingRelease())
    tools = _tools(create_mcp_app(reader([]), inbound_token=MCP_TOKEN, planner=planner))

    annotations = tools["publish_plan"]["annotations"]
    assert annotations["readOnlyHint"] is False
    assert annotations["idempotentHint"] is False


def test_no_planner_means_no_planning_tools() -> None:
    tools = _tools(create_mcp_app(reader([]), inbound_token=MCP_TOKEN))

    assert not {"compile_plan", "publish_plan"} & set(tools)


# --- behaviour ------------------------------------------------------------


class _RecordingRelease:
    def __init__(self) -> None:
        self.batch_ids: list[str] = []

    async def release(self, batch: Any, holdouts: Any) -> None:
        self.batch_ids.append(batch.batch_id)


def test_compile_plan_returns_batches_but_not_holdout_expectations() -> None:
    app = create_mcp_app(reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS))

    result = _call(app, "compile_plan", {"plan": _plan_json()})

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert [batch["batch_id"] for batch in payload["batches"]] == ["collect", "mail"]
    assert payload["holdout_case_counts"] == {"collect": 1, "mail": 1}
    assert "holdout-2" not in json.dumps(result)


def test_compile_plan_reports_policy_violations_as_tool_errors() -> None:
    app = create_mcp_app(reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS))
    rogue = _plan_json()
    rogue["enrichments"]["collect"]["capability_tags"] = ["root"]

    result = _call(app, "compile_plan", {"plan": rogue})

    assert result["isError"] is True


def test_publish_plan_releases_every_batch_held_for_review_as_captain() -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"index": len(seen)}, request=request)

    release = GatewayPlanningClient(
        "https://gateway.test",
        CAPTAIN_TOKEN,
        httpx.AsyncClient(transport=httpx.MockTransport(gateway)),
        hold_for_review=True,
    )
    app = create_mcp_app(
        reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS, release_client=release)
    )

    result = _call(app, "publish_plan", {"plan": _plan_json()})

    assert result["isError"] is False
    assert result["structuredContent"]["published"] is True
    bodies = [json.loads(request.content) for request in seen]
    work_batches = [body for body in bodies if body["block_type"] == "work_batch"]
    assert [body["data"]["batch_id"] for body in work_batches] == ["collect", "mail"]
    assert all(body["status"] == "pending_review" for body in work_batches)
    assert all(request.headers["authorization"] == f"Bearer {CAPTAIN_TOKEN}" for request in seen)


def test_publish_conflicts_surface_without_partial_success_claims() -> None:
    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "batch exists"}, request=request)

    release = GatewayPlanningClient(
        "https://gateway.test",
        CAPTAIN_TOKEN,
        httpx.AsyncClient(transport=httpx.MockTransport(gateway)),
        hold_for_review=True,
    )
    app = create_mcp_app(
        reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS, release_client=release)
    )

    result = _call(app, "publish_plan", {"plan": _plan_json()})

    assert result["isError"] is True
    assert "409" in json.dumps(result["content"])
    assert CAPTAIN_TOKEN not in json.dumps(result)


# --- constitution ---------------------------------------------------------


def _bad_plan_json() -> dict[str, Any]:
    bad = _plan_json()
    bad["subtasks"][1]["description"] = "tiny"
    return bad


def test_check_constitution_reports_the_deterministic_verdict() -> None:
    def app() -> Any:
        return create_mcp_app(reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS))

    passed = _call(app(), "check_constitution", {"plan": _plan_json()})["structuredContent"]
    failed = _call(app(), "check_constitution", {"plan": _bad_plan_json()})["structuredContent"]

    assert passed["accepted"] is True and passed["semantic_review"] == "not_performed"
    assert failed["accepted"] is False
    assert failed["rejections"][0].startswith("s2: malformed")


def test_check_constitution_is_read_only() -> None:
    tools = _tools(create_mcp_app(reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS)))

    assert tools["check_constitution"]["annotations"]["readOnlyHint"] is True


def test_publish_refuses_a_plan_the_constitution_rejects() -> None:
    release = _RecordingRelease()
    app = create_mcp_app(
        reader([]), inbound_token=MCP_TOKEN, planner=CaptainPlanner(SETTINGS, release_client=release)
    )

    result = _call(app, "publish_plan", {"plan": _bad_plan_json()})

    assert result["isError"] is True
    assert "constitution check failed" in json.dumps(result["content"])
    assert release.batch_ids == []
