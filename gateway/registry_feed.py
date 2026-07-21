"""HTTP-only Minibook registry feed without importing its forge pipeline."""

from __future__ import annotations

import hashlib
import os
from typing import Any
from uuid import UUID

import aiohttp
from pydantic import BaseModel, ConfigDict

from agenten.agent_runtime.contracts import (
    AgentRuntimeResult,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.delivery.minibook_events import MinibookProjectionEvent


class MinibookProjectionFeedPage(BaseModel):
    """One cursor page from Captain's redacted, read-only projection feed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[MinibookProjectionEvent, ...]
    cursor: str
    has_more: bool


def factory_promotion_projection(
    block: dict[str, Any],
    job: dict[str, Any],
) -> MinibookProjectionEvent:
    """Build the strict public view of one committed Factory promotion."""

    return MinibookProjectionEvent.model_validate(
        {
            "schema": "captain.minibook-projection.v2",
            "event_id": block["event_id"],
            "correlation_id": job["correlation_id"],
            "causation_id": job.get("event_id"),
            "occurred_at": block["occurred_at"],
            "producer": "captain-gateway",
            "subject_id": f"subject:{block['job_id']}",
            "subject_version": block["subject_version"],
            "event_type": "capability.promoted",
            "payload": {
                "view": "validation",
                "template_id": "factory_capability_ready_to_use",
                "status_id": "ready_to_use",
                "actor_role_id": "captain_gateway",
            },
        }
    )


def runtime_result_projection(
    result: dict[str, Any],
) -> MinibookProjectionEvent | None:
    """Project only successful Codex builds representable by the v2 catalog."""

    validated = AgentRuntimeResult.model_validate(result)
    if (
        validated.producer != "agent-runtime"
        or validated.status is not RuntimeStatus.SUCCEEDED
        or validated.operation
        not in {RuntimeOperation.CODEX_RUN, RuntimeOperation.CODEX_RESUME}
    ):
        return None
    subject_digest = bytearray(
        hashlib.sha256(
            (
                "captain-runtime-subject:"
                f"{validated.correlation_id}:{validated.subject_id}"
            ).encode("utf-8")
        )
        .digest()[:16]
    )
    subject_digest[6] = (subject_digest[6] & 0x0F) | 0x40
    subject_digest[8] = (subject_digest[8] & 0x3F) | 0x80
    payload: dict[str, object] = {
        "view": "build",
        "template_id": "runtime_build_recorded",
        "status_id": "built",
        "actor_role_id": "codex_worker",
    }
    if validated.artifact_refs:
        payload["artifact_digest"] = f"sha256:{validated.artifact_refs[0].sha256}"
    return MinibookProjectionEvent.model_validate(
        {
            "schema": "captain.minibook-projection.v2",
            "event_id": validated.event_id,
            "correlation_id": validated.correlation_id,
            "causation_id": validated.command_id,
            "occurred_at": validated.occurred_at,
            "producer": "captain-gateway",
            "subject_id": f"subject:{UUID(bytes=bytes(subject_digest))}",
            "subject_version": validated.subject_version,
            "event_type": "codex.result",
            "payload": payload,
        }
    )


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
