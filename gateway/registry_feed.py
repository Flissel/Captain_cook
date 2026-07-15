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
