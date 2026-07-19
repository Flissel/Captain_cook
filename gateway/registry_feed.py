"""HTTP-only Minibook registry feed without importing its forge pipeline."""

from __future__ import annotations

import os
from typing import Any

import aiohttp


async def _post_registry(payload: dict[str, Any]) -> None:
    base_url = os.getenv("MINIBOOK_URL", "http://localhost:8080").rstrip("/")
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/api/v1/registry", json=payload) as response:
            response.raise_for_status()


async def mirror_validated_batch(block: dict[str, Any]) -> None:
    if block.get("block_type") != "batch_done" or block.get("status") != "succeeded":
        return
    data = block.get("data", {})
    payload = {
        "team_key": data["batch_id"],
        "run_id": str(data.get("run_id", data["batch_id"])),
        "eval_score": int(data.get("eval_score", 10)),
        "eval_reason": str(data.get("eval_reason", "Ledger validation succeeded")),
        "status": "validated",
        "todo_status": "completed",
        "output_dir": data.get("output_dir"),
        "tools_py_path": data.get("artifact_ref"),
        "mcp_servers": list(data.get("validated_tools", [])),
        "capabilities": list(data.get("capabilities", [])),
        "agent_name": data.get("agent_name"),
    }
    await _post_registry(payload)


async def mirror_captain_projection(block: dict[str, Any]) -> None:
    """Project only Captain-promoted factory capabilities into Minibook."""

    if block.get("event_type") != "factory_lifecycle" or block.get("phase") != "capability_promoted":
        await mirror_validated_batch(block)
        return
    if block.get("status") != "succeeded":
        return
    await _post_registry(
        {
            "team_key": block["capability_id"],
            "run_id": block["job_id"],
            "eval_score": 10,
            "eval_reason": "Captain promoted the factory capability after asserted evidence.",
            "status": "validated",
            "todo_status": "completed",
            "output_dir": None,
            "tools_py_path": None,
            "mcp_servers": [],
            "capabilities": [block["capability_id"]],
            "agent_name": None,
        }
    )
