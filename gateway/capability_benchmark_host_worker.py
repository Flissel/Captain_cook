"""Fresh-process HostAutoGen runner for public generic benchmark cases."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

import httpx
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import HandoffMessage
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat, Swarm

from agenten.agent_factory.business_benchmark_production_ports import (
    ProxyResponsesChatCompletionClient,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateEvaluator,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.team_execution import FactoryAutoGenTeamManifestV1
from agenten.agent_runtime.contracts import ArtifactRef


def _read_prompt(workspace: Path, reference: ArtifactRef) -> str:
    matches = [path for path in workspace.rglob("*") if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == reference.sha256]
    if len(matches) != 1:
        raise ValueError("sealed system prompt is unavailable")
    return matches[0].read_text(encoding="utf-8")


def _tool_function(
    *, tool_key: str, authorization: dict[str, object],
    gateway_url: str, observed: dict[str, ArtifactRef],
) -> Callable[[dict[str, object]], dict[str, object]]:
    def execute(arguments: dict[str, object]) -> dict[str, object]:
        payload = {
            "schema": "captain.capability-benchmark-tool-request.v1",
            "tool_key": tool_key, "call_id": authorization["call_id"],
            "lease_id": authorization["lease_id"],
            "call_ref": authorization["call_ref"],
            "lease_ref": authorization["lease_ref"],
            "operation": authorization["operation"], "arguments": arguments,
        }
        with httpx.Client(
            timeout=httpx.Timeout(30.0), follow_redirects=False, trust_env=False,
        ) as client:
            response = client.post(
                gateway_url.rstrip("/") + "/v1/capability-benchmark/tools/execute",
                headers={"Authorization": "Bearer " + str(authorization["scoped_token"])},
                json=payload,
            )
        response.raise_for_status()
        value = response.json()
        if set(value) != {"schema", "value", "result_ref", "receipt_ref"} or value["schema"] != "captain.capability-benchmark-tool-response.v1":
            raise ValueError("Gateway tool readback is invalid")
        observed[tool_key] = ArtifactRef.model_validate(value["result_ref"])
        result = value["value"]
        if not isinstance(result, dict):
            raise ValueError("Gateway tool result is invalid")
        return result

    execute.__name__ = tool_key
    execute.__doc__ = f"Execute the exact scoped {tool_key} read-only benchmark tool."
    return execute


async def execute(payload: dict[str, object]) -> dict[str, object]:
    request = payload["request"]
    prepared = payload["prepared"]
    configuration = payload["configuration"]
    if not isinstance(request, dict) or not isinstance(prepared, dict) or not isinstance(configuration, dict):
        raise ValueError("isolated benchmark payload is invalid")
    material = prepared["material"]
    if not isinstance(material, dict) or material.get("schema") != "captain.capability-benchmark-process-material.v1":
        raise ValueError("isolated benchmark material is invalid")
    job = AgentFactoryJobV3.model_validate(material["job"])
    candidate_manifest = FactoryCandidateManifest.model_validate_json(
        base64.b64decode(str(material["candidate_manifest_base64"]), validate=True)
    )
    archive = base64.b64decode(str(material["candidate_archive_base64"]), validate=True)
    if hashlib.sha256(archive).hexdigest() != candidate_manifest.source_archive_ref.sha256:
        raise ValueError("isolated candidate archive changed")
    plan = request["execution_plan"]
    case = request["execution_case"]
    if not isinstance(plan, dict) or not isinstance(case, dict):
        raise ValueError("isolated execution binding is invalid")
    model_binding = json.loads(base64.b64decode(str(material["model_binding_base64"])))
    if model_binding != {
        "schema": "captain.generic-benchmark-model.v1",
        "model_id": request["model_id"],
    }:
        raise ValueError("isolated model binding changed")
    client = ProxyResponsesChatCompletionClient(
        endpoint=str(configuration["proxy_url"]).rstrip("/") + "/v1/responses",
        token=str(configuration["proxy_token"]), model=str(request["model_id"]),
        context_reference="gateway://provider-budget-contexts/" + str(configuration["context_sha256"]),
    )
    authorizations = {
        str(item["tool_key"]): item for item in prepared["authorized_tools"]
    }
    declared = tuple(str(item) for item in case["declared_tool_keys"])
    if set(authorizations) != set(declared):
        raise ValueError("isolated tool authorization changed")
    observed: dict[str, ArtifactRef] = {}
    tools = {
        key: _tool_function(
            tool_key=key, authorization=authorizations[key],
            gateway_url=str(configuration["gateway_url"]), observed=observed,
        ) for key in declared
    }
    task = json.dumps({
        "schema": "captain.generic-benchmark-public-task.v1",
        "setup": case["setup"], "action": case["action"],
        "opaque_source_refs": case["opaque_source_refs"],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    started = time.monotonic_ns()
    with tempfile.TemporaryDirectory(prefix="captain-isolated-benchmark-") as temporary:
        archive_path = Path(temporary) / "candidate.zip"
        archive_path.write_bytes(archive)
        candidate = ResolvedFactoryCandidate(
            candidate=candidate_manifest, source_archive=archive_path,
        )
        with FactoryCandidateEvaluator().verified_team_workspace(candidate) as (workspace, manifest):
            if not isinstance(manifest, FactoryAutoGenTeamManifestV1):
                raise ValueError("isolated team manifest is invalid")
            if request["team_kind"] == "team":
                agents = [AssistantAgent(
                    name=item.name, model_client=client,
                    tools=[tools[name] for name in item.tools if name in tools],
                    handoffs=list(item.handoffs),
                    system_message=_read_prompt(workspace, item.system_prompt_ref),
                    max_tool_iterations=max(1, manifest.max_tool_calls),
                ) for item in manifest.agents]
                termination = MaxMessageTermination(manifest.max_messages)
                if manifest.conversation_pattern == "swarm":
                    runner = Swarm(agents, termination_condition=termination, max_turns=manifest.max_messages)
                elif manifest.conversation_pattern == "selector_group_chat":
                    runner = SelectorGroupChat(agents, model_client=client, termination_condition=termination, max_turns=manifest.max_messages)
                else:
                    runner = RoundRobinGroupChat(agents, termination_condition=termination, max_turns=manifest.max_messages)
                result = await runner.run(task=task)
                agent_names = tuple(item.name for item in manifest.agents)
                execution_ref = plan["team_manifest_ref"]
            else:
                baseline = json.loads(base64.b64decode(str(material["baseline_policy_base64"])))
                source = manifest.agents[0]
                agent = AssistantAgent(
                    name=str(baseline["agent_name"]), model_client=client,
                    tools=[tools[name] for name in baseline["allowed_tools"]],
                    system_message=_read_prompt(workspace, source.system_prompt_ref),
                    max_tool_iterations=max(1, int(baseline["max_tool_calls"])),
                )
                result = await agent.run(task=task)
                agent_names = (str(baseline["agent_name"]),)
                execution_ref = plan["baseline_policy_ref"]
    if set(observed) != set(declared):
        raise ValueError("isolated HostAutoGen did not call every declared tool")
    usage = client.total_usage()
    pricing = configuration["pricing"]
    cost = (
        Decimal(usage.prompt_tokens) * Decimal(str(pricing["input_cost_per_million_usd"]))
        + Decimal(usage.completion_tokens) * Decimal(str(pricing["output_cost_per_million_usd"]))
    ) / Decimal(1_000_000)
    messages = [{
        "message_key": f"message-{index}",
        "content": str(getattr(message, "content", "")),
    } for index, message in enumerate(result.messages, start=1)]
    handoffs = [{
        "from_agent": str(message.source), "to_agent": str(message.target),
        "reason_code": "autogen_handoff",
    } for message in result.messages if isinstance(message, HandoffMessage)]
    return {
        "schema": "captain.capability-benchmark-process-result.v1",
        "observations": {
            "message_count": len(messages), "handoff_count": len(handoffs),
            "tool_call_count": len(observed),
            "termination_reason": str(result.stop_reason or "task_completed"),
        },
        "rationale": str(result.stop_reason or "task_completed"),
        "messages": messages, "handoffs": handoffs,
        "tool_result_refs": {
            key: value.model_dump(mode="json") for key, value in observed.items()
        },
        "cost_usd": format(cost, "f"),
        "latency_ms": max(1, (time.monotonic_ns() - started) // 1_000_000),
        "candidate_ref": plan["candidate_ref"], "execution_ref": execution_ref,
        "agent_names": list(agent_names),
    }


def main() -> None:
    raw = json.loads(sys.stdin.buffer.read())
    result = asyncio.run(execute(raw))
    sys.stdout.buffer.write(json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode())


if __name__ == "__main__":
    main()
