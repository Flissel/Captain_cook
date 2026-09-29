import json

import pytest

import main
from agenten.demo import run_demo
from agenten.llm.model_client import build_replay_model_client

_LLM_PREFIX = "llm_reported:"


def _report() -> str:
    # Role-agnostic: householders run in arbitrary order and the executor
    # (not the model) decides which role a report belongs to.
    return json.dumps(
        {"decision": "accept", "artifacts": [], "evidence": ["checked"], "limitations": []}
    )


def _evidence(summary) -> list[str]:
    return [
        item
        for block in summary.blocks
        if block["block_type"] == "subproblem"
        for item in (block.get("result") or {}).get("evidence", [])
    ]


@pytest.mark.asyncio
async def test_demo_runs_householders_on_an_injected_model_client():
    # One identical reply per householder; the replay client never touches the network.
    client = build_replay_model_client([_report() for _ in range(4)])

    summary = await run_demo(householder_model_client=client)

    results = [
        block["result"] for block in summary.blocks if block["block_type"] == "subproblem"
    ]
    assert summary.success is True
    assert len(results) == 4
    # Every householder report carries the provenance marker as its first evidence item.
    assert all(result["evidence"][0].startswith(_LLM_PREFIX) for result in results)


@pytest.mark.asyncio
async def test_default_demo_stays_deterministic():
    summary = await run_demo()

    evidence = _evidence(summary)
    assert evidence
    assert not any(item.startswith(_LLM_PREFIX) for item in evidence)


def test_cli_defaults_to_the_deterministic_executor():
    assert main.parse_args(["demo"]).householder_executor == "deterministic"


def test_cli_accepts_the_llm_executor():
    assert main.parse_args(["demo", "--householder-executor", "llm"]).householder_executor == "llm"
