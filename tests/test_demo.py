import json

import pytest

from agenten.demo import run_demo


@pytest.mark.asyncio
async def test_run_demo_writes_an_inspectable_success_artifact(tmp_path):
    output = tmp_path / "evidence" / "demo-run.json"

    summary = await run_demo(output)

    assert summary.success is True
    assert summary.done_count == 2
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["success"] is True


@pytest.mark.asyncio
async def test_run_demo_contains_only_terminal_subproblems():
    summary = await run_demo()

    subproblems = [block for block in summary.blocks if block["block_type"] == "subproblem"]

    assert {block["status"] for block in subproblems} == {"done"}
    assert all("description" in block for block in subproblems)
