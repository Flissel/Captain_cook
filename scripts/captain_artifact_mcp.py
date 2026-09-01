"""Minimal MCP server exposing Captain's content-addressed artifact store.

Captain's Hermes planner must return `artifact://sha256/<digest>` references
that Captain can actually resolve. A model driven through the Claude Code shim
has no way to persist one on its own, so it either inlines the plan or would
have to invent a digest -- and it rightly refuses to invent one.

This server gives it exactly two tools and nothing else:

    write_artifact(content, media_type) -> {"uri", "sha256", "media_type"}
    read_artifact(uri)                  -> the stored text

Storage layout matches ContentAddressedArtifactAdapter byte for byte:
``<root>/<digest[:2]>/<digest>``, written once, never overwritten.

Speaks MCP over stdio with newline-delimited JSON-RPC. Standard library only.

Environment:
    CAPTAIN_ARTIFACT_ROOT  required, e.g. <repo>/.captain-cook/artifacts/sha256
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "captain-artifacts"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "write_artifact",
        "description": (
            "Persist content in Captain's content-addressed artifact store and "
            "return a resolvable artifact:// reference. Use this for every plan "
            "body, decision log or blueprint instead of inlining it, and use the "
            "returned uri/sha256 verbatim -- never invent a digest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Exact bytes to store, as text."},
                "media_type": {
                    "type": "string",
                    "description": "IANA media type, e.g. application/json or text/markdown.",
                    "default": "application/json",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "read_artifact",
        "description": (
            "Read back an artifact by its artifact://sha256/<digest> uri, for "
            "example the prompt referenced by a runtime command."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "artifact://sha256/<digest>"},
            },
            "required": ["uri"],
        },
    },
    {
        "name": "get_contract_schema",
        "description": (
            "Return the exact JSON schema Captain validates a result against. "
            "Call this before composing a typed result so every required field "
            "is present and no unknown field is added -- the contracts reject "
            "both. Known names: captain.hermes-plan-result.v1."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schema": {
                    "type": "string",
                    "description": "Contract name, e.g. captain.hermes-plan-result.v1",
                },
            },
            "required": ["schema"],
        },
    },
]

_CONTRACTS = {
    "captain.hermes-plan-result.v1": ("agenten.agent_runtime.contracts", "HermesPlanResult"),
    "captain.agent-runtime-result.v1": ("agenten.agent_runtime.contracts", "AgentRuntimeResult"),
}


def get_contract_schema(schema: str) -> dict[str, Any]:
    """Resolve a contract name to the live Pydantic model's JSON schema.

    Read from the repository itself rather than a copy, so the schema can never
    drift from what the runtime actually enforces.
    """
    target = _CONTRACTS.get(schema.strip())
    if target is None:
        raise RuntimeError(
            f"unknown contract {schema!r}; known: {', '.join(sorted(_CONTRACTS))}"
        )
    repo_root = os.environ.get("CAPTAIN_REPO_ROOT", "").strip()
    if not repo_root:
        # artifact root is <repo>/.captain-cook/artifacts/sha256
        repo_root = str(artifact_root().parents[2])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    module_name, class_name = target
    module = __import__(module_name, fromlist=[class_name])
    model = getattr(module, class_name)
    return model.model_json_schema()


def artifact_root() -> Path:
    raw = os.environ.get("CAPTAIN_ARTIFACT_ROOT", "").strip()
    if not raw:
        raise RuntimeError("CAPTAIN_ARTIFACT_ROOT is not set")
    return Path(raw)


def _digest_path(root: Path, digest: str) -> Path:
    return root / digest[:2] / digest


def write_artifact(content: str, media_type: str = "application/json") -> dict[str, str]:
    payload = content.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    target = _digest_path(artifact_root(), digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Content-addressed: identical bytes are already there. Rewriting would
        # only risk corrupting a good artifact, so treat it as success.
        if target.read_bytes() != payload:
            raise RuntimeError("artifact digest collision or corrupted store entry")
    else:
        target.write_bytes(payload)
    return {
        "uri": f"artifact://sha256/{digest}",
        "sha256": digest,
        "media_type": media_type,
    }


def read_artifact(uri: str) -> str:
    prefix = "artifact://sha256/"
    if not uri.startswith(prefix):
        raise RuntimeError("uri must look like artifact://sha256/<digest>")
    digest = uri[len(prefix):]
    if _SHA256.fullmatch(digest) is None:
        raise RuntimeError("uri does not carry a sha-256 digest")
    target = _digest_path(artifact_root(), digest)
    if not target.is_file():
        raise RuntimeError("artifact is not in the store")
    return target.read_bytes().decode("utf-8", errors="replace")


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "write_artifact":
        result = write_artifact(
            str(arguments.get("content", "")),
            str(arguments.get("media_type") or "application/json"),
        )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if name == "read_artifact":
        return {"content": [{"type": "text", "text": read_artifact(str(arguments.get("uri", "")))}]}
    if name == "get_contract_schema":
        schema = get_contract_schema(str(arguments.get("schema", "")))
        return {"content": [{"type": "text", "text": json.dumps(schema)}]}
    raise RuntimeError(f"unknown tool {name}")


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": requested or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        try:
            return {"jsonrpc": "2.0", "id": message_id, "result": call_tool(name, arguments)}
        except Exception as exc:  # surfaced to the model, not swallowed
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if message_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle(message)
        except Exception as exc:  # never die on one bad message
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
